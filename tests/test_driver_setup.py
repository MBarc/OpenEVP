import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import driver_setup  # noqa: E402
from app.driver_setup import ManifestMissing, is_protected  # noqa: E402

PF = r"C:\Program Files"
DRIVER_DIR = os.path.join(os.path.dirname(driver_setup.__file__), "driver")


@unittest.skipUnless(sys.platform == "win32", "Windows paths")
class ProtectedLocationTests(unittest.TestCase):
    def test_inside_program_files(self):
        self.assertTrue(is_protected(PF + r"\OpenEVP\_internal\driver\install-winusb.ps1", PF))

    def test_elsewhere_is_refused(self):
        self.assertFalse(is_protected(r"C:\Users\x\Downloads\app\driver\install-winusb.ps1", PF))
        self.assertFalse(is_protected(r"C:\Program Files Evil\x.ps1", PF))            # a prefix is not a parent
        self.assertFalse(is_protected(PF + r"\..\Users\x.ps1", PF))
        self.assertFalse(is_protected(PF + r"\x.ps1", None))


class EnsureManifestTests(unittest.TestCase):
    """_ensure_manifest: from source, a missing app/driver/models.json (a build
    output tools/make_driver_manifest.py writes) is generated on the fly."""

    def test_frozen_never_generates(self):
        with mock.patch.object(sys, "_MEIPASS", "C:\\fake", create=True), \
                mock.patch("subprocess.run") as run:
            driver_setup._ensure_manifest(os.path.join("C:\\fake", "driver"))
        run.assert_not_called()

    def test_present_manifest_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, driver_setup.MANIFEST), "w", encoding="utf-8").close()
            with mock.patch("subprocess.run") as run:
                driver_setup._ensure_manifest(d)
        run.assert_not_called()

    def test_generation_failure_names_the_command(self):
        err = subprocess.CalledProcessError(1, ["python", "tools/make_driver_manifest.py"],
                                            stderr="make_driver_manifest: boom")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("subprocess.run", side_effect=err):
            with self.assertRaises(ManifestMissing) as cm:
                driver_setup._ensure_manifest(d)
        msg = str(cm.exception)
        self.assertIn("python tools/make_driver_manifest.py", msg)
        self.assertIn("boom", msg)

    def generate_twice_and_check(self):
        """Generate the manifest from the current registry in two separate
        temporary driver folders (the tool, end to end): same bytes both times."""
        generated = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as d:
                driver_setup._ensure_manifest(d)
                with open(os.path.join(d, driver_setup.MANIFEST), "rb") as f:
                    generated.append(f.read())
        self.assertTrue(json.loads(generated[0].decode("utf-8"))["models"])
        self.assertEqual(generated[0], generated[1])        # same registry in, same bytes out

    def keep_real_manifest(self):
        """The checkout's app/driver/models.json (a git-ignored build output):
        its bytes or None; it is put back as it was after the test."""
        real = os.path.join(DRIVER_DIR, driver_setup.MANIFEST)
        original = None
        if os.path.isfile(real):
            with open(real, "rb") as f:
                original = f.read()

        def restore():
            if original is None:
                if os.path.isfile(real):
                    os.remove(real)
                return
            with open(real, "rb") as f:
                current = f.read()
            if current != original:
                with open(real, "wb") as f:
                    f.write(original)
        self.addCleanup(restore)
        return real, original

    def test_missing_manifest_is_really_generated(self):
        """A driver folder without the manifest gets one from the current
        registry; the checkout's own build output is neither compared with nor
        changed."""
        real, original = self.keep_real_manifest()
        self.generate_twice_and_check()
        if original is None:
            self.assertFalse(os.path.isfile(real))
        else:
            with open(real, "rb") as f:
                self.assertEqual(f.read(), original)

    def test_a_stale_build_output_does_not_fail_the_suite(self):
        """After an intentional registry/INF/driver version change the built
        models.json is stale until build_windows.ps1 regenerates it; the suite
        (which the build runs first) must still pass."""
        real, _original = self.keep_real_manifest()
        with open(real, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "driver_version": "0.0.0.1", "driver_date": "01/01/2020",
                       "models": [], "inf": "stale"}, f)
        self.generate_twice_and_check()
        with open(real, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["inf"], "stale")        # never overwritten by a test

class BuildOrderTests(unittest.TestCase):
    def test_the_build_regenerates_the_manifest_before_the_tests(self):
        """build_windows.ps1 writes models.json from the current registry before
        running the suite, so a registry/INF change rebuilds cleanly."""
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "build_windows.ps1"), encoding="utf-8") as f:
            script = f.read()
        make = script.index(r"python tools\make_driver_manifest.py")
        self.assertLess(make, script.index("python -m unittest discover"))
        self.assertLess(make, script.index('--add-data "app/driver;driver"'))

if __name__ == "__main__":
    unittest.main()
