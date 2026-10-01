"""The player's spectrogram: openevp.spectrogram (FFT size and rows for the sample
rate, a tone in its row, the coarser levels, the PNG tiles), the audio server
serving its tiles and reading the audio a URL serves, and the backend's
spectrogram() / set_spectrogram()."""
import os
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zlib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_marks_api as tm  # noqa: E402
from test_stretch import sine_wav  # noqa: E402
from app import backend  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import pcm, spectrogram  # noqa: E402


def compute(wav):
    return spectrogram.compute(pcm.Wav(wav))


def read_png(data):
    """(width, height, palette bytes, rows of indices) of an 8-bit palette PNG."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + body)
        chunks[kind] = chunks.get(kind, b"") + body
        pos += 12 + n
    w, h, depth, ctype = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, ctype) == (8, 3)
    raw = np.frombuffer(zlib.decompress(chunks[b"IDAT"]), np.uint8).reshape(h, w + 1)
    assert not raw[:, 0].any()                          # filter: none
    return w, h, chunks[b"PLTE"], raw[:, 1:]


class ComputeTests(unittest.TestCase):
    def test_fft_size_and_rows_by_sample_rate(self):
        for rate, fft, fmax in ((8000, 256, 4000), (16000, 512, 8000), (44100, 1024, 8000), (48000, 2048, 8000)):
            info = compute(sine_wav(1000.0, seconds=1.0, rate=rate)).info()
            self.assertEqual((info["fft"], info["fmax"]), (fft, fmax), rate)
            self.assertEqual(info["rows"], int(fmax * fft / rate) + 1)
            self.assertAlmostEqual(info["column_seconds"], fft / 4 / rate)
            self.assertEqual(info["columns"], -(-rate // (fft // 4)))

    def test_a_tone_is_in_its_row(self):
        sp = compute(sine_wav(1000.0, seconds=2.0, rate=8000, amp=0.3))
        cols = sp.levels[0]
        loud = np.argmax(cols[50:-50].mean(axis=0))
        self.assertEqual(loud, 32)                                       # 1000 Hz / 31.25 Hz per bin
        self.assertEqual(int(cols[100, 32]), 255)                        # the loudest bins: full scale
        self.assertLess(int(cols[100, 100]), 60)                         # 3125 Hz: far below

    def test_stereo_uses_both_channels(self):
        sp = compute(sine_wav(seconds=1.0, rate=16000, channels=2, freqs=[500.0, 3000.0]))
        mean = sp.levels[0][20:-20].mean(axis=0)
        self.assertGreater(mean[16], 200)                                # 500 Hz (left)
        self.assertGreater(mean[96], 200)                                # 3000 Hz (right)

    def test_levels_halve_keeping_the_louder(self):
        sp = compute(sine_wav(seconds=20.0, rate=8000))
        self.assertEqual([lv.shape[0] for lv in sp.levels], [2500, 1250, 625, 313])
        a, b = sp.levels[0], sp.levels[1]
        np.testing.assert_array_equal(b[:-1], np.maximum(a[0:-1:2], a[1::2])[:b.shape[0] - 1])

    def test_long_recordings_get_fewer_columns(self):
        old = spectrogram.MAX_COLUMNS
        spectrogram.MAX_COLUMNS = 1000
        try:
            info = compute(sine_wav(seconds=60.0, rate=8000)).info()
        finally:
            spectrogram.MAX_COLUMNS = old
        self.assertLessEqual(info["columns"], 1000)
        self.assertAlmostEqual(info["column_seconds"] * info["columns"], 60.0, delta=0.5)

    def test_tiles(self):
        sp = compute(sine_wav(1000.0, seconds=30.0, rate=8000))
        w, h, palette, idx = read_png(sp.tile(0, 0))
        self.assertEqual((w, h, len(palette)), (512, 129, 768))
        np.testing.assert_array_equal(idx, sp.levels[0][:512].T[::-1])  # highest frequency on top
        self.assertEqual(read_png(sp.tile(0, 7))[0], 3750 - 7 * 512)     # the last one: what is left
        for level, index in ((0, 8), (0, -1), (9, 0), (-1, 0)):
            self.assertIsNone(sp.tile(level, index))
        self.assertLess(sum(palette[:3]), 20)                            # inferno: near black for silence
        r, g, b = palette[-3:]
        self.assertTrue(r > 240 and g > 240 and 130 < b < 190, (r, g, b))  # pale yellow for the loudest
        mid = palette[128 * 3:128 * 3 + 3]
        self.assertTrue(mid[0] > 150 and mid[1] < 80, tuple(mid))     # red-magenta in between

    def test_stop(self):
        with self.assertRaises(spectrogram.Cancelled):
            spectrogram.compute(pcm.Wav(sine_wav(seconds=2.0)), should_stop=lambda: True)


class ServerTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.s = AudioServer(lambda key: sine_wav(seconds=1.0), self.dir)
        self.s.start()
        self.addCleanup(self.s.stop)

    def picked(self, name="a.wav", data=None):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data or sine_wav(seconds=2.0))
        return path, self.s.prepare_file(path)

    def test_tiles_are_served(self):
        _path, info = self.picked()
        with self.s.open_audio(info["url"]) as f, pcm.WavFile(f) as r:
            sp = spectrogram.compute(r)
        base = self.s.add_spectrogram(info["url"], sp)
        with urllib.request.urlopen(f"{base}/0/0.png", timeout=5) as resp:
            self.assertEqual((resp.status, resp.headers["Content-Type"]), (200, "image/png"))
            self.assertEqual(resp.read(), sp.tile(0, 0))
        for bad in (f"{base}/0/99.png", f"{base}/12345/0.png", base.replace("/spec/", "/spec/0") + "/0/0.png",
                    base.replace(base.split("/")[3], "nottoken") + "/0/0.png"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(bad, timeout=5)
            self.assertEqual(cm.exception.code, 404)
            cm.exception.close()
        self.assertEqual(self.s.spectrogram_of(info["url"]), (base, sp))

    def test_only_the_last_few_are_kept(self):
        urls = [self.picked(f"{i}.wav", sine_wav(seconds=1.0 + i))[1]["url"] for i in range(4)]
        for u in urls:
            with self.s.open_audio(u) as f, pcm.WavFile(f) as r:
                self.s.add_spectrogram(u, spectrogram.compute(r))
        self.assertIsNone(self.s.spectrogram_of(urls[0]))
        self.assertIsNotNone(self.s.spectrogram_of(urls[3]))

    def test_open_audio(self):
        path, info = self.picked()
        with self.s.open_audio(info["url"]) as f:
            self.assertEqual(f.read(4), b"RIFF")
        with open(path, "ab") as f:                                       # changed on disk: refused
            f.write(b"\0\0")
        with self.assertRaises(ValueError):
            self.s.open_audio(info["url"])
        for bad in ("http://127.0.0.1:1/x/0123456789abcdef.wav", "nonsense", None):
            with self.assertRaises(ValueError):
                self.s.open_audio(bad)
        cached = self.s.prepare(("dev", "A", 1))                          # a decode in the cache
        with self.s.open_audio(cached["url"]) as f:
            self.assertEqual(f.read(4), b"RIFF")


class ApiTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def real_api(self, path, store=None):
        server = AudioServer(lambda key: None, os.path.join(self.tmp, "cache"))
        os.makedirs(os.path.join(self.tmp, "cache"), exist_ok=True)
        server.start()
        self.addCleanup(server.stop)
        api = backend.Api(self.m, self.emit, lambda start: None, self.dest, server, pick_wav=lambda start: path,
                          store=store or self.store)
        self.addCleanup(api.shutdown)
        return api

    def test_spectrogram_of_the_loaded_recording(self):
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(sine_wav(1000.0, seconds=3.0))
        api = self.real_api(path)
        loaded = api.open_wav()
        r = api.spectrogram(loaded["rec"])
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["fft"], r["rows"], r["columns"], r["fmax"], r["tile"]), (256, 129, 375, 4000.0, 512))
        self.assertTrue(r["tiles"].startswith("http://127.0.0.1:"))
        with urllib.request.urlopen(r["tiles"] + "/0/0.png", timeout=5) as resp:
            self.assertEqual(read_png(resp.read())[:2], (375, 129))
        self.assertEqual(api.spectrogram(loaded["rec"], loaded["url"])["tiles"], r["tiles"])   # computed once
        self.assertEqual(api.spectrogram("nope")["error"], backend.RELOAD)
        r = api.spectrogram(loaded["rec"], "http://127.0.0.1:1/x/0123456789abcdef.wav")       # not this recording's
        self.assertEqual(r["error"], "That audio is not loaded. Load the recording again.")
        with open(path, "ab") as f:
            f.write(b"\0\0")
        api._server._specs.clear()
        r = api.spectrogram(loaded["rec"])
        self.assertFalse(r["ok"])
        self.assertIn("changed on disk", r["error"])
        self.assertNotIn(self.tmp, r["error"])

    def test_one_job_per_url_and_superseded_jobs_stop(self):
        import threading
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(sine_wav(1000.0, seconds=3.0))
        api = self.real_api(path)
        loaded = api.open_wav()
        rec = loaded["rec"]
        started, release, computed = threading.Event(), threading.Event(), []
        real = spectrogram.compute

        def slow(reader, should_stop=None):
            computed.append(1)
            started.set()
            release.wait(5)
            if should_stop():
                raise spectrogram.Cancelled()
            return real(reader, should_stop)
        results = []
        with mock.patch.object(spectrogram, "compute", slow):
            a = threading.Thread(target=lambda: results.append(("a", api.spectrogram(rec))))
            b = threading.Thread(target=lambda: results.append(("b", api.spectrogram(rec))))
            a.start()
            self.assertTrue(started.wait(5))
            b.start()                                         # the same URL: waits for the first
            time.sleep(0.2)
            release.set()
            a.join(5)
            b.join(5)
        self.assertEqual(len(computed), 1)
        self.assertEqual(dict(results)["a"], dict(results)["b"])
        self.assertTrue(dict(results)["a"]["ok"])
        # Another recording loaded while one is being made: that job is cancelled.
        api._server._specs.clear()
        started.clear()
        release.clear()
        with mock.patch.object(spectrogram, "compute", slow):
            t = threading.Thread(target=lambda: results.append(("c", api.spectrogram(rec))))
            t.start()
            self.assertTrue(started.wait(5))
            api.open_wav()                                    # the player moves on
            release.set()
            t.join(5)
        self.assertEqual(dict(results)["c"], {"ok": False, "cancelled": True, "error": "Stopped."})
        self.assertEqual(api._spec_jobs, {})
        # And the page can cancel it itself (hiding the spectrogram, unloading the recording).
        api._server._specs.clear()
        started.clear()
        release.clear()
        rec2 = api.open_wav()["rec"]
        with mock.patch.object(spectrogram, "compute", slow):
            t = threading.Thread(target=lambda: results.append(("d", api.spectrogram(rec2))))
            t.start()
            self.assertTrue(started.wait(5))
            self.assertEqual(api.cancel_spectrogram(), {"ok": True})
            release.set()
            t.join(5)
        self.assertEqual(dict(results)["d"]["cancelled"], True)
        self.assertTrue(api.spectrogram(rec2)["ok"])        # asked again: made

    def test_no_store_is_on(self):
        api = backend.Api(self.m, self.emit, lambda start: None, self.dest, self.server, store=None)
        self.addCleanup(api.shutdown)
        self.assertTrue(api.spectrogram_shown())

    def test_setting(self):
        api = self.real_api(None)
        # On for a new user, and for anyone who never touched it (no setting stored).
        self.assertEqual((api.spectrogram_shown(), api.capabilities()["spectrogram"]), (True, True))
        self.assertIsNone(self.store.get_setting(backend.SPECTROGRAM))
        # Turned off: off from then on, also at the next start.
        self.assertEqual(api.set_spectrogram(False), {"ok": True, "spectrogram": False, "remembered": True})
        self.assertFalse(self.real_api(None).spectrogram_shown())
        self.assertEqual(api.set_spectrogram(True), {"ok": True, "spectrogram": True, "remembered": True})
        self.assertTrue(self.real_api(None).spectrogram_shown())
        self.assertFalse(api.set_spectrogram("yes")["ok"])
        self.store.set_setting(backend.SPECTROGRAM, "damaged")
        self.assertTrue(api.spectrogram_shown())                      # damaged: the default, on
        self.store.set_setting(backend.SPECTROGRAM, False)
        second = AppData(os.path.join(self.tmp, "appdata"))
        self.addCleanup(second.close)
        other = self.real_api(None, store=second)
        self.assertFalse(other.spectrogram_shown())                  # the first window's "off"
        self.assertEqual(other.set_spectrogram(True)["remembered"], False)
        self.assertTrue(other.spectrogram_shown())


if __name__ == "__main__":
    unittest.main()
