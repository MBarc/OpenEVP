# OpenEVP technical documentation

How OpenEVP talks to the recorders, how it compares with Sony's own software,
and how to build it. For using the app, see the [user guide](user-guide.md).

**Contents**

- [Why the ICD-ST25 needed this](#why-the-icd-st25-needed-this)
- [Fidelity to Sony's Digital Voice Editor](#fidelity-to-sonys-digital-voice-editor)
- [Read-only by design](#read-only-by-design)
- [Driver setup](#driver-setup)
- [Command-line tool](#command-line-tool)
- [Enhance: player and export](#enhance-player-and-export)
- [Noise reduction](#noise-reduction)
- [Spectrogram](#spectrogram)
- [Recorder modules](#recorder-modules)
- [How it works](#how-it-works)
- [Build](#build)
- [Decoder documentation](#decoder-documentation)

## Why the ICD-ST25 needed this

The ICD-ST25 (about 2004) is not a USB drive. It speaks a proprietary protocol
through Sony's `ICDUSB2.sys`, a **32-bit-only** driver that cannot load on
64-bit Windows. Sony's current *Sound Organizer* cannot talk to it at all. The
only working path used to be a 32-bit Windows VM running Sony's *Digital Voice
Editor* (DVE). That setup also hits a multi-core race in Sony's
`IcdUsb2.dll`, which cancels USB requests mid-transfer and locks the recorder
up until its cable is replugged.

OpenEVP talks to the recorder directly through the standard,
Microsoft-signed **WinUSB** driver. It is single-threaded, so it has no such
race.

## Fidelity to Sony's Digital Voice Editor

### ICD-ST25

For the ICD-ST25, OpenEVP writes `.dvf` files in the format DVE saves. DVE
opens them and converts them to WAV **byte-identically** to WAVs made from
DVE's own downloads (verified on 20 recordings). The `.dvf` files themselves
differ from DVE's only in two time counters DVE rounds differently, which do
not affect the audio.

OpenEVP also exports and plays WAV itself, with its own decoder for Sony's
LPEC format: its WAV files are byte-identical to the ones DVE writes for the
same recordings (checked on the same 20 recordings), so DVE is no longer
needed.

### ICD-ST10

The ICD-ST10 uses the same USB ID, driver and protocol as the ICD-ST25, so it
is set up and found the same way; the app shows it as an ICD-ST10 once it has
read it.

It records in one of three modes, per recording:

- **ST mode** is LPEC ST (44.1 kHz stereo), which OpenEVP decodes with its own
  LPEC ST decoder ([lpec-st.md](lpec-st.md)). ST recordings play, take EVP
  marks and export as 44.1 kHz stereo WAV files whose samples are
  byte-identical to those of Sony's own decoder (checked on three
  recordings).
- **LP mode** is the ICD-ST25's LPEC LP and plays like an ICD-ST25 recording
  (a real one decoded byte-identically to Sony's decoder).
- **SP mode** is LPEC SP (16 kHz mono), the same codec as LP at 16 kHz.
  OpenEVP's LPEC decoder plays it and converts it to 16 kHz mono WAV files
  whose samples are byte-identical to those of Sony's own decoder (checked on
  one recording and on thousands of generated and damaged frames; see
  [lpec.md](lpec.md)).

ICD-ST10 recordings are saved as `.dvf` files too, and `.dvf` files saved by
an earlier OpenEVP play without downloading them again. Their `.dvf` header is
OpenEVP's own for now, since no `.dvf` saved by DVE from an ICD-ST10 has been
compared yet.

## Read-only by design

Nothing on a recorder is changed or deleted. For the ICD-ST25 the program can
only send the exact read commands DVE itself sends to list and download
recordings. Opcode, frame length and arguments are all checked in
`st25/policy.py`.

## Driver setup

The installer binds the recorder (USB ID `054C 0103`) to Microsoft's own
WinUSB driver, which is built into Windows. It uses only tools that ship with
Windows:

1. It writes a small driver description (INF) for this one device.
2. It catalogs and signs that INF with a certificate made on this PC.
3. It trusts the certificate on this PC only, and deletes its private key
   right after signing. The certificate is made fresh on each PC and can't
   sign other certificates.

The driver works whether or not the recorder is plugged in: Windows applies it
when the recorder is plugged into any USB port. **Set up recorder** in the app
runs the same step.

Uninstalling (**Settings → Apps → OpenEVP**) removes the app, the driver
package and that certificate, which puts the PC back as it was.

## Command-line tool

The ICD-ST25 command-line tool is installed with the app, as
`command-line\openevp-st25.exe` in the install folder:

```
openevp-st25.exe [OUTPUT_FOLDER] [--list] [--folder A-E] [--raw] [--wav] [--open]
```

- `--wav` also writes a `.wav` beside each `.dvf` (and fills in a missing
  `.wav` beside a `.dvf` saved earlier).
- `--open` shows the folder when done.
- `--raw` also keeps the undecoded transfer data, even for recordings it
  cannot convert.

`openevp-st25.exe --check-wav FILE.dvf` decodes one `.dvf` file to check that
WAV conversion works in this build, without touching the recorder or saving
anything.

On Linux the command-line tool runs as-is with the system libusb:
`python3 st25-download.py OUTPUT` (it needs permission to open the USB
device).

## Enhance: player and export

The player runs the enhancements in Web Audio; an export runs the same chain
in numpy (`openevp/enhance.py`, with the Leveler ported from the browser's own
compressor in `openevp/leveler.py`). Against Chromium's offline renderer they
agree to within rounding (87 dB or more below the signal). What you hear can
still differ very slightly: the player filters at the sound card's rate after
resampling, and its Leveler delays the sound by 6 ms (an export keeps every
position, so its marks stay put).

## Noise reduction

`openevp/denoise.py` uses a short-time Fourier transform (about 32 ms Hann
windows, 75% overlap). Each bin's power is smoothed over 5 frames and 5 bins,
then gated against the learnt profile (fully lowered up to 3 dB above the
noise, untouched from 10 dB above), so a lone noise peak never opens on its
own (the cause of the "musical noise" of simple spectral subtraction).

## Spectrogram

The spectrogram is computed by OpenEVP itself, not the page: about 32 ms
windows (256-point FFT at 8 kHz, 512 at 16 kHz, 1024 at 44.1/48 kHz), up to
8 kHz (a 44.1 kHz recording has little but hiss above that), at several levels
of detail served as image tiles, so only what is on screen is drawn. A
30-minute ICD-ST25 recording takes under 2 seconds and about 60 MB. MP3
recordings and clips get one too: they are decoded to WAV like every other
file.

## Recorder modules

Each recorder is one module in `openevp/recorders/`; see
[its README](../openevp/recorders/README.md) to add one.

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

It runs the tests, then builds `dist\openevp-st25.exe` (ICD-ST25 command
line), `dist\OpenEVP\` (desktop app) and `dist\OpenEVP-Setup-<version>.exe`
(the installer). The tests run in the release gate
(`OPENEVP_RELEASE_GATE=1`): a decoder test that can't run because the tables or
the C core are missing or damaged fails the build instead of being skipped.

Before publishing a release, run `python tools\release_check.py` after the
build. It re-runs the tests in the release gate, checks the built command-line
tool and app, and prints the manual checklist (a real recorder, the driver,
the updater).

WAV conversion needs two more things for each of the two Sony decoders (LPEC,
for LP and SP, and LPEC ST), all kept out of the repository:

- Their C cores, `openevp\decoders\sony_lpec\lpec_core.dll` and
  `openevp\decoders\sony_lpec_st\lpec_st_core.dll`, which the build script
  compiles with `python tools\build_lpec_core.py`. That needs a 64-bit
  MinGW-w64 `gcc` on `PATH` (the C uses GCC's `__int128`, so MSVC can't build
  it). Without the DLLs the decoders still work in pure Python, about 60 (LP)
  and 15 (LPEC ST) times slower.
- Their table data (see [Legal](../README.md#legal)):
  - `openevp\decoders\sony_lpec\data\lpec_tables.json` and
    `openevp\decoders\sony_lpec\data\lpec_sp_tables.json`
    (`python tools\import_lpec_tables.py`, from dumps of DVE's `LPEC.dll` in
    the `--tables-dir` / `OPENEVP_TABLE_DUMPS` folder; the SP file needs the
    `sp_*` dumps);
  - `openevp\decoders\sony_lpec_st\data\lpec_st_tables.json`
    (`python tools\import_lpec_st_tables.py`, from a copy of DVE's
    `lcstde.ax` in the same `--tables-dir` / `OPENEVP_TABLE_DUMPS` folder, or
    `--dll`).

  The build **fails** without them. For a development build without WAV
  conversion, pass `-NoLpecTables`:
  `powershell -ExecutionPolicy Bypass -File build_windows.ps1 -NoLpecTables`.

## Decoder documentation

- [lpec.md](lpec.md): the LPEC decoder (ICD-ST25 LP, 8 kHz, and ICD-ST10 SP,
  16 kHz).
- [lpec-st.md](lpec-st.md): the LPEC ST decoder (ICD-ST10 ST, 44.1 kHz
  stereo).
- [app-test-checklist.md](app-test-checklist.md): the desktop app's hardware
  checklist.
