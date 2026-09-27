import io
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.audio_server import AudioServer  # noqa: E402
from openevp import wavinfo  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402


def make_wav(seconds=1.0, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(int(((i % 200) - 100) * 300).to_bytes(2, "little", signed=True) for i in range(n)))
    return buf.getvalue()


WAV = make_wav()


class AudioServerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()

        def provider(key):
            self.calls.append(key)
            self.gate.wait(5)
            if key[2] == 99:
                raise ValueError("no recording A-099")
            return WAV

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.s = AudioServer(provider, self.dir.name, max_bytes=3 * len(WAV))
        self.s.start()
        self.addCleanup(self.s.stop)

    def get(self, url, headers=None):
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5)

    def test_prepare_gives_url_peaks_and_duration(self):
        info = self.s.prepare(("1-4@7", "A", 7))
        self.assertAlmostEqual(info["duration"], 1.0, places=3)
        self.assertTrue(0 < len(info["peaks"]) <= 400)                 # 400 per second
        self.assertTrue(all(0.0 <= p <= 1.0 for p in info["peaks"]))
        self.assertEqual(info["fp"], wavinfo.wav_fingerprint(io.BytesIO(WAV)))
        r = self.get(info["url"])
        self.assertEqual((r.status, r.headers["Content-Type"], r.headers["Accept-Ranges"]),
                         (200, "audio/wav", "bytes"))
        self.assertEqual(r.read(), WAV)

    def test_range_request(self):
        url = self.s.prepare(("1-4@7", "A", 1))["url"]
        r = self.get(url, {"Range": "bytes=10-19"})
        self.assertEqual(r.status, 206)
        self.assertEqual(r.headers["Content-Range"], f"bytes 10-19/{len(WAV)}")
        self.assertEqual(r.read(), WAV[10:20])
        self.assertEqual(self.get(url, {"Range": "bytes=-4"}).read(), WAV[-4:])

    def test_concurrent_prepares_decode_once(self):
        self.gate.clear()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.s.prepare(("1-4@7", "A", 1))))
                   for _ in range(3)]
        for t in threads:
            t.start()
        self.gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len({r["url"] for r in results}), 1)

    def test_byte_limit_evicts_oldest_and_forget_drops_a_device(self):
        urls = [self.s.prepare(("1-4@7", "A", n))["url"] for n in (1, 2, 3, 4)]
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get(urls[0])                                   # evicted: over 3 x len(WAV)
        self.assertEqual(c.exception.code, 404)
        self.assertEqual(self.get(urls[3]).read(), WAV)
        self.s.forget("1-4@7")
        with self.assertRaises(urllib.error.HTTPError):
            self.get(urls[3])
        self.assertEqual(os.listdir(self.dir.name), [])

    def test_wrong_token_and_provider_errors(self):
        url = self.s.prepare(("1-4@7", "A", 1))["url"]
        bad = url.replace(self.s._token, "x" * len(self.s._token))
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get(bad)
        self.assertEqual(c.exception.code, 404)
        with self.assertRaises(ValueError):
            self.s.prepare(("1-4@7", "A", 99))

    def test_inflight_cleared_after_provider_failure(self):
        with self.assertRaises(ValueError):
            self.s.prepare(("1-4@7", "A", 99))
        self.assertEqual(self.s._inflight, {})



def pcm_wav(path, width, channels, frames, rate=48000):
    """A WAV whose loudest sample in channel 1 is half scale and in channel 0 quarter scale."""
    import struct
    full = 1 << (8 * width - 1)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        out = bytearray()
        for i in range(frames):
            for c in range(channels):
                v = int(((i % 100) - 50) / 50 * full * (0.25 if c == 0 else 0.5))
                if width == 1:
                    out += bytes([v + 128])
                elif width == 3:
                    out += (v & 0xFFFFFF).to_bytes(3, "little")
                else:
                    out += struct.pack({2: "<h", 4: "<i"}[width], v)
        w.writeframes(bytes(out))

    def test_retarget_prefix_keeps_a_file_served_in_place_playing(self):
        folder = os.path.join(self.dir.name, "lib", "Old Mill")
        os.makedirs(folder)
        path = os.path.join(folder, "a.wav")
        with open(path, "wb") as f:
            f.write(WAV)
        info = self.s.prepare_file(path)
        moved = os.path.join(self.dir.name, "lib", "Mill 2")
        os.rename(folder, moved)
        with self.assertRaises(urllib.error.HTTPError):          # the old place is gone
            self.get(info["url"]).close()
        self.s.retarget_prefix(folder, moved)
        with self.get(info["url"]) as r:
            self.assertEqual(r.read(), WAV)
        again = self.s.prepare_file(os.path.join(moved, "a.wav"))  # found under its new key: no new entry
        self.assertEqual(again["url"], info["url"])
        self.s.retarget_prefix(os.path.join(self.dir.name, "elsewhere"), moved)   # nothing under it: no change
        with self.get(info["url"]) as r:
            self.assertEqual(r.status, 200)

    @unittest.skipUnless(sys.platform == "win32", "Windows file sharing")
    def test_a_file_being_served_can_be_renamed(self):
        from app import audio_server
        path = os.path.join(self.dir.name, "open.wav")
        with open(path, "wb") as f:
            f.write(WAV)
        with audio_server._open_shared(path) as f:
            os.rename(path, path + ".moved")                      # refused with Python's own open()
            self.assertEqual(f.read(), WAV)
        with self.assertRaises(FileNotFoundError):
            audio_server._open_shared(path)


class AnalyzeTests(unittest.TestCase):
    def test_24_bit_stereo_and_8_bit(self):
        from app import audio_server
        with tempfile.TemporaryDirectory() as d:
            for width, channels in ((3, 2), (1, 1), (2, 2)):
                p = os.path.join(d, f"w{width}c{channels}.wav")
                pcm_wav(p, width, channels, 9000)
                with open(p, "rb") as f:
                    peaks, duration, rate, fp = audio_server._analyze(f)
                self.assertEqual(rate, 48000)
                self.assertAlmostEqual(duration, 9000 / 48000)
                self.assertAlmostEqual(max(peaks), 0.5 if channels == 2 else 0.25, places=2)
                self.assertEqual(fp, wavinfo.wav_fingerprint(p))

    def test_chunked_peaks_equal_whole_file_peaks(self):
        from unittest import mock
        from app import audio_server
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "long.wav")
            pcm_wav(p, 3, 2, 50000)
            with open(p, "rb") as f:
                whole = audio_server._analyze(f)
            with mock.patch.object(audio_server, "CHUNK_BYTES", 1000), open(p, "rb") as f:
                chunked = audio_server._analyze(f)
            self.assertEqual(whole, chunked)
            self.assertLessEqual(len(whole[0]), audio_server.MAX_PEAKS)

    def test_peaks_follow_the_deepest_zoom_and_are_capped(self):
        from unittest import mock
        from app import audio_server
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "two-seconds.wav")
            pcm_wav(p, 2, 1, 16000, rate=8000)
            with open(p, "rb") as f:
                self.assertEqual(len(audio_server._analyze(f)[0]), 2 * audio_server.PEAKS_PER_SECOND)
            with mock.patch.object(audio_server, "MAX_PEAKS", 100), open(p, "rb") as f:
                self.assertEqual(len(audio_server._analyze(f)[0]), 100)

    def test_an_empty_wav_has_no_fingerprint(self):
        """Empty WAVs of one format must not share one identity (and its marks)."""
        from app import audio_server
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "empty.wav")
            pcm_wav(p, 2, 1, 0, rate=8000)
            with open(p, "rb") as f:
                self.assertEqual(audio_server._analyze(f), ([], 0.0, 8000, None))

    def test_not_a_wav(self):
        from app import audio_server
        with self.assertRaises(ValueError):
            audio_server._analyze(io.BytesIO(b"this is not audio"))


class RealDecoderFingerprintTests(unittest.TestCase):
    """_analyze()'s fp on a real decoded recording (not a synthetic pcm_wav
    fixture) matches wavinfo.wav_fingerprint of the same WAV, and survives
    with_markers(). Skipped cleanly when openevp.decoders.sony_lpec's extracted
    tables are not available in this checkout."""

    VECTOR = os.path.join(os.path.dirname(__file__), "vectors", "single-frame.dvf")

    def test_fp_matches_real_decoded_audio_and_survives_markers(self):
        from st25 import audio
        if not audio.available():
            release_gate.skip_or_fail(f"WAV conversion is not available: {audio.status()}")
        from app import audio_server
        with open(self.VECTOR, "rb") as f:
            dvf_bytes = f.read()
        wav = audio.dvf_to_wav(dvf_bytes)
        _, _, _, fp = audio_server._analyze(io.BytesIO(wav))
        self.assertEqual(fp, wavinfo.wav_fingerprint(io.BytesIO(wav)))
        marked = wavinfo.with_markers(wav, [{"start": 0.0, "end": 0.0, "cls": "A", "note": "x"}])
        self.assertEqual(wavinfo.wav_fingerprint(io.BytesIO(marked)), fp)


class PickedFileTests(unittest.TestCase):
    def test_picked_file_is_served_in_place_and_never_deleted(self):
        with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as mine:
            s = AudioServer(lambda key: WAV, cache, max_bytes=1)
            s.start()
            self.addCleanup(s.stop)
            p = os.path.join(mine, "dr60.wav")
            pcm_wav(p, 3, 2, 4800)
            info = s.prepare_file(p)
            with open(p, "rb") as f:
                self.assertEqual(urllib.request.urlopen(info["url"], timeout=5).read(), f.read())
            self.assertEqual(os.listdir(cache), [])                  # nothing copied
            s.prepare(("1-4@7", "A", 1))                              # tiny budget: evicts everything else
            s.forget("1-4@7")
            self.assertTrue(os.path.exists(p))
            self.assertEqual(urllib.request.urlopen(info["url"], timeout=5).status, 200)
            with self.assertRaises(ValueError):
                bad = os.path.join(mine, "notes.wav")
                with open(bad, "w") as f:
                    f.write("hello")
                s.prepare_file(bad)

    def test_a_file_changed_on_disk_is_refused_not_served_under_the_old_handle(self):
        with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as mine:
            s = AudioServer(lambda key: WAV, cache)
            s.start()
            self.addCleanup(s.stop)
            p = os.path.join(mine, "take.wav")
            pcm_wav(p, 2, 1, 4800)
            info = s.prepare_file(p)
            st = os.stat(p)
            self.assertEqual(info["stat"], (st.st_size, st.st_mtime_ns))
            self.assertEqual(urllib.request.urlopen(info["url"], timeout=5).status, 200)
            pcm_wav(p, 2, 1, 9600)                                   # another recording at the same path
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(info["url"], timeout=5)
            self.assertEqual(cm.exception.code, 409)
            cm.exception.close()
            # Same size, only the time changed (an in-place edit): refused as well.
            pcm_wav(p, 2, 1, 4800)
            info = s.prepare_file(p)
            os.utime(p, ns=(st.st_atime_ns, os.stat(p).st_mtime_ns + 10**9))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(info["url"], timeout=5)
            self.assertEqual(cm.exception.code, 409)
            cm.exception.close()
            # Loading it again gives a new handle for the new version, which plays.
            again = s.prepare_file(p)
            self.assertNotEqual(again["url"], info["url"])
            self.assertEqual(urllib.request.urlopen(again["url"], timeout=5).status, 200)


if __name__ == "__main__":
    unittest.main()
