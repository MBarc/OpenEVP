import io
import os
import sys
import tempfile
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fakes import fake_models  # noqa: E402
from fixtures import DATE, make_raw  # noqa: E402
from recorder_contract import RecorderContract  # noqa: E402

from openevp import formats, recorders  # noqa: E402
from openevp.recorders import base  # noqa: E402
from st25 import audio, dvf  # noqa: E402
from st25.export import save_dvf, target_path  # noqa: E402


def wav_bytes(frames=b"\x01\x00\x02\x00", rate=8000):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return out.getvalue()


class Placeholder(base.Model):
    model_id = "vendor-planned"
    name = "Vendor Planned"
    supported = False


class Other(base.Model):
    """A supported model that claims whatever USB id it is given."""
    native = formats.by_ext(".wav")

    def __init__(self, model_id, usb_ids, found=()):
        self.model_id, self.name, self.usb_ids, self._found = model_id, model_id, usb_ids, found

    def discover(self):
        return [base.DiscoveredDevice(cid, self.model_id, "here") for cid in self._found]


# ---- the model registry ------------------------------------------------------
class RegistryTests(unittest.TestCase):
    def test_built_in_models_are_consistent(self):
        recorders.check(recorders.models())
        self.assertEqual(recorders.check(recorders.MODELS), list(recorders.MODELS))

    def test_register_lookup_and_unregister(self):
        alpha, beta = fake_models.install(self)
        self.assertIn(alpha, recorders.models())
        self.assertIn(beta, recorders.supported())
        self.assertIs(recorders.get("fake-alpha"), alpha)
        self.assertIs(recorders.find(0xF0F0, 0x0002), beta)
        self.assertIsNone(recorders.find(0xF0F0, 0x0003))
        self.assertIsNone(recorders.get("nope"))
        recorders.unregister("fake-alpha")
        self.assertIsNone(recorders.get("fake-alpha"))
        recorders.register(alpha)                   # cleanup unregisters it again

    def test_ambiguous_usb_ids_are_rejected(self):
        alpha, _beta = fake_models.install(self)
        before = recorders.models()
        with self.assertRaisesRegex(ValueError, "f0f0:0001"):
            recorders.register(Other("copycat", ((0xF0F0, 0x0001),)))
        self.assertEqual(recorders.models(), before)
        with self.assertRaises(ValueError):          # the import-time check of the static list
            recorders.check([alpha, Other("copycat", ((0xF0F0, 0x0001),))])
        with self.assertRaises(ValueError):          # one model claiming an id twice
            recorders.check([Other("twice", ((1, 2), (1, 2)))])

    def test_duplicate_model_ids_are_rejected(self):
        fake_models.install(self)
        with self.assertRaisesRegex(ValueError, "fake-alpha"):
            recorders.register(Other("fake-alpha", ((5, 5),)))

    def test_native_format_must_be_registered(self):
        unregistered = formats.Format(ext=".nope", label="x", same=bytes.__eq__, seconds=lambda p: None)
        model = Other("x", ())
        model.native = unregistered
        with self.assertRaisesRegex(ValueError, "not registered"):
            recorders.check([model])

    def test_malformed_models_are_rejected(self):
        for bad in (Other("", ((1, 1),)), Other("x", ((1, 0x10000),)), Other("x", ((1,),))):
            with self.subTest(bad=bad.model_id), self.assertRaises(ValueError):
                recorders.check([bad])
        no_format = Other("x", ())
        no_format.native = None
        with self.assertRaises(ValueError):
            recorders.check([no_format])

    def test_placeholders_are_listed_but_unsupported(self):
        fake_models.unplug_shipped(self)
        recorders.register(Placeholder())
        self.addCleanup(recorders.unregister, Placeholder.model_id)
        self.assertIn("vendor-planned", [m.model_id for m in recorders.models()])
        self.assertNotIn("vendor-planned", [m.model_id for m in recorders.supported()])
        self.assertEqual(Placeholder().discover(), [])
        with self.assertRaises(base.RecorderError):
            Placeholder().open(base.DiscoveredDevice("x", "vendor-planned", ""))
        self.assertEqual(recorders.discover_all(), ([], []))

    def test_placeholders_claim_no_usb_ids_or_driver(self):
        class Claims(Placeholder):
            usb_ids = ((1, 2),)

        class Driver(Placeholder):
            needs_winusb = True

        for cls in (Claims, Driver):
            with self.subTest(cls=cls.__name__), self.assertRaises(ValueError):
                recorders.check([cls()])

    def test_discover_all_merges_models(self):
        alpha, beta = fake_models.install(self)
        a, b = alpha.plug(), beta.plug()
        recorders.register(Placeholder())            # never asked to discover
        self.addCleanup(recorders.unregister, Placeholder.model_id)
        found, problems = recorders.discover_all()
        self.assertEqual([(d.connection_id, d.model_id) for d in found],
                         [(a, "fake-alpha"), (b, "fake-beta")])
        self.assertEqual(problems, [])

    def test_one_broken_model_does_not_hide_the_others(self):
        alpha, beta = fake_models.install(self)
        a = alpha.plug()
        beta.plug()
        failure = OSError("libusb is missing")
        with mock.patch.object(beta, "discover", side_effect=failure):
            found, problems = recorders.discover_all()
        self.assertEqual([d.connection_id for d in found], [a])
        self.assertEqual(problems, [("fake-beta", failure)])

    def test_clashing_connection_ids_are_dropped_and_reported(self):
        alpha, _beta = fake_models.install(self)
        a = alpha.plug()
        recorders.register(Other("o1", ((1, 1),), found=["same", "mine"]))
        self.addCleanup(recorders.unregister, "o1")
        recorders.register(Other("o2", ((1, 2),), found=["same"]))
        self.addCleanup(recorders.unregister, "o2")
        found, problems = recorders.discover_all()
        self.assertEqual([d.connection_id for d in found], [a, "mine"])
        self.assertEqual([m for m, _e in problems], ["o1", "o2"])
        for _m, e in problems:
            self.assertIsInstance(e, base.RejectedConnection)        # a ValueError the app drops, not keeps
            self.assertEqual(e.connection_id, "same")
            self.assertIn("same", str(e))


# ---- records and the error table ---------------------------------------------
class BaseTests(unittest.TestCase):
    def test_download_is_data_xor_error(self):
        self.assertEqual(base.Download("A-001", "a.dvf", data=b"x").data, b"x")
        self.assertEqual(base.Download("A-001", "a.dvf", error="bad").error, "bad")
        with self.assertRaises(ValueError):
            base.Download("A-001", "a.dvf")
        with self.assertRaises(ValueError):
            base.Download("A-001", "a.dvf", data=b"x", error="bad")

    def test_error_to_state_table(self):
        cases = [
            (base.NotReady("stale"), (base.NEEDS_REPLUG, True)),
            (base.RecorderError("broke", advice="replug"), (base.NEEDS_REPLUG, True)),
            (base.DeviceGone("gone"), (base.NEEDS_REPLUG, True)),
            (base.DriverMissing("no driver"), (base.NEEDS_DRIVER, False)),
            (base.Cancelled("stop"), (None, False)),
            (ValueError("no folder"), (None, False)),
        ]
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(base.state_for(exc), expected)

    def test_errors_carry_advice(self):
        e = base.RecorderError("It stopped answering.", advice="Unplug it and plug it back in.")
        self.assertEqual((str(e), e.message, e.advice),
                         ("It stopped answering.", "It stopped answering.", "Unplug it and plug it back in."))
        self.assertEqual(base.NotReady("x").advice, "")
        self.assertIs(base.Cancelled, formats.Cancelled)

    def test_safe_names(self):
        for ok in ("A", "Folder one", "Ünïcødé", "rec 1.fk2", "001_A_007_Owner_2029_05_23.dvf",
                   "CONSOLE", "COM10", "con x.txt", "CONIN"):
            self.assertTrue(base.safe_name_ok(ok), ok)
        for bad in ("", ".", "..", "a/b", "a\\b", "a:b", "a*", "x\x01", "trail.", "trail ", " lead",
                    "CON", "con.txt", "LPT1", "com9.dvf", "x" * 121, None, 7,
                    "CONIN$", "conout$.txt", "COM¹", "lpt².dvf", "LPT³", "CON .txt", "nul  .a.b"):
            self.assertFalse(base.safe_name_ok(bad), repr(bad))

    def test_discovered_device_defaults(self):
        d = base.DiscoveredDevice("1@2", "m", "USB port 1")
        self.assertEqual((d.state, d.message, d.locator), (base.READY, "", None))
        with self.assertRaises(ValueError):
            base.DiscoveredDevice("1@2", "m", "USB port 1", state="broken")


# ---- the format registry -----------------------------------------------------
class FormatTests(unittest.TestCase):
    def test_lookup(self):
        self.assertIs(formats.by_ext(".DVF"), formats.by_ext(".dvf"))
        self.assertEqual(formats.by_ext(".dvf").label, "Sony original")
        self.assertEqual(formats.by_ext(".wav").ext, ".wav")
        self.assertIsNone(formats.by_ext(".mp3"))
        self.assertIsNone(formats.by_ext("dvf"))
        self.assertEqual([f.ext for f in formats.all()][:2], [".wav", ".dvf"])

    def test_register_and_unregister(self):
        fake_models.install(self)
        self.assertIs(formats.by_ext(".fk1"), fake_models.FK1)
        with self.assertRaises(ValueError):
            formats.register(fake_models.FK1)
        for bad in ("fk3", ".FK3", "", ".a/b", ".a.b"):
            with self.subTest(ext=bad), self.assertRaises(ValueError):
                formats.Format(ext=bad, label="x", same=bytes.__eq__, seconds=lambda p: None)

    def test_wav_equality_is_bytes(self):
        wav = formats.by_ext(".wav")
        self.assertTrue(wav.same(b"RIFF-a", b"RIFF-a"))
        self.assertFalse(wav.same(b"RIFF-a", b"RIFF-b"))

    def test_dvf_equality_ignores_time_counters(self):
        fmt = formats.by_ext(".dvf")
        a = dvf.build(make_raw(2958, 100), DATE, "X")
        recounted = bytearray(a)
        recounted[1024 + 6:1024 + 10] = b"\x12\x34\x56\x78"     # DVE rewrote a block counter
        other = dvf.build(make_raw(4000, 900), DATE, "X")
        self.assertTrue(fmt.same(a, a))
        self.assertTrue(fmt.same(bytes(recounted), a))
        self.assertFalse(fmt.same(other, a))
        # Today's (frozen, A1) semantics, one predicate for every user:
        # fingerprints compared as they are, so two damaged files match.
        self.assertIs(fmt.same, dvf.same_audio)
        self.assertTrue(fmt.same(b"damaged", b"also damaged"))
        self.assertFalse(fmt.same(b"damaged", a))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.dvf")
            with open(path, "wb") as f:
                f.write(b"damaged")
            self.assertEqual(save_dvf(b"also damaged", d, "r.dvf"), (path, True))
            self.assertEqual(target_path(d, "r.dvf", dvf.audio_fingerprint(b"x")), (path, True))

    def test_seconds_from_headers(self):
        with tempfile.TemporaryDirectory() as d:
            w = os.path.join(d, "r.wav")
            with open(w, "wb") as f:
                f.write(wav_bytes(b"\0\0" * 4000))
            self.assertEqual(formats.by_ext(".wav").seconds(w), 0.5)
            v = os.path.join(d, "r.dvf")
            with open(v, "wb") as f:
                f.write(dvf.build(make_raw(2958, 100), DATE, "X"))
            header = dvf.build(make_raw(2958, 100), DATE, "X")[464:468]
            self.assertEqual(formats.by_ext(".dvf").seconds(v),
                             round(int.from_bytes(header, "big") / 750, 1))
            self.assertIsNone(formats.by_ext(".dvf").seconds(os.path.join(d, "missing.dvf")))

    def test_size_limits(self):
        self.assertEqual(formats.by_ext(".dvf").max_bytes, 512 << 20)
        self.assertIsNone(formats.by_ext(".wav").max_bytes)

    def test_wav_decoder_is_a_pcm_passthrough(self):
        dec = formats.by_ext(".wav").decoder
        self.assertEqual((dec.available(), dec.reason(), dec.warning()), (True, None, None))
        data = wav_bytes()
        self.assertEqual(dec.to_wav(data), data)
        with self.assertRaises(formats.DecodeError):
            dec.to_wav(b"RIFF nonsense")
        with self.assertRaises(formats.Cancelled):
            dec.to_wav(data, should_stop=lambda: True)


class DvfDecoderTests(unittest.TestCase):
    dec = formats.by_ext(".dvf").decoder

    def test_unavailable_gives_todays_reason(self):
        with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": None}):
            self.assertFalse(self.dec.available())
            self.assertEqual(self.dec.reason(), audio.status())
            self.assertIn("not included in this build", self.dec.reason())
            self.assertIsNone(self.dec.warning())
            with self.assertRaises(formats.DecoderUnavailable):
                self.dec.to_wav(b"x")

    def test_slow_mode_is_a_warning(self):
        with mock.patch.object(audio, "available", return_value=True), \
                mock.patch.object(audio, "status", return_value=audio.SLOW_MODE):
            self.assertTrue(self.dec.available())
            self.assertIsNone(self.dec.reason())
            self.assertEqual(self.dec.warning(), audio.SLOW_MODE)

    def test_to_wav_wraps_st25_audio(self):
        with mock.patch.object(audio, "dvf_to_wav", return_value=b"RIFF") as call:
            stop = lambda: False  # noqa: E731
            self.assertEqual(self.dec.to_wav(b"dvf", should_stop=stop), b"RIFF")
            call.assert_called_once_with(b"dvf", should_stop=stop)
        with mock.patch.object(audio, "dvf_to_wav", side_effect=audio.Cancelled("stopped")):
            with self.assertRaises(formats.Cancelled):
                self.dec.to_wav(b"dvf")
        with mock.patch.object(audio, "dvf_to_wav", side_effect=ValueError("bad frame 3")):
            with self.assertRaisesRegex(formats.DecodeError, "bad frame 3"):
                self.dec.to_wav(b"dvf")
        for failure in (MemoryError(), OSError(2, "gone", r"C:\secret\tables.bin")):
            with mock.patch.object(audio, "dvf_to_wav", side_effect=failure):
                with self.assertRaises(type(failure)) as caught:    # not the file's fault
                    self.dec.to_wav(b"dvf")
                self.assertIs(caught.exception, failure)


# ---- the fakes, against the shared contract ----------------------------------
class FakeAlphaContract(RecorderContract, unittest.TestCase):
    def setUp(self):
        self.model, _beta = fake_models.install(self)
        self.model.plug()
        self.addCleanup(lambda: self.assertEqual(self.model.open_sessions, set()))


class FakeBetaContract(RecorderContract, unittest.TestCase):
    def setUp(self):
        _alpha, self.model = fake_models.install(self)
        self.model.plug()
        self.addCleanup(lambda: self.assertEqual(self.model.open_sessions, set()))


class FakeBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.alpha, self.beta = fake_models.install(self)

    def test_reconnect_changes_the_connection_id(self):
        cid = self.alpha.plug()
        session = self.alpha.open(self.alpha.discover()[0])
        self.addCleanup(session.close)
        new = self.alpha.reconnect(cid)
        self.assertNotEqual(new, cid)
        self.assertEqual([d.connection_id for d in self.alpha.discover()], [new])
        with self.assertRaises(base.DeviceGone):
            session.folders()

    def test_injected_failures_and_states(self):
        self.beta.plug()
        session = self.beta.open(self.beta.discover()[0])
        self.addCleanup(session.close)
        self.beta.devices[self.beta.discover()[0].connection_id].fail_with = base.NotReady("stale")
        with self.assertRaises(base.NotReady):
            session.folders()
        self.assertTrue(session.folders())
        session.close()
        with self.assertRaises(base.NotReady):
            session.folders()
        device = fake_models.FakeBeta.sample_device()
        device.state, device.message = base.NEEDS_DRIVER, "needs its driver"
        self.beta.plug(device)
        found = self.beta.discover()[-1]
        self.assertEqual((found.state, found.message), (base.NEEDS_DRIVER, "needs its driver"))
        with self.assertRaises(base.DriverMissing):
            self.beta.open(found)

    def test_differences_from_the_st25(self):
        self.assertIsNone(self.beta.native.decoder)
        self.assertNotEqual(self.alpha.native.ext, ".dvf")
        self.alpha.plug()
        session = self.alpha.open(self.alpha.discover()[0])
        self.addCleanup(session.close)
        data = session.download("1", 1).data
        with wave.open(io.BytesIO(self.alpha.native.decoder.to_wav(data))) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 8000))


if __name__ == "__main__":
    unittest.main()
