import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.devices import NEEDS_DRIVER, NEEDS_REPLUG, READY, DeviceGone, DeviceManager  # noqa: E402
from st25.protocol import RecorderStuck  # noqa: E402
from st25.usb import DriverMissing  # noqa: E402


class FakeSession:
    owner = "Casey"

    def __init__(self, device_id):
        self.device_id = device_id
        self.closed = False

    def close(self):
        self.closed = True


def boom(session):
    raise RecorderStuck("data stopped after 0 of 10 bytes")


class DeviceManagerTests(unittest.TestCase):
    def setUp(self):
        self.present = ["1-4@7"]
        self.opened = []
        self.removed = []
        self.open_error = None

        def open_fn(device_id):
            if self.open_error:
                raise self.open_error
            s = FakeSession(device_id)
            self.opened.append(s)
            return s

        self.m = DeviceManager(lambda: list(self.present), open_fn, on_removed=self.removed.append)
        self.addCleanup(self.m.close)

    def test_unknown_until_listed(self):
        with self.assertRaises(DeviceGone):
            self.m.with_session("1-4@7", lambda s: 1)
        self.m.refresh()
        self.assertEqual(self.m.with_session("1-4@7", lambda s: 1), 1)

    def test_lists_and_opens_lazily_on_one_thread(self):
        self.assertEqual(self.m.refresh(), [{"id": "1-4@7", "port": "1-4", "state": READY,
                                             "message": "", "owner": ""}])
        self.assertEqual(self.opened, [])
        names = {self.m.with_session("1-4@7", lambda s: threading.current_thread().name) for _ in range(3)}
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(len(names), 1)
        self.assertNotEqual(names.pop(), threading.current_thread().name)
        self.assertEqual(self.m.refresh()[0]["owner"], "Casey")

    def test_stuck_advises_replug_and_a_retry_reopens(self):
        self.m.refresh()
        with self.assertRaises(RecorderStuck):
            self.m.with_session("1-4@7", boom)
        self.assertTrue(self.opened[0].closed)
        self.assertEqual(self.m.refresh()[0]["state"], NEEDS_REPLUG)
        self.assertEqual(self.m.with_session("1-4@7", lambda s: 7), 7)      # user clicked again
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.m.refresh()[0]["state"], READY)

    def test_replug_is_a_new_connection(self):
        self.m.refresh()
        self.m.with_session("1-4@7", lambda s: 1)
        self.present = ["1-4@8"]                     # unplugged and replugged between polls
        self.assertEqual([d["id"] for d in self.m.refresh()], ["1-4@8"])
        self.assertTrue(self.opened[0].closed)
        self.assertEqual(self.removed, ["1-4@7"])
        with self.assertRaises(DeviceGone):
            self.m.with_session("1-4@7", lambda s: 1)
        self.assertEqual(self.m.with_session("1-4@8", lambda s: s.device_id), "1-4@8")

    def test_missing_driver_is_retryable(self):
        self.m.refresh()
        self.open_error = DriverMissing("no WinUSB")
        with self.assertRaises(DriverMissing):
            self.m.with_session("1-4@7", lambda s: 1)
        self.assertEqual(self.m.refresh()[0]["state"], NEEDS_DRIVER)
        self.open_error = None
        self.assertEqual(self.m.with_session("1-4@7", lambda s: 2), 2)
        self.assertEqual(self.m.refresh()[0]["state"], READY)

    def test_setup_entries_need_the_driver_and_never_open(self):
        self.present = ["setup:USB\VID_054C&PID_0103\X"]
        d = self.m.refresh()[0]
        self.assertEqual((d["state"], d["port"]), (NEEDS_DRIVER, ""))
        with self.assertRaises(DriverMissing):
            self.m.with_session(d["id"], lambda s: 1)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.m.refresh()[0]["state"], NEEDS_DRIVER)

    def test_a_stuck_recorder_does_not_block_another(self):
        self.present = ["1-4@7", "2-1@3"]
        self.m.refresh()
        with self.assertRaises(RecorderStuck):
            self.m.with_session("1-4@7", boom)
        self.assertEqual(self.m.with_session("2-1@3", lambda s: "fine"), "fine")
        states = {d["id"]: d["state"] for d in self.m.refresh()}
        self.assertEqual(states, {"1-4@7": NEEDS_REPLUG, "2-1@3": READY})


if __name__ == "__main__":
    unittest.main()
