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

let ready = null;
const window = {
  pywebview: { api },
  addEventListener(name, fn, capture) { if (name === "pywebviewready") ready = fn; else listen(window, name, fn, capture); },
  innerWidth: 1000, innerHeight: 700,
  localStorage: { getItem: () => null, setItem() {} },
};
const context = { window, document, WaveSurfer: anything, console, getComputedStyle: () => ({ getPropertyValue: () => "" }),
                  setTimeout: () => 0, clearTimeout() {}, requestAnimationFrame: () => 0, localStorage: window.localStorage,
                  Map, Set, JSON, Promise, Number, String, Math, Object, Array, RegExp };
vm.createContext(context);
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
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", true], ["Delete…", true], ["Export clips", true]]);
  assert.strictEqual(menu.children[3].title, vm.runInContext("readOnlyTip()", context));   // says why
  press("Escape");
  vm.runInContext(`S.caps.marks_read_only = false; renderLibraryBar();`, context);
  assert.ok(!$("library-new").disabled && $("library-tools").title === "");

  // A folder row: Open / Rename / Delete; it becomes the selected folder; keys move and choose.
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", false], ["Delete…", false], ["Export clips", false]]);
  assert.strictEqual(L.selFolder, "f1");
  assert.strictEqual(document.activeElement.textContent, "Open");
  press("ArrowUp");
  assert.strictEqual(document.activeElement.textContent, "Export clips");  // wraps round
  press("ArrowUp");
  assert.strictEqual(document.activeElement.textContent, "Delete…");
  press("ArrowDown"); press("ArrowDown"); press("ArrowDown");
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
  // Off exactly when the toolbar's buttons are: during an operation, and for the library's root.
  L.selFolder = "f1"; L.op = true; context.renderLibrary();
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", true], ["Delete…", true], ["Export clips", true]]);
  assert.ok($("library-rename").disabled && $("library-delete").disabled);
  press("Escape");
  assert.ok(menu.hidden, "Escape closes it");
  L.op = false; context.renderLibrary();
  assert.ok(!$("library-rename").disabled);
  const rootRow = document.createElement("tr"); rootRow.className = "lib-folder"; rootRow.folderId = "root";
  rightClick(rootRow);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", true], ["Delete…", true], ["Export clips", false]]);
  L.selFolder = "root"; context.renderLibraryBar();
  assert.ok($("library-rename").disabled && $("library-delete").disabled);
  fire([window], "pointerdown", { target: document.body });
  assert.ok(menu.hidden, "a click outside closes it");
  L.selFolder = null;

  // A recording row: Play and Move to… for that row; a ticked row stands for every ticked one.
  const realTimeout = context.setTimeout;
  context.setTimeout = (fn, ms) => { if (!ms) setImmediate(fn); return 0; };   // finishFolderOp runs
  rightClick(recRow("r3").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Rename…", false], ["Move to…", false], ["Export clips", true]]);
  choose("Play");
  await settle();
  assert.deepStrictEqual(ops.pop(), ["play", "r3"]);
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
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Rename…", false], ["Move 2 recordings to…", false], ["Export clips", true]]);
  choose("Move 2 recordings to…");
  assert.strictEqual($("folder-dialog-title").textContent, "Move 2 recordings to…");
  pick("f2").onclick();
  await context.folderDialogOk();
  await settle();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.pop())), ["move", ["r1", "r2"], "f2"]);
  context.pickGroup(recRow("r1").group, true); context.pickGroup(recRow("r2").group, true);
  context.renderLibrary();
  rightClick(recRow("r3").cells[1]);                                  // not ticked: just itself
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Rename…", false], ["Move to…", false], ["Export clips", true]]);
  L.op = true;
  rightClick(recRow("r3").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Rename…", true], ["Move to…", true], ["Export clips", true]]);
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
  assert.strictEqual(menu.children[1].title, "Renames this recording only");
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
  assert.deepStrictEqual(menuItems()[1], ["Rename…", true]);
  press("Escape");
  L.op = false;
  vm.runInContext(`S.caps.marks_read_only = true;`, context);
  rightClick(recRow("r4").cells[1]);
  assert.deepStrictEqual(menuItems()[1], ["Rename…", true]);
  press("Escape");
  press("F2");
  assert.ok(!dialogShown());
  vm.runInContext(`S.caps.marks_read_only = false;`, context);
  document.activeElement = null;
  context.setTimeout = realTimeout;

  // All recordings (no folders): empty space offers nothing, but the browser menu stays off there.
  L.flat = true; context.renderLibrary();
  e = rightClick($("empty"));
  assert.ok(e.defaultPrevented && menu.hidden);
  rightClick(recRow("r1").cells[1]);
  assert.deepStrictEqual(menuItems().map(([t]) => t), ["Play", "Rename…", "Move 2 recordings to…", "Export clips"]);
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
  assert.deepStrictEqual(menuItems()[3], ["Export clips", true]);                  // nothing marked in it
  assert.strictEqual(menu.children[3].title, "No EVPs marked in this recording");
  press("Escape");
  rightClick(recRow("r4").cells[1]);
  assert.deepStrictEqual(menuItems()[3], ["Export clips", false]);
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
  assert.deepStrictEqual(menuItems()[3], ["Export clips", true]);
  assert.strictEqual(menu.children[3].title, "Wait for the clips being exported");
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
  assert.deepStrictEqual(menuItems()[3], ["Export clips", true]);
  assert.strictEqual(menu.children[3].title, "Wait for the WAV or clips being saved");
  press("Escape");
  vm.runInContext(`S.exportingMarked = false; renderLibrary();`, context);
  assert.ok($("banner-action").hidden);                                            // nothing to open
  await settle();
  // A folder: its id; the summary counts what was already there and what was skipped, and why.
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems()[3], ["Export clips", false]);
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
  assert.match(menu.children[3].title, /as its own WAV clip$/);
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
  assert.match(menu.children[3].title, /as its own MP3 clip$/);
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
  assert.deepStrictEqual(menuItems()[3], ["Export clips", true]);                  // no marks: nothing to cut
  press("Escape");

  // A Clips folder OpenEVP made: listed with its own icon and "Clips" tag, its clips counted apart and
  // playable, but never EVPs (even one marked in the player): no chips, no filter, no count, not in the
  // All recordings view; Export clips is off for it and its clips; recordings can't be moved into it.
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
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", false], ["Delete…", false], ["Export clips", true]]);
  assert.strictEqual(menu.children[3].title, "These are clips already");
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
  // Inside it: the clips, each "Clip" in the EVP column; one plays; Export clips is off.
  context.openLibraryFolder("fc");
  await settle();
  assert.deepStrictEqual(rowsNow().map((r) => r.group.main.id), ["c1", "c2"]);
  const clipRow = recRow("c1");
  assert.ok(clipRow.classList.contains("lib-clip"));
  assert.strictEqual(clipRow.cells[5].textContent, "Clip");
  assert.match(clipRow.cells[5].title, /2 marks of its own, not counted as EVPs/);
  assert.strictEqual(recRow("c2").cells[5].textContent, "Clip");
  rightClick(clipRow.cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Rename…", false], ["Move to…", false], ["Export clips", true]]);
  assert.strictEqual(menu.children[3].title, "This is a clip already");
  press("Escape");
  ops.length = 0;
  clipRow.onclick();
  await settle();
  assert.deepStrictEqual(ops, [["play", "c1"]]);
  // The All recordings view: recordings only.
  vm.runInContext(`S.lib.flat = true; renderLibrary();`, context);
  assert.deepStrictEqual(rowsNow().map((r) => r.group.main.id), ["r1"]);
  vm.runInContext(`S.lib.flat = false; renderLibrary();`, context);

  // An MP3 clip (the default clip format): listed and playable in the Clips folder; the page loads the
  // MP3 itself (no server peaks: wavesurfer decodes it at its own rate) and the mark tools are off.
  const mp3Reason = "MP3 clips can't be marked. Mark the recording itself, or export the clip as WAV to mark it.";
  vm.runInContext(`S.caps.formats.mp3 = { label: "MP3 clip", playable: true, reason: null };`, context);
  api.list_library = async () => ({ ok: true, folder: "C:\\save", scan_id: 61, exists: true, truncated: false, indexing: false,
                                    pending: 0, folders: clipFolders,
                                    files: [marked(libFile("r1", "a.wav", "root", "fp1"), 1),
                                            { ...libFile("c3", "a_EVP-B_00m03.0s_hi.mp3", "fc", null), type: "mp3", clip: true }] });
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
  choose("Move to…");                                          // an MP3 clip stays in a Clips folder
  assert.ok(pick("f1").disabled && pick("f1").title === "MP3 clips stay in Clips folders");
  assert.ok(pick("root").disabled);
  context.closeFolderDialog();
  assert.ok(vm.runInContext(`mp3StaysInClips("f1", ["c3"]) && !mp3StaysInClips("fc", ["c3"]) && !mp3StaysInClips("f1", ["r1"])`, context));
  vm.runInContext(`S.wsCalls = []; S.ws = new Proxy(S.ws, { get: (t, k) =>
    k === "load" ? (...a) => { S.wsCalls.push(["load", ...a]); return Promise.resolve(); }
    : k === "setOptions" ? (o) => { S.wsCalls.push(["options", o]); } : t[k] });`, context);
  api.play_library = async (id) => { ops.push(["play", id]);
    return { ok: true, url: "http://127.0.0.1:1/t/c3.mp3", peaks: [], duration: 4000, rate: 44100, channels: 2, fp: null,
             compressed: true, rec: "h9", name: "a_EVP-B_00m03.0s_hi.mp3", imported: 0, marks: [], reviewed: false,
             backup: { status: null, detail: "" }, backup_needed: false, markable: false, mark_reason: mp3Reason }; };
  ops.length = 0;
  mp3Row.onclick();
  await settle();
  assert.deepStrictEqual(ops, [["play", "c3"]]);
  const wsCalls = JSON.parse(vm.runInContext("JSON.stringify(S.wsCalls)", context));
  assert.deepStrictEqual(wsCalls.filter((c) => c[0] === "load"), [["load", "http://127.0.0.1:1/t/c3.mp3"]], "no peaks: decoded in the page");
  // (even a long one, past the full-detail budget a WAV would be drawn from server peaks for)
  assert.ok(wsCalls.some((c) => c[0] === "options" && c[1].sampleRate === 44100), "decoded at its own rate");
  assert.strictEqual(vm.runInContext("S.zoomMax", context), 44100);                  // zooms to the sample
  assert.ok($("mark-evp").disabled && $("reviewed").disabled);
  assert.strictEqual($("mark-evp").title, mp3Reason);
  vm.runInContext(`S.region = { start: 0.1, end: 0.2 }; $("player-loaded").hidden = false;`, context);
  fire([document], "keydown", { key: "m", target: document.body });                // M: no mark form either
  assert.ok(!vm.runInContext("S.markForm", context));
  assert.ok($("export-clips").disabled && $("export-marked").disabled);
  // Another recording loaded: the tools are back.
  vm.runInContext(`setCurrent("A-001", ${JSON.stringify({ rec: "h1", duration: 3, fp: "fpP", marks: [],
                                                        backup: { status: null, detail: "" }, reviewed: false })});`, context);
  assert.ok(!$("mark-evp").disabled && $("mark-evp").title !== mp3Reason);
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

  // An MP3 clip (it can't be marked): no marks, no 🔁 rows, the mark tools off; the selection's own Loop
  // still works (playback only), and the end of the file replays only a looping selection.
  vm.runInContext(`setCurrent("x_EVP-B_00m01.0s.mp3", ${JSON.stringify({ rec: "h4", duration: 3, fp: null, marks: [],
    backup: { status: null, detail: "" }, reviewed: false, markable: false, mark_reason: mp3Reason, compressed: true })});
    drawMarks(S.playSeq);`, context);
  assert.ok($("marks-list").hidden && $("marks-list").children.length === 0 && $("mark-controls").hidden);
  assert.ok($("mark-evp").disabled && $("mark-evp").title === mp3Reason);
  $("loop-mark").checked = true; $("loop-mark").onchange();                       // no active mark: nothing loops
  assert.deepStrictEqual(looping(), [null, false]);
  wsLog.length = 0;
  context.playbackFinished();                                                      // the clip just ends
  assert.deepStrictEqual(wsLog, []);
  const mp3Sel = fakeRegions.addRegion({ id: "sel3", start: 0.2, end: 0.9 });
  context.regionCreated(mp3Sel);
  assert.ok(!$("selection-controls").hidden && $("mark-controls").hidden);
  $("loop-selection").checked = true;
  context.regionOut(mp3Sel);
  context.playbackFinished();                                                      // a selection that ends at the end
  assert.deepStrictEqual(wsLog, [["play", "sel3", undefined], ["play", "sel3", undefined]]);
  $("loop-selection").checked = false;
  wsLog.length = 0;
  context.regionOut(mp3Sel);
  context.playbackFinished();
  assert.deepStrictEqual(wsLog, []);
  vm.runInContext(`clearSelection(); setCurrent(null);`, context);
  assert.ok(!$("mark-evp").title.includes("MP3"));

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
  // An MP3 clip (compressed: decoded by the page) the same.
  slide(6);
  await context.loadIntoPlayer(vm.runInContext("S.playSeq", context), "x_EVP-B_00m01.0s.mp3",
                               { rec: "h10", duration: 3, fp: null, marks: [], url: "u2", rate: 44100, channels: 1, compressed: true,
                                 markable: false, mark_reason: mp3Reason, backup: { status: null, detail: "" }, reviewed: false }, false);
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
    ["marked", "h12", 0.5, false], ["clips", "h12", null, 0.5, false], ["clips", "h12", "e1", 0.5, false],
    ["files", ["f1"], libJob - 1], ["clips", "h12", null, 1, true], ["clips", "h12", null, 1, true]]);
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
  context.__ws = realPlayer[0]; context.__regions = realPlayer[1];
  vm.runInContext("S.ws = __ws; S.regions = __regions;", context);
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
