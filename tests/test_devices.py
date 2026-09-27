import os
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.devices import NEEDS_DRIVER, NEEDS_REPLUG, READY, DeviceGone, DeviceManager  # noqa: E402
from openevp.recorders import base  # noqa: E402
from openevp.recorders.base import DriverMissing, RecorderError  # noqa: E402
from openevp.recorders import sony_st25  # noqa: E402
from openevp.recorders.sony_st25 import SETUP_MESSAGE  # noqa: E402
from st25.folder import TableError  # noqa: E402
from st25.protocol import RecorderError as St25RecorderError, RecorderStuck  # noqa: E402
from st25.usb import DriverMissing as St25DriverMissing, UsbError  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")

MODEL = "sony-icd-st25"


def discovered(device_id):
    """What the ST25 model reports for a connection id (a "setup:" one needs its driver)."""
    if device_id.startswith("setup:"):
        return base.DiscoveredDevice(device_id, MODEL, "", state=NEEDS_DRIVER, message=SETUP_MESSAGE)
    port = device_id.split("@")[0]
    return base.DiscoveredDevice(device_id, MODEL, "port " + port, locator=device_id, where="on USB port " + port)


class FakeSession:
    owner = "Casey"

    def __init__(self, device_id):
        self.device_id = device_id
        self.closed = False

    def close(self):
        self.closed = True


def boom(session):
    raise RecorderError("data stopped after 0 of 10 bytes")       # e.g. st25's RecorderStuck, translated


class DeviceManagerTests(unittest.TestCase):
    def setUp(self):
        self.present = ["1-4@7"]
        self.opened = []
        self.removed = []
        self.open_error = None

        def open_fn(model, device):
            self.assertEqual(model.model_id, MODEL)
            if self.open_error:
                raise self.open_error
            s = FakeSession(device.locator)
            self.opened.append(s)
            return s

        self.m = DeviceManager(lambda: ([discovered(i) for i in self.present], []), open_fn,
                               on_removed=self.removed.append)
        self.addCleanup(self.m.close)

    def test_unknown_until_listed(self):
        with self.assertRaises(DeviceGone):
            self.m.with_session("1-4@7", lambda s: 1)
        self.m.refresh()
        self.assertEqual(self.m.with_session("1-4@7", lambda s: 1), 1)

    def test_lists_and_opens_lazily_on_one_thread(self):
        self.assertEqual(self.m.refresh(), [{"id": "1-4@7", "model_id": MODEL, "model": "Sony ICD-ST25",
                                             "port": "port 1-4", "state": READY, "message": "", "owner": ""}])
        self.assertEqual(self.opened, [])
        names = {self.m.with_session("1-4@7", lambda s: threading.current_thread().name) for _ in range(3)}
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(len(names), 1)
        self.assertNotEqual(names.pop(), threading.current_thread().name)
        self.assertEqual(self.m.refresh()[0]["owner"], "Casey")

    def test_stuck_advises_replug_and_a_retry_reopens(self):
        self.m.refresh()
        with self.assertRaises(RecorderError):
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
        with self.assertRaises(DeviceGone) as caught:
            self.m.with_session("1-4@7", lambda s: 1)
        self.assertEqual(str(caught.exception), "the recorder on USB port 1-4 was unplugged")
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
        self.present = [r"setup:USB\VID_054C&PID_0103\X"]
        d = self.m.refresh()[0]
        self.assertEqual((d["state"], d["port"], d["message"]), (NEEDS_DRIVER, "", SETUP_MESSAGE))
        with self.assertRaises(DriverMissing) as caught:
            self.m.with_session(d["id"], lambda s: 1)
        self.assertEqual(str(caught.exception), SETUP_MESSAGE)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.m.refresh()[0]["state"], NEEDS_DRIVER)

    def test_a_failed_discovery_leaves_that_models_recorders_alone(self):
        """libusb failing to list says nothing about the recorders already open:
        as in v0.7.2 they stay listed and open, and only the problem is reported."""
        self.m.refresh()
        self.m.with_session("1-4@7", lambda s: 1)
        failure = RecorderError("libusb_init failed: not found")
        self.m._discover = lambda: ([], [(MODEL, failure)])
        rows, problems = self.m.refresh(problems=True)
        self.assertEqual([(r["id"], r["state"], r["owner"]) for r in rows], [("1-4@7", READY, "Casey")])
        self.assertEqual([(m.model_id, e) for m, e in problems], [(MODEL, failure)])
        self.assertFalse(self.opened[0].closed)
        self.assertEqual(self.removed, [])
        self.assertEqual(self.m.with_session("1-4@7", lambda s: 2), 2)
        self.assertEqual(len(self.opened), 1)
        self.m._discover = lambda: ([], [("another-model", failure)])     # only the failed model's are kept
        self.assertEqual(self.m.refresh(), [])
        self.assertTrue(self.opened[0].closed)
        self.assertEqual(self.removed, ["1-4@7"])

    def test_a_stuck_recorder_does_not_block_another(self):
        self.present = ["1-4@7", "2-1@3"]
        self.m.refresh()
        with self.assertRaises(RecorderError):
            self.m.with_session("1-4@7", boom)
        self.assertEqual(self.m.with_session("2-1@3", lambda s: "fine"), "fine")
        states = {d["id"]: d["state"] for d in self.m.refresh()}
        self.assertEqual(states, {"1-4@7": NEEDS_REPLUG, "2-1@3": READY})


class RawSt25Session:
    """Stands in for st25.session.RecorderSession: messages() raises the next
    scripted st25 exception, if any."""

    def __init__(self, script, device_id):
        self.script, self.device_id, self.closed = script, device_id, False
        self.owner = "Casey"

    def messages(self, letter):
        failure = self.script.pop(0) if self.script else None
        if failure is not None:
            raise failure
        return []

    def close(self):
        self.closed = True


def v072_devices():
    """app/devices.py as released in v0.7.2 (from git), or None without the history."""
    try:
        source = subprocess.run(["git", "-C", ROOT, "show", "v0.7.2:app/devices.py"], capture_output=True,
                                check=True, text=True, encoding="utf-8").stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    module = types.ModuleType("v072_devices")
    exec(compile(source, "v0.7.2:app/devices.py", "exec"), module.__dict__)
    return module


# The ST25's connection errors, each raised on two requests in a row and then not.
ST25_FAILURES = [St25RecorderError("the recorder answered 0x05"), RecorderStuck("data stopped after 0 of 10 bytes"),
                 UsbError("LIBUSB_ERROR_IO"), TableError("folder table too short")]


def st25_trace(make_manager, request, failure):
    """What the user sees for one ST25 connection when two requests fail with
    failure and a third works: [(step, what)] with list states, errors and opens."""
    script, opened = [failure, failure], []

    def raw_open(device_id):
        s = RawSt25Session(script, device_id)
        opened.append(s)
        return s
    manager = make_manager(raw_open)
    trace = []
    try:
        def listed():
            return [(d["id"], d["state"], d["message"]) for d in manager.refresh()]
        trace.append(("list", listed()))
        for _ in range(3):
            try:
                trace.append(("ok", request(manager)))
            except Exception as e:          # the type differs (st25's vs the shared one); the text must not
                trace.append(("error", str(e), [s.closed for s in opened]))
            trace.append(("list", listed(), len(opened)))
    finally:
        manager.close()
    return trace


def new_manager(raw_open):
    """Today's manager with the real ST25 adapter (SonyST25.open, ST25Session)
    over RawSt25Session."""
    patcher = mock.patch.object(sony_st25.RecorderSession, "open", side_effect=raw_open)

    def open_device(model, device):
        with patcher:
            return model.open(device)
    return DeviceManager(lambda: ([discovered("1-4@7")], []), open_device)


def old_manager(module):
    return lambda raw_open: module.DeviceManager(lambda: ["1-4@7"], raw_open)


class St25KeepsV072BehaviourTests(unittest.TestCase):
    """R10: after an ST25 RecorderError/UsbError/TableError the session is
    closed and the recorder listed as needing a replug, but the next request
    opens a new session (and success lists it READY again), exactly as v0.7.2.
    Only base.NotReady latches (test_app_recorders)."""

    @staticmethod
    def expected(failure):
        text = str(failure)
        return [("list", [("1-4@7", READY, "")]),
                ("error", text, [True]), ("list", [("1-4@7", NEEDS_REPLUG, text)], 1),
                ("error", text, [True, True]), ("list", [("1-4@7", NEEDS_REPLUG, text)], 2),
                ("ok", 0), ("list", [("1-4@7", READY, "")], 3)]

    def test_the_st25_adapter_retries_as_v072_did(self):
        for failure in ST25_FAILURES:
            with self.subTest(failure=type(failure).__name__):
                trace = st25_trace(new_manager, lambda m: len(m.with_session("1-4@7", lambda s: s.recordings("A"))),
                                   failure)
                self.assertEqual(trace, self.expected(failure))

    def test_the_same_as_the_real_v072_code(self):
        module = v072_devices()
        if module is None:
            self.skipTest("no git history with the v0.7.2 tag (e.g. a source export)")
        for failure in ST25_FAILURES + [St25DriverMissing("not bound to WinUSB")]:
            with self.subTest(failure=type(failure).__name__):
                old = st25_trace(old_manager(module), lambda m: len(m.with_session("1-4@7", lambda s: s.messages("A"))),
                                 failure)
                new = st25_trace(new_manager, lambda m: len(m.with_session("1-4@7", lambda s: s.recordings("A"))),
                                 failure)
                self.assertEqual(new, old)
                if not isinstance(failure, St25DriverMissing):
                    self.assertEqual(old, self.expected(failure))


if __name__ == "__main__":
    unittest.main()
