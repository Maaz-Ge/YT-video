"""
Speech-to-text sentence timeline — Tatterveil Scene Studio.

Hybrid STT:
  • whisper-1 (STT_TIMING_MODEL) — word-level start/end timestamps from the audio.
  • gpt-4o-transcribe (STT_MODEL) — more accurate transcript for alignment anchors.
    Original script text is still the source of truth for saved sentences; STT text
    is only used to improve timing alignment.

Flow:
  1. split_script_sentences()   — break the original script into sentences.
  2. transcribe_words()         — whisper-1 word timestamps (sliced under 25 MB).
  3. transcribe_reference_text()— gpt-4o-transcribe full transcript (optional).
  4. align_sentences()          — map script sentences → exact audio start/end.
  5. build_sentence_timeline()  — orchestrates the above.

Each sentence's end_time is the END of its last spoken word (not the next
sentence's start). Scene timestamps are the span of their assigned sentences only.
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
    return re.findall(r"[a-z0-9]+", (text or "").lower())


# ─── 2. WAV slicing ───────────────────────────────────────────────────────────

def _wav_duration_seconds(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return (frames / float(rate)) if rate else 0.0


def _slice_wav(path: Path, tmp_dir: Path, max_bytes: int) -> list[tuple[Path, float]]:
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        params = w.getparams()
        framerate = w.getframerate()
        nframes = w.getnframes()
        bytes_per_frame = max(1, w.getsampwidth() * w.getnchannels())

    total_bytes = nframes * bytes_per_frame
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


# ─── 3. whisper-1 word timestamps ───────────────────────────────────────────

def _transcribe_words_chunk(client: OpenAI, wav_path: Path) -> list[dict]:
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            with open(wav_path, "rb") as fh:
                resp = client.audio.transcriptions.create(
                    model=config.STT_TIMING_MODEL,
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
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "whisper timing attempt %s/%s for %s failed: %s",
                attempt + 1, config.MAX_RETRIES, wav_path.name, exc,
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"whisper timing failed for {wav_path.name}: {last_exc}")


def transcribe_words(wav_path: Path) -> list[dict]:
    """Word-level timestamps from whisper-1, stitched across 25 MB slices."""
    client = _get_client()
    all_words: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="tatterveil_stt_timing_") as tmp:
        slices = _slice_wav(wav_path, Path(tmp), config.STT_MAX_UPLOAD_BYTES)
        for chunk_path, offset in slices:
            words = _transcribe_words_chunk(client, chunk_path)
            for w in words:
                all_words.append(
                    {
                        "word": w["word"],
                        "start": w["start"] + offset,
                        "end": w["end"] + offset,
                    }
                )
    return all_words


# ─── 4. gpt-4o-transcribe reference transcript (alignment aid) ────────────────

def _transcribe_text_chunk(client: OpenAI, wav_path: Path) -> str:
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            with open(wav_path, "rb") as fh:
                resp = client.audio.transcriptions.create(
                    model=config.STT_MODEL,
                    file=fh,
                    response_format="json",
                )
            if isinstance(resp, dict):
                return str(resp.get("text") or "")
            text = getattr(resp, "text", None)
            return str(text or "")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "gpt-4o-transcribe attempt %s/%s for %s failed: %s",
                attempt + 1, config.MAX_RETRIES, wav_path.name, exc,
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"gpt-4o-transcribe failed for {wav_path.name}: {last_exc}")


def transcribe_reference_text(wav_path: Path) -> str:
    """Full transcript from gpt-4o-transcribe (used only to improve alignment)."""
    if config.STT_MODEL == config.STT_TIMING_MODEL:
        return ""
    client = _get_client()
    parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="tatterveil_stt_ref_") as tmp:
        slices = _slice_wav(wav_path, Path(tmp), config.STT_MAX_UPLOAD_BYTES)
        for chunk_path, _offset in slices:
            parts.append(_transcribe_text_chunk(client, chunk_path).strip())
    return " ".join(p for p in parts if p).strip()


# ─── 5. Align script sentences to audio ───────────────────────────────────────

def _build_token_timeline(
    stt_words: list[dict],
) -> tuple[list[str], list[float], list[float]]:
    """Flatten STT words into tokens with parallel start/end arrays."""
    tokens: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for w in stt_words:
        for t in _norm_tokens(w["word"]):
            tokens.append(t)
            starts.append(float(w["start"]))
            ends.append(float(w["end"]))
    return tokens, starts, ends


def _anchor_map(
    reference_tokens: list[str],
    stt_tokens: list[str],
    stt_starts: list[float],
    stt_ends: list[float],
) -> tuple[dict[int, float], dict[int, float]]:
    """Map reference-token index → audio start/end from STT alignment."""
    start_anchors: dict[int, float] = {}
    end_anchors: dict[int, float] = {}
    if not reference_tokens or not stt_tokens:
        return start_anchors, end_anchors

    matcher = SequenceMatcher(None, reference_tokens, stt_tokens, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            ref_i = block.a + k
            stt_i = block.b + k
            start_anchors[ref_i] = stt_starts[stt_i]
            end_anchors[ref_i] = stt_ends[stt_i]
    return start_anchors, end_anchors


def _interp_time(
    idx: int,
    anchors: dict[int, float],
    n_tokens: int,
    audio_duration: float,
    *,
    use_end: bool = False,
) -> float:
    """Interpolate start or end time for a token index from sparse anchors."""
    if not anchors:
        return audio_duration * (idx / max(1, n_tokens - 1)) if use_end and n_tokens > 1 else 0.0

    sorted_idx = sorted(anchors)
    pos = bisect.bisect_left(sorted_idx, idx)
    if pos < len(sorted_idx) and sorted_idx[pos] == idx:
        return anchors[idx]

    left = sorted_idx[pos - 1] if pos > 0 else None
    right = sorted_idx[pos] if pos < len(sorted_idx) else None

    if left is None:
        r_idx = right or 0
        r_t = anchors.get(r_idx, 0.0)
        return r_t * (idx / r_idx) if r_idx > 0 else 0.0
    if right is None:
        l_t = anchors[left]
        span = (n_tokens - 1) - left
        if span <= 0:
            return l_t
        frac = (idx - left) / span
        return l_t + (audio_duration - l_t) * frac

    l_t, r_t = anchors[left], anchors[right]
    span = right - left
    if span <= 0:
        return l_t
    return l_t + (r_t - l_t) * ((idx - left) / span)


def align_sentences(
    sentences: list[str],
    stt_words: list[dict],
    audio_duration: float,
    reference_text: str = "",
) -> list[dict]:
    """Map each script sentence to exact audio start/end from STT word boundaries.

    • start_time = first spoken word of the sentence
    • end_time   = last spoken word of the sentence (not the next sentence's start)
    • Saved text is always the original script sentence
    """
    n_sent = len(sentences)
    if n_sent == 0:
        return []

    orig_tokens: list[str] = []
    sent_first: list[int | None] = []
    sent_last: list[int | None] = []
    for s in sentences:
        toks = _norm_tokens(s)
        if toks:
            sent_first.append(len(orig_tokens))
            sent_last.append(len(orig_tokens) + len(toks) - 1)
            orig_tokens.extend(toks)
        else:
            sent_first.append(None)
            sent_last.append(None)

    stt_tokens, stt_starts, stt_ends = _build_token_timeline(stt_words)

    # Prefer gpt-4o-transcribe text for alignment (more accurate), fall back to script.
    ref_tokens = _norm_tokens(reference_text) if reference_text else orig_tokens
    start_anchors, end_anchors = _anchor_map(ref_tokens, stt_tokens, stt_starts, stt_ends)

    # If gpt-4o alignment is weak, also merge script-direct anchors.
    if reference_text:
        s2, e2 = _anchor_map(orig_tokens, stt_tokens, stt_starts, stt_ends)
        if len(s2) > len(start_anchors):
            start_anchors, end_anchors = s2, e2
        else:
            start_anchors.update(s2)
            end_anchors.update(e2)

    n_ref = len(ref_tokens)
    n_orig = len(orig_tokens)

    def _map_orig_to_ref(orig_idx: int) -> int:
        if not ref_tokens or ref_tokens == orig_tokens:
            return orig_idx
        if orig_idx < 0:
            return 0
        if orig_idx >= n_orig:
            return max(0, n_ref - 1)
        frac = orig_idx / max(1, n_orig - 1)
        return min(n_ref - 1, round(frac * (n_ref - 1)))

    timeline: list[dict] = []
    for i in range(n_sent):
        fi, li = sent_first[i], sent_last[i]
        if fi is None or li is None:
            start_t = timeline[-1]["end_time"] if timeline else 0.0
            end_t = start_t
        else:
            ref_fi = _map_orig_to_ref(fi)
            ref_li = _map_orig_to_ref(li)
            start_t = _interp_time(ref_fi, start_anchors, n_ref, audio_duration, use_end=False)
            end_t = _interp_time(ref_li, end_anchors, n_ref, audio_duration, use_end=True)
            # Prefer exact anchors when available on original indices too.
            s2, e2 = _anchor_map(orig_tokens, stt_tokens, stt_starts, stt_ends)
            if fi in s2:
                start_t = s2[fi]
            if li in e2:
                end_t = e2[li]

        start_t = max(0.0, min(float(audio_duration), start_t))
        end_t = max(start_t, min(float(audio_duration), end_t))

        if i == 0:
            start_t = 0.0
        if i > 0 and start_t < timeline[-1]["end_time"]:
            start_t = timeline[-1]["end_time"]
        if end_t < start_t:
            end_t = start_t

        timeline.append(
            {
                "sentence_index": i + 1,
                "start_time": round(start_t, 3),
                "end_time": round(end_t, 3),
                "duration": round(end_t - start_t, 3),
                "text": sentences[i],
                "word_count": len(sentences[i].split()),
            }
        )

    if timeline:
        timeline[-1]["end_time"] = round(float(audio_duration), 3)
        timeline[-1]["duration"] = round(
            timeline[-1]["end_time"] - timeline[-1]["start_time"], 3
        )

    return timeline


# ─── 6. Public entry point ────────────────────────────────────────────────────

def build_sentence_timeline(script: str, wav_path: Path) -> dict:
    """Produce per-sentence timeline from the generated voice-over."""
    started = time.monotonic()
    sentences = split_script_sentences(script)
    if not sentences:
        raise RuntimeError("Script produced no sentences for the timeline.")

    audio_duration = _wav_duration_seconds(Path(wav_path))
    if audio_duration <= 0:
        raise RuntimeError("Voice-over has zero measurable duration.")

    stt_words = transcribe_words(Path(wav_path))

    reference_text = ""
    try:
        reference_text = transcribe_reference_text(Path(wav_path))
        if reference_text:
            logger.info(
                "gpt-4o-transcribe reference: %s chars for alignment.",
                len(reference_text),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gpt-4o-transcribe reference skipped: %s", exc)

    timeline = align_sentences(
        sentences, stt_words, audio_duration, reference_text=reference_text
    )

    elapsed = round(time.monotonic() - started, 2)
    logger.info(
        "Sentence timeline: %s sentences, %s whisper words, %.1fs audio in %ss "
        "(timing=%s, align=%s).",
        len(timeline), len(stt_words), audio_duration, elapsed,
        config.STT_TIMING_MODEL, config.STT_MODEL,
    )
    return {
        "source": "stt",
        "stt_model": config.STT_MODEL,
        "stt_timing_model": config.STT_TIMING_MODEL,
        "audio_path": Path(wav_path).name,
        "audio_duration_seconds": round(audio_duration, 3),
        "sentence_count": len(timeline),
        "stt_word_count": len(stt_words),
        "align_seconds": elapsed,
        "sentences": timeline,
    }
