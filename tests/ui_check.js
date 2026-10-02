// A headless check of app/ui/app.js with a stubbed backend (run by test_ui.py with Node,
// no browser): the recorder list shows "<model> — <owner>", a recorder's folders come from
// its listing (labels as text, never markup), the export menu comes from its model, and
// selections hand the backend back the exact folder ids and recording numbers, even with
// ":" or "|" in them. The EVP Library's right-click menu runs the same code as the folder tools
// (New folder, Rename, Delete, Move to…), with the same enabled state; a recording row adds
// Rename… (and F2), which sends the ids of that recording's files in the folder shown. EVP clips: the
// player's Export clips and each mark's Save clip, and the library's Export clips on a recording or a
// folder (a background job with progress, Cancel and a summary with Open folder). Prints "ok" or throws.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

// ---- a small DOM: enough of it for app.js ------------------------------------------
class ClassList {
  constructor(el) { this.el = el; }
  get set() { return new Set(this.el.className.split(/\s+/).filter(Boolean)); }
  contains(c) { return this.set.has(c); }
  add(c) { const s = this.set; s.add(c); this.el.className = [...s].join(" "); }
  remove(c) { const s = this.set; s.delete(c); this.el.className = [...s].join(" "); }
  toggle(c, on) { if (on === undefined) on = !this.contains(c); on ? this.add(c) : this.remove(c); return on; }
}

class Element {
  constructor(tag, id) {
    this.tagName = String(tag).toUpperCase(); this.id = id || ""; this.children = []; this.parentNode = null;
    this.className = ""; this.classList = new ClassList(this); this.style = {}; this.dataset = {};
    this.hidden = false; this.disabled = false; this.title = ""; this.value = ""; this.checked = false;
    this._text = ""; this.options = this.children; this.cells = this.children;
  }
  get textContent() { return this._text + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  set textContent(t) { this.children.length = 0; this._text = String(t); }
  set innerHTML(h) { this.children.length = 0; this._text = ""; this._html = h; }
  get innerHTML() { return this._html || ""; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get nextSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; }
  get isConnected() { return true; }
  appendChild(c) { return this.insertBefore(c, null); }
  append(...cs) { for (const c of cs) this.appendChild(c); }
  replaceChildren(...cs) { this.children.length = 0; this._text = ""; this.append(...cs); }
  insertBefore(c, ref) {
    if (typeof c !== "string") { if (c.parentNode) c.remove(); c.parentNode = this; }
    const at = ref ? this.children.indexOf(ref) : -1;
    at < 0 ? this.children.push(c) : this.children.splice(at, 0, c);
    return c;
  }
  remove() {
    if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
    this.parentNode = null;
  }
  addEventListener(type, fn, capture) { listen(this, type, fn, capture); }
  removeEventListener() {}
  setAttribute(k, v) { this[k] = v; }
  getAttribute(k) { return this[k]; }
  focus() { document.activeElement = this; } select() {} scrollIntoView() {}
  contains(x) { for (; x; x = x.parentNode) if (x === this) return true; return false; }
  // Simple selectors only ("tr.lib-folder", "#id", ".cls"), comma-separated; others never match.
  closest(sel) {
    const one = (el, s) => {
      const m = /^([a-z]*)((?:\.[\w-]+)*)(?:#([\w-]+))?$/i.exec(s.trim());
      if (!m) return false;
      return (!m[1] || el.tagName === m[1].toUpperCase()) && (!m[3] || el.id === m[3]) &&
             m[2].split(".").filter(Boolean).every((c) => el.classList.contains(c));
    };
    for (let el = this; el && el instanceof Element; el = el.parentNode) if (sel.split(",").some((s) => one(el, s))) return el;
    return null;
  }
  getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 80 }; }
  querySelectorAll() { return []; }
  querySelector(sel) {
    const m = /option\[value="(.*)"\]/.exec(sel);
    return m ? this.children.find((c) => c.value === m[1]) || null : null;
  }
  get clientWidth() { return 800; }
}

// Event listeners: capture ones first, stopPropagation honoured (enough for one target).
const listeners = new Map();
function listen(target, type, fn, capture) {
  if (!listeners.has(target)) listeners.set(target, []);
  const l = { type, fn, capture: !!(capture === true || (capture && capture.capture)) }, all = listeners.get(target);
  if (!all.some((x) => x.type === l.type && x.fn === l.fn && x.capture === l.capture)) all.push(l);   // as the DOM: once
}
function fire(targets, type, props = {}) {
  let stopped = false;
  const e = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
              stopPropagation() { stopped = true; }, ...props };
  const all = targets.flatMap((t) => (listeners.get(t) || []).filter((l) => l.type === type));
  for (const l of [...all.filter((x) => x.capture), ...all.filter((x) => !x.capture)]) { if (stopped) break; l.fn(e); }
  return e;
}

const byId = new Map();
const document = {
  getElementById(id) { if (!byId.has(id)) byId.set(id, new Element(id === "format" ? "select" : "div", id)); return byId.get(id); },
  createElement(tag) { return new Element(tag); },
  createTextNode(t) { return String(t); },                 // text: a plain string child
  createDocumentFragment() { return new Element("#fragment"); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener(type, fn, capture) { listen(document, type, fn, capture); },
  activeElement: null,
  body: new Element("body"),
};
// The format menu as index.html ships it.
const format = document.getElementById("format");
for (const [value, text] of [["dvf", ".dvf (Sony original)"], ["wav", "WAV"]]) {
  const o = document.createElement("option"); o.value = value; o.textContent = text; format.appendChild(o);
}
format.value = "dvf";

const anything = new Proxy(function () {}, {        // WaveSurfer and its plugins: accepts every call
  get: (t, k) => (k === "then" ? undefined : anything),
  apply: () => anything,
});

// ---- the stubbed backend -----------------------------------------------------------
const calls = [];
// capabilities().enhance_spec: openevp.enhance.spec() (test_enhance.py checks that the two agree).
const ENHANCE_SPEC = {"boost_max": 24, "q": 0.7071067811865475, "rumble": 120.0, "voice": [300.0, 3400.0], "hiss": 5000.0, "hiss_min_rate": 12000, "hum_harmonics": 4, "hum_width": 5.0, "leveler": {"light": {"threshold": -24.0, "knee": 12.0, "ratio": 2.0, "attack": 0.01, "release": 0.25}, "medium": {"threshold": -32.0, "knee": 10.0, "ratio": 4.0, "attack": 0.005, "release": 0.25}, "strong": {"threshold": -42.0, "knee": 6.0, "ratio": 8.0, "attack": 0.003, "release": 0.2}}, "limit_range": 64.0, "limit_knee": 0.8, "limit_points": 32769};
const BETA = "beta#1";
const listing = {
  [BETA]: { ok: true, model: "Fake Beta", model_id: "fake-beta", playable: false, play_reason: "no decoder",
            formats: [{ value: "fk2", label: ".fk2 (Fake Beta original)", available: true, reason: null },
                      { value: "wav", label: "WAV", available: false, reason: "OpenEVP cannot convert them" }],
            folders: [{ id: "a:b|c", label: "<b>Folder</b> one", recordings: [
                         { number: "rec:1", label: "a_b-rec:1", recorded: "", seconds: null, owner: null, problem: null },
                         { number: 7, label: "a_b-007", recorded: "x", seconds: 2, owner: null, problem: null }] },
                      { id: "7", label: "Seven", recordings: [
                         { number: "7", label: "7-7", recorded: "", seconds: null, owner: null, problem: null }] }] },
  "1-4@7": { ok: true, model: "Sony ICD-ST25", model_id: "sony-icd-st25", playable: true, play_reason: null,
             formats: [{ value: "dvf", label: ".dvf (Sony original)", available: true, reason: null },
                       { value: "wav", label: "WAV", available: true, reason: null }],
             folders: "ABCDE".split("").map((l) => ({ id: l, label: `Folder ${l}`, recordings: l === "A" ? [
               { number: 1, label: "A-001", recorded: "2029-05-23 19:54:04", seconds: 1.3, owner: "Casey", problem: null }] : [] })) },
  // An ICD-ST10 (discovered as an ST25, relabelled once opened), in a build without the LPEC ST decoder:
  // the recorder plays (its LP recordings would), but its LPEC ST recording says why it can't, per row.
  // (With the decoder it plays: see below.)
  "2-1@3": { ok: true, model: "Sony ICD-ST10", model_id: "sony-icd-st10", playable: true, play_reason: null,
             formats: [{ value: "dvf", label: ".dvf (Sony original)", available: true, reason: null },
                       { value: "wav", label: "WAV", available: true, reason: null }],
             folders: "ABCDE".split("").map((l) => ({ id: l, label: `Folder ${l}`, recordings: l === "A" ? [
               { number: 1, label: "A-001", recorded: "undated", seconds: 20.1, owner: "", problem: null,
                 play_problem: "LPEC ST (ICD-ST10) playback is not included in this build" }] : [] })) },
};
const api = {
  capabilities: async () => ({ models: ["Sony ICD-ST25"], wav: true, wav_status: null, version: "0.0", marks: true, marks_read_only: false,
                               enhance_spec: ENHANCE_SPEC,
                               store_problems: [], formats: { dvf: { playable: true }, wav: { playable: true } } }),
  default_destination: async () => "C:\\save",
  check_update: async () => ({ ok: true, available: false, current: "0.0" }),
  list_library: async () => ({ ok: true, folder: "C:\\save", scan_id: 1, exists: false, truncated: false,
                               indexing: false, pending: 0, files: [], folders: [] }),
  devices: async () => ({ ok: true, problems: [{ error: "libusb_init failed", advice: "Unplug it." }], devices: [
    { id: "1-4@7", model_id: "sony-icd-st25", model: "Sony ICD-ST25", port: "port 1-4", state: "ready", message: "", owner: "Casey" },
    { id: "setup:X", model_id: "sony-icd-st25", model: "Sony ICD-ST25", port: "", state: "needs_driver", message: "m", owner: "" },
    { id: BETA, model_id: "fake-beta", model: "Fake Beta", port: "vol", state: "ready", message: "", owner: "" },
    { id: "2-1@3", model_id: "sony-icd-st25", model: "Sony ICD-ST25", port: "port 2-1", state: "ready", message: "", owner: "" },
    { id: "gone", model_id: "fake-beta", model: "Fake Beta", port: "", state: "ready", message: "", owner: "" }] }),
  recordings: async (id) => listing[id],
  export: async (...args) => { calls.push(["export", ...args]); return { ok: true, job: args[4] }; },
  audio: async (...args) => { calls.push(["audio", ...args]); return { ok: false, error: "stub" }; },
};

// ---- a fake Web Audio: nodes that remember their params and connections -------------------
const audioContexts = [];
class FakeParam { constructor(v) { this.value = v; } }
class FakeNode {
  constructor(ctx, kind) { this.kind = kind; this.outs = []; ctx.nodes.push(this); }
  connect(n) { this.outs.push(n); return n; }
  disconnect() { this.outs = []; }
}
class FakeAudioContext {
  constructor() { this.state = "suspended"; this.nodes = []; this.resumes = 0; this.sources = 0;
                  this.destination = new FakeNode(this, "destination"); audioContexts.push(this); }
  resume() { this.resumes++; this.state = "running"; return Promise.resolve(); }
  createMediaElementSource(m) { this.sources++; const n = new FakeNode(this, "source"); n.media = m; return n; }
  createBiquadFilter() { const n = new FakeNode(this, "biquad"); n.type = "lowpass"; n.frequency = new FakeParam(350); n.Q = new FakeParam(1); return n; }
  createDynamicsCompressor() {
    const n = new FakeNode(this, "compressor");
    for (const k of ["threshold", "knee", "ratio", "attack", "release"]) n[k] = new FakeParam(0);
    return n;
  }
  createGain() { const n = new FakeNode(this, "gain"); n.gain = new FakeParam(1); return n; }
  createWaveShaper() { const n = new FakeNode(this, "shaper"); n.curve = null; return n; }
}
// The chain from the media element to the speakers, as [kind, ...its settings].
function audioChain(ctx) {
  const out = [];
  let n = ctx.nodes.find((x) => x.kind === "source");
  for (let guard = 0; n && guard < 50; guard++) {
    if (n.outs.length !== 1) return [...out, `${n.kind} has ${n.outs.length} outputs`];
    n = n.outs[0];
    if (n.kind === "destination") return out;
    if (n.kind === "biquad") out.push([n.type, n.frequency.value, Math.round(n.Q.value * 1000) / 1000]);
    else if (n.kind === "gain") out.push(["gain", Math.round(n.gain.value * 10000) / 10000]);
    else if (n.kind === "compressor") out.push(["compressor", n.threshold.value, n.knee.value, n.ratio.value, n.attack.value, n.release.value]);
    else out.push(["shaper", n.curve.length]);
  }
  return out;
}

let ready = null;
const window = {
  AudioContext: FakeAudioContext,
  pywebview: { api },
  addEventListener(name, fn, capture) { if (name === "pywebviewready") ready = fn; else listen(window, name, fn, capture); },
  innerWidth: 1000, innerHeight: 700,
  localStorage: { getItem: () => null, setItem() {} },
};
const context = { window, document, WaveSurfer: anything, console, getComputedStyle: () => ({ getPropertyValue: () => "" }),
                  setTimeout: () => 0, clearTimeout() {}, requestAnimationFrame: () => 0, localStorage: window.localStorage,
                  Map, Set, JSON, Promise, Number, String, Math, Object, Array, RegExp };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "app", "ui", "notes.js"), "utf8"), context);   // as index.html loads it
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "app", "ui", "app.js"), "utf8"), context);

const $ = (id) => document.getElementById(id);
const texts = (el) => el.children.map((c) => (typeof c === "string" ? c : c.textContent));

(async () => {
  await ready();
  await new Promise((r) => setImmediate(r));
  // The recorder list: "<model> — <owner>", a recorder needing its driver, one without an owner.
  const names = $("devices").children.map((box) => box.children[0].textContent);
  assert.deepStrictEqual(names, ["Sony ICD-ST25 — Casey", "Sony ICD-ST25 — needs setup", "Fake Beta #3 (vol)",
                                 "Sony ICD-ST25 #4 (port 2-1)", "Fake Beta #5"]);   // the location as the model says it
  assert.strictEqual($("banner-text").textContent, "libusb_init failed Unplug it.");      // a discovery problem, said once
  // Then the whole listing fails: that error is shown too (not hidden by the problem shown before);
  // and once everything works again the banner is cleared.
  const devices = api.devices;
  api.devices = async () => ({ ok: false, error: "listing failed", advice: "Try again." });
  await context.poll();
  assert.strictEqual($("banner-text").textContent, "listing failed Try again.");
  $("banner-text").textContent = "something else";
  await context.poll();
  assert.strictEqual($("banner-text").textContent, "something else");                     // not re-stamped
  api.devices = async () => ({ ...(await devices()), problems: [] });
  await context.poll();
  assert.ok($("banner").hidden);
  api.devices = devices;
  await context.poll();

  // The ST25 looks as it always has: Folder A..E, ".dvf (Sony original)" then WAV.
  await context.openDevice("1-4@7");
  const st25 = $("devices").children[0];
  assert.deepStrictEqual(st25.children.slice(1).map((row) => texts(row)), "ABCDE".split("").map((l) => [`Folder ${l}`, l === "A" ? "1" : "0"]));
  assert.deepStrictEqual(texts(format), [".dvf (Sony original)", "WAV"]);
  assert.deepStrictEqual(format.children.map((o) => o.value), ["dvf", "wav"]);
  const row = $("rows").children[0];
  assert.deepStrictEqual(texts(row).slice(1), ["001", "2029-05-23 19:54:04", "0:01", ""]);
  assert.ok($("device-table").classList.contains("playable"));

  // A recorder with odd ids: labels are text (no markup), the menu is its model's.
  await context.openDevice(BETA);
  const beta = $("devices").children[2];
  assert.deepStrictEqual(beta.children.slice(1).map((r) => texts(r)), [["<b>Folder</b> one", "2"], ["Seven", "1"]]);
  assert.ok(!beta.children[1].innerHTML, "folder rows are built from text, not HTML");
  assert.deepStrictEqual(texts(format), [".fk2 (Fake Beta original)", "WAV (unavailable)"]);
  assert.deepStrictEqual(format.children.map((o) => o.disabled), [false, true]);
  assert.strictEqual(format.children[1].title, "OpenEVP cannot convert them.");
  assert.strictEqual(format.value, "fk2");
  assert.ok(!$("device-table").classList.contains("playable"));
  assert.deepStrictEqual($("rows").children.map((r) => texts(r).slice(1, 4)), [["rec:1", "", ""], ["007", "x", "0:02"]]);

  // Select both recordings of the folder "a:b|c" and one of "7" (number "7", not 7); export.
  $("all").checked = true; $("all").onclick();
  vm.runInContext(`S.folder = "7"; renderMain();`, context);   // the folder "7" (a click on its row)
  $("rows").children[0].children[0].children[0].checked = true;
  $("rows").children[0].children[0].children[0].onclick({ stopPropagation() {} });
  assert.strictEqual($("export").textContent, "Export 3 selected");
  $("rows").children[0].onclick && $("rows").children[0].onclick();        // not playable: no click handler
  await $("export").onclick();
  const [, device, items, fmt] = calls.find((c) => c[0] === "export");
  assert.deepStrictEqual([device, fmt], [BETA, "fk2"]);
  assert.strictEqual(JSON.stringify(items),               // (the page's objects: compared as JSON)
                     JSON.stringify([{ folder: "a:b|c", number: "rec:1" }, { folder: "a:b|c", number: 7 },
                                     { folder: "7", number: "7" }]));
  assert.ok(!calls.some((c) => c[0] === "audio"));
  // The export finishes (the stub never says so): nothing is running any more.
  window.onBackendEvent("export-done", { job: vm.runInContext("S.job", context), saved: 3, skipped: 0, notes: [], dest: "D:/save" });
  assert.ok(!vm.runInContext("S.exporting", context));

  // An ICD-ST10 in a build without the LPEC ST decoder: its LPEC ST recording can be selected and
  // exported, not played; the reason is said plainly on that row (note, tooltip), never as an error.
  await context.openDevice("2-1@3");
  assert.deepStrictEqual(texts(format), [".dvf (Sony original)", "WAV"]);
  assert.ok($("device-table").classList.contains("playable"));
  assert.strictEqual($("status").textContent, "");
  assert.strictEqual($("banner-text").textContent, "", "not an error");
  assert.strictEqual($("devices").children[3].children[0].textContent, "Sony ICD-ST10 #4 (port 2-1)");  // relabelled at once
  const st10row = $("rows").children[0];
  assert.deepStrictEqual(texts(st10row).slice(1),
                         ["001", "undated", "0:20", "LPEC ST (ICD-ST10) playback is not included in this build."]);
  assert.ok(!st10row.onclick, "not playable: no click handler");
  assert.ok(st10row.classList.contains("unplayable"));
  assert.strictEqual(st10row.title, "LPEC ST (ICD-ST10) playback is not included in this build.");
  assert.ok(!st10row.children[0].children[0].disabled, "it can be selected for export");
  assert.ok(!calls.some((c) => c[0] === "audio"));

  // The same ICD-ST10 with the LPEC ST decoder, holding recordings in three modes, in a build
  // without the LPEC SP tables: LPEC ST and LP play, LPEC SP says why it can't (per recording, not
  // per recorder), and all three can be exported.
  const SP = "the WAV decoder could not be loaded: this build does not include the LPEC SP table data";
  listing["2-1@3"] = { ...listing["2-1@3"],
                       folders: "ABCDE".split("").map((l) => ({ id: l, label: `Folder ${l}`, recordings: l === "A" ? [
                         { number: 1, label: "A-001", recorded: "undated", seconds: 20.1, owner: "", problem: null, play_problem: null },
                         { number: 2, label: "A-002", recorded: "undated", seconds: 9.1, owner: "", problem: null, play_problem: null },
                         { number: 3, label: "A-003", recorded: "undated", seconds: 8.4, owner: "", problem: null, play_problem: SP },
                       ] : [] })) };
  vm.runInContext("S.device = null; S.folders = [];", context);
  $("status").textContent = "";
  await context.openDevice("2-1@3");
  assert.deepStrictEqual(texts(format), [".dvf (Sony original)", "WAV"]);
  assert.ok($("device-table").classList.contains("playable"));
  assert.strictEqual($("status").textContent, "");
  const [st10st, st10lp, st10sp] = $("rows").children;
  assert.ok(st10st.onclick && st10lp.onclick, "LPEC ST and LP: a click plays them");
  assert.ok(!st10sp.onclick, "LPEC SP: no click handler");
  assert.strictEqual(st10sp.title, "The WAV decoder could not be loaded: this build does not include the LPEC SP table data.");
  assert.strictEqual(texts(st10sp)[4], "The WAV decoder could not be loaded: this build does not include the LPEC SP table data.");
  assert.strictEqual(texts(st10lp)[4], "");
  assert.ok(!st10sp.children[0].children[0].disabled, "it can be selected for export");
  await st10st.onclick();
  await new Promise((r) => setImmediate(r));
  await st10lp.onclick();
  await new Promise((r) => setImmediate(r));
  assert.deepStrictEqual(calls.filter((c) => c[0] === "audio").map((c) => c.slice(1)),
                         [["2-1@3", "A", 1], ["2-1@3", "A", 2]]);

  // The waveform: drawn from the audio itself up to FULL_DETAIL_SAMPLES samples over all channels
  // (30 minutes of 8 kHz mono, about 2.7 minutes of 44.1 kHz stereo), with a zoom down to one
  // pixel per sample at the file's own rate; longer files are drawn from the server's peaks.
  const load = async (r) => {
    const seq = vm.runInContext("++S.playSeq", context);
    await context.loadIntoPlayer(seq, "x", { url: "http://x/a.wav", peaks: [0.5], ...r }, false);
    return vm.runInContext("S.zoomMax", context);
  };
  assert.strictEqual(await load({ duration: 30 * 60, rate: 8000, channels: 1 }), 8000);     // ICD-ST25: as before
  assert.strictEqual(await load({ duration: 30 * 60 + 1, rate: 8000, channels: 1 }), 400);
  assert.strictEqual(await load({ duration: 30 * 60, rate: 8000 }), 8000);                  // channels unknown: mono
  assert.strictEqual(await load({ duration: 160, rate: 44100, channels: 2 }), 44100);       // ICD-ST10, short
  assert.strictEqual(await load({ duration: 170, rate: 44100, channels: 2 }), 400);         // 3 minutes: peaks
  assert.strictEqual(await load({ duration: 320, rate: 44100, channels: 1 }), 44100);       // the same samples in mono
  assert.strictEqual(await load({ duration: 92 * 60, rate: 44100, channels: 2 }), 400);     // the longest ST10 file
  assert.ok(context.fullDetail({ duration: 160, rate: 44100, channels: 2 }));
  assert.ok(!context.fullDetail({ duration: 160, rate: 44100, channels: 3 }));
  // The slider is logarithmic: its ends are "fit" and one pixel per sample, every step the same factor,
  // at 8000 px/s as at 44100; slider and zoom convert back and forth exactly.
  const near = (a, b) => Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(b));
  for (const max of [400, 8000, 44100]) {
    assert.strictEqual(context.zoomPx(0, max), 0);
    assert.ok(near(context.zoomPx(1000, max), max));
    assert.ok(near(context.zoomPx(500, max) / context.zoomPx(250, max), context.zoomPx(750, max) / context.zoomPx(500, max)));
    for (const px of [11, 57, 400, max]) assert.ok(near(context.zoomPx(context.zoomSlider(px, max), max), px), `${px} at ${max}`);
  }
  // A deeper zoom than the next file allows becomes that file's deepest; the slider follows.
  vm.runInContext("S.zoomPx = 20000;", context);
  assert.strictEqual(await load({ duration: 30 * 60, rate: 8000, channels: 1 }), 8000);
  assert.strictEqual(vm.runInContext("S.zoomPx", context), 8000);
  assert.strictEqual(Number($("zoom").value), 1000);
  vm.runInContext("S.zoomPx = 0;", context);
  context.unloadPlayer();                                          // the checks below start with an empty player
  // ---- no recorder: the prompt names the supported models from capabilities(), not a fixed one ----
  api.devices = async () => ({ ok: true, problems: [], devices: [] });
  await context.poll();
  assert.strictEqual($("devices").children[0].textContent, "No recorder connected. Plug in a supported recorder (Sony ICD-ST25).");
  assert.ok(!$("devices").innerHTML.includes("ICD-ST25"), "model names are text, not markup");
  vm.runInContext(`S.caps.models = ["Sony ICD-ST25", "Other X1"]; renderMain();`, context);
  assert.strictEqual($("empty").textContent, "Plug in a supported recorder (Sony ICD-ST25, Other X1) and select it on the left.");
  vm.runInContext(`S.caps.models = []; renderMain();`, context);
  assert.strictEqual($("empty").textContent, "Plug in a supported recorder and select it on the left.");
  const html = fs.readFileSync(path.join(__dirname, "..", "app", "ui", "index.html"), "utf8");
  assert.ok(html.includes('<p id="empty" class="muted">Plug in a supported recorder and select it on the left.</p>'));
  assert.ok(html.includes('supported by Sony or Panasonic. "Sony", "ICD-ST25", "ICD-ST10" and "Digital Voice Editor" are ' +
                          'trademarks of Sony Corporation. "Panasonic" and "RR-DR60" are trademarks of Panasonic Corporation.'));

  // ---- the EVP Library: "New folder", and the right-click menu ----
  assert.ok(/id="library-new"[^>]*>New folder<\/button>/.test(html) && !/new investigation/i.test(html));
  assert.ok(html.includes('id="context-menu"'));
  const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); context.renderLibrary(); };
  const libFile = (id, name, folder, fp) => ({ id, name, folder_id: folder, type: "wav", fp, investigation: "", seconds: 3,
                                               marks: { A: 0, B: 0, C: 0 }, reviewed: false, notes: "", error: null });
  const libFolders = [{ id: "root", name: "", rel: [], parent: null }, { id: "f1", name: "Old Mill", rel: ["Old Mill"], parent: "root" },
                      { id: "f2", name: "Night 2", rel: ["Night 2"], parent: "root" }];
  // A library with no recordings at all: said without naming a recorder's formats.
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 2, exists: true, truncated: false, indexing: false,
                                    pending: 0, files: [], folders: [libFolders[0]] });
  context.showLibrary();
  await settle();
  assert.strictEqual($("empty").textContent, "No recordings in C:\\save yet.");
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 3, exists: true, truncated: false, indexing: false,
                                    pending: 0, folders: libFolders,
                                    files: [libFile("r1", "a.wav", "root", "fp1"), libFile("r2", "b.wav", "root", "fp2"),
                                            libFile("r3", "c.wav", "root", "fp3")] });
  const ops = [];
  api.play_library = async (id) => { ops.push(["play", id]); return { ok: false, error: "stub" }; };
  api.move_files = async (ids, to) => { ops.push(["move", ids, to]); return { ok: true, ids: {} }; };
  // Show / Open in File Explorer: the backend gets an id, never a path.
  const explored = [];
  let exploreAnswer = { ok: true };
  api.show_library_file = async (...args) => { explored.push(["file", ...args]); return exploreAnswer; };
  api.open_library_folder = async (...args) => { explored.push(["folder", ...args]); return exploreAnswer; };
  await context.loadLibrary();
  await settle();
  const L = vm.runInContext("S.lib", context);
  const menu = $("context-menu"), dialogShown = () => !$("folder-dialog").hidden;
  const rowsNow = () => $("library-rows").children;
  const folderRow = (id) => rowsNow().find((r) => r.folderId === id);
  const recRow = (id) => rowsNow().find((r) => r.group && r.group.main.id === id);
  const rightClick = (target) => fire([$("list-scroll")], "contextmenu", { target, clientX: 50, clientY: 60 });
  const menuItems = () => menu.children.map((b) => [b.textContent, b.disabled]);
  const choose = (label) => menu.children.find((b) => b.textContent === label).onclick();
  const press = (k) => fire([document], "keydown", { key: k, target: document.activeElement || document.body });
  assert.deepStrictEqual(rowsNow().map((r) => r.folderId || r.group.main.id), ["f2", "f1", "r1", "r2", "r3"]);
  menu.hidden = true;                                               // as index.html has it

  // Empty space: New folder, the same dialog as the button (an empty name box).
  let e = rightClick($("empty"));
  assert.ok(e.defaultPrevented, "no browser menu over the library");
  assert.ok(!menu.hidden);
  assert.deepStrictEqual(menuItems(), [["New folder…", false]]);
  assert.strictEqual(menu.children[0].getAttribute("role"), "menuitem");
  assert.strictEqual(document.activeElement, menu.children[0]);
  choose("New folder…");
  assert.ok(menu.hidden && dialogShown());
  assert.strictEqual($("folder-dialog-title").textContent, "New folder");
  assert.strictEqual($("folder-dialog-name").value, "");
  assert.ok(!$("folder-dialog-name").hidden);
  context.closeFolderDialog();
  $("library-new").onclick();                                      // the button: the very same dialog
  assert.strictEqual($("folder-dialog-title").textContent, "New folder");
  context.closeFolderDialog();

  // A second OpenEVP window (read-only store) can look but not change folders.
  vm.runInContext(`S.caps.marks_read_only = true; renderLibraryBar();`, context);
  assert.ok($("library-new").disabled && /can't be changed right now/.test($("library-tools").title));
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Open in File Explorer", false], ["Rename…", true], ["Delete…", true], ["Export clips", true]]);
  assert.strictEqual(menu.children[4].title, vm.runInContext("readOnlyTip()", context));   // says why
  press("Escape");
  vm.runInContext(`S.caps.marks_read_only = false; renderLibraryBar();`, context);
  assert.ok(!$("library-new").disabled && $("library-tools").title === "");

  // A folder row: Open / Rename / Delete; it becomes the selected folder; keys move and choose.
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Open in File Explorer", false], ["Rename…", false], ["Delete…", false], ["Export clips", false]]);
  assert.strictEqual(L.selFolder, "f1");
  assert.strictEqual(document.activeElement.textContent, "Open");
  press("ArrowUp");
  assert.strictEqual(document.activeElement.textContent, "Export clips");  // wraps round
  press("ArrowUp");
  assert.strictEqual(document.activeElement.textContent, "Delete…");
  press("ArrowDown"); press("ArrowDown"); press("ArrowDown");
  assert.strictEqual(document.activeElement.textContent, "Open in File Explorer");
  press("ArrowDown");
  assert.strictEqual(document.activeElement.textContent, "Rename…");
  press("Enter");
  assert.ok(menu.hidden);
  assert.strictEqual($("folder-dialog-title").textContent, "Rename “Old Mill”");
  context.closeFolderDialog();
  rightClick(folderRow("f2").cells[1]);
  choose("Open");
  assert.strictEqual(L.folderId, "f2");
  context.openLibraryFolder("root");
  await settle();
  rightClick(folderRow("f1").cells[1]);
  choose("Open in File Explorer");
  await settle();
  assert.ok(menu.hidden);
  assert.deepStrictEqual(explored.splice(0), [["folder", "f1"]]);
  assert.strictEqual(L.folderId, "root", "the library stays where it is");
  const rootTarget = document.createElement("tr"); rootTarget.className = "lib-folder"; rootTarget.folderId = "root";
  rightClick(rootTarget);
  choose("Open in File Explorer");
  await settle();
  assert.deepStrictEqual(explored.splice(0), [["folder", "root"]]);
  context.openLibraryFolder("root");
  await settle();
  // Off exactly when the toolbar's buttons are: during an operation, and for the library's root.
  L.selFolder = "f1"; L.op = true; context.renderLibrary();
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Open in File Explorer", false], ["Rename…", true], ["Delete…", true], ["Export clips", true]]);
  assert.ok($("library-rename").disabled && $("library-delete").disabled);
  press("Escape");
  assert.ok(menu.hidden, "Escape closes it");
  L.op = false; context.renderLibrary();
  assert.ok(!$("library-rename").disabled);
  const rootRow = document.createElement("tr"); rootRow.className = "lib-folder"; rootRow.folderId = "root";
  rightClick(rootRow);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Open in File Explorer", false], ["Rename…", true], ["Delete…", true], ["Export clips", false]]);
  L.selFolder = "root"; context.renderLibraryBar();
  assert.ok($("library-rename").disabled && $("library-delete").disabled);
  fire([window], "pointerdown", { target: document.body });
  assert.ok(menu.hidden, "a click outside closes it");
  L.selFolder = null;

  // A recording row: Play and Move to… for that row; a ticked row stands for every ticked one.
  const realTimeout = context.setTimeout;
  context.setTimeout = (fn, ms) => { if (!ms) setImmediate(fn); return 0; };   // finishFolderOp runs
  rightClick(recRow("r3").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Show in File Explorer", false], ["Copy file", false], ["Rename…", false], ["Move to…", false], ["Export clips", true]]);
  choose("Play");
  await settle();
  assert.deepStrictEqual(ops.pop(), ["play", "r3"]);
  rightClick(recRow("r3").cells[1]);
  assert.strictEqual(document.activeElement.textContent, "Play");
  press("ArrowDown");
  assert.strictEqual(document.activeElement.textContent, "Show in File Explorer");
  assert.strictEqual(document.activeElement.title, "c.wav");
  press("Enter");
  await settle();
  assert.ok(menu.hidden);
  assert.deepStrictEqual(explored.splice(0), [["file", "r3"]]);
  assert.ok(!ops.length, "nothing played");
  exploreAnswer = { ok: false, error: "c.wav is no longer there. Refresh the list." };
  rightClick(recRow("r3").cells[1]);
  choose("Show in File Explorer");
  await settle();
  assert.strictEqual($("banner-text").textContent, "c.wav is no longer there. Refresh the list.");
  exploreAnswer = { ok: true };
  explored.length = 0;
  rightClick(recRow("r3").cells[1]);
  choose("Move to…");
  assert.strictEqual($("folder-dialog-title").textContent, "Move 1 recording to…");
  const pick = (id) => $("folder-dialog-body").children[0].children.find((b) => b.folderId === id);
  pick("f1").onclick();
  await context.folderDialogOk();
  await settle();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.pop())), ["move", ["r3"], "f1"]);
  context.pickGroup(recRow("r1").group, true); context.pickGroup(recRow("r2").group, true);
  context.renderLibrary();
  rightClick(recRow("r2").cells[1]);                                  // ticked: both ticked recordings
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Show in File Explorer", false], ["Copy 2 files", false], ["Rename…", false], ["Move 2 recordings to…", false], ["Export clips", true]]);
  choose("Move 2 recordings to…");
  assert.strictEqual($("folder-dialog-title").textContent, "Move 2 recordings to…");
  pick("f2").onclick();
  await context.folderDialogOk();
  await settle();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.pop())), ["move", ["r1", "r2"], "f2"]);
  context.pickGroup(recRow("r1").group, true); context.pickGroup(recRow("r2").group, true);
  context.renderLibrary();
  rightClick(recRow("r3").cells[1]);                                  // not ticked: just itself
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Show in File Explorer", false], ["Copy file", false], ["Rename…", false], ["Move to…", false], ["Export clips", true]]);
  L.op = true;
  rightClick(recRow("r3").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Show in File Explorer", false], ["Copy file", false], ["Rename…", true], ["Move to…", true], ["Export clips", true]]);
  L.op = false;
  press("Escape");

  // Rename… on a recording row: the dialog shows its main file's name without the extension
  // (all of it selected), and sends the ids of that recording's files in the folder shown.
  api.rename_files = async (ids, name) => { ops.push(["rename", ids, name]);
                                            return { ok: true, renamed: [{ from: "c.wav", to: "Knock.wav" }], ids: { r3: "r9" } }; };
  vm.runInContext(`S.playing = "lib|r3";`, context);
  rightClick(recRow("r3").cells[1]);
  choose("Rename…");
  assert.ok(menu.hidden && dialogShown());
  assert.strictEqual($("folder-dialog-title").textContent, "Rename “c.wav”");
  assert.strictEqual($("folder-dialog-name").value, "c");
  assert.ok(!$("folder-dialog-name").hidden && $("folder-dialog-body").hidden);
  $("folder-dialog-name").value = "Knock";
  await context.folderDialogOk();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.pop())), ["rename", ["r3"], "Knock"]);
  assert.ok(!dialogShown());
  assert.strictEqual(vm.runInContext("S.playing", context), "lib|r9");     // the highlight follows the new id
  assert.strictEqual($("banner-text").textContent, "Renamed “c.wav” to “Knock.wav”.");
  await settle();
  vm.runInContext(`S.playing = null;`, context);
  // A failed rename keeps the dialog open with the reason.
  api.rename_files = async (ids, name) => { ops.push(["rename", ids, name]);
                                            return { ok: false, error: 'There is already a file named "a.wav" here.' }; };
  rightClick(recRow("r3").cells[1]);
  choose("Rename…");
  $("folder-dialog-name").value = "a";
  await context.folderDialogOk();
  assert.ok(dialogShown());
  assert.strictEqual($("folder-dialog-error").textContent, 'There is already a file named "a.wav" here.');
  context.closeFolderDialog();
  await settle();
  ops.length = 0;
  // A .dvf and its .wav not checked yet: rows of their own. The dialog opens for the .dvf; then
  // indexing joins them into one recording: both files are sent (the group as it is now).
  const listed = await api.list_library();
  const withPair = (fp, scan) => async () => ({ ...listed, scan_id: scan, files: [...listed.files,
    { ...libFile("r4", "x.dvf", "root", fp), type: "dvf" }, libFile("r5", "x.wav", "root", fp)] });
  api.list_library = withPair(null, 4);
  await context.loadLibrary();
  await settle();
  api.rename_files = async (ids, name) => { ops.push(["rename", ids, name]); return { ok: true, renamed: [], ids: {} }; };
  rightClick(recRow("r4").cells[1]);
  choose("Rename…");
  assert.ok(!/renamed together/.test($("folder-dialog-body").textContent));     // one file when it opened
  api.list_library = withPair("fpX", 5);
  await context.loadLibrary();
  await settle();
  $("folder-dialog-name").value = "Attic";
  await context.folderDialogOk();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.pop())), ["rename", ["r4", "r5"], "Attic"]);
  await settle();
  // Show in File Explorer on a .dvf with its .wav: the file the row names (the .dvf) only.
  rightClick(recRow("r4").cells[1]);
  assert.strictEqual(menu.children[1].title, "x.dvf");
  choose("Show in File Explorer");
  await settle();
  assert.deepStrictEqual(explored.splice(0), [["file", "r4"]]);
  // One row at a time is reachable with Tab; the arrow keys move between rows, Enter plays.
  const tabs = () => rowsNow().filter((r) => r.group).map((r) => [r.group.main.id, r.tabIndex]);
  assert.deepStrictEqual(tabs(), [["r1", 0], ["r2", -1], ["r3", -1], ["r4", -1]]);
  recRow("r4").onfocus(); recRow("r4").focus();
  assert.deepStrictEqual(tabs(), [["r1", -1], ["r2", -1], ["r3", -1], ["r4", 0]]);
  let k = press("ArrowUp");
  assert.ok(k.defaultPrevented && document.activeElement === recRow("r3"));
  press("ArrowDown");
  assert.strictEqual(document.activeElement, recRow("r4"));
  press("Enter");
  await settle();
  assert.deepStrictEqual(ops.pop(), ["play", "r4"]);
  // F2 on the focused row opens the same dialog, now for both files.
  k = press("F2");
  assert.ok(k.defaultPrevented && dialogShown());
  assert.strictEqual($("folder-dialog-title").textContent, "Rename “x.dvf”");
  assert.strictEqual($("folder-dialog-name").value, "x");
  assert.ok(/x\.dvf and x\.wav are renamed together/.test($("folder-dialog-body").textContent));
  // The same name: nothing is sent, the dialog closes, the player is left alone.
  vm.runInContext(`S.playing = "lib|r4"; S.current = { rec: "h" };`, context);
  await context.folderDialogOk();
  assert.ok(!dialogShown() && !ops.some((o) => o[0] === "rename"));
  assert.strictEqual(vm.runInContext("S.playing", context), "lib|r4");
  vm.runInContext(`S.playing = null; S.current = null;`, context);
  // A failure that left a file under its new name: said in the banner, the dialog closes.
  api.rename_files = async (ids, name) => { ops.push(["rename", ids, name]);
    return { ok: false, error: "x.wav was not renamed: boom. New.dvf could not be given its old name back.",
             ids: { r4: "r7" } }; };
  recRow("r4").focus();
  press("F2");
  $("folder-dialog-name").value = "New";
  await context.folderDialogOk();
  assert.ok(!dialogShown());
  assert.ok(/could not be given its old name back/.test($("banner-text").textContent));
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.pop())), ["rename", ["r4", "r5"], "New"]);
  await settle();
  // F2 while the right-click menu is open only closes the menu.
  rightClick(recRow("r4").cells[1]);
  k = press("F2");
  assert.ok(k.defaultPrevented && menu.hidden && !dialogShown());
  // With several recordings ticked, Rename… says it is for this one only.
  context.pickGroup(recRow("r1").group, true); context.pickGroup(recRow("r2").group, true);
  context.renderLibrary();
  rightClick(recRow("r2").cells[1]);
  assert.strictEqual(menu.children[3].title, "Renames this recording only");
  press("Escape");                                                    // (r1 and r2 stay ticked)
  // F2 on a folder row renames the folder; off while an operation runs or in a second window.
  folderRow("f1").focus();
  k = press("F2");
  assert.ok(k.defaultPrevented);
  assert.strictEqual($("folder-dialog-title").textContent, "Rename “Old Mill”");
  context.closeFolderDialog();
  L.selFolder = null;
  L.op = true;
  recRow("r4").focus();
  press("F2");
  assert.ok(!dialogShown());
  rightClick(recRow("r4").cells[1]);
  assert.deepStrictEqual(menuItems().slice(1, 4), [["Show in File Explorer", false], ["Copy file", false], ["Rename…", true]]);
  press("Escape");
  L.op = false;
  vm.runInContext(`S.caps.marks_read_only = true;`, context);
  rightClick(recRow("r4").cells[1]);
  assert.deepStrictEqual(menuItems().slice(1, 4), [["Show in File Explorer", false], ["Copy file", false], ["Rename…", true]]);
  press("Escape");
  press("F2");
  assert.ok(!dialogShown());
  vm.runInContext(`S.caps.marks_read_only = false;`, context);
  document.activeElement = null;
  context.setTimeout = realTimeout;

  // Drag out: every drag of a row is a Windows file drag the backend starts (the browser's own drag
  // is cancelled); the page sends one file id per recording (its WAV copy when it has one, else its
  // own file: the backend shares a .dvf as a playable copy), never a path. The same drag dropped on a
  // library folder in this window moves the recordings (all their files), as before; Copy file and
  // Ctrl+C put the same files on the clipboard.
  {
    const listedBefore = api.list_library, picked = new Set(L.selected);
    L.selected.clear();
    api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 40, exists: true, truncated: false, indexing: false,
                                      pending: 0, folders: libFolders,
                                      files: [{ ...libFile("d1", "x.dvf", "root", "fpx"), type: "dvf" }, libFile("w1", "x.wav", "root", "fpx"),
                                              { ...libFile("d2", "y.dvf", "root", "fpy"), type: "dvf" },
                                              { ...libFile("m1", "z.mpeg", "root", "fpz"), type: "mpeg" }] });
    await context.loadLibrary();
    await settle();
    const shares = [];
    let release, answer = { ok: true, started: true, count: 1, effect: "copy" };
    api.drag_out = (ids) => { shares.push(["drag", JSON.parse(JSON.stringify(ids))]);
                              return new Promise((r) => { release = () => r(answer); }); };
    api.copy_files = async (ids) => { shares.push(["copy", JSON.parse(JSON.stringify(ids))]); return { ok: true, count: ids.length }; };
    const dragEvent = (props = {}) => ({ defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props });
    // One recording (a .dvf with its WAV): the WAV goes out; the browser's drag is cancelled.
    let ev = dragEvent();
    recRow("d1").ondragstart(ev);
    assert.ok(ev.defaultPrevented, "the browser's own drag is cancelled");
    assert.deepStrictEqual(shares.splice(0), [["drag", ["w1"]]]);
    assert.ok(recRow("d1").draggable);
    assert.deepStrictEqual(JSON.parse(vm.runInContext("JSON.stringify(S.drag)", context)), { ids: ["d1", "w1"] });
    recRow("d2").ondragstart(dragEvent());                             // one drag at a time
    assert.deepStrictEqual(shares, []);
    // Dropped back on a library folder: the page moves the recording (the drag offers copy only).
    const dt = { dropEffect: "none", types: ["Files"] };
    ev = fire([$("library-rows")], "dragover", { target: folderRow("f1").cells[1], dataTransfer: dt });
    assert.ok(ev.defaultPrevented && dt.dropEffect === "copy");
    assert.ok(folderRow("f1").classList.contains("drop-target"));
    ev = fire([$("library-rows")], "dragover", { target: recRow("d2").cells[1], dataTransfer: { dropEffect: "none", types: ["Files"] } });
    assert.ok(!ev.defaultPrevented, "not a folder: no drop");
    ops.length = 0;
    ev = fire([$("library-rows"), document], "drop", { target: folderRow("f1").cells[1], dataTransfer: dt });
    assert.ok(ev.defaultPrevented);
    await settle();
    assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.splice(0))), [["move", ["d1", "w1"], "f1"]]);
    assert.strictEqual(vm.runInContext("S.drag", context), null);
    release();
    await settle();
    assert.ok(!vm.runInContext("S.sharing", context));
    // Picked rows drag together, one file each: the .dvf without a WAV goes as itself (the backend
    // makes its MP3, saying so), an .mpeg as itself.
    await context.loadLibrary();
    await settle();
    for (const id of ["d1", "d2", "m1"]) context.pickGroup(recRow(id).group, true);
    context.renderLibrary();
    recRow("m1").ondragstart(dragEvent());
    assert.deepStrictEqual(shares.splice(0), [["drag", ["w1", "d2", "m1"]]]);
    window.onBackendEvent("share-preparing", { name: "y.dvf" });
    assert.strictEqual($("status").textContent, "Preparing y.dvf to share…");
    answer = { ok: true, started: false, count: 3, made: 1 };          // let go before it was ready
    release();
    await settle();
    assert.strictEqual($("status").textContent, "Ready to share: drag them again.");
    assert.strictEqual(vm.runInContext("S.drag", context), null);
    // A failure is shown; nothing is left half-way.
    answer = { ok: false, error: "x.dvf is no longer there. Refresh the list." };
    recRow("d2").ondragstart(dragEvent());
    release();
    await settle();
    assert.strictEqual($("banner-text").textContent, "x.dvf is no longer there. Refresh the list.");
    assert.ok(!vm.runInContext("S.sharing || S.drag", context));
    shares.length = 0;
    // The All recordings view: dragged out too, but no folder takes it.
    L.flat = true; context.renderLibrary();
    L.selected.clear(); context.renderLibrary();
    answer = { ok: true, started: true, count: 1, effect: "none" };
    recRow("d2").ondragstart(dragEvent());
    assert.deepStrictEqual(shares.splice(0), [["drag", ["d2"]]]);
    assert.strictEqual(context.dropTarget({ target: recRow("d2").cells[1] }), null);
    release();
    await settle();
    L.flat = false; context.renderLibrary();
    // Not while a folder operation runs.
    L.op = true;
    ev = dragEvent();
    recRow("d2").ondragstart(ev);
    assert.ok(ev.defaultPrevented && !shares.length);
    L.op = false;
    // Copy file: the right-click menu, and Ctrl+C on a focused row.
    rightClick(recRow("d1").cells[1]);
    assert.deepStrictEqual(menuItems()[2], ["Copy file", false]);
    assert.match(menu.children[2].title, /paste it with Ctrl\+V into Discord, WhatsApp/);
    choose("Copy file");
    await settle();
    assert.deepStrictEqual(shares.splice(0), [["copy", ["w1"]]]);
    assert.strictEqual($("status").textContent, "Copied 1 file. Paste it with Ctrl+V into a chat, an email or a folder.");
    context.pickGroup(recRow("d2").group, true); context.pickGroup(recRow("m1").group, true);
    context.renderLibrary();
    recRow("m1").focus();
    const k = fire([document], "keydown", { key: "c", ctrlKey: true, target: recRow("m1") });
    assert.ok(k.defaultPrevented);
    await settle();
    assert.deepStrictEqual(shares.splice(0), [["copy", ["d2", "m1"]]]);
    rightClick(recRow("m1").cells[1]);
    assert.deepStrictEqual(menuItems()[2], ["Copy 2 files", false]);
    press("Escape");
    fire([document], "keydown", { key: "c", ctrlKey: true, target: document.body });   // not on a row: nothing
    await settle();
    assert.deepStrictEqual(shares, []);
    document.activeElement = null;
    L.selected.clear();
    for (const id of picked) L.selected.add(id);
    api.list_library = async () => ({ ...(await listedBefore()), scan_id: 41 });   // (never an older scan)
    await context.loadLibrary();
    api.list_library = listedBefore;
    await settle();
  }

  // All recordings (no folders): empty space offers nothing, but the browser menu stays off there.
  L.flat = true; context.renderLibrary();
  e = rightClick($("empty"));
  assert.ok(e.defaultPrevented && menu.hidden);
  rightClick(recRow("r1").cells[1]);
  assert.deepStrictEqual(menuItems().map(([t]) => t), ["Play", "Show in File Explorer", "Copy 2 files", "Rename…", "Move 2 recordings to…", "Export clips"]);
  fire([window], "scroll", {});
  assert.ok(menu.hidden, "scrolling closes it");
  L.flat = false;
  // Not in the library (a recorder shown): the browser's own menu.
  vm.runInContext(`S.view = "device";`, context);
  e = rightClick($("empty"));
  assert.ok(!e.defaultPrevented && menu.hidden);

  // A read-only store: the mark and folder tools say the backend's own reason, and
  // "store-writable" (the store could be locked after all) brings them back without a restart.
  const caps = api.capabilities;
  const reason = "Another OpenEVP (process 42) is open; marks can only be changed there. If you don't see its window, it may still be closing.";
  const markTip = $("mark-evp").title;
  api.capabilities = async () => ({ ...(await caps()), marks_read_only: true, marks_read_only_reason: reason });
  vm.runInContext(`S.caps = { ...S.caps, marks_read_only: true, marks_read_only_reason: ${JSON.stringify(reason)} };
                   renderMarkTools(); renderLibraryBar();`, context);
  assert.ok($("mark-evp").disabled && $("reviewed").disabled);
  assert.strictEqual($("mark-evp").title, reason);
  assert.strictEqual($("reviewed-label").title, reason);
  assert.strictEqual($("library-tools").title, reason);
  assert.ok(!/Another OpenEVP window is open/.test(vm.runInContext("marksTip()", context)));
  window.onBackendEvent("store-writable", {});                     // still read-only when asked: nothing changes
  await settle();
  assert.ok($("mark-evp").disabled && $("mark-evp").title === reason);
  api.capabilities = caps;
  window.onBackendEvent("store-writable", {});
  await settle();
  assert.ok(!$("mark-evp").disabled && !$("reviewed").disabled);
  assert.strictEqual($("mark-evp").title, markTip);
  assert.strictEqual($("library-tools").title, "");
  assert.ok(vm.runInContext("marksWritable() && marksTip() === ''", context));
  assert.match($("banner-text").textContent, /can be changed again/);

  // Startup's capabilities() answer and a "store-writable" one arrive out of order: the older
  // (read-only) answer never wins, whichever order they come in.
  const readOnlyCaps = async () => ({ ...(await caps()), marks_read_only: true, marks_read_only_reason: reason });
  for (const staleFirst of [false, true]) {
    let answerStartup;
    api.capabilities = () => new Promise((res) => { answerStartup = async () => res(await readOnlyCaps()); });
    vm.runInContext("S.started = false;", context);
    const started = ready();                                       // startup asks; its answer is held back
    api.capabilities = caps;                                       // the store is writable by the time the event asks
    if (staleFirst) await answerStartup();                         // the stale answer lands first...
    window.onBackendEvent("store-writable", {});                   // ...or only after the event's answer
    await settle();
    if (!staleFirst) await answerStartup();
    await started;
    await settle();
    assert.ok(vm.runInContext("S.started && !S.caps.marks_read_only", context), `staleFirst=${staleFirst}`);
    assert.ok(!$("mark-evp").disabled && $("mark-evp").title === markTip, `staleFirst=${staleFirst}`);
    assert.strictEqual($("library-tools").title, "");
  }

  // Startup's default_destination() answer (the Save-to folder from before recovery) is held
  // back while "store-writable" asks again: the recovered folder wins, whichever lands first.
  const dests = api.default_destination;
  for (const staleFirst of [false, true]) {
    let answerStartup;
    api.default_destination = () => new Promise((res) => { answerStartup = () => res("C:\\old"); });
    vm.runInContext("S.started = false;", context);
    const started = ready();
    await settle();                                                // startup is waiting for its folder
    api.default_destination = async () => "C:\\remembered";      // recovery applied the remembered one
    if (staleFirst) answerStartup();
    window.onBackendEvent("store-writable", {});
    await settle();
    if (!staleFirst) answerStartup();
    await started;
    await settle();
    assert.strictEqual(vm.runInContext("S.dest", context), "C:\\remembered", `staleFirst=${staleFirst}`);
    assert.strictEqual($("dest").textContent, "C:\\remembered", `staleFirst=${staleFirst}`);
  }
  api.default_destination = dests;

  // ---- EVP clips: the player's Export clips and Save clip, and the library's two menu entries ----
  assert.ok(/id="export-marked"[^\n]*\n\s*<button id="export-clips"[^>]*>Export clips<\/button>/.test(html),
            "Export clips sits next to the WAV with marks");
  const clipCalls = [];
  api.open_folder = async (folder) => { clipCalls.push(["open", folder]); return { ok: true }; };
  api.export_clips = async (rec, id) => { clipCalls.push(["clips", rec, id]);
    return { ok: true, saved: id ? 1 : 2, already: id ? 0 : 1, names: [], notes: [], folder: "D:/save/A/Clips" }; };
  const marks = [{ id: "m1", start: 1, end: 1.5, cls: "A", note: "hi" }, { id: "m2", start: 2, end: 2.2, cls: "C", note: "" }];
  vm.runInContext(`setCurrent("A-001", ${JSON.stringify({ rec: "h1", duration: 3, fp: "fpP", marks,
                                                        backup: { status: null, detail: "" }, reviewed: false })});`, context);
  assert.ok(!$("export-clips").disabled);
  await $("export-clips").onclick();
  assert.deepStrictEqual(clipCalls.pop(), ["clips", "h1", null]);                 // every mark
  assert.strictEqual($("banner-text").textContent, "✓ 2 clips saved (1 already there).");
  assert.ok($("banner").className === "ok" && $("banner-action").textContent === "Open folder");
  await $("banner-action").onclick();
  assert.deepStrictEqual(clipCalls.pop(), ["open", "D:/save/A/Clips"]);
  const saveClip = (i) => $("marks-list").children[i].children.find((b) => b.textContent === "Save clip");
  assert.strictEqual($("marks-list").children.length, 2);
  assert.ok(!saveClip(1).disabled);
  saveClip(1).onclick({ stopPropagation() {} });
  await settle();
  assert.deepStrictEqual(clipCalls.pop(), ["clips", "h1", "m2"]);                 // that mark only
  assert.strictEqual($("banner-text").textContent, "✓ 1 clip saved.");
  api.export_clips = async () => ({ ok: false, error: reason });                   // a second window: its reason
  await $("export-clips").onclick();
  assert.strictEqual($("banner-text").textContent, reason);
  vm.runInContext(`setCurrent(null);`, context);

  // The library: Export clips on a recording (its files) and on a folder, as a background job.
  api.export_clips_files = async (ids, job) => { clipCalls.push(["files", ids, job]); return { ok: true, job }; };
  api.export_clips_folder = async (id, job) => { clipCalls.push(["folder", id, job]); return { ok: true, job }; };
  api.cancel_clips = async (job) => { clipCalls.push(["cancel", job]); return { ok: true }; };
  context.showLibrary();
  await settle();
  vm.runInContext(`setSummary("fpX", { marks: { A: 1, B: 0, C: 2 }, reviewed: false, notes: "" }); renderLibrary();`, context);
  rightClick(recRow("r1").cells[1]);
  assert.deepStrictEqual(menuItems()[5], ["Export clips", true]);                  // nothing marked in it
  assert.strictEqual(menu.children[5].title, "No EVPs marked in this recording");
  press("Escape");
  rightClick(recRow("r4").cells[1]);
  assert.deepStrictEqual(menuItems()[5], ["Export clips", false]);
  choose("Export clips");
  await settle();
  let [what, ids, job] = JSON.parse(JSON.stringify(clipCalls.pop()));
  assert.deepStrictEqual([what, ids], ["files", ["r4", "r5"]]);                  // the .dvf and its WAV
  assert.ok(vm.runInContext("S.clips.running", context));
  assert.match($("banner-text").textContent, /^Exporting the clips of x\.dvf…/);
  // Meanwhile the folder tools, Export WAV with marks and the clip actions wait for it.
  context.renderLibrary();
  assert.ok($("library-new").disabled && $("library-move").disabled, "the folder tools wait for the clips");
  vm.runInContext(`S.marks = [{ id: "m9", start: 1, end: 2, cls: "A", note: "" }]; renderMarks();`, context);
  assert.ok($("export-marked").disabled && $("export-clips").disabled);
  assert.ok($("marks-list").children[0].children.find((b) => b.textContent === "Save clip").disabled);
  vm.runInContext(`S.marks = []; renderMarks();`, context);
  window.onBackendEvent("clips-progress", { job, done: 1, total: 2, name: "x.dvf" });
  assert.strictEqual($("banner-text").textContent, "Exporting the clips of x.dvf… 1 of 2");
  assert.strictEqual($("banner-action").textContent, "Cancel");
  rightClick(folderRow("f1").cells[1]);                                            // one job at a time
  assert.deepStrictEqual(menuItems()[4], ["Export clips", true]);
  assert.strictEqual(menu.children[4].title, "Wait for the clips being exported");
  press("Escape");
  $("banner-action").onclick();
  assert.deepStrictEqual(clipCalls.pop(), ["cancel", job]);
  window.onBackendEvent("clips-progress", { job, done: 2, total: 2, name: "x.wav" });
  assert.ok($("banner-action").disabled, "Cancel is asked once");
  window.onBackendEvent("clips-done", { job: job - 1, saved: 9, already: 0, skipped: [], notes: [], folder: null,
                                        cancelled: false, recordings: 1 });            // an older job: ignored
  assert.ok(vm.runInContext("S.clips.running", context));
  window.onBackendEvent("clips-done", { job, saved: 0, already: 0, skipped: [], notes: [], folder: null,
                                        cancelled: true, closing: false, recordings: 0 });
  assert.ok(!vm.runInContext("S.clips.running", context));
  assert.strictEqual($("banner-text").textContent, "Stopped. 0 clips saved.");
  context.renderLibrary();
  assert.ok(!$("library-new").disabled, "the folder tools are back");
  // A WAV with marks being saved: the library's tools and Export clips wait too.
  vm.runInContext(`S.exportingMarked = true; renderLibrary();`, context);
  assert.ok($("library-new").disabled);
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems()[4], ["Export clips", true]);
  assert.strictEqual(menu.children[4].title, "Wait for the WAV or clips being saved");
  press("Escape");
  vm.runInContext(`S.exportingMarked = false; renderLibrary();`, context);
  assert.ok($("banner-action").hidden);                                            // nothing to open
  await settle();
  // A folder: its id; the summary counts what was already there and what was skipped, and why.
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems()[4], ["Export clips", false]);
  choose("Export clips");
  await settle();
  [what, ids, job] = clipCalls.pop();
  assert.deepStrictEqual([what, ids], ["folder", "f1"]);
  assert.match($("banner-text").textContent, /^Exporting the clips of Old Mill…/);
  window.onBackendEvent("clips-done", { job, saved: 5, already: 2, recordings: 3, notes: [], folder: "D:/save/Old Mill/Clips",
                                        skipped: ["bad.dvf (its audio could not be decoded: damaged)"], cancelled: false });
  assert.strictEqual($("banner-text").textContent,
                     "✓ 5 clips saved (2 already there). Skipped 1 recording: bad.dvf (its audio could not be decoded: damaged).");
  assert.strictEqual($("banner").className, "");                                  // a warning: something was skipped
  await $("banner-action").onclick();
  assert.deepStrictEqual(clipCalls.pop(), ["open", "D:/save/Old Mill/Clips"]);
  // Refused at once (a second window, say): said, and nothing stays running.
  api.export_clips_folder = async () => ({ ok: false, error: "The library changed. Refresh and try again." });
  await settle();
  rightClick(folderRow("f1").cells[1]);
  choose("Export clips");
  await settle();
  assert.ok(!vm.runInContext("S.clips.running", context));
  assert.strictEqual($("banner-text").textContent, "The library changed. Refresh and try again.");
  // The call itself fails (rejects): nothing stays running either.
  api.export_clips_folder = async () => { throw new Error("boom"); };
  rightClick(folderRow("f1").cells[1]);
  choose("Export clips");
  await settle();
  assert.ok(!vm.runInContext("S.clips.running", context));
  assert.strictEqual($("banner-text").textContent, "The clips export did not start: boom");

  // ---- Open audio file…: WAV or MP3 (the dialog's filter is app/main.py's AUDIO_FILE_TYPES) ----
  assert.ok(/<button id="open-wav" title="Open a WAV or MP3 file">Open audio file…<\/button>/.test(html));
  assert.ok(/<button id="open-wav-2" class="link" title="Open another WAV or MP3 file">Open audio file…<\/button>/.test(html));
  assert.ok(!/Open WAV file/.test(html));

  // ---- the clip format: a menu next to Export clips (MP3 by default), one setting for the player and the library ----
  assert.ok(/<button id="export-clips"[^>]*>Export clips<\/button>\s*<select id="clip-format"[^>]*>\s*<option value="mp3">MP3 \(for sharing\)<\/option>\s*<option value="wav">WAV \(full quality\)<\/option>\s*<\/select>/.test(html),
            "the Clip format menu sits right after Export clips: MP3 (for sharing) first, then WAV (full quality)");
  assert.strictEqual($("clip-format").value, "mp3");                                // capabilities() said nothing: MP3
  assert.match($("export-clips").title, /own short MP3 clip/);
  const formatCalls = [];
  api.set_clip_format = async (fmt) => { formatCalls.push(fmt); return { ok: true, format: fmt, remembered: true }; };
  $("clip-format").value = "wav";
  await $("clip-format").onchange();
  assert.deepStrictEqual(formatCalls, ["wav"]);
  assert.strictEqual(vm.runInContext("S.caps.clip_format", context), "wav");
  assert.strictEqual($("clip-format").value, "wav");
  assert.match($("export-clips").title, /own short WAV clip/);
  rightClick(folderRow("f1").cells[1]);                                            // the library's menu says so too
  assert.match(menu.children[4].title, /as its own WAV clip$/);
  press("Escape");
  vm.runInContext(`setCurrent("A-001", ${JSON.stringify({ rec: "h1", duration: 3, fp: "fpP", marks,
                                                        backup: { status: null, detail: "" }, reviewed: false })});`, context);
  assert.match($("marks-list").children[0].children.find((b) => b.textContent === "Save clip").title, /own WAV clip/);
  vm.runInContext(`setCurrent(null);`, context);
  api.set_clip_format = async () => ({ ok: false, error: "Unknown clip format." });   // refused: said, and shown as it was
  $("clip-format").value = "mp3";
  await $("clip-format").onchange();
  assert.strictEqual($("banner-text").textContent, "Unknown clip format.");
  assert.strictEqual($("clip-format").value, "wav");
  api.set_clip_format = async (fmt) => ({ ok: true, format: fmt, remembered: false });  // a second window: this session
  $("clip-format").value = "mp3";
  await $("clip-format").onchange();
  assert.strictEqual($("clip-format").value, "mp3");
  rightClick(folderRow("f1").cells[1]);
  assert.match(menu.children[4].title, /as its own MP3 clip$/);
  press("Escape");
  // The remembered format arrives with capabilities() (startup, or when the store becomes writable).
  vm.runInContext(`S.caps.clip_format = "wav"; showClipFormat();`, context);
  assert.strictEqual($("clip-format").value, "wav");
  vm.runInContext(`S.caps.clip_format = "mp3"; showClipFormat();`, context);

  // An ICD-ST10 .dvf in the library, in a build without the LPEC ST decoder: greyed out with its
  // reason (not the ⚠ of a damaged file), and a click says why instead of asking the backend to play it.
  const st10File = { ...libFile("r7", "001_A_001_Unknown.dvf", "root", null), type: "dvf", seconds: 20.1,
                     error: "001_A_001_Unknown.dvf can't be played or marked: LPEC ST (ICD-ST10) playback is not included in this build.", unplayable: "LPEC ST (ICD-ST10) playback is not included in this build" };
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 50, exists: true, truncated: false, indexing: false,
                                    pending: 0, folders: libFolders, files: [st10File, libFile("r8", "b.wav", "root", "fp8")] });
  context.showLibrary();
  await context.loadLibrary();
  await settle();
  const st10lib = recRow("r7");
  assert.ok(st10lib.classList.contains("unplayable"));
  assert.strictEqual(st10lib.title, "LPEC ST (ICD-ST10) playback is not included in this build.");
  assert.strictEqual(st10lib.cells[5].textContent, "—");
  assert.strictEqual(st10lib.cells[5].title, "LPEC ST (ICD-ST10) playback is not included in this build.");
  assert.ok(!recRow("r8").classList.contains("unplayable"));
  ops.length = 0;
  st10lib.onclick();
  await settle();
  assert.strictEqual($("banner-text").textContent, "LPEC ST (ICD-ST10) playback is not included in this build.");
  assert.deepStrictEqual(ops, [], "never sent to the backend to play");
  rightClick(st10lib.cells[1]);
  assert.deepStrictEqual(menu.children[0].textContent, "Play");
  assert.ok(menu.children[0].disabled);
  assert.strictEqual(menu.children[0].title, "LPEC ST (ICD-ST10) playback is not included in this build.");
  assert.deepStrictEqual(menuItems()[5], ["Export clips", true]);                  // no marks: nothing to cut
  press("Escape");

  // A Clips folder OpenEVP made: listed with its own icon and "Clips" tag, its clips counted apart and
  // playable, but never EVPs (even one marked in the player): no chips, no filter, no count, not in the
  // All recordings view; Export clips is off for the folder (no bulk re-clipping) but on for a marked clip;
  // recordings can't be moved into it.
  const clipFolders = [...libFolders, { id: "fc", name: "Clips", rel: ["Clips"], parent: "root", clips: true, in_clips: true }];
  const marked = (f, A) => ({ ...f, marks: { A, B: 0, C: 0 } });
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 60, exists: true, truncated: false, indexing: false,
                                    pending: 0, folders: clipFolders,
                                    files: [marked(libFile("r1", "a.wav", "root", "fp1"), 1),
                                            { ...marked(libFile("c1", "a_EVP-A_00m01.0s.wav", "fc", "fpc1"), 2), clip: true },
                                            { ...libFile("c2", "a_EVP-A_00m02.0s.wav", "fc", "fpc2"), clip: true }] });
  await context.loadLibrary();
  await settle();
  assert.strictEqual($("library-count").textContent, "(1)");                       // recordings only
  const clipsRow = folderRow("fc");
  assert.ok(clipsRow.classList.contains("lib-clips"));
  assert.strictEqual(clipsRow.cells[1].textContent, "🎞️ Clips Clips2 clips");
  assert.strictEqual(clipsRow.cells[1].children[0].children[1].className, "badge badge-clips");
  assert.strictEqual(clipsRow.cells[5].textContent, "", "no EVP chips for clips");
  assert.match(clipsRow.title, /not counted as EVPs/);
  assert.ok(!folderRow("f1").classList.contains("lib-clips"));
  assert.strictEqual(folderRow("f1").cells[1].textContent, "📁 Old MillNo recordings");
  rightClick(clipsRow.cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Open in File Explorer", false], ["Rename…", false], ["Delete…", false], ["Export clips", true]]);
  assert.strictEqual(menu.children[4].title, "A folder of clips: right-click one clip to cut clips from it");
  choose("Open in File Explorer");
  await settle();
  assert.deepStrictEqual(explored.splice(0), [["folder", "fc"]]);
  rightClick(clipsRow.cells[1]);
  press("Escape");
  // "Has EVPs": the recording, not the folder of (marked) clips.
  vm.runInContext(`S.lib.filter = "evp"; renderLibrary();`, context);
  assert.deepStrictEqual(rowsNow().map((r) => r.folderId || r.group.main.id), ["r1"]);
  vm.runInContext(`S.lib.filter = "all"; renderLibrary();`, context);
  // Moving the recording: the Clips folder is not offered, nor a drop target.
  rightClick(recRow("r1").cells[1]);
  choose("Move to…");
  assert.ok(pick("fc").disabled && pick("fc").title === "A Clips folder is for EVP clips only");
  assert.ok(!pick("f1").disabled);
  context.closeFolderDialog();
  vm.runInContext(`S.drag = { ids: ["r1"] };`, context);
  assert.strictEqual(context.dropTarget({ target: clipsRow.cells[1] }), null);
  assert.strictEqual(context.dropTarget({ target: folderRow("f1").cells[1] }), folderRow("f1"));
  vm.runInContext(`S.drag = null;`, context);
  // Inside it: the clips, each "Clip" in the EVP column; one plays; Export clips is on for a marked one
  // (its clips go beside it, in the same Clips folder) and off for an unmarked one.
  context.openLibraryFolder("fc");
  await settle();
  assert.deepStrictEqual(rowsNow().map((r) => r.group.main.id), ["c1", "c2"]);
  const clipRow = recRow("c1");
  assert.ok(clipRow.classList.contains("lib-clip"));
  assert.strictEqual(clipRow.cells[5].textContent, "Clip");
  assert.match(clipRow.cells[5].title, /2 marks of its own, not counted as EVPs/);
  assert.strictEqual(recRow("c2").cells[5].textContent, "Clip");
  rightClick(clipRow.cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Show in File Explorer", false], ["Copy file", false], ["Rename…", false], ["Move to…", false], ["Export clips", false]]);
  assert.match(menu.children[5].title, /^Save each EVP marked in this clip as its own (MP3|WAV) clip \(in the same Clips folder\)$/);
  choose("Show in File Explorer");
  await settle();
  assert.deepStrictEqual(explored.splice(0), [["file", "c1"]]);
  rightClick(clipRow.cells[1]);
  api.export_clips_files = async (ids, job) => { clipCalls.push(["files", ids, job]); return { ok: true, job }; };
  choose("Export clips");
  await settle();
  [what, ids, job] = JSON.parse(JSON.stringify(clipCalls.pop()));
  assert.deepStrictEqual([what, ids], ["files", ["c1"]]);
  assert.match($("banner-text").textContent, /^Exporting the clips of a_EVP-A_00m01\.0s\.wav…/);
  window.onBackendEvent("clips-done", { job, saved: 2, already: 0, recordings: 1, notes: [], folder: "D:/lib/Clips",
                                        skipped: [], cancelled: false });
  assert.ok(!vm.runInContext("S.clips.running", context));
  assert.strictEqual($("banner-text").textContent, "✓ 2 clips saved.");
  rightClick(recRow("c2").cells[1]);
  assert.deepStrictEqual(menuItems()[5], ["Export clips", true]);
  assert.strictEqual(menu.children[5].title, "No EVPs marked in this clip");
  press("Escape");
  ops.length = 0;
  clipRow.onclick();
  await settle();
  assert.deepStrictEqual(ops, [["play", "c1"]]);
  // The All recordings view: recordings only.
  vm.runInContext(`S.lib.flat = true; renderLibrary();`, context);
  assert.deepStrictEqual(rowsNow().map((r) => r.group.main.id), ["r1"]);
  vm.runInContext(`S.lib.flat = false; renderLibrary();`, context);

  // An MP3 clip (the default clip format): a clip like a WAV one. It plays as every file does, as a
  // WAV the audio server decoded (its peaks drawn at once), can be marked, and moves like a WAV clip.
  vm.runInContext(`S.caps.formats.mp3 = { label: "MP3", playable: true, reason: null };`, context);
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 61, exists: true, truncated: false, indexing: false,
                                    pending: 0, folders: clipFolders,
                                    files: [marked(libFile("r1", "a.wav", "root", "fp1"), 1),
                                            { ...libFile("c3", "a_EVP-B_00m03.0s_hi.mp3", "fc", "fc3"), type: "mp3", clip: true }] });
  context.openLibraryFolder("root");
  await context.loadLibrary();
  await settle();
  assert.strictEqual(folderRow("fc").cells[1].textContent, "🎞️ Clips Clips1 clip");
  context.openLibraryFolder("fc");
  await settle();
  const mp3Row = recRow("c3");
  assert.ok(mp3Row.classList.contains("lib-clip") && !mp3Row.classList.contains("unplayable"));
  assert.strictEqual(mp3Row.cells[5].textContent, "Clip");
  rightClick(mp3Row.cells[1]);
  assert.deepStrictEqual(menuItems()[0], ["Play", false]);
  choose("Move to…");                                          // anywhere a WAV clip may go
  assert.ok(!pick("f1").disabled && !pick("root").disabled);
  context.closeFolderDialog();
  vm.runInContext(`S.drag = { ids: ["c3"] };`, context);
  assert.ok(vm.runInContext(`typeof mp3StaysInClips === "undefined"`, context));
  vm.runInContext(`S.drag = null;`, context);
  vm.runInContext(`S.wsCalls = []; S.ws = new Proxy(S.ws, { get: (t, k) =>
    k === "load" ? (...a) => { S.wsCalls.push(["load", ...a]); return Promise.resolve(); }
    : k === "setOptions" ? (o) => { S.wsCalls.push(["options", o]); } : t[k] });`, context);
  api.play_library = async (id) => { ops.push(["play", id]);
    return { ok: true, url: "http://127.0.0.1:1/t/c3.wav", peaks: [0.5, 0.25], duration: 4000, rate: 44100, channels: 2,
             fp: "fc3", rec: "h9", name: "a_EVP-B_00m03.0s_hi.mp3", imported: 0, marks: [], reviewed: false,
             backup: { status: null, detail: "" }, backup_needed: false }; };
  ops.length = 0;
  mp3Row.onclick();
  await settle();
  assert.deepStrictEqual(ops, [["play", "c3"]]);
  const wsCalls = JSON.parse(vm.runInContext("JSON.stringify(S.wsCalls)", context));
  // A long one, past the full-detail budget, is drawn from the server's peaks, as a WAV is.
  assert.deepStrictEqual(wsCalls.filter((c) => c[0] === "load"), [["load", "http://127.0.0.1:1/t/c3.wav", [[0.5, 0.25]], 4000]]);
  assert.strictEqual(vm.runInContext("S.zoomMax", context), 400);
  assert.ok(!$("mark-evp").disabled && !$("reviewed").disabled);                 // marked like any clip
  assert.ok(!/MP3/.test($("mark-evp").title));
  // Marked, the MP3 clip is cut into clips like any recording: Export clips and Save clip.
  api.export_clips = async (rec, id) => { clipCalls.push(["clips", rec, id]);
    return { ok: true, saved: 1, already: 0, names: [], notes: [], folder: "D:/lib/Clips" }; };
  const m7 = [{ id: "m7", start: 0.5, end: 0.9, cls: "B", note: "" }];
  api.get_marks = async () => ({ ok: true, marks: m7, reviewed: false, backup: { status: null, detail: "" } });
  vm.runInContext(`S.marks = ${JSON.stringify(m7)}; renderMarks();`, context);
  assert.ok(!$("export-clips").disabled);
  await $("export-clips").onclick();
  assert.deepStrictEqual(clipCalls.pop(), ["clips", "h9", null]);
  assert.strictEqual($("banner-text").textContent, "✓ 1 clip saved.");
  const clipSave = $("marks-list").children[0].children.find((b) => b.textContent === "Save clip");
  assert.ok(!clipSave.disabled);
  clipSave.onclick({ stopPropagation() {} });
  await settle();
  assert.deepStrictEqual(clipCalls.pop(), ["clips", "h9", "m7"]);
  delete api.get_marks;
  vm.runInContext(`S.marks = []; renderMarks();`, context);
  vm.runInContext(`setCurrent(null); S.region = null;`, context);
  context.openLibraryFolder("root");
  await settle();

  // ---- looping a saved mark: a click selects it (the bar shows it, with Loop), the row's 🔁 loops it ----
  // A recording WaveSurfer and regions plugin: what was played, from where, and whether it stops at the end.
  const wsLog = [];
  let wsTime = 0, wsPlaying = false;
  const fakeWs = new Proxy({
    play: (t, end) => { wsLog.push(["ws.play", t, end]); wsPlaying = true; if (t != null) wsTime = t; },
    isPlaying: () => wsPlaying, getCurrentTime: () => wsTime, getDuration: () => 3,
    playPause: () => { wsPlaying = !wsPlaying; }, pause: () => { wsPlaying = false; },
  }, { get: (t, k) => (k in t ? t[k] : anything) });
  const madeRegions = [];
  const fakeRegions = {
    addRegion: (o) => {
      const r = { ...o, element: { part: o.id }, removed: false,
                  play(stop) { wsLog.push(["play", this.id, stop]); wsPlaying = true; wsTime = this.start; },
                  remove() { this.removed = true; }, setOptions(x) { Object.assign(this, x); } };
      madeRegions.push(r);
      return r;
    },
    getRegions: () => madeRegions.filter((r) => !r.removed), on() {},
  };
  context.__ws = fakeWs; context.__regions = fakeRegions;
  const realPlayer = vm.runInContext("[S.ws, S.regions]", context);
  vm.runInContext("S.ws = __ws; S.regions = __regions;", context);
  context.confirm = () => true;
  api.delete_mark = async () => ({ ok: true });
  api.get_marks = async () => ({ ok: true, marks: [], reviewed: false, backup: { status: null, detail: "" } });
  const loopMarks = [{ id: "m1", start: 1, end: 1.5, cls: "A", note: "hi" }, { id: "m2", start: 2, end: 2.4, cls: "B", note: "" },
                     { id: "m3", start: 2.8, end: 2.8, cls: "C", note: "imported" }];
  const loadMarked = (rec, marks = loopMarks) => {
    vm.runInContext(`setCurrent("A-001", ${JSON.stringify({ rec, duration: 3, fp: null, marks,
                                                          backup: { status: null, detail: "" }, reviewed: false })});
                     drawMarks(S.playSeq);`, context);
  };
  const reg = (id) => vm.runInContext("S.markRegions", context).get(id);
  const markRowEl = (i) => $("marks-list").children[i];
  const loopBtn = (i) => markRowEl(i).loopButton;
  const looping = () => JSON.parse(vm.runInContext("JSON.stringify([S.activeMark, S.markLoop])", context));
  const ev = { stopPropagation() {} };
  const lastPlay = () => wsLog[wsLog.length - 1];
  $("player-loaded").hidden = false;
  loadMarked("h1");
  assert.ok($("mark-controls").hidden && !$("selection-hint").hidden, "nothing selected yet");

  // A click on a mark on the waveform: it plays once, as before, and the bar shows it with Loop (no Mark EVP).
  context.regionClicked(reg("m1"), ev);
  assert.deepStrictEqual(lastPlay(), ["play", "mark-m1", true]);
  assert.ok(!$("mark-controls").hidden && $("selection-controls").hidden && $("selection-hint").hidden);
  assert.strictEqual($("active-mark-label").textContent, "EVP A · 0:01.0 – 0:01.5");
  assert.ok(!$("loop-mark").checked && !$("loop-mark").disabled);
  assert.ok(markRowEl(0).classList.contains("active") && !markRowEl(1).classList.contains("active"));
  assert.deepStrictEqual(looping(), ["m1", false]);
  context.regionOut(reg("m1"));                                     // not looping: it just ends
  assert.deepStrictEqual(lastPlay(), ["play", "mark-m1", true]);
  // Loop ticked while it plays: it carries on (the stop at its end dropped), and region-out replays it.
  $("loop-mark").checked = true; $("loop-mark").onchange();
  assert.deepStrictEqual(lastPlay(), ["ws.play", 1, undefined]);
  assert.deepStrictEqual(looping(), ["m1", true]);
  assert.strictEqual(loopBtn(0).getAttribute("aria-pressed"), "true");
  assert.strictEqual(loopBtn(0).title, "Stop looping this EVP");
  wsLog.length = 0;
  context.regionOut(reg("m1"));
  assert.deepStrictEqual(lastPlay(), ["play", "mark-m1", undefined]);   // from its start, on past its end
  context.regionOut(reg("m2"));                                     // another mark: no replay
  assert.strictEqual(wsLog.length, 1);
  // Its edges dragged while it loops: the bar and the loop use the new bounds at once.
  Object.assign(reg("m1"), { start: 0.5, end: 1.2 });
  context.regionUpdated(reg("m1"));
  assert.strictEqual($("active-mark-label").textContent, "EVP A · 0:00.5 – 0:01.2");
  context.regionOut(reg("m1"));
  assert.strictEqual(wsTime, 0.5);
  assert.deepStrictEqual(looping(), ["m1", true]);
  // A click on the mark itself keeps it; anywhere else on the waveform deselects it (and its loop).
  context.waveformClick({ composedPath: () => [{}, reg("m1").element, {}] });
  assert.deepStrictEqual(looping(), ["m1", true]);
  context.waveformClick({ composedPath: () => [{}] });
  assert.deepStrictEqual(looping(), [null, false]);
  assert.ok($("mark-controls").hidden && !$("selection-hint").hidden);
  wsLog.length = 0;
  context.regionOut(reg("m1"));
  assert.strictEqual(wsLog.length, 0, "no loop after deselecting");

  // The row's 🔁: loops that mark (the active one, Loop ticked); again: stops it, this pass ending at its end.
  wsPlaying = false;
  loopBtn(1).onclick(ev);
  assert.deepStrictEqual(looping(), ["m2", true]);
  assert.deepStrictEqual(lastPlay(), ["play", "mark-m2", false]);
  assert.ok($("loop-mark").checked && markRowEl(1).classList.contains("active"));
  assert.deepStrictEqual([loopBtn(0).getAttribute("aria-pressed"), loopBtn(1).getAttribute("aria-pressed")], ["false", "true"]);
  assert.strictEqual(loopBtn(1).getAttribute("aria-label"), "Stop looping this EVP");
  assert.strictEqual(loopBtn(1).tagName, "BUTTON", "a button: Tab and Enter/Space reach it");
  loopBtn(1).onclick(ev);
  assert.deepStrictEqual(looping(), ["m2", false]);
  assert.deepStrictEqual(lastPlay(), ["ws.play", 2, 2.4]);
  assert.strictEqual(loopBtn(1).getAttribute("aria-label"), "Loop this EVP");
  wsLog.length = 0;
  context.regionOut(reg("m2"));
  assert.strictEqual(wsLog.length, 0);
  loopBtn(1).onclick(ev);
  assert.deepStrictEqual(looping(), ["m2", true]);
  // A point marker has nothing to loop.
  assert.ok(loopBtn(2).disabled && /no length to loop/.test(loopBtn(2).title));
  // A click on a row selects it (a note being edited does not).
  markRowEl(0).onclick({ target: markRowEl(0).children[1] });
  assert.deepStrictEqual(looping(), ["m1", false]);
  markRowEl(1).onclick({ target: { tagName: "INPUT" } });
  assert.deepStrictEqual(looping(), ["m1", false]);

  // The selection and a mark never loop at the same time.
  loopBtn(1).onclick(ev);
  const sel = fakeRegions.addRegion({ id: "sel", start: 0.1, end: 0.3 });
  context.regionCreated(sel);
  assert.deepStrictEqual(looping(), [null, false], "a new selection takes the bar and the loop");
  assert.ok(!$("selection-controls").hidden && $("mark-controls").hidden);
  $("loop-selection").checked = true;
  wsLog.length = 0;
  context.regionOut(reg("m2"));
  context.regionOut(sel);
  assert.deepStrictEqual(wsLog, [["play", "sel", undefined]]);
  context.regionClicked(reg("m2"), ev);                             // a mark clicked: the selection goes
  assert.ok(vm.runInContext("S.region === null", context) && sel.removed && !$("loop-selection").checked);
  assert.deepStrictEqual(looping(), ["m2", false]);
  context.regionCreated(fakeRegions.addRegion({ id: "sel2", start: 0.1, end: 0.3 }));
  $("loop-selection").checked = true;
  loopBtn(0).onclick(ev);                                           // the row's 🔁 too
  assert.ok(vm.runInContext("S.region === null", context) && !$("loop-selection").checked);
  assert.deepStrictEqual(looping(), ["m1", true]);

  // The mark form takes the bar while open; then the active mark is back.
  vm.runInContext(`openMarkForm(S.marks[0])`, context);
  assert.ok($("mark-controls").hidden && !$("mark-form").hidden);
  context.closeMarkForm();
  assert.ok(!$("mark-controls").hidden);
  assert.deepStrictEqual(looping(), ["m1", true]);

  // Deleting the looping mark ends its loop.
  await context.deleteMark(vm.runInContext("S.marks[0]", context));
  assert.deepStrictEqual(looping(), [null, false]);
  assert.ok($("mark-controls").hidden);
  // So does loading another recording.
  loadMarked("h1");
  loopBtn(1).onclick(ev);
  const oldRegion = reg("m2");
  loadMarked("h2");
  assert.deepStrictEqual(looping(), [null, false]);
  assert.ok($("mark-controls").hidden);
  wsLog.length = 0;
  context.regionOut(oldRegion);
  assert.strictEqual(wsLog.length, 0);

  // A read-only store (a second window): looping is playback only, so it still works.
  vm.runInContext(`S.caps = { ...S.caps, marks_read_only: true }; renderMarkTools();`, context);
  loadMarked("h3");
  assert.ok(markRowEl(1).children.find((b) => b.textContent === "✎").disabled);
  assert.ok(!loopBtn(1).disabled);
  loopBtn(1).onclick(ev);
  assert.deepStrictEqual(looping(), ["m2", true]);
  wsLog.length = 0;
  context.regionOut(reg("m2"));
  assert.deepStrictEqual(wsLog, [["play", "mark-m2", undefined]]);
  context.regionClicked(reg("m1"), ev);
  assert.deepStrictEqual([looping(), lastPlay()], [["m1", false], ["play", "mark-m1", true]]);
  $("loop-mark").checked = true; $("loop-mark").onchange();
  assert.deepStrictEqual(looping(), ["m1", true]);
  vm.runInContext(`S.caps = { ...S.caps, marks_read_only: false }; renderMarkTools(); setCurrent(null);`, context);
  assert.deepStrictEqual(looping(), [null, false]);

  // A recording that can't be marked (the backend says why): no marks, no 🔁 rows, the mark tools off; the selection's own Loop
  const noMarksReason = "This recording has no audio to mark.";
  // still works (playback only), and the end of the file replays only a looping selection.
  vm.runInContext(`setCurrent("empty.wav", ${JSON.stringify({ rec: "h4", duration: 3, fp: null, marks: [],
    backup: { status: null, detail: "" }, reviewed: false, markable: false, mark_reason: noMarksReason })});
    drawMarks(S.playSeq);`, context);
  assert.ok($("marks-list").hidden && $("marks-list").children.length === 0 && $("mark-controls").hidden);
  assert.ok($("mark-evp").disabled && $("mark-evp").title === noMarksReason);
  $("loop-mark").checked = true; $("loop-mark").onchange();                       // no active mark: nothing loops
  assert.deepStrictEqual(looping(), [null, false]);
  wsLog.length = 0;
  context.playbackFinished();                                                      // the clip just ends
  assert.deepStrictEqual(wsLog, []);
  const noMarkSel = fakeRegions.addRegion({ id: "sel3", start: 0.2, end: 0.9 });
  context.regionCreated(noMarkSel);
  assert.ok(!$("selection-controls").hidden && $("mark-controls").hidden);
  $("loop-selection").checked = true;
  context.regionOut(noMarkSel);
  context.playbackFinished();                                                      // a selection that ends at the end
  assert.deepStrictEqual(wsLog, [["play", "sel3", undefined], ["play", "sel3", undefined]]);
  $("loop-selection").checked = false;
  wsLog.length = 0;
  context.regionOut(noMarkSel);
  context.playbackFinished();
  assert.deepStrictEqual(wsLog, []);
  vm.runInContext(`clearSelection(); setCurrent(null);`, context);
  assert.ok(!$("mark-evp").title.includes("no audio"));

  // ---- playback speed: a slider over 0.25× … 2× next to Zoom and Height, with Keep pitch ----
  // A media element and a WaveSurfer that play at its rate: time moves by dt × rate, and a region
  // whose end is crossed fires region-out, as the regions plugin does on timeupdate.
  const media = { playbackRate: 1, defaultPlaybackRate: 1, preservesPitch: true };
  const rateLog = [];
  let spPlaying = false, spTime = 0;
  const spWs = new Proxy({
    setPlaybackRate: (rate, keep) => { rateLog.push([rate, keep]); if (keep != null) media.preservesPitch = keep; media.playbackRate = rate; },
    getMediaElement: () => media,
    play: (t) => { spPlaying = true; if (t != null) spTime = t; }, pause: () => { spPlaying = false; },
    stop: () => { throw new Error("a speed change must not stop playback"); },
    isPlaying: () => spPlaying, getCurrentTime: () => spTime, getDuration: () => 3,
    getScroll: () => 0, setScroll: () => {}, zoom: () => {},
    playPause: () => { spPlaying = !spPlaying; },
    load: async () => { media.playbackRate = media.defaultPlaybackRate; },          // the HTML load algorithm
  }, { get: (t, k) => (k in t ? t[k] : anything) });
  context.__ws = spWs; context.__regions = fakeRegions;
  vm.runInContext("S.ws = __ws; S.regions = __regions;", context);
  const speedCalls = [];
  api.set_playback_speed = async (speed, keep) => { speedCalls.push([speed, keep]);
                                                    return { ok: true, playback_speed: speed, keep_pitch: keep, remembered: true }; };
  const speedState = () => JSON.parse(vm.runInContext("JSON.stringify([S.speed, S.keepPitch])", context));
  const speedSaved = () => vm.runInContext("S.speedSave || Promise.resolve()", context);
  const key = (k, target = document.body, extra = {}) => fire([document], "keydown", { key: k, target, ...extra });
  const slide = (i) => { $("speed").value = String(i); $("speed").oninput(); };
  const BACKSLASH = String.fromCharCode(92);
  $("player-loaded").hidden = false;
  // Startup: 1× and Keep pitch (capabilities() said nothing), the label plain.
  assert.deepStrictEqual(speedState(), [1, true]);
  assert.strictEqual($("speed").value, "3");
  assert.strictEqual($("speed-value").textContent, "1×");
  assert.ok(!$("speed-label").classList.contains("changed") && $("keep-pitch").checked);
  assert.ok(html.includes(`[ slower, ] faster, ${BACKSLASH} back to 1×`), "the shortcuts are in its title");
  assert.ok(/<input id="speed" type="range" min="0" max="6" step="1" value="3">/.test(html), "7 steps, 1× in the middle");
  // The slider's steps: the label shows each, highlighted whenever it isn't 1×; it applies at once, mid-play.
  spPlaying = true; spTime = 1.2;
  const steps = [];
  for (let i = 0; i <= 6; i++) {
    slide(i);
    steps.push([$("speed-value").textContent, $("speed-label").classList.contains("changed"), media.playbackRate]);
  }
  assert.deepStrictEqual(steps, [["0.25×", true, 0.25], ["0.5×", true, 0.5], ["0.75×", true, 0.75], ["1×", false, 1],
                                 ["1.25×", true, 1.25], ["1.5×", true, 1.5], ["2×", true, 2]]);
  assert.ok(spPlaying && spTime === 1.2, "still playing, from where it was");
  assert.strictEqual(media.defaultPlaybackRate, 2);
  assert.ok(rateLog.every(([, keep]) => keep === true), "Keep pitch on: preservesPitch stays true");
  await speedSaved();
  assert.deepStrictEqual(speedCalls, [[2, true]]);              // remembered: one save, of the latest
  slide(5); await new Promise((r) => setImmediate(r)); slide(4); slide(0);   // changes while a save is on its way
  await speedSaved();
  assert.deepStrictEqual(speedCalls.slice(1), [[1.5, true], [0.25, true]]);   // saved after it, the last one last
  slide(6); await speedSaved();
  speedCalls.length = 0;
  // Double-click resets it to 1×.
  fire([$("speed")], "dblclick", { target: $("speed") });
  assert.deepStrictEqual([speedState(), $("speed").value, $("speed-value").textContent, media.playbackRate], [[1, true], "3", "1×", 1]);
  await speedSaved();
  fire([$("speed")], "dblclick", { target: $("speed") });                          // already 1×: nothing to save
  await speedSaved();
  assert.deepStrictEqual(speedCalls, [[1, true]]);
  // Keep pitch off: tape-style (preservesPitch false), at the same speed; on again: true.
  slide(1);
  rateLog.length = 0; speedCalls.length = 0;
  $("keep-pitch").checked = false; $("keep-pitch").onchange();
  assert.deepStrictEqual([rateLog[rateLog.length - 1], media.preservesPitch, speedState()], [[0.5, false], false, [0.5, false]]);
  $("keep-pitch").checked = true; $("keep-pitch").onchange();
  assert.deepStrictEqual([rateLog[rateLog.length - 1], media.preservesPitch], [[0.5, true], true]);
  await speedSaved();
  assert.deepStrictEqual(speedCalls, [[0.5, true]]);           // off and on again before it was saved
  $("keep-pitch").checked = false; $("keep-pitch").onchange();
  await speedSaved();
  assert.deepStrictEqual(speedCalls, [[0.5, true], [0.5, false]]);
  $("keep-pitch").checked = true; $("keep-pitch").onchange();
  await speedSaved();
  assert.ok(spPlaying && spTime === 1.2);
  // Keys: ] faster, [ slower (one step, stopping at the ends), \ back to 1×.
  speedCalls.length = 0;
  let ke = key("]");
  assert.ok(ke.defaultPrevented);
  assert.strictEqual(speedState()[0], 0.75);
  key("["); key("["); key("["); key("[");
  assert.strictEqual(speedState()[0], 0.25);
  key(BACKSLASH);
  assert.strictEqual(speedState()[0], 1);
  for (let i = 0; i < 9; i++) key("]");
  assert.strictEqual($("speed-value").textContent, "2×");
  key(BACKSLASH);
  // … but not while typing (a note, the mark form, a menu), with Ctrl, or with the player empty.
  for (const tag of ["input", "textarea", "select"]) {
    ke = key("]", document.createElement(tag));
    assert.ok(!ke.defaultPrevented && speedState()[0] === 1, tag);
  }
  const rangeEl = document.createElement("input"); rangeEl.type = "range";
  key("]", rangeEl);                                                                // the slider itself focused: it works
  assert.strictEqual(speedState()[0], 1.25);
  key(BACKSLASH, document.body, { ctrlKey: true });
  assert.strictEqual(speedState()[0], 1.25);
  $("player-loaded").hidden = true;
  key(BACKSLASH);
  assert.strictEqual(speedState()[0], 1.25);
  $("player-loaded").hidden = false;
  await speedSaved();
  assert.deepStrictEqual(speedCalls, [[1.25, true]]);
  // A refused save says why, and the speed stays (it applies for now).
  api.set_playback_speed = async () => ({ ok: false, error: "Unknown playback speed." });
  slide(1);
  await speedSaved();
  assert.ok($("banner-text").textContent.includes("Unknown playback speed."));
  assert.strictEqual(media.playbackRate, 0.5);
  context.banner("");
  api.set_playback_speed = async (speed, keep) => { speedCalls.push([speed, keep]);
                                                    return { ok: true, playback_speed: speed, keep_pitch: keep, remembered: false }; };
  // A new recording keeps the speed (a load resets playbackRate to defaultPlaybackRate, the speed).
  await context.loadIntoPlayer(vm.runInContext("S.playSeq", context), "Recording A-002",
                               { rec: "h9", duration: 3, fp: null, marks: [], url: "u", peaks: [], rate: 8000, channels: 1,
                                 backup: { status: null, detail: "" }, reviewed: false }, false);
  assert.deepStrictEqual([speedState(), media.playbackRate, $("speed-value").textContent], [[0.5, true], 0.5, "0.5×"]);
  // An MP3 (decoded to a WAV by the backend) the same.
  slide(6);
  await context.loadIntoPlayer(vm.runInContext("S.playSeq", context), "x_EVP-B_00m01.0s.mp3",
                               { rec: "h10", duration: 3, fp: "fpM", marks: [], url: "u2.wav", peaks: [], rate: 44100, channels: 1,
                                 backup: { status: null, detail: "" }, reviewed: false }, false);
  assert.deepStrictEqual([speedState(), media.playbackRate], [[2, true], 2]);
  // The remembered setting comes from capabilities() at startup: setupSpeed() applies it.
  vm.runInContext(`S.caps = { ...S.caps, playback_speed: 0.75, keep_pitch: false }; setupSpeed();`, context);
  assert.deepStrictEqual([speedState(), $("speed").value, media.playbackRate, media.preservesPitch, $("keep-pitch").checked],
                         [[0.75, false], "2", 0.75, false, false]);
  vm.runInContext(`S.caps = { ...S.caps, playback_speed: 3, keep_pitch: "x" }; setupSpeed();`, context);   // damaged: 1×, Keep pitch
  assert.deepStrictEqual(speedState(), [1, true]);
  // ---- exports at the current speed: "Exports at 0.5×" beside the speed, only when it isn't 1× ----
  const exportShown = () => [$("export-speed-label").hidden, $("export-speed").checked, $("export-speed-text").textContent];
  assert.deepStrictEqual(exportShown().slice(0, 1), [true], "hidden at 1×");
  slide(1);
  assert.deepStrictEqual(exportShown(), [false, true, "Exports at 0.5×"]);           // ticked when it leaves 1×
  slide(0);
  assert.deepStrictEqual(exportShown(), [false, true, "Exports at 0.25×"]);
  $("export-speed").checked = false; $("export-speed").onchange();                 // unticked: it stays so …
  slide(1);
  assert.deepStrictEqual(exportShown(), [false, false, "Exports at 0.5×"]);
  assert.deepStrictEqual(Array.from(context.exportSpeed()), [1, true]);
  slide(3);
  assert.deepStrictEqual(exportShown().slice(0, 1), [true]);
  slide(1);                                                                         // … until it is back at 1×
  assert.deepStrictEqual(exportShown(), [false, true, "Exports at 0.5×"]);
  // After a restart at a remembered 0.5×: the speed is kept, the box shows but is NOT ticked (exports at 1×);
  // only moving the speed off 1× in this session ticks it.
  vm.runInContext(`S.caps = { ...S.caps, playback_speed: 0.5, keep_pitch: true }; setupSpeed();`, context);
  assert.deepStrictEqual([speedState(), exportShown()], [[0.5, true], [false, false, "Exports at 0.5×"]]);
  assert.deepStrictEqual(Array.from(context.exportSpeed()), [1, true]);
  slide(2);                                                                         // 0.5× -> 0.75×: still not ticked
  assert.deepStrictEqual(exportShown(), [false, false, "Exports at 0.75×"]);
  slide(3); slide(1);                                                               // off 1× by the user: ticked
  assert.deepStrictEqual(exportShown(), [false, true, "Exports at 0.5×"]);
  vm.runInContext(`S.caps = { ...S.caps, playback_speed: 1, keep_pitch: true };`, context);
  // The player's exports pass the speed and Keep pitch: Export WAV with marks (with progress), Export clips and
  // Save clip; at 1×, or with the box unticked, 1 and true. The library's Export clips never passes a speed.
  const speedExports = [];
  const savedExports = { marked: api.export_marked, clips: api.export_clips, files: api.export_clips_files };
  let finishMarked = null;
  api.export_marked = (...a) => { speedExports.push(["marked", ...a]);
                                  return new Promise((res) => { finishMarked = () => res({ ok: true, saved: true, already: false, name: "x_0.5x.wav", folder_name: "" }); }); };
  api.export_clips = async (...a) => { speedExports.push(["clips", ...a]); return { ok: true, saved: 1, already: 0, names: [], notes: [], folder: null }; };
  api.export_clips_files = async (...a) => { speedExports.push(["files", ...a]); return { ok: true, job: a[1] }; };
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 1, exists: false, truncated: false,
                                    indexing: false, pending: 0, files: [], folders: [] });
  $("keep-pitch").checked = false; $("keep-pitch").onchange();
  loadMarked("h12", [{ id: "e1", start: 1, end: 1.5, cls: "A", note: "" }]);
  const marking = context.exportMarked();
  assert.strictEqual($("status").textContent, "Saving a WAV with the marks at 0.5×…");
  window.onBackendEvent("speed-progress", { done: 50, total: 200 });
  assert.deepStrictEqual([$("status").textContent, $("progress").hidden, $("progress-fill").style.width],
                         ["Saving a WAV with the marks at 0.5×… 25%", false, "25%"]);
  finishMarked(); await marking;
  assert.deepStrictEqual([$("status").textContent, $("progress").hidden], ["", true]);
  window.onBackendEvent("speed-progress", { done: 50, total: 200 });              // a late one: ignored
  assert.strictEqual($("status").textContent, "");
  await context.exportClips(null);
  await context.exportClips({ id: "e1" });
  await context.exportLibraryClips({ files: ["f1"] }, "one.wav");
  assert.ok($("banner-text").textContent.includes("Exporting the clips of one.wav…"));
  window.onBackendEvent("clips-done", { job: vm.runInContext("S.clips.job", context), saved: 0, already: 0, recordings: 0,
                                         skipped: [], notes: [], folder: null, cancelled: false });
  $("export-speed").checked = false; $("export-speed").onchange();
  await context.exportClips(null);
  slide(3); $("keep-pitch").checked = true; $("keep-pitch").onchange();
  await context.exportClips(null);
  const libJob = vm.runInContext("S.clips.job", context) + 1;
  assert.deepStrictEqual(JSON.parse(JSON.stringify(speedExports)), [
    ["marked", "h12", 0.5, false, null], ["clips", "h12", null, 0.5, false, null], ["clips", "h12", "e1", 0.5, false, null],
    ["files", ["f1"], libJob - 1], ["clips", "h12", null, 1, true, null], ["clips", "h12", null, 1, true, null]]);
  slide(1);
  assert.deepStrictEqual(Array.from(context.exportSpeed()), [0.5, true]);
  $("player-loaded").hidden = true;                                                // no player, no box shown: normal speed
  assert.deepStrictEqual(Array.from(context.exportSpeed()), [1, true]);
  $("player-loaded").hidden = false;
  slide(3);
  Object.assign(api, { export_marked: savedExports.marked, export_clips: savedExports.clips, export_clips_files: savedExports.files });
  context.banner("");
  // Looping still triggers at 0.5× and 2×: the selection's loop and a mark's loop, via the fake regions.
  for (const idx of [1, 6]) {
    slide(idx);
    const rate = media.playbackRate;
    vm.runInContext(`clearSelection(); setCurrent(null);`, context);
    loadMarked("h11", [{ id: "s1", start: 1, end: 1.5, cls: "A", note: "" }]);
    const run = (r, seconds) => {                         // play on for some wall-clock seconds at the media's rate
      let outs = 0;
      for (let tw = 0; tw < seconds; tw += 0.05) {
        const before = spTime;
        spTime += 0.05 * rate;
        if (before < r.end && spTime >= r.end) {
          outs++;
          const n = wsLog.length;
          context.regionOut(r);
          if (wsLog.length > n && wsLog[wsLog.length - 1][1] === r.id) spTime = r.start;   // replayed from its start
        }
      }
      return outs;
    };
    loopBtn(0).onclick(ev);
    spTime = 1; spPlaying = true;
    const markOuts = run(reg("s1"), 0.5 / rate * 3 + 0.2);   // three passes' worth of wall-clock time
    assert.ok(markOuts >= 3 && spTime >= 1 && spTime < 1.5 + 0.05 * rate, `mark loop at ${rate}×: ${markOuts}`);
    const loopSel = fakeRegions.addRegion({ id: `selsp${idx}`, start: 2, end: 2.5 });
    context.regionCreated(loopSel);
    $("loop-selection").checked = true;
    spTime = 2;
    const selOuts = run(loopSel, 0.5 / rate * 3 + 0.2);
    assert.ok(selOuts >= 3 && spTime >= 2 && spTime < 2.5 + 0.05 * rate, `selection loop at ${rate}×: ${selOuts}`);
    assert.strictEqual(media.playbackRate, rate, "a replay keeps the speed");
    wsLog.length = 0;
    context.playbackFinished();                                                    // the end of the file: it loops too
    assert.deepStrictEqual(wsLog, [["play", `selsp${idx}`, undefined]]);
    $("loop-selection").checked = false;
  }
  vm.runInContext(`clearSelection(); setCurrent(null);`, context);

  // ---- Enhance: Boost, Leveler, filters, Hum remover through Web Audio; never on unnoticed ----
  // The real media element (ours, asking for CORS) went to WaveSurfer.create().
  assert.ok(/<button type="button" role="tab" id="tab-enhance" data-tab="enhance" aria-controls="panel-enhance"/.test(html));
  assert.ok(/id="export-heard-label" hidden/.test(html) && /id="enhanced-tag"[^>]*hidden/.test(html));
  assert.ok(html.includes('<input id="boost" type="range" min="0" max="24" step="1" value="0">'));
  for (const id of ["leveler", "leveler-strength", "voice-filter", "cut-rumble", "cut-hiss", "hum", "enhance-reset"]) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  context.setSpeed(1); await speedSaved();
  // Startup with nothing remembered: all off, the plain label, no audio graph at all.
  const enh = () => JSON.parse(vm.runInContext("JSON.stringify(S.enh.settings)", context));
  // [the Enhance tab marked as changed, the "Enhanced" tag hidden, "Exports enhanced" hidden, ticked]
  const enhShown = () => [$("tab-enhance").classList.contains("changed") && $("tab-enhance").dataset.on === "1", $("enhanced-tag").hidden,
                          $("export-heard-label").hidden, $("export-heard").checked];
  assert.deepStrictEqual(enh(), { boost: 0, leveler: false, strength: "medium", voice: false, rumble: false, hiss: false, hum: "off" });
  assert.deepStrictEqual(enhShown(), [false, true, true, false]);
  assert.strictEqual(audioContexts.length, 0, "no AudioContext until something is on");
  assert.strictEqual($("tab-enhance").title, "Boost, Leveler, filters and noise reduction (what you hear; the file is never changed)");
  // A player with a media element playing at 0.5×, Keep pitch, mid-play.
  const enhMedia = { playbackRate: 0.5, defaultPlaybackRate: 0.5, preservesPitch: true };
  let enhPlaying = true;
  const enhWs = new Proxy({ getMediaElement: () => enhMedia, isPlaying: () => enhPlaying, getCurrentTime: () => 1.5,
                            getDuration: () => 3, setPlaybackRate: (r, k) => { enhMedia.playbackRate = r; enhMedia.preservesPitch = k; },
                            stop: () => { throw new Error("Enhance must not stop playback"); } },
                          { get: (t, k) => (k in t ? t[k] : anything) });
  context.__ws = enhWs;
  vm.runInContext("S.ws = __ws;", context);
  const enhCalls = [];
  api.set_enhance = async (st) => { enhCalls.push(st); return { ok: true, enhance: st, remembered: true }; };
  const enhSaved = () => vm.runInContext("S.enh.save || Promise.resolve()", context);
  vm.runInContext(`setCurrent("Recording A-001", { rec: "e1", duration: 3, fp: "fpe", rate: 8000, marks: [],
                                                      backup: { status: null, detail: "" }, reviewed: false });`, context);
  $("player-loaded").hidden = false;
  // Boost: the graph is made once, from the media element; gain then the soft limiter; applied at once.
  $("boost").value = "6"; $("boost").oninput();
  assert.strictEqual(audioContexts.length, 1);
  const actx = audioContexts[0];
  assert.strictEqual(actx.nodes.find((n) => n.kind === "source").media, enhMedia);
  assert.deepStrictEqual(audioChain(actx), [["gain", 1.9953], ["gain", 0.0156], ["shaper", 32769]]);
  assert.ok(actx.resumes >= 1 && actx.state === "running", "resumed (autoplay rules)");
  assert.deepStrictEqual(enhShown(), [true, false, false, true]);   // turned on by the user: exports follow
  assert.strictEqual($("boost-value").textContent, "+6 dB");
  assert.deepStrictEqual([enhMedia.playbackRate, enhMedia.preservesPitch, enhPlaying], [0.5, true, true]);
  // The limiter's curve: linear to 0.8, then soft towards 1 (the WaveShaper's input is scaled by 1/64).
  const shaper = actx.nodes.find((n) => n.kind === "shaper");
  const at = (v) => shaper.curve[Math.round((v / 64 + 1) * (shaper.curve.length - 1) / 2)];
  assert.ok(Math.abs(at(0.5) - 0.5) < 0.003 && Math.abs(at(-0.8) + 0.8) < 0.003 && at(1) < 0.96 && at(3) > 0.99 && at(64) <= 1);
  await enhSaved();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(enhCalls)), [{ boost: 6, leveler: false, strength: "medium", voice: false, rumble: false, hiss: false, hum: "off" }]);
  // Each filter, at this recording's 8 kHz: Q in dB for a high- or low-pass (Butterworth: -3.01 dB), a notch's as is.
  $("voice-filter").checked = true; $("voice-filter").onchange();
  $("cut-rumble").checked = true; $("cut-rumble").onchange();
  $("hum").value = "60"; $("hum").onchange();
  $("leveler").checked = true; $("leveler").onchange();
  $("leveler-strength").value = "strong"; $("leveler-strength").onchange();
  assert.strictEqual($("tab-enhance").title, "Boost, Leveler, filters and noise reduction (what you hear; the file is never changed). " +
                     "On now: Leveler (strong), Boost +6 dB, Voice filter, Cut rumble, Hum remover 60 Hz.");
  assert.deepStrictEqual(audioChain(actx), [["highpass", 120, -3.01], ["highpass", 300, -3.01], ["lowpass", 3400, -3.01],
                                            ["notch", 60, 12], ["notch", 120, 24], ["notch", 180, 36], ["notch", 240, 48],
                                            ["compressor", -42, 6, 8, 0.003, 0.2], ["gain", 1.9953], ["gain", 0.0156], ["shaper", 32769]]);
  assert.strictEqual(audioContexts.length, 1, "one graph, rebuilt in place");
  assert.strictEqual(actx.sources, 1, "the media element is routed once");
  assert.ok(!$("leveler-strength").disabled);
  // Cut hiss: nothing above 4 kHz at 8 kHz, so it is off (and says why); a 44.1 kHz recording gets it.
  $("cut-hiss").checked = true; $("cut-hiss").onchange();
  assert.ok($("cut-hiss").disabled && /no hiss band/.test($("cut-hiss-label").title));
  assert.ok(!audioChain(actx).some((st) => st[0] === "lowpass" && st[1] === 5000));
  vm.runInContext(`setCurrent("x.wav", { rec: "e2", duration: 3, fp: "fpx", rate: 44100, marks: [], backup: { status: null, detail: "" }, reviewed: false });`, context);
  assert.ok(!$("cut-hiss").disabled);
  assert.deepStrictEqual(audioChain(actx).slice(2, 5), [["lowpass", 3400, -3.01], ["lowpass", 5000, -3.01], ["notch", 60, 12]]);
  assert.deepStrictEqual([enhMedia.playbackRate, enhMedia.preservesPitch, enhPlaying], [0.5, true, true]);
  await enhSaved();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(enhCalls[enhCalls.length - 1])), { boost: 6, leveler: true, strength: "strong", voice: true, rumble: true, hiss: true, hum: "60" });
  // Exports pass the settings while "Exports enhanced" is ticked; unticked (or nothing on): null.
  const heardExports = [];
  const savedEnhApi = { clips: api.export_clips, marked: api.export_marked };
  api.export_clips = async (...a) => { heardExports.push(a); return { ok: true, saved: 1, already: 0, names: [], notes: [], folder: null }; };
  api.export_marked = async (...a) => { heardExports.push(a); return { ok: true, saved: true, already: false, name: "x_enhanced.wav", folder_name: "" }; };
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 1, exists: false, truncated: false, indexing: false, pending: 0, files: [], folders: [] });
  vm.runInContext(`S.marks = [{ id: "m1", start: 1, end: 1.5, cls: "A", note: "" }];`, context);
  await context.exportClips({ id: "m1" });
  const statusSeen = [];
  api.export_marked = async (...a) => { statusSeen.push($("status").textContent); heardExports.push(a);
                                        return { ok: true, saved: true, already: false, name: "x_enhanced.wav", folder_name: "" }; };
  await context.exportMarked();
  assert.deepStrictEqual(statusSeen, ["Saving a WAV with the marks, enhanced…"]);
  $("export-heard").checked = false; $("export-heard").onchange();
  await context.exportClips(null);
  const on = { boost: 6, leveler: true, strength: "strong", voice: true, rumble: true, hiss: true, hum: "60" };
  assert.deepStrictEqual(JSON.parse(JSON.stringify(heardExports)),
                         [["e2", "m1", 1, true, { enhance: on }], ["e2", 1, true, { enhance: on }], ["e2", null, 1, true, null]]);
  // Unticked, it stays so while things change; Reset turns everything off, and the source plays straight out.
  $("boost").value = "3"; $("boost").oninput();
  assert.strictEqual($("export-heard").checked, false);
  $("enhance-reset").onclick();
  assert.deepStrictEqual(enh(), { boost: 0, leveler: false, strength: "medium", voice: false, rumble: false, hiss: false, hum: "off" });
  assert.deepStrictEqual(audioChain(actx), []);
  assert.deepStrictEqual(enhShown(), [false, true, true, false]);
  assert.strictEqual(context.exportHeard(), null);
  // On again from all off: ticked again (as "Exports at 0.5×" is when the speed leaves 1×).
  $("hum").value = "50"; $("hum").onchange();
  assert.deepStrictEqual(enhShown(), [true, false, false, true]);
  assert.deepStrictEqual(audioChain(actx).map((st) => st[1]), [50, 100, 150, 200]);
  $("boost").value = "0"; fire([$("boost")], "dblclick", { target: $("boost") });
  // A player that is not shown exports as recorded.
  $("player-loaded").hidden = true;
  assert.strictEqual(context.exportHeard(), null);
  $("player-loaded").hidden = false;
  // Playing resumes a suspended context.
  actx.state = "suspended";
  context.resumeAudio(true);
  await settle();
  assert.strictEqual(actx.state, "running");
  assert.ok(!/couldn't start the audio/.test($("banner-text").textContent));
  // If it stays suspended, or resume() is refused, playing says so plainly (no silent playback).
  const realResume = actx.resume;
  actx.state = "suspended";
  actx.resume = () => Promise.resolve();                                           // resolves, still suspended
  context.resumeAudio(true);
  await settle();
  assert.match($("banner-text").textContent, /Enhance couldn't start the audio.*Click Play again/);
  assert.strictEqual($("banner-action").textContent, "Try again");
  context.banner("");
  actx.resume = () => Promise.reject(new Error("NotAllowedError"));
  context.resumeAudio(true);
  await settle();
  assert.match($("banner-text").textContent, /Enhance couldn't start the audio/);
  actx.resume = realResume;
  $("banner-action").onclick();                                                    // Try again: it runs now
  await settle();
  assert.strictEqual(actx.state, "running");
  actx.onstatechange();
  assert.ok($("banner").hidden || !/couldn't start/.test($("banner-text").textContent));
  context.resumeAudio(false);                                                      // a change while running: nothing said
  assert.ok(!/couldn't start/.test($("banner-text").textContent));
  // Remembered settings at the next start: on (and shown on), but "Exports enhanced" not ticked.
  vm.runInContext(`S.caps = { ...S.caps, enhance: { boost: 12, leveler: false, strength: "light", voice: true, rumble: false, hiss: false, hum: "off" } }; setupEnhance();`, context);
  assert.deepStrictEqual(enhShown(), [true, false, false, false]);
  assert.strictEqual(context.exportHeard(), null);
  vm.runInContext(`S.caps = { ...S.caps, enhance: { boost: "lots", voice: 3, hum: "50" } }; setupEnhance();`, context);   // damaged: field by field
  assert.deepStrictEqual(enh(), { boost: 0, leveler: false, strength: "medium", voice: false, rumble: false, hiss: false, hum: "50" });
  // A refused save says why; the settings still apply for now.
  api.set_enhance = async () => ({ ok: false, error: "Unknown enhancement settings." });
  $("cut-rumble").checked = true; $("cut-rumble").onchange();
  await enhSaved();
  assert.ok($("banner-text").textContent.includes("Unknown enhancement settings."));
  assert.strictEqual(audioChain(actx)[0][1], 120);
  $("enhance-reset").onclick();
  api.set_enhance = async (st) => ({ ok: true, enhance: st, remembered: true });
  await enhSaved();
  Object.assign(api, { export_clips: savedEnhApi.clips, export_marked: savedEnhApi.marked });
  context.banner("");
  vm.runInContext(`setCurrent(null);`, context);

  // ---- The player's settings as tabs: View, Speed, Enhance ----
  for (const [t, label] of [["view", "View"], ["speed", "Speed"], ["enhance", "Enhance"]]) {
    assert.ok(new RegExp(`role="tab" id="tab-${t}" data-tab="${t}" aria-controls="panel-${t}"`).test(html), t);
    assert.ok(new RegExp(`<div id="panel-${t}" class="tab-panel[^"]*" role="tabpanel" aria-labelledby="tab-${t}"`).test(html), t);
    assert.ok(html.includes(`>${label}<span class="tab-dot" hidden aria-hidden="true">•</span></button>`), label);
  }
  assert.ok(/<div id="player-tabs" role="tablist" aria-label="Player settings">/.test(html));
  // Each control is in its tab's panel.
  const inPanel = (id, panel) => html.indexOf(`id="${id}"`) > html.indexOf(`<div id="panel-${panel}"`) &&
    (panel === "enhance" || html.indexOf(`id="${id}"`) < html.indexOf(`<div id="panel-${{ view: "speed", speed: "enhance" }[panel]}"`));
  for (const id of ["zoom", "height", "spectrogram"]) assert.ok(inPanel(id, "view"), id);
  for (const id of ["speed", "speed-value", "keep-pitch", "export-speed"]) assert.ok(inPanel(id, "speed"), id);
  for (const id of ["boost", "leveler", "leveler-strength", "voice-filter", "cut-rumble", "cut-hiss", "hum", "reduce-noise",
                    "noise-amount", "export-heard", "enhance-reset"]) assert.ok(inPanel(id, "enhance"), id);
  const shownTab = () => ["view", "speed", "enhance"].filter((t) => !$(`panel-${t}`).hidden);
  const selected = () => ["view", "speed", "enhance"].filter((t) => $(`tab-${t}`)["aria-selected"] === "true");
  const tabMarked = () => ["view", "speed", "enhance"].filter((t) => $(`tab-${t}`).dataset.on === "1");
  const wsBeforeTabs = vm.runInContext("S.ws", context);
  context.__ws = spWs;
  vm.runInContext("S.ws = __ws;", context);
  // Startup with nothing remembered: View; the tabs reached with Tab are only the selected one.
  const stored = {};
  const realStorage = { get: window.localStorage.getItem, set: window.localStorage.setItem };
  window.localStorage.getItem = (k) => (k in stored ? stored[k] : null);
  window.localStorage.setItem = (k, v) => { stored[k] = String(v); };
  vm.runInContext("setupTabs()", context);
  assert.deepStrictEqual([shownTab(), selected(), $("tab-view").tabIndex, $("tab-speed").tabIndex], [["view"], ["view"], 0, -1]);
  // A click picks a tab (and it is remembered); the arrow keys move along (round), Home and End to the ends.
  $("tab-speed").onclick();
  assert.deepStrictEqual([shownTab(), selected(), document.activeElement, stored["openevp.player-tab"]],
                         [["speed"], ["speed"], $("tab-speed"), "speed"]);
  const tabKey = (k) => fire([$("player-tabs")], "keydown", { key: k, target: document.activeElement });
  let tk = tabKey("ArrowRight");
  assert.ok(tk.defaultPrevented);
  assert.deepStrictEqual([shownTab(), document.activeElement], [["enhance"], $("tab-enhance")]);
  tabKey("ArrowRight");
  assert.deepStrictEqual(shownTab(), ["view"]);                                     // round
  tabKey("ArrowLeft");
  assert.deepStrictEqual(shownTab(), ["enhance"]);
  tabKey("Home");
  assert.deepStrictEqual(shownTab(), ["view"]);
  tabKey("End");
  assert.deepStrictEqual([shownTab(), $("tab-enhance").tabIndex, $("tab-view").tabIndex], [["enhance"], 0, -1]);
  tk = tabKey("x");
  assert.ok(!tk.defaultPrevented && shownTab()[0] === "enhance");
  // Remembered: the next start opens on it; a damaged value falls back to View.
  vm.runInContext("setupTabs()", context);
  assert.deepStrictEqual(shownTab(), ["enhance"]);
  stored["openevp.player-tab"] = "nonsense";
  vm.runInContext("setupTabs()", context);
  assert.deepStrictEqual(shownTab(), ["view"]);
  // Shortcuts work whichever tab is shown: with Enhance shown, ] changes the speed and the Speed tab
  // gets its dot (and says what is on); \ takes it back.
  $("tab-enhance").onclick();
  context.setSpeed(1); await speedSaved();
  vm.runInContext(`S.keepPitch = true; showSpeed();`, context);
  assert.ok(!tabMarked().includes("speed"));
  key("]");
  assert.deepStrictEqual([speedState()[0], shownTab()], [1.25, ["enhance"]]);
  assert.ok(tabMarked().includes("speed"));
  assert.strictEqual($("tab-speed").title, "Playback speed and Keep pitch. On now: Speed 1.25×.");
  $("keep-pitch").checked = false; $("keep-pitch").onchange();
  assert.strictEqual($("tab-speed").title, "Playback speed and Keep pitch. On now: Speed 1.25×, Keep pitch off.");
  $("keep-pitch").checked = true; $("keep-pitch").onchange();
  key(BACKSLASH);
  assert.ok(!tabMarked().includes("speed") && $("tab-speed").title === "Playback speed and Keep pitch");
  await speedSaved();
  // With View shown: the Enhance tab's dot says Boost is on; M still opens the mark form; Space still plays.
  $("tab-view").onclick();
  loadMarked("htab", [{ id: "t1", start: 1, end: 1.5, cls: "A", note: "" }]);
  $("boost").value = "3"; $("boost").oninput();
  assert.ok(tabMarked().includes("enhance") && $("tab-enhance").title.endsWith("On now: Boost +3 dB."));
  assert.deepStrictEqual(shownTab(), ["view"], "a change never switches the tab");
  $("enhance-reset").onclick();
  assert.ok(!tabMarked().includes("enhance"));
  const tabSel = fakeRegions.addRegion({ id: "tabsel", start: 2, end: 2.5 });
  context.regionCreated(tabSel);
  key("m");
  assert.ok(vm.runInContext("!!S.markForm", context), "M opens the mark form with View shown");
  context.closeMarkForm();
  const spaceBefore = spPlaying;
  fire([document], "keydown", { code: "Space", key: " ", target: document.body });
  assert.strictEqual(spPlaying, !spaceBefore, "Space plays/pauses with View shown");
  fire([document], "keydown", { code: "Space", key: " ", target: document.body });
  // The mark loop works with the Speed tab shown.
  $("tab-speed").onclick();
  loopBtn(0).onclick(ev);
  assert.ok(vm.runInContext("S.markLoop && S.activeMark === 't1'", context));
  loopBtn(0).onclick(ev);
  // View's dot: the spectrogram turned off, Height raised, or the waveform zoomed in past fit
  // (fit-to-width itself is plain navigation: no dot). Its tooltip names the zoom, like Speed's "…×".
  vm.runInContext(`S.spec.on = true; showTabMarks();`, context);
  vm.runInContext(`S.zoomPx = 1200; showTabMarks();`, context);        // fit is 800/3 px/s here: 4.5×
  assert.ok(tabMarked().includes("view"), "zoomed in past fit marks View");
  assert.strictEqual($("tab-view").title, "Zoom, Height and the spectrogram. On now: Zoom 4.5×.");
  vm.runInContext(`S.zoomPx = 0; showTabMarks();`, context);
  assert.ok(!tabMarked().includes("view"), "back at fit (0): the dot clears");
  // The zoom slider and the mouse wheel over the waveform both move S.zoomPx, and both refresh the dot.
  $("zoom").value = String(context.zoomSlider(2000, vm.runInContext("S.zoomMax", context))); $("zoom").oninput();
  assert.ok(vm.runInContext("S.zoomPx", context) > 0 && tabMarked().includes("view"), "the slider marks View once zoomed in");
  $("zoom").value = "0"; $("zoom").oninput();
  assert.ok(vm.runInContext("S.zoomPx", context) === 0 && !tabMarked().includes("view"), "the slider back at fit clears the dot");
  fire([$("waveform")], "wheel", { deltaY: -100, clientX: 400 });
  assert.ok(vm.runInContext("S.zoomPx", context) > 0 && tabMarked().includes("view"), "wheel-zooming in marks View");
  for (let i = 0; i < 20 && vm.runInContext("S.zoomPx", context) > 0; i++) fire([$("waveform")], "wheel", { deltaY: 100, clientX: 400 });
  assert.ok(vm.runInContext("S.zoomPx", context) === 0 && !tabMarked().includes("view"), "wheel-zooming back out to fit clears the dot");
  $("height").value = "20"; $("height").oninput();
  assert.ok(tabMarked().includes("view") && $("tab-view").title.endsWith("On now: Height raised."));
  $("height").value = "1"; $("height").oninput();
  vm.runInContext(`S.spec.on = false; showTabMarks();`, context);
  assert.ok(tabMarked().includes("view") && $("tab-view").title.endsWith("On now: Spectrogram off."));
  vm.runInContext(`S.spec.on = true; showTabMarks(); clearSelection(); setCurrent(null); S.enh.exportHeard = false; showEnhance();`, context);
  window.localStorage.getItem = realStorage.get; window.localStorage.setItem = realStorage.set;
  $("tab-view").onclick();
  context.__ws = wsBeforeTabs;
  vm.runInContext("S.ws = __ws;", context);
  // ---- Spectrogram: tiles from the backend, the level of detail for the zoom, only those in view ----
  assert.ok(/<input id="spectrogram" type="checkbox"> Spectrogram<\/label>/.test(html));
  // On by default: capabilities() said nothing (a new user), so it is on; only an explicit false turns it off.
  assert.ok($("spectrogram").checked && vm.runInContext("S.spec.on", context), "on unless turned off");
  vm.runInContext(`S.caps = { ...S.caps, spectrogram: false }; setupSpectrogram();`, context);
  assert.ok(!$("spectrogram").checked && !vm.runInContext("S.spec.on", context), "turned off: stays off");
  vm.runInContext(`S.caps = { ...S.caps, spectrogram: true }; setupSpectrogram();`, context);
  assert.ok($("spectrogram").checked);
  vm.runInContext(`S.spec.on = false; $("spectrogram").checked = false;`, context);
  const wrapper = document.createElement("div");
  let wrapW = 800, wsScroll = 0;
  Object.defineProperty(wrapper, "clientWidth", { get: () => wrapW });
  const specWs = new Proxy({ getWrapper: () => wrapper, getScroll: () => wsScroll, getDuration: () => 100, getMediaElement: () => enhMedia },
                           { get: (t, k) => (k in t ? t[k] : anything) });
  context.__ws = specWs;
  vm.runInContext("S.ws = __ws;", context);
  const specCalls = [], specSaves = [];
  const specInfo = { ok: true, tiles: "http://t/spec/abc", columns: 12500, rows: 129, column_seconds: 0.008, levels: 6, tile: 512,
                     fmax: 4000, fft: 256 };
  let specAnswer = null;
  api.spectrogram = (rec, url) => { specCalls.push([rec, url]); return new Promise((res) => { specAnswer = res; }); };
  api.set_spectrogram = async (on) => { specSaves.push(on); return { ok: true, spectrogram: on, remembered: true }; };
  const loadSp = (rec, extra = {}) => vm.runInContext(`setCurrent("Recording", ${JSON.stringify({ rec, duration: 100, fp: "f" + rec, rate: 8000,
    url: `http://a/${rec}.wav`, marks: [], backup: { status: null, detail: "" }, reviewed: false, ...extra })});`, context);
  const specEl = () => vm.runInContext("S.spec.el", context);
  const tiles = () => [...vm.runInContext("S.spec.imgs", context).keys()];
  const answer = async (r) => { specAnswer(r); await new Promise((res) => setImmediate(res)); };
  loadSp("s1");
  $("spectrogram").checked = true; $("spectrogram").onchange();
  assert.deepStrictEqual(specCalls, [["s1", "http://a/s1.wav"]]);                  // the audio the player plays
  assert.ok(wrapper.children.includes(specEl()), "inside wavesurfer's wrapper: scrolled and zoomed with it");
  assert.strictEqual(specEl().msg.textContent, "Computing the spectrogram…");
  await answer(specInfo);
  assert.strictEqual(specEl().msg.textContent, "");
  assert.deepStrictEqual(specEl().labels.children.map((c) => [c.textContent, c.style.bottom]),
                         [["1 kHz", "25.00%"], ["2 kHz", "50.00%"], ["3 kHz", "75.00%"]]);
  // Fit to 800 px: level 3 (782 columns would be too few, 1563 is enough); its 4 tiles, placed by time.
  assert.deepStrictEqual(tiles(), ["3/0", "3/1", "3/2", "3/3"]);
  const img0 = vm.runInContext(`S.spec.imgs.get("3/1")`, context);
  assert.deepStrictEqual([img0.src, img0.style.left, img0.style.width, img0.style.imageRendering],
                         ["http://t/spec/abc/3/1.png", "32.76400%", "32.76800%", "auto"]);
  await vm.runInContext("S.spec.save || Promise.resolve()", context);
  assert.deepStrictEqual(specSaves, [true]);
  // Zoomed in (400 px per second) and scrolled: full detail, only the tiles in view (and one each side).
  wrapW = 40000; wsScroll = 20000;
  context.renderSpectrogram();
  assert.deepStrictEqual(tiles(), ["0/11", "0/12", "0/13"]);
  assert.strictEqual(vm.runInContext(`S.spec.imgs.get("0/12").style.imageRendering`, context), "pixelated");
  assert.strictEqual(specEl().children.filter((c) => c.tagName === "IMG").length, 3, "tiles out of view are removed");
  wrapW = 800; wsScroll = 0;
  // Another recording before the answer: that answer is dropped; the new one asks again.
  loadSp("s2");
  assert.deepStrictEqual(tiles(), []);
  const late = specAnswer;
  vm.runInContext("loadSpectrogram()", context);
  const second = specAnswer;
  late(specInfo); await new Promise((res) => setImmediate(res));
  assert.deepStrictEqual(tiles(), []);
  specAnswer = second;
  await answer({ ok: false, error: "No spectrogram for x: the file changed on disk: load it again" });
  assert.strictEqual(specEl().msg.textContent, "No spectrogram for x: the file changed on disk: load it again");
  // An MP3 (a recording or a clip) is decoded to WAV by the backend: it gets one like any other.
  specCalls.length = 0;
  loadSp("s3");
  vm.runInContext("loadSpectrogram()", context);
  assert.deepStrictEqual(specCalls, [["s3", "http://a/s3.wav"]]);
  await answer(specInfo);
  // Off: gone, and remembered.
  let specCancels = 0;
  api.cancel_spectrogram = async () => { specCancels++; return { ok: true }; };
  loadSp("s5");                                                                    // a request in flight, then unloaded
  vm.runInContext("loadSpectrogram()", context);
  vm.runInContext("hideSpectrogram()", context);
  assert.strictEqual(specCancels, 1, "the backend job is cancelled");
  vm.runInContext("hideSpectrogram()", context);
  assert.strictEqual(specCancels, 1, "nothing to cancel");
  loadSp("s4");
  vm.runInContext("loadSpectrogram()", context);
  await answer(specInfo);
  assert.strictEqual(tiles().length, 4);
  $("spectrogram").checked = false; $("spectrogram").onchange();
  assert.ok(specEl() === null && tiles().length === 0 && !wrapper.children.some((c) => c.className === "spectrogram"));
  await vm.runInContext("S.spec.save || Promise.resolve()", context);
  assert.deepStrictEqual(specSaves, [true, false]);
  // Remembered on at the next start.
  vm.runInContext(`S.caps = { ...S.caps, spectrogram: true }; setupSpectrogram();`, context);
  assert.ok($("spectrogram").checked && vm.runInContext("S.spec.on", context));
  // Opening a recording: the waveform is loaded and drawn first; the spectrogram is only asked for
  // a moment later (a timer), and not at all if another recording was opened meanwhile.
  const timers = [];
  context.setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
  specCalls.length = 0;
  const opened = { rec: "s6", duration: 100, fp: "fs6", rate: 8000, url: "http://a/s6.wav", peaks: [0.5], marks: [],
                   backup: { status: null, detail: "" }, reviewed: false };
  await context.loadIntoPlayer(vm.runInContext("++S.playSeq", context), "Recording s6", opened, false);
  assert.deepStrictEqual(specCalls, [], "nothing asked before the waveform is up");
  const specTimer = timers.find(([, ms]) => ms === 150);
  assert.ok(specTimer, "asked shortly after");
  specTimer[0]();
  assert.deepStrictEqual(specCalls, [["s6", "http://a/s6.wav"]]);
  await answer(specInfo);
  assert.strictEqual(tiles().length, 4);
  timers.length = 0; specCalls.length = 0;
  await context.loadIntoPlayer(vm.runInContext("++S.playSeq", context), "Recording s7", { ...opened, rec: "s7", url: "http://a/s7.wav" }, false);
  const stale = timers.find(([, ms]) => ms === 150);
  vm.runInContext("S.playSeq++", context);                                         // another recording opened meanwhile
  stale[0]();
  assert.deepStrictEqual(specCalls, []);
  vm.runInContext(`S.spec.on = false; $("spectrogram").checked = false;`, context);   // off: no timer at all
  timers.length = 0;
  await context.loadIntoPlayer(vm.runInContext("++S.playSeq", context), "Recording s8", { ...opened, rec: "s8" }, false);
  assert.ok(!timers.some(([, ms]) => ms === 150));
  context.setTimeout = () => 0;
  vm.runInContext(`S.spec.on = false; hideSpectrogram(); setCurrent(null);`, context);

  // ---- Noise reduction: Learn noise from a selection, Reduce noise plays a noise-reduced version ----
  assert.ok(html.includes('<button id="learn-noise"'));
  assert.ok(/id="reduce-noise"/.test(html) && /<input id="noise-amount" type="range" min="0" max="100" step="5" value="40"/.test(html));
  assert.ok(/watery, warbling artefacts that can sound like whispers or voices/.test(html), "the tooltip warns");
  const nLog = [];
  let nTime = 12.5, nPlaying = true;
  const nWs = new Proxy({ getMediaElement: () => enhMedia, getWrapper: () => wrapper, getScroll: () => 0, getDuration: () => 100,
                          getCurrentTime: () => nTime, isPlaying: () => nPlaying,
                          load: async (...a) => { nLog.push(JSON.parse(JSON.stringify(["load", ...a]))); nPlaying = false; },
                          setTime: (t) => { nLog.push(["setTime", t]); nTime = t; }, play: () => { nLog.push(["play"]); nPlaying = true; },
                          setOptions: () => {} },
                        { get: (t, k) => (k in t ? t[k] : anything) });
  context.__ws = nWs;
  vm.runInContext("S.ws = __ws;", context);
  const nCalls = [];
  let reduceAnswer = null;
  api.learn_noise = async (...a) => { nCalls.push(["learn", ...a]); return { ok: true, profile: "p1", seconds: 0.8 }; };
  api.reduce_noise = (...a) => { nCalls.push(["reduce", ...a]); return new Promise((res) => { reduceAnswer = res; }); };
  api.cancel_denoise = async (job) => { nCalls.push(["cancel", job]); return { ok: true }; };
  api.spectrogram = async () => ({ ok: false, error: "x" });
  const loadN = (rec, extra = {}) => vm.runInContext(`setCurrent("Recording", ${JSON.stringify({ rec, duration: 100, fp: "fp-" + rec, rate: 44100,
    url: `http://a/${rec}.wav`, peaks: [0.5], marks: [], backup: { status: null, detail: "" }, reviewed: false, ...extra })});`, context);
  loadN("n1");
  vm.runInContext("S.current.full = false;", context);
  assert.ok($("reduce-noise").disabled && $("noise-amount").disabled, "no profile yet");
  assert.match($("noise-status").textContent, /background noise only/);
  assert.ok(!$("learn-noise").disabled);
  // Learn noise from the selection.
  const nSel = fakeRegions.addRegion({ id: "nsel", start: 2, end: 3.25 });
  context.regionCreated(nSel);
  await context.learnNoise();
  assert.deepStrictEqual(nCalls, [["learn", "n1", 2, 3.25]]);
  assert.ok(!$("reduce-noise").disabled);
  assert.strictEqual($("noise-status").textContent, "Noise learnt from 0:02.0 – 0:03.3.");
  assert.match($("banner-text").textContent, /Noise learnt/);
  // Reduce noise: progress with Cancel, then the noise-reduced audio plays from where it was.
  $("reduce-noise").checked = true; $("reduce-noise").onchange();
  const job1 = vm.runInContext("S.noise.job", context);
  assert.deepStrictEqual(nCalls[1], ["reduce", "n1", "p1", 40, job1, "http://a/n1.wav"]);
  assert.strictEqual($("noise-status").textContent, "Reducing the noise…");
  window.onBackendEvent("denoise-progress", { job: job1, done: 25, total: 100 });
  assert.deepStrictEqual([$("banner-text").textContent, $("banner-action").textContent, $("progress-fill").style.width],
                         ["Reducing the noise… 25%", "Cancel", "25%"]);
  window.onBackendEvent("denoise-progress", { job: job1 + 7, done: 90, total: 100 });             // another job's: ignored
  assert.strictEqual($("banner-text").textContent, "Reducing the noise… 25%");
  assert.ok($("enhanced-tag").hidden, "not on until it plays");
  reduceAnswer({ ok: true, url: "http://a/n1-dn.wav", peaks: [0.2], duration: 100, rate: 44100, channels: 1 });
  await settle(); await settle();
  assert.deepStrictEqual(nLog, [["load", "http://a/n1-dn.wav", [[0.2]], 100], ["setTime", 12.5], ["play"]]);
  assert.strictEqual(vm.runInContext("S.current.playing", context), "http://a/n1-dn.wav");
  assert.deepStrictEqual(enhShown(), [true, false, false, true]);     // on, and exports follow (turned on now)
  assert.ok($("banner").hidden && $("progress").hidden);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(context.exportHeard())), { denoise: { profile: "p1", amount: 40 } });
  assert.strictEqual(vm.runInContext("S.current.fp", context), "fp-n1", "marks stay the recording's");
  // A new amount: made again (the old one kept playing meanwhile); a newer answer wins.
  $("noise-amount").value = "60"; $("noise-amount").oninput();
  assert.strictEqual($("noise-amount-value").textContent, "60%");
  $("noise-amount").onchange();
  const job2 = vm.runInContext("S.noise.job", context);
  assert.deepStrictEqual(nCalls.slice(2), [["reduce", "n1", "p1", 60, job2, "http://a/n1-dn.wav"]]);      // the first had finished: nothing to cancel
  nLog.length = 0; nPlaying = false; nTime = 40;
  reduceAnswer({ ok: true, url: "http://a/n1-dn60.wav", peaks: [0.1], duration: 100, rate: 44100, channels: 1 });
  await settle(); await settle();
  assert.deepStrictEqual(nLog, [["load", "http://a/n1-dn60.wav", [[0.1]], 100], ["setTime", 40]]);   // paused: stays paused
  assert.deepStrictEqual(JSON.parse(JSON.stringify(context.exportHeard())), { denoise: { profile: "p1", amount: 60 } });
  // With Enhance on too, both go to exports.
  $("cut-rumble").checked = true; $("cut-rumble").onchange();
  assert.deepStrictEqual(Object.keys(context.exportHeard()), ["enhance", "denoise"]);
  $("cut-rumble").checked = false; $("cut-rumble").onchange();
  // Cancel while it runs: the job is stopped, Reduce noise goes off, the recording's own audio plays.
  $("noise-amount").value = "20"; $("noise-amount").onchange();
  const job2b = vm.runInContext("S.noise.job", context);
  $("noise-amount").value = "30"; $("noise-amount").onchange();                     // changed again while it runs
  const job3 = vm.runInContext("S.noise.job", context);
  assert.deepStrictEqual(nCalls.slice(-2), [["cancel", job2b], ["reduce", "n1", "p1", 30, job3, "http://a/n1-dn60.wav"]]);
  nLog.length = 0;
  $("banner-action").onclick();
  assert.deepStrictEqual(nCalls[nCalls.length - 1], ["cancel", job3]);
  assert.ok(!$("reduce-noise").checked && !vm.runInContext("S.noise.on", context));
  await settle();
  assert.deepStrictEqual(nLog, [["load", "http://a/n1.wav", [[0.5]], 100], ["setTime", 40]]);
  reduceAnswer({ ok: false, cancelled: true, error: "Stopped." });
  await settle();
  assert.deepStrictEqual(enhShown(), [false, true, true, true]);
  assert.strictEqual(context.exportHeard(), null);
  // A failure says why and turns it off.
  $("reduce-noise").checked = true; $("reduce-noise").onchange();
  reduceAnswer({ ok: false, error: "Could not reduce the noise of x: no memory." });
  await settle();
  assert.ok(!$("reduce-noise").checked && $("banner-text").textContent.includes("Could not reduce the noise"));
  // Another recording: off, and no profile; back to the first one: its profile is still there (this session).
  loadN("n2");
  assert.ok($("reduce-noise").disabled && !$("reduce-noise").checked);
  loadN("n1");
  assert.ok(!$("reduce-noise").disabled && !$("reduce-noise").checked);
  // Audio without a fingerprint (an empty WAV) can't learn noise.
  loadN("n3", { fp: null });
  assert.ok($("learn-noise").disabled && /no audio/.test($("learn-noise").title));
  // Reset turns Reduce noise off too.
  loadN("n1");
  vm.runInContext("S.current.full = true;", context);
  $("reduce-noise").checked = true; $("reduce-noise").onchange();
  reduceAnswer({ ok: true, url: "http://a/n1-dn.wav", peaks: [0.2], duration: 100, rate: 44100, channels: 1 });
  await settle(); await settle();
  const lastLoad = nLog.filter((x) => x[0] === "load").pop();
  assert.deepStrictEqual(lastLoad, ["load", "http://a/n1-dn.wav"]);                   // full detail: decoded by the page
  $("enhance-reset").onclick();
  assert.ok(!$("reduce-noise").checked && vm.runInContext("S.current.playing", context) === "http://a/n1.wav");
  context.banner("");
  vm.runInContext(`clearSelection(); setCurrent(null);`, context);
  context.__ws = realPlayer[0]; context.__regions = realPlayer[1];
  vm.runInContext("S.ws = __ws; S.regions = __regions;", context);

  // The update dialog lists every release the update skips over, newest first, each under its
  // version heading with its notes beneath; markup in the notes stays text.
  api.check_update = async () => ({ ok: true, available: true, current: "0.9.7", version: "0.10.0", notes: "n",
    page: "p", can_install: true, earlier: 2, releases: [
      { version: "0.10.0", date: "2026-10-01", notes: "## Fixes\n- <img src=x onerror=alert(1)>" },
      { version: "0.9.9", date: "2026-09-20", notes: "y".repeat(800) },
      { version: "0.9.8", date: "2026-09-10", notes: "" }] });
  await context.checkForUpdate(false);
  const dlg = $("update-notes");
  assert.ok(!$("update-dialog").hidden);
  assert.strictEqual($("update-title").textContent, "OpenEVP 0.10.0 is available");
  const sections = dlg.children.filter((c) => c.tagName === "DETAILS");
  assert.deepStrictEqual(sections.map((d) => d.children[0].children[0].textContent), ["What's new in 0.10.0", "0.9.9", "0.9.8"]);
  assert.deepStrictEqual(sections.map((d) => d.open), [true, false, false]);
  const first = sections[0].children[1];
  assert.deepStrictEqual(first.children.map((c) => c.tagName), ["H4", "UL"]);
  assert.strictEqual(first.children[1].textContent, "<img src=x onerror=alert(1)>");
  assert.ok(!dlg.innerHTML && !first.children[1].children[0].innerHTML, "notes are built from text, not HTML");
  assert.strictEqual(sections[2].children[1].textContent, "No notes for this release.");
  assert.strictEqual(dlg.lastChild.textContent, "…and 2 earlier updates.");
  $("update-later").onclick();
  assert.ok($("update-dialog").hidden);
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
