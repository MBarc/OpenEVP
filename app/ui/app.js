"use strict";
// Talks to app/backend.py through pywebview. All recorder I/O happens there on one thread.
// Every async response is checked against the request it answers (sequence numbers,
// device id), so a slow answer for a recorder the user has left is dropped.
const $ = (id) => document.getElementById(id);
const S = { devices: [], device: null, folder: "A", folders: {}, caps: { wav: false },
            dest: "", selected: new Set(), ws: null, playing: null,
            loadSeq: 0, playSeq: 0, job: 0, exporting: false, deviceError: false, settingUp: false,
            view: "device", saved: null, savedSeq: 0,     // view: "device" (a recorder) or "saved" (this PC)
            // The loaded recording ({rec, name, duration}; rec is the backend's handle) and its EVP marks.
            current: null, marks: [], markRegions: new Map(), markTimers: new Map(), markForm: null,
            formCls: "B", lastCls: "B", backup: null, backupRunning: false, exportingMarked: false,
            backupEvents: new Map(), moveSeq: new Map() };   // rec -> backup events seen; mark id -> latest move

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
  loadSaved();
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
  if (S.playing) { S.playSeq++; S.ws.empty(); S.playing = null; setCurrent(null); showPlayerEmpty(); }  // a file stays loaded
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
  if (full) {
    S.ws.setOptions({ sampleRate: r.rate });
    await S.ws.load(r.url);
  } else {
    await S.ws.load(r.url, [r.peaks], r.duration);
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
  $("saved-entry").className = "device-name" + (S.view === "saved" ? " selected" : "");
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
  const saved = S.view === "saved";
  $("device-table").hidden = saved;
  $("saved-table").hidden = !saved;
  if (saved) renderSaved(); else renderRows();
  updateExport();
}

// ---- Saved recordings: the files in the save folder (the "active folder") ----
async function loadSaved() {
  const seq = ++S.savedSeq;
  const r = await api().list_saved();
  if (seq !== S.savedSeq) return;
  S.saved = r;
  $("saved-count").textContent = r.files.length ? `(${r.files.length})` : "";
  $("saved-entry").title = r.folder;
  if (S.view === "saved") renderSaved();
}

function showSaved() {
  S.view = "saved"; banner("");
  renderDevices(); renderMain();
  loadSaved();
}

function renderSaved() {
  const rows = $("saved-rows");
  rows.innerHTML = "";
  const r = S.saved;
  const files = r ? r.files : [];
  $("empty").hidden = files.length > 0;
  if (!r) $("empty").textContent = "Loading…";
  else if (!r.exists) $("empty").textContent = "Nothing saved yet. Export recordings from a recorder and they appear here.";
  else if (!files.length) $("empty").textContent = `No .dvf or WAV files in ${r.folder} yet.`;
  for (const f of files) {
    const tr = document.createElement("tr");
    if (S.playing === `file|${f.id}`) tr.className = "playing";
    const playable = f.type === "wav" || S.caps.wav;
    if (!playable) { tr.classList.add("unplayable"); tr.title = "Can't play .dvf files. " + wavStatus(); }
    const badge = `<span class="badge badge-${f.type}">${f.type.toUpperCase()}</span>`;
    const cells = [f.name, f.folder || "—", badge, f.seconds == null ? "" : fmtTime(f.seconds), f.modified];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      if (i === 2) td.innerHTML = c; else td.textContent = c;
      tr.appendChild(td);
    });
    tr.onclick = () => playSaved(f);
    rows.appendChild(tr);
  }
  if (r && r.truncated) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="5" class="muted">Only the first ${files.length} files are shown.</td>`;
    rows.appendChild(tr);
  }
}

async function playSaved(f) {
  if (f.type === "dvf" && !S.caps.wav) {
    banner("Can't play .dvf files. " + wavStatus() + " WAV files still play.");
    return;
  }
  const seq = ++S.playSeq;
  banner(""); status(`Loading ${f.name}…`);
  const r = await api().play_saved(f.id);
  if (seq !== S.playSeq) return;
  if (!r.ok) { status(""); showError(r); loadSaved(); return; }
  S.playing = `file|${f.id}`; renderSaved();
  await loadIntoPlayer(seq, f.name, r, true);
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

$("saved-entry").onclick = showSaved;

$("dest").onclick = async () => {
  const d = await api().choose_destination();
  if (d) { S.dest = d; $("dest").textContent = d; loadSaved(); }
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
    loadSaved();
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
  S.ws.on("error", (e) => { banner(`Could not play this recording: ${e}`); $("play").disabled = true; });
  $("play").onclick = () => S.ws.playPause();
  $("zoom").oninput = () => S.ws.zoom(Number($("zoom").value));
  $("height").oninput = () => S.ws.setOptions({ barHeight: Number($("height").value) });
  $("waveform").addEventListener("wheel", wheelZoom, { passive: false });
  setupSelection();                         // once: the plugin stays registered across loads
  setupMarks();
  $("open-wav").onclick = openWav;
  $("open-wav-2").onclick = openWav;
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
  S.current = r ? { rec: r.rec, name: label, duration: r.duration } : null;
  S.marks = r ? sortMarks(r.marks || []) : [];
  S.backup = r ? r.backup : null;
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
function replaceMark(mark) {
  S.marks = sortMarks(S.marks.map((m) => (m.id === mark.id ? mark : m)));
  const region = S.markRegions.get(mark.id);
  if (region) region.setOptions({ start: mark.start, end: mark.end, color: MARK_COLORS[mark.cls], content: mark.cls });
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
  const { rec, duration } = S.current, cls = S.formCls, events = backupEventsSeen(rec);
  const note = $("mark-note").value.trim();
  $("mark-save").disabled = true;
  const r = form.id
    ? await api().update_mark(rec, form.id, null, null, cls, note)
    : await api().add_mark(rec, S.region.start, Math.min(S.region.end, duration || S.region.end), cls, note);
  $("mark-save").disabled = false;
  if (!showing(rec)) return;                              // another recording: its form was closed
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
}

// ---- dragging a mark's band (or an edge) saves the new times after a short pause ----
function scheduleMarkMove(region) {
  const id = region.id.slice("mark-".length);
  const old = S.marks.find((m) => m.id === id);
  if (!S.current || !old) return;
  clearTimeout(S.markTimers.get(id));
  S.markTimers.delete(id);
  if (Math.abs(old.start - region.start) < 1e-6 && Math.abs(old.end - region.end) < 1e-6) return;   // a click, not a move
  const { rec, duration } = S.current, move = (S.moveSeq.get(id) || 0) + 1;
  S.moveSeq.set(id, move);                                   // answers to earlier moves are now stale
  S.markTimers.set(id, setTimeout(() => { S.markTimers.delete(id); saveMarkMove(rec, move, id, region, duration); }, 300));
}

async function saveMarkMove(rec, move, id, region, duration) {
  const end = Math.min(region.end, duration || region.end);
  const r = await api().update_mark(rec, id, region.start, end, null, null);
  if (S.moveSeq.get(id) !== move) return;                    // a later move of this mark decides
  if (!r.ok) showError(r);                                   // also after switching: the move was not saved
  if (!showing(rec)) return;
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
  const rec = S.current.rec;
  const r = await api().update_mark(rec, m.id, null, null, null, note);
  if (!r.ok) showError(r);
  if (!showing(rec)) return;
  if (r.ok) { replaceMark(r.mark); return; }
  renderMarks();                                             // the typed note stays in its field to try again
  const span = $("marks-list").querySelector(`.mark-note[data-id="${m.id}"]`);
  if (span) editNoteInline(span, m, note);
}

async function deleteMark(m) {
  if (!S.current || !confirm("Delete this mark?")) return;
  const rec = S.current.rec;
  const r = await api().delete_mark(rec, m.id);
  if (!showing(rec)) return;
  if (!r.ok) { showError(r); return; }
  clearTimeout(S.markTimers.get(m.id));
  S.markTimers.delete(m.id);
  const region = S.markRegions.get(m.id);
  if (region) region.remove();
  S.markRegions.delete(m.id);
  S.marks = S.marks.filter((x) => x.id !== m.id);
  if (S.markForm && S.markForm.id === m.id) closeMarkForm();
  renderMarks();
}

async function setReviewed() {
  if (!S.current) return;
  const want = $("reviewed").checked, rec = S.current.rec;
  const r = await api().set_reviewed(rec, want);
  if (!showing(rec)) return;
  if (!r.ok) { $("reviewed").checked = !want; showError(r); }
}

// ---- backup of a marked recorder recording, and the WAV export with marks ----
function renderBackup() {
  const el = $("backup-status"), b = S.backup || { status: null, detail: "" };
  const saved = !S.backupRunning && b.status === "saved";
  const failed = !S.backupRunning && b.status === "failed";
  el.className = saved ? "ok" : failed ? "warn" : "";
  el.textContent = S.backupRunning ? "Backing up…" : saved ? "Backed up to your save folder"
    : failed ? b.detail || "The backup failed." : "";      // the detail names the recording and why
  el.title = saved ? b.detail : "";
  el.hidden = !el.textContent;
  $("retry-backup").hidden = !failed;
  $("retry-backup").disabled = !marksWritable();
  $("retry-backup").title = marksTip();
}

function backupEvent(event, p) {
  const done = event === "backup-done";
  S.backupEvents.set(p.rec, backupEventsSeen(p.rec) + 1);
  if (done) banner(`✓ Backed up ${p.label} so this EVP is safe even if the recorder is wiped. ${p.detail}`, "ok");
  else banner(p.detail, "warn", marksWritable() ? { label: "Retry backup", run: () => retryBackup(p.rec) } : null);
  if (done) loadSaved();
  if (!S.current) return;
  if (p.rec !== S.current.rec) { refreshBackup(); return; }   // the same recording may be loaded again under a new handle
  S.backupRunning = false;
  S.backup = { status: done ? "saved" : "failed", detail: p.detail };
  renderBackup();
}

async function refreshBackup() {
  const rec = S.current.rec;
  const r = await api().get_marks(rec);
  if (!showing(rec) || !r.ok) return;
  S.backup = r.backup;
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
  loadSaved();
}
