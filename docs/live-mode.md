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
prompt every session. `install_when_loaded(window)` adds a handler on the first
`loaded` event (on the GUI thread, via `form.Invoke`):

- kind `Microphone` from the app's own page (same scheme, host and port as the
  page shown; `http`, host `127.0.0.1` or `localhost`) is **allowed**, with
  `SavesInProfile = False`;
- `Microphone` from anything else is **denied**; an exception in the handler
  denies too;
- every other kind is left to WebView2 (unchanged behaviour).

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
       base64 -> Api.live_chunk(session, seq, data), one call in flight at a time
backend (app/live.py): seq checked, base64 decoded, disk space checked,
       -> Splitter (import with split) or straight -> WavPart (.part)
```

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
  the user. It never touches the session's own `.part`, and a read-only store
  (a second window) recovers nothing.
- Finishing: `close()` returns the frame count and the fingerprint, computed
  while writing (SHA-256 over `wavinfo.fingerprint_prefix` + the PCM, the same
  as `wavinfo.wav_fingerprint`). `publish()` renames without ever replacing a
  file (`os.rename` on Windows refuses; the next free `"<stem> (N)"` is used).
  The fingerprint goes into the library's index (`remember_fp`), so listing
  never reads the file again.
- Names: `Live YYYY-MM-DD HH-MM-SS.wav`; imports
  `Import YYYY-MM-DD HH-MM-SS (n).wav` (the time Record was pressed, n the
  piece).
- Limits: recording stops (and saves) when the drive would have less than
  500 MB free, warning from 15 minutes before; and at 4 GiB - 2 MiB of audio
  (RIFF's limit), about 6.2 h of 48 kHz stereo. Start is refused with less than
  500 MB + one minute free.

## Marks

A mark (M) is a region of 2 s ending at the moment M was pressed (shorter at
the very start), class C, note "Marked while recording". The marks store holds
regions with a minimum length (point markers exist only as imports), and a
region shows on the waveform and plays like any other mark; class and note are
edited in the player afterwards. The page sends the time it counted (frames
captured since Record) after flushing its pending audio; the backend keeps the
mark in memory and in the sidecar and stores it against the fingerprint when
the file is finished (clamped to the file's length). In an import, a mark goes
to the piece being written, or to the one that ended less than 2 s before
(stored at once); in a gap it is refused ("waiting for sound").

## Splitting an import on silence (`openevp/silence.py`)

50 ms blocks, RMS in dBFS over all channels.

- **Idle floor:** the 20th percentile of the last second, the lowest seen so far
  (never rises), with -100 dBFS as the lowest floor used (all-zero input).
  `threshold = floor + 8 dB`. The cable's hiss is the floor; a recording's own
  background (room tone, the recorder's mic hiss, played back) is above it.
- **Start:** the first block over the threshold, with 0.5 s of pre-roll.
- **End:** `gap` seconds (default 3, 0.5-60, or off) of blocks at or under the
  threshold; 0.5 s of that quiet is kept, the rest dropped.
- **Never splitting a recording:** a piece only ends when its own level (the
  20th percentile of its blocks so far, excluding the quiet run being judged,
  with at least 3 s of them) is at least 8 dB over the floor. If Play was
  pressed before Record, the first recording's background is the floor, its
  pauses look like gaps, and it is not split; the first real gap lowers the
  floor and splitting works from there. The guide says to press Record first.
- **Clicks:** a piece with under 1 s of loud blocks is discarded.
- Quiet blocks are held in memory only until the gap length; past it (when the
  piece cannot be split) they are written, so memory stays bounded.

## While recording

- One session at a time; it needs the writable store (a second window cannot
  record).
- `_fs_begin` refuses folder operations ("Stop the recording first.") and
  `install_update` refuses; admission is checked under `_workers_lock` on both
  sides (and never with `_lib_lock` inside it: `_list_library` takes them the
  other way round).
- Closing the window asks ("Recording in progress"); `shutdown()` finishes the
  session before closing the store.
- The page greys out the sidebar; the input's track ending (unplugged) stops and
  saves.
- The input remembered is `{id, label}` (setting `live`, with the split and the
  Import toggle). Device ids are per-origin and change between sessions in
  private mode, so the page asks for the remembered id as `ideal`, then
  re-opens by **label** with `exact` if that is a different device.
- Listening is never remembered: off whenever the view opens.

## Smoke test

`OpenEVP.exe --smoke` sets `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` to
`--use-fake-device-for-media-stream --autoplay-policy=no-user-gesture-required`
(only in that process; restored after), then `_smoke_live` runs `liveSmoke(1.5)`
in the page: the default (fake) input recorded through the Record button's own
code, one mark, saved into the throwaway library folder, and checks the WAV
(16-bit, rate and channels as captured, length) and that the mark was stored.
No `--use-fake-ui-for-media-stream`: the app's own permission handler must
answer, or `getUserMedia` waits for an invisible prompt and the check times out.
`tools/release_check.py` fails a report without a good `live` section.

## Tests

- `tests/test_live.py`: the writer (incremental writes, header rewrites, never
  overwriting, the size limit, recovery of a cut-off `.part`), the splitter on
  synthetic signals (tones with gaps, hiss only, pauses shorter than the gap,
  a recording no louder than the floor, Play before Record, clicks, stereo,
  digital silence), the backend (placement, names, marks after finishing, the
  player result, Clips refused, import pieces and their marks, the disk-space
  stop, a second window, folder operations waiting, closing saves, crash
  recovery), the permission decision and the smoke check.
- `tests/ui_check.js`: the view, the input found again by name, raw-input
  constraints, folder choice, Listen with Enhance, record / stop states, the
  chunk bytes, M, the player opening, Import with its settings and guide, a
  backend-initiated stop, Windows blocking the microphone, recovery messages.

## Known limits

- Not yet tried with a real microphone, USB adapter or RR-DR60 (manual
  checklist items 47-54).
- Chromium may resample when the input's rate differs from what the
  AudioContext accepts; the saved rate is the AudioContext's.
- On Stop, the player's audio server reads the whole file once for peaks (a few
  seconds for an hour of 48 kHz stereo).
- A file stops at 4 GB rather than continuing into a second file.
