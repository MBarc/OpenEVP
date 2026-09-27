"""The driver manifest (tools/make_driver_manifest.py) and the scripts that read it.

The ST25-only INF must stay byte for byte the one v0.5.0-v0.7.2 installed
(same DriverVer 1.0.1.0, so an upgrade reinstalls the package already in the
driver store); more models add entries; placeholders and non-WinUSB models add
nothing; the INF never changes without a new driver version.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import make_driver_manifest as mdm  # noqa: E402
from fakes import fake_models  # noqa: E402
from openevp import recorders  # noqa: E402
from openevp.recorders import base  # noqa: E402

DRIVER = os.path.join(ROOT, "app", "driver")

# The INF install-winusb.ps1 wrote in v0.7.0-v0.7.2 (its here-string, as Set-Content wrote it).
V072_INF = "\r\n".join([
    "[Version]",
    'Signature   = "$Windows NT$"',
    "Class       = USBDevice",
    "ClassGUID   = {88BAE032-5A81-49f0-BC3D-A4FF138216D6}",
    "Provider    = %Provider%",
    "CatalogFile = st25_winusb.cat",
    "DriverVer   = 09/25/2026,1.0.1.0",
    "",
    "[Manufacturer]",
    "%Provider% = Devices, NTamd64, NTx86, NTarm64",
    "",
    "[Devices.NTamd64]",
    r"%DeviceName% = USB_Install, USB\VID_054C&PID_0103",
    "",
    "[Devices.NTx86]",
    r"%DeviceName% = USB_Install, USB\VID_054C&PID_0103",
    "",
    "[Devices.NTarm64]",
    r"%DeviceName% = USB_Install, USB\VID_054C&PID_0103",
    "",
    "[USB_Install]",
    "Include = winusb.inf",
    "Needs   = WINUSB.NT",
    "",
    "[USB_Install.Services]",
    "Include = winusb.inf",
    "Needs   = WINUSB.NT.Services",
    "",
    "[USB_Install.HW]",
    "AddReg = Dev_AddReg",
    "",
    "[Dev_AddReg]",
    'HKR,,DeviceInterfaceGUIDs,0x10000,"{7C3C3F6B-2E54-4F0B-9C1B-5D2A8B3E0A25}"',
    "",
    "[Strings]",
    'Provider   = "OpenEVP"',
    'DeviceName = "Sony IC Recorder (ST) - WinUSB"',
])


class Second(base.Model):
    """A made-up second WinUSB model with two USB ids."""
    model_id = "test-second"
    name = "Test Recorder 2"
    usb_ids = ((0x1234, 0x00AB), (0x1234, 0x00AC))
    needs_winusb = True


class MassStorage(base.Model):
    model_id = "test-mass-storage"
    name = "Mass Storage Recorder"
    usb_ids = ((0x1234, 0x0F00),)
    needs_winusb = False


def st25():
    return recorders.get("sony-icd-st25")


def releases_for(models, version="1.0.2.0", date="10/01/2026"):
    """The shipped releases plus one for these models (what a developer appends)."""
    return mdm.DRIVER_RELEASES + [(version, date, mdm.body_hash(mdm.winusb_models(models)))]


class ShippedManifestTests(unittest.TestCase):
    def test_st25_only_today(self):
        m = mdm.manifest()
        self.assertEqual(m["schema"], 1)
        self.assertEqual((m["driver_version"], m["driver_date"]), ("1.0.1.0", "09/25/2026"))
        self.assertEqual(m["models"], [{"model_id": "sony-icd-st25", "name": "Sony ICD-ST25",
                                        "hardware_ids": [r"USB\VID_054C&PID_0103"]}])

    def test_the_st25_inf_is_v072s_byte_for_byte(self):
        self.assertEqual(mdm.manifest()["inf"], V072_INF)

    def test_placeholders_never_get_an_entry(self):
        placeholders = [m for m in recorders.models() if not m.supported]
        self.assertTrue(placeholders)                      # the ST10 and RR-DR60 slots
        self.assertEqual(mdm.winusb_models(recorders.models()), [st25()])

    def test_the_shipped_inf_is_pinned_by_the_last_release(self):
        """Fails when the INF changes without a DriverVer bump (see DRIVER_RELEASES)."""
        self.assertEqual(mdm.release_for([st25()]), mdm.DRIVER_RELEASES[-1][:2])
        mdm.check_releases(mdm.DRIVER_RELEASES)

    def test_written_as_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "models.json")
            with open(os.devnull, "w") as quiet:
                stdout, sys.stdout = sys.stdout, quiet
                try:
                    self.assertEqual(mdm.main(["--out", out]), 0)
                finally:
                    sys.stdout = stdout
            with open(out, encoding="ascii") as f:
                self.assertEqual(json.load(f), mdm.manifest())


class TwoModelTests(unittest.TestCase):
    def test_inf_for_two_models(self):
        models = [st25(), Second(), MassStorage()]
        m = mdm.manifest(models, releases_for(models))
        self.assertEqual([x["model_id"] for x in m["models"]], ["sony-icd-st25", "test-second"])
        self.assertEqual(m["models"][1]["hardware_ids"], [r"USB\VID_1234&PID_00AB", r"USB\VID_1234&PID_00AC"])
        lines = m["inf"].split("\r\n")
        self.assertIn("DriverVer   = 10/01/2026,1.0.2.0", lines)
        for arch in ("NTamd64", "NTx86", "NTarm64"):
            at = lines.index(f"[Devices.{arch}]")
            self.assertEqual(lines[at + 1:at + 5], [
                r"%DeviceName% = USB_Install, USB\VID_054C&PID_0103",
                r"%DeviceName2% = USB_Install, USB\VID_1234&PID_00AB",
                r"%DeviceName2% = USB_Install, USB\VID_1234&PID_00AC",
                ""])
        self.assertEqual(lines[-3:], ['Provider   = "OpenEVP"', 'DeviceName = "Sony IC Recorder (ST) - WinUSB"',
                                      'DeviceName2 = "Test Recorder 2 - WinUSB"'])
        self.assertNotIn("0F00", m["inf"])                  # the mass-storage model
        # Everything else is the ST25 INF unchanged.
        rest = [l for l in lines if "DeviceName2" not in l and not l.startswith("DriverVer")]
        self.assertEqual(rest, [l for l in V072_INF.split("\r\n") if not l.startswith("DriverVer")])

    def test_through_the_registry_with_the_fake_models(self):
        """FakeAlpha uses WinUSB, FakeBeta does not: only FakeAlpha joins the ST25."""
        fake_models.install(self)
        m = mdm.manifest(None, releases_for(recorders.models()))
        self.assertEqual([(x["model_id"], x["hardware_ids"]) for x in m["models"]],
                         [("sony-icd-st25", [r"USB\VID_054C&PID_0103"]), ("fake-alpha", [r"USB\VID_F0F0&PID_0001"])])
        self.assertIn('DeviceName2 = "Fake Alpha - WinUSB"', m["inf"])
        self.assertNotIn("PID_0002", m["inf"])

    def test_a_new_model_without_a_new_driver_version_fails(self):
        with self.assertRaises(mdm.ManifestError) as caught:
            mdm.manifest([st25(), Second()])
        self.assertIn("append to DRIVER_RELEASES", str(caught.exception))
        self.assertIn(mdm.body_hash([st25(), Second()]), str(caught.exception))

    def test_releases_must_rise(self):
        h = mdm.body_hash([st25(), Second()])
        for bad in (mdm.DRIVER_RELEASES + [("1.0.1.0", "10/01/2026", h)],     # same version
                    mdm.DRIVER_RELEASES + [("1.0.0.9", "10/01/2026", h)],     # lower
                    mdm.DRIVER_RELEASES + [("1.0.2.0", "01/01/2026", h)],     # earlier date
                    mdm.DRIVER_RELEASES + [("1.0.2", "10/01/2026", h)],       # three parts
                    mdm.DRIVER_RELEASES + [("1.0.2.0", "10/01/2026", mdm.DRIVER_RELEASES[0][2])]):  # old body
            with self.subTest(bad=bad[-1]), self.assertRaises(mdm.ManifestError):
                mdm.manifest([st25(), Second()], bad)

    def test_going_back_to_an_older_inf_needs_a_new_version_too(self):
        models = [st25(), Second()]
        releases = releases_for(models)
        with self.assertRaises(mdm.ManifestError):
            mdm.manifest([st25()], releases)

    def test_bad_models_are_refused(self):
        class NoIds(Second):
            model_id, usb_ids = "no-ids", ()

        class Quoted(Second):
            winusb_name = 'Evil" %x%'

        class Unicode(Second):
            name = "Récorder"

        class Percent(Second):
            winusb_name = "100% recorder"

        class LineEnd(Second):
            winusb_name = "Evil\n"

        class QuotedModelName(Second):
            name, winusb_name = 'Evil" name', "Fine name"

        class LineEndModelId(Second):
            model_id = "evil\n"

        for bad in (NoIds(), Quoted(), Unicode(), Percent(), LineEnd(), QuotedModelName(), LineEndModelId()):
            with self.subTest(bad=bad), self.assertRaises(mdm.ManifestError):
                mdm.inf_text(mdm.winusb_models([bad]), "10/01/2026", "1.0.2.0")
        with self.assertRaises(mdm.ManifestError):
            mdm.inf_text([], "10/01/2026", "1.0.2.0")


POWERSHELL = shutil.which("powershell")


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell")
class ScriptTests(unittest.TestCase):
    """manifest.ps1 as install-winusb.ps1 uses it (never the install itself)."""

    def ps(self, command):
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", command], capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()

    def write_inf(self, data):
        """Read data through Read-DriverManifest and write its INF as the setup does;
        returns (exit code, output, INF bytes or None)."""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "models.json")
            mdm.write(src, data)
            inf = os.path.join(d, "st25_winusb.inf")
            script = os.path.join(DRIVER, "manifest.ps1")
            code, out = self.ps(f"$ErrorActionPreference = 'Stop'; . '{script}'; "
                                f"$m = Read-DriverManifest '{src}'; "
                                f"Set-Content -Path '{inf}' -Encoding ASCII -Value $m.Inf; "
                                f"'{{0}}|{{1}}' -f $m.Version, (@($m.Models | ForEach-Object {{ @($_.hardware_ids) }}) -join ',')")
            written = None
            if os.path.exists(inf):
                with open(inf, "rb") as f:
                    written = f.read()
        return code, out, written

    def test_the_st25_package_is_written_as_in_v072(self):
        code, out, written = self.write_inf(mdm.manifest())
        self.assertEqual(code, 0, out)
        self.assertEqual(out, r"1.0.1.0|USB\VID_054C&PID_0103")
        self.assertEqual(written, (V072_INF + "\r\n").encode("ascii"))

    def test_two_models(self):
        models = [st25(), Second()]
        code, out, written = self.write_inf(mdm.manifest(models, releases_for(models)))
        self.assertEqual(code, 0, out)
        self.assertEqual(out, r"1.0.2.0|USB\VID_054C&PID_0103,USB\VID_1234&PID_00AB,USB\VID_1234&PID_00AC")
        self.assertIn(rb"%DeviceName2% = USB_Install, USB\VID_1234&PID_00AC", written)

    def test_a_bad_manifest_stops_the_setup(self):
        good = mdm.manifest()
        hw = r"USB\VID_054C&PID_0103"
        bad = {
            "schema": dict(good, schema=2),
            "version": dict(good, driver_version="1.0"),
            "no models": dict(good, models=[]),
            "wildcard id": dict(good, models=[dict(good["models"][0], hardware_ids=["USB\\VID_054C&PID_*"])]),
            "unlisted INF device": dict(good, inf=good["inf"].replace(
                f"%DeviceName% = USB_Install, {hw}\r\n\r\n[Devices.NTx86]",
                f"%DeviceName% = USB_Install, {hw}\r\n%DeviceName% = USB_Install, USB\\VID_1111&PID_2222"
                "\r\n\r\n[Devices.NTx86]")),
            "DriverVer mismatch": dict(good, driver_version="1.0.9.0"),
            "other catalog": dict(good, inf=good["inf"].replace("st25_winusb.cat", "x.cat")),
            # Review fix round 1: anything but the template is refused, whatever its spelling.
            "unspaced device line": dict(good, inf=good["inf"].replace(
                "[Devices.NTamd64]\r\n", "[Devices.NTamd64]\r\n%DeviceName%=USB_Install,USB\\VID_1111&PID_2222\r\n")),
            "device line elsewhere": dict(good, inf=good["inf"].replace(
                "[USB_Install]\r\n", f"[USB_Install]\r\n%DeviceName% = USB_Install, {hw}\r\n")),
            "sections differ": dict(good, inf=good["inf"].replace(
                f"[Devices.NTx86]\r\n%DeviceName% = USB_Install, {hw}",
                "[Devices.NTx86]\r\n%DeviceName% = USB_Install, USB\\VID_1111&PID_2222")),
            "undefined name key": dict(good, inf=good["inf"].replace("%DeviceName% =", "%DeviceName7% =")),
            "name twice": dict(good, inf=good["inf"] + '\r\nDeviceName = "Again"'),
            "quoted INF name": dict(good, inf=good["inf"].replace(
                '"Sony IC Recorder (ST) - WinUSB"', '"Evil" name %x%"')),
            "percent INF name": dict(good, inf=good["inf"].replace(
                '"Sony IC Recorder (ST) - WinUSB"', '"100% Evil"')),
            "quoted model name": dict(good, models=[dict(good["models"][0], name='Evil" name %x%')]),
            "model name with a line end": dict(good, models=[dict(good["models"][0], name="Evil\n")]),
            "extra AddReg": dict(good, inf=good["inf"].replace(
                "[Dev_AddReg]\r\n", '[Dev_AddReg]\r\nHKR,,Evil,0x10000,"x"\r\n')),
            "extra directive": dict(good, inf=good["inf"].replace(
                "[USB_Install]\r\n", "[USB_Install]\r\nCopyFiles = Evil\r\n")),
            "extra section": dict(good, inf=good["inf"] + "\r\n\r\n[Evil]\r\nCopyFiles = x"),
            "lone line feed": dict(good, inf=good["inf"].replace("[USB_Install]\r\n", "[USB_Install]\nx\r\n")),
            "tab": dict(good, inf=good["inf"].replace("Include = winusb.inf", "Include =\twinusb.inf", 1)),
        }
        for name, data in bad.items():
            with self.subTest(name):
                code, out, written = self.write_inf(data)
                self.assertNotEqual(code, 0)
                self.assertIn("driver manifest", out)
                self.assertIsNone(written)

    def test_a_missing_manifest_stops_the_setup(self):
        script = os.path.join(DRIVER, "manifest.ps1")
        code, out = self.ps(f"$ErrorActionPreference = 'Stop'; . '{script}'; "
                            f"Read-DriverManifest '{os.path.join(DRIVER, 'no-such.json')}'")
        self.assertNotEqual(code, 0)
        self.assertIn("is missing", out)

    def test_the_scripts_parse(self):
        for name in ("install-winusb.ps1", "uninstall-winusb.ps1", "manifest.ps1"):
            path = os.path.join(DRIVER, name)
            code, out = self.ps(f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                                f"'{path}', [ref]$null, [ref]$e); if ($e) {{ $e | Out-String; exit 1 }}")
            self.assertEqual(code, 0, f"{name}: {out}")


class ScriptTextTests(unittest.TestCase):
    """What the scripts must keep (read as text; they are never run here)."""

    def read(self, name):
        with open(os.path.join(DRIVER, name), encoding="ascii") as f:
            return f.read()

    def test_no_device_is_hard_coded(self):
        for name in ("install-winusb.ps1", "uninstall-winusb.ps1"):
            self.assertNotIn("VID_", self.read(name))
            self.assertNotIn("DriverVer   =", self.read(name))
            self.assertNotIn("$DriverDate", self.read(name))
            self.assertNotIn('[version]"', self.read(name))

    def test_package_names_are_the_ones_every_version_used(self):
        install, uninstall = self.read("install-winusb.ps1"), self.read("uninstall-winusb.ps1")
        self.assertEqual(mdm.INF_NAME, "st25_winusb.inf")
        self.assertIn(f'-like "*\\{mdm.INF_NAME}"', install)
        self.assertIn(f'-like "*\\{mdm.INF_NAME}"', uninstall)
        self.assertIn(f'Join-Path $pkg "{mdm.INF_NAME}"', install)
        self.assertIn(f'Join-Path $pkg "{mdm.CAT_NAME}"', install)
        self.assertIn(f"CatalogFile = {mdm.CAT_NAME}", mdm.manifest()["inf"])

    def test_hardening_is_kept(self):
        install, uninstall = self.read("install-winusb.ps1"), self.read("uninstall-winusb.ps1")
        for text in (install, uninstall):
            self.assertIn('$env:PSModulePath = Join-Path $sys "WindowsPowerShell\\v1.0\\Modules"', text)
            self.assertIn('$pnputil = Join-Path $sys "pnputil.exe"', text)
        self.assertIn('/inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F"', install)
        self.assertIn("-DeleteKey", install)
        self.assertIn("$okCodes = 0, 259, 3010", install)
        self.assertIn('. (Join-Path $PSScriptRoot "manifest.ps1")', install)
        self.assertIn('Read-DriverManifest (Join-Path $PSScriptRoot "models.json")', install)


if __name__ == "__main__":
    unittest.main()
