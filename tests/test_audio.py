import io
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sony_icd import audio  # noqa: E402


class AudioTests(unittest.TestCase):
    def test_unavailable_without_decoder(self):
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": None}):
            self.assertFalse(audio.available())
            self.assertIn("not included in this build", audio.status())
            with self.assertRaises(audio.DecoderUnavailable):
                audio.dvf_to_wav(b"x")

    def test_broken_decoder_is_reported_not_hidden(self):
        broken = ModuleNotFoundError("No module named 'numpy'", name="numpy")
        with mock.patch.object(audio.importlib, "import_module", side_effect=broken):
            self.assertFalse(audio.available())
            self.assertIn("could not be loaded", audio.status())
            self.assertIn("numpy", audio.status())

    def test_delegates_to_decoder(self):
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data: b"RIFF" + data
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            self.assertTrue(audio.available())
            self.assertIsNone(audio.status())
            self.assertEqual(audio.dvf_to_wav(b"x"), b"RIFFx")

    def test_tables_missing_is_reported_not_hidden(self):
        """openevp.decoders.sony_lpec imports fine (the code exists) but its extracted table
        data does not: this must surface as "could not be loaded", the same
        as any other decoder failure, not silently claim availability until
        the first export or playback blows up."""
        class FakeTablesMissing(RuntimeError):
            pass

        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data: b"RIFF" + data
        fake.TablesMissing = FakeTablesMissing

        def check():
            raise FakeTablesMissing("openevp/decoders/sony_lpec/data/lpec_tables.json not found")
        fake.check = check
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            self.assertFalse(audio.available())
            self.assertIn("could not be loaded", audio.status())
            self.assertIn("lpec_tables.json", audio.status())
            with self.assertRaises(audio.DecoderUnavailable):
                audio.dvf_to_wav(b"x")

    def test_missing_check_does_not_block_availability(self):
        """A decoder module with no check() (e.g. an older stub) is still
        reported as available from dvf_to_wav alone."""
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data: b"RIFF" + data
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            self.assertTrue(audio.available())

    def test_frozen_build_without_the_fast_decoder_reports_slow_mode(self):
        """A built app always ships lpec_core.dll; if it fails to load the
        decoder still works (pure Python) but status() says so."""
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data: b"RIFF" + data
        fake.fast_decoder_available = lambda: False
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            with mock.patch.object(sys, "frozen", True, create=True):
                self.assertTrue(audio.available())
                self.assertIn("slow mode", audio.status())
                self.assertEqual(audio.dvf_to_wav(b"x"), b"RIFFx")
            self.assertIsNone(audio.status())         # from source: not a warning
            fake.fast_decoder_available = lambda: True
            with mock.patch.object(sys, "frozen", True, create=True):
                self.assertIsNone(audio.status())

    def test_should_stop_is_passed_and_cancellation_translated(self):
        class FakeCancelled(Exception):
            pass

        def dvf_to_wav(data, should_stop=None):
            if should_stop():
                raise FakeCancelled("stopped")
            return b"RIFF" + data
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = dvf_to_wav
        fake.Cancelled = FakeCancelled
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            self.assertEqual(audio.dvf_to_wav(b"x", should_stop=lambda: False), b"RIFFx")
            with self.assertRaises(audio.Cancelled):
                audio.dvf_to_wav(b"x", should_stop=lambda: True)

    def test_tables_missing_message_is_plain(self):
        """What reaches the UI names no path and no developer tooling."""
        from openevp.decoders import sony_lpec
        from openevp.decoders.sony_lpec import tables
        missing = tables.Path(os.path.dirname(__file__)) / "no-such-dir" / "lpec_tables.json"
        with mock.patch.object(sony_lpec, "check", lambda: tables.load(missing)):
            msg = audio.status()
        self.assertEqual(msg, "the WAV decoder could not be loaded: "
                              "this build does not include the LPEC table data")



def st10_header():
    """The first bytes of an ICD-ST10 .dvf: enough for sony_icd.dvf.codec() (0x24)."""
    from sony_icd import dvf
    h = bytearray(dvf._TEMPLATES[dvf.MODE_ST])
    return bytes(h)


class St10DispatchTests(unittest.TestCase):
    """A .dvf is decoded by the decoder for its codec byte: LPEC ST (0x24) by
    openevp.decoders.sony_lpec_st, anything else by the LP decoder."""

    def fakes(self):
        class FakeCancelled(Exception):
            pass
        lp = types.ModuleType("openevp.decoders.sony_lpec")
        lp.dvf_to_wav = lambda data, should_stop=None: b"LP" + bytes(data[:2])
        st = types.ModuleType("openevp.decoders.sony_lpec_st")
        st.Cancelled = FakeCancelled
        st.dvf_to_wav = lambda data, should_stop=None: b"ST"

        def write(data, f, should_stop=None):
            f.write(b"ST-streamed")
            return 11
        st.dvf_write_wav = write

        def pcm(data, should_stop=None):
            def chunks():
                yield b"\x01\x00\x02\x00"
                if should_stop is not None and should_stop():
                    raise FakeCancelled("stopped")
                yield b"\x03\x00\x04\x00"
            return 2, 2, 44100, chunks()
        st.dvf_pcm = pcm
        return lp, st

    def test_dispatch_by_codec(self):
        lp, st = self.fakes()
        with mock.patch.dict(sys.modules, {lp.__name__: lp, st.__name__: st}):
            self.assertEqual(audio.dvf_to_wav(b"MS_VOICE-not-a-header"), b"LPMS")
            self.assertEqual(audio.dvf_to_wav(st10_header()), b"ST")
            self.assertTrue(audio.available(audio.CODEC_ST))
            self.assertIsNone(audio.status(audio.CODEC_ST))

    def test_streamed_forms(self):
        lp, st = self.fakes()
        with mock.patch.dict(sys.modules, {lp.__name__: lp, st.__name__: st}):
            f = io.BytesIO()
            self.assertEqual(audio.dvf_write_wav(st10_header(), f), 11)
            self.assertEqual(f.getvalue(), b"ST-streamed")
            f = io.BytesIO()
            self.assertEqual(audio.dvf_write_wav(b"x", f), 3)           # LP: made, then written
            self.assertEqual(f.getvalue(), b"LPx")
            self.assertIsNone(audio.dvf_pcm(b"x"))                      # LP cannot stream
            channels, width, rate, chunks = audio.dvf_pcm(st10_header())
            self.assertEqual((channels, width, rate, b"".join(chunks)), (2, 2, 44100, b"\x01\x00\x02\x00\x03\x00\x04\x00"))
            with self.assertRaises(audio.Cancelled):
                b"".join(audio.dvf_pcm(st10_header(), should_stop=lambda: True)[3])

    def test_a_build_without_the_st_decoder(self):
        lp, _st = self.fakes()
        with mock.patch.dict(sys.modules, {lp.__name__: lp, "openevp.decoders.sony_lpec_st": None}):
            self.assertTrue(audio.available())                          # LP still converts
            self.assertFalse(audio.available(audio.CODEC_ST))
            self.assertEqual(audio.status(audio.CODEC_ST), "LPEC ST (ICD-ST10) playback is not included in this build")
            with self.assertRaises(audio.DecoderUnavailable):
                audio.dvf_to_wav(st10_header())
            with self.assertRaises(audio.DecoderUnavailable):
                audio.dvf_write_wav(st10_header(), io.BytesIO())
            self.assertEqual(audio.dvf_to_wav(b"x"), b"LPx")

    def test_st_tables_missing_message_is_plain(self):
        from openevp.decoders import sony_lpec_st
        missing = os.path.join(os.path.dirname(__file__), "no-such-dir", "lpec_st_tables.json")
        with mock.patch.object(sony_lpec_st, "check", lambda: sony_lpec_st.tables.load(missing)):
            self.assertEqual(audio.status(audio.CODEC_ST), "the LPEC ST decoder could not be loaded: "
                                                           "this build does not include the LPEC ST table data")

    def test_slow_mode_for_the_st_decoder(self):
        lp, st = self.fakes()
        st.fast_decoder_available = lambda: False
        with mock.patch.dict(sys.modules, {lp.__name__: lp, st.__name__: st}),                 mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(audio.available(audio.CODEC_ST))
            self.assertEqual(audio.status(audio.CODEC_ST), audio.SLOW_MODE)
            self.assertIsNone(audio.status())                           # the LP fake has no C core flag

if __name__ == "__main__":
    unittest.main()
