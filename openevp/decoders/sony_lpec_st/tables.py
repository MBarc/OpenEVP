"""LPEC ST decoder tables: load the extracted ones, generate the run-time ones.

Extracted tables are read verbatim from Sony's ``lcstde.ax`` (Digital Voice
Editor's LPEC ST filter) by ``tools/import_lpec_st_tables.py``, which writes
them into ``openevp/decoders/sony_lpec_st/data/lpec_st_tables.json``
(git-ignored, generated locally, never committed; shipped in the build like
the LP decoder's ``lpec_tables.json``). ``load()`` raises TablesMissing when
it is absent, so the package stays importable without it; only decoding
needs the data.

Floats are stored in the JSON as IEEE-754 single-precision bit patterns
and converted here. Two tables are not in the DLL image because the DLL
computes them when it is loaded (x87 fsin/fcos): the tone sine table and
the tone window; they are generated below with the exact x87 emulation.
"""

from __future__ import annotations

import json
from array import array
import os
import struct
from fractions import Fraction
from pathlib import Path

from . import x87

DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "data" / "lpec_st_tables.json"


class TablesMissing(RuntimeError):
    """The extracted table data file is not present (or not usable).

    The message is meant for users ("this build does not include the LPEC ST
    table data"); ``path`` is the file that was looked for and ``hint`` tells
    a developer how to create it."""

    MESSAGE = "this build does not include the LPEC ST table data"
    HINT = ("run tools/import_lpec_st_tables.py (it needs a copy of Sony's lcstde.ax) "
            "to create it")

    def __init__(self, message=None, path=None, hint=None):
        super().__init__(message or self.MESSAGE)
        self.path = path
        self.hint = hint or self.HINT


class TablesInvalid(TablesMissing):
    """The table data file exists but is damaged: not JSON, or a table
    missing or of the wrong shape or type."""

    MESSAGE = "the LPEC ST table data in this build is damaged"


class Huff:
    """A Huffman descriptor: lookup of ``maxbits`` peeked bits -> symbol,
    code length per symbol, and the spectrum layout fields."""

    __slots__ = ("maxbits", "lut", "lens", "n", "group", "shift", "unsigned", "bits", "mask")

    def __init__(self, d):
        self.maxbits = d["maxbits"]
        self.lut = tuple(d["lut"])
        self.lens = tuple(d["lens"])
        self.n = d["n"]
        self.group = d["group"]
        self.shift = d["shift"]
        self.unsigned = d["unsigned"]
        self.bits = d["bits"]
        self.mask = d["mask"]


def _floats(bits):
    return list(struct.unpack(f"<{len(bits)}f", struct.pack(f"<{len(bits)}I", *bits)))


def f32(x: float) -> float:
    a = array("f", (x,))
    return a[0]


class Tables:
    pass


_HUFF_TABLES = {"H_WL", "H_SF_DELTA", "H_SF", "H_CT", "H_SPEC", "H_TONE", "H_GAIN"}

# Every table the decoder indexes, with its length (outer length for nested
# ones); load() rejects a file that lacks one or has another size, so a
# damaged file fails up front instead of deep inside a decode.
_SIZES = {
    "XOR_KEYS": 64, "QU_TO_SB": 33, "QU_LEN": 32, "QU_START": 33, "SB_QU": 17,
    "SB_REVERSE": 16, "GAIN_EXP": 16, "SHAPE_GROUP": 34, "WL_SHAPES": 1152,
    "SF_SHAPES": 576, "WL_WEIGHTS": 224, "SF_WEIGHTS": 96, "CT_REMAP": 64, "CT_BITS": 2,
    "SB_POWGRPS": 18, "SB_POWGRP": 16, "TONE_MODE_BITS": 2, "AMPSF_MODE_BITS": 2,
    "AMPIDX_MODE_BITS": 2, "H_WL": 4, "H_SF_DELTA": 4, "H_SF": 4, "H_CT": 4,
    "H_SPEC": 113, "H_TONE": 10, "H_GAIN": 11, "F_SF": 64, "F_WL_MANT": 8,
    "F_PWR_LEVELS": 16, "PWR_SB_QU": 16, "NOISE": 1024, "NOISE_OFS": 48,
    "F_IMDCT_C": 127, "F_IMDCT_S": 127, "IMDCT_ORDER": 128, "F_WINDOWS": 4,
    "F_GAIN_STEP": 96, "F_TONE_RAMP": 4, "F_AMP_SF": 64, "F_AMP_IDX": 16,
    "F_QMF_PRE": 16, "F_QMF_DCT": 19, "F_QMF_WIN": 192, "QMF_IDX": 7,
}
_SCALARS = ("C_Q15", "C_HALF", "C_ONE", "C_MINUS_ONE", "C_ZERO", "C_SINE_STEP",
            "C_WIN_STEP", "C_WIN_2PI", "D_WIN_ONE", "D_WIN_HALF")

# Inner shapes of the nested tables (the C core and dsp.py index them at fixed sizes).
_ROWS = {"F_WINDOWS": 256, "QMF_IDX": 3}


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _content_problem(name, v):
    """Why table ``name``'s entries are unusable, or None: every entry an
    integer (floats are stored as bit patterns), nested rows of the right
    length, and Huffman descriptors whose lookup table fits their code lengths."""
    if name in _HUFF_TABLES:
        for i, d in enumerate(v):
            if d is None and name == "H_SPEC" and i == 0:
                continue
            if not isinstance(d, dict):
                return f"entry {i} is not a descriptor"
            fields = ("maxbits", "n", "group", "shift", "unsigned", "bits", "mask")
            if not all(_is_int(d.get(k)) for k in fields):
                return f"descriptor {i} is incomplete"
            lut, lens = d.get("lut"), d.get("lens")
            if not (isinstance(lut, list) and isinstance(lens, list) and lens
                    and len(lut) == 1 << d["maxbits"]):
                return f"descriptor {i} has a malformed lookup table"
            if not all(_is_int(x) and 0 <= x < len(lens) for x in lut) or not all(_is_int(x) for x in lens):
                return f"descriptor {i} has a malformed lookup table"
        return None
    if name in _ROWS:
        if not all(isinstance(r, list) and len(r) == _ROWS[name] and all(_is_int(x) for x in r) for r in v):
            return f"rows of {_ROWS[name]} integers expected"
        return None
    if not all(_is_int(x) for x in v):
        return "integers expected"
    return None


_CACHE: dict = {}


def _generate(t: Tables) -> None:
    """Tables the DLL builds when it is loaded (FUN_10007920), x87 exact."""
    # tone sine: fstp dword (fsin(i * step)); i * step rounded to 53 bits
    step = t.C_SINE_STEP
    t.TONE_SINE = [x87.round_to_float32(x87.sincos_ext(i * step)[0]) for i in range(2048)]
    # tone window: fstp dword ((1.0 - fcos((i * s) * 2pi)) * 0.5)
    win = []
    for i in range(256):
        c = x87.sincos_ext((i * t.C_WIN_STEP) * t.C_WIN_2PI)[1]
        d = x87.round_to_double(Fraction(t.D_WIN_ONE) - c)
        win.append(f32(d * t.D_WIN_HALF))
    t.TONE_WINDOW = win


def load(path: Path = DEFAULT_DATA_FILE) -> Tables:
    path = Path(path)
    if not path.is_file():
        raise TablesMissing(path=path)
    st = path.stat()
    key = (os.path.normcase(os.path.abspath(str(path))), st.st_mtime_ns, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TablesInvalid(path=path, hint=f"{path.name} could not be read: {e}") from e
    if not isinstance(raw, dict):
        raise TablesInvalid(path=path, hint=f"{path.name} is not a JSON object")
    for name, size in _SIZES.items():
        v = raw.get(name)
        if not isinstance(v, list) or len(v) != size:
            raise TablesInvalid(path=path, hint=f"table {name} missing or not {size} entries")
        problem = _content_problem(name, v)
        if problem:
            raise TablesInvalid(path=path, hint=f"table {name}: {problem}")
    for name in _SCALARS:
        if not isinstance(raw.get(name), (int, float)) or isinstance(raw.get(name), bool):
            raise TablesInvalid(path=path, hint=f"constant {name} missing")
    t = Tables()
    for name, v in raw.items():
        if name.startswith("_"):
            continue
        if name in _HUFF_TABLES:
            v = [Huff(d) if d is not None else None for d in v]
        elif name.startswith("F_"):
            v = [_floats(r) for r in v] if v and isinstance(v[0], list) else _floats(v)
        elif name.startswith("C_"):
            v = _floats([v])[0]
        if name == "XOR_KEYS":
            v = bytes(v)
        setattr(t, name, v)
    _generate(t)
    _CACHE[key] = t
    return t
