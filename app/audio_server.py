"""Serve decoded recordings to the player over http://127.0.0.1.

Only this machine can connect, and every URL carries a random per-run token.
Decoded WAVs live in a disk cache bounded by total size (CACHE_BYTES) and are
served from disk with Range support, so long recordings stream. A decoder that
can stream writes straight into the cache file, and peaks are computed from
the file, so even a 90-minute ICD-ST10 recording (about 930 MB of WAV) is never
held in memory for playback. prepare() returns
waveform peaks and the duration, so the player never has to download and
decode a whole file just to draw it.

prepare_file() does the same for a WAV file the user picked (for example from a
recorder that writes WAV itself): it is served from where it is, never copied or
deleted, and only files registered this way can be reached. The file's size and
modification time are recorded when it is fingerprinted, and which file it is
(its volume serial number and file id, kept across renames and moves); if any of
them differs when the player asks for audio, the request is refused (409), so
the player never gets different audio under a handle whose marks belong to the
fingerprinted audio -- nor another file put at that path, or reached through a
folder swapped for a junction since it was loaded, even with the same size and
time. (The file is reopened by path for each request rather than held open for
as long as it is loaded: an open file, even one shared for deleting, stops
Windows renaming, moving or recycling the folder it is in, and the library does
those with a recording loaded.) A file whose file system gives no usable
identity (FAT/exFAT USB sticks, some network shares) is not served in place at
all: a private copy of it goes into the cache at load time, like a decode, and
that copy is served (within the same budget and eviction rules).
The file is not copied to snapshot it: WAVs can be gigabytes. Files are opened
so that they can still be moved, renamed or recycled while they are being read
(Windows FILE_SHARE_DELETE); retarget_prefix() then points a served file at its
new place, and its URL keeps working.

Every file reaches the page as a PCM WAV: a .dvf or an MP3 is decoded here
(prepare(), into the cache), never by the page, so there is one playback path.

The listening tools read the audio a URL serves (open_audio(): the cached
decode, or the user's file, refused once it changed on disk), and the player's
spectrogram is served from here too: add_spectrogram() keeps the last few
(openevp.spectrogram) and serves their tiles as PNG images.
"""
import errno
import hashlib
import os
import re
import secrets
import shutil
import sys
import threading
import wave
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from openevp import wavinfo

# Sent with every response (Handler.end_headers): any origin may read it (only this machine can connect,
# and every URL carries the per-run token); the page may read the range headers of a partial answer.
CORS_HEADERS = (("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges"))
SPECTROGRAMS = 3                       # spectrograms kept (a 30-minute ICD-ST25 one is about 60 MB)
PEAKS_PER_SECOND = 400                 # the player's deepest zoom (px per second), so zooming shows real detail
MAX_PEAKS = 400_000                    # longer files get fewer per second (keeps the page responsive)
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


CHUNK_BYTES = 4 << 20                  # peaks are computed in chunks: a WAV can be gigabytes


def _samples(data, width):
    """PCM bytes -> signed int64 samples (8-bit WAV is unsigned, 24-bit has no numpy type)."""
    if width == 1:
        return np.frombuffer(data, dtype=np.uint8).astype(np.int64) - 128
    if width == 3:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int64)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        return np.where(v >= 1 << 23, v - (1 << 24), v)
    return np.frombuffer(data, dtype={2: "<i2", 4: "<i4"}[width]).astype(np.int64)


def _analyze(f, fingerprint=True):
    """(peaks, duration, rate, fp) for a PCM WAV: PEAKS_PER_SECOND values in 0..1 per
    second of audio (at most MAX_PEAKS in all), each the loudest sample of any channel
    in its slice, plus the sample rate and the audio fingerprint (openevp.wavinfo) of the
    decoded samples. Reads in chunks, never the whole file. A WAV with no samples
    has fp None: every empty WAV of one format would otherwise share one identity
    (and one set of marks), so it gets none and cannot be marked. fingerprint False:
    fp is None too (audio that must never be taken for a recording)."""
    try:
        w = wave.open(f)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV file ({e})") from None
    with w:
        rate, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        if width not in (1, 2, 3, 4):
            raise ValueError(f"unsupported sample size ({8 * width} bit)")
        h = hashlib.sha256(wavinfo.fingerprint_prefix(ch, width, rate))
        if not n or not rate:
            return [], 0.0, rate, None
        expected = n * ch * width
        count = min(MAX_PEAKS, n, max(1, -(-n * PEAKS_PER_SECOND // rate)))
        per = -(-n // count)                                  # frames per peak (ceil)
        chunk = per * max(1, CHUNK_BYTES // (per * ch * width))  # whole peaks per chunk
        full = float(1 << (8 * width - 1))
        peaks = []
        total = 0
        while True:
            data = w.readframes(chunk)
            if not data:
                break
            total += len(data)
            if fingerprint:
                h.update(data)
            mags = np.abs(_samples(data, width).reshape(-1, ch)).max(axis=1)
            peaks.extend((np.maximum.reduceat(mags, np.arange(0, len(mags), per)) / full).tolist())
        if total != expected:
            raise ValueError("the WAV file is truncated")
        return [round(min(p, 1.0), 4) for p in peaks], n / rate, rate, h.hexdigest() if fingerprint else None


def _channels(f):
    """The channel count of an analyzed (so readable) PCM WAV file, from its header."""
    f.seek(0)
    with wave.open(f) as w:
        return w.getnchannels()


def _open_shared(path):
    """Open a file for reading (binary) without stopping anyone from reading,
    writing, renaming or deleting it meanwhile. Python's own open() on Windows
    does not share delete access, so a file being played could not be moved."""
    if sys.platform != "win32":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_READ, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 0x80000000, 3, 0x80
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE = 0x1, 0x2, 0x4
    handle = kernel32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                  None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        err = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(err).strip(), path, err)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def _stat_of(st):
    """(size, mtime_ns): what identifies one version of a file served in place."""
    return st.st_size, st.st_mtime_ns


def _ident_of(st):
    """(volume serial number, file id) of an open file: which file it is, the
    same across renames and moves on its volume. None where the file system
    gives no usable one (no file id or no volume serial number)."""
    return (st.st_dev, st.st_ino) if st.st_ino and st.st_dev else None


def _not_as_loaded(e, st):
    """Is the open file (os.fstat st) not the one a served-in-place entry was
    loaded from: another version of it, or another file altogether?"""
    if "stat" in e and _stat_of(st) != e["stat"]:
        return True
    return e.get("ident") is not None and _ident_of(st) != e["ident"]


# The decoded-WAV cache's budget on disk. The longest ICD-ST10 recording (its
# 32 MB of flash, about 92 minutes of 44.1 kHz stereo) decodes to about 930 MB,
# so this holds two of those, or many hours of ICD-ST25 audio (8 kHz mono). The
# entry just prepared is always kept, so the cache can exceed the budget by at
# most that one file; older entries are evicted first.
CACHE_BYTES = 2 << 30
CACHE_PREFIX = "openevp-audio-"        # the app's cache folder in the temp folder: <prefix><random>
OLD_CACHE_PREFIXES = ("st25-audio-",)  # 0.9.9 and earlier: their leftovers are cleaned up too


IN_USE = ".in-use"                     # held open by the app that owns a cache folder
STALE_AFTER = 60                       # seconds: an unmarked folder younger than this may be starting up


def hold_cache(folder):
    """Mark a cache folder as in use for as long as the returned file stays open:
    Windows refuses to delete an open file, so clean_stale_caches() in another
    OpenEVP leaves this folder alone. Close it before removing the folder."""
    return open(os.path.join(folder, IN_USE), "wb")


def clean_stale_caches(parent, keep=None):
    """Delete cache folders (CACHE_PREFIX*, or an OLD_CACHE_PREFIXES one) that
    earlier runs left in ``parent`` (the temp folder) after a crash or a power
    cut: they can hold gigabytes of
    decoded audio. Two OpenEVP windows can run at once, so a folder whose
    IN_USE file cannot be deleted (another running app holds it open) is kept,
    and so is one without it that is younger than STALE_AFTER (an app just
    starting). ``keep``: a folder to leave alone. Returns the folders removed."""
    import shutil
    import time
    removed = []
    try:
        names = os.listdir(parent)
    except OSError:
        return removed
    for name in names:
        path = os.path.join(parent, name)
        if not name.startswith((CACHE_PREFIX,) + OLD_CACHE_PREFIXES) or (keep and os.path.normcase(path) == os.path.normcase(keep)):
            continue
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        marker = os.path.join(path, IN_USE)
        try:
            if os.path.exists(marker):
                os.remove(marker)              # fails while its app runs (the file is open)
            elif time.time() - os.path.getmtime(path) < STALE_AFTER:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed.append(path)
    return removed


def _disk_full(e):
    """Whether an OSError says the disk is full."""
    return e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)   # HANDLE_DISK_FULL, DISK_FULL


class AudioServer:
    def __init__(self, provider, cache_dir, max_bytes=CACHE_BYTES):
        self._provider = provider            # (device_id, folder id, number) -> WAV bytes
        self._dir = cache_dir
        self._max = max_bytes
        self._token = secrets.token_urlsafe(16)
        self._entries = OrderedDict()        # key -> {"file", "size", "peaks", "duration", "rate", "channels", "fp"}; LRU order
        self._by_file = {}                   # file id -> key
        self._lock = threading.Lock()
        self._inflight = {}                  # key -> threading.Lock (one decode per key)
        self._pinned = set()                 # keys never evicted (the player's recording and the version it plays)
        self._specs = OrderedDict()          # spectrogram id -> (file id, openevp.spectrogram.Spectrogram); LRU order
        self._httpd = None

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server._handle(self)

            def do_OPTIONS(self):                # a CORS preflight (a fetch with a Range header, say)
                self.send_response(204)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Range")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def end_headers(self):
                # Every answer, errors included, carries the CORS headers: the UI is served from
                # another local port and its media element asks for CORS (Web Audio needs it), so
                # without them a 404/409/416 would reach the page as an opaque network failure.
                for k, v in CORS_HEADERS:
                    self.send_header(k, v)
                super().end_headers()

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, name="audio-server", daemon=True).start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _url(self, file_id):
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/{self._token}/{file_id}.wav"

    def prepare(self, key, make=None, write=None, expected=None, fingerprint=True):
        """Decode a recording into the cache (once per key) and return _info().
        ``write(f)``, when given, decodes straight into the cache file ``f`` (a
        seekable binary file), so a long recording is never held in memory;
        otherwise ``make()`` (or the provider) returns the WAV bytes. Either
        way the peaks and fingerprint are then read back from the file in
        chunks. A WAV that is not playable PCM raises ValueError and leaves
        nothing behind.

        Room is made first: older entries are evicted down to the budget minus
        ``expected`` (the WAV's expected size, when known; ``write`` can also
        call reserve() once it knows). If the disk fills up anyway, every other
        entry is evicted and the decode is tried once more. fingerprint False: no
        fingerprint is taken (fp None), for audio that is not a recording of its own
        (a noise-reduced version of one)."""
        with self._lock:
            gate = self._inflight.setdefault(key, threading.Lock())
        with gate:                                   # concurrent requests wait for one decode
            with self._lock:
                e = self._entries.get(key)
                if e is not None:
                    self._entries.move_to_end(key)
                    return self._info(e)
            try:
                file_id = secrets.token_hex(8)
                path = os.path.join(self._dir, file_id + ".wav")
                if expected:
                    self.reserve(expected)
                try:
                    try:
                        self._produce(key, path, make, write)
                    except OSError as e:
                        if not _disk_full(e):
                            raise
                        self._remove(path)
                        with self._lock:
                            self._evict(keep=None, limit=0)      # everything else goes; then once more
                        self._produce(key, path, make, write)
                    with open(path, "rb") as f:
                        peaks, duration, rate, fp = _analyze(f, fingerprint)
                        channels = _channels(f)
                        size = os.fstat(f.fileno()).st_size
                except BaseException:
                    self._remove(path)
                    raise
                with self._lock:
                    self._entries[key] = {"file": file_id, "size": size, "peaks": peaks, "duration": duration,
                                          "rate": rate, "channels": channels, "fp": fp}
                    self._by_file[file_id] = key
                    self._evict(keep=key)
                return self._info(self._entries[key])
            finally:
                with self._lock:
                    self._inflight.pop(key, None)

    def _produce(self, key, path, make, write):
        """Write the decoded WAV to path (see prepare)."""
        if write is not None:
            with open(path, "wb") as f:
                write(f)
            return
        wav = make() if make else self._provider(key)
        with open(path, "wb") as f:
            f.write(wav)

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def reserve(self, nbytes):
        """Make room for a WAV of about ``nbytes`` about to be written: evict
        the oldest entries down to the budget minus that (never below 0)."""
        if nbytes:
            with self._lock:
                self._evict(keep=None, limit=max(0, self._max - int(nbytes)))

    def prepare_file(self, path):
        """Register a WAV file the user picked; returns {"url", "peaks", "duration",
        "rate", "fp", "stat"} (fp None for a WAV with no samples; stat is the
        (size, mtime_ns) the fingerprint belongs to, for the caller's own checks --
        not for the page). Raises ValueError for a file that is not a playable PCM
        WAV, or that changed while it was being read."""
        path = os.path.abspath(path)
        with _open_shared(path) as f:
            st = os.fstat(f.fileno())
            stat, ident = _stat_of(st), _ident_of(st)
            if ident is None:
                return self._prepare_copy(path, f, stat)
            key = ("file", os.path.normcase(path), *stat, ident)
            with self._lock:
                e = self._entries.get(key)
                if e is not None:
                    self._entries.move_to_end(key)
                    return {**self._info(e), "stat": e["stat"]}
            peaks, duration, rate, fp = _analyze(f)
            channels = _channels(f)
            now = os.stat(path)
            if _stat_of(os.fstat(f.fileno())) != stat or _stat_of(now) != stat or (
                    ident is not None and _ident_of(now) != ident):
                raise ValueError("the file changed while it was being read; try again")
        file_id = secrets.token_hex(8)
        with self._lock:
            # size 0: served in place, so it takes nothing from the decoded-WAV cache budget
            self._entries[key] = {"file": file_id, "size": 0, "peaks": peaks, "duration": duration, "rate": rate,
                                  "channels": channels, "fp": fp, "path": path, "stat": stat, "ident": ident}
            self._by_file[file_id] = key
            e = self._entries[key]
        return {**self._info(e), "stat": stat}

    def _prepare_copy(self, path, f, stat):
        """prepare_file() for a file with no usable identity (f: it, open; stat:
        its (size, mtime_ns)): a private copy of it in the cache, made from the
        open file and served like a decode, so nothing put at its path later can
        be served in its place. ValueError when it changed while being copied."""
        def write(out):
            f.seek(0)
            shutil.copyfileobj(f, out, 1 << 20)
            if _stat_of(os.fstat(f.fileno())) != stat:
                raise ValueError("the file changed while it was being read; try again")
        info = self.prepare(("copy", os.path.normcase(path), *stat), write=write, expected=stat[0])
        return {**info, "stat": stat}

    def _info(self, e):
        """What the player needs: the URL, peaks for a quick first drawing, the
        duration, the sample rate and channel count (short files are then drawn
        from the audio itself), and the audio fingerprint (openevp.wavinfo) of
        the decoded samples."""
        return {"url": self._url(e["file"]), "peaks": e["peaks"], "duration": e["duration"], "rate": e["rate"],
                "channels": e["channels"], "fp": e["fp"]}

    def _file_of(self, url):
        """(path, entry) of the audio a URL of this server serves; ValueError if none."""
        m = re.fullmatch(r"http://127\.0\.0\.1:\d+/([^/]+)/([0-9a-f]{16})\.wav", url or "")
        with self._lock:
            key = self._by_file.get(m.group(2)) if m and secrets.compare_digest(m.group(1), self._token) else None
            e = self._entries.get(key) if key is not None else None
            if e is None:
                raise ValueError("that audio is no longer loaded: load the recording again")
            path = e.get("path") or os.path.join(self._dir, e["file"] + ".wav")
            return path, dict(e)

    def _key_for(self, url):
        """The cache key of the audio a URL of this server serves, or None (under _lock)."""
        m = re.fullmatch(r"http://127\.0\.0\.1:\d+/([^/]+)/([0-9a-f]{16})\.wav", url or "")
        if not m or not secrets.compare_digest(m.group(1), self._token):
            return None
        return self._by_file.get(m.group(2))

    def serves(self, url):
        """Does a URL of this server serve audio now?"""
        with self._lock:
            return self._key_for(url) is not None

    def pin(self, urls):
        """Keep the audio these URLs serve in the cache, never evicted, until the next
        pin() (which replaces the set): the recording in the player and the version of
        it that plays, so making another version can never push them out."""
        with self._lock:
            self._pinned = {k for k in (self._key_for(u) for u in urls) if k is not None}

    def drop_versions(self, prefix, keep_urls=()):
        """Drop every cached entry whose key starts with prefix (the versions of one
        recording), except those keep_urls serve and pinned ones. Returns how many."""
        with self._lock:
            keep = {self._key_for(u) for u in keep_urls}
            gone = [k for k in self._entries if k[:len(prefix)] == prefix and k not in keep and k not in self._pinned]
            for k in gone:
                self._drop(k)
        return len(gone)

    def file_id(self, url):
        """The file id of a URL of this server (ValueError if it serves nothing)."""
        return self._file_of(url)[1]["file"]

    def open_audio(self, url):
        """The WAV a URL serves, opened for reading (a binary file; close it). A file
        served in place that changed on disk since it was loaded is refused
        (ValueError), as the player's requests are."""
        path, e = self._file_of(url)
        f = _open_shared(path)
        if _not_as_loaded(e, os.fstat(f.fileno())):
            f.close()
            raise ValueError("the file changed on disk: load it again")
        return f

    def open_cached(self, key):
        """The decoded WAV cached under key, opened for reading (a binary file; close
        it), or None when nothing is cached under it (nothing is decoded here)."""
        with self._lock:
            e = self._entries.get(key)
            if e is None or e.get("path"):           # a file served in place is not a decode
                return None
            path = os.path.join(self._dir, e["file"] + ".wav")
        try:
            return _open_shared(path)
        except OSError:
            return None

    def add_spectrogram(self, url, spec):
        """Keep a spectrogram of the audio at url; returns its tiles' base URL
        (<base>/<level>/<index>.png). The oldest beyond SPECTROGRAMS are dropped."""
        file_id = self.file_id(url)
        sid = secrets.token_hex(8)
        with self._lock:
            self._specs[sid] = (file_id, spec)
            while len(self._specs) > SPECTROGRAMS:
                self._specs.popitem(last=False)
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/{self._token}/spec/{sid}"

    def spectrogram_of(self, url):
        """(tiles base URL, spectrogram) already kept for the audio at url, or None."""
        file_id = self.file_id(url)
        with self._lock:
            for sid, (fid, spec) in reversed(self._specs.items()):
                if fid == file_id:
                    self._specs.move_to_end(sid)
                    return f"http://127.0.0.1:{self._httpd.server_address[1]}/{self._token}/spec/{sid}", spec
        return None

    def _tile(self, h, m):
        with self._lock:
            ok = secrets.compare_digest(m.group(1), self._token) and m.group(2) in self._specs
            spec = self._specs[m.group(2)][1] if ok else None
        png = spec.tile(int(m.group(3)), int(m.group(4))) if spec is not None and len(m.group(3)) < 4 and len(m.group(4)) < 9 else None
        if png is None:
            h.send_error(404)
            return
        h.send_response(200)
        h.send_header("Content-Type", "image/png")
        h.send_header("Content-Length", str(len(png)))
        h.send_header("Cache-Control", "max-age=3600")
        h.end_headers()
        h.wfile.write(png)

    def retarget_prefix(self, old, new):
        """A file or folder moved from `old` to `new` (same volume, so the same
        size and mtime): every file served in place from `old` or under it is
        served from its new place, under the same URL."""
        old, new = os.path.abspath(old), os.path.abspath(new)
        old_key = os.path.normcase(old)
        inner = old_key.rstrip(os.sep) + os.sep
        with self._lock:
            for key in list(self._entries):
                e = self._entries[key]
                path = e.get("path")
                if path is None:
                    continue
                k = os.path.normcase(path)
                if k != old_key and not k.startswith(inner):
                    continue
                moved = new + path[len(old):]
                new_key = ("file", os.path.normcase(moved), *key[2:])
                e["path"] = moved
                self._entries[new_key] = self._entries.pop(key)   # most recently used: kept longest
                self._by_file[e["file"]] = new_key

    def forget_files(self, paths):
        """These files were deleted: stop serving them in place and drop their
        decoded copies (never a pinned entry, which may be playing). Returns how
        many entries went."""
        keys = {os.path.normcase(os.path.abspath(p)) for p in paths}
        with self._lock:
            gone = [k for k, e in self._entries.items() if k not in self._pinned and (
                (e.get("path") is not None and os.path.normcase(e["path"]) in keys)
                or (len(k) > 1 and isinstance(k[1], str) and k[1] in keys))]
            for k in gone:
                self._drop(k)
        return len(gone)

    def forget(self, device_id):
        with self._lock:
            for key in [k for k in self._entries if k[0] == device_id]:
                self._drop(key)

    def _evict(self, keep, limit=None):
        """Drop the oldest decoded entries (never ``keep``, never a pinned one, never picked files)
        until the cache holds at most ``limit`` bytes (default: the budget)."""
        limit = self._max if limit is None else limit
        total = sum(e["size"] for e in self._entries.values())
        for key in list(self._entries):
            if total <= limit:
                break
            if key != keep and key not in self._pinned and "path" not in self._entries[key]:   # picked: no space
                total -= self._entries[key]["size"]
                self._drop(key)

    def _drop(self, key):
        e = self._entries.pop(key)
        self._by_file.pop(e["file"], None)
        if "path" in e:
            return                                   # the user's own file: never deleted
        try:
            os.remove(os.path.join(self._dir, e["file"] + ".wav"))
        except OSError:
            pass                                     # still being served; the temp dir is removed at exit

    def _handle(self, h):
        t = re.fullmatch(r"/([^/]+)/spec/([0-9a-f]{16})/(\d+)/(\d+)\.png", h.path)
        if t:
            self._tile(h, t)
            return
        m = re.fullmatch(r"/([^/]+)/([0-9a-f]{16})\.wav", h.path)
        with self._lock:
            ok = m and secrets.compare_digest(m.group(1), self._token) and m.group(2) in self._by_file
            entry = self._entries.get(self._by_file[m.group(2)]) if ok else None
        if not entry:
            h.send_error(404)
            return
        path = entry.get("path") or os.path.join(self._dir, m.group(2) + ".wav")
        try:
            f = _open_shared(path)
        except OSError:
            h.send_error(404)
            return
        with f:
            st = os.fstat(f.fileno())
            if _not_as_loaded(entry, st):
                # Not the audio that was fingerprinted (and marked): the page must load it again.
                h.send_error(409, "The file changed on disk")
                return
            size = st.st_size
            start, end, status = 0, size - 1, 200
            r = _RANGE.match(h.headers.get("Range", ""))
            if r and (r.group(1) or r.group(2)):
                if r.group(1):
                    start = int(r.group(1))
                    end = min(int(r.group(2)), end) if r.group(2) else end
                else:
                    start = max(0, size - int(r.group(2)))
                if start > end:
                    h.send_error(416)
                    return
                status = 206
            h.send_response(status)
            h.send_header("Content-Type", "audio/wav")
            h.send_header("Accept-Ranges", "bytes")
            h.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            h.end_headers()
            f.seek(start)
            left = end - start + 1
            while left:
                chunk = f.read(min(left, 1 << 16))
                if not chunk:
                    break
                h.wfile.write(chunk)
                left -= len(chunk)
