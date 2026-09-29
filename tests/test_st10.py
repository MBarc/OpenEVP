"""The Sony ICD-ST10 in the st25 package: its folder tables (message-list mode
byte 0x6c), its LPEC ST .dvf files and downloads.

Synthetic data only, shaped like the real ST10's (tests/fixtures.py
make_st_raw reproduces the real wire layout: block headers, frame offsets,
283-byte frames with 16-bit counters, counter-0 segment frames).
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, ST_FRAME, FakeRecorderDevice, make_raw, make_st_frames, make_st_raw, make_table  # noqa: E402
from st25 import dvf  # noqa: E402
from st25.folder import parse  # noqa: E402
from st25.protocol import Recorder, RecorderError  # noqa: E402
from st25.session import RecorderSession, build_dvf  # noqa: E402

UNDATED = b"\xff" * 8
# Counters like the real A-001: starting at 2, a counter-0 frame at each restart.
COUNTERS = list(range(2, 30)) + list(range(0, 12)) + list(range(0, 9))
FRAMES = make_st_frames(COUNTERS)
LENGTH = len(FRAMES) + 10 * -(-len(FRAMES) // 1014)       # valid bytes incl. block headers


def st_seconds(payload_bytes):
    return payload_bytes / ST_FRAME * 2048 / 44100


class St10FolderTests(unittest.TestCase):
    def test_st10_entries(self):
        m = parse(make_table([(0, 0xFFFFFFFF, 0x180000, LENGTH, UNDATED, "", 0x6C),
                              (1, 0xFFFFFFFF, 0x19E400, 3000, UNDATED, "", 0x6C)]))
        self.assertEqual([(x.number, x.slot, x.mode, x.problem) for x in m],
                         [(1, 0, dvf.MODE_ST, ""), (2, 1, dvf.MODE_ST, "")])
        self.assertEqual(m[0].owner, "")                     # the 90 00 tag is left alone
        self.assertEqual(m[0].when(), "no date")
        self.assertAlmostEqual(m[0].seconds(), st_seconds(len(FRAMES)))

    def test_lp_entries_keep_mode_0(self):
        m = parse(make_table([(0, 1, 0x1000, 2958, DATE, "X")]))[0]
        self.assertEqual((m.mode, m.problem), (dvf.MODE_LP, ""))
        self.assertAlmostEqual(m.seconds(), 2928 / 750)

    def test_mixed_modes(self):
        m = parse(make_table([(0, 1, 0x1000, 2958, DATE, "X"),
                              (1, 0xFFFFFFFF, 0x3000, LENGTH, UNDATED, "", 0x6C)]))
        self.assertEqual([x.mode for x in m], [dvf.MODE_LP, dvf.MODE_ST])
        self.assertEqual(m[0].owner, "X")

    def test_unknown_mode_is_a_problem_for_that_recording_only(self):
        m = parse(make_table([(0, 1, 0x1000, 2958, DATE, "X", 0x33),
                              (1, 2, 0x3000, 3000, DATE, "X")]))
        self.assertIn("0x33", m[0].problem)
        self.assertEqual((m[0].length, m[0].date, m[0].mode), (2958, DATE, 0x33))   # the rest still parses
        self.assertIsNone(m[0].seconds())
        self.assertEqual(m[1].problem, "")

    def test_a_high_byte_other_than_0x60_still_stops_the_table(self):
        t = bytearray(make_table([(0, 1, 0x1000, 2958, DATE, "X")]))
        t[0:4] = struct.pack(">HH", 0, 0x616C)
        with self.assertRaises(ValueError):
            parse(bytes(t))


class St10DvfTests(unittest.TestCase):
    def build(self, frames=FRAMES, **kw):
        return dvf.build(make_st_raw(frames), UNDATED, "", mode=dvf.MODE_ST, **kw)

    def test_round_trip(self):
        f = self.build(expected_length=LENGTH)
        self.assertIsNone(dvf.validate(f))
        self.assertEqual(dvf.payload(f), FRAMES)
        self.assertEqual(dvf.codec(f), dvf.CODEC_ST)
        self.assertEqual(dvf.mode_of(f), dvf.MODE_ST)

    def test_header_fields(self):
        f = self.build()
        blocks = -(-len(FRAMES) // 1014)
        self.assertEqual(f[:8], b"MS_VOICE")
        self.assertEqual(f[52:60], UNDATED)
        self.assertEqual(f[61], 0x24)                                    # LPEC ST
        self.assertEqual(f[62:64], b"\x00\x02")                          # stereo
        self.assertEqual(struct.unpack(">II", f[64:72]), (48234, 6029))
        self.assertEqual(struct.unpack(">I", f[156:160])[0], blocks * 1024)
        self.assertEqual(struct.unpack(">I", f[464:468])[0], len(FRAMES))
        self.assertEqual(f[434:464], bytes(30))
        self.assertEqual(f[512:1024], b"\xff" * 512)
        self.assertAlmostEqual(dvf.seconds(len(FRAMES), dvf.MODE_ST), st_seconds(len(FRAMES)))

    def test_only_the_codec_fields_differ_from_lp(self):
        st = self.build()
        lp = dvf.build(make_raw(len(st) - 1024 - 5, 1), UNDATED, "")
        self.assertEqual([i for i in range(512) if st[i] != lp[i] and not 152 <= i < 160 and not 464 <= i < 468],
                         [61, 63, 66, 67, 70, 71])

    def test_same_audio(self):
        f = self.build()
        g = bytearray(f)
        g[1024 + 9] ^= 1                            # block time counters do not count
        self.assertTrue(dvf.same_audio(bytes(g), f))
        h = bytearray(f)
        h[1024 + 50] ^= 1
        self.assertFalse(dvf.same_audio(bytes(h), f))
        lp = dvf.build(make_raw(3000, 1), UNDATED, "")
        self.assertFalse(dvf.same_audio(lp, f))

    def test_lp_blocks_are_not_st_frames(self):
        with self.assertRaises(dvf.FormatError):
            dvf.build(make_raw(3000, 1), UNDATED, "", mode=dvf.MODE_ST)

    def test_st_frames_must_end_on_a_frame(self):
        with self.assertRaises(dvf.FormatError):
            self.build(FRAMES[:-5])

    def test_frame_counters_must_follow_on(self):
        bad = make_st_frames(list(range(2, 30)) + [40] + list(range(41, 50)))
        with self.assertRaises(dvf.FormatError):
            self.build(bad)

    def test_frame_counter_wraps_or_restarts(self):
        ok = make_st_frames([65534, 65535, 0, 1, 2, 0, 1])
        self.assertIsNone(dvf.validate(self.build(ok)))

    def test_frame_offsets_must_match(self):
        raw = bytearray(make_st_raw(FRAMES))
        raw[1056 + 1] ^= 4                          # the second block's frame offset
        with self.assertRaises(dvf.FormatError):
            dvf.build(bytes(raw), UNDATED, "", mode=dvf.MODE_ST)

    def test_validate_checks_the_frames_too(self):
        f = bytearray(self.build())
        f[1024 + 10 + ST_FRAME + 1] ^= 0x10         # the second frame's counter
        self.assertIsNotNone(dvf.validate(bytes(f)))
        self.assertIsNone(dvf.audio_fingerprint(bytes(f)))

    def test_an_lp_header_on_st_frames_is_still_lp(self):
        # What v0.8.2 wrote for an ST10 recording (a mislabelled LP file): still a
        # valid LP-layout file, so it is never mistaken for another recording.
        old = dvf.build(make_st_raw(FRAMES), UNDATED, "")
        self.assertIsNone(dvf.validate(old))
        self.assertEqual(dvf.codec(old), dvf.CODEC_LP)
        self.assertEqual(dvf.payload(old), FRAMES)

    def test_unknown_mode(self):
        with self.assertRaises(dvf.FormatError):
            dvf.build(make_st_raw(FRAMES), UNDATED, "", mode=0x33)
        self.assertIsNone(dvf.seconds(1000, 0x33))

    def test_codec_of_short_or_foreign_data(self):
        self.assertIsNone(dvf.codec(b"MS_VOICE"))
        self.assertIsNone(dvf.mode_of(b"x" * 600))


def session(folders, voice, identify="ICD-ST10"):
    dev = FakeRecorderDevice(folders, voice=voice, identify=identify)
    r = Recorder.__new__(Recorder)
    r.dev = dev
    s = RecorderSession(r)
    s.connect()
    return s, dev


class St10SessionTests(unittest.TestCase):
    FOLDERS = {1: [(0, 0xFFFFFFFF, 0x180000, LENGTH, UNDATED, "", 0x6C)]}

    def test_identify(self):
        s, _ = session(self.FOLDERS, {(1, 1): make_st_raw(FRAMES)})
        self.assertEqual(s.model, "ICD-ST10")

    def test_download(self):
        s, dev = session(self.FOLDERS, {(1, 1): make_st_raw(FRAMES)})
        d = s.download("A", 1)
        self.assertEqual((d.label, d.name, d.error), ("A-001", "001_A_001_Unknown.dvf", ""))
        self.assertIsNone(dvf.validate(d.dvf))
        self.assertEqual(dvf.codec(d.dvf), dvf.CODEC_ST)
        self.assertEqual(dvf.payload(d.dvf), FRAMES)
        self.assertEqual(dev.voice_calls, [(1, 1)])

    def test_data_that_is_not_st_frames_is_a_problem_not_a_bad_file(self):
        raw = bytearray(make_st_raw(FRAMES))
        raw[10 + ST_FRAME + 1] ^= 0x10              # the second frame's counter
        s, _ = session(self.FOLDERS, {(1, 1): bytes(raw)})
        d = s.download("A", 1)
        self.assertEqual(d.dvf, b"")
        self.assertIn("frame", d.error)

    def test_a_length_the_table_does_not_give_is_refused(self):
        m = parse(make_table([(0, 0xFFFFFFFF, 0x180000, LENGTH + 283, UNDATED, "", 0x6C)]))[0]
        with self.assertRaises(dvf.FormatError):
            build_dvf(make_st_raw(FRAMES), m, "A-001")

    def test_the_counter_check_still_applies(self):
        m = parse(make_table([(0, 0x1234, 0x180000, LENGTH, UNDATED, "", 0x6C)]))[0]
        with self.assertRaises(RecorderError):
            build_dvf(make_st_raw(FRAMES), m, "A-001")

    def test_unknown_mode_is_never_downloaded(self):
        s, dev = session({1: [(0, 1, 0x1000, 3000, DATE, "X", 0x33)]}, {})
        d = s.download("A", 1)
        self.assertIn("0x33", d.error)
        self.assertEqual(dev.voice_calls, [])


if __name__ == "__main__":
    unittest.main()
