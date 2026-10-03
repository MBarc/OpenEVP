"""Show in File Explorer / Open in File Explorer from the EVP Library's right-click
menu: the page sends only a file or folder id from the latest listing; the backend
resolves it inside the library folder (refusing anything else) and starts Explorer
without a shell."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_folders import FolderApiBase  # noqa: E402
from test_library import dvf_bytes, wav_bytes  # noqa: E402
from app import backend, library_ops  # noqa: E402
from openevp import paths  # noqa: E402


class ShowLibraryFileTests(FolderApiBase):
    def setUp(self):
        super().setUp()
        show = mock.patch.object(backend, "show_in_folder", return_value=True)
        self.show = show.start()
        self.addCleanup(show.stop)

    def test_each_copy_of_a_recording_is_shown_by_its_own_id(self):
        dvf = self.write("Old Mill/x.dvf", dvf_bytes())
        wav = self.write("Old Mill/x.wav", wav_bytes(b"x"))
        api = self.new_api()
        r = api.list_library()
        self.assertEqual(api.show_library_file(self.file(r, "x.dvf")), {"ok": True})
        self.assertEqual(api.show_library_file(self.file(r, "x.wav")), {"ok": True})
        self.assertEqual([os.path.normcase(c.args[0]) for c in self.show.call_args_list],
                         [os.path.normcase(dvf), os.path.normcase(wav)])

    def test_a_clip_is_shown_in_its_clips_folder(self):
        library_ops._make_clips_folder(os.path.join(self.lib, "Clips"))
        clip = self.write("Clips/x_EVP-1.wav", wav_bytes(b"c"))
        api = self.new_api()
        r = api.list_library()
        self.assertEqual(api.show_library_file(self.file(r, "x_EVP-1.wav")), {"ok": True})
        self.assertEqual(os.path.normcase(self.show.call_args.args[0]), os.path.normcase(clip))

    def test_unknown_ids_are_refused(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        self.assertEqual(api.show_library_file("x")["error"], backend.LIB_CHANGED)   # not listed yet
        api.list_library()
        for bad in ("nope", None, 7, ["a"], {"path": "C:\\Windows\\notepad.exe"}, "C:\\Windows\\notepad.exe"):
            self.assertEqual(api.show_library_file(bad)["error"], backend.LIB_CHANGED, bad)
        self.show.assert_not_called()

    def test_a_path_outside_the_library_is_refused(self):
        self.write("a.wav", wav_bytes(b"a"))
        outside = os.path.join(self.tmp, "outside.wav")
        with open(outside, "wb") as f:
            f.write(wav_bytes(b"o"))
        api = self.new_api()
        r = api.list_library()
        fid = self.file(r, "a.wav")
        api._library[fid] = outside                      # an id that no longer points into the library
        self.assertEqual(api.show_library_file(fid)["error"], backend.LIB_CHANGED)
        api._library[fid] = os.path.join(self.lib, "..", "outside.wav")
        self.assertEqual(api.show_library_file(fid)["error"], backend.LIB_CHANGED)
        self.show.assert_not_called()

    def test_another_library_folder_or_a_moved_one_is_refused(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        fid = self.file(api.list_library(), "a.wav")
        with mock.patch.object(api, "_root_moved", return_value=True):
            self.assertEqual(api.show_library_file(fid)["error"], backend.ROOT_CHANGED)
        other = os.path.join(self.tmp, "Other")
        os.makedirs(other)
        api._lib_folder = other                          # chosen since the listing
        self.assertEqual(api.show_library_file(fid)["error"], backend.LIB_CHANGED)
        self.show.assert_not_called()

    def test_a_missing_file_gets_a_plain_message(self):
        path = self.write("Old Mill/gone.wav", wav_bytes(b"g"))
        api = self.new_api()
        fid = self.file(self.index(api), "gone.wav")    # the indexer is done with it: it can be deleted
        os.remove(path)
        res = api.show_library_file(fid)
        self.assertEqual((res["ok"], res["error"]), (False, "gone.wav is no longer there. Refresh the list."))
        self.show.assert_not_called()

    def test_explorer_failing_to_start_is_said(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        fid = self.file(api.list_library(), "a.wav")
        self.show.return_value = False
        res = api.show_library_file(fid)
        self.assertEqual((res["ok"], res["error"]), (False, "File Explorer could not be opened."))


class OpenLibraryFolderTests(FolderApiBase):
    def setUp(self):
        super().setUp()
        op = mock.patch.object(backend, "open_folder", return_value=True)
        self.open = op.start()
        self.addCleanup(op.stop)

    def opened(self):
        return [os.path.normcase(os.path.abspath(c.args[0])) for c in self.open.call_args_list]

    def test_the_library_a_folder_and_a_clips_folder_open(self):
        self.write("Old Mill/a.wav", wav_bytes(b"a"))
        library_ops._make_clips_folder(os.path.join(self.lib, "Old Mill", "Clips"))
        api = self.new_api()
        r = api.list_library()
        for fid in ("root", self.folder(r, "Old Mill"), self.folder(r, "Old Mill", "Clips")):
            self.assertEqual(api.open_library_folder(fid), {"ok": True}, fid)
        self.assertEqual(self.opened(), [os.path.normcase(p) for p in (
            self.lib, os.path.join(self.lib, "Old Mill"), os.path.join(self.lib, "Old Mill", "Clips"))])

    def test_unknown_or_outside_ids_are_refused(self):
        os.makedirs(os.path.join(self.lib, "a"))
        api = self.new_api()
        self.assertEqual(api.open_library_folder("root")["error"], backend.LIB_CHANGED)   # not listed yet
        r = api.list_library()
        for bad in ("nope", None, 3, [], self.lib, "C:\\Windows"):
            self.assertEqual(api.open_library_folder(bad)["error"], backend.LIB_CHANGED, bad)
        fid = self.folder(r, "a")
        api._library_folders[fid] = self.tmp             # the library's parent
        self.assertEqual(api.open_library_folder(fid)["error"], backend.LIB_CHANGED)
        with mock.patch.object(api, "_root_moved", return_value=True):
            self.assertEqual(api.open_library_folder("root")["error"], backend.ROOT_CHANGED)
        self.open.assert_not_called()

    def test_a_missing_folder_gets_a_plain_message(self):
        os.makedirs(os.path.join(self.lib, "Night 2"))
        api = self.new_api()
        fid = self.folder(api.list_library(), "Night 2")
        os.rmdir(os.path.join(self.lib, "Night 2"))
        res = api.open_library_folder(fid)
        self.assertEqual((res["ok"], res["error"]), (False, "The folder Night 2 is no longer there. Refresh the list."))
        self.open.assert_not_called()


class ExplorerCommandTests(unittest.TestCase):
    PATH = "C:\\Ghost Hunts\\Old Mill, night 2\\EVP 1, Grüße ☃.wav"

    def test_the_command_line_quotes_the_whole_path_after_select(self):
        with mock.patch.dict(os.environ, {"SystemRoot": "C:\\WINDOWS"}):
            self.assertEqual(paths.explorer_select_command(self.PATH),
                             '"C:\\WINDOWS\\explorer.exe" /select,"' + self.PATH + '"')
            # A comma with no space in the path: still quoted (an argument list would not be).
            self.assertEqual(paths.explorer_select_command("C:\\p,q\\r,s.wav"),
                             '"C:\\WINDOWS\\explorer.exe" /select,"C:\\p,q\\r,s.wav"')

    def test_explorer_is_started_without_a_shell_and_reaped(self):
        proc = mock.Mock()
        with mock.patch.object(paths.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"SystemRoot": "C:\\WINDOWS"}), \
                mock.patch.object(paths.subprocess, "Popen", return_value=proc) as popen:
            self.assertTrue(paths.show_in_folder(self.PATH))
        popen.assert_called_once_with('"C:\\WINDOWS\\explorer.exe" /select,"' + self.PATH + '"')
        self.assertNotIn("shell", popen.call_args.kwargs)
        for _ in range(100):                            # the reaper thread waits for it
            if proc.wait.called:
                break
            time.sleep(0.01)
        proc.wait.assert_called_once_with()

    def test_a_failed_start_or_another_system_returns_false(self):
        with mock.patch.object(paths.sys, "platform", "win32"), \
                mock.patch.object(paths.subprocess, "Popen", side_effect=OSError("no")):
            self.assertFalse(paths.show_in_folder(self.PATH))
        with mock.patch.object(paths.sys, "platform", "linux"), \
                mock.patch.object(paths.subprocess, "Popen") as popen:
            self.assertFalse(paths.show_in_folder(self.PATH))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
