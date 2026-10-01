"""EVP clips: cutting one WAV per mark (openevp.clips), the player's Export clips /
Save clip (Api.export_clips) and the library's background clips job
(Api.export_clips_files / export_clips_folder)."""
import io
import math
import os
import struct
import sys
import threading
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import test_folders as tf  # noqa: E402
from app import library_ops  # noqa: E402
import test_library as tl  # noqa: E402
import test_marks_api as tm  # noqa: E402
from app import backend  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import clips, wavinfo  # noqa: E402

WAIT = 30


_WAV_CLIPS = mock.patch.object(backend.Api, "clip_format", lambda self: "wav")


def setUpModule():
    """These tests are about WAV clips (MP3 is the default format: see test_mp3_clips)."""
    _WAV_CLIPS.start()


def tearDownModule():
    """A module cleanup (unittest.addModuleCleanup) is not run by pytest: undo it here."""
    _WAV_CLIPS.stop()


def pcm_wav(rate, channels, seconds, width=2):
    """A WAV whose every frame holds its own index (so a cut can be checked frame by frame)."""
    n = int(rate * seconds)
    frames = b"".join(struct.pack("<i", i)[:width] * channels for i in range(n))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)
    return out.getvalue(), frames


def params(wav):
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes(), w.readframes(w.getnframes())


def mark(start, end, cls="A", note=""):
    return {"start": start, "end": end, "cls": cls, "note": note}


class CutTests(unittest.TestCase):
    def test_bounds_pad_and_clamp(self):
        self.assertEqual(clips.bounds(3.0, 4.0, 10.0), (2.5, 4.5))
        self.assertEqual(clips.bounds(0.2, 0.4, 10.0), (0.0, 0.9))           # clamped at the start
        self.assertEqual(clips.bounds(9.8, 9.9, 10.0), (9.3, 10.0))          # and at the end
        self.assertEqual(clips.bounds(0.1, 9.95, 10.0), (0.0, 10.0))
        self.assertEqual(clips.bounds(5.0, 5.0, 10.0), (4.5, 5.5))           # a point mark

    def test_8k_mono_keeps_its_format_and_samples(self):
        wav, frames = pcm_wav(8000, 1, 3.0)
        clip = clips.cut(wav, mark(1.0, 1.25, "B", "hello"))
        ch, width, rate, n, data = params(clip)
        self.assertEqual((ch, width, rate), (1, 2, 8000))
        self.assertEqual(n, int(8000 * 1.25))                                  # 0.5 s + 0.25 s + 0.5 s
        self.assertEqual(data, frames[2 * 4000:2 * 14000])                     # exactly those frames, untouched
        (m,) = wavinfo.read_markers(io.BytesIO(clip))
        self.assertAlmostEqual(m["start"], 0.5, places=4)                      # the EVP sits 0.5 s in
        self.assertAlmostEqual(m["end"], 0.75, places=4)
        self.assertEqual(m["note"], "EVP B: hello")                            # the label an editor shows

    def test_44k_stereo_keeps_its_format_and_samples(self):
        wav, frames = pcm_wav(44100, 2, 2.0)
        clip = clips.cut(wav, mark(0.2, 0.3, "A"))
        ch, width, rate, n, data = params(clip)
        self.assertEqual((ch, width, rate), (2, 2, 44100))
        self.assertEqual(n, int(44100 * 0.8))                                  # clamped at 0: 0 .. 0.8 s
        self.assertEqual(data, frames[:4 * n])
        (m,) = wavinfo.read_markers(io.BytesIO(clip))
        self.assertAlmostEqual(m["start"], 0.2, places=4)
        self.assertEqual(m["note"], "EVP A")

    def test_24_bit_and_the_end_clamp(self):
        wav, frames = pcm_wav(8000, 1, 1.0, width=3)
        clip = clips.cut(wav, mark(0.9, 1.0, "C"))
        ch, width, rate, n, data = params(clip)
        self.assertEqual((width, n), (3, int(8000 * 0.6)))
        self.assertEqual(data, frames[3 * 3200:])

    def test_odd_data_lengths_are_padded(self):
        for width in (1, 3):                                               # 8-bit, and 24-bit (block align 3)
            wav, frames = pcm_wav(11025, 1, 1.0, width=width)
            clip = clips.cut(wav, mark(0.3, 0.4, "B", "odd"))
            n = math.ceil(0.9 * 11025)                                     # 0 .. 0.9 s: an odd number of frames
            self.assertEqual(n % 2, 1)
            self.assertEqual(len(clip) % 2, 0, width)                      # the data chunk's pad byte is there
            self.assertEqual(struct.unpack_from("<I", clip, 4)[0], len(clip) - 8)
            ch, got_width, rate, got_n, data = params(clip)
            self.assertEqual((got_width, rate, got_n), (width, 11025, n))
            self.assertEqual(data, frames[:width * n])
            (m,) = wavinfo.read_markers(io.BytesIO(clip))                  # the chunks after the pad byte read back
            self.assertEqual(m["note"], "EVP B: odd")
            self.assertAlmostEqual(m["start"], 0.3, places=3)

    def test_extra_chunks_and_old_markers_are_not_copied(self):
        wav, _ = pcm_wav(8000, 1, 2.0)
        marked = wavinfo.with_markers(wav, [mark(0.1, 0.2, "A", "old"), mark(1.5, 1.6, "B", "other")])
        clip = clips.cut(marked, mark(1.0, 1.1, "C", "new"))
        self.assertEqual([m["note"] for m in wavinfo.read_markers(io.BytesIO(clip))], ["EVP C: new"])

    def test_a_mark_outside_the_audio_is_refused(self):
        wav, _ = pcm_wav(8000, 1, 1.0)
        with self.assertRaises(ValueError):
            clips.cut(wav, mark(5.0, 5.5))
        with self.assertRaises(ValueError):
            clips.cut(b"not a wav", mark(0.1, 0.2))

    def test_names(self):
        self.assertEqual(clips.stamp(12.4), "00m12.4s")
        self.assertEqual(clips.stamp(59.96), "01m00.0s")                       # rounds into the next minute
        self.assertEqual(clips.stamp(754.0), "12m34.0s")
        self.assertEqual(clips.name("001_A_003", mark(12.4, 13.0, "A")), "001_A_003_EVP-A_00m12.4s.wav")
        self.assertEqual(clips.name("x", mark(1.0, 2.0, "B", "get out")), "x_EVP-B_00m01.0s_get out.wav")
        self.assertEqual(clips.safe_note('who: "are" you?/\\|*<>\tnow'), "who are you now")
        self.assertEqual(clips.safe_note("trailing dots..."), "trailing dots")
        self.assertEqual(clips.safe_note(" ... "), "")
        self.assertEqual(clips.safe_note("a\u200bb\u202e c\ufeff\u2066d"), "ab cd")      # Cf: invisible, dropped
        self.assertEqual(clips.safe_note("\u200b\u200f"), "")
        self.assertEqual(clips.name("x", mark(1.0, 2.0, "C", "???")), "x_EVP-C_00m01.0s.wav")
        long = clips.safe_note("a" * 30 + " " + "b" * 30)
        self.assertEqual(len(long), clips.MAX_NOTE)
        self.assertEqual(clips.name("x", mark(1.0, 2.0, "A", "hi"), with_note=False), "x_EVP-A_00m01.0s.wav")


class PlayerClipsTests(unittest.TestCase):
    """export_clips() on a loaded recording, with the fixtures of test_marks_api."""
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api
    wait_event = tm.MarksApiTests.wait_event
    load = tm.MarksApiTests.load
    marked_file = tm.MarksApiTests.marked_file

    def clips_dir(self, *parts):
        return os.path.join(self.dest, *parts, backend.CLIPS)

    def test_device_recording_all_marks_then_one(self):
        rec = self.load()["rec"]
        self.assertEqual(self.api.export_clips(rec)["error"], "This recording has no marks yet.")
        a = self.api.add_mark(rec, 0.1, 0.4, "A", "hi there")["mark"]
        self.assertEqual(self.wait_event()[0], "backup-done")
        self.api.add_mark(rec, 0.6, 0.7, "C", "")
        r = self.api.export_clips(rec)
        stem = os.path.splitext(tm.DVF_1)[0]
        names = [f"{stem}_EVP-A_00m00.1s_hi there.wav", f"{stem}_EVP-C_00m00.6s.wav"]
        self.assertEqual((r["ok"], r["saved"], r["already"], r["names"], r["notes"]), (True, 2, 0, names, []))
        self.assertEqual(r["folder"], self.clips_dir("A"))                  # beside the WAV with marks
        self.assertEqual(sorted(os.listdir(r["folder"])), sorted(names + [library_ops.CLIPS_MARKER]))
        with wave.open(os.path.join(r["folder"], names[0])) as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (8000, 1))   # the decoded audio's own format
            self.assertEqual(w.getnframes(), int(8000 * 0.9))                  # 0 .. 0.9 s (clamped at 0)
        (m,) = wavinfo.read_markers(os.path.join(r["folder"], names[0]))
        self.assertEqual(m["note"], "EVP A: hi there")
        # Again: identical bytes are already saved, nothing is overwritten or numbered.
        r = self.api.export_clips(rec)
        self.assertEqual((r["saved"], r["already"]), (0, 2))
        self.assertEqual(len(os.listdir(r["folder"])), 3)
        # Save clip: that mark only. After the mark moved, same name, other bytes: a numbered copy.
        self.api.update_mark(rec, a["id"], end=0.5)
        r = self.api.export_clips(rec, a["id"])
        self.assertEqual((r["saved"], r["already"], r["names"]), (1, 0, [f"{stem}_EVP-A_00m00.1s_hi there (2).wav"]))
        self.assertEqual(self.api.export_clips(rec, "nope")["error"], "That mark is no longer there.")
        self.assertEqual(self.api.export_clips(rec, 5)["error"], "That mark is no longer there.")
        self.assertFalse(self.api._busy.locked())

    def test_library_file_goes_to_its_investigations_clips_folder(self):
        library = os.path.join(self.tmp, "Library")
        path = os.path.join(library, "Old Jail", "Night 2", "cell 3.wav")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(tm.wav_bytes(b"jail", seconds=3.0))
        api = self.new_api(pick_wav=lambda start: path)
        api._lib_folder = library
        rec = api.open_wav()["rec"]
        api.add_mark(rec, 1.2, 1.5, "B", 'who: "is" there?')
        r = api.export_clips(rec)
        self.assertEqual(r["names"], ["cell 3_EVP-B_00m01.2s_who is there.wav"])
        self.assertEqual(r["folder"], self.clips_dir("Old Jail"))
        with open(path, "rb") as f:
            original = f.read()
        with open(os.path.join(r["folder"], r["names"][0]), "rb") as f:
            clip = f.read()
        self.assertEqual(params(clip)[4], params(original)[4][2 * int(0.7 * 8000):2 * int(2.0 * 8000)])

    def test_admission_matches_export_marked(self):
        api, rec = self.marked_file()
        self.assertTrue(api._busy.acquire(blocking=False))                  # an export is running
        try:
            self.assertEqual(api.export_clips(rec)["error"], backend.CLIPS_BUSY)
            api._updating = True
            self.assertEqual(api.export_clips(rec)["error"], backend.CLIPS_BUSY_UPDATE)
            self.assertEqual(api.export_clips_folder("root", 1)["error"], backend.LIB_CHANGED)   # (not listed)
        finally:
            api._updating = False
            api._busy.release()
        self.assertTrue(api.export_clips(rec)["ok"])
        self.assertEqual(api.export_clips("nope")["error"], backend.RELOAD)
        api.request_stop()
        self.assertEqual(api.export_clips(rec)["error"], backend.CLOSING)
        self.assertFalse(api._busy.locked())

    def test_shutdown_waits_for_it_and_the_close_prompt_names_it(self):
        api, rec = self.marked_file()
        entered, go = threading.Event(), threading.Event()
        real = backend.save_wav

        def slow(*a):
            entered.set()
            go.wait(5)
            return real(*a)
        result = []
        with mock.patch.object(backend, "save_wav", slow):
            saver = threading.Thread(target=lambda: result.append(api.export_clips(rec)))
            saver.start()
            self.assertTrue(entered.wait(5))
            self.assertTrue(api.saving_marked())
            self.assertEqual(api.export_marked(rec)["error"], backend.MARKED_BUSY)
            closer = threading.Thread(target=api.shutdown)
            closer.start()
            closer.join(0.3)
            self.assertTrue(closer.is_alive())
            go.set()
            closer.join(5)
            saver.join(5)
        self.assertTrue(result[0]["ok"], result)
        self.assertFalse(api.saving_marked())

    def test_read_only_store_is_refused(self):
        second = AppData(os.path.join(self.tmp, "appdata"))                   # the first holds the lock
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        rec = api.audio(tm.ID, "A", 1)["rec"]
        r = api.export_clips(rec)
        self.assertFalse(r["ok"])
        self.assertIn("Another OpenEVP", r["error"])
        self.assertIn("Another OpenEVP", api.export_clips_folder("root", 1)["error"])


class LibraryClipsTests(unittest.TestCase):
    """The library's clips job, with the fixtures of test_library (the library is the Save-to folder)."""
    setUp = tl.LibraryTests.setUp
    new_api = tl.LibraryTests.new_api
    write = tl.LibraryTests.write
    index = tl.LibraryTests.index

    def mark(self, data, start, end, cls="A", note=""):
        fp = wavinfo.wav_fingerprint(io.BytesIO(data))
        self.store.add_mark(fp, start, end, cls, note, name="x", duration=2.0)
        return fp

    def run_job(self, start):
        r = start()
        self.assertTrue(r["ok"], r)
        with self.events.cond:
            ok = self.events.cond.wait_for(
                lambda: any(n in ("clips-done", "clips-failed") and p["job"] == r["job"] for n, p in self.events.items),
                WAIT)
        self.assertTrue(ok, self.events.items)
        return next((n, p) for n, p in self.events.items if n in ("clips-done", "clips-failed") and p["job"] == r["job"])

    def folder_id(self, listing, *rel):
        return next(d["id"] for d in listing["folders"] if d["rel"] == list(rel))

    def test_folder_job_over_nested_folders(self):
        night1 = tm.wav_bytes(b"n1", seconds=2.0)
        deeper = tm.wav_bytes(b"deep", seconds=2.0)
        self.write("Case/Night 1/one.wav", night1)
        self.write("Case/Night 1/Deeper/two.wav", deeper)
        self.write("Case/Night 1/plain.wav", tm.wav_bytes(b"plain", seconds=2.0))   # no marks: passed over
        self.write("Case/Night 1/broken.wav", b"RIFF junk")                            # damaged: reported
        self.write("Case/Night 1/x.dvf", tl.dvf_bytes())                               # decoder missing: reported
        self.write("Other/three.wav", tm.wav_bytes(b"three", seconds=2.0))             # not in the folder
        self.write("Case/Clips/old.wav", deeper)                                        # clips already: left out
        self.write("Case/Clips/" + library_ops.CLIPS_MARKER, b"")                     # (OpenEVP made that folder)
        self.mark(night1, 0.5, 0.7, "A", "hello")
        self.mark(night1, 1.0, 1.1, "B")
        self.mark(deeper, 1.5, 1.6, "C", "knock")
        api = self.new_api()
        fake = tl.FakeDecoder()
        with fake.installed(), mock.patch.object(backend, "_decoder_problems",
                                                 lambda kinds: {"dvf": "the WAV decoder is not available"}):
            listing = self.index(api)
            event, p = self.run_job(lambda: api.export_clips_folder(self.folder_id(listing, "Case"), 7))
        self.assertEqual(event, "clips-done")
        clips_dir = os.path.join(self.lib, "Case", backend.CLIPS)
        self.assertEqual((p["saved"], p["already"], p["recordings"], p["cancelled"]), (3, 0, 2, False))
        self.assertEqual(p["folder"], clips_dir)
        self.assertEqual(p["where"], "Case")
        self.assertEqual(sorted(os.listdir(clips_dir)),
                         [library_ops.CLIPS_MARKER, "old.wav", "one_EVP-A_00m00.5s_hello.wav", "one_EVP-B_00m01.0s.wav",
                          "two_EVP-C_00m01.5s_knock.wav"])
        self.assertEqual(len(p["skipped"]), 2)
        self.assertTrue(any(s.startswith("broken.wav (its audio could not be decoded") for s in p["skipped"]), p)
        self.assertIn("x.dvf (the WAV decoder is not available)", p["skipped"])
        self.assertEqual(fake.calls, 0)
        progress = [q for n, q in self.events.items if n == "clips-progress"]
        self.assertEqual([q["done"] for q in progress], list(range(1, 6)))
        self.assertEqual({q["total"] for q in progress}, {5})
        self.assertFalse(api._busy.locked())
        # Again: all already there.
        event, p = self.run_job(lambda: api.export_clips_folder(self.folder_id(listing, "Case"), 8))
        self.assertEqual((p["saved"], p["already"]), (0, 3))

    def test_a_recording_and_its_copies_export_once(self):
        data = tl.dvf_bytes()
        dvf_path = self.write("Case/rec.dvf", data)
        self.write("Case/rec.wav", tl.DECODED)                                # the same audio as the .dvf decodes to
        self.mark(tl.DECODED, 0.2, 0.4, "A")
        api = self.new_api()
        fake = tl.FakeDecoder()
        with fake.installed():
            listing = self.index(api)
            ids = [f["id"] for f in listing["files"]]
            event, p = self.run_job(lambda: api.export_clips_files(ids, 3))
        self.assertEqual((event, p["saved"], p["recordings"]), ("clips-done", 1, 1))
        clips_dir = os.path.join(self.lib, "Case", backend.CLIPS)
        self.assertEqual(sorted(os.listdir(clips_dir)), [library_ops.CLIPS_MARKER, "rec_EVP-A_00m00.2s.wav"])
        marker = os.path.join(clips_dir, library_ops.CLIPS_MARKER)
        with open(marker, "rb") as f:
            self.assertIn(b"OpenEVP made this folder", f.read())
        if sys.platform == "win32":
            self.assertTrue(os.stat(marker).st_file_attributes & 0x2, "the marker is hidden")
        self.assertTrue(os.path.isfile(dvf_path))
        self.assertEqual(api.export_clips_files(["unknown"], 4)["error"], backend.LIB_CHANGED)
        self.assertEqual(api.export_clips_files([], 4)["error"], "Nothing valid is selected.")
        self.assertEqual(api.export_clips_folder("nope", 4)["error"], backend.LIB_CHANGED)

    def test_a_damaged_recording_is_skipped_with_its_reason(self):
        bad = tl.dvf_bytes(counter=7)
        self.write("Case/bad.dvf", bad)
        good = tm.wav_bytes(b"good", seconds=2.0)
        self.write("Case/good.wav", good)
        self.mark(good, 0.2, 0.4, "B")
        api = self.new_api()
        fake = tl.FakeDecoder()
        fake.bad.add(bad)
        with fake.installed():
            listing = self.index(api)
            event, p = self.run_job(lambda: api.export_clips_folder("root", 5))
        self.assertEqual((event, p["saved"]), ("clips-done", 1))
        self.assertEqual(p["skipped"], ["bad.dvf (its audio could not be decoded: this is not a recording)"])
        self.assertTrue(listing["ok"])

    def test_cancel_stops_the_job_and_frees_the_app(self):
        for i in range(3):
            self.write(f"Case/{i}.dvf", tl.dvf_bytes(counter=i + 1))
        self.mark(tl.DECODED, 0.2, 0.4, "A")
        api = self.new_api()
        fake = tl.FakeDecoder(block=1, honour_stop=True)
        with fake.installed():
            # The library as a listing leaves it, without starting the indexer: every file decodes.
            api._library_folders = {"root": self.lib}
            api._library_root = backend._root_identity(self.lib)
            r = api.export_clips_folder("root", 9)
            self.assertTrue(fake.started.wait(WAIT))
            self.assertTrue(api.clips_running())                               # the close prompt names it
            self.assertFalse(api.exporting())
            self.assertEqual(api.export_clips_folder("root", 10)["error"], backend.CLIPS_BUSY)
            api.cancel_clips(8)                                                # another job: nothing happens
            self.assertFalse(fake.saw_stop)
            api.cancel_clips(9)
            event, p = self.run_job(lambda: r)
        self.assertEqual((event, p["cancelled"], p["saved"]), ("clips-done", True, 0))
        self.assertFalse(api.clips_running())
        self.assertTrue(fake.saw_stop)
        self.assertFalse(api._busy.locked())
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Case", backend.CLIPS)))

    def listed_without_indexing(self, api):
        """The library as a listing leaves it, without starting the indexer: every file decodes."""
        api._library_folders = {"root": self.lib}
        api._library_root = backend._root_identity(self.lib)

    def test_a_save_to_change_during_the_job_does_not_split_it(self):
        self.write("Case1/a.dvf", tl.dvf_bytes())
        b = tm.wav_bytes(b"b", seconds=2.0)
        self.write("Case2/b.wav", b)
        self.mark(tl.DECODED, 0.2, 0.4, "A")
        self.mark(b, 0.5, 0.6, "B")
        api = self.new_api()
        fake = tl.FakeDecoder(block=1)
        with fake.installed():
            self.listed_without_indexing(api)
            r = api.export_clips_folder("root", 1)
            self.assertTrue(fake.started.wait(WAIT))                           # a.dvf is being decoded
            elsewhere = os.path.join(self.tmp, "Elsewhere")
            os.makedirs(elsewhere)
            self.picked = elsewhere
            self.assertEqual(api.choose_destination(), elsewhere)              # Save-to (and so the library) moves
            fake.release.set()
            event, p = self.run_job(lambda: r)
        self.assertEqual((event, p["saved"], p["recordings"]), ("clips-done", 2, 2))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Case1", backend.CLIPS, "a_EVP-A_00m00.2s.wav")))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Case2", backend.CLIPS, "b_EVP-B_00m00.5s.wav")))
        self.assertEqual(p["folder"], self.lib)                                  # Open folder: the Save-to it used
        self.assertEqual(os.listdir(elsewhere), [])

    def test_shutdown_during_a_folder_job_stops_it_and_waits(self):
        for i in range(3):
            self.write(f"Case/{i}.dvf", tl.dvf_bytes(counter=i + 1))
        self.mark(tl.DECODED, 0.2, 0.4, "A")
        api = self.new_api()
        fake = tl.FakeDecoder(block=1, honour_stop=True)
        with fake.installed():
            self.listed_without_indexing(api)
            r = api.export_clips_folder("root", 4)
            self.assertTrue(fake.started.wait(WAIT))
            api.shutdown()                                                      # joins the job
            self.assertTrue(self.store._closed)
            event, p = self.run_job(lambda: r)
        self.assertEqual((event, p["cancelled"], p["closing"]), ("clips-done", True, True))
        self.assertFalse(api._busy.locked())
        self.assertFalse(api.clips_running())

    def test_the_busy_lock_is_free_when_the_page_hears_the_job_is_over(self):
        good = tm.wav_bytes(b"good", seconds=2.0)
        self.write("Case/good.wav", good)
        self.mark(good, 0.2, 0.4, "B")
        seen = []

        def emit(name, payload):
            if name in ("clips-done", "clips-failed"):
                seen.append((name, api._busy.locked(), api.clips_running()))
            self.events(name, payload)
        api = backend.Api(None, emit, lambda start: None, self.lib, self.server, store=self.store)
        self.addCleanup(api.shutdown)
        self.listed_without_indexing(api)
        self.run_job(lambda: api.export_clips_folder("root", 2))
        self.assertEqual(seen, [("clips-done", False, False)])

    def test_a_path_too_long_drops_the_note_then_skips_the_clip(self):
        api = self.new_api()
        wav = tm.wav_bytes(b"w", seconds=2.0)
        outdir = os.path.join(self.lib, "Case", backend.CLIPS)
        marks = [dict(mark(0.2, 0.3, "A", "a long note here"), id="1"), dict(mark(1.0, 1.1, "B"), id="2")]
        with mock.patch.object(backend.folders, "too_long", lambda p: len(os.path.basename(p)) > 22):
            saved, already, names, notes = api._save_clips(wav, marks, outdir, "x")
        self.assertEqual((saved, already, names, notes), (2, 0, ["x_EVP-A_00m00.2s.wav", "x_EVP-B_00m01.0s.wav"], []))
        with mock.patch.object(backend.folders, "too_long", lambda p: True):
            saved, already, names, notes = api._save_clips(wav, marks[:1], outdir, "x")
        self.assertEqual((saved, already, names), (0, 0, []))
        self.assertEqual(notes, ["x: the EVP at 00m00.2s was not saved (the path would be too long for Windows)"])

    def test_closing_refuses_a_new_job(self):
        api = self.new_api()
        os.makedirs(self.lib)
        self.index(api)
        api.request_stop()
        self.assertEqual(api.export_clips_folder("root", 1)["error"], backend.CLOSING)
        self.assertFalse(api._busy.locked())

    def test_no_store_means_no_clips(self):
        api = self.new_api(store=None)
        os.makedirs(self.lib)
        self.index(api)
        self.assertEqual(api.export_clips_folder("root", 1)["error"], backend.NO_MARKS)


class ClipsFoldersInTheLibraryTests(tf.FolderApiBase):
    """The Clips folders OpenEVP made (they hold its marker file, at any depth) are listed
    with their clips, which play; but their markers are never imported, they never count
    as EVPs, Export clips never cuts them again and recordings cannot be moved in. Folder
    operations carry them along. A folder the user named Clips is an ordinary folder."""

    def populate(self):
        self.real = tm.wav_bytes(b"real", seconds=2.0)
        self.write("Case/Night/real.wav", wavinfo.with_markers(self.real, [mark(0.5, 0.7, "A", "hi")]))
        self.clip_a = wavinfo.with_markers(tm.wav_bytes(b"clip a"), [mark(0.5, 0.6, "A", "a clip")])
        self.clip_b = wavinfo.with_markers(tm.wav_bytes(b"clip b"), [mark(0.5, 0.6, "B", "b clip")])
        library_ops._make_clips_folder(os.path.join(self.lib, "Case", "Clips"))
        library_ops._make_clips_folder(os.path.join(self.lib, "Case", "Night", "clips"))   # any depth
        library_ops._make_clips_folder(os.path.join(self.lib, "CLIPS"))
        self.write("Case/Clips/a.wav", self.clip_a)
        self.write("Case/Night/clips/b.wav", self.clip_b)
        self.write("CLIPS/c.wav", tm.wav_bytes(b"clip c"))
        self.write("Case/Clips/Deeper/d.wav", tm.wav_bytes(b"clip d"))
        self.user = tm.wav_bytes(b"user", seconds=2.0)
        self.write("Other/Clips/user.wav", self.user)                     # the user's own folder named Clips

    def fp(self, data):
        return wavinfo.wav_fingerprint(io.BytesIO(data))

    def row(self, r, name):
        return next(f for f in r["files"] if f["name"] == name)

    def test_listed_with_their_clips_but_never_imported_or_counted(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        self.assertEqual(sorted((f["name"], f["clip"]) for f in r["files"]),
                         [("a.wav", True), ("b.wav", True), ("c.wav", True), ("d.wav", True),
                          ("real.wav", False), ("user.wav", False)])
        self.assertTrue(all(f["fp"] for f in r["files"]))                       # indexed (and so playable)
        flags = {tuple(d["rel"]): (d.get("clips", False), d.get("in_clips", False)) for d in r["folders"]}
        self.assertEqual(flags, {(): (False, False), ("Case",): (False, False), ("Case", "Clips"): (True, True),
                                 ("Case", "Clips", "Deeper"): (False, True), ("Case", "Night"): (False, False),
                                 ("Case", "Night", "clips"): (True, True), ("CLIPS",): (True, True),
                                 ("Other",): (False, False), ("Other", "Clips"): (False, False)})
        self.assertEqual(self.row(r, "a.wav")["folder_id"], self.folder(r, "Case", "Clips"))
        self.assertEqual(len(self.store.marks(self.fp(self.real))), 1)          # markers outside Clips: imported
        for clip in (self.clip_a, self.clip_b):
            self.assertIsNone(self.store.recording(self.fp(clip)))              # never from a clip
        self.assertEqual(self.row(r, "a.wav")["marks"], {"A": 0, "B": 0, "C": 0})
        self.assertEqual(set(self.store.summary()), {self.fp(self.real)})       # "Has EVPs" and counts: the recording only
        info = api.folder_info(self.folder(r, "Case"))
        self.assertEqual({k: info[k] for k in ("recordings", "with_evps", "clips", "evps_at_least", "other_files",
                                               "subfolders")},
                         {"recordings": 1, "with_evps": 1, "clips": 3, "evps_at_least": False, "other_files": 2,
                          "subfolders": 4})
        info = api.folder_info(self.folder(r, "Case", "Clips"))                  # the Clips folder itself
        self.assertEqual({k: info[k] for k in ("recordings", "with_evps", "clips", "subfolders")},
                         {"recordings": 0, "with_evps": 0, "clips": 2, "subfolders": 1})
        info = api.folder_info(self.folder(r, "Case", "Clips", "Deeper"))        # a folder inside one
        self.assertEqual((info["recordings"], info["clips"]), (0, 1))

    def test_a_clip_plays_without_importing_its_markers_and_a_mark_added_never_counts(self):
        self.populate()
        api = self.new_api()
        r = api.list_library()                                                   # not indexed yet: played first
        loaded = api.play_library(self.file(r, "a.wav"))
        self.assertTrue(loaded["ok"], loaded)
        self.assertEqual((loaded["name"], loaded["imported"]), ("a.wav", 0))
        self.assertIsNone(self.store.recording(self.fp(self.clip_a)))
        self.assertEqual(api.get_marks(loaded["rec"])["marks"], [])
        r = self.index(api)
        self.assertIsNone(self.store.recording(self.fp(self.clip_a)))           # nor by the indexer
        # Marked on purpose in the player: kept as usual, but the folders' figures stay the recordings'.
        self.assertTrue(api.add_mark(loaded["rec"], 0.2, 0.4, "C", "on the clip")["ok"])
        self.assertEqual(len(self.store.marks(self.fp(self.clip_a))), 1)
        r = self.index(api)
        self.assertEqual(self.row(r, "a.wav")["marks"], {"A": 0, "B": 0, "C": 1})
        self.assertTrue(self.row(r, "a.wav")["clip"])
        self.assertEqual(len(api.library_marks(self.file(r, "a.wav"))["marks"]), 1)
        for rel, evps in ((("Case",), 1), (("Case", "Clips"), 0)):
            info = api.folder_info(self.folder(r, *rel))
            self.assertEqual(info["with_evps"], evps, rel)
        # Played again: still nothing imported, the mark is still the one added.
        again = api.play_library(self.file(r, "a.wav"))
        self.assertEqual(again["imported"], 0)
        self.assertEqual([m["note"] for m in self.store.marks(self.fp(self.clip_a))], ["on the clip"])

    def test_export_clips_never_cuts_clips(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        self.store.add_mark(self.fp(self.user), 1.0, 1.2, "C", "", name="user.wav", duration=2.0)
        self.store.add_mark(self.fp(self.clip_b), 0.1, 0.2, "A", "", name="b.wav", duration=1.0)   # a clip, marked
        event, p = LibraryClipsTests.run_job(self, lambda: api.export_clips_folder("root", 1))
        self.assertEqual((event, p["recordings"], p["saved"], p["skipped"]), ("clips-done", 2, 2, []))
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Case", "Clips"))),
                         [library_ops.CLIPS_MARKER, "Deeper", "a.wav", "real_EVP-A_00m00.5s_hi.wav"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Case", "Night", "clips"))),
                         [library_ops.CLIPS_MARKER, "b.wav"])                    # the marked clip: not cut
        # The user's Clips folder got the clip, but no marker: OpenEVP did not create it, so it stays ordinary.
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Other", "Clips"))),
                         ["user.wav", "user_EVP-C_00m01.0s.wav"])
        r = self.index(api)
        self.assertEqual(sorted(f["name"] for f in r["files"] if not f["clip"]),
                         ["real.wav", "user.wav", "user_EVP-C_00m01.0s.wav"])
        self.assertTrue(self.row(r, "real_EVP-A_00m00.5s_hi.wav")["clip"])
        # A Clips folder (or one inside it), or clips picked as files: refused, nothing written.
        for rel in (("Case", "Clips"), ("Case", "Clips", "Deeper"), ("Case", "Night", "clips")):
            self.assertEqual(api.export_clips_folder(self.folder(r, *rel), 2)["error"], library_ops.CLIPS_AGAIN)
        self.assertEqual(api.export_clips_files([self.file(r, "b.wav")], 3)["error"], library_ops.CLIPS_AGAIN)
        self.assertFalse(api.clips_running())
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Case", "Night", "clips"))),
                         [library_ops.CLIPS_MARKER, "b.wav"])

    def test_a_removed_marker_makes_them_recordings(self):
        self.populate()
        os.remove(os.path.join(self.lib, "Case", "Clips", library_ops.CLIPS_MARKER))
        r = self.index(self.new_api())
        self.assertEqual(sorted(f["name"] for f in r["files"] if not f["clip"]), ["a.wav", "d.wav", "real.wav", "user.wav"])
        self.assertEqual(len(self.store.marks(self.fp(self.clip_a))), 1)        # an ordinary WAV's markers
        self.assertIsNone(self.store.recording(self.fp(self.clip_b)))

    def test_a_save_to_folder_named_clips_is_an_ordinary_folder(self):
        self.lib = os.path.join(self.tmp, "Clips")                           # Save-to and library: "Clips"
        os.makedirs(self.lib)
        self.write("Case/x.wav", tm.wav_bytes(b"x", seconds=2.0))
        api = self.new_api()
        r = self.index(api)
        self.assertEqual([(f["name"], f["clip"]) for f in r["files"]], [("x.wav", False)])
        self.store.add_mark(self.fp(tm.wav_bytes(b"x", seconds=2.0)), 0.5, 0.6, "A", "", name="x", duration=2.0)
        event, p = LibraryClipsTests.run_job(self, lambda: api.export_clips_folder("root", 1))
        self.assertEqual((event, p["saved"]), ("clips-done", 1))
        self.assertEqual([(f["name"], f["clip"]) for f in self.index(api)["files"]],
                         [("x.wav", False), ("x_EVP-A_00m00.5s.wav", True)])

    def test_rename_and_delete_carry_clips_along_untouched(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        self.assertTrue(api.rename_folder(self.folder(r, "Case"), "Case 2")["ok"])
        with open(os.path.join(self.lib, "Case 2", "Clips", "a.wav"), "rb") as f:
            self.assertEqual(f.read(), self.clip_a)
        with open(os.path.join(self.lib, "Case 2", "Night", "clips", "b.wav"), "rb") as f:
            self.assertEqual(f.read(), self.clip_b)
        r = self.index(api)
        self.assertEqual(sorted(f["name"] for f in r["files"] if f["clip"]), ["a.wav", "b.wav", "c.wav", "d.wav"])
        self.assertTrue(next(d for d in r["folders"] if d["rel"] == ["Case 2", "Clips"])["clips"])
        with mock.patch.object(backend.Api, "_index_file", side_effect=AssertionError("clips are never read")):
            self.assertEqual(api.delete_folder(self.folder(r, "Case 2")), {"ok": True, "backups": 0})
        binned = os.path.join(self.tmp, "bin-1")
        with open(os.path.join(binned, "Clips", "a.wav"), "rb") as f:
            self.assertEqual(f.read(), self.clip_a)
        self.assertTrue(os.path.isfile(os.path.join(binned, "Clips", "Deeper", "d.wav")))
        self.assertIsNone(self.store.recording(self.fp(self.clip_a)))
        self.assertTrue(os.path.isfile(os.path.join(binned, "Clips", library_ops.CLIPS_MARKER)))
        self.assertEqual(self.names(), ["CLIPS", "Other"])

    def test_a_clips_folder_itself_renames_and_deletes_like_any_folder(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        loaded = api.play_library(self.file(r, "c.wav"))
        self.assertTrue(api.rename_folder(self.folder(r, "CLIPS"), "Shared EVPs")["ok"])
        r = self.index(api)
        renamed = next(d for d in r["folders"] if d["rel"] == ["Shared EVPs"])
        self.assertTrue(renamed["clips"])                                       # its marker went along
        self.assertTrue(self.row(r, "c.wav")["clip"])
        self.assertEqual(api.recording_changed(loaded["rec"]), {"ok": True, "changed": False})
        info = api.folder_info(renamed["id"])
        self.assertEqual((info["recordings"], info["clips"], info["with_evps"]), (0, 1, 0))
        self.assertEqual(api.delete_folder(renamed["id"]), {"ok": True, "backups": 0})
        self.assertEqual(self.recycled, [os.path.join(self.lib, "Shared EVPs")])   # the Recycle Bin, never for good
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "bin-1", "c.wav")))
        self.assertEqual(self.names(), ["Case", "Other"])

    def test_clips_never_push_recordings_off_the_list(self):
        library_ops._make_clips_folder(os.path.join(self.lib, "A Clips"))     # walked before the recordings
        for i in range(4):
            self.write(f"A Clips/c{i}.wav", tm.wav_bytes(b"clip %d" % i))
        for i in range(3):
            self.write(f"B/r{i}.wav", tm.wav_bytes(b"rec %d" % i))
        api = self.new_api()
        with mock.patch.object(backend, "SAVED_LIMIT", 3):
            r = api.list_library()
            self.assertEqual(sorted(f["name"] for f in r["files"] if not f["clip"]), ["r0.wav", "r1.wav", "r2.wav"])
            self.assertEqual(sorted(f["name"] for f in r["files"] if f["clip"]), ["c0.wav", "c1.wav", "c2.wav"])
            self.assertTrue(r["truncated"])                                     # a clip was left off
            os.remove(os.path.join(self.lib, "A Clips", "c3.wav"))
            r = api.list_library()
            self.assertEqual((len(r["files"]), r["truncated"]), (6, False))      # 3 recordings + 3 clips: all there
            self.write("B/r3.wav", tm.wav_bytes(b"rec 3"))
            r = api.list_library()
            self.assertEqual(len([f for f in r["files"] if not f["clip"]]), 3)
            self.assertTrue(r["truncated"])

    def test_save_to_inside_a_clips_folder_is_refused(self):
        self.populate()
        api = self.new_api()
        before = api.default_destination()
        for rel in (("Case", "Clips"), ("Case", "Clips", "Deeper")):
            picked = os.path.join(self.lib, *rel)
            api._pick = lambda start, picked=picked: picked
            self.assertEqual(api.choose_destination(), {**backend._fail(backend.SAVE_TO_CLIPS)})
            self.assertEqual(api.default_destination(), before)
            self.assertNotEqual(self.store.get_setting("save_folder"), picked)
        ordinary = os.path.join(self.lib, "Other", "Clips")                      # the user's own: fine
        api._pick = lambda start: ordinary
        self.assertEqual(api.choose_destination(), ordinary)

    def test_the_player_never_cuts_clips_from_a_clip(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        loaded = api.play_library(self.file(r, "a.wav"))
        self.assertTrue(api.add_mark(loaded["rec"], 0.2, 0.4, "C", "")["ok"])
        res = api.export_clips(loaded["rec"])
        self.assertEqual((res["ok"], res["error"]), (False, library_ops.CLIPS_AGAIN))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Case", "Clips", "Clips")))
        self.assertFalse(api._busy.locked())

    def test_recordings_cannot_be_moved_into_a_clips_folder(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        for rel in (("Case", "Clips"), ("Case", "Clips", "Deeper")):
            res = api.move_files([self.file(r, "real.wav")], self.folder(r, *rel))
            self.assertEqual((res["ok"], res["error"]), (False, library_ops.CLIPS_ONLY))
        res = api.move_files([self.file(r, "a.wav"), self.file(r, "real.wav")], self.folder(r, "Case", "Clips", "Deeper"))
        self.assertEqual(res["error"], library_ops.CLIPS_ONLY)                  # any recording among them: none move
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Case", "Night", "real.wav")))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Case", "Clips", "a.wav")))
        # Clips move between Clips folders, and out of one (they are then ordinary WAVs).
        res = api.move_files([self.file(r, "a.wav")], self.folder(r, "Case", "Clips", "Deeper"))
        self.assertEqual(res["moved"], 1, res)
        r = api.list_library()
        res = api.move_files([self.file(r, "c.wav")], self.folder(r, "Other"))
        self.assertEqual(res["moved"], 1, res)
        r = api.list_library()
        self.assertEqual({f["name"]: f["clip"] for f in r["files"] if f["name"] in ("a.wav", "c.wav")},
                         {"a.wav": True, "c.wav": False})


if __name__ == "__main__":
    unittest.main()
