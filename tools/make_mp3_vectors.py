"""Make the MP3 decoder's test vectors (tests/vectors/mp3/): small synthetic files.

    python tools/make_mp3_vectors.py [--ffmpeg ffmpeg]

  tone-8k-mono.mp3, tone-16k-mono.mp3, tone-44k-stereo.mp3   lameenc (as OpenEVP's clips)
  tone-32k-stereo.mp2    MPEG-1 layer II (ffmpeg's mp2 encoder)
  vbr-xing-22k.mp3       VBR layer III with a Xing tag frame (ffmpeg's libmp3lame)
  video.mpeg             an MPEG-PS video (MPEG-1 video + MP2 audio): never a recording

Each tone is 1 s of 440 Hz (left) and 660 Hz (right) at half scale. Then the
decoded PCM's SHA-256 of each audio file is written to vectors.json with the
current mp3_core.dll (tests/test_mp3_decoder.py checks them). The files are
kept as committed; run this again only to replace them.
"""
import argparse
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "vectors", "mp3")
sys.path.insert(0, ROOT)


def tone(rate, channels, seconds=1.0):
    """16-bit PCM: 440 Hz left, 660 Hz right, at half scale."""
    n = int(rate * seconds)
    return b"".join(struct.pack("<h", int(0.5 * 32767 * math.sin(2 * math.pi * (440 + 220 * c) * i / rate)))
                    for i in range(n) for c in range(channels))


def wav(rate, channels, pcm):
    align = 2 * channels
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * align, align, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def lame(rate, channels, kbps):
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(2)
    return bytes(enc.encode(tone(rate, channels))) + bytes(enc.flush())


def ffmpeg(exe, args, src_wav=None):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-bitexact"]
        if src_wav is not None:
            path = os.path.join(tmp, "in.wav")
            with open(path, "wb") as f:
                f.write(src_wav)
            cmd += ["-i", path]
        subprocess.run(cmd + args + [out], check=True)
        with open(out, "rb") as f:
            return f.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    files = {
        "tone-8k-mono.mp3": lame(8000, 1, 64),
        "tone-16k-mono.mp3": lame(16000, 1, 128),
        "tone-44k-stereo.mp3": lame(44100, 2, 128),
        "tone-32k-stereo.mp2": ffmpeg(args.ffmpeg, ["-c:a", "mp2", "-b:a", "128k", "-f", "mp2"],
                                      wav(32000, 2, tone(32000, 2))),
        "vbr-xing-22k.mp3": ffmpeg(args.ffmpeg, ["-c:a", "libmp3lame", "-q:a", "6", "-write_xing", "1",
                                                 "-id3v2_version", "0", "-f", "mp3"],
                                   wav(22050, 1, tone(22050, 1))),
        "video.mpeg": ffmpeg(args.ffmpeg, ["-f", "lavfi", "-i", "testsrc=size=64x48:rate=25:duration=0.2",
                                           "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
                                           "-c:v", "mpeg1video", "-c:a", "mp2", "-b:a", "64k", "-f", "mpeg"]),
    }
    from openevp.decoders import mp3
    vectors = {}
    for name, data in files.items():
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(data)
        if name.endswith(".mpeg"):
            continue
        channels, _width, rate, chunks = mp3.stream(data)
        pcm = b"".join(chunks)
        vectors[name] = {"rate": rate, "channels": channels, "frames": len(pcm) // (2 * channels),
                         "pcm_sha256": hashlib.sha256(pcm).hexdigest()}
        print(f"{name}: {len(data)} bytes, {rate} Hz, {channels} ch, {len(pcm) // (2 * channels)} frames")
    with open(os.path.join(OUT, "vectors.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(vectors, f, indent=1, sort_keys=True)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
