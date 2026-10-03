"""Clean-room LPEC decoder: ICD-ST25 / ICD-ST10 "LP" (8000 Hz / 6000 bit/s)
and ICD-ST10 "SP" (16000 Hz / 16000 bit/s).

See docs/lpec.md for the algorithm this package implements; the two modes
are one algorithm with two configurations (config.LP, config.SP), each with
its own table data.

This package is importable without the extracted table data: only calling
into the decoder needs it. `dvf_to_wav` raises `TablesMissing` (from
`openevp.decoders.sony_lpec.tables.load()`) when the recording's data file
(`openevp/decoders/sony_lpec/data/lpec_tables.json` for LP,
`lpec_sp_tables.json` for SP) is absent. `sony_icd/audio.py` calls `check()`
(with the codec) to detect that up front, so `capabilities()["wav_status"]`
(and anything gated on `audio.available()`) reports "could not be loaded"
immediately instead of only on the first export or playback attempt.
"""

from . import tables as _tables
from ._core import available as fast_decoder_available
from .config import LP, SP
from .decoder import CONFIGS, Cancelled, dvf_to_wav, frame_count
from .tables import TablesInvalid, TablesMissing

__all__ = ["Cancelled", "LP", "SP", "TablesInvalid", "TablesMissing", "dvf_to_wav",
           "fast_decoder_available", "frame_count"]


def check(codec=None) -> None:
    """Raise TablesMissing if the extracted table data is not present.

    ``codec`` is the .dvf codec byte whose configuration is meant (sony_icd.dvf:
    CODEC_LP, the default, or CODEC_SP). Does not decode anything; it only
    loads and validates the table data, the same way the first call to
    dvf_to_wav would. Called by sony_icd/audio.py so unavailability surfaces
    right away.
    """
    _tables.load(config=CONFIGS.get(codec, LP))
