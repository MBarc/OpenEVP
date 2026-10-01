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

ICD-ST10 (LPEC ST, 44.1 kHz stereo):

15. Plug it in and click it: it is listed as "Sony ICD-ST10"; its recordings show
    "undated" (clock not set) and lengths matching `st25-download --list`; WAV
    is available in the export menu.
16. Export as .dvf: the files are saved (`001_A_001_Unknown.dvf`...); exporting
    again says they are already there.
17. Click a recording: it plays in stereo; zooming in on a short one shows the
    left channel above the line and the right below.
18. Mark it: the backup saves the .dvf and a WAV with the mark. Export an
    unmarked one as WAV: it matches `st25-download --wav` byte for byte.
19. Open the Save-to folder in the EVP Library: the ST10 files are listed with
    their length and marks, and play.
20. ICD-ST10 clips: mark an EVP on an ST recording and one on an SP recording,
    **Export clips** on each: the clips open as 44.1 kHz stereo and 16 kHz mono
    respectively, 1 s plus the mark long. In a build without the SP tables, a
    library folder holding an SP recording lists it as skipped ("can't be
    played"), and it is still listed normally afterwards.

EVP clips:

21. EVP clips: mark two EVPs on A-001 (one with a note containing `?` and `:`),
    click **Export clips**: "2 clips saved"; **Open folder** shows
    `Save to\A\Clips` with two MP3s (the default **Clip format**) named
    `…_EVP-<class>_<MMmSS.s>s[_note].mp3`; they play in Windows' player and show
    the mark as the title and the note as the comment. Switch Clip format to WAV
    and export again: two WAVs named `…_EVP-<class>_<MMmSS.s>s[_note].wav`.
    Each opens in an audio editor (Audacity, say) as 8 kHz mono, with the EVP
    0.5 s in and its label ("EVP A: …") shown as a marker. Export again: "0 clips
    saved (2 already there)". **Save clip** on one row saves only that one.
22. In the EVP Library, right-click a folder holding marked recordings in
    subfolders, plus a damaged `.dvf` → **Export clips**: progress shows, the
    summary counts the clips and names the damaged file as skipped with its
    reason. Start it again on a big folder and click **Cancel**: it stops after
    the current recording. Close the window during one: the app asks first.
23. A second OpenEVP window: Export clips (player and library) is refused with
    the "another OpenEVP is open" reason.
24. After exporting clips into the library: the EVP Library lists the `Clips`
    folder with the 🎞️ icon, a **Clips** tag and "N clips"; the library total,
    "Has EVPs" and the folder counts and chips are unchanged. Open it: each clip
    shows **Clip** in the EVP column and plays on a click, with no marks imported
    (the marks list stays empty). Mark one in the player: the mark stays, but no
    count, chip or "Has EVPs" changes. **All recordings** shows no clips. Export
    clips is greyed out on the `Clips` folder and on a clip; Export clips on the
    investigation cuts nothing from the clips. Moving a recording into `Clips`
    is not offered (and dragging onto it does nothing). The `Clips` folder holds a
    hidden `.openevp-clips` file (Explorer: show hidden items). Renaming the
    investigation folder keeps its `Clips` folder; rename `Clips` itself (it keeps
    its icon), then delete it: the dialog counts its clips, and it goes to the
    Recycle Bin. Delete `.openevp-clips`: its clips count as recordings.
    MP3 clips: listed with **Clip**, play on a click (waveform drawn, zoom works),
    the mark tools are off with "MP3 clips can't be marked…"; Move to… offers only
    other `Clips` folders; an `.mp3` elsewhere in the library is not listed.
