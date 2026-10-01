"""The audio server's CORS headers. The page is served from another local port and
its media element asks for CORS (crossorigin="anonymous": Web Audio, for Enhance,
only gets the samples of a cross-origin element that asked), so every answer must
carry them -- 200 and 206 for every kind of audio it serves (a WAV served in place,
a .dvf decode, an MP3 decode, a noise-reduced copy), the spectrogram's PNG tiles,
and the errors (404, 409 for a file changed on disk, 416), which would otherwise
reach the page as an opaque network failure -- and a preflight is answered."""
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
import test_marks_api as tm  # noqa: E402
from test_denoise import noisy, pcm16  # noqa: E402
from app import backend  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from openevp import formats  # noqa: E402

ORIGIN = "http://127.0.0.1:5000"                   # the page's own (another) port
MP3 = os.path.join(os.path.dirname(__file__), "vectors", "mp3", "tone-8k-mono.mp3")


def mp3_problem():
    fmt = formats.by_ext(".mp3")
    if fmt is None:
        return "no .mp3 format"
    return (fmt.decoder.reason() if fmt.decoder is not None and not fmt.decoder.available() else None)


class CorsTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def real_api(self):
        cache = os.path.join(self.tmp, "cache")
        os.makedirs(cache, exist_ok=True)
        server = AudioServer(lambda key: pcm16(noisy(8000, seconds=1.0), 8000), cache)
        server.start()
        self.addCleanup(server.stop)
        picked = []
        api = backend.Api(self.m, self.emit, lambda start: None, self.dest, server,
                          pick_wav=lambda start: picked[-1], store=self.store)
        self.addCleanup(api.shutdown)
        return api, server, picked

    def request(self, url, method="GET", headers=None):
        """(status, headers) of a request from the page's origin; errors too."""
        req = urllib.request.Request(url, method=method, headers={"Origin": ORIGIN, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            with e:
                e.read()
                return e.code, e.headers

    def assert_cors(self, url, status, headers=None, method="GET"):
        got, h = self.request(url, method, headers)
        self.assertEqual(got, status, url)
        self.assertEqual(h["Access-Control-Allow-Origin"], "*", (url, status))
        self.assertIn("Content-Range", h["Access-Control-Expose-Headers"])
        return h

    def every_status(self, url):
        self.assert_cors(url, 200)
        h = self.assert_cors(url, 206, {"Range": "bytes=10-99"})
        self.assertEqual(h["Content-Range"].split("/")[0], "bytes 10-99")
        self.assert_cors(url, 206, {"Range": "bytes=-50"})
        self.assert_cors(url, 416, {"Range": "bytes=999999999-"})

    def test_every_kind_of_audio_and_every_answer(self):
        api, server, picked = self.real_api()
        # A WAV served in place: 200, 206, 416; then changed on disk: 409.
        path = os.path.join(self.tmp, "take.wav")
        with open(path, "wb") as f:
            f.write(pcm16(noisy(8000, seconds=3.0), 8000))
        picked.append(path)
        wav = api.open_wav()
        self.every_status(wav["url"])
        # A .dvf decode (a recorder recording, decoded into the cache).
        api.devices()
        dvf = api.audio(tm.ID, "A", 1)
        self.assertTrue(dvf["ok"], dvf)
        self.every_status(dvf["url"])
        # A noise-reduced copy.
        rec = wav["rec"]
        dn = api.reduce_noise(rec, api.learn_noise(rec, 0, 1)["profile"], 40, 1)
        self.every_status(dn["url"])
        # A spectrogram tile.
        sp = api.spectrogram(rec)
        h = self.assert_cors(sp["tiles"] + "/0/0.png", 200)
        self.assertEqual(h["Content-Type"], "image/png")
        # 404s: an unknown file, a wrong token, a tile that isn't there, nonsense.
        base = wav["url"].rsplit("/", 1)[0]
        self.assert_cors(base + "/0123456789abcdef.wav", 404)
        self.assert_cors(wav["url"].replace(base.rsplit("/", 1)[1], "wrongtoken"), 404)
        self.assert_cors(sp["tiles"] + "/0/999.png", 404)
        self.assert_cors(base + "/nothing", 404)
        # 409: the file served in place changed since it was loaded (its marks belong to the old one).
        with open(path, "ab") as f:
            f.write(b"\0\0")
        h = self.assert_cors(wav["url"], 409)
        self.assert_cors(wav["url"], 409, {"Range": "bytes=0-99"})
        # A preflight (OPTIONS) for a Range request.
        h = self.assert_cors(wav["url"], 204, {"Access-Control-Request-Method": "GET",
                                               "Access-Control-Request-Headers": "range"}, method="OPTIONS")
        self.assertIn("GET", h["Access-Control-Allow-Methods"])
        self.assertIn("Range", h["Access-Control-Allow-Headers"])

    @release_gate.require(os.path.isfile(MP3) and mp3_problem() is None,
                          f"the MP3 decoder is not available ({mp3_problem()})")
    def test_an_mp3_decode(self):
        api, server, picked = self.real_api()
        path = os.path.join(self.tmp, "song.mp3")
        shutil.copyfile(MP3, path)
        picked.append(path)
        mp3 = api.open_wav()
        self.assertTrue(mp3["ok"], mp3)
        self.assertTrue(mp3["url"].endswith(".wav"))                          # decoded to WAV here
        self.every_status(mp3["url"])
        # The listening tools work on it as on any recording.
        self.assertTrue(api.spectrogram(mp3["rec"])["ok"])
        lp = api.learn_noise(mp3["rec"], 0.1, 0.6)
        self.assertTrue(lp["ok"], lp)
        dn = api.reduce_noise(mp3["rec"], lp["profile"], 40, 2)
        self.assertEqual((dn["ok"], dn["duration"]), (True, mp3["duration"]))
        self.every_status(dn["url"])


class TempDirTests(unittest.TestCase):
    def test_preflight_on_a_bare_server(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        s = AudioServer(lambda key: b"", d.name)
        s.start()
        self.addCleanup(s.stop)
        url = f"http://127.0.0.1:{s._httpd.server_address[1]}/x/0123456789abcdef.wav"
        req = urllib.request.Request(url, method="OPTIONS", headers={"Origin": ORIGIN})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual((r.status, r.headers["Access-Control-Allow-Origin"], r.headers["Content-Length"]), (204, "*", "0"))


if __name__ == "__main__":
    unittest.main()
