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
recorder's headphone output into line-in) can split on silence
(openevp.silence): each piece is "Import YYYY-MM-DD HH-MM-SS (n).wav". Stop (or a
nearly full disk, or a file reaching 4 GB) finishes the file: the .part gets its
final name (never over an existing file), the marks made while recording are
stored against its fingerprint (computed while writing) and its fingerprint is
put in the library's index, so the library never reads it again to list it.

A mark (M) is a 2-second region ending at the moment it was made (shorter at
the very start), class C, note MARK_NOTE: the marks store holds regions, and
class and note can be changed in the player afterwards.

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
import wave

from openevp import livewav, silence, wavinfo

from . import folders
from .library_ops import CLOSING, ROOT_CHANGED, _fail, _file_id, _plain, _root_identity, _under_clips
from .store import MIN_MARK_LENGTH, StoreReadOnly, StoreUnavailable

PARTS_SETTING = "live_parts"     # the .part files not finished yet (absolute paths)
LIVE_SETTING = "live"            # {"input": {"id", "label"}, "split": seconds (0 = off), "import": bool}
RESERVE_BYTES = 500 << 20        # recording stops before the disk has less than this free
WARN_SECONDS = 15 * 60           # ... and warns once less than this much audio still fits
MARK_SECONDS = 2.0               # a mark: this long, ending when M was pressed
MARK_CLASS = "C"
MARK_NOTE = "Marked while recording"
MAX_CHUNK = 8 << 20              # bytes of PCM in one live_chunk (half a second is under 400 KB)
MARK_SLACK = 2.0                 # seconds a mark may be ahead of the audio received (the page's own buffer)
LATE_MARK = 2.0                  # seconds after a piece ended a mark still goes to it (import)
RATES = (8000, 192000)

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
NO_MOMENT = "No recording was running at that moment (OpenEVP was waiting for sound)."
STOPPED_MOVED = ("Recording stopped because the folder it was saving into is no longer where it was "
                 "in the library. What was recorded until then is saved.")
UNRECOVERED = " (unrecovered).raw"


class _Moved(OSError):
    """The folder a recording goes into is no longer where it was in the library."""

    def __init__(self):
        super().__init__("the folder is no longer where it was in the library")


class _Kept(Exception):
    """live_recover(): a leftover could not be read as audio and was kept under another name."""


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

    def __init__(self, folder, name, rate, channels, start_frame, mode):
        self.folder, self.name, self.start_frame = folder, name, start_frame
        self.part = os.path.join(folder, name + livewav.PART)
        self.sidecar = self.part + ".json"
        self.marks = []
        self.meta = {"version": 1, "name": name, "rate": rate, "channels": channels, "mode": mode,
                     "started": datetime.datetime.now().isoformat(timespec="seconds"), "marks": self.marks}
        _write_sidecar(self.sidecar, self.meta)
        try:
            self.writer = livewav.WavPart(self.part, rate, channels)
        except BaseException:
            _remove(self.sidecar)
            raise

    def save_marks(self):
        _write_sidecar(self.sidecar, self.meta)


class _Session:
    def __init__(self, ops, sid, mode, root, folder, rate, channels, split, pins=None, root_id=None):
        self.ops, self.id, self.mode, self.root, self.folder = ops, sid, mode, root, folder
        self.pins, self.root_id = pins, root_id   # the folders held for the recording, and what the root was
        self.moved = False                       # the folder was found elsewhere before a new file: stopped
        self.rate, self.channels, self.align = rate, channels, channels * livewav.WIDTH
        self.byte_rate = rate * self.align
        self.stamp = _stamp(datetime.datetime.now())
        self.lock = threading.Lock()
        self.seq = 0
        self.frames = 0                          # frames received (the stream)
        self.piece = None
        self.count = 0                           # pieces started
        self.saved = []                          # finished files: {"path", "fp", "frames", "start_frame", "name"}
        self.wholes = []                         # the whole input of an import (at most one), finished
        self.problems = []
        self.dropped_marks = 0
        self.stopped = None                      # why it stopped by itself (a sentence), once it has
        self.full = False                        # the file being written reached livewav.MAX_DATA
        self.done = False
        self.splitter = None
        self.whole = None                        # an import split on silence: the whole input as one file too
        if mode == "import" and split:
            self.byte_rate *= 2                  # two files are written: the pieces and the whole
            self.whole = self._open(f"Import {self.stamp} (full).wav", 0)
            self.splitter = silence.Splitter(rate, channels, self, gap=split)
        else:
            self.start(0)

    # ---- the sink: openevp.silence.Splitter (or Live mode itself) drives these ----
    def contained(self):
        """Is the folder still where it was, inside the same library folder (no link on the way)?"""
        return (self.root_id is None or _root_identity(self.root) == self.root_id) and \
            folders.inside(self.root, self.folder, allow_root=True)

    def _open(self, name, stream_frame):
        if not self.contained():
            raise _Moved()
        piece = _Piece(self.folder, name, self.rate, self.channels, stream_frame, self.mode)
        try:
            self.ops._register_part(piece.part)
        except Exception:
            piece.writer.abort()
            _remove(piece.sidecar)
            raise
        return piece

    def start(self, stream_frame):
        self.count += 1
        name = (f"Live {self.stamp}.wav" if self.mode == "live" else f"Import {self.stamp} ({self.count}).wav")
        if self.done or self.moved:
            return
        try:
            self.piece = self._open(name, stream_frame)
        except _Moved:
            if self.count == 1:
                raise                            # at the start: refused, nothing recorded yet
            self.moved = True                    # the recording stops (live_chunk says why)

    def _put(self, piece, data):
        if self.full or piece is None:
            return
        try:
            piece.writer.write(data)
        except livewav.Full:                     # 4 GB: the rest is dropped and the recording stops
            self.full = True

    def write(self, data):
        self._put(self.piece, data)

    def end(self):
        piece, self.piece = self.piece, None
        if piece is not None:
            self.ops._finish_piece(self, piece)

    def discard(self):
        piece, self.piece = self.piece, None
        if piece is not None:
            piece.writer.abort()
            _remove(piece.sidecar)
            self.ops._unregister_part(piece.part)

    # ---- the stream -------------------------------------------------------------
    def feed(self, data):
        self.frames += len(data) // self.align
        if self.whole is not None:
            self._put(self.whole, data)
        if self.splitter is not None:
            self.splitter.feed(data)
        else:
            self.write(data)

    def finish(self):
        """Stop: finish the files being written. The whole input of an import is kept
        only when it was split into more than one piece (one piece is the same audio).
        Never raises; problems are kept in self.problems."""
        if self.done:
            return
        self.done = True
        try:
            if self.splitter is not None:
                self.splitter.finish()
            if self.piece is not None:
                if self.piece.writer.frames == 0:
                    self.discard()
                else:
                    self.end()
            whole, self.whole = self.whole, None
            if whole is not None:
                if self.count > 1 and whole.writer.frames:
                    self.ops._finish_piece(self, whole, whole=True)
                else:
                    whole.writer.abort()
                    _remove(whole.sidecar)
                    self.ops._unregister_part(whole.part)
        except Exception as e:
            self.problems.append(f"The recording could not be finished: {_plain(e)}. It is saved as "
                                 "a .part file and will be finished when OpenEVP starts again.")
            self.piece = None

    def status(self):
        return {"ok": True, "seconds": round(self.frames / self.rate, 3),
                "piece": self.count if self.piece is not None else None,
                "file": self.piece.name if self.piece is not None else None,
                "saved": len(self.saved), "stopped": self.stopped}


class LiveOps:
    """Live mode and analog import; mixed into app.backend.Api."""

    def _live_init(self):
        self._live_lock = threading.Lock()       # held around every call that touches the session
        self._live = None                        # the _Session recording now
        self._parts_lock = threading.Lock()
        self._live_saved = None                  # Live settings picked in this session (when they could not be remembered)

    # ---- state other parts of the app ask about ----
    def recording(self):
        """True while a Live recording or an import runs (the close prompt asks)."""
        return self._live is not None

    def _live_busy(self):
        """Why a folder operation or an update must wait now (a recording runs), or None."""
        return RECORDING if self._live is not None else None

    # ---- settings ----
    def live_settings(self):
        """The Live view's remembered choices: the input last used ({"id", "label"} or
        None), the silence split in seconds (0 = off) and whether Import was on."""
        saved = self._store.get_setting(LIVE_SETTING) if self._store is not None else None
        saved = self._live_saved or (saved if isinstance(saved, dict) else {})
        split = _split(saved.get("split"))
        return {"ok": True, "input": _clean_input(saved.get("input")),
                "split": silence.DEFAULT_GAP if split is None else split,
                "import": saved.get("import") is True, "max_split": silence.MAX_GAP,
                "reserve_mb": RESERVE_BYTES >> 20}

    def set_live_settings(self, changes):
        if not isinstance(changes, dict) or set(changes) - {"input", "split", "import"}:
            return _fail("Unknown Live settings.")
        current = self.live_settings()
        new = {"input": current["input"], "split": current["split"], "import": current["import"]}
        if "input" in changes:
            new["input"] = _clean_input(changes["input"]) if changes["input"] is not None else None
            if changes["input"] is not None and new["input"] is None:
                return _fail("Unknown input.")
        if "split" in changes:
            if _split(changes["split"]) is None:
                return _fail(f"The silence gap must be between 0.5 and {silence.MAX_GAP:g} seconds.")
            new["split"] = _split(changes["split"])
        if "import" in changes:
            if not isinstance(changes["import"], bool):
                return _fail("Unknown Live settings.")
            new["import"] = changes["import"]
        self._live_saved = new                   # this session's, also when it cannot be remembered
        if self._store is not None:
            try:
                self._store.set_setting(LIVE_SETTING, new)
            except (StoreReadOnly, StoreUnavailable):
                pass
        return {"ok": True, **self.live_settings()}

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
            if free < RESERVE_BYTES + 60 * rate * channels * livewav.WIDTH * (2 if mode == "import" and split else 1):
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
        return {"ok": True, "session": session.id, "file": session.piece.name if session.piece else None,
                "folder": os.path.basename(os.path.normpath(folder)), "rate": rate, "channels": channels}

    def _session(self, sid):
        s = self._live
        return s if s is not None and isinstance(sid, str) and s.id == sid else None

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
            if s.moved:
                return self._live_end(s, STOPPED_MOVED)
            out = s.status()
            left = None
            if free is not None:
                left = (free - RESERVE_BYTES) / s.byte_rate
            biggest = s.whole or s.piece          # the file nearest 4 GB (an import's whole input)
            if biggest is not None:
                room = biggest.writer.room() / (s.rate * s.align)
                left = room if left is None else min(left, room)
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
        MARK_SECONDS region ending there, in the file being written (or, in an import,
        the one that ended less than LATE_MARK seconds before)."""
        if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at or at < 0:
            return _fail("Unknown moment.")
        with self._live_lock:
            s = self._session(sid)
            if s is None:
                return _fail(NOT_RECORDING)
            now = s.frames / s.rate
            at = min(float(at), now + MARK_SLACK)
            piece = s.piece
            if piece is not None and at >= piece.start_frame / s.rate:
                rel = at - piece.start_frame / s.rate
                mark = {"start": round(max(0.0, rel - MARK_SECONDS), 3), "end": round(rel, 3),
                        "cls": MARK_CLASS, "note": MARK_NOTE}
                if mark["end"] - mark["start"] < MIN_MARK_LENGTH:
                    mark["end"] = round(mark["start"] + MIN_MARK_LENGTH, 3)
                piece.marks.append(mark)
                try:
                    piece.save_marks()
                except OSError:
                    pass                         # kept in memory: stored when the file is finished
                if s.whole is not None:          # the whole input has it at its own time (it starts at 0)
                    s.whole.marks.append({**mark, "start": round(max(0.0, at - MARK_SECONDS), 3), "end": round(at, 3)})
                    try:
                        s.whole.save_marks()
                    except OSError:
                        pass
                return {"ok": True, "mark": {"at": at, "file": piece.name, **mark}}
            last = s.saved[-1] if s.saved else None
            if last is not None:
                end_at = (last["start_frame"] + last["frames"]) / s.rate
                if at <= end_at + LATE_MARK:
                    rel = min(at, end_at) - last["start_frame"] / s.rate
                    mark = _clamped({"start": max(0.0, rel - MARK_SECONDS), "end": rel, "cls": MARK_CLASS,
                                     "note": MARK_NOTE}, last["frames"] / s.rate)
                    if mark is not None:
                        try:
                            self._store.add_mark(last["fp"], mark["start"], mark["end"], mark["cls"], mark["note"],
                                                 name=os.path.basename(last["path"]), duration=last["frames"] / s.rate)
                            last["marks"] += 1
                            return {"ok": True, "mark": {"at": at, "file": os.path.basename(last["path"]), **mark}}
                        except (StoreReadOnly, StoreUnavailable, ValueError) as e:
                            return _fail(str(e))
            return _fail(NO_MOMENT)

    def live_stop(self, sid):
        """Stop and save: {"ok", "files": [{"id", "name", "seconds", "marks"}], "folder",
        "problems", "dropped_marks", "player" (Live mode: the recording, loaded for the
        player as play_library() would)}."""
        with self._live_lock:
            s = self._session(sid)
            if s is None:
                return _fail(NOT_RECORDING)
            return self._live_finish(s)

    def _live_finish(self, s):
        """Finish a session (under _live_lock) and describe what was saved."""
        try:
            s.finish()
        finally:
            if s.pins is not None:
                s.pins.close()
        if self._live is s:
            self._live = None
        def row(f):
            return {"id": _file_id(f["path"]), "name": os.path.basename(f["path"]),
                    "seconds": round(f["frames"] / s.rate, 1), "marks": f["marks"]}
        files = [row(f) for f in s.saved]
        out = {"ok": True, "files": files, "folder": os.path.basename(os.path.normpath(s.folder)),
               "problems": list(s.problems), "dropped_marks": s.dropped_marks, "mode": s.mode,
               "whole": row(s.wholes[0]) if s.wholes else None}
        if s.mode == "live" and s.saved and not self._stop.is_set():
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

    def _finish_piece(self, s, piece, whole=False):
        """A file is complete: rename it, store its marks and its fingerprint (never raises).
        whole: an import's whole input (listed in "whole", not among the pieces)."""
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
        stored = self._store_live_marks(fp, path, seconds, piece.marks, s)
        _remove(piece.sidecar)
        self._unregister_part(piece.part)
        self._index_live(path, fp, seconds)
        (s.wholes if whole else s.saved).append({"path": path, "fp": fp, "frames": frames,
                                                 "start_frame": piece.start_frame, "marks": stored})

    def _store_live_marks(self, fp, path, seconds, marks, s=None):
        """Store marks made while recording against the finished file. A mark already
        there (the same times, class and note: stored before a crash) is not added
        again, so this can be run twice."""
        stored = 0
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
            except (StoreReadOnly, StoreUnavailable, ValueError):
                if s is not None:
                    s.dropped_marks += 1
        return stored

    def _index_live(self, path, fp, seconds):
        """Put a finished file's fingerprint in the library's index: listing it never reads it again."""
        try:
            st = os.stat(path)
            self._store.remember_fp(path, st.st_size, st.st_mtime_ns, fp, round(seconds, 1))
            self._store.flush_index()
        except (OSError, StoreReadOnly, StoreUnavailable):
            pass

    def _live_shutdown(self):
        """The app is closing: finish the recording running (its file is saved)."""
        with self._live_lock:
            if self._live is not None:
                self._live_finish(self._live)

    # ---- crash recovery ----
    def live_recover(self):
        """Finish the .part files a crash left behind (once the store is writable):
        {"ok", "recovered": [{"name", "folder", "seconds", "marks"}], "failed": [text]}."""
        out = {"ok": True, "recovered": [], "failed": []}
        store = self._store
        if store is None or store.read_only:
            return out
        with self._live_lock:
            active = set()
            if self._live is not None:
                for piece in (self._live.piece, self._live.whole):
                    if piece is not None:
                        active.add(os.path.normcase(piece.part))
            for part in self._parts():
                if os.path.normcase(part) in active:
                    continue
                try:
                    got = self._recover_part(part)
                except Exception as e:
                    got = None
                    out["failed"].append(f"{os.path.basename(part)}: {_plain(e)}")
                    # left in the list only while the file is there to try again
                    if os.path.exists(part):
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
        stored = self._store_live_marks(fp, target, seconds, _sidecar_marks(meta))
        _remove(sidecar)
        self._index_live(target, fp, seconds)
        return {"name": os.path.basename(target), "folder": os.path.basename(os.path.dirname(target)),
                "seconds": round(seconds, 1), "marks": stored, "id": _file_id(target)}

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
            return self._recover_published(part, sidecar, meta)
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
        stored = self._store_live_marks(fp, path, seconds, _sidecar_marks(meta))
        _remove(sidecar)
        self._index_live(path, fp, seconds)
        return {"name": os.path.basename(path), "folder": os.path.basename(folder), "seconds": round(seconds, 1),
                "marks": stored, "id": _file_id(path)}
