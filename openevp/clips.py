"""EVP clips: one short WAV per mark, cut from a recording's decoded audio.

A clip keeps the recording's own sample format -- whatever its fmt chunk says
(8 kHz mono 16-bit for an ST25 LP recording, 44.1 kHz stereo for a WAV from
elsewhere...): the fmt chunk is copied as it is and the data chunk is cut on
whole sample frames (the fmt chunk's block align), so nothing is resampled or
converted. Each clip runs from PAD seconds before its mark to PAD seconds after
it, clamped to the recording, and carries the mark as a standard RIFF marker
(wavinfo.with_markers), placed where the EVP is inside the clip.

A clip can also be an MP3 for sharing (FORMATS; openevp.mp3): the same cut,
encoded, with the mark as its ID3 title and the note as its comment.
"""
import math
import re
import struct
import unicodedata

from . import wavinfo

FORMATS = ("mp3", "wav")    # clip formats; the first is the default
PAD = 0.5                   # seconds of audio kept on each side of the mark
MAX_NOTE = 40               # characters of the note kept in a clip's file name

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def _layout(buf):
    """(fmt chunk bytes as found, sample rate, block align, data offset, data length)
    of a PCM WAV. Lenient about what a decoder or another program may write (a RIFF
    size that is off, a data chunk that claims more than the file holds, extra
    chunks): only the fmt and data chunks matter here. Raises ValueError when
    either is missing or the format says nothing usable."""
    if len(buf) < 12 or bytes(buf[0:4]) != b"RIFF" or bytes(buf[8:12]) != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt = data = None
    pos = 12
    while pos + 8 <= len(buf) and (fmt is None or data is None):
        cid = bytes(buf[pos:pos + 4])
        size = struct.unpack_from("<I", buf, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt " and fmt is None:
            fmt = bytes(buf[body:body + size])
        elif cid == b"data" and data is None:
            data = (body, min(size, len(buf) - body))
        pos = body + size + (size & 1)
    if fmt is None or len(fmt) < 16:
        raise ValueError("no fmt chunk")
    if data is None:
        raise ValueError("no data chunk")
    channels, rate, _byte_rate, block_align, bits = struct.unpack_from("<HIIHH", fmt, 2)
    if not block_align:
        block_align = channels * ((bits + 7) // 8)
    if not rate or not block_align:
        raise ValueError("the WAV format gives no sample rate or frame size")
    return fmt, rate, block_align, data[0], data[1]


def bounds(start, end, duration, pad=PAD):
    """(clip start, clip end) in seconds: the mark with pad on each side, clamped
    to [0, duration]."""
    return max(0.0, start - pad), min(float(duration), max(end, start) + pad)


def cut(wav_bytes, mark, pad=PAD):
    """A clip of one mark ({"start", "end", "cls", "note"}, seconds) as WAV bytes in
    the recording's own format, with the mark written in as a marker. Raises
    ValueError when the WAV cannot be read or the mark is not inside it."""
    buf = memoryview(wav_bytes)
    fmt, rate, align, data_at, data_len = _layout(buf)
    frames = data_len // align
    start, end = float(mark["start"]), float(mark["end"])
    lo, hi = bounds(start, end, frames / rate, pad)
    first = max(0, math.floor(lo * rate + 1e-9))
    last = min(frames, math.ceil(hi * rate - 1e-9))
    if last <= first or start * rate >= frames:
        raise ValueError("the mark is not inside the recording")
    audio = buf[data_at + first * align:data_at + last * align]
    offset = first / rate
    body = [b"WAVE",
            b"fmt " + struct.pack("<I", len(fmt)) + fmt + (b"\0" if len(fmt) & 1 else b""),
            b"data" + struct.pack("<I", len(audio)), audio, b"\0" if len(audio) & 1 else b""]
    size = sum(len(p) for p in body)
    plain = b"".join([b"RIFF", struct.pack("<I", size), *body])
    inside = {"start": max(0.0, start - offset), "end": min(max(end, start) - offset, (last - first) / rate),
              "cls": mark["cls"], "note": mark.get("note") or ""}
    return wavinfo.with_markers(plain, [inside])


def safe_note(note, limit=MAX_NOTE):
    """A note as it can be part of a Windows file name: characters Windows refuses
    removed, runs of spaces made one, cut to `limit` characters, no trailing dot
    or space; invisible format characters (Unicode category Cf: zero-width spaces,
    direction marks...) dropped. "" when nothing usable is left."""
    text = "".join(c for c in (note or "") if unicodedata.category(c) != "Cf")
    text = _FORBIDDEN.sub(" ", text)
    text = " ".join(text.split())
    text = text[:limit].rstrip(" .")
    return text.lstrip(" .")


def stamp(seconds):
    """Where a mark starts, for a file name: 12.4 s -> "00m12.4s", 754.0 -> "12m34.0s"."""
    tenths = max(0, round(float(seconds) * 10))
    return f"{tenths // 600:02d}m{(tenths % 600) // 10:02d}.{tenths % 10}s"


def name(stem, mark, with_note=True, fmt="wav"):
    """A clip's file name: <stem>_EVP-<cls>_<MMmSS.s>s[_<note>].<fmt>."""
    note = safe_note(mark.get("note")) if with_note else ""
    return f"{stem}_EVP-{mark['cls']}_{stamp(mark['start'])}" + (f"_{note}" if note else "") + f".{fmt}"


def title(mark):
    """A clip's title (its MP3 tag): "EVP B at 1:02.4: get out" (the note left out when empty)."""
    tenths = max(0, round(float(mark["start"]) * 10))
    text = f"EVP {mark['cls']} at {tenths // 600}:{(tenths % 600) // 10:02d}.{tenths % 10}"
    note = " ".join((mark.get("note") or "").split())
    return f"{text}: {note}" if note else text


def make(wav_bytes, mark, fmt="wav", pad=PAD):
    """A clip of one mark in fmt ("wav": cut(); "mp3": that cut encoded, see
    openevp.mp3). Raises ValueError as cut() does (or when the MP3 encoder cannot
    take the audio), RuntimeError (mp3.UNAVAILABLE) when MP3 is not available."""
    clip = cut(wav_bytes, mark, pad)
    if fmt == "wav":
        return clip
    if fmt != "mp3":
        raise ValueError(f"unknown clip format {fmt!r}")
    from . import mp3
    return mp3.encode(clip, title(mark), (mark.get("note") or "").strip())
