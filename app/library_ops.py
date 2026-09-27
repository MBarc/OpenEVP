"""Library folder operations of the Api (a mixin): create, rename, delete (to
the Recycle Bin only) and move recordings, always inside the library folder.

Each operation holds the Api's _busy lock (no export, WAV with marks or update
meanwhile), refuses while a backup is queued or written, and pauses the
library indexer (_fs_op) before it touches a file. Everything that remembers a
path -- the fingerprint cache, rec handles, files served in place -- is
re-keyed to the new place, so marks follow and nothing is decoded again.

The small helpers shared with app/backend.py (plain errors, _fail, opaque ids)
live here so that backend.py can import them without a cycle.
"""
import errno
import hashlib
import os
from collections import OrderedDict

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
FS_WAIT = 10                # seconds a folder operation waits for the indexer to pause


def _plain(e):
    """An error in plain words that names a file, never its full path."""
    if isinstance(e, OSError) and e.filename:
        return f"{e.strerror or type(e).__name__} ({os.path.basename(e.filename)})"
    return str(e) or type(e).__name__


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

    def _fs_begin(self, pause=True):
        """Start a folder operation: None, or the _fail() saying why not. Holds
        _busy until _fs_end(). With pause, the indexer is stopped between files
        first (and no new indexer job or backup starts until _fs_end())."""
        if self._stop.is_set():
            return _fail(CLOSING)
        if not self._busy.acquire(blocking=False):
            return _fail(FS_BUSY)
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
        self._busy.release()

    def _retarget_prefix(self, old, new):
        """A file or folder moved from old to new: re-key the fingerprint cache
        (marks follow, nothing is decoded again), the read-only session cache, rec
        handles and the files the audio server serves in place. Decoded .dvf
        entries are left alone (they decode again when next played)."""
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

    def _walk(self, path):
        """A fresh look at everything in a folder (never through a symlink or
        junction): counts, and the cached fingerprints of its recordings."""
        info = {"recordings": 0, "with_evps": 0, "unindexed": 0, "other_files": 0, "subfolders": 0,
                "bytes": 0, "fps": set()}
        stack = [path]
        while stack:
            where = stack.pop()
            try:
                with os.scandir(where) as it:
                    entries = list(it)
            except OSError:
                info["unindexed"] += 1          # unreadable: whatever is in it is unknown
                continue
            for e in entries:
                try:
                    if e.is_symlink() or getattr(e, "is_junction", lambda: False)():
                        info["other_files"] += 1
                        continue
                    if e.is_dir():
                        info["subfolders"] += 1
                        stack.append(e.path)
                        continue
                    st = e.stat()
                except OSError:
                    continue
                info["bytes"] += st.st_size
                kind = os.path.splitext(e.name)[1].lower()[1:]
                if e.name.startswith(".") or kind not in ("dvf", "wav"):
                    info["other_files"] += 1
                    continue
                info["recordings"] += 1
                cached = self._cached_fp(e.path, st.st_size, st.st_mtime_ns)
                fp = cached.get("fp") if cached else None
                if cached is None:
                    info["unindexed"] += 1
                if fp:
                    info["fps"].add(fp)
                    r = self._store.recording(fp)
                    if r is not None and r["marks"]:
                        info["with_evps"] += 1
        return info

    def _saved_backups(self, fps):
        """The fingerprints among fps whose recorder backup is recorded as saved."""
        if self._store is None:
            return set()
        return {fp for fp in fps if self._store.backup(fp)["status"] == "saved"}

    def _fps_outside(self, folder, wanted):
        """Which of the fingerprints `wanted` have a copy in the library (latest
        listing) outside folder."""
        with self._lib_lock:
            paths = list(self._library.values())
        found = set()
        for path in paths:
            if not wanted - found:
                break
            if folders.under(path, folder):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            cached = self._cached_fp(path, st.st_size, st.st_mtime_ns)
            if cached and cached.get("fp") in wanted:
                found.add(cached["fp"])
        return found

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
        try:
            if not os.path.isdir(parent) or not folders.inside(root, parent, allow_root=True):
                return _fail(LIB_CHANGED)
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
        try:
            if not os.path.isdir(old) or not folders.inside(root, old):
                return _fail(LIB_CHANGED)
            parent, old_name = os.path.split(os.path.abspath(old))
            if name == old_name:
                return {"ok": True, "id": folder_id}
            target = os.path.join(parent, name)
            if folders.too_long(target):
                return _fail(PATH_TOO_LONG)
            if folders.name_taken(parent, name, ignore=old_name):
                return _fail(f'There is already something named "{name}" here.')
            try:
                os.rename(old, target)
            except FileExistsError:
                return _fail(f'There is already something named "{name}" here.')
            except OSError as e:
                return _fail(f"{old_name} was not renamed: {_fs_problem(e)}.")
            self._retarget_prefix(old, target)
            self._flush_index()
            return {"ok": True, "id": _folder_id(self._rel(root, target))}
        finally:
            self._fs_end()

    def folder_info(self, folder_id):
        """What deleting a folder would put in the Recycle Bin, counted fresh from
        disk: {"ok", "name", "recordings", "with_evps", "evps_at_least" (some
        recordings are not checked yet, so there may be more), "backups" (marked
        recorder recordings whose only backup copy is in it), "other_files",
        "subfolders", "bytes"}."""
        try:
            found = self._lib_paths(folder_id)
            if found is None:
                return _fail(LIB_CHANGED)
            root, path = found
            if not os.path.isdir(path) or not folders.inside(root, path, allow_root=folder_id == "root"):
                return _fail(LIB_CHANGED)
            info = self._walk(path)
            saved = self._saved_backups(info["fps"])
            backups = len(saved - self._fps_outside(path, saved))
            return {"ok": True, "name": os.path.basename(os.path.normpath(path)),
                    "recordings": info["recordings"], "with_evps": info["with_evps"],
                    "evps_at_least": info["unindexed"] > 0, "backups": backups,
                    "other_files": info["other_files"], "subfolders": info["subfolders"], "bytes": info["bytes"]}
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
        try:
            if not os.path.isdir(path) or not folders.inside(root, path):
                return _fail(LIB_CHANGED)
            candidates = self._saved_backups(self._walk(path)["fps"])
            error = None
            try:
                self._recycle(os.path.abspath(path))
            except folders.RecycleError as e:
                error = str(e)
            except Exception as e:
                error = f"{os.path.basename(path)} was not moved to the Recycle Bin: {_plain(e)}"
            # Every backup with no copy left (in what remains of the folder, or
            # elsewhere in the library) now shows as not backed up.
            left = self._walk(path)["fps"] if os.path.lexists(path) else set()
            lost = candidates - left
            lost -= self._fps_outside(path, lost)
            for fp in sorted(lost):
                if self._store.backup(fp)["status"] == "saved":
                    self._record_backup(fp, "failed", BACKUP_RECYCLED)
            if error:
                return {**_fail(error), "backups": len(lost)}
            return {"ok": True, "backups": len(lost)}
        finally:
            self._fs_end()

    def move_files(self, file_ids, folder_id):
        """Move library files (by id) into a library folder. A name already taken
        there gets the next free "<stem> (N)<ext>" (one N shared by the files with
        the same stem, so a .dvf and its .wav stay together); nothing is
        overwritten. Marks follow (same fingerprint). {"ok", "moved", "skipped"
        (already there), "renamed": [{"from", "to"}], "failed": [{"name", "error"}],
        "ids": {old file id: new file id}}."""
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
        try:
            if not os.path.isdir(target) or not folders.inside(root, target, allow_root=True):
                return _fail(LIB_CHANGED)
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
            for group in groups.values():
                self._move_group(group, target, result)
            if result["moved"]:
                self._flush_index()
            return result
        finally:
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
                try:
                    if os.path.lexists(dest):       # os.rename replaces a file outside Windows
                        raise FileExistsError(dest)
                    os.rename(path, dest)
                except FileExistsError:
                    taken.add(new_name.casefold())
                    continue
                except OSError as e:
                    problem = _fs_problem(e)
                    result["failed"].append({"name": name, "error": problem[:1].upper() + problem[1:] + "."})
                    break
                taken.add(new_name.casefold())
                self._retarget_prefix(path, dest)
                result["moved"] += 1
                result["ids"][fid] = _file_id(dest)
                if new_name != name:
                    result["renamed"].append({"from": name, "to": new_name})
                break
            else:
                result["failed"].append({"name": name, "error": "No free name was found for it."})
