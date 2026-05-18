"""
Tatterveil Scene Studio — Flask application.
"""

import io
import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
import tempfile
import zipfile
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
from engine import pipeline
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


def _list_projects() -> list[dict]:
    projects = []
    for d in sorted(config.PROJECTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if d.is_dir():
            meta = _load_meta(d.name)
            state = _get_state(d.name)
            if meta:
                projects.append({
                    "id": d.name,
                    "name": meta.get("name", "Untitled"),
                    "style": meta.get("style", "Tatterveil"),
                    "quality": meta.get("quality", "medium"),
                    "total_scenes": meta.get("scene_plan", {}).get("total_scenes", 0),
                    "duration_minutes": meta.get("scene_plan", {}).get("duration_minutes", 0),
                    "step": state.get("step", "unknown") if state else "unknown",
                    "progress": state.get("progress", 0) if state else 0,
                    "created_at": meta.get("created_at", 0),
                })
    return projects


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
                   message="Analysing script length and estimating duration…")

        script = meta["script"]
        duration = pipeline.estimate_duration(script)
        scene_plan = pipeline.compute_scene_plan(
            duration=duration,
            first_rate=meta["first_rate"],
            rest_rate=meta["rest_rate"],
        )
        # Update meta with computed plan
        meta["scene_plan"] = scene_plan
        _meta_path(project_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")

        timing_log["analyse_seconds"] = round(time.monotonic() - step_start, 2)
        _set_state(project_id, step="analysing", progress=8,
                   message=f"Script is ~{duration:.1f} min → {scene_plan['total_scenes']} scenes planned",
                   scene_plan=scene_plan)

        # ── Step 2: Split script + generate prompts ──────────────────────────
        step_start = time.monotonic()
        _set_state(project_id, step="prompting", progress=12,
                   message="Splitting script into scenes and generating visual prompts…")

        scenes = pipeline.split_and_prompt(
            title=meta["name"],
            script=script,
            scene_plan=scene_plan,
        )
        scenes = ensure_scene_entries(scenes)

        timing_log["prompt_seconds"] = round(time.monotonic() - step_start, 2)
        _save_scenes(project_id, scenes)
        _set_state(project_id, step="prompting_done", progress=25,
                   message=f"Generated {len(scenes)} scene prompts in "
                           f"{timing_log['prompt_seconds']:.1f}s. Starting image generation…",
                   total_scenes=len(scenes),
                   scenes_done=0)

        # ── Step 3: Generate images in parallel ──────────────────────────────
        step_start = time.monotonic()
        total = len(scenes)

        def on_progress(done: int, total: int, scene: dict) -> None:
            pct = 25 + int(done / total * 75)
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
        timing_log["finished_at"] = time.time()

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

    buf = io.BytesIO()
    lines = [
        "# filename | slot | start_sec | end_sec | duration_sec",
        "# Each MP4 is one still image held for the scene duration on the script timeline.",
    ]

    raw_export = {
        "project_meta": meta,
        "timing": timing,
        "scenes": ordered,
        "exported_at": time.time(),
    }

    with tempfile.TemporaryDirectory(prefix="tatterveil_export_") as tmp:
        tmp_path = Path(tmp)
        mp4_jobs: list[tuple[str, Path]] = []

        for s in ordered:
            slot = int(s.get("slot_number") or s.get("scene_number") or 0)
            if s.get("image_status") != "done":
                raise RuntimeError(
                    f"Scene slot {slot} has no completed image; finish or remove failed rows first."
                )
            rel = str(s.get("image_path", "")).replace("\\", "/")
            ip = project_dir / rel
            if not ip.exists():
                raise RuntimeError(f"Missing image for slot {slot}: {ip.name}")

            dur = float(s.get("duration") or 0.0)
            if dur <= 0:
                dur = float(s.get("end_time", 0)) - float(s.get("start_time", 0))

            mp4_name = f"scene_{slot:03d}.mp4"
            mp4_path = tmp_path / mp4_name
            _image_to_mp4(ip, mp4_path, dur)
            mp4_jobs.append((mp4_name, mp4_path))

            lines.append(
                f"{mp4_name}\tslot={slot:03d}\tstart={float(s.get('start_time', 0)):.3f}\t"
                f"end={float(s.get('end_time', 0)):.3f}\tduration={dur:.3f}"
            )

        manifest = tmp_path / "scene_timestamps.txt"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        raw_json = tmp_path / "project_export_metadata.json"
        raw_json.write_text(json.dumps(raw_export, indent=2, ensure_ascii=False), encoding="utf-8")

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(manifest, arcname="scene_timestamps.txt")
            zf.write(raw_json, arcname="project_export_metadata.json")
            for name, pth in sorted(mp4_jobs, key=lambda x: x[0]):
                zf.write(pth, arcname=name)

    return buf.getvalue(), f"{slug}_tatterveil_export.zip"


def _run_regenerate_worker(project_id: str, parent_entry_id: str, instructions: str) -> None:
    try:
        _set_state(
            project_id,
            regeneration={"busy": True, "parent_entry_id": parent_entry_id},
        )
        meta = _load_meta(project_id)
        if not meta:
            return
        scenes = _scenes_live(project_id)
        hit = find_entry(scenes, parent_entry_id)
        if hit is None:
            logger.warning("Regenerate: unknown entry %s", parent_entry_id)
            return
        _, base = hit
        slot = int(base.get("slot_number") or base.get("scene_number") or 0)
        v = next_variant_index(scenes, slot)

        new_prompt, new_neg = pipeline.refine_prompt_for_regeneration(
            previous_prompt=base.get("prompt") or "",
            previous_negative=base.get("negative_prompt"),
            script_segment=base.get("script_segment") or "",
            user_instructions=instructions,
        )

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

        project_dir = config.PROJECTS_DIR / project_id
        try:
            out_path, elapsed = pipeline.generate_image(
                neo,
                meta["quality"],
                meta.get("resolution", config.DEFAULT_RESOLUTION),
                project_dir,
            )
            neo["image_path"] = out_path.relative_to(project_dir).as_posix()
            neo["image_filename"] = out_path.name
            neo["image_status"] = "done"
            neo["image_seconds"] = round(elapsed, 2)
        except Exception as exc:
            logger.error("Regenerate image failed: %s", exc, exc_info=True)
            neo["image_status"] = "error"
            neo["image_error"] = str(exc)
        _save_scenes(project_id, scenes)
    finally:
        _set_state(project_id, regeneration={"busy": False, "parent_entry_id": None})


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    projects = _list_projects()
    return render_template("index.html", projects=projects)


@app.route("/api/estimate", methods=["POST"])
def api_estimate():
    """Return live scene-count estimate for the form preview."""
    data = request.get_json(silent=True) or {}
    script = data.get("script", "")
    first_rate = int(data.get("first_rate", 3))
    rest_rate = int(data.get("rest_rate", 2))

    duration = pipeline.estimate_duration(script) if script.strip() else 0.0
    plan = pipeline.compute_scene_plan(duration, first_rate, rest_rate)
    return jsonify(plan)


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

    scenes = _scenes_live(project_id)
    dup_slots = duplicate_slot_numbers(scenes)
    export_blocked = bool(dup_slots)

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
        }
    )


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
    return render_template(
        "project.html",
        project=meta,
        state=state,
        scenes=scenes,
        duplicate_slots=duplicate_slots,
        export_available=export_available,
        timing=timing,
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
    return send_file(img_path, mimetype=mime)


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
    payload = request.get_json(silent=True) or {}
    instr = (payload.get("instructions") or "").strip()
    if not instr:
        return jsonify({"error": "Instructions are required."}), 400
    if _load_meta(project_id) is None:
        abort(404)
    scenes = _scenes_live(project_id)
    if find_entry(scenes, entry_id) is None:
        abort(404)
    st = _get_state(project_id) or {}
    if st.get("regeneration", {}).get("busy"):
        return jsonify({"error": "Another regeneration is already running."}), 429

    threading.Thread(
        target=_run_regenerate_worker,
        args=(project_id, entry_id, instr),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


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
