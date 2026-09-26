import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from st25 import dvf  # noqa: E402
import make_test_dvf as m  # noqa: E402


def frames(sizes):
    return [bytes((i * 7 + j) & 0xFF for j in range(n)) for i, n in enumerate(sizes)]


class PackTests(unittest.TestCase):
    def check(self, sizes):
        fr = frames(sizes)
        data = m.pack(fr)
        self.assertIsNone(dvf.validate(data))
        self.assertEqual(m.payload(data), b"".join(fr))
        starts, pos = set(), 0
        for n in sizes:
            starts.add(pos)
            pos += n
        audio = data[1024:]
        for k in range(len(audio) // 1024):
            off = struct.unpack(">H", audio[k * 1024:k * 1024 + 2])[0]
            self.assertIn(k * m.PAYLOAD + off - dvf.BLOCK_HEADER, starts, f"block {k}")
        return data

    def test_all_48_matches_the_recorder_pattern(self):
        data = self.check([48] * 100)
        offs = [struct.unpack(">H", data[1024 + k * 1024:1026 + k * 1024])[0] for k in range(4)]
        self.assertEqual(offs, [10, 52, 46, 40])             # as in real recordings

    def test_mixed_sizes_and_block_boundaries(self):
        self.check([48, 60, 36, 48] * 60)
        self.check([60] * 169)                               # 10140 bytes = exactly 10 blocks
        self.check([36])                                     # a single frame

    def test_a_60_byte_frame_shifts_the_next_offset_like_real_data(self):
        # 20 x 48 + 60 = 1020 bytes: the next block starts 6 bytes into a frame (offset 16),
        # the pattern seen in block 1 of A-009.
        data = self.check([48] * 20 + [60] + [48] * 30)
        self.assertEqual(struct.unpack(">H", data[2048:2050])[0], 16)



class VectorTests(unittest.TestCase):
    """The committed decoder vectors are well-formed and self-consistent."""

    DIR = os.path.join(os.path.dirname(__file__), "vectors")

    def test_vectors(self):
        import glob
        import hashlib
        import json
        metas = sorted(glob.glob(os.path.join(self.DIR, "*.json")))
        self.assertGreaterEqual(len(metas), 10)
        for path in metas:
            meta = json.loads(Path(path).read_text(encoding="utf-8"))
            data = Path(self.DIR, meta["name"] + ".dvf").read_bytes()
            self.assertIsNone(dvf.validate(data), meta["name"])
            if meta["pcm_file"]:
                pcm = Path(self.DIR, meta["pcm_file"]).read_bytes()
                self.assertEqual(hashlib.sha256(pcm).hexdigest(), meta["pcm_sha256"], meta["name"])
                self.assertEqual(len(pcm), 2 * meta["pcm_samples"], meta["name"])


if __name__ == "__main__":
    unittest.main()
