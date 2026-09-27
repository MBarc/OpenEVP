"""The API the desktop UI calls (pywebview exposes Api's public methods to JS).

Every public method returns JSON-able values and never raises: errors come back
as {"ok": False, "error", "advice", "state"} so the UI can show them. Exports
run on their own (non-daemon) thread and report through emit(); their
downloads still go through the DeviceManager's single USB thread, and files
are written only after each download has finished. shutdown() stops an export
between recordings and waits for it, so closing the window never abandons one
half-way through a file.

EVP marks live in the app data store (app.store.AppData), keyed by the audio
fingerprint of the decoded samples. The page never receives a file's path, and
it never names a recording by fingerprint: every loaded recording gets an
opaque "rec" handle, and the marks calls take that handle (the fingerprint
itself does reach the page, only to group copies of one recording). For a recording on a recorder the handle also holds the
exact .dvf bytes that were decoded for playback, so the backup made when the
recording is first marked saves exactly the audio that was marked. Backups run
on one background worker (registered with the export worker, so shutdown()
waits for both) and report through "backup-done" / "backup-failed" events.

The EVP library lists the .dvf/.wav files of one folder tree by opaque ids.
Fingerprints come from the store's index cache; files not in it are
fingerprinted by one background indexer (also a registered worker), which
reports "library-row" / "library-progress" / "library-done" events tagged with
the scan_id of the list_library() call that started it.

Library folders can be created, renamed and deleted (to the Recycle Bin only),
and recordings moved between them: see app/library_ops.py (mixed into Api) and
app/folders.py.
"""
import datetime
import io
import os
import secrets
import struct
import threading
import wave
from collections import OrderedDict

from st25 import __version__, audio, wavinfo
from st25.cli import open_folder
from st25.export import save_dvf, save_wav

from .updater import UpdateCancelled
from st25.folder import TableError
from st25.protocol import RecorderError
from st25.session import LETTERS
from st25.usb import DriverMissing, UsbError

from . import folders
from .devices import NEEDS_DRIVER, NEEDS_REPLUG, READY, DeviceGone
from .library_ops import (CLOSING, HANDLES, SESSION_CACHE, LibraryOps, _bounded_put, _fail,  # noqa: F401
                          _file_id, _folder_id, _plain)
from .library_ops import (BACKUP_RECYCLED, FS_BUSY, FS_WAIT, INDEXER_BUSY, LIB_CHANGED,  # noqa: F401
                          PATH_TOO_LONG)
from .store import StoreReadOnly, StoreUnavailable

REPLUG = "Unplug the recorder's USB cable, wait a few seconds and plug it back in."
# Stage 4 (installer) replaces this with a "Set up recorder" button.
DRIVER = "Click \"Set up recorder\" to install its driver (Windows will ask for permission once)."
DISK = "Check free disk space and that the folder can be written to."


NO_MARKS = "Marks are not available here."
RELOAD = "Load the recording again."
NO_AUDIO = "This recording has no audio to mark."
BACKUP_RUNNING = "Wait for the backup of a marked recording to finish, then update."
MARKED_BUSY = "Wait for the export to finish, then save the WAV with marks."
MARKED_BUSY_UPDATE = "An update is being installed; the WAV with marks was not saved."


class _BackupRunning(Exception):
    """install_update: a backup is running when the installer would take over."""


def _download(manager, key):
    """The session's Download for one recording; raises if it cannot be played."""
    device_id, letter, number = key
    dl = manager.with_session(device_id, lambda s: s.download(letter, number))
    if dl.error:
        raise ValueError(f"{dl.label} cannot be played: {dl.error}")
    return dl


def recording_wav(manager, key, should_stop=None):
    """WAV bytes for one recording (the AudioServer provider). ``should_stop``
    lets app shutdown interrupt a long decode (see audio.dvf_to_wav)."""
    return audio.dvf_to_wav(_download(manager, key).dvf, should_stop=should_stop)


def _investigation(path, library):
    """The investigation a file belongs to: the first folder under the library
    folder ("" for a file directly in it, or for one outside the library)."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(library))
    except ValueError:                          # another drive
        return ""
    parts = rel.split(os.sep)
    if parts[0] == os.pardir or len(parts) < 2:
        return ""
    return parts[0]


SAVED_LIMIT = 5000          # the library lists at most this many files (the page says so)
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


INDEX_FLUSH_EVERY = 20      # the library indexer writes its cache after this many files
DVF_MAX_BYTES = 512 << 20   # far beyond any ICD-ST25 recording (~200 hours of LP audio)


def _stat_of(st):
    """(size, mtime_ns) of an os.stat result: one version of a file on disk."""
    return st.st_size, st.st_mtime_ns


def _wav_length(f):
    """Exact length in seconds of a PCM WAV (a path or a file object)."""
    with wave.open(f) as w:
        return w.getnframes() / w.getframerate() if w.getframerate() else None


def _scan_library(folder):
    """([(investigation, name, kind, path, stat, folder_rel)], [folder_rel, ...],
    truncated, complete): every .dvf/.wav in folder and all its subfolders -- a
    folder's files first (by name), then its subfolders in name order. Walking
    stops as soon as more than SAVED_LIMIT files were found. Symlinked folders
    and junctions are not followed (no loops); names starting with "." (Mac
    "._x.wav" companions, temp files, hidden folders) are skipped. complete is
    False when a folder could not be read (its files are missing from the
    list). investigation is the first folder under `folder` ("" for files
    directly in it). The folders list holds every subfolder walked (including
    empty ones), each as a tuple of relative path parts from `folder`;
    folder_rel is that same tuple for the folder directly containing the file
    (empty for a file in `folder` itself)."""
    found = []
    subfolders = []
    complete = True
    stack = [(folder, "", ())]
    while stack:
        where, investigation, rel = stack.pop()
        try:
            with os.scandir(where) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            complete = False
            continue
        subdirs = []
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if folders.entry_is_link(e):
                    continue
                if e.is_dir():
                    subdirs.append(e)
                    continue
                kind = os.path.splitext(e.name)[1].lower()[1:]
                if kind not in ("dvf", "wav") or not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            found.append((investigation, e.name, kind, e.path, st, rel))
            if len(found) > SAVED_LIMIT:
                return found[:SAVED_LIMIT], subfolders, True, complete
        for d in subdirs:
            subfolders.append(rel + (d.name,))
        for d in reversed(subdirs):
            stack.append((d.path, investigation or d.name, rel + (d.name,)))
    return found, subfolders, False, complete


def _marks_row(counts, reviewed, notes):
    """A library row's marks fields: counts per class, reviewed, and the lower-cased
    note text (for search)."""
    return {"marks": {c: counts.get(c, 0) for c in ("A", "B", "C")}, "reviewed": bool(reviewed),
            "notes": notes or ""}


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


class Api(LibraryOps):
    def __init__(self, manager, emit, pick_folder, default_dest, audio_server, driver_setup=None,
                 pick_wav=None, updater=None, quit_app=None, can_install=False, before_install=None,
                 store=None, store_problems=(), recycle=None):
        self._manager = manager            # private attributes are not exposed to JS
        self._emit = emit
        self._pick = pick_folder             # (start_dir) -> path or None (a folder dialog)
        self._dest = default_dest             # the current save folder (changed with choose_destination)
        self._server = audio_server
        self._driver_setup = driver_setup     # () -> (exit_code, log); see app/driver_setup.py
        self._pick_wav = pick_wav             # (start_dir) -> path or None (a file dialog)
        self._updater = updater               # app.updater (check / download / launch), or None
        self._quit = quit_app                 # () -> None: close the window without asking
        self._can_install = can_install       # only the installed app can replace itself
        self._update = None                   # the release found by the last check_update()
        self._before_install = before_install # () -> None, just before the installer starts
        self._updating = False
        self._busy = threading.Lock()      # held while an export, export_marked() or an update install runs
        self._marked_done = None              # threading.Event while export_marked() runs; shutdown waits for it
        self._stop = threading.Event()
        self._workers = []                    # every background thread (export, backup); shutdown joins them
        self._workers_lock = threading.Lock()
        self._store = store                   # app.store.AppData, or None: no marks
        self._store_problems = list(store_problems)   # why the store could not be opened (from main.py)
        self._recs_lock = threading.Lock()
        self._recs = OrderedDict()            # rec handle -> {"fp", "duration", "name", "label", "source"}
        self._dvfs = OrderedDict()            # (device_id, letter, number) -> (Download, fp of its decode)
        self._backup_lock = threading.Lock()
        self._backup_queue = OrderedDict()    # fp -> (rec, entry): one pending backup per recording
        self._backup_running = None           # fp being backed up right now
        self._backup_thread = None
        self._lib_lock = threading.Lock()
        self._library = {}                    # id -> path, from the last list_library()
        self._library_folders = {}            # folder id -> path, from the last list_library()
        self._scan_id = 0                     # bumped by every list_library(); in every library-* event
        self._lib_job = None                  # (scan_id, files to fingerprint) waiting for the indexer
        self._indexer = None                  # the one library indexer thread, while it runs
        self._lib_folder = None               # the library folder chosen in this session
        self._scan_folder = None              # normcased folder of the latest scan
        self._fp_session = OrderedDict()      # index key -> (size, mtime_ns, entry): read-only store
        self._recycle = recycle or folders.recycle   # (path) -> None or raises folders.RecycleError
        self._fs_op = False                   # a folder operation is moving files (under _lib_lock)
        self._fs_gen = 0                      # bumped when one starts and ends: an older scan must not prune
        if store is not None:
            saved = store.get_setting("save_folder")
            if isinstance(saved, str) and saved and os.path.isdir(saved):
                self._dest = saved

    def capabilities(self):
        """What this app can do. Store problems (a damaged marks file set aside, a
        second window) are reported here, once, for the page to show."""
        store = self._store
        return {"wav": audio.available(), "wav_status": audio.status(), "version": __version__,
                "marks": store is not None, "marks_read_only": bool(store is not None and store.read_only),
                "store_problems": self._store_problems + (store.problems() if store is not None else [])}

    def devices(self):
        try:
            return {"ok": True, "devices": self._manager.refresh()}
        except Exception as e:
            return _error(e)

    def recordings(self, device_id):
        try:
            by_letter = self._manager.with_session(device_id, lambda s: [(l, s.messages(l)) for l in LETTERS])
        except Exception as e:
            return _error(e)
        return {"ok": True, "folders": [{"letter": l, "recordings": [_recording(m) for m in msgs]}
                                        for l, msgs in by_letter]}

    def audio(self, device_id, letter, number):
        if not audio.available():
            return _fail(f"Playback: {audio.status()}.")
        work = _parse_items([{"folder": letter, "number": number}])
        if work is None:
            return _fail("No such recording.")
        key = (device_id, *work[0])
        letter, number = work[0]

        made = []

        def make():                      # runs only when the audio server has no decode cached
            dl = _download(self._manager, key)
            made.append(dl)
            return audio.dvf_to_wav(dl.dvf, should_stop=self._stop.is_set)
        try:
            info = self._server.prepare(key, make=make)
        except Exception as e:
            return _error(e)
        fp = info.get("fp")
        with self._recs_lock:
            if made:                     # decoded by this call: info describes exactly these bytes
                dl = made[-1]
                _bounded_put(self._dvfs, key, (dl, fp))
            else:                        # a cached decode: trust a capture only with the same audio
                held = self._dvfs.get(key)
                dl = held[0] if held is not None and fp is not None and held[1] == fp else None
                if dl is not None:
                    self._dvfs.move_to_end(key)
        # Without the captured bytes (evicted meanwhile, or not matching) a backup downloads
        # once more and checks the audio is still the same before trusting it (_device_audio).
        label = f"{letter}-{number:03d}"
        source = {"kind": "device", "device": device_id, "letter": letter, "number": number, "label": label,
                  "dvf": dl.dvf if dl else None, "dvf_name": dl.name if dl else None}
        return self._loaded(info, dl.name if dl else label, label, source)

    # ---- recording handles and marks ------------------------------------------
    def _loaded(self, info, name, label, source, extra=None):
        """The player's result for a loaded recording: the audio server's info, a new
        rec handle, and the recording's marks, reviewed flag and backup status."""
        rec = secrets.token_hex(8)
        entry = {"fp": info.get("fp"), "duration": info.get("duration"), "name": name, "label": label,
                 "source": source}
        with self._recs_lock:
            _bounded_put(self._recs, rec, entry)
        return {"ok": True, **info, **(extra or {}), "rec": rec, **self._marks_state(entry["fp"], source)}

    def _marks_state(self, fp, source=None):
        """marks, reviewed, backup; backup_needed is True for a marked recorder
        recording whose backup is not saved (never queued, refused, or failed), so
        the page can offer Retry backup even when no backup event will come."""
        r = self._store.recording(fp) if self._store is not None and fp else None
        if r is None:
            return {"marks": [], "reviewed": False, "backup": {"status": None, "detail": ""},
                    "backup_needed": False}
        needed = bool(source is not None and source.get("kind") == "device" and r["marks"]
                      and r["backup"]["status"] != "saved")
        return {"marks": r["marks"], "reviewed": r["reviewed"], "backup": r["backup"],
                "backup_needed": needed}

    def _entry(self, rec):
        if not isinstance(rec, str):
            return None
        with self._recs_lock:
            entry = self._recs.get(rec)
            if entry is not None:
                self._recs.move_to_end(rec)
            return entry

    def _with_mark_store(self, rec, action):
        """Run action(entry) for a marks call, turning every failure into _fail()."""
        if self._store is None:
            return _fail(NO_MARKS)
        entry = self._entry(rec)
        if entry is None:
            return _fail(RELOAD)
        if not entry["fp"]:                   # an empty WAV has no fingerprint (see _analyze)
            return _fail(NO_AUDIO)
        try:
            return action(entry)
        except (StoreReadOnly, StoreUnavailable, ValueError) as e:
            return _fail(str(e))
        except Exception as e:
            return _fail(f"{type(e).__name__}: {e}")

    def get_marks(self, rec):
        return self._with_mark_store(rec, lambda e: {"ok": True, **self._marks_state(e["fp"], e["source"])})

    def add_mark(self, rec, start, end, cls, note=""):
        """Add a mark. The first mark on a recorder recording (or the next one after a
        failed backup) also queues a backup into the Save-to folder; its result comes
        as a "backup-done" / "backup-failed" event, never by delaying the mark."""
        def act(e):
            mark = self._store.add_mark(e["fp"], start, end, cls, note, name=e["name"], duration=e["duration"])
            return {"ok": True, "mark": mark, "backup_queued": self._queue_backup(rec, e)}
        return self._with_mark_store(rec, act)

    def update_mark(self, rec, mark_id, start=None, end=None, cls=None, note=None):
        return self._with_mark_store(rec, lambda e: {"ok": True, "mark": self._store.update_mark(
            e["fp"], mark_id, cls=cls, note=note, start=start, end=end)})

    def delete_mark(self, rec, mark_id):
        return self._with_mark_store(rec, lambda e: {"ok": True, "deleted": self._store.delete_mark(e["fp"], mark_id)})

    def set_reviewed(self, rec, reviewed):
        def act(e):
            self._store.set_reviewed(e["fp"], reviewed)
            return {"ok": True, "reviewed": reviewed}
        return self._with_mark_store(rec, act)

    def retry_backup(self, rec):
        """Back up a marked recorder recording whose backup failed (the marks list's
        Retry backup). {"ok", "queued"}: queued is False when nothing needs doing."""
        return self._with_mark_store(rec, lambda e: {"ok": True, "queued": self._queue_backup(rec, e)})

    # ---- backups of marked recorder recordings ----------------------------------
    def backing_up(self):
        """True while a backup is queued or being written."""
        with self._backup_lock:
            return bool(self._backup_queue) or self._backup_running is not None

    def _queue_backup(self, rec, entry):
        """Queue one backup of a recorder recording unless it is saved already or
        already queued/running. Returns True if a backup was queued."""
        if entry["source"]["kind"] != "device" or self._store.read_only:
            return False
        fp = entry["fp"]
        label = entry["source"]["label"]
        with self._backup_lock:
            # The status is read under the lock: a backup finishing meanwhile records
            # "saved" before it releases _backup_running, so it is never queued twice.
            if self._store.backup(fp)["status"] == "saved":
                return False
            if fp in self._backup_queue or fp == self._backup_running:
                return False
            # Not while closing or while an update installs (the installer replaces the
            # app). The mark is kept and shows as not backed up (backup_needed); the
            # next mark or Retry backup queues it again.
            if self._stop.is_set():
                refused = f"{label} was not backed up because the app is closing."
            elif self._updating:
                refused = f"{label} was not backed up because an update is being installed."
            else:
                refused = None
                self._backup_queue[fp] = (rec, entry)
                if self._backup_thread is None:
                    thread = threading.Thread(target=self._backup_worker, name="backup")
                    try:
                        thread.start()
                    except Exception as e:
                        self._backup_queue.pop(fp)
                        refused = f"{label} was not backed up: {_plain(e)}"
                    else:
                        self._backup_thread = thread
                        self._add_worker(thread)
        if refused is None:
            return True
        self._record_backup(fp, "failed", refused)
        return False

    def _backup_worker(self):
        me = threading.current_thread()
        try:
            while True:
                with self._backup_lock:
                    # The worker gives up its slot in the same locked step that finds
                    # nothing left to do: a backup queued a moment later then starts a
                    # new worker instead of joining a queue nobody reads any more.
                    if self._stop.is_set():
                        dropped = list(self._backup_queue.items())
                        self._backup_queue.clear()
                        self._backup_thread = None
                        break
                    if not self._backup_queue:
                        self._backup_thread = None
                        return
                    # A folder operation checks backing_up() after setting _fs_op, and
                    # this reads _fs_op under the same lock: never both at once.
                    paused = self._fs_op
                    if not paused:
                        fp, (rec, entry) = self._backup_queue.popitem(last=False)
                        self._backup_running = fp
                if paused:
                    self._stop.wait(0.05)
                    continue
                try:
                    self._backup(fp, rec, entry)
                except Exception as e:                # never let the worker die silently
                    self._backup_result(fp, rec, entry, "failed",
                                        f"{entry['source']['label']} was not backed up: {_plain(e)}")
                finally:
                    with self._backup_lock:
                        self._backup_running = None
            # The app is closing (shutdown() closes the store only after this thread
            # ends): record every backup that never started, so it shows as failed
            # and is retried the next time that recording is marked.
            for fp, (rec, entry) in dropped:
                self._closed_before_backup(fp, rec, entry)
        finally:
            # Only a worker that died unexpectedly still owns the slot. Then every
            # backup still queued is recorded as failed (shown with Retry backup),
            # never silently dropped.
            left = []
            with self._backup_lock:
                if self._backup_thread is me:
                    left = list(self._backup_queue.items())
                    self._backup_queue.clear()
                    self._backup_thread = None
                    self._backup_running = None
            for fp, (rec, entry) in left:
                detail = f"{entry['source']['label']} was not backed up: the backup stopped unexpectedly."
                try:
                    self._backup_result(fp, rec, entry, "failed", detail)
                except Exception:
                    self._record_backup(fp, "failed", detail)

    def _closed_before_backup(self, fp, rec, entry):
        self._backup_result(fp, rec, entry, "failed",
                            f"{entry['source']['label']} was not backed up because the app closed.")

    def _device_audio(self, entry):
        """(dvf bytes, dvf name, WAV or None) for a recorder recording's handle. When
        the handle has no captured bytes, the recording is downloaded once more, and
        trusted only if it decodes to the same audio that was loaded (then the WAV
        from that check is returned too). Raises ValueError in plain words."""
        src = entry["source"]
        with self._recs_lock:
            if src["dvf"] is not None:
                return src["dvf"], src["dvf_name"], None
        try:
            dl = _download(self._manager, (src["device"], src["letter"], src["number"]))
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(_error(e)["error"]) from e
        wav = audio.dvf_to_wav(dl.dvf, should_stop=self._stop.is_set)
        if wavinfo.wav_fingerprint(io.BytesIO(wav)) != entry["fp"]:
            raise ValueError(f"{src['label']} on the recorder is no longer the recording that was marked. "
                             "Load it again, then retry.")
        with self._recs_lock:
            src["dvf"], src["dvf_name"] = dl.dvf, dl.name
        return dl.dvf, dl.name, wav

    def _backup(self, fp, rec, entry):
        """Save the recording's .dvf into <Save to>/<letter>/, then (with the decoder)
        a WAV copy with the marks. "saved" once the .dvf is there; a failed WAV copy
        is reported in the detail, not as a failed backup."""
        src = entry["source"]
        label = src["label"]
        try:
            data, dvf_name, wav = self._device_audio(entry)
        except audio.Cancelled:                       # the app is closing; retried on the next mark
            self._closed_before_backup(fp, rec, entry)
            return
        except ValueError as e:
            self._backup_result(fp, rec, entry, "failed", f"{label} was not backed up: {e}")
            return
        outdir = os.path.join(self._dest, src["letter"])
        try:
            os.makedirs(outdir, exist_ok=True)
            path, already = save_dvf(data, outdir, dvf_name)
        except OSError as e:
            self._backup_result(fp, rec, entry, "failed", f"{label} was not backed up: {_plain(e)}")
            return
        name = os.path.basename(path)
        detail = f"{name} was already saved" if already else f"Saved as {name}"
        if audio.available():
            wav_name = os.path.splitext(dvf_name)[0] + ".wav"
            try:
                if wav is None:
                    wav = audio.dvf_to_wav(data, should_stop=self._stop.is_set)
                wav_path, _ = save_wav(wavinfo.with_markers(wav, self._store.marks(fp)), outdir, wav_name)
                detail += f", with a WAV copy ({os.path.basename(wav_path)})"
            except audio.Cancelled:
                detail += ", but the WAV copy was not made because the app is closing"
            except Exception as e:
                detail += f", but the WAV copy failed: {_plain(e)}"
        self._backup_result(fp, rec, entry, "saved", detail + ".")

    def _record_backup(self, fp, status, detail):
        """Record a backup status; returns detail, extended if it could not be recorded."""
        try:
            self._store.set_backup(fp, status, detail)
        except (StoreReadOnly, StoreUnavailable, ValueError) as e:
            detail += f" (This could not be recorded: {e})"
        return detail

    def _backup_result(self, fp, rec, entry, status, detail):
        detail = self._record_backup(fp, status, detail)
        self._emit("backup-done" if status == "saved" else "backup-failed",
                   {"rec": rec, "label": entry["source"]["label"], "detail": detail})

    # ---- WAV with marks ----------------------------------------------------------
    def _marked_wav(self, wav):
        """(wav, note): a freshly decoded WAV with the marks of its recording written
        in, or unchanged when it has none. note says why marks were left out."""
        if self._store is None:
            return wav, None
        try:
            marks = self._store.marks(wavinfo.wav_fingerprint(io.BytesIO(wav)))
        except ValueError:
            return wav, None
        if not marks:
            return wav, None
        try:
            return wavinfo.with_markers(wav, marks), None
        except ValueError as e:
            return wav, f"saved without its marks ({e})"

    def export_marked(self, rec):
        """Save a WAV with the current marks of the loaded recording into the Save-to
        folder: <letter>/ for a recorder recording; for a file in the library, the
        folder named like its investigation (the first folder under the library);
        any other file goes into the Save-to folder itself. Never replaces a file: identical bytes
        count as already saved, anything else gets a numbered name.

        It runs on the caller's thread while holding _busy, like an export: it does
        not start while an export or an update install runs, and neither starts
        while it runs. Admission checks _stop under _workers_lock, the lock
        shutdown() takes after setting _stop, so shutdown() either refuses it or
        sees it and waits for it before closing the store."""
        if self._store is None:
            return _fail(NO_MARKS)
        entry = self._entry(rec)
        if entry is None:
            return _fail(RELOAD)
        if not entry["fp"]:
            return _fail(NO_AUDIO)
        with self._workers_lock:
            if self._stop.is_set():
                return _fail(CLOSING)
            if not self._busy.acquire(blocking=False):
                return _fail(MARKED_BUSY_UPDATE if self._updating else MARKED_BUSY)
            done = self._marked_done = threading.Event()
        try:
            return self._export_marked(entry)
        finally:
            with self._workers_lock:
                self._marked_done = None
            self._busy.release()
            done.set()

    def saving_marked(self):
        """True while export_marked() runs (the close prompt says so)."""
        return self._marked_done is not None

    def _export_marked(self, entry):
        marks = self._store.marks(entry["fp"])
        if not marks:
            return _fail("This recording has no marks yet.")
        src = entry["source"]
        try:
            if src["kind"] == "device":
                if not audio.available():
                    return _fail(f"WAV export: {audio.status()}.")
                try:
                    data, dvf_name, wav = self._device_audio(entry)
                except ValueError as e:
                    return _fail(str(e))
                if wav is None:
                    wav = audio.dvf_to_wav(data, should_stop=self._stop.is_set)
                outdir = os.path.join(self._dest, src["letter"])
                out_name = os.path.splitext(dvf_name)[0] + ".wav"
            else:
                path = src["path"]
                name = os.path.basename(path)
                with open(path, "rb") as f:
                    raw = f.read()
                if name.lower().endswith(".dvf"):
                    if not audio.available():
                        return _fail(f"WAV export: {audio.status()}.")
                    wav = audio.dvf_to_wav(raw, should_stop=self._stop.is_set)
                else:
                    wav = raw
                del raw
                try:
                    same = wavinfo.wav_fingerprint(io.BytesIO(wav)) == entry["fp"]
                except ValueError:
                    same = False
                if not same:
                    return _fail(f"{name} has changed since it was loaded. Load it again.")
                investigation = _investigation(path, self._library_path())
                outdir = os.path.join(self._dest, investigation) if investigation else self._dest
                out_name = os.path.splitext(name)[0] + ".wav"
            marked = wavinfo.with_markers(wav, marks)
        except audio.Cancelled:
            return _fail(CLOSING)
        except OSError as e:
            return _fail(f"Could not read {entry['name']}: {_plain(e)}")
        except ValueError as e:
            return _fail(f"Could not add the marks to {entry['name']}: {e}")
        except Exception as e:
            return _error(e)
        del wav
        try:
            os.makedirs(outdir, exist_ok=True)
            path, already = save_wav(marked, outdir, out_name)
        except OSError as e:
            return _fail(f"Could not save {out_name}: {_plain(e)}", DISK)
        folder_name = os.path.basename(outdir) if outdir != self._dest else ""   # the subfolder's name, never a path
        return {"ok": True, "saved": not already, "already": already, "name": os.path.basename(path),
                "folder_name": folder_name}

    def _import_markers(self, path, info, name, stat):
        """Import a WAV's embedded markers, once per recording (the store decides:
        never when it was imported before or already has marks). Returns the count.
        stat is the (size, mtime_ns) of the file when info["fp"] was computed: the
        markers are read only from that same version of the file, never from one
        that was replaced or edited since (they would land on the wrong recording)."""
        fp = info.get("fp")
        if self._store is None or not fp or self._store.read_only or stat is None:
            return 0
        try:
            if self._store.is_imported(fp):     # import_marks refuses (and flags) a marked one
                return 0
            with open(path, "rb") as f:
                if _stat_of(os.fstat(f.fileno())) != tuple(stat):
                    return 0
                found = wavinfo.read_markers(f)
                if _stat_of(os.fstat(f.fileno())) != tuple(stat):
                    return 0
            if not found:
                return 0
            return self._store.import_marks(fp, found, name, info.get("duration"))
        except (StoreReadOnly, StoreUnavailable, ValueError, OSError):
            return 0

    def recording_changed(self, rec):
        """Is a loaded file no longer the version that was fingerprinted (edited,
        replaced or removed)? The page asks when the player fails to load it."""
        entry = self._entry(rec)
        if entry is None:
            return {"ok": True, "changed": False}
        src = entry["source"]
        if src.get("kind") != "file" or src.get("stat") is None:
            return {"ok": True, "changed": False}
        try:
            return {"ok": True, "changed": _stat_of(os.stat(src["path"])) != tuple(src["stat"])}
        except OSError:
            return {"ok": True, "changed": True}

    def default_destination(self):
        return self._dest

    def _start_folder(self):
        """Where the file and folder dialogs start: the save folder, or its parent (Documents)
        while nothing has been exported yet."""
        for d in (self._dest, os.path.dirname(self._dest)):
            if os.path.isdir(d):
                return d
        return None

    # ---- files on disk (library files and WAVs opened from the file dialog) -------
    def _play_file(self, path):
        """The player's result for a .wav (served in place, its embedded markers
        imported once) or a .dvf (decoded through the audio server) on disk."""
        if not path or not os.path.isfile(path):
            return _fail("That file is no longer there. Refresh the list.")
        name = os.path.basename(path)
        source = {"kind": "file", "path": path}
        try:
            if name.lower().endswith(".wav"):
                info = self._server.prepare_file(path)
                source["stat"] = info.pop("stat", None)
                imported = self._import_markers(path, info, name, source["stat"])
                return self._loaded(info, name, name, source, {"name": name, "imported": imported})
            if not audio.available():
                return _fail(f"Playing .dvf files: {audio.status()}.")
            st = os.stat(path)
            key = ("dvf", os.path.normcase(path), st.st_size, st.st_mtime_ns)

            def decode():
                with open(path, "rb") as f:
                    return audio.dvf_to_wav(f.read(), should_stop=self._stop.is_set)
            return self._loaded(self._server.prepare(key, make=decode), name, name, source, {"name": name})
        except Exception as e:
            return _fail(f"Could not play {name}: {_plain(e)}")

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
            return _fail(f"Could not open {os.path.basename(path)}: {_plain(e)}")
        name = os.path.basename(path)
        stat = info.pop("stat", None)
        imported = self._import_markers(path, info, name, stat)
        return self._loaded(info, name, name, {"kind": "file", "path": path, "stat": stat},
                            {"name": name, "imported": imported})

    # ---- the EVP library (a folder of recordings, fingerprinted in the background) ----
    def _library_path(self):
        folder = self._lib_folder
        if folder is None and self._store is not None:
            folder = self._store.get_setting("library_folder")
        return folder if isinstance(folder, str) and folder else self._dest

    def library_folder(self):
        """The library folder (default: the Save-to folder); shown to the user."""
        folder = self._library_path()
        return {"ok": True, "folder": folder, "exists": os.path.isdir(folder)}

    def choose_library_folder(self):
        """Let the user pick the library folder, starting in the current one; returns
        it (or None if cancelled). Remembered for the next start."""
        current = self._library_path()
        try:
            picked = self._pick(current if os.path.isdir(current) else self._start_folder())
        except Exception:                       # the dialog failed; keep the current folder
            return None
        if not picked:
            return None
        self._lib_folder = picked
        if self._store is not None:
            try:
                self._store.set_setting("library_folder", picked)
            except (StoreReadOnly, StoreUnavailable):
                pass                            # used for this session; not remembered
        return picked

    def list_library(self):
        """Every .dvf/.wav in the library folder and all its subfolders (at most
        SAVED_LIMIT). Returns at once, from the folder listing and the fingerprint
        cache: files not fingerprinted yet have fp None and go to the background
        indexer, whose "library-row", "library-progress" and "library-done" events
        carry this call's scan_id. The page gets ids, never paths."""
        try:
            return self._list_library()
        except Exception as e:
            return _fail(f"Could not list the library: {_plain(e)}")

    def _list_library(self):
        folder = self._library_path()
        with self._lib_lock:
            self._scan_id += 1                  # the running job hands over to this scan's job
            scan_id = self._scan_id
            fs_gen = self._fs_gen
            self._scan_folder = os.path.normcase(os.path.abspath(folder))   # another folder: cancel
        result = {"ok": True, "folder": folder, "scan_id": scan_id, "exists": os.path.isdir(folder),
                  "truncated": False, "indexing": False, "pending": 0, "files": [], "folders": []}
        if not result["exists"]:
            with self._lib_lock:
                if scan_id == self._scan_id:
                    self._library = {}
                    self._library_folders = {}
                    self._lib_job = None
            return result
        found, subfolders, truncated, complete = _scan_library(folder)
        store = self._store
        summary = store.summary() if store is not None else {}
        table, files, pending = {}, [], []
        for investigation, name, kind, path, st, rel in found:
            fid = _file_id(path)
            table[fid] = path
            fp = error = seconds = None
            if store is None:
                seconds = _seconds(path, kind)
            else:
                cached = self._cached_fp(path, st.st_size, st.st_mtime_ns)
                if cached is None:
                    pending.append((fid, path, kind, st.st_size, st.st_mtime_ns))
                else:
                    fp, error, seconds = cached.get("fp"), cached.get("error"), cached.get("seconds")
            s = summary.get(fp) if fp else None
            files.append({"id": fid, "name": name, "investigation": investigation, "type": kind,
                          "seconds": seconds,
                          "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                          "fp": fp, **_marks_row(s or {}, s and s["reviewed"], s and s["notes"]),
                          "error": error, "folder_id": _folder_id(rel)})
        folder_table = {"root": folder}
        folder_rows = [{"id": "root", "parent": None, "name": os.path.basename(os.path.normpath(folder)),
                        "rel": []}]
        for parts in sorted(subfolders, key=lambda p: tuple(part.lower() for part in p)):
            fid = _folder_id(parts)
            folder_table[fid] = os.path.join(folder, *parts)
            folder_rows.append({"id": fid, "parent": _folder_id(parts[:-1]), "name": parts[-1],
                                "rel": list(parts)})
        if store is not None and not store.read_only:
            try:
                # Files past the limit, or in a folder that could not be read, were
                # not seen: their cache entries are kept.
                if not truncated and complete:
                    with self._lib_lock:        # not while (or if) a folder operation moved files
                        if not self._fs_op and self._fs_gen == fs_gen:
                            store.prune_index(folder, [f[3] for f in found])
                if not pending:
                    store.flush_index()
            except (StoreReadOnly, StoreUnavailable):
                pass
        result.update(truncated=truncated, files=files, folders=folder_rows)
        with self._lib_lock:
            if scan_id != self._scan_id:        # a newer list_library() overtook this one
                return result
            if self._fs_op or self._fs_gen != fs_gen:
                # A folder operation ran during this scan: its paths may be out of
                # date. The page lists again after the operation.
                result["paused"] = True
                return result
            self._library = table
            self._library_folders = folder_table
            self._lib_job = None
            if not pending or self._stop.is_set():
                return result
            self._lib_job = (scan_id, self._scan_folder, pending)
            if self._indexer is None:
                thread = threading.Thread(target=self._library_worker, name="library-indexer")
                try:
                    thread.start()
                except Exception:
                    self._lib_job = None        # the rows stay unindexed until the next scan
                    return result
                self._indexer = thread
                self._add_worker(thread)
        result.update(indexing=True, pending=len(pending))
        return result

    def _library_worker(self):
        """The one indexer thread. It runs the latest scan's job. A newer scan of
        the same folder takes over between files: the file being read finishes and
        is cached, so the newer job gets it for free. Another folder, or closing,
        also cancels a .dvf decode half-way. Two files are never decoded at once."""
        while True:
            with self._lib_lock:
                job, self._lib_job = self._lib_job, None
                if job is None or self._stop.is_set():
                    self._indexer = None
                    return
            try:
                self._index_job(*job)
            except Exception:                   # only when emit() itself fails; _index_job
                pass                            # reports its own errors in library-done

    def _index_job(self, scan_id, folder, pending):
        def superseded():
            return self._stop.is_set() or self._scan_id != scan_id or self._fs_op

        def cancelled():                        # polled during a .dvf decode or a WAV's hashing
            return self._stop.is_set() or self._scan_folder != folder or self._fs_op
        unflushed = 0
        try:
            decoder_problem = None
            if any(p[2] == "dvf" for p in pending) and not audio.available():
                decoder_problem = audio.status()
            for done, (fid, path, kind, size, mtime_ns) in enumerate(pending, 1):
                if superseded():
                    return
                row, stored = self._index_file(path, kind, size, mtime_ns, cancelled, decoder_problem)
                if stored:
                    unflushed += 1
                    if unflushed >= INDEX_FLUSH_EVERY:
                        unflushed = 0
                        self._flush_index()
                if superseded():                # kept, but the page has moved on
                    return
                if row is not None:
                    self._emit("library-row", {"scan_id": scan_id, "id": fid, **row})
                self._emit("library-progress", {"scan_id": scan_id, "done": done, "total": len(pending)})
            self._emit("library-done", {"scan_id": scan_id})
        except Exception as e:
            if not superseded():                # the page is waiting for this scan: tell it
                self._emit("library-done", {"scan_id": scan_id,
                                            "error": f"The library could not be fully indexed: {_plain(e)}"})
        finally:
            self._flush_index()

    def _flush_index(self):
        if self._store is None or self._store.read_only:
            return
        try:
            self._store.flush_index()
        except (StoreReadOnly, StoreUnavailable):
            pass

    def _cached_fp(self, path, size, mtime_ns):
        """The cached index entry of a file (fp, or "error"), or None. A read-only
        store (a second window) cannot be written, so its new results are kept in
        memory for this session; its index.json (loaded all the same) is still
        read."""
        store = self._store
        if store is None:
            return None
        with self._lib_lock:
            held = self._fp_session.get(os.path.normcase(os.path.abspath(path)))
        if held and held[:2] == (size, mtime_ns):
            return dict(held[2])
        return store.cached_fp(path, size, mtime_ns)

    def _remember_fp(self, path, size, mtime_ns, fp, seconds, error):
        """Cache one result. True if it went into the store's index (to be flushed)."""
        store = self._store
        if store is None:
            return False
        if store.read_only:
            entry = {"size": size, "mtime_ns": mtime_ns, "fp": fp, "seconds": seconds}
            if error is not None:
                entry["error"] = error
            with self._lib_lock:
                _bounded_put(self._fp_session, os.path.normcase(os.path.abspath(path)),
                             (size, mtime_ns, entry), SESSION_CACHE)
            return False
        try:
            store.remember_fp(path, size, mtime_ns, fp, seconds, error=error)
            return True
        except (StoreReadOnly, StoreUnavailable):
            return False

    def _index_file(self, path, kind, size, mtime_ns, cancelled, decoder_problem):
        """(the library-row fields or None, whether the store's index was written)
        for one file. None only when the decode was cancelled."""
        name = os.path.basename(path)
        hit = self._cached_fp(path, size, mtime_ns)
        if hit is not None:                     # done by the job this one took over from
            return {"fp": hit.get("fp"), "seconds": hit.get("seconds"), "error": hit.get("error"),
                    **self._fp_marks(hit.get("fp"))}, False
        fp = length = error = None
        cacheable = True
        try:
            if kind == "wav":
                fp = wavinfo.wav_fingerprint(path, should_stop=cancelled)
                length = _wav_length(path)
            elif decoder_problem:
                # Not the file's fault: nothing is cached, so a later scan tries
                # again once the decoder is there.
                error, cacheable = decoder_problem, False
            elif size > DVF_MAX_BYTES:
                error = f"{name} is too large to be an ICD-ST25 recording."
            else:
                with open(path, "rb") as f:
                    data = f.read()
                wav = audio.dvf_to_wav(data, should_stop=cancelled)
                del data
                fp = wavinfo.wav_fingerprint(io.BytesIO(wav))
                length = _wav_length(io.BytesIO(wav))
                del wav
        except (audio.Cancelled, wavinfo.Stopped):
            return None, False
        except (MemoryError, audio.DecoderUnavailable) as e:   # not the file's fault: not kept
            fp, error, cacheable = None, _plain(e), False
        except OSError as e:                    # locked or vanished: may work next time
            fp, error, cacheable = None, _plain(e), False
        except Exception as e:                  # not a readable recording: remembered
            fp, error = None, _plain(e)
        try:
            st = os.stat(path)
        except OSError:
            return {"fp": None, "seconds": None, "error": f"{name} is no longer there.",
                    **_marks_row({}, False, "")}, False
        if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
            # Changed while it was read (still being copied or recorded?): nothing
            # is kept, and the next scan reads it again.
            return {"fp": None, "seconds": None,
                    "error": f"{name} changed while it was being read. Refresh the list to try again.",
                    **_marks_row({}, False, "")}, False
        seconds = round(length, 1) if length else _seconds(path, kind)
        stored = cacheable and self._remember_fp(path, size, mtime_ns, fp, seconds, error)
        if kind == "wav" and fp and length:     # an empty WAV gets no marks (no identity of its own)
            self._import_markers(path, {"fp": fp, "duration": length}, name, (size, mtime_ns))
        return {"fp": fp, "seconds": seconds, "error": error, **self._fp_marks(fp)}, stored

    def _fp_marks(self, fp):
        """_marks_row for one recording, read straight from the store."""
        r = self._store.recording(fp) if self._store is not None and fp else None
        if r is None:
            return _marks_row({}, False, "")
        counts = {}
        for m in r["marks"]:
            counts[m["cls"]] = counts.get(m["cls"], 0) + 1
        return _marks_row(counts, r["reviewed"], "\n".join(m["note"].lower() for m in r["marks"] if m["note"]))

    def _library_file(self, file_id):
        if not isinstance(file_id, str):
            return None
        with self._lib_lock:
            return self._library.get(file_id)

    def play_library(self, file_id):
        """Prepare a library file (by id from list_library) for the player."""
        return self._play_file(self._library_file(file_id))

    def library_marks(self, file_id):
        """The marks of a library file's recording, by its cached fingerprint
        ([] while the file is not fingerprinted yet)."""
        path = self._library_file(file_id)
        if not path:
            return _fail("That file is no longer there. Refresh the list.")
        if self._store is None:
            return {"ok": True, "marks": []}
        try:
            st = os.stat(path)
        except OSError:
            return _fail("That file is no longer there. Refresh the list.")
        cached = self._cached_fp(path, st.st_size, st.st_mtime_ns)   # also a read-only window's own results
        fp = cached.get("fp") if cached else None
        return {"ok": True, "marks": self._store.marks(fp) if fp else []}

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
            return _fail(CLOSING)
        if not self._busy.acquire(blocking=False):      # held from here on: no export can start
            return _fail("Wait for the export to finish, then update.")
        with self._backup_lock:                         # from here on no backup can be queued
            if self._backup_queue or self._backup_running is not None:
                self._busy.release()
                return _fail(BACKUP_RUNNING)
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
            if self.backing_up():                       # never hand over while a backup is writing
                raise _BackupRunning()
            if self._before_install:
                self._before_install()
            self._updater.launch(path)
        except Exception as e:
            if path:                                    # downloaded but not started: don't keep it
                self._updater.discard(path)
            with self._backup_lock:
                self._updating = False
            self._busy.release()
            if isinstance(e, _BackupRunning):
                return _fail(BACKUP_RUNNING)
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
        (or None if cancelled). The choice is remembered for the next start."""
        try:
            picked = self._pick(self._start_folder())
        except Exception:                       # the dialog failed; keep the current folder
            return None
        if picked:
            self._dest = picked
            if self._store is not None:
                try:
                    self._store.set_setting("save_folder", picked)
                except (StoreReadOnly, StoreUnavailable):
                    pass                        # used for this session; not remembered
        return picked

    def export(self, device_id, items, fmt, dest, job):
        if self._stop.is_set():
            return _fail(CLOSING)
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
        with self._workers_lock:              # started and registered in one step against shutdown()
            if self._stop.is_set():
                self._busy.release()
                return _fail(CLOSING)
            try:
                thread.start()
            except Exception as e:
                self._busy.release()
                return _fail(f"Could not start the export: {e}")
            self._workers = [t for t in self._workers if t.is_alive()] + [thread]
        return {"ok": True, "job": job}

    def _add_worker(self, thread):
        """Register a started background thread for shutdown() to wait for."""
        with self._workers_lock:
            self._workers = [t for t in self._workers if t.is_alive()] + [thread]

    def exporting(self):
        """True while an export of recorder recordings runs (export_marked(): saving_marked())."""
        return self._busy.locked() and not self._updating and self._marked_done is None and not self._fs_op

    def stopping(self):
        """True once the app is closing: long decodes poll this to stop early."""
        return self._stop.is_set()

    def request_stop(self):
        """Refuse further exports immediately (called from main.py when the user
        confirms closing, before webview.start() has returned)."""
        self._stop.set()

    def shutdown(self):
        """Stop background work and wait for it (an export stops between recordings;
        a backup finishes the file it is writing, and queued backups are recorded as
        not made; the library indexer stops before its next file; a WAV being
        saved with its marks stops its decode or finishes writing), then close the
        store, which writes the fingerprint cache."""
        self._stop.set()
        with self._backup_lock:                 # no backup worker can start after this
            pass
        with self._lib_lock:                    # nor a library indexer
            pass
        with self._workers_lock:                # nor an export or export_marked()
            workers = list(self._workers)
            marked = self._marked_done
        for t in workers:
            if t.is_alive():
                t.join()
        if marked is not None:
            marked.wait()
        if self._store is not None:
            self._store.close()

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
                        wav, problem = self._marked_wav(wav)
                        if problem:
                            notes.append(f"{dl.label}: {problem}")
                        _, done = save_wav(wav, outdir, dl.name[:-4] + ".wav")
                        del wav
                except audio.Cancelled:            # the window was closed during a long decode
                    failed(_fail("Stopped because the app is closing."))
                    return
                except OSError as e:
                    failed(_fail(f"Could not save {dl.label}: {_plain(e)}", DISK))
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
