"""The app driven end to end through the recorder interface: the device manager,
the backend API and the library, with the fake models of tests/fakes (one with
a decoder, one without and with awkward ids), the ST25 on its fixture recorder
next to them, reconnects, a model whose discovery fails, and shutdown."""
import io
import os
import sys
import tempfile
import threading
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fakes import fake_models  # noqa: E402
from fakes.fake_models import FakeFolder, FakeRecording, alpha_bytes  # noqa: E402
from fixtures import DATE, FakeRecorderDevice  # noqa: E402
from app import backend  # noqa: E402
from app.devices import NEEDS_DRIVER, NEEDS_REPLUG, READY, DeviceManager  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import formats, recorders, wavinfo  # noqa: E402
from openevp.recorders import base  # noqa: E402
from openevp.recorders.sony_st25 import ST25Session  # noqa: E402
from st25.protocol import Recorder  # noqa: E402
from st25.session import RecorderSession  # noqa: E402

WAIT = 10
ST25_ID = "1-4@7"
ST25_FOLDERS = {1: [(0, 100, 0x1000, 2958, DATE, "Casey")]}


class Server:
    """Like the real AudioServer: caches decodes by key, fingerprints them, forgets a device."""

    def __init__(self):
        self.cache, self.made, self.forgotten = {}, [], []

    def prepare(self, key, make=None):
        if key not in self.cache:
            wav = make()
            self.made.append(key)
            with wave.open(io.BytesIO(wav)) as w:
                duration = w.getnframes() / w.getframerate()
            self.cache[key] = {"url": "http://x/a.wav", "peaks": [0.5], "duration": duration, "rate": 8000,
                               "fp": wavinfo.wav_fingerprint(io.BytesIO(wav))}
        return dict(self.cache[key])

    def prepare_file(self, path):
        with wave.open(path) as w:
            duration = w.getnframes() / w.getframerate()
        return {"url": "http://x/f.wav", "peaks": [0.1], "duration": duration, "rate": 8000,
                "fp": wavinfo.wav_fingerprint(path), "stat": (os.stat(path).st_size, os.stat(path).st_mtime_ns)}

    def forget(self, device_id):
        self.forgotten.append(device_id)
        for key in [k for k in self.cache if k[0] == device_id]:
            del self.cache[key]


class Events:
    def __init__(self):
        self.items, self.cond = [], threading.Condition()

    def __call__(self, name, payload):
        with self.cond:
            self.items.append((name, payload))
            self.cond.notify_all()

    def wait(self, *names):
        """The first event named so that was not waited for before."""
        with self.cond:
            ok = self.cond.wait_for(lambda: any(n in names for n, _ in self.items), WAIT)
            assert ok, f"no {names} event"
            i = next(i for i, (n, _) in enumerate(self.items) if n in names)
            return self.items.pop(i)


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.alpha, self.beta = fake_models.install(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = os.path.join(tmp.name, "save")
        self.store = AppData(os.path.join(tmp.name, "appdata"))
        self.addCleanup(self.store.close)
        self.server = Server()
        self.events = Events()
        self.st25 = []                          # fixture ST25 connection ids attached (see discover)
        self.m = DeviceManager(self.discover, self.open_device, on_removed=self.server.forget)
        self.addCleanup(self.m.close)
        self.api = backend.Api(self.m, self.events, lambda start: None, self.dest, self.server, store=self.store)
        self.addCleanup(self.api.shutdown)      # runs before m.close: the app's own order

    # The real registry discovery (fake models), plus fixture ST25s when attached.
    def discover(self):
        found, problems = recorders.discover_all()
        return found + [base.DiscoveredDevice(i, "sony-icd-st25", "port " + i.split("@")[0], locator=i,
                                           where="on USB port " + i.split("@")[0])
                        for i in self.st25], problems

    def open_device(self, model, device):
        if model.model_id != "sony-icd-st25":
            return model.open(device)
        r = Recorder.__new__(Recorder)
        r.dev = FakeRecorderDevice(ST25_FOLDERS)
        s = RecorderSession(r)
        s.connect()
        return ST25Session(s)

    def rows(self):
        r = self.api.devices()
        self.assertTrue(r["ok"], r)
        return {d["id"]: d for d in r["devices"]}

    def export(self, device_id, items, fmt):
        r = self.api.export(device_id, items, fmt, self.dest, 1)
        self.assertTrue(r["ok"], r)
        return self.events.wait("export-done", "export-failed")

    def files(self, *parts):
        path = os.path.join(self.dest, *parts)
        return sorted(os.listdir(path)) if os.path.isdir(path) else []

    def read(self, *parts):
        with open(os.path.join(self.dest, *parts), "rb") as f:
            return f.read()


class FakeAlphaTests(AppTestBase):
    """A recorder with its own format and a decoder."""

    def setUp(self):
        super().setUp()
        self.dev_id = self.alpha.plug()
        self.device = self.alpha.devices[self.dev_id]
        self.rows()                             # the page polls devices() first

    def test_listed_with_its_model_name_and_owner(self):
        row = self.rows()[self.dev_id]
        self.assertEqual((row["model_id"], row["model"], row["port"], row["state"], row["owner"]),
                         ("fake-alpha", "Fake Alpha", "USB port 9", READY, ""))
        r = self.api.recordings(self.dev_id)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.rows()[self.dev_id]["owner"], "Test Owner")
        self.assertEqual((r["model"], r["playable"], r["play_reason"]), ("Fake Alpha", True, None))
        self.assertEqual([(f["id"], f["label"], len(f["recordings"])) for f in r["folders"]],
                         [("1", "Voice 1", 2), ("2", "Voice 2", 1), ("3", "Voice 3", 0)])
        first = r["folders"][0]["recordings"][0]
        self.assertEqual(first, {"number": 1, "label": "Voice 1-001", "recorded": "2026-09-01 21:01",
                                 "seconds": 0.5, "owner": "Test Owner", "problem": None})
        self.assertEqual(r["formats"], [
            {"value": "fk1", "label": ".fk1 (Fake Alpha original)", "available": True, "reason": None},
            {"value": "wav", "label": "WAV", "available": True, "reason": None}])

    def test_play(self):
        r = self.api.audio(self.dev_id, "1", 2)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["fp"])
        self.assertEqual(self.server.made, [(self.dev_id, "1", 2)])
        src = self.api._entry(r["rec"])["source"]
        self.assertEqual((src["model"], src["format"], src["folder"], src["safe_name"], src["number"], src["label"]),
                         ("fake-alpha", fake_models.FK1, "1", "Voice 1", 2, "Voice 1-002"))
        self.assertEqual((src["native"], src["native_name"]),
                         (self.device.folders[0].recordings[1].data, "ALPHA_1_002.fk1"))
        self.assertFalse(self.api.audio(self.dev_id, "1", "2")["ok"])          # "2" is not the recording 2
        self.assertFalse(self.api.audio(self.dev_id, "9", 1)["ok"])
        self.assertFalse(self.api.audio(self.dev_id, "1", True)["ok"])

    def test_export_native_then_wav_then_again(self):
        items = [{"folder": "1", "number": 1}, {"folder": "2", "number": 1}]
        event, p = self.export(self.dev_id, items, "fk1")
        self.assertEqual((event, p["saved"], p["skipped"], p["notes"]), ("export-done", 2, 0, []))
        self.assertEqual(self.files("Voice 1"), ["ALPHA_1_001.fk1"])
        self.assertEqual(self.files("Voice 2"), ["ALPHA_2_001.fk1"])
        self.assertEqual(self.read("Voice 1", "ALPHA_1_001.fk1"), self.device.folders[0].recordings[0].data)
        event, p = self.export(self.dev_id, items, "wav")
        self.assertEqual((event, p["saved"]), ("export-done", 2))
        self.assertEqual(self.files("Voice 1"), ["ALPHA_1_001.fk1", "ALPHA_1_001.wav"])
        wav = self.read("Voice 1", "ALPHA_1_001.wav")
        self.assertEqual(wav, fake_models.AlphaDecoder().to_wav(self.device.folders[0].recordings[0].data))
        event, p = self.export(self.dev_id, items, "fk1")
        self.assertEqual((p["saved"], p["skipped"]), (0, 2))                   # already there
        self.assertFalse(self.api.export(self.dev_id, items, "dvf", self.dest, 2)["ok"])   # not this model's

    def test_marks_backup_and_export_marked(self):
        rec = self.api.audio(self.dev_id, "1", 1)["rec"]
        self.assertTrue(self.api.add_mark(rec, 0.1, 0.2, "A", "hello")["backup_queued"])
        event, p = self.events.wait("backup-done", "backup-failed")
        self.assertEqual(event, "backup-done", p)
        self.assertEqual(p["label"], "Voice 1-001")
        self.assertEqual(self.files("Voice 1"), ["ALPHA_1_001.fk1", "ALPHA_1_001.wav"])
        self.assertEqual(self.read("Voice 1", "ALPHA_1_001.fk1"), self.device.folders[0].recordings[0].data)
        markers = wavinfo.read_markers(os.path.join(self.dest, "Voice 1", "ALPHA_1_001.wav"))
        self.assertEqual([m["note"] for m in markers], ["EVP A: hello"])
        self.alpha.unplug(self.dev_id)                       # the captured bytes are enough from here on
        self.rows()
        r = self.api.export_marked(rec)
        self.assertEqual((r["ok"], r["already"], r["folder_name"]), (True, True, "Voice 1"))

    def test_a_backup_without_captured_bytes_downloads_again_and_checks(self):
        self.api.audio(self.dev_id, "1", 1)
        self.api._natives.clear()
        rec = self.api.audio(self.dev_id, "1", 1)["rec"]    # the server's cached decode
        downloads = self.device.downloads
        self.api.add_mark(rec, 0.1, 0.2, "B", "")
        self.assertEqual(self.events.wait("backup-done", "backup-failed")[0], "backup-done")
        self.assertEqual(self.device.downloads, downloads + 1)
        self.api.audio(self.dev_id, "1", 2)
        self.api._natives.clear()
        rec = self.api.audio(self.dev_id, "1", 2)["rec"]
        self.device.folders[0].recordings[1].data = alpha_bytes([5] * 800)   # not what was marked
        self.api.add_mark(rec, 0.1, 0.2, "B", "")
        event, p = self.events.wait("backup-done", "backup-failed")
        self.assertEqual(event, "backup-failed")
        self.assertIn("no longer the recording that was marked", p["detail"])

    def test_item_errors_let_an_export_continue_and_failures_stop_it(self):
        self.device.folders[0].recordings[1].problem = "damaged"
        self.api.recordings(self.dev_id)
        event, p = self.export(self.dev_id, [{"folder": "1", "number": 2}, {"folder": "1", "number": 1}], "fk1")
        self.assertEqual((event, p["saved"], p["notes"]), ("export-done", 1, ["Voice 1 #2: not saved (damaged)"]))
        self.device.fail_with = base.RecorderError("the fake recorder stopped answering")
        event, p = self.export(self.dev_id, [{"folder": "2", "number": 1}], "fk1")
        self.assertEqual((event, p["error"], p["advice"], p["state"]),
                         ("export-failed", "the fake recorder stopped answering", backend.REPLUG, NEEDS_REPLUG))
        self.assertEqual(self.rows()[self.dev_id]["state"], NEEDS_REPLUG)
        self.assertEqual(self.alpha.open_sessions, set())               # closed: replug advised
        event, p = self.export(self.dev_id, [{"folder": "2", "number": 1}], "fk1")   # a retry opens it again
        self.assertEqual((event, p["saved"]), ("export-done", 1))
        self.assertEqual(self.rows()[self.dev_id]["state"], READY)

    def test_an_unusable_download_name_is_never_written(self):
        for bad in ("..\\evil.fk1", "../evil.fk1", "evil.exe", "CON.fk1", ".fk1"):
            with self.subTest(name=bad):
                self.device.folders[0].recordings[0].filename = bad
                event, p = self.export(self.dev_id, [{"folder": "1", "number": 1}], "fk1")
                self.assertEqual((event, p["saved"]), ("export-done", 0))
                self.assertIn("file name OpenEVP cannot use", p["notes"][0])
        self.assertFalse(os.path.exists(self.dest))                     # checked before anything is written
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["appdata"])

    def test_needs_driver_is_never_opened(self):
        other = self.alpha.plug(fake_models.FakeAlpha.sample_device())
        self.alpha.devices[other].state = NEEDS_DRIVER
        self.alpha.devices[other].message = "no driver for this one"
        row = self.rows()[other]
        self.assertEqual((row["state"], row["message"]), (NEEDS_DRIVER, "no driver for this one"))
        r = self.api.recordings(other)
        self.assertEqual((r["ok"], r["error"], r["advice"], r["state"]),
                         (False, "no driver for this one", backend.DRIVER, NEEDS_DRIVER))
        self.assertEqual(len(self.alpha.open_sessions), 0)
        self.assertTrue(self.api.recordings(self.dev_id)["ok"])             # the other one is fine


class FakeBetaTests(AppTestBase):
    """A recorder without a decoder, with awkward folder ids, labels and numbers."""

    def setUp(self):
        super().setUp()
        self.dev_id = self.beta.plug()
        self.device = self.beta.devices[self.dev_id]
        self.rows()                             # the page polls devices() first

    def test_listed_as_the_model_reports_it(self):
        r = self.api.recordings(self.dev_id)
        self.assertTrue(r["ok"], r)
        self.assertEqual([(f["id"], f["label"]) for f in r["folders"]],
                         [("folder one", "<b>Folder</b> one"), ("Ünïcødé ✓", "Ünïcødé ✓"), ("..", ".."),
                          ("a:b", "a:b")])
        self.assertEqual([x["number"] for x in r["folders"][0]["recordings"]], ["rec:1", "rec 2"])
        self.assertEqual(r["folders"][2]["recordings"][0]["problem"], "this one is damaged")
        self.assertEqual(r["folders"][0]["recordings"][0]["seconds"], None)
        self.assertEqual(self.rows()[self.dev_id]["owner"], "")
        self.assertFalse(r["playable"])
        self.assertIn("cannot convert Fake Beta original (.fk2) files", r["play_reason"])
        self.assertEqual([(f["value"], f["label"], f["available"]) for f in r["formats"]],
                         [("fk2", ".fk2 (Fake Beta original)", True), ("wav", "WAV", False)])

    def test_not_playable_markable_or_convertible_said_plainly(self):
        r = self.api.audio(self.dev_id, "folder one", "rec:1")
        self.assertFalse(r["ok"])
        self.assertIn("Playback: OpenEVP cannot convert", r["error"])
        self.assertEqual(self.server.made, [])
        r = self.api.export(self.dev_id, [{"folder": "folder one", "number": "rec:1"}], "wav", self.dest, 1)
        self.assertFalse(r["ok"])
        self.assertIn("WAV export: OpenEVP cannot convert", r["error"])

    def test_export_native_under_the_safe_names(self):
        items = [{"folder": "folder one", "number": "rec:1"}, {"folder": "folder one", "number": "rec 2"},
                 {"folder": "Ünïcødé ✓", "number": "ü/3"}, {"folder": "..", "number": "4"},
                 {"folder": "a:b", "number": "a:b:5"}]
        event, p = self.export(self.dev_id, items, "fk2")
        self.assertEqual((event, p["saved"], p["notes"]),
                         ("export-done", 4, [".. #4: not saved (this one is damaged)"]))
        self.assertEqual(sorted(os.listdir(self.dest)), ["Unicode", "a_b", "folder one"])
        self.assertEqual(self.files("folder one"), ["rec 1.fk2", "rec 2.fk2"])
        self.assertEqual(self.files("Unicode"), ["ü 3.fk2"])
        self.assertEqual(self.read("a_b", "rec 5.fk2"), b"FK2\0five")

    def test_ids_are_checked_against_the_listing(self):
        for items in ([{"folder": "../x", "number": "rec:1"}], [{"folder": "folder one", "number": "rec"}],
                      [{"folder": "a", "number": "b:5"}], [{"folder": "a:b:5", "number": ""}],
                      [{"folder": "folder one", "number": 1}], [{"folder": ["a"], "number": "rec:1"}],
                      [{"folder": "folder one", "number": 1.5}]):
            with self.subTest(items=items):
                self.assertFalse(self.api.export(self.dev_id, items, "fk2", self.dest, 1)["ok"])
        self.assertFalse(os.path.exists(self.dest))

    def test_a_listing_the_app_cannot_use_is_refused(self):
        for folders in ([FakeFolder("x", "X", "..")], [FakeFolder("x", "X", "a/b")],
                        [FakeFolder("x", "X", "Same"), FakeFolder("y", "Y", "same")],
                        [FakeFolder("x", "X", "x"), FakeFolder("x", "X2", "x2")],
                        [FakeFolder("x", "X", "x", [FakeRecording("1"), FakeRecording("1")])]):
            with self.subTest(folders=[f.safe_name for f in folders]):
                self.device.folders = folders
                r = self.api.recordings(self.dev_id)
                self.assertFalse(r["ok"])
                self.assertIn("OpenEVP cannot use", r["error"])

    def test_library_lists_decoderless_files_with_a_reason(self):
        os.makedirs(os.path.join(self.dest, "folder one"))
        with open(os.path.join(self.dest, "folder one", "rec 1.fk2"), "wb") as f:
            f.write(b"FK2\0one")
        r = self.api.list_library()
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))    # nothing to fingerprint
        [row] = r["files"]
        self.assertEqual((row["name"], row["type"], row["fp"], row["seconds"]), ("rec 1.fk2", "fk2", None, None))
        self.assertIn("can't be played or marked", row["error"])
        path = os.path.join(self.dest, "folder one", "rec 1.fk2")
        self.assertIsNone(self.store.cached_fp(path, os.stat(path).st_size, os.stat(path).st_mtime_ns))
        r = self.api.play_library(row["id"])
        self.assertFalse(r["ok"])
        self.assertIn("Playing .fk2 files: OpenEVP cannot convert", r["error"])
        caps = self.api.capabilities()["formats"]
        self.assertEqual((caps["fk2"]["playable"], caps["fk1"]["playable"], caps["wav"]["playable"]),
                         (False, True, True))


class TogetherTests(AppTestBase):
    def test_st25_and_a_fake_at_once(self):
        self.st25.append(ST25_ID)
        alpha = self.alpha.plug()
        rows = self.rows()
        self.assertEqual({i: r["model"] for i, r in rows.items()}, {alpha: "Fake Alpha", ST25_ID: "Sony ICD-ST25"})
        st25 = self.api.recordings(ST25_ID)
        self.assertEqual([(f["id"], f["label"]) for f in st25["folders"]], [(l, f"Folder {l}") for l in "ABCDE"])
        self.assertEqual(st25["formats"][0]["label"], ".dvf (Sony original)")
        self.assertTrue(self.api.recordings(alpha)["ok"])
        self.assertEqual(self.export(ST25_ID, [{"folder": "A", "number": 1}], "dvf")[1]["saved"], 1)
        self.assertEqual(self.export(alpha, [{"folder": "1", "number": 1}], "fk1")[1]["saved"], 1)
        self.assertEqual(self.files("A"), ["001_A_001_Casey_2029_05_23.dvf"])
        self.assertEqual(self.files("Voice 1"), ["ALPHA_1_001.fk1"])
        self.assertFalse(self.api.export(ST25_ID, [{"folder": "1", "number": 1}], "dvf", self.dest, 3)["ok"])
        self.assertFalse(self.api.export(alpha, [{"folder": "A", "number": 1}], "fk1", self.dest, 3)["ok"])
        # the library lists every registered format (the save folder is the library)
        self.export(alpha, [{"folder": "1", "number": 1}], "wav")
        types = sorted(f["type"] for f in self.api.list_library()["files"])
        self.assertEqual(types, ["dvf", "fk1", "wav"])

    def test_reconnect_is_a_new_connection_and_drops_the_old_ones_caches(self):
        old = self.alpha.plug()
        self.rows()
        self.assertTrue(self.api.audio(old, "1", 1)["ok"])
        self.assertIn(old, self.api._listings)
        self.assertTrue(any(k[0] == old for k in self.api._natives))
        new = self.alpha.reconnect(old)
        self.assertEqual(list(self.rows()), [new])
        self.assertNotIn(old, self.api._listings)
        self.assertFalse(any(k[0] == old for k in self.api._natives))
        self.assertEqual(self.server.forgotten, [old])
        self.assertEqual(self.alpha.open_sessions, set())                # the old session was closed
        r = self.api.audio(old, "1", 1)
        self.assertEqual((r["ok"], r["error"]), (False, "the recorder (USB port 9) was unplugged"))
        self.assertTrue(self.api.audio(new, "1", 1)["ok"])
        self.assertEqual(self.server.made, [(old, "1", 1), (new, "1", 1)])   # decoded afresh

    def test_one_models_discovery_failing_hides_no_other_recorder(self):
        alpha = self.alpha.plug()
        self.beta.plug()
        with mock.patch.object(self.beta, "discover", side_effect=RuntimeError("volume scan failed")):
            r = self.api.devices()
        self.assertEqual([d["id"] for d in r["devices"]], [alpha])
        self.assertEqual(r["problems"], [{"error": "Fake Beta recorders could not be looked for "
                                                   "(RuntimeError: volume scan failed).", "advice": ""}])
        with mock.patch.object(self.beta, "discover", side_effect=base.RecorderError("libusb failed", "Restart.")):
            r = self.api.devices()
        self.assertEqual(r["problems"], [{"error": "libusb failed", "advice": "Restart."}])
        self.assertEqual(len(self.api.devices()["devices"]), 2)
        self.assertEqual(self.api.devices()["problems"], [])

    def test_a_failed_discovery_keeps_that_models_recorders(self):
        alpha = self.alpha.plug()
        self.rows()
        loaded = self.api.audio(alpha, "1", 1)
        failure = base.RecorderError("libusb_init failed: not found")
        with mock.patch.object(self.alpha, "discover", side_effect=failure):
            r = self.api.devices()
        self.assertEqual([(d["id"], d["owner"]) for d in r["devices"]], [(alpha, "Test Owner")])
        self.assertEqual(r["problems"], [{"error": "libusb_init failed: not found", "advice": backend.REPLUG}])
        self.assertIn(alpha, self.api._listings)                          # nothing was dropped
        self.assertTrue(any(k[0] == alpha for k in self.api._natives))
        self.assertEqual((self.server.forgotten, len(self.alpha.open_sessions)), ([], 1))
        again = self.api.audio(alpha, "1", 1)
        self.assertEqual((again["ok"], again["fp"]), (True, loaded["fp"]))
        self.assertEqual(self.server.made, [(alpha, "1", 1)])             # still the cached decode

    def test_not_ready_latches_until_the_recorder_is_replugged(self):
        """A3: NotReady says the session is invalid until a replug, so no new
        session is opened on that connection meanwhile (unlike a RecorderError)."""
        alpha = self.alpha.plug()
        self.rows()
        self.assertTrue(self.api.recordings(alpha)["ok"])
        opens = mock.patch.object(self.alpha, "open", wraps=self.alpha.open).start()
        self.addCleanup(mock.patch.stopall)
        self.api._listings.clear()                                       # the next request goes to the recorder
        self.alpha.devices[alpha].fail_with = base.NotReady("the recorder reset its connection")
        r = self.api.recordings(alpha)
        self.assertEqual((r["ok"], r["error"], r["advice"], r["state"]),
                         (False, "the recorder reset its connection", backend.REPLUG, NEEDS_REPLUG))
        self.assertEqual(self.alpha.open_sessions, set())                 # closed
        for _ in range(2):                                               # polls and retries change nothing
            self.assertEqual(self.rows()[alpha]["state"], NEEDS_REPLUG)
            r = self.api.recordings(alpha)
            self.assertEqual((r["ok"], r["error"], r["state"]),
                             (False, "the recorder reset its connection", NEEDS_REPLUG))
            with self.assertRaises(base.NotReady):
                self.m.with_session(alpha, lambda s: 1)
        self.assertEqual((opens.call_count, self.alpha.open_sessions), (0, set()))
        new = self.alpha.reconnect(alpha)                                # the replug
        self.assertEqual({i: d["state"] for i, d in self.rows().items()}, {new: READY})
        self.assertTrue(self.api.recordings(new)["ok"])
        self.assertEqual(opens.call_count, 1)

    def test_a_recorder_error_is_not_latched(self):
        """Any other RecorderError closes the session and the next request opens
        a new one (the ST25's v0.7.2 behaviour, see test_devices)."""
        alpha = self.alpha.plug()
        self.rows()
        self.alpha.devices[alpha].fail_with = base.RecorderError("data stopped")
        self.assertEqual(self.api.recordings(alpha)["state"], NEEDS_REPLUG)
        self.assertEqual(self.rows()[alpha]["state"], NEEDS_REPLUG)
        self.assertTrue(self.api.recordings(alpha)["ok"])
        self.assertEqual(self.rows()[alpha]["state"], READY)

    def test_a_connection_claimed_by_two_models_is_closed_and_dropped(self):
        """A4 (Astra's probe): a second model claiming an attached recorder's
        connection id makes it ambiguous; it is closed, its caches dropped and
        it cannot be opened, whereas a failed discovery keeps it."""
        alpha = self.alpha.plug()
        other = self.alpha.plug()
        self.rows()
        loaded = self.api.audio(alpha, "1", 1)
        self.assertTrue(loaded["ok"])
        self.assertEqual(len(self.alpha.open_sessions), 1)
        clash = base.DiscoveredDevice(alpha, self.beta.model_id, "Fake Beta volume", locator=alpha)
        with mock.patch.object(self.beta, "discover", return_value=[clash]):
            r = self.api.devices()
            self.assertEqual([d["id"] for d in r["devices"]], [other])
            self.assertEqual(r["problems"], [
                {"error": f"{name} recorders could not be looked for (RejectedConnection: more than one "
                          f"recorder reports the connection id {alpha!r}).", "advice": ""}
                for name in ("Fake Alpha", "Fake Beta")])
            self.assertEqual(self.alpha.open_sessions, set())             # closed
            self.assertEqual(self.server.forgotten, [alpha])             # its decodes dropped
            self.assertNotIn(alpha, self.api._listings)
            self.assertFalse(any(k[0] == alpha for k in self.api._natives))
            r = self.api.recordings(alpha)
            self.assertEqual((r["ok"], r["error"]), (False, "the recorder (USB port 9) cannot be used: "
                                                            "more than one recorder reports its connection"))
            self.assertEqual(self.alpha.open_sessions, set())             # never reopened
            self.assertTrue(self.api.recordings(other)["ok"])            # the other recorder is untouched
        # Even when its own model's discovery fails at the same time, a rejected connection is dropped.
        self.alpha.unplug(alpha)
        third = self.alpha.plug()
        self.rows()
        self.assertTrue(self.api.recordings(third)["ok"])
        clash = base.DiscoveredDevice(third, self.beta.model_id, "", locator=third)
        with mock.patch.object(self.alpha, "discover", side_effect=base.RecorderError("libusb failed")),                 mock.patch.object(self.beta, "discover", return_value=[clash, clash]):
            rows, _problems = self.m.refresh(problems=True)
        self.assertEqual([d["id"] for d in rows], [other])                  # kept: its model failed
        self.assertEqual([s._id for s in self.alpha.open_sessions], [other])  # third's session was closed
        self.assertIn(third, self.server.forgotten)

    def test_an_unknown_model_is_a_plain_note(self):
        stray = base.DiscoveredDevice("stray#1", "no-such-model", "somewhere")
        found = self.discover
        self.discover = lambda: (found()[0] + [stray], found()[1])
        self.m._discover = self.discover
        r = self.api.devices()
        self.assertEqual((r["devices"], r["problems"]), ([], [
            {"error": "Some recorders could not be looked for (ValueError: no recorder model 'no-such-model').",
             "advice": ""}]))

    def test_a_cached_play_does_not_wait_for_the_device_thread(self):
        alpha = self.alpha.plug()
        self.rows()
        self.assertTrue(self.api.audio(alpha, "1", 1)["ok"])
        entered, go = threading.Event(), threading.Event()
        self.addCleanup(go.set)
        busy = threading.Thread(target=self.m.with_session, args=(alpha, lambda s: (entered.set(), go.wait(WAIT))))
        busy.start()
        self.addCleanup(busy.join)
        self.assertTrue(entered.wait(WAIT))                               # e.g. an export's download
        done = []
        player = threading.Thread(target=lambda: done.append(self.api.audio(alpha, "1", 1)))
        player.start()
        player.join(WAIT / 2)
        self.assertEqual([r["ok"] for r in done], [True])
        go.set()

    def test_shutdown_waits_for_the_export_then_the_recorders_close(self):
        alpha = self.alpha.plug()
        self.rows()
        self.api.recordings(alpha)
        entered, go = threading.Event(), threading.Event()
        real = self.m.with_session

        def slow(device_id, fn):
            entered.set()
            go.wait(WAIT)
            return real(device_id, fn)
        self.m.with_session = slow
        self.assertTrue(self.api.export(alpha, [{"folder": "1", "number": 1}, {"folder": "1", "number": 2}],
                                        "fk1", self.dest, 7)["ok"])
        self.assertTrue(entered.wait(WAIT))
        closer = threading.Thread(target=self.api.shutdown)
        closer.start()
        closer.join(0.2)
        self.assertTrue(closer.is_alive())                               # waits for the recording in flight
        self.assertEqual(len(self.alpha.open_sessions), 1)
        go.set()
        closer.join(WAIT)
        self.assertFalse(closer.is_alive())
        event, p = self.events.wait("export-done", "export-failed")
        self.assertEqual((event, p["saved"]), ("export-failed", 1))       # stopped after the first
        self.assertEqual(self.files("Voice 1"), ["ALPHA_1_001.fk1"])
        self.m.close()                                                   # then main.py closes the recorders
        self.assertEqual(self.alpha.open_sessions, set())


if __name__ == "__main__":
    unittest.main()
