# Desktop app: hardware checklist

Run on Windows with the ICD-ST25 bound to WinUSB (Zadig until the installer exists).
Record date, app version and result for each line.

1. Start the app with the recorder unplugged: "No recorder connected".
2. Plug it in: within about 2 s it appears as "Sony ICD-ST25 #1 (port …)".
3. Click it: folders A–E appear with counts; folder A lists 20 recordings with the
   same dates and lengths as `st25-download --list`.
4. Select all in A, export as .dvf to an empty folder: "20 saved"; the files are
   byte-identical to the CLI's output (compare SHA-256).
5. Export again to the same folder: "0 saved, 20 already there".
6. Quick replug: unplug and replug within 1 s (faster than the 2 s poll). The
   recorder must come back as a fresh connection (no stale folders, and no
   playback of cached audio from before).
7. Pull the cable during an export: the status says "Stopped", the banner gives
   the replug advice. Replug it; export again: the rest are saved, none duplicated.
8. Recorder without WinUSB (for example on another PC): selecting it shows the
   driver advice, not a crash.
9. Close the window during an export: the app asks first; answering yes stops
   after the current recording and the process exits without leaving temp files.
10. Clean Windows 10 VM without the WebView2 Runtime: starting the app shows the
    WebView2 message, not a blank window.

Once the decoder exists (run these on the **built** app, so a decoder missing
from the frozen build is caught):

11. Play A-001: the waveform appears at once (from peaks), seeking and zoom work.
12. Play the longest recording: memory use of the app stays reasonable (Task
    Manager), seeking near the end works without a long wait.
13. Stress: while exporting all of A as WAV, play other recordings. The export
    finishes with no "stuck" errors. Repeat three times.
14. Two recorders at once (when available): both listed; exporting from one while
    browsing the other works.
