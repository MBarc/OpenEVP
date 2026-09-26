import importlib
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
