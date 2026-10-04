"""Persistent app data: EVP marks, settings and the fingerprint index.

Everything lives under one folder (``%APPDATA%\\OpenEVP`` in the app) in three
JSON files: ``settings.json``, ``marks.json`` and ``index.json``. Writes are
atomic (temp file + ``os.replace``) and a corrupt or unreadable file is set
aside rather than overwritten, so a bad write or an interrupted process never
loses the rest of the store.

Only one ``AppData`` may write to a folder at a time: the constructor takes an
exclusive lock on ``<folder>/.lock`` for the life of the object (trying for up
to ``lock_wait`` seconds, so a previous OpenEVP that is still closing does not
make this one read-only) and records who holds it in ``.lock.owner`` (process
id and program name, plain text). An instance that cannot get the lock opens
read-only -- reads still work, writes raise ``StoreReadOnly`` with the reason
(``read_only_reason``) -- and ``watch_lock()`` keeps trying in the background:
once it gets the lock it reloads everything from disk and becomes writable.
"""
import copy
import ctypes
import datetime
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import uuid

_WINDOWS = sys.platform.startswith("win")
if _WINDOWS:
    import msvcrt
else:
    import fcntl

CLASSES = ("A", "B", "C")
MIN_MARK_LENGTH = 0.05      # seconds; new marks made in the UI must be at least this long
MAX_NOTE_LENGTH = 500
DURATION_EPSILON = 0.01    # seconds of float slack allowed when validating a mark against
                            # a recording's duration at *load* time (not for new marks)
_BACKUP_STATUSES = ("saved", "failed", None)
_LABEL_RE = re.compile(r"^EVP ([ABC])(?::\s?(.*))?$")   # "EVP A: note" or bare "EVP A"


class StoreReadOnly(Exception):
    """A write was attempted on a read-only store (another instance holds the lock,
    or the lock file could not be opened)."""


class StoreUnavailable(Exception):
    """The store's JSON files could not be written (disk full, permissions, ...)."""


# ---- small helpers ---------------------------------------------------------------

def _finite_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _index_key(path):
    return os.path.normcase(os.path.abspath(path))


def _blank_recording():
    return {"marks": [], "reviewed": False, "name": "", "duration": None,
            "imported": False, "backup": _blank_backup()}


MAX_QUESTION_LENGTH = 300


def _clean_question(q, duration):
    """One question asked while recording ({"id", "at", "text", "created"}), or None."""
    if not isinstance(q, dict) or not isinstance(q.get("text"), str) or not _finite_number(q.get("at")):
        return None
    at, text = float(q["at"]), q["text"].strip()[:MAX_QUESTION_LENGTH]
    if at < 0 or not text or (duration is not None and at > duration + DURATION_EPSILON):
        return None
    qid = q.get("id") if isinstance(q.get("id"), str) and q.get("id") else uuid.uuid4().hex[:12]
    created = q.get("created") if isinstance(q.get("created"), str) else ""
    return {"id": qid, "at": at, "text": text, "created": created}


def _blank_backup():
    return {"status": None, "detail": "", "paths": []}


def _clamp_duration(duration, marks):
    """A duration is never stored smaller than the furthest mark end already
    present, so a shorter duration passed later can't make a load silently
    drop marks (see DURATION_EPSILON for the load-time tolerance)."""
    if not marks:
        return duration
    max_end = max(m["end"] for m in marks)
    if duration is None or duration < max_end:
        return max_end
    return duration


def _unavailable_message(basename):
    """A fixed, plain-words message: never includes OS error text or a full
    path (R3/R10 -- error messages use the file's name, not its path)."""
    return f"Could not save {basename}. Check that the disk has space and the folder can be written to."


def _write_json(path, obj):
    """Atomic write: temp file in the same folder, fsync, then os.replace."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _set_aside(path, tag):
    """Try to rename path to <path>.<tag>-<timestamp>. Returns (ok, basename);
    on failure (path, e.g., can't be renamed) returns (False, None) and the
    original file is left exactly where it was."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    new_path = f"{path}.{tag}-{ts}"
    try:
        os.replace(path, new_path)
    except OSError:
        return False, None
    return True, os.path.basename(new_path)


def _read_json_raw(path):
    """Read and parse one JSON file. Returns (status, data):
    "missing" (no such file), "io_error" (couldn't even open/read it --
    treated as transiently unavailable, never as corrupt), "bad_json"
    (opened fine but isn't valid UTF-8 JSON), or "ok" (data is the parsed value).
    The bytes are decoded here, not by the file object, so invalid UTF-8
    (UnicodeDecodeError) counts as malformed content like any other bad JSON.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "io_error", None
    try:
        return "ok", json.loads(raw.decode("utf-8"))
    except ValueError:                  # includes UnicodeDecodeError and JSONDecodeError
        return "bad_json", None


def _check_common(start, end, cls, note):
    """Type/shape validation shared by add_mark and update_mark. Does not check
    start < end: callers decide what ordering they require."""
    if not _finite_number(start) or not _finite_number(end):
        raise ValueError("Start and end must be numbers.")
    if cls not in CLASSES:
        raise ValueError(f"Class must be one of A, B, C (got {cls!r}).")
    if not isinstance(note, str):
        raise ValueError("Note must be text.")
    if len(note) > MAX_NOTE_LENGTH:
        raise ValueError(f"A note can be at most {MAX_NOTE_LENGTH} characters.")
    if start < 0:
        raise ValueError("Start cannot be negative.")


def _parse_import_mark(item, duration):
    """One imported marker -> a mark dict, or None if it's not usable.

    ``item`` is one of openevp.wavinfo.read_markers()'s dicts: {"start", "end",
    "note"}, where "note" is the raw RIFF label text -- "EVP <cls>: <note>"
    (or bare "EVP <cls>") for marks OpenEVP wrote, or arbitrary text left by
    another tool.
    """
    if not isinstance(item, dict):
        return None
    start, end, label = item.get("start"), item.get("end"), item.get("note", "")
    if not _finite_number(start) or not _finite_number(end):
        return None
    if start < 0 or end < start:
        return None
    if duration is not None and end > duration:
        return None
    if not isinstance(label, str):
        label = ""
    match = _LABEL_RE.match(label)
    cls, note = (match.group(1), match.group(2) or "") if match else ("C", label)
    if len(note) > MAX_NOTE_LENGTH:
        note = note[:MAX_NOTE_LENGTH]
    return {"id": uuid.uuid4().hex[:12], "start": float(start), "end": float(end),
            "cls": cls, "note": note, "created": _now_iso()}


def _clean_mark(m, duration):
    """Validate one mark record loaded from disk; None if it's not usable."""
    if not isinstance(m, dict):
        return None
    mid, start, end, cls = m.get("id"), m.get("start"), m.get("end"), m.get("cls")
    note, created = m.get("note", ""), m.get("created")
    if not isinstance(mid, str) or not mid:
        return None
    if not _finite_number(start) or not _finite_number(end):
        return None
    if cls not in CLASSES:
        return None
    if not isinstance(note, str) or len(note) > MAX_NOTE_LENGTH:
        return None
    if not isinstance(created, str):
        return None
    if start < 0 or end < start:
        return None
    if duration is not None and end > duration + DURATION_EPSILON:
        return None
    return {"id": mid, "start": float(start), "end": float(end), "cls": cls,
            "note": note, "created": created}


def _clean_recording(rec):
    """Validate one recording record loaded from disk.

    Returns (ok, cleaned, dropped_marks). ok is False if the record's own
    shape is unusable (the whole entry is dropped); dropped_marks counts
    individually-invalid marks that were skipped from an otherwise-usable
    record.
    """
    if not isinstance(rec, dict):
        return False, None, 0
    marks_in = rec.get("marks", [])
    if not isinstance(marks_in, list):
        return False, None, 0
    reviewed = rec.get("reviewed", False)
    if not isinstance(reviewed, bool):
        reviewed = False
    name = rec.get("name", "")
    if not isinstance(name, str):
        name = ""
    duration = rec.get("duration")
    if duration is not None and not _finite_number(duration):
        duration = None
    imported = rec.get("imported", False)
    if not isinstance(imported, bool):
        imported = False
    backup = rec.get("backup")
    if not isinstance(backup, dict) or backup.get("status") not in _BACKUP_STATUSES:
        backup = _blank_backup()
    else:
        detail = backup.get("detail", "")
        paths = backup.get("paths", [])        # absent before OpenEVP 0.8: no paths known
        backup = {"status": backup.get("status"), "detail": detail if isinstance(detail, str) else "",
                  "paths": [p for p in paths if isinstance(p, str) and p] if isinstance(paths, list) else []}
    cleaned_marks, dropped = [], 0
    for m in marks_in:
        cm = _clean_mark(m, duration)
        if cm is None:
            dropped += 1
        else:
            cleaned_marks.append(cm)
    cleaned = {"marks": cleaned_marks, "reviewed": reviewed, "name": name,
               "duration": float(duration) if duration is not None else None,
               "imported": imported, "backup": backup}
    return True, cleaned, dropped


# ---- the per-folder lock ----------------------------------------------------------

def _owner_path(lock_path):
    return lock_path + ".owner"


def _exe_name():
    """This program's file name (OpenEVP.exe when installed, python.exe from source)."""
    return os.path.basename(sys.executable or "") or "python"


def _write_owner(lock_path):
    """Record who holds the lock -- process id, program name and the process's
    creation time (so a reused process id is not taken for it) -- for a second
    instance to say who it is."""
    created = _process_created(os.getpid())
    try:
        with open(_owner_path(lock_path), "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n{_exe_name()}\n{'' if created is None else created}\n")
    except OSError:
        pass                            # only words the second instance's message


def _read_owner(lock_path):
    """(pid, program name, creation time or None) from .lock.owner, or None if it
    is missing or unreadable. A file without the creation time (written before it
    was recorded) gives None for it."""
    try:
        with open(_owner_path(lock_path), encoding="utf-8") as f:
            lines = f.read(4096).splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    exe = lines[1].strip() if len(lines) > 1 else ""
    try:
        created = int(lines[2].strip()) if len(lines) > 2 and lines[2].strip() else None
    except ValueError:
        created = None
    return (pid, exe, created) if pid > 0 else None


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                                               ctypes.POINTER(ctypes.c_uint32)]
    k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    return k32


def _process_info(pid):
    """(program file name, creation time or None) of the process running as `pid`,
    or None if no such process is running (or it cannot be looked at)."""
    if _WINDOWS:
        k32 = _kernel32()
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            code = ctypes.c_uint32()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
                return None
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            times = [ctypes.c_uint64() for _ in range(4)]
            ok = k32.GetProcessTimes(h, *(ctypes.byref(t) for t in times))
            return os.path.basename(buf.value), (times[0].value if ok else None)
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        image = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            created = int(f.read().rsplit(")", 1)[1].split()[19])   # starttime, in clock ticks
    except (OSError, ValueError, IndexError):
        created = None
    return image, created


def _process_created(pid):
    info = _process_info(pid)
    return info[1] if info else None


def _open_problem(error):
    reason = getattr(error, "strerror", None) or type(error).__name__
    return (f"OpenEVP couldn't open its data lock file (.lock, {reason}) \u2014 "
            "marks can't be saved right now.")


def _held_problem(lock_path):
    """Why the lock is held: by another OpenEVP (its recorded process is still
    running, under the program name and creation time it recorded), or by
    something else."""
    owner = _read_owner(lock_path)
    info = _process_info(owner[0]) if owner is not None else None
    if info is not None:
        pid, exe, created = owner
        image, actual = info
        # The creation time, when recorded, must match too: a process id can be reused.
        # One recorded but unreadable now is not trusted; only an owner file from before
        # creation times were recorded (none in it) falls back to the name alone.
        same = created is None or (actual is not None and created == actual)
        if image and exe and image.casefold() == exe.casefold() and same:
            return (f"Another OpenEVP (process {pid}) is open; marks can only be changed there. "
                    "If you don't see its window, it may still be closing.")
    return "OpenEVP couldn't lock its data (held by another program), so marks can't be saved right now."


def _take_lock(path):
    """Try once to take the exclusive lock. Returns (file, None) when taken (and
    .lock.owner written), else (None, why: a plain sentence for the user)."""
    try:
        f = open(path, "a+b")
    except OSError as e:
        return None, _open_problem(e)
    try:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:               # msvcrt.locking wants at least one byte to lock
            f.write(b"0")
            f.flush()
    except OSError as e:
        f.close()
        return None, _open_problem(e)
    try:
        f.seek(0)
        if _WINDOWS:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None, _held_problem(path)
    _write_owner(path)
    return f, None


def _acquire_lock(path):
    """Try once to take the exclusive lock. Returns (file_or_None, locked: bool)."""
    f, _problem = _take_lock(path)
    return f, f is not None


def _release_lock(f):
    try:
        os.remove(_owner_path(f.name))      # ours: we hold the lock
    except OSError:
        pass
    try:
        f.seek(0)
        if _WINDOWS:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        f.close()


def _notify(callback):
    try:
        callback()
    except Exception:
        pass


RETRY_JOIN_TIMEOUT = 5.0    # seconds close() waits for the lock-retry thread


# ---- AppData ------------------------------------------------------------------

class AppData:
    """Thread-safe (one process-wide RLock) access to one app-data folder."""

    def __init__(self, folder, lock_wait=0.0, lock_poll=0.25, retry_interval=3.0):
        """lock_wait: how long to keep trying for the lock (every lock_poll seconds)
        before opening read-only; retry_interval: how often watch_lock() tries again."""
        os.makedirs(folder, exist_ok=True)
        self._folder = folder
        self._lock = threading.RLock()
        self._closed = False
        self._problems = []
        self._lock_path = os.path.join(folder, ".lock")
        self._retry_interval = retry_interval
        self._retry_stop = threading.Event()
        self._retry_thread = None
        self._settings_path = os.path.join(folder, "settings.json")
        self._marks_path = os.path.join(folder, "marks.json")
        self._index_path = os.path.join(folder, "index.json")
        # Questions asked while recording live have a file of their own, never marks.json:
        # an older OpenEVP rewrites marks.json with only the fields it knows, so it must
        # never hold them. Older versions do not know this file and never touch it.
        self._questions_path = os.path.join(folder, "questions.json")
        # The lock is taken *before* anything is loaded: only the lock holder
        # (the single writer) may set a bad file aside. A read-only instance
        # that loaded first and renamed the writer's files out from under it
        # would be destructive, so loading must know its role first.
        self._lock_file, self._lock_problem = self._wait_for_lock(lock_wait, lock_poll)
        self.read_only = self._lock_file is None
        self._load_all()

    def _wait_for_lock(self, wait, poll):
        """Keep trying for the lock for `wait` seconds: a previous OpenEVP that is
        still closing (its window gone, its workers finishing) lets go soon."""
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            f, problem = _take_lock(self._lock_path)
            if f is not None or time.monotonic() + poll > deadline:
                return f, problem
            time.sleep(poll)

    def _load_all(self):
        self._settings_blocked = False
        self._marks_blocked = False
        self._index_blocked = False
        self._questions_blocked = False
        self._settings = self._load_settings()
        self._data = self._load_marks()
        self._qdata = self._load_questions()
        self._index = self._load_index()
        self._index_dirty = False

    @property
    def read_only_reason(self):
        """Why this store is read-only (a sentence for the user), or None if it is writable."""
        with self._lock:
            if not self.read_only:
                return None
            return self._lock_problem or "OpenEVP couldn't lock its data, so marks can't be saved right now."

    def problems(self):
        with self._lock:
            return ([self.read_only_reason] if self.read_only else []) + list(self._problems)

    def watch_lock(self, on_writable=None):
        """While read-only, keep trying for the lock in the background (every
        retry_interval seconds). Once it is taken, everything is reloaded from
        disk (the other holder may have changed it; nothing changed here, since
        a read-only store refuses every write), the store becomes writable and
        on_writable() is called on that thread. close() stops and joins it."""
        with self._lock:
            if not self.read_only or self._closed or self._retry_thread is not None:
                return
            self._retry_thread = threading.Thread(target=self._retry_lock, args=(on_writable,),
                                                  name="store-lock-retry", daemon=True)
            self._retry_thread.start()

    def _retry_lock(self, on_writable):
        while not self._retry_stop.wait(self._retry_interval):
            with self._lock:
                if self._closed or not self.read_only:
                    return
                f, problem = _take_lock(self._lock_path)
                if f is None:
                    self._lock_problem = problem        # the holder may have changed
                    continue
                self._lock_file, self._lock_problem = f, None
                self.read_only = False                  # before loading: the writer repairs bad files
                self._problems = []
                self._load_all()
            if on_writable is not None:
                # Told on a thread of its own that close() never waits for: the
                # callback may block (it reaches the window), and shutdown must not.
                threading.Thread(target=_notify, args=(on_writable,), name="store-writable",
                                 daemon=True).start()
            return

    def close(self):
        self._retry_stop.set()
        t = self._retry_thread
        if t is not None and t is not threading.current_thread():
            t.join(RETRY_JOIN_TIMEOUT)          # it only does file work under the lock
        with self._lock:
            if self._closed:
                return
            try:
                self.flush_index()
            except StoreUnavailable:
                pass
            if self._lock_file is not None:
                _release_lock(self._lock_file)
                self._lock_file = None
            self._closed = True

    def _require_writable(self):
        if self._closed:
            raise StoreUnavailable("OpenEVP is closing; the change was not saved.")
        if self.read_only:
            raise StoreReadOnly(self.read_only_reason)

    # ---- loading --------------------------------------------------------------

    def _handle_bad_file(self, path, basename, subject, blocked_attr, tag="corrupt", label=None):
        """A file that was readable but is malformed (bad JSON, wrong shape, or
        a future version). Only the lock holder may rename it aside; a
        read-only instance leaves it untouched and just reports the problem
        (the writing window will repair it)."""
        label = label or "could not be read"
        if self.read_only:
            self._problems.append(f"{basename} {label} (read-only here); it will be repaired "
                                   "the next time the main OpenEVP window loads it.")
            return
        ok, new_name = _set_aside(path, tag)
        if ok:
            self._problems.append(f"{basename} {label}; it was set aside as {new_name} and "
                                   f"{subject} starts fresh.")
        else:
            self._problems.append(f"{basename} {label} and could not be set aside; "
                                   f"{subject} cannot be saved until this is fixed.")
            setattr(self, blocked_attr, True)

    def _handle_unavailable_file(self, basename, subject, blocked_attr):
        """The file itself could not even be opened/read (permissions, or a
        transient sharing violation while the writer is mid-replace). This is
        never treated as corrupt: nothing is renamed, in either role."""
        self._problems.append(f"{basename} could not be read right now; {subject} is "
                               "unavailable until this is fixed.")
        setattr(self, blocked_attr, True)

    def _load_settings(self):
        path = self._settings_path
        status, data = _read_json_raw(path)
        if status == "missing":
            return {}
        if status == "io_error":
            self._handle_unavailable_file("settings.json", "settings", "_settings_blocked")
            return {}
        if status == "bad_json" or not isinstance(data, dict):
            self._handle_bad_file(path, "settings.json", "settings", "_settings_blocked")
            return {}
        return data

    def _load_marks(self):
        path = self._marks_path
        default = {"version": 1, "recordings": {}}
        status, data = _read_json_raw(path)
        if status == "missing":
            return default
        if status == "io_error":
            self._handle_unavailable_file("marks.json", "marking", "_marks_blocked")
            return default
        if status == "bad_json" or not isinstance(data, dict) or not isinstance(data.get("recordings"), dict):
            self._handle_bad_file(path, "marks.json", "marking", "_marks_blocked")
            return default
        if data.get("version") != 1:
            self._handle_bad_file(path, "marks.json", "marking", "_marks_blocked", tag="future",
                                   label="is from a newer version of OpenEVP")
            return default
        cleaned, dropped = {}, 0
        for fp, rec in data["recordings"].items():
            if not isinstance(fp, str):
                dropped += 1
                continue
            ok, cleaned_rec, n_dropped_marks = _clean_recording(rec)
            dropped += n_dropped_marks
            if not ok:
                dropped += 1
                continue
            cleaned[fp] = cleaned_rec
        if dropped:
            self._problems.append(f"{dropped} invalid record(s) in marks.json were skipped.")
        return {"version": 1, "recordings": cleaned}

    def _load_questions(self):
        path = self._questions_path
        default = {"version": 1, "recordings": {}}
        status, data = _read_json_raw(path)
        if status == "missing":
            return default
        if status == "io_error":
            self._handle_unavailable_file("questions.json", "the question log", "_questions_blocked")
            return default
        if status == "bad_json" or not isinstance(data, dict) or not isinstance(data.get("recordings"), dict):
            self._handle_bad_file(path, "questions.json", "the question log", "_questions_blocked")
            return default
        if data.get("version") != 1:
            self._handle_bad_file(path, "questions.json", "the question log", "_questions_blocked", tag="future",
                                   label="is from a newer version of OpenEVP")
            return default
        cleaned, dropped = {}, 0
        for fp, rec in data["recordings"].items():
            if not isinstance(fp, str) or not isinstance(rec, dict) or not isinstance(rec.get("questions"), list):
                dropped += 1
                continue
            duration = rec.get("duration")
            duration = float(duration) if _finite_number(duration) else None
            qs = []
            for x in rec["questions"]:
                q = _clean_question(x, duration)
                if q is None:
                    dropped += 1
                else:
                    qs.append(q)
            name = rec.get("name") if isinstance(rec.get("name"), str) else ""
            cleaned[fp] = {"name": name, "duration": duration, "questions": qs}
        if dropped:
            self._problems.append(f"{dropped} invalid record(s) in questions.json were skipped.")
        return {"version": 1, "recordings": cleaned}

    def _load_index(self):
        path = self._index_path
        default = {"files": {}}
        status, data = _read_json_raw(path)
        if status == "missing":
            return default
        if status == "io_error":
            self._handle_unavailable_file("index.json", "the fingerprint cache", "_index_blocked")
            return default
        if status == "bad_json" or not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            self._handle_bad_file(path, "index.json", "the fingerprint cache", "_index_blocked")
            return default
        files = {}
        for k, v in data["files"].items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            size, mtime_ns = v.get("size"), v.get("mtime_ns")
            if not isinstance(size, int) or isinstance(size, bool):
                continue
            if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool):
                continue
            error = v.get("error")
            if error is not None:
                if not isinstance(error, str):
                    continue
                files[k] = {"size": size, "mtime_ns": mtime_ns, "fp": None, "seconds": None, "error": error}
                continue
            fp, seconds = v.get("fp"), v.get("seconds")
            if not isinstance(fp, str):
                continue
            if seconds is not None and not _finite_number(seconds):
                continue
            files[k] = {"size": size, "mtime_ns": mtime_ns, "fp": fp, "seconds": seconds}
        return {"files": files}

    # ---- settings ---------------------------------------------------------------

    def get_setting(self, name, default=None):
        with self._lock:
            return self._settings.get(name, default)

    def set_setting(self, name, value):
        with self._lock:
            self._require_writable()
            if self._settings_blocked:
                raise StoreUnavailable(_unavailable_message("settings.json"))
            new_settings = dict(self._settings)
            new_settings[name] = value
            try:
                _write_json(self._settings_path, new_settings)
            except OSError as e:
                raise StoreUnavailable(_unavailable_message("settings.json")) from e
            self._settings = new_settings

    # ---- marks --------------------------------------------------------------

    def _save_marks(self, new_data):
        if self._marks_blocked:
            raise StoreUnavailable(_unavailable_message("marks.json"))
        try:
            _write_json(self._marks_path, new_data)
        except OSError as e:
            raise StoreUnavailable(_unavailable_message("marks.json")) from e
        self._data = new_data

    def marks(self, fp):
        with self._lock:
            rec = self._data["recordings"].get(fp)
            if not rec:
                return []
            return sorted((dict(m) for m in rec["marks"]), key=lambda m: m["start"])

    def recording(self, fp):
        with self._lock:
            rec = self._data["recordings"].get(fp)
            if not rec:
                return None
            return {
                "name": rec["name"],
                "duration": rec["duration"],
                "reviewed": rec["reviewed"],
                "imported": rec["imported"],
                "backup": {"status": rec["backup"]["status"], "detail": rec["backup"]["detail"]},
                "marks": sorted((dict(m) for m in rec["marks"]), key=lambda m: m["start"]),
            }

    # ---- questions (questions.json) ----------------------------------------------

    def questions(self, fp):
        """The questions asked while this recording was made (Live mode's question log).
        Kept by fingerprint like marks, so a recording deleted and restored has them again."""
        with self._lock:
            rec = self._qdata["recordings"].get(fp)
            return sorted((dict(q) for q in rec["questions"]), key=lambda q: q["at"]) if rec else []

    def add_question(self, fp, at, text, name="", duration=None):
        """Store a question asked at `at` seconds into the recording. The same question
        (time and text) already there is not added again, so this can be run twice."""
        with self._lock:
            self._require_writable()
            q = _clean_question({"at": at, "text": text, "created": _now_iso()}, duration)
            if q is None:
                raise ValueError("A question needs its text and a time inside the recording.")
            existing = self._qdata["recordings"].get(fp)
            if existing and any(abs(x["at"] - q["at"]) < 1e-6 and x["text"] == q["text"]
                                for x in existing["questions"]):
                return None
            if self._questions_blocked:
                raise StoreUnavailable(_unavailable_message("questions.json"))
            new_data = copy.deepcopy(self._qdata)
            rec = new_data["recordings"].setdefault(fp, {"name": "", "duration": None, "questions": []})
            rec["questions"].append(q)
            if name and not rec["name"]:
                rec["name"] = name
            if duration is not None and (rec["duration"] is None or rec["duration"] < duration):
                rec["duration"] = float(duration)
            try:
                _write_json(self._questions_path, new_data)
            except OSError as e:
                raise StoreUnavailable(_unavailable_message("questions.json")) from e
            self._qdata = new_data
            return dict(q)

    def add_mark(self, fp, start, end, cls, note, name="", duration=None):
        with self._lock:
            self._require_writable()
            _check_common(start, end, cls, note)
            if end - start < MIN_MARK_LENGTH:
                raise ValueError(f"A mark must be at least {MIN_MARK_LENGTH} seconds long.")
            existing = self._data["recordings"].get(fp)
            effective_duration = duration if duration is not None else (
                existing["duration"] if existing else None)
            if effective_duration is not None:
                if not _finite_number(effective_duration) or effective_duration < 0:
                    raise ValueError("Duration must be a non-negative number.")
                if end > effective_duration:
                    raise ValueError("A mark cannot extend past the end of the recording.")
            mark = {"id": uuid.uuid4().hex[:12], "start": float(start), "end": float(end),
                    "cls": cls, "note": note, "created": _now_iso()}
            new_data = copy.deepcopy(self._data)
            rec = new_data["recordings"].setdefault(fp, _blank_recording())
            rec["marks"].append(mark)
            # A recording marked in the app never auto-imports markers later:
            # the WAVs OpenEVP writes carry these marks, and re-importing them
            # would bring back marks the user has since deleted.
            rec["imported"] = True
            if name:
                rec["name"] = name
            if duration is not None:
                rec["duration"] = _clamp_duration(float(duration), rec["marks"])
            self._save_marks(new_data)
            return dict(mark)

    def update_mark(self, fp, mark_id, cls=None, note=None, start=None, end=None):
        with self._lock:
            self._require_writable()
            rec = self._data["recordings"].get(fp)
            old = next((m for m in rec["marks"] if m["id"] == mark_id), None) if rec else None
            if old is None:
                raise ValueError("No such mark.")
            new_cls = old["cls"] if cls is None else cls
            new_note = old["note"] if note is None else note
            new_start = old["start"] if start is None else start
            new_end = old["end"] if end is None else end
            _check_common(new_start, new_end, new_cls, new_note)
            if start is not None or end is not None:
                if new_end - new_start < MIN_MARK_LENGTH:
                    raise ValueError(f"A mark must be at least {MIN_MARK_LENGTH} seconds long.")
            elif new_end < new_start:
                raise ValueError("Start must not be after end.")
            duration = rec["duration"]
            if duration is not None and new_end > duration:
                raise ValueError("A mark cannot extend past the end of the recording.")
            new_data = copy.deepcopy(self._data)
            new_rec = new_data["recordings"][fp]
            updated = None
            for m in new_rec["marks"]:
                if m["id"] == mark_id:
                    m["cls"], m["note"] = new_cls, new_note
                    m["start"], m["end"] = float(new_start), float(new_end)
                    updated = m
                    break
            self._save_marks(new_data)
            return dict(updated)

    def delete_mark(self, fp, mark_id):
        with self._lock:
            self._require_writable()
            rec = self._data["recordings"].get(fp)
            if not rec or not any(m["id"] == mark_id for m in rec["marks"]):
                return False
            new_data = copy.deepcopy(self._data)
            new_rec = new_data["recordings"][fp]
            new_rec["marks"] = [m for m in new_rec["marks"] if m["id"] != mark_id]
            self._save_marks(new_data)
            return True

    def set_reviewed(self, fp, reviewed):
        with self._lock:
            self._require_writable()
            if not isinstance(reviewed, bool):
                raise ValueError("reviewed must be a boolean.")
            new_data = copy.deepcopy(self._data)
            rec = new_data["recordings"].setdefault(fp, _blank_recording())
            rec["reviewed"] = reviewed
            self._save_marks(new_data)

    def is_reviewed(self, fp):
        with self._lock:
            rec = self._data["recordings"].get(fp)
            return bool(rec["reviewed"]) if rec else False

    def import_marks(self, fp, marks, name, duration):
        """Import embedded markers, once per fp (see ``imported``/R3).

        Also refuses -- without ever raising -- when the recording already
        has marks, even if ``imported`` wasn't set yet: this covers a
        recorder recording whose backup WAV embeds the very marks the user
        just made, so re-reading that WAV (via indexing or open_wav) must not
        duplicate them. Either way, imported is set so it is never retried.
        """
        with self._lock:
            self._require_writable()
            existing = self._data["recordings"].get(fp)
            if existing and (existing["imported"] or existing["marks"]):
                if not existing["imported"]:
                    new_data = copy.deepcopy(self._data)
                    new_data["recordings"][fp]["imported"] = True
                    self._save_marks(new_data)
                return 0
            new_data = copy.deepcopy(self._data)
            rec = new_data["recordings"].setdefault(fp, _blank_recording())
            added = 0
            for item in marks or []:
                parsed = _parse_import_mark(item, duration)
                if parsed is None:
                    continue
                rec["marks"].append(parsed)
                added += 1
            if name:
                rec["name"] = name
            if duration is not None:
                rec["duration"] = _clamp_duration(float(duration), rec["marks"])
            rec["imported"] = True
            self._save_marks(new_data)
            return added

    def is_imported(self, fp):
        with self._lock:
            rec = self._data["recordings"].get(fp)
            return bool(rec["imported"]) if rec else False

    def set_imported(self, fp):
        with self._lock:
            self._require_writable()
            new_data = copy.deepcopy(self._data)
            rec = new_data["recordings"].setdefault(fp, _blank_recording())
            rec["imported"] = True
            self._save_marks(new_data)

    def backup(self, fp):
        """{"status", "detail"} of a recording's backup (its file paths: backup_record())."""
        with self._lock:
            rec = self._data["recordings"].get(fp)
            return {"status": rec["backup"]["status"], "detail": rec["backup"]["detail"]} if rec                 else {"status": None, "detail": ""}

    def backup_record(self, fp):
        """The whole backup record: {"status", "detail", "paths"} (the saved files)."""
        with self._lock:
            rec = self._data["recordings"].get(fp)
            b = rec["backup"] if rec else _blank_backup()
            return {"status": b["status"], "detail": b["detail"], "paths": list(b["paths"])}

    def saved_backups(self):
        """{fp: [paths of its saved backup files]} for every backup recorded as saved
        (an empty list for one saved before the paths were recorded)."""
        with self._lock:
            return {fp: list(rec["backup"]["paths"]) for fp, rec in self._data["recordings"].items()
                    if rec["backup"]["status"] == "saved"}

    @staticmethod
    def _checked_backup(status, detail, paths):
        if status not in _BACKUP_STATUSES:
            raise ValueError(f"Unknown backup status {status!r}.")
        if not isinstance(detail, str):
            raise ValueError("detail must be text.")
        paths = list(paths or ())
        if not all(isinstance(p, str) and p for p in paths):
            raise ValueError("paths must be text.")
        return {"status": status, "detail": detail, "paths": [os.path.abspath(p) for p in paths]}

    def set_backup(self, fp, status, detail="", paths=()):
        """Record a backup status; paths are the files a saved backup wrote (the .dvf
        and its WAV copy), so that deleting them can be noticed."""
        self.set_backups({fp: {"status": status, "detail": detail, "paths": paths}})

    def set_backups(self, records):
        """Several backup records ({fp: {"status", "detail", "paths"}}) in one write:
        all of them are saved, or none."""
        with self._lock:
            self._require_writable()
            checked = {fp: self._checked_backup(r.get("status"), r.get("detail", ""), r.get("paths"))
                       for fp, r in records.items()}
            if not checked:
                return
            new_data = copy.deepcopy(self._data)
            for fp, backup in checked.items():
                new_data["recordings"].setdefault(fp, _blank_recording())["backup"] = backup
            self._save_marks(new_data)

    def move_backup_paths(self, old, new):
        """A file or folder moved from old to new: backup paths at or under old now
        name the same place under new. Returns {fp: its paths before} for exactly
        the records changed (for restore_backup_paths); {} when none was."""
        with self._lock:
            self._require_writable()
            old_key, new_abs = _index_key(old), os.path.abspath(new)
            under = old_key.rstrip(os.sep) + os.sep
            new_data, changed = None, {}
            for fp, rec in self._data["recordings"].items():
                paths = rec["backup"]["paths"]
                if not any(_index_key(p) == old_key or _index_key(p).startswith(under) for p in paths):
                    continue
                if new_data is None:
                    new_data = copy.deepcopy(self._data)
                moved = []
                for p in paths:
                    k = _index_key(p)
                    if k == old_key or k.startswith(under):
                        p = new_abs + os.path.abspath(p)[len(old_key):]
                    moved.append(p)
                new_data["recordings"][fp]["backup"]["paths"] = moved
                changed[fp] = list(paths)
            if new_data is not None:
                self._save_marks(new_data)
            return changed

    def restore_backup_paths(self, before):
        """Put back the paths of exactly the records move_backup_paths() changed
        ({fp: paths before}); no other record is touched."""
        with self._lock:
            self._require_writable()
            new_data = copy.deepcopy(self._data)
            for fp, paths in before.items():
                rec = new_data["recordings"].get(fp)
                if rec is not None:
                    rec["backup"]["paths"] = list(paths)
            self._save_marks(new_data)

    def summary(self):
        with self._lock:
            result = {}
            for fp, rec in self._data["recordings"].items():
                counts = {"A": 0, "B": 0, "C": 0}
                notes = []
                for m in rec["marks"]:
                    counts[m["cls"]] += 1
                    if m["note"]:
                        notes.append(m["note"].lower())
                result[fp] = {**counts, "reviewed": rec["reviewed"], "notes": "\n".join(notes)}
            return result

    # ---- fingerprint index ----------------------------------------------------

    def cached_fp(self, path, size, mtime_ns):
        with self._lock:
            entry = self._index["files"].get(_index_key(path))
            if entry and entry["size"] == size and entry["mtime_ns"] == mtime_ns:
                return dict(entry)
            return None

    def remember_fp(self, path, size, mtime_ns, fp, seconds, error=None):
        """Cache one file's fingerprint result, or (if `error` is given, with
        `fp` typically None) cache that fingerprinting it failed -- so the
        library indexer doesn't retry a known-bad file every scan. cached_fp
        returns whatever was stored; callers check for an "error" key."""
        with self._lock:
            self._require_writable()
            entry = {"size": size, "mtime_ns": mtime_ns, "fp": fp, "seconds": seconds}
            if error is not None:
                entry["error"] = error
            self._index["files"][_index_key(path)] = entry
            self._index_dirty = True

    def flush_index(self):
        with self._lock:
            if not self._index_dirty:
                return
            self._require_writable()
            if self._index_blocked:
                raise StoreUnavailable(_unavailable_message("index.json"))
            try:
                _write_json(self._index_path, self._index)
            except OSError as e:
                raise StoreUnavailable(_unavailable_message("index.json")) from e
            self._index_dirty = False

    def prune_index(self, folder, seen_keys):
        """Drop cached entries under `folder` that were not in the latest scan.
        `seen_keys` may be raw paths -- they're normalized the same way keys
        stored by remember_fp are, so callers don't have to know the key
        format."""
        with self._lock:
            self._require_writable()
            prefix = os.path.normcase(os.path.abspath(folder))
            under = prefix.rstrip(os.sep) + os.sep      # a drive root already ends in one
            seen = {_index_key(k) for k in seen_keys}
            to_delete = [k for k in self._index["files"]
                         if (k == prefix or k.startswith(under)) and k not in seen]
            for k in to_delete:
                del self._index["files"][k]
            if to_delete:
                self._index_dirty = True

    def forget_index(self, paths):
        """Drop the cached entries of these files (deleted). Returns how many went."""
        with self._lock:
            self._require_writable()
            files = self._index["files"]
            gone = [k for k in {_index_key(p) for p in paths} if k in files]
            for k in gone:
                del files[k]
            if gone:
                self._index_dirty = True
            return len(gone)

    def move_index_prefix(self, old, new):
        """A file or folder moved from `old` to `new`: re-key every cached entry
        for `old` itself or anything under it to the same place under `new`
        (same size and mtime, so nothing is fingerprinted again). Returns how
        many entries moved. An entry already cached at the new place is replaced."""
        with self._lock:
            self._require_writable()
            old_key, new_key = _index_key(old), _index_key(new)
            under = old_key.rstrip(os.sep) + os.sep
            files = self._index["files"]
            moving = [k for k in files if k == old_key or k.startswith(under)]
            entries = [(k, files.pop(k)) for k in moving]
            for k, entry in entries:
                files[new_key + k[len(old_key):]] = entry
            if entries:
                self._index_dirty = True
            return len(entries)

    def index_under(self, folder):
        """{key: entry} of every cached entry for `folder` itself or anything under it."""
        with self._lock:
            key = _index_key(folder)
            under = key.rstrip(os.sep) + os.sep
            return {k: dict(v) for k, v in self._index["files"].items() if k == key or k.startswith(under)}
