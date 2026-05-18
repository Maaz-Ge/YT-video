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

## 6. Regeneration

**Route:** `POST /api/projects/<id>/scenes/<entry_id>/regenerate`  
**Body:** `{ "instructions": "..." }`

1. Validates instructions and rejects concurrent regenerations (`regeneration.busy` in status).
2. Background thread `_run_regenerate_worker()`:
   - Loads parent row via `find_entry()`.
   - `next_variant_index()` picks the next `variant_index` for that slot.
   - `refine_prompt_for_regeneration()` — second LLM call with `REFINE_PROMPT_SYSTEM`; merges **previous prompt + negatives + script segment + user instructions** into a new JSON `{ prompt, negative_prompt }`.
   - Appends a **new** row (old row untouched).
   - Calls `generate_image()` for the new row only.

The UI polls faster while `regeneration.busy` is true.

---

## 7. Deleting a variant

**Route:** `DELETE /api/projects/<id>/scenes/<entry_id>`

Removes the row from `scenes.json` and deletes the PNG if present. Deletes do not shift `slot_number`s (timeline stays stable).

---

## 8. ZIP export

**Route:** `GET /api/projects/<id>/export.zip`

Preconditions:

- **No duplicate slots** — otherwise HTTP **409** with `duplicate_slots`.
- Each remaining row must have `image_status == done` — otherwise HTTP **503** with an explanatory message.

Process (`_build_export_zip`):

1. Sort rows by `(slot_number, variant_index)` — with unique slots this is chronological slot order.
2. For each row, derive duration: `duration` or `end_time - start_time`.
3. **ffmpeg** still image → H.264 MP4: one file per slot named `scene_NNN.mp4`.
4. **Zip contents:**
   - `scene_001.mp4`, …
   - `scene_timestamps.txt` — tab-separated manifest (filename, slot, start, end, duration).
   - `project_export_metadata.json` — raw `meta`, `timing`, full `scenes` array, `exported_at` timestamp.

**Requirement:** `ffmpeg` on `PATH` (documented in README).

---

## 9. HTTP API summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/estimate` | Live scene count estimate. |
| POST | `/api/generate` | New project + start `_run_generation` thread. |
| GET | `/api/projects/<id>/status` | Poll: `step`, `progress`, `scenes[]`, `duplicate_slots`, `export_blocked`, optional `regeneration`. |
| GET | `/api/projects/<id>/export.zip` | ZIP download (blocked on duplicates). |
| POST | `/api/projects/<id>/scenes/<entry_id>/regenerate` | Start background regen (new variant). |
| DELETE | `/api/projects/<id>/scenes/<entry_id>` | Remove one variant row + image. |
| DELETE | `/api/projects/<id>` | Delete entire project directory. |

---

## 10. Frontend behaviour (`static/js/app.js`)

- **Polling** — Recursive `pollStatus()` with adaptive delay: faster during active steps or regeneration busy; slower when idle but duplicates may still resolve.
- **`syncSceneGrid(scenes)`** — If the API returns a non-empty `scenes` array, reconcile DOM:
  - Remove cards whose `data-entry-id` is absent from the payload.
  - Create missing cards with `buildSceneCard()`.
  - Update images/errors on existing cards (`updateSceneCardMedia()`).
- **Read more** — Uses `.seg-trunc` / `.seg-full` / `.btn-expand-text` and delegated clicks (no broken inline `onclick` with huge escaped strings).
- **Export** — `fetch` ZIP as blob; handles 409 duplicate errors with toast.
- **Initial SSR** — `initProjectPage()` seeds progress bar width and hides the progress strip when `INIT_STEP` is `done` or `error`.

---

## 11. Configuration & secrets

- `OPENAI_API_KEY` required.
- `OPENAI_TEXT_MODEL`, `IMAGE_MODEL` override defaults (`config.py`).
- `.env` is loaded from this package directory or sibling `image_generator/.env`.

---

## 12. Operational limits & failure modes

- Partial image failures leave `image_status: error`; export aborts until fixed or rows removed manually (future UX could prune).
- Concurrent regeneration returns **429**.
- ffmpeg missing → **503** on export.

This should be enough for a new engineer to trace any request from the browser → JSON stores → worker threads → OpenAI APIs → filesystem.
