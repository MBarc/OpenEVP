"""Build the optional C cores of the Sony decoders:

    openevp/decoders/sony_lpec/_lpec.c        -> openevp/decoders/sony_lpec/lpec_core.dll (LPEC LP)
    openevp/decoders/sony_lpec_st/_lpec_st.c  -> openevp/decoders/sony_lpec_st/lpec_st_core.dll (LPEC ST)

    python tools/build_lpec_core.py [--cc gcc]

Needs a 64-bit MinGW-w64 gcc on PATH: _lpec.c is GCC C (its x87 fcos
emulation uses unsigned __int128), so MSVC cannot build it. The flags are
part of the decoders' bit-exactness (docs/lpec.md and docs/lpec-st.md,
"Arithmetic"): plain IEEE double arithmetic in source order, so no fused
multiply-add (-ffp-contract=off) and no fast-math reassociation; x86-64 gcc
does double arithmetic in SSE2, never with x87 excess precision.
-static-libgcc keeps the DLLs dependent only on Windows' own runtime. The
DLLs are build outputs (git-ignored); without them the decoders run in pure
Python.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECODERS = ROOT / "openevp" / "decoders"
SOURCE = DECODERS / "sony_lpec" / "_lpec.c"
TARGET = DECODERS / "sony_lpec" / "lpec_core.dll"
ST_SOURCE = DECODERS / "sony_lpec_st" / "_lpec_st.c"
ST_TARGET = DECODERS / "sony_lpec_st" / "lpec_st_core.dll"
BUILDS = ((SOURCE, TARGET), (ST_SOURCE, ST_TARGET))

FLAGS = [
    "-O2", "-ffp-contract=off", "-fno-fast-math", "-std=c11",
    "-Wall", "-Wextra", "-shared", "-static-libgcc", "-s",
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cc", default="gcc", help="C compiler (default: gcc)")
    args = parser.parse_args(argv)
    for source, target in BUILDS:
        cmd = [args.cc, *FLAGS, "-o", str(target), str(source)]
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
