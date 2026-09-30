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
itself does reach the page, only to group copies of one recording). For a
recording on a recorder the handle also holds its provenance: the exact native
bytes that were decoded for playback (a .dvf for the ST25), their file name,
their format and where they came from (recorder, folder, number), so the
backup made when the recording is first marked saves exactly the audio that
was marked. Backups run
on one background worker (registered with the export worker, so shutdown()
waits for both) and report through "backup-done" / "backup-failed" events.

Recorders are reached only through their model (openevp.recorders): the
listing, downloads and the native format (openevp.formats) with its decoder
come from it. Folder ids and recording numbers from the page are checked
against the recorder's own listing; exports and backups go into
<Save to>/<folder safe_name>/<the download's file name>.

The EVP library lists the recording files (the extensions of openevp.formats)
of one folder tree by opaque ids.
Fingerprints come from the store's index cache; files not in it are
fingerprinted by one background indexer (also a registered worker), which
reports "library-row" / "library-progress" / "library-done" events tagged with
the scan_id of the list_library() call that started it.

Library folders can be created, renamed and deleted (to the Recycle Bin only),
and recordings moved between them: see app/library_ops.py (mixed into Api) and
app/folders.py.
"""
import datetime
import os
import secrets
import threading
import wave
from collections import OrderedDict

from openevp import __version__, formats, recorders, wavinfo
from openevp.export import save_unique, save_wav
from openevp.paths import open_folder
from openevp.recorders import base as rbase

from .updater import UpdateCancelled

from . import folders
from .devices import NEEDS_DRIVER, NEEDS_REPLUG, READY, DeviceGone
from .library_ops import (CLOSING, HANDLES, SESSION_CACHE, LibraryOps, _bounded_put, _fail,  # noqa: F401
                          _decoder_problem, _decoder_problems, _file_id, _folder_id, _kind_format, _plain)
from .library_ops import (BACKUP_RECYCLED, FS_BUSY, FS_WAIT, INDEXER_BUSY, LIB_CHANGED,  # noqa: F401
                          PATH_TOO_LONG, ROOT_CHANGED, _root_identity)
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
    device_id, folder_id, number = key
    dl = manager.with_session(device_id, lambda s: s.download(folder_id, number))
    if dl.error:
        raise ValueError(f"{dl.label} cannot be played: {dl.error}")
    return dl


def _decoder(fmt):
    """fmt's decoder, or raise formats.DecoderUnavailable saying why there is none."""
    if fmt.decoder is None:
        raise formats.DecoderUnavailable(f"OpenEVP cannot convert {fmt.label} ({fmt.ext}) files to WAV")
    return fmt.decoder


def recording_wav(manager, key, should_stop=None):
    """WAV bytes for one recording (the AudioServer provider). ``should_stop``
    lets app shutdown interrupt a long decode (formats.Cancelled). Raises
    formats.DecoderUnavailable when the recorder's model, or this recording's
    codec (e.g. an ICD-ST10's LPEC SP in a build without its table data), cannot be played (the model is read after
    the download: opening the recorder may relabel it)."""
    data = _download(manager, key).data
    model = manager.model(key[0])
    problem = model.wav_problem() or model.native.data_problem(data)
    if problem:
        raise formats.DecoderUnavailable(problem)
    return _decoder(model.native).to_wav(data, should_stop=should_stop)


def save_native(fmt, data, outdir, name):
    """Save a recorder's native file unless the format says an existing file
    already holds the same recording (a .dvf: the same audio, ignoring the time
    counters DVE rewrites). Never replaces a file. Returns (path, already_saved)."""
    return save_unique(data, outdir, name, lambda existing: fmt.same(existing, data))


def _usable_filename(fmt, name):
    """Whether a download's file name can be written as is (A5)."""
    return rbase.safe_name_ok(name) and name.lower().endswith(fmt.ext) and len(name) > len(fmt.ext)


def _label(folder, number):
    """How messages name a recording: "A-007" (folder safe name, number)."""
    if isinstance(number, int):
        return f"{folder['safe_name']}-{number:03d}"
    return f"{folder['safe_name']}-{number}"


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


def _seconds(path, kind):
    """Length of a saved file from its header only (never reads the audio)."""
    fmt = _kind_format(kind)
    return fmt.seconds(path) if fmt is not None else None


INDEX_FLUSH_EVERY = 20      # the library indexer writes its cache after this many files


def _stat_of(st):
    """(size, mtime_ns) of an os.stat result: one version of a file on disk."""
    return st.st_size, st.st_mtime_ns


def _wav_length(f):
    """Exact length in seconds of a PCM WAV (a path or a file object)."""
    with wave.open(f) as w:
        return w.getnframes() / w.getframerate() if w.getframerate() else None


def _scan_library(folder):
    """([(investigation, name, kind, path, stat, folder_rel)], [folder_rel, ...],
    truncated, complete): every recording file (an extension in openevp.formats:
    .dvf, .wav...) in folder and all its subfolders -- a
    folder's files first (by name), then its subfolders in name order. Walking
    stops as soon as more than SAVED_LIMIT files were found. Symlinked folders
    and junctions are not followed (no loops); names starting with "." (Mac
    "._x.wav" companions, temp files, hidden folders) and folders Windows marks
    hidden or system (AppData, $RECYCLE.BIN...) are skipped. complete is
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
                    if not folders.entry_is_hidden(e):  # AppData, System Volume Information...
                        subdirs.append(e)
                    continue
                kind = os.path.splitext(e.name)[1].lower()[1:]
                if not kind or _kind_format(kind) is None or not e.is_file():
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


def _sentence(text):
    """A reason phrase ("LPEC ST ... can't be played yet") as a sentence."""
    text = str(text).strip() or "WAV conversion is not available"
    return text[0].upper() + text[1:] + ("" if text[-1] in ".!?" else ".")


def _wav_problem(source, data=None):
    """Why a loaded recording (its source, and its native bytes if known)
    cannot be converted to WAV now, or None: its recorder model's reason for a
    recording from a recorder, its format's otherwise, then its own header's."""
    fmt = source["format"]
    model = recorders.get(source.get("model")) if source.get("kind") == "device" else None
    problem = model.wav_problem() if model is not None else _decoder_problem(fmt)
    if problem is None and data is not None:
        problem = fmt.data_problem(data)
    return problem


def _unwrapped(e):
    """The decoder's own exception behind a formats.DecodeError (or another formats
    wrapper), so messages name it as they always have ("FormatError: ...")."""
    while isinstance(e, (formats.DecodeError, formats.DecoderUnavailable, formats.Cancelled))             and e.__cause__ is not None:
        e = e.__cause__
    return e


def _error(e):
    """The page's error for a recorder failure (base.state_for() decides the state)."""
    if isinstance(e, formats.DecoderUnavailable):      # not a failure: a plain sentence, no class name
        return _fail(_sentence(e))
    e = _unwrapped(e)
    if isinstance(e, DeviceGone):
        return _fail(str(e), "", "")
    if isinstance(e, rbase.DriverMissing):
        return _fail(str(e), e.advice or DRIVER, NEEDS_DRIVER)
    if isinstance(e, rbase.RecorderError):             # NotReady, DeviceGone, connection failures
        return _fail(str(e), e.advice or REPLUG, NEEDS_REPLUG)
    return _fail(f"{type(e).__name__}: {e}")


def _problem(model, e):
    """A model whose discovery failed, for the page: a recorder failure (e.g.
    libusb could not be loaded) as the recorder list's error with its advice;
    anything else as a plain note. The other models' recorders are still listed."""
    if isinstance(e, rbase.RecorderError):
        return {k: v for k, v in _error(e).items() if k in ("error", "advice")}
    what = f"{model.name} recorders" if model is not None else "Some recorders"
    return {"error": f"{what} could not be looked for ({type(e).__name__}: {e}).", "advice": ""}


def _item_key(value):
    """A folder id or recording number from the page, in a form that cannot be
    mistaken for another (1 and "1" and True differ), or None if it is neither."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return (type(value).__name__, value)


def _parse_items(items):
    """[(folder id, number)] or None if items is not a non-empty list of
    {"folder", "number"} of the right types (checked against a listing later)."""
    if not isinstance(items, list) or not items:
        return None
    work = []
    for i in items:
        if not isinstance(i, dict):
            return None
        folder_id, number = i.get("folder"), i.get("number")
        if not isinstance(folder_id, str) or _item_key(number) is None:
            return None
        work.append((folder_id, number))
    return work


def _check_listing(model, folders_, rows):
    """Refuse a listing the app could not use safely (A5): folder ids must be
    unique strings, safe names valid and unique ignoring case; numbers unique
    ints or strings per folder. Raises ValueError."""
    ids, safe = set(), set()
    for f in folders_:
        if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not isinstance(f.get("label"), str):
            raise ValueError(f"{model.name} reported a folder OpenEVP cannot use")
        if f["id"] in ids or not rbase.safe_name_ok(f.get("safe_name")) or f["safe_name"].casefold() in safe:
            raise ValueError(f"{model.name} reported a folder OpenEVP cannot use")
        ids.add(f["id"])
        safe.add(f["safe_name"].casefold())
        numbers = set()
        for r in rows[f["id"]]:
            k = _item_key(r.get("number")) if isinstance(r, dict) else None
            if k is None or k in numbers:
                raise ValueError(f"{model.name} reported a recording OpenEVP cannot use")
            numbers.add(k)


def _recording(folder, r):
    """One recording row for the page. play_problem: why this recording cannot be
    played (e.g. its codec has no decoder), or None; the model's own reason is
    the listing's play_reason."""
    seconds = r.get("seconds")
    return {"number": r["number"], "label": _label(folder, r["number"]),
            "recorded": r.get("recorded_label") or "",
            "seconds": round(seconds, 1) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None,
            "owner": r.get("owner"), "problem": r.get("problem") or None,
            "play_problem": r.get("play_problem") or None}


class Api(LibraryOps):
    def __init__(self, manager, emit, pick_folder, default_dest, audio_server, driver_setup=None,
                 pick_wav=None, updater=None, quit_app=None, can_install=False, before_install=None,
                 store=None, store_problems=(), recycle=None):
        self._manager = manager            # private attributes are not exposed to JS
        self._emit = emit
        self._pick = pick_folder             # (start_dir) -> path or None (a folder dialog)
        self._dest = default_dest             # the current save folder (changed with choose_destination)
        self._dest_chosen = False             # the user picked a Save-to folder in this session
        self._dest_lock = threading.Lock()    # _dest_chosen and the _dest it decides, together
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
        self._natives = OrderedDict()         # (device_id, folder id, number) -> (Download, fp of its decode)
        self._listings = {}                   # device_id -> the recorder's listing (see _read_listing)
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
        self._headers = OrderedDict()         # index key -> (size, mtime_ns, Format.file_problem result)
        self._headers_lock = threading.Lock()
        self._recycle = recycle or folders.recycle   # (path) -> None or raises folders.RecycleError
        self._fs_op = False                   # a folder operation is moving files (under _lib_lock)
        self._fs_gen = 0                      # bumped when one starts and ends: an older scan must not prune
        self._fs_busy = False                 # a folder operation holds _busy (not an export)
        self._library_root = None             # _root_identity() of the library folder at the last listing
        if store is not None:
            saved = store.get_setting("save_folder")
            if isinstance(saved, str) and saved and os.path.isdir(saved):
                self._dest = saved

    def capabilities(self):
        """What this app can do. wav / wav_status: whether the recorders' native
        formats can be converted to WAV (played, marked), and why not, or a
        slow-mode warning. formats: per library file type ("dvf", "wav"...),
        whether it can be played and why not. models: the names of the recorder
        models OpenEVP can read (for "plug in a supported recorder"). Store problems (a damaged marks
        file set aside, a second window) are reported here, once, for the page
        to show."""
        store = self._store
        natives = []
        for m in recorders.supported():
            if m.native is not formats.WAV and m.native not in natives:
                natives.append(m.native)
        decoders = [f.decoder for f in natives if f.decoder is not None]
        unavailable = [d for d in decoders if not d.available()]
        if unavailable:
            wav_status = unavailable[0].reason() or "the WAV decoder is not available"
        else:
            wav_status = next((w for w in (d.warning() for d in decoders) if w), None)
        kinds = {}
        for f in formats.all():
            problem = _decoder_problem(f)
            kinds[f.ext[1:]] = {"label": f.label, "playable": problem is None, "reason": problem}
        return {"wav": not unavailable, "wav_status": wav_status, "formats": kinds, "version": __version__,
                "models": [m.name for m in recorders.supported()],
                "marks": store is not None, "marks_read_only": bool(store is not None and store.read_only),
                "marks_read_only_reason": store.read_only_reason if store is not None else None,
                "store_problems": self._store_problems + (store.problems() if store is not None else [])}

    def watch_store(self):
        """While the store is read-only (another OpenEVP holds it, or its lock file
        could not be opened), keep trying for it in the background; once this
        instance can write, the page hears "store-writable" and asks capabilities()
        again. The store's close() (at the end of shutdown()) stops and joins it."""
        if self._store is not None:
            self._store.watch_lock(self._store_writable)

    def _store_writable(self):
        """The store just became writable and reloaded its settings: the remembered
        Save-to folder applies now, unless one was picked in this session (that
        choice could not be saved while read-only, and it stays)."""
        if self._stop.is_set():
            return
        saved = self._store.get_setting("save_folder")
        if isinstance(saved, str) and saved and os.path.isdir(saved):   # no lock held over disk access
            with self._dest_lock:
                if not self._dest_chosen:         # checked here: a pick made meanwhile stays
                    self._dest = saved
        self._emit("store-writable", {})

    def devices(self):
        """{"ok", "devices": [{"id", "model_id", "model", "port", "state", "message",
        "owner"}], "problems": [{"error", "advice"}]}: problems are models whose
        recorders could not be looked for (the other models' recorders are still listed)."""
        try:
            rows, failed = self._manager.refresh(problems=True)
        except Exception as e:
            return _error(e)
        present = {d["id"] for d in rows}
        with self._recs_lock:               # a replug is a new connection id: drop the old one's caches
            for device_id in [d for d in self._listings if d not in present]:
                del self._listings[device_id]
            for key in [k for k in self._natives if k[0] not in present]:
                del self._natives[key]
        return {"ok": True, "devices": rows, "problems": [_problem(m, e) for m, e in failed]}

    def _read_listing(self, device_id):
        """Read a recorder's folders and recordings, check them (A5) and remember
        them for this connection: {"model", "folders": [folder], "rows": {folder id:
        [row]}}. Raises what the manager or the session raised, or ValueError.
        The model is asked for after the read: opening the recorder tells the
        manager what it is (an ICD-ST10 is discovered as an ST25)."""

        def read(s):
            folders_ = s.folders()
            return folders_, {f["id"]: s.recordings(f["id"]) for f in folders_}
        folders_, rows = self._manager.with_session(device_id, read)
        model = self._manager.model(device_id)
        _check_listing(model, folders_, rows)
        listing = {"model": model, "folders": [dict(f) for f in folders_], "rows": rows}
        with self._recs_lock:
            self._listings[device_id] = listing
        return listing

    def _listing(self, device_id):
        """The remembered listing of a connection (read once if there is none)."""
        with self._recs_lock:
            listing = self._listings.get(device_id)
        return listing if listing is not None else self._read_listing(device_id)

    def _find(self, device_id, folder_id, number):
        """(model, folder, row) of a recording the page names, checked against the
        recorder's own listing (never trusted as is); None if there is no such one."""
        if not isinstance(folder_id, str) or _item_key(number) is None:
            return None
        listing = self._listing(device_id)
        folder = next((f for f in listing["folders"] if f["id"] == folder_id), None)
        if folder is None:
            return None
        row = next((r for r in listing["rows"][folder_id] if _item_key(r["number"]) == _item_key(number)), None)
        return None if row is None else (listing["model"], folder, row)

    def _export_formats(self, model):
        """A model's export menu: its native format, then WAV (unavailable, with
        the reason, when its recordings cannot be converted: Model.wav_problem).
        [{"value", "label", "available", "reason"}]"""
        native = model.native
        out = [{"value": native.ext[1:], "label": f"{native.ext} ({native.label})", "available": True,
                "reason": None}]
        if native is not formats.WAV:
            problem = model.wav_problem()
            out.append({"value": "wav", "label": "WAV", "available": problem is None, "reason": problem})
        return out

    def recordings(self, device_id):
        """The recorder's folders and recordings, read afresh: {"ok", "model",
        "model_id", "folders": [{"id", "label", "recordings": [row]}], "formats"
        (the export menu), "playable", "play_reason" (why not, or None)}."""
        try:
            listing = self._read_listing(device_id)
        except Exception as e:
            return _error(e)
        model = listing["model"]
        problem = model.wav_problem()
        return {"ok": True, "model": model.name, "model_id": model.model_id,
                "folders": [{"id": f["id"], "label": f["label"],
                             "recordings": [_recording(f, r) for r in listing["rows"][f["id"]]]}
                            for f in listing["folders"]],
                "formats": self._export_formats(model), "playable": problem is None, "play_reason": problem}

    def audio(self, device_id, folder_id, number):
        # The model comes with the remembered listing: a play served from the decode
        # cache never waits behind an export's download on the device thread.
        try:
            model = self._listing(device_id)["model"]
        except Exception as e:
            return _error(e)
        fmt = model.native
        problem = model.wav_problem()
        if problem:
            return _fail(f"Playback: {problem}.")
        try:
            found = self._find(device_id, folder_id, number)
        except Exception as e:
            return _error(e)
        if found is None:
            return _fail("No such recording.")
        _model, folder, row = found
        if row.get("play_problem"):          # this recording's codec (e.g. LPEC ST without its tables), not the model's
            return _fail(f"Playback: {row['play_problem']}.")
        number = row["number"]
        key = (device_id, folder_id, number)

        made = []

        def write(f):                    # runs only when the audio server has no decode cached
            if not made:                 # (a retry after a full disk reuses the download)
                made.append(_download(self._manager, key))
            dl = made[-1]
            self._reserve(fmt, dl.data)
            formats.write_wav(fmt, dl.data, f, should_stop=self._stop.is_set)
        try:
            info = self._server.prepare(key, write=write)
        except Exception as e:
            return _error(e)
        fp = info.get("fp")
        with self._recs_lock:
            if made:                     # decoded by this call: info describes exactly these bytes
                dl = made[-1]
                _bounded_put(self._natives, key, (dl, fp))
            else:                        # a cached decode: trust a capture only with the same audio
                held = self._natives.get(key)
                dl = held[0] if held is not None and fp is not None and held[1] == fp else None
                if dl is not None:
                    self._natives.move_to_end(key)
        # Without the captured bytes (evicted meanwhile, or not matching) a backup downloads
        # once more and checks the audio is still the same before trusting it (_device_audio).
        # The provenance (A2): the native bytes, their file name and format, and where
        # they came from; kept with the handle, so a backup works after an unplug too.
        label = _label(folder, number)
        source = {"kind": "device", "device": device_id, "model": model.model_id, "format": fmt,
                  "folder": folder_id, "safe_name": folder["safe_name"], "number": number, "label": label,
                  "native": dl.data if dl else None, "native_name": dl.filename if dl else None}
        return self._loaded(info, dl.filename if dl else label, label, source)

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
        """(native bytes, their file name, WAV or None) for a recorder recording's
        handle. When the handle has no captured bytes, the recording is downloaded
        once more from the same recorder, folder and number, and trusted only if it
        decodes to the same audio that was loaded (fingerprinted in place); that
        decode is returned as the WAV, so the caller (a WAV copy, a marked export)
        does not decode it again. Raises ValueError in plain words."""
        src = entry["source"]
        with self._recs_lock:
            if src["native"] is not None:
                return src["native"], src["native_name"], None
        try:
            dl = _download(self._manager, (src["device"], src["folder"], src["number"]))
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(_error(e)["error"]) from e
        if not _usable_filename(src["format"], dl.filename):
            raise ValueError(f"the recorder gave {src['label']} a file name OpenEVP cannot use")
        wav = _decoder(src["format"]).to_wav(dl.data, should_stop=self._stop.is_set)
        with wavinfo.buffer_file(wav) as f:
            same = wavinfo.wav_fingerprint(f) == entry["fp"]
        if not same:
            raise ValueError(f"{src['label']} on the recorder is no longer the recording that was marked. "
                             "Load it again, then retry.")
        with self._recs_lock:
            src["native"], src["native_name"] = dl.data, dl.filename
        return dl.data, dl.filename, wav

    def _backup(self, fp, rec, entry):
        """Save the recording's native file (a .dvf for the ST25) into
        <Save to>/<folder safe name>/, then (with the decoder) a WAV copy with the
        marks. "saved" once the native file is there; a failed WAV copy is
        reported in the detail, not as a failed backup."""
        src = entry["source"]
        label = src["label"]
        fmt = src["format"]
        try:
            data, native_name, wav = self._device_audio(entry)
        except formats.Cancelled:                     # the app is closing; retried on the next mark
            self._closed_before_backup(fp, rec, entry)
            return
        except ValueError as e:
            self._backup_result(fp, rec, entry, "failed", f"{label} was not backed up: {e}")
            return
        if not _usable_filename(fmt, native_name):
            self._backup_result(fp, rec, entry, "failed",
                                f"{label} was not backed up: the recorder gave it a file name OpenEVP cannot use.")
            return
        outdir = os.path.join(self._dest, src["safe_name"])
        try:
            os.makedirs(outdir, exist_ok=True)
            path, already = save_native(fmt, data, outdir, native_name)
        except OSError as e:
            self._backup_result(fp, rec, entry, "failed", f"{label} was not backed up: {_plain(e)}")
            return
        name = os.path.basename(path)
        detail = f"{name} was already saved" if already else f"Saved as {name}"
        paths = [path]                                # remembered: deleting them undoes the backup
        if _wav_problem(src, data) is None:
            wav_name = os.path.splitext(native_name)[0] + ".wav"
            try:
                if wav is None:
                    wav = _decoder(fmt).to_wav(data, should_stop=self._stop.is_set)
                wav_path, _ = save_wav(wavinfo.marked_parts(wav, self._store.marks(fp)), outdir, wav_name)
                del wav
                paths.append(wav_path)
                detail += f", with a WAV copy ({os.path.basename(wav_path)})"
            except formats.Cancelled:
                detail += ", but the WAV copy was not made because the app is closing"
            except Exception as e:
                detail += f", but the WAV copy failed: {_plain(e)}"
        self._backup_result(fp, rec, entry, "saved", detail + ".", paths)

    def _record_backup(self, fp, status, detail, paths=()):
        """Record a backup status (with the files a saved backup wrote); returns
        detail, extended if it could not be recorded."""
        try:
            self._store.set_backup(fp, status, detail, paths)
        except (StoreReadOnly, StoreUnavailable, ValueError) as e:
            detail += f" (This could not be recorded: {e})"
        return detail

    def _backup_result(self, fp, rec, entry, status, detail, paths=()):
        detail = self._record_backup(fp, status, detail, paths)
        self._emit("backup-done" if status == "saved" else "backup-failed",
                   {"rec": rec, "label": entry["source"]["label"], "detail": detail})

    # ---- WAV with marks ----------------------------------------------------------
    def _reserve(self, fmt, data):
        """Before a decode into the audio server's cache: make room for the WAV it
        is expected to write (older decodes are evicted first)."""
        reserve = getattr(self._server, "reserve", None)
        if reserve is not None:
            reserve(formats.expected_wav_bytes(fmt, data))

    def _marked_wav(self, wav):
        """(wav, note): a freshly decoded WAV with the marks of its recording written
        in (as wavinfo.marked_parts: pieces, not a copy), or unchanged when it has
        none. note says why marks were left out."""
        if self._store is None:
            return wav, None
        try:
            with wavinfo.buffer_file(wav) as f:
                marks = self._store.marks(wavinfo.wav_fingerprint(f))
        except ValueError:
            return wav, None
        if not marks:
            return wav, None
        try:
            return wavinfo.marked_parts(wav, marks), None
        except ValueError as e:
            return wav, f"saved without its marks ({e})"

    def export_marked(self, rec):
        """Save a WAV with the current marks of the loaded recording into the Save-to
        folder: <folder safe name>/ (the ST25's A..E) for a recorder recording; for a file in the library, the
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
        read_only = self._second_window()
        if read_only:
            return _fail(read_only)
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
                problem = _wav_problem(src)
                if problem:
                    return _fail(f"WAV export: {problem}.")
                try:
                    data, native_name, wav = self._device_audio(entry)
                except ValueError as e:
                    return _fail(str(e))
                problem = _wav_problem(src, data)
                if problem:
                    return _fail(f"WAV export: {problem}.")
                if not _usable_filename(src["format"], native_name):
                    return _fail(f"The recorder gave {src['label']} a file name OpenEVP cannot use.")
                if wav is None:
                    wav = _decoder(src["format"]).to_wav(data, should_stop=self._stop.is_set)
                outdir = os.path.join(self._dest, src["safe_name"])
                out_name = os.path.splitext(native_name)[0] + ".wav"
            else:
                path = src["path"]
                name = os.path.basename(path)
                with open(path, "rb") as f:
                    raw = f.read()
                fmt = formats.by_ext(os.path.splitext(name)[1])
                if fmt is not None and fmt is not formats.WAV:
                    problem = _decoder_problem(fmt) or fmt.data_problem(raw)
                    if problem:
                        return _fail(f"WAV export: {problem}.")
                    wav = _decoder(fmt).to_wav(raw, should_stop=self._stop.is_set)
                else:
                    wav = raw
                del raw
                try:
                    with wavinfo.buffer_file(wav) as f:
                        same = wavinfo.wav_fingerprint(f) == entry["fp"]
                except ValueError:
                    same = False
                if not same:
                    return _fail(f"{name} has changed since it was loaded. Load it again.")
                investigation = _investigation(path, self._library_path())
                outdir = os.path.join(self._dest, investigation) if investigation else self._dest
                out_name = os.path.splitext(name)[0] + ".wav"
            marked = wavinfo.marked_parts(wav, marks)
        except formats.Cancelled:
            return _fail(CLOSING)
        except OSError as e:
            return _fail(f"Could not read {entry['name']}: {_plain(e)}")
        except formats.DecodeError as e:                 # as the decoder's own error was before
            cause = _unwrapped(e)
            if isinstance(cause, ValueError):
                return _fail(f"Could not add the marks to {entry['name']}: {cause}")
            return _error(cause)
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
        imported once) or another recording file, e.g. a .dvf (decoded through the
        audio server by its format's decoder), on disk."""
        if not path or not os.path.isfile(path):
            return _fail("That file is no longer there. Refresh the list.")
        name = os.path.basename(path)
        source = {"kind": "file", "path": path}
        try:
            fmt = formats.by_ext(os.path.splitext(name)[1])
            if fmt is None or fmt is formats.WAV:
                info = self._server.prepare_file(path)
                source["stat"] = info.pop("stat", None)
                imported = self._import_markers(path, info, name, source["stat"])
                return self._loaded(info, name, name, source, {"name": name, "imported": imported})
            problem = _decoder_problem(fmt)
            if problem:
                return _fail(f"Playing {fmt.ext} files: {problem}.")
            problem = fmt.file_problem(path)          # this file (e.g. an ICD-ST10 recording)
            if problem:
                return _fail(f"{name}: {_sentence(problem)}")
            st = os.stat(path)
            key = (fmt.ext[1:], os.path.normcase(path), st.st_size, st.st_mtime_ns)

            def write(out):
                with open(path, "rb") as f:
                    data = f.read()
                self._reserve(fmt, data)
                formats.write_wav(fmt, data, out, should_stop=self._stop.is_set)
            return self._loaded(self._server.prepare(key, write=write), name, name, source, {"name": name})
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
        """Every recording file (.dvf, .wav...) in the library folder and all its subfolders (at most
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
                    self._library_root = None
                    self._lib_job = None
            return result
        root_identity = _root_identity(folder)  # before the walk: what the folder ids point into
        found, subfolders, truncated, complete = _scan_library(folder)
        store = self._store
        summary = store.summary() if store is not None else {}
        table, files, pending = {}, [], []
        for investigation, name, kind, path, st, rel in found:
            fid = _file_id(path)
            table[fid] = path
            fp = error = seconds = unplayable = None
            fmt = _kind_format(kind)
            cached = None
            if fmt.decoder is not None and store is not None:
                cached = self._cached_fp(path, st.st_size, st.st_mtime_ns)
            if fmt.decoder is not None and cached is None:
                # A file's own header can say it cannot be decoded yet (an ICD-ST10
                # recording; only the header is read): listed like a file without
                # a decoder, and never cached. A cached file was decoded before.
                unplayable = self._file_problem(fmt, path, st)
            if fmt.decoder is None:
                # Listed, never fingerprinted or cached: no decoder is not the file's fault.
                seconds, error = fmt.seconds(path), f"{name} can't be played or marked: {_decoder_problem(fmt)}."
            elif unplayable:
                seconds, error = fmt.seconds(path), f"{name} can't be played or marked: {unplayable}."
            elif store is None:
                seconds = _seconds(path, kind)
            elif cached is None:
                pending.append((fid, path, kind, st.st_size, st.st_mtime_ns))
            else:
                fp, error, seconds = cached.get("fp"), cached.get("error"), cached.get("seconds")
            s = summary.get(fp) if fp else None
            files.append({"id": fid, "name": name, "investigation": investigation, "type": kind,
                          "seconds": seconds,
                          "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                          "fp": fp, **_marks_row(s or {}, s and s["reviewed"], s and s["notes"]),
                          "error": error, "unplayable": unplayable, "folder_id": _folder_id(rel)})
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
            self._library_root = root_identity
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

    def _file_problem(self, fmt, path, st):
        """fmt.file_problem(path), read once per version (size, mtime) of the file:
        such a file is never cached in the store's index, and without a store no
        file is, so a listing would otherwise read its header every time."""
        key = os.path.normcase(os.path.abspath(path))
        with self._headers_lock:
            held = self._headers.get(key)
        if held is not None and held[:2] == (st.st_size, st.st_mtime_ns):
            return held[2]
        problem = fmt.file_problem(path)
        with self._headers_lock:
            _bounded_put(self._headers, key, (st.st_size, st.st_mtime_ns, problem), SESSION_CACHE)
        return problem

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
            problems = _decoder_problems(p[2] for p in pending)
            for done, (fid, path, kind, size, mtime_ns) in enumerate(pending, 1):
                if superseded():
                    return
                row, stored = self._index_file(path, kind, size, mtime_ns, cancelled, problems.get(kind))
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
                    "unplayable": None, **self._fp_marks(hit.get("fp"))}, False
        fp = length = error = unplayable = None
        cacheable = True
        fmt = _kind_format(kind)
        try:
            if kind == "wav":
                fp = wavinfo.wav_fingerprint(path, should_stop=cancelled)
                length = _wav_length(path)
            elif decoder_problem:
                # Not the file's fault: nothing is cached, so a later scan tries
                # again once the decoder is there.
                error, cacheable = decoder_problem, False
            elif unplayable := fmt.file_problem(path):
                # Its header says it cannot be decoded yet (an ICD-ST10 recording):
                # the file is not read, and nothing is cached (a later decoder will play it).
                error, cacheable = f"{name} can't be played or marked: {unplayable}.", False
            elif fmt.max_bytes is not None and size > fmt.max_bytes:
                error = f"{name} is too large to be {fmt.a_recording()}."
            else:
                with open(path, "rb") as f:
                    data = f.read()
                fp, length = formats.analyze(fmt, data, should_stop=cancelled)
                del data
        except (formats.Cancelled, wavinfo.Stopped):
            return None, False
        except (MemoryError, formats.DecoderUnavailable) as e:   # not the file's fault: not kept
            fp, error, cacheable = None, _plain(e), False
            if isinstance(e, formats.DecoderUnavailable):
                unplayable = _plain(e)
        except OSError as e:                    # locked or vanished: may work next time
            fp, error, cacheable = None, _plain(e), False
        except Exception as e:                  # not a readable recording: remembered
            fp, error = None, _plain(e)
        try:
            st = os.stat(path)
        except OSError:
            return {"fp": None, "seconds": None, "error": f"{name} is no longer there.",
                    "unplayable": None, **_marks_row({}, False, "")}, False
        if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
            # Changed while it was read (still being copied or recorded?): nothing
            # is kept, and the next scan reads it again.
            return {"fp": None, "seconds": None,
                    "error": f"{name} changed while it was being read. Refresh the list to try again.",
                    "unplayable": None, **_marks_row({}, False, "")}, False
        seconds = round(length, 1) if length else _seconds(path, kind)
        stored = cacheable and self._remember_fp(path, size, mtime_ns, fp, seconds, error)
        if kind == "wav" and fp and length:     # an empty WAV gets no marks (no identity of its own)
            self._import_markers(path, {"fp": fp, "duration": length}, name, (size, mtime_ns))
        return {"fp": fp, "seconds": seconds, "error": error, "unplayable": unplayable,
                **self._fp_marks(fp)}, stored

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
            with self._dest_lock:
                self._dest = picked
                self._dest_chosen = True
            if self._store is not None:
                try:
                    self._store.set_setting("save_folder", picked)
                except (StoreReadOnly, StoreUnavailable):
                    pass                        # used for this session; not remembered
        return picked

    def _second_window(self):
        """Why this window does not export (the store's read-only reason), or None.
        Another OpenEVP holding the store may be renaming or deleting the folders
        this one would write into."""
        return self._store_read_only()

    def export(self, device_id, items, fmt, dest, job):
        if self._stop.is_set():
            return _fail(CLOSING)
        read_only = self._second_window()
        if read_only:
            return _fail(read_only)
        try:
            model = self._listing(device_id)["model"]
        except Exception as e:
            return _error(e)
        menu = {f["value"]: f for f in self._export_formats(model)}
        if not isinstance(fmt, str) or fmt not in menu:
            return _fail(f"Unknown format {fmt!r}.")
        if not menu[fmt]["available"]:
            return _fail(f"WAV export: {menu[fmt]['reason']}.")
        work = _parse_items(items)
        if work is None:
            return _fail("Nothing valid is selected.")
        if not isinstance(dest, str) or not dest:
            return _fail("Choose a folder to save to.")
        try:
            found = [self._find(device_id, folder_id, number) for folder_id, number in work]
        except Exception as e:
            return _error(e)
        if not all(found):
            return _fail("Nothing valid is selected.")
        work = [(folder, row["number"]) for _model, folder, row in found]
        to_wav = fmt == "wav" and model.native is not formats.WAV
        if not self._busy.acquire(blocking=False):
            return _fail("An export is already running.")
        thread = threading.Thread(target=self._export, args=(device_id, model.native, work, to_wav, dest, job),
                                  name="export")
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
        return self._busy.locked() and not self._updating and self._marked_done is None and not self._fs_busy

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

    def _export(self, device_id, native, work, to_wav, dest, job):
        """Download each recording (on the device thread), then save it (here, never
        inside a recorder call) as <dest>/<folder safe name>/<its file name>: the
        native file, or a WAV with the recording's marks (to_wav)."""
        saved = skipped = 0
        notes = []

        def failed(err):
            self._emit("export-failed", {**err, "job": job, "saved": saved, "skipped": skipped, "notes": notes})

        try:
            for i, (folder, number) in enumerate(work, 1):
                if self._stop.is_set():
                    failed(_fail("Stopped because the app is closing."))
                    return
                try:
                    dl = self._manager.with_session(device_id, lambda s, f=folder["id"], n=number: s.download(f, n))
                except Exception as e:
                    failed(_error(e))
                    return
                if dl.error:
                    notes.append(f"{dl.label}: not saved ({dl.error})")
                    continue
                if not _usable_filename(native, dl.filename):
                    notes.append(f"{dl.label}: not saved (the recorder gave it a file name OpenEVP cannot use)")
                    continue
                outdir = os.path.join(dest, folder["safe_name"])
                try:
                    os.makedirs(outdir, exist_ok=True)
                    if not to_wav:
                        _, done = save_native(native, dl.data, outdir, dl.filename)
                    else:
                        wav = _decoder(native).to_wav(dl.data, should_stop=self._stop.is_set)
                        wav, problem = self._marked_wav(wav)
                        if problem:
                            notes.append(f"{dl.label}: {problem}")
                        _, done = save_wav(wav, outdir, os.path.splitext(dl.filename)[0] + ".wav")
                        del wav
                except formats.Cancelled:          # the window was closed during a long decode
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
