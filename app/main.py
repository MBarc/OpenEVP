"""Desktop app entry: wires the device manager, audio server and UI together."""
import ctypes
import json
import os
import shutil
import sys
import tempfile

import webview

from st25.cli import default_output
from st25.protocol import PID, VID
from st25.session import RecorderSession
from st25.usb import list_devices

from . import updater
from .audio_server import AudioServer
from .backend import Api, recording_wav
from .devices import DeviceManager
from .driver_setup import set_up_driver
from .pnp import needs_setup, present_instances
from .store import AppData

WEBVIEW2 = ("OpenEVP needs the Microsoft Edge WebView2 Runtime, which is part of "
            "Windows 11 and most Windows 10 PCs. Install it from "
            "https://developer.microsoft.com/microsoft-edge/webview2/ and start the app again.")


def _recorders():
    """Usable recorders (libusb), plus placeholders for ones still needing their driver."""
    ids = list_devices(VID, PID)
    return ids + needs_setup(present_instances(), len(ids))


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


def _close_question(exporting, backing_up):
    """(title, text) for the close prompt while work is still running, or None."""
    backup = ("A backup of a marked recording is still being saved. Closing now may stop it; "
              "a backup that did not finish is shown as failed and can be retried later.")
    if exporting:
        text = "An export is still running. Stop it after the current recording and close?"
        return "Export in progress", f"{text} {backup}" if backing_up else text
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


def main():
    _own_taskbar_identity()
    running = _announce_running()   # keeps the mutex handles alive until the app closes
    cache = tempfile.mkdtemp(prefix="st25-audio-")
    manager = None
    server = None
    api = None
    store = None
    try:
        # A playback decode stops early (audio.Cancelled) once the app is closing.
        server = AudioServer(lambda key: recording_wav(
            manager, key, should_stop=lambda: api is not None and api.stopping()), cache)
        manager = DeviceManager(_recorders, RecorderSession.open, on_removed=server.forget)
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

        default_dest, _warning = default_output()      # Documents\OpenEVP; created by the first export
        store, store_problems = _open_store()           # the remembered Save-to folder replaces default_dest
        frozen = getattr(sys, "frozen", False)
        if frozen:
            updater.clean_old_downloads()
        api = Api(manager, emit, pick_folder, default_dest, server,
                  driver_setup=set_up_driver if sys.platform == "win32" else None, pick_wav=pick_wav,
                  updater=updater, quit_app=quit_for_update, can_install=frozen and sys.platform == "win32",
                  before_install=lambda: _hand_over(running), store=store, store_problems=store_problems)
        # A relative URL is served by pywebview's built-in HTTP server, relative to the
        # entry script (or the PyInstaller bundle), so the UI files ship as data.
        window = webview.create_window("OpenEVP", "app/ui/index.html", js_api=api,
                                       width=1100, height=720, min_size=(800, 500))

        def on_closing():
            if closing_for_update:
                return True
            if api.updating():
                proceed = window.create_confirmation_dialog(
                    "Update in progress", "The update is still downloading. Cancel it and close?")
                if proceed:
                    api.request_stop()
                return proceed
            question = _close_question(api.exporting(), api.backing_up())
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
            webview.start(gui="edgechromium", http_server=True, icon=_icon())
        except Exception as e:
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
