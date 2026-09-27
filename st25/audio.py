"""WAV from .dvf, through our own LPEC decoder (openevp.decoders.sony_lpec).

The app greys out WAV export and playback while available() is False and
shows status() as the reason. status() can also be set while available()
is True: a built (frozen) app whose fast C decoder could not be loaded still
converts, in slow mode, and says so.
"""
import importlib
import sys

DECODER = "openevp.decoders.sony_lpec"      # imported on demand: optional in a build

SLOW_MODE = ("slow mode: the fast decoder could not be loaded, so converting "
             "recordings to WAV takes much longer than usual")


class DecoderUnavailable(Exception):
    pass


class Cancelled(Exception):
    """dvf_to_wav was stopped by its should_stop callback."""


def _decoder():
    """(module, None) when the decoder can be used, else (None, reason)."""
    try:
        mod = importlib.import_module(DECODER)
    except ModuleNotFoundError as e:
        if e.name == DECODER:
            return None, "WAV conversion is not included in this build"
        return None, f"the WAV decoder could not be loaded: {e}"
    except ImportError as e:
        return None, f"the WAV decoder could not be loaded: {e}"
    if not hasattr(mod, "dvf_to_wav"):
        return None, "the WAV decoder could not be loaded: it has no dvf_to_wav()"
    check = getattr(mod, "check", None)
    if check is not None:
        try:
            check()
        except Exception as e:
            return None, f"the WAV decoder could not be loaded: {e}"
    return mod, None


def available():
    return _decoder()[0] is not None


def status():
    """Why WAV conversion is unavailable, or a warning that it runs in slow
    mode; None when it works normally.

    Slow mode is only reported for a built app (sys.frozen), which always
    ships the fast decoder: from source, pure Python is a normal choice."""
    mod, reason = _decoder()
    if mod is None:
        return reason
    fast = getattr(mod, "fast_decoder_available", None)
    if getattr(sys, "frozen", False) and fast is not None and not fast():
        return SLOW_MODE
    return None


def dvf_to_wav(dvf_bytes, should_stop=None):
    """The WAV for a .dvf recording. ``should_stop``: a callable polled
    during the decode; when it returns true, Cancelled is raised."""
    mod, reason = _decoder()
    if mod is None:
        raise DecoderUnavailable(reason)
    if should_stop is None:
        return mod.dvf_to_wav(dvf_bytes)
    try:
        return mod.dvf_to_wav(dvf_bytes, should_stop=should_stop)
    except Exception as e:
        stopped = getattr(mod, "Cancelled", None)
        if stopped is not None and isinstance(e, stopped):
            raise Cancelled(str(e)) from e
        raise
