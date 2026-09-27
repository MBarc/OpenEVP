<p align="center"><img src="assets/logo.svg" width="128" alt="OpenEVP logo: audio level bars shaped like a ghost"></p>

# OpenEVP

Get the recordings off your ghost-hunting voice recorders and onto a modern
Windows PC: one installer, no manufacturer software, no old 32-bit computer.

## Recorders

| Recorder | Status |
|---|---|
| Sony ICD-ST25 | ✅ Supported: download, play, WAV export, EVP marks |
| Sony ICD-ST10 | 🛠 Planned |
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
**Play** or **Move to…** (all the ticked recordings, if you right-click a
ticked one).

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
- **LP mode only.** Only LP recordings have been checked. A recording whose data
  does not carry the LP marker is reported and not saved. Whether SP recordings
  carry a different marker is unknown until an SP sample has been analysed, so
  record in LP.
- Messages in table slots 64 and above rely on an assumed layout. Each message is
  cross-checked against its downloaded data, so a wrong assumption stops the
  download rather than writing a bad file.

## How it works

| Layer | Details |
|---|---|
| Transport | vendor control requests on interface 0, `wValue 0xABAB`: status `0x01` (4 bytes), command `0x80` (24/32-byte frame), reply `0x81`; audio on bulk IN endpoint `0x81` |
| Status | `00 00` idle, `0f 81 LLLL` reply of `LLLL` bytes ready, `0f 01` busy |
| Folder table | 137 NAND pages of 528 bytes (512 data + 16 spare): page 0 message list, page 2 start counters, page 5+ flash address ranges, pages 9+ entries (owner, date) |
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

WAV conversion needs two more things, both kept out of the repository:

- the LPEC decoder's C core, `openevp\decoders\sony_lpec\lpec_core.dll`, which the build script
  compiles with `python tools\build_lpec_core.py`. That needs a 64-bit
  MinGW-w64 `gcc` on `PATH` (the C uses GCC's `__int128`, so MSVC can't build it).
  Without the DLL the decoder still works in pure Python, about 60 times slower.
- the LPEC table data, `openevp\decoders\sony_lpec\data\lpec_tables.json` (see *Legal*). The build
  **fails** without it. For a development build without WAV conversion, pass
  `-NoLpecTables`:
  `powershell -ExecutionPolicy Bypass -File build_windows.ps1 -NoLpecTables`.

On Linux the command-line tool runs as-is with the system libusb:
`python3 st25-download.py OUTPUT` (it needs permission to open the USB device).

## License

Copyright (C) 2026 Michael Barcelo. OpenEVP is free software under the
GNU General Public License v3.0 or later; see [`LICENSE`](LICENSE).

## Legal

OpenEVP is an independent project and is not affiliated with, endorsed by, or supported by Sony or Panasonic. "Sony", "ICD-ST25", "ICD-ST10" and "Digital Voice Editor" are trademarks of Sony Corporation. "Panasonic" and "RR-DR60" are trademarks of Panasonic Corporation.

To play and convert recordings made on Sony IC recorders, OpenEVP includes numeric tables needed to read Sony's LPEC audio format. They are included only so owners can access their own recordings (interoperability). Rights holders who object can open an issue at https://github.com/MBarc/OpenEVP/issues and the tables will be removed promptly. The tables ship inside the installer only, as one data file
(`openevp/decoders/sony_lpec/data/lpec_tables.json`); they are not part of this repository.

OpenEVP is provided "as is", without warranty of any kind.

## Third-party

`vendor/libusb-1.0.30/libusb-1.0.dll` is libusb 1.0.30 (MinGW64 build) from the
official PGP-signed release (signed by Tormod Volden), licensed LGPL-2.1; see
`vendor/libusb-1.0.30/COPYING`. The build script checks its SHA-256.

`vendor/webview2/MicrosoftEdgeWebview2Setup.exe` is Microsoft's Evergreen WebView2
bootstrapper (Authenticode-signed by Microsoft; the build checks its SHA-256). It is
redistributable, and the installer runs it only when WebView2 is missing.
`app/ui/vendor/wavesurfer.min.js` is wavesurfer.js (BSD-3-Clause).
