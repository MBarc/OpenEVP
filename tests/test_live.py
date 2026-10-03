"""Live mode and analog import: the WAV written while recording (openevp.livewav),
splitting an import on silence (openevp.silence), and the backend's recording
calls (app/live.py): placement and names in the library, marks stored against
the finished file's fingerprint, the disk-space stop, and finishing the .part
files a crash left behind."""
import base64
import collections
import json
import os
import shutil
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

class Sink:
    def __init__(self):
        self.pieces = []                                  # [start frame, bytes, how it ended]

    def start(self, at):
        assert not self.pieces or self.pieces[-1][2] == "end", "a piece started before the last one ended"
        self.pieces.append([at, bytearray(), None])

    def write(self, data):
        self.pieces[-1][1] += data

    def end(self):
        self.pieces[-1][2] = "end"

    def seconds(self, channels=1):
        return [round(len(p[1]) / 2 / channels / RATE, 2) for p in self.pieces]

    def joined(self):
        return b"".join(bytes(p[1]) for p in self.pieces)


def run_split(parts, gap=3.0, chunk=0.37):
    """Feed the parts (arrays, one after the other) in odd-sized chunks; then finish."""
    sink = Sink()
    sp = silence.Splitter(RATE, 1, sink, gap=gap)
    data = pcm(np.concatenate(parts))
    step = int(chunk * RATE) * 2
    for i in range(0, len(data), step):
        sp.feed(data[i:i + step])
    sp.finish()
    return sink, sp, data


class SplitterTests(unittest.TestCase):
    def line(self, s, seed):                              # the cable's hiss while the recorder plays nothing
        return noise(s, db=-85, seed=seed)

    def recording(self, s, seed):                         # a recording: room tone with a voice in it
        x = noise(s, db=-50, seed=seed)
        a, b = int(0.3 * len(x)), int(0.6 * len(x))
        x[a:b] += tone(s, db=-15)[:b - a]
        return x

    def split(self, parts, gap=3.0):
        sink, sp, data = run_split(parts, gap)
        self.assertEqual(sink.joined(), data, "the pieces put back together are the input exactly")
        self.assertTrue(all(p[2] == "end" for p in sink.pieces))
        starts = [p[0] for p in sink.pieces]
        self.assertEqual(starts, [sum(len(q[1]) for q in sink.pieces[:i]) // 2 for i in range(len(starts))])
        return sink, sp

    def test_tones_with_gaps_become_one_file_each(self):
        sink, sp = self.split([self.line(2, 1), self.recording(5, 2), self.line(4, 3), self.recording(6, 4),
                               self.line(5, 5), self.recording(4, 6), self.line(1, 7)])
        self.assertEqual(len(sink.pieces), 3)
        # each new one starts half a second before its sound; the gap stays at the end of the one before
        self.assertAlmostEqual(sink.pieces[1][0] / RATE, 2 + 5 + 4 - 0.5, delta=0.06)
        self.assertAlmostEqual(sink.pieces[2][0] / RATE, 2 + 5 + 4 + 6 + 5 - 0.5, delta=0.06)

    def test_hiss_alone_is_one_file(self):
        sink, sp = self.split([self.line(20, 1)])
        self.assertEqual(len(sink.pieces), 1)

    def test_brief_pauses_never_split_a_recording(self):
        voice = lambda s, seed: noise(s, db=-50, seed=seed) + tone(s, db=-15)
        pause = lambda s, seed: noise(s, db=-50, seed=seed)          # the recording's own background
        sink, sp = self.split([self.line(2, 1), voice(2, 2), pause(2.5, 3), voice(1, 4), pause(2.9, 5),
                               voice(2, 6), self.line(4, 7)])
        self.assertEqual(len(sink.pieces), 1)

    def test_silent_pauses_shorter_than_the_gap_never_split(self):
        voice = lambda s: tone(s, db=-15) + noise(s, db=-50)
        sink, sp = self.split([self.line(2, 1), voice(2), self.line(2.5, 2), voice(2), self.line(5, 3)])
        self.assertEqual(len(sink.pieces), 1)

    def test_a_recording_no_louder_than_the_floor_is_never_split(self):
        # Record pressed after Play: the recording's background is the quietest thing heard,
        # so its pauses cannot be told from a gap; the piece stays whole.
        voice = lambda s: tone(s, db=-15) + noise(s, db=-50, seed=9)
        quiet = lambda s, seed: noise(s, db=-50, seed=seed)
        sink, sp = self.split([quiet(2, 1), voice(1.5), quiet(5, 2), voice(1.5), quiet(5, 3), voice(1.5)])
        self.assertEqual(len(sink.pieces), 1)

    def test_astras_case_steady_sound_then_the_same_room_tone_is_not_split(self):
        # 2 s of room tone, 4 s of steady sound, 4 s of that room tone again, more sound: the
        # piece shows no background of its own, so nothing says the pause is a gap.
        room = lambda s, seed: noise(s, db=-50, seed=seed)
        sound = lambda s: tone(s, db=-15)
        sink, sp = self.split([room(2, 1), sound(4), room(4, 2), sound(4), room(1, 3)])
        self.assertEqual(len(sink.pieces), 1)

    def test_play_before_record_splits_once_a_real_gap_was_heard(self):
        # The first recording's background was the floor at first; the gap after it is quieter
        # (the cable's hiss), so from there on the recordings split as usual.
        sink, sp = self.split([self.recording(6, 1), self.line(5, 2), self.recording(6, 3), self.line(5, 4),
                               self.recording(5, 5)])
        self.assertEqual(len(sink.pieces), 3)
        self.assertLess(sp.threshold(), -70)

    def test_a_click_is_not_a_file_of_its_own(self):
        click = tone(0.2, db=-10)
        sink, sp = self.split([self.line(2, 1), self.recording(5, 2), self.line(4, 3), click, self.line(4, 4),
                               self.recording(5, 5), self.line(1, 6)])
        self.assertEqual(len(sink.pieces), 2)                       # the click goes with the recording after it
        self.assertAlmostEqual(sink.pieces[1][0] / RATE, 2 + 5 + 4 - 0.5, delta=0.06)

    def test_stop_during_a_gap_keeps_it_in_the_last_piece(self):
        sink, sp = self.split([self.line(2, 1), self.recording(5, 2), self.line(6, 3)])
        self.assertEqual(len(sink.pieces), 1)

    def test_a_longer_gap_setting_keeps_it_whole(self):
        sink, _ = self.split([self.line(2, 1), self.recording(4, 2), self.line(4, 3), self.recording(4, 4)], gap=5.0)
        self.assertEqual(len(sink.pieces), 1)

    def test_stereo_and_the_floor_threshold(self):
        sink = Sink()
        sp = silence.Splitter(RATE, 2, sink, gap=1.0)
        self.assertIsNone(sp.threshold())
        sp.feed(pcm(noise(2, db=-85, channels=2)))
        self.assertAlmostEqual(sp.threshold(), -85 - 1 + silence.MARGIN_DB, delta=2.5)
        sp.feed(pcm(noise(4, db=-50, channels=2) + np.vstack([np.zeros((3 * RATE, 2)), tone(1, db=-12, channels=2)])))
        sp.feed(pcm(noise(2, db=-85, channels=2)))
        sp.feed(pcm(tone(2, db=-12, channels=2)))
        sp.feed(b"\1\0\2\0")                               # one frame short of a block: kept at Stop
        sp.finish()
        self.assertEqual(len(sink.pieces), 2)
        self.assertEqual(len(sink.joined()), (2 + 4 + 2 + 2) * RATE * 4 + 4)

    def test_digital_silence_is_not_taken_as_the_floor(self):
        sink = Sink()
        sp = silence.Splitter(RATE, 1, sink)
        sp.feed(bytes(2 * 2 * RATE))
        self.assertEqual(sp.threshold(), silence.MIN_FLOOR_DB + silence.MARGIN_DB)


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

    def test_import_splits_into_numbered_files_with_marks_in_the_right_one(self):
        a = self.api()
        with mock.patch("app.live.datetime") as dt:
            dt.datetime.now.return_value = __import__("datetime").datetime(2026, 10, 3, 9, 0, 0)
            sid = self.start(a, mode="import", split=3.0)["session"]
        line = lambda s, seed: noise(s, db=-85, seed=seed)

        def rec(s, seed):                                 # room tone with a voice in the middle
            x = noise(s, db=-50, seed=seed)
            x[len(x) // 3:2 * len(x) // 3] += tone(s, db=-15)[:2 * len(x) // 3 - len(x) // 3]
            return x
        stream = np.concatenate([line(2, 1), rec(4, 2), line(5, 3), rec(3, 4), line(1, 5)])
        step = RATE // 2
        seq = 0
        for i in range(0, len(stream), step):
            r = self.send(a, sid, stream[i:i + step], seq)
            seq += 1
            if i == 4 * RATE:                             # 4 s into the stream: inside the first recording
                self.assertEqual(r["file"], "Import 2026-10-03 09-00-00 (1).wav")
                m = a.live_mark(sid, 4.5)
                self.assertEqual(m["mark"]["file"], "Import 2026-10-03 09-00-00 (1).wav")
            if i == 9 * RATE:                             # in the gap: still the first file (the gap ends it)
                self.assertEqual(r["file"], "Import 2026-10-03 09-00-00 (1).wav")
                self.assertEqual(a.live_mark(sid, 9.5)["mark"]["file"], "Import 2026-10-03 09-00-00 (1).wav")
        r = a.live_stop(sid)
        names = [f["name"] for f in r["files"]]
        self.assertEqual(names, ["Import 2026-10-03 09-00-00 (1).wav", "Import 2026-10-03 09-00-00 (2).wav"])
        self.assertEqual([f["marks"] for f in r["files"]], [2, 0])
        self.assertEqual((r["whole"]["name"], r["whole"]["marks"]), ("Import 2026-10-03 09-00-00 (full).wav", 2))
        self.assertNotIn("player", r)                     # an import does not open the player
        # Nothing is lost: the pieces put together are the whole input, which is saved too.
        audio = []
        for n in names + [r["whole"]["name"]]:
            with wave.open(os.path.join(self.lib, n)) as w:
                audio.append(w.readframes(w.getnframes()))
        self.assertEqual(audio[0] + audio[1], audio[2])
        self.assertEqual(audio[2], pcm(stream))
        self.assertAlmostEqual(len(audio[0]) / 2 / RATE, 2 + 4 + 5 - 0.5, delta=0.06)
        first = os.path.join(self.lib, names[0])
        marks = self.store.marks(wavinfo.wav_fingerprint(first))
        self.assertEqual([m["end"] for m in marks], [4.5, 9.5])     # the first file starts with the input
        whole = self.store.marks(wavinfo.wav_fingerprint(os.path.join(self.lib, r["whole"]["name"])))
        self.assertEqual([m["end"] for m in whole], [4.5, 9.5])

    def test_an_import_that_never_splits_is_one_file_without_a_full_copy(self):
        a = self.api()
        sid = self.start(a, mode="import", split=3.0)["session"]
        self.send(a, sid, noise(4, db=-85), 0)
        r = a.live_stop(sid)
        self.assertEqual(len(r["files"]), 1)
        self.assertIsNone(r["whole"])
        self.assertEqual(len([n for n in os.listdir(self.lib) if n.endswith(".wav")]), 1)
        self.assertEqual(self.store.get_setting(live.PARTS_SETTING), [])

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

    def test_an_import_stops_before_a_new_file_if_the_folder_moved(self):
        a = self.api()
        sid = self.start(a, mode="import", split=3.0)["session"]
        line = lambda s, seed: noise(s, db=-85, seed=seed)

        def rec(s, seed):
            x = noise(s, db=-50, seed=seed)
            x[len(x) // 3:2 * len(x) // 3] += tone(s, db=-15)[:2 * len(x) // 3 - len(x) // 3]
            return x
        stream = np.concatenate([line(2, 1), rec(4, 2), line(5, 3), rec(3, 4)])
        step, seq, r = RATE // 2, 0, None
        for i in range(0, len(stream), step):
            if i == 11 * RATE:                            # the next piece is about to start: the root now resolves elsewhere
                patch = mock.patch("app.live._root_identity", return_value=("elsewhere", False))
                patch.start()
                self.addCleanup(patch.stop)
            r = a.live_chunk(sid, seq, base64.b64encode(pcm(stream[i:i + step])).decode())
            seq += 1
            if r.get("stopped"):
                break
        self.assertEqual(r["stopped"], live.STOPPED_MOVED)
        self.assertEqual([f["name"][-7:] for f in r["result"]["files"]], ["(1).wav"])   # the first piece is kept
        self.assertFalse(a.recording())

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

    def test_closing_the_app_saves_the_recording(self):
        a = backend.Api(None, self.events, lambda s: None, self.lib, self.server, store=self.store)
        sid = self.start(a)["session"]
        self.send(a, sid, tone(1.0), 0)
        a.live_mark(sid, 0.9)
        a.shutdown()
        names = [n for n in os.listdir(self.lib)]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".wav"))

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
    def test_only_the_apps_own_page_gets_the_microphone(self):
        page = "http://127.0.0.1:38685/index.html"
        self.assertEqual(mic_permission.decide("Microphone", "http://127.0.0.1:38685/", page), "allow")
        for other in ("http://127.0.0.1:9999/", "https://127.0.0.1:38685/", "http://evil.example:38685/",
                      "http://localhost:38685/", "file:///C:/x.html", "nonsense"):
            self.assertEqual(mic_permission.decide("Microphone", other, page), "deny", other)
        self.assertEqual(mic_permission.decide("Microphone", "http://192.168.1.5:80/", "http://192.168.1.5:80/"), "deny")
        self.assertIsNone(mic_permission.decide("Camera", "http://127.0.0.1:38685/", page))


if __name__ == "__main__":
    unittest.main()
