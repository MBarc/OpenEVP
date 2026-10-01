"""MP3 encoding for EVP clips: a clip WAV (openevp.clips.cut) as an MP3 for sharing.

Encoded with lameenc (the LAME encoder; LGPL), 128 kbps CBR at the clip's own
sample rate: nothing is resampled. LAME's limits, as found with lameenc 1.8.4:
  - MPEG-2.5 rates (8000, 11025, 12000 Hz: an ICD-ST25 LP recording is 8 kHz)
    allow at most 64 kbps, so those clips are 64 kbps CBR (8 bits per sample,
    plenty for 8 kHz speech);
  - a rate that is not an MPEG rate at all (say 96000 or 22000 Hz, a WAV from
    elsewhere) is resampled by LAME to the nearest MPEG rate it supports; the
    nine MPEG rates (8000 to 48000 Hz) never are.
Mono stays mono, stereo is joint stereo; a WAV with more channels is refused.
The samples become 16-bit first (LAME's input): 8-, 24- and 32-bit PCM and
float WAVs are converted, 16-bit ones are passed as they are.

An ID3v2.3 tag goes in front: the title (the mark's class and time, and the
note) and a comment (the note). Encoding is deterministic: the same clip gives
the same bytes (LAME is, and the tag holds no time), so a second export finds
an identical file already saved.
"""
import struct

try:
    import lameenc
except ImportError:                      # a build without it: MP3 export says so
    lameenc = None

UNAVAILABLE = "MP3 export isn't available in this build; use WAV."
BITRATE = 128                            # kbps, CBR
MPEG25_MAX = 64                          # kbps: LAME's highest bitrate at 8000, 11025 and 12000 Hz
MPEG25_RATES = (8000, 11025, 12000)
QUALITY = 2                              # LAME's -q 2: high quality, still fast for a clip

_PCM, _FLOAT, _EXTENSIBLE = 1, 3, 0xFFFE


def available():
    return lameenc is not None


def version():
    """lameenc's version ("1.8.4"), or None (not installed, or no metadata)."""
    if lameenc is None:
        return None
    try:
        from importlib import metadata
        return metadata.version("lameenc")
    except Exception:
        return None


def _wav_audio(wav):
    """(rate, channels, 16-bit little-endian interleaved samples) of a clip WAV."""
    import numpy as np
    from . import clips
    buf = memoryview(wav)
    fmt, rate, align, data_at, data_len = clips._layout(buf)
    tag, channels, _rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == _EXTENSIBLE and len(fmt) >= 26:
        tag = struct.unpack_from("<H", fmt, 24)[0]           # the sub-format GUID's first two bytes
    if channels not in (1, 2):
        raise ValueError(f"MP3 clips are mono or stereo; this audio has {channels} channels")
    width = align // channels
    raw = bytes(buf[data_at:data_at + (data_len // align) * align])
    if tag == _PCM and width == 2:
        return rate, channels, raw
    if tag == _FLOAT and width in (4, 8):
        x = np.frombuffer(raw, "<f4" if width == 4 else "<f8")
        x = np.clip(np.nan_to_num(x) * 32768.0, -32768, 32767)
        return rate, channels, np.round(x).astype("<i2").tobytes()
    if tag == _PCM and width == 1:                           # unsigned
        x = (np.frombuffer(raw, np.uint8).astype(np.int16) - 128) << 8
        return rate, channels, x.astype("<i2").tobytes()
    if tag == _PCM and width in (3, 4):                      # the top two bytes of each sample
        b = np.frombuffer(raw, np.uint8).reshape(-1, width)
        return rate, channels, np.ascontiguousarray(b[:, width - 2:]).tobytes()
    raise ValueError(f"MP3 clips can't be made from this WAV format ({tag}, {bits}-bit)")


def encode(wav, title="", comment=""):
    """An MP3 (bytes, ID3v2.3 tag first) of a clip WAV. Raises RuntimeError
    (UNAVAILABLE) without lameenc, ValueError when the WAV cannot be encoded."""
    if lameenc is None:
        raise RuntimeError(UNAVAILABLE)
    rate, channels, pcm = _wav_audio(wav)
    enc = lameenc.Encoder()
    enc.set_bit_rate(MPEG25_MAX if rate in MPEG25_RATES else BITRATE)
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(QUALITY)
    try:
        audio = bytes(enc.encode(pcm)) + bytes(enc.flush())
    except Exception as e:                                   # lameenc raises its own errors
        raise ValueError(f"the MP3 encoder refused it: {e}") from None
    return id3(title, comment) + audio


def _text(value):
    """An ID3v2.3 text: ISO-8859-1 (encoding 0) when it fits, else UTF-16 with a BOM (1)."""
    try:
        return b"\x00", value.encode("latin-1"), b"\x00"
    except UnicodeEncodeError:
        return b"\x01", b"\xff\xfe" + value.encode("utf-16-le"), b"\x00\x00"


def _frame(fid, body):
    return fid + struct.pack(">I", len(body)) + b"\x00\x00" + body


def id3(title="", comment=""):
    """An ID3v2.3 tag with a title (TIT2) and a comment (COMM, language "eng", no
    description), each left out when empty; b"" when both are."""
    frames = b""
    if title:
        enc, text, _end = _text(title)
        frames += _frame(b"TIT2", enc + text)
    if comment:
        enc, text, end = _text(comment)
        frames += _frame(b"COMM", enc + b"eng" + (b"\xff\xfe" if enc == b"\x01" else b"") + end + text)
    if not frames:
        return b""
    n = len(frames)
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])   # syncsafe
    return b"ID3\x03\x00\x00" + size + frames
