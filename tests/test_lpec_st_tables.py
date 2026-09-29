"""The LPEC ST table data (openevp/decoders/sony_lpec_st/data/lpec_st_tables.json)
and tools/import_lpec_st_tables.py, which writes it from Sony's lcstde.ax.

- A missing or damaged file is TablesMissing / TablesInvalid with a plain
  message (what reaches users names no path and no developer tooling).
- The shipped file has every table at the size the decoder and its C core
  index, and the tables generated at load time come out as expected.
- The import tool refuses any DLL but the pinned build; with the research
  copy of lcstde.ax (OPENEVP_TABLE_DUMPS/lcstde.ax, or OPENEVP_LCSTDE) it
  reproduces the shipped file exactly (skipped without it, as the LP
  decoder's import test is).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))
import import_lpec_st_tables  # noqa: E402
import release_gate  # noqa: E402
from openevp.decoders.sony_lpec_st import TablesMissing, tables  # noqa: E402
from test_lpec_st_vectors import HAVE_TABLES, NO_TABLES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _research_dll():
    """The research copy of lcstde.ax, or None."""
    for p in (os.environ.get("OPENEVP_LCSTDE"),
              os.environ.get("OPENEVP_TABLE_DUMPS") and os.path.join(os.environ["OPENEVP_TABLE_DUMPS"],
                                                                     "lcstde.ax")):
        if p and os.path.isfile(p):
            return Path(p)
    return None


class MissingTests(unittest.TestCase):
    def test_missing_file_raises_a_plain_message(self):
        missing = Path(tempfile.gettempdir()) / "no-such-lpec-st-tables.json"
        with self.assertRaises(TablesMissing) as cm:
            tables.load(missing)
        msg = str(cm.exception)
        self.assertEqual(msg, "this build does not include the LPEC ST table data")
        for bad in ("import_lpec_st_tables", "lcstde", ":\\", "/"):
            self.assertNotIn(bad, msg)
        self.assertEqual(cm.exception.path, missing)
        self.assertIn("tools/import_lpec_st_tables.py", cm.exception.hint)

    def test_damaged_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(tables.TablesInvalid) as cm:
                tables.load(p)
            self.assertEqual(str(cm.exception), "the LPEC ST table data in this build is damaged")
            p.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(tables.TablesInvalid):
                tables.load(p)


@release_gate.require(HAVE_TABLES, NO_TABLES)
class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.t = tables.load()

    def raw(self):
        return json.loads(tables.DEFAULT_DATA_FILE.read_text(encoding="utf-8"))

    def test_sizes(self):
        t = self.t
        self.assertEqual(len(t.QU_START), 33)
        self.assertEqual(t.QU_START[-1], 2048)
        self.assertEqual(sum(t.QU_LEN), 2048)
        self.assertEqual(len(t.H_SPEC), 113)
        self.assertIsNone(t.H_SPEC[0])
        self.assertEqual(len(t.F_WINDOWS), 4)
        self.assertTrue(all(len(w) == 256 for w in t.F_WINDOWS))
        self.assertEqual(len(t.F_QMF_WIN), 192)
        self.assertEqual(len(t.QMF_IDX), 7)
        self.assertEqual(len(t.XOR_KEYS), 64)

    def test_huffman_lookup_tables(self):
        for name in ("H_WL", "H_SF_DELTA", "H_SF", "H_CT", "H_TONE", "H_GAIN"):
            for h in getattr(self.t, name):
                self.assertEqual(len(h.lut), 1 << h.maxbits)
                for sym in h.lut:
                    self.assertTrue(0 < h.lens[sym] <= h.maxbits)

    def test_generated_tone_tables(self):
        t = self.t
        self.assertEqual(len(t.TONE_SINE), 2048)
        self.assertEqual(len(t.TONE_WINDOW), 256)
        self.assertEqual(t.TONE_SINE[0], 0.0)
        self.assertEqual(t.TONE_SINE[512], 1.0)
        self.assertEqual(t.TONE_WINDOW[0], 0.0)
        self.assertEqual(t.TONE_WINDOW[128], 1.0)
        # Not exactly odd-symmetric: the DLL steps by a float approximation
        # of 2*pi/2048 (the vectors pin both tables bit for bit).
        self.assertAlmostEqual(t.TONE_SINE[1024 + 100], -t.TONE_SINE[100], places=6)
        self.assertTrue(all(a <= b for a, b in zip(t.TONE_WINDOW[:128], t.TONE_WINDOW[1:129])))

    def test_source_is_pinned(self):
        self.assertEqual(self.raw()["_source"], {"file": "lcstde.ax", "sha256": import_lpec_st_tables.EXPECTED_SHA256})

    def test_a_wrong_shape_or_type_is_rejected(self):
        raw = self.raw()
        for name, bad in (("F_WINDOWS", [[0] * 255] * 4), ("QU_START", [0.5] * 33), ("NOISE", [0] * 1023),
                          ("H_WL", [{"maxbits": 2, "lut": [0, 0, 0], "lens": [1]}] * 4)):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                p = Path(d) / "t.json"
                p.write_text(json.dumps({**raw, name: bad}), encoding="utf-8")
                with self.assertRaises(tables.TablesInvalid) as cm:
                    tables.load(p)
                self.assertIn(name, cm.exception.hint)

    def test_load_is_cached_per_file_version(self):
        self.assertIs(tables.load(), self.t)


class ImportToolTests(unittest.TestCase):
    def test_another_build_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unexpected build of lcstde.ax"):
            import_lpec_st_tables.build(b"MZ not the pinned DLL")

    def test_missing_dll_and_no_folder(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stderr"):
            self.assertEqual(import_lpec_st_tables.main(["--tables-dir", d, "--out", os.path.join(d, "o.json")]), 1)
            self.assertFalse(os.path.exists(os.path.join(d, "o.json")))
        with mock.patch.object(import_lpec_st_tables, "DEFAULT_TABLES_DIR", None), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                import_lpec_st_tables.main([])

    def test_the_dll_is_looked_for_in_the_dumps_folder(self):
        dll = _research_dll()
        if dll is None:
            self.skipTest("set OPENEVP_TABLE_DUMPS (a folder holding lcstde.ax) or OPENEVP_LCSTDE "
                          "to check the import against Sony's DLL")
        if not HAVE_TABLES:
            release_gate.skip_or_fail(NO_TABLES)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "lcstde.ax").write_bytes(dll.read_bytes())
            out = Path(d) / "out.json"
            with mock.patch("sys.stdout"):
                self.assertEqual(import_lpec_st_tables.main(["--tables-dir", d, "--out", str(out)]), 0)
            self.assertEqual(out.read_bytes(), tables.DEFAULT_DATA_FILE.read_bytes())


if __name__ == "__main__":
    unittest.main()
