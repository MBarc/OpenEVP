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
         -> [Listen] -> Enhance nodes -> destination        (off by default)
page:  batches -> meter + waveform columns; while recording, ~0.5 s of PCM ->
       base64 -> a queue -> one sender: Api.live_chunk(session, seq, data)
backend (app/live.py): seq checked, base64 decoded, disk space checked,
       -> [import split: the whole input] + Splitter, or straight -> WavPart (.part)
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
- **Closing the window** during a recording: `main.py` holds the close, runs
  the page's `liveDrainForClose()` (the same steps as Stop) and waits for it, at
  most 60 s, then closes; `shutdown()` finishes whatever the page could not hand
  over.
- The backend finishes a session that gets no chunk or mark for 60 s (the page
  is gone or stuck), so its file is not left open until the app closes.

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
- Finishing: `close()` returns the frame count and the fingerprint, computed
  while writing (SHA-256 over `wavinfo.fingerprint_prefix` + the PCM, the same
  as `wavinfo.wav_fingerprint`). `publish()` renames without ever replacing a
  file (`os.rename` on Windows refuses; the next free `"<stem> (N)"` is used).
  The fingerprint goes into the library's index (`remember_fp`), so listing
  never reads the file again.
- Names: `Live YYYY-MM-DD HH-MM-SS.wav`; imports
  `Import YYYY-MM-DD HH-MM-SS (n).wav` (the time Record was pressed, n the
  piece) and, when split into more than one piece, the whole input as
  `Import YYYY-MM-DD HH-MM-SS (full).wav`.
- Limits: recording stops (and saves) when the drive would have less than
  500 MB free, warning from 15 minutes before (an import that keeps the whole
  input counts twice the data rate); and at 4 GiB - 2 MiB of audio per file
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
the millisecond: the store refuses a mark past the end). In an import, a mark
goes to the piece being written (during a gap that is still the piece before
it) and to the whole input's file.

## Splitting an import on silence (`openevp/silence.py`)

50 ms blocks, RMS in dBFS over all channels. **Nothing is discarded**: the
pieces, put back together, are the input sample for sample.

- **Idle floor:** the 20th percentile of the last second, the lowest seen so far
  (never rises), with -100 dBFS as the lowest floor used (all-zero input).
  `threshold = floor + 8 dB`. The cable's hiss is the floor; a recording's own
  background (room tone, the recorder's mic hiss, played back) is above it.
- **Pieces:** the first starts with the first sample. After `gap` seconds
  (default 3, 0.5-60, or off) of blocks at or under the threshold, a split is
  pending; at the next loud block the piece ends and the next starts. The last
  0.5 s of every quiet run is always held back before it is written (a rolling
  pre-roll), so the next piece begins 0.5 s before its sound even when the gap
  is exactly `gap` seconds (Astra's re-review: it started at the sound). Stop:
  whatever is held back goes into the last piece, and so does the last partial
  block.
- **Evidence before a split** (both reviews: changing loudness was taken for a
  background, and split recordings in two). A piece may end only when:
  - it has a background of its own: a level it holds **steadily** for whole
    seconds (the 10th-90th percentile of a second's block levels within 6 dB),
    in **at least two separate stretches** (the level comes back), the quietest
    such level at least 8 dB above the floor. A quieter passage heard once, or
    loudness that just changes, is not a background;
  - its content stands out from that background (95th percentile of its blocks
    at least 8 dB over it);
  - the quiet run is back at the idle floor seen before (its median within
    4 dB): the input returned to the level it had while the recorder played
    nothing;
  - at least 3 s of the piece were judged, not counting the quiet run being
    judged, nor quiet before the piece's first sound (the gap before it);
  - it has at least 1 s of loud blocks (a click joins the next piece).
  Otherwise the quiet becomes part of the piece. When Play was pressed before
  Record, the first recording's background was the floor at first, so it shows
  none of its own and stays joined to the next recording.
- **The whole input is kept too** (decision for the review's suggestion): an
  import split on silence also writes `Import ... (full).wav`, with the marks,
  so a split in the wrong place costs nothing. It is dropped when only one
  piece was made (the same audio). It costs twice the disk space while
  importing, which the disk check counts.

## While recording

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
- If a saved Live recording cannot be opened in the player, Stop's result says
  why (`player_error`) and the page shows it; the file is saved either way.

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
  - the splitter on synthetic signals, every case checking that the pieces put
    together are the input: tones with gaps, hiss only, pauses shorter than the
    gap, a recording no louder than the floor, Astra's steady-sound case, Play
    before Record, clicks, Stop in a gap, stereo, digital silence;
  - the backend: placement, names, marks after finishing (also at the last
    sample), the player result and its failure, Clips refused, import pieces,
    the whole input and their marks, the disk-space and 4 GB stops, a second
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
  failure, Import with its settings, guide and whole-input message, a
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
- The import splitter errs towards not splitting: recordings with no quieter
  background of their own (steady sound) stay joined to the next one.
