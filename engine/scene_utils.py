"""Scene list helpers: entry ids, filenames, duplicate detection, ordering."""

from __future__ import annotations

import shutil
import uuid
from collections import Counter
from pathlib import Path
from typing import Any


def image_filename_for_scene(scene: dict[str, Any]) -> str:
    slot = int(scene.get("slot_number") or scene.get("scene_number") or 1)
    v = int(scene.get("variant_index", 0))
    if v <= 0:
        return f"scene_{slot:03d}.png"
    return f"scene_{slot:03d}_v{v}.png"


def ensure_scene_entries(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every scene row has entry_id, slot_number, variant_index, image_filename."""
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(scenes):
        s = dict(raw)
        sn = int(s.get("scene_number") or s.get("slot_number") or i + 1)
        s["slot_number"] = sn
        s["scene_number"] = sn
        s.setdefault("variant_index", 0)
        if not s.get("entry_id"):
            s["entry_id"] = uuid.uuid4().hex
        path_name: str | None = None
        if s.get("image_path"):
            path_name = Path(str(s["image_path"]).replace("\\", "/")).name
        fn = s.get("image_filename") or path_name or image_filename_for_scene(s)
        s["image_filename"] = fn
        if not s.get("image_path"):
            s["image_path"] = f"images/{fn}"
        out.append(s)
    return out


def next_variant_index(scenes: list[dict[str, Any]], slot_number: int) -> int:
    mx = -1
    for s in scenes:
        if int(s.get("slot_number") or s.get("scene_number") or -1) != slot_number:
            continue
        mx = max(mx, int(s.get("variant_index", 0)))
    return mx + 1


def count_variants_for_slot(scenes: list[dict[str, Any]], slot_number: int) -> int:
    return sum(
        1
        for s in scenes
        if int(s.get("slot_number") or s.get("scene_number") or -1) == slot_number
    )


def duplicate_slot_numbers(scenes: list[dict[str, Any]]) -> list[int]:
    slots = [int(s.get("slot_number") or s.get("scene_number") or 0) for s in scenes]
    counts = Counter(slots)
    return sorted([slot for slot, n in counts.items() if n > 1 and slot > 0])


def sort_scenes_for_display(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        scenes,
        key=lambda s: (
            int(s.get("slot_number") or s.get("scene_number") or 0),
            int(s.get("variant_index", 0)),
        ),
    )


def find_entry(scenes: list[dict[str, Any]], entry_id: str) -> tuple[int, dict[str, Any]] | None:
    for i, s in enumerate(scenes):
        if s.get("entry_id") == entry_id:
            return i, s
    return None


def promote_variants_after_delete(
    scenes: list[dict[str, Any]],
    slot_number: int,
    project_dir: Path,
) -> list[dict[str, Any]]:
    """After deleting variant_index=0, reindex remaining variants and rename PNGs."""
    images_dir = project_dir / "images"
    slot_rows = [
        dict(s)
        for s in scenes
        if int(s.get("slot_number") or s.get("scene_number") or -1) == slot_number
    ]
    other = [
        s for s in scenes
        if int(s.get("slot_number") or s.get("scene_number") or -1) != slot_number
    ]
    if not slot_rows:
        return ensure_scene_entries(scenes)

    slot_rows.sort(key=lambda s: int(s.get("variant_index", 0)))
    reindexed: list[dict[str, Any]] = []
    for new_v, row in enumerate(slot_rows):
        row = dict(row)
        old_path = images_dir / str(row.get("image_filename") or "")
        row["variant_index"] = new_v
        row["image_filename"] = image_filename_for_scene(row)
        row["image_path"] = f"images/{row['image_filename']}"
        new_path = images_dir / row["image_filename"]
        if old_path.exists() and old_path != new_path:
            if new_path.exists():
                try:
                    new_path.unlink()
                except OSError:
                    pass
            try:
                shutil.move(str(old_path), str(new_path))
            except OSError:
                pass
        reindexed.append(row)

    return ensure_scene_entries(other + reindexed)
