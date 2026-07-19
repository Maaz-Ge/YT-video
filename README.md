# Tatterveil Scene Studio

A purpose-built SaaS platform that converts Tatterveil YouTube scripts into
atmospheric, photorealistic scene images **and per-scene voice-overs**. Paste a
script → receive a full set of timestamped, AI-generated visuals **plus a
spoken narration clip per scene** that faithfully follows the Tatterveil
Visual Style Guide.

---

## Quick Start

### Local development (Python)

```bash
cd YT-video
pip install -r requirements.txt
python app.py          # starts on http://localhost:5001
```

**Exports:** install [FFmpeg](https://ffmpeg.org/) on your PATH (used to turn each PNG into an MP4 of the scene duration).

### Production (Docker — recommended on a server)

```bash
cd YT-video
cp .env.example .env    # add OPENAI_API_KEY
docker compose build
docker compose up -d    # http://localhost:5001
```

ffmpeg and Python deps are included in the image; `projects/` is persisted via a volume mount.  
**Full step-by-step server migration** (from tmux + venv): see **[DOCKER-DEPLOY.md](DOCKER-DEPLOY.md)**.

Requires an `.env` file in this folder (or in `../image_generator/`):

```
OPENAI_API_KEY=sk-...
ELEVEN_API_KEY=<your-elevenlabs-key>    # optional — leave blank to skip voice-overs
```

When `ELEVEN_API_KEY` is set, the whole script is narrated as **one combined WAV** (`full_voiceover.wav`) using
ElevenLabs **`eleven_v3`** with the **`/with-timestamps`** API. Sentence-level timestamps come directly from
ElevenLabs character alignment — no Whisper STT. The measured audio length drives scene splitting.
Voice settings: `stability=0.75`, `similarity_boost=0.85`, `style=0.35`; narration **speed** is user-selected (0.25–1.0).
ffmpeg is required to merge MP3 chunks and convert to WAV.

---

## Architecture

```
YT-video/
├── app.py               Flask web application
├── config.py            All runtime constants and env-var defaults
├── technicalguide.md    Deep-dive: data model, APIs, workers, export pipeline
├── requirements.txt     Python dependencies
├── .env                 API keys (not committed)
├── projects/            Generated project data (gitignored)
│
├── engine/
│   ├── scene_utils.py   Stable `entry_id`s, PNG/MP3 filenames, duplicate-slot detection
│   ├── style_guide.py   Full Tatterveil style guide encoded as Python constants
│   │                    + the LLM system prompt used for scene splitting
│   ├── freeform.py      Style-free batch prompts + reference-image style extract / refine
│   ├── pipeline.py      Core generation engine (scene grouping + prompts + images + safety retry)
│   └── voice.py         ElevenLabs /with-timestamps → combined WAV + sentence timeline
│
├── templates/
│   ├── layout.html      Base template (nav, toast notifications)
│   ├── index.html       Landing page + project creation form
│   └── project.html     Project view: progress tracking + scene grid
│
└── static/
    ├── css/style.css    Dark atmospheric UI design system
    └── js/app.js        Form logic, live estimate, polling, scene injection
```

---

## AI Models

| Role | Model |
|---|---|
| Script analysis + prompt generation | `gpt-5.4-mini` |
| Image generation | `gpt-image-2` |
| Voice-over (TTS, one combined file) | ElevenLabs `eleven_multilingual_v2` (speed 1.0) |

Both models are configurable via environment variables:

```
OPENAI_TEXT_MODEL=gpt-5.4-mini
IMAGE_MODEL=gpt-image-2
```

---

## Generation Pipeline (3 Steps)

### Step 1 — Script Analysis

File: `engine/pipeline.py` → `estimate_duration()` + `compute_scene_plan()`

```
script word count ÷ 150 wpm = estimated video duration (minutes)
```

**Two-rate scene count calculation:**

```
first_scenes = min(duration, 5 min) × first_rate  (scenes/min)
rest_scenes  = max(0, duration − 5 min) × rest_rate (scenes/min)
total_scenes = first_scenes + rest_scenes
```

The user sets both rates independently on the creation form. The live estimate
panel updates in real time as you type and adjust the sliders.

**Example:**
- Script: 1 200 words → ~8 minutes
- First rate: 3 scenes/min, Rest rate: 2 scenes/min
- First segment: 5 min × 3 = 15 scenes
- Remaining: 3 min × 2 = 6 scenes
- **Total: 21 scenes**

---

### Step 2 — Scene Splitting + Prompt Generation (single LLM call)

File: `engine/pipeline.py` → `split_and_prompt()`  
System prompt source: `engine/style_guide.py` → `SCENE_SPLIT_SYSTEM_PROMPT`

One call to **`gpt-5.4-mini`** does all of the following simultaneously:

#### 2a. Script splitting with timestamps

The LLM splits the full script into exactly N scenes. Each scene receives:

| Field | Description |
|---|---|
| `scene_number` | 1-indexed integer |
| `start_time` | seconds from video start |
| `end_time` | seconds |
| `duration` | seconds |
| `script_segment` | verbatim portion of script |

Timing is distributed proportionally to script content density.

#### 2b. Scene type classification (1 of 5 types)

| Type | Name | Used when |
|---|---|---|
| 1 | Artifact Close-up | Script focuses on a specific object or mechanism |
| 2 | Environmental Wide Shot | Script describes a location or landscape |
| 3 | Discovery Scene | Script describes finding or revealing something |
| 4 | Abstract / Conceptual | Script describes ideas, mysteries, unknowns |
| 5 | Period Context | Script references specific historical human activity |

Each type drives a different prompting approach:
- **Type 1:** Extreme close-up, surface texture, oxidation, single directional light
- **Type 2:** Wide atmospheric shot, deep depth layers, mist, low horizon
- **Type 3:** Object partially emerging, period-accurate context, torch from one side
- **Type 4:** Physical symbolic imagery from the Abstraction Solutions Library
- **Type 5:** Period-accurate environment, era-appropriate materials

#### 2c. Time period detection

The LLM reads the script segment and applies the correct visual era:

| Script signal | Visual context |
|---|---|
| "ancient" / "BCE" / "X years ago" | No humans, pure artifact/environment |
| "1901" / "early 20th century" | Sepia tones, gas lamps, period equipment |
| "Victorian" / "1800s" | Victorian-era materials, warm lamp light |
| "Ice Age" / "34,000 years ago" | Cold blue-grey tundra, frozen landscape |
| "underwater" / "shipwreck" | Murky blue-green, particles, depth |
| "underground" / "cavern" | Torch glow, carved stone, deep shadow |
| *(none)* | Default ancient world context |

**Critical rule:** When a discovery is mentioned, show the artifact or location — not the people discovering it.

#### 2d. Abstraction mode

Triggered automatically by phrases such as:
- "we still don't know" / "remains a mystery" / "nobody knows why"
- "the knowledge vanished" / "disappeared entirely"
- "hidden for thousands of years" / "what lies beneath"
- "more sophisticated than we assumed"

When triggered, the LLM selects a **physical symbolic scene** from the
Abstraction Solutions Library instead of attempting literal depiction:

| Concept | Visual solution |
|---|---|
| Lost knowledge | Ancient manuscript half-buried in dark soil, candlelight fading |
| Mystery | Stone corridor, torch-lit, sealed door slightly ajar |
| Civilization collapse | Overgrown throne room, nature reclaiming stone |
| Time passing | Extreme close-up of weathered stone with moss in cracks |
| Hidden / buried | Excavation pit at dusk, something partially revealed |
| Scale of achievement | Tiny human silhouette (back turned) vs enormous structure |
| Unanswered questions | Stone wall of undeciphered carvings, single torch |
| Discovery moment | First-person POV torch illuminating something from darkness |
| Deliberate concealment | Heavy stone blocks in dark underground space |
| Time compression | Layered geological strata in cliff face |

#### 2e. Prompt construction

Each scene gets a minimum 80-word prompt that always includes:
- `"atmospheric photorealistic"` — style anchor
- `"documentary photography style"` — quality benchmark
- `"16:9 landscape"` — composition lock
- Detected time period injected explicitly
- Atmospheric elements: mist, deep shadow, crushed blacks, ambient light
- Scene-type-specific composition guidance
- Universal negative constraints folded inline as avoidance language

---

### Step 3 — Image Generation

File: `engine/pipeline.py` → `generate_all_images()` + `generate_image()`

All scenes are rendered in parallel (3 workers by default).

#### gpt-image-2 API call

```python
response = client.images.generate(
    model  = "gpt-image-2",
    prompt = full_prompt,       # positive + negative constraints combined
    n      = 1,
    size   = "2048x1152",       # always — true 16:9 at full resolution
    quality= "low|medium|high", # set by user on creation form
)
image_bytes = base64.b64decode(response.data[0].b64_json)
```

Output is saved as **PNG** (`scene_001.png`, `scene_002.png`, …).

#### Resolution

All images are always `2048 × 1152` pixels — true 16:9 at full gpt-image-2 resolution.

#### Quality options

| UI Label | API value | Notes |
|---|---|---|
| Low | `"low"` | Fastest, lowest API cost |
| Medium | `"medium"` | Balanced (default) |
| High | `"high"` | Best quality, highest cost |

#### Retry logic

Each image gets up to 3 attempts with exponential back-off (2.5 s → 5 s → 7.5 s).  
If all attempts fail the scene is marked `error` and generation continues for remaining scenes.

---

## Regenerate & duplicate timeline slots

- Click **Regenerate** on any completed scene card, type your change instructions and **Add to queue**. The LLM rewrites the prompt (preserving the Tatterveil style) and a new image is rendered as a **new variant row** with a fresh filename (`scene_002_v1.png`, …). Older variants are kept until you delete them.
- **Up to 4 image renders run in parallel** (see `REGEN_PARALLELISM`). You can queue as many regenerations as you like — the rest wait their turn. Prompt-rewrite LLM calls run in parallel across all 4 workers.
- A **Regeneration queue** card above the scene grid shows each job's state — `Composing new prompt…` → `Generating new image…` → `Done`. A spinner badge also overlays the new variant card while its image is being rendered.
- Rows are keyed by **`entry_id`**. Duplicate **slots** (same timeline index) disable **ZIP export** until you delete extra variants.

---

## Single Image Studio (standalone)

A separate, self-contained generator for one-off images that has **nothing to do
with the Tatterveil batch pipeline** — no script, no voice-over, no scene timeline.

On the Studio landing page a two-tab switch lets you pick **Batch Generation**
(the default script → scenes flow) or **Single Image**.

In the Single Image tab you provide:

- **Prompt** — sent to `gpt-image-2` **verbatim**. No Tatterveil style, no negative
  constraints, and no content-safety rewrite are added. You get exactly what you ask for.
- **Resolution** and **Quality** — the same options as batch generation.
- **Aspect ratio** is fixed at **16:9** (enforced by the resolution presets).

Behaviour:

- Generate as many images as you like. **Up to 3 render in parallel**
  (`SINGLE_IMAGE_PARALLELISM`, default 3) via a shared thread pool; the rest queue
  automatically — the same queue model as regeneration.
- Each image appears as a card with the **image**, its **prompt** (in a
  `View image prompt` dropdown), and **Download** / **Delete** buttons. Click an
  image to open it full-screen in the lightbox.
- If a prompt is blocked by OpenAI's content policy, that card shows an **error**
  (the prompt is *not* auto-rewritten) — edit your wording and generate again.
- Images **persist across restarts** (stored in `singles/singles.json` + `singles/images/`)
  until you delete them.

Single images live in their own `singles/` directory, completely separate from
`projects/`, so they never appear in the project list or affect the batch
"only one project generating at a time" lock.

---

## Freeform Batch (style-free scene pipeline)

A fourth studio mode — same end-to-end batch pipeline as Tatterveil
(**script → voice + timestamps → scene split → prompts → parallel images →
regenerate / ZIP export**), but **without** the hardcoded Tatterveil style guide.

On the Studio page choose **Freeform Batch**. Provide:

- **Script**, narration speed, scene rates, resolution, quality — same as Tatterveil batch
- **Special instructions** *(optional)* — creative direction for prompt writing across
  all scenes (camera angles, mix of close-ups / wides, mood, etc.)
- **Reference image** *(optional)* — uploaded still; GPT extracts a visual style brief
  that is applied consistently to every scene prompt

Prompt behaviour:

| Special instructions | Reference image | Resulting image prompt |
|----------------------|-----------------|------------------------|
| empty | none | Exactly that scene’s `script_segment` (image model interprets freely) |
| set | optional | LLM builds prompts from script + instructions (+ style brief if present) |
| optional | set | Style brief extracted first, then prompts stay consistent with it |

Voice chunking/concat, scene timing, image workers, regeneration queue, and ZIP export
are shared with Tatterveil batch. Freeform projects store `pipeline_type: "freeform"`
in `meta.json` and live in the same `projects/` folder (already Docker-persisted).

Create via `POST /api/generate-freeform` (JSON or multipart with `reference_image`).

---

## Single Voice Studio (standalone)

A separate generator for one-off **voice-overs only** — no images, no scenes, no
timeline. Selectable from the same Studio tab switch as **Batch Generation** and
**Single Image**.

In the Single Voice tab you provide:

- **Script** — the full narration to speak.
- **Narration speed** — the same 0.25–1.0 slider used by batch generation.

The voice engine, ElevenLabs settings, and chunk-and-stitch logic are **identical
to the batch pipeline** — it calls the same `engine.voice.generate_voice_with_timestamps()`.
Long scripts are split into sentence-aligned chunks, each sent to ElevenLabs
`/with-timestamps`, then concatenated with ffmpeg into **one combined WAV**
(`full_voiceover.wav`).

Behaviour:

- Generate as many clips as you like. **Up to `SINGLE_VOICE_PARALLELISM` (default 2)**
  render in parallel via a shared thread pool; the rest queue automatically.
- Each clip appears as a card with an inline **audio player**, a `View script`
  dropdown, duration / chunk / speed tags, and **Download** / **Delete** buttons.
  While rendering, the card shows live `Chunk X of Y…` progress.
- Requires `ELEVEN_API_KEY`; without it the request is rejected. If a render
  fails, the card shows the error and can be deleted.
- Clips **persist across restarts** (each stored in `voices/<id>/`) until deleted.

Like single images, voice-overs live outside `projects/` and never affect the
batch project list or generation lock. **ffmpeg is required** (same as batch voice).

---

## Cost preview & confirmation

The system uses the OpenAI image price table:

| Resolution | Low | Medium | High |
|------------|-----|--------|------|
| 1280×720   | $0.003 | $0.028 | $0.114 |
| 2048×1152  | $0.005 | $0.042 | $0.170 |
| 3840×2160  | $0.011 | $0.100 | $0.400 |

Total cost = `(scene count × per-image price) + $1` (flat overhead for the scene-splitter + prompt-writer LLM calls).

- The **live estimate panel** updates the cost as you tweak the script, resolution, quality, and timing sliders.
- Clicking **Generate Scenes** opens a **cost-confirmation modal** — generation only runs after you click **Confirm & Generate**.
- After generation finishes, the **project settings card** shows `$X.XXXX` (final), counting only images that succeeded. While generating, it shows the `~$X.XXXX` estimate based on the planned scene count.

---

## ZIP export with progress (`ffmpeg`)

When every slot has exactly one row and all those rows are successful:

1. **`scene_timestamps.txt`** — lists each MP4 filename with slot, start, end, and duration (seconds).
2. **`scene_001.mp4`, `scene_002.mp4`, …** — ascending slot order; each clip is exactly the scene's duration (`duration` field, or `end_time − start_time`).
3. **`project_export_metadata.json`** — project `meta`, `timing`, full `scenes` array, export timestamp.

The **Download ZIP** button now opens a **progress modal** that polls the backend job (`/api/projects/<id>/exports/<job_id>`) showing "Rendering MP4 X/Y — slot ZZZ", then "Packing ZIP archive…", with a live percent bar. The browser download triggers automatically when the archive is ready.

Requires **ffmpeg** on `PATH`. See **`technicalguide.md`** for internals.

---

## Image lightbox

Click any completed scene image (or its **Enlarge** button) to open a full-screen lightbox. Close via the `×` button, the `Esc` key, or by clicking the dark backdrop.

---

## Style Guide Encoding

`engine/style_guide.py` encodes the full Tatterveil Visual Style Guide as:

- `UNIVERSAL_NEGATIVE` — negative prompt terms applied to every scene
- `TYPE_NEGATIVES` — additional negatives per scene type
- `SCENE_TYPE_NAMES` / `SCENE_TYPE_COLORS` — UI display data
- `PERIOD_LABELS` — human-readable era names for UI
- `SCENE_SPLIT_SYSTEM_PROMPT` — the complete LLM system prompt with all rules,
  classification schemas, abstraction library, output format, and prompt requirements

---

## Flask Routes

| Method | Route | Description |
|---|---|---|
| `GET` | `/` | Landing page + recent projects list |
| `POST` | `/api/estimate` | Live scene-count + **cost** estimate (JSON) |
| `GET` | `/api/pricing` | Raw pricing table (`image_costs`, `prompt_overhead_usd`) |
| `POST` | `/api/generate` | Create Tatterveil batch project + start background generation |
| `POST` | `/api/generate-freeform` | Create freeform batch project (optional instructions + reference image) |
| `GET` | `/api/projects/<id>/status` | Polling: step, scenes, duplicate slots, `export_blocked`, `regeneration_jobs[]`, `cost_estimate`, `cost_actual` |
| `POST` | `/api/projects/<id>/exports` | Start a progress-aware ZIP export job |
| `GET` | `/api/projects/<id>/exports/<job_id>` | Poll export progress (stage, percent, current/total) |
| `GET` | `/api/projects/<id>/exports/<job_id>/file` | Download the finished ZIP |
| `GET` | `/api/projects/<id>/export.zip` | Legacy synchronous ZIP download (no progress) |
| `POST` | `/api/projects/<id>/scenes/<entry>/regenerate` | Enqueue a regeneration (up to 4 run in parallel) |
| `GET` | `/api/projects/<id>/regenerations` | List active + recently-finished regeneration jobs |
| `DELETE` | `/api/projects/<id>/regenerations/<job_id>` | Dismiss a finished regeneration job |
| `DELETE` | `/api/projects/<id>/scenes/<entry>` | Remove one variant and its PNG |
| `GET` | `/projects/<id>` | Project view page |
| `GET` | `/projects/<id>/images/<filename>` | Serve generated image (PNG) |
| `DELETE` | `/api/projects/<id>` | Delete project + all files |
| `POST` | `/api/singles` | Queue a standalone single image (up to 3 run in parallel) |
| `GET` | `/api/singles` | List single images (newest first) + queue info |
| `DELETE` | `/api/singles/<image_id>` | Delete one single image + its files |
| `GET` | `/singles/images/<filename>` | Serve a single image (PNG) |
| `GET` | `/singles/previews/<filename>` | Serve a single-image JPEG preview |
| `GET` | `/singles/download/<filename>` | Download a single image (attachment) |
| `POST` | `/api/voices` | Queue a standalone voice-over (up to 2 run in parallel) |
| `GET` | `/api/voices` | List voice-overs (newest first) + queue info |
| `DELETE` | `/api/voices/<voice_id>` | Delete one voice-over + its directory |
| `GET` | `/voices/<voice_id>/audio` | Stream a voice-over WAV |
| `GET` | `/voices/<voice_id>/download` | Download a voice-over (attachment) |

See **`technicalguide.md`** for the full data model and worker flow.

---

## Project Data Layout

```
projects/
└── <project_id>/
    ├── meta.json      project settings (name, script, quality, rates, scene_plan)
    ├── status.json    live generation state (step, progress, message)
    ├── scenes.json    scene array (timestamps, prompts, image paths, status)
    └── images/
        ├── scene_001.png
        ├── scene_002_v1.png   # optional extra variant after regenerate
        └── ...

singles/                       # standalone Single Image Studio (separate from projects/)
├── singles.json               # array of single-image records (prompt, status, …)
└── images/
    ├── single_<id>.png
    └── thumbs/<id>.jpg         # grid previews

voices/                        # standalone Single Voice Studio (separate from projects/)
├── voices.json                # array of voice records (script, status, duration, …)
└── <voice_id>/
    ├── voiceovers/full_voiceover.wav
    └── sentence_timeline.json
```

---

## Tatterveil Visual Style Rules (summary)

The system enforces all of the following on every generated image:

- **Master style:** Atmospheric Photorealistic — benchmark is National Geographic
  archaeology documentary photography
- **Lighting:** Always ambient/diffused. Never direct sunlight, studio light, or spotlight.
  Mist or atmospheric haze must be present.
- **Color:** Desaturated 15–25%, cool blue-green shadows, warm amber candlelight used
  sparingly, aged muted gold on artifacts (never bright)
- **Composition:** 16:9, rule of thirds, always foreground + midground + background depth
- **Humans:** Avoided. If needed for scale: back turned, face in shadow, period-accurate only
- **Surfaces:** Nothing clean or undamaged — everything shows age, weathering, oxidation

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | OpenAI API key |
| `OPENAI_TEXT_MODEL` | `gpt-5.4-mini` | LLM for scene splitting + prompts |
| `IMAGE_MODEL` | `gpt-image-2` | Image generation model |
| `WORDS_PER_MINUTE` | `150` | Narration pace for duration estimate |
| `MIN_DURATION` | `1.0` | Minimum video duration (minutes) |
| `FIRST_SEGMENT` | `5` | Minutes before scene-rate switches |
| `MAX_WORKERS` | `3` | Parallel image generation threads (initial run) |
| `REGEN_PARALLELISM` | `4` | Max concurrent regeneration image renders |
| `SINGLE_IMAGE_PARALLELISM` | `3` | Max concurrent Single Image Studio renders |
| `SINGLE_VOICE_PARALLELISM` | `2` | Max concurrent Single Voice Studio renders |
| `FREEFORM_REF_MAX_BYTES` | `10485760` | Max upload size for freeform reference images (10 MB) |
| `IMAGE_COSTS` | *(table above)* | Per-image USD price by resolution × quality |
| `PROMPT_GENERATION_FLAT_COST` | `1.00` | Flat per-project overhead added to cost preview |
| `MAX_RETRIES` | `3` | Retry attempts per image |
| `RETRY_DELAY` | `2.5` | Base back-off delay (seconds) |
