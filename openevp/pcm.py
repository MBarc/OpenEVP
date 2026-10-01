"""PCM WAV audio as float32 blocks, for the listening tools (openevp.enhance,
openevp.denoise, openevp.spectrogram): read any range of sample frames (zeros
outside the recording) from WAV bytes in memory or from a WAV file on disk, and
write float samples back in the recording's own sample format.

Samples are float32 in [-1, 1), shape (frames, channels); the conversions are
openevp.stretch's (exact for 8/16-bit, rounded and clipped on the way back).
"""
import math
import struct
import wave

import numpy as np

from . import clips, stretch


class Wav:
    """A PCM (or float) WAV held in memory: its fmt chunk, rate, channels and
    sample frames (a memoryview, never copied)."""

    def __init__(self, wav_bytes):
        buf = memoryview(wav_bytes)
        self.fmt, self.rate, align, at, length = clips._layout(buf)
        self.kind, self.width, self.channels = stretch._format(self.fmt)
        self.frames = length // align
        self._raw = buf[at:at + self.frames * self.channels * self.width]

    def read(self, f0, f1):
        """Frames [f0, f1) as float32 (f1 - f0, channels); zeros outside the recording."""
        out = np.zeros((max(0, f1 - f0), self.channels), np.float32)
        lo, hi = max(0, f0), min(self.frames, f1)
        if hi > lo:
            fw = self.channels * self.width
            stretch._decode_into(out[lo - f0:hi - f0], self._raw[lo * fw:hi * fw], self.kind, self.width,
                                 self.channels)
        return out

    def raw(self, f0, f1):
        """The PCM bytes of frames [f0, f1) (inside the recording), as they are."""
        fw = self.channels * self.width
        return self._raw[f0 * fw:f1 * fw]

    def output(self, frames=None):
        """A Writer for a WAV like this one (same fmt chunk) of `frames` frames (default: as many)."""
        return Writer(self.fmt, self.kind, self.width, self.channels, self.frames if frames is None else frames)


class WavFile:
    """A PCM WAV file on disk (anything Python's wave module reads: 8/16/24/32-bit
    integer PCM), read a range at a time. Use as a context manager."""

    def __init__(self, f):
        try:
            self._w = wave.open(f)
        except (wave.Error, EOFError) as e:
            raise ValueError(f"not a PCM WAV file ({e})") from None
        self.rate, self.channels = self._w.getframerate(), self._w.getnchannels()
        self.width, self.frames = self._w.getsampwidth(), self._w.getnframes()
        self.kind = "uint" if self.width == 1 else "int"
        if self.width not in (1, 2, 3, 4) or not self.rate or not self.channels:
            self._w.close()
            raise ValueError("this WAV format is not supported here")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._w.close()

    def read(self, f0, f1):
        """Frames [f0, f1) as float32 (f1 - f0, channels); zeros outside the recording."""
        out = np.zeros((max(0, f1 - f0), self.channels), np.float32)
        lo, hi = max(0, f0), min(self.frames, f1)
        if hi > lo:
            self._w.setpos(lo)
            data = self._w.readframes(hi - lo)
            got = len(data) // (self.channels * self.width)
            if got != hi - lo:
                raise ValueError("the WAV file is truncated")
            stretch._decode_into(out[lo - f0:hi - f0], data, self.kind, self.width, self.channels)
        return out


class Writer:
    """A WAV being written: a bytearray holding the whole file (header written
    once), filled a block of float samples at a time with put()."""

    def __init__(self, fmt, kind, width, channels, frames):
        self.kind, self.width, self.channels, self.frames = kind, width, channels, frames
        data_len = frames * channels * width
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt + (b"\0" if len(fmt) & 1 else b"")
        size = 4 + len(fmt_chunk) + 8 + data_len + (data_len & 1)
        if size + 8 > 0xFFFFFFFF:
            raise ValueError("the WAV would exceed the 4 GiB limit")
        head = b"RIFF" + struct.pack("<I", size) + b"WAVE" + fmt_chunk + b"data" + struct.pack("<I", data_len)
        self.data = bytearray(len(head) + data_len + (data_len & 1))
        self.data[:len(head)] = head
        self._pos = len(head)
        self._end = len(head) + data_len

    def put(self, y):
        """Append float samples (n, channels), rounded and clipped to the format."""
        b = stretch._to_bytes(y, self.kind, self.width)
        self.data[self._pos:self._pos + len(b)] = b
        self._pos += len(b)

    def put_raw(self, b):
        """Append PCM bytes as they are."""
        self.data[self._pos:self._pos + len(b)] = b
        self._pos += len(b)

    def done(self):
        """The finished WAV (raises AssertionError if not every frame was written)."""
        if self._pos != self._end:
            raise AssertionError("the output length is not what was planned")
        return self.data


def encode(y, kind, width):
    """Float samples -> PCM bytes in a format (see stretch._to_bytes)."""
    return stretch._to_bytes(y, kind, width)


def frame_size(rate):
    """The STFT size of the listening tools for a sample rate: the power of two
    nearest 32 ms (256 at 8 kHz, 512 at 16 kHz, 1024 at 44.1/48 kHz; 128..4096)."""
    return int(min(4096, max(128, 1 << round(math.log2(max(1, rate) * 0.032)))))


def hann(n):
    """A periodic Hann window of n points (float64)."""
    return np.sin(np.pi * np.arange(n) / n) ** 2
