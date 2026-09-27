"""Release check: run after build_windows.ps1, before publishing a release.

    python tools/release_check.py

Automated (exits non-zero if any fails):
  1. the whole test suite in the release gate (OPENEVP_RELEASE_GATE=1): the
     decoder's golden tests must run, not skip (tables + C core present and
     intact), and no ResourceWarning may appear; reports how many decoder
     tests ran;
  2. the built command-line tool (dist\\openevp-st25.exe): --version, and
     --check-wav on the 10-minute test vector (the fast decoder, not slow mode);
  3. the built app (dist\\OpenEVP\\): every module of app/ and openevp/ is frozen
     into it, and the decoder's tables and DLL, libusb, the icon and the driver
     files (with the manifest) are bundled, identical to the sources; so is every
     file of the UI (app/ui/ in the source tree: index.html, app.js, style.css,
     the favicon, vendor/...), with nothing else in the bundled UI folder;
  4. the built app starts (OpenEVP.exe --smoke): the backend and the WebView2
     page in a hidden window, the page loads its scripts and styles and can
     fetch every bundled UI file, and the JS bridge answers capabilities().

Then prints the manual checklist (a real ICD-ST25, the driver, the updater).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")
DIST = os.path.join(REPO, "dist")
CLI = os.path.join(DIST, "openevp-st25.exe")
APP_DIR = os.path.join(DIST, "OpenEVP")
APP_EXE = os.path.join(APP_DIR, "OpenEVP.exe")
INTERNAL = os.path.join(APP_DIR, "_internal")
LONG_VECTOR = os.path.join(TESTS, "vectors", "long-mixed-10min.dvf")
LONG_SECONDS = 600.0                        # 9,375 frames x 512 samples at 8000 Hz

# Test modules whose every test exercises the LPEC decoder, and single decoder
# tests elsewhere (the marks fingerprint of decoded audio, the real decoder
# through the app and the library, the release gate's own presence check).
DECODER_MODULES = ("test_lpec_bitstream", "test_lpec_core", "test_lpec_vectors")
DECODER_TESTS = (
    "test_audio_server.RealDecoderFingerprintTests.test_fp_matches_real_decoded_audio_and_survives_markers",
    "test_wavinfo.GoldenFingerprintTests.test_fingerprint_of_the_decoded_vector",
    "test_library.LibraryTests.test_dvf_and_its_wav_share_fp_real_decoder",
    "test_migration.V072AppDataTests.test_the_dvf_decodes_to_the_same_recording_and_exports_the_same_wav",
    "test_release_gate.DecoderPresentTests.test_tables_and_core_load",
)
# Decoder tests that need research data a release build does not have: they may skip.
RESEARCH_ONLY = ("test_lpec_real", "test_lpec_tables")

MANUAL = """\
MANUAL CHECKS (the owner, before publishing)

Real Sony ICD-ST25, in the built app (dist\\OpenEVP\\OpenEVP.exe):
  [ ] It is listed with its owner name; folders A-E and their recordings list.
  [ ] A recording plays (waveform, audio).
  [ ] Export .dvf and WAV: files and names as before; running it again skips them.
  [ ] Mark a recording: the backup is saved (.dvf + WAV with the marks); unplug/replug keeps the marks.
  [ ] Library: the exported files are listed with their marks; play one, export marked.
Command-line tool (dist\\openevp-st25.exe): --list, then a download with --wav.

Driver (a test PC or VM):
  [ ] Fresh install of the new setup: the WinUSB driver installs; the recorder is found.
  [ ] Upgrade over v0.7.2: the driver is updated (DriverVer); marks, settings and library kept.
  [ ] Uninstall: the driver package is removed; the recorder goes back to its default driver.

Updater:
  [ ] From an installed v0.7.2, "Check for updates" finds this release, downloads it
      (signature verified) and installs it; the app restarts on the new version.
"""


# ---- 1. the test suite in the release gate ---------------------------------------

def _is_decoder_test(test_id):
    module = test_id.split(".", 1)[0]
    return module in DECODER_MODULES or module in RESEARCH_ONLY or test_id in DECODER_TESTS


def _run_suite_here(summary_path):
    """Child process: run the suite, write each test's outcome as JSON."""
    class Recording(unittest.TextTestResult):
        outcomes = {}

        def addSuccess(self, test):
            super().addSuccess(test)
            self.outcomes[test.id()] = "ok"

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self.outcomes[test.id()] = "skipped: " + reason

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.outcomes[test.id()] = "FAIL"

        def addError(self, test, err):
            super().addError(test, err)
            self.outcomes[test.id()] = "ERROR"

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self.outcomes[test.id()] = "ok"

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self.outcomes[test.id()] = "FAIL"

    suite = unittest.defaultTestLoader.discover(TESTS, top_level_dir=TESTS)
    result = unittest.TextTestRunner(resultclass=Recording, verbosity=1).run(suite)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({"outcomes": Recording.outcomes, "ran": result.testsRun,
                   "ok": result.wasSuccessful()}, f)
    return 0 if result.wasSuccessful() else 1


def check_suite():
    print("== 1. Test suite in the release gate (OPENEVP_RELEASE_GATE=1)")
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        summary_path = os.path.join(tmp, "summary.json")
        env = dict(os.environ, OPENEVP_RELEASE_GATE="1")
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--run-suite", summary_path],
                              cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        output = proc.stdout + proc.stderr
        try:
            with open(summary_path, encoding="utf-8") as f:
                summary = json.load(f)
        except (OSError, ValueError):
            print(output[-4000:])
            return ["the test suite did not finish (no summary)"]
    outcomes = summary["outcomes"]
    tail = [line for line in output.strip().splitlines() if line.startswith(("Ran ", "OK", "FAILED"))]
    print("   " + " / ".join(tail[-2:]))
    if proc.returncode != 0 or not summary["ok"]:
        print(output[-6000:])
        problems.append("the test suite failed")
    if "ResourceWarning" in output:
        problems.append("the test suite printed a ResourceWarning")
    decoder = {t: o for t, o in outcomes.items() if _is_decoder_test(t)}
    ran = sorted(t for t, o in decoder.items() if o == "ok")
    skipped = {t: o for t, o in decoder.items() if o.startswith("skipped")}
    allowed = {t: o for t, o in skipped.items() if t.split(".", 1)[0] in RESEARCH_ONLY}
    print(f"   decoder tests: {len(ran)} ran, {len(skipped)} skipped "
          f"({len(allowed)} need research data: {', '.join(RESEARCH_ONLY)})")
    for t, o in sorted(skipped.items()):
        if t not in allowed:
            problems.append(f"decoder test {t} {o}")
    for r in DECODER_MODULES + DECODER_TESTS:
        if not any(t == r or t.startswith(r + ".") for t in ran):
            problems.append(f"no decoder test ran from {r}")
    other_skips = sorted(t for t, o in outcomes.items() if o.startswith("skipped") and t not in decoder)
    if other_skips:
        print(f"   other skipped tests ({len(other_skips)}, platform or opt-in):")
        for t in other_skips:
            print(f"     {t}: {outcomes[t][9:]}")
    return problems


# ---- 2. the built command-line tool ---------------------------------------------

def _run(args, timeout=300):
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_cli():
    print(f"== 2. Built command-line tool ({os.path.relpath(CLI, REPO)})")
    if not os.path.isfile(CLI):
        return [f"{CLI} not found: run build_windows.ps1 first"]
    problems = []
    sys.path.insert(0, REPO)
    import openevp
    code, out = _run([CLI, "--version"], timeout=60)
    print(f"   --version: {out}")
    if code != 0 or out != openevp.__version__:
        problems.append(f"--version gave {out!r} (exit {code}), not {openevp.__version__}")
    code, out = _run([CLI, "--check-wav", LONG_VECTOR])
    for line in out.splitlines():
        print(f"   {line}")
    if code != 0 or not out.splitlines() or not out.splitlines()[-1].startswith("OK: decoded"):
        problems.append(f"--check-wav failed (exit {code})")
    elif f"({LONG_SECONDS:.1f} s of audio)" not in out:
        problems.append(f"--check-wav did not decode {LONG_SECONDS:.1f} s of audio")
    if "slow mode" in out:
        problems.append("the built CLI decodes in slow mode: its lpec_core.dll did not load")
    return problems


# ---- 3. the built app -------------------------------------------------------------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _source_modules(package):
    """Every module of a source package, dotted (package itself included)."""
    root = os.path.join(REPO, package)
    found = set()
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(folder, REPO).replace(os.sep, ".")
        for name in files:
            if name == "__init__.py":
                found.add(rel)
            elif name.endswith(".py"):
                found.add(f"{rel}.{name[:-3]}")
    return found


def frozen_modules(exe):
    """The Python modules frozen into a PyInstaller executable's PYZ."""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(exe)
    for name, entry in archive.toc.items():
        if entry[-1] == "z":
            return set(archive.open_embedded_archive(name).toc)
    raise ValueError(f"{exe} has no PYZ archive")


BUNDLED = (   # (file in _internal, its source)
    ("openevp/decoders/sony_lpec/data/lpec_tables.json", "openevp/decoders/sony_lpec/data/lpec_tables.json"),
    ("openevp/decoders/sony_lpec/lpec_core.dll", "openevp/decoders/sony_lpec/lpec_core.dll"),
    ("libusb-1.0.dll", "vendor/libusb-1.0.30/libusb-1.0.dll"),
    ("assets/st25.ico", "assets/st25.ico"),
    ("driver/install-winusb.ps1", "app/driver/install-winusb.ps1"),
    ("driver/uninstall-winusb.ps1", "app/driver/uninstall-winusb.ps1"),
    ("driver/manifest.ps1", "app/driver/manifest.ps1"),
    ("driver/models.json", "app/driver/models.json"),
)


UI_SOURCE = os.path.join(REPO, "app", "ui")
UI_BUILT = os.path.join(INTERNAL, "app", "ui")


def ui_files(folder):
    """Every file under a UI folder, relative with "/" (e.g. vendor/regions.min.js)."""
    found = set()
    for base, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            found.add(os.path.relpath(os.path.join(base, name), folder).replace(os.sep, "/"))
    return found


def ui_bundle_problems(source, built):
    """([problem], files checked): every file of the source UI folder must be in
    the built one with the same bytes, and the built one must hold nothing else."""
    want = ui_files(source)
    if not want:
        return [f"no UI files found in {source}"], 0
    if not os.path.isdir(built):
        return [f"the built app has no UI folder ({built})"], 0
    have = ui_files(built)
    problems = [f"the built app's UI has no {name}" for name in sorted(want - have)]
    problems += [f"the built app's UI has {name}, which is not in app/ui" for name in sorted(have - want)]
    for name in sorted(want & have):
        a = os.path.join(source, *name.split("/"))
        b = os.path.join(built, *name.split("/"))
        if _sha256(a) != _sha256(b):
            problems.append(f"the built app's UI file {name} differs from app/ui/{name}")
    return problems, len(want)


def check_app():
    print(f"== 3. Built app ({os.path.relpath(APP_DIR, REPO)})")
    if not os.path.isfile(APP_EXE):
        return [f"{APP_EXE} not found: run build_windows.ps1 first"]
    problems = []
    try:
        modules = frozen_modules(APP_EXE)
    except Exception as e:
        return [f"could not read the frozen modules of {APP_EXE}: {e}"]
    want = _source_modules("app") | _source_modules("openevp")
    missing = sorted(want - modules)
    print(f"   {len(want - set(missing))}/{len(want)} modules of app/ and openevp/ frozen in")
    if missing:
        problems.append("modules not frozen into the app: " + ", ".join(missing))
    if "st25.cli" in modules:
        problems.append("the app bundles st25.cli (the app must never import the CLI)")
    for bundled, source in BUNDLED:
        path = os.path.join(INTERNAL, *bundled.split("/"))
        src = os.path.join(REPO, *source.split("/"))
        if not os.path.isfile(path):
            problems.append(f"the built app has no _internal/{bundled}")
        elif not os.path.isfile(src):
            problems.append(f"{source} (the source of _internal/{bundled}) not found")
        elif _sha256(path) != _sha256(src):
            problems.append(f"_internal/{bundled} differs from {source}")
    if not any(p.startswith("_internal/") for p in problems):
        print(f"   {len(BUNDLED)} bundled files present and identical to their sources")
    ui_problems, checked = ui_bundle_problems(UI_SOURCE, UI_BUILT)
    if not ui_problems:
        print(f"   {checked} UI files (app/ui) present and identical, nothing extra")
    return problems + ui_problems


# ---- 4. the built app starts ------------------------------------------------------

SMOKE_TIMEOUT = 180                 # the app gives the page 60 s; startup and shutdown on top


def check_gui():
    print(f"== 4. Built app starts: {os.path.relpath(APP_EXE, REPO)} --smoke (hidden window)")
    if not os.path.isfile(APP_EXE):
        return [f"{APP_EXE} not found: run build_windows.ps1 first"]
    with tempfile.TemporaryDirectory() as tmp:
        report_path = os.path.join(tmp, "smoke.json")
        try:
            code, out = _run([APP_EXE, "--smoke", report_path], timeout=SMOKE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return [f"OpenEVP.exe --smoke did not exit within {SMOKE_TIMEOUT} s"]
        try:
            with open(report_path, encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError):
            return [f"OpenEVP.exe --smoke wrote no report (exit {code}){': ' + out if out else ''}"]
    page = report.get("page", {})
    loaded = [k for k in ("app_js", "notes_js", "wavesurfer", "regions", "style_css", "bridge") if page.get(k)]
    print(f"   page: title {page.get('title')!r}, loaded {', '.join(loaded) or 'nothing'}, "
          f"version shown {page.get('version_shown')!r}")
    print(f"   fetched {len(report.get('ui_files', []))} UI files; capabilities(): "
          f"version {report.get('capabilities', {}).get('version')!r}, "
          f"wav {report.get('capabilities', {}).get('wav')!r}")
    problems = [f"GUI smoke: {p}" for p in report.get("problems", [])]
    if code != 0 and not problems:
        problems.append(f"OpenEVP.exe --smoke exited {code}")
    if code == 0 and not report.get("ok"):
        problems.append("OpenEVP.exe --smoke exited 0 but its report is not ok")
    return problems


def main(argv):
    if len(argv) == 2 and argv[0] == "--run-suite":
        return _run_suite_here(argv[1])
    if argv:
        print(__doc__)
        return 2
    problems = []
    for check in (check_suite, check_cli, check_app, check_gui):
        found = check()
        for p in found:
            print(f"   PROBLEM: {p}")
        problems += found
        print()
    print(MANUAL)
    if problems:
        print(f"RELEASE CHECK FAILED: {len(problems)} problem(s) above.")
        return 1
    print("Automated release checks passed. Do the manual checks above before publishing.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
