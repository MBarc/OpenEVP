"""The player's spectrogram, computed here (numpy) and drawn by the page from PNG tiles.

compute() reads a PCM WAV a block at a time (never all of it as floats) and
makes one column per hop of the mid signal (the mean of the channels):
  - FFT size by sample rate (frame_size(): ~32 ms, 256 points at 8 kHz, 512 at
    16 kHz, 1024 at 44.1/48 kHz), periodic Hann window, hop a quarter of that,
    longer on a long recording so there are at most MAX_COLUMNS columns (a 90-minute
    ICD-ST10 recording: about 18 ms per column);
  - rows: the bins from 0 Hz up to min(Nyquist, MAX_HZ) (voices and their formants
    live below 8 kHz; above that a 44.1 kHz recording has little but hiss);
  - levels in dB, mapped to 0..255 over RANGE_DB below the recording's loudest
    bins (its 99.9th percentile), so a quiet recording is not drawn black.
Coarser levels for zoomed-out views halve the columns each time, keeping the
louder of each pair (a short voice stays visible). tile() encodes TILE columns of
one level as an 8-bit palette PNG (the colour map "inferno": dark blue-black for
silence through red to pale yellow for the loudest, perceptually even, so
formants stand out as bright bands), highest frequency at the top.
"""
import struct
import zlib

import numpy as np

from . import pcm

MAX_COLUMNS = 300_000
MAX_HZ = 8000.0
RANGE_DB = 75.0
TILE = 512                  # columns per tile
BLOCK_SAMPLES = 1 << 21     # FFT input per block (columns x FFT size): about 16 MB of floats

# "inferno" (matplotlib, CC0), as Matt Zucker's polynomial fit: t in 0..1 -> RGB.
_INFERNO = ((0.0002189403691192265, 0.001651004631001012, -0.01948089843709184),
            (0.1065134194856116, 0.5639564367884091, 3.932712388889277),
            (11.60249308247187, -3.972853965665698, -15.9423941062914),
            (-41.70399613139459, 17.43639888205313, 44.35414519872813),
            (77.162935699427, -33.40235894210092, -81.80730925738993),
            (-71.31942824499214, 32.62606426397723, 73.20951985803202),
            (25.13112622477341, -12.24266895238567, -23.07032500287172))


def palette():
    """256 RGB triples (bytes) of the colour map."""
    t = np.linspace(0, 1, 256)[:, None]
    rgb = sum(np.array(c)[None, :] * t ** i for i, c in enumerate(_INFERNO))
    return (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()


# The night screen's map: black to dark red to a dim orange at the loudest, never bright, so it keeps
# night vision. The same formula as app/ui/live.js nightLut(): t in 0..1 -> RGB.
def night_palette():
    """256 RGB triples (bytes): red on black."""
    t = np.linspace(0, 1, 256)
    rgb = np.stack([0.9 * t ** 1.1, 0.32 * t ** 2.8, 0.03 * t ** 3], axis=1)
    return (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()


_PALETTE = palette()
PALETTES = {"inferno": _PALETTE, "night": night_palette()}    # the colour maps a tile can be drawn in


class Spectrogram:
    """The columns (uint8, columns x rows) of one recording, and its coarser levels."""

    def __init__(self, cols, rate, n, hop, fmax):
        self.levels = [cols]
        while self.levels[-1].shape[0] > TILE:
            c = self.levels[-1]
            if c.shape[0] % 2:
                c = np.concatenate([c, c[-1:]])
            self.levels.append(np.maximum(c[0::2], c[1::2]))
        self.rate, self.n, self.hop, self.fmax = rate, n, hop, fmax

    def info(self):
        """What the page needs to lay the tiles out."""
        return {"columns": int(self.levels[0].shape[0]), "rows": int(self.levels[0].shape[1]),
                "column_seconds": self.hop / self.rate, "levels": len(self.levels), "tile": TILE,
                "fmax": self.fmax, "fft": self.n}

    def tile(self, level, index, cmap="inferno"):
        """Tile `index` of a level as PNG bytes in the colour map cmap (PALETTES), or None if there
        is no such tile (or no such map)."""
        if not 0 <= level < len(self.levels):
            return None
        cols = self.levels[level]
        part = cols[index * TILE:(index + 1) * TILE]
        if index < 0 or not part.shape[0] or cmap not in PALETTES:
            return None
        return png(np.ascontiguousarray(part.T[::-1]), PALETTES[cmap])


def png(index_rows, palette_bytes=_PALETTE):
    """An 8-bit palette PNG of a (height, width) uint8 array of colour-map indices."""
    h, w = index_rows.shape
    raw = np.zeros((h, w + 1), np.uint8)                       # filter byte 0 (none) per row
    raw[:, 1:] = index_rows

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)) +
            chunk(b"PLTE", palette_bytes) + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


class Cancelled(Exception):
    """should_stop() said stop."""


def compute(reader, should_stop=None):
    """The Spectrogram of reader (pcm.Wav / pcm.WavFile)."""
    rate, frames = reader.rate, reader.frames
    n = pcm.frame_size(rate)
    hop = max(n // 4, -(-frames // MAX_COLUMNS))
    fmax = min(rate / 2, MAX_HZ)
    rows = int(fmax * n / rate) + 1
    count = max(1, -(-frames // hop))                          # column c is centred on sample c * hop
    w = pcm.hann(n)
    db = np.empty((count, rows), np.float16)                  # dB to 0.03 dB: half the memory of float32
    block = max(256, BLOCK_SAMPLES // n)
    for c0 in range(0, count, block):
        if should_stop is not None and should_stop():
            raise Cancelled()
        c1 = min(count, c0 + block)
        s0 = c0 * hop - n // 2
        x = reader.read(s0, (c1 - 1) * hop - n // 2 + n)
        mid = x.mean(axis=1, dtype=np.float64) if x.shape[1] > 1 else x[:, 0].astype(np.float64)
        idx = (np.arange(c1 - c0) * hop)[:, None] + np.arange(n)
        spec = np.fft.rfft(mid[idx] * w, axis=1)[:, :rows]
        db[c0:c1] = 10 * np.log10(np.maximum(spec.real ** 2 + spec.imag ** 2, 1e-20))
    top = float(np.percentile(db[::max(1, count // 20000)].astype(np.float32), 99.9))
    cols = np.empty((count, rows), np.uint8)
    for c0 in range(0, count, block):                         # mapped a block at a time: no full-size temporaries
        part = db[c0:c0 + block].astype(np.float32)
        cols[c0:c0 + block] = np.clip((part - (top - RANGE_DB)) * (255 / RANGE_DB), 0, 255)
    del db
    return Spectrogram(cols, rate, n, hop, fmax)
