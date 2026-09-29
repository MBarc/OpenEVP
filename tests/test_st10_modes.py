"""An ICD-ST10 holds recordings in three modes, chosen per recording: LPEC ST
(0x6C), LPEC LP (0x00, the ICD-ST25's codec) and LPEC SP (0x20, 16 kHz mono).

LPEC SP is downloaded and saved (codec 0x2A, the LP header template with SP's
channel and rate fields), but OpenEVP has no SP decoder yet: it refuses to
play or convert it with a plain reason, per recording, while the recorder's
LP and ST recordings still play.
"""
import contextlib
import io
import os
import struct
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fixtures import DATE, FakeRecorderDevice, make_raw, make_st_raw, make_table  # noqa: E402
from test_app_recorders import AppTestBase  # noqa: E402
from test_st10_model import FRAMES  # noqa: E402
from app import backend  # noqa: E402
from openevp import formats  # noqa: E402
from openevp.recorders.sony_st25 import ST25Session  # noqa: E402
from st25 import audio, cli, dvf  # noqa: E402
from st25.folder import parse  # noqa: E402
from st25.protocol import Recorder  # noqa: E402
from st25.session import RecorderSession  # noqa: E402

SP = "LPEC SP (16 kHz) audio can't be played yet"
UNDATED = b"\xff" * 8
SP_LENGTH = 16938                       # A-006 of the real ICD-ST10: 17 blocks, 16768 payload bytes
SP_COUNTER = 0x353D6E4A
ID = "1-4@7"


def sp_dvf(length=SP_LENGTH, date=UNDATED):
    return dvf.build(make_raw(length, SP_COUNTER), date, "", expected_length=length, mode=dvf.MODE_SP)


def tiny_wav(rate, channels):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(range(64)) * channels * 4)
    return buf.getvalue()


def fake_decoders():
    """Fake LP and ST decoder modules, each recording the codec bytes it was given."""
    calls = []
    mods = {}
    for name, rate, channels in (("openevp.decoders.sony_lpec", 8000, 1), ("openevp.decoders.sony_lpec_st", 44100, 2)):
        m = types.ModuleType(name)
        wav = tiny_wav(rate, channels)

        def to_wav(data, should_stop=None, wav=wav, name=name):
            calls.append((name.rsplit(".", 1)[1], dvf.codec(data)))
            return wav
        m.dvf_to_wav = to_wav
        mods[name] = m
    return mods, calls


class SpFileTests(unittest.TestCase):
    def test_header_is_the_lp_template_with_sp_fields(self):
        sp = sp_dvf()
        lp = dvf.build(make_raw(SP_LENGTH, SP_COUNTER), UNDATED, "", expected_length=SP_LENGTH)
        self.assertEqual((sp[61], sp[62:64], sp[64:68], sp[68:72]),
                         (0x2A, b"\x00\x01", struct.pack(">I", 16000), struct.pack(">I", 2000)))
        self.assertEqual([i for i in range(len(sp)) if sp[i] != lp[i]], [61, 66, 67, 70, 71])
        self.assertIsNone(dvf.validate(sp))
        self.assertEqual((dvf.codec(sp), dvf.mode_of(sp)), (dvf.CODEC_SP, dvf.MODE_SP))
        self.assertIsNotNone(dvf.audio_fingerprint(sp))
        self.assertEqual(dvf.codec_of_mode(0x20), 0x2A)

    def test_duration_is_payload_over_2000(self):
        payload = SP_LENGTH - 10 * 17
        self.assertEqual(dvf.seconds(payload, dvf.MODE_SP), payload / 2000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.dvf")
            with open(path, "wb") as f:
                f.write(sp_dvf())
            self.assertEqual(formats.DVF.seconds(path), round(payload / 2000, 1))

    def test_no_st_frame_check(self):
        # LP-style blocks whose frame-offset fields mean nothing to LPEC ST are fine for SP.
        raw = bytearray(make_raw(3000, 1))
        raw[0:2] = b"\x00\x42"
        dvf.build(bytes(raw), UNDATED, "", expected_length=3000, mode=dvf.MODE_SP)
        with self.assertRaises(dvf.FormatError):
            dvf.build(bytes(raw), UNDATED, "", expected_length=3000, mode=dvf.MODE_ST)

    def test_folder_table_mode_0x20_is_known(self):
        m = parse(make_table([(5, 0x353D6E4A, 0x100000, SP_LENGTH, DATE, "", 0x20)]))[0]
        self.assertEqual((m.mode, m.problem, m.blocks), (0x20, "", 17))
        self.assertEqual(m.seconds(), (SP_LENGTH - 170) / 2000)

    def test_refused_plainly_never_decoded_as_lp(self):
        sp = sp_dvf()
        mods, calls = fake_decoders()
        with mock.patch.dict(sys.modules, mods):
            self.assertFalse(audio.available(dvf.CODEC_SP))
            self.assertEqual(audio.status(dvf.CODEC_SP), SP)
            self.assertEqual(formats.codec_problem(formats.CODEC_SP), SP)
            self.assertEqual(formats.DVF.data_problem(sp), SP)
            with self.assertRaises(audio.DecoderUnavailable):
                audio.dvf_to_wav(sp)
            with self.assertRaises(formats.DecoderUnavailable):
                formats.DVF.decoder.to_wav(sp)
            with self.assertRaises(formats.DecoderUnavailable):
                formats.write_wav(formats.DVF, sp, io.BytesIO())
            self.assertIsNone(formats.DVF.data_problem(dvf.build(make_raw(2958, 1), DATE, "X")))
        self.assertEqual(calls, [])
        self.assertEqual(formats.DVF.decoder.wav_bytes(sp), 44 + (SP_LENGTH - 170) * 16)


def mixed_folders():
    """Folder A of an ICD-ST10 with one recording in each mode: ST, LP, SP."""
    st = make_st_raw(FRAMES[1])
    st_length = len(FRAMES[1]) + 10 * (len(st) // 1056)
    return ({1: [(0, 0xFFFFFFFF, 0x180000, st_length, UNDATED, "", 0x6C),
                 (1, 0x353D6E81, 0x190000, 2958, UNDATED, "", 0x00),
                 (2, SP_COUNTER, 0x1A0000, SP_LENGTH, UNDATED, "", 0x20)]},
            {(1, 1): st})


class MixedModesAppTests(AppTestBase):
    """Playability is per recording: on one ST10, LP and ST play, SP says why not."""

    def setUp(self):
        mods, self.calls = fake_decoders()
        patch = mock.patch.dict(sys.modules, mods)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()
        self.st25.append(ID)            # discovered by the ST25 model, as the real one is
        self.rows()

    def open_device(self, model, device):
        folders, voice = mixed_folders()
        r = Recorder.__new__(Recorder)
        r.dev = FakeRecorderDevice(folders, voice=voice, identify="ICD-ST10")
        s = RecorderSession(r)
        s.connect()
        return ST25Session(s)

    def test_listing(self):
        r = self.api.recordings(ID)
        self.assertEqual((r["model_id"], r["playable"], r["play_reason"]), ("sony-icd-st10", True, None))
        rows = r["folders"][0]["recordings"]
        self.assertEqual([(x["label"], x["problem"], x["play_problem"]) for x in rows],
                         [("A-001", None, None), ("A-002", None, None), ("A-003", None, SP)])
        self.assertEqual(rows[2]["seconds"], round((SP_LENGTH - 170) / 2000, 1))
        self.assertEqual(r["formats"][1], {"value": "wav", "label": "WAV", "available": True, "reason": None})

    def test_lp_and_st_play_sp_refused(self):
        self.api.recordings(ID)
        for n in (1, 2):
            r = self.api.audio(ID, "A", n)
            self.assertTrue(r["ok"], r)
        self.assertEqual(self.calls, [("sony_lpec_st", dvf.CODEC_ST), ("sony_lpec", dvf.CODEC_LP)])
        r = self.api.audio(ID, "A", 3)
        self.assertEqual((r["ok"], r["error"]), (False, f"Playback: {SP}."))
        self.assertEqual(self.server.made, [(ID, "A", 1), (ID, "A", 2)])
        with self.assertRaises(formats.DecoderUnavailable) as cm:
            backend.recording_wav(self.m, (ID, "A", 3))
        self.assertEqual(str(cm.exception), SP)
        self.assertEqual(len(self.calls), 2)

    def test_exports(self):
        self.api.recordings(ID)
        items = [{"folder": "A", "number": n} for n in (1, 2, 3)]
        name, p = self.export(ID, items, "dvf")
        self.assertEqual((name, p["saved"], p["notes"]), ("export-done", 3, []))
        sp = self.read("A", "001_A_003_Unknown.dvf")
        self.assertEqual((dvf.validate(sp), dvf.codec(sp)), (None, dvf.CODEC_SP))
        self.assertEqual(sp, sp_dvf())
        name, p = self.export(ID, items, "wav")
        self.assertEqual((name, p["saved"], p["notes"]), ("export-done", 2, [f"A-003: not converted ({SP})"]))
        self.assertEqual([f for f in self.files("A") if f.endswith(".wav")],
                         ["001_A_001_Unknown.wav", "001_A_002_Unknown.wav"])


class MixedModesCliTests(unittest.TestCase):
    def run_cli(self, *argv):
        folders, voice = mixed_folders()
        dev = FakeRecorderDevice(folders, voice=voice, identify="ICD-ST10")
        r = Recorder.__new__(Recorder)
        r.dev = dev
        mods, self.calls = fake_decoders()
        buf = io.StringIO()
        with mock.patch.object(cli, "Recorder", return_value=r), mock.patch.dict(sys.modules, mods), \
                contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_every_mode_is_labelled(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli(d, "--list", "--folder", "A")
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line.startswith("    ")]
        self.assertEqual([line.split("  ")[-1] for line in lines], ["LPEC ST", "LPEC LP", "LPEC SP"])
        self.assertIn(f"{(SP_LENGTH - 170) / 2000:7.1f} s", lines[2])

    def test_sp_is_saved_without_a_wav(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli(d, "--folder", "A", "--wav")
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(os.listdir(outdir)),
                             ["001_A_001_Unknown.dvf", "001_A_001_Unknown.wav", "001_A_002_Unknown.dvf",
                              "001_A_002_Unknown.wav", "001_A_003_Unknown.dvf"])
            with open(os.path.join(outdir, "001_A_003_Unknown.dvf"), "rb") as f:
                self.assertEqual(f.read(), sp_dvf())
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.calls), [("sony_lpec", dvf.CODEC_LP), ("sony_lpec_st", dvf.CODEC_ST)])
        self.assertIn(f"saved 001_A_003_Unknown.dvf  (8 s audio, ", out)
        self.assertIn(f"; no WAV: {SP}", out)


if __name__ == "__main__":
    unittest.main()
