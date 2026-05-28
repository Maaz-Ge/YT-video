"""Scene list helpers: entry ids, filenames, duplicate detection, ordering."""

from __future__ import annotations

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


def voice_filename_for_scene(scene: dict[str, Any]) -> str:
    """One voice-over per timeline slot (variants share the same audio)."""
    slot = int(scene.get("slot_number") or scene.get("scene_number") or 1)
    return f"scene_{slot:03d}.wav"


def ensure_scene_entries(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every scene row has entry_id, slot_number, variant_index, image_filename, voice_filename."""
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

        # Voice-over fields (one per slot; variants reuse).
        vn = voice_filename_for_scene(s)
        if not s.get("voice_filename"):
            s["voice_filename"] = vn
        if not s.get("voice_path"):
            s["voice_path"] = f"voiceovers/{s['voice_filename']}"
        s.setdefault("voice_status", "pending")
        s.setdefault("voice_error", None)
        s.setdefault("voice_seconds", None)
        out.append(s)
    return out


def next_variant_index(scenes: list[dict[str, Any]], slot_number: int) -> int:
    mx = -1
    for s in scenes:
        if int(s.get("slot_number") or s.get("scene_number") or -1) != slot_number:
            continue
        mx = max(mx, int(s.get("variant_index", 0)))
    return mx + 1


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
