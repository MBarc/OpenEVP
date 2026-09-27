"""The shared recorder contract: checks any Model/Session implementation
against openevp.recorders.base.

Not a test module by itself (no test_ prefix). Mix it into a TestCase:

    class MyModelContract(RecorderContract, unittest.TestCase):
        def setUp(self):
            self.model = ...            # the Model under test
            ...                         # make sure it discovers one ready recorder

The mixin only uses the interface: discover(), open(), the session calls and
the model's native Format. Every session it opens is closed in cleanup.
"""
import io
import wave

from openevp import formats
from openevp.recorders import base


class RecorderContract:
    model = None                        # set by the concrete test's setUp

    # ---- helpers ----------------------------------------------------------
    def ready_device(self):
        ready = [d for d in self.model.discover() if d.state == base.READY]
        self.assertTrue(ready, "the contract needs one ready recorder")
        return ready[0]

    def open_session(self):
        session = self.model.open(self.ready_device())
        self.addCleanup(session.close)
        return session

    def listing(self, session):
        """[(folder, [recording])] for the whole recorder."""
        return [(f, session.recordings(f["id"])) for f in session.folders()]

    # ---- identity ---------------------------------------------------------
    def test_contract_identity(self):
        m = self.model
        self.assertIsInstance(m, base.Model)
        self.assertIsInstance(m.model_id, str)
        self.assertTrue(m.model_id)
        self.assertIsInstance(m.name, str)
        self.assertTrue(m.name)
        self.assertIs(m.supported, True)
        self.assertIsInstance(m.needs_winusb, bool)
        for pair in m.usb_ids:
            self.assertEqual(len(pair), 2)
            for n in pair:
                self.assertIsInstance(n, int)
                self.assertTrue(0 <= n <= 0xFFFF)
        self.assertIsInstance(m.native, formats.Format)
        self.assertIs(formats.by_ext(m.native.ext), m.native, "the native format must be registered")

    def test_contract_discovery(self):
        found = self.model.discover()
        self.assertIsInstance(found, list)
        ids = [d.connection_id for d in found]
        self.assertEqual(len(ids), len(set(ids)), "connection ids must be unique")
        for d in found:
            self.assertIsInstance(d, base.DiscoveredDevice)
            self.assertIsInstance(d.connection_id, str)
            self.assertTrue(d.connection_id)
            self.assertEqual(d.model_id, self.model.model_id)
            self.assertIsInstance(d.location, str)
            self.assertIn(d.state, base.STATES)
            self.assertIsInstance(d.message, str)

    # ---- listing ----------------------------------------------------------
    def test_contract_folders(self):
        session = self.open_session()
        owner = session.owner
        self.assertTrue(owner is None or isinstance(owner, str))
        folders = session.folders()
        self.assertIsInstance(folders, list)
        self.assertTrue(folders, "a recorder has at least one folder")
        for f in folders:
            self.assertEqual(set(f), set(base.FOLDER_FIELDS))
            self.assertIsInstance(f["id"], str)
            self.assertIsInstance(f["label"], str)
            self.assertTrue(base.safe_name_ok(f["safe_name"]), f"unsafe safe_name {f['safe_name']!r}")
        self.assertEqual(len({f["id"] for f in folders}), len(folders), "folder ids must be unique")
        self.assertEqual(len({f["safe_name"].casefold() for f in folders}), len(folders),
                         "safe names must be unique, ignoring case")

    def test_contract_recordings(self):
        session = self.open_session()
        for _folder, recs in self.listing(session):
            self.assertIsInstance(recs, list)
            for r in recs:
                self.assertEqual(set(r), set(base.RECORDING_FIELDS))
                self.assertIsInstance(r["number"], (int, str))
                self.assertNotIsInstance(r["number"], bool)
                self.assertIsInstance(r["recorded_label"], str)
                self.assertIsInstance(r["recorded_sort"], str)
                self.assertTrue(r["seconds"] is None or isinstance(r["seconds"], (int, float)))
                self.assertTrue(r["owner"] is None or isinstance(r["owner"], str))
                self.assertTrue(r["problem"] is None or isinstance(r["problem"], str))
            numbers = [r["number"] for r in recs]
            self.assertEqual(len(numbers), len(set(numbers)), "numbers must be unique in a folder")

    def test_contract_listing_is_stable(self):
        session = self.open_session()
        self.assertEqual(self.listing(session), self.listing(session))

    # ---- downloads --------------------------------------------------------
    def test_contract_downloads(self):
        session = self.open_session()
        seen = 0
        for folder, recs in self.listing(session):
            names = set()
            for r in recs:
                dl = session.download(folder["id"], r["number"])
                self.assertIsInstance(dl, base.Download)
                self.assertIsInstance(dl.label, str)
                self.assertTrue(base.safe_name_ok(dl.filename), f"unsafe file name {dl.filename!r}")
                self.assertTrue(dl.filename.lower().endswith(self.model.native.ext))
                self.assertNotEqual(bool(dl.data), bool(dl.error), "data XOR error")
                self.assertEqual(bool(r["problem"]), bool(dl.error),
                                 "a recording has a problem exactly when its download has an error")
                self.assertNotIn(dl.filename.casefold(), names, "file names must be unique in a folder")
                names.add(dl.filename.casefold())
                if dl.data:
                    self.assertTrue(self.model.native.same(dl.data, dl.data))
                    again = session.download(folder["id"], r["number"])
                    self.assertEqual(again.data, dl.data)
                    self.assertEqual(again.filename, dl.filename)
                seen += 1
        self.assertTrue(seen, "the contract needs at least one recording")

    def test_contract_unknown_ids(self):
        session = self.open_session()
        folders = session.folders()
        missing = "\0not a folder\0"
        self.assertNotIn(missing, [f["id"] for f in folders])
        with self.assertRaises(ValueError):
            session.recordings(missing)
        with self.assertRaises(ValueError):
            session.download(missing, 1)
        with self.assertRaises(ValueError):
            session.download(folders[0]["id"], "\0no such recording\0")

    def test_contract_closed_session_is_not_ready(self):
        session = self.model.open(self.ready_device())
        folder = session.folders()[0]["id"]
        session.close()
        for call in (session.folders, lambda: session.recordings(folder),
                     lambda: session.download(folder, 1)):
            with self.assertRaises(base.NotReady):
                call()

    # ---- decoder ----------------------------------------------------------
    def first_data(self, session):
        for folder, recs in self.listing(session):
            for r in recs:
                dl = session.download(folder["id"], r["number"])
                if dl.data:
                    return dl.data
        self.fail("the contract needs one downloadable recording")

    def test_contract_decoder(self):
        decoder = self.model.native.decoder
        if decoder is None:
            self.skipTest(f"{self.model.model_id} has no decoder")
        available = decoder.available()
        self.assertIsInstance(available, bool)
        reason, warning = decoder.reason(), decoder.warning()
        self.assertTrue(reason is None or isinstance(reason, str))
        self.assertTrue(warning is None or isinstance(warning, str))
        session = self.open_session()
        data = self.first_data(session)
        if not available:
            self.assertTrue(reason, "an unavailable decoder says why")
            with self.assertRaises(formats.DecoderUnavailable):
                decoder.to_wav(data)
            return
        self.assertIsNone(reason)
        wav = decoder.to_wav(data)
        with wave.open(io.BytesIO(wav)) as w:       # PCM WAV (wave reads PCM only)
            self.assertGreater(w.getframerate(), 0)
            self.assertGreater(w.getnframes(), 0)
        self.assertEqual(decoder.to_wav(data, should_stop=lambda: False), wav)
        with self.assertRaises(formats.Cancelled):
            decoder.to_wav(data, should_stop=lambda: True)
        with self.assertRaises(formats.DecodeError):
            decoder.to_wav(b"\0 not a recording \0")
