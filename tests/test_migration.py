"""An upgrade from v0.7.2 keeps everything (spec A13).

tests/migration/v0.7.2/ holds settings.json, marks.json and index.json as
OpenEVP v0.7.2 wrote them (made by v0.7.2's own store, see
tests/migration/make_v0_7_2_fixture.py), over synthetic recordings built here
from tests/vectors/: the backed-up .dvf of a marked recorder recording and its
marked WAV copy, another marked WAV, and a file that could not be read.

Loading that folder keeps every mark, reviewed and imported flag, backup
status and backup path, setting and index entry; the recordings' marks show
when they are played through the app's Api and in the library listing (from
the cached fingerprints, nothing decoded again); a marked WAV export carries
the same markers. The .dvf is also played and exported through the real
decoder (a golden test: it fails in the release gate when the decoder can't
run).
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from fixtures import st25_manager  # noqa: E402
from test_marks_api import FakeServer  # noqa: E402
from app import backend  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import wavinfo  # noqa: E402
from st25 import audio  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "migration", "v0.7.2")
VECTORS = os.path.join(HERE, "vectors")
MTIME_NS = 1_874_000_000_000_000_000
BROKEN = b"not a recording" * 10            # as make_v0_7_2_fixture.py wrote it

FP_A001 = "5f86e4302eda8b5df22263ad13da9232d764d467dbb027e1231483d63bc81a4c"   # tone-440-quiet
FP_CELL3 = "08a6fafb1974bee3ec76281548b0afea1e90c73954797e97c90b3fa62fc33722"  # steps
FP_A002 = "4cc5ba43d73c5072636fab4109a8efa41ef25cb616f859a518d1b89b50dfa596"   # white-noise
FP_REVIEWED = "b092172f2ae0522c33fd61010242e8e9ca3e1651a5e181f8df839bddd5228609"  # silence

DVF_1 = "001_A_001_Casey_2029_05_23.dvf"
WAV_1 = "001_A_001_Casey_2029_05_23.wav"


def wav_of(vector):
    """The canonical WAV of a vector's reference decode (what dvf_to_wav gives)."""
    with open(os.path.join(VECTORS, vector + ".pcm"), "rb") as f:
        pcm = f.read()
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
                       1, 1, 8000, 16000, 2, 16, b"data", len(pcm)) + pcm


def labels(marks):
    """A mark list as the RIFF labels OpenEVP writes: [(start, end, "EVP <cls>[: note]")]."""
    return [(m["start"], m["end"], f"EVP {m['cls']}" + (f": {m['note']}" if m["note"] else "")) for m in marks]


def marker_labels(path):
    return [(round(m["start"], 3), round(m["end"], 3), m["note"]) for m in wavinfo.read_markers(path)]


@unittest.skipUnless(sys.platform == "win32", "v0.7.2 ran on Windows: its stored paths are Windows paths")
class V072AppDataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "Root")
        self.save = os.path.join(self.root, "OpenEVP")
        self.appdata = os.path.join(self.root, "AppData")
        os.makedirs(self.appdata)
        self.fixture = {}
        for name in ("settings.json", "marks.json", "index.json"):
            with open(os.path.join(FIXTURE, name), encoding="utf-8") as f:
                text = f.read()
            root = os.path.normcase(self.root) if name == "index.json" else self.root
            text = text.replace("%ROOT%", json.dumps(root)[1:-1])
            with open(os.path.join(self.appdata, name), "w", encoding="utf-8") as f:
                f.write(text)
            self.fixture[name] = json.loads(text)
        self.recordings = self.fixture["marks.json"]["recordings"]
        self.write_library()

    def write_library(self):
        """The files the fixture's backups and index name, as v0.7.2 left them."""
        os.makedirs(os.path.join(self.save, "A"))
        os.makedirs(os.path.join(self.save, "Old Jail"))
        self.dvf1 = os.path.join(self.save, "A", DVF_1)
        self.wav1 = os.path.join(self.save, "A", WAV_1)
        self.cell3 = os.path.join(self.save, "Old Jail", "cell 3.wav")
        broken = os.path.join(self.save, "Old Jail", "broken.dvf")
        shutil.copyfile(os.path.join(VECTORS, "tone-440-quiet.dvf"), self.dvf1)
        files = {self.wav1: wavinfo.with_markers(wav_of("tone-440-quiet"), self.recordings[FP_A001]["marks"]),
                 self.cell3: wav_of("steps"), broken: BROKEN}
        for path, data in files.items():
            with open(path, "wb") as f:
                f.write(data)
        for path in (self.dvf1, *files):
            os.utime(path, ns=(MTIME_NS, MTIME_NS))

    def open_store(self):
        store = AppData(self.appdata)
        self.addCleanup(store.close)
        return store

    def new_api(self, store, pick_wav=None):
        manager = st25_manager(lambda: [], lambda device_id: None)
        self.addCleanup(manager.close)
        self.events = []
        self.library_done = threading.Event()

        def emit(event, payload):
            self.events.append((event, payload))
            if event == "library-done":
                self.library_done.set()
        api = backend.Api(manager, emit, lambda start: None, os.path.join(self.root, "elsewhere"), FakeServer(),
                          pick_wav=pick_wav, store=store)
        self.addCleanup(api.shutdown)
        return api

    def fixture_bytes(self):
        out = {}
        for name in ("settings.json", "marks.json", "index.json"):
            with open(os.path.join(self.appdata, name), "rb") as f:
                out[name] = f.read()
        return out

    # ---- the store --------------------------------------------------------------

    def test_every_record_survives_loading(self):
        before = self.fixture_bytes()
        store = self.open_store()
        self.assertEqual((store.problems(), store.read_only), ([], False))
        self.assertEqual(store.get_setting("save_folder"), self.save)
        self.assertEqual(store.get_setting("library_folder"), self.save)
        self.assertEqual(set(store.summary()), set(self.recordings))
        for fp, rec in self.recordings.items():
            got = store.recording(fp)
            self.assertEqual(got["marks"], sorted(rec["marks"], key=lambda m: m["start"]), fp)
            self.assertEqual((got["name"], got["duration"], got["reviewed"], got["imported"]),
                             (rec["name"], rec["duration"], rec["reviewed"], rec["imported"]), fp)
            self.assertEqual(store.backup_record(fp), rec["backup"], fp)
        self.assertEqual(store.backup_record(FP_A001)["paths"], [self.dvf1, self.wav1])
        self.assertEqual(store.backup(FP_A002)["status"], "failed")
        self.assertEqual(store.saved_backups(), {FP_A001: [self.dvf1, self.wav1]})
        self.assertEqual((store.is_reviewed(FP_REVIEWED), store.marks(FP_REVIEWED)), (True, []))
        for key, entry in self.fixture["index.json"]["files"].items():
            self.assertEqual(store.cached_fp(key, entry["size"], entry["mtime_ns"]),
                             {"fp": None, "seconds": None, **entry}, key)
        store.flush_index()
        self.assertEqual(self.fixture_bytes(), before)            # loading rewrote nothing

    # ---- through the app ------------------------------------------------------------

    def test_the_library_lists_the_marks_from_the_cached_fingerprints(self):
        store = self.open_store()
        api = self.new_api(store)
        self.assertEqual(api.default_destination(), self.save)       # the Save-to setting
        r = api.list_library()
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["folder"], r["pending"], r["indexing"]), (self.save, 0, False))   # nothing to decode
        rows = {(f["investigation"], f["name"]): f for f in r["files"]}
        self.assertEqual(set(rows), {("A", DVF_1), ("A", WAV_1), ("Old Jail", "cell 3.wav"),
                                     ("Old Jail", "broken.dvf")})
        for key in (("A", DVF_1), ("A", WAV_1)):
            row = rows[key]
            self.assertEqual((row["fp"], row["marks"], row["reviewed"], row["notes"], row["error"]),
                             (FP_A001, {"A": 1, "B": 1, "C": 0}, True, "get out", None), key)
        cell = rows[("Old Jail", "cell 3.wav")]
        self.assertEqual((cell["fp"], cell["marks"], cell["reviewed"], cell["notes"]),
                         (FP_CELL3, {"A": 0, "B": 0, "C": 2}, False, "whisper?\nknock"))
        self.assertEqual(rows[("Old Jail", "broken.dvf")]["error"], "broken.dvf is not a Sony ICD-ST25 recording.")
        self.assertEqual(api.library_marks(rows[("A", DVF_1)]["id"])["marks"], store.marks(FP_A001))
        self.assertEqual(self.fixture_bytes()["marks.json"].count(b'"id"'), 5)   # no mark added or lost

    def test_playing_shows_the_marks_and_a_marked_export_carries_them(self):
        store = self.open_store()
        api = self.new_api(store)
        rows = {f["name"]: f for f in api.list_library()["files"]}

        r = api.play_library(rows[WAV_1]["id"])
        self.assertTrue(r["ok"], r)
        want = self.recordings[FP_A001]
        self.assertEqual((r["fp"], r["marks"], r["reviewed"], r["imported"]),
                         (FP_A001, want["marks"], True, 0))              # its embedded markers not imported twice
        self.assertEqual(r["backup"], {"status": "saved", "detail": want["backup"]["detail"]})
        self.assertFalse(r["backup_needed"])
        # Exported with its marks, it is byte for byte the WAV copy v0.7.2's backup wrote.
        self.assertEqual(api.export_marked(r["rec"]),
                         {"ok": True, "saved": False, "already": True, "name": WAV_1, "folder_name": "A"})
        self.assertEqual(marker_labels(self.wav1), labels(want["marks"]))

        r = api.play_library(rows["cell 3.wav"]["id"])
        cell = self.recordings[FP_CELL3]
        self.assertEqual((r["fp"], r["marks"], r["reviewed"]), (FP_CELL3, cell["marks"], False))
        out = api.export_marked(r["rec"])
        self.assertEqual(out, {"ok": True, "saved": True, "already": False, "name": "cell 3 (2).wav",
                               "folder_name": "Old Jail"})
        self.assertEqual(marker_labels(os.path.join(self.save, "Old Jail", out["name"])), labels(cell["marks"]))
        self.assertEqual(wavinfo.read_markers(self.cell3), [])            # the user's file is untouched

    def test_a_wav_opened_from_the_file_dialog_shows_its_marks(self):
        api = self.new_api(self.open_store(), pick_wav=lambda start: self.cell3)
        r = api.open_wav()
        self.assertEqual((r["ok"], r["fp"], r["marks"], r["imported"]),
                         (True, FP_CELL3, self.recordings[FP_CELL3]["marks"], 0))

    @release_gate.require(audio.available(), "the LPEC decoder is not available")
    def test_the_dvf_decodes_to_the_same_recording_and_exports_the_same_wav(self):
        store = self.open_store()
        api = self.new_api(store)
        rows = {f["name"]: f for f in api.list_library()["files"]}
        r = api.play_library(rows[DVF_1]["id"])
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["fp"], r["marks"], r["reviewed"]),
                         (FP_A001, self.recordings[FP_A001]["marks"], True))
        self.assertEqual(api.export_marked(r["rec"]),
                         {"ok": True, "saved": False, "already": True, "name": WAV_1, "folder_name": "A"})


if __name__ == "__main__":
    unittest.main()
