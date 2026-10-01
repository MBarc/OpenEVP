"""Exports at the player's speed: openevp.stretch (tape-style resampling and the
WSOLA time stretch that keeps the pitch), clips cut at a speed (openevp.clips),
and the backend's Export WAV with marks / Export clips / library clips job at a
speed: names ending in the speed, marks moved to match, identical bytes the
second time (so the identical-skip rule still applies)."""
import io
import os
import struct
import sys
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
import test_library as tl  # noqa: E402
import test_marks_api as tm  # noqa: E402
from app import backend, library_ops  # noqa: E402
from openevp import clips, mp3, stretch, wavinfo  # noqa: E402

WAIT = 60


def sine_wav(freq=440.0, seconds=1.0, rate=8000, channels=1, amp=0.5, width=2, freqs=None):
    """A WAV of a sine (channel c at freqs[c], if given), as 16-bit PCM (or width 1/3, or 4: float32)."""
    t = np.arange(int(round(seconds * rate))) / rate
    freqs = freqs or [freq] * channels
    x = np.stack([amp * np.sin(2 * np.pi * f * t) for f in freqs], 1)
    if width == 4:
        tag, raw = 3, x.astype("<f4").tobytes()
    elif width == 1:
        tag, raw = 1, (np.round(x * 127) + 128).astype(np.uint8).tobytes()
    elif width == 3:
        v = np.round(x * 8388607).astype(np.int32).reshape(-1)
        b = np.stack([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF], 1).astype(np.uint8)
        tag, raw = 1, b.tobytes()
    else:
        tag, raw = 1, np.round(x * 32767).astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * width, channels * width, 8 * width)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(raw)) + raw
    return b"RIFF" + struct.pack("<I", len(body)) + body


def samples(wav):
    """(rate, float samples (frames, channels)) of a WAV stretch wrote."""
    fmt, rate, align, at, n = clips._layout(memoryview(bytes(wav)))
    kind, width, channels = stretch._format(fmt)
    return rate, stretch._to_float(bytes(wav)[at:at + n // align * align], kind, width, channels).astype(np.float64)


def peak_freq(x, rate):
    """The strongest frequency of x (one channel), to a fraction of a bin (parabolic peak)."""
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    k = int(np.argmax(spec[1:])) + 1
    a, b, c = np.log(spec[k - 1:k + 2] + 1e-12)
    return (k + 0.5 * (a - c) / (a - 2 * b + c)) * rate / len(x)


class TapeTests(unittest.TestCase):
    def test_frequency_follows_speed_and_length_is_one_over_speed(self):
        wav = sine_wav(1000.0, seconds=1.0, rate=8000)
        for speed in backend.SPEEDS:
            if speed == 1:
                continue
            rate, y = samples(stretch.change_speed(wav, speed, keep_pitch=False))
            self.assertEqual(rate, 8000)                                       # written at the original rate
            self.assertEqual(len(y), round(8000 / speed), speed)
            if 1000.0 * speed < 3900:
                self.assertAlmostEqual(peak_freq(y[:, 0], rate), 1000.0 * speed, delta=1000.0 * speed * 0.01)

    def test_stereo_both_channels(self):
        wav = sine_wav(seconds=1.0, rate=44100, freqs=[300.0, 700.0], channels=2)
        rate, y = samples(stretch.change_speed(wav, 0.5, keep_pitch=False))
        self.assertEqual(y.shape, (88200, 2))
        self.assertAlmostEqual(peak_freq(y[:, 0], rate), 150.0, delta=1.5)
        self.assertAlmostEqual(peak_freq(y[:, 1], rate), 350.0, delta=3.5)


class WsolaTests(unittest.TestCase):
    def check(self, wav, rate, freqs, speed):
        out = stretch.change_speed(wav, speed, keep_pitch=True)
        r, y = samples(out)
        _r, x = samples(wav)
        self.assertEqual(r, rate)
        frame = 2 * round(stretch.FRAME * rate / 2)
        self.assertLessEqual(abs(len(y) - len(x) / speed), frame, speed)      # x1/speed, +-1 frame
        self.assertLessEqual(np.abs(y).max(), np.abs(x).max() + 1e-6)          # never louder: no clipping
        body = y[frame:-frame]                                                  # the edges fade
        for c, f in enumerate(freqs):
            self.assertAlmostEqual(peak_freq(body[:, c], rate), f, delta=f * 0.02, msg=(speed, c))
        return out

    def test_mono_keeps_the_frequency(self):
        wav = sine_wav(440.0, seconds=1.0, rate=8000, amp=0.99)
        for speed in (0.25, 0.5, 0.75, 1.25, 1.5, 2.0):
            self.check(wav, 8000, [440.0], speed)

    def test_stereo_in_lockstep(self):
        wav = sine_wav(seconds=1.0, rate=44100, freqs=[440.0, 660.0], channels=2, amp=0.9)
        for speed in (0.5, 2.0):
            self.check(wav, 44100, [440.0, 660.0], speed)
        same = sine_wav(523.0, seconds=0.5, rate=44100, channels=2)            # both channels alike: still alike
        _r, y = samples(stretch.change_speed(same, 0.5))
        self.assertTrue(np.array_equal(y[:, 0], y[:, 1]))

    def test_deterministic_and_unchanged_at_1x(self):
        wav = tm.wav_bytes(b"noise", seconds=2.0)
        a = stretch.change_speed(wav, 0.5)
        self.assertEqual(a, stretch.change_speed(wav, 0.5))
        self.assertEqual(stretch.change_speed(wav, 0.5, False), stretch.change_speed(wav, 0.5, False))
        _r, x = samples(wav)
        _r, y = samples(stretch.change_speed(wav, 1))
        self.assertTrue(np.array_equal(x, y))

    def test_sample_formats(self):
        for width in (1, 3, 4):
            wav = sine_wav(440.0, seconds=0.5, rate=8000, width=width, amp=0.8)
            out = stretch.change_speed(wav, 0.5)
            self.assertEqual(bytes(out[20:36]), wav[20:36], width)                  # the fmt chunk as it was
            r, y = samples(out)
            self.assertEqual(len(y), 8000)
            self.assertAlmostEqual(peak_freq(y[200:-200, 0], r), 440.0, delta=9)
        with self.assertRaises(ValueError):
            stretch.change_speed(b"RIFF junk", 0.5)

    def test_progress_and_stop(self):
        wav = sine_wav(seconds=2.0)
        seen = []
        stretch.change_speed(wav, 0.5, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[-1], (32000, 32000))
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))
        with self.assertRaises(stretch.Cancelled):
            stretch.change_speed(wav, 0.5, should_stop=lambda: True)
        with self.assertRaises(stretch.Cancelled):
            stretch.change_speed(wav, 0.5, keep_pitch=False, should_stop=lambda: True)


class ClipTests(unittest.TestCase):
    def test_names_end_in_the_speed(self):
        m = {"start": 12.4, "end": 13.0, "cls": "A", "note": "hi"}
        self.assertEqual(clips.name("x", m, fmt="mp3", speed=0.5), "x_EVP-A_00m12.4s_hi_0.5x.mp3")
        self.assertEqual(clips.name("x", m, speed=2.0), "x_EVP-A_00m12.4s_hi_2x.wav")
        self.assertEqual(clips.name("x", m, speed=1.0), "x_EVP-A_00m12.4s_hi.wav")
        self.assertEqual(stretch.suffix(0.25), "_0.25x")

    def test_a_clip_at_half_speed(self):
        wav = sine_wav(440.0, seconds=4.0)
        m = {"start": 1.0, "end": 1.5, "cls": "B", "note": "x"}
        plain = clips.cut(wav, m)
        for keep in (True, False):
            slow = clips.cut(wav, m, speed=0.5, keep_pitch=keep)
            with wave.open(io.BytesIO(plain)) as a, wave.open(io.BytesIO(slow)) as b:
                self.assertEqual(b.getframerate(), 8000)
                self.assertEqual(b.getnframes(), 2 * a.getnframes())               # the 0.5 s pads are stretched too
            [mk] = wavinfo.read_markers(io.BytesIO(slow))
            self.assertAlmostEqual(mk["start"], 1.0, places=3)                     # 0.5 s pad, at 0.5x: 1 s in
            self.assertAlmostEqual(mk["end"], 2.0, places=3)
            self.assertEqual(slow, clips.cut(wav, m, speed=0.5, keep_pitch=keep))  # deterministic


def scaled(marks, speed):
    return [(round(m["start"], 3), round(m["end"], 3)) for m in marks]


class ExportAtSpeedTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def picked(self, data):
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(data)
        api = self.new_api(pick_wav=lambda start: path)
        return api, api.open_wav()["rec"]

    def test_export_marked_at_half_speed(self):
        api, rec = self.picked(sine_wav(440.0, seconds=2.0))
        api.add_mark(rec, 0.2, 0.3, "C", "knock")
        api.add_mark(rec, 1.0, 1.1, "A", "")
        self.events.clear()
        r = api.export_marked(rec, 0.5, True)
        self.assertEqual((r["ok"], r["saved"], r["name"]), (True, True, "take_0.5x.wav"), r)
        out = os.path.join(self.dest, r["name"])
        with wave.open(out) as w:
            self.assertEqual((w.getframerate(), w.getnframes()), (8000, 32000))
        self.assertEqual(scaled(wavinfo.read_markers(out), 0.5), [(0.4, 0.6), (2.0, 2.2)])   # x2
        progress = [p for e, p in self.events if e == "speed-progress"]
        self.assertTrue(progress and progress[-1] == {"done": 32000, "total": 32000})
        self.assertEqual(api.export_marked(rec, 0.5, True)["already"], True)       # identical bytes: skipped
        r = api.export_marked(rec, 0.5, False)                                      # tape: other bytes, a new name
        self.assertEqual((r["saved"], r["name"]), (True, "take_0.5x (2).wav"))
        r = api.export_marked(rec, 2, False)
        self.assertEqual(r["name"], "take_2x.wav")
        self.assertEqual(scaled(wavinfo.read_markers(os.path.join(self.dest, r["name"])), 2), [(0.1, 0.15), (0.5, 0.55)])
        r = api.export_marked(rec)                                                  # 1x: as before
        self.assertEqual(r["name"], "take.wav")
        for bad in ((0.3, True), (1, "no"), (None, True)):
            self.assertEqual(api.export_marked(rec, *bad)["error"], backend.BAD_SPEED)

    def test_export_clips_at_a_speed(self):
        api, rec = self.picked(sine_wav(440.0, seconds=3.0))
        api.set_clip_format("wav")
        m = api.add_mark(rec, 1.0, 1.4, "B", "hello")["mark"]
        r = api.export_clips(rec, None, 0.75, True)
        self.assertEqual((r["ok"], r["saved"], r["names"]), (True, 1, ["take_EVP-B_00m01.0s_hello_0.75x.wav"]), r)
        path = os.path.join(r["folder"], r["names"][0])
        with wave.open(path) as w:
            self.assertEqual(w.getnframes(), round(1.4 * 8000 / 0.75))            # (0.4 s + 2 x 0.5 s pad) / 0.75
        [mk] = wavinfo.read_markers(path)
        self.assertAlmostEqual(mk["start"], 0.5 / 0.75, places=3)
        r = api.export_clips(rec, m["id"], 0.75, True)                              # Save clip: identical, skipped
        self.assertEqual((r["saved"], r["already"]), (0, 1))
        self.assertEqual(api.export_clips(rec, None, 5, True)["error"], backend.BAD_SPEED)

    @release_gate.require(mp3.available(), "lameenc is not installed (pip install -r requirements-app.txt)")
    def test_an_mp3_clip_at_half_speed(self):
        api, rec = self.picked(sine_wav(440.0, seconds=3.0))
        api.set_clip_format("mp3")
        api.add_mark(rec, 1.0, 1.4, "A", "")
        r = api.export_clips(rec, None, 0.5, False)
        self.assertEqual((r["saved"], r["names"]), (1, ["take_EVP-A_00m01.0s_0.5x.mp3"]), r)
        with open(os.path.join(r["folder"], r["names"][0]), "rb") as f:
            rate, _channels, seconds = mp3.info(f.read())
        self.assertEqual(rate, 8000)
        self.assertAlmostEqual(seconds, 2.8, delta=0.2)                             # 1.4 s at 0.5x (+ the encoder's frames)
        self.assertEqual(api.export_clips(rec, None, 0.5, False)["already"], 1)     # deterministic


class LibraryClipsAtSpeedTests(unittest.TestCase):
    setUp = tl.LibraryTests.setUp
    new_api = tl.LibraryTests.new_api
    write = tl.LibraryTests.write
    index = tl.LibraryTests.index

    def run_job(self, start):
        r = start()
        self.assertTrue(r["ok"], r)
        with self.events.cond:
            ok = self.events.cond.wait_for(
                lambda: any(n in ("clips-done", "clips-failed") and p["job"] == r["job"] for n, p in self.events.items),
                WAIT)
        self.assertTrue(ok, self.events.items)
        return next((n, p) for n, p in self.events.items if n in ("clips-done", "clips-failed") and p["job"] == r["job"])

    def test_folder_job_at_double_speed(self):
        one = sine_wav(440.0, seconds=2.0)
        self.write("Case/one.wav", one)
        self.store.add_mark(wavinfo.wav_fingerprint(io.BytesIO(one)), 0.5, 0.7, "A", "", name="x", duration=2.0)
        api = self.new_api()
        api.set_clip_format("wav")
        self.index(api)
        self.assertEqual(api.export_clips_folder("root", 1, 3, True)["error"], backend.BAD_SPEED)
        event, p = self.run_job(lambda: api.export_clips_folder("root", 2, 2, False))
        self.assertEqual((event, p["saved"]), ("clips-done", 1), p)
        folder = os.path.join(self.lib, "Case", backend.CLIPS)
        self.assertEqual(sorted(os.listdir(folder)), [library_ops.CLIPS_MARKER, "one_EVP-A_00m00.5s_2x.wav"])
        with wave.open(os.path.join(folder, "one_EVP-A_00m00.5s_2x.wav")) as w:
            self.assertEqual(w.getnframes(), round(1.2 * 8000 / 2))
        event, p = self.run_job(lambda: api.export_clips_folder("root", 3, 2, False))
        self.assertEqual((p["saved"], p["already"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
