"""Library folder operations of the Api (a mixin): create, rename, delete (to
the Recycle Bin only), and move and rename recordings, always inside the
library folder.

Each operation holds the Api's _busy lock (no export, WAV with marks or update
meanwhile), refuses while a backup is queued or written or when another
OpenEVP window holds the store, refuses when the library folder itself now
resolves elsewhere than at the last listing, and pauses the library indexer
(_fs_op) before it touches a file. Everything that remembers a
path -- the fingerprint cache, rec handles, files served in place -- is
re-keyed to the new place, so marks follow and nothing is decoded again.

The small helpers shared with app/backend.py (plain errors, _fail, opaque ids)
live here so that backend.py can import them without a cycle.
"""
import errno
import hashlib
import os
from collections import OrderedDict

from openevp import formats

from . import folders
from .devices import READY
from .store import StoreReadOnly, StoreUnavailable

HANDLES = 32                 # recording handles (and captured .dvf bytes) kept at once
SESSION_CACHE = 20000       # fingerprints kept in memory while the store is read-only
CLOSING = "The app is closing."
FS_BUSY = "Wait for the export or backup to finish."
LIB_CHANGED = "The library changed. Refresh and try again."
INDEXER_BUSY = "The library is busy checking a recording. Try again in a moment."
PATH_TOO_LONG = "The folder path would be too long for Windows. Choose a shorter name."
BACKUP_RECYCLED = "The backup was moved to the Recycle Bin."
BACKUP_UNCHECKED = ("The backup could not be checked before a library folder was deleted "
                    "(it may have been in it); back it up again.")
ROOT_CHANGED = "The library folder changed. Refresh and try again."
CLIPS = "Clips"             # the subfolder EVP clips go into
CLIPS_MARKER = ".openevp-clips"   # in a Clips folder OpenEVP created: its files are clips, not recordings
CLIPS_MARKER_TEXT = (b"OpenEVP made this folder for EVP clips. The EVP Library lists these clips but never "
                     b"counts them as EVPs while this file is here; delete this file to treat them as "
                     b"recordings.\r\n")
CLIPS_ONLY = "A Clips folder is for EVP clips only. Move recordings to another folder."
CLIPS_AGAIN = "That is an EVP clip (or a folder of them); clips are not cut from clips."
FS_WAIT = 10                # seconds a folder operation waits for the indexer to pause
RENAME_TRIES = 4            # os.rename attempts when a file is briefly in use (antivirus, indexing)
RENAME_PAUSE = 0.33         # seconds between them (about 1 s in all)
_SHARING = (5, 32, 33)      # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION


def _plain(e):
    """An error in plain words that names a file, never its full path."""
    if isinstance(e, OSError) and e.filename:
        return f"{e.strerror or type(e).__name__} ({os.path.basename(e.filename)})"
    return str(e) or type(e).__name__


def _clips_folder(path):
    """Is a folder a Clips folder OpenEVP created (it holds CLIPS_MARKER)? Its files
    (and those of every folder inside it) are clips: the library lists them and
    they play, but they never count as EVPs, their markers are never imported and
    Export clips never cuts them again. A folder the user named Clips is an
    ordinary folder."""
    try:
        return os.path.isfile(os.path.join(path, CLIPS_MARKER))
    except (OSError, ValueError):
        return False


def _under_clips(path, root=None):
    """Is a folder a Clips folder OpenEVP made, or inside one? Looks at path and
    its parents up to the library folder root (never root itself, as the listing
    never counts the library folder as a Clips folder) or the drive's root."""
    try:
        path = os.path.abspath(path)
        stop = os.path.normcase(os.path.abspath(root)) if root else None
    except (OSError, ValueError):
        return False
    while True:
        if stop is not None and os.path.normcase(path) == stop:
            return False
        if _clips_folder(path):
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def _is_clip(path, root=None):
    """Is a file a clip (in a Clips folder OpenEVP made, at any depth)?"""
    try:
        return _under_clips(os.path.dirname(os.path.abspath(path)), root)
    except (OSError, ValueError):
        return False


def _make_clips_folder(path):
    """Create a Clips folder for clips; when this creates it, mark it as OpenEVP's
    (a hidden CLIPS_MARKER file). A folder that was already there is left as it is."""
    if os.path.isdir(path):
        return
    os.makedirs(path, exist_ok=True)
    marker = os.path.join(path, CLIPS_MARKER)
    try:
        with open(marker, "xb") as f:
            f.write(CLIPS_MARKER_TEXT)
    except FileExistsError:
        return
    folders.hide(marker)


def _kind_format(kind):
    """The Format of a library file kind ("dvf", "wav": its extension without the dot)."""
    return formats.by_ext("." + kind)


def _decoder_problem(fmt):
    """Why fmt cannot be decoded now (a phrase), or None when it can."""
    return formats.decoder_problem(fmt)


def _decoder_problems(kinds):
    """{kind: why its files cannot be decoded now} for the library file kinds
    given ("dvf", "fk2"...; WAV never has a problem), checked once per job."""
    out = {}
    for kind in set(kinds):
        fmt = _kind_format(kind)
        if fmt is not None and fmt is not formats.WAV:
            problem = _decoder_problem(fmt)
            if problem:
                out[kind] = problem
    return out


def _fs_problem(e):
    """Why a file or folder could not be moved or renamed, in plain words."""
    win = getattr(e, "winerror", None)
    if win == 17 or getattr(e, "errno", None) == errno.EXDEV:
        return "it is on another drive, so it was not moved"
    if win in (5, 32, 33) or isinstance(e, PermissionError):
        return "it is open in another program (close it and try again)"
    if isinstance(e, FileNotFoundError):
        return "it is no longer there (refresh the list)"
    return _plain(e)


def _bounded_put(cache, key, value, limit=HANDLES):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _fail(message, advice="", state=READY):
    return {"ok": False, "error": message, "advice": advice, "state": state}


def _file_id(path):
    """The opaque id the page uses for a file on disk (never its path)."""
    return hashlib.sha1(os.path.normcase(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _folder_id(rel_parts):
    """The opaque id the page uses for a library folder (never its path):
    "root" for the library folder itself, else the first 16 hex of the sha1 of
    the normcased relative path (its parts joined by os.sep)."""
    if not rel_parts:
        return "root"
    return hashlib.sha1(os.path.normcase(os.sep.join(rel_parts))
                        .encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _root_identity(root):
    """(where the library folder really is, is it itself a link) -- or None when
    it cannot be resolved. Recorded by each listing, checked by each operation."""
    try:
        return os.path.normcase(os.path.realpath(root)), folders.is_link(root)
    except (OSError, ValueError):
        return None


class LibraryOps:
    """Folder operations; mixed into app.backend.Api (uses its locks, store and caches)."""

    # ---- library folders: create, rename, delete (Recycle Bin), move recordings ----
    def _lib_paths(self, folder_id):
        """(library root, the folder's path) from the latest listing, or None when
        either is unknown or the library folder has changed since."""
        if not isinstance(folder_id, str):
            return None
        with self._lib_lock:
            root = self._library_folders.get("root")
            path = self._library_folders.get(folder_id)
        if root is None or path is None:
            return None
        if os.path.normcase(os.path.abspath(root)) != os.path.normcase(os.path.abspath(self._library_path())):
            return None
        return root, path

    def _root_moved(self, root):
        """Is the library folder no longer what the latest listing saw: does it
        resolve somewhere else now, or has it become a symlink or junction? (A
        library folder swapped for a junction would otherwise carry every
        folder id to another place on disk.)"""
        with self._lib_lock:
            seen = self._library_root
        return seen is None or _root_identity(root) != seen

    def _usable(self, root, path, allow_root=False, pins=None):
        """None when path is still a folder inside the unchanged library folder,
        else the _fail() saying why not. With pins (folders.Pins), the library
        folder and every folder down to path are held first, so that what is
        checked here stays true until the operation lets go of them."""
        if pins is not None:
            try:
                pins.chain(root, path)
            except (OSError, ValueError):
                return _fail(LIB_CHANGED)
        if self._root_moved(root):
            return _fail(ROOT_CHANGED)
        if not os.path.isdir(path) or not folders.inside(root, path, allow_root=allow_root):
            return _fail(LIB_CHANGED)
        return None

    def _rename(self, src, dst):
        """os.rename, tried again for about a second while a file is briefly in use
        (an antivirus scan, the search indexer, a player letting go)."""
        for attempt in range(RENAME_TRIES):
            try:
                return os.rename(src, dst)
            except OSError as e:
                if getattr(e, "winerror", None) not in _SHARING or attempt == RENAME_TRIES - 1:
                    raise
            self._stop.wait(RENAME_PAUSE)

    def _store_read_only(self):
        """Why the store is read-only, in its own words (another OpenEVP holds it, or
        its lock file could not be opened), or None: writable, or no store at all."""
        return self._store.read_only_reason if self._store is not None else None

    def _fs_begin(self, pause=True):
        """Start a folder operation: None, or the _fail() saying why not. Holds
        _busy until _fs_end(). With pause, the indexer is stopped between files
        first (and no new indexer job or backup starts until _fs_end())."""
        if self._stop.is_set():
            return _fail(CLOSING)
        read_only = self._store_read_only()
        if read_only:
            # Another window holds the store: it could be exporting or backing up
            # into these folders, and backup states could not be recorded here.
            return _fail(read_only)
        if not self._busy.acquire(blocking=False):
            return _fail(FS_BUSY)
        self._fs_busy = True                # exporting() is not this
        indexer = None
        if pause:
            with self._lib_lock:
                self._fs_op = True
                self._fs_gen += 1
                self._lib_job = None
                indexer = self._indexer
        if self.backing_up():               # after _fs_op is set: the backup worker starts none now
            self._fs_end()
            return _fail(FS_BUSY)
        if indexer is not None:
            indexer.join(FS_WAIT)
            if indexer.is_alive():
                self._fs_end()
                return _fail(INDEXER_BUSY)
        return None

    def _fs_end(self):
        with self._lib_lock:
            if self._fs_op:
                self._fs_op = False
                self._fs_gen += 1
        self._fs_busy = False
        self._busy.release()

    def _retarget_prefix(self, old, new):
        """A file or folder moved from old to new: re-key the fingerprint cache
        (marks follow, nothing is decoded again), the read-only session cache, rec
        handles and the files the audio server serves in place. Decoded .dvf
        entries are left alone (they decode again when next played). Backup
        paths are not here: _move_backups() records them before the rename."""
        old, new = os.path.abspath(old), os.path.abspath(new)
        store = self._store
        copied = {}
        if store is not None:
            try:
                store.move_index_prefix(old, new)
            except (StoreReadOnly, StoreUnavailable):
                # A read-only store keeps its entries at the old place: this
                # session remembers them at the new one.
                copied = store.index_under(old)
        with self._lib_lock:
            for key in [k for k in self._fp_session if folders.under(k, old)]:
                self._fp_session[os.path.normcase(folders.rebase(key, old, new))] = self._fp_session.pop(key)
            for key, e in copied.items():
                _bounded_put(self._fp_session, os.path.normcase(folders.rebase(key, old, new)),
                             (e["size"], e["mtime_ns"], e), SESSION_CACHE)
        old_name, new_name = os.path.basename(old), os.path.basename(new)
        with self._recs_lock:
            for entry in self._recs.values():
                src = entry["source"]
                if src.get("kind") != "file" or not folders.under(src["path"], old):
                    continue
                exact = os.path.normcase(os.path.abspath(src["path"])) == os.path.normcase(old)
                src["path"] = folders.rebase(src["path"], old, new)
                if exact and old_name != new_name:          # a file that got a "(N)" name
                    for field in ("name", "label"):
                        if entry.get(field) == old_name:
                            entry[field] = new_name
        retarget = getattr(self._server, "retarget_prefix", None)
        if retarget is not None:
            retarget(old, new)

    def _move_backups(self, old, new):
        """Record that the backup files at or under old are about to be at new --
        before they move, so that a backup is never lost track of. Raises
        StoreReadOnly/StoreUnavailable when that cannot be saved (the caller then
        does not move anything). Returns an undo function (None: nothing to undo)."""
        store = self._store
        if store is None:
            return None
        before = store.move_backup_paths(old, new)
        if not before:
            return None
        return lambda: store.restore_backup_paths(before)     # exactly those records, never by prefix

    def _walk(self, path):
        """A fresh look at everything in a folder (never through a symlink or
        junction): counts, and the cached fingerprints of its recordings. The
        files of a Clips folder OpenEVP made (path itself, one inside it, or one it
        is in) count as clips: never recordings, never read or fingerprinted."""
        info = {"recordings": 0, "with_evps": 0, "unindexed": 0, "other_files": 0, "subfolders": 0,
                "bytes": 0, "fps": set(), "pending": [], "unknown": 0, "clips": 0}
        stack = [(path, _under_clips(path, self._library_path()))]
        while stack:
            where, in_clips = stack.pop()
            try:
                with os.scandir(where) as it:
                    entries = list(it)
            except OSError:
                info["unindexed"] += 1          # unreadable: whatever is in it is unknown
                info["unknown"] += 1
                continue
            for e in entries:
                try:
                    if folders.entry_is_link(e):
                        info["other_files"] += 1
                        continue
                    if e.is_dir():
                        info["subfolders"] += 1
                        stack.append((e.path, in_clips or _clips_folder(e.path)))   # clips go along
                        continue
                    st = e.stat()
                except OSError:
                    continue
                info["bytes"] += st.st_size
                kind = os.path.splitext(e.name)[1].lower()[1:]
                fmt = _kind_format(kind) if kind else None
                if e.name.startswith(".") or fmt is None:
                    info["other_files"] += 1
                    continue
                if in_clips:                    # a clip: not a recording, never read
                    info["clips"] += 1
                    continue
                info["recordings"] += 1
                if fmt.decoder is None:         # never fingerprinted, so never a marked recording's backup
                    continue
                cached = self._cached_fp(e.path, st.st_size, st.st_mtime_ns)
                fp = cached.get("fp") if cached else None
                if cached is None:
                    info["unindexed"] += 1
                    info["pending"].append((e.path, kind, st.st_size, st.st_mtime_ns))
                elif not fp:
                    info["unknown"] += 1        # could not be fingerprinted (damaged, unreadable)
                if fp:
                    info["fps"].add(fp)
                    r = self._store.recording(fp)
                    if r is not None and r["marks"]:
                        info["with_evps"] += 1
        return info

    def _backups_in(self, path, walked, fingerprint=False):
        """{fp: (its backup record, its backup files found in path, the detail to
        record)} for every recorder backup recorded as saved that deleting path
        may take: one of the files it saved (recorded with the backup) is in
        path, or a recording in path has its fingerprint (the union: counting
        one too many only offers a needless Retry backup). walked: _walk(path).

        With fingerprint (a delete), recordings in path not fingerprinted yet are
        fingerprinted first; if some recording there still has no fingerprint,
        every saved backup whose files are not all where they were recorded
        might be it, and is included as unchecked. None if the app closes
        meanwhile."""
        if self._store is None:
            return {}
        saved = self._store.saved_backups()
        if not saved:
            return {}
        unknown = walked["unknown"] + (len(walked["pending"]) if not fingerprint else 0)
        if fingerprint and walked["pending"]:
            fps, failed = self._fingerprint(walked["pending"])
            if fps is None:
                return None
            walked["fps"] |= fps
            unknown += failed
        found = {}
        for fp, paths in saved.items():
            there = [p for p in paths if folders.under(p, path) and os.path.lexists(p)]
            if there or fp in walked["fps"]:
                found[fp] = (there, BACKUP_RECYCLED)
            elif fingerprint and unknown and not (paths and all(os.path.lexists(p) for p in paths)):
                found[fp] = ([], BACKUP_UNCHECKED)
        return {fp: (self._store.backup_record(fp), there, detail) for fp, (there, detail) in found.items()}

    def _fingerprint(self, pending):
        """(fingerprints, how many could not be fingerprinted) of recordings not in
        the index yet ([(path, kind, size, mtime_ns)] from _walk), cached as the
        indexer would; (None, n) when the app started closing meanwhile."""
        problems = _decoder_problems(kind for _, kind, _, _ in pending)
        fps, failed = set(), 0
        try:
            for path, kind, size, mtime_ns in pending:
                if self._stop.is_set():
                    return None, failed
                row, _stored = self._index_file(path, kind, size, mtime_ns, self._stop.is_set, problems.get(kind))
                if row is None:                     # cancelled: the app is closing
                    return None, failed
                if row.get("fp"):
                    fps.add(row["fp"])
                else:
                    failed += 1
        finally:
            self._flush_index()
        return fps, failed

    @staticmethod
    def _rel(root, path):
        return tuple(os.path.relpath(os.path.abspath(path), os.path.abspath(root)).split(os.sep))

    def create_folder(self, parent_id, name):
        """Create a folder in a library folder. {"ok", "id"}."""
        try:
            return self._create_folder(parent_id, name)
        except Exception as e:
            return _fail(f"Could not create the folder: {_plain(e)}")

    def _create_folder(self, parent_id, name):
        name, problem = folders.clean_name(name)
        if problem:
            return _fail(problem)
        found = self._lib_paths(parent_id)
        if found is None:
            return _fail(LIB_CHANGED)
        root, parent = found
        refused = self._fs_begin(pause=False)
        if refused:
            return refused
        pins = folders.Pins()
        try:
            refused = self._usable(root, parent, allow_root=True, pins=pins)
            if refused:
                return refused
            target = os.path.join(parent, name)
            if folders.too_long(target):
                return _fail(PATH_TOO_LONG)
            if folders.name_taken(parent, name):
                return _fail(f'There is already something named "{name}" here.')
            try:
                os.mkdir(target)
            except FileExistsError:
                return _fail(f'There is already something named "{name}" here.')
            except OSError as e:
                return _fail(f"Could not create {name}: {_plain(e)}")
            return {"ok": True, "id": _folder_id(self._rel(root, target))}
        finally:
            pins.close()
            self._fs_end()

    def rename_folder(self, folder_id, name):
        """Rename a library folder (a change of case only is allowed). {"ok", "id"}:
        the folder's new id (its subfolders' ids change too; the page relists)."""
        try:
            return self._rename_folder(folder_id, name)
        except Exception as e:
            return _fail(f"Could not rename the folder: {_plain(e)}")

    def _rename_folder(self, folder_id, name):
        if folder_id == "root":
            return _fail("The library folder itself cannot be renamed here.")
        name, problem = folders.clean_name(name)
        if problem:
            return _fail(problem)
        found = self._lib_paths(folder_id)
        if found is None:
            return _fail(LIB_CHANGED)
        root, old = found
        refused = self._fs_begin()
        if refused:
            return refused
        pins = folders.Pins()
        try:
            # Held down to its parent: the folder itself must stay free to be renamed.
            refused = self._usable(root, os.path.dirname(os.path.abspath(old)), allow_root=True, pins=pins)
            refused = refused or self._usable(root, old)
            if refused:
                return refused
            parent, old_name = os.path.split(os.path.abspath(old))
            if name == old_name:
                return {"ok": True, "id": folder_id}
            target = os.path.join(parent, name)
            longest = self._longest_path(old)
            if len(longest) + len(os.path.abspath(target)) - len(os.path.abspath(old)) >= folders.MAX_PATH_CHARS:
                return _fail(PATH_TOO_LONG)
            if folders.name_taken(parent, name, ignore=old_name):
                return _fail(f'There is already something named "{name}" here.')
            try:
                undo = self._move_backups(old, target)
            except (StoreReadOnly, StoreUnavailable) as e:
                return _fail(f"{old_name} was not renamed: the recorder backups in it could not be "
                             f"recorded at their new place ({e}).")
            try:
                self._rename(old, target)
            except OSError as e:
                if undo is not None:
                    try:
                        undo()
                    except (StoreReadOnly, StoreUnavailable):
                        pass                        # a stale path: found by fingerprint all the same
                if isinstance(e, FileExistsError):
                    return _fail(f'There is already something named "{name}" here.')
                return _fail(f"{old_name} was not renamed: {_fs_problem(e)}.")
            try:                                    # renamed: nothing below may report otherwise
                self._retarget_prefix(old, target)
            except Exception:
                pass                                # its recordings are fingerprinted again
            self._follow_save_folder(old, target)
            self._flush_index()
            return {"ok": True, "id": _folder_id(self._rel(root, target))}
        finally:
            pins.close()
            self._fs_end()

    @staticmethod
    def _longest_path(path):
        """The longest absolute path of path itself or anything in it (links not followed)."""
        longest = os.path.abspath(path)
        stack = [longest]
        while stack:
            try:
                with os.scandir(stack.pop()) as it:
                    entries = list(it)
            except OSError:
                continue
            for e in entries:
                if len(e.path) > len(longest):
                    longest = e.path
                try:
                    if not folders.entry_is_link(e) and e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                except OSError:
                    continue
        return longest

    def _follow_save_folder(self, old, new):
        """The Save-to folder was old or inside it: it is now under new (remembered
        for the next start when the store can be written)."""
        if not self._dest or not folders.under(self._dest, old):
            return
        self._dest = folders.rebase(self._dest, old, new)
        if self._store is not None:
            try:
                self._store.set_setting("save_folder", self._dest)
            except (StoreReadOnly, StoreUnavailable):
                pass                                # used for this session; not remembered

    def folder_info(self, folder_id):
        """What deleting a folder would put in the Recycle Bin, counted fresh from
        disk: {"ok", "name", "recordings", "with_evps", "clips" (files in Clips folders
        OpenEVP made: never counted as recordings), "evps_at_least" (some
        recordings are not checked yet, so there may be more), "backups" (marked
        recorder recordings whose backup files are in it), "other_files",
        "subfolders", "bytes", "save_folder" (it is, or holds, the Save-to folder:
        the next export or backup creates that folder again)}."""
        try:
            found = self._lib_paths(folder_id)
            if found is None:
                return _fail(LIB_CHANGED)
            root, path = found
            refused = self._usable(root, path, allow_root=folder_id == "root")
            if refused:
                return refused
            info = self._walk(path)
            backups = len(self._backups_in(path, info))
            return {"ok": True, "name": os.path.basename(os.path.normpath(path)),
                    "recordings": info["recordings"], "with_evps": info["with_evps"], "clips": info["clips"],
                    "evps_at_least": info["unindexed"] > 0, "backups": backups,
                    "other_files": info["other_files"], "subfolders": info["subfolders"], "bytes": info["bytes"],
                    "save_folder": bool(self._dest) and folders.under(self._dest, path)}
        except Exception as e:
            return _fail(f"Could not look into the folder: {_plain(e)}")

    def delete_folder(self, folder_id):
        """Move a library folder to the Recycle Bin (never deleted for good). {"ok",
        "backups"}: marked recorder recordings whose backup went with it are marked
        as not backed up, so Retry backup appears."""
        try:
            return self._delete_folder(folder_id)
        except Exception as e:
            return _fail(f"The folder was not deleted: {_plain(e)}")

    def _delete_folder(self, folder_id):
        if folder_id == "root":
            return _fail("The library folder itself cannot be deleted here.")
        found = self._lib_paths(folder_id)
        if found is None:
            return _fail(LIB_CHANGED)
        root, path = found
        refused = self._fs_begin()
        if refused:
            return refused
        pins = folders.Pins()
        try:
            refused = self._usable(root, path, pins=pins)
            if refused:
                return refused
            name = os.path.basename(os.path.normpath(path))
            candidates = self._backups_in(path, self._walk(path), fingerprint=True)
            if candidates is None:
                return _fail(CLOSING)
            # The backups going with the folder show as not backed up (Retry
            # backup) -- recorded before anything is deleted: if that cannot be
            # saved, nothing is deleted.
            if candidates:
                try:
                    self._store.set_backups({fp: {"status": "failed", "detail": detail}
                                             for fp, (_, _, detail) in candidates.items()})
                except (StoreReadOnly, StoreUnavailable, ValueError) as e:
                    return _fail(f"{name} was not deleted: the recorder backups in it could not be "
                                 f"marked as not backed up ({e}).")
            error = None
            try:
                # The folder stays held until just before the shell moves it (a
                # held folder cannot be recycled); the library folder stays held.
                self._recycle(os.path.abspath(path), before=lambda: pins.release(path))
            except folders.RecycleError as e:
                error = str(e)
            except Exception as e:
                error = f"{name} was not moved to the Recycle Bin: {_plain(e)}"
            lost = set(candidates)
            try:
                lost -= self._backups_left(path, candidates)
            except Exception:
                pass                                # kept as not backed up: Retry backup is harmless
            if error:
                return {**_fail(error), "backups": len(lost)}
            return {"ok": True, "backups": len(lost)}
        finally:
            pins.close()
            self._fs_end()

    def _backups_left(self, path, candidates):
        """After a delete: the candidates (see _backups_in) whose backup is still
        there for sure (a file in use, a cancelled delete) -- its recorded files
        in the folder all remain, or a recording with its fingerprint remains in
        the folder -- recorded as saved again. Unchecked ones stay failed."""
        left_fps = self._walk(path)["fps"] if os.path.lexists(path) else set()
        kept = {}
        for fp, (record, there, detail) in candidates.items():
            if detail != BACKUP_RECYCLED:
                continue
            if (there and all(os.path.lexists(p) for p in there)) or (not there and fp in left_fps):
                kept[fp] = record
        if kept:
            self._store.set_backups(kept)
        return set(kept)

    def move_files(self, file_ids, folder_id):
        """Move library files (by id) into a library folder. A name already taken
        there gets the next free "<stem> (N)<ext>" (one N shared by the files with
        the same stem, so a .dvf and its .wav stay together); nothing is
        overwritten. Marks follow (same fingerprint). {"ok", "moved", "skipped"
        (already there), "renamed": [{"from", "to"}], "failed": [{"name", "error"}],
        "ids": {old file id: new file id}}. When nothing moved and something
        failed, ok is False with a summary "error" (the detail fields stay)."""
        try:
            return self._move_files(file_ids, folder_id)
        except Exception as e:
            return _fail(f"The recordings were not moved: {_plain(e)}")

    def _move_files(self, file_ids, folder_id):
        if not isinstance(file_ids, list) or not file_ids or not all(isinstance(i, str) for i in file_ids):
            return _fail("Nothing valid is selected.")
        found = self._lib_paths(folder_id)
        if found is None:
            return _fail(LIB_CHANGED)
        root, target = found
        paths = OrderedDict()
        for fid in file_ids:
            path = self._library_file(fid)
            if path is None:
                return _fail(LIB_CHANGED)
            paths[fid] = path
        refused = self._fs_begin()
        if refused:
            return refused
        pins = folders.Pins()
        try:
            refused = self._usable(root, target, allow_root=True, pins=pins)
            if refused:
                return refused
            # A Clips folder holds clips only: a recording moved in would stop counting
            # (and its markers would never be imported). Clips may move between them.
            if _under_clips(target, root) and not all(_is_clip(p, root) for p in paths.values()):
                return _fail(CLIPS_ONLY)
            for path in paths.values():             # the folders the files come from
                try:
                    pins.chain(root, os.path.dirname(os.path.abspath(path)))
                except (OSError, ValueError):
                    pass                            # gone: the file is reported below
            result = {"ok": True, "moved": 0, "skipped": 0, "renamed": [], "failed": [], "ids": {}}
            groups = OrderedDict()                  # stem -> [(file id, path)]
            here = os.path.normcase(os.path.abspath(target))
            for fid, path in paths.items():
                name = os.path.basename(path)
                if os.path.normcase(os.path.dirname(os.path.abspath(path))) == here:
                    result["skipped"] += 1
                    continue
                if not os.path.isfile(path) or not folders.inside(root, path):
                    result["failed"].append({"name": name, "error": "It is no longer there. Refresh the list."})
                    continue
                groups.setdefault(os.path.splitext(name)[0].casefold(), []).append((fid, path))
            try:
                for group in groups.values():
                    try:
                        self._move_group(group, target, result)
                    except Exception as e:          # the files of this group not moved yet
                        for fid, path in group:
                            if fid not in result["ids"] and not any(
                                    f["name"] == os.path.basename(path) for f in result["failed"]):
                                result["failed"].append({"name": os.path.basename(path),
                                                         "error": f"It was not moved: {_plain(e)}."})
            finally:
                if result["moved"]:
                    self._flush_index()
            if not result["moved"] and result["failed"]:
                first = result["failed"][0]
                summary = (f"{first['name']} was not moved. {first['error']}" if len(result["failed"]) == 1
                           else f"None of the {len(result['failed'])} files were moved. "
                                f"{first['name']}: {first['error']}")
                result.update(_fail(summary))
            return result
        finally:
            pins.close()
            self._fs_end()

    def _move_group(self, group, target, result):
        """Move files sharing one stem into target, with one "(N)" for all of them
        when any of their names is taken there."""
        with os.scandir(target) as it:
            taken = {e.name.casefold() for e in it}

        def named(name, n):
            stem, ext = os.path.splitext(name)
            return name if n is None else f"{stem} ({n}){ext}"

        def next_free(names, n):
            """n, or the lowest N after it, free for every name."""
            while not all(named(x, n).casefold() not in taken for x in names):
                n = 2 if n is None else n + 1
            return n
        shared = next_free([os.path.basename(p) for _, p in group], None)
        for fid, path in group:
            name = os.path.basename(path)
            n = shared
            for _attempt in range(5):               # another program may take the name first
                n = next_free([name], n)
                new_name = named(name, n)
                dest = os.path.join(target, new_name)
                if folders.too_long(dest):
                    result["failed"].append({"name": name, "error": "The new path would be too long for Windows."})
                    break
                if os.path.lexists(dest):           # os.rename replaces a file outside Windows
                    taken.add(new_name.casefold())
                    continue
                try:
                    undo = self._move_backups(path, dest)
                except (StoreReadOnly, StoreUnavailable) as e:
                    result["failed"].append({"name": name, "error": f"Its recorder backup could not be "
                                                                    f"recorded at the new place ({e})."})
                    break
                try:
                    self._rename(path, dest)
                except OSError as e:
                    if undo is not None:
                        try:
                            undo()
                        except (StoreReadOnly, StoreUnavailable):
                            pass                    # a stale path: found by fingerprint all the same
                    if isinstance(e, FileExistsError):
                        taken.add(new_name.casefold())
                        continue
                    problem = _fs_problem(e)
                    result["failed"].append({"name": name, "error": problem[:1].upper() + problem[1:] + "."})
                    break
                taken.add(new_name.casefold())
                try:
                    self._retarget_prefix(path, dest)
                except Exception:
                    pass                            # moved all the same; fingerprinted again later
                result["moved"] += 1
                result["ids"][fid] = _file_id(dest)
                if new_name != name:
                    result["renamed"].append({"from": name, "to": new_name})
                break
            else:
                result["failed"].append({"name": name, "error": "No free name was found for it."})

    def rename_files(self, file_ids, new_stem):
        """Give library files (by id: one recording's files, all in one folder) the
        name new_stem, each keeping its own extension (a .dvf and its .wav stay
        together). Nothing is overwritten: a name taken there (ignoring case) is
        refused before anything is renamed, except a change of case of the file's
        own name; if a later file cannot be renamed, the ones already renamed are
        named back. Marks follow (same fingerprint). {"ok", "renamed": [{"from",
        "to"}], "ids": {old file id: new file id}}."""
        try:
            return self._rename_files(file_ids, new_stem)
        except Exception as e:
            return _fail(f"The recording was not renamed: {_plain(e)}")

    def _rename_files(self, file_ids, new_stem):
        if not isinstance(file_ids, list) or not file_ids or not all(isinstance(i, str) for i in file_ids):
            return _fail("Nothing valid is selected.")
        stem, problem = folders.clean_name(new_stem, what="file")
        if problem:
            return _fail(problem)
        found = self._lib_paths("root")
        if found is None:
            return _fail(LIB_CHANGED)
        root = found[0]
        paths = OrderedDict()
        for fid in file_ids:
            path = self._library_file(fid)
            if path is None:
                return _fail(LIB_CHANGED)
            paths[fid] = os.path.abspath(path)
        where = {os.path.normcase(os.path.dirname(p)) for p in paths.values()}
        if len(where) != 1:
            return _fail("Only files in one folder can be renamed together.")
        folder = os.path.dirname(next(iter(paths.values())))
        refused = self._fs_begin()
        if refused:
            return refused
        pins = folders.Pins()
        try:
            refused = self._usable(root, folder, allow_root=True, pins=pins)
            if refused:
                return refused
            # Every target is checked before anything is renamed.
            plan, targets = [], {}
            for fid, path in paths.items():
                name = os.path.basename(path)
                if not os.path.isfile(path) or not folders.inside(root, path):
                    return _fail(f"{name} is no longer there. Refresh the list.")
                new_name = stem + os.path.splitext(name)[1]
                other = targets.get(new_name.casefold())
                if other is not None and other != name:
                    return _fail(f'{other} and {name} would both be named "{new_name}". '
                                 "Rename one of them in File Explorer.")
                targets[new_name.casefold()] = name
                dest = os.path.join(folder, new_name)
                if new_name != name:
                    if folders.too_long(dest):
                        return _fail("The new name would make the path too long for Windows. "
                                     "Choose a shorter name.")
                    if folders.name_taken(folder, new_name, ignore=name):
                        return _fail(f'There is already a file named "{new_name}" here. Nothing was renamed.')
                plan.append((fid, path, dest))
            done = []                               # (file id, path, dest, undo backups) renamed so far
            for fid, path, dest in plan:
                if path == dest:
                    continue
                name, new_name = os.path.basename(path), os.path.basename(dest)
                case_only = os.path.normcase(path) == os.path.normcase(dest)
                problem, undo = None, None
                try:
                    if not case_only and os.path.lexists(dest):    # os.rename replaces a file outside Windows
                        problem = f'There is already a file named "{new_name}" here'
                    else:
                        try:
                            undo = self._move_backups(path, dest)
                        except (StoreReadOnly, StoreUnavailable) as e:
                            problem = (f"{name} was not renamed: its recorder backup could not be "
                                       f"recorded under the new name ({e})")
                        else:
                            try:
                                self._rename(path, dest)
                            except OSError as e:
                                self._undo_backups(undo)
                                problem = (f'There is already a file named "{new_name}" here'
                                           if isinstance(e, FileExistsError)
                                           else f"{name} was not renamed: {_fs_problem(e)}")
                            else:
                                done.append((fid, path, dest, undo))
                except Exception as e:              # anything unforeseen: the earlier files are named back too
                    self._undo_backups(undo)
                    problem = f"{name} was not renamed: {_plain(e)}"
                if problem:
                    return self._roll_back_renames(done, problem)
            result = {"ok": True, "renamed": [], "ids": {}}
            for fid, path, dest in plan:
                result["ids"][fid] = _file_id(dest)
            for fid, path, dest, _undo in done:
                result["renamed"].append({"from": os.path.basename(path), "to": os.path.basename(dest)})
                try:
                    self._retarget_prefix(path, dest)
                except Exception:
                    pass                            # renamed all the same; fingerprinted again later
            if done:
                self._flush_index()
            return result
        finally:
            pins.close()
            self._fs_end()

    @staticmethod
    def _undo_backups(undo):
        if undo is not None:
            try:
                undo()
            except Exception:
                pass                                # a stale path: found by fingerprint all the same

    def _roll_back_renames(self, done, problem):
        """A file of a recording could not be renamed: name back the ones renamed
        before it (latest first), their backup paths first. The _fail() saying so;
        a file that cannot be named back (its old name taken meanwhile, or in use)
        keeps its new name, where its backup paths and index entry follow it."""
        stuck, ids = [], {}
        for fid, path, dest, undo in reversed(done):
            self._undo_backups(undo)
            try:
                case_only = os.path.normcase(path) == os.path.normcase(dest)
                if not case_only and os.path.lexists(path):
                    raise FileExistsError(errno.EEXIST, "The old name is taken", os.path.basename(path))
                self._rename(dest, path)
            except Exception:
                stuck.append(os.path.basename(dest))
                ids[fid] = _file_id(dest)
                try:
                    self._move_backups(path, dest)
                except Exception:
                    pass                            # a stale path: found by fingerprint all the same
                try:
                    self._retarget_prefix(path, dest)
                except Exception:
                    pass
        if stuck:
            try:
                self._flush_index()
            except Exception:
                pass
            problem += (f". {', '.join(reversed(stuck))} could not be given "
                        f"{'its' if len(stuck) == 1 else 'their'} old name back")
        else:
            problem += ". Nothing was renamed"
        return {**_fail(problem + "."), "ids": ids}
