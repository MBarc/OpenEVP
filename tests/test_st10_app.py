"""The app with a Sony ICD-ST10: discovered as an ICD-ST25 (same USB id), shown
as an ST10 once opened, its recordings exported as LPEC ST .dvf files, played,
marked and exported as 44.1 kHz stereo WAV through the LPEC ST decoder.

In a build without the LPEC ST decoder (or its tables) the phase-1 mechanism
still holds: "can't be played" is said plainly everywhere (recorder list,
export menu, player, library) with the reason, never as a damaged file, and
nothing about such a file is cached."""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from fixtures import DATE, make_raw, st_audio_wav  # noqa: E402
from test_app_recorders import AppTestBase  # noqa: E402
from test_library import LibraryTests  # noqa: E402
from test_st10 import NO_ST_TABLES, _st_tables  # noqa: E402
from test_st10_model import FRAMES, ST_MISSING, st10_folders, st_estimate, without_st_decoder  # noqa: E402
from app import backend  # noqa: E402
from fixtures import FakeRecorderDevice  # noqa: E402
from openevp import formats, wavinfo  # noqa: E402
from openevp.recorders.sony_st25 import ST25Session  # noqa: E402
from st25 import dvf  # noqa: E402
from st25.protocol import Recorder  # noqa: E402
from st25.session import RecorderSession  # noqa: E402

ID = "1-4@7"
UNDATED = b"\xff" * 8
HAVE_ST = _st_tables()
WAV_1 = st_audio_wav()                  # Sony's decode of A-001 (FRAMES[0])
SECONDS_1 = round(37 * 2048 / 44100, 1)  # its exact length: 40 frames, 1 swallowed, 1 counter-0, 1 swallowed


def st10_dvf(frames=FRAMES[0]):
    from fixtures import make_st_raw
    return dvf.build(make_st_raw(frames), UNDATED, "", mode=dvf.MODE_ST)


def fp_of(wav):
    return wavinfo.wav_fingerprint(io.BytesIO(wav))


class St10Base(AppTestBase):
    identify = "ICD-ST10"
    without_decoder = False

    def setUp(self):
        if self.without_decoder:
            patch = without_st_decoder()
            patch.start()
            self.addCleanup(patch.stop)
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


@release_gate.require(HAVE_ST, NO_ST_TABLES)
class St10AppTests(St10Base):
    """With the LPEC ST decoder: playable, exported as WAV, marked and backed up."""

    def test_listed_as_an_st10_and_playable(self):
        self.assertEqual(self.first_rows[ID]["model"], "Sony ICD-ST25")      # before it was opened
        r = self.api.recordings(ID)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["model"], r["model_id"]), ("Sony ICD-ST10", "sony-icd-st10"))
        self.assertEqual((r["playable"], r["play_reason"]), (True, None))
        self.assertEqual(r["formats"], [
            {"value": "dvf", "label": ".dvf (Sony original)", "available": True, "reason": None},
            {"value": "wav", "label": "WAV", "available": True, "reason": None}])
        rows = r["folders"][0]["recordings"]
        self.assertEqual([(x["label"], x["recorded"], x["owner"], x["problem"]) for x in rows],
                         [("A-001", "undated", "", None), ("A-002", "undated", "", None)])
        self.assertEqual(rows[0]["seconds"], st_estimate(FRAMES[0]))
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

    def test_wav_export_is_sonys_decode_then_already_saved(self):
        self.api.recordings(ID)
        items = [{"folder": "A", "number": 1}, {"folder": "A", "number": 2}]
        name, p = self.export(ID, items, "wav")
        self.assertEqual((name, p["saved"], p["skipped"], p["notes"]), ("export-done", 2, 0, []))
        self.assertEqual(self.files("A"), ["001_A_001_Unknown.wav", "001_A_002_Unknown.wav"])
        self.assertEqual(self.read("A", "001_A_001_Unknown.wav"), WAV_1)
        name, p = self.export(ID, items, "wav")
        self.assertEqual((p["saved"], p["skipped"]), (0, 2))

    def test_play_mark_backup_and_export_marked(self):
        self.api.recordings(ID)
        r = self.api.audio(ID, "A", 1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.server.made, [(ID, "A", 1)])
        self.assertEqual(r["fp"], fp_of(WAV_1))
        self.assertTrue(self.api.add_mark(r["rec"], 0.5, 0.6, "A", "st10")["backup_queued"])
        event, p = self.events.wait("backup-done", "backup-failed")
        self.assertEqual(event, "backup-done", p)
        self.assertEqual(self.files("A"), ["001_A_001_Unknown.dvf", "001_A_001_Unknown.wav"])
        marked = self.read("A", "001_A_001_Unknown.wav")
        self.assertEqual([m["note"] for m in wavinfo.read_markers(io.BytesIO(marked))], ["EVP A: st10"])
        self.assertEqual(fp_of(marked), r["fp"])                          # the marks re-key nothing
        e = self.api.export_marked(r["rec"])
        self.assertEqual((e["ok"], e["already"]), (True, True))

    def test_a_backup_without_captured_bytes_downloads_again_and_checks(self):
        self.api.recordings(ID)
        self.api.audio(ID, "A", 1)
        self.api._natives.clear()
        rec = self.api.audio(ID, "A", 1)["rec"]        # the server's cached decode: no captured bytes
        self.api.add_mark(rec, 0.1, 0.2, "B", "")
        event, p = self.events.wait("backup-done", "backup-failed")
        self.assertEqual(event, "backup-done", p)
        self.assertEqual(fp_of(self.read("A", "001_A_001_Unknown.wav")), fp_of(WAV_1))

    def test_playback_streams_into_the_audio_servers_cache(self):
        """The real AudioServer: the recording is decoded straight into its cache
        file (never as a whole WAV in memory: to_wav is not called)."""
        import tempfile
        from unittest import mock
        from app.audio_server import AudioServer
        with tempfile.TemporaryDirectory() as cache:
            server = AudioServer(None, cache)
            server.start()
            self.addCleanup(server.stop)
            self.api._server = server
            self.api.recordings(ID)
            with mock.patch.object(type(formats.DVF.decoder), "to_wav", side_effect=AssertionError("to_wav")):
                r = self.api.audio(ID, "A", 1)
            self.assertTrue(r["ok"], r)
            self.assertEqual((r["fp"], r["rate"], r["channels"]), (fp_of(WAV_1), 44100, 2))
            [name] = os.listdir(cache)
            with open(os.path.join(cache, name), "rb") as f:
                self.assertEqual(f.read(), WAV_1)
            server.stop()

    def test_a_redownload_is_decoded_once(self):
        """No captured bytes: the check decode is the WAV copy's decode too."""
        from unittest import mock
        self.api.recordings(ID)
        self.api.audio(ID, "A", 1)
        self.api._natives.clear()
        rec = self.api.audio(ID, "A", 1)["rec"]
        real = type(formats.DVF.decoder).to_wav
        with mock.patch.object(type(formats.DVF.decoder), "to_wav", autospec=True, side_effect=real) as decode:
            self.api.add_mark(rec, 0.1, 0.2, "B", "")
            event, p = self.events.wait("backup-done", "backup-failed")
        self.assertEqual(event, "backup-done", p)
        self.assertEqual(decode.call_count, 1)

    def test_recording_wav(self):
        self.api.recordings(ID)
        self.assertEqual(bytes(backend.recording_wav(self.m, (ID, "A", 1))), WAV_1)


class St10WithoutDecoderTests(St10Base):
    """A build without the LPEC ST decoder: the ST10 is listed and its
    recordings saved, and "can't be played" is said with the reason."""
    without_decoder = True

    def test_listed_as_an_st10_and_not_playable(self):
        r = self.api.recordings(ID)
        self.assertEqual((r["model_id"], r["playable"], r["play_reason"]), ("sony-icd-st10", False, ST_MISSING))
        self.assertEqual(r["formats"][1], {"value": "wav", "label": "WAV", "available": False, "reason": ST_MISSING})

    def test_dvf_export_still_works(self):
        self.api.recordings(ID)
        name, p = self.export(ID, [{"folder": "A", "number": 1}], "dvf")
        self.assertEqual((name, p["saved"]), ("export-done", 1))

    def test_wav_export_refused_plainly(self):
        self.api.recordings(ID)
        r = self.api.export(ID, [{"folder": "A", "number": 1}], "wav", self.dest, 1)
        self.assertEqual((r["ok"], r["error"]), (False, f"WAV export: {ST_MISSING}."))

    def test_play_refused_without_a_decode(self):
        self.api.recordings(ID)
        r = self.api.audio(ID, "A", 1)
        self.assertEqual((r["ok"], r["error"]), (False, f"Playback: {ST_MISSING}."))
        self.assertEqual(self.server.made, [])

    def test_recording_wav_is_decoder_unavailable(self):
        self.api.recordings(ID)
        with self.assertRaises(formats.DecoderUnavailable):
            backend.recording_wav(self.m, (ID, "A", 1))


class UnknownIdentityTests(St10Base):
    """An ST10 table on a recorder that does not say it is an ST10: shown as an
    ST25 as before; its LPEC ST recordings are still decoded by their codec
    byte (never as LP)."""
    identify = "ICD-ST99"

    def test_listed_as_an_st25_and_playable(self):
        r = self.api.recordings(ID)
        self.assertEqual((r["model_id"], r["playable"]), ("sony-icd-st25", True))

    @release_gate.require(HAVE_ST, NO_ST_TABLES)
    def test_plays_and_exports_by_the_codec(self):
        self.api.recordings(ID)
        r = self.api.audio(ID, "A", 1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["fp"], fp_of(WAV_1))
        name, p = self.export(ID, [{"folder": "A", "number": 1}], "wav")
        self.assertEqual((name, p["saved"], p["notes"]), ("export-done", 1, []))

    def test_without_the_decoder_play_and_wav_export_say_why(self):
        with without_st_decoder():
            self.api.recordings(ID)
            r = self.api.audio(ID, "A", 1)
            self.assertEqual((r["ok"], r["error"]), (False, ST_MISSING + "."))     # a sentence, no class name
            name, p = self.export(ID, [{"folder": "A", "number": 1}], "wav")
            self.assertEqual((name, p["saved"]), ("export-done", 0))
            self.assertEqual(p["notes"], [f"A-001: not converted ({ST_MISSING})"])
            with self.assertRaises(formats.DecoderUnavailable):
                backend.recording_wav(self.m, (ID, "A", 1))


class ErrorTests(unittest.TestCase):
    def test_decoder_unavailable_is_a_plain_sentence(self):
        r = backend._error(formats.DecoderUnavailable(ST_MISSING))
        self.assertEqual((r["ok"], r["error"], r["advice"]), (False, ST_MISSING + ".", ""))


class St10LibraryTests(unittest.TestCase):
    """An ICD-ST10 .dvf in the library: indexed (fingerprint, exact length) and
    played like any recording; without the decoder, listed with its length and
    the reason, never read whole, fingerprinted or cached."""
    setUp = LibraryTests.setUp
    new_api, write, index, by_name = LibraryTests.new_api, LibraryTests.write, LibraryTests.index, LibraryTests.by_name

    @release_gate.require(HAVE_ST, NO_ST_TABLES)
    def test_indexed_and_played(self):
        path = self.write("Old Mill/A/001_A_001_Unknown.dvf", st10_dvf())
        api = self.new_api()
        r = self.index(api)
        f = self.by_name(r)["001_A_001_Unknown.dvf"]
        row = self.events.rows(r["scan_id"])[f["id"]]              # what the indexer found
        self.assertEqual((row["fp"], row["unplayable"], row["error"]), (fp_of(WAV_1), None, None))
        self.assertEqual(row["seconds"], SECONDS_1)                 # the decoded length, not the estimate
        self.assertEqual(self.store.cached_fp(path, *self.stat(path))["fp"], fp_of(WAV_1))
        f = self.by_name(self.index(api))["001_A_001_Unknown.dvf"]  # listed again: from the index
        self.assertEqual((f["fp"], f["seconds"]), (fp_of(WAV_1), SECONDS_1))
        r = api.play_library(f["id"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["fp"], fp_of(WAV_1))

    @release_gate.require(HAVE_ST, NO_ST_TABLES)
    def test_indexing_fingerprints_without_a_wav_in_memory(self):
        from unittest import mock
        self.write("x.dvf", st10_dvf())
        with mock.patch.object(type(formats.DVF.decoder), "to_wav", side_effect=AssertionError("to_wav")):
            r = self.index(self.new_api())
        row = self.events.rows(r["scan_id"])[self.by_name(r)["x.dvf"]["id"]]
        self.assertEqual((row["fp"], row["seconds"], row["error"]), (fp_of(WAV_1), SECONDS_1, None))

    def test_without_the_decoder_listed_as_not_playable_and_never_cached(self):
        path = self.write("Old Mill/A/001_A_001_Unknown.dvf", st10_dvf())
        self.write("Old Mill/A/lp.dvf", dvf.build(make_raw(3010, 100), DATE, "X", expected_length=3010))
        with without_st_decoder():
            api = self.new_api()
            for _ in range(2):
                r = self.index(api)
                f = self.by_name(r)["001_A_001_Unknown.dvf"]
                self.assertEqual((f["fp"], f["unplayable"]), (None, ST_MISSING))
                self.assertEqual(f["error"], f"001_A_001_Unknown.dvf can't be played or marked: {ST_MISSING}.")
                self.assertEqual(f["seconds"], st_estimate(FRAMES[0]))
                self.assertIsNone(self.by_name(r)["lp.dvf"]["unplayable"])
                self.assertIsNone(self.store.cached_fp(path, *self.stat(path)))
            self.assertNotIn(f["id"], self.events.rows())                  # never sent to the indexer
            r = api.play_library(f["id"])
            self.assertEqual((r["ok"], r["error"]), (False, f"001_A_001_Unknown.dvf: {ST_MISSING}."))
            self.assertEqual(self.server.made, [])

    def test_headers_are_read_once_per_version_of_a_file(self):
        from unittest import mock
        path = self.write("x.dvf", st10_dvf())
        real = formats.Format.file_problem
        for store in ("default", None):
            with self.subTest(store=store), without_st_decoder(), \
                    mock.patch.object(formats.Format, "file_problem", autospec=True, side_effect=real) as spy:
                api = self.new_api(store=store)
                for _ in range(3):
                    f = self.by_name(self.index(api))["x.dvf"]
                    self.assertEqual(f["unplayable"], ST_MISSING)
                self.assertEqual(spy.call_count, 1)
                with open(path, "ab") as fh:                    # a new version of the file: read again
                    fh.write(b"\xff" * 1024)
                self.index(api)
                self.assertEqual(spy.call_count, 2)

    def test_the_indexer_checks_the_header_first(self):
        path = self.write("x.dvf", st10_dvf())
        with without_st_decoder():
            api = self.new_api()
            size, mtime = self.stat(path)
            row, stored = api._index_file(path, "dvf", size, mtime, lambda: False, None)
            self.assertEqual((row["fp"], row["unplayable"], stored), (None, ST_MISSING, False))
            self.assertIsNone(self.store.cached_fp(path, size, mtime))

    @staticmethod
    def stat(path):
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns


# Only the tests defined here: the imported bases' own tests run in their modules.
del LibraryTests

if __name__ == "__main__":
    unittest.main()
