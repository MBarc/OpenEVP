"""Sony ICD-ST25: the recorder interface over the sony_icd package.

An adapter only: the USB transport, its read-only command policy
(sony_icd/usb.py, sony_icd/policy.py), the protocol, the folder tables and the .dvf
container are sony_icd's, unchanged. The session wraps sony_icd.session.RecorderSession
privately and never offers a transfer method of its own.

What the app sees:
- discovery: every recorder on USB id 054C:0103 (an ICD-ST25 or ICD-ST10)
  libusb can open (connection id "<port>@<address>", a new one on every
  replug) plus a NEEDS_DRIVER placeholder ("setup:<Windows
  instance id>") for each such instance Windows lists without the WinUSB
  driver (openevp.pnp.needs_setup: read per instance; counted only if
  Windows cannot say); location = "port <USB port>" ("port 1-4", shown by the
  app as "(port 1-4)" as it always has), "" for a placeholder.
- folders A..E: id "A".., label "Folder A".., safe_name "A".. (the export
  subfolders).
- recordings: the folder table's messages, numbered 1.. as on the recorder;
  recorded_label is the date as the app has always shown it ("2029-05-23
  19:54:04", "undated"; stored dates are shown as stored, 31 February too),
  recorded_sort the same text ("" when undated); play_problem is why that
  recording's codec cannot be decoded now (an ICD-ST10 mixes LPEC LP, SP
  and ST recordings, so this is per recording, not per model), or None.
- downloads: the .dvf Digital Voice Editor would save, under its file name.
- model_id: the recorder's own identify string picks the model it is shown as
  (MODEL_IDS: an ICD-ST10 answers to the same USB id and protocol). Any other
  string is treated as an ST25, as it always was, with a log note.

Errors (sony_icd -> shared vocabulary, so state_for() gives the app states it has
always used):

    sony_icd exception                      raised as        state / session
    sony_icd.usb.DriverMissing              DriverMissing    needs_driver / kept
    sony_icd.protocol.RecorderError (incl.  RecorderError    needs_replug / closed
      RecorderStuck), sony_icd.usb.UsbError,
      sony_icd.folder.TableError
    a "setup:" placeholder opened           DriverMissing    needs_driver / kept
    a call on a closed session              NotReady         needs_replug / closed
    ValueError (unknown folder/number)      ValueError       unchanged / kept

Messages are the sony_icd exception's text unchanged; advice is left to the app.
"""
import logging

from openevp import formats, pnp as _pnp
from openevp.recorders import base
from sony_icd import dvf as _dvf
from sony_icd.folder import TableError
from sony_icd.protocol import PID, VID, RecorderError as _IcdRecorderError
from sony_icd import protocol as _protocol
from sony_icd.session import LETTERS, RecorderSession
from sony_icd.usb import DriverMissing as _IcdDriverMissing
from sony_icd.usb import UsbError, list_devices

SETUP_PREFIX = _pnp.SETUP_PREFIX
# The identify strings of the recorders this module reads, and the model each is shown as.
MODEL_IDS = {"ICD-ST25": "sony-icd-st25", "ICD-ST10": "sony-icd-st10"}
_log = logging.getLogger(__name__)
SETUP_MESSAGE = "This recorder's driver is not set up on this PC yet."   # shown in the recorder list


def _translated(e):
    """The shared-vocabulary exception for a sony_icd one, or None to let it through."""
    if isinstance(e, _IcdDriverMissing):
        return base.DriverMissing(str(e))
    if isinstance(e, (_IcdRecorderError, UsbError, TableError)):
        return base.RecorderError(str(e))
    return None


def _call(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        mapped = _translated(e)
        if mapped is None:
            raise
        raise mapped from e


def _recording(m):
    """One recording row; the display matches what the app has always shown."""
    when = m.when() if m.dated else ""          # the stored date as is, e.g. "2029-02-31 ..."
    seconds = m.seconds()                       # None for a mode OpenEVP does not know
    codec = _dvf.codec_of_mode(m.mode)
    play = None if m.problem or codec is None else formats.codec_problem(codec)
    return {"number": m.number, "recorded_label": when or "undated", "recorded_sort": when,
            "seconds": None if seconds is None else round(seconds, 1), "owner": m.owner, "problem": m.problem,
            "play_problem": play}


class SonyIcdSession(base.Session):
    def __init__(self, session):
        self._session = session         # sony_icd RecorderSession; private, never handed out
        self._closed = False
        identity = getattr(session, "model", "")
        if identity not in MODEL_IDS:
            _log.info("recorder identifies as %r: read as an ICD-ST25", identity)

    def _check(self):
        if self._closed:
            raise base.NotReady("this recorder connection is closed")

    @staticmethod
    def _letter(folder_id):
        if not isinstance(folder_id, str) or folder_id not in tuple(LETTERS):
            raise ValueError(f"no folder {folder_id!r}")
        return folder_id

    @property
    def owner(self):
        return self._session.owner or None

    @property
    def model_id(self):
        return MODEL_IDS.get(getattr(self._session, "model", ""), SonyST25.model_id)

    def folders(self):
        self._check()
        return [{"id": l, "label": f"Folder {l}", "safe_name": l} for l in LETTERS]

    def recordings(self, folder_id):
        self._check()
        letter = self._letter(folder_id)
        return [_recording(m) for m in _call(self._session.messages, letter)]

    def download(self, folder_id, number):
        self._check()
        letter = self._letter(folder_id)
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"no recording {number!r} in folder {letter}")
        dl = _call(self._session.download, letter, number)
        if dl.error:
            return base.Download(dl.label, dl.name, error=dl.error)
        return base.Download(dl.label, dl.name, data=dl.dvf)

    def close(self):
        if not self._closed:
            self._closed = True
            self._session.close()


class SonyST25(base.Model):
    model_id = "sony-icd-st25"
    name = "Sony ICD-ST25"
    usb_ids = ((VID, PID),)
    needs_winusb = True
    winusb_name = "Sony IC Recorder (ST) - WinUSB"     # the driver's device name since v0.5.0
    native = formats.DVF

    def discover(self):
        ids = _call(list_devices, VID, PID)
        usable = [base.DiscoveredDevice(connection_id=i, model_id=self.model_id,
                                        location="port " + i.split("@")[0], locator=i,
                                        where="on USB port " + i.split("@")[0]) for i in ids]
        setup = [base.DiscoveredDevice(connection_id=i, model_id=self.model_id, location="",
                                       state=base.NEEDS_DRIVER, message=SETUP_MESSAGE)
                 for i in _pnp.needs_setup(_pnp.present_instances(self.usb_ids), len(ids))]
        return usable + setup

    def open(self, device):
        return open_session(self, device)

    def transfer_progress(self):
        return transfer_progress()


def transfer_progress():
    """Model.transfer_progress for the recorders this module reads."""
    got, size = _protocol.TRANSFER
    return got / size if size else None


def open_session(model, device):
    """Model.open for the recorders this module reads (the ICD-ST10's too):
    device must be one ``model`` discovered."""
    if device.model_id != model.model_id:
        raise ValueError(f"{device.connection_id!r} is not a {model.name}")
    if device.connection_id.startswith(SETUP_PREFIX) or device.locator is None:
        raise base.DriverMissing(SETUP_MESSAGE)
    return SonyIcdSession(_call(RecorderSession.open, device.locator))

