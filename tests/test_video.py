"""Real video import, timeline alignment, cache lifecycle and API integration."""
import io
from contextlib import closing
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
import wave
from unittest import mock

import numpy as np

from app.audio_server import AudioServer
from app.backend import Api
from app.store import AppData
from openevp import formats, video, wavinfo


@unittest.skipUnless(video.Decoder().available(), "imageio-ffmpeg is not installed")
class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.path = str(Path(cls.temp.name) / "camera with spaces.mkv")
        # PCM begins 400 ms after the video, so a naive extract would desync.
        subprocess.run([video.executable(), "-nostdin", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1.4",
                        "-itsoffset", "0.4", "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=8000:duration=1",
                        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "pcm_s16le", cls.path],
                       check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.server = AudioServer(None, self.cache.name)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_timeline_and_fingerprint_match_streamed_index(self):
        fmt = formats.by_ext(".mkv")
        self.assertEqual(formats.read_source(fmt, self.path), self.path)
        wav = fmt.decoder.to_wav(self.path)
        with wave.open(io.BytesIO(wav)) as w:
            self.assertAlmostEqual(w.getnframes() / w.getframerate(), 1.4, places=2)
            samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            self.assertFalse(np.any(samples[:3100]), "initial silence preserves audio's 400 ms offset")
            self.assertTrue(np.any(samples[3300:]))
        fp, seconds = formats.analyze(fmt, self.path)
        self.assertEqual(fp, wavinfo.wav_fingerprint(io.BytesIO(wav)))
        self.assertAlmostEqual(seconds, 1.4, places=2)

    def test_open_video_ranges_cache_and_audio_exports(self):
        api = Api(None, lambda *args: None, None, self.cache.name, self.server,
                  pick_wav=lambda start: self.path)
        result = api.open_wav()
        self.assertTrue(result["ok"], result)
        self.assertIn("video_url", result)
        self.assertTrue(result["url"].endswith(".wav"))
        request = urllib.request.Request(result["video_url"], headers={"Range": "bytes=0-63"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertIn(b"ftyp", response.read())
        # Reading the preview back proves it has a playable video stream.
        preview = next(Path(self.cache.name).glob("*.mp4"))
        video.run(preview, ["-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"])
        with mock.patch.object(video, "preview", side_effect=AssertionError("must use cache")):
            again = api.open_wav()
        self.assertEqual(result["video_url"], again["video_url"])
        exported = api._entry_audio(api._entry(result["rec"]), "export")
        self.assertIsInstance(exported, tuple)
        self.assertEqual(wavinfo.wav_fingerprint(io.BytesIO(exported[0])), result["fp"])
        self.assertGreater(sum(e["size"] for e in self.server._entries.values()), preview.stat().st_size)
        self.server.pin([])
        self.server.reserve(1 << 40)
        self.assertFalse(list(Path(self.cache.name).glob("*.mp4")))
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(result["video_url"])
        self.assertEqual(error.exception.code, 404)

    def test_cancelled_preview_leaves_no_cache_files(self):
        def cancelled(target):
            Path(target).write_bytes(b"partial")
            raise formats.Cancelled("stopped")
        with self.assertRaises(formats.Cancelled):
            self.server.prepare(("video", 1), write=lambda out: video.Decoder().write_wav(self.path, out),
                                preview=cancelled)
        self.assertEqual(list(Path(self.cache.name).iterdir()), [])
        with self.assertRaises(formats.Cancelled):
            video.Decoder().to_wav(self.path, should_stop=lambda: True)

    def test_video_without_audio_has_actionable_error(self):
        silent = str(Path(self.cache.name) / "silent.mp4")
        video.preview(self.path, silent)
        api = Api(None, lambda *args: None, None, self.cache.name, self.server,
                  pick_wav=lambda start: silent)
        result = api.open_wav()
        self.assertFalse(result["ok"])
        self.assertIn("audio track", result["error"])
        self.assertEqual(list(Path(self.cache.name).glob("*.wav")), [])

    def test_missing_decoder_is_not_bad_recording(self):
        with mock.patch.object(video, "executable", side_effect=ImportError()):
            self.assertFalse(video.Decoder().available())
            with self.assertRaises(formats.DecoderUnavailable):
                video.Decoder().to_wav(self.path)

    def test_marks_survive_reopen_and_library_index_matches(self):
        with tempfile.TemporaryDirectory() as data, closing(AppData(data)) as store:
            api = Api(None, lambda *args: None, None, self.cache.name, self.server, store=store)
            loaded = api._play_file(self.path)
            marked = api.add_mark(loaded["rec"], 0.5, 0.8, "B", "Check movement on camera")
            self.assertTrue(marked["ok"], marked)
            self.assertTrue(api.set_reviewed(loaded["rec"], True)["ok"])
            st = Path(self.path).stat()
            indexed, _ = api._index_file(self.path, "mkv", st.st_size, st.st_mtime_ns, lambda: False, None)
            self.assertEqual(indexed["fp"], loaded["fp"])
            reopened = api._play_file(self.path)
            self.assertTrue(reopened["reviewed"])
            self.assertEqual(reopened["marks"][0]["note"], "Check movement on camera")
