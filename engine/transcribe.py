"""
Speech-to-text sentence timeline — Tatterveil Scene Studio.

Goal: turn the generated voice-over into a per-sentence timeline whose
timestamps come from the ACTUAL audio, while keeping the verbatim original
script text (STT output is only used for *timing*, never for the saved text,
so transcription hallucinations can never leak into the final data).

Flow:
  1. split_script_sentences()   — break the original script into sentences.
  2. transcribe_words()         — whisper-1 word-level timestamps. Long audio is
                                  sliced under the 25 MB upload limit with the
                                  stdlib `wave` module and the per-chunk word
                                  timestamps are offset back onto the full timeline.
  3. align_sentences()          — match the script's words to the STT words and
                                  read each sentence's start/end from the audio.
  4. build_sentence_timeline()  — orchestrates the above into the final dict.

The result is contiguous (each sentence ends where the next begins) and the
final sentence ends exactly at the measured audio length.
"""

from __future__ import annotations

import bisect
import contextlib
import logging
import re
import tempfile
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path

from openai import OpenAI

import config

logger = logging.getLogger(__name__)

_client: OpenAI | None = None

# Common abbreviations whose trailing period must NOT end a sentence.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "no", "vol", "fig", "gen", "col", "capt", "sgt", "lt", "rev", "hon",
    "e.g", "i.e", "a.m", "p.m",
}


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


# ─── 1. Sentence segmentation (verbatim original text) ────────────────────────

def split_script_sentences(script: str) -> list[str]:
    """Split the original script into sentences, preserving the exact text.

    Whitespace inside a sentence is collapsed to single spaces (so the stored
    text is clean), but no words are altered or dropped.
    """
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
            # Merge false splits after a known abbreviation (e.g. "Dr. Khepri").
            if last_word in _ABBREVIATIONS:
                sentences[-1] = f"{prev} {part}"
                continue
        sentences.append(part)
    return sentences


def _norm_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens used for alignment only."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())


# ─── 2. WAV slicing + STT (word-level timestamps) ─────────────────────────────

def _wav_duration_seconds(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return (frames / float(rate)) if rate else 0.0


def _slice_wav(path: Path, tmp_dir: Path, max_bytes: int) -> list[tuple[Path, float]]:
    """Split a WAV into time slices each safely under ``max_bytes``.

    Returns a list of (chunk_path, offset_seconds). When the whole file already
    fits, returns a single (original_path, 0.0) entry so we avoid a needless copy.
    """
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        params = w.getparams()
        framerate = w.getframerate()
        nframes = w.getnframes()
        bytes_per_frame = max(1, w.getsampwidth() * w.getnchannels())

    total_bytes = nframes * bytes_per_frame
    # Headroom for the ~44-byte WAV header on every chunk.
    budget = max(bytes_per_frame, int(max_bytes) - 4096)
    if total_bytes <= budget or framerate <= 0:
        return [(path, 0.0)]

    frames_per_chunk = max(1, budget // bytes_per_frame)
    chunks: list[tuple[Path, float]] = []
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
            chunks.append((chunk_path, start_frame / float(framerate)))
            start_frame += n
            idx += 1
    logger.info("STT: sliced audio into %s chunk(s) under %s bytes.", len(chunks), budget)
    return chunks


def _transcribe_chunk(client: OpenAI, wav_path: Path) -> list[dict]:
    """Call whisper-1 for one chunk and return [{word, start, end}, ...]."""
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            with open(wav_path, "rb") as fh:
                resp = client.audio.transcriptions.create(
                    model=config.STT_MODEL,
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
                        "word": str(word),
                        "start": float(start),
                        "end": float(end if end is not None else start),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001 — retried below
            last_exc = exc
            logger.warning(
                "STT attempt %s/%s for %s failed: %s",
                attempt + 1, config.MAX_RETRIES, wav_path.name, exc,
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"STT failed for {wav_path.name}: {last_exc}")


def transcribe_words(wav_path: Path) -> list[dict]:
    """Word-level timestamps for the whole audio, stitched across 25 MB slices."""
    client = _get_client()
    all_words: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="tatterveil_stt_") as tmp:
        slices = _slice_wav(wav_path, Path(tmp), config.STT_MAX_UPLOAD_BYTES)
        for chunk_path, offset in slices:
            words = _transcribe_chunk(client, chunk_path)
            for w in words:
                all_words.append(
                    {
                        "word": w["word"],
                        "start": w["start"] + offset,
                        "end": w["end"] + offset,
                    }
                )
    return all_words


# ─── 3. Align script sentences to the audio timeline ──────────────────────────

def align_sentences(
    sentences: list[str],
    stt_words: list[dict],
    audio_duration: float,
) -> list[dict]:
    """Read each sentence's start/end from the audio via word alignment.

    Original sentence text is kept verbatim; only timing comes from STT.
    """
    n_sent = len(sentences)
    if n_sent == 0:
        return []

    # Flatten the original script into normalized tokens, recording the first
    # global token index of each sentence.
    orig_tokens: list[str] = []
    sentence_first_token: list[int | None] = []
    for s in sentences:
        toks = _norm_tokens(s)
        sentence_first_token.append(len(orig_tokens) if toks else None)
        orig_tokens.extend(toks)

    # Flatten STT words into normalized tokens carrying their start time.
    stt_tokens: list[str] = []
    stt_token_start: list[float] = []
    for w in stt_words:
        for t in _norm_tokens(w["word"]):
            stt_tokens.append(t)
            stt_token_start.append(float(w["start"]))

    # Anchor map: original-token-index → audio start time, from equal blocks.
    anchors: dict[int, float] = {}
    if orig_tokens and stt_tokens:
        matcher = SequenceMatcher(None, orig_tokens, stt_tokens, autojunk=False)
        for block in matcher.get_matching_blocks():
            for k in range(block.size):
                anchors[block.a + k] = stt_token_start[block.b + k]

    sorted_anchor_idx = sorted(anchors)
    n_tokens = len(orig_tokens)

    def time_for_token(idx: int) -> float:
        if not sorted_anchor_idx:
            # No alignment at all — fall back to proportional placement.
            return audio_duration * (idx / max(1, n_tokens))
        pos = bisect.bisect_left(sorted_anchor_idx, idx)
        if pos < len(sorted_anchor_idx) and sorted_anchor_idx[pos] == idx:
            return anchors[idx]
        left = sorted_anchor_idx[pos - 1] if pos > 0 else None
        right = sorted_anchor_idx[pos] if pos < len(sorted_anchor_idx) else None
        if left is None:  # before the first anchor
            r_idx = right or 0
            r_t = anchors.get(r_idx, 0.0)
            return r_t * (idx / r_idx) if r_idx > 0 else 0.0
        if right is None:  # after the last anchor → scale toward audio end
            l_t = anchors[left]
            span = (n_tokens - 1) - left
            if span <= 0:
                return l_t
            return l_t + (audio_duration - l_t) * ((idx - left) / span)
        l_t, r_t = anchors[left], anchors[right]
        return l_t + (r_t - l_t) * ((idx - left) / (right - left))

    # Per-sentence start times, then make them monotonic and contiguous.
    starts: list[float] = []
    for si in range(n_sent):
        first_idx = sentence_first_token[si]
        if first_idx is None:
            starts.append(starts[-1] if starts else 0.0)
        else:
            starts.append(max(0.0, min(float(audio_duration), time_for_token(first_idx))))

    starts[0] = 0.0
    for i in range(1, n_sent):
        if starts[i] < starts[i - 1]:
            starts[i] = starts[i - 1]

    timeline: list[dict] = []
    for i in range(n_sent):
        start_t = starts[i]
        end_t = starts[i + 1] if i + 1 < n_sent else float(audio_duration)
        if end_t < start_t:
            end_t = start_t
        timeline.append(
            {
                "sentence_index": i + 1,
                "start_time": round(start_t, 2),
                "end_time": round(end_t, 2),
                "duration": round(end_t - start_t, 2),
                "text": sentences[i],
                "word_count": len(sentences[i].split()),
            }
        )
    return timeline


# ─── 4. Public entry point ────────────────────────────────────────────────────

def build_sentence_timeline(script: str, wav_path: Path) -> dict:
    """Produce the sentence timeline for a generated voice-over.

    Raises on failure so the caller can fall back to the word-count split.
    """
    started = time.monotonic()
    sentences = split_script_sentences(script)
    if not sentences:
        raise RuntimeError("Script produced no sentences for the timeline.")

    audio_duration = _wav_duration_seconds(Path(wav_path))
    if audio_duration <= 0:
        raise RuntimeError("Voice-over has zero measurable duration.")

    stt_words = transcribe_words(Path(wav_path))
    timeline = align_sentences(sentences, stt_words, audio_duration)

    elapsed = round(time.monotonic() - started, 2)
    logger.info(
        "Sentence timeline: %s sentences from %s STT words (%.1fs audio) in %ss.",
        len(timeline), len(stt_words), audio_duration, elapsed,
    )
    return {
        "source": "stt",
        "stt_model": config.STT_MODEL,
        "audio_path": Path(wav_path).name,
        "audio_duration_seconds": round(audio_duration, 3),
        "sentence_count": len(timeline),
        "stt_word_count": len(stt_words),
        "align_seconds": elapsed,
        "sentences": timeline,
    }
