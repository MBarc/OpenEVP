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

DLL_PATH = Path(__file__).resolve().parent / "lpec_core.dll"
_ABI_VERSION = 2
FRAME_SAMPLES = 512

# Table order: the T_* enums in _lpec.c.
_DOUBLE_TABLES = (
    "C1", "C2", "C3", "PT", "SHAPES", "GAIN", "BG1", "BG2", "VQ2", "VQ4",
    "VQ8", "NA", "NB", "WIN512", "WIN512_SQ", "WIN1024", "FFT_SIN_2048",
    "FFT_SIN_1536", "POST256", "POST384", "POST512", "LSP_INIT",
    "DEFAULT_SHAPE",
)
_INT_TABLES = ("D1", "D2", "D3", "PQ", "S2048", "S1536")


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
    lsp_a = frame.lsp_a or (0, 0, 0)
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
    PCM, 512 samples per frame. (Packs them and calls decode_packed; the
    decoder itself packs frame by frame instead, so it never holds a list
    of Frame objects.)"""
    packed = array("i")
    for frame in frames:
        pack_frame(frame, packed)
    return decode_packed(tables, packed, len(frames))


def _table_pointers(tables):
    """The ctypes table arrays (kept alive by the caller) and the two
    pointer arrays lpec_decode takes."""
    dbufs = [(ctypes.c_double * len(v))(*v)
             for v in (_flat(getattr(tables, n)) for n in _DOUBLE_TABLES)]
    ibufs = [(ctypes.c_int32 * len(v))(*v)
             for v in (_flat(getattr(tables, n)) for n in _INT_TABLES)]
    dptrs = (ctypes.POINTER(ctypes.c_double) * len(dbufs))(
        *[ctypes.cast(b, ctypes.POINTER(ctypes.c_double)) for b in dbufs])
    iptrs = (ctypes.POINTER(ctypes.c_int32) * len(ibufs))(
        *[ctypes.cast(b, ctypes.POINTER(ctypes.c_int32)) for b in ibufs])
    return (dbufs, ibufs), dptrs, iptrs


def decode_packed(tables, packed: array, nframes: int, prefix: int = 0) -> bytearray:
    """Decode ``nframes`` packed frame records (``packed``, built with
    pack_frame) with a fresh decoder state.

    Returns a bytearray of ``prefix`` zero bytes followed by the
    little-endian int16 PCM (512 samples per frame). The C core writes the
    PCM straight into that bytearray, so the output exists once; ``prefix``
    leaves room for a header (dvf_to_wav's 44-byte WAV header) without a
    second copy.
    """
    if _lib is None:
        raise RuntimeError(f"{DLL_PATH.name} is not available")
    keep, dptrs, iptrs = _table_pointers(tables)

    out = bytearray(prefix + nframes * FRAME_SAMPLES * 2)
    pcm = (ctypes.c_int16 * (nframes * FRAME_SAMPLES)).from_buffer(out, prefix)
    if not len(packed):
        packed = array("i", [0])
    frame_buf = (ctypes.c_int32 * len(packed)).from_buffer(packed)
    try:
        done = _lib.lpec_decode(dptrs, len(keep[0]), iptrs, len(keep[1]),
                                frame_buf, len(packed), nframes, pcm)
    finally:
        # Release the buffer exports so ``out`` and ``packed`` can be
        # resized or freed by the caller.
        del pcm, frame_buf
    if done != nframes:
        raise RuntimeError(f"lpec_core: decoded {done} of {nframes} frames")
    return out
