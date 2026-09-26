"""The LPEC decoder: per-frame state machine and whole-stream decoding.

See docs/lpec.md: "API behaviour and framing", "Parameter sets", "Frame
layout", "Long-term (pitch) predictor", "LPC synthesis", "End of frame" and
"State between frames".

``Decoder`` is the equivalent of the DLL's decoder object after
``InitDecoder(8000, 6000)``: ``decode_frame(data)`` decodes one frame and
returns its 512 samples plus the number of bytes the frame consumed;
``reset()`` is the partial ``ResetDecoder``. ``decode_payload(payload)``
decodes a whole frame stream the way DVE does and returns little-endian
int16 PCM. ``dvf_to_wav(dvf_bytes)`` validates and decodes a whole .dvf file
and returns the WAV DVE would have written for it.
"""

from __future__ import annotations

import math
import struct
from array import array
from typing import Callable, Iterator, List, Optional, Tuple

from .. import dvf as dvf_module
from . import _core, bitstream, params, synthesis
from . import tables as tables_module

FRAME_SAMPLES = 512
ORDER = 10

# Frame layout (docs/lpec.md, "Frame layout"): transform type -> overlap
# parameter Λ (N comes from params.TRANSFORM_N).
_OVERLAP = {0: 256, 1: 256, 2: 512, 3: 512}

# Pitch predictor sections (docs/lpec.md): (length, lag slot).
_SECTIONS = {
    0: ((128, 0), (256, 1), (128, 2)),
    1: ((256, 0), (256, 2)),
    2: ((128, 0), (256, 1), (128, 2)),
    3: ((256, 0), (256, 2)),
}


class Decoder:
    """Decoder state (docs/lpec.md, "State between frames")."""

    def __init__(self, tables=None):
        self.t = tables if tables is not None else tables_module.load()
        self.ones = list(self.t.DEFAULT_SHAPE)
        # State that InitDecoder sets and ResetDecoder does not touch.
        self.lsp1_i1 = 0                      # stage-1 LSP index of slot 1
        self.noise = params.NoisePosition(0)  # noise position z
        self.H = [0.0] * 768                  # H[256..767]: never cleared by reset
        self.pq0 = [0, 0, 0]                  # taps slot 0 (Q15): never read
        self.reset()

    def reset(self) -> None:
        """The partial ResetDecoder (docs/lpec.md, "State between frames")."""
        t = self.t
        self.lsp0 = list(t.LSP_INIT)
        self.q0 = [params.s16(math.floor(l * 65536.0)) for l in self.lsp0]
        self.lag0 = 0
        self.taps0 = [0.0, 0.0, 0.0]
        self.flags = [0, 0, 0]          # shape flags: previous, half 0, half 1
        self.shapes = [self.ones] * 3   # shapes: previous, half 0, half 1
        self.mem = [0.0] * ORDER        # LPC filter memory (last 10 outputs y)
        for i in range(256):
            self.H[i] = 0.0
        self.O = [0.0] * 512            # overlap buffer

    # ------------------------------------------------------------------

    def decode_frame(self, data: bytes) -> Tuple[List[int], int]:
        """Decode one frame from ``data`` (the bytes available, at most one
        frame's worth is read, wrapping to the start when fewer are given).

        Returns (512 int16 samples, bytes consumed): the reader's byte
        position after the frame, i.e. the frame length when ``data`` holds
        the whole frame.
        """
        t = self.t
        frame = bitstream.parse_frame(data, self.lsp1_i1, t.AB)
        self.lsp1_i1 = frame.lsp1_i1_next
        mode = frame.mode

        # --- Parameter sets (docs/lpec.md, "Parameter sets") ---
        l2 = params.lsp_double(t, *frame.lsp_b)
        q2 = params.lsp_q16(t, *frame.lsp_b)
        l1: Optional[List[float]] = None
        q1: Optional[List[int]] = None
        if mode == 0 and frame.mid_frame_lsp == 1:
            l1 = params.lsp_double(t, *frame.lsp_a)
            q1 = params.lsp_q16(t, *frame.lsp_a)
        elif mode in (0, 2):
            l1 = params.interpolate_lsp_double(self.lsp0, l2)
            q1 = params.interpolate_lsp_q16(self.q0, q2)

        lag2, taps2, pq2 = params.pitch(t, *frame.pitch_b)
        lag1, taps1, pq1 = (0, None, None)
        if frame.pitch_a is not None:
            lag1, taps1, pq1 = params.pitch(t, *frame.pitch_a)

        # --- Shape state (docs/lpec.md, "Overlap-add", shaped) ---
        if mode == 0:
            for h in range(2):
                self.flags[1 + h] = frame.shape_flags[h]
                idx = frame.shape_indices[h]
                self.shapes[1 + h] = list(t.SHAPES[idx]) if frame.shape_flags[h] else self.ones
        elif mode == 2:
            self.flags[1] = self.flags[2] = 0
            self.shapes[1] = self.shapes[2] = self.ones
        elif mode == 3:
            self.shapes[1] = self.shapes[2] = self.ones

        # --- Coefficient blocks -> transforms -> excitation x ---
        slot_q = {1: q1, 2: q2}
        slot_lag = {1: lag1, 2: lag2}
        slot_pq = {1: pq1, 2: pq2}
        Ys = []
        for block in frame.blocks:
            k = block.slot
            X = params.coefficients(t, block, slot_q[k], slot_lag[k], slot_pq[k], self.noise)
            N = params.TRANSFORM_N[block.type]
            Ys.append(synthesis.inverse_transform(t, X, N, _OVERLAP[block.type]))

        O = self.O
        if mode == 0:
            x = []
            for h in range(2):
                x += synthesis.overlap_add_shaped(
                    O, Ys[h], self.flags[h], self.shapes[h], self.flags[h + 1],
                    self.shapes[h + 1], t.WIN512, t.WIN512_SQ)
        elif mode == 1:
            x = synthesis.overlap_add_plain(O, Ys[0], 512, 256, t.WIN1024)
        elif mode == 2:
            x = synthesis.overlap_add_plain(O, Ys[0], 256, 256, t.WIN512)
            x += synthesis.overlap_add_plain(O, Ys[1], 256, 512, t.WIN512)
        else:
            x = synthesis.overlap_add_plain(O, Ys[0], 512, 512, t.WIN1024)

        # --- Long-term predictor over H[256..767] ---
        slot_pitch = {0: (self.lag0, self.taps0), 1: (lag1, taps1), 2: (lag2, taps2)}
        H = self.H
        n = 256
        for length, slot in _SECTIONS[mode]:
            lag, taps = slot_pitch[slot]
            synthesis.pitch_section(H, x, n, length, lag, taps)
            n += length

        # --- LPC synthesis with interpolated coefficients ---
        y = list(self.mem)
        if mode == 0 and frame.mid_frame_lsp == 1:
            runs = ((self.lsp0, l1, 8), (l1, l2, 8))    # 2 x 256 samples
        else:
            runs = ((self.lsp0, l2, 16),)               # 512 samples
        pos = 256
        for lA, lB, steps in runs:
            inv = 1.0 / steps
            for k in range(steps + 1):
                tk = k * inv
                block_len = 16 if k in (0, steps) else 32
                a = synthesis.lsp_to_lpc(synthesis.interpolate_lsp(lA, lB, tk))
                synthesis.lpc_filter(H, pos, block_len, a, y)
                pos += block_len
        outputs = y[ORDER:]
        samples = [synthesis.to_int16(v) for v in outputs]

        # --- End of frame (docs/lpec.md, "End of frame") ---
        H[0:256] = H[512:768]
        self.mem = outputs[-ORDER:]
        self.lsp0 = l2
        self.q0 = q2
        self.lag0 = lag2
        self.taps0 = taps2
        self.pq0 = pq2
        self.flags[0] = self.flags[2]
        self.shapes[0] = self.shapes[2]

        return samples, _bytes_consumed(bitstream.MODE_BYTES[mode], len(data))


def _bytes_consumed(frame_bytes: int, available: int) -> int:
    """The reader's byte position after a frame of ``frame_bytes`` read
    from ``available`` bytes: the frame length, or, when the reader had to
    wrap to the start of the short input (docs/lpec.md, "Short input"),
    the position it reached in its second pass over those bytes."""
    if frame_bytes <= available:
        return frame_bytes
    return (frame_bytes - 1) % available + 1


class Cancelled(Exception):
    """Decoding was stopped by the caller's ``should_stop`` callback."""


# How often (in frames) the frame loop polls ``should_stop``: 64 frames is
# about 4 s of audio; in pure Python that is about 4 s of decoding, so a stop is noticed within seconds.
_STOP_CHECK_FRAMES = 64

# The canonical 44-byte WAV header Digital Voice Editor writes: RIFF, a 16-byte 'fmt ' chunk (PCM, mono, 8000 Hz, 16-bit),
# then 'data'.
_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
WAV_HEADER_BYTES = _WAV_HEADER.size   # 44


def _frame_chunks(payload: bytes, should_stop: Optional[Callable[[], bool]] = None
                  ) -> Iterator[bytes]:
    """DVE's framing loop (docs/lpec.md, "API behaviour and framing"): each
    frame gets the bytes that are left, up to one frame's worth (the length
    comes from the mode bits of its first byte), and the stream advances by
    the bytes that frame consumed. When the data ends inside a frame the
    reader wraps within what is left and decoding continues from its
    position until nothing is left.

    Polls ``should_stop`` every _STOP_CHECK_FRAMES frames and raises
    Cancelled when it returns true.
    """
    pos = 0
    total = len(payload)
    n = 0
    while pos < total:
        if should_stop is not None and n % _STOP_CHECK_FRAMES == 0 and should_stop():
            raise Cancelled("decoding was stopped")
        length = bitstream.MODE_BYTES[bitstream.frame_mode(payload[pos])]
        chunk = payload[pos:pos + length]
        yield chunk
        pos += _bytes_consumed(length, len(chunk))
        n += 1


def decode_payload(payload: bytes, tables=None, use_core: Optional[bool] = None,
                   should_stop: Optional[Callable[[], bool]] = None) -> bytearray:
    """Decode a whole LPEC frame stream to little-endian int16 PCM.

    Mirrors DVE's loop (see _frame_chunks). ``use_core``: None uses the C
    core (st25/lpec/_core.py) when it loaded and pure Python otherwise;
    False forces pure Python; True requires the core (RuntimeError without
    it). Both paths give identical PCM. ``should_stop``, if given, is
    polled every few dozen frames; when it returns true the decode raises
    Cancelled (a long pure-Python decode can take minutes).
    """
    return _decode(payload, tables, use_core, should_stop, 0)


def _decode(payload: bytes, tables, use_core: Optional[bool],
            should_stop: Optional[Callable[[], bool]], prefix: int) -> bytearray:
    """decode_payload, returning ``prefix`` zero bytes followed by the PCM
    in one bytearray (so dvf_to_wav can fill in a header in place)."""
    if use_core is None:
        use_core = _core.available()
    if use_core and not _core.available():
        raise RuntimeError("the LPEC C core (lpec_core.dll) is not available")
    t = tables if tables is not None else tables_module.load()
    chunks = _frame_chunks(payload, should_stop)

    if use_core:
        # Parse here (with the same carried slot-1 LSP index as
        # Decoder.decode_frame) and pack each frame as it is parsed, so the
        # parsed Frame objects never pile up; then decode all of them in
        # one call.
        packed = array("i")
        nframes = 0
        lsp1_i1 = 0
        for chunk in chunks:
            frame = bitstream.parse_frame(chunk, lsp1_i1, t.AB)
            lsp1_i1 = frame.lsp1_i1_next
            _core.pack_frame(frame, packed)
            nframes += 1
        return _core.decode_packed(t, packed, nframes, prefix)

    dec = Decoder(t)
    out = bytearray(prefix)
    pack = struct.Struct(f"<{FRAME_SAMPLES}h").pack
    for chunk in chunks:
        samples, _used = dec.decode_frame(chunk)
        out += pack(*samples)
    return out


def dvf_to_wav(dvf_bytes: bytes, tables=None,
               should_stop: Optional[Callable[[], bool]] = None) -> bytearray:
    """Decode a Sony ICD-ST25 "LP" .dvf recording to a WAV file.

    Validates the file first (st25.dvf.validate), so a non-LP or damaged
    file raises st25.dvf.FormatError with a clear reason instead of decoding
    garbage. The result is the canonical 44-byte header (RIFF, a 16-byte
    'fmt ' chunk: PCM, mono, 8000 Hz, 16-bit, then 'data') followed by the
    PCM, nothing else -- exactly what Digital Voice Editor writes. It is one bytearray: the PCM is decoded straight into it
    after room for the header, so a long recording's audio is never copied.
    ``should_stop``: see decode_payload.
    """
    reason = dvf_module.validate(dvf_bytes)
    if reason is not None:
        raise dvf_module.FormatError(reason)
    wav = _decode(dvf_module.payload(dvf_bytes), tables, None, should_stop, WAV_HEADER_BYTES)
    n = len(wav) - WAV_HEADER_BYTES
    _WAV_HEADER.pack_into(wav, 0, b"RIFF", 36 + n, b"WAVE", b"fmt ", 16,
                          1, 1, 8000, 16000, 2, 16, b"data", n)
    return wav
