"""JPEG previews for scene grids — full PNGs stay on disk for export/lightbox."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

THUMB_MAX_WIDTH: int = int(os.getenv("THUMB_MAX_WIDTH", "1280"))
THUMB_JPEG_QUALITY: int = int(os.getenv("THUMB_JPEG_QUALITY", "82"))


def thumb_path_for(image_path: Path) -> Path:
    """Map ``images/scene_001_v0.png`` → ``images/thumbs/scene_001_v0.jpg``."""
    return image_path.parent / "thumbs" / f"{image_path.stem}.jpg"


def ensure_thumbnail(image_path: Path) -> Path | None:
    """Create or refresh a grid-sized JPEG preview. Returns path or None if missing."""
    if not image_path.is_file():
        return None

    dest = thumb_path_for(image_path)
    try:
        if dest.is_file():
            if dest.stat().st_mtime >= image_path.stat().st_mtime:
                return dest
    except OSError:
        pass

    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image previews") from exc

    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        width, height = im.size
        if width > THUMB_MAX_WIDTH:
            new_h = max(1, int(height * THUMB_MAX_WIDTH / width))
            im = im.resize((THUMB_MAX_WIDTH, new_h), Image.Resampling.LANCZOS)
        im.save(dest, "JPEG", quality=THUMB_JPEG_QUALITY, optimize=True)

    logger.debug("Preview written → %s", dest.name)
    return dest
