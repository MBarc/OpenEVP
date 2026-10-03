import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice, make_raw  # noqa: E402
from sony_icd.folder import parse  # noqa: E402
from sony_icd.protocol import Recorder  # noqa: E402

FOLDERS = {1: [(0, 100, 0x1000, 2958, DATE, "Owner"), (1, 900, 0x3000, 4000, DATE, "Owner")],
           3: [(0, 50, 0x1000, 3000, DATE, "Owner")]}


def recorder(dev):
    r = Recorder.__new__(Recorder)
    r.dev = dev
    return r


class FakeRecorderTests(unittest.TestCase):
    def test_serves_tables_and_voice_of_the_folder_in_the_opcode(self):
        dev = FakeRecorderDevice(FOLDERS)
        r = recorder(dev)
        msgs = parse(r.folder_table(1))
        self.assertEqual([m.length for m in msgs], [2958, 4000])
        self.assertEqual(parse(r.folder_table(2)), [])       # the last table read is B's
        self.assertEqual(r.voice_data(1, 2, msgs[1].blocks), make_raw(4000, 900))
        self.assertEqual(r.voice_data(3, 1, 3), make_raw(3000, 50))
        self.assertEqual(dev.voice_calls, [(1, 2), (3, 1)])


if __name__ == "__main__":
    unittest.main()
