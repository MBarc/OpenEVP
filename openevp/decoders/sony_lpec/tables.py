"""LPEC decoder tables: load the extracted ones, generate the derivable ones.

See docs/lpec.md, section "Tables". That section splits every table the
decoder needs into two groups:

- **Extracted** tables have no known formula; they come from
  JSON dumps of the tables in Digital Voice Editor's ``LPEC.dll``, by way of ``tools/import_lpec_tables.py``, which writes them into
  ``openevp/decoders/sony_lpec/data/lpec_tables.json`` (git-ignored, generated locally, never
  committed).
- **Derivable** tables are computed here from the formulas in docs/lpec.md,
  either from nothing (the "Tables generated at run time" section, plus a
  few trivial or constant static tables) or from an extracted table (the
  Q16/Q15 codebooks, which are rounded copies of the double codebooks).

``load()`` returns a single ``Tables`` object with one attribute per table,
named after the doc's short names (``C1``, ``GAIN``, ``S2048``, ...). It
raises ``TablesMissing`` when the extracted data file is not present, so
``openevp.decoders.sony_lpec`` stays importable without it; only decoding needs the data.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import List, Optional, Sequence

from . import x87
from .config import LP, SP, Config

Row = Sequence[float]        # a tuple once loaded (see _freeze)
IntRow = Sequence[int]

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_DATA_FILE = DATA_DIR / LP.data_file          # LPEC LP (8000 Hz)
SP_DATA_FILE = DATA_DIR / SP.data_file               # LPEC SP (16000 Hz)

# Extracted tables, loaded verbatim from the data file (see the "Static
# tables" section of docs/lpec.md; every one of these is marked "no,
# extract" there), with the shape the decoder relies on: (rows, columns)
# for a matrix, (length,) for a vector. The C core (_lpec.c) and the
# pure-Python modules index these at fixed sizes, so load() rejects a file
# whose tables have any other shape instead of letting it reach them.
_EXTRACTED_SHAPES = {
    "C1": (64, 10), "C2": (64, 10), "C3": (64, 10),
    "PT": (64, 3),
    "SHAPES": (128, 8),
    "GAIN": (128,),
    "BG1": (64, 8), "BG2": (64, 8),
    "VQ2": (256, 2), "VQ4": (256, 4), "VQ8": (256, 8),
    "AB": (64, 8),
    "NA": (1024,), "NB": (1024,),
}
_EXTRACTED_NAMES = tuple(_EXTRACTED_SHAPES)
_INT_TABLES = frozenset({"AB"})     # int16 values; the rest are doubles


def extracted_shapes(cfg: Config = LP) -> dict:
    """The extracted tables of a configuration and their shapes: LP's are
    _EXTRACTED_SHAPES; LPEC SP has a fourth LSP stage, 16-dimensional LSP
    codebooks and 10 bands (docs/lpec.md, "LPEC SP (16000 Hz)")."""
    if cfg == LP:
        return dict(_EXTRACTED_SHAPES)
    shapes = {f"C{k + 1}": (64, cfg.order) for k in range(cfg.lsp_stages)}
    for name, shape in _EXTRACTED_SHAPES.items():
        if not name.startswith("C"):
            shapes[name] = shape
    shapes.update({"BG1": (64, cfg.bands), "BG2": (64, cfg.bands), "AB": (64, cfg.bands)})
    return shapes


class TablesMissing(RuntimeError):
    """The extracted table data file is not present (or not usable).

    Raised by :func:`load`. The message is meant for users ("this build
    does not include the LPEC table data"); ``path`` is the file that was
    looked for and ``hint`` tells a developer how to create it
    (``tools/import_lpec_tables.py``, which needs a folder of table dumps
    from ``LPEC.dll``).
    """

    MESSAGE = "this build does not include the LPEC table data"
    HINT = ("run tools/import_lpec_tables.py (it needs a folder of table "
            "dumps from LPEC.dll) to create it")

    def __init__(self, message=None, path=None, hint=None):
        super().__init__(message or self.MESSAGE)
        self.path = path
        self.hint = hint or self.HINT


class TablesInvalid(TablesMissing):
    """The table data file exists but is damaged: not JSON, a table
    missing, or a table with the wrong shape or element type."""

    MESSAGE = "the LPEC table data in this build is damaged"


@dataclass(frozen=True)
class Tables:
    """One attribute per table in docs/lpec.md, "Tables", for one
    configuration (``RATE`` 8000: LPEC LP, 16000: LPEC SP)."""

    # Every table is stored as a tuple (of tuples, for a matrix): the cached
    # Tables object is shared by every decode, on any thread.

    RATE: int               # 8000 or 16000: selects config.LP / config.SP

    # --- Extracted (from data/lpec_tables.json / data/lpec_sp_tables.json) ---
    C: Sequence             # LSP codebooks, one per stage, double, 64 x order
    PT: Sequence[Row]       # pitch taps, double, 64 x 3
    SHAPES: Sequence[Row]   # temporal shapes, double, 128 x 8
    GAIN: Row               # global gain, double, 128
    BG1: Sequence[Row]      # band gain stage 1, double, 64 x bands
    BG2: Sequence[Row]      # band gain stage 2, double, 64 x bands
    VQ2: Sequence[Row]      # coefficient VQ, 2-dim, double, 256 x 2
    VQ4: Sequence[Row]      # coefficient VQ, 4-dim, double, 256 x 4
    VQ8: Sequence[Row]      # coefficient VQ, 8-dim, double, 256 x 8
    AB: Sequence[IntRow]    # allocation base, int16, 64 x bands
    NA: Row                 # noise A, double, 1024
    NB: Row                 # noise B, double, 1024

    # --- Derived from the extracted codebooks above ---
    D: Sequence             # LSP codebooks, int16, one per stage: rhu(C_k * 2^shift_k)
    PQ: Sequence[IntRow]    # pitch taps, Q15 int16, 64 x 3: rhu(PT*32768)

    # --- Trivial or constant static tables ---
    R: int                  # allocation reference (128 LP, 432 SP)
    S2048: IntRow           # int16 sine, period 2048, 2049 entries
    S1536: IntRow           # int16 sine, period 1536, 1537 entries
    DEFAULT_SHAPE: Row      # 8 doubles, all 1.0
    ZERO_TAPS_F: Row        # 3 doubles, all 0.0 (lag 0)
    ZERO_TAPS_Q15: IntRow   # 3 int16, all 0 (lag 0)

    # --- Generated at (the equivalent of) first InitDecoder ---
    WIN: Row                # F-point sine window v (F = frame)
    WIN_SQ: Row             # its square, v2
    WIN_LONG: Row           # 2F-point sine window
    FFT_SIN_2048: Row       # FFT sine, period 2048
    FFT_SIN_1536: Row       # FFT sine, period 1536
    POST: Sequence          # post-twiddle by transform type t (N_t / 2 entries)
    LSP_INIT: Row           # initial LSPs, order + 1 doubles

    @property
    def config(self) -> Config:
        return SP if self.RATE == SP.rate else LP

    # LP names (docs/lpec.md) for the stage codebooks and run-time tables.
    C1 = property(lambda self: self.C[0])
    C2 = property(lambda self: self.C[1])
    C3 = property(lambda self: self.C[2])
    D1 = property(lambda self: self.D[0])
    D2 = property(lambda self: self.D[1])
    D3 = property(lambda self: self.D[2])
    WIN512 = property(lambda self: self._lp(self.WIN))
    WIN512_SQ = property(lambda self: self._lp(self.WIN_SQ))
    WIN1024 = property(lambda self: self._lp(self.WIN_LONG))
    POST256 = property(lambda self: self._lp(self.POST[0]))
    POST384 = property(lambda self: self._lp(self.POST[1]))
    POST512 = property(lambda self: self._lp(self.POST[3]))

    def _lp(self, table):
        if self.RATE != LP.rate:
            raise AttributeError("an LP table name used on the LPEC SP tables")
        return table


# --------------------------------------------------------------------------
# Integer/rounding helpers (docs/lpec.md, "Conventions")
# --------------------------------------------------------------------------

def _rhu(x: float) -> int:
    """rhu(x) = floor(x + 0.5)."""
    return math.floor(x + 0.5)


def _scale_round(table: Sequence[Row], scale: float) -> List[IntRow]:
    """rhu(table * scale), element-wise, keeping the table's shape."""
    return [[_rhu(x * scale) for x in row] for row in table]


# --------------------------------------------------------------------------
# Static, derivable tables (docs/lpec.md, "Static tables in the DLL image")
# --------------------------------------------------------------------------

def _sine_q15(period: int) -> IntRow:
    """int16 sine table of the given period, `period + 1` entries (guard).

    clamp(rhu(32768*sin(2*pi*i/period)), -32767, 32767), i = 0..period.
    """
    out = []
    for i in range(period + 1):
        v = _rhu(32768.0 * math.sin(2.0 * math.pi * i / period))
        out.append(max(-32767, min(32767, v)))
    return out


# --------------------------------------------------------------------------
# Tables generated at run time (docs/lpec.md, "Tables generated at run time")
# --------------------------------------------------------------------------

def _sine_window(n: int) -> tuple:
    """The n-point sine window `v` and its square `v2`.

    v[i]  = sqrt((1 - fcos(((i + 0.5)*6.28318530718)*(1/n)))*0.5)
    v2[i] = (1 - fcos(same))*0.5

    docs/lpec.md notes that "1 - fcos(...)" uses the 64-bit fcos value
    before it is rounded to a double: only the subtraction's *result* is
    rounded, which is what avoids the cancellation error plain double
    arithmetic would introduce for small angles.
    """
    v = []
    v2 = []
    for i in range(n):
        theta = ((i + 0.5) * 6.28318530718) * (1.0 / n)
        cos_ext = x87.fcos_ext(theta)
        diff = float(Fraction(1) - cos_ext)
        y = diff * 0.5
        v2.append(y)
        v.append(math.sqrt(y))
    return v, v2


def _fft_sin_2048() -> Row:
    return [x87.fsin(i * (6.283185307 / 2048)) for i in range(2048)]


def _fft_sin_1536() -> Row:
    return [x87.fsin((2 * i) * (3.1415926535 / 1536)) for i in range(1536)]


def _post_twiddle(n: int) -> Row:
    """Post-twiddle table for transform length n, n/2 entries.

    fsin((2i + 1)*(3.14159265359/(2n))), i = 0..n/2 - 1.
    """
    return [x87.fsin((2 * i + 1) * (3.14159265359 / (2 * n))) for i in range(n // 2)]


def _lsp_init(order: int = 10) -> Row:
    return [i * (0.48 / (order + 1)) for i in range(order + 1)]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

# In-process cache, keyed by (absolute path, mtime, size) so a file that is
# replaced (e.g. a rebuilt tools/import_lpec_tables.py output) is picked up
# on the next load() instead of serving stale data. Parsing + generating the
# tables takes ~0.65 s; audio.dvf_to_wav() calls load() on every recording,
# so without this a WAV export of many recordings, or repeated playback
# clicks, redo that work every single time.
_CACHE: dict = {}


def _check_shape(name: str, value, shape) -> None:
    """Raise TablesInvalid unless ``value`` is a list of ``shape`` (rows,
    columns) or (length,) numbers of the table's type."""
    def bad(what):
        return TablesInvalid(path=None, hint=f"table {name}: {what}")

    want_int = name in _INT_TABLES

    def check_numbers(seq, where):
        for x in seq:
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                raise bad(f"{where} holds a non-number ({type(x).__name__})")
            if want_int and not isinstance(x, int):
                raise bad(f"{where} holds a non-integer")
            if not want_int and not math.isfinite(x):
                raise bad(f"{where} holds a non-finite value")

    if not isinstance(value, list) or len(value) != shape[0]:
        got = len(value) if isinstance(value, list) else type(value).__name__
        raise bad(f"expected {shape[0]} entries, got {got}")
    if len(shape) == 1:
        check_numbers(value, "the table")
        return
    for i, row in enumerate(value):
        if not isinstance(row, list) or len(row) != shape[1]:
            got = len(row) if isinstance(row, list) else type(row).__name__
            raise bad(f"row {i}: expected {shape[1]} entries, got {got}")
        check_numbers(row, f"row {i}")


def _freeze(table):
    """A table as a tuple (of tuples, for a matrix)."""
    if isinstance(table, (list, tuple)):
        return tuple(_freeze(x) if isinstance(x, (list, tuple)) else x for x in table)
    return table


def load(path: Optional[Path] = None, config: Config = LP) -> Tables:
    """Load the extracted tables of ``config`` (LPEC LP by default) and
    generate the derivable ones.

    ``path`` defaults to the configuration's data file (``data/lpec_tables.json``
    for LP, ``data/lpec_sp_tables.json`` for SP), which
    ``tools/import_lpec_tables.py`` writes. Raises TablesMissing if it does not
    exist, and TablesInvalid (a TablesMissing) if it is not valid JSON or any
    table is missing or has the wrong shape. Repeated calls for the same file
    (by path, mtime and size) reuse the first result instead of re-reading
    and re-parsing it; a changed or replaced file is detected and reloaded.
    """
    path = Path(path) if path is not None else DATA_DIR / config.data_file
    if not path.is_file():
        if config == LP:
            raise TablesMissing(path=path)
        raise TablesMissing(f"this build does not include the LPEC {config.name} table data", path=path)

    st = path.stat()
    key = (config.name, os.path.normcase(os.path.abspath(str(path))), st.st_mtime_ns, st.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise TablesInvalid(path=path, hint=f"{path.name} could not be read: {e}") from e
    if not isinstance(raw, dict):
        raise TablesInvalid(path=path, hint=f"{path.name} is not a JSON object")

    extracted = {}
    for name, shape in extracted_shapes(config).items():
        if name not in raw:
            raise TablesInvalid(path=path, hint=f"{path.name} has no table {name}")
        try:
            _check_shape(name, raw[name], shape)
        except TablesInvalid as e:
            e.path = path
            raise
        extracted[name] = raw[name]

    stages = [f"C{k + 1}" for k in range(config.lsp_stages)]
    win, win_sq = _sine_window(config.frame)
    win_long, _win_long_sq = _sine_window(2 * config.frame)  # no square table for 2F
    posts = {n: _post_twiddle(n) for n in sorted(set(config.transform_n))}

    fields = dict(
        C=[extracted.pop(n) for n in stages],
        **extracted,
        D=[_scale_round(raw[n], float(1 << q)) for n, q in zip(stages, config.lsp_q_shifts)],
        PQ=_scale_round(extracted["PT"], 32768.0),
        S2048=_sine_q15(2048),
        S1536=_sine_q15(1536),
        DEFAULT_SHAPE=[1.0] * 8,
        ZERO_TAPS_F=[0.0, 0.0, 0.0],
        ZERO_TAPS_Q15=[0, 0, 0],
        WIN=win,
        WIN_SQ=win_sq,
        WIN_LONG=win_long,
        FFT_SIN_2048=_fft_sin_2048(),
        FFT_SIN_1536=_fft_sin_1536(),
        POST=[posts[n] for n in config.transform_n],
        LSP_INIT=_lsp_init(config.order),
    )
    result = Tables(RATE=config.rate, R=config.alloc_ref[0], **{k: _freeze(v) for k, v in fields.items()})
    _CACHE[key] = result
    return result
