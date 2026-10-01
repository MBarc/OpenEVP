"""MP3 clips (openevp.mp3, openevp.clips.make): encoding at the recording's own
rate, the ID3v2.3 tag, determinism (a second export finds the clip already
saved), the clip format setting (Api.clip_format / set_clip_format, MP3 by
default) and what happens without lameenc; and MP3 files in the EVP Library:
recordings anywhere (decoded by openevp.decoders.mp3, played as WAV, marked,
counted), clips in the Clips folders OpenEVP made, whatever the extension
(.mpeg from WhatsApp Web), never a file that only has the extension.

The encoding tests need lameenc (requirements-app.txt): skipped without it, a
failure in the release gate."""
import io
import math
import os
import struct
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
import test_library as tl  # noqa: E402
import test_marks_api as tm  # noqa: E402
from app import backend, library_ops  # noqa: E402
from app.audio_server import AudioServer  # noqa: E402
from app.store import AppData  # noqa: E402
from openevp import clips, formats, mp3, wavinfo  # noqa: E402
from openevp.decoders import mp3 as mp3dec  # noqa: E402
from openevp.decoders.mp3 import _core as _mp3core  # noqa: E402

NO_LAMEENC = "lameenc is not installed (pip install -r requirements-app.txt)"
WAIT = 30

# MPEG audio frame headers: bitrates (kbps) by version, sample rates by version, samples per frame.
_BITRATES = {3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),        # MPEG-1 layer III
             2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}             # MPEG-2 and 2.5
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def tone_wav(rate, channels, seconds, width=2, fmt_tag=1):
    """A WAV of a 440 Hz tone (left) and 660 Hz (right): 16-bit unless width says otherwise
    (1, 3 or 4; fmt_tag 3 with width 4 is 32-bit float)."""
    n = int(rate * seconds)
    out = []
    for i in range(n):
        for c in range(channels):
            x = 0.5 * math.sin(2 * math.pi * (440 + 220 * c) * i / rate)
            if fmt_tag == 3:
                out.append(struct.pack("<f", x))
            elif width == 1:
                out.append(bytes([int(128 + 127 * x)]))
            else:
                out.append(struct.pack("<i", int(x * (2 ** (8 * width - 1) - 1)))[:width])
    data = b"".join(out)
    align = channels * width
    fmt = struct.pack("<HHIIHH", fmt_tag, channels, rate, rate * align, align, 8 * width)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def read_id3(data):
    """({frame id: decoded text}, tag length) of an ID3v2.3 tag at the start of data."""
    assert data[:5] == b"ID3\x03\x00", data[:10]
    size = sum((b & 0x7F) << (7 * (3 - i)) for i, b in enumerate(data[6:10]))
    frames, pos, end = {}, 10, 10 + size
    while pos + 10 <= end and data[pos:pos + 4] != b"\0\0\0\0":
        fid = data[pos:pos + 4].decode("ascii")
        n = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 10:pos + 10 + n]
        enc, rest = body[0], body[1:]
        if fid == "COMM":
            assert rest[:3] == b"eng"
            rest = rest[3:]
            term = b"\0" if enc == 0 else b"\0\0"
            if enc == 0:
                rest = rest[rest.index(term) + 1:]
            else:                                            # BOM + an empty description, 2-byte aligned
                at = next(i for i in range(0, len(rest), 2) if rest[i:i + 2] == term)
                rest = rest[at + 2:]
        frames[fid] = rest.decode("latin-1") if enc == 0 else rest.decode("utf-16")
        pos += 10 + n
    return frames, end


def mpeg_frames(data):
    """[(sample rate, kbps, channels, samples)] of every MPEG audio frame in data, which
    must be nothing but frames (no gap, no garbage)."""
    found, pos = [], 0
    while pos < len(data):
        head = data[pos:pos + 4]
        assert len(head) == 4, f"a truncated frame at {pos}"
        h = int.from_bytes(head, "big")
        assert h >> 21 == 0x7FF, f"no frame sync at byte {pos}"
        version, layer = (h >> 19) & 3, (h >> 17) & 3
        assert version != 1 and layer == 1, f"not MPEG layer III at {pos}"
        kbps = _BITRATES[3 if version == 3 else 2][(h >> 12) & 15]
        rate = _RATES[version][(h >> 10) & 3]
        padding, mode = (h >> 9) & 1, (h >> 6) & 3
        samples = 1152 if version == 3 else 576
        size = samples // 8 * kbps * 1000 // rate + padding
        found.append((rate, kbps, 1 if mode == 3 else 2, samples))
        pos += size
    assert pos == len(data), "the last frame runs past the end"
    return found


def mark(start, end, cls="A", note=""):
    return {"start": start, "end": end, "cls": cls, "note": note}


@release_gate.require(mp3.available(), NO_LAMEENC)
class EncodeTests(unittest.TestCase):
    def check(self, rate, channels, kbps):
        wav = tone_wav(rate, channels, 3.0)
        m = mark(1.0, 1.25, "B", "who's there")
        clip = clips.make(wav, m, "mp3")
        tags, at = read_id3(clip)
        self.assertEqual(tags, {"TIT2": "EVP B at 0:01.0: who's there", "COMM": "who's there"})
        frames = mpeg_frames(clip[at:])
        self.assertEqual({(f[0], f[1], f[2]) for f in frames}, {(rate, kbps, channels)})   # never resampled
        seconds = sum(f[3] for f in frames) / rate
        wav_clip = clips.cut(wav, m)                                      # the same bounds as the WAV clip
        with wave.open(io.BytesIO(wav_clip)) as w:
            want = w.getnframes() / w.getframerate()
        self.assertAlmostEqual(want, 1.25)
        # LAME adds its encoder delay and pads the last frame: at most a few frames more.
        self.assertGreaterEqual(seconds, want)
        self.assertLess(seconds - want, 3 * frames[0][3] / rate + 0.01, (rate, seconds, want))
        self.assertEqual(clips.make(wav, m, "mp3"), clip)                 # deterministic, tag and all
        return clip

    def test_8k_mono_is_64_kbps_mpeg_2_5(self):
        self.check(8000, 1, 64)                                          # LAME's highest at 8 kHz

    def test_16k_mono_is_128_kbps(self):
        self.check(16000, 1, 128)

    def test_44k_stereo_is_128_kbps(self):
        self.check(44100, 2, 128)

    def test_other_sample_formats_are_made_16_bit(self):
        for width, tag in ((1, 1), (3, 1), (4, 1), (4, 3)):
            clip = clips.make(tone_wav(8000, 1, 1.0, width=width, fmt_tag=tag), mark(0.2, 0.3), "mp3")
            frames = mpeg_frames(clip[read_id3(clip)[1]:])
            self.assertEqual(frames[0][:3], (8000, 64, 1), (width, tag))

    def test_more_than_two_channels_is_refused(self):
        with self.assertRaisesRegex(ValueError, "mono or stereo"):
            clips.make(tone_wav(8000, 3, 1.0), mark(0.2, 0.3), "mp3")

    def test_a_mark_outside_the_audio_is_refused_as_for_wav(self):
        with self.assertRaises(ValueError):
            clips.make(tone_wav(8000, 1, 1.0), mark(5.0, 5.5), "mp3")
        with self.assertRaises(ValueError):
            clips.make(tone_wav(8000, 1, 1.0), mark(0.1, 0.2), "ogg")


class Id3Tests(unittest.TestCase):
    def test_latin1_and_utf16_texts(self):
        tags, _at = read_id3(mp3.id3("EVP A at 0:12.4: hello", "hello"))
        self.assertEqual(tags, {"TIT2": "EVP A at 0:12.4: hello", "COMM": "hello"})
        tag = mp3.id3("EVP C at 1:02.0: 你好 – ok", "你好 – ok")
        self.assertEqual(read_id3(tag)[0], {"TIT2": "EVP C at 1:02.0: 你好 – ok", "COMM": "你好 – ok"})
        self.assertNotIn(b"\x00\x00\x00\x00", tag[10:14])

    def test_no_note_no_comment_and_a_long_note(self):
        self.assertEqual(read_id3(mp3.id3("EVP A at 0:00.5"))[0], {"TIT2": "EVP A at 0:00.5"})
        self.assertEqual(mp3.id3("", ""), b"")
        note = "n" * 500                                                  # the longest note a mark has
        tags, at = read_id3(mp3.id3("t: " + note, note))
        self.assertEqual((tags["COMM"], at), (note, len(mp3.id3("t: " + note, note))))   # syncsafe size

    def test_titles(self):
        self.assertEqual(clips.title(mark(12.4, 13.0, "A")), "EVP A at 0:12.4")
        self.assertEqual(clips.title(mark(754.0, 755.0, "B", "  get\n out ")), "EVP B at 12:34.0: get out")
        self.assertEqual(clips.title(mark(59.96, 60.0, "C")), "EVP C at 1:00.0")
        self.assertEqual(clips.name("x", mark(1.0, 2.0, "B", "get out"), fmt="mp3"), "x_EVP-B_00m01.0s_get out.mp3")


class SettingTests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api

    def test_mp3_by_default_and_remembered(self):
        self.assertEqual(self.api.clip_format(), "mp3")
        caps = self.api.capabilities()
        self.assertEqual((caps["clip_format"], caps["mp3"]), ("mp3", mp3.available()))
        self.assertEqual(self.api.set_clip_format("wav"), {"ok": True, "format": "wav", "remembered": True})
        self.assertEqual(self.store.get_setting(backend.CLIP_FORMAT), "wav")
        self.assertEqual(self.new_api().clip_format(), "wav")              # the next start
        self.assertEqual(self.api.set_clip_format("flac")["error"], "Unknown clip format.")
        self.assertEqual(self.api.set_clip_format(None)["ok"], False)
        self.assertEqual(self.api.clip_format(), "wav")
        self.store.set_setting(backend.CLIP_FORMAT, "nonsense")           # a damaged setting: the default
        self.assertEqual(self.api.clip_format(), "mp3")

    def test_a_second_window_keeps_it_for_the_session(self):
        second = AppData(os.path.join(self.tmp, "appdata"))                # the first holds the lock
        self.addCleanup(second.close)
        api = self.new_api(store=second)
        self.assertEqual(api.set_clip_format("wav"), {"ok": True, "format": "wav", "remembered": False})
        self.assertEqual(api.clip_format(), "wav")
        self.assertEqual(self.api.clip_format(), "mp3")                    # not remembered

    def test_no_store(self):
        api = backend.Api(self.m, self.emit, lambda start: None, self.dest, self.server, store=None)
        self.addCleanup(api.shutdown)
        self.assertEqual(api.clip_format(), "mp3")
        self.assertEqual(api.set_clip_format("wav")["remembered"], False)
        self.assertEqual(api.clip_format(), "wav")


@release_gate.require(mp3.available(), NO_LAMEENC)
class PlayerMp3Tests(unittest.TestCase):
    setUp = tm.MarksApiTests.setUp
    new_api = tm.MarksApiTests.new_api
    wait_event = tm.MarksApiTests.wait_event
    load = tm.MarksApiTests.load

    def test_export_clips_and_save_clip_as_mp3(self):
        rec = self.load()["rec"]
        a = self.api.add_mark(rec, 0.1, 0.4, "A", "hi there")["mark"]
        self.assertEqual(self.wait_event()[0], "backup-done")
        self.api.add_mark(rec, 0.6, 0.7, "C", "")
        r = self.api.export_clips(rec)
        stem = os.path.splitext(tm.DVF_1)[0]
        names = [f"{stem}_EVP-A_00m00.1s_hi there.mp3", f"{stem}_EVP-C_00m00.6s.mp3"]
        self.assertEqual((r["ok"], r["saved"], r["already"], r["names"], r["notes"]), (True, 2, 0, names, []))
        folder = os.path.join(self.dest, "A", backend.CLIPS)
        self.assertEqual(r["folder"], folder)
        with open(os.path.join(folder, names[0]), "rb") as f:
            data = f.read()
        tags, at = read_id3(data)
        self.assertEqual(tags, {"TIT2": "EVP A at 0:00.1: hi there", "COMM": "hi there"})
        frames = mpeg_frames(data[at:])
        self.assertEqual(frames[0][:3], (8000, 64, 1))
        # Again: the same bytes, so already saved; nothing overwritten or numbered.
        r = self.api.export_clips(rec)
        self.assertEqual((r["saved"], r["already"], r["names"]), (0, 2, names))
        self.assertEqual(sorted(os.listdir(folder)), sorted(names + [library_ops.CLIPS_MARKER]))
        # Save clip of a moved mark: other bytes, a numbered copy.
        self.api.update_mark(rec, a["id"], end=0.5)
        r = self.api.export_clips(rec, a["id"])
        self.assertEqual((r["saved"], r["names"]), (1, [f"{stem}_EVP-A_00m00.1s_hi there (2).mp3"]))
        # WAV and MP3 clips of one mark live side by side.
        self.api.set_clip_format("wav")
        r = self.api.export_clips(rec, a["id"])
        self.assertEqual((r["saved"], r["names"]), (1, [f"{stem}_EVP-A_00m00.1s_hi there.wav"]))
        self.assertFalse(self.api._busy.locked())


class LibraryMp3Tests(unittest.TestCase):
    setUp = tl.LibraryTests.setUp
    new_api = tl.LibraryTests.new_api
    write = tl.LibraryTests.write
    index = tl.LibraryTests.index

    def add_mark(self, data, start, end, cls="A", note=""):
        from openevp import wavinfo
        self.store.add_mark(wavinfo.wav_fingerprint(io.BytesIO(data)), start, end, cls, note, name="x", duration=2.0)

    def run_job(self, start):
        r = start()
        self.assertTrue(r["ok"], r)
        with self.events.cond:
            ok = self.events.cond.wait_for(
                lambda: any(n in ("clips-done", "clips-failed") and p["job"] == r["job"] for n, p in self.events.items),
                WAIT)
        self.assertTrue(ok, self.events.items)
        return next((n, p) for n, p in self.events.items if n in ("clips-done", "clips-failed") and p["job"] == r["job"])

    @release_gate.require(mp3.available(), NO_LAMEENC)
    def test_folder_job_saves_mp3_and_skips_identical_ones(self):
        one = tm.wav_bytes(b"one", seconds=2.0)
        two = tm.wav_bytes(b"two", seconds=2.0, rate=16000)
        self.write("Case/Night 1/one.wav", one)
        self.write("Case/Night 1/Deeper/two.wav", two)
        self.add_mark(one, 0.5, 0.7, "A", "hello")
        self.add_mark(two, 1.5, 1.6, "C", "knock")
        api = self.new_api()
        self.index(api)
        event, p = self.run_job(lambda: api.export_clips_folder("root", 1))
        self.assertEqual((event, p["saved"], p["already"], p["recordings"], p["notes"]), ("clips-done", 2, 0, 2, []))
        clips_dir = os.path.join(self.lib, "Case", backend.CLIPS)
        self.assertEqual(sorted(os.listdir(clips_dir)),
                         [library_ops.CLIPS_MARKER, "one_EVP-A_00m00.5s_hello.mp3", "two_EVP-C_00m01.5s_knock.mp3"])
        with open(os.path.join(clips_dir, "two_EVP-C_00m01.5s_knock.mp3"), "rb") as f:
            data = f.read()
        self.assertEqual(read_id3(data)[0]["COMM"], "knock")
        self.assertEqual(mpeg_frames(data[read_id3(data)[1]:])[0][:3], (16000, 128, 1))
        event, p = self.run_job(lambda: api.export_clips_folder("root", 2))
        self.assertEqual((p["saved"], p["already"]), (0, 2))
        self.assertEqual(len(os.listdir(clips_dir)), 3)

    @release_gate.require(mp3.available(), NO_LAMEENC)
    def test_the_format_is_taken_when_the_job_starts(self):
        one = tm.wav_bytes(b"one", seconds=2.0)
        self.write("Case/one.wav", one)
        self.add_mark(one, 0.5, 0.7, "A")
        api = self.new_api()
        self.index(api)
        real = api._save_clips

        def switch_then_save(*a):
            api.set_clip_format("wav")                                    # changed while the job runs
            return real(*a)
        with mock.patch.object(api, "_save_clips", switch_then_save):
            event, p = self.run_job(lambda: api.export_clips_folder("root", 1))
        self.assertEqual(p["saved"], 1)
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Case", backend.CLIPS)))[1:],
                         ["one_EVP-A_00m00.5s.mp3"])

    def test_without_lameenc_mp3_is_refused_plainly_and_wav_still_works(self):
        one = tm.wav_bytes(b"one", seconds=2.0)
        path = self.write("Case/one.wav", one)
        self.add_mark(one, 0.5, 0.7, "A")
        with mock.patch.object(mp3, "lameenc", None):
            api = self.new_api()
            self.index(api)
            self.assertEqual(api.capabilities()["mp3"], False)
            self.assertEqual(api.capabilities()["mp3_status"], mp3.UNAVAILABLE)
            self.assertEqual(mp3.UNAVAILABLE, "MP3 export isn't available in this build; use WAV.")
            r = api.export_clips_folder("root", 1)
            self.assertEqual((r["ok"], r["error"]), (False, mp3.UNAVAILABLE))
            self.assertFalse(api._busy.locked())
            ids = [f["id"] for f in self.index(api)["files"]]
            self.assertEqual(api.export_clips_files(ids, 2)["error"], mp3.UNAVAILABLE)
            api._pick_wav = lambda start: path
            rec = api.open_wav()["rec"]
            self.assertEqual(api.export_clips(rec)["error"], mp3.UNAVAILABLE)
            with self.assertRaisesRegex(RuntimeError, "isn't available"):
                clips.make(one, mark(0.5, 0.7), "mp3")
            self.assertFalse(os.path.exists(os.path.join(self.lib, "Case", backend.CLIPS)))
            # WAV is unaffected.
            api.set_clip_format("wav")
            r = api.export_clips(rec)
            self.assertEqual((r["ok"], r["names"]), (True, ["one_EVP-A_00m00.5s.wav"]))
            event, p = self.run_job(lambda: api.export_clips_folder("root", 3))
            self.assertEqual((event, p["already"]), ("clips-done", 1))
            self.assertFalse(api._busy.locked())



def mp3_clip(seconds=3.0, rate=8000, channels=1, start=1.0, note="clip"):
    return clips.make(tone_wav(rate, channels, seconds), mark(start, start + 0.25, "B", note), "mp3")


def mp3_recording(seconds=3.0, rate=8000, channels=1):
    """An MP3 of a tone, as another program (or WhatsApp) would save it: no tag."""
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(64 if rate in mp3.MPEG25_RATES else 128)
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(2)
    wav = tone_wav(rate, channels, seconds)
    return bytes(enc.encode(wav[44:])) + bytes(enc.flush())


def mp3_vector(name):
    with open(os.path.join(os.path.dirname(__file__), "vectors", "mp3", name), "rb") as f:
        return f.read()


NO_CORE = f"the MP3 decoder is not built ({mp3dec.reason()}; python tools/build_lpec_core.py)"


@release_gate.require(mp3.available(), NO_LAMEENC)
@release_gate.require(mp3dec.available(), NO_CORE)
class LibraryMp3RecordingsTests(unittest.TestCase):
    """MP3 files in the EVP Library, with the fixtures of test_library and a real audio
    server: recordings everywhere (listed, indexed, fingerprinted, played as a decoded
    WAV, marked, counted, exported), clips in the Clips folders OpenEVP made (listed,
    played and marked like WAV clips, never counted), whatever extension an MP3 has
    (.mpeg from WhatsApp Web) -- and never a file that only has an MP3 extension."""
    new_api, write, index = tl.LibraryTests.new_api, tl.LibraryTests.write, tl.LibraryTests.index
    run_job = LibraryMp3Tests.run_job

    def setUp(self):
        tl.LibraryTests.setUp(self)
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.server = AudioServer(None, cache.name)
        self.server.start()
        self.addCleanup(self.server.stop)

    def populate(self):
        self.song = mp3_recording()
        self.write("Case/song.mp3", self.song)
        # A clip OpenEVP exported, shared over WhatsApp and saved back by WhatsApp Web as .mpeg.
        self.whatsapp = mp3_clip(note="hello")
        self.write("Case/WhatsApp Audio 2026-09-30 at 11.30.05 PM.mpeg", self.whatsapp)
        self.write("Case/video.mpeg", mp3_vector("video.mpeg"))               # an MPEG video: never listed
        self.write("Case/notes.mp3", b"hello, this is text\n" * 50)          # a renamed text file: never listed
        self.write("Case/empty.mpeg", b"")
        self.write("Case/damaged.mp3", mp3.id3("EVP A at 0:01.0") + b"junk" * 500)   # a tag, then nothing
        library_ops._make_clips_folder(os.path.join(self.lib, "Case", "Clips"))
        self.clip = mp3_clip(rate=16000, note="clip")                         # not the WhatsApp copy's audio
        self.write("Case/Clips/song_EVP-B_00m01.0s_clip.mp3", self.clip)
        self.write("Other/Clips/mine.mp3", self.song)                         # the user's own "Clips" folder

    def fp(self, data):
        return formats.analyze(formats.MP3, data)[0]

    def rows(self, r):
        """{name: row} of a listing, with what its indexer reported."""
        events = self.events.rows(r["scan_id"])
        return {f["name"]: {**f, **events.get(f["id"], {})} for f in r["files"]}

    def test_listed_anywhere_indexed_and_counted(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        got = self.rows(r)
        whatsapp = "WhatsApp Audio 2026-09-30 at 11.30.05 PM.mpeg"
        self.assertEqual(sorted(got), sorted(["song.mp3", whatsapp, "damaged.mp3",
                                              "song_EVP-B_00m01.0s_clip.mp3", "mine.mp3"]))
        song = got["song.mp3"]
        self.assertEqual((song["type"], song["clip"], song["error"], song["unplayable"], song["fp"]),
                         ("mp3", False, None, None, self.fp(self.song)))
        self.assertAlmostEqual(song["seconds"], 3.1, delta=0.15)
        self.assertEqual((got[whatsapp]["type"], got[whatsapp]["fp"]), ("mpeg", self.fp(self.whatsapp)))
        self.assertEqual(got["mine.mp3"]["fp"], song["fp"])                   # a copy: the same recording
        self.assertTrue(got["song_EVP-B_00m01.0s_clip.mp3"]["clip"])
        self.assertEqual(got["song_EVP-B_00m01.0s_clip.mp3"]["fp"], self.fp(self.clip))
        self.assertIsNone(got["damaged.mp3"]["fp"])
        self.assertIn("not an MP3 file", got["damaged.mp3"]["error"])
        # Indexed: the next listing comes from the cache.
        again = self.rows(self.index(api))
        self.assertEqual(again["song.mp3"]["fp"], song["fp"])
        # Marks count like a WAV's (the clip's are an EVP already: the page never counts clips).
        self.store.add_mark(song["fp"], 0.5, 0.7, "A", "who", name="song.mp3", duration=3.0)
        self.store.add_mark(got[whatsapp]["fp"], 0.5, 0.6, "B", "", name=whatsapp, duration=1.0)
        r = self.index(api)
        rows = self.rows(r)
        self.assertEqual((rows["song.mp3"]["marks"], rows[whatsapp]["marks"]["B"]), ({"A": 1, "B": 0, "C": 0}, 1))
        self.assertEqual(api.capabilities()["formats"]["mpeg"], {"label": "MP3", "playable": True, "reason": None})
        # Delete's folder info: the video, the text and the empty file are other files.
        case = next(d["id"] for d in r["folders"] if d["rel"] == ["Case"])
        info = api.folder_info(case)
        self.assertEqual((info["recordings"], info["clips"], info["with_evps"]), (3, 1, 2))
        self.assertGreaterEqual(info["other_files"], 4)

    def test_a_sniff_is_read_once_per_version(self):
        self.populate()
        api = self.new_api()
        reads = []
        real = formats.Format.is_format

        def spy(fmt, path):
            reads.append(os.path.basename(path))
            return real(fmt, path)
        with mock.patch.object(formats.Format, "is_format", spy):
            self.index(api)
            first = len(reads)
            self.index(api)
            self.assertEqual(len(reads), first)                                # cached by (size, mtime)
            path = os.path.join(self.lib, "Case", "video.mpeg")
            with open(path, "wb") as f:                                        # replaced by an MP3: listed now
                f.write(self.song + b"\0")
            names = [f["name"] for f in self.index(api)["files"]]
        self.assertEqual(reads[first:], ["video.mpeg"])
        self.assertIn("video.mpeg", names)

    def test_listing_reads_only_an_mp3s_header(self):
        """list_library() runs on the page's call: without a store (so nothing is indexed) an
        MP3's length comes from its headers, never from reading its audio."""
        import builtins
        long_mp3 = mp3_vector("tone-44k-stereo.mp3") * 200                    # ~3.3 MB, 209 s
        self.write("Case/long.mp3", long_mp3)
        self.write("Case/long.mpeg", mp3.id3("EVP A at 0:01.0") + long_mp3)
        real_open, read = builtins.open, []

        def counting_open(*a, **k):
            f = real_open(*a, **k)
            if "b" in (a[1] if len(a) > 1 else k.get("mode", "r")):
                real_read = f.read

                def counted(n=-1):
                    got = real_read(n)
                    read.append((os.path.basename(a[0]), len(got)))
                    return got
                f.read = counted
            return f
        api = self.new_api(store=None)
        with mock.patch.object(formats, "open", counting_open, create=True),                 mock.patch.object(backend, "open", counting_open, create=True):
            r = api.list_library()
        rows = {f["name"]: f for f in r["files"]}
        self.assertEqual((r["indexing"], rows["long.mp3"]["seconds"], rows["long.mpeg"]["seconds"]),
                         (False, 209.0, 209.0))
        for name in ("long.mp3", "long.mpeg"):
            got = sum(n for who, n in read if who == name)
            self.assertTrue(0 < got <= 4 * mp3dec.SNIFF_BYTES, (name, read))      # its sniff and length

    def test_marks_follow_the_audio_across_a_rename_to_mpeg(self):
        self.populate()
        api = self.new_api()
        self.index(api)
        fp = self.fp(self.song)
        self.store.add_mark(fp, 1.0, 1.2, "C", "knock", name="song.mp3", duration=3.0)
        os.rename(os.path.join(self.lib, "Case", "song.mp3"), os.path.join(self.lib, "Case", "song.mpeg"))
        r = self.index(api)
        row = self.rows(r)["song.mpeg"]
        self.assertEqual((row["fp"], row["marks"]["C"]), (fp, 1))
        p = api.play_library(row["id"])
        self.assertEqual(([m["note"] for m in p["marks"]], p["fp"]), (["knock"], fp))

    def test_played_as_a_decoded_wav_marked_and_exported(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        fid = next(f["id"] for f in r["files"] if f["name"] == "song.mp3")
        p = api.play_library(fid)
        self.assertTrue(p["ok"], p)
        self.assertTrue(p["url"].endswith(".wav"))
        self.assertNotIn("compressed", p)
        self.assertNotIn("markable", p)
        self.assertEqual((p["rate"], p["channels"], p["fp"]), (8000, 1, self.fp(self.song)))
        self.assertTrue(p["peaks"])
        with urllib.request.urlopen(p["url"], timeout=5) as resp:
            body = resp.read()
            self.assertEqual(resp.headers["Content-Type"], "audio/wav")
        self.assertEqual(body, bytes(formats.MP3.decoder.to_wav(self.song)))
        self.assertEqual(api.play_library(fid)["url"], p["url"])               # decoded once (the disk cache)
        rec = p["rec"]
        added = api.add_mark(rec, 1.0, 1.3, "A", "a voice")
        self.assertTrue(added["ok"], added)
        self.assertEqual(api.set_reviewed(rec, True)["ok"], True)
        # Save with marks: a WAV with RIFF markers, into the investigation's folder.
        saved = api.export_marked(rec)
        self.assertEqual((saved["ok"], saved["name"], saved["folder_name"]), (True, "song.wav", "Case"), saved)
        out = os.path.join(self.lib, "Case", "song.wav")
        self.assertEqual([(round(m["start"], 2), m["note"]) for m in wavinfo.read_markers(out)],
                         [(1.0, "EVP A: a voice")])
        self.assertEqual(wavinfo.wav_fingerprint(out), p["fp"])               # the same audio: the same marks
        # Export clips: cut from the decoded PCM, in the clip format.
        c = api.export_clips(rec)
        self.assertEqual((c["ok"], c["names"]), (True, ["song_EVP-A_00m01.0s_a voice.mp3"]), c)
        api.set_clip_format("wav")
        c = api.export_clips(rec)
        self.assertEqual(c["names"], ["song_EVP-A_00m01.0s_a voice.wav"])
        with wave.open(os.path.join(c["folder"], c["names"][0])) as w:
            self.assertEqual((w.getframerate(), w.getnframes()), (8000, round(1.3 * 8000)))
        self.assertEqual(api.recording_changed(rec), {"ok": True, "changed": False})
        # The page's marks come back from the library by fingerprint.
        self.assertEqual([m["note"] for m in api.library_marks(fid)["marks"]], ["a voice"])

    def test_mp3_clips_are_clips_like_wav_ones(self):
        self.populate()
        library_ops._make_clips_folder(os.path.join(self.lib, "Other", "Night", "Clips"))
        api = self.new_api()
        r = self.index(api)
        folder = {tuple(d["rel"]): d["id"] for d in r["folders"]}
        name = "song_EVP-B_00m01.0s_clip.mp3"
        fid = next(f["id"] for f in r["files"] if f["name"] == name)
        p = api.play_library(fid)
        self.assertTrue(p["ok"], p)
        self.assertEqual(p["fp"], self.fp(self.clip))
        self.assertTrue(api.add_mark(p["rec"], 0.1, 0.2, "A", "")["ok"])        # markable, as a WAV clip is
        self.assertEqual(api.export_clips(p["rec"])["error"], backend.CLIPS_AGAIN)
        self.assertEqual(api.export_clips_files([fid], 1)["error"], backend.CLIPS_AGAIN)
        # Moved like a WAV clip: to another Clips folder, or out (it is then a recording).
        song = next(f["id"] for f in r["files"] if f["name"] == "song.mp3")
        self.assertEqual(api.move_files([song], folder[("Case", "Clips")])["error"], library_ops.CLIPS_ONLY)
        res = api.move_files([fid], folder[("Other", "Night", "Clips")])
        self.assertTrue(res["ok"], res)
        r = self.index(api)
        self.assertTrue(self.rows(r)[name]["clip"])
        res = api.move_files([self.rows(r)[name]["id"]], folder[("Other",)])
        self.assertTrue(res["ok"], res)
        row = self.rows(self.index(api))[name]
        self.assertFalse(row["clip"])
        self.assertEqual(row["marks"]["A"], 1)                                 # its mark followed it

    def test_export_clips_job_reads_mp3_recordings_never_mp3_clips(self):
        self.populate()
        self.store.add_mark(self.fp(self.song), 0.5, 0.6, "A", "", name="song.mp3", duration=3.0)
        self.store.add_mark(self.fp(self.clip), 0.1, 0.2, "C", "", name="clip", duration=1.0)
        api = self.new_api()
        self.index(api)
        event, p = self.run_job(lambda: api.export_clips_folder("root", 1))
        # song.mp3, and its copy in Other/Clips (a folder the user named Clips), each get theirs.
        self.assertEqual((event, p["recordings"], p["saved"]), ("clips-done", 2, 2), p)
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Case", "Clips"))),
                         [library_ops.CLIPS_MARKER, "song_EVP-A_00m00.5s.mp3", "song_EVP-B_00m01.0s_clip.mp3"])

    def test_open_audio_file(self):
        self.populate()
        api = self.new_api()
        path = os.path.join(self.lib, "Case", "WhatsApp Audio 2026-09-30 at 11.30.05 PM.mpeg")
        api._pick_wav = lambda start: path
        r = api.open_wav()
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["name"], r["rate"], r["fp"]), (os.path.basename(path), 8000, self.fp(self.whatsapp)))
        self.assertTrue(r["url"].endswith(".wav"))
        api._pick_wav = lambda start: os.path.join(self.lib, "Case", "video.mpeg")
        self.assertEqual(api.open_wav()["error"], "video.mpeg is not an MP3 file.")
        api._pick_wav = lambda start: os.path.join(self.lib, "Case", "damaged.mp3")
        self.assertIn("not an MP3 file", api.open_wav()["error"])
        wav = self.write("Case/take.wav", tone_wav(8000, 1, 1.0))
        api._pick_wav = lambda start: wav
        self.assertTrue(api.open_wav()["ok"])
        from app import main
        self.assertEqual(main.AUDIO_FILE_TYPES[0], "Audio files (*.wav;*.mp3;*.mpeg;*.mpga;*.mp2;*.m2a)")
        self.assertIn("All files (*.*)", main.AUDIO_FILE_TYPES)


class Mp3WithoutTheDecoderTests(unittest.TestCase):
    """Without mp3_core.dll: MP3 files are listed but can't be played, with the reason, and
    nothing about them is cached as damaged (they index once the decoder is there)."""
    new_api, write, index = tl.LibraryTests.new_api, tl.LibraryTests.write, tl.LibraryTests.index
    setUp = LibraryMp3RecordingsTests.setUp

    @release_gate.require(mp3.available(), NO_LAMEENC)
    @release_gate.require(mp3dec.available(), NO_CORE)
    def test_listed_unplayable_then_indexed(self):
        song = mp3_recording()
        self.write("Case/song.mp3", song)
        api = self.new_api()
        with mock.patch.object(_mp3core, "_lib", None):
            reason = mp3dec.reason()
            r = self.index(api)
            row = r["files"][0]
            row = {**row, **self.events.rows(r["scan_id"]).get(row["id"], {})}
            self.assertEqual((row["name"], row["fp"]), ("song.mp3", None))
            self.assertIn("mp3_core.dll", row["error"])
            caps = api.capabilities()["formats"]["mp3"]
            self.assertEqual((caps["playable"], caps["reason"]), (False, reason))
            self.assertEqual(api.play_library(row["id"])["error"], f"Playing .mp3 files: {reason}.")
            path = os.path.join(self.lib, "Case", "song.mp3")
            st = os.stat(path)
            self.assertIsNone(self.store.cached_fp(path, st.st_size, st.st_mtime_ns))   # never cached as damaged
        r = self.index(api)
        row = {**r["files"][0], **self.events.rows(r["scan_id"]).get(r["files"][0]["id"], {})}
        self.assertEqual(row["fp"], formats.analyze(formats.MP3, song)[0])
        self.assertTrue(api.play_library(row["id"])["ok"])


if __name__ == "__main__":
    unittest.main()
