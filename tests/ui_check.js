// A headless check of app/ui/app.js with a stubbed backend (run by test_ui.py with Node,
// no browser): the recorder list shows "<model> — <owner>", a recorder's folders come from
// its listing (labels as text, never markup), the export menu comes from its model, and
// selections hand the backend back the exact folder ids and recording numbers, even with
// ":" or "|" in them. The EVP Library's right-click menu runs the same code as the folder tools
// (New folder, Rename, Delete, Move to…), with the same enabled state. Prints "ok" or throws.
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
  assert.ok($("library-new").disabled && /Another OpenEVP window/.test($("library-tools").title));
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", true], ["Delete…", true]]);
  press("Escape");
  vm.runInContext(`S.caps.marks_read_only = false; renderLibraryBar();`, context);
  assert.ok(!$("library-new").disabled && $("library-tools").title === "");

  // A folder row: Open / Rename / Delete; it becomes the selected folder; keys move and choose.
  rightClick(folderRow("f1").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", false], ["Delete…", false]]);
  assert.strictEqual(L.selFolder, "f1");
  assert.strictEqual(document.activeElement.textContent, "Open");
  press("ArrowUp");
  assert.strictEqual(document.activeElement.textContent, "Delete…");  // wraps round
  press("ArrowDown"); press("ArrowDown");
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
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", true], ["Delete…", true]]);
  assert.ok($("library-rename").disabled && $("library-delete").disabled);
  press("Escape");
  assert.ok(menu.hidden, "Escape closes it");
  L.op = false; context.renderLibrary();
  assert.ok(!$("library-rename").disabled);
  const rootRow = document.createElement("tr"); rootRow.className = "lib-folder"; rootRow.folderId = "root";
  rightClick(rootRow);
  assert.deepStrictEqual(menuItems(), [["Open", false], ["Rename…", true], ["Delete…", true]]);
  L.selFolder = "root"; context.renderLibraryBar();
  assert.ok($("library-rename").disabled && $("library-delete").disabled);
  fire([window], "pointerdown", { target: document.body });
  assert.ok(menu.hidden, "a click outside closes it");
  L.selFolder = null;

  // A recording row: Play and Move to… for that row; a ticked row stands for every ticked one.
  const realTimeout = context.setTimeout;
  context.setTimeout = (fn, ms) => { if (!ms) setImmediate(fn); return 0; };   // finishFolderOp runs
  rightClick(recRow("r3").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Move to…", false]]);
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
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Move 2 recordings to…", false]]);
  choose("Move 2 recordings to…");
  assert.strictEqual($("folder-dialog-title").textContent, "Move 2 recordings to…");
  pick("f2").onclick();
  await context.folderDialogOk();
  await settle();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ops.pop())), ["move", ["r1", "r2"], "f2"]);
  context.pickGroup(recRow("r1").group, true); context.pickGroup(recRow("r2").group, true);
  context.renderLibrary();
  rightClick(recRow("r3").cells[1]);                                  // not ticked: just itself
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Move to…", false]]);
  L.op = true;
  rightClick(recRow("r3").cells[1]);
  assert.deepStrictEqual(menuItems(), [["Play", false], ["Move to…", true]]);
  L.op = false;
  press("Escape");
  context.setTimeout = realTimeout;

  // All recordings (no folders): empty space offers nothing, but the browser menu stays off there.
  L.flat = true; context.renderLibrary();
  e = rightClick($("empty"));
  assert.ok(e.defaultPrevented && menu.hidden);
  rightClick(recRow("r1").cells[1]);
  assert.deepStrictEqual(menuItems().map(([t]) => t), ["Play", "Move 2 recordings to…"]);
  fire([window], "scroll", {});
  assert.ok(menu.hidden, "scrolling closes it");
  L.flat = false;
  // Not in the library (a recorder shown): the browser's own menu.
  vm.runInContext(`S.view = "device";`, context);
  e = rightClick($("empty"));
  assert.ok(!e.defaultPrevented && menu.hidden);
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
