"""
ElevenLabs voice-over generation per scene.

One WAV per timeline slot (variants share the same audio because the
script_segment is fixed per slot).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import config
from engine.scene_utils import voice_filename_for_scene

logger = logging.getLogger(__name__)

_eleven_client = None
_eleven_voice_settings_cls = None


def _get_client():
    """Lazy-import + instantiate the ElevenLabs client."""
    global _eleven_client, _eleven_voice_settings_cls
    if _eleven_client is not None:
        return _eleven_client, _eleven_voice_settings_cls

    if not config.ELEVEN_API_KEY:
        raise RuntimeError(
            "ELEVEN_API_KEY is not configured — set it in your .env to generate voice-overs."
        )

    try:
        from elevenlabs.client import ElevenLabs
        from elevenlabs import VoiceSettings
    except ImportError as exc:
        raise RuntimeError(
            "The `elevenlabs` package is not installed. Run `pip install elevenlabs`."
        ) from exc

    _eleven_client = ElevenLabs(api_key=config.ELEVEN_API_KEY)
    _eleven_voice_settings_cls = VoiceSettings
    return _eleven_client, _eleven_voice_settings_cls


def voice_output_path(scene: dict, project_dir: Path) -> Path:
    voices_dir = project_dir / "voiceovers"
    fn = scene.get("voice_filename") or voice_filename_for_scene(scene)
    return voices_dir / fn


def generate_voice(scene: dict, project_dir: Path) -> tuple[Path, float]:
    """
    Render the scene's script_segment to a WAV voice clip via ElevenLabs.

    Returns (output_path, elapsed_seconds).
    """
    text = (scene.get("script_segment") or "").strip()
    if not text:
        raise RuntimeError("script_segment is empty for this scene")

    client, VoiceSettings = _get_client()
    voices_dir = project_dir / "voiceovers"
    voices_dir.mkdir(parents=True, exist_ok=True)

    out_path = voice_output_path(scene, project_dir)
    slot = int(scene.get("slot_number") or scene.get("scene_number") or 0)

    settings_dict = dict(config.ELEVEN_VOICE_SETTINGS)
    voice_settings = VoiceSettings(**settings_dict)

    started = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            audio = client.text_to_speech.convert(
                voice_id=config.ELEVEN_VOICE_ID,
                model_id=config.ELEVEN_MODEL_ID,
                text=text,
                output_format=config.ELEVEN_OUTPUT_FORMAT,
                voice_settings=voice_settings,
            )
            with open(out_path, "wb") as fh:
                for chunk in audio:
                    if chunk:
                        fh.write(chunk)
            elapsed = time.monotonic() - started
            logger.info(
                "Voice slot %03d saved → %s  (%.1fs, voice=%s)",
                slot,
                out_path.name,
                elapsed,
                config.ELEVEN_VOICE_ID,
            )
            return out_path, elapsed
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Voice attempt %s/%s for slot %s failed: %s",
                attempt + 1,
                config.MAX_RETRIES,
                slot,
                exc,
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))

    raise RuntimeError(
        f"Voice generation failed for slot {slot} after "
        f"{config.MAX_RETRIES} attempts: {last_exc}"
    )


def generate_all_voiceovers(
    scenes: list[dict],
    project_dir: Path,
    on_progress: Callable[[int, int, dict], None] | None = None,
) -> tuple[list[dict], dict]:
    """
    Generate voice-overs for one row per unique slot (variants share).

    Returns (updated_scenes, summary). `updated_scenes` is the same list with
    voice_status / voice_seconds / voice_error filled in on every row.
    """
    slot_to_rows: dict[int, list[dict]] = {}
    for s in scenes:
        slot = int(s.get("slot_number") or s.get("scene_number") or 0)
        slot_to_rows.setdefault(slot, []).append(s)

    unique_slots = sorted(slot_to_rows.keys())
    total = len(unique_slots)
    if total == 0:
        return scenes, {"total_slots": 0, "successful": 0, "failed": 0, "wall_clock_seconds": 0.0}

    per_slot_time: dict[int, float] = {}
    started = time.monotonic()

    workers = max(1, int(config.VOICE_WORKERS))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_slot = {}
        for slot in unique_slots:
            # Use the first row for that slot as the template (script_segment is identical).
            template = slot_to_rows[slot][0]
            future_to_slot[pool.submit(generate_voice, template, project_dir)] = slot

        done = 0
        for future in as_completed(future_to_slot):
            slot = future_to_slot[future]
            done += 1
            rows = slot_to_rows[slot]
            try:
                out_path, elapsed = future.result()
                rel = out_path.relative_to(project_dir).as_posix()
                fn = out_path.name
                for r in rows:
                    r["voice_path"] = rel
                    r["voice_filename"] = fn
                    r["voice_status"] = "done"
                    r["voice_seconds"] = round(elapsed, 2)
                    r["voice_error"] = None
                per_slot_time[slot] = elapsed
            except Exception as exc:
                for r in rows:
                    r["voice_status"] = "error"
                    r["voice_error"] = str(exc)
                logger.error("Voice slot %s failed: %s", slot, exc)

            if on_progress:
                on_progress(done, total, rows[0])

    wall = time.monotonic() - started
    successful = len(per_slot_time)
    failed = total - successful
    avg_time = (sum(per_slot_time.values()) / successful) if successful else 0.0

    summary = {
        "total_slots":         total,
        "successful":          successful,
        "failed":              failed,
        "wall_clock_seconds":  round(wall, 2),
        "avg_voice_seconds":   round(avg_time, 2),
        "parallel_workers":    workers,
        "voice_id":            config.ELEVEN_VOICE_ID,
        "model_id":            config.ELEVEN_MODEL_ID,
    }

    logger.info(
        "Voice generation: %s/%s ok, %s failed | wall %.1fs | avg %.1fs/slot",
        successful, total, failed, wall, avg_time,
    )
    return scenes, summary
