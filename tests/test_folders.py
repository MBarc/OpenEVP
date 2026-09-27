"""Library folders: name rules, containment, the Recycle Bin call, and the Api's
create / rename / folder_info / delete_folder / move_files (tasks 2 and 3)."""
import ctypes
import os
import shutil
import stat
import subprocess
import sys
import types
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_library import WAIT, Events, FakeDecoder, FakeServer, dvf_bytes, wav_bytes  # noqa: E402
from app import backend, folders  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from app.store import AppData  # noqa: E402
from st25 import wavinfo  # noqa: E402

WINDOWS = sys.platform == "win32"


class NameTests(unittest.TestCase):
    def test_valid_names(self):
        for name, cleaned in [("Old Mill", "Old Mill"), ("  Old Mill  ", "Old Mill"), ("2026-09-26 Asylum", "2026-09-26 Asylum"),
                              ("Café ñ 幽霊", "Café ñ 幽霊"), ("x" * 120, "x" * 120), ("CONSOLE", "CONSOLE"),
                              ("COM10", "COM10"), ("a.b", "a.b"), ("Investigation #3 (night)", "Investigation #3 (night)")]:
            self.assertEqual(folders.clean_name(name), (cleaned, None), name)

    def test_invalid_names(self):
        bad = ["", "   ", None, 5, "x" * 121, ".", "..", ".hidden", "a.", "a. ", "a<b", "a>b", 'a"b', "a:b",
               "a/b", "a\\b", "a|b", "a?b", "a*b", "a\tb", "a\x00b", "a\x1fb",
               "CON", "con", "Prn", "AUX", "NUL", "nul.txt", "NUL .txt", "COM1", "com9", "LPT1", "lpt9.x",
               "COM0", "LPT0", "COM\u00b9", "COM\u00b2", "LPT\u00b3", "CONIN$", "conout$"]
        for name in bad:
            cleaned, problem = folders.clean_name(name)
            self.assertIsNone(cleaned, repr(name))
            self.assertTrue(problem and "\\\\" not in problem, repr(name))

    def test_too_long(self):
        self.assertTrue(folders.too_long("C:\\" + "x" * 240))
        self.assertFalse(folders.too_long(os.path.abspath("short")))


class ContainmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "lib")
        os.makedirs(os.path.join(self.root, "a", "b"))
        os.makedirs(os.path.join(tmp.name, "outside"))
        self.tmp = tmp.name

    def test_inside(self):
        self.assertTrue(folders.inside(self.root, os.path.join(self.root, "a", "b")))
        self.assertFalse(folders.inside(self.root, self.root))
        self.assertTrue(folders.inside(self.root, self.root, allow_root=True))
        self.assertFalse(folders.inside(self.root, os.path.join(self.tmp, "outside")))
        self.assertFalse(folders.inside(self.root, os.path.join(self.root, "..", "outside")))
        self.assertFalse(folders.inside(self.root, os.path.join(self.root, "missing")))

    def test_a_path_that_resolves_elsewhere_is_refused(self):
        inner = os.path.join(self.root, "a")
        real = os.path.realpath

        def junction(p, *args, **kw):                   # "a" swapped for a junction to outside
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(inner)):
                return os.path.join(self.tmp, "outside")
            return real(p, *args, **kw)
        with mock.patch.object(folders.os.path, "realpath", junction):
            self.assertFalse(folders.inside(self.root, inner))

    def test_a_link_leaf_is_refused(self):
        with mock.patch.object(folders, "is_link", return_value=True):
            self.assertFalse(folders.inside(self.root, os.path.join(self.root, "a")))

    def test_under_and_rebase(self):
        a = os.path.join(self.root, "Old Mill")
        self.assertTrue(folders.under(os.path.join(a, "x.wav"), a))
        self.assertTrue(folders.under(a, a))
        self.assertFalse(folders.under(a + " 2", a))
        self.assertEqual(folders.rebase(os.path.join(a, "s", "x.wav"), a, os.path.join(self.root, "New")),
                         os.path.join(os.path.abspath(self.root), "New", "s", "x.wav"))


class LinkTests(unittest.TestCase):
    def fake(self, attrs, tag, mode=stat.S_IFDIR):
        return types.SimpleNamespace(st_mode=mode, st_file_attributes=attrs, st_reparse_tag=tag)

    def test_only_name_surrogates_are_links(self):
        self.assertTrue(folders._link_stat(self.fake(0x400, 0xA0000003)))            # junction
        self.assertTrue(folders._link_stat(self.fake(0x400, 0xA000000C)))            # symlink
        self.assertFalse(folders._link_stat(self.fake(0x400, 0x9000001A, stat.S_IFREG)))   # OneDrive placeholder
        self.assertFalse(folders._link_stat(self.fake(0x10, 0)))                      # a plain folder
        self.assertTrue(folders._link_stat(types.SimpleNamespace(st_mode=stat.S_IFLNK)))

    @unittest.skipUnless(WINDOWS, "junctions are Windows-only")
    def test_a_real_junction_is_detected_and_not_listed(self):
        with tempfile.TemporaryDirectory() as d:
            lib, outside = os.path.join(d, "lib"), os.path.join(d, "outside")
            os.makedirs(os.path.join(lib, "real"))
            os.makedirs(outside)
            with open(os.path.join(outside, "o.wav"), "wb") as f:
                f.write(wav_bytes(b"o"))
            link = os.path.join(lib, "jump")
            done = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], capture_output=True)
            if done.returncode != 0:
                self.skipTest("could not create a junction")
            try:
                with os.scandir(lib) as it:
                    kinds = {e.name: folders.entry_is_link(e) for e in it}
                self.assertEqual(kinds, {"jump": True, "real": False})
                self.assertTrue(folders.is_link(link))
                self.assertFalse(folders.inside(lib, link))
                found, subfolders, _, _ = backend._scan_library(lib)
                self.assertEqual((found, subfolders), ([], [("real",)]))
            finally:
                os.rmdir(link)                                  # removes the junction, not its target
            self.assertTrue(os.path.isfile(os.path.join(outside, "o.wav")))


class RecycleBinTests(unittest.TestCase):
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout")
    def test_struct_layout(self):
        self.assertEqual(ctypes.sizeof(folders.SHFILEOPSTRUCTW), 56)
        self.assertEqual(ctypes.sizeof(folders.SHQUERYRBINFO), 24)
        self.assertEqual(folders.SHFILEOPSTRUCTW.fFlags.offset, 32)
        self.assertEqual(folders.RECYCLE_FLAGS, 0x40 | 0x10 | 0x400 | 0x4 | 0x4000 | 0x8000)

    @unittest.skipIf(WINDOWS, "off Windows only")
    def test_off_windows_nothing_is_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(folders.RecycleError):
                folders.recycle(d)
            self.assertTrue(os.path.isdir(d))

    @unittest.skipUnless(WINDOWS, "Windows only")
    def test_network_and_overlong_paths_are_refused(self):
        with self.assertRaises(folders.RecycleError):
            folders.recycle("\\\\server\\share\\folder")
        with self.assertRaises(folders.RecycleError):
            folders.recycle("C:\\" + "x" * 300)

    @unittest.skipUnless(WINDOWS and os.environ.get("OPENEVP_TEST_RECYCLE") == "1",
                         "set OPENEVP_TEST_RECYCLE=1 to put a test folder in the real Recycle Bin")
    def test_real_recycle_bin(self):
        base = tempfile.mkdtemp(prefix="openevp-recycle-test-")
        self.addCleanup(shutil.rmtree, base, True)
        drive = os.path.splitdrive(base)[0] + "\\"

        def items():
            info = folders.SHQUERYRBINFO(cbSize=ctypes.sizeof(folders.SHQUERYRBINFO))
            if ctypes.windll.shell32.SHQueryRecycleBinW(drive, ctypes.byref(info)) != 0:
                self.skipTest("no Recycle Bin on this drive")
            return info.i64NumItems
        before = items()
        target = os.path.join(base, "OpenEVP recycle test")
        os.makedirs(os.path.join(target, "sub"))
        held = os.path.join(target, "sub", "a.wav")
        with open(held, "wb") as f:
            f.write(b"x")
        with open(held, "rb"):                              # in use: refused, the folder stays
            with self.assertRaises(folders.RecycleError):
                folders.recycle(target)
        self.assertTrue(os.path.isfile(held))
        folders.recycle(target)
        self.assertFalse(os.path.lexists(target))
        self.assertGreater(items(), before)                # in the Recycle Bin, not deleted for good


class FolderApiBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.lib = os.path.join(self.tmp, "OpenEVP")
        os.makedirs(self.lib)
        self.store = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(self.store.close)
        self.events = Events()
        self.recycled = []
        self.server = FakeServer()

    def fake_recycle(self, path):
        self.recycled.append(path)
        shutil.move(path, os.path.join(self.tmp, "bin-" + str(len(self.recycled))))

    def new_api(self, store="default", server=None, recycle="default"):
        api = backend.Api(None, self.events, lambda start: None, self.lib, server or self.server,
                          store=self.store if store == "default" else store,
                          recycle=self.fake_recycle if recycle == "default" else recycle)
        api._lib_folder = self.lib                       # the library stays put when a test moves Save-to
        self.addCleanup(api.shutdown)
        return api

    def write(self, rel, data):
        path = os.path.join(self.lib, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def index(self, api):
        """list_library(), wait for its indexer, and list again (with fingerprints)."""
        r = api.list_library()
        self.assertTrue(r["ok"], r)
        if r["indexing"]:
            self.events.wait_done(r["scan_id"])
            r = api.list_library()
            self.assertEqual(r["pending"], 0)
        return r

    def folder(self, r, *rel):
        return next(f["id"] for f in r["folders"] if f["rel"] == list(rel))

    def file(self, r, name):
        return next(f["id"] for f in r["files"] if f["name"] == name)

    def names(self, *rel):
        return sorted(os.listdir(os.path.join(self.lib, *rel)))


class CreateRenameTests(FolderApiBase):
    def test_create_in_root_and_nested(self):
        api = self.new_api()
        r = api.list_library()
        made = api.create_folder("root", "  Old Mill ")
        self.assertEqual(made["ok"], True, made)
        self.assertTrue(os.path.isdir(os.path.join(self.lib, "Old Mill")))
        r = api.list_library()
        self.assertEqual(made["id"], self.folder(r, "Old Mill"))
        inner = api.create_folder(made["id"], "Night 1")
        self.assertTrue(inner["ok"], inner)
        r = api.list_library()
        self.assertEqual(inner["id"], self.folder(r, "Old Mill", "Night 1"))

    def test_create_refuses_bad_names_clashes_and_unknown_ids(self):
        self.write("Old Mill/x.wav", wav_bytes(b"x"))
        self.write("notes.txt", b"n")
        api = self.new_api()
        self.assertEqual(api.create_folder("root", "x")["error"], backend.LIB_CHANGED)   # not listed yet
        api.list_library()
        for name in ("old mill", "NOTES.TXT"):
            r = api.create_folder("root", name)
            self.assertFalse(r["ok"])
            self.assertIn("already", r["error"])
        self.assertFalse(api.create_folder("root", "CON")["ok"])
        self.assertFalse(api.create_folder("root", "a/b")["ok"])
        self.assertEqual(api.create_folder("nope", "x")["error"], backend.LIB_CHANGED)
        self.assertEqual(api.create_folder(None, "x")["error"], backend.LIB_CHANGED)
        self.assertFalse(api.create_folder("root", "y" * 120 + "/")["ok"])
        with mock.patch.object(backend.folders, "too_long", return_value=True):
            self.assertEqual(api.create_folder("root", "Fine")["error"], backend.PATH_TOO_LONG)
        self.assertEqual(self.names(), ["Old Mill", "notes.txt"])

    def test_create_refused_during_an_export_or_a_backup(self):
        api = self.new_api()
        api.list_library()
        api._busy.acquire()
        try:
            self.assertEqual(api.create_folder("root", "A")["error"], backend.FS_BUSY)
        finally:
            api._busy.release()
        with mock.patch.object(api, "backing_up", return_value=True):
            self.assertEqual(api.create_folder("root", "A")["error"], backend.FS_BUSY)
        self.assertEqual(self.names(), [])
        self.assertTrue(api.create_folder("root", "A")["ok"])          # _busy was released each time

    def test_rename_returns_the_new_id_and_marks_follow(self):
        self.write("Old Mill/A/x.dvf", dvf_bytes())
        self.write("Old Mill/y.wav", wav_bytes(b"y"))
        fake = FakeDecoder()
        api = self.new_api()
        with fake.installed():
            r = self.index(api)
            self.assertEqual(fake.calls, 1)
            fp_before = {f["name"]: f["fp"] for f in r["files"]}
            renamed = api.rename_folder(self.folder(r, "Old Mill"), "Mill 2")
            self.assertTrue(renamed["ok"], renamed)
            r = api.list_library()
        self.assertEqual(renamed["id"], self.folder(r, "Mill 2"))
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))    # fingerprints re-keyed, nothing to redo
        self.assertEqual(fake.calls, 1)
        self.assertEqual({f["name"]: f["fp"] for f in r["files"]}, fp_before)
        self.assertEqual(self.names(), ["Mill 2"])

    def test_rename_case_only_same_name_clash_and_root(self):
        self.write("Old Mill/y.wav", wav_bytes(b"y"))
        self.write("Asylum/z.wav", wav_bytes(b"z"))
        api = self.new_api()
        r = api.list_library()
        mill = self.folder(r, "Old Mill")
        self.assertEqual(api.rename_folder(mill, "Old Mill"), {"ok": True, "id": mill})
        clash = api.rename_folder(mill, "asylum")
        self.assertFalse(clash["ok"])
        self.assertIn("already", clash["error"])
        self.assertFalse(api.rename_folder("root", "Other")["ok"])
        self.assertFalse(api.rename_folder(mill, "bad|name")["ok"])
        case = api.rename_folder(mill, "OLD MILL")
        self.assertTrue(case["ok"], case)
        self.assertEqual(self.names(), ["Asylum", "OLD MILL"])
        r = api.list_library()
        self.assertEqual(case["id"], self.folder(r, "OLD MILL"))

    def test_rename_refused_when_a_nested_file_would_get_too_long(self):
        base = os.path.join(self.lib, "A")
        room = folders.MAX_PATH_CHARS - 4 - len(os.path.abspath(base)) - len(os.sep) * 2 - len(".wav")
        deep = os.path.join(base, "d" * (room // 2), "e" * (room - room // 2 - 1))
        os.makedirs(deep)
        path = deep + os.sep + "x.wav"
        with open(path, "wb") as f:
            f.write(wav_bytes(b"x"))
        self.assertLess(len(os.path.abspath(path)), folders.MAX_PATH_CHARS)
        grow = folders.MAX_PATH_CHARS - len(os.path.abspath(path))       # extra characters that reach the limit
        api = self.new_api()
        r = api.list_library()
        res = api.rename_folder(self.folder(r, "A"), "A" + "b" * grow)
        self.assertEqual(res["error"], backend.PATH_TOO_LONG)
        self.assertEqual(self.names(), ["A"])
        self.assertTrue(api.rename_folder(self.folder(r, "A"), "A" + "b" * (grow - 1))["ok"])

    def test_the_save_to_folder_follows_a_rename(self):
        os.makedirs(os.path.join(self.lib, "Old Mill", "Exports"))
        os.makedirs(os.path.join(self.lib, "Asylum"))
        api = self.new_api()
        api._dest = os.path.join(self.lib, "Old Mill", "Exports")
        r = api.list_library()
        self.assertTrue(api.folder_info(self.folder(r, "Old Mill"))["save_folder"])
        self.assertTrue(api.folder_info(self.folder(r, "Old Mill", "Exports"))["save_folder"])
        self.assertFalse(api.folder_info(self.folder(r, "Asylum"))["save_folder"])
        self.assertTrue(api.rename_folder(self.folder(r, "Asylum"), "Asylum 2")["ok"])
        self.assertEqual(api.default_destination(), os.path.join(self.lib, "Old Mill", "Exports"))
        self.assertTrue(api.rename_folder(self.folder(r, "Old Mill"), "Mill 2")["ok"])
        moved = os.path.abspath(os.path.join(self.lib, "Mill 2", "Exports"))
        self.assertEqual(api.default_destination(), moved)
        self.assertEqual(self.store.get_setting("save_folder"), moved)

    def test_the_save_to_folder_follows_on_a_read_only_store(self):
        os.makedirs(os.path.join(self.lib, "Old Mill"))
        second = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        api._dest = os.path.join(self.lib, "Old Mill")
        r = api.list_library()
        self.assertTrue(api.rename_folder(self.folder(r, "Old Mill"), "Mill 2")["ok"])
        self.assertEqual(api.default_destination(), os.path.abspath(os.path.join(self.lib, "Mill 2")))

    def test_rename_of_a_folder_that_escaped_is_refused(self):
        self.write("Old Mill/y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = api.list_library()
        with mock.patch.object(backend.folders, "inside", return_value=False):
            self.assertEqual(api.rename_folder(self.folder(r, "Old Mill"), "X")["error"], backend.LIB_CHANGED)
        self.assertEqual(self.names(), ["Old Mill"])

    @unittest.skipUnless(WINDOWS, "Windows refuses to rename a folder holding an open file")
    def test_rename_with_a_file_held_open_fails_plainly(self):
        path = self.write("Old Mill/y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = api.list_library()
        with open(path, "rb"):
            res = api.rename_folder(self.folder(r, "Old Mill"), "X")
        self.assertFalse(res["ok"])
        self.assertIn("open in another program", res["error"])
        self.assertNotIn(self.lib, res["error"])
        self.assertEqual(self.names(), ["Old Mill"])


class DeleteTests(FolderApiBase):
    def test_folder_info_counts_fresh_from_disk(self):
        self.write("Old Mill/x.dvf", dvf_bytes())
        self.write("Old Mill/A/y.wav", wav_bytes(b"y"))
        self.write("Old Mill/A/photo.jpg", b"12345")
        self.write("Old Mill/B/.hidden", b"1")
        os.makedirs(os.path.join(self.lib, "Old Mill", "C", "D"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = self.index(api)
        fp = next(f["fp"] for f in r["files"] if f["name"] == "y.wav")
        self.store.add_mark(fp, 0.1, 0.5, "A", "")
        self.write("Old Mill/C/new.wav", wav_bytes(b"new"))              # not indexed yet
        info = api.folder_info(self.folder(r, "Old Mill"))
        self.assertEqual({k: info[k] for k in ("ok", "name", "recordings", "with_evps", "evps_at_least",
                                                "backups", "other_files", "subfolders")},
                         {"ok": True, "name": "Old Mill", "recordings": 3, "with_evps": 1, "evps_at_least": True,
                          "backups": 0, "other_files": 2, "subfolders": 4})
        self.assertGreater(info["bytes"], 5)
        self.assertEqual(api.folder_info("nope")["error"], backend.LIB_CHANGED)

    def test_delete_goes_to_the_recycle_bin_never_rmtree(self):
        self.write("Old Mill/A/y.wav", wav_bytes(b"y"))
        self.write("Asylum/z.wav", wav_bytes(b"z"))
        api = self.new_api()
        r = api.list_library()
        with mock.patch.object(shutil, "rmtree", side_effect=AssertionError("never")), \
                mock.patch.object(os, "remove", side_effect=AssertionError("never")), \
                mock.patch.object(os, "rmdir", side_effect=AssertionError("never")):
            res = api.delete_folder(self.folder(r, "Old Mill"))
        self.assertEqual(res, {"ok": True, "backups": 0})
        self.assertEqual([os.path.normcase(p) for p in self.recycled],
                         [os.path.normcase(os.path.abspath(os.path.join(self.lib, "Old Mill")))])
        self.assertEqual(self.names(), ["Asylum"])

    def test_delete_of_the_save_to_folder_is_allowed(self):
        os.makedirs(os.path.join(self.lib, "Exports"))
        api = self.new_api()
        api._dest = os.path.join(self.lib, "Exports")
        r = api.list_library()
        self.assertTrue(api.folder_info(self.folder(r, "Exports"))["save_folder"])
        self.assertTrue(api.delete_folder(self.folder(r, "Exports"))["ok"])

    def test_a_file_in_use_is_left_behind_with_a_plain_partial_result(self):
        self.write("Old Mill/gone.wav", wav_bytes(b"gone"))
        locked = self.write("Old Mill/A/locked.wav", wav_bytes(b"locked"))
        self.write("Old Mill/A/photo.jpg", b"1")
        api = self.new_api(recycle=None)
        r = self.index(api)
        fps = {f["name"]: f["fp"] for f in r["files"]}
        for name in ("gone.wav", "locked.wav"):
            self.store.add_mark(fps[name], 0.1, 0.5, "A", "")
            self.store.set_backup(fps[name], "saved", "Saved")

        def shell(path):                      # what SHFileOperationW does with a file in use
            self.recycled.append(path)
            os.remove(os.path.join(path, "gone.wav"))
            os.remove(os.path.join(path, "A", "photo.jpg"))
            raise folders.RecycleError(folders.IN_USE)
        api._recycle = shell
        res = api.delete_folder(self.folder(r, "Old Mill"))
        self.assertEqual((res["ok"], res["error"], res["backups"]), (False, folders.IN_USE, 1))
        self.assertTrue(os.path.isfile(locked))
        self.assertEqual(self.store.backup(fps["gone.wav"])["status"], "failed")
        self.assertEqual(self.store.backup(fps["locked.wav"])["status"], "saved")
        self.assertFalse(api._fs_op)
        self.assertFalse(api._busy.locked())
        r = api.list_library()                                             # the page relists
        self.assertEqual([(f["name"], f["folder_id"]) for f in r["files"]],
                         [("locked.wav", self.folder(r, "Old Mill", "A"))])
        self.assertEqual(r["files"][0]["fp"], fps["locked.wav"])
        info = api.folder_info(self.folder(r, "Old Mill"))
        self.assertEqual((info["recordings"], info["other_files"], info["backups"]), (1, 0, 1))

    def test_delete_refuses_root_unknown_escaped_and_busy(self):
        self.write("Old Mill/y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = api.list_library()
        mill = self.folder(r, "Old Mill")
        self.assertFalse(api.delete_folder("root")["ok"])
        self.assertEqual(api.delete_folder("nope")["error"], backend.LIB_CHANGED)
        with mock.patch.object(backend.folders, "inside", return_value=False):
            self.assertEqual(api.delete_folder(mill)["error"], backend.LIB_CHANGED)
        api._busy.acquire()                                              # an export runs
        try:
            self.assertEqual(api.delete_folder(mill)["error"], "Wait for the export or backup to finish.")
        finally:
            api._busy.release()
        with mock.patch.object(api, "backing_up", return_value=True):
            self.assertEqual(api.delete_folder(mill)["error"], backend.FS_BUSY)
        self.assertFalse(api._fs_op)
        self.assertEqual(self.recycled, [])
        self.assertEqual(self.names(), ["Old Mill"])

    def test_recycle_failure_leaves_the_folder_and_says_so(self):
        self.write("Old Mill/y.wav", wav_bytes(b"y"))

        def refuse(path):
            raise folders.RecycleError(folders.NO_RECYCLE_BIN)

        def crash(path):
            raise OSError(5, "Access is denied", path)
        for recycle, expected in ((refuse, folders.NO_RECYCLE_BIN), (crash, "Access is denied (Old Mill)")):
            api = self.new_api(recycle=recycle)
            r = api.list_library()
            res = api.delete_folder(self.folder(r, "Old Mill"))
            self.assertFalse(res["ok"])
            self.assertIn(expected, res["error"])
            self.assertNotIn(self.tmp, res["error"])
            self.assertEqual(self.names(), ["Old Mill"])
            self.assertFalse(api._busy.locked())

    def test_backups_that_went_to_the_recycle_bin_show_as_failed(self):
        self.write("Save/A/only.wav", wav_bytes(b"only"))          # its only copy
        self.write("Save/A/both.wav", wav_bytes(b"both"))          # another copy stays
        self.write("Elsewhere/both.wav", wav_bytes(b"both"))
        self.write("Save/A/unsaved.wav", wav_bytes(b"unsaved"))
        api = self.new_api()
        r = self.index(api)
        fps = {f["name"]: f["fp"] for f in r["files"]}
        for name in ("only.wav", "both.wav"):
            self.store.add_mark(fps[name], 0.1, 0.5, "A", "")
            self.store.set_backup(fps[name], "saved", f"Saved as {name}")
        self.store.add_mark(fps["unsaved.wav"], 0.1, 0.5, "A", "")
        self.store.set_backup(fps["unsaved.wav"], "failed", "no")
        info = api.folder_info(self.folder(r, "Save"))
        self.assertEqual((info["recordings"], info["with_evps"], info["backups"]), (3, 3, 1))
        res = api.delete_folder(self.folder(r, "Save"))
        self.assertEqual(res, {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup(fps["only.wav"]), {"status": "failed", "detail": backend.BACKUP_RECYCLED})
        self.assertEqual(self.store.backup(fps["both.wav"])["status"], "saved")
        self.assertEqual(self.store.backup(fps["unsaved.wav"]), {"status": "failed", "detail": "no"})
        self.assertEqual(len(self.store.marks(fps["only.wav"])), 1)      # marks are never deleted

    def test_delete_pauses_the_indexer_first(self):
        self.write("Old Mill/x.dvf", dvf_bytes(1))
        self.write("Old Mill/y.dvf", dvf_bytes(2))
        fake = FakeDecoder(block=1, honour_stop=True)
        api = self.new_api()
        with fake.installed():
            r = api.list_library()
            self.assertTrue(fake.started.wait(WAIT))
            res = api.delete_folder(self.folder(r, "Old Mill"))
            self.assertTrue(res["ok"], res)
            self.assertTrue(fake.saw_stop)                                 # the decode was cancelled
            self.assertIsNone(api._indexer)
            self.assertEqual(fake.calls, 1)
            r = api.list_library()
        self.assertEqual(r["files"], [])
        self.assertNotIn("paused", r)

    def test_listing_during_an_operation_starts_no_indexer(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        with api._lib_lock:
            api._fs_op = True
        r = api.list_library()
        self.assertEqual((r["indexing"], r.get("paused")), (False, True))
        self.assertIsNone(api._indexer)
        with api._lib_lock:
            api._fs_op = False
        self.index(api)                                                   # indexes again afterwards
        self.assertTrue(api.list_library()["files"][0]["fp"])

    def test_a_scan_that_overlapped_an_operation_is_not_used(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        first = api.list_library()
        self.events.wait_done(first["scan_id"])
        table = dict(api._library)
        self.write("b.wav", wav_bytes(b"b"))
        real = backend._scan_library

        def overlapped(folder):
            found = real(folder)
            with api._lib_lock:
                api._fs_gen += 2                                          # an operation started and ended
            return found
        with mock.patch.object(backend, "_scan_library", overlapped):
            r = api.list_library()
        self.assertEqual((r["paused"], r["indexing"]), (True, False))
        self.assertEqual(api._library, table)                              # the older table is kept
        self.assertIsNone(api._lib_job)
        r = api.list_library()                                             # the relist after it
        self.assertNotIn("paused", r)
        self.assertEqual(len(api._library), 2)

    def test_backup_worker_waits_for_a_folder_operation(self):
        api = self.new_api()
        ran = threading.Event()
        with mock.patch.object(api, "_backup", side_effect=lambda fp, rec, entry: ran.set()):
            with api._lib_lock:
                api._fs_op = True
            with api._backup_lock:
                api._backup_queue["fp"] = ("rec", {"source": {"label": "A-001"}})
                api._backup_thread = threading.Thread(target=api._backup_worker, name="backup")
                api._backup_thread.start()
                api._add_worker(api._backup_thread)
            self.assertFalse(ran.wait(0.3))
            self.assertTrue(api.backing_up())
            with api._lib_lock:
                api._fs_op = False
            self.assertTrue(ran.wait(WAIT))


class MoveTests(FolderApiBase):
    def test_move_into_a_nested_folder_keeps_fingerprints_without_decoding(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", wav_bytes(b"x"))
        os.makedirs(os.path.join(self.lib, "Old Mill", "Night 1"))
        fake = FakeDecoder()
        api = self.new_api()
        with fake.installed():
            r = self.index(api)
            self.assertEqual(fake.calls, 1)
            before = {f["name"]: f["fp"] for f in r["files"]}
            ids = [self.file(r, "x.dvf"), self.file(r, "x.wav")]
            res = api.move_files(ids, self.folder(r, "Old Mill", "Night 1"))
            self.assertEqual((res["ok"], res["moved"], res["skipped"], res["renamed"], res["failed"]),
                             (True, 2, 0, [], []))
            r = api.list_library()
        self.assertEqual((r["indexing"], r["pending"]), (False, 0))
        self.assertEqual(fake.calls, 1)                                    # no decode after the move
        self.assertEqual({f["name"]: f["fp"] for f in r["files"]}, before)
        self.assertEqual(self.names("Old Mill", "Night 1"), ["x.dvf", "x.wav"])
        self.assertEqual(set(res["ids"].values()), {f["id"] for f in r["files"]})
        self.assertEqual(set(res["ids"]), set(ids))

    def test_clash_gives_one_shared_number_and_same_folder_is_skipped(self):
        self.write("x.dvf", dvf_bytes())
        self.write("x.wav", wav_bytes(b"x"))
        self.write("A/x.wav", wav_bytes(b"other"))                 # only the .wav clashes
        self.write("A/x (2).dvf", dvf_bytes(7))                   # and "(2)" is taken for the .dvf
        self.write("A/here.wav", wav_bytes(b"here"))
        with FakeDecoder().installed():
            api = self.new_api()
            r = api.list_library()
            res = api.move_files([self.file(r, "x.dvf"), self.file(r, "x.wav"), self.file(r, "here.wav")],
                                 self.folder(r, "A"))
        self.assertEqual((res["moved"], res["skipped"], res["failed"]), (2, 1, []))
        self.assertEqual(sorted(res["renamed"], key=lambda x: x["from"]),
                         [{"from": "x.dvf", "to": "x (3).dvf"}, {"from": "x.wav", "to": "x (3).wav"}])
        self.assertEqual(self.names("A"), ["here.wav", "x (2).dvf", "x (3).dvf", "x (3).wav", "x.wav"])
        with open(os.path.join(self.lib, "A", "x.wav"), "rb") as f:
            self.assertEqual(f.read(), wav_bytes(b"other"))            # never overwritten

    def test_one_failure_does_not_stop_the_others(self):
        self.write("a.wav", wav_bytes(b"a"))
        self.write("b.wav", wav_bytes(b"b"))
        self.write("c.wav", wav_bytes(b"c"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        real = os.rename

        def rename(src, dst):
            if os.path.basename(src) == "b.wav":
                raise PermissionError(13, "Access is denied", src)
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename):
            res = api.move_files([self.file(r, n) for n in ("a.wav", "b.wav", "c.wav")], self.folder(r, "T"))
        self.assertEqual((res["ok"], res["moved"]), (True, 2))
        self.assertEqual(res["failed"], [{"name": "b.wav",
                                          "error": "It is open in another program (close it and try again)."}])
        self.assertEqual(self.names("T"), ["a.wav", "c.wav"])
        self.assertEqual(self.names(), ["T", "b.wav"])

    def test_cross_volume_is_reported_never_copied(self):
        self.write("a.wav", wav_bytes(b"a"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        err = OSError(18, "Invalid cross-device link")
        with mock.patch.object(backend.os, "rename", side_effect=err), \
                mock.patch.object(shutil, "copy2", side_effect=AssertionError("never")), \
                mock.patch.object(shutil, "move", side_effect=AssertionError("never")):
            res = api.move_files([self.file(r, "a.wav")], self.folder(r, "T"))
        self.assertEqual(res["failed"], [{"name": "a.wav", "error": "It is on another drive, so it was not moved."}])
        self.assertEqual(self.names(), ["T", "a.wav"])

    @unittest.skipUnless(WINDOWS, "Windows refuses to move a file held open without delete sharing")
    def test_a_file_held_open_elsewhere_fails_plainly(self):
        held = self.write("a.wav", wav_bytes(b"a"))
        self.write("b.wav", wav_bytes(b"b"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        with open(held, "rb"):
            res = api.move_files([self.file(r, "a.wav"), self.file(r, "b.wav")], self.folder(r, "T"))
        self.assertEqual(res["moved"], 1)
        self.assertEqual([f["name"] for f in res["failed"]], ["a.wav"])
        self.assertIn("open in another program", res["failed"][0]["error"])
        self.assertEqual(self.names("T"), ["b.wav"])

    def test_refusals(self):
        self.write("a.wav", wav_bytes(b"a"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        a, t = self.file(r, "a.wav"), self.folder(r, "T")
        self.assertFalse(api.move_files([], t)["ok"])
        self.assertFalse(api.move_files("a", t)["ok"])
        self.assertFalse(api.move_files([1], t)["ok"])
        self.assertEqual(api.move_files(["nope"], t)["error"], backend.LIB_CHANGED)
        self.assertEqual(api.move_files([a], "nope")["error"], backend.LIB_CHANGED)
        api._busy.acquire()
        try:
            self.assertEqual(api.move_files([a], t)["error"], backend.FS_BUSY)
        finally:
            api._busy.release()
        with mock.patch.object(backend.folders, "inside", return_value=False):
            self.assertEqual(api.move_files([a], t)["error"], backend.LIB_CHANGED)
        os.remove(os.path.join(self.lib, "a.wav"))
        res = api.move_files([a], t)
        self.assertEqual((res["ok"], res["moved"], [f["name"] for f in res["failed"]]), (False, 0, ["a.wav"]))
        self.assertEqual(res["error"], "a.wav was not moved. It is no longer there. Refresh the list.")

    def test_a_failing_group_or_rekey_does_not_stop_the_rest(self):
        self.write("a.dvf", dvf_bytes(1))
        self.write("a.wav", wav_bytes(b"a"))
        self.write("b.wav", wav_bytes(b"b"))
        self.write("c.wav", wav_bytes(b"c"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = self.index(api)                             # no indexer left to flush meanwhile
        real_scandir = os.scandir
        calls = []

        def scandir(path="."):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.join(self.lib, "T")):
                calls.append(path)
                if len(calls) == 1:                     # the first group cannot look into the target
                    raise PermissionError(13, "Access is denied", path)
            return real_scandir(path)
        real_retarget = api._retarget_prefix

        def retarget(old, new):
            if os.path.basename(old) == "b.wav":
                raise RuntimeError("cache trouble")
            return real_retarget(old, new)
        flushed = []
        with mock.patch.object(os, "scandir", scandir), \
                mock.patch.object(api, "_retarget_prefix", retarget), \
                mock.patch.object(api, "_flush_index", lambda: flushed.append(1)):
            res = api.move_files([self.file(r, n) for n in ("a.dvf", "a.wav", "b.wav", "c.wav")],
                                 self.folder(r, "T"))
        self.assertEqual((res["ok"], res["moved"]), (True, 2))
        self.assertEqual(sorted(f["name"] for f in res["failed"]), ["a.dvf", "a.wav"])
        self.assertIn("Access is denied", res["failed"][0]["error"])
        self.assertEqual(self.names("T"), ["b.wav", "c.wav"])            # b.wav moved despite the re-key error
        self.assertEqual(flushed, [1])
        self.assertFalse(api._busy.locked())

    def test_a_read_only_store_keeps_fingerprints_for_the_session(self):
        self.write("x.dvf", dvf_bytes())
        os.makedirs(os.path.join(self.lib, "T"))
        fake = FakeDecoder()
        with fake.installed():
            self.index(self.new_api())                                     # the writing window indexed it
            self.store.flush_index()
            second = AppData(os.path.join(self.tmp, "appdata"))
            self.addCleanup(second.close)
            self.assertTrue(second.read_only)
            api = self.new_api(store=second)
            r = api.list_library()
            self.assertEqual(r["pending"], 0)
            res = api.move_files([self.file(r, "x.dvf")], self.folder(r, "T"))
            self.assertEqual(res["moved"], 1, res)
            r = api.list_library()
        self.assertEqual((r["pending"], fake.calls), (0, 1))
        self.assertTrue(r["files"][0]["fp"])

    def test_a_loaded_recording_keeps_playing_after_its_folder_moves(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        server = AudioServer(lambda key: b"", cache.name)
        server.start()
        self.addCleanup(server.stop)
        path = self.write("Old Mill/y.wav", wav_bytes(b"y"))
        with open(path, "rb") as f:
            data = f.read()
        os.makedirs(os.path.join(self.lib, "Asylum"))
        api = self.new_api(server=server)
        r = self.index(api)
        loaded = api.play_library(self.file(r, "y.wav"))
        self.assertTrue(loaded["ok"], loaded)
        with urllib.request.urlopen(loaded["url"], timeout=5) as resp:   # being played
            resp.read(100)
            res = api.move_files([self.file(r, "y.wav")], self.folder(r, "Asylum"))
        self.assertEqual(res["moved"], 1, res)
        with urllib.request.urlopen(loaded["url"], timeout=5) as resp:
            self.assertEqual(resp.read(), data)
        self.assertEqual(api.recording_changed(loaded["rec"]), {"ok": True, "changed": False})
        r = api.list_library()
        renamed = api.rename_folder(self.folder(r, "Asylum"), "Asylum 2")
        self.assertTrue(renamed["ok"], renamed)
        with urllib.request.urlopen(loaded["url"], timeout=5) as resp:
            self.assertEqual(resp.read(), data)
        self.assertEqual(api.recording_changed(loaded["rec"]), {"ok": True, "changed": False})
        self.assertTrue(api.add_mark(loaded["rec"], 0.1, 0.3, "B", "knock")["ok"])
        self.assertEqual(len(self.store.marks(wavinfo.wav_fingerprint(
            os.path.join(self.lib, "Asylum 2", "y.wav")))), 1)

    def test_a_renamed_file_updates_the_handle_name(self):
        self.write("y.wav", wav_bytes(b"y"))
        self.write("T/y.wav", wav_bytes(b"other"))
        api = self.new_api()
        r = api.list_library()
        loaded = api.play_library(self.file(r, "y.wav"))
        res = api.move_files([self.file(r, "y.wav")], self.folder(r, "T"))
        self.assertEqual(res["renamed"], [{"from": "y.wav", "to": "y (2).wav"}])
        entry = api._entry(loaded["rec"])
        self.assertEqual((entry["name"], os.path.basename(entry["source"]["path"])), ("y (2).wav", "y (2).wav"))


if __name__ == "__main__":
    unittest.main()
