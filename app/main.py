"""Desktop app entry: wires the device manager, audio server and UI together."""
import ctypes
import json
import os
import shutil
import sys
import tempfile
import threading
import time

import webview

from openevp import __version__
from openevp.paths import default_output

from . import events, folders, mic_permission, native_share, sharing, updater
from .audio_server import CACHE_PREFIX, AudioServer, clean_stale_caches, hold_cache
from .backend import Api, night_setting, recording_wav
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


def _recycler(get_window):
    """folders.recycle for the Api: the app window owns any prompt Windows shows
    (in front of it), and before= (let go of a held folder) is passed through.
    get_window() is asked at each call: the Api is made before its window is."""
    def recycle(path, before=None):
        return folders.recycle(path, owner=_window_handle(get_window()), before=before)
    return recycle


def _icon():
    """The app icon: bundled in the app folder when frozen, in assets/ when run from source."""
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    path = os.path.join(base, "assets", "openevp.ico")
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


STORE_LOCK_WAIT = 5.0      # seconds _open_store() keeps trying for the store's lock


def _open_store():
    """(AppData or None, [problem]): the app's own data folder (%APPDATA%\\OpenEVP).
    If it cannot be created or opened the app still runs, without EVP marks, and
    says why: no store failure may stop the app from starting."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    try:
        # A previous OpenEVP may still be closing (window gone, workers finishing):
        # wait a few seconds for its lock rather than opening read-only.
        return AppData(os.path.join(base, "OpenEVP"), lock_wait=STORE_LOCK_WAIT), []
    except OSError as e:
        return None, [f"EVP marks are off: OpenEVP could not create its data folder ({e.strerror or e})."]
    except Exception as e:
        return None, [f"EVP marks are off: OpenEVP could not open its data ({type(e).__name__}: {e})."]


def _close_question(exporting, backing_up, saving_marked=False, clips_running=False, changing_files=False,
                    recording=False):
    """(title, text) for the close prompt while work is still running, or None."""
    backup = ("A backup of a marked recording is still being saved. Closing now may stop it; "
              "a backup that did not finish is shown as failed and can be retried later.")
    if recording:
        text = "A recording is running. Closing stops it and saves what was recorded. Close?"
        return "Recording in progress", f"{backup} {text}" if backing_up else text
    if changing_files:
        text = ("Files in the library are still being renamed, moved or deleted. "
                "OpenEVP closes once that is finished. Close?")
        return "Library files in use", f"{backup} {text}" if backing_up else text
    if clips_running:
        text = "Clips are still being exported. Stop after the current recording and close?"
        return "Export in progress", f"{backup} {text}" if backing_up else text
    if exporting:
        text = "An export is still running. Stop it after the current recording and close?"
        return "Export in progress", f"{text} {backup}" if backing_up else text
    if saving_marked:
        text = ("A WAV with its EVP marks (or its EVP clips) is still being saved. Closing now may stop "
                "it before it is saved (the marks themselves are kept). Close?")
        return "Export in progress", f"{backup} {text}" if backing_up else text
    if backing_up:
        return "Backup in progress", f"{backup} Close?"
    return None


NIGHT_BG = "#0A0505"                # the night screen's background (style.css html.night --bg)


def _start_page(night):
    """The page and the window's background: with the night screen on, both dark from the first
    paint (index.html reads ?night=1 before its styles load), so the window never flashes white."""
    return ("app/ui/index.html" + ("?night=1" if night else ""), NIGHT_BG if night else "#FFFFFF")

CLOSE_FINALIZE_SHARE = 0.25         # the part of the close's time kept for the backend's own finish
CLOSE_DRAIN_TIMEOUT = 70           # seconds closing waits for the page to save a recording (its own steps are bounded too)
CLOSE_PAGE_MARGIN = 3.0            # seconds of the page's share it is not told about: polling and the report after
CLOSE_REPORT_WAIT = 2.0            # seconds the window waits for the page's list of changes not saved, and again to write it
CLOSE_LAST_WAIT = 5.0              # seconds the window's own last steps (before_close, destroy) are waited for
_DRAIN_START_JS = "liveDrainForClose({ms}); true"
_DRAIN_DONE_JS = "window.__liveDrained === true"
_UNSAVED_JS = "JSON.stringify(liveUnsavedNow())"


def _drain_start_js(page_seconds):
    """The page's whole Stop (flush, chunks, marks, the backend's finish) fits in this budget."""
    return _DRAIN_START_JS.format(ms=int(max(0.0, page_seconds - CLOSE_PAGE_MARGIN) * 1000))


def _bounded(fn, seconds, name):
    """Run fn on a thread of its own and wait for it at most `seconds`: True if it returned."""
    done = threading.Event()

    def run():
        try:
            fn()
        except Exception:
            pass
        finally:
            done.set()
    threading.Thread(target=run, name=name, daemon=True).start()
    return done.wait(max(0.0, seconds))


def _close_after_drain(window, before_close, finalize=None, timeout=CLOSE_DRAIN_TIMEOUT, poll=0.2,
                       clock=time.monotonic, sleep=time.sleep, finalize_share=CLOSE_FINALIZE_SHARE, report=None):
    """Closing during a recording, on a worker thread: the page stops and saves it
    as Stop does (the worklet's last samples, every queued chunk, then the backend
    finishes the file: live.js liveDrainForClose). The whole close takes at most
    timeout seconds, whatever hangs:
    - the page is asked on a thread of its own (pywebview's evaluate_js waits for
      the page without a time limit) and gets all but the last finalize_share of
      the time; it is told its budget (less CLOSE_PAGE_MARGIN) and fits every one
      of its waits into it;
    - then the page's list of marks that may not have been
      saved (liveUnsavedNow: called off, unanswered, or still waiting) is read,
      briefly, and given to report() (Api.log_unsaved: a plain append to a log
      file, flushed to the disk; never the store, whose lock a stalled write may
      hold), which keeps it for the next start: a closing window cannot show it,
      and it must never be lost without a word. report() runs on a thread of its
      own, waited for at most CLOSE_REPORT_WAIT seconds;
    - if it did not finish, finalize() (the backend finishing the file with what
      has arrived) runs on a thread of its own too, until the deadline: it may wait
      on the recording's lock or on the disk;
    then before_close() and the window closes, finished or not (both on threads of
    their own, waited for at most CLOSE_LAST_WAIT seconds: nothing here waits without
    a limit). A file left unfinished is a .part that the next start recovers."""
    start = time.monotonic()
    drained = threading.Event()
    page_seconds = timeout * (1 - finalize_share)
    page_deadline = clock() + page_seconds
    unsaved = []

    def ask_page():
        try:
            window.evaluate_js(_drain_start_js(page_seconds))
            while clock() < page_deadline:
                if window.evaluate_js(_DRAIN_DONE_JS):
                    drained.set()
                    return
                sleep(poll)
        except Exception:
            pass                                  # the page is gone

    def read_unsaved():
        try:
            got = json.loads(window.evaluate_js(_UNSAVED_JS) or "null")
            if got:
                unsaved.append(got)
        except Exception:
            pass                                  # the page is gone, or hung

    def run_finalize():
        try:
            finalize()
        except Exception:
            pass
    try:
        asker = threading.Thread(target=ask_page, name="close-drain-page", daemon=True)
        asker.start()
        asker.join(page_seconds)
        if report is not None:
            left = lambda: timeout - (time.monotonic() - start)     # noqa: E731
            _bounded(read_unsaved, min(CLOSE_REPORT_WAIT, left()), "close-drain-unsaved")
            if unsaved:
                got = unsaved[0]
                _bounded(lambda: report(got), min(CLOSE_REPORT_WAIT, left()), "close-drain-report")
        if not drained.is_set() and finalize is not None:
            finisher = threading.Thread(target=run_finalize, name="close-finalize", daemon=True)
            finisher.start()
            finisher.join(max(0.0, timeout - (time.monotonic() - start)))
    finally:
        _bounded(before_close, CLOSE_LAST_WAIT, "close-before")
        _bounded(window.destroy, CLOSE_LAST_WAIT, "close-destroy")


def _own_taskbar_identity():
    """Group the window under OpenEVP (with its icon) in the taskbar, not under
    python.exe when running from source."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MBarc.OpenEVP")
        except (AttributeError, OSError):
            pass


# ---- --smoke: the release check's frozen GUI smoke test -------------------------
# OpenEVP.exe --smoke [REPORT.json] checks that both Sony decoders (LPEC LP and SP
# for the ICD-ST25 and ICD-ST10, LPEC ST for the ICD-ST10) load with their tables
# and fast C cores, that MP3 clips can be encoded (lameenc) and MP3 files decoded
# (mp3_core.dll, minimp3: the encoded clip is decoded back),
# starts the backend and the WebView2 page in a hidden window, checks that the page
# loaded (its scripts, styles and every bundled UI file served) and that the JS
# bridge answers, then exits: 0 if all is well, 1 if not, with the details in
# REPORT.json (a --windowed build has no console).
# It uses a throwaway data folder and save folder, no updater (no network) and no
# driver setup; without the flag nothing changes.
SMOKE_FLAG = "--smoke"
# Open audio file…: WAV and MP3 (with the other extensions MP3 files are saved
# under, e.g. WhatsApp Web's .mpeg).
AUDIO_FILE_TYPES = ("Audio files (*.wav;*.mp3;*.mpeg;*.mpga;*.mp2;*.m2a)", "WAV audio (*.wav)",
                    "MP3 audio (*.mp3;*.mpeg;*.mpga;*.mp2;*.m2a)", "All files (*.*)")
SMOKE_TIMEOUT = 60                      # seconds for the page to load and answer

# Run in the page: start the checks that need promises (the bridge, and fetching
# each bundled UI file); their results land in window.__openevpSmoke. (The page
# never decodes audio itself: every file reaches it as a WAV from the audio server.)
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
  live_js: typeof liveSmoke === "function",
  wavesurfer: typeof WaveSurfer !== "undefined" && typeof WaveSurfer.create === "function",
  regions: typeof WaveSurfer !== "undefined" && typeof WaveSurfer.Regions !== "undefined",
  style_css: Array.from(document.styleSheets).some(s => (s.href || "").endsWith("/style.css") && s.cssRules.length > 0),
  bridge: !!(window.pywebview && window.pywebview.api && window.pywebview.api.capabilities),
  version_shown: (document.getElementById("version") || {}).textContent || "",
  started: window.__openevpStarted === true,
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


# Live mode in the smoke test: Chromium's fake audio input (a beep) stands in for a
# microphone, so no real input is ever opened; the app's own microphone permission
# handler (app/mic_permission.py) must answer, or getUserMedia would wait for a prompt
# nobody sees and the check times out.
SMOKE_BROWSER_ARGS = "--use-fake-device-for-media-stream --autoplay-policy=no-user-gesture-required"
SMOKE_LIVE_SECONDS = 1.5
_SMOKE_LIVE_JS = """
window.__openevpLive = null;
liveSmoke(%s).then(r => { window.__openevpLive = JSON.stringify(r); },
                   e => { window.__openevpLive = JSON.stringify({ok: false, error: String(e)}); });
"true";
"""


def _smoke_live(window, report, home):
    """Record a moment from the fake input through the Live view's own code (permission,
    getUserMedia, the AudioWorklet, the bridge, the WAV writer, a mark), and check the
    WAV it saved into the library folder."""
    import wave
    problems = report["problems"]
    window.evaluate_js(_SMOKE_LIVE_JS % SMOKE_LIVE_SECONDS)
    deadline = time.monotonic() + SMOKE_TIMEOUT
    raw = None
    while time.monotonic() < deadline:
        raw = window.evaluate_js("window.__openevpLive")
        if raw:
            break
        time.sleep(0.2)
    if not raw:
        problems.append(f"Live recording did not finish within {SMOKE_TIMEOUT} s (no microphone permission?)")
        return
    live = report["live"] = json.loads(raw)
    if not live.get("ok") or not live.get("files"):
        problems.append(f"Live recording failed: {live.get('error') or 'nothing was saved'}")
        return
    if not live.get("mark") or live["files"][0].get("marks") != 1:
        problems.append("Live recording: the mark made while recording was not stored")
    path = os.path.join(home, "save", live["files"][0]["name"])
    try:
        with wave.open(path) as w:
            live["wav"] = {"rate": w.getframerate(), "channels": w.getnchannels(), "frames": w.getnframes(),
                           "width": w.getsampwidth()}
    except Exception as e:
        problems.append(f"Live recording: the saved WAV could not be read ({type(e).__name__}: {e})")
        return
    got = live["wav"]
    if got["width"] != 2 or got["rate"] != live.get("rate") or got["channels"] != live.get("channels")             or got["frames"] < 0.5 * SMOKE_LIVE_SECONDS * got["rate"]:
        problems.append(f"Live recording: the saved WAV is wrong: {got}")


def _smoke_check(window, report, home=None):
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
            elif started and state["smoke"] and state["version_shown"] == want_version and state["started"]:
                break
            time.sleep(0.2)
        report["page"] = {k: v for k, v in state.items() if k != "smoke"}
        if state.get("title") != "OpenEVP":
            problems.append(f"index.html did not load (title {state.get('title')!r})")
        for key, what in (("app_js", "app.js"), ("notes_js", "notes.js"), ("live_js", "live.js"),
                          ("wavesurfer", "vendor/wavesurfer.min.js"),
                          ("regions", "vendor/regions.min.js"), ("style_css", "style.css"),
                          ("bridge", "the JS bridge (window.pywebview.api)")):
            if not state.get(key):
                problems.append(f"{what} did not load in the page")
        if not state.get("started"):
            problems.append("the page did not finish starting (an error stopped its startup)")
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
        if home is not None and not problems:
            _smoke_live(window, report, home)
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


# (report name, codec byte, what it plays) for each decoder the smoke test loads.
SMOKE_DECODERS = (("lpec", 0x2C, "LPEC LP (ICD-ST25)"), ("lpec_sp", 0x2A, "LPEC SP (ICD-ST10)"),
                  ("lpec_st", 0x24, "LPEC ST (ICD-ST10)"))


def _smoke_decoders(report):
    """Record whether each Sony decoder loads (tables, fast C core); a problem when
    one is unavailable or would run in slow mode. The LPEC ST decoder also decodes
    a few frames, so its C core is initialised from the bundled tables."""
    from sony_icd import audio
    decoders = report["decoders"] = {}
    for name, codec, what in SMOKE_DECODERS:
        ok, status = audio.available(codec), audio.status(codec)
        decoders[name] = {"available": ok, "status": status}
        if not ok:
            report["problems"].append(f"{what} decoding is not available: {status}")
        elif status:
            report["problems"].append(f"{what}: {status}")
    if decoders["lpec_st"]["available"]:
        try:
            from openevp.decoders import sony_lpec_st
            frames = b"".join(bytes([0, n, 0]) + bytes(280) for n in (1, 2, 3))   # rejected: no samples
            decoders["lpec_st"]["decoded"] = len(sony_lpec_st.decode(frames)[2])
        except Exception as e:
            report["problems"].append(f"LPEC ST (ICD-ST10) decoding failed: {type(e).__name__}: {e}")


def _smoke_mp3(report):
    """Record whether MP3 clips can be encoded (a short 8 kHz clip is cut and encoded:
    lameenc loads and runs) and MP3 files decoded (the clip is decoded back by
    mp3_core.dll, minimp3); a problem when either cannot."""
    from openevp import clips, mp3
    from openevp.decoders import mp3 as mp3dec
    info = report["mp3"] = {"available": mp3.available(), "version": mp3.version(),
                            "decoder": mp3dec.available(), "decoder_status": mp3dec.reason()}
    if not info["decoder"]:
        report["problems"].append(f"MP3 decoding is not available: {mp3dec.reason()}")
    if not info["available"]:
        report["problems"].append(f"MP3 encoding is not available: {mp3.UNAVAILABLE}")
        return
    try:
        import io
        import wave
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(bytes(16000))
        clip = clips.make(out.getvalue(), {"start": 0.4, "end": 0.6, "cls": "A", "note": "smoke"}, "mp3")
        info["bytes"] = len(clip)
        tag = 10 + sum((b & 0x7F) << (7 * (3 - i)) for i, b in enumerate(clip[6:10])) if clip[:3] == b"ID3" else 0
        if not tag or clip[tag] != 0xFF or clip[tag + 1] & 0xE0 != 0xE0:     # the ID3 tag, then an MPEG frame
            report["problems"].append("MP3 encoding gave no MP3")
        if info["decoder"]:                     # 1 s (0 .. 1.0 s of the WAV), back as 8 kHz mono
            wav = mp3dec.to_wav(clip)
            with wave.open(io.BytesIO(bytes(wav))) as w:
                decoded = info["decoded"] = {"rate": w.getframerate(), "channels": w.getnchannels(),
                                             "seconds": round(w.getnframes() / w.getframerate(), 3)}
            if decoded["rate"] != 8000 or decoded["channels"] != 1 or not 0.9 <= decoded["seconds"] <= 1.3:
                report["problems"].append(f"the 1 s MP3 clip decoded wrongly: {decoded}")
    except Exception as e:
        report["problems"].append(f"MP3 encoding failed: {type(e).__name__}: {e}")


def _smoke_main(report_path):
    report = {"ok": False, "version": __version__, "problems": []}
    home = tempfile.mkdtemp(prefix="openevp-smoke-")
    before = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = SMOKE_BROWSER_ARGS   # the fake audio input
    try:
        _smoke_decoders(report)
        _smoke_mp3(report)
        _run_app(smoke=(report, home))
    except Exception as e:
        report["problems"].append(f"the app did not start: {type(e).__name__}: {e}")
    finally:
        if before is None:
            os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
        else:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = before
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
    # Decode caches left in the temp folder by runs that crashed (another running
    # OpenEVP holds its own open, so it is kept: see clean_stale_caches).
    clean_stale_caches(tempfile.gettempdir())
    cache = tempfile.mkdtemp(prefix=CACHE_PREFIX)
    cache_lock = hold_cache(cache)
    manager = None
    server = None
    api = None
    store = None
    dispatcher = None
    try:
        # A playback decode stops early (formats.Cancelled) once the app is closing.
        server = AudioServer(lambda key: recording_wav(
            manager, key, should_stop=lambda: api is not None and api.stopping()), cache)
        # Every supported recorder model (openevp.recorders), opened through its model.
        manager = DeviceManager(on_removed=server.forget)
        server.start()
        window = None

        def send(event, payload):                         # on the dispatcher's thread only (app/events.py)
            if window is not None:
                window.evaluate_js(f"window.onBackendEvent({json.dumps(event)}, {json.dumps(payload)})")

        # Workers post events and never wait for the page: evaluate_js has no time limit.
        dispatcher = events.Dispatcher(send)
        emit = dispatcher.emit

        def pick_wav(start_dir):
            dialog = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
            result = window.create_file_dialog(dialog, directory=start_dir or "",
                                               file_types=AUDIO_FILE_TYPES)
            return result[0] if result else None

        def pick_folder(start_dir):
            folder_dialog = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
            result = window.create_file_dialog(folder_dialog, directory=start_dir or "")
            return result[0] if result else None

        closing_for_update = False
        closing_after_recording = False

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
        native = sys.platform == "win32" and not smoke   # drag out / Copy file (app.native_share)
        if native:
            sharing.clean()                             # MP3s made to share, SHARE_MAX_AGE old (another window's are younger)
        if frozen:
            updater.clean_old_downloads()
        api = Api(manager, emit, pick_folder, default_dest, server,
                  driver_setup=set_up_driver if sys.platform == "win32" and not smoke else None,
                  pick_wav=pick_wav, updater=None if smoke else updater, quit_app=quit_for_update,
                  can_install=frozen and sys.platform == "win32",
                  before_install=lambda: _hand_over(running), store=store, store_problems=store_problems,
                  recycle=_recycler(lambda: window),
                  drag_files=(lambda paths: native_share.drag_files(window.native, paths)) if native else None,
                  copy_files=(lambda paths: native_share.copy_files(window.native, paths)) if native else None)
        api.watch_store()                               # read-only: keep trying for the store's lock
        # A relative URL is served by pywebview's built-in HTTP server, relative to the
        # entry script (or the PyInstaller bundle), so the UI files ship as data.
        # The night screen is known before the window opens: the window's own background and the
        # page's first paint (index.html reads ?night=1 before its styles) are dark from the start.
        page, background = _start_page(night_setting(store))
        window = webview.create_window("OpenEVP", page, js_api=api, width=1100, height=720, min_size=(800, 500),
                                       hidden=bool(smoke), background_color=background)

        def close_now():
            nonlocal closing_after_recording
            closing_after_recording = True
            api.request_stop()

        def on_closing():
            if closing_for_update or closing_after_recording:
                return True
            if api.updating():
                proceed = window.create_confirmation_dialog(
                    "Update in progress", "The update is still downloading. Cancel it and close?")
                if proceed:
                    api.request_stop()
                return proceed
            question = _close_question(api.exporting(), api.backing_up(), api.saving_marked(),
                                       api.clips_running(), api.changing_files(), api.recording())
            if question:
                proceed = window.create_confirmation_dialog(*question)
                if proceed and api.recording():
                    # Not yet: the page first saves what it still holds (bounded), then the
                    # window closes (close_now lets that close through).
                    threading.Thread(target=_close_after_drain, args=(window, close_now, api.finish_recording),
                                     kwargs={"report": api.log_unsaved},
                                     name="close-drain", daemon=True).start()
                    return False
                if proceed:
                    # Refuse further downloads right away: webview.start() (and the
                    # api.shutdown() after it) may not return for a while yet.
                    api.request_stop()
                return proceed
            return True

        window.events.closing += on_closing
        # Live mode: the app's own page may use the microphone without a prompt (app/mic_permission.py).
        mic_permission.install_early(
            window, on_problem=(lambda e: smoke[0]["problems"].append(
                f"the microphone permission handler could not be added: {type(e).__name__}: {e}")) if smoke else None)
        try:
            # Require WebView2: pywebview would otherwise fall back to the old MSHTML
            # engine, which cannot run this UI.
            if smoke:
                webview.start(_smoke_check, (window, smoke[0], smoke[1]), gui="edgechromium", http_server=True,
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
        if dispatcher is not None:
            dispatcher.close()                          # bounded: a hung page never holds the exit
        elif store is not None:
            store.close()
        if server is not None:
            server.stop()
        if manager is not None:
            manager.close()
        cache_lock.close()
        shutil.rmtree(cache, ignore_errors=True)
