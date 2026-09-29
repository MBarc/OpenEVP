"""Import the LPEC ST decoder tables from Sony's lcstde.ax into a local data file.

The LPEC ST decoder (openevp/decoders/sony_lpec_st, docs/lpec-st.md) needs
tables from lcstde.ax, Digital Voice Editor's DirectShow filter for the
ICD-ST10's "LPEC ST" modes. This script reads them verbatim from a copy of
that DLL and writes them into one file,
`openevp/decoders/sony_lpec_st/data/lpec_st_tables.json`. That file is
git-ignored like the LP decoder's `lpec_tables.json`
(tools/import_lpec_tables.py): the tables are not committed to the
repository, only shipped inside the build (see README, *Legal*).

The DLL is identified by its SHA-256: every address below is specific to
that one build, so any other file is refused rather than turned into a
silently wrong decoder. Tables the DLL computes when it loads (the tone sine
table and window) are not extracted; openevp.decoders.sony_lpec_st.tables
generates them.

Usage:
    python tools/import_lpec_st_tables.py --tables-dir PATH [--dll PATH] [--out PATH]

--tables-dir defaults to the OPENEVP_TABLE_DUMPS environment variable, the
folder of research dumps the LP tables come from; the DLL is looked for
there as lcstde.ax unless --dll names it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLES_DIR = Path(os.environ["OPENEVP_TABLE_DUMPS"]) if os.environ.get("OPENEVP_TABLE_DUMPS") else None
DLL_NAME = "lcstde.ax"
DEFAULT_OUT = REPO_ROOT / "openevp" / "decoders" / "sony_lpec_st" / "data" / "lpec_st_tables.json"
EXPECTED_SHA256 = "71155a2bf88d17885436fec1628b376165b82ded54be831ff91bcf77a870fdef"


class PE:
    """Minimal PE32 reader: maps virtual addresses to file bytes."""

    def __init__(self, data: bytes):
        self.data = data
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("not a PE file")
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        optsz = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        self.base = struct.unpack_from("<I", data, opt + 28)[0]
        self.sections = []
        for i in range(nsec):
            s = opt + optsz + 40 * i
            vsize, va, rawsz, rawptr = struct.unpack_from("<IIII", data, s + 8)
            self.sections.append((va, max(vsize, rawsz), rawptr, rawsz))

    def read(self, addr: int, n: int) -> bytes:
        rva = addr - self.base
        for va, size, rawptr, rawsz in self.sections:
            if va <= rva and rva + n <= va + size:
                off = rva - va
                chunk = self.data[rawptr + off: rawptr + min(off + n, rawsz)]
                return chunk + b"\0" * (n - len(chunk))
        raise ValueError(f"address {addr:#x} not mapped")

    def u8(self, a, n): return list(self.read(a, n))
    def s8(self, a, n): return [x - 256 if x > 127 else x for x in self.read(a, n)]
    def u16(self, a, n): return list(struct.unpack(f"<{n}H", self.read(a, 2 * n)))
    def s16(self, a, n): return list(struct.unpack(f"<{n}h", self.read(a, 2 * n)))
    def s32(self, a, n): return list(struct.unpack(f"<{n}i", self.read(a, 4 * n)))
    def u32(self, a, n): return list(struct.unpack(f"<{n}I", self.read(a, 4 * n)))

    def f32(self, a, n):
        # Floats are stored as their IEEE bit patterns (uint32) so the JSON
        # round-trips exactly; lpec_st.tables converts them.
        return self.u32(a, n)


def huff(pe: PE, addr: int) -> dict:
    """A Huffman descriptor (24 bytes): code table, lookup table, sizes and
    the spectrum fields (coefficients per codeword, group size, shift,
    sign mode, bits per coefficient, mask)."""
    codes, lut, _unused, sizes = pe.u32(addr, 4)
    b = pe.u8(addr + 16, 8)
    maxbits = b[0]
    nsym = sizes & 0xFFFF
    lutsize = sizes >> 16
    if lutsize != 1 << maxbits:
        raise ValueError(f"descriptor {addr:#x}: lut size {lutsize} != 2^{maxbits}")
    lens = [pe.read(codes + 4 * i + 2, 1)[0] for i in range(nsym)]
    return {
        "maxbits": maxbits, "lut": pe.u8(lut, lutsize), "lens": lens,
        "n": b[1], "group": b[2], "shift": b[3], "unsigned": b[4], "bits": b[5], "mask": b[6],
    }


def huff_list(pe, addr, count):
    return [huff(pe, addr + 24 * i) for i in range(count)]


def extract(pe: PE) -> dict:
    t = {}
    # --- frame header scrambling: 4 keys of 16 bytes ---
    t["XOR_KEYS"] = pe.u8(0x1004d5cc, 64)
    # --- bitstream layout ---
    t["QU_TO_SB"] = pe.u8(0x10014784, 33)          # quant unit -> subband
    t["QU_LEN"] = pe.u8(0x10014720, 32)            # coefficients per quant unit
    t["QU_START"] = pe.u16(0x10014740, 33)         # first coefficient of each quant unit
    t["SB_QU"] = pe.u8(0x100147a8, 17)             # subband -> first quant unit
    t["SB_REVERSE"] = pe.u8(0x100147bc, 16)        # IMDCT input order per subband
    t["GAIN_EXP"] = pe.s16(0x100147dc, 16)         # gain level code -> exponent
    t["SHAPE_GROUP"] = pe.u8(0x10015aaf, 34)       # [n] -> group count for n units; [1+i] -> group of unit i
    t["WL_SHAPES"] = pe.s8(0x100252f8, 8 * 16 * 9)
    t["SF_SHAPES"] = pe.s8(0x10024fe8, 64 * 9)
    t["WL_WEIGHTS"] = pe.s8(0x10025758, 7 * 32)
    t["SF_WEIGHTS"] = pe.s32(0x10014998, 3 * 32)
    t["CT_REMAP"] = pe.u8(0x100178bc, 64)
    t["CT_BITS"] = pe.u8(0x10024164, 2)
    t["SB_POWGRPS"] = pe.u8(0x10023837, 18)        # [nsb] -> number of power groups - 1
    t["SB_POWGRP"] = pe.u8(0x10023838, 16)         # subband -> power group
    t["TONE_MODE_BITS"] = pe.u8(0x10014874, 2)
    t["AMPSF_MODE_BITS"] = pe.u8(0x100148ac, 2)
    t["AMPIDX_MODE_BITS"] = pe.u8(0x100148d4, 2)
    # --- Huffman descriptors ---
    t["H_WL"] = huff_list(pe, 0x1004ed40, 4)
    t["H_SF_DELTA"] = huff_list(pe, 0x1004ece0, 4)
    t["H_SF"] = huff_list(pe, 0x1004ec80, 4)
    t["H_CT"] = huff_list(pe, 0x1004ec20, 4)       # ec20, ec38, ec50, ec68
    t["H_SPEC"] = [None] + [huff(pe, 0x1004e188 + 24 * i) for i in range(1, 113)]
    t["H_TONE"] = huff_list(pe, 0x1004e0ac, 10)    # e0ac .. e184
    t["H_GAIN"] = huff_list(pe, 0x10050ae0, 11)    # ae0 .. bd0
    # --- dequantisation / power compensation / noise ---
    t["F_SF"] = pe.f32(0x10025f00, 64)             # scale factor index -> scale
    t["F_WL_MANT"] = pe.f32(0x10025ce0, 8)         # word length -> quantiser step
    t["F_PWR_LEVELS"] = pe.f32(0x10025c70, 16)     # power level -> noise gain
    t["PWR_SB_QU"] = pe.u8(0x10025cb0, 16)         # subband -> first unit that gets noise
    t["NOISE"] = pe.s16(0x10023868, 1024)          # noise samples (Q15)
    t["NOISE_OFS"] = pe.u16(0x10014b58, 48)        # time-domain noise start per subband
    # --- IMDCT (128 coefficients -> 256 samples per subband) ---
    t["F_IMDCT_C"] = pe.f32(pe.u32(0x1004f7a8, 1)[0], 127)
    t["F_IMDCT_S"] = pe.f32(pe.u32(0x1004f7a4, 1)[0], 127)
    t["IMDCT_ORDER"] = pe.s16(pe.u32(0x1004f7a0, 1)[0], 128)
    t["F_WINDOWS"] = [pe.f32(a, 256) for a in (0x1004f7b0, 0x1004fbb0, 0x1004ffb0, 0x100503b0)]
    # --- gain compensation: interpolation steps (3 per level difference;
    # sized to cover every difference the level codes can produce) ---
    t["F_GAIN_STEP"] = pe.f32(0x10025848, 96)
    # --- tones ---
    t["F_TONE_RAMP"] = pe.f32(0x10025838, 4)
    t["F_AMP_SF"] = pe.f32(0x10026000, 64)
    t["F_AMP_IDX"] = pe.f32(0x10026100, 16)
    # --- 16-band synthesis filter bank ---
    t["F_QMF_PRE"] = pe.f32(0x100507b0, 16)        # input weights
    t["F_QMF_DCT"] = pe.f32(0x100507f0, 19)        # butterfly constants
    t["F_QMF_WIN"] = pe.f32(0x10025900, 192)       # 0x10025900 .. 0x10025c00: filter coefficients
    t["QMF_IDX"] = [pe.s32(0x10025c00 + 12 * k, 3) for k in range(7)]
    # --- scalar constants (float bit patterns, doubles as numbers) ---
    t["C_Q15"] = pe.f32(0x10014bb8, 1)[0]          # 1/32768
    t["C_HALF"] = pe.f32(0x10014618, 1)[0]
    t["C_ONE"] = pe.f32(0x1001461c, 1)[0]
    t["C_MINUS_ONE"] = pe.f32(0x10015800, 1)[0]
    t["C_ZERO"] = pe.f32(0x10014bdc, 1)[0]
    t["C_SINE_STEP"] = pe.f32(0x10014bd8, 1)[0]    # sine table: fsin(i * this)
    t["C_WIN_STEP"] = pe.f32(0x10014bd4, 1)[0]     # window: (1 - fcos(i * this * C_WIN_2PI)) * 0.5
    t["C_WIN_2PI"] = pe.f32(0x10014bd0, 1)[0]
    t["D_WIN_ONE"] = list(struct.unpack("<d", pe.read(0x10014bc8, 8)))[0]
    t["D_WIN_HALF"] = list(struct.unpack("<d", pe.read(0x10014bc0, 8)))[0]
    return t


def build(dll_bytes: bytes) -> dict:
    """The tables of lcstde.ax (its bytes), with their source recorded.
    Raises ValueError for any other build of the DLL."""
    digest = hashlib.sha256(dll_bytes).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError(f"unexpected build of {DLL_NAME} (sha256 {digest}); "
                         f"only sha256 {EXPECTED_SHA256} is known")
    tables = extract(PE(dll_bytes))
    tables["_source"] = {"file": DLL_NAME, "sha256": digest}
    return tables


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tables-dir", type=Path, default=DEFAULT_TABLES_DIR,
        help=f"folder of research dumps holding {DLL_NAME}; default: $OPENEVP_TABLE_DUMPS",
    )
    parser.add_argument("--dll", type=Path, default=None,
                        help=f"the {DLL_NAME} to read (default: <tables-dir>/{DLL_NAME})")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help=f"output path for the combined, git-ignored table data file (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)
    dll = args.dll
    if dll is None:
        if args.tables_dir is None:
            parser.error("--tables-dir (or $OPENEVP_TABLE_DUMPS) or --dll is required")
        dll = args.tables_dir / DLL_NAME
    if not dll.is_file():
        print(f"error: {dll} not found", file=sys.stderr)
        return 1
    try:
        tables = build(dll.read_bytes())
    except ValueError as e:
        print(f"error: {dll}: {e}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(tables, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {len(tables) - 1} tables to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
