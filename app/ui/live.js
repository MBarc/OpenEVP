"use strict";
// ---- Live mode and analog import: record from one of the PC's audio inputs (app/live.py) ----
// The input is opened with getUserMedia, with echo cancellation, noise suppression and automatic
// gain off, so the file holds the input exactly as Windows delivers it. An AudioWorklet
// (live-worklet.js) turns it into 16-bit PCM; while recording, the page sends it to the backend
// in numbered chunks of about half a second, one call at a time (live_chunk), and the backend
// writes the WAV as it goes. An AnalyserNode feeds the scrolling spectrogram; the waveform and
// the level meter come from the worklet's batches. Both draw on animation frames only, scrolling
// what is already drawn, so an hour costs no more than a minute.
// Listening (monitoring) is off whenever the view opens; while it is on, its panel's options (the
// player's Enhance settings, shared) apply to what is heard only. The saved file is always the raw input.
const LV = {
  settings: null,                        // live_settings(): input, split, import
  mode: "live",                          // "live" or "import"
  stream: null, ctx: null, src: null, node: null, analyser: null, monitor: [], opening: 0, gen: 0,
  heardAnalyser: null, enhanced: false,  // the spectrogram of what is heard, while listening enhanced
  rate: 0, channels: 0, deviceId: "", label: "", devices: [],
  rec: null,                             // the recording (see startRecording)
  flushId: 0, flushWaiters: new Map(),   // worklet flushes waiting for their answer
  meter: { peak: 0, rms: 0, held: 0, heldAt: 0 },
  wave: { cols: [], min: 32768, max: -32769, n: 0, perCol: 0 },
  spec: { bins: null, last: 0, lut: null, img: null },
  raf: 0, lastStatus: 0, drawn: 0, color: "",
  hist: null, redrawTimer: 0, sizeWatch: null, ratioWatch: null,   // drawn columns kept for redrawing (historyReset)
  quietFrames: 0,                        // frames in a row under LIVE_SILENT_DB (the "no sound coming in" warning)
  markedTimer: 0,                        // the "Marked" flash
  cuts: null,                            // an import's cuts: {job, fp, cuts, suggested, looking, splitJob}
  cutRegions: [],                        // the cuts drawn on the waveform
  jobs: new Map(), jobEvents: new Map(), // import jobs the page knows (job -> handler); events of jobs it does not yet
};
const LIVE_COLS_PER_SEC = 40;            // waveform and spectrogram: the same time scale, 25 ms a column
const LIVE_CHUNK_SEC = 0.5;              // audio sent to the backend per call
const CUT_MIN = 0.5;                     // seconds: a cut needs this much on each side (app/live.py MIN_PIECE)
const LIVE_SILENT_DB = -80;              // under this for LIVE_SILENT_SEC: "No sound coming in"
const LIVE_SILENT_SEC = 3;
const LIVE_METER_SILENT_DB = -90;        // the meter says "Silent" under this, not a jittering number
const LIVE_MARKED_MS = 1200;             // how long "Marked" shows after a mark
const LIVE_MAX_QUEUE_SEC = 10;           // audio captured but not yet taken by the backend, at most
const LIVE_CALL_MS = 15000;              // a bridge call slower than this: the backend is not answering
const LIVE_FLUSH_MS = 2000;              // the worklet's answer to a flush, at most
const LIVE_MARK_DRAIN_MS = 60000;        // Stop gives the marks still queued this long in all
const LIVE_DRAIN_MS = 30000;             // Stop waits at most this long for the queued audio to be taken
const LIVE_STOP_RESERVE_MS = 8000;       // of a close's budget, kept for the backend's finish at the end
const QUEUE_FULL = "Recording stopped: OpenEVP could not save the audio as fast as it came in, so it stopped " +
                   "rather than hold more and more of it in memory. What was saved until then is kept.";
const NOT_ANSWERING = "OpenEVP stopped answering while saving the recording. What was saved until then is kept.";
const STOP_NOT_ANSWERING = "OpenEVP is not answering. What was recorded until then is on disk and is finished " +
                           "when OpenEVP closes, or when it starts again.";
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
  $("live-field").checked = !!LV.settings.field;
  applyField();
  showListen();
  if (!S.lib.listed) await loadLibrary();
  liveFolders();
  renderLive();
  if (!liveOpen()) return;                          // left while the settings or the library loaded
  await openInput(LV.settings.input);
}

// Called by renderMain whenever another view is shown: the input is let go of.
function liveViewLeft() {
  document.body.classList.remove("live-night");
  closeInput();                                     // always: an input still opening is called off too
}

// The rate Enhance works at while the Live view is open (app.js enhRate), else 0.
function liveRate() { return liveOpen() && LV.rate ? LV.rate : 0; }

// The Enhance settings changed (app.js setEnhance, here or in the player): what is heard follows at once.
function liveEnhanceChanged() { if (LV.ctx) applyMonitor(); showListen(); }

// ---- the Listen panel: what is heard, shown only while Listen is on ----
// Its options are the player's Enhance settings (one setting, shared), under plainer names:
// Volume is Boost; "Even out loud and quiet" is the Leveler (Off, or its strength); Clean up is the
// voice filter, Cut rumble and Cut hiss; Hum is the hum remover. The player's tab keeps its own layout.
function showListen() {
  const on = $("live-monitor").checked, st = S.enh.settings;
  $("live-listen").hidden = !on;
  if (!st) return;
  $("live-boost").value = String(st.boost);
  $("live-boost-value").textContent = st.boost ? `+${st.boost} dB` : "0 dB";
  const even = st.leveler ? st.strength : "off";
  for (const b of $("live-even").children) b.setAttribute("aria-pressed", String(b.dataset.v === even));
  for (const b of $("live-hum").children) b.setAttribute("aria-pressed", String(b.dataset.v === st.hum));
  for (const [id, key] of [["live-voice", "voice"], ["live-rumble", "rumble"], ["live-hiss", "hiss"]]) {
    $(id).setAttribute("aria-pressed", String(!!st[key]));
  }
  const hiss = hissAvailable(enhRate());
  $("live-hiss").disabled = !hiss;
  $("live-hiss").title = hiss ? "Lower everything above 5 kHz (hiss)"
    : "This input has nothing above 4 kHz, so there is no hiss band to cut.";
}

// The segmented choices: [value, label]. "Even out" Off is the Leveler off; the others, its strength.
const LIVE_EVEN = [["off", "Off"], ["light", "Light"], ["medium", "Medium"], ["strong", "Strong"]];
const LIVE_HUM = [["off", "Off"], ["60", "60 Hz"], ["50", "50 Hz"]];

function segButtons(box, choices, pick) {
  box.textContent = "";
  for (const [v, label] of choices) {
    const b = document.createElement("button");
    b.type = "button"; b.dataset.v = v; b.textContent = label;
    b.onclick = () => pick(v);
    box.appendChild(b);
  }
}

function setupListen() {
  $("live-boost").oninput = () => setEnhance({ boost: Number($("live-boost").value) });
  segButtons($("live-even"), LIVE_EVEN, (v) => setEnhance(v === "off" ? { leveler: false } : { leveler: true, strength: v }));
  segButtons($("live-hum"), LIVE_HUM, (v) => setEnhance({ hum: v }));
  for (const [id, key] of [["live-voice", "voice"], ["live-rumble", "rumble"], ["live-hiss", "hiss"]]) {
    $(id).onclick = () => setEnhance({ [key]: !(S.enh.settings && S.enh.settings[key]) });
  }
  $("live-enh-reset").onclick = () => setEnhance({ ...ENH_DEFAULT });
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
// The dark red night screen (remembered): the Live view only.
function applyField() {
  $("live").classList.toggle("field", $("live-field").checked);
  document.body.classList.toggle("live-night", $("live-field").checked && liveOpen());   // the window around it too
  LV.color = "";                                    // the waveform's colour follows
  LV.emptyColor = "";
  liveRedraw();
}

// "about 9 h left": recording time left before the drive is nearly full (or the file at 4 GB).
function fmtLeft(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) return "";
  if (seconds >= 3600) return `about ${Math.floor(seconds / 3600)} h left`;
  if (seconds >= 60) return `about ${Math.floor(seconds / 60)} min left`;
  return "under a minute left";
}

function renderLive() {
  const rec = liveRecording(), stopping = rec && LV.rec.closing;
  const ready = !!LV.node && !LV.opening;
  $("live-record").textContent = rec ? (stopping ? "Saving…" : "■ Stop") : "● Record";
  $("live-record").disabled = stopping || (!rec && !ready);
  $("live-record").classList.toggle("recording", rec);
  $("live-mark").disabled = !rec || stopping;
  for (const id of ["live-input", "live-folder", "live-mode-live", "live-mode-import", "live-split", "live-close"]) $(id).disabled = rec;
  $("live-gap").disabled = rec || !$("live-split").checked;
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
// Every open has its own generation (LV.gen only grows): leaving the view, closing the input or
// opening another calls it off, and after each step that waits it checks it is still wanted (its
// generation, and the Live view still shown). A stream that arrives for an open called off is let go.
async function openInput(want) {
  const gen = ++LV.gen;
  LV.opening = gen;
  closeInput(true);
  renderLive();
  const stale = () => gen !== LV.gen || !liveOpen();
  const release = (stream) => { for (const t of stream.getTracks()) t.stop(); return false; };
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    liveStatus("Recording is not available in this window.", "warn"); LV.opening = 0; renderLive(); return false;
  }
  liveStatus("Opening the input…");
  try {
    let stream = await navigator.mediaDevices.getUserMedia(audioConstraints(want && want.id, false));
    if (stale()) return release(stream);
    await listInputs();
    if (stale()) return release(stream);
    const track = stream.getAudioTracks()[0];
    const have = track.getSettings().deviceId || "";
    const target = want && (LV.devices.find((d) => d.deviceId === want.id) || LV.devices.find((d) => d.label === want.label));
    if (target && target.deviceId !== have) {
      release(stream);
      stream = await navigator.mediaDevices.getUserMedia(audioConstraints(target.deviceId, true));
      if (stale()) return release(stream);
    }
    if (!await startGraph(stream, stale)) return false;
    LV.opening = 0;
    liveStatus(want && target === undefined && want.label
      ? `"${want.label}" is not connected, so ${LV.label || "the default input"} is used.` : "");
    showInputs();
    renderLive();
    return true;
  } catch (e) {
    if (stale()) return false;
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

// Build the capture graph for a stream: true once it is LV's input; false when the open was called
// off meanwhile (stale()), and then nothing of it is left running.
async function startGraph(stream, stale = () => false) {
  const track = stream.getAudioTracks()[0];
  const st = track.getSettings();
  const AC = window.AudioContext || window.webkitAudioContext;
  let ctx;
  try { ctx = new AC({ sampleRate: st.sampleRate || undefined, latencyHint: "playback" }); }
  catch (e) { ctx = new AC({ latencyHint: "playback" }); }     // a rate Web Audio refuses: its own (48 kHz)
  let mine = false;                                     // LV holds this graph
  try {
    await ctx.audioWorklet.addModule("live-worklet.js");
    if (stale()) throw STALE_OPEN;
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
    mine = true;
    LV.wave.perCol = Math.max(1, Math.round(ctx.sampleRate / LIVE_COLS_PER_SEC));
    historyReset();                                     // another input: its own history
    LV.quietFrames = 0;
    $("live-silent").hidden = true;
    $("live-input-info").textContent = `${track.label || "Input"} · ${+(ctx.sampleRate / 1000).toFixed(1)} kHz ` +
                                       `${channels === 2 ? "stereo" : "mono"}`;
    LV.spec.bins = new Uint8Array(analyser.frequencyBinCount);
    track.onended = inputEnded;
    if (ctx.resume) await ctx.resume();
    if (stale()) throw STALE_OPEN;
    applyMonitor();
    if (!LV.raf) LV.raf = requestAnimationFrame(liveFrame);
    return true;
  } catch (e) {
    if (mine && LV.ctx === ctx) closeInput(true);     // LV's input until now: closed as a whole
    try { ctx.close(); } catch (x) { /* already closed */ }
    stream.getTracks().forEach((t) => t.stop());
    if (e === STALE_OPEN) return false;
    throw e;
  }
}
const STALE_OPEN = new Error("the input is no longer wanted");

function closeInput(keepSeq = false) {
  if (!keepSeq) { LV.gen++; LV.opening = 0; }           // an open still under way is called off
  if (LV.node) { LV.node.port.onmessage = null; try { LV.node.disconnect(); } catch (e) { /* gone */ } }
  if (LV.stream) for (const t of LV.stream.getTracks()) { t.onended = null; t.stop(); }
  if (LV.ctx) { try { LV.ctx.close(); } catch (e) { /* gone */ } }
  Object.assign(LV, { stream: null, ctx: null, src: null, node: null, analyser: null, monitor: [], heardAnalyser: null, enhanced: false });
  LV.meter.peak = LV.meter.rms = LV.meter.held = 0;
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
// What is heard: with Listen on, the input through the Enhance chain (the Listen panel's options, the
// player's settings) to the speakers. The spectrogram always shows what is heard: while listening
// with any option set it reads a second analyser at the end of the chain; otherwise the input itself.
// The capture never goes through the chain: the worklet and the raw analyser take the source
// directly, so the saved file and the waveform stay the input as it came in. With Listen off, no
// chain exists at all (no CPU spent on it).
function applyMonitor() {
  const ctx = LV.ctx;
  if (!ctx) return;
  for (const n of LV.monitor) { try { n.disconnect(); } catch (e) { /* gone */ } }
  LV.monitor = [];
  try { LV.src.disconnect(); } catch (e) { /* not connected */ }
  LV.src.connect(LV.node); LV.src.connect(LV.analyser);            // the capture: always raw
  const listen = $("live-monitor").checked;
  const stages = listen && S.enh.settings ? enhanceGraph(S.enh.settings, ctx.sampleRate) : [];
  LV.enhanced = stages.length > 0;                    // listening, and an option changes what is heard
  if (!listen) return;
  LV.monitor = stages.flatMap((st) => makeNodes(ctx, st));
  let at = LV.src;
  for (const n of LV.monitor) { at.connect(n); at = n; }
  at.connect(ctx.destination);
  if (LV.enhanced) {
    if (!LV.heardAnalyser) {
      LV.heardAnalyser = ctx.createAnalyser();
      LV.heardAnalyser.fftSize = 2048; LV.heardAnalyser.smoothingTimeConstant = 0;
      LV.heardAnalyser.minDecibels = LIVE_DB_FLOOR; LV.heardAnalyser.maxDecibels = LIVE_DB_TOP;
    }
    at.connect(LV.heardAnalyser);
  }
}

// The analyser the spectrogram reads: what is heard. Enhanced while listening with an option set, else the input.
function specAnalyser() { return LV.enhanced && LV.heardAnalyser ? LV.heardAnalyser : LV.analyser; }

// ---- audio from the worklet: meter, waveform, and the chunks a recording sends ----
function liveAudio(m) {
  if (m.flushed !== undefined) {                       // the worklet's answer to a flush: all it held came first
    const done = LV.flushWaiters.get(m.flushed);
    LV.flushWaiters.delete(m.flushed);
    if (done) done();
    return;
  }
  const pcm = new Int16Array(m.pcm), ch = LV.channels, n = m.frames;
  const meter = LV.meter;
  checkSilence(n, m.sumsq, ch);
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
    if (rec.pendingFrames >= LV.rate * LIVE_CHUNK_SEC) queueChunk(rec);
    // Audio the backend has not taken yet, bounded: a bridge that stalls must not fill memory.
    if (!rec.overflow && rec.inflightFrames + rec.queuedFrames + rec.pendingFrames > LV.rate * LIVE_MAX_QUEUE_SEC) {
      rec.overflow = true;
      stopRecording({ reason: QUEUE_FULL, drain: false });
    }
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

// A backend call that gives up after ms: {ok: false, timeout: true} if the bridge does not answer.
// ms: a time limit, or a promise that resolves when the time is up (liveWait: it can shrink).
function callWithTimeout(call, ms) {
  return new Promise((resolve) => {
    let done = false;
    const late = () => { if (!done) { done = true; resolve({ ok: false, timeout: true, error: NOT_ANSWERING }); } };
    const timer = typeof ms === "number" ? setTimeout(late, ms) : (ms.then(late), null);
    Promise.resolve().then(call).then(
      (r) => { if (!done) { done = true; clearTimeout(timer); resolve(r || { ok: false, error: "no answer" }); } },
      (e) => { if (!done) { done = true; clearTimeout(timer); resolve({ ok: false, error: `${(e && e.message) || e}` }); } });
  });
}

function liveNow() { return Date.now(); }

// A wait for a recording: ms, or less once the window is closing. rec.deadline (the budget app/main.py
// gave the page) can arrive while the wait runs (a close during a Stop already under way): every wait
// re-arms then (setDeadline), so it ends by the deadline less `reserve` (LIVE_STOP_RESERVE_MS: kept
// for the finish).
function liveWait(rec, ms, reserve = LIVE_STOP_RESERVE_MS) {
  const end = liveNow() + ms;
  return new Promise((resolve) => {
    let timer = null;
    const done = () => { rec.waiters.delete(arm); resolve(); };
    const arm = () => {
      clearTimeout(timer);
      const until = rec.deadline ? Math.min(end, rec.deadline - reserve) : end;
      const left = until - liveNow();
      if (left <= 0) done(); else timer = setTimeout(done, left);
    };
    rec.waiters.add(arm);
    arm();
  });
}

// The window is closing: from now on every wait of this recording ends in time (an earlier deadline wins).
function setDeadline(rec, deadline) {
  if (!deadline || (rec.deadline && rec.deadline <= deadline)) return;
  rec.deadline = deadline;
  for (const arm of [...rec.waiters]) arm();
}

// Time left before the deadline less the reserve (Infinity when not closing).
function timeLeft(rec, reserve = LIVE_STOP_RESERVE_MS) {
  return rec.deadline ? rec.deadline - reserve - liveNow() : Infinity;
}

// Resolves after ms (or never sooner): a bound for waiting on something else.
function liveDelay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

// The audio held so far becomes the next numbered chunk; one sender sends them in order, one at a time.
// Once sending has ended unsuccessfully, what would be queued is counted as missing instead.
function queueChunk(rec) {
  if (rec.dead || rec.abandoned || rec.ended) {
    lose(rec, rec.pendingFrames, rec.dead ? "OpenEVP stopped answering" : "");
    rec.pending = []; rec.pendingFrames = 0;
    return rec.sending || Promise.resolve();
  }
  if (rec.pending.length) {
    rec.queue.push({ seq: rec.seq++, data: toBase64(rec.pending), frames: rec.pendingFrames });
    rec.queuedFrames += rec.pendingFrames;
    rec.pending = []; rec.pendingFrames = 0;
  }
  if (!rec.sending && rec.queue.length && !rec.abandoned) rec.sending = pump(rec);
  return rec.sending || Promise.resolve();
}

async function pump(rec) {
  try {
    while (rec.queue.length && !rec.ended && !rec.dead && !rec.abandoned) {
      const c = rec.queue.shift();                      // in flight now: no longer counted as queued
      rec.queuedFrames -= c.frames;
      rec.inflightFrames = c.frames;
      const r = await callWithTimeout(() => api().live_chunk(rec.session, c.seq, c.data), LIVE_CALL_MS);
      const unsure = rec.inflightFrames;                // 0 if Stop already counted it as missing
      rec.inflightFrames = 0;
      if (r.timeout) {                                  // it may or may not have arrived: counted as missing
        rec.dead = true; rec.failed = NOT_ANSWERING;
        lose(rec, unsure, "OpenEVP stopped answering");
        loseBacklog(rec, "OpenEVP stopped answering");
        break;
      }
      if (!r.ok) { rec.failed = r.error; lose(rec, unsure, r.error); loseBacklog(rec, r.error); break; }
      rec.status = r;
      if (r.stopped) { rec.ended = true; rec.stoppedBy = r; loseBacklog(rec, "the recording had stopped"); break; }
      afterChunk(rec);
    }
  } finally {
    rec.sending = null;
  }
  afterChunk(rec);
}

// Until every chunk queued so far has been answered (or ms passed): true if it was.
async function drained(rec, ms) {
  const late = liveWait(rec, ms).then(() => "late");
  while (rec.sending) {
    if (await Promise.race([rec.sending.then(() => "sent"), late]) === "late") return false;
  }
  return !rec.queue.length || rec.abandoned || rec.dead || rec.ended;
}

// The backend's answer to a chunk: the status line, or the end the backend chose.
function afterChunk(rec) {
  if (LV.rec !== rec) return;
  if (rec.failed && !rec.closing) {
    stopRecording({ reason: rec.dead ? `Recording stopped: ${NOT_ANSWERING}` : `Recording stopped: ${rec.failed}`, drain: false });
    return;
  }
  if (rec.stoppedBy && !rec.closing) { recordingDone(rec, rec.stoppedBy.result, rec.stoppedBy.stopped); return; }
  const r = rec.status;
  if (!r) return;
  liveStatus(r.warning || "", r.warning ? "warn" : "");
  if (typeof r.left_seconds === "number") $("live-left").textContent = fmtLeft(r.left_seconds);
}

// Ask the worklet for the samples it holds that do not fill a batch yet; resolves once it has
// sent them (its answer comes after them on the same port), or after ms.
function flushWorklet(ms, rec = null) {
  const node = LV.node;
  if (!node || !node.port || !node.port.postMessage) return Promise.resolve(false);
  const id = ++LV.flushId;
  return new Promise((resolve) => {
    let done = false;
    const giveUp = () => { if (!done) { done = true; LV.flushWaiters.delete(id); resolve(false); } };
    const timer = rec ? (liveWait(rec, ms).then(giveUp), null) : setTimeout(giveUp, ms);
    LV.flushWaiters.set(id, () => { if (!done) { done = true; clearTimeout(timer); resolve(true); } });
    node.port.postMessage({ flush: id });
  });
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
  $("live-left").textContent = fmtLeft(r.left_seconds);
  LV.rec = { session: r.session, seq: 0, pending: [], pendingFrames: 0, frames: 0, queue: [], queuedFrames: 0,
             sending: null, closing: false, stopping: false, ended: false, dead: false, abandoned: false,
             overflow: false, marks: [], folder, status: null, failed: null, stoppedBy: null,
             markChain: Promise.resolve(), marksCancelled: false, unsaved: [], marksPending: [],
             markInflight: null, deadline: null, waiters: new Set(), file: r.file,
             stopPromise: null, lostFrames: 0, losses: [], tailUnknown: false, inflightFrames: 0 };
  LV.wave.cols = [];
  $("live-file").textContent = `Recording ${r.file}`;
  renderLive();
  return r;
}

// M (or the MARK button): the last 3 seconds, ending now, as a mark (app/live.py: class C, "not graded
// yet"; graded and adjusted later in the EVP Library). Shown by a star on the waveform and "Marked".
async function liveMark() {
  const rec = LV.rec;
  if (!rec || rec.closing || rec.ended) return null;
  const at = rec.frames / LV.rate;
  queueChunk(rec);                                     // the audio up to now, before the mark
  await drained(rec, LIVE_CALL_MS);
  if (LV.rec !== rec || rec.closing) return null;
  const r = await markOp(rec, (ms) => callWithTimeout(() => api().live_mark(rec.session, at), ms),
                         `the mark at ${fmtTime(at)}`);
  if (!r.ok) { liveStatus(r.error, "warn"); return r; }
  rec.marks.push(r.mark);
  addMarker("★");
  flashMarked();
  return r;
}

function flashMarked() {
  const el = $("live-marked");
  el.hidden = false;
  clearTimeout(LV.markedTimer);
  LV.markedTimer = setTimeout(() => { el.hidden = true; }, LIVE_MARKED_MS);
}

// The recording's marks reach the backend one at a time (a promise chain per recording), each call
// with its own time limit, and Stop waits for all of them before the file is finished (drainMarks).
// A mark that did not get through is listed (rec.unsaved: `what`) and said when the recording is saved.
function markOp(rec, fn, what) {
  rec.marksPending.push(what);
  const step = async () => {
    rec.marksPending.splice(rec.marksPending.indexOf(what), 1);
    if (rec.marksCancelled || timeLeft(rec) <= 0) {     // Stop gave up waiting: never sent after the finish
      rec.unsaved.push(what);
      return { ok: false, cancelled: true, error: "The recording was saved before this mark got through." };
    }
    rec.markInflight = what;
    const r = await fn(liveWait(rec, LIVE_CALL_MS));    // its own limit, shrunk if the window closes meanwhile
    rec.markInflight = null;
    if (!r || !r.ok) {
      rec.unsaved.push(what);
      if (r && r.timeout && rec.closing) rec.marksCancelled = true;  // not answering: the rest is not sent
    }
    return r;
  };
  const run = rec.markChain.then(step, step);
  rec.markChain = run.catch(() => {});
  return run;
}

// Stop: every queued mark first. Should that take too long, or a call go unanswered, what is still
// queued is called off (not sent after the finish) and the one under way ends within its own time
// limit; recordingDone says which did not get through.
async function drainMarks(rec) {
  const all = await Promise.race([rec.markChain.then(() => true), liveWait(rec, LIVE_MARK_DRAIN_MS).then(() => false)]);
  if (all) return;
  rec.marksCancelled = true;
  await rec.markChain;
}

// A labelled marker on the live waveform at the newest column (kept in the history, so it is drawn
// again after a resize).
function addMarker(label) {
  const h = LV.hist;
  const col = h.waveN + LV.wave.cols.length, prev = h.markers[h.markers.length - 1];
  // A label close after another goes a row lower, so marks made close together stay readable.
  const row = prev && col - prev.col < 48 * (window.devicePixelRatio || 1) ? (prev.row + 1) % 3 : 0;
  h.markers.push({ col, label, row });
  if (h.markers.length > 500) h.markers.shift();
}

// Stop and save. With drain (the default): the worklet's last samples (an acknowledged flush), then
// every chunk queued, then the backend finishes the file; each step bounded in time. Without it (the
// queue overflowed or the bridge stopped answering): what is queued is dropped and the backend
// finishes what it has. If the backend does not answer at all, its file is finished when OpenEVP
// closes (or, after a crash, when it starts again).
// Audio that may be missing from the end of the saved file: counted, with why, for the final message.
function lose(rec, frames, why) {
  if (!frames) return;
  rec.lostFrames += frames;
  if (why && !rec.losses.includes(why)) rec.losses.push(why);
}

// The chunk in flight when Stop gives up on it: counted as missing (once: the sender then sees 0).
function loseInflight(rec, why) {
  lose(rec, rec.inflightFrames, why);
  rec.inflightFrames = 0;
}

// Sending ended unsuccessfully: every chunk still queued is missing too.
function loseBacklog(rec, why) {
  lose(rec, rec.queuedFrames, why);
  rec.queue = []; rec.queuedFrames = 0;
}

// One Stop per recording: asked again (Stop clicked twice, the window closing during "Saving…"),
// it is the same Stop, and whoever asks waits for it to finish.
function stopRecording(opts = {}) {
  const rec = LV.rec;
  if (!rec) return Promise.resolve(null);
  if (opts.deadline) setDeadline(rec, opts.deadline);   // a close during a Stop under way: its waits shrink too
  if (!rec.stopPromise) rec.stopPromise = runStop(rec, opts);
  return rec.stopPromise;
}

async function runStop(rec, opts) {
  rec.closing = true;
  renderLive();
  liveStatus("Saving…");
  const drain = opts.drain !== false && !rec.dead;
  if (drain && !rec.ended && !await flushWorklet(LIVE_FLUSH_MS, rec)) {  // batches still arrive until its answer
    rec.tailUnknown = true;                                       // the worklet's last samples never came
  }
  rec.stopping = true;                                            // from here on nothing more is taken
  if (drain && !rec.ended) {
    queueChunk(rec);
    if (!await drained(rec, LIVE_DRAIN_MS)) {
      rec.abandoned = true;                             // the chunk in flight, the queue and the rest: missing
      const why = "saving the last of it took too long";
      loseInflight(rec, why);
      lose(rec, rec.queuedFrames + rec.pendingFrames, why);
      rec.queue = []; rec.queuedFrames = 0; rec.pending = []; rec.pendingFrames = 0;
    }
  } else {
    rec.abandoned = true;
    lose(rec, rec.queuedFrames + rec.pendingFrames, rec.overflow ? "it could not be saved fast enough" : "");
    rec.queue = []; rec.queuedFrames = 0; rec.pending = []; rec.pendingFrames = 0;
    if (rec.sending) await Promise.race([rec.sending, liveWait(rec, LIVE_CALL_MS)]);
    loseInflight(rec, rec.overflow ? "it could not be saved fast enough" : "OpenEVP stopped answering");
  }
  let r;
  if (rec.stoppedBy) r = rec.stoppedBy.result;
  else {
    // Marks made before Stop reach the backend first, all of them.
    await drainMarks(rec);
    // The finish: its own limit, or what is left of a close's budget (the reserve kept for it).
    r = await callWithTimeout(() => api().live_stop(rec.session), liveWait(rec, LIVE_CALL_MS, 0));
    if (r.timeout) r = { ok: false, error: STOP_NOT_ANSWERING };
  }
  return recordingDone(rec, r, opts.reason || (rec.stoppedBy && rec.stoppedBy.stopped) || "", opts);
}

// The window is closing during a recording (app/main.py waits for this, bounded): stop and save
// as Stop does, without opening the player.
// budgetMs: the time app/main.py gives the page for all of it; the window's own finish follows.
async function liveDrainForClose(budgetMs) {
  window.__liveDrained = false;
  LV.closingRec = LV.rec;
  const deadline = budgetMs > 0 ? liveNow() + budgetMs : null;
  try { if (LV.rec) await stopRecording({ open: false, reason: "OpenEVP is closing.", deadline }); }
  finally { window.__liveDrained = true; }
  return true;
}

// What of the recording being saved as the window closes may not have been saved: changes called
// off or unanswered, the one under way, and any still waiting. app/main.py reads this (whether or not
// the page finished in time) and keeps it for the next start.
function liveUnsavedNow() {
  const rec = LV.closingRec;
  if (!rec) return null;
  const items = [...rec.unsaved, ...(rec.markInflight ? [rec.markInflight] : []), ...rec.marksPending];
  return items.length ? { file: rec.file || "", items } : null;
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
  const missing = rec.lostFrames / LV.rate + (rec.tailUnknown ? 2048 / LV.rate : 0);
  const truncated = missing > 0 ? { seconds: Math.round(missing * 10) / 10, reasons: rec.losses.slice(),
                                    tail: !!rec.tailUnknown } : null;
  const missingText = !truncated ? "" : rec.lostFrames
    ? `the last ${Math.max(0.1, Math.ceil(missing * 10) / 10)} seconds may be missing` : "the last moment (under a tenth of a second) may be missing";
  const why = truncated ? [...rec.losses, ...(rec.tailUnknown ? ["the capture did not hand over its last samples"] : [])]
    .filter(Boolean).join("; ") : "";
  if (!r || !r.ok) {
    banner(`The recording could not be saved: ${(r && r.error) || "unknown error"}` +
           (truncated ? ` Also, ${missingText} (${why}).` : ""));
    return r && { ...r, truncated };
  }
  r = { ...r, truncated };
  const files = r.files || [];
  const notes = [...(r.problems || [])];
  if (rec.unsaved.length) notes.push(`These marks may not have been saved: ${rec.unsaved.join("; ")}. ` +
                                     "Check them in the player.");
  if (r.dropped_marks) notes.push(`${plural(r.dropped_marks, "mark")} fell outside the saved audio and were left out.`);
  if (r.player_error) notes.push(`It could not be opened in the player (${r.player_error.replace(/[.\s]+$/, "")}); find it in the EVP Library.`);
  let msg;
  if (!files.length) msg = LV.mode === "import" ? "Nothing was saved: no sound arrived." : "Nothing was saved.";
  else if (files.length === 1) msg = `Saved ${files[0].name} in ${r.folder}`;
  else msg = `Saved ${plural(files.length, "recording")} in ${r.folder}`;
  if (files.length) msg = truncated ? `${msg}, but ${missingText} (${why}).` : `✓ ${msg}.`;
  if (r.suggest) {                         // an import: cuts are suggested, then the user decides
    LV.cuts = { job: r.suggest.job, fp: r.suggest.fp, cuts: [], suggested: null, looking: true, splitJob: null };
    registerJob(r.suggest.job, suggestEvent);
  }
  const text = [reason, msg, ...notes].filter(Boolean).join(" ");
  banner(text, reason || notes.length || !files.length || truncated ? "warn" : "ok");
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
    banner(text, reason || notes.length || truncated ? "warn" : "ok");     // loading clears nothing, but say it last
    showCuts();                                                            // an import's cuts, once known
  }
  return r;
}

// ---- an import: suggested cuts on the waveform, confirmed by the user, then the split ----
// After an import's Stop the whole recording opens in the player; the backend looks for the gaps
// between recordings (app/live.py _suggest_job) and the page draws them as cuts on the waveform.
// The user removes cuts, adds their own (at the play cursor), and confirms ("Split into N
// recordings") or keeps the import as one. Nothing is split without that.
// Job events can come before the page knows the job (the id comes back with Stop's answer, or
// split_import's): they wait in LV.jobEvents until the job is registered.
function registerJob(job, handler) {
  LV.jobs.set(job, handler);
  const waiting = LV.jobEvents.get(job) || [];
  LV.jobEvents.delete(job);
  for (const [event, p] of waiting) handler(event, p);
}

function liveJobEvent(event, p) {
  const handler = LV.jobs.get(p.job);
  if (handler) { handler(event, p); return; }
  if (!LV.jobEvents.has(p.job)) {
    LV.jobEvents.set(p.job, []);
    while (LV.jobEvents.size > 20) LV.jobEvents.delete(LV.jobEvents.keys().next().value);
  }
  LV.jobEvents.get(p.job).push([event, p]);
}

function suggestEvent(event, p) {
  const C = LV.cuts;
  if (!C || C.job !== p.job) return;
  if (event === "import-suggest-progress") { status(`Looking for the gaps between recordings… ${p.percent}%`); return; }
  LV.jobs.delete(p.job);
  status("");
  C.looking = false;
  if (event === "import-suggest-done") {
    C.cuts = p.cuts.slice();
    C.suggested = p.cuts.length;
  } else {
    C.cuts = [];
    C.suggested = 0;
    banner(p.error, "warn");
  }
  showCuts();
}

function cutsShown() { return !!(LV.cuts && S.current && S.current.fp === LV.cuts.fp); }

// The cut bar under the waveform, and the cuts drawn on it.
function showCuts() {
  const C = LV.cuts, bar = $("cut-bar");
  clearCutRegions();
  if (!cutsShown() || C.looking) { bar.hidden = true; return; }
  bar.hidden = false;
  const n = C.cuts.length, busy = !!C.splitJob;
  $("cut-text").textContent = busy ? "Splitting…"
    : !n ? (C.suggested === 0 ? "No gaps between recordings found. Play to a spot between two recordings and add a cut there, or keep it as one."
                              : "No cuts. Add one at the play cursor, or keep it as one.")
         : `${plural(n, "cut")}. Remove any that are wrong (✕), add one at the play cursor, then split.`;
  const list = $("cut-list");
  list.textContent = "";
  C.cuts.forEach((t, i) => {
    const chip = document.createElement("span");
    chip.className = "cut-chip";
    const at = document.createElement("button");
    at.type = "button"; at.className = "link"; at.textContent = `✂ ${fmtPrecise(t)}`;
    at.title = "Play from here";
    at.onclick = () => { S.ws.setTime(Math.max(0, t - 1)); S.ws.play(); };
    const x = document.createElement("button");
    x.type = "button"; x.className = "cut-remove"; x.textContent = "✕"; x.title = "Remove this cut";
    x.disabled = busy;
    x.onclick = () => removeCut(i);
    chip.append(at, x);
    list.appendChild(chip);
    if (S.regions && S.regions.addRegion) {
      const region = S.regions.addRegion({ id: `cut-${i}`, start: t, color: "rgba(200, 50, 50, 0.9)", content: "✂",
                                           drag: false, resize: false });
      LV.cutRegions.push(region);
    }
  });
  $("cut-add").disabled = busy;
  $("cut-keep").disabled = busy;
  $("cut-split").disabled = busy || !n;
  $("cut-split").textContent = `Split into ${plural(n + 1, "recording")}`;
}

function clearCutRegions() {
  for (const r of LV.cutRegions) { try { r.remove(); } catch (e) { /* gone */ } }
  LV.cutRegions = [];
}

function removeCut(i) {
  if (!LV.cuts || LV.cuts.splitJob) return;
  LV.cuts.cuts.splice(i, 1);
  showCuts();
}

function addCut() {
  const C = LV.cuts;
  if (!C || C.splitJob || !S.current) return;
  const t = Math.round(S.ws.getCurrentTime() * 1000) / 1000, d = S.current.duration || 0;
  if (t < CUT_MIN || t > d - CUT_MIN || C.cuts.some((c) => Math.abs(c - t) < CUT_MIN)) {
    status(`A cut needs at least ${CUT_MIN} seconds on each side. Move the play cursor and try again.`);
    return;
  }
  status("");
  C.cuts.push(t);
  C.cuts.sort((a, b) => a - b);
  showCuts();
}

// A cut on the waveform was clicked (app.js regionClicked): it goes.
function cutClicked(region) {
  const i = LV.cutRegions.indexOf(region);
  if (i >= 0) removeCut(i);
}

async function splitImport() {
  const C = LV.cuts;
  if (!C || C.splitJob || !C.cuts.length || !S.current) return;
  const r = await api().split_import(S.current.rec, C.cuts.slice());
  if (!r.ok) { banner([r.error, r.advice].filter(Boolean).join(" ")); return; }
  C.splitJob = r.job;
  banner("Splitting it into separate recordings…", "ok", { label: "Cancel splitting", run: cancelSplit });
  progress(0, 100);
  showCuts();
  registerJob(r.job, splitEvent);
}

function keepAsOne() {
  if (!LV.cuts || LV.cuts.splitJob) return;
  LV.cuts = null;
  showCuts();
  banner("Kept as one recording.", "ok");
}

async function cancelSplit() {
  if (!LV.cuts || !LV.cuts.splitJob) return;
  const r = await api().cancel_import_split(LV.cuts.splitJob);
  if (!r.ok) banner(r.error);
}

function splitEvent(event, p) {
  if (!LV.cuts || p.job !== LV.cuts.splitJob) return;
  if (event === "import-split-progress") {
    progress(p.percent, 100);
    status(`Splitting the import into separate recordings… ${p.percent}%`);
    return;
  }
  LV.jobs.delete(p.job);
  LV.cuts = null;
  showCuts();
  progress(0, null);
  status("");
  if (event === "import-split-done") {
    const later = (p.problems || []).join(" ");
    banner(`✓ Split ${p.full} into ${plural(p.files.length, "recording")} in ${p.folder}. The whole import is kept too.` +
           (later ? ` ${later}` : ""), later ? "warn" : "ok");
  } else banner(p.error, "warn");
  loadLibrary();
}

// Another recording was loaded in the player (app.js setCurrent): the cuts belong to the import.
function liveCutsLeft(r) {
  if (LV.cuts && !LV.cuts.splitJob && (!r || r.fp !== LV.cuts.fp)) LV.cuts = null;
  clearCutRegions();
  $("cut-bar").hidden = true;
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

// The meter: the bar follows the peak; the number is the peak held for a moment (readable, not
// jittering), "Silent" under LIVE_METER_SILENT_DB; "Too loud" while the input clips.
function drawMeter() {
  const m = LV.meter, db = dbfs(m.peak), held = dbfs(m.held);
  const pos = (d) => `${Math.max(0, Math.min(100, (d + 60) / 60 * 100))}%`;
  $("live-meter-fill").style.width = pos(db);
  $("live-meter-peak").style.left = pos(held);
  const clip = m.held >= 0.99;
  $("live-meter").classList.toggle("clip", clip);
  $("live-clip").hidden = !clip;
  $("live-level").textContent = held < LIVE_METER_SILENT_DB ? "Silent" : `${Math.round(held)} dB`;
}

// No sound coming in (a muted microphone, the wrong input): said after LIVE_SILENT_SEC under
// LIVE_SILENT_DB, cleared as soon as sound comes. Counted in frames, so it is the input's own time.
function checkSilence(frames, sumsq, channels) {
  const db = 10 * Math.log10(Math.max(1e-12, sumsq / Math.max(1, frames * channels)));
  LV.quietFrames = db < LIVE_SILENT_DB ? LV.quietFrames + frames : 0;
  $("live-silent").hidden = LV.quietFrames < LIVE_SILENT_SEC * LV.rate;
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

// ---- what was drawn, kept to draw it again: a canvas is cleared whenever it is resized (the window
// resized, or moved to a screen with another pixel ratio). The last LIVE_HISTORY_COLS columns of
// each (more than the widest canvas) live in ring buffers: the waveform's low/high, and the
// spectrogram's frequency bins up to its top. A resize redraws the whole canvas from them.
const LIVE_HISTORY_COLS = 8192;
function historyReset() {
  LV.hist = { wave: new Float32Array(LIVE_HISTORY_COLS * 2), waveN: 0, spec: null, specN: 0, specBins: 0, markers: [] };
}

function pushWave(lo, hi) {
  const h = LV.hist, i = (h.waveN++ % LIVE_HISTORY_COLS) * 2;
  h.wave[i] = lo; h.wave[i + 1] = hi;
}

function pushSpec(bins, kept, times) {
  const h = LV.hist;
  if (!h.spec || h.specBins !== kept) { h.spec = new Uint8Array(LIVE_HISTORY_COLS * kept); h.specBins = kept; h.specN = 0; }
  for (let t = 0; t < times; t++) h.spec.set(bins.subarray(0, kept), (h.specN++ % LIVE_HISTORY_COLS) * kept);
}

function waveColor() {
  if (!LV.color) LV.color = getComputedStyle($("live")).getPropertyValue("--live-accent").trim() ||
                            getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#2f6f5e";
  return LV.color;
}

// The spectrogram's colour where nothing was drawn yet: lighter than silence (inferno's near black),
// so silence shows as a dark band moving in.
function emptyColor() {
  if (!LV.emptyColor) LV.emptyColor = getComputedStyle($("live")).getPropertyValue("--live-empty").trim() || "#2b2b2b";
  return LV.emptyColor;
}

// The marks whose columns are among the last m drawn, at their place.
function drawMarkers(c, g, m) {
  const h = LV.hist, first = h.waveN - m;
  g.fillStyle = waveColor();
  for (const mk of h.markers) {
    if (mk.col < first || mk.col >= h.waveN) continue;
    const x = c.width - (h.waveN - mk.col);
    g.fillRect(x, 0, 2, c.height);
    if (g.fillText) {
      const px = window.devicePixelRatio || 1;
      g.font = `bold ${Math.round(14 * px)}px sans-serif`;
      g.textAlign = "right";                          // left of the line: drawn while the line is at the edge
      g.fillText(mk.label, x - 4 * px, (16 + 16 * (mk.row || 0)) * px);
    }
  }
}

function redrawWave(c, g) {
  const h = LV.hist, m = Math.min(h.waveN, LIVE_HISTORY_COLS, c.width), mid = c.height / 2;
  g.clearRect(0, 0, c.width, c.height);
  g.fillStyle = waveColor();
  for (let k = 0; k < m; k++) {
    const i = ((h.waveN - m + k) % LIVE_HISTORY_COLS) * 2;
    const y0 = mid - h.wave[i + 1] * mid, y1 = mid - h.wave[i] * mid;
    g.fillRect(c.width - m + k, y0, 1, Math.max(1, y1 - y0));
  }
  drawMarkers(c, g, m);
}

// For each row of a canvas this tall, the spectrogram bin it shows (0 Hz at the bottom).
function specRows(height) {
  const top = Math.min(LIVE_SPEC_HZ, LV.rate / 2), binHz = LV.rate / (LV.analyser ? LV.analyser.fftSize : 2048);
  const rows = new Int32Array(height);
  for (let y = 0; y < height; y++) rows[y] = Math.round((height - 1 - y) / Math.max(1, height - 1) * top / binHz);
  return rows;
}

function redrawSpec(c, g) {
  const h = LV.hist, sp = LV.spec;
  g.fillStyle = emptyColor(); g.fillRect(0, 0, c.width, c.height);
  sp.img = g.createImageData(1, c.height);
  sp.rows = specRows(c.height);
  const m = h.spec ? Math.min(h.specN, LIVE_HISTORY_COLS, c.width) : 0;
  if (!m) return;
  if (!sp.lut) sp.lut = infernoLut();
  const img = g.createImageData(m, c.height), px = img.data, kept = h.specBins;
  for (let k = 0; k < m; k++) {
    const base = ((h.specN - m + k) % LIVE_HISTORY_COLS) * kept;
    for (let y = 0; y < c.height; y++) {
      const v = h.spec[base + Math.min(kept - 1, sp.rows[y])], o = (y * m + k) * 4;
      px[o] = sp.lut[v * 3]; px[o + 1] = sp.lut[v * 3 + 1]; px[o + 2] = sp.lut[v * 3 + 2]; px[o + 3] = 255;
    }
  }
  g.putImageData(img, c.width - m, 0);
}

// Both live canvases drawn again from their history, sized to their boxes (debounced: a resize
// fires many times while the window is dragged).
function liveRedraw() {
  LV.redrawTimer = 0;
  for (const [id, redraw] of [["live-wave", redrawWave], ["live-spec", redrawSpec]]) {
    const c = $(id);
    if (!c.getContext || (id === "live-spec" && !LV.analyser)) continue;
    canvasSize(c);
    redraw(c, c.getContext("2d"));
  }
}

function scheduleRedraw() {
  if (LV.redrawTimer) clearTimeout(LV.redrawTimer);
  LV.redrawTimer = setTimeout(liveRedraw, 100);
}

// A resize of either canvas, or a change of the screen's pixel ratio, redraws them from history.
function watchLiveSizes() {
  if (typeof ResizeObserver !== "undefined" && !LV.sizeWatch) {
    LV.sizeWatch = new ResizeObserver(scheduleRedraw);
    for (const id of ["live-wave", "live-spec"]) LV.sizeWatch.observe($(id));
  }
  if (window.matchMedia && !LV.ratioWatch) {
    const arm = () => {
      LV.ratioWatch = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
      LV.ratioWatch.addEventListener("change", () => { LV.ratioWatch = null; scheduleRedraw(); arm(); }, { once: true });
    };
    arm();
  }
}

function drawWave() {
  const c = $("live-wave"), cols = LV.wave.cols;
  if (!c.getContext) return;
  const g = c.getContext("2d");
  for (const [lo, hi] of cols) pushWave(lo, hi);
  if (canvasSize(c)) { cols.length = 0; redrawWave(c, g); return; }   // resized: everything again
  if (!cols.length) return;
  const n = Math.min(cols.length, c.width), mid = c.height / 2;
  shiftLeft(g, c, n);
  g.fillStyle = waveColor();
  const from = cols.length - n;
  for (let i = 0; i < n; i++) {
    const [lo, hi] = cols[from + i];
    const y0 = mid - hi * mid, y1 = mid - lo * mid;
    g.fillRect(c.width - n + i, y0, 1, Math.max(1, y1 - y0));
  }
  drawMarkers(c, g, n);                                 // markers among the columns just drawn
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
  const c = $("live-spec"), a = specAnalyser(), sp = LV.spec;
  if (!a || !c.getContext) return;
  const g = c.getContext("2d");
  if (canvasSize(c) || !sp.img || sp.img.height !== c.height) redrawSpec(c, g);   // resized: everything again
  if (!sp.last || now - sp.last > 1000) { sp.last = now; return; }    // first frame, or back after a pause
  const due = Math.min(c.width, Math.floor((now - sp.last) * LIVE_COLS_PER_SEC / 1000));
  if (due < 1) return;
  sp.last += due * 1000 / LIVE_COLS_PER_SEC;
  if (!sp.lut) sp.lut = infernoLut();
  a.getByteFrequencyData(sp.bins);
  const top = Math.min(LIVE_SPEC_HZ, LV.rate / 2), binHz = LV.rate / a.fftSize, h = c.height;
  pushSpec(sp.bins, Math.min(sp.bins.length, Math.ceil(top / binHz) + 1), due);
  const px = sp.img.data;
  for (let y = 0; y < h; y++) {
    const v = sp.bins[Math.min(sp.bins.length - 1, sp.rows[y])];
    px[y * 4] = sp.lut[v * 3]; px[y * 4 + 1] = sp.lut[v * 3 + 1]; px[y * 4 + 2] = sp.lut[v * 3 + 2]; px[y * 4 + 3] = 255;
  }
  shiftLeft(g, c, due);
  for (let i = due; i >= 1; i--) g.putImageData(sp.img, c.width - i, 0);
}

// ---- crash recovery: files a crash left unfinished are finished at startup ----
async function liveRecover() {
  const r = await api().live_recover();
  const unsaved = r.unsaved || [];
  if (!r.ok || (!r.recovered.length && !r.failed.length && !unsaved.length)) return r;
  const parts = [];
  if (r.recovered.length) {
    const names = r.recovered.map((f) => `${f.name} (in ${f.folder})`).join(", ");
    parts.push(r.recovered.length === 1 ? `A recording was cut off last time; OpenEVP saved what it had: ${names}.`
                                        : `${r.recovered.length} recordings were cut off last time; OpenEVP saved what it had: ${names}.`);
  }
  if (r.failed.length) parts.push(`Could not finish: ${r.failed.join(" · ")}`);
  for (const u of unsaved) {
    parts.push(`OpenEVP closed while saving ${u.file || "a recording"}, and these marks may not have been saved: ` +
               `${u.items.join("; ")}. Check them in the player.`);
  }
  banner(parts.join(" "), r.failed.length || unsaved.length ? "warn" : "ok");
  loadLibrary();
  return r;
}

// ---- wiring ----
function setupLive() {
  historyReset();
  watchLiveSizes();
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
  $("live-monitor").onchange = () => { applyMonitor(); showListen(); renderLive(); };
  $("live-field").onchange = () => { applyField(); saveLive({ field: $("live-field").checked }); };
  $("live-sound-settings").onclick = () => api().open_sound_settings();
  setupListen();
  $("live-record").onclick = () => (liveRecording() ? stopRecording() : startRecording());
  $("live-mark").onclick = liveMark;
  $("cut-add").onclick = addCut;
  $("cut-split").onclick = splitImport;
  $("cut-keep").onclick = keepAsOne;
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
                mark: !!(mark && mark.ok), marked: mark && mark.mark, files: (r && r.files) || [], error: r && r.error,
                dropped_marks: r && r.dropped_marks, problems: r && r.problems };
  closeInput();
  S.view = "library";
  return out;
}
