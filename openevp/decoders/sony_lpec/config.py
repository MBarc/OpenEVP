"""LPEC decoder configurations: LPEC LP (8000 Hz / 6000 bit/s) and LPEC SP
(16000 Hz / 16000 bit/s).

Sony's LPEC.dll runs one algorithm, parameterised by
``InitDecoder(rate, bitrate)``; see docs/lpec.md, "Configuration constants"
and "LPEC SP (16000 Hz)". A ``Config`` holds every value the decoder derives
from the pair. They are computed here from the formulas the doc gives
(``_derive``), and the two configurations the decoder supports are checked
against the values the DLL itself stores (the table in the doc) when this
module is imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


def _rhu(num: int, den: int) -> int:
    """floor(num/den + 0.5) for non-negative integers, exactly."""
    return (2 * num + den) // (2 * den)


@dataclass(frozen=True)
class Config:
    """The constants ``InitDecoder(rate, bitrate)`` derives (docs/lpec.md)."""

    name: str                   # "LP" / "SP"
    rate: int                   # sample rate, Hz
    bitrate: int                # bit/s
    frame: int                  # samples per frame F (512 / 1024)
    order: int                  # LPC order (10 / 16)
    bands: int                  # bands per coefficient block (8 / 10)
    lsp_stages: int             # LSP VQ stages, 6 bits each (3 / 4)
    lag_bits: int               # pitch lag field width (7 / 8)
    mode_bits: Tuple[int, ...]  # frame bit budget by mode 0..3
    transform_n: Tuple[int, ...]  # transform length N by type t 0..3
    band_width: Tuple[int, ...]   # W[t]
    first_coded: Tuple[int, ...]  # s[t]
    end_coded: Tuple[int, ...]    # e[t] = s + bands*W
    overlap: Tuple[int, ...]      # the transform's overlap parameter Lambda[t]
    alloc_ref: Tuple[int, ...]    # ref[t]
    alloc_scale: Tuple[int, ...]  # sc[t]
    lsp_q_shifts: Tuple[int, ...]  # Q format of the int16 LSP codebooks D1..
    data_file: str              # the extracted-table file in data/

    @property
    def mode_bytes(self) -> Tuple[int, ...]:
        return tuple(b // 8 for b in self.mode_bits)

    @property
    def half(self) -> int:
        return self.frame // 2

    @property
    def quarter(self) -> int:
        return self.frame // 4

    @property
    def history(self) -> int:
        """Samples of pitch history kept between frames (a fixed 2 KB buffer
        in the DLL: 256 doubles at either rate)."""
        return 256

    @property
    def max_lag(self) -> int:
        return (1 << self.lag_bits) - 1


def _derive(name: str, rate: int, bitrate: int, alloc_r: int, data_file: str) -> Config:
    """The constants InitDecoder(rate, bitrate) computes (docs/lpec.md)."""
    frame = 512 if rate < 11026 else 1024
    wide = rate > 8000
    order = 16 if wide else 10
    bands = 10 if wide else 8
    stages = 4 if wide else 3
    x = 32 * ((frame * bitrate + 31) // (rate * 32))
    mode_bits = (x, x * 3 // 4, x * 5 // 4, x)
    transform_n = (frame, frame * 3 // 2, frame * 3 // 2, frame * 2)
    m = [n // 2 for n in transform_n]
    # The DLL narrows the coded band for LP's 6000 bit/s (and below): the
    # coded region ends at 3500 Hz and starts about 32 Hz up; otherwise it
    # ends at 15/16 of Nyquist and starts about 30 Hz up.
    narrow = rate <= 8000 and 0 < bitrate <= 6000
    top = 3500 if narrow else rate * 15 // 32
    low = 32 if narrow else 30
    unit = (128 * top) // (bands * (rate // 2))
    band_width = tuple((mt // 128) * unit for mt in m)
    first = tuple(max(1, _rhu(low * mt, rate // 2)) for mt in m)
    end = tuple(s + bands * w for s, w in zip(first, band_width))
    overlap = (frame // 2, frame // 2, frame, frame)
    ref = (alloc_r, alloc_r * 3 // 2, alloc_r * 3 // 2, alloc_r * 2)
    scale = tuple(_rhu(1 << 22, bands * w) for w in band_width)
    lag_bits = 7
    lag_range = (rate // 8000) * 120
    if lag_range > 128:
        lag_bits += 1
    if lag_range > 256:
        lag_bits += 1
    shifts = (16, 19, 19, 19) if stages == 4 else (16, 17, 18)
    return Config(name=name, rate=rate, bitrate=bitrate, frame=frame, order=order, bands=bands,
                  lsp_stages=stages, lag_bits=lag_bits, mode_bits=mode_bits,
                  transform_n=transform_n, band_width=band_width, first_coded=first,
                  end_coded=end, overlap=overlap, alloc_ref=ref, alloc_scale=scale,
                  lsp_q_shifts=shifts, data_file=data_file)


LP = _derive("LP", 8000, 6000, 128, "lpec_tables.json")
SP = _derive("SP", 16000, 16000, 432, "lpec_sp_tables.json")

# The values LPEC.dll stores after InitDecoder (docs/lpec.md); a formula
# above that drifted from them would fail here, at import.
_EXPECTED = {
    LP: dict(frame=512, order=10, bands=8, lsp_stages=3, lag_bits=7,
             mode_bits=(384, 288, 480, 384), transform_n=(512, 768, 768, 1024),
             band_width=(28, 42, 42, 56), first_coded=(2, 3, 3, 4),
             end_coded=(226, 339, 339, 452), overlap=(256, 256, 512, 512),
             alloc_ref=(128, 192, 192, 256), alloc_scale=(18725, 12483, 12483, 9362)),
    SP: dict(frame=1024, order=16, bands=10, lsp_stages=4, lag_bits=8,
             mode_bits=(1024, 768, 1280, 1024), transform_n=(1024, 1536, 1536, 2048),
             band_width=(48, 72, 72, 96), first_coded=(2, 3, 3, 4),
             end_coded=(482, 723, 723, 964), overlap=(512, 512, 1024, 1024),
             alloc_ref=(432, 648, 648, 864), alloc_scale=(8738, 5825, 5825, 4369)),
}
for _cfg, _want in _EXPECTED.items():
    for _k, _v in _want.items():
        if getattr(_cfg, _k) != _v:
            raise AssertionError(f"LPEC {_cfg.name} config: {_k} = {getattr(_cfg, _k)!r}, expected {_v!r}")
del _cfg, _want, _k, _v

BY_NAME = {"LP": LP, "SP": SP}
