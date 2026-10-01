"""Noise reduction from a noise sample (openevp.denoise) and the backend around it:
the noise floor drops and a tone survives, the length never changes, any range is
exactly that part of the whole, the noise-reduced version is cached by recording,
profile and amount, never fingerprinted or listed, marks stay the recording's,
and exports "as heard" include it."""
import os
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_marks_api as tm  # noqa: E402
from test_stretch import float_wav, samples  # noqa: E402
from app import backend  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from openevp import clips, denoise, enhance, pcm, wavinfo  # noqa: E402


def noisy(rate=8000, seconds=10.0, channels=1, tone=440.0, noise=0.02, amp=0.2, seed=0):
    """White noise all through, a tone from 4 s to 7 s (float samples (frames, channels))."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    rng = np.random.default_rng(seed)
    x = noise * rng.standard_normal((n, channels))
    x += np.where((t > 4) & (t < 7), amp * np.sin(2 * np.pi * tone * t), 0)[:, None]
    return x


def pcm16(x, rate):
    """A 16-bit PCM WAV of float samples (frames, channels)."""
    import io
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.round(np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()


def db(a):
    return 20 * np.log10(np.sqrt(np.mean(np.square(a))))


def tone_amp(x, freq, rate):
    t = np.arange(len(x)) / rate
    return 2 * abs(np.mean(x * np.exp(-2j * np.pi * freq * t)))


class DenoiseTests(unittest.TestCase):
    def run_it(self, x, rate, amount, learn=(0.0, 2.0)):
        src = pcm.Wav(float_wav(x, rate))
        prof = denoise.learn(src, int(learn[0] * rate), int(learn[1] * rate))
        out = denoise.reduce_wav(float_wav(x, rate), prof, amount)
        return prof, samples(out)[1]

    def test_noise_floor_drops_and_a_tone_survives(self):
        for rate, tone in ((8000, 440.0), (16000, 1000.0), (44100, 1000.0)):
            x = noisy(rate, tone=tone)
            for amount, floor in ((denoise.DEFAULT_AMOUNT, 9.0), (100, 25.0)):
                _prof, y = self.run_it(x, rate, amount)
                self.assertEqual(y.shape, x.shape)                                      # same length, channels
                drop = db(x[rate:3 * rate, 0]) - db(y[rate:3 * rate, 0])               # noise only
                self.assertGreater(drop, floor, (rate, amount))
                a, b = int(4.5 * rate), int(6.5 * rate)
                kept = 20 * np.log10(tone_amp(y[a:b, 0], tone, rate) / tone_amp(x[a:b, 0], tone, rate))
                self.assertLess(abs(kept), 0.5, (rate, amount))                         # the tone: within 0.5 dB
        _prof, y = self.run_it(x, 44100, 0)                                             # 0%: the input itself
        self.assertLess(np.abs(y - x).max(), 1e-5)

    def test_default_is_mild(self):
        self.assertEqual(denoise.DEFAULT_AMOUNT, 40)
        self.assertAlmostEqual(20 * np.log10(1 - denoise.depth(denoise.DEFAULT_AMOUNT)), -12.0, places=6)

    def test_stereo_channels_each_their_own(self):
        x = noisy(16000, channels=2)
        x[:, 1] *= 0.25                                                                  # quieter noise on the right
        prof, y = self.run_it(x, 16000, 100)
        self.assertEqual(prof.mag.shape, (2, 257))
        for ch in (0, 1):
            self.assertGreater(db(x[16000:48000, ch]) - db(y[16000:48000, ch]), 25)

    def test_a_range_is_exactly_that_part_of_the_whole(self):
        x = noisy(8000, seconds=6.0)
        wav = pcm16(x, 8000)
        prof = denoise.learn(pcm.Wav(wav), 0, 16000)
        whole = denoise.reduce_wav(wav, prof, 55)
        frames = clips._layout(memoryview(bytes(whole)))
        data = bytes(whole)[frames[3]:frames[3] + frames[4]]
        for first, last in ((0, 1), (12345, 23456), (47000, 48000), (100, 101)):
            self.assertEqual(denoise.reduce_frames(wav, prof, 55, first, last), data[2 * first:2 * last])
        self.assertEqual(denoise.reduce_wav(wav, prof, 55), whole)                       # deterministic
        old = denoise.CHUNK_SAMPLES
        denoise.CHUNK_SAMPLES = 97 * 256
        try:
            self.assertEqual(denoise.reduce_wav(wav, prof, 55), whole)                   # the chunking changes nothing
        finally:
            denoise.CHUNK_SAMPLES = old

    def test_profile(self):
        x = noisy(8000)
        src = pcm.Wav(pcm16(x, 8000))
        a, b = denoise.learn(src, 0, 8000), denoise.learn(src, 0, 8000)
        self.assertEqual(a.id, b.id)
        self.assertNotEqual(a.id, denoise.learn(src, 8000, 16000).id)
        with self.assertRaisesRegex(ValueError, "at least 0.25 seconds"):
            denoise.learn(src, 0, 1000)
        with self.assertRaisesRegex(ValueError, "silent"):
            denoise.learn(pcm.Wav(pcm16(np.zeros((8000, 1)), 8000)), 0, 8000)
        with self.assertRaisesRegex(ValueError, "learn it again"):
            denoise.reduce(pcm.Wav(pcm16(noisy(16000), 16000)), lambda y: None, a, 40)    # another rate
        with self.assertRaises(denoise.Cancelled):
            denoise.reduce(src, lambda y: None, a, 40, should_stop=lambda: True)


class ServerTests(unittest.TestCase):
    def test_no_fingerprint_when_asked(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        s = AudioServer(lambda key: pcm16(noisy(), 8000), d.name)
        s.start()
        self.addCleanup(s.stop)
        self.assertIsNone(s.prepare(("denoise", "x"), make=lambda: pcm16(noisy(), 8000), fingerprint=False)["fp"])
        self.assertIsNotNone(s.prepare(("dev", "A", 1))["fp"])


class ApiTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def real_api(self, data, name="take.wav"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        cache = os.path.join(self.tmp, "cache")
        os.makedirs(cache, exist_ok=True)
        server = AudioServer(lambda key: None, cache)
        server.start()
        self.addCleanup(server.stop)
        api = backend.Api(self.m, self.emit, lambda start: None, self.dest, server, pick_wav=lambda start: path,
                          store=self.store)
        self.addCleanup(api.shutdown)
        self.path, self.cache = path, cache
        return api, api.open_wav()

    def test_learn_reduce_cache_and_identity(self):
        x = noisy(8000)
        api, loaded = self.real_api(pcm16(x, 8000))
        rec, fp = loaded["rec"], loaded["fp"]
        lp = api.learn_noise(rec, 0.0, 2.0)
        self.assertEqual((lp["ok"], lp["seconds"]), (True, 2.0), lp)
        self.events.clear()
        r = api.reduce_noise(rec, lp["profile"], 40, 7)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("fp", r)
        self.assertEqual((r["duration"], r["rate"], r["channels"]), (loaded["duration"], 8000, 1))   # same positions
        progress = [p for e, p in self.events if e == "denoise-progress"]
        self.assertTrue(progress and progress[-1] == {"job": 7, "done": 80000, "total": 80000})
        with api._server.open_audio(r["url"]) as f:
            heard = samples(f.read())[1]
        self.assertGreater(db(x[8000:24000, 0]) - db(heard[8000:24000, 0]), 9)
        # Cached by recording, profile and amount: the same answer, nothing computed again.
        self.events.clear()
        self.assertEqual(api.reduce_noise(rec, lp["profile"], 40, 8)["url"], r["url"])
        self.assertEqual([e for e, _ in self.events], [])
        self.assertNotEqual(api.reduce_noise(rec, lp["profile"], 60, 9)["url"], r["url"])
        other = api.learn_noise(rec, 2.0, 3.5)["profile"]
        self.assertNotEqual(api.reduce_noise(rec, other, 40, 10)["url"], r["url"])
        # Never fingerprinted, never a recording: no fp in the cache, the store knows only the original.
        entry = api._server._entries[api._server._by_file[api._server.file_id(r["url"])]]
        self.assertIsNone(entry["fp"])
        m = api.add_mark(rec, 4.5, 5.0, "B", "tone")
        self.assertTrue(m["ok"])
        self.assertEqual([k for k in self.store._data.get("recordings", {})], [fp])
        self.assertEqual(api.get_marks(rec)["marks"][0]["id"], m["mark"]["id"])
        self.assertEqual(os.listdir(os.path.dirname(self.path)).count("take.wav"), 1)      # the original: untouched
        lib = api.list_library()
        self.assertEqual([f["name"] for f in lib["files"]], [])                          # nothing in the library
        # The spectrogram can show it (it is this recording's), not another URL.
        self.assertTrue(api.spectrogram(rec, r["url"])["ok"])

    def test_refusals_and_cancel(self):
        api, loaded = self.real_api(pcm16(noisy(8000, seconds=20.0), 8000))
        rec = loaded["rec"]
        self.assertEqual(api.learn_noise("nope", 0, 1)["error"], backend.RELOAD)
        for bad in ((1, 1), (2, 1), (None, 1), (True, 2), (0, float("nan"))):
            self.assertEqual(api.learn_noise(rec, *bad)["error"], "Select a part of the recording first.", bad)
        self.assertIn("at least 0.25 seconds", api.learn_noise(rec, 1.0, 1.1)["error"])
        lp = api.learn_noise(rec, 0, 2)["profile"]
        self.assertEqual(api.reduce_noise(rec, "nope", 40, 1)["error"], backend.NO_PROFILE)
        for bad in (-1, 101, "40", None, True):
            self.assertEqual(api.reduce_noise(rec, lp, bad, 1)["error"], "Unknown noise reduction amount.")
        # Another recording cannot use this one's profile.
        other_api, other = self.real_api(pcm16(noisy(8000, seed=3), 8000), "other.wav")
        self.assertEqual(other_api.reduce_noise(other["rec"], lp, 40, 1)["error"], backend.NO_PROFILE)

        def emit(event, payload):                          # cancel as soon as it reports progress
            if event == "denoise-progress":
                api.cancel_denoise(payload["job"])
        api._emit = emit
        old = denoise.CHUNK_SAMPLES
        denoise.CHUNK_SAMPLES = 64 * 256
        try:
            r = api.reduce_noise(rec, lp, 40, 5)
        finally:
            denoise.CHUNK_SAMPLES = old
        self.assertEqual((r["ok"], r.get("cancelled")), (False, True))
        self.assertEqual([n for n in os.listdir(self.cache) if n.endswith(".wav")], [])   # nothing left behind
        self.assertEqual(api._denoising, {})
        api._emit = self.emit
        self.assertTrue(api.reduce_noise(rec, lp, 40, 6)["ok"])                         # and it works afterwards


class ExportTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api
    real_api = ApiTests.real_api

    def test_exports_as_heard_include_it(self):
        x = noisy(8000, seconds=8.0)
        data = pcm16(x, 8000)
        api, loaded = self.real_api(data)
        rec = loaded["rec"]
        api.set_clip_format("wav")
        lp = api.learn_noise(rec, 0.0, 2.0)["profile"]
        api.add_mark(rec, 4.5, 5.0, "A", "tone")
        heard = {"denoise": {"profile": lp, "amount": 40}}
        r = api.export_marked(rec, 1, True, heard)
        self.assertEqual((r["ok"], r["name"]), (True, "take_enhanced.wav"), r)
        out = os.path.join(self.dest, r["name"])
        prof = api._noise[lp][1]
        expect = denoise.reduce_wav(data, prof, 40)
        with open(out, "rb") as f:
            got = f.read()
        self.assertEqual(samples(got)[1].tolist(), samples(expect)[1].tolist())       # what the player plays
        self.assertEqual(len(wavinfo.read_markers(out)), 1)
        self.assertTrue(api.export_marked(rec, 1, True, heard)["already"])            # deterministic
        r = api.export_marked(rec, 0.5, True, {"denoise": {"profile": lp, "amount": 40}, "enhance": dict(enhance.DEFAULT, boost=6)})
        self.assertEqual(r["name"], "take_0.5x_enhanced.wav")
        with wave.open(os.path.join(self.dest, r["name"])) as w:
            self.assertEqual(w.getnframes(), 2 * 64000)
        # A clip: exactly those samples of the noise-reduced recording.
        r = api.export_clips(rec, None, 1, True, heard)
        self.assertEqual(r["names"], ["take_EVP-A_00m04.5s_tone_enhanced.wav"])
        with open(os.path.join(r["folder"], r["names"][0]), "rb") as f:
            clip = samples(f.read())[1]
        whole = samples(expect)[1]
        np.testing.assert_array_equal(clip, whole[32000:44000])                         # 4.0 .. 5.5 s
        self.assertEqual(api.export_clips(rec, None, 1, True, heard)["already"], 1)
        # A profile that is gone (or another recording's) is refused, and says what to do.
        for bad in ({"denoise": {"profile": "gone", "amount": 40}},):
            r = api.export_marked(rec, 1, True, bad)
            self.assertEqual(r["error"], "The noise profile for this recording is gone. " + backend.NO_PROFILE)
        for bad in ({"denoise": {"profile": lp}}, {"denoise": {"profile": 3, "amount": 40}}, {"denoise": "x"},
                    {"denoise": {"profile": lp, "amount": 140}}):
            self.assertEqual(api.export_clips(rec, None, 1, True, bad)["error"], backend.BAD_HEARD, bad)


if __name__ == "__main__":
    unittest.main()
