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
  listeners.get(target).push({ type, fn, capture: !!(capture === true || (capture && capture.capture)) });
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
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
