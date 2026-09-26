"""Connected recorders, with all recorder I/O on one thread.

The ST25 abandons a transaction if the host is late or overlaps requests, so
every USB operation (device listing included) is queued onto a single worker
thread and runs strictly one at a time. The UI polls refresh() only after its
previous poll returned, so polls never pile up behind a download.

Connections are identified by st25.usb connection ids ("<port>@<address>");
a replug gets a new id, and on_removed lets caches drop the old one.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from st25.folder import TableError
from st25.protocol import RecorderError
from st25.usb import DriverMissing, UsbError

READY, NEEDS_DRIVER, NEEDS_REPLUG = "ready", "needs_driver", "needs_replug"
SETUP_PREFIX = "setup:"      # ids of recorders Windows sees but libusb cannot (app/pnp.py)
SETUP_MESSAGE = "This recorder's driver is not set up on this PC yet."


class DeviceGone(Exception):
    pass


@dataclass
class _Entry:
    state: str = READY
    message: str = ""
    session: object = None


class DeviceManager:
    def __init__(self, enumerate_fn, open_fn, on_removed=None):
        self._enumerate = enumerate_fn          # () -> [connection id]
        self._open = open_fn                    # connection id -> RecorderSession
        self._on_removed = on_removed or (lambda device_id: None)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="st25-usb")
        self._devices = {}                      # id -> _Entry; touched only on the pool thread

    def _call(self, fn, *args):
        return self._pool.submit(fn, *args).result()

    def refresh(self):
        return self._call(self._refresh)

    def with_session(self, device_id, fn):
        """Run fn(session) on the USB thread, opening the recorder if needed."""
        return self._call(self._with_session, device_id, fn)

    def close(self):
        self._call(self._close_all)
        self._pool.shutdown()

    # ---- pool thread only -------------------------------------------------
    def _refresh(self):
        present = self._enumerate()
        for device_id in list(self._devices):
            if device_id not in present:
                self._close(self._devices.pop(device_id))
                self._on_removed(device_id)
        for device_id in present:
            if device_id.startswith(SETUP_PREFIX):
                self._devices.setdefault(device_id, _Entry(NEEDS_DRIVER, SETUP_MESSAGE))
            else:
                self._devices.setdefault(device_id, _Entry())
        return [self._describe(d) for d in present]

    def _describe(self, device_id):
        e = self._devices[device_id]
        owner = e.session.owner if e.session is not None else ""
        port = "" if device_id.startswith(SETUP_PREFIX) else device_id.split("@")[0]
        return {"id": device_id, "port": port, "state": e.state,
                "message": e.message, "owner": owner}

    def _with_session(self, device_id, fn):
        e = self._devices.get(device_id)
        if e is None:
            raise DeviceGone(f"the recorder on USB port {device_id.split('@')[0]} was unplugged")
        if device_id.startswith(SETUP_PREFIX):
            raise DriverMissing(SETUP_MESSAGE)
        try:
            if e.session is None:
                e.session = self._open(device_id)
            result = fn(e.session)
        except DriverMissing as ex:
            e.state, e.message = NEEDS_DRIVER, str(ex)
            raise
        except (RecorderError, UsbError, TableError) as ex:
            e.state, e.message = NEEDS_REPLUG, str(ex)
            self._close(e)
            raise
        e.state, e.message = READY, ""
        return result

    def _close(self, e):
        if e.session is not None:
            try:
                e.session.close()
            finally:
                e.session = None

    def _close_all(self):
        for e in self._devices.values():
            self._close(e)
