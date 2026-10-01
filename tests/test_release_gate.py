"""The release gate (tests/release_gate.py): with OPENEVP_RELEASE_GATE=1 a
decoder test that cannot run fails instead of skipping, and the decoder's
tables and C core must be present and loadable."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import release_check  # noqa: E402
import release_gate  # noqa: E402


def _run(case_class):
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(case_class).run(result)
    return result


class DecoderPresentTests(unittest.TestCase):
    def test_tables_and_core_load(self):
        problem = release_gate.decoder_problem()
        if problem:
            release_gate.skip_or_fail(problem)


class GateTests(unittest.TestCase):
    def make_cases(self):
        class Method(unittest.TestCase):
            @release_gate.require(False, "no tables")
            def test_x(self):
                pass

        @release_gate.require(False, "no core")
        class Whole(unittest.TestCase):
            def setUp(self):
                pass

            def test_a(self):
                pass

            def test_b(self):
                pass

        class Runtime(unittest.TestCase):
            def test_x(self):
                release_gate.skip_or_fail("no tables")
        return Method, Whole, Runtime

    def test_outside_the_gate_missing_requirements_skip(self):
        with mock.patch.object(release_gate, "ENABLED", False):
            results = [_run(case) for case in self.make_cases()]
        for case, r in zip(("Method", "Whole", "Runtime"), results):
            self.assertEqual((len(r.failures), len(r.errors), len(r.skipped) > 0), (0, 0, True), case)

    def test_in_the_gate_missing_requirements_fail(self):
        with mock.patch.object(release_gate, "ENABLED", True):
            cases = self.make_cases()
            results = [_run(case) for case in cases]
        self.assertEqual([(len(r.failures), len(r.skipped)) for r in results], [(1, 0), (2, 0), (1, 0)])
        for r in results:
            self.assertIn("OPENEVP_RELEASE_GATE=1", r.failures[0][1])

    def test_a_met_requirement_runs_either_way(self):
        for enabled in (False, True):
            with mock.patch.object(release_gate, "ENABLED", enabled):
                class Ok(unittest.TestCase):
                    @release_gate.require(True, "unused")
                    def test_x(self):
                        pass
                r = _run(Ok)
            self.assertEqual((r.testsRun, len(r.failures), len(r.skipped)), (1, 0, 0))

    def test_damaged_tables_are_a_problem(self):
        from openevp.decoders.sony_lpec import tables
        # tables_problem() calls tables.load() with no path: it reads the
        # configuration's file in tables.DATA_DIR.
        with tempfile.TemporaryDirectory() as d:
            for name in ("lpec_tables.json", "lpec_sp_tables.json"):
                Path(d, name).write_text("not JSON", encoding="utf-8")
            with mock.patch.object(tables, "DATA_DIR", Path(d)):
                self.assertIn("damaged", release_gate.tables_problem())
                self.assertIn("damaged", release_gate.sp_tables_problem())
                self.assertIn("damaged", release_gate.decoder_problem())

    def test_missing_tables_and_core_are_problems(self):
        from openevp.decoders.sony_lpec import _core, tables
        missing = Path(__file__).with_name("no-such-tables.json")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tables, "DATA_DIR", Path(d)):
            self.assertIn("does not include the LPEC table data", release_gate.tables_problem())
            self.assertIn("does not include the LPEC SP table data", release_gate.sp_tables_problem())
        with mock.patch.object(_core, "available", return_value=False), \
                mock.patch.object(_core, "DLL_PATH", missing):
            self.assertIn("is not built", release_gate.core_problem())


class UiBundleTests(unittest.TestCase):
    """release_check: every file of app/ui must be bundled, byte for byte."""

    def write(self, folder, name, data=b"x"):
        path = os.path.join(folder, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_the_list_comes_from_the_source_ui_folder(self):
        names = release_check.ui_files(release_check.UI_SOURCE)
        self.assertTrue({"index.html", "app.js", "style.css", "favicon.svg", "vendor/wavesurfer.min.js",
                         "vendor/regions.min.js"} <= names)

    def test_missing_stale_and_extra_files_are_problems(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as built:
            for name in ("index.html", "app.js", "style.css", "vendor/a.js"):
                self.write(src, name, name.encode())
                self.write(built, name, name.encode())
            self.assertEqual(release_check.ui_bundle_problems(src, built), ([], 4))
            self.write(built, "app.js", b"an older app.js")
            os.remove(os.path.join(built, "vendor", "a.js"))
            self.write(built, "old.css")
            problems, _ = release_check.ui_bundle_problems(src, built)
            self.assertEqual(problems, ["the built app's UI has no vendor/a.js",
                                        "the built app's UI has old.css, which is not in app/ui",
                                        "the built app's UI file app.js differs from app/ui/app.js"])
            self.assertIn("no UI folder", release_check.ui_bundle_problems(src, os.path.join(built, "no"))[0][0])


class ExtensionBundleTests(unittest.TestCase):
    """release_check: lameenc (MP3 clips) must be bundled as an extension module."""

    def test_lameenc_is_looked_for_by_name(self):
        self.assertIn("lameenc", release_check.EXTENSIONS)
        with tempfile.TemporaryDirectory() as built:
            self.assertEqual(release_check.extension_problems(built),
                             ["the built app has no lameenc extension module (_internal/lameenc.*.pyd)"])
            open(os.path.join(built, "lameenc.txt"), "wb").close()               # not an extension module
            self.assertEqual(len(release_check.extension_problems(built)), 1)
            open(os.path.join(built, "lameenc.cp312-win_amd64.pyd"), "wb").close()
            self.assertEqual(release_check.extension_problems(built), [])
        self.assertEqual(len(release_check.extension_problems(os.path.join(built, "gone"))), 1)


class GuiSmokeCheckTests(unittest.TestCase):
    """release_check.check_gui reads OpenEVP.exe --smoke's exit code and report."""

    def run_check(self, code, report):
        def fake_run(args, timeout):
            self.assertEqual(args[1], "--smoke")
            if report is not None:
                with open(args[2], "w", encoding="utf-8") as f:
                    json.dump(report, f)
            return code, ""
        with mock.patch.object(release_check, "_run", fake_run), \
                mock.patch.object(release_check.os.path, "isfile", return_value=True):
            return release_check.check_gui()

    def test_a_good_report_passes(self):
        self.assertEqual(self.run_check(0, {"ok": True, "problems": [], "page": {"title": "OpenEVP"},
                                            "mp3": {"available": True, "version": "1.8.4", "bytes": 9000,
                                                    "decoder": True, "decoder_status": None,
                                                    "decoded": {"rate": 8000, "channels": 1, "seconds": 1.08}}}), [])

    def test_a_report_without_mp3_encoding_fails(self):
        self.assertEqual(self.run_check(0, {"ok": True, "problems": [], "page": {"title": "OpenEVP"}}),
                         ["GUI smoke: the report says nothing about MP3 encoding"])

    def test_a_report_without_mp3_decoding_fails(self):
        """A build from before MP3 recordings (its smoke test never decodes one) is not this release."""
        self.assertEqual(self.run_check(0, {"ok": True, "problems": [], "page": {"title": "OpenEVP"},
                                            "mp3": {"available": True, "version": "1.8.4", "bytes": 9000}}),
                         ["GUI smoke: the report says nothing about MP3 decoding"])

    def test_failures_are_reported(self):
        self.assertEqual(self.run_check(1, {"ok": False, "problems": ["app.js did not load in the page"]}),
                         ["GUI smoke: app.js did not load in the page"])
        self.assertIn("wrote no report", self.run_check(1, None)[0])
        self.assertEqual(self.run_check(0, {"ok": False, "problems": []}),
                         ["OpenEVP.exe --smoke exited 0 but its report is not ok"])


if __name__ == "__main__":
    unittest.main()
