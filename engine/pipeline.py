"""
Core generation pipeline — Tatterveil Scene Studio.

Steps:
  1. estimate_duration()     — word-count → video minutes
  2. compute_scene_plan()    — two-rate scene count calculation
  3. split_and_prompt()      — single LLM call: splits script + generates prompts
  4. generate_all_images()   — parallel DALL-E 3 image generation
"""

import base64
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from openai import OpenAI

import config
from engine.style_guide import (
    SCENE_TYPE_NAMES,
    build_scene_split_system_prompt,
)

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


# ─── Content-moderation handling ─────────────────────────────────────────────

# User-facing copy shown whenever OpenAI blocks script/scene content for safety.
MODERATION_USER_MESSAGE = (
    "Your script contains content that was blocked by the AI safety system. "
    "Please edit the flagged wording and try again."
)
MODERATION_SCENE_MESSAGE = (
    "This scene was blocked by the AI safety system. "
    "Edit the wording for this part of the script, then regenerate."
)

# Substrings that identify a safety/moderation rejection across text + image APIs.
_MODERATION_MARKERS = (
    "moderation_blocked",
    "content_policy_violation",
    "content_filter",
    "safety system",
    "safety_system",
    "rejected by the safety",
    "image_generation_user_error",
    "your request was rejected",
)


class ContentModerationError(RuntimeError):
    """Raised when OpenAI rejects script/scene content for safety reasons."""

    def __init__(self, message: str = MODERATION_USER_MESSAGE, *, original: Exception | None = None):
        super().__init__(message)
        self.original = original


def is_moderation_error(exc: Exception) -> bool:
    """Best-effort detection of an OpenAI safety/moderation rejection."""
    if isinstance(exc, ContentModerationError):
        return True
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.lower() in (
        "moderation_blocked",
        "content_policy_violation",
    ):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _MODERATION_MARKERS)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


# ─── 1. Duration estimation ───────────────────────────────────────────────────

def estimate_duration(script: str, voice_speed: float = 1.0) -> float:
    """Return estimated video duration in minutes (preview only).

    Estimated from character count (~CHARS_PER_MINUTE characters per minute at a
    normal speaking pace), which tracks narration length more closely than word
    count. Real duration still comes from the generated voice-over. Slower
    narration speed (lower ElevenLabs speed value) produces longer audio, so the
    estimate scales inversely with ``voice_speed``.
    """
    chars = len(script or "")
    raw = chars / config.CHARS_PER_MINUTE
    speed = max(0.25, min(1.0, float(voice_speed or 1.0)))
    raw = raw / speed
    return max(raw, config.MIN_DURATION)


# ─── 2. Scene plan calculation ────────────────────────────────────────────────

def compute_scene_plan(
    duration: float,
    first_rate: int,
    rest_rate: int,
) -> dict:
    """
    Return a scene plan dict with total scene count and timing breakdown.

    first_rate: scenes per minute for the first FIRST_SEGMENT minutes
    rest_rate:  scenes per minute for remaining duration
    """
    first_seg = min(duration, float(config.FIRST_SEGMENT))
    rest_seg = max(0.0, duration - config.FIRST_SEGMENT)

    first_scenes = max(1, round(first_seg * first_rate))
    rest_scenes = round(rest_seg * rest_rate)
    total = first_scenes + rest_scenes

    return {
        "duration_minutes": round(duration, 2),
        "duration_seconds": round(duration * 60, 1),
        "first_segment_minutes": round(first_seg, 2),
        "rest_segment_minutes": round(rest_seg, 2),
        "first_segment_scenes": first_scenes,
        "rest_segment_scenes": rest_scenes,
        "total_scenes": total,
        "first_rate": first_rate,
        "rest_rate": rest_rate,
    }


# ─── 3. LLM: script split + prompt generation ────────────────────────────────

def _robust_parse(raw: str) -> list[dict]:
    """Parse LLM response into a list of scene dicts with multiple fallbacks."""
    # Clean markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()

    # Try: {"scenes": [...]}
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            for key in ("scenes", "scene_list", "data", "items", "results"):
                if key in obj and isinstance(obj[key], list):
                    return obj[key]
        if isinstance(obj, list):
            return obj
    except json.JSONDecodeError:
        pass

    # Try: extract outermost [...] array
    arr_match = re.search(r"\[[\s\S]*\]", cleaned)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except json.JSONDecodeError:
            pass

    # Try: extract outermost {...} object
    obj_match = re.search(r"\{[\s\S]*\}", cleaned)
    if obj_match:
        try:
            obj = json.loads(obj_match.group(0))
            if isinstance(obj, dict):
                for key in ("scenes", "scene_list", "data", "items"):
                    if key in obj and isinstance(obj[key], list):
                        return obj[key]
        except json.JSONDecodeError:
            pass

    raise ValueError("Could not parse scene JSON from LLM response")


def _equal_chunks(start: int, end: int, n: int) -> list[tuple[int, int]]:
    """Split index range [start, end) into n roughly-equal (a, b) pairs."""
    length = max(0, end - start)
    if n <= 0:
        return []
    boundaries = [start + round(i * length / n) for i in range(n + 1)]
    return [(boundaries[i], boundaries[i + 1]) for i in range(n)]


def compute_equal_segments(script: str, scene_plan: dict) -> list[dict]:
    """
    Deterministically split the script into scenes so that:
      • Each scene **within** the first 5 minutes covers the same number of words.
      • Each scene **within** the remaining duration covers the same number of words.
      • Time per scene is equal within each segment.

    Returns a list of dicts with: scene_number, slot_number, start_time, end_time,
    duration, script_segment, word_count.
    """
    tokens = script.split()
    n_total = len(tokens)
    first_n = int(scene_plan.get("first_segment_scenes", 0) or 0)
    rest_n = int(scene_plan.get("rest_segment_scenes", 0) or 0)
    first_min = float(scene_plan.get("first_segment_minutes", 0) or 0.0)
    rest_min = float(scene_plan.get("rest_segment_minutes", 0) or 0.0)
    total_min = first_min + rest_min

    if n_total == 0 or total_min <= 0 or (first_n + rest_n) == 0:
        return []

    # Word budget per segment, proportional to time.
    if rest_n > 0 and rest_min > 0:
        split_idx = round(n_total * first_min / total_min)
    else:
        split_idx = n_total
    split_idx = max(0, min(n_total, split_idx))

    segments: list[dict] = []
    scene_no = 1

    # ── First segment ──
    if first_n > 0:
        chunks = _equal_chunks(0, split_idx, first_n)
        seg_secs = first_min * 60.0
        per_scene_secs = seg_secs / first_n
        for i, (a, b) in enumerate(chunks):
            start_t = i * per_scene_secs
            end_t = seg_secs if i == first_n - 1 else (i + 1) * per_scene_secs
            seg_text = " ".join(tokens[a:b]).strip()
            segments.append(
                {
                    "scene_number": scene_no,
                    "slot_number": scene_no,
                    "start_time": round(start_t, 2),
                    "end_time": round(end_t, 2),
                    "duration": round(end_t - start_t, 2),
                    "script_segment": seg_text,
                    "word_count": (b - a),
                }
            )
            scene_no += 1

    # ── Rest segment ──
    if rest_n > 0 and rest_min > 0:
        chunks = _equal_chunks(split_idx, n_total, rest_n)
        base = first_min * 60.0
        seg_secs = rest_min * 60.0
        per_scene_secs = seg_secs / rest_n
        for i, (a, b) in enumerate(chunks):
            start_t = base + i * per_scene_secs
            end_t = base + seg_secs if i == rest_n - 1 else base + (i + 1) * per_scene_secs
            seg_text = " ".join(tokens[a:b]).strip()
            segments.append(
                {
                    "scene_number": scene_no,
                    "slot_number": scene_no,
                    "start_time": round(start_t, 2),
                    "end_time": round(end_t, 2),
                    "duration": round(end_t - start_t, 2),
                    "script_segment": seg_text,
                    "word_count": (b - a),
                }
            )
            scene_no += 1

    return segments


# ─── 3b. Sentence-aligned scene grouping (per-minute scene budget) ───────────

def _minute_index(sentence: dict) -> int:
    """Calendar minute bucket from when the sentence *starts* in the audio."""
    return int(float(sentence.get("start_time", 0.0)) // 60)


def _partition_sentences(sentences: list[dict], k: int) -> list[list[dict]]:
    """Split consecutive sentences into k contiguous, as-equal-as-possible groups."""
    n = len(sentences)
    if n == 0 or k <= 0:
        return []
    k = min(k, n)
    if k == 1:
        return [list(sentences)]
    if k == n:
        return [[s] for s in sentences]

    boundaries = [round(i * n / k) for i in range(k + 1)]
    groups: list[list[dict]] = []
    for i in range(k):
        group = list(sentences[boundaries[i]:boundaries[i + 1]])
        if group:
            groups.append(group)
    return groups


def _build_minute_plan(
    sentences: list[dict],
    scene_plan: dict,
    audio_duration: float,
) -> list[dict]:
    """Bucket sentences by calendar minute; each minute gets up to `rate` scenes."""
    first_rate = max(1, int(scene_plan.get("first_rate", 1) or 1))
    rest_rate = max(1, int(scene_plan.get("rest_rate", 1) or 1))

    buckets: dict[int, list[dict]] = {}
    for s in sentences:
        buckets.setdefault(_minute_index(s), []).append(s)

    max_minute = max(0, int(float(audio_duration) // 60))
    plan: list[dict] = []
    for minute in range(max_minute + 1):
        bucket = buckets.get(minute)
        if not bucket:
            continue
        rate = first_rate if minute < int(config.FIRST_SEGMENT) else rest_rate
        target = min(rate, len(bucket))
        plan.append({"minute": minute, "sentences": bucket, "target": target})
    return plan


def _normalize_segments(segments: list[dict], audio_duration: float) -> list[dict]:
    """Ensure monotonic timeline; last scene ends exactly at audio length."""
    if not segments:
        return segments
    out: list[dict] = []
    for i, seg in enumerate(segments):
        start = float(seg["start_time"])
        end = float(seg["end_time"])
        if i == 0:
            start = 0.0
        elif out:
            prev_end = float(out[-1]["end_time"])
            if start < prev_end:
                start = prev_end
        if end < start:
            end = start
        row = dict(seg)
        row["start_time"] = round(start, 3)
        row["end_time"] = round(end, 3)
        row["duration"] = round(end - start, 3)
        out.append(row)
    out[-1]["end_time"] = round(float(audio_duration), 3)
    out[-1]["duration"] = round(out[-1]["end_time"] - out[-1]["start_time"], 3)
    return out


def build_scene_segments_from_sentences(
    title: str,
    sentences: list[dict],
    scene_plan: dict,
    audio_duration: float | None = None,
) -> list[dict]:
    """
    Group whole sentences into scenes using real voice-over timing.

    • Sentences bucketed by calendar minute (by start_time).
    • Each minute → exactly `target` scenes (rate capped by sentence count).
    • Deterministic equal sentence-count split within each minute (no LLM).
    • Scene start/end = first sentence start → last sentence end (exact span).
    """
    del title  # reserved for future use; grouping is fully deterministic.
    sents = sorted(sentences, key=lambda s: int(s.get("sentence_index", 0)))
    if not sents:
        return []

    duration = float(
        audio_duration
        if audio_duration is not None
        else scene_plan.get("duration_seconds", 0)
    )
    if duration <= 0:
        duration = float(sents[-1].get("end_time", 0))

    plan = _build_minute_plan(sents, scene_plan, duration)

    final_groups: list[list[dict]] = []
    for entry in plan:
        final_groups.extend(_partition_sentences(entry["sentences"], entry["target"]))

    segments: list[dict] = []
    for num, group in enumerate(g for g in final_groups if g):
        scene_no = num + 1
        text = " ".join(s["text"].strip() for s in group).strip()
        start_t = float(group[0]["start_time"])
        end_t = float(group[-1]["end_time"])
        segments.append(
            {
                "scene_number": scene_no,
                "slot_number": scene_no,
                "start_time": round(start_t, 3),
                "end_time": round(end_t, 3),
                "duration": round(end_t - start_t, 3),
                "script_segment": text,
                "word_count": sum(len(s["text"].split()) for s in group),
                "sentence_indices": [int(s["sentence_index"]) for s in group],
            }
        )

    segments = _normalize_segments(segments, duration)
    logger.info(
        "Built %s scenes from %s sentences across %s minute(s) (audio %.1fs).",
        len(segments), len(sents), len(plan), duration,
    )
    return segments


def _sanitize_scene_enrichment(row: dict, abstraction_enabled: bool) -> dict:
    """When abstract mode is off, strip any LLM hallucinated abstraction fields."""
    if abstraction_enabled:
        return row
    out = dict(row)
    out["abstraction_mode"] = False
    out.pop("abstraction_concept", None)
    out.pop("absence_technique", None)
    st = int(out.get("scene_type") or 0)
    if st == 4:
        # Remap accidental Type 4 → Environmental Wide (safe literal default).
        out["scene_type"] = 2
        out["scene_type_name"] = SCENE_TYPE_NAMES.get(2, "Environmental Wide Shot")
    return out


def _merge_locked_scenes(
    pre_segments: list[dict],
    llm_scenes: list[dict],
    abstraction_enabled: bool,
) -> list[dict]:
    """Keep deterministic timing/script; take LLM enrichment fields only."""
    by_num: dict[int, dict] = {}
    for row in llm_scenes:
        try:
            n = int(row.get("scene_number") or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            by_num[n] = row

    merged: list[dict] = []
    for pre in pre_segments:
        n = int(pre["scene_number"])
        enrich = by_num.get(n, {})
        row = dict(enrich)
        row["scene_number"] = pre["scene_number"]
        row["slot_number"] = pre["slot_number"]
        row["start_time"] = pre["start_time"]
        row["end_time"] = pre["end_time"]
        row["duration"] = pre["duration"]
        row["script_segment"] = pre["script_segment"]
        row["word_count"] = pre["word_count"]
        if pre.get("sentence_indices") is not None:
            row["sentence_indices"] = pre["sentence_indices"]
        row = _sanitize_scene_enrichment(row, abstraction_enabled)
        merged.append(row)
    return merged


def split_and_prompt(
    title: str,
    script: str,
    scene_plan: dict,
    pre_segments: list[dict] | None = None,
    abstraction_enabled: bool = False,
) -> list[dict]:
    """
    Two-stage scene build:

    1. Use pre-built scene segments (sentence-aligned to the real voice-over via
       build_scene_segments_from_sentences) when provided. Otherwise fall back to
       the deterministic equal-word split (compute_equal_segments).
    2. Ask the LLM to enrich each pre-split scene with scene_type, time_period,
       abstraction info, and the image prompt — without re-splitting the script.
    """
    duration_minutes = scene_plan["duration_minutes"]
    duration_seconds = scene_plan["duration_seconds"]

    if pre_segments is None:
        pre_segments = compute_equal_segments(script, scene_plan)
    if not pre_segments:
        raise RuntimeError(
            "Could not split script into scenes — empty script or invalid scene plan."
        )

    # The locked segments are the real scene count (sentence grouping may differ
    # slightly from the planned target), so the LLM is told the true number.
    scene_count = len(pre_segments)

    system_prompt = build_scene_split_system_prompt(
        scene_count=scene_count,
        duration_minutes=duration_minutes,
        duration_seconds=duration_seconds,
        abstraction_enabled=abstraction_enabled,
    )

    client = _get_client()

    literal_note = ""
    if not abstraction_enabled:
        literal_note = (
            "\n\nCRITICAL: Abstract/conceptual mode is OFF for this project. "
            "Use only literal scene types 1, 2, 3, or 5. Do not output "
            "abstraction_mode, abstraction_concept, or absence_technique fields. "
            "Depict concrete real-world subjects only.\n"
        )

    batch_size = max(1, int(config.PROMPT_BATCH_SIZE))
    batches = [
        pre_segments[i:i + batch_size]
        for i in range(0, len(pre_segments), batch_size)
    ]
    all_merged: list[dict] = []

    for batch_idx, batch in enumerate(batches):
        locked_payload = [
            {
                "scene_number": p["scene_number"],
                "start_time": p["start_time"],
                "end_time": p["end_time"],
                "duration": p["duration"],
                "script_segment": p["script_segment"],
            }
            for p in batch
        ]
        batch_note = ""
        if len(batches) > 1:
            batch_note = (
                f"\nBATCH {batch_idx + 1} of {len(batches)} — enrich ONLY the "
                f"{len(batch)} scenes listed below (scene numbers are global).\n"
            )

        user_msg = (
            f"VIDEO TITLE: {title}\n"
            f"TOTAL DURATION: {duration_minutes:.2f} minutes ({duration_seconds:.0f} seconds)\n"
            f"SCENE COUNT (entire video): {scene_count}\n"
            f"{batch_note}\n"
            "IMPORTANT: Scenes are already split on whole script sentences with exact "
            "voice-over timestamps. Do NOT change scene_number, start_time, end_time, "
            "duration, or script_segment. Return them verbatim. Your job is to add the "
            "documentary classification and Tatterveil image prompt for each scene.\n"
            f"{literal_note}\n"
            f"LOCKED SCENES (use exactly these segments and timings):\n"
            f"{json.dumps(locked_payload, ensure_ascii=False)}\n\n"
            f"FULL SCRIPT (context only — for tone / continuity):\n{script.strip()}"
        )

        batch_merged: list[dict] | None = None
        for attempt in range(config.MAX_RETRIES):
            try:
                response = client.chat.completions.create(
                    model=config.TEXT_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.25,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content or ""
                llm_scenes = _robust_parse(raw)
                logger.info(
                    "LLM batch %s/%s returned %s enriched scenes (locked %s)",
                    batch_idx + 1, len(batches), len(llm_scenes), len(batch),
                )
                batch_merged = _merge_locked_scenes(batch, llm_scenes, abstraction_enabled)
                break
            except Exception as exc:
                if is_moderation_error(exc):
                    logger.error("Scene-prompt generation blocked by safety system: %s", exc)
                    raise ContentModerationError(original=exc) from exc
                logger.warning(
                    "LLM batch %s attempt %s/%s failed: %s",
                    batch_idx + 1, attempt + 1, config.MAX_RETRIES, exc,
                )
                if attempt < config.MAX_RETRIES - 1:
                    time.sleep(config.RETRY_DELAY * (attempt + 1))

        if batch_merged is None:
            raise RuntimeError(
                f"LLM failed to generate scene prompts for batch {batch_idx + 1} "
                f"after all retries."
            )
        all_merged.extend(batch_merged)

    return all_merged


# ─── 4. Image generation ──────────────────────────────────────────────────────

def _build_final_prompt(scene: dict) -> str:
    """
    Return the final prompt string for gpt-image-2.
    gpt-image-2 has no separate negative_prompt field, so we fold the
    negative constraints inline as explicit avoidance language.
    """
    pos = scene.get("prompt", "")
    neg = scene.get("negative_prompt", "")
    if neg:
        pos = f"{pos.rstrip('. ')}. AVOID in this image: {neg}."
    return pos


def image_output_path(scene: dict, project_dir: Path) -> Path:
    """Resolved images/ path for this scene row (uses image_filename when set)."""
    from engine.scene_utils import image_filename_for_scene

    images_dir = project_dir / "images"
    fn = scene.get("image_filename") or image_filename_for_scene(scene)
    return images_dir / fn


def generate_image(
    scene: dict,
    quality: str,
    resolution: str,
    project_dir: Path,
    *,
    auto_rephrase_on_moderation: bool = False,
) -> tuple[Path, float]:
    """
    Generate one scene image via gpt-image-2.

    Returns (output_path, elapsed_seconds). Retries up to MAX_RETRIES on
    transient errors with exponential back-off.

    When auto_rephrase_on_moderation is True (initial project generation), a
    safety block triggers up to IMAGE_MODERATION_RETRIES LLM rewrites before
    surfacing the friendly moderation error to the UI.
    """
    images_dir = project_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    scene_num = int(scene.get("slot_number") or scene.get("scene_number") or 0)
    out_path = image_output_path(scene, project_dir)

    if resolution not in config.RESOLUTION_PRESETS:
        resolution = config.DEFAULT_RESOLUTION
    if quality not in config.QUALITY_OPTIONS:
        quality = config.DEFAULT_QUALITY

    client = _get_client()
    started_at = time.monotonic()
    safety_rephrases = 0

    while True:
        prompt = _build_final_prompt(scene)
        last_exc: Exception | None = None
        rephrase_requested = False

        for attempt in range(config.MAX_RETRIES):
            try:
                attempt_start = time.monotonic()
                response = client.images.generate(
                    model=config.IMAGE_MODEL,
                    prompt=prompt,
                    n=1,
                    size=resolution,
                    quality=quality,
                )
                image_b64 = response.data[0].b64_json
                image_bytes = base64.b64decode(image_b64)
                out_path.write_bytes(image_bytes)

                try:
                    from engine.thumbnails import ensure_thumbnail

                    ensure_thumbnail(out_path)
                except Exception as exc:
                    logger.warning("Preview thumbnail failed for %s: %s", out_path.name, exc)

                elapsed = time.monotonic() - started_at
                attempt_time = time.monotonic() - attempt_start
                logger.info(
                    f"Scene {scene_num:03d} saved → {out_path.name}  "
                    f"({attempt_time:.1f}s, {resolution}, q={quality})"
                )
                return out_path, elapsed

            except Exception as exc:
                last_exc = exc
                if is_moderation_error(exc):
                    if (
                        auto_rephrase_on_moderation
                        and safety_rephrases < config.IMAGE_MODERATION_RETRIES
                    ):
                        safety_rephrases += 1
                        logger.info(
                            "Scene %s blocked by safety — auto-rephrase %s/%s",
                            scene_num,
                            safety_rephrases,
                            config.IMAGE_MODERATION_RETRIES,
                        )
                        new_pos, new_neg = rephrase_prompt_for_safety(
                            scene, safety_rephrases
                        )
                        scene["prompt"] = new_pos
                        scene["negative_prompt"] = new_neg
                        rephrase_requested = True
                        break
                    logger.error(
                        "Scene %s image blocked by safety system: %s", scene_num, exc
                    )
                    raise ContentModerationError(
                        MODERATION_SCENE_MESSAGE, original=exc
                    ) from exc
                logger.warning(
                    f"Image attempt {attempt + 1}/{config.MAX_RETRIES} "
                    f"for scene {scene_num} failed: {exc}"
                )
                if attempt < config.MAX_RETRIES - 1:
                    time.sleep(config.RETRY_DELAY * (attempt + 1))

        if rephrase_requested:
            continue

        raise RuntimeError(
            f"Image generation failed for scene {scene_num} after "
            f"{config.MAX_RETRIES} attempts: {last_exc}"
        )


def generate_all_images(
    scenes: list[dict],
    quality: str,
    resolution: str,
    project_dir: Path,
    on_progress: Callable[[int, int, dict], None] | None = None,
    *,
    auto_rephrase_on_moderation: bool = False,
) -> tuple[list[dict], dict]:
    """
    Generate images for all scenes in parallel.

    Returns:
        (updated_scenes, timing_summary)
        timing_summary keys:
            total_scenes, successful, failed,
            wall_clock_seconds, sum_image_seconds,
            avg_image_seconds, fastest_seconds, slowest_seconds,
            per_scene_seconds (dict of {scene_number: seconds})
    """
    total = len(scenes)
    results: dict[str, dict] = {s["entry_id"]: s for s in scenes}
    per_scene_times: dict[str, float] = {}

    batch_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        future_to_eid = {
            pool.submit(
                generate_image,
                scene,
                quality,
                resolution,
                project_dir,
                auto_rephrase_on_moderation=auto_rephrase_on_moderation,
            ):
                scene["entry_id"]
            for scene in scenes
        }

        done = 0
        for future in as_completed(future_to_eid):
            eid = future_to_eid[future]
            done += 1
            try:
                img_path, elapsed = future.result()
                results[eid]["image_path"]      = img_path.relative_to(project_dir).as_posix()
                results[eid]["image_filename"]  = img_path.name
                results[eid]["image_status"]    = "done"
                results[eid]["image_seconds"]   = round(elapsed, 2)
                per_scene_times[eid]            = elapsed
            except Exception as exc:
                results[eid]["image_status"] = "error"
                if is_moderation_error(exc):
                    results[eid]["image_error"] = MODERATION_SCENE_MESSAGE
                    results[eid]["image_error_kind"] = "moderation"
                else:
                    results[eid]["image_error"] = (
                        "Image generation failed. Please try regenerating this scene."
                    )
                    results[eid]["image_error_kind"] = "generic"
                logger.error(f"Entry {eid} image failed permanently: {exc}")

            if on_progress:
                on_progress(done, total, results[eid])

    wall_clock = time.monotonic() - batch_start
    successful = len(per_scene_times)
    failed     = total - successful
    sum_time   = sum(per_scene_times.values()) if per_scene_times else 0.0
    avg_time   = (sum_time / successful) if successful else 0.0
    fastest    = min(per_scene_times.values()) if per_scene_times else 0.0
    slowest    = max(per_scene_times.values()) if per_scene_times else 0.0

    summary = {
        "total_scenes":        total,
        "successful":          successful,
        "failed":              failed,
        "wall_clock_seconds":  round(wall_clock, 2),
        "sum_image_seconds":   round(sum_time, 2),
        "avg_image_seconds":   round(avg_time, 2),
        "fastest_seconds":     round(fastest, 2),
        "slowest_seconds":     round(slowest, 2),
        "per_scene_seconds":   {str(n): round(t, 2) for n, t in per_scene_times.items()},
        "resolution":          resolution,
        "quality":             quality,
        "parallel_workers":    config.MAX_WORKERS,
    }

    logger.info(
        f"━━ Generation complete ━━ "
        f"{successful}/{total} ok, {failed} failed | "
        f"wall {wall_clock:.1f}s | avg {avg_time:.1f}s/img | "
        f"fastest {fastest:.1f}s | slowest {slowest:.1f}s"
    )

    return [results[s["entry_id"]] for s in scenes], summary


# ─── 5. Prompt refinement (regenerate image) ─────────────────────────────────

SAFETY_REPHRASE_SYSTEM = """You revise image-generation prompts that were blocked by an AI safety filter.

The user provides the script lines for one documentary scene plus the prompt that was rejected.

Return JSON:
  "prompt": string — full revised positive prompt (at least ~80 words), same Tatterveil atmospheric photorealistic documentary look, period, and composition intent.
  "negative_prompt": string or null — revised negative constraints.

Rules:
- Remove or soften wording that could trigger moderation (graphic violence, explicit content, inflammatory depictions stated bluntly).
- Keep 16:9 landscape photorealistic documentary language; no watermarks; no text in image.
- Do not change the historical/subject focus unless required for safety.
- Each rewrite attempt should use noticeably different phrasing than the previous prompt.
"""


def rephrase_prompt_for_safety(scene: dict, attempt: int) -> tuple[str, str | None]:
    """Rewrite an image prompt so it is less likely to trip OpenAI safety filters."""
    client = _get_client()
    payload = {
        "attempt": attempt,
        "script_segment": scene.get("script_segment") or "",
        "prompt": scene.get("prompt") or "",
        "negative_prompt": scene.get("negative_prompt") or "",
        "instruction": (
            "Rewrite the prompt to pass content moderation while preserving the "
            "documentary scene intent. Use neutral, archival, atmospheric wording."
        ),
    }
    for try_no in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": SAFETY_REPHRASE_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0.45 + (attempt - 1) * 0.1,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
            pos = (data.get("prompt") or "").strip()
            neg = data.get("negative_prompt")
            if isinstance(neg, str):
                neg = neg.strip() or None
            else:
                neg = None
            if not pos:
                raise ValueError("empty prompt from safety rephrase")
            return pos, neg
        except Exception as exc:
            if is_moderation_error(exc):
                raise ContentModerationError(
                    MODERATION_SCENE_MESSAGE, original=exc
                ) from exc
            logger.warning("rephrase_prompt_for_safety attempt %s failed: %s", try_no + 1, exc)
            if try_no < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (try_no + 1))
    raise RuntimeError("LLM failed to rephrase prompt for safety after retries.")


REFINE_PROMPT_SYSTEM = """You revise image-generation prompts for a documentary-style YouTube visual pipeline.

The user will give an EXISTING positive prompt, optional negative constraints, the script lines for that scene, and NEW instructions.

Return a JSON object with keys:
  "prompt": string — full revised positive prompt (at least ~80 words), same overall Tatterveil / atmospheric photorealistic documentary look as the original, but incorporating the user's new instructions.
  "negative_prompt": string or null — revised negative constraints; if minor change only, reuse and lightly adjust the previous negatives; use null only if no negatives are needed.

Rules:
- Preserve period, scene type, and composition intent from the original unless the user explicitly asks to change them.
- Keep 16:9 landscape, photorealistic documentary language, no watermarks, no text in image.
- Apply the user's new instructions faithfully while staying stylistically consistent.
"""


def refine_prompt_for_regeneration(
    previous_prompt: str,
    previous_negative: str | None,
    script_segment: str,
    user_instructions: str,
) -> tuple[str, str | None]:
    """Ask the text model to merge the old prompt with new user instructions."""
    client = _get_client()
    payload = {
        "previous_prompt": previous_prompt,
        "previous_negative_prompt": previous_negative or "",
        "script_segment": script_segment,
        "user_instructions": user_instructions.strip(),
    }
    for attempt in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": REFINE_PROMPT_SYSTEM},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                temperature=0.35,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
            pos = (data.get("prompt") or "").strip()
            neg = data.get("negative_prompt")
            if isinstance(neg, str):
                neg = neg.strip() or None
            else:
                neg = None
            if not pos:
                raise ValueError("empty prompt from model")
            return pos, neg
        except Exception as exc:
            if is_moderation_error(exc):
                logger.error("Regeneration prompt blocked by safety system: %s", exc)
                raise ContentModerationError(
                    MODERATION_SCENE_MESSAGE, original=exc
                ) from exc
            logger.warning("refine_prompt attempt %s failed: %s", attempt + 1, exc)
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError("LLM failed to refine prompt after retries.")
