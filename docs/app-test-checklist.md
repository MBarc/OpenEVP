# Desktop app: hardware checklist

Run on Windows with the ICD-ST25 bound to WinUSB (Zadig until the installer exists).
Record date, app version and result for each line.

1. Start the app with the recorder unplugged: "No recorder connected".
2. Plug it in: within about 2 s it appears as "Sony ICD-ST25 #1 (port …)".
3. Click it: folders A–E appear with counts; folder A lists 20 recordings with the
   same dates and lengths as `openevp-cli --list`.
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
    "undated" (clock not set) and lengths matching `openevp-cli --list`; WAV
    is available in the export menu.
16. Export as .dvf: the files are saved (`001_A_001_Unknown.dvf`...); exporting
    again says they are already there.
17. Click a recording: it plays in stereo; zooming in on a short one shows the
    left channel above the line and the right below.
18. Mark it: the backup saves the .dvf and a WAV with the mark. Export an
    unmarked one as WAV: it matches `openevp-cli --wav` byte for byte.
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
    clips is greyed out on the `Clips` folder; Export clips on the investigation
    cuts nothing from the clips. Clips from a clip: with the marked clip loaded,
    **Export clips** and **Save clip** save `<clip>_EVP-…` into that same `Clips`
    folder (no `Clips\Clips`); right-click the clip → **Export clips** does the
    same, for a WAV and an MP3 clip. The new clips count nothing either. Moving a recording into `Clips`
    is not offered (and dragging onto it does nothing). The `Clips` folder holds a
    hidden `.openevp-clips` file (Explorer: show hidden items). Renaming the
    investigation folder keeps its `Clips` folder; rename `Clips` itself (it keeps
    its icon), then delete it: the dialog counts its clips, and it goes to the
    Recycle Bin. Delete `.openevp-clips`: its clips count as recordings.
    MP3 clips: listed with **Clip**, play on a click (waveform drawn, zoom works),
    can be marked (not counted), and move like WAV clips (out of `Clips` they are
    recordings). An `.mp3` elsewhere in the library is a recording: listed, indexed,
    plays, marks count; Save with marks writes a WAV, Export clips cuts from it.
    A WhatsApp `.mpeg` (an MP3) is listed and plays; an `.mpeg` video is not listed.
    **Open audio file…** opens a WAV, an `.mp3` or a WhatsApp `.mpeg`.

Looping a saved EVP:

25. On a recording with two marks, click one on the waveform: it plays once, as
    before, and the bar under the waveform shows `EVP <class> · <start> – <end>`
    with **▶ Play**, **Loop** and **Deselect** (no **Mark EVP**); its row in the
    marks list is highlighted. Tick **Loop**: it repeats without a gap you would
    notice. Drag one of its edges while it loops: the next pass uses the new
    edge. Space pauses it; untick Loop and it stops at the mark's end. Click
    elsewhere on the waveform (or **Deselect**): the loop stops.
26. Click a row's 🔁: that mark loops and the button shows as on (Tab reaches it,
    Enter and Space work); click it again: the loop stops at the mark's end. 🔁
    on the other row moves the loop there. Drag a new selection while a mark
    loops: the mark's loop stops and the bar shows the selection. Delete the
    looping mark, or open another recording: nothing loops on. An imported point
    marker's 🔁 is greyed out. In a second OpenEVP window (read-only), looping
    works the same.

Playback speed:

27. On the **Speed** tab: **Speed 1×** and **Keep pitch** (ticked). Play
    a recording with speech and drag Speed to 0.5×: it slows down at once, from
    where it was (no restart), and the label turns highlighted; the voice keeps
    its normal pitch. Untick **Keep pitch**: still 0.5×, now deeper, like a slow
    tape. Tick it again. Try 2× both ways. The cursor, the time and the selection's
    and a mark's Loop stay right at 0.5× and 2× (a loop still comes back to its
    start). `]` and `[` step faster and slower, `\` goes back to 1×, and so does a
    double-click on the slider; none of them do anything while typing a note.
    Open another recording, and an MP3 clip: the speed stays. Close and start
    OpenEVP again: speed and Keep pitch are as you left them. In a second OpenEVP
    window, a change applies there for that session only.
28. Set Speed to 0.5×: **Exports at 0.5×** appears under Keep pitch, ticked, and
    the tab reads **Speed •** (it
    is hidden at 1×). On a marked recording, **Export WAV with marks**: a
    progress bar and percentage show while it slows down, and it saves
    `<name>_0.5x.wav`; open it in an audio editor: twice as long, the voice at
    normal pitch to the very last sample, and the EVP markers sit on the EVPs.
    Export again: "already saved". Untick **Keep pitch** and export again:
    `<name>_0.5x-tape.wav`, deeper. **Export clips** and a row's **Save clip**
    save `…_0.5x.mp3` (and `.wav` with WAV picked): each clip is twice as long,
    the 0.5 s around the EVP included. The library's **Export clips** on a folder
    saves normal-speed clips with the usual names. Untick **Exports at 0.5×**:
    the player's exports are at normal speed too. Close and start OpenEVP: the
    speed is still 0.5× but **Exports at 0.5×** is unticked until you move the
    speed to 1× and back. Time Export WAV with marks at 0.5× (Keep pitch)
    on a 30-minute ICD-ST25 recording and a few-minute ICD-ST10 stereo one.

Enhance:

29. Open the **Enhance** tab. Play a recording with speech and drag **Boost** to
    +12 dB: it gets louder at once, without a restart or a click of the cursor,
    and loud parts do not crackle (the limiter). The tab reads **Enhance •** (its
    tooltip lists what is on) and an **Enhanced** tag shows above the waveform.
    One at a time, listen to each: **Leveler** Light / Medium / Strong (quiet
    parts come up), **Voice filter** (thinner, telephone-like), **Cut rumble**
    (handling thumps and wind go), **Hum remover** 60 Hz and 50 Hz on a recording
    with mains hum (the hum goes, the voice stays), **Cut hiss** on an ICD-ST10
    recording (greyed out, with a reason, on an ICD-ST25 one). With each on, set
    Speed to 0.5× and 2×, with and without Keep pitch: speed and pitch behave
    as before; the cursor, a selection's loop and a mark's loop stay right.
    **Reset**: everything off, the dot and the tag gone.
30. Turn Boost on: **Exports enhanced** appears, ticked. Export WAV with marks:
    `<name>_enhanced.wav`, which sounds like what you heard; again: "already
    saved". With Speed 0.5×: `<name>_0.5x_enhanced.wav`. Export clips and Save
    clip: `…_enhanced.mp3`. Untick it: plain names. The library's Export clips:
    plain names. Close and start OpenEVP: the settings are still on (**Enhance •**,
    tag shown), **Exports enhanced** unticked. A second OpenEVP window keeps its
    own changes for that session only.

Spectrogram:

31. On a fresh install (or a PC where it was never unticked), **Spectrogram**
    (on the View tab) is ticked. Open a recording: the waveform appears as quickly as
    without it and plays at once; within a couple of seconds the spectrogram fills
    in under the waveform, labelled 1, 2, 3 kHz on
    an ICD-ST25 recording (up to 8 kHz on an ICD-ST10 one); speech shows as
    bright stacked bands. Zoom in with the wheel: it stays lined up with the
    waveform and gets sharper (harmonics visible); scroll along: the labels stay
    at the left edge; the cursor, the marks and a drag selection cover it; a click
    on it seeks. Zoom right out on the longest recording (30 minutes): no freeze,
    the whole length drawn. Open another recording: its own spectrogram; an MP3
    recording or clip has one too. Untick it: gone, the player its usual height. Close
    and start OpenEVP: still off, and opening recordings asks for none. Tick it
    again: it is back, and stays on after a restart.

Noise reduction:

32. On a recording with steady background noise, drag across a second of noise
    only and click **Learn noise**: a banner says it was learnt (with an
    **Enhance** button that opens the Enhance tab). Hovering the Reduce noise
    line says where it was learnt from. Tick **Reduce noise** while playing: a progress bar with
    **Cancel** shows, then playback goes on from where it was, quieter between
    words; the voices sound the same. Hover Reduce noise and its slider: both
    warn about watery artefacts. Move the amount to 100%: it is made again and
    sounds watery (that is the warning); back to 40%. Marks, a selection's loop,
    a mark's loop, the speed (0.5×, Keep pitch on and off) and the other
    enhancements all work on it; the spectrogram shows the noise-reduced audio.
    Add a mark while it is on: the mark is listed under the recording in the
    EVP Library (not under anything new); the library shows no new file.
    Untick it: the original plays on from where it was.
33. On the 30-minute recording, time Reduce noise; Cancel halfway: it stops,
    and Reduce noise is unticked. Load another recording: Reduce noise is off and
    greyed (no profile); go back: the profile is still there. An MP3
    recording and an MP3 clip: Learn noise, Reduce noise, the spectrogram,
    Enhance and exports at a speed or enhanced all work as on a WAV.
34. With Reduce noise on, **Exports enhanced** is ticked: Export WAV with marks
    saves `<name>_enhanced.wav` that sounds like what you heard; with Speed 0.5×
    and Boost too: `<name>_0.5x_enhanced.wav`; Save clip: `…_enhanced.mp3`.
    Export again: "already saved".

Player settings tabs:

35. Right of the waveform: **View**, **Speed** and **Enhance** tabs, no taller than
    the old column of controls (the player is not taller than before). Click each:
    View has Zoom, Height, Spectrogram; Speed has the speed, Keep pitch and
    "Exports at …×"; Enhance has Boost, Leveler, the filters, Hum, Reduce noise,
    "Exports enhanced" and Reset. The box keeps its size when switching. Tab
    reaches the selected tab only; the arrow keys, Home and End move between
    tabs (with a screen reader: announced as tabs, "selected"). Set Speed to
    0.5× and Boost on, then show View: **Speed •** and **Enhance •** show dots,
    and their tooltips say "On now: Speed 0.5×" / "On now: Boost +6 dB". With
    the View tab shown, `]` `[` `\` change the speed (the Speed tab's dot follows),
    M marks a selection, Space plays and pauses, a mark's 🔁 loops. Untick
    Spectrogram: **View •**. Zoom in (the slider or the mouse wheel over the
    waveform): **View •** with "On now: Zoom …×"; zoom back out to fit the
    whole file: the dot clears. Close and start OpenEVP: the tab you last
    picked is shown.

Show in File Explorer:

36. In the EVP Library, right-click a recording that has both a `.dvf` and a
    `.wav`: **Show in File Explorer** is right under Play (the arrow keys reach
    it). Choose it: File Explorer opens on its folder with the `.dvf` selected.
    Do the same on a WAV, an MP3 and a clip in a `Clips` folder, in a folder whose
    name has spaces, commas and accents (e.g. `Old Mill, night 2 – Grüße`): each
    opens with that file selected. Right-click a folder, a `Clips` folder and the
    library folder's own row: **Open in File Explorer** (under Open) opens it.
    Delete a file in Explorer, then Show in File Explorer on its row: "<name> is
    no longer there. Refresh the list." and no Explorer window.

Drag out and Copy file (by hand: no automated test drives the real mouse):

37. In the EVP Library, drag a WAV row into a **Discord** chat: Discord shows the
    file ready to send, with the same name; send it to yourself and it plays. Do
    the same with a clip (`Clips` folder) and an MP3.
37a. Drag an MP3 saved as `.mpeg` (a WhatsApp Web voice note) into **WhatsApp
    desktop** and send it: it arrives as `<name>.mp3`, plays, and WhatsApp does
    not crash. Same with Copy file + Ctrl+V. The `.mpeg` in the library keeps its
    name and is unchanged; the `.mp3` in `%TEMP%\openevp-share\` is a separate
    copy. Do the same in Discord.
38. Drag a `.dvf` that has **no** `.wav` beside it into **WhatsApp desktop**: the
    status line says "Preparing <name>.dvf to share…" for a moment (keep holding),
    then WhatsApp takes `<name>.mp3`; it plays there. Drag the same row again: it
    starts at once (no Preparing). Drag a `.dvf` that **has** a `.wav` of the same
    recording: the `.wav` is what arrives. Put an unrelated `.wav` named like a
    `.dvf` beside it: dragging the `.dvf` sends `<name>.mp3`, never that `.wav`.
39. Drag a row into **WhatsApp Web** in a browser (Edge or Chrome): the file is
    attached as from File Explorer.
40. Drag a row onto the **desktop** and into an Explorer folder: a copy appears;
    the original is still in the library (refresh it). Hold Shift while dropping
    on a folder on the same drive: still a copy, never a move.
41. Tick three recordings (one `.dvf` without a WAV), drag one of the ticked rows
    into Discord: all three files arrive (the `.dvf` as an MP3). In the **All
    recordings** view rows can be dragged out too.
42. Drag a `.dvf` without a WAV and let go before "Preparing" ends: no drag, and
    the status says "Ready to share: drag it again."; the next drag starts at once.
    Drag a row and press Esc while dragging: nothing is dropped.
43. Still works inside the window: drag a row onto a folder row, and onto a
    breadcrumb step: it moves there, as before (the cursor shows "+" because the
    drag offers copy only; the page moves it). Drag a row out of the window and
    back onto a folder: it moves. Dropped on an empty part of the list: nothing.
44. Right-click a row → **Copy file** (right under Show in File Explorer; two
    ticked rows: **Copy 2 files**), then Ctrl+V in a Discord chat: the file is
    attached. Ctrl+V in an Explorer folder: a copy appears. Focus a row with the
    keyboard (Tab, arrows) and press **Ctrl+C**, then paste: the same. The status
    line says "Copied 1 file. …".
45. Close OpenEVP after sharing a `.dvf` as MP3; the MP3 in
    `%TEMP%\openevp-share\` stays (a chat may still be reading it). Start
    OpenEVP more than 6 hours later: it is gone.

Deleting recordings and clips:

46. In the EVP Library, right-click a recording that has both a `.dvf` and a
    `.wav` with marks → **Delete…** (last in the menu): the dialog lists both
    files, says how many EVP marks they carry, and that they go to the Recycle
    Bin; Cancel has the focus. Confirm: both are in the Recycle Bin (never
    deleted for good), the row and the library count update. Restore them from
    the Recycle Bin and refresh: the recording is back with its marks. Tick
    three recordings and press **Delete** on one of them: the dialog lists all
    three. Type a search that hides one of them and press **Delete** again: only
    the two in view are listed. Delete a clip in a `Clips` folder the same way. Delete the recording
    playing in the player: the player empties first. Open the `.wav` in another
    program that locks it (or keep it open in Audacity) and delete the
    recording: OpenEVP says the `.dvf` went and the `.wav` is still there, and
    why. In a second OpenEVP window, Delete… is greyed out and the key says why;
    during an export or a backup it is refused.

Record live and Import from a recorder (by hand, in the built app: no automated
test opens a real input):

47. **Real microphone.** Click **Record live**. The first time, no permission
    prompt appears (OpenEVP grants its own page the microphone). The Input list
    names the PC's inputs; the meter moves when you speak. Pick another input,
    close and reopen OpenEVP: the same input is chosen. Record 30 s, speaking
    now and then, press **M** twice: a star appears on the waveform and
    "Marked" shows for a moment each time. Stop opens `Live <date> <time>.wav`
    in the player with two class C marks "Marked while recording, not graded
    yet", each 3 s long and ending about where M was pressed; the library lists
    it in the folder that was picked.
    The file plays in another program (Audacity, VLC) and its sample rate and
    channels match the input (Windows Sound settings, the input's Advanced tab).
48. **Listen.** Listen is off on opening the view, and its panel is hidden. With
    headphones on, tap it: you hear yourself, with a small delay, and the panel
    shows Volume, Even out loud and quiet (Off, Light, Medium, Strong), Clean up
    (Voice only, Cut rumble, Cut hiss), Hum (Off, 60 Hz, 50 Hz) and Reset, in that
    order, all easy to tap. Set Volume to +12 dB: louder in the headphones at
    once; the saved file is not louder (compare its waveform with one recorded
    without). Click EVP Library in the sidebar: the player's Enhance tab shows Boost +12 dB
    too; change it there, go back to Live: the same. With a hum in the input,
    turn on Hum 60 Hz: the hum lines go from the spectrogram at once, the
    waveform is unchanged; turn Listen off: the hum lines come back (the
    spectrogram shows what is heard). There is no Enhance what I hear or Show
    what I hear button. Resize the window while recording: the waveform and
    spectrogram stay.
49. **Windows blocking the microphone.** In Settings, Privacy & security,
    Microphone, turn off "Let desktop apps access your microphone"; open Record
    live: OpenEVP says Windows is blocking it and **Open microphone settings**
    opens that page. Turn it back on, pick the input again: it works.
50. **USB audio adapter with a recorder's headphone output.** RR-DR60 (2.5 mm to
    3.5 mm adapter) or any recorder, into a USB adapter's line or mic input.
    **Import from a recorder**, the guide is shown; volume at about the middle,
    meter out of the red. Record first, then play three recordings back to
    back, then Stop: `Import <date> <time> (full).wav` opens in the player and,
    a moment later, red ✂ marks appear between the recordings, half a second
    before each one's sound, none in the middle of a pause inside a recording.
    Remove one (✕), add one at the play cursor (**✂ Add cut here**), click a mark
    to remove it, then **Split into N recordings**: the parts appear in the
    library, each starting where its cut was. Another import: **Keep as one**
    leaves only the full file. Another: click **Cancel splitting** while it
    splits: only the full file remains, and the banner says so. Untick
    **Suggest cuts**: one plain file, no suggestions.
51. **An hour-long session.** Record live for 60+ minutes at 48 kHz: the
    waveform and spectrogram keep scrolling smoothly to the end; Task Manager
    shows OpenEVP's (and its WebView2 processes') CPU low and memory flat after
    the first minutes. Marks made near the end land in the right place. Stop:
    the file opens within a few seconds and is about 690 MB (stereo).
52. **Pulling the plug.** Record live for a few minutes, press M once, then cut
    the power (or end OpenEVP.exe and msedgewebview2.exe in Task Manager). The
    folder holds `Live <…>.wav.part`, which already plays in VLC. Start
    OpenEVP: it says it saved the cut-off recording; the `.part` is now
    `Live <…>.wav`, missing at most the last ~5 seconds, with its mark. Unplug
    the USB adapter in the middle of a recording: it stops, says the input
    stopped, and the recording is saved.
53. **Disk nearly full.** On a USB stick with a little over 500 MB free,
    record into a library folder on it: a warning appears when less than 15
    minutes fit, then it stops by itself and saves; at least 500 MB stay free.
54. **While recording**, clicking EVP Library in the sidebar asks "Stop recording
    and save it?" (big buttons): Keep recording goes on recording; Stop and save
    saves it, then shows the library. Not recording, the same click leaves at once
    and the microphone is let go (Windows' microphone icon goes). Closing the window asks
    first (and saves the recording if you close), Check for updates won't
    install, and a second OpenEVP window cannot record (it says why).
55. **Closing mid-recording.** Record live for a minute, speak right before
    closing the window, answer the prompt: the window closes within a few
    seconds and the saved file ends with what you said (nothing cut off). Try
    renaming the recording's folder in File Explorer during a recording: Windows
    refuses; after Stop it works.
56. **Import, whole input.** After a split, the full file is as long as the
    parts together and has the marks made while importing; each part has the
    marks that fall in it; playing the parts back to back sounds exactly like the
    full one. Import an hour from a recorder: the suggestions appear within a few
    seconds of Stop, and the split within a minute or so.
57. **Closing during a split.** Start a split of a long import and close
    OpenEVP: it closes within a few seconds; the full file is there, no
    half-written parts are left (after the next start), and nothing hangs in
    Task Manager.
58. **Touch screen in the field.** On a tablet or touchscreen laptop, open Record
    live: every button and toggle is easy to hit with a finger; MARK is bottom
    right under the thumb. There is no Night screen button on this screen; turn
    on **Night screen** in the top bar: the whole app goes dark red, this screen
    included; reopen OpenEVP: it is still on, and the window opens dark with no
    white flash. "about N h left" shows before Record. Turn Listen on, close the
    screen and open it again: Listen is off and its options are hidden. Rotate or resize the window: the
    waveform and spectrogram fill the space and keep their picture. Mute the microphone: within about 3 s the screen says "No sound
    coming in…", the meter says Silent, the waveform shows a flat line and the
    spectrogram a dark band moving in; **Open sound settings** opens Windows'
    sound settings; unmute: the message goes. Shout into the mic: **Too loud**.
59. **Marking in the field.** While recording, tap MARK a few seconds in, then
    again within a second: two stars, the second a row lower, and "Marked" each
    time; there is no list, class or note on this screen. Stop: in the player
    each mark is class C, "Marked while recording, not graded yet", 3 s long;
    grade one in the EVP Library. A mark in the first second is shorter (it
    starts at 0:00). "about N h left" in the status strip matches the free space
    on the drive less 500 MB, at the input's rate (94 GB free at 48 kHz stereo:
    about 135 h), not the 6 h a 4 GB file holds.
60. **Night screen, app wide.** Tick **Night screen** in the top bar: the EVP
    Library, the player, the recording screen, the About and other dialogs,
    the right-click menus, banners and the scrollbars turn dark red on black;
    nothing stays bright white. Play a recording: the waveform is red and the
    spectrogram red on black (scroll and zoom: new tiles come in red too). A, B
    and C marks are told apart (red, amber, rose, with letters). Close and
    reopen OpenEVP: it opens dark at once, with no white flash. Untick it: back
    to the usual colours everywhere, the spectrogram in its usual colours.

