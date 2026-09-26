import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice  # noqa: E402
from app import backend  # noqa: E402
from app.devices import NEEDS_REPLUG, DeviceManager  # noqa: E402
from st25.protocol import Recorder  # noqa: E402
from st25.session import RecorderSession  # noqa: E402
from st25.usb import UsbError  # noqa: E402

FOLDERS = {1: [(0, 100, 0x1000, 2958, DATE, "Casey"), (1, 900, 0x3000, 4000, DATE, "Casey")]}
ID = "1-4@7"


class FakeAudioServer:
    def __init__(self):
        self.prepared = []

    def prepare(self, key):
        self.prepared.append(key)
        return {"url": f"http://x/{key[1]}{key[2]}.wav", "peaks": [0.5], "duration": 1.0}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.dev = FakeRecorderDevice(FOLDERS)

        def open_fn(device_id):
            r = Recorder.__new__(Recorder)
            r.dev = self.dev
            s = RecorderSession(r)
            s.connect()
            return s

        self.m = DeviceManager(lambda: [ID], open_fn)
        self.addCleanup(self.m.close)
        self.events = []
        self.done = threading.Event()

        def emit(event, payload):
            self.events.append((event, payload))
            if event in ("export-done", "export-failed"):
                self.done.set()

        self.server = FakeAudioServer()
        self.api = backend.Api(self.m, emit, lambda: None, "DEST", self.server)
        self.addCleanup(self.api.shutdown)
        self.api.devices()

    def wait(self):
        self.assertTrue(self.done.wait(5))
        self.done.clear()
        return self.events[-1]

    def test_setup_driver_reports_success_failure_and_cancel(self):
        from app.driver_setup import SetupCancelled
        calls = []

        def ok():
            calls.append(1)
            return 0, "... DONE"
        api = backend.Api(self.m, self.events.append, lambda: None, "DEST", self.server, driver_setup=ok)
        self.assertEqual(api.setup_driver(), {"ok": True, "restart": False})
        self.assertEqual(calls, [1])

        api = backend.Api(self.m, self.events.append, lambda: None, "DEST", self.server,
                          driver_setup=lambda: (1, "line\n12:00:00 FAILED: pnputil failed with exit code 5"))
        r = api.setup_driver()
        self.assertFalse(r["ok"])
        self.assertIn("pnputil failed", r["error"])

        def cancel():
            raise SetupCancelled("the administrator prompt was declined")
        r = backend.Api(self.m, self.events.append, lambda: None, "DEST", self.server, driver_setup=cancel).setup_driver()
        self.assertFalse(r["ok"])
        self.assertIn("declined", r["error"])

    def test_open_folder_only_opens_folders(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(backend, "open_folder", return_value=True) as op:
            self.assertEqual(self.api.open_folder(d), {"ok": True})
            f = os.path.join(d, "x.exe")
            open(f, "w").close()
            self.assertFalse(self.api.open_folder(f)["ok"])        # a file would be run, never opened
            self.assertFalse(self.api.open_folder(123)["ok"])
            op.assert_called_once_with(d)

    def test_open_wav_uses_only_the_picked_file(self):
        prepared = []

        class Server(FakeAudioServer):
            def prepare_file(self, path):
                prepared.append(path)
                if path.endswith("bad.wav"):
                    raise ValueError("not a PCM WAV file")
                return {"url": "http://x/f.wav", "peaks": [0.1], "duration": 2.0}

        def api_with(pick):
            return backend.Api(self.m, self.events.append, lambda: None, "DEST", Server(), pick_wav=pick)

        self.assertEqual(api_with(lambda start: r"C:\rec\dr60.wav").open_wav(),
                         {"ok": True, "name": "dr60.wav", "url": "http://x/f.wav", "peaks": [0.1], "duration": 2.0})
        self.assertEqual(api_with(lambda start: None).open_wav(), {"ok": False, "cancelled": True})
        r = api_with(lambda start: r"C:\rec\bad.wav").open_wav()
        self.assertFalse(r["ok"])
        self.assertIn("not a PCM WAV", r["error"])
        self.assertFalse(backend.Api(self.m, self.events.append, lambda: None, "DEST", Server()).open_wav()["ok"])
        self.assertEqual(prepared, [r"C:\rec\dr60.wav", r"C:\rec\bad.wav"])

    def test_save_folder_is_tracked_and_both_dialogs_start_in_it(self):
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "OpenEVP")
            other = os.path.join(d, "elsewhere")
            os.mkdir(other)
            starts = []

            def pick(start):
                starts.append(start)
                return None
            api = backend.Api(self.m, self.events.append, pick, dest, self.server, pick_wav=pick)
            api.open_wav()                                   # no save folder yet: start in its parent
            self.assertIsNone(api.choose_destination())      # cancelled: nothing changes
            self.assertFalse(os.path.exists(dest))           # and it is never created just to start there
            os.mkdir(dest)                                    # as the first export would
            api.open_wav()
            api.choose_destination()
            self.assertEqual(api.default_destination(), dest)
            api._pick = lambda start: starts.append(start) or other
            self.assertEqual(api.choose_destination(), other)
            self.assertEqual(api.default_destination(), other)
            api.open_wav()
            self.assertEqual(starts, [d, d, dest, dest, dest, other])

    def test_recordings(self):
        r = self.api.recordings(ID)
        self.assertTrue(r["ok"])
        a = r["folders"][0]
        self.assertEqual(a["letter"], "A")
        self.assertEqual([x["number"] for x in a["recordings"]], [1, 2])
        self.assertEqual(a["recordings"][0]["when"], "2029-05-23 19:54:04")
        self.assertEqual([f["recordings"] for f in r["folders"][1:]], [[], [], [], []])

    def test_export_dvf_then_rerun_skips(self):
        with tempfile.TemporaryDirectory() as d:
            items = [{"folder": "A", "number": 1}, {"folder": "A", "number": 2}]
            self.assertEqual(self.api.export(ID, items, "dvf", d, 1), {"ok": True, "job": 1})
            event, p = self.wait()
            self.assertEqual((event, p["job"], p["saved"], p["skipped"]), ("export-done", 1, 2, 0))
            self.assertEqual(sorted(os.listdir(os.path.join(d, "A"))),
                             ["001_A_001_Casey_2029_05_23.dvf", "001_A_002_Casey_2029_05_23.dvf"])
            self.api.export(ID, items, "dvf", d, 2)
            event, p = self.wait()
            self.assertEqual((p["job"], p["saved"], p["skipped"]), (2, 0, 2))
            self.assertEqual(self.dev.voice_calls, [(1, 1), (1, 2)])       # cached, not downloaded again

    def test_bad_requests_are_rejected_without_locking(self):
        for items in ([], [{"folder": "Z", "number": 1}], [{"folder": 3, "number": 1}], [{"folder": "A", "number": "x"}],
                      [{"folder": "A", "number": 0}], [{"folder": "A"}], "A1", [{"folder": "A", "number": True}]):
            self.assertFalse(self.api.export(ID, items, "dvf", "D", 1)["ok"], items)
        self.assertFalse(self.api.export(ID, [{"folder": "A", "number": 1}], "mp3", "D", 1)["ok"])
        self.assertFalse(self.api.export(ID, [{"folder": "A", "number": 1}], "dvf", "", 1)["ok"])
        self.assertFalse(self.api.exporting())
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self.api.export(ID, [{"folder": "A", "number": 1}], "dvf", d, 3)["ok"])
            self.assertEqual(self.wait()[0], "export-done")

    def test_wav_needs_decoder(self):
        with mock.patch.dict(sys.modules, {"st25.lpec": None}):
            caps = self.api.capabilities()
            self.assertFalse(caps["wav"])
            self.assertIn("not included in this build", caps["wav_status"])
            self.assertFalse(self.api.export(ID, [{"folder": "A", "number": 1}], "wav", "D", 1)["ok"])
            self.assertFalse(self.api.audio(ID, "A", 1)["ok"])
        self.assertEqual(self.server.prepared, [])

    def test_wav_tables_missing_is_reported_not_crashed(self):
        """st25.lpec exists (imports fine) but its extracted table data does
        not: capabilities() must say "could not be loaded", and both wav
        export and live playback must be refused cleanly rather than blowing
        up on the first attempt."""
        class FakeTablesMissing(RuntimeError):
            pass

        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        fake.TablesMissing = FakeTablesMissing
        fake.check = mock.Mock(side_effect=FakeTablesMissing("lpec_tables.json not found"))
        with mock.patch.dict(sys.modules, {"st25.lpec": fake}):
            caps = self.api.capabilities()
            self.assertFalse(caps["wav"])
            self.assertIn("could not be loaded", caps["wav_status"])
            r = self.api.export(ID, [{"folder": "A", "number": 1}], "wav", "D", 1)
            self.assertFalse(r["ok"])
            self.assertIn("could not be loaded", r["error"])
            r = self.api.audio(ID, "A", 1)
            self.assertFalse(r["ok"])
            self.assertIn("could not be loaded", r["error"])
        self.assertEqual(self.server.prepared, [])

    def test_export_wav_and_audio_with_decoder(self):
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with mock.patch.dict(sys.modules, {"st25.lpec": fake}), tempfile.TemporaryDirectory() as d:
            self.api.export(ID, [{"folder": "A", "number": 1}], "wav", d, 1)
            self.assertEqual(self.wait()[0], "export-done")
            self.assertEqual(os.listdir(os.path.join(d, "A")), ["001_A_001_Casey_2029_05_23.wav"])
            self.assertEqual(self.api.audio(ID, "A", 1),
                             {"ok": True, "url": "http://x/A1.wav", "peaks": [0.5], "duration": 1.0})
            self.assertEqual(self.server.prepared, [(ID, "A", 1)])
            self.assertEqual(backend.recording_wav(self.m, (ID, "A", 1))[:4], b"RIFF")

    def test_closing_the_app_interrupts_a_wav_decode(self):
        """The export passes the backend's stop event into the decode: a long
        (pure-Python) decode stops when the app closes instead of running on."""
        class FakeCancelled(Exception):
            pass

        seen = []

        def dvf_to_wav(data, should_stop=None):
            seen.append(should_stop())
            self.api.request_stop()            # the user closes the window mid-decode
            if should_stop():
                raise FakeCancelled("stopped")
            return b"RIFF" + data[:8]
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = dvf_to_wav
        fake.Cancelled = FakeCancelled
        with mock.patch.dict(sys.modules, {"st25.lpec": fake}), tempfile.TemporaryDirectory() as d:
            self.api.export(ID, [{"folder": "A", "number": 1}, {"folder": "A", "number": 2}], "wav", d, 1)
            event, payload = self.wait()
            self.assertEqual(event, "export-failed")
            self.assertIn("closing", payload["error"])
            self.assertEqual(payload["saved"], 0)
            self.assertEqual(seen, [False])
            self.assertEqual(os.listdir(os.path.join(d, "A")), [])

    def test_stuck_recorder_reports_replug(self):
        def stall(ep, dest, offset, max_len, timeout_ms):
            return 0, -7
        self.dev.bulk_in_into = stall
        r = self.api.recordings(ID)
        self.assertFalse(r["ok"])
        self.assertEqual(r["advice"], backend.REPLUG)
        self.assertEqual(r["state"], NEEDS_REPLUG)
        self.assertEqual(self.api.devices()["devices"][0]["state"], NEEDS_REPLUG)

    def test_devices_ok_shape(self):
        r = self.api.devices()
        self.assertTrue(r["ok"])
        self.assertEqual(r["devices"][0]["id"], ID)

    def test_devices_reports_enumeration_failure_without_raising(self):
        def boom():
            raise UsbError("libusb_init failed: not found")
        m = DeviceManager(boom, lambda device_id: None)
        self.addCleanup(m.close)
        api = backend.Api(m, self.events.append, lambda: None, "DEST", self.server)
        self.addCleanup(api.shutdown)
        r = api.devices()
        self.assertFalse(r["ok"])
        self.assertIn("libusb_init failed", r["error"])

    def test_choose_destination_swallows_picker_errors(self):
        def picker(start):
            raise RuntimeError("dialog boom")
        api = backend.Api(self.m, self.events.append, picker, "DEST", self.server)
        self.addCleanup(api.shutdown)
        self.assertIsNone(api.choose_destination())

    def test_request_stop_refuses_new_exports(self):
        self.api.request_stop()
        self.assertFalse(self.api.export(ID, [{"folder": "A", "number": 1}], "dvf", "D", 1)["ok"])

    def test_shutdown_stops_between_recordings_and_refuses_new_exports(self):
        real = self.m.with_session

        def closing_after_first_download(device_id, fn):
            result = real(device_id, fn)
            self.api._stop.set()                   # as if the window closed during recording 1
            return result

        self.m.with_session = closing_after_first_download
        with tempfile.TemporaryDirectory() as d:
            items = [{"folder": "A", "number": 1}, {"folder": "A", "number": 2}]
            self.assertTrue(self.api.export(ID, items, "dvf", d, 5)["ok"])
            event, p = self.wait()
            self.assertEqual((event, p["saved"]), ("export-failed", 1))   # recording 1 finished, 2 not started
            self.assertIn("closing", p["error"])
            self.api.shutdown()
            self.assertFalse(self.api.exporting())
            self.assertFalse(self.api.export(ID, [{"folder": "A", "number": 1}], "dvf", d, 6)["ok"])

    def test_thread_start_failure_releases_lock_and_reports_error(self):
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("boom")):
            r = self.api.export(ID, [{"folder": "A", "number": 1}], "dvf", "D", 1)
        self.assertFalse(r["ok"])
        self.assertFalse(self.api.exporting())


if __name__ == "__main__":
    unittest.main()
