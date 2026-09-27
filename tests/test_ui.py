"""The page (app/ui/app.js) with a stubbed backend, headless in Node: see ui_check.js."""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is not installed")
class PageTests(unittest.TestCase):
    def test_recorder_list_folders_and_export_menu(self):
        run = subprocess.run([NODE, os.path.join(HERE, "ui_check.js")], capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "ok"), run.stderr)

    def test_release_notes_render_as_markdown(self):
        run = subprocess.run([NODE, os.path.join(HERE, "notes_check.js")], capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "ok"), run.stderr)

    def test_the_page_parses(self):
        run = subprocess.run([NODE, "--check", os.path.join(HERE, "..", "app", "ui", "app.js")],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
