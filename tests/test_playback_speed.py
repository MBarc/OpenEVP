"""The player's playback speed setting (Api.playback_speed / set_playback_speed):
1x and keep-pitch by default, remembered across starts, a damaged stored value
falling back to its default, and a second (read-only) window keeping its choice
for the session only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_marks_api as tm  # noqa: E402
from app import backend  # noqa: E402
from app.store import AppData  # noqa: E402


class PlaybackSpeedTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def test_default_and_remembered(self):
        self.assertEqual(self.api.playback_speed(), {"playback_speed": 1.0, "keep_pitch": True})
        caps = self.api.capabilities()
        self.assertEqual((caps["playback_speed"], caps["keep_pitch"]), (1.0, True))
        self.assertEqual(self.api.set_playback_speed(0.75, False),
                         {"ok": True, "playback_speed": 0.75, "keep_pitch": False, "remembered": True})
        self.assertEqual((self.store.get_setting(backend.PLAYBACK_SPEED), self.store.get_setting(backend.KEEP_PITCH)),
                         (0.75, False))
        self.assertEqual(self.new_api().playback_speed(), {"playback_speed": 0.75, "keep_pitch": False})   # the next start
        self.assertEqual(self.api.set_playback_speed(2, True)["playback_speed"], 2.0)     # an int from JS: a float
        self.assertIsInstance(self.api.playback_speed()["playback_speed"], float)

    def test_bad_values_are_refused(self):
        for speed, keep in ((0.3, True), (3, True), (None, True), ("1", True), (True, True), (float("nan"), True),
                            (1, None), (1, "yes"), (1, 1)):
            r = self.api.set_playback_speed(speed, keep)
            self.assertEqual((r["ok"], r["error"]), (False, "Unknown playback speed."), (speed, keep))
        self.assertEqual(self.api.playback_speed(), {"playback_speed": 1.0, "keep_pitch": True})

    def test_a_damaged_setting_falls_back(self):
        self.api.set_playback_speed(1.5, False)
        for bad in ("fast", 0.3, 9, None, [1], True):
            self.store.set_setting(backend.PLAYBACK_SPEED, bad)
            self.assertEqual(self.api.playback_speed(), {"playback_speed": 1.0, "keep_pitch": False}, bad)
        self.store.set_setting(backend.PLAYBACK_SPEED, 0.5)
        for bad in ("no", 0, None, {}):
            self.store.set_setting(backend.KEEP_PITCH, bad)
            self.assertEqual(self.api.playback_speed(), {"playback_speed": 0.5, "keep_pitch": True}, bad)

    def test_a_second_window_keeps_it_for_the_session(self):
        second = AppData(os.path.join(self.tmp, "appdata"))                # the first holds the lock
        self.addCleanup(second.close)
        self.assertTrue(second.read_only)
        api = self.new_api(store=second)
        self.assertEqual(api.set_playback_speed(0.5, False),
                         {"ok": True, "playback_speed": 0.5, "keep_pitch": False, "remembered": False})
        self.assertEqual(api.playback_speed(), {"playback_speed": 0.5, "keep_pitch": False})
        self.assertEqual(api.capabilities()["playback_speed"], 0.5)
        self.assertEqual(self.api.playback_speed(), {"playback_speed": 1.0, "keep_pitch": True})   # not remembered
        self.assertEqual(self.store.get_setting(backend.PLAYBACK_SPEED), None)

    def test_no_store(self):
        api = backend.Api(self.m, self.emit, lambda start: None, self.dest, self.server, store=None)
        self.addCleanup(api.shutdown)
        self.assertEqual(api.playback_speed(), {"playback_speed": 1.0, "keep_pitch": True})
        self.assertEqual(api.set_playback_speed(1.25, True)["remembered"], False)
        self.assertEqual(api.playback_speed(), {"playback_speed": 1.25, "keep_pitch": True})


if __name__ == "__main__":
    unittest.main()
