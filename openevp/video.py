"""Video import using the bundled FFmpeg. Originals are never modified.

The first audio track is decoded to PCM; a silent H.264 preview shares its
zero-based container timeline. Work is disk-backed and cancellable.
"""
import io
import os
import shutil
import subprocess
import tempfile
import wave

EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".wmv", ".mpg", ".mts", ".m2ts")


def executable():
    from imageio_ffmpeg import get_ffmpeg_exe
    return get_ffmpeg_exe()


def run(path, options, should_stop=None):
    from openevp.formats import Cancelled, DecodeError, DecoderUnavailable
    if should_stop and should_stop():
        raise Cancelled("stopped")
    try:
        exe = executable()
    except (ImportError, RuntimeError) as e:
        raise DecoderUnavailable("Video support requires imageio-ffmpeg; install requirements-app.txt") from e
    # No shell or stdin; file-only input protocols prevent a playlist from
    # making network requests. Diagnostics stay on disk rather than in RAM.
    with tempfile.TemporaryFile() as errors:
        cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-protocol_whitelist", "file,pipe", "-copyts", "-start_at_zero",
               "-i", os.fspath(path), *options]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=errors, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while True:
                try:
                    code = proc.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if should_stop and should_stop():
                        raise Cancelled("stopped")
            if code:
                raise DecodeError("Video could not be read. It must contain a readable video track and audio track.")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def preview(path, target, should_stop=None):
    """A browser-compatible, silent preview, bounded to 720p (no upscaling)."""
    run(path, ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf",
               "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", "-f", "mp4", target], should_stop)


class Decoder:
    def available(self):
        return self.reason() is None

    def reason(self):
        try:
            executable()
        except (ImportError, RuntimeError):
            return "Video support requires imageio-ffmpeg; install requirements-app.txt"
        return None

    def warning(self):
        return None

    def write_wav(self, data, f, should_stop=None):
        # Normal app paths pass a filename, so even multi-GB videos aren't read
        # into Python memory. Bytes remain supported by the format contract.
        with tempfile.TemporaryDirectory(prefix="openevp-video-") as folder:
            if isinstance(data, (str, os.PathLike)):
                path = data
            else:
                path = os.path.join(folder, "input")
                with open(path, "wb") as source:
                    source.write(data)
            target = os.path.join(folder, "audio.wav")
            run(path, ["-map", "0:a:0", "-vn", "-sn", "-dn", "-af", "aresample=async=1:first_pts=0",
                       "-c:a", "pcm_s16le", "-f", "wav", target], should_stop)
            with open(target, "rb") as source:
                shutil.copyfileobj(source, f, 1 << 20)
            return os.path.getsize(target)

    def to_wav(self, data, should_stop=None):
        out = io.BytesIO()
        self.write_wav(data, out, should_stop)
        return out.getvalue()

    def pcm(self, data, should_stop=None):
        # The fingerprint path stays bounded in memory too.
        from openevp.formats import Cancelled
        temp = tempfile.TemporaryFile()
        try:
            self.write_wav(data, temp, should_stop)
            temp.seek(0)
            wav = wave.open(temp, "rb")
        except BaseException:
            temp.close()
            raise

        def chunks():
            try:
                while True:
                    if should_stop and should_stop():
                        raise Cancelled("stopped")
                    chunk = wav.readframes(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                wav.close()
                temp.close()
        return wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), chunks()
