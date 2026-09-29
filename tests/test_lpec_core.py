"""The optional C core (openevp/decoders/sony_lpec/_lpec.c -> lpec_core.dll) must be an exact
drop-in for the pure-Python decoder.

- Its x87 fcos emulation matches openevp.decoders.sony_lpec.x87.fcos bit for bit.
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
import release_gate  # noqa: E402
from openevp.decoders.sony_lpec import x87  # noqa: E402
from openevp.decoders.sony_lpec import _core  # noqa: E402
from test_lpec_vectors import _HAVE_TABLES, SHORT_VECTORS, python_pcm  # noqa: E402
from test_lpec_vectors import vector_payload as _payload  # noqa: E402
from test_lpec_sp_vectors import HAVE_TABLES as _HAVE_SP_TABLES  # noqa: E402

_NO_CORE = ("openevp/decoders/sony_lpec/lpec_core.dll is not built (or failed to load); run "
            "python tools/build_lpec_core.py")


def _bits(x: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", x))[0]


@release_gate.require(_core.available(), _NO_CORE)
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


@release_gate.require(_HAVE_TABLES,
                      "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@release_gate.require(_core.available(), _NO_CORE)
class CoreIdenticalOutputTests(unittest.TestCase):
    """decode_payload through the C core == the pure-Python decode."""

    def _check(self, name):
        from openevp.decoders.sony_lpec.decoder import decode_payload
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


@release_gate.require(_HAVE_TABLES,
                      "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
class BackendSelectionTests(unittest.TestCase):
    """use_core=None picks the core when it loaded, else pure Python;
    use_core=True without the core is an error, not a silent fallback."""

    def test_default_uses_the_core_when_available(self):
        from openevp.decoders.sony_lpec import decoder
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
        from openevp.decoders.sony_lpec import decoder
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


@release_gate.require(_HAVE_TABLES,
                      "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@release_gate.require(_core.available(), _NO_CORE)
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
        from openevp.decoders.sony_lpec.decoder import decode_payload
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


# SHA-256 of decode_payload(random_payload(seed)), seeds 0..49, recorded from
# the LP decoder before it took a configuration (branch feature/st10-decoder,
# C core and pure Python agreeing): the LP output must never change.
PINNED_LP_SHA256 = (
    "4ad6403744aa94e3ef8d43a91cedb1f3321d3b93c3279fc24e0845a53487cc8f",
    "de60ddbb0fd185c43c16a600d9dda8f98b69d8e51afbe02bf1a747f5f2610beb",
    "959ceba702bddd36025c72ec3f5a214c403c9aec735fbc985b473b6447ee9611",
    "a6e330b29de5ada87519319718186bed2527f967f8d00d447fd047b5d634ba92",
    "72975ed3f07d2e510964541cbc37895dfa1bb7b09c50a0e35cf535f44d0e3738",
    "f771913ff43c0d131cc93feead522c98e7b5c22da6a2654e1f72b4a8a8c3d3e1",
    "0073774c70b94d3bed720e0886a537ed37c2bca203f2321247a7736f1803a2ee",
    "13db74cc783d5ddd58d6f6d20877d58fd7c11221f9feaeb89b8286850aaf73ec",
    "20e5f14c1fc1bf69fca986e97355a811cd319a55b6e76dd5a6dc33eab1895fb3",
    "dc2ab1f41f2aac0b7c2522caba04ce21d5aa737e291a536a514fe57b68278b79",
    "3c500e583c37301af1c4e1edcac49e0d2ed7517d252b0e64656604a6914b2e38",
    "c004798f2bc299e236c64b3892aaa563d669699f0ac8ab975fb3dd933b0576cc",
    "1b4a1dc67bc1f3c8534465e8086b47535d99fa8571d20073f1781bf6f2888574",
    "9c1fe31f24db8eaeae0b6b82ed8b4166d79ea619c255a50031ec7c6fc5e79245",
    "addac6efee758b7e19d7e5120941513e22964c82e8285ff638eb5587c2501069",
    "4fce80b7aadbbecc67516f32a8761f8bbc84feff1feb1d29b24bd0f819c93da7",
    "eca88fbbb19c770758101c2f7e0bcee89c33e217479623c172ffeba57c799d57",
    "f00c9568336f059c20504e10d5cc23f8df3d5bffebfb7dbc78f026936ad92a8e",
    "dae4a49380e43adbbae7127705a94f6b6484cffaa8e8b5a4c6fb545fa30edd6d",
    "b1be9df97a79856fd9a3a47c5f41ede6634db0133572e5c85e51a33c82c50e96",
    "c7bc4fceb81f02b4e4eb45fe22ecab14df45f7a6ec8617d2d7d58b43dd171fc4",
    "d4717425cce03f69540574c671c98a5369faf1565f2f65eabbb1d64c4cffe83d",
    "39d55b136ffd6e4b1f0501f672c43519d0e6cbdeb8a3830dd9d1f4e42c2ad784",
    "4ca897404ce8ecc19b968ba16a5cf17c21b1adc999342bc516168cfa3cd2a41d",
    "671a07c06a46c1d5adb3d43fa4979b286e443bcea35e69f8e573a7f203f24e1e",
    "4815dedc7b0b5dd8b178f89b60cf7d180688319b626b1538e11cb90a58cf1216",
    "70dabc48fd2a28f807b4e34385dc74a5cef6aabe24bb52d70c87ea8db385a02f",
    "0b3be6e456875069db71b9d7108c16ee9e21c5e84f1db60fa45873461eb4d26f",
    "444074e298f25adc0275fba0426b702ff5e383ac29419cdd82eff19ed41c49ba",
    "5df20f2d36e11a6a66324a29e1a541b8e6587ced95f9f7f97dff59111da42215",
    "ad9564a81ed02956fc1a0841ffd092bf434fcb7a16a13a3bc5b2b16b003ca689",
    "0707b7a8fab8cc29dc59796e53a12dc9bf0d368a653f67b4e80941d29d72d005",
    "851d01520fa85978ea3c46dce3859f05774c286fb0a43ea0a46e831e6c7dc523",
    "22825dc59eb125835d4b632af7b6c11b8ba8a9e472da6ac45166ba9f83bfd473",
    "4c61e79cda856b835a304f37015c60e0005dfa77460a48cb9ac25e35e4a30305",
    "445cab3f613f1385c39a5aa6337dba073a4e5c1197b543c27709ed03fe68656b",
    "e28d6d82f19a425364c3a1e46083b5681da0b823162998415c6694107bbb594f",
    "59d5d60cc1b154f41afcda19b203fccb838149ea37628a42c72f2f50503399f6",
    "2f41899d527074eed3d0be08801e61a62247cf1e5e81bae2b8a299047e8ecf27",
    "40b253272c116bab14222e5807c7b424bf040c8f9a80b77b0f27433a055db7e7",
    "2f6cd15ea87bf0c8f0cf1cfd35b93b977192fedc29438c5827a4e47f6d783f40",
    "885520e0786ae5db47c149e748933331087aed1605c74c68a9e4d74bffdcf7fc",
    "f5b16b8946341113c8deaef2fde1f218a6047ec31717b0366649d57fdd1a1240",
    "c224e34c5213357c7a7c1eb43e8626c11bd0918d81a53050eb9c279b3b57628b",
    "51385463f220db18ba4515548b2c51fd3468a214a59318014011b2348688cc7a",
    "723b47132371a86917099850e638e8145b495b1d5231b62fb5e1cd572e54c607",
    "cc7941d2637b8ff16e0fe74c2c8c04b546e49a56e6cec54667e16f6d2df0eb3c",
    "e18a125477ecde23f61ca7e24e5d549a9e324343a0c51b2ccbf73a3920ef6603",
    "c58dbe474e788953686392578d1c5917ff2f09d80b7a1573c53aaf47ed2f8f7d",
    "2857570e0960b29be5e94f70ee6946eb612549c393733e83e15a844d577b1d98",
)


def random_payload(seed):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(rng.randrange(1, 480)))


@release_gate.require(_HAVE_TABLES,
                      "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
class PinnedLpOutputTests(unittest.TestCase):
    """LP output is pinned to the decoder before the SP configuration existed."""

    def _check(self, use_core):
        import hashlib
        from openevp.decoders.sony_lpec.decoder import decode_payload
        for seed, want in enumerate(PINNED_LP_SHA256):
            with self.subTest(seed=seed):
                got = decode_payload(random_payload(seed), use_core=use_core)
                self.assertEqual(hashlib.sha256(got).hexdigest(), want)

    def test_pure_python(self):
        self._check(False)

    @release_gate.require(_core.available(), _NO_CORE)
    def test_c_core(self):
        self._check(True)


@release_gate.require(_HAVE_TABLES,
                      "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@release_gate.require(_core.available(), _NO_CORE)
class MalformedRecordTests(unittest.TestCase):
    """decode_frame's defensive checks on the packed record (openevp/decoders/sony_lpec/
    _lpec.c, ~line 950 on): a record with a field pushed out of the range
    the pure-Python parser could ever produce must be rejected (-1 from the
    C core -> decode_frames raises RuntimeError), not read out of bounds.

    Each test here starts from a real, valid frame (parsed by
    openevp.decoders.sony_lpec.bitstream, so every other field is in range) and pushes
    exactly one field out of range, isolating one of the added checks.
    A test process that returns normally (raises RuntimeError, doesn't
    crash) is itself the "no crash" evidence.
    """

    @classmethod
    def setUpClass(cls):
        from openevp.decoders.sony_lpec import bitstream, tables as tables_module
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


@release_gate.require(_HAVE_SP_TABLES, "openevp/decoders/sony_lpec/data/lpec_sp_tables.json not found; "
                                       "run tools/import_lpec_tables.py")
@release_gate.require(_core.available(), _NO_CORE)
class MalformedSpRecordTests(unittest.TestCase):
    """The same defensive checks with the SP configuration's limits (8-bit
    lags up to 255, 4 LSP stages, 10 bands up to 96 wide)."""

    @classmethod
    def setUpClass(cls):
        from openevp.decoders.sony_lpec import bitstream, config, tables as tables_module
        from test_lpec_sp_vectors import vector_payload as sp_payload
        cls.t = tables_module.load(config=config.SP)
        frames, lsp1 = [], 0
        for chunk in bitstream.split_frames(sp_payload("random-frames"), config.SP):
            f = bitstream.parse_frame(chunk, lsp1, cls.t.AB, config.SP)
            lsp1 = f.lsp1_i1_next
            frames.append(f)
        cls.two_block = next(f for f in frames if f.mode == 0 and f.pitch_b[0] and f.blocks[1].vq8)
        cls.one_block = next(f for f in frames if f.mode == 3)

    def _expect_rejected(self, frame):
        with self.assertRaises(RuntimeError):
            _core.decode_frames(self.t, [frame])

    def test_baseline_frames_are_valid(self):
        _core.decode_frames(self.t, [copy.deepcopy(self.two_block)])
        _core.decode_frames(self.t, [copy.deepcopy(self.one_block)])

    def test_lag_255_is_valid_and_256_is_not(self):
        frame = copy.deepcopy(self.two_block)
        frame.pitch_b[0] = 255                     # the widest 8-bit lag: fine
        if frame.pitch_b[1] is None:
            frame.pitch_b[1] = 0
        _core.decode_frames(self.t, [frame])
        frame.pitch_b[0] = 256
        self._expect_rejected(frame)

    def test_lp_packing_is_rejected(self):
        frame = copy.deepcopy(self.one_block)
        frame.lsp_b = frame.lsp_b[:3]              # an LP-shaped (3-stage) record
        self._expect_rejected(frame)

    def test_fourth_lsp_index_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        frame.lsp_b[3] = 64
        self._expect_rejected(frame)

    def test_band_wider_than_96(self):
        frame = copy.deepcopy(self.one_block)       # type 3: W = 96, the widest band
        a = frame.blocks[0].alloc
        a.ns[9] += 97 - (a.n4[9] + a.n2[9] + a.n1[9] + a.ns[9])
        a.ks = sum(a.ns)
        frame.blocks[0].signs = frame.blocks[0].signs + [0] * (a.ks - len(frame.blocks[0].signs))
        self._expect_rejected(frame)

    def test_band_allocation_overruns_the_band_width(self):
        frame = copy.deepcopy(self.two_block)       # type 0: W = 48
        frame.blocks[0].alloc.n4[9] += 50
        self._expect_rejected(frame)

    def test_vq_index_out_of_range(self):
        frame = copy.deepcopy(self.two_block)
        frame.blocks[1].vq8[0] = 256
        self._expect_rejected(frame)

    def test_tables_that_do_not_match_the_configuration_are_refused(self):
        import dataclasses
        short = dataclasses.replace(self.t, C=tuple(tuple(r[:10] for r in c) for c in self.t.C))
        with self.assertRaises(ValueError):
            _core.decode_frames(short, [copy.deepcopy(self.one_block)])
        narrow = dataclasses.replace(self.t, AB=tuple(r[:8] for r in self.t.AB))
        with self.assertRaises(ValueError):
            _core.decode_frames(narrow, [copy.deepcopy(self.one_block)])


@release_gate.require(_HAVE_TABLES,
                      "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
@release_gate.require(_core.available(), _NO_CORE)
class DefaultShapeFromTablesTests(unittest.TestCase):
    """The C core takes the default temporal shape from Tables.DEFAULT_SHAPE
    (it has no copy of its own), so the two backends cannot drift apart:
    changing the table changes both decodes, identically."""

    def test_both_backends_follow_tables_default_shape(self):
        import dataclasses
        from openevp.decoders.sony_lpec import tables as tables_module
        from openevp.decoders.sony_lpec.decoder import decode_payload
        t = tables_module.load()
        odd = dataclasses.replace(t, DEFAULT_SHAPE=(0.5,) * 8)
        payload = _payload("tone-1000-fullscale")
        py = decode_payload(payload, odd, use_core=False)
        core = decode_payload(payload, odd, use_core=True)
        self.assertEqual(core, py)
        self.assertNotEqual(core, python_pcm("tone-1000-fullscale"))


@release_gate.require(_HAVE_TABLES,
                      "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py")
class CancelTests(unittest.TestCase):
    """should_stop interrupts a decode (both backends) with Cancelled. The
    vectors are short, so the poll interval is lowered to 4 frames here."""

    def setUp(self):
        from unittest import mock
        from openevp.decoders.sony_lpec import decoder
        patcher = mock.patch.object(decoder, "_STOP_CHECK_FRAMES", 4)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, use_core, stop_after):
        from openevp.decoders.sony_lpec import Cancelled
        from openevp.decoders.sony_lpec.decoder import decode_payload
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
        from openevp.decoders.sony_lpec.decoder import decode_payload
        payload = _payload("white-noise")
        polls = []
        decode_payload(payload, use_core=False, should_stop=lambda: polls.append(1) or False)
        self.assertGreater(len(polls), 1, "white-noise must be long enough to poll twice")
        self.assertEqual(len(self._run(False, 1)), 2)

    @release_gate.require(_core.available(), _NO_CORE)
    def test_core_decode_can_be_stopped(self):
        self._run(True, 0)

    def test_not_stopping_gives_the_normal_output(self):
        from openevp.decoders.sony_lpec.decoder import decode_payload
        got = decode_payload(_payload("silence"), use_core=False, should_stop=lambda: False)
        self.assertEqual(got, python_pcm("silence"))


if __name__ == "__main__":
    unittest.main()
