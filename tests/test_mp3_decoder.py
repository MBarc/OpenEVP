"""The MP3 decoder (openevp.decoders.mp3: minimp3 in mp3_core.dll) and MP3 as a
recording format (openevp.formats): decode vectors made with lameenc at 8 kHz
mono, 16 kHz mono and 44.1 kHz stereo (rate, channels, length within the
encoder's delay and padding, the tone's frequency), the committed vectors
(tests/vectors/mp3, tools/make_mp3_vectors.py: layer II, a Xing VBR file),
determinism, damaged and truncated files, sniffing (.mpeg audio or video), and
what happens without the C core (DecoderUnavailable, never a DecodeError).

Every test that decodes needs mp3_core.dll (tools/build_lpec_core.py): skipped
without it, a failure in the release gate."""
import hashlib
import io
import json
import math
import os
import struct
import sys
import unittest
import wave
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
from openevp import formats, mp3  # noqa: E402
from openevp.decoders import mp3 as mp3dec  # noqa: E402
from openevp.decoders.mp3 import _core  # noqa: E402

VECTORS = os.path.join(os.path.dirname(__file__), "vectors", "mp3")
NO_CORE = f"the MP3 decoder is not built ({mp3dec.reason()}; python tools/build_lpec_core.py)"
NO_LAMEENC = "lameenc is not installed (pip install -r requirements-app.txt)"


def tone_pcm(rate, channels, seconds, freqs=(440.0, 660.0)):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    x = np.stack([0.5 * np.sin(2 * math.pi * freqs[c] * t) for c in range(channels)], axis=1)
    return np.round(x * 32767).astype("<i2").tobytes()


def lame(rate, channels, seconds=2.0, kbps=None):
    """An MP3 of the tone, encoded with lameenc as OpenEVP's clips are (no tag)."""
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps or (64 if rate in mp3.MPEG25_RATES else 128))
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(2)
    return bytes(enc.encode(tone_pcm(rate, channels, seconds))) + bytes(enc.flush())


def vector(name):
    with open(os.path.join(VECTORS, name), "rb") as f:
        return f.read()


def wav_params(wav):
    with wave.open(io.BytesIO(bytes(wav))) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes(), w.readframes(w.getnframes())


def peak_hz(pcm, rate, channels, channel=0):
    x = np.frombuffer(pcm, "<i2").reshape(-1, channels)[:, channel].astype(float)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.argmax(spectrum) * rate / len(x)


@release_gate.require(mp3dec.available(), NO_CORE)
@release_gate.require(mp3.available(), NO_LAMEENC)
class LameVectorTests(unittest.TestCase):
    def check(self, rate, channels):
        data = lame(rate, channels)
        wav = mp3dec.to_wav(data)
        got_rate, got_channels, width, frames, pcm = wav_params(wav)
        self.assertEqual((got_rate, got_channels, width), (rate, channels, 2))
        # LAME adds its delay (576 + 529 samples) in front and pads the last frame: at most 3 frames more.
        spf = 1152 if rate >= 32000 else 576
        self.assertGreaterEqual(frames, 2 * rate)
        self.assertLessEqual(frames, 2 * rate + 3 * spf)
        for c, want in zip(range(channels), (440.0, 660.0)):
            self.assertAlmostEqual(peak_hz(pcm, rate, channels, c), want, delta=2.0)
        self.assertEqual(mp3dec.to_wav(data), wav)                       # deterministic
        out = io.BytesIO()
        self.assertEqual(mp3dec.write_wav(data, out), len(wav))
        self.assertEqual(out.getvalue(), bytes(wav))                    # streamed: the same WAV
        got = mp3dec.seconds(data)
        self.assertEqual(got[:2], (rate, channels))
        self.assertAlmostEqual(got[2], frames / rate)
        self.assertLessEqual(abs(mp3dec.wav_bytes(data) - len(wav)), 4 * spf * channels * 2)

    def test_8k_mono(self):
        self.check(8000, 1)

    def test_16k_mono(self):
        self.check(16000, 1)

    def test_44k_stereo(self):
        self.check(44100, 2)

    def test_an_openevp_clip_with_its_tag(self):
        """A clip as OpenEVP exports it (ID3v2.3 title + comment, then the frames)."""
        data = mp3.id3("EVP A at 1:54.0: hello", "hello") + lame(8000, 1)
        self.assertEqual(mp3dec.to_wav(data), mp3dec.to_wav(lame(8000, 1)))   # the tag is skipped
        self.assertTrue(mp3dec.sniff(data[:mp3dec.SNIFF_BYTES]))
        self.assertEqual(mp3dec.to_wav(data + b"TAG" + bytes(125)), mp3dec.to_wav(data))   # an ID3v1 tag too


@release_gate.require(mp3dec.available(), NO_CORE)
class CommittedVectorTests(unittest.TestCase):
    """The committed vectors decode to the PCM recorded when they were made: the same file
    and the same decoder give the same samples, build after build."""

    def test_vectors(self):
        with open(os.path.join(VECTORS, "vectors.json"), encoding="utf-8") as f:
            want = json.load(f)
        self.assertEqual(len(want), 5)
        for name, w in want.items():
            with self.subTest(name):
                channels, width, rate, chunks = mp3dec.stream(vector(name))
                pcm = b"".join(chunks)
                self.assertEqual((rate, channels, width, len(pcm) // (2 * channels)),
                                 (w["rate"], w["channels"], 2, w["frames"]))
                self.assertEqual(hashlib.sha256(pcm).hexdigest(), w["pcm_sha256"])
                self.assertAlmostEqual(peak_hz(pcm, rate, channels), 440.0, delta=3.0)

    def test_layer_ii_and_xing(self):
        self.assertEqual(mp3dec.frame_header(vector("tone-32k-stereo.mp2"))[:4], (3, 2, 32000, 2))
        data = vector("vbr-xing-22k.mp3")
        self.assertIn(b"Xing", data[:200])
        frames, pos = 0, 0
        while (h := mp3dec.frame_header(data, pos)) is not None:
            frames, pos = frames + 1, pos + h[5]
        self.assertEqual(mp3dec.seconds(data)[2] * 22050, (frames - 1) * 576)   # the Xing frame: no audio


@release_gate.require(mp3dec.available(), NO_CORE)
class DamagedTests(unittest.TestCase):
    def test_truncated_decodes_what_is_there(self):
        data = vector("tone-44k-stereo.mp3")
        whole = wav_params(mp3dec.to_wav(data))[3]
        for cut in (len(data) // 2, len(data) - 100, 1000):
            part = wav_params(mp3dec.to_wav(data[:cut]))[3]
            self.assertTrue(0 < part < whole, (cut, part))

    def test_garbage_in_the_middle_is_skipped(self):
        data = vector("tone-16k-mono.mp3")
        mid = len(data) // 2
        damaged = data[:mid] + bytes(range(256)) * 8 + data[mid + 500:]
        frames = wav_params(mp3dec.to_wav(damaged))[3]
        self.assertTrue(10000 < frames < 17280, frames)
        noisy = bytearray(data)
        rng = np.random.default_rng(1)
        for at in rng.integers(0, len(noisy), 300):                      # flipped bytes everywhere
            noisy[at] ^= 0xFF
        mp3dec.to_wav(bytes(noisy))                                      # decodes something or says why; never crashes

    def test_nothing_to_decode_is_a_plain_error(self):
        for data in (b"", b"ID3\x03\x00\x00\x00\x00\x00\x00", b"hello, this is text\n" * 100, bytes(5000),
                     vector("tone-8k-mono.mp3")[:300]):
            with self.subTest(data=data[:20]), self.assertRaisesRegex(mp3dec.Mp3Error, "not an MP3 file"):
                mp3dec.to_wav(data)
        with self.assertRaises(mp3dec.Mp3Error):
            mp3dec.seconds(b"junk" * 100)

    def test_random_data_never_crashes(self):
        rng = np.random.default_rng(7)
        for i in range(40):
            data = rng.integers(0, 256, int(rng.integers(0, 20000)), dtype=np.uint8).tobytes()
            if i % 2:
                data = b"\xff\xfb\x90\x44" + data                        # a frame header, then garbage
            try:
                mp3dec.to_wav(data)
            except mp3dec.Mp3Error:
                pass

    def test_too_long_is_refused_plainly(self):
        with mock.patch.object(mp3dec, "MAX_PCM_BYTES", 10000):
            with self.assertRaisesRegex(mp3dec.Mp3Error, "too long"):
                mp3dec.to_wav(vector("tone-16k-mono.mp3"))
        with mock.patch.object(mp3dec, "MAX_BYTES", 100):
            with self.assertRaisesRegex(mp3dec.Mp3Error, "too large"):
                mp3dec.to_wav(vector("tone-16k-mono.mp3"))

    def test_cancelled(self):
        with self.assertRaises(mp3dec.Cancelled):
            mp3dec.to_wav(vector("tone-8k-mono.mp3"), should_stop=lambda: True)


class SniffTests(unittest.TestCase):
    """sniff(): from the first SNIFF_BYTES only; no decoder needed."""

    def test_mp3_files(self):
        for name in ("tone-8k-mono.mp3", "tone-16k-mono.mp3", "tone-44k-stereo.mp3", "tone-32k-stereo.mp2",
                     "vbr-xing-22k.mp3"):
            self.assertTrue(mp3dec.sniff(vector(name)[:mp3dec.SNIFF_BYTES]), name)
        self.assertTrue(mp3dec.sniff(mp3.id3("EVP A at 0:01.0") + b"anything"))      # an ID3v2 tag

    def test_other_files(self):
        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(bytes(1000))
        video = vector("video.mpeg")
        self.assertEqual(video[:4], b"\x00\x00\x01\xba")                           # an MPEG-PS pack header
        for name, data in (("MPEG-PS video", video), ("text", b"hello, this is text\n" * 50), ("empty", b""),
                           ("WAV", wav.getvalue()), ("zeros", bytes(4096)), ("one sync", b"\xff\xfb\x90\x44" + bytes(100)),
                           ("junk then frames", b"junk" + vector("tone-8k-mono.mp3"))):
            self.assertFalse(mp3dec.sniff(data[:mp3dec.SNIFF_BYTES]), name)

    def test_formats(self):
        for ext in (".mp3", ".mpeg", ".mpga", ".mp2", ".m2a", ".MPEG"):
            fmt = formats.by_ext(ext)
            self.assertEqual((fmt.label, fmt.decoder, fmt.max_bytes), ("MP3", formats.MP3_DECODER, mp3dec.MAX_BYTES))
        self.assertIsNone(formats.WAV.sniff)


@release_gate.require(mp3dec.available(), NO_CORE)
class FormatTests(unittest.TestCase):
    def test_analyze_streams_and_matches_the_wav(self):
        from openevp import wavinfo
        data = vector("tone-44k-stereo.mp3")
        fmt = formats.by_ext(".mpeg")
        fp, length = formats.analyze(fmt, data)
        wav = fmt.decoder.to_wav(data)
        with wavinfo.buffer_file(wav) as f:
            self.assertEqual(wavinfo.wav_fingerprint(f), fp)                      # pcm1: of the decoded PCM
        self.assertAlmostEqual(length, 46080 / 44100)
        self.assertEqual(formats.analyze(formats.MP3, data)[0], fp)              # the extension changes nothing

    def test_errors_in_the_formats_terms(self):
        fmt = formats.MP3
        with self.assertRaises(formats.DecodeError):
            fmt.decoder.to_wav(b"not an mp3" * 50)
        with self.assertRaises(formats.DecodeError):
            formats.analyze(fmt, b"not an mp3" * 50)
        with self.assertRaises(formats.Cancelled):
            fmt.decoder.to_wav(vector("tone-8k-mono.mp3"), should_stop=lambda: True)
        out = io.BytesIO()
        self.assertEqual(formats.write_wav(fmt, vector("tone-8k-mono.mp3"), out), 44 + 9216 * 2)

    def test_seconds_from_the_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.mpeg")
            with open(path, "wb") as f:
                f.write(vector("tone-16k-mono.mp3"))
            self.assertEqual(formats.MP3.seconds(path), round(17280 / 16000, 1))
            self.assertTrue(formats.MP3.is_format(path))
            with open(path, "wb") as f:
                f.write(vector("video.mpeg"))
            self.assertFalse(formats.MP3.is_format(path))
            self.assertIsNone(formats.MP3.seconds(path))
            self.assertFalse(formats.MP3.is_format(os.path.join(tmp, "missing.mp3")))


class UnavailableTests(unittest.TestCase):
    """Without mp3_core.dll there is no MP3 decoding at all: DecoderUnavailable, with a reason."""

    def test_without_the_core(self):
        with mock.patch.object(_core, "_lib", None):
            self.assertFalse(mp3dec.available())
            self.assertIn("mp3_core.dll", mp3dec.reason())
            self.assertIn("MP3 decoder", formats.decoder_problem(formats.MP3))
            with self.assertRaisesRegex(mp3dec.Unavailable, "mp3_core.dll"):
                mp3dec.to_wav(b"\xff\xfb\x90\x44" * 100)
            for call in (lambda: formats.MP3.decoder.to_wav(vector("tone-8k-mono.mp3")),
                         lambda: formats.analyze(formats.MP3, vector("tone-8k-mono.mp3")),
                         lambda: formats.write_wav(formats.MP3, vector("tone-8k-mono.mp3"), io.BytesIO())):
                with self.assertRaises(formats.DecoderUnavailable) as e:
                    call()
                self.assertNotIsInstance(e.exception, formats.DecodeError)
            self.assertTrue(mp3dec.sniff(vector("tone-8k-mono.mp3")))              # sniffing needs no core

    def test_a_dll_with_another_abi_does_not_load(self):
        if not _core.DLL_PATH.is_file():
            release_gate.skip_or_fail(NO_CORE)
        with mock.patch.object(_core, "_ABI_VERSION", 999):
            self.assertIsNone(_core._load())
        self.assertIsNotNone(_core._load())


if __name__ == "__main__":
    unittest.main()
