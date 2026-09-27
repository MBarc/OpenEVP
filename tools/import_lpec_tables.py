"""Import the extracted (non-derivable) LPEC tables into a local data file.

docs/lpec.md's "Tables" section splits the decoder's tables into extracted
ones (no known formula, "no, extract" in the Static tables list) and
derivable ones (computed by openevp/decoders/sony_lpec/tables.py). This script copies only
the extracted ones from a folder of JSON dumps of the tables in Digital
Voice Editor's `LPEC.dll` (one file per table, with its address, size and
element type recorded) into
one file, `openevp/decoders/sony_lpec/data/lpec_tables.json`. That file is git-ignored:
these tables are not committed to the repository (pending legal review).

For each table, this script checks that the source JSON's recorded address,
element count and element type match docs/lpec.md before trusting its
values, so an out-of-date or mismatched dump is caught here
rather than producing a silently wrong decoder.

Usage:
    python tools/import_lpec_tables.py --tables-dir PATH [--out PATH]

--tables-dir defaults to the OPENEVP_TABLE_DUMPS environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLES_DIR = Path(os.environ["OPENEVP_TABLE_DUMPS"]) if os.environ.get("OPENEVP_TABLE_DUMPS") else None
DEFAULT_OUT = REPO_ROOT / "openevp" / "decoders" / "sony_lpec" / "data" / "lpec_tables.json"

# doc name -> (dump file, doc address, doc shape, doc element type).
# Addresses and shapes are docs/lpec.md, "Tables" > "Static tables (in the
# DLL image)"; only the rows marked "no, extract" appear here.
EXTRACTED = {
    "C1": ("lsp_cb1_f64.json", 0x10024440, (64, 10), "float64"),
    "C2": ("lsp_cb2_f64.json", 0x10025840, (64, 10), "float64"),
    "C3": ("lsp_cb3_f64.json", 0x10026c40, (64, 10), "float64"),
    "PT": ("pitch_taps_f64.json", 0x10037d98, (64, 3), "float64"),
    "SHAPES": ("shape8_f64.json", 0x10040960, (128, 8), "float64"),
    "GAIN": ("gain_global_f64.json", 0x100394d0, (128,), "float64"),
    "BG1": ("band_gain1_f64.json", 0x1003c0d0, (64, 8), "float64"),
    "BG2": ("band_gain2_f64.json", 0x1003d0d0, (64, 8), "float64"),
    "VQ2": ("vq_dim2_f64.json", 0x10030440, (256, 2), "float64"),
    "VQ4": ("vq_dim4_f64.json", 0x10031440, (256, 4), "float64"),
    "VQ8": ("vq_dim8_f64.json", 0x10033440, (256, 8), "float64"),
    "AB": ("alloc_base_i16.json", 0x100388b0, (64, 8), "int16"),
    "NA": ("noise_a_f64.json", 0x10044970, (1024,), "float64"),
    "NB": ("noise_b_f64.json", 0x10042970, (1024,), "float64"),
}


def _reshape(flat: list, shape: Tuple[int, ...]) -> list:
    if len(shape) == 1:
        return list(flat)
    rows, cols = shape
    return [flat[r * cols:(r + 1) * cols] for r in range(rows)]


def _element_count(shape: Tuple[int, ...]) -> int:
    n = 1
    for d in shape:
        n *= d
    return n


def _load_one(tables_dir: Path, name: str, filename: str, address: int,
              shape: Tuple[int, ...], dtype: str) -> list:
    source = tables_dir / filename
    with open(source, encoding="utf-8") as f:
        raw = json.load(f)

    raw_address = raw["address"]
    got_address = int(raw_address, 16) if isinstance(raw_address, str) else raw_address
    if got_address != address:
        raise ValueError(
            f"{name}: address mismatch: docs/lpec.md says {address:#010x}, "
            f"{filename} says {got_address:#010x}"
        )

    if raw["type"] != dtype:
        raise ValueError(
            f"{name}: type mismatch: docs/lpec.md says {dtype}, {filename} says {raw['type']}"
        )

    expected_count = _element_count(shape)
    if raw["count"] != expected_count or len(raw["values"]) != expected_count:
        raise ValueError(
            f"{name}: size mismatch: docs/lpec.md says {shape} ({expected_count} values), "
            f"{filename} has count={raw['count']}, len(values)={len(raw['values'])}"
        )

    return _reshape(raw["values"], shape)


def build(tables_dir: Path) -> dict:
    return {
        name: _load_one(tables_dir, name, filename, address, shape, dtype)
        for name, (filename, address, shape, dtype) in EXTRACTED.items()
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tables-dir", type=Path, default=DEFAULT_TABLES_DIR, required=DEFAULT_TABLES_DIR is None,
        help="folder with the table dumps (*.json); default: $OPENEVP_TABLE_DUMPS",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help=f"output path for the combined, git-ignored table data file (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    if not args.tables_dir.is_dir():
        print(f"error: {args.tables_dir} not found", file=sys.stderr)
        return 1

    tables = build(args.tables_dir)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(tables, f)

    print(f"wrote {len(tables)} tables to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
