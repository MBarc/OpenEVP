"""Clean-room LPEC ST decoder (Sony ICD-ST10, 44100 Hz stereo, 283-byte frames).

See docs/lpec-st.md for the algorithm. Bit-exact with the decoder in Sony's
lcstde.ax: the float32 output before the int16 conversion matches bit for
bit, so the PCM is identical. Pure Python (bitstream.py, dsp.py), with an
optional C core for the synthesis (_lpec_st.c -> lpec_st_core.dll, built by
tools/build_lpec_core.py) that gives the same output about 15x faster.

This package is importable without the extracted table data: only calling
into the decoder needs it. ``dvf_to_wav`` raises ``TablesMissing`` (from
``tables.load()``) when ``openevp/decoders/sony_lpec_st/data/lpec_st_tables.json``
is absent. st25/audio.py calls ``check()`` to detect that up front, so an
ICD-ST10 recording is reported as "can't be played" with the reason instead
of failing on the first playback.

API:
    dvf_to_wav(dvf_bytes) -> WAV (bytearray; 44.1 kHz stereo 16-bit)
    dvf_write_wav(dvf_bytes, f) -> the same WAV written to a file as it decodes
    dvf_pcm(dvf_bytes) -> (channels, width, rate, PCM chunks) for fingerprinting
    decode(frames_bytes) -> (44100, 2, interleaved int16 little-endian PCM bytes)
    Decoder: frame-level pure-Python decoder (decode_frame / frame_pcm / reset)
    payload_from_raw(raw): the frame payload of a raw ICD-ST10 voice dump
"""

from . import tables as _tables
from ._core import available as fast_decoder_available
from .decoder import (CHANNELS, FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE, Cancelled,
                      Decoder, decode, dvf_pcm, dvf_to_wav, dvf_write_wav, payload_from_raw,
                      pcm_chunks)
from .tables import TablesInvalid, TablesMissing

__all__ = ["CHANNELS", "FRAME_BYTES", "FRAME_SAMPLES", "SAMPLE_RATE", "Cancelled",
           "Decoder", "TablesInvalid", "TablesMissing", "check", "decode", "dvf_pcm",
           "dvf_to_wav", "dvf_write_wav", "fast_decoder_available", "payload_from_raw",
           "pcm_chunks"]


def check() -> None:
    """Raise TablesMissing if the extracted table data is not present (or is
    damaged). Decodes nothing; loads and checks the tables as the first
    decode would."""
    _tables.load()
