"""The MP3 decoder's C core (_mp3.c with vendor/minimp3 -> mp3_core.dll), via ctypes.

Built by tools/build_lpec_core.py; release builds always ship it. There is no
pure-Python fallback: when the DLL is missing, fails to load or has another
interface (ABI), ``available()`` is False and MP3 files cannot be decoded.
Each ``Decoder`` owns its own state (mp3c_state_size() bytes), so decodes on
different threads never share one.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Optional

DLL_PATH = Path(__file__).resolve().parent / "mp3_core.dll"
_ABI_VERSION = 1
F_CHANNELS, F_RATE, F_LAYER, F_FRAMES, F_DROPPED, F_COUNT = range(6)


def _load() -> Optional[ctypes.CDLL]:
    try:
        lib = ctypes.CDLL(str(DLL_PATH))
        lib.mp3c_abi_version.restype = ctypes.c_int
        lib.mp3c_abi_version.argtypes = []
        if lib.mp3c_abi_version() != _ABI_VERSION:
            return None
        for name in ("mp3c_state_size", "mp3c_max_frame_samples"):
            fn = getattr(lib, name)
            fn.restype = ctypes.c_int
            fn.argtypes = []
        lib.mp3c_init.restype = None
        lib.mp3c_init.argtypes = [ctypes.c_void_p]
        lib.mp3c_decode.restype = ctypes.c_int
        lib.mp3c_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int64,
                                    ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_int16),
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_int64)]
        return lib
    except (OSError, AttributeError):
        return None


_lib = _load()


def available() -> bool:
    """True when mp3_core.dll loaded and has the expected interface."""
    return _lib is not None


def problem() -> Optional[str]:
    """Why the core cannot be used (a phrase), or None."""
    if _lib is not None:
        return None
    if not DLL_PATH.is_file():
        return f"the MP3 decoder ({DLL_PATH.name}) is missing from this build"
    return f"the MP3 decoder ({DLL_PATH.name}) could not be loaded"


class Decoder:
    """One decode of one file (bytes): ``run(pcm, cap)`` decodes the next frames
    into pcm (a ctypes int16 array of cap sample frames x 2 channels at most)
    and returns the sample frames written (0 at the end). ``pos`` is how far
    into the file it got; ``fmt`` the format and counts (F_*)."""

    def __init__(self, data: bytes, start: int = 0, end: Optional[int] = None):
        if _lib is None:
            raise RuntimeError(problem())
        if not isinstance(data, bytes):
            data = bytes(data)
        self._data = data                        # kept alive: the core reads it in place
        self._len = len(data) if end is None else max(0, min(end, len(data)))
        self._mem = ctypes.create_string_buffer(_lib.mp3c_state_size())
        _lib.mp3c_init(self._mem)
        self._pos = ctypes.c_int64(max(0, min(start, self._len)))
        self.fmt = (ctypes.c_int64 * F_COUNT)()

    @property
    def pos(self) -> int:
        return self._pos.value

    def run(self, pcm, cap: int) -> int:
        n = _lib.mp3c_decode(self._mem, self._data, self._len, ctypes.byref(self._pos), pcm, cap, self.fmt)
        if n < 0:
            raise ValueError("mp3_core refused the call")
        return n


def max_frame_samples() -> int:
    """The most sample frames one MPEG audio frame decodes to (1152)."""
    return _lib.mp3c_max_frame_samples() if _lib is not None else 1152
