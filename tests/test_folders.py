"""Library folders: name rules, containment, the Recycle Bin call, and the Api's
create / rename / folder_info / delete_folder / move_files (tasks 2 and 3)."""
import ctypes
import dataclasses
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
from app import backend, folders, library_ops  # noqa: E402
from app.store import StoreUnavailable  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import formats  # noqa: E402
from st25 import audio as st25_audio  # noqa: E402  (the .dvf decoder behind openevp.formats.DVF)
from openevp import wavinfo  # noqa: E402

WINDOWS = sys.platform == "win32"


class _AlwaysUnreadable:
    """A stand-in .dvf decoder: available (so indexing does not stall on
    "pending"), but every recording fails to decode, as for a genuinely
    unreadable one. Independent of the real LPEC tables/DLL, which the
    grouping and backup-identity behaviour under test does not need."""

    def available(self):
        return True

    def reason(self):
        return None

    def warning(self):
        return None

    def to_wav(self, data, should_stop=None):
        raise formats.DecodeError("not decodable (test stub)")


def dvf_decodes_as_unreadable():
    """Patches formats.DVF's decoder (via the registry) for the duration of a
    ``with`` block, so .dvf indexing completes without the real decoder."""
    stub = dataclasses.replace(formats.DVF, decoder=_AlwaysUnreadable())
    return mock.patch.dict(formats._registry, {".dvf": stub})


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

    def fake_recycle(self, path, before=None):
        if before is not None:
            before()                                     # what folders.recycle does before the shell call
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

    def test_a_second_window_organises_nothing(self):
        self.write("Old Mill/y.wav", wav_bytes(b"y"))
        second = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(second.close)
        self.assertTrue(second.read_only)
        api = self.new_api(store=second)
        r = api.list_library()
        mill = self.folder(r, "Old Mill")
        for res in (api.create_folder("root", "New"), api.rename_folder(mill, "Mill 2"),
                    api.delete_folder(mill), api.move_files([self.file(r, "y.wav")], "root")):
            self.assertEqual(res["error"], second.read_only_reason)
            self.assertIn(f"Another OpenEVP (process {os.getpid()}) is open", res["error"])
        self.assertEqual(self.names(), ["Old Mill"])
        self.assertEqual(self.names("Old Mill"), ["y.wav"])
        self.assertEqual(self.recycled, [])
        self.assertFalse(api._busy.locked())
        self.assertTrue(api.folder_info(mill)["ok"])                       # looking is fine

    def test_a_briefly_locked_folder_is_renamed_after_a_retry(self):
        os.makedirs(os.path.join(self.lib, "Old Mill"))
        api = self.new_api()
        r = api.list_library()
        real, calls = os.rename, []

        def rename(src, dst):
            calls.append(src)
            if len(calls) < 3:                                  # an antivirus scan holds it twice
                e = PermissionError(13, "The process cannot access the file")
                e.winerror = 32
                raise e
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename),                 mock.patch.object(library_ops, "RENAME_PAUSE", 0.01):
            res = api.rename_folder(self.folder(r, "Old Mill"), "Mill 2")
        self.assertTrue(res["ok"], res)
        self.assertEqual((len(calls), self.names()), (3, ["Mill 2"]))

        def always(src, dst):
            calls.append(src)
            e = PermissionError(13, "The process cannot access the file", src)
            e.winerror = 32
            raise e
        calls.clear()
        r = api.list_library()
        with mock.patch.object(backend.os, "rename", always),                 mock.patch.object(library_ops, "RENAME_PAUSE", 0.01):
            res = api.rename_folder(self.folder(r, "Mill 2"), "Mill 3")
        self.assertEqual(len(calls), library_ops.RENAME_TRIES)
        self.assertIn("open in another program", res["error"])

    def test_a_folder_operation_is_not_an_export(self):
        api = self.new_api()
        api.list_library()
        seen = []
        real = os.mkdir

        def mkdir(path, *a, **kw):
            seen.append(api.exporting())
            return real(path, *a, **kw)
        with mock.patch.object(backend.os, "mkdir", mkdir):
            self.assertTrue(api.create_folder("root", "A")["ok"])
        self.assertEqual(seen, [False])
        api._busy.acquire()                                               # an export
        try:
            self.assertTrue(api.exporting())
        finally:
            api._busy.release()

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

        def shell(path, before=None):         # what SHFileOperationW does with a file in use
            self.recycled.append(path)
            os.remove(os.path.join(path, "gone.wav"))
            os.remove(os.path.join(path, "A", "photo.jpg"))
            raise folders.RecycleError(folders.IN_USE.format("Old Mill"))
        api._recycle = shell
        res = api.delete_folder(self.folder(r, "Old Mill"))
        self.assertEqual((res["ok"], res["error"], res["backups"]),
                         (False, "Old Mill was not (completely) moved to the Recycle Bin: a file is in use, "
                                 "or deleting was cancelled.", 1))
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

        def refuse(path, before=None):
            raise folders.RecycleError(folders.NO_RECYCLE_BIN)

        def crash(path, before=None):
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
        # both.wav counts too: a recording in the folder has its fingerprint, and
        # without recorded files nothing says which copy was the backup (Q11).
        self.assertEqual((info["recordings"], info["with_evps"], info["backups"]), (3, 3, 2))
        res = api.delete_folder(self.folder(r, "Save"))
        self.assertEqual(res, {"ok": True, "backups": 2})
        self.assertEqual(self.store.backup(fps["only.wav"]), {"status": "failed", "detail": backend.BACKUP_RECYCLED})
        self.assertEqual(self.store.backup(fps["both.wav"])["status"], "failed")
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

        def overlapped(folder, clips=None):
            found = real(folder, clips)
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
        with dvf_decodes_as_unreadable():
            r = self.index(api)                         # no indexer left to flush meanwhile
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

    def test_a_briefly_locked_file_is_moved_after_a_retry(self):
        self.write("a.wav", wav_bytes(b"a"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        real, calls = os.rename, []

        def rename(src, dst):
            calls.append(src)
            if len(calls) == 1:
                e = PermissionError(13, "The process cannot access the file")
                e.winerror = 33
                raise e
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename),                 mock.patch.object(library_ops, "RENAME_PAUSE", 0.01):
            res = api.move_files([self.file(r, "a.wav")], self.folder(r, "T"))
        self.assertEqual((res["ok"], res["moved"], res["failed"], len(calls)), (True, 1, [], 2))

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


class RecycleBinSettingsTests(unittest.TestCase):
    """Refused up front what the Recycle Bin would delete for good (a fake registry)."""
    GUID = "{5f1394b7-0000-0000-0000-000000000000}"

    def refuses(self, settings, size, guid=GUID):
        asked = []

        def settings_of(g):
            asked.append(g)
            return settings
        result = folders.bin_refuses("C:\\", size, guid_of=lambda root: guid, settings_of=settings_of)
        self.assertEqual(asked, [guid] if guid else [])
        return result

    def test_immediate_delete_and_capacity(self):
        mb = 1024 * 1024
        self.assertTrue(self.refuses({"NukeOnDelete": 1}, 1))
        self.assertTrue(self.refuses({"NukeOnDelete": 1, "MaxCapacity": 50000}, 1))
        self.assertTrue(self.refuses({"NukeOnDelete": 0, "MaxCapacity": 10}, 10 * mb + 1))
        self.assertFalse(self.refuses({"NukeOnDelete": 0, "MaxCapacity": 10}, 10 * mb))
        self.assertTrue(self.refuses({"MaxCapacity": 0}, 1))

    def test_missing_settings_mean_windows_defaults(self):
        self.assertFalse(self.refuses({}, 10 ** 12))                  # capacity unknown: never refused on size
        self.assertFalse(self.refuses({"NukeOnDelete": 0}, 10 ** 12))
        self.assertFalse(self.refuses({"NukeOnDelete": 1}, 1, guid=None))   # no volume GUID: nothing known

    def test_registry_reader(self):
        class Key:
            def __init__(self, values):
                self.values = values

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        fake = types.SimpleNamespace(HKEY_CURRENT_USER="HKCU", REG_DWORD=4, REG_SZ=1)
        opened = []

        def open_key(hive, sub, values):
            opened.append((hive, sub))
            if values is None:
                raise FileNotFoundError(2, "missing")
            return Key(values)

        def query(key, name):
            if name not in key.values:
                raise FileNotFoundError(2, "missing")
            return key.values[name]
        for values, expected in (({"NukeOnDelete": (1, 4), "MaxCapacity": (300, 4)},
                                   {"NukeOnDelete": 1, "MaxCapacity": 300}),
                                  ({"MaxCapacity": ("300", 1)}, {}),                  # not a DWORD: ignored
                                  ({}, {}), (None, {})):
            fake.OpenKey = lambda hive, sub, v=values: open_key(hive, sub, v)
            fake.QueryValueEx = query
            with mock.patch.dict(sys.modules, {"winreg": fake}):
                self.assertEqual(folders.bin_settings(self.GUID), expected)
        self.assertEqual(opened[0], ("HKCU", folders.BITBUCKET + "\\" + self.GUID))

    def test_tree_size(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "a", "b"))
            for rel, n in (("x", 3), ("a/y", 5), ("a/b/z", 7)):
                with open(os.path.join(d, *rel.split("/")), "wb") as f:
                    f.write(b"." * n)
            self.assertEqual(folders.tree_size(d), 15)

    @unittest.skipUnless(WINDOWS, "Windows only")
    def test_recycle_refuses_before_the_shell_is_asked(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "Old Mill")
            os.makedirs(target)
            with open(os.path.join(target, "a.wav"), "wb") as f:
                f.write(b"x" * 10)
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
            if kernel32.GetDriveTypeW(os.path.splitdrive(target)[0] + "\\") != folders.DRIVE_FIXED:
                self.skipTest("the temp folder is not on a fixed drive")
            for settings in ({"NukeOnDelete": 1}, {"MaxCapacity": 0}):
                with mock.patch.object(folders, "bin_settings", return_value=settings):
                    with self.assertRaises(folders.RecycleError) as caught:
                        folders.recycle(target)
                self.assertEqual(str(caught.exception), folders.NO_ROOM)
                self.assertTrue(os.path.isfile(os.path.join(target, "a.wav")))

    def test_messages(self):
        self.assertIn("can't go to the Recycle Bin", folders.NO_ROOM)
        self.assertEqual(folders.IN_USE.format("Old Mill"),
                         "Old Mill was not (completely) moved to the Recycle Bin: a file is in use, "
                         "or deleting was cancelled.")
        self.assertEqual(folders.RECYCLE_FLAGS & folders.FOF_WANTNUKEWARNING, folders.FOF_WANTNUKEWARNING)


class RootAndWindowTests(FolderApiBase):
    def test_a_redirected_library_folder_is_refused(self):
        self.write("Old Mill/y.wav", wav_bytes(b"y"))
        os.makedirs(os.path.join(self.tmp, "Outside", "Old Mill"))
        api = self.new_api()
        r = api.list_library()
        mill, y = self.folder(r, "Old Mill"), self.file(r, "y.wav")
        real = os.path.realpath
        lib_key = os.path.normcase(os.path.abspath(self.lib))

        def redirected(p, *a, **kw):                    # the library folder now resolves elsewhere
            p = os.path.abspath(p)
            key = os.path.normcase(p)
            if key == lib_key or key.startswith(lib_key + os.sep):
                return os.path.join(self.tmp, "Outside") + p[len(lib_key):]
            return real(p, *a, **kw)

        def calls():
            return (api.create_folder("root", "New"), api.rename_folder(mill, "X"), api.delete_folder(mill),
                    api.move_files([y], mill), api.folder_info(mill))
        with mock.patch.object(os.path, "realpath", redirected):
            for res in calls():
                self.assertEqual(res["error"], backend.ROOT_CHANGED)
        real_link = folders.is_link
        with mock.patch.object(folders, "is_link",              # the library folder became a junction
                               lambda p: os.path.normcase(os.path.abspath(p)) == lib_key or real_link(p)):
            for res in calls():
                self.assertEqual(res["error"], backend.ROOT_CHANGED)
        self.assertEqual(self.recycled, [])
        self.assertEqual(self.names(), ["Old Mill"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "Outside", "Old Mill")), [])
        api.list_library()                                      # listed again: fine again
        self.assertTrue(api.create_folder("root", "New")["ok"])

    def test_a_library_folder_that_was_a_link_already_is_accepted(self):
        os.makedirs(os.path.join(self.lib, "A"))
        lib_key = os.path.normcase(os.path.abspath(self.lib))
        real_link = folders.is_link
        with mock.patch.object(folders, "is_link",
                               lambda p: os.path.normcase(os.path.abspath(p)) == lib_key or real_link(p)):
            api = self.new_api()
            r = api.list_library()
            self.assertTrue(api.rename_folder(self.folder(r, "A"), "B")["ok"])

    def test_delete_is_refused_when_backup_states_cannot_be_saved(self):
        path = self.write("A/x.dvf", dvf_bytes())
        api = self.new_api()
        r = api.list_library()
        self.store.set_backup("fp1", "saved", "Saved as x.dvf", [path])
        with mock.patch.object(self.store, "set_backups", side_effect=StoreUnavailable("disk full")):
            res = api.delete_folder(self.folder(r, "A"))
        self.assertFalse(res["ok"])
        self.assertIn("not deleted", res["error"])
        self.assertIn("disk full", res["error"])
        self.assertEqual((self.recycled, self.names("A")), ([], ["x.dvf"]))
        self.assertEqual(self.store.backup("fp1")["status"], "saved")
        self.assertFalse(api._busy.locked())

    def test_backup_states_are_saved_before_the_folder_goes(self):
        path = self.write("A/x.dvf", dvf_bytes())
        api = self.new_api()
        r = api.list_library()
        self.store.set_backup("fp1", "saved", "Saved as x.dvf", [path])
        seen = []
        api._recycle = lambda p, before=None: (seen.append(self.store.backup("fp1")["status"]),
                                                self.fake_recycle(p, before))
        self.assertEqual(api.delete_folder(self.folder(r, "A")), {"ok": True, "backups": 1})
        self.assertEqual(seen, ["failed"])


@unittest.skipUnless(WINDOWS, "directory handles are Windows-only")
class PinTests(FolderApiBase):
    def try_rename(self, path):
        try:
            os.rename(path, path + " moved")
        except OSError as e:
            return e.winerror
        os.rename(path + " moved", path)
        return "renamed"

    def test_pins_hold_a_folder_and_its_parents_only(self):
        a = os.path.join(self.lib, "A")
        os.makedirs(os.path.join(a, "B"))
        with folders.Pins() as pins:
            pins.chain(self.lib, a)
            self.assertEqual(self.try_rename(self.lib), 32)
            self.assertEqual(self.try_rename(a), 32)
            self.assertEqual(self.try_rename(os.path.join(a, "B")), "renamed")   # inside: free
            pins.release(a)
            self.assertEqual(self.try_rename(a), "renamed")
        self.assertEqual(self.try_rename(self.lib), "renamed")
        with self.assertRaises(OSError):
            folders.Pins().add(os.path.join(self.lib, "missing"))

    def test_a_linked_library_folder_holds_its_target_too(self):
        target = os.path.join(self.tmp, "Real library")
        os.makedirs(os.path.join(target, "Old Mill"))
        link = os.path.join(self.tmp, "Linked")
        done = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        if done.returncode != 0:
            self.skipTest("could not create a junction")
        self.addCleanup(os.rmdir, link)
        with folders.Pins() as pins:
            pins.chain(link, link)
            self.assertEqual(self.try_rename(target), 32)            # the real folder cannot be swapped
        self.assertEqual(self.try_rename(target), "renamed")
        self.lib = link
        api = self.new_api()
        api._lib_folder = link
        r = api.list_library()
        seen = []

        def recycle(path, before=None):
            before()
            seen.append(self.try_rename(target))                     # a direct child is going: still held
            self.fake_recycle(path)
        api._recycle = recycle
        self.assertEqual(api.delete_folder(self.folder(r, "Old Mill")), {"ok": True, "backups": 0})
        self.assertEqual(seen, [32])

    def test_the_library_folder_cannot_be_replaced_during_a_delete(self):
        self.write("Old Mill/A/y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = api.list_library()
        seen = {}
        real_walk = api._walk

        def walk(path):                                  # the long part of a delete
            seen.setdefault("root", self.try_rename(self.lib))
            seen.setdefault("folder", self.try_rename(os.path.join(self.lib, "Old Mill")))
            return real_walk(path)

        def recycle(path, before=None):
            seen["folder before the shell"] = self.try_rename(os.path.join(self.lib, "Old Mill"))
            before()
            seen["root at recycle"] = self.try_rename(self.lib)
            self.fake_recycle(path)                     # the folder itself is free to go
        api._recycle = recycle
        with mock.patch.object(api, "_walk", walk):
            self.assertEqual(api.delete_folder(self.folder(r, "Old Mill")), {"ok": True, "backups": 0})
        self.assertEqual(seen, {"root": 32, "folder": 32, "folder before the shell": 32, "root at recycle": 32})
        self.assertEqual(self.try_rename(self.lib), "renamed")               # let go afterwards

    def test_rename_and_move_hold_the_library_folder(self):
        self.write("Old Mill/y.wav", wav_bytes(b"y"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        seen = []
        real = os.rename

        def rename(src, dst):
            try:
                real(self.lib, self.lib + " moved")
                seen.append("renamed")
                real(self.lib + " moved", self.lib)
            except OSError as e:
                seen.append(e.winerror)
            return real(src, dst)
        with mock.patch.object(backend.os, "rename", rename):
            self.assertTrue(api.move_files([self.file(r, "y.wav")], self.folder(r, "T"))["ok"])
            r = api.list_library()
            self.assertTrue(api.rename_folder(self.folder(r, "Old Mill"), "Mill 2")["ok"])
        self.assertEqual(seen, [32, 32])
        self.assertEqual(self.names(), ["Mill 2", "T"])


class SecondWindowExportTests(FolderApiBase):
    def test_a_second_window_does_not_export(self):
        second = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        with mock.patch.object(backend.threading, "Thread", side_effect=AssertionError("never")):
            res = api.export("dev", [{"folder": "A", "number": 1}], "dvf", self.lib, "job")
        self.assertEqual(res["error"], second.read_only_reason)
        self.assertIn(f"Another OpenEVP (process {os.getpid()}) is open", res["error"])
        self.assertEqual(api.export_marked("rec")["error"], second.read_only_reason)
        self.assertFalse(api._busy.locked())

    def test_an_unopenable_lock_file_is_said_as_it_is(self):
        folder = os.path.join(self.tmp, "appdata2")
        os.makedirs(os.path.join(folder, ".lock"))               # can't be opened as a file
        store = AppData(folder)
        self.addCleanup(store.close)
        api = self.new_api(store=store)
        reason = store.read_only_reason
        self.assertTrue(reason.startswith("OpenEVP couldn't open its data lock file"), reason)
        with mock.patch.object(backend.threading, "Thread", side_effect=AssertionError("never")):
            self.assertEqual(api.export("dev", [{"folder": "A", "number": 1}], "dvf", self.lib, "job")["error"], reason)
        self.assertEqual(api.export_marked("rec")["error"], reason)
        api.list_library()
        self.assertEqual(api.create_folder("root", "New")["error"], reason)
        self.assertNotIn("Another OpenEVP", reason)


class BackupIdentityTests(FolderApiBase):
    """Backups are known by the files they saved (recorded with the backup)."""

    def backup(self, fp, *rels):
        paths = [os.path.join(self.lib, *rel.split("/")) for rel in rels]
        self.store.set_backup(fp, "saved", "Saved", paths)

    def test_an_unindexed_backup_is_counted_and_invalidated(self):
        self.write("Save/A/x.dvf", dvf_bytes())
        self.write("Save/A/x.wav", wav_bytes(b"x"))
        api = self.new_api()
        r = api.list_library()                                   # nothing fingerprinted yet
        self.backup("fpX", "Save/A/x.dvf", "Save/A/x.wav")
        self.store.add_mark("fpX", 0.1, 0.5, "A", "")
        self.assertEqual(api.folder_info(self.folder(r, "Save"))["backups"], 1)
        self.assertEqual(api.folder_info(self.folder(r, "Save", "A"))["backups"], 1)
        self.assertEqual(api.delete_folder(self.folder(r, "Save")), {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup_record("fpX"),
                         {"status": "failed", "detail": backend.BACKUP_RECYCLED, "paths": []})

    def test_detection_is_the_union_of_recorded_files_and_fingerprints(self):
        self.write("Save/A/x.wav", wav_bytes(b"x"))
        self.write("Mine/x.wav", wav_bytes(b"x"))                 # a copy of the backed-up recording
        self.write("Other/z.wav", wav_bytes(b"z"))
        api = self.new_api()
        r = self.index(api)
        fp = next(f["fp"] for f in r["files"] if f["name"] == "x.wav")
        self.backup(fp, "Save/A/x.wav")
        self.assertEqual(api.folder_info(self.folder(r, "Other"))["backups"], 0)
        self.assertEqual(api.folder_info(self.folder(r, "Save"))["backups"], 1)     # by its recorded file
        self.assertEqual(api.folder_info(self.folder(r, "Mine"))["backups"], 1)     # by its fingerprint
        self.assertEqual(api.delete_folder(self.folder(r, "Mine")), {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup(fp), {"status": "failed", "detail": backend.BACKUP_RECYCLED})

    def test_a_stale_path_reused_by_another_file_does_not_hide_the_backup(self):
        self.write("Save/A/x.dvf", dvf_bytes(9))                  # an unrelated recording, same name
        self.write("T/x.wav", wav_bytes(b"x"))                    # the backup, moved while unrecorded
        api = self.new_api()
        with FakeDecoder().installed():
            r = self.index(api)
        fp = next(f["fp"] for f in r["files"] if f["name"] == "x.wav")
        self.backup(fp, "Save/A/x.dvf")
        self.assertEqual(api.delete_folder(self.folder(r, "T")), {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup(fp)["status"], "failed")

    def test_a_moved_dvf_is_found_although_its_wav_stayed(self):
        self.write("Save/A/x.dvf", dvf_bytes(4))
        self.write("Save/A/x.wav", wav_bytes(b"x"))
        api = self.new_api()
        with FakeDecoder().installed():
            r = self.index(api)
        fp = next(f["fp"] for f in r["files"] if f["name"] == "x.dvf")
        self.store.set_backup(fp, "saved", "Saved", [os.path.join(self.lib, "Save", "A", "x.dvf"),
                                                     os.path.join(self.lib, "Save", "A", "x.wav")])
        os.makedirs(os.path.join(self.lib, "T"))
        os.rename(os.path.join(self.lib, "Save", "A", "x.dvf"),       # moved by hand: unrecorded
                  os.path.join(self.lib, "T", "x.dvf"))
        with FakeDecoder().installed():
            r = self.index(api)
        self.assertEqual(api.delete_folder(self.folder(r, "T")), {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup(fp)["status"], "failed")

    def test_backups_whose_recorded_files_are_all_gone_fall_back_to_the_fingerprint(self):
        # Moved without the new place being recorded (the store could not be
        # written then): the recorded paths name nothing any more.
        self.write("Elsewhere/y.wav", wav_bytes(b"y"))
        api = self.new_api()
        r = self.index(api)
        fp = r["files"][0]["fp"]
        self.backup(fp, "Save/A/y.wav")                           # not there
        self.assertEqual(api.folder_info(self.folder(r, "Elsewhere"))["backups"], 1)
        self.assertEqual(api.delete_folder(self.folder(r, "Elsewhere")), {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup(fp)["status"], "failed")

    def test_backup_paths_are_saved_before_a_move_or_the_move_is_refused(self):
        self.write("Save/A/x.wav", wav_bytes(b"x"))
        self.write("Save/A/other.wav", wav_bytes(b"other"))       # not a backup: moves anyway
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = self.index(api)
        fp = next(f["fp"] for f in r["files"] if f["name"] == "x.wav")
        old = os.path.join(self.lib, "Save", "A", "x.wav")
        self.backup(fp, "Save/A/x.wav")
        real_move = self.store.move_backup_paths
        with mock.patch.object(self.store, "move_backup_paths",
                               side_effect=lambda o, n: real_move(o, n) if not folders.under(old, o)
                               else (_ for _ in ()).throw(StoreUnavailable("disk full"))):
            res = api.move_files([self.file(r, "x.wav"), self.file(r, "other.wav")], self.folder(r, "T"))
        self.assertEqual((res["moved"], [f["name"] for f in res["failed"]]), (1, ["x.wav"]))
        self.assertIn("recorder backup", res["failed"][0]["error"])
        self.assertEqual((self.names("Save", "A"), self.names("T")), (["x.wav"], ["other.wav"]))
        self.assertEqual(self.store.backup_record(fp)["paths"], [old])
        # recorded before the file moves
        seen = []
        real_rename = os.rename

        def rename(src, dst):
            seen.append(self.store.backup_record(fp)["paths"])
            return real_rename(src, dst)
        r = api.list_library()
        with mock.patch.object(backend.os, "rename", rename):
            self.assertEqual(api.move_files([self.file(r, "x.wav")], self.folder(r, "T"))["moved"], 1)
        new = os.path.join(os.path.abspath(self.lib), "T", "x.wav")
        self.assertEqual(seen, [[new]])
        self.assertEqual(self.store.backup_record(fp)["paths"], [new])

    def test_a_failed_move_or_rename_puts_the_backup_paths_back(self):
        self.write("Save/A/x.wav", wav_bytes(b"x"))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        old = os.path.join(os.path.abspath(self.lib), "Save", "A", "x.wav")
        self.backup("fpX", "Save/A/x.wav")
        err = PermissionError(13, "Access is denied")
        with mock.patch.object(backend.os, "rename", side_effect=err):
            self.assertEqual(api.move_files([self.file(r, "x.wav")], self.folder(r, "T"))["moved"], 0)
            self.assertFalse(api.rename_folder(self.folder(r, "Save"), "Save 2")["ok"])
        self.assertEqual(self.store.backup_record("fpX")["paths"], [old])
        with mock.patch.object(self.store, "move_backup_paths", side_effect=StoreUnavailable("disk full")):
            res = api.rename_folder(self.folder(r, "Save"), "Save 2")
        self.assertIn("could not be recorded", res["error"])
        self.assertEqual(self.names(), ["Save", "T"])

    def test_a_failed_move_does_not_rewrite_an_unrelated_stale_path(self):
        # Astra's reproduction: backup A records A/x.dvf; backup B has a stale
        # path T/x.dvf (B's real recording is elsewhere and cannot be fingerprinted).
        self.write("A/x.dvf", dvf_bytes(1))
        self.write("Elsewhere/b.dvf", dvf_bytes(2))
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        a_path = os.path.join(os.path.abspath(self.lib), "A", "x.dvf")
        stale = os.path.join(os.path.abspath(self.lib), "T", "x.dvf")
        self.store.set_backup("fpA", "saved", "Saved", [a_path])
        self.store.set_backup("fpB", "saved", "Saved", [stale])
        with mock.patch.object(backend.os, "rename", side_effect=PermissionError(13, "Access is denied")):
            res = api.move_files([self.file(r, "x.dvf")], self.folder(r, "T"))
        self.assertEqual(res["moved"], 0)
        self.assertEqual(self.store.backup_record("fpA")["paths"], [a_path])
        self.assertEqual(self.store.backup_record("fpB")["paths"], [stale])       # untouched
        with self.store._lock:                                 # b.dvf: not indexed, and no decoder
            self.store._index["files"].clear()
        with mock.patch.object(st25_audio, "available", return_value=False):
            res = api.delete_folder(self.folder(r, "Elsewhere"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.store.backup("fpB"), {"status": "failed", "detail": library_ops.BACKUP_UNCHECKED})
        self.assertEqual(self.store.backup("fpA")["status"], "saved")

    def test_an_unreadable_recording_marks_unmatched_backups_unchecked(self):
        self.write("A/x.dvf", dvf_bytes(5))
        self.write("Save/y.wav", wav_bytes(b"y"))
        api = self.new_api()
        with dvf_decodes_as_unreadable():
            r = self.index(api)
        y = next(f["fp"] for f in r["files"] if f["name"] == "y.wav")
        self.store.set_backup("fpLegacy", "saved", "Saved")        # could be one of them: nothing says otherwise
        self.backup("fpKnown", "Save/y.wav")                      # its file is where it was recorded
        with self.store._lock:                                     # x.dvf not indexed, and no decoder now
            self.store._index["files"].pop(os.path.normcase(os.path.join(self.lib, "A", "x.dvf")), None)
        with mock.patch.object(st25_audio, "available", return_value=False):
            res = api.delete_folder(self.folder(r, "A"))
        self.assertEqual(res, {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup("fpLegacy"), {"status": "failed", "detail": library_ops.BACKUP_UNCHECKED})
        self.assertEqual(self.store.backup("fpKnown")["status"], "saved")
        self.assertNotEqual(y, "fpKnown")

    def test_unchecked_backups_are_saved_before_the_delete_or_it_is_refused(self):
        self.write("A/bad.wav", b"not a wav")
        api = self.new_api()
        r = self.index(api)
        self.store.set_backup("fpLegacy", "saved", "Saved")
        with mock.patch.object(st25_audio, "available", return_value=False), \
                mock.patch.object(self.store, "set_backups", side_effect=StoreUnavailable("disk full")):
            res = api.delete_folder(self.folder(r, "A"))
        self.assertIn("not deleted", res["error"])
        self.assertEqual((self.recycled, self.names("A")), ([], ["bad.wav"]))
        self.assertEqual(self.store.backup("fpLegacy")["status"], "saved")

    def test_old_backups_without_paths_count_wherever_they_are(self):
        # Ruling Q9: not only in the current Save-to folder (it may have been changed since).
        self.write("A/x.wav", wav_bytes(b"x"))
        self.write("A/y.wav", wav_bytes(b"y"))
        self.write("Elsewhere/y.wav", wav_bytes(b"y"))            # another copy of y stays
        os.makedirs(os.path.join(self.lib, "B"))
        api = self.new_api()
        api._dest = os.path.join(self.lib, "B")                   # Save to changed from A to B
        r = self.index(api)
        fps = {f["name"]: f["fp"] for f in r["files"]}
        for fp in set(fps.values()):
            self.store.set_backup(fp, "saved", "Saved")          # saved by an older OpenEVP: no paths
        self.assertEqual(api.folder_info(self.folder(r, "A"))["backups"], 2)   # a copy elsewhere: still counted
        self.assertEqual(api.delete_folder(self.folder(r, "A")), {"ok": True, "backups": 2})
        self.assertEqual(self.store.backup(fps["x.wav"])["status"], "failed")
        self.assertEqual(self.store.backup(fps["y.wav"])["status"], "failed")

    def test_old_backups_are_found_with_an_empty_index(self):
        self.write("A/x.wav", wav_bytes(b"x"))
        self.write("A/z.dvf", dvf_bytes(3))
        fp = wavinfo.wav_fingerprint(os.path.join(self.lib, "A", "x.wav"))
        self.store.set_backup(fp, "saved", "Saved")               # no paths, and nothing indexed
        fake = FakeDecoder()
        with fake.installed():
            api = self.new_api()
            r = self.index(api)
            with self.store._lock:                                 # the index cache was lost
                self.store._index["files"].clear()
            self.assertIsNone(api._cached_fp(os.path.join(self.lib, "A", "x.wav"), 0, 0))
            res = api.delete_folder(self.folder(r, "A"))
        self.assertEqual(res, {"ok": True, "backups": 1})
        self.assertEqual(self.store.backup(fp)["status"], "failed")

    def test_fingerprinting_stops_when_the_app_closes(self):
        self.write("A/x.wav", wav_bytes(b"x"))
        self.store.set_backup("fpOld", "saved", "Saved")
        api = self.new_api()
        r = api.list_library()
        with mock.patch.object(api, "_index_file", side_effect=lambda *a: (api._stop.set(), (None, False))[1]):
            res = api.delete_folder(self.folder(r, "A"))
        self.assertEqual(res["error"], backend.CLOSING)
        self.assertEqual((self.recycled, self.names("A")), ([], ["x.wav"]))
        self.assertEqual(self.store.backup("fpOld")["status"], "saved")

    def test_moved_and_renamed_backups_are_followed(self):
        self.write("Save/A/x.dvf", dvf_bytes())
        os.makedirs(os.path.join(self.lib, "T"))
        api = self.new_api()
        r = api.list_library()
        self.backup("fpX", "Save/A/x.dvf")
        self.assertTrue(api.rename_folder(self.folder(r, "Save"), "Save 2")["ok"])
        self.assertEqual(self.store.backup_record("fpX")["paths"],
                         [os.path.join(os.path.abspath(self.lib), "Save 2", "A", "x.dvf")])
        r = api.list_library()
        self.assertEqual(api.move_files([self.file(r, "x.dvf")], self.folder(r, "T"))["moved"], 1)
        self.assertEqual(self.store.backup_record("fpX")["paths"],
                         [os.path.join(os.path.abspath(self.lib), "T", "x.dvf")])
        r = api.list_library()
        self.assertEqual(api.folder_info(self.folder(r, "Save 2"))["backups"], 0)
        self.assertEqual(api.folder_info(self.folder(r, "T"))["backups"], 1)

    def test_a_backup_left_behind_is_saved_again(self):
        self.write("Save/A/gone.dvf", dvf_bytes(1))
        locked = self.write("Save/B/locked.dvf", dvf_bytes(2))
        api = self.new_api()
        r = api.list_library()
        self.backup("fpGone", "Save/A/gone.dvf")
        self.backup("fpLocked", "Save/B/locked.dvf")

        def shell(path, before=None):                           # a file in use stays behind
            shutil.rmtree(os.path.join(path, "A"))
            raise folders.RecycleError(folders.IN_USE.format("Save"))
        api._recycle = shell
        res = api.delete_folder(self.folder(r, "Save"))
        self.assertEqual((res["ok"], res["backups"]), (False, 1))
        self.assertEqual(self.store.backup_record("fpGone")["status"], "failed")
        self.assertEqual(self.store.backup_record("fpLocked"),
                         {"status": "saved", "detail": "Saved", "paths": [os.path.abspath(locked)]})


@unittest.skipUnless(WINDOWS, "hidden and system attributes are Windows-only")
class HiddenFolderTests(FolderApiBase):
    def test_hidden_and_system_folders_are_not_listed(self):
        self.write("AppData/x.wav", wav_bytes(b"x"))
        self.write("Sys/y.wav", wav_bytes(b"y"))
        self.write("Shown/z.wav", wav_bytes(b"z"))
        set_attrs = ctypes.windll.kernel32.SetFileAttributesW
        set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        self.assertTrue(set_attrs(os.path.join(self.lib, "AppData"), 0x2))      # hidden
        self.assertTrue(set_attrs(os.path.join(self.lib, "Sys"), 0x4))          # system
        self.addCleanup(set_attrs, os.path.join(self.lib, "Sys"), 0x80)
        self.addCleanup(set_attrs, os.path.join(self.lib, "AppData"), 0x80)
        r = self.new_api().list_library()
        self.assertEqual([f["rel"] for f in r["folders"]], [[], ["Shown"]])
        self.assertEqual([f["name"] for f in r["files"]], ["z.wav"])


if __name__ == "__main__":
    unittest.main()
