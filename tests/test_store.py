import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import (AppData, StoreReadOnly, StoreUnavailable, _acquire_lock, _process_created,  # noqa: E402
                       _release_lock)


def _mp_probe_read_only(folder, queue):
    """Run in a child process: report whether a second AppData on the same
    folder opens read-only."""
    store = AppData(folder)
    queue.put(store.read_only)
    store.close()


def _mp_hold_lock(folder, ready, release):
    """Run in a child process: hold the folder's store until told to let go."""
    store = AppData(folder)
    ready.put((os.getpid(), store.read_only))
    release.wait(30)
    store.close()


def _dead_pid():
    """The process id of a process that has already exited."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class StoreTests(unittest.TestCase):
    def test_add_then_reload_persists_sorted_and_unique_ids(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            m1 = store.add_mark("fp1", 5.0, 6.0, "A", "hello")
            m2 = store.add_mark("fp1", 1.0, 2.0, "B", "world")
            self.assertNotEqual(m1["id"], m2["id"])
            store.close()

            reloaded = AppData(d)
            marks = reloaded.marks("fp1")
            self.assertEqual([m["id"] for m in marks], [m2["id"], m1["id"]])
            self.assertEqual(marks[0]["note"], "world")
            self.assertEqual(marks[1]["note"], "hello")
            reloaded.close()

    def test_validation_errors_write_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            with self.assertRaises(ValueError):
                store.add_mark("fp1", 1.0, 2.0, "D", "note")           # bad class
            with self.assertRaises(ValueError):
                store.add_mark("fp1", 2.0, 2.0, "A", "note")           # start >= end
            with self.assertRaises(ValueError):
                store.add_mark("fp1", -1.0, 2.0, "A", "note")          # negative start
            with self.assertRaises(ValueError):
                store.add_mark("fp1", 1.0, 20.0, "A", "note", duration=10.0)  # end > duration
            with self.assertRaises(ValueError):
                store.add_mark("fp1", 1.0, 2.0, "A", "x" * 501)        # note too long
            self.assertEqual(store.marks("fp1"), [])
            self.assertFalse(os.path.exists(os.path.join(d, "marks.json")))
            store.close()

    def test_update_and_delete(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            m = store.add_mark("fp1", 1.0, 2.0, "A", "note")

            updated = store.update_mark("fp1", m["id"], cls="B", note="changed")
            self.assertEqual(updated["cls"], "B")
            self.assertEqual(updated["note"], "changed")

            moved = store.update_mark("fp1", m["id"], start=0.5, end=0.8)
            self.assertAlmostEqual(moved["start"], 0.5)
            self.assertAlmostEqual(moved["end"], 0.8)

            with self.assertRaises(ValueError):
                store.update_mark("fp1", m["id"], start=1.0, end=1.02)  # too short
            with self.assertRaises(ValueError):
                store.update_mark("fp1", "no-such-id")

            self.assertFalse(store.delete_mark("fp1", "no-such-id"))
            self.assertTrue(store.delete_mark("fp1", m["id"]))
            self.assertEqual(store.marks("fp1"), [])
            store.close()

    def test_reviewed_toggle_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            store.add_mark("fp1", 1.0, 2.0, "A", "Hello There")
            store.add_mark("fp1", 3.0, 4.0, "A", "")
            store.add_mark("fp1", 5.0, 6.0, "B", "World")

            self.assertFalse(store.is_reviewed("fp1"))
            store.set_reviewed("fp1", True)
            self.assertTrue(store.is_reviewed("fp1"))

            summary = store.summary()
            self.assertEqual(summary["fp1"]["A"], 2)
            self.assertEqual(summary["fp1"]["B"], 1)
            self.assertEqual(summary["fp1"]["C"], 0)
            self.assertTrue(summary["fp1"]["reviewed"])
            self.assertEqual(summary["fp1"]["notes"], "hello there\nworld")
            store.close()

            reloaded = AppData(d)
            self.assertTrue(reloaded.is_reviewed("fp1"))
            reloaded.close()

    def test_corrupt_marks_json_is_set_aside_and_next_write_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "marks.json"), "w", encoding="utf-8") as f:
                f.write("{not json")

            store = AppData(d)
            self.assertEqual(store.marks("anything"), [])
            self.assertEqual(len(store.problems()), 1)
            corrupt = [n for n in os.listdir(d) if n.startswith("marks.json.corrupt-")]
            self.assertEqual(len(corrupt), 1)

            store.add_mark("fp1", 1.0, 2.0, "A", "note")
            with open(os.path.join(d, "marks.json"), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["version"], 1)
            self.assertEqual(len(data["recordings"]["fp1"]["marks"]), 1)
            store.close()

    def test_invalid_utf8_in_marks_json_is_set_aside_like_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "marks.json"), "wb") as f:
                f.write(b"{\"version\": 1, \"recordings\": {\"" + bytes([0xFF, 0xFE, 0x80]) + b"\": {}}}")

            store = AppData(d)                      # must not raise UnicodeDecodeError
            self.assertEqual(store.marks("anything"), [])
            self.assertEqual(len(store.problems()), 1)
            corrupt = [n for n in os.listdir(d) if n.startswith("marks.json.corrupt-")]
            self.assertEqual(len(corrupt), 1)

            store.add_mark("fp1", 1.0, 2.0, "A", "note")
            with open(os.path.join(d, "marks.json"), encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["recordings"]["fp1"]["marks"]), 1)
            store.close()

    def test_import_marks_parses_labels_and_runs_once(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            markers = [
                {"start": 1.0, "end": 1.0, "note": "EVP A: get out"},
                {"start": 2.0, "end": 3.0, "note": "door slam"},
                {"start": 4.0, "end": 4.0, "note": "EVP B"},
            ]
            added = store.import_marks("fp1", markers, "recording 1", 10.0)
            self.assertEqual(added, 3)

            marks = store.marks("fp1")
            self.assertEqual(marks[0]["cls"], "A")
            self.assertEqual(marks[0]["note"], "get out")
            self.assertEqual(marks[0]["start"], marks[0]["end"])   # imported point marker
            self.assertEqual(marks[1]["cls"], "C")
            self.assertEqual(marks[1]["note"], "door slam")
            self.assertEqual(marks[2]["cls"], "B")
            self.assertEqual(marks[2]["note"], "")                # bare "EVP B", no colon
            self.assertTrue(store.is_imported("fp1"))

            again = store.import_marks("fp1", markers, "recording 1", 10.0)
            self.assertEqual(again, 0)
            self.assertEqual(len(store.marks("fp1")), 3)

            # 0 importable markers still marks the fp imported
            added2 = store.import_marks("fp2", [], "recording 2", 5.0)
            self.assertEqual(added2, 0)
            self.assertTrue(store.is_imported("fp2"))
            store.close()

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            self.assertIsNone(store.get_setting("save_folder"))
            store.set_setting("save_folder", "C:/EVPs")
            store.set_setting("library_folder", "C:/Library")
            store.close()

            reloaded = AppData(d)
            self.assertEqual(reloaded.get_setting("save_folder"), "C:/EVPs")
            self.assertEqual(reloaded.get_setting("library_folder"), "C:/Library")
            self.assertEqual(reloaded.get_setting("missing", "default"), "default")
            reloaded.close()

    def test_index_cached_fp_flush_and_prune(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            path = os.path.join(d, "rec.wav")
            self.assertIsNone(store.cached_fp(path, 100, 1000))
            store.remember_fp(path, 100, 1000, "abcd1234", 12.5)
            entry = store.cached_fp(path, 100, 1000)
            self.assertEqual(entry["fp"], "abcd1234")
            self.assertIsNone(store.cached_fp(path, 100, 1001))   # mtime differs -> miss
            self.assertIsNone(store.cached_fp(path, 99, 1000))    # size differs -> miss

            store.flush_index()
            self.assertTrue(os.path.exists(os.path.join(d, "index.json")))
            store.close()

            reloaded = AppData(d)
            self.assertEqual(reloaded.cached_fp(path, 100, 1000)["fp"], "abcd1234")
            reloaded.prune_index(d, seen_keys=set())
            reloaded.flush_index()
            self.assertIsNone(reloaded.cached_fp(path, 100, 1000))
            reloaded.close()

    def test_recording_imported_and_backup_fields(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            store.add_mark("fp1", 1.0, 2.0, "A", "note", name="Rec 1", duration=10.0)

            rec = store.recording("fp1")
            self.assertEqual(rec["name"], "Rec 1")
            self.assertEqual(rec["duration"], 10.0)
            self.assertFalse(rec["reviewed"])
            self.assertTrue(rec["imported"])            # marked in the app: never auto-imports
            self.assertEqual(rec["backup"], {"status": None, "detail": ""})
            self.assertEqual(len(rec["marks"]), 1)
            self.assertIsNone(store.recording("nope"))

            store.set_reviewed("fp2", True)
            self.assertFalse(store.recording("fp2")["imported"])
            store.set_imported("fp2")
            self.assertTrue(store.is_imported("fp2"))

            store.set_backup("fp1", "failed", "disk full")
            self.assertEqual(store.backup("fp1"), {"status": "failed", "detail": "disk full"})
            store.set_backup("fp1", "saved")
            self.assertEqual(store.backup("fp1"), {"status": "saved", "detail": ""})
            with self.assertRaises(ValueError):
                store.set_backup("fp1", "bogus")
            store.close()

    def test_backup_paths_are_kept_moved_and_reloaded(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            save = os.path.join(d, "Save")
            dvf, wav = os.path.join(save, "A", "x.dvf"), os.path.join(save, "A", "x.wav")
            store.set_backup("fp1", "saved", "Saved as x.dvf", [dvf, wav])
            store.set_backup("fp2", "saved", "old style")                 # no paths known
            store.set_backup("fp3", "failed", "no", [dvf])
            self.assertEqual(store.backup("fp1"), {"status": "saved", "detail": "Saved as x.dvf"})
            self.assertEqual(store.recording("fp1")["backup"], {"status": "saved", "detail": "Saved as x.dvf"})
            self.assertEqual(store.backup_record("fp1")["paths"], [dvf, wav])
            self.assertEqual(store.saved_backups(), {"fp1": [dvf, wav], "fp2": []})
            moved = os.path.join(d, "Save 2")
            self.assertEqual(store.move_backup_paths(save, moved),        # fp3's path follows too
                             {"fp1": [dvf, wav], "fp3": [dvf]})
            self.assertEqual(store.move_backup_paths(os.path.join(d, "elsewhere"), save), {})
            self.assertEqual(store.backup_record("fp1")["paths"],
                             [os.path.join(moved, "A", "x.dvf"), os.path.join(moved, "A", "x.wav")])
            # an undo restores exactly the changed records, never by prefix
            other = os.path.join(moved, "A", "y.dvf")
            store.set_backup("fp4", "saved", "", [other])
            before = store.move_backup_paths(os.path.join(moved, "A", "x.dvf"), os.path.join(d, "T", "x.dvf"))
            self.assertEqual(set(before), {"fp1", "fp3"})
            store.set_backup("fp5", "saved", "", [os.path.join(d, "T", "x.dvf")])     # stale, unrelated
            store.restore_backup_paths(before)
            self.assertEqual(store.backup_record("fp1")["paths"][0], os.path.join(moved, "A", "x.dvf"))
            self.assertEqual(store.backup_record("fp5")["paths"], [os.path.join(d, "T", "x.dvf")])
            self.assertEqual(store.backup_record("fp4")["paths"], [other])
            with self.assertRaises(ValueError):
                store.set_backups({"fp1": {"status": "saved", "detail": "", "paths": [5]}})
            store.set_backups({"fp1": {"status": "failed", "detail": "gone"}, "fp2": {"status": "failed"}})
            self.assertEqual(store.backup_record("fp1"), {"status": "failed", "detail": "gone", "paths": []})
            store.close()
            reloaded = AppData(d)
            self.assertEqual(reloaded.backup_record("fp3")["paths"], [os.path.join(moved, "A", "x.dvf")])
            reloaded.close()

    def test_marks_file_without_backup_paths_loads(self):
        with tempfile.TemporaryDirectory() as d:
            data = {"version": 1, "recordings": {
                "fp1": {"marks": [], "reviewed": False, "name": "", "duration": None, "imported": False,
                        "backup": {"status": "saved", "detail": "Saved as x.dvf"}},
                "fp2": {"marks": [], "reviewed": False, "name": "", "duration": None, "imported": False,
                        "backup": {"status": "saved", "detail": "", "paths": "not a list"}}}}
            with open(os.path.join(d, "marks.json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
            store = AppData(d)
            self.assertEqual(store.backup("fp1"), {"status": "saved", "detail": "Saved as x.dvf"})
            self.assertEqual(store.saved_backups(), {"fp1": [], "fp2": []})
            self.assertEqual(store.problems(), [])
            store.close()

    def test_concurrency_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)

            def worker(n):
                for i in range(50):
                    store.add_mark(f"fp{n}", float(i), float(i) + 1, "A", "")

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            store.close()

            reloaded = AppData(d)
            total = sum(len(reloaded.marks(f"fp{n}")) for n in range(4))
            self.assertEqual(total, 200)
            reloaded.close()

    def test_a_closed_store_refuses_writes(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            store.add_mark("fp1", 1.0, 2.0, "A", "")
            store.close()
            for write in (lambda: store.add_mark("fp1", 3.0, 4.0, "B", ""),
                          lambda: store.set_reviewed("fp1", True),
                          lambda: store.set_setting("save_folder", d),
                          lambda: store.remember_fp(os.path.join(d, "x.wav"), 1, 1, "fp", 1.0)):
                with self.assertRaises(StoreUnavailable) as cm:
                    write()
                self.assertNotIn(d, str(cm.exception))
            store.close()                                   # closing twice is fine
            reloaded = AppData(d)
            self.assertEqual(len(reloaded.marks("fp1")), 1)
            reloaded.close()

    def test_a_marked_recording_never_imports_embedded_markers(self):
        """Deleting every mark must not let the markers in OpenEVP's own WAVs
        (backup, export) bring them back."""
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            mark = store.add_mark("fp1", 1.0, 2.0, "A", "manual")
            store.delete_mark("fp1", mark["id"])
            markers = [{"start": 1.0, "end": 2.0, "note": "EVP A: manual"}]
            self.assertEqual(store.import_marks("fp1", markers, "x.wav", 10.0), 0)
            self.assertEqual(store.marks("fp1"), [])
            store.close()

    def test_second_appdata_same_folder_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            self.assertFalse(first.read_only)
            second = AppData(d)
            self.assertTrue(second.read_only)
            with self.assertRaises(StoreReadOnly):
                second.add_mark("fp1", 1.0, 2.0, "A", "note")
            self.assertEqual(second.marks("fp1"), [])   # reads still work
            second.close()
            first.close()

    # ---- Fix round 1 -----------------------------------------------------------

    def test_import_marks_does_not_duplicate_when_marks_already_exist(self):
        """A recording with marks but no `imported` flag (marks.json written
        before add_mark set the flag) must not have embedded markers imported
        on top of its marks."""
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            store.add_mark("fp1", 1.0, 2.0, "A", "manual mark")
            store._data["recordings"]["fp1"]["imported"] = False     # as older data had it
            self.assertFalse(store.is_imported("fp1"))

            markers = [{"start": 3.0, "end": 3.0, "note": "EVP A: get out"}]
            added = store.import_marks("fp1", markers, "recording 1", 10.0)
            self.assertEqual(added, 0)
            self.assertTrue(store.is_imported("fp1"))       # flagged so it's never retried
            self.assertEqual(len(store.marks("fp1")), 1)    # only the original manual mark
            store.close()

    def test_failed_set_aside_blocks_future_writes_and_leaves_file_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "marks.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            real_replace = os.replace

            def flaky_replace(src, dst):
                if ".corrupt-" in os.path.basename(dst) or ".future-" in os.path.basename(dst):
                    raise OSError("simulated: cannot rename")
                return real_replace(src, dst)

            with mock.patch("app.store.os.replace", side_effect=flaky_replace):
                store = AppData(d)
            problems = store.problems()
            self.assertTrue(any("could not be set aside" in p for p in problems), problems)
            with self.assertRaises(StoreUnavailable):
                store.add_mark("fp1", 1.0, 2.0, "A", "note")
            # the original corrupt file was never moved and never overwritten
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "{not json")
            self.assertEqual([n for n in os.listdir(d) if "corrupt" in n], [])
            store.close()

    def test_read_only_instance_never_renames_bad_files(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "marks.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            lock_file, locked = _acquire_lock(os.path.join(d, ".lock"))  # hold the lock ourselves
            self.assertTrue(locked)
            try:
                store = AppData(d)
                self.assertTrue(store.read_only)
                self.assertTrue(any("read-only" in p for p in store.problems()), store.problems())
                self.assertEqual([n for n in os.listdir(d) if "corrupt" in n], [])
                with open(os.path.join(d, "marks.json"), encoding="utf-8") as f:
                    self.assertEqual(f.read(), "{not json")
                store.close()
            finally:
                _release_lock(lock_file)

    def test_permission_error_on_read_is_unavailable_not_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "marks.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"version": 1, "recordings": {}}')
            real_open = open

            def flaky_open(file, *a, **kw):
                if os.path.abspath(str(file)) == os.path.abspath(path):
                    raise PermissionError("simulated: access denied")
                return real_open(file, *a, **kw)

            with mock.patch("builtins.open", side_effect=flaky_open):
                store = AppData(d)
            problems = store.problems()
            self.assertTrue(any("could not be read right now" in p for p in problems), problems)
            self.assertEqual([n for n in os.listdir(d) if "corrupt" in n or "future" in n], [])
            with self.assertRaises(StoreUnavailable):
                store.add_mark("fp1", 1.0, 2.0, "A", "note")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"version": 1, "recordings": {}})
            store.close()

    def test_add_mark_never_shrinks_duration_below_existing_marks(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            store.add_mark("fp1", 8.0, 9.0, "A", "note", duration=10.0)
            # A later, smaller duration must not drop below the furthest mark end (9.0)
            # already on record, even though 3.0 alone would have been a valid bound
            # for the mark just being added (end 2.0).
            store.add_mark("fp1", 1.0, 2.0, "A", "note", duration=3.0)
            self.assertGreaterEqual(store.recording("fp1")["duration"], 9.0)
            self.assertEqual(len(store.marks("fp1")), 2)
            store.close()

            reloaded = AppData(d)
            self.assertEqual(len(reloaded.marks("fp1")), 2)   # nothing lost on reload either
            reloaded.close()

    def test_load_epsilon_tolerance_for_duration_vs_mark_end(self):
        with tempfile.TemporaryDirectory() as d:
            marks_path = os.path.join(d, "marks.json")
            blank_backup = {"status": None, "detail": ""}
            data = {"version": 1, "recordings": {
                "fp1": {"marks": [{"id": "a" * 12, "start": 1.0, "end": 5.004, "cls": "A",
                                    "note": "", "created": "2024-01-01T00:00:00"}],
                        "reviewed": False, "name": "", "duration": 5.0,
                        "imported": False, "backup": blank_backup},
                "fp2": {"marks": [{"id": "b" * 12, "start": 1.0, "end": 6.0, "cls": "A",
                                    "note": "", "created": "2024-01-01T00:00:00"}],
                        "reviewed": False, "name": "", "duration": 5.0,
                        "imported": False, "backup": blank_backup},
            }}
            with open(marks_path, "w", encoding="utf-8") as f:
                json.dump(data, f)

            store = AppData(d)
            self.assertEqual(len(store.marks("fp1")), 1)   # 0.004s over: within epsilon, kept
            self.assertEqual(len(store.marks("fp2")), 0)   # 1.0s over: dropped
            self.assertEqual(len(store.problems()), 1)
            store.close()

    def test_index_can_cache_a_failure_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            path = os.path.join(d, "bad.dvf")
            store.remember_fp(path, 123, 456, None, None, error="could not decode")
            entry = store.cached_fp(path, 123, 456)
            self.assertEqual(entry["error"], "could not decode")
            self.assertIsNone(entry["fp"])
            store.flush_index()
            store.close()

            reloaded = AppData(d)
            entry2 = reloaded.cached_fp(path, 123, 456)
            self.assertEqual(entry2["error"], "could not decode")
            reloaded.close()

    def test_prune_index_normalizes_seen_keys(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            path = os.path.join(d, "rec.wav")
            store.remember_fp(path, 100, 1000, "abcd1234", 12.5)
            store.prune_index(d, seen_keys={path.upper()})   # raw, differently-cased path
            self.assertIsNotNone(store.cached_fp(path, 100, 1000))
            store.close()

    def test_prune_index_at_a_drive_root(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            try:
                root = os.path.abspath(os.sep)                 # e.g. C:\ -- already ends in a separator
                gone = os.path.join(root, "openevp-no-such-dir", "gone.wav")
                kept = os.path.join(root, "kept.wav")
                store.remember_fp(gone, 1, 2, "aa", 1.0)
                store.remember_fp(kept, 1, 2, "bb", 1.0)
                store.prune_index(root, seen_keys={kept})
                self.assertIsNone(store.cached_fp(gone, 1, 2))
                self.assertIsNotNone(store.cached_fp(kept, 1, 2))
            finally:
                store.close()

    def test_failed_write_leaves_memory_unchanged_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            store.add_mark("fp1", 1.0, 2.0, "A", "first")
            before = store.marks("fp1")

            with mock.patch("app.store.os.replace", side_effect=OSError("simulated disk full")):
                with self.assertRaises(StoreUnavailable):
                    store.add_mark("fp1", 5.0, 6.0, "B", "second")

            self.assertEqual(store.marks("fp1"), before)     # memory unchanged
            leftovers = [n for n in os.listdir(d)
                         if n not in ("marks.json", "settings.json", "index.json", ".lock", ".lock.owner")]
            self.assertEqual(leftovers, [])                  # no leftover temp file
            store.close()


    def test_move_index_prefix_rekeys_a_folder_or_one_file(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(os.path.join(d, "appdata"))
            lib = os.path.join(d, "lib")
            old, new = os.path.join(lib, "Old Mill"), os.path.join(lib, "Mill 2")
            store.remember_fp(os.path.join(old, "a.wav"), 1, 10, "fpa", 1.0)
            store.remember_fp(os.path.join(old, "sub", "b.dvf"), 2, 20, "fpb", 2.0)
            store.remember_fp(os.path.join(lib, "Old Mill 2", "c.wav"), 3, 30, "fpc", 3.0)   # a sibling, not under
            store.remember_fp(os.path.join(new, "a.wav"), 9, 90, "stale", 9.0)              # replaced
            store.flush_index()
            self.assertEqual(store.move_index_prefix(old, new), 2)
            self.assertIsNone(store.cached_fp(os.path.join(old, "a.wav"), 1, 10))
            self.assertEqual(store.cached_fp(os.path.join(new, "a.wav"), 1, 10)["fp"], "fpa")
            self.assertEqual(store.cached_fp(os.path.join(new, "sub", "b.dvf"), 2, 20)["fp"], "fpb")
            self.assertEqual(store.cached_fp(os.path.join(lib, "Old Mill 2", "c.wav"), 3, 30)["fp"], "fpc")
            # one file
            self.assertEqual(store.move_index_prefix(os.path.join(new, "a.wav"), os.path.join(lib, "a (2).wav")), 1)
            self.assertEqual(store.cached_fp(os.path.join(lib, "a (2).wav"), 1, 10)["fp"], "fpa")
            self.assertEqual(set(store.index_under(new)), {os.path.normcase(os.path.join(new, "sub", "b.dvf"))})
            self.assertEqual(store.move_index_prefix(os.path.join(d, "nothing"), new), 0)
            store.close()                                   # flushes the re-keyed index
            reopened = AppData(os.path.join(d, "appdata"))
            self.assertEqual(reopened.cached_fp(os.path.join(new, "sub", "b.dvf"), 2, 20)["fp"], "fpb")
            reopened.close()

    def test_move_index_prefix_refuses_on_a_read_only_store(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            second = AppData(d)
            try:
                with self.assertRaises(StoreReadOnly):
                    second.move_index_prefix(os.path.join(d, "a"), os.path.join(d, "b"))
            finally:
                second.close()
                store.close()


class StoreLockTests(unittest.TestCase):
    """Why a store is read-only, waiting for the lock, and getting it later."""

    def test_lock_file_that_cannot_be_opened_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, ".lock"))                  # can't be opened as a file
            store = AppData(d)
            try:
                self.assertTrue(store.read_only)
                reason = store.read_only_reason
                self.assertTrue(reason.startswith("OpenEVP couldn't open its data lock file (.lock, "), reason)
                self.assertTrue(reason.endswith("marks can't be saved right now."), reason)
                self.assertNotIn(d, reason)                     # the file's name, never its path
                self.assertNotIn("Another OpenEVP", reason)
                self.assertEqual(store.problems()[0], reason)
                with self.assertRaises(StoreReadOnly) as cm:
                    store.add_mark("fp1", 1.0, 2.0, "A", "")
                self.assertEqual(str(cm.exception), reason)
            finally:
                store.close()

    def test_held_by_a_live_openevp_names_its_process(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            second = AppData(d)
            try:
                with open(os.path.join(d, ".lock.owner"), encoding="utf-8") as f:
                    pid, exe, created = f.read().split()
                self.assertEqual((pid, exe), (str(os.getpid()), os.path.basename(sys.executable)))
                self.assertEqual(int(created), _process_created(os.getpid()))
                self.assertTrue(second.read_only)
                self.assertEqual(second.read_only_reason,
                                 f"Another OpenEVP (process {os.getpid()}) is open; marks can only be changed "
                                 "there. If you don't see its window, it may still be closing.")
                self.assertIsNone(first.read_only_reason)
                self.assertEqual(first.problems(), [])
            finally:
                second.close()
                first.close()
            self.assertFalse(os.path.exists(os.path.join(d, ".lock.owner")))   # the holder removes it

    def test_held_by_a_dead_or_foreign_owner_says_another_program(self):
        other = "OpenEVP couldn't lock its data (held by another program), so marks can't be saved right now."
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            owner = os.path.join(d, ".lock.owner")
            try:
                exe = os.path.basename(sys.executable)
                created = _process_created(os.getpid())
                for text in (f"{_dead_pid()}\n{exe}\n",                                    # its process is gone
                             f"{os.getpid()}\nnotepad.exe\n",                           # not the program recorded
                             f"{os.getpid()}\n{exe}\n{created + 1}\n",                 # a reused process id
                             "garbage", None):                                           # unreadable, missing
                    if text is None:
                        os.remove(owner)
                    else:
                        with open(owner, "w", encoding="utf-8") as f:
                            f.write(text)
                    second = AppData(d)
                    try:
                        self.assertTrue(second.read_only)
                        self.assertEqual(second.read_only_reason, other, text)
                    finally:
                        second.close()
            finally:
                first.close()

    def test_owner_file_without_a_creation_time_is_still_believed(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            try:
                with open(os.path.join(d, ".lock.owner"), "w", encoding="utf-8") as f:
                    f.write(f"{os.getpid()}\n{os.path.basename(sys.executable)}\n")   # the older format
                second = AppData(d)
                try:
                    self.assertIn(f"Another OpenEVP (process {os.getpid()}) is open", second.read_only_reason)
                finally:
                    second.close()
            finally:
                first.close()

    def test_a_blocking_callback_never_holds_up_close(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            second = AppData(d, retry_interval=0.05)
            entered, forever = threading.Event(), threading.Event()
            self.addCleanup(forever.set)                        # lets the stuck callback end after the test

            def stuck():
                entered.set()
                forever.wait()                                  # like evaluate_js on a closing window
            second.watch_lock(stuck)
            first.close()
            self.assertTrue(entered.wait(5), "callback never ran")
            self.assertFalse(second.read_only)
            t0 = time.monotonic()
            second.close()
            self.assertLess(time.monotonic() - t0, 2)
            self.assertFalse(second._retry_thread.is_alive())
            third = AppData(d)                                  # the lock was let go
            try:
                self.assertFalse(third.read_only)
            finally:
                third.close()

    def test_startup_waits_for_a_closing_holder(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            timer = threading.Timer(0.4, first.close)
            timer.start()
            t0 = time.monotonic()
            try:
                second = AppData(d, lock_wait=5.0, lock_poll=0.05)
            finally:
                timer.join()
            try:
                self.assertFalse(second.read_only)
                self.assertLess(time.monotonic() - t0, 4.0)
                second.add_mark("fp1", 1.0, 2.0, "A", "")
            finally:
                second.close()

    def test_startup_gives_up_after_the_wait(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            t0 = time.monotonic()
            second = AppData(d, lock_wait=0.3, lock_poll=0.05)
            try:
                self.assertTrue(second.read_only)
                self.assertGreaterEqual(time.monotonic() - t0, 0.2)
            finally:
                second.close()
                first.close()

    def test_background_retry_becomes_writable_and_reloads(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            second = AppData(d, retry_interval=0.05)
            got = threading.Event()
            try:
                self.assertTrue(second.read_only)
                second.watch_lock(got.set)
                time.sleep(0.2)
                self.assertTrue(second.read_only)               # still held: still read-only
                self.assertFalse(got.is_set())
                mark = first.add_mark("fp1", 1.0, 2.0, "B", "written by the other one")
                first.set_setting("save_folder", "X:\\evp")
                first.remember_fp(os.path.join(d, "a.wav"), 1, 2, "fpa", 3.0)
                first.close()                                   # flushes the index, lets go
                self.assertTrue(got.wait(5), "never became writable")
                self.assertFalse(second.read_only)
                self.assertIsNone(second.read_only_reason)
                self.assertEqual(second.problems(), [])
                self.assertEqual([m["id"] for m in second.marks("fp1")], [mark["id"]])
                self.assertEqual(second.get_setting("save_folder"), "X:\\evp")
                self.assertEqual(second.cached_fp(os.path.join(d, "a.wav"), 1, 2)["fp"], "fpa")
                second.add_mark("fp1", 3.0, 4.0, "A", "")        # writable now
                with open(os.path.join(d, ".lock.owner"), encoding="utf-8") as f:
                    self.assertEqual(f.read().split()[0], str(os.getpid()))
            finally:
                second.close()
            self.assertFalse(second._retry_thread.is_alive())
            reopened = AppData(d)
            self.assertEqual(len(reopened.marks("fp1")), 2)     # nothing the other one wrote was lost
            reopened.close()

    def test_background_retry_after_the_lock_file_opens_again(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, ".lock"))
            store = AppData(d, retry_interval=0.05)
            got = threading.Event()
            try:
                store.watch_lock(got.set)
                time.sleep(0.15)
                self.assertTrue(store.read_only)
                os.rmdir(os.path.join(d, ".lock"))
                self.assertTrue(got.wait(5))
                self.assertFalse(store.read_only)
                store.add_mark("fp1", 1.0, 2.0, "A", "")
            finally:
                store.close()

    def test_close_stops_and_joins_the_retry(self):
        with tempfile.TemporaryDirectory() as d:
            first = AppData(d)
            second = AppData(d, retry_interval=60)
            calls = []
            second.watch_lock(lambda: calls.append(1))
            thread = second._retry_thread
            self.assertTrue(thread.is_alive())
            t0 = time.monotonic()
            second.close()
            self.assertLess(time.monotonic() - t0, 5)
            self.assertFalse(thread.is_alive())
            first.close()
            self.assertEqual(calls, [])
            self.assertTrue(second.read_only)                   # a closed store never takes the lock
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)                                  # writable: nothing to watch
            store.watch_lock()
            self.assertIsNone(store._retry_thread)
            store.close()


class StoreMultiprocessTests(unittest.TestCase):
    def test_held_by_another_process_names_it_until_it_closes(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = multiprocessing.get_context("spawn")
            ready, release = ctx.Queue(), ctx.Event()
            p = ctx.Process(target=_mp_hold_lock, args=(d, ready, release))
            p.start()
            try:
                pid, child_read_only = ready.get(timeout=30)
                self.assertFalse(child_read_only)
                store = AppData(d, retry_interval=0.05)
                got = threading.Event()
                try:
                    self.assertTrue(store.read_only)
                    self.assertIn(f"Another OpenEVP (process {pid}) is open", store.read_only_reason)
                    store.watch_lock(got.set)
                    release.set()
                    self.assertTrue(got.wait(15), "never became writable")
                    self.assertFalse(store.read_only)
                finally:
                    store.close()
            finally:
                release.set()
                p.join(timeout=15)
                ready.close()
                ready.join_thread()

    def test_second_process_on_same_folder_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            store = AppData(d)
            ctx = multiprocessing.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=_mp_probe_read_only, args=(d, q))
            p.start()
            try:
                result = q.get(timeout=15)
            finally:
                p.join(timeout=15)
            store.close()
            self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
