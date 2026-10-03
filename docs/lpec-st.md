# LPEC ST decoder, 44100 Hz stereo (ICD-ST10)

A clean-room implementation of the decoder in Sony's `lcstde.ax` (Digital
Voice Editor's DirectShow filter for the "LPEC ST" modes, sha256
`71155a2bf88d17885436fec1628b376165b82ded54be831ff91bcf77a870fdef`), written
from a static and dynamic analysis of that DLL. The package is
`openevp.decoders.sony_lpec_st`; the ICD-ST25's LPEC LP decoder is described
in [lpec.md](lpec.md).

## Status

**Complete and bit-exact.** The float32 output of every frame, before the
int16 conversion, matches `lcstde.ax` bit for bit, so the PCM is identical.

| Test | Result |
|---|---|
| A_001, A_002, A_003 (real ICD-ST10 recordings, 681 frames, 31.4 s) vs the reference WAVs `*_sony_lcstde_44k_stereo.wav` | byte-identical PCM (100.0000% of samples, max abs diff 0) |
| same, frame by frame vs a live run of lcstde.ax | accept/reject status identical; float32 output 100% bit-identical |
| 3 generated streams, 2,941 random *valid* frames exercising every mode and feature, 59 decoder resets | 100% bit-identical float32 output |
| 3 mutated streams, 5,818 damaged real frames (bit flips, random bytes, header flags, resets): 2,477 accepted, 3,341 rejected | reject/accept identical on every frame; accepted frames 100% bit-identical (so the state left by rejected frames matches too) |
| earlier fuzz rounds during development (4,400 generated + 5,800 mutated frames) | identical |
| tables the DLL computes at load time (tone sine, tone window) | bit-identical to the DLL's own copies in memory |

Max absolute difference everywhere: **0**. Percentage of samples that match
exactly: **100%**.

What is not covered is listed under [Open issues](#open-issues). The
synthetic vectors are in `tests/vectors/lpec_st/`; the real recordings are
not in the repository (`tests/test_lpec_st_real.py` runs on them when
`OPENEVP_ST10_RECORDINGS` names their folder).

## Files (`openevp/decoders/sony_lpec_st/`)

| File | What |
|---|---|
| `__init__.py` | package API: `dvf_to_wav`, `dvf_write_wav`, `dvf_pcm`, `decode`, `Decoder`, `payload_from_raw`, `check`, `TablesMissing` |
| `decoder.py` | framing: header descrambling, counter-0 resets, start-up skip, rejected frames, int16 output; the .dvf entry points |
| `bitstream.py` | bit reader and the complete frame parser (integer work only) |
| `dsp.py` | synthesis: dequantisation, power compensation, stereo, IMDCT, gain compensation, tones, noise, 16-band filter bank |
| `_lpec_st.c`, `_core.py` | the optional C core: the synthesis of `dsp.py` and the int16 conversion, bit for bit (see [Performance](#performance)) |
| `tables.py` | loads `data/lpec_st_tables.json`, generates the two run-time tables (x87-exact) |
| `x87.py` | exact x87 `fsin`/`fcos` emulation (a copy of the LP decoder's, plus two rounding helpers) |
| `data/lpec_st_tables.json` | the extracted tables: **not in the repository** (git-ignored); written by `tools/import_lpec_st_tables.py`, shipped in the build |

`tools/import_lpec_st_tables.py` reads the tables from a copy of `lcstde.ax`
(`--tables-dir`, default `$OPENEVP_TABLE_DUMPS`, the folder the LP tables'
dumps come from, or `--dll`) and refuses any other build of the DLL.
`tools/build_lpec_core.py` builds `lpec_st_core.dll` next to the LP decoder's
`lpec_core.dll`.

## API

```python
from openevp.decoders.sony_lpec_st import dvf_to_wav, decode, payload_from_raw
wav = dvf_to_wav(dvf_bytes)                   # an ICD-ST10 .dvf -> WAV (44.1 kHz stereo 16-bit)
rate, channels, pcm = decode(frames_bytes)    # 44100, 2, interleaved little-endian int16 bytes
pcm = decode(payload_from_raw(raw_dump))[2]   # a raw 1056-byte-block ICD-ST10 voice dump
```

`dvf_to_wav` checks the file (`sony_icd.dvf.validate`) and its codec byte (0x24)
first, so a damaged file or an LPEC LP file raises `sony_icd.dvf.FormatError`.
`dvf_write_wav(dvf_bytes, f)` writes the same WAV into a file as it decodes,
and `dvf_pcm(dvf_bytes)` gives the PCM frame by frame (for the marks
fingerprint), so a long recording is never held in memory. Every entry point
takes `should_stop` (polled every 16 frames, raises `Cancelled`) and
`use_core` (None: the C core when it loaded; False: pure Python).

## The codec

LPEC ST is a transform codec: 2048 samples per channel per frame, split by
a 16-band synthesis filter bank into 16 subbands of 128 samples, each coded
with a 256-point IMDCT, plus gain control, sinusoidal "tones" and noise
filling. Its bitstream syntax is structurally the same as Sony's ATRAC3plus
as known from public reverse engineering (32 quantisation units, word
lengths, scale factors, code-table selectors, Huffman-coded spectrum, gain
control points, tones, noise); every detail here was taken from
`lcstde.ax` itself, not from any other implementation.

ICD-ST10: 44100 Hz, stereo, 280 codec bytes per 2048-sample frame (about 48.2 kbit/s).

## Framing (`decoder.py`)

- Payload: 283-byte frames = 3-byte header (big-endian 16-bit counter,
  flags byte) + 280 codec bytes. A trailing partial frame is ignored.
- Counter 0: segment-header frame. Skipped; the decoder is reset (a fresh
  decoder, as the reference run of `lcstde.ax` does).
- Flags bits 7-6 = `01`: the first 8 codec bytes are XOR-scrambled with key
  `(flags >> 4) & 3` (4 keys of 16 bytes in the DLL) starting at key byte
  `flags & 15` (wrapping). Other values: no scrambling (the DLL's check
  returns 0 for `10`/`11`, and the frame is still decoded).
- A fresh decoder outputs nothing for its first accepted frame (start-up
  delay); after each reset too.
- A rejected frame outputs nothing and changes no decoder state. The reader
  may run past the frame into the following bytes (up to bit 0x10000 of the
  frame; beyond that it reads 0), as the DLL does in the caller's buffer.
- Output: int16 by round-half-to-even of the float32 value (x87 `fistp`),
  clamped; 2048 samples per channel per frame.

OpenEVP keeps this framing in its WAV files (the first frame after each reset
gives no samples, and a counter-0 frame is a reset), so the samples, and with
them the EVP marks fingerprint (`openevp.wavinfo`), are exactly those of
Sony's decoder: a WAV OpenEVP writes for an ICD-ST10 recording is
byte-identical to the one `lcstde.ax` gives (checked on three real
recordings). Any change to the decoder's output re-keys every ICD-ST10
recording's marks.

The length shown before a recording is decoded (the recorder's list, a .dvf's
header) is an estimate from the frame count alone: every whole frame but the
first. That is exact for a recording made in one go; each restart inside a
recording makes it about 0.09 s long. The decoded WAV's length is exact, and
the library shows it once a file is indexed.

## Bitstream (`bitstream.py`)

MSB-first. Each n-bit read takes the three bytes at the current byte and
shifts (the DLL's reader). Huffman codes: peek `maxbits` bits, a lookup
table gives the symbol, the reader then moves back by `maxbits - len`.

Frame: `0` bit (else reject 0x208), then units until type `11`:
`01` = stereo channel unit (exactly one; `00` = mono is rejected in a
stereo decoder), `10` = extension (5 bits, 11-bit length, that many bytes,
skipped). After each unit, more than `280*8-2` bits used rejects the frame.

Channel unit, in order:

1. number of quantisation units `nqu` = 5 bits + 1 (29..31 only set a
   warning; the frame still decodes); subbands `nsb` from a table; mute bit.
2. **Word lengths** (per channel, 2-bit mode; channel 1 can code relative
   to channel 0): raw 3 bits; direct values + fixed-width deltas above a
   minimum, with weight tables; a shape (base, shape index) plus Huffman
   deltas, optionally in pairs with a skip bit; Huffman deltas from the
   previous unit (mode 3). Fill modes for the uncoded units (zeros, ones,
   1-bit flags, or ones up to a coded boundary). Coded units = last unit
   with a nonzero word length in either channel.
3. **Scale factors** (6-bit, 2-bit mode each channel): raw; direct values
   + fixed-width deltas or shape + 4-bit corrections, optional weight
   subtraction; shape + Huffman deltas; delta chains; channel 1 relative to
   channel 0 (Huffman, delta-of-deltas, or copy).
4. **Code-table selectors**: a "full table" bit, then per channel a table
   set bit, a 2-bit mode and an optional count; per unit a flag: 1 = coded
   (raw 2/3 bits, Huffman, delta chain, or relative to channel 0), 2 =
   channel-1 unit empty but channel-0 unit coded (1 bit).
5. **Spectrum**: per coded unit, one of 112 Huffman tables (table set,
   selector, word length); codewords pack 1, 2 or 4 coefficients, either
   signed fields or magnitudes followed by sign bits; some tables code
   groups with a 1-bit "all zero" flag. Then 4-bit power levels (when more
   than 2 units are coded), then stereo swap and negate flags per subband.
6. **Window shapes**: a flag per subband per channel.
7. **Gain control** per channel: presence, coded / total subbands; per
   subband up to 7 points (level 0..15, location 0..31), each part in one
   of 4 modes per channel (raw, Huffman, delta chains, relative to the
   previous subband or to channel 0, "raw location" coding that grows
   narrower as locations rise). Levels must differ between neighbours and
   locations must rise, or the frame is rejected.
8. **Tones**: presence, amplitude mode, number of tone bands (Huffman);
   share / swap / negate flags per band; per channel: envelope (start/stop
   positions), number of waves (<= 48 in total), frequencies (10 bits,
   ascending with shrinking widths, descending, or Huffman deltas against
   channel 0), amplitude scale factors and indices (several modes; channel
   1 predicts from the nearest channel-0 wave within 7 frequency steps),
   5-bit phases. Shared bands copy channel 0 (and channel 0's previous
   envelope); swapped bands exchange channels.
9. **Noise**: presence bit, 4-bit level, 4-bit table index.

## Synthesis (`dsp.py`)

Per frame, in the DLL's order:

1. **Stereo fill-in**: a channel-1 unit with no data whose code-table flag
   is 0, where channel 0 is coded, takes channel 0's quantised values and
   word length.
2. **Dequantisation**: `f32(q * f32(SF[sf] * MANT[wl]))`.
3. **Power compensation** per coded subband: noise from a 1024-entry table
   (start position from the sum of all scale factors, stepping by 0x80 per
   subband) scaled by `SF * MANT * f32(PWR[level] / 2^shift) / 2^wl`, where
   `shift` comes from the subband's gain points (the other channel's when the
   subband is swapped), added to units above a per-subband start.
4. Stereo swap (128 coefficients per subband) and negate (channel 1), mute.
5. Per channel, per subband below `nsb`: **IMDCT** 128 -> 256: pre-twiddle
   (coefficient order reversed on odd subbands), a 64-point complex radix-2
   FFT, post-rotation with one of 4 windows chosen by the previous and
   current window-shape flags. Subbands at and above `nsb` output zeros and
   clear their overlap.
6. **Gain compensation** when either frame has gain points: a 256-point
   gain curve from the previous and current points (steps interpolated over
   3 samples from a table), applied to the new half and to the part of the
   overlap after the first level change; otherwise plain overlap-add.
7. **Tones**: per band, the previous frame's waves (second half) and the
   current frame's (first half) from a 2048-entry sine table, envelope
   ramps, cross-faded with a 256-point window unless an envelope boundary
   applies; channel-1 negation.
8. **Noise** (time domain): 16 subbands x 128 samples from the noise table
   at per-subband offsets, scaled by `2^level / 32768`.
9. **Filter bank**: per output sample, 16 subband values are weighted,
   passed through a 16-point butterfly network and a 10-stage polyphase
   structure. The DLL runs it over a 3-phase circular buffer; `dsp.qmf`
   documents the equivalent explicit recurrence, which lets each stage run
   over all 128 samples at once.

### Arithmetic

The DLL mixes x87 code (run with precision control 53 bits, the Windows
default, confirmed in a live run: control word 0x9001F) and SSE packed-single
code (chosen when CPUID reports SSE, i.e. on every CPU since 1999).
Reproduced exactly:

- x87: each operation is an IEEE double operation (Python float); values
  stored to float variables are rounded to float32 (`array('f')`); values
  the DLL keeps on the FPU stack stay doubles.
- SSE: each lane result is rounded to float32 on its own (a double
  operation on float32 inputs followed by one rounding is exact for + - *).
- Operation order is the DLL's wherever it can matter.
- Tone sine and window tables: the DLL computes them at load with
  `fsin`/`fcos` (64-bit mantissa, argument reduced with a 66-bit pi); they
  are generated with the same exact emulation the LP decoder uses.

The C core (`_lpec_st.c`) follows `dsp.py` operation by operation: each
Python float operation is one IEEE double operation in C, in the same order,
and each `f32()` rounding is a `(float)` conversion. It must be built without
fused multiply-add and without fast-math (`-ffp-contract=off
-fno-fast-math`, as `tools/build_lpec_core.py` does; x86-64 gcc does double
arithmetic in SSE2, so there is no x87 excess precision). Its float32 output
is checked against Sony's on every test vector (`tests/test_lpec_st_core.py`).

## Tables

Extracted verbatim into the JSON (floats as IEEE bit patterns): unit layout
(sizes, starts, unit->subband), 4 XOR keys, shape and weight tables for word
lengths and scale factors, code-table remap, all Huffman descriptors
(4 + 4 + 4 + 4 + 112 + 10 + 11), scale factor and quantiser step tables,
power levels, noise table and offsets, IMDCT twiddles, bit-reversal order
and 4 windows, gain steps, tone amplitude tables and ramp, filter-bank
weights, butterfly constants, window and index tables, and scalar constants.
Nothing Sony-derived is written into the code: the butterfly constants are
indices into the extracted table.

## Performance

CPython 3.12, per second of audio (CPU):

| | |
|---|---|
| pure Python (`use_core=False`), real recordings | 0.36 s |
| pure Python, worst case (16 subbands, tones in every frame) | 0.40 s |
| with the C core, real recordings | 0.022 s |
| with the C core, generated streams (all features) | 0.016-0.022 s |

With the C core, parsing the bitstream (still Python) is about two thirds of
the time. The longest ICD-ST10 recording (its 32 MB of flash, about 92
minutes) decodes in about two minutes with the C core, half an hour in pure
Python. Loading the tables takes about 0.35 s (cached per process).

Its WAV is 176,400 bytes per second, about 930 MB for 92 minutes. The app
decodes a recording for playback straight into its cache file and
fingerprints it frame by frame (library indexing), so neither holds the WAV
in memory; a WAV export or a marked backup holds it once (see the user
guide's [known limits](user-guide.md#known-limits)).

## Open issues

- **x87-only CPUs**: on a CPU without SSE the DLL takes a separate x87
  synthesis path (FUN_10006f20 and callees) whose rounding differs. Not
  implemented; irrelevant on any machine that runs DVE today.
- **Other configurations** the DLL supports (mono streams, 48000 Hz, the
  24 kbit/s "STLP" mode with 147-byte frames) are not handled; the ICD-ST10
  recordings are 44100 Hz stereo, 283-byte frames. The frame length is a
  constant here.
- **DVE itself** was not run: the reference is `lcstde.ax` called directly,
  through the same entry points DVE's filter uses. How DVE treats counter-0
  frames, the start-up frame and rejected frames in its own WAV export is
  assumed to match that reference run.
- The real recordings exercise only a small part of the format (raw word
  lengths and scale factors, gain control, power levels, 8 subbands; no
  tones, noise, window shapes or scrambled headers). Everything else is
  verified on generated and mutated frames only; recordings in other ST10
  modes (if any) would be worth one more comparison with `lcstde.ax`.
- The PCM framing (skipping the first frame after each reset) mirrors the
  reference run of `lcstde.ax`; OpenEVP keeps it (see Framing).
