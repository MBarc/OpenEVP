"""LPEC ST decoder: frame loop, segment handling and the public API.

A recording's payload is a sequence of fixed 283-byte frames: a 3-byte
header (a 16-bit big-endian counter and a flags byte) and 280 codec bytes.

- Counter 0 marks a segment-header frame (at each recording restart): it
  is not audio. It is skipped and the decoder is reset.
- Flags bits 7-6 = 01: the first 8 codec bytes are XOR-scrambled with 16-byte
  key ``flags >> 4 & 3`` starting at key byte ``flags & 15``.
- A fresh decoder does not output its first frame (the DLL's start-up
  delay: the first call returns 0 samples). This holds after every reset.
- A frame the DLL rejects outputs nothing and leaves the decoder state as
  it was.

Output: 44100 Hz, 2 channels, 2048 samples per channel per frame, int16
by round-half-even of the float32 result, clamped.
"""

from __future__ import annotations

import struct
from array import array
from typing import Callable, Iterator, List, Optional, Tuple

from sony_icd import dvf as dvf_module
from . import _core, bitstream, dsp
from . import tables as tables_module

FRAME_BYTES = 283
CODEC_BYTES = 280
FRAME_SAMPLES = 2048
SAMPLE_RATE = 44100
CHANNELS = 2


class Cancelled(Exception):
    """Decoding was stopped by the caller's ``should_stop`` callback."""


def descramble(frame: bytes, keys: bytes) -> bytes:
    """Undo the header-selected XOR on the first 8 codec bytes."""
    f = frame[2]
    if f >> 6 != 1:
        return frame
    b = bytearray(frame)
    k = ((f >> 4) & 3) * 16
    s = f & 15
    for i in range(8):
        b[3 + i] ^= keys[k + ((s + i) & 15)]
    return bytes(b)


def parse(data: bytes, offset: int, t) -> Optional[bitstream.Unit]:
    """Parse the frame at data[offset:offset+283], or None when the DLL would
    reject it. The DLL reads the frame in place in the caller's buffer, so a
    damaged frame may read on into the following bytes (up to bit 0x10000 of
    the frame); the reader gets the same bytes (missing ones read as zero)."""
    fr = data[offset:offset + FRAME_BYTES]
    tail = data[offset + FRAME_BYTES:offset + FRAME_BYTES + 8200]
    buf = descramble(fr, t.XOR_KEYS) + tail + bytes(8208 - len(tail))
    try:
        return bitstream.parse_frame(buf, 3, t)
    except bitstream.ParseError:
        return None


class Decoder:
    """One decoder instance (the DLL's decoder after init(44100, 2, 280))."""

    def __init__(self, tables=None):
        self.t = tables if tables is not None else tables_module.load()
        self.reset()

    def reset(self) -> None:
        self.state = [dsp.ChannelState(), dsp.ChannelState()]
        self.prevT = dsp.ToneInfo()
        self.skip = 1                     # frames still to swallow

    def decode_frame(self, data: bytes, offset: int = 0) -> Optional[Tuple[List[float], List[float]]]:
        """Decode the frame at data[offset:offset+283] (bytes after it may be
        read by the bit reader, as in the DLL; missing ones read as zero).

        Returns the two float32 channels (2048 each), or None when the frame
        is rejected. Does not apply the start-up skip (see ``frame_pcm``).
        """
        t = self.t
        u = parse(data, offset, t)
        if u is None:
            return None
        st0, st1 = self.state
        for b in u.share_prev_env:
            st1.tones[b].copy_env(st0.tones[b])
        spectra = dsp.dequantize(u, [st0.gain, st1.gain], t)
        curT = dsp.ToneInfo(u.tones_present, u.amp_mode, u.tone_negate)
        u.noise_counter = u.noise_table
        out = []
        for ci in range(2):
            out.append(dsp.synthesize(self.state[ci], u.ch[ci], u, spectra[ci], self.prevT, curT, t))
        self.prevT = curT
        return out[0], out[1]

    def frame_pcm(self, data: bytes, offset: int = 0) -> bytes:
        """Decode one frame and return its interleaved int16 PCM as bytes
        (empty for a swallowed or rejected frame)."""
        res = self.decode_frame(data, offset)
        if res is None:
            return b""
        if self.skip:
            self.skip -= 1
            return b""
        return to_pcm(*res)


def to_pcm(left: List[float], right: List[float]) -> bytes:
    """Interleaved int16 PCM: round half to even (the FPU's fistp) and clamp."""
    try:
        M = 6755399441055744.0          # 1.5 * 2**52: x + M - M rounds x half-to-even
        lo, hi = -32768, 32767
        li = [int((v + M) - M) for v in left]
        ri = [int((v + M) - M) for v in right]
        out = array("h", [0]) * (2 * len(li))
        out[0::2] = array("h", [lo if v < lo else (hi if v > hi else v) for v in li])
        out[1::2] = array("h", [lo if v < lo else (hi if v > hi else v) for v in ri])
    except (OverflowError, ValueError):   # inf / NaN: take the slow path
        out = array("h", [0]) * (2 * len(left))
        for i in range(len(left)):
            out[2 * i] = _to_int16(left[i])
            out[2 * i + 1] = _to_int16(right[i])
    return out.tobytes()


def _to_int16(v: float) -> int:
    if v != v:                      # NaN: fistp gives the integer indefinite
        return -32768
    if v >= 32767.5:
        return 32767
    if v <= -32768.5:
        return -32768
    r = round(v)                    # round half to even, like fistp
    if r > 32767:
        return 32767
    if r < -32768:
        return -32768
    return r


_STOP_CHECK_FRAMES = 16          # about 0.7 s of audio (a few ms with the C core)


def _frames(frames_bytes, should_stop: Optional[Callable[[], bool]],
            progress: Optional[Callable[[float], None]] = None) -> Iterator[int]:
    """The offset of every whole 283-byte frame, polling ``should_stop``
    every _STOP_CHECK_FRAMES frames (Cancelled when it returns true) and
    telling ``progress(fraction)`` there how far it is (1.0 at the end)."""
    total = len(frames_bytes) // FRAME_BYTES
    for n, pos in enumerate(range(0, len(frames_bytes) - FRAME_BYTES + 1, FRAME_BYTES)):
        if n % _STOP_CHECK_FRAMES == 0:
            if should_stop is not None and should_stop():
                raise Cancelled("decoding was stopped")
            if progress is not None:
                progress(n / total)
        yield pos
    if progress is not None:
        progress(1.0)


def _use_core(use_core: Optional[bool]) -> bool:
    if use_core is None:
        return _core.available()
    if use_core and not _core.available():
        raise RuntimeError("the LPEC ST C core (lpec_st_core.dll) is not available")
    return use_core


def pcm_chunks(frames_bytes: bytes, tables=None, should_stop: Optional[Callable[[], bool]] = None,
               use_core: Optional[bool] = None,
               progress: Optional[Callable[[float], None]] = None) -> Iterator[bytes]:
    """Decode an LPEC ST payload frame by frame: yields each output frame's
    interleaved little-endian int16 PCM (8192 bytes; swallowed and rejected
    frames yield nothing). Counter-0 frames are skipped and reset the
    decoder; a trailing partial frame is ignored (see the module docstring).

    ``use_core``: None uses the C core (_core.py) when it loaded and pure
    Python otherwise; False forces pure Python; True requires the core.
    Both give identical PCM. Nothing but the current frame is held, so a
    caller can stream a long recording to a file or a hash.
    ``progress(fraction)``: see _frames.
    """
    t = tables if tables is not None else tables_module.load()
    if not _use_core(use_core):
        dec = Decoder(t)
        for pos in _frames(frames_bytes, should_stop, progress):
            if frames_bytes[pos] == 0 and frames_bytes[pos + 1] == 0:
                dec.reset()
                continue
            pcm = dec.frame_pcm(frames_bytes, pos)
            if pcm:
                yield pcm
        return
    core = _core.CoreDecoder(t)
    skip = 1
    for pos in _frames(frames_bytes, should_stop, progress):
        if frames_bytes[pos] == 0 and frames_bytes[pos + 1] == 0:
            core.reset()
            skip = 1
            continue
        u = parse(frames_bytes, pos, t)
        if u is None:
            continue
        pcm = core.frame_pcm(u)
        if skip:
            skip -= 1
            continue
        yield pcm


def decode(frames_bytes: bytes, tables=None,
           should_stop: Optional[Callable[[], bool]] = None,
           use_core: Optional[bool] = None) -> Tuple[int, int, bytes]:
    """Decode an LPEC ST payload (concatenated 283-byte frames).

    Returns (sample_rate, channels, interleaved little-endian int16 PCM).
    See pcm_chunks.
    """
    pcm = bytearray()
    for chunk in pcm_chunks(frames_bytes, tables, should_stop, use_core):
        pcm += chunk
    return SAMPLE_RATE, CHANNELS, bytes(pcm)


def payload_from_raw(raw: bytes) -> bytes:
    """The frame payload of an ICD-ST10 voice dump: 1056-byte blocks, each
    two 512-byte data halves with 16 spare bytes after each; the data
    starts with a 10-byte header whose bytes 4-5 (big-endian) give the end
    of the valid data in the 1024-byte block."""
    pay = bytearray()
    for k in range(len(raw) // 1056):
        b = raw[k * 1056:k * 1056 + 512] + raw[k * 1056 + 528:k * 1056 + 1040]
        end = struct.unpack(">H", b[4:6])[0]
        pay += b[10:end]
    return bytes(pay)


# ---- .dvf files (sony_icd.dvf) ----------------------------------------------------

# The canonical 44-byte WAV header: RIFF, a 16-byte 'fmt ' chunk (PCM, 2
# channels, 44100 Hz, 16-bit), then 'data': the LP decoder's layout.
_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
WAV_HEADER_BYTES = _WAV_HEADER.size   # 44


def wav_header(pcm_bytes: int) -> bytes:
    """The 44-byte header of a WAV holding ``pcm_bytes`` of LPEC ST PCM."""
    return _WAV_HEADER.pack(b"RIFF", 36 + pcm_bytes, b"WAVE", b"fmt ", 16, 1, CHANNELS, SAMPLE_RATE,
                            SAMPLE_RATE * CHANNELS * 2, CHANNELS * 2, 16, b"data", pcm_bytes)


def dvf_payload(dvf_bytes) -> bytes:
    """The LPEC ST frame stream of a .dvf, after checking the file: raises
    sony_icd.dvf.FormatError for a damaged file or one of another codec (an
    ICD-ST25's LPEC LP is never decoded as LPEC ST)."""
    reason = dvf_module.validate(dvf_bytes)
    if reason is not None:
        raise dvf_module.FormatError(reason)
    codec = dvf_module.codec(dvf_bytes)
    if codec != dvf_module.CODEC_ST:
        raise dvf_module.FormatError(f"this is not an LPEC ST recording (codec 0x{codec:02x}); "
                                     "the LPEC ST decoder cannot convert it")
    payload_bytes = struct.unpack(">I", bytes(dvf_bytes[464:468]))[0]    # validate() checked it
    if max_wav_bytes(payload_bytes) > MAX_WAV_BYTES:
        raise dvf_module.FormatError(
            f"this LPEC ST recording is too long to convert: its WAV could be "
            f"{max_wav_bytes(payload_bytes) / 2**30:.1f} GiB, over the WAV format's 4 GiB limit")
    return dvf_module.payload(dvf_bytes)


# A WAV's RIFF size field is 32 bits. ICD-ST10 recordings (at most 32 MB of
# flash, about 930 MB of WAV) are far below; a larger .dvf is refused before
# anything is decoded, allocated or written.
MAX_WAV_BYTES = 0xFFFFFFFF


def max_wav_bytes(payload_bytes: int) -> int:
    """The largest WAV ``payload_bytes`` of LPEC ST frames can decode to:
    every whole frame giving its 2048 stereo samples."""
    return WAV_HEADER_BYTES + payload_bytes // FRAME_BYTES * FRAME_SAMPLES * CHANNELS * 2


def dvf_to_wav(dvf_bytes, tables=None, should_stop: Optional[Callable[[], bool]] = None,
               use_core: Optional[bool] = None, progress: Optional[Callable[[float], None]] = None) -> bytearray:
    """Decode an ICD-ST10 (LPEC ST) .dvf recording to a WAV file: the
    canonical 44-byte header (PCM, 2 channels, 44100 Hz, 16-bit) and the
    PCM, in one bytearray the PCM is decoded straight into (a 90-minute
    recording is about 930 MB, held once). Sony's framing is kept (the first
    frame after each reset gives no samples), so the samples, and with them
    the marks fingerprint, are exactly those of Sony's decoder."""
    payload = dvf_payload(dvf_bytes)
    # Sized for every frame up front and cut to what was decoded at the end:
    # growing a bytearray chunk by chunk reallocates it, briefly holding two
    # copies of a long recording.
    wav = bytearray(WAV_HEADER_BYTES + len(payload) // FRAME_BYTES * FRAME_SAMPLES * CHANNELS * 2)
    pos = WAV_HEADER_BYTES
    for chunk in pcm_chunks(payload, tables, should_stop, use_core, progress):
        wav[pos:pos + len(chunk)] = chunk
        pos += len(chunk)
    del wav[pos:]
    wav[0:WAV_HEADER_BYTES] = wav_header(pos - WAV_HEADER_BYTES)
    return wav


def dvf_write_wav(dvf_bytes, f, tables=None, should_stop: Optional[Callable[[], bool]] = None,
                  use_core: Optional[bool] = None, progress: Optional[Callable[[float], None]] = None) -> int:
    """dvf_to_wav, written to ``f`` (a seekable binary file) frame by frame
    instead of held in memory; the header is filled in last. Returns the
    WAV's size in bytes."""
    payload = dvf_payload(dvf_bytes)
    start = f.tell()
    f.write(bytes(WAV_HEADER_BYTES))
    n = 0
    for chunk in pcm_chunks(payload, tables, should_stop, use_core, progress):
        f.write(chunk)
        n += len(chunk)
    end = f.tell()
    f.seek(start)
    f.write(wav_header(n))
    f.seek(end)
    return WAV_HEADER_BYTES + n


def dvf_pcm(dvf_bytes, tables=None, should_stop: Optional[Callable[[], bool]] = None,
            use_core: Optional[bool] = None):
    """(channels, sample width, rate, PCM chunks) of an ICD-ST10 .dvf, for
    hashing the decoded samples (openevp.wavinfo.fingerprint_stream) without
    holding them. The file is checked before this returns; the chunks are
    decoded as they are consumed."""
    payload = dvf_payload(dvf_bytes)
    return CHANNELS, 2, SAMPLE_RATE, pcm_chunks(payload, tables, should_stop, use_core)
