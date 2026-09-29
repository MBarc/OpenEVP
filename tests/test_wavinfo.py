import io
import os
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openevp import wavinfo  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402


def make_wav_from_pcm(data, rate=8000, ch=1, width=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(data)
    return buf.getvalue()


def make_wav(seconds=2.0, rate=8000, ch=1, width=2):
    """A WAV long enough to hold marks out past 1.75s (ruling: the plan's own
    fixture was too short for the marks its tests used)."""
    nframes = int(seconds * rate)
    data = bytes((i * 37) % 256 for i in range(nframes * ch * width))
    return make_wav_from_pcm(data, rate=rate, ch=ch, width=width)


def make_odd_sized_data_wav():
    """8-bit mono WAV whose data chunk has an odd number of bytes -- Python's
    own wave writer pads it, so this exercises with_markers()/read_markers()
    pad-byte handling on a real odd-sized chunk, not a hand-built one."""
    nframes = 4001                                    # odd: 1 byte/frame at 8-bit mono
    data = bytes((i * 3) % 256 for i in range(nframes))
    return make_wav_from_pcm(data, rate=8000, ch=1, width=1)


def make_extensible_wav(data, rate=8000, ch=1, width=2):
    """A hand-built WAVE_FORMAT_EXTENSIBLE PCM WAV with the same samples a
    plain-PCM WAV built from `data` would have."""
    block_align = ch * width
    ext = struct.pack("<HHL16s", 22, width * 8, 0, wave.KSDATAFORMAT_SUBTYPE_PCM)
    fmt_body = struct.pack("<HHLLHH", wave.WAVE_FORMAT_EXTENSIBLE, ch, rate,
                            rate * block_align, block_align, width * 8) + ext
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
    pad = b"\0" if len(data) & 1 else b""
    data_chunk = b"data" + struct.pack("<I", len(data)) + data + pad
    body = b"WAVE" + fmt_chunk + data_chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _append_chunk(wav_bytes, chunk_bytes):
    """wav_bytes with a hand-built, complete chunk appended and the RIFF size
    field updated to include it."""
    riff_size = struct.unpack_from("<I", wav_bytes, 4)[0]
    new_size = riff_size + len(chunk_bytes)
    return wav_bytes[:4] + struct.pack("<I", new_size) + wav_bytes[8:] + chunk_bytes


class FingerprintTests(unittest.TestCase):
    def test_same_samples_same_fingerprint_whatever_the_container(self):
        a = make_wav()
        marked = wavinfo.with_markers(a, [{"start": 0.1, "end": 0.3, "cls": "A", "note": "get out"}])
        self.assertNotEqual(a, marked)
        self.assertEqual(wavinfo.wav_fingerprint(io.BytesIO(a)), wavinfo.wav_fingerprint(io.BytesIO(marked)))

    def test_format_is_part_of_the_fingerprint(self):
        self.assertNotEqual(wavinfo.wav_fingerprint(io.BytesIO(make_wav(rate=8000))),
                             wavinfo.wav_fingerprint(io.BytesIO(make_wav(rate=16000))))

    def test_not_a_wav(self):
        with self.assertRaises(ValueError):
            wavinfo.wav_fingerprint(io.BytesIO(b"nope"))

    def test_empty_input_is_rejected_not_crashed_on(self):
        with self.assertRaises(ValueError):
            wavinfo.wav_fingerprint(io.BytesIO(b""))

    def test_prefix_is_versioned_and_covers_the_format(self):
        self.assertEqual(wavinfo.fingerprint_prefix(1, 2, 8000), b"pcm1:1:2:8000\n")

    def test_truncated_data_chunk_is_rejected(self):
        full = make_wav(seconds=0.5)
        truncated = full[:-500]                       # header still claims the full frame count
        with self.assertRaises(ValueError):
            wavinfo.wav_fingerprint(io.BytesIO(truncated))

    def test_should_stop_stops_between_pieces(self):
        wav = make_wav(seconds=0.5)
        with self.assertRaises(wavinfo.Stopped):
            wavinfo.wav_fingerprint(io.BytesIO(wav), should_stop=lambda: True)
        self.assertEqual(wavinfo.wav_fingerprint(io.BytesIO(wav), should_stop=lambda: False),
                         wavinfo.wav_fingerprint(io.BytesIO(wav)))

    def test_accepts_a_path(self):
        wav = make_wav()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav")
            with open(p, "wb") as f:
                f.write(wav)
            self.assertEqual(wavinfo.wav_fingerprint(p), wavinfo.wav_fingerprint(io.BytesIO(wav)))

    def test_extensible_pcm_fingerprints_like_plain_pcm(self):
        data = bytes((i * 5) % 256 for i in range(4000))
        plain = make_wav_from_pcm(data, rate=8000, ch=1, width=2)
        extensible = make_extensible_wav(data, rate=8000, ch=1, width=2)
        self.assertEqual(wavinfo.wav_fingerprint(io.BytesIO(plain)),
                          wavinfo.wav_fingerprint(io.BytesIO(extensible)))


class MarkerTests(unittest.TestCase):
    def test_round_trip_and_still_a_valid_wav(self):
        marks = [{"start": 0.25, "end": 1.0, "cls": "A", "note": "get out"},
                 {"start": 1.5, "end": 1.75, "cls": "C", "note": ""}]
        out = wavinfo.with_markers(make_wav(), marks)
        with wave.open(io.BytesIO(out)) as w:
            self.assertEqual(w.getnframes(), 16000)
        got = wavinfo.read_markers(io.BytesIO(out))
        self.assertEqual([(m["start"], m["end"], m["note"]) for m in got],
                          [(0.25, 1.0, "EVP A: get out"), (1.5, 1.75, "EVP C")])

    def test_point_mark_has_end_equal_to_start(self):
        out = wavinfo.with_markers(make_wav(), [{"start": 0.5, "end": 0.5, "cls": "B", "note": "tick"}])
        got = wavinfo.read_markers(io.BytesIO(out))
        self.assertEqual(got, [{"start": 0.5, "end": 0.5, "note": "EVP B: tick"}])

    def test_marked_parts_are_with_markers_without_the_copy(self):
        wav = bytearray(make_wav())
        marks = [{"start": 0.25, "end": 1.0, "cls": "A", "note": "get out"}]
        parts = wavinfo.marked_parts(wav, marks)
        self.assertEqual(b"".join(parts), wavinfo.with_markers(wav, marks))
        self.assertTrue(any(isinstance(p, memoryview) and p.obj is wav and len(p) > 1000 for p in parts))

    def test_buffer_file_reads_in_place(self):
        wav = bytearray(make_wav())
        with wavinfo.buffer_file(wav) as f:
            self.assertEqual(wavinfo.wav_fingerprint(f), wavinfo.wav_fingerprint(io.BytesIO(bytes(wav))))
            f.seek(0)
            self.assertEqual(f.read(4), b"RIFF")
            self.assertEqual(f.seek(-2, io.SEEK_END), len(wav) - 2)
            self.assertEqual(f.read(), bytes(wav[-2:]))
        wav += b"x"                                  # closed: the bytearray can grow again
        with wavinfo.buffer_file(b"") as f, self.assertRaises(ValueError):
            wavinfo.wav_fingerprint(f)

    def test_replacing_markers_does_not_accumulate(self):
        once = wavinfo.with_markers(make_wav(), [{"start": 0, "end": 0.5, "cls": "B", "note": "x"}])
        twice = wavinfo.with_markers(once, [{"start": 0, "end": 0.5, "cls": "B", "note": "x"}])
        self.assertEqual(once, twice)

    def test_garbage_after_data_is_ignored(self):
        self.assertEqual(wavinfo.read_markers(io.BytesIO(make_wav() + b"LIST\xff\xff\xff\x7f")), [])

    def test_a_list_that_lies_about_its_size_is_ignored_not_crashed_on(self):
        lying = b"LIST" + struct.pack("<I", 0x7FFFFFFF) + b"adtl" + \
            wavinfo._sub(b"labl", struct.pack("<I", 1) + b"hi\0")
        wav = _append_chunk(make_wav(), lying)
        self.assertEqual(wavinfo.read_markers(io.BytesIO(wav)), [])

    def test_zeroed_sample_rate_with_a_cue_chunk_does_not_crash(self):
        """A fmt chunk with a zeroed (garbage) sample rate must not blow up
        the start/rate, end/rate division once a real cue chunk is present."""
        wav = bytearray(wavinfo.with_markers(make_wav(), [{"start": 0.1, "end": 0.2, "cls": "A", "note": "x"}]))
        fmt_payload = wav.index(b"fmt ") + 8
        struct.pack_into("<I", wav, fmt_payload + 4, 0)          # zero nSamplesPerSec
        self.assertEqual(wavinfo.read_markers(io.BytesIO(bytes(wav))), [])

    def test_missing_path_returns_no_markers_not_an_exception(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "does-not-exist.wav")
            self.assertEqual(wavinfo.read_markers(missing), [])

    def test_non_ascii_note_round_trips(self):
        out = wavinfo.with_markers(make_wav(), [{"start": 0.1, "end": 0.2, "cls": "A", "note": "café ☕ günther"}])
        got = wavinfo.read_markers(io.BytesIO(out))
        self.assertEqual(got[0]["note"], "EVP A: café ☕ günther")

    def test_odd_sized_data_chunk_padding_is_handled(self):
        wav = make_odd_sized_data_wav()
        out = wavinfo.with_markers(wav, [{"start": 0.1, "end": 0.3, "cls": "A", "note": "x"}])
        with wave.open(io.BytesIO(out)) as w:
            self.assertEqual(w.getnframes(), 4001)
        got = wavinfo.read_markers(io.BytesIO(out))
        self.assertEqual([(m["start"], m["end"], m["note"]) for m in got], [(0.1, 0.3, "EVP A: x")])

    def test_read_markers_accepts_a_path(self):
        out = wavinfo.with_markers(make_wav(), [{"start": 0.2, "end": 0.4, "cls": "A", "note": "x"}])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.wav")
            with open(p, "wb") as f:
                f.write(out)
            got = wavinfo.read_markers(p)
        self.assertEqual([(m["start"], m["end"], m["note"]) for m in got], [(0.2, 0.4, "EVP A: x")])

    def test_read_markers_never_reads_the_data_chunk(self):
        """A file object whose data-chunk bytes explode if read must still work:
        read_markers() is only allowed to seek past 'data', never read it."""

        class ExplodingData(io.BytesIO):
            def __init__(self, wav_bytes, data_start, data_end):
                super().__init__(wav_bytes)
                self._data_start, self._data_end = data_start, data_end

            def read(self, size=-1):
                pos = self.tell()
                if self._data_start <= pos < self._data_end:
                    raise AssertionError("read_markers() read inside the data chunk")
                return super().read(size)

        wav = wavinfo.with_markers(make_wav(), [{"start": 0.1, "end": 0.2, "cls": "A", "note": "x"}])
        chunks = wavinfo._strict_chunks(memoryview(wav))
        cid, off, size = next(c for c in chunks if c[0] == b"data")
        f = ExplodingData(wav, off, off + size)
        got = wavinfo.read_markers(f)
        self.assertEqual([(m["start"], m["end"], m["note"]) for m in got], [(0.1, 0.2, "EVP A: x")])

    def test_with_markers_rejects_non_wav_bytes(self):
        with self.assertRaises(ValueError):
            wavinfo.with_markers(b"not a wav at all", [])

    def test_with_markers_rejects_bad_riff_size(self):
        wav = bytearray(make_wav())
        struct.pack_into("<I", wav, 4, 999999)         # RIFF size no longer matches the actual length
        with self.assertRaises(ValueError):
            wavinfo.with_markers(bytes(wav), [])

    def test_with_markers_rejects_missing_data_chunk(self):
        fmt_body = struct.pack("<HHLLHH", 1, 1, 8000, 16000, 2, 16)
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        wav = b"RIFF" + struct.pack("<I", len(body)) + body
        with self.assertRaises(ValueError):
            wavinfo.with_markers(wav, [])


class GoldenFingerprintTests(unittest.TestCase):
    """EVP marks are keyed by this fingerprint. If either test fails, every
    recording's marks are re-keyed (existing marks no longer match their
    recordings): a change to the fingerprint or to the decoder's output needs
    a migration, not a new golden value."""

    VECTORS = os.path.join(os.path.dirname(__file__), "vectors")
    GOLDEN = "22da7d09128a98010dd035a10917654fde0553ddc3d3a4756b72687d52bde2c7"   # sweep-50-4000

    def test_fingerprint_of_the_reference_pcm(self):
        with open(os.path.join(self.VECTORS, "sweep-50-4000.pcm"), "rb") as f:
            pcm = f.read()
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(pcm)
        self.assertEqual(wavinfo.wav_fingerprint(io.BytesIO(out.getvalue())), self.GOLDEN)

    def test_fingerprint_of_the_decoded_vector(self):
        from st25 import audio
        if not audio.available():
            release_gate.skip_or_fail(f"WAV conversion is not available: {audio.status()}")
        with open(os.path.join(self.VECTORS, "sweep-50-4000.dvf"), "rb") as f:
            wav = audio.dvf_to_wav(f.read())
        self.assertEqual(wavinfo.wav_fingerprint(io.BytesIO(wav)), self.GOLDEN)


if __name__ == "__main__":
    unittest.main()
