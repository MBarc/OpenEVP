"""Build the optional LPEC C core: openevp/decoders/sony_lpec/_lpec.c -> openevp/decoders/sony_lpec/lpec_core.dll.

    python tools/build_lpec_core.py [--cc gcc]

Needs a 64-bit MinGW-w64 gcc on PATH: _lpec.c is GCC C (its x87 fcos
emulation uses unsigned __int128), so MSVC cannot build it. The flags are part of the decoder's
bit-exactness (docs/lpec.md, "Arithmetic"): plain IEEE double arithmetic
in source order, so no fused multiply-add (-ffp-contract=off) and no
fast-math reassociation. -static-libgcc keeps the DLL dependent only on
Windows' own runtime. The DLL is a build output (git-ignored); without it
openevp.decoders.sony_lpec decodes in pure Python.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "openevp" / "decoders" / "sony_lpec" / "_lpec.c"
TARGET = ROOT / "openevp" / "decoders" / "sony_lpec" / "lpec_core.dll"

FLAGS = [
    "-O2", "-ffp-contract=off", "-fno-fast-math", "-std=c11",
    "-Wall", "-Wextra", "-shared", "-static-libgcc", "-s",
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cc", default="gcc", help="C compiler (default: gcc)")
    args = parser.parse_args(argv)
    cmd = [args.cc, *FLAGS, "-o", str(TARGET), str(SOURCE)]
    print(" ".join(cmd))
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        print(f"{args.cc} not found; install MinGW-w64 gcc (x86_64)", file=sys.stderr)
        return 1
    if result.returncode != 0:
        return result.returncode
    print(f"built {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
