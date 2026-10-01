"""Build the C cores of the decoders:

    openevp/decoders/sony_lpec/_lpec.c        -> openevp/decoders/sony_lpec/lpec_core.dll (LPEC LP)
    openevp/decoders/sony_lpec_st/_lpec_st.c  -> openevp/decoders/sony_lpec_st/lpec_st_core.dll (LPEC ST)
    openevp/decoders/mp3/_mp3.c               -> openevp/decoders/mp3/mp3_core.dll (MP3, with minimp3)

    python tools/build_lpec_core.py [--cc gcc]

Needs a 64-bit MinGW-w64 gcc on PATH: _lpec.c is GCC C (its x87 fcos
emulation uses unsigned __int128), so MSVC cannot build it. The flags are
part of the decoders' bit-exactness (docs/lpec.md and docs/lpec-st.md,
"Arithmetic"): plain IEEE double arithmetic in source order, so no fused
multiply-add (-ffp-contract=off) and no fast-math reassociation; x86-64 gcc
does double arithmetic in SSE2, never with x87 excess precision.
-static-libgcc keeps the DLLs dependent only on Windows' own runtime. The
DLLs are build outputs (git-ignored); without them the Sony decoders run in
pure Python. The MP3 decoder has no pure-Python fallback: without
mp3_core.dll MP3 files cannot be played (formats.DecoderUnavailable). Its
source includes vendor/minimp3/minimp3.h (lieff/minimp3, CC0), pinned by
SHA-256 here: a changed header fails the build.
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECODERS = ROOT / "openevp" / "decoders"
SOURCE = DECODERS / "sony_lpec" / "_lpec.c"
TARGET = DECODERS / "sony_lpec" / "lpec_core.dll"
ST_SOURCE = DECODERS / "sony_lpec_st" / "_lpec_st.c"
ST_TARGET = DECODERS / "sony_lpec_st" / "lpec_st_core.dll"
MP3_SOURCE = DECODERS / "mp3" / "_mp3.c"
MP3_TARGET = DECODERS / "mp3" / "mp3_core.dll"
MINIMP3 = ROOT / "vendor" / "minimp3"
# lieff/minimp3 at commit ea99364f61c14656440e8d77e9c233ccf3124633 (2026-07-27): minimp3.h
MINIMP3_SHA256 = "57e437c5c1f0e8b243885d3929c8973b5e6c778451e0100ab4251d19915cb3ad"
BUILDS = ((SOURCE, TARGET, []), (ST_SOURCE, ST_TARGET, []), (MP3_SOURCE, MP3_TARGET, ["-I", str(MINIMP3)]))

FLAGS = [
    "-O2", "-ffp-contract=off", "-fno-fast-math", "-std=c11",
    "-Wall", "-Wextra", "-shared", "-static-libgcc", "-s",
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cc", default="gcc", help="C compiler (default: gcc)")
    args = parser.parse_args(argv)
    header = MINIMP3 / "minimp3.h"
    if hashlib.sha256(header.read_bytes()).hexdigest() != MINIMP3_SHA256:
        print(f"{header} is not the pinned minimp3 (SHA-256 mismatch)", file=sys.stderr)
        return 1
    for source, target, extra in BUILDS:
        cmd = [args.cc, *FLAGS, *extra, "-o", str(target), str(source)]
        print(" ".join(cmd))
        try:
            result = subprocess.run(cmd)
        except FileNotFoundError:
            print(f"{args.cc} not found; install MinGW-w64 gcc (x86_64)", file=sys.stderr)
            return 1
        if result.returncode != 0:
            return result.returncode
        print(f"built {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
