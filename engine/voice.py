"""
ElevenLabs voice-over generation — single combined narration for the whole script.

Flow:
  1. chunk_script()           — split the script under ElevenLabs' 10k-char limit.
  2. generate_full_voiceover()— render each chunk sequentially with a fixed seed
                                 and neighbour-text context (so the timbre stays
                                 identical across chunks), then concatenate every
                                 chunk into one WAV file.
  3. The combined file's measured length is the *actual* video duration that
     drives scene splitting downstream.
"""

from __future__ import annotations

import contextlib
import logging
import re
import tempfile
import time
import wave
from pathlib import Path
from typing import Callable

import config

logger = logging.getLogger(__name__)

_eleven_client = None
_eleven_voice_settings_cls = None
# Whether the installed elevenlabs SDK accepts the consistency kwargs
# (seed / previous_text / next_text). Disabled automatically on first TypeError.
_supports_extras = True

# Context window passed as previous_text / next_text for prosody continuity.
_CONTEXT_CHARS = 500


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


# ─── Script chunking (stay under the 10k-char request limit) ──────────────────

def _split_sentences(text: str, max_chars: int) -> list[str]:
    """Split a long block into sentence-sized pieces, hard-splitting on words
    only when a single sentence still exceeds max_chars."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(s) <= max_chars:
            out.append(s)
            continue
        cur = ""
        for w in s.split():
            if len(cur) + len(w) + 1 <= max_chars:
                cur = (cur + " " + w).strip()
            else:
                if cur:
                    out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out


def chunk_script(script: str, max_chars: int | None = None) -> list[str]:
    """
    Split the script into ordered chunks, each <= max_chars characters, breaking
    on paragraph boundaries first, then sentences, then words. Order is preserved
    so the concatenated audio reads the script start-to-finish.
    """
    if max_chars is None:
        max_chars = int(config.VOICE_MAX_CHARS)
    text = (script or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            # Paragraph itself too big → break into sentences.
            for sent in _split_sentences(para, max_chars):
                if len(cur) + len(sent) + 1 <= max_chars:
                    cur = (cur + " " + sent).strip()
                else:
                    flush()
                    cur = sent
        else:
            joined_len = len(cur) + len(para) + 2
            if cur and joined_len <= max_chars:
                cur = cur + "\n\n" + para
            elif not cur:
                cur = para
            else:
                flush()
                cur = para
    flush()
    return chunks


# ─── WAV utilities (no external deps) ─────────────────────────────────────────

def _wav_duration_seconds(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return (frames / float(rate)) if rate else 0.0


def _concat_wav_files(paths: list[Path], out_path: Path) -> None:
    """Concatenate same-format WAV files into one using the stdlib wave module."""
    if not paths:
        raise RuntimeError("No audio chunks to concatenate.")
    with contextlib.closing(wave.open(str(out_path), "wb")) as out:
        params_set = False
        for p in paths:
            with contextlib.closing(wave.open(str(p), "rb")) as w:
                if not params_set:
                    out.setparams(w.getparams())
                    params_set = True
                out.writeframes(w.readframes(w.getnframes()))


# ─── ElevenLabs convert (with consistency kwargs + graceful fallback) ────────

def _convert_chunk(client, text: str, voice_settings, previous_text: str | None,
                   next_text: str | None):
    """Call text_to_speech.convert with consistency kwargs; fall back if the
    installed SDK version doesn't accept them."""
    global _supports_extras
    base = dict(
        voice_id=config.ELEVEN_VOICE_ID,
        model_id=config.ELEVEN_MODEL_ID,
        text=text,
        output_format=config.ELEVEN_OUTPUT_FORMAT,
        voice_settings=voice_settings,
    )
    if _supports_extras:
        extras = {"seed": int(config.ELEVEN_SEED)}
        if previous_text:
            extras["previous_text"] = previous_text
        if next_text:
            extras["next_text"] = next_text
        try:
            return client.text_to_speech.convert(**base, **extras)
        except TypeError as exc:
            logger.warning(
                "ElevenLabs SDK rejected consistency kwargs (%s); retrying without them.",
                exc,
            )
            _supports_extras = False
    return client.text_to_speech.convert(**base)


# ─── Public API ───────────────────────────────────────────────────────────────

def generate_full_voiceover(
    script: str,
    project_dir: Path,
    speed: float | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """
    Render the entire script to a single combined WAV voice-over.

    Returns a dict:
        {
          "status": "done",
          "path": "voiceovers/full_voiceover.wav",   # relative to project_dir
          "filename": "full_voiceover.wav",
          "duration_seconds": <float>,
          "chunks": <int>,
          "voice_id": ..., "model_id": ..., "output_format": ...,
        }
    """
    text = (script or "").strip()
    if not text:
        raise RuntimeError("Script is empty — nothing to voice.")

    fmt = str(config.ELEVEN_OUTPUT_FORMAT)
    if not fmt.startswith("wav"):
        raise RuntimeError(
            "The combined voice-over requires a WAV output format. "
            "Set ELEVEN_OUTPUT_FORMAT to a wav_* value (e.g. wav_44100)."
        )

    client, VoiceSettings = _get_client()
    voices_dir = project_dir / "voiceovers"
    voices_dir.mkdir(parents=True, exist_ok=True)

    settings_dict = dict(config.ELEVEN_VOICE_SETTINGS)
    narration_speed = max(
        0.25, min(1.0, float(speed if speed is not None else config.DEFAULT_VOICE_SPEED))
    )
    settings_dict["speed"] = narration_speed
    voice_settings = VoiceSettings(**settings_dict)
    chunks = chunk_script(text, config.VOICE_MAX_CHARS)
    total = len(chunks)
    logger.info("Voice-over: %s chunk(s), %s chars total.", total, len(text))

    out_filename = "full_voiceover.wav"
    out_path = voices_dir / out_filename
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="tatterveil_vo_") as tmp:
        tmp_path = Path(tmp)
        chunk_paths: list[Path] = []

        for i, chunk_text in enumerate(chunks):
            prev_ctx = chunks[i - 1][-_CONTEXT_CHARS:] if i > 0 else None
            next_ctx = chunks[i + 1][:_CONTEXT_CHARS] if i + 1 < total else None

            last_exc: Exception | None = None
            chunk_file = tmp_path / f"chunk_{i:03d}.wav"
            for attempt in range(config.MAX_RETRIES):
                try:
                    audio = _convert_chunk(
                        client, chunk_text, voice_settings, prev_ctx, next_ctx
                    )
                    with open(chunk_file, "wb") as fh:
                        for piece in audio:
                            if piece:
                                fh.write(piece)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Voice chunk %s/%s attempt %s/%s failed: %s",
                        i + 1, total, attempt + 1, config.MAX_RETRIES, exc,
                    )
                    if attempt < config.MAX_RETRIES - 1:
                        time.sleep(config.RETRY_DELAY * (attempt + 1))
            if last_exc is not None:
                raise RuntimeError(
                    f"Voice generation failed for chunk {i + 1}/{total} after "
                    f"{config.MAX_RETRIES} attempts: {last_exc}"
                )

            chunk_paths.append(chunk_file)
            if on_progress:
                on_progress(i + 1, total)

        _concat_wav_files(chunk_paths, out_path)

    duration = _wav_duration_seconds(out_path)
    elapsed = time.monotonic() - started
    logger.info(
        "Voice-over complete → %s  (%.1fs wall, %.1fs audio, %s chunks)",
        out_path.name, elapsed, duration, total,
    )

    return {
        "status": "done",
        "path": out_path.relative_to(project_dir).as_posix(),
        "filename": out_filename,
        "duration_seconds": round(duration, 2),
        "chunks": total,
        "voice_id": config.ELEVEN_VOICE_ID,
        "model_id": config.ELEVEN_MODEL_ID,
        "output_format": config.ELEVEN_OUTPUT_FORMAT,
        "speed": narration_speed,
        "generated_at": time.time(),
    }
