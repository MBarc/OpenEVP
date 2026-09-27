"""Saving .dvf recordings without ever replacing an existing file.

The never-overwrite publication itself is shared (openevp.export, re-exported
here for compatibility); what counts as "already saved" for a .dvf (identical
audio, ignoring the time counters DVE rewrites) is ST25-specific and stays here.
"""
from openevp.export import _choose, publish, save_raw, save_unique, save_wav  # noqa: F401

from . import dvf


def target_path(outdir, name, fingerprint):
    """(path, already_saved) for a .dvf: identical audio counts as already saved."""
    return _choose(outdir, name, lambda existing: dvf.audio_matches(existing, fingerprint))


def save_dvf(data, outdir, name):
    """Save a .dvf unless one with identical audio is already there (DVE rewrites
    time counters, so bytes may differ). Returns (path, already_saved)."""
    fingerprint = dvf.audio_fingerprint(data)
    return save_unique(data, outdir, name, lambda existing: dvf.audio_matches(existing, fingerprint))
