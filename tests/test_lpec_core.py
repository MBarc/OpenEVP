"""The optional C core (st25/lpec/_lpec.c -> lpec_core.dll) must be an exact
drop-in for the pure-Python decoder.

- Its x87 fcos emulation matches st25.lpec.x87.fcos bit for bit.
- decode_payload through the C core gives the same PCM as the pure-Python
  path on every test vector.
- The pure-Python path is still selectable (use_core=False) and is what
  decode_payload falls back to when the DLL is absent.

Skipped when the DLL has not been built (python tools/build_lpec_core.py)
or the git-ignored table data is absent.
"""

import copy
import math
import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from st25.lpec import x87  # noqa: E402
from st25.lpec import _core  # noqa: E402
from test_lpec_vectors import _HAVE_TABLES, SHORT_VECTORS, python_pcm  # noqa: E402
from test_lpec_vectors import vector_payload as _payload  # noqa: E402

_NO_CORE = ("st25/lpec/lpec_core.dll is not built (or failed to load); run "
            "python tools/build_lpec_core.py")


def _bits(x: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", x))[0]


@unittest.skipUnless(_core.available(), _NO_CORE)
class CoreFcosTests(unittest.TestCase):
    """lpec_fcos == x87.fcos, bit for bit."""

    def _check(self, xs):
        for x in xs:
            want = x87.fcos(x)
            got = _core.fcos(x)
            self.assertEqual(_bits(got), _bits(want), f"fcos({x!r}): {got!r} != {want!r}")

    def test_special_points(self):
        pi = math.pi
        xs = [0.0, -0.0, 5e-324, 1e-300, 1e-20, 2.0 ** -33, 2.0 ** -32, 1e-9, 1e-5,
              0.5, 0.78, 0.7853981633974483, 0.7853981633974484, 1.0, 1.5,
              pi / 2, math.nextafter(pi / 2, 0), math.nextafter(pi / 2, 4),
              2.0, 2.356194490192345, 3.0, pi, math.nextafter(pi, 0),
              math.nextafter(pi, 4), 3.9269908169872414, 3 * pi / 2, 5.0,
              2 * pi, 6.28318530718, 10.0, 100.0, 12345.678, -1.0, -pi / 2, -3.0]
        self._check(xs)

    def test_lsp_arguments(self):
        # The decoder's only per-frame call: fcos((l*PI_C)*2.0), 0 <= l < 0.5.
        rng = random.Random(1234)
        xs = [(rng.random() * 0.5 * 3.14159265359) * 2.0 for _ in range(3000)]
        self._check(xs)

    def test_wide_range(self):
        rng = random.Random(99)
        xs = [rng.uniform(-50.0, 50.0) for _ in range(1500)]
        xs += [rng.uniform(0.0, 1.0) * 2.0 ** -rng.randint(0, 60) for _ in range(500)]
        self._check(xs)


@unittest.skipUnless(_HAVE_TABLES,
                     "st25/lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@unittest.skipUnless(_core.available(), _NO_CORE)
class CoreIdenticalOutputTests(unittest.TestCase):
    """decode_payload through the C core == the pure-Python decode."""

    def _check(self, name):
        from st25.lpec.decoder import decode_payload
        got = decode_payload(_payload(name), use_core=True)
        self.assertEqual(got, python_pcm(name), f"{name}: C core differs from pure Python")


def _make_test(order, name):
    def test(self):
        self._check(name)
    test.__name__ = f"test_{order:02d}_" + name.replace("-", "_")
    test.__doc__ = f"{name}: C core output == pure-Python output"
    return test


for _order, _name in enumerate(SHORT_VECTORS, 1):
    _t = _make_test(_order, _name)
    setattr(CoreIdenticalOutputTests, _t.__name__, _t)


@unittest.skipUnless(_HAVE_TABLES,
                     "st25/lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
class BackendSelectionTests(unittest.TestCase):
    """use_core=None picks the core when it loaded, else pure Python;
    use_core=True without the core is an error, not a silent fallback."""

    def test_default_uses_the_core_when_available(self):
        from st25.lpec import decoder
        calls = []
        real = _core.decode_packed

        def spy(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        if not _core.available():
            self.assertEqual(decoder.decode_payload(_payload("single-frame")),
                             python_pcm("single-frame"))
            return
        _core.decode_packed = spy
        try:
            decoder.decode_payload(_payload("single-frame"))
        finally:
            _core.decode_packed = real
        self.assertEqual(calls, [1])

    def test_forcing_the_core_without_it_raises(self):
        from st25.lpec import decoder
        saved = _core._lib
        _core._lib = None
        try:
            with self.assertRaises(RuntimeError):
                decoder.decode_payload(_payload("single-frame"), use_core=True)
            # ... and the default silently falls back to pure Python.
            self.assertEqual(decoder.decode_payload(_payload("single-frame")),
                             python_pcm("single-frame"))
        finally:
            _core._lib = saved


@unittest.skipUnless(_HAVE_TABLES,
                     "st25/lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@unittest.skipUnless(_core.available(), _NO_CORE)
class RandomPayloadParityTests(unittest.TestCase):
    """decode_payload through the C core vs. pure Python, on payloads that
    are not real recordings: seeded random bytes, and truncated (short
    input, docs/lpec.md "Short input") copies of a real one.

    Most frame fields are raw, unvalidated bit reads (bitstream.py), so
    almost any bytes parse into *some* Frame -- this is not the malformed
    *packed record* the C core defends against (MalformedRecordTests below),
    it is the ordinary parse-then-pack path on odd input. Both backends must
    still agree: the same PCM, or both raise.
    """

    def _check(self, payload):
        from st25.lpec.decoder import decode_payload
        want = want_err = got = got_err = None
        try:
            want = decode_payload(payload, use_core=False)
        except Exception as e:  # noqa: BLE001 - comparing failure, not type
            want_err = e
        try:
            got = decode_payload(payload, use_core=True)
        except Exception as e:  # noqa: BLE001
            got_err = e
        if want_err is None and got_err is None:
            self.assertEqual(got, want)
        else:
            self.assertTrue(
                want_err is not None and got_err is not None,
                f"only one backend raised: pure Python={want_err!r}, core={got_err!r}",
            )

    def test_seeded_random_bytes(self):
        rng = random.Random(20260925)
        for length in (1, 5, 17, 36, 48, 60, 100, 137, 240):
            payload = bytes(rng.randrange(256) for _ in range(length))
            with self.subTest(length=length):
                self._check(payload)

    def test_truncated_real_payload(self):
        base = _payload("tone-1000-fullscale")
        for cut in (1, 7, 17, 35, 47, 59, len(base) // 2, len(base) - 1):
            with self.subTest(cut=cut):
                self._check(base[:cut])


@unittest.skipUnless(_HAVE_TABLES,
                     "st25/lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@unittest.skipUnless(_core.available(), _NO_CORE)
class MalformedRecordTests(unittest.TestCase):
    """decode_frame's defensive checks on the packed record (st25/lpec/
    _lpec.c, ~line 950 on): a record with a field pushed out of the range
    the pure-Python parser could ever produce must be rejected (-1 from the
    C core -> decode_frames raises RuntimeError), not read out of bounds.

    Each test here starts from a real, valid frame (parsed by
    st25.lpec.bitstream, so every other field is in range) and pushes
    exactly one field out of range, isolating one of the added checks.
    A test process that returns normally (raises RuntimeError, doesn't
    crash) is itself the "no crash" evidence.
    """

    @classmethod
    def setUpClass(cls):
        from st25.lpec import bitstream, tables as tables_module
        cls.t = tables_module.load()

        # single-frame's only frame: mode 0, F=1 (both LSP slots read), both
        # pitch lags non-zero, one shape flag set -- exercises every field.
        two_block_chunks = bitstream.split_frames(_payload("single-frame"))
        cls.two_block = bitstream.parse_frame(two_block_chunks[0], 0, cls.t.AB)

        # silence's 5th frame: mode 3 (a single, slot-2-only block).
        one_block_chunks = bitstream.split_frames(_payload("silence"))
        lsp1_i1 = 0
        frame = None
        for chunk in one_block_chunks[:5]:
            frame = bitstream.parse_frame(chunk, lsp1_i1, cls.t.AB)
            lsp1_i1 = frame.lsp1_i1_next
        cls.one_block = frame
        assert cls.two_block.mode == 0 and len(cls.two_block.blocks) == 2
        assert cls.two_block.mid_frame_lsp == 1
        assert cls.two_block.pitch_a[0] and cls.two_block.pitch_b[0]
        assert cls.two_block.shape_flags == [0, 1]
        assert cls.one_block.mode == 3 and len(cls.one_block.blocks) == 1
        assert cls.one_block.blocks[0].slot == 2

    def _expect_rejected(self, frame):
        with self.assertRaises(RuntimeError):
            _core.decode_frames(self.t, [frame])

    def test_baseline_frames_are_valid(self):
        """The fixtures themselves decode, so a rejection below is really
        the one mutated field, not an already-broken fixture."""
        _core.decode_frames(self.t, [copy.deepcopy(self.two_block)])
        _core.decode_frames(self.t, [copy.deepcopy(self.one_block)])

    def test_nblocks_inconsistent_with_mode(self):
        frame = copy.deepcopy(self.two_block)
        frame.blocks = frame.blocks[:1]  # mode 0 always has 2 blocks
        self._expect_rejected(frame)

    def test_slot_1_block_in_a_mode_that_never_has_one(self):
        frame = copy.deepcopy(self.one_block)
        frame.blocks[0].slot = 1  # mode 3 only ever has a slot-2 block
        self._expect_rejected(frame)

    def test_lsp_index_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        frame.lsp_b[0] = 100  # C1/C2/C3 table rows are 0..63
        self._expect_rejected(frame)

    def test_pitch_pgidx_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        frame.pitch_a[1] = 200  # PT/PQ table rows are 0..63
        self._expect_rejected(frame)

    def test_shape_index_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        frame.shape_indices[1] = 250  # SHAPES table rows are 0..127
        self._expect_rejected(frame)

    def test_global_gain_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        frame.blocks[0].global_gain = 200  # GAIN has 128 entries
        self._expect_rejected(frame)

    def test_band_gain_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        frame.blocks[0].band_gain1 = 100  # BG1/BG2 table rows are 0..63
        self._expect_rejected(frame)

    def test_vq_index_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        self.assertGreater(len(frame.blocks[0].vq2), 0)
        frame.blocks[0].vq2[0] = 999  # VQ2/VQ4/VQ8 codebooks have 256 rows
        self._expect_rejected(frame)

    def test_band_allocation_overruns_the_band_width(self):
        frame = copy.deepcopy(self.two_block)
        frame.blocks[0].alloc.n4[0] += 20  # band 0's n4+n2+n1+ns now > W
        self._expect_rejected(frame)

    def test_band_counts_inconsistent_with_k1(self):
        frame = copy.deepcopy(self.two_block)
        frame.blocks[0].alloc.n2[3] += 4  # sum(n2) no longer matches 4*k1
        self._expect_rejected(frame)


@unittest.skipUnless(_HAVE_TABLES,
                     "st25/lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@unittest.skipUnless(_core.available(), _NO_CORE)
class DefaultShapeFromTablesTests(unittest.TestCase):
    """The C core takes the default temporal shape from Tables.DEFAULT_SHAPE
    (it has no copy of its own), so the two backends cannot drift apart:
    changing the table changes both decodes, identically."""

    def test_both_backends_follow_tables_default_shape(self):
        import dataclasses
        from st25.lpec import tables as tables_module
        from st25.lpec.decoder import decode_payload
        t = tables_module.load()
        odd = dataclasses.replace(t, DEFAULT_SHAPE=(0.5,) * 8)
        payload = _payload("tone-1000-fullscale")
        py = decode_payload(payload, odd, use_core=False)
        core = decode_payload(payload, odd, use_core=True)
        self.assertEqual(core, py)
        self.assertNotEqual(core, python_pcm("tone-1000-fullscale"))


@unittest.skipUnless(_HAVE_TABLES,
                     "st25/lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
class CancelTests(unittest.TestCase):
    """should_stop interrupts a decode (both backends) with Cancelled. The
    vectors are short, so the poll interval is lowered to 4 frames here."""

    def setUp(self):
        from unittest import mock
        from st25.lpec import decoder
        patcher = mock.patch.object(decoder, "_STOP_CHECK_FRAMES", 4)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, use_core, stop_after):
        from st25.lpec import Cancelled
        from st25.lpec.decoder import decode_payload
        calls = []

        def should_stop():
            calls.append(1)
            return len(calls) > stop_after

        with self.assertRaises(Cancelled):
            decode_payload(_payload("white-noise"), use_core=use_core, should_stop=should_stop)
        return calls

    def test_pure_python_decode_can_be_stopped(self):
        self.assertEqual(len(self._run(False, 0)), 1)

    def test_a_stop_mid_stream_is_honoured(self):
        from st25.lpec.decoder import decode_payload
        payload = _payload("white-noise")
        polls = []
        decode_payload(payload, use_core=False, should_stop=lambda: polls.append(1) or False)
        self.assertGreater(len(polls), 1, "white-noise must be long enough to poll twice")
        self.assertEqual(len(self._run(False, 1)), 2)

    @unittest.skipUnless(_core.available(), _NO_CORE)
    def test_core_decode_can_be_stopped(self):
        self._run(True, 0)

    def test_not_stopping_gives_the_normal_output(self):
        from st25.lpec.decoder import decode_payload
        got = decode_payload(_payload("silence"), use_core=False, should_stop=lambda: False)
        self.assertEqual(got, python_pcm("silence"))


if __name__ == "__main__":
    unittest.main()
