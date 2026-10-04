"""Deleting recordings and clips in the EVP Library: Api.delete_info and
Api.delete_files put a recording's files (a .dvf and its .wav, in one folder)
in the Recycle Bin together -- never deleted for good -- keep their marks,
refuse while a second window, an export or a backup runs, report the files that
could not go, and drop what the caches remember of the ones that went."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_folders import FolderApiBase  # noqa: E402
from test_library import DECODED, FakeDecoder, dvf_bytes, wav_bytes  # noqa: E402
from app import backend, folders, library_ops  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from app.store import AppData, StoreUnavailable  # noqa: E402


class DeleteFilesTests(FolderApiBase):
    def recycled_names(self):
        return [os.path.basename(p) for p in self.recycled]

    def test_a_dvf_and_its_wav_go_together_to_the_recycle_bin(self):
        self.write("Old Mill/001_A_003.dvf", dvf_bytes())
        self.write("Old Mill/001_A_003.wav", DECODED)
        self.write("Old Mill/other.wav", wav_bytes(b"other"))
        self.write("Asylum/001_A_003.wav", DECODED)       # the same audio in another folder: stays
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
            dvf = next(f["id"] for f in r["files"] if f["name"] == "001_A_003.dvf")
            info = api.delete_info([dvf])                          # the row's main file only
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["names"], ["001_A_003.dvf", "001_A_003.wav"])
            self.assertEqual((info["recordings"], info["clips"], info["marks"], info["unchecked"]), (1, 0, 0, 0))
            with mock.patch.object(shutil, "rmtree", side_effect=AssertionError("never")), \
                    mock.patch.object(os, "remove", side_effect=AssertionError("never")), \
                    mock.patch.object(os, "unlink", side_effect=AssertionError("never")):
                res = api.delete_files(info["ids"])
        self.assertEqual(res, {"ok": True, "deleted": ["001_A_003.dvf", "001_A_003.wav"], "failed": [],
                               "split": [], "backups": 0})
        self.assertEqual(self.recycled_names(), ["001_A_003.dvf", "001_A_003.wav"])
        self.assertEqual(self.names("Old Mill"), ["other.wav"])
        self.assertEqual(self.names("Asylum"), ["001_A_003.wav"])
        self.assertFalse(api._busy.locked())
        self.assertFalse(api._fs_op)

    def test_a_copy_missing_from_the_ids_is_refused(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", DECODED)
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
            res = api.delete_files([self.file(r, "x.dvf")])        # its .wav was not in the dialog
        self.assertEqual(res["error"], backend.LIB_CHANGED)
        self.assertEqual(self.recycled, [])
        self.assertEqual(self.names(), ["x.dvf", "x.wav"])

    def test_several_recordings_and_a_clip(self):
        self.write("a.wav", wav_bytes(b"a"))
        self.write("b.wav", wav_bytes(b"b"))
        clips = os.path.join(self.lib, "Clips")
        library_ops._make_clips_folder(clips)
        self.write("Clips/a EVP 1.wav", wav_bytes(b"clip"))
        api = self.new_api()
        r = self.index(api)
        ids = [self.file(r, n) for n in ("a.wav", "b.wav", "a EVP 1.wav")]
        info = api.delete_info(ids)
        self.assertEqual((info["recordings"], info["clips"], len(info["ids"])), (2, 1, 3))
        res = api.delete_files(info["ids"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["deleted"]), ["a EVP 1.wav", "a.wav", "b.wav"])
        self.assertEqual(self.names(), ["Clips"])
        self.assertEqual(os.listdir(clips), [library_ops.CLIPS_MARKER])

    def test_marks_are_counted_and_kept(self):
        self.write("y.wav", wav_bytes(b"y"))
        self.write("z.wav", wav_bytes(b"z"))
        api = self.new_api()
        r = self.index(api)
        fps = {f["name"]: f["fp"] for f in r["files"]}
        self.store.add_mark(fps["y.wav"], 0.1, 0.3, "A", "voice")
        self.store.add_mark(fps["y.wav"], 0.4, 0.5, "B", "")
        self.store.add_mark(fps["z.wav"], 0.1, 0.2, "C", "")
        self.store.add_question(fps["y.wav"], 0.2, "Is anyone here?")   # Live's question log: kept the same way
        info = api.delete_info([self.file(r, "y.wav"), self.file(r, "z.wav")])
        self.assertEqual((info["marks"], info["with_evps"]), (3, 2))
        self.assertTrue(api.delete_files(info["ids"])["ok"])
        self.assertEqual(len(self.store.marks(fps["y.wav"])), 2)      # in marks.json still
        self.assertEqual(len(self.store.marks(fps["z.wav"])), 1)
        self.assertEqual(len(self.store.questions(fps["y.wav"])), 1)
        # Restored from the Recycle Bin, the recording has its marks again.
        shutil.move(os.path.join(self.tmp, "bin-1"), os.path.join(self.lib, "y.wav"))
        r = self.index(api)
        self.assertEqual(r["files"][0]["marks"], {"A": 1, "B": 1, "C": 0})
        self.assertEqual([q["text"] for q in self.store.questions(fps["y.wav"])], ["Is anyone here?"])

    def test_caches_are_dropped(self):
        path = self.write("y.wav", wav_bytes(b"y"))
        self.write("keep.wav", wav_bytes(b"k"))
        api = self.new_api()
        r = self.index(api)
        fid = self.file(r, "y.wav")
        key = os.path.normcase(os.path.abspath(path))
        self.assertIsNotNone(self.store.cached_fp(path, os.stat(path).st_size, os.stat(path).st_mtime_ns))
        st = os.stat(path)
        api._headers[key] = (st.st_size, st.st_mtime_ns, None)
        library_ops._sniffed[key] = (st.st_size, st.st_mtime_ns, True)
        api._fp_session[key] = (st.st_size, st.st_mtime_ns, {})
        self.server.forget_files = mock.Mock()
        self.assertTrue(api.delete_files([fid])["ok"])
        self.assertNotIn(key, self.store.index_under(self.lib))
        self.assertNotIn(key, api._headers)
        self.assertNotIn(key, library_ops._sniffed)
        self.assertNotIn(key, api._fp_session)
        self.assertIsNone(api._library_file(fid))
        self.server.forget_files.assert_called_once_with([os.path.abspath(path)])
        with open(os.path.join(self.tmp, "appdata", "index.json"), encoding="utf-8") as f:
            self.assertNotIn("y.wav", f.read())                     # flushed

    def test_the_audio_server_forgets_a_deleted_file(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        server = AudioServer(lambda key: b"", cache.name)
        server.start()
        self.addCleanup(server.stop)
        self.write("y.wav", wav_bytes(b"y"))
        self.write("z.wav", wav_bytes(b"z"))
        api = self.new_api(server=server)
        r = self.index(api)
        y = api.play_library(self.file(r, "y.wav"))
        z = api.play_library(self.file(r, "z.wav"))                  # the player's now (pinned)
        self.assertTrue(api.delete_files([self.file(r, "y.wav")])["ok"])
        self.assertFalse(server.serves(y["url"]))
        self.assertTrue(server.serves(z["url"]))

    def test_refusals(self):
        self.write("A/a.wav", wav_bytes(b"a"))
        api = self.new_api()
        self.assertEqual(api.delete_files(["x"])["error"], backend.LIB_CHANGED)     # not listed yet
        self.assertEqual(api.delete_info(["x"])["error"], backend.LIB_CHANGED)
        r = api.list_library()
        a = self.file(r, "a.wav")
        for bad in ([], "a", [1], None):
            self.assertFalse(api.delete_files(bad)["ok"])
            self.assertFalse(api.delete_info(bad)["ok"])
        self.assertEqual(api.delete_files(["nope"])["error"], backend.LIB_CHANGED)
        api._busy.acquire()                                               # an export, a WAV with marks, clips
        try:
            self.assertEqual(api.delete_files([a])["error"], backend.FS_BUSY)
        finally:
            api._busy.release()
        with mock.patch.object(api, "backing_up", return_value=True):     # a recorder backup
            self.assertEqual(api.delete_files([a])["error"], backend.FS_BUSY)
        with mock.patch.object(backend.folders, "inside", return_value=False):
            self.assertEqual(api.delete_files([a])["error"], backend.LIB_CHANGED)
        busy = mock.Mock()
        busy.is_alive.return_value = True
        api._indexer = busy
        with mock.patch.object(library_ops, "FS_WAIT", 0.01):
            self.assertEqual(api.delete_files([a])["error"], backend.INDEXER_BUSY)
        api._indexer = None
        api._stop.set()
        self.assertEqual(api.delete_files([a])["error"], backend.CLOSING)
        api._stop.clear()
        self.assertEqual(self.recycled, [])
        self.assertEqual(self.names("A"), ["a.wav"])
        self.assertFalse(api._busy.locked())
        os.remove(os.path.join(self.lib, "A", "a.wav"))
        self.assertEqual(api.delete_info([a])["error"], "a.wav is no longer there. Refresh the list.")
        self.assertIn("no longer there", api.delete_files([a])["error"])

    def test_a_second_window_deletes_nothing(self):
        self.write("y.wav", wav_bytes(b"y"))
        second = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(second.close)
        self.assertTrue(second.read_only)
        api = self.new_api(store=second)
        r = api.list_library()
        self.assertEqual(api.delete_files([self.file(r, "y.wav")])["error"], second.read_only_reason)
        self.assertEqual(self.names(), ["y.wav"])
        self.assertEqual(self.recycled, [])

    def test_a_file_in_use_is_reported_and_the_others_still_go(self):
        self.write("a.wav", wav_bytes(b"a"))
        self.write("b.wav", wav_bytes(b"b"))
        api = self.new_api()
        r = self.index(api)

        def recycle(path, before=None):
            if os.path.basename(path) == "a.wav":
                raise folders.RecycleError(folders.FILE_IN_USE.format("a.wav"))
            self.fake_recycle(path)
        api._recycle = recycle
        res = api.delete_files([self.file(r, "a.wav"), self.file(r, "b.wav")])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["deleted"], ["b.wav"])
        self.assertEqual(res["failed"], [{"name": "a.wav", "error": folders.FILE_IN_USE.format("a.wav")}])
        self.assertEqual(res["split"], [])
        self.assertEqual(self.names(), ["a.wav"])
        r = api.list_library()
        self.assertEqual([f["name"] for f in r["files"]], ["a.wav"])

    def test_nothing_deleted_says_so(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api(recycle=lambda path, before=None: (_ for _ in ()).throw(
            folders.RecycleError(folders.NO_RECYCLE_BIN)))
        r = api.list_library()
        res = api.delete_files([self.file(r, "a.wav")])
        self.assertEqual((res["ok"], res["error"], res["deleted"]), (False, folders.NO_RECYCLE_BIN, []))
        self.assertEqual(self.names(), ["a.wav"])

    def test_half_a_recording_is_reported(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", DECODED)
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)

        def recycle(path, before=None):
            if path.endswith(".wav"):
                raise OSError(32, "The process cannot access the file", path)
            self.fake_recycle(path)
        api._recycle = recycle
        res = api.delete_files([self.file(r, "x.dvf"), self.file(r, "x.wav")])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["deleted"], ["x.dvf"])
        self.assertEqual(res["split"], [{"deleted": ["x.dvf"], "kept": ["x.wav"]}])
        self.assertEqual(res["failed"][0]["name"], "x.wav")
        self.assertNotIn(self.tmp, res["failed"][0]["error"])
        self.assertEqual(self.names(), ["x.wav"])

    def test_the_first_failure_of_a_recording_stops_its_other_copies(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", DECODED)
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)

        def recycle(path, before=None):
            self.recycled.append(path)
            raise folders.RecycleError(folders.FILE_IN_USE.format(os.path.basename(path)))
        api._recycle = recycle
        res = api.delete_files([self.file(r, "x.dvf"), self.file(r, "x.wav")])
        self.assertFalse(res["ok"])
        self.assertEqual(self.recycled_names(), ["x.dvf"])            # the .wav was never tried
        self.assertEqual((res["deleted"], res["split"]), ([], []))
        self.assertEqual(self.names(), ["x.dvf", "x.wav"])

    def test_recorder_backups_that_go_show_as_failed(self):
        dvf = self.write("Save/x.dvf", dvf_bytes())
        wav = self.write("Save/x.wav", DECODED)
        self.write("Save/kept.wav", wav_bytes(b"kept"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
        fps = {f["name"]: f["fp"] for f in r["files"]}
        self.store.add_mark(fps["x.wav"], 0.1, 0.5, "A", "")
        self.store.set_backup(fps["x.wav"], "saved", "Saved", paths=[dvf, wav])
        self.store.add_mark(fps["kept.wav"], 0.1, 0.5, "A", "")
        self.store.set_backup(fps["kept.wav"], "saved", "Saved")
        info = api.delete_info([self.file(r, "x.dvf")])
        self.assertEqual(info["backups"], 1)
        res = api.delete_files(info["ids"])
        self.assertEqual((res["ok"], res["backups"]), (True, 1))
        self.assertEqual(self.store.backup(fps["x.wav"]), {"status": "failed", "detail": backend.BACKUP_RECYCLED})
        self.assertEqual(self.store.backup(fps["kept.wav"])["status"], "saved")
        self.assertEqual(len(self.store.marks(fps["x.wav"])), 1)

    def test_a_backup_left_behind_is_saved_again(self):
        wav = self.write("y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = self.index(api)
        fp = r["files"][0]["fp"]
        self.store.add_mark(fp, 0.1, 0.5, "A", "")
        self.store.set_backup(fp, "saved", "Saved", paths=[wav])
        api._recycle = lambda path, before=None: (_ for _ in ()).throw(folders.RecycleError("in use"))
        res = api.delete_files([self.file(r, "y.wav")])
        self.assertEqual((res["ok"], res["backups"]), (False, 0))
        self.assertEqual(self.store.backup(fp)["status"], "saved")

    def test_backups_that_cannot_be_recorded_stop_the_delete(self):
        wav = self.write("y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = self.index(api)
        fp = r["files"][0]["fp"]
        self.store.set_backup(fp, "saved", "Saved", paths=[wav])
        with mock.patch.object(self.store, "set_backups", side_effect=StoreUnavailable("disk full")):
            res = api.delete_files([self.file(r, "y.wav")])
        self.assertFalse(res["ok"])
        self.assertIn("Nothing was deleted", res["error"])
        self.assertEqual(self.recycled, [])


class RecycleFileTests(unittest.TestCase):
    def test_tree_size_of_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.wav")
            with open(path, "wb") as f:
                f.write(b"x" * 7)
            self.assertEqual(folders.tree_size(path), 7)
            self.assertEqual(folders.tree_size(os.path.join(d, "missing")), 0)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_a_file_refused_up_front_says_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.wav")
            with open(path, "wb") as f:
                f.write(b"x")
            with mock.patch.object(folders, "bin_refuses", return_value=True), \
                    mock.patch.object(folders, "tree_size", return_value=1):
                try:
                    folders.recycle(path)
                except folders.RecycleError as e:
                    message = str(e)
                else:
                    self.fail("not refused")
            if message != folders.NO_RECYCLE_BIN.replace("the folder", "the file"):   # a fixed drive
                self.assertEqual(message, folders.NO_ROOM.replace("This folder", "This file"))
            self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()
