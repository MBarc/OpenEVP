"""The API the desktop UI calls (pywebview exposes Api's public methods to JS).

Every public method returns JSON-able values and never raises: errors come back
as {"ok": False, "error", "advice", "state"} so the UI can show them. Exports
run on their own (non-daemon) thread and report through emit(); their
downloads still go through the DeviceManager's single USB thread, and files
are written only after each download has finished. shutdown() stops an export
between recordings and waits for it, so closing the window never abandons one
half-way through a file.
"""
import datetime
import hashlib
import os
import struct
import threading
import wave

from st25 import __version__, audio
from st25.cli import open_folder
from st25.export import save_dvf, save_wav

from .updater import UpdateCancelled
from st25.folder import TableError
from st25.protocol import RecorderError
from st25.session import LETTERS
from st25.usb import DriverMissing, UsbError

from .devices import NEEDS_DRIVER, NEEDS_REPLUG, READY, DeviceGone

REPLUG = "Unplug the recorder's USB cable, wait a few seconds and plug it back in."
# Stage 4 (installer) replaces this with a "Set up recorder" button.
DRIVER = "Click \"Set up recorder\" to install its driver (Windows will ask for permission once)."
DISK = "Check free disk space and that the folder can be written to."


def recording_wav(manager, key, should_stop=None):
    """WAV bytes for one recording (the AudioServer provider). ``should_stop``
    lets app shutdown interrupt a long decode (see audio.dvf_to_wav)."""
    device_id, letter, number = key
    dl = manager.with_session(device_id, lambda s: s.download(letter, number))
    if dl.error:
        raise ValueError(f"{dl.label} cannot be played: {dl.error}")
    return audio.dvf_to_wav(dl.dvf, should_stop=should_stop)


def _fail(message, advice="", state=READY):
    return {"ok": False, "error": message, "advice": advice, "state": state}


SAVED_LIMIT = 5000          # a folder with more is listed partially (the page says so)
LP_BYTES_PER_SECOND = 750   # ST25 LP audio


def _seconds(path, kind):
    """Length of a saved file from its header only (never reads the audio)."""
    try:
        if kind == "wav":
            with wave.open(path) as w:
                return round(w.getnframes() / w.getframerate(), 1) if w.getframerate() else None
        with open(path, "rb") as f:
            header = f.read(468)
        if len(header) == 468:
            return round(struct.unpack(">I", header[464:468])[0] / LP_BYTES_PER_SECOND, 1)
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        pass
    return None


def _update_problem(e, what="Could not check for updates"):
    """A plain-words reason for a failed update check or download."""
    code = getattr(e, "code", None)
    if code == 404:
        reason = "no releases were found on GitHub"
    elif code in (403, 429):
        reason = "GitHub is limiting requests; try again in an hour"
    elif isinstance(e, OSError) and not code:
        reason = "could not reach GitHub (are you online?)"
    else:
        reason = str(e)
    return f"{what}: {reason}."


def _error(e):
    if isinstance(e, DriverMissing):
        return _fail(str(e), DRIVER, NEEDS_DRIVER)
    if isinstance(e, DeviceGone):
        return _fail(str(e), "", "")
    if isinstance(e, (RecorderError, UsbError, TableError)):
        return _fail(str(e), REPLUG, NEEDS_REPLUG)
    return _fail(f"{type(e).__name__}: {e}")


def _recording(m):
    return {"number": m.number, "when": m.when(), "seconds": round(m.seconds(), 1),
            "owner": m.owner, "problem": m.problem}


def _parse_items(items):
    """[(letter, number)] or None if items is not a non-empty list of valid recordings."""
    if not isinstance(items, list) or not items:
        return None
    work = []
    for i in items:
        if not isinstance(i, dict):
            return None
        letter, number = i.get("folder"), i.get("number")
        if not isinstance(letter, str) or len(letter) != 1 or letter not in LETTERS:
            return None
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 0xFFFF:
            return None
        work.append((letter, number))
    return work


class Api:
    def __init__(self, manager, emit, pick_folder, default_dest, audio_server, driver_setup=None,
                 pick_wav=None, updater=None, quit_app=None, can_install=False, before_install=None):
        self._manager = manager            # private attributes are not exposed to JS
        self._emit = emit
        self._pick = pick_folder             # (start_dir) -> path or None (a folder dialog)
        self._dest = default_dest             # the current save folder (changed with choose_destination)
        self._server = audio_server
        self._driver_setup = driver_setup     # () -> (exit_code, log); see app/driver_setup.py
        self._pick_wav = pick_wav             # (start_dir) -> path or None (a file dialog)
        self._saved = {}                      # id -> path, from the last list_saved()
        self._updater = updater               # app.updater (check / download / launch), or None
        self._quit = quit_app                 # () -> None: close the window without asking
        self._can_install = can_install       # only the installed app can replace itself
        self._update = None                   # the release found by the last check_update()
        self._before_install = before_install # () -> None, just before the installer starts
        self._updating = False
        self._busy = threading.Lock()      # held while an export runs
        self._stop = threading.Event()
        self._thread = None

    def capabilities(self):
        return {"wav": audio.available(), "wav_status": audio.status(), "version": __version__}

    def devices(self):
        try:
            return {"ok": True, "devices": self._manager.refresh()}
        except Exception as e:
            return _error(e)

    def recordings(self, device_id):
        try:
            folders = self._manager.with_session(device_id, lambda s: [(l, s.messages(l)) for l in LETTERS])
        except Exception as e:
            return _error(e)
        return {"ok": True, "folders": [{"letter": l, "recordings": [_recording(m) for m in msgs]}
                                        for l, msgs in folders]}

    def audio(self, device_id, letter, number):
        if not audio.available():
            return _fail(f"Playback: {audio.status()}.")
        work = _parse_items([{"folder": letter, "number": number}])
        if work is None:
            return _fail("No such recording.")
        try:
            return {"ok": True, **self._server.prepare((device_id, *work[0]))}
        except Exception as e:
            return _error(e)

    def default_destination(self):
        return self._dest

    def _start_folder(self):
        """Where the file and folder dialogs start: the save folder, or its parent (Documents)
        while nothing has been exported yet."""
        for d in (self._dest, os.path.dirname(self._dest)):
            if os.path.isdir(d):
                return d
        return None

    # ---- saved recordings (the save folder) -------------------------------------
    def list_saved(self):
        """The .dvf and .wav files in the save folder and its subfolders (one level:
        exports go to <folder>/<recorder folder letter>/). The page gets ids, never paths."""
        folder = self._dest
        if not os.path.isdir(folder):
            self._saved = {}
            return {"ok": True, "folder": folder, "exists": False, "files": [], "truncated": False}
        found = []
        for sub in [""] + sorted(e.name for e in os.scandir(folder) if e.is_dir()):
            where = os.path.join(folder, sub)
            try:
                entries = sorted(os.scandir(where), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                kind = os.path.splitext(e.name)[1].lower()[1:]
                if kind in ("dvf", "wav") and e.is_file():
                    found.append((sub, e.name, kind, e.path))
        truncated = len(found) > SAVED_LIMIT
        self._saved = {}
        files = []
        for sub, name, kind, path in found[:SAVED_LIMIT]:
            fid = hashlib.sha1(os.path.normcase(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]
            self._saved[fid] = path
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append({"id": fid, "name": name, "folder": sub, "type": kind, "seconds": _seconds(path, kind),
                          "size": st.st_size,
                          "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        return {"ok": True, "folder": folder, "exists": True, "files": files, "truncated": truncated}

    def play_saved(self, file_id):
        """Prepare a saved file (by id from list_saved) for the player."""
        path = self._saved.get(file_id) if isinstance(file_id, str) else None
        if not path or not os.path.isfile(path):
            return _fail("That file is no longer there. Refresh the list.")
        name = os.path.basename(path)
        try:
            if name.lower().endswith(".wav"):
                return {"ok": True, "name": name, **self._server.prepare_file(path)}
            if not audio.available():
                return _fail(f"Playing .dvf files: {audio.status()}.")
            st = os.stat(path)
            key = ("dvf", os.path.normcase(path), st.st_size, st.st_mtime_ns)

            def decode():
                with open(path, "rb") as f:
                    return audio.dvf_to_wav(f.read(), should_stop=self._stop.is_set)
            return {"ok": True, "name": name, **self._server.prepare(key, make=decode)}
        except Exception as e:
            return _fail(f"Could not play {name}: {e}")

    def open_wav(self):
        """Let the user pick a WAV file and prepare it for the player. The path comes
        only from the file dialog, never from the page."""
        if self._pick_wav is None:
            return _fail("Opening files is not available here.")
        try:
            path = self._pick_wav(self._start_folder())
        except Exception as e:
            return _fail(f"Could not show the file dialog: {e}")
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            info = self._server.prepare_file(path)
        except (OSError, ValueError) as e:
            return _fail(f"Could not open {os.path.basename(path)}: {e}")
        return {"ok": True, "name": os.path.basename(path), **info}

    # ---- updates ------------------------------------------------------------------
    def check_update(self):
        """Is a newer release out? {ok, available, current[, version, notes, page]}."""
        current = {"ok": True, "available": False, "current": __version__}
        if self._updater is None:
            return current
        try:
            info = self._updater.check(__version__)
        except Exception as e:
            return _fail(_update_problem(e))
        self._update = info
        if not info:
            return current
        return {**current, "available": True, "version": info["version"], "notes": info["notes"],
                "page": info["page"], "can_install": self._can_install}

    def install_update(self):
        """Download the release found by check_update(), check its signature, start
        its installer and close the app so the installer can replace it."""
        info = self._update
        if not info:
            return _fail("Check for updates first.")
        if not self._can_install:
            return _fail("Updates install only in the installed app, not when running from source.")
        if self._stop.is_set():
            return _fail("The app is closing.")
        if not self._busy.acquire(blocking=False):      # held from here on: no export can start
            return _fail("Wait for the export to finish, then update.")
        self._updating = True
        shown = [-1]

        def progress(fraction):                         # whole percents only: each is a JS call
            pct = int(100 * fraction)
            if pct != shown[0]:
                shown[0] = pct
                self._emit("update-progress", {"percent": pct})
        path = None
        try:
            path = self._updater.download(info, progress=progress, cancelled=self._stop.is_set)
            if self._stop.is_set():                     # the window was closed while downloading
                raise UpdateCancelled()
            if self._before_install:
                self._before_install()
            self._updater.launch(path)
        except Exception as e:
            if path:                                    # downloaded but not started: don't keep it
                self._updater.discard(path)
            self._updating = False
            self._busy.release()
            if isinstance(e, UpdateCancelled):
                return _fail("The update was cancelled.")
            return _fail(_update_problem(e, "The update was not installed"))
        if self._quit:
            self._quit()
        return {"ok": True}

    def updating(self):
        return self._updating

    def setup_driver(self):
        """Install the recorder's WinUSB driver (one Windows admin prompt)."""
        if self._driver_setup is None:
            return _fail("Driver setup is only available in the Windows app.")
        try:
            code, log = self._driver_setup()
        except Exception as e:                  # declined prompt, or Windows refused to start it
            return _fail(f"The recorder was not set up: {e}.")
        if code not in (0, 3010):
            last = [line for line in log.splitlines() if line.strip()][-1:] or ["no details were logged"]
            return _fail(f"The recorder was not set up (code {code}): {last[0]}")
        return {"ok": True, "restart": code == 3010}

    def open_folder(self, path):
        """Show an export folder in Explorer. Folders only: opening a file would run it."""
        if not isinstance(path, str) or not os.path.isdir(path):
            return _fail("That folder does not exist.")
        return {"ok": bool(open_folder(path))}

    def choose_destination(self):
        """Let the user pick the save folder, starting in the current one; returns it
        (or None if cancelled)."""
        try:
            picked = self._pick(self._start_folder())
        except Exception:                       # the dialog failed; keep the current folder
            return None
        if picked:
            self._dest = picked
        return picked

    def export(self, device_id, items, fmt, dest, job):
        if self._stop.is_set():
            return _fail("The app is closing.")
        if fmt not in ("dvf", "wav"):
            return _fail(f"Unknown format {fmt!r}.")
        if fmt == "wav" and not audio.available():
            return _fail(f"WAV export: {audio.status()}.")
        work = _parse_items(items)
        if work is None:
            return _fail("Nothing valid is selected.")
        if not isinstance(dest, str) or not dest:
            return _fail("Choose a folder to save to.")
        if not self._busy.acquire(blocking=False):
            return _fail("An export is already running.")
        thread = threading.Thread(target=self._export, args=(device_id, work, fmt, dest, job), name="export")
        try:
            thread.start()
        except Exception as e:
            self._busy.release()
            return _fail(f"Could not start the export: {e}")
        self._thread = thread
        return {"ok": True, "job": job}

    def exporting(self):
        return self._busy.locked() and not self._updating

    def stopping(self):
        """True once the app is closing: long decodes poll this to stop early."""
        return self._stop.is_set()

    def request_stop(self):
        """Refuse further exports immediately (called from main.py when the user
        confirms closing, before webview.start() has returned)."""
        self._stop.set()

    def shutdown(self):
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join()

    def _export(self, device_id, work, fmt, dest, job):
        saved = skipped = 0
        notes = []

        def failed(err):
            self._emit("export-failed", {**err, "job": job, "saved": saved, "skipped": skipped, "notes": notes})

        try:
            for i, (letter, number) in enumerate(work, 1):
                if self._stop.is_set():
                    failed(_fail("Stopped because the app is closing."))
                    return
                try:
                    dl = self._manager.with_session(device_id, lambda s, l=letter, n=number: s.download(l, n))
                except Exception as e:
                    failed(_error(e))
                    return
                if dl.error:
                    notes.append(f"{dl.label}: not saved ({dl.error})")
                    continue
                outdir = os.path.join(dest, letter)
                try:
                    os.makedirs(outdir, exist_ok=True)
                    if fmt == "dvf":
                        _, done = save_dvf(dl.dvf, outdir, dl.name)
                    else:
                        wav = audio.dvf_to_wav(dl.dvf, should_stop=self._stop.is_set)
                        _, done = save_wav(wav, outdir, dl.name[:-4] + ".wav")
                        del wav
                except audio.Cancelled:            # the window was closed during a long decode
                    failed(_fail("Stopped because the app is closing."))
                    return
                except OSError as e:
                    failed(_fail(f"Could not save {dl.label}: {e}", DISK))
                    return
                except Exception as e:             # the decoder rejected this recording
                    notes.append(f"{dl.label}: not converted ({e})")
                    continue
                if done:
                    skipped += 1
                else:
                    saved += 1
                self._emit("export-progress", {"job": job, "done": i, "total": len(work), "label": dl.label})
            self._emit("export-done", {"job": job, "saved": saved, "skipped": skipped, "notes": notes,
                                       "dest": dest})
        finally:
            self._busy.release()
