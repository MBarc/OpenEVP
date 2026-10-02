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

    def test_recycle_is_owned_by_the_window_and_passes_before(self):
        main = self.main()
        window = mock.Mock()
        window.native.Handle.ToInt64.return_value = 4242
        before = mock.Mock()
        with mock.patch.object(main.folders, "recycle") as recycle:
            main._recycler(window)("C:/lib/x.wav", before=before)   # a folder delete passes before=
            main._recycler(window)("C:/lib/y.wav")
        self.assertEqual(recycle.call_args_list,
                         [mock.call("C:/lib/x.wav", owner=4242, before=before),
                          mock.call("C:/lib/y.wav", owner=4242, before=None)])

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

    def test_close_question_has_its_own_words_for_a_clips_job(self):
        main = self.main()
        title, text = main._close_question(False, False, False, True)
        self.assertEqual((title, text), ("Export in progress",
                                         "Clips are still being exported. Stop after the current recording and close?"))
        self.assertIn("backup of a marked recording", main._close_question(False, True, False, True)[1])

    def test_only_an_explicit_smoke_flag_starts_the_smoke_test(self):
        """--smoke is for tools/release_check.py; any other command line runs the app as before."""
        main = self.main()
        self.assertEqual(main._smoke_request([]), (False, None))
        self.assertEqual(main._smoke_request(["/RELAUNCH"]), (False, None))
        self.assertEqual(main._smoke_request(["x", "--smoke"]), (False, None))
        self.assertEqual(main._smoke_request(["--smoke"]), (True, None))
        self.assertEqual(main._smoke_request(["--smoke", "r.json"]), (True, "r.json"))
        with mock.patch.object(main, "_run_app") as run, mock.patch.object(main, "_smoke_main") as smoke:
            self.assertEqual(main.main([]), 0)
        run.assert_called_once_with()
        smoke.assert_not_called()

    def test_smoke_reports_a_failed_start_with_exit_1(self):
        main = self.main()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(main, "_run_app", side_effect=RuntimeError("no WebView2")):
            report = os.path.join(d, "r.json")
            self.assertEqual(main.main(["--smoke", report]), 1)
            with open(report, encoding="utf-8") as f:
                self.assertIn("no WebView2", f.read())

    def test_smoke_checks_both_decoders(self):
        """--smoke fails a build whose LPEC LP, SP or ST decoder does not load, or
        loads in slow mode; here (from source) they load, or the gate says why not."""
        main = self.main()
        report = {"problems": []}
        main._smoke_decoders(report)
        self.assertEqual(set(report["decoders"]), {"lpec", "lpec_sp", "lpec_st"})
        if report["decoders"]["lpec_st"]["available"]:
            self.assertEqual(report["decoders"]["lpec_st"]["decoded"], 0)
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec_st": None}):
            report = {"problems": []}
            main._smoke_decoders(report)
        self.assertIn("LPEC ST (ICD-ST10) decoding is not available: "
                      "LPEC ST (ICD-ST10) playback is not included in this build", report["problems"])

    def test_smoke_reports_mp3_encoding(self):
        """--smoke encodes an MP3 clip and decodes it back, so a build without lameenc
        or without the MP3 decoder's C core fails it."""
        main = self.main()
        from openevp import mp3
        from openevp.decoders import mp3 as mp3dec
        from openevp.decoders.mp3 import _core
        report = {"problems": []}
        main._smoke_mp3(report)
        if mp3.available() and mp3dec.available():
            self.assertEqual(report["problems"], [])
            self.assertTrue(report["mp3"]["available"] and report["mp3"]["bytes"] > 0)
            self.assertTrue(report["mp3"]["version"])
            self.assertEqual((report["mp3"]["decoder"], report["mp3"]["decoded"]["rate"],
                              report["mp3"]["decoded"]["channels"]), (True, 8000, 1))
            self.assertTrue(0.9 <= report["mp3"]["decoded"]["seconds"] <= 1.3, report["mp3"])
        with mock.patch.object(mp3, "lameenc", None), mock.patch.object(_core, "_lib", None):
            report = {"problems": []}
            main._smoke_mp3(report)
            why = mp3dec.reason()
        self.assertIn("mp3_core.dll", why)
        self.assertEqual((report["mp3"]["available"], report["mp3"]["version"], report["mp3"]["decoder"]),
                         (False, None, False))
        self.assertEqual(report["problems"], ["MP3 decoding is not available: " + why,
                                              "MP3 encoding is not available: " + mp3.UNAVAILABLE])

    def test_smoke_lists_every_ui_file(self):
        main = self.main()
        files = main.ui_files(main._ui_dir())
        self.assertTrue({"index.html", "app.js", "style.css", "favicon.svg", "vendor/wavesurfer.min.js",
                         "vendor/regions.min.js"} <= set(files))


if __name__ == "__main__":
    unittest.main()
