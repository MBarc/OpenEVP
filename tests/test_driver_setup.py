import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.driver_setup import is_protected  # noqa: E402

PF = r"C:\Program Files"


@unittest.skipUnless(sys.platform == "win32", "Windows paths")
class ProtectedLocationTests(unittest.TestCase):
    def test_inside_program_files(self):
        self.assertTrue(is_protected(PF + r"\OpenEVP\_internal\driver\install-winusb.ps1", PF))

    def test_elsewhere_is_refused(self):
        self.assertFalse(is_protected(r"C:\Users\x\Downloads\app\driver\install-winusb.ps1", PF))
        self.assertFalse(is_protected(r"C:\Program Files Evil\x.ps1", PF))            # a prefix is not a parent
        self.assertFalse(is_protected(PF + r"\..\Users\x.ps1", PF))
        self.assertFalse(is_protected(PF + r"\x.ps1", None))


if __name__ == "__main__":
    unittest.main()
