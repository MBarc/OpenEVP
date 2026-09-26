import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, make_raw  # noqa: E402
from st25 import dvf  # noqa: E402
from st25.export import publish, save_dvf, save_wav  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
