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
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"st25.lpec": fake}):
            code, out, _dev = self._run(d, ["--wav"])
            self.assertEqual(code, 0)
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(os.listdir(outdir)), sorted(DVF_NAMES + WAV_NAMES))
            for name in WAV_NAMES:
                with open(os.path.join(outdir, name), "rb") as f:
                    self.assertTrue(f.read().startswith(b"RIFF"))
            self.assertIn("wav saved", out)

    def test_without_wav_flag_no_wav_written(self):
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"st25.lpec": fake}):
            code, out, _dev = self._run(d)          # no --wav
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(os.path.join(d, "A"))), sorted(DVF_NAMES))

    def test_decoder_unavailable_warns_once_and_still_saves_dvf(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"st25.lpec": None}):
            code, out, _dev = self._run(d, ["--wav"])
            self.assertEqual(code, 0)
            self.assertIn("Note: --wav requested but", out)
            self.assertIn("not included in this build", out)
            outdir = os.path.join(d, "A")
            self.assertEqual(sorted(os.listdir(outdir)), sorted(DVF_NAMES))   # dvf still saved
            self.assertEqual(out.count("Note: --wav requested"), 1)          # not per-recording

    def test_wav_never_overwrites_and_is_skipped_when_identical(self):
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"st25.lpec": fake}):
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
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data, should_stop=None: b"RIFF" + data[:8]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"st25.lpec": fake}):
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
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = boom
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"st25.lpec": fake}):
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
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = lambda data: b"RIFF" + bytes(44 - 4) + b"\0" * 16000
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"st25.lpec": fake}), \
                mock.patch.object(cli, "Recorder", side_effect=AssertionError("must not touch the recorder")):
            path = self._write_dvf(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        self.assertEqual(code, 0)
        self.assertIn("OK: decoded", buf.getvalue())

    def test_check_wav_reports_decoder_unavailable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(sys.modules, {"st25.lpec": None}):
            path = self._write_dvf(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        self.assertEqual(code, 1)
        self.assertIn("not available", buf.getvalue())

    def test_check_wav_missing_tables_plain_message_plus_developer_hint(self):
        """The reason is the plain user-facing one; only --check-wav adds the
        developer hint on how to create the table data."""
        from st25.lpec import TablesMissing, tables
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
        fake = types.ModuleType("st25.lpec")
        fake.dvf_to_wav = boom
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(sys.modules, {"st25.lpec": fake}):
            path = self._write_dvf(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--check-wav", path])
        self.assertEqual(code, 1)
        self.assertIn("Could not decode", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
