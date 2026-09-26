"""Persistent app data: EVP marks, settings and the fingerprint index.

Everything lives under one folder (``%APPDATA%\\OpenEVP`` in the app) in three
JSON files: ``settings.json``, ``marks.json`` and ``index.json``. Writes are
atomic (temp file + ``os.replace``) and a corrupt or unreadable file is set
aside rather than overwritten, so a bad write or an interrupted process never
loses the rest of the store.

Only one ``AppData`` may write to a folder at a time: the constructor takes an
exclusive lock on ``<folder>/.lock`` for the life of the object. A second
instance on the same folder (another window, or another process) opens
read-only: reads still work, writes raise ``StoreReadOnly``.
"""
import copy
import datetime
import json
import math
import os
import re
import sys
import tempfile
import threading
import uuid

_WINDOWS = sys.platform.startswith("win")
if _WINDOWS:
    import msvcrt
else:
    import fcntl

CLASSES = ("A", "B", "C")
MIN_MARK_LENGTH = 0.05      # seconds; new marks made in the UI must be at least this long
MAX_NOTE_LENGTH = 500
_BACKUP_STATUSES = ("saved", "failed", None)
_LABEL_RE = re.compile(r"^EVP ([ABC])(?::\s?(.*))?$")   # "EVP A: note" or bare "EVP A"


class StoreReadOnly(Exception):
    """A write was attempted on a read-only store (another window holds the lock)."""


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
            "imported": False, "backup": {"status": None, "detail": ""}}


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
    """Rename path to <path>.<tag>-<timestamp> (best effort). Returns the new basename."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    new_path = f"{path}.{tag}-{ts}"
    try:
        os.replace(path, new_path)
    except OSError:
        pass
    return os.path.basename(new_path)


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

    ``item`` is one of st25.wavinfo.read_markers()'s dicts: {"start", "end",
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
    if duration is not None and end > duration:
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
        backup = {"status": None, "detail": ""}
    else:
        detail = backup.get("detail", "")
        backup = {"status": backup.get("status"), "detail": detail if isinstance(detail, str) else ""}
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

def _acquire_lock(path):
    """Try to take the exclusive lock. Returns (file_or_None, locked: bool)."""
    try:
        f = open(path, "a+b")
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:               # msvcrt.locking wants at least one byte to lock
            f.write(b"0")
            f.flush()
    except OSError:
        return None, False
    try:
        f.seek(0)
        if _WINDOWS:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None, False
    return f, True


def _release_lock(f):
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


# ---- AppData ------------------------------------------------------------------

class AppData:
    """Thread-safe (one process-wide RLock) access to one app-data folder."""

    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self._folder = folder
        self._lock = threading.RLock()
        self._problems = []
        self._settings_path = os.path.join(folder, "settings.json")
        self._marks_path = os.path.join(folder, "marks.json")
        self._index_path = os.path.join(folder, "index.json")
        self._settings = self._load_settings()
        self._data = self._load_marks()
        self._index = self._load_index()
        self._index_dirty = False
        self._lock_file, locked = _acquire_lock(os.path.join(folder, ".lock"))
        self.read_only = not locked
        self._closed = False

    def problems(self):
        return list(self._problems)

    def close(self):
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
        if self.read_only:
            raise StoreReadOnly("Another OpenEVP window is open; marks can only be changed there.")

    # ---- loading --------------------------------------------------------------

    def _load_settings(self):
        path = self._settings_path
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            name = _set_aside(path, "corrupt")
            self._problems.append(f"settings.json could not be read; it was set aside as {name} "
                                   "and settings were reset.")
            return {}
        return data

    def _load_marks(self):
        path = self._marks_path
        default = {"version": 1, "recordings": {}}
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("recordings"), dict):
            name = _set_aside(path, "corrupt")
            self._problems.append(f"marks.json could not be read; it was set aside as {name} "
                                   "and marking starts fresh.")
            return default
        if data.get("version") != 1:
            name = _set_aside(path, "future")
            self._problems.append(f"marks.json is from a newer version of OpenEVP; it was set "
                                   f"aside as {name} and marking starts fresh.")
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

    def _load_index(self):
        path = self._index_path
        default = {"files": {}}
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            name = _set_aside(path, "corrupt")
            self._problems.append(f"index.json could not be read; it was set aside as {name} "
                                   "and the fingerprint cache starts fresh.")
            return default
        files = {}
        for k, v in data["files"].items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            size, mtime_ns, fp, seconds = v.get("size"), v.get("mtime_ns"), v.get("fp"), v.get("seconds")
            if not isinstance(size, int) or isinstance(size, bool):
                continue
            if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool):
                continue
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
            new_settings = dict(self._settings)
            new_settings[name] = value
            try:
                _write_json(self._settings_path, new_settings)
            except OSError as e:
                raise StoreUnavailable(f"Could not save settings: {e}") from e
            self._settings = new_settings

    # ---- marks --------------------------------------------------------------

    def _save_marks(self, new_data):
        try:
            _write_json(self._marks_path, new_data)
        except OSError as e:
            raise StoreUnavailable(f"Could not save marks: {e}") from e
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
                "backup": dict(rec["backup"]),
                "marks": sorted((dict(m) for m in rec["marks"]), key=lambda m: m["start"]),
            }

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
            if name:
                rec["name"] = name
            if duration is not None:
                rec["duration"] = float(duration)
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
        with self._lock:
            self._require_writable()
            if self.is_imported(fp):
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
                rec["duration"] = float(duration)
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
        with self._lock:
            rec = self._data["recordings"].get(fp)
            return dict(rec["backup"]) if rec else {"status": None, "detail": ""}

    def set_backup(self, fp, status, detail=""):
        with self._lock:
            self._require_writable()
            if status not in _BACKUP_STATUSES:
                raise ValueError(f"Unknown backup status {status!r}.")
            if not isinstance(detail, str):
                raise ValueError("detail must be text.")
            new_data = copy.deepcopy(self._data)
            rec = new_data["recordings"].setdefault(fp, _blank_recording())
            rec["backup"] = {"status": status, "detail": detail}
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

    def remember_fp(self, path, size, mtime_ns, fp, seconds):
        with self._lock:
            self._require_writable()
            self._index["files"][_index_key(path)] = {
                "size": size, "mtime_ns": mtime_ns, "fp": fp, "seconds": seconds}
            self._index_dirty = True

    def flush_index(self):
        with self._lock:
            if not self._index_dirty:
                return
            self._require_writable()
            try:
                _write_json(self._index_path, self._index)
            except OSError as e:
                raise StoreUnavailable(f"Could not save the fingerprint index: {e}") from e
            self._index_dirty = False

    def prune_index(self, folder, seen_keys):
        """Drop cached entries under `folder` that were not in the latest scan."""
        with self._lock:
            self._require_writable()
            prefix = os.path.normcase(os.path.abspath(folder))
            seen = set(seen_keys)
            to_delete = [k for k in self._index["files"]
                         if (k == prefix or k.startswith(prefix + os.sep)) and k not in seen]
            for k in to_delete:
                del self._index["files"][k]
            if to_delete:
                self._index_dirty = True
