"""Sharing library files with other programs (app.sharing): a drag out of the EVP
Library, or Copy file. The page sends only file ids from the latest listing; the
backend resolves them inside the library folder (refusing anything else) and
shares files other programs can play: a WAV or MP3 as itself, a .dvf as the WAV
beside it or as an MP3 made into the share cache (kept for a while: the drop
target may read it after the drag ends). The Windows drag and clipboard calls
are stand-ins here (see test_native_share.py for the real ones)."""
import io
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_folders import FolderApiBase  # noqa: E402
from test_library import DECODED, FakeDecoder, dvf_bytes, wav_bytes  # noqa: E402
from app import backend, library_ops, sharing  # noqa: E402
from openevp import mp3  # noqa: E402


def id3_title(data):
    """The TIT2 text of an MP3's ID3v2.3 tag (latin-1), or None."""
    if data[:3] != b"ID3":
        return None
    at = data.find(b"TIT2")
    if at < 0:
        return None
    size = int.from_bytes(data[at + 4:at + 8], "big")
    return data[at + 11:at + 10 + size].decode("latin-1")


class ShareBase(FolderApiBase):
    def setUp(self):
        super().setUp()
        self.share = os.path.join(self.tmp, "share")
        root = mock.patch.object(sharing, "share_root", return_value=self.share)
        root.start()
        self.addCleanup(root.stop)
        self.dragged, self.copied = [], []
        self.drag_answer = "copy"

    def drag(self, paths):
        self.dragged.append(list(paths))
        if isinstance(self.drag_answer, Exception):
            raise self.drag_answer
        return self.drag_answer

    def copy(self, paths):
        self.copied.append(list(paths))

    def new_api(self, **kw):
        api = super().new_api(**kw)
        api._drag_files, api._copy_files = self.drag, self.copy
        return api

    def ids(self, api):
        """{name: id} once the indexer is done (it decodes nothing while a test counts decodes)."""
        return {f["name"]: f["id"] for f in self.index(api)["files"]}

    def norm(self, paths):
        return [os.path.normcase(os.path.abspath(p)) for p in paths]


class SharePathTests(ShareBase):
    def test_wav_mp3_and_clips_are_shared_as_themselves(self):
        wav = self.write("Old Mill/a.wav", wav_bytes(b"a"))
        song = self.write("b.mp3", mp3.encode(wav_bytes(b"b"), "b"))
        library_ops._make_clips_folder(os.path.join(self.lib, "Clips"))
        clip = self.write("Clips/a_EVP-A_00m01.0s.mp3", mp3.encode(wav_bytes(b"c"), "clip"))
        api = self.new_api()
        ids = self.ids(api)
        r = api.drag_out([ids["a.wav"], ids["b.mp3"], ids["a_EVP-A_00m01.0s.mp3"]])
        self.assertEqual(r, {"ok": True, "started": True, "count": 3, "effect": "copy"})
        self.assertEqual(self.norm(self.dragged[0]), self.norm([wav, song, clip]))
        self.assertFalse(os.path.exists(self.share), "nothing made")

    def test_an_mp3_under_another_extension_goes_as_name_mp3(self):
        """WhatsApp desktop crashed sending a .mpeg (it takes it for video): an MP3
        saved as .mpeg, .mpga, .mp2 or .m2a goes as <name>.mp3, a hard link in the
        share cache; the original is left as it is."""
        data = mp3.encode(wav_bytes(b"m"), "m")
        names = ["WhatsApp Audio 2026-09-30.mpeg", "b.mpga", "c.mp2", "d.m2a"]
        originals = [self.write(n, data) for n in names]
        before = [(os.path.getsize(p), os.stat(p).st_mtime_ns) for p in originals]
        api = self.new_api()
        ids = self.ids(api)
        self.assertTrue(api.drag_out([ids[n] for n in names])["ok"])
        shared = self.dragged[0]
        self.assertEqual([os.path.basename(p) for p in shared],
                         ["WhatsApp Audio 2026-09-30.mp3", "b.mp3", "c.mp3", "d.mp3"])
        for orig, link in zip(originals, shared):
            self.assertEqual(os.path.dirname(os.path.dirname(link)), self.share)
            self.assertTrue(os.path.samefile(orig, link), "a hard link on the same volume")
        self.assertEqual([(os.path.getsize(p), os.stat(p).st_mtime_ns) for p in originals], before)
        self.assertEqual(sorted(os.listdir(self.lib)), sorted(names), "nothing renamed or added")
        # Copy file: the same .mp3, reused (nothing linked again) while the original is unchanged.
        with mock.patch.object(sharing.os, "link", side_effect=AssertionError("not again")):
            self.assertEqual(api.copy_files([ids[names[0]]]), {"ok": True, "count": 1})
        self.assertEqual(self.copied, [[shared[0]]])
        # Changed since: a new one (the old one goes with the 6-hour cleanup).
        with open(originals[0], "ab") as f:
            f.write(data)
        api.drag_out([self.ids(api)[names[0]]])
        self.assertNotEqual(os.path.normcase(self.dragged[-1][0]), os.path.normcase(shared[0]))
        self.assertTrue(os.path.samefile(self.dragged[-1][0], originals[0]))

    def test_an_mp3_on_another_volume_is_copied(self):
        data = mp3.encode(wav_bytes(b"m"), "m")
        orig = self.write("x.mpeg", data)
        api = self.new_api()
        fid = self.ids(api)["x.mpeg"]
        with mock.patch.object(sharing.os, "link", side_effect=OSError(17, "not the same device")):
            self.assertTrue(api.drag_out([fid])["ok"])
        (copy,) = self.dragged[0]
        self.assertEqual(os.path.basename(copy), "x.mp3")
        self.assertFalse(os.path.samefile(orig, copy))
        with open(copy, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(os.listdir(os.path.dirname(copy)), ["x.mp3"], "no partial file left")
        with mock.patch.object(sharing.shutil, "copyfile", side_effect=OSError(28, "disk full")), \
                mock.patch.object(sharing.os, "link", side_effect=OSError(17, "not the same device")):
            with open(orig, "ab") as f:
                f.write(b"\0")
            r = api.drag_out([self.ids(api)["x.mpeg"]])
        self.assertTrue(r["error"].startswith("Could not prepare x.mpeg to share: "), r)

    def test_a_dvf_goes_as_the_wav_beside_it(self):
        self.write("Old Mill/x.dvf", dvf_bytes())
        wav = self.write("Old Mill/x.wav", wav_bytes(b"x"))
        api = self.new_api()
        ids = self.ids(api)
        with FakeDecoder().installed():
            self.assertTrue(api.drag_out([ids["x.dvf"]])["ok"])
            self.assertTrue(api.drag_out([ids["x.dvf"], ids["x.wav"]])["ok"])   # both copies: once
        self.assertEqual([self.norm(d) for d in self.dragged], [self.norm([wav])] * 2)
        self.assertFalse(os.path.exists(self.share))

    def test_a_dvf_without_a_wav_goes_as_an_mp3_made_for_it(self):
        dvf = self.write("Old Mill/Night one.dvf", dvf_bytes())
        api = self.new_api()
        ids = self.ids(api)
        fake = FakeDecoder()
        with fake.installed():
            r = api.drag_out([ids["Night one.dvf"]])
        self.assertEqual(r, {"ok": True, "started": True, "count": 1, "effect": "copy"})
        (made,) = self.dragged[0]
        self.assertEqual(os.path.basename(made), "Night one.mp3")
        st = os.stat(dvf)
        self.assertEqual(os.path.normcase(made), os.path.normcase(sharing.made_path(self.share, dvf, st)))
        self.assertEqual(os.path.dirname(os.path.dirname(made)), self.share)
        with open(made, "rb") as f:
            data = f.read()
        self.assertEqual(id3_title(data), "Night one")
        self.assertEqual(data, mp3.encode(DECODED, "Night one"))      # the whole recording, as clips are encoded
        self.assertEqual(fake.calls, 1)
        self.assertIn(("share-preparing", {"name": "Night one.dvf"}), self.events.items)
        self.assertEqual(os.listdir(os.path.dirname(made)), ["Night one.mp3"], "no partial file left")
        # The next drag (or Copy file) reuses it; the original is untouched.
        with fake.installed():
            r = api.copy_files([ids["Night one.dvf"]])
        self.assertEqual(r, {"ok": True, "count": 1})
        self.assertEqual(self.copied, [[made]])
        self.assertEqual(fake.calls, 1)
        with open(dvf, "rb") as f:
            self.assertEqual(f.read(), dvf_bytes())

    def test_the_players_decode_is_reused(self):
        self.write("x.dvf", dvf_bytes())
        api = self.new_api()
        ids = self.ids(api)
        cached = wav_bytes(b"from the player", seconds=0.5)
        keys = []

        def open_cached(key):
            keys.append(key)
            return io.BytesIO(cached)
        self.server.open_cached = open_cached
        fake = FakeDecoder()
        with fake.installed():
            self.assertTrue(api.drag_out([ids["x.dvf"]])["ok"])
        self.assertEqual(fake.calls, 0)
        self.assertEqual(keys[0][0], "dvf")
        with open(self.dragged[0][0], "rb") as f:
            self.assertEqual(f.read(), mp3.encode(cached, "x"))

    def test_two_recordings_with_one_name_never_share_an_mp3(self):
        self.write("A/x.dvf", dvf_bytes(100))
        self.write("B/x.dvf", dvf_bytes(200))
        api = self.new_api()
        ids = [f["id"] for f in self.index(api)["files"]]
        with FakeDecoder().installed():
            self.assertTrue(api.drag_out(ids)["ok"])
        a, b = self.dragged[0]
        self.assertEqual((os.path.basename(a), os.path.basename(b)), ("x.mp3", "x.mp3"))
        self.assertNotEqual(os.path.dirname(a), os.path.dirname(b))

    def test_several_files_in_the_order_given_each_once(self):
        a = self.write("a.wav", wav_bytes(b"a"))
        b = self.write("b.wav", wav_bytes(b"b"))
        c = self.write("c.wav", wav_bytes(b"c"))
        api = self.new_api()
        ids = self.ids(api)
        api.drag_out([ids["c.wav"], ids["a.wav"], ids["c.wav"], ids["b.wav"]])
        self.assertEqual(self.norm(self.dragged[0]), self.norm([c, a, b]))

    def test_unknown_ids_and_paths_are_refused(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        self.assertEqual(api.drag_out(["x"])["error"], backend.LIB_CHANGED)          # not listed yet
        fid = self.ids(api)["a.wav"]
        for bad in ("nope", None, 7, ["a"], {"path": "C:\\Windows\\notepad.exe"}, "C:\\Windows\\notepad.exe"):
            self.assertEqual(api.drag_out([bad])["error"], backend.LIB_CHANGED, bad)
            self.assertEqual(api.copy_files([fid, bad])["error"], backend.LIB_CHANGED, bad)   # all or nothing
        for bad in (fid, None, 7, {"ids": [fid]}):                                          # not a list of ids
            self.assertEqual(api.drag_out(bad)["error"], backend.LIB_CHANGED, bad)
        self.assertEqual(api.drag_out([])["error"], backend.LIB_CHANGED)
        self.assertEqual(api.drag_out([fid] * (sharing.SHARE_LIMIT + 1))["error"],
                         f"Share at most {sharing.SHARE_LIMIT} files at once.")
        self.assertEqual((self.dragged, self.copied), ([], []))

    def test_a_path_outside_the_library_or_another_library_is_refused(self):
        self.write("a.wav", wav_bytes(b"a"))
        outside = os.path.join(self.tmp, "outside.wav")
        with open(outside, "wb") as f:
            f.write(wav_bytes(b"o"))
        api = self.new_api()
        fid = self.ids(api)["a.wav"]
        api._library[fid] = outside
        self.assertEqual(api.drag_out([fid])["error"], backend.LIB_CHANGED)
        api._library[fid] = os.path.join(self.lib, "..", "outside.wav")
        self.assertEqual(api.copy_files([fid])["error"], backend.LIB_CHANGED)
        fid = self.ids(api)["a.wav"]
        with mock.patch.object(api, "_root_moved", return_value=True):
            self.assertEqual(api.drag_out([fid])["error"], backend.ROOT_CHANGED)
        other = os.path.join(self.tmp, "Other")
        os.makedirs(other)
        api._lib_folder = other
        self.assertEqual(api.drag_out([fid])["error"], backend.LIB_CHANGED)
        self.assertEqual((self.dragged, self.copied), ([], []))

    def test_a_missing_file_gets_a_plain_message(self):
        path = self.write("gone.wav", wav_bytes(b"g"))
        api = self.new_api()
        fid = self.ids(api)["gone.wav"]
        os.remove(path)
        self.assertEqual(api.drag_out([fid])["error"], "gone.wav is no longer there. Refresh the list.")

    def test_without_mp3_or_a_decoder_a_dvf_cannot_be_shared(self):
        self.write("x.dvf", dvf_bytes())
        api = self.new_api()
        fid = self.ids(api)["x.dvf"]
        with mock.patch.object(mp3, "available", return_value=False):
            self.assertEqual(api.drag_out([fid])["error"], f"x.dvf can't be shared: {mp3.UNAVAILABLE}")
        with mock.patch.object(sharing, "_decoder_problem", return_value="no decoder here"):
            self.assertEqual(api.copy_files([fid])["error"], "x.dvf can't be shared: no decoder here.")
        bad = FakeDecoder()
        bad.bad.add(dvf_bytes())
        with bad.installed():
            r = api.drag_out([fid])
        self.assertTrue(r["error"].startswith("Could not prepare x.dvf to share: "), r)
        self.assertEqual(self.dragged, [])
        left = [f for _d, _s, files in os.walk(self.share) for f in files]
        self.assertEqual(left, [], "nothing half-made is left")


class DragTests(ShareBase):
    def test_the_button_let_go_while_preparing(self):
        self.write("x.dvf", dvf_bytes())
        api = self.new_api()
        fid = self.ids(api)["x.dvf"]
        self.drag_answer = None
        with FakeDecoder().installed():
            self.assertEqual(api.drag_out([fid]), {"ok": True, "started": False, "count": 1, "made": 1})
            self.assertEqual(api.drag_out([fid]), {"ok": True, "started": False, "count": 1, "made": 0})
        self.drag_answer = "none"
        self.assertEqual(api.drag_out([fid])["effect"], "none")

    def test_a_failing_drag_or_copy_is_reported(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        fid = self.ids(api)["a.wav"]
        self.drag_answer = OSError("no drag today")
        self.assertEqual(api.drag_out([fid])["error"], "The file could not be dragged: no drag today")
        api._copy_files = mock.Mock(side_effect=RuntimeError("clipboard busy"))
        self.assertEqual(api.copy_files([fid])["error"], "The file could not be copied: clipboard busy")

    def test_not_available_without_a_window(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = super(ShareBase, self).new_api()                       # no drag or clipboard (e.g. --smoke)
        fid = self.ids(api)["a.wav"]
        self.assertEqual(api.drag_out([fid])["error"], sharing.NOT_HERE)
        self.assertEqual(api.copy_files([fid])["error"], sharing.COPY_NOT_HERE)

    def test_one_drag_at_a_time(self):
        self.write("a.wav", wav_bytes(b"a"))
        api = self.new_api()
        fid = self.ids(api)["a.wav"]
        started, release = threading.Event(), threading.Event()

        def slow(paths):
            started.set()
            release.wait(10)
            return "copy"
        api._drag_files = slow
        result = {}
        t = threading.Thread(target=lambda: result.update(api.drag_out([fid])))
        t.start()
        self.assertTrue(started.wait(10))
        self.assertEqual(api.drag_out([fid])["error"], sharing.SHARE_BUSY)
        self.assertEqual(api.copy_files([fid])["error"], sharing.SHARE_BUSY)
        release.set()
        t.join(10)
        self.assertTrue(result["started"])
        self.assertEqual(api.copy_files([fid]), {"ok": True, "count": 1})


class CleanTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def entry(self, name, age, folder=True):
        path = os.path.join(self.root, name)
        if folder:
            os.makedirs(path)
            with open(os.path.join(path, "x.mp3"), "wb") as f:
                f.write(b"mp3")
        else:
            with open(path, "wb") as f:
                f.write(b"part")
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    def test_only_what_is_old_enough_goes(self):
        old = self.entry("old", sharing.SHARE_MAX_AGE + 60)
        young = self.entry("young", sharing.SHARE_MAX_AGE - 600)
        stray = self.entry("stray.part", sharing.SHARE_MAX_AGE + 60, folder=False)
        self.assertEqual(sharing.clean(self.root), 2)
        self.assertEqual((os.path.exists(old), os.path.exists(young), os.path.exists(stray)), (False, True, False))
        self.assertEqual(sharing.clean(os.path.join(self.root, "missing")), 0)

    def test_a_shared_mp3_is_kept_while_in_use(self):
        old = self.entry("old", sharing.SHARE_MAX_AGE + 60)
        with mock.patch.object(sharing.shutil, "rmtree", side_effect=PermissionError("in use")):
            self.assertEqual(sharing.clean(self.root), 0)            # never raises; tried again next time
        self.assertTrue(os.path.exists(old))

    def test_sharing_again_keeps_it_fresh(self):
        old = self.entry("old", sharing.SHARE_MAX_AGE - 1)
        sharing._touch(old)
        self.assertEqual(sharing.clean(self.root, now=time.time() + 2), 0)
        self.assertTrue(os.path.exists(old))

    def test_made_path(self):
        st = os.stat(self.root)
        a = sharing.made_path("R", os.path.join(self.root, "Night 1.dvf"), st)
        b = sharing.made_path("R", os.path.join(self.root, "other", "Night 1.dvf"), st)
        self.assertEqual(os.path.basename(a), "Night 1.mp3")
        self.assertEqual(os.path.dirname(os.path.dirname(a)), "R")
        self.assertNotEqual(a, b)
        self.assertRegex(os.path.basename(os.path.dirname(a)), "^[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
