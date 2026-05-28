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
from engine.style_guide import SCENE_SPLIT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


# ─── 1. Duration estimation ───────────────────────────────────────────────────

def estimate_duration(script: str) -> float:
    """Return estimated video duration in minutes based on word count."""
    words = len(script.split())
    raw = words / config.WORDS_PER_MINUTE
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


def split_and_prompt(
    title: str,
    script: str,
    scene_plan: dict,
) -> list[dict]:
    """
    Two-stage scene build:

    1. Deterministically split the script into equal-word segments using
       compute_equal_segments() — guarantees equal scene length within each
       segment of the timeline.
    2. Ask the LLM to enrich each pre-split scene with scene_type, time_period,
       abstraction info, and the image prompt — without re-splitting the script.
    """
    scene_count = scene_plan["total_scenes"]
    duration_minutes = scene_plan["duration_minutes"]
    duration_seconds = scene_plan["duration_seconds"]

    pre_segments = compute_equal_segments(script, scene_plan)
    if not pre_segments:
        raise RuntimeError(
            "Could not split script into scenes — empty script or invalid scene plan."
        )

    # Compact list passed to the LLM: it must NOT change scene_number, timing or text.
    locked_payload = [
        {
            "scene_number": p["scene_number"],
            "start_time": p["start_time"],
            "end_time": p["end_time"],
            "duration": p["duration"],
            "script_segment": p["script_segment"],
        }
        for p in pre_segments
    ]

    system_prompt = SCENE_SPLIT_SYSTEM_PROMPT.format(
        scene_count=scene_count,
        duration_minutes=duration_minutes,
        duration_seconds=duration_seconds,
    )

    user_msg = (
        f"VIDEO TITLE: {title}\n"
        f"TOTAL DURATION: {duration_minutes:.2f} minutes ({duration_seconds:.0f} seconds)\n"
        f"SCENE COUNT: {scene_count}\n\n"
        "IMPORTANT: The script has already been split into equally-sized scenes. "
        "Do NOT change scene_number, start_time, end_time, duration, or script_segment. "
        "Return them verbatim. Your job is to add the documentary classification and "
        "Tatterveil image prompt for each scene.\n\n"
        f"LOCKED SCENES (use exactly these segments and timings):\n"
        f"{json.dumps(locked_payload, ensure_ascii=False)}\n\n"
        f"FULL SCRIPT (context only — for tone / continuity):\n{script.strip()}"
    )

    client = _get_client()

    for attempt in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.25,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            llm_scenes = _robust_parse(raw)
            logger.info(
                "LLM returned %s enriched scenes (locked %s)",
                len(llm_scenes), len(pre_segments),
            )

            # Merge: keep pre-computed timing + script_segment, take enrichment from LLM.
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
                # Locked fields always come from the deterministic split.
                row["scene_number"] = pre["scene_number"]
                row["slot_number"]  = pre["slot_number"]
                row["start_time"]   = pre["start_time"]
                row["end_time"]     = pre["end_time"]
                row["duration"]     = pre["duration"]
                row["script_segment"] = pre["script_segment"]
                row["word_count"]   = pre["word_count"]
                merged.append(row)
            return merged

        except Exception as exc:
            logger.warning(f"LLM attempt {attempt + 1}/{config.MAX_RETRIES} failed: {exc}")
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))

    raise RuntimeError("LLM failed to generate scene prompts after all retries.")


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
) -> tuple[Path, float]:
    """
    Generate one scene image via gpt-image-2.

    Returns (output_path, elapsed_seconds). Retries up to MAX_RETRIES on
    transient errors with exponential back-off.
    """
    images_dir = project_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    scene_num = int(scene.get("slot_number") or scene.get("scene_number") or 0)
    out_path = image_output_path(scene, project_dir)

    if resolution not in config.RESOLUTION_PRESETS:
        resolution = config.DEFAULT_RESOLUTION
    if quality not in config.QUALITY_OPTIONS:
        quality = config.DEFAULT_QUALITY

    prompt = _build_final_prompt(scene)
    client = _get_client()

    started_at = time.monotonic()
    last_exc: Exception | None = None

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
            image_b64   = response.data[0].b64_json
            image_bytes = base64.b64decode(image_b64)
            out_path.write_bytes(image_bytes)

            elapsed       = time.monotonic() - started_at
            attempt_time  = time.monotonic() - attempt_start
            logger.info(
                f"Scene {scene_num:03d} saved → {out_path.name}  "
                f"({attempt_time:.1f}s, {resolution}, q={quality})"
            )
            return out_path, elapsed

        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"Image attempt {attempt + 1}/{config.MAX_RETRIES} "
                f"for scene {scene_num} failed: {exc}"
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))

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
            pool.submit(generate_image, scene, quality, resolution, project_dir):
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
                results[eid]["image_error"]  = str(exc)
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
            logger.warning("refine_prompt attempt %s failed: %s", attempt + 1, exc)
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError("LLM failed to refine prompt after retries.")
