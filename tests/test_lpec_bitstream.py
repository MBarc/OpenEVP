"""Tests for openevp.decoders.sony_lpec.bitstream: the MSB-first bit reader, mode -> frame
length splitting, and full field-order / coefficient-block parsing. See
docs/lpec.md, "Bitstream".
"""

import glob
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))
from openevp.decoders.sony_lpec import TablesMissing, bitstream, tables  # noqa: E402
import release_gate  # noqa: E402
import make_test_dvf  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS_DIR = REPO_ROOT / "tests" / "vectors"


def _load_tables_once():
    try:
        return tables.load()
    except TablesMissing:
        return None


_TABLES = _load_tables_once()


def _vector_metas():
    return sorted(glob.glob(str(VECTORS_DIR / "*.json")))


class BitReaderTests(unittest.TestCase):
    """The MSB-first bit reader itself: order, boundaries, wraparound."""

    def test_reads_bits_msb_first_across_byte_boundaries(self):
        # 0xB4 0x2F = 1011 0100 0010 1111
        r = bitstream.BitReader(bytes([0xB4, 0x2F]))
        self.assertEqual(r.read_bits(1), 1)
        self.assertEqual(r.read_bits(3), 0b011)
        self.assertEqual(r.read_bits(4), 0b0100)
        self.assertEqual(r.read_bits(8), 0b00101111)
        self.assertEqual(r.pos, 16)

    def test_a_field_can_cross_a_byte_boundary(self):
        r = bitstream.BitReader(bytes([0b00000001, 0b10000000]))
        r.read_bits(7)  # consume the seven leading zero bits of byte 0
        self.assertEqual(r.read_bits(3), 0b110)  # last bit of byte0, first two of byte1

    def test_wraps_to_the_start_of_the_same_buffer_when_exhausted(self):
        # docs/lpec.md, "API behaviour and framing" (Short input): the reader
        # wraps to the start of the same bytes and keeps reading; `pos` keeps
        # counting (it is the doc's "used" bit counter), only indexing wraps.
        r = bitstream.BitReader(bytes([0b10110010]))
        first_byte = r.read_bits(8)
        wrapped = r.read_bits(8)
        self.assertEqual(first_byte, wrapped)
        self.assertEqual(r.pos, 16)

    def test_rejects_an_empty_buffer(self):
        with self.assertRaises(ValueError):
            bitstream.BitReader(b"")


class SplitFramesTests(unittest.TestCase):
    """Mode -> frame length splitting, against synthetic and real payloads."""

    def _mode_byte(self, mode):
        return mode << 6

    def test_mode_is_the_top_two_bits(self):
        for mode in range(4):
            self.assertEqual(bitstream.frame_mode(self._mode_byte(mode) | 0x3F), mode)

    def test_splits_a_clean_run_of_frames_by_mode(self):
        payload = (
            bytes([self._mode_byte(0)]) + bytes(47)   # mode 0 -> 48 bytes
            + bytes([self._mode_byte(1)]) + bytes(35)  # mode 1 -> 36 bytes
            + bytes([self._mode_byte(2)]) + bytes(59)  # mode 2 -> 60 bytes
        )
        frames = bitstream.split_frames(payload)
        self.assertEqual([len(f) for f in frames], [48, 36, 60])

    def test_a_short_final_frame_is_kept_as_one_truncated_chunk(self):
        # A mode-2 (60-byte) frame with only 48 bytes actually available:
        # the "ends-mid-frame" test vector's scenario.
        payload = bytes([self._mode_byte(2)]) + bytes(47)
        frames = bitstream.split_frames(payload)
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 48)

    def test_every_vector_splits_into_the_documented_frame_count_and_sizes(self):
        metas = _vector_metas()
        self.assertGreaterEqual(len(metas), 10)
        for path in metas:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f)
            with self.subTest(vector=meta["name"]):
                with open(VECTORS_DIR / (meta["name"] + ".dvf"), "rb") as f:
                    data = f.read()
                payload = make_test_dvf.payload(data)
                frames = bitstream.split_frames(payload)
                self.assertEqual(len(frames), meta["frames"])
                got_sizes = {}
                for f in frames:
                    got_sizes[str(len(f))] = got_sizes.get(str(len(f)), 0) + 1
                self.assertEqual(got_sizes, meta["frame_sizes"])


class BitAllocationTests(unittest.TestCase):
    """The "Bit allocation" arithmetic in isolation, with a stub AB row."""

    def test_allocation_counts_are_non_negative_and_fit_the_block(self):
        # A representative AB row (doesn't need to be the real extracted
        # table -- this test only exercises the integer arithmetic itself).
        ab_row = [200, 180, 160, 140, 120, 100, 80, 60]
        for t, beta in ((0, 365), (1, 269), (2, 269), (3, 365)):
            with self.subTest(type=t, beta=beta):
                alloc = bitstream.bit_allocation(ab_row, t, beta)
                self.assertTrue(all(k >= 0 for k in (alloc.k0, alloc.k1, alloc.k2, alloc.ks)))
                W = bitstream._ALLOC_CONSTS[t][0]
                for b in range(8):
                    self.assertLessEqual(alloc.n4[b] + alloc.n2[b] + alloc.n1[b] + alloc.ns[b], W)
                # Step 4 always leaves S4 even and step 5 always leaves S2 a
                # multiple of 4 (both steps only stop once that holds), so
                # K0 and K1 are exact halves/quarters of the band sums. S1
                # (and so K2) is only forced to a multiple of 8 when step 6
                # has enough band capacity to finish in its one pass -- not
                # asserted here since these ab/beta values are synthetic.
                self.assertEqual(alloc.k0 * 2, sum(alloc.n4))
                self.assertEqual(alloc.k1 * 4, sum(alloc.n2))
                self.assertLessEqual(alloc.k2 * 8, sum(alloc.n1))

    def test_index_and_sign_bits_are_a_multiple_of_eight_or_less_than_beta(self):
        # Steps 6-7 round the index bits down to a multiple of 8 and use the
        # signs to mop up the remainder, so header + indices + signs <= beta.
        ab_row = [64] * 8
        for t, beta in ((0, 200), (1, 300), (2, 480), (3, 384)):
            alloc = bitstream.bit_allocation(ab_row, t, beta)
            index_bits = 8 * (alloc.k0 + alloc.k1 + alloc.k2)
            self.assertEqual(index_bits % 8, 0)
            self.assertLessEqual(19 + index_bits + alloc.ks, beta)


class _BitWriter:
    """MSB-first bit writer for building synthetic frames in tests."""

    def __init__(self):
        self.bits = []

    def write(self, value, width):
        for i in range(width - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def to_bytes(self, total_bytes):
        bits = self.bits + [0] * (total_bytes * 8 - len(self.bits))
        assert len(bits) == total_bytes * 8, "frame overflows its byte budget"
        out = bytearray(total_bytes)
        for i, bit in enumerate(bits):
            if bit:
                out[i // 8] |= 1 << (7 - (i % 8))
        return bytes(out)


def _pack_pitch(w, pitch):
    lag, pgidx = pitch
    w.write(lag, 7)
    if lag != 0:
        w.write(pgidx or 0, 6)


def _pack_coefficient_block(w, ab_table, t, beta, i1):
    """Header + as many (zero-valued) index/sign bits as `beta` allocates,
    using bitstream.bit_allocation itself to size the block -- this only
    needs to place the *right number* of bits, not realistic values.
    """
    alloc = bitstream.bit_allocation(ab_table[i1], t, beta)
    w.write(0, 7)  # global gain
    w.write(0, 6)  # band gain 1
    w.write(0, 6)  # band gain 2
    for _ in range(alloc.k0 + alloc.k1 + alloc.k2):
        w.write(0, 8)
    for _ in range(alloc.ks):
        w.write(0, 1)


def _pack_mode0(ab_table, F, lsp_a, lsp_b):
    """A synthetic, well-formed 48-byte mode-0 frame (pitch A/B = lag 0,
    both shape flags 0)."""
    w = _BitWriter()
    w.write(0, 2)
    w.write(F, 1)
    if F == 1:
        for v in lsp_a:
            w.write(v, 6)
    for v in lsp_b:
        w.write(v, 6)
    _pack_pitch(w, (0, None))  # pitch A
    _pack_pitch(w, (0, None))  # pitch B
    r = 384 - len(w.bits)
    w.write(0, 1)  # S0
    w.write(0, 1)  # S1
    beta0 = (r // 2) - 1
    beta1 = (r // 2) - 1
    i1_block0 = lsp_a[0] if F == 1 else lsp_b[0]  # the slot-1 quirk, applied here too
    _pack_coefficient_block(w, ab_table, 0, beta0, i1_block0)
    _pack_coefficient_block(w, ab_table, 0, beta1, lsp_b[0])
    return w.to_bytes(48)


def _pack_mode2(ab_table, lsp_b, i1_block0):
    """A synthetic, well-formed 60-byte mode-2 frame (pitch A/B = lag 0).

    `i1_block0` is the stage-1 index the *test* expects parse_frame to
    select for the first coefficient block (after the slot-1 quirk); the
    block is sized for that index, so a real parse_frame that picks a
    different one will read a mismatched number of bits, and the
    resulting `block0.i1` will disagree with what the test asserts.
    """
    w = _BitWriter()
    w.write(2, 2)
    for v in lsp_b:
        w.write(v, 6)
    _pack_pitch(w, (0, None))  # pitch A
    _pack_pitch(w, (0, None))  # pitch B
    beta0 = (384 - len(w.bits)) // 2
    _pack_coefficient_block(w, ab_table, 0, beta0, i1_block0)
    beta1 = 480 - len(w.bits)
    _pack_coefficient_block(w, ab_table, 2, beta1, lsp_b[0])
    return w.to_bytes(60)


class Slot1IndexThreadingTests(unittest.TestCase):
    """Pin the exact stage-1 LSP index a mode-2 block 0 uses, across the
    three cases docs/lpec.md distinguishes for the "stage-1 LSP index of
    slot 1" state (the "slot-1 quirk" in "Bit allocation", and "State
    between frames"). This targets bitstream._slot1_i1 and the
    lsp1_i1_next threading in isolation, on synthetic frames, so a
    swapped or backwards substitution would be caught here even though it
    could still pass the aggregate bit-budget checks in ParseFrameTests.
    """

    AB = [[80] * 8 for _ in range(64)]  # a stub allocation-base table

    def test_after_a_preceding_mode0_f0_frame_uses_slot2s_own_i1(self):
        # docs/lpec.md: mode 0 with F=0 sets slot 1's stored index to -1;
        # the "slot-1 quirk" then substitutes slot 2's own (freshly read) i1.
        frame0 = bitstream.parse_frame(
            _pack_mode0(self.AB, F=0, lsp_a=None, lsp_b=[7, 20, 30]), 0, self.AB
        )
        self.assertEqual(frame0.lsp1_i1_next, -1)

        lsp_b2 = [12, 1, 2]
        chunk = _pack_mode2(self.AB, lsp_b2, i1_block0=12)
        frame2 = bitstream.parse_frame(chunk, frame0.lsp1_i1_next, self.AB)
        self.assertEqual(frame2.blocks[0].i1, 12)

    def test_after_a_preceding_mode0_f1_frame_uses_set_as_read_i1(self):
        # docs/lpec.md: mode 0 with F=1 reads slot 1's own LSP set (A) and
        # stores its i1; mode 2 must use that stored value, not slot 2's.
        frame0 = bitstream.parse_frame(
            _pack_mode0(self.AB, F=1, lsp_a=[9, 1, 2], lsp_b=[40, 3, 4]), 0, self.AB
        )
        self.assertEqual(frame0.lsp1_i1_next, 9)

        lsp_b2 = [15, 5, 6]
        chunk = _pack_mode2(self.AB, lsp_b2, i1_block0=9)
        frame2 = bitstream.parse_frame(chunk, frame0.lsp1_i1_next, self.AB)
        self.assertEqual(frame2.blocks[0].i1, 9)

    def test_the_initdecoder_default_of_zero_is_used_as_is(self):
        # docs/lpec.md, "State between frames": the stage-1 LSP index of
        # slot 1 starts at 0 (observed heap default) after InitDecoder --
        # 0 is not -1, so no substitution happens, even though slot 2's
        # own i1 (20, below) differs from it.
        lsp_b = [20, 2, 3]
        chunk = _pack_mode2(self.AB, lsp_b, i1_block0=0)
        frame = bitstream.parse_frame(chunk, 0, self.AB)
        self.assertEqual(frame.blocks[0].i1, 0)


class ParseFrameTests(unittest.TestCase):
    """Full frame parsing against the real, bit-exact test vectors."""

    @classmethod
    def setUpClass(cls):
        if _TABLES is None:
            release_gate.skip_or_fail(
                "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run "
                "tools/import_lpec_tables.py to generate it locally"
            )
        cls.ab = _TABLES.AB

    def _frames_of(self, name):
        with open(VECTORS_DIR / f"{name}.dvf", "rb") as f:
            data = f.read()
        return bitstream.split_frames(make_test_dvf.payload(data))

    @staticmethod
    def _meta(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_field_widths_never_exceed_the_frames_bit_budget(self):
        # "Field widths for each mode add up to the documented bit budget":
        # every field bitstream.parse_frame reads (fixed-width fields plus
        # the allocation-driven coefficient block) must fit inside the
        # frame's mode budget, with any leftover being end-of-frame padding.
        checked_modes = set()
        prev_i1 = 0
        for path in _vector_metas():
            meta = self._meta(path)
            if meta["name"] == "ends-mid-frame":
                continue  # its last frame is deliberately short; see SplitFramesTests
            for chunk in self._frames_of(meta["name"]):
                frame = bitstream.parse_frame(chunk, prev_i1, self.ab)
                prev_i1 = frame.lsp1_i1_next
                checked_modes.add(frame.mode)
                total_bits = 2 + sum(b.bits_used for b in frame.blocks)
                total_bits += _fixed_field_bits(frame)
                self.assertLessEqual(total_bits, bitstream.MODE_BITS[frame.mode], meta["name"])
                self.assertLessEqual(total_bits, len(chunk) * 8, meta["name"])
        self.assertEqual(checked_modes, {0, 1, 2, 3}, "not every mode was exercised by the vectors")

    def test_parse_frame_never_wraps_a_complete_frame(self):
        # For every non-truncated frame in every vector, the bit reader must
        # never need to wrap (BitReader.pos must stay within the bytes handed
        # to this frame) -- wraparound is only for the deliberately short
        # last frame of a stream.
        prev_i1 = 0
        for path in _vector_metas():
            meta = self._meta(path)
            for chunk in self._frames_of(meta["name"]):
                if len(chunk) < bitstream.MODE_BYTES[bitstream.frame_mode(chunk[0])]:
                    continue  # the deliberately truncated last frame: wraparound is expected
                frame = bitstream.parse_frame(chunk, prev_i1, self.ab)
                prev_i1 = frame.lsp1_i1_next
                bits_read = 2 + sum(b.bits_used for b in frame.blocks) + _fixed_field_bits(frame)
                self.assertLessEqual(bits_read, len(chunk) * 8, meta["name"])


def _fixed_field_bits(frame):
    """Bits used by everything in `frame` outside the coefficient blocks."""
    bits = 0
    if frame.mode == 0:
        bits += 1  # F
        bits += 18 if frame.lsp_a is not None else 0
        bits += 18  # lsp_b
        bits += _pitch_bits(frame.pitch_a) + _pitch_bits(frame.pitch_b)
        bits += 2  # the two shape flags
        bits += sum(7 for i in frame.shape_indices if i is not None)
    elif frame.mode == 1:
        bits += 18 + _pitch_bits(frame.pitch_b)
    elif frame.mode == 2:
        bits += 18 + _pitch_bits(frame.pitch_a) + _pitch_bits(frame.pitch_b)
    elif frame.mode == 3:
        bits += 18 + _pitch_bits(frame.pitch_b)
    return bits


def _pitch_bits(pitch):
    lag, pgidx = pitch
    return 7 + (6 if pgidx is not None else 0)


if __name__ == "__main__":
    unittest.main()
