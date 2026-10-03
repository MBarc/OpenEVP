"""Backend events to the page, posted without ever waiting for it.

The backend's workers (exports, backups, the library indexer, clips, noise
reduction, spectrograms, sharing, an import's suggest and split jobs) report
through Api's emit(event, payload). Delivering an event means pywebview's
window.evaluate_js, which waits for the page with no time limit: a hung page
would hang the worker, and closing (which waits for the workers) with it.

So emit() only puts the event on a queue and returns; one dispatcher thread (a
daemon: it never keeps the process alive) sends them to the page in order. The
queue is bounded: when the page stops taking events, the oldest are dropped
(progress first), never the caller held up.
"""
import collections
import threading

MAX_QUEUED = 10000


class Dispatcher:
    def __init__(self, send, max_queued=MAX_QUEUED):
        self._send = send                        # (event, payload) -> None; may block
        self._queue = collections.deque()
        self._max = max_queued
        self._cond = threading.Condition()
        self._closed = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="page-events", daemon=True)
        self._thread.start()

    def emit(self, event, payload):
        """Post an event for the page; returns at once."""
        with self._cond:
            if self._closed:
                return
            if len(self._queue) >= self._max:
                self._drop_one()
            self._queue.append((event, payload))
            self._cond.notify()

    def _drop_one(self):
        for i, (event, _) in enumerate(self._queue):
            if event.endswith("-progress"):
                del self._queue[i]
                break
        else:
            self._queue.popleft()
        self.dropped += 1

    def _run(self):
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed and not self._queue:
                    return
                event, payload = self._queue.popleft()
            try:
                self._send(event, payload)
            except Exception:
                pass                             # the window is gone: nothing to tell

    def close(self, timeout=1.0):
        """No more events; what is queued is still sent if the page takes it within
        timeout. Never waits longer, whatever the page does."""
        with self._cond:
            self._closed = True
            self._cond.notify()
        self._thread.join(timeout)
