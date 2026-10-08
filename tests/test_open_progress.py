"""Progress while a recording opens: the decoders report how far they are
(and stop when asked, inside the C core too), and Api.audio passes it to the
page as "open-progress" events. Synthetic vectors only."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import release_gate  # noqa: E402
from openevp.decoders.sony_lpec import _core, decoder  # noqa: E402
from openevp.decoders.sony_lpec_st import decoder as st_decoder  # noqa: E402
import test_backend  # noqa: E402  (the module: importing BackendTests would collect its tests here)
from test_lpec_vectors import _HAVE_TABLES, VECTORS_DIR  # noqa: E402
from test_lpec_vectors import vector_payload  # noqa: E402

NO_TABLES = "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py"


def long_payload():
    from sony_icd import dvf
    with open(VECTORS_DIR / "long-mixed-10min.dvf", "rb") as f:
        return dvf.payload(f.read())


def decode(payload, use_core, **kw):
    return decoder._decode(payload, decoder._tables_for(None, None), use_core, kw.pop("should_stop", None), 0, **kw)


@release_gate.require(_HAVE_TABLES, NO_TABLES)
class LpecProgressTests(unittest.TestCase):
    def check(self, payload, use_core):
        seen = []
        pcm = decode(payload, use_core, progress=seen.append)
        self.assertEqual(pcm, decode(payload, use_core))                 # reporting changes nothing
        self.assertEqual(seen, sorted(seen))
        self.assertEqual((seen[0], seen[-1]), (0.0, 1.0))
        return seen

    @release_gate.require(_core.available(), "lpec_core.dll not built")
    def test_the_core_path_reports_parsing_then_decoding(self):
        seen = self.check(long_payload(), True)
        self.assertTrue(any(0 < f < decoder._PARSE_SHARE for f in seen))
        self.assertTrue(any(decoder._PARSE_SHARE < f < 1 for f in seen))   # from inside the core

    def test_the_python_path_reports_too(self):
        self.check(vector_payload("white-noise"), False)

    @release_gate.require(_core.available(), "lpec_core.dll not built")
    def test_a_stop_is_heard_inside_the_core(self):
        seen = []
        with self.assertRaises(decoder.Cancelled):
            decode(long_payload(), True, progress=seen.append,
                   should_stop=lambda: bool(seen) and seen[-1] > 0.5)
        self.assertLess(seen[-1], 0.6)                                     # it stopped soon after


class StProgressTests(unittest.TestCase):
    def test_frames_report_to_the_end(self):
        seen = []
        n = len(list(st_decoder._frames(bytes(283 * 200), None, seen.append)))
        self.assertEqual(n, 200)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual((seen[0], seen[-1]), (0.0, 1.0))


class OpenProgressEventTests(unittest.TestCase):
    setUp = test_backend.BackendTests.setUp            # its fake ST25 and Api, not its tests

    def test_audio_reports_converting_then_finishing(self):
        fake = types.ModuleType("openevp.decoders.sony_lpec")

        def dvf_to_wav(data, should_stop=None, progress=None):
            for f in (0.0, 0.004, 0.5, 1.0):                # 0.004: same whole percent as 0, not sent
                progress(f)
            return b"RIFF" + data[:8]
        fake.dvf_to_wav = dvf_to_wav
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            self.assertTrue(self.api.audio(test_backend.ID, "A", 1)["ok"])
        sent = [(p["job"], p["stage"], p["fraction"]) for e, p in self.events if e == "open-progress"]
        job = f"{test_backend.ID}/A/1"
        self.assertEqual([s for s in sent if s[1] != "download"],
                         [(job, "convert", 0.0), (job, "convert", 0.5), (job, "convert", 1.0), (job, "finish", 1.0)])


if __name__ == "__main__":
    unittest.main()
