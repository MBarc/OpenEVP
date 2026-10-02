"""Sharing library files with other programs: drag them out of the EVP Library
(Discord, WhatsApp, File Explorer, email...) or copy them to the clipboard as files.

The page sends file ids from the latest listing, never paths; each id is resolved
inside the library folder as Show in File Explorer resolves it. What is shared is a
file other programs can play:

- a WAV or MP3 (also .mpeg and the other MP3 extensions), a clip included: the file itself;
- a recorder's own file (a .dvf): the WAV beside it (same name, .wav) if there is
  one, else an MP3 of the whole recording, made on demand with the clip MP3
  settings (openevp.mp3) into the share cache as <name>.mp3.

The share cache is a folder in the temp folder. Each recording gets its own
subfolder (named from its path, size and modification time), so two recordings
with the same name never collide, and a second drag of the same recording reuses
its MP3. Made files must outlive the drop (the program dropped on may read the file
after the drag has ended, or the clipboard may be pasted later), so they are
removed only once they are SHARE_MAX_AGE old: at the next start, or when the next
one is made. Sharing only ever copies: the drag offers Copy as its only effect, so
no drop target can move or delete the original.

The drag and the clipboard themselves are Windows calls on the window's GUI
thread (app.native_share); the backend gets them as callables, so all of this is
tested without a window."""
import hashlib
import io
import os
import shutil
import tempfile
import threading
import time

from openevp import formats, mp3

from . import folders
from .library_ops import LIB_CHANGED, ROOT_CHANGED, _decoder_problem, _fail, _not_format, _plain

SHARE_DIR = "openevp-share"           # in the temp folder
SHARE_MAX_AGE = 6 * 3600             # seconds a made MP3 is kept after it was last used
SHARE_LIMIT = 100                    # files in one drag or copy
SHARE_BUSY = "A drag is still being prepared. Try again in a moment."
NOT_HERE = "Dragging files out of OpenEVP is not available here."
COPY_NOT_HERE = "Copying files is not available here."


def share_root():
    return os.path.join(tempfile.gettempdir(), SHARE_DIR)


def playable_as_is(fmt):
    """Can other programs play this format's files as they are (WAV, MP3)?"""
    return fmt is formats.WAV or fmt in formats.MP3_FORMATS


def wav_beside(path):
    """The WAV beside a recorder's file (x.dvf -> x.wav in the same folder), or None."""
    wav = os.path.splitext(path)[0] + ".wav"
    return wav if os.path.isfile(wav) else None


def made_path(root, path, st):
    """Where the MP3 made of the recording at path (os.stat st) goes:
    <root>/<16 hex of its path, size and mtime>/<its name>.mp3."""
    key = f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{st.st_mtime_ns}"
    sub = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return os.path.join(root, sub, os.path.splitext(os.path.basename(path))[0] + ".mp3")


def clean(root=None, max_age=SHARE_MAX_AGE, now=None):
    """Remove what the share cache holds that is max_age old or more (a subfolder
    by its own modification time, refreshed whenever its MP3 is shared again).
    Never raises; returns how many entries went."""
    root = root or share_root()
    now = time.time() if now is None else now
    gone = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        try:
            if now - entry.stat(follow_symlinks=False).st_mtime < max_age:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.remove(entry.path)
            gone += 1
        except OSError:
            pass                                    # in use (being dropped or pasted): next time
    return gone


def _touch(path):
    try:
        os.utime(path)
    except OSError:
        pass


class ShareOps:
    """Drag out / Copy file; mixed into app.backend.Api (uses its library listing,
    audio server, stop flag and emit). Api.__init__ sets _share_lock, _drag_files
    (paths -> "copy", "none", or None when the mouse button was already up) and
    _copy_files (paths -> None), each None where there is no window to do it."""

    def drag_out(self, file_ids):
        """Start a Windows file drag of library files (ids from list_library), as if
        dragged from File Explorer, while the mouse button is still down. Files that
        need an MP3 made are made first ("share-preparing" events). Returns when the
        drag ends: {ok, started, count, effect ("copy" if it was dropped somewhere
        that took it, else "none")}; started False when the button was let go before
        the files were ready (they are kept, so the next drag starts at once)."""
        if self._drag_files is None:
            return _fail(NOT_HERE)
        return self._share(file_ids, self._drag_files, drag=True)

    def copy_files(self, file_ids):
        """Put library files (ids from list_library) on the clipboard as files, as
        Copy in File Explorer does, so Ctrl+V pastes them into a chat or a folder.
        The same files a drag would share. {ok, count}."""
        if self._copy_files is None:
            return _fail(COPY_NOT_HERE)
        return self._share(file_ids, self._copy_files, drag=False)

    def _share(self, file_ids, act, drag):
        lock = self._share_lock
        if not lock.acquire(blocking=False):
            return _fail(SHARE_BUSY)
        try:
            got = self._share_paths(file_ids)
            if not got["ok"]:
                return got
            paths = got["paths"]
            try:
                effect = act(paths)
            except Exception as e:
                what = "dragged" if drag else "copied"
                return _fail(f"The {'file' if len(paths) == 1 else 'files'} could not be {what}: {_plain(e)}")
            if not drag:
                return {"ok": True, "count": len(paths)}
            if effect is None:
                return {"ok": True, "started": False, "count": len(paths), "made": got["made"]}
            return {"ok": True, "started": True, "count": len(paths), "effect": effect}
        finally:
            lock.release()

    def _share_paths(self, file_ids):
        """{ok, paths, made}: the files to share for these ids (see the module
        docstring), each once, in the order given; made counts the MP3s made now.
        Any id that is not a file inside the library folder fails the whole call."""
        if not isinstance(file_ids, list) or not file_ids or not all(isinstance(i, str) for i in file_ids):
            return _fail(LIB_CHANGED)
        if len(file_ids) > SHARE_LIMIT:
            return _fail(f"Share at most {SHARE_LIMIT} files at once.")
        with self._lib_lock:
            root = self._library_folders.get("root")
            sources = [self._library.get(i) for i in file_ids]
        if root is None or any(p is None for p in sources):
            return _fail(LIB_CHANGED)
        if os.path.normcase(os.path.abspath(root)) != os.path.normcase(os.path.abspath(self._library_path())):
            return _fail(LIB_CHANGED)
        if self._root_moved(root):
            return _fail(ROOT_CHANGED)
        for path in sources:
            if not os.path.isfile(path):
                return _fail(f"{os.path.basename(path)} is no longer there. Refresh the list.")
            if not folders.inside(root, path):
                return _fail(LIB_CHANGED)
        out, seen, made = [], set(), 0
        for path in sources:
            fmt = formats.by_ext(os.path.splitext(path)[1])
            if fmt is not None and not playable_as_is(fmt):
                beside = wav_beside(path)
                if beside is not None and folders.inside(root, beside):
                    path = beside
                else:
                    got = self._made_mp3(path, fmt)
                    if not got["ok"]:
                        return got
                    made += got["made"]
                    path = got["path"]
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                out.append(path)
        return {"ok": True, "paths": out, "made": made}

    def _made_mp3(self, path, fmt):
        """{ok, path, made}: the MP3 of a recorder's file in the share cache,
        made now (made 1) unless it is there already."""
        name = os.path.basename(path)
        if not mp3.available():
            return _fail(f"{name} can't be shared: {mp3.UNAVAILABLE}")
        problem = _decoder_problem(fmt) or fmt.file_problem(path)
        if problem:
            return _fail(f"{name} can't be shared: {problem}.")
        problem = _not_format(fmt, path)
        if problem:
            return _fail(problem)
        root = share_root()
        try:
            st = os.stat(path)
            if fmt.max_bytes is not None and st.st_size > fmt.max_bytes:
                return _fail(f"{name} is too large to be {fmt.a_recording()}.")
            target = made_path(root, path, st)
            if os.path.isfile(target):
                _touch(os.path.dirname(target))
                return {"ok": True, "path": target, "made": 0}
            clean(root)
            self._emit("share-preparing", {"name": name})
            wav = self._share_wav(path, fmt, st)
            data = mp3.encode(wav, title=os.path.splitext(name)[0])
            del wav
            os.makedirs(os.path.dirname(target), exist_ok=True)
            part = target + f".{os.getpid()}-{threading.get_ident()}.part"
            try:
                with open(part, "wb") as f:
                    f.write(data)
                os.replace(part, target)
            except BaseException:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
        except formats.Cancelled:
            return _fail("The app is closing.")
        except Exception as e:
            return _fail(f"Could not prepare {name} to share: {_plain(e)}")
        return {"ok": True, "path": target, "made": 1}

    def _share_wav(self, path, fmt, st):
        """The WAV (bytes) of a recorder's file: the audio server's decode when the
        player has one cached, else decoded now."""
        key = (fmt.ext[1:], os.path.normcase(path), st.st_size, st.st_mtime_ns)   # as _play_file's
        cached = getattr(self._server, "open_cached", None)
        f = cached(key) if cached is not None else None
        if f is not None:
            with f:
                return f.read()
        with open(path, "rb") as src:
            data = src.read()
        out = io.BytesIO()
        formats.write_wav(fmt, data, out, should_stop=self._stop.is_set)
        return out.getvalue()
