"""Regenerate tests/migration/v0.7.2/*.json: the AppData that OpenEVP v0.7.2
wrote, made by v0.7.2's own app/store.py and st25/wavinfo.py (taken from the
v0.7.2 git tag), over synthetic audio from tests/vectors/.

    python tests/migration/make_v0_7_2_fixture.py

The recordings (all synthetic): A-001, a recorder recording marked twice,
reviewed and backed up (.dvf + a marked WAV copy); cell 3.wav, a WAV whose
marks were imported and then added to; A-002, marked, whose backup failed;
and one reviewed without marks. index.json caches the fingerprints of the
library files, including one file that could not be read. The temporary root
folder is written as %ROOT% (tests/test_migration.py puts its own back).
"""
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VECTORS = os.path.join(REPO, "tests", "vectors")
OUT = os.path.join(HERE, "v0.7.2")
MTIME_NS = 1_874_000_000_000_000_000
BROKEN = b"not a recording" * 10


def wav_of(vector):
    """The canonical WAV of a vector's reference decode (what dvf_to_wav gives)."""
    with open(os.path.join(VECTORS, vector + ".pcm"), "rb") as f:
        pcm = f.read()
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
                       1, 1, 8000, 16000, 2, 16, b"data", len(pcm)) + pcm


def v072_modules(tmp):
    for src, dst in (("app/store.py", "store072.py"), ("st25/wavinfo.py", "wavinfo072.py")):
        code = subprocess.run(["git", "-C", REPO, "show", f"v0.7.2:{src}"], check=True,
                              capture_output=True).stdout
        with open(os.path.join(tmp, dst), "wb") as f:
            f.write(code)
    sys.path.insert(0, tmp)
    import store072
    import wavinfo072
    return store072, wavinfo072


def main():
    tmp = tempfile.mkdtemp()
    try:
        store, wavinfo = v072_modules(tmp)
        root = os.path.join(tmp, "ROOTDIR")
        save = os.path.join(root, "OpenEVP")
        os.makedirs(os.path.join(save, "A"))
        os.makedirs(os.path.join(save, "Old Jail"))
        s = store.AppData(os.path.join(root, "AppData"))
        s.set_setting("save_folder", save)
        s.set_setting("library_folder", save)

        def fp_of(wav):
            return wavinfo.wav_fingerprint(io.BytesIO(wav))

        def seconds(wav):
            return (len(wav) - 44) / 2 / 8000

        wav1 = wav_of("tone-440-quiet")
        fp1 = fp_of(wav1)
        s.add_mark(fp1, 0.5, 1.25, "A", "get out", name="A-001", duration=seconds(wav1))
        s.add_mark(fp1, 1.5, 2.0, "B", "", name="A-001", duration=seconds(wav1))
        s.set_reviewed(fp1, True)
        dvf1 = os.path.join(save, "A", "001_A_001_Casey_2029_05_23.dvf")
        wavp1 = os.path.join(save, "A", "001_A_001_Casey_2029_05_23.wav")
        shutil.copyfile(os.path.join(VECTORS, "tone-440-quiet.dvf"), dvf1)
        with open(wavp1, "wb") as f:
            f.write(wavinfo.with_markers(wav1, s.marks(fp1)))
        s.set_backup(fp1, "saved", "Saved as 001_A_001_Casey_2029_05_23.dvf, with a WAV copy "
                     "(001_A_001_Casey_2029_05_23.wav).", [dvf1, wavp1])

        wav2 = wav_of("steps")
        fp2 = fp_of(wav2)
        cell = os.path.join(save, "Old Jail", "cell 3.wav")
        with open(cell, "wb") as f:
            f.write(wav2)
        s.import_marks(fp2, [{"start": 0.25, "end": 0.75, "note": "whisper?"}], "cell 3.wav", seconds(wav2))
        s.add_mark(fp2, 1.0, 1.5, "C", "knock", name="cell 3.wav", duration=seconds(wav2))

        wav3 = wav_of("white-noise")
        fp3 = fp_of(wav3)
        s.add_mark(fp3, 0.1, 0.9, "A", "my name", name="A-002", duration=seconds(wav3))
        s.set_backup(fp3, "failed", "A-002 was not backed up: The recorder was unplugged.")

        s.set_reviewed(fp_of(wav_of("silence")), True)

        broken = os.path.join(save, "Old Jail", "broken.dvf")
        with open(broken, "wb") as f:
            f.write(BROKEN)
        for p in (dvf1, wavp1, cell, broken):
            os.utime(p, ns=(MTIME_NS, MTIME_NS))
        s.remember_fp(dvf1, os.path.getsize(dvf1), MTIME_NS, fp1, round(seconds(wav1), 1))
        s.remember_fp(wavp1, os.path.getsize(wavp1), MTIME_NS, fp1, round(seconds(wav1), 1))
        s.remember_fp(cell, os.path.getsize(cell), MTIME_NS, fp2, round(seconds(wav2), 1))
        s.remember_fp(broken, len(BROKEN), MTIME_NS, None, None,
                      error="broken.dvf is not a Sony ICD-ST25 recording.")
        s.close()

        os.makedirs(OUT, exist_ok=True)
        for name in ("settings.json", "marks.json", "index.json"):
            with open(os.path.join(root, "AppData", name), encoding="utf-8") as f:
                text = f.read()
            for form in (root, os.path.normcase(root)):
                text = text.replace(json.dumps(form)[1:-1], "%ROOT%")
            assert "ROOTDIR" not in text.upper(), name
            with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
