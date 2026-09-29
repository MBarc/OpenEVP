"""The app with a Sony ICD-ST10: discovered as an ICD-ST25 (same USB id), shown
as an ST10 once opened, its recordings exported as LPEC ST .dvf files, and
"can't be played yet" said plainly everywhere (recorder list, export menu,
player, library), never as a damaged file."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fixtures import DATE, make_raw  # noqa: E402
from test_app_recorders import AppTestBase  # noqa: E402
from test_library import LibraryTests  # noqa: E402
from test_st10_model import FRAMES, st10_folders  # noqa: E402
from app import backend  # noqa: E402
from fixtures import FakeRecorderDevice  # noqa: E402
from openevp import formats  # noqa: E402
from openevp.recorders.sony_st25 import ST25Session  # noqa: E402
from st25 import dvf  # noqa: E402
from st25.protocol import Recorder  # noqa: E402
from st25.session import RecorderSession  # noqa: E402

ID = "1-4@7"
NOT_YET = "LPEC ST (ICD-ST10) audio can't be played yet"
UNDATED = b"\xff" * 8


def st10_dvf(frames=FRAMES[0]):
    from fixtures import make_st_raw
    return dvf.build(make_st_raw(frames), UNDATED, "", mode=dvf.MODE_ST)


class St10AppTests(AppTestBase):
    identify = "ICD-ST10"

    def setUp(self):
        super().setUp()
        self.st25.append(ID)            # discovered by the ST25 model, as the real one is
        self.first_rows = self.rows()

    def open_device(self, model, device):
        self.assertEqual(model.model_id, "sony-icd-st25")          # the discovering model opens it
        folders, voice = st10_folders()
        r = Recorder.__new__(Recorder)
        r.dev = FakeRecorderDevice(folders, voice=voice, identify=self.identify)
        s = RecorderSession(r)
        s.connect()
        return ST25Session(s)

    def test_listed_as_an_st10_and_not_playable(self):
        self.assertEqual(self.first_rows[ID]["model"], "Sony ICD-ST25")      # before it was opened
        r = self.api.recordings(ID)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["model"], r["model_id"]), ("Sony ICD-ST10", "sony-icd-st10"))
        self.assertEqual((r["playable"], r["play_reason"]), (False, NOT_YET))
        self.assertEqual(r["formats"], [
            {"value": "dvf", "label": ".dvf (Sony original)", "available": True, "reason": None},
            {"value": "wav", "label": "WAV", "available": False, "reason": NOT_YET}])
        rows = r["folders"][0]["recordings"]
        self.assertEqual([(x["label"], x["recorded"], x["owner"], x["problem"]) for x in rows],
                         [("A-001", "undated", "", None), ("A-002", "undated", "", None)])
        self.assertEqual(rows[0]["seconds"], round(len(FRAMES[0]) / 283 * 2048 / 44100, 1))
        self.assertEqual(self.rows()[ID]["model"], "Sony ICD-ST10")

    def test_export_dvf_then_again(self):
        self.api.recordings(ID)
        items = [{"folder": "A", "number": 1}, {"folder": "A", "number": 2}]
        name, p = self.export(ID, items, "dvf")
        self.assertEqual((name, p["saved"], p["skipped"], p["notes"]), ("export-done", 2, 0, []))
        self.assertEqual(self.files("A"), ["001_A_001_Unknown.dvf", "001_A_002_Unknown.dvf"])
        for n, frames in enumerate(FRAMES, 1):
            data = self.read("A", f"001_A_{n:03d}_Unknown.dvf")
            self.assertIsNone(dvf.validate(data))
            self.assertEqual(dvf.payload(data), frames)
        name, p = self.export(ID, items, "dvf")
        self.assertEqual((p["saved"], p["skipped"]), (0, 2))              # same audio: already saved
        self.assertEqual(len(self.files("A")), 2)

    def test_wav_export_refused_plainly(self):
        self.api.recordings(ID)
        r = self.api.export(ID, [{"folder": "A", "number": 1}], "wav", self.dest, 1)
        self.assertEqual((r["ok"], r["error"]), (False, f"WAV export: {NOT_YET}."))

    def test_play_refused_without_a_decode(self):
        self.api.recordings(ID)
        r = self.api.audio(ID, "A", 1)
        self.assertEqual((r["ok"], r["error"]), (False, f"Playback: {NOT_YET}."))
        self.assertEqual(self.server.made, [])

    def test_recording_wav_is_decoder_unavailable(self):
        self.api.recordings(ID)
        with self.assertRaises(formats.DecoderUnavailable):
            backend.recording_wav(self.m, (ID, "A", 1))


class UnknownIdentityTests(St10AppTests):
    """An ST10 table on a recorder that does not say it is an ST10: shown as an
    ST25 as before, and still never decoded as LP."""
    identify = "ICD-ST99"

    def test_listed_as_an_st10_and_not_playable(self):
        r = self.api.recordings(ID)
        self.assertEqual((r["model_id"], r["playable"]), ("sony-icd-st25", True))

    def test_wav_export_refused_plainly(self):
        self.api.recordings(ID)
        name, p = self.export(ID, [{"folder": "A", "number": 1}], "wav")
        self.assertEqual((name, p["saved"]), ("export-done", 0))
        self.assertEqual(p["notes"], [f"A-001: not converted ({NOT_YET})"])

    def test_play_refused_without_a_decode(self):
        self.api.recordings(ID)
        r = self.api.audio(ID, "A", 1)
        self.assertEqual((r["ok"], r["error"]), (False, NOT_YET + "."))     # a sentence, no class name

    def test_recording_wav_is_decoder_unavailable(self):
        self.api.recordings(ID)
        with self.assertRaises(formats.DecoderUnavailable):
            backend.recording_wav(self.m, (ID, "A", 1))


class ErrorTests(unittest.TestCase):
    def test_decoder_unavailable_is_a_plain_sentence(self):
        r = backend._error(formats.DecoderUnavailable(NOT_YET))
        self.assertEqual((r["ok"], r["error"], r["advice"]), (False, NOT_YET + ".", ""))


class St10LibraryTests(unittest.TestCase):
    """An ICD-ST10 .dvf in the library: listed with its length and a "can't be
    played yet" reason, never read whole, fingerprinted or cached."""
    setUp = LibraryTests.setUp
    new_api, write, index, by_name = LibraryTests.new_api, LibraryTests.write, LibraryTests.index, LibraryTests.by_name

    def test_listed_as_not_playable_yet_and_never_cached(self):
        path = self.write("Old Mill/A/001_A_001_Unknown.dvf", st10_dvf())
        self.write("Old Mill/A/lp.dvf", dvf.build(make_raw(3010, 100), DATE, "X", expected_length=3010))
        api = self.new_api()
        for _ in range(2):
            r = self.index(api)
            f = self.by_name(r)["001_A_001_Unknown.dvf"]
            self.assertEqual((f["fp"], f["unplayable"]), (None, NOT_YET))
            self.assertEqual(f["error"], f"001_A_001_Unknown.dvf can't be played or marked: {NOT_YET}.")
            self.assertEqual(f["seconds"], round(len(FRAMES[0]) / 283 * 2048 / 44100, 1))
            self.assertIsNone(self.by_name(r)["lp.dvf"]["unplayable"])
            self.assertIsNone(self.store.cached_fp(path, *self.stat(path)))
        self.assertNotIn(f["id"], self.events.rows())                  # never sent to the indexer
        r = api.play_library(f["id"])
        self.assertEqual((r["ok"], r["error"]), (False, f"001_A_001_Unknown.dvf: {NOT_YET}."))
        self.assertEqual(self.server.made, [])

    def test_headers_are_read_once_per_version_of_a_file(self):
        from unittest import mock
        path = self.write("x.dvf", st10_dvf())
        real = formats.Format.file_problem
        for store in ("default", None):
            with self.subTest(store=store), \
                    mock.patch.object(formats.Format, "file_problem", autospec=True, side_effect=real) as spy:
                api = self.new_api(store=store)
                for _ in range(3):
                    f = self.by_name(self.index(api))["x.dvf"]
                    self.assertEqual(f["unplayable"], NOT_YET)
                self.assertEqual(spy.call_count, 1)
                with open(path, "ab") as fh:                    # a new version of the file: read again
                    fh.write(b"\xff" * 1024)
                self.index(api)
                self.assertEqual(spy.call_count, 2)

    def test_the_indexer_checks_the_header_first(self):
        path = self.write("x.dvf", st10_dvf())
        api = self.new_api()
        size, mtime = self.stat(path)
        row, stored = api._index_file(path, "dvf", size, mtime, lambda: False, None)
        self.assertEqual((row["fp"], row["unplayable"], stored), (None, NOT_YET, False))
        self.assertIsNone(self.store.cached_fp(path, size, mtime))

    @staticmethod
    def stat(path):
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns


# Only the tests defined here: the imported bases' own tests run in their modules.
del LibraryTests

if __name__ == "__main__":
    unittest.main()
