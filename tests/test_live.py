"""Live mode and analog import: the WAV written while recording (openevp.livewav),
splitting an import on silence (openevp.silence), and the backend's recording
calls (app/live.py): placement and names in the library, marks stored against
the finished file's fingerprint, the disk-space stop, and finishing the .part
files a crash left behind."""
import base64
import collections
import json
import os
import struct
import sys
import tempfile
import unittest
import wave
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_library import Events, FakeServer  # noqa: E402
from app import backend, live, mic_permission  # noqa: E402
from app.main import _close_question  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import livewav, silence, wavinfo  # noqa: E402

RATE = 8000


def tone(seconds, rate=RATE, db=-12.0, hz=440.0, channels=1):
    t = np.arange(int(seconds * rate)) / rate
    x = np.sin(2 * np.pi * hz * t) * (10 ** (db / 20)) * np.sqrt(2)
    return np.repeat(x[:, None], channels, axis=1)


def noise(seconds, rate=RATE, db=-80.0, channels=1, seed=1):
    rng = np.random.default_rng(seed)
    return rng.standard_normal((int(seconds * rate), channels)) * (10 ** (db / 20))


def pcm(x):
    return np.clip(np.round(x * 32768), -32768, 32767).astype("<i2").tobytes()


class Tmp(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = t.name


# ---- the WAV written while recording ------------------------------------------------

class WavPartTests(Tmp):
    def test_incremental_writes_keep_a_playable_file_and_the_fingerprint_matches(self):
        path = os.path.join(self.tmp, "a.wav.part")
        w = livewav.WavPart(path, RATE, 2, header_every=1.0)
        data = pcm(tone(0.6, channels=2))
        w.write(data)
        with open(path, "rb") as f:                       # not a whole second yet: header still says 0
            self.assertEqual(struct.unpack("<I", f.read(44)[40:44])[0], 0)
        w.write(data)                                     # 1.2 s: the header was rewritten
        with wave.open(path) as r:                        # readable while still being written
            self.assertEqual((r.getnchannels(), r.getframerate(), r.getnframes()), (2, RATE, int(1.2 * RATE)))
        frames, fp = w.close()
        self.assertEqual(frames, int(1.2 * RATE))
        self.assertEqual(fp, wavinfo.wav_fingerprint(path))
        with wave.open(path) as r:
            self.assertEqual(r.readframes(r.getnframes()), data + data)

    def test_never_writes_over_an_existing_file(self):
        path = os.path.join(self.tmp, "a.wav.part")
        with open(path, "wb") as f:
            f.write(b"keep")
        with self.assertRaises(FileExistsError):
            livewav.WavPart(path, RATE, 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"keep")

    def test_whole_frames_only_and_the_size_limit(self):
        w = livewav.WavPart(os.path.join(self.tmp, "a.part"), RATE, 2, max_data=4000)
        self.addCleanup(w.close)
        with self.assertRaises(ValueError):
            w.write(b"\0\0")                              # half a stereo frame
        w.write(bytes(4000))
        with self.assertRaises(livewav.Full):
            w.write(bytes(4))
        self.assertEqual(w.room(), 0)

    def test_publish_numbers_a_taken_name(self):
        for n in ("Live x.wav", "Live x (2).wav"):
            open(os.path.join(self.tmp, n), "wb").close()
        part = os.path.join(self.tmp, "Live x.wav.part")
        livewav.WavPart(part, RATE, 1).close()
        got = livewav.publish(part, self.tmp, "Live x.wav")
        self.assertEqual(os.path.basename(got), "Live x (3).wav")
        self.assertFalse(os.path.exists(part))
        self.assertEqual(os.path.getsize(os.path.join(self.tmp, "Live x.wav")), 0)   # untouched

    def test_recover_a_part_cut_off_mid_frame(self):
        path = os.path.join(self.tmp, "a.wav.part")
        w = livewav.WavPart(path, RATE, 2, header_every=1000)
        w.write(pcm(tone(1.0, channels=2)))
        w._f.flush()                                      # a crash: the header still says 0 frames
        w._f.write(b"\1\2\3")                             # and a partial frame at the end
        w._f.close()
        self.assertEqual(livewav.recover(path), (RATE, 2, RATE))
        with wave.open(path) as r:
            self.assertEqual(r.getnframes(), RATE)

    def test_recover_without_a_header_needs_the_format(self):
        path = os.path.join(self.tmp, "a.wav.part")
        with open(path, "wb") as f:
            f.write(bytes(44) + bytes(100))
        with self.assertRaises(ValueError):
            livewav.recover(path)
        self.assertEqual(livewav.recover(path, 8000, 1), (8000, 1, 50))
        with wave.open(path) as r:
            self.assertEqual((r.getframerate(), r.getnframes()), (8000, 50))


# ---- splitting an import on silence ---------------------------------------------------

def cuts_of(parts, gap=3.0, channels=1):
    """Where the pieces after the first start (seconds), for these parts one after the other."""
    x = np.concatenate(parts)
    samples = np.frombuffer(pcm(x), dtype="<i2")
    block = silence.block_frames(RATE)
    levels = silence.block_levels(samples, channels, block)
    return [round(c * block / RATE, 3) for c in silence.find_cuts(levels, RATE, gap)]


class SplitterTests(unittest.TestCase):
    """openevp.silence: where an import splits, from the whole recording at once."""

    def line(self, s, seed):                              # the cable's hiss while the recorder plays nothing
        return noise(s, db=-85, seed=seed)

    def recording(self, s, seed):                         # a recording: room tone with a voice in it
        x = noise(s, db=-50, seed=seed)
        a, b = int(0.3 * len(x)), int(0.6 * len(x))
        x[a:b] += tone(s, db=-15)[:b - a]
        return x

    def test_recordings_with_gaps_split_half_a_second_before_each_sound(self):
        self.assertEqual(cuts_of([self.line(2, 1), self.recording(5, 2), self.line(4, 3), self.recording(6, 4),
                                  self.line(5, 5), self.recording(4, 6), self.line(1, 7)]),
                         [2 + 5 + 4 - 0.5, 2 + 5 + 4 + 6 + 5 - 0.5])

    def test_a_gap_of_exactly_the_setting_still_has_its_pre_roll(self):
        self.assertEqual(cuts_of([self.line(2, 1), self.recording(5, 2), self.line(3, 3), self.recording(5, 4)]),
                         [2 + 5 + 3 - 0.5])

    def test_hiss_alone_and_quiet_at_the_ends_never_split(self):
        self.assertEqual(cuts_of([self.line(20, 1)]), [])
        self.assertEqual(cuts_of([self.line(6, 1), self.recording(5, 2), self.line(6, 3)]), [])

    def test_brief_pauses_never_split_a_recording(self):
        voice = lambda s, seed: noise(s, db=-50, seed=seed) + tone(s, db=-15)
        pause = lambda s, seed: noise(s, db=-50, seed=seed)          # the recording's own background
        self.assertEqual(cuts_of([self.line(2, 1), voice(2, 2), pause(2.5, 3), voice(1, 4), pause(2.9, 5),
                                  voice(2, 6), self.line(4, 7)]), [])

    def test_silent_pauses_shorter_than_the_gap_never_split(self):
        voice = lambda s: tone(s, db=-15) + noise(s, db=-50)
        self.assertEqual(cuts_of([self.line(2, 1), voice(2), self.line(2.5, 2), voice(2), self.line(5, 3)]), [])

    def test_a_recording_whose_pauses_sit_at_the_floor_is_never_cut_there(self):
        # No line hiss between recordings to tell them apart: the quietest thing in the file is the
        # recording's own room tone, its pauses sit at the floor, and the piece shows nothing of its
        # own above the floor. No cut.
        voice = lambda s: tone(s, db=-15) + noise(s, db=-50, seed=9)
        quiet = lambda s, seed: noise(s, db=-50, seed=seed)
        self.assertEqual(cuts_of([quiet(2, 1), voice(1.5), quiet(1, 4), voice(1), quiet(5, 2), voice(1.5),
                                  quiet(1, 5), voice(1), quiet(5, 3), voice(1.5)]), [])

    def test_astras_third_case_splits_only_at_the_true_floor(self):
        # 2 s at -50, 2 s at -30, 2 s at -15, 2 s at -30, 4 s at -65, more sound. Looked at as a whole,
        # -65 dB is the quietest the input ever is: the only cut is in that stretch, half a second
        # before the sound after it. The -50 dB stretch is not taken for the floor.
        x = [noise(2, db=-50, seed=1), noise(2, db=-30, seed=2), noise(2, db=-15, seed=3), noise(2, db=-30, seed=4),
             noise(4, db=-65, seed=5), noise(3, db=-15, seed=6)]
        self.assertEqual(cuts_of(x), [12 - 0.5])
        # Without the -65 dB stretch, nothing in the file is quieter than the room tone at its
        # start, which never comes back between sounds: no cut at all.
        self.assertEqual(cuts_of(x[:4] + [noise(4, db=-30, seed=7), x[5]]), [])

    def test_quiet_that_is_not_the_floor_does_not_split(self):
        # Under the recording's background but not back at the cable's idle hiss: no cut.
        self.assertEqual(cuts_of([self.line(2, 1), self.recording(6, 2), noise(5, db=-68, seed=3),
                                  self.recording(6, 4)]), [])

    def test_a_click_in_a_gap_stays_with_the_piece_before(self):
        click = tone(0.2, db=-10)
        self.assertEqual(cuts_of([self.line(2, 1), self.recording(5, 2), self.line(4, 3), click, self.line(4, 4),
                                  self.recording(5, 5), self.line(1, 6)]), [2 + 5 + 4 + 0.2 + 4 - 0.5])

    def test_a_longer_gap_setting_keeps_it_whole(self):
        self.assertEqual(cuts_of([self.line(2, 1), self.recording(4, 2), self.line(4, 3), self.recording(4, 4)],
                                 gap=5.0), [])

    def test_stereo(self):
        rec = noise(4, db=-50, channels=2)
        rec[RATE:2 * RATE] += tone(1, db=-12, channels=2)
        self.assertEqual(cuts_of([noise(2, db=-85, channels=2), rec, noise(4, db=-85, channels=2), rec],
                                 channels=2), [2 + 4 + 4 - 0.5])

    def test_digital_silence_is_not_taken_as_the_floor(self):
        block = silence.block_frames(RATE)
        self.assertEqual(silence.floor_of(silence.block_levels(np.zeros(4 * RATE, np.int16), 1, block), 20),
                         silence.MIN_FLOOR_DB)

    def test_an_hour_long_import_is_planned_in_seconds(self):
        # One pass over the whole file: an hour of 8 kHz audio (58 MB) with a recording every
        # ten minutes, read and planned in a few seconds; the analysis alone is linear.
        import time as clock
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hour.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                quiet = noise(60, db=-85, seed=1)
                busy = quiet.copy()
                busy[10 * RATE:40 * RATE] = self.recording(30, 2)
                quiet, busy = pcm(quiet), pcm(busy)
                for minute in range(60):
                    w.writeframes(busy if minute % 10 == 5 else quiet)
            t0 = clock.monotonic()
            cuts, rate, channels, frames = silence.plan(path, 3.0)
            took = clock.monotonic() - t0
        self.assertEqual(frames, 3600 * RATE)
        self.assertEqual([round(c / RATE, 2) for c in cuts], [15 * 60 + 10 - 0.5 + 600 * k for k in range(5)])
        self.assertLess(took, 15, f"planning an hour took {took:.1f} s")
        steady = np.full(72000 * 4, -60.0) + np.random.default_rng(1).normal(0, 0.5, 72000 * 4)   # four hours of blocks
        t0 = clock.monotonic()
        self.assertEqual(silence.find_cuts(steady, RATE), [])
        self.assertLess(clock.monotonic() - t0, 5)


# ---- the backend: recording into the library -----------------------------------------

class LiveApiTests(Tmp):
    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.tmp, "OpenEVP")
        self.store = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(self.store.close)
        self.events = Events()
        self.server = FakeServer()

    def api(self):
        a = backend.Api(None, self.events, lambda s: None, self.lib, self.server, store=self.store)
        self.addCleanup(a.shutdown)
        return a

    def start(self, a, **kw):
        opts = {"mode": "live", "folder": "root", "rate": RATE, "channels": 1, "split": 0, **kw}
        r = a.live_start(opts)
        self.assertTrue(r["ok"], r)
        return r

    def send(self, a, sid, x, seq):
        r = a.live_chunk(sid, seq, base64.b64encode(pcm(x)).decode())
        self.assertTrue(r["ok"], r)
        return r

    def test_live_recording_lands_in_the_library_with_its_marks(self):
        a = self.api()
        with mock.patch("app.live.datetime") as dt:
            dt.datetime.now.return_value = __import__("datetime").datetime(2026, 10, 3, 21, 5, 9)
            r = self.start(a)
        self.assertEqual(r["file"], "Live 2026-10-03 21-05-09.wav")
        sid = r["session"]
        part = os.path.join(self.lib, "Live 2026-10-03 21-05-09.wav.part")
        self.assertTrue(os.path.isfile(part))
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [part])
        x = tone(3.0)
        self.send(a, sid, x[:RATE], 0)
        m = a.live_mark(sid, 1.0)
        self.assertEqual((m["ok"], m["mark"]["start"], m["mark"]["end"]), (True, 0.0, 1.0))   # cut at the start
        self.send(a, sid, x[RATE:], 1)
        m2 = a.live_mark(sid, 2.5)
        self.assertEqual((m2["mark"]["start"], m2["mark"]["end"], m2["mark"]["cls"]), (0.5, 2.5, "C"))
        self.assertFalse(a.live_chunk(sid, 5, "AAAA")["ok"] and a.recording())   # out of order: stopped and saved
        self.assertFalse(a.recording())
        path = os.path.join(self.lib, "Live 2026-10-03 21-05-09.wav")
        with wave.open(path) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (RATE, 1, 3 * RATE))
        fp = wavinfo.wav_fingerprint(path)
        marks = self.store.marks(fp)
        self.assertEqual([(m["start"], m["end"], m["cls"], m["note"]) for m in marks],
                         [(0.0, 1.0, "C", live.MARK_NOTE), (0.5, 2.5, "C", live.MARK_NOTE)])
        self.assertFalse(os.path.exists(part) or os.path.exists(part + ".json"))
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])
        st = os.stat(path)                                # the library never reads it again to list it
        self.assertEqual(self.store.cached_fp(path, st.st_size, st.st_mtime_ns)["fp"], fp)

    def test_a_mark_at_the_very_last_sample_is_kept(self):
        # M just before Stop: the mark ends at the last sample received, a time with many
        # decimals; rounding it to milliseconds must not put it past the end of the file.
        a = self.api()
        sid = self.start(a, rate=44100)["session"]
        frames = 88063                                    # 1.99689... s
        self.send(a, sid, tone(frames / 44100, rate=44100), 0)
        self.assertTrue(a.live_mark(sid, frames / 44100)["ok"])
        r = a.live_stop(sid)
        self.assertEqual((r["files"][0]["marks"], r["dropped_marks"]), (1, 0))
        m = self.store.marks(wavinfo.wav_fingerprint(os.path.join(self.lib, r["files"][0]["name"])))[0]
        self.assertLessEqual(m["end"], frames / 44100)

    def test_stop_opens_it_for_the_player_and_never_overwrites(self):
        a = self.api()
        fixed = __import__("datetime").datetime(2026, 1, 2, 3, 4, 5)
        os.makedirs(self.lib)
        open(os.path.join(self.lib, "Live 2026-01-02 03-04-05.wav"), "wb").close()
        with mock.patch("app.live.datetime") as dt:
            dt.datetime.now.return_value = fixed
            sid = self.start(a)["session"]
        self.send(a, sid, tone(1.0), 0)
        r = a.live_stop(sid)
        self.assertEqual([f["name"] for f in r["files"]], ["Live 2026-01-02 03-04-05 (2).wav"])
        self.assertTrue(r["player"]["ok"])
        self.assertEqual(r["player"]["name"], "Live 2026-01-02 03-04-05 (2).wav")
        self.assertEqual(os.path.getsize(os.path.join(self.lib, "Live 2026-01-02 03-04-05.wav")), 0)
        self.assertFalse(a.live_stop(sid)["ok"])

    def test_a_recording_that_cannot_be_opened_in_the_player_says_so(self):
        a = self.api()
        sid = self.start(a)["session"]
        self.send(a, sid, tone(1.0), 0)
        with mock.patch.object(self.server, "prepare_file", side_effect=OSError(28, "No space left on device")):
            r = a.live_stop(sid)
        self.assertNotIn("player", r)
        self.assertIn("No space left on device", r["player_error"])
        self.assertEqual(len(r["files"]), 1)                              # saved all the same
        self.assertTrue(os.path.isfile(os.path.join(self.lib, r["files"][0]["name"])))

    def test_into_a_library_folder_and_never_into_clips(self):
        a = self.api()
        os.makedirs(os.path.join(self.lib, "Old Mill", "Clips"))
        with open(os.path.join(self.lib, "Old Mill", "Clips", ".openevp-clips"), "wb") as f:
            f.write(b"x")
        listing = a.list_library()
        ids = {tuple(d["rel"]): d["id"] for d in listing["folders"]}
        r = a.live_start({"mode": "live", "folder": ids[("Old Mill", "Clips")], "rate": RATE, "channels": 1})
        self.assertFalse(r["ok"])
        self.assertIn("Clips", r["error"])
        sid = self.start(a, folder=ids[("Old Mill",)])["session"]
        self.send(a, sid, tone(0.5), 0)
        r = a.live_stop(sid)
        self.assertEqual(r["folder"], "Old Mill")
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Old Mill", r["files"][0]["name"])))
        self.assertFalse(a.live_start({"mode": "live", "folder": "nope", "rate": RATE, "channels": 1})["ok"])

    def import_stream(self):
        line = lambda s, seed: noise(s, db=-85, seed=seed)

        def rec(s, seed):                                 # room tone with a voice in the middle
            x = noise(s, db=-50, seed=seed)
            x[len(x) // 3:2 * len(x) // 3] += tone(s, db=-15)[:2 * len(x) // 3 - len(x) // 3]
            return x
        return np.concatenate([line(2, 1), rec(4, 2), line(5, 3), rec(3, 4), line(1, 5)])

    def record_import(self, a, stream, split=3.0, marks=()):
        with mock.patch("app.live.datetime") as dt:
            dt.datetime.now.return_value = __import__("datetime").datetime(2026, 10, 3, 9, 0, 0)
            r = self.start(a, mode="import", split=split)
        sid = r["session"]
        step = RATE // 2
        for seq, i in enumerate(range(0, len(stream), step)):
            self.send(a, sid, stream[i:i + step], seq)
            for at in marks:
                if i < at * RATE <= i + step:
                    self.assertTrue(a.live_mark(sid, at)["ok"])
        return r, a.live_stop(sid)

    def wait_event(self, names, timeout=30):
        with self.events.cond:
            ok = self.events.cond.wait_for(lambda: any(n in names for n, _ in self.events.items), timeout)
        self.assertTrue(ok, self.events.items)
        return next((n, p) for n, p in self.events.items if n in names)

    def test_an_import_is_one_file_then_split_into_one_file_per_recording(self):
        a = self.api()
        stream = self.import_stream()
        r, stop = self.record_import(a, stream, marks=(4.5, 9.5))
        self.assertEqual(r["file"], "Import 2026-10-03 09-00-00 (full).wav")    # recorded as one file
        self.assertEqual([f["name"] for f in stop["files"]], ["Import 2026-10-03 09-00-00 (full).wav"])
        self.assertIn("job", stop["split"])
        self.assertNotIn("player", stop)                 # an import does not open the player
        name, done = self.wait_event({"import-split-done", "import-split-failed"})
        self.assertEqual(name, "import-split-done", done)
        self.assertTrue(any(n == "import-split-progress" for n, _ in self.events.items))
        names = [f["name"] for f in done["files"]]
        self.assertEqual(names, ["Import 2026-10-03 09-00-00 (1).wav", "Import 2026-10-03 09-00-00 (2).wav"])
        self.assertEqual([f["marks"] for f in done["files"]], [2, 0])
        # The whole file is kept, and the pieces put together are exactly it.
        audio = {}
        for n in names + ["Import 2026-10-03 09-00-00 (full).wav"]:
            with wave.open(os.path.join(self.lib, n)) as w:
                audio[n] = w.readframes(w.getnframes())
        self.assertEqual(audio[names[0]] + audio[names[1]], audio["Import 2026-10-03 09-00-00 (full).wav"])
        self.assertEqual(audio["Import 2026-10-03 09-00-00 (full).wav"], pcm(stream))
        self.assertAlmostEqual(len(audio[names[0]]) / 2 / RATE, 2 + 4 + 5 - 0.5, delta=0.001)
        # Marks: the whole file has both; each piece the ones that end in it, at its own times.
        first = self.store.marks(wavinfo.wav_fingerprint(os.path.join(self.lib, names[0])))
        self.assertEqual([m["end"] for m in first], [4.5, 9.5])
        whole = self.store.marks(wavinfo.wav_fingerprint(os.path.join(self.lib, "Import 2026-10-03 09-00-00 (full).wav")))
        self.assertEqual([m["end"] for m in whole], [4.5, 9.5])
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])
        self.assertFalse(a.splitting())

    def test_marks_go_to_the_piece_they_end_in(self):
        a = self.api()
        r, stop = self.record_import(a, self.import_stream(), marks=(12.5,))
        name, done = self.wait_event({"import-split-done", "import-split-failed"})
        second = self.store.marks(wavinfo.wav_fingerprint(os.path.join(self.lib, done["files"][1]["name"])))
        self.assertEqual([(m["start"], m["end"]) for m in second], [(0.0, 2.0)])    # the piece starts at 10.5 s

    def test_an_import_without_the_split_is_one_plain_file(self):
        a = self.api()
        r, stop = self.record_import(a, self.import_stream(), split=0)
        self.assertEqual(r["file"], "Import 2026-10-03 09-00-00.wav")
        self.assertNotIn("split", stop)

    def test_an_import_with_no_gaps_stays_one_file_and_says_so(self):
        a = self.api()
        r, stop = self.record_import(a, noise(6, db=-85))
        name, done = self.wait_event({"import-split-done", "import-split-failed"})
        self.assertEqual((name, done["files"]), ("import-split-done", []))
        self.assertEqual([n for n in os.listdir(self.lib) if n.endswith(".wav")], ["Import 2026-10-03 09-00-00 (full).wav"])

    def test_cancelling_the_split_keeps_the_whole_file_and_nothing_else(self):
        import threading
        a = self.api()
        go, real_plan = threading.Event(), live.silence.plan

        def slow_plan(path, gap, should_stop=None, progress=None):
            self.assertTrue(go.wait(10))
            return real_plan(path, gap, should_stop, progress)
        with mock.patch.object(live.silence, "plan", slow_plan):
            r, stop = self.record_import(a, self.import_stream())
            self.assertTrue(a.splitting())
            # While it splits, Record and folder operations wait.
            self.assertEqual(a.live_start({"mode": "live", "folder": "root", "rate": RATE, "channels": 1})["error"],
                             live.SPLITTING)
            self.assertTrue(a.cancel_import_split(stop["split"]["job"])["ok"])
            go.set()
            name, done = self.wait_event({"import-split-done", "import-split-failed"})
        self.assertEqual((name, done["cancelled"]), ("import-split-failed", True))
        self.assertIn("It is kept as one file", done["error"])
        self.assertEqual(sorted(os.listdir(self.lib)), ["Import 2026-10-03 09-00-00 (full).wav"])
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])
        self.assertFalse(a.splitting())

    def test_a_split_whose_folder_moved_is_not_made(self):
        a = self.api()
        with mock.patch("app.live._root_identity", side_effect=lambda root: getattr(self, "_root", None) or
                        __import__("app.library_ops", fromlist=["_root_identity"])._root_identity(root)):
            r, stop = self.record_import(a, self.import_stream())
            self._root = ("elsewhere", False)
            name, done = self.wait_event({"import-split-done", "import-split-failed"})
        self.assertEqual(name, "import-split-failed")
        self.assertIn("no longer where it was", done["error"])
        self.assertEqual([n for n in os.listdir(self.lib) if n.endswith(".wav")], ["Import 2026-10-03 09-00-00 (full).wav"])

    def test_pieces_a_crash_left_half_written_are_deleted_at_the_next_start(self):
        a = self.api()
        os.makedirs(self.lib)
        part = os.path.join(self.lib, "Import x (1).wav.part")
        with open(part, "wb") as f:
            f.write(livewav.header(RATE, 1, 2 * RATE) + pcm(tone(1.0)))
        with open(part + ".json", "w", encoding="utf-8") as f:
            json.dump({"name": "Import x (1).wav", "rate": RATE, "channels": 1, "derived": True, "marks": []}, f)
        self.store.set_setting(live.PARTS_SETTING, [part])
        self.assertEqual(a.live_recover(), {"ok": True, "recovered": [], "failed": []})
        self.assertEqual(os.listdir(self.lib), [])

    @unittest.skipUnless(sys.platform == "win32", "folders are held open only on Windows")
    def test_the_destination_cannot_be_renamed_or_swapped_while_recording(self):
        a = self.api()
        os.makedirs(os.path.join(self.lib, "Night 1"))
        fid = next(d["id"] for d in a.list_library()["folders"] if d["rel"] == ["Night 1"])
        sid = self.start(a, folder=fid)["session"]
        self.send(a, sid, tone(0.5), 0)
        for src in (os.path.join(self.lib, "Night 1"), self.lib):
            with self.assertRaises(PermissionError):
                os.rename(src, src + " moved")
        a.live_stop(sid)
        os.rename(os.path.join(self.lib, "Night 1"), os.path.join(self.lib, "Night 2"))   # let go after Stop

    def test_a_library_folder_swapped_since_it_was_listed_is_refused(self):
        a = self.api()
        os.makedirs(self.lib)
        a.list_library()
        with mock.patch("app.live._root_identity", return_value=("elsewhere", False)), \
                mock.patch.object(type(a), "_root_moved", return_value=True):
            r = a.live_start({"mode": "live", "folder": "root", "rate": RATE, "channels": 1})
        self.assertFalse(r["ok"])

    def test_the_disk_filling_up_stops_and_saves(self):
        a = self.api()
        free = collections.namedtuple("usage", "total used free")
        with mock.patch("app.live.shutil.disk_usage", return_value=free(0, 0, live.RESERVE_BYTES + (10 << 20))):
            sid = self.start(a)["session"]
            r = self.send(a, sid, tone(0.5), 0)
            self.assertIn("minute", r["warning"])
        with mock.patch("app.live.shutil.disk_usage", return_value=free(0, 0, live.RESERVE_BYTES + 100)):
            r = a.live_chunk(sid, 1, base64.b64encode(pcm(tone(0.5))).decode())
        self.assertEqual(r["stopped"], live.STOPPED_DISK)
        self.assertEqual(len(r["result"]["files"]), 1)
        self.assertFalse(a.recording())
        with mock.patch("app.live.shutil.disk_usage", return_value=free(0, 0, live.RESERVE_BYTES)):
            r = a.live_start({"mode": "live", "folder": "root", "rate": RATE, "channels": 1})
        self.assertEqual(r["error"], live.NO_SPACE)

    def test_a_file_reaching_4_gb_stops_and_is_saved(self):
        a = self.api()
        with mock.patch.object(livewav, "MAX_DATA", 3 * RATE * 2):        # 3 s of 8 kHz mono stands in for 4 GB
            sid = self.start(a)["session"]
            self.send(a, sid, tone(2.0), 0)
            r = a.live_chunk(sid, 1, base64.b64encode(pcm(tone(2.0))).decode())
        self.assertEqual(r["stopped"], live.STOPPED_SIZE)
        f = r["result"]["files"][0]
        self.assertEqual(f["seconds"], 2.0)                               # the chunk that did not fit is left out
        self.assertFalse(a.recording())

    def test_a_second_window_cannot_record_and_bad_settings_are_refused(self):
        a = self.api()
        for bad in ({"mode": "x", "rate": RATE, "channels": 1}, {"mode": "live", "rate": 100, "channels": 1},
                    {"mode": "live", "rate": RATE, "channels": 3}, {"mode": "import", "rate": RATE, "channels": 1,
                                                                    "split": 99}, None):
            self.assertFalse(a.live_start(bad)["ok"], bad)
        second = AppData(os.path.join(self.tmp, "appdata"))          # another window holds the store
        self.addCleanup(second.close)
        self.assertTrue(second.read_only)
        b = backend.Api(None, self.events, lambda s: None, self.lib, self.server, store=second)
        self.addCleanup(b.shutdown)
        r = b.live_start({"mode": "live", "rate": RATE, "channels": 1})
        self.assertFalse(r["ok"])
        self.assertIn(live.NO_STORE, r["error"])
        self.assertEqual(b.live_recover()["recovered"], [])

    def test_folder_operations_and_updates_wait_for_the_recording(self):
        a = self.api()
        os.makedirs(os.path.join(self.lib, "x"))
        listing = a.list_library()
        fid = next(d["id"] for d in listing["folders"] if d["rel"] == ["x"])
        sid = self.start(a)["session"]
        self.assertEqual(a.rename_folder(fid, "y")["error"], live.RECORDING)
        self.assertEqual(_close_question(False, False, recording=a.recording())[0], "Recording in progress")
        a.live_stop(sid)
        self.assertTrue(a.rename_folder(fid, "y")["ok"])

    def updater_api(self):
        """An Api that can install updates, with a download that waits until released."""
        import threading
        started, release = threading.Event(), threading.Event()

        class Updater:
            def download(self, info, progress=None, cancelled=None):
                started.set()
                release.wait(10)
                raise OSError("stopped by the test")

            def discard(self, path):
                pass
        a = backend.Api(None, self.events, lambda s: None, self.lib, self.server, store=self.store,
                        updater=Updater(), can_install=True)
        self.addCleanup(a.shutdown)
        a._update = {"version": "9.9.9"}
        return a, started, release

    def test_an_update_and_a_recording_never_both_start(self):
        import threading
        a, started, release = self.updater_api()
        # An update is admitted first: Record is refused while it runs, allowed after.
        t = threading.Thread(target=a.install_update)
        t.start()
        self.assertTrue(started.wait(10))
        r = a.live_start({"mode": "live", "folder": "root", "rate": RATE, "channels": 1})
        self.assertEqual(r["error"], live.UPDATING)
        release.set()
        t.join(10)
        sid = self.start(a)["session"]
        # A recording first: the update is refused.
        self.assertEqual(a.install_update()["error"], backend.UPDATE_RECORDING)
        a.live_stop(sid)

    def test_finish_recording_saves_what_has_arrived(self):
        a = self.api()
        self.assertEqual(a.finish_recording(), {"ok": True, "finished": False})
        sid = self.start(a)["session"]
        self.send(a, sid, tone(1.0), 0)
        self.assertEqual(a.finish_recording(), {"ok": True, "finished": True})
        self.assertFalse(a.recording())
        self.assertEqual(len([n for n in os.listdir(self.lib) if n.endswith(".wav")]), 1)

    def test_closing_does_not_wait_forever_for_a_stuck_recording(self):
        # A chunk write stuck on the disk holds the recording's lock: shutdown gives up after a
        # while and the app closes; the .part is finished at the next start.
        import time as clock
        a = backend.Api(None, self.events, lambda s: None, self.lib, self.server, store=self.store)
        sid = self.start(a)["session"]
        self.send(a, sid, tone(1.0), 0)
        part = a._live.piece.part
        a._live_lock.acquire()
        try:
            with mock.patch.object(live, "SHUTDOWN_WAIT", 0.2):
                t0 = clock.monotonic()
                a.shutdown()
                self.assertLess(clock.monotonic() - t0, 3)
        finally:
            a._live_lock.release()
        a._live.piece.writer._f.close()
        a._live.pins.close()
        self.assertTrue(os.path.isfile(part))
        self.assertIn(part, self.store.get_setting(live.PARTS_SETTING))

    def test_closing_the_app_saves_the_recording(self):
        a = backend.Api(None, self.events, lambda s: None, self.lib, self.server, store=self.store)
        sid = self.start(a)["session"]
        self.send(a, sid, tone(1.0), 0)
        a.live_mark(sid, 0.9)
        a.shutdown()
        names = [n for n in os.listdir(self.lib)]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".wav"))

    def test_a_session_nobody_feeds_is_finished_by_the_backend(self):
        a = self.api()
        with mock.patch.object(live, "LIVE_IDLE", 0.3), mock.patch.object(live, "LIVE_WATCH_EVERY", 0.05):
            sid = self.start(a)["session"]
            self.send(a, sid, tone(1.0), 0)
            s = a._live
            self.assertTrue(s.closed.wait(5))
        self.assertFalse(a.recording())
        self.assertEqual(s.stopped, live.STOPPED_IDLE)
        self.assertEqual(len([n for n in os.listdir(self.lib) if n.endswith(".wav")]), 1)
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])
        self.assertEqual(a.live_chunk(sid, 1, base64.b64encode(pcm(tone(0.5))).decode())["error"], live.NOT_RECORDING)

    def test_a_crash_leaves_a_part_that_the_next_start_finishes(self):
        a = self.api()
        with mock.patch("app.live.datetime") as dt:
            dt.datetime.now.return_value = __import__("datetime").datetime(2026, 5, 6, 7, 8, 9)
            sid = self.start(a)["session"]
        x = tone(12.0)                                    # past HEADER_EVERY: the header is up to date on disk
        for i in range(12):
            self.send(a, sid, x[i * RATE:(i + 1) * RATE], i)
        a.live_mark(sid, 11.0)
        # The crash: the process dies with the file open (simulated: closed without finishing).
        s = a._live
        s.piece.writer._f.close()
        s.pins.close()                                    # a dead process holds nothing
        a._live = None
        part = s.piece.part
        with wave.open(part) as w:                        # already playable as it is
            self.assertGreaterEqual(w.getnframes(), 10 * RATE)
        # The next start: a new Api on the same store finishes it.
        b = self.api()
        r = b.live_recover()
        self.assertEqual([f["name"] for f in r["recovered"]], ["Live 2026-05-06 07-08-09.wav"])
        self.assertEqual(r["recovered"][0]["marks"], 1)
        path = os.path.join(self.lib, "Live 2026-05-06 07-08-09.wav")
        with wave.open(path) as w:
            self.assertEqual(w.getnframes(), 12 * RATE)
        self.assertEqual(len(self.store.marks(wavinfo.wav_fingerprint(path))), 1)
        self.assertFalse(os.path.exists(part) or os.path.exists(part + ".json"))
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])
        self.assertEqual(b.live_recover()["recovered"], [])

    def test_a_crash_between_the_rename_and_the_marks_still_stores_them(self):
        a = self.api()
        with mock.patch("app.live.datetime") as dt:
            dt.datetime.now.return_value = __import__("datetime").datetime(2026, 5, 6, 7, 8, 9)
            sid = self.start(a)["session"]
        self.send(a, sid, tone(3.0), 0)
        a.live_mark(sid, 2.5)
        # The crash: right after the rename, before the marks reach the store.
        with mock.patch.object(type(a), "_store_live_marks", side_effect=SystemExit("power cut")):
            with self.assertRaises(SystemExit):
                a._live_finish(a._live)
        a._live = None
        path = os.path.join(self.lib, "Live 2026-05-06 07-08-09.wav")
        part = path + ".part"
        self.assertTrue(os.path.isfile(path) and not os.path.exists(part))
        self.assertTrue(os.path.isfile(part + ".json"))                  # the journal survived
        fp = wavinfo.wav_fingerprint(path)
        self.assertEqual(self.store.marks(fp), [])
        b = self.api()
        r = b.live_recover()
        self.assertEqual([(f["name"], f["marks"]) for f in r["recovered"]], [("Live 2026-05-06 07-08-09.wav", 1)])
        self.assertEqual(len(self.store.marks(fp)), 1)
        self.assertFalse(os.path.exists(part + ".json"))
        # Run again (say the crash came after some marks were stored): nothing is doubled.
        meta = {"name": "Live 2026-05-06 07-08-09.wav", "rate": RATE, "channels": 1, "published": path, "fp": fp,
                "frames": 3 * RATE, "marks": [{"start": 0.5, "end": 2.5, "cls": "C", "note": live.MARK_NOTE}]}
        with open(part + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f)
        self.store.set_setting(live.PARTS_SETTING, [part])
        b.live_recover()
        self.assertEqual(len(self.store.marks(fp)), 1)

    def test_a_journal_pointing_at_another_file_is_not_trusted(self):
        a = self.api()
        os.makedirs(self.lib)
        path = os.path.join(self.lib, "Live x.wav")
        with open(path, "wb") as f:
            f.write(livewav.header(RATE, 1, 2 * RATE) + pcm(tone(1.0)))
        part = path + ".part"
        meta = {"name": "Live x.wav", "rate": RATE, "channels": 1, "published": path, "fp": "0" * 64,
                "frames": RATE, "marks": [{"start": 0.0, "end": 0.5, "cls": "C", "note": "n"}]}
        with open(part + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f)
        self.store.set_setting(live.PARTS_SETTING, [part])
        self.assertEqual(a.live_recover()["recovered"], [])
        self.assertEqual(self.store.marks(wavinfo.wav_fingerprint(path)), [])

    def test_recovery_never_touches_the_file_being_recorded(self):
        a = self.api()
        sid = self.start(a)["session"]
        self.send(a, sid, tone(1.0), 0)
        self.assertEqual(a.live_recover(), {"ok": True, "recovered": [], "failed": []})
        self.assertTrue(a.recording())
        a.live_stop(sid)

    def test_recovery_drops_an_empty_part_and_a_missing_one(self):
        a = self.api()
        os.makedirs(self.lib)
        empty = os.path.join(self.lib, "Live e.wav.part")
        with open(empty, "wb") as f:
            f.write(livewav.header(RATE, 1, 0))
        self.store.set_setting(live.PARTS_SETTING, [empty, os.path.join(self.lib, "gone.wav.part")])
        r = a.live_recover()
        self.assertEqual((r["recovered"], r["failed"]), ([], []))
        self.assertFalse(os.path.exists(empty))
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])

    def test_an_unreadable_part_is_kept_never_deleted(self):
        # The header is damaged and there is no sidecar: the bytes cannot be read as audio,
        # but they may be recoverable by hand, so the file is kept under a name that says so.
        a = self.api()
        os.makedirs(self.lib)
        part = os.path.join(self.lib, "Live 2026-01-01 00-00-00.wav.part")
        audio = pcm(tone(1.0))
        with open(part, "wb") as f:
            f.write(b"garbage-not-a-header".ljust(44, bytes(1)) + audio)
        self.store.set_setting(live.PARTS_SETTING, [part])
        r = a.live_recover()
        self.assertEqual(r["recovered"], [])
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("kept as Live 2026-01-01 00-00-00 (unrecovered).raw", r["failed"][0])
        kept = os.path.join(self.lib, "Live 2026-01-01 00-00-00 (unrecovered).raw")
        with open(kept, "rb") as f:
            self.assertEqual(f.read()[44:], audio)                     # every byte kept
        self.assertFalse(os.path.exists(part))
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])

    def test_an_unreadable_part_with_no_bytes_after_the_header_is_dropped(self):
        a = self.api()
        os.makedirs(self.lib)
        part = os.path.join(self.lib, "Live z.wav.part")
        with open(part, "wb") as f:
            f.write(b"x" * 44)
        self.store.set_setting(live.PARTS_SETTING, [part])
        self.assertEqual(a.live_recover(), {"ok": True, "recovered": [], "failed": []})
        self.assertEqual(os.listdir(self.lib), [])

    def test_settings_are_remembered(self):
        a = self.api()
        self.assertEqual((a.live_settings()["split"], a.live_settings()["input"]), (3.0, None))
        r = a.set_live_settings({"input": {"id": "abc", "label": "USB Audio (Line)"}, "split": 5, "import": True})
        self.assertTrue(r["ok"])
        b = self.api()
        got = b.live_settings()
        self.assertEqual((got["input"], got["split"], got["import"]), ({"id": "abc", "label": "USB Audio (Line)"}, 5, True))
        self.assertTrue(b.set_live_settings({"split": 0})["ok"])
        self.assertEqual(b.live_settings()["split"], 0)
        for bad in ({"split": 0.1}, {"split": True}, {"input": {"id": 1}}, {"import": "yes"}, {"other": 1}, []):
            self.assertFalse(b.set_live_settings(bad)["ok"], bad)


class LivePlaybackTests(Tmp):
    """A finished Live recording opens in the player through the real audio server: served
    in place (its file identity checked on every open), or, on a file system with no file
    identity (FAT/exFAT sticks), from a private copy in the cache. Either way the player gets
    the file's exact bytes and the fingerprint the marks were stored under."""

    def setUp(self):
        super().setUp()
        import urllib.request
        from app.audio_server import AudioServer
        self.urlopen = urllib.request.urlopen
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)
        self.server = AudioServer(lambda key: None, self.cache)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.store = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(self.store.close)
        self.lib = os.path.join(self.tmp, "OpenEVP")
        self.api = backend.Api(None, Events(), lambda s: None, self.lib, self.server, store=self.store)
        self.addCleanup(self.api.shutdown)

    def record(self):
        sid = self.api.live_start({"mode": "live", "folder": "root", "rate": RATE, "channels": 2})["session"]
        x = tone(2.0, channels=2)
        for i in range(4):
            self.assertTrue(self.api.live_chunk(sid, i, base64.b64encode(pcm(x[i * RATE // 2:(i + 1) * RATE // 2])).decode())["ok"])
        self.api.live_mark(sid, 1.5)
        r = self.api.live_stop(sid)
        path = os.path.join(self.lib, r["files"][0]["name"])
        with open(path, "rb") as f:
            return r, f.read(), wavinfo.wav_fingerprint(path)

    def check(self, r, data, fp):
        player = r["player"]
        self.assertTrue(player["ok"], player)
        self.assertEqual((player["fp"], player["duration"], player["rate"], player["channels"]), (fp, 2.0, RATE, 2))
        self.assertEqual(len(player["marks"]), 1)                       # the mark made while recording
        with self.urlopen(player["url"], timeout=5) as resp:
            self.assertEqual(resp.read(), data)
        self.assertTrue(self.api.get_marks(player["rec"])["ok"])

    def test_served_in_place_with_its_identity(self):
        r, data, fp = self.record()
        self.check(r, data, fp)
        self.assertEqual(os.listdir(self.cache), [n for n in os.listdir(self.cache) if n.startswith(".")], "nothing copied")

    def test_served_from_a_private_copy_without_identity(self):
        from app import audio_server
        with mock.patch.object(audio_server, "_ident_of", return_value=None):
            r, data, fp = self.record()
        self.check(r, data, fp)
        self.assertTrue([n for n in os.listdir(self.cache) if not n.startswith(".")], "a copy in the cache")


class CloseDrainTests(unittest.TestCase):
    """Closing during a recording: the window waits (bounded) for the page to save it."""

    class Window:
        def __init__(self, answers):
            self.answers, self.js, self.destroyed = list(answers), [], False

        def evaluate_js(self, js):
            self.js.append(js)
            return self.answers.pop(0) if "__liveDrained" in js and self.answers else ("true" if "liveDrain" in js else None)

        def destroy(self):
            self.destroyed = True

    def test_waits_for_the_page_then_closes(self):
        from app import main
        w, order = self.Window([False, False, True]), []
        main._close_after_drain(w, lambda: order.append("before_close"), sleep=lambda s: None)
        self.assertEqual(w.js[0], main._DRAIN_START_JS)
        self.assertEqual(w.js.count(main._DRAIN_DONE_JS), 3)
        self.assertEqual((order, w.destroyed), (["before_close"], True))

    def test_a_page_that_never_finishes_is_waited_for_only_so_long(self):
        from app import main
        t = [0.0]

        def sleep(s):
            t[0] += s
        w = self.Window([])
        main._close_after_drain(w, lambda: None, timeout=3, poll=1, clock=lambda: t[0], sleep=sleep)
        self.assertTrue(w.destroyed)
        self.assertLessEqual(t[0], 4)

    def test_a_hung_page_cannot_hold_the_close_past_the_deadline(self):
        # evaluate_js waits for the page without a time limit; a hung page never answers.
        import threading
        import time as clock
        from app import main
        never = threading.Event()

        class Hung(self.Window):
            def evaluate_js(self, js):
                never.wait()
        w, order = Hung([]), []
        t0 = clock.monotonic()
        main._close_after_drain(w, lambda: order.append("close"), finalize=lambda: order.append("finalize"), timeout=0.3)
        self.assertLess(clock.monotonic() - t0, 3)
        self.assertEqual((order, w.destroyed), (["finalize", "close"], True))
        never.set()

    def test_a_finalize_that_hangs_cannot_hold_the_close_either(self):
        # The page did not finish, and the backend's own finish is stuck (waiting for the
        # recording's lock, or on the disk): the window closes at the deadline all the same.
        import threading
        import time as clock
        from app import main
        never = threading.Event()

        class Hung(self.Window):
            def evaluate_js(self, js):
                never.wait()
        w, order = Hung([]), []

        def stuck():
            order.append("finalize")
            never.wait()
        t0 = clock.monotonic()
        main._close_after_drain(w, lambda: order.append("close"), finalize=stuck, timeout=0.4)
        took = clock.monotonic() - t0
        self.assertLess(took, 2, took)
        self.assertEqual((order, w.destroyed), (["finalize", "close"], True))
        never.set()

    def test_a_page_that_finishes_needs_no_finalize(self):
        from app import main
        w, order = self.Window([True]), []
        main._close_after_drain(w, lambda: order.append("close"), finalize=lambda: order.append("finalize"),
                                sleep=lambda s: None)
        self.assertEqual(order, ["close"])

    def test_a_page_that_is_gone_still_closes(self):
        from app import main

        class Gone(self.Window):
            def evaluate_js(self, js):
                raise RuntimeError("no page")
        w = Gone([])
        main._close_after_drain(w, lambda: None)
        self.assertTrue(w.destroyed)


class SmokeLiveTests(Tmp):
    """OpenEVP.exe --smoke records a moment from Chromium's fake input through the page (the
    page part runs in the real WebView2 only; here the window is a stand-in)."""

    def window(self, answer):
        class W:
            def evaluate_js(self, js):
                return answer if "__openevpLive" in js and "liveSmoke" not in js else "true"
        return W()

    def saved(self, name, seconds=2.0, rate=44100, channels=2):
        os.makedirs(os.path.join(self.tmp, "save"), exist_ok=True)
        with wave.open(os.path.join(self.tmp, "save", name), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(int(seconds * rate) * 2 * channels))

    def test_a_good_recording_passes_and_a_bad_one_is_reported(self):
        from app import main
        self.saved("Live a.wav")
        good = {"ok": True, "rate": 44100, "channels": 2, "mark": True, "label": "Fake Default Audio Input",
                "files": [{"name": "Live a.wav", "marks": 1}]}
        report = {"problems": []}
        main._smoke_live(self.window(json.dumps(good)), report, self.tmp)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["live"]["wav"]["frames"], 88200)
        for bad, words in (({**good, "ok": False, "error": "NotAllowedError"}, "NotAllowedError"),
                           ({**good, "mark": False}, "mark"),
                           ({**good, "rate": 48000}, "wrong"),
                           ({**good, "files": [{"name": "missing.wav", "marks": 1}]}, "could not be read")):
            report = {"problems": []}
            main._smoke_live(self.window(json.dumps(bad)), report, self.tmp)
            self.assertTrue(any(words in p for p in report["problems"]), (bad, report))

    def test_the_fake_input_is_used_only_by_the_smoke_test(self):
        from app import main
        seen = []
        with mock.patch.object(main, "_run_app", side_effect=lambda smoke: seen.append(
                os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"))),                 mock.patch.object(main, "_smoke_decoders"), mock.patch.object(main, "_smoke_mp3"),                 mock.patch.dict(os.environ, {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--mine"}):
            main._smoke_main(None)
            self.assertEqual(os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"], "--mine")
        self.assertEqual(seen, [main.SMOKE_BROWSER_ARGS])
        self.assertIn("--use-fake-device-for-media-stream", main.SMOKE_BROWSER_ARGS)
        self.assertNotIn("fake-ui", main.SMOKE_BROWSER_ARGS)      # the app's own permission handler must answer


class MicPermissionTests(unittest.TestCase):
    PAGE = "http://127.0.0.1:38685/index.html"

    def test_only_the_pinned_page_gets_the_microphone(self):
        p = mic_permission.Policy(self.PAGE)
        self.assertEqual(p.decide("Microphone", "http://127.0.0.1:38685/", self.PAGE), "allow")
        for other in ("http://127.0.0.1:9999/", "https://127.0.0.1:38685/", "http://evil.example:38685/",
                      "http://localhost:38685/", "file:///C:/x.html", "nonsense"):
            self.assertEqual(p.decide("Microphone", other, self.PAGE), "deny", other)
        self.assertIsNone(p.decide("Camera", "http://127.0.0.1:38685/", self.PAGE))

    def test_trust_does_not_follow_navigation(self):
        # The window now shows another local server (or another page of the app's own
        # server): it asks for itself, with the window's own address. Denied: the trust
        # belongs to the page pinned at start.
        p = mic_permission.Policy(self.PAGE)
        self.assertEqual(p.decide("Microphone", "http://127.0.0.1:9999/", "http://127.0.0.1:9999/index.html"), "deny")
        self.assertEqual(p.decide("Microphone", "http://127.0.0.1:38685/", "http://127.0.0.1:38685/other.html"), "deny")
        self.assertFalse(p.may_navigate("http://127.0.0.1:9999/index.html"))
        self.assertFalse(p.may_navigate("https://example.com/"))
        self.assertTrue(p.may_navigate("http://127.0.0.1:38685/index.html"))

    def test_frames_and_unpinnable_pages_are_denied(self):
        p = mic_permission.Policy(self.PAGE)
        self.assertEqual(p.decide("Microphone", "http://127.0.0.1:38685/", self.PAGE, from_frame=True), "deny")
        for page in (None, "http://192.168.1.5:80/index.html", "https://127.0.0.1:1/x", "file:///C:/x.html"):
            q = mic_permission.Policy(page)
            self.assertIsNone(q.origin)
            self.assertEqual(q.decide("Microphone", "http://127.0.0.1:38685/", self.PAGE), "deny", page)
            self.assertFalse(q.may_navigate(self.PAGE))


@unittest.skipUnless(os.environ.get("OPENEVP_TEST_WEBVIEW2") == "1" and sys.platform == "win32",
                     "set OPENEVP_TEST_WEBVIEW2=1 to check the microphone handlers in a hidden WebView2 window "
                     "(Chromium's fake audio input only)")
class MicPermissionWebView2Tests(unittest.TestCase):
    def test_in_a_real_webview2(self):
        import subprocess
        run = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "webview2_mic_probe.py")],
                             capture_output=True, text=True, timeout=120)
        got = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(got, {"policy": True, "pinned": True, "top": "allowed", "frame": "NotAllowedError",
                               "navigation": "cancelled", "same_origin_page": "NotAllowedError"})


if __name__ == "__main__":
    unittest.main()
