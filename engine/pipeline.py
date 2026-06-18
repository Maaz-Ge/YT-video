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

from openai import BadRequestError, OpenAI

import config
from engine.style_guide import SCENE_SPLIT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_client: OpenAI | None = None

_CONTENT_POLICY_MARKERS = (
    "content_policy", "content policy", "safety system", "safety filter",
    "moderation", "blocked", "refused", "refusal", "violat",
)


class ContentPolicyError(RuntimeError):
    """Raised when an LLM call rejects input for content-safety reasons."""

    def __init__(self, message: str, *, stage: str = "prompt"):
        self.stage = stage
        super().__init__(message)


def is_content_policy_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(m in msg for m in _CONTENT_POLICY_MARKERS):
        return True
    if isinstance(exc, BadRequestError):
        return any(m in msg for m in _CONTENT_POLICY_MARKERS)
    return False


def _raise_if_content_policy(exc: Exception, *, stage: str) -> None:
    if is_content_policy_error(exc):
        raise ContentPolicyError(
            "Script rejected by content safety filters. "
            "Start a new project with a revised script.",
            stage=stage,
        ) from exc


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


# ─── 3b. Sentence-aligned logical scene grouping (per-minute scene budget) ────

SCENE_GROUPING_SYSTEM = """\
You are a documentary video editor. A narrated script has been split \
into numbered SENTENCES, each with the exact start/end time (seconds) measured from the \
real ElevenLabs voice-over. The sentences are organised into one-MINUTE buckets, and every minute \
has a required number of scenes (target_scenes).

Your job: within EACH minute, group that minute's consecutive sentences into EXACTLY \
target_scenes scenes. One image will represent each scene.

HARD RULES (never break):
1. NEVER split a sentence — each sentence belongs to exactly one scene.
2. NEVER put sentences from different minutes into the same scene.
3. For each minute, output EXACTLY target_scenes scenes that use ALL of that minute's \
   sentences, in order, with no gaps or overlaps.
4. Keep related sentences together when possible, but spoken **word count** per scene \
   within a minute must stay roughly even — no scene should have more than ~2.5× the \
   words of the smallest scene in that same minute.
5. If a minute's target_scenes equals its sentence count, every sentence becomes its \
   own scene.

OUTPUT — JSON only, no prose:
{
  "scenes": [
    { "scene_number": 1, "minute": 0, "sentence_indices": [1, 2] },
    { "scene_number": 2, "minute": 0, "sentence_indices": [3, 4] }
  ]
}
List scenes in time order. Every sentence index must appear exactly once.
"""


def _minute_index(sentence: dict) -> int:
    """Assign a sentence to the minute bucket where most of its audio plays.

    At each 60s boundary, compare duration in the earlier vs later minute.
    Example: 55–63s → 5s in minute 0, 3s in minute 1 → minute 0.
    Example: 57–68s → 3s in minute 0, 8s in minute 1 → minute 1.
    Ties fall back to midpoint.
    """
    start = float(sentence.get("start_time", 0.0))
    end = float(sentence.get("end_time", start))
    if end <= start:
        return int(start // 60)

    boundary = int(start // 60) + 1
    boundary_sec = boundary * 60.0
    if end <= boundary_sec:
        return int(start // 60)

    in_prev = boundary_sec - start
    in_next = end - boundary_sec
    if in_prev > in_next:
        return boundary - 1
    if in_next > in_prev:
        return boundary
    return int(((start + end) / 2.0) // 60)


def _sentence_words(sentence: dict) -> int:
    wc = sentence.get("word_count")
    if wc is not None:
        return max(0, int(wc))
    return len(str(sentence.get("text", "")).split())


def _sentence_duration(sentence: dict) -> float:
    dur = float(sentence.get("duration") or 0.0)
    if dur <= 0:
        dur = max(
            0.0,
            float(sentence.get("end_time", 0)) - float(sentence.get("start_time", 0)),
        )
    return dur


def _partition_by_weight(
    sentences: list[dict],
    k: int,
    weights: list[float],
) -> list[list[dict]]:
    """Split consecutive sentences into k groups balanced on cumulative weight."""
    n = len(sentences)
    if n == 0 or k <= 0:
        return []
    k = min(k, n)
    if k == 1:
        return [list(sentences)]
    if k == n:
        return [[s] for s in sentences]

    cum = [0.0]
    for w in weights[:n]:
        cum.append(cum[-1] + max(0.0, float(w)))

    total = cum[-1]
    cuts: list[int] = []
    if total > 0:
        for i in range(1, k):
            target = total * i / k
            best_j, best_d = 1, abs(cum[1] - target)
            for j in range(1, n):
                d = abs(cum[j] - target)
                if d < best_d:
                    best_j, best_d = j, d
            cuts.append(best_j)
    else:
        cuts = sorted({min(max(round(i * n / k), 1), n - 1) for i in range(1, k)})

    cuts = sorted({c for c in cuts if 0 < c < n})
    if len(cuts) != k - 1:
        cuts = sorted({min(max(round(i * n / k), 1), n - 1) for i in range(1, k)})

    groups: list[list[dict]] = []
    prev = 0
    for c in cuts:
        groups.append(list(sentences[prev:c]))
        prev = c
    groups.append(list(sentences[prev:]))
    return [g for g in groups if g]


def _partition_by_words(sentences: list[dict], k: int) -> list[list[dict]]:
    """Split consecutive sentences into k word-balanced groups (no sentence cut)."""
    return _partition_by_weight(
        sentences,
        k,
        [float(_sentence_words(s)) for s in sentences],
    )


def _partition_by_time(sentences: list[dict], k: int) -> list[list[dict]]:
    """Split consecutive sentences into k time-balanced groups (no sentence cut)."""
    return _partition_by_weight(
        sentences,
        k,
        [_sentence_duration(s) for s in sentences],
    )


def _groups_word_balanced(
    groups: list[list[dict]],
    max_ratio: float = 2.5,
) -> bool:
    """True when no scene's word count is wildly out of line with its peers."""
    counts = [sum(_sentence_words(s) for s in g) for g in groups if g]
    if len(counts) < 2:
        return True
    lo, hi = min(counts), max(counts)
    if lo <= 0:
        return hi <= 0
    return hi / lo <= max_ratio


def _build_minute_plan(sentences: list[dict], scene_plan: dict) -> list[dict]:
    """Bucket sentences per minute and assign each minute a target scene count.

    target_scenes = the requested rate for that minute (first_rate for the first
    FIRST_SEGMENT minutes, rest_rate afterwards), capped at the number of
    sentences in the minute — we can never make more scenes than sentences
    without splitting a sentence.
    """
    first_rate = max(1, int(scene_plan.get("first_rate", 1) or 1))
    rest_rate = max(1, int(scene_plan.get("rest_rate", 1) or 1))

    buckets: dict[int, list[dict]] = {}
    for s in sentences:
        buckets.setdefault(_minute_index(s), []).append(s)

    plan: list[dict] = []
    for minute in sorted(buckets):
        bucket = buckets[minute]
        rate = first_rate if minute < int(config.FIRST_SEGMENT) else rest_rate
        target = max(1, min(rate, len(bucket)))
        plan.append({"minute": minute, "sentences": bucket, "target": target})
    return plan


def _minute_groups_valid(groups: list[list[dict]] | None, entry: dict) -> bool:
    """True when one minute's groups use all its sentences in exactly the target
    number of scenes, in order, with no sentence split or reordering."""
    if not groups or len(groups) != int(entry["target"]):
        return False
    expected = [int(s["sentence_index"]) for s in entry["sentences"]]
    flat: list[int] = []
    for g in groups:
        if not g:
            return False
        flat.extend(int(s["sentence_index"]) for s in g)
    if flat != expected:
        return False
    return _groups_word_balanced(groups)


def _llm_group_minutes(title: str, plan: list[dict]) -> dict[int, list[list[dict]]] | None:
    """Ask the LLM to group each minute's sentences into its target scene count.

    Returns {minute_index: [group, ...]} (each minute validated by the caller),
    or None when the call fails outright.
    """
    by_index = {int(s["sentence_index"]): s for e in plan for s in e["sentences"]}
    total_scenes = sum(int(e["target"]) for e in plan)
    minutes_payload = [
        {
            "minute": e["minute"],
            "target_scenes": int(e["target"]),
            "sentences": [
                {
                    "sentence_index": int(s["sentence_index"]),
                    "start_time": s["start_time"],
                    "end_time": s["end_time"],
                    "text": s["text"],
                }
                for s in e["sentences"]
            ],
        }
        for e in plan
    ]
    user_msg = (
        f"VIDEO TITLE: {title}\n"
        f"MINUTES: {len(plan)} | TOTAL SCENES REQUIRED: {total_scenes}\n\n"
        "Group each minute's sentences into exactly its target_scenes scenes, "
        "following every hard rule. Return JSON only.\n\n"
        f"MINUTES:\n{json.dumps(minutes_payload, ensure_ascii=False)}"
    )

    client = _get_client()
    for attempt in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": SCENE_GROUPING_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(re.sub(r"```(?:json)?\s*", "", raw).strip())
            scene_rows = data.get("scenes") if isinstance(data, dict) else data
            if not isinstance(scene_rows, list):
                raise ValueError("missing 'scenes' array")

            by_minute: dict[int, list[list[dict]]] = {}
            for row in scene_rows:
                idxs = row.get("sentence_indices") if isinstance(row, dict) else None
                if not isinstance(idxs, list) or not idxs:
                    continue
                group = [by_index[int(i)] for i in idxs if int(i) in by_index]
                if not group:
                    continue
                # A scene belongs to the minute of its first sentence; cross-minute
                # scenes simply fail that minute's validation downstream.
                by_minute.setdefault(_minute_index(group[0]), []).append(group)
            return by_minute
        except Exception as exc:
            _raise_if_content_policy(exc, stage="scene_grouping")
            logger.warning("LLM minute-grouping attempt %s failed: %s", attempt + 1, exc)
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    return None


def make_scenes_contiguous(segments: list[dict], audio_duration: float) -> list[dict]:
    """Ensure scene MP4 durations tile the narration with no gaps or overlaps."""
    if not segments:
        return segments
    ordered = sorted(segments, key=lambda s: int(s.get("scene_number", 0)))
    for i in range(len(ordered) - 1):
        ordered[i]["end_time"] = round(float(ordered[i + 1]["start_time"]), 2)
        ordered[i]["duration"] = round(
            ordered[i]["end_time"] - float(ordered[i]["start_time"]), 2
        )
    ordered[-1]["end_time"] = round(float(audio_duration), 2)
    ordered[-1]["duration"] = round(
        ordered[-1]["end_time"] - float(ordered[-1]["start_time"]), 2
    )
    return ordered


def build_scene_segments_from_sentences(
    title: str,
    sentences: list[dict],
    scene_plan: dict,
    audio_duration: float | None = None,
) -> list[dict]:
    """
    Group whole sentences into scenes using the real voice-over timing, honouring
    the requested per-minute scene rate.

    • Sentences are bucketed into one-minute windows by their midpoint, so each
      sentence lands in the minute it mostly plays in (natural boundary tolerance).
    • Every minute is split into its target number of scenes (the selected rate,
      capped at that minute's sentence count — we never split a sentence, so a
      minute with fewer sentences than the rate simply yields fewer scenes).
    • The LLM may group sentences by meaning when its split is word-balanced; otherwise
      a deterministic word-balanced partition is used so scenes in the same minute
      carry similar script length.
    • Each scene's start/end comes directly from its sentences, so the timeline
      matches the narration and the final scene ends exactly at the audio length.
    """
    sents = sorted(sentences, key=lambda s: int(s.get("sentence_index", 0)))
    if not sents:
        return []

    plan = _build_minute_plan(sents, scene_plan)

    try:
        llm_by_minute = _llm_group_minutes(title, plan)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM minute grouping unavailable: %s", exc)
        llm_by_minute = None

    final_groups: list[list[dict]] = []
    fallback_minutes: list[int] = []
    for entry in plan:
        groups = (llm_by_minute or {}).get(entry["minute"])
        if _minute_groups_valid(groups, entry):
            final_groups.extend(groups)  # type: ignore[arg-type]
        else:
            fallback_minutes.append(entry["minute"])
            final_groups.extend(_partition_by_words(entry["sentences"], entry["target"]))

    if fallback_minutes:
        logger.info(
            "Word-balanced scene split used for %s/%s minute(s): %s",
            len(fallback_minutes), len(plan), fallback_minutes,
        )

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
                "start_time": round(start_t, 2),
                "end_time": round(end_t, 2),
                "duration": round(end_t - start_t, 2),
                "script_segment": text,
                "word_count": sum(len(s["text"].split()) for s in group),
                "sentence_indices": [int(s["sentence_index"]) for s in group],
            }
        )
    if audio_duration and audio_duration > 0:
        segments = make_scenes_contiguous(segments, float(audio_duration))
    return segments


def split_and_prompt(
    title: str,
    script: str,
    scene_plan: dict,
    pre_segments: list[dict] | None = None,
) -> list[dict]:
    """
    Two-stage scene build:

    1. Use pre-built scene segments (sentence-aligned to the real voice-over via
       build_scene_segments_from_sentences) when provided. Otherwise fall back to
       the deterministic equal-word split (compute_equal_segments).
    2. Ask the LLM to enrich each pre-split scene with scene_type, time_period,
       vista rhythm, and the image prompt — without re-splitting the script.
    """
    duration_minutes = scene_plan["duration_minutes"]
    duration_seconds = scene_plan["duration_seconds"]

    if not pre_segments:
        raise RuntimeError(
            "Could not split script into scenes — sentence timeline is required."
        )

    # The locked segments are the real scene count (sentence grouping may differ
    # slightly from the planned target), so the LLM is told the true number.
    scene_count = len(pre_segments)

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
        "Return them verbatim. Your job is to add the documentary classification, "
        "shot rhythm (is_vista_shot, shot_scale per Step 3.5), human figure policy, "
        "and Tatterveil image prompt for each scene.\n\n"
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
                if pre.get("sentence_indices") is not None:
                    row["sentence_indices"] = pre["sentence_indices"]
                merged.append(row)
            return merged

        except Exception as exc:
            _raise_if_content_policy(exc, stage="prompt_generation")
            logger.warning(f"LLM attempt {attempt + 1}/{config.MAX_RETRIES} failed: {exc}")
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))

    raise RuntimeError("LLM failed to generate scene prompts after all retries.")


# ─── 4. Image generation ──────────────────────────────────────────────────────

SAFETY_REWRITE_SYSTEM = """You rewrite image-generation prompts that were blocked by a content safety filter.

The user gives the ORIGINAL prompt, optional negatives, the script lines for that scene, and the API error message.

Return JSON: { "prompt": "...", "negative_prompt": "..." or null }

Rules:
- Preserve Tatterveil documentary intent: same scene type, period, is_vista_shot, shot_scale.
- Keep "atmospheric photorealistic", "documentary photography style", "16:9 landscape".
- Rephrase any wording that may trigger moderation (violence, gore, nudity, minors, hate, real living persons).
- Use documentary-safe language: distant figures, obscured faces, archaeological context, weathered materials.
- Do NOT change the story beat — only soften phrasing so the image API accepts it.
- Minimum ~80 words in the positive prompt.
"""


def rewrite_prompt_for_safety(
    scene: dict,
    script_segment: str,
    api_error: str,
) -> tuple[str, str | None]:
    """Ask the text model to rephrase a blocked prompt."""
    client = _get_client()
    payload = {
        "original_prompt": scene.get("prompt") or "",
        "original_negative_prompt": scene.get("negative_prompt") or "",
        "script_segment": script_segment,
        "scene_type": scene.get("scene_type"),
        "time_period": scene.get("time_period"),
        "is_vista_shot": scene.get("is_vista_shot"),
        "shot_scale": scene.get("shot_scale"),
        "api_error": api_error[:500],
    }
    for attempt in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": SAFETY_REWRITE_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0.3,
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
                raise ValueError("empty safety rewrite prompt")
            return pos, neg
        except Exception as exc:
            _raise_if_content_policy(exc, stage="safety_rewrite")
            logger.warning("safety rewrite attempt %s failed: %s", attempt + 1, exc)
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError("LLM failed to rewrite prompt for safety after retries.")


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

    On content-policy failure, rewrites the prompt via LLM and retries up to
    IMAGE_SAFETY_MAX_RETRIES times.
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
    safety_attempts = 0
    last_exc: Exception | None = None

    while safety_attempts <= int(config.IMAGE_SAFETY_MAX_RETRIES):
        prompt = _build_final_prompt(scene)
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
                scene["prompt_rewrite_count"] = safety_attempts
                logger.info(
                    f"Scene {scene_num:03d} saved → {out_path.name}  "
                    f"({attempt_time:.1f}s, {resolution}, q={quality}, "
                    f"safety_rewrites={safety_attempts})"
                )
                return out_path, elapsed

            except Exception as exc:
                last_exc = exc
                if is_content_policy_error(exc):
                    break
                logger.warning(
                    f"Image attempt {attempt + 1}/{config.MAX_RETRIES} "
                    f"for scene {scene_num} failed: {exc}"
                )
                if attempt < config.MAX_RETRIES - 1:
                    time.sleep(config.RETRY_DELAY * (attempt + 1))
        else:
            raise RuntimeError(
                f"Image generation failed for scene {scene_num} after "
                f"{config.MAX_RETRIES} attempts: {last_exc}"
            )

        if not is_content_policy_error(last_exc):
            raise RuntimeError(
                f"Image generation failed for scene {scene_num}: {last_exc}"
            )

        if safety_attempts >= int(config.IMAGE_SAFETY_MAX_RETRIES):
            raise RuntimeError(
                f"Image blocked by content policy for scene {scene_num} after "
                f"{config.IMAGE_SAFETY_MAX_RETRIES} prompt rewrites. "
                f"Try again with different instructions. Last error: {last_exc}"
            )

        safety_attempts += 1
        logger.info(
            "Scene %s blocked by content policy — safety rewrite %s/%s",
            scene_num, safety_attempts, config.IMAGE_SAFETY_MAX_RETRIES,
        )
        new_prompt, new_neg = rewrite_prompt_for_safety(
            scene,
            scene.get("script_segment") or "",
            str(last_exc),
        )
        scene["prompt"] = new_prompt
        scene["negative_prompt"] = new_neg

    raise RuntimeError(
        f"Image generation failed for scene {scene_num}: {last_exc}"
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
- Preserve period, scene type, shot_scale, and is_vista_shot intent from the original unless the user explicitly asks to change them.
- Vista/establishing shots: high vantage, atmospheric perspective, depth layers, specific weather and time of day — never generic flat postcard wides.
- Humans: include period-accurate figures when the script calls for people; faces never readable (backs turned, shadow, distance, silhouette only).
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
