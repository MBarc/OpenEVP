"""The recorder interface: what a supported voice recorder model provides.

Adding a recorder means one module in openevp/recorders/ with a Model
subclass and a Session subclass, listed in openevp.recorders.MODELS. The app
never talks to a recorder any other way, and never sends raw commands: each
model enforces its own read-only command policy inside its transport.

Model (one per supported model, a registry entry)
    model_id      stable id, e.g. "sony-icd-st25"
    name          display name, e.g. "Sony ICD-ST25"
    usb_ids       ((vid, pid), ...) it answers to; no two models may claim one
    needs_winusb  whether the app's driver setup binds it to WinUSB (its usb_ids
                  go into the driver INF: tools/make_driver_manifest.py)
    winusb_name   the device name that driver shows in Device Manager
                  (default "<name> - WinUSB"); plain ASCII, no quotes or %
    native        its openevp.formats.Format (the file type it downloads);
                  the format must be registered in openevp.formats
    wav_problem() why none of its recordings can be converted to WAV
                  (played, marked) now, or None; by default its format's
                  (openevp.formats.decoder_problem). One recording's own
                  reason is its row's play_problem (Session.recordings)
    supported     False for a placeholder (a planned model with no code yet:
                  no usb_ids, no driver, never discovered or opened)
    discover()    -> [DiscoveredDevice]: the recorders of this model attached
                  now, including ones that cannot be used yet (state)
    open(device)  -> Session for a DiscoveredDevice this model returned

Models that share a USB id (the Sony ICD-ST25 and ICD-ST10) are discovered
and opened by one of them; the session then says what the recorder is
(Session.model_id) and the app shows and treats it as that model from then on.

Session (one connected recorder; used only on the app's single device thread)
    owner                    owner name the recorder reports, or None
    model_id                 the registered model the recorder says it is, when
                             that is not the model that opened it; None (the
                             default) means the opening model
    folders()                -> [{"id", "label", "safe_name"}]
    recordings(folder_id)    -> [{"number", "recorded_label", "recorded_sort",
                                  "seconds", "owner", "problem"[, "play_problem"]}]
    download(folder_id, number) -> Download
    close()                  release the device (the app calls it exactly once)

Ids and labels are opaque. Folder ids and recording numbers (int or str) are
only ever handed back to the same session; the app checks them against the
session's own listing and never builds a path from them. Labels are display
text, shown as plain text. The one thing that becomes part of a path is a
folder's ``safe_name`` (the export subfolder; must pass safe_name_ok() and be
unique ignoring case) and a Download's ``filename`` (must pass safe_name_ok()
and end with the native extension). An unknown folder id or recording number
raises ValueError.

Recording rows are for display and are built by the model: ``recorded_label``
is the text shown for when it was recorded; when that is unknown it is the
model's own text for it (the ST25 shows "undated") or "" to show nothing.
``recorded_sort`` is a string that sorts in recording order ("" when unknown). No datetime is forced on a model, so a
recorder's own dates are shown as it stores them. ``seconds`` may be None
(unknown). ``problem`` is None or "" for a recording that can be downloaded,
otherwise why it cannot (its download then carries the same kind of error).
``play_problem`` (optional) is None, "" or absent for a recording that can be
played when its model can (Model.wav_problem), otherwise why this one cannot
(e.g. its codec has no decoder): it is still downloaded and saved.

Errors and what the app does with them (state_for() is the one mapping):

    exception              device state     session
    NotReady               NEEDS_REPLUG     closed: invalid, the user replugs; no
                                            new session is opened on that
                                            connection until it disappears
    RecorderError          NEEDS_REPLUG     closed: the connection failed
    DeviceGone             NEEDS_REPLUG     closed: it is no longer attached
    DriverMissing          NEEDS_DRIVER     kept (none could be opened)
    Cancelled              unchanged        kept
    anything else          unchanged        kept (e.g. ValueError: a bad id)

A problem with one recording is not an exception: download() returns a
Download with ``error`` set, and a batch export carries on with the next one.
Decoding and saving never run inside a session call: the app downloads the
native bytes first and converts or writes them after the call returns.
"""
import re
from dataclasses import dataclass

from openevp import formats
from openevp.formats import Cancelled  # noqa: F401 - part of the recorder vocabulary

READY, NEEDS_DRIVER, NEEDS_REPLUG = "ready", "needs_driver", "needs_replug"
STATES = (READY, NEEDS_DRIVER, NEEDS_REPLUG)

FOLDER_FIELDS = ("id", "label", "safe_name")
RECORDING_FIELDS = ("number", "recorded_label", "recorded_sort", "seconds", "owner", "problem")
OPTIONAL_RECORDING_FIELDS = ("play_problem",)


# ---- errors ---------------------------------------------------------------------
class RecorderError(Exception):
    """The recorder failed; ``message`` says what happened, ``advice`` what
    the user can do about it ("" when there is nothing to add)."""

    def __init__(self, message, advice=""):
        super().__init__(message)
        self.message = message
        self.advice = advice


class NotReady(RecorderError):
    """The session is no longer valid; the recorder must be replugged."""


class DriverMissing(RecorderError):
    """The recorder is attached but its driver is not set up on this PC."""


class DeviceGone(RecorderError):
    """The recorder is no longer attached."""


class RejectedConnection(ValueError):
    """Discovery refused a connection id (e.g. two models claimed it): it is
    ambiguous, so it is not a recorder the app may use. Unlike a model whose
    discovery failed, the connection is known to be unusable: the app closes
    it and forgets it. ``connection_id`` is the refused id."""

    def __init__(self, message, connection_id):
        super().__init__(message)
        self.connection_id = connection_id


def state_for(exc):
    """(new device state or None to leave it, whether to close the session)
    for an exception raised by Model.open or a Session call."""
    if isinstance(exc, DriverMissing):
        return NEEDS_DRIVER, False
    if isinstance(exc, RecorderError):          # NotReady, DeviceGone, connection failures
        return NEEDS_REPLUG, True
    return None, False


# ---- records --------------------------------------------------------------------
@dataclass(frozen=True)
class DiscoveredDevice:
    """One attached recorder, as a model's discover() found it.

    ``connection_id`` is opaque and unique among all attached recorders; it
    changes when the recorder is reconnected. ``location`` is display text
    chosen by the model, saying where the recorder is ("port 1-4" for an ST25
    on USB port 1-4, "E: drive"); the app shows it as is, in parentheses
    ("Sony ICD-ST25 #1 (port 1-4)"), and "" means nothing to show.
    ``message`` explains a state other than READY.
    ``locator`` is private to the model (whatever its open() needs).
    ``where`` optionally names the place in the app's messages, read after
    "the recorder" ("on USB port 1-4" for an ST25: "the recorder on USB port
    1-4 was unplugged"); "" means "(<location>)", or nothing without a location."""
    connection_id: str
    model_id: str
    location: str
    state: str = READY
    message: str = ""
    locator: object = None
    where: str = ""

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"unknown device state {self.state!r}")


@dataclass(frozen=True)
class Download:
    """One recording read off the recorder: ``data`` (the native file's
    bytes) or ``error`` (why it cannot be saved), never both, never neither.
    ``filename`` is the native file's name; ``label`` names the recording in
    messages ("A-007")."""
    label: str
    filename: str
    data: bytes = b""
    error: str = ""

    def __post_init__(self):
        if bool(self.data) == bool(self.error):
            raise ValueError("a Download has either data or an error")


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows device names, also with an extension and with spaces before it ("CON .txt").
_RESERVED = re.compile(r"(?i)(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³]) *(\..*)?")
MAX_SAFE_NAME = 120


def safe_name_ok(name):
    """Whether name is usable as one Windows file or folder name as is."""
    return (isinstance(name, str) and 0 < len(name) <= MAX_SAFE_NAME
            and name not in (".", "..") and not _UNSAFE.search(name)
            and name == name.strip() and not name.endswith(".")
            and not _RESERVED.fullmatch(name))


# ---- the interface --------------------------------------------------------------
class Session:
    """A connected recorder. See the module docstring for the contract."""

    @property
    def owner(self):
        return None

    @property
    def model_id(self):
        return None

    def folders(self):
        raise NotImplementedError

    def recordings(self, folder_id):
        raise NotImplementedError

    def download(self, folder_id, number):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class Model:
    """A recorder model. Subclass it and set the attributes; see the module
    docstring. A placeholder sets only model_id, name and supported = False
    and keeps the default discover()/open()."""
    model_id = ""
    name = ""
    usb_ids = ()
    needs_winusb = False
    winusb_name = None
    native = None
    supported = True

    def discover(self):
        return []

    def open(self, device):
        raise RecorderError(f"{self.name} is not supported yet.")

    def wav_problem(self):
        """Why this model's recordings cannot be converted to WAV (played,
        marked) now, as a phrase, or None when they can."""
        if self.native is None:
            return f"{self.name} is not supported yet"
        return formats.decoder_problem(self.native)

    def transfer_progress(self):
        """How far the download running on this model's recorder is (0 to 1),
        or None if it cannot tell. Read from another thread while it runs."""
        return None

    def __repr__(self):
        return f"<{type(self).__name__} {self.model_id}>"
