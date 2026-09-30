"""Optional C core for the LPEC ST decoder (_lpec_st.c -> lpec_st_core.dll).

The DLL runs the synthesis (dsp.py) and the int16 conversion
(decoder.to_pcm) of each frame bit for bit like the pure-Python modules;
Python still parses the bitstream (bitstream.py) and hands each parsed frame
over as one packed int32 record (``pack_unit``). The decoder state (overlap,
filter-bank history, the previous frame's gain, window and tone data) lives
in a buffer owned by ``CoreDecoder``, one per decode, so decodes on
different threads never share it. Built by tools/build_lpec_core.py; when
the DLL is missing or fails to load, ``available()`` is False and
openevp.decoders.sony_lpec_st.decoder uses pure Python (about 15x slower).
"""

from __future__ import annotations

import ctypes
from array import array
from pathlib import Path
from typing import Optional

DLL_PATH = Path(__file__).resolve().parent / "lpec_st_core.dll"
_ABI_VERSION = 1
FRAME_SAMPLES = 2048
MAX_WAVES = 48          # MAXW in _lpec_st.c: waves in one band (at most 48 per frame)

# Table order: the D_* / I_* enums in _lpec_st.c.
_DOUBLE_TABLES = (
    "F_SF", "F_WL_MANT", "F_PWR_LEVELS", "F_IMDCT_C", "F_IMDCT_S", "F_WINDOWS",
    "F_GAIN_STEP", "F_TONE_RAMP", "F_AMP_SF", "F_AMP_IDX", "F_QMF_PRE", "F_QMF_DCT",
    "F_QMF_WIN", "TONE_SINE", "TONE_WINDOW",
)
_SCALARS = ("C_Q15", "C_HALF", "C_MINUS_ONE", "C_ZERO")
_INT_TABLES = (
    "QU_START", "QU_LEN", "SB_QU", "SB_REVERSE", "GAIN_EXP", "SB_POWGRP", "PWR_SB_QU",
    "NOISE", "NOISE_OFS", "IMDCT_ORDER",
)

_I32P = ctypes.POINTER(ctypes.c_int32)
_DP = ctypes.POINTER(ctypes.c_double)


def record_size() -> int:
    """The int32 count of one pack_unit() record (the DLL's RECORD must match)."""
    unit = 9 + 4 * 16
    channel = 32 * 3 + 5 + 16 + 16 * (1 + 7 + 7) + 16 * (6 + 4 * MAX_WAVES) + 2048
    return unit + 2 * channel


def _load() -> Optional[ctypes.CDLL]:
    try:
        lib = ctypes.CDLL(str(DLL_PATH))
        lib.lst_abi_version.restype = ctypes.c_int
        lib.lst_abi_version.argtypes = []
        if lib.lst_abi_version() != _ABI_VERSION:
            return None
        for name in ("lst_state_size", "lst_record_size"):
            fn = getattr(lib, name)
            fn.restype = ctypes.c_int
            fn.argtypes = []
        lib.lst_init.restype = ctypes.c_int
        lib.lst_init.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DP), ctypes.POINTER(ctypes.c_int),
                                 ctypes.c_int, ctypes.POINTER(_I32P), ctypes.POINTER(ctypes.c_int),
                                 ctypes.c_int]
        lib.lst_reset.restype = None
        lib.lst_reset.argtypes = [ctypes.c_void_p]
        lib.lst_frame.restype = ctypes.c_int
        lib.lst_frame.argtypes = [ctypes.c_void_p, _I32P, ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_int16)]
        if lib.lst_record_size() != record_size():
            return None                 # a DLL built for another record layout: use pure Python
        return lib
    except (OSError, AttributeError):
        return None


_lib = _load()
RECORD_SIZE = _lib.lst_record_size() if _lib is not None else 0


def available() -> bool:
    """True when lpec_st_core.dll loaded and has the expected interface."""
    return _lib is not None


def _flat(table) -> list:
    if table and isinstance(table[0], (list, tuple)):
        return [v for row in table for v in row]
    return list(table)


_NO_WAVES = [0] * (4 * MAX_WAVES)


def pack_unit(u) -> array:
    """One parsed frame (bitstream.Unit) as the int32 record lst_frame reads
    (the layout of ``unpack`` in _lpec_st.c)."""
    rec = [u.ncoded_qu, u.ncoded_sb, u.nsb, u.mute, u.noise_present, u.noise_level,
           u.noise_table, u.tones_present, u.amp_mode]
    rec += _exact(u.swap, 16, "swap")
    rec += _exact(u.negate, 16, "negate")
    rec += _exact(u.tone_negate, 16, "tone_negate")
    share = [0] * 16
    for b in u.share_prev_env:
        share[b] = 1
    rec += share
    for c in u.ch:
        rec += _exact(c.wl, 32, "wl")
        rec += _exact(c.sf, 32, "sf")
        rec += _exact(c.ct, 32, "ct")
        rec += _exact(c.power, 5, "power")
        rec += _exact(c.wnd, 16, "wnd")
        for g in c.gain:
            rec.append(g.npoints)
            rec += _exact(g.loc, 7, "gain loc")
            rec += _exact(g.lev, 7, "gain lev")
        for tb in c.tones:
            waves = tb.waves
            nw = len(waves)
            rec += (tb.has_start, tb.has_stop, tb.start_pos, tb.stop_pos, tb.nwavs, nw)
            if not nw:
                rec += _NO_WAVES
                continue
            if nw > MAX_WAVES:
                raise ValueError(f"a tone band has {nw} waves (at most {MAX_WAVES})")
            fill = [0] * (MAX_WAVES - nw)
            rec += [w.amp_sf for w in waves] + fill
            rec += [w.amp_idx for w in waves] + fill
            rec += [w.phase for w in waves] + fill
            rec += [w.freq for w in waves] + fill
        rec += _exact(c.spec, 2048, "spec")
    return array("i", rec)


def _exact(values, n, name):
    if len(values) != n:
        raise ValueError(f"{name} has {len(values)} entries, not {n}")
    return values


class CoreError(RuntimeError):
    """lst_frame refused a frame record or its tables (a bug, not bad audio:
    the pure-Python decoder would fail the same way)."""


class CoreDecoder:
    """One C decoder state (lst_state_size() bytes), initialised from ``tables``."""

    def __init__(self, tables):
        if _lib is None:
            raise RuntimeError(f"{DLL_PATH.name} is not available")
        self._mem = ctypes.create_string_buffer(_lib.lst_state_size())
        dvals = [_flat(getattr(tables, n)) for n in _DOUBLE_TABLES]
        dvals.append([getattr(tables, n) for n in _SCALARS])
        ivals = [_flat(getattr(tables, n)) for n in _INT_TABLES]
        dbufs = [(ctypes.c_double * len(v))(*v) for v in dvals]
        ibufs = [(ctypes.c_int32 * len(v))(*v) for v in ivals]
        dptrs = (_DP * len(dbufs))(*[ctypes.cast(b, _DP) for b in dbufs])
        iptrs = (_I32P * len(ibufs))(*[ctypes.cast(b, _I32P) for b in ibufs])
        dlens = (ctypes.c_int * len(dvals))(*[len(v) for v in dvals])
        ilens = (ctypes.c_int * len(ivals))(*[len(v) for v in ivals])
        if _lib.lst_init(self._mem, dptrs, dlens, len(dvals), iptrs, ilens, len(ivals)) != 0:
            raise CoreError("lpec_st_core: the tables have the wrong shape or content")
        self._pcm = (ctypes.c_int16 * (2 * FRAME_SAMPLES))()
        self._float = (ctypes.c_float * (2 * FRAME_SAMPLES))()

    def reset(self) -> None:
        _lib.lst_reset(self._mem)

    def _run(self, u, want_float: bool):
        rec = pack_unit(u)
        buf = (ctypes.c_int32 * len(rec)).from_buffer(rec)
        try:
            rc = _lib.lst_frame(self._mem, buf, len(rec), self._float if want_float else None,
                                None if want_float else self._pcm)
        finally:
            del buf
        if rc != 0:
            raise CoreError(f"lpec_st_core: frame refused (code {rc})")

    def frame_pcm(self, u) -> bytes:
        """Synthesize a parsed frame; its interleaved int16 PCM (little-endian)."""
        self._run(u, False)
        return bytes(self._pcm)

    def frame_float(self, u):
        """Synthesize a parsed frame; its two float32 channels (tests)."""
        self._run(u, True)
        f = list(self._float)
        return f[:FRAME_SAMPLES], f[FRAME_SAMPLES:]
