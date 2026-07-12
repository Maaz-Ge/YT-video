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

const COST_FORMATTER = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

function formatUSD(n) {
  if (n == null || isNaN(n)) return "—";
  return COST_FORMATTER.format(Number(n));
}

window._latestEstimate = null;

function getSelectedResolution() {
  const el = document.querySelector('input[name="resolution"]:checked');
  return el ? el.value : "2048x1152";
}

function getSelectedQuality() {
  const el = document.querySelector('input[name="quality"]:checked');
  return el ? el.value : "medium";
}

function getVoiceSpeed() {
  const el = document.getElementById("voice_speed");
  if (!el) return 1.0;
  const v = parseFloat(el.value);
  if (isNaN(v)) return 1.0;
  return Math.max(0.25, Math.min(1.0, Math.round(v * 100) / 100));
}

function clampVoiceSpeedInput(input) {
  const v = Math.max(0.25, Math.min(1.0, parseFloat(input.value) || 1.0));
  const rounded = Math.round(v * 100) / 100;
  input.value = String(rounded);
  return rounded;
}

function initEstimatePanel() {
  const scriptEl    = document.getElementById("script");
  const firstSlider = document.getElementById("first_rate_slider");
  const firstInput  = document.getElementById("first_rate");
  const restSlider  = document.getElementById("rest_rate_slider");
  const restInput   = document.getElementById("rest_rate");
  const speedSlider = document.getElementById("voice_speed_slider");
  const speedInput  = document.getElementById("voice_speed");

  if (!scriptEl) return;

  let debounceTimer;

  function syncSlider(slider, input, onSync) {
    if (!slider || !input) return;
    slider.addEventListener("input", () => {
      input.value = slider.value;
      if (onSync) onSync();
      scheduleEstimate();
      updateTimingHints();
    });
    input.addEventListener("input", () => {
      let v = +input.value;
      if (input === speedInput) {
        v = Math.max(+input.min, Math.min(+input.max, v || 1));
        input.value = v;
        slider.value = v;
      } else {
        v = Math.max(+input.min, Math.min(+input.max, v || 1));
        input.value = v;
        slider.value = v;
      }
      if (onSync) onSync();
      scheduleEstimate();
      updateTimingHints();
    });
    input.addEventListener("blur", () => {
      if (input === speedInput) {
        const v = clampVoiceSpeedInput(input);
        slider.value = v;
        updateVoiceSpeedHint();
        scheduleEstimate();
      }
    });
  }

  syncSlider(firstSlider, firstInput);
  syncSlider(restSlider, restInput);
  syncSlider(speedSlider, speedInput, updateVoiceSpeedHint);

  scriptEl.addEventListener("input", () => { scheduleEstimate(); });

  document.querySelectorAll('input[name="resolution"], input[name="quality"]').forEach((r) => {
    r.addEventListener("change", scheduleEstimate);
  });

  function scheduleEstimate() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchEstimate, 300);
  }

  function updateVoiceSpeedHint() {
    const hint = document.getElementById("voice-speed-hint");
    if (!hint) return;
    const s = getVoiceSpeed();
    if (s >= 0.95) hint.textContent = "Normal pace — matches typical documentary narration";
    else if (s >= 0.7) hint.textContent = "Slightly slower — longer video, more breathing room";
    else if (s >= 0.45) hint.textContent = "Slow pace — noticeably longer narration";
    else hint.textContent = "Slowest pace — longest video length";
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
    const resolution = getSelectedResolution();
    const quality   = getSelectedQuality();
    const voiceSpeed = getVoiceSpeed();
    const words     = script.split(/\s+/).filter(Boolean).length;
    const chars     = script.length;

    const anaWords    = document.getElementById("ana-words");
    const anaDuration = document.getElementById("ana-duration");
    const anaScenes   = document.getElementById("ana-scenes");
    const anaHint     = document.getElementById("ana-hint");
    if (anaWords) anaWords.textContent = words.toLocaleString();

    if (!script) {
      if (anaDuration) anaDuration.textContent = "—";
      if (anaScenes)   anaScenes.textContent   = "—";
      if (anaHint)     anaHint.textContent      = "";
      window._latestEstimate = null;
      clearEstimatePanel();
      return;
    }

    try {
      const res = await fetch("/api/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script, first_rate: firstRate, rest_rate: restRate, resolution, quality, voice_speed: voiceSpeed }),
      });
      if (!res.ok) return;
      const data = await res.json();
      window._latestEstimate = data;

      if (anaDuration) anaDuration.textContent = data.duration_minutes.toFixed(1);
      if (anaScenes)   anaScenes.textContent   = data.total_scenes;
      if (anaHint) {
        anaHint.textContent = words < 100
          ? "Tip: longer scripts produce richer video output"
          : words > 2000
          ? "Long script — consider reducing scene rates to control cost"
          : "";
      }

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
          `${chars.toLocaleString()} characters (~1200/min) at speed ${voiceSpeed} → ~${data.duration_minutes.toFixed(1)} min preview (final length from narration)`;
      }

      const c = data.cost || {};
      const costTotalEl = document.getElementById("est-cost-total");
      const costPerEl   = document.getElementById("est-cost-per-image");
      const costScenesEl= document.getElementById("est-cost-scenes");
      const costPromptEl= document.getElementById("est-cost-prompt");
      const costVoiceEl = document.getElementById("est-cost-voice");
      if (costTotalEl) costTotalEl.textContent = formatUSD(c.total_usd);
      if (costPerEl)   costPerEl.textContent   = formatUSD(c.per_image_usd);
      if (costScenesEl)costScenesEl.textContent= c.total_scenes != null ? c.total_scenes : "—";
      if (costPromptEl)costPromptEl.textContent= (c.prompt_overhead_usd != null ? c.prompt_overhead_usd.toFixed(2) : "1.00");
      if (costVoiceEl) costVoiceEl.textContent = (c.voice_cost_usd != null ? c.voice_cost_usd.toFixed(2) : "—");
    } catch (e) { /* silent */ }
  }

  function clearEstimatePanel() {
    ["est-duration","est-first-scenes","est-rest-scenes","est-total-scenes"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.textContent = "—";
    });
    const ct = document.getElementById("est-calc-text");
    if (ct) ct.textContent = "Enter your script to see the estimate.";
    const costTotalEl = document.getElementById("est-cost-total");
    if (costTotalEl) costTotalEl.textContent = "—";
    const costPerEl = document.getElementById("est-cost-per-image");
    if (costPerEl) costPerEl.textContent = "—";
    const costScenesEl = document.getElementById("est-cost-scenes");
    if (costScenesEl) costScenesEl.textContent = "—";
  }

  document.querySelectorAll('input[name="resolution"]').forEach((r) => {
    r.addEventListener("change", () => {
      const line = document.getElementById("style-res-line");
      if (line) line.textContent = `${r.value.replace("x", "×")} · 16:9 · PNG`;
    });
  });

  updateTimingHints();
  updateVoiceSpeedHint();
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

  function setSubmitting(busy) {
    if (busy) {
      submitLabel.textContent = "Launching generation…";
      submitIcon.classList.add("hidden");
      submitSpinner.classList.remove("hidden");
      submitBtn.disabled = true;
    } else {
      submitLabel.textContent = "Generate Scenes";
      submitIcon.classList.remove("hidden");
      submitSpinner.classList.add("hidden");
      submitBtn.disabled = false;
    }
  }

  async function submitProject(payload) {
    setSubmitting(true);
    try {
      const res = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.status === 409) {
        const pid = data.active_project_id;
        const msg = data.error || "Another project is still generating.";
        showToast(msg, "error", 7000);
        if (pid) {
          setTimeout(() => {
            window.location.href = `/projects/${pid}`;
          }, 800);
        }
        setSubmitting(false);
        return;
      }
      if (!res.ok) throw new Error(data.error || "Generation failed.");
      showToast("Project created! Redirecting…", "ok", 2000);
      setTimeout(() => {
        window.location.href = `/projects/${data.project_id}`;
      }, 600);
    } catch (err) {
      showToast(err.message, "error");
      setSubmitting(false);
    }
  }

  function fillCostConfirmModal(payload, estimate) {
    const set = (id, v) => {
      const el = document.getElementById(id);
      if (el) el.textContent = v;
    };
    set("cc-resolution", payload.resolution || "—");
    set("cc-quality", payload.quality || "—");
    set("cc-voice-speed", payload.voice_speed != null ? String(payload.voice_speed) : "—");
    const c = (estimate && estimate.cost) || null;
    set("cc-scenes", estimate ? estimate.total_scenes : "—");
    if (c) {
      set("cc-per-image", formatUSD(c.per_image_usd));
      set("cc-images-subtotal", formatUSD(c.images_subtotal_usd));
      set("cc-prompt-overhead", formatUSD(c.prompt_overhead_usd));
      set("cc-total", formatUSD(c.total_usd));
    } else {
      set("cc-per-image", "—");
      set("cc-images-subtotal", "—");
      set("cc-prompt-overhead", "—");
      set("cc-total", "—");
    }
  }

  function openCostConfirmModal(payload, estimate) {
    const modal = document.getElementById("cost-confirm-modal");
    if (!modal) {
      submitProject(payload);
      return;
    }
    fillCostConfirmModal(payload, estimate);
    modal.hidden = false;

    const cancelBtn = document.getElementById("cc-cancel");
    const confirmBtn = document.getElementById("cc-confirm");

    function close() {
      modal.hidden = true;
      cancelBtn.removeEventListener("click", onCancel);
      confirmBtn.removeEventListener("click", onConfirm);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onEsc);
    }
    function onCancel() { close(); }
    function onConfirm() { close(); submitProject(payload); }
    function onBackdrop(e) { if (e.target === modal) close(); }
    function onEsc(e) { if (e.key === "Escape") close(); }

    cancelBtn.addEventListener("click", onCancel);
    confirmBtn.addEventListener("click", onConfirm);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onEsc);
  }

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

    const formData = new FormData(form);
    const payload  = Object.fromEntries(formData.entries());
    payload.voice_speed = getVoiceSpeed();

    let estimate = window._latestEstimate;
    try {
      const res = await fetch("/api/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          script,
          first_rate: parseInt(payload.first_rate, 10) || 3,
          rest_rate:  parseInt(payload.rest_rate,  10) || 2,
          resolution: payload.resolution,
          quality:    payload.quality,
          voice_speed: payload.voice_speed,
        }),
      });
      if (res.ok) {
        estimate = await res.json();
        window._latestEstimate = estimate;
      }
    } catch (e) { /* fall back to last cached */ }

    openCostConfirmModal(payload, estimate);
  });
}

/* ── Project page: polling + scene grid sync ─────────────────────────────────
   Polls /api/projects/<id>/status while work is in flight or duplicates exist.
   ───────────────────────────────────────────────────────────────────────── */

let _pollTimer = null;
let _lastProgress = -1;
let _regenTargetEntryId = null;
let _regenIsBlocked = false;

const TERMINAL_STEPS = ["done", "error"];
const STEP_ORDER = ["queued", "analysing", "voicing", "prompting", "prompting_done", "generating", "done"];

function initProjectPage() {
  if (typeof window.PROJECT_ID === "undefined") return;

  const step = typeof window.INIT_STEP === "string" ? window.INIT_STEP : "queued";
  const progress = typeof window.INIT_PROGRESS === "number" ? window.INIT_PROGRESS : 0;
  const progressSection = document.getElementById("progress-section");
  if (progressSection) {
    progressSection.style.display =
      step === "done" || step === "error" ? "none" : "block";
  }
  const bar = document.getElementById("progress-bar");
  if (bar != null) {
    bar.style.width = `${progress}%`;
    _lastProgress = progress;
  }
  const pctEl = document.getElementById("progress-pct");
  if (pctEl != null) {
    pctEl.textContent = `${Math.round(progress)}%`;
  }
  const msgEl = document.getElementById("progress-message");
  if (msgEl && typeof window.INIT_MESSAGE === "string" && window.INIT_MESSAGE) {
    msgEl.textContent = window.INIT_MESSAGE;
  }
  updateStepTrack(step);

  const grid = document.getElementById("scene-grid");
  if (grid) {
    grid.addEventListener("click", onSceneGridClick);
    grid.addEventListener("keydown", onSceneGridKey);
  }

  initRegenerateModal();
  initExportButton();
  initLightbox();

  if (window.VOICEOVER) updateProjectVoiceover(window.VOICEOVER);

  pollStatus();
}

function pollDelayMs(data) {
  const step = data.step || "queued";
  const regenBusy = data.regeneration && data.regeneration.busy;
  if (regenBusy) return 1200;
  if (!TERMINAL_STEPS.includes(step)) return 2000;
  if (data.export_blocked) return 3000;
  if (Array.isArray(data.regeneration_jobs) && data.regeneration_jobs.length > 0) return 3000;
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
    if (del.disabled || !id) return;
    if (!confirm("Delete this image variant? This cannot be undone.")) return;
    fetch(`/api/projects/${window.PROJECT_ID}/scenes/${id}`, { method: "DELETE" })
      .then(async (r) => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || "Delete failed");
        showToast("Variant removed.", "ok");
        pollStatus();
      })
      .catch((err) => showToast(err.message || "Delete failed.", "error"));
    return;
  }

  const reg = e.target.closest(".btn-regenerate-variant");
  if (reg) {
    const id = reg.getAttribute("data-entry-id");
    if (!id) return;
    const card = reg.closest(".scene-card");
    const slot = card ? card.querySelector(".scene-badge-num") : null;
    const errEl = card ? card.querySelector(".scene-image-error") : null;
    const isBlocked = errEl && errEl.style.display !== "none";
    openRegenerateModal(id, slot ? slot.textContent.trim() : null, isBlocked);
    return;
  }

  const lbTrigger = e.target.closest('[data-action="open-lightbox"]');
  if (lbTrigger) {
    const card = lbTrigger.closest(".scene-card");
    if (card) openLightboxForCard(card);
    return;
  }

  const dismiss = e.target.closest(".btn-dismiss-regen");
  if (dismiss) {
    const jid = dismiss.getAttribute("data-job-id");
    if (jid) dismissRegenJob(jid);
    return;
  }
}

function onSceneGridKey(e) {
  if (e.key !== "Enter" && e.key !== " ") return;
  const lbTrigger = e.target.closest('[data-action="open-lightbox"]');
  if (!lbTrigger) return;
  e.preventDefault();
  const card = lbTrigger.closest(".scene-card");
  if (card) openLightboxForCard(card);
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
  if (counter) {
    if (step === "generating") {
      counter.style.display = "flex";
      if (counterDone) counterDone.textContent = data.scenes_done || 0;
      if (counterTotal) counterTotal.textContent = data.total_scenes || 0;
    } else {
      counter.style.display = "none";
    }
  }

  const progressSection = document.getElementById("progress-section");
  if (progressSection) {
    progressSection.style.display = step === "done" || step === "error" ? "none" : "block";
  }

  renderRegenJobsPanel(data.regeneration_jobs || [], data.regeneration);
  updateProjectCostDisplay(data.cost_actual, data.cost_estimate);
  updateProjectVoiceover(data.voiceover);

  if (Array.isArray(data.scenes)) {
    syncSceneGrid(data.scenes);
    annotateRegenTargets(data.scenes, data.regeneration_jobs || []);
    updateScenesHeader(data.scenes);
    updateExportAvailability(data);
  }

  if (step === "done" && !window._doneToastShown) {
    window._doneToastShown = true;
    const failed = (data.scenes || []).filter((s) => s.image_status === "error").length;
    if (failed > 0) {
      showToast(
        `Generation finished with ${failed} scene image(s) failed. Review errors below.`,
        "error",
        8000
      );
    } else {
      showToast("All scenes generated successfully!", "ok", 5000);
    }
  }

  if (step === "error") {
    const code = data.error_code || "generation_failed";
    if (window._lastErrorCode !== code || window._lastErrorMessage !== message) {
      window._lastErrorCode = code;
      window._lastErrorMessage = message;
      let msg = message;
      if (code === "content_policy_script") {
        msg =
          "Script rejected by content safety filters. Start a new project with a revised script.";
      } else if (code === "voice_failed") {
        msg = message || "Voice-over generation failed. Check your ElevenLabs API key and try again.";
      }
      showToast(msg, "error", 10000);
    }
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
    voicing: "step-voicing",
    prompting: "step-prompting",
    prompting_done: "step-prompting",
    generating: "step-generating",
    done: "step-done",
    error: null,
  };

  const activeId = stepMap[currentStep];
  const stepIds = ["step-analysing", "step-voicing", "step-prompting", "step-generating", "step-done"];
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

function variantCountBySlot(scenes) {
  const counts = {};
  for (const s of scenes || []) {
    const slot = s.slot_number || s.scene_number || 0;
    counts[slot] = (counts[slot] || 0) + 1;
  }
  return counts;
}

function syncSceneGrid(scenes) {
  const grid = document.getElementById("scene-grid");
  if (!grid) return;
  if (!scenes.length) return;

  const sorted = sortScenesClient(scenes);
  const slotCounts = variantCountBySlot(scenes);
  const wanted = new Set(sorted.map((s) => s.entry_id).filter(Boolean));

  grid.querySelectorAll(".scene-card").forEach((node) => {
    const id = node.getAttribute("data-entry-id");
    if (!id || !wanted.has(id)) node.remove();
  });

  sorted.forEach((scene, idx) => {
    if (!scene.entry_id) return;
    const id = scene.entry_id;
    const slot = scene.slot_number || scene.scene_number || 0;
    const variantCount = slotCounts[slot] || 1;
    let card = document.getElementById(`scene-entry-${id}`);
    if (!card) {
      card = buildSceneCard(scene, idx, variantCount);
      grid.appendChild(card);
      requestAnimationFrame(() => card.classList.add("animate-fade-up"));
    } else {
      updateSceneCardMedia(card, scene, variantCount);
    }
  });
}

function fmtClock(totalSeconds) {
  const s = Math.max(0, Math.round(Number(totalSeconds) || 0));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}

function updateProjectVoiceover(voiceover) {
  const section = document.getElementById("voiceover-section");
  const audio = document.getElementById("voiceover-audio");
  const sub = document.getElementById("voiceover-sub");
  if (!section || !audio) return;

  if (!voiceover || voiceover.status !== "done" || !voiceover.url) {
    section.style.display = "none";
    return;
  }

  section.style.display = "block";
  const src = `/projects/${window.PROJECT_ID}/${voiceover.url}`;
  if (audio.getAttribute("src") !== src) audio.setAttribute("src", src);
  if (sub) {
    const chunks = voiceover.chunks || 1;
    sub.textContent =
      `${fmtClock(voiceover.duration_seconds)} · ${chunks} chunk${chunks === 1 ? "" : "s"} · ` +
      "one combined audio for the whole video";
  }
}

function scenePreviewUrl(filename, cacheBust) {
  const bust = cacheBust ? `?t=${cacheBust}` : "";
  return `/projects/${window.PROJECT_ID}/previews/${filename}${bust}`;
}

function sceneFullImageUrl(filename) {
  return `/projects/${window.PROJECT_ID}/images/${filename}`;
}

function updateSceneCardMedia(card, scene, variantCount = 1) {
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
      img.decoding = "async";
      if (ph) ph.replaceWith(img);
      else wrap.insertBefore(img, wrap.firstChild);
    }
    img.src = scenePreviewUrl(filename, stamp);
    img.dataset.fullSrc = sceneFullImageUrl(filename);
    wrap.classList.add("scene-img-wrap--clickable");
    if (!wrap.hasAttribute("data-action")) {
      wrap.setAttribute("data-action", "open-lightbox");
      wrap.setAttribute("role", "button");
      wrap.setAttribute("tabindex", "0");
    }
    if (!wrap.querySelector(".scene-zoom-btn")) {
      const zb = document.createElement("button");
      zb.type = "button";
      zb.className = "scene-zoom-btn";
      zb.setAttribute("data-action", "open-lightbox");
      zb.setAttribute("aria-label", "View image full screen");
      zb.innerHTML =
        '<svg width="14" height="14" viewBox="0 0 14 14" fill="none">' +
        '<circle cx="6" cy="6" r="4" stroke="currentColor" stroke-width="1.4"/>' +
        '<path d="M9 9l3 3M6 4v4M4 6h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
        "</svg>Enlarge";
      wrap.appendChild(zb);
    }
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
  const canRegen = scene.image_status === "done" || scene.image_status === "error";
  if (regBtn) {
    regBtn.disabled = !canRegen;
    regBtn.title = scene.image_status === "error"
      ? "Image was blocked — regenerate with new instructions"
      : "";
  }

  const delBtn = card.querySelector(".btn-delete-variant");
  if (delBtn) {
    const canDelete = (variantCount || 1) > 1;
    delBtn.disabled = !canDelete;
    delBtn.title = canDelete
      ? "Remove this variant"
      : "Cannot delete the only image for this scene slot";
  }

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

function buildSceneCard(scene, idx, variantCount) {
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

  const zoomBtn = hasImage
    ? `<button type="button" class="scene-zoom-btn" data-action="open-lightbox" aria-label="View image full screen">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <circle cx="6" cy="6" r="4" stroke="currentColor" stroke-width="1.4"/>
          <path d="M9 9l3 3M6 4v4M4 6h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        </svg>
        Enlarge
      </button>`
    : "";

  const imgBlock = hasImage
    ? `<img src="${scenePreviewUrl(filename)}" data-full-src="${sceneFullImageUrl(filename)}" alt="Scene ${num}" class="scene-img" loading="lazy" decoding="async" />${zoomBtn}`
    : `<div class="scene-img-placeholder"><div class="placeholder-spinner"></div></div>`;

  const wrapAttrs = hasImage
    ? ' class="scene-img-wrap scene-img-wrap--clickable" role="button" tabindex="0" data-action="open-lightbox"'
    : ' class="scene-img-wrap"';

  const canDelete = (variantCount || 1) > 1;
  const deleteDisabled = canDelete ? "" : "disabled";
  const deleteTitle = canDelete
    ? "Remove this variant"
    : "Cannot delete the only image for this scene slot";

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
    <div${wrapAttrs}>
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
        <button type="button" class="btn btn-ghost btn-sm btn-regenerate-variant" data-entry-id="${scene.entry_id}"
          title="${scene.image_status === "error" ? "Image was blocked — regenerate with new instructions" : ""}"
          ${(scene.image_status === "done" || scene.image_status === "error") ? "" : "disabled"}>
          ${scene.image_status === "error" ? "Fix blocked image" : "Regenerate"}
        </button>
        <button type="button" class="btn btn-ghost btn-sm btn-delete-variant" data-entry-id="${scene.entry_id}" ${deleteDisabled} title="${escapeAttr(deleteTitle)}">Delete variant</button>
      </div>
      <details class="scene-prompt-details">
        <summary class="scene-prompt-toggle">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M2 4l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          View image prompt
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
        : "ZIP: WAV, scene MP4s, project_meta.txt, scene_timestamps.txt.";
  }
}

let _exportJobId = null;
let _exportPollTimer = null;

function initExportButton() {
  const btn = document.getElementById("btn-export-zip");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/projects/${window.PROJECT_ID}/exports`, {
        method: "POST",
      });
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
        let msg = "Could not start export";
        try { const j = await res.json(); msg = j.error || msg; } catch (e) { /* ignore */ }
        showToast(msg, "error");
        btn.disabled = false;
        return;
      }
      const data = await res.json();
      _exportJobId = data.job.job_id;
      openExportModal();
      pollExportStatus();
    } catch (e) {
      showToast("Export failed.", "error");
      btn.disabled = false;
    }
  });

  const closeBtn = document.getElementById("export-modal-close");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => closeExportModal(false));
  }
  const overlay = document.getElementById("export-modal");
  if (overlay) {
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeExportModal(false);
    });
  }
}

function openExportModal() {
  const modal = document.getElementById("export-modal");
  if (!modal) return;
  modal.hidden = false;
  setExportProgress(0, "Queued…", "");
}

function closeExportModal(success) {
  const modal = document.getElementById("export-modal");
  if (modal) modal.hidden = true;
  clearTimeout(_exportPollTimer);
  _exportPollTimer = null;
  const btn = document.getElementById("btn-export-zip");
  if (btn) btn.disabled = false;
}

function setExportProgress(percent, stage, count) {
  const bar = document.getElementById("export-modal-bar");
  const pct = document.getElementById("export-modal-percent");
  const stageEl = document.getElementById("export-modal-stage");
  const countEl = document.getElementById("export-modal-count");
  if (bar) bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  if (pct) pct.textContent = `${Math.round(percent)}%`;
  if (stageEl && stage) stageEl.textContent = stage;
  if (countEl) countEl.textContent = count || "";
}

async function pollExportStatus() {
  if (!_exportJobId) return;
  try {
    const res = await fetch(
      `/api/projects/${window.PROJECT_ID}/exports/${_exportJobId}`
    );
    if (!res.ok) {
      showToast("Lost the export job.", "error");
      closeExportModal(false);
      return;
    }
    const data = await res.json();
    const job = data.job;
    const total = job.total || 0;
    const cur = job.current || 0;
    const count = total ? `${cur} / ${total}` : "";
    setExportProgress(job.percent || 0, job.message || job.stage || "Working…", count);

    if (job.status === "done") {
      setExportProgress(100, "Archive ready — downloading…", count);
      triggerExportDownload(job);
      return;
    }
    if (job.status === "error") {
      const msg = job.error || "Export failed";
      showToast(msg, "error", 8000);
      const stageEl = document.getElementById("export-modal-stage");
      if (stageEl) stageEl.textContent = msg;
      const btn = document.getElementById("btn-export-zip");
      if (btn) btn.disabled = false;
      return;
    }
    _exportPollTimer = setTimeout(pollExportStatus, job.stage === "rendering_mp4s" ? 1200 : 700);
  } catch (e) {
    _exportPollTimer = setTimeout(pollExportStatus, 1500);
  }
}

function triggerExportDownload(job) {
  const url = `/api/projects/${window.PROJECT_ID}/exports/${_exportJobId}/file`;
  const a = document.createElement("a");
  a.href = url;
  a.download = job.file_name || "export.zip";
  document.body.appendChild(a);
  a.click();
  a.remove();
  showToast("ZIP downloaded.", "ok");
  setTimeout(() => closeExportModal(true), 800);
}

/* ── Lightbox ─────────────────────────────────────────────────────────────── */

function initLightbox() {
  const lb = document.getElementById("lightbox");
  const close = document.getElementById("lightbox-close");
  if (!lb || !close) return;
  close.addEventListener("click", closeLightbox);
  lb.addEventListener("click", (e) => {
    if (e.target === lb) closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (!lb.hidden && e.key === "Escape") closeLightbox();
  });
}

function openLightboxForCard(card) {
  const lb = document.getElementById("lightbox");
  const img = document.getElementById("lightbox-image");
  const cap = document.getElementById("lightbox-caption");
  if (!lb || !img) return;
  const src = card.querySelector("img.scene-img");
  if (!src) return;
  const fullSrc = src.getAttribute("data-full-src") || src.dataset.fullSrc || src.src;
  img.src = fullSrc;
  img.alt = src.alt || "";
  if (cap) {
    const slot = card.querySelector(".scene-badge-num");
    const time = card.querySelector(".scene-badge-time");
    const variant = card.querySelector(".scene-badge-variant");
    const parts = [];
    if (slot) parts.push(`Scene ${slot.textContent.trim()}`);
    if (variant) parts.push(variant.textContent.trim());
    if (time) parts.push(time.textContent.trim());
    cap.textContent = parts.join(" · ");
  }
  lb.hidden = false;
  document.body.classList.add("body-lock");
}

function closeLightbox() {
  const lb = document.getElementById("lightbox");
  if (!lb) return;
  lb.hidden = true;
  document.body.classList.remove("body-lock");
  const img = document.getElementById("lightbox-image");
  if (img) img.src = "";
}

function initRegenerateModal() {
  const modal = document.getElementById("regen-modal");
  const cancel = document.getElementById("regen-cancel");
  const submit = document.getElementById("regen-submit");
  const ta = document.getElementById("regen-instructions");
  if (!modal || !cancel || !submit || !ta) return;

  function close() {
    modal.hidden = true;
    _regenTargetEntryId = null;
    ta.value = "";
  }

  cancel.addEventListener("click", close);

  modal.addEventListener("click", (e) => {
    if (e.target === modal) close();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) close();
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
          body: JSON.stringify({ instructions: instr, is_blocked: _regenIsBlocked }),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Regenerate request failed");
      showToast("Added to the regeneration queue.", "ok", 4000);
      close();
      pollStatus();
    } catch (e) {
      showToast(e.message, "error");
    }
    submit.disabled = false;
  });
}

function openRegenerateModal(entryId, slotLabel, isBlocked) {
  const modal = document.getElementById("regen-modal");
  const ta = document.getElementById("regen-instructions");
  const target = document.getElementById("regen-modal-target");
  const hint = document.getElementById("regen-modal-hint");
  const label = document.getElementById("regen-instructions-label");
  if (!modal || !ta) return;
  _regenTargetEntryId = entryId;
  _regenIsBlocked = !!isBlocked;
  ta.value = "";
  if (target) {
    target.textContent = slotLabel ? `Target: scene slot ${slotLabel}` : "";
  }
  if (hint) {
    hint.textContent = isBlocked
      ? "This image was blocked by OpenAI's content policy. Describe a completely safe visual replacement — avoid any sensitive wording. Your description will be used to generate the new image."
      : "Describe what you want changed — the AI will refine the prompt while keeping the scene's style and era.";
    hint.className = isBlocked ? "regen-hint regen-hint--blocked" : "regen-hint";
  }
  if (label) {
    label.textContent = isBlocked ? "Safe visual description for the new image:" : "Instructions for the new image:";
  }
  modal.hidden = false;
  ta.focus();
}

/* ── Regeneration jobs panel ─────────────────────────────────────────────── */

const REGEN_STATE_LABEL = {
  queued: "Queued — waiting for a worker",
  refining_prompt: "Composing new prompt…",
  generating_image: "Generating new image…",
  done: "Done",
  error: "Failed",
};

function renderRegenJobsPanel(jobs, regen) {
  const section = document.getElementById("regen-jobs-section");
  const list = document.getElementById("regen-jobs-list");
  const activeEl = document.getElementById("regen-active-count");
  const queuedEl = document.getElementById("regen-queued-count");
  const maxEl = document.getElementById("regen-max-parallel");
  if (!section || !list) return;

  if (!jobs || jobs.length === 0) {
    section.style.display = "none";
    list.innerHTML = "";
    return;
  }
  section.style.display = "block";

  let active = 0;
  let queued = 0;
  jobs.forEach((j) => {
    if (j.state === "refining_prompt" || j.state === "generating_image") active += 1;
    else if (j.state === "queued") queued += 1;
  });
  if (activeEl) activeEl.textContent = active;
  if (queuedEl) queuedEl.textContent = queued;
  if (maxEl && regen && regen.max_parallel) maxEl.textContent = regen.max_parallel;

  const existing = new Map();
  list.querySelectorAll("li.regen-job").forEach((node) => {
    existing.set(node.dataset.jobId, node);
  });

  const wanted = new Set(jobs.map((j) => j.job_id));
  existing.forEach((node, jid) => {
    if (!wanted.has(jid)) node.remove();
  });

  jobs.forEach((job) => {
    let node = existing.get(job.job_id);
    if (!node) {
      node = document.createElement("li");
      node.className = "regen-job";
      node.dataset.jobId = job.job_id;
      list.appendChild(node);
    }
    updateRegenJobNode(node, job);
  });
}

function updateRegenJobNode(node, job) {
  const state = job.state || "queued";
  const isDone = state === "done";
  const isErr = state === "error";
  const slot = job.slot_number != null ? String(job.slot_number).padStart(2, "0") : "—";
  const stage = job.stage_message || REGEN_STATE_LABEL[state] || state;
  const errText = job.error ? ` · ${job.error}` : "";
  const instr = (job.instructions || "").slice(0, 90);
  const variantLabel =
    job.variant_index != null && job.variant_index !== "" ? `Variant ${job.variant_index}` : "";

  node.classList.toggle("regen-job--running", !isDone && !isErr);
  node.classList.toggle("regen-job--done", isDone);
  node.classList.toggle("regen-job--error", isErr);

  node.innerHTML = `
    <div class="regen-job-row">
      <span class="regen-job-slot">Scene ${escapeHTML(slot)}</span>
      <span class="regen-job-state regen-job-state--${escapeAttr(state)}">
        ${!isDone && !isErr ? '<span class="regen-spinner"></span>' : ""}
        ${escapeHTML(stage)}${errText ? `<span class="regen-job-err">${escapeHTML(errText)}</span>` : ""}
      </span>
      ${variantLabel ? `<span class="regen-job-variant">${escapeHTML(variantLabel)}</span>` : ""}
      ${
        (isDone || isErr)
          ? `<button type="button" class="btn-dismiss-regen" data-job-id="${escapeAttr(job.job_id)}" aria-label="Dismiss">×</button>`
          : ""
      }
    </div>
    ${instr ? `<div class="regen-job-instr">“${escapeHTML(instr)}${(job.instructions || "").length > 90 ? "…" : ""}”</div>` : ""}
  `;
}

async function dismissRegenJob(jobId) {
  try {
    const res = await fetch(`/api/projects/${window.PROJECT_ID}/regenerations/${jobId}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error("Failed");
    pollStatus();
  } catch (e) { /* ignore */ }
}

function annotateRegenTargets(scenes, jobs) {
  const grid = document.getElementById("scene-grid");
  if (!grid) return;
  grid.querySelectorAll(".scene-card .scene-regen-badge").forEach((el) => el.remove());

  const byEntry = new Map();
  jobs.forEach((j) => {
    if (j.state === "done" || j.state === "error") return;
    if (j.new_entry_id) byEntry.set(j.new_entry_id, j);
  });

  scenes.forEach((s) => {
    if (!s.entry_id) return;
    const job = byEntry.get(s.entry_id);
    if (!job) return;
    const card = document.getElementById(`scene-entry-${s.entry_id}`);
    if (!card) return;
    const wrap = card.querySelector(".scene-img-wrap");
    if (!wrap) return;
    const badge = document.createElement("div");
    badge.className = "scene-regen-badge";
    badge.innerHTML = `<span class="regen-spinner"></span>${escapeHTML(
      job.stage_message || REGEN_STATE_LABEL[job.state] || ""
    )}`;
    wrap.appendChild(badge);
  });
}

function updateProjectCostDisplay(actual, estimate) {
  const valEl = document.getElementById("proj-cost-value");
  const subEl = document.getElementById("proj-cost-sub");
  if (!valEl || !subEl) return;
  const c = actual || estimate;
  if (!c) {
    valEl.textContent = "—";
    subEl.textContent = "awaiting plan";
    return;
  }
  const isFinal = !!actual;
  valEl.textContent = (isFinal ? "" : "~") + formatUSD(c.total_usd);
  subEl.textContent =
    (isFinal ? "Final · " : "Estimate · ") +
    `${c.total_scenes} images × ${formatUSD(c.per_image_usd)} + ${formatUSD(c.prompt_overhead_usd)} prompt`;
}

/* ── Home page: project list polling + generation lock ───────────────────── */

let _homePollTimer = null;
let _projectsFilter = "all";

function initHomePage() {
  const grid = document.getElementById("projects-grid");
  if (!grid) return;

  document.querySelectorAll(".projects-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      _projectsFilter = tab.getAttribute("data-filter") || "all";
      document.querySelectorAll(".projects-tab").forEach((t) => {
        const on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      applyProjectsFilter();
    });
  });

  applyProjectsFilter();
  pollHomeProjects();
}

function applyProjectsFilter() {
  const grid = document.getElementById("projects-grid");
  const filterEmpty = document.getElementById("projects-filter-empty");
  if (!grid) return;

  let visible = 0;
  grid.querySelectorAll(".project-card").forEach((card) => {
    const group = card.getAttribute("data-filter-group") || "complete";
    let show = true;
    if (_projectsFilter === "in_progress") show = group === "in_progress";
    else if (_projectsFilter === "complete") show = group === "complete";
    card.style.display = show ? "" : "none";
    if (show) visible += 1;
  });

  if (filterEmpty) {
    const total = grid.querySelectorAll(".project-card").length;
    filterEmpty.hidden = total === 0 || visible > 0 || _projectsFilter === "all";
  }
}

function applyGenerationLock(locked, active) {
  const banner = document.getElementById("generation-locked-banner");
  const submitBtn = document.getElementById("submit-btn");
  const layout = document.querySelector(".form-layout");
  const pctEl = document.getElementById("generation-locked-pct");
  const link = document.getElementById("generation-locked-link");

  if (banner) banner.hidden = !locked;
  if (submitBtn) {
    submitBtn.disabled = !!locked;
    submitBtn.setAttribute("aria-disabled", locked ? "true" : "false");
  }
  if (layout) layout.classList.toggle("form-layout--locked", !!locked);

  if (locked && active) {
    if (pctEl) pctEl.textContent = String(active.progress ?? 0);
    if (link) link.href = `/projects/${active.id}`;
  }
}

function updateProjectCard(card, p) {
  const step = p.step || "unknown";
  const generating = !!p.is_generating;
  card.setAttribute("data-step", step);
  card.setAttribute("data-generating", generating ? "1" : "0");
  card.setAttribute(
    "data-filter-group",
    generating ? "in_progress" : step === "done" || step === "error" ? "complete" : "complete"
  );

  const dot = card.querySelector(".project-status-dot");
  if (dot) {
    dot.className = `project-status-dot status-${step}`;
  }

  const scenesEl = card.querySelector(".project-scenes");
  if (scenesEl && p.total_scenes != null) scenesEl.textContent = `${p.total_scenes} scenes`;

  const durEl = card.querySelector(".project-duration");
  if (durEl && p.duration_minutes != null) {
    durEl.textContent = `${Number(p.duration_minutes).toFixed(1)} min`;
  }

  const stateEl = card.querySelector(".project-state");
  if (stateEl) {
    stateEl.classList.remove("done", "error", "processing");
    if (step === "done") {
      stateEl.classList.add("done");
      stateEl.textContent = "Complete";
      stateEl.removeAttribute("data-progress");
    } else if (step === "error") {
      stateEl.classList.add("error");
      stateEl.textContent = "Failed";
      stateEl.removeAttribute("data-progress");
    } else {
      stateEl.classList.add("processing");
      stateEl.setAttribute("data-progress", "1");
      stateEl.textContent = `${p.progress ?? 0}% — Processing`;
    }
  }
}

function buildProjectCard(p) {
  const step = p.step || "unknown";
  const generating = !!p.is_generating;
  const filterGroup = generating
    ? "in_progress"
    : step === "done" || step === "error"
      ? "complete"
      : "complete";

  let stateHtml;
  if (step === "done") {
    stateHtml = '<span class="project-state done">Complete</span>';
  } else if (step === "error") {
    stateHtml = '<span class="project-state error">Failed</span>';
  } else {
    stateHtml = `<span class="project-state processing" data-progress>${p.progress ?? 0}% — Processing</span>`;
  }

  const card = document.createElement("a");
  card.href = `/projects/${p.id}`;
  card.className = "project-card";
  card.dataset.projectId = p.id;
  card.dataset.step = step;
  card.dataset.generating = generating ? "1" : "0";
  card.dataset.filterGroup = filterGroup;
  card.innerHTML = `
    <div class="project-card-top">
      <span class="project-status-dot status-${escapeAttr(step)}"></span>
      <span class="project-quality-tag">${escapeHTML(p.quality || "medium")}</span>
    </div>
    <h3 class="project-card-name">${escapeHTML(p.name || "Untitled")}</h3>
    <div class="project-card-meta">
      <span class="project-scenes">${p.total_scenes ?? 0} scenes</span>
      <span class="meta-sep">·</span>
      <span class="project-duration">${Number(p.duration_minutes || 0).toFixed(1)} min</span>
      <span class="meta-sep">·</span>
      <span>${escapeHTML(p.style || "Tatterveil")}</span>
    </div>
    <div class="project-card-footer">
      ${stateHtml}
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 7h10M8 3l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
  `;
  return card;
}

async function pollHomeProjects() {
  try {
    const res = await fetch("/api/projects");
    if (!res.ok) {
      scheduleHomePoll(8000);
      return;
    }
    const data = await res.json();
    const projects = data.projects || [];
    const grid = document.getElementById("projects-grid");
    const empty = document.getElementById("projects-empty");

    if (empty) empty.hidden = projects.length > 0;

    if (grid) {
      const existing = new Map();
      grid.querySelectorAll(".project-card").forEach((c) => {
        existing.set(c.dataset.projectId, c);
      });

      projects.forEach((p) => {
        let card = existing.get(p.id);
        if (!card) {
          card = buildProjectCard(p);
          grid.insertBefore(card, grid.firstChild);
        } else {
          updateProjectCard(card, p);
        }
      });
    }

    applyGenerationLock(!!data.generation_locked, data.active_generation);
    applyProjectsFilter();

    const needsFast =
      data.generation_locked ||
      projects.some((p) => p.is_generating);
    scheduleHomePoll(needsFast ? 2500 : 12000);
  } catch (e) {
    scheduleHomePoll(8000);
  }
}

function scheduleHomePoll(ms) {
  clearTimeout(_homePollTimer);
  _homePollTimer = setTimeout(pollHomeProjects, ms);
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

/* ── Single Image studio ──────────────────────────────────────────────────── */

let _singlesPollTimer = null;

function initSingleImageStudio() {
  const form = document.getElementById("single-form");
  const gallery = document.getElementById("single-gallery");
  if (!form || !gallery) return;

  initStudioModeTabs();
  initSingleLightbox();

  const submitBtn = document.getElementById("single-submit-btn");
  const spinner = document.getElementById("single-submit-spinner");
  const label = document.getElementById("single-submit-label");
  const promptEl = document.getElementById("single_prompt");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = (promptEl.value || "").trim();
    if (!prompt) {
      showToast("Enter a prompt to generate an image.", "error");
      return;
    }
    const resolution = getSingleResolution();
    const quality = getSingleQuality();

    if (submitBtn) submitBtn.disabled = true;
    if (spinner) spinner.classList.remove("hidden");
    if (label) label.textContent = "Adding to queue…";

    try {
      const res = await fetch("/api/singles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, resolution, quality }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Generation request failed");
      showToast("Added to the image queue.", "ok", 3500);
      promptEl.value = "";
      pollSingles();
    } catch (err) {
      showToast(err.message, "error");
    }

    if (submitBtn) submitBtn.disabled = false;
    if (spinner) spinner.classList.add("hidden");
    if (label) label.textContent = "Generate Image";
  });

  gallery.addEventListener("click", (e) => {
    const del = e.target.closest(".btn-delete-single");
    if (del) {
      deleteSingle(del.dataset.id);
      return;
    }
    const img = e.target.closest(".single-card-img");
    if (img) {
      openSingleLightbox(img.getAttribute("data-full-src") || img.src, img.alt || "");
    }
  });

  pollSingles();
}

function initStudioModeTabs() {
  const tabs = document.querySelectorAll(".studio-mode-tab");
  const batch = document.getElementById("mode-batch");
  const single = document.getElementById("mode-single");
  if (!tabs.length || !batch || !single) return;

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const mode = tab.getAttribute("data-mode");
      tabs.forEach((t) => {
        const on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      const isSingle = mode === "single";
      batch.hidden = isSingle;
      single.hidden = !isSingle;
    });
  });
}

function getSingleResolution() {
  const el = document.querySelector('input[name="single_resolution"]:checked');
  return el ? el.value : "2048x1152";
}

function getSingleQuality() {
  const el = document.querySelector('input[name="single_quality"]:checked');
  return el ? el.value : "medium";
}

async function pollSingles() {
  const gallery = document.getElementById("single-gallery");
  if (!gallery) return;
  clearTimeout(_singlesPollTimer);

  try {
    const res = await fetch("/api/singles");
    if (!res.ok) {
      _singlesPollTimer = setTimeout(pollSingles, 8000);
      return;
    }
    const data = await res.json();
    const images = data.images || [];
    renderSinglesGallery(images);
    updateSingleQueueInfo(data);

    const busy = images.some((i) => i.status === "pending" || i.status === "generating");
    if (busy) _singlesPollTimer = setTimeout(pollSingles, 2500);
  } catch (e) {
    _singlesPollTimer = setTimeout(pollSingles, 8000);
  }
}

function updateSingleQueueInfo(data) {
  const info = document.getElementById("single-queue-info");
  const activeEl = document.getElementById("single-active-count");
  const maxEl = document.getElementById("single-max-parallel");
  if (!info) return;
  const active = data.active_count || 0;
  if (activeEl) activeEl.textContent = active;
  if (maxEl && data.max_parallel) maxEl.textContent = data.max_parallel;
  info.hidden = active === 0;
}

function renderSinglesGallery(images) {
  const gallery = document.getElementById("single-gallery");
  const empty = document.getElementById("single-gallery-empty");
  if (!gallery) return;

  if (empty) empty.hidden = images.length > 0;

  const existing = new Map();
  gallery.querySelectorAll(".single-card").forEach((node) => {
    existing.set(node.dataset.id, node);
  });

  const wanted = new Set(images.map((i) => i.id));
  existing.forEach((node, id) => {
    if (!wanted.has(id)) node.remove();
  });

  images.forEach((img) => {
    let node = existing.get(img.id);
    if (!node) {
      node = document.createElement("div");
      node.className = "single-card";
      node.dataset.id = img.id;
      gallery.appendChild(node);
    } else if (node.dataset.status === "done" && img.status === "done") {
      // Already fully rendered — don't rewrite, so an open prompt dropdown
      // stays open across polling refreshes.
      return;
    }
    updateSingleCard(node, img);
  });

  // Keep newest first (API already sorts; reorder DOM to match).
  images.forEach((img) => {
    const node = gallery.querySelector(`.single-card[data-id="${CSS.escape(img.id)}"]`);
    if (node) gallery.appendChild(node);
  });
}

function updateSingleCard(node, img) {
  const status = img.status || "pending";
  node.dataset.status = status;

  let media;
  if (status === "done" && img.preview_url) {
    media = `
      <div class="single-card-media">
        <img class="single-card-img" src="${escapeAttr(img.preview_url)}" data-full-src="${escapeAttr(img.image_url)}" alt="${escapeAttr(img.prompt)}" loading="lazy" />
      </div>`;
  } else if (status === "error") {
    media = `
      <div class="single-card-media single-card-media--error">
        <span class="single-card-error-icon">!</span>
        <span class="single-card-error-text">${escapeHTML(img.error || "Generation failed")}</span>
      </div>`;
  } else {
    media = `
      <div class="single-card-media single-card-media--pending">
        <span class="spinner"></span>
        <span class="single-card-pending-text">${status === "generating" ? "Generating…" : "Queued…"}</span>
      </div>`;
  }

  const actions = `
    <div class="single-card-actions">
      ${
        status === "done" && img.download_url
          ? `<a class="btn btn-ghost btn-sm" href="${escapeAttr(img.download_url)}" download>Download</a>`
          : ""
      }
      <button type="button" class="btn btn-ghost btn-sm btn-delete-single" data-id="${escapeAttr(img.id)}">Delete</button>
    </div>`;

  const prompt = img.prompt || "";
  const promptHtml = `
    <details class="scene-prompt-details single-prompt-details">
      <summary class="scene-prompt-toggle">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M2 4l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        View image prompt
      </summary>
      <div class="scene-prompt-body">
        <p class="scene-prompt-text">${escapeHTML(prompt)}</p>
      </div>
    </details>`;

  node.innerHTML = `
    ${media}
    <div class="single-card-body">
      ${promptHtml}
      <div class="single-card-meta">
        <span class="single-card-tag">${escapeHTML(img.resolution || "")}</span>
        <span class="single-card-tag">${escapeHTML(img.quality || "")}</span>
      </div>
      ${actions}
    </div>`;
}

async function deleteSingle(id) {
  if (!id) return;
  if (!confirm("Delete this image?")) return;
  try {
    const res = await fetch(`/api/singles/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Delete failed");
    const node = document.querySelector(`.single-card[data-id="${CSS.escape(id)}"]`);
    if (node) node.remove();
    pollSingles();
  } catch (e) {
    showToast("Delete failed.", "error");
  }
}

function initSingleLightbox() {
  const lb = document.getElementById("single-lightbox");
  const close = document.getElementById("single-lightbox-close");
  if (!lb || !close) return;
  close.addEventListener("click", closeSingleLightbox);
  lb.addEventListener("click", (e) => {
    if (e.target === lb) closeSingleLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (!lb.hidden && e.key === "Escape") closeSingleLightbox();
  });
}

function openSingleLightbox(src, alt) {
  const lb = document.getElementById("single-lightbox");
  const img = document.getElementById("single-lightbox-image");
  const cap = document.getElementById("single-lightbox-caption");
  if (!lb || !img || !src) return;
  img.src = src;
  img.alt = alt || "";
  if (cap) cap.textContent = alt || "";
  lb.hidden = false;
  document.body.classList.add("body-lock");
}

function closeSingleLightbox() {
  const lb = document.getElementById("single-lightbox");
  if (!lb) return;
  lb.hidden = true;
  document.body.classList.remove("body-lock");
  const img = document.getElementById("single-lightbox-image");
  if (img) img.src = "";
}

/* ── Bootstrap ────────────────────────────────────────────────────────────── */

document.addEventListener("DOMContentLoaded", () => {
  initEstimatePanel();
  initCreateForm();
  initHomePage();
  initSingleImageStudio();
  initProjectPage();
  initDeleteButton();

  const grid = document.getElementById("scene-grid");
  if (grid && grid.querySelectorAll(".scene-card").length > 0) {
    const header = document.getElementById("scenes-header");
    if (header) header.style.display = "block";
  }
});
