import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, make_raw  # noqa: E402
from sony_icd import dvf  # noqa: E402
from openevp.export import publish, save_wav  # noqa: E402
from sony_icd.export import save_dvf  # noqa: E402


class ExportTests(unittest.TestCase):
    def test_save_dvf_skips_same_audio_and_numbers_different(self):
        a = dvf.build(make_raw(2958, 100), DATE, "X")
        b = dvf.build(make_raw(4000, 900), DATE, "X")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(save_dvf(a, d, "r.dvf"), (os.path.join(d, "r.dvf"), False))
            self.assertEqual(save_dvf(a, d, "r.dvf"), (os.path.join(d, "r.dvf"), True))
            self.assertEqual(save_dvf(b, d, "r.dvf"), (os.path.join(d, "r (2).dvf"), False))

    def test_save_wav_compares_bytes_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            publish(b"RIFF-other", os.path.join(d, "r.wav"))
            self.assertEqual(save_wav(b"RIFF-mine", d, "r.wav"), (os.path.join(d, "r (2).wav"), False))
            self.assertEqual(save_wav(b"RIFF-mine", d, "r.wav"), (os.path.join(d, "r (2).wav"), True))
            with open(os.path.join(d, "r.wav"), "rb") as f:
                self.assertEqual(f.read(), b"RIFF-other")


    def test_a_wav_in_pieces_is_written_and_compared_without_joining(self):
        wav = b"RIFF" + bytes(range(256)) * 9000                  # > 2 MB: compared in several blocks
        parts = [memoryview(wav)[:4], memoryview(wav)[4:1 << 20], bytearray(wav[1 << 20:])]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(save_wav(parts, d, "r.wav"), (os.path.join(d, "r.wav"), False))
            with open(os.path.join(d, "r.wav"), "rb") as f:
                self.assertEqual(f.read(), wav)
            self.assertEqual(save_wav(wav, d, "r.wav"), (os.path.join(d, "r.wav"), True))
            self.assertEqual(save_wav(parts, d, "r.wav"), (os.path.join(d, "r.wav"), True))
            other = bytearray(wav)
            other[-1] ^= 1                                          # same size, last byte differs
            self.assertEqual(save_wav(bytes(other), d, "r.wav"), (os.path.join(d, "r (2).wav"), False))
            self.assertEqual(save_wav(wav[:-1], d, "r.wav"), (os.path.join(d, "r (3).wav"), False))

    def test_publish_writes_pieces_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            publish([b"RI", memoryview(b"FF-"), bytearray(b"x")], os.path.join(d, "a.wav"))
            with open(os.path.join(d, "a.wav"), "rb") as f:
                self.assertEqual(f.read(), b"RIFF-x")

if __name__ == "__main__":
    unittest.main()
