# Live mode and analog import (developer notes)

Record from one of the PC's audio inputs into the EVP Library, with a live
waveform, spectrogram and level meter; or play a recorder that only has a
headphone jack (Panasonic RR-DR60) into line-in and get one WAV per recording.
User-facing instructions are in `docs/user-guide.md` ("Recording live",
"Importing from a recorder with only a headphone jack").

## Where the audio is captured: the spike

Two options were weighed:

- **A, in the page:** `getUserMedia` + `enumerateDevices` in WebView2, an
  AudioWorklet turning the input into 16-bit PCM, chunks sent to the backend,
  an AnalyserNode for the spectrogram.
- **B, in the backend:** WASAPI through ctypes COM (no new dependency) or
  `sounddevice`/PortAudio (a new one).

**A was chosen.** The spike (a hidden pywebview window, the same pywebview
6.2.1 / WebView2 as the app) proved the whole path without touching a real
input:

- `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--use-fake-device-for-media-stream
  --use-file-for-fake-audio-capture=tone.wav"` gives the page Chromium's fake
  inputs only (no real device is enumerated or opened), fed from a generated
  WAV. The variable is honoured although pywebview sets its own
  `AdditionalBrowserArguments`.
- A `PermissionRequested` handler added through pythonnet on
  `window.native.webview.CoreWebView2` fired once, for kind `Microphone`, with
  `Uri` = the page's origin (`http://127.0.0.1:<port>/`); setting
  `State = Allow` let `getUserMedia` resolve with no prompt.
- With echo cancellation, noise suppression and AGC off, a 440 Hz left / 660 Hz
  right test tone at -13.5 dBFS RMS came back at exactly those frequencies and
  that level, on the right channels, at the fake device's 44.1 kHz.
- 26 base64 chunks (a quarter second each at 44.1 kHz stereo) went through
  `js_api` in order; the slowest call took 37 ms.

Why A over B:

- **Testable without Michael's microphone.** Chromium's fake device exercises
  the real capture path (permission, getUserMedia, the worklet, the bridge, the
  writer) in headless tests and in the frozen app's `--smoke`. B can only be
  tested against a real capture endpoint (no virtual cable is installed here).
- **No new dependency, nothing new to bundle.** WebView2 and pythonnet ship
  already; B with ctypes WASAPI would be several hundred lines of COM vtables
  (IMMDeviceEnumerator, IPropertyStore for names, IAudioClient,
  IAudioCaptureClient, format negotiation), and `sounddevice` would add
  PortAudio to the bundle and to `release_check`.
- **The spectrogram and listening come free.** An AnalyserNode costs the page
  nothing; listening with Enhance reuses the player's Web Audio chain
  (`enhanceGraph` / `makeNodes`).
- **Robust in the frozen app:** the release check proves it on every build
  (see "Smoke test").

What A costs: the audio crosses the bridge as base64 (about 256 KB/s at 48 kHz
stereo; measured backend cost about 1.2 ms per half-second chunk, so about 8 s
of CPU per hour), and the input's rate is whatever Chromium opens it at (the
Windows mix format, usually 48 kHz).

## Microphone permission (`app/mic_permission.py`)

pywebview does not handle WebView2's `PermissionRequested`, and it runs
WebView2 in private mode, so without a handler WebView2 would show its own
prompt every session. `install_early(window)` subscribes in the window's
`before_show` event (on the GUI thread) to the WebView2 control's
`CoreWebView2InitializationCompleted`, and there attaches the handlers, before
the app's page or any frame in it exists. The app's page is **pinned** then:
`window.real_url`, the URL pywebview loads from its own server on 127.0.0.1.

- kind `Microphone` is **allowed** (with `SavesInProfile = False`) only when the
  request comes from the pinned origin (scheme, host, port) and the window still
  shows the pinned page;
- `Microphone` from anything else is **denied**, and every frame gets its own
  `PermissionRequested` handler (via `FrameCreated`) that denies microphone
  requests and marks them handled, so they never reach the window's handler;
- top-level navigation away from the pinned origin is cancelled
  (`NavigationStarting`); the page has no links of its own;
- every other kind is left to WebView2; a page that cannot be pinned (not http
  on 127.0.0.1/localhost) gets no microphone at all; an exception in a handler
  denies.

Checked in a hidden WebView2 with Chromium's fake input
(`tests/webview2_mic_probe.py`, run by `test_live.MicPermissionWebView2Tests`
with `OPENEVP_TEST_WEBVIEW2=1`): the page is allowed, a same-origin iframe with
`allow="microphone"` is denied, navigation to another local server is
cancelled, another page of the same server is denied. (Attaching only after the
first `loaded` event, as at first, left a frame made before that allowed.)

Windows' privacy switch still applies; the page maps `NotAllowedError` /
`NotReadableError` to a plain message with **Open microphone settings**
(`ms-settings:privacy-microphone`, `Api.open_mic_settings`).

## Data flow

```
input -> getUserMedia (EC/NS/AGC off, channelCount ideal 2)
      -> AudioContext({sampleRate: track rate})
         -> AudioWorkletNode "openevp-capture" (live-worklet.js): float -> int16
            (x * 32768, rounded: exact for a 16-bit source), batches of 2048 frames
            with peak and sum of squares -> port.postMessage (transferred)
         -> AnalyserNode (fftSize 2048, -100..-25 dB) -> spectrogram
         -> [Listen or Show what I hear] -> Enhance nodes (the player's settings)
               -> destination (Listen, off by default) / a second AnalyserNode (Show what I hear)
page:  batches -> meter + waveform columns; while recording, ~0.5 s of PCM ->
       base64 -> a queue -> one sender: Api.live_chunk(session, seq, data)
backend (app/live.py): seq checked, base64 decoded, disk space checked,
       -> WavPart (.part): one file, for Live and Import alike
after Stop (an import with a silence gap set): it opens in the player with
       suggested cuts (openevp.silence, one pass over the whole file, in the
       background); the user edits and confirms them; a job writes one WAV per part
```

**Bounds on the page** (`live.js`):

- Audio captured but not yet taken by the backend (held + queued + in flight)
  is at most 10 s. Past that, capture stops with a plain message, the queue is
  dropped and the backend finishes what it has. One call is in flight at a time.
- A bridge call slower than 15 s means the backend is not answering: recording
  stops the same way.
- **Stop** = an acknowledged worklet flush (the worklet sends the samples short
  of a batch, then its answer, on the same port; at most 2 s), then the queue
  drains (at most 30 s), then `live_stop` (at most 15 s). If the backend never
  answers, the page says the file is finished when OpenEVP closes or starts again.
- There is one Stop per recording: `stopRecording()` returns the Stop already
  running, so Stop pressed twice, or the window closing during "Saving...",
  waits for it.
- Audio Stop could not hand over (a chunk that timed out or was refused, a
  worklet that never answered its flush, a drain out of time, an overflowing
  queue) is counted; the final message then says what was saved and that the
  last N seconds may be missing, and why, as a warning (never "✓ Saved").
  Whenever sending ends unsuccessfully, the chunk in flight and everything
  still queued (and anything that would have been queued after) is counted.
- **Closing the window** during a recording: `main.py` holds the close, runs
  the page's `liveDrainForClose()` (the same Stop) and waits for it. The whole
  close takes at most 70 s, whatever hangs: the page is asked on a thread of its
  own (pywebview's `evaluate_js` waits for the page with no time limit) and gets
  the first three quarters; if it did not finish, `Api.finish_recording()`
  (which may wait on the recording's lock or the disk) runs on a thread of its
  own until the deadline; then the window closes regardless. `shutdown()` waits
  at most 10 s for the recording's lock. A file left unfinished is a `.part` that
  the next start recovers.
- The backend finishes a session that gets no chunk or mark for 60 s (the page
  is gone or stuck), so its file is not left open until the app closes.
- **Events never wait for the page.** `evaluate_js` has no time limit, so no
  worker calls it: `Api`'s emit posts to `app/events.py`'s dispatcher and one
  daemon thread delivers events in order. Only progress is ever given up: when
  the queue is full, incoming progress replaces the queued progress of its kind
  (same event and job or scan) or is dropped, other events push out the oldest
  progress, and with no progress queued the queue grows rather than lose a row
  or a "done". Closing tells the import jobs to stop and waits for them at most
  10 s in all (a job is registered and started in one step under
  `_workers_lock`, so none is ever joined unstarted, and nothing from that wait
  escapes `shutdown()`); the dispatcher is closed with a 1 s bound.

Drawing runs on animation frames at most 30 times a second and only scrolls
what is already drawn (`globalCompositeOperation = "copy"` for the shift: a
transparent canvas drawn over itself would keep every old stroke), so an hour
costs the same as a minute. Waveform and spectrogram share one time scale
(40 columns a second).

## Files and crash safety (`openevp/livewav.py`)

- A recording is written to `<final name>.part` in the folder it ends up in,
  with a hidden `<final name>.part.json` sidecar: format, final name, mode and
  the marks made so far (rewritten atomically on each mark).
- `WavPart` writes a standard 44-byte PCM header at once and rewrites its two
  size fields every 5 s of audio, flushing and `fsync`-ing then. A crash leaves
  a playable file missing at most the last 5 s.
- Every `.part` is listed in the store setting `live_parts` from its first byte
  until it is finished. `Api.live_recover()` (the page calls it at startup and
  on `store-writable`) fixes each leftover's header from its size (cut to whole
  frames), fingerprints it, renames it, stores the sidecar's marks and tells
  the user. It never touches the session's own files, and a read-only store
  (a second window) recovers nothing.
- **Nothing that may hold audio is deleted.** Only a file proven empty (a
  readable header and less than one frame after it, or no bytes past where a
  header would be) is removed. A file whose header is unreadable and whose
  sidecar gives no format is kept as `<name> (unrecovered).raw` (sidecar beside
  it) and the user is told.
- **The destination is journalled.** Before the rename, the sidecar records the
  destination, the fingerprint and the frame count. A crash between the rename
  and storing the marks leaves the WAV and the sidecar; recovery checks the WAV
  is still that audio (fingerprint) and stores the marks. Storing marks is
  idempotent (a mark with the same times, class and note is not added twice).
- **The journal goes only once everything is stored.** When the store refuses
  a mark or a question (read-only, or its file could not be written), the WAV
  is kept and named as usual, but the sidecar and the `live_parts` entry stay,
  and the user is told ("... OpenEVP will try again the next time it starts").
  Recovery tries again (`_MetaPending` keeps the entry) until all of it is
  stored; so it does when the published WAV could not be read just now (the
  entry stays while the `.part` or its journal is there). A split piece is
  journalled before it is published (destination, fingerprint, frames, its
  marks and questions; still `derived`, so a `.part` left by a crash is deleted
  while a published piece has its metadata stored by recovery). If that journal
  cannot be written the piece is not published and is cleaned up; if publishing
  fails the piece is cleaned up. A mark the store rejects as invalid (outside the
  file) is dropped and counted, as before.
- Finishing: `close()` returns the frame count and the fingerprint, computed
  while writing (SHA-256 over `wavinfo.fingerprint_prefix` + the PCM, the same
  as `wavinfo.wav_fingerprint`). `publish()` renames without ever replacing a
  file (`os.rename` on Windows refuses; the next free `"<stem> (N)"` is used).
  The fingerprint goes into the library's index (`remember_fp`), so listing
  never reads the file again.
- Names: `Live YYYY-MM-DD HH-MM-SS.wav`; an import `Import YYYY-MM-DD HH-MM-SS.wav`,
  or `Import YYYY-MM-DD HH-MM-SS (full).wav` when it is to be split, and then its
  pieces `Import YYYY-MM-DD HH-MM-SS (n).wav` (the time Record was pressed).
- Limits: recording stops (and saves) when the drive would have less than
  500 MB free, warning from 15 minutes before; and at 4 GiB - 2 MiB of audio per file
  (RIFF's limit), about 6.2 h of 48 kHz stereo. Start is refused with less than
  500 MB + one minute free.

## Marks

A mark (M) is a region of 2 s ending at the moment M was pressed (shorter at
the very start), class C, note "Marked while recording". The marks store holds
regions with a minimum length (point markers exist only as imports), and a
region shows on the waveform and plays like any other mark; class and note are
edited in the player afterwards. The page sends the time it counted (frames
captured since Record) after its queued audio has been taken; the backend keeps
the mark in memory and in the sidecar and stores it against the fingerprint
when the file is finished (clamped to the file's length, times rounded down to
the millisecond: the store refuses a mark past the end). When an import is
split, each piece gets the marks of the whole file that end in it, at its own
times (a mark starting before the piece is cut at its start).

## Splitting an import: suggested cuts, confirmed by the user

An import is recorded as one file, exactly as Live mode records. After four
review rounds on automatic splitting (a streaming splitter, then an automatic
offline one, each with corner cases), the ruling was to put the user in the
loop: OpenEVP **suggests** cuts and never splits without confirmation.

**After Stop** (`LiveOps._suggest_job`, in the background): the import opens in
the player at once; `openevp.silence.plan` looks for the gaps and the page
draws each suggested cut as a marker on the waveform (`live.js` showCuts), with
a bar under it: each cut as a chip (✕ removes it; clicking the marker removes it
too), **✂ Add cut here** (at the play cursor; at least 0.5 s from the ends and
from other cuts), **Split into N recordings** and **Keep as one**.

**The split** (`Api.split_import(rec, cuts)`, then `LiveOps._split_job`): the
confirmed cut list, checked (sorted, inside the file, parts at least 0.5 s, at
most 500 cuts), for the recording in the player (a WAV inside the library; its
fingerprint is checked again before reading). Each part is written to a `.part`
listed in `live_parts` with a `"derived": true` sidecar (a crash leaves nothing
to finish: the next start deletes them); only when all are written does each
get its name (`<stem without " (full)"> (n).wav`), the marks that end in it (at
its own times; a mark starting before the part is cut at its start) and its
fingerprint in the index. The parts put together are the whole file, sample for
sample, and **the whole file is always kept**. Cancelled (**Cancel splitting**),
failed, or the app closing: the parts written so far are deleted and the page
is told. Progress comes as `import-split-progress`, the result as
`import-split-done` / `import-split-failed`.

**Job ids and events.** A job is registered before its thread starts. Its id
reaches the page with the answer (Stop's `suggest`, `split_import`'s `job`),
which can come after the job's first events: the page keeps events of jobs it
does not know yet (`LV.jobEvents`, the last 20 jobs) and replays them when the
job is registered, so a fast job never leaves a stale "Splitting…".

**Suggestions** (`openevp/silence.py`), simple and conservative, linear in the
length of the file (prefix counts, no percentile per candidate; thousands of
candidates take milliseconds), cancellable throughout:

1. **Levels:** one pass reads the file in chunks and computes the RMS (dBFS) of
   every 50 ms block.
2. **Floor:** every steady second (10th-90th percentile within 6 dB) gives its
   median; the floor is the 10th percentile of those medians over the file.
3. **Background:** the most common median (to the dB) of the steady seconds
   more than 8 dB above the floor: the level the recordings sit at between their
   sounds. No background, or no content more than 8 dB above it: no suggestion.
4. **Gaps:** runs of blocks at the floor (within 4 dB), at least the gap
   setting long (default 3 s), with sound (more than 8 dB above the floor) before
   and after. Two runs are one gap only when what is between them is under
   0.5 s in all, under 20% of the merged span, and under 1 s of sound (a click
   or a dip; eight seconds just above the floor never are). Quiet at the start
   and the end is never a gap.
5. **Cuts:** 0.5 s before the next sound (never before the gap starts).
6. **Evidence per piece:** its content (from its first sound up to its gap) must
   have at least 1 s of sound, at least 20% at the background, and under 20% at
   the floor; otherwise it joins the next piece. A recording whose pauses sit at
   the floor, or that is all loud with no background of its own, gets no cut.

## While recording

- While cuts are being looked for in an import, or it is being split, Record,
  the library's folder operations and updates wait (`_live_busy`), and the
  split holds the folders (`folders.Pins`) and checks them again before each part.
  A split is admitted like a recording: under `_workers_lock`, refused while a
  folder operation (`_fs_done`) or an update (`_update_claim`) is admitted.
- One session at a time; it needs the writable store (a second window cannot
  record).
- The folders from the library folder down to the destination are held open
  (`folders.Pins`) for the whole recording, so none can be renamed, moved or
  swapped for a junction; the library folder's identity (`_root_identity`) is
  recorded at Record and the destination re-checked (identity, no link on the
  way: `folders.inside`) before every new file. An import stops, keeping what
  it saved, if it moved. Recording into the library folder itself refuses a
  library folder that changed since it was listed.
- `_fs_begin` refuses folder operations ("Stop the recording first."). The
  update and a recording are admitted under one lock (`_workers_lock`):
  `install_update` takes `_busy` and sets `_update_claim` there and refuses a
  recording; `live_start` refuses while `_update_claim` is set and sets the
  recording there. Never take `_lib_lock` inside `_workers_lock`
  (`_list_library` takes them the other way round).
- Closing the window asks ("Recording in progress"), then saves as Stop does
  (see "Bounds on the page"); `shutdown()` finishes the session before closing
  the store.
- The page greys out the sidebar; the input's track ending (unplugged) stops and
  saves.
- The input remembered is `{id, label}` (setting `live`, with the split and the
  Import toggle). Device ids are per-origin and change between sessions in
  private mode, so the page asks for the remembered id as `ideal`, then
  re-opens by **label** with `exact` if that is a different device.
- Listening is never remembered: off whenever the view opens.
- **Enhance in the Live view** is the player's own Enhance tab panel (`#panel-enhance`,
  its handlers in app.js `setupEnhance`), moved into the Live view's "Enhance what I
  hear" box while the view is open and back afterwards (live.js `borrowEnhance` /
  `returnEnhance`): one implementation, one stored setting (`enhance`), so a change in
  either place is the other's too (`setEnhance` also calls `liveEnhanceChanged`).
  Noise reduction (learnt from a recording) and "Exports enhanced" are hidden there;
  Cut hiss follows the input's rate (`enhRate` asks `liveRate`). The chain
  (`applyMonitor`) is built only while Listen or Show what I hear is on: the source
  goes to the worklet and the raw analyser directly (the saved file and the waveform
  are always the input) and, through the chain, to the speakers (Listen) and to a
  second analyser (Show what I hear, remembered as the setting `live.heard`), which
  the spectrogram then reads. The nodes are Web Audio's own (biquads, compressor,
  gain, wave shaper): a few, rebuilt only when a setting changes.
- If a saved Live recording cannot be opened in the player, Stop's result says
  why (`player_error`) and the page shows it; the file is saved either way.

## The screen: made for touch in the field

Only the Live screen is touch sized; the rest of the app keeps its sizes.

- **Layout:** a status strip (the elapsed time at 44 px; a 260 px meter with
  "Too loud" while it clips and "Silent" under -90 dB, its number the peak held
  for 1.5 s; the time left on the drive from the backend's `left_seconds`, in
  `live_start` and every `live_chunk` answer, as "about 9 h left"; the input and
  its format), then the scopes and the Session panel. The scopes fill the space
  that is left (spectrogram 7, waveform 3) and redraw from their history on any
  resize. The Session panel sits beside them from 1000 px wide, under them
  below that: `liveLayout()` sets `#live.wide` from a ResizeObserver, so it is
  testable without a layout engine.
- **Touch:** every button, select and field on this screen is at least 48 px;
  every on/off choice (Listen, Show what I hear, Enhance what I hear, Night
  screen, Suggest cuts, and the Enhance options inside the borrowed panel) is a
  checkbox drawn as a big toggle button (CSS `label:has(input:checked)`, the
  checkbox kept for keyboards and screen readers). Record is bottom left, MARK
  bottom right under the thumb, with the toggles on the row below; M still works.
- **Night screen** (`live.field`, remembered): dark red colours for the screen
  and the window around it while it is open (`body.live-night`), the waveform in
  red, the spectrogram dimmed and reddened by a CSS filter.
- **Preview and silence:** the input is drawn from the moment it opens; nothing
  is sent until Record. The spectrogram's empty area is grey, lighter than
  silence, so silence shows as a dark band moving in, and the waveform as a flat
  line. Under -80 dB RMS for 3 s of the input's own frames, the screen says "No
  sound coming in. Is the mic muted, or is the wrong input selected?" with
  **Open sound settings** (`Api.open_sound_settings`, `ms-settings:sound`); it
  clears as soon as sound comes.
- **Questions** (`Api.live_question`): logged at the moment Enter or Log
  question is pressed, kept in the sidecar (`questions`), stored at the finish
  in `questions.json` (`AppData.add_question`, idempotent: never marks, never
  counted as EVPs), also on crash recovery and mapped into the parts of a split
  import. `questions.json` is its own file, keyed by fingerprint, with the marks
  store's rules (the folder lock, atomic writes, a second window read-only, a bad
  or newer file set aside). It is never in `marks.json`: an older OpenEVP
  rewrites `marks.json` with only the fields it knows, and must not be able to
  drop them; older versions never open `questions.json`. Like marks, questions
  stay when a recording is deleted, so it has them again when restored.
  A test build kept questions in `marks.json`: on load they are merged into
  `questions.json` (the same time and text is one), which is written atomically
  first; only then is `marks.json` rewritten without them. A crash between leaves
  them in both and the next load merges again without doubling. While they
  cannot be moved (a read-only window, or `questions.json` cannot be written),
  they are shown from memory and every `marks.json` write keeps them. They are drawn on the live waveform
  as labelled markers (kept in the history, so a resize keeps them) and shown
  in the player as markers (`q-` regions) and a list ("Questions asked:").
- **The live marks list:** each mark as it is made, with its time, A/B/C
  buttons and a note; a change goes to `Api.live_mark_update` at once (the mark
  in the sidecar), and is stored with the file at the finish through the same
  journalled path as every mark.
- **Order:** marks, edits and questions go to the backend one at a time, in the
  order they were made (a promise chain per recording, `metaOp`), each call with
  its own time limit, and Stop waits for all of them before `live_stop`
  (`drainMeta`). If one goes unanswered during Stop, or the whole wait passes
  60 s, what is still queued is called off (never sent after the finish) and the
  saved message lists the changes that may not have been saved. Each edit carries
  a number that only grows; the backend keeps the newest number applied per mark
  and field (class, note), so a late call never undoes a newer change of the
  same field and never blocks a change of another (`"applied": [fields]`, or
  `{"stale": true}`).
- **Opening the input:** every open has a generation (`LV.gen`, only grows).
  Leaving the view, closing the input or opening another calls it off; after
  each await it checks its generation and that the Live view is still shown,
  and a stream that arrives for an open called off is stopped at once.

## Smoke test

`OpenEVP.exe --smoke` sets `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` to
`--use-fake-device-for-media-stream --autoplay-policy=no-user-gesture-required`
(only in that process; restored after), then `_smoke_live` runs `liveSmoke(1.5)`
in the page: the default (fake) input recorded through the Record button's own
code (Stop with its flush and drain), one mark, saved into the throwaway
library folder, and checks the WAV (16-bit, rate and channels as captured,
length) and that the mark was stored. No `--use-fake-ui-for-media-stream`: the
app's own permission handler must answer, or `getUserMedia` waits for an
invisible prompt and the check times out. `tools/release_check.py` fails a
report without a good `live` section.

## Tests

- `tests/test_live.py`:
  - the writer: incremental writes, header rewrites, never overwriting, the
    size limit, recovery of a cut-off `.part`, an unreadable one kept;
  - the suggestions on synthetic signals: recordings with gaps, a gap of exactly
    the setting (its pre-roll), hiss only and quiet at the ends, pauses shorter
    than the gap, a recording whose pauses sit at the floor, Astra's third and
    fourth-pass cases (a first long floor-level pause between loud passages,
    dips around a long near-floor passage), quiet that is not the floor, a click
    in a gap, a longer gap setting, stereo, digital silence, an hour-long file
    planned in seconds, thousands of rejected candidates in well under a
    second, and cancellation;
  - the backend: placement, names, marks after finishing (also at the last
    sample), the player result and its failure, Clips refused, an import with
    suggested cuts and a split only when confirmed (parts put together equal the
    whole file, marks in the right part, the user's own cuts, bad cut lists, keep
    as one, no gaps, cancelled, folder moved, half-written parts deleted at the
    next start, the job id registered before its thread, shutdown with a blocked
    page and with a job stuck in its emit), the disk-space and 4 GB stops, a second
    window, folder operations and updates waiting (both directions), the
    destination held and re-checked, closing saves, an idle session finished,
    crash recovery (a cut-off `.part`, a crash between rename and marks, a
    journal pointing at another file);
  - playback through the real audio server (in place, and from a private copy
    without file identity); the permission policy; the close drain; the smoke
    check; and, opt-in, the permission handlers in a real WebView2.
- `tests/ui_check.js`: the view, the input found again by name, raw-input
  constraints, folder choice, Listen with Enhance, record / stop states, the
  chunk bytes, the flushed tail saved on Stop, M, the player opening and its
  failure, Import with its settings and guide, the suggested cuts (markers,
  remove, add, refuse one too close, confirm, keep as one, events before the job
  is known), the split's progress, Cancel and results, Astra's drain case, a
  backend-initiated stop, a stalled bridge (bounded queue, timeouts, Stop
  bounded), a worklet that never answers its flush, Windows blocking the
  microphone, recovery messages.

## Known limits

- Not yet tried with a real microphone, USB adapter or RR-DR60 (manual
  checklist items 47-56).
- Chromium may resample when the input's rate differs from what the
  AudioContext accepts; the saved rate is the AudioContext's.
- On Stop, the player's audio server reads the whole file once for peaks (a few
  seconds for an hour of 48 kHz stereo).
- A file stops at 4 GB rather than continuing into a second file.
- While recording, the library folder and the folders down to the destination
  cannot be renamed in File Explorer either (they are held open).
- The import splitter errs towards not splitting: a recording whose own
  quiet parts sit at the floor stays joined to the next one. The pieces appear
  shortly after Stop (an hour takes seconds, plus writing the copies).
