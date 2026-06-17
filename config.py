import os
from pathlib import Path
from dotenv import load_dotenv

# Prefer local NEW/.env, fall back to image_generator/.env
_local_env   = Path(__file__).parent / ".env"
_sibling_env = Path(__file__).parent.parent / "image_generator" / ".env"
load_dotenv(_local_env if _local_env.exists() else _sibling_env)

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# ── Models ────────────────────────────────────────────────────────────────────
TEXT_MODEL:  str = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini")   # scene splitting + prompt generation
IMAGE_MODEL: str = os.getenv("IMAGE_MODEL",       "gpt-image-2")    # image generation

# ── Speech-to-text (sentence-level timing for logical scene splitting) ───────
# Only `whisper-1` returns word/segment timestamps (timestamp_granularities),
# which we need to align each script sentence to the real voice-over timeline.
# gpt-4o-transcribe is more accurate on text but exposes no timestamps, and we
# discard the transcript text anyway (the original script is the source of truth),
# so whisper-1 is the right choice here.
STT_MODEL: str = os.getenv("STT_MODEL", "whisper-1")
# OpenAI caps a single transcription upload at 25 MB; we split the WAV into
# time slices safely under this many bytes and stitch the timestamps back.
STT_MAX_UPLOAD_BYTES: int = int(os.getenv("STT_MAX_UPLOAD_BYTES", str(24 * 1024 * 1024)))

# ── ElevenLabs (voice-over per scene) ────────────────────────────────────────
ELEVEN_API_KEY: str = os.getenv("ELEVEN_API_KEY", "")
ELEVEN_VOICE_ID: str = os.getenv("ELEVEN_VOICE_ID", "VuLPiW02W0Qm8465ksBZ")
ELEVEN_MODEL_ID: str = os.getenv("ELEVEN_MODEL_ID", "eleven_multilingual_v2")
# Must be a format accepted by your ElevenLabs account tier/API.
# Example valid values: wav_44100, wav_48000, mp3_44100_128, pcm_44100.
ELEVEN_OUTPUT_FORMAT: str = os.getenv("ELEVEN_OUTPUT_FORMAT", "wav_44100")
# Voice settings calibrated for the documentary delivery (matches test11labs.py).
# speed=1.0 is the natural pace; keep the rest fixed so every chunk of the
# combined voice-over sounds identical.
ELEVEN_VOICE_SETTINGS: dict = {
    "stability":        float(os.getenv("ELEVEN_STABILITY",        "0.50")),
    "similarity_boost": float(os.getenv("ELEVEN_SIMILARITY_BOOST", "0.65")),
    "style":            float(os.getenv("ELEVEN_STYLE",            "0.07")),
    "use_speaker_boost": True,
    "speed":            float(os.getenv("ELEVEN_SPEED",            "1.0")),
}
# Fixed seed → deterministic, consistent timbre across every chunk of the
# combined voice-over (so chunk boundaries don't drift in tone).
ELEVEN_SEED: int = int(os.getenv("ELEVEN_SEED", "12345"))
# ElevenLabs caps a single text-to-speech request at 10,000 characters; we chunk
# below that with headroom, then concatenate the chunks into one audio file.
VOICE_MAX_CHARS: int = int(os.getenv("VOICE_MAX_CHARS", "9000"))

# ── gpt-image-2 resolution presets (all 16:9) ────────────────────────────────
# User picks resolution AND quality independently on the creation form.
RESOLUTION_PRESETS: dict = {
    "1280x720":  {"label": "HD",        "tag": "1280×720",  "note": "Fastest"},
    "2048x1152": {"label": "2K",        "tag": "2048×1152", "note": "Balanced"},
    "3840x2160": {"label": "4K Ultra",  "tag": "3840×2160", "note": "Highest detail"},
}
DEFAULT_RESOLUTION: str = "2048x1152"

# ── gpt-image-2 quality (independent of resolution) ──────────────────────────
QUALITY_OPTIONS: tuple = ("low", "medium", "high")
DEFAULT_QUALITY: str = "medium"

# ── Narration pacing ──────────────────────────────────────────────────────────
# PREVIEW ONLY. The real video duration now comes from the generated ElevenLabs
# voice-over (we measure the audio length). These constants only drive the
# pre-generation estimate (live scene-count + cost preview).
# At a normal speaking pace there are ~1200 characters of script per minute.
CHARS_PER_MINUTE: int = int(os.getenv("CHARS_PER_MINUTE", "1200"))
WORDS_PER_MINUTE: int = 150     # legacy fallback (kept for reference)
MIN_DURATION: float   = 1.0     # minimum video duration in minutes
FIRST_SEGMENT: int    = 5       # minutes before the scene-rate switches
DEFAULT_VOICE_SPEED: float = 1.0   # ElevenLabs narration speed (0.25–1.0); locked per project at create time

# ── Abstraction (conceptual "absence" visuals) ───────────────────────────────
# When enabled, conceptual/intangible script lines become symbolic absence-based
# images (empty pedestal, fading clock, …). When disabled, every scene is a
# literal/atmospheric visual of whatever the script actually describes.
DEFAULT_ABSTRACTION_ENABLED: bool = False

# ── Generation settings ───────────────────────────────────────────────────────
MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "3"))
MAX_RETRIES: int  = 3
RETRY_DELAY: float = 2.5
# Parallel ffmpeg encodes during ZIP export (CPU-bound; safe to raise on multi-core VPS).
EXPORT_FFMPEG_WORKERS: int = int(os.getenv("EXPORT_FFMPEG_WORKERS", "4"))

# ── Storage ───────────────────────────────────────────────────────────────────
PROJECTS_DIR: Path = Path(__file__).parent / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Pricing (USD per image) ──────────────────────────────────────────────────
# Per-image cost lookup by resolution and quality (provided pricing table).
IMAGE_COSTS: dict = {
    "1280x720":  {"low": 0.003, "medium": 0.028, "high": 0.114},
    "2048x1152": {"low": 0.005, "medium": 0.042, "high": 0.170},
    "3840x2160": {"low": 0.011, "medium": 0.100, "high": 0.400},
}

# Flat one-time overhead per project for the scene-splitting + prompt-generation
# LLM call (covers text-model usage outside the image API itself).
PROMPT_GENERATION_FLAT_COST: float = 1.00

# Voice-over cost per minute of narration (ElevenLabs TTS + whisper-1 STT for the
# sentence timeline). ~$0.18 + ~$0.006 per minute = ~$0.186/min.
VOICE_COST_PER_MINUTE: float = float(os.getenv("VOICE_COST_PER_MINUTE", "0.186"))

# ── Regeneration queue ───────────────────────────────────────────────────────
# Max simultaneous regeneration *image* renders (matches the OpenAI image
# parallelism the provider supports). Additional jobs queue up.
REGEN_PARALLELISM: int = int(os.getenv("REGEN_PARALLELISM", "4"))
