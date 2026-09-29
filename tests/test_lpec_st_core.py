"""The optional C core of the LPEC ST decoder (_lpec_st.c -> lpec_st_core.dll)
must be an exact drop-in for the pure-Python synthesis.

- Its float32 output of every accepted frame is Sony's, bit for bit
  (float_sha256 of every vector), and its PCM is the pure-Python PCM.
- The pure-Python path stays selectable (use_core=False) and is what the
  decoder falls back to when the DLL is absent.
- Two decodes on two threads (the player and the library indexer) do not
  disturb each other: the whole state is per decode.
- A damaged record or table is refused, never read out of bounds.

Skipped when the DLL has not been built (python tools/build_lpec_core.py)
or the git-ignored table data is absent (failures in the release gate).
"""

import hashlib
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from openevp.decoders.sony_lpec_st import _core, decoder, tables  # noqa: E402
from test_lpec_st_vectors import HAVE_TABLES, NO_TABLES, VECTORS, float_sha, meta, payload, python_pcm  # noqa: E402

NO_CORE = ("openevp/decoders/sony_lpec_st/lpec_st_core.dll is not built (or failed to load); run "
           "python tools/build_lpec_core.py")
SLOW_TESTS = os.environ.get("OPENEVP_SLOW_TESTS") == "1"


def core_floats(state, data, pos):
    if state is None:
        return _core.CoreDecoder(tables.load())
    u = decoder.parse(data, pos, tables.load())
    return None if u is None else state.frame_float(u)


@release_gate.require(HAVE_TABLES, NO_TABLES)
@release_gate.require(_core.available(), NO_CORE)
class CoreVectorTests(unittest.TestCase):
    """C core == Sony (float32 and PCM) on every vector."""

    def check(self, name):
        m = meta(name)
        pcm = decoder.decode(payload(name), use_core=True)[2]
        self.assertEqual(len(pcm), m["pcm_bytes"])
        self.assertEqual(hashlib.sha256(pcm).hexdigest(), m["pcm_sha256"])
        self.assertEqual(float_sha(name, core_floats), m["float_sha256"])


def _make(name):
    def test(self):
        self.check(name)
    test.__name__ = "test_" + name.replace("-", "_")
    test.__doc__ = f"{name}: the C core gives Sony's float32 output and PCM"
    return test


for _name in VECTORS:
    _t = _make(_name)
    setattr(CoreVectorTests, _t.__name__, _t)


@release_gate.require(HAVE_TABLES, NO_TABLES)
@release_gate.require(_core.available(), NO_CORE)
class CoreIdenticalToPythonTests(unittest.TestCase):
    """decode() through the C core == the pure-Python decode."""

    def test_same_pcm_on_every_short_vector(self):
        for name in VECTORS:
            if name == "all-features-400" and not SLOW_TESTS:
                continue                    # minutes in pure Python; its hashes are checked above
            with self.subTest(name):
                self.assertEqual(decoder.decode(payload(name), use_core=True)[2], python_pcm(name))

    def test_default_is_the_core(self):
        with mock.patch.object(_core.CoreDecoder, "frame_pcm", side_effect=AssertionError("core used")):
            with self.assertRaises(AssertionError):
                decoder.decode(payload("all-features-40"))
            decoder.decode(payload("all-features-40"), use_core=False)      # pure Python: not the core

    def test_two_threads_decode_independently(self):
        want = decoder.decode(payload("damaged-300"), use_core=True)[2]
        other = decoder.decode(payload("all-features-40"), use_core=True)[2]
        results = {}

        def run(key, name):
            results[key] = decoder.decode(payload(name), use_core=True)[2]
        threads = [threading.Thread(target=run, args=(i, "damaged-300" if i % 2 else "all-features-40"))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([results[i] for i in range(4)], [other, want, other, want])

    def test_a_malformed_record_is_refused(self):
        t = tables.load()
        core = _core.CoreDecoder(t)
        data = payload("two-frames")
        u = decoder.parse(data, 0, t)
        u.ch[0].sf[0] = 64                  # out of range: the parser never produces it
        with self.assertRaises(_core.CoreError):
            core.frame_pcm(u)
        u = decoder.parse(data, 0, t)
        u.ch[1].spec = u.ch[1].spec[:100]   # wrong length
        with self.assertRaises(ValueError):
            core.frame_pcm(u)

    def test_damaged_tables_are_refused(self):
        class Bad:
            pass
        bad = Bad()
        bad.__dict__.update(vars(tables.load()))
        bad.IMDCT_ORDER = [200] * 128       # an index past its table
        with self.assertRaises(_core.CoreError):
            _core.CoreDecoder(bad)


class FallbackTests(unittest.TestCase):
    """Without the DLL the decoder is pure Python, and asking for the core says so."""

    def test_use_core_true_without_the_dll(self):
        with mock.patch.object(_core, "available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "lpec_st_core.dll"):
                list(decoder.pcm_chunks(b"", tables=object(), use_core=True))

    @release_gate.require(HAVE_TABLES, NO_TABLES)
    def test_default_without_the_dll_is_pure_python(self):
        with mock.patch.object(_core, "available", return_value=False), \
                mock.patch.object(_core, "CoreDecoder", side_effect=AssertionError("core used")):
            self.assertEqual(decoder.decode(payload("all-features-40"))[2], python_pcm("all-features-40"))

    def test_record_layout_matches_the_dll(self):
        if not _core.available():
            release_gate.skip_or_fail(NO_CORE)
        self.assertEqual(_core.RECORD_SIZE,
                         9 + 64 + 2 * (32 * 3 + 5 + 16 + 16 * 15 + 16 * (6 + 4 * _core.MAX_WAVES) + 2048))


if __name__ == "__main__":
    unittest.main()
