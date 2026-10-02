<div align="center">

<img src="assets/logo.svg" width="128" alt="OpenEVP logo: audio level bars shaped like a ghost">

# OpenEVP

**Get the recordings off your ghost-hunting voice recorder, then find, mark and share your EVPs.**

[![Latest release](https://img.shields.io/github/v/release/MBarc/OpenEVP?label=latest%20release)](https://github.com/MBarc/OpenEVP/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/MBarc/OpenEVP/total?label=downloads)](https://github.com/MBarc/OpenEVP/releases)
[![Licence: GPL-3.0-or-later](https://img.shields.io/badge/licence-GPL--3.0--or--later-blue)](LICENSE)
[![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6)](#faq)

### [⬇ Download OpenEVP for Windows](https://github.com/MBarc/OpenEVP/releases/latest)

</div>

One free installer for a modern Windows PC. No manufacturer software, and no
old 32-bit computer kept around just to read your recorder.

![OpenEVP playing a recording: a waveform with a spectrogram underneath, a class A EVP selected, and the list of EVP marks with their notes](docs/images/player.png)

## What it does

- **Downloads from your recorder.** Plug it in, tick the recordings, click
  **Export**. Nothing on the recorder is ever changed or deleted.
- **Plays them with a waveform and a spectrogram**, so a voice stands out
  even under noise.
- **Marks EVPs as class A, B or C** with a note on what you hear. Marks save
  themselves.
- **EVP Library:** every recording you've saved, sorted into investigation
  folders, with filters for A/B/C, “has EVPs” and “not reviewed”.
- **Clips:** saves each EVP as its own short clip, as an **MP3 for sharing** or
  a full-quality WAV.
- **Speed:** slow a recording down to 0.25× (or speed it up to 2×), keeping
  the pitch or tape-style.
- **Enhance:** boost, leveler, voice filter, rumble, hiss and hum removal,
  live while you listen. Your recording is never changed.
- **Noise reduction** that learns your room's background noise.
- **MP3 files too:** open WAVs and MP3s from anywhere on your PC, including
  voice notes and clips saved from WhatsApp Web.
- **Updates itself:** it tells you when a new version is out and installs it
  for you.

## Supported recorders

| Recorder | Status |
|---|---|
| Sony ICD-ST25 | ✅ Supported |
| Sony ICD-ST10 | ✅ Supported, in all three recording modes (ST, SP and LP) |
| Panasonic RR-DR60 | ❌ Can't be read directly: it has no PC connection, only a headphone jack. Capturing its recordings through a PC's line-in is a possible future idea, not a promise. |

**Have another recorder?** [Open an issue](https://github.com/MBarc/OpenEVP/issues)
and tell us which one.

## Quick start

1. **[Download](https://github.com/MBarc/OpenEVP/releases/latest)**
   `OpenEVP-Setup-<version>.exe` and run it. When Windows asks for permission,
   click **Yes**. The installer sets up everything, including the recorder's
   driver.
2. **Plug in your recorder** and open **OpenEVP** from the Start menu.
3. **Click the recorder** on the left, tick the recordings you want and click
   **Export**. By default they're saved to `Documents\OpenEVP`.
4. **Click a recording to play it.** Drag across the waveform over a voice and
   press **M** to mark it as an EVP.

The [user guide](docs/user-guide.md) covers everything else.

## Screenshots

The **EVP Library**, with every recording sorted by investigation and its
EVPs listed under it:

![The EVP Library: recordings from three investigations with their length, A/B/C EVP counts and reviewed ticks; two recordings are expanded to show their EVP notes](docs/images/library.png)

The **Enhance** tab: boost, voice filter and a 60 Hz hum remover turned on
while a class A EVP is selected:

![The player with the Enhance tab open: Boost at +9 dB, Voice filter ticked and Hum set to 60 Hz, an Enhanced tag above the waveform and a selected class A EVP](docs/images/enhance.png)

## FAQ

### Windows says “Windows protected your PC”. Is it safe?

Click **More info**, then **Run anyway**. Windows shows this warning the first
time because OpenEVP's installer isn't code-signed with a commercial
certificate. It isn't a sign that anything is wrong. OpenEVP is
open source, so anyone can read exactly what it does. Its updates are
checked against the project's own signing key before they're installed.

### Does it work on a Mac?

Not yet. OpenEVP needs 64-bit (x64) Windows 10 (version 2004 or later) or
Windows 11. Windows in “S mode” can't run it, and ARM-based Windows PCs
aren't supported yet.

### Where do my files go?

By default to `Documents\OpenEVP`, with one subfolder per recorder folder. Click the
**Save to** path in the app to open it, or **Change…** to pick another folder.
Your EVP marks and settings are kept separately, in `%APPDATA%\OpenEVP`.

### Are my recordings changed?

Never. OpenEVP only reads from your recorder: nothing on it is changed or
deleted. Saved files are never overwritten, and the listening aids (speed,
Enhance and noise reduction) only change what you hear, not the recording.
Your EVP marks are kept separately from the audio, so they're safe too.

### How do updates work?

When OpenEVP starts, it checks GitHub for a newer version. If there is one,
it shows what's new and offers **Update now** or **Not now**. **Update now**
downloads the installer, checks that it really comes from this project, asks
Windows for permission and reopens OpenEVP on the new version. You can also
click **Check for updates** at the top of the window at any time.

### The recorder shows as “needs setup”, or seems stuck

See [Troubleshooting](docs/user-guide.md#troubleshooting) in the user guide.

## Learn more

- **[User guide](docs/user-guide.md):** every feature, step by step.
- **[Technical documentation](docs/technical.md):** how OpenEVP talks to the
  recorders, the driver setup, the command-line tool and how to build it.
- **[Adding a recorder](openevp/recorders/README.md):** for developers.

## Licence

Copyright (C) 2026 Michael Barcelo. OpenEVP is free software under the
GNU General Public License v3.0 or later; see [`LICENSE`](LICENSE).

## Legal

OpenEVP is an independent project and is not affiliated with, endorsed by, or
supported by Sony or Panasonic. "Sony", "ICD-ST25", "ICD-ST10" and "Digital
Voice Editor" are trademarks of Sony Corporation. "Panasonic" and "RR-DR60" are
trademarks of Panasonic Corporation.

To play and convert recordings made on Sony IC recorders, OpenEVP includes
numeric tables needed to read Sony's LPEC audio formats (LPEC LP, used by the
ICD-ST25 and ICD-ST10, and LPEC SP and LPEC ST, used by the ICD-ST10). They are
included only so owners can access their own recordings (interoperability).
Rights holders who object can open an issue at
https://github.com/MBarc/OpenEVP/issues and the tables will be removed
promptly. The tables ship inside the installer only, as three data files
(`openevp/decoders/sony_lpec/data/lpec_tables.json` for LPEC LP,
`openevp/decoders/sony_lpec/data/lpec_sp_tables.json` for LPEC SP and
`openevp/decoders/sony_lpec_st/data/lpec_st_tables.json` for LPEC ST); they are
not part of this repository.

OpenEVP is provided "as is", without warranty of any kind.

## Third-party notices

- **libusb:** `vendor/libusb-1.0.30/libusb-1.0.dll` is libusb 1.0.30 (MinGW64
  build) from the official PGP-signed release (signed by Tormod Volden),
  licensed LGPL-2.1; see `vendor/libusb-1.0.30/COPYING`. The build script
  checks its SHA-256.
- **lameenc and LAME:** MP3 clips are encoded with
  [lameenc](https://github.com/chrisstaite/lameenc) (installed from PyPI,
  `requirements-app.txt`), licensed LGPL-3.0-or-later, which includes the
  [LAME](https://lame.sourceforge.io/) MP3 encoder, licensed
  LGPL-2.0-or-later. The app bundles it as a separate extension module
  (`_internal\lameenc.*.pyd`), with its license in
  `_internal\lameenc-*.dist-info`.
- **minimp3:** MP3 files are decoded with
  [minimp3](https://github.com/lieff/minimp3), dedicated to the public domain
  under CC0 1.0 Universal; see `vendor/minimp3/LICENSE`.
  `vendor/minimp3/minimp3.h` is commit
  `ea99364f61c14656440e8d77e9c233ccf3124633` (2026-07-27), pinned by SHA-256
  in `tools/build_lpec_core.py`; it is compiled into
  `openevp/decoders/mp3/mp3_core.dll` with OpenEVP's own wrapper (`_mp3.c`).
  On x86-64 minimp3 always takes its SSE2 code path, so the same file decodes
  to the same samples on every PC.
- **Chromium DynamicsCompressor:** the Leveler (`openevp/leveler.py`) is a
  port of Chromium's Web Audio DynamicsCompressor
  (`third_party/blink/renderer/platform/audio/dynamics_compressor.cc`,
  Copyright (C) 2011 Google Inc.), licensed BSD-3-Clause; the full notice is in
  [`LICENSES/chromium-dynamics-compressor.txt`](LICENSES/chromium-dynamics-compressor.txt),
  which the app bundles (`_internal\LICENSES\`) and the release check verifies.
- **WebView2:** `vendor/webview2/MicrosoftEdgeWebview2Setup.exe` is
  Microsoft's Evergreen WebView2 bootstrapper (Authenticode-signed by
  Microsoft; the build checks its SHA-256). It is redistributable, and the
  installer runs it only when WebView2 is missing.
- **wavesurfer.js:** `app/ui/vendor/wavesurfer.min.js` is wavesurfer.js
  (BSD-3-Clause).
