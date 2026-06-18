"""
ElevenLabs voice-over + sentence timestamps — single combined narration.

Flow:
  1. split_script_sentences()  — verbatim script sentences (from transcribe module)
  2. group_sentences_into_chunks() — stay under ELEVEN_TIMESTAMP_CHUNK_CHARS
  3. For each chunk: POST /with-timestamps → MP3 + character alignment
  4. Token-match alignment → per-sentence start/end; contiguous timeline + audio scaling
  5. ffmpeg concat MP3 chunks → ffmpeg convert to WAV (duration preserved)
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import requests

import config
from engine.transcribe import split_script_sentences

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _require_eleven_key() -> None:
    if not config.ELEVEN_API_KEY:
        raise RuntimeError(
            "ELEVEN_API_KEY is not configured — set it in your .env to generate voice-overs."
        )


def _voice_settings_payload(speed: float) -> dict:
    narration_speed = max(0.25, min(1.0, float(speed)))
    return {
        "stability": float(config.ELEVEN_VOICE_SETTINGS.get("stability", 0.75)),
        "similarity_boost": float(config.ELEVEN_VOICE_SETTINGS.get("similarity_boost", 0.85)),
        "style": float(config.ELEVEN_VOICE_SETTINGS.get("style", 0.07)),
        "use_speaker_boost": bool(config.ELEVEN_VOICE_SETTINGS.get("use_speaker_boost", True)),
        "speed": narration_speed,
    }


def group_sentences_into_chunks(
    sentences: list[str],
    max_chars: int | None = None,
) -> list[list[str]]:
    """Group whole sentences into API chunks; never split a sentence across chunks."""
    if max_chars is None:
        max_chars = int(config.ELEVEN_TIMESTAMP_CHUNK_CHARS)
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        needed = len(sentence) + (1 if current else 0)
        if current_len + needed > max_chars and current:
            chunks.append(current)
            current = [sentence]
            current_len = len(sentence)
        else:
            current.append(sentence)
            current_len += needed

    if current:
        chunks.append(current)
    return chunks


def chunk_script(script: str, max_chars: int | None = None) -> list[str]:
    """Return ordered chunk texts (space-joined sentences) for progress display."""
    sents = split_script_sentences(script)
    groups = group_sentences_into_chunks(sents, max_chars)
    return [" ".join(g) for g in groups]


def _wav_duration_seconds(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return (frames / float(rate)) if rate else 0.0


def _which_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _media_duration_seconds(path: Path) -> float:
    """Read duration from any audio file ffmpeg understands (MP3, WAV, …)."""
    ff = _which_ffmpeg()
    if not ff:
        return 0.0
    proc = subprocess.run(
        [ff, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    stderr = proc.stderr or ""
    m = _DURATION_RE.search(stderr)
    if not m:
        return 0.0
    h, mn, sec = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(sec)


def _ffmpeg_concat_mp3(mp3_paths: list[Path], out_mp3: Path) -> None:
    ff = _which_ffmpeg()
    if not ff:
        raise RuntimeError(
            "ffmpeg is not installed or not on PATH — required to merge voice-over chunks."
        )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as lst:
        for p in mp3_paths:
            safe = str(p.resolve()).replace("'", "'\\''")
            lst.write(f"file '{safe}'\n")
        list_path = lst.name
    try:
        proc = subprocess.run(
            [ff, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", str(out_mp3)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip() or "ffmpeg concat failed"
            raise RuntimeError(msg)
    finally:
        Path(list_path).unlink(missing_ok=True)


def _ffmpeg_mp3_to_wav(mp3_path: Path, wav_path: Path) -> None:
    ff = _which_ffmpeg()
    if not ff:
        raise RuntimeError(
            "ffmpeg is not installed or not on PATH — required to convert voice-over to WAV."
        )
    proc = subprocess.run(
        [ff, "-y", "-i", str(mp3_path), "-acodec", "pcm_s16le", str(wav_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or "ffmpeg mp3→wav failed"
        raise RuntimeError(msg)


def _call_with_timestamps(text: str, voice_settings: dict) -> tuple[bytes, dict]:
    """POST ElevenLabs /with-timestamps; returns (mp3_bytes, alignment_dict)."""
    _require_eleven_key()
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/"
        f"{config.ELEVEN_VOICE_ID}/with-timestamps"
    )
    payload = {
        "text": text,
        "model_id": config.ELEVEN_MODEL_ID,
        "voice_settings": voice_settings,
    }
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                headers={
                    "xi-api-key": config.ELEVEN_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=int(config.ELEVEN_REQUEST_TIMEOUT),
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"ElevenLabs API error {resp.status_code}: {resp.text[:500]}"
                )
            data = resp.json()
            audio_bytes = base64.b64decode(data["audio_base64"])
            alignment = data.get("normalized_alignment") or data.get("alignment") or {}
            return audio_bytes, alignment
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "ElevenLabs with-timestamps attempt %s/%s failed: %s",
                attempt + 1, config.MAX_RETRIES, exc,
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError(
        f"ElevenLabs voice + timestamps failed after {config.MAX_RETRIES} attempts: {last_exc}"
    )


def _norm_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _norm_align_char(ch: str) -> str:
    if ch in ("\r", "\n", "\t"):
        return " "
    return ch


def _alignment_word_stream(
    chars: list,
    starts: list,
    ends: list,
) -> list[tuple[str, int, int]]:
    """Build (token, first_char_idx, last_char_idx) from character alignment."""
    words: list[tuple[str, int, int]] = []
    n = min(len(chars), len(starts), len(ends))
    i = 0
    while i < n:
        ch = _norm_align_char(str(chars[i]))
        if not ch.isalnum():
            i += 1
            continue
        first_idx = i
        letters: list[str] = []
        while i < n:
            c = _norm_align_char(str(chars[i]))
            if not c.isalnum():
                break
            letters.append(c.lower())
            i += 1
        words.append(("".join(letters), first_idx, i - 1))
    return words


def _find_sentence_word_span(
    sent_tokens: list[str],
    word_stream: list[tuple[str, int, int]],
    from_word: int,
) -> tuple[int, int, int] | None:
    """Return (first_char_idx, last_char_idx, next_word_index) or None."""
    if not sent_tokens or not word_stream:
        return None
    m = len(sent_tokens)
    n = len(word_stream)
    for i in range(from_word, n - m + 1):
        if all(word_stream[i + j][0] == sent_tokens[j] for j in range(m)):
            return word_stream[i][1], word_stream[i + m - 1][2], i + m
    return None


def _sentence_row(text: str, start: float, end: float, index: int) -> dict:
    start = round(max(0.0, start), 2)
    end = round(max(start, end), 2)
    return {
        "sentence_index": index,
        "start_time": start,
        "end_time": end,
        "duration": round(end - start, 2),
        "text": text,
        "word_count": len(text.split()),
    }


def _proportional_sentence_times(
    sentences: list[str],
    time_offset: float,
    chunk_duration: float,
    start_index: int,
) -> list[dict]:
    if not sentences:
        return []
    weights = [max(1, len(s)) for s in sentences]
    total_w = sum(weights)
    cursor = time_offset
    out: list[dict] = []
    for i, s in enumerate(sentences):
        dur = chunk_duration * (weights[i] / total_w) if total_w else 0.0
        end = cursor + dur if i < len(sentences) - 1 else time_offset + chunk_duration
        out.append(_sentence_row(s, cursor, end, start_index + i))
        cursor = end
    return out


def _rescale_time_rows(
    rows: list[dict],
    base: float,
    old_end: float,
    new_end: float,
) -> None:
    """Linearly rescale start/end times in rows from [base, old_end] → [base, new_end]."""
    if not rows or old_end <= base or abs(old_end - new_end) < 0.05:
        return
    scale = (new_end - base) / (old_end - base)
    for row in rows:
        for key in ("start_time", "end_time"):
            row[key] = round(base + (float(row[key]) - base) * scale, 2)
        row["duration"] = round(row["end_time"] - row["start_time"], 2)


def make_timeline_contiguous(timeline: list[dict], audio_duration: float) -> None:
    """In-place: each sentence ends when the next begins; last ends at audio_duration."""
    if not timeline:
        return
    timeline.sort(key=lambda s: int(s.get("sentence_index", 0)))
    for i in range(len(timeline) - 1):
        timeline[i]["end_time"] = timeline[i + 1]["start_time"]
        timeline[i]["duration"] = round(
            timeline[i]["end_time"] - timeline[i]["start_time"], 2
        )
    timeline[-1]["end_time"] = round(float(audio_duration), 2)
    timeline[-1]["duration"] = round(
        timeline[-1]["end_time"] - timeline[-1]["start_time"], 2
    )


def alignment_to_sentence_timestamps(
    sentences: list[str],
    alignment: dict,
    time_offset: float = 0.0,
    start_index: int = 0,
) -> list[dict]:
    """
    Map character-level alignment to sentence-level timestamps via token matching.
    """
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    n = min(len(chars), len(starts), len(ends))

    if n == 0 or not sentences:
        chunk_dur = float(ends[-1]) if ends else 0.0
        return _proportional_sentence_times(
            sentences, time_offset, chunk_dur, start_index + 1
        )

    word_stream = _alignment_word_stream(chars, starts, ends)
    align_tokens = [w[0] for w in word_stream]
    results: list[dict] = []
    word_cursor = 0

    for si, sentence in enumerate(sentences):
        sent_tokens = _norm_tokens(sentence)
        span = _find_sentence_word_span(sent_tokens, word_stream, word_cursor)

        if span is None and sent_tokens and align_tokens:
            matcher = SequenceMatcher(None, sent_tokens, align_tokens[word_cursor:], autojunk=False)
            block = matcher.get_matching_blocks()
            if block and block[0].size > 0:
                b = block[0]
                wi = word_cursor + b.b
                if wi + b.size <= len(word_stream):
                    span = (
                        word_stream[wi][1],
                        word_stream[wi + b.size - 1][2],
                        wi + b.size,
                    )

        if span is None:
            chunk_end = float(ends[n - 1]) + time_offset
            prev_end = results[-1]["end_time"] if results else time_offset
            remaining = len(sentences) - si
            tail = max(0.0, chunk_end - prev_end)
            per = tail / remaining if remaining else 0.0
            for j, s in enumerate(sentences[si:]):
                st = prev_end + j * per
                en = st + per if j < remaining - 1 else chunk_end
                results.append(_sentence_row(s, st, en, start_index + len(results) + 1))
            break

        first_char, last_char, word_cursor = span
        sentence_start = float(starts[first_char]) + time_offset
        sentence_end = float(ends[last_char]) + time_offset
        results.append(
            _sentence_row(
                sentence,
                sentence_start,
                max(sentence_end, sentence_start),
                start_index + len(results) + 1,
            )
        )

    for i in range(len(results)):
        if i > 0 and results[i]["start_time"] < results[i - 1]["end_time"]:
            results[i]["start_time"] = results[i - 1]["end_time"]
        if results[i]["end_time"] < results[i]["start_time"]:
            results[i]["end_time"] = results[i]["start_time"]
        results[i]["duration"] = round(
            results[i]["end_time"] - results[i]["start_time"], 2
        )

    return results


def generate_voice_with_timestamps(
    script: str,
    project_dir: Path,
    speed: float | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """
    Render the full script via ElevenLabs /with-timestamps and produce:
      - voiceovers/full_voiceover.wav
      - sentences[] with real start/end times

    Raises on failure (no Whisper fallback).
    """
    text = (script or "").strip()
    if not text:
        raise RuntimeError("Script is empty — nothing to voice.")

    _require_eleven_key()
    if _which_ffmpeg() is None:
        raise RuntimeError(
            "ffmpeg is not installed or not on PATH — required for voice-over assembly."
        )

    narration_speed = max(
        0.25, min(1.0, float(speed if speed is not None else config.DEFAULT_VOICE_SPEED))
    )
    voice_settings = _voice_settings_payload(narration_speed)

    sentences = split_script_sentences(text)
    if not sentences:
        raise RuntimeError("Script produced no sentences for voice-over.")

    chunk_groups = group_sentences_into_chunks(sentences)
    total_chunks = len(chunk_groups)
    logger.info(
        "Voice + timestamps: %s sentences, %s chunk(s), %s chars.",
        len(sentences), total_chunks, len(text),
    )

    voices_dir = project_dir / "voiceovers"
    voices_dir.mkdir(parents=True, exist_ok=True)
    out_filename = "full_voiceover.wav"
    out_path = voices_dir / out_filename
    started = time.monotonic()

    all_timeline: list[dict] = []
    global_index = 0

    with tempfile.TemporaryDirectory(prefix="tatterveil_vo_") as tmp:
        tmp_path = Path(tmp)
        mp3_paths: list[Path] = []
        time_offset = 0.0

        for i, chunk_sentences in enumerate(chunk_groups):
            chunk_start = time_offset
            chunk_text = " ".join(chunk_sentences)
            audio_bytes, alignment = _call_with_timestamps(chunk_text, voice_settings)

            mp3_file = tmp_path / f"chunk_{i:03d}.mp3"
            mp3_file.write_bytes(audio_bytes)
            mp3_paths.append(mp3_file)

            chunk_times = alignment_to_sentence_timestamps(
                chunk_sentences,
                alignment,
                time_offset=chunk_start,
                start_index=global_index,
            )

            mp3_dur = _media_duration_seconds(mp3_file)
            if chunk_times and mp3_dur > 0:
                align_end = float(chunk_times[-1]["end_time"])
                measured_end = chunk_start + mp3_dur
                _rescale_time_rows(chunk_times, chunk_start, align_end, measured_end)

            for row in chunk_times:
                global_index += 1
                row["sentence_index"] = global_index
                all_timeline.append(row)

            if mp3_dur > 0:
                time_offset = chunk_start + mp3_dur
            elif chunk_times:
                time_offset = float(chunk_times[-1]["end_time"])
            elif alignment.get("character_end_times_seconds"):
                time_offset = (
                    float(alignment["character_end_times_seconds"][-1]) + chunk_start
                )

            partial = {
                "source": "elevenlabs_timestamps",
                "model_id": config.ELEVEN_MODEL_ID,
                "chunks_done": i + 1,
                "chunks_total": total_chunks,
                "sentences": all_timeline,
            }
            (project_dir / "sentence_timeline.json").write_text(
                json.dumps(partial, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            if on_progress:
                on_progress(i + 1, total_chunks)

        combined_mp3 = tmp_path / "combined.mp3"
        combined_wav = tmp_path / "combined.wav"
        _ffmpeg_concat_mp3(mp3_paths, combined_mp3)
        _ffmpeg_mp3_to_wav(combined_mp3, combined_wav)
        out_path.write_bytes(combined_wav.read_bytes())

    duration = _wav_duration_seconds(out_path)
    if all_timeline and duration > 0:
        align_total = float(all_timeline[-1]["end_time"])
        if abs(align_total - duration) > 0.05:
            _rescale_time_rows(all_timeline, 0.0, align_total, duration)
        make_timeline_contiguous(all_timeline, duration)

    elapsed = time.monotonic() - started
    logger.info(
        "Voice + timestamps complete → %s  (%.1fs wall, %.1fs audio, %s sentences)",
        out_path.name, elapsed, duration, len(all_timeline),
    )

    timeline_payload = {
        "source": "elevenlabs_timestamps",
        "model_id": config.ELEVEN_MODEL_ID,
        "audio_path": out_filename,
        "audio_duration_seconds": round(duration, 3),
        "sentence_count": len(all_timeline),
        "align_seconds": round(elapsed, 2),
        "sentences": all_timeline,
    }

    (project_dir / "sentence_timeline.json").write_text(
        json.dumps(timeline_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "status": "done",
        "path": out_path.relative_to(project_dir).as_posix(),
        "filename": out_filename,
        "duration_seconds": round(duration, 2),
        "chunks": total_chunks,
        "sentence_count": len(all_timeline),
        "voice_id": config.ELEVEN_VOICE_ID,
        "model_id": config.ELEVEN_MODEL_ID,
        "speed": narration_speed,
        "generated_at": time.time(),
        "sentences": all_timeline,
    }


generate_full_voiceover = generate_voice_with_timestamps
