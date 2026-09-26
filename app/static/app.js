/* didder GUI — vanilla JS, no build step.
 *
 * State is a flat object whose keys are exactly the server's Params fields, so
 * a render request is just {session, params: state}. The server is the single
 * source of truth for validation and for the generated command.
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const STORAGE_LAST = "didder-gui.last-state.v1";
const STORAGE_PRESETS = "didder-gui.presets.v1";
const DEBOUNCE_MS = 150;

const DEFAULTS = Object.freeze({
  algorithm: "bayer",
  bayer_x: 4,
  bayer_y: 4,
  odm_name: "ClusteredDot4x4",
  odm_custom: false,
  odm_matrix: $("#odm-matrix").value,
  edm_name: "FloydSteinberg",
  edm_custom: false,
  edm_matrix: $("#edm-matrix").value,
  serpentine: false,
  random_advanced: false,
  random_min: -0.5,
  random_max: 0.5,
  random_rgb: [-0.5, 0.5, -0.5, 0.5, -0.5, 0.5],
  seed: null,
  palette_mode: "colors",
  palette: ["black", "white"],
  mmcq: 8,
  recolor_enabled: false,
  recolor: ["black", "F273FF"],
  strength: 100,
  brightness: 0,
  contrast: 0,
  saturation: 0,
  grayscale: false,
  no_exif_rotation: false,
  width: null,
  height: null,
  upscale: 1,
  format: "png",
  compression: "default",
  no_overwrite: false,
  multi_mode: "batch",
  fps: 10,
  loop: 0,
  threads: null,
});

let config = null;
let state = clone(DEFAULTS);
let session = null;
let images = [];
let activeIndex = 0;

// Render bookkeeping: every request gets a monotonically increasing id, and a
// response is only applied if it is still the newest one. The previous fetch
// is also aborted, and the server kills the superseded didder process.
let requestSeq = 0;
let inflight = null;
let debounceTimer = null;
let lastPreview = null;

// Zoom: an integer number of *device* pixels per image pixel.
let zoomMode = "fit";
let zoomFactor = 1;

// Decoded images drawn into the stage canvases.
let afterImage = null;
let beforeImage = null;
let beforeUrl = null;
const MAX_CANVAS_EDGE = 16384;

function clone(o) { return JSON.parse(JSON.stringify(o)); }
function storageGet(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; }
  catch { return fallback; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode etc. */ }
}

/** Keep only known keys (the server rejects unknown fields). */
function sanitizeState(raw) {
  const out = clone(DEFAULTS);
  if (!raw || typeof raw !== "object") return out;
  for (const k of Object.keys(DEFAULTS)) {
    if (k in raw && raw[k] !== undefined) out[k] = raw[k];
  }
  return out;
}

async function api(path, body, { signal, method } = {}) {
  const res = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  return { status: res.status, ok: res.ok, data };
}

/* ======================================================================== */
/* Init                                                                     */
/* ======================================================================== */

async function init() {
  const { data } = await api("/api/config");
  config = data;

  $("#didder-version").textContent = config.didder;
  $("#upload-limit").textContent = `max ${config.max_upload_mb} MB each`;
  $("#threads-note").textContent = `container CPU quota: ${config.threads_default}`;

  buildSelects();
  buildSliders();
  buildDuals();
  buildSwatchEditors();
  wireControls();
  wireUpload();
  wireStage();
  renderPresetList();

  state = sanitizeState(storageGet(STORAGE_LAST, null));
  if (state.threads == null) state.threads = config.threads_default;
  if (!config.mmcq_supported && state.palette_mode === "mmcq") state.palette_mode = "colors";

  renderAll();
  changed({ immediate: true });
}

function buildSelects() {
  const bayer = $("#bayer");
  const groups = {};
  for (const c of config.bayer_combos) {
    if (!groups[c.group]) {
      groups[c.group] = document.createElement("optgroup");
      groups[c.group].label = c.group;
      bayer.append(groups[c.group]);
    }
    groups[c.group].append(new Option(c.label, `${c.x}x${c.y}`));
  }

  for (const n of config.odm_names) $("#odm-name").append(new Option(n, n));
  for (const n of config.edm_names) $("#edm-name").append(new Option(n, n));
  for (const n of config.compression_types) $("#compression").append(new Option(n, n));
  for (let n = 2; n <= 256; n *= 2) $("#mmcq").append(new Option(`mmcq:${n}`, String(n)));

  const presetSel = $("#palette-preset");
  for (const p of config.palette_presets) presetSel.append(new Option(p.name, p.id));

  if (!config.mmcq_supported) {
    $("#mmcq-tab").disabled = true;
    $("#mmcq-tab").title = "Not supported by this didder build";
    $("#mmcq-unsupported").hidden = false;
  }
}

/* ======================================================================== */
/* Sliders with numeric entry                                               */
/* ======================================================================== */

const sliders = {};

function buildSliders() {
  for (const el of $$(".slider-field")) {
    const key = el.dataset.slider;
    const [min, max, step, def] = ["min", "max", "step", "default"].map((a) => Number(el.dataset[a]));
    const label = el.querySelector(".label");
    const hint = el.querySelector(".hint");

    const top = document.createElement("div");
    top.className = "top";
    top.append(label);

    const right = document.createElement("div");
    right.className = "num";
    const num = document.createElement("input");
    Object.assign(num, { type: "number", min, max, step, id: `${key}-num` });
    num.dataset.field = key;
    const pct = document.createElement("span");
    pct.textContent = "%";
    const reset = document.createElement("button");
    Object.assign(reset, { type: "button", className: "reset", textContent: "↺", title: `Reset to ${def}%` });
    right.append(reset, num, pct);
    top.append(right);

    const range = document.createElement("input");
    Object.assign(range, { type: "range", min, max, step, id: `${key}-range` });
    range.setAttribute("aria-label", label.textContent);

    const err = document.createElement("small");
    err.className = "field-error";
    err.dataset.errorFor = key;

    el.prepend(top, range);
    if (hint) el.append(hint);
    el.append(err);

    const set = (v, fromNum) => {
      if (fromNum && (v === "" || Number.isNaN(Number(v)))) return;
      let n = Number(v);
      n = Math.max(min, Math.min(max, n));
      state[key] = n;
      range.value = String(n);
      if (!fromNum) num.value = String(n);
      changed();
    };
    range.addEventListener("input", () => set(range.value, false));
    num.addEventListener("input", () => set(num.value, true));
    num.addEventListener("change", () => { num.value = String(state[key]); });
    reset.addEventListener("click", () => { num.value = String(def); set(def, false); });

    sliders[key] = { el, range, num };
  }
}

function renderSliders() {
  for (const [key, s] of Object.entries(sliders)) {
    s.range.value = String(state[key]);
    s.num.value = String(state[key]);
  }
  const random = state.algorithm === "random";
  sliders.strength.el.classList.toggle("disabled", random);
  sliders.strength.range.disabled = random;
  sliders.strength.num.disabled = random;
  $("#strength-hint").textContent = random
    ? "The random command ignores --strength."
    : "Try ~64% for bayer/odm on colour images, ~80% to reduce edm noise.";
}

/* ======================================================================== */
/* Dual-handle range sliders (random min/max)                               */
/* ======================================================================== */

const duals = {};

function buildDuals() {
  const defs = { all: "Noise range (min / max)", r: "Red", g: "Green", b: "Blue" };
  for (const el of $$(".dual")) {
    const key = el.dataset.dual;
    el.innerHTML = `
      <div class="top">
        <span>${defs[key]}</span>
        <span class="vals">
          <input type="number" step="0.01" min="-1" max="1" aria-label="${defs[key]} minimum">
          <span>/</span>
          <input type="number" step="0.01" min="-1" max="1" aria-label="${defs[key]} maximum">
        </span>
      </div>
      <div class="track">
        <div class="rail"></div><div class="zero"></div><div class="fill"></div>
        <input type="range" min="-1" max="1" step="0.01" aria-label="${defs[key]} minimum">
        <input type="range" min="-1" max="1" step="0.01" aria-label="${defs[key]} maximum">
      </div>`;
    const [numLo, numHi] = $$(".vals input", el);
    const [lo, hi] = $$(".track input", el);
    const fill = $(".fill", el);

    const get = () => key === "all"
      ? [state.random_min, state.random_max]
      : [state.random_rgb["rgb".indexOf(key) * 2], state.random_rgb["rgb".indexOf(key) * 2 + 1]];
    const put = (a, b) => {
      a = Math.round(a * 100) / 100;
      b = Math.round(b * 100) / 100;
      if (key === "all") { state.random_min = a; state.random_max = b; }
      else { const i = "rgb".indexOf(key) * 2; state.random_rgb[i] = a; state.random_rgb[i + 1] = b; }
    };
    const paint = () => {
      const [a, b] = get();
      lo.value = a; hi.value = b; numLo.value = a; numHi.value = b;
      fill.style.left = `${((a + 1) / 2) * 100}%`;
      fill.style.width = `${Math.max(0, ((b - a) / 2) * 100)}%`;
      // Keep the lower thumb grabbable when both handles meet at the right end.
      lo.style.zIndex = a > 0.9 ? "3" : "2";
    };
    lo.addEventListener("input", () => { const [, b] = get(); put(Math.min(Number(lo.value), b), b); paint(); changed(); });
    hi.addEventListener("input", () => { const [a] = get(); put(a, Math.max(Number(hi.value), a)); paint(); changed(); });
    const fromNums = () => {
      const a = Number(numLo.value), b = Number(numHi.value);
      if (numLo.value === "" || numHi.value === "" || Number.isNaN(a) || Number.isNaN(b)) return;
      put(Math.max(-1, Math.min(1, a)), Math.max(-1, Math.min(1, b)));
      const [pa, pb] = get();
      lo.value = pa; hi.value = pb;
      fill.style.left = `${((pa + 1) / 2) * 100}%`;
      fill.style.width = `${Math.max(0, ((pb - pa) / 2) * 100)}%`;
      changed();  // min > max is reported inline by the server
    };
    numLo.addEventListener("input", fromNums);
    numHi.addEventListener("input", fromNums);
    numLo.addEventListener("change", paint);
    numHi.addEventListener("change", paint);
    duals[key] = { paint };
  }
}

/* ======================================================================== */
/* Colour swatch editors (palette + recolor)                                */
/* ======================================================================== */

const editors = {};

function buildSwatchEditors() {
  editors.palette = makeSwatchEditor($("#palette-swatches"), "palette", false);
  editors.recolor = makeSwatchEditor($("#recolor-swatches"), "recolor", true);

  $("#palette-add").addEventListener("click", () => editors.palette.add());
  $("#recolor-add").addEventListener("click", () => editors.recolor.add());

  $("#palette-paste").addEventListener("click", () => {
    const text = prompt(
      "Paste a didder palette string (space-separated colours), e.g.\n1E1E1E CDCDCD EDEDED FFFFFF",
      state.palette.join(" "),
    );
    if (text == null) return;
    const list = text.trim().split(/\s+/).filter(Boolean);
    if (list.length) { state.palette = list; editors.palette.render(); changed(); }
  });

  $("#recolor-match").addEventListener("click", () => {
    matchRecolorLength(state.palette_mode === "mmcq" ? state.mmcq : state.palette.length);
    editors.recolor.render();
    changed();
  });

  $("#derive-gray").addEventListener("click", async () => {
    const { ok, data } = await api("/api/derive-grayscale", { colors: state.recolor });
    if (!ok) {
      showError("Cannot derive palette", typeof data?.detail === "string" ? data.detail : "Fix the invalid recolor colours first.");
      return;
    }
    state.palette_mode = "colors";
    state.palette = data.palette;
    renderAll();
    changed();
  });
}

/** Trim or pad the recolor list to n entries (padding repeats the last colour). */
function matchRecolorLength(n) {
  const cur = state.recolor.slice(0, n);
  while (cur.length < n) cur.push(cur.length ? cur[cur.length - 1] : "black");
  state.recolor = cur;
}

function makeSwatchEditor(container, key, allowAlpha) {
  let dragFrom = null;

  function row(i) {
    const value = state[key][i];
    const el = document.createElement("div");
    el.className = "swatch";
    el.draggable = false;
    el.innerHTML = `
      <span class="grip" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
      <input type="color" aria-label="Pick colour ${i + 1}">
      <input type="text" spellcheck="false" autocomplete="off" aria-label="Colour ${i + 1} value">
      <span class="ops">
        <button type="button" data-op="up" title="Move up" aria-label="Move up">↑</button>
        <button type="button" data-op="down" title="Move down" aria-label="Move down">↓</button>
        <button type="button" data-op="del" title="Remove" aria-label="Remove colour">×</button>
      </span>
      <small class="err"></small>`;
    const [picker, text] = $$("input", el);
    text.value = value;
    const hex = quickHex(value);
    if (hex) picker.value = hex;

    picker.addEventListener("input", () => {
      // Preserve an RGBA alpha channel when the colour is changed via picker.
      const cur = state[key][i] || "";
      const parts = cur.split(",");
      const h = picker.value.slice(1);
      if (allowAlpha && parts.length === 4) {
        const [r, g, b] = [0, 2, 4].map((o) => parseInt(h.slice(o, o + 2), 16));
        state[key][i] = `${r},${g},${b},${parts[3].trim()}`;
      } else {
        state[key][i] = h;
      }
      text.value = state[key][i];
      changed();
    });
    text.addEventListener("input", () => {
      state[key][i] = text.value.trim();
      const h = quickHex(text.value);
      if (h) picker.value = h;
      changed();
    });

    el.querySelector(".ops").addEventListener("click", (ev) => {
      const op = ev.target.dataset?.op;
      if (!op) return;
      const list = state[key];
      if (op === "del") {
        if (list.length <= 1) return;
        list.splice(i, 1);
      } else if (op === "up" && i > 0) {
        [list[i - 1], list[i]] = [list[i], list[i - 1]];
      } else if (op === "down" && i < list.length - 1) {
        [list[i + 1], list[i]] = [list[i], list[i + 1]];
      } else return;
      render();
      changed();
    });

    // Drag-to-reorder, initiated only from the grip so text selection works.
    const grip = el.querySelector(".grip");
    grip.addEventListener("mousedown", () => { el.draggable = true; });
    el.addEventListener("dragstart", (ev) => { dragFrom = i; el.classList.add("dragging"); ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", String(i)); });
    el.addEventListener("dragend", () => { el.draggable = false; el.classList.remove("dragging"); $$(".drop-before", container).forEach((n) => n.classList.remove("drop-before")); });
    el.addEventListener("dragover", (ev) => { if (dragFrom == null) return; ev.preventDefault(); ev.stopPropagation(); el.classList.add("drop-before"); });
    el.addEventListener("dragleave", () => el.classList.remove("drop-before"));
    el.addEventListener("drop", (ev) => {
      if (dragFrom == null) return;
      ev.preventDefault(); ev.stopPropagation();
      const list = state[key];
      const [moved] = list.splice(dragFrom, 1);
      list.splice(dragFrom < i ? i - 1 : i, 0, moved);
      dragFrom = null;
      render();
      changed();
    });
    return el;
  }

  function render() {
    container.replaceChildren(...state[key].map((_, i) => row(i)));
  }

  function add() {
    const list = state[key];
    list.push(list.length ? list[list.length - 1] : "808080");
    render();
    changed();
    const inputs = $$('input[type=text]', container);
    inputs[inputs.length - 1]?.focus();
  }

  /** Apply per-swatch results from /api/check-colors without re-rendering. */
  function mark(results) {
    const rows = $$(".swatch", container);
    results.forEach((r, i) => {
      const el = rows[i];
      if (!el) return;
      const [picker, text] = $$("input", el);
      const err = $(".err", el);
      text.classList.toggle("invalid", !r.ok);
      err.textContent = r.ok ? "" : r.error;
      if (r.ok && document.activeElement !== picker) picker.value = r.hex;
      if (r.ok && r.alpha !== 255) {
        err.innerHTML = `<span class="alpha-badge">alpha ${r.alpha}/255</span>`;
      }
    });
  }

  return { render, add, mark };
}

/** Best-effort hex for the colour picker; the server remains authoritative. */
function quickHex(v) {
  const s = String(v || "").trim();
  let m = s.match(/^#?([0-9a-f]{6})$/i);
  if (m) return `#${m[1].toLowerCase()}`;
  m = s.match(/^#?([0-9a-f])([0-9a-f])([0-9a-f])$/i);
  if (m && !/^\d+$/.test(s)) return `#${m[1]}${m[1]}${m[2]}${m[2]}${m[3]}${m[3]}`.toLowerCase();
  m = s.match(/^(\d{1,3}),(\d{1,3}),(\d{1,3})(?:,\d{1,3})?$/);
  if (m) return "#" + m.slice(1, 4).map((n) => Math.min(255, +n).toString(16).padStart(2, "0")).join("");
  if (/^\d{1,3}$/.test(s) && +s <= 255) { const h = (+s).toString(16).padStart(2, "0"); return `#${h}${h}${h}`; }
  return null;
}

/* ======================================================================== */
/* Wiring                                                                   */
/* ======================================================================== */

function bindCheckbox(id, key, after) {
  $(id).addEventListener("change", (e) => { state[key] = e.target.checked; after?.(); changed(); });
}

/** Integer/number inputs: invalid entry becomes an inline error, never a request. */
function bindNumber(id, key, { integer = true, optional = false, min = -Infinity, max = Infinity, label } = {}) {
  const el = $(id);
  el.addEventListener("input", () => {
    const raw = el.value.trim();
    clientErrors.delete(key);
    if (raw === "") {
      if (optional) state[key] = null;
      else clientErrors.set(key, `${label} is required.`);
    } else {
      const n = Number(raw);
      if (!Number.isFinite(n)) clientErrors.set(key, `${label} must be a number.`);
      else if (integer && !Number.isInteger(n)) {
        clientErrors.set(key, key === "upscale"
          ? "Upscale must be a whole number — a fractional factor would distort the dither pattern."
          : `${label} must be a whole number.`);
      } else if (n < min || n > max) clientErrors.set(key, `${label} must be between ${min} and ${max}.`);
      else state[key] = n;
    }
    changed();
  });
}

const clientErrors = new Map();
// Editors whose swatches currently show their own inline error; the same
// message from the server is then not repeated under the whole panel.
const swatchInvalid = { palette: false, recolor: false };

function wireControls() {
  $$("#algo-tabs button").forEach((b) => b.addEventListener("click", () => {
    state.algorithm = b.dataset.algo;
    renderAll();
    changed();
  }));

  $("#bayer").addEventListener("change", (e) => {
    const [x, y] = e.target.value.split("x").map(Number);
    state.bayer_x = x; state.bayer_y = y;
    changed();
  });

  $("#odm-name").addEventListener("change", (e) => { state.odm_name = e.target.value; changed(); });
  $("#edm-name").addEventListener("change", (e) => { state.edm_name = e.target.value; changed(); });
  $("#odm-matrix").addEventListener("input", (e) => { state.odm_matrix = e.target.value; changed(); });
  $("#edm-matrix").addEventListener("input", (e) => { state.edm_matrix = e.target.value; changed(); });
  bindCheckbox("#odm-custom", "odm_custom", renderAlgo);
  bindCheckbox("#edm-custom", "edm_custom", renderAlgo);
  bindCheckbox("#serpentine", "serpentine");
  bindCheckbox("#random-advanced", "random_advanced", renderAlgo);

  bindNumber("#seed", "seed", { optional: true, min: -(2 ** 53), max: 2 ** 53, label: "Seed" });
  $("#seed-roll").addEventListener("click", () => {
    state.seed = Math.floor(Math.random() * 2 ** 31);
    $("#seed").value = state.seed;
    clientErrors.delete("seed");
    changed();
  });

  $$("#palette-mode button").forEach((b) => b.addEventListener("click", () => {
    if (b.disabled) return;
    state.palette_mode = b.dataset.mode;
    renderAll();
    changed();
  }));
  $("#mmcq").addEventListener("change", (e) => { state.mmcq = Number(e.target.value); changed(); });

  $("#palette-preset").addEventListener("change", (e) => {
    const p = config.palette_presets.find((x) => x.id === e.target.value);
    $("#palette-preset-note").textContent = p?.note || "";
    if (!p) return;
    state.palette_mode = "colors";
    state.palette = [...p.palette];
    if (p.recolor) {
      state.recolor_enabled = true;
      state.recolor = [...p.recolor];
    } else if (state.recolor_enabled && state.recolor.length !== state.palette.length) {
      state.recolor_enabled = false;  // would otherwise be an immediate length mismatch
    }
    renderAll();
    changed();
  });

  bindCheckbox("#recolor-enabled", "recolor_enabled", () => {
    // Recolor must match the palette length; start from a valid state.
    const n = state.palette_mode === "mmcq" ? state.mmcq : state.palette.length;
    if (state.recolor_enabled && state.recolor.length !== n) matchRecolorLength(n);
    renderAll();
  });
  bindCheckbox("#grayscale", "grayscale");
  bindCheckbox("#no-exif-rotation", "no_exif_rotation");
  bindCheckbox("#no-overwrite", "no_overwrite");

  bindNumber("#width", "width", { optional: true, min: 1, max: 20000, label: "Width" });
  bindNumber("#height", "height", { optional: true, min: 1, max: 20000, label: "Height" });
  bindNumber("#upscale", "upscale", { min: 1, max: 32, label: "Upscale" });
  bindNumber("#threads", "threads", { optional: true, min: 1, max: 256, label: "Threads" });
  bindNumber("#fps", "fps", { integer: false, optional: true, min: 0.01, max: 100, label: "FPS" });
  bindNumber("#loop", "loop", { min: 0, max: 65535, label: "Loops" });

  $("#format").addEventListener("change", (e) => { state.format = e.target.value; renderAll(); changed(); });
  $("#compression").addEventListener("change", (e) => { state.compression = e.target.value; changed(); });

  $$("#multi-mode button").forEach((b) => b.addEventListener("click", () => {
    state.multi_mode = b.dataset.mode;
    if (state.multi_mode === "animate") state.format = "gif";
    renderAll();
    changed();
  }));

  // Presets
  $("#preset-save").addEventListener("click", savePreset);
  $("#preset-name").addEventListener("keydown", (e) => { if (e.key === "Enter") savePreset(); });
  $("#reset-all").addEventListener("click", () => {
    if (!confirm("Reset every setting to its default?")) return;
    state = clone(DEFAULTS);
    state.threads = config.threads_default;
    clientErrors.clear();
    renderAll();
    changed();
  });

  // Errors / export
  $("#error-close").addEventListener("click", hideError);
  $("#export-close").addEventListener("click", () => { $("#export-result").hidden = true; });
  $("#export-btn").addEventListener("click", doExport);
  $("#copy-cmd").addEventListener("click", copyCommand);
}

/* ======================================================================== */
/* Rendering state → controls                                               */
/* ======================================================================== */

function renderAll() {
  renderAlgo();

  $("#bayer").value = `${state.bayer_x}x${state.bayer_y}`;
  $("#odm-name").value = state.odm_name;
  $("#edm-name").value = state.edm_name;
  $("#odm-matrix").value = state.odm_matrix;
  $("#edm-matrix").value = state.edm_matrix;
  $("#odm-custom").checked = state.odm_custom;
  $("#edm-custom").checked = state.edm_custom;
  $("#serpentine").checked = state.serpentine;
  $("#random-advanced").checked = state.random_advanced;
  $("#seed").value = state.seed ?? "";
  Object.values(duals).forEach((d) => d.paint());

  // Palette
  const mmcq = state.palette_mode === "mmcq";
  $$("#palette-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === state.palette_mode));
  $("#palette-colors-pane").hidden = mmcq;
  $("#palette-mmcq-pane").hidden = !mmcq;
  $("#mmcq").value = String(state.mmcq);
  editors.palette.render();

  // Recolor
  $("#recolor-enabled").checked = state.recolor_enabled;
  $("#recolor-pane").hidden = !state.recolor_enabled;
  editors.recolor.render();

  renderSliders();
  $("#grayscale").checked = state.grayscale;
  $("#no-exif-rotation").checked = state.no_exif_rotation;

  // Sizing / output
  $("#width").value = state.width ?? "";
  $("#height").value = state.height ?? "";
  $("#upscale").value = state.upscale;
  $("#format").value = state.format;
  $("#compression").value = state.compression;
  $("#compression").disabled = state.format !== "png";
  $("#threads").value = state.threads ?? "";
  $("#no-overwrite").checked = state.no_overwrite;
  $("#fps").value = state.fps ?? "";
  $("#loop").value = state.loop;

  renderMulti();
}

function renderAlgo() {
  $$("#algo-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.algo === state.algorithm)));
  $$(".algo-pane").forEach((p) => { p.hidden = p.dataset.pane !== state.algorithm; });
  $("#odm-name-field").hidden = state.odm_custom;
  $("#odm-matrix-field").hidden = !state.odm_custom;
  $("#edm-name-field").hidden = state.edm_custom;
  $("#edm-matrix-field").hidden = !state.edm_custom;
  $("#random-simple").hidden = state.random_advanced;
  $("#random-rgb").hidden = !state.random_advanced;
  if (sliders.strength) renderSliders();
}

function renderMulti() {
  const multi = images.length > 1;
  const btn = $("#export-btn");
  if (btn.textContent !== "Exporting…") {
    btn.disabled = !images.length;
    btn.textContent = multi
      ? (state.multi_mode === "animate" ? "Export animated GIF" : `Export all ${images.length}`)
      : "Export full resolution";
  }
  $("#multi-fieldset").hidden = !multi;
  $$("#multi-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === state.multi_mode));
  const animate = state.multi_mode === "animate";
  $("#animate-fields").hidden = !animate;
  if (!multi) return;
  const sizes = new Set(images.map((i) => `${i.width}x${i.height}`));
  $("#multi-hint").textContent = animate
    ? (sizes.size > 1 && !(state.width && state.height)
        ? "Frames differ in size: set both width and height, or didder will refuse to build the GIF."
        : `Combines ${images.length} images into one animated GIF, in list order.`)
    : `Exports each of the ${images.length} images with the same settings (downloaded as a zip).`;
}

/* ======================================================================== */
/* Change → validate → command → preview                                    */
/* ======================================================================== */

function changed({ immediate = false } = {}) {
  storageSet(STORAGE_LAST, state);
  renderMulti();
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(run, immediate ? 0 : DEBOUNCE_MS);
}

function clearFieldErrors() {
  $$(".field-error").forEach((e) => { e.textContent = ""; });
  $$(".invalid").forEach((e) => {
    if (!e.closest(".swatch")) e.classList.remove("invalid");
  });
}

function showFieldErrors(errors) {
  const unplaced = [];
  for (const { field, message } of errors) {
    const key = normalizeField(field);
    if (swatchInvalid[key] && /not an RGB tuple|not a valid RGB|RGBA|0-255|space|comma|empty color/.test(message)) continue;
    const slot = $(`[data-error-for="${key}"]`);
    if (slot && !isHidden(slot)) {
      slot.textContent = slot.textContent ? `${slot.textContent} ${message}` : message;
      $$(`[data-field="${key}"]`).forEach((el) => el.classList.add("invalid"));
    } else {
      unplaced.push(`${field}: ${message}`);
    }
  }
  if (unplaced.length) showError("Invalid settings", unplaced.join("\n"));
}

function normalizeField(field) {
  const f = String(field).split(".")[0];
  if (f === "bayer_x" || f === "bayer_y") return "bayer";
  if (f.startsWith("random_")) return "random";
  if (f === "palette_mode") return "palette";
  return f;
}

function isHidden(el) {
  for (let n = el; n; n = n.parentElement) if (n.hidden) return true;
  return false;
}

function setCommand(text, valid) {
  $("#command").textContent = text;
  $(".command").classList.toggle("invalid", !valid);
}

async function run() {
  const seq = ++requestSeq;
  inflight?.abort();
  const controller = new AbortController();
  inflight = controller;
  const signal = controller.signal;

  clearFieldErrors();

  if (clientErrors.size) {
    showFieldErrors([...clientErrors].map(([field, message]) => ({ field, message })));
    setCommand($("#command").textContent, false);
    setStatus("fix the highlighted fields");
    return;
  }

  try {
    // 1) Per-swatch colour checks (for inline errors) + server-side command.
    const colorChecks = [];
    if (state.palette_mode === "colors") colorChecks.push(api("/api/check-colors", { colors: state.palette }, { signal }).then((r) => ["palette", r]));
    if (state.recolor_enabled) colorChecks.push(api("/api/check-colors", { colors: state.recolor, allow_alpha: true }, { signal }).then((r) => ["recolor", r]));

    const cmdReq = api("/api/command", { session: session || "", params: state, image_index: activeIndex }, { signal });
    const [cmd, ...checks] = await Promise.all([cmdReq, ...colorChecks]);
    if (seq !== requestSeq) return;

    swatchInvalid.palette = swatchInvalid.recolor = false;
    for (const [key, r] of checks) {
      if (!r.ok) continue;
      editors[key].mark(r.data.colors);
      swatchInvalid[key] = r.data.colors.some((c) => !c.ok);
    }

    if (!cmd.ok) {
      const errs = cmd.data?.detail?.errors;
      if (errs) showFieldErrors(errs);
      else showError("Request failed", JSON.stringify(cmd.data?.detail ?? cmd.data, null, 2));
      setCommand($("#command").textContent, false);
      setStatus("invalid settings — not rendered");
      $("#canvas").classList.add("stale");
      return;
    }
    setCommand(cmd.data.command, true);
    if ($("#error-banner").dataset.kind === "Invalid settings") hideError();

    if (!session || !images.length) { setStatus(""); return; }

    // 2) Preview render.
    setStatus("rendering…", true);
    $("#canvas").classList.add("stale");
    const res = await api("/api/preview", { session, params: state, image_index: activeIndex }, { signal });
    if (seq !== requestSeq || res.status === 409) return;  // superseded

    if (!res.ok) {
      const d = res.data || {};
      if (d.detail?.errors) { showFieldErrors(d.detail.errors); setStatus("invalid settings"); }
      else if (d.error) {
        showError("didder exited with an error", d.error);
        setStatus("didder failed");
      } else {
        showError("Preview failed", typeof d.detail === "string" ? d.detail : JSON.stringify(d, null, 2));
        setStatus("preview failed");
        if (res.status === 404) { session = null; images = []; renderImageList(); }
      }
      return;
    }

    // Decode off-screen first, then swap, so the old frame stays until ready.
    const img = new Image();
    img.src = res.data.url;
    try { await img.decode(); } catch { if (seq === requestSeq) showError("Preview failed", "The rendered image could not be decoded."); return; }
    if (seq !== requestSeq) return;

    hideError({ onlyDidder: true });
    lastPreview = res.data;
    afterImage = img;
    $("#preview-canvas").dataset.src = res.data.url;
    if (beforeUrl !== res.data.source_url) {
      beforeUrl = res.data.source_url;
      const before = new Image();
      before.src = beforeUrl;
      before.decode().then(() => {
        if (beforeUrl === before.src || before.src.endsWith(beforeUrl)) { beforeImage = before; layout(); }
      }).catch(() => {});
    }
    $("#canvas").hidden = false;
    $("#empty").hidden = true;
    $("#canvas").classList.remove("stale");
    setCommand(res.data.command, true);
    renderScaleNote();
    layout();
    setStatus(`${res.data.width}×${res.data.height} · ${res.data.duration_ms} ms`);
  } catch (err) {
    if (err.name === "AbortError") return;
    if (seq !== requestSeq) return;
    showError("Network error", String(err));
    setStatus("offline?");
  }
}

function setStatus(text, busy = false) {
  const el = $("#status");
  el.textContent = text;
  el.classList.toggle("busy", busy);
}

function renderScaleNote() {
  const p = lastPreview;
  const note = $("#scale-note");
  if (!p) { note.hidden = true; return; }
  const pct = Math.round(p.preview_scale * 100);
  if (p.preview_scale >= 0.999) {
    note.hidden = true;
    return;
  }
  const factor = (1 / p.preview_scale).toFixed(p.preview_scale > 0.5 ? 1 : 0);
  note.hidden = false;
  note.textContent =
    `⚠ Preview is dithered from a ${pct}% downscale of the ${p.source_width}×${p.source_height} original. ` +
    `Dither patterns are the same size in pixels at full resolution, so in the export they will look about ` +
    `${factor}× finer relative to the image. Export to see the real result.`;
}

/* ======================================================================== */
/* Errors                                                                   */
/* ======================================================================== */

function showError(title, body) {
  $("#error-title").textContent = title;
  $("#error-body").textContent = body;
  $("#error-banner").hidden = false;
  $("#error-banner").dataset.kind = title;
}

function hideError({ onlyDidder = false } = {}) {
  const banner = $("#error-banner");
  if (onlyDidder && banner.dataset.kind && !/didder|Invalid|Preview/.test(banner.dataset.kind)) return;
  banner.hidden = true;
}

/* ======================================================================== */
/* Upload                                                                   */
/* ======================================================================== */

function wireUpload() {
  const zone = $("#dropzone");
  const input = $("#file-input");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { if (input.files.length) upload(input.files); input.value = ""; });

  // Whole-window drop target.
  let depth = 0;
  const overlay = $("#drop-overlay");
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  window.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; depth++; overlay.hidden = false; });
  window.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; if (--depth <= 0) { depth = 0; overlay.hidden = true; } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files);
  });
}

async function upload(fileList) {
  const files = [...fileList];
  const maxBytes = config.max_upload_mb * 1024 * 1024;
  const rejected = files.filter((f) => (f.type && !f.type.startsWith("image/")) || f.size > maxBytes);
  if (rejected.length) {
    showError("Some files were not uploaded", rejected.map((f) =>
      f.size > maxBytes ? `${f.name}: larger than ${config.max_upload_mb} MB` : `${f.name}: not an image (${f.type})`).join("\n"));
  }
  const ok = files.filter((f) => !rejected.includes(f));
  if (!ok.length) return;

  const fd = new FormData();
  ok.forEach((f) => fd.append("files", f, f.name));
  const zone = $("#dropzone");
  zone.classList.add("busy");
  setStatus(`uploading ${ok.length} file${ok.length > 1 ? "s" : ""}…`, true);
  try {
    const q = session ? `?session=${encodeURIComponent(session)}` : "";
    let res = await fetch(`/api/upload${q}`, { method: "POST", body: fd });
    if (res.status === 404 && session) {  // session expired server-side: start over
      session = null;
      images = [];
      res = await fetch("/api/upload", { method: "POST", body: fd });
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showError("Upload failed", typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data));
      setStatus("upload failed");
      return;
    }
    const firstNew = images.length ? data.images.length - data.added.length : 0;
    session = data.session;
    images = data.images;
    activeIndex = firstNew;
    hideError();
    renderImageList();
    changed({ immediate: true });
  } catch (err) {
    showError("Upload failed", String(err));
  } finally {
    zone.classList.remove("busy");
  }
}

function renderImageList() {
  const list = $("#image-list");
  list.replaceChildren(...images.map((img, i) => {
    const li = document.createElement("li");
    li.className = i === activeIndex ? "active" : "";
    li.title = "Preview this image";
    li.innerHTML = `<span class="name"></span><span class="dims"></span><button type="button" class="ghost" aria-label="Remove image">×</button>`;
    $(".name", li).textContent = img.name;
    $(".dims", li).textContent = `${img.width}×${img.height}`;
    li.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      activeIndex = i;
      renderImageList();
      changed({ immediate: true });
    });
    $("button", li).addEventListener("click", () => removeImage(i));
    return li;
  }));
  if (!images.length) {
    afterImage = null;
    beforeImage = null;
    beforeUrl = null;
    $("#canvas").hidden = true;
    $("#empty").hidden = false;
    $("#scale-note").hidden = true;
    lastPreview = null;
  }
  renderMulti();
}

async function removeImage(i) {
  const { ok, data } = await api(`/api/session/${session}/remove/${i}`, {}, { method: "POST" });
  if (!ok) { showError("Could not remove image", data?.detail || ""); return; }
  images = data.images;
  activeIndex = Math.min(activeIndex, Math.max(0, images.length - 1));
  renderImageList();
  changed({ immediate: true });
}

/* ======================================================================== */
/* Stage: integer-only zoom, before/after                                   */
/* ======================================================================== */

function wireStage() {
  $$("#zoom-mode button").forEach((b) => b.addEventListener("click", () => {
    zoomMode = b.dataset.zoom === "fit" ? "fit" : "manual";
    if (zoomMode === "manual") zoomFactor = 1;
    layout();
  }));
  $("#zoom-in").addEventListener("click", () => { zoomFactor = currentFactor() + 1; zoomMode = "manual"; layout(); });
  $("#zoom-out").addEventListener("click", () => { zoomFactor = Math.max(1, currentFactor() - 1); zoomMode = "manual"; layout(); });

  window.addEventListener("resize", layout);
  $("#stage").addEventListener("scroll", snapScroll, { passive: true });
  // In the stacked narrow layout the whole page scrolls, moving the stage.
  window.addEventListener("scroll", () => requestAnimationFrame(layout), { passive: true });
  // Re-layout when the device pixel ratio changes (browser zoom, moving screens).
  const watchDpr = () => {
    matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener("change", () => { layout(); watchDpr(); }, { once: true });
  };
  watchDpr();

  // Before/after
  const canvas = $("#canvas");
  const divider = $("#divider");
  $("#compare").addEventListener("change", (e) => {
    canvas.classList.toggle("comparing", e.target.checked);
    divider.hidden = !e.target.checked;
  });
  let dragging = false;
  const setSplit = (clientX) => {
    const r = canvas.getBoundingClientRect();
    const pct = Math.max(0, Math.min(100, ((clientX - r.left) / r.width) * 100));
    canvas.style.setProperty("--split", `${pct}%`);
  };
  canvas.addEventListener("pointerdown", (e) => {
    if (!canvas.classList.contains("comparing")) return;
    dragging = true;
    canvas.setPointerCapture(e.pointerId);
    setSplit(e.clientX);
  });
  canvas.addEventListener("pointermove", (e) => { if (dragging) setSplit(e.clientX); });
  canvas.addEventListener("pointerup", () => { dragging = false; });
  canvas.addEventListener("pointercancel", () => { dragging = false; });
}

function currentFactor() {
  return Number($("#canvas").dataset.factor || 1);
}

/**
 * Express devicePixelRatio as a fraction p/q (1.5 -> 3/2, 1.25 -> 5/4). A
 * length of n*q CSS px is then exactly n*p device px, so anything sized and
 * placed on that grid is whole in *both* CSS and device pixels.
 */
function dprFraction(dpr) {
  for (let q = 1; q <= 64; q++) {
    const p = Math.round(dpr * q);
    if (p > 0 && Math.abs(dpr * q - p) < 1e-3) return { p, q };
  }
  return { p: Math.round(dpr) || 1, q: 1 };
}

/**
 * Draw the preview so every image pixel is an exact k x k block of *device*
 * pixels, with k a positive integer.
 *
 * An <img> with image-rendering: pixelated is not enough on HiDPI screens: the
 * browser snaps the element's edges (on the CSS-px or device-px grid,
 * depending on the browser), and when the box isn't whole on that grid the two
 * edges round apart and a row or column of the dither is dropped or doubled.
 * Measured in Chromium at DPR 2: a 533-row preview painted as 532 rows.
 *
 * So the preview is a <canvas> whose backing store is exactly its device-pixel
 * size, drawn at integer scale with smoothing off, and whose CSS size and
 * position are whole in both CSS and device pixels (see dprFraction). Nothing
 * is left for the browser to round or resample. "Fit" picks the largest k that
 * fits; it never goes below 1 (the stage scrolls instead).
 */
function layout() {
  const canvas = $("#canvas");
  const stageEl = $("#stage");
  const after = $("#preview-canvas");
  const before = $("#before-canvas");
  if (!afterImage) return;
  const w = afterImage.naturalWidth;
  const h = afterImage.naturalHeight;
  if (!w || !h) return;

  const dpr = window.devicePixelRatio || 1;
  const { p, q } = dprFraction(dpr);
  const margin = 16;

  // When the preview overflows, the stage becomes a composited scroll layer.
  // If that layer's origin is on a fractional device pixel (e.g. a 381px
  // sidebar at DPR 1.25 = 476.25 device px), the whole layer gets resampled
  // no matter how carefully the canvas inside is placed. So first nudge the
  // stage itself onto the grid (by < q CSS px).
  stageEl.style.marginLeft = "0px";
  stageEl.style.marginTop = "0px";
  const raw = stageEl.getBoundingClientRect();
  const nudge = (v) => Math.ceil((v - 1e-6) / q) * q - v;
  stageEl.style.marginLeft = `${nudge(raw.left + stageEl.clientLeft)}px`;
  stageEl.style.marginTop = `${nudge(raw.top + stageEl.clientTop)}px`;

  const viewW = stageEl.clientWidth;
  const viewH = stageEl.clientHeight;
  const kMax = Math.max(1, Math.floor(MAX_CANVAS_EDGE / Math.max(w, h)));
  let k;
  if (zoomMode === "fit") {
    const availW = Math.max(1, viewW - 2 * margin) * dpr;
    const availH = Math.max(1, viewH - 2 * margin) * dpr;
    k = Math.max(1, Math.floor(Math.min(availW / w, availH / h)));
  } else {
    k = Math.max(1, Math.round(zoomFactor));
  }
  k = Math.min(k, kMax);
  zoomFactor = k;

  // Device-pixel size of the art, padded up to the p-grid so the CSS size is
  // a whole number of CSS px as well. The padding (< p device px) stays
  // transparent over the checkerboard.
  const devW = w * k;
  const devH = h * k;
  const boxW = Math.ceil(devW / p) * p;
  const boxH = Math.ceil(devH / p) * p;
  const cssW = (boxW / p) * q;
  const cssH = (boxH / p) * q;

  for (const c of [after, before]) {
    if (c.width !== boxW) c.width = boxW;
    if (c.height !== boxH) c.height = boxH;
  }
  canvas.style.width = `${cssW}px`;
  canvas.style.height = `${cssH}px`;
  canvas.dataset.factor = String(k);
  canvas.dataset.devW = String(devW);
  canvas.dataset.devH = String(devH);

  // Place the origin on the q-grid in viewport CSS px (hence whole device px).
  // `left`/`top` are relative to the stage's scrolled content, whose origin
  // (at scroll 0) may itself be fractional; scroll offsets are kept on the
  // q-grid separately by snapScroll().
  const stageRect = stageEl.getBoundingClientRect();
  const place = (origin, view, size) =>
    Math.round((origin + Math.max(margin, (view - size) / 2)) / q) * q - origin;
  const left = place(stageRect.left + stageEl.clientLeft, viewW, cssW);
  const top = place(stageRect.top + stageEl.clientTop, viewH, cssH);
  canvas.style.left = `${left}px`;
  canvas.style.top = `${top}px`;

  // Absolutely positioned content doesn't reserve a trailing margin in the
  // scroll area, so a 1px sizer does it.
  const sizer = $("#stage-sizer");
  sizer.style.left = `${left + cssW + margin - 1}px`;
  sizer.style.top = `${top + cssH + margin - 1}px`;

  const ctx = after.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, boxW, boxH);
  ctx.drawImage(afterImage, 0, 0, devW, devH);

  const bctx = before.getContext("2d");
  bctx.clearRect(0, 0, boxW, boxH);
  if (beforeImage) {
    // The original is a photo, not dither: smooth scaling is right for it.
    bctx.imageSmoothingEnabled = true;
    bctx.imageSmoothingQuality = "high";
    bctx.drawImage(beforeImage, 0, 0, devW, devH);
  }

  $("#zoom-label").textContent = `${k}×`;
  $("#zoom-label").title = dpr === 1
    ? `${k} screen pixels per image pixel`
    : `${k} device pixels per image pixel (devicePixelRatio ${dpr})`;
  $$("#zoom-mode button").forEach((b) => {
    const active = zoomMode === "fit" ? b.dataset.zoom === "fit" : (b.dataset.zoom === "1" && k === 1);
    b.classList.toggle("active", active);
  });
}

/**
 * Scrolling moves the canvas by the scroll offset, which may not be on the
 * q-grid (e.g. an odd number of CSS px at DPR 1.5). Once scrolling settles,
 * nudge the offset onto the grid so the preview is pixel-exact again.
 */
let scrollSnapTimer = null;
function snapScroll() {
  clearTimeout(scrollSnapTimer);
  scrollSnapTimer = setTimeout(() => {
    const stageEl = $("#stage");
    const { q } = dprFraction(window.devicePixelRatio || 1);
    const sx = Math.round(stageEl.scrollLeft / q) * q;
    const sy = Math.round(stageEl.scrollTop / q) * q;
    if (sx !== stageEl.scrollLeft || sy !== stageEl.scrollTop) stageEl.scrollTo(sx, sy);
  }, 120);
}

/* ======================================================================== */
/* Export                                                                   */
/* ======================================================================== */

async function doExport() {
  if (clientErrors.size) { run(); return; }
  const btn = $("#export-btn");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Exporting…";
  setStatus("exporting at full resolution…", true);
  try {
    const res = await api("/api/export", { session, params: state, image_index: activeIndex });
    const d = res.data || {};
    if (!res.ok) {
      if (d.detail?.errors) {
        showFieldErrors(d.detail.errors);
        if (d.detail.message) showError("Cannot export", d.detail.message);
      } else if (d.error) showError("didder exited with an error", d.error);
      else showError("Export failed", typeof d.detail === "string" ? d.detail : JSON.stringify(d, null, 2));
      setStatus("export failed");
      return;
    }
    const target = d.zip_url || d.files[0].url;
    triggerDownload(target);

    const list = $("#export-files");
    list.replaceChildren(...d.files.map((f) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = f.url;
      a.textContent = f.name;
      a.setAttribute("download", f.name);
      li.append(a, document.createTextNode(` — ${formatBytes(f.bytes)}`));
      return li;
    }));
    if (d.zip_url) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = d.zip_url;
      a.textContent = "all files (zip)";
      a.setAttribute("download", "dithered.zip");
      li.append(a);
      list.prepend(li);
    }
    $("#export-result").hidden = false;
    setStatus(`exported in ${d.duration_ms} ms`);
  } catch (err) {
    showError("Export failed", String(err));
  } finally {
    btn.textContent = label;
    renderMulti();
  }
}

function triggerDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(2)} MB`;
}

async function copyCommand() {
  const text = $("#command").textContent;
  const btn = $("#copy-cmd");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API needs a secure context; 127.0.0.1 qualifies, but fall back anyway.
    const range = document.createRange();
    range.selectNodeContents($("#command"));
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand("copy");
    sel.removeAllRanges();
  }
  btn.textContent = "Copied ✓";
  setTimeout(() => { btn.textContent = "Copy"; }, 1200);
}

/* ======================================================================== */
/* Saved presets (localStorage)                                             */
/* ======================================================================== */

function savePreset() {
  const name = $("#preset-name").value.trim();
  if (!name) { $("#preset-name").focus(); return; }
  const presets = storageGet(STORAGE_PRESETS, {});
  if (presets[name] && !confirm(`Overwrite preset "${name}"?`)) return;
  presets[name] = { saved: new Date().toISOString(), state: clone(state) };
  storageSet(STORAGE_PRESETS, presets);
  $("#preset-name").value = "";
  renderPresetList();
}

function renderPresetList() {
  const presets = storageGet(STORAGE_PRESETS, {});
  const names = Object.keys(presets).sort((a, b) => a.localeCompare(b));
  const list = $("#preset-list");
  if (!names.length) {
    const li = document.createElement("li");
    li.className = "empty-note";
    li.textContent = "No saved presets yet. They are stored in this browser.";
    list.replaceChildren(li);
    return;
  }
  list.replaceChildren(...names.map((name) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="name"></span><button type="button" class="secondary">Load</button><button type="button" class="ghost" aria-label="Delete preset">×</button>`;
    $(".name", li).textContent = name;
    $(".name", li).title = `Saved ${new Date(presets[name].saved).toLocaleString()}`;
    const [load, del] = $$("button", li);
    load.addEventListener("click", () => {
      state = sanitizeState(presets[name].state);
      if (state.threads == null) state.threads = config.threads_default;
      if (!config.mmcq_supported && state.palette_mode === "mmcq") state.palette_mode = "colors";
      clientErrors.clear();
      renderAll();
      renderImageList();
      changed({ immediate: true });
    });
    del.addEventListener("click", () => {
      if (!confirm(`Delete preset "${name}"?`)) return;
      const all = storageGet(STORAGE_PRESETS, {});
      delete all[name];
      storageSet(STORAGE_PRESETS, all);
      renderPresetList();
    });
    return li;
  }));
}

init().catch((err) => {
  showError("Failed to start", String(err));
  console.error(err);
});
