"""
Speech-to-text sentence timeline — Tatterveil Scene Studio.

Turn the generated voice-over into a per-sentence timeline whose timestamps
come from the ACTUAL audio. The original script text is always kept verbatim;
STT output is used only for timing.

Flow:
  1. split_script_sentences()  — break the script into sentences.
  2. transcribe_words()        — whisper-1 word timestamps on time-sliced
                                 chunks, stitched onto one global timeline.
  3. align_sentences()         — sequential forward word matching; each sentence
                                 starts where the previous one ended.
  4. build_sentence_timeline() — orchestrates the above; raises on any failure
                                 (no estimated / proportional fallbacks).
"""

from __future__ import annotations

import contextlib
import logging
import re
import tempfile
import time
import wave
from pathlib import Path

from openai import OpenAI

import config

logger = logging.getLogger(__name__)

_client: OpenAI | None = None

# User-facing message when STT or alignment fails.
STT_USER_MESSAGE = (
    "Speech-to-text could not align your script to the generated voice-over. "
    "Please try creating this project again in a few minutes."
)

# Common abbreviations whose trailing period must NOT end a sentence.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "no", "vol", "fig", "gen", "col", "capt", "sgt", "lt", "rev", "hon",
    "e.g", "i.e", "a.m", "p.m",
}

# Max consecutive STT tokens to skip while matching one script token.
_MAX_STT_SKIPS = 25

# How many seconds the final sentence may end before the audio file ends.
_MAX_TAIL_GAP_SECONDS = 3.0

# First sentence may begin after this many seconds of leading silence.
_MAX_LEAD_SILENCE_SECONDS = 8.0


class STTError(RuntimeError):
    """Base error for speech-to-text / alignment failures."""

    def __init__(self, message: str = STT_USER_MESSAGE):
        super().__init__(message)


class STTTranscriptionError(STTError):
    """Raised when whisper-1 cannot transcribe the audio."""


class STTAlignmentError(STTError):
    """Raised when script sentences cannot be aligned to STT word timings."""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


# ─── 1. Sentence segmentation (verbatim original text) ────────────────────────

def split_script_sentences(script: str) -> list[str]:
    """Split the original script into sentences, preserving the exact text."""
    text = re.sub(r"\s+", " ", (script or "").strip())
    if not text:
        return []

    raw_parts = re.split(r"(?<=[.!?])\s+", text)
    sentences: list[str] = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        if sentences:
            prev = sentences[-1]
            last_word = prev.split()[-1].lower().strip(".,;:!?\"')(")
            if last_word in _ABBREVIATIONS:
                sentences[-1] = f"{prev} {part}"
                continue
        sentences.append(part)
    return sentences


def _norm_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens used for alignment only."""
    text = (text or "").lower()
    text = text.replace("'", "'").replace("'", "'")
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return re.findall(r"[a-z0-9]+", text)


def _token_eq(stt_tok: str, script_tok: str) -> bool:
    """True when an STT token matches a script token (minor STT drift allowed)."""
    if stt_tok == script_tok:
        return True
    # STT sometimes truncates or merges tokens on long compound words.
    short, long = (stt_tok, script_tok) if len(stt_tok) <= len(script_tok) else (script_tok, stt_tok)
    if len(short) >= 4 and long.startswith(short):
        return True
    return False


# ─── 2. WAV slicing + STT (word-level timestamps) ─────────────────────────────

def _wav_duration_seconds(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return (frames / float(rate)) if rate else 0.0


def _slice_wav(path: Path, tmp_dir: Path) -> list[tuple[Path, float, float]]:
    """Split a WAV into STT upload slices.

    Returns (chunk_path, global_offset_seconds, chunk_duration_seconds).
    Slices advance without overlap; boundary words are handled by sorting the
    merged global word list.
    """
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        params = w.getparams()
        framerate = w.getframerate()
        nframes = w.getnframes()
        bytes_per_frame = max(1, w.getsampwidth() * w.getnchannels())

    if framerate <= 0:
        dur = _wav_duration_seconds(path)
        return [(path, 0.0, dur)]

    bytes_budget = max(bytes_per_frame, int(config.STT_MAX_UPLOAD_BYTES) - 4096)
    frames_per_byte_cap = max(1, bytes_budget // bytes_per_frame)
    frames_per_time_cap = max(1, int(config.STT_MAX_CHUNK_SECONDS * framerate))
    frames_per_chunk = min(frames_per_byte_cap, frames_per_time_cap)

    if nframes <= frames_per_chunk:
        return [(path, 0.0, nframes / float(framerate))]

    chunks: list[tuple[Path, float, float]] = []
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        idx = 0
        start_frame = 0
        while start_frame < nframes:
            w.setpos(start_frame)
            n = min(frames_per_chunk, nframes - start_frame)
            data = w.readframes(n)
            chunk_path = tmp_dir / f"stt_chunk_{idx:03d}.wav"
            with contextlib.closing(wave.open(str(chunk_path), "wb")) as out:
                out.setparams(params)
                out.writeframes(data)
            offset = start_frame / float(framerate)
            duration = n / float(framerate)
            chunks.append((chunk_path, offset, duration))
            if start_frame + n >= nframes:
                break
            start_frame += n
            idx += 1

    logger.info(
        "STT: sliced %.1fs audio into %s non-overlapping chunk(s) (~%.1fs each).",
        nframes / framerate,
        len(chunks),
        frames_per_chunk / framerate,
    )
    return chunks


def _transcribe_chunk(client: OpenAI, wav_path: Path) -> list[dict]:
    """Call whisper-1 for one chunk and return [{word, start, end}, ...]."""
    model = "whisper-1"
    if config.STT_MODEL != "whisper-1":
        logger.warning(
            "STT_MODEL=%r does not expose word timestamps; forcing whisper-1.",
            config.STT_MODEL,
        )

    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            with open(wav_path, "rb") as fh:
                resp = client.audio.transcriptions.create(
                    model=model,
                    file=fh,
                    response_format="verbose_json",
                    timestamp_granularities=["word"],
                )
            words = getattr(resp, "words", None)
            if words is None and isinstance(resp, dict):
                words = resp.get("words")
            out: list[dict] = []
            for w in words or []:
                word = w.get("word") if isinstance(w, dict) else getattr(w, "word", None)
                start = w.get("start") if isinstance(w, dict) else getattr(w, "start", None)
                end = w.get("end") if isinstance(w, dict) else getattr(w, "end", None)
                if word is None or start is None:
                    continue
                out.append(
                    {
                        "word": str(word).strip(),
                        "start": float(start),
                        "end": float(end if end is not None else start),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "STT attempt %s/%s for %s failed: %s",
                attempt + 1, config.MAX_RETRIES, wav_path.name, exc,
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise STTTranscriptionError(
        f"Speech-to-text failed for {wav_path.name}. {STT_USER_MESSAGE}"
    ) from last_exc


def _merge_stt_words(
    chunk_results: list[tuple[list[dict], float]],
) -> list[dict]:
    """Merge per-chunk word lists onto one global timeline."""
    global_words: list[dict] = []
    for words, offset in chunk_results:
        for w in words:
            start = w["start"] + offset
            end = w["end"] + offset
            if end < start:
                end = start
            global_words.append({"word": w["word"], "start": start, "end": end})

    if not global_words:
        return []

    global_words.sort(key=lambda x: (x["start"], x["end"]))

    merged: list[dict] = []
    for w in global_words:
        if merged:
            prev = merged[-1]
            # Drop near-duplicate boundary words from adjacent chunks.
            if abs(w["start"] - prev["start"]) < 0.04 and _norm_tokens(w["word"]) == _norm_tokens(prev["word"]):
                if w["end"] > prev["end"]:
                    merged[-1] = w
                continue
            if w["start"] < prev["end"] - 0.02:
                w = dict(w)
                w["start"] = prev["end"]
                if w["end"] <= w["start"]:
                    w["end"] = w["start"] + 0.04
        merged.append(w)

    return merged


def transcribe_words(wav_path: Path) -> list[dict]:
    """Word-level timestamps for the whole audio, stitched across time slices."""
    client = _get_client()
    with tempfile.TemporaryDirectory(prefix="tatterveil_stt_") as tmp:
        slices = _slice_wav(wav_path, Path(tmp))
        chunk_results: list[tuple[list[dict], float]] = []
        for chunk_path, offset, _dur in slices:
            words = _transcribe_chunk(client, chunk_path)
            if not words:
                raise STTTranscriptionError(
                    f"Speech-to-text returned no words for audio slice at {offset:.1f}s. "
                    f"{STT_USER_MESSAGE}"
                )
            chunk_results.append((words, offset))
        merged = _merge_stt_words(chunk_results)
        if not merged:
            raise STTTranscriptionError(STT_USER_MESSAGE)
        return merged


# ─── 3. Sequential per-sentence word alignment ────────────────────────────────

def _flatten_stt(stt_words: list[dict]) -> tuple[list[str], list[float], list[float]]:
    """Expand STT words into per-token arrays with start/end times."""
    tokens: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for w in stt_words:
        toks = _norm_tokens(w["word"])
        if not toks:
            continue
        ws = float(w["start"])
        we = float(w["end"])
        if we <= ws:
            we = ws + 0.04
        for i, t in enumerate(toks):
            tokens.append(t)
            if len(toks) == 1:
                starts.append(ws)
                ends.append(we)
            else:
                span = we - ws
                frac0 = i / len(toks)
                frac1 = (i + 1) / len(toks)
                starts.append(ws + span * frac0)
                ends.append(ws + span * frac1)
    return tokens, starts, ends


def _try_match_at(
    sent_tokens: list[str],
    stt_tokens: list[str],
    start_idx: int,
) -> tuple[int, int] | None:
    """Attempt to match all script tokens forward from ``start_idx``."""
    si = 0
    first: int | None = None
    last: int | None = None
    i = start_idx
    misses = 0
    max_misses = max(_MAX_STT_SKIPS, len(sent_tokens) // 2)

    while si < len(sent_tokens) and i < len(stt_tokens):
        if _token_eq(stt_tokens[i], sent_tokens[si]):
            if first is None:
                first = i
            last = i
            si += 1
            misses = 0
        else:
            misses += 1
            if misses > max_misses:
                return None
        i += 1

    if si < len(sent_tokens) or first is None or last is None:
        return None
    return first, last


def _match_sentence_forward(
    sent_tokens: list[str],
    stt_tokens: list[str],
    cursor: int,
) -> tuple[int | None, int | None, int]:
    """Find the next sentence in the STT stream, never looking backward."""
    if not sent_tokens:
        return None, None, cursor
    if cursor >= len(stt_tokens):
        return None, None, cursor

    for start in range(cursor, len(stt_tokens)):
        hit = _try_match_at(sent_tokens, stt_tokens, start)
        if hit is not None:
            first, last = hit
            return first, last, last + 1

    return None, None, cursor


def _validate_timeline(
    timeline: list[dict],
    audio_duration: float,
) -> None:
    """Raise when the built timeline is not strictly contiguous and valid."""
    if not timeline:
        raise STTAlignmentError(STT_USER_MESSAGE)

    prev_end = 0.0
    for row in timeline:
        start = float(row["start_time"])
        end = float(row["end_time"])
        dur = float(row["duration"])

        if end <= start:
            raise STTAlignmentError(
                f"Sentence {row['sentence_index']} has zero or negative duration. "
                f"{STT_USER_MESSAGE}"
            )
        if start + 0.001 < prev_end:
            raise STTAlignmentError(
                f"Sentence {row['sentence_index']} overlaps the previous sentence "
                f"({start:.3f}s < {prev_end:.3f}s). {STT_USER_MESSAGE}"
            )
        if abs(dur - (end - start)) > 0.05:
            raise STTAlignmentError(
                f"Sentence {row['sentence_index']} has inconsistent duration. "
                f"{STT_USER_MESSAGE}"
            )
        prev_end = end

    if float(timeline[0]["start_time"]) > _MAX_LEAD_SILENCE_SECONDS:
        raise STTAlignmentError(
            f"First sentence starts at {timeline[0]['start_time']}s — voice-over "
            f"alignment looks wrong. {STT_USER_MESSAGE}"
        )

    tail_gap = audio_duration - float(timeline[-1]["end_time"])
    if tail_gap > _MAX_TAIL_GAP_SECONDS:
        raise STTAlignmentError(
            f"Sentence timeline ends {tail_gap:.1f}s before the voice-over ends. "
            f"{STT_USER_MESSAGE}"
        )


def align_sentences(
    sentences: list[str],
    stt_words: list[dict],
    audio_duration: float,
) -> list[dict]:
    """Assign each sentence start/end from matched STT words.

    Rules:
    - Every sentence must match STT words in forward order (no fallbacks).
    - Sentence 1 starts at its first matched word.
    - Sentence N (>1) starts exactly where sentence N-1 ended.
    - Sentence end = last matched word end in the voice-over.
    """
    n_sent = len(sentences)
    if n_sent == 0:
        raise STTAlignmentError("Script produced no sentences for the timeline.")

    stt_tokens, stt_starts, stt_ends = _flatten_stt(stt_words)
    if not stt_tokens:
        raise STTAlignmentError(
            "Speech-to-text produced no word timestamps. " + STT_USER_MESSAGE
        )

    orig_word_count = sum(len(_norm_tokens(s)) for s in sentences)
    ratio = len(stt_tokens) / max(1, orig_word_count)
    if ratio < 0.70 or ratio > 1.40:
        raise STTAlignmentError(
            f"Script and voice-over word counts differ too much "
            f"(script≈{orig_word_count}, STT={len(stt_tokens)}). "
            "Ensure the script exactly matches the generated narration. "
            + STT_USER_MESSAGE
        )

    cursor = 0
    matches: list[tuple[int, int]] = []

    for i, sent in enumerate(sentences):
        sent_tokens = _norm_tokens(sent)
        if not sent_tokens:
            raise STTAlignmentError(
                f"Sentence {i + 1} has no words to align. {STT_USER_MESSAGE}"
            )
        first_idx, last_idx, cursor = _match_sentence_forward(
            sent_tokens, stt_tokens, cursor
        )
        if first_idx is None or last_idx is None:
            preview = " ".join(sentences[i].split()[:8])
            raise STTAlignmentError(
                f"Could not align sentence {i + 1} ({len(sent_tokens)} words) "
                f'to the voice-over: "{preview}...". {STT_USER_MESSAGE}'
            )
        matches.append((first_idx, last_idx))

    timeline: list[dict] = []
    for i, (first_idx, last_idx) in enumerate(matches):
        stt_end = float(stt_ends[last_idx])
        if i == 0:
            start_t = max(0.0, float(stt_starts[first_idx]))
        else:
            start_t = timeline[i - 1]["end_time"]

        end_t = stt_end
        if end_t <= start_t:
            raise STTAlignmentError(
                f"Sentence {i + 1} ends before it starts after sequential chaining "
                f"({end_t:.3f}s <= {start_t:.3f}s). {STT_USER_MESSAGE}"
            )

        timeline.append(
            {
                "sentence_index": i + 1,
                "start_time": round(start_t, 3),
                "end_time": round(end_t, 3),
                "duration": round(end_t - start_t, 3),
                "speech_start": round(float(stt_starts[first_idx]), 3),
                "text": sentences[i],
                "word_count": len(sentences[i].split()),
            }
        )

    tail_gap = audio_duration - timeline[-1]["end_time"]
    if 0.0 < tail_gap <= _MAX_TAIL_GAP_SECONDS:
        timeline[-1]["end_time"] = round(audio_duration, 3)
        timeline[-1]["duration"] = round(
            timeline[-1]["end_time"] - timeline[-1]["start_time"], 3
        )

    _validate_timeline(timeline, audio_duration)

    logger.info(
        "Sequential alignment: %s/%s sentences matched; timeline spans 0–%.1fs of %.1fs audio.",
        len(matches), n_sent, timeline[-1]["end_time"], audio_duration,
    )
    return timeline


# ─── 4. Public entry point ────────────────────────────────────────────────────

def build_sentence_timeline(script: str, wav_path: Path) -> dict:
    """Produce the sentence timeline for a generated voice-over.

    Raises ``STTError`` on any transcription or alignment failure — there are
    no estimated / proportional fallbacks.
    """
    started = time.monotonic()
    sentences = split_script_sentences(script)
    if not sentences:
        raise STTAlignmentError("Script produced no sentences for the timeline.")

    audio_duration = _wav_duration_seconds(Path(wav_path))
    if audio_duration <= 0:
        raise STTTranscriptionError("Voice-over has zero measurable duration.")

    stt_words = transcribe_words(Path(wav_path))
    timeline = align_sentences(sentences, stt_words, audio_duration)

    elapsed = round(time.monotonic() - started, 2)
    logger.info(
        "Sentence timeline: %s sentences from %s STT words (%.1fs audio) in %ss.",
        len(timeline), len(stt_words), audio_duration, elapsed,
    )
    return {
        "source": "stt",
        "stt_model": "whisper-1",
        "alignment": "sequential_word_match",
        "audio_path": Path(wav_path).name,
        "audio_duration_seconds": round(audio_duration, 3),
        "sentence_count": len(timeline),
        "stt_word_count": len(stt_words),
        "align_seconds": elapsed,
        "sentences": timeline,
    }
