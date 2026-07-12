import os
from pathlib import Path
from dotenv import load_dotenv

# Prefer local NEW/.env, fall back to image_generator/.env
_local_env   = Path(__file__).parent / ".env"
_sibling_env = Path(__file__).parent.parent / "image_generator" / ".env"
load_dotenv(_local_env if _local_env.exists() else _sibling_env)

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# ── Models ────────────────────────────────────────────────────────────────────
TEXT_MODEL:  str = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini")
IMAGE_MODEL: str = os.getenv("IMAGE_MODEL",       "gpt-image-2")

# ── ElevenLabs (voice-over + sentence timestamps via /with-timestamps) ───────
ELEVEN_API_KEY: str = os.getenv("ELEVEN_API_KEY", "")
ELEVEN_VOICE_ID: str = os.getenv("ELEVEN_VOICE_ID", "VuLPiW02W0Qm8465ksBZ")
ELEVEN_MODEL_ID: str = os.getenv("ELEVEN_MODEL_ID", "eleven_v3")
ELEVEN_VOICE_SETTINGS: dict = {
    "stability":        float(os.getenv("ELEVEN_STABILITY",        "0.75")),
    "similarity_boost": float(os.getenv("ELEVEN_SIMILARITY_BOOST", "0.85")),
    "style":            float(os.getenv("ELEVEN_STYLE",            "0.07")),
    "use_speaker_boost": True,
    "speed":            float(os.getenv("ELEVEN_SPEED",            "1.0")),
}
ELEVEN_TIMESTAMP_CHUNK_CHARS: int = int(os.getenv("ELEVEN_TIMESTAMP_CHUNK_CHARS", "2000"))
ELEVEN_REQUEST_TIMEOUT: int = int(os.getenv("ELEVEN_REQUEST_TIMEOUT", "300"))
VOICE_MAX_CHARS: int = int(os.getenv("VOICE_MAX_CHARS", str(ELEVEN_TIMESTAMP_CHUNK_CHARS)))

# ── gpt-image-2 resolution presets (all 16:9) ────────────────────────────────
RESOLUTION_PRESETS: dict = {
    "1280x720":  {"label": "HD",        "tag": "1280×720",  "note": "Fastest"},
    "2048x1152": {"label": "2K",        "tag": "2048×1152", "note": "Balanced"},
    "3840x2160": {"label": "4K Ultra",  "tag": "3840×2160", "note": "Highest detail"},
}
DEFAULT_RESOLUTION: str = "2048x1152"

QUALITY_OPTIONS: tuple = ("low", "medium", "high")
DEFAULT_QUALITY: str = "medium"

# ── Narration pacing (preview only) ──────────────────────────────────────────
CHARS_PER_MINUTE: int = int(os.getenv("CHARS_PER_MINUTE", "1200"))
WORDS_PER_MINUTE: int = 150
MIN_DURATION: float   = 1.0
FIRST_SEGMENT: int    = 5
DEFAULT_VOICE_SPEED: float = 1.0

# ── Generation settings ───────────────────────────────────────────────────────
MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "4"))
MAX_RETRIES: int  = 3
RETRY_DELAY: float = 2.5
IMAGE_SAFETY_MAX_RETRIES: int = int(os.getenv("IMAGE_SAFETY_MAX_RETRIES", "5"))
EXPORT_FFMPEG_WORKERS: int = int(os.getenv("EXPORT_FFMPEG_WORKERS", "6"))
REGEN_PARALLELISM: int = int(os.getenv("REGEN_PARALLELISM", "3"))
SINGLE_IMAGE_PARALLELISM: int = int(os.getenv("SINGLE_IMAGE_PARALLELISM", "3"))

# ── Storage ───────────────────────────────────────────────────────────────────
PROJECTS_DIR: Path = Path(__file__).parent / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Standalone single-image studio (raw prompt, no Tatterveil style) lives outside
# PROJECTS_DIR so it never appears in the project list or the generation lock.
SINGLES_DIR: Path = Path(__file__).parent / "singles"
SINGLES_DIR.mkdir(parents=True, exist_ok=True)

# ── Pricing (USD) ────────────────────────────────────────────────────────────
IMAGE_COSTS: dict = {
    "1280x720":  {"low": 0.003, "medium": 0.028, "high": 0.114},
    "2048x1152": {"low": 0.005, "medium": 0.042, "high": 0.170},
    "3840x2160": {"low": 0.011, "medium": 0.100, "high": 0.400},
}
PROMPT_GENERATION_FLAT_COST: float = 1.00
VOICE_COST_PER_MINUTE: float = float(os.getenv("VOICE_COST_PER_MINUTE", "0.186"))
