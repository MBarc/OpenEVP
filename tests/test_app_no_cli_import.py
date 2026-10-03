"""app/ is the desktop app; it must never import sony_icd.cli (that is the
command-line downloader's own entry point, A11). Two checks: a static AST
scan of every file under app/ for that import, and a fresh-interpreter
check that importing app.backend / app.main does not pull it in as a
side effect (checking sys.modules in this process would be unreliable,
since other tests in the same run may have already imported sony_icd.cli).
"""
import ast
import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_DIR = os.path.join(ROOT, "app")


def _imports_the_cli(path):
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "sony_icd.cli" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "sony_icd.cli":
                return True
            if node.module == "sony_icd" and any(alias.name == "cli" for alias in node.names):
                return True
    return False


def _loads_the_cli(module_name):
    """(returncode, stdout) of a fresh interpreter that imports module_name
    and reports whether sony_icd.cli ended up in sys.modules."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module_name}, sys; print('sony_icd.cli' in sys.modules)"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr


class NoCliImportTests(unittest.TestCase):
    def test_no_file_under_app_imports_the_cli(self):
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(APP_DIR):
            for name in filenames:
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    if _imports_the_cli(path):
                        offenders.append(path)
        self.assertEqual(offenders, [], f"app/ files importing sony_icd.cli: {offenders}")

    def test_importing_app_backend_does_not_load_the_cli(self):
        code, out, err = _loads_the_cli("app.backend")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "False")

    def test_importing_app_main_does_not_load_the_cli(self):
        if importlib.util.find_spec("webview") is None:
            self.skipTest("pywebview is not installed")
        code, out, err = _loads_the_cli("app.main")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "False")


if __name__ == "__main__":
    unittest.main()
