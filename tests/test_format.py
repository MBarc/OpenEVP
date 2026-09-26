"""Offline tests for the folder-table parser, the .dvf builder, the command
allow-list and file publishing.

They use synthetic data shaped like a real ICD-ST25 (no recordings are stored
in this repository). The format rules were verified against 20 real LP
recordings: Digital Voice Editor converts the builder's output to WAV
byte-identically to files it saved itself.
"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from st25 import dvf  # noqa: E402
from st25.export import publish, target_path  # noqa: E402
from st25.folder import FIRST_ENTRY_PAGE, PAGE, TABLE_SIZE, TableError, parse  # noqa: E402
from st25.protocol import (BLOCK_RAW, CMD_FOLDER_INFO, CMD_GET_VOICE, CMD_READ_BLOCK,  # noqa: E402
                           _args_ok, _completion_opcode)
from fixtures import DATE, FakeRecorderDevice, make_raw, make_table  # noqa: E402


class FolderTests(unittest.TestCase):
    def test_empty_folder(self):
        t = bytearray(b"\xff" * TABLE_SIZE)
        t[4 * PAGE:4 * PAGE + 20] = bytes(range(20))   # real empty folders look like this
        self.assertEqual(parse(bytes(t)), [])

    def test_messages(self):
        t = make_table([(0, 0x3AB79EC2, 0x1D0000, 2958, DATE, "Casey Q Hunters"),
                        (1, 0x3AB7A5C1, 0x1D0C00, 48444, b"\xff" * 8, "Casey Q Hunters")])
        m = parse(t)
        self.assertEqual([x.number for x in m], [1, 2])
        self.assertEqual([x.blocks for x in m], [3, 48])
        self.assertEqual(m[0].owner, "Casey Q Hunters")
        self.assertEqual(m[0].when(), "2029-05-23 19:54:04")
        self.assertFalse(m[1].dated)
        self.assertAlmostEqual(m[0].seconds(), 3.904, places=3)
        self.assertFalse(m[0].problem or m[1].problem)

    def test_slot_beyond_64_uses_next_range_page(self):
        m = parse(make_table([(70, 0x1000, 0x200000, 5000, DATE, "X")]))
        self.assertEqual(m[0].blocks, 5)
        self.assertEqual(m[0].start_counter, 0x1000)
        self.assertEqual(m[0].problem, "")

    def test_missing_range_is_a_problem_not_a_crash(self):
        t = bytearray(make_table([(0, 1, 0x1000, 3000, DATE, "X")]))
        t[5 * PAGE:5 * PAGE + 8] = b"\xff" * 8
        self.assertIn("no address range", parse(bytes(t))[0].problem)

    def test_reversed_range(self):
        t = bytearray(make_table([(0, 1, 0x1000, 3000, DATE, "X")]))
        t[5 * PAGE:5 * PAGE + 8] = struct.pack(">II", 0x2000, 0x1000 | 0x80000000)
        self.assertIn("reversed", parse(bytes(t))[0].problem)

    def test_implausible_length_is_a_problem(self):
        t = bytearray(make_table([(0, 1, 0x1000, 3000, DATE, "X")]))
        t[5 * PAGE:5 * PAGE + 8] = struct.pack(">II", 0x0, 0x7FFFFFFF | 0x80000000)
        m = parse(bytes(t))[0]
        self.assertIn("implausible length", m.problem)
        self.assertEqual(m.blocks, 0)

    def test_duplicate_slot_rejected(self):
        t = bytearray(make_table([(0, 1, 0x1000, 3000, DATE, "X")]))
        t[4:8] = struct.pack(">HH", 0, 0x6000)
        with self.assertRaises(TableError):
            parse(bytes(t))

    def test_owner_needs_tag(self):
        t = bytearray(make_table([(0, 1, 0x1000, 3000, DATE, "X")]))
        base = (FIRST_ENTRY_PAGE + 0) * PAGE
        t[base + 274:base + 276] = b"\xff\xff"
        self.assertEqual(parse(bytes(t))[0].owner, "")

    def test_implausible_date_is_undated(self):
        m = parse(make_table([(0, 1, 0x1000, 3000, bytes.fromhex("07ed0d2019000003"), "X")]))
        self.assertFalse(m[0].dated)

    def test_wrong_size(self):
        with self.assertRaises(ValueError):
            parse(b"\xff" * 100)


class DvfTests(unittest.TestCase):
    def test_build_layout(self):
        raw = make_raw(2958, 0x3AB79EC2)
        f = dvf.build(raw, DATE, "Casey Q Hunters", expected_length=2958)
        self.assertEqual(len(f), 1024 + 3 * 1024)
        self.assertEqual(f[:8], b"MS_VOICE")
        self.assertEqual(f[52:60], DATE)
        self.assertEqual(f[152], 4)
        self.assertEqual(int.from_bytes(f[153:156], "big"), 1024 - 910)   # tail padding
        self.assertEqual(struct.unpack(">I", f[156:160])[0], 3 * 1024)
        self.assertEqual(f[434:449], b"Casey Q Hunters")
        self.assertEqual(struct.unpack(">I", f[464:468])[0], 2928)        # 2958 - 3*10
        self.assertEqual(f[512:1024], b"\xff" * 512)
        audio = f[1024:]
        self.assertEqual(audio[2048 + 910:], b"\xff" * (1024 - 910))      # tail filled with FF
        self.assertEqual(audio[:512], raw[:512])                            # spare bytes dropped
        self.assertEqual(audio[512:1024], raw[528:1040])
        self.assertIsNotNone(dvf.audio_fingerprint(f))

    def test_large_recording_no_overflow(self):
        raw = make_raw(17000 * 1024, 1)                 # > 16 MiB of audio
        f = dvf.build(raw, DATE, "X", expected_length=17000 * 1024)
        self.assertEqual(struct.unpack(">I", f[156:160])[0], 17000 * 1024)

    def test_truncated_data_rejected(self):
        raw = make_raw(2958, 1)
        with self.assertRaises(dvf.FormatError):
            dvf.build(raw[:BLOCK_RAW], DATE, "X", expected_length=2958)

    def test_sp_marker_rejected(self):
        with self.assertRaises(dvf.FormatError):
            dvf.build(make_raw(2958, 1, marker=b"\x00\x0b"), DATE, "X")

    def test_short_inner_block_rejected(self):
        with self.assertRaises(dvf.FormatError):
            dvf.build(make_raw(4000, 1, short_block=1), DATE, "X")

    def test_rejects_bad_length(self):
        with self.assertRaises(dvf.FormatError):
            dvf.build(b"\0" * 1000, DATE, "x")

    def test_filename(self):
        self.assertEqual(dvf.filename("A", 7, "Casey Q Hunters", DATE, True),
                         "001_A_007_Casey Q Hunters_2029_05_23.dvf")
        self.assertEqual(dvf.filename("A", 18, "Casey Q Hunters", b"\xff" * 8, False),
                         "001_A_018_Casey Q Hunters.dvf")

    def test_filename_sanitized(self):
        self.assertEqual(dvf.filename("A", 1, "..\\a:b/c*?", DATE, False), "001_A_001_.._a_b_c__.dvf")
        self.assertEqual(dvf.filename("A", 1, "", DATE, False), "001_A_001_Unknown.dvf")
        self.assertEqual(dvf.filename("A", 1, "CON", DATE, False), "001_A_001_Unknown.dvf")

    def test_payload_round_trips_through_build(self):
        # A full first block and a partly-filled second one: build() pads the
        # second block's unused tail with 0xFF, which payload() must exclude,
        # returning only each block's header-stripped valid bytes.
        raw = make_raw(dvf.BLOCK + 500, 100)
        f = dvf.build(raw, DATE, "X", expected_length=dvf.BLOCK + 500)
        audio = f[1024:]
        self.assertEqual(len(audio), 2 * dvf.BLOCK)
        expected = audio[dvf.BLOCK_HEADER:dvf.BLOCK] + audio[dvf.BLOCK + dvf.BLOCK_HEADER:dvf.BLOCK + 500]
        self.assertEqual(dvf.payload(f), expected)


class ValidateTests(unittest.TestCase):
    def good(self):
        return dvf.build(make_raw(2958, 100), DATE, "Casey Q Hunters", expected_length=2958)

    def test_own_output_is_valid(self):
        self.assertIsNone(dvf.validate(self.good()))

    def test_dve_variations_are_valid(self):
        f = bytearray(self.good())
        f[58] = (f[58] - 1) % 60          # DVE rounds seconds
        f[1024 + 9] += 2                  # and block counters
        self.assertIsNone(dvf.validate(bytes(f)))

    def test_damage_is_detected(self):
        g = self.good()
        cases = {
            "zeroed header": g[:8] + bytes(1016) + g[1024:],
            "bad length field": g[:156] + b"\0\0\0\0" + g[160:],
            "bad payload field": g[:464] + b"\0\0\0\1" + g[468:],
            "bad padding field": g[:153] + b"\0\0\1" + g[156:],
            "damaged padding": g[:600] + b"\0" + g[601:],
            "damaged block": g[:1024 + 4] + b"\x02\x00" + g[1024 + 6:],
            "truncated": g[:-1024],
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.assertIsNotNone(dvf.validate(data))
                self.assertIsNone(dvf.audio_fingerprint(data))


class AllowListTests(unittest.TestCase):
    def test_only_dve_commands(self):
        self.assertTrue(_args_ok([CMD_READ_BLOCK, 0x1E0, 0]))
        self.assertFalse(_args_ok([CMD_READ_BLOCK, 0x1E0, 0x3C0]))
        self.assertTrue(_args_ok([CMD_FOLDER_INFO | (3 << 8), 0, 0]))
        self.assertFalse(_args_ok([CMD_FOLDER_INFO | (6 << 8), 0, 0]))
        self.assertTrue(_args_ok([CMD_GET_VOICE, 5 << 16, 1, 3, 3 * BLOCK_RAW]))
        self.assertFalse(_args_ok([CMD_GET_VOICE, 5 << 16, 1, 3, 3 * BLOCK_RAW + 1]))
        self.assertFalse(_args_ok([0x11FF0002, 5 << 16, 1, 3, 3 * BLOCK_RAW]))
        self.assertFalse(_args_ok([0xDEADBEEF, 0, 0]))

    def test_bad_size_rejected_before_allocation(self):
        from unittest import mock
        from st25.protocol import Recorder, RecorderError
        r = Recorder.__new__(Recorder)          # no USB device needed
        r.dev = mock.Mock()
        with mock.patch("builtins.bytearray", side_effect=AssertionError("allocated")):
            with self.assertRaises(RecorderError):
                r.voice_data(1, 2_000_000)          # > 32 MB
            with self.assertRaises(RecorderError):
                r.query_bulk([CMD_GET_VOICE, 1 << 16, 1, 3, 3 * BLOCK_RAW], 28, 10 ** 9)
        self.assertEqual(r.dev.mock_calls, [])      # no USB traffic of any kind

    def _guarded_device(self):
        import ctypes
        from unittest import mock
        from st25.usb import Device
        d = Device.__new__(Device)               # no real USB device; policy is fixed in usb.py
        d._lib = mock.Mock()
        d._lib.libusb_control_transfer.side_effect = lambda h, rt, rq, v, i, buf, n, t: n
        d._lib.libusb_bulk_transfer.return_value = 0
        d._ctrl = ctypes.create_string_buffer(Device.CONTROL_BUF)
        d._bulk = ctypes.create_string_buffer(Device.BULK_CHUNK)
        d._got = ctypes.c_int(0)
        d._h = object()
        return d

    def test_usb_layer_blocks_everything_but_dve_frames(self):
        from st25 import protocol
        from st25.usb import UsbError
        d = self._guarded_device()
        good = protocol.FRAME_PREFIX + struct.pack(">III", protocol.CMD_DEVICE_INFO, 0, 0)
        bad_frames = [
            protocol.FRAME_PREFIX + struct.pack(">III", 0xDEADBEEF, 0, 0),
            protocol.FRAME_PREFIX + struct.pack(">III", protocol.CMD_READ_BLOCK, 0x1E0, 0x3C0),
            b"\x01" + protocol.FRAME_PREFIX[1:] + struct.pack(">III", protocol.CMD_DEVICE_INFO, 0, 0),
            good + b"\0",
        ]
        for frame in bad_frames:
            with self.assertRaises(UsbError):
                d.control_out(protocol.REQTYPE_OUT, protocol.REQ_COMMAND, protocol.WVALUE, 0, frame, 1000)
        for args in [(protocol.REQTYPE_OUT, 0x81, protocol.WVALUE, 0, good),      # wrong request
                     (0x40, protocol.REQ_COMMAND, protocol.WVALUE, 0, good),        # device recipient
                     (protocol.REQTYPE_OUT, protocol.REQ_COMMAND, 0, 0, good)]:    # wrong wValue
            with self.assertRaises(UsbError):
                d.control_out(*args, 1000)
        with self.assertRaises(UsbError):
            d.control_in(protocol.REQTYPE_IN, 0x02, protocol.WVALUE, 0, 4, 1000)
        with self.assertRaises(UsbError):                       # SET_CONFIGURATION via the IN method
            d.control_in(0x00, 0x09, 1, 0, 0, 1000)
        with self.assertRaises(UsbError):                       # an OUT request type via the IN method
            d.control_in(0x41, protocol.REQ_STATUS, protocol.WVALUE, 0, 4, 1000)
        with self.assertRaises(UsbError):                       # bulk OUT endpoint via the IN method
            d.bulk_in_into(0x02, bytearray(64), 0, 64, 1000)
        with self.assertRaises(UsbError):
            d.bulk_in_into(0x82, bytearray(64), 0, 64, 1000)
        d._lib.libusb_control_transfer.assert_not_called()
        d._lib.libusb_bulk_transfer.assert_not_called()
        d.control_out(protocol.REQTYPE_OUT, protocol.REQ_COMMAND, protocol.WVALUE, 0, good, 1000)
        self.assertEqual(d._lib.libusb_control_transfer.call_count, 1)

    def test_close_makes_only_lifecycle_calls(self):
        d = self._guarded_device()
        d._ctx = object()
        d.close()
        called = {c[0] for c in d._lib.method_calls}
        self.assertEqual(called, {"libusb_release_interface", "libusb_close", "libusb_exit"})

    def test_completion_opcode(self):
        self.assertEqual(_completion_opcode(0x091001FF), 0x09100100)
        self.assertEqual(_completion_opcode(0x11FF0001), 0x11000001)
        self.assertEqual(_completion_opcode(0x092000FF), 0x09200000)


class PublishTests(unittest.TestCase):
    def test_never_overwrites_and_skips_only_identical_audio(self):
        with tempfile.TemporaryDirectory() as d:
            f = dvf.build(make_raw(2958, 100), DATE, "X")
            fp = dvf.audio_fingerprint(f)
            p, done = target_path(d, "a.dvf", fp)
            self.assertFalse(done)
            publish(f, p)
            with self.assertRaises(OSError):
                publish(b"other", p)                      # destination exists
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), f)
            self.assertEqual(target_path(d, "a.dvf", fp), (p, True))
            # same audio, counters rewritten the way DVE does -> still the same recording
            dve = bytearray(f)
            dve[1024 + 9] += 2
            self.assertEqual(dvf.audio_fingerprint(bytes(dve)), fp)
            # undated recordings all have counter 0xFFFFFFFF; different audio must not collide
            u1 = dvf.build(make_raw(2958, 0xFFFFFFFF - 5), b"\xff" * 8, "X")
            u2 = bytearray(u1)
            u2[1024 + 100] ^= 0xFF
            self.assertNotEqual(dvf.audio_fingerprint(u1), dvf.audio_fingerprint(bytes(u2)))
            other, done = target_path(d, "a.dvf", "different")
            self.assertFalse(done)
            self.assertTrue(other.endswith("a (2).dvf"))
            self.assertEqual([x for x in os.listdir(d) if x.endswith(".part")], [])

    def test_resume_finds_copy_after_a_gap(self):
        with tempfile.TemporaryDirectory() as d:
            mine = dvf.build(make_raw(2958, 100), DATE, "X")
            other = dvf.build(make_raw(4000, 900), DATE, "X")
            publish(other, os.path.join(d, "a.dvf"))
            publish(mine, os.path.join(d, "a (3).dvf"))     # "(2)" was deleted by the user
            publish(other, os.path.join(d, "a (12).dvf"))
            publish(mine, os.path.join(d, "b.dvf"))         # another name never matches
            self.assertEqual(target_path(d, "a.dvf", dvf.audio_fingerprint(mine)),
                             (os.path.join(d, "a (3).dvf"), True))
            # a new recording takes the lowest free number, never an existing file
            self.assertEqual(target_path(d, "a.dvf", "different"), (os.path.join(d, "a (2).dvf"), False))
            with tempfile.TemporaryDirectory() as d2:
                publish(other, os.path.join(d2, "a (1).dvf"))
                self.assertEqual(target_path(d2, "a.dvf", "x"), (os.path.join(d2, "a.dvf"), False))
            self.assertEqual(target_path(os.path.join(d, "missing"), "a.dvf", "x"),
                             (os.path.join(d, "missing", "a.dvf"), False))


class RawTests(unittest.TestCase):
    def test_raw_reuses_identical_and_never_overwrites(self):
        from st25.export import save_raw
        with tempfile.TemporaryDirectory() as d:
            p1 = save_raw(b"one", d, "a")
            self.assertEqual(p1, os.path.join(d, "a.raw"))
            self.assertEqual(save_raw(b"one", d, "a"), p1)             # rerun: nothing new
            p2 = save_raw(b"two", d, "a")
            self.assertEqual(p2, os.path.join(d, "a (2).raw"))
            with open(p1, "rb") as f:
                self.assertEqual(f.read(), b"one")
            self.assertEqual(sorted(os.listdir(d)), ["a (2).raw", "a.raw"])

    def test_raw_gap_and_many_names(self):
        from st25.export import save_raw
        with tempfile.TemporaryDirectory() as d:
            publish(b"other", os.path.join(d, "a.raw"))
            publish(b"mine", os.path.join(d, "a (3).raw"))           # "(2)" deleted
            self.assertEqual(save_raw(b"mine", d, "a"), os.path.join(d, "a (3).raw"))
            self.assertEqual(sorted(os.listdir(d)), ["a (3).raw", "a.raw"])
        with tempfile.TemporaryDirectory() as d:
            publish(b"x0", os.path.join(d, "a.raw"))
            for i in range(2, 101):
                publish(b"x%d" % i, os.path.join(d, f"a ({i}).raw"))
            self.assertEqual(save_raw(b"new", d, "a"), os.path.join(d, "a (101).raw"))


class ErrorReportTests(unittest.TestCase):
    def _main_output(self, exc):
        import contextlib
        import io
        from unittest import mock
        from st25 import cli

        def fake_run(args, pr):
            pr.saved, pr.skipped, pr.out_root = 3, 2, "OUT"
            pr.problems.append("A-002: skipped (x)")
            pr.current = "A-006"
            raise exc

        buf = io.StringIO()
        with mock.patch.object(cli, "run", fake_run), contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main([]), 1)
        return buf.getvalue()

    def test_fatal_error_reports_progress_and_recording(self):
        from st25.protocol import RecorderStuck
        out = self._main_output(RecorderStuck("data stopped"))
        self.assertIn("ERROR at recording A-006: data stopped", out)
        self.assertIn("Unplug its USB cable", out)
        self.assertIn("3 saved, 2 already saved before.", out)
        self.assertIn("NOTE A-002", out)
        self.assertIn("Files are in: OUT", out)

    def test_unexpected_error_gets_traceback_and_replug_advice(self):
        out = self._main_output(ZeroDivisionError("boom"))
        self.assertIn("Unexpected error at recording A-006", out)
        self.assertIn("Traceback", out)
        self.assertIn("Unplug its USB cable", out)
        self.assertIn("3 saved, 2 already saved before.", out)


class DesktopTests(unittest.TestCase):
    def test_fallback_is_announced_and_real(self):
        from unittest import mock
        from st25 import cli
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(cli.sys, "platform", "linux"), \
                mock.patch.object(cli.os.path, "expanduser", lambda p: home):
            folder, warning = cli.documents_dir()
            self.assertEqual(folder, home)                        # no Documents: never invent one
            self.assertIn("home folder", warning)
            os.mkdir(os.path.join(home, "Documents"))
            self.assertEqual(cli.documents_dir(), (os.path.join(home, "Documents"), None))
            self.assertEqual(cli.default_output(), (os.path.join(home, "Documents", "OpenEVP"), None))
            self.assertFalse(os.path.exists(os.path.join(home, "Documents", "OpenEVP")))   # not created


class BulkStallTests(unittest.TestCase):
    """query_bulk's inactivity handling, against the emulated recorder, on a
    simulated clock. Each scripted read is (bytes, rc, seconds); seconds=None
    means the read lasts its full timeout."""

    def _run(self, reads):
        from unittest import mock
        from st25 import protocol
        dev = FakeRecorderDevice()
        script = list(reads)
        clock = [0.0]
        timeouts = []

        def bulk_in_into(ep, dest, offset, max_len, timeout_ms):
            timeouts.append(timeout_ms)
            n, rc, secs = script.pop(0) if script else (0, -7, None)
            clock[0] += timeout_ms / 1000 if secs is None else secs
            n = min(n, max_len, len(dev.bulk))
            dest[offset:offset + n] = dev.bulk[:n]
            dev.bulk = dev.bulk[n:]
            return n, rc

        dev.bulk_in_into = bulk_in_into
        r = protocol.Recorder.__new__(protocol.Recorder)
        r.dev = dev
        with mock.patch.object(protocol.time, "monotonic", lambda: clock[0]):
            try:
                return r.folder_table(1), clock[0], timeouts
            except protocol.RecorderStuck as e:
                return e, clock[0], timeouts

    def test_partial_reads_at_timeout_are_progress(self):
        table, _, _ = self._run([(1000, -7, None), (20000, -7, None), (TABLE_SIZE, 0, 0.01)])
        self.assertEqual(len(table), TABLE_SIZE)

    def test_empty_read_at_timeout_is_stuck(self):
        err, t, _ = self._run([(1000, -7, None), (0, -7, None)])
        self.assertIn("after 1000 of", str(err))
        self.assertLessEqual(t, 10.0)

    def test_empty_successful_reads_do_not_extend_the_limit(self):
        # Data returned at a timeout (t=5), an empty successful read at t=9.9:
        # the next read may only wait the 0.1 s left, so "stuck" comes at t=10.
        err, t, timeouts = self._run([(1000, -7, None), (0, 0, 4.9), (0, -7, None)])
        self.assertIn("after 1000 of", str(err))
        self.assertAlmostEqual(t, 10.0, places=2)
        self.assertEqual(timeouts[-1], 100)

    def test_many_empty_successful_reads_stop_at_the_limit(self):
        err, t, _ = self._run([(0, 0, 0.3)] * 100)
        self.assertIn("after 0 of", str(err))
        self.assertLessEqual(t, 5.0 + 0.3)


class ToolTests(unittest.TestCase):
    def test_probe_runs_against_current_api(self):
        import contextlib
        import io
        from st25.protocol import Recorder
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        import probe
        r = Recorder.__new__(Recorder)
        r.dev = FakeRecorderDevice()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            probe.probe(r, d)
            files = sorted(os.listdir(d))
            self.assertEqual(len(files), 5 + 5 * 3)
            self.assertEqual(os.path.getsize(os.path.join(d, "folder3_table.bin")), TABLE_SIZE)


if __name__ == "__main__":
    unittest.main()
