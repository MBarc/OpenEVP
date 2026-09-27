"""Desktop app entry: wires the device manager, audio server and UI together."""
import ctypes
import json
import os
import shutil
import sys
import tempfile
import time

import webview

from openevp import __version__
from openevp.paths import default_output

from . import folders, updater
from .audio_server import AudioServer
from .backend import Api, recording_wav
from .devices import DeviceManager
from .driver_setup import set_up_driver
from .store import AppData

WEBVIEW2 = ("OpenEVP needs the Microsoft Edge WebView2 Runtime, which is part of "
            "Windows 11 and most Windows 10 PCs. Install it from "
            "https://developer.microsoft.com/microsoft-edge/webview2/ and start the app again.")


def _window_handle(window):
    """The app window's HWND (the owner of a Recycle Bin prompt), or None."""
    try:
        return int(window.native.Handle.ToInt64()) or None
    except Exception:
        return None


def _icon():
    """The app icon: bundled in the app folder when frozen, in assets/ when run from source."""
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    path = os.path.join(base, "assets", "st25.ico")
    return path if os.path.isfile(path) else None


def _fatal(text):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, text, "OpenEVP", 0x10)
    else:
        print(text, file=sys.stderr)


RUNNING_MUTEX = "OpenEVPRunning"   # must match AppMutex in installer/openevp.iss


def _announce_running():
    """Hold a named mutex while the app runs, so the installer and uninstaller can ask
    the user to close it first instead of failing on files in use. Kept for the life
    of the process (returned handles are never closed)."""
    if sys.platform != "win32":
        return []
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    return [k32.CreateMutexW(None, False, name) for name in (RUNNING_MUTEX, "Global\\" + RUNNING_MUTEX)]


def _another_instance_running():
    """After dropping our own handles: does some other OpenEVP still hold the mutex?"""
    if sys.platform != "win32":
        return False
    k32 = ctypes.windll.kernel32
    k32.OpenMutexW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    SYNCHRONIZE = 0x00100000
    for name in (RUNNING_MUTEX, "Global\\" + RUNNING_MUTEX):
        h = k32.OpenMutexW(SYNCHRONIZE, False, name)
        if h:
            k32.CloseHandle(h)
            return True
    return False


def _stop_announcing(handles):
    """Drop the running-mutex just before starting an update's installer: the app is
    about to close, and the silent installer would otherwise give up on seeing it."""
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        for h in handles:
            if h:
                k32.CloseHandle(h)
    handles.clear()


def _hand_over(running):
    """Just before an update's installer starts: drop our running-mutex (the silent
    installer gives up on seeing it) - unless another OpenEVP window still holds it,
    in which case the installer would quietly quit, so refuse instead."""
    _stop_announcing(running)
    if _another_instance_running():
        running[:] = _announce_running()
        raise updater.UpdateError("another OpenEVP window is open; close it, then update")


def _open_store():
    """(AppData or None, [problem]): the app's own data folder (%APPDATA%\\OpenEVP).
    If it cannot be created or opened the app still runs, without EVP marks, and
    says why: no store failure may stop the app from starting."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    try:
        return AppData(os.path.join(base, "OpenEVP")), []
    except OSError as e:
        return None, [f"EVP marks are off: OpenEVP could not create its data folder ({e.strerror or e})."]
    except Exception as e:
        return None, [f"EVP marks are off: OpenEVP could not open its data ({type(e).__name__}: {e})."]


def _close_question(exporting, backing_up, saving_marked=False):
    """(title, text) for the close prompt while work is still running, or None."""
    backup = ("A backup of a marked recording is still being saved. Closing now may stop it; "
              "a backup that did not finish is shown as failed and can be retried later.")
    if exporting:
        text = "An export is still running. Stop it after the current recording and close?"
        return "Export in progress", f"{text} {backup}" if backing_up else text
    if saving_marked:
        text = ("A WAV with its EVP marks is still being saved. Closing now may stop it before it "
                "is saved (the marks themselves are kept). Close?")
        return "Export in progress", f"{backup} {text}" if backing_up else text
    if backing_up:
        return "Backup in progress", f"{backup} Close?"
    return None


def _own_taskbar_identity():
    """Group the window under OpenEVP (with its icon) in the taskbar, not under
    python.exe when running from source."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MBarc.OpenEVP")
        except (AttributeError, OSError):
            pass


# ---- --smoke: the release check's frozen GUI smoke test -------------------------
# OpenEVP.exe --smoke [REPORT.json] starts the backend and the WebView2 page in a
# hidden window, checks that the page loaded (its scripts, styles and every bundled
# UI file served) and that the JS bridge answers, then exits: 0 if all is well, 1
# if not, with the details in REPORT.json (a --windowed build has no console).
# It uses a throwaway data folder and save folder, no updater (no network) and no
# driver setup; without the flag nothing changes.
SMOKE_FLAG = "--smoke"
SMOKE_TIMEOUT = 60                      # seconds for the page to load and answer

# Run in the page: start the checks that need promises (the bridge, fetching each
# bundled UI file); their results land in window.__openevpSmoke.
_SMOKE_START_JS = """
window.__openevpSmoke = null;
Promise.all([
  window.pywebview.api.capabilities(),
  Promise.all(%s.map(f => fetch(f, {cache: "no-store"}).then(r => [f, r.ok, r.status], e => [f, false, String(e)])))
]).then(([caps, files]) => { window.__openevpSmoke = JSON.stringify({caps: caps, files: files}); },
        e => { window.__openevpSmoke = JSON.stringify({error: String(e)}); });
"true";
"""
# Run in the page: what loaded (synchronous).
_SMOKE_STATE_JS = """JSON.stringify({
  title: document.title,
  app_js: typeof window.onBackendEvent === "function",
  notes_js: typeof renderNotes === "function",
  wavesurfer: typeof WaveSurfer !== "undefined" && typeof WaveSurfer.create === "function",
  regions: typeof WaveSurfer !== "undefined" && typeof WaveSurfer.Regions !== "undefined",
  style_css: Array.from(document.styleSheets).some(s => (s.href || "").endsWith("/style.css") && s.cssRules.length > 0),
  bridge: !!(window.pywebview && window.pywebview.api && window.pywebview.api.capabilities),
  version_shown: (document.getElementById("version") || {}).textContent || "",
  smoke: window.__openevpSmoke === undefined ? null : window.__openevpSmoke
})"""


def _smoke_request(argv):
    """(True, report path or None) for "--smoke [REPORT]", else (False, None)."""
    if argv and argv[0] == SMOKE_FLAG and len(argv) <= 2:
        return True, (argv[1] if len(argv) == 2 else None)
    return False, None


def _ui_dir():
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    return os.path.join(base, "app", "ui")


def ui_files(ui_dir):
    """Every file of the UI folder, relative and with "/" (index.html, vendor/x.js...)."""
    found = []
    for folder, dirs, files in os.walk(ui_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            found.append(os.path.relpath(os.path.join(folder, name), ui_dir).replace(os.sep, "/"))
    return found


def _smoke_check(window, report):
    """On pywebview's worker thread once the GUI loop runs: check the page, then
    close the window (which ends webview.start())."""
    problems = report["problems"]
    try:
        deadline = time.monotonic() + SMOKE_TIMEOUT
        if not window.events.loaded.wait(SMOKE_TIMEOUT):
            problems.append(f"the page did not load within {SMOKE_TIMEOUT} s")
            return
        files = ui_files(_ui_dir())
        report["ui_files"] = files
        want_version = f"v{__version__}"
        started = False
        state = {}
        while time.monotonic() < deadline:
            state = json.loads(window.evaluate_js(_SMOKE_STATE_JS))
            if state["bridge"] and not started:
                window.evaluate_js(_SMOKE_START_JS % json.dumps(files))
                started = True
            elif started and state["smoke"] and state["version_shown"] == want_version:
                break
            time.sleep(0.2)
        report["page"] = {k: v for k, v in state.items() if k != "smoke"}
        if state.get("title") != "OpenEVP":
            problems.append(f"index.html did not load (title {state.get('title')!r})")
        for key, what in (("app_js", "app.js"), ("notes_js", "notes.js"), ("wavesurfer", "vendor/wavesurfer.min.js"),
                          ("regions", "vendor/regions.min.js"), ("style_css", "style.css"),
                          ("bridge", "the JS bridge (window.pywebview.api)")):
            if not state.get(key):
                problems.append(f"{what} did not load in the page")
        if state.get("version_shown") != want_version:
            problems.append(f"app.js did not start up through the bridge (version shown "
                            f"{state.get('version_shown')!r}, not {want_version!r})")
        if not state.get("smoke"):
            problems.append(f"the bridge did not answer capabilities() within {SMOKE_TIMEOUT} s")
            return
        smoke = json.loads(state["smoke"])
        if "error" in smoke:
            problems.append(f"capabilities() through the bridge failed: {smoke['error']}")
            return
        report["capabilities"] = smoke["caps"]
        if smoke["caps"].get("version") != __version__:
            problems.append(f"capabilities() answered version {smoke['caps'].get('version')!r}")
        for name, ok, status in smoke["files"]:
            if not ok:
                problems.append(f"the page could not fetch {name} ({status})")
    except Exception as e:
        problems.append(f"the smoke check failed: {type(e).__name__}: {e}")
    finally:
        try:
            window.destroy()
        except Exception:
            pass


def main(argv=None):
    """Run the app; returns the process exit code (only --smoke sets one)."""
    smoke, smoke_report = _smoke_request(sys.argv[1:] if argv is None else argv)
    if smoke:
        return _smoke_main(smoke_report)
    _run_app()
    return 0


def _smoke_main(report_path):
    report = {"ok": False, "version": __version__, "problems": []}
    home = tempfile.mkdtemp(prefix="openevp-smoke-")
    try:
        _run_app(smoke=(report, home))
    except Exception as e:
        report["problems"].append(f"the app did not start: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    report["ok"] = not report["problems"]
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    return 0 if report["ok"] else 1


def _run_app(smoke=None):
    """The app. smoke: (report, a throwaway folder) for --smoke (see above)."""
    _own_taskbar_identity()
    running = [] if smoke else _announce_running()   # keeps the mutex handles alive until the app closes
    cache = tempfile.mkdtemp(prefix="st25-audio-")
    manager = None
    server = None
    api = None
    store = None
    try:
        # A playback decode stops early (formats.Cancelled) once the app is closing.
        server = AudioServer(lambda key: recording_wav(
            manager, key, should_stop=lambda: api is not None and api.stopping()), cache)
        # Every supported recorder model (openevp.recorders), opened through its model.
        manager = DeviceManager(on_removed=server.forget)
        server.start()
        window = None

        def emit(event, payload):
            if window is not None:
                try:
                    window.evaluate_js(f"window.onBackendEvent({json.dumps(event)}, {json.dumps(payload)})")
                except Exception:
                    pass                                  # the window is already gone during shutdown

        def pick_wav(start_dir):
            dialog = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
            result = window.create_file_dialog(dialog, directory=start_dir or "",
                                               file_types=("WAV audio (*.wav)", "All files (*.*)"))
            return result[0] if result else None

        def pick_folder(start_dir):
            folder_dialog = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
            result = window.create_file_dialog(folder_dialog, directory=start_dir or "")
            return result[0] if result else None

        closing_for_update = False

        def quit_for_update():
            nonlocal closing_for_update
            closing_for_update = True
            window.destroy()

        if smoke:
            default_dest = os.path.join(smoke[1], "save")
            store, store_problems = AppData(os.path.join(smoke[1], "appdata")), []
        else:
            default_dest, _warning = default_output()   # Documents\OpenEVP; created by the first export
            store, store_problems = _open_store()       # the remembered Save-to folder replaces default_dest
        frozen = getattr(sys, "frozen", False) and not smoke
        if frozen:
            updater.clean_old_downloads()
        api = Api(manager, emit, pick_folder, default_dest, server,
                  driver_setup=set_up_driver if sys.platform == "win32" and not smoke else None,
                  pick_wav=pick_wav, updater=None if smoke else updater, quit_app=quit_for_update,
                  can_install=frozen and sys.platform == "win32",
                  before_install=lambda: _hand_over(running), store=store, store_problems=store_problems,
                  recycle=lambda path: folders.recycle(path, owner=_window_handle(window)))
        # A relative URL is served by pywebview's built-in HTTP server, relative to the
        # entry script (or the PyInstaller bundle), so the UI files ship as data.
        window = webview.create_window("OpenEVP", "app/ui/index.html", js_api=api,
                                       width=1100, height=720, min_size=(800, 500), hidden=bool(smoke))

        def on_closing():
            if closing_for_update:
                return True
            if api.updating():
                proceed = window.create_confirmation_dialog(
                    "Update in progress", "The update is still downloading. Cancel it and close?")
                if proceed:
                    api.request_stop()
                return proceed
            question = _close_question(api.exporting(), api.backing_up(), api.saving_marked())
            if question:
                proceed = window.create_confirmation_dialog(*question)
                if proceed:
                    # Refuse further downloads right away: webview.start() (and the
                    # api.shutdown() after it) may not return for a while yet.
                    api.request_stop()
                return proceed
            return True

        window.events.closing += on_closing
        try:
            # Require WebView2: pywebview would otherwise fall back to the old MSHTML
            # engine, which cannot run this UI.
            if smoke:
                webview.start(_smoke_check, (window, smoke[0]), gui="edgechromium", http_server=True,
                              icon=_icon())
            else:
                webview.start(gui="edgechromium", http_server=True, icon=_icon())
        except Exception as e:
            if smoke:
                smoke[0]["problems"].append(f"WebView2 did not start: {type(e).__name__}: {e}")
            else:
                _fatal(f"{WEBVIEW2}\n\n({e})")
    finally:
        if api is not None:
            api.shutdown()                              # joins the workers, then closes the store
        elif store is not None:
            store.close()
        if server is not None:
            server.stop()
        if manager is not None:
            manager.close()
        shutil.rmtree(cache, ignore_errors=True)
