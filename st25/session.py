"""One connected recorder: Digital Voice Editor's connect sequence, folder
listings and downloads.

Not thread-safe. The desktop app uses it only on its single USB thread
(app/devices.py); the recorder abandons a transaction if requests overlap.
"""
import struct
from dataclasses import dataclass

from . import dvf
from .folder import parse
from .protocol import Recorder, RecorderError

LETTERS = "ABCDE"


def build_dvf(raw, m, label):
    """The .dvf for message m's downloaded wire data.

    Raises RecorderError if the data is not the message the folder table
    describes, and dvf.FormatError if it cannot be converted.
    """
    first = struct.unpack(">I", raw[6:10])[0]
    if first != m.start_counter:
        raise RecorderError(f"{label}: downloaded data does not match the folder table "
                            f"(counter {first:#x} != {m.start_counter:#x}); stopping")
    return dvf.build(raw, m.date, m.owner, expected_length=m.length)


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

    def close(self):
        self.rec.close()
