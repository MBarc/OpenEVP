"""Optional C core for the LPEC decoder (openevp/decoders/sony_lpec/_lpec.c -> lpec_core.dll).

The DLL runs the per-frame parameter-to-PCM pipeline (params, synthesis,
the x87 fcos emulation) bit for bit like the pure-Python modules; Python
still parses the bitstream (openevp.decoders.sony_lpec.bitstream) and hands the parsed
frames over in one packed int32 array. Built by tools/build_lpec_core.py;
when the DLL is missing or fails to load, ``available()`` is False and
openevp.decoders.sony_lpec.decoder uses pure Python (~60x slower; st25.audio.status() says
so in a frozen build, where the DLL should always be present).
"""

from __future__ import annotations

import ctypes
from array import array
from pathlib import Path
from typing import List, Optional

from . import bitstream
from .config import LP, Config

DLL_PATH = Path(__file__).resolve().parent / "lpec_core.dll"
_ABI_VERSION = 3
FRAME_SAMPLES = LP.frame      # LP's; a decode uses its tables' config.frame

# Table order: the T_* enums in _lpec.c. "C" and "D" are the per-stage
# codebooks (C1..C4, D1..D4: a 3-stage configuration passes its third twice),
# "POST" the post-twiddle tables by transform type.
_DOUBLE_TABLES = (
    "C", "PT", "SHAPES", "GAIN", "BG1", "BG2", "VQ2", "VQ4",
    "VQ8", "NA", "NB", "WIN", "WIN_SQ", "WIN_LONG", "FFT_SIN_2048",
    "FFT_SIN_1536", "POST", "LSP_INIT", "DEFAULT_SHAPE",
)
_INT_TABLES = ("D", "PQ", "S2048", "S1536")


def _load() -> Optional[ctypes.CDLL]:
    try:
        lib = ctypes.CDLL(str(DLL_PATH))
        lib.lpec_abi_version.restype = ctypes.c_int
        lib.lpec_abi_version.argtypes = []
        if lib.lpec_abi_version() != _ABI_VERSION:
            return None
        lib.lpec_fcos.restype = ctypes.c_double
        lib.lpec_fcos.argtypes = [ctypes.c_double]
        lib.lpec_decode.restype = ctypes.c_int
        lib.lpec_decode.argtypes = [
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_double)), ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_int32)), ctypes.c_int,
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
        ]
        return lib
    except (OSError, AttributeError):
        return None


_lib = _load()


def available() -> bool:
    """True when lpec_core.dll loaded and has the expected interface."""
    return _lib is not None


def fcos(x: float) -> float:
    """The DLL's x87 fcos emulation (for tests; see openevp.decoders.sony_lpec.x87.fcos)."""
    return _lib.lpec_fcos(x)


def _flat(table) -> list:
    if table and isinstance(table[0], (list, tuple)):
        return [v for row in table for v in row]
    return list(table)


def pack_frame(frame: bitstream.Frame, out: array) -> None:
    """Append one frame record (layout read by decode_frame in _lpec.c)."""
    lsp_a = frame.lsp_a or (0,) * len(frame.lsp_b)
    lag_a, pg_a = frame.pitch_a if frame.pitch_a is not None else (0, None)
    lag_b, pg_b = frame.pitch_b
    flags = frame.shape_flags or (0, 0)
    idx = frame.shape_indices or (None, None)
    out.extend((
        frame.mode, frame.mid_frame_lsp, *frame.lsp_b, *lsp_a,
        lag_a, pg_a or 0, lag_b, pg_b or 0,
        flags[0], flags[1], idx[0] or 0, idx[1] or 0,
        len(frame.blocks),
    ))
    for b in frame.blocks:
        a = b.alloc
        out.extend((b.slot, b.type, b.global_gain, b.band_gain1, b.band_gain2))
        out.extend(a.n4)
        out.extend(a.n2)
        out.extend(a.n1)
        out.extend(a.ns)
        out.extend((a.k0, a.k1, a.k2, a.ks))
        out.extend(b.vq2)
        out.extend(b.vq4)
        out.extend(b.vq8)
        out.extend(b.signs)


def decode_frames(tables, frames: List[bitstream.Frame]) -> bytearray:
    """Decode parsed frames with a fresh decoder state; little-endian int16
    PCM, a frame's samples (LP 512, SP 1024) per frame. (Packs them and calls decode_packed; the
    decoder itself packs frame by frame instead, so it never holds a list
    of Frame objects.)"""
    packed = array("i")
    for frame in frames:
        pack_frame(frame, packed)
    return decode_packed(tables, packed, len(frames))


def _config_array(cfg: Config):
    """The configuration as the int32 array lpec_decode takes (CFG_* in _lpec.c)."""
    values = [cfg.frame, cfg.order, cfg.bands, cfg.lsp_stages, cfg.lag_bits,
              *cfg.transform_n, *cfg.band_width, *cfg.first_coded, *cfg.end_coded,
              *cfg.overlap]
    return (ctypes.c_int32 * len(values))(*values)


def _expand(tables, name):
    """The tables behind one T_* slot group, in enum order."""
    value = getattr(tables, name)
    if name in ("C", "D"):
        stages = list(value)
        return stages + [stages[-1]] * (4 - len(stages))
    if name == "POST":
        return list(value)
    return [value]


def _shape(v, depth):
    """(len, len of each row, ...) of a nested table, or None if ragged."""
    out = [len(v)]
    for _ in range(depth - 1):
        lengths = {len(x) for x in v}
        if len(lengths) != 1:
            return None
        v = v[0]
        out.append(lengths.pop())
    return tuple(out)


def check_tables(tables) -> None:
    """Raise ValueError unless every table has the size its configuration
    needs: the C core indexes the tables by the configuration it is given
    (order, bands, frame, transform lengths) and cannot see their lengths."""
    cfg = tables.config
    want = {
        "C": (cfg.lsp_stages, 64, cfg.order), "D": (cfg.lsp_stages, 64, cfg.order),
        "BG1": (64, cfg.bands), "BG2": (64, cfg.bands), "AB": (64, cfg.bands),
        "PT": (64, 3), "PQ": (64, 3), "SHAPES": (128, 8), "GAIN": (128,),
        "VQ2": (256, 2), "VQ4": (256, 4), "VQ8": (256, 8), "NA": (1024,), "NB": (1024,),
        "WIN": (cfg.frame,), "WIN_SQ": (cfg.frame,), "WIN_LONG": (2 * cfg.frame,),
        "FFT_SIN_2048": (2048,), "FFT_SIN_1536": (1536,), "S2048": (2049,), "S1536": (1537,),
        "LSP_INIT": (cfg.order + 1,), "DEFAULT_SHAPE": (8,),
    }
    for name, dims in want.items():
        got = _shape(getattr(tables, name), len(dims))
        if got != dims:
            raise ValueError(f"LPEC {cfg.name} table {name} has shape {got}; the configuration needs {dims}")
    posts = _shape(tables.POST, 1)
    if posts != (4,) or any(len(tables.POST[t]) != n // 2 for t, n in enumerate(cfg.transform_n)):
        raise ValueError(f"LPEC {cfg.name} post-twiddle tables do not match the transform lengths")


def _table_pointers(tables):
    """The ctypes table arrays (kept alive by the caller) and the two
    pointer arrays lpec_decode takes."""
    check_tables(tables)
    dbufs = [(ctypes.c_double * len(v))(*v)
             for v in (_flat(t) for n in _DOUBLE_TABLES for t in _expand(tables, n))]
    ibufs = [(ctypes.c_int32 * len(v))(*v)
             for v in (_flat(t) for n in _INT_TABLES for t in _expand(tables, n))]
    dptrs = (ctypes.POINTER(ctypes.c_double) * len(dbufs))(
        *[ctypes.cast(b, ctypes.POINTER(ctypes.c_double)) for b in dbufs])
    iptrs = (ctypes.POINTER(ctypes.c_int32) * len(ibufs))(
        *[ctypes.cast(b, ctypes.POINTER(ctypes.c_int32)) for b in ibufs])
    return (dbufs, ibufs), dptrs, iptrs


def decode_packed(tables, packed: array, nframes: int, prefix: int = 0) -> bytearray:
    """Decode ``nframes`` packed frame records (``packed``, built with
    pack_frame) with a fresh decoder state.

    Returns a bytearray of ``prefix`` zero bytes followed by the
    little-endian int16 PCM (the configuration's frame length per frame). The C core writes the
    PCM straight into that bytearray, so the output exists once; ``prefix``
    leaves room for a header (dvf_to_wav's 44-byte WAV header) without a
    second copy.
    """
    if _lib is None:
        raise RuntimeError(f"{DLL_PATH.name} is not available")
    keep, dptrs, iptrs = _table_pointers(tables)
    cfg = tables.config
    cfg_array = _config_array(cfg)

    out = bytearray(prefix + nframes * cfg.frame * 2)
    pcm = (ctypes.c_int16 * (nframes * cfg.frame)).from_buffer(out, prefix)
    if not len(packed):
        packed = array("i", [0])
    frame_buf = (ctypes.c_int32 * len(packed)).from_buffer(packed)
    try:
        done = _lib.lpec_decode(cfg_array, len(cfg_array), dptrs, len(keep[0]), iptrs, len(keep[1]),
                                frame_buf, len(packed), nframes, pcm)
    finally:
        # Release the buffer exports so ``out`` and ``packed`` can be
        # resized or freed by the caller.
        del pcm, frame_buf
    if done != nframes:
        raise RuntimeError(f"lpec_core: decoded {done} of {nframes} frames")
    return out
