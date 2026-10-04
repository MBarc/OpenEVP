"""Suggesting where an imported recording splits into one file per recording.

An analog import (a recorder played into line-in) is recorded as one file, as
Live mode records. After Stop, this module looks at the whole file and
suggests cuts; the user sees them on the waveform, changes them, and confirms
(or keeps the import as one file). Nothing is split without that, so the
suggestions may be imperfect; they err towards suggesting nothing.

Two linear passes:
1. Levels: the RMS level (dBFS, all channels) of every 50 ms block, read from
   the file in chunks (file_levels).
2. Cuts (find_cuts), from the whole file at once, with prefix counts (no
   percentile per candidate), checking for cancellation as it goes:
   - the floor: what the input sounds like while the recorder plays nothing.
     Each whole second whose block levels are steady (10th to 90th percentile
     within STEADY_DB) gives its median; the floor is a low percentile of those
     medians over the entire file;
   - the background: the level the recordings themselves sit at between their
     sounds (room tone): the most common median of the steady seconds above the
     floor. Without one, or without content standing out from it (more than
     MARGIN_DB above it), nothing is suggested;
   - a gap: at least `gap` seconds of blocks at the floor (within MATCH_DB),
     with sound (more than MARGIN_DB above the floor) before and after it. Two
     stretches at the floor are one gap only when what is between them is short
     (under MERGE_OTHER in all and MERGE_SHARE of the span, and under
     MIN_SIGNAL of sound: a click); the quiet at the start and the end is never
     a gap;
   - each cut is PREROLL seconds before the next sound (never before its gap);
   - a piece is suggested only if its content (from its first sound up to its
     gap) has at least MIN_SIGNAL of sound, at least PIECE_SHARE of it at the
     background, and less than PIECE_SHARE at the floor. A recording whose
     pauses sit at the floor, or that is all loud with no background of its own,
     is never cut; a piece without the evidence joins the next one.
"""
import math
import wave

import numpy as np

BLOCK_SECONDS = 0.05
MARGIN_DB = 8.0              # sound: this far above the floor
MATCH_DB = 4.0               # at the floor: within this of it
STEADY_DB = 6.0              # a whole second whose block levels stay within this (10th-90th percentile) is steady
FLOOR_PERCENTILE = 10        # the floor: this percentile of the steady seconds' medians
PIECE_SHARE = 0.2            # a piece: at least this share of its content at the recordings' background, under it at the floor
MERGE_OTHER = 0.5            # seconds: two stretches at the floor are one gap only with less than this between, in all
MERGE_SHARE = 0.2            # ... and under this share of the merged span
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


def background_of(levels, per_second, floor):
    """The level the recordings themselves sit at between their sounds (room tone, the
    recorder's own hiss): the most common median of the steady seconds above the floor
    by the margin, to the dB. None when there is none."""
    n = len(levels) // per_second
    if n == 0:
        return None
    sec = np.asarray(levels[:n * per_second], dtype=np.float64).reshape(n, per_second)
    p10, med, p90 = np.percentile(sec, [10, 50, 90], axis=1)
    above = med[((p90 - p10) <= STEADY_DB) & (med > floor + MARGIN_DB)]
    if not len(above):
        return None
    dbs = np.round(above).astype(np.int64)
    counts = np.bincount(dbs - dbs.min())
    return float(dbs.min() + int(np.argmax(counts)))


def find_cuts(levels, rate, gap=DEFAULT_GAP, preroll=PREROLL, min_signal=MIN_SIGNAL, should_stop=None):
    """Suggested cuts: the block indexes where pieces after the first begin ([] for one
    piece). Linear in the number of blocks (prefix counts; no percentile per candidate),
    and stopped by should_stop (Cancelled)."""
    def check():
        if should_stop is not None and should_stop():
            raise Cancelled()
    check()
    per_second = int(round(1.0 / BLOCK_SECONDS))
    levels = np.asarray(levels, dtype=np.float64)
    floor = floor_of(levels, per_second)
    if floor is None:
        return []
    check()
    background = background_of(levels, per_second, floor)
    if background is None:
        return []                                # nothing in the file but the floor and passing sound
    sound = levels > floor + MARGIN_DB
    loud = levels > background + MARGIN_DB
    if loud.sum() < int(math.ceil(min_signal / BLOCK_SECONDS)):
        return []                                # no content stands out from that background at all
    at_floor = levels <= floor + MATCH_DB
    near_bg = np.abs(levels - background) <= MATCH_DB
    pre_sound, pre_floor, pre_bg = (np.concatenate([[0], np.cumsum(m)]) for m in (sound, at_floor, near_bg))
    sound_at = np.flatnonzero(sound)
    if not len(sound_at):
        return []
    check()
    gap_blocks = max(1, int(math.ceil(gap / BLOCK_SECONDS)))
    pre = int(round(preroll / BLOCK_SECONDS))
    min_blocks = int(math.ceil(min_signal / BLOCK_SECONDS))
    max_other = int(round(MERGE_OTHER / BLOCK_SECONDS))
    # Stretches at the floor; two of them with a little between (a click, a dip) are one gap,
    # only when what is between is short: under MERGE_OTHER in all, under MERGE_SHARE of the
    # merged span, with less than min_signal of sound.
    starts, ends = _runs(at_floor)
    runs = []                                    # [start, end, blocks not at the floor inside]
    for k, (a, b) in enumerate(zip(starts, ends)):
        if k % 4096 == 0:
            check()
        a, b = int(a), int(b)
        if runs:
            pa, pb, other = runs[-1]
            between = a - pb
            if (other + between <= max_other and (other + between) <= MERGE_SHARE * (b - pa)
                    and pre_sound[a] - pre_sound[pb] < min_blocks):
                runs[-1] = [pa, b, other + between]
                continue
        runs.append([a, b, 0])

    def evidence(a, b):
        """Does the piece whose content runs from a to b (its gap left out) stand on its own?
        At least min_signal of sound, a fair share of the recordings' own background (so it
        shows a background distinct from the floor), and hardly any of it at the floor."""
        if b <= a:
            return False
        first = sound_at[np.searchsorted(sound_at, a)] if np.searchsorted(sound_at, a) < len(sound_at) else b
        if first >= b:
            return False
        length = b - first
        return (pre_sound[b] - pre_sound[first] >= min_blocks
                and pre_bg[b] - pre_bg[first] >= PIECE_SHARE * length
                and pre_floor[b] - pre_floor[first] < PIECE_SHARE * length)

    cuts, begin = [], 0
    for k, (a, b, _) in enumerate(runs):
        if k % 4096 == 0:
            check()
        if b - a < gap_blocks or a <= sound_at[0] or b > sound_at[-1]:
            continue                             # too short, or before the first / after the last sound
        nxt = int(sound_at[np.searchsorted(sound_at, b)])   # the next sound after the gap
        cut = max(a, nxt - pre)
        if evidence(begin, a):                   # the piece up to this gap stands on its own
            cuts.append(cut)
            begin = cut
    last = int(sound_at[-1]) + 1
    while cuts and not evidence(cuts[-1], last):
        cuts.pop()                               # the last piece does not: it joins the one before
    return cuts


def plan(path, gap=DEFAULT_GAP, should_stop=None, progress=None):
    """(cut frames, rate, channels, frames) for a WAV: the frame offsets where pieces
    after the first begin (suggestions)."""
    levels, rate, channels, frames = file_levels(path, should_stop, progress)
    block = block_frames(rate)
    return [c * block for c in find_cuts(levels, rate, gap, should_stop=should_stop)], rate, channels, frames
