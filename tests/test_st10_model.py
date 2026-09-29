"""The Sony ICD-ST10 as a recorder model: found and opened through the ICD-ST25
model (same USB id and protocol), shown as an ST10 once its session says so,
recordings listed and downloaded as LPEC ST .dvf files and played through the
LPEC ST decoder (not playable, with the reason, in a build without it)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fixtures import DATE, ST_FRAME, FakeRecorderDevice, make_st_raw, st_audio_frames  # noqa: E402
from recorder_contract import RecorderContract  # noqa: E402
from app.devices import READY, DeviceManager  # noqa: E402
from openevp import formats, recorders  # noqa: E402
from openevp.recorders import base  # noqa: E402
from openevp.recorders.sony_st25 import ST25Session, SonyST25  # noqa: E402
from st25 import dvf  # noqa: E402
from st25.protocol import PID, VID, Recorder  # noqa: E402
from st25.session import RecorderSession  # noqa: E402

PORT_ID = "1-4@7"
UNDATED = b"\xff" * 8
# Two recordings of generated LPEC ST frames that decode (not recorded audio):
# the second is the first 20 frames of the first (so other audio).
FRAMES = [st_audio_frames(), st_audio_frames()[:20 * ST_FRAME]]
ST_MISSING = "LPEC ST (ICD-ST10) playback is not included in this build"


def without_st_decoder():
    """A build without the LPEC ST decoder (the LP one is still there)."""
    return mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec_st": None})


def st_estimate(frames):
    """The listed length: every whole frame but the first (st25.dvf.seconds)."""
    return round((len(frames) // ST_FRAME - 1) * 2048 / 44100, 1)


def st10_folders():
    """(folders, voice) of an emulated ICD-ST10: folder A holds two LPEC ST recordings."""
    msgs, voice = [], {}
    for n, frames in enumerate(FRAMES, 1):
        raw = make_st_raw(frames)
        blocks = len(raw) // 1056
        msgs.append((n - 1, 0xFFFFFFFF, 0x180000 + 0x10000 * n, len(frames) + 10 * blocks, UNDATED, "", 0x6C))
        voice[(1, n)] = raw
    return {1: msgs}, voice


def st10_device():
    folders, voice = st10_folders()
    return FakeRecorderDevice(folders, voice=voice, identify="ICD-ST10")


def st10_session():
    r = Recorder.__new__(Recorder)
    r.dev = st10_device()
    s = RecorderSession(r)
    s.connect()
    return ST25Session(s)


class ST10Contract(RecorderContract, unittest.TestCase):
    """The shared contract, with the ICD-ST10 discovered as its own model (in the
    app the ST25 model discovers it: see ManagerTests)."""

    def setUp(self):
        self.model = recorders.get("sony-icd-st10")
        found = base.DiscoveredDevice(PORT_ID, self.model.model_id, "port 1-4", locator=PORT_ID)
        for target, value in ((self.model, "discover"), ):
            patcher = mock.patch.object(target, value, lambda: [found])
            patcher.start()
            self.addCleanup(patcher.stop)

        def device(vid, pid, device_id):
            assert (vid, pid, device_id) == (VID, PID, PORT_ID)
            return st10_device()
        patcher = mock.patch("st25.protocol.Device", device)
        patcher.start()
        self.addCleanup(patcher.stop)


class ModelTests(unittest.TestCase):
    def test_registered_and_supported_without_usb_ids_or_driver(self):
        m = recorders.get("sony-icd-st10")
        self.assertEqual((m.name, m.supported, tuple(m.usb_ids), m.needs_winusb), ("Sony ICD-ST10", True, (), False))
        self.assertIs(m.native, formats.DVF)
        self.assertIn(m, recorders.supported())
        self.assertEqual(m.discover(), [])
        self.assertIs(recorders.find(VID, PID), recorders.get("sony-icd-st25"))
        self.assertEqual(m.wav_problem(), formats.codec_problem(formats.CODEC_ST))
        with without_st_decoder():
            self.assertEqual(m.wav_problem(), ST_MISSING)
            self.assertIsNone(recorders.get("sony-icd-st25").wav_problem())     # the LP decoder is there

    def test_the_st25_keeps_its_wav_problem(self):
        st25 = recorders.get("sony-icd-st25")
        self.assertEqual(st25.wav_problem(), formats.decoder_problem(formats.DVF))

    def test_session_reports_the_model_it_identifies_as(self):
        s = st10_session()
        self.addCleanup(s.close)
        self.assertEqual(s.model_id, "sony-icd-st10")
        rows = s.recordings("A")
        self.assertEqual([(r["number"], r["recorded_label"], r["owner"], r["problem"]) for r in rows],
                         [(1, "undated", "", ""), (2, "undated", "", "")])
        self.assertEqual(rows[0]["seconds"], st_estimate(FRAMES[0]))
        dl = s.download("A", 1)
        self.assertEqual(dl.filename, "001_A_001_Unknown.dvf")
        self.assertIsNone(dvf.validate(dl.data))
        self.assertEqual(dvf.payload(dl.data), FRAMES[0])

    def test_unknown_or_missing_identity_opens_as_an_st25(self):
        for identify in ("", "ICD-ST99", "ICD-ST25"):
            with self.subTest(identify=identify):
                r = Recorder.__new__(Recorder)
                r.dev = FakeRecorderDevice({1: [(0, 100, 0x1000, 2958, DATE, "Casey")]}, identify=identify)
                s = RecorderSession(r)
                s.connect()
                session = ST25Session(s)
                self.assertEqual(session.model_id, "sony-icd-st25")
                self.assertEqual(session.download("A", 1).error, "")

    def test_an_unknown_mode_is_a_listed_problem(self):
        r = Recorder.__new__(Recorder)
        r.dev = FakeRecorderDevice({1: [(0, 1, 0x1000, 3000, DATE, "X", 0x33)]}, identify="ICD-ST10")
        s = RecorderSession(r)
        s.connect()
        [row] = ST25Session(s).recordings("A")
        self.assertIsNone(row["seconds"])
        self.assertIn("0x33", row["problem"])


def st25_discovered(device_id):
    port = device_id.split("@")[0]
    return base.DiscoveredDevice(device_id, SonyST25.model_id, "port " + port, locator=device_id,
                                 where="on USB port " + port)


class ManagerTests(unittest.TestCase):
    """The device manager with an ICD-ST10 discovered by the ST25 model."""

    def setUp(self):
        self.present, self.problems = [PORT_ID], []
        self.opened = []

        def open_device(model, device):
            self.opened.append(model.model_id)
            return st10_session()
        self.m = DeviceManager(lambda: ([st25_discovered(i) for i in self.present], list(self.problems)),
                               open_device)
        self.addCleanup(self.m.close)

    def test_shown_as_an_st10_once_opened(self):
        [row] = self.m.refresh()
        self.assertEqual((row["model_id"], row["model"]), ("sony-icd-st25", "Sony ICD-ST25"))   # not opened yet
        self.m.with_session(PORT_ID, lambda s: s.folders())
        [row] = self.m.refresh()
        self.assertEqual((row["model_id"], row["model"], row["state"]), ("sony-icd-st10", "Sony ICD-ST10", READY))
        self.assertEqual(self.m.model(PORT_ID).model_id, "sony-icd-st10")

    def test_reopened_by_the_st25_model(self):
        self.m.refresh()
        self.m.with_session(PORT_ID, lambda s: s.folders())
        with self.assertRaises(base.RecorderError):
            self.m.with_session(PORT_ID, self.fail_once)
        self.m.with_session(PORT_ID, lambda s: s.folders())       # a new session, opened as before
        self.assertEqual(self.opened, ["sony-icd-st25", "sony-icd-st25"])
        self.assertEqual(self.m.model(PORT_ID).model_id, "sony-icd-st10")

    @staticmethod
    def fail_once(session):
        raise base.RecorderError("data stopped")

    def test_kept_when_the_st25_discovery_fails(self):
        self.m.refresh()
        self.m.with_session(PORT_ID, lambda s: s.folders())
        self.present, self.problems = [], [("sony-icd-st25", RuntimeError("no libusb"))]
        rows, failed = self.m.refresh(problems=True)
        self.assertEqual([(r["id"], r["model_id"]) for r in rows], [(PORT_ID, "sony-icd-st10")])
        self.assertEqual(self.opened, ["sony-icd-st25"])        # the session was kept, not reopened
        self.m.with_session(PORT_ID, lambda s: s.folders())
        self.assertEqual(self.opened, ["sony-icd-st25"])

    def test_gone_when_unplugged(self):
        self.m.refresh()
        self.m.with_session(PORT_ID, lambda s: s.folders())
        self.present = []
        self.assertEqual(self.m.refresh(), [])

    def test_a_session_naming_an_unknown_or_placeholder_model_keeps_the_opener(self):
        for model_id in ("no-such-model", "panasonic-rr-dr60", ""):
            with self.subTest(model_id=model_id):
                session = mock.Mock(model_id=model_id, owner=None)
                m = DeviceManager(lambda: ([st25_discovered(PORT_ID)], []), lambda model, device: session)
                self.addCleanup(m.close)
                m.refresh()
                m.with_session(PORT_ID, lambda s: None)
                self.assertEqual(m.model(PORT_ID).model_id, "sony-icd-st25")


if __name__ == "__main__":
    unittest.main()
