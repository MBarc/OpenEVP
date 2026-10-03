"""WAV files written while they are recorded (Live mode and analog import).

A recording is written to "<final name>.part" in the folder it will end up in,
as a 16-bit PCM WAV whose header is rewritten every few seconds of audio (and
the file flushed to disk then), so a crash or a pulled plug leaves a playable
file holding all but the last few seconds. Stop renames the .part to its final
name, never over an existing file (a numbered name is used instead).

A .part left behind by a crash is finished by recover(): its header is fixed
from the file's size (cut to whole frames) and it gets its final name.

The fingerprint of the audio (openevp.wavinfo: SHA-256 over a prefix and the
raw PCM) is computed while writing, so Stop never reads an hour-long file again.
"""
import hashlib
import os
import struct

from .wavinfo import fingerprint_prefix

HEADER_BYTES = 44
HEADER_EVERY = 5.0                       # seconds of audio between header rewrites (and fsyncs)
MAX_DATA = (1 << 32) - (1 << 21)          # bytes of audio per file: under RIFF's 4 GiB limit, with room
PART = ".part"
WIDTH = 2                                 # 16-bit PCM only


class Full(Exception):
    """The file would pass MAX_DATA."""


class Unreadable(ValueError):
    """recover(): the file has no readable header and its format is not known, so
    what it holds cannot be told apart from noise. The file is left untouched."""


def header(rate, channels, data_bytes):
    """The 44-byte header of a 16-bit PCM WAV holding data_bytes of audio."""
    align = channels * WIDTH
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE" +
            b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * align, align, 8 * WIDTH) +
            b"data" + struct.pack("<I", data_bytes))


def parse_header(head):
    """(rate, channels) from a header this module wrote, or None if it is not one."""
    if len(head) < HEADER_BYTES or head[:4] != b"RIFF" or head[8:16] != b"WAVEfmt " or head[36:40] != b"data":
        return None
    size, tag, channels, rate, _byterate, align, bits = struct.unpack("<IHHIIHH", head[16:36])
    if size != 16 or tag != 1 or bits != 8 * WIDTH or channels not in (1, 2) or align != channels * WIDTH:
        return None
    if not 1000 <= rate <= 384000:
        return None
    return rate, channels


class WavPart:
    """One WAV being written. write() takes whole frames of 16-bit little-endian
    PCM (interleaved when stereo); close() returns (frames, fingerprint)."""

    def __init__(self, path, rate, channels, header_every=HEADER_EVERY, max_data=None):
        if channels not in (1, 2) or not 1000 <= rate <= 384000:
            raise ValueError("unsupported audio format")
        self.path = path
        self.rate = rate
        self.channels = channels
        self.align = channels * WIDTH
        self.data_bytes = 0
        max_data = MAX_DATA if max_data is None else max_data
        self._max = max_data - max_data % self.align
        self._every = max(self.align, int(header_every * rate) * self.align)
        self._synced = 0
        self._hash = hashlib.sha256(fingerprint_prefix(channels, WIDTH, rate))
        self._f = open(path, "xb")                # never over an existing file
        try:
            self._f.write(header(rate, channels, 0))
            self._f.flush()
        except BaseException:
            self._f.close()
            raise

    @property
    def frames(self):
        return self.data_bytes // self.align

    @property
    def seconds(self):
        return self.frames / self.rate

    def room(self):
        """Bytes of audio that still fit in this file."""
        return self._max - self.data_bytes

    def write(self, data):
        if len(data) % self.align:
            raise ValueError("not whole frames")
        if self.data_bytes + len(data) > self._max:
            raise Full()
        self._f.write(data)
        self._hash.update(data)
        self.data_bytes += len(data)
        if self.data_bytes - self._synced >= self._every:
            self.sync()

    def sync(self):
        """Rewrite the header for the audio written so far and flush it all to disk."""
        f = self._f
        f.flush()
        f.seek(4)
        f.write(struct.pack("<I", 36 + self.data_bytes))
        f.seek(40)
        f.write(struct.pack("<I", self.data_bytes))
        f.seek(0, os.SEEK_END)
        f.flush()
        os.fsync(f.fileno())
        self._synced = self.data_bytes

    def close(self):
        """Finish the file: (frames, fingerprint)."""
        if not self._f.closed:
            try:
                self.sync()
            finally:
                self._f.close()
        return self.frames, self._hash.hexdigest()

    def abort(self):
        """Close and delete the file (a piece too short to keep)."""
        try:
            self._f.close()
        finally:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass


def numbered(name, n):
    stem, ext = os.path.splitext(name)
    return name if n == 1 else f"{stem} ({n}){ext}"


def publish(part_path, folder, name, tries=1000):
    """Rename a finished .part to name in folder, or to "<stem> (2)<ext>" and so on
    when that is taken: never over an existing file. Returns the path used."""
    for n in range(1, tries + 1):
        target = os.path.join(folder, numbered(name, n))
        if os.path.lexists(target):
            continue
        try:
            if os.name == "nt":
                os.rename(part_path, target)      # refuses to replace on Windows
            else:
                os.link(part_path, target)        # refuses to replace on POSIX
                os.unlink(part_path)
            return target
        except FileExistsError:
            continue
    raise OSError(f"could not find a free file name for {name}")


def recover(part_path, rate=None, channels=None):
    """Fix the header of a .part left by a crash: the audio is what the file holds,
    cut to whole frames. rate/channels are used when the header is unreadable
    (a file cut off before its first header was written). Returns (rate, channels,
    frames): frames 0 only for a file proven to hold no audio (a readable header
    and less than one frame after it). Raises Unreadable, without changing the
    file, when the header is unreadable and no format is given."""
    with open(part_path, "r+b") as f:
        head = f.read(HEADER_BYTES)
        fmt = parse_header(head)
        if fmt is None:
            if rate is None or channels is None:
                raise Unreadable("the file has no readable WAV header")
            fmt = (int(rate), int(channels))
        rate, channels = fmt
        align = channels * WIDTH
        size = f.seek(0, os.SEEK_END)
        data = max(0, size - HEADER_BYTES)
        data = min(data - data % align, MAX_DATA - MAX_DATA % align)
        f.truncate(HEADER_BYTES + data)
        f.seek(0)
        f.write(header(rate, channels, data))
        f.flush()
        os.fsync(f.fileno())
    return rate, channels, data // align
