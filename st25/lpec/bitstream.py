"""LPEC bitstream: MSB-first bit reading, mode -> frame length, field
parsing into a frame record.

See docs/lpec.md, "Bitstream" (Field order, Coefficient block (budget
beta), What lag and pgidx select), and "Configuration constants". This
module reads the raw fields a frame is made of; it does not decode them
into physical quantities (LSPs, gains, spectral coefficients) -- that is
st25/lpec/params.py's job, from the frame record this module returns.

Frame record shape
-------------------
``parse_frame(data, prev_lsp1_i1, ab_table)`` returns a ``Frame``:

- ``mode`` (0-3), ``num_bytes`` (bytes handed to this frame -- the chunk
  ``split_frames`` produced; less than the mode's nominal length only for
  the last, truncated frame of a stream), ``truncated`` (bool).
- ``mid_frame_lsp``: the ``F`` flag (mode 0 only; 0 otherwise).
- ``lsp_a``: ``[i1, i2, i3]`` for parameter slot 1 ("set A"), or ``None``
  when slot 1 isn't read this frame (its LSPs are interpolated instead;
  see docs/lpec.md "Parameter sets").
- ``lsp_b``: ``[i1, i2, i3]`` for slot 2 ("set B"); always read.
- ``pitch_a``, ``pitch_b``: ``[lag, pgidx]`` for slots 1 and 2; ``pgidx``
  is ``None`` when ``lag == 0`` (no pgidx field is read). ``pitch_a`` is
  ``None`` in modes 1 and 3 (no slot-1 pitch).
- ``shape_flags``: ``[S0, S1]``, mode 0 only, else ``None``.
- ``shape_indices``: ``[idx0, idx1]`` (each ``None`` when its flag is 0),
  mode 0 only, else ``None``.
- ``blocks``: a list of ``CoefficientBlock``, in field order (two for
  modes 0 and 2, one for modes 1 and 3).
- ``lsp1_i1_next``: the "stage-1 LSP index of slot 1" state to carry into
  the *next* frame's ``parse_frame`` call as ``prev_lsp1_i1`` (see docs/
  lpec.md, "State between frames"; only mode-0 frames actually change it
  -- F=0 sets it to -1, F=1 sets it to set A's own i1; other modes pass
  ``prev_lsp1_i1`` straight through). The very first frame after
  ``InitDecoder`` is called with ``prev_lsp1_i1=0`` (the doc's observed
  heap-zero default).

``CoefficientBlock`` fields: ``slot`` (1 or 2), ``type`` (0-3, transform
type t), ``budget`` (the field-order-computed beta passed in), ``i1`` (the
stage-1 LSP index actually used for allocation, after the "slot-1 quirk"
substitution), ``global_gain`` (7-bit), ``band_gain1``/``band_gain2``
(6-bit each), ``alloc`` (the ``Allocation`` computed from beta and ``i1``),
``vq2``/``vq4``/``vq8`` (the raw 8-bit VQ indices, ``alloc.k0``/``k1``/
``k2`` of each), ``signs`` (``alloc.ks`` raw 0/1 bits; 0 means +0.6, 1
means -0.6 per docs/lpec.md "Filling the coefficients"), and ``bits_used``
(the block's actual bit length: 19 + 8*(k0+k1+k2) + ks, which can be a
few bits less than ``budget`` -- see "Bit allocation" steps 6-7).

``Allocation`` fields: ``n4``/``n2``/``n1``/``ns`` (per-band coefficient
counts, 8 entries each, in the 4-bit/2-bit/1-bit/sign-only rate classes)
and the totals ``k0``/``k1``/``k2``/``ks`` (VQ index counts and total sign
count). Ranking coefficients within a band and assigning VQ/sign/noise
values to them (docs/lpec.md "Ranking" and "Filling the coefficients") is
params.py's job; this module only frames how many raw bits of each kind
the bitstream holds.

Callers must supply the extracted allocation-base table ``AB`` (64 x 8
int16, ``Tables.AB`` from st25.lpec.tables) -- this module does not load
tables itself, so its pure bit-reading logic is testable without the
(git-ignored, locally-generated) table data file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

# Frame length in bytes by mode (docs/lpec.md, "Bitstream").
MODE_BYTES = {0: 48, 1: 36, 2: 60, 3: 48}

# Frame bit budget by mode (docs/lpec.md, "Bitstream" and "Configuration
# constants": "budget by mode | 384, 288, 480, 384 bits").
MODE_BITS = {0: 384, 1: 288, 2: 480, 3: 384}

BANDS = 8  # docs/lpec.md, "Configuration constants": bands = 8.

# type t -> (band width W[t], allocation reference ref[t], allocation
# scale sc[t]); docs/lpec.md, "Configuration constants".
_ALLOC_CONSTS = {
    0: (28, 128, 18725),
    1: (42, 192, 12483),
    2: (42, 192, 12483),
    3: (56, 256, 9362),
}


# --------------------------------------------------------------------------
# Frame records
# --------------------------------------------------------------------------

@dataclass
class Allocation:
    """Per-band bit allocation for one coefficient block (plain ints)."""

    n4: List[int]
    n2: List[int]
    n1: List[int]
    ns: List[int]
    k0: int
    k1: int
    k2: int
    ks: int


@dataclass
class CoefficientBlock:
    """One coefficient block's raw fields (docs/lpec.md, "Coefficient
    block (budget beta)")."""

    slot: int
    type: int
    budget: int
    i1: int
    global_gain: int
    band_gain1: int
    band_gain2: int
    alloc: Allocation
    vq2: List[int]
    vq4: List[int]
    vq8: List[int]
    signs: List[int]
    bits_used: int


@dataclass
class Frame:
    """One frame's raw bitstream fields (docs/lpec.md, "Field order")."""

    mode: int
    num_bytes: int
    truncated: bool
    mid_frame_lsp: int
    lsp_a: Optional[List[int]]
    lsp_b: List[int]
    pitch_a: Optional[List[Optional[int]]]
    pitch_b: List[Optional[int]]
    shape_flags: Optional[List[int]]
    shape_indices: Optional[List[Optional[int]]]
    blocks: List[CoefficientBlock]
    lsp1_i1_next: int


# --------------------------------------------------------------------------
# MSB-first bit reader
# --------------------------------------------------------------------------

class BitReader:
    """MSB-first bit reader over a fixed byte buffer.

    Bytes are read in order, each byte from its most significant bit down
    (docs/lpec.md, "Bitstream", "Bit order"). Reads wrap to the start of
    the buffer once its bits are exhausted: "API behaviour and framing"
    ("Short input") says the DLL's reader wraps to the start of the same
    n bytes and keeps reading when a frame needs more bits than it was
    given -- what the last, truncated frame of a recording goes through.
    ``pos`` counts every bit read, uncapped (it is the doc's "used" bit
    counter); only the *indexing* into ``data`` wraps.
    """

    def __init__(self, data: bytes):
        if not data:
            raise ValueError("BitReader needs at least one byte")
        self.data = data
        self.nbits = len(data) * 8
        self.pos = 0

    def read_bits(self, k: int) -> int:
        value = 0
        for _ in range(k):
            i = self.pos % self.nbits
            byte = self.data[i // 8]
            bit = (byte >> (7 - (i % 8))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value


# --------------------------------------------------------------------------
# Frame mode -> length, and payload splitting
# --------------------------------------------------------------------------

def frame_mode(first_byte: int) -> int:
    """The 2-bit mode: a frame's first field, the top two bits of its
    first byte (MSB-first)."""
    return (first_byte >> 6) & 0b11


def split_frames(payload: bytes) -> List[bytes]:
    """Split a frame stream into per-frame byte chunks.

    Each frame's length follows from its first 2 bits (the mode), per
    docs/lpec.md "Bitstream". If the stream ends before the last frame's
    nominal length, that final chunk is short: it is still one frame (the
    decoder decodes it via BitReader's wraparound), and splitting stops
    there.
    """
    frames = []
    pos = 0
    n = len(payload)
    while pos < n:
        mode = frame_mode(payload[pos])
        length = MODE_BYTES[mode]
        take = min(length, n - pos)
        frames.append(payload[pos:pos + take])
        pos += take
    return frames


# --------------------------------------------------------------------------
# Integer helpers (docs/lpec.md, "Conventions")
# --------------------------------------------------------------------------

def _s16(x: int) -> int:
    """s16(x): the low 16 bits of x as a signed value (a store to int16)."""
    x &= 0xFFFF
    return x - 0x10000 if x & 0x8000 else x


# --------------------------------------------------------------------------
# Small raw fields (docs/lpec.md, "Field order")
# --------------------------------------------------------------------------

def _read_lsp_set(reader: BitReader) -> List[int]:
    """Three 6-bit stage indices [i1, i2, i3] ("LSP set")."""
    return [reader.read_bits(6) for _ in range(3)]


def _read_pitch(reader: BitReader) -> List[Optional[int]]:
    """[lag, pgidx]; a 7-bit lag, then a 6-bit pgidx only when lag != 0."""
    lag = reader.read_bits(7)
    pgidx = reader.read_bits(6) if lag != 0 else None
    return [lag, pgidx]


def _slot1_i1(prev_lsp1_i1: int, lsp_b_i1: int) -> int:
    """The stage-1 LSP index used for a block on slot 1: slot 1's own
    stored index, or slot 2's when slot 1's is -1 (docs/lpec.md, "Bit
    allocation", "Slot-1 quirk")."""
    return lsp_b_i1 if prev_lsp1_i1 == -1 else prev_lsp1_i1


# --------------------------------------------------------------------------
# Bit allocation (docs/lpec.md, "Coefficient block" > "Bit allocation")
# --------------------------------------------------------------------------

def _band_split(ab_value: int, adj: int, W: int):
    """n4, n2, n1 for one band (step 2)."""
    x = ab_value + adj
    u = ((20480 - (x >> 1)) * x) >> 14
    n2 = ((((u * u) >> 15) * 29127 >> 13) * W) >> 15
    n4 = ((((x * x) >> 15) * 29127 >> 15) * W) >> 15
    n1 = ((x * W) >> 13) - (4 * n4 + 2 * n2)
    if n1 > (W - n4) - n2:
        n1 = 0
        n4 = ((x - 16384) * W + 8192) >> 14
        n2 = W - n4
    return n4, n2, n1


def bit_allocation(ab_row: Sequence[int], t: int, beta: int) -> Allocation:
    """The per-band bit allocation for one coefficient block.

    ``ab_row`` is AB[i1] (the allocation-base row for the block's
    stage-1 LSP index, with the "slot-1 quirk" substitution already
    applied by the caller). ``beta`` is the block's total bit budget
    (``B = beta - 19`` is the input to the allocation itself).
    """
    W, ref, sc = _ALLOC_CONSTS[t]
    B = beta - 19
    adj = _s16(((B - ref) * sc) >> 9)

    n4 = [0] * BANDS
    n2 = [0] * BANDS
    n1 = [0] * BANDS
    for b in range(BANDS):
        n4[b], n2[b], n1[b] = _band_split(ab_row[b], adj, W)

    S4, S2, S1 = sum(n4), sum(n2), sum(n1)

    # Step 4: S4 even.
    if S4 % 2 != 0:
        for b in range(BANDS - 1, -1, -1):
            if n4[b] > 0:
                n4[b] -= 1
                n2[b] += 2
                S4 -= 1
                S2 += 2
                break

    # Step 5: S2 a multiple of 4.
    rho = S2 % 4
    if rho != 0:
        if B <= 4 * S4 + 2 * S2 + S1:
            for b in range(BANDS - 1, -1, -1):
                if rho == 0:
                    break
                if n2[b] > 0:
                    n2[b] -= 1
                    n1[b] += 1
                    S2 -= 1
                    S1 += 1
                    rho -= 1
        else:
            rho = 4 - rho
            for b in range(BANDS):
                if rho == 0:
                    break
                if n1[b] > 0:
                    n1[b] -= 1
                    n2[b] += 1
                    S1 -= 1
                    S2 += 1
                    rho -= 1

    # Step 6: fill to a byte.
    T42 = 4 * S4 + 2 * S2
    eps = 8 * (B // 8) - (T42 + S1)
    if eps < 0:
        b = BANDS - 1
        guard = 0
        while eps < 0:
            if n1[b] > 0:
                n1[b] -= 1
                S1 -= 1
                eps += 1
            b = BANDS - 1 if b == 0 else b - 1
            guard += 1
            if guard > 100000:
                # docs/lpec.md, "Open points": unreached in any known test --
                # "the DLL would loop forever if every n1 were 0".
                raise ArithmeticError(
                    "bit allocation: fill-to-byte did not converge (every "
                    "band's n1 stayed 0)"
                )
    elif eps > 0:
        for b in range(BANDS):
            if eps == 0:
                break
            kappa = W - (n1[b] + n2[b] + n4[b])
            if kappa > 0:
                add = min(kappa, (eps + 1) // 2)
                n1[b] += add
                S1 += add
                eps -= add

    # Step 7: signs.
    ns = [0] * BANDS
    rho_s = B - (T42 + S1)
    for b in range(BANDS):
        kappa = W - (n1[b] + n2[b] + n4[b])
        if kappa < 1:
            ns[b] = 0
        else:
            ns[b] = min(kappa, rho_s)
            rho_s -= ns[b]
        if rho_s <= 0:
            break

    k0, k1, k2 = S4 // 2, S2 // 4, S1 // 8
    ks = sum(ns)
    return Allocation(n4=n4, n2=n2, n1=n1, ns=ns, k0=k0, k1=k1, k2=k2, ks=ks)


def _read_coefficient_block(
    reader: BitReader, slot: int, t: int, beta: int, i1: int, ab_table: Sequence[Sequence[int]]
) -> CoefficientBlock:
    """Steps 1-5 of "Coefficient block (budget beta)"."""
    start = reader.pos
    global_gain = reader.read_bits(7)
    band_gain1 = reader.read_bits(6)
    band_gain2 = reader.read_bits(6)
    alloc = bit_allocation(ab_table[i1], t, beta)
    vq2 = [reader.read_bits(8) for _ in range(alloc.k0)]
    vq4 = [reader.read_bits(8) for _ in range(alloc.k1)]
    vq8 = [reader.read_bits(8) for _ in range(alloc.k2)]
    signs = [reader.read_bits(1) for _ in range(alloc.ks)]
    return CoefficientBlock(
        slot=slot,
        type=t,
        budget=beta,
        i1=i1,
        global_gain=global_gain,
        band_gain1=band_gain1,
        band_gain2=band_gain2,
        alloc=alloc,
        vq2=vq2,
        vq4=vq4,
        vq8=vq8,
        signs=signs,
        bits_used=reader.pos - start,
    )


# --------------------------------------------------------------------------
# Field order, per mode (docs/lpec.md, "Field order")
# --------------------------------------------------------------------------

def _parse_mode0(reader: BitReader, prev_lsp1_i1: int, ab_table) -> Frame:
    F = reader.read_bits(1)
    if F == 0:
        lsp_a = None
        lsp_b = _read_lsp_set(reader)
        lsp1_i1_next = -1
        i1_block0 = _slot1_i1(-1, lsp_b[0])
    else:
        lsp_a = _read_lsp_set(reader)
        lsp_b = _read_lsp_set(reader)
        lsp1_i1_next = lsp_a[0]
        i1_block0 = lsp_a[0]

    pitch_a = _read_pitch(reader)
    pitch_b = _read_pitch(reader)

    r = 384 - reader.pos

    shape_flags = []
    shape_indices = []
    for _ in range(2):
        s = reader.read_bits(1)
        shape_flags.append(s)
        shape_indices.append(reader.read_bits(7) if s == 1 else None)

    c0 = 8 if shape_flags[0] == 1 else 1
    c1 = 8 if shape_flags[1] == 1 else 1
    beta0 = (r // 2) - c0
    beta1 = (r // 2) - c1

    block0 = _read_coefficient_block(reader, 1, 0, beta0, i1_block0, ab_table)
    block1 = _read_coefficient_block(reader, 2, 0, beta1, lsp_b[0], ab_table)

    return Frame(
        mode=0,
        num_bytes=len(reader.data),
        truncated=len(reader.data) < MODE_BYTES[0],
        mid_frame_lsp=F,
        lsp_a=lsp_a,
        lsp_b=lsp_b,
        pitch_a=pitch_a,
        pitch_b=pitch_b,
        shape_flags=shape_flags,
        shape_indices=shape_indices,
        blocks=[block0, block1],
        lsp1_i1_next=lsp1_i1_next,
    )


def _parse_mode1(reader: BitReader, prev_lsp1_i1: int, ab_table) -> Frame:
    lsp_b = _read_lsp_set(reader)
    pitch_b = _read_pitch(reader)
    beta = 288 - reader.pos
    block = _read_coefficient_block(reader, 2, 1, beta, lsp_b[0], ab_table)
    return Frame(
        mode=1,
        num_bytes=len(reader.data),
        truncated=len(reader.data) < MODE_BYTES[1],
        mid_frame_lsp=0,
        lsp_a=None,
        lsp_b=lsp_b,
        pitch_a=None,
        pitch_b=pitch_b,
        shape_flags=None,
        shape_indices=None,
        blocks=[block],
        lsp1_i1_next=prev_lsp1_i1,
    )


def _parse_mode2(reader: BitReader, prev_lsp1_i1: int, ab_table) -> Frame:
    lsp_b = _read_lsp_set(reader)
    pitch_a = _read_pitch(reader)
    pitch_b = _read_pitch(reader)

    beta0 = (384 - reader.pos) // 2  # docs/lpec.md: "note 384, not 480"
    i1_block0 = _slot1_i1(prev_lsp1_i1, lsp_b[0])
    block0 = _read_coefficient_block(reader, 1, 0, beta0, i1_block0, ab_table)

    beta1 = 480 - reader.pos  # "used counted after the first block"
    block1 = _read_coefficient_block(reader, 2, 2, beta1, lsp_b[0], ab_table)

    return Frame(
        mode=2,
        num_bytes=len(reader.data),
        truncated=len(reader.data) < MODE_BYTES[2],
        mid_frame_lsp=0,
        lsp_a=None,
        lsp_b=lsp_b,
        pitch_a=pitch_a,
        pitch_b=pitch_b,
        shape_flags=None,
        shape_indices=None,
        blocks=[block0, block1],
        lsp1_i1_next=prev_lsp1_i1,  # mode 2 leaves the stored slot-1 index alone
    )


def _parse_mode3(reader: BitReader, prev_lsp1_i1: int, ab_table) -> Frame:
    lsp_b = _read_lsp_set(reader)
    pitch_b = _read_pitch(reader)
    beta = 384 - reader.pos
    block = _read_coefficient_block(reader, 2, 3, beta, lsp_b[0], ab_table)
    return Frame(
        mode=3,
        num_bytes=len(reader.data),
        truncated=len(reader.data) < MODE_BYTES[3],
        mid_frame_lsp=0,
        lsp_a=None,
        lsp_b=lsp_b,
        pitch_a=None,
        pitch_b=pitch_b,
        shape_flags=None,
        shape_indices=None,
        blocks=[block],
        lsp1_i1_next=prev_lsp1_i1,
    )


_MODE_PARSERS = {0: _parse_mode0, 1: _parse_mode1, 2: _parse_mode2, 3: _parse_mode3}


def parse_frame(data: bytes, prev_lsp1_i1: int, ab_table: Sequence[Sequence[int]]) -> Frame:
    """Parse one frame's worth of bytes (as produced by ``split_frames``)
    into a ``Frame`` record of plain ints and lists.

    ``prev_lsp1_i1`` is the persistent "stage-1 LSP index of slot 1" state
    from the previous frame (0 for the first frame after ``InitDecoder``;
    see the module docstring and docs/lpec.md "State between frames").
    ``ab_table`` is the extracted 64 x 8 allocation-base table (``Tables.AB``).
    """
    reader = BitReader(data)
    mode = reader.read_bits(2)
    return _MODE_PARSERS[mode](reader, prev_lsp1_i1, ab_table)
