"""Vector gate for the LPEC ST decoder (openevp.decoders.sony_lpec_st).

Every vector in tests/vectors/lpec_st/ is a synthetic frame stream (random
valid frames covering every coding mode and feature, one also with damaged
and rejected frames; no recorded audio) decoded by Sony's own lcstde.ax.
The decoder must reproduce Sony's float32 output of every accepted frame bit
for bit (float_sha256) and its PCM byte for byte (pcm_sha256 and, for the
small vectors, the committed .pcm):

  <name>.bin   the 283-byte frames
  <name>.pcm   Sony's PCM for it (interleaved int16; the first frame after
               each reset and rejected frames give no samples)
  <name>.json  frame count, SHA-256 of the PCM and of the float32 output

These tests run the pure-Python path (use_core=False); test_lpec_st_core
checks the C core against the same vectors. all-features-400 takes minutes
in pure Python, so its test here exists only with OPENEVP_SLOW_TESTS=1 (the
C core test covers it always).

Needs the git-ignored table data (openevp/decoders/sony_lpec_st/data/
lpec_st_tables.json); skipped without it (a failure in the release gate).
"""

import functools
import hashlib
import json
import os
import sys
import unittest
from array import array
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from openevp.decoders.sony_lpec_st import TablesMissing, decoder, tables  # noqa: E402

VECTORS_DIR = Path(__file__).resolve().parent / "vectors" / "lpec_st"
VECTORS = tuple(sorted(p.stem for p in VECTORS_DIR.glob("*.json")))
SLOW = {"all-features-400"}          # a few minutes in pure Python
NO_TABLES = ("openevp/decoders/sony_lpec_st/data/lpec_st_tables.json not found; "
             "run tools/import_lpec_st_tables.py")


def _tables_present():
    try:
        tables.load()
        return True
    except TablesMissing:
        return False


HAVE_TABLES = _tables_present()
SLOW_TESTS = os.environ.get("OPENEVP_SLOW_TESTS") == "1"


def meta(name):
    return json.loads((VECTORS_DIR / f"{name}.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def payload(name) -> bytes:
    return (VECTORS_DIR / f"{name}.bin").read_bytes()


@functools.lru_cache(maxsize=None)
def python_pcm(name) -> bytes:
    """The pure-Python decode of a vector (cached: test_lpec_st_core compares
    the C core against it)."""
    return decoder.decode(payload(name), use_core=False)[2]


def float_sha(name, frame_floats) -> str:
    """SHA-256 of the float32 output of every accepted frame, as Sony's
    decoder produced it (make_vectors.py). ``frame_floats(dec_state, data,
    pos)`` returns (left, right) or None for a rejected frame; it is given
    a fresh state per reset."""
    data = payload(name)
    h = hashlib.sha256()
    state = frame_floats(None, None, None)
    for pos in range(0, len(data) - decoder.FRAME_BYTES + 1, decoder.FRAME_BYTES):
        if data[pos] == 0 and data[pos + 1] == 0:
            state = frame_floats(None, None, None)
            continue
        res = frame_floats(state, data, pos)
        if res is not None:
            h.update(array("f", res[0]).tobytes())
            h.update(array("f", res[1]).tobytes())
    return h.hexdigest()


def python_floats(state, data, pos):
    if state is None:
        return decoder.Decoder(tables.load())
    return state.decode_frame(data, pos)


@release_gate.require(HAVE_TABLES, NO_TABLES)
class VectorTests(unittest.TestCase):
    """Pure Python == Sony, float for float and sample for sample."""

    def check(self, name):
        m = meta(name)
        pcm = python_pcm(name)
        self.assertEqual(len(pcm), m["pcm_bytes"])
        pcm_file = VECTORS_DIR / f"{name}.pcm"
        if pcm_file.is_file():
            self.assertEqual(pcm, pcm_file.read_bytes())
        self.assertEqual(hashlib.sha256(pcm).hexdigest(), m["pcm_sha256"])
        self.assertEqual(float_sha(name, python_floats), m["float_sha256"])

    def test_the_vectors_are_there(self):
        self.assertEqual(VECTORS, ("all-features-40", "all-features-400", "damaged-300", "two-frames"))

    def test_decode_returns_rate_and_channels(self):
        self.assertEqual(decoder.decode(payload("two-frames"), use_core=False)[:2], (44100, 2))


def _make(name):
    def test(self):
        self.check(name)
    test.__name__ = "test_" + name.replace("-", "_")
    test.__doc__ = f"{name}: pure Python gives Sony's float32 output and PCM"
    return test


for _name in VECTORS:
    if _name in SLOW and not SLOW_TESTS:
        continue                        # test_lpec_st_core checks it on the C core
    _t = _make(_name)
    setattr(VectorTests, _t.__name__, _t)


if __name__ == "__main__":
    unittest.main()
