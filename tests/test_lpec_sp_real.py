"""Real-recording gate for the LPEC decoder on ICD-ST10 recordings (SP, and
LP made on an ICD-ST10).

<DIR> is the folder OPENEVP_ST10_SP_RECORDINGS names. For each raw
ICD-ST10 download <DIR>/<X>_*.raw (1056-byte wire blocks) with Sony's own
decode of it next to it, <DIR>/<X>_sony_lpec_sp_16k_mono.wav (LPEC SP) or
<DIR>/<X>_sony_lpec_lp_8k.wav (LPEC LP), both made by Sony's LPEC.dll
(InitCodec(rate, bitrate, 0), DVE's loop):

- the raw data saved as a .dvf (sony_icd.dvf.build, as a download does) and
  converted through the app's format (openevp.formats: .dvf, codec 0x2A or
  0x2C) gives Sony's WAV file exactly, header included, on the C core and in
  pure Python;
- its length is sony_icd.dvf.sp_seconds() for SP.

Skipped unless OPENEVP_ST10_SP_RECORDINGS names such a folder. Nothing from
the recordings is copied, written or committed.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from openevp import formats  # noqa: E402
from openevp.decoders.sony_lpec import _core, decoder  # noqa: E402
from sony_icd import dvf  # noqa: E402
from test_lpec_sp_vectors import HAVE_TABLES, NO_TABLES  # noqa: E402

_ROOT = os.environ.get("OPENEVP_ST10_SP_RECORDINGS", "")
_ENABLED = bool(_ROOT) and Path(_ROOT).is_dir()
_REFS = {"_sony_lpec_sp_16k_mono.wav": dvf.MODE_SP, "_sony_lpec_lp_8k.wav": dvf.MODE_LP}


def _pairs():
    """[(raw, Sony's WAV, mode)] in the folder."""
    found = []
    if not _ENABLED:
        return found
    for raw in sorted(Path(_ROOT).glob("*.raw")):
        prefix = raw.stem.split("_")[0]
        for suffix, mode in _REFS.items():
            ref = raw.with_name(prefix + suffix)
            if ref.is_file():
                found.append((raw, ref, mode))
    return found


_PAIRS = _pairs()


@unittest.skipUnless(_ENABLED, "set OPENEVP_ST10_SP_RECORDINGS to the folder holding <X>_*.raw and "
                               "<X>_sony_lpec_sp_16k_mono.wav (or <X>_sony_lpec_lp_8k.wav)")
class RealRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE_TABLES:
            release_gate.skip_or_fail(NO_TABLES)

    def test_there_is_an_sp_recording(self):
        self.assertTrue(any(mode == dvf.MODE_SP for _, _, mode in _PAIRS), f"no SP recording in {_ROOT}")

    def check(self, raw, ref_path, mode, pure_python):
        ref = ref_path.read_bytes()
        file = dvf.build(raw.read_bytes(), b"\xff" * 8, "", mode=mode)
        self.assertIsNone(dvf.validate(file))
        if pure_python:
            wav = decoder._decode(dvf.payload(file), decoder.load_tables(decoder.config_for(file)), False,
                                  None, 0)
            self.assertTrue(bytes(wav) == ref[44:], f"{raw.name}: the PCM differs from {ref_path.name}")
            return
        wav = formats.DVF.decoder.to_wav(file)
        self.assertTrue(bytes(wav) == ref, f"{raw.name}: the WAV differs from {ref_path.name}")
        if mode == dvf.MODE_SP:
            self.assertEqual((len(wav) - 44) / 2 / 16000, dvf.sp_seconds(dvf.payload(file)))

    def test_c_core_and_formats(self):
        if not _core.available():
            release_gate.skip_or_fail("lpec_core.dll is not built")
        for raw, ref, mode in _PAIRS:
            with self.subTest(raw.name):
                self.check(raw, ref, mode, pure_python=False)

    def test_pure_python(self):
        for raw, ref, mode in _PAIRS:
            with self.subTest(raw.name):
                self.check(raw, ref, mode, pure_python=True)


if __name__ == "__main__":
    unittest.main()
