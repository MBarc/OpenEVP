"""Two fake recorder models that are not the ST25 (tests only).

- FakeAlpha ("fake-alpha", native ".fk1"): has a decoder. Its recordings are
  b"FAK1" + sample rate (u32 LE) + 16-bit mono PCM; the decoder wraps that in a
  WAV. Plain folder ids, integer recording numbers, an owner.
- FakeBeta ("fake-beta", native ".fk2"): no decoder, unknown durations, no
  owner, and awkward ids/labels: spaces, unicode, "..", ":", markup in a
  label, string recording numbers, a recording with a problem.

Recorders are plugged in and out with model.plug()/unplug()/reconnect();
each plug gets a new connection id, like a real replug. device.fail_with
makes the next session call raise (then clears itself); device.state makes
discovery report e.g. NEEDS_DRIVER. model.open_sessions holds the sessions
not yet closed, so tests can check nothing leaks.

install(testcase) registers fresh instances of both models and their
formats for the duration of one test. It also calls unplug_shipped(testcase),
which makes the shipped models (the real ST25) discover nothing during the
test, so a recorder plugged into the test PC never shows up in it.
"""
import io
import itertools
import os
import struct
import wave
from dataclasses import dataclass, field
from unittest import mock

from openevp import formats, recorders
from openevp.recorders import base

MAGIC = b"FAK1"
RATE = 8000


# ---- formats ---------------------------------------------------------------
def alpha_bytes(samples, rate=RATE):
    return MAGIC + struct.pack("<I", rate) + struct.pack(f"<{len(samples)}h", *samples)


class AlphaDecoder:
    """Decoder for .fk1 (always available)."""

    def available(self):
        return True

    def reason(self):
        return None

    def warning(self):
        return None

    def to_wav(self, data, should_stop=None):
        if should_stop is not None and should_stop():
            raise formats.Cancelled("stopped")
        if len(data) < 8 or data[:4] != MAGIC or (len(data) - 8) % 2:
            raise formats.DecodeError("not a .fk1 recording")
        rate = struct.unpack("<I", data[4:8])[0]
        if not rate:
            raise formats.DecodeError("a .fk1 recording with no sample rate")
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data[8:])
        return out.getvalue()


def _alpha_seconds(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        size = os.path.getsize(path)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != MAGIC:
        return None
    rate = struct.unpack("<I", head[4:8])[0]
    return round((size - 8) / 2 / rate, 1) if rate else None


FK1 = formats.Format(ext=".fk1", label="Fake Alpha original", decoder=AlphaDecoder(),
                     same=lambda existing, new: existing == new,
                     seconds=_alpha_seconds, max_bytes=16 << 20)
FK2 = formats.Format(ext=".fk2", label="Fake Beta original", decoder=None,
                     same=lambda existing, new: existing == new,
                     seconds=lambda path: None, max_bytes=1 << 20)


# ---- recorders -------------------------------------------------------------
@dataclass
class FakeRecording:
    number: object
    data: bytes = b""
    filename: str = ""
    recorded_label: str = ""
    recorded_sort: str = ""
    seconds: object = None
    owner: object = None
    problem: object = None

    def row(self):
        return {"number": self.number, "recorded_label": self.recorded_label,
                "recorded_sort": self.recorded_sort, "seconds": self.seconds,
                "owner": self.owner, "problem": self.problem}


@dataclass
class FakeFolder:
    id: str
    label: str
    safe_name: str
    recordings: list = field(default_factory=list)


@dataclass
class FakeDevice:
    folders: list
    owner: object = None
    location: str = "USB port 9"
    state: str = base.READY
    message: str = ""
    fail_with: object = None          # an exception the next session call raises
    downloads: int = 0                # recordings read off the device


class FakeSession(base.Session):
    def __init__(self, model, connection_id, device):
        self._model, self._id, self._device = model, connection_id, device
        self.closed = False

    def _check(self):
        if self.closed:
            raise base.NotReady("this session is closed")
        if self._model.devices.get(self._id) is not self._device:
            raise base.DeviceGone("the fake recorder was unplugged")
        failure, self._device.fail_with = self._device.fail_with, None
        if failure is not None:
            raise failure

    def _folder(self, folder_id):
        for f in self._device.folders:
            if f.id == folder_id:
                return f
        raise ValueError(f"no folder {folder_id!r}")

    @property
    def owner(self):
        return self._device.owner

    def folders(self):
        self._check()
        return [{"id": f.id, "label": f.label, "safe_name": f.safe_name} for f in self._device.folders]

    def recordings(self, folder_id):
        self._check()
        return [r.row() for r in self._folder(folder_id).recordings]

    def download(self, folder_id, number):
        self._check()
        folder = self._folder(folder_id)
        for r in folder.recordings:
            if r.number == number:
                label = f"{folder.label} #{number}"
                if r.problem:
                    return base.Download(label, r.filename, error=r.problem)
                self._device.downloads += 1
                return base.Download(label, r.filename, data=r.data)
        raise ValueError(f"no recording {number!r} in folder {folder_id!r}")

    def close(self):
        self.closed = True
        self._model.open_sessions.discard(self)


class FakeModel(base.Model):
    prefix = "fake"

    def __init__(self):
        self.devices = {}             # connection id -> FakeDevice
        self.open_sessions = set()
        self._ids = itertools.count(1)

    def plug(self, device=None):
        """Attach a recorder (default: a fresh sample one); returns its connection id."""
        connection_id = f"{self.prefix}#{next(self._ids)}"
        self.devices[connection_id] = device if device is not None else self.sample_device()
        return connection_id

    def unplug(self, connection_id):
        return self.devices.pop(connection_id)

    def reconnect(self, connection_id):
        """Unplug and plug the same recorder back in: a new connection id."""
        return self.plug(self.unplug(connection_id))

    def discover(self):
        return [base.DiscoveredDevice(connection_id=cid, model_id=self.model_id,
                                      location=d.location, state=d.state, message=d.message,
                                      locator=cid)
                for cid, d in self.devices.items()]

    def open(self, device):
        found = self.devices.get(device.locator)
        if found is None:
            raise base.DeviceGone("the fake recorder was unplugged")
        if found.state == base.NEEDS_DRIVER:
            raise base.DriverMissing(found.message or "no driver", advice="set up the driver")
        session = FakeSession(self, device.locator, found)
        self.open_sessions.add(session)
        return session


def _tone(n, step):
    return [((i * step) % 2000) - 1000 for i in range(n)]


class FakeAlpha(FakeModel):
    model_id = "fake-alpha"
    name = "Fake Alpha"
    usb_ids = ((0xF0F0, 0x0001),)
    needs_winusb = True
    native = FK1
    prefix = "alpha"

    @staticmethod
    def sample_device():
        def rec(folder, n, step):
            return FakeRecording(number=n, data=alpha_bytes(_tone(RATE // 2 + n, step)),
                                 filename=f"ALPHA_{folder}_{n:03d}.fk1",
                                 recorded_label=f"2026-09-0{n} 21:0{n}", recorded_sort=f"2026090{n}210{n}",
                                 seconds=0.5, owner="Test Owner")
        return FakeDevice(owner="Test Owner", folders=[
            FakeFolder("1", "Voice 1", "Voice 1", [rec("1", 1, 7), rec("1", 2, 11)]),
            FakeFolder("2", "Voice 2", "Voice 2", [rec("2", 1, 13)]),
            FakeFolder("3", "Voice 3", "Voice 3", []),
        ])


class FakeBeta(FakeModel):
    model_id = "fake-beta"
    name = "Fake Beta"
    usb_ids = ((0xF0F0, 0x0002),)
    needs_winusb = False
    native = FK2
    prefix = "beta"

    @staticmethod
    def sample_device():
        def rec(number, body, name, problem=None):
            return FakeRecording(number=number, data=b"" if problem else b"FK2\0" + body,
                                 filename=name, problem=problem)
        return FakeDevice(owner=None, location="Fake Beta volume", folders=[
            FakeFolder("folder one", "<b>Folder</b> one", "folder one",
                       [rec("rec:1", b"one", "rec 1.fk2"), rec("rec 2", b"two", "rec 2.fk2")]),
            FakeFolder("Ünïcødé ✓", "Ünïcødé ✓", "Unicode", [rec("ü/3", "drei".encode(), "ü 3.fk2")]),
            FakeFolder("..", "..", "dotdot", [rec("4", b"", "rec 4.fk2", problem="this one is damaged")]),
            FakeFolder("a:b", "a:b", "a_b", [rec("a:b:5", b"five", "rec 5.fk2")]),
        ])


def unplug_shipped(testcase):
    """For one test, the shipped models (recorders.MODELS) find no recorders."""
    for model in recorders.MODELS:
        patcher = mock.patch.object(model, "discover", return_value=[])
        patcher.start()
        testcase.addCleanup(patcher.stop)


def install(testcase):
    """Register fresh FakeAlpha and FakeBeta models and their formats for one
    test; returns (alpha, beta). Everything is unregistered in cleanup, and the
    shipped models discover nothing meanwhile (unplug_shipped)."""
    unplug_shipped(testcase)
    alpha, beta = FakeAlpha(), FakeBeta()
    for fmt in (FK1, FK2):
        formats.register(fmt)
        testcase.addCleanup(formats.unregister, fmt.ext)
    for model in (alpha, beta):
        recorders.register(model)
        testcase.addCleanup(recorders.unregister, model.model_id)
    return alpha, beta
