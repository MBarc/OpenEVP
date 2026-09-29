"use strict";
// Talks to app/backend.py through pywebview. All recorder I/O happens there on one thread.
// Every async response is checked against the request it answers (sequence numbers,
// device id), so a slow answer for a recorder the user has left is dropped.
const $ = (id) => document.getElementById(id);
// A recorder's folders and recordings come from its model (backend recordings()): folder
// ids and recording numbers are opaque (any string, a number), shown only through labels
// and textContent, and handed back to the backend as they came.
const S = { devices: [], device: null, folder: null, folders: [], caps: { wav: false },
            capsAsked: 0, capsApplied: 0, destAsked: 0, destApplied: 0, started: false,
            playable: false, formats: [], model: "",       // the open recorder's: can it play, its export menu
            dest: "", selected: new Map(), ws: null, playing: null,
            loadSeq: 0, playSeq: 0, job: 0, exporting: false, deviceError: "", settingUp: false,
            view: "device",                               // "device" (a recorder) or "library" (this PC)
            drag: null,                                   // recordings being dragged in the library: {ids}
            // The EVP library: the listing, its scan, what is shown (see loadLibrary).
            lib: { seq: 0, loading: false, listed: false, scanId: 0, buffer: [], folder: "", exists: true,
                   truncated: false, indexing: false, done: 0, total: 0, checkError: "", problem: "",
                   files: [], byId: new Map(), groups: [], rowEls: new Map(), subEls: new Map(),
                   subMarks: new Map(), expanded: new Set(), filter: "all", search: "", renderQueued: false,
                   moreRow: null,
                   // Folder view (see libraryGroups): the folder shown, by id and by its relative path
                   // parts (ids change when an ancestor is renamed; the path finds it again), or flat.
                   folders: [], folderById: new Map(), folderId: "root", folderRel: [], flat: false,
                   folderEls: new Map(), selFolder: null, paused: false, pausedRetry: false,
                   selected: new Set(),                   // file ids of the recordings picked (checkboxes) for a move
                   shown: [],                             // the recording groups shown (select all)
                   op: false, renderHeld: false,          // a folder operation running; a redraw held back by a drag
                   summaries: new Map(),                  // fp -> {marks, reviewed, notes}: one per recording, not per file
                   subTokens: new Map(), subToken: 0 },   // key -> token of the subMarks fetch that may still answer
            // The loaded recording ({rec, name, duration}; rec is the backend's handle) and its EVP marks.
            current: null, marks: [], markRegions: new Map(), markTimers: new Map(), markForm: null,
            formCls: "B", lastCls: "B", backup: null, backupNeeded: false, backupRunning: false, exportingMarked: false,
            backupEvents: new Map(), moveSeq: new Map(),     // rec -> backup events seen; mark id -> latest move
            moves: new Map(),                                // mark id -> {busy, next}: one update_mark move in flight per mark
            markGen: 0, markBusy: 0,                         // player mark calls: started (generation) and in flight
            // EVP clips: the player's call in flight, and the library's background job (its number, running).
            savingClips: false, clips: { job: 0, running: false },
            reloads: new Map() };                            // rec -> {wanted, running}: marks reloads, per loaded recording

function api() { return window.pywebview.api; }
function fmtTime(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
// One recording on a recorder, as a Map key: JSON keeps any id (":" or "|" in it, 1 vs "1") apart.
function key(device, folder, number) { return JSON.stringify([device, folder, number]); }
// kind: "warn" (default) or "ok"; action: optional {label, run} shown as a button.
function banner(text, kind = "warn", action = null) {
  $("banner").hidden = !text;
  $("banner").className = kind === "ok" ? "ok" : "";
  $("banner-text").textContent = text || "";
  const b = $("banner-action");
  b.hidden = !action;
  if (action) { b.textContent = action.label; b.onclick = action.run; }
}
function progress(done, total) {
  $("progress").hidden = total === null;
  $("progress-fill").style.width = total ? `${Math.round(100 * done / total)}%` : "0";
}
function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function status(text) { $("status").textContent = text || ""; }
function showError(r) { banner([r.error, r.advice].filter(Boolean).join(" ")); }
// capabilities().wav_status (why WAV conversion is off, or a slow-mode warning) as a sentence.
function wavStatus() { return sentence(S.caps.wav_status); }

// capabilities() again. Answers can arrive out of order (the startup call and a
// "store-writable" one): an older answer never replaces a newer one already applied.
async function loadCaps() {
  const gen = ++S.capsAsked;
  const caps = await api().capabilities();
  if (gen > S.capsApplied) { S.caps = caps; S.capsApplied = gen; }
  return S.caps;
}

// default_destination() again, numbered the same way: startup's answer never replaces
// the one a "store-writable" (or a folder rename) asked for after it.
async function loadDest() {
  const gen = ++S.destAsked;
  const d = await api().default_destination();
  if (gen > S.destApplied && d) {
    S.dest = d; S.destApplied = gen;
    if (S.started) $("dest").textContent = d;
  }
  return S.dest;
}

// The user picked a Save-to folder: it outranks every answer still on its way.
function setDest(d) { S.dest = d; S.destApplied = ++S.destAsked; $("dest").textContent = d; }

window.addEventListener("pywebviewready", async () => {
  await loadCaps();
  await loadDest();
  S.started = true;                          // from here on, storeWritable() redraws what this draws
  $("dest").textContent = S.dest;
  $("version").textContent = `v${S.caps.version}`;
  $("about-version").textContent = `v${S.caps.version}`;
  // The player always accepts a WAV file from disk. Clicking a recording to play it
  // (and WAV export) needs our LPEC decoder; without it rows are not clickable.
  setupPlayer();
  setupLibrary();
  loadLibrary();
  if (S.caps.wav) {
    $("player-hint").textContent = "Select a recording, or open a WAV file, to analyze it here.";
    $("device-table").classList.add("playable");
  } else {
    $("player-hint").textContent = "Open a WAV file to analyze it here. Recordings can't be played " +
                                   "here. " + wavStatus();
    const wav = $("format").querySelector('option[value="wav"]');
    wav.disabled = true;
    wav.textContent = "WAV (unavailable)";
    wav.title = wavStatus();
  }
  // Said once: slow-mode decoding, and a damaged marks file that was set aside.
  const notes = [S.caps.wav && S.caps.wav_status ? wavStatus() : "", ...(S.caps.store_problems || [])];
  if (notes.some(Boolean)) banner(notes.filter(Boolean).join(" "));
  poll();
  checkForUpdate(false);                   // quietly: only speaks up if there is an update
});

// ---- Updates: a newer GitHub release is offered in a dialog; the user decides ----
async function checkForUpdate(manual) {
  if (manual) status("Checking for updates…");
  const r = await api().check_update();
  if (manual) status("");
  if (!r.ok) { if (manual) showError(r); return; }
  if (!r.available) { if (manual) banner(`You have the latest version (${r.current}).`, "ok"); return; }
  $("update-title").textContent = `OpenEVP ${r.version} is available`;
  renderNotes($("update-notes"), r.notes || "");
  $("update-status").textContent = r.can_install
    ? `You have ${r.current}. Windows will ask for permission, then OpenEVP reopens on the new version. ` +
      "If it doesn't, start it from the Start menu."
    : `You have ${r.current}. This copy runs from source, so install the new version from the release page.`;
  $("update-now").disabled = !r.can_install;
  $("update-later").disabled = false;
  $("update-later").textContent = "Not now";
  $("update-dialog").hidden = false;
}

$("check-update").onclick = () => checkForUpdate(true);
$("version").onclick = () => { $("about-dialog").hidden = false; };
$("about-close").onclick = () => { $("about-dialog").hidden = true; };
$("update-later").onclick = () => { $("update-dialog").hidden = true; };
$("update-now").onclick = async () => {
  $("update-now").disabled = $("update-later").disabled = true;
  $("update-status").textContent = "Downloading…";
  const r = await api().install_update();
  if (r.ok) { $("update-status").textContent = "Starting the installer…"; return; }
  $("update-status").textContent = r.error;
  $("update-later").disabled = false;
  $("update-later").textContent = "Close";
};

async function poll() {
  try {
    const r = await api().devices();
    // The recorder list's error (e.g. libusb failed to load), or the models whose recorders
    // could not be looked for (the other models' recorders are still listed): each text is
    // shown once, not re-stamped on every poll while it persists; cleared when it is gone.
    const problem = !r.ok ? [r.error, r.advice].filter(Boolean).join(" ")
      : (r.problems || []).map((p) => [p.error, p.advice].filter(Boolean).join(" ")).join(" ");
    if (problem) {
      if (S.deviceError !== problem) { banner(problem); S.deviceError = problem; }
    } else if (S.deviceError) { banner(""); S.deviceError = ""; }
    if (r.ok) {
      S.devices = r.devices;
      renderDevices();
    }
  } finally {
    setTimeout(poll, 2000);            // next poll only after this one returned
  }
}

// "Plug in a supported recorder (Sony ICD-ST25)": the models from capabilities(), so a new model
// shows up here by itself.
function plugIn() {
  const names = Array.isArray(S.caps.models) ? S.caps.models.filter((n) => typeof n === "string" && n) : [];
  return names.length ? `Plug in a supported recorder (${names.join(", ")})` : "Plug in a supported recorder";
}

function deviceLabel(d, i) {
  if (d.state === "needs_driver" && !d.port) return `${d.model} — needs setup`;
  // d.port is the model's own display location ("port 1-4" for an ST25), shown as is.
  return d.owner ? `${d.model} — ${d.owner}` : `${d.model} #${i + 1}` + (d.port ? ` (${d.port})` : "");
}

function currentFolder() { return S.folders.find((f) => f.id === S.folder) || null; }

function leaveDevice() {
  S.device = null; S.folders = []; S.folder = null; S.selected.clear(); S.loadSeq++;
  if (S.playing && !S.playing.startsWith("lib|")) {        // a recorder recording; a file on this PC stays loaded
    S.playSeq++; S.ws.empty(); S.playing = null; setCurrent(null); showPlayerEmpty();
  }
  renderMain();
}

function showPlayerEmpty() {
  $("player-loaded").hidden = true; $("player-empty").hidden = false;
}

function showPlayerLoaded(label) {
  $("player-empty").hidden = true; $("player-loaded").hidden = false;
  $("now-playing").textContent = label; $("play").disabled = true; $("play").textContent = "▶";
}

// Load a prepared audio {url, peaks, duration} into the player. Precomputed peaks and the
// duration let wavesurfer draw at once and stream the audio instead of decoding it all.
// Recordings up to this many samples are drawn from the audio itself (every
// sample, both halves of the wave), so zooming in shows real detail; longer
// ones use the server's peaks (400 per second), which keeps memory in check.
const FULL_DETAIL_SAMPLES = 30 * 60 * 8000;          // 30 minutes of ST25 audio

async function loadIntoPlayer(seq, label, r, autoplay) {
  showPlayerLoaded(label);
  clearSelection();
  setCurrent(label, r);
  const full = r.rate && r.duration * r.rate <= FULL_DETAIL_SAMPLES;
  const zoom = $("zoom");
  zoom.max = full ? Math.min(r.rate, 8000) : 400;   // px per second; at 8000 one pixel is one ST25 sample
  if (Number(zoom.value) > Number(zoom.max)) zoom.value = zoom.max;
  try {
    if (full) {
      S.ws.setOptions({ sampleRate: r.rate });
      await S.ws.load(r.url);
    } else {
      await S.ws.load(r.url, [r.peaks], r.duration);
    }
  } catch (e) {                                   // already reported by the "error" handler
    if (seq === S.playSeq) status("");
    return;
  }
  if (seq !== S.playSeq) return;
  status("");
  drawMarks(seq);
  if (r.imported) banner(`Loaded ${plural(r.imported, "EVP mark")} stored in this file.`, "ok");
  if (autoplay) S.ws.play();
}

async function openWav() {
  const seq = ++S.playSeq;
  const r = await api().open_wav();
  if (seq !== S.playSeq || r.cancelled) return;
  if (!r.ok) { showError(r); return; }
  banner("");
  if (S.playing) { S.playing = null; renderMain(); }
  await loadIntoPlayer(seq, r.name, r, false);
}

function renderDevices() {
  const nav = $("devices");
  nav.innerHTML = "";
  $("library-entry").className = "device-name" + (S.view === "library" ? " selected" : "");
  if (S.device && !S.devices.some((d) => d.id === S.device)) {
    leaveDevice(); banner("The recorder was unplugged.");
  }
  if (!S.devices.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = `No recorder connected. ${plugIn()}.`;
    nav.appendChild(p);
    return;
  }
  S.devices.forEach((d, i) => {
    const box = document.createElement("div");
    box.className = "device";
    const name = document.createElement("div");
    name.className = `device-name state-${d.state}` + (d.id === S.device && S.view === "device" ? " selected" : "");
    name.textContent = deviceLabel(d, i);
    if (d.state !== "ready") name.title = d.message;
    name.onclick = () => openDevice(d.id);
    box.appendChild(name);
    if (d.state === "needs_driver") {
      const setup = document.createElement("button");
      setup.className = "setup";
      setup.textContent = S.settingUp ? "Setting up…" : "Set up recorder";
      setup.disabled = S.settingUp;
      setup.title = "Installs the recorder's driver on this PC. Windows asks for permission once.";
      setup.onclick = setupRecorder;
      box.appendChild(setup);
    }
    if (d.id === S.device) {
      for (const f of S.folders) {
        const row = document.createElement("div");
        row.className = "folder" + (f.id === S.folder && S.view === "device" ? " selected" : "");
        const label = document.createElement("span"), count = document.createElement("span");
        label.textContent = f.label;
        count.className = "muted"; count.textContent = String(f.recordings.length);
        row.append(label, count);
        row.onclick = () => { S.folder = f.id; S.view = "device"; renderDevices(); renderMain(); };
        box.appendChild(row);
      }
    }
    nav.appendChild(box);
  });
}

async function setupRecorder() {
  S.settingUp = true; renderDevices(); banner("");
  status("Setting up the recorder… Windows will ask for permission.");
  try {
    const r = await api().setup_driver();
    status("");
    if (r.ok && r.restart) banner("✓ Recorder set up. Restart Windows to finish, then plug it in again.", "ok");
    else if (r.ok) banner("✓ Recorder set up. It will appear in a moment.", "ok");
    else showError(r);
  } finally {
    S.settingUp = false; renderDevices();
  }
}

async function openDevice(id) {
  if (id === S.device && S.folders.length) {                // already read: just show it again
    S.view = "device"; renderDevices(); renderMain(); return;
  }
  if (S.exporting) { banner("Wait for the export to finish before switching recorders."); return; }
  S.view = "device";
  leaveDevice();
  const seq = S.loadSeq;
  S.device = id; banner(""); status("Reading the recorder…"); renderDevices();
  const r = await api().recordings(id);
  if (seq !== S.loadSeq || S.device !== id) return;       // the user moved on
  status("");
  if (!r.ok) { showError(r); S.device = null; renderDevices(); return; }
  S.folders = r.folders;
  S.folder = (r.folders.find((f) => f.recordings.length) || r.folders[0] || {}).id ?? null;
  S.model = r.model; S.playable = !!r.playable; S.formats = r.formats || [];
  $("device-table").classList.toggle("playable", S.playable);
  setFormats(S.formats);
  renderDevices(); renderMain();
}

// The export menu for the recorder shown: its native format ("dvf": ".dvf (Sony original)"),
// then WAV, greyed out with the reason when its recordings can't be converted here.
function setFormats(list) {
  const sel = $("format"), was = sel.value;
  sel.textContent = "";
  for (const f of list) {
    const o = document.createElement("option");
    o.value = f.value;
    o.textContent = f.available ? f.label : `${f.label} (unavailable)`;
    o.disabled = !f.available;
    if (!f.available) o.title = sentence(f.reason);
    sel.appendChild(o);
  }
  const keep = list.find((f) => f.value === was && f.available);
  sel.value = keep ? was : (list.find((f) => f.available) || {}).value || "";
}

// A backend reason ("the WAV decoder could not be loaded: ...") as a sentence.
function sentence(t) {
  t = t || "WAV conversion is not available";
  return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".");
}

function renderMain() {
  const library = S.view === "library";
  $("device-table").hidden = library;
  $("library-table").hidden = $("library-bar").hidden = !library;
  if (!library) { $("library-crumbs").hidden = true; $("library-crumbs").sig = null; }   // renderCrumbs shows it again
  if (library) renderLibrary(); else renderRows();
  updateExport();
}

// ---- EVP Library: the recordings in a folder (default: the save folder) with their EVP marks ----
// The backend lists files and fingerprints new ones in the background ("library-*" events,
// tagged with the scan_id of the listing they belong to). Copies of one recording (same
// fingerprint: a .dvf and its WAV, or a WAV saved twice) show as one row.
const LIB_FILTERS = {
  all: () => true,
  evp: (g) => g.marks.A + g.marks.B + g.marks.C > 0,
  A: (g) => g.marks.A > 0, B: (g) => g.marks.B > 0, C: (g) => g.marks.C > 0,
  unreviewed: (g) => !g.reviewed,
};

function libFileKey(f) { return f.fp || `id:${f.id}`; }       // un-indexed files are rows of their own

async function loadLibrary() {
  const L = S.lib, seq = ++L.seq;
  L.loading = true;
  const r = await api().list_library();
  if (seq !== L.seq) return;                  // a newer listing is on its way; its events are buffered
  L.loading = false;
  // Listings that raced each other can answer out of order: never go back to an older scan
  // (its events are dropped already; the newer scan's events would be dropped next).
  if (r.ok && r.scan_id < L.scanId) { scheduleLibraryRender(); return; }
  const buffered = L.buffer;
  L.buffer = [];
  L.listed = true;
  clearSubMarks();                            // expanded rows fetch their marks again
  if (!r.ok) {
    Object.assign(L, { files: [], byId: new Map(), indexing: false, problem: [r.error, r.advice].filter(Boolean).join(" ") });
    scheduleLibraryRender();
    return;
  }
  Object.assign(L, { scanId: r.scan_id, folder: r.folder, exists: r.exists, truncated: r.truncated,
                     indexing: r.indexing, done: 0, total: r.pending, checkError: "", problem: "",
                     files: r.files, byId: new Map(r.files.map((f) => [f.id, f])), summaries: new Map(),
                     folders: r.folders || [], folderById: new Map((r.folders || []).map((d) => [d.id, d])),
                     paused: !!r.paused });
  for (const id of [...L.selected]) if (!L.byId.has(id)) L.selected.delete(id);
  const shownBefore = L.folderId;
  findLibraryFolder(!r.paused && !r.truncated);   // an incomplete listing may lack the folder
  if (L.folderId !== shownBefore) { L.selected.clear(); L.selFolder = null; }   // another folder is shown
  for (const f of r.files) if (f.fp && !L.summaries.has(f.fp)) L.summaries.set(f.fp, fileSummary(f));
  // The backend is the source of truth: if it differs from the player (markers imported
  // meanwhile), the player fetches its marks again instead of overwriting the listing.
  if (S.current && S.current.fp && L.summaries.has(S.current.fp)) checkPlayerMarks(L.summaries.get(S.current.fp));
  for (const [event, p] of buffered) if (p.scan_id === L.scanId) libraryEvent(event, p);
  scheduleLibraryRender();
  // A newer scan started while this listing was on its way (listings racing each other).
  if (buffered.some(([, p]) => p.scan_id > L.scanId)) { loadLibrary(); return; }
  // A folder operation overlapped this listing, so the indexer was not started: once no
  // operation of ours runs, list once more (never in a loop: only after an unpaused listing).
  if (r.paused && !L.op && !L.pausedRetry) { L.pausedRetry = true; loadLibrary(); }
  else if (!r.paused) L.pausedRetry = false;
}

// "library-row" / "-progress" / "-done". Events of an older listing are dropped; events that
// come before their listing's answer (the indexer may be quicker than the reply) wait for it.
function libraryEvent(event, p) {
  const L = S.lib;
  if (p.scan_id !== L.scanId) {
    if (p.scan_id < L.scanId) return;                     // an older scan
    if (L.loading) L.buffer.push([event, p]);             // its listing's answer is on its way
    else loadLibrary();                                   // a scan this page never got the listing of
    return;
  }
  if (event === "library-row") {
    const f = L.byId.get(p.id);
    if (!f) return;
    const fp = p.fp || null;
    setFileFp(f, fp);
    Object.assign(f, { seconds: p.seconds, error: p.error });
    if (!fp) Object.assign(f, fileSummary(p));
    else {
      // Every copy of the recording shows these marks, whichever file reported them (a WAV's
      // imported markers belong to its .dvf too), and an expanded row fetches them again.
      // If it is the recording in the player, the player checks its marks against the backend.
      setSummary(fp, fileSummary(p));
      dropSubMarks(fp);
      if (S.current && S.current.fp === fp) checkPlayerMarks(fileSummary(p));
    }
  } else if (event === "library-progress") {
    L.done = p.done; L.total = p.total;
  } else if (event === "library-done") {
    L.indexing = false;
    L.checkError = p.error || "";
  }
  scheduleLibraryRender();
}

function fileSummary(x) { return { marks: x.marks, reviewed: !!x.reviewed, notes: x.notes || "" }; }

// A recording's marks summary, kept per fingerprint and copied to every file with that fp.
function setSummary(fp, sum) {
  const L = S.lib;
  L.summaries.set(fp, sum);
  let hit = false;
  for (const f of L.files) {
    if (f.fp !== fp) continue;
    hit = true;
    Object.assign(f, { marks: { ...sum.marks }, reviewed: sum.reviewed, notes: sum.notes });
  }
  return hit;
}

// A file learns (or loses) its fingerprint: an expanded row stays expanded under its new key.
function setFileFp(f, fp) {
  const was = libFileKey(f);
  f.fp = fp;
  const now = libFileKey(f);
  if (was !== now && S.lib.expanded.delete(was)) S.lib.expanded.add(now);
}

// The recordings: files grouped by fingerprint, in the order of their main file. In the
// folder view, copies in different folders are rows of their own (a group per folder and fp).
// key = the row; recKey = the recording (its marks, expanded state).
function libraryGroups() {
  const L = S.lib, groups = new Map(), order = new Map();
  L.files.forEach((f, i) => {
    order.set(f, i);
    const rk = libFileKey(f), k = L.flat ? rk : `${f.folder_id}|${rk}`;
    if (!groups.has(k)) groups.set(k, { key: k, recKey: rk, folderId: f.folder_id, files: [] });
    groups.get(k).files.push(f);
  });
  const out = [];
  for (const g of groups.values()) {
    const byName = g.files.slice().sort((a, b) => a.name.localeCompare(b.name));
    const main = byName.find((f) => f.type !== "wav") || byName[0];   // a recorder's own file (.dvf) before its WAV
    g.main = main;
    g.copies = g.files.filter((f) => f !== main);
    g.fp = main.fp;
    const sum = (g.fp && L.summaries.get(g.fp)) || fileSummary(main);   // by fp: whichever copy reported it
    g.marks = sum.marks;
    g.reviewed = sum.reviewed;
    g.notes = sum.notes;
    g.seconds = g.files.map((f) => f.seconds).find((s) => s != null);
    g.error = main.error;
    g.order = order.get(main);
    out.push(g);
  }
  return out.sort((a, b) => a.order - b.order);
}

// extra: more text the search may find it by (the names of the folders it is in, in the folder view).
function libraryMatches(g, extra = "") {
  const L = S.lib;
  if (!LIB_FILTERS[L.filter](g)) return false;
  const q = L.search.trim().toLowerCase();
  if (!q) return true;
  const text = [...g.files.flatMap((f) => [f.name, f.investigation]), g.notes, extra].join("\n").toLowerCase();
  return text.includes(q);
}

// Events can come in hundreds: redraw at most once a frame, updating rows in place.
function scheduleLibraryRender() {
  if (S.lib.renderQueued) return;
  S.lib.renderQueued = true;
  requestAnimationFrame(renderLibrary);
}

function renderLibrary() {
  const L = S.lib;
  L.renderQueued = false;
  if (S.drag) { L.renderHeld = true; return; }   // rows stay put under the pointer; redrawn on dragend
  L.groups = libraryGroups();
  $("library-count").textContent = L.listed && L.files.length ? `(${new Set(L.files.map(libFileKey)).size})` : "";
  $("library-entry").title = L.folder || "";
  if (S.view !== "library") return;
  renderLibraryBar();
  renderCrumbs();
  const wanted = [], live = new Set(), liveFolders = new Set();
  let shown = 0;
  if (!L.flat) {
    for (const d of libraryFolderRows()) {
      if (!d.visible) continue;
      liveFolders.add(d.id);
      shown++;
      wanted.push(libraryFolderRow(d));
    }
  }
  L.shown = [];
  for (const g of L.groups) {
    live.add(g.key);
    if (!L.flat && g.folderId !== L.folderId) continue;
    if (!libraryMatches(g)) continue;
    shown++;
    L.shown.push(g);
    wanted.push(libraryRow(g));
    if (L.expanded.has(g.recKey)) wanted.push(...libraryMarkRows(g));
  }
  for (const k of L.rowEls.keys()) if (!live.has(k)) { L.rowEls.delete(k); L.subEls.delete(k); }
  for (const k of L.folderEls.keys()) if (!liveFolders.has(k)) L.folderEls.delete(k);
  if (L.selFolder && !liveFolders.has(L.selFolder)) L.selFolder = null;
  if (L.truncated) {
    L.moreRow.firstChild.textContent = `Only the first ${L.files.length} files are shown.`;
    wanted.push(L.moreRow);
  }
  rovingTabs();
  const all = $("library-all"), picked = L.shown.filter(groupPicked).length;
  all.checked = picked > 0 && picked === L.shown.length;
  all.indeterminate = picked > 0 && picked < L.shown.length;
  all.disabled = !L.shown.length;
  // Put the rows in order, moving only the ones out of place (no flicker, scroll kept).
  const body = $("library-rows");
  let at = body.firstChild;
  for (const el of wanted) {
    if (el === at) at = at.nextSibling;
    else body.insertBefore(el, at);
  }
  while (at) { const next = at.nextSibling; at.remove(); at = next; }
  const empty = $("empty");
  empty.hidden = shown > 0;
  if (!L.listed) empty.textContent = "Loading…";
  else if (L.problem) empty.textContent = L.problem;
  else if (!L.exists) empty.textContent = `${L.folder} does not exist yet. Export recordings from a recorder, or choose another library folder above.`;
  else if (!L.files.length && (L.flat || L.folders.length <= 1)) empty.textContent = `No recordings in ${L.folder} yet.`;
  else if (L.flat || libraryFiltering()) empty.textContent = "No recordings match.";
  else empty.textContent = "This folder is empty. Drag recordings here or use Move to…";
}

// Tab reaches one recording row: the one last focused, else the one playing, else the first;
// the arrow keys move between them (libraryKeys).
function rovingTabs() {
  const L = S.lib, keys = new Set(L.shown.map((g) => g.key));
  const playing = L.shown.find(libraryPlaying);
  const at = keys.has(L.focusKey) ? L.focusKey : playing ? playing.key : L.shown.length ? L.shown[0].key : null;
  for (const g of L.shown) {
    const tr = L.rowEls.get(g.key);
    if (tr) tr.tabIndex = g.key === at ? 0 : -1;
  }
}

function libraryFiltering() { return S.lib.filter !== "all" || !!S.lib.search.trim(); }

// ---- Folder view: the breadcrumb, the current folder's subfolders, navigation ----
// The folders arrive in tree order (root first, parent before child); where a folder sits
// comes from its parent chain.
function libraryFolderChain(id) {
  const L = S.lib, chain = [];
  for (let d = L.folderById.get(id); d; d = d.parent == null ? null : L.folderById.get(d.parent)) chain.unshift(d);
  return chain;
}

function sameRel(a, b) {                     // Windows names: another case is the same folder
  return a.length === b.length && a.every((p, i) => p.toLowerCase() === b[i].toLowerCase());
}

// After a listing: the folder shown is found again by its relative path (an ancestor renamed
// gives it a new id), else its nearest existing ancestor, else the library itself. remember:
// what was found becomes the folder to show from now on (not after a listing made during a
// folder operation, whose folders may be out of date).
function findLibraryFolder(remember) {
  const L = S.lib;
  let found = null;
  for (let n = L.folderRel.length; n >= 0 && !found; n--) {
    const rel = L.folderRel.slice(0, n);
    found = L.folders.find((d) => sameRel(d.rel, rel)) || null;
  }
  L.folderId = found ? found.id : "root";
  if (found && remember && found.rel.join("\0") !== L.folderRel.join("\0")) {
    L.folderRel = found.rel.slice();
    saveLibraryView();
  }
}

// A folder was renamed (for Task 5): the folder shown, or one inside it, keeps its place under
// the new name when the page lists again.
function libraryFolderRenamed(oldRel, newRel) {
  const L = S.lib;
  if (L.folderRel.length >= oldRel.length && sameRel(L.folderRel.slice(0, oldRel.length), oldRel)) {
    L.folderRel = [...newRel, ...L.folderRel.slice(oldRel.length)];
    saveLibraryView();
  }
}

function openLibraryFolder(id) {
  const L = S.lib, d = L.folderById.get(id);
  if (!d) return;
  L.folderId = d.id; L.folderRel = d.rel.slice();
  L.selFolder = null; L.selected.clear();
  saveLibraryView();
  $("list-scroll").scrollTop = 0;
  scheduleLibraryRender();
}

function libraryUp() {
  const d = S.lib.folderById.get(S.lib.folderId);
  if (d && d.parent != null) openLibraryFolder(d.parent);
}

// The current folder's subfolders (by name), each with the recordings in its whole subtree
// (each recording once) and their EVPs. With a filter or search, a subfolder shows only if
// a recording somewhere in it matches (the search also finds it by the names of the folders
// it is in); without a class filter, a subfolder whose name the search finds shows too.
function libraryFolderRows() {
  const L = S.lib, rows = new Map(), q = L.search.trim().toLowerCase();
  for (const d of L.folders) {
    if (d.parent !== L.folderId) continue;
    const named = !!q && L.filter === "all" && d.name.toLowerCase().includes(q);
    rows.set(d.id, { ...d, recs: new Map(), visible: !libraryFiltering() || named });
  }
  if (!rows.size) return [];
  // folder id -> [the subfolder of the current folder it is in (or null), the names from there down]
  const top = new Map();
  const topOf = (id) => {
    if (top.has(id)) return top.get(id);
    const d = L.folderById.get(id);
    let t = [null, ""];
    if (rows.has(id)) t = [id, d.name];
    else if (d && d.parent != null) {
      const [up, names] = topOf(d.parent);
      if (up != null) t = [up, `${names}\n${d.name}`];
    }
    top.set(id, t);
    return t;
  };
  for (const g of L.groups) {
    const [t, names] = topOf(g.folderId);
    if (t == null) continue;
    const row = rows.get(t);
    row.recs.set(g.recKey, g.marks);           // a recording once, with its marks (un-indexed ones too)
    if (!row.visible && libraryMatches(g, names)) row.visible = true;
  }
  for (const row of rows.values()) {
    row.count = row.recs.size;
    row.marks = { A: 0, B: 0, C: 0 };
    for (const m of row.recs.values()) for (const c of ["A", "B", "C"]) row.marks[c] += (m && m[c]) || 0;
  }
  return [...rows.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true }));
}

// A subfolder's row: a click selects it, a double-click (or Enter while selected) opens it.
function libraryFolderRow(d) {
  const L = S.lib;
  let tr = L.folderEls.get(d.id);
  if (!tr) {
    tr = document.createElement("tr");
    tr.className = "lib-folder";
    tr.tabIndex = 0;                           // keyboard users can reach it (Tab), then Enter opens it
    for (let i = 0; i < 7; i++) tr.appendChild(document.createElement("td"));
    tr.onclick = () => { L.selFolder = tr.folderId; tr.focus({ preventScroll: true }); scheduleLibraryRender(); };
    tr.onfocus = () => { if (L.selFolder !== tr.folderId) { L.selFolder = tr.folderId; scheduleLibraryRender(); } };
    tr.ondblclick = () => openLibraryFolder(tr.folderId);
    tr.title = "Double-click to open";
    L.folderEls.set(d.id, tr);
  }
  tr.folderId = d.id;
  const selected = L.selFolder === d.id;
  const sig = JSON.stringify([d.name, d.count, d.marks, selected, !!S.caps.marks]);
  if (tr.sig === sig) return tr;
  tr.sig = sig;
  tr.classList.toggle("selected", selected);
  const [, cName, , , , cEvp] = tr.cells;
  cName.textContent = "";
  const name = document.createElement("div");
  name.className = "lib-folder-name";
  name.textContent = `📁 ${d.name}`;
  const count = document.createElement("div");
  count.className = "lib-also";
  count.textContent = d.count ? plural(d.count, "recording") : "No recordings";
  cName.append(name, count);
  cEvp.textContent = "";
  if (S.caps.marks) {
    for (const c of ["A", "B", "C"]) {
      if (!d.marks[c]) continue;
      const chip = document.createElement("span");
      chip.className = `cls-chip cls-${c}`;
      chip.textContent = `${c}×${d.marks[c]}`;
      chip.title = `${plural(d.marks[c], "class " + c + " EVP")} in this folder`;
      cEvp.append(chip, " ");
    }
  }
  return tr;
}

// Library › Old Mill › Night 2: every folder above the current one is a button.
function renderCrumbs() {
  const L = S.lib, bar = $("library-crumbs");
  bar.hidden = L.flat || !L.listed || !!L.problem || !L.exists || !L.folders.length;
  if (bar.hidden) return;
  const chain = libraryFolderChain(L.folderId);
  const sig = JSON.stringify(chain.map((d) => [d.id, d.name]));
  if (bar.sig === sig) return;
  bar.sig = sig;
  bar.textContent = "";
  chain.forEach((d, i) => {
    if (i) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.textContent = "›";
      bar.appendChild(sep);
    }
    const name = d.name || "Library";
    if (i === chain.length - 1) {
      const here = document.createElement("span");
      here.className = "crumb-here";
      here.textContent = name;
      bar.appendChild(here);
    } else {
      const b = document.createElement("button");
      b.type = "button"; b.className = "link crumb";
      b.textContent = name;
      b.title = `Go to ${name}`;
      b.onclick = () => openLibraryFolder(d.id);
      b.folderId = d.id;                       // a drop target for dragged recordings
      bar.appendChild(b);
    }
  });
}

// The All recordings switch and the folder shown are remembered for this viewer (best effort).
const LIB_VIEW_KEY = "openevp.library-view";
function saveLibraryView() {
  try { localStorage.setItem(LIB_VIEW_KEY, JSON.stringify({ flat: S.lib.flat, rel: S.lib.folderRel })); } catch (e) { /* not kept */ }
}
function loadLibraryView() {
  try {
    const v = JSON.parse(localStorage.getItem(LIB_VIEW_KEY) || "null");
    if (!v) return;
    S.lib.flat = v.flat === true;
    if (Array.isArray(v.rel) && v.rel.every((p) => typeof p === "string")) S.lib.folderRel = v.rel;
  } catch (e) { /* the defaults */ }
}

// Enter on a folder row opens it; Backspace goes up a level; Escape clears the recordings
// picked (not while typing or in a dialog).
function libraryKeys(e) {
  const L = S.lib;
  if (S.view !== "library" || e.ctrlKey || e.altKey || e.metaKey) return;
  if (typingIn(e.target) || document.querySelector(".modal:not([hidden])")) return;
  if (e.key === "Escape" && L.selected.size) {
    e.preventDefault();
    L.selected.clear();
    scheduleLibraryRender();
    return;
  }
  if (e.key === "F2") { renameKey(e); return; }
  const recRow = e.target.closest && e.target.closest("tr.lib-row");
  if (recRow && recRow === e.target && recRow.group) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (L.selFolder) { L.selFolder = null; scheduleLibraryRender(); }
      playLibrary(recRow.group, null);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const i = L.shown.findIndex((g) => g.key === recRow.group.key);
      const next = L.shown[i + (e.key === "ArrowDown" ? 1 : -1)];
      const tr = next && L.rowEls.get(next.key);
      if (tr) tr.focus();
      return;
    }
  }
  if (L.flat) return;
  const row = e.target.closest && e.target.closest("tr.lib-folder");
  if (e.key === "Enter" && row && row === e.target && L.folderById.has(row.folderId)) {
    e.preventDefault();
    openLibraryFolder(row.folderId);
  } else if (e.key === "Backspace") {
    e.preventDefault();
    libraryUp();
  }
}

// F2: rename the recording row (or folder row) that has the focus, else the selected folder,
// else the recording in the player -- as File Explorer does.
function renameKey(e) {
  const L = S.lib, at = e.target.closest ? e.target : null;
  const recRow = at && at.closest("tr.lib-row"), folderRow = at && at.closest("tr.lib-folder");
  let g = recRow && recRow.group, folderId = null;
  if (!g) {
    if (folderRow && !L.flat) folderId = folderRow.folderId;
    else if (L.selFolder && !L.flat) folderId = L.selFolder;
    else g = L.shown.find(libraryPlaying) || null;
  }
  if (!g && !folderId) return;
  e.preventDefault();
  if (g) { if (canRenameRecording(g)) renameRecordingDialog(g); }
  else if (canChangeFolder(folderId)) { L.selFolder = folderId; renameFolderDialog(); }
}

function renderLibraryBar() {
  const L = S.lib;
  $("library-folder").textContent = L.folder || "…";
  for (const b of document.querySelectorAll("#library-filters button")) b.setAttribute("aria-pressed", String(b.dataset.filter === L.filter));
  $("library-filters").hidden = !S.caps.marks;
  const st = $("library-status");
  st.classList.toggle("warn", !L.indexing && !!L.checkError);
  st.textContent = L.indexing ? `Checking recordings… ${L.done} of ${L.total}` : L.checkError ? "Could not check some recordings" : "";
  // Folders show only if a recording in them matches: one not checked yet may still match.
  if (L.indexing && !L.flat && libraryFiltering()) st.textContent += " (folders may appear as recordings are checked)";
  st.title = L.indexing ? "" : L.checkError;
  $("library-flat").checked = L.flat;
  // The folder tools: New / Rename / Delete in the folder view, Move to… in both views.
  for (const id of ["library-new", "library-rename", "library-delete"]) $(id).hidden = L.flat;
  $("library-new").disabled = !canNewFolder();
  $("library-rename").disabled = $("library-delete").disabled = !canChangeFolder(L.selFolder);
  $("library-move").disabled = !canMove([...L.selected]);
  const n = pickedRecordings();
  $("library-tools").title = S.caps.marks_read_only ? readOnlyTip() : "";
  $("library-move").title = n ? `Move ${plural(n, "selected recording")} to another folder`
                              : "Tick recordings, then move them to another folder (or drag them onto a folder)";
}

// When the folder tools (and the same items of the library's right-click menu) can be used.
function libraryToolsReady() {
  const L = S.lib;
  // A second OpenEVP window (its store is read-only) can view the library but not change it.
  return L.listed && !L.problem && L.exists && L.folders.length > 0 && !L.op && !S.caps.marks_read_only;
}
function canNewFolder() { return libraryToolsReady() && !S.lib.flat && S.lib.folderById.has(S.lib.folderId); }
function canChangeFolder(id) { return libraryToolsReady() && S.lib.folderById.has(id) && id !== "root"; }   // rename, delete
function canMove(ids) { return libraryToolsReady() && ids.length > 0 && S.lib.folders.length >= 2; }
function canRenameRecording(g) { return libraryToolsReady() && groupPickIds(g).length > 0; }

function typePlayable(type) {                  // can files of this type ("dvf", "wav"...) be played here?
  const t = (S.caps.formats || {})[type];
  return t ? !!t.playable : type === "wav";
}

function typeReason(type) {                    // why not, as a sentence
  const t = (S.caps.formats || {})[type];
  return sentence(t && t.reason);
}

function libraryPlayable(g) {                  // the file to play: the main one, or a WAV copy without the decoder
  return typePlayable(g.main.type) ? g.main : g.files.find((f) => f.type === "wav") || null;
}

function libraryPlaying(g) { return g.files.some((f) => S.playing === `lib|${f.id}`); }

// One recording's row: created once per key, its cells rewritten only when something changed.
function libraryRow(g) {
  const L = S.lib;
  let tr = L.rowEls.get(g.key);
  if (!tr) {
    tr = document.createElement("tr");
    tr.className = "lib-row";
    tr.tabIndex = -1;                          // one row at a time is reachable with Tab (rovingTabs)
    tr.onfocus = () => { if (L.focusKey !== tr.group.key) { L.focusKey = tr.group.key; rovingTabs(); } };
    for (let i = 0; i < 7; i++) tr.appendChild(document.createElement("td"));
    const pick = document.createElement("input");
    pick.type = "checkbox"; pick.className = "lib-pick"; pick.title = "Select (to move it)";
    pick.onclick = (e) => { e.stopPropagation(); pickGroup(tr.group, pick.checked); };
    const toggle = document.createElement("button");
    toggle.className = "lib-toggle";
    toggle.onclick = (e) => { e.stopPropagation(); toggleLibraryRow(tr.group); };
    tr.cells[0].append(pick, toggle);
    tr.ondragstart = (e) => startDrag(e, tr.group);
    tr.ondragend = endDrag;
    tr.onclick = () => {
      if (L.selFolder) { L.selFolder = null; scheduleLibraryRender(); }
      playLibrary(tr.group, null);
    };
    L.rowEls.set(g.key, tr);
  }
  tr.group = g;
  const picked = groupPicked(g);
  tr.cells[0].firstChild.checked = picked;
  tr.classList.toggle("picked", picked);
  tr.draggable = !L.flat;                      // onto a folder row or a breadcrumb segment
  const total = g.marks.A + g.marks.B + g.marks.C;
  const expanded = L.expanded.has(g.recKey);
  const playable = libraryPlayable(g);
  const sig = JSON.stringify([g.files.map((f) => [f.id, f.name, f.investigation, f.type]), g.seconds, g.marks, g.reviewed,
                              g.error, g.fp, L.indexing, expanded, libraryPlaying(g), !!playable]);
  if (tr.sig === sig) return tr;
  tr.sig = sig;
  tr.classList.toggle("playing", libraryPlaying(g));
  tr.classList.toggle("unplayable", !playable);
  tr.title = playable ? "" : `Can't play .${g.main.type} files. ` + typeReason(g.main.type);
  const [cToggle, cName, cInv, cType, cLen, cEvp, cRev] = tr.cells;
  const toggle = cToggle.lastChild;
  toggle.hidden = !total && !expanded;
  toggle.textContent = expanded ? "▾" : "▸";
  toggle.title = expanded ? "Hide the EVPs" : "Show the EVPs";
  cName.textContent = "";
  const name = document.createElement("div");
  name.textContent = g.main.name;
  cName.appendChild(name);
  if (g.copies.length) {
    const also = document.createElement("div");
    also.className = "lib-also";
    also.textContent = "also: " + g.copies.map((f) => f.investigation && f.investigation !== g.main.investigation
      ? `${f.name} (${f.investigation})` : f.name).join(", ");
    cName.appendChild(also);
  }
  cInv.textContent = g.main.investigation || "—";
  cType.textContent = "";
  for (const t of [...new Set([g.main.type, ...g.copies.map((f) => f.type)])]) {
    const badge = document.createElement("span");
    badge.className = `badge badge-${t}`;
    badge.textContent = t.toUpperCase();
    cType.append(badge, " ");
  }
  cLen.textContent = g.seconds == null ? "" : fmtTime(g.seconds);
  cEvp.textContent = "";
  cEvp.title = "";
  if (!S.caps.marks) {
    // no marks store: nothing to show
  } else if (g.error) {
    const warn = document.createElement("span");
    warn.className = "lib-error";
    warn.textContent = "⚠";
    cEvp.title = g.error;
    cEvp.appendChild(warn);
  } else if (!g.fp) {
    cEvp.textContent = L.indexing ? "…" : "—";
    cEvp.title = L.indexing ? "Not checked yet" : "";
  } else if (!total) {
    cEvp.textContent = "—";
  } else {
    for (const c of ["A", "B", "C"]) {
      if (!g.marks[c]) continue;
      const chip = document.createElement("span");
      chip.className = `cls-chip cls-${c}`;
      chip.textContent = `${c}×${g.marks[c]}`;
      chip.title = `${plural(g.marks[c], "class " + c + " EVP")}`;
      cEvp.append(chip, " ");
    }
  }
  cRev.textContent = g.reviewed ? "✓" : "";
  return tr;
}

// The sub-rows of an expanded recording: one per mark (or a loading / error line).
function libraryMarkRows(g) {
  const L = S.lib, marks = L.subMarks.get(g.recKey);   // marks per recording, rows per group
  if (marks === undefined) fetchLibraryMarks(g);
  const held = L.subEls.get(g.key);
  if (held && held.marks === marks && held.fp === g.fp) return held.els;
  let els;
  if (!Array.isArray(marks)) {
    const tr = document.createElement("tr");
    tr.className = "lib-mark";
    const td = document.createElement("td");
    td.colSpan = 7;
    td.textContent = marks && marks.error ? marks.error : "Loading the EVPs…";
    tr.appendChild(td);
    els = [tr];
  } else {
    els = marks.map((m) => {
      const tr = document.createElement("tr");
      tr.className = "lib-mark";
      tr.title = "Play this EVP";
      tr.appendChild(document.createElement("td"));
      const td = document.createElement("td");
      td.colSpan = 6;
      const chip = document.createElement("span");
      chip.className = `cls-chip cls-${m.cls}`; chip.textContent = m.cls; chip.title = `Class ${m.cls}`;
      const time = document.createElement("span");
      time.className = "mark-time";
      time.textContent = isPoint(m) ? fmtPrecise(m.start) : `${fmtPrecise(m.start)} – ${fmtPrecise(m.end)}`;
      const note = document.createElement("span");
      note.className = "lib-mark-note" + (m.note ? "" : " empty");
      note.textContent = m.note || "No note";
      td.append(chip, time, note);
      tr.appendChild(td);
      tr.onclick = () => playLibrary(L.rowEls.get(g.key)?.group || g, m);
      return tr;
    });
    if (!els.length) {
      const tr = document.createElement("tr");
      tr.className = "lib-mark";
      const td = document.createElement("td");
      td.colSpan = 7; td.textContent = "No EVPs marked.";
      tr.appendChild(td);
      els = [tr];
    }
  }
  L.subEls.set(g.key, { marks, fp: g.fp, els });
  return els;
}

// The expanded rows' marks cache. Every change of an entry (a fetch starting, marks from the
// player, the entry dropped or the cache cleared) gives it a new token; a fetch's answer is
// used only while its own token is still the entry's, so an older answer can never win.
function setSubMarks(key, value) {
  const L = S.lib, token = ++L.subToken;
  L.subTokens.set(key, token);
  L.subMarks.set(key, value);
  return token;
}
function dropSubMarks(key) { S.lib.subTokens.delete(key); S.lib.subMarks.delete(key); }
function clearSubMarks() { S.lib.subTokens.clear(); S.lib.subMarks.clear(); }

async function fetchLibraryMarks(g) {
  const L = S.lib, key = g.recKey, seq = L.seq;
  const token = setSubMarks(key, null);       // loading
  const r = await api().library_marks(g.main.id);
  if (seq !== L.seq || L.subTokens.get(key) !== token) return;   // relisted, updated or fetched again meanwhile
  setSubMarks(key, r.ok ? sortMarks(r.marks) : { error: [r.error, r.advice].filter(Boolean).join(" ") });
  scheduleLibraryRender();
}

function toggleLibraryRow(g) {
  const L = S.lib;
  if (!L.expanded.delete(g.recKey)) L.expanded.add(g.recKey);
  scheduleLibraryRender();
}

// Play a recording (mark = null), or one of its EVPs: loaded first unless it is in the player already.
async function playLibrary(g, mark) {
  if (mark && S.current && g.fp && S.current.fp === g.fp) {
    playMark(S.marks.find((m) => m.id === mark.id) || mark);
    return;
  }
  const f = libraryPlayable(g);
  if (!f) { banner(`Can't play .${g.main.type} files. ` + typeReason(g.main.type) + " WAV files still play."); return; }
  const seq = ++S.playSeq;
  banner(""); status(`Loading ${f.name}…`);
  const r = await api().play_library(f.id);
  if (seq !== S.playSeq) return;
  if (!r.ok) { status(""); showError(r); loadLibrary(); return; }
  S.playing = `lib|${f.id}`;
  if (!f.fp && !f.error && r.fp) setFileFp(f, r.fp);   // known now, before the indexer gets to it
  scheduleLibraryRender();
  await loadIntoPlayer(seq, f.name, r, !mark);
  if (mark && seq === S.playSeq) playMark(S.marks.find((m) => m.id === mark.id) || mark);
}

// The player changed a recording's marks or reviewed flag: update its library rows in place.
function marksSummary(marks, reviewed) {
  const counts = { A: 0, B: 0, C: 0 };
  for (const m of marks) counts[m.cls] = (counts[m.cls] || 0) + 1;
  const notes = marks.map((m) => m.note).filter(Boolean).map((n) => n.toLowerCase()).join("\n");
  return { marks: counts, reviewed: !!reviewed, notes };
}

function sameSummary(a, b) {
  return ["A", "B", "C"].every((c) => (a.marks[c] || 0) === (b.marks[c] || 0)) &&
         !!a.reviewed === !!b.reviewed && (a.notes || "") === (b.notes || "");
}

// Marks from the player (its own changes) or from the backend: update the library rows in place.
function syncLibraryMarks(fp, marks, reviewed) {
  if (!fp) return;
  const L = S.lib;
  const hit = setSummary(fp, marksSummary(marks, reviewed));
  if (L.subMarks.has(fp)) setSubMarks(fp, sortMarks(marks));
  if (hit) scheduleLibraryRender();
}

// The backend reports a summary for the recording in the player (a library listing or row).
// If it differs from what the player shows (e.g. markers imported from a WAV copy after an
// unmarked .dvf was loaded), the player fetches the marks again: the backend is the truth.
function checkPlayerMarks(sum) {
  if (!S.current || sameSummary(sum, marksSummary(S.marks, $("reviewed").checked))) return;
  reloadCurrentMarks();
}

// Ask the backend for the loaded recording's marks again (see reloadPlayerMarks).
function reloadCurrentMarks() {
  if (!S.current) return;
  const rec = S.current.rec, st = S.reloads.get(rec) || { wanted: 0, running: false };
  S.reloads.set(rec, st);
  st.wanted++;
  if (!st.running) reloadPlayerMarks(rec, st);
}

// Run a mark-changing call of the player; reloadPlayerMarks never applies an answer that
// overlapped one (it could miss that change).
async function markCall(call) {
  S.markGen++; S.markBusy++;
  try { return await call(); } finally { S.markBusy--; S.markGen++; }
}

function playerSaving() { return S.markBusy > 0 || S.markTimers.size > 0 || S.moves.size > 0; }

// Fetch the loaded recording's marks until an answer that no change of the player's own
// overlapped has been applied after the latest request for one (checkPlayerMarks). The state
// belongs to this rec: a reload still running for a recording the player has left never
// swallows a request made for the one now loaded.
async function reloadPlayerMarks(rec, st) {
  st.running = true;
  try {
    for (let tries = 0; tries < 50 && showing(rec); tries++) {
      if (playerSaving()) { await new Promise((res) => setTimeout(res, 300)); continue; }
      const want = st.wanted, gen = S.markGen;
      const r = await api().get_marks(rec);
      if (!showing(rec) || !r.ok) return;
      if (gen !== S.markGen || playerSaving()) continue;        // overlapped a change of its own: ask again
      applyPlayerMarks(r);
      if (want === st.wanted) return;                           // no newer backend report meanwhile
    }
  } finally {
    st.running = false;
    if (S.reloads.get(rec) === st) S.reloads.delete(rec);        // served (or left); a new report starts afresh
  }
}

// The backend's marks replace the player's (regions added, moved or removed to match).
function applyPlayerMarks(r) {
  S.marks = sortMarks(r.marks);
  const ids = new Set(S.marks.map((m) => m.id));
  for (const [id, region] of S.markRegions) if (!ids.has(id)) { region.remove(); S.markRegions.delete(id); }
  if (S.markForm && S.markForm.id && !ids.has(S.markForm.id)) closeMarkForm();
  const drawn = !!S.ws.getDuration();                           // otherwise drawMarks adds them on "ready"
  for (const m of S.marks) {
    const region = S.markRegions.get(m.id);
    if (region) region.setOptions({ start: m.start, end: m.end, color: MARK_COLORS[m.cls], content: m.cls });
    else if (drawn) addMarkRegion(m);
  }
  $("reviewed").checked = !!r.reviewed;
  S.backup = r.backup; S.backupNeeded = !!r.backup_needed;
  renderMarks();                                                // also syncs the library rows
}

// A marks call answered after the player moved on to another recording: ask for that one's marks.
async function refreshLibraryMarks(rec, fp) {
  if (!fp) return;
  const r = await api().get_marks(rec);
  if (r.ok) syncLibraryMarks(fp, r.marks, r.reviewed);
}

function showLibrary() {
  S.view = "library"; banner("");
  renderDevices(); renderMain();
  loadLibrary();
}

function setupLibrary() {
  $("library-entry").onclick = showLibrary;
  $("library-folder").onclick = async () => {
    const d = await api().choose_library_folder();
    if (d) {
      Object.assign(S.lib, { folder: d, folderId: "root", folderRel: [], selFolder: null });
      S.lib.expanded.clear(); S.lib.selected.clear();
      saveLibraryView();
      loadLibrary();
    }
  };
  loadLibraryView();
  $("library-flat").onchange = () => {
    Object.assign(S.lib, { flat: $("library-flat").checked, selFolder: null });
    S.lib.selected.clear();
    saveLibraryView();
    scheduleLibraryRender();
  };
  document.addEventListener("keydown", libraryKeys);
  for (const b of document.querySelectorAll("#library-filters button")) {
    b.onclick = () => { S.lib.filter = b.dataset.filter; scheduleLibraryRender(); };
  }
  $("library-search").oninput = () => { S.lib.search = $("library-search").value; scheduleLibraryRender(); };
  setupFolderTools();
  const more = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = 7; td.className = "muted";
  more.appendChild(td);
  S.lib.moreRow = more;
}

// ---- Folder management: New folder, Rename, Delete (Recycle Bin), picking recordings,
// Move to… and drag and drop. The page sends ids, never paths; after an operation it lists again.
// The recording in the player is unloaded before an operation touches its file (so nothing holds
// it open) and loaded again from its new place afterwards.

// A row's files that a pick (or a drag) moves: the group's files in the folder view (a group is
// one folder's copies); in the All recordings view the copies beside its main file only.
function groupPickIds(g) {
  const files = S.lib.flat ? g.files.filter((f) => f.folder_id === g.main.folder_id) : g.files;
  return files.map((f) => f.id);
}
function groupPicked(g) { return groupPickIds(g).every((id) => S.lib.selected.has(id)); }
function pickGroup(g, on) {
  for (const id of groupPickIds(g)) on ? S.lib.selected.add(id) : S.lib.selected.delete(id);
  scheduleLibraryRender();
}
function recordingsIn(ids) {                   // how many recordings (rows) these files are
  const L = S.lib;
  return new Set(ids.map((id) => L.byId.get(id)).filter(Boolean)
                    .map((f) => (L.flat ? "" : f.folder_id) + "|" + libFileKey(f))).size;
}
function pickedRecordings() { return recordingsIn([...S.lib.selected]); }

function humanSize(bytes) {
  if (bytes < 1024) return plural(bytes, "byte");
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

function inFolderTree(folderId, topId) {       // is folderId topId or inside it?
  for (let d = S.lib.folderById.get(folderId); d; d = d.parent == null ? null : S.lib.folderById.get(d.parent)) {
    if (d.id === topId) return true;
  }
  return false;
}

function errorText(r) { return [r.error, r.advice].filter(Boolean).join(" ") || "It did not work."; }
function dialogText(text) { const p = document.createElement("p"); p.textContent = text; return p; }

// ---- the dialog: a title, a body, an optional name field, OK / Cancel ----
// run(name) does the work: it returns a message to show under the field (the dialog stays
// open to try again), or nothing to close the dialog.
const FD = { run: null, busy: false, back: null };

function folderDialog({ title, body = null, name = null, ok, okDisabled = false, focus = null, run }) {
  $("folder-dialog-title").textContent = title;
  const box = $("folder-dialog-body");
  box.textContent = "";
  if (body) box.append(...body);
  box.hidden = !body;
  const input = $("folder-dialog-name");
  input.hidden = name === null;
  input.value = name || "";
  input.disabled = false;
  showDialogError("");
  $("folder-dialog-ok").textContent = ok;
  $("folder-dialog-ok").disabled = okDisabled;
  $("folder-dialog-cancel").disabled = false;
  Object.assign(FD, { run, busy: false, back: document.activeElement });
  $("folder-dialog").hidden = false;
  if (!input.hidden) { input.focus(); input.select(); } else (focus || $("folder-dialog-cancel")).focus();
}

function showDialogError(text) {
  const el = $("folder-dialog-error");
  el.textContent = text || "";
  el.hidden = !text;
}

function closeFolderDialog() {
  if (FD.busy) return;
  $("folder-dialog").hidden = true;
  FD.run = null;
  const back = FD.back;
  FD.back = null;
  if (back && back.isConnected && typeof back.focus === "function") back.focus({ preventScroll: true });
}

async function folderDialogOk() {
  if (FD.busy || !FD.run || $("folder-dialog-ok").disabled) return;
  FD.busy = true;
  const input = $("folder-dialog-name");
  $("folder-dialog-ok").disabled = $("folder-dialog-cancel").disabled = input.disabled = true;
  let problem;
  try { problem = await FD.run(input.value); } catch (e) { problem = String(e); }
  FD.busy = false;
  $("folder-dialog-ok").disabled = $("folder-dialog-cancel").disabled = input.disabled = false;
  if (!problem) { closeFolderDialog(); return; }
  showDialogError(problem);
  if (!input.hidden) { input.focus(); input.select(); }
}

// An operation on the library: one at a time; the tools are off while it runs.
async function libraryOp(what, call) {
  const L = S.lib;
  L.op = true; status(what); scheduleLibraryRender();
  try { return await call(); } finally { L.op = false; status(""); scheduleLibraryRender(); }
}

// After the dialog has closed: list again (and whatever follows), without holding the dialog open.
function finishFolderOp(then) { setTimeout(() => { then().catch((e) => banner(String(e))); }, 0); }

// A refused or failed operation still lists again: it paused the indexer, and part of it may be done.
function relistAfterFailure() { finishFolderOp(loadLibrary); }

// The Save-to folder follows a rename of the folder holding it (the backend already uses the new one).
async function refreshDest() {
  await loadDest();
}

// ---- the recording in the player, around an operation ----
function heldLibraryFile() {                   // the library file in the player, or null
  if (!S.current || !S.playing || !S.playing.startsWith("lib|")) return null;
  const L = S.lib, id = S.playing.slice(4), f = L.byId.get(id);
  if (!f) return null;
  const d = L.folderById.get(f.folder_id);
  return { id, name: f.name, folderId: f.folder_id, rel: d ? d.rel.slice() : null, time: S.ws.getCurrentTime() };
}

function unloadPlayer() {                      // stop, and let go of the file
  S.playSeq++;
  S.ws.pause();
  S.ws.empty();
  const media = S.ws.getMediaElement && S.ws.getMediaElement();
  if (media) { media.removeAttribute("src"); media.load(); }
  S.playing = null;
  setCurrent(null);
  showPlayerEmpty();
}

// Load a held recording again, by its file id after the operation (null: it is gone).
async function reloadHeld(held, id) {
  const f = id && S.lib.byId.get(id);
  if (!f) return;                              // deleted (or not listed): the player stays empty
  const seq = ++S.playSeq;
  const r = await api().play_library(id);
  if (seq !== S.playSeq) return;               // the user played something else meanwhile
  if (!r.ok) { showError(r); return; }
  S.playing = `lib|${id}`;
  if (!f.fp && !f.error && r.fp) setFileFp(f, r.fp);
  scheduleLibraryRender();
  await loadIntoPlayer(seq, f.name, r, false);
  if (seq === S.playSeq && held.time) S.ws.setTime(Math.min(held.time, S.ws.getDuration() || held.time));
}

// The file listed under this name in the folder with these relative path parts (after a rename).
function findLibraryFile(rel, name) {
  const L = S.lib, d = L.folders.find((x) => sameRel(x.rel, rel));
  if (!d) return null;
  const f = L.files.find((x) => x.folder_id === d.id && x.name === name);
  return f ? f.id : null;
}

function selectFolderRow(id) {
  const L = S.lib;
  if (!L.flat && L.folderById.has(id) && L.folderById.get(id).parent === L.folderId) {
    L.selFolder = id;
    scheduleLibraryRender();
  }
}

// ---- New folder / Rename / Delete ----
function newFolderDialog() {
  const L = S.lib, here = L.folderById.get(L.folderId);
  if (!here || L.flat || L.op) return;
  folderDialog({
    title: "New folder",
    body: [dialogText(`A new folder in ${here.name || "the library"}.`)],
    name: "", ok: "Create",
    run: async (name) => {
      const r = await libraryOp("Creating the folder…", () => api().create_folder(here.id, name));
      if (!r.ok) { relistAfterFailure(); return errorText(r); }
      banner(`Created “${name.trim()}”.`, "ok");
      finishFolderOp(async () => { await loadLibrary(); selectFolderRow(r.id); });
      return null;
    },
  });
}

function renameFolderDialog() {
  const L = S.lib, d = L.folderById.get(L.selFolder);
  if (!d || d.id === "root" || L.op) return;
  folderDialog({
    title: `Rename “${d.name}”`, name: d.name, ok: "Rename",
    run: async (name) => {
      const held = heldLibraryFile(), touched = !!held && inFolderTree(held.folderId, d.id);
      if (touched) unloadPlayer();
      const r = await libraryOp("Renaming the folder…", () => api().rename_folder(d.id, name));
      if (!r.ok) {
        if (touched) await reloadHeld(held, held.id);  // still where it was
        relistAfterFailure();
        return errorText(r);
      }
      const newName = name.trim(), oldRel = d.rel.slice(), newRel = [...oldRel.slice(0, -1), newName];
      banner(`Renamed “${d.name}” to “${newName}”.`, "ok");
      libraryFolderRenamed(oldRel, newRel);            // the folder shown keeps its place if it was inside
      finishFolderOp(async () => {
        await refreshDest();
        await loadLibrary();
        selectFolderRow(r.id);
        if (touched) {
          const rel = [...newRel, ...held.rel.slice(oldRel.length)];
          reloadHeld(held, findLibraryFile(rel, held.name));
        }
      });
      return null;
    },
  });
}

async function deleteFolderDialog() {
  const L = S.lib, d = L.folderById.get(L.selFolder);
  if (!d || d.id === "root" || L.op) return;
  const info = await libraryOp("Looking into the folder…", () => api().folder_info(d.id));
  if (!info.ok) { showError(info); relistAfterFailure(); return; }
  const list = document.createElement("ul");
  const item = (text) => { const li = document.createElement("li"); li.textContent = text; list.appendChild(li); };
  let evps;
  if (info.evps_at_least && !info.with_evps) evps = "not all checked for EVPs yet";
  else if (info.with_evps) evps = `${info.evps_at_least ? "at least " : ""}${info.with_evps} with EVPs`;
  else evps = "none with EVPs";
  item(info.recordings ? `${plural(info.recordings, "recording")} — ${evps}` : "No recordings");
  if (info.backups) item(plural(info.backups, "recorder backup"));
  if (info.other_files) item(`${plural(info.other_files, "other file")} such as photos or video`);
  if (info.subfolders) item(plural(info.subfolders, "folder"));
  item(`${humanSize(info.bytes)} in all`);
  const body = [dialogText("It contains:"), list];
  if (info.save_folder) body.push(dialogText("This is your Save to folder; exports will create it again."));
  folderDialog({
    title: `Move “${info.name || d.name}” to the Recycle Bin?`, body, ok: "Move to Recycle Bin",
    run: async () => {
      const held = heldLibraryFile(), touched = !!held && inFolderTree(held.folderId, d.id);
      if (touched) unloadPlayer();
      const r = await libraryOp("Moving the folder to the Recycle Bin…", () => api().delete_folder(d.id));
      const lost = r.backups ? ` ${plural(r.backups, "recorder backup")} went with it; those recordings offer Retry backup.` : "";
      if (r.ok) banner(`Moved “${d.name}” to the Recycle Bin.${lost}`, "ok");
      else showError(r);                                // part of it may be gone: list again either way
      finishFolderOp(async () => {
        await loadLibrary();
        if (touched) reloadHeld(held, S.lib.byId.has(held.id) ? held.id : null);   // still there: back in the player
      });
      return null;
    },
  });
}

// ---- Rename a recording: its files in the folder shown get one new name, each its own extension ----
function fileStem(name) { const dot = name.lastIndexOf("."); return dot > 0 ? name.slice(0, dot) : name; }

// The row's group as the latest listing has it (indexing may have joined a .wav to its .dvf
// since the dialog opened): by its key, else the group now holding its main file.
function currentGroup(g) {
  const L = S.lib, groups = L.groups || [];
  return groups.find((x) => x.key === g.key) || groups.find((x) => x.files.some((f) => f.id === g.main.id)) || null;
}

function renameRecordingDialog(g) {
  const L = S.lib, ids = groupPickIds(g).filter((id) => L.byId.has(id));
  if (!ids.length || L.op) return;
  const names = ids.map((id) => L.byId.get(id).name);
  folderDialog({
    title: `Rename “${g.main.name}”`,
    body: names.length > 1 ? [dialogText(`${names.join(" and ")} are renamed together; each keeps its extension.`)] : null,
    name: fileStem(g.main.name), ok: "Rename",
    run: async (name) => {
      const now = currentGroup(g);
      if (!now) { relistAfterFailure(); return "That recording is no longer there. Refresh the list."; }
      const ids = groupPickIds(now).filter((id) => L.byId.has(id));
      const stem = name.trim();
      if (!stem) return "Type a name for the file.";
      // Nothing to do: the player is left alone.
      if (ids.every((id) => { const f = L.byId.get(id); return f.name === stem + f.name.slice(fileStem(f.name).length); })) return null;
      const held = heldLibraryFile(), touched = !!held && ids.includes(held.id);
      if (touched) unloadPlayer();
      const r = await libraryOp("Renaming the recording…", () => api().rename_files(ids, name));
      const newIds = r.ids || {};
      if (!r.ok) {
        finishFolderOp(async () => {                     // a file that could not be named back has a new id
          await loadLibrary();
          if (touched) reloadHeld(held, newIds[held.id] || held.id);
        });
        if (!Object.keys(newIds).length) return errorText(r);
        banner(errorText(r), "warn");                    // part of it was renamed: these ids are gone
        return null;
      }
      const renamed = r.renamed || [];
      banner(renamed.length ? `Renamed ${renamed.map((x) => `“${x.from}” to “${x.to}”`).join(", ")}.`
                            : "The name is unchanged.", "ok");
      for (const [from, to] of Object.entries(newIds)) {
        if (from !== to && L.selected.delete(from)) L.selected.add(to);
      }
      // The playing highlight follows its file to the new id.
      const playingId = S.playing && S.playing.startsWith("lib|") ? S.playing.slice(4) : null;
      if (playingId && newIds[playingId]) S.playing = `lib|${newIds[playingId]}`;
      finishFolderOp(async () => {
        await loadLibrary();
        if (touched) reloadHeld(held, newIds[held.id] || held.id);
      });
      return null;
    },
  });
}

// ---- Move to… (the folder tree) and the move itself ----
// ids: the files to move (default: the recordings ticked).
function moveDialog(ids = [...S.lib.selected]) {
  const L = S.lib;
  ids = ids.filter((id) => L.byId.has(id));
  if (!ids.length || L.op) return;
  let target = null, first = null;
  const list = document.createElement("div");
  list.className = "folder-picker";
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Folders");
  for (const d of L.folders) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "folder-pick";
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", "false");
    b.style.paddingLeft = `${8 + 18 * (libraryFolderChain(d.id).length - 1)}px`;
    b.textContent = `📁 ${d.name || "Library"}`;
    b.folderId = d.id;
    // The folder every picked file is in already (the folder shown, in the folder view).
    b.disabled = ids.every((id) => L.byId.get(id).folder_id === d.id);
    if (b.disabled) b.title = "They are in this folder already";
    b.onclick = () => {
      target = d.id;
      for (const x of list.children) x.setAttribute("aria-selected", String(x === b));
      $("folder-dialog-ok").disabled = false;
    };
    b.ondblclick = () => { b.onclick(); folderDialogOk(); };
    if (!first && !b.disabled) first = b;
    list.appendChild(b);
  }
  folderDialog({
    title: `Move ${plural(recordingsIn(ids), "recording")} to…`, body: [list], ok: "Move", okDisabled: true, focus: first,
    run: async () => {
      if (target) finishFolderOp(() => moveRecordings(ids, target));
      return null;
    },
  });
}

async function moveRecordings(ids, targetId) {
  const L = S.lib, to = L.folderById.get(targetId);
  if (!to || L.op || !ids.length) return;
  const n = recordingsIn(ids);
  const held = heldLibraryFile(), touched = !!held && ids.includes(held.id);
  if (touched) unloadPlayer();
  const r = await libraryOp(`Moving ${plural(n, "recording")}…`, () => api().move_files(ids, targetId));
  const newIds = r.ids || {}, moved = Object.keys(newIds), failed = r.failed || [];
  const where = to.name || "the library";
  const parts = [];
  if (!r.ok) parts.push(errorText(r));
  else if (moved.length) parts.push(`Moved ${plural(recordingsIn(moved), "recording")} to ${where}.`);
  else if (r.skipped) parts.push(`They are in ${where} already.`);
  if (r.renamed && r.renamed.length) {
    parts.push(`The name was taken, so: ${r.renamed.map((x) => `${x.from} → ${x.to}`).join(", ")}.`);
  }
  if (r.ok && failed.length) {
    parts.push(`Not moved: ${failed.map((x) => `${x.name} (${String(x.error).replace(/\.$/, "")})`).join(" · ")}.`);
  }
  banner(parts.join(" "), r.ok && !failed.length ? "ok" : "warn");
  for (const id of (r.ok && !failed.length ? ids : moved)) L.selected.delete(id);
  // The playing highlight follows its file to the new id.
  const playingId = S.playing && S.playing.startsWith("lib|") ? S.playing.slice(4) : null;
  if (playingId && newIds[playingId]) S.playing = `lib|${newIds[playingId]}`;
  await loadLibrary();
  if (touched) reloadHeld(held, newIds[held.id] || held.id);
}

// ---- drag and drop: recording rows onto a folder row or a breadcrumb segment ----
function startDrag(e, g) {
  const L = S.lib;
  if (L.flat || L.op) { e.preventDefault(); return; }
  // A picked row drags every picked recording; any other row just itself.
  const ids = groupPicked(g) ? [...L.selected].filter((id) => L.byId.has(id)) : groupPickIds(g);
  S.drag = { ids };
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("application/x-openevp-recordings", JSON.stringify(ids));
  const label = $("drag-label"), n = recordingsIn(ids);
  label.textContent = n === 1 ? g.main.name : plural(n, "recording");
  e.dataTransfer.setDragImage(label, -12, -12);
}

function endDrag() {
  if (!S.drag) return;
  S.drag = null;
  for (const el of document.querySelectorAll(".drop-target")) el.classList.remove("drop-target");
  if (S.lib.renderHeld) { S.lib.renderHeld = false; scheduleLibraryRender(); }
}

// The folder a drag is over (a subfolder row or an ancestor in the breadcrumb), if it can take it.
function dropTarget(e) {
  const L = S.lib;
  if (!S.drag || L.op || L.flat || !e.target.closest) return null;
  const el = e.target.closest("tr.lib-folder, #library-crumbs button.crumb");
  if (!el || !L.folderById.has(el.folderId) || el.folderId === L.folderId) return null;
  return el;
}

function setupDragAndDrop() {
  for (const zone of [$("library-rows"), $("library-crumbs")]) {
    zone.addEventListener("dragover", (e) => {
      const el = dropTarget(e);
      if (!el) return;
      e.preventDefault();                      // on a valid target only
      e.dataTransfer.dropEffect = "move";
      for (const x of document.querySelectorAll(".drop-target")) if (x !== el) x.classList.remove("drop-target");
      el.classList.add("drop-target");
    });
    zone.addEventListener("dragleave", (e) => {
      const el = e.target.closest && e.target.closest(".drop-target");
      if (el && !el.contains(e.relatedTarget)) el.classList.remove("drop-target");
    });
    zone.addEventListener("drop", (e) => {
      const el = dropTarget(e);
      if (!el) return;
      e.preventDefault();
      const ids = S.drag.ids, to = el.folderId;
      endDrag();
      moveRecordings(ids, to).catch((err) => { banner(String(err)); loadLibrary(); });
    });
  }
  // Anything else dropped on the page (files from Explorer, say) is swallowed: the window must
  // not navigate to it. Text may still be dropped into a text field.
  const intoField = (e) => !S.drag && typingIn(e.target) && !e.dataTransfer.types.includes("Files");
  document.addEventListener("dragover", (e) => {
    if (e.defaultPrevented || intoField(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "none";
  });
  document.addEventListener("drop", (e) => {
    if (e.defaultPrevented || intoField(e)) return;
    e.preventDefault();
    endDrag();
  });
  // No pointer goes down and the window is not focused again while a drag runs: if either
  // happens with a drag still recorded, its dragend was missed. Clear it.
  window.addEventListener("pointerdown", () => { if (S.drag) endDrag(); }, true);
  window.addEventListener("mousedown", () => { if (S.drag) endDrag(); }, true);
  window.addEventListener("focus", () => { if (S.drag) endDrag(); });
}

function setupFolderTools() {
  $("library-new").onclick = newFolderDialog;
  $("library-rename").onclick = renameFolderDialog;
  $("library-delete").onclick = deleteFolderDialog;
  $("library-move").onclick = () => moveDialog();
  $("library-all").onclick = () => {
    const on = $("library-all").checked;
    for (const g of S.lib.shown) for (const id of groupPickIds(g)) on ? S.lib.selected.add(id) : S.lib.selected.delete(id);
    scheduleLibraryRender();
  };
  $("folder-dialog-ok").onclick = folderDialogOk;
  $("folder-dialog-cancel").onclick = closeFolderDialog;
  $("folder-dialog").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeFolderDialog(); }
    else if (e.key === "Enter" && e.target === $("folder-dialog-name")) { e.preventDefault(); folderDialogOk(); }
  });
  setupDragAndDrop();
  setupLibraryMenu();
}

// ---- the library's right-click menu: the folder tools for the row (or the empty space) clicked ----
// Each item runs the same function as its toolbar button and is off whenever that button would be.
const CM = { back: null };

function libraryMenuItems(target) {
  const L = S.lib;
  const folderRow = target.closest && target.closest("tr.lib-folder");
  if (folderRow && !L.flat && L.folderById.has(folderRow.folderId)) {
    const id = folderRow.folderId;
    selectFolderRow(id);                       // the row the menu is for, as a click would
    const why = canChangeFolder(id) ? "" : L.op ? "Wait for the operation to finish" : "";
    return [
      { label: "Open", run: () => openLibraryFolder(id) },
      { label: "Rename…", disabled: !canChangeFolder(id), title: why, run: () => { L.selFolder = id; renameFolderDialog(); } },
      { label: "Delete…", disabled: !canChangeFolder(id), title: why, run: () => { L.selFolder = id; deleteFolderDialog(); } },
      { label: "Export clips", disabled: !canExportClips(), title: clipsTip("Save every EVP in this folder (and its folders) as its own WAV clip"),
        run: () => exportLibraryClips({ folder: id }, L.folderById.get(id).name || "the library") },
    ];
  }
  const recRow = target.closest && target.closest("tr.lib-row");
  if (recRow && recRow.group) {
    const g = recRow.group;
    // A ticked row stands for every recording ticked; any other row for itself only.
    const ids = groupPicked(g) ? [...L.selected].filter((x) => L.byId.has(x)) : groupPickIds(g);
    const n = recordingsIn(ids), playable = !!libraryPlayable(g);
    return [
      { label: "Play", disabled: !playable, title: playable ? "" : `Can't play .${g.main.type} files. ` + typeReason(g.main.type),
        run: () => { if (L.selFolder) { L.selFolder = null; scheduleLibraryRender(); } playLibrary(g, null); } },
      // Rename… is for the row clicked (its files in the folder shown), ticked or not.
      { label: "Rename…", disabled: !canRenameRecording(g),
        title: L.op ? "Wait for the operation to finish" : n > 1 ? "Renames this recording only" : "",
        run: () => renameRecordingDialog(g) },
      { label: n > 1 ? `Move ${plural(n, "recording")} to…` : "Move to…", disabled: !canMove(ids), run: () => moveDialog(ids) },
      // Export clips is for the row clicked (its files: the copies of one recording), ticked or not.
      { label: "Export clips", disabled: !canExportClips() || !groupMarked(g),
        title: clipsTip(groupMarked(g) ? "Save each EVP of this recording as its own WAV clip" : "No EVPs marked in this recording"),
        run: () => exportLibraryClips({ files: g.files.map((f) => f.id) }, g.main.name) },
    ];
  }
  if (L.flat) return [];                       // no folders in the All recordings view
  return [{ label: "New folder…", disabled: !canNewFolder(), run: newFolderDialog }];
}

function libraryMenuOpen() { return !$("context-menu").hidden; }

function openLibraryMenu(e) {
  if (S.view !== "library") return;
  e.preventDefault();                          // no browser menu over the library (text fields elsewhere keep theirs)
  closeLibraryMenu(false);
  const back = document.activeElement;
  const items = libraryMenuItems(e.target);
  if (!items.length) return;
  const menu = $("context-menu");
  for (const it of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "context-item";
    b.setAttribute("role", "menuitem");
    b.textContent = it.label;
    b.disabled = !!it.disabled;
    if (it.title) b.title = it.title;
    b.onclick = () => { if (b.disabled) return; closeLibraryMenu(true); it.run(); };
    menu.appendChild(b);
  }
  CM.back = back;
  menu.hidden = false;
  // At the pointer (from the keyboard's menu key: at the element), kept inside the window.
  let x = e.clientX || 0, y = e.clientY || 0;
  if (!x && !y && e.target.getBoundingClientRect) {
    const r = e.target.getBoundingClientRect();
    x = r.left + 8; y = r.top + r.height / 2;
  }
  const box = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(x, (window.innerWidth || 0) - box.width - 2))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, (window.innerHeight || 0) - box.height - 2))}px`;
  const first = [...menu.children].find((b) => !b.disabled);
  (first || menu).focus({ preventScroll: true });
}

// refocus: give the focus back to where it was (Escape; an item chosen, so that a dialog it
// opens returns the focus there too).
function closeLibraryMenu(refocus) {
  const menu = $("context-menu");
  if (menu.hidden) return;
  menu.hidden = true;
  menu.textContent = "";
  const back = CM.back;
  CM.back = null;
  if (refocus && back && back.isConnected && typeof back.focus === "function") back.focus({ preventScroll: true });
}

function libraryMenuKeys(e) {
  if (!libraryMenuOpen()) return;
  const items = [...$("context-menu").children].filter((b) => !b.disabled);
  const at = items.indexOf(document.activeElement);
  const stop = () => { e.preventDefault(); e.stopPropagation(); };
  if (e.key === "Escape") { stop(); closeLibraryMenu(true); }
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    stop();
    if (!items.length) return;
    const step = e.key === "ArrowDown" ? 1 : -1;
    items[at < 0 ? (step > 0 ? 0 : items.length - 1) : (at + step + items.length) % items.length].focus();
  } else if (e.key === "Home" || e.key === "End") {
    stop();
    if (items.length) items[e.key === "Home" ? 0 : items.length - 1].focus();
  } else if (e.key === "Enter" || e.key === " ") {
    stop();
    if (at >= 0) items[at].onclick();
  } else if (e.key === "Tab" || e.key === "F2") { stop(); closeLibraryMenu(true); }
}

function setupLibraryMenu() {
  $("list-scroll").addEventListener("contextmenu", openLibraryMenu);
  $("context-menu").addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("keydown", libraryMenuKeys, true);   // before the page's own keys
  window.addEventListener("pointerdown", (e) => {
    if (libraryMenuOpen() && !$("context-menu").contains(e.target)) closeLibraryMenu(false);
  }, true);
  window.addEventListener("scroll", () => closeLibraryMenu(false), true);
  window.addEventListener("resize", () => closeLibraryMenu(false));
  window.addEventListener("blur", () => closeLibraryMenu(false));
}

function renderRows() {
  const rows = $("rows");
  rows.innerHTML = "";
  const folder = S.device ? currentFolder() : null;
  const recs = folder ? folder.recordings : [];
  $("empty").hidden = recs.length > 0;
  $("empty").textContent = S.device ? `${folder ? folder.label : "This folder"} is empty.`
                                    : `${plugIn()} and select it on the left.`;
  for (const r of recs) {
    const tr = document.createElement("tr");
    const k = key(S.device, folder.id, r.number);
    if (S.playing === k) tr.className = "playing";
    const box = document.createElement("input");
    box.type = "checkbox"; box.checked = S.selected.has(k); box.disabled = !!r.problem;
    const item = { device: S.device, folder: folder.id, number: r.number };
    box.onclick = (e) => { e.stopPropagation(); box.checked ? S.selected.set(k, item) : S.selected.delete(k); updateExport(); };
    const no = typeof r.number === "number" ? String(r.number).padStart(3, "0") : String(r.number);
    const cells = [no, r.recorded, r.seconds == null ? "" : fmtTime(r.seconds), r.problem || ""];
    const first = document.createElement("td"); first.appendChild(box); tr.appendChild(first);
    cells.forEach((c, i) => { const td = document.createElement("td"); td.textContent = c; if (i === 3) td.className = "note"; tr.appendChild(td); });
    if (S.playable) tr.onclick = () => play(S.device, folder.id, r.number, r.label);
    rows.appendChild(tr);
  }
  $("all").checked = recs.length > 0 && recs.every((r) => r.problem || S.selected.has(key(S.device, folder.id, r.number)));
  updateExport();
}

$("all").onclick = () => {
  const folder = currentFolder();
  for (const r of folder ? folder.recordings : []) {
    if (r.problem) continue;
    const k = key(S.device, folder.id, r.number);
    $("all").checked ? S.selected.set(k, { device: S.device, folder: folder.id, number: r.number }) : S.selected.delete(k);
  }
  renderMain();
};

function selectedItems() {
  return [...S.selected.values()].filter((i) => i.device === S.device)
    .map((i) => ({ folder: i.folder, number: i.number }));
}

function updateExport() {
  const n = S.device && S.view === "device" ? selectedItems().length : 0;
  $("export").disabled = S.exporting || S.clips.running || n === 0;
  if (S.exporting) return;                 // the button shows progress while exporting
  $("export").textContent = n ? `Export ${n} selected` : "Export selected";
}

$("dest").onclick = async () => {
  const d = await api().choose_destination();
  if (d) { setDest(d); loadLibrary(); }   // the library may be the save folder
};

$("export").onclick = async () => {
  const items = selectedItems();
  const job = ++S.job;
  S.exporting = true; updateExport(); banner(""); status("");
  $("export").textContent = `Exporting 0 of ${items.length}…`; progress(0, items.length);
  const r = await api().export(S.device, items, $("format").value, S.dest, job);
  if (!r.ok && job === S.job) { S.exporting = false; progress(0, null); showError(r); updateExport(); }
};

// Called by app/main.py through evaluate_js. Events for an older job are ignored.
window.onBackendEvent = (event, p) => {
  if (event === "update-progress") {        // not tied to an export job
    $("update-status").textContent = `Downloading… ${p.percent}%`;
    return;
  }
  if (event === "backup-done" || event === "backup-failed") { backupEvent(event, p); return; }   // not an export job
  if (event === "store-writable") { storeWritable(); return; }                                     // nor this
  if (event.startsWith("library-")) { libraryEvent(event, p); return; }                           // nor these
  if (event.startsWith("clips-")) { clipsEvent(event, p); return; }                               // a clips job's own
  if (p.job !== S.job) return;
  if (event === "export-progress") {
    $("export").textContent = `Exporting ${p.done} of ${p.total}…`;
    progress(p.done, p.total);
  }
  if (event === "export-done") {
    S.exporting = false; progress(0, null); updateExport();
    const open = { label: "Open folder", run: () => api().open_folder(p.dest) };
    let msg;
    if (p.saved && p.skipped) msg = `✓ Saved ${plural(p.saved, "recording")} to ${p.dest} (${p.skipped} were already there).`;
    else if (p.saved) msg = `✓ Saved ${plural(p.saved, "recording")} to ${p.dest}.`;
    else if (p.skipped) msg = `✓ Nothing new: ${p.skipped === 1 ? "it was" : `all ${p.skipped} were`} already saved in ${p.dest}.`;
    else msg = "Nothing was saved.";
    if (p.notes.length) banner(`${msg} Not saved: ${p.notes.join(" · ")}`, "warn", open);
    else banner(msg, "ok", open);
    loadLibrary();
  }
  if (event === "export-failed") {
    S.exporting = false; progress(0, null); updateExport();
    const done = p.saved || p.skipped ? ` (${p.saved} saved, ${p.skipped} already there before it stopped)` : "";
    banner([`Export stopped${done}: ${p.error}`, p.advice].filter(Boolean).join(" "));
  }
};

function setupPlayer() {
  const css = getComputedStyle(document.body);
  S.ws = WaveSurfer.create({ container: "#waveform", height: 80,
                             waveColor: css.getPropertyValue("--muted").trim(),
                             progressColor: css.getPropertyValue("--accent").trim() });
  S.ws.on("ready", () => { $("play").disabled = false; tick(); });
  S.ws.on("timeupdate", tick);
  S.ws.on("play", () => { $("play").textContent = "❚❚"; });
  S.ws.on("pause", () => { $("play").textContent = "▶"; });
  S.ws.on("error", (e) => { $("play").disabled = true; audioFailed(e); });
  $("play").onclick = () => S.ws.playPause();
  $("zoom").oninput = () => S.ws.zoom(Number($("zoom").value));
  $("height").oninput = () => S.ws.setOptions({ barHeight: Number($("height").value) });
  $("waveform").addEventListener("wheel", wheelZoom, { passive: false });
  setupSelection();                         // once: the plugin stays registered across loads
  setupMarks();
  $("open-wav").onclick = openWav;
  $("open-wav-2").onclick = openWav;
}

// The player could not load (or keep streaming) the current recording. A file served in place
// is refused once it changes on disk (its marks belong to the version that was loaded).
async function audioFailed(e) {
  const cur = S.current;
  banner(`Could not play this recording: ${(e && e.message) || e || "unknown error"}`);
  if (!cur) return;
  const r = await api().recording_changed(cur.rec);
  if (S.current === cur && r.ok && r.changed) banner("This file changed on disk. Open it again.");
}

// Mouse wheel over the waveform zooms in and out, keeping the spot under the pointer in place.
// With Shift held it changes the height instead, so quiet parts can be made taller.
function wheelZoom(e) {
  const duration = S.ws.getDuration();
  const delta = e.deltaY || e.deltaX;                         // Shift turns the wheel sideways in Chromium
  if (!duration || !delta) return;
  e.preventDefault();
  if (e.shiftKey) {
    const h = $("height");
    h.value = Math.max(1, Math.min(Number(h.max), Number(h.value) * (delta < 0 ? 1.25 : 0.8)));
    S.ws.setOptions({ barHeight: Number(h.value) });
    return;
  }
  const slider = $("zoom");
  const fit = $("waveform").clientWidth / duration;          // px per second when the whole file fits
  const now = Number(slider.value) || fit;
  let next = Math.min(Number(slider.max), now * (delta < 0 ? 1.25 : 0.8));
  if (next <= fit) next = 0;                                  // zoomed all the way out: fit to width
  if (next === Number(slider.value)) return;
  const x = e.clientX - $("waveform").getBoundingClientRect().left;
  const t = (S.ws.getScroll() + x) / now;                    // the second under the pointer
  slider.value = next;
  S.ws.zoom(next);
  if (next) S.ws.setScroll(t * next - x);
}

// ---- Selection: drag across the waveform to pick a part; play or loop just that part ----
function fmtPrecise(s) {
  const m = Math.floor(s / 60);
  return `${m}:${(s - 60 * m).toFixed(1).padStart(4, "0")}`;
}

function setupSelection() {
  S.regions = S.ws.registerPlugin(WaveSurfer.Regions.create());
  S.regions.enableDragSelection({ color: "rgba(108, 195, 167, 0.28)" });
  S.region = null;
  S.regions.on("region-created", (r) => {
    if (isMark(r)) return;                                  // marks live beside the selection
    for (const other of S.regions.getRegions()) if (other !== r && !isMark(other)) other.remove();   // one selection at a time
    S.region = r; showSelection();
  });
  S.regions.on("region-updated", (r) => {
    if (r === S.region) showSelection();
    else if (isMark(r)) scheduleMarkMove(r);
  });
  S.regions.on("region-out", (r) => {
    if (r === S.region && $("loop-selection").checked) r.play();
  });
  S.regions.on("region-clicked", (r, e) => { e.stopPropagation(); isMark(r) ? r.play(true) : r.play(); });
  $("play-selection").onclick = () => { if (S.region) S.region.play(); };
  $("clear-selection").onclick = clearSelection;
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || $("player-loaded").hidden || /INPUT|SELECT|BUTTON|TEXTAREA/.test(e.target.tagName)) return;
    e.preventDefault();
    S.ws.playPause();
  });
}

function showSelection() {
  const r = S.region;
  $("selection-hint").hidden = !!r || !!S.markForm;       // the mark form takes the bar while open
  $("selection-controls").hidden = !r || !!S.markForm;
  if (r) $("selection-range").textContent = `${fmtPrecise(r.start)} – ${fmtPrecise(r.end)} (${(r.end - r.start).toFixed(1)} s)`;
}

function clearSelection() {                                 // the selection only: marks stay
  if (S.region) S.region.remove();
  S.region = null;
  $("loop-selection").checked = false;
  showSelection();
}

function tick() { $("time").textContent = `${fmtTime(S.ws.getCurrentTime())} / ${fmtTime(S.ws.getDuration())}`; }

async function play(device, folder, number, label) {
  if (!S.playable) return;
  const seq = ++S.playSeq;
  banner(""); $("play").disabled = true; status(`Loading ${label}…`);
  const r = await api().audio(device, folder, number);
  if (seq !== S.playSeq || S.device !== device) return;    // the user picked something else
  if (!r.ok) { status(""); showError(r); return; }
  S.playing = key(device, folder, number); renderMain();
  await loadIntoPlayer(seq, `Recording ${label}`, r, true);
}

// ---- EVP marks: a selection saved as an EVP (class A/B/C + note), kept per recording ----
const MARK_COLORS = { A: "rgba(220, 60, 60, .30)", B: "rgba(230, 150, 30, .30)", C: "rgba(70, 130, 220, .30)" };
const MIN_MARK = 0.05;                        // seconds; the backend refuses shorter marks
const READ_ONLY_TIP = "Marks can't be changed right now.";   // only if the backend gave no reason
const NO_MARKS_TIP = "Marks are not available here.";

function isMark(r) { return r.id.startsWith("mark-"); }
function marksWritable() { return !!S.caps.marks && !S.caps.marks_read_only; }
// Why the store is read-only, in the backend's words (another OpenEVP, or its lock file could not be opened).
function readOnlyTip() { return S.caps.marks_read_only_reason || READ_ONLY_TIP; }
function marksTip() { return !S.caps.marks ? NO_MARKS_TIP : S.caps.marks_read_only ? readOnlyTip() : ""; }
function showing(rec) { return !!S.current && S.current.rec === rec; }   // is this handle's recording still in the player?
function backupEventsSeen(rec) { return S.backupEvents.get(rec) || 0; }
function isPoint(m) { return m.end <= m.start; }        // imported point markers: no length to drag
function typingIn(el) {
  return el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "SELECT" ||
         (el.tagName === "INPUT" && !/^(checkbox|radio|range|button)$/.test(el.type));
}

// The player's mark tools: enabled, or disabled with why (their own tooltip otherwise).
function renderMarkTools() {
  $("reviewed-label").hidden = !S.caps.marks;
  $("reviewed").disabled = $("mark-evp").disabled = !marksWritable();
  for (const id of ["reviewed-label", "mark-evp"]) {
    const el = $(id);
    if (el.dataset.tip === undefined) el.dataset.tip = el.title;
    el.title = marksTip() || el.dataset.tip;
  }
}

// The store became writable (the other OpenEVP closed, or its lock file opens now): the
// backend reloaded marks from disk, so the tools come back and the marks are read again.
async function storeWritable() {
  await loadCaps();                           // before startup is done, startup draws with these
  await loadDest();                           // the remembered Save-to folder applies now
  if (!S.started || S.caps.marks_read_only) return;
  renderMarkTools();
  renderLibraryBar();
  if (S.current) {
    closeMarkForm();
    for (const region of S.markRegions.values()) region.remove();   // redrawn draggable
    S.markRegions.clear();
    if (S.ws.getDuration()) for (const m of S.marks) addMarkRegion(m);
    renderMarks();
    reloadCurrentMarks();
  }
  loadLibrary();
  banner("OpenEVP's data can be changed again: marks and folders are back.", "ok");
}

function setupMarks() {
  renderMarkTools();
  $("mark-evp").onclick = () => openMarkForm(null);
  $("mark-save").onclick = saveMarkForm;
  $("mark-cancel").onclick = closeMarkForm;
  for (const b of document.querySelectorAll(".cls-toggle button")) b.onclick = () => pickClass(b.dataset.cls);
  $("mark-form").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeMarkForm(); }
    else if (e.key === "Enter" && e.target.tagName !== "BUTTON") { e.preventDefault(); saveMarkForm(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "m" && e.key !== "M") return;
    if (e.ctrlKey || e.altKey || e.metaKey || typingIn(e.target)) return;
    if (!S.region || S.markForm || !marksWritable() || $("player-loaded").hidden) return;
    if (document.querySelector(".modal:not([hidden])")) return;        // the About or update dialog is open
    e.preventDefault();
    openMarkForm(null);
  });
  $("reviewed").onchange = setReviewed;
  $("retry-backup").onclick = () => retryBackup(S.current && S.current.rec);
  $("export-marked").onclick = exportMarked;
  $("export-clips").onclick = () => exportClips(null);
}

// The recording now in the player (its label and the backend's audio result), or none (null).
function setCurrent(label, r) {
  closeMarkForm();
  for (const region of S.markRegions.values()) region.remove();
  S.markRegions.clear();
  S.current = r ? { rec: r.rec, name: label, duration: r.duration, fp: r.fp || null } : null;
  S.marks = r ? sortMarks(r.marks || []) : [];
  S.backup = r ? r.backup : null;
  S.backupNeeded = !!(r && r.backup_needed);
  S.backupRunning = false;
  $("reviewed").checked = !!(r && r.reviewed);
  renderMarks();
}

function sortMarks(marks) { return marks.slice().sort((a, b) => a.start - b.start); }

// Draw the marks once the audio is loaded; a newer load (seq) wins.
function drawMarks(seq) {
  if (!S.ws.getDuration()) { S.ws.once("ready", () => { if (seq === S.playSeq) drawMarks(seq); }); return; }
  for (const m of S.marks) if (!S.markRegions.has(m.id)) addMarkRegion(m);
}

function addMarkRegion(m) {
  const movable = marksWritable() && !isPoint(m);
  const region = S.regions.addRegion({ id: "mark-" + m.id, start: m.start, end: m.end, color: MARK_COLORS[m.cls],
                                       content: m.cls, drag: movable, resize: movable, minLength: MIN_MARK });
  S.markRegions.set(m.id, region);
}

// A saved mark replaces the old one in the list and on the waveform.
// While a move of it is still to be saved, the band stays where the user dragged it.
function replaceMark(mark) {
  S.marks = sortMarks(S.marks.map((m) => (m.id === mark.id ? mark : m)));
  const region = S.markRegions.get(mark.id);
  const where = movePending(mark.id) ? {} : { start: mark.start, end: mark.end };
  if (region) region.setOptions({ ...where, color: MARK_COLORS[mark.cls], content: mark.cls });
  renderMarks();
}

// ---- the form in the selection bar: a new mark (mark = null) or editing one ----
function openMarkForm(mark) {
  if (!S.current || !marksWritable() || (!mark && !S.region)) return;
  S.markForm = { id: mark ? mark.id : null };
  pickClass(mark ? mark.cls : S.lastCls);
  $("mark-note").value = mark ? mark.note : "";
  $("mark-form-label").textContent = mark ? `Edit the EVP at ${fmtPrecise(mark.start)}:` : "Mark as EVP:";
  $("mark-save").disabled = false;
  $("mark-form").hidden = false;
  showSelection();
  $("mark-note").focus();
}

function closeMarkForm() {
  if (!S.markForm) return;
  S.markForm = null;
  $("mark-form").hidden = true;
  if ($("mark-form").contains(document.activeElement)) document.activeElement.blur();
  showSelection();
}

function pickClass(cls) {
  S.formCls = cls;
  for (const b of document.querySelectorAll(".cls-toggle button")) b.setAttribute("aria-pressed", String(b.dataset.cls === cls));
}

async function saveMarkForm() {
  const form = S.markForm;
  if (!form || !S.current || $("mark-save").disabled) return;
  if (!form.id && !S.region) { closeMarkForm(); return; }
  const { rec, duration, fp } = S.current, cls = S.formCls, events = backupEventsSeen(rec);
  const note = $("mark-note").value.trim();
  $("mark-save").disabled = true;
  const r = form.id
    ? await markCall(() => api().update_mark(rec, form.id, null, null, cls, note))
    : await markCall(() => api().add_mark(rec, S.region.start, Math.min(S.region.end, duration || S.region.end), cls, note));
  $("mark-save").disabled = false;
  if (!showing(rec)) { if (r.ok) refreshLibraryMarks(rec, fp); return; }   // another recording: its form was closed
  if (!r.ok) { showError(r); return; }                    // the form stays open: the note is not lost
  S.lastCls = cls;
  if (S.markForm === form) closeMarkForm();               // (it may have been cancelled meanwhile)
  if (form.id) { replaceMark(r.mark); return; }
  clearSelection();
  S.marks = sortMarks([...S.marks, r.mark]);
  addMarkRegion(r.mark);
  // The backup's own event may have come before this answer: then it is over already.
  if (r.backup_queued && backupEventsSeen(rec) === events) S.backupRunning = true;
  renderMarks();
  if (!r.backup_queued) refreshBackup();                  // refused (closing, updating) or not needed
}

// ---- dragging a mark's band (or an edge) saves the new times after a short pause ----
// Moves of one mark are sent one at a time: while an update_mark for it is in flight, a newer
// move waits (replacing any move already waiting) and is sent when that call returns. So the
// backend always stores the latest move last, and never an older one after a newer one.
// moveSeq numbers the moves: only the answer to the latest one updates the player.
function scheduleMarkMove(region) {
  const id = region.id.slice("mark-".length);
  const old = S.marks.find((m) => m.id === id);
  if (!S.current || !old) return;
  clearTimeout(S.markTimers.get(id));
  S.markTimers.delete(id);
  if (Math.abs(old.start - region.start) < 1e-6 && Math.abs(old.end - region.end) < 1e-6) return;   // a click, not a move
  const { rec, duration, fp } = S.current, move = (S.moveSeq.get(id) || 0) + 1;
  S.moveSeq.set(id, move);                                   // answers to earlier moves are now stale
  S.markTimers.set(id, setTimeout(() => {
    S.markTimers.delete(id);
    queueMarkMove(id, { rec, fp, move, region, start: region.start, end: Math.min(region.end, duration || region.end) });
  }, 300));
}

function queueMarkMove(id, job) {
  const q = S.moves.get(id) || { busy: false, next: null };
  S.moves.set(id, q);
  if (q.busy) { q.next = job; return; }                      // coalesced: only the latest waiting move is sent
  sendMarkMove(id, q, job);
}

// Is a move of this mark still to be saved (waiting for its pause, waiting its turn, or in flight)?
function movePending(id) {
  const q = S.moves.get(id);
  return S.markTimers.has(id) || !!(q && (q.busy || q.next));
}

async function sendMarkMove(id, q, job) {
  q.busy = true;
  let r;
  try {
    r = await markCall(() => api().update_mark(job.rec, id, job.start, job.end, null, null));
  } catch (e) {
    r = { ok: false, error: `The move was not saved: ${e}` };
  }
  q.busy = false;
  if (q.next) {                                              // a newer move waited for this one
    const next = q.next;
    q.next = null;
    sendMarkMove(id, q, next);
    return;
  }
  S.moves.delete(id);
  if (S.moveSeq.get(id) !== job.move) return;                // a later move of this mark decides
  const { rec, fp, region } = job;
  if (!r.ok) showError(r);                                   // also after switching: the move was not saved
  if (!showing(rec)) { if (r.ok) refreshLibraryMarks(rec, fp); return; }
  if (r.ok) { replaceMark(r.mark); return; }
  const old = S.marks.find((m) => m.id === id);
  if (old) region.setOptions({ start: old.start, end: old.end });   // back to where it is stored
}

// ---- the marks list under the selection bar ----
function renderMarks() {
  $("marks").hidden = !S.current || !S.caps.marks;
  const list = $("marks-list");
  list.innerHTML = "";
  list.hidden = !S.marks.length;
  for (const m of S.marks) list.appendChild(markRow(m));
  $("marks-empty").hidden = S.marks.length > 0;
  $("export-marked").disabled = !S.marks.length || S.exportingMarked || S.savingClips;
  $("export-clips").disabled = !S.marks.length || S.exportingMarked || S.savingClips || S.clips.running;
  renderBackup();
  // Every change of the loaded marks ends here: the library shows the same counts and notes.
  if (S.current) syncLibraryMarks(S.current.fp, S.marks, $("reviewed").checked);
}

function markButton(text, title, run, enabled = true) {
  const b = document.createElement("button");
  b.textContent = text; b.title = title; b.disabled = !enabled;
  b.onclick = (e) => { e.stopPropagation(); run(); };
  return b;
}

function markRow(m) {
  const row = document.createElement("div");
  row.className = "mark-row";
  const chip = document.createElement("span");
  chip.className = `cls-chip cls-${m.cls}`; chip.textContent = m.cls; chip.title = `Class ${m.cls}`;
  const time = document.createElement("span");
  time.className = "mark-time";
  time.textContent = isPoint(m) ? fmtPrecise(m.start) : `${fmtPrecise(m.start)} – ${fmtPrecise(m.end)}`;
  const writable = marksWritable(), tip = marksTip();
  const note = document.createElement("span");
  note.className = "mark-note" + (m.note ? "" : " empty") + (writable ? " editable" : "");
  note.dataset.id = m.id;
  note.textContent = m.note || (writable ? "Add a note" : "No note");
  note.title = writable ? (m.note ? `${m.note}\n(click to edit)` : "Click to add a note") : [m.note, tip].filter(Boolean).join("\n");
  if (writable) note.onclick = () => editNoteInline(note, m);
  row.append(chip, time, note,
    markButton("▶", "Play this EVP", () => playMark(m)),
    markButton("Save clip", "Save this EVP as its own WAV clip (in a Clips folder)", () => exportClips(m),
               !S.exportingMarked && !S.savingClips && !S.clips.running),
    markButton("✎", tip || "Change the class or note", () => openMarkForm(m), writable),
    markButton("✕", tip || "Delete this mark", () => deleteMark(m), writable));
  return row;
}

function playMark(m) {
  const region = S.markRegions.get(m.id);
  if (region) region.play(true); else S.ws.play(m.start, isPoint(m) ? undefined : m.end);
}

// Click a note to edit it in place: Enter or leaving the field saves, Escape cancels.
function editNoteInline(span, m, text = m.note) {
  const input = document.createElement("input");
  input.type = "text"; input.maxLength = 500; input.value = text; input.className = "mark-note-edit";
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save && input.value.trim() !== m.note) saveNote(m, input.value.trim()); else renderMarks();
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(true);
  span.replaceWith(input);
  input.focus();
}

async function saveNote(m, note) {
  if (!S.current) return;
  const { rec, fp } = S.current;
  const r = await markCall(() => api().update_mark(rec, m.id, null, null, null, note));
  if (!r.ok) showError(r);
  if (!showing(rec)) { if (r.ok) refreshLibraryMarks(rec, fp); return; }
  if (r.ok) { replaceMark(r.mark); return; }
  renderMarks();                                             // the typed note stays in its field to try again
  const span = $("marks-list").querySelector(`.mark-note[data-id="${m.id}"]`);
  if (span) editNoteInline(span, m, note);
}

async function deleteMark(m) {
  if (!S.current || !confirm("Delete this mark?")) return;
  const { rec, fp } = S.current;
  const r = await markCall(() => api().delete_mark(rec, m.id));
  if (!showing(rec)) { if (r.ok) refreshLibraryMarks(rec, fp); return; }
  if (!r.ok) { showError(r); return; }
  clearTimeout(S.markTimers.get(m.id));
  S.markTimers.delete(m.id);
  const q = S.moves.get(m.id);
  if (q) q.next = null;                                      // a waiting move of a deleted mark is dropped
  S.moveSeq.set(m.id, (S.moveSeq.get(m.id) || 0) + 1);       // and the answer to one in flight is ignored
  const region = S.markRegions.get(m.id);
  if (region) region.remove();
  S.markRegions.delete(m.id);
  S.marks = S.marks.filter((x) => x.id !== m.id);
  if (S.markForm && S.markForm.id === m.id) closeMarkForm();
  renderMarks();
}

async function setReviewed() {
  if (!S.current) return;
  const want = $("reviewed").checked, { rec, fp } = S.current;
  const r = await markCall(() => api().set_reviewed(rec, want));
  if (!showing(rec)) { if (r.ok) refreshLibraryMarks(rec, fp); return; }
  if (!r.ok) { $("reviewed").checked = !want; showError(r); return; }
  syncLibraryMarks(fp, S.marks, want);
}

// ---- backup of a marked recorder recording, and the WAV export with marks ----
function renderBackup() {
  const el = $("backup-status"), b = S.backup || { status: null, detail: "" };
  const saved = !S.backupRunning && b.status === "saved";
  const failed = !S.backupRunning && b.status === "failed";
  // Marked on the recorder but never backed up (no backup was started, so no event will come).
  const needed = !S.backupRunning && !saved && !failed && S.backupNeeded && S.marks.length > 0;
  el.className = saved ? "ok" : failed || needed ? "warn" : "";
  el.textContent = S.backupRunning ? "Backing up…" : saved ? "Backed up to your save folder"
    : failed ? b.detail || "The backup failed."            // the detail names the recording and why
    : needed ? "Not backed up yet" : "";
  el.title = saved ? b.detail : "";
  el.hidden = !el.textContent;
  $("retry-backup").hidden = !(failed || needed);
  $("retry-backup").disabled = !marksWritable();
  $("retry-backup").title = marksTip();
}

function backupEvent(event, p) {
  const done = event === "backup-done";
  S.backupEvents.set(p.rec, backupEventsSeen(p.rec) + 1);
  if (done) banner(`✓ Backed up ${p.label} so this EVP is safe even if the recorder is wiped. ${p.detail}`, "ok");
  else banner(p.detail, "warn", marksWritable() ? { label: "Retry backup", run: () => retryBackup(p.rec) } : null);
  if (done) loadLibrary();
  if (!S.current) return;
  if (p.rec !== S.current.rec) { refreshBackup(); return; }   // the same recording may be loaded again under a new handle
  S.backupRunning = false;
  S.backup = { status: done ? "saved" : "failed", detail: p.detail };
  S.backupNeeded = !done;
  renderBackup();
}

async function refreshBackup() {
  const rec = S.current.rec;
  const r = await api().get_marks(rec);
  if (!showing(rec) || !r.ok) return;
  S.backup = r.backup;
  S.backupNeeded = !!r.backup_needed;
  renderBackup();
}

async function retryBackup(rec) {
  if (!rec) return;
  const events = backupEventsSeen(rec);
  const r = await api().retry_backup(rec);
  if (!r.ok) { showError(r); return; }
  banner("");
  if (!showing(rec)) return;
  if (!r.queued) refreshBackup();
  else if (backupEventsSeen(rec) === events) { S.backupRunning = true; renderBackup(); }   // unless it already ended
}

async function exportMarked() {
  if (!S.current) return;
  const rec = S.current.rec;
  S.exportingMarked = true; renderMarks(); status("Saving a WAV with the marks…");
  const r = await api().export_marked(rec);
  S.exportingMarked = false; status(""); renderMarks();
  if (!r.ok) { showError(r); return; }
  const where = r.folder_name ? `the ${r.folder_name} folder of your save folder` : "your save folder";
  banner(r.already ? `✓ ${r.name} with these marks was already saved in ${where}.`
                   : `✓ Saved ${r.name} with its EVP marks in ${where}.`, "ok");
  loadLibrary();
}

// ---- EVP clips: each mark as its own short WAV, in a Clips folder beside the WAV with marks ----
// "3 clips saved (1 already there)", what was skipped and why, and Open folder when there is one.
function clipsSummary(p) {
  const parts = [];
  if (p.cancelled) parts.push(p.closing ? "Stopped because the app is closing." : "Stopped.");
  parts.push(`${p.cancelled ? "" : "✓ "}${plural(p.saved, "clip")} saved` + (p.already ? ` (${p.already} already there).` : "."));
  if (p.skipped && p.skipped.length) parts.push(`Skipped ${plural(p.skipped.length, "recording")}: ${p.skipped.join(" · ")}.`);
  if (p.notes && p.notes.length) parts.push(`Not saved: ${p.notes.join(" · ")}.`);
  if (!p.cancelled && p.recordings === 0 && !(p.skipped && p.skipped.length)) parts.push("No EVPs were marked there.");
  const kind = p.cancelled || (p.skipped && p.skipped.length) || (p.notes && p.notes.length) ? "warn" : "ok";
  const open = p.folder ? { label: "Open folder", run: () => api().open_folder(p.folder) } : null;
  banner(parts.join(" "), kind, open);
}

// The player: every mark of the loaded recording (mark = null), or one mark (its Save clip).
async function exportClips(mark) {
  if (!S.current || S.savingClips) return;
  const rec = S.current.rec;
  S.savingClips = true; renderMarks(); status(mark ? "Saving the clip…" : "Saving the clips…");
  let r;
  try { r = await api().export_clips(rec, mark ? mark.id : null); } finally { S.savingClips = false; status(""); renderMarks(); }
  if (!r.ok) { showError(r); return; }
  clipsSummary({ ...r, skipped: [], cancelled: false, recordings: 1 });
  loadLibrary();                               // the clips may be in the library
}

// The library's Export clips: off while an operation, an export or another clips job runs, and
// in a second window (it would say why).
function canExportClips() {
  return !!S.caps.marks && !S.caps.marks_read_only && !S.lib.op && !S.clips.running && !S.exporting;
}
function clipsTip(tip) {
  if (S.caps.marks_read_only) return readOnlyTip();
  if (S.clips.running) return "Wait for the clips being exported";
  if (S.lib.op || S.exporting) return "Wait for the operation to finish";
  return tip;
}
function groupMarked(g) { return g.marks.A + g.marks.B + g.marks.C > 0; }

// A folder (and its folders) or one recording's files, as a background job: progress, Cancel.
async function exportLibraryClips(what, name) {
  if (!canExportClips()) return;
  const job = ++S.clips.job;
  S.clips.running = true; S.clips.name = name; S.clips.cancelling = false;
  renderMarks(); updateExport();
  clipsRunning(0, null);
  const r = what.folder ? await api().export_clips_folder(what.folder, job) : await api().export_clips_files(what.files, job);
  if (!r.ok && job === S.clips.job && S.clips.running) {
    S.clips.running = false; progress(0, null); renderMarks(); updateExport(); scheduleLibraryRender();
    showError(r);
  }
}

function clipsRunning(done, total) {
  progress(done, total === null ? 1 : total);
  const count = total ? ` ${done} of ${total}` : "";
  banner(`Exporting the clips of ${S.clips.name || "the recordings"}…${count}`, "ok",
         { label: "Cancel", run: () => { S.clips.cancelling = true; $("banner-action").disabled = true; api().cancel_clips(S.clips.job); } });
  $("banner-action").disabled = !!S.clips.cancelling;             // asked once: it stops after the recording it is on
}

function clipsEvent(event, p) {
  if (p.job !== S.clips.job || !S.clips.running) return;      // an older job
  if (event === "clips-progress") { clipsRunning(p.done, p.total); return; }
  S.clips.running = false;
  progress(0, null);
  $("banner-action").disabled = false;
  renderMarks(); updateExport(); scheduleLibraryRender();
  if (event === "clips-done") clipsSummary(p);
  else {
    const done = p.saved || p.already ? ` (${p.saved} saved, ${p.already} already there before it stopped)` : "";
    banner([`The clips export stopped${done}: ${p.error}`, p.advice].filter(Boolean).join(" "), "warn",
           p.folder ? { label: "Open folder", run: () => api().open_folder(p.folder) } : null);
  }
  loadLibrary();
}
