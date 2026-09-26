import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class MainImportTests(unittest.TestCase):
    """app/main.py is only run by the desktop app; importing it here catches syntax and
    wiring mistakes that no other test would."""

    def test_main_imports(self):
        try:
            importlib.import_module("webview")
        except ImportError:
            self.skipTest("pywebview is not installed")
        main = importlib.import_module("app.main")
        self.assertTrue(callable(main.main))

    def main(self):
        try:
            importlib.import_module("webview")
        except ImportError:
            self.skipTest("pywebview is not installed")
        return importlib.import_module("app.main")

    def test_store_lives_in_appdata_and_a_failure_turns_marks_off(self):
        main = self.main()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"APPDATA": d}):
            store, problems = main._open_store()
            self.assertEqual(problems, [])
            store.close()
            self.assertTrue(os.path.isdir(os.path.join(d, "OpenEVP")))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"APPDATA": d}):
            open(os.path.join(d, "OpenEVP"), "w").close()        # a file where the folder should be
            store, problems = main._open_store()
            self.assertIsNone(store)
            self.assertIn("EVP marks are off", problems[0])

    def test_any_store_failure_still_lets_the_app_start(self):
        main = self.main()
        failure = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"APPDATA": d}), \
                mock.patch.object(main, "AppData", side_effect=failure):
            store, problems = main._open_store()
        self.assertIsNone(store)
        self.assertEqual(len(problems), 1)
        self.assertIn("EVP marks are off", problems[0])

    def test_close_question_mentions_a_running_backup(self):
        main = self.main()
        self.assertIsNone(main._close_question(False, False))
        self.assertEqual(main._close_question(False, True)[0], "Backup in progress")
        title, text = main._close_question(True, True)
        self.assertEqual(title, "Export in progress")
        self.assertIn("backup of a marked recording", text)
        self.assertNotIn("backup", main._close_question(True, False)[1])

    def test_close_question_mentions_a_wav_being_saved_with_marks(self):
        main = self.main()
        title, text = main._close_question(False, False, True)
        self.assertEqual(title, "Export in progress")
        self.assertIn("WAV with its EVP marks", text)
        self.assertIn("backup of a marked recording", main._close_question(False, True, True)[1])


if __name__ == "__main__":
    unittest.main()
