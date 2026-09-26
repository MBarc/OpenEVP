import os
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, make_raw  # noqa: E402
from app import backend  # noqa: E402
from st25 import audio, dvf  # noqa: E402


class FakeServer:
    def __init__(self):
        self.files, self.made = [], []

    def prepare_file(self, path):
        self.files.append(path)
        return {"url": "http://x/f.wav", "peaks": [0.1], "duration": 1.0}

    def prepare(self, key, make=None):
        self.made.append((key[0], make()))
        return {"url": "http://x/d.wav", "peaks": [0.2], "duration": 2.0}


def write_wav(path, seconds=2.0, rate=8000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(2 * int(seconds * rate)))


class SavedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "OpenEVP")
        self.server = FakeServer()
        self.api = backend.Api(None, lambda *a: None, lambda: None, self.dest, self.server)

    def populate(self):
        os.makedirs(os.path.join(self.dest, "A", "deeper"))
        with open(os.path.join(self.dest, "A", "001_A_001_X.dvf"), "wb") as f:
            f.write(dvf.build(make_raw(3010, 100), DATE, "X", expected_length=3010))   # 3 blocks: 2980 audio bytes
        write_wav(os.path.join(self.dest, "A", "session.WAV"), 2.5)
        write_wav(os.path.join(self.dest, "top.wav"), 1.0)
        write_wav(os.path.join(self.dest, "A", "deeper", "ignored.wav"))            # only one level deep
        open(os.path.join(self.dest, "notes.txt"), "w").close()                     # not audio

    def test_missing_folder(self):
        r = self.api.list_saved()
        self.assertEqual((r["ok"], r["exists"], r["files"]), (True, False, []))
        self.assertFalse(os.path.exists(self.dest))                                  # never created by listing

    def test_lists_dvf_and_wav_one_level_deep(self):
        self.populate()
        r = self.api.list_saved()
        got = [(f["folder"], f["name"], f["type"], f["seconds"]) for f in r["files"]]
        self.assertEqual(got, [("", "top.wav", "wav", 1.0),
                               ("A", "001_A_001_X.dvf", "dvf", round(2980 / 750, 1)),
                               ("A", "session.WAV", "wav", 2.5)])
        self.assertTrue(all("path" not in f for f in r["files"]))                   # the page never sees paths
        self.assertEqual([f["id"] for f in self.api.list_saved()["files"]], [f["id"] for f in r["files"]])

    def test_play_wav_and_dvf(self):
        self.populate()
        files = {f["name"]: f["id"] for f in self.api.list_saved()["files"]}
        r = self.api.play_saved(files["session.WAV"])
        self.assertEqual((r["ok"], r["name"]), (True, "session.WAV"))
        self.assertTrue(self.server.files[0].endswith("session.WAV"))
        with mock.patch.dict(sys.modules, {"st25.lpec": None}):
            r = self.api.play_saved(files["001_A_001_X.dvf"])
            self.assertFalse(r["ok"])
            self.assertIn(audio.status(), r["error"])
            self.assertNotIn("coming soon", r["error"])
        fake_missing_tables = types.ModuleType("st25.lpec")
        fake_missing_tables.dvf_to_wav = lambda data: data

        def boom_check():
            raise RuntimeError("lpec_tables.json not found")
        fake_missing_tables.check = boom_check
        with mock.patch.dict(sys.modules, {"st25.lpec": fake_missing_tables}):
            r = self.api.play_saved(files["001_A_001_X.dvf"])
            self.assertFalse(r["ok"])
            self.assertIn("could not be loaded", r["error"])
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:4]
        with mock.patch.dict(sys.modules, {"st25.lpec": fake}):
            r = self.api.play_saved(files["001_A_001_X.dvf"])
        self.assertEqual((r["ok"], r["url"]), (True, "http://x/d.wav"))
        self.assertEqual(self.server.made[0][0], "dvf")
        self.assertTrue(self.server.made[0][1].startswith(b"RIFFMS_V"))

    def test_unknown_or_vanished_file(self):
        self.populate()
        files = {f["name"]: f["id"] for f in self.api.list_saved()["files"]}
        self.assertFalse(self.api.play_saved("0123456789abcdef")["ok"])
        self.assertFalse(self.api.play_saved(None)["ok"])
        os.remove(os.path.join(self.dest, "top.wav"))
        self.assertFalse(self.api.play_saved(files["top.wav"])["ok"])


if __name__ == "__main__":
    unittest.main()
