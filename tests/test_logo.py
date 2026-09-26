import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import make_logo  # noqa: E402

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")


class LogoTests(unittest.TestCase):
    def test_committed_assets_match_the_generator(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d, mock.patch.object(make_logo, "ASSETS", d), \
                contextlib.redirect_stdout(io.StringIO()):
            make_logo.main()
            for name in ("logo.svg",):
                with open(os.path.join(d, name)) as a, open(os.path.join(ASSETS, name)) as b:
                    self.assertEqual(a.read(), b.read(), f"{name} is stale: run tools/make_logo.py")
        ico = Image.open(os.path.join(ASSETS, "st25.ico"))
        self.assertEqual(sorted(ico.info["sizes"]), [(s, s) for s in make_logo.ICO_SIZES])

    def test_favicon_is_the_logo(self):
        with open(os.path.join(ASSETS, "logo.svg")) as a, \
                open(os.path.join(os.path.dirname(__file__), "..", "app", "ui", "favicon.svg")) as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
