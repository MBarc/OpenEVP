"""Tests for openevp.decoders.sony_lpec.tables: sizes/types of the derivable tables (a),
bit-exact match against the table dumps where available (b, set
OPENEVP_TABLE_DUMPS to their folder), and
TablesMissing without a data file (c). See docs/lpec.md, "Tables".
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openevp.decoders.sony_lpec import TablesMissing, tables  # noqa: E402
from openevp.decoders.sony_lpec.tables import TablesInvalid  # noqa: E402


def _zeros(name, shape):
    zero = 0 if name == "AB" else 0.0
    if len(shape) == 1:
        return [zero] * shape[0]
    return [[zero] * shape[1] for _ in range(shape[0])]


# A synthetic data file: every extracted table with the shape load() requires,
# all zeros (no real table data).
_MINIMAL_EXTRACTED = {name: _zeros(name, shape) for name, shape in tables._EXTRACTED_SHAPES.items()}

REPO_ROOT = Path(__file__).resolve().parent.parent
DVE_TABLES_DIR = Path(os.environ.get("OPENEVP_TABLE_DUMPS") or REPO_ROOT / "no-table-dumps")

INT16_MIN, INT16_MAX = -32768, 32767


def _flatten(rows):
    if isinstance(rows, (int, float)):
        return [rows]
    if rows and isinstance(rows[0], (list, tuple)):
        return [x for row in rows for x in row]
    return list(rows)


def _load_tables_once():
    try:
        return tables.load()
    except TablesMissing:
        return None


_TABLES = _load_tables_once()


class DerivableSizeAndTypeTests(unittest.TestCase):
    """(a) Every derivable table has the documented size and type."""

    @classmethod
    def setUpClass(cls):
        if _TABLES is None:
            raise unittest.SkipTest(
                "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run "
                "tools/import_lpec_tables.py to generate it locally"
            )
        cls.t = _TABLES

    def _assert_int16_matrix(self, rows, shape):
        self.assertEqual(len(rows), shape[0])
        for row in rows:
            self.assertEqual(len(row), shape[1])
            for x in row:
                self.assertIsInstance(x, int)
                self.assertTrue(INT16_MIN <= x <= INT16_MAX)

    def test_q16_q17_q18_lsp_codebooks(self):
        self._assert_int16_matrix(self.t.D1, (64, 10))
        self._assert_int16_matrix(self.t.D2, (64, 10))
        self._assert_int16_matrix(self.t.D3, (64, 10))

    def test_q15_pitch_taps(self):
        self._assert_int16_matrix(self.t.PQ, (64, 3))

    def test_allocation_reference(self):
        self.assertIsInstance(self.t.R, int)
        self.assertEqual(self.t.R, 128)

    def test_int16_sine_tables(self):
        for table, period in ((self.t.S2048, 2048), (self.t.S1536, 1536)):
            self.assertEqual(len(table), period + 1)
            for x in table:
                self.assertIsInstance(x, int)
                self.assertTrue(-32767 <= x <= 32767)

    def test_trivial_static_tables(self):
        self.assertEqual(self.t.DEFAULT_SHAPE, (1.0,) * 8)
        self.assertEqual(self.t.ZERO_TAPS_F, (0.0, 0.0, 0.0))
        self.assertEqual(self.t.ZERO_TAPS_Q15, (0, 0, 0))

    def test_every_table_is_immutable(self):
        """The cached Tables object is shared across threads: every table is
        a tuple (of tuples), never a list something could mutate."""
        import dataclasses
        for field in dataclasses.fields(self.t):
            value = getattr(self.t, field.name)
            if isinstance(value, int):
                continue
            with self.subTest(table=field.name):
                self.assertIsInstance(value, tuple)
                for row in value:
                    self.assertNotIsInstance(row, list)

    def test_runtime_window_and_fft_sine_tables(self):
        for table, n in (
            (self.t.WIN512, 512),
            (self.t.WIN512_SQ, 512),
            (self.t.WIN1024, 1024),
            (self.t.FFT_SIN_2048, 2048),
            (self.t.FFT_SIN_1536, 1536),
        ):
            self.assertEqual(len(table), n)
            for x in table:
                self.assertIsInstance(x, float)

    def test_runtime_post_twiddle_tables(self):
        for table, n in ((self.t.POST256, 256), (self.t.POST384, 384), (self.t.POST512, 512)):
            self.assertEqual(len(table), n)

    def test_runtime_initial_lsps(self):
        self.assertEqual(len(self.t.LSP_INIT), 11)
        self.assertEqual(self.t.LSP_INIT[0], 0.0)


class BitExactAgainstDveReTests(unittest.TestCase):
    """(b) Generated tables equal the corresponding extracted JSON exactly."""

    # Tables.<attr> -> <dumps>/<file>; only the *generated* (derivable)
    # tables are checked here, and only where there is a corresponding
    # dump. Trivial tables (all-ones, all-zero) have no dump.
    _COMPARISONS = {
        "D1": "lsp_cb1_i16.json",
        "D2": "lsp_cb2_i16.json",
        "D3": "lsp_cb3_i16.json",
        "PQ": "pitch_taps_i16.json",
        "R": "alloc_ref_i16.json",
        "S2048": "sin_q15_2048.json",
        "S1536": "sin_q15_1536.json",
        "WIN512": "rt_win_sine_512.json",
        "WIN512_SQ": "rt_win_sine2_512.json",
        "WIN1024": "rt_win_sine_1024.json",
        "FFT_SIN_2048": "rt_fft_sin_2048.json",
        "FFT_SIN_1536": "rt_fft_sin_1536.json",
        "POST256": "rt_mdct_post_256.json",
        "POST384": "rt_mdct_post_384.json",
        "POST512": "rt_mdct_post_512.json",
        "LSP_INIT": "rt_lsp_init_f64.json",
    }

    @classmethod
    def setUpClass(cls):
        if _TABLES is None:
            raise unittest.SkipTest(
                "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run "
                "tools/import_lpec_tables.py to generate it locally"
            )
        if not DVE_TABLES_DIR.is_dir():
            raise unittest.SkipTest(f"{DVE_TABLES_DIR} not found; skipping bit-exact comparison")
        cls.t = _TABLES

    def test_generated_tables_match_dve_re_exactly(self):
        for attr, filename in self._COMPARISONS.items():
            with self.subTest(table=attr):
                with open(DVE_TABLES_DIR / filename, encoding="utf-8") as f:
                    expected = json.load(f)["values"]
                got = _flatten(getattr(self.t, attr))
                self.assertEqual(len(got), len(expected))
                for i, (g, e) in enumerate(zip(got, expected)):
                    self.assertEqual(g, e, f"{attr}[{i}]: {g!r} != {e!r}")


class TablesMissingTests(unittest.TestCase):
    """(c) Loading without the data file raises TablesMissing."""

    def test_missing_data_file_raises(self):
        missing_path = REPO_ROOT / "st25" / "lpec" / "data" / "does-not-exist.json"
        self.assertFalse(missing_path.exists())
        with self.assertRaises(TablesMissing):
            tables.load(missing_path)

    def test_hint_names_the_old_location_when_a_copy_is_still_there(self):
        # A checkout from before v0.8 (st25/lpec -> openevp/decoders/sony_lpec)
        # may still have its table data at the old path: say so.
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "lpec_tables.json"
            old.write_text("{}", encoding="utf-8")
            with mock.patch.object(tables, "_OLD_DATA_FILE", old):
                with self.assertRaises(TablesMissing) as cm:
                    tables.load(REPO_ROOT / "st25" / "lpec" / "data" / "does-not-exist.json")
            self.assertIn("old location", cm.exception.hint)
            self.assertIn("st25/lpec/data", cm.exception.hint)
            self.assertIn("openevp/decoders/sony_lpec/data", cm.exception.hint)

    def test_hint_is_the_plain_one_when_no_old_copy_exists(self):
        with mock.patch.object(tables, "_OLD_DATA_FILE", REPO_ROOT / "st25" / "lpec" / "data" / "lpec_tables.json"):
            with self.assertRaises(TablesMissing) as cm:
                tables.load(REPO_ROOT / "st25" / "lpec" / "data" / "does-not-exist.json")
        self.assertNotIn("old location", cm.exception.hint)


class InvalidTablesTests(unittest.TestCase):
    """A data file with a table of the wrong shape (e.g. truncated) is
    rejected by load() with TablesInvalid (a TablesMissing, so everything
    that handles a missing file handles it too), before any of it can reach
    the C core, which indexes the tables at fixed sizes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lpec_tables.json"

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f)

    def _assert_rejected(self, data):
        self._write(data)
        with self.assertRaises(TablesInvalid) as cm:
            tables.load(self.path)
        self.assertIsInstance(cm.exception, TablesMissing)
        # A plain message for users; the detail is in .hint, and no path
        # (let alone an absolute one) is in the message.
        self.assertEqual(str(cm.exception), "the LPEC table data in this build is damaged")
        self.assertNotIn(self.tmp.name, str(cm.exception))
        return cm.exception

    def test_the_synthetic_fixture_itself_loads(self):
        self._write(_MINIMAL_EXTRACTED)
        self.assertEqual(len(tables.load(self.path).NA), 1024)

    def test_truncated_table_file_is_rejected(self):
        full = json.dumps(_MINIMAL_EXTRACTED)
        self._assert_rejected(full[: len(full) // 2])

    def test_short_vector_table_is_rejected(self):
        e = self._assert_rejected(dict(_MINIMAL_EXTRACTED, NA=[0.0] * 1000))
        self.assertIn("NA", e.hint)

    def test_short_matrix_row_is_rejected(self):
        c1 = [list(r) for r in _MINIMAL_EXTRACTED["C1"]]
        c1[5] = c1[5][:9]
        e = self._assert_rejected(dict(_MINIMAL_EXTRACTED, C1=c1))
        self.assertIn("C1", e.hint)

    def test_missing_rows_are_rejected(self):
        self._assert_rejected(dict(_MINIMAL_EXTRACTED, SHAPES=_MINIMAL_EXTRACTED["SHAPES"][:127]))

    def test_missing_table_is_rejected(self):
        data = dict(_MINIMAL_EXTRACTED)
        del data["VQ8"]
        self._assert_rejected(data)

    def test_non_numbers_are_rejected(self):
        ab = [list(r) for r in _MINIMAL_EXTRACTED["AB"]]
        ab[0][0] = 1.5                        # AB is int16
        self._assert_rejected(dict(_MINIMAL_EXTRACTED, AB=ab))
        self._assert_rejected(dict(_MINIMAL_EXTRACTED, GAIN=["x"] * 128))
        self._assert_rejected(dict(_MINIMAL_EXTRACTED, VQ2=[[0.0, 0.0]] * 255 + [[0.0, [0.0]]]))

    def test_not_an_object_is_rejected(self):
        self._assert_rejected([1, 2, 3])


class TablesMissingMessageTests(unittest.TestCase):
    """The message that reaches users is plain: no path, no developer hint."""

    def test_message_is_plain(self):
        missing = REPO_ROOT / "st25" / "lpec" / "data" / "does-not-exist.json"
        with self.assertRaises(TablesMissing) as cm:
            tables.load(missing)
        msg = str(cm.exception)
        self.assertEqual(msg, "this build does not include the LPEC table data")
        for bad in ("dumps", "import_lpec_tables", str(REPO_ROOT), ":\\", "/"):
            self.assertNotIn(bad, msg)
        self.assertEqual(cm.exception.path, missing)
        self.assertIn("tools/import_lpec_tables.py", cm.exception.hint)


class CachingTests(unittest.TestCase):
    """load() caches the parsed result in-process, keyed by path + mtime + size:
    audio.dvf_to_wav() -> _decoder() -> check() -> load() runs per recording, and
    re-parsing the JSON every time (~0.65 s) is too slow for exports of many
    recordings or repeated playback clicks.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lpec_tables.json"
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(_MINIMAL_EXTRACTED, f)

    def test_second_load_does_not_reread_the_file(self):
        first = tables.load(self.path)
        with mock.patch("builtins.open", side_effect=AssertionError("must not reopen a cached file")):
            second = tables.load(self.path)
        self.assertIs(second, first)

    def test_changed_file_is_reloaded_not_served_stale(self):
        first = tables.load(self.path)
        changed = dict(_MINIMAL_EXTRACTED, GAIN=[1.0, 2.0] + [0.0] * 126)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(changed, f)
        os.utime(self.path, (0, 0))                # force a different mtime even on fast filesystems
        second = tables.load(self.path)
        self.assertIsNot(second, first)
        self.assertEqual(second.GAIN[:3], (1.0, 2.0, 0.0))

    def test_missing_file_still_raises_after_a_prior_successful_load(self):
        tables.load(self.path)                       # warm the cache for a different path
        missing = self.path.with_name("does-not-exist.json")
        with self.assertRaises(TablesMissing):
            tables.load(missing)


class CheckTests(unittest.TestCase):
    """openevp.decoders.sony_lpec.check(), which sony_icd/audio.py uses to detect
    TablesMissing without decoding anything, mirrors tables.load()'s availability."""

    def test_check_matches_tables_load(self):
        from openevp.decoders import sony_lpec
        if _TABLES is None:
            with self.assertRaises(TablesMissing):
                sony_lpec.check()
        else:
            sony_lpec.check()     # must not raise when the data file is present


if __name__ == "__main__":
    unittest.main()
