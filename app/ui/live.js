"use strict";
// ---- Live mode and analog import: record from one of the PC's audio inputs (app/live.py) ----
// The input is opened with getUserMedia, with echo cancellation, noise suppression and automatic
// gain off, so the file holds the input exactly as Windows delivers it. An AudioWorklet
// (live-worklet.js) turns it into 16-bit PCM; while recording, the page sends it to the backend
// in numbered chunks of about half a second, one call at a time (live_chunk), and the backend
// writes the WAV as it goes. An AnalyserNode feeds the scrolling spectrogram; the waveform and
// the level meter come from the worklet's batches. Both draw on animation frames only, scrolling
// what is already drawn, so an hour costs no more than a minute.
// Listening (monitoring) is off whenever the view opens; with "Enhance what I hear" the player's
// Enhance settings apply to what is heard only. The saved file is always the raw input.
const LV = {
  settings: null,                        // live_settings(): input, split, import
  mode: "live",                          // "live" or "import"
  stream: null, ctx: null, src: null, node: null, analyser: null, monitor: [], opening: 0,
  rate: 0, channels: 0, deviceId: "", label: "", devices: [],
  rec: null,                             // the recording: {session, seq, pending, pendingFrames, frames, inflight, stopping, marks}
  meter: { peak: 0, rms: 0, held: 0, heldAt: 0 },
  wave: { cols: [], min: 32768, max: -32769, n: 0, perCol: 0 },
  spec: { bins: null, last: 0, lut: null, img: null },
  raf: 0, lastStatus: 0, drawn: 0, color: "",
};
const LIVE_COLS_PER_SEC = 40;            // waveform and spectrogram: the same time scale, 25 ms a column
const LIVE_CHUNK_SEC = 0.5;              // audio sent to the backend per call
const LIVE_SPEC_HZ = 8000;               // the spectrogram's top (as the player's)
const LIVE_DB_FLOOR = -100, LIVE_DB_TOP = -25;   // its range: 75 dB, as the player's
const MIC_SETTINGS = { label: "Open microphone settings", run: () => api().open_mic_settings() };

function liveOpen() { return S.view === "live"; }
function liveRecording() { return !!LV.rec; }
function liveStatus(text, kind = "") { const el = $("live-status"); el.textContent = text || ""; el.className = kind; }

// ---- opening and leaving the view ----
async function openLive() {
  if (S.exporting) { banner("Wait for the export to finish, then record."); return; }
  if (!S.caps.marks || S.caps.marks_read_only) {
    banner(S.caps.marks_read_only_reason ? `Recording is not available: ${S.caps.marks_read_only_reason}`
                                         : "Recording is not available: OpenEVP's data folder could not be opened.");
    return;
  }
  if (S.current && S.ws) S.ws.pause();
  S.view = "live"; banner("");
  renderDevices(); renderMain();
  if (!LV.settings) {
    const r = await api().live_settings();
    LV.settings = r.ok ? r : { input: null, split: 3, import: false, max_split: 60 };
  }
  setLiveMode(LV.settings.import ? "import" : "live", false);
  $("live-split").checked = LV.settings.split > 0;
  $("live-gap").value = String(LV.settings.split > 0 ? LV.settings.split : 3);
  $("live-gap").disabled = !$("live-split").checked;
  $("live-monitor").checked = false;          // never on by itself: speakers next to a microphone feed back
  $("live-enhance").checked = false;
  if (!S.lib.listed) await loadLibrary();
  liveFolders();
  renderLive();
  await openInput(LV.settings.input);
}

// Called by renderMain whenever another view is shown: the input is let go of.
function liveViewLeft() {
  if (!LV.stream && !LV.ctx) return;
  closeInput();
}

// The folders a recording can go into: the library's (not Clips folders), the one shown in the
// library picked (else the library folder itself).
function liveFolders() {
  const sel = $("live-folder"), L = S.lib, keep = sel.value;
  sel.textContent = "";
  const folders = (L.folders || []).filter((d) => !d.in_clips);
  if (!folders.some((d) => d.id === "root")) folders.unshift({ id: "root", rel: [], name: "" });
  for (const d of folders) {
    const o = document.createElement("option");
    o.value = d.id;
    o.textContent = d.id === "root" ? "EVP Library (top folder)" : d.rel.join(" › ");
    sel.appendChild(o);
  }
  const shown = !L.flat && folders.some((d) => d.id === L.folderId) ? L.folderId : "root";
  sel.value = folders.some((d) => d.id === keep) ? keep : shown;
}

function setLiveMode(mode, save = true) {
  if (liveRecording()) return;
  LV.mode = mode;
  $("live-mode-live").setAttribute("aria-pressed", String(mode === "live"));
  $("live-mode-import").setAttribute("aria-pressed", String(mode === "import"));
  $("live-import").hidden = mode !== "import";
  $("live-title").textContent = mode === "import" ? "Import from a recorder" : "Record live";
  if (save) saveLive({ import: mode === "import" });
  renderLive();
}

async function saveLive(changes) {
  const r = await api().set_live_settings(changes);
  if (r.ok) LV.settings = r;
  else liveStatus(r.error, "warn");
}

function liveGap() {
  const v = Number($("live-gap").value);
  return isFinite(v) && v >= 0.5 && v <= (LV.settings ? LV.settings.max_split : 60) ? Math.round(v * 2) / 2 : null;
}

function splitChanged() {
  $("live-gap").disabled = !$("live-split").checked || liveRecording();
  const gap = liveGap();
  if ($("live-split").checked && gap === null) { liveStatus("The silence gap must be between 0.5 and 60 seconds.", "warn"); return; }
  liveStatus("");
  saveLive({ split: $("live-split").checked ? gap : 0 });
}

// The buttons and fields: what can be changed now.
function renderLive() {
  const rec = liveRecording(), stopping = rec && LV.rec.stopping;
  const ready = !!LV.node && !LV.opening;
  $("live-record").textContent = rec ? (stopping ? "Saving…" : "■ Stop") : "● Record";
  $("live-record").disabled = stopping || (!rec && !ready);
  $("live-record").classList.toggle("recording", rec);
  $("live-mark").disabled = !rec || stopping;
  for (const id of ["live-input", "live-folder", "live-mode-live", "live-mode-import", "live-split", "live-close"]) $(id).disabled = rec;
  $("live-gap").disabled = rec || !$("live-split").checked;
  $("live-enhance").disabled = !$("live-monitor").checked;
  document.body.classList.toggle("recording", rec);
  if (!rec) $("live-time").textContent = "0:00";
}

// ---- the input ----
// exact: that input or fail; otherwise it is only preferred (a remembered id may be stale).
function audioConstraints(deviceId, exact) {
  const a = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: { ideal: 2 } };
  if (deviceId) a.deviceId = exact ? { exact: deviceId } : { ideal: deviceId };
  return { audio: a, video: false };
}

// Why getUserMedia failed, in plain words, and whether Windows' privacy setting may be the cause.
function inputProblem(e) {
  const name = e && e.name;
  if (name === "NotAllowedError" || name === "SecurityError")
    return ["Windows is not letting OpenEVP use the microphone. In Settings, Privacy & security, Microphone, " +
            "turn on \"Microphone access\" and \"Let desktop apps access your microphone\".", true];
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return ["No audio input was found. Plug in a microphone or audio adapter, then choose it here.", false];
  if (name === "NotReadableError" || name === "AbortError")
    return ["The input could not be opened. Another program may be using it, or Windows may be blocking " +
            "desktop apps from the microphone (Settings, Privacy & security, Microphone).", true];
  return [`The input could not be opened: ${(e && e.message) || e}`, false];
}

// Open an input: the one asked for (by id, else by name: ids can change between sessions), else
// Windows' default. Asking for the default first is what lets the page see the inputs' names.
async function openInput(want) {
  const seq = ++LV.opening;
  closeInput(true);
  renderLive();
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    liveStatus("Recording is not available in this window.", "warn"); LV.opening = 0; renderLive(); return false;
  }
  liveStatus("Opening the input…");
  try {
    let stream = await navigator.mediaDevices.getUserMedia(audioConstraints(want && want.id, false));
    if (seq !== LV.opening) { stream.getTracks().forEach((t) => t.stop()); return false; }
    await listInputs();
    const track = stream.getAudioTracks()[0];
    const have = track.getSettings().deviceId || "";
    const target = want && (LV.devices.find((d) => d.deviceId === want.id) || LV.devices.find((d) => d.label === want.label));
    if (target && target.deviceId !== have) {
      stream.getTracks().forEach((t) => t.stop());
      stream = await navigator.mediaDevices.getUserMedia(audioConstraints(target.deviceId, true));
      if (seq !== LV.opening) { stream.getTracks().forEach((t) => t.stop()); return false; }
    }
    await startGraph(stream);
    if (seq !== LV.opening) { closeInput(true); return false; }
    LV.opening = 0;
    liveStatus(want && target === undefined && want.label
      ? `"${want.label}" is not connected, so ${LV.label || "the default input"} is used.` : "");
    showInputs();
    renderLive();
    return true;
  } catch (e) {
    if (seq !== LV.opening) return false;
    LV.opening = 0;
    const [text, settings] = inputProblem(e);
    liveStatus(text, "warn");
    if (settings) banner(text, "warn", MIC_SETTINGS);
    await listInputs().catch(() => {});
    showInputs();
    renderLive();
    return false;
  }
}

async function listInputs() {
  const all = await navigator.mediaDevices.enumerateDevices();
  LV.devices = all.filter((d) => d.kind === "audioinput");
}

function showInputs() {
  const sel = $("live-input");
  sel.textContent = "";
  LV.devices.forEach((d, i) => {
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || `Input ${i + 1}`;
    sel.appendChild(o);
  });
  if (!LV.devices.length) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "No input found";
    sel.appendChild(o);
  }
  sel.value = LV.deviceId;
}

async function startGraph(stream) {
  const track = stream.getAudioTracks()[0];
  const st = track.getSettings();
  const AC = window.AudioContext || window.webkitAudioContext;
  let ctx;
  try { ctx = new AC({ sampleRate: st.sampleRate || undefined, latencyHint: "playback" }); }
  catch (e) { ctx = new AC({ latencyHint: "playback" }); }     // a rate Web Audio refuses: its own (48 kHz)
  try {
    await ctx.audioWorklet.addModule("live-worklet.js");
    const channels = Math.min(2, st.channelCount || 1) === 2 ? 2 : 1;
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "openevp-capture", {
      numberOfInputs: 1, numberOfOutputs: 0, channelCount: channels, channelCountMode: "explicit",
      channelInterpretation: "discrete", processorOptions: { channels, batch: 2048 } });
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048; analyser.smoothingTimeConstant = 0;
    analyser.minDecibels = LIVE_DB_FLOOR; analyser.maxDecibels = LIVE_DB_TOP;
    src.connect(node); src.connect(analyser);
    node.port.onmessage = (e) => liveAudio(e.data);
    Object.assign(LV, { stream, ctx, src, node, analyser, rate: ctx.sampleRate, channels,
                        deviceId: st.deviceId || "", label: track.label || "" });
    LV.wave.perCol = Math.max(1, Math.round(ctx.sampleRate / LIVE_COLS_PER_SEC));
    LV.spec.bins = new Uint8Array(analyser.frequencyBinCount);
    track.onended = inputEnded;
    if (ctx.resume) await ctx.resume();
    applyMonitor();
    if (!LV.raf) LV.raf = requestAnimationFrame(liveFrame);
  } catch (e) {
    try { ctx.close(); } catch (x) { /* already closed */ }
    stream.getTracks().forEach((t) => t.stop());
    throw e;
  }
}

function closeInput(keepSeq = false) {
  if (!keepSeq) LV.opening = 0;
  if (LV.node) { LV.node.port.onmessage = null; try { LV.node.disconnect(); } catch (e) { /* gone */ } }
  if (LV.stream) for (const t of LV.stream.getTracks()) { t.onended = null; t.stop(); }
  if (LV.ctx) { try { LV.ctx.close(); } catch (e) { /* gone */ } }
  Object.assign(LV, { stream: null, ctx: null, src: null, node: null, analyser: null, monitor: [] });
  LV.meter.peak = LV.meter.rms = 0;
  if (LV.raf) { cancelAnimationFrame(LV.raf); LV.raf = 0; }
}

// The input went away (an adapter unplugged): a recording is saved, and the user told.
async function inputEnded() {
  const text = "The input stopped (was it unplugged?).";
  if (liveRecording()) { await stopRecording({ reason: `${text} The recording was saved.` }); }
  closeInput();
  liveStatus(`${text} Choose an input to go on.`, "warn");
  renderLive();
}

// ---- listening ----
function applyMonitor() {
  const ctx = LV.ctx;
  if (!ctx) return;
  for (const n of LV.monitor) { try { n.disconnect(); } catch (e) { /* gone */ } }
  LV.monitor = [];
  try { LV.src.disconnect(); } catch (e) { /* not connected */ }
  LV.src.connect(LV.node); LV.src.connect(LV.analyser);
  if (!$("live-monitor").checked) return;
  const stages = $("live-enhance").checked && S.enh.settings ? enhanceGraph(S.enh.settings, ctx.sampleRate) : [];
  LV.monitor = stages.flatMap((st) => makeNodes(ctx, st));
  let at = LV.src;
  for (const n of LV.monitor) { at.connect(n); at = n; }
  at.connect(ctx.destination);
}

// ---- audio from the worklet: meter, waveform, and the chunks a recording sends ----
function liveAudio(m) {
  const pcm = new Int16Array(m.pcm), ch = LV.channels, n = m.frames;
  const meter = LV.meter;
  meter.peak = Math.max(meter.peak * 0.85, m.peak);
  meter.rms = Math.sqrt(m.sumsq / Math.max(1, n * ch));
  if (m.peak >= meter.held || performance.now() - meter.heldAt > 1500) { meter.held = m.peak; meter.heldAt = performance.now(); }
  const w = LV.wave;
  for (let i = 0; i < n; i++) {
    let v = pcm[i * ch];
    if (ch === 2) v = (v + pcm[i * ch + 1]) / 2;
    if (v < w.min) w.min = v;
    if (v > w.max) w.max = v;
    if (++w.n >= w.perCol) {
      w.cols.push([w.min / 32768, w.max / 32768]);
      if (w.cols.length > 4000) w.cols.splice(0, w.cols.length - 4000);   // nothing drew them (a hidden window)
      w.min = 32768; w.max = -32769; w.n = 0;
    }
  }
  const rec = LV.rec;
  if (rec && !rec.stopping && !rec.ended) {
    rec.pending.push(pcm);
    rec.pendingFrames += n;
    rec.frames += n;
    if (rec.pendingFrames >= LV.rate * LIVE_CHUNK_SEC) sendChunk(rec);
  }
}

function toBase64(int16s) {
  let total = 0;
  for (const p of int16s) total += p.length;
  const all = new Int16Array(total);
  let at = 0;
  for (const p of int16s) { all.set(p, at); at += p.length; }
  const bytes = new Uint8Array(all.buffer);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// One call at a time, in order: the next chunk waits for the previous answer.
function sendChunk(rec) {
  if (!rec.pending.length) return rec.inflight;
  const data = toBase64(rec.pending), seq = rec.seq++;
  rec.pending = []; rec.pendingFrames = 0;
  rec.inflight = rec.inflight.then(async () => {
    if (rec.ended) return;
    let r;
    try { r = await api().live_chunk(rec.session, seq, data); }
    catch (e) { r = { ok: false, error: `${(e && e.message) || e}` }; }
    if (rec.ended) return;
    if (!r.ok) { rec.failed = r.error; return; }
    rec.status = r;
    if (r.stopped) { rec.ended = true; rec.stoppedBy = r; }
  });
  rec.inflight.then(() => afterChunk(rec));
  return rec.inflight;
}

// The backend's answer to a chunk: the status line, or the end the backend chose.
function afterChunk(rec) {
  if (LV.rec !== rec) return;
  if (rec.failed && !rec.stopping) {
    const why = rec.failed;
    rec.failed = null;
    stopRecording({ reason: `Recording stopped: ${why}` });
    return;
  }
  if (rec.stoppedBy && !rec.stopping) { recordingDone(rec, rec.stoppedBy.result, rec.stoppedBy.stopped); return; }
  const r = rec.status;
  if (!r) return;
  if (LV.mode === "import") {
    $("live-file").textContent = r.file ? `Recording ${r.file}` : "Waiting for sound…";
    if (r.saved) $("live-file").textContent += ` · ${plural(r.saved, "file")} saved`;
  }
  liveStatus(r.warning || "", r.warning ? "warn" : "");
}

// ---- record, mark, stop ----
async function startRecording() {
  if (liveRecording() || !LV.node) return null;
  const gap = liveGap();
  const split = LV.mode === "import" && $("live-split").checked ? gap : 0;
  if (split === null) { liveStatus("The silence gap must be between 0.5 and 60 seconds.", "warn"); return null; }
  banner(""); liveStatus("");
  const folder = $("live-folder").value || "root";
  const r = await api().live_start({ mode: LV.mode, folder, rate: LV.rate, channels: LV.channels, split });
  if (!r.ok) { liveStatus([r.error, r.advice].filter(Boolean).join(" "), "warn"); return r; }
  LV.rec = { session: r.session, seq: 0, pending: [], pendingFrames: 0, frames: 0, inflight: Promise.resolve(),
             stopping: false, ended: false, marks: [], folder, status: null, failed: null, stoppedBy: null };
  LV.wave.cols = [];
  $("live-marks").textContent = "";
  $("live-file").textContent = r.file ? `Recording ${r.file}` : "Waiting for sound…";
  renderLive();
  return r;
}

// M: the moment heard now, as a mark (a 2-second region ending now; class and note can be changed
// in the player afterwards).
async function liveMark() {
  const rec = LV.rec;
  if (!rec || rec.stopping || rec.ended) return null;
  const at = rec.frames / LV.rate;
  sendChunk(rec);                                      // the audio up to now, before the mark
  await rec.inflight;
  const r = await api().live_mark(rec.session, at);
  if (!r.ok) { liveStatus(r.error, "warn"); return r; }
  rec.marks.push(r.mark);
  const li = document.createElement("li");
  li.textContent = `★ ${fmtTime(at)}` + (LV.mode === "import" ? ` (${r.mark.file})` : "");
  $("live-marks").appendChild(li);
  return r;
}

async function stopRecording(opts = {}) {
  const rec = LV.rec;
  if (!rec || rec.stopping) return null;
  rec.stopping = true;
  renderLive();
  liveStatus("Saving…");
  sendChunk(rec);
  await rec.inflight;
  let r;
  if (rec.stoppedBy) r = rec.stoppedBy.result;
  else {
    try { r = await api().live_stop(rec.session); } catch (e) { r = { ok: false, error: `${(e && e.message) || e}` }; }
  }
  return recordingDone(rec, r, opts.reason || (rec.stoppedBy && rec.stoppedBy.stopped) || "", opts);
}

// The recording is over (Stop, or the backend stopped it): say what was saved; a Live recording
// opens in the player, in its library folder.
async function recordingDone(rec, r, reason, opts = {}) {
  if (LV.rec !== rec) return r;
  LV.rec = null;
  rec.ended = true;
  renderLive();
  liveStatus("");
  $("live-file").textContent = "";
  if (!r || !r.ok) { banner(`The recording could not be saved: ${(r && r.error) || "unknown error"}`); return r; }
  const files = r.files || [];
  const notes = [...(r.problems || [])];
  if (r.dropped_marks) notes.push(`${plural(r.dropped_marks, "mark")} fell outside the saved audio and were left out.`);
  let msg;
  if (!files.length) msg = LV.mode === "import" ? "Nothing was saved: no sound arrived." : "Nothing was saved.";
  else if (files.length === 1) msg = `✓ Saved ${files[0].name} in ${r.folder}.`;
  else msg = `✓ Saved ${plural(files.length, "recording")} in ${r.folder}.`;
  const text = [reason, msg, ...notes].filter(Boolean).join(" ");
  banner(text, reason || notes.length || !files.length ? "warn" : "ok");
  if (opts.open === false) return r;
  await loadLibrary();
  if (r.player && files.length) {
    // The recording opens in the player, with the library showing its folder.
    closeInput();
    if (S.lib.folderById.has(rec.folder)) openLibraryFolder(rec.folder);
    S.view = "library"; renderDevices(); renderMain();
    const seq = ++S.playSeq;
    S.playing = `lib|${files[0].id}`;
    scheduleLibraryRender();
    await loadIntoPlayer(seq, files[0].name, r.player, false);
    banner(text, reason || notes.length ? "warn" : "ok");     // loading clears nothing, but say it last
  }
  return r;
}

// ---- drawing: meter, waveform, spectrogram (animation frames only) ----
const LIVE_FRAME_MS = 1000 / 30;           // drawn at most 30 times a second: smooth, and light on the CPU
function liveFrame(now) {
  LV.raf = requestAnimationFrame(liveFrame);
  if (now - LV.drawn < LIVE_FRAME_MS - 2) return;
  LV.drawn = now;
  drawMeter();
  drawWave();
  drawSpec(now);
  if (LV.rec && now - LV.lastStatus > 250) {
    LV.lastStatus = now;
    $("live-time").textContent = fmtTime(LV.rec.frames / LV.rate);
  }
}

function dbfs(v) { return v > 0 ? 20 * Math.log10(v) : -Infinity; }

function drawMeter() {
  const m = LV.meter, db = dbfs(m.peak), held = dbfs(m.held);
  const pos = (d) => `${Math.max(0, Math.min(100, (d + 60) / 60 * 100))}%`;
  $("live-meter-fill").style.width = pos(db);
  $("live-meter-peak").style.left = pos(held);
  $("live-meter").classList.toggle("clip", m.held >= 0.99);
  $("live-level").textContent = isFinite(db) ? `${Math.round(db)} dB` : "−∞ dB";
}

function canvasSize(c) {
  const w = Math.max(1, Math.floor(c.clientWidth * (window.devicePixelRatio || 1)));
  const h = Math.max(1, Math.floor(c.clientHeight * (window.devicePixelRatio || 1)));
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; return true; }
  return false;
}

// Scroll a canvas n pixels to the left; the n columns on the right are left empty. "copy" replaces
// what was there: drawn over itself (source-over), a transparent canvas would keep every old stroke.
function shiftLeft(g, c, n) {
  g.globalCompositeOperation = "copy";
  g.drawImage(c, -n, 0);
  g.globalCompositeOperation = "source-over";
}

function drawWave() {
  const c = $("live-wave"), cols = LV.wave.cols;
  if (!c.getContext) return;
  const g = c.getContext("2d");
  if (canvasSize(c)) g.clearRect(0, 0, c.width, c.height);
  if (!cols.length) return;
  const n = Math.min(cols.length, c.width), mid = c.height / 2;
  shiftLeft(g, c, n);
  if (!LV.color) LV.color = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#2f6f5e";
  g.fillStyle = LV.color;
  const from = cols.length - n;
  for (let i = 0; i < n; i++) {
    const [lo, hi] = cols[from + i];
    const y0 = mid - hi * mid, y1 = mid - lo * mid;
    g.fillRect(c.width - n + i, y0, 1, Math.max(1, y1 - y0));
  }
  cols.length = 0;
}

// "inferno" (openevp/spectrogram.py's palette): 256 colours, quiet dark to loud yellow.
const INFERNO = [[0.0002189403691192265, 0.001651004631001012, -0.01948089843709184],
                 [0.1065134194856116, 0.5639564367884091, 3.932712388889277],
                 [11.60249308247187, -3.972853965665698, -15.9423941062914],
                 [-41.70399613139459, 17.43639888205313, 44.35414519872813],
                 [77.162935699427, -33.40235894210092, -81.80730925738993],
                 [-71.31942824499214, 32.62606426397723, 73.20951985803202],
                 [25.13112622477341, -12.24266895238567, -23.07032500287172]];
function infernoLut() {
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    for (let k = 0; k < 3; k++) {
      let v = 0;
      for (let p = 0; p < INFERNO.length; p++) v += INFERNO[p][k] * Math.pow(t, p);
      lut[i * 3 + k] = Math.round(Math.max(0, Math.min(1, v)) * 255);
    }
  }
  return lut;
}

function drawSpec(now) {
  const c = $("live-spec"), a = LV.analyser, sp = LV.spec;
  if (!a || !c.getContext) return;
  const g = c.getContext("2d");
  if (canvasSize(c) || !sp.img || sp.img.height !== c.height) {
    g.fillStyle = "#000"; g.fillRect(0, 0, c.width, c.height);
    sp.img = g.createImageData(1, c.height);
  }
  if (!sp.last || now - sp.last > 1000) { sp.last = now; return; }    // first frame, or back after a pause
  const due = Math.min(c.width, Math.floor((now - sp.last) * LIVE_COLS_PER_SEC / 1000));
  if (due < 1) return;
  sp.last += due * 1000 / LIVE_COLS_PER_SEC;
  if (!sp.lut) sp.lut = infernoLut();
  a.getByteFrequencyData(sp.bins);
  const top = Math.min(LIVE_SPEC_HZ, LV.rate / 2), binHz = LV.rate / a.fftSize, h = c.height;
  const px = sp.img.data;
  for (let y = 0; y < h; y++) {
    const hz = (h - 1 - y) / (h - 1) * top;
    const v = sp.bins[Math.min(sp.bins.length - 1, Math.round(hz / binHz))];
    px[y * 4] = sp.lut[v * 3]; px[y * 4 + 1] = sp.lut[v * 3 + 1]; px[y * 4 + 2] = sp.lut[v * 3 + 2]; px[y * 4 + 3] = 255;
  }
  shiftLeft(g, c, due);
  for (let i = due; i >= 1; i--) g.putImageData(sp.img, c.width - i, 0);
}

// ---- crash recovery: files a crash left unfinished are finished at startup ----
async function liveRecover() {
  const r = await api().live_recover();
  if (!r.ok || (!r.recovered.length && !r.failed.length)) return r;
  const parts = [];
  if (r.recovered.length) {
    const names = r.recovered.map((f) => `${f.name} (in ${f.folder})`).join(", ");
    parts.push(r.recovered.length === 1 ? `A recording was cut off last time; OpenEVP saved what it had: ${names}.`
                                        : `${r.recovered.length} recordings were cut off last time; OpenEVP saved what it had: ${names}.`);
  }
  if (r.failed.length) parts.push(`Could not finish: ${r.failed.join(" · ")}`);
  banner(parts.join(" "), r.failed.length ? "warn" : "ok");
  loadLibrary();
  return r;
}

// ---- wiring ----
function setupLive() {
  $("live-entry").onclick = openLive;
  $("open-live").onclick = openLive;
  $("live-close").onclick = () => { if (!liveRecording()) showLibrary(); };
  $("live-mode-live").onclick = () => setLiveMode("live");
  $("live-mode-import").onclick = () => setLiveMode("import");
  $("live-input").onchange = async () => {
    const d = LV.devices.find((x) => x.deviceId === $("live-input").value);
    if (!d || liveRecording()) return;
    const want = { id: d.deviceId, label: d.label };
    if (await openInput(want)) saveLive({ input: { id: LV.deviceId, label: LV.label } });
  };
  $("live-split").onchange = splitChanged;
  $("live-gap").onchange = splitChanged;
  $("live-monitor").onchange = () => { applyMonitor(); renderLive(); };
  $("live-enhance").onchange = applyMonitor;
  $("live-record").onclick = () => (liveRecording() ? stopRecording() : startRecording());
  $("live-mark").onclick = liveMark;
  document.addEventListener("keydown", liveKeys);
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", inputsChanged);
  }
}

// M while the Live view is open: a mark (when recording).
function liveKeys(e) {
  if (!liveOpen() || (e.key !== "m" && e.key !== "M")) return;
  if (e.ctrlKey || e.altKey || e.metaKey || typingIn(e.target)) return;
  if (document.querySelector(".modal:not([hidden])")) return;
  e.preventDefault();
  liveMark();
}

// An input plugged in or out: the list follows (the input in use, if it went, ends its track).
async function inputsChanged() {
  if (!liveOpen()) return;
  await listInputs().catch(() => {});
  showInputs();
}

// The release check's frozen-app test (OpenEVP.exe --smoke, with Chromium's fake audio input):
// the default input recorded for a moment through the same code as the Record button, one mark,
// saved into the library folder, without opening the player.
async function liveSmoke(seconds) {
  S.view = "live";
  LV.settings = LV.settings || { input: null, split: 3, import: false, max_split: 60 };
  setLiveMode("live", false);
  liveFolders();
  if (!await openInput(null)) return { ok: false, error: $("live-status").textContent || "the input did not open" };
  const started = await startRecording();
  if (!started || !started.ok) { closeInput(); return { ok: false, error: (started && started.error) || "not started" }; }
  await new Promise((res) => setTimeout(res, seconds * 1000));
  const mark = await liveMark();
  const r = await stopRecording({ open: false });
  const out = { ok: !!(r && r.ok), rate: LV.rate, channels: LV.channels, label: LV.label,
                mark: !!(mark && mark.ok), files: (r && r.files) || [], error: r && r.error };
  closeInput();
  S.view = "library";
  return out;
}
