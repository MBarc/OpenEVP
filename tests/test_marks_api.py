"""EVP marks through the Api: recording handles, backups of marked recorder
recordings, WAV exports that carry the marks, marker import, Save-to."""
import hashlib
import io
import os
import sys
import tempfile
import threading
import types
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice, made_wav, st25_manager  # noqa: E402
from app import backend  # noqa: E402
from app.store import AppData, _acquire_lock  # noqa: E402
from openevp import wavinfo  # noqa: E402
from sony_icd.protocol import Recorder  # noqa: E402
from sony_icd.session import RecorderSession  # noqa: E402

FOLDERS = {1: [(0, 100, 0x1000, 2958, DATE, "Casey"), (1, 900, 0x3000, 4000, DATE, "Casey")]}
ID = "1-4@7"
DVF_1 = "001_A_001_Casey_2029_05_23.dvf"
WAV_1 = "001_A_001_Casey_2029_05_23.wav"


def wav_bytes(seed, seconds=1.0, rate=8000):
    """A small valid PCM WAV whose samples depend on seed."""
    n = int(seconds * rate)
    pcm = (hashlib.sha256(seed).digest() * (2 * n // 32 + 1))[:2 * n]
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def fake_decoder(decode=None):
    mod = types.ModuleType("openevp.decoders.sony_lpec")
    mod.dvf_to_wav = decode or (lambda data, should_stop=None: wav_bytes(data))
    return mod


class FakeServer:
    """Caches decodes by key like the real AudioServer and fingerprints them."""

    def __init__(self):
        self.cache = {}
        self.made = 0

    def prepare(self, key, make=None, write=None):
        if key not in self.cache:
            wav = made_wav(make, write)
            self.made += 1
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


class MarksApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.dest = os.path.join(self.tmp, "save")
        self.store = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(self.store.close)
        self.dev = FakeRecorderDevice(FOLDERS)

        def open_fn(device_id):
            r = Recorder.__new__(Recorder)
            r.dev = self.dev
            s = RecorderSession(r)
            s.connect()
            return s

        self.m = st25_manager(lambda: [ID], open_fn)
        self.addCleanup(self.m.close)
        self.sessions = 0
        real = self.m.with_session

        def counting(device_id, fn):
            self.sessions += 1
            return real(device_id, fn)
        self.m.with_session = counting
        self.events = []
        self.event = threading.Event()

        def emit(event, payload):
            self.events.append((event, payload))
            if event.startswith("backup-") or event in ("export-done", "export-failed"):
                self.event.set()

        self.emit = emit
        self.server = FakeServer()
        self.decoder = mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake_decoder()})
        self.decoder.start()
        self.addCleanup(self.decoder.stop)
        self.api = self.new_api()
        self.api.devices()

    def new_api(self, store=None, pick_folder=lambda start: None, pick_wav=None):
        api = backend.Api(self.m, self.emit, pick_folder, self.dest, self.server,
                          pick_wav=pick_wav, store=store or self.store)
        self.addCleanup(api.shutdown)
        return api

    def wait_event(self):
        self.assertTrue(self.event.wait(5), "no event arrived")
        self.event.clear()
        return self.events[-1]

    def load(self, number=1):
        r = self.api.audio(ID, "A", number)
        self.assertTrue(r["ok"], r)
        return r

    # ---- handles and the marks round trip -------------------------------------

    def test_audio_result_carries_a_handle_and_the_marks_state(self):
        r = self.load()
        self.assertRegex(r["rec"], r"^[0-9a-f]{16}$")
        self.assertEqual((r["marks"], r["reviewed"]), ([], False))
        self.assertEqual(r["backup"], {"status": None, "detail": ""})
        self.assertIn("fp", r)
        self.assertNotIn("dvf", str(r.keys()))

    def test_round_trip(self):
        rec = self.load()["rec"]
        added = self.api.add_mark(rec, 0.1, 0.4, "A", "a voice")
        self.assertTrue(added["ok"], added)
        mark_id = added["mark"]["id"]
        got = self.api.get_marks(rec)
        self.assertEqual([(m["id"], m["cls"], m["note"]) for m in got["marks"]], [(mark_id, "A", "a voice")])
        r = self.api.update_mark(rec, mark_id, cls="B", note="two words", start=0.2, end=0.5)
        self.assertEqual((r["ok"], r["mark"]["cls"], r["mark"]["start"]), (True, "B", 0.2))
        self.assertEqual(self.api.set_reviewed(rec, True)["ok"], True)
        again = self.load()                                   # a new handle sees the same marks
        self.assertNotEqual(again["rec"], rec)
        self.assertEqual([m["note"] for m in again["marks"]], ["two words"])
        self.assertTrue(again["reviewed"])
        self.assertEqual(self.api.delete_mark(rec, mark_id), {"ok": True, "deleted": True})
        self.assertEqual(self.api.get_marks(rec)["marks"], [])

    def test_invalid_class_and_unknown_handle_fail_plainly(self):
        rec = self.load()["rec"]
        r = self.api.add_mark(rec, 0.1, 0.4, "D", "")
        self.assertFalse(r["ok"])
        self.assertIn("Class must be one of A, B, C", r["error"])
        self.assertEqual(self.api.get_marks(rec)["marks"], [])
        for bad in ("0123456789abcdef", None, 5):
            r = self.api.get_marks(bad)
            self.assertEqual((r["ok"], r["error"]), (False, "Load the recording again."))

    def test_handles_are_bounded(self):
        first = self.load()["rec"]
        for _ in range(backend.HANDLES):
            self.load()
        self.assertEqual(self.api.get_marks(first)["error"], "Load the recording again.")

    def test_no_store_means_no_marks(self):
        api = backend.Api(self.m, self.emit, lambda s: None, self.dest, self.server)
        self.addCleanup(api.shutdown)
        rec = api.audio(ID, "A", 1)["rec"]
        r = api.add_mark(rec, 0.1, 0.4, "A", "")
        self.assertEqual((r["ok"], r["error"]), (False, "Marks are not available here."))
        self.assertFalse(api.export_marked(rec)["ok"])

    def test_read_only_store_fails_marks_plainly(self):
        second = AppData(os.path.join(self.tmp, "appdata"))      # the first holds the lock
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        self.assertTrue(api.capabilities()["marks_read_only"])
        rec = api.audio(ID, "A", 1)["rec"]
        r = api.add_mark(rec, 0.1, 0.4, "A", "")
        self.assertFalse(r["ok"])
        self.assertIn(f"Another OpenEVP (process {os.getpid()}) is open", r["error"])
        self.assertTrue(api.get_marks(rec)["ok"])                  # reading still works

    def test_read_only_reason_and_store_writable_event(self):
        second = AppData(os.path.join(self.tmp, "appdata"), retry_interval=0.05)   # the first holds the lock
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        caps = api.capabilities()
        self.assertTrue(caps["marks_read_only"])
        self.assertIn(f"Another OpenEVP (process {os.getpid()}) is open", caps["marks_read_only_reason"])
        self.assertIn(caps["marks_read_only_reason"], caps["store_problems"])
        self.assertIsNone(self.api.capabilities()["marks_read_only_reason"])
        got = threading.Event()
        emit = self.emit

        def watching(event, payload):
            emit(event, payload)
            if event == "store-writable":
                got.set()
        api._emit = watching
        api.watch_store()
        self.store.close()                                         # the other window closes
        self.assertTrue(got.wait(5), "no store-writable event")
        caps = api.capabilities()
        self.assertEqual((caps["marks_read_only"], caps["marks_read_only_reason"], caps["store_problems"]),
                         (False, None, []))
        rec = api.audio(ID, "A", 1)["rec"]
        self.assertTrue(api.add_mark(rec, 0.1, 0.4, "A", "")["ok"])
        thread = second._retry_thread
        api.shutdown()
        self.assertFalse(thread.is_alive())

    def test_store_writable_applies_the_remembered_save_folder(self):
        remembered = os.path.join(self.tmp, "remembered")
        picked = os.path.join(self.tmp, "picked")
        os.makedirs(remembered)
        os.makedirs(picked)
        results = {}
        for choose in (False, True):
            first = AppData(os.path.join(self.tmp, f"appdata-{choose}"))
            second = AppData(os.path.join(self.tmp, f"appdata-{choose}"), retry_interval=0.05)
            self.addCleanup(second.close)
            self.addCleanup(first.close)
            got = threading.Event()
            api = self.new_api(store=second, pick_folder=lambda start: picked)
            real = api._emit
            api._emit = lambda e, p: (real(e, p), got.set() if e == "store-writable" else None)
            if choose:
                self.assertEqual(api.choose_destination(), picked)   # not remembered: read-only
            api.watch_store()
            first.set_setting("save_folder", remembered)             # the other window's choice
            first.close()
            self.assertTrue(got.wait(5), "no store-writable event")
            results[choose] = api.default_destination()
        self.assertEqual(results, {False: remembered, True: picked})

    def test_a_pick_made_while_recovery_checks_the_folder_stays(self):
        remembered = os.path.join(self.tmp, "remembered")
        picked = os.path.join(self.tmp, "picked")
        os.makedirs(remembered)
        os.makedirs(picked)
        first = AppData(os.path.join(self.tmp, "appdata-race"))
        first.set_setting("save_folder", remembered)
        second = AppData(os.path.join(self.tmp, "appdata-race"), retry_interval=60)
        self.addCleanup(second.close)
        first.close()
        api = self.new_api(store=second, pick_folder=lambda start: picked)
        real_isdir, raced, busy = os.path.isdir, [], []

        def isdir(path):
            # The user picks a folder just as recovery has looked at the remembered one.
            if path == remembered and not busy:
                busy.append(1)
                raced.append(api.choose_destination())
            return real_isdir(path)
        second._lock_file, second.read_only = _acquire_lock(second._lock_path)[0], False   # recovered
        with mock.patch.object(backend.os.path, "isdir", side_effect=isdir):
            api._store_writable()
        self.assertEqual(raced, [picked])
        self.assertEqual(api.default_destination(), picked)
        self.assertEqual(second.get_setting("save_folder"), picked)   # and it is remembered now

    def test_shutdown_joins_the_store_retry(self):
        second = AppData(os.path.join(self.tmp, "appdata"), retry_interval=60)
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        api.watch_store()
        thread = second._retry_thread
        self.assertTrue(thread.is_alive())
        api.shutdown()
        self.assertFalse(thread.is_alive())
        self.assertNotIn("store-writable", [e for e, _p in self.events])

    def test_capabilities_report_store_problems(self):
        caps = self.api.capabilities()
        self.assertEqual((caps["store_problems"], caps["marks_read_only"], caps["marks"]), ([], False, True))
        api = backend.Api(self.m, self.emit, lambda s: None, self.dest, self.server,
                          store_problems=["Marks are off: the folder could not be created."])
        self.addCleanup(api.shutdown)
        caps = api.capabilities()
        self.assertEqual(caps["store_problems"], ["Marks are off: the folder could not be created."])
        self.assertFalse(caps["marks"])

    # ---- backups -------------------------------------------------------------

    def test_first_mark_backs_up_once_with_the_captured_bytes(self):
        rec = self.load()["rec"]
        captured = self.api._natives[(ID, "A", 1)][0].data
        sessions = self.sessions
        self.assertTrue(self.api.add_mark(rec, 0.1, 0.4, "A", "hello")["ok"])
        event, p = self.wait_event()
        self.assertEqual(event, "backup-done", p)
        self.assertEqual((p["rec"], p["label"]), (rec, "A-001"))
        self.assertIn(DVF_1, p["detail"])
        self.assertNotIn(self.dest, p["detail"])                     # names only, never paths
        self.assertEqual(self.sessions, sessions)                   # never downloaded again
        folder = os.path.join(self.dest, "A")
        self.assertEqual(sorted(os.listdir(folder)), [DVF_1, WAV_1])
        with open(os.path.join(folder, DVF_1), "rb") as f:
            self.assertEqual(f.read(), captured)
        self.assertEqual([m["note"] for m in wavinfo.read_markers(os.path.join(folder, WAV_1))], ["EVP A: hello"])
        self.assertEqual(self.api.get_marks(rec)["backup"], {"status": "saved", "detail": p["detail"]})  # no paths
        fp = self.api._entry(rec)["fp"]
        self.assertEqual(sorted(self.store.backup_record(fp)["paths"]),       # remembered for deleting
                         [os.path.join(os.path.abspath(folder), n) for n in (DVF_1, WAV_1)])
        self.assertTrue(self.api.add_mark(rec, 0.5, 0.7, "B", "")["ok"])   # a second mark: no second backup
        self.api.shutdown()
        self.assertEqual([e for e, _ in self.events if e.startswith("backup")], ["backup-done"])
        self.assertEqual(sorted(os.listdir(folder)), [DVF_1, WAV_1])

    def test_file_recordings_are_not_backed_up(self):
        path = os.path.join(self.tmp, "Hotel", "session.wav")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(wav_bytes(b"file"))
        api = self.new_api(pick_wav=lambda start: path)
        rec = api.open_wav()["rec"]
        r = api.add_mark(rec, 0.1, 0.4, "C", "")
        self.assertEqual((r["ok"], r["backup_queued"]), (True, False))
        api.shutdown()
        self.assertFalse(os.path.exists(self.dest))

    def test_a_missing_capture_is_downloaded_once_and_verified(self):
        self.load()
        self.api._natives.clear()                           # as if evicted while the decode stays cached
        rec = self.load()["rec"]                         # served from the (fake) audio server's cache
        sessions = self.sessions
        self.assertTrue(self.api.add_mark(rec, 0.1, 0.4, "A", "")["ok"])
        event, p = self.wait_event()
        self.assertEqual(event, "backup-done", p)
        self.assertEqual(self.sessions, sessions + 1)
        self.assertIn(DVF_1, os.listdir(os.path.join(self.dest, "A")))

    def test_a_capture_that_no_longer_matches_is_not_backed_up(self):
        self.load()
        self.api._natives.clear()
        rec = self.load()["rec"]
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake_decoder(lambda d, should_stop=None: wav_bytes(b"x"))}):
            self.assertTrue(self.api.add_mark(rec, 0.1, 0.4, "A", "")["ok"])
            event, p = self.wait_event()
        self.assertEqual(event, "backup-failed")
        self.assertIn("no longer", p["detail"])
        self.assertEqual(self.api.get_marks(rec)["backup"]["status"], "failed")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "A", DVF_1)))

    def test_failed_backup_keeps_the_mark_and_retries_on_the_next_mark(self):
        os.makedirs(self.dest)
        blocker = os.path.join(self.dest, "A")
        open(blocker, "w").close()                       # a file where the folder should go
        rec = self.load()["rec"]
        self.assertTrue(self.api.add_mark(rec, 0.1, 0.4, "A", "first")["ok"])
        event, p = self.wait_event()
        self.assertEqual(event, "backup-failed")
        self.assertIn("A-001", p["detail"])
        self.assertNotIn(self.tmp, p["detail"])
        state = self.api.get_marks(rec)
        self.assertEqual([m["note"] for m in state["marks"]], ["first"])
        self.assertEqual(state["backup"]["status"], "failed")
        os.remove(blocker)
        self.assertTrue(self.api.add_mark(rec, 0.5, 0.7, "B", "")["ok"])
        self.assertEqual(self.wait_event()[0], "backup-done")
        self.assertIn(DVF_1, os.listdir(blocker))

    def test_retry_backup(self):
        os.makedirs(self.dest)
        blocker = os.path.join(self.dest, "A")
        open(blocker, "w").close()
        rec = self.load()["rec"]
        self.api.add_mark(rec, 0.1, 0.4, "A", "")
        self.assertEqual(self.wait_event()[0], "backup-failed")
        os.remove(blocker)
        self.assertEqual(self.api.retry_backup(rec), {"ok": True, "queued": True})
        self.assertEqual(self.wait_event()[0], "backup-done")
        self.assertEqual(self.api.retry_backup(rec), {"ok": True, "queued": False})   # already saved

    def test_wav_copy_failure_is_reported_separately(self):
        rec = self.load()["rec"]
        with mock.patch.object(backend, "save_wav", side_effect=OSError(28, "No space left on device",
                                                                       os.path.join(self.dest, "A", WAV_1))):
            self.api.add_mark(rec, 0.1, 0.4, "A", "")
            event, p = self.wait_event()
        self.assertEqual(event, "backup-done")
        self.assertIn("WAV copy failed", p["detail"])
        self.assertIn(WAV_1, p["detail"])
        self.assertNotIn(self.dest, p["detail"])
        self.assertEqual(self.api.get_marks(rec)["backup"]["status"], "saved")
        self.assertEqual(os.listdir(os.path.join(self.dest, "A")), [DVF_1])

    def test_shutdown_joins_the_backup_worker(self):
        entered, go = threading.Event(), threading.Event()
        real = backend.save_native

        def slow(*a):
            entered.set()
            go.wait(5)
            return real(*a)
        rec = self.load()["rec"]
        with mock.patch.object(backend, "save_native", slow):
            self.api.add_mark(rec, 0.1, 0.4, "A", "")
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.api.backing_up())
            self.assertFalse(self.api.install_update()["ok"])
            closer = threading.Thread(target=self.api.shutdown)
            closer.start()
            closer.join(0.3)
            self.assertTrue(closer.is_alive())                  # waiting for the backup
            go.set()
            closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertFalse(self.api.backing_up())
        self.assertIn(DVF_1, os.listdir(os.path.join(self.dest, "A")))
        self.assertTrue(self.store._closed)

    # ---- fix round 1: update, closing, capture pairing, worker bookkeeping ------

    def updating_api(self, during_download):
        """self.api set up to install an update; during_download() runs mid-download."""
        calls = []

        class Updater:
            def download(self, info, progress, cancelled):
                during_download()
                return "setup.exe"

            def launch(self, path):
                calls.append(("launch", path))

            def discard(self, path):
                calls.append(("discard", path))
        self.api._updater = Updater()
        self.api._update = {"version": "9.0.0"}
        self.api._can_install = True
        self.api._before_install = lambda: calls.append(("mutex dropped",))
        return calls

    def test_no_backup_is_queued_while_an_update_installs(self):
        rec = self.load()["rec"]
        seen = []
        calls = self.updating_api(lambda: seen.append(self.api.add_mark(rec, 0.1, 0.4, "A", "")))
        self.assertEqual(self.api.install_update(), {"ok": True})
        self.assertEqual((seen[0]["ok"], seen[0]["backup_queued"]), (True, False))   # the mark itself is kept
        self.assertEqual(calls, [("mutex dropped",), ("launch", "setup.exe")])
        self.assertFalse(self.api.backing_up())
        state = self.api.get_marks(rec)                                           # shown, and retried later
        self.assertEqual(state["backup"], {"status": "failed", "detail":
                         "A-001 was not backed up because an update is being installed."})
        self.assertTrue(state["backup_needed"])
        self.assertEqual([e for e, _ in self.events if e.startswith("backup")], [])

    def test_update_rechecks_for_a_backup_before_handing_over(self):
        calls = self.updating_api(lambda: None)
        with mock.patch.object(self.api, "backing_up", return_value=True):
            r = self.api.install_update()
        self.assertEqual((r["ok"], r["error"]), (False, backend.BACKUP_RUNNING))
        self.assertEqual(calls, [("discard", "setup.exe")])                        # never handed over
        self.assertFalse(self.api.updating())
        self.assertFalse(self.api.exporting())                                     # the lock was released

    def test_closing_records_queued_backups_as_not_made(self):
        entered, go = threading.Event(), threading.Event()
        real = backend.save_native

        def slow(*a):
            entered.set()
            go.wait(5)
            return real(*a)
        first = self.load(1)["rec"]
        second = self.load(2)
        with mock.patch.object(backend, "save_native", slow):
            self.api.add_mark(first, 0.1, 0.4, "A", "")
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.api.add_mark(second["rec"], 0.1, 0.4, "B", "")["backup_queued"])
            self.api.request_stop()                         # the user confirmed closing
            go.set()
            self.api.shutdown()
        got = {p["rec"]: (e, p["detail"]) for e, p in self.events if e.startswith("backup")}
        self.assertEqual(got[first][0], "backup-done")
        self.assertEqual(got[second["rec"]], ("backup-failed", "A-002 was not backed up because the app closed."))
        self.assertEqual(self.store.backup(second["fp"])["status"], "failed")

    def test_a_verification_stopped_by_closing_is_recorded(self):
        self.load()
        self.api._natives.clear()
        rec = self.load()["rec"]

        class Stopped(Exception):
            pass

        def stopped(data, should_stop=None):
            raise Stopped("closing")
        mod = fake_decoder(stopped)
        mod.Cancelled = Stopped
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": mod}):
            self.api.add_mark(rec, 0.1, 0.4, "A", "")
            event, p = self.wait_event()
        self.assertEqual((event, p["detail"]), ("backup-failed", "A-001 was not backed up because the app closed."))
        self.assertEqual(self.api.get_marks(rec)["backup"]["status"], "failed")

    def test_a_capture_is_used_only_with_the_audio_it_decoded_to(self):
        first = self.load()
        self.assertIsNotNone(self.api._recs[first["rec"]]["source"]["native"])
        dl, _ = self.api._natives[(ID, "A", 1)]
        self.api._natives[(ID, "A", 1)] = (dl, "another recording's fp")   # e.g. after a replug
        again = self.load()                                            # the server's cached decode
        self.assertIsNone(self.api._recs[again["rec"]]["source"]["native"])

    def test_workers_are_registered_under_a_lock(self):
        class Alive:
            def is_alive(self):
                return True

        def add():
            for _ in range(300):
                self.api._add_worker(Alive())
        threads = [threading.Thread(target=add) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.api._workers), 8 * 300)
        self.api._workers = []                              # the fakes cannot be joined

    def test_a_crashing_backup_never_leaves_backing_up_set(self):
        rec = self.load()["rec"]
        crashed = threading.Event()
        with mock.patch.object(self.api, "_backup", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.api, "_backup_result", side_effect=RuntimeError("emit failed")), \
                mock.patch.object(threading, "excepthook", lambda args: crashed.set()):
            self.api.add_mark(rec, 0.1, 0.4, "A", "")
            self.assertTrue(crashed.wait(5))
            with self.api._workers_lock:
                workers = list(self.api._workers)
            for t in workers:
                t.join(5)
        self.assertFalse(self.api.backing_up())
        self.assertIsNone(self.api._backup_thread)
        self.assertTrue(self.api.add_mark(rec, 0.5, 0.7, "B", "")["backup_queued"])   # a new worker starts
        self.assertEqual(self.wait_event()[0], "backup-done")

    # ---- WAV exports --------------------------------------------------------

    def test_wav_export_carries_marks_and_unmarked_has_none(self):
        rec = self.load()["rec"]
        self.api.add_mark(rec, 0.1, 0.4, "A", "hello")
        self.api.add_mark(rec, 0.5, 0.9, "B", "")
        self.assertEqual(self.wait_event()[0], "backup-done")
        out = os.path.join(self.tmp, "export")
        items = [{"folder": "A", "number": 1}, {"folder": "A", "number": 2}]
        self.assertTrue(self.api.export(ID, items, "wav", out, 1)["ok"])
        event, p = self.wait_event()
        self.assertEqual((event, p["saved"], p["notes"]), ("export-done", 2, []))
        marked = wavinfo.read_markers(os.path.join(out, "A", WAV_1))
        self.assertEqual([(round(m["start"], 3), round(m["end"], 3), m["note"]) for m in marked],
                         [(0.1, 0.4, "EVP A: hello"), (0.5, 0.9, "EVP B")])
        self.assertEqual(wavinfo.read_markers(os.path.join(out, "A", "001_A_002_Casey_2029_05_23.wav")), [])
        self.api.add_mark(rec, 0.95, 1.0, "C", "")               # different marks: a numbered copy
        self.api.export(ID, items[:1], "wav", out, 2)
        event, p = self.wait_event()
        self.assertEqual((p["saved"], p["skipped"]), (1, 0))
        self.api.export(ID, items[:1], "wav", out, 3)            # same marks again: already saved
        event, p = self.wait_event()
        self.assertEqual((p["saved"], p["skipped"]), (0, 1))
        self.assertIn("001_A_001_Casey_2029_05_23 (2).wav", os.listdir(os.path.join(out, "A")))

    def test_export_marked_device_and_file(self):
        rec = self.load()["rec"]
        self.assertEqual(self.api.export_marked(rec)["ok"], False)          # nothing marked yet
        self.api.add_mark(rec, 0.1, 0.4, "A", "hi")
        self.assertEqual(self.wait_event()[0], "backup-done")
        r = self.api.export_marked(rec)
        self.assertEqual(r, {"ok": True, "saved": False, "already": True, "name": WAV_1,
                             "folder_name": "A"})   # the backup's copy
        self.api.add_mark(rec, 0.5, 0.6, "B", "")
        r = self.api.export_marked(rec)
        self.assertEqual(r, {"ok": True, "saved": True, "already": False,
                             "name": "001_A_001_Casey_2029_05_23 (2).wav", "folder_name": "A"})
        self.assertEqual(len(wavinfo.read_markers(os.path.join(self.dest, "A", r["name"]))), 2)

        library = os.path.join(self.tmp, "Library")
        path = os.path.join(library, "Old Jail", "Night 2", "cell 3.wav")   # a sub-folder of the investigation
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(wav_bytes(b"jail"))
        api = self.new_api(pick_wav=lambda start: path)
        api._lib_folder = library
        frec = api.open_wav()["rec"]
        api.add_mark(frec, 0.2, 0.3, "C", "knock")
        r = api.export_marked(frec)
        self.assertEqual(r, {"ok": True, "saved": True, "already": False, "name": "cell 3.wav",
                             "folder_name": "Old Jail"})
        out = os.path.join(self.dest, "Old Jail", "cell 3.wav")
        self.assertEqual([m["note"] for m in wavinfo.read_markers(out)], ["EVP C: knock"])
        self.assertEqual(wavinfo.read_markers(path), [])                    # the user's file is untouched
        with open(path, "wb") as f:                                          # changed since it was loaded
            f.write(wav_bytes(b"other"))
        r = api.export_marked(frec)
        self.assertFalse(r["ok"])
        self.assertIn("cell 3.wav", r["error"])
        self.assertNotIn(self.tmp, r["error"])

    def marked_file(self):
        """A picked WAV with one mark, in a new api: (api, rec)."""
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(wav_bytes(b"take"))
        api = self.new_api(pick_wav=lambda start: path)
        rec = api.open_wav()["rec"]
        self.assertTrue(api.add_mark(rec, 0.2, 0.3, "C", "")["ok"])
        return api, rec

    def test_export_marked_is_refused_during_an_export_an_update_or_closing(self):
        api, rec = self.marked_file()
        self.assertTrue(api._busy.acquire(blocking=False))                  # an export is running
        try:
            self.assertEqual(api.export_marked(rec)["error"], backend.MARKED_BUSY)
            api._updating = True                                             # an update is installing
            self.assertEqual(api.export_marked(rec)["error"], backend.MARKED_BUSY_UPDATE)
        finally:
            api._updating = False
            api._busy.release()
        self.assertTrue(api.export_marked(rec)["ok"])
        api.request_stop()
        self.assertEqual(api.export_marked(rec)["error"], backend.CLOSING)
        self.assertFalse(api._busy.locked())

    def test_shutdown_waits_for_export_marked_and_no_update_or_export_starts_meanwhile(self):
        api, rec = self.marked_file()
        entered, go = threading.Event(), threading.Event()
        real = backend.save_wav
        closed_while_saving = []

        def slow(*a):
            entered.set()
            go.wait(5)
            closed_while_saving.append(self.store._closed)
            return real(*a)
        result = []
        with mock.patch.object(backend, "save_wav", slow):
            saver = threading.Thread(target=lambda: result.append(api.export_marked(rec)))
            saver.start()
            self.assertTrue(entered.wait(5))
            self.assertTrue(api.saving_marked())
            self.assertFalse(api.exporting())                                # the close prompt names it separately
            api._update, api._can_install = {"version": "9.0.0"}, True
            self.assertIn("export", api.install_update()["error"])
            self.assertFalse(api.updating())
            self.assertEqual(api.export(ID, [{"folder": "A", "number": 1}], "dvf", self.dest, 1)["error"],
                             "An export is already running.")
            self.assertEqual(api.export_marked(rec)["error"], backend.MARKED_BUSY)
            closer = threading.Thread(target=api.shutdown)
            closer.start()
            closer.join(0.3)
            self.assertTrue(closer.is_alive())                               # waiting for the WAV
            self.assertFalse(self.store._closed)
            go.set()
            closer.join(5)
            saver.join(5)
        self.assertFalse(closer.is_alive())
        self.assertEqual(closed_while_saving, [False])
        self.assertTrue(result[0]["ok"], result)
        self.assertTrue(self.store._closed)
        self.assertFalse(api.saving_marked())

    def test_export_marked_is_refused_once_shutdown_has_begun(self):
        api, rec = self.marked_file()
        api.shutdown()
        self.assertEqual(api.export_marked(rec)["error"], backend.CLOSING)

    def test_export_marked_outside_an_investigation_goes_to_the_save_to_root(self):
        library = os.path.join(self.tmp, "Library")
        os.makedirs(library)
        root_file = os.path.join(library, "loose.wav")
        outside = os.path.join(self.tmp, "Elsewhere", "far.wav")
        os.makedirs(os.path.dirname(outside))
        for path, seed in ((root_file, b"loose"), (outside, b"far")):
            with open(path, "wb") as f:
                f.write(wav_bytes(seed))
        for path in (root_file, outside):
            api = self.new_api(pick_wav=lambda start, p=path: p)
            api._lib_folder = library
            rec = api.open_wav()["rec"]
            api.add_mark(rec, 0.2, 0.3, "C", "")
            r = api.export_marked(rec)
            self.assertEqual((r["ok"], r["folder_name"]), (True, ""), r)
            self.assertTrue(os.path.isfile(os.path.join(self.dest, os.path.basename(path))))

    def test_investigation_of_a_path(self):
        lib = os.path.join(self.tmp, "Library")
        self.assertEqual(backend._investigation(os.path.join(lib, "Case1", "Night2", "x.wav"), lib), "Case1")
        self.assertEqual(backend._investigation(os.path.join(lib, "Case1", "x.wav"), lib), "Case1")
        self.assertEqual(backend._investigation(os.path.join(lib, "x.wav"), lib), "")
        self.assertEqual(backend._investigation(os.path.join(self.tmp, "Other", "x.wav"), lib), "")
        self.assertEqual(backend._investigation(os.path.join(self.tmp, "Library2", "C", "x.wav"), lib), "")

    # ---- backups: races and visibility ------------------------------------------

    def test_a_backup_queued_as_the_worker_exits_is_never_lost(self):
        """The worker releases the backup lock after finding the queue empty; a
        backup queued right then must still run. Deterministic: the queueing
        happens inside exactly that gap."""
        first = self.load(1)["rec"]
        second = self.load(2)
        api = self.api
        real_lock = api._backup_lock
        fired = []

        class GapLock:
            """Wraps the backup lock: right after the worker's release that saw an
            empty queue, queue the second recording's backup."""
            def __enter__(self):
                real_lock.acquire()
                self.saw_empty = (threading.current_thread().name == "backup" and not api._backup_queue
                                  and api._backup_running is None)
                return self

            def __exit__(self, *exc):
                saw_empty = self.saw_empty
                real_lock.release()
                if saw_empty and not fired:
                    fired.append(None)                  # once (_queue_backup takes this lock too)
                    fired[0] = api._queue_backup(second["rec"], api._entry(second["rec"]))
                return False
        api._backup_lock = GapLock()
        # Mark the second recording in the store directly (add_mark would queue its backup).
        self.store.add_mark(second["fp"], 0.1, 0.4, "B", "")
        api.add_mark(first, 0.1, 0.4, "A", "")
        deadline = threading.Event()
        for _ in range(100):
            if len([e for e, _ in self.events if e.startswith("backup")]) >= 2:
                break
            deadline.wait(0.05)
        self.assertEqual(fired, [True])
        api.shutdown()
        self.assertEqual(self.store.backup(second["fp"])["status"], "saved")
        self.assertEqual(sorted(p["rec"] for e, p in self.events if e == "backup-done"),
                         sorted([first, second["rec"]]))
        self.assertIsNone(api._backup_thread)
        self.assertFalse(api._backup_queue)

    def test_marked_but_not_backed_up_is_reported(self):
        rec = self.load()["rec"]
        self.assertFalse(self.api.get_marks(rec)["backup_needed"])      # nothing marked
        self.api.request_stop()                                          # closing: the backup is refused
        r = self.api.add_mark(rec, 0.1, 0.4, "A", "")
        self.assertEqual((r["ok"], r["backup_queued"]), (True, False))
        state = self.api.get_marks(rec)
        self.assertEqual(state["backup"]["status"], "failed")
        self.assertTrue(state["backup_needed"])

    def test_a_fresh_load_shows_a_missing_backup(self):
        rec = self.load()["rec"]
        with mock.patch.object(self.api, "_queue_backup", return_value=False):
            self.api.add_mark(rec, 0.1, 0.4, "A", "")                    # status stays None
        r = self.load()
        self.assertEqual((r["backup"]["status"], r["backup_needed"]), (None, True))

    def test_a_failed_worker_start_is_recorded(self):
        rec = self.load()["rec"]
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start a thread")):
            r = self.api.add_mark(rec, 0.1, 0.4, "A", "")
        self.assertEqual((r["ok"], r["backup_queued"]), (True, False))
        state = self.api.get_marks(rec)
        self.assertEqual(state["backup"], {"status": "failed",
                                           "detail": "A-001 was not backed up: cannot start a thread"})
        self.assertTrue(state["backup_needed"])
        self.assertFalse(self.api.backing_up())
        self.assertIsNone(self.api._backup_thread)

    def test_an_empty_wav_cannot_be_marked(self):
        path = os.path.join(self.tmp, "empty.wav")
        with open(path, "wb") as f:
            f.write(wav_bytes(b"e", seconds=0))
        server = mock.Mock()
        server.prepare_file.return_value = {"url": "http://x/e.wav", "peaks": [], "duration": 0.0,
                                            "rate": 8000, "fp": None}
        api = backend.Api(self.m, self.emit, lambda start: None, self.dest, server,
                          pick_wav=lambda start: path, store=self.store)
        self.addCleanup(api.shutdown)
        r = api.open_wav()
        self.assertEqual((r["ok"], r["fp"], r["marks"], r["imported"]), (True, None, [], 0))
        for call in (lambda: api.add_mark(r["rec"], 0.0, 0.1, "A", ""), lambda: api.get_marks(r["rec"]),
                     lambda: api.export_marked(r["rec"])):
            self.assertEqual(call()["error"], backend.NO_AUDIO)

    # ---- import of embedded markers ------------------------------------------

    def test_deleted_marks_never_come_back_from_the_apps_own_wavs(self):
        rec = self.load()["rec"]
        self.api.add_mark(rec, 0.1, 0.4, "A", "hello")
        self.assertEqual(self.wait_event()[0], "backup-done")
        self.api.add_mark(rec, 0.5, 0.6, "B", "")
        exported = self.api.export_marked(rec)                        # a numbered copy with both marks
        self.assertTrue(exported["ok"], exported)
        backup_wav = os.path.join(self.dest, "A", WAV_1)
        export_wav = os.path.join(self.dest, "A", exported["name"])
        for m in self.api.get_marks(rec)["marks"]:
            self.api.delete_mark(rec, m["id"])
        for path in (backup_wav, export_wav):
            self.assertTrue(wavinfo.read_markers(path))                # the files do carry markers
            info = self.server.prepare_file(path)
            self.assertEqual(self.api._import_markers(path, info, os.path.basename(path), info["stat"]), 0)
            api = self.new_api(pick_wav=lambda start, p=path: p)
            r = api.open_wav()
            self.assertEqual((r["imported"], r["marks"]), (0, []))
        self.assertEqual(self.api.get_marks(rec)["marks"], [])


    def test_open_wav_imports_embedded_markers_once(self):
        path = os.path.join(self.tmp, "marked.wav")
        with open(path, "wb") as f:
            f.write(wavinfo.with_markers(wav_bytes(b"m"), [
                {"start": 0.1, "end": 0.3, "cls": "A", "note": "yes"},
                {"start": 0.5, "end": 0.6, "cls": "B", "note": ""}]))
        api = self.new_api(pick_wav=lambda start: path)
        r = api.open_wav()
        self.assertEqual((r["ok"], r["name"], r["imported"]), (True, "marked.wav", 2))
        self.assertEqual([(m["cls"], m["note"]) for m in r["marks"]], [("A", "yes"), ("B", "")])
        for m in r["marks"]:
            api.delete_mark(r["rec"], m["id"])
        r = api.open_wav()
        self.assertEqual((r["imported"], r["marks"]), (0, []))           # never imported again

    def test_markers_are_imported_only_from_the_version_that_was_fingerprinted(self):
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(wav_bytes(b"x"))
        info = self.server.prepare_file(path)                             # fingerprinted: no markers yet
        stat = info.pop("stat")
        # Replaced before the import reads it: a marked file of the same name.
        with open(path, "wb") as f:
            f.write(wavinfo.with_markers(wav_bytes(b"y"), [{"start": 0.1, "end": 0.3, "cls": "A", "note": "no"}]))
        os.utime(path, ns=(stat[1], stat[1] + 10**9))
        self.assertEqual(self.api._import_markers(path, info, "take.wav", stat), 0)
        self.assertEqual(self.store.marks(info["fp"]), [])
        self.assertEqual(self.api._import_markers(path, info, "take.wav", None), 0)   # unknown version: never
        # The same version as fingerprinted imports.
        fresh = self.server.prepare_file(path)
        self.assertEqual(self.api._import_markers(path, fresh, "take.wav", fresh["stat"]), 1)

    def test_recording_changed_reports_a_file_edited_after_loading(self):
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(wav_bytes(b"x"))
        api = self.new_api(pick_wav=lambda start: path)
        r = api.open_wav()
        self.assertNotIn("stat", r)                                         # never sent to the page
        self.assertEqual(api.recording_changed(r["rec"]), {"ok": True, "changed": False})
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        self.assertEqual(api.recording_changed(r["rec"]), {"ok": True, "changed": True})
        os.remove(path)
        self.assertEqual(api.recording_changed(r["rec"]), {"ok": True, "changed": True})
        self.assertEqual(self.api.recording_changed(self.load()["rec"]), {"ok": True, "changed": False})
        self.assertEqual(api.recording_changed("nope"), {"ok": True, "changed": False})

    # ---- Save-to ----------------------------------------------------------------

    def test_save_to_persists(self):
        chosen = os.path.join(self.tmp, "Investigations")
        os.mkdir(chosen)
        api = self.new_api(pick_folder=lambda start: chosen)
        self.assertEqual(api.choose_destination(), chosen)
        self.store.close()
        store = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(store.close)
        self.assertEqual(self.new_api(store=store).default_destination(), chosen)
        os.rmdir(chosen)                                               # gone: back to the default
        self.assertEqual(self.new_api(store=store).default_destination(), self.dest)


if __name__ == "__main__":
    unittest.main()
