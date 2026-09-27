"""Vector gate for the LPEC decoder: every short test vector in
tests/vectors/ must decode byte-identical to its committed reference PCM.

The frame stream of a vector is tools/make_test_dvf.payload() of its .dvf;
the reference .pcm is little-endian int16, decoded by Sony's own decoder. The short vectors are decoded in pure
Python (use_core=False), which keeps the fallback path covered;
test_lpec_core checks the C core against these same decodes. The long
10-minute vector only has a SHA-256; its test (LongMixedShaTests) runs on
the C core when lpec_core.dll is built (a few seconds), and in pure Python
(several minutes) only when OPENEVP_SLOW_TESTS=1 is set.

Needs the git-ignored extracted table data (openevp/decoders/sony_lpec/data/lpec_tables.json);
skipped when it is absent.
"""

import functools
import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from openevp.decoders.sony_lpec import TablesMissing, _core, tables  # noqa: E402
import make_test_dvf  # noqa: E402

VECTORS_DIR = Path(__file__).resolve().parent / "vectors"
FRAME_SAMPLES = 512

# In the order the brief asks for: the simplest first, then the rest.
SHORT_VECTORS = (
    "single-frame", "silence", "lsb-noise",
    "impulses", "steps", "tone-440-quiet", "tone-1000-fullscale",
    "tone-3500-clipped", "sweep-50-4000", "white-noise", "pink-noise",
    "ends-mid-block", "ends-mid-frame",
)


def _tables_present():
    try:
        tables.load()
        return True
    except TablesMissing:
        return False


_HAVE_TABLES = _tables_present()
_SLOW_TESTS = os.environ.get("OPENEVP_SLOW_TESTS") == "1"


def _first_difference(got: bytes, want: bytes) -> str:
    """Describe where two little-endian int16 PCM streams first differ."""
    n_got, n_want = len(got) // 2, len(want) // 2
    a = struct.unpack(f"<{n_got}h", got[:2 * n_got])
    b = struct.unpack(f"<{n_want}h", want[:2 * n_want])
    for i in range(min(n_got, n_want)):
        if a[i] != b[i]:
            return (
                f"first difference at sample {i} (frame {i // FRAME_SAMPLES}, "
                f"sample {i % FRAME_SAMPLES} in the frame): got {a[i]}, "
                f"want {b[i]}; lengths {n_got} / {n_want} samples"
            )
    return f"lengths differ: got {n_got} samples, want {n_want}"


@functools.lru_cache(maxsize=None)
def vector_payload(name: str) -> bytes:
    """The frame stream of tests/vectors/<name>.dvf."""
    with open(VECTORS_DIR / f"{name}.dvf", "rb") as f:
        return make_test_dvf.payload(f.read())


@functools.lru_cache(maxsize=None)
def python_pcm(name: str) -> bytes:
    """The pure-Python decode of a vector (cached: test_lpec_core compares
    the C core against it)."""
    from openevp.decoders.sony_lpec.decoder import decode_payload
    return decode_payload(vector_payload(name), use_core=False)


@release_gate.require(
    _HAVE_TABLES,
    "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py",
)
class ShortVectorTests(unittest.TestCase):
    """Pure-Python decode_payload(payload) == the committed .pcm, byte for
    byte."""

    def _check(self, name):
        with open(VECTORS_DIR / f"{name}.pcm", "rb") as f:
            want = f.read()
        got = python_pcm(name)
        if got != want:
            self.fail(f"{name}: {_first_difference(got, want)}")


def _make_test(order, name):
    def test(self):
        self._check(name)
    # Numbered so unittest (which sorts by name) runs them in brief order.
    test.__name__ = f"test_{order:02d}_" + name.replace("-", "_")
    test.__doc__ = f"{name} decodes byte-identical to {name}.pcm"
    return test


for _order, _name in enumerate(SHORT_VECTORS, 1):
    _t = _make_test(_order, _name)
    setattr(ShortVectorTests, _t.__name__, _t)


class BytesConsumedTests(unittest.TestCase):
    """The reader's byte position after a frame (docs/lpec.md, "API
    behaviour and framing", "Short input")."""

    def test_a_complete_frame_consumes_its_length(self):
        from openevp.decoders.sony_lpec.decoder import _bytes_consumed
        self.assertEqual(_bytes_consumed(48, 48), 48)
        self.assertEqual(_bytes_consumed(36, 60), 36)

    def test_a_short_frame_wraps_and_reports_its_second_pass_position(self):
        from openevp.decoders.sony_lpec.decoder import _bytes_consumed
        # 48 bytes read from 36: all 36, then 12 more from the start.
        self.assertEqual(_bytes_consumed(48, 36), 12)
        # 48 bytes read from 24: exactly two passes, ends at the end.
        self.assertEqual(_bytes_consumed(48, 24), 24)


@release_gate.require(
    _HAVE_TABLES,
    "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py",
)
class WavOutputTests(unittest.TestCase):
    """dvf_to_wav: the canonical 44-byte header Digital Voice Editor writes,
    then decode_payload's PCM, nothing else."""

    # RIFF/WAVE, 16-byte 'fmt ' (PCM, mono, 8000 Hz, 16000 B/s, block align 2,
    # 16-bit), 'data': exactly the bytes Digital Voice Editor writes.
    _FMT_CHUNK = bytes.fromhex(
        "01 00 01 00 40 1f 00 00 80 3e 00 00 02 00 10 00".replace(" ", ""))

    def test_header_is_the_canonical_44_bytes_then_the_pcm(self):
        from openevp.decoders.sony_lpec.decoder import decode_payload, dvf_to_wav
        import make_test_dvf

        with open(VECTORS_DIR / "single-frame.dvf", "rb") as f:
            dvf_bytes = f.read()
        pcm = decode_payload(make_test_dvf.payload(dvf_bytes))

        wav = dvf_to_wav(dvf_bytes)

        self.assertEqual(len(wav), 44 + len(pcm))
        self.assertEqual(wav[0:4], b"RIFF")
        self.assertEqual(struct.unpack("<I", wav[4:8])[0], 36 + len(pcm))
        self.assertEqual(wav[8:12], b"WAVE")
        self.assertEqual(wav[12:16], b"fmt ")
        self.assertEqual(struct.unpack("<I", wav[16:20])[0], 16)
        self.assertEqual(wav[20:36], self._FMT_CHUNK)
        self.assertEqual(wav[36:40], b"data")
        self.assertEqual(struct.unpack("<I", wav[40:44])[0], len(pcm))
        self.assertEqual(wav[44:], pcm)

    def test_matches_pythons_own_wave_module(self):
        """The header is exactly what Python's wave module writes for this PCM."""
        import io
        import wave
        from openevp.decoders.sony_lpec.decoder import decode_payload, dvf_to_wav
        import make_test_dvf

        with open(VECTORS_DIR / "tone-440-quiet.dvf", "rb") as f:
            dvf_bytes = f.read()
        pcm = decode_payload(make_test_dvf.payload(dvf_bytes))

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(pcm)
        want = buf.getvalue()

        self.assertEqual(dvf_to_wav(dvf_bytes), want)

    def test_rejects_a_damaged_or_non_lp_file(self):
        from st25 import dvf
        from openevp.decoders.sony_lpec.decoder import dvf_to_wav

        with self.assertRaises(dvf.FormatError):
            dvf_to_wav(b"not a dvf file at all")


@release_gate.require(
    _core.available() or _SLOW_TESTS,
    "lpec_core.dll not built (python tools/build_lpec_core.py) and pure Python "
    "takes ~4 min; build the DLL or set OPENEVP_SLOW_TESTS=1",
)
class LongMixedShaTests(unittest.TestCase):
    """long-mixed-10min (9,375 frames): decode_payload's PCM (the C core
    when built, else pure Python) matches the committed SHA-256
    (docs/lpec.md, "Status"). Also reports (does not assert) how long the
    decode took."""

    @release_gate.require(
        _HAVE_TABLES,
        "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py",
    )
    def test_sha256_matches_and_reports_timing(self):
        import hashlib
        import json
        import time
        from openevp.decoders.sony_lpec.decoder import decode_payload
        import make_test_dvf

        with open(VECTORS_DIR / "long-mixed-10min.dvf", "rb") as f:
            payload = make_test_dvf.payload(f.read())
        with open(VECTORS_DIR / "long-mixed-10min.json") as f:
            meta = json.load(f)

        start = time.perf_counter()
        got = decode_payload(payload)
        elapsed = time.perf_counter() - start

        backend = "C core" if _core.available() else "pure Python"
        print(f"\nlong-mixed-10min ({backend}): decoded {meta['frames']} frames in "
              f"{elapsed:.1f} s ({elapsed / meta['frames'] * 1000:.1f} ms/frame)")

        self.assertEqual(len(got), meta["pcm_samples"] * 2)
        self.assertEqual(hashlib.sha256(got).hexdigest(), meta["pcm_sha256"])


@release_gate.require(
    _HAVE_TABLES,
    "openevp/decoders/sony_lpec/data/lpec_tables.json not found; run tools/import_lpec_tables.py",
)
class DecoderStateTests(unittest.TestCase):
    """reset() is the partial ResetDecoder of docs/lpec.md, "State between
    frames"."""

    def test_reset_clears_only_what_resetdecoder_clears(self):
        from openevp.decoders.sony_lpec import bitstream
        from openevp.decoders.sony_lpec.decoder import Decoder

        dec = Decoder()
        with open(VECTORS_DIR / "tone-440-quiet.dvf", "rb") as f:
            frames = bitstream.split_frames(make_test_dvf.payload(f.read()))
        for chunk in frames[:6]:
            dec.decode_frame(chunk)
        z, lsp1_i1, stale = dec.noise.z, dec.lsp1_i1, list(dec.H[256:])
        self.assertNotEqual(z, 0)
        self.assertTrue(any(stale))

        dec.reset()

        self.assertEqual(dec.lsp0, list(dec.t.LSP_INIT))
        self.assertEqual(dec.q0[0], 0)
        self.assertEqual((dec.lag0, dec.taps0), (0, [0.0, 0.0, 0.0]))
        self.assertEqual(dec.flags, [0, 0, 0])
        self.assertEqual(dec.shapes, [[1.0] * 8] * 3)
        self.assertEqual(dec.mem, [0.0] * 10)
        self.assertEqual(dec.H[:256], [0.0] * 256)
        self.assertEqual(dec.O, [0.0] * 512)
        # Not touched by ResetDecoder:
        self.assertEqual(dec.noise.z, z)
        self.assertEqual(dec.lsp1_i1, lsp1_i1)
        self.assertEqual(dec.H[256:], stale)


if __name__ == "__main__":
    unittest.main()
