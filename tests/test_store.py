import json
import multiprocessing
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import AppData, StoreReadOnly  # noqa: E402


def _mp_probe_read_only(folder, queue):
    """Run in a child process: report whether a second AppData on the same
    folder opens read-only."""
    store = AppData(folder)
    queue.put(store.read_only)
    store.close()


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
            self.assertFalse(rec["imported"])
            self.assertEqual(rec["backup"], {"status": None, "detail": ""})
            self.assertEqual(len(rec["marks"]), 1)
            self.assertIsNone(store.recording("nope"))

            store.set_imported("fp1")
            self.assertTrue(store.is_imported("fp1"))

            store.set_backup("fp1", "failed", "disk full")
            self.assertEqual(store.backup("fp1"), {"status": "failed", "detail": "disk full"})
            store.set_backup("fp1", "saved")
            self.assertEqual(store.backup("fp1"), {"status": "saved", "detail": ""})
            with self.assertRaises(ValueError):
                store.set_backup("fp1", "bogus")
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


class StoreMultiprocessTests(unittest.TestCase):
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
