"""Recording file formats, by extension: what the library, playback, indexing
and exports need to know about a file, independent of any connected recorder.

A Format describes one native file type:

- ``ext``: lowercase extension with its dot (".dvf"); the registry key.
- ``label``: what the export menu calls it ("Sony original").
- ``same(existing_bytes, new_bytes) -> bool``: whether a file already on
  disk holds the same recording, for never-overwrite saving. Format-specific:
  a .dvf compares its audio with the block time counters removed (Digital
  Voice Editor rewrites them); a WAV compares bytes.
- ``seconds(path) -> float | None``: the length from the file's header only
  (never decodes); None when unknown.
- ``max_bytes``: files larger than this are not treated as recordings of this
  format (None: no limit).
- ``noun``: one file of the format in a sentence ("a Sony ICD-ST
  recording"), e.g. in "x.dvf is too large to be a Sony ICD-ST recording.";
  "" means "a <label> recording".
- ``header_problem(header) -> str | None``: optional. Why this particular
  file cannot be decoded (yet), judged from its first HEADER_BYTES bytes
  only, or None. Not the file's fault, like DecoderUnavailable: such a file
  is listed and saved but not played, marked or fingerprinted, and nothing
  about it is cached. file_problem(path) and data_problem(data) apply it.
- ``decoder``: None when OpenEVP cannot turn the format into audio (such
  files are still listed, saved and backed up, but not played, marked or
  fingerprinted). Otherwise an object with:

  - ``available() -> bool``: whether to_wav can run now.
  - ``reason() -> str | None``: why it cannot (None when available).
  - ``warning() -> str | None``: shown while available, e.g. slow mode.
  - ``to_wav(data, should_stop=None) -> bytes``: a PCM WAV. ``should_stop``
    is polled during the decode. Raises Cancelled when it returned true,
    DecoderUnavailable when available() is False (not the file's fault:
    never cache it as a damaged file), DecodeError when the data is not a
    valid recording. MemoryError and OSError pass through unchanged.
  - optional ``write_wav(data, f, should_stop=None) -> int``: to_wav written
    into a seekable binary file as it decodes (a long recording is never
    held in memory); the same exceptions. Returns the WAV's size.
  - optional ``pcm(data, should_stop=None)``: (channels, sample width, rate,
    PCM chunks) decoded as the chunks are consumed, or None when this data
    cannot be streamed; for fingerprinting without a WAV in memory.

decoder_problem(fmt) is why a format cannot be decoded now at all.
write_wav(fmt, ...) and analyze(fmt, ...) use a decoder's streamed forms
when it has them and fall back to to_wav.

The registry holds .wav (built in, a PCM passthrough) and .dvf (Sony ICD-ST25
and ICD-ST10). A .dvf is decoded by its codec byte, through st25.audio:
LPEC LP (ICD-ST25) by the Sony LPEC decoder, LPEC ST (ICD-ST10) by the
Sony LPEC ST decoder. The format-level availability is the LP decoder's;
header_problem says when an LPEC ST file's own decoder is unavailable (then
to_wav raises DecoderUnavailable). Tests add their own formats with
register()/unregister().
"""
import io
import re
import struct
import wave
from dataclasses import dataclass
from typing import Callable, Optional

from openevp import wavinfo as _wavinfo
from st25 import audio as _st25_audio
from st25 import dvf as _dvf


class Cancelled(Exception):
    """A decode (or another cancellable operation) was stopped by its caller."""


class DecodeError(Exception):
    """The data is not a valid recording of the format: the file's fault."""


class DecoderUnavailable(Exception):
    """The decoder cannot run in this build or on this PC: not the file's fault."""


_EXT = re.compile(r"\.[a-z0-9_-]+")
HEADER_BYTES = 512                  # what header_problem() is given of a file


@dataclass(frozen=True, eq=False)
class Format:
    ext: str
    label: str
    same: Callable[[bytes, bytes], bool]
    seconds: Callable[[str], Optional[float]]
    decoder: object = None
    max_bytes: Optional[int] = None
    noun: str = ""
    header_problem: Optional[Callable[[bytes], Optional[str]]] = None

    def __post_init__(self):
        if not isinstance(self.ext, str) or not _EXT.fullmatch(self.ext):
            raise ValueError(f"a format extension is a lowercase '.name', not {self.ext!r}")

    def a_recording(self):
        """The noun for one file of this format ("a Sony ICD-ST recording")."""
        return self.noun or f"a {self.label} recording"

    def data_problem(self, data):
        """Why this file's bytes (its header is enough) cannot be decoded, or None."""
        if self.header_problem is None:
            return None
        return self.header_problem(bytes(data[:HEADER_BYTES]))

    def file_problem(self, path):
        """data_problem() for a file on disk, reading only its header. None when
        the header says nothing against it, or it cannot be read (the decode
        reports that as it always has)."""
        if self.header_problem is None:
            return None
        try:
            with open(path, "rb") as f:
                header = f.read(HEADER_BYTES)
        except OSError:
            return None
        return self.header_problem(header)


def decoder_problem(fmt):
    """Why fmt cannot be decoded now (a phrase), or None when it can."""
    if fmt.decoder is None:
        return f"OpenEVP cannot convert {fmt.label} ({fmt.ext}) files to WAV"
    if not fmt.decoder.available():
        return fmt.decoder.reason() or "the WAV decoder is not available"
    return None


# ---- WAV (built in) -----------------------------------------------------------
class _PcmPassthrough:
    """A WAV "decodes" to itself, once the wave module accepts it as PCM."""

    def available(self):
        return True

    def reason(self):
        return None

    def warning(self):
        return None

    def to_wav(self, data, should_stop=None):
        if should_stop is not None and should_stop():
            raise Cancelled("stopped")
        try:
            with wave.open(io.BytesIO(data)) as w:
                w.getparams()
        except (wave.Error, EOFError) as e:
            raise DecodeError(f"not a PCM WAV file: {e}") from e
        return data


def _wav_seconds(path):
    try:
        with wave.open(path) as w:
            return round(w.getnframes() / w.getframerate(), 1) if w.getframerate() else None
    except (OSError, EOFError, wave.Error):
        return None


WAV = Format(ext=".wav", label="WAV", decoder=_PcmPassthrough(),
             same=lambda existing, new: existing == new, seconds=_wav_seconds)


# ---- Sony ICD-ST .dvf ---------------------------------------------------------
LP_BYTES_PER_SECOND = _dvf.LP_BYTES_PER_SECOND     # ST25 LP audio
CODEC_LP, CODEC_ST = _dvf.CODEC_LP, _dvf.CODEC_ST


def codec_problem(codec):
    """Why .dvf recordings of ``codec`` (CODEC_LP or CODEC_ST) cannot be
    decoded now (a phrase, e.g. the LPEC ST tables are missing), or None."""
    return None if _st25_audio.available(codec) else _st25_audio.status(codec)


def _dvf_problem(header):
    """A .dvf's header_problem: an ICD-ST10 recording (LPEC ST) whose decoder
    cannot run now. (An LP recording's problem is the format's own.)"""
    return codec_problem(CODEC_ST) if _dvf.codec(header) == CODEC_ST else None


class _SonyLpec:
    """st25.audio (the Sony LPEC and LPEC ST decoders) behind the Format
    decoder contract. The format-level availability is the LP decoder's; an
    LPEC ST file whose decoder is unavailable raises DecoderUnavailable (not
    the file's fault)."""

    def available(self):
        return _st25_audio.available()

    def reason(self):
        return None if _st25_audio.available() else _st25_audio.status()

    def warning(self):
        return _st25_audio.status() if _st25_audio.available() else None

    def _run(self, data, call):
        problem = _dvf_problem(data[:HEADER_BYTES])
        if problem:
            raise DecoderUnavailable(problem)
        return _translated(call)

    def to_wav(self, data, should_stop=None):
        return self._run(data, lambda: _st25_audio.dvf_to_wav(data, should_stop=should_stop))

    def write_wav(self, data, f, should_stop=None):
        return self._run(data, lambda: _st25_audio.dvf_write_wav(data, f, should_stop=should_stop))

    def pcm(self, data, should_stop=None):
        stream = self._run(data, lambda: _st25_audio.dvf_pcm(data, should_stop=should_stop))
        if stream is None:
            return None
        channels, width, rate, chunks = stream

        def translated():
            it = iter(chunks)
            while True:
                chunk = _translated(lambda: next(it, None))
                if chunk is None:
                    return
                yield chunk
        return channels, width, rate, translated()


def _translated(call):
    """call(), with st25.audio's and the decoder's exceptions in this module's terms."""
    try:
        return call()
    except _st25_audio.Cancelled as e:
        raise Cancelled(str(e)) from e
    except _st25_audio.DecoderUnavailable as e:
        raise DecoderUnavailable(str(e)) from e
    except (MemoryError, OSError):     # not the file's fault; OSError may name a path
        raise
    except Exception as e:
        raise DecodeError(str(e) or type(e).__name__) from e


def _dvf_seconds(path):
    try:
        with open(path, "rb") as f:
            header = f.read(468)
    except OSError:
        return None
    if len(header) < 468:
        return None
    mode = _dvf.MODE_ST if _dvf.mode_of(header) == _dvf.MODE_ST else _dvf.MODE_LP
    return round(_dvf.seconds(struct.unpack(">I", header[464:468])[0], mode), 1)


DVF = Format(ext=".dvf", label="Sony original", decoder=_SonyLpec(), same=_dvf.same_audio,
             seconds=_dvf_seconds,
             max_bytes=512 << 20,       # far beyond any ICD-ST recording (~200 hours of LP audio)
             noun="a Sony ICD-ST recording", header_problem=_dvf_problem)


# ---- the registry -------------------------------------------------------------
_registry = {WAV.ext: WAV, DVF.ext: DVF}


def by_ext(ext):
    """The Format for an extension such as ".dvf" (any case), or None."""
    return _registry.get(ext.lower()) if isinstance(ext, str) else None


def all():  # noqa: A001 - the registry's natural name; the builtin is not used here
    """Every registered Format, built-ins first."""
    return list(_registry.values())


def register(fmt):
    """Add a Format (tests, until a model ships its own). Returns it."""
    if fmt.ext in _registry:
        raise ValueError(f"a format for {fmt.ext} is already registered")
    _registry[fmt.ext] = fmt
    return fmt


def unregister(ext):
    _registry.pop(ext, None)


# ---- decoding without holding a whole WAV -------------------------------------
def _decoder_of(fmt):
    if fmt.decoder is None:
        raise DecoderUnavailable(f"OpenEVP cannot convert {fmt.label} ({fmt.ext}) files to WAV")
    return fmt.decoder


def write_wav(fmt, data, f, should_stop=None):
    """Decode ``data`` (a file of ``fmt``) into ``f``, a seekable binary file
    open for writing. Streams when the decoder can (write_wav), so a long
    recording is never held in memory as a WAV; otherwise writes to_wav's
    result. Returns the WAV's size. Exceptions as Format.decoder.to_wav."""
    dec = _decoder_of(fmt)
    write = getattr(dec, "write_wav", None)
    if write is not None:
        return write(data, f, should_stop=should_stop)
    wav = dec.to_wav(data, should_stop=should_stop)
    f.write(wav)
    return len(wav)


def analyze(fmt, data, should_stop=None):
    """(audio fingerprint, exact length in seconds or None) of ``data``
    decoded, the same values openevp.wavinfo.wav_fingerprint and the WAV's
    header give for the WAV to_wav would make. A decoder that can stream
    (pcm) is hashed as it decodes, with no WAV in memory. Exceptions as
    Format.decoder.to_wav, plus ValueError for a WAV that is not PCM."""
    dec = _decoder_of(fmt)
    stream = getattr(dec, "pcm", None)
    stream = stream(data, should_stop=should_stop) if stream is not None else None
    if stream is not None:
        channels, width, rate, chunks = stream
        total = 0

        def counted():
            nonlocal total
            for c in chunks:
                total += len(c)
                yield c
        fp = _wavinfo.fingerprint_stream(channels, width, rate, counted())
        frames = total // (channels * width)
        return fp, (frames / rate if rate else None)
    wav = dec.to_wav(data, should_stop=should_stop)
    with _wavinfo.buffer_file(wav) as f:
        fp = _wavinfo.wav_fingerprint(f)
    with _wavinfo.buffer_file(wav) as f, wave.open(f) as w:
        return fp, (w.getnframes() / w.getframerate() if w.getframerate() else None)


def fingerprint(fmt, data, should_stop=None):
    """analyze()'s fingerprint alone."""
    return analyze(fmt, data, should_stop)[0]
