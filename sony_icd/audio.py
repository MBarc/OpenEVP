"""WAV from .dvf, through our own Sony decoders, picked by the file's codec byte:

- LPEC LP (ICD-ST25 and ICD-ST10, codec 0x2c): openevp.decoders.sony_lpec, 8 kHz mono;
- LPEC SP (ICD-ST10, codec 0x2a): openevp.decoders.sony_lpec in its 16 kHz
  configuration (the same decoder, its own table data), 16 kHz mono;
- LPEC ST (ICD-ST10, codec 0x24): openevp.decoders.sony_lpec_st, 44.1 kHz stereo.

The app greys out WAV export and playback while available() is False and
shows status() as the reason. status() can also be set while available()
is True: a built (frozen) app whose fast C decoder could not be loaded still
converts, in slow mode, and says so. available() and status() speak of the
LP decoder unless given another codec (CODEC_ST, CODEC_SP); dvf_to_wav() and
its streamed forms choose by the data's own codec byte.
"""
import importlib
import sys

from sony_icd import dvf as _dvf

DECODER = "openevp.decoders.sony_lpec"         # imported on demand: optional in a build
ST_DECODER = "openevp.decoders.sony_lpec_st"
CODEC_LP, CODEC_SP, CODEC_ST = _dvf.CODEC_LP, _dvf.CODEC_SP, _dvf.CODEC_ST

SLOW_MODE = ("slow mode: the fast decoder could not be loaded, so converting "
             "recordings to WAV takes much longer than usual")


class DecoderUnavailable(Exception):
    pass


class Cancelled(Exception):
    """dvf_to_wav was stopped by its should_stop callback."""


def _name(codec):
    return ST_DECODER if codec == CODEC_ST else DECODER


def _decoder(codec=None):
    """(module, None) when the decoder for ``codec`` (LP by default) can be
    used, else (None, reason)."""
    name = _name(codec)
    what = "the LPEC ST decoder" if name == ST_DECODER else "the WAV decoder"
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            if name == ST_DECODER:
                return None, "LPEC ST (ICD-ST10) playback is not included in this build"
            return None, "WAV conversion is not included in this build"
        return None, f"{what} could not be loaded: {e}"
    except ImportError as e:
        return None, f"{what} could not be loaded: {e}"
    if not hasattr(mod, "dvf_to_wav"):
        return None, f"{what} could not be loaded: it has no dvf_to_wav()"
    check = getattr(mod, "check", None)
    if check is not None:
        try:
            if codec == CODEC_SP:           # the LPEC decoder's 16 kHz table data
                check(codec)
            else:
                check()
        except Exception as e:
            return None, f"{what} could not be loaded: {e}"
    return mod, None


def available(codec=None):
    return _decoder(codec)[0] is not None


def status(codec=None):
    """Why WAV conversion (of ``codec``, LP by default) is unavailable, or a
    warning that it runs in slow mode; None when it works normally.

    Slow mode is only reported for a built app (sys.frozen), which always
    ships the fast decoders: from source, pure Python is a normal choice."""
    mod, reason = _decoder(codec)
    if mod is None:
        return reason
    fast = getattr(mod, "fast_decoder_available", None)
    if getattr(sys, "frozen", False) and fast is not None and not fast():
        return SLOW_MODE
    return None


def _module_for(dvf_bytes):
    mod, reason = _decoder(_dvf.codec(dvf_bytes))
    if mod is None:
        raise DecoderUnavailable(reason)
    return mod


def _cancelled(mod, e):
    stopped = getattr(mod, "Cancelled", None)
    return stopped is not None and isinstance(e, stopped)


def dvf_to_wav(dvf_bytes, should_stop=None):
    """The WAV for a .dvf recording. ``should_stop``: a callable polled
    during the decode; when it returns true, Cancelled is raised."""
    mod = _module_for(dvf_bytes)
    if should_stop is None:
        return mod.dvf_to_wav(dvf_bytes)
    try:
        return mod.dvf_to_wav(dvf_bytes, should_stop=should_stop)
    except Exception as e:
        if _cancelled(mod, e):
            raise Cancelled(str(e)) from e
        raise


def dvf_write_wav(dvf_bytes, f, should_stop=None):
    """dvf_to_wav written into ``f`` (a seekable binary file); returns its size.
    A decoder that can stream (LPEC ST) writes as it decodes, so a long
    recording is never held in memory; otherwise the WAV is made, then written."""
    mod = _module_for(dvf_bytes)
    write = getattr(mod, "dvf_write_wav", None)
    if write is None:
        wav = dvf_to_wav(dvf_bytes, should_stop=should_stop)
        f.write(wav)
        return len(wav)
    try:
        return write(dvf_bytes, f, should_stop=should_stop)
    except Exception as e:
        if _cancelled(mod, e):
            raise Cancelled(str(e)) from e
        raise


def dvf_pcm(dvf_bytes, should_stop=None):
    """(channels, sample width, rate, PCM chunks) of a .dvf's decoded audio,
    decoded as the chunks are consumed, or None when its decoder cannot
    stream (then use dvf_to_wav). The chunks raise Cancelled when stopped."""
    mod = _module_for(dvf_bytes)
    pcm = getattr(mod, "dvf_pcm", None)
    if pcm is None:
        return None
    channels, width, rate, chunks = pcm(dvf_bytes, should_stop=should_stop)

    def translated():
        try:
            yield from chunks
        except Exception as e:
            if _cancelled(mod, e):
                raise Cancelled(str(e)) from e
            raise
    return channels, width, rate, translated()
