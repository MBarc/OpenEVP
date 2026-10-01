"""Changing the speed of a WAV for an export at the player's speed (0.25x to 2x).

Two ways, as the player plays it:
  - tape (keep_pitch=False): the audio played at `speed`, as a tape machine would:
    resampled by linear interpolation, so the length is x1/speed and every
    frequency is x speed; written at the original sample rate.
  - keep pitch (keep_pitch=True): a time stretch by WSOLA (waveform-similarity
    overlap-add), tuned for speech: 30 ms Hann frames at 50% overlap, each one
    taken from within +-12 ms of where it would nominally be, at the offset whose
    waveform best continues the previous frame (cross-correlation). A stereo (or
    wider) recording is aligned on its mid signal and every channel uses the same
    offsets, so the channels stay in lockstep. The input is padded by reflection
    at both ends and every output sample gets two frames whose windows sum to 1,
    so the level holds to the first and the last sample; every sample is a
    weighted mean of input samples: never louder than the input, never clipped.

Both work on the WAV's own sample format (8/16/24/32-bit PCM, 32/64-bit float)
and give it back in that format at the same rate: only the data chunk changes
(any marker chunks are dropped; a caller writes the marks back, scaled). Output
is deterministic (numpy, no randomness, no threads), so a second export of the
same thing gives identical bytes.

Memory, besides the caller's own copy of the input: the output file (one
bytearray, written once), the input as float32 samples (decoded a block at a
time; WSOLA pads that same array), for WSOLA on two or more channels a float32
mid signal (one sample per frame), and a few blocks of BLOCK samples. For a
16-bit input that is about 2x (mono) to 3x (stereo) the input, plus the output.
"""
import math
import struct

import numpy as np

from . import clips

FRAME = 0.030               # seconds: WSOLA frame (Hann window)
TOLERANCE = 0.012           # seconds: how far a frame may move to line up
BLOCK = 1 << 16             # samples processed per block

_PCM, _FLOAT, _EXTENSIBLE = 1, 3, 0xFFFE


class Cancelled(Exception):
    """should_stop() said stop."""


def suffix(speed, keep_pitch=True):
    """The file-name suffix for an export at speed: 0.5 -> "_0.5x", 2.0 -> "_2x";
    tape-style (keep_pitch False) -> "_0.5x-tape"."""
    return f"_{float(speed):g}x" + ("" if keep_pitch else "-tape")


def _format(fmt):
    """(sample kind "int"/"uint"/"float", bytes per sample, channels) of a fmt chunk."""
    tag, channels, _rate, _byte_rate, align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == _EXTENSIBLE and len(fmt) >= 26:
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if not channels:
        raise ValueError("the WAV format gives no channels")
    width = (align // channels) if align else (bits + 7) // 8
    if tag == _FLOAT and width in (4, 8):
        return "float", width, channels
    if tag == _PCM and width == 1:
        return "uint", 1, channels
    if tag == _PCM and width in (2, 3, 4):
        return "int", width, channels
    raise ValueError(f"this WAV format can't be slowed down or sped up ({tag}, {bits}-bit)")


def _decode_into(dest, raw, kind, width, channels):
    """PCM bytes -> float32 samples in [-1, 1), written into dest (frames, channels)
    a block at a time, so no full-length temporary is made."""
    frames = dest.shape[0]
    for f0 in range(0, frames, BLOCK):
        f1 = min(frames, f0 + BLOCK)
        part = raw[f0 * channels * width:f1 * channels * width]
        if kind == "float":
            x = np.nan_to_num(np.frombuffer(part, "<f4" if width == 4 else "<f8").astype(np.float32))
        elif kind == "uint":
            x = np.frombuffer(part, np.uint8).astype(np.float32)
            x -= 128.0
            x *= 1.0 / 128.0
        elif width == 3:
            b = np.frombuffer(part, np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v >= 1 << 23, v - (1 << 24), v)
            x = v.astype(np.float32)
            x *= 1.0 / float(1 << 23)
        else:
            x = np.frombuffer(part, "<i2" if width == 2 else "<i4").astype(np.float32)
            x *= 1.0 / float(1 << (8 * width - 1))             # a power of two: exact
        dest[f0:f1] = x.reshape(-1, channels)


def _to_float(raw, kind, width, channels):
    """PCM bytes -> float32 samples in [-1, 1), shape (frames, channels)."""
    x = np.empty((len(raw) // (width * channels), channels), np.float32)
    _decode_into(x, raw, kind, width, channels)
    return x


def _to_bytes(y, kind, width):
    """float samples (any shape, interleaved order) -> PCM bytes, rounded and clipped."""
    y = np.asarray(y).reshape(-1)
    if width >= 3:
        y = y.astype(np.float64)                               # float32 cannot hold 24/32-bit steps
    if kind == "float":
        return y.astype("<f4" if width == 4 else "<f8").tobytes()
    if kind == "uint":
        return (np.clip(np.round(y * 128.0), -128, 127) + 128).astype(np.uint8).tobytes()
    full = float(1 << (8 * width - 1))
    v = np.clip(np.round(y * full), -full, full - 1)
    if width == 2:
        return v.astype("<i2").tobytes()
    if width == 4:
        return v.astype("<i4").tobytes()
    v = v.astype(np.int32)
    out = np.empty((v.size, 3), np.uint8)
    out[:, 0], out[:, 1], out[:, 2] = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF
    return out.tobytes()


def out_frames(n, speed):
    """How many sample frames n input frames become at speed."""
    return int(round(n / speed))


def _tape(x, speed, emit, should_stop, report):
    """x (frames, channels) played at speed: linear interpolation at i * speed."""
    n = x.shape[0]
    total = out_frames(n, speed)
    last = n - 1
    for at in range(0, total, BLOCK):
        if should_stop():
            raise Cancelled()
        i = np.arange(at, min(total, at + BLOCK), dtype=np.float64) * speed
        lo = np.minimum(i.astype(np.int64), last)
        frac = (i - lo).astype(np.float32)[:, None]
        a = x[lo]
        y = x[np.minimum(lo + 1, last)]
        y -= a
        y *= frac
        y += a
        emit(y)
        report(min(total, at + BLOCK), total)


def _reflect_index(t, n):
    """Indices t (any integers) folded into [0, n) by reflection about both ends."""
    if n == 1:
        return np.zeros_like(t)
    period = 2 * (n - 1)
    t = np.abs(t) % period
    return np.where(t >= n, period - t, t)


def _sliding_corr(region, template):
    """corr[j] = sum(region[j:j + len(template)] * template), for every j."""
    return np.correlate(region, template, "valid")


def _wsola(raw, kind, width, channels, rate, speed, emit, should_stop, report):
    """The PCM samples raw time-stretched by WSOLA to len/speed frames, pitch kept.

    Frame k (from k = -1) goes to output k * hop (hop = half a frame: 50% overlap)
    and is read from the input near k * hop * speed, at the offset (within +-tol)
    whose mid signal best matches how the previous frame's input goes on; frames
    -1 and 0 are one hop apart in the input (so output segment 0 is the input
    itself) and frame 0 is where it nominally is. Above ~16 kHz the search runs on the
    mid signal averaged down to ~8 kHz first, then is refined at the full rate
    within one coarse step. Overlap-add is done afterwards, in blocks of frames:
    output segment k is the first half of frame k plus the second half of frame
    k - 1, divided by the windows' sum there (1 for a periodic Hann)."""
    n = len(raw) // (width * channels)
    total = out_frames(n, speed)
    size = max(4, 2 * int(round(FRAME * rate / 2)))            # even
    hop = size // 2
    tol = max(1, int(round(TOLERANCE * rate)))
    win = (np.sin(np.pi * np.arange(size) / size) ** 2).astype(np.float32)   # periodic Hann
    frames = -(-total // hop)                                  # segments 0..frames-1 cover the output
    front = tol + size                                         # input index i is padded index i + front
    end = max(front + n, front + int(math.ceil(frames * hop * speed)) + tol + 2 * size + 2)
    xp = np.empty((end, channels), np.float32)                 # the input, padded by reflection
    _decode_into(xp[front:front + n], raw, kind, width, channels)
    xp[:front] = xp[front + _reflect_index(np.arange(-front, 0), n)]
    xp[front + n:] = xp[front + _reflect_index(np.arange(n, end - front), n)]
    mid = xp[:, 0] if channels == 1 else xp.mean(axis=1, dtype=np.float32)
    d = max(1, rate // 8000)                                   # coarse search step
    if d > 1:
        md = mid[:mid.size // d * d].reshape(-1, d).mean(axis=1, dtype=np.float64)
        sd = size // d
    starts = np.empty(frames + 1, np.int64)                    # starts[k + 1]: frame k's, from k = -1
    prev = None
    for k in range(-1, frames):
        if k % 2048 == 0:
            if should_stop():
                raise Cancelled()
            report(k * hop // 2, total)                        # the search: the first half of the work
        nominal = front + int(round(k * hop * speed))
        if k == -1:
            start = front - hop                                # frame 0 goes straight on from it: segment 0
        elif k == 0:                                           # is the input itself, from sample 0
            start = nominal
        else:
            t0 = prev + hop                                    # how the previous frame goes on
            lo, hi = nominal - tol, nominal + tol
            if d > 1:
                ts, a = t0 // d, lo // d
                corr = _sliding_corr(md[a:hi // d + sd], md[ts:ts + sd])
                c = (a + int(np.argmax(corr))) * d + (t0 - ts * d)
                lo, hi = max(lo, c - d), min(hi, c + d)
                if lo > hi:
                    lo = hi = min(max(c, nominal - tol), nominal + tol)
            corr = _sliding_corr(mid[lo:hi + size].astype(np.float64), mid[t0:t0 + size].astype(np.float64))
            start = lo + int(np.argmax(corr))
        starts[k + 1] = prev = start
    del mid
    if d > 1:
        del md
    norm = (win[:hop] + win[hop:])[:, None]
    offs = np.arange(size)
    step = max(1, BLOCK // hop)
    carry = xp[starts[0] + offs[hop:]] * win[hop:, None]       # frame -1's second half
    done = 0
    for k0 in range(0, frames, step):
        if should_stop():
            raise Cancelled()
        g = xp[starts[1 + k0:1 + k0 + step, None] + offs]      # (m, size, channels)
        g *= win[None, :, None]
        seg = g[:, :hop]                                       # a view: summed in place
        last = g[-1, hop:].copy()
        seg[1:] += g[:-1, hop:]
        seg[0] += carry
        carry = last
        seg /= norm
        y = seg.reshape(-1, channels)[:max(0, total - done)]
        if y.size:
            emit(y)
            done += y.shape[0]
        del g, seg, y
        report(total // 2 + done // 2, total)
    report(total, total)


def change_speed(wav_bytes, speed, keep_pitch=True, should_stop=None, progress=None):
    """A WAV (a bytearray: its fmt chunk as it was, a new data chunk, no other chunks) of
    wav_bytes at speed (0 < speed; 1 gives the audio unchanged), pitch kept or
    tape-style. progress(done, total) is called now and then (output frames);
    should_stop() is checked as it goes (raises Cancelled). Raises ValueError when
    the WAV cannot be read or its sample format is not supported, MemoryError when
    there is not enough memory (see the module's note)."""
    if not speed > 0:
        raise ValueError("the speed must be above 0")
    should_stop = should_stop or (lambda: False)
    buf = memoryview(wav_bytes)
    fmt, rate, align, data_at, data_len = clips._layout(buf)
    kind, width, channels = _format(fmt)
    frames = data_len // align
    raw = buf[data_at:data_at + frames * channels * width]
    total = frames if speed == 1 else out_frames(frames, speed)
    data_len = total * channels * width
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt + (b"\0" if len(fmt) & 1 else b"")
    size = 4 + len(fmt_chunk) + 8 + data_len + (data_len & 1)
    if size + 8 > 0xFFFFFFFF:
        raise ValueError("the WAV at this speed would exceed the 4 GiB limit")
    head = b"RIFF" + struct.pack("<I", size) + b"WAVE" + fmt_chunk + b"data" + struct.pack("<I", data_len)
    out = bytearray(len(head) + data_len + (data_len & 1))   # the whole file, written once
    out[:len(head)] = head
    pos = [len(head)]

    def emit(y):
        b = _to_bytes(y, kind, width)
        out[pos[0]:pos[0] + len(b)] = b
        pos[0] += len(b)

    def report(done, total_):
        if progress is not None:
            progress(done, total_)

    if frames == 0:
        pass
    elif speed == 1:
        out[pos[0]:pos[0] + len(raw)] = raw
        pos[0] += len(raw)
    elif keep_pitch:
        _wsola(raw, kind, width, channels, rate, float(speed), emit, should_stop, report)
    else:
        x = _to_float(raw, kind, width, channels)
        _tape(x, float(speed), emit, should_stop, report)
        del x
    if pos[0] != len(head) + data_len:
        raise AssertionError("the output length is not what was planned")
    return out


def scale_marks(marks, speed):
    """Marks (seconds) moved to where they are at speed: start and end x1/speed."""
    return [{**m, "start": float(m["start"]) / speed, "end": float(m["end"]) / speed} for m in marks]
