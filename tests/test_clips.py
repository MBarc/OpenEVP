"""EVP clips: cutting one WAV per mark (openevp.clips), the player's Export clips /
Save clip (Api.export_clips) and the library's background clips job
(Api.export_clips_files / export_clips_folder)."""
import io
import os
import struct
import sys
import threading
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import test_library as tl  # noqa: E402
import test_marks_api as tm  # noqa: E402
from app import backend  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import clips, wavinfo  # noqa: E402

WAIT = 30


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
        self.assertEqual(sorted(os.listdir(r["folder"])), sorted(names))
        with wave.open(os.path.join(r["folder"], names[0])) as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (8000, 1))   # the decoded audio's own format
            self.assertEqual(w.getnframes(), int(8000 * 0.9))                  # 0 .. 0.9 s (clamped at 0)
        (m,) = wavinfo.read_markers(os.path.join(r["folder"], names[0]))
        self.assertEqual(m["note"], "EVP A: hi there")
        # Again: identical bytes are already saved, nothing is overwritten or numbered.
        r = self.api.export_clips(rec)
        self.assertEqual((r["saved"], r["already"]), (0, 2))
        self.assertEqual(len(os.listdir(r["folder"])), 2)
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
                         ["old.wav", "one_EVP-A_00m00.5s_hello.wav", "one_EVP-B_00m01.0s.wav",
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
        self.assertEqual(os.listdir(os.path.join(self.lib, "Case", backend.CLIPS)), ["rec_EVP-A_00m00.2s.wav"])
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
            self.assertTrue(api.exporting())                                   # the close prompt: an export runs
            self.assertEqual(api.export_clips_folder("root", 10)["error"], backend.CLIPS_BUSY)
            api.cancel_clips(8)                                                # another job: nothing happens
            self.assertFalse(fake.saw_stop)
            api.cancel_clips(9)
            event, p = self.run_job(lambda: r)
        self.assertEqual((event, p["cancelled"], p["saved"]), ("clips-done", True, 0))
        self.assertTrue(fake.saw_stop)
        self.assertFalse(api._busy.locked())
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Case", backend.CLIPS)))

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


if __name__ == "__main__":
    unittest.main()
