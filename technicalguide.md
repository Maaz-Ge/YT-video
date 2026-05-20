# Tatterveil Scene Studio — Technical Guide

This document explains how each major subsystem fits together end-to-end: data model, backend workers, APIs, frontend polling, regeneration, exports, and external dependencies.

---

## 1. Repository layout

| Area | Role |
|------|------|
| `app.py` | Flask app: routes, persisted `status.json`, scene CRUD, ZIP export, regenerate worker orchestration. |
| `config.py` | Environment variables, paths (`PROJECTS_DIR`), model names, quality/resolution presets. |
| `engine/pipeline.py` | Duration estimate → scene plan → LLM split/prompt → parallel image generation → prompt refinement for regeneration. |
| `engine/style_guide.py` | Tatterveil rules and the **system** prompt for the initial scene split. |
| `engine/scene_utils.py` | Stable `entry_id`s, filenames (`scene_001.png` vs `scene_001_v1.png`), sorting, duplicate-slot detection. |
| `templates/` | Jinja HTML; `project.html` SSR scene cards + modal. |
| `static/js/app.js` | Form handling, polling, grid sync, regenerate modal, export download. |
| `projects/<id>/` | Per-project folder: `meta.json`, `status.json`, `scenes.json`, `timing.json`, `images/`. |

---

## 2. Project folder on disk

```
projects/<project_id>/
  meta.json           # User-facing settings: title, script, style, quality, resolution, rates, scene_plan, created_at
  status.json         # Live step, progress, messages, optional regeneration flag
  scenes.json         # Array of **scene rows** (see §3)
  timing.json         # Written when initial batch finishes: step timings + image_summary
  images/             # PNG outputs
```

- **`meta.script`** holds the full original script (also duplicated inside export metadata for archival).
- **`status.json`** is merged with in-memory `_state` for fast reads during polling.

---

## 3. Scene row model (`scenes.json`)

Each element is one **variant** of a **timeline slot**:

| Field | Meaning |
|-------|---------|
| `entry_id` | Stable UUID hex; primary key for DOM, API, and image job tracking. |
| `slot_number` / `scene_number` | Timeline index (1-based). Both are kept in sync for backwards compatibility. |
| `start_time`, `end_time`, `duration` | Seconds on the virtual narration timeline (from the LLM split). |
| `variant_index` | `0` = first image for that slot; `1+` = regenerations. |
| `script_segment`, `scene_type`, `time_period`, etc. | Classification + text from the splitter LLM. |
| `prompt`, `negative_prompt` | Text passed (directly or merged) into image generation. |
| `image_filename`, `image_path` | Relative paths under the project dir, e.g. `images/scene_003_v1.png`. |
| `image_status` | `pending` → `done` or `error`. |
| `regenerated_from_entry_id` | Set on new rows created by regenerate (audit). |

### Filenames

- Slot `N`, variant `0`: `scene_NNN.png`
- Slot `N`, variant `≥1`: `scene_NNN_vV.png`

`engine/scene_utils.ensure_scene_entries()` normalizes older projects: assigns missing `entry_id`, aligns `slot_number`, and derives `image_filename` from `image_path` when needed. `_scenes_live()` in `app.py` persists those migrations once.

### Duplicate slots

A **duplicate** means: more than one row shares the same `slot_number`. That is intentional after **Regenerate**, which **appends** a new row instead of replacing the old PNG.

`duplicate_slot_numbers()` returns sorted slot indices; the UI and `export_blocked` flag use this to block ZIP export until the user deletes extras.

---

## 4. Generation pipeline (initial project)

Implemented in `_run_generation()` + `engine/pipeline.py`.

1. **Analyse** — `estimate_duration(script)` uses word count ÷ `WORDS_PER_MINUTE`. `compute_scene_plan()` applies dual rates (first segment vs rest).
2. **Split + prompt** — Single chat completion with JSON output; `split_and_prompt()` parses robustly (markdown fences, alternate keys).
3. **Normalize rows** — `ensure_scene_entries(scenes)` adds ids and filenames.
4. **Images** — `generate_all_images()` uses a `ThreadPoolExecutor` (`MAX_WORKERS`). Each future is keyed by **`entry_id`**, not `scene_number`, so duplicate slots would not overwrite each other.

`on_progress` persists `scenes.json` and updates `status.json` so the UI can show partial results.

---

## 5. Image generation

`generate_image(scene, quality, resolution, project_dir)`:

- Builds the API prompt via `_build_final_prompt()` (negative constraints inlined; gpt-image-2 has no separate negative field).
- Writes bytes to `image_output_path()` / `scene_utils.image_filename_for_scene()`.
- Retries up to `MAX_RETRIES` with backoff.

---

## 6. Regeneration queue (up to `REGEN_PARALLELISM` in parallel)

**Route:** `POST /api/projects/<id>/scenes/<entry_id>/regenerate`
**Body:** `{ "instructions": "..." }`
**Response:** `{ "ok": true, "job": { ...public job view... } }`

Regenerations no longer block — every request enqueues a job in a shared `ThreadPoolExecutor(max_workers=REGEN_PARALLELISM)` (default **4**). Users can queue as many requests as they like; up to 4 image renders run simultaneously and the rest wait their turn.

Each job is a dict in `_regen_jobs` with the lifecycle:

```
queued → refining_prompt → generating_image → done
                                         ↘  error
```

| Field | Meaning |
|-------|---------|
| `job_id` | UUID hex. |
| `parent_entry_id` | The scene row the user clicked **Regenerate** on. |
| `new_entry_id` | Filled in once the new variant row is appended to `scenes.json`. |
| `slot_number`, `variant_index` | Set after the new row exists. |
| `state` | `queued` / `refining_prompt` / `generating_image` / `done` / `error`. |
| `stage_message` | Human-friendly label shown in the UI panel. |
| `error` | Populated on failure. |
| `created_at`, `updated_at` | Wall-clock timestamps for sorting + auto-pruning. |

`_run_regen_job()` orchestrates the worker:

1. Read the parent row under the per-project **scene lock** (`_scene_lock(project_id)`).
2. **Outside the lock**, call `refine_prompt_for_regeneration()` — this LLM call runs in parallel across all workers, so multiple prompt rewrites can be in flight at once.
3. Re-acquire the scene lock, compute `next_variant_index(slot)`, append the new row (`image_status="pending"`), and persist `scenes.json`. The lock guarantees no two parallel jobs land on the same `variant_index`.
4. Update the job to `state="generating_image"` and call `pipeline.generate_image()` for the new row.
5. On success, re-acquire the scene lock, write `image_path / image_status="done" / image_seconds`. On failure, persist `image_status="error"` + error string.

Finished jobs stay visible for **60s** so the UI can show their final state, then `_list_regen_jobs()` prunes them automatically on the next poll. The user can also dismiss a finished job manually via `DELETE /api/projects/<id>/regenerations/<job_id>`.

The status endpoint reports the full queue:

```jsonc
{
  "regeneration_jobs": [ { "job_id": "...", "state": "generating_image", ... } ],
  "regeneration": { "busy": true, "active_count": 2, "max_parallel": 4 }
}
```

Frontend polling uses these to render a **Regeneration queue** card above the scene grid and a small badge overlay on each in-flight new variant card showing "Generating new image…".

---

## 7. Deleting a variant

**Route:** `DELETE /api/projects/<id>/scenes/<entry_id>`

Removes the row from `scenes.json` and deletes the PNG if present. Deletes do not shift `slot_number`s (timeline stays stable).

---

## 8. Cost calculation

Per-image cost lives in `config.IMAGE_COSTS` and follows the user-supplied price table:

| Resolution | Low | Medium | High |
|------------|-----|--------|------|
| 1280×720   | $0.003 | $0.028 | $0.114 |
| 2048×1152  | $0.005 | $0.042 | $0.170 |
| 3840×2160  | $0.011 | $0.100 | $0.400 |

A flat **`PROMPT_GENERATION_FLAT_COST = 1.00`** (USD) is added once per project to cover the LLM scene-splitter + per-scene prompt-writer text-model usage.

`_estimate_cost(resolution, quality, total_scenes)` returns:

```jsonc
{
  "per_image_usd": 0.042,
  "images_subtotal_usd": 1.26,
  "prompt_overhead_usd": 1.00,
  "total_usd": 2.26,
  "total_scenes": 30,
  "resolution": "2048x1152",
  "quality": "medium"
}
```

Lifecycle of cost in `meta.json`:

- `cost_estimate` — saved when the scene plan is computed (start of `_run_generation`). Counts **planned** scene count.
- `cost_actual` — saved when generation finishes. Counts **successful** images only (failed renders are not billed).

The frontend uses this in three places:

1. **Live estimate panel** on the index page — updates whenever script/rates/resolution/quality change. Calls `POST /api/estimate` and reads back `data.cost`.
2. **Cost-confirmation modal** that intercepts the Generate Scenes submit. Shows resolution / quality / scenes / per-image / images subtotal / prompt overhead / **estimated total**. Generation only runs after the user clicks **Confirm & Generate**.
3. **Project settings card** on the project page — shows `~$X.XXXX` while in progress (estimate) and `$X.XXXX` final once `cost_actual` lands.

`GET /api/pricing` returns the raw table for any UI consumer that wants to render its own preview.

---

## 9. ZIP export with progress

ZIP builds are now backed by an in-memory job registry so the UI can poll progress while ffmpeg renders each MP4.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/projects/<id>/exports` | Start a job. Returns `{ "job": { job_id, status, ... } }`. Rejects with **409** if duplicate slots exist. |
| GET | `/api/projects/<id>/exports/<job_id>` | Poll status (see fields below). |
| GET | `/api/projects/<id>/exports/<job_id>/file` | Download the produced ZIP once `status == "done"`. **409** while running, **410** if the file has been garbage-collected. |
| GET | `/api/projects/<id>/export.zip` | Legacy synchronous endpoint (still works, no progress). |

Job dict (public fields):

| Field | Meaning |
|-------|---------|
| `status` | `queued` / `running` / `done` / `error`. |
| `stage` | `queued` / `rendering_mp4s` / `zipping` / `ready` / `blocked` / `failed`. |
| `percent` | 0–100, derived from `current / total` where `total = num_scenes + 1` (one step per MP4 + one final zip-write step). |
| `current`, `total` | Numeric progress. |
| `message` | Human-friendly stage text like `"Rendering MP4 12/30 — slot 012 (4.0s)"`. |
| `file_name`, `size_bytes` | Filled in when ready. |
| `duplicate_slots`, `error` | Populated on blocked/failed jobs. |

The on-disk ZIP lives in `tempdir/tatterveil_exports/<job_id>.zip`. `_gc_old_export_jobs()` runs whenever a new job is created and discards finished jobs (and their zip files) older than 30 minutes.

The UI opens a modal with a progress bar that polls `/exports/<job_id>` every 700 ms, then issues a normal browser download once the job reports `status="done"`.

ZIP contents are unchanged from the legacy endpoint:

- `scene_NNN.mp4` per slot, in increasing slot order, with `duration = scene.duration` (or `end_time − start_time`).
- `scene_timestamps.txt` — tab-separated manifest (filename, slot, start, end, duration).
- `project_export_metadata.json` — raw `meta`, `timing`, full `scenes` array, `exported_at`.

**Requirement:** `ffmpeg` on `PATH`. The export job fails fast with stage `failed` and a clear error message when missing.

---

## 10. HTTP API summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/estimate` | Scene-count estimate + cost (`{ ...plan, cost: {...} }`). |
| GET | `/api/pricing` | Raw pricing table for UI consumers. |
| POST | `/api/generate` | New project + start `_run_generation` thread. |
| GET | `/api/projects/<id>/status` | Poll: `step`, `progress`, `scenes[]`, `duplicate_slots`, `export_blocked`, `regeneration_jobs[]`, `regeneration{busy,active_count,max_parallel}`, `cost_estimate`, `cost_actual`. |
| POST | `/api/projects/<id>/exports` | Start a progress-aware ZIP export job. |
| GET | `/api/projects/<id>/exports/<job_id>` | Poll job status / percent. |
| GET | `/api/projects/<id>/exports/<job_id>/file` | Download finished ZIP. |
| GET | `/api/projects/<id>/export.zip` | Legacy synchronous ZIP download (no progress). |
| POST | `/api/projects/<id>/scenes/<entry_id>/regenerate` | Enqueue a regeneration job (queue, 4 in parallel). |
| GET | `/api/projects/<id>/regenerations` | List active + recently-finished regeneration jobs. |
| DELETE | `/api/projects/<id>/regenerations/<job_id>` | Dismiss a finished regeneration job. |
| DELETE | `/api/projects/<id>/scenes/<entry_id>` | Remove one variant row + image. |
| DELETE | `/api/projects/<id>` | Delete entire project directory. |

---

## 11. Frontend behaviour (`static/js/app.js`)

- **Polling** — Recursive `pollStatus()` with adaptive delay: faster while generating or while a regen job is in flight; slower when fully idle.
- **`syncSceneGrid(scenes)`** — Reconciles DOM with the polled `scenes[]` keyed by `entry_id`. Newly-appended regen variants appear immediately as pending cards with a spinner.
- **Lightbox** — Clicking any completed image (or its **Enlarge** button) opens a full-screen overlay. Close via the `×` button, Escape, or clicking the backdrop. Dynamically-added cards from `buildSceneCard()` get the same affordance.
- **Regeneration queue card** — Above the grid, lists every in-flight + recently-finished job with state label (`Composing new prompt…` / `Generating new image…` / `Done` / error). An overlay badge on the new pending card mirrors the state. Finished jobs auto-disappear after 60s server-side.
- **Cost preview + confirmation** — Live cost rendered in the estimate panel; the Generate button opens a confirmation modal showing resolution / quality / scenes / per-image / images subtotal / prompt overhead / total before any work starts. The project page settings card shows `~$X.XXXX` estimate, then `$X.XXXX` final once generation completes.
- **Export progress modal** — Replaces the legacy direct download. Bar + percent + stage message; auto-triggers the browser download when the job reports `status="done"`.
- **Read more** — Uses `.seg-trunc` / `.seg-full` / `.btn-expand-text` with delegated clicks (no inline `onclick` with huge escaped strings).
- **Initial SSR** — `initProjectPage()` seeds progress bar width and hides the progress strip when `INIT_STEP` is `done` or `error`.

---

## 12. Concurrency notes

- `_scene_lock(project_id)` (lazy per-project `threading.Lock`) wraps every read-modify-write on `scenes.json` performed by regeneration workers so concurrent jobs cannot race on `next_variant_index()` or clobber each other's writes.
- The regeneration `ThreadPoolExecutor` is process-global; it's reused across requests via a guarded `_get_regen_executor()` (lazy init under `_regen_executor_lock`).
- Initial-generation image renders still go through `engine.pipeline.generate_all_images()`'s own `ThreadPoolExecutor(MAX_WORKERS)`. The two pools are independent.

---

## 13. Configuration & secrets

- `OPENAI_API_KEY` required.
- `OPENAI_TEXT_MODEL`, `IMAGE_MODEL` override defaults (`config.py`).
- `REGEN_PARALLELISM` (default `4`) — max concurrent regeneration image renders.
- `IMAGE_COSTS` and `PROMPT_GENERATION_FLAT_COST` — pricing inputs for the cost preview.
- `.env` is loaded from this package directory or sibling `image_generator/.env`.

---

## 14. Operational limits & failure modes

- Partial image failures leave `image_status: error`; ZIP export aborts until fixed or rows removed manually.
- Export job missing ffmpeg → job ends with `status="error"`, `stage="failed"`, and a clear error message that the UI shows in the progress modal.
- Regeneration jobs that fail mid-flight leave the new variant row with `image_status="error"` and the job in `state="error"`; the user can delete the failed variant from the card.
- Export ZIPs live in the system temp directory and are auto-cleaned 30 min after the job finishes.

This should be enough for a new engineer to trace any request from the browser → JSON stores → worker threads → OpenAI APIs → filesystem.
