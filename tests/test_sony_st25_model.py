"""The Sony ICD-ST25 model (openevp.recorders.sony_st25): the shared recorder
contract, and the adapter against today's st25 RecorderSession on the same
emulated recorder (listings, bytes, file names, errors, the exact commands
sent, folder reselection), plus the placeholder models."""
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice, make_raw  # noqa: E402
from recorder_contract import RecorderContract  # noqa: E402
from app import devices as app_devices  # noqa: E402
from openevp import pnp  # noqa: E402
from openevp.pnp import needs_setup  # noqa: E402
from openevp import formats, recorders  # noqa: E402
from openevp.recorders import base  # noqa: E402
from openevp.recorders import sony_st25  # noqa: E402
from st25 import policy  # noqa: E402
from st25.folder import PAGE, TableError  # noqa: E402
from st25.protocol import PID, VID, RecorderError, RecorderStuck  # noqa: E402
from st25.session import RecorderSession  # noqa: E402
from st25.usb import DriverMissing, UsbError  # noqa: E402

UNDATED = b"\xff" * 8
FEB31 = bytes.fromhex("07ed021f0b0c0d00")        # 2029-02-31 11:12:13: shown as stored (A12)
# folder -> make_table() tuples (slot, start counter, start address, length, date, owner)
FOLDERS = {1: [(0, 100, 0x1000, 2958, DATE, "Casey"), (1, 900, 0x3000, 4000, DATE, "Casey"),
               (2, 1500, 0x6000, 1500, UNDATED, "Casey")],
           2: [(0, 50, 0x1000, 3000, FEB31, "")],
           3: [(0, 70, 0x1000, 2048, DATE, "Casey"), (1, 90, 0x2000, 2048, DATE, "Casey")],
           5: [(4, 300, 0x9000, 1100, DATE, "Casey Q Hunters")]}
BROKEN = {3: {0}}          # folder 3, slot 0: its address range is missing (a recording with a problem)
PORT_ID = "1-4@7"


class Device(FakeRecorderDevice):
    """The emulated recorder, recording every command frame it is sent."""

    def __init__(self, folders=FOLDERS, broken=BROKEN, skew=0):
        super().__init__(folders)
        self.broken, self.skew = broken, skew
        self.frames = []
        self.closed = False

    def control_out(self, rt, req, val, idx, data, timeout_ms):
        self.frames.append(bytes(data))
        super().control_out(rt, req, val, idx, data, timeout_ms)
        op = struct.unpack(">I", data[12:16])[0]
        if op & 0xFFFF00FF == policy.CMD_FOLDER_INFO:
            table = bytearray(self.bulk)
            for slot in self.broken.get(self.current, ()):
                at = (5 + slot // 64) * PAGE + (slot % 64) * 8
                table[at:at + 8] = b"\xff" * 8
            self.bulk = bytes(table)
        elif op == policy.CMD_GET_VOICE and self.skew:
            _slot, counter, _start, length, _date, _owner = self.folders[self.current][(struct.unpack(
                ">I", data[16:20])[0] >> 16) - 1]
            self.bulk = make_raw(length, counter + self.skew)

    def close(self):
        self.closed = True


def today(dev):
    """Today's RecorderSession on dev, opened the way the app opens it."""
    with mock.patch("st25.protocol.Device", lambda vid, pid, device_id: dev):
        return RecorderSession.open(PORT_ID)


# What v0.7.2's app showed (frozen here: the app now shows the adapter's rows).
TODAY_SETUP_MESSAGE = "This recorder's driver is not set up on this PC yet."


def today_row(m):
    """A recording row as v0.7.2's app showed it (backend._recording + app.js)."""
    r = {"number": m.number, "when": m.when(), "seconds": round(m.seconds(), 1), "owner": m.owner,
         "problem": m.problem}
    return {"number": r["number"], "recorded_label": "undated" if r["when"] == "no date" else r["when"],
            "seconds": r["seconds"], "owner": r["owner"], "problem": r["problem"]}


class Plugged:
    """Patches USB discovery and opening so the model sees emulated recorders."""

    def __init__(self, testcase, ids=(PORT_ID,), instances=(), make_device=Device, drivers=None):
        """drivers: {Windows instance id: (driver service, problem code)} as
        Windows reports them; None = Windows cannot say (pnp counts instead)."""
        self.ids, self.instances, self.make_device = list(ids), list(instances), make_device
        self.drivers = drivers
        self.devices = []
        for target, value in (("openevp.recorders.sony_st25.list_devices", self._list),
                              ("openevp.pnp._usb_instances", lambda: list(self.instances)),
                              ("openevp.pnp.driver_of", self._driver_of),
                              ("st25.protocol.Device", self._open)):
            patcher = mock.patch(target, value)
            patcher.start()
            testcase.addCleanup(patcher.stop)

    def _driver_of(self, instance_id):
        if self.drivers is None:
            raise pnp.PnpError("Windows cannot say")
        return self.drivers[instance_id]

    def _list(self, vid, pid):
        assert (vid, pid) == (VID, PID)
        return list(self.ids)

    def _open(self, vid, pid, device_id):
        assert (vid, pid, device_id) == (VID, PID, PORT_ID)
        dev = self.make_device()
        self.devices.append(dev)
        return dev


class ST25Contract(RecorderContract, unittest.TestCase):
    def setUp(self):
        self.model = recorders.get("sony-icd-st25")
        Plugged(self)


class AdapterMatchesTodayTests(unittest.TestCase):
    def setUp(self):
        self.model = recorders.get("sony-icd-st25")
        self.plugged = Plugged(self)

    def adapter(self):
        [device] = self.model.discover()
        session = self.model.open(device)
        self.addCleanup(session.close)
        return session, self.plugged.devices[-1]

    def test_identity(self):
        m = self.model
        self.assertIsInstance(m, sony_st25.SonyST25)
        self.assertEqual((m.model_id, m.name, m.usb_ids, m.needs_winusb), (
            "sony-icd-st25", "Sony ICD-ST25", ((0x054C, 0x0103),), True))
        self.assertIs(m.native, formats.DVF)
        self.assertIs(recorders.find(0x054C, 0x0103), m)

    def test_folders_are_a_to_e(self):
        s, _ = self.adapter()
        self.assertEqual(s.folders(), [{"id": l, "label": f"Folder {l}", "safe_name": l} for l in "ABCDE"])

    def test_listing_matches_today(self):
        s, _ = self.adapter()
        old = today(Device())
        ours = {f["id"]: s.recordings(f["id"]) for f in s.folders()}
        theirs = {l: [today_row(m) for m in old.messages(l)] for l in "ABCDE"}
        self.assertEqual({l: [{k: r[k] for k in r if k != "recorded_sort"} for r in rows]
                          for l, rows in ours.items()}, theirs)
        self.assertEqual(s.owner, old.owner)
        self.assertEqual([len(ours[l]) for l in "ABCDE"], [3, 1, 2, 0, 1])
        self.assertEqual(ours["A"][0]["recorded_label"], "2029-05-23 19:54:04")
        self.assertEqual(ours["A"][2]["recorded_label"], "undated")
        self.assertEqual(ours["B"][0]["recorded_label"], "2029-02-31 11:12:13")
        self.assertEqual([r["recorded_sort"] for r in ours["A"]], ["2029-05-23 19:54:04"] * 2 + [""])
        self.assertTrue(ours["C"][0]["problem"])
        old.close()

    def test_downloads_match_today(self):
        s, dev = self.adapter()
        old_dev = Device()
        old = today(old_dev)
        for letter in "ABCDE":
            old.messages(letter)             # the same requests in the same order
            for r in s.recordings(letter):
                with self.subTest(letter=letter, number=r["number"]):
                    ours, theirs = s.download(letter, r["number"]), old.download(letter, r["number"])
                    self.assertEqual((ours.label, ours.filename, ours.data, ours.error),
                                     (theirs.label, theirs.name, theirs.dvf, theirs.error))
        self.assertEqual(s.download("A", 3).filename, "001_A_003_Casey.dvf")          # undated
        self.assertEqual(s.download("B", 1).filename, "001_B_001_Unknown_2029_02_31.dvf")  # as stored
        self.assertEqual(s.download("C", 1).error, "no address range found for slot 0")
        self.assertEqual(dev.frames, old_dev.frames)
        self.assertEqual(dev.voice_calls, old_dev.voice_calls)
        old.close()

    def test_command_sequence_and_folder_reselection_match_today(self):
        s, dev = self.adapter()
        old_dev = Device()
        old = today(old_dev)
        for session, (listing, download) in ((s, (s.recordings, s.download)),
                                             (old, (old.messages, old.download))):
            listing("A")
            listing("B")                 # the recorder's current folder is now B
            download("A", 2)             # so A is read again before GET_VOICE
            download("A", 2)             # a second request is served from the session's cache
            listing("A")
        self.assertEqual(dev.frames, old_dev.frames)
        self.assertEqual(dev.voice_calls, [(1, 2)])
        ops = [struct.unpack(">I", f[12:16])[0] for f in dev.frames]
        self.assertEqual(ops[:4], [policy.CMD_DEVICE_INFO, policy.CMD_READ_BLOCK, policy.CMD_READ_BLOCK,
                                   policy.CMD_INFO_03])
        self.assertEqual(ops[5:], [policy.CMD_FOLDER_INFO | 1 << 8, policy.CMD_FOLDER_INFO | 2 << 8,
                                   policy.CMD_FOLDER_INFO | 1 << 8, policy.CMD_GET_VOICE])
        old.close()

    def test_close_releases_the_device_once(self):
        s, dev = self.adapter()
        s.close()
        self.assertTrue(dev.closed)
        s.close()
        with self.assertRaises(base.NotReady):
            s.recordings("A")

    def test_unknown_folders_and_numbers(self):
        s, _ = self.adapter()
        for folder in ("AB", "", "a", "F", 1, None):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                s.recordings(folder)
        for number in (0, 4, True, "1", 1.0, None):
            with self.subTest(number=number), self.assertRaises(ValueError):
                s.download("A", number)

    def test_no_transfer_method_is_exposed(self):
        s, _ = self.adapter()
        self.assertEqual(sorted(a for a in dir(s) if not a.startswith("_")),
                         ["close", "download", "folders", "owner", "recordings"])


class ErrorTests(unittest.TestCase):
    """st25's errors reach the app as the shared vocabulary, and state_for()
    gives the device state (and closes the session) exactly as today's
    DeviceManager does for the original exception."""

    FAILURES = [DriverMissing("the recorder on USB port 1-4 does not have the WinUSB driver"),
                UsbError("bulk IN failed: LIBUSB_ERROR_IO after 0 bytes"),
                RecorderError("recorder not ready (status 0f01aaaa)"),
                RecorderStuck("data stopped after 0 of 10 bytes"),
                TableError("message list contains a slot twice"),
                ValueError("no recording A-009")]

    @staticmethod
    def today_outcome(exc):
        """(state, message, session closed) as v0.7.2's DeviceManager left a
        recorder after exc (frozen: it caught st25's exceptions itself)."""
        if isinstance(exc, DriverMissing):
            return app_devices.NEEDS_DRIVER, str(exc), False
        if isinstance(exc, (RecorderError, UsbError, TableError)):
            return app_devices.NEEDS_REPLUG, str(exc), True
        return app_devices.READY, "", False

    def manager_outcome(self, exc):
        """(state, message, session closed) as the app's DeviceManager leaves a
        recorder whose st25 session raised exc (through the adapter)."""
        inner = mock.Mock(spec=RecorderSession)
        inner.owner = ""
        inner.messages.side_effect = exc
        device = base.DiscoveredDevice(PORT_ID, "sony-icd-st25", "port 1-4", locator=PORT_ID)
        m = app_devices.DeviceManager(lambda: ([device], []), lambda model, d: sony_st25.ST25Session(inner))
        self.addCleanup(m.close)
        m.refresh()
        with self.assertRaises(Exception):
            m.with_session(PORT_ID, lambda s: s.recordings("A"))
        [row] = m.refresh()
        return row["state"], row["message"], inner.close.called

    def test_the_app_leaves_recorders_as_today(self):
        for exc in self.FAILURES:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self.manager_outcome(exc), self.today_outcome(exc))

    def test_errors_map_to_todays_states(self):
        for exc in self.FAILURES:
            with self.subTest(exc=type(exc).__name__):
                inner = mock.Mock(spec=RecorderSession)
                inner.messages.side_effect = exc
                s = sony_st25.ST25Session(inner)
                with self.assertRaises(Exception) as caught:
                    s.recordings("A")
                got = caught.exception
                state, close = base.state_for(got)
                old_state, old_message, old_closed = self.today_outcome(exc)
                self.assertEqual(state or base.READY, old_state)
                self.assertEqual(close, old_closed)
                self.assertEqual(str(got), str(exc))
                if type(exc) is ValueError:        # (TableError is a ValueError too)
                    self.assertIs(got, exc)
                else:
                    self.assertIsInstance(got, base.RecorderError)
                    self.assertIs(got.__cause__, exc)
                    self.assertEqual(str(got), old_message)

    def test_counter_mismatch_is_a_recorder_error_as_today(self):
        plugged = Plugged(self, make_device=lambda: Device(skew=1))
        model = recorders.get("sony-icd-st25")
        s = model.open(model.discover()[0])
        self.addCleanup(s.close)
        old = today(Device(skew=1))
        with self.assertRaises(RecorderError) as theirs:
            old.download("A", 1)
        old.close()
        with self.assertRaises(base.RecorderError) as ours:
            s.download("A", 1)
        self.assertEqual(str(ours.exception), str(theirs.exception))
        self.assertEqual(base.state_for(ours.exception), (base.NEEDS_REPLUG, True))
        self.assertEqual(len(plugged.devices), 1)

    def test_open_failures(self):
        model = recorders.get("sony-icd-st25")
        device = base.DiscoveredDevice(PORT_ID, model.model_id, "port 1-4", locator=PORT_ID)
        for exc, kind, state in ((DriverMissing("no WinUSB"), base.DriverMissing, base.NEEDS_DRIVER),
                                 (UsbError("the recorder on USB port 1-4 is no longer connected"),
                                  base.RecorderError, base.NEEDS_REPLUG)):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("st25.protocol.Device", side_effect=exc), self.assertRaises(kind) as caught:
                model.open(device)
            self.assertEqual(str(caught.exception), str(exc))
            self.assertEqual(base.state_for(caught.exception)[0], state)

    def test_a_failed_connect_releases_the_device(self):
        class Silent(Device):
            def control_in(self, *args):
                raise UsbError("control IN request 0x01 failed: LIBUSB_ERROR_PIPE")

        plugged = Plugged(self, make_device=Silent)
        model = recorders.get("sony-icd-st25")
        with self.assertRaises(base.RecorderError):
            model.open(model.discover()[0])
        self.assertTrue(plugged.devices[0].closed)


class DiscoveryTests(unittest.TestCase):
    A = "USB\\VID_054C&PID_0103\\5&38E97A59&0&7"
    B = "USB\\VID_054C&PID_0103\\5&38E97A59&0&3"

    def setUp(self):
        self.model = recorders.get("sony-icd-st25")

    def test_other_devices_never_count_as_an_st25(self):
        """Only the ST25's own instances are matched: another device (another
        recorder model, or the ST25's composite interfaces) is neither a
        placeholder nor subtracted from the ST25's count."""
        other = r"USB\VID_1234&PID_5678\SERIAL1"
        interface = r"USB\VID_054C&PID_0103&MI_00\6&1&0"
        Plugged(self, ids=[], instances=[other, interface, self.A])
        self.assertEqual([d.connection_id for d in self.model.discover()], ["setup:" + self.A])
        Plugged(self, ids=[PORT_ID], instances=[other, self.A])
        self.assertEqual([d.connection_id for d in self.model.discover()], [PORT_ID])

    def test_the_placeholder_goes_to_the_instance_without_the_driver(self):
        """A6 / Astra: recorder A works, B has no driver: B (not the first sorted
        instance) is the setup placeholder, with today's message and format."""
        Plugged(self, ids=[PORT_ID], instances=[self.A, self.B],
                drivers={self.A: ("WinUSB", 0), self.B: ("", 28)})
        found = self.model.discover()
        self.assertEqual([d.connection_id for d in found], [PORT_ID, "setup:" + self.B])
        self.assertEqual((found[1].location, found[1].state, found[1].message),
                         ("", base.NEEDS_DRIVER, TODAY_SETUP_MESSAGE))
        Plugged(self, ids=[PORT_ID], instances=[self.A, self.B],
                drivers={self.A: ("", 0), self.B: ("WinUSB", 0)})
        self.assertEqual([d.connection_id for d in self.model.discover()], [PORT_ID, "setup:" + self.A])

    def test_a_recorder_on_libusbk_or_libusb0_gets_no_placeholder(self):
        """R11: an ST25 Zadig bound to libusbK or libusb0 is usable, as in v0.7.2."""
        for service in ("libusbK", "libusb0", "WinUSB"):
            with self.subTest(service=service):
                Plugged(self, ids=[PORT_ID], instances=[self.A], drivers={self.A: (service, 0)})
                self.assertEqual([d.connection_id for d in self.model.discover()], [PORT_ID])

    def test_ids_and_setup_placeholders_as_today(self):
        Plugged(self, ids=[PORT_ID], instances=[self.A, self.B])
        found = self.model.discover()
        self.assertEqual([d.connection_id for d in found], [PORT_ID] + needs_setup([self.A, self.B], 1))
        usable, setup = found
        self.assertEqual((usable.model_id, usable.location, usable.state, usable.message, usable.locator),
                         ("sony-icd-st25", "port 1-4", base.READY, "", PORT_ID))
        self.assertEqual((setup.location, setup.state, setup.message),
                         ("", base.NEEDS_DRIVER, TODAY_SETUP_MESSAGE))
        with self.assertRaises(base.DriverMissing) as caught:
            self.model.open(setup)
        self.assertEqual(str(caught.exception), TODAY_SETUP_MESSAGE)
        self.assertEqual(base.state_for(caught.exception), (base.NEEDS_DRIVER, False))

    def test_the_unplugged_message_is_v072s(self):
        """The app's message for a recorder that went away, word for word as in v0.7.2."""
        plugged = Plugged(self, ids=[PORT_ID], instances=[])
        self.assertEqual(self.model.discover()[0].where, "on USB port 1-4")
        m = app_devices.DeviceManager()                     # the real discovery, through the registry
        self.addCleanup(m.close)
        self.assertEqual([r["id"] for r in m.refresh()], [PORT_ID])
        plugged.ids = []
        m.refresh()
        with self.assertRaises(app_devices.DeviceGone) as caught:
            m.with_session(PORT_ID, lambda s: 1)
        self.assertEqual(str(caught.exception), "the recorder on USB port 1-4 was unplugged")

    def test_nothing_plugged_in(self):
        Plugged(self, ids=[], instances=[])
        self.assertEqual(self.model.discover(), [])

    def test_through_the_registry(self):
        Plugged(self, ids=[PORT_ID], instances=[self.A])
        found, problems = recorders.discover_all()
        self.assertEqual([(d.connection_id, d.model_id) for d in found], [(PORT_ID, "sony-icd-st25")])
        self.assertEqual(problems, [])

    def test_a_libusb_failure_is_a_problem_not_a_crash(self):
        with mock.patch("openevp.recorders.sony_st25.list_devices", side_effect=UsbError("no libusb")):
            found, problems = recorders.discover_all()
        self.assertEqual(found, [])
        [(model_id, exc)] = problems
        self.assertEqual((model_id, str(exc)), ("sony-icd-st25", "no libusb"))
        self.assertIsInstance(exc, base.RecorderError)

    def test_another_models_device_is_refused(self):
        with self.assertRaises(ValueError):
            self.model.open(base.DiscoveredDevice(PORT_ID, "fake-alpha", "", locator=PORT_ID))


class PlaceholderTests(unittest.TestCase):
    def test_st10_and_rrdr60_are_registered_but_not_supported(self):
        for model_id, name in (("sony-icd-st10", "Sony ICD-ST10"), ("panasonic-rr-dr60", "Panasonic RR-DR60")):
            with self.subTest(model_id=model_id):
                m = recorders.get(model_id)
                self.assertEqual(m.name, name)
                self.assertIs(m.supported, False)
                self.assertEqual((tuple(m.usb_ids), m.needs_winusb, m.native), ((), False, None))
                self.assertNotIn(m, recorders.supported())
                self.assertEqual(m.discover(), [])
                with self.assertRaises(base.RecorderError):
                    m.open(base.DiscoveredDevice("x", model_id, ""))
        self.assertEqual([m.model_id for m in recorders.MODELS],
                         ["sony-icd-st25", "sony-icd-st10", "panasonic-rr-dr60"])


if __name__ == "__main__":
    unittest.main()
