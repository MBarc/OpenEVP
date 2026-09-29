"""Vector gate for the LPEC decoder in its SP configuration (ICD-ST10 "SP":
16000 Hz, 16000 bit/s): every short test vector in tests/vectors/lpec_sp/
must decode byte-identical to its committed reference PCM.

The vectors are synthetic: PCM (tones, noise, a sweep) encoded by Sony's own
LPEC encoder at 16000 Hz (random-frames: random bytes in frames of all four
modes), packed as an ICD-ST10 SP .dvf (codec 0x2A) by tools/make_test_dvf.py,
and decoded by Sony's LPEC.dll (InitDecoder(16000, 16000), DVE's frame loop)
for the .pcm. No recorded audio.

- the short vectors in pure Python (use_core=False) and through the C core;
- the 1-minute vector's SHA-256 (C core; pure Python only with
  OPENEVP_SLOW_TESTS=1);
- dvf_to_wav: a 16000 Hz mono WAV of exactly that PCM, the frame count
  st25.dvf.sp_frames() gives, and never LP decoding of SP data (or the other
  way round).

Needs the git-ignored table data (openevp/decoders/sony_lpec/data/
lpec_sp_tables.json, tools/import_lpec_tables.py); skipped when it is absent
(a failure in the release gate).
"""

import functools
import hashlib
import io
import json
import os
import sys
import unittest
import wave
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
import make_test_dvf  # noqa: E402
from openevp.decoders.sony_lpec import TablesMissing, _core, config, tables  # noqa: E402
from st25 import dvf  # noqa: E402

VECTORS_DIR = Path(__file__).resolve().parent / "vectors" / "lpec_sp"
FRAME = 1024

SHORT_VECTORS = (
    "single-frame", "silence", "impulses", "tone-440-quiet", "tone-1000-fullscale",
    "tone-7000-clipped", "sweep-50-8000", "white-noise", "pink-noise", "random-frames",
    "ends-mid-block", "ends-mid-frame",
)


def _tables_present():
    try:
        tables.load(config=config.SP)
        return True
    except TablesMissing:
        return False


HAVE_TABLES = _tables_present()
NO_TABLES = ("openevp/decoders/sony_lpec/data/lpec_sp_tables.json not found; "
             "run tools/import_lpec_tables.py")
_SLOW_TESTS = os.environ.get("OPENEVP_SLOW_TESTS") == "1"


def _read(name, ext):
    with open(VECTORS_DIR / f"{name}.{ext}", "rb") as f:
        return f.read()


def meta(name):
    return json.loads(_read(name, "json"))


@functools.lru_cache(maxsize=None)
def vector_payload(name: str) -> bytes:
    return make_test_dvf.payload(_read(name, "dvf"))


@functools.lru_cache(maxsize=None)
def python_pcm(name: str) -> bytes:
    from openevp.decoders.sony_lpec.decoder import decode_payload
    return bytes(decode_payload(vector_payload(name), config=config.SP, use_core=False))


def _first_difference(got: bytes, want: bytes) -> str:
    import array
    a, b = array.array("h", got[:len(got) // 2 * 2]), array.array("h", want[:len(want) // 2 * 2])
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return (f"first difference at sample {i} (frame {i // FRAME}, sample {i % FRAME}): "
                    f"got {a[i]}, want {b[i]}; lengths {len(a)} / {len(b)} samples")
    return f"lengths differ: got {len(a)} samples, want {len(b)}"


class VectorFilesTests(unittest.TestCase):
    """The committed vectors are what they say (no tables needed)."""

    def test_every_vector_is_an_sp_dvf_with_its_metadata(self):
        for name in SHORT_VECTORS + ("long-mixed-1min",):
            with self.subTest(name):
                data = _read(name, "dvf")
                m = meta(name)
                self.assertIsNone(dvf.validate(data))
                self.assertEqual((dvf.codec(data), dvf.mode_of(data)), (dvf.CODEC_SP, dvf.MODE_SP))
                payload = dvf.payload(data)
                self.assertEqual(sum(int(k) * v for k, v in m["frame_sizes"].items()), len(payload))
                # DVE's loop decodes this many frames, 1024 samples each.
                self.assertEqual(dvf.sp_frames(payload) * FRAME, m["pcm_samples"])
                if m["pcm_file"]:
                    pcm = _read(name, "pcm")
                    self.assertEqual(len(pcm), 2 * m["pcm_samples"])
                    self.assertEqual(hashlib.sha256(pcm).hexdigest(), m["pcm_sha256"])

    def test_the_short_frame_at_the_end_is_decoded_as_the_dll_does(self):
        # ends-mid-frame: 24 whole frames, then a 128-byte frame cut to 98 bytes:
        # read round those 98 bytes it ends at byte 30, then 68 bytes are left,
        # read round again (ends at 60), then 8 bytes (ends at 8): 27 frames.
        m = meta("ends-mid-frame")
        self.assertEqual(m["frame_sizes"], {"98": 1, "128": 23, "160": 1})
        self.assertEqual(m["pcm_samples"], 27 * FRAME)


@release_gate.require(HAVE_TABLES, NO_TABLES)
class ShortVectorTests(unittest.TestCase):
    """Pure-Python decode_payload(payload, config=SP) == the committed .pcm."""

    def _check(self, name):
        want = _read(name, "pcm")
        got = python_pcm(name)
        if got != want:
            self.fail(f"{name}: {_first_difference(got, want)}")


def _make_test(order, name):
    def test(self):
        self._check(name)
    test.__name__ = f"test_{order:02d}_" + name.replace("-", "_")
    test.__doc__ = f"{name} decodes byte-identical to {name}.pcm"
    return test


for _order, _name in enumerate(SHORT_VECTORS, 1):
    _t = _make_test(_order, _name)
    setattr(ShortVectorTests, _t.__name__, _t)


@release_gate.require(HAVE_TABLES, NO_TABLES)
@release_gate.require(_core.available(), "openevp/decoders/sony_lpec/lpec_core.dll is not built (or failed "
                                         "to load); run python tools/build_lpec_core.py")
class CoreTests(unittest.TestCase):
    """The C core in its SP configuration == pure Python == Sony."""

    def test_short_vectors_on_the_core(self):
        from openevp.decoders.sony_lpec.decoder import decode_payload
        for name in SHORT_VECTORS:
            with self.subTest(name):
                got = bytes(decode_payload(vector_payload(name), config=config.SP, use_core=True))
                self.assertEqual(got, _read(name, "pcm"), _first_difference(got, _read(name, "pcm")))
                self.assertEqual(got, python_pcm(name))


@release_gate.require(HAVE_TABLES, NO_TABLES)
@release_gate.require(_core.available() or _SLOW_TESTS,
                      "lpec_core.dll not built (python tools/build_lpec_core.py) and pure Python takes "
                      "about a minute; build the DLL or set OPENEVP_SLOW_TESTS=1")
class LongMixedShaTests(unittest.TestCase):
    """long-mixed-1min (938 frames, 60 s): the PCM's SHA-256."""

    def test_sha256(self):
        from openevp.decoders.sony_lpec.decoder import decode_payload
        m = meta("long-mixed-1min")
        got = decode_payload(vector_payload("long-mixed-1min"), config=config.SP)
        self.assertEqual(len(got), 2 * m["pcm_samples"])
        self.assertEqual(hashlib.sha256(got).hexdigest(), m["pcm_sha256"])


@release_gate.require(HAVE_TABLES, NO_TABLES)
class WavOutputTests(unittest.TestCase):
    """dvf_to_wav picks the configuration by the codec byte."""

    def test_an_sp_file_becomes_a_16_khz_mono_wav_of_its_pcm(self):
        from openevp.decoders.sony_lpec.decoder import dvf_to_wav
        data = _read("tone-440-quiet", "dvf")
        wav = bytes(dvf_to_wav(data))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(_read("tone-440-quiet", "pcm"))
        self.assertEqual(wav, buf.getvalue())          # the canonical 44-byte header, then the PCM
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual(w.getnframes() / w.getframerate(), dvf.sp_seconds(dvf.payload(data)))

    def test_the_formats_layer_decodes_it_with_an_exact_size(self):
        from openevp import formats
        data = _read("ends-mid-frame", "dvf")
        wav = formats.DVF.decoder.to_wav(data)
        self.assertEqual(len(wav), formats.DVF.decoder.wav_bytes(data))
        self.assertEqual(bytes(wav[44:]), _read("ends-mid-frame", "pcm"))

    def test_tables_must_match_the_codec(self):
        from openevp.decoders.sony_lpec.decoder import decode_payload, dvf_to_wav
        lp = tables.load()
        with self.assertRaises(ValueError):
            dvf_to_wav(_read("single-frame", "dvf"), tables=lp)
        with self.assertRaises(ValueError):
            decode_payload(vector_payload("single-frame"), tables=lp, config=config.SP)

    def test_lp_files_still_decode_as_lp(self):
        from openevp.decoders.sony_lpec.decoder import dvf_to_wav
        lp_dir = VECTORS_DIR.parent
        with open(lp_dir / "single-frame.dvf", "rb") as f:
            wav = bytes(dvf_to_wav(f.read()))
        with open(lp_dir / "single-frame.pcm", "rb") as f:
            self.assertEqual(wav[44:], f.read())
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual(w.getframerate(), 8000)


@release_gate.require(HAVE_TABLES, NO_TABLES)
class DecoderStateTests(unittest.TestCase):
    """The SP decoder's state has the SP sizes (docs/lpec.md, "LPEC SP")."""

    def test_state_sizes_and_reset(self):
        from openevp.decoders.sony_lpec import bitstream
        from openevp.decoders.sony_lpec.decoder import Decoder
        dec = Decoder(tables.load(config=config.SP))
        self.assertEqual((len(dec.lsp0), len(dec.q0), len(dec.mem), len(dec.H), len(dec.O)),
                         (17, 17, 16, 256 + 1024, 1024))
        self.assertEqual(dec.lsp0, [i * (0.48 / 17) for i in range(17)])
        frames = bitstream.split_frames(vector_payload("tone-440-quiet"), config.SP)
        self.assertEqual({len(f) for f in frames}, {96, 128, 160})
        for chunk in frames[:6]:
            samples, used = dec.decode_frame(chunk)
            self.assertEqual((len(samples), used), (1024, len(chunk)))
        z, stale = dec.noise.z, list(dec.H[256:])
        dec.reset()
        self.assertEqual(dec.mem, [0.0] * 16)
        self.assertEqual(dec.H[:256], [0.0] * 256)
        self.assertEqual(dec.O, [0.0] * 1024)
        self.assertEqual((dec.noise.z, dec.H[256:]), (z, stale))       # not touched by ResetDecoder


class ConfigTests(unittest.TestCase):
    """The SP configuration InitDecoder(16000, 16000) derives (docs/lpec.md)."""

    def test_values(self):
        sp = config.SP
        self.assertEqual((sp.rate, sp.frame, sp.order, sp.bands, sp.lsp_stages, sp.lag_bits),
                         (16000, 1024, 16, 10, 4, 8))
        self.assertEqual(sp.mode_bytes, (128, 96, 160, 128))
        self.assertEqual(sp.mode_bytes, dvf.SP_FRAME_BYTES)
        self.assertEqual(sp.transform_n, (1024, 1536, 1536, 2048))
        self.assertEqual(sp.band_width, (48, 72, 72, 96))
        self.assertEqual(sp.end_coded, (482, 723, 723, 964))
        self.assertEqual(config.LP.mode_bytes, (48, 36, 60, 48))


if __name__ == "__main__":
    unittest.main()
