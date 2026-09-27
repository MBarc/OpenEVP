"""Clean-room LPEC decoder (ICD-ST25 "LP", 8000 Hz / 6000 bit/s).

See docs/lpec.md for the algorithm this package implements.

This package is importable without the extracted table data: only calling
into the decoder needs it. `dvf_to_wav` raises `TablesMissing` (from
`openevp.decoders.sony_lpec.tables.load()`) when `openevp/decoders/sony_lpec/data/lpec_tables.json` is absent.
`st25/audio.py` calls `check()` (if present) to detect that up front, so
`capabilities()["wav_status"]` (and anything gated on `audio.available()`)
reports "could not be loaded" immediately instead of only on the first
export or playback attempt.
"""

from . import tables as _tables
from ._core import available as fast_decoder_available
from .decoder import Cancelled, dvf_to_wav
from .tables import TablesInvalid, TablesMissing

__all__ = ["Cancelled", "TablesInvalid", "TablesMissing", "dvf_to_wav",
           "fast_decoder_available"]


def check() -> None:
    """Raise TablesMissing if the extracted table data is not present.

    Does not decode anything; it only loads and validates the table data,
    the same way the first call to dvf_to_wav would. Called by
    st25/audio.py so unavailability surfaces right away.
    """
    _tables.load()
