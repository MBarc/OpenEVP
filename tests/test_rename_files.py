"""Renaming a recording in the EVP Library: Api.rename_files gives a recording's
files (a .dvf and its .wav, in one folder) one new name, each keeping its own
extension -- never overwriting, all or nothing, with backups, the fingerprint
index, rec handles and the audio server following."""
import os
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_folders import FolderApiBase  # noqa: E402
from test_library import FakeDecoder, dvf_bytes, wav_bytes  # noqa: E402
from app import backend, library_ops  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from app.store import AppData, StoreUnavailable  # noqa: E402
from openevp import wavinfo  # noqa: E402


class RenameFilesTests(FolderApiBase):
    def group(self, r, *names):
        return [self.file(r, n) for n in names]

    def test_a_dvf_and_its_wav_get_the_new_name_and_keep_their_fingerprints(self):
        self.write("Old Mill/x.dvf", dvf_bytes())
        self.write("Old Mill/x.wav", wav_bytes(b"x"))
        fake = FakeDecoder()
        api = self.new_api()
        with fake.installed():
            r = self.index(api)
            self.assertEqual(fake.calls, 1)
            before = {os.path.splitext(f["name"])[1]: f["fp"] for f in r["files"]}
            ids = self.group(r, "x.dvf", "x.wav")
            res = api.rename_files(ids, "  Knock in the attic ")
            self.assertTrue(res["ok"], res)
            r = api.list_library()
        self.assertEqual(sorted(res["renamed"], key=lambda x: x["from"]),
                         [{"from": "x.dvf", "to": "Knock in the attic.dvf"},
                          {"from": "x.wav", "to": "Knock in the attic.wav"}])
        self.assertEqual(self.names("Old Mill"), ["Knock in the attic.dvf", "Knock in the attic.wav"])
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))    # re-keyed: nothing decoded again
        self.assertEqual(fake.calls, 1)
        self.assertEqual({os.path.splitext(f["name"])[1]: f["fp"] for f in r["files"]}, before)
        self.assertEqual(set(res["ids"]), set(ids))
        self.assertEqual(set(res["ids"].values()), {f["id"] for f in r["files"]})
        self.assertFalse(api._busy.locked())

    def test_different_stems_all_get_the_one_new_name(self):
        self.write("a.dvf", dvf_bytes())
        self.write("b.wav", wav_bytes(b"b"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = api.list_library()
            res = api.rename_files(self.group(r, "a.dvf", "b.wav"), "b")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["renamed"], [{"from": "a.dvf", "to": "b.dvf"}])   # b.wav was already named so
        self.assertEqual(self.names(), ["b.dvf", "b.wav"])

    def test_marks_still_show_after_the_rename(self):
        self.write("y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = self.index(api)
        fp = r["files"][0]["fp"]
        self.store.add_mark(fp, 0.1, 0.3, "A", "voice")
        res = api.rename_files([self.file(r, "y.wav")], "Voice")
        self.assertTrue(res["ok"], res)
        r = api.list_library()
        row = r["files"][0]
        self.assertEqual((row["name"], row["fp"], row["marks"]["A"]), ("Voice.wav", fp, 1))
        self.assertEqual(len(api.library_marks(row["id"])["marks"]), 1)

    def test_a_taken_name_is_refused_and_nothing_changes(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", wav_bytes(b"x"))
        self.write("taken.WAV", wav_bytes(b"other"))                  # only the .wav would clash (any case)
        with FakeDecoder().installed():
            api = self.new_api()
            r = api.list_library()
            res = api.rename_files(self.group(r, "x.dvf", "x.wav"), "TAKEN")
        self.assertFalse(res["ok"])
        self.assertIn('already a file named "TAKEN.wav"', res["error"])
        self.assertNotIn(self.lib, res["error"])
        self.assertEqual(self.names(), ["taken.WAV", "x.dvf", "x.wav"])
        with open(os.path.join(self.lib, "taken.WAV"), "rb") as f:
            self.assertEqual(f.read(), wav_bytes(b"other"))
        self.assertFalse(api._busy.locked())

    def test_invalid_names_are_refused(self):
        self.write("x.wav", wav_bytes(b"x"))
        api = self.new_api()
        r = api.list_library()
        x = self.file(r, "x.wav")
        for bad in ["", "   ", None, 5, "a/b", "a\\b", "a:b", 'a"b', "a|b", "a?b", "a*b", "a<b", "a\x01b", "CON", "nul",
                    "COM1", "LPT¹", "CONIN$", "a.", "a. ", ".hidden", "x" * 121]:
            res = api.rename_files([x], bad)
            self.assertFalse(res["ok"], repr(bad))
            self.assertNotIn("folder", res["error"], repr(bad))
        self.assertEqual(self.names(), ["x.wav"])

    def test_refusals(self):
        self.write("A/a.wav", wav_bytes(b"a"))
        self.write("B/b.wav", wav_bytes(b"b"))
        api = self.new_api()
        self.assertEqual(api.rename_files(["x"], "n")["error"], backend.LIB_CHANGED)   # not listed yet
        r = api.list_library()
        a, b = self.file(r, "a.wav"), self.file(r, "b.wav")
        self.assertFalse(api.rename_files([], "n")["ok"])
        self.assertFalse(api.rename_files("a", "n")["ok"])
        self.assertFalse(api.rename_files([1], "n")["ok"])
        self.assertEqual(api.rename_files(["nope"], "n")["error"], backend.LIB_CHANGED)
        self.assertIn("one folder", api.rename_files([a, b], "n")["error"])
        api._busy.acquire()                                               # an export
        try:
            self.assertEqual(api.rename_files([a], "n")["error"], backend.FS_BUSY)
        finally:
            api._busy.release()
        with mock.patch.object(api, "backing_up", return_value=True):
            self.assertEqual(api.rename_files([a], "n")["error"], backend.FS_BUSY)
        with mock.patch.object(backend.folders, "inside", return_value=False):
            self.assertEqual(api.rename_files([a], "n")["error"], backend.LIB_CHANGED)
        with mock.patch.object(backend.folders, "too_long", return_value=True):
            self.assertIn("too long", api.rename_files([a], "n")["error"])
        os.remove(os.path.join(self.lib, "A", "a.wav"))
        self.assertEqual(api.rename_files([a], "n")["error"], "a.wav is no longer there. Refresh the list.")
        self.assertEqual((self.names("A"), self.names("B")), ([], ["b.wav"]))
        self.assertFalse(api._busy.locked())

    def test_refused_while_the_indexer_will_not_pause(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        r = api.list_library()
        busy = mock.Mock()
        busy.is_alive.return_value = True
        api._indexer = busy
        with mock.patch.object(library_ops, "FS_WAIT", 0.01):
            self.assertEqual(api.rename_files([self.file(r, "a.wav")], "n")["error"], backend.INDEXER_BUSY)
        api._indexer = None
        self.assertEqual(self.names(), ["a.wav"])
        self.assertFalse(api._busy.locked())

    def test_two_files_that_would_get_the_same_name_are_refused(self):
        self.write("a.wav", wav_bytes(b"a"))
        self.write("b.WAV", wav_bytes(b"b"))
        api = self.new_api()
        r = api.list_library()
        res = api.rename_files(self.group(r, "a.wav", "b.WAV"), "n")
        self.assertFalse(res["ok"])
        self.assertIn("would both be named", res["error"])
        self.assertEqual(self.names(), ["a.wav", "b.WAV"])

    def test_case_only_rename_and_the_same_name(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", wav_bytes(b"x"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = api.list_library()
            ids = self.group(r, "x.dvf", "x.wav")
            same = api.rename_files(ids, "x")
            self.assertEqual(same, {"ok": True, "renamed": [], "ids": {i: i for i in ids}})
            res = api.rename_files(ids, "X")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.names(), ["X.dvf", "X.wav"])
        self.assertEqual(len(res["renamed"]), 2)

    def test_when_the_second_file_fails_the_first_is_named_back(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", wav_bytes(b"x"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
        fps = {f["name"]: f["fp"] for f in r["files"]}
        real = os.rename

        def rename(src, dst):
            if os.path.basename(src) == "x.wav":
                raise PermissionError(13, "Access is denied", src)
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename):
            res = api.rename_files(self.group(r, "x.dvf", "x.wav"), "New")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "x.wav was not renamed: it is open in another program "
                                       "(close it and try again). Nothing was renamed.")
        self.assertEqual(self.names(), ["x.dvf", "x.wav"])
        r = api.list_library()
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))    # the index was never moved
        self.assertEqual({f["name"]: f["fp"] for f in r["files"]}, fps)
        self.assertFalse(api._busy.locked())

    def test_a_file_that_cannot_be_named_back_is_said_and_re_keyed(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", wav_bytes(b"x"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
        real = os.rename

        def rename(src, dst):
            if os.path.basename(src) in ("x.wav", "New.dvf"):
                raise PermissionError(13, "Access is denied", src)
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename), mock.patch.object(library_ops, "RENAME_PAUSE", 0.01):
            res = api.rename_files(self.group(r, "x.dvf", "x.wav"), "New")
        self.assertFalse(res["ok"])
        self.assertIn("New.dvf could not be given its old name back", res["error"])
        self.assertEqual(res["ids"], {self.file(r, "x.dvf"): backend._file_id(os.path.join(self.lib, "New.dvf"))})
        self.assertEqual(self.names(), ["New.dvf", "x.wav"])
        r = api.list_library()
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))

    def setup_backed_up_pair(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", wav_bytes(b"x"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
        fp = next(f["fp"] for f in r["files"] if f["name"] == "x.dvf")
        lib = os.path.abspath(self.lib)
        old = [os.path.join(lib, "x.dvf"), os.path.join(lib, "x.wav")]
        self.store.set_backup(fp, "saved", "Saved", old)
        return api, r, fp, old

    def test_an_unexpected_error_on_the_second_file_names_the_first_back(self):
        api, r, fp, old = self.setup_backed_up_pair()
        real, calls = self.store.move_backup_paths, []

        def move(o, n):
            calls.append(o)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real(o, n)
        with mock.patch.object(self.store, "move_backup_paths", move):
            res = api.rename_files(self.group(r, "x.dvf", "x.wav"), "New")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "x.wav was not renamed: boom. Nothing was renamed.")
        self.assertEqual((res["ids"], self.names()), ({}, ["x.dvf", "x.wav"]))
        self.assertEqual(self.store.backup_record(fp)["paths"], old)
        self.assertFalse(api._busy.locked())
        r = api.list_library()
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))

    def test_an_undo_that_raises_does_not_stop_the_roll_back(self):
        api, r, fp, old = self.setup_backed_up_pair()
        real = os.rename

        def rename(src, dst):
            if os.path.basename(src) == "x.wav":
                raise PermissionError(13, "Access is denied", src)
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename),                 mock.patch.object(self.store, "restore_backup_paths", side_effect=RuntimeError("store trouble")):
            res = api.rename_files(self.group(r, "x.dvf", "x.wav"), "New")
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"].endswith("Nothing was renamed."), res)
        self.assertEqual(self.names(), ["x.dvf", "x.wav"])
        self.assertFalse(api._busy.locked())

    def test_an_old_name_taken_meanwhile_is_never_overwritten(self):
        api, r, fp, old = self.setup_backed_up_pair()
        real, seen = os.rename, []

        def rename(src, dst):
            if os.path.basename(src) == "x.wav":
                with open(old[0], "wb") as f:              # another program takes x.dvf meanwhile
                    f.write(b"intruder")
                raise PermissionError(13, "Access is denied", src)
            seen.append((os.path.basename(src), list(self.store.backup_record(fp)["paths"])))
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename):
            res = api.rename_files(self.group(r, "x.dvf", "x.wav"), "New")
        self.assertFalse(res["ok"])
        self.assertIn("New.dvf could not be given its old name back", res["error"])
        self.assertEqual(res["ids"], {self.file(r, "x.dvf"): backend._file_id(os.path.join(self.lib, "New.dvf"))})
        self.assertEqual(self.names(), ["New.dvf", "x.dvf", "x.wav"])
        with open(old[0], "rb") as f:
            self.assertEqual(f.read(), b"intruder")
        self.assertEqual(len(seen), 1)                          # never renamed back over it
        self.assertEqual(self.store.backup_record(fp)["paths"],
                         [os.path.join(os.path.abspath(self.lib), "New.dvf"), old[1]])   # follows the stuck file

    def test_backup_records_are_restored_before_a_file_is_named_back(self):
        api, r, fp, old = self.setup_backed_up_pair()
        real, seen = os.rename, []

        def rename(src, dst):
            if os.path.basename(src) == "x.wav":
                raise PermissionError(13, "Access is denied", src)
            seen.append((os.path.basename(src), list(self.store.backup_record(fp)["paths"])))
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename):
            self.assertFalse(api.rename_files(self.group(r, "x.dvf", "x.wav"), "New")["ok"])
        new_dvf = os.path.join(os.path.abspath(self.lib), "New.dvf")
        self.assertEqual(seen, [("x.dvf", [new_dvf, old[1]]), ("New.dvf", old)])

    def test_a_briefly_locked_file_is_renamed_after_a_retry(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        r = api.list_library()
        real, calls = os.rename, []

        def rename(src, dst):
            calls.append(src)
            if len(calls) < 3:
                e = PermissionError(13, "The process cannot access the file")
                e.winerror = 32
                raise e
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename), mock.patch.object(library_ops, "RENAME_PAUSE", 0.01):
            res = api.rename_files([self.file(r, "a.wav")], "b")
        self.assertTrue(res["ok"], res)
        self.assertEqual((len(calls), self.names()), (3, ["b.wav"]))

    def test_backup_paths_follow_and_are_put_back_on_failure(self):
        self.write("Save/x.dvf", dvf_bytes())
        self.write("Save/x.wav", wav_bytes(b"x"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
        fp = next(f["fp"] for f in r["files"] if f["name"] == "x.dvf")
        save = os.path.join(os.path.abspath(self.lib), "Save")
        old = [os.path.join(save, "x.dvf"), os.path.join(save, "x.wav")]
        self.store.set_backup(fp, "saved", "Saved", old)
        ids = self.group(r, "x.dvf", "x.wav")
        # The second file fails: the first is named back, and so are the backup paths.
        real = os.rename

        def rename(src, dst):
            if os.path.basename(src) == "x.wav":
                raise PermissionError(13, "Access is denied", src)
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename):
            self.assertFalse(api.rename_files(ids, "New")["ok"])
        self.assertEqual(self.store.backup_record(fp)["paths"], old)
        # The backup paths cannot be saved: nothing is renamed.
        with mock.patch.object(self.store, "move_backup_paths", side_effect=StoreUnavailable("disk full")):
            res = api.rename_files(ids, "New")
        self.assertIn("could not be recorded", res["error"])
        self.assertEqual(self.names("Save"), ["x.dvf", "x.wav"])
        # Saved before each file is renamed.
        seen = []

        def watch(src, dst):
            seen.append(list(self.store.backup_record(fp)["paths"]))
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", watch):
            self.assertTrue(api.rename_files(ids, "New")["ok"])
        new = [os.path.join(save, "New.dvf"), os.path.join(save, "New.wav")]
        self.assertEqual(seen, [[new[0], old[1]], new])
        self.assertEqual(self.store.backup_record(fp)["paths"], new)
        self.assertEqual(self.store.backup(fp)["status"], "saved")

    def test_a_loaded_recording_keeps_playing(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        server = AudioServer(lambda key: b"", cache.name)
        server.start()
        self.addCleanup(server.stop)
        path = self.write("Old Mill/y.wav", wav_bytes(b"y"))
        with open(path, "rb") as f:
            data = f.read()
        api = self.new_api(server=server)
        r = self.index(api)
        loaded = api.play_library(self.file(r, "y.wav"))
        self.assertTrue(loaded["ok"], loaded)
        with urllib.request.urlopen(loaded["url"], timeout=5) as resp:   # being played
            resp.read(100)
            res = api.rename_files([self.file(r, "y.wav")], "Whisper")
        self.assertTrue(res["ok"], res)
        with urllib.request.urlopen(loaded["url"], timeout=5) as resp:
            self.assertEqual(resp.read(), data)
        self.assertEqual(api.recording_changed(loaded["rec"]), {"ok": True, "changed": False})
        entry = api._entry(loaded["rec"])
        self.assertEqual((entry["name"], os.path.basename(entry["source"]["path"])), ("Whisper.wav", "Whisper.wav"))
        self.assertTrue(api.add_mark(loaded["rec"], 0.1, 0.3, "B", "knock")["ok"])
        self.assertEqual(len(self.store.marks(wavinfo.wav_fingerprint(
            os.path.join(self.lib, "Old Mill", "Whisper.wav")))), 1)
        r = api.list_library()
        self.assertEqual(list(res["ids"].values()), [self.file(r, "Whisper.wav")])
        self.assertTrue(api.play_library(self.file(r, "Whisper.wav"))["ok"])       # the page reloads it by its new id

    def test_a_second_window_renames_nothing(self):
        self.write("y.wav", wav_bytes(b"y"))
        second = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(second.close)
        self.assertTrue(second.read_only)
        api = self.new_api(store=second)
        r = api.list_library()
        self.assertEqual(api.rename_files([self.file(r, "y.wav")], "z")["error"], backend.SECOND_WINDOW)
        self.assertEqual(self.names(), ["y.wav"])
        self.assertFalse(api._busy.locked())


if __name__ == "__main__":
    unittest.main()
