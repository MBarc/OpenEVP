import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice, make_raw, make_table  # noqa: E402
from st25 import dvf  # noqa: E402
from st25.folder import parse  # noqa: E402
from st25.protocol import Recorder, RecorderError  # noqa: E402
from st25.session import RecorderSession, build_dvf  # noqa: E402

FOLDERS = {1: [(0, 100, 0x1000, 2958, DATE, "Casey"), (1, 900, 0x3000, 4000, DATE, "Casey")],
           2: [(0, 50, 0x1000, 3000, DATE, "Casey")]}


def session(folders=FOLDERS):
    dev = FakeRecorderDevice(folders)
    r = Recorder.__new__(Recorder)
    r.dev = dev
    s = RecorderSession(r)
    s.connect()
    return s, dev


class SessionTests(unittest.TestCase):
    def test_lists_folders_and_owner(self):
        s, _ = session()
        self.assertEqual([m.number for m in s.messages("A")], [1, 2])
        self.assertEqual(s.messages("C"), [])
        self.assertEqual(s.owner, "Casey")

    def test_download_builds_dvf_and_is_cached(self):
        s, dev = session()
        d1 = s.download("A", 2)
        self.assertEqual(d1.label, "A-002")
        self.assertEqual(d1.name, "001_A_002_Casey_2029_05_23.dvf")
        self.assertEqual(d1.dvf, dvf.build(make_raw(4000, 900), DATE, "Casey", expected_length=4000))
        self.assertIs(s.download("A", 2), d1)
        self.assertEqual(dev.voice_calls, [(1, 2)])

    def test_download_reselects_the_folder_first(self):
        s, dev = session()
        s.messages("A")
        s.messages("B")                  # the recorder's current folder is now B
        s.download("A", 1)
        self.assertEqual(dev.voice_calls, [(1, 1)])

    def test_unsupported_recording_is_reported_not_raised(self):
        s, _ = session()
        s.messages("A")[0].problem = "no address range found for slot 0"
        d = s.download("A", 1)
        self.assertEqual((d.dvf, d.error), (b"", "no address range found for slot 0"))

    def test_counter_mismatch_raises(self):
        m = parse(make_table([(0, 100, 0x1000, 2958, DATE, "X")]))[0]
        with self.assertRaises(RecorderError):
            build_dvf(make_raw(2958, 101), m, "A-001")

    def test_unknown_number_raises(self):
        s, _ = session()
        with self.assertRaises(ValueError):
            s.download("A", 3)


if __name__ == "__main__":
    unittest.main()
