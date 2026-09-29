"""End-to-end tests for st25.cli.run()/main() against an emulated recorder:
the --wav flag writes a WAV beside each .dvf, using the same never-overwrite
rules as the .dvf files themselves, and never crashes the download when the
decoder is unavailable.
"""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice  # noqa: E402
from st25 import cli  # noqa: E402
from st25.protocol import Recorder  # noqa: E402

FOLDERS = {1: [(0, 100, 0x1000, 2958, DATE, "Casey"), (1, 900, 0x3000, 4000, DATE, "Casey")]}
DVF_NAMES = ["001_A_001_Casey_2029_05_23.dvf", "001_A_002_Casey_2029_05_23.dvf"]
WAV_NAMES = ["001_A_001_Casey_2029_05_23.wav", "001_A_002_Casey_2029_05_23.wav"]


def _fake_recorder(dev):
    r = Recorder.__new__(Recorder)     # skip __init__: no real USB device
    r.dev = dev
    return r


class WavFlagTests(unittest.TestCase):
    def _run(self, tmp, argv_extra=(), dev=None):
        dev = dev if dev is not None else FakeRecorderDevice(FOLDERS)
        buf = io.StringIO()
        with mock.patch.object(cli, "Recorder", return_value=_fake_recorder(dev)), \
                contextlib.redirect_stdout(buf):
            code = cli.main([tmp, "--folder", "A", *argv_extra])
        return code, buf.getvalue(), dev

    def test_wav_written_beside_each_dvf(self):
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            code, out, _dev = self._run(d, ["--wav"])
            self.assertEqual(code, 0)
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(os.listdir(outdir)), sorted(DVF_NAMES + WAV_NAMES))
            for name in WAV_NAMES:
                with open(os.path.join(outdir, name), "rb") as f:
                    self.assertTrue(f.read().startswith(b"RIFF"))
            self.assertIn("wav saved", out)

    def test_without_wav_flag_no_wav_written(self):
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            code, out, _dev = self._run(d)          # no --wav
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(os.path.join(d, "A"))), sorted(DVF_NAMES))

    def test_decoder_unavailable_warns_once_and_still_saves_dvf(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": None}):
            code, out, _dev = self._run(d, ["--wav"])
            self.assertEqual(code, 0)
            self.assertIn("Note: --wav requested but", out)
            self.assertIn("not included in this build", out)
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(os.listdir(outdir)), sorted(DVF_NAMES))   # dvf still saved
            self.assertEqual(out.count("Note: --wav requested"), 1)          # not per-recording

    def test_wav_never_overwrites_and_is_skipped_when_identical(self):
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            dev = FakeRecorderDevice(FOLDERS)
            self._run(d, ["--wav"], dev=dev)
            outdir = os.path.join(d, "A")
            before = {n: os.path.getmtime(os.path.join(outdir, n)) for n in WAV_NAMES}
            # Second run: the .dvf already matches (skipped), the .wav content is
            # identical too, so save_wav() must not touch either file again.
            code, out, dev2 = self._run(d, ["--wav"], dev=FakeRecorderDevice(FOLDERS))
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(outdir)), sorted(DVF_NAMES + WAV_NAMES))
            after = {n: os.path.getmtime(os.path.join(outdir, n)) for n in WAV_NAMES}
            self.assertEqual(before, after)
            self.assertNotIn("wav saved", out)          # nothing new was written

    def test_wav_backfilled_for_a_previously_downloaded_recording(self):
        """A .dvf saved without --wav in an earlier run gets its .wav filled
        in on a later --wav run, without re-saving (or duplicating) the .dvf."""
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            self._run(d, [], dev=FakeRecorderDevice(FOLDERS))           # no --wav
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(os.listdir(outdir)), sorted(DVF_NAMES))
            code, out, _dev = self._run(d, ["--wav"], dev=FakeRecorderDevice(FOLDERS))
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(outdir)), sorted(DVF_NAMES + WAV_NAMES))
            # The backfill itself must be reported, not just performed silently.
            self.assertEqual(out.count("wav saved"), len(WAV_NAMES))
            for name in WAV_NAMES:
                self.assertIn(f"wav saved {name}", out)
            self.assertIn(f"{len(WAV_NAMES)} WAV file", out)     # Progress.summary() counts them too

    def test_wav_conversion_failure_is_a_note_not_a_crash(self):
        def boom(data):
            raise ValueError("bad frame")
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = boom
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            code, out, _dev = self._run(d, ["--wav"])
            self.assertEqual(code, 1)                     # problems were recorded
            self.assertIn("WAV not converted", out)
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(f for f in os.listdir(outdir) if f.endswith(".dvf")),
                             sorted(DVF_NAMES))            # the .dvf files are unaffected


class CheckWavTests(unittest.TestCase):
    """--check-wav: verifies WAV conversion works (for a build) without a
    recorder attached."""

    def _write_dvf(self, d):
        from fixtures import make_raw
        from st25 import dvf
        path = os.path.join(d, "sample.dvf")
        with open(path, "wb") as f:
            f.write(dvf.build(make_raw(2958, 100), DATE, "X", expected_length=2958))
        return path

    def test_check_wav_success_never_touches_the_recorder(self):
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = lambda data: b"RIFF" + bytes(44 - 4) + b"\0" * 16000
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}), \
                mock.patch.object(cli, "Recorder", side_effect=AssertionError("must not touch the recorder")):
            path = self._write_dvf(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        self.assertEqual(code, 0)
        self.assertIn("OK: decoded", buf.getvalue())

    def test_check_wav_reports_decoder_unavailable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": None}):
            path = self._write_dvf(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        self.assertEqual(code, 1)
        self.assertIn("not available", buf.getvalue())

    def test_check_wav_missing_tables_plain_message_plus_developer_hint(self):
        """The reason is the plain user-facing one; only --check-wav adds the
        developer hint on how to create the table data."""
        from openevp.decoders.sony_lpec import TablesMissing, tables
        with tempfile.TemporaryDirectory() as d,                 mock.patch.object(tables, "load", side_effect=TablesMissing()):
            path = self._write_dvf(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("not available: the WAV decoder could not be loaded: "
                      "this build does not include the LPEC table data", out)
        self.assertIn("(Developers: run tools/import_lpec_tables.py", out)

    def test_check_wav_reports_missing_file(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--check-wav", "does-not-exist.dvf"])
        self.assertEqual(code, 1)
        self.assertIn("Could not read", buf.getvalue())

    def test_check_wav_reports_decode_failure(self):
        def boom(data):
            raise ValueError("bad frame")
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.dvf_to_wav = boom
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": fake}):
            path = self._write_dvf(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        self.assertEqual(code, 1)
        self.assertIn("Could not decode", buf.getvalue())


class St10Tests(unittest.TestCase):
    """An ICD-ST10: listed with LPEC ST durations, saved as LPEC ST .dvf files;
    --wav skips them with a note (they can't be converted yet), and
    --check-wav says so for an ST10 file."""

    def setUp(self):
        from fixtures import make_st_frames, make_st_raw
        self.frames = make_st_frames(list(range(2, 40)) + list(range(0, 20)))
        raw = make_st_raw(self.frames)
        length = len(self.frames) + 10 * (len(raw) // 1056)
        self.folders = {1: [(0, 0xFFFFFFFF, 0x180000, length, b"\xff" * 8, "", 0x6C),
                            (1, 0x3AB79EC2, 0x190000, 2958, DATE, "Casey")]}     # an LP one too
        self.voice = {(1, 1): raw}
        fake = types.ModuleType("openevp.decoders.sony_lpec")
        fake.calls = []

        def to_wav(data, should_stop=None):
            fake.calls.append(data)
            return b"RIFF" + data[:8]
        fake.dvf_to_wav = to_wav
        self.decoder = fake

    def run_cli(self, *argv):
        dev = FakeRecorderDevice(self.folders, voice=self.voice, identify="ICD-ST10")
        buf = io.StringIO()
        with mock.patch.object(cli, "Recorder", return_value=_fake_recorder(dev)), \
                mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": self.decoder}), \
                contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_list(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli(d, "--list", "--folder", "A")
            self.assertEqual(os.listdir(d), [])
        self.assertEqual(code, 0)
        self.assertIn("Connected: ICD-ST10", out)
        self.assertNotIn("Warning", out)
        seconds = len(self.frames) / 283 * 2048 / 44100
        self.assertIn(f"    1  no date              {seconds:7.1f} s    LPEC ST", out)
        self.assertIn(f"    2  2029-05-23 19:54:04  {2928 / 750:7.1f} s  Casey", out)

    def test_download_with_wav_skips_the_st10_recording(self):
        from st25 import dvf
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli(d, "--folder", "A", "--wav")
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(os.listdir(outdir)), ["001_A_001_Unknown.dvf", "001_A_002_Casey_2029_05_23.dvf",
                                                          "001_A_002_Casey_2029_05_23.wav"])
            with open(os.path.join(outdir, "001_A_001_Unknown.dvf"), "rb") as f:
                data = f.read()
        self.assertEqual(code, 0)
        self.assertIsNone(dvf.validate(data))
        self.assertEqual(dvf.payload(data), self.frames)
        self.assertEqual(len(self.decoder.calls), 1)                         # the LP one only
        self.assertIn("no WAV: LPEC ST (ICD-ST10) audio can't be played yet", out)

    def test_check_wav_on_an_st10_file(self):
        from fixtures import make_st_raw
        from st25 import dvf
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "st10.dvf")
            with open(path, "wb") as f:
                f.write(dvf.build(make_st_raw(self.frames), b"\xff" * 8, "", mode=dvf.MODE_ST))
            buf = io.StringIO()
            with mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec": self.decoder}), \
                    contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        self.assertEqual(code, 1)
        self.assertIn("LPEC ST (ICD-ST10) audio can't be played yet", buf.getvalue())
        self.assertEqual(self.decoder.calls, [])

    def test_an_unknown_model_is_read_as_an_st25_with_a_warning(self):
        self.folders = {1: [(0, 0x3AB79EC2, 0x190000, 2958, DATE, "Casey")]}
        dev = FakeRecorderDevice(self.folders, identify="ICD-ST99")
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(cli, "Recorder", return_value=_fake_recorder(dev)), \
                contextlib.redirect_stdout(buf):
            code = cli.main([d, "--list"])
        self.assertEqual(code, 0)
        self.assertIn("Warning: only the ICD-ST25 and ICD-ST10 have been verified; this is 'ICD-ST99'.", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
