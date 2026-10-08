"""Connected recorders, with all recorder I/O on one thread.

A recorder may abandon a transaction if the host is late or overlaps requests
(the ST25 does), so every recorder operation (discovery included) is queued
onto a single worker thread and runs strictly one at a time. The UI polls
refresh() only after its previous poll returned, so polls never pile up
behind a download.

Recorders are found by openevp.recorders.discover_all() and opened through
their model (openevp.recorders.base). Each is identified by its opaque
connection id; a replug gets a new id, and on_removed lets caches drop the
old one. What an error does to a recorder's state and session is
base.state_for()'s table: the manager applies it and re-raises.

A recorder is shown as the model its open session says it is
(Session.model_id: an ICD-ST10 is discovered and opened by the ICD-ST25
model, which shares its USB id), and as the discovering model until then; a
new recorder is opened by the refresh that finds it, so it is listed as its
own model from the start (the discovering model only if that open fails). The
discovering model still opens it again and decides, when its discovery fails,
that it is kept.

Two errors are sticky beyond that table:
- base.NotReady says the session is invalid until the recorder is replugged,
  so that connection is latched in NEEDS_REPLUG: no new session is opened on
  it (every request raises NotReady again) until it disappears from
  discovery; the replug is a new connection id with a fresh entry. Any other
  RecorderError (sony_icd's RecorderError/UsbError/TableError) only closes the
  session, and the next request opens a new one, exactly as in v0.7.2.
- a connection discovery rejects (base.RejectedConnection: e.g. two models
  claim its id) is closed and forgotten like an unplugged one, even when its
  model's discovery also failed; a plain discovery failure keeps its model's
  recorders as they are.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from openevp import recorders
from openevp.recorders import base
from openevp.recorders.base import NEEDS_DRIVER, NEEDS_REPLUG, READY  # noqa: F401 - the app's state names


GONE_KEPT = 64                              # removed recorders remembered for "was unplugged" messages


class DeviceGone(Exception):
    """The connection id is not (or no longer) attached."""


@dataclass
class _Entry:
    device: base.DiscoveredDevice
    model: base.Model             # what the recorder is shown and treated as (see _relabel)
    state: str = READY
    message: str = ""
    session: object = None
    latched: bool = False         # a NotReady: unusable until this connection disappears
    opener: base.Model = None     # the model that discovered it: it opens the recorder

    def __post_init__(self):
        if self.opener is None:
            self.opener = self.model


def _where(device):
    """The recorder's place in a message, after "the recorder" (see DiscoveredDevice.where)."""
    if device.where:
        return device.where
    return f"({device.location})" if device.location else ""


def _open_with_model(model, device):
    return model.open(device)


class DeviceManager:
    def __init__(self, discover=None, open_device=None, on_removed=None):
        # () -> ([DiscoveredDevice], [(model_id, exception)])
        self._discover = discover or recorders.discover_all
        # (model, DiscoveredDevice) -> base.Session
        self._open = open_device or _open_with_model
        self._on_removed = on_removed or (lambda device_id: None)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recorder-io")
        self._devices = {}                      # connection id -> _Entry; touched only on the pool thread
        self._gone = OrderedDict()              # connection id -> why it went, for recent removals (messages)
        self._closed = False

    def _call(self, fn, *args):
        return self._pool.submit(fn, *args).result()

    def refresh(self, problems=False):
        """[device row]: the attached recorders. With problems, ([device row],
        [(model or None, exception)]): also the models whose discovery failed
        (the other models' recorders are still listed)."""
        rows, failed = self._call(self._refresh)
        return (rows, failed) if problems else rows

    def with_session(self, device_id, fn):
        """Run fn(session) on the worker thread, opening the recorder if needed."""
        return self._call(self._with_session, device_id, fn)

    def model(self, device_id):
        """The recorder model of an attached recorder; raises DeviceGone."""
        return self._call(self._model, device_id)

    def close(self):
        """Close every session, then stop the worker (once; later calls do nothing)."""
        if self._closed:
            return
        self._closed = True
        self._call(self._close_all)
        self._pool.shutdown()

    # ---- pool thread only -------------------------------------------------
    def _refresh(self):
        found, problems = self._discover()
        # Rejected connections are known to be unusable (not merely unlisted):
        # they are dropped below whatever else their models reported.
        rejected = {exc.connection_id for _model_id, exc in problems
                    if isinstance(exc, base.RejectedConnection)}
        present = {}
        for d in found:
            if d.connection_id in rejected:
                continue
            model = recorders.get(d.model_id)
            if model is None:
                problems.append((d.model_id, ValueError(f"no recorder model {d.model_id!r}")))
                continue
            present[d.connection_id] = (d, model)
        # A model whose discovery failed (e.g. libusb could not be loaded) says nothing
        # about its recorders: they are kept as they are (sessions, caches, list rows),
        # as when the whole listing failed, and only the problem is reported.
        failed = {model_id for model_id, exc in problems if not isinstance(exc, base.RejectedConnection)}
        kept = [i for i, e in self._devices.items()
                if i not in present and i not in rejected and e.opener.model_id in failed]
        for device_id in list(self._devices):
            if device_id not in present and device_id not in kept:
                e = self._devices.pop(device_id)
                where = _where(e.device)
                the = f"the recorder {where}" if where else "the recorder"
                self._gone[device_id] = (f"{the} cannot be used: more than one recorder reports its connection"
                                         if device_id in rejected else f"{the} was unplugged")
                while len(self._gone) > GONE_KEPT:
                    self._gone.popitem(last=False)
                self._close(e)
                self._on_removed(device_id)
        for device_id, (d, model) in present.items():
            e = self._devices.get(device_id)
            if e is None:
                self._devices[device_id] = _Entry(d, model, d.state, d.message)
                if d.state == READY:             # open it now, so it is listed as its own model at once
                    try:
                        self._with_session(device_id, lambda s: None)
                    except Exception:            # its state says why; the next request tries again
                        pass
                continue
            e.device = d
            if d.state != READY:                 # discovery knows it cannot be used (e.g. no driver)
                e.state, e.message = d.state, d.message
                self._close(e)
        return ([self._describe(device_id) for device_id in [*present, *kept]],
                [(recorders.get(model_id), exc) for model_id, exc in problems])

    def _describe(self, device_id):
        e = self._devices[device_id]
        owner = (e.session.owner or "") if e.session is not None else ""
        return {"id": device_id, "model_id": e.model.model_id, "model": e.model.name,
                "port": e.device.location, "state": e.state, "message": e.message, "owner": owner}

    def _entry(self, device_id):
        e = self._devices.get(device_id)
        if e is None:
            why = self._gone.get(device_id) if isinstance(device_id, str) else None
            raise DeviceGone(why or "the recorder was unplugged")
        return e

    def _model(self, device_id):
        return self._entry(device_id).model

    def _with_session(self, device_id, fn):
        e = self._entry(device_id)
        try:
            if e.device.state == NEEDS_DRIVER:   # never opened: it has no driver on this PC
                raise base.DriverMissing(e.device.message or "This recorder's driver is not set up on this PC.")
            if e.device.state == NEEDS_REPLUG:
                raise base.NotReady(e.device.message or "This recorder must be plugged in again.")
            if e.latched:                    # a NotReady: only a replug (a new connection) helps
                raise base.NotReady(e.message or "This recorder must be plugged in again.")
            if e.session is None:
                e.session = self._open(e.opener, e.device)
                self._relabel(e)
            result = fn(e.session)
        except Exception as ex:
            state, close = base.state_for(ex)
            if state is not None and not e.latched:
                e.state, e.message = state, str(ex)
            if isinstance(ex, base.NotReady):
                e.latched = True
            if close:
                self._close(e)
            raise
        e.state, e.message = READY, ""
        return result

    @staticmethod
    def _relabel(e):
        """Show the recorder as the supported model its new session says it is
        (Session.model_id), else as the model that discovered it."""
        model_id = getattr(e.session, "model_id", None)
        model = recorders.get(model_id) if isinstance(model_id, str) and model_id else None
        e.model = model if model is not None and model.supported else e.opener

    def _close(self, e):
        if e.session is not None:
            try:
                e.session.close()
            finally:
                e.session = None

    def _close_all(self):
        for e in self._devices.values():
            self._close(e)
