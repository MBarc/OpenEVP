"""The EVP library backend: listing a folder of recordings, the background
fingerprint indexer (events, cache, failures, cancellation) and playing files."""
import dataclasses
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
from fixtures import DATE, made_wav, make_raw  # noqa: E402
import release_gate  # noqa: E402
from app import backend  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import formats  # noqa: E402
from openevp import wavinfo  # noqa: E402
from sony_icd import audio, dvf  # noqa: E402

WAIT = 30


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


def dvf_bytes(counter=100):
    return dvf.build(make_raw(3010, counter), DATE, "X", expected_length=3010)


DECODED = wav_bytes(b"decoded", seconds=2.0)       # what the fake decoder returns for every .dvf


class FakeDecoder:
    """A stand-in openevp.decoders.sony_lpec: returns DECODED (or raises for data in `bad`), counts
    calls, and can hold the first `block` calls until released."""

    class Cancelled(Exception):
        pass

    def __init__(self, block=0, honour_stop=False):
        self.calls = 0
        self.bad = set()
        self.block = block
        self.honour_stop = honour_stop
        self.started = threading.Event()
        self.release = threading.Event()
        self.saw_stop = None
        self.module = types.ModuleType("openevp.decoders.sony_lpec")
        self.module.dvf_to_wav = self.decode
        self.module.Cancelled = FakeDecoder.Cancelled

    def decode(self, data, should_stop=None, progress=None):
        self.calls += 1
        if self.calls <= self.block:
            self.started.set()
            if self.honour_stop:
                while not should_stop():
                    self.release.wait(0.01)
                self.saw_stop = True
                raise FakeDecoder.Cancelled("stopped")
            self.release.wait(WAIT)
            self.saw_stop = bool(should_stop and should_stop())
        if data in self.bad:
            raise ValueError("this is not a recording")
        return DECODED

    def installed(self):
        return mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": self.module})


class FakeServer:
    def __init__(self):
        self.files, self.made = [], []

    def prepare_file(self, path):
        self.files.append(path)
        with wave.open(path) as w:
            duration = w.getnframes() / w.getframerate()
        return {"url": "http://x/f.wav", "peaks": [0.1], "duration": duration,
                "fp": wavinfo.wav_fingerprint(path), "stat": (os.stat(path).st_size, os.stat(path).st_mtime_ns)}

    def prepare(self, key, make=None, write=None):
        wav = made_wav(make, write)
        self.made.append(key)
        return {"url": "http://x/d.wav", "peaks": [0.2], "duration": 2.0,
                "fp": wavinfo.wav_fingerprint(io.BytesIO(wav))}


class Events:
    def __init__(self):
        self.items = []
        self.cond = threading.Condition()

    def __call__(self, name, payload):
        with self.cond:
            self.items.append((name, payload))
            self.cond.notify_all()

    def wait_done(self, scan_id, timeout=WAIT):
        with self.cond:
            ok = self.cond.wait_for(lambda: ("library-done", {"scan_id": scan_id}) in self.items, timeout)
        if not ok:
            raise AssertionError(f"no library-done for scan {scan_id}: {self.items}")

    def rows(self, scan_id=None):
        return {p["id"]: p for n, p in list(self.items)
                if n == "library-row" and (scan_id is None or p["scan_id"] == scan_id)}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.lib = os.path.join(self.tmp, "OpenEVP")
        self.store = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(self.store.close)
        self.events = Events()
        self.server = FakeServer()
        self.picked = None

    def new_api(self, store="default"):
        api = backend.Api(None, self.events, lambda start: self.picked, self.lib, self.server,
                          store=self.store if store == "default" else store)
        self.addCleanup(api.shutdown)
        return api

    def write(self, rel, data):
        path = os.path.join(self.lib, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def populate(self):
        self.write("Old Mill/A/x.dvf", dvf_bytes())
        self.write("Old Mill/y.wav", wav_bytes(b"y", 1.5))
        self.write("z.wav", wav_bytes(b"z", 1.0))
        self.write("a/b/c/d/deep.wav", wav_bytes(b"deep", 0.5))
        self.write("notes.txt", b"not audio")

    def index(self, api):
        """list_library() and wait for its indexer (if any) to finish."""
        r = api.list_library()
        self.assertTrue(r["ok"], r)
        if r["indexing"]:
            self.events.wait_done(r["scan_id"])
        return r

    def by_name(self, r):
        return {f["name"]: f for f in r["files"]}

    # ---- listing ---------------------------------------------------------------

    def test_rows_investigations_types_and_no_depth_limit(self):
        self.populate()
        fake = FakeDecoder()
        with fake.installed():
            r = self.new_api().list_library()
        self.assertEqual((r["ok"], r["exists"], r["truncated"], r["folder"]), (True, True, False, self.lib))
        self.assertIsInstance(r["scan_id"], int)
        got = [(f["investigation"], f["name"], f["type"]) for f in r["files"]]
        self.assertEqual(got, [("", "z.wav", "wav"), ("a", "deep.wav", "wav"),
                               ("Old Mill", "y.wav", "wav"), ("Old Mill", "x.dvf", "dvf")])
        for f in r["files"]:
            self.assertEqual(set(f), {"id", "name", "investigation", "type", "seconds", "modified", "fp",
                                      "marks", "reviewed", "notes", "error", "unplayable", "folder_id", "clip"})
            self.assertEqual((f["fp"], f["marks"], f["reviewed"], f["notes"], f["error"], f["clip"]),
                             (None, {"A": 0, "B": 0, "C": 0}, False, "", None, False))
            self.assertRegex(f["id"], "^[0-9a-f]{16}$")
            self.assertNotIn(self.tmp, repr(f))                              # the page never sees paths

    def test_missing_folder(self):
        r = self.new_api().list_library()
        self.assertEqual((r["ok"], r["exists"], r["files"], r["truncated"]), (True, False, [], False))
        self.assertIn("scan_id", r)
        self.assertFalse(os.path.exists(self.lib))                           # never created by listing

    def test_limit_is_enforced_while_walking(self):
        for i in range(4):
            self.write(f"t{i}.wav", wav_bytes(b"t%d" % i))
        self.write("sub/s.wav", wav_bytes(b"s"))
        seen = []
        real = os.scandir

        def spy(path="."):
            seen.append(os.path.normcase(str(path)))
            return real(path)
        with mock.patch.object(backend, "SAVED_LIMIT", 3), mock.patch("os.scandir", spy):
            r = self.new_api(store=None).list_library()
        self.assertTrue(r["truncated"])
        self.assertEqual([f["name"] for f in r["files"]], ["t0.wav", "t1.wav", "t2.wav"])
        self.assertNotIn(os.path.normcase(os.path.join(self.lib, "sub")), seen)   # stopped before walking it

    def test_library_folder_default_and_choose(self):
        api = self.new_api()
        self.assertEqual(api.library_folder(), {"ok": True, "folder": self.lib, "exists": False})
        other = os.path.join(self.tmp, "Cases")
        os.makedirs(other)
        self.picked = other
        self.assertEqual(api.choose_library_folder(), other)
        self.assertEqual(api.library_folder(), {"ok": True, "folder": other, "exists": True})
        self.assertEqual(self.store.get_setting("library_folder"), other)
        self.assertEqual(self.new_api().library_folder()["folder"], other)  # remembered
        self.picked = None
        self.assertIsNone(api.choose_library_folder())                      # cancelled: unchanged
        self.assertEqual(api.library_folder()["folder"], other)

    # ---- folders -----------------------------------------------------------------

    def test_folders_nested_empty_dot_and_true_tree_order(self):
        self.populate()
        os.makedirs(os.path.join(self.lib, "Empty"))
        os.makedirs(os.path.join(self.lib, ".hidden"))
        r = self.new_api(store=None).list_library()
        self.assertEqual(r["folders"][0], {"id": "root", "parent": None, "name": "OpenEVP", "rel": [],
                                             "in_clips": False, "clips": False})
        order = [tuple(f["rel"]) for f in r["folders"][1:]]
        self.assertEqual(order, [("a",), ("a", "b"), ("a", "b", "c"), ("a", "b", "c", "d"),
                                 ("Empty",), ("Old Mill",), ("Old Mill", "A")])
        for f in r["folders"]:
            if f["id"] != "root":
                self.assertRegex(f["id"], "^[0-9a-f]{16}$")
        by_rel = {tuple(f["rel"]): f for f in r["folders"]}
        self.assertNotIn((".hidden",), by_rel)                               # dot folders are not listed
        root_id = r["folders"][0]["id"]
        self.assertEqual(by_rel[("a",)]["parent"], root_id)
        self.assertEqual(by_rel[("a", "b")]["parent"], by_rel[("a",)]["id"])
        self.assertEqual(by_rel[("a", "b", "c")]["parent"], by_rel[("a", "b")]["id"])
        self.assertEqual(by_rel[("a", "b", "c", "d")]["parent"], by_rel[("a", "b", "c")]["id"])
        self.assertEqual(by_rel[("Empty",)]["parent"], root_id)
        self.assertEqual(by_rel[("Old Mill",)]["parent"], root_id)
        self.assertEqual(by_rel[("Old Mill", "A")]["parent"], by_rel[("Old Mill",)]["id"])
        self.assertEqual(by_rel[("a",)]["name"], "a")
        self.assertEqual(by_rel[("Old Mill", "A")]["name"], "A")
        for f in r["folders"]:
            self.assertNotIn("depth", f)
            self.assertEqual(set(f), {"id", "parent", "name", "rel", "clips", "in_clips"})
            self.assertEqual((f["clips"], f["in_clips"]), (False, False))

    def test_folder_ids_are_stable_across_scans(self):
        self.populate()
        api = self.new_api(store=None)
        first = api.list_library()
        second = api.list_library()
        self.assertEqual({tuple(f["rel"]): f["id"] for f in first["folders"]},
                         {tuple(f["rel"]): f["id"] for f in second["folders"]})

    def test_files_carry_the_right_folder_id(self):
        self.populate()
        r = self.new_api(store=None).list_library()
        by_rel = {tuple(f["rel"]): f["id"] for f in r["folders"]}
        got = {f["name"]: f["folder_id"] for f in r["files"]}
        self.assertEqual(got["z.wav"], "root")
        self.assertEqual(got["deep.wav"], by_rel[("a", "b", "c", "d")])
        self.assertEqual(got["y.wav"], by_rel[("Old Mill",)])
        self.assertEqual(got["x.dvf"], by_rel[("Old Mill", "A")])

    def test_missing_folder_has_no_folders(self):
        r = self.new_api().list_library()
        self.assertEqual(r["folders"], [])

    # ---- indexing ----------------------------------------------------------------

    def test_fp_arrives_by_events_then_comes_from_the_cache(self):
        self.populate()
        fake = FakeDecoder()
        api = self.new_api()
        with fake.installed():
            first = api.list_library()
            self.events.wait_done(first["scan_id"])
            self.assertEqual(fake.calls, 1)
            rows = self.events.rows(first["scan_id"])
            names = self.by_name(first)
            self.assertEqual(set(rows), {f["id"] for f in first["files"]})
            self.assertEqual(rows[names["x.dvf"]["id"]]["fp"], wavinfo.wav_fingerprint(io.BytesIO(DECODED)))
            self.assertEqual(rows[names["x.dvf"]["id"]]["seconds"], 2.0)
            self.assertEqual(rows[names["y.wav"]["id"]]["fp"],
                             wavinfo.wav_fingerprint(os.path.join(self.lib, "Old Mill", "y.wav")))
            self.assertEqual(rows[names["y.wav"]["id"]]["seconds"], 1.5)
            for row in rows.values():
                self.assertEqual(set(row), {"scan_id", "id", "fp", "marks", "reviewed", "notes", "seconds",
                                            "error", "unplayable"})
            progress = [p for n, p in self.events.items if n == "library-progress"]
            self.assertEqual(progress[-1], {"scan_id": first["scan_id"], "done": 4, "total": 4})

            before = len(self.events.items)
            second = api.list_library()
            self.assertEqual(fake.calls, 1)                                  # nothing decoded again
            self.assertEqual({f["id"]: f["fp"] for f in second["files"]},
                             {i: r["fp"] for i, r in rows.items()})
            self.assertEqual(self.by_name(second)["y.wav"]["seconds"], 1.5)
            self.assertIsNone(api._indexer)                                   # no indexer needed
            self.assertEqual(len(self.events.items), before)
        with open(os.path.join(self.tmp, "appdata", "index.json"), encoding="utf-8") as f:
            self.assertIn(rows[names["z.wav"]["id"]]["fp"], f.read())        # flushed at the end

    def test_cache_is_pruned(self):
        self.populate()
        api = self.new_api()
        with FakeDecoder().installed():
            self.index(api)
        z = os.path.join(self.lib, "z.wav")
        st = os.stat(z)
        self.assertIsNotNone(self.store.cached_fp(z, st.st_size, st.st_mtime_ns))
        os.remove(z)
        with FakeDecoder().installed():
            api.list_library()
        self.assertIsNone(self.store.cached_fp(z, st.st_size, st.st_mtime_ns))
        y = os.path.join(self.lib, "Old Mill", "y.wav")
        st = os.stat(y)
        self.assertIsNotNone(self.store.cached_fp(y, st.st_size, st.st_mtime_ns))

    def test_flushes_every_20_files(self):
        for i in range(45):
            self.write(f"f{i:02d}.wav", wav_bytes(b"%d" % i, 0.05))
        api = self.new_api()
        with mock.patch.object(self.store, "flush_index", wraps=self.store.flush_index) as flush:
            self.index(api)
        self.assertEqual(flush.call_count, 3)                                 # after 20, 40, and at the end

    def test_dvf_and_its_wav_share_fp_fake_decoder(self):
        self.write("case/rec.dvf", dvf_bytes())
        self.write("case/rec.wav", DECODED)
        with FakeDecoder().installed():
            r = self.index(self.new_api())
        rows = self.events.rows(r["scan_id"])
        fps = {rows[f["id"]]["fp"] for f in r["files"]}
        self.assertEqual(len(fps), 1)
        self.assertIsNotNone(fps.pop())

    @release_gate.require(audio.available(), "the LPEC decoder is not available")
    def test_dvf_and_its_wav_share_fp_real_decoder(self):
        data = dvf_bytes()
        self.write("case/rec.dvf", data)
        self.write("case/rec.wav", audio.dvf_to_wav(data))
        r = self.index(self.new_api())
        rows = self.events.rows(r["scan_id"])
        fps = {rows[f["id"]]["fp"] for f in r["files"]}
        self.assertEqual(len(fps), 1)
        self.assertIsNotNone(fps.pop())

    def test_failures_are_cached(self):
        self.write("broken.wav", b"RIFF junk that is not a wav")
        bad_dvf = dvf_bytes(counter=7)
        self.write("bad.dvf", bad_dvf)
        fake = FakeDecoder()
        fake.bad.add(bad_dvf)
        api = self.new_api()
        with fake.installed():
            r = self.index(api)
            rows = self.events.rows(r["scan_id"])
            names = self.by_name(r)
            for name in ("broken.wav", "bad.dvf"):
                self.assertIsNone(rows[names[name]["id"]]["fp"])
                self.assertTrue(rows[names[name]["id"]]["error"])
                self.assertNotIn(self.tmp, rows[names[name]["id"]]["error"])
            self.assertIn("not a recording", rows[names["bad.dvf"]["id"]]["error"])
            self.assertEqual(fake.calls, 1)
            again = api.list_library()
            self.assertEqual(fake.calls, 1)                                  # not retried
            self.assertIsNone(api._indexer)
        for name in ("broken.wav", "bad.dvf"):
            self.assertEqual(self.by_name(again)[name]["error"], rows[names[name]["id"]]["error"])

    def test_missing_decoder_is_reported_but_not_cached(self):
        self.write("x.dvf", dvf_bytes())
        api = self.new_api()
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": None}):
            status = audio.status()
            r = self.index(api)
        row = self.events.rows(r["scan_id"])[r["files"][0]["id"]]
        self.assertEqual((row["fp"], row["error"]), (None, status))
        fake = FakeDecoder()
        with fake.installed():                                                # the decoder is there now
            again = api.list_library()
            self.assertEqual((again["files"][0]["fp"], again["files"][0]["error"]), (None, None))
            self.events.wait_done(again["scan_id"])
        self.assertEqual(fake.calls, 1)
        self.assertIsNotNone(self.events.rows(again["scan_id"])[r["files"][0]["id"]]["fp"])

    def test_file_changed_while_hashed_is_discarded(self):
        path = self.write("live.wav", wav_bytes(b"live"))
        self.write("still.wav", wav_bytes(b"still"))
        real = wavinfo.wav_fingerprint

        def changing(f, should_stop=None):
            fp = real(f)
            if f == path:
                with open(path, "ab") as out:                                 # recorder still writing
                    out.write(b"\0" * 10)
            return fp
        api = self.new_api()
        with mock.patch.object(backend.wavinfo, "wav_fingerprint", changing):
            r = self.index(api)
        rows = self.events.rows(r["scan_id"])
        ids = {f["name"]: f["id"] for f in r["files"]}
        live = rows[ids["live.wav"]]                                          # no stale fp emitted
        self.assertIsNone(live["fp"])
        self.assertIn("changed while it was being read", live["error"])
        self.assertIsNotNone(rows[ids["still.wav"]]["fp"])
        st = os.stat(path)
        self.assertIsNone(self.store.cached_fp(path, st.st_size, st.st_mtime_ns))
        again = api.list_library()
        self.assertIsNone(self.by_name(again)["live.wav"]["fp"])              # redone by the next scan
        self.events.wait_done(again["scan_id"])
        self.assertEqual(self.events.rows(again["scan_id"])[ids["live.wav"]]["fp"], real(path))

    def test_embedded_markers_are_imported_while_indexing(self):
        self.write("Old Mill/marked.wav", wavinfo.with_markers(wav_bytes(b"m"), [
            {"start": 0.1, "end": 0.3, "cls": "A", "note": "Hello There"},
            {"start": 0.5, "end": 0.6, "cls": "B", "note": ""}]))
        api = self.new_api()
        r = self.index(api)
        fid = r["files"][0]["id"]
        row = self.events.rows(r["scan_id"])[fid]
        self.assertEqual((row["marks"], row["notes"]), ({"A": 1, "B": 1, "C": 0}, "hello there"))
        self.assertEqual(len(self.store.marks(row["fp"])), 2)
        again = api.list_library()["files"][0]
        self.assertEqual((again["fp"], again["marks"], again["notes"], again["name"]),
                         (row["fp"], {"A": 1, "B": 1, "C": 0}, "hello there", "marked.wav"))  # search fields
        self.assertEqual([m["note"] for m in api.library_marks(fid)["marks"]], ["Hello There", ""])

    def test_marks_summary_and_reviewed_join_rows(self):
        path = self.write("z.wav", wav_bytes(b"z"))
        fp = wavinfo.wav_fingerprint(path)
        self.store.add_mark(fp, 0.1, 0.4, "C", "Knock")
        self.store.set_reviewed(fp, True)
        api = self.new_api()
        r = self.index(api)
        row = self.events.rows(r["scan_id"])[r["files"][0]["id"]]
        self.assertEqual((row["marks"], row["reviewed"], row["notes"]), ({"A": 0, "B": 0, "C": 1}, True, "knock"))
        f = api.list_library()["files"][0]
        self.assertEqual((f["marks"], f["reviewed"], f["notes"]), ({"A": 0, "B": 0, "C": 1}, True, "knock"))

    # ---- one indexer: newer scans, closing -------------------------------------------

    def test_newer_scan_stops_the_old_indexer(self):
        self.write("one.dvf", dvf_bytes(1))
        self.write("two.dvf", dvf_bytes(2))
        other = os.path.join(self.tmp, "Other")
        os.makedirs(other)
        with open(os.path.join(other, "w.wav"), "wb") as f:
            f.write(wav_bytes(b"w"))
        fake = FakeDecoder(block=1)
        api = self.new_api()
        with fake.installed():
            first = api.list_library()
            self.assertTrue(fake.started.wait(WAIT))
            old_thread = api._indexer
            self.picked = other
            api.choose_library_folder()
            second = api.list_library()
            self.assertGreater(second["scan_id"], first["scan_id"])
            mark = len(self.events.items)
            fake.release.set()
            self.events.wait_done(second["scan_id"])
        self.assertTrue(fake.saw_stop)                                        # should_stop told it to stop
        self.assertEqual(fake.calls, 1)                                       # two.dvf never decoded
        later = self.events.items[mark:]
        self.assertTrue(later)
        self.assertTrue(all(p["scan_id"] == second["scan_id"] for n, p in later))
        self.assertTrue(all("scan_id" in p for n, p in self.events.items))
        self.assertEqual(self.events.rows(first["scan_id"]), {})
        self.assertNotIn(("library-done", {"scan_id": first["scan_id"]}), self.events.items)
        old_thread.join(WAIT)                         # the same thread ran both jobs, then ended
        self.assertFalse(old_thread.is_alive())
        self.assertIs(api._indexer, None)
        indexers = [t for t in threading.enumerate() if t.name == "library-indexer"]
        self.assertEqual(indexers, [])
        # the old folder's files cannot be played any more; the new one's can
        self.assertFalse(api.play_library(first["files"][0]["id"])["ok"])
        self.assertTrue(api.play_library(second["files"][0]["id"])["ok"])

    def test_same_folder_refresh_keeps_the_file_in_flight(self):
        self.write("one.dvf", dvf_bytes(1))
        self.write("two.dvf", dvf_bytes(2))
        fake = FakeDecoder(block=1)
        api = self.new_api()
        with fake.installed():
            first = api.list_library()
            self.assertTrue(fake.started.wait(WAIT))
            second = api.list_library()                                       # same folder
            self.assertEqual((second["indexing"], second["pending"]), (True, 2))
            fake.release.set()
            self.events.wait_done(second["scan_id"])
        self.assertFalse(fake.saw_stop)                                       # the decode was not cancelled
        self.assertEqual(fake.calls, 2)                                       # one.dvf decoded once, not twice
        rows = self.events.rows(second["scan_id"])
        self.assertEqual(set(rows), {f["id"] for f in second["files"]})
        self.assertTrue(all(r["fp"] for r in rows.values()))
        self.assertEqual(self.events.rows(first["scan_id"]), {})

    def test_indexing_flag(self):
        self.populate()
        with FakeDecoder().installed():
            r = self.new_api(store=None).list_library()
            self.assertEqual((r["indexing"], r["pending"]), (False, 0))
            api = self.new_api()
            with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")):
                r = api.list_library()
            self.assertEqual((r["indexing"], r["pending"]), (False, 0))
            r = api.list_library()
            self.assertEqual((r["indexing"], r["pending"]), (True, 4))
            self.events.wait_done(r["scan_id"])
            r = api.list_library()                                            # everything cached
            self.assertEqual((r["indexing"], r["pending"]), (False, 0))

    def test_a_job_that_fails_still_ends_with_library_done(self):
        self.write("z.wav", wav_bytes(b"z"))
        api = self.new_api()
        with mock.patch.object(api, "_index_file", side_effect=RuntimeError("disk on fire")):
            r = api.list_library()
            with self.events.cond:
                ok = self.events.cond.wait_for(
                    lambda: any(n == "library-done" for n, p in self.events.items), WAIT)
        self.assertTrue(ok)
        done = [p for n, p in self.events.items if n == "library-done"]
        self.assertEqual(done[0]["scan_id"], r["scan_id"])
        self.assertIn("disk on fire", done[0]["error"])

    def test_read_only_store_keeps_results_for_the_session(self):
        self.write("x.dvf", dvf_bytes())
        second = AppData(os.path.join(self.tmp, "appdata"))                   # another window: read-only
        self.addCleanup(second.close)
        self.assertTrue(second.read_only)
        fake = FakeDecoder()
        api = self.new_api(store=second)
        with fake.installed():
            self.index(api)
            again = api.list_library()
        self.assertEqual(fake.calls, 1)
        self.assertFalse(again["indexing"])
        self.assertIsNotNone(again["files"][0]["fp"])

    def test_read_only_store_library_marks_use_the_session_results(self):
        self.write("x.dvf", dvf_bytes())
        fp = wavinfo.wav_fingerprint(io.BytesIO(DECODED))
        self.store.add_mark(fp, 0.2, 0.5, "A", "hello", name="x.dvf", duration=2.0)
        self.store.close()
        folder = os.path.join(self.tmp, "appdata")
        holder = AppData(folder)                                              # another window holds the lock
        self.addCleanup(holder.close)
        reader = AppData(folder)
        self.addCleanup(reader.close)
        self.assertTrue(reader.read_only)
        api = self.new_api(store=reader)
        with FakeDecoder().installed():
            r = self.index(api)                                               # the fp is only in this session
        marks = api.library_marks(r["files"][0]["id"])
        self.assertEqual([(m["cls"], m["note"]) for m in marks["marks"]], [("A", "hello")])

    def test_read_only_store_uses_the_saved_index(self):
        self.write("x.dvf", dvf_bytes())
        self.write("y.wav", wav_bytes(b"y"))
        with FakeDecoder().installed():
            first = self.index(self.new_api())
        fps = {f["id"]: f["fp"] for f in self.events.rows(first["scan_id"]).values()}
        self.store.close()                                                    # index.json written
        folder = os.path.join(self.tmp, "appdata")
        holder = AppData(folder)                                              # another window holds the lock
        self.addCleanup(holder.close)
        reader = AppData(folder)
        self.addCleanup(reader.close)
        self.assertTrue(reader.read_only)
        fake = FakeDecoder()
        with fake.installed():
            r = self.new_api(store=reader).list_library()
        self.assertEqual(fake.calls, 0)
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))
        self.assertEqual({f["id"]: f["fp"] for f in r["files"]}, fps)
        self.assertTrue(all(fps.values()))

    def test_unreadable_subfolder_is_not_pruned(self):
        path = self.write("sub/s.wav", wav_bytes(b"s"))
        self.write("t.wav", wav_bytes(b"t"))
        api = self.new_api()
        self.index(api)
        real = os.scandir
        sub = os.path.normcase(os.path.join(self.lib, "sub"))

        def denied(p="."):
            if os.path.normcase(str(p)) == sub:
                raise PermissionError(13, "Access is denied", str(p))
            return real(p)
        with mock.patch("os.scandir", denied):
            r = api.list_library()
        self.assertEqual([f["name"] for f in r["files"]], ["t.wav"])
        st = os.stat(path)
        self.assertIsNotNone(self.store.cached_fp(path, st.st_size, st.st_mtime_ns))

    def test_dot_names_are_skipped(self):
        self.write("._z.wav", b"\0\5\26\7AppleDouble")
        self.write(".hidden/h.wav", wav_bytes(b"h"))
        self.write("z.wav", wav_bytes(b"z"))
        r = self.new_api(store=None).list_library()
        self.assertEqual([f["name"] for f in r["files"]], ["z.wav"])

    def test_oversized_dvf_is_refused_and_remembered(self):
        self.write("huge.dvf", dvf_bytes())
        fake = FakeDecoder()
        api = self.new_api()
        small = dataclasses.replace(formats.DVF, max_bytes=100)                 # the limit is the format's
        with fake.installed(), mock.patch.dict(formats._registry, {".dvf": small}):
            r = self.index(api)
            again = api.list_library()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self.events.rows(r["scan_id"])[r["files"][0]["id"]]["error"],
                         "huge.dvf is too large to be a Sony ICD-ST recording.")
        self.assertEqual((again["indexing"], again["files"][0]["error"]),
                         (False, "huge.dvf is too large to be a Sony ICD-ST recording."))

    def test_decoder_unavailable_or_out_of_memory_is_not_remembered(self):
        self.write("x.dvf", dvf_bytes())
        api = self.new_api()
        for problem in (audio.DecoderUnavailable("the decoder went away"), MemoryError()):
            fake = FakeDecoder()

            def decode(data, should_stop=None, problem=problem):
                raise problem
            fake.module.dvf_to_wav = decode
            with fake.installed():
                r = self.index(api)
            self.assertTrue(r["indexing"])                                     # retried every time
            row = self.events.rows(r["scan_id"])[r["files"][0]["id"]]
            self.assertEqual(row["fp"], None)
            self.assertTrue(row["error"])
        with FakeDecoder().installed():
            r = self.index(api)
        self.assertIsNotNone(self.events.rows(r["scan_id"])[r["files"][0]["id"]]["fp"])

    def test_stop_stops_the_indexer(self):
        self.write("one.dvf", dvf_bytes(1))
        self.write("two.dvf", dvf_bytes(2))
        fake = FakeDecoder(block=1)
        api = self.new_api()
        with fake.installed():
            r = api.list_library()
            self.assertTrue(fake.started.wait(WAIT))
            thread = api._indexer
            api.request_stop()
            fake.release.set()
            thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(fake.calls, 1)
        self.assertNotIn(("library-done", {"scan_id": r["scan_id"]}), self.events.items)
        self.assertEqual(self.events.rows(), {})
        with fake.installed():
            again = api.list_library()                                        # closing: no new indexer
        self.assertEqual((again["indexing"], again["pending"]), (False, 0))
        self.assertIsNone(api._indexer)

    def test_shutdown_joins_the_indexer_and_flushes_the_cache(self):
        wav = self.write("a.wav", wav_bytes(b"a"))
        self.write("b.dvf", dvf_bytes())
        fake = FakeDecoder(block=1, honour_stop=True)                        # stops only via should_stop
        api = self.new_api()
        with fake.installed():
            api.list_library()
            self.assertTrue(fake.started.wait(WAIT))
            thread = api._indexer
            api.shutdown()
        self.assertFalse(thread.is_alive())
        self.assertTrue(fake.saw_stop)
        reopened = AppData(os.path.join(self.tmp, "appdata"))               # shutdown closed the store
        self.addCleanup(reopened.close)
        st = os.stat(wav)
        self.assertEqual(reopened.cached_fp(wav, st.st_size, st.st_mtime_ns)["fp"], wavinfo.wav_fingerprint(wav))

    # ---- playing and marks ------------------------------------------------------------

    def test_play_library_and_library_marks(self):
        self.populate()
        api = self.new_api()
        fake = FakeDecoder()
        with fake.installed():
            r = api.list_library()
            ids = {f["name"]: f["id"] for f in r["files"]}
            self.assertEqual(api.library_marks(ids["y.wav"]), {"ok": True, "marks": []})  # not indexed yet
            self.events.wait_done(r["scan_id"])
            w = api.play_library(ids["y.wav"])
            d = api.play_library(ids["x.dvf"])
        self.assertEqual((w["ok"], w["name"], w["imported"]), (True, "y.wav", 0))
        for res in (w, d):
            self.assertRegex(res["rec"], "^[0-9a-f]{16}$")
            self.assertEqual((res["marks"], res["reviewed"], res["backup"]),
                             ([], False, {"status": None, "detail": ""}))
        self.assertEqual(d["url"], "http://x/d.wav")
        self.assertEqual(self.server.made[0][0], "dvf")
        self.assertTrue(api.add_mark(d["rec"], 0.2, 0.5, "B", "whisper")["ok"])
        marks = api.library_marks(ids["x.dvf"])
        self.assertEqual([m["note"] for m in marks["marks"]], ["whisper"])
        self.assertFalse(api.play_library("0123456789abcdef")["ok"])
        self.assertFalse(api.play_library(None)["ok"])
        self.assertFalse(api.library_marks("0123456789abcdef")["ok"])
        os.remove(os.path.join(self.lib, "z.wav"))
        gone = api.play_library(ids["z.wav"])
        self.assertFalse(gone["ok"])
        self.assertNotIn(self.tmp, gone["error"])

    def test_play_library_imports_wav_markers(self):
        self.write("marked.wav", wavinfo.with_markers(wav_bytes(b"m"), [
            {"start": 0.1, "end": 0.3, "cls": "A", "note": "yes"}]))
        api = self.new_api()
        with mock.patch.object(api, "_library_worker"):                      # not indexed first
            fid = api.list_library()["files"][0]["id"]
        r = api.play_library(fid)
        self.assertEqual((r["ok"], r["imported"]), (True, 1))
        self.assertEqual([(m["cls"], m["note"]) for m in r["marks"]], [("A", "yes")])

    def test_without_a_store_files_are_listed_and_played(self):
        self.populate()
        api = self.new_api(store=None)
        r = api.list_library()
        self.assertEqual(len(r["files"]), 4)
        self.assertTrue(all(f["fp"] is None and f["marks"] == {"A": 0, "B": 0, "C": 0} for f in r["files"]))
        self.assertEqual(self.by_name(r)["y.wav"]["seconds"], 1.5)            # from the header
        self.assertIsNone(api._indexer)
        self.assertEqual(self.events.items, [])
        ids = {f["name"]: f["id"] for f in r["files"]}
        self.assertTrue(api.play_library(ids["z.wav"])["ok"])
        self.assertEqual(api.library_marks(ids["z.wav"]), {"ok": True, "marks": []})

    # ---- moved from the old "saved recordings" tests ------------------------------

    def test_header_lengths_upper_case_names_and_stable_ids(self):
        self.write("A/001_A_001_X.dvf", dvf_bytes())                          # 3 blocks: 2980 audio bytes
        self.write("A/session.WAV", wav_bytes(b"s", 2.5))
        api = self.new_api(store=None)
        r = api.list_library()
        got = {f["name"]: (f["type"], f["seconds"]) for f in r["files"]}
        self.assertEqual(got, {"001_A_001_X.dvf": ("dvf", round(2980 / 750, 1)), "session.WAV": ("wav", 2.5)})
        self.assertEqual([f["id"] for f in api.list_library()["files"]], [f["id"] for f in r["files"]])
        w = api.play_library(self.by_name(r)["session.WAV"]["id"])
        self.assertEqual((w["ok"], w["name"]), (True, "session.WAV"))

    def test_playing_a_dvf_without_a_working_decoder(self):
        self.write("x.dvf", dvf_bytes())
        api = self.new_api(store=None)
        fid = api.list_library()["files"][0]["id"]
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": None}):
            r = api.play_library(fid)
            self.assertFalse(r["ok"])
            self.assertIn(audio.status(), r["error"])
        missing_tables = types.ModuleType("openevp.decoders.sony_lpec")
        missing_tables.dvf_to_wav = lambda data: data

        def boom_check():
            raise RuntimeError("lpec_tables.json not found")
        missing_tables.check = boom_check
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": missing_tables}):
            r = api.play_library(fid)
        self.assertFalse(r["ok"])
        self.assertIn("could not be loaded", r["error"])
        self.assertEqual(self.server.made, [])


if __name__ == "__main__":
    unittest.main()
