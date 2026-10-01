"""MP3 clips (openevp.mp3, openevp.clips.make): encoding at the recording's own
rate, the ID3v2.3 tag, determinism (a second export finds the clip already
saved), the clip format setting (Api.clip_format / set_clip_format, MP3 by
default) and what happens without lameenc; and MP3 clips in the EVP Library:
listed and played (served as they are) in the Clips folders OpenEVP made only,
never fingerprinted, indexed, counted or marked.

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
from openevp import clips, mp3, wavinfo  # noqa: E402

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


class Mp3InfoTests(unittest.TestCase):
    """mp3.info(): the format and length from the frame headers (no lameenc needed to read)."""

    @release_gate.require(mp3.available(), NO_LAMEENC)
    def test_rate_channels_and_length(self):
        for rate, channels in ((8000, 1), (16000, 1), (44100, 2)):
            data = mp3_clip(rate=rate, channels=channels)
            got_rate, got_channels, seconds = mp3.info(data)
            frames = mpeg_frames(data[read_id3(data)[1]:])
            self.assertEqual((got_rate, got_channels), (rate, channels))
            self.assertAlmostEqual(seconds, sum(f[3] for f in frames) / rate)
            self.assertTrue(1.25 <= seconds < 1.5, seconds)
        data = mp3_clip()
        self.assertEqual(mp3.info(data[read_id3(data)[1]:]), mp3.info(data))          # no tag
        self.assertEqual(mp3.info(b"junk" + data[read_id3(data)[1]:])[:2], (8000, 1))  # junk before the frames
        self.assertLess(mp3.info(data[:-100])[2], mp3.info(data)[2])                   # a truncated last frame

    def test_not_an_mp3(self):
        for data in (b"", b"RIFF....WAVEfmt ", b"ID3\x03\x00\x00\x00\x00\x00\x00", bytes(5000)):
            with self.assertRaises(ValueError):
                mp3.info(data)


class AudioServerMp3Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.s = AudioServer(None, self.dir.name)
        self.s.start()
        self.addCleanup(self.s.stop)

    def get(self, url, headers=None):
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5)

    def http_error(self, url):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get(url)
        e.exception.close()
        return e.exception.code

    @release_gate.require(mp3.available(), NO_LAMEENC)
    def test_served_as_it_is_with_ranges(self):
        data = mp3_clip(rate=44100, channels=2)
        folder = os.path.join(self.dir.name, "clips")
        os.makedirs(folder)
        path = os.path.join(folder, "x_EVP-B_00m01.0s.mp3")
        with open(path, "wb") as f:
            f.write(data)
        info = self.s.prepare_mp3(path)
        self.assertTrue(info["url"].endswith(".mp3"))
        self.assertEqual((info["peaks"], info["fp"], info["compressed"], info["rate"], info["channels"]),
                         ([], None, True, 44100, 2))
        self.assertAlmostEqual(info["duration"], mp3.info(data)[2])
        self.assertEqual(info["stat"], (os.stat(path).st_size, os.stat(path).st_mtime_ns))
        with self.get(info["url"]) as r:
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, "audio/mpeg"))
            self.assertEqual(r.read(), data)
        with self.get(info["url"], {"Range": "bytes=10-19"}) as r:
            self.assertEqual((r.status, r.read()), (206, data[10:20]))
        self.assertEqual(self.http_error(info["url"][:-4] + ".wav"), 404)    # the same id as a .wav: not found
        self.assertEqual(self.s.prepare_mp3(path)["url"], info["url"])     # registered once per version
        self.assertEqual(sorted(os.listdir(self.dir.name)), ["clips"])       # served in place, nothing cached
        with open(path, "ab") as f:                                         # changed on disk: refused
            f.write(b"x")
        self.assertEqual(self.http_error(info["url"]), 409)

    def test_not_an_mp3_is_refused(self):
        path = os.path.join(self.dir.name, "fake.mp3")
        with open(path, "wb") as f:
            f.write(tone_wav(8000, 1, 0.5))
        with self.assertRaises(ValueError):
            self.s.prepare_mp3(path)


@release_gate.require(mp3.available(), NO_LAMEENC)
class LibraryMp3ClipsTests(unittest.TestCase):
    """MP3 clips in the EVP Library, with the fixtures of test_library and a real audio server."""
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
        self.rec = tm.wav_bytes(b"rec", seconds=2.0)
        self.write("Case/rec.wav", self.rec)
        library_ops._make_clips_folder(os.path.join(self.lib, "Case", "Clips"))
        self.clip = mp3_clip(note="hello")
        self.write("Case/Clips/rec_EVP-B_00m01.0s_hello.mp3", self.clip)
        self.write("Case/Clips/rec_EVP-A_00m00.5s.wav", clips.cut(self.rec, mark(0.5, 0.6)))
        self.write("Case/Clips/Deeper/old.mp3", mp3_clip(note="deeper"))         # any depth inside
        self.write("Case/Clips/broken.mp3", b"not an mp3 at all")
        self.write("Case/song.mp3", self.clip)                                     # not in a Clips folder
        self.write("Other/Clips/mine.mp3", self.clip)                              # the user's own "Clips" folder

    def test_listed_as_clips_only_in_openevps_clips_folders(self):
        self.populate()
        api = self.new_api()
        indexed = []
        real = api._index_file

        def spy(path, *a, **k):
            indexed.append(os.path.basename(path))
            return real(path, *a, **k)
        with mock.patch.object(api, "_index_file", spy):
            r = self.index(api)
        rows = {f["name"]: f for f in r["files"]}
        self.assertEqual(sorted(rows), ["broken.mp3", "old.mp3", "rec.wav", "rec_EVP-A_00m00.5s.wav",
                                        "rec_EVP-B_00m01.0s_hello.mp3"])          # song.mp3, mine.mp3: never
        row = rows["rec_EVP-B_00m01.0s_hello.mp3"]
        self.assertEqual((row["type"], row["clip"], row["fp"], row["error"], row["unplayable"]),
                         ("mp3", True, None, None, None))
        self.assertAlmostEqual(row["seconds"], round(mp3.info(self.clip)[2], 1))
        self.assertTrue(rows["old.mp3"]["clip"])
        self.assertIn("broken.mp3 can't be played: not an MP3 file", rows["broken.mp3"]["error"])
        self.assertFalse(any(n.endswith(".mp3") for n in indexed), indexed)        # never fingerprinted
        for path in (os.path.join(self.lib, "Case", "Clips", "rec_EVP-B_00m01.0s_hello.mp3"),
                     os.path.join(self.lib, "Case", "song.mp3")):
            st = os.stat(path)
            self.assertIsNone(self.store.cached_fp(path, st.st_size, st.st_mtime_ns))
        self.assertEqual(set(self.store.summary()), set())                         # nothing marked, nothing counted
        self.assertEqual(api.capabilities()["formats"]["mp3"], {"label": "MP3 clip", "playable": True, "reason": None})
        # Delete's folder info counts them as clips; an MP3 elsewhere is an other file.
        info = api.folder_info(next(d["id"] for d in r["folders"] if d["rel"] == ["Case"]))
        self.assertEqual((info["recordings"], info["clips"]), (1, 4))
        self.assertGreaterEqual(info["other_files"], 1)                            # song.mp3 (and the marker)
        other = api.folder_info(next(d["id"] for d in r["folders"] if d["rel"] == ["Other"]))
        self.assertEqual((other["recordings"], other["clips"], other["other_files"]), (0, 0, 1))

    def test_played_as_it_is_and_never_marked(self):
        self.populate()
        api = self.new_api()
        r = self.index(api)
        fid = next(f["id"] for f in r["files"] if f["name"] == "rec_EVP-B_00m01.0s_hello.mp3")
        p = api.play_library(fid)
        self.assertTrue(p["ok"], p)
        self.assertTrue(p["url"].endswith(".mp3"))
        self.assertEqual((p["compressed"], p["fp"], p["rate"], p["channels"], p["marks"], p["imported"]),
                         (True, None, 8000, 1, [], 0))
        self.assertEqual((p["markable"], p["mark_reason"]), (False, library_ops.MP3_NO_MARKS))
        with urllib.request.urlopen(p["url"], timeout=5) as resp:
            self.assertEqual((resp.headers["Content-Type"], resp.read()), ("audio/mpeg", self.clip))
        rec = p["rec"]
        self.assertEqual(api.add_mark(rec, 0.1, 0.2, "A", "")["error"], library_ops.MP3_NO_MARKS)
        self.assertEqual(api.set_reviewed(rec, True)["error"], library_ops.MP3_NO_MARKS)
        self.assertEqual(api.export_marked(rec)["error"], library_ops.MP3_NO_MARKS)
        self.assertEqual(api.export_clips(rec)["error"], library_ops.MP3_NO_MARKS)
        self.assertEqual(api.recording_changed(rec), {"ok": True, "changed": False})
        self.assertEqual(self.store.summary(), {})
        self.assertEqual(api.library_marks(fid), {"ok": True, "marks": []})
        bad = next(f["id"] for f in r["files"] if f["name"] == "broken.mp3")
        self.assertIn("not an MP3 file", api.play_library(bad)["error"])
        # An MP3 outside a Clips folder never plays (it is never listed; checked here directly).
        song = os.path.join(self.lib, "Case", "song.mp3")
        self.assertIn("only as EVP clips", api._play_file(song, root=self.lib)["error"])

    def test_an_mp3_clip_moves_only_between_clips_folders(self):
        self.populate()
        library_ops._make_clips_folder(os.path.join(self.lib, "Other", "Night", "Clips"))
        api = self.new_api()
        r = self.index(api)
        folder = {tuple(d["rel"]): d["id"] for d in r["folders"]}
        fid = next(f["id"] for f in r["files"] if f["name"] == "rec_EVP-B_00m01.0s_hello.mp3")
        wav = next(f["id"] for f in r["files"] if f["name"] == "rec_EVP-A_00m00.5s.wav")
        for target in (("Case",), ("Other",), ("Other", "Clips")):              # (a Clips folder the user named)
            self.assertEqual(api.move_files([fid], folder[target])["error"], library_ops.MP3_STAYS)
        self.assertEqual(api.move_files([wav, fid], folder[("Case",)])["error"], library_ops.MP3_STAYS)
        res = api.move_files([fid], folder[("Other", "Night", "Clips")])        # another Clips folder of OpenEVP's
        self.assertTrue(res["ok"], res)
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Other", "Night", "Clips", "rec_EVP-B_00m01.0s_hello.mp3")))
        r = self.index(api)
        self.assertTrue(next(f for f in r["files"] if f["name"] == "rec_EVP-B_00m01.0s_hello.mp3")["clip"])

    def test_export_clips_never_reads_mp3_clips(self):
        self.populate()
        self.store.add_mark(wavinfo.wav_fingerprint(io.BytesIO(self.rec)), 0.5, 0.6, "A", "", name="rec.wav",
                            duration=2.0)
        api = self.new_api()
        r = self.index(api)
        event, p = self.run_job(lambda: api.export_clips_folder("root", 1))
        self.assertEqual((event, p["recordings"], p["saved"], p["skipped"]), ("clips-done", 1, 1, []))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Case", "Clips", "rec_EVP-A_00m00.5s.mp3")))
        clip_ids = [f["id"] for f in r["files"] if f["name"].endswith(".mp3")]
        self.assertEqual(api.export_clips_files(clip_ids, 2)["error"], backend.CLIPS_AGAIN)


if __name__ == "__main__":
    unittest.main()
