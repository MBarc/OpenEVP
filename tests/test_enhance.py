"""Enhance (openevp.enhance, openevp.leveler): the settings, the chain for a
sample rate, the biquads against a direct recursion, the Leveler against a
sample-by-sample transcription of the browser's compressor, the limiter, and the
backend: the remembered setting and exports "as heard" (names ending in
"_enhanced", combined with the speed; identical bytes the second time)."""
import inspect
import json
import math
import os
import sys
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_library as tl  # noqa: E402
import test_marks_api as tm  # noqa: E402
from test_stretch import float_wav, samples, sine_wav  # noqa: E402
from app import backend  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import clips, enhance, leveler, wavinfo  # noqa: E402

D = enhance.DEFAULT


def tone(freq, seconds, rate, amp=0.5):
    t = np.arange(int(seconds * rate)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def amp_at(x, freq, rate):
    """The amplitude of freq in x (a steady part)."""
    t = np.arange(len(x)) / rate
    return 2 * abs(np.mean(x * np.exp(-2j * np.pi * freq * t)))


def run_chain(x, settings, rate):
    """enhance.process() on float samples (mono), as floats."""
    out = enhance.process(float_wav(x[:, None], rate), settings)
    return samples(out)[1][:, 0]


class SettingsTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(enhance.normalize(dict(D)), D)
        self.assertEqual(enhance.normalize(dict(D, boost=11.6))["boost"], 12)
        for bad in (None, [], dict(D, boost=25), dict(D, boost=-1), dict(D, boost=True), dict(D, hum="55"),
                    dict(D, leveler="yes"), dict(D, strength="max"), {k: v for k, v in D.items() if k != "voice"},
                    dict(D, boost=float("nan"))):
            with self.assertRaises(ValueError, msg=bad):
                enhance.normalize(bad)
        damaged = {"boost": "loud", "leveler": True, "strength": "max", "hum": "50", "voice": 1}
        self.assertEqual(enhance.normalize(damaged, strict=False), dict(D, leveler=True, hum="50"))
        self.assertEqual(enhance.normalize("x", strict=False), D)

    def test_graph_for_a_sample_rate(self):
        self.assertEqual(enhance.graph(D, 8000), [])
        self.assertFalse(enhance.active(D, 8000))
        everything = dict(D, boost=6, leveler=True, strength="light", voice=True, rumble=True, hiss=True, hum="60")
        q = 1 / math.sqrt(2)
        self.assertEqual(enhance.graph(everything, 8000), [
            ("highpass", 120.0, q), ("highpass", 300.0, q), ("lowpass", 3400.0, q),       # no Cut hiss at 8 kHz
            ("notch", 60.0, 12.0), ("notch", 120.0, 24.0), ("notch", 180.0, 36.0), ("notch", 240.0, 48.0),
            ("compressor", "light"), ("gain", 6.0), ("limiter",)])
        g = enhance.graph(dict(D, hiss=True, hum="50"), 44100)
        self.assertEqual(g[0], ("lowpass", 5000.0, q))
        self.assertEqual([s[1] for s in g[1:]], [50.0, 100.0, 150.0, 200.0])
        self.assertFalse(enhance.active(dict(D, hiss=True), 11025))                    # nothing above 5 kHz to cut
        self.assertTrue(enhance.active(dict(D, hiss=True), 16000))
        self.assertEqual(enhance.graph(dict(D, hum="60"), 400), [("notch", 60.0, 12.0), ("notch", 120.0, 24.0),
                                                                ("notch", 180.0, 36.0)])   # below 0.95 x Nyquist
        self.assertEqual(enhance.graph(dict(D, voice=True), 8000)[-1][0], "lowpass")   # no limiter without gain

    def test_spec_for_the_page(self):
        sp = enhance.spec()
        self.assertEqual((sp["boost_max"], sp["hiss_min_rate"], sp["hum_harmonics"]), (24, 12000, 4))
        self.assertEqual(sorted(sp["leveler"]), ["light", "medium", "strong"])
        # The page check's copy (tests/ui_check.js) is the same.
        with open(os.path.join(os.path.dirname(__file__), "ui_check.js"), encoding="utf-8") as f:
            line = next(x for x in f if x.startswith("const ENHANCE_SPEC = "))
        self.assertEqual(json.loads(line[len("const ENHANCE_SPEC = "):].rstrip().rstrip(";")), json.loads(json.dumps(sp)))


class FilterTests(unittest.TestCase):
    def direct(self, x, coefs):
        """The biquads run sample by sample (the textbook recursion)."""
        y = np.asarray(x, np.float64)
        for b0, b1, b2, a1, a2 in coefs:
            out = np.zeros_like(y)
            x1 = x2 = y1 = y2 = 0.0
            for i, v in enumerate(y):
                o = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                x2, x1, y2, y1 = x1, v, y1, o
                out[i] = o
            y = out
        return y

    def test_fir_equals_the_recursion(self):
        rate = 8000
        x = np.random.default_rng(1).standard_normal(6000) * 0.2
        s = dict(D, rumble=True, voice=True, hum="60")
        coefs = [enhance.biquad(k, f, q, rate) for k, f, q in enhance.graph(s, rate)]
        ours = run_chain(x.astype(np.float32), s, rate)
        ref = self.direct(x.astype(np.float32), coefs)
        self.assertLess(np.abs(ours - ref).max(), 1e-6)

    def test_responses(self):
        rate = 44100
        for kind, f in (("lowpass", 3400.0), ("highpass", 300.0)):
            h = enhance.response([enhance.biquad(kind, f, 1 / math.sqrt(2), rate)], rate * 2)
            self.assertAlmostEqual(20 * math.log10(abs(h[int(2 * f)])), -3.01, places=1)   # Butterworth: -3 dB there
        h = enhance.response([enhance.biquad("notch", 60.0, 12.0, rate)], rate * 2)
        self.assertLess(abs(h[120]), 1e-6)                                             # 60 Hz: gone
        self.assertAlmostEqual(abs(h[2000]), 1.0, places=3)                            # 1 kHz: untouched

    def test_hum_remover_and_voice_filter_on_tones(self):
        rate = 8000
        x = tone(60, 4, rate, 0.3) + tone(1000, 4, rate, 0.3)
        y = run_chain(x.astype(np.float32), dict(D, hum="60"), rate)[2 * rate:]
        self.assertLess(amp_at(y, 60, rate), 0.003)                                    # 60 Hz down by 40 dB
        self.assertAlmostEqual(amp_at(y, 1000, rate), 0.3, delta=0.003)
        y = run_chain((tone(60, 4, rate, 0.3) + tone(1000, 4, rate, 0.3)).astype(np.float32), dict(D, voice=True), rate)
        self.assertLess(amp_at(y[2 * rate:], 60, rate), 0.02)
        self.assertGreater(amp_at(y[2 * rate:], 1000, rate), 0.28)


class LevelerTests(unittest.TestCase):
    def reference(self, x, preset, rate):
        """Chromium's DynamicsCompressor::Process, transcribed sample by sample (float64),
        its output shifted back by the pre-delay."""
        c = leveler.Curve(preset["threshold"], preset["knee"], preset["ratio"])
        post = c.makeup()
        attack_frames = max(0.001, preset["attack"]) * rate
        rf = rate * preset["release"]
        a, b, cc, d, e = [rf * sum(w * z for w, z in zip(row, leveler._ZONES)) for row in leveler._POLY]
        sat = leveler.SAT_RELEASE * rate
        delay = int(leveler.PRE_DELAY * rate)
        xs = np.concatenate([x, np.zeros(delay)])
        buf = np.zeros(len(xs) + delay)
        out = np.zeros(len(xs))
        det, gain, max_db = 0.0, 1.0, -1.0
        for i in range(0, len(xs), 32):
            scaled = math.asin(det) / (math.pi / 2)
            rel = scaled > gain
            diff = (-1.0 if rel else 1.0) if scaled == 0 else 20 * math.log10(gain / scaled)
            if rel:
                max_db = -1.0
                z = 0.25 * (min(0.0, max(-12.0, diff)) + 12)
                rate_ = 10 ** (5 / (a + b * z + cc * z * z + d * z ** 3 + e * z ** 4) / 20)
            else:
                max_db = diff if (max_db == -1 or max_db < diff) else max_db
                rate_ = 1 - (0.25 / max(0.5, max_db)) ** (1 / attack_frames)
            for j in range(i, min(i + 32, len(xs))):
                buf[j + delay] = xs[j]
                lv = abs(xs[j])
                att = 1.0 if lv <= 0.0001 else float(c.saturate(lv)) / lv
                dba = max(2.0, -20 * math.log10(att))
                r = 10 ** (dba / sat / 20) - 1
                det += (att - det) * (r if att > det else 1)
                det = min(1.0, det)
                if rate_ < 1:
                    gain += (scaled - gain) * rate_
                else:
                    gain = min(1.0, gain * rate_)
                out[j] = buf[j] * post * math.sin(math.pi / 2 * gain)
        return out[delay:]

    def test_matches_the_browser_algorithm(self):
        rate = 8000
        t = np.arange(rate * 2) / rate
        x = np.where(t % 0.5 < 0.25, 0.02, 0.7) * np.sin(2 * np.pi * 300 * t)
        for name, preset in enhance.LEVELER.items():
            ours = run_chain(x.astype(np.float32), dict(D, leveler=True, strength=name), rate)
            ref = enhance.limit(self.reference(x.astype(np.float32).astype(np.float64), preset, rate))
            self.assertLess(np.abs(ours - ref).max(), 1e-5, name)

    def test_quiet_parts_come_up(self):
        rate = 8000
        t = np.arange(rate * 4) / rate
        x = np.where(t < 2, 0.02, 0.5) * np.sin(2 * np.pi * 440 * t)
        y = run_chain(x.astype(np.float32), dict(D, leveler=True, strength="strong"), rate)
        before = amp_at(x[rate // 2:rate], 440, rate) / amp_at(x[3 * rate:], 440, rate)
        after = amp_at(y[rate // 2:rate], 440, rate) / amp_at(y[3 * rate:], 440, rate)
        self.assertGreater(20 * math.log10(after / before), 12)                       # the gap shrinks by > 12 dB
        self.assertGreater(enhance.makeup_db(enhance.LEVELER["strong"]), 0)


class LimiterTests(unittest.TestCase):
    def test_boost_never_clips(self):
        rate = 8000
        x = tone(440, 1, rate, 0.9).astype(np.float32)
        y = run_chain(x, dict(D, boost=24), rate)
        self.assertLessEqual(np.abs(y).max(), 1.0)
        self.assertGreater(np.abs(y).max(), 0.95)
        quiet = tone(440, 1, rate, 0.01).astype(np.float32)                           # +24 dB: 0.158, below the knee
        y = run_chain(quiet, dict(D, boost=24), rate)
        self.assertAlmostEqual(np.abs(y).max() / 0.01, 10 ** (24 / 20), delta=0.05)

    def test_curve(self):
        c = enhance.limit_curve()
        self.assertEqual(len(c), enhance.LIMIT_POINTS)
        x = np.array([-100.0, -2.0, -0.5, 0.0, 0.3, 0.8, 1.5, 64.0, 1000.0])
        y = enhance.limit(x, c)
        np.testing.assert_allclose(y[2:6], [-0.5, 0.0, 0.3, 0.8], atol=1e-12)          # linear to the knee
        self.assertTrue(np.all(np.abs(y) < 1.0 + 1e-9) and np.all(np.diff(y) >= 0))


class ProcessTests(unittest.TestCase):
    def test_same_format_and_length_deterministic(self):
        for channels, width, rate in ((1, 2, 8000), (2, 2, 44100), (1, 3, 16000), (2, 4, 44100)):
            wav = sine_wav(440.0, seconds=1.3, rate=rate, channels=channels, width=width, amp=0.3)
            s = dict(D, boost=6, leveler=True, voice=True, hum="50")
            out = enhance.process(wav, s)
            a, b = clips._layout(memoryview(wav)), clips._layout(memoryview(bytes(out)))
            self.assertEqual((a[0], a[1], a[2], a[4]), (b[0], b[1], b[2], b[4]))       # fmt, rate, align, data length
            self.assertEqual(out, enhance.process(wav, s))
        wav = sine_wav(440.0, seconds=0.5)
        self.assertEqual(clips._layout(memoryview(bytes(enhance.process(wav, D))))[4], 8000)   # all off: unchanged
        self.assertEqual(samples(enhance.process(wav, D))[1].tolist(), samples(wav)[1].tolist())

    def test_stop(self):
        with self.assertRaises(enhance.Cancelled):
            enhance.process(sine_wav(seconds=10.0), dict(D, boost=3), should_stop=lambda: True)


class EnhanceSettingTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def test_default_remembered_damaged_and_second_window(self):
        self.assertEqual(self.api.enhance_settings(), D)
        caps = self.api.capabilities()
        self.assertEqual((caps["enhance"], caps["enhance_spec"]), (D, enhance.spec()))
        on = dict(D, boost=9, hum="50")
        self.assertEqual(self.api.set_enhance(on), {"ok": True, "enhance": on, "remembered": True})
        self.assertEqual(self.new_api().enhance_settings(), on)                          # the next start
        self.assertEqual(self.api.set_enhance(dict(D, boost=99))["error"], backend.BAD_HEARD)
        self.store.set_setting(backend.ENHANCE, {"boost": "x", "voice": True})          # damaged: field by field
        self.assertEqual(self.api.enhance_settings(), dict(D, voice=True))
        second = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        self.assertEqual(api.set_enhance(on)["remembered"], False)
        self.assertEqual(api.enhance_settings(), on)


class ExportAsHeardTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def picked(self, data):
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(data)
        api = self.new_api(pick_wav=lambda start: path)
        return api, api.open_wav()["rec"]

    def test_export_marked_as_heard(self):
        api, rec = self.picked(sine_wav(440.0, seconds=2.0, amp=0.05))
        api.add_mark(rec, 0.2, 0.3, "C", "knock")
        heard = {"enhance": dict(D, boost=12)}
        r = api.export_marked(rec, 1, True, heard)
        self.assertEqual((r["ok"], r["saved"], r["name"]), (True, True, "take_enhanced.wav"), r)
        out = os.path.join(self.dest, r["name"])
        with open(out, "rb") as f:
            rate, y = samples(f.read())
        self.assertAlmostEqual(np.abs(y).max() / 0.05, 10 ** (12 / 20), delta=0.05)  # +12 dB
        self.assertEqual([(round(m["start"], 3), round(m["end"], 3)) for m in wavinfo.read_markers(out)], [(0.2, 0.3)])
        self.assertTrue(api.export_marked(rec, 1, True, heard)["already"])            # deterministic: skipped
        r = api.export_marked(rec, 1, True, {"enhance": dict(D, boost=6)})            # other settings: a new copy
        self.assertEqual((r["saved"], r["name"]), (True, "take_enhanced (2).wav"))
        r = api.export_marked(rec, 0.5, True, heard)
        self.assertEqual(r["name"], "take_0.5x_enhanced.wav")
        with wave.open(os.path.join(self.dest, r["name"])) as w:
            self.assertEqual(w.getnframes(), 32000)
        self.assertEqual(api.export_marked(rec, 0.5, False, heard)["name"], "take_0.5x-tape_enhanced.wav")
        self.assertEqual(api.export_marked(rec, 1, True, {"enhance": D})["name"], "take.wav")   # nothing on
        self.assertEqual(api.export_marked(rec, 1, True, None)["name"], "take.wav")
        self.assertEqual(api.export_marked(rec, 1, True, {"enhance": dict(D, hiss=True)})["name"], "take.wav")   # 8 kHz
        for bad in ({"enhance": {"boost": 3}}, {"enhance": dict(D, boost=30)}, {"loud": True}, "x", [1]):
            self.assertEqual(api.export_marked(rec, 1, True, bad)["error"], backend.BAD_HEARD, bad)
        self.assertEqual(wavinfo.read_markers(os.path.join(self.tmp, "take.wav")), [])    # the original: untouched

    def test_export_clips_as_heard(self):
        api, rec = self.picked(sine_wav(440.0, seconds=3.0, amp=0.05))
        api.set_clip_format("wav")
        m = api.add_mark(rec, 1.0, 1.4, "B", "hello")["mark"]
        heard = {"enhance": dict(D, voice=True, leveler=True)}
        r = api.export_clips(rec, None, 1, True, heard)
        self.assertEqual((r["saved"], r["names"]), (1, ["take_EVP-B_00m01.0s_hello_enhanced.wav"]), r)
        r = api.export_clips(rec, None, 0.75, False, heard)
        self.assertEqual(r["names"], ["take_EVP-B_00m01.0s_hello_0.75x-tape_enhanced.wav"])
        path = os.path.join(r["folder"], r["names"][0])
        with wave.open(path) as w:
            self.assertEqual(w.getnframes(), round(1.4 * 8000 / 0.75))               # the speed's length, kept
        self.assertAlmostEqual(wavinfo.read_markers(path)[0]["start"], 0.5 / 0.75, places=3)
        r = api.export_clips(rec, m["id"], 0.75, False, heard)                          # Save clip: identical, skipped
        self.assertEqual((r["saved"], r["already"]), (0, 1))
        self.assertEqual(clips.name("x", m, speed=0.5, enhanced=True), "x_EVP-B_00m01.0s_hello_0.5x_enhanced.wav")
        self.assertEqual(api.export_clips(rec, None, 1, True, {"enhance": 5})["error"], backend.BAD_HEARD)


class LibraryClipsStayAsRecordedTests(unittest.TestCase):
    setUp = tl.LibraryTests.setUp
    new_api = tl.LibraryTests.new_api

    def test_no_enhancement_for_the_library_job(self):
        api = self.new_api()
        self.assertNotIn("heard", inspect.signature(api.export_clips_folder).parameters)
        self.assertNotIn("heard", inspect.signature(api.export_clips_files).parameters)
        self.assertNotIn("heard", inspect.signature(api._clips_job).parameters)


if __name__ == "__main__":
    unittest.main()
