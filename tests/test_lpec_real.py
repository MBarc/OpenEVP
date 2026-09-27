"""Real-recording gate for the LPEC decoder (ruling R6, docs/lpec.md "Status").

Compares every ICD-ST25 recording in <DIR>/Raw/A/*.dvf with Digital Voice
Editor's own WAV of it in <DIR>/Converted/ (matching base name) byte for
byte, where <DIR> is the folder the OPENEVP_REAL_RECORDINGS environment
variable names.

Runs only when OPENEVP_REAL_RECORDINGS names an existing folder with that
layout; otherwise skipped with a clear message. dvf_to_wav uses the C core when
openevp/decoders/sony_lpec/lpec_core.dll is built (the 20 recordings, 18,622 frames, take
seconds); pure Python decodes at roughly 65 ms/frame (about 20 minutes).

This test only reads the real recordings and DVE's WAVs into memory for
comparison. It never copies, writes or commits a real recording or its PCM.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openevp.decoders.sony_lpec import TablesMissing, tables  # noqa: E402

_ROOT = os.environ.get("OPENEVP_REAL_RECORDINGS", "")
RAW_DIR = Path(_ROOT) / "Raw" / "A"
CONVERTED_DIR = Path(_ROOT) / "Converted"
EXPECTED_COUNT = 20

_ENABLED = bool(_ROOT) and RAW_DIR.is_dir()
_DVF_FILES = sorted(RAW_DIR.glob("*.dvf")) if _ENABLED else []


def _tables_present():
    try:
        tables.load()
        return True
    except TablesMissing:
        return False


_SKIP_REASON = (
    "set OPENEVP_REAL_RECORDINGS to a folder holding Raw/A/*.dvf and "
    "Converted/*.wav to run the 20 real-recording gate (seconds with "
    "lpec_core.dll, about 20 minutes in pure Python)"
)


@unittest.skipUnless(_ENABLED, _SKIP_REASON)
class RealRecordingTests(unittest.TestCase):
    """Every real .dvf decodes to a WAV byte-identical to DVE's own conversion."""

    @classmethod
    def setUpClass(cls):
        if not _tables_present():
            raise unittest.SkipTest(
                "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")

    def test_finds_all_20_recordings(self):
        self.assertEqual(
            len(_DVF_FILES), EXPECTED_COUNT,
            f"expected {EXPECTED_COUNT} recordings in {RAW_DIR}, found {len(_DVF_FILES)}")


def _make_test(dvf_path, wav_path):
    def test(self):
        from openevp.decoders.sony_lpec.decoder import dvf_to_wav

        if not wav_path.is_file():
            self.fail(f"no matching {wav_path.name} in {CONVERTED_DIR}")
        got = dvf_to_wav(dvf_path.read_bytes())
        want = wav_path.read_bytes()
        if got != want:
            self.fail(f"{dvf_path.name}: differs from {wav_path.name} "
                      f"({len(got)} vs {len(want)} bytes)")
    test.__name__ = "test_" + dvf_path.stem.replace(" ", "_").replace(".", "_")
    test.__doc__ = f"{dvf_path.name} decodes byte-identical to {wav_path.name}"
    return test


for _path in _DVF_FILES:
    _wav = CONVERTED_DIR / (_path.stem + ".wav")
    _t = _make_test(_path, _wav)
    setattr(RealRecordingTests, _t.__name__, _t)


if __name__ == "__main__":
    unittest.main()
