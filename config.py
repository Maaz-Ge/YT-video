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
WORDS_PER_MINUTE: int = 150     # typical YouTube narration pace
MIN_DURATION: float   = 1.0     # minimum video duration in minutes
FIRST_SEGMENT: int    = 5       # minutes before the scene-rate switches

# ── Generation settings ───────────────────────────────────────────────────────
MAX_WORKERS: int  = 3
MAX_RETRIES: int  = 3
RETRY_DELAY: float = 2.5

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

# ── Regeneration queue ───────────────────────────────────────────────────────
# Max simultaneous regeneration *image* renders (matches the OpenAI image
# parallelism the provider supports). Additional jobs queue up.
REGEN_PARALLELISM: int = 4
