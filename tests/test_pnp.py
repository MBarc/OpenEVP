import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.pnp import needs_setup  # noqa: E402

A = "USB\VID_054C&PID_0103\5&38E97A59&0&7"
B = "USB\VID_054C&PID_0103\5&38E97A59&0&3"


class NeedsSetupTests(unittest.TestCase):
    def test_all_usable_means_nothing_to_set_up(self):
        self.assertEqual(needs_setup([A], 1), [])
        self.assertEqual(needs_setup([], 0), [])

    def test_recorder_without_driver_is_listed(self):
        self.assertEqual(needs_setup([A], 0), ["setup:" + A])
        self.assertEqual(len(needs_setup([A, B], 1)), 1)


if __name__ == "__main__":
    unittest.main()
