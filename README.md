<p align="center"><img src="assets/logo.svg" width="128" alt="OpenEVP logo: audio level bars shaped like a ghost"></p>

# OpenEVP

Get the recordings off your ghost-hunting voice recorders and onto a modern
Windows PC: one installer, no manufacturer software, no old 32-bit computer.

## Recorders

| Recorder | Status |
|---|---|
| Sony ICD-ST25 | ✅ Supported: download, play, WAV export, EVP marks, EVP clips |
| Sony ICD-ST10 | ✅ Supported (LPEC ST, SP and LP): download, play, WAV export, EVP marks, EVP clips |
| Panasonic RR-DR60 | 🛠 Planned |

Have a recorder you'd like supported? [Open an issue](https://github.com/MBarc/OpenEVP/issues).

Each recorder is one module in `openevp/recorders/`; see
[its README](openevp/recorders/README.md) to add one.

For the ICD-ST25, OpenEVP writes `.dvf` files in the format Sony's *Digital Voice
Editor* (DVE) saves. DVE opens them and converts them to WAV **byte-identically**
to WAVs made from DVE's own downloads (verified on 20 recordings). The `.dvf`
files themselves differ from DVE's only in two time counters DVE rounds
differently, which do not affect the audio.

OpenEVP also exports and plays WAV itself, with its own decoder for Sony's LPEC
format: its WAV files are byte-identical to the ones DVE writes for the same
recordings (checked on the same 20 recordings), so DVE is no longer needed.

The ICD-ST10 uses the same USB ID, driver and protocol as the ICD-ST25, so it is
set up and found the same way; the app shows it as an ICD-ST10 once it has read
it. Its recordings are in another codec, LPEC ST (44.1 kHz stereo), which
OpenEVP decodes with its own LPEC ST decoder
([docs/lpec-st.md](docs/lpec-st.md)): they play, take EVP marks and export as
44.1 kHz stereo WAV files whose samples are byte-identical to those of Sony's
own decoder (checked on three recordings). They are saved as `.dvf` files too,
and `.dvf` files saved by an earlier OpenEVP play without downloading them
again. Their `.dvf` header is OpenEVP's own for now, since no `.dvf` saved by
DVE from an ICD-ST10 has been compared yet.

The ICD-ST10 records in one of three modes, per recording. Its ST mode is LPEC
ST, above. Its LP mode is the ICD-ST25's LPEC LP and plays like an ST25
recording (a real one decoded byte-identically to Sony's decoder). Its SP mode
is LPEC SP (16 kHz mono), the same codec at 16 kHz: OpenEVP's LPEC decoder
plays it and converts it to 16 kHz mono WAV files whose samples are
byte-identical to those of Sony's own decoder (checked on one recording and
on thousands of generated and damaged frames; see [docs/lpec.md](docs/lpec.md)).

Nothing on a recorder is changed or deleted. For the ST25 the program can only
send the exact read commands DVE itself sends to list and download recordings.
Opcode, frame length and arguments are all checked in `st25/policy.py`.

## Why the ST25 needed this

The ICD-ST25 (about 2004) is not a USB drive. It speaks a proprietary protocol
through Sony's `ICDUSB2.sys`, a **32-bit-only** driver that cannot load on 64-bit
Windows. Sony's current *Sound Organizer* cannot talk to it at all. The only
working path used to be a 32-bit Windows VM running DVE. That setup also hits a
multi-core race in Sony's `IcdUsb2.dll`, which cancels USB requests mid-transfer
and locks the recorder up until its cable is replugged.

This tool talks to the recorder directly through the standard, Microsoft-signed
**WinUSB** driver. It is single-threaded, so it has no such race.

## Install (once per PC)

Download `OpenEVP-Setup-<version>.exe` from the
[Releases](https://github.com/MBarc/OpenEVP/releases) page
and run it. Windows asks for permission once; click **Yes**. That's all. The
installer sets up everything:

- the desktop app and the command-line tool;
- the recorder's driver. It works whether or not the recorder is plugged in;
  Windows applies it when the recorder is plugged into any USB port;
- Microsoft's WebView2 runtime, if this PC doesn't have it yet (Windows 11
  already does).

How the driver is set up: the installer binds the recorder (USB ID `054C 0103`)
to Microsoft's own WinUSB driver, which is built into Windows. It uses only tools
that ship with Windows:
1. It writes a small driver description (INF) for this one device.
2. It catalogs and signs that INF with a certificate made on this PC.
3. It trusts the certificate on this PC only, and deletes its private key right
   after signing. The certificate is made fresh on each PC and can't sign other
   certificates.

Uninstalling (**Settings → Apps → OpenEVP**) removes the app, the
driver package and that certificate, which puts the PC back as it was.

Windows may say *"Windows protected your PC"* the first time, because the
installer isn't code-signed. Click **More info → Run anyway**.

Needs 64-bit (x64) Windows 10 (version 2004 or later) or Windows 11; Windows in
"S mode" can't run it, and ARM-based PCs aren't supported yet. On an older Windows 10 PC without WebView2, the installer downloads
it, so that PC needs internet during setup.

## Each time

1. Plug in the recorder and open **OpenEVP** from the Start menu.
2. Click the recorder on the left. Its folders (A–E) and recordings appear.
3. Tick the recordings you want, or the box at the top of the list for all of
   them, and click **Export**. By default they go to `Documents\OpenEVP`, one
   subfolder per recorder folder (created by the first export); click the path to
   open it, or **Change…** to pick another folder. A recording is skipped if a file
   with identical audio is already there, and **existing files are never
   overwritten**. A different recording with the same name is saved as
   `... (2).dvf`.
4. To get WAV files instead, pick **WAV** as the format before clicking
   **Export** (8000 Hz, 16-bit mono, the same file DVE writes). Click a recording,
   or a saved `.dvf` or `.wav` file, to play it and see its waveform.

If a recorder ever shows as **needs setup** (for example after its driver was
removed), click **Set up recorder** in the app. It runs the same driver step as
the installer.

The ST25 command-line tool is installed too (`command-line\openevp-st25.exe` in the
install folder): `openevp-st25.exe [OUTPUT_FOLDER] [--list] [--folder A-E] [--raw] [--wav] [--open]`.
`--wav` also writes a `.wav` beside each `.dvf` (and fills in a missing `.wav`
beside a `.dvf` saved earlier); `--open` shows the folder when done; `--raw` also
keeps the undecoded transfer data, even for recordings it cannot convert.
`openevp-st25.exe --check-wav FILE.dvf` decodes one `.dvf` file to check that WAV
conversion works in this build, without touching the recorder or saving anything.

### If it says the recorder may be stuck

If a transfer is interrupted, the recorder gives up on it and keeps answering
"busy". **Unplug the USB cable, wait a few seconds, plug it back in**, and try
again. Recordings already saved are skipped.

## Marking EVPs

While a recording is loaded in the player at the bottom of the window:

1. Drag across the waveform to select the part you want to mark.
2. Press **M** (or click **★ Mark EVP**).
3. Pick a class — **A**: clear, anyone hears the words; **B**: fairly clear,
   most people agree on the words; **C**: faint, hard to make out — type a
   note on what you hear, and click **Save**.

The mark appears as a colored band on the waveform and in the list below it,
where you can play it (▶), edit its class or note (✎), delete it (✕), or drag
its edges to adjust it. Click a note to edit it in place. Marks save
themselves as you make them.

Tick **Reviewed** once you've listened all the way through a recording.

A mark belongs to the recording's audio, not to one file: if the same audio
exists as a `.dvf` and a WAV, or has been saved more than once, marking it
anywhere marks it everywhere.

**Open audio file…** plays a WAV or an MP3 from anywhere on the PC. MP3 files
are full recordings, like WAVs: they play with the same waveform, zoom, loop
and speed, can be marked, and Save with marks / Export clips work on them
(Save with marks writes a WAV). This includes MP3s saved under another
extension, such as the `.mpeg` files WhatsApp Web saves voice notes and shared
clips as (`.mp3`, `.mpeg`, `.mpga`, `.mp2` and `.m2a` are read; a file counts
only if its first bytes really are MPEG audio, so an `.mpeg` video is ignored).
Marks belong to the audio, so renaming `x.mp3` to `x.mpeg` keeps them. An MP3's
own tags (title, comment) are never read as marks.

## EVP Library

Click **EVP Library** in the sidebar for every recording saved to disk
(default: the Save-to folder above; click its name to point it elsewhere,
such as a shared drive). Copies of the same recording — a `.dvf` and its WAV,
or a WAV saved twice — are grouped into one row, showing its investigation
(the subfolder it came from), type, length, how many class A/B/C EVPs it
has, and whether it's been reviewed. Click a row to play it, the triangle to
see its marks, or a mark to jump straight to it. Filter with the **All / Has
EVPs / A / B / C / Not reviewed** buttons above the list, or search by file
name, investigation (folder) name, or note.

### Folders

The library shows folders like File Explorer: a breadcrumb path at the top,
subfolders first, then that folder's recordings. Click a folder row to select
it; double-click it, or press Enter while it's selected, to open it. Click a
step in the breadcrumb to jump back up, or press Backspace to go up one
level. Folders can nest as deep as you like. Turn on **All recordings** to
switch to a flat, filterable list of every recording in the library
instead, with no folders.

**New folder** creates a folder inside the one you're viewing, with
the name box left empty (the investigation may have been days ago) — type a
name and click Create. Select a folder to **Rename** it, or **Delete** it.
Delete asks first, listing what's inside: how many recordings and how many
have EVPs (or that not all have been checked yet), any recorder backups that
would go with it (those recordings get **Retry backup** afterward), other
files such as photos or video, subfolders, and the total size. If it's your
Save-to folder, the confirmation says so, since exporting there will just
create it again. The folder goes to the Recycle Bin. OpenEVP refuses up
front when it can tell Windows wouldn't recycle it — on a drive with no
Recycle Bin (such as some network or external drives), when the Recycle Bin
is set to delete files immediately, or, when Windows reports the Recycle
Bin's size, when the folder is bigger than that — and says to use File
Explorer instead. If Windows still finds it can't recycle something, it asks
before deleting anything for good. If a file in the folder is in use, what
could be recycled is, the rest stays where it was, and OpenEVP says so.

Right-click in the library for the same tools: on an empty part of the list,
**New folder**; on a folder, **Open**, **Rename** or **Delete**; on a recording,
**Play**, **Rename…**, **Move to…** (all the ticked recordings, if you right-click a
ticked one) or **Export clips**. A folder's **Export clips** does every marked recording
in it and in its subfolders (see *EVP clips* below).
**Show in File Explorer** (on a recording or clip) opens its folder in File Explorer with
the file selected — the file the row names, so the `.dvf` when there's also a `.wav` copy;
**Open in File Explorer** (on a folder, including a `Clips` folder) opens that folder.

**Rename…** (or **F2**) renames a recording: its `.dvf` and `.wav` in that folder get the new name,
each keeping its extension; a name that's already taken is refused, and marks stay with it.

A second OpenEVP window can't create, rename, delete or move anything in the
library, and can't export; do those in the first window.

Move recordings between folders by ticking their checkboxes and clicking
**Move to…**, or by dragging a row onto a folder or a breadcrumb step. Marks
travel with the audio automatically — nothing about a recording's marks
changes when it moves. The **Investigation** column always shows the
top-level folder a recording sits in, however deep it's nested.

Recorder exports still go to the separate **Save to** folder set above;
folders here just organize what's already in the library.

**Backups.** The first time you mark a recording still on the recorder,
OpenEVP saves a copy of it (the `.dvf`, and a WAV if conversion is available)
to the Save-to folder in the background, so the mark isn't lost if the
recorder is later wiped. A line under the marks list shows when it's done,
or says it isn't backed up yet (if it failed, or couldn't start because the
app was closing or updating) and offers **Retry backup**.

**Export WAV with marks** saves a WAV copy of the loaded recording, with its
marks written in, to the Save-to folder (into the recorder folder's letter,
or the investigation's folder for a file in the library). Those marks are standard RIFF
`cue`/`labl`/`ltxt` WAV markers — the marker format many audio editors can
read (none has been verified with OpenEVP yet).

### The player's settings

Right of the waveform, three small tabs hold the player's settings: **View**
(Zoom, Height, Spectrogram), **Speed** (speed, Keep pitch, Exports at …×) and
**Enhance** (the listening aids, Reduce noise, Exports enhanced). Click a tab or
use the arrow keys on it (Home and End for the first and last); the last one
you picked is shown again next time. A tab with something changed from its
default shows a dot (**Speed •** while the speed isn't 1×, **Enhance •** while
anything there is on, **View •** with the spectrogram off, the height raised or
the waveform zoomed in past fit-to-width), and its tooltip says what, so a
setting is never hidden behind another tab. The
keyboard shortcuts (`[` `]` `\`, M, Space) work whichever tab is shown.

### Playback speed

**Speed**, on the Speed tab, plays at 0.25× to 2× (`[` slower, `]`
faster, `\` or a double-click back to 1×). With **Keep pitch** ticked (the
default) speech slows down at its normal pitch; untick it to hear it like a
tape, deeper when slower. The speed and Keep pitch are remembered.

While the speed isn't 1×, **Exports at 0.5×** (ticked when you move the speed
off 1×; not after a restart, even though the speed itself is remembered) makes
the player's Export WAV with marks, Export clips and Save clip save at that
speed, the same way it plays: the names end in the speed (`…_0.5x.wav`,
`…_EVP-A_00m12.4s_0.5x.mp3`, or `…_0.5x-tape.wav` with Keep pitch off), the
marks are moved to match, and a clip's 0.5 s on each side is slowed down with
it. Untick it for normal-speed exports. The library's Export clips is always at
normal speed. Slowing down a whole recording with Keep pitch takes a few
seconds (a progress bar shows how far it is).

### Enhance

The **Enhance** tab holds the listening aids. They change
only what you hear, live while it plays; the recording itself and its marks are
never touched:

- **Boost**: up to +24 dB louder. A soft limiter keeps it from clipping.
- **Leveler** (Light, Medium, Strong): a compressor that brings quiet parts up
  and loud ones down.
- **Voice filter**: keeps the voice band, about 300 to 3400 Hz.
- **Cut rumble**: lowers everything below about 120 Hz (handling noise, wind).
- **Cut hiss**: lowers everything above 5 kHz. An ICD-ST25 recording (8 kHz) has
  nothing up there, so it is greyed out for those.
- **Hum remover**: notches out 60 Hz (or 50 Hz) mains hum and its next three
  harmonics.
- **Reset** turns everything off.

The settings are remembered. While any of them is on, the tab reads
**Enhance •** and an **Enhanced** tag sits above the waveform, so it is never
left on unnoticed. **Exports enhanced** then shows on the tab: like **Exports at
0.5×** it is ticked only when you turn enhancement on in this session, and it
makes the player's Export WAV with marks, Export clips and Save clip save what
you hear, named `…_enhanced` (`…_0.5x_enhanced.wav` with a speed: the speed is
applied first, then the enhancements, as the player does). The library's Export
clips always saves as recorded. Saving the same thing again gives identical
files, so it says "already saved".

The player runs the enhancements in Web Audio; an export runs the same chain in
numpy (`openevp/enhance.py`, with the Leveler ported from the browser's own
compressor in `openevp/leveler.py`). Against Chromium's offline renderer they
agree to within rounding (87 dB or more below the signal). What you hear can
still differ very slightly: the player filters at the sound card's rate after
resampling, and its Leveler delays the sound by 6 ms (an export keeps every
position, so its marks stay put).

### Noise reduction

To take steady background noise down (hiss, fans, air conditioning, traffic),
drag across a stretch with **only** that noise in it (no voices, at least a
quarter of a second; a second or two is better) and click **Learn noise** under
the waveform. Then tick **Reduce noise** on the **Enhance** tab (hover it to see
where the noise was learnt from). OpenEVP makes a
noise-reduced copy of the recording's audio (a progress bar with Cancel shows
while it does; a 30-minute ICD-ST25 recording takes about 5 seconds) and the
player switches to it where it was, playing on. Its **amount** sets how far the
noise goes down: 40% (the default) lowers it by 12 dB, 100% by 30 dB.

Keep it low. Strong noise reduction leaves watery, warbling artefacts, and those
can sound like whispers or voices: an "EVP" heard only with Reduce noise on
should be checked with it off.

The copy has exactly the recording's length and sample rate, so marks,
selections, loops, the speed and the other enhancements all work on it as
usual, and marks stay the recording's own: the copy is never fingerprinted,
never listed in the EVP Library, and lives only in OpenEVP's temporary cache.
The noise profile is kept per recording until OpenEVP closes; loading another
recording turns Reduce noise off. **Exports enhanced** includes it (named
`…_enhanced`); it is applied first, then the speed, then the other
enhancements, as the player does.

How it works (`openevp/denoise.py`): a short-time Fourier transform (about 32 ms
Hann windows, 75% overlap), each bin's power smoothed over 5 frames and 5 bins,
then gated against the learnt profile (fully lowered up to 3 dB above the noise,
untouched from 10 dB above), so a lone noise peak never opens on its own (the
cause of the "musical noise" of simple spectral subtraction).

### Spectrogram

**Spectrogram** (on the View tab) is on unless you untick it: it shows under the waveform, time across,
pitch up (0 Hz at the bottom, labelled in kHz), loudness as colour from black
through purple and red to pale yellow. A voice shows as stacked bright bands
(its harmonics) shaped by its formants, which is often easier to spot than in
the waveform, even under noise. It scrolls and zooms with the waveform, and the
cursor, the marks and a drag selection cover it too. Untick it and it stays off
(remembered); tick it again any time. Opening a recording is never slowed down by
it: the waveform comes first, and the spectrogram fills in a moment later.

It is computed by OpenEVP itself, not the page: about 32 ms windows (256-point
FFT at 8 kHz, 512 at 16 kHz, 1024 at 44.1/48 kHz), up to 8 kHz (a 44.1 kHz
recording has little but hiss above that), at several levels of detail served
as image tiles, so only what is on screen is drawn. A 30-minute ICD-ST25
recording takes under 2 seconds and about 60 MB. MP3 recordings and clips get
one too: they are decoded to WAV like every other file.

### EVP clips

**Export clips** (next to Export WAV with marks) saves every mark of the loaded
recording as its own short clip; **Save clip** in a mark's row saves just that one.
The **Clip format** menu next to Export clips picks **MP3 (for sharing)**, the
default, or **WAV (full quality)**; it is remembered, and the library's Export
clips uses it too.
In the library, right-click a recording, or a folder (every marked recording in
it and its subfolders), and choose **Export clips**. A folder runs in the
background with a progress bar and **Cancel**; recordings that can't be decoded
(no decoder, an ICD-ST10 mode this build can't play, or damaged) are skipped
and listed with the reason. When it's done
the banner says how many clips were saved (and how many were already there),
with **Open folder**.

- Each clip is the mark plus **0.5 s** on each side (less at the very start or
  end of the recording), cut from the decoded audio in its own format — an ST25
  recording stays 8 kHz mono, an ICD-ST10 one 44.1 kHz stereo (ST) or 16 kHz
  mono (SP); nothing is resampled.
- MP3 clips are 128 kbps CBR at the recording's own sample rate (LAME allows
  at most 64 kbps at 8 kHz, so ST25 clips are 64 kbps), with the mark (class,
  time and note) as the ID3 title and the note as the comment. A WAV from
  elsewhere at a rate MP3 doesn't have (say 96 kHz) is resampled by the encoder
  to the nearest MP3 rate; mono and stereo only.
- Clips go into a **`Clips`** folder inside the folder Export WAV with marks
  uses (for example `Save to\A\Clips` or `Save to\Old Mill\Clips`), named
  `<recording>_EVP-<class>_<MMmSS.s>s[_<note>].mp3` (or `.wav`), e.g.
  `001_A_003_EVP-A_00m12.4s.mp3`. The note is shortened and characters Windows
  doesn't allow in file names are left out.
- In a WAV clip, the mark's class and note are written in as a WAV marker, like
  in the WAV with marks.
- Nothing is overwritten: a clip already saved with the same bytes counts as
  "already there"; a different one gets a numbered name ("… (2).wav").
- The EVP Library lists the `Clips` folders OpenEVP creates, with a film icon
  and a **Clips** tag. Open one to see its clips; click a clip to play it. A clip
  is an EVP already, so it never counts as one: its marker is not read as a
  mark, clips are left out of **Has EVPs** (and the other filters), the folder
  counts, the library total and the EVP chips, and **All recordings** leaves
  them out (they are copies of parts of recordings). You can still mark a clip
  in the player; the mark is kept, but it doesn't count either.
- **Clips from a clip**: load a clip (WAV or MP3), mark the part you want and
  use Export clips or Save clip, or right-click the clip in the library and
  choose **Export clips**. The new clips go into the same `Clips` folder as the
  clip they were cut from (never a `Clips` folder inside it), named after it,
  e.g. `001_A_003_EVP-A_00m12.4s_EVP-A_00m00.8s.mp3`. A folder's Export clips
  (and Export clips on a `Clips` folder itself) never cuts clips from clips.
- MP3 clips are clips exactly like WAV clips: listed, played and markable, never
  counted, and they can be moved out of the `Clips` folder (then they are
  ordinary recordings). An MP3 anywhere else in the library is a recording.
- Rename or delete a `Clips` folder like any other folder (delete goes to the
  Recycle Bin), and renaming, moving or deleting a folder takes its `Clips`
  folder along. Recordings can't be moved into a `Clips` folder: it is for
  clips only. WAV clips can be moved out of it, and then are ordinary WAVs.
- OpenEVP recognises its own `Clips` folders by a small hidden file inside,
  `.openevp-clips`; delete that file and its clips (WAV or MP3) count as
  ordinary recordings.
  A folder you named `Clips` yourself is an ordinary folder.

### Where marks live

Marks and settings (including the library folder) are stored on this PC in
`%APPDATA%\OpenEVP\`: `marks.json`, `settings.json`, and `index.json` (an
internal cache of files already checked). Only one OpenEVP window writes at a
time — a second one open at the same time can still browse and play, but
can't add, edit or delete marks (it says so) until the first is closed.

## Limits

- **Verified** natively on Linux against one ICD-ST25: 20 LP-mode recordings in
  folder A (owner set, dated and undated), folders B–E empty. On Windows 11 the
  installer set up the driver on its own and the app listed all 20 recordings
  from the same recorder (2026-09-25).
- **Folders B–E**: up to v0.8.2 the download command always named folder A,
  so recordings in B–E were reported and not saved (never saved wrong). It now
  names the folder, as Digital Voice Editor does; checked on an ICD-ST10
  (folders B and C), not yet on an ICD-ST25.
- **ICD-ST25: LP mode only.** Only LP recordings have been checked. The folder
  table names each recording's mode; a mode OpenEVP doesn't know is reported
  and not saved. SP would be caught this way if SP uses a different mode byte
  (unverified), so record in LP.
- **ICD-ST10**, verified on one recorder with three recordings (2026-09-29).
  Its recordings have no owner name, and no date while its clock isn't set, so
  their files are named like `001_A_001_Unknown.dvf`. The length listed before a
  recording is decoded counts its frames: it is exact for a recording made in
  one go, and each restart inside a recording adds about 0.09 s (the decoded
  length, shown in the library, is exact). Its flash size is not known; a
  recording longer than 32 MB (about an hour and a half of LPEC ST) would be
  reported, not saved. Recordings made with its clock set are untested. Its SP
  mode's `.dvf` header is OpenEVP's own guess (the LP header with SP's codec,
  channel and rate fields) until a DVE-saved SP file can be compared. An SP
  recording's length before it is downloaded is its size over 2000 bytes a
  second, which can be off by a few hundredths of a second; once saved, its
  length is counted from its frames and is exact.
- **Long ICD-ST10 recordings are big**: 92 minutes of 44.1 kHz stereo is about
  930 MB of WAV. Playback decodes straight into a disk cache (2 GB, the oldest
  recordings dropped first, room made before a decode and again if the disk
  fills up; a cache left behind by a crash is deleted at the next start) and
  the library fingerprints recordings without
  holding their audio, but a WAV export or a marked backup holds one decoded
  recording in memory while it is saved. The player draws the waveform from
  the audio itself only for the first 2.7 minutes' worth of stereo samples
  (30 minutes of ICD-ST25 audio); longer recordings are drawn from 400 peaks
  per second. Decoding takes about 1.4 s of CPU per minute of audio (about two
  minutes for the longest recording).
- An ICD-ST10 shows as ICD-ST25 until you open it, and uses the same driver.
- Messages in table slots 64 and above rely on an assumed layout. Each message is
  cross-checked against its downloaded data, so a wrong assumption stops the
  download rather than writing a bad file.

## How it works

| Layer | Details |
|---|---|
| Transport | vendor control requests on interface 0, `wValue 0xABAB`: status `0x01` (4 bytes), command `0x80` (24/32-byte frame), reply `0x81`; audio on bulk IN endpoint `0x81` |
| Status | `00 00` idle, `0f 81 LLLL` reply of `LLLL` bytes ready, `0f 01` busy |
| Folder table | 137 NAND pages of 528 bytes (512 data + 16 spare): page 0 message list, page 2 start counters, page 5+ flash address ranges, pages 9+ entries (owner, date) |
| Download | `GET_VOICE` opcode `0x11FF000N` for folder N = 1–5 (A–E), then the message number, block count and size; reading a folder's table does not select the folder |
| Audio | per message, `blocks × 1056` bytes on the wire = 2 × (512 data + 16 spare) per block |
| `.dvf` | 512-byte header + 512 × `0xFF` + one 1024-byte block per wire block (spare dropped, tail of the last block filled with `0xFF`) |

The details are in the module docstrings.

## Build

Needs x64 Python 3.12 and [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install JRSoftware.InnoSetup`):

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

It runs the tests, then builds `dist\openevp-st25.exe` (ST25 command line),
`dist\OpenEVP\` (desktop app) and `dist\OpenEVP-Setup-<version>.exe` (the
installer). The tests run in the release gate (`OPENEVP_RELEASE_GATE=1`): a
decoder test that can't run because the tables or the C core are missing or
damaged fails the build instead of being skipped.

Before publishing a release, run `python tools\release_check.py` after the
build. It re-runs the tests in the release gate, checks the built command-line
tool and app, and prints the manual checklist (a real recorder, the driver,
the updater).

WAV conversion needs two more things for each of the two Sony decoders (LPEC,
for LP and SP, and LPEC ST), all kept out of the repository:

- their C cores, `openevp\decoders\sony_lpec\lpec_core.dll` and
  `openevp\decoders\sony_lpec_st\lpec_st_core.dll`, which the build script
  compiles with `python tools\build_lpec_core.py`. That needs a 64-bit
  MinGW-w64 `gcc` on `PATH` (the C uses GCC's `__int128`, so MSVC can't build it).
  Without the DLLs the decoders still work in pure Python, about 60 (LP) and
  15 (LPEC ST) times slower.
- their table data (see *Legal*), `openevp\decoders\sony_lpec\data\lpec_tables.json`
  and `openevp\decoders\sony_lpec\data\lpec_sp_tables.json`
  (`python tools\import_lpec_tables.py`, from dumps of DVE's `LPEC.dll` in the
  `--tables-dir` / `OPENEVP_TABLE_DUMPS` folder; the SP file needs the `sp_*`
  dumps) and
  `openevp\decoders\sony_lpec_st\data\lpec_st_tables.json`
  (`python tools\import_lpec_st_tables.py`, from a copy of DVE's `lcstde.ax` in
  the same `--tables-dir` / `OPENEVP_TABLE_DUMPS` folder, or `--dll`). The build
  **fails** without them. For a development build without WAV conversion, pass
  `-NoLpecTables`:
  `powershell -ExecutionPolicy Bypass -File build_windows.ps1 -NoLpecTables`.

On Linux the command-line tool runs as-is with the system libusb:
`python3 st25-download.py OUTPUT` (it needs permission to open the USB device).

## License

Copyright (C) 2026 Michael Barcelo. OpenEVP is free software under the
GNU General Public License v3.0 or later; see [`LICENSE`](LICENSE).

## Legal

OpenEVP is an independent project and is not affiliated with, endorsed by, or supported by Sony or Panasonic. "Sony", "ICD-ST25", "ICD-ST10" and "Digital Voice Editor" are trademarks of Sony Corporation. "Panasonic" and "RR-DR60" are trademarks of Panasonic Corporation.

To play and convert recordings made on Sony IC recorders, OpenEVP includes numeric tables needed to read Sony's LPEC audio formats (LPEC LP, used by the ICD-ST25 and ICD-ST10, and LPEC SP and LPEC ST, used by the ICD-ST10). They are included only so owners can access their own recordings (interoperability). Rights holders who object can open an issue at https://github.com/MBarc/OpenEVP/issues and the tables will be removed promptly. The tables ship inside the installer only, as three data files
(`openevp/decoders/sony_lpec/data/lpec_tables.json` for LPEC LP,
`openevp/decoders/sony_lpec/data/lpec_sp_tables.json` for LPEC SP and
`openevp/decoders/sony_lpec_st/data/lpec_st_tables.json` for LPEC ST); they are not part of this repository.

OpenEVP is provided "as is", without warranty of any kind.

## Third-party

`vendor/libusb-1.0.30/libusb-1.0.dll` is libusb 1.0.30 (MinGW64 build) from the
official PGP-signed release (signed by Tormod Volden), licensed LGPL-2.1; see
`vendor/libusb-1.0.30/COPYING`. The build script checks its SHA-256.

MP3 clips are encoded with [lameenc](https://github.com/chrisstaite/lameenc)
(installed from PyPI, `requirements-app.txt`), licensed LGPL-3.0-or-later, which
includes the [LAME](https://lame.sourceforge.io/) MP3 encoder, licensed
LGPL-2.0-or-later. The app bundles it as a separate extension module
(`_internal\lameenc.*.pyd`), with its license in `_internal\lameenc-*.dist-info`.

MP3 files are decoded with [minimp3](https://github.com/lieff/minimp3),
dedicated to the public domain under CC0 1.0 Universal; see
`vendor/minimp3/LICENSE`. `vendor/minimp3/minimp3.h` is commit
`ea99364f61c14656440e8d77e9c233ccf3124633` (2026-07-27), pinned by SHA-256 in
`tools/build_lpec_core.py`; it is compiled into
`openevp/decoders/mp3/mp3_core.dll` with OpenEVP's own wrapper (`_mp3.c`).
On x86-64 minimp3 always takes its SSE2 code path, so the same file decodes to
the same samples on every PC.

The Leveler (`openevp/leveler.py`) is a port of Chromium's Web Audio
DynamicsCompressor (`third_party/blink/renderer/platform/audio/dynamics_compressor.cc`,
Copyright (C) 2011 Google Inc.), licensed BSD-3-Clause; the full notice is in
[`LICENSES/chromium-dynamics-compressor.txt`](LICENSES/chromium-dynamics-compressor.txt),
which the app bundles (`_internal\LICENSES\`) and the release check verifies.

`vendor/webview2/MicrosoftEdgeWebview2Setup.exe` is Microsoft's Evergreen WebView2
bootstrapper (Authenticode-signed by Microsoft; the build checks its SHA-256). It is
redistributable, and the installer runs it only when WebView2 is missing.
`app/ui/vendor/wavesurfer.min.js` is wavesurfer.js (BSD-3-Clause).
