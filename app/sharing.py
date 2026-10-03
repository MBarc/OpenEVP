"""Sharing library files with other programs: drag them out of the EVP Library
(Discord, WhatsApp, File Explorer, email...) or copy them to the clipboard as files.

The page sends file ids from the latest listing, never paths; each id is resolved
inside the library folder as Show in File Explorer resolves it. What is shared is a
file other programs can play:

- a WAV or .mp3, a clip included: the file itself;
- an MP3 saved as .mpeg (WhatsApp Web's name), .mpga, .mp2 or .m2a: a copy of
  it as <name>.mp3 in the share cache, since some programs take .mpeg for video
  (a copy, never a hard link: a program that writes to it can't change the original);
- a recorder's own file (a .dvf): the WAV beside it (same name, .wav) if it holds
  the same audio (the fingerprints of the decoded audio match: the library's
  cached ones, else worked out now), else an MP3 of the whole recording, made on
  demand with the clip MP3 settings (openevp.mp3) into the share cache as
  <name>.mp3. A WAV that only shares the name is never sent in its place.

The share cache is a folder in the temp folder. Each recording gets its own
subfolder (named from its path, size and modification time), so two recordings
with the same name never collide. What a program is given there is its own
fresh copy, in a folder of its own (<subfolder>/<12 random hex>/<name>.mp3),
made for that one drag or copy and never handed out again: a program that
edits or replaces the file it was given changes nothing anyone else gets. The
MP3 made of a .dvf is kept as a private master (<subfolder>/master/, never
handed out) with its SHA-256 beside it, and each share copies it while checking
that digest, so a second drag of the same recording does not encode it again
but a master that changed is made afresh. (Chosen over checking a digest of
the handed-out file itself: that file stays writable by whoever got it, so a
check before reuse could always be raced, and one recipient could still change
what the next one gets.) Made files must outlive the drop (the program dropped
on may read the file after the drag has ended, or the clipboard may be pasted
later), so they are removed only once they are SHARE_MAX_AGE old: at the next
start, or when the next one is made. Sharing only ever copies: the drag offers
Copy as its only effect, so no drop target can move or delete the original.

The share cache's folders are never followed through a link: the cache folder
and every subfolder must be a plain folder (no symlink, junction or other
reparse point), checked with lstat and held open while it is used (so it can't
be swapped for a link halfway); a link found in their place is removed -- the
link only -- and the folder made afresh. Cleaning removes only what the app
positively made: subfolders named as made_dir names them and marked with
OWNER_MARK (or, as v0.9.9 left them, holding nothing but .mp3 and .part files),
and in them only plain files and the master and hand-out folders holding plain
files. Anything else in the temp folder is left alone.

The drag and the clipboard themselves are Windows calls on the window's GUI
thread (app.native_share); the backend gets them as callables, so all of this is
tested without a window."""
import hashlib
import io
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time

from openevp import formats, mp3, wavinfo

from . import folders
from .library_ops import LIB_CHANGED, _decoder_problem, _fail, _not_format, _plain

SHARE_DIR = "openevp-share"           # in the temp folder
SHARE_MAX_AGE = 6 * 3600             # seconds a made MP3 is kept after it was last used
SHARE_LIMIT = 100                    # files in one drag or copy
SHARE_BUSY = "A drag is still being prepared. Try again in a moment."
NOT_HERE = "Dragging files out of OpenEVP is not available here."
COPY_NOT_HERE = "Copying files is not available here."
OWNER_MARK = ".openevp-share"        # in every share-cache subfolder the app made
NOT_PLAIN = "the share folder in the temp folder is not a plain folder"
_SUBFOLDER = re.compile(r"[0-9a-f]{16}")
_HANDOUT = re.compile(r"[0-9a-f]{12}")     # one recipient's copy is in a folder named like this
MASTER = "master"                    # in a recording's subfolder: the MP3 made of it, never handed out
DIGEST = "sha256"                    # beside the master: its SHA-256, in hex
_REPARSE_POINT = 0x400               # FILE_ATTRIBUTE_REPARSE_POINT
_DIRECTORY = 0x10                    # FILE_ATTRIBUTE_DIRECTORY


def share_root():
    return os.path.join(tempfile.gettempdir(), SHARE_DIR)


def playable_as_is(fmt):
    """Can other programs play this format's files as they are (WAV, MP3)?"""
    return fmt is formats.WAV or fmt in formats.MP3_FORMATS


def wav_beside(path):
    """The WAV beside a recorder's file (x.dvf -> x.wav in the same folder), or None."""
    wav = os.path.splitext(path)[0] + ".wav"
    return wav if os.path.isfile(wav) else None


def made_dir(root, path, st):
    """The share cache's subfolder for the recording at path (os.stat st):
    <root>/<16 hex of its path, size and mtime>."""
    key = f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{st.st_mtime_ns}"
    return os.path.join(root, hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:16])


def made_path(root, path, st):
    """Where the MP3 made of the recording at path (os.stat st) is kept, never
    handed out: <made_dir>/master/<its name>.mp3."""
    return os.path.join(made_dir(root, path, st), MASTER, os.path.splitext(os.path.basename(path))[0] + ".mp3")


def _is_reparse(st):
    """Is an lstat result a symlink, junction or any other reparse point?"""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _plain_folder(st):
    return stat.S_ISDIR(st.st_mode) and not _is_reparse(st)


def _plain_file(st):
    return stat.S_ISREG(st.st_mode) and not _is_reparse(st)


def _drop_link(path, st):
    """Remove the link or junction at path itself, never what it leads to."""
    if stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & _DIRECTORY:
        os.rmdir(path)                              # a junction or folder symlink: the link goes
    else:
        os.unlink(path)


def _folder(path, pins, make=True):
    """path as a plain folder, held in pins (folders.Pins: while held it can't be
    renamed, removed or swapped for a link) and checked again once held. Made if
    missing (with make; else None). A link or junction at path is removed -- the
    link only -- and a folder made in its place. Raises OSError otherwise."""
    for _ in range(3):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if not make:
                return None
            try:
                os.mkdir(path)
            except FileExistsError:
                pass
            continue
        if _is_reparse(st):
            _drop_link(path, st)
            continue
        if not stat.S_ISDIR(st.st_mode):
            break
        pins.add(path)
        if not _plain_folder(os.lstat(path)):
            break
        return path
    raise OSError(None, NOT_PLAIN, path)


def _cache_folder(sub, pins):
    """The share cache's subfolder `sub`, a plain folder marked as the app's,
    held (with the cache folder) in pins."""
    root = _folder(share_root(), pins)
    folder = _folder(os.path.join(root, sub), pins)
    mark = os.path.join(folder, OWNER_MARK)
    try:
        if not _plain_file(os.lstat(mark)):
            raise OSError(None, NOT_PLAIN, mark)
    except FileNotFoundError:
        with open(mark, "xb"):
            pass
    return folder


def _reusable(path):
    """Is path a plain file (never a link)?"""
    try:
        return _plain_file(os.lstat(path))
    except OSError:
        return False


class _Damaged(Exception):
    """A master no longer matches the digest recorded when it was made."""


def _hand_out(sub, name, fill, pins):
    """A fresh copy for one recipient: <sub>/<12 random hex>/<name>, written by
    fill(part path) and put in place only once complete. Its folder is new, a
    plain folder held in pins; nothing is left of it when fill fails."""
    for _ in range(8):
        folder = os.path.join(sub, secrets.token_hex(6))
        try:
            os.mkdir(folder)
            break
        except FileExistsError:
            continue
    else:
        raise OSError(None, "no free name for the copy to share", sub)
    target = os.path.join(folder, name)
    part = target + ".part"
    try:
        _folder(folder, pins)
        fill(part)
        os.replace(part, target)
    except BaseException:
        for gone in (part, target):
            try:
                os.remove(gone)
            except OSError:
                pass
        pins.release(folder)
        try:
            os.rmdir(folder)
        except OSError:
            pass
        raise
    return target


def _copy_checked(src, digest, dst):
    """Copy src to dst, hashing what is copied; _Damaged unless it is digest."""
    h = hashlib.sha256()
    with open(src, "rb") as f, open(dst, "xb") as out:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
            out.write(block)
    if not secrets.compare_digest(h.hexdigest(), digest):
        raise _Damaged(src)


def _read_digest(folder):
    """The master's recorded SHA-256 (64 hex), or None."""
    path = os.path.join(folder, DIGEST)
    if not _reusable(path):
        return None
    try:
        with open(path, "r", encoding="ascii") as f:
            text = f.read(80).strip()
    except (OSError, ValueError):
        return None
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def _write_file(path, data):
    """data into path, put in place only once complete."""
    part = path + f".{os.getpid()}-{threading.get_ident()}.part"
    try:
        with open(part, "wb") as f:
            f.write(data)
        os.replace(part, path)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def _write_new(path, data):
    with open(path, "xb") as f:
        f.write(data)


def _empty(folder, pins):
    """Remove a master or hand-out folder holding nothing but plain files (held
    while they go), then the folder. False (and nothing removed) otherwise."""
    pins.add(folder)
    try:
        if not _plain_folder(os.lstat(folder)):
            return False
        with os.scandir(folder) as it:
            entries = [(e.name, e.stat(follow_symlinks=False)) for e in it]
        if not all(_plain_file(st) for _name, st in entries):
            return False
        for name, _st in entries:
            os.remove(os.path.join(folder, name))
    finally:
        pins.release(folder)
    os.rmdir(folder)
    return True


def _remove_owned(folder, pins, max_age=SHARE_MAX_AGE, now=None):
    """Clean one subfolder of the share cache if the app made it: marked with
    OWNER_MARK (or, as v0.9.9 made them, holding only .mp3 and .part files),
    holding only plain files and master / hand-out folders of plain files.
    Anything else is left alone. One max_age old goes whole; in a younger one
    only the hand-out folders that are max_age old go. The folder is held while
    its contents go, so it can't be swapped for a link meanwhile. True when the
    folder itself went."""
    now = time.time() if now is None else now
    pins.add(folder)
    try:
        st = os.lstat(folder)
        if not _plain_folder(st):
            return False
        with os.scandir(folder) as it:
            entries = [(e.name, e.path, e.stat(follow_symlinks=False)) for e in it]
        files = [name for name, _p, est in entries if _plain_file(est)]
        subs = [(name, p, est) for name, p, est in entries
                if _plain_folder(est) and (name == MASTER or _HANDOUT.fullmatch(name))]
        if len(files) + len(subs) != len(entries):
            return False                                # something the app did not make
        owned = OWNER_MARK in files
        if not owned and (subs or not all(n.lower().endswith((".mp3", ".part")) for n in files)):
            return False
        if now - st.st_mtime < max_age:
            for name, p, est in subs:                   # shared lately: only old copies go
                if name != MASTER and now - est.st_mtime >= max_age:
                    _empty(p, pins)
            return False
        for _name, p, _est in subs:
            if not _empty(p, pins):
                return False
        for name in files:
            if name != OWNER_MARK:
                os.remove(os.path.join(folder, name))
        if owned:
            os.remove(os.path.join(folder, OWNER_MARK))     # last: a half-cleaned folder stays known
    finally:
        pins.release(folder)
    os.rmdir(folder)
    return True


def clean(root=None, max_age=SHARE_MAX_AGE, now=None):
    """Remove the share cache's subfolders that are max_age old or more (by their
    own modification time, refreshed whenever their recording is shared again),
    and older copies in younger ones, only what the app made (see _remove_owned). The cache folder must be a plain
    folder: a link in its place is removed (the link only) and nothing else.
    Never raises; returns how many subfolders went."""
    root = root or share_root()
    now = time.time() if now is None else now
    gone = 0
    try:
        with folders.Pins() as pins:
            if _folder(root, pins, make=False) is None:
                return 0
            with os.scandir(root) as it:
                entries = list(it)
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                    if (_plain_folder(st) and _SUBFOLDER.fullmatch(entry.name)
                            and _remove_owned(entry.path, pins, max_age, now)):
                        gone += 1
                except OSError:
                    pass                            # in use (being dropped or pasted): next time
    except OSError:
        pass
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
        for path in sources:
            got = self._check_library_file(root, path)
            if isinstance(got, dict):
                return got
        out, seen, made = [], set(), 0
        for path in sources:
            fmt = formats.by_ext(os.path.splitext(path)[1])
            if fmt is not None and not playable_as_is(fmt):
                beside = wav_beside(path)
                got = self._recorder_file_share(path, fmt, beside if beside and folders.inside(root, beside) else None)
                if not got["ok"]:
                    return got
                made += got["made"]
                path = got["path"]
            elif fmt in formats.MP3_FORMATS and fmt.ext != ".mp3":
                got = self._as_mp3(path)
                if not got["ok"]:
                    return got
                path = got["path"]
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                out.append(path)
        return {"ok": True, "paths": out, "made": made}

    def _as_mp3(self, path):
        """{ok, path}: an MP3 saved under another extension (.mpeg, .mpga, .mp2,
        .m2a) as <name>.mp3 in the share cache: a fresh copy for each share
        (_hand_out), never a hard link (a program that writes to what it was
        given must not change the original, nor what anyone is given later).
        Some programs take .mpeg for video (WhatsApp desktop crashed sending
        one). Layer II audio is named .mp3 too: players accept it, the video
        path is the problem. The original is never renamed or written."""
        name = os.path.basename(path)
        root = share_root()
        try:
            st = os.stat(path)
            clean(root)
            with folders.Pins() as pins:
                sub = _cache_folder(os.path.basename(made_dir(root, path, st)), pins)
                target = _hand_out(sub, os.path.splitext(name)[0] + ".mp3",
                                   lambda part: shutil.copyfile(path, part), pins)
        except Exception as e:
            return _fail(f"Could not prepare {name} to share: {_plain(e)}")
        return {"ok": True, "path": target}

    def _recorder_file_share(self, path, fmt, beside):
        """{ok, path, made}: what is shared for a recorder's file (a .dvf): the WAV
        beside it (beside, already checked to be inside the library folder, or
        None) when it holds the same audio, else the MP3 made of it (_made_mp3).
        The same audio: the fingerprints of the decoded audio match -- the
        library's cached ones when it has both; otherwise the .dvf is decoded
        now, and a WAV that turns out not to match gets the MP3 made of that
        very decode (never decoded twice)."""
        if beside is None:
            return self._made_mp3(path, fmt)
        name = os.path.basename(path)
        known, wav_fp = self._known_fps(path, beside)
        if wav_fp is None:                              # not a readable PCM WAV: not the same audio
            return self._made_mp3(path, fmt)
        if known is not None:
            return {"ok": True, "path": beside, "made": 0} if known == wav_fp else self._made_mp3(path, fmt)
        problem = self._recorder_file_problem(path, fmt)
        if problem:
            return problem
        try:
            st = os.stat(path)
            self._emit("share-preparing", {"name": name})
            wav = self._share_wav(path, fmt, st)
            with wavinfo.buffer_file(wav) as f:
                same = wavinfo.wav_fingerprint(f, should_stop=self._stop.is_set) == wav_fp
        except (formats.Cancelled, wavinfo.Stopped):
            return _fail("The app is closing.")
        except Exception as e:
            return _fail(f"Could not prepare {name} to share: {_plain(e)}")
        if same:
            return {"ok": True, "path": beside, "made": 0}
        return self._made_mp3(path, fmt, decoded=(wav, st))

    def _known_fps(self, path, wav):
        """(the cached fingerprint of the recorder's file at path, or None; the
        fingerprint of the WAV beside it: cached, else worked out from the file
        now; None when it is not a readable PCM WAV)."""
        def cached(p):
            try:
                st = os.stat(p)
            except OSError:
                return None
            got = self._cached_fp(p, st.st_size, st.st_mtime_ns)
            return got.get("fp") if got else None
        wav_fp = cached(wav)
        if wav_fp is None:
            try:
                wav_fp = wavinfo.wav_fingerprint(wav, should_stop=self._stop.is_set)
            except (OSError, ValueError, wavinfo.Stopped):
                wav_fp = None
        return cached(path), wav_fp

    def _recorder_file_problem(self, path, fmt):
        """Why a recorder's file can't be decoded to share it (a _fail()), or None."""
        name = os.path.basename(path)
        problem = _decoder_problem(fmt) or fmt.file_problem(path)
        if problem:
            return _fail(f"{name} can't be shared: {problem}.")
        problem = _not_format(fmt, path)
        if problem:
            return _fail(problem)
        try:
            size = os.stat(path).st_size
        except OSError as e:
            return _fail(f"Could not prepare {name} to share: {_plain(e)}")
        if fmt.max_bytes is not None and size > fmt.max_bytes:
            return _fail(f"{name} is too large to be {fmt.a_recording()}.")
        return None

    def _made_mp3(self, path, fmt, decoded=None):
        """{ok, path, made}: the MP3 of a recorder's file in the share cache,
        made now (made 1) unless it is there already. decoded: (its WAV, the
        os.stat it was decoded at), when it was decoded already."""
        name = os.path.basename(path)
        if not mp3.available():
            return _fail(f"{name} can't be shared: {mp3.UNAVAILABLE}")
        problem = self._recorder_file_problem(path, fmt)
        if problem:
            return problem
        root = share_root()
        stem = os.path.splitext(name)[0]
        made = 0
        try:
            st = decoded[1] if decoded is not None else os.stat(path)
            clean(root)
            with folders.Pins() as pins:
                sub = _cache_folder(os.path.basename(made_dir(root, path, st)), pins)
                keep = _folder(os.path.join(sub, MASTER), pins)
                master = os.path.join(keep, stem + ".mp3")
                digest = _read_digest(keep)
                target = None
                if digest is not None and _reusable(master):
                    try:
                        target = _hand_out(sub, stem + ".mp3", lambda part: _copy_checked(master, digest, part), pins)
                    except _Damaged:
                        target = None                   # changed since it was made: made afresh
                if target is None:
                    if decoded is not None:
                        wav = decoded[0]
                        decoded = None
                    else:
                        self._emit("share-preparing", {"name": name})
                        wav = self._share_wav(path, fmt, st)
                    data = mp3.encode(wav, title=stem)
                    del wav
                    try:
                        os.remove(os.path.join(keep, DIGEST))
                    except FileNotFoundError:
                        pass
                    _write_file(master, data)
                    _write_file(os.path.join(keep, DIGEST), hashlib.sha256(data).hexdigest().encode("ascii"))
                    target = _hand_out(sub, stem + ".mp3", lambda part: _write_new(part, data), pins)
                    made = 1
        except formats.Cancelled:
            return _fail("The app is closing.")
        except Exception as e:
            return _fail(f"Could not prepare {name} to share: {_plain(e)}")
        return {"ok": True, "path": target, "made": made}

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
