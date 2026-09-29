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
modification time are recorded when it is fingerprinted; if either differs when
the player asks for audio, the request is refused (409), so the player never gets
different audio under a handle whose marks belong to the fingerprinted audio.
The file is not copied to snapshot it: WAVs can be gigabytes. Files are opened
so that they can still be moved, renamed or recycled while they are being read
(Windows FILE_SHARE_DELETE); retarget_prefix() then points a served file at its
new place, and its URL keeps working.
"""
import errno
import hashlib
import os
import re
import secrets
import sys
import threading
import wave
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from openevp import wavinfo

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


def _analyze(f):
    """(peaks, duration, rate, fp) for a PCM WAV: PEAKS_PER_SECOND values in 0..1 per
    second of audio (at most MAX_PEAKS in all), each the loudest sample of any channel
    in its slice, plus the sample rate and the audio fingerprint (openevp.wavinfo) of the
    decoded samples. Reads in chunks, never the whole file. A WAV with no samples
    has fp None: every empty WAV of one format would otherwise share one identity
    (and one set of marks), so it gets none and cannot be marked."""
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
            h.update(data)
            mags = np.abs(_samples(data, width).reshape(-1, ch)).max(axis=1)
            peaks.extend((np.maximum.reduceat(mags, np.arange(0, len(mags), per)) / full).tolist())
        if total != expected:
            raise ValueError("the WAV file is truncated")
        return [round(min(p, 1.0), 4) for p in peaks], n / rate, rate, h.hexdigest()


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


# The decoded-WAV cache's budget on disk. The longest ICD-ST10 recording (its
# 32 MB of flash, about 92 minutes of 44.1 kHz stereo) decodes to about 930 MB,
# so this holds two of those, or many hours of ICD-ST25 audio (8 kHz mono). The
# entry just prepared is always kept, so the cache can exceed the budget by at
# most that one file; older entries are evicted first.
CACHE_BYTES = 2 << 30
CACHE_PREFIX = "st25-audio-"           # the app's cache folder in the temp folder: <prefix><random>


IN_USE = ".in-use"                     # held open by the app that owns a cache folder
STALE_AFTER = 60                       # seconds: an unmarked folder younger than this may be starting up


def hold_cache(folder):
    """Mark a cache folder as in use for as long as the returned file stays open:
    Windows refuses to delete an open file, so clean_stale_caches() in another
    OpenEVP leaves this folder alone. Close it before removing the folder."""
    return open(os.path.join(folder, IN_USE), "wb")


def clean_stale_caches(parent, keep=None):
    """Delete cache folders (CACHE_PREFIX*) that earlier runs left in ``parent``
    (the temp folder) after a crash or a power cut: they can hold gigabytes of
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
        if not name.startswith(CACHE_PREFIX) or (keep and os.path.normcase(path) == os.path.normcase(keep)):
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
        self._httpd = None

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server._handle(self)

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

    def prepare(self, key, make=None, write=None, expected=None):
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
        entry is evicted and the decode is tried once more."""
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
                        peaks, duration, rate, fp = _analyze(f)
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
            stat = _stat_of(os.fstat(f.fileno()))
            key = ("file", os.path.normcase(path), *stat)
            with self._lock:
                e = self._entries.get(key)
                if e is not None:
                    self._entries.move_to_end(key)
                    return {**self._info(e), "stat": e["stat"]}
            peaks, duration, rate, fp = _analyze(f)
            channels = _channels(f)
            if _stat_of(os.fstat(f.fileno())) != stat or _stat_of(os.stat(path)) != stat:
                raise ValueError("the file changed while it was being read; try again")
        file_id = secrets.token_hex(8)
        with self._lock:
            # size 0: served in place, so it takes nothing from the decoded-WAV cache budget
            self._entries[key] = {"file": file_id, "size": 0, "peaks": peaks, "duration": duration, "rate": rate,
                                  "channels": channels, "fp": fp, "path": path, "stat": stat}
            self._by_file[file_id] = key
            e = self._entries[key]
        return {**self._info(e), "stat": stat}

    def _info(self, e):
        """What the player needs: the URL, peaks for a quick first drawing, the
        duration, the sample rate and channel count (short files are then drawn
        from the audio itself), and the audio fingerprint (openevp.wavinfo) of
        the decoded samples."""
        return {"url": self._url(e["file"]), "peaks": e["peaks"], "duration": e["duration"], "rate": e["rate"],
                "channels": e["channels"], "fp": e["fp"]}

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

    def forget(self, device_id):
        with self._lock:
            for key in [k for k in self._entries if k[0] == device_id]:
                self._drop(key)

    def _evict(self, keep, limit=None):
        """Drop the oldest decoded entries (never ``keep``, never picked files)
        until the cache holds at most ``limit`` bytes (default: the budget)."""
        limit = self._max if limit is None else limit
        total = sum(e["size"] for e in self._entries.values())
        for key in list(self._entries):
            if total <= limit:
                break
            if key != keep and "path" not in self._entries[key]:   # picked files use no cache space
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
            if "stat" in entry and _stat_of(st) != entry["stat"]:
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
            h.send_header("Access-Control-Allow-Origin", "*")   # the UI is served from another local port
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
