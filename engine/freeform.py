"""
Freeform batch pipeline helpers — same timing/voice/image flow as Tatterveil,
but with NO hardcoded style guide.

Prompt rules:
  • No special instructions AND no reference style → prompt = script_segment only
  • Special instructions and/or reference-derived style → LLM builds per-scene
    prompts that stay consistent with those constraints
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import time
from pathlib import Path

from openai import OpenAI

import config

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


STYLE_EXTRACT_SYSTEM = """\
You analyse a single reference image and extract a concise, reusable VISUAL STYLE BRIEF
for generating a sequence of video stills that should look like they belong to the same
production.

Return JSON only:
{
  "style_summary": "80–160 words describing look: lighting, color grade, texture, mood, lens/camera feel, composition tendencies, era/setting cues if any. Concrete and actionable for an image model.",
  "style_keywords": ["short", "keyword", "list"]
}

Do NOT invent a story or subjects that are not visually supported.
Do NOT mention watermarks, UI chrome, or the fact that this is a reference photo.
Keep the brief style-focused so it can be applied to many different script scenes.
"""


FREEFORM_PROMPT_SYSTEM = """\
You write image-generation prompts for a freeform (no fixed brand style) video pipeline.

You receive LOCKED scenes (scene_number, timings, script_segment must be returned verbatim)
plus optional GLOBAL CONSTRAINTS:
  • special_instructions — user creative direction (camera, pacing of close-ups, mood, etc.)
  • style_from_reference — visual style brief extracted from a reference image

TASK: For each of exactly {scene_count} scenes, produce a prompt suited to that scene's
script_segment while applying the global constraints consistently across ALL scenes.

Rules:
1. Do NOT change scene_number, start_time, end_time, duration, or script_segment.
2. Every scene's prompt must reflect its own script_segment content.
3. If style_from_reference is present, every prompt must incorporate that look (lighting,
   color, texture, mood) so the set feels coherent.
4. If special_instructions are present, obey them across the set (e.g. mix of close-ups
   and wides) without contradicting the script meaning.
5. Aspect: 16:9 landscape. No watermarks, no burned-in text/logos unless the script needs them.
6. Prompts should be concrete and image-model-ready (typically 40–120 words).
7. Do NOT invent a Tatterveil / documentary archaeology style unless the user asked for it.

Return JSON:
{{
  "scenes": [
    {{
      "scene_number": 1,
      "start_time": 0.0,
      "end_time": 1.0,
      "duration": 1.0,
      "script_segment": "...",
      "prompt": "...",
      "negative_prompt": null
    }}
  ]
}}
"""


FREEFORM_REFINE_SYSTEM = """\
You revise an image-generation prompt for a freeform (no fixed brand style) video pipeline.

The user gives an EXISTING prompt, optional negatives, the script lines for that scene,
optional project-level special_instructions / style_from_reference, and NEW instructions.

Return JSON: {{ "prompt": "...", "negative_prompt": "..." or null }}

Rules:
- Preserve 16:9 landscape intent and the scene's script meaning.
- Apply the user's new instructions faithfully.
- If style_from_reference or special_instructions are provided, keep the revised prompt
  consistent with those global constraints.
- Do NOT inject a Tatterveil / documentary archaeology look unless asked.
- No watermarks or burned-in logos/text unless requested.
"""


def _robust_parse_scenes(raw: str) -> list[dict]:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            for key in ("scenes", "scene_list", "data", "items"):
                if key in obj and isinstance(obj[key], list):
                    return obj[key]
        if isinstance(obj, list):
            return obj
    except json.JSONDecodeError:
        pass
    arr = re.search(r"\[[\s\S]*\]", cleaned)
    if arr:
        try:
            return json.loads(arr.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("Could not parse freeform scene JSON from LLM response")


def extract_style_from_reference(image_path: Path) -> dict:
    """
    Ask the vision-capable text model for a reusable style brief.

    Returns {"style_summary": str, "style_keywords": list[str]}.
    """
    if not image_path.is_file():
        raise RuntimeError(f"Reference image not found: {image_path}")

    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    client = _get_client()
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": STYLE_EXTRACT_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Extract a reusable visual style brief from this reference image.",
                            },
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(re.sub(r"```(?:json)?\s*", "", raw).strip())
            summary = (data.get("style_summary") or "").strip()
            keywords = data.get("style_keywords") or []
            if not isinstance(keywords, list):
                keywords = []
            keywords = [str(k).strip() for k in keywords if str(k).strip()]
            if not summary:
                raise ValueError("empty style_summary from model")
            return {"style_summary": summary, "style_keywords": keywords}
        except Exception as exc:
            last_exc = exc
            logger.warning("Style extract attempt %s failed: %s", attempt + 1, exc)
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"Failed to extract style from reference image: {last_exc}")


def _script_only_scenes(pre_segments: list[dict]) -> list[dict]:
    """No instructions / no reference → prompt is exactly the script segment."""
    out: list[dict] = []
    for pre in pre_segments:
        row = {
            "scene_number": pre["scene_number"],
            "slot_number": pre["slot_number"],
            "start_time": pre["start_time"],
            "end_time": pre["end_time"],
            "duration": pre["duration"],
            "script_segment": pre["script_segment"],
            "word_count": pre.get("word_count"),
            "prompt": (pre.get("script_segment") or "").strip(),
            "negative_prompt": None,
            "scene_type": None,
            "scene_type_name": "Freeform",
            "time_period": None,
        }
        if pre.get("sentence_indices") is not None:
            row["sentence_indices"] = pre["sentence_indices"]
        out.append(row)
    return out


def split_and_prompt_freeform(
    title: str,
    script: str,
    scene_plan: dict,
    pre_segments: list[dict],
    special_instructions: str | None = None,
    style_from_reference: str | None = None,
) -> list[dict]:
    """
    Build freeform scene rows from locked segments.

    When both special_instructions and style_from_reference are empty, skips the
    LLM and sets each prompt to the script_segment verbatim.
    """
    if not pre_segments:
        raise RuntimeError("Could not split script into scenes — sentence timeline is required.")

    instructions = (special_instructions or "").strip()
    style_brief = (style_from_reference or "").strip()

    if not instructions and not style_brief:
        logger.info(
            "Freeform: no instructions/style — using script_segment as prompt for %s scenes",
            len(pre_segments),
        )
        return _script_only_scenes(pre_segments)

    duration_minutes = scene_plan["duration_minutes"]
    duration_seconds = scene_plan["duration_seconds"]
    scene_count = len(pre_segments)

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

    system_prompt = FREEFORM_PROMPT_SYSTEM.format(scene_count=scene_count)
    user_payload = {
        "video_title": title,
        "total_duration_minutes": duration_minutes,
        "total_duration_seconds": duration_seconds,
        "scene_count": scene_count,
        "special_instructions": instructions or None,
        "style_from_reference": style_brief or None,
        "locked_scenes": locked_payload,
        "full_script": script.strip(),
    }
    user_msg = (
        "Generate image prompts for the locked scenes. "
        "Return JSON with a 'scenes' array. Do not change locked fields.\n\n"
        f"{json.dumps(user_payload, ensure_ascii=False)}"
    )

    client = _get_client()
    for attempt in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            llm_scenes = _robust_parse_scenes(raw)
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
                prompt = (enrich.get("prompt") or "").strip()
                if not prompt:
                    prompt = (pre.get("script_segment") or "").strip()
                neg = enrich.get("negative_prompt")
                if isinstance(neg, str):
                    neg = neg.strip() or None
                else:
                    neg = None
                row = {
                    "scene_number": pre["scene_number"],
                    "slot_number": pre["slot_number"],
                    "start_time": pre["start_time"],
                    "end_time": pre["end_time"],
                    "duration": pre["duration"],
                    "script_segment": pre["script_segment"],
                    "word_count": pre.get("word_count"),
                    "prompt": prompt,
                    "negative_prompt": neg,
                    "scene_type": enrich.get("scene_type"),
                    "scene_type_name": enrich.get("scene_type_name") or "Freeform",
                    "time_period": enrich.get("time_period"),
                }
                if pre.get("sentence_indices") is not None:
                    row["sentence_indices"] = pre["sentence_indices"]
                merged.append(row)

            logger.info("Freeform LLM returned prompts for %s scenes", len(merged))
            return merged
        except Exception as exc:
            logger.warning(
                "Freeform prompt attempt %s/%s failed: %s",
                attempt + 1, config.MAX_RETRIES, exc,
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))

    raise RuntimeError("Freeform LLM failed to generate scene prompts after all retries.")


def refine_prompt_freeform(
    previous_prompt: str,
    previous_negative: str | None,
    script_segment: str,
    user_instructions: str,
    special_instructions: str | None = None,
    style_from_reference: str | None = None,
) -> tuple[str, str | None]:
    """Regenerate-path refine that does not force Tatterveil style."""
    client = _get_client()
    payload = {
        "previous_prompt": previous_prompt,
        "previous_negative_prompt": previous_negative or "",
        "script_segment": script_segment,
        "user_instructions": user_instructions.strip(),
        "special_instructions": (special_instructions or "").strip() or None,
        "style_from_reference": (style_from_reference or "").strip() or None,
    }
    for attempt in range(config.MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.TEXT_MODEL,
                messages=[
                    {"role": "system", "content": FREEFORM_REFINE_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
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
            logger.warning("freeform refine attempt %s failed: %s", attempt + 1, exc)
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError("LLM failed to refine freeform prompt after retries.")


def build_safe_replacement_prompt_freeform(user_description: str) -> str:
    """Blocked-recovery path: use the user's text + 16:9 only (no Tatterveil anchors)."""
    desc = user_description.strip().rstrip(".")
    return f"{desc}. 16:9 landscape format, no watermarks, no text overlay."
