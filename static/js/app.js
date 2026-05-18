/* ═══════════════════════════════════════════════════════════════════════════
   ThomCreates Scene Studio — Frontend Logic
   ═══════════════════════════════════════════════════════════════════════════ */

"use strict";

/* ── Floating tooltip system ──────────────────────────────────────────────── */

(function initTooltips() {
  const tip = document.getElementById("tooltip");
  if (!tip) return;

  document.addEventListener("mouseover", (e) => {
    const btn = e.target.closest("[data-tip]");
    if (!btn) { tip.classList.remove("visible"); return; }

    tip.textContent = btn.getAttribute("data-tip");
    tip.classList.add("visible");
    positionTip(btn);
  });

  document.addEventListener("mouseout", (e) => {
    if (!e.target.closest("[data-tip]")) tip.classList.remove("visible");
  });

  document.addEventListener("mousemove", (e) => {
    if (tip.classList.contains("visible")) {
      const btn = e.target.closest("[data-tip]");
      if (btn) positionTip(btn);
    }
  });

  function positionTip(anchor) {
    const r = anchor.getBoundingClientRect();
    const tw = 300;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    const top = r.top - 10; // above anchor
    tip.style.left  = left + "px";
    tip.style.top   = top  + "px";
    tip.style.transform = "translateY(-100%)";
  }
})();

/* ── Toast notifications ──────────────────────────────────────────────────── */

function showToast(message, type = "info", duration = 4000) {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(20px)";
    toast.style.transition = "opacity 250ms, transform 250ms";
    setTimeout(() => toast.remove(), 260);
  }, duration);
}

/* ── Clipboard helper ─────────────────────────────────────────────────────── */

function copyPrompt(btn) {
  const prompt = btn.getAttribute("data-prompt");
  if (!prompt) return;
  navigator.clipboard.writeText(prompt).then(() => {
    const original = btn.innerHTML;
    btn.textContent = "Copied!";
    btn.style.color = "var(--clr-ok)";
    setTimeout(() => {
      btn.innerHTML = original;
      btn.style.color = "";
    }, 1800);
  });
}

/* ── Live estimate (index page) ───────────────────────────────────────────── */

function initEstimatePanel() {
  const scriptEl    = document.getElementById("script");
  const firstSlider = document.getElementById("first_rate_slider");
  const firstInput  = document.getElementById("first_rate");
  const restSlider  = document.getElementById("rest_rate_slider");
  const restInput   = document.getElementById("rest_rate");

  if (!scriptEl) return;

  let debounceTimer;

  function syncSlider(slider, input) {
    slider.addEventListener("input", () => { input.value = slider.value; scheduleEstimate(); updateTimingHints(); });
    input.addEventListener("input", () => {
      const v = Math.max(+input.min, Math.min(+input.max, +input.value || 1));
      input.value = v; slider.value = v; scheduleEstimate(); updateTimingHints();
    });
  }

  syncSlider(firstSlider, firstInput);
  syncSlider(restSlider, restInput);

  scriptEl.addEventListener("input", () => { scheduleEstimate(); });

  function scheduleEstimate() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchEstimate, 300);
  }

  function updateTimingHints() {
    const fr = parseInt(firstInput.value, 10) || 3;
    const rr = parseInt(restInput.value, 10)  || 2;
    const fh = document.getElementById("first-rate-hint");
    const rh = document.getElementById("rest-rate-hint");
    if (fh) fh.textContent = fr === 1 ? "1 image per minute" : `1 image every ${Math.round(60/fr)} seconds`;
    if (rh) rh.textContent = rr === 1 ? "1 image per minute" : `1 image every ${Math.round(60/rr)} seconds`;
  }

  async function fetchEstimate() {
    const script    = scriptEl.value.trim();
    const firstRate = parseInt(firstInput.value, 10) || 3;
    const restRate  = parseInt(restInput.value,  10) || 2;
    const words     = script.split(/\s+/).filter(Boolean).length;

    // Update inline analysis bar
    const anaWords    = document.getElementById("ana-words");
    const anaDuration = document.getElementById("ana-duration");
    const anaScenes   = document.getElementById("ana-scenes");
    const anaHint     = document.getElementById("ana-hint");
    if (anaWords) anaWords.textContent = words.toLocaleString();

    if (!script) {
      if (anaDuration) anaDuration.textContent = "—";
      if (anaScenes)   anaScenes.textContent   = "—";
      if (anaHint)     anaHint.textContent      = "";
      clearEstimatePanel();
      return;
    }

    try {
      const res = await fetch("/api/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script, first_rate: firstRate, rest_rate: restRate }),
      });
      if (!res.ok) return;
      const data = await res.json();

      // Inline analysis bar
      if (anaDuration) anaDuration.textContent = data.duration_minutes.toFixed(1);
      if (anaScenes)   anaScenes.textContent   = data.total_scenes;
      if (anaHint) {
        anaHint.textContent = words < 100
          ? "Tip: longer scripts produce richer video output"
          : words > 2000
          ? "Long script — consider reducing scene rates to control cost"
          : "";
      }

      // Estimate panel
      const estDuration = document.getElementById("est-duration");
      const estFirst    = document.getElementById("est-first-scenes");
      const estRest     = document.getElementById("est-rest-scenes");
      const estTotal    = document.getElementById("est-total-scenes");
      const calcText    = document.getElementById("est-calc-text");

      if (estDuration) estDuration.textContent = data.duration_minutes.toFixed(1);
      if (estFirst) {
        const seg = Math.min(data.duration_minutes, 5).toFixed(1);
        estFirst.textContent = `${seg} min × ${firstRate}/min = ${data.first_segment_scenes}`;
      }
      if (estRest) {
        const seg = Math.max(0, data.duration_minutes - 5).toFixed(1);
        estRest.textContent = `${seg} min × ${restRate}/min = ${data.rest_segment_scenes}`;
      }
      if (estTotal) estTotal.textContent = data.total_scenes;
      if (calcText) {
        calcText.textContent =
          `${words.toLocaleString()} words ÷ 150 wpm = ${data.duration_minutes.toFixed(1)} min video`;
      }
    } catch (e) { /* silent */ }
  }

  function clearEstimatePanel() {
    ["est-duration","est-first-scenes","est-rest-scenes","est-total-scenes"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.textContent = "—";
    });
    const ct = document.getElementById("est-calc-text");
    if (ct) ct.textContent = "Enter your script to see the estimate.";
  }

  // Resolution selector → update style preview line live
  document.querySelectorAll('input[name="resolution"]').forEach((r) => {
    r.addEventListener("change", () => {
      const line = document.getElementById("style-res-line");
      if (line) line.textContent = `${r.value.replace("x", "×")} · 16:9 · PNG`;
    });
  });

  updateTimingHints();
  scriptEl.dispatchEvent(new Event("input"));
}

/* ── Create form submission ───────────────────────────────────────────────── */

function initCreateForm() {
  const form       = document.getElementById("create-form");
  const submitBtn  = document.getElementById("submit-btn");
  const submitLabel = document.getElementById("submit-label");
  const submitIcon  = document.getElementById("submit-icon");
  const submitSpinner = document.getElementById("submit-spinner");

  if (!form) return;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    const name   = form.querySelector("#name").value.trim();
    const script = form.querySelector("#script").value.trim();

    if (!name) {
      showToast("Please enter a video title.", "error");
      form.querySelector("#name").focus();
      return;
    }
    if (!script || script.split(/\s+/).length < 10) {
      showToast("Script is too short — please add more content.", "error");
      form.querySelector("#script").focus();
      return;
    }

    // Collect form data
    const formData = new FormData(form);
    const payload  = Object.fromEntries(formData.entries());

    // Show loading state
    submitLabel.textContent = "Launching generation…";
    submitIcon.classList.add("hidden");
    submitSpinner.classList.remove("hidden");
    submitBtn.disabled = true;

    try {
      const res = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.error || "Generation failed.");
      }

      showToast("Project created! Redirecting…", "ok", 2000);
      setTimeout(() => {
        window.location.href = `/projects/${data.project_id}`;
      }, 600);

    } catch (err) {
      showToast(err.message, "error");
      submitLabel.textContent = "Generate Scenes";
      submitIcon.classList.remove("hidden");
      submitSpinner.classList.add("hidden");
      submitBtn.disabled = false;
    }
  });
}

/* ── Project page: polling + scene grid sync ─────────────────────────────────
   Polls /api/projects/<id>/status while work is in flight or duplicates exist.
   ───────────────────────────────────────────────────────────────────────── */

let _pollTimer = null;
let _lastProgress = -1;
let _regenTargetEntryId = null;

const TERMINAL_STEPS = ["done", "error"];
const STEP_ORDER = ["queued", "analysing", "prompting", "prompting_done", "generating", "done"];

function initProjectPage() {
  if (typeof window.PROJECT_ID === "undefined") return;

  const step = typeof window.INIT_STEP === "string" ? window.INIT_STEP : "queued";
  const progressSection = document.getElementById("progress-section");
  if (progressSection && (step === "done" || step === "error")) {
    progressSection.style.display = "none";
  }
  const bar = document.getElementById("progress-bar");
  if (bar != null && typeof window.INIT_PROGRESS === "number") {
    bar.style.width = `${window.INIT_PROGRESS}%`;
    _lastProgress = window.INIT_PROGRESS;
  }
  const pctEl = document.getElementById("progress-pct");
  if (pctEl != null && typeof window.INIT_PROGRESS === "number") {
    pctEl.textContent = `${Math.round(window.INIT_PROGRESS)}%`;
  }

  const grid = document.getElementById("scene-grid");
  if (grid) {
    grid.addEventListener("click", onSceneGridClick);
  }

  initRegenerateModal();
  initExportButton();

  pollStatus();
}

function pollDelayMs(data) {
  const step = data.step || "queued";
  const regenBusy = data.regeneration && data.regeneration.busy;
  if (regenBusy) return 1200;
  if (!TERMINAL_STEPS.includes(step)) return 2000;
  if (data.export_blocked) return 3000;
  return 6000;
}

async function pollStatus() {
  try {
    const res = await fetch(`/api/projects/${window.PROJECT_ID}/status`);
    if (!res.ok) {
      scheduleNextPoll(5000);
      return;
    }
    const data = await res.json();
    applyStatus(data);
    scheduleNextPoll(pollDelayMs(data));
  } catch (e) {
    scheduleNextPoll(5000);
  }
}

function scheduleNextPoll(ms) {
  clearTimeout(_pollTimer);
  _pollTimer = setTimeout(pollStatus, ms);
}

function onSceneGridClick(e) {
  const expandBtn = e.target.closest(".btn-expand-text");
  if (expandBtn && !expandBtn.closest(".scene-card-actions")) {
    e.preventDefault();
    const wrap = expandBtn.closest(".scene-script-seg");
    if (!wrap) return;
    const full = wrap.querySelector(".seg-full");
    const trunc = wrap.querySelector(".seg-trunc");
    if (!full || !trunc) return;
    const expanded = expandBtn.getAttribute("data-expanded") === "1";
    if (!expanded) {
      full.removeAttribute("hidden");
      trunc.setAttribute("hidden", "");
      expandBtn.textContent = "Show less";
      expandBtn.setAttribute("data-expanded", "1");
    } else {
      full.setAttribute("hidden", "");
      trunc.removeAttribute("hidden");
      expandBtn.textContent = "… read more";
      expandBtn.setAttribute("data-expanded", "0");
    }
    return;
  }

  const del = e.target.closest(".btn-delete-variant");
  if (del) {
    const id = del.getAttribute("data-entry-id");
    if (!id || !confirm("Delete this image variant? This cannot be undone.")) return;
    fetch(`/api/projects/${window.PROJECT_ID}/scenes/${id}`, { method: "DELETE" })
      .then((r) => {
        if (!r.ok) throw new Error("Delete failed");
        showToast("Variant removed.", "ok");
        pollStatus();
      })
      .catch(() => showToast("Delete failed.", "error"));
    return;
  }

  const reg = e.target.closest(".btn-regenerate-variant");
  if (reg) {
    const id = reg.getAttribute("data-entry-id");
    if (!id) return;
    openRegenerateModal(id);
  }
}

/* ── Status → UI ──────────────────────────────────────────────────────────── */

function applyStatus(data) {
  const step = data.step || "queued";
  const progress = data.progress || 0;
  const message = data.message || "";

  const bar = document.getElementById("progress-bar");
  if (bar && progress !== _lastProgress) {
    bar.style.width = `${progress}%`;
    _lastProgress = progress;
  }

  const pctEl = document.getElementById("progress-pct");
  if (pctEl) pctEl.textContent = `${progress}%`;

  const msgEl = document.getElementById("progress-message");
  if (msgEl) msgEl.textContent = message;

  updateStepTrack(step);

  const counter = document.getElementById("image-counter");
  const counterDone = document.getElementById("counter-done");
  const counterTotal = document.getElementById("counter-total");
  if (counter && step === "generating") {
    counter.style.display = "flex";
    if (counterDone) counterDone.textContent = data.scenes_done || 0;
    if (counterTotal) counterTotal.textContent = data.total_scenes || 0;
  }

  const progressSection = document.getElementById("progress-section");
  if (progressSection) {
    progressSection.style.display = step === "done" || step === "error" ? "none" : "block";
  }

  const regBusy = document.getElementById("regen-busy-banner");
  if (regBusy) {
    regBusy.style.display =
      data.regeneration && data.regeneration.busy ? "flex" : "none";
  }

  if (Array.isArray(data.scenes)) {
    syncSceneGrid(data.scenes);
    updateScenesHeader(data.scenes);
    updateExportAvailability(data);
  }

  if (step === "done" && !window._doneToastShown) {
    window._doneToastShown = true;
    showToast("All scenes generated successfully!", "ok", 5000);
  }

  if (step === "error") {
    showToast(`Error: ${message}`, "error", 8000);
    clearTimeout(_pollTimer);
  }

  updateDuplicateBanner(data.duplicate_slots);
}

function updateDuplicateBanner(slots) {
  const el = document.getElementById("duplicate-scenes-banner");
  const txt = document.getElementById("duplicate-scenes-text");
  if (!el || !txt) return;
  if (!slots || slots.length === 0) {
    el.style.display = "none";
    txt.textContent = "";
    return;
  }
  el.style.display = "flex";
  const label = slots.map((s) => `slot ${s}`).join(", ");
  txt.textContent = `Multiple images share the same timestamps for: ${label}. Delete variants until each timestamp has exactly one image to enable ZIP export.`;
}

function updateStepTrack(currentStep) {
  const stepMap = {
    queued: "step-analysing",
    analysing: "step-analysing",
    prompting: "step-prompting",
    prompting_done: "step-prompting",
    generating: "step-generating",
    done: "step-done",
    error: null,
  };

  const activeId = stepMap[currentStep];
  const stepIds = ["step-analysing", "step-prompting", "step-generating", "step-done"];
  const activeIdx = stepIds.indexOf(activeId);

  stepIds.forEach((id, idx) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove("active", "done");
    if (activeIdx >= 0) {
      if (idx < activeIdx) el.classList.add("done");
      if (idx === activeIdx) el.classList.add("active");
    }
  });
}

/* ── Scene grid ───────────────────────────────────────────────────────────── */

function sortScenesClient(scenes) {
  return [...scenes].sort((a, b) => {
    const sa = a.slot_number || a.scene_number || 0;
    const sb = b.slot_number || b.scene_number || 0;
    if (sa !== sb) return sa - sb;
    return (a.variant_index || 0) - (b.variant_index || 0);
  });
}

function syncSceneGrid(scenes) {
  const grid = document.getElementById("scene-grid");
  if (!grid) return;
  if (!scenes.length) return;

  const sorted = sortScenesClient(scenes);
  const wanted = new Set(sorted.map((s) => s.entry_id).filter(Boolean));

  grid.querySelectorAll(".scene-card").forEach((node) => {
    const id = node.getAttribute("data-entry-id");
    if (!id || !wanted.has(id)) node.remove();
  });

  sorted.forEach((scene, idx) => {
    if (!scene.entry_id) return;
    const id = scene.entry_id;
    let card = document.getElementById(`scene-entry-${id}`);
    if (!card) {
      card = buildSceneCard(scene, idx);
      grid.appendChild(card);
      requestAnimationFrame(() => card.classList.add("animate-fade-up"));
    } else {
      updateSceneCardMedia(card, scene);
    }
  });
}

function updateSceneCardMedia(card, scene) {
  const hasImage = scene.image_path && scene.image_status === "done";
  const wrap = card.querySelector(".scene-img-wrap");
  if (!wrap) return;
  const filename = hasImage ? scene.image_path.split(/[/\\]/).pop() : null;
  const stamp = Date.now();
  if (hasImage) {
    let img = wrap.querySelector("img.scene-img");
    if (!img) {
      const ph = wrap.querySelector(".scene-img-placeholder");
      img = document.createElement("img");
      img.className = "scene-img";
      img.alt = `Scene ${scene.slot_number || scene.scene_number}`;
      img.loading = "lazy";
      if (ph) ph.replaceWith(img);
      else wrap.insertBefore(img, wrap.firstChild);
    }
    img.src = `/projects/${window.PROJECT_ID}/images/${filename}?t=${stamp}`;
  }
  const pend = card.querySelector(".scene-pending-label");
  if (pend) {
    pend.style.display = scene.image_status === "pending" ? "block" : "none";
  }
  const err = card.querySelector(".scene-image-error");
  if (err) {
    err.style.display =
      scene.image_status === "error" ? "block" : "none";
    if (scene.image_status === "error") err.textContent = scene.image_error || "Image failed";
  }
  const regBtn = card.querySelector(".btn-regenerate-variant");
  if (regBtn) regBtn.disabled = scene.image_status !== "done";

  let dupBar = card.querySelector(".scene-dup-banner");
  if (scene.slot_has_duplicates) {
    if (!dupBar) {
      dupBar = document.createElement("div");
      dupBar.className = "scene-dup-banner";
      dupBar.textContent = "Duplicate timestamp — keep one variant for export";
      card.insertBefore(dupBar, card.firstChild);
    }
    dupBar.style.display = "block";
  } else if (dupBar) {
    dupBar.remove();
  }
}

function buildSceneCard(scene, idx) {
  const slot = scene.slot_number || scene.scene_number;
  const num = slot;
  const startMM = fmtTime(scene.start_time || 0);
  const endMM = fmtTime(scene.end_time || 0);
  const typeName =
    (window.SCENE_TYPE_NAMES || {})[scene.scene_type] || scene.scene_type_name || "Unknown";
  const typeColor = (window.SCENE_TYPE_COLORS || {})[scene.scene_type] || "#555";
  const periodLabel =
    (window.PERIOD_LABELS || {})[scene.time_period] || scene.time_period || "";
  const hasImage = scene.image_path && scene.image_status === "done";
  const filename = hasImage ? scene.image_path.split(/[/\\]/).pop() : null;
  const vLabel = (scene.variant_index || 0) === 0 ? "Original" : `Variant ${scene.variant_index}`;

  const card = document.createElement("div");
  card.className = "scene-card";
  card.id = `scene-entry-${scene.entry_id}`;
  card.dataset.entryId = scene.entry_id;
  card.style.setProperty("--delay", `${idx * 0.04}s`);

  const dupBanner =
    scene.slot_has_duplicates
      ? `<div class="scene-dup-banner">Duplicate timestamp — keep one variant for export</div>`
      : "";

  const imgBlock = hasImage
    ? `<img src="/projects/${window.PROJECT_ID}/images/${filename}" alt="Scene ${num}" class="scene-img" loading="lazy" />`
    : `<div class="scene-img-placeholder"><div class="placeholder-spinner"></div></div>`;

  const abstractTag = scene.abstraction_mode
    ? `<span class="scene-abstract-tag">Abstract Mode</span>`
    : "";

  const reasoningHtml =
    scene.time_period_reasoning || scene.scene_type_reasoning
      ? `<div class="scene-reasoning">
      ${scene.time_period_reasoning ? `<p class="reasoning-item"><span class="reasoning-key">Era:</span> ${escapeHTML(scene.time_period_reasoning)}</p>` : ""}
      ${scene.scene_type_reasoning ? `<p class="reasoning-item"><span class="reasoning-key">Type:</span> ${escapeHTML(scene.scene_type_reasoning)}</p>` : ""}
    </div>`
      : "";

  const seg = scene.script_segment || "";
  const scriptSegHtml =
    seg.length > 220
      ? `<p class="scene-script-seg">
          <span class="seg-trunc">${escapeHTML(seg.slice(0, 220))}</span>
          <span class="seg-full" hidden>${escapeHTML(seg)}</span>
          <button type="button" class="btn-expand-text" data-expanded="0">… read more</button>
        </p>`
      : `<p class="scene-script-seg"><span class="seg-only">${escapeHTML(seg)}</span></p>`;

  const negHtml = scene.negative_prompt
    ? `<p class="scene-prompt-label-inner" style="margin-top:0.75rem">Negative constraints:</p>
    <p class="scene-prompt-text scene-prompt-negative">${escapeHTML(scene.negative_prompt)}</p>`
    : "";

  const promptEscaped = escapeAttr(scene.prompt || "");

  const errDisplay = scene.image_status === "error" ? "block" : "none";
  const pendDisplay = scene.image_status === "pending" ? "block" : "none";

  card.innerHTML = `
    ${dupBanner}
    <div class="scene-img-wrap">
      ${imgBlock}
      <div class="scene-pending-label" style="display:${pendDisplay}">Rendering…</div>
      <div class="scene-image-error" style="display:${errDisplay}">${escapeHTML(scene.image_error || "")}</div>
      <div class="scene-img-overlay">
        <span class="scene-badge-num">${String(num).padStart(2, "0")}</span>
        <span class="scene-badge-time">${startMM} – ${endMM}</span>
        <span class="scene-badge-variant">${escapeHTML(vLabel)}</span>
      </div>
    </div>
    <div class="scene-card-body">
      <div class="scene-card-tags">
        <span class="scene-type-tag" style="--tag-color:${typeColor}">${escapeHTML(typeName)}</span>
        <span class="scene-period-tag">${escapeHTML(periodLabel)}</span>
        ${abstractTag}
      </div>
      ${reasoningHtml}
      <div class="scene-section">
        <p class="scene-section-label">
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
            <rect x="1" y="1" width="9" height="9" rx="1" stroke="currentColor" stroke-width="1.2"/>
            <path d="M3 4h5M3 6.5h3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
          </svg>
          Script covered in this scene
        </p>
        ${scriptSegHtml}
      </div>
      <div class="scene-card-actions">
        <button type="button" class="btn btn-ghost btn-sm btn-regenerate-variant" data-entry-id="${scene.entry_id}" ${scene.image_status === "done" ? "" : "disabled"}>Regenerate</button>
        <button type="button" class="btn btn-ghost btn-sm btn-delete-variant" data-entry-id="${scene.entry_id}">Delete variant</button>
      </div>
      <details class="scene-prompt-details">
        <summary class="scene-prompt-toggle">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M2 4l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Prompt sent to gpt-image-2
        </summary>
        <div class="scene-prompt-body">
          <p class="scene-prompt-label-inner">Positive prompt:</p>
          <p class="scene-prompt-text">${escapeHTML(scene.prompt || "")}</p>
          ${negHtml}
          <div class="scene-prompt-actions">
            <button class="btn-copy-prompt" onclick="copyPrompt(this)" data-prompt="${promptEscaped}">
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                <rect x="1.5" y="3" width="7" height="8" rx="1" stroke="currentColor" stroke-width="1.2"/>
                <path d="M3 3V2a.5.5 0 01.5-.5h7a.5.5 0 01.5.5v8a.5.5 0 01-.5.5H10"
                  stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
              </svg>
              Copy prompt
            </button>
          </div>
        </div>
      </details>
    </div>
  `;

  return card;
}

function updateScenesHeader(scenes) {
  const header = document.getElementById("scenes-header");
  const badge = document.getElementById("scenes-count-badge");
  if (!header) return;

  const ready = scenes.filter((s) => s.image_status === "done").length;
  const total = scenes.length;

  if (ready > 0 || total > 0) {
    header.style.display = "block";
    if (badge) badge.textContent = `${ready} / ${total} images`;
  }
}

function updateExportAvailability(data) {
  const btn = document.getElementById("btn-export-zip");
  const hint = document.getElementById("export-zip-hint");
  if (!btn) return;
  const blocked = data.export_blocked;
  const step = data.step || "";
  const can =
    !blocked &&
    (step === "done" || step === "error") &&
    Array.isArray(data.scenes) &&
    data.scenes.some((s) => s.image_status === "done");
  btn.disabled = !can;
  if (hint) {
    hint.textContent = blocked
      ? "Export disabled: resolve duplicate timestamps first."
      : step !== "done" && step !== "error"
        ? "Export is available when generation finishes."
        : "Download MP4 chunks + metadata as a ZIP.";
  }
}

function initExportButton() {
  const btn = document.getElementById("btn-export-zip");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/projects/${window.PROJECT_ID}/export.zip`);
      if (res.status === 409) {
        const j = await res.json();
        showToast(
          (j.error || "Export blocked") +
            (j.duplicate_slots ? ` Slots: ${j.duplicate_slots.join(", ")}` : ""),
          "error",
          8000
        );
        btn.disabled = false;
        return;
      }
      if (!res.ok) {
        let msg = "Export failed";
        try {
          const j = await res.json();
          msg = j.error || msg;
        } catch (e) { /* ignore */ }
        showToast(msg, "error");
        btn.disabled = false;
        return;
      }
      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const m = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/.exec(cd);
      let fname = "export.zip";
      if (m && m[1]) fname = m[1].replace(/['"]/g, "");
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      a.click();
      URL.revokeObjectURL(url);
      showToast("ZIP downloaded.", "ok");
    } catch (e) {
      showToast("Export failed.", "error");
    }
    btn.disabled = false;
  });
}

function initRegenerateModal() {
  const modal = document.getElementById("regen-modal");
  const cancel = document.getElementById("regen-cancel");
  const submit = document.getElementById("regen-submit");
  const ta = document.getElementById("regen-instructions");
  if (!modal || !cancel || !submit || !ta) return;

  cancel.addEventListener("click", () => {
    modal.hidden = true;
    _regenTargetEntryId = null;
    ta.value = "";
  });

  modal.addEventListener("click", (e) => {
    if (e.target === modal) cancel.click();
  });

  submit.addEventListener("click", async () => {
    const instr = ta.value.trim();
    if (!_regenTargetEntryId || !instr) {
      showToast("Add instructions for the new image.", "error");
      return;
    }
    submit.disabled = true;
    try {
      const res = await fetch(
        `/api/projects/${window.PROJECT_ID}/scenes/${_regenTargetEntryId}/regenerate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ instructions: instr }),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Regenerate request failed");
      showToast("Regeneration started — new variant will appear shortly.", "ok", 5000);
      cancel.click();
      pollStatus();
    } catch (e) {
      showToast(e.message, "error");
    }
    submit.disabled = false;
  });
}

function openRegenerateModal(entryId) {
  const modal = document.getElementById("regen-modal");
  const ta = document.getElementById("regen-instructions");
  if (!modal || !ta) return;
  _regenTargetEntryId = entryId;
  ta.value = "";
  modal.hidden = false;
  ta.focus();
}

/* ── Delete project ───────────────────────────────────────────────────────── */

function initDeleteButton() {
  const btn = document.getElementById("btn-delete");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!confirm("Delete this project and all generated images?")) return;
    try {
      await fetch(`/api/projects/${window.PROJECT_ID}`, { method: "DELETE" });
      window.location.href = "/";
    } catch (e) {
      showToast("Delete failed.", "error");
    }
  });
}

/* ── Utility ──────────────────────────────────────────────────────────────── */

function fmtTime(seconds) {
  const s = Math.round(seconds || 0);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

function escapeHTML(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(str) {
  return String(str).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ── Bootstrap ────────────────────────────────────────────────────────────── */

document.addEventListener("DOMContentLoaded", () => {
  initEstimatePanel();
  initCreateForm();
  initProjectPage();
  initDeleteButton();

  const grid = document.getElementById("scene-grid");
  if (grid && grid.querySelectorAll(".scene-card").length > 0) {
    const header = document.getElementById("scenes-header");
    if (header) header.style.display = "block";
  }
});
