"""Splitting an imported recording on silence, after it was recorded.

An analog import (a recorder played into line-in) is recorded as one file, as
Live mode records. After Stop, this module looks at the whole file and says
where it splits into one file per recording on the recorder. The whole file
is always kept; the pieces are extra files whose concatenation is the whole
file, sample for sample.

Two linear passes:
1. Levels: the RMS level (dBFS, all channels) of every 50 ms block, read from
   the file in chunks (file_levels).
2. Cuts (find_cuts), from the whole file at once:
   - the floor: what the input sounds like while the recorder plays nothing.
     Each whole second whose block levels are steady (10th to 90th percentile
     within STEADY_DB) gives its median; the floor is a low percentile of those
     medians over the entire file, fixed before any decision;
   - a gap: at least `gap` seconds of blocks at the floor (within MATCH_DB),
     with sound (blocks more than MARGIN_DB above the floor) somewhere before
     and after it. The quiet at the start and the end is never a gap, and a
     click between two stretches at the floor (less than MIN_SIGNAL of sound)
     does not break one: it stays at the end of the piece before;
   - each new piece starts PREROLL seconds before its first sound (never
     before its gap starts), so the gap stays at the end of the piece before;
   - a piece is kept only with evidence it is a recording of its own: at least
     MIN_SIGNAL seconds of sound, and its own quiet level (the 20th percentile
     from its first sound to its last) at least MARGIN_DB above the floor. A
     recording whose pauses sit at the floor (no line hiss between recordings
     to tell them apart) is never cut at those pauses. A piece without that
     evidence joins the next one.
When in doubt, no cut.
"""
import math
import wave

import numpy as np

BLOCK_SECONDS = 0.05
MARGIN_DB = 8.0              # sound: this far above the floor
MATCH_DB = 4.0               # at the floor: within this of it
STEADY_DB = 6.0              # a whole second whose block levels stay within this (10th-90th percentile) is steady
FLOOR_PERCENTILE = 10        # the floor: this percentile of the steady seconds' medians
PIECE_PERCENTILE = 20        # a piece's own quiet level
MIN_FLOOR_DB = -100.0        # an all-zero input is never taken as the floor itself
PREROLL = 0.5
MIN_SIGNAL = 1.0
DEFAULT_GAP = 3.0
MAX_GAP = 60.0
SILENT_DB = -120.0
READ_FRAMES = 1 << 20        # frames read from the file at a time


class Cancelled(Exception):
    """The analysis was stopped by its should_stop callback."""


def block_levels(samples, channels, block):
    """dBFS (RMS, all channels) of each whole block of `block` frames in an int16 array."""
    n = len(samples) // (channels * block)
    if n == 0:
        return np.zeros(0)
    x = samples[:n * channels * block].astype(np.float64).reshape(n, channels * block) / 32768.0
    ms = np.mean(x * x, axis=1)
    with np.errstate(divide="ignore"):
        return np.maximum(SILENT_DB, 10.0 * np.log10(ms))


def block_frames(rate):
    return max(1, int(round(rate * BLOCK_SECONDS)))


def file_levels(path, should_stop=None, progress=None):
    """(levels, rate, channels, frames) of a 16-bit PCM WAV: the level of each whole
    block (the last partial block has none). progress(done, total) in frames."""
    with wave.open(path) as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM can be split")
        rate, channels, frames = w.getframerate(), w.getnchannels(), w.getnframes()
        block = block_frames(rate)
        step = max(block, READ_FRAMES - READ_FRAMES % block)
        out, done = [], 0
        while done < frames:
            if should_stop is not None and should_stop():
                raise Cancelled()
            data = w.readframes(min(step, frames - done))
            if not data:
                break
            n = len(data) // (2 * channels)
            out.append(block_levels(np.frombuffer(data, dtype="<i2"), channels, block).astype(np.float32))
            done += n
            if progress is not None:
                progress(done, frames)
    return (np.concatenate(out) if out else np.zeros(0, np.float32)), rate, channels, frames


def floor_of(levels, per_second):
    """The floor (dBFS) of a whole recording's block levels, or None when it has no
    steady second at all."""
    n = len(levels) // per_second
    if n == 0:
        return None
    sec = np.asarray(levels[:n * per_second], dtype=np.float64).reshape(n, per_second)
    p10, med, p90 = np.percentile(sec, [10, 50, 90], axis=1)
    steady = med[(p90 - p10) <= STEADY_DB]
    if not len(steady):
        return None
    return max(float(np.percentile(steady, FLOOR_PERCENTILE)), MIN_FLOOR_DB)


def _runs(mask):
    """(starts, ends) of the runs of True in a boolean array (ends exclusive)."""
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


def find_cuts(levels, rate, gap=DEFAULT_GAP, preroll=PREROLL, min_signal=MIN_SIGNAL):
    """The block indexes where pieces after the first begin ([] for one piece)."""
    per_second = int(round(1.0 / BLOCK_SECONDS))
    levels = np.asarray(levels, dtype=np.float64)
    floor = floor_of(levels, per_second)
    if floor is None:
        return []
    sound = levels > floor + MARGIN_DB
    sound_at = np.flatnonzero(sound)
    if not len(sound_at):
        return []
    gap_blocks = max(1, int(math.ceil(gap / BLOCK_SECONDS)))
    pre = int(round(preroll / BLOCK_SECONDS))
    starts, ends = _runs(levels <= floor + MATCH_DB)
    min_blocks = int(math.ceil(min_signal / BLOCK_SECONDS))
    # A click between two stretches at the floor (less than min_signal of sound) does not end a
    # gap: the two stretches are one gap, and the click stays with the piece before.
    counted = np.concatenate([[0], np.cumsum(sound)])
    runs = []
    for a, b in zip(starts, ends):
        if runs and counted[a] - counted[runs[-1][1]] < min_blocks:
            runs[-1][1] = int(b)
        else:
            runs.append([int(a), int(b)])
    candidates = []
    for a, b in runs:
        if b - a < gap_blocks or a <= sound_at[0] or b > sound_at[-1]:
            continue                             # too short, or before the first / after the last sound
        nxt = sound_at[np.searchsorted(sound_at, b)]   # the next sound after the gap
        candidates.append((max(int(a), int(nxt) - pre), a))   # (where the next piece starts, where this gap starts)

    def evidence(a, b):                          # the piece's content: from a to b (its gap left out)
        idx = sound_at[(sound_at >= a) & (sound_at < b)]
        if len(idx) < min_blocks:
            return False
        own = levels[idx[0]:idx[-1] + 1]
        return float(np.percentile(own, PIECE_PERCENTILE)) >= floor + MARGIN_DB

    cuts, begin = [], 0
    for c, gap_start in candidates:
        if evidence(begin, gap_start):           # the piece up to this gap stands on its own
            cuts.append(c)
            begin = c
    while cuts and not evidence(cuts[-1], len(levels)):
        cuts.pop()                               # the last piece does not: it joins the one before
    return cuts


def plan(path, gap=DEFAULT_GAP, should_stop=None, progress=None):
    """(cut frames, rate, channels, frames) for a WAV: the frame offsets where pieces
    after the first begin."""
    levels, rate, channels, frames = file_levels(path, should_stop, progress)
    block = block_frames(rate)
    return [c * block for c in find_cuts(levels, rate, gap)], rate, channels, frames
