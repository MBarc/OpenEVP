# OpenEVP user guide

Everything OpenEVP can do, step by step. New to it? The
[Quick start](../README.md#quick-start) gets you going in four steps; come back
here when you want to know more.

**Contents**

- [Installing](#installing)
- [Getting recordings off your recorder](#getting-recordings-off-your-recorder)
- [Troubleshooting](#troubleshooting)
- [Marking EVPs](#marking-evps)
- [Opening WAV and MP3 files](#opening-wav-and-mp3-files)
- [The EVP Library](#the-evp-library)
  - [Folders](#folders)
  - [Right-click menu](#right-click-menu)
  - [Moving recordings](#moving-recordings)
  - [Backups](#backups)
  - [Export WAV with marks](#export-wav-with-marks)
- [The player's settings](#the-players-settings)
- [Playback speed](#playback-speed)
- [Enhance](#enhance)
- [Noise reduction](#noise-reduction)
- [Spectrogram](#spectrogram)
- [EVP clips](#evp-clips)
- [Where your marks live](#where-your-marks-live)
- [Known limits](#known-limits)

## Installing

You only do this once per PC.

1. Download `OpenEVP-Setup-<version>.exe` from the
   [Releases](https://github.com/MBarc/OpenEVP/releases/latest) page and run it.
2. Windows may say *"Windows protected your PC"* the first time, because the
   installer isn't code-signed. Click **More info → Run anyway**.
3. Windows asks for permission once; click **Yes**. That's all.

The installer sets up everything:

- the desktop app and the command-line tool;
- the recorder's driver. It works whether or not the recorder is plugged in;
  Windows applies it when the recorder is plugged into any USB port. (How
  that works is in the [technical documentation](technical.md#driver-setup).)
- Microsoft's WebView2 runtime, if this PC doesn't have it yet (Windows 11
  already does). On an older Windows 10 PC without WebView2, the installer
  downloads it, so that PC needs internet during setup.

**What you need:** 64-bit (x64) Windows 10 (version 2004 or later) or
Windows 11. Windows in "S mode" can't run it, and ARM-based PCs aren't
supported yet.

**Uninstalling:** **Settings → Apps → OpenEVP** removes the app, the driver
package and the certificate the installer made, which puts the PC back as it
was.

## Getting recordings off your recorder

1. Plug in the recorder and open **OpenEVP** from the Start menu.
2. Click the recorder on the left. Its folders (A–E) and recordings appear.
3. Tick the recordings you want, or the box at the top of the list for all of
   them, and click **Export**.
   - By default they go to `Documents\OpenEVP`, one subfolder per recorder
     folder (created by the first export). Click the path to open it, or
     **Change…** to pick another folder.
   - A recording is skipped if a file with identical audio is already there.
   - **Existing files are never overwritten.** A different recording with the
     same name is saved as `... (2).dvf`.
4. To get WAV files instead of Sony's `.dvf` files, pick **WAV** as the format
   before clicking **Export**. For an ICD-ST25 that's 8000 Hz, 16-bit mono,
   the same file Sony's Digital Voice Editor writes.

Click a recording, or a saved `.dvf` or `.wav` file, to play it and see its
waveform.

Nothing on the recorder is ever changed or deleted: OpenEVP only reads from
it.

## Troubleshooting

### The recorder shows as "needs setup"

This can happen, for example, after its driver was removed. Click **Set up
recorder** in the app. It runs the same driver step as the installer.

### It says the recorder may be stuck

If a transfer is interrupted, the recorder gives up on it and keeps answering
"busy". **Unplug the USB cable, wait a few seconds, plug it back in**, and try
again. Recordings already saved are skipped.

## Marking EVPs

While a recording is loaded in the player at the bottom of the window:

1. Drag across the waveform to select the part you want to mark.
2. Press **M** (or click **★ Mark EVP**).
3. Pick a class, type a note on what you hear, and click **Save**:
   - **A**: clear, anyone hears the words;
   - **B**: fairly clear, most people agree on the words;
   - **C**: faint, hard to make out.

The mark appears as a coloured band on the waveform and in the list below it.
From the list you can play it (▶), edit its class or note (✎) or delete it
(✕), and you can drag its edges to adjust it. Click a note to edit it in
place. Marks save themselves as you make them.

Tick **Reviewed** once you've listened all the way through a recording.

A mark belongs to the recording's audio, not to one file. If the same audio
exists as a `.dvf` and a WAV, or has been saved more than once, marking it
anywhere marks it everywhere.

## Opening WAV and MP3 files

**Open audio file…** plays a WAV or an MP3 from anywhere on the PC.

MP3 files are full recordings, just like WAVs: they play with the same
waveform, zoom, loop and speed, they can be marked, and **Export WAV with
marks** and **Export clips** work on them (Export WAV with marks writes a WAV).

That includes MP3s saved under another extension, such as the `.mpeg` files
WhatsApp Web saves voice notes and shared clips as. OpenEVP reads `.mp3`,
`.mpeg`, `.mpga`, `.mp2` and `.m2a` files, but a file only counts if its first
bytes really are MPEG audio, so an `.mpeg` video is ignored.

Marks belong to the audio, so renaming `x.mp3` to `x.mpeg` keeps them. An
MP3's own tags (title, comment) are never read as marks.

## The EVP Library

Click **EVP Library** in the sidebar to see every recording saved to disk.
By default that's your Save-to folder; click the library's folder name to
point it somewhere else, such as a shared drive.

Copies of the same recording (a `.dvf` and its WAV, or a WAV saved twice) are
grouped into one row. Each row shows:

- its investigation (the subfolder it came from);
- its type and length;
- how many class A, B and C EVPs it has;
- whether it's been reviewed.

Click a row to play it, the triangle to see its marks, or a mark to jump
straight to it. Filter with the **All / Has EVPs / A / B / C / Not reviewed**
buttons above the list, or search by file name, investigation (folder) name,
or note.

### Folders

The library shows folders like File Explorer: a breadcrumb path at the top,
subfolders first, then that folder's recordings.

- Click a folder row to select it. Double-click it, or press **Enter** while
  it's selected, to open it.
- Click a step in the breadcrumb to jump back up, or press **Backspace** to go
  up one level.
- Folders can nest as deep as you like.
- Turn on **All recordings** to switch to a flat, filterable list of every
  recording in the library instead, with no folders.

**New folder** creates a folder inside the one you're viewing. The name box
starts empty (the investigation may have been days ago): type a name and click
**Create**. Select a folder to **Rename** it or **Delete** it.

**Delete** asks first, listing what's inside: how many recordings, and how
many have EVPs (or that not all have been checked yet); any recorder backups
that would go with it (those recordings get **Retry backup** afterwards);
other files such as photos or video; subfolders; and the total size. If it's
your Save-to folder, the confirmation says so, since exporting there will just
create it again.

The folder goes to the **Recycle Bin**. OpenEVP refuses up front, and says to
use File Explorer instead, when it can tell Windows wouldn't recycle it:

- on a drive with no Recycle Bin (such as some network or external drives);
- when the Recycle Bin is set to delete files immediately;
- or, when Windows reports the Recycle Bin's size, when the folder is bigger
  than that.

If Windows still finds it can't recycle something, OpenEVP asks before
deleting anything for good. If a file in the folder is in use, what could be
recycled is, the rest stays where it was, and OpenEVP says so.

### Right-click menu

Right-click in the library for the same tools:

- on an empty part of the list: **New folder**;
- on a folder: **Open**, **Rename** or **Delete**;
- on a recording: **Play**, **Rename…**, **Move to…** (all the ticked
  recordings, if you right-click a ticked one) or **Export clips**.

A folder's **Export clips** does every marked recording in it and in its
subfolders (see [EVP clips](#evp-clips)).

**Show in File Explorer** (on a recording or clip) opens its folder in File
Explorer with the file selected: the file the row names, so the `.dvf` when
there's also a `.wav` copy. **Open in File Explorer** (on a folder, including
a `Clips` folder) opens that folder.

**Rename…** (or **F2**) renames a recording: its `.dvf` and `.wav` in that
folder get the new name, each keeping its extension. A name that's already
taken is refused, and the marks stay with the recording.

A second OpenEVP window can't create, rename, delete or move anything in the
library, and can't export; do those in the first window.

### Moving recordings

Move recordings between folders by ticking their checkboxes and clicking
**Move to…**, or by dragging a row onto a folder or a breadcrumb step. Marks
travel with the audio automatically: nothing about a recording's marks changes
when it moves. The **Investigation** column always shows the top-level folder
a recording sits in, however deep it's nested.

Recorder exports still go to the separate **Save to** folder; folders in the
library just organise what's already there.

### Backups

The first time you mark a recording that's still on the recorder, OpenEVP
saves a copy of it (the `.dvf`, and a WAV if conversion is available) to the
Save-to folder in the background, so the mark isn't lost if the recorder is
later wiped.

A line under the marks list shows when it's done. If the backup failed, or
couldn't start because the app was closing or updating, the line says it
isn't backed up yet and offers **Retry backup**.

### Export WAV with marks

**Export WAV with marks** saves a WAV copy of the loaded recording, with its
marks written in, to the Save-to folder: into the recorder folder's letter, or
the investigation's folder for a file in the library.

The marks are standard RIFF `cue`/`labl`/`ltxt` WAV markers, the marker format
many audio editors can read. None has been verified with OpenEVP yet.

## The player's settings

To the right of the waveform, three small tabs hold the player's settings:

- **View:** Zoom, Height, Spectrogram;
- **Speed:** speed, Keep pitch, Exports at …×;
- **Enhance:** the listening aids, Reduce noise, Exports enhanced.

Click a tab, or use the arrow keys on it (**Home** and **End** for the first
and last). The last one you picked is shown again next time.

A tab with something changed from its default shows a dot, and its tooltip
says what, so a setting is never hidden behind another tab:

- **Speed •** while the speed isn't 1×;
- **Enhance •** while anything there is on;
- **View •** with the spectrogram off, the height raised or the waveform
  zoomed in past fit-to-width.

The keyboard shortcuts (`[` `]` `\`, **M**, **Space**) work whichever tab is
shown.

## Playback speed

**Speed**, on the Speed tab, plays at 0.25× to 2×: `[` is slower, `]` is
faster, and `\` or a double-click goes back to 1×.

With **Keep pitch** ticked (the default), speech slows down at its normal
pitch. Untick it to hear it like a tape, deeper when slower. The speed and
Keep pitch are remembered.

While the speed isn't 1×, **Exports at 0.5×** (or whatever the speed is)
makes the player's **Export WAV with marks**, **Export clips** and **Save
clip** save at that speed, the same way it plays:

- It's ticked when you move the speed off 1×, but not after a restart, even
  though the speed itself is remembered. Untick it for normal-speed exports.
- The file names end in the speed (`…_0.5x.wav`,
  `…_EVP-A_00m12.4s_0.5x.mp3`, or `…_0.5x-tape.wav` with Keep pitch off).
- The marks are moved to match, and a clip's 0.5 s on each side is slowed down
  with it.
- The library's **Export clips** is always at normal speed.
- Slowing down a whole recording with Keep pitch takes a few seconds (a
  progress bar shows how far it is).

## Enhance

The **Enhance** tab holds the listening aids. They change only what you hear,
live while it plays; the recording itself and its marks are never touched.

- **Boost:** up to +24 dB louder. A soft limiter keeps it from clipping.
- **Leveler** (Light, Medium, Strong): a compressor that brings quiet parts up
  and loud ones down.
- **Voice filter:** keeps the voice band, about 300 to 3400 Hz.
- **Cut rumble:** lowers everything below about 120 Hz (handling noise, wind).
- **Cut hiss:** lowers everything above 5 kHz. An ICD-ST25 recording (8 kHz)
  has nothing up there, so it's greyed out for those.
- **Hum remover:** notches out 60 Hz (or 50 Hz) mains hum and its next three
  harmonics.
- **Reset** turns everything off.

The settings are remembered. While any of them is on, the tab reads
**Enhance •** and an **Enhanced** tag sits above the waveform, so it's never
left on unnoticed.

**Exports enhanced** then shows on the tab. Like **Exports at 0.5×**, it's
ticked only when you turn enhancement on in this session. It makes the
player's **Export WAV with marks**, **Export clips** and **Save clip** save
what you hear, named `…_enhanced` (`…_0.5x_enhanced.wav` with a speed: the
speed is applied first, then the enhancements, as the player does). The
library's **Export clips** always saves as recorded. Saving the same thing
again gives identical files, so it says "already saved".

An enhanced export matches what you hear to within rounding. It can differ
very slightly, because the player works at your sound card's rate and its
Leveler delays the sound by 6 ms, while an export keeps every position so its
marks stay put. The details are in the
[technical documentation](technical.md#enhance-player-and-export).

## Noise reduction

To take steady background noise down (hiss, fans, air conditioning,
traffic):

1. Drag across a stretch with **only** that noise in it: no voices, at least a
   quarter of a second (a second or two is better).
2. Click **Learn noise** under the waveform.
3. Tick **Reduce noise** on the **Enhance** tab. Hover it to see where the
   noise was learnt from.

OpenEVP makes a noise-reduced copy of the recording's audio (a progress bar
with **Cancel** shows while it does; a 30-minute ICD-ST25 recording takes about
5 seconds), and the player switches to it where it was, playing on. Its
**amount** sets how far the noise goes down: 40% (the default) lowers it by
12 dB, 100% by 30 dB.

**Keep it low.** Strong noise reduction leaves watery, warbling artefacts, and
those can sound like whispers or voices. An "EVP" heard only with Reduce noise
on should be checked with it off.

The copy has exactly the recording's length and sample rate, so marks,
selections, loops, the speed and the other enhancements all work on it as
usual, and marks stay the recording's own. The copy is never fingerprinted,
never listed in the EVP Library, and lives only in OpenEVP's temporary cache.

The noise profile is kept per recording until OpenEVP closes; loading another
recording turns Reduce noise off. **Exports enhanced** includes it (named
`…_enhanced`): noise reduction is applied first, then the speed, then the other
enhancements, as the player does.

How the noise reduction works is in the
[technical documentation](technical.md#noise-reduction).

## Spectrogram

**Spectrogram** (on the **View** tab) is on unless you untick it. It shows
under the waveform: time across, pitch up (0 Hz at the bottom, labelled in
kHz), and loudness as colour, from black through purple and red to pale
yellow.

A voice shows as stacked bright bands (its harmonics) shaped by its formants,
which is often easier to spot than in the waveform, even under noise.

- It scrolls and zooms with the waveform, and the cursor, the marks and a drag
  selection cover it too.
- Untick it and it stays off (remembered); tick it again any time.
- Opening a recording is never slowed down by it: the waveform comes first,
  and the spectrogram fills in a moment later.
- A 30-minute ICD-ST25 recording takes under 2 seconds and about 60 MB.
- MP3 recordings and clips get one too.

How it's computed is in the [technical documentation](technical.md#spectrogram).

## EVP clips

**Export clips** (next to Export WAV with marks) saves every mark of the
loaded recording as its own short clip; **Save clip** in a mark's row saves
just that one.

The **Clip format** menu next to Export clips picks **MP3 (for sharing)**, the
default, or **WAV (full quality)**. It's remembered, and the library's Export
clips uses it too.

In the library, right-click a recording, or a folder (every marked recording
in it and its subfolders), and choose **Export clips**. A folder runs in the
background with a progress bar and **Cancel**. Recordings that can't be
decoded (no decoder, an ICD-ST10 mode this build can't play, or damaged) are
skipped and listed with the reason. When it's done, the banner says how many
clips were saved (and how many were already there), with **Open folder**.

**What a clip contains**

- Each clip is the mark plus **0.5 s** on each side (less at the very start or
  end of the recording), cut from the decoded audio in its own format: an
  ICD-ST25 recording stays 8 kHz mono, an ICD-ST10 one 44.1 kHz stereo (ST) or
  16 kHz mono (SP). Nothing is resampled.
- MP3 clips are 128 kbps CBR at the recording's own sample rate. (LAME allows
  at most 64 kbps at 8 kHz, so ICD-ST25 clips are 64 kbps.) The mark (class,
  time and note) is the ID3 title and the note is the comment. A WAV from
  elsewhere at a rate MP3 doesn't have (say 96 kHz) is resampled by the
  encoder to the nearest MP3 rate; mono and stereo only.
- In a WAV clip, the mark's class and note are written in as a WAV marker, as
  in a WAV with marks.

**Where clips go**

- Clips go into a **`Clips`** folder inside the folder Export WAV with marks
  uses (for example `Save to\A\Clips` or `Save to\Old Mill\Clips`).
- They're named `<recording>_EVP-<class>_<MMmSS.s>s[_<note>].mp3` (or `.wav`),
  for example `001_A_003_EVP-A_00m12.4s.mp3`. The note is shortened, and
  characters Windows doesn't allow in file names are left out.
- Nothing is overwritten: a clip already saved with the same bytes counts as
  "already there"; a different one gets a numbered name ("… (2).wav").

**Clips in the library**

- The EVP Library lists the `Clips` folders OpenEVP creates, with a film icon
  and a **Clips** tag. Open one to see its clips; click a clip to play it.
- A clip is an EVP already, so it never counts as one: its marker isn't read
  as a mark, and clips are left out of **Has EVPs** (and the other filters),
  the folder counts, the library total and the EVP chips. **All recordings**
  leaves them out too (they're copies of parts of recordings).
- You can still mark a clip in the player; the mark is kept, but it doesn't
  count either.
- MP3 clips are clips exactly like WAV clips: listed, played and markable,
  never counted.

**Clips from a clip**

Load a clip (WAV or MP3), mark the part you want and use **Export clips** or
**Save clip**, or right-click the clip in the library and choose **Export
clips**. The new clips go into the same `Clips` folder as the clip they were
cut from (never a `Clips` folder inside it), named after it, for example
`001_A_003_EVP-A_00m12.4s_EVP-A_00m00.8s.mp3`. A folder's Export clips (and
Export clips on a `Clips` folder itself) never cuts clips from clips.

**Managing `Clips` folders**

- Rename or delete a `Clips` folder like any other folder (delete goes to the
  Recycle Bin). Renaming, moving or deleting a folder takes its `Clips` folder
  along.
- Recordings can't be moved into a `Clips` folder: it's for clips only.
- Clips (WAV or MP3) can be moved out of it, and then they're ordinary
  recordings. An MP3 anywhere else in the library is a recording.
- OpenEVP recognises its own `Clips` folders by a small hidden file inside,
  `.openevp-clips`. Delete that file and its clips (WAV or MP3) count as
  ordinary recordings. A folder you named `Clips` yourself is an ordinary
  folder.

## Where your marks live

Marks and settings (including the library folder) are stored on this PC in
`%APPDATA%\OpenEVP\`:

- `marks.json`: your EVP marks;
- `settings.json`: your settings;
- `index.json`: an internal cache of files already checked.

Only one OpenEVP window writes at a time. A second one open at the same time
can still browse and play, but can't add, edit or delete marks (it says so)
until the first is closed.

## Known limits

- **What's been verified:** natively on Linux against one ICD-ST25, with 20
  LP-mode recordings in folder A (owner set, dated and undated) and folders
  B–E empty. On Windows 11 the installer set up the driver on its own and the
  app listed all 20 recordings from the same recorder (2026-09-25).
- **Folders B–E:** up to v0.8.2 the download command always named folder A, so
  recordings in B–E were reported and not saved (never saved wrong). It now
  names the folder, as Digital Voice Editor does. This has been checked on an
  ICD-ST10 (folders B and C), not yet on an ICD-ST25.
- **ICD-ST25: LP mode only.** Only LP recordings have been checked. The folder
  table names each recording's mode; a mode OpenEVP doesn't know is reported
  and not saved. SP would be caught this way if SP uses a different mode byte
  (unverified), so **record in LP**.
- **ICD-ST10:** verified on one recorder with three recordings (2026-09-29).
  - Its recordings have no owner name, and no date while its clock isn't set,
    so their files are named like `001_A_001_Unknown.dvf`.
  - The length listed before a recording is decoded counts its frames: it's
    exact for a recording made in one go, and each restart inside a recording
    adds about 0.09 s. The decoded length, shown in the library, is exact.
  - Its flash size isn't known; a recording longer than 32 MB (about an hour
    and a half of LPEC ST) would be reported, not saved.
  - Recordings made with its clock set are untested.
  - Its SP mode's `.dvf` header is OpenEVP's own guess (the LP header with SP's
    codec, channel and rate fields) until a DVE-saved SP file can be compared.
  - An SP recording's length before it's downloaded is its size over 2000
    bytes a second, which can be off by a few hundredths of a second. Once
    saved, its length is counted from its frames and is exact.
- **Long ICD-ST10 recordings are big:** 92 minutes of 44.1 kHz stereo is
  about 930 MB of WAV.
  - Playback decodes straight into a disk cache (2 GB; the oldest recordings
    are dropped first, room is made before a decode and again if the disk
    fills up; a cache left behind by a crash is deleted at the next start).
  - The library fingerprints recordings without holding their audio, but a
    WAV export or a marked backup holds one decoded recording in memory while
    it's saved.
  - The player draws the waveform from the audio itself only for the first
    2.7 minutes' worth of stereo samples (30 minutes of ICD-ST25 audio);
    longer recordings are drawn from 400 peaks per second.
  - Decoding takes about 1.4 s of CPU per minute of audio (about two minutes
    for the longest recording).
- An ICD-ST10 shows as an ICD-ST25 until you open it, and uses the same
  driver.
- Messages in table slots 64 and above rely on an assumed layout. Each message
  is cross-checked against its downloaded data, so a wrong assumption stops
  the download rather than writing a bad file.
