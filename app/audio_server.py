"""Serve decoded recordings to the player over http://127.0.0.1.

Only this machine can connect, and every URL carries a random per-run token.
Decoded WAVs live in a disk cache bounded by total size and are served from
disk with Range support, so long recordings stream. prepare() returns
waveform peaks and the duration, so the player never has to download and
decode a whole file just to draw it.

prepare_file() does the same for a WAV file the user picked (for example from a
recorder that writes WAV itself): it is served from where it is, never copied or
deleted, and only files registered this way can be reached.
"""
import io
import os
import re
import secrets
import threading
import wave
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

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
    """(peaks, duration) for a PCM WAV: PEAKS_PER_SECOND values in 0..1 per second of
    audio (at most MAX_PEAKS in all), each the loudest sample of any channel in its
    slice. Reads in chunks, never the whole file."""
    try:
        w = wave.open(f)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV file ({e})") from None
    with w:
        rate, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        if width not in (1, 2, 3, 4):
            raise ValueError(f"unsupported sample size ({8 * width} bit)")
        if not n or not rate:
            return [], 0.0, rate
        count = min(MAX_PEAKS, n, max(1, -(-n * PEAKS_PER_SECOND // rate)))
        per = -(-n // count)                                  # frames per peak (ceil)
        chunk = per * max(1, CHUNK_BYTES // (per * ch * width))  # whole peaks per chunk
        full = float(1 << (8 * width - 1))
        peaks = []
        while True:
            data = w.readframes(chunk)
            if not data:
                break
            mags = np.abs(_samples(data, width).reshape(-1, ch)).max(axis=1)
            peaks.extend((np.maximum.reduceat(mags, np.arange(0, len(mags), per)) / full).tolist())
        return [round(min(p, 1.0), 4) for p in peaks], n / rate, rate


class AudioServer:
    def __init__(self, provider, cache_dir, max_bytes=1 << 30):
        self._provider = provider            # (device_id, letter, number) -> WAV bytes
        self._dir = cache_dir
        self._max = max_bytes
        self._token = secrets.token_urlsafe(16)
        self._entries = OrderedDict()        # key -> {"file", "size", "peaks", "duration"}; LRU order
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

    def prepare(self, key, make=None):
        with self._lock:
            gate = self._inflight.setdefault(key, threading.Lock())
        with gate:                                   # concurrent requests wait for one decode
            with self._lock:
                e = self._entries.get(key)
                if e is not None:
                    self._entries.move_to_end(key)
                    return self._info(e)
            try:
                wav = make() if make else self._provider(key)
                peaks, duration, rate = _analyze(io.BytesIO(wav))
                file_id = secrets.token_hex(8)
                with open(os.path.join(self._dir, file_id + ".wav"), "wb") as f:
                    f.write(wav)
                with self._lock:
                    self._entries[key] = {"file": file_id, "size": len(wav), "peaks": peaks, "duration": duration,
                                          "rate": rate}
                    self._by_file[file_id] = key
                    self._evict(keep=key)
                return self._info(self._entries[key])
            finally:
                with self._lock:
                    self._inflight.pop(key, None)

    def prepare_file(self, path):
        """Register a WAV file the user picked; returns {"url", "peaks", "duration"}.
        Raises ValueError for a file that is not a playable PCM WAV."""
        path = os.path.abspath(path)
        st = os.stat(path)
        key = ("file", os.path.normcase(path), st.st_size, st.st_mtime_ns)
        with self._lock:
            e = self._entries.get(key)
            if e is not None:
                self._entries.move_to_end(key)
                return self._info(e)
        with open(path, "rb") as f:
            peaks, duration, rate = _analyze(f)
        file_id = secrets.token_hex(8)
        with self._lock:
            # size 0: served in place, so it takes nothing from the decoded-WAV cache budget
            self._entries[key] = {"file": file_id, "size": 0, "peaks": peaks, "duration": duration, "rate": rate,
                                  "path": path}
            self._by_file[file_id] = key
        return self._info(self._entries[key])

    def _info(self, e):
        """What the player needs: the URL, peaks for a quick first drawing, the
        duration, and the sample rate (short files are then drawn from the audio itself)."""
        return {"url": self._url(e["file"]), "peaks": e["peaks"], "duration": e["duration"], "rate": e["rate"]}

    def forget(self, device_id):
        with self._lock:
            for key in [k for k in self._entries if k[0] == device_id]:
                self._drop(key)

    def _evict(self, keep):
        total = sum(e["size"] for e in self._entries.values())
        for key in list(self._entries):
            if total <= self._max:
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
            f = open(path, "rb")
        except OSError:
            h.send_error(404)
            return
        with f:
            size = os.fstat(f.fileno()).st_size
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
