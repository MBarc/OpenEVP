"use strict";
// Talks to app/backend.py through pywebview. All recorder I/O happens there on one thread.
// Every async response is checked against the request it answers (sequence numbers,
// device id), so a slow answer for a recorder the user has left is dropped.
const $ = (id) => document.getElementById(id);
const S = { devices: [], device: null, folder: "A", folders: {}, caps: { wav: false },
            dest: "", selected: new Set(), ws: null, playing: null,
            loadSeq: 0, playSeq: 0, job: 0, exporting: false, deviceError: false, settingUp: false,
            view: "device",                               // "device" (a recorder) or "library" (this PC)
            // The EVP library: the listing, its scan, what is shown (see loadLibrary).
            lib: { seq: 0, loading: false, listed: false, scanId: 0, buffer: [], folder: "", exists: true,
                   truncated: false, indexing: false, done: 0, total: 0, checkError: "", problem: "",
                   files: [], byId: new Map(), groups: [], rowEls: new Map(), subEls: new Map(),
                   subMarks: new Map(), expanded: new Set(), filter: "all", search: "", renderQueued: false,
                   moreRow: null,
                   summaries: new Map(),                  // fp -> {marks, reviewed, notes}: one per recording, not per file
                   subTokens: new Map(), subToken: 0 },   // key -> token of the subMarks fetch that may still answer
            // The loaded recording ({rec, name, duration}; rec is the backend's handle) and its EVP marks.
            current: null, marks: [], markRegions: new Map(), markTimers: new Map(), markForm: null,
            formCls: "B", lastCls: "B", backup: null, backupNeeded: false, backupRunning: false, exportingMarked: false,
            backupEvents: new Map(), moveSeq: new Map(),     // rec -> backup events seen; mark id -> latest move
            moves: new Map(),                                // mark id -> {busy, next}: one update_mark move in flight per mark
            markGen: 0, markBusy: 0, reloadingMarks: false, reloadWanted: 0 };  // player mark calls: started (generation) and in flight

function api() { return window.pywebview.api; }
function fmtTime(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function key(device, folder, number) { return `${device}|${folder}:${number}`; }
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
function wavStatus() {
  const t = S.caps.wav_status || "WAV conversion is not available";
  return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".");
}

window.addEventListener("pywebviewready", async () => {
  S.caps = await api().capabilities();
  S.dest = await api().default_destination();
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
  $("update-notes").textContent = r.notes || "";
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
    if (!r.ok) {
      // Show the enumeration error once (e.g. libusb failed to load); do not
      // keep re-stamping the banner on every poll while it persists.
      if (!S.deviceError) { showError(r); S.deviceError = true; }
    } else {
      if (S.deviceError) { banner(""); S.deviceError = false; }
      S.devices = r.devices;
      renderDevices();
    }
  } finally {
    setTimeout(poll, 2000);            // next poll only after this one returned
  }
}

function deviceLabel(d, i) {
  if (d.state === "needs_driver" && !d.port) return "ST25 — needs setup";
  return d.owner ? `ST25 — ${d.owner}` : `ST25 #${i + 1} (port ${d.port})`;
}

function leaveDevice() {
  S.device = null; S.folders = {}; S.selected.clear(); S.loadSeq++;
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
    nav.innerHTML = '<p class="muted">No recorder connected. Plug in an ICD-ST25.</p>';
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
      for (const f of Object.keys(S.folders)) {
        const row = document.createElement("div");
        row.className = "folder" + (f === S.folder && S.view === "device" ? " selected" : "");
        row.innerHTML = `<span>Folder ${f}</span><span class="muted">${S.folders[f].length}</span>`;
        row.onclick = () => { S.folder = f; S.view = "device"; renderDevices(); renderMain(); };
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
  if (id === S.device && Object.keys(S.folders).length) {   // already read: just show it again
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
  for (const f of r.folders) S.folders[f.letter] = f.recordings;
  S.folder = r.folders.find((f) => f.recordings.length)?.letter || "A";
  renderDevices(); renderMain();
}

function renderMain() {
  const library = S.view === "library";
  $("device-table").hidden = library;
  $("library-table").hidden = $("library-bar").hidden = !library;
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
                     files: r.files, byId: new Map(r.files.map((f) => [f.id, f])), summaries: new Map() });
  for (const f of r.files) if (f.fp && !L.summaries.has(f.fp)) L.summaries.set(f.fp, fileSummary(f));
  // The backend is the source of truth: if it differs from the player (markers imported
  // meanwhile), the player fetches its marks again instead of overwriting the listing.
  if (S.current && S.current.fp && L.summaries.has(S.current.fp)) checkPlayerMarks(L.summaries.get(S.current.fp));
  for (const [event, p] of buffered) if (p.scan_id === L.scanId) libraryEvent(event, p);
  scheduleLibraryRender();
  // A newer scan started while this listing was on its way (listings racing each other).
  if (buffered.some(([, p]) => p.scan_id > L.scanId)) loadLibrary();
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

// The recordings: files grouped by fingerprint, in the order of their main file.
function libraryGroups() {
  const L = S.lib, groups = new Map(), order = new Map();
  L.files.forEach((f, i) => {
    order.set(f, i);
    const k = libFileKey(f);
    if (!groups.has(k)) groups.set(k, { key: k, files: [] });
    groups.get(k).files.push(f);
  });
  const out = [];
  for (const g of groups.values()) {
    const byName = g.files.slice().sort((a, b) => a.name.localeCompare(b.name));
    const main = byName.find((f) => f.type === "dvf") || byName[0];
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

function libraryMatches(g) {
  const L = S.lib;
  if (!LIB_FILTERS[L.filter](g)) return false;
  const q = L.search.trim().toLowerCase();
  if (!q) return true;
  const text = [...g.files.flatMap((f) => [f.name, f.investigation]), g.notes].join("\n").toLowerCase();
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
  L.groups = libraryGroups();
  $("library-count").textContent = L.listed && L.groups.length ? `(${L.groups.length})` : "";
  $("library-entry").title = L.folder || "";
  if (S.view !== "library") return;
  renderLibraryBar();
  const wanted = [], live = new Set();
  let shown = 0;
  for (const g of L.groups) {
    live.add(g.key);
    if (!libraryMatches(g)) continue;
    shown++;
    wanted.push(libraryRow(g));
    if (L.expanded.has(g.key)) wanted.push(...libraryMarkRows(g));
  }
  for (const k of L.rowEls.keys()) if (!live.has(k)) { L.rowEls.delete(k); L.subEls.delete(k); }
  if (L.truncated) {
    L.moreRow.firstChild.textContent = `Only the first ${L.files.length} files are shown.`;
    wanted.push(L.moreRow);
  }
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
  else if (!L.files.length) empty.textContent = `No .dvf or WAV files in ${L.folder} yet.`;
  else empty.textContent = "No recordings match.";
}

function renderLibraryBar() {
  const L = S.lib;
  $("library-folder").textContent = L.folder || "…";
  for (const b of document.querySelectorAll("#library-filters button")) b.setAttribute("aria-pressed", String(b.dataset.filter === L.filter));
  $("library-filters").hidden = !S.caps.marks;
  const st = $("library-status");
  st.classList.toggle("warn", !L.indexing && !!L.checkError);
  st.textContent = L.indexing ? `Checking recordings… ${L.done} of ${L.total}` : L.checkError ? "Could not check some recordings" : "";
  st.title = L.indexing ? "" : L.checkError;
}

function libraryPlayable(g) {                  // the file to play: the main one, or a WAV copy without the decoder
  return S.caps.wav ? g.main : g.files.find((f) => f.type === "wav") || null;
}

function libraryPlaying(g) { return g.files.some((f) => S.playing === `lib|${f.id}`); }

// One recording's row: created once per key, its cells rewritten only when something changed.
function libraryRow(g) {
  const L = S.lib;
  let tr = L.rowEls.get(g.key);
  if (!tr) {
    tr = document.createElement("tr");
    tr.className = "lib-row";
    for (let i = 0; i < 7; i++) tr.appendChild(document.createElement("td"));
    const toggle = document.createElement("button");
    toggle.className = "lib-toggle";
    toggle.onclick = (e) => { e.stopPropagation(); toggleLibraryRow(tr.group); };
    tr.cells[0].appendChild(toggle);
    tr.onclick = () => playLibrary(tr.group, null);
    L.rowEls.set(g.key, tr);
  }
  tr.group = g;
  const total = g.marks.A + g.marks.B + g.marks.C;
  const expanded = L.expanded.has(g.key);
  const playable = libraryPlayable(g);
  const sig = JSON.stringify([g.files.map((f) => [f.id, f.name, f.investigation, f.type]), g.seconds, g.marks, g.reviewed,
                              g.error, g.fp, L.indexing, expanded, libraryPlaying(g), !!playable]);
  if (tr.sig === sig) return tr;
  tr.sig = sig;
  tr.classList.toggle("playing", libraryPlaying(g));
  tr.classList.toggle("unplayable", !playable);
  tr.title = playable ? "" : "Can't play .dvf files. " + wavStatus();
  const [cToggle, cName, cInv, cType, cLen, cEvp, cRev] = tr.cells;
  const toggle = cToggle.firstChild;
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
  const L = S.lib, marks = L.subMarks.get(g.key);
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
  const L = S.lib, key = g.key, seq = L.seq;
  const token = setSubMarks(key, null);       // loading
  const r = await api().library_marks(g.main.id);
  if (seq !== L.seq || L.subTokens.get(key) !== token) return;   // relisted, updated or fetched again meanwhile
  setSubMarks(key, r.ok ? sortMarks(r.marks) : { error: [r.error, r.advice].filter(Boolean).join(" ") });
  scheduleLibraryRender();
}

function toggleLibraryRow(g) {
  const L = S.lib;
  if (!L.expanded.delete(g.key)) L.expanded.add(g.key);
  scheduleLibraryRender();
}

// Play a recording (mark = null), or one of its EVPs: loaded first unless it is in the player already.
async function playLibrary(g, mark) {
  if (mark && S.current && g.fp && S.current.fp === g.fp) {
    playMark(S.marks.find((m) => m.id === mark.id) || mark);
    return;
  }
  const f = libraryPlayable(g);
  if (!f) { banner("Can't play .dvf files. " + wavStatus() + " WAV files still play."); return; }
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
  S.reloadWanted++;
  if (!S.reloadingMarks) reloadPlayerMarks(S.current.rec);
}

// Run a mark-changing call of the player; reloadPlayerMarks never applies an answer that
// overlapped one (it could miss that change).
async function markCall(call) {
  S.markGen++; S.markBusy++;
  try { return await call(); } finally { S.markBusy--; S.markGen++; }
}

function playerSaving() { return S.markBusy > 0 || S.markTimers.size > 0 || S.moves.size > 0; }

// Fetch the loaded recording's marks until an answer that no change of the player's own
// overlapped has been applied after the latest request for one (checkPlayerMarks).
async function reloadPlayerMarks(rec) {
  S.reloadingMarks = true;
  try {
    for (let tries = 0; tries < 50 && showing(rec); tries++) {
      if (playerSaving()) { await new Promise((res) => setTimeout(res, 300)); continue; }
      const want = S.reloadWanted, gen = S.markGen;
      const r = await api().get_marks(rec);
      if (!showing(rec) || !r.ok) return;
      if (gen !== S.markGen || playerSaving()) continue;        // overlapped a change of its own: ask again
      applyPlayerMarks(r);
      if (want === S.reloadWanted) return;                      // no newer backend report meanwhile
    }
  } finally {
    S.reloadingMarks = false;
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
    if (d) { S.lib.folder = d; S.lib.expanded.clear(); loadLibrary(); }
  };
  for (const b of document.querySelectorAll("#library-filters button")) {
    b.onclick = () => { S.lib.filter = b.dataset.filter; scheduleLibraryRender(); };
  }
  $("library-search").oninput = () => { S.lib.search = $("library-search").value; scheduleLibraryRender(); };
  const more = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = 7; td.className = "muted";
  more.appendChild(td);
  S.lib.moreRow = more;
}

function renderRows() {
  const rows = $("rows");
  rows.innerHTML = "";
  const recs = S.device ? (S.folders[S.folder] || []) : [];
  $("empty").hidden = recs.length > 0;
  $("empty").textContent = S.device ? `Folder ${S.folder} is empty.` : "Plug in an ICD-ST25 and select it on the left.";
  for (const r of recs) {
    const tr = document.createElement("tr");
    const k = key(S.device, S.folder, r.number);
    if (S.playing === k) tr.className = "playing";
    const box = document.createElement("input");
    box.type = "checkbox"; box.checked = S.selected.has(k); box.disabled = !!r.problem;
    box.onclick = (e) => { e.stopPropagation(); box.checked ? S.selected.add(k) : S.selected.delete(k); updateExport(); };
    const cells = [String(r.number).padStart(3, "0"), r.when === "no date" ? "undated" : r.when, fmtTime(r.seconds), r.problem];
    const first = document.createElement("td"); first.appendChild(box); tr.appendChild(first);
    cells.forEach((c, i) => { const td = document.createElement("td"); td.textContent = c; if (i === 3) td.className = "note"; tr.appendChild(td); });
    if (S.caps.wav) tr.onclick = () => play(S.device, S.folder, r.number);
    rows.appendChild(tr);
  }
  $("all").checked = recs.length > 0 && recs.every((r) => r.problem || S.selected.has(key(S.device, S.folder, r.number)));
  updateExport();
}

$("all").onclick = () => {
  for (const r of S.folders[S.folder] || []) {
    if (r.problem) continue;
    const k = key(S.device, S.folder, r.number);
    $("all").checked ? S.selected.add(k) : S.selected.delete(k);
  }
  renderMain();
};

function selectedItems() {
  const prefix = `${S.device}|`;
  return [...S.selected].filter((k) => k.startsWith(prefix)).map((k) => {
    const [folder, number] = k.slice(prefix.length).split(":");
    return { folder, number: Number(number) };
  });
}

function updateExport() {
  const n = S.device && S.view === "device" ? selectedItems().length : 0;
  $("export").disabled = S.exporting || n === 0;
  if (S.exporting) return;                 // the button shows progress while exporting
  $("export").textContent = n ? `Export ${n} selected` : "Export selected";
}

$("dest").onclick = async () => {
  const d = await api().choose_destination();
  if (d) { S.dest = d; $("dest").textContent = d; loadLibrary(); }   // the library may be the save folder
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
  if (event.startsWith("library-")) { libraryEvent(event, p); return; }                           // nor these
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

async function play(device, folder, number) {
  if (!S.caps.wav) return;
  const seq = ++S.playSeq;
  const label = `${folder}-${String(number).padStart(3, "0")}`;
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
const READ_ONLY_TIP = "Another OpenEVP window is open; marks can only be changed there.";
const NO_MARKS_TIP = "Marks are not available here.";

function isMark(r) { return r.id.startsWith("mark-"); }
function marksWritable() { return !!S.caps.marks && !S.caps.marks_read_only; }
function marksTip() { return !S.caps.marks ? NO_MARKS_TIP : S.caps.marks_read_only ? READ_ONLY_TIP : ""; }
function showing(rec) { return !!S.current && S.current.rec === rec; }   // is this handle's recording still in the player?
function backupEventsSeen(rec) { return S.backupEvents.get(rec) || 0; }
function isPoint(m) { return m.end <= m.start; }        // imported point markers: no length to drag
function typingIn(el) {
  return el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "SELECT" ||
         (el.tagName === "INPUT" && !/^(checkbox|radio|range|button)$/.test(el.type));
}

function setupMarks() {
  $("reviewed-label").hidden = !S.caps.marks;
  $("reviewed").disabled = $("mark-evp").disabled = !marksWritable();
  $("reviewed-label").title = marksTip() || $("reviewed-label").title;
  $("mark-evp").title = marksTip() || $("mark-evp").title;
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
  $("export-marked").disabled = !S.marks.length || S.exportingMarked;
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
