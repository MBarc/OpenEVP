"""EVP clips from ICD-ST10 recordings: cut from the decoded audio in its own
format (LPEC ST: 44.1 kHz stereo; LPEC SP: 16 kHz mono), from the player (a
recorder recording, a library file) and from the library's clips job; and a
recording that cannot be played in this build (its codec's decoder or tables
missing) is skipped and reported, never cached as damaged.

Uses the synthetic LPEC ST frames of test_st10_model / fixtures and the
synthetic LPEC SP vectors in tests/vectors/lpec_sp/ (real decoders: skipped
without the git-ignored tables, a failure in the release gate)."""
import io
import math
import os
import sys
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import release_gate  # noqa: E402
import test_library as tl  # noqa: E402
from test_lpec_sp_vectors import HAVE_TABLES as HAVE_SP, NO_TABLES as NO_SP_TABLES, VECTORS_DIR  # noqa: E402
from test_st10 import NO_ST_TABLES  # noqa: E402
from test_st10_app import HAVE_ST, ID, WAV_1, St10Base, fp_of, st10_dvf  # noqa: E402
from test_st10_model import ST_MISSING, without_st_decoder  # noqa: E402
from test_st10_modes import NO_SP, fake_decoders, sp_dvf  # noqa: E402
from app import backend, library_ops  # noqa: E402
from openevp import clips, formats, wavinfo  # noqa: E402

WAIT = 60
SP_VECTOR = "tone-1000-fullscale"          # 20 LPEC SP frames: 20480 samples, 1.28 s at 16 kHz


def sp_vector():
    return (VECTORS_DIR / f"{SP_VECTOR}.dvf").read_bytes()


def layout(wav):
    """(rate, channels, sample width, frames, frame bytes) of a WAV."""
    with wave.open(io.BytesIO(wav)) as w:
        n = w.getnframes()
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), n, w.readframes(n)


def expected_frames(start, end, rate, frames):
    lo, hi = clips.bounds(start, end, frames / rate)
    first = max(0, math.floor(lo * rate + 1e-9))
    return first, min(frames, math.ceil(hi * rate - 1e-9))


class ClipCheck:
    def check_clip(self, clip, source_wav, start, end, rate, channels):
        """The clip holds exactly the source's frames around the mark, in the source's
        own format (read from the decoded WAV's fmt chunk, nothing resampled)."""
        s_rate, s_channels, s_width, s_frames, s_data = layout(bytes(source_wav))
        self.assertEqual((s_rate, s_channels, s_width), (rate, channels, 2))
        c_rate, c_channels, c_width, c_frames, c_data = layout(clip)
        self.assertEqual((c_rate, c_channels, c_width), (rate, channels, 2))
        first, last = expected_frames(start, end, rate, s_frames)
        self.assertEqual(c_frames, last - first)
        align = channels * 2
        self.assertEqual(c_data, s_data[first * align:last * align])
        (m,) = wavinfo.read_markers(io.BytesIO(clip))
        self.assertEqual(m["note"].split(":")[0], "EVP A")


class St10LibraryClipsTests(ClipCheck, unittest.TestCase):
    setUp = tl.LibraryTests.setUp
    new_api, write, index, by_name = (tl.LibraryTests.new_api, tl.LibraryTests.write, tl.LibraryTests.index,
                                      tl.LibraryTests.by_name)

    def run_job(self, start):
        r = start()
        self.assertTrue(r["ok"], r)
        with self.events.cond:
            ok = self.events.cond.wait_for(
                lambda: any(n in ("clips-done", "clips-failed") and p["job"] == r["job"] for n, p in self.events.items),
                WAIT)
        self.assertTrue(ok, self.events.items)
        return next((n, p) for n, p in self.events.items if n in ("clips-done", "clips-failed") and p["job"] == r["job"])

    def clip_of(self, folder, name):
        with open(os.path.join(self.lib, folder, backend.CLIPS, name), "rb") as f:
            return f.read()

    @release_gate.require(HAVE_ST, NO_ST_TABLES)
    def test_lpec_st_recording_gives_44k_stereo_clips(self):
        path = self.write("Old Mill/A/001_A_001_Unknown.dvf", st10_dvf())
        self.store.add_mark(fp_of(WAV_1), 0.6, 0.7, "A", "st", name="x", duration=1.7)
        self.store.add_mark(fp_of(WAV_1), 1.6, 1.7, "A", "", name="x", duration=1.7)      # clamped at the end
        api = self.new_api()
        listing = self.index(api)
        event, p = self.run_job(lambda: api.export_clips_files([f["id"] for f in listing["files"]], 1))
        self.assertEqual((event, p["saved"], p["recordings"], p["skipped"], p["notes"]), ("clips-done", 2, 1, [], []))
        self.check_clip(self.clip_of("Old Mill", "001_A_001_Unknown_EVP-A_00m00.6s_st.wav"), WAV_1, 0.6, 0.7, 44100, 2)
        self.check_clip(self.clip_of("Old Mill", "001_A_001_Unknown_EVP-A_00m01.6s.wav"), WAV_1, 1.6, 1.7, 44100, 2)
        self.assertEqual(self.store.cached_fp(path, *tl_stat(path))["fp"], fp_of(WAV_1))
        # The player: the library file loaded, its clips cut from a fresh decode (as export_marked does).
        f = self.by_name(self.index(api))["001_A_001_Unknown.dvf"]
        r = api.play_library(f["id"])
        self.assertTrue(r["ok"], r)
        e = api.export_clips(r["rec"])
        self.assertEqual((e["ok"], e["saved"], e["already"]), (True, 0, 2))           # the same bytes: already there
        self.assertFalse(api._busy.locked())
        # The clips are listed (in OpenEVP's Clips folder) and play, but their markers are
        # never imported, and a folder job never cuts clips from them.
        self.index(api)
        rows = self.by_name(api.list_library())
        clip_names = sorted(n for n, f in rows.items() if f["clip"])
        self.assertEqual(clip_names, ["001_A_001_Unknown_EVP-A_00m00.6s_st.wav", "001_A_001_Unknown_EVP-A_00m01.6s.wav"])
        self.assertFalse(rows["001_A_001_Unknown.dvf"]["clip"])
        for name in clip_names:
            played = api.play_library(rows[name]["id"])
            self.assertEqual((played["ok"], played["imported"]), (True, 0), name)
            self.assertIsNone(self.store.recording(wavinfo.wav_fingerprint(io.BytesIO(self.clip_of("Old Mill", name)))))
            self.assertEqual(rows[name]["marks"], {"A": 0, "B": 0, "C": 0})
        event, p = self.run_job(lambda: api.export_clips_folder("root", 2))
        self.assertEqual((event, p["saved"], p["already"], p["recordings"]), ("clips-done", 0, 2, 1))
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Old Mill", backend.CLIPS))),
                         [library_ops.CLIPS_MARKER] + clip_names)

    @release_gate.require(HAVE_SP, NO_SP_TABLES)
    def test_lpec_sp_recording_gives_16k_mono_clips(self):
        data = sp_vector()
        self.assertEqual(data[61], 0x2A)                                                 # an ICD-ST10 SP .dvf
        wav = formats.DVF.decoder.to_wav(data)
        self.write("Case/sp.dvf", data)
        with wavinfo.buffer_file(wav) as f:
            fp = wavinfo.wav_fingerprint(f)
        self.store.add_mark(fp, 0.6, 0.7, "A", "sp", name="x", duration=1.28)
        api = self.new_api()
        self.index(api)
        event, p = self.run_job(lambda: api.export_clips_folder("root", 2))
        self.assertEqual((event, p["saved"], p["skipped"]), ("clips-done", 1, []))
        self.check_clip(self.clip_of("Case", "sp_EVP-A_00m00.6s_sp.wav"), wav, 0.6, 0.7, 16000, 1)

    def test_unplayable_recordings_are_skipped_counted_and_never_cached(self):
        st_path = self.write("Case/st.dvf", st10_dvf())
        sp_path = self.write("Case/sp.dvf", sp_dvf())
        good = tl.wav_bytes(b"good", 2.0)
        self.write("Case/good.wav", good)
        self.store.add_mark(fp_of(WAV_1), 0.6, 0.7, "A", "", name="x", duration=1.7)   # marked earlier
        self.store.add_mark(fp_of(good), 0.5, 0.6, "A", "", name="x", duration=2.0)
        mods, _calls = fake_decoders(sp_tables=False)
        with without_st_decoder(), mock.patch.dict(sys.modules, {"openevp.decoders.sony_lpec":
                                                                 mods["openevp.decoders.sony_lpec"]}):
            api = self.new_api()
            self.index(api)
            event, p = self.run_job(lambda: api.export_clips_folder("root", 3))
        self.assertEqual((event, p["saved"], p["recordings"]), ("clips-done", 1, 1))
        self.assertEqual(sorted(p["skipped"]), [f"sp.dvf (can't be played: {NO_SP})",
                                                f"st.dvf (can't be played: {ST_MISSING})"])
        for path in (st_path, sp_path):
            self.assertIsNone(self.store.cached_fp(path, *tl_stat(path)), path)
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Case", backend.CLIPS))),
                         [library_ops.CLIPS_MARKER, "good_EVP-A_00m00.5s.wav"])
        self.assertFalse(api._busy.locked())

    def test_decoder_unavailable_during_the_decode_is_skipped_not_damaged(self):
        # The header said nothing against it (or changed after it was read); the decoder then refuses.
        path = self.write("st.dvf", st10_dvf())
        self.store.add_mark(fp_of(WAV_1), 0.6, 0.7, "A", "", name="x", duration=1.7)
        with mock.patch.object(formats.Format, "file_problem", return_value=None),                 mock.patch.object(formats.Format, "data_problem", return_value=None),                 mock.patch.object(type(formats.DVF.decoder), "to_wav",
                                  side_effect=formats.DecoderUnavailable(ST_MISSING)):
            api = self.new_api()
            self.index(api)
            event, p = self.run_job(lambda: api.export_clips_folder("root", 4))
        self.assertEqual((event, p["skipped"]), ("clips-done", [f"st.dvf (can't be played: {ST_MISSING})"]))
        cached = self.store.cached_fp(path, *tl_stat(path))              # the indexer's, untouched by the job
        self.assertEqual((cached["fp"], cached.get("error")), (fp_of(WAV_1), None))

def tl_stat(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


@release_gate.require(HAVE_ST, NO_ST_TABLES)
class St10PlayerClipsTests(ClipCheck, St10Base):
    """A recorder recording of an ICD-ST10, played and marked: Export clips cuts
    44.1 kHz stereo clips into <Save to>\\A\\Clips."""

    def test_export_clips_from_the_recorder(self):
        self.api.recordings(ID)
        r = self.api.audio(ID, "A", 1)
        self.assertTrue(r["ok"], r)
        self.api.add_mark(r["rec"], 0.6, 0.7, "A", "st10")
        self.events.wait("backup-done", "backup-failed")
        e = self.api.export_clips(r["rec"])
        self.assertEqual((e["ok"], e["saved"], e["notes"]), (True, 1, []), e)
        self.assertEqual(e["folder"], os.path.join(self.dest, "A", backend.CLIPS))
        self.assertEqual(e["names"], ["001_A_001_Unknown_EVP-A_00m00.6s_st10.wav"])
        with open(os.path.join(e["folder"], e["names"][0]), "rb") as f:
            self.check_clip(f.read(), WAV_1, 0.6, 0.7, 44100, 2)
        self.assertFalse(self.api._busy.locked())


if __name__ == "__main__":
    unittest.main()
