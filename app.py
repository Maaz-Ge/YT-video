"""
Tatterveil Scene Studio — Flask application.
"""

import io
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

import config
from engine import pipeline, voice as voice_engine
from engine import thumbnails
from engine.scene_utils import (
    duplicate_slot_numbers,
    ensure_scene_entries,
    find_entry,
    image_filename_for_scene,
    next_variant_index,
    sort_scenes_for_display,
)
from engine.style_guide import SCENE_TYPE_COLORS, SCENE_TYPE_NAMES, PERIOD_LABELS

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "thomcreates-scene-studio-secret"


@app.template_filter("format_ts")
def format_ts(ts: float | None) -> str:
    """Format a Unix timestamp into a human-readable date string."""
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%B %d, %Y  %H:%M")
    except Exception:
        return "—"

# In-memory project state (also persisted to disk)
_state: dict[str, dict] = {}
_state_lock = threading.Lock()


# ─── State helpers ────────────────────────────────────────────────────────────

def _state_path(project_id: str) -> Path:
    return config.PROJECTS_DIR / project_id / "status.json"


def _scenes_path(project_id: str) -> Path:
    return config.PROJECTS_DIR / project_id / "scenes.json"


def _parse_voice_speed(raw) -> float:
    """Clamp ElevenLabs narration speed to 0.25–1.0 (locked when the project is created)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = float(config.DEFAULT_VOICE_SPEED)
    return round(max(0.25, min(1.0, v)), 2)


def _meta_path(project_id: str) -> Path:
    return config.PROJECTS_DIR / project_id / "meta.json"


def _set_state(project_id: str, **kwargs) -> None:
    with _state_lock:
        if project_id not in _state:
            _state[project_id] = {}
        _state[project_id].update(kwargs)
        _state[project_id]["updated_at"] = time.time()
        try:
            _state_path(project_id).write_text(
                json.dumps(_state[project_id]), encoding="utf-8"
            )
        except Exception:
            pass


def _get_state(project_id: str) -> dict | None:
    with _state_lock:
        if project_id in _state:
            return dict(_state[project_id])
    # Fallback: load from disk
    path = _state_path(project_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _state[project_id] = data
            return dict(data)
        except Exception:
            pass
    return None


def _save_scenes(project_id: str, scenes: list[dict]) -> None:
    _scenes_path(project_id).write_text(
        json.dumps(scenes, indent=2), encoding="utf-8"
    )


def _load_scenes(project_id: str) -> list[dict] | None:
    p = _scenes_path(project_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _scenes_live(project_id: str) -> list[dict]:
    """
    Scenes list with entry_id / slot_number / filenames guaranteed.
    Persists migration when older rows lack entry_id.
    """
    from engine.scene_utils import ensure_scene_entries

    raw = _load_scenes(project_id)
    if raw is None:
        return []
    rows = [dict(s) for s in raw]
    needs_save = any(not r.get("entry_id") for r in rows)
    fixed = ensure_scene_entries(rows)

    if needs_save:
        try:
            _save_scenes(project_id, fixed)
        except Exception:
            pass
    return fixed


def _load_meta(project_id: str) -> dict | None:
    p = _meta_path(project_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _load_timing(project_id: str) -> dict | None:
    p = config.PROJECTS_DIR / project_id / "timing.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


# ─── Cost helpers ────────────────────────────────────────────────────────────

def _per_image_cost(resolution: str | None, quality: str | None) -> float:
    table = config.IMAGE_COSTS.get(resolution or "", {})
    return float(table.get(quality or "", 0.0) or 0.0)


def _estimate_cost(resolution: str | None, quality: str | None, total_scenes: int) -> dict:
    per_image = _per_image_cost(resolution, quality)
    scenes = max(0, int(total_scenes or 0))
    images_subtotal = round(per_image * scenes, 4)
    prompt_overhead = round(float(config.PROMPT_GENERATION_FLAT_COST), 4)
    total = round(images_subtotal + prompt_overhead, 4)
    return {
        "resolution": resolution,
        "quality": quality,
        "per_image_usd": round(per_image, 4),
        "total_scenes": scenes,
        "images_subtotal_usd": images_subtotal,
        "prompt_overhead_usd": prompt_overhead,
        "total_usd": total,
    }


# ─── Per-project scene lock (avoid races during regeneration) ────────────────

_scenes_locks: dict[str, threading.Lock] = {}
_scenes_locks_meta = threading.Lock()


def _scene_lock(project_id: str) -> threading.Lock:
    with _scenes_locks_meta:
        lk = _scenes_locks.get(project_id)
        if lk is None:
            lk = threading.Lock()
            _scenes_locks[project_id] = lk
        return lk


# Steps where the initial batch generation worker is still running.
ACTIVE_GENERATION_STEPS = frozenset({
    "queued",
    "analysing",
    "prompting",
    "prompting_done",
    "generating",
    "voicing",
})


def _step_is_generating(step: str | None) -> bool:
    return bool(step and step in ACTIVE_GENERATION_STEPS)


def _find_active_generation() -> dict | None:
    """Return the first in-flight batch-generation project (reads status.json on disk)."""
    for d in sorted(
        config.PROJECTS_DIR.iterdir(),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    ):
        if not d.is_dir():
            continue
        meta = _load_meta(d.name)
        if not meta:
            continue
        state = _get_state(d.name) or {}
        step = state.get("step", "unknown")
        if not _step_is_generating(step):
            continue
        plan = meta.get("scene_plan") or {}
        return {
            "id": d.name,
            "name": meta.get("name", "Untitled"),
            "step": step,
            "progress": int(state.get("progress") or 0),
            "message": state.get("message") or "",
            "total_scenes": state.get("total_scenes")
            or plan.get("total_scenes")
            or 0,
            "scenes_done": state.get("scenes_done") or 0,
        }
    return None


def _list_projects() -> list[dict]:
    projects = []
    for d in sorted(config.PROJECTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if d.is_dir():
            meta = _load_meta(d.name)
            state = _get_state(d.name)
            if meta:
                plan = meta.get("scene_plan") or {}
                step = state.get("step", "unknown") if state else "unknown"
                projects.append({
                    "id": d.name,
                    "name": meta.get("name", "Untitled"),
                    "style": meta.get("style", "Tatterveil"),
                    "quality": meta.get("quality", "medium"),
                    "total_scenes": plan.get("total_scenes")
                    or (state.get("total_scenes") if state else 0)
                    or 0,
                    "duration_minutes": plan.get("duration_minutes", 0),
                    "step": step,
                    "progress": int(state.get("progress") or 0) if state else 0,
                    "message": (state.get("message") or "") if state else "",
                    "is_generating": _step_is_generating(step),
                    "created_at": meta.get("created_at", 0),
                })
    return projects


def _projects_api_payload() -> dict:
    projects = _list_projects()
    active = _find_active_generation()
    return {
        "projects": projects,
        "active_generation": active,
        "generation_locked": active is not None,
    }


# ─── Background generation worker ────────────────────────────────────────────

def _run_generation(project_id: str, meta: dict) -> None:
    """Background thread: split script → generate prompts → generate images."""
    project_dir = config.PROJECTS_DIR / project_id
    project_start = time.monotonic()
    timing_log = {"started_at": time.time()}

    try:
        # ── Step 1: Analyse script ───────────────────────────────────────────
        step_start = time.monotonic()
        _set_state(project_id, step="analysing", progress=3,
                   message="Analysing script…")

        script = meta["script"]
        timing_log["analyse_seconds"] = round(time.monotonic() - step_start, 2)

        # ── Step 2: Generate the full voice-over FIRST ───────────────────────
        # The measured audio length is the real video duration that drives the
        # scene split (so scenes line up with the spoken narration).
        voice_start = time.monotonic()
        voiceover_info: dict
        if config.ELEVEN_API_KEY:
            chunks = voice_engine.chunk_script(script, config.VOICE_MAX_CHARS)
            total_chunks = max(1, len(chunks))
            _set_state(
                project_id, step="voicing", progress=8,
                message=f"Generating voice-over for the full script "
                        f"({total_chunks} chunk{'s' if total_chunks != 1 else ''})…",
                voice_total=total_chunks, voice_done=0,
            )

            def on_voice_progress(done: int, total: int) -> None:
                pct = 8 + int(done / max(1, total) * 27)  # 8 → 35
                _set_state(
                    project_id, step="voicing", progress=min(35, pct),
                    message=f"Generated voice chunk {done} of {total}…",
                    voice_total=total, voice_done=done,
                )

            try:
                voiceover_info = voice_engine.generate_full_voiceover(
                    script=script,
                    project_dir=project_dir,
                    speed=_parse_voice_speed(meta.get("voice_speed")),
                    on_progress=on_voice_progress,
                )
                duration = max(
                    voiceover_info["duration_seconds"] / 60.0, config.MIN_DURATION
                )
                logger.info(
                    "Voice-over duration %.1fs → %.2f min (drives scene plan).",
                    voiceover_info["duration_seconds"], duration,
                )
            except Exception as exc:
                logger.error("Voice-over generation failed: %s", exc, exc_info=True)
                voiceover_info = {"status": "error", "error": str(exc)}
                duration = pipeline.estimate_duration(
                    script, _parse_voice_speed(meta.get("voice_speed"))
                )
        else:
            logger.info("ELEVEN_API_KEY not set — skipping voice-over generation.")
            voiceover_info = {
                "status": "skipped",
                "error": "ELEVEN_API_KEY not configured",
            }
            duration = pipeline.estimate_duration(
                script, _parse_voice_speed(meta.get("voice_speed"))
            )

        meta["voiceover"] = voiceover_info
        timing_log["voice_seconds"] = round(time.monotonic() - voice_start, 2)

        # ── Step 3: Compute scene plan from the actual duration ──────────────
        scene_plan = pipeline.compute_scene_plan(
            duration=duration,
            first_rate=meta["first_rate"],
            rest_rate=meta["rest_rate"],
        )
        meta["scene_plan"] = scene_plan
        meta["cost_estimate"] = _estimate_cost(
            meta.get("resolution"),
            meta.get("quality"),
            scene_plan.get("total_scenes", 0),
        )
        _meta_path(project_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        _set_state(project_id, step="voicing", progress=36,
                   message=f"Voice-over is ~{duration:.1f} min → "
                           f"{scene_plan['total_scenes']} scenes planned",
                   scene_plan=scene_plan)

        # ── Step 4: Split script + generate prompts ──────────────────────────
        step_start = time.monotonic()
        _set_state(project_id, step="prompting", progress=38,
                   message="Splitting script into scenes and generating visual prompts…")

        scenes = pipeline.split_and_prompt(
            title=meta["name"],
            script=script,
            scene_plan=scene_plan,
        )
        scenes = ensure_scene_entries(scenes)

        timing_log["prompt_seconds"] = round(time.monotonic() - step_start, 2)
        _save_scenes(project_id, scenes)
        _set_state(project_id, step="prompting_done", progress=48,
                   message=f"Generated {len(scenes)} scene prompts in "
                           f"{timing_log['prompt_seconds']:.1f}s. Starting image generation…",
                   total_scenes=len(scenes),
                   scenes_done=0)

        # ── Step 5: Generate images in parallel ──────────────────────────────
        step_start = time.monotonic()
        total = len(scenes)

        def on_progress(done: int, total: int, scene: dict) -> None:
            pct = 48 + int(done / total * 52)
            _save_scenes(project_id, scenes)
            _set_state(
                project_id,
                step="generating",
                progress=pct,
                message=f"Generated {done} of {total} images…",
                total_scenes=total,
                scenes_done=done,
            )

        scenes, image_summary = pipeline.generate_all_images(
            scenes=scenes,
            quality=meta["quality"],
            resolution=meta.get("resolution", config.DEFAULT_RESOLUTION),
            project_dir=project_dir,
            on_progress=on_progress,
        )

        timing_log["image_seconds"] = round(time.monotonic() - step_start, 2)
        timing_log["total_seconds"] = round(time.monotonic() - project_start, 2)
        timing_log["image_summary"] = image_summary
        timing_log["voiceover"] = voiceover_info
        timing_log["finished_at"] = time.time()

        # Persist final cost based on actually-successful images.
        successful = int(image_summary.get("successful", total) or 0)
        meta["cost_actual"] = _estimate_cost(
            meta.get("resolution"),
            meta.get("quality"),
            successful,
        )
        _meta_path(project_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # Persist timing log alongside scenes/meta
        (project_dir / "timing.json").write_text(
            json.dumps(timing_log, indent=2), encoding="utf-8"
        )

        # Print a nice summary to logs
        logger.info(
            f"━━━━━━━━━━━━ PROJECT SUMMARY  {meta['name']!r} ━━━━━━━━━━━━\n"
            f"  Scenes:      {image_summary['successful']}/{image_summary['total_scenes']} successful\n"
            f"  Resolution:  {image_summary['resolution']}  |  quality={image_summary['quality']}\n"
            f"  Analyse:     {timing_log['analyse_seconds']:.1f}s\n"
            f"  Prompts:     {timing_log['prompt_seconds']:.1f}s\n"
            f"  Images:      {timing_log['image_seconds']:.1f}s (wall clock)\n"
            f"  Total:       {timing_log['total_seconds']:.1f}s\n"
            f"  Avg/image:   {image_summary['avg_image_seconds']:.1f}s\n"
            f"  Fastest:     {image_summary['fastest_seconds']:.1f}s\n"
            f"  Slowest:     {image_summary['slowest_seconds']:.1f}s\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        _save_scenes(project_id, scenes)
        _set_state(
            project_id,
            step="done",
            progress=100,
            message=f"All scenes generated. Avg {image_summary['avg_image_seconds']:.1f}s/image, "
                    f"total {timing_log['total_seconds']:.1f}s.",
            total_scenes=total,
            scenes_done=total,
            timing_summary=image_summary,
            total_seconds=timing_log["total_seconds"],
        )
        logger.info(f"Project {project_id} completed — {total} scenes.")

    except Exception as exc:
        logger.error(f"Project {project_id} failed: {exc}", exc_info=True)
        _set_state(project_id, step="error", progress=0,
                   message=f"Generation failed: {exc}")


# ─── Export & regenerate helpers ──────────────────────────────────────────────

class ExportBlockedError(Exception):
    """Cannot build a zip while more than one scene row shares the same timeline slot."""

    def __init__(self, duplicate_slots: list[int]):
        self.duplicate_slots = duplicate_slots
        super().__init__("duplicate scene slots")


def _which_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _image_to_mp4(image_path: Path, out_mp4: Path, duration_sec: float) -> None:
    ff = _which_ffmpeg()
    if not ff:
        raise RuntimeError(
            "ffmpeg is not installed or not on PATH — install ffmpeg to export video chunks."
        )
    dur = max(0.05, float(duration_sec))
    cmd = [
        ff,
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-c:v",
        "libx264",
        "-t",
        str(dur),
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        str(out_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or "ffmpeg failed"
        raise RuntimeError(msg)


def _parallel_render_export_mp4s(
    ordered: list[dict],
    project_dir: Path,
    tmp_path: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Render scene stills to MP4 with parallel ffmpeg (bounded by EXPORT_FFMPEG_WORKERS)."""
    workers = max(1, int(config.EXPORT_FFMPEG_WORKERS))

    def _one(scene: dict) -> tuple[str, Path, str]:
        slot = int(scene.get("slot_number") or scene.get("scene_number") or 0)
        if scene.get("image_status") != "done":
            raise RuntimeError(
                f"Scene slot {slot} has no completed image; finish or remove failed rows first."
            )
        rel = str(scene.get("image_path", "")).replace("\\", "/")
        ip = project_dir / rel
        if not ip.exists():
            raise RuntimeError(f"Missing image for slot {slot:03d}: {ip.name}")

        dur = float(scene.get("duration") or 0.0)
        if dur <= 0:
            dur = float(scene.get("end_time", 0)) - float(scene.get("start_time", 0))

        mp4_name = f"scene_{slot:03d}.mp4"
        mp4_path = tmp_path / mp4_name
        _image_to_mp4(ip, mp4_path, dur)
        line = (
            f"{mp4_name}\tslot={slot:03d}\tstart={float(scene.get('start_time', 0)):.3f}\t"
            f"end={float(scene.get('end_time', 0)):.3f}\tduration={dur:.3f}"
        )
        return mp4_name, mp4_path, line

    mp4_jobs: list[tuple[str, Path]] = []
    manifest_lines: list[str] = []
    total = len(ordered)
    done = 0
    lock = threading.Lock()

    if workers <= 1 or total <= 1:
        for scene in ordered:
            name, path, line = _one(scene)
            mp4_jobs.append((name, path))
            manifest_lines.append(line)
            done += 1
            if on_progress:
                on_progress(done, total, name)
        mp4_jobs.sort(key=lambda x: x[0])
        manifest_lines.sort()
        return mp4_jobs, manifest_lines

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, scene) for scene in ordered]
        for future in as_completed(futures):
            name, path, line = future.result()
            with lock:
                mp4_jobs.append((name, path))
                manifest_lines.append(line)
                done += 1
                if on_progress:
                    on_progress(done, total, name)

    mp4_jobs.sort(key=lambda x: x[0])
    manifest_lines.sort()
    return mp4_jobs, manifest_lines


# ─── Export-job machinery (progress-aware ZIP build) ─────────────────────────

_export_jobs: dict[str, dict] = {}
_export_jobs_lock = threading.Lock()
_export_tmpdir = Path(tempfile.gettempdir()) / "tatterveil_exports"
_export_tmpdir.mkdir(parents=True, exist_ok=True)


def _export_job_public(job: dict) -> dict:
    """Return a view of an export job safe to send to clients."""
    keep = {
        "job_id",
        "project_id",
        "status",
        "stage",
        "message",
        "percent",
        "current",
        "total",
        "file_name",
        "size_bytes",
        "error",
        "duplicate_slots",
        "created_at",
        "updated_at",
    }
    return {k: v for k, v in job.items() if k in keep}


def _update_export_job(job_id: str, **fields) -> None:
    with _export_jobs_lock:
        j = _export_jobs.get(job_id)
        if j is None:
            return
        j.update(fields)
        j["updated_at"] = time.time()


def _gc_old_export_jobs(max_age_seconds: float = 1800.0) -> None:
    """Discard finished export jobs and their on-disk zips older than 30 min."""
    now = time.time()
    stale: list[str] = []
    with _export_jobs_lock:
        for jid, j in list(_export_jobs.items()):
            if j.get("status") not in ("done", "error"):
                continue
            if now - float(j.get("updated_at", 0)) > max_age_seconds:
                stale.append(jid)
        for jid in stale:
            j = _export_jobs.pop(jid, None)
            fp = (j or {}).get("file_path")
            if fp:
                try:
                    Path(fp).unlink(missing_ok=True)
                except OSError:
                    pass


def _run_export_job(project_id: str, job_id: str) -> None:
    """Background worker that builds the ZIP, updating progress as it goes."""
    try:
        meta = _load_meta(project_id)
        if not meta:
            _update_export_job(job_id, status="error", stage="failed",
                               error="Project not found")
            return

        scenes = _scenes_live(project_id)
        dup = duplicate_slot_numbers(scenes)
        if dup:
            _update_export_job(
                job_id,
                status="error",
                stage="blocked",
                error="Resolve duplicate timeline slots before exporting.",
                duplicate_slots=dup,
            )
            return

        ordered = sort_scenes_for_display(scenes)
        if not ordered:
            _update_export_job(job_id, status="error", stage="failed",
                               error="No scenes to export.")
            return

        for s in ordered:
            slot = int(s.get("slot_number") or s.get("scene_number") or 0)
            if s.get("image_status") != "done":
                _update_export_job(
                    job_id,
                    status="error",
                    stage="failed",
                    error=f"Slot {slot:03d} has no completed image; finish or remove failed rows first.",
                )
                return

        if _which_ffmpeg() is None:
            _update_export_job(
                job_id,
                status="error",
                stage="failed",
                error="ffmpeg is not installed or not on PATH — install ffmpeg to export.",
            )
            return

        project_dir = config.PROJECTS_DIR / project_id
        timing = _load_timing(project_id)

        raw_name = meta.get("name") or "project"
        slug = "".join(c if c.isalnum() or c in "._-" else "_" for c in raw_name)[:80] or "project"
        zip_filename = f"{slug}_tatterveil_export.zip"
        zip_path = _export_tmpdir / f"{job_id}.zip"

        # One combined voice-over for the whole video (if present).
        voiceover = meta.get("voiceover") or {}
        vo_rel = str(voiceover.get("path") or "").replace("\\", "/")
        vo_src = project_dir / vo_rel if vo_rel else None
        has_audio = bool(
            voiceover.get("status") == "done" and vo_src is not None and vo_src.exists()
        )
        vo_arcname = f"voiceovers/{voiceover.get('filename') or 'full_voiceover.wav'}" if has_audio else ""

        total_steps = len(ordered) + 1  # MP4 renders + final ZIP packaging step
        _update_export_job(
            job_id,
            status="running",
            stage="rendering_mp4s",
            current=0,
            total=total_steps,
            percent=0,
            message=f"Rendering {len(ordered)} MP4 chunks…",
        )

        manifest_lines = [
            "# filename | slot | start_sec | end_sec | duration_sec",
            "# Each MP4 is one still image held for the scene duration on the script timeline.",
        ]
        if has_audio:
            manifest_lines.append(
                f"# Combined narration for the whole video: {vo_arcname} "
                f"({float(voiceover.get('duration_seconds', 0)):.1f}s)."
            )

        raw_export = {
            "project_meta": meta,
            "timing": timing,
            "scenes": ordered,
            "exported_at": time.time(),
        }

        with tempfile.TemporaryDirectory(prefix=f"tatterveil_export_{job_id}_") as tmp:
            tmp_path = Path(tmp)
            total_mp4 = len(ordered)

            def on_mp4_progress(done: int, total: int, mp4_name: str) -> None:
                _update_export_job(
                    job_id,
                    stage="rendering_mp4s",
                    current=done,
                    percent=int(done * 100 / total_steps),
                    message=f"Rendering MP4 {done}/{total} — {mp4_name}",
                )

            try:
                mp4_jobs, mp4_manifest_lines = _parallel_render_export_mp4s(
                    ordered, project_dir, tmp_path, on_progress=on_mp4_progress
                )
            except RuntimeError as exc:
                _update_export_job(
                    job_id, status="error", stage="failed", error=str(exc)
                )
                return
            manifest_lines.extend(mp4_manifest_lines)
            done = total_mp4

            manifest = tmp_path / "scene_timestamps.txt"
            manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
            raw_json = tmp_path / "project_export_metadata.json"
            raw_json.write_text(
                json.dumps(raw_export, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            _update_export_job(
                job_id,
                stage="zipping",
                current=done,
                percent=int(done * 100 / total_steps),
                message="Packing ZIP archive…",
            )

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(manifest, arcname="scene_timestamps.txt")
                zf.write(raw_json, arcname="project_export_metadata.json")
                for name, pth in sorted(mp4_jobs, key=lambda x: x[0]):
                    zf.write(pth, arcname=name, compress_type=zipfile.ZIP_STORED)
                if has_audio:
                    zf.write(vo_src, arcname=vo_arcname, compress_type=zipfile.ZIP_STORED)

        size = zip_path.stat().st_size if zip_path.exists() else 0
        with _export_jobs_lock:
            j = _export_jobs.get(job_id)
            if j is not None:
                j.update(
                    {
                        "status": "done",
                        "stage": "ready",
                        "percent": 100,
                        "current": total_steps,
                        "message": "Archive ready to download.",
                        "file_path": str(zip_path),
                        "file_name": zip_filename,
                        "size_bytes": size,
                        "updated_at": time.time(),
                    }
                )

    except Exception as exc:
        logger.exception("Export job %s failed", job_id)
        _update_export_job(job_id, status="error", stage="failed", error=str(exc))


def _build_export_zip(project_id: str) -> tuple[bytes, str]:
    meta = _load_meta(project_id)
    if not meta:
        abort(404)
    scenes = _scenes_live(project_id)
    dup = duplicate_slot_numbers(scenes)
    if dup:
        raise ExportBlockedError(dup)

    ordered = sort_scenes_for_display(scenes)
    project_dir = config.PROJECTS_DIR / project_id
    timing = _load_timing(project_id)

    raw_name = meta.get("name") or "project"
    slug = "".join(c if c.isalnum() or c in "._-" else "_" for c in raw_name)[:80] or "project"

    voiceover = meta.get("voiceover") or {}
    vo_rel = str(voiceover.get("path") or "").replace("\\", "/")
    vo_src = project_dir / vo_rel if vo_rel else None
    has_audio = bool(
        voiceover.get("status") == "done" and vo_src is not None and vo_src.exists()
    )
    vo_arcname = f"voiceovers/{voiceover.get('filename') or 'full_voiceover.wav'}" if has_audio else ""

    buf = io.BytesIO()
    lines = [
        "# filename | slot | start_sec | end_sec | duration_sec",
        "# Each MP4 is one still image held for the scene duration on the script timeline.",
    ]
    if has_audio:
        lines.append(
            f"# Combined narration for the whole video: {vo_arcname} "
            f"({float(voiceover.get('duration_seconds', 0)):.1f}s)."
        )

    raw_export = {
        "project_meta": meta,
        "timing": timing,
        "scenes": ordered,
        "exported_at": time.time(),
    }

    with tempfile.TemporaryDirectory(prefix="tatterveil_export_") as tmp:
        tmp_path = Path(tmp)
        mp4_jobs, mp4_manifest_lines = _parallel_render_export_mp4s(
            ordered, project_dir, tmp_path
        )
        lines.extend(mp4_manifest_lines)

        manifest = tmp_path / "scene_timestamps.txt"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        raw_json = tmp_path / "project_export_metadata.json"
        raw_json.write_text(json.dumps(raw_export, indent=2, ensure_ascii=False), encoding="utf-8")

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(manifest, arcname="scene_timestamps.txt")
            zf.write(raw_json, arcname="project_export_metadata.json")
            for name, pth in sorted(mp4_jobs, key=lambda x: x[0]):
                zf.write(pth, arcname=name, compress_type=zipfile.ZIP_STORED)
            if has_audio:
                zf.write(vo_src, arcname=vo_arcname, compress_type=zipfile.ZIP_STORED)

    return buf.getvalue(), f"{slug}_tatterveil_export.zip"


# ─── Regeneration queue (up to REGEN_PARALLELISM concurrent image renders) ──

_regen_jobs: dict[str, dict] = {}
_regen_jobs_lock = threading.Lock()
_regen_executor_lock = threading.Lock()
_regen_executor: ThreadPoolExecutor | None = None


def _get_regen_executor() -> ThreadPoolExecutor:
    global _regen_executor
    with _regen_executor_lock:
        if _regen_executor is None:
            _regen_executor = ThreadPoolExecutor(
                max_workers=max(1, int(config.REGEN_PARALLELISM)),
                thread_name_prefix="regen",
            )
        return _regen_executor


def _regen_job_public(job: dict) -> dict:
    keep = {
        "job_id",
        "project_id",
        "parent_entry_id",
        "new_entry_id",
        "slot_number",
        "variant_index",
        "instructions",
        "status",          # queued | running | done | error
        "state",           # queued | refining_prompt | generating_image | done | error
        "stage_message",
        "error",
        "created_at",
        "updated_at",
    }
    return {k: v for k, v in job.items() if k in keep}


def _update_regen_job(job_id: str, **fields) -> None:
    with _regen_jobs_lock:
        j = _regen_jobs.get(job_id)
        if not j:
            return
        j.update(fields)
        j["updated_at"] = time.time()


def _list_regen_jobs(project_id: str, prune_max_age: float = 60.0) -> list[dict]:
    """Return jobs for a project, pruning finished ones older than `prune_max_age`."""
    now = time.time()
    out: list[dict] = []
    stale: list[str] = []
    with _regen_jobs_lock:
        for jid, j in _regen_jobs.items():
            if j.get("project_id") != project_id:
                continue
            if j.get("status") in ("done", "error") and (now - float(j.get("updated_at", 0))) > prune_max_age:
                stale.append(jid)
                continue
            out.append(_regen_job_public(j))
        for jid in stale:
            _regen_jobs.pop(jid, None)
    out.sort(key=lambda x: x.get("created_at", 0))
    return out


def _regen_busy_count(project_id: str) -> int:
    with _regen_jobs_lock:
        return sum(
            1
            for j in _regen_jobs.values()
            if j.get("project_id") == project_id and j.get("status") not in ("done", "error")
        )


def _enqueue_regen_job(project_id: str, parent_entry_id: str, instructions: str) -> dict:
    """Create + submit a regeneration job. Returns the public job view."""
    job_id = uuid.uuid4().hex
    now = time.time()
    job = {
        "job_id": job_id,
        "project_id": project_id,
        "parent_entry_id": parent_entry_id,
        "new_entry_id": None,
        "slot_number": None,
        "variant_index": None,
        "instructions": instructions,
        "status": "queued",
        "state": "queued",
        "stage_message": "Waiting for a worker…",
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    with _regen_jobs_lock:
        _regen_jobs[job_id] = job
    _get_regen_executor().submit(_run_regen_job, job_id)
    return _regen_job_public(job)


def _run_regen_job(job_id: str) -> None:
    """Worker: refine prompt → append new variant row → generate image."""
    with _regen_jobs_lock:
        j = _regen_jobs.get(job_id)
        if not j:
            return
        project_id = j["project_id"]
        parent_entry_id = j["parent_entry_id"]
        instructions = j["instructions"]

    try:
        meta = _load_meta(project_id)
        if not meta:
            _update_regen_job(job_id, status="error", state="error",
                              stage_message="Project not found", error="Project not found")
            return

        # Snapshot the parent to read its prompt/segment outside the scene lock.
        with _scene_lock(project_id):
            scenes = _scenes_live(project_id)
            hit = find_entry(scenes, parent_entry_id)
            if hit is None:
                _update_regen_job(job_id, status="error", state="error",
                                  stage_message="Parent scene no longer exists",
                                  error="Parent scene was deleted")
                return
            _, base = hit
            slot = int(base.get("slot_number") or base.get("scene_number") or 0)

        _update_regen_job(
            job_id,
            status="running",
            state="refining_prompt",
            slot_number=slot,
            stage_message="Composing new prompt with your instructions…",
        )

        # LLM prompt refinement happens *outside* the scene lock so multiple
        # workers can call the text model in parallel.
        new_prompt, new_neg = pipeline.refine_prompt_for_regeneration(
            previous_prompt=base.get("prompt") or "",
            previous_negative=base.get("negative_prompt"),
            script_segment=base.get("script_segment") or "",
            user_instructions=instructions,
        )

        # Append the new variant row under the scene lock so concurrent regens
        # don't collide on variant_index or filename.
        with _scene_lock(project_id):
            scenes = _scenes_live(project_id)
            v = next_variant_index(scenes, slot)
            neo: dict = dict(base)
            neo["entry_id"] = uuid.uuid4().hex
            neo["variant_index"] = v
            neo["prompt"] = new_prompt
            neo["negative_prompt"] = new_neg
            neo["image_status"] = "pending"
            neo["image_error"] = None
            neo["image_seconds"] = None
            neo["regenerated_from_entry_id"] = parent_entry_id
            neo["image_filename"] = image_filename_for_scene(neo)
            neo["image_path"] = f"images/{neo['image_filename']}"
            scenes.append(neo)
            _save_scenes(project_id, scenes)

        _update_regen_job(
            job_id,
            state="generating_image",
            new_entry_id=neo["entry_id"],
            variant_index=v,
            stage_message="Rendering the new image…",
        )

        project_dir = config.PROJECTS_DIR / project_id
        try:
            out_path, elapsed = pipeline.generate_image(
                neo,
                meta["quality"],
                meta.get("resolution", config.DEFAULT_RESOLUTION),
                project_dir,
            )
            with _scene_lock(project_id):
                scenes = _scenes_live(project_id)
                hit2 = find_entry(scenes, neo["entry_id"])
                if hit2 is not None:
                    i, row = hit2
                    row["image_path"] = out_path.relative_to(project_dir).as_posix()
                    row["image_filename"] = out_path.name
                    row["image_status"] = "done"
                    row["image_seconds"] = round(elapsed, 2)
                    scenes[i] = row
                    _save_scenes(project_id, scenes)
            _update_regen_job(job_id, status="done", state="done",
                              stage_message=f"Done in {elapsed:.1f}s")
        except Exception as exc:
            logger.error("Regenerate image failed for job %s: %s", job_id, exc, exc_info=True)
            with _scene_lock(project_id):
                scenes = _scenes_live(project_id)
                hit2 = find_entry(scenes, neo["entry_id"])
                if hit2 is not None:
                    i, row = hit2
                    row["image_status"] = "error"
                    row["image_error"] = str(exc)
                    scenes[i] = row
                    _save_scenes(project_id, scenes)
            _update_regen_job(job_id, status="error", state="error",
                              stage_message="Image generation failed",
                              error=str(exc))
    except Exception as exc:
        logger.exception("Regenerate job %s crashed", job_id)
        _update_regen_job(job_id, status="error", state="error",
                          stage_message="Job crashed", error=str(exc))


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    payload = _projects_api_payload()
    return render_template(
        "index.html",
        projects=payload["projects"],
        active_generation=payload["active_generation"],
        generation_locked=payload["generation_locked"],
    )


@app.route("/api/projects")
def api_list_projects():
    """JSON project list + whether a new batch generation may be started."""
    return jsonify(_projects_api_payload())


@app.route("/api/estimate", methods=["POST"])
def api_estimate():
    """Return live scene-count + cost estimate for the form preview."""
    data = request.get_json(silent=True) or {}
    script = data.get("script", "")
    first_rate = int(data.get("first_rate", 3))
    rest_rate = int(data.get("rest_rate", 2))
    resolution = data.get("resolution") or config.DEFAULT_RESOLUTION
    quality = data.get("quality") or config.DEFAULT_QUALITY
    voice_speed = _parse_voice_speed(data.get("voice_speed", config.DEFAULT_VOICE_SPEED))

    duration = pipeline.estimate_duration(script, voice_speed) if script.strip() else 0.0
    plan = pipeline.compute_scene_plan(duration, first_rate, rest_rate)
    cost = _estimate_cost(resolution, quality, plan.get("total_scenes", 0))
    return jsonify({**plan, "cost": cost, "voice_speed": voice_speed})


@app.route("/api/pricing")
def api_pricing():
    """Static pricing table used by the UI to draw the cost preview."""
    return jsonify(
        {
            "image_costs": config.IMAGE_COSTS,
            "prompt_overhead_usd": round(float(config.PROMPT_GENERATION_FLAT_COST), 4),
        }
    )


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """Create a new project and start background generation."""
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "Untitled").strip()
    script = (data.get("script") or "").strip()
    style = data.get("style", "Tatterveil")
    aspect_ratio = data.get("aspect_ratio", "16:9")
    quality = data.get("quality", config.DEFAULT_QUALITY)
    resolution = data.get("resolution", config.DEFAULT_RESOLUTION)
    first_rate = int(data.get("first_rate", 3))
    rest_rate = int(data.get("rest_rate", 2))
    voice_speed = _parse_voice_speed(data.get("voice_speed", config.DEFAULT_VOICE_SPEED))

    if not script:
        return jsonify({"error": "Script is required."}), 400
    if len(script.split()) < 10:
        return jsonify({"error": "Script is too short. Please provide more content."}), 400
    if quality not in config.QUALITY_OPTIONS:
        return jsonify({"error": f"Invalid quality. Choose: {list(config.QUALITY_OPTIONS)}"}), 400
    if resolution not in config.RESOLUTION_PRESETS:
        return jsonify({"error": f"Invalid resolution. Choose: {list(config.RESOLUTION_PRESETS)}"}), 400
    first_rate = max(1, min(first_rate, 10))
    rest_rate = max(1, min(rest_rate, 10))

    active = _find_active_generation()
    if active:
        return (
            jsonify(
                {
                    "error": (
                        "Another project is still generating. "
                        "Wait for it to finish or open it from Recent Projects."
                    ),
                    "active_project_id": active["id"],
                    "active_project_name": active.get("name"),
                    "progress": active.get("progress"),
                }
            ),
            409,
        )

    project_id = uuid.uuid4().hex
    project_dir = config.PROJECTS_DIR / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": project_id,
        "name": name,
        "script": script,
        "style": style,
        "aspect_ratio": aspect_ratio,
        "quality": quality,
        "resolution": resolution,
        "first_rate": first_rate,
        "rest_rate": rest_rate,
        "voice_speed": voice_speed,
        "created_at": time.time(),
        "scene_plan": {},
    }
    _meta_path(project_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    _set_state(project_id, step="queued", progress=0,
               message="Project queued. Starting generation…")

    thread = threading.Thread(target=_run_generation, args=(project_id, meta), daemon=True)
    thread.start()

    return jsonify({"project_id": project_id})


@app.route("/api/projects/<project_id>/status")
def api_status(project_id: str):
    """Polling endpoint: returns current generation status + available scenes."""
    state = _get_state(project_id)
    if state is None:
        abort(404)

    meta = _load_meta(project_id) or {}
    scenes = _scenes_live(project_id)
    dup_slots = duplicate_slot_numbers(scenes)
    export_blocked = bool(dup_slots)
    regen_jobs = _list_regen_jobs(project_id)
    regen_busy = any(j.get("status") not in ("done", "error") for j in regen_jobs)

    safe_scenes = []
    dup_set = set(dup_slots)
    for s in scenes:
        slot = int(s.get("slot_number") or s.get("scene_number") or 0)
        safe_scenes.append(
            {
                "entry_id": s.get("entry_id"),
                "slot_number": slot,
                "variant_index": int(s.get("variant_index", 0)),
                "scene_number": s.get("scene_number"),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "duration": s.get("duration"),
                "scene_type": s.get("scene_type"),
                "scene_type_name": s.get("scene_type_name"),
                "time_period": s.get("time_period"),
                "abstraction_mode": s.get("abstraction_mode"),
                "script_segment": s.get("script_segment", ""),
                "prompt": s.get("prompt", ""),
                "negative_prompt": s.get("negative_prompt"),
                "time_period_reasoning": s.get("time_period_reasoning"),
                "scene_type_reasoning": s.get("scene_type_reasoning"),
                "image_path": s.get("image_path"),
                "image_status": s.get("image_status", "pending"),
                "image_error": s.get("image_error"),
                "slot_has_duplicates": slot in dup_set,
            }
        )

    return jsonify(
        {
            **state,
            "scenes": safe_scenes,
            "duplicate_slots": dup_slots,
            "export_blocked": export_blocked,
            "regeneration_jobs": regen_jobs,
            "regeneration": {
                "busy": regen_busy,
                "active_count": sum(
                    1 for j in regen_jobs if j.get("status") not in ("done", "error")
                ),
                "max_parallel": int(config.REGEN_PARALLELISM),
            },
            "cost_estimate": meta.get("cost_estimate"),
            "cost_actual": meta.get("cost_actual"),
            "voiceover": _voiceover_public(meta.get("voiceover")),
        }
    )


def _voiceover_public(vo: dict | None) -> dict | None:
    """Client-facing view of the combined voice-over (adds a stream URL)."""
    if not vo or not isinstance(vo, dict):
        return None
    out = dict(vo)
    if vo.get("status") == "done" and vo.get("filename"):
        out["url"] = f"voiceovers/{vo['filename']}"
    return out


@app.route("/projects/<project_id>")
def project_view(project_id: str):
    meta = _load_meta(project_id)
    if meta is None:
        abort(404)
    state = _get_state(project_id) or {}
    scenes_all = _scenes_live(project_id)
    scenes = sort_scenes_for_display(scenes_all)
    duplicate_slots = duplicate_slot_numbers(scenes_all)
    timing = _load_timing(project_id)
    has_done_image = any(s.get("image_status") == "done" for s in scenes_all)
    step = state.get("step", "queued")
    export_available = bool(
        not duplicate_slots
        and step in ("done", "error")
        and has_done_image
    )
    is_generating = _step_is_generating(step)
    return render_template(
        "project.html",
        project=meta,
        state=state,
        scenes=scenes,
        duplicate_slots=duplicate_slots,
        export_available=export_available,
        timing=timing,
        is_generating=is_generating,
        cost_estimate=meta.get("cost_estimate"),
        cost_actual=meta.get("cost_actual"),
        voiceover=_voiceover_public(meta.get("voiceover")),
        regen_parallelism=int(config.REGEN_PARALLELISM),
        scene_type_names=SCENE_TYPE_NAMES,
        scene_type_colors=SCENE_TYPE_COLORS,
        period_labels=PERIOD_LABELS,
    )


@app.route("/projects/<project_id>/images/<filename>")
def serve_image(project_id: str, filename: str):
    img_path = config.PROJECTS_DIR / project_id / "images" / filename
    if not img_path.exists():
        abort(404)
    mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    resp = send_file(img_path, mimetype=mime, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/projects/<project_id>/previews/<filename>")
def serve_preview(project_id: str, filename: str):
    """Grid-sized JPEG preview (~100–400 KB) instead of full 4K PNG through Flask."""
    img_path = config.PROJECTS_DIR / project_id / "images" / filename
    if not img_path.exists():
        abort(404)
    try:
        thumb = thumbnails.ensure_thumbnail(img_path)
    except Exception as exc:
        logger.exception("Preview generation failed for %s", filename)
        abort(500, description=str(exc))
    if thumb is None or not thumb.exists():
        abort(404)
    resp = send_file(thumb, mimetype="image/jpeg", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/projects/<project_id>/voiceovers/<filename>")
def serve_voiceover(project_id: str, filename: str):
    audio_path = config.PROJECTS_DIR / project_id / "voiceovers" / filename
    if not audio_path.exists():
        abort(404)
    lower = filename.lower()
    if lower.endswith(".wav"):
        mime = "audio/wav"
    elif lower.endswith(".ogg"):
        mime = "audio/ogg"
    else:
        mime = "audio/mpeg"
    return send_file(audio_path, mimetype=mime)


@app.route("/api/projects/<project_id>/export.zip")
def api_export_zip(project_id: str):
    if _load_meta(project_id) is None:
        abort(404)
    try:
        data, fname = _build_export_zip(project_id)
    except ExportBlockedError as e:
        return (
            jsonify(
                {
                    "error": "Resolve duplicate timeline slots before exporting.",
                    "duplicate_slots": e.duplicate_slots,
                }
            ),
            409,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    bio = io.BytesIO(data)
    bio.seek(0)
    return send_file(
        bio,
        mimetype="application/zip",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/api/projects/<project_id>/scenes/<entry_id>/regenerate", methods=["POST"])
def api_regenerate_scene(project_id: str, entry_id: str):
    """Queue a single regeneration job. Up to REGEN_PARALLELISM run in parallel."""
    payload = request.get_json(silent=True) or {}
    instr = (payload.get("instructions") or "").strip()
    if not instr:
        return jsonify({"error": "Instructions are required."}), 400
    if _load_meta(project_id) is None:
        abort(404)
    scenes = _scenes_live(project_id)
    if find_entry(scenes, entry_id) is None:
        abort(404)
    job = _enqueue_regen_job(project_id, entry_id, instr)
    return jsonify({"ok": True, "job": job})


@app.route("/api/projects/<project_id>/regenerations", methods=["GET"])
def api_list_regen_jobs(project_id: str):
    if _load_meta(project_id) is None:
        abort(404)
    return jsonify(
        {
            "jobs": _list_regen_jobs(project_id),
            "max_parallel": int(config.REGEN_PARALLELISM),
        }
    )


@app.route("/api/projects/<project_id>/regenerations/<job_id>", methods=["DELETE"])
def api_dismiss_regen_job(project_id: str, job_id: str):
    """Remove a *finished* job from the visible queue (running jobs are kept)."""
    with _regen_jobs_lock:
        j = _regen_jobs.get(job_id)
        if not j or j.get("project_id") != project_id:
            abort(404)
        if j.get("status") not in ("done", "error"):
            return (
                jsonify({"error": "Job is still running; wait for it to finish."}),
                409,
            )
        _regen_jobs.pop(job_id, None)
    return jsonify({"ok": True})


# ─── Export job endpoints (progress-aware) ───────────────────────────────────

@app.route("/api/projects/<project_id>/exports", methods=["POST"])
def api_create_export_job(project_id: str):
    if _load_meta(project_id) is None:
        abort(404)
    _gc_old_export_jobs()
    scenes = _scenes_live(project_id)
    dup = duplicate_slot_numbers(scenes)
    if dup:
        return (
            jsonify(
                {
                    "error": "Resolve duplicate timeline slots before exporting.",
                    "duplicate_slots": dup,
                }
            ),
            409,
        )

    job_id = uuid.uuid4().hex
    now = time.time()
    with _export_jobs_lock:
        _export_jobs[job_id] = {
            "job_id": job_id,
            "project_id": project_id,
            "status": "queued",
            "stage": "queued",
            "percent": 0,
            "current": 0,
            "total": 0,
            "message": "Queued…",
            "file_path": None,
            "file_name": None,
            "size_bytes": 0,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
    threading.Thread(target=_run_export_job, args=(project_id, job_id), daemon=True).start()
    with _export_jobs_lock:
        job = _export_jobs[job_id]
        return jsonify({"job": _export_job_public(job)})


@app.route("/api/projects/<project_id>/exports/<job_id>", methods=["GET"])
def api_get_export_job(project_id: str, job_id: str):
    with _export_jobs_lock:
        j = _export_jobs.get(job_id)
        if not j or j.get("project_id") != project_id:
            abort(404)
        return jsonify({"job": _export_job_public(j)})


@app.route("/api/projects/<project_id>/exports/<job_id>/file", methods=["GET"])
def api_download_export(project_id: str, job_id: str):
    with _export_jobs_lock:
        j = _export_jobs.get(job_id)
        if not j or j.get("project_id") != project_id:
            abort(404)
        if j.get("status") != "done":
            return jsonify({"error": "Export not ready yet."}), 409
        fp = j.get("file_path")
        fn = j.get("file_name") or "export.zip"
    if not fp or not Path(fp).exists():
        return jsonify({"error": "Export file is no longer available."}), 410
    return send_file(
        fp,
        mimetype="application/zip",
        as_attachment=True,
        download_name=fn,
    )


@app.route("/api/projects/<project_id>/scenes/<entry_id>", methods=["DELETE"])
def api_delete_scene_row(project_id: str, entry_id: str):
    if _load_meta(project_id) is None:
        abort(404)
    scenes = _scenes_live(project_id)
    removed: dict | None = None
    kept: list[dict] = []
    for s in scenes:
        if s.get("entry_id") == entry_id:
            removed = s
        else:
            kept.append(s)
    if removed is None:
        abort(404)
    rel = removed.get("image_path")
    if rel:
        ip = config.PROJECTS_DIR / project_id / str(rel).replace("\\", "/")
        if ip.exists():
            try:
                ip.unlink()
            except OSError:
                pass
    _save_scenes(project_id, kept)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id: str):
    project_dir = config.PROJECTS_DIR / project_id
    if project_dir.exists():
        shutil.rmtree(project_dir)
    with _state_lock:
        _state.pop(project_id, None)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
