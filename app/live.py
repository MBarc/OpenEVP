"""Live mode and analog import (a mixin of app.backend.Api): recording from one of
the PC's audio inputs into the EVP Library. See docs/live-mode.md.

The page captures the audio (getUserMedia and an AudioWorklet: the input exactly
as Windows delivers it, with echo cancellation, noise suppression and automatic
gain off) and sends it here as 16-bit PCM in numbered chunks of about half a
second (live_chunk, base64 over the pywebview bridge, one call at a time). Here
it is written straight to disk (openevp.livewav): "<name>.part" beside a hidden
"<name>.part.json" sidecar holding the format, the final name and the marks
made so far, both in the library folder the recording goes into. The .part's
header is rewritten every few seconds, so a crash leaves a playable file, and
every .part is listed in the setting "live_parts" until it is finished; the
next start finishes any left behind (live_recover) and says so.

Live mode makes one file, "Live YYYY-MM-DD HH-MM-SS.wav". Import mode (a
recorder's headphone output into line-in) records one file the same way,
"Import YYYY-MM-DD HH-MM-SS (full).wav" when it is to be split on silence:
after Stop it opens in the player with suggested cuts (openevp.silence: one
pass over the whole file, in the background), which the user edits and
confirms (split_import); a background job then writes one file per part beside
it, "Import YYYY-MM-DD HH-MM-SS (n).wav", which together are the whole file;
the whole file is always kept. Stop (or a
nearly full disk, or a file reaching 4 GB) finishes the file: the .part gets its
final name (never over an existing file), the marks made while recording are
stored against its fingerprint (computed while writing) and its fingerprint is
put in the library's index, so the library never reads it again to list it.

A mark (M) is a 3-second region ending at the moment it was made (shorter at
the very start), class C, note MARK_NOTE: the marks store holds regions, and
class and note are set later in the EVP Library (the player).

Recording needs the writable store (the list of unfinished files and the marks
live there): a second OpenEVP window cannot record. While a recording runs, the
library's folder operations (rename, move, delete) and updates wait.
"""
import base64
import binascii
import datetime
import json
import math
import os
import secrets
import shutil
import threading
import time
import wave

from openevp import livewav, silence, wavinfo

from . import folders
from .library_ops import CLOSING, ROOT_CHANGED, _fail, _file_id, _plain, _root_identity, _under_clips
from .store import MIN_MARK_LENGTH, StoreReadOnly, StoreUnavailable

PARTS_SETTING = "live_parts"     # the .part files not finished yet (absolute paths)
MAX_UNSAVED = 200
UNSAVED_LOG = "live-unsaved.jsonl"  # in the data folder: changes made while recording that may not have been
                                    # saved when OpenEVP closed ({"file", "items"} a line), said at the next start
LIVE_SETTING = "live"            # {"input": {"id", "label"}, "split": seconds (0 = off), "import": bool,
                                 #  "field": bool (the dark night screen)}
RESERVE_BYTES = 500 << 20        # recording stops before the disk has less than this free
WARN_SECONDS = 15 * 60           # ... and warns once less than this much audio still fits
MARK_SECONDS = 3.0               # a mark: this long, ending when M was pressed
MARK_CLASS = "C"
MARK_NOTE = "Marked while recording, not graded yet"
MAX_CHUNK = 8 << 20              # bytes of PCM in one live_chunk (half a second is under 400 KB)
MARK_SLACK = 2.0                 # seconds a mark may be ahead of the audio received (the page's own buffer)
RATES = (8000, 192000)
LIVE_IDLE = 60.0                 # seconds with no chunk or mark: the page is gone or stuck, the file is finished
LIVE_WATCH_EVERY = 5.0
SHUTDOWN_WAIT = 10.0             # seconds closing waits for the recording's lock

NOT_RECORDING = "Nothing is being recorded."
RECORDING = "Stop the recording first."
NO_STORE = "Recording needs OpenEVP's data folder"
NO_FOLDER = "That folder is no longer in the library. Choose another one."
IN_CLIPS = "A Clips folder is for EVP clips only. Choose another folder to record into."
NO_SPACE = ("There is not enough free space on that drive to record (OpenEVP keeps at least 500 MB free). "
            "Free some space or choose a folder on another drive.")
STOPPED_DISK = "Recording stopped because the drive is nearly full (OpenEVP keeps at least 500 MB free)."
STOPPED_SIZE = "Recording stopped because the file reached 4 GB, the most a WAV file can hold."
UPDATING = "An update is being installed, so OpenEVP is about to close. Record after it restarts."
SPLITTING = "Wait for the import to be split into separate recordings, or cancel that."
SPLIT_NO_SPACE = "there is not enough free space on the drive for the separate files"
MAX_CUTS = 500                   # cuts in one split
MIN_PIECE = 0.5                  # seconds: the shortest part a split makes
JOB_JOIN = 10.0                  # seconds closing waits for the import jobs, in all
UNRECOVERED = " (unrecovered).raw"
STOPPED_IDLE = "Recording stopped because no audio arrived for a minute. What was recorded until then is saved."


class _Moved(OSError):
    """The folder a recording goes into is no longer where it was in the library."""

    def __init__(self):
        super().__init__("the folder is no longer where it was in the library")


class _Kept(Exception):
    """live_recover(): a leftover could not be read as audio and was kept under another name."""


class _MetaPending(Exception):
    """live_recover(): the audio is saved, but not all its marks reached the
    store; its journal and its place in the recovery list are kept, to try again."""


def _meta_later(name, why):
    """A finished file whose marks could not all be stored yet (said to the user)."""
    return (f"{name} is saved, but some of its marks could not be stored yet: {why} "
            "They are kept, and OpenEVP will try again the next time it starts.")


def _stamp(now):
    return now.strftime("%Y-%m-%d %H-%M-%S")


def _clean_input(v):
    if not isinstance(v, dict):
        return None
    label, ident = v.get("label"), v.get("id")
    if not isinstance(label, str) or not isinstance(ident, str) or len(label) > 300 or len(ident) > 300:
        return None
    return {"label": label, "id": ident}


def _split(v):
    """A split gap from the page: 0 (off) or 0.5..MAX_GAP seconds, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v == 0:
        return 0
    return float(v) if 0.5 <= v <= silence.MAX_GAP else None


def _write_sidecar(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        folders.hide(path)
    except Exception:
        pass


def _journal(sidecar, meta, target, fp, frames):
    """Before a finished .part is renamed: write down where it is going and what it
    holds, so a crash between the rename and storing its marks can be finished
    (live_recover finds the WAV by this and stores the marks then)."""
    meta.update(published=target, fp=fp, frames=frames)
    _write_sidecar(sidecar, meta)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _sidecar_marks(meta):
    """The marks a sidecar holds that look like marks (anything else is ignored)."""
    marks = meta.get("marks") if isinstance(meta.get("marks"), list) else []
    return [m for m in marks if isinstance(m, dict) and all(
        isinstance(m.get(k), (int, float)) and not isinstance(m.get(k), bool) for k in ("start", "end"))
        and m.get("cls") in ("A", "B", "C") and isinstance(m.get("note"), str)]


def _clamped(mark, duration):
    """A mark (start, end in the file's seconds) fitted into a file of `duration`
    seconds, or None when too little of it is left."""
    # Milliseconds, rounded down: a mark made at the very last sample must not round past the end.
    end = math.floor(min(mark["end"], duration) * 1000) / 1000
    start = math.floor(max(0.0, min(mark["start"], end - MIN_MARK_LENGTH)) * 1000) / 1000
    if end - start < MIN_MARK_LENGTH - 1e-9:
        return None
    return {**mark, "start": start, "end": end}


class _Piece:
    """One file being written: its .part, sidecar, writer, where it starts in the
    stream, and the marks made in it so far (in its own seconds)."""

    def __init__(self, folder, name, rate, channels, start_frame, mode, derived=False):
        self.folder, self.name, self.start_frame = folder, name, start_frame
        self.part = os.path.join(folder, name + livewav.PART)
        self.sidecar = self.part + ".json"
        self.marks = []
        self.meta = {"version": 1, "name": name, "rate": rate, "channels": channels, "mode": mode,
                     "started": datetime.datetime.now().isoformat(timespec="seconds"), "marks": self.marks}
        if derived:                              # a piece cut from a kept file: never recovered, just deleted
            self.meta["derived"] = True
        _write_sidecar(self.sidecar, self.meta)
        try:
            self.writer = livewav.WavPart(self.part, rate, channels)
        except BaseException:
            _remove(self.sidecar)
            raise

    def save_marks(self):
        _write_sidecar(self.sidecar, self.meta)


class _Session:
    """One recording: a single file, as Live mode and Import both record. An import
    with a silence gap set is split into pieces after Stop (LiveOps._split_job)."""

    def __init__(self, ops, sid, mode, root, folder, rate, channels, split, pins=None, root_id=None):
        self.ops, self.id, self.mode, self.root, self.folder = ops, sid, mode, root, folder
        self.split = split if mode == "import" else 0
        self.pins, self.root_id = pins, root_id   # the folders held for the recording, and what the root was
        self.last_seen = time.monotonic()        # the page's last chunk or mark (see LiveOps._live_watch)
        self.closed = threading.Event()          # set once finished
        self.rate, self.channels, self.align = rate, channels, channels * livewav.WIDTH
        self.byte_rate = rate * self.align
        self.stamp = _stamp(datetime.datetime.now())
        self.seq = 0
        self.frames = 0                          # frames received
        self.saved = []                          # the finished file: {"path", "fp", "frames", "marks"}
        self.problems = []
        self.dropped_marks = 0
        self.stopped = None                      # why it stopped by itself (a sentence), once it has
        self.full = False                        # the file reached livewav.MAX_DATA
        self.done = False
        if mode == "live":
            name = f"Live {self.stamp}.wav"
        else:
            name = f"Import {self.stamp} (full).wav" if self.split else f"Import {self.stamp}.wav"
        if not self.contained():
            raise _Moved()
        self.piece = _Piece(folder, name, rate, channels, 0, mode)
        try:
            ops._register_part(self.piece.part)
        except Exception:
            self.piece.writer.abort()
            _remove(self.piece.sidecar)
            raise

    def contained(self):
        """Is the folder still where it was, inside the same library folder (no link on the way)?"""
        return (self.root_id is None or _root_identity(self.root) == self.root_id) and \
            folders.inside(self.root, self.folder, allow_root=True)

    def feed(self, data):
        self.frames += len(data) // self.align
        if self.full or self.piece is None:
            return
        try:
            self.piece.writer.write(data)
        except livewav.Full:                     # 4 GB: the rest is dropped and the recording stops
            self.full = True

    def finish(self):
        """Stop: finish the file. Never raises; problems are kept in self.problems."""
        if self.done:
            return
        self.done = True
        piece, self.piece = self.piece, None
        if piece is None:
            return
        try:
            if piece.writer.frames == 0:
                piece.writer.abort()
                _remove(piece.sidecar)
                self.ops._unregister_part(piece.part)
            else:
                self.ops._finish_piece(self, piece)
        except Exception as e:
            self.problems.append(f"The recording could not be finished: {_plain(e)}. It is saved as "
                                 "a .part file and will be finished when OpenEVP starts again.")

    def status(self):
        return {"ok": True, "seconds": round(self.frames / self.rate, 3),
                "file": self.piece.name if self.piece is not None else None, "stopped": self.stopped}


class LiveOps:
    """Live mode and analog import; mixed into app.backend.Api."""

    def _live_init(self):
        self._live_lock = threading.Lock()       # held around every call that touches the session
        self._live = None                        # the _Session recording now
        self._parts_lock = threading.Lock()
        self._live_saved = None                  # Live settings picked in this session (when they could not be remembered)
        self._splits = {}                        # job -> (cancel Event, thread): cuts being looked for, or a split

    # ---- state other parts of the app ask about ----
    def recording(self):
        """True while a Live recording or an import runs (the close prompt asks)."""
        return self._live is not None

    def _live_busy(self):
        """Why a folder operation or an update must wait now (a recording runs, or an
        import is being split), or None."""
        return RECORDING if self._live is not None else (SPLITTING if self._splits else None)

    # ---- settings ----
    def live_settings(self):
        """The Live view's remembered choices: the input last used ({"id", "label"} or
        None), the silence split in seconds (0 = off), whether Import was on, and whether
        the night screen is on. (The Enhance settings are the player's: one setting, shared.)"""
        saved = self._store.get_setting(LIVE_SETTING) if self._store is not None else None
        saved = self._live_saved or (saved if isinstance(saved, dict) else {})
        split = _split(saved.get("split"))
        return {"ok": True, "input": _clean_input(saved.get("input")),
                "split": silence.DEFAULT_GAP if split is None else split,
                "import": saved.get("import") is True,
                "field": saved.get("field") is True,
                "max_split": silence.MAX_GAP,
                "reserve_mb": RESERVE_BYTES >> 20}

    def set_live_settings(self, changes):
        if not isinstance(changes, dict) or set(changes) - {"input", "split", "import", "field"}:
            return _fail("Unknown Live settings.")
        current = self.live_settings()
        new = {"input": current["input"], "split": current["split"], "import": current["import"],
               "field": current["field"]}
        if "input" in changes:
            new["input"] = _clean_input(changes["input"]) if changes["input"] is not None else None
            if changes["input"] is not None and new["input"] is None:
                return _fail("Unknown input.")
        if "split" in changes:
            if _split(changes["split"]) is None:
                return _fail(f"The silence gap must be between 0.5 and {silence.MAX_GAP:g} seconds.")
            new["split"] = _split(changes["split"])
        for key in ("import", "field"):
            if key in changes:
                if not isinstance(changes[key], bool):
                    return _fail("Unknown Live settings.")
                new[key] = changes[key]
        self._live_saved = new                   # this session's, also when it cannot be remembered
        if self._store is not None:
            try:
                self._store.set_setting(LIVE_SETTING, new)
            except (StoreReadOnly, StoreUnavailable):
                pass
        return {"ok": True, **self.live_settings()}

    def open_sound_settings(self):
        """Windows' sound settings (no sound coming in: a muted microphone, the wrong input)."""
        try:
            os.startfile("ms-settings:sound")
            return {"ok": True}
        except (OSError, AttributeError) as e:
            return _fail(f"Windows Settings could not be opened: {_plain(e)}")

    def open_mic_settings(self):
        """Windows' microphone privacy page (when Windows blocks desktop apps from the microphone)."""
        try:
            os.startfile("ms-settings:privacy-microphone")
            return {"ok": True}
        except (OSError, AttributeError) as e:
            return _fail(f"Windows Settings could not be opened: {_plain(e)}")

    # ---- the list of unfinished files ----
    def _parts(self):
        got = self._store.get_setting(PARTS_SETTING) if self._store is not None else None
        return [p for p in got if isinstance(p, str)] if isinstance(got, list) else []

    def _register_part(self, part):
        with self._parts_lock:
            self._store.set_setting(PARTS_SETTING, self._parts() + [part])

    def _unregister_part(self, part):
        with self._parts_lock:
            try:
                self._store.set_setting(PARTS_SETTING, [p for p in self._parts()
                                                        if os.path.normcase(p) != os.path.normcase(part)])
            except (StoreReadOnly, StoreUnavailable):
                pass                             # finished all the same; the next start finds no .part

    # ---- recording ----
    def _live_folder(self, folder_id):
        """(library root, folder) to record into, or a _fail(). "root" (or None) is the
        library folder itself, created if it is not there yet."""
        if folder_id in (None, "root"):
            root = self._library_path()
            with self._lib_lock:
                listed = self._library_root is not None and self._library_folders.get("root") is not None \
                    and os.path.normcase(os.path.abspath(self._library_folders["root"])) == \
                    os.path.normcase(os.path.abspath(root))
            if listed and self._root_moved(root):     # swapped for a link since the library listed it
                return _fail(ROOT_CHANGED)
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as e:
                return _fail(f"Could not create the library folder: {_plain(e)}")
            return root, root
        found = self._lib_paths(folder_id)
        if found is None:
            return _fail(NO_FOLDER)
        root, path = found
        problem = self._usable(root, path, allow_root=True)
        if problem:
            return _fail(NO_FOLDER)
        return root, path

    def live_start(self, options):
        """Start recording: options {"mode": "live" | "import", "folder": a library folder
        id ("root" for the library folder), "rate", "channels" (1 or 2), "split": seconds
        of silence that start a new file (import; 0 = off)}. {"ok", "session", "file",
        "folder"} or a failure."""
        if not isinstance(options, dict):
            return _fail("Unknown recording settings.")
        mode, rate, channels = options.get("mode"), options.get("rate"), options.get("channels")
        split = _split(options.get("split", 0))
        if mode not in ("live", "import") or isinstance(rate, bool) or not isinstance(rate, int) \
                or not RATES[0] <= rate <= RATES[1] or channels not in (1, 2) or isinstance(channels, bool) \
                or split is None:
            return _fail("Unknown recording settings.")
        if self._stop.is_set():
            return _fail(CLOSING)
        store = self._store
        if store is None:
            return _fail(f"{NO_STORE}, which could not be opened.")
        if store.read_only:
            return _fail(f"{NO_STORE}: {store.read_only_reason}")
        with self._live_lock:
            if self._live is not None:
                return _fail("A recording is already running.")
            if self._splits:
                return _fail(SPLITTING)
            got = self._live_folder(options.get("folder"))
            if isinstance(got, dict):
                return got
            root, folder = got
            # Held for the whole recording: no folder from the library folder down to this
            # one can be renamed, moved or swapped for a junction while files go into it.
            pins = folders.Pins()
            try:
                pins.chain(root, folder)
            except (OSError, ValueError):
                pins.close()
                return _fail(NO_FOLDER)
            root_id = _root_identity(root)
            if root_id is None or not folders.inside(root, folder, allow_root=True):
                pins.close()
                return _fail(NO_FOLDER)
            if _under_clips(folder, root):
                pins.close()
                return _fail(IN_CLIPS)
            try:
                free = shutil.disk_usage(folder).free
            except OSError as e:
                pins.close()
                return _fail(f"Could not check the free space: {_plain(e)}")
            if free < RESERVE_BYTES + 60 * rate * channels * livewav.WIDTH:
                pins.close()
                return _fail(NO_SPACE)
            # Admitted against folder operations in one step: _fs_begin() checks for a
            # recording under _workers_lock (never take _lib_lock inside it: see _list_library).
            with self._workers_lock:
                if self._stop.is_set():
                    pins.close()
                    return _fail(CLOSING)
                if self._fs_done is not None:
                    pins.close()
                    return _fail("Wait for the library to finish renaming, moving or deleting files.")
                if self._update_claim:           # install_update() admits itself under this lock too
                    pins.close()
                    return _fail(UPDATING)
                try:
                    session = _Session(self, secrets.token_hex(8), mode, root, folder, rate, channels, split,
                                       pins=pins, root_id=root_id)
                except (StoreReadOnly, StoreUnavailable) as e:
                    pins.close()
                    return _fail(f"{NO_STORE}: {e}")
                except _Moved:
                    pins.close()
                    return _fail(NO_FOLDER)
                except OSError as e:
                    pins.close()
                    return _fail(f"Could not start the recording: {_plain(e)}")
                self._live = session
        threading.Thread(target=self._live_watch, args=(session,), name="live-watch", daemon=True).start()
        return {"ok": True, "session": session.id, "file": session.piece.name,
                "left_seconds": max(0, int(min((free - RESERVE_BYTES) / session.byte_rate,
                                               session.piece.writer.room() / session.byte_rate))),
                "folder": os.path.basename(os.path.normpath(folder)), "rate": rate, "channels": channels}

    def _session(self, sid):
        s = self._live
        if s is not None and isinstance(sid, str) and s.id == sid:
            s.last_seen = time.monotonic()
            return s
        return None

    def _live_watch(self, s):
        """While a session runs: if the page has sent nothing for LIVE_IDLE seconds (it
        is gone, or its bridge stopped answering), finish the file so it is not left
        open until the app closes."""
        while not s.closed.wait(LIVE_WATCH_EVERY):
            if time.monotonic() - s.last_seen > LIVE_IDLE:
                with self._live_lock:
                    if self._live is s and not s.done:
                        s.stopped = STOPPED_IDLE
                        self._live_finish(s, open_player=False)
                return

    def live_chunk(self, sid, seq, data):
        """Write the next chunk (seq: 0, 1, 2... in order; data: base64 16-bit PCM,
        whole frames). The status: seconds received, the file being written, files
        saved, a warning when the disk is getting full or the file nears 4 GB, and
        "stopped" (with the result of finishing) once the recording stopped by itself."""
        with self._live_lock:
            s = self._session(sid)
            if s is None:
                return _fail(NOT_RECORDING)
            if isinstance(seq, bool) or not isinstance(seq, int) or seq != s.seq:
                return self._live_end(s, "Part of the recording did not arrive, so it was stopped and saved.")
            if not isinstance(data, str) or len(data) > MAX_CHUNK * 4 // 3 + 4:
                return _fail("Unknown audio data.")
            try:
                pcm = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                return _fail("Unknown audio data.")
            if len(pcm) % s.align:
                return _fail("Unknown audio data.")
            s.seq += 1
            try:
                free = shutil.disk_usage(s.folder).free
            except OSError:
                free = None
            if free is not None and free - len(pcm) < RESERVE_BYTES:
                return self._live_end(s, STOPPED_DISK)
            try:
                s.feed(pcm)
            except OSError as e:
                return self._live_end(s, f"Recording stopped: the file could not be written ({_plain(e)}).")
            if s.full:
                return self._live_end(s, STOPPED_SIZE)
            out = s.status()
            left = None
            if free is not None:
                left = (free - RESERVE_BYTES) / s.byte_rate
            if s.piece is not None:
                room = s.piece.writer.room() / s.byte_rate
                left = room if left is None else min(left, room)
            if left is not None:
                out["left_seconds"] = max(0, int(left))      # the status strip: "about 9 h left"
            if left is not None and left < WARN_SECONDS:
                minutes = max(1, int(left // 60))
                out["warning"] = (f"Only about {minutes} minute{'s' if minutes != 1 else ''} of recording "
                                  "left before OpenEVP stops it (disk space or the 4 GB file limit).")
            return out

    def _live_end(self, s, reason):
        """The recording stops by itself (under _live_lock): finished, and the reason said."""
        s.stopped = reason
        result = self._live_finish(s)
        return {"ok": True, **s.status(), "stopped": reason, "result": result}

    def live_mark(self, sid, at):
        """Mark the moment `at` (seconds since Record, as the page counts them): a
        MARK_SECONDS region ending there. An import split later gives each piece the
        marks that end in it."""
        if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at or at < 0:
            return _fail("Unknown moment.")
        with self._live_lock:
            s = self._session(sid)
            if s is None or s.piece is None:
                return _fail(NOT_RECORDING)
            at = min(float(at), s.frames / s.rate + MARK_SLACK)
            mark = {"start": round(max(0.0, at - MARK_SECONDS), 3), "end": round(at, 3),
                    "cls": MARK_CLASS, "note": MARK_NOTE}
            if mark["end"] - mark["start"] < MIN_MARK_LENGTH:
                mark["end"] = round(mark["start"] + MIN_MARK_LENGTH, 3)
            s.piece.marks.append(mark)
            try:
                s.piece.save_marks()
            except OSError:
                pass                             # kept in memory: stored when the file is finished
            return {"ok": True, "mark": {"at": at, "file": s.piece.name, **mark}}

    def live_stop(self, sid):
        """Stop and save: {"ok", "files": [{"id", "name", "seconds", "marks"}], "folder",
        "problems", "dropped_marks", "player" (Live mode: the recording, loaded for the
        player as play_library() would)}."""
        with self._live_lock:
            s = self._session(sid)
            if s is None:
                return _fail(NOT_RECORDING)
            return self._live_finish(s)

    def _live_finish(self, s, open_player=True):
        """Finish a session (under _live_lock) and describe what was saved."""
        try:
            s.finish()
        finally:
            if s.pins is not None:
                s.pins.close()
            if self._live is s:
                self._live = None
            s.closed.set()
        def row(f):
            return {"id": _file_id(f["path"]), "name": os.path.basename(f["path"]),
                    "seconds": round(f["frames"] / s.rate, 1), "marks": f["marks"]}
        files = [row(f) for f in s.saved]
        out = {"ok": True, "files": files, "folder": os.path.basename(os.path.normpath(s.folder)),
               "problems": list(s.problems), "dropped_marks": s.dropped_marks, "mode": s.mode}
        if s.split and s.saved and not self._stop.is_set():
            # An import: the cuts are suggested in the background ("import-suggest-*"), shown on
            # the waveform for the user to confirm (split_import) or not.
            job = self._start_job(self._suggest_job, s.saved[0]["path"], s.saved[0]["fp"], s.split)
            if isinstance(job, str):
                out["suggest"] = {"job": job, "fp": s.saved[0]["fp"]}
        if (s.mode == "live" or s.split) and s.saved and open_player and not self._stop.is_set():
            # Saved either way; if it cannot be opened in the player, the page says why.
            try:
                loaded = self._play_file(s.saved[-1]["path"], root=s.root, library=True)
            except Exception as e:
                loaded = _fail(f"{type(e).__name__}: {e}")
            if loaded.get("ok"):
                out["player"] = loaded
            else:
                out["player_error"] = loaded.get("error") or "it could not be opened"

        return out

    def _finish_piece(self, s, piece):
        """A file is complete: rename it, store its marks and its fingerprint (never raises)."""
        try:
            frames, fp = piece.writer.close()
        except OSError as e:
            s.problems.append(f"{piece.name} could not be finished ({_plain(e)}); it will be finished "
                              "when OpenEVP starts again.")
            return
        if frames == 0:
            piece.writer.abort()
            _remove(piece.sidecar)
            self._unregister_part(piece.part)
            return
        try:
            path = livewav.publish(piece.part, piece.folder, piece.name,
                                   before=lambda target: _journal(piece.sidecar, piece.meta, target, fp, frames))
        except OSError as e:
            s.problems.append(f"{piece.name} could not be given its name ({_plain(e)}); it will be "
                              "finished when OpenEVP starts again.")
            return
        seconds = frames / s.rate
        stored, why = self._store_live_marks(fp, path, seconds, piece.marks, s)
        if why is None:
            _remove(piece.sidecar)
            self._unregister_part(piece.part)
        else:
            # The journal (where the file went, and its marks) stays, and so does its
            # place in the recovery list: the next start stores what is missing.
            s.problems.append(_meta_later(os.path.basename(path), why))
        self._index_live(path, fp, seconds)
        s.saved.append({"path": path, "fp": fp, "frames": frames, "marks": stored})

    def _store_live_marks(self, fp, path, seconds, marks, s=None):
        """Store marks made while recording against the finished file. A mark already
        there (the same times, class and note: stored before a crash) is not added
        again, so this can be run twice. (stored, why): why is the store's reason when
        it could not take some marks now (read-only, or the file could not be written)."""
        stored, why = 0, None
        have = {(m["start"], m["end"], m["cls"], m["note"]) for m in self._store.marks(fp)}
        for m in marks:
            m = _clamped(m, seconds)
            if m is None:
                if s is not None:
                    s.dropped_marks += 1
                continue
            if (m["start"], m["end"], m["cls"], m["note"]) in have:
                stored += 1
                continue
            try:
                self._store.add_mark(fp, m["start"], m["end"], m["cls"], m["note"],
                                     name=os.path.basename(path), duration=seconds)
                stored += 1
            except (StoreReadOnly, StoreUnavailable) as e:
                why = why or (str(e) or "the marks could not be saved.")
            except ValueError:
                if s is not None:
                    s.dropped_marks += 1
        return stored, why

    def _index_live(self, path, fp, seconds):
        """Put a finished file's fingerprint in the library's index: listing it never reads it again."""
        try:
            st = os.stat(path)
            self._store.remember_fp(path, st.st_size, st.st_mtime_ns, fp, round(seconds, 1))
            self._store.flush_index()
        except (OSError, StoreReadOnly, StoreUnavailable):
            pass

    def finish_recording(self):
        """Finish the recording running now with what has arrived (the window is closing
        and its page did not stop it in time). {"ok", "finished"}."""
        with self._live_lock:
            s = self._live
            if s is None:
                return {"ok": True, "finished": False}
            s.stopped = s.stopped or "OpenEVP was closing."
            self._live_finish(s, open_player=False)
        return {"ok": True, "finished": True}

    def _live_shutdown(self):
        """The app is closing: finish the recording running (its file is saved). If the
        recording's lock is held that long (a write stuck on the disk), the app closes
        without: the .part is finished when OpenEVP starts again."""
        if not self._live_lock.acquire(timeout=SHUTDOWN_WAIT):
            return
        try:
            if self._live is not None:
                self._live_finish(self._live, open_player=False)
        finally:
            self._live_lock.release()

    # ---- splitting an import: suggested cuts, confirmed by the user, then the split ----
    def _start_job(self, target, *args, exclusive=False):
        """Run target(job, cancel, *args) on a thread of its own: its id, or a _fail()
        saying why not (closing; with exclusive, a library folder operation or an update
        admitted already, which in turn refuse while a job runs: _live_busy). Admission,
        registration and start are one step under _workers_lock, so shutdown's snapshot
        never holds a job whose thread has not started. The page buffers events of a job
        it does not know yet (live.js), so an event that comes before the id reaches the
        page is never lost."""
        job = secrets.token_hex(6)
        cancel = threading.Event()
        thread = threading.Thread(target=self._run_job, args=(job, target, cancel) + args, name="import-job",
                                  daemon=True)
        with self._workers_lock:
            if self._stop.is_set():
                return _fail(CLOSING)
            if exclusive and self._fs_done is not None:
                return _fail("Wait for the library to finish renaming, moving or deleting files.")
            if exclusive and self._update_claim:
                return _fail(UPDATING)
            self._splits[job] = (cancel, thread)
            try:
                thread.start()
            except Exception as e:
                self._splits.pop(job, None)
                return _fail(f"Could not start: {_plain(e)}")
        return job

    def _run_job(self, job, target, cancel, *args):
        try:
            target(job, cancel, *args)
        finally:
            self._splits.pop(job, None)

    def cancel_import_split(self, job):
        """Stop looking for cuts in an import, or stop splitting it (its file stays as it is)."""
        held = self._splits.get(job) if isinstance(job, str) else None
        if held is None:
            return _fail("That import is no longer being split.")
        held[0].set()
        return {"ok": True}

    def splitting(self):
        """True while cuts are being looked for in an import, or it is being split."""
        return bool(self._splits)

    def _stop_jobs(self, timeout=None):
        """Closing: every import job is told to stop, then waited for, at most timeout
        seconds in all (a job never blocks on the page: events are posted, not sent)."""
        timeout = JOB_JOIN if timeout is None else timeout
        with self._workers_lock:                 # registered and started together (_start_job)
            jobs = list(self._splits.values())
        for cancel, _ in jobs:
            cancel.set()
        deadline = time.monotonic() + timeout
        for _, thread in jobs:
            if thread.ident is None:             # never started: nothing to wait for
                continue
            try:
                thread.join(max(0.0, deadline - time.monotonic()))
            except RuntimeError:
                pass

    def _suggest_job(self, job, cancel, path, fp, gap):
        """Look for the gaps between recordings in a finished import (openevp.silence, one
        pass over the file) and tell the page where it suggests cutting: "import-suggest-done"
        {job, fp, cuts (seconds), duration}, or "import-suggest-failed"."""
        stopped = lambda: cancel.is_set() or self._stop.is_set()   # noqa: E731
        shown = [-1]

        def progress(done, total):
            pct = int(100 * done / max(1, total))
            if pct != shown[0]:
                shown[0] = pct
                self._emit("import-suggest-progress", {"job": job, "percent": pct})
        try:
            cuts, rate, _channels, frames = silence.plan(path, gap, stopped, progress)
            self._emit("import-suggest-done", {"job": job, "fp": fp, "duration": round(frames / rate, 3),
                                               "cuts": [round(c / rate, 3) for c in cuts]})
        except Exception as e:
            self._emit("import-suggest-failed", {"job": job, "fp": fp, "cancelled": isinstance(e, silence.Cancelled),
                                                 "error": f"Could not look for the gaps in {os.path.basename(path)}: "
                                                          f"{_plain(e)}. You can still add cuts yourself."})

    def split_import(self, rec, cuts):
        """Split the recording loaded in the player (rec: its handle; a WAV in the library)
        at the cuts the user confirmed (seconds, from the suggestions and their own), into
        one WAV per part beside it; the recording itself is kept. {"ok", "job"}: progress
        and the result come as "import-split-*" events."""
        entry = self._entry(rec)
        if entry is None:
            return _fail("Load the recording again.")
        src = entry.get("source") or {}
        path, root = src.get("path"), src.get("library")
        if src.get("kind") != "file" or not root or not path or not path.lower().endswith(".wav") or not entry.get("fp"):
            return _fail("Only a WAV in the EVP Library can be split.")
        duration = entry.get("duration") or 0
        if not isinstance(cuts, list) or not cuts or len(cuts) > MAX_CUTS or any(
                isinstance(c, bool) or not isinstance(c, (int, float)) or c != c for c in cuts):
            return _fail("Choose where to split first.")
        cuts = sorted(float(c) for c in cuts)
        edges = [0.0] + cuts + [float(duration)]
        if cuts[0] <= 0 or cuts[-1] >= duration or any(b - a < MIN_PIECE for a, b in zip(edges, edges[1:])):
            return _fail(f"Each part must be at least {MIN_PIECE:g} seconds long.")
        if self._stop.is_set():
            return _fail(CLOSING)
        if self._store is None or self._store.read_only:
            return _fail(f"{NO_STORE}: {self._store.read_only_reason if self._store else 'it could not be opened'}")
        with self._live_lock:
            if self._live is not None:
                return _fail(RECORDING)
            if self._splits:
                return _fail(SPLITTING)
            got = self._check_library_file(root, path)      # still that file, inside the same library folder
            if isinstance(got, dict):
                return got
            folder = os.path.dirname(path)
            # The same exclusions as recording: a folder operation or an update admitted
            # already refuses it (and they refuse while it runs).
            job = self._start_job(self._split_job, root, folder, path, entry["fp"], cuts, exclusive=True)
        if isinstance(job, dict):
            return job
        return {"ok": True, "job": job}

    def _split_job(self, job, cancel, root, folder, path, fp, cut_seconds):
        """Split a WAV (kept as it is) at the confirmed cuts into one WAV per part: each is
        written to a .part (listed in "live_parts" with a "derived" sidecar: a crash leaves
        nothing to finish, the next start deletes them) and, once all are written, each
        gets its name, its share of the marks and its fingerprint in the index. Cancelled,
        failed, or the app closing: the parts made so far are deleted and the page is told."""
        stopped = lambda: cancel.is_set() or self._stop.is_set()   # noqa: E731
        shown = [-1]
        stem = os.path.splitext(os.path.basename(path))[0]
        base = stem[:-len(" (full)")] if stem.endswith(" (full)") else stem

        def progress(fraction):
            pct = int(100 * fraction)
            if pct != shown[0]:
                shown[0] = pct
                self._emit("import-split-progress", {"job": job, "percent": pct})
        parts = []
        pins = folders.Pins()
        try:
            try:
                pins.chain(root, folder)
            except (OSError, ValueError):
                raise _Moved() from None
            if not folders.inside(root, path):
                raise _Moved()
            with wave.open(path) as w:
                rate, channels, n = w.getframerate(), w.getnchannels(), w.getnframes()
                if w.getsampwidth() != 2:
                    raise ValueError("only 16-bit PCM can be split")
                if wavinfo.wav_fingerprint(path, should_stop=stopped) != fp:
                    raise ValueError("it changed since it was loaded")
                cuts = sorted({int(round(c * rate)) for c in cut_seconds if 0 < int(round(c * rate)) < n})
                if not cuts:
                    raise ValueError("there is nothing to split")
                if shutil.disk_usage(folder).free - os.path.getsize(path) < RESERVE_BYTES:
                    raise OSError(SPLIT_NO_SPACE)
                bounds = list(zip([0] + cuts, cuts + [n]))
                copied = 0
                for i, (a, b) in enumerate(bounds, 1):
                    if not folders.inside(root, folder, allow_root=True):
                        raise _Moved()
                    piece = _Piece(folder, f"{base} ({i}).wav", rate, channels, a, "import", derived=True)
                    parts.append(piece)
                    self._register_part(piece.part)
                    w.setpos(a)
                    left = b - a
                    while left:
                        if stopped():
                            raise silence.Cancelled()
                        data = w.readframes(min(left, silence.READ_FRAMES))
                        if not data:
                            raise ValueError("the file is shorter than it was")
                        piece.writer.write(data)
                        k = len(data) // (2 * channels)
                        left -= k
                        copied += k
                        progress(copied / max(1, n))
                    piece.result = piece.writer.close()
            if stopped():
                raise silence.Cancelled()
            all_marks = self._store.marks(fp)
            files, problems = [], []
            for piece, (a, b) in zip(parts, bounds):
                got, pfp = piece.result
                seconds = got / rate
                lo, hi = a / rate, b / rate
                mine = [{**m, "start": m["start"] - lo, "end": m["end"] - lo} for m in all_marks
                        if (lo < m["end"] <= hi) or (a == 0 and m["end"] <= hi)]
                piece.marks[:] = mine            # in its journal (piece.meta holds this list)
                # Journalled before it is published (still "derived": a .part left by a crash is
                # deleted, a published piece has its marks stored by the next start). If the journal
                # cannot be written, the piece is not published (and is cleaned up below).
                target = livewav.publish(piece.part, folder, piece.name,
                                         before=lambda t, piece=piece, pfp=pfp, got=got:
                                         _journal(piece.sidecar, piece.meta, t, pfp, got))
                piece.published = True
                stored, why = self._store_live_marks(pfp, target, seconds, mine)
                if why is None:
                    _remove(piece.sidecar)
                    self._unregister_part(piece.part)
                else:                            # its journal stays: the next start stores what is missing
                    problems.append(_meta_later(os.path.basename(target), why))
                self._index_live(target, pfp, seconds)
                files.append({"id": _file_id(target), "name": os.path.basename(target),
                              "seconds": round(seconds, 1), "marks": stored})
            self._emit("import-split-done", {"job": job, "files": files, "folder": os.path.basename(folder),
                                             "full": os.path.basename(path), "problems": problems})
        except Exception as e:
            for piece in parts:
                if getattr(piece, "published", False):
                    continue
                try:
                    piece.writer.abort()
                except OSError:
                    pass
                _remove(piece.part)
                _remove(piece.sidecar)
                self._unregister_part(piece.part)
            cancelled = isinstance(e, (silence.Cancelled, wavinfo.Stopped))
            reason = "it was cancelled" if cancelled else (
                "its folder is no longer where it was in the library" if isinstance(e, _Moved) else _plain(e))
            self._emit("import-split-failed", {"job": job, "cancelled": cancelled, "full": os.path.basename(path),
                                               "error": f"{os.path.basename(path)} was not split into separate "
                                                        f"recordings: {reason}. It is kept as one file."})
        finally:
            pins.close()

    # ---- crash recovery ----
    def _unsaved_log(self):
        folder = getattr(self._store, "_folder", None) if self._store is not None else None
        return os.path.join(folder, UNSAVED_LOG) if folder else None

    def log_unsaved(self, report):
        """The window is closing while a recording is saved, and these marks
        may not have been saved ({"file", "items": [text]} from the page): appended to a plain log
        file in the data folder, flushed to the disk, for the next start to say (live_recover).
        Never through the store: its lock may be held by a stalled write, and closing must not wait."""
        path = self._unsaved_log()
        if path is None or not isinstance(report, dict) or not isinstance(report.get("items"), list):
            return False
        items = [x[:400] for x in report["items"] if isinstance(x, str) and x][:MAX_UNSAVED]
        name = report.get("file") if isinstance(report.get("file"), str) else ""
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"file": name[:300], "items": items}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            return False

    def _read_unsaved_log(self):
        """The changes log_unsaved() wrote down, once (the file is removed after reading)."""
        path = self._unsaved_log()
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        k = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(k, dict) and isinstance(k.get("items"), list) and k["items"]:
                        out.append({"file": k.get("file") if isinstance(k.get("file"), str) else "",
                                    "items": [x for x in k["items"] if isinstance(x, str)]})
            os.remove(path)
        except (OSError, TypeError):
            pass
        return out

    def live_recover(self):
        """Finish the .part files a crash left behind (once the store is writable):
        {"ok", "recovered": [{"name", "folder", "seconds", "marks"}], "failed": [text],
        "unsaved": [{"file", "items"}] (changes that may not have been saved when OpenEVP
        last closed during a recording, from live-unsaved.jsonl; said once)}."""
        out = {"ok": True, "recovered": [], "failed": [], "unsaved": []}
        store = self._store
        if store is None or store.read_only:
            return out
        out["unsaved"] = self._read_unsaved_log()
        with self._live_lock:
            active = set()
            if self._live is not None and self._live.piece is not None:
                active.add(os.path.normcase(self._live.piece.part))
            for part in self._parts():
                if os.path.normcase(part) in active:
                    continue
                try:
                    got = self._recover_part(part)
                except _MetaPending as e:
                    out["failed"].append(str(e))         # the audio is saved; kept in the list to try again
                    continue
                except Exception as e:
                    got = None
                    out["failed"].append(f"{os.path.basename(part)}: {_plain(e)}")
                    # Left in the list while there is something to try again: the .part, or its
                    # journal (a published WAV that could not be read just now, its marks not stored).
                    if os.path.exists(part) or os.path.exists(part + ".json"):
                        continue
                if got:
                    out["recovered"].append(got)
                self._unregister_part(part)
        return out

    def _recover_published(self, part, sidecar, meta):
        """The .part is gone. If its sidecar journals a WAV it was renamed to (a crash
        between the rename and storing the marks) and that WAV is still the same audio,
        store the marks now; otherwise there is nothing left to do."""
        target, fp, frames = meta.get("published"), meta.get("fp"), meta.get("frames")
        if not (isinstance(target, str) and isinstance(fp, str) and isinstance(frames, int) and frames > 0
                and os.path.dirname(os.path.normcase(target)) == os.path.dirname(os.path.normcase(part))
                and os.path.isfile(target)):
            _remove(sidecar)
            return None
        try:
            same = wavinfo.wav_fingerprint(target) == fp
        except ValueError:
            same = False
        if not same:                             # not the file it was (replaced since)
            _remove(sidecar)
            return None
        with wave.open(target) as w:
            rate = w.getframerate()
        seconds = frames / rate
        return self._recovered(sidecar, fp, target, seconds, meta)

    def _recovered(self, sidecar, fp, path, seconds, meta):
        """A recovered file: store its marks; the journal goes only once all are stored."""
        stored, why = self._store_live_marks(fp, path, seconds, _sidecar_marks(meta))
        self._index_live(path, fp, seconds)
        if why is not None:
            raise _MetaPending(_meta_later(os.path.basename(path), why))
        _remove(sidecar)
        return {"name": os.path.basename(path), "folder": os.path.basename(os.path.dirname(path)),
                "seconds": round(seconds, 1), "marks": stored, "id": _file_id(path)}

    def _recover_part(self, part):
        """Finish one leftover .part; None when there was nothing to keep."""
        sidecar = part + ".json"
        meta = {}
        try:
            with open(sidecar, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        if not isinstance(meta, dict):
            meta = {}
        if not os.path.isfile(part):
            # Published (a split piece too: journalled before it was), its marks maybe not stored.
            return self._recover_published(part, sidecar, meta)
        if meta.get("derived"):                  # a piece being cut from a kept import: just deleted
            _remove(part)
            _remove(sidecar)
            return None
        folder = os.path.dirname(part)
        name = meta.get("name")
        base = os.path.basename(part)[:-len(livewav.PART)]
        if not isinstance(name, str) or os.path.basename(name) != name or not name.lower().endswith(".wav"):
            name = base if base.lower().endswith(".wav") else base + ".wav"
        rate, channels = meta.get("rate"), meta.get("channels")
        try:
            rate, channels, frames = livewav.recover(part, rate if isinstance(rate, int) else None,
                                                     channels if channels in (1, 2) else None)
        except livewav.Unreadable:
            # Neither the header nor the sidecar says what the bytes are: never deleted
            # (unless there are no bytes after where a header would be), kept under a
            # name that says so.
            if os.path.getsize(part) <= livewav.HEADER_BYTES:
                _remove(part)
                _remove(sidecar)
                return None
            kept = livewav.publish(part, folder, os.path.splitext(name)[0] + UNRECOVERED)
            if os.path.exists(sidecar):
                try:
                    livewav.publish(sidecar, folder, os.path.basename(kept) + ".json")
                except OSError:
                    pass
            raise _Kept(f"it could not be read as audio, so it was kept as {os.path.basename(kept)} "
                        "in the same folder (nothing was deleted)") from None
        if not frames:                           # a readable header and no audio after it
            _remove(part)
            _remove(sidecar)
            return None
        fp = wavinfo.wav_fingerprint(part)
        path = livewav.publish(part, folder, name, before=lambda target: _journal(sidecar, meta, target, fp, frames))
        seconds = frames / rate
        return self._recovered(sidecar, fp, path, seconds, meta)
