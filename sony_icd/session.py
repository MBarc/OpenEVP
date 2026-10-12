"""One connected recorder: Digital Voice Editor's connect sequence, folder
listings and downloads.

Not thread-safe. The desktop app uses it only on its single USB thread
(app/devices.py); the recorder abandons a transaction if requests overlap.
"""
import struct
from dataclasses import dataclass

from . import dvf
from .folder import parse
from .protocol import Recorder, RecorderError, folder_counts
from .usb import UsbError

LETTERS = "ABCDE"


def build_dvf(raw, m, label):
    """The .dvf for message m's downloaded wire data.

    Raises RecorderError if the data is not the message the folder table
    describes, and dvf.FormatError if it cannot be converted.

    The first block's time counter must match the table's. On an ICD-ST10
    whose clock was never set both are 0xFFFFFFFF, so that check proves
    nothing there; its LPEC ST data is checked instead by dvf.build: the
    table's length, and whole frames with consecutive counters and each
    block's frame offset where the frames say it must be.
    """
    first = struct.unpack(">I", raw[6:10])[0]
    if first != m.start_counter:
        raise RecorderError(f"{label}: downloaded data does not match the folder table "
                            f"(counter {first:#x} != {m.start_counter:#x}); stopping")
    return dvf.build(raw, m.date, m.owner, expected_length=m.length, mode=m.mode)


@dataclass
class Download:
    label: str            # "A-007"
    name: str             # the .dvf file name (DVE's naming)
    dvf: bytes = b""      # empty when error is set
    error: str = ""       # why this recording cannot be saved


class RecorderSession:
    def __init__(self, recorder):
        self.rec = recorder
        self.model = ""
        self._tables = {}         # letter -> [Message]
        self._downloads = {}      # (letter, number) -> Download

    @classmethod
    def open(cls, device_id=None):
        rec = Recorder(device_id)
        try:
            s = cls(rec)
            s.connect()
            return s
        except BaseException:
            rec.close()
            raise

    def connect(self):
        info = self.rec.device_info()
        self.model = info[36:52].split(b"\0")[0].decode("latin-1", "replace")
        # Digital Voice Editor reads these on connect; keep the same sequence.
        self.rec.read_block(0x1E0, 0)
        self.rec.read_block(0x1E0, 0x1E0)
        self.rec.info_03()
        self.rec.target_status()

    @property
    def owner(self):
        for msgs in self._tables.values():
            for m in msgs:
                if m.owner:
                    return m.owner
        return ""

    def messages(self, letter):
        if letter not in LETTERS:
            raise ValueError(f"no folder {letter!r}")
        if letter not in self._tables:
            self._tables[letter] = parse(self.rec.folder_table(LETTERS.index(letter) + 1))
        return self._tables[letter]

    def download(self, letter, number):
        key = (letter, number)
        if key in self._downloads:
            return self._downloads[key]
        msgs = self.messages(letter)
        if not 1 <= number <= len(msgs):
            raise ValueError(f"no recording {letter}-{number:03d}")
        m = msgs[number - 1]
        label = f"{letter}-{number:03d}"
        name = dvf.filename(letter, m.number, m.owner, m.date, m.dated)
        if m.problem:
            dl = Download(label, name, error=m.problem)
        else:
            # GET_VOICE names the folder itself; reading its table does not select it.
            raw = self.rec.voice_data(LETTERS.index(letter) + 1, m.number, m.blocks)
            try:
                dl = Download(label, name, dvf=build_dvf(raw, m, label))
            except dvf.FormatError as e:
                dl = Download(label, name, error=str(e))
        self._downloads[key] = dl
        return dl

    def delete(self, letter, number, slot, counter, length):
        """Delete recording ``number`` of folder ``letter`` from the recorder, if it is still the
        one the user picked (slot, start counter, length). Re-reads the counts and the table
        before (nothing is sent if they differ) and after (the count must be one lower and the
        others unchanged). Returns its label. Raises ValueError with a sentence
        for the user when it refuses; RecorderError when the outcome is unclear."""
        if letter not in LETTERS:
            raise ValueError(f"no folder {letter!r}")
        fi = LETTERS.index(letter) + 1
        label = f"{letter}-{number:03d}"
        self._forget(letter)
        count = folder_counts(self.rec.target_status())[fi]
        msgs = self.messages(letter)
        m = msgs[number - 1] if 1 <= number <= len(msgs) else None
        if count != len(msgs) or m is None or (m.slot, m.start_counter, m.length) != (slot, counter, length):
            raise ValueError(f"{label} changed on the recorder; refresh and try again.")
        unclear = (f"{label} may have been deleted; the recorder stopped answering. Unplug it, "
                   "plug it back in, and check the folder before deleting again.")
        try:
            status = self.rec.delete_message(fi, number)
        except (RecorderError, UsbError) as e:      # the frame may have gone out: outcome unknown
            raise RecorderError(unclear) from e
        finally:
            self._forget(letter)
        if status == 1:
            raise ValueError("Replace the recorder's batteries, then try again.")
        if status:
            raise ValueError(f"The recorder refused (status {status}). Nothing else was deleted.")
        try:
            now = self.messages(letter)
            now_count = folder_counts(self.rec.target_status())[fi]
        except (RecorderError, UsbError) as e:
            raise RecorderError(unclear) from e

        def ident(ms):
            return [(x.start_counter, x.length) for x in ms]
        if now_count != count - 1 or len(now) != count - 1:
            raise ValueError(f"After deleting {label} the recorder lists {len(now)} recording"
                             f"{'' if len(now) == 1 else 's'} in folder {letter}, not {count - 1}. "
                             "Check the folder before deleting more.")
        if ident(now) != ident(msgs[:number - 1] + msgs[number:]):
            raise ValueError(f"After deleting {label} the recorder lists different recordings in folder "
                             f"{letter} than expected. Check the folder before deleting more.")
        return label

    def _forget(self, letter):
        """Drop a folder's cached table and downloads: numbers shift after a delete."""
        self._tables.pop(letter, None)
        for key in [k for k in self._downloads if k[0] == letter]:
            del self._downloads[key]

    def close(self):
        self.rec.close()
