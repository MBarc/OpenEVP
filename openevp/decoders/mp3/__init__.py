"""MP3 decoding: MPEG-1, -2 and -2.5 audio, layers I, II and III (.mp3, and the
.mpeg / .mpga / .mp2 / .m2a files other programs save, e.g. WhatsApp Web's
.mpeg voice notes), to 16-bit PCM at the file's own sample rate and channels.

Decoded by minimp3 (vendor/minimp3, CC0) in a C core (_mp3.c -> mp3_core.dll,
see _core.py). There is no pure-Python fallback: without the core,
``Unavailable`` is raised (never ``Mp3Error``: not the file's fault).
Deterministic: the same file and the same build give the same PCM, on every
x86-64 CPU (see _mp3.c).

Damaged or truncated files decode as far as their frames are valid (minimp3
resyncs past junk); a file in which nothing decodes raises ``Mp3Error``.
``sniff()`` tells an MP3 from other files with the same extensions (an .mpeg
video) from its first SNIFF_BYTES only.

API:
    sniff(header) -> bool
    stream(data, should_stop=None) -> (channels, 2, rate, PCM chunks)
    write_wav(data, f, should_stop=None) -> the WAV's size, written as it decodes
    to_wav(data, should_stop=None) -> WAV (bytearray)
    seconds(data) -> the length counted from the frames (nothing decoded)
    wav_bytes(data) -> about how big the WAV will be
"""
from __future__ import annotations

import ctypes
import struct

from . import _core

EXTENSIONS = (".mp3", ".mpeg", ".mpga", ".mp2", ".m2a")
SNIFF_BYTES = 4096                       # what sniff() is given of a file
MAX_BYTES = 512 << 20                    # larger files are not treated as MP3 recordings
MAX_PCM_BYTES = (2 << 30) - 44           # decoded audio, at most (about 3.4 hours of 44.1 kHz stereo)
CHUNK_FRAMES = 1 << 16                   # sample frames per call into the core


class Mp3Error(ValueError):
    """The data is not a usable MP3: the file's fault."""


class Unavailable(RuntimeError):
    """The decoder's C core is not in this build (or does not load)."""


class Cancelled(Exception):
    """should_stop() returned true."""


def available():
    return _core.available()


def reason():
    """Why MP3 files cannot be decoded now (a phrase), or None."""
    return _core.problem()


# ---- headers --------------------------------------------------------------------
_KBPS = {  # (MPEG-1?, layer) -> bitrate index 1..14 in kbps
    (True, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def frame_header(data, at=0):
    """(version bits, layer, sample rate, channels, kbps, frame bytes) of an MPEG
    audio frame header at data[at:at + 4], or None when it is not a valid one
    (free-format frames, with no bitrate, count as not valid)."""
    if at < 0 or at + 4 > len(data):
        return None
    h = int.from_bytes(bytes(data[at:at + 4]), "big")
    version, layer_bits, br, sr = (h >> 19) & 3, (h >> 17) & 3, (h >> 12) & 15, (h >> 10) & 3
    if h >> 21 != 0x7FF or version == 1 or layer_bits == 0 or br in (0, 15) or sr == 3:
        return None
    layer = 4 - layer_bits
    mpeg1 = version == 3
    kbps = _KBPS[(mpeg1, layer)][br - 1]
    rate = _RATES[version][sr]
    pad = (h >> 9) & 1
    if layer == 1:
        size = (12 * kbps * 1000 // rate + pad) * 4
    else:
        spf = 1152 if layer == 2 or mpeg1 else 576
        size = spf // 8 * kbps * 1000 // rate + pad
    return version, layer, rate, 1 if (h >> 6) & 3 == 3 else 2, kbps, size


def id3_length(data):
    """Bytes taken by an ID3v2 tag at the start of data (0 when there is none)."""
    if len(data) < 10 or bytes(data[:3]) != b"ID3" or data[3] == 0xFF or data[4] == 0xFF:
        return 0
    if any(b & 0x80 for b in bytes(data[6:10])):
        return 0                                                    # not a syncsafe size: not a tag
    size = sum((b & 0x7F) << (7 * (3 - i)) for i, b in enumerate(bytes(data[6:10])))
    return 10 + size + (10 if data[5] & 0x10 else 0)                # a v2.4 footer


def sniff(header):
    """Is this the start of an MP3 file? header: its first SNIFF_BYTES bytes (or
    all of it). Yes for an ID3v2 tag, or a valid MPEG audio frame at the very
    start followed by a second frame of the same version, layer and sample rate.
    No for anything else: an MPEG video (.mpeg), a renamed text file, an empty file."""
    if id3_length(header):
        return True
    first = frame_header(header, 0)
    if first is None:
        return False
    second = frame_header(header, first[5])
    return second is not None and second[:3] == first[:3]


def _audio_span(data):
    """(start, end) of the frames: after an ID3v2 tag, before an ID3v1 tag."""
    start = min(id3_length(data), len(data))
    end = len(data)
    if end - start >= 128 and bytes(data[end - 128:end - 125]) == b"TAG":
        end -= 128
    return start, end


# ---- decoding ---------------------------------------------------------------------
def _decoder(data):
    problem = _core.problem()
    if problem:
        raise Unavailable(problem)
    if len(data) > MAX_BYTES:
        raise Mp3Error(f"it is too large to be an MP3 recording (over {MAX_BYTES >> 20} MB)")
    start, end = _audio_span(data)
    return _core.Decoder(data, start, end)


def stream(data, should_stop=None):
    """(channels, sample width 2, sample rate, PCM chunks) of an MP3 (bytes): the
    chunks (interleaved little-endian int16) are decoded as they are consumed.
    The first one is decoded here, so a file in which nothing decodes raises
    Mp3Error at once; a file whose audio exceeds MAX_PCM_BYTES raises it while
    the chunks are consumed. Raises Unavailable, Cancelled."""
    dec = _decoder(data)
    buf = (ctypes.c_int16 * (2 * CHUNK_FRAMES))()

    def run():
        if should_stop is not None and should_stop():
            raise Cancelled("stopped")
        n = dec.run(buf, CHUNK_FRAMES)
        return ctypes.string_at(buf, n * dec.fmt[_core.F_CHANNELS] * 2) if n else b""

    first = run()
    if not first:
        raise Mp3Error("not an MP3 file (no MPEG audio could be decoded)")
    channels, rate = int(dec.fmt[_core.F_CHANNELS]), int(dec.fmt[_core.F_RATE])

    def chunks():
        total, chunk = 0, first
        while chunk:
            total += len(chunk)
            if total > MAX_PCM_BYTES:
                raise Mp3Error(f"it is too long to open (more than {MAX_PCM_BYTES >> 20} MB of audio)")
            yield chunk
            chunk = run()
    return channels, 2, rate, chunks()


def _header(channels, rate, size):
    align = channels * 2
    return (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * align, align, 16)
            + b"data" + struct.pack("<I", size))


def write_wav(data, f, should_stop=None):
    """The decoded audio as a 16-bit PCM WAV written into f (seekable, binary) as
    it decodes. Returns the WAV's size. Exceptions as stream()."""
    channels, _width, rate, chunks = stream(data, should_stop)
    start = f.tell()
    f.write(_header(channels, rate, 0))
    size = 0
    for c in chunks:
        f.write(c)
        size += len(c)
    end = f.tell()
    f.seek(start)
    f.write(_header(channels, rate, size))
    f.seek(end)
    return 44 + size


def to_wav(data, should_stop=None):
    """The decoded audio as a 16-bit PCM WAV (a bytearray). Exceptions as stream()."""
    channels, _width, rate, chunks = stream(data, should_stop)
    out = bytearray(_header(channels, rate, 0))
    for c in chunks:
        out += c
    out[:44] = _header(channels, rate, len(out) - 44)
    return out


def seconds(data):
    """(sample rate, channels, seconds) counted from the frames that would decode
    (nothing is decoded: minimp3 reads their headers). Raises Mp3Error when there
    are none, Unavailable."""
    dec = _decoder(data)
    total = 0
    while True:
        n = dec.run(None, 1 << 30)
        if not n:
            break
        total += n
    rate = int(dec.fmt[_core.F_RATE])
    if not total or not rate:
        raise Mp3Error("not an MP3 file (no MPEG audio frames)")
    return rate, int(dec.fmt[_core.F_CHANNELS]), total / rate


def wav_bytes(data):
    """About how big to_wav's WAV will be, from the first frame's bitrate (exact
    for constant-bitrate files), or None."""
    start, end = _audio_span(data)
    head = bytes(data[start:start + SNIFF_BYTES])
    at = next((i for i in range(len(head) - 3) if frame_header(head, i) is not None), None)
    if at is None:
        return None
    _v, _layer, rate, channels, kbps, _size = frame_header(head, at)
    return 44 + int((end - start) * 8 / (kbps * 1000) * rate) * channels * 2
