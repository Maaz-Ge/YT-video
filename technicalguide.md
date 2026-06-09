# Tatterveil Scene Studio — Technical Guide

This document explains how each major subsystem fits together end-to-end: data model, backend workers, APIs, frontend polling, regeneration, exports, and external dependencies.

---

## 1. Repository layout

| Area | Role |
|------|------|
| `app.py` | Flask app: routes, persisted `status.json`, scene CRUD, ZIP export, regenerate worker orchestration, voice-over stage. |
| `config.py` | Environment variables, paths (`PROJECTS_DIR`), model names, quality/resolution presets, ElevenLabs settings. |
| `engine/pipeline.py` | Scene plan (from the measured audio length) → **sentence-aligned logical scene grouping** (LLM, with a deterministic time-balanced fallback) → LLM enrichment/prompt → parallel image generation → prompt refinement for regeneration. Still falls back to the legacy equal-word split when no voice-over/STT is available. |
| `engine/voice.py` | ElevenLabs TTS: chunk the script under the 10k-char cap, render each chunk with a fixed seed + neighbour context, concatenate into **one combined WAV**, measure its duration. |
| `engine/transcribe.py` | Speech-to-text sentence timeline: slice the WAV under the 25 MB STT limit, get **whisper-1 word timestamps**, align the **original** script sentences to the audio, and emit `sentence_timeline.json` (timestamps from the voice, text from the script). |
| `engine/style_guide.py` | Tatterveil rules and the **system** prompt for scene enrichment + image prompt generation. |
| `engine/scene_utils.py` | Stable `entry_id`s, image filenames (`scene_001.png`), sorting, duplicate-slot detection. |
| `templates/` | Jinja HTML; `project.html` SSR scene cards + modal + a single combined-voice-over `<audio>` player. |
| `static/js/app.js` | Form handling, polling, grid sync, regenerate modal, export download, combined voice-over player sync. |
| `projects/<id>/` | Per-project folder: `meta.json`, `status.json`, `scenes.json`, `timing.json`, `images/`, `voiceovers/`. |

---

## 2. Project folder on disk

```
projects/<project_id>/
  meta.json           # User-facing settings: title, script, ..., scene_plan, voiceover{path,duration_seconds,chunks,...}
  status.json         # Live step, progress, messages, regeneration flag, voice_total/voice_done (chunk counts)
  scenes.json         # Array of **scene rows** (see §3)
  sentence_timeline.json  # Per-sentence start/end from STT (text = original script); drives scene grouping
  timing.json         # Written when generation finishes: step timings + image_summary + voiceover info
  images/             # PNG outputs (one per scene variant)
  voiceovers/         # full_voiceover.wav — ONE combined narration for the whole script
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

Voice-over is **not** a per-scene field. There is one combined narration for the
whole video stored in `meta["voiceover"]`:

```jsonc
"voiceover": {
  "status": "done",                       // done | error | skipped
  "path": "voiceovers/full_voiceover.wav",
  "filename": "full_voiceover.wav",
  "duration_seconds": 510.3,              // measured audio length → drives the scene plan
  "chunks": 3,                            // number of ElevenLabs requests stitched together
  "voice_id": "...", "model_id": "...", "output_format": "wav_44100"
}
```

### Filenames

- Slot `N`, variant `0`: `scene_NNN.png`
- Slot `N`, variant `≥1`: `scene_NNN_vV.png`
- Combined narration for the whole video: `voiceovers/full_voiceover.wav`

`engine/scene_utils.ensure_scene_entries()` normalizes older projects: assigns missing `entry_id`, aligns `slot_number`, and derives `image_filename` from `image_path` when needed. `_scenes_live()` in `app.py` persists those migrations once.

### Duplicate slots

A **duplicate** means: more than one row shares the same `slot_number`. That is intentional after **Regenerate**, which **appends** a new row instead of replacing the old PNG.

`duplicate_slot_numbers()` returns sorted slot indices; the UI and `export_blocked` flag use this to block ZIP export until the user deletes extras.

---

## 4. Generation pipeline (initial project)

Implemented in `_run_generation()` + `engine/pipeline.py` + `engine/voice.py`.
The voice-over is generated **first** because the measured audio length is the
real video duration that drives scene splitting.

1. **Analyse** — light pass over the script.
2. **Voice-over (whole script)** — `voice_engine.generate_full_voiceover()`:
   - `chunk_script()` splits the script into ordered pieces ≤ `VOICE_MAX_CHARS` (default 9000, under ElevenLabs' 10k hard cap), breaking on paragraphs → sentences → words.
   - Each chunk is rendered sequentially with the **same** `voice_id` / `model_id` / `voice_settings` (speed **1.0**), a **fixed `ELEVEN_SEED`**, and `previous_text` / `next_text` context so the timbre and prosody stay identical across chunk boundaries.
   - Chunks are concatenated with the stdlib `wave` module into `voiceovers/full_voiceover.wav`, and its duration is measured from the WAV header (no ffmpeg/ffprobe needed for audio).
   - The result is stored in `meta["voiceover"]`. If `ELEVEN_API_KEY` is empty or the call fails, the step is skipped/errored and duration falls back to `estimate_duration(script)` so images still generate.
3. **Sentence timeline (STT)** — `transcribe.build_sentence_timeline(script, wav)`:
   - `split_script_sentences()` breaks the **original** script into sentences (verbatim text).
   - `transcribe_words()` slices the WAV into pieces under `STT_MAX_UPLOAD_BYTES` (stdlib `wave`, no ffmpeg), calls **`whisper-1`** with `response_format="verbose_json"` + `timestamp_granularities=["word"]`, and offsets each slice's word timestamps back onto the full timeline.
   - `align_sentences()` matches the script's words to the STT words (`difflib.SequenceMatcher`) and reads each sentence's start/end from the audio. Boundaries are contiguous and the **last sentence ends exactly at the measured audio length**. The saved text is always the original script — STT is used only for timing, so hallucinations never leak in.
   - Result is written to `sentence_timeline.json` and summarised in `meta["sentence_timeline"]`. On any failure the step is skipped and the pipeline falls back to the equal-word split.
4. **Scene plan** — `compute_scene_plan(duration_from_audio, first_rate, rest_rate)` applies dual rates (first 5 min vs rest) as the **target** scene counts.
5. **Logical scene grouping (per-minute budget)** — `build_scene_segments_from_sentences()` groups **whole sentences** into scenes (never splitting a sentence) using the per-sentence timestamps:
   - `_build_minute_plan()` buckets sentences into **one-minute windows by each sentence's midpoint** (`_minute_index`), giving natural boundary tolerance: a sentence that mostly plays before a boundary stays in the earlier minute. Each minute gets a `target_scenes` = the selected rate (`first_rate` for minutes `< FIRST_SEGMENT`, else `rest_rate`), **capped at that minute's sentence count** (you can't make more scenes than sentences without splitting one).
   - An LLM (`SCENE_GROUPING_SYSTEM`) splits each minute's sentences into exactly its `target_scenes`. Results are validated **per minute** (`_minute_groups_valid`: exact count + all that minute's sentences in order); any minute that fails falls back to a deterministic **time-balanced** partition (`_partition_by_time`). This guarantees the requested per-minute scene counts (fixes the earlier under-count where 3/min over 5 min produced far fewer than 15 scenes).
   - Each scene's `start_time`/`end_time` comes directly from its sentences, so timings match the narration. The real scene count then updates `scene_plan` (`first/rest_segment_scenes`, `total_scenes`) and `cost_estimate`.
   - When no STT timeline exists, `split_and_prompt()` falls back to `compute_equal_segments()` (legacy equal-word split).
6. **Enrich + prompt** — `split_and_prompt(pre_segments=...)` passes the locked segments to the LLM, which adds `scene_type`, `time_period`, `prompt`, `negative_prompt`, etc. Locked timing/`script_segment`/`sentence_indices` fields are always honoured when merging.
7. **Images** — `generate_all_images()` uses a `ThreadPoolExecutor` (`MAX_WORKERS`). Each future is keyed by **`entry_id`**, not `scene_number`, so duplicate slots would not overwrite each other.

Progress (`status.json`): analyse ~3-5%, voice-over 8-36% (with `voice_done` / `voice_total` = chunks rendered), transcription ~37%, prompts 44-48%, images 48-100%.

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
| `percent` | 0–100, derived from `current / total` where `total = num_scenes + 1` (one step per MP4 + the final zip-write step that also bundles the combined audio). |
| `current`, `total` | Numeric progress. |
| `message` | Human-friendly stage text like `"Rendering MP4 12/30 — slot 012 (4.0s)"`. |
| `file_name`, `size_bytes` | Filled in when ready. |
| `duplicate_slots`, `error` | Populated on blocked/failed jobs. |

The on-disk ZIP lives in `tempdir/tatterveil_exports/<job_id>.zip`. `_gc_old_export_jobs()` runs whenever a new job is created and discards finished jobs (and their zip files) older than 30 minutes.

The UI opens a modal with a progress bar that polls `/exports/<job_id>` every 700 ms, then issues a normal browser download once the job reports `status="done"`.

ZIP contents:

- `scene_NNN.mp4` per slot, in increasing slot order, with `duration = scene.duration` (or `end_time − start_time`).
- `voiceovers/full_voiceover.wav` — the single combined narration for the whole video (present when `meta.voiceover.status == "done"`).
- `scene_timestamps.txt` — tab-separated manifest (filename, slot, start, end, duration) with a header line referencing the combined audio file.
- `project_export_metadata.json` — raw `meta` (including `voiceover`), `timing`, full `scenes` array, `exported_at`.

**Requirement:** `ffmpeg` on `PATH`. The export job fails fast with stage `failed` and a clear error message when missing.

---

## 10. HTTP API summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/estimate` | Scene-count estimate + cost (`{ ...plan, cost: {...} }`). |
| GET | `/api/pricing` | Raw pricing table for UI consumers. |
| POST | `/api/generate` | New project + start `_run_generation` thread. |
| GET | `/api/projects/<id>/status` | Poll: `step`, `progress`, `scenes[]`, `duplicate_slots`, `export_blocked`, `regeneration_jobs[]`, `regeneration{...}`, `cost_estimate`, `cost_actual`, `voiceover{status,url,duration_seconds,chunks}`. |
| GET | `/projects/<id>/voiceovers/<filename>` | Stream the combined voice-over audio (`full_voiceover.wav`). |
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
- **Combined voice-over player** — `updateProjectVoiceover(data.voiceover)` shows a single `<audio>` player (the `#voiceover-section`) above the scene grid once the narration is ready; scene cards themselves no longer carry audio.
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
- `ELEVEN_API_KEY` optional — when set, the whole script is narrated via ElevenLabs `eleven_multilingual_v2`, voice `VuLPiW02W0Qm8465ksBZ`, with `stability=0.26`, `similarity_boost=0.33`, `style=0.07`, `use_speaker_boost=True`, `speed=1.0`. All overridable via `ELEVEN_VOICE_ID`, `ELEVEN_MODEL_ID`, `ELEVEN_STABILITY`, `ELEVEN_SIMILARITY_BOOST`, `ELEVEN_STYLE`, `ELEVEN_SPEED`.
- `ELEVEN_SEED` (default `12345`) — fixed seed so every chunk of the combined audio shares the same timbre.
- `ELEVEN_OUTPUT_FORMAT` (default `wav_44100`) — must be a `wav_*` format; the chunks are concatenated with the stdlib `wave` module.
- `VOICE_MAX_CHARS` (default `9000`) — max characters per ElevenLabs request (hard cap is 10,000). The script is chunked below this and stitched together.
- `OPENAI_TEXT_MODEL`, `IMAGE_MODEL` override defaults (`config.py`).
- `REGEN_PARALLELISM` (default `4`) — max concurrent regeneration image renders.
- `WORDS_PER_MINUTE` (default `150`) — **preview only**. The real duration comes from the measured voice-over; this constant just powers the pre-generation scene-count + cost estimate.
- `IMAGE_COSTS` and `PROMPT_GENERATION_FLAT_COST` — pricing inputs for the cost preview (voice-over cost is governed by your ElevenLabs plan and is not included in this estimate).
- `.env` is loaded from this package directory or sibling `image_generator/.env`.

---

## 14. Operational limits & failure modes

- Partial image failures leave `image_status: error`; ZIP export aborts until fixed or rows removed manually.
- If voice generation fails (or `ELEVEN_API_KEY` is unset), `meta.voiceover.status` becomes `error`/`skipped` and the pipeline **falls back** to `estimate_duration(script)` for the scene plan, so images still generate. The export simply omits the audio file in that case.
- A single ElevenLabs chunk failing all retries fails the whole voice-over (the script must read end-to-end); the project continues with the estimated duration.
- Export job missing ffmpeg → job ends with `status="error"`, `stage="failed"`. ffmpeg is required for the MP4 chunks; the combined audio is already a WAV and is bundled as-is (no transcode).
- Regeneration jobs that fail mid-flight leave the new variant row with `image_status="error"`; the user can delete the failed variant. The combined voice-over is project-level, so regenerating an image never touches the audio.
- Export ZIPs live in the system temp directory and are auto-cleaned 30 min after the job finishes.

This should be enough for a new engineer to trace any request from the browser → JSON stores → worker threads → OpenAI APIs → filesystem.
