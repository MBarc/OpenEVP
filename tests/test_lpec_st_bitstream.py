"""Unit tests for the LPEC ST framing and bitstream reader
(openevp.decoders.sony_lpec_st).

The reader, header descrambling and frame-level rejections are checked on
hand-built frames; the full parser is covered by the vector gate
(test_lpec_st_vectors, test_lpec_st_core).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from openevp.decoders.sony_lpec_st import TablesMissing, bitstream, decoder, tables  # noqa: E402


def _tables_present():
    try:
        tables.load()
        return True
    except TablesMissing:
        return False


def _bits_to_bytes(bits, n=280):
    b = bytearray(n)
    for i, v in enumerate(bits):
        if v:
            b[i >> 3] |= 0x80 >> (i & 7)
    return bytes(b)


class ReaderTests(unittest.TestCase):
    def test_msb_first_reads(self):
        br = bitstream.BitReader(bytes([0b10110010, 0b01111111, 0, 0, 0]), 0)
        self.assertEqual(br.read(1), 1)
        self.assertEqual(br.read(3), 0b011)
        self.assertEqual(br.read(8), 0b00100111)
        self.assertEqual(br.pos, 12)

    def test_zero_bit_read(self):
        br = bitstream.BitReader(bytes([0xFF] * 4), 0)
        self.assertEqual(br.read(0), 0)
        self.assertEqual(br.pos, 0)

    def test_base_offset(self):
        br = bitstream.BitReader(bytes([0, 0, 0, 0xA5, 0, 0]), 3)
        self.assertEqual(br.read(8), 0xA5)

    def test_reads_past_0x10000_give_zero(self):
        br = bitstream.BitReader(bytes([0xFF] * 9000), 0)
        br.pos = 0x10000
        self.assertEqual(br.read(8), 0)
        self.assertEqual(br.pos, 0x10008)


class SignExtendTests(unittest.TestCase):
    def test_sext(self):
        self.assertEqual(bitstream._sext(0b1000, 4), -8)
        self.assertEqual(bitstream._sext(0b0111, 4), 7)
        self.assertEqual(bitstream._sext(0xFF, 8), -1)


@release_gate.require(_tables_present(), "openevp/decoders/sony_lpec_st/data/lpec_st_tables.json not found; "
                                         "run tools/import_lpec_st_tables.py")
class FramingTests(unittest.TestCase):
    def setUp(self):
        self.t = tables.load()

    def test_descramble_only_for_flags_01(self):
        fr = bytes([0, 5, 0x00]) + bytes(280)
        self.assertEqual(decoder.descramble(fr, self.t.XOR_KEYS), fr)
        for flags in (0x80, 0xC0, 0xBF):
            fr = bytes([0, 5, flags]) + bytes(280)
            self.assertEqual(decoder.descramble(fr, self.t.XOR_KEYS), fr)

    def test_descramble_key_and_offset(self):
        keys = self.t.XOR_KEYS
        flags = 0x40 | (2 << 4) | 14           # key 2, starting at byte 14
        out = decoder.descramble(bytes([0, 5, flags]) + bytes(280), keys)
        want = bytes(keys[32 + (14 + i) % 16] for i in range(8))
        self.assertEqual(out[3:11], want)
        self.assertEqual(out[11:], bytes(272))

    def _parse(self, bits):
        return bitstream.parse_frame(_bits_to_bytes(bits) + bytes(8300), 0, self.t)

    def test_start_bit_must_be_zero(self):
        with self.assertRaises(bitstream.ParseError) as cm:
            self._parse([1])
        self.assertEqual(cm.exception.code, 0x208)

    def test_mono_unit_rejected(self):
        with self.assertRaises(bitstream.ParseError) as cm:
            self._parse([0, 0, 0])
        self.assertEqual(cm.exception.code, 0x20A)

    def test_no_unit_rejected(self):
        with self.assertRaises(bitstream.ParseError) as cm:
            self._parse([0, 1, 1])
        self.assertEqual(cm.exception.code, 0x20D)

    def test_extension_unit_is_skipped(self):
        # extension: type 2, 5 bits, 11-bit length 1, one byte; then end
        bits = [0, 1, 0] + [0] * 5 + [0] * 10 + [1] + [0] * 8 + [1, 1]
        with self.assertRaises(bitstream.ParseError) as cm:
            self._parse(bits)
        self.assertEqual(cm.exception.code, 0x20D)   # skipped, still no channel unit

    def test_empty_payload(self):
        rate, ch, pcm = decoder.decode(b"", self.t)
        self.assertEqual((rate, ch, pcm), (44100, 2, b""))

    def _vector(self):
        path = os.path.join(os.path.dirname(__file__), "vectors", "lpec_st", "all-features-40.bin")
        with open(path, "rb") as f:
            return f.read()

    def test_first_frame_after_a_reset_gives_no_samples(self):
        """Sony's framing: a fresh decoder swallows its first accepted frame,
        and a counter-0 frame (skipped) resets it."""
        data = self._vector()
        frames = [data[i:i + 283] for i in range(0, len(data), 283)]
        first = [f for f in frames if f[0:2] != b"\x00\x00"][:3]
        self.assertEqual(len(decoder.decode(b"".join(first), self.t)[2]), 2 * 8192)
        reset = bytes(283)                                   # counter 0: a segment header
        got = decoder.decode(first[0] + first[1] + reset + first[2], self.t)[2]
        self.assertEqual(len(got), 8192)                     # frames 1 and 3 swallowed, 2 kept
        self.assertEqual(len(decoder.decode(reset + b"".join(first), self.t)[2]), 2 * 8192)

    def test_a_trailing_partial_frame_is_ignored(self):
        data = self._vector()[:3 * 283]
        self.assertEqual(decoder.decode(data + data[:100], self.t), decoder.decode(data, self.t))

    def test_should_stop_cancels(self):
        with self.assertRaises(decoder.Cancelled):
            decoder.decode(self._vector(), self.t, should_stop=lambda: True)


if __name__ == "__main__":
    unittest.main()
