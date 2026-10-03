"""Real-recording gate for the LPEC ST decoder (ICD-ST10).

For each recording <DIR>/A_00n.raw (a raw ICD-ST10 download: 1056-byte wire
blocks) compares OpenEVP's WAV with Sony's own decode of it,
<DIR>/A_00n_sony_lcstde_44k_stereo.wav (lcstde.ax), byte for byte, where
<DIR> is the folder OPENEVP_ST10_RECORDINGS names:

- the raw data saved as a .dvf (sony_icd.dvf.build, as a download does) and
  converted through the app's format (openevp.formats: .dvf, codec 0x24)
  gives Sony's WAV file exactly, header included, on the C core and in pure
  Python;
- the marks fingerprint of that WAV is the fingerprint of Sony's WAV, and
  the streamed paths (write to a file, fingerprint without a WAV) agree.

Skipped unless OPENEVP_ST10_RECORDINGS names a folder with that layout.
Nothing from the recordings is copied, written or committed.
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from openevp import formats, wavinfo  # noqa: E402
from openevp.decoders.sony_lpec_st import _core, decoder  # noqa: E402
from sony_icd import dvf  # noqa: E402
from test_lpec_st_vectors import HAVE_TABLES, NO_TABLES  # noqa: E402

_ROOT = os.environ.get("OPENEVP_ST10_RECORDINGS", "")
_ENABLED = bool(_ROOT) and Path(_ROOT).is_dir()
_RAWS = sorted(Path(_ROOT).glob("A_*.raw")) if _ENABLED else []


@unittest.skipUnless(_ENABLED, "set OPENEVP_ST10_RECORDINGS to the folder holding A_00n.raw "
                               "and A_00n_sony_lcstde_44k_stereo.wav")
class RealRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE_TABLES:
            release_gate.skip_or_fail(NO_TABLES)

    def test_the_recordings_are_there(self):
        self.assertTrue(_RAWS, f"no A_*.raw in {_ROOT}")

    def check(self, raw, pure_python):
        ref_path = raw.with_name(raw.stem + "_sony_lcstde_44k_stereo.wav")
        if not ref_path.is_file():
            self.fail(f"no {ref_path.name} next to {raw.name}")
        ref = ref_path.read_bytes()
        file = dvf.build(raw.read_bytes(), b"\xff" * 8, "", mode=dvf.MODE_ST)
        self.assertIsNone(dvf.validate(file))
        self.assertEqual(dvf.codec(file), dvf.CODEC_ST)
        if pure_python:
            wav = decoder.dvf_to_wav(file, use_core=False)
        else:
            wav = formats.DVF.decoder.to_wav(file)
        self.assertTrue(wav == ref, f"{raw.name}: the WAV differs from {ref_path.name}")
        return file, ref

    def test_c_core_and_formats(self):
        if not _core.available():
            release_gate.skip_or_fail("lpec_st_core.dll is not built")
        for raw in _RAWS:
            with self.subTest(raw.name):
                file, ref = self.check(raw, pure_python=False)
                fp = wavinfo.wav_fingerprint(io.BytesIO(ref))
                self.assertEqual(formats.fingerprint(formats.DVF, file), fp)
                with tempfile.TemporaryFile() as f:
                    formats.write_wav(formats.DVF, file, f)
                    f.seek(0)
                    self.assertTrue(f.read() == ref)

    def test_pure_python_on_the_shortest(self):
        raw = min(_RAWS, key=lambda p: p.stat().st_size)
        self.check(raw, pure_python=True)


if __name__ == "__main__":
    unittest.main()
