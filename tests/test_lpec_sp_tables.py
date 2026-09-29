"""The LPEC SP (16000 Hz) table data (openevp/decoders/sony_lpec/data/
lpec_sp_tables.json) and tools/import_lpec_tables.py, which writes it.

- the import tool's SP list and the loader's shapes agree, and a damaged or
  missing SP file is refused like an LP one (no table data needed);
- sizes and types of the derived tables (needs the SP data file);
- bit-exact against the dumps of LPEC.dll where there are some (set
  OPENEVP_TABLE_DUMPS to their folder): the int16 LSP codebooks (Q16, Q19),
  the Q15 pitch taps and the run-time windows, post-twiddles and initial
  LSPs the DLL computes for 16000 Hz.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import import_lpec_tables  # noqa: E402
from openevp.decoders.sony_lpec import TablesMissing, config, tables  # noqa: E402
from openevp.decoders.sony_lpec.tables import TablesInvalid  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DUMPS = Path(os.environ.get("OPENEVP_TABLE_DUMPS") or REPO_ROOT / "no-table-dumps")
SHAPES = tables.extracted_shapes(config.SP)


def _zeros(name, shape):
    zero = 0 if name == "AB" else 0.0
    if len(shape) == 1:
        return [zero] * shape[0]
    return [[zero] * shape[1] for _ in range(shape[0])]


MINIMAL = {name: _zeros(name, shape) for name, shape in SHAPES.items()}


def _flatten(rows):
    if isinstance(rows, (int, float)):
        return [rows]
    if rows and isinstance(rows[0], (list, tuple)):
        return [x for row in rows for x in row]
    return list(rows)


def _load():
    try:
        return tables.load(config=config.SP)
    except TablesMissing:
        return None


_TABLES = _load()


class ShapeTests(unittest.TestCase):
    def test_the_import_tool_writes_what_the_loader_reads(self):
        self.assertEqual({k: v[2] for k, v in import_lpec_tables.EXTRACTED_SP.items()}, SHAPES)
        self.assertEqual(list(SHAPES)[:4], ["C1", "C2", "C3", "C4"])
        self.assertEqual((SHAPES["C4"], SHAPES["BG1"], SHAPES["AB"]), ((64, 16), (64, 10), (64, 10)))

    def test_a_synthetic_file_loads_and_a_damaged_one_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lpec_sp_tables.json"
            path.write_text(json.dumps(MINIMAL), encoding="utf-8")
            t = tables.load(path, config=config.SP)
            self.assertEqual((t.config, len(t.C), len(t.D), len(t.LSP_INIT), len(t.WIN), len(t.WIN_LONG)),
                             (config.SP, 4, 4, 17, 1024, 2048))
            self.assertEqual([len(p) for p in t.POST], [512, 768, 768, 1024])
            with self.assertRaises(AttributeError):
                t.WIN512                                    # an LP-only name
            bad = dict(MINIMAL, C4=[[0.0] * 10] * 64)      # LP's LSP order
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(TablesInvalid) as cm:
                tables.load(path, config=config.SP)
            self.assertIn("C4", cm.exception.hint)
            del bad["C4"]
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(TablesInvalid):
                tables.load(path, config=config.SP)
            # The LP file is no SP file.
            with self.assertRaises(TablesInvalid):
                tables.load(path, config=config.LP)

    def test_missing_file_says_which_tables(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TablesMissing) as cm:
                tables.load(Path(d) / "lpec_sp_tables.json", config=config.SP)
        self.assertEqual(str(cm.exception), "this build does not include the LPEC SP table data")

    def test_the_tool_writes_the_sp_file_only_from_sp_dumps(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d)
            for sets in (import_lpec_tables.EXTRACTED, import_lpec_tables.EXTRACTED_SP):
                for name, (filename, address, shape, dtype) in sets.items():
                    n = 1
                    for k in shape:
                        n *= k
                    (src / filename).write_text(json.dumps({"address": hex(address), "type": dtype, "count": n,
                                                            "values": [0] * n}), encoding="utf-8")
            lp, sp = src / "out" / "lp.json", src / "out" / "sp.json"
            self.assertEqual(import_lpec_tables.main(["--tables-dir", str(src), "--out", str(lp),
                                                      "--sp-out", str(sp)]), 0)
            self.assertEqual(set(json.loads(sp.read_text())), set(SHAPES))
            self.assertEqual(set(json.loads(lp.read_text())), set(tables.extracted_shapes()))
            (src / "sp_lsp_cb4_f64.json").unlink()
            sp.unlink()
            self.assertEqual(import_lpec_tables.main(["--tables-dir", str(src), "--out", str(lp),
                                                      "--sp-out", str(sp)]), 0)
            self.assertFalse(sp.exists())


@unittest.skipIf(_TABLES is None, "openevp/decoders/sony_lpec/data/lpec_sp_tables.json not found; "
                                  "run tools/import_lpec_tables.py")
class DerivedTablesTests(unittest.TestCase):
    def test_sizes(self):
        t = _TABLES
        for d in t.D:
            self.assertEqual((len(d), len(d[0])), (64, 16))
            self.assertTrue(all(isinstance(x, int) and -32768 <= x <= 32767 for row in d for x in row))
        self.assertEqual(t.R, 432)
        self.assertEqual(len(t.LSP_INIT), 17)


@unittest.skipIf(_TABLES is None, "no LPEC SP table data")
@unittest.skipUnless(DUMPS.is_dir(), f"{DUMPS} not found; set OPENEVP_TABLE_DUMPS")
class BitExactAgainstDumpsTests(unittest.TestCase):
    COMPARISONS = {
        "D1": "sp_lsp_cb1_i16.json", "D2": "sp_lsp_cb2_i16.json", "D3": "sp_lsp_cb3_i16.json",
        "D4": "sp_lsp_cb4_i16.json", "PQ": "sp_pitch_taps_i16.json", "R": "sp_alloc_ref_i16.json",
        "WIN": "rt_win_sine_1024.json", "WIN_SQ": "rt_win_sine2_1024.json",
        "WIN_LONG": "rt_win_b380_2048.json", "POST0": "rt_mdct_post_512.json",
        "POST1": "rt_mdct_post_768.json", "POST3": "rt_mdct_post_1024.json",
        "LSP_INIT": "rt_lsp_init16_f64.json",
    }

    def value(self, attr):
        t = _TABLES
        if attr[0] == "D" and attr[1:].isdigit():
            return t.D[int(attr[1:]) - 1]
        if attr.startswith("POST"):
            return t.POST[int(attr[4:])]
        return getattr(t, attr)

    def test_generated_tables_match_the_dll(self):
        for attr, filename in self.COMPARISONS.items():
            with self.subTest(table=attr):
                with open(DUMPS / filename, encoding="utf-8") as f:
                    want = json.load(f)["values"]
                self.assertEqual(_flatten(self.value(attr)), want)


if __name__ == "__main__":
    unittest.main()
