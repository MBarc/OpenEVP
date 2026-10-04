"""The installer's [Run] entries (installer/openevp.iss).

Inno Setup runs a [Run] entry without the postinstall flag during the install
step, BEFORE CurStepChanged(ssPostInstall), where this installer installs WebView2
and the recorder driver. An entry that starts the app must therefore be postinstall,
or an in-app update reopens OpenEVP while the driver step is still running.
postinstall entries run once Setup finishes, in a silent install too unless they
are "unchecked" (Inno 6.7: Setup.MainForm.pas, TMainForm.Install and Finish).
"""
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from app import updater  # noqa: E402

ISS = os.path.join(ROOT, "installer", "openevp.iss")


def section(text, name):
    """The non-comment, non-blank lines of one [Section]."""
    m = re.search(r"^\[" + re.escape(name) + r"\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip() and not ln.strip().startswith(";")]


def parse_entry(line):
    """'Key: "value"; Key2: value' -> {key: value}; Flags as a set."""
    entry = {}
    for part in re.findall(r'(\w+):\s*("[^"]*"|[^;]*)', line):
        key, value = part[0], part[1].strip().strip('"')
        entry[key] = set(value.split()) if key == "Flags" else value
    entry.setdefault("Flags", set())
    return entry


class RunSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ISS, encoding="utf-8") as f:
            cls.text = f.read()
        cls.entries = [parse_entry(ln) for ln in section(cls.text, "Run")]
        cls.app = [e for e in cls.entries if e["Filename"] == r"{app}\OpenEVP.exe"]

    def relaunch(self):
        [entry] = [e for e in self.app if "IsRelaunch" in e.get("Check", "")]
        return entry

    def test_two_entries_start_the_app(self):
        self.assertEqual(len(self.app), 2)

    def test_nothing_starts_before_post_install(self):
        """Every [Run] entry waits for ssPostInstall (WebView2 + driver)."""
        for entry in self.entries:
            self.assertIn("postinstall", entry["Flags"], entry)

    def test_update_relaunch_flags(self):
        entry = self.relaunch()
        self.assertEqual(entry["Flags"], {"postinstall", "nowait", "runasoriginaluser", "skipifnotsilent"})
        self.assertNotIn("unchecked", entry["Flags"])     # unchecked would never run in a silent install
        self.assertNotIn("skipifsilent", entry["Flags"])
        self.assertIn("HasWebView2", entry["Check"])

    def test_interactive_start_checkbox(self):
        [entry] = [e for e in self.app if "IsRelaunch" not in e.get("Check", "")]
        self.assertEqual(entry["Description"], "Start OpenEVP")
        self.assertEqual(entry["Flags"], {"postinstall", "nowait", "skipifsilent"})
        self.assertEqual(entry["Check"], "HasWebView2")

    def test_driver_step_is_post_install(self):
        """The step the relaunch must wait for lives in CurStepChanged(ssPostInstall)."""
        m = re.search(r"procedure CurStepChanged\(CurStep: TSetupStep\);(.*?)\nend;", self.text, re.S)
        body = m.group(1)
        self.assertIn("if CurStep <> ssPostInstall then", body)
        self.assertIn("RunDriverScript('install-winusb.ps1')", body)
        self.assertIn("MicrosoftEdgeWebview2Setup.exe", body)

    def test_updater_runs_setup_silent_with_relaunch(self):
        """skipifnotsilent + IsRelaunch match what the app's updater passes."""
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.launch(os.path.join("C:\\", "x", "OpenEVP-Setup-1.0.0.exe"))
        args = popen.call_args[0][0]
        self.assertIn("/SILENT", args)
        self.assertIn("/RELAUNCH", args)
        self.assertNotIn("/VERYSILENT", args)
        self.assertIn('CompareText(ParamStr(I), \'/RELAUNCH\') = 0', self.text)


if __name__ == "__main__":
    unittest.main()
