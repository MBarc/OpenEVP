# LPEC decoder, 8000 Hz / 6000 bit/s (ICD-ST25 "LP") and 16000 Hz / 16000 bit/s (ICD-ST10 "SP")

Written 2026-09-25 from a static map of Sony's `LPEC.dll` (sha256
`ed3fd84a709bf0ece623b50c58d15f66dc2c02c37b4e2f568095ec91e3c356df`) and confirmed
dynamically against it. This document describes the algorithm in prose,
formulas and field tables for an independent reimplementation. It contains no
Sony code and no extracted table data. The sections up to
[Open points](#open-points) describe the 8000 Hz mode;
[LPEC SP (16000 Hz)](#lpec-sp-16000-hz) lists what differs at 16000 Hz.

## Status

**Complete for this mode.** A local, never-committed research model that
implements exactly this description reproduces Sony's decoder:

| Test | Result |
|---|---|
| `tests/vectors/*` (13 short vectors) | PCM byte-identical to the committed `.pcm` |
| `tests/vectors/long-mixed-10min` (9,375 frames) | SHA-256 of the PCM matches the committed value |
| 20 real recordings (18,622 frames) | byte-identical to DVE's WAVs |
| 15 random byte streams (about 3,700 frames, all four modes, pitch lags down to 1, unstable pitch filters, stale indices) | byte-identical to the DLL |
| internal state after every frame (random stream, one vector, one real recording) | every double of the LSPs, the pre-rounding output, the pitch history and the overlap buffer is bit-identical |
| `ResetDecoder` in the middle of a stream | identical to the DLL |

A negative control (changing the association of one sum) is detected in every
frame of the state comparison, so the comparison does test operation order.

What remains unverified is listed under [Open points](#open-points).

## Contents

1. [Conventions](#conventions)
2. [API behaviour and framing](#api-behaviour-and-framing)
3. [Configuration constants](#configuration-constants)
4. [Bitstream](#bitstream)
5. [Parameter decoding](#parameter-decoding)
6. [Coefficient block](#coefficient-block)
7. [Synthesis](#synthesis)
8. [State between frames](#state-between-frames)
9. [Arithmetic](#arithmetic)
10. [Tables](#tables)
11. [LPEC SP (16000 Hz)](#lpec-sp-16000-hz)
12. [Open points](#open-points)

## Conventions

**Floating point.**
- Every real value is an IEEE-754 binary64 double. Every `+ − × ÷` rounds to
  nearest-even and is evaluated in exactly the order the parentheses show; no
  fused multiply-add. The DLL runs x87 code at 53-bit precision, which rounds
  like binary64 (see [Arithmetic](#arithmetic)).
- `a·b` means a × b. Where both operand orders are the same value (`a·b = b·a`,
  `a+b = b+a`), the order is not given. **Association is always given.**
- Decimal constants mean "the binary64 nearest to this decimal". The DLL uses
  four different truncated decimal values of π or 2π: `3.14159265359` (per
  frame), and `6.28318530718`, `6.283185307`, `3.1415926535` (table
  generation). Use them as written, not the true π.
- `floor(x)` is the mathematical floor. `trunc(x)` rounds toward zero.

**Integers.**
- 32-bit two's complement with wrap-around unless noted.
- `x >> k` is an arithmetic (flooring) shift. `x >>> k` is a logical shift of
  the 32-bit pattern.
- `a div b` is C division, rounding toward zero. `a mod b` is the matching
  remainder.
- `s16(x)` keeps the low 16 bits of x as a signed value (a store to an int16).
- `rhu(x)` = floor(x + 0.5).

**Indices.** Arrays are 0-based. `lsp[0]` is always 0; the 10 LSPs are
`lsp[1..10]`.

## API behaviour and framing

- `InitDecoder(8000, 6000)` builds the decoder for this mode. (The constructor
  allocates nothing; `ResetDecoder` before `InitDecoder` faults.)
- `Decode(out, in, n)` decodes **one frame** and writes **512 samples**
  (64 ms at 8 kHz). It returns 512.
  - It copies the `n` bytes at `in` into an internal 1,024-byte buffer, so `n`
    above 1,024 overruns it. The frame length is not a parameter: it follows
    from the frame's first 2 bits (the mode), see [Bitstream](#bitstream).
    Frames are 36, 48 or 60 bytes, so `n` = 60 (or whatever is left) is enough.
  - **Short input.** When the frame needs more bytes than `n`, the bit reader
    wraps to the start of the same `n` bytes and keeps reading. The frame is
    still decoded and its 512 samples output. This is what the last, truncated
    frame of a recording goes through. A rewrite that must match DVE's last frame
    has to wrap the same way.
  - **Continuing after a short frame.** The bytes consumed are the reader's byte
    position after wrapping, which can be fewer than the bytes left; decoding then
    continues with the rest. It stops when no bytes are left, or when a frame
    consumes nothing or would run past the end (after outputting that frame).
    Example: the `ends-mid-frame` vector (40 × 48 bytes + one 48-byte frame cut
    to 36) decodes to 42 frames, not 41.
  - After a frame, the number of bytes consumed is the reader's byte position
    (48, 36 or 60 for complete frames).
  - `in = NULL` outputs 512 zero samples and clears the filter memories (the
    same buffers `ResetDecoder` clears, and the end-of-frame "current becomes
    previous" copy is done first). DVE does not use this.
- `ResetDecoder()` performs a **partial** reset, listed in
  [State between frames](#state-between-frames).

## Configuration constants

`InitDecoder(8000, 6000)` derives these values (formulas given where they
matter; the rewrite can hard-code the values):

| Name | Value | Meaning |
|---|---|---|
| frame | 512 | samples per frame; half = 256, quarter = 128 |
| order | 10 | LPC order |
| budget by mode | 384, 288, 480, 384 bits | `X·{1, 3/4, 5/4, 1}` with `X = 32·((512·6000 + 31) div (8000·32)) = 384` |
| transform type t | 0, 1, 2, 3 | selects N below |
| N (transform length) | 512, 768, 768, 1024 | coefficients M = N/2 = 256, 384, 384, 512 |
| bands | 8 | per coefficient block |
| band width W[t] | 28, 42, 42, 56 | `(M div 128)·14`; 14 = `floor(128 / (8·4000) · 3500)` |
| first coded coefficient s[t] | 2, 3, 3, 4 | `rhu(32·M / 4000)`, at least 1 |
| end of coded region e[t] | 226, 339, 339, 452 | `s + 8·W`; coefficients `0..s−1` (below about 32 Hz) and `e..M−1` (above 3,500 Hz) are zero |
| allocation reference ref[t] | 128, 192, 192, 256 | `R·{1, 1.5, 1.5, 2}`, R = 128 (a table constant for 6000 bit/s) |
| allocation scale sc[t] | 18725, 12483, 12483, 9362 | `rhu(2^22 / (8·W))` |
| gain bits | 19 | 7 + 6 + 6 at the start of each coefficient block |
| pitch lag bits | 7 | |
| LSP stages × bits | 3 × 6 | |

## Bitstream

- **Bit order.** Bytes are read in order, each byte from its most significant
  bit down. A field of k bits is the next k bits, first bit most significant.
  Fields cross byte boundaries freely.
- **Frames are byte-aligned.** After the last field, the reader skips to the
  frame's bit budget (budgets are multiples of 8).
- **Frame length is signalled by the first 2 bits (the mode):**

| mode | bytes | bits | structure |
|---|---|---|---|
| 0 | 48 | 384 | two 256-coefficient transforms, optional mid-frame LSP set, per-half temporal shaping |
| 1 | 36 | 288 | one 384-coefficient transform, LSP + one pitch |
| 2 | 60 | 480 | a 256-coefficient transform then a 384-coefficient transform |
| 3 | 48 | 384 | one 512-coefficient transform, LSP + one pitch |

The ST25 corpus has 48-byte frames (modes 0 and 3) almost everywhere, with some
60-byte (mode 2) and 36-byte (mode 1) frames, matching the 17,917 / 382 / 323
count in the spike.

### Field order

"LSP set" = three 6-bit indices (stage 1, 2, 3; 18 bits).
"Pitch" = a 7-bit **lag**; when the lag is not 0, a 6-bit **gain index**
("pgidx") follows. (The DLL's string `Error in frame %d(lag = %d, pgidx = %d)`
belongs to the encoder's pitch search; the decoder has no error path.)

Mode 0:
1. `F`, 1 bit (mid-frame LSP flag).
2. If F = 0: LSP set **B** (end of frame). If F = 1: LSP set **A**
   (mid-frame), then LSP set **B**.
3. Pitch **A**, then pitch **B**.
4. Let `r = 384 − (bits read so far)`.
5. For half h = 0, then h = 1: shape flag `S_h`, 1 bit; if `S_h` = 1, a 7-bit
   shape index.
6. Coefficient block for half 0 (type 0), budget `(r div 2) − c_0`.
7. Coefficient block for half 1 (type 0), budget `(r div 2) − c_1`,
   where `c_h` = 8 if `S_h` = 1, else 1.

Mode 1: LSP set B; pitch B; coefficient block type 1, budget `288 − used`.

Mode 2: LSP set B; pitch A; pitch B; coefficient block type 0 with budget
`(384 − used) div 2` (note 384, not 480); coefficient block type 2 with
budget `480 − used` (used counted after the first block).

Mode 3: LSP set B; pitch B; coefficient block type 3, budget `384 − used`.

"used" is the number of bits read since the start of the frame, including the
2 mode bits. The budget only drives the bit allocation; the next field starts
right after the block's last field, and any unused bits sit at the end of the
frame.

### Coefficient block (budget β)

1. Global gain index, 7 bits.
2. Band-gain index 1, 6 bits; band-gain index 2, 6 bits.
3. The decoder then computes an allocation from `β − 19` (see
   [Bit allocation](#bit-allocation)). It yields four counts `K0, K1, K2, Ks`.
4. `K0` indices of 8 bits (2-dimensional VQ), then `K1` indices of 8 bits
   (4-dimensional VQ), then `K2` indices of 8 bits (8-dimensional VQ).
5. `Ks` sign bits, 1 bit each.

### What "lag" and "pgidx" select

- **Lag** (7 bits): 0 = no long-term prediction for that section; otherwise the
  delay in samples (1–127) of a 3-tap long-term predictor.
- **pgidx** (6 bits): a row of the 64 × 3 pitch-tap table (double taps `b0, b1,
  b2` and a Q15 copy). The Q15 middle tap `g = tap1_q15` is also used as the
  "voicing" value in the coefficient block (envelope and noise fill).

## Parameter decoding

### Parameter sets

A frame has up to three parameter "slots" per quantity: slot 0 = the previous
frame's end, slot 1 = mid-frame (A), slot 2 = end of frame (B). What each mode
fills:

| mode | LSP slot 1 | LSP slot 2 | pitch slot 1 | pitch slot 2 |
|---|---|---|---|---|
| 0, F = 0 | interpolated (below) | read | read | read |
| 0, F = 1 | read | read | read | read |
| 1 | not touched | read | not touched | read |
| 2 | interpolated | read | read | read |
| 3 | not touched | read | not touched | read |

**Interpolated slot 1** (mode 0 with F = 0, and mode 2):
- double LSPs: `l1[j] = (l2[j] + l0[j])·0.5`, j = 0..10;
- Q16 LSPs: `q1[j] = s16((q0[j] + q2[j]) div 2)`;
- and the "stage-1 index" of slot 1 is set to −1 in mode 0 with F = 0 only
  (mode 2 leaves the old value; see [Bit allocation](#bit-allocation)).

### LSP decoding (two parallel representations)

Each LSP set `(i1, i2, i3)` gives a double vector and a Q16 integer vector.

**Double path.**
1. `l[j+1] = (C3[i3][j] + C2[i2][j]) + C1[i1][j]`, j = 0..9, with `l[0] = 0.0`.
   `C1..C3` are 64 × 10 double codebooks.
2. Upper-half limit, for j = 10 down to 6: if `l[j] ≥ 0.5` then
   `l[j] = lim`. Then `lim = l[j] − 0.01`. `lim` starts at 0.49.
3. Lower-half limit, for j = 1 up to 5: if `l[j] < 0.0` then `l[j] = lim`.
   Then `lim = l[j] + 0.01`. `lim` starts at 0.01.
4. `l[0] = 0.0`.
5. One sorting pass, j = 1..10: if `l[j] < l[j−1]`, swap them; then, if j ≥ 2
   and `l[j−2] > l[j−1]`, swap those two as well. (One step back only; this
   is not a full sort.)
6. Spacing pass, j = 1..10: if `l[j] − l[j−1] < 0.01`:
   - j = 1: `l[1] = 0.01`;
   - otherwise, with `s = l[j−1] + l[j]`: `l[j] = (0.01 + s)·0.5` and
     `l[j−1] = (s − 0.01)·0.5`.
7. `l[0] = 0.0`.

Units: 0.5 = half the sample rate (4 kHz).

**Q16 path** (used only for the spectral envelope of the coefficient block).
1. `q[j+1] = s16((D3[i3][j] + 2·D2[i2][j] + 4·D1[i1][j]) >> 2)`, with the
   int16 codebooks `D1` (Q16), `D2` (Q17), `D3` (Q18) (derivable from `C1..C3`,
   see [Tables](#tables)).
2. Upper-half limit, j = 10 down to 6: if `q[j]` is negative as an int16
   (that is, the value reached 0.5 and wrapped), `q[j] = s16(lim)`. Then
   `lim = q[j] − 655`. `lim` starts at 32112.
3. Lower-half limit, j = 1 up to 5: if `q[j] < 0`, `q[j] = s16(lim)`. Then
   `lim = q[j] + 655`. `lim` starts at 655.
4. `q[0] = 0`; the same one-step sorting pass as the double path (signed
   compares).
5. If `q[1] < 655`, `q[1] = 655`.
6. Spacing pass, j = 2..10: with `a = q[j]`, `b = q[j−1]`: if `a − b < 655`,
   then `q[j] = s16((b + 656 + a) div 2)` and `q[j−1] = s16((a + b − 654) div 2)`.
   (656 and −654 are not symmetric; that is what the code does.)
7. `q[0] = 0`.

The stage-1 index `i1` of each set is remembered (it selects the allocation
table row).

### Pitch parameters

For each pitch field read: lag `L`; if `L ≠ 0`, index `p` gives double taps
`(b0, b1, b2) = PT[p]` and Q15 taps `PQ[p]`. If `L = 0`, both are zero.

## Coefficient block

A block decodes the M = N/2 spectral coefficients `X[0..M−1]` of one
transform of type t, for parameter slot `k` (the LSP and pitch slot it uses):

| call | slot k | type t |
|---|---|---|
| mode 0, half 0 | 1 | 0 |
| mode 0, half 1 | 2 | 0 |
| mode 1 | 2 | 1 |
| mode 2, first | 1 | 0 |
| mode 2, second | 2 | 2 |
| mode 3 | 2 | 3 |

### Gains

- `G = GAIN[global index]` (128 doubles).
- `bg[b] = BG2[index2][b] + BG1[index1][b]` for bands b = 0..7 (two 64 × 8
  double codebooks). If `bg[b] < 1e−05`, `bg[b] = 1e−05`.
- `pg = double(PQ_k[1]) · 3.0517578125e−05` (the Q15 middle tap of slot k as a
  double; 0 when the slot's lag is 0).

### Spectral envelope (integer)

The envelope ranks the coefficients; it must be reproduced exactly, because
ranking decides which coefficient receives which value.

**Step 1: Q16 LSPs to Q-scaled LPC coefficients** (from slot k's `q[0..10]`).
- Cosines: for j = 0..10, with `e = q[j]·2^11` (32-bit), `f = (e & 0xFFFF) >> 1`,
  `i = e >> 16`, `u = (i + 512) mod 2048`:
  `c[j] = s16(((32768 − f)·S[u] + S[u+1]·f) >> 15)`, where `S` is the 2,049-entry
  int16 sine table of period 2048.
- Define `mul(X, c) = 2·(((lo·c) >> 15) + 2·hi·c)` with `lo = X & 0xFFFF`
  (unsigned) and `hi = X >> 16`.
- Start: `Q[0] = −2^23`, `Q[1] = 2^23`, `P[0] = −2^23`, `P[1] = −2^23`, all
  others 0.
- For i = 1..5, with `a = c[2i]` and `b = c[2i−1]`:
  - `TQ[1] = mul(Q[0], a)`, `TP[1] = mul(P[0], b)`;
  - for m = 2..i: `TQ[m] = mul(Q[m−1], a) − Q[m−2]`,
    `TP[m] = mul(P[m−1], b) − P[m−2]`;
  - for m = 1..i: `Q[m] = Q[m] − TQ[m]`, then `Q[2i+1−m] = −Q[m]`;
    `P[m] = P[m] − TP[m]`, then `P[2i+1−m] = P[m]`.
- `R[j] = −(P[j] + Q[j])` for j = 0..10.
- Normalise: let `A` = bitwise OR of `|R[j]|`. Let `d` = the number of left
  shifts (0..16, stop at 16) after which bit 30 of A is set.
- `a16[j] = s16((R[j] + 2^(15−d)) >> (16 − d))`, j = 0..10.

**Step 2: autocorrelation of `a16`** (with a split accumulator).
- `lo = Σ (a16[i]² & 0x7FFF)`, `hi = Σ (a16[i]² >> 15)`, i = 0..10.
  Then `l = lo & 0x7FFF`, `h = hi + (lo >>> 15)`.
- `s` = 19 − (the number of doublings of `|h|` after which bit 18 is set,
  0..19); `s` = 0 if that never happens.
- Re-split with `x = s − 10`:
  - x > 0: `l = l | ((h & (2^x − 1)) << 15)`; if `s − 14 > 0`,
    `l = l >>> (s − 14)`; `h = h >> x`.
  - x < 0: `h = (h << −x) | (l >> (s + 5))`; `l = l & (2^(s+5) − 1)`.
- `E0 = h + 2`, `E1 = l`.
- For lag m = 1..10: `r[m] = s16(Σhi + (Σlo >>> s))` where, over
  i = 0..10−m, `p = a16[i]·a16[i+m]`, `Σlo += p & (2^s − 1)`,
  `Σhi += p >> s`.

**Step 3: power spectrum at M bins** (k = 0..M−1).
- Table: if N mod 3 = 0, the int16 sine table of period 1536 with step
  `1536 div N`; otherwise the period-2048 table with step `2048 div N`.
  Write `T(p) = table[step·(p mod N)]`, which is sin(2πp/N) in Q15.
- `accH = E0`, `accL = E1`; for m = 1..10, with `p = N/4 + m·k`:
  `v = r[m]·T(p)`, `accL += v & 0x7FFFF`, `accH += v >> 19`.
- **Lag 0** (slot k's lag): `w[k] = (accH << 13) + (accL >>> 6)`.
- **Lag L ≠ 0**: with `gq = s16(trunc(32768·pg))` (the Q15 tap), `c = T(N/4 +
  L·k)`, `f = (gq·((gq − 2c) >> 2) + 2^28) >> 15`:
  `w[k] = (((accL & 0x7FFFF) >> 4)·f >>> 14) + ((accH + (accL >>> 19))·f)·2`.
  (The first product is taken as a 32-bit pattern and shifted logically.)
  If w is negative the DLL prints an "underflow" message and continues.

`w[k]` approximates |A(e^jω)|²·|1 − g·e^−jωL|²: **small w = strong
spectrum = important**.

### Ranking

For each band b (coefficients `s + b·W ... s + b·W + W − 1`), each coefficient
`x` at position `j` inside the band gets the key `(w[x] & ~0x7F) | j`
(a 32-bit signed value; keys inside a band are unique). The band's coefficients
are sorted by **ascending** key. Any correct sort gives the same order.

### Bit allocation

Input: `B = β − 19`, type t, and the stage-1 LSP index `i1` of slot k.
Slot-1 quirk: if slot 1's stored index is −1, use slot 2's. In mode 2 the slot-1
index is **not** refreshed, so it is whatever the last mode-0 frame left (−1
after F = 0, set A's `i1` after F = 1, 0 after `InitDecoder`).

1. `adj = s16(((B − ref[t])·sc[t]) >> 9)`.
2. For each band b: `x = AB[i1][b] + adj` (AB = 64 × 8 int16 allocation table)
   and split x into counts `n4, n2, n1` (coefficients coded with 4, 2 and 1 bit
   each) for band width W:
   - `u = ((20480 − (x >> 1))·x) >> 14`
   - `n2 = ((((u·u) >> 15)·29127 >> 13)·W) >> 15`
   - `n4 = ((((x·x) >> 15)·29127 >> 15)·W) >> 15`
   - `n1 = ((x·W) >> 13) − (4·n4 + 2·n2)`
   - if `n1 > (W − n4) − n2`: `n1 = 0`, `n4 = ((x − 16384)·W + 8192) >> 14`,
     `n2 = W − n4`.
3. Sums `S4, S2, S1` over the bands.
4. **S4 even:** if S4 is odd, take the last band (from band 7 down) with
   `n4 > 0`: `n4 −= 1`, `n2 += 2` (S4 −= 1, S2 += 2).
5. **S2 multiple of 4:** let `ρ = S2 mod 4`. If ρ ≠ 0:
   - if `B ≤ 4·S4 + 2·S2 + S1`: from band 7 down, for each band with
     `n2 > 0`: `n2 −= 1`, `n1 += 1`, ρ −= 1; stop at ρ = 0;
   - else: ρ = 4 − ρ; from band 0 up, for each band with `n1 > 0`:
     `n1 −= 1`, `n2 += 1`, ρ −= 1; stop at ρ = 0.
   (Each band is visited once and changed by one step.)
6. **Fill to a byte:** `T42 = 4·S4 + 2·S2` (after step 5),
   `ε = 8·(B div 8) − (T42 + S1)`:
   - ε < 0: repeat passes from band 7 down, taking one `n1` from each band
     with `n1 > 0`, until ε = 0 (the DLL would loop forever if every `n1`
     were 0);
   - ε > 0: one pass from band 0 up: capacity `κ = W − (n1 + n2 + n4)`; if
     κ > 0, add `min(κ, (ε + 1) div 2)` to `n1` and subtract it from ε; stop
     at ε = 0.
7. **Signs:** `ρs = B − (T42 + S1)` (T42 from step 6, S1 after it). From band
   0 up: `κ = W − (n1 + n2 + n4)`; if κ < 1 the band gets 0 sign
   coefficients, else it gets `ns = min(κ, ρs)` and `ρs −= ns`; stop after the
   band where ρs reaches 0 (later bands get 0).
8. Counts: `K0 = S4 div 2`, `K1 = S2 div 4`, `K2 = S1 div 8`, `Ks = Σ ns`.

### Filling the coefficients

- Read the index lists (field order in [Bitstream](#bitstream)).
- Three VQ streams, one per class c ∈ {2-dim, 4-dim, 8-dim}, each with a
  current vector (starting at the class's first index) and a position inside
  it (starting at 0). Vectors run on **across bands**.
- For each band b = 0..7, in the band's sorted order:
  1. the first `n4` coefficients take consecutive values from the 2-dim
     stream, the next `n2` from the 4-dim stream, the next `n1` from the 8-dim
     stream (a stream moves to its next vector when its position reaches the
     dimension);
  2. the next `ns` coefficients take `+0.6` (sign bit 0) or `−0.6` (sign bit
     1), consuming sign bits in order;
  3. the rest of the band is **noise**: with the persistent noise position
     `z` (0..1023), `x = ((NB[z]·0.9)·pg) + ((NA[z]·(1.0 − pg))·0.9)`, then
     `z = (z + 1) mod 1024`. `z` carries over to the next band, block and
     **frame**.
- Then `X[0..s−1] = 0` and `X[e..M−1] = 0`.
- Scale every coded coefficient of band b: `X = (X·bg[b])·G`.

## Synthesis

### Inverse transform (N = 2M outputs from M coefficients, overlap parameter Λ)

Let h = N/2 (= M), q = N/4, `T` the double sine table of the transform (period
2048 for N = 512 and 1024, period 1536 for N = 768) and `σ` its stride
(4 for N = 512, 2 for 1024, 2 for 768), so `T[kσ] ≈ sin(2πk/N)`.

1. **Pre-twiddle** into complex `z[i] = re[i] + j·im[i]`, i = 0..h−1:
   - i < q: `re[i] = X[2i]·T[(q−i)σ]`, `im[i] = X[2i]·T[iσ]`;
   - i ≥ q: `re[i] = X[N−1−2i]·T[(i−q)σ]`, `im[i] = X[N−1−2i]·T[(N−i)σ]`.
2. **Complex FFT of size h** (positive exponent, scaled by 1/h), described
   exactly below.
3. **Post-twiddle** with `w[i] = sin((2i+1)·π_c/(2N))` (`π_c` =
   3.14159265359), i = 0..h−1:
   `Y[N − Λ/2 − 1 − i] = (((−re[i])·w[h−1−i]) + (im[i]·w[i]))·2.0`.
4. **Fold:** `Y[N − Λ/2 + j] = Y[N − Λ/2 − 1 − j]` for j < Λ/2, then
   `Y[j] = −Y[N − Λ − 1 − j]` for j < (N − Λ)/2.

**FFT, size n = 256 or 512 (radix 2).** Arrays `re, im`; sine table of period
2048, `S[k]`, cosine `S[k + 512]`.
- Stages: `size = n`, `stride = 2048 / n`. While size > 4: `half = size/2`; for
  k = 0..half−1: `ws = S[k·stride]`, `wc = S[k·stride + 512]`; for every
  `a = k, k + size, ...` below n, with `b = a + half`:
  - `re[a] = re[a] + re[b]`; `im[a] = im[b] + im[a]`;
  - `re[b] = ((re_a − re_b)·wc) − ((im_a − im_b)·ws)`;
  - `im[b] = ((im_a − im_b)·wc) + ((re_a − re_b)·ws)`;
  (`re_a` etc. are the values before this butterfly). Then `stride *= 2`,
  `size = half`.
- Last stage on each group of four `r0..r3`, `i0..i3`, with `c = 1.0 / n`
  (n as a double; for the radix-3 case below this is the full size 384):
  - `re0 = ((r3 + r1) + (r2 + r0))·c`, `re1 = ((r2 + r0) − (r3 + r1))·c`
  - `re2 = ((r0 − r2) − (i1 − i3))·c`, `re3 = ((i1 − i3) + (r0 − r2))·c`
  - `im0 = ((i1 + i3) + (i0 + i2))·c`, `im1 = ((i0 + i2) − (i1 + i3))·c`
  - `im2 = ((i0 − i2) + (r1 − r3))·c`, `im3 = ((i0 − i2) − (r1 − r3))·c`
- Bit reversal: the classic in-place permutation (for i = 0..n−2, swap i and j
  when i < j; then subtract halves from j while the half ≤ j, and add the
  first half that is not).

**FFT, size 384 (one radix-3 stage, then three radix-2 FFTs of 128).**
- `m = 128`, `s3 = sqrt(3.0)·0.5`, sine table of period 1536 `U[k]`, cosine
  `U[k + 384]`, twiddle step 4.
- For j = 0..m−1 with `x0, x1, x2 = re[j], re[j+m], re[j+2m]` and `y0, y1, y2`
  likewise from `im`, and `k = 4j`:
  - `re[j] = x0 + (x2 + x1)`, `im[j] = y0 + (y2 + y1)`
  - `α = x0 − (0.5·(x2 + x1))`, `β = ((y2 + y1)·0.5) + (−y0)`,
    `γ = (x1 − x2)·s3`, `δ = (y1 − y2)·s3`
  - `u1 = α − δ`, `v1 = β − γ`, `u2 = δ + α`, `v2 = γ + β`
  - `re[j+m] = (U[k]·v1) + (U[k+384]·u1)`, `im[j+m] = (u1·U[k]) − (v1·U[k+384])`
  - `re[j+2m] = (U[2k]·v2) + (U[2k+384]·u2)`, `im[j+2m] = (u2·U[2k]) − (v2·U[2k+384])`
- Each third (offset 0, m, 2m) then goes through the radix-2 algorithm above
  with n = 128 (stage twiddles from the period-2048 table, stride 16), but the
  last-stage scale is `1.0 / 384`.
- Reorder: `out[3j] = first third[j]`, `out[3j+1] = second[j]`,
  `out[3j+2] = third[j]`, for re and im separately.

### Overlap-add

`O` is the persistent 512-sample overlap buffer.

**Plain** (length ℓ, carry κ, window `v`): `out[i] = (O[i]·v[ℓ−1−i]) + (Y[i]·v[i])`
for i < ℓ; then `O[0..κ−1] = Y[ℓ..ℓ+κ−1]`.

**Shaped** (mode 0 halves). Window `v` = the 512-point sine window,
`v2` = its square (separate tables, see [Tables](#tables)).

Shape flags and 8-point shapes are **separate** state, each with three entries
(previous, half 0, half 1):
- mode 0 reads both half flags and sets each half's shape to the table row
  (flag 1) or to all-ones (flag 0);
- mode 2 sets both half flags to 0 and both half shapes to all-ones;
- mode 3 sets both half shapes to all-ones but **leaves the flags**;
- mode 1 changes neither;
- at the end of every frame, previous flag = half-1 flag and previous shape =
  half-1 shape.

So a mode-0 frame after a mode-3 or mode-1 frame can see a previous flag of 1
with an all-ones previous shape; the flags choose the formula, the shapes give
the values. For half 0, (`fp`, `Ap`) = previous and (`fc`, `Ac`) = half 0; for
half 1, (`fp`, `Ap`) = half 0 and (`fc`, `Ac`) = half 1.
- `fp` = 0 and `fc` = 0: `out[j] = (O[j]·v[256+j]) + (v[j]·Y[j])`, j < 256.
- Otherwise, for quarter-block r = 0..3 (samples j = 64r..64r+63):
  `ρ1 = Ap[7−r] / Ap[4+r]`, `ρ2 = Ac[3−r] / Ac[r]`,
  `out[j] = ((O[j]·(Ap[7−r]·v[256+j])) + ((Ac[3−r]·v[j])·Y[j])) / ((v2[256+j]·ρ1) + (v2[j]·ρ2))`.
- Then `O[0..255] = Y[256..511]`.

### Frame layout (excitation `x[0..511]`)

| mode | transforms (type, N, Λ) | overlap-add |
|---|---|---|
| 0 | (0, 512, 256) for half 0 → `x[0..255]`; again for half 1 → `x[256..511]` | shaped, each half |
| 1 | (1, 768, 256) | plain, ℓ = 512, κ = 256, 1024-point sine window |
| 2 | (0, 512, 256) → `x[0..255]`; then (2, 768, 512) → `x[256..511]` | plain, ℓ = 256, κ = 256, 512-point window; then plain, ℓ = 256, κ = 512, 512-point window |
| 3 | (3, 1024, 512) | plain, ℓ = 512, κ = 512, 1024-point window |

### Long-term (pitch) predictor

A 768-sample buffer `H`: `H[0..255]` = the last 256 output samples of this stage
from earlier frames, `H[256..767]` = this frame. The frame is split into
sections, each with a lag and taps:

| mode | sections (length: lag slot) |
|---|---|
| 0, 2 | 128: slot 0, 256: slot 1, 128: slot 2 |
| 1, 3 | 256: slot 0, 256: slot 2 |

Slot 0 is the previous frame's slot 2: **the first section of a frame uses the
previous frame's last pitch.**

For a section with lag L and taps `(b0, b1, b2)`, for each sample n (index into
H) in order:
- L = 0: `H[n] = x[n − 256]`.
- L ≠ 0: `H[n] = (((b2·H[n−L−1]) + x[n−256]) + (b1·H[n−L])) + (H[n−L+1]·b0)`.

With L = 1 the last term reads `H[n]` before it is written, which holds that
position's value **from the previous frame** (see the stale buffer in
[State](#state-between-frames)).

Before a section with L ≠ 0, the taps are **checked for stability**; if the
check fails the taps become `(0, b1, 0)`:
- Build `c[0..L+1]`: `c[0] = 1.0`, zeros, then `c[L−1] = −b0`, `c[L] = −b1`,
  `c[L+1] = −b2` (written in that order, so L = 1 overwrites `c[0]`).
- `r[j] = floor((c[j]·0.125)·32768.0)·3.0517578125e−05`, j = 0..L+1.
- For m = L+1 down to 1: if `|r[m]| > 0.1225` → **unstable**. Otherwise
  `κ = r[m]·8.0` and, for j = 0..m, `r'[j] = floor(((r[j] − ((r[m]·r[m−j])·8.0)) / (1.0 − (κ·κ)))·32768.0)·3.0517578125e−05`;
  then `r = r'`.
- Stable if the loop finishes.

### LPC synthesis

The all-pole filter `1/A(z)`, `A(z) = 1 + Σ a_m z^−m` (order 10), runs on
`H[256..767]` with a 10-sample memory (the previous frame's last 10 outputs).
For each output sample: `y = e`; then for m = 1..10 in order,
`y = y − (y[n−m]·a_m)`.

**Coefficient interpolation.**
- Every frame except mode 0 with F = 1: K = 16 steps over 512 samples from
  slot 0 (the previous frame's end LSPs) to slot 2. For step k = 0..16,
  `t = k·(1.0/16)`; the block length is 16 samples for k = 0 and k = 16 and
  32 otherwise.
- Mode 0 with F = 1: two runs of 8 steps over 256 samples, slot 0 → slot 1,
  then slot 1 → slot 2 (block lengths 16, 32, …, 32, 16).
- For each step: `l[j] = (lA[j]·(1.0 − t)) + (lB[j]·t)`, j = 0..10, then
  convert to `a` (below) and filter the block.

**LSPs to LPC (double).** `C[j] = fcos((l[j−1]·π_c)·2.0)` for j = 1..11 (with
`π_c` = 3.14159265359 and `fcos` as defined in [Arithmetic](#arithmetic)). Start
`P[0] = −1`, `P[1] = −1`, `Q[0] = −1`, `Q[1] = 1`, the rest 0. For i = 2, 4, 6,
8, 10, with `u = C[i]` and `v = C[i+1]`:
- `TQ[1] = (v·2.0)·Q[0]`, `TP[1] = (u·2.0)·P[0]`;
- for m = 2..i/2: `TQ[m] = ((Q[m−1]·v)·2.0) − Q[m−2]`,
  `TP[m] = ((P[m−1]·u)·2.0) − P[m−2]`;
- for m = 1..i/2: `Q[m] = Q[m] − TQ[m]`, then `Q[i+1−m] = −Q[m]`;
  `P[m] = P[m] − TP[m]`, then `P[i+1−m] = P[m]`.

Then `a_m = (Q[m] + P[m])·(−0.5)`, m = 0..10 (`a_0` = 1).

### Output

For each of the 512 filter outputs y: `v = floor(y + 0.5)`; 32767 if v > 32767;
−32768 if v < −32768; otherwise v. (The DLL converts the already-integral value
exactly.)

### End of frame

1. `H[0..255] = H[512..767]`. `H[256..767]` is **not cleared**.
2. Filter memory = the last 10 outputs y (before rounding).
3. Slot 2 becomes slot 0 for LSPs (double and Q16), lag, taps (double and Q15),
   the half-1 shape and the half-1 flag.
4. Skip to the frame's bit budget.

## State between frames

| State | Size | `InitDecoder` | `ResetDecoder` |
|---|---|---|---|
| LSP slot 0 (double) | 11 | `j·(0.48/11)`, j = 0..10 | same |
| LSP slot 0 (Q16) | 11 | `s16(floor(l·65536.0))` of the above | same |
| LSP slots 1, 2 | 11 each | (always recomputed before use) | not touched |
| lag slot 0, taps slot 0 (double) | | 0 | 0 |
| taps slot 0 (Q15) | 3 | never read | not touched |
| lags / taps slots 1, 2 | | (always read before use) | not touched |
| shape flags, previous/current | 3 | 0 | 0 |
| shapes, previous/current | 3 | all-ones | all-ones |
| stage-1 LSP index of slot 1 | 1 | uninitialised heap (0 observed) | not touched |
| noise position z | 1 | 0 | **not touched** |
| filter memory | 10 | 0 | 0 |
| pitch buffer H[0..255] | 256 | 0 | 0 |
| pitch buffer H[256..767] | 512 | heap (0 observed) | **not touched** |
| overlap buffer O | 512 | 0 | 0 |

The reader's byte buffer holds only the current frame; nothing else carries over.

## Arithmetic

- **Doubles everywhere.** The decoder never stores a float32 or an 80-bit
  value; every stored real is a double. The x87 unit runs at its default
  53-bit precision (the DLL never changes the control word), so each
  `+ − × ÷` and `sqrt` rounds like binary64. The extended exponent range only
  matters for overflow or subnormals, which did not occur in any test.
- **fcos (per frame, LSP → LPC).** The x87 `fcos` result has a 64-bit mantissa,
  then the store rounds it to 53 bits. It is **not** always the correctly
  rounded cosine: on 20,151 test arguments a correctly rounded double cosine
  differed in 102 cases, and the Windows CRT `cos` in 761. The exact emulation,
  which matched all 20,151: reduce the argument by the nearest multiple of π/2
  using **π rounded to 66 bits**, evaluate cos/sin of the remainder with ≥ 128
  bits, round to a 64-bit mantissa, then to a double.
  - Practical impact: with the CRT `cos` (and with plain-double window tables),
    the research model still produced **identical PCM on all 20 recordings and
    all vectors**. So the exact emulation is only needed for strict
    double-level identity; build it anyway (double-double arithmetic is enough)
    and let the vectors decide.
  - **The emulation is a model, not the hardware.** Measured later (an Intel
    Core i5-9600K, 2026-09-29): against a quad-precision cosine with the same
    66-bit-π reduction, the CPU's 64-bit `fcos` result is off by up to 0.69 of
    a 64-bit ulp (2.8% of arguments are not the correctly rounded 64-bit
    value). After the store to a double that changes about **9 in 1,000,000**
    arguments in [0, π]; the LPEC SP work met one in A-006 (argument
    0.9881057607923576: the CPU gives 0x1.19bd5177382d8p-1, the emulation one
    ulp less). Such a difference stays in the decoder state for a while but is
    around 1e-10 of a sample: no PCM sample differed in any SP or LP test.
    With the CPU's own `fcos` swapped in, every double matched the DLL. Since
    the DLL uses whatever CPU it runs on, its exact doubles can differ between
    CPUs; OpenEVP keeps the deterministic emulation so its output (and the
    marks keyed by it) is the same on every machine.
- **Rounding to integers.** The decoder calls `floor` (not `__ftol`, no
  rounding-mode changes):
  - output samples: `floor(y + 0.5)`, then clamped;
  - pitch stability check: `floor` on the Q15 grid;
  - the envelope's `trunc(32768·pg)` is a truncating conversion (exact here,
    because `pg` is a Q15 value).
- **Integer parts** (Q16 LSPs, envelope, allocation) use 32-bit wrap-around,
  arithmetic shifts except where `>>>` is written, and C division.
- **Operation order** is given in every formula above. Two traps found during
  the mapping, where a decompiler's reading was wrong: the pitch predictor sum
  and the FFT's last-stage sums. Both are written here as the machine code
  evaluates them.

## Tables

"Derivable" tables must be generated from the formula (exactly as written);
the others have to be extracted from `LPEC.dll` and are **not in this
repository** (`tools/import_lpec_tables.py` turns local dumps of them into the
app's data file). Addresses are virtual addresses in the
DLL (image base 0x10000000).

### Static tables (in the DLL image)

| Name | Address | Size × type | Purpose | Derivable? |
|---|---|---|---|---|
| LSP codebook stage 1 `C1` | 0x10024440 | 64 × 10 double | LSP VQ | no, extract |
| LSP codebook stage 2 `C2` | 0x10025840 | 64 × 10 double | LSP VQ | no, extract |
| LSP codebook stage 3 `C3` | 0x10026c40 | 64 × 10 double | LSP VQ | no, extract |
| LSP codebook stage 1 Q16 `D1` | 0x10023040 | 64 × 10 int16 | envelope path | **yes**: `rhu(C1·65536)` (verified, 0 differences) |
| LSP codebook stage 2 Q17 `D2` | 0x10023540 | 64 × 10 int16 | envelope path | **yes**: `rhu(C2·131072)` |
| LSP codebook stage 3 Q18 `D3` | 0x10023a40 | 64 × 10 int16 | envelope path | **yes**: `rhu(C3·262144)` |
| pitch taps `PT` | 0x10037d98 | 64 × 3 double | LTP taps | no, extract |
| pitch taps Q15 `PQ` | 0x10037bf6 | 64 × 3 int16 | envelope, noise | **yes**: `rhu(PT·32768)` (verified) |
| temporal shapes | 0x10040960 | 128 × 8 double | mode-0 shaped overlap | no, extract |
| global gain `GAIN` | 0x100394d0 | 128 double | block gain | no, extract (not a clean geometric series) |
| band gain stage 1 `BG1` | 0x1003c0d0 | 64 × 8 double | band gains | no, extract |
| band gain stage 2 `BG2` | 0x1003d0d0 | 64 × 8 double | band gains | no, extract |
| VQ, 2-dim | 0x10030440 | 256 × 2 double | coefficients | no, extract |
| VQ, 4-dim | 0x10031440 | 256 × 4 double | coefficients | no, extract |
| VQ, 8-dim | 0x10033440 | 256 × 8 double | coefficients | no, extract |
| allocation base `AB` | 0x100388b0 | 64 × 8 int16 | bits per band by LSP stage-1 index (6000 bit/s) | no, extract |
| allocation reference R | 0x10038cb0 | 1 int16 (= 128) | see constants | a constant |
| noise A `NA` | 0x10044970 | 1024 double | noise fill (unvoiced part) | no, extract (random-looking, uniform in ±1) |
| noise B `NB` | 0x10042970 | 1024 double | noise fill (voiced part) | no, extract |
| int16 sine, period 2048 `S` | 0x10046f10 | 2049 int16 | envelope | **yes**: `clamp(rhu(32768·sin(2πi/2048)), −32767, 32767)`, i = 0..2048 (verified) |
| int16 sine, period 1536 | 0x10047f10 | 1537 int16 | envelope (N = 768) | **yes**: same with 1536 (verified) |
| default shape | 0x10046a00 | 8 double | all 1.0 | trivial |
| zero taps | 0x10049bb0 / 0x10049bc8 | 3 double / 3 int16 | lag 0 | trivial |

The allocation base and the noise tables might be computable from the encoder's
design (for example from the LSP codebook), but no formula was found; treat
them as data.

### Tables generated at run time (the DLL computes them on the first `InitDecoder`)

All derivable; the formulas below reproduce the DLL's values **bit for bit**
only when the x87 intermediate precision is emulated (a 64-bit-mantissa
`fsin`/`fcos` result, then 53-bit rounding of each following operation).
Plain double evaluation differs in the last bit for some entries (see
[Arithmetic](#arithmetic) for why that was harmless in practice).

| Name | Address | Size | Formula (i = 0..size−1) | Exact with x87 emulation |
|---|---|---|---|---|
| 512-point sine window `v` | 0x1004a040 | 512 | `sqrt((1 − fcos(((i + 0.5)·6.28318530718)·(1/512)))·0.5)` | yes, 512/512 |
| its square `v2` | 0x1004b040 | 512 | `(1 − fcos(same))·0.5` | yes |
| 1024-point sine window | 0x1004f8a0 | 1024 | as `v` with 1/1024 | yes |
| FFT sine, period 2048 | 0x10062940 | 2048 | `fsin(i·(6.283185307/2048))` | yes, with the 66-bit-π reduction (2 entries need it) |
| FFT sine, period 1536 | 0x10066940 | 1536 | `fsin((2i)·(3.1415926535/1536))` | yes, with the 66-bit-π reduction (1 entry needs it) |
| post-twiddle, N = 512 / 768 / 1024 | 0x1004d040 / 0x1004e040 / 0x100598a0 | N/2 | `fsin((2i + 1)·(3.14159265359/(2N)))` | yes |
| initial LSPs | 0x1004f840 | 11 | `i·(0.48/11)` | yes (plain double) |

In each formula the products are evaluated left to right with 53-bit rounding;
`1 − fcos(…)` uses the 64-bit `fcos` value before rounding (that is where plain
double code differs).

## LPEC SP (16000 Hz)

The ICD-ST10's SP mode (folder-table mode byte 0x20, .dvf codec 0x2A) is the
same decoder after `InitDecoder(16000, 16000)` (DVE's `lpecde.ax` calls
`LPEC::InitCodec(16000, 16000, 0)`, which gives identical PCM). Everything
above applies with the constants below; this section lists every place where
the value, the loop bound or the table differs from LP. Written 2026-09-29
from the same DLL and confirmed against it (`openevp.decoders.sony_lpec` with
`config.SP` is the implementation).

### Status (SP)

| Test | Result |
|---|---|
| real ICD-ST10 SP recording A-006 (131 frames, 8.384 s) | PCM byte-identical to Sony's decode (100% of samples), pure Python and C core |
| `tests/vectors/lpec_sp/*` (12 short synthetic vectors, Sony-encoded, plus random frames) | byte-identical to the DLL's PCM |
| `tests/vectors/lpec_sp/long-mixed-1min` (938 frames) | SHA-256 of the DLL's PCM |
| 6,000 random frames of all four modes (with truncated frames and `ResetDecoder` calls), 6,000 damaged real frames (bit flips, random bytes, changed mode bits, truncation, resets), 1,121 encoder frames | every sample and the reader's byte count identical, frame by frame, pure Python and C core |
| A-006, the encoder frames, 3,000 random and 3,000 damaged frames with the host CPU's own `fcos` in place of the emulation (research only) | every double of the pre-rounding output bit-identical in every frame |

With the emulated `fcos` the PCM is identical everywhere; a few doubles differ
wherever this CPU's `fcos` is not correctly rounded (see
[Arithmetic](#arithmetic)).

### Configuration constants (both modes)

`InitDecoder(rate, bitrate)` derives:

| Name | LP (8000, 6000) | SP (16000, 16000) | Rule |
|---|---|---|---|
| frame F | 512 | 1024 | 1024 if rate ≥ 11026 else 512 (4096 above 22050, unused) |
| order | 10 | 16 | 16 if rate > 8000 |
| bands | 8 | 10 | 10 if rate > 8000 |
| LSP stages × bits | 3 × 6 | 4 × 6 | 4 stages if rate > 8000 |
| pitch lag bits | 7 | 8 | 7, +1 if (rate div 8000)·120 > 128 |
| budget X | 384 | 1024 | `32·((F·bitrate + 31) div (rate·32))`; by mode `X·{1, 3/4, 5/4, 1}`: SP frames are 128, 96, 160, 128 bytes |
| N by type t | 512, 768, 768, 1024 | 1024, 1536, 1536, 2048 | `F, 3F/2, 3F/2, 2F` |
| overlap Λ by type | 256, 256, 512, 512 | 512, 512, 1024, 1024 | `F/2, F/2, F, F` |
| coded band top | 3500 Hz | 7500 Hz | 3500 if rate ≤ 8000 and 0 < bitrate ≤ 6000, else `rate·15 div 32` |
| unit | 14 | 12 | `floor(128 / (bands·(rate/2)) · top)` |
| W[t] | 28, 42, 42, 56 | 48, 72, 72, 96 | `(M div 128)·unit` |
| s[t] | 2, 3, 3, 4 | 2, 3, 3, 4 | `rhu(c·M / (rate/2))`, at least 1; c = 32 in the narrow (LP) case, else 30 |
| e[t] | 226, 339, 339, 452 | 482, 723, 723, 964 | `s + bands·W` |
| R | 128 | 432 | a table constant (0x10038cb0 / 0x100388a0) |
| ref[t] | 128, 192, 192, 256 | 432, 648, 648, 864 | `R·{1, 1.5, 1.5, 2}` |
| sc[t] | 18725, 12483, 12483, 9362 | 8738, 5825, 5825, 4369 | `rhu(2^22 / (bands·W))` |

The global gain (7 bits, 128 entries), band-gain indices (6 + 6 bits), shape
index (7 bits, 128 × 8 shapes), pgidx (6 bits, 64 × 3 taps), VQ indices (8
bits; 2-, 4- and 8-dimensional codebooks), the 19 gain bits, the noise tables
and the pitch history (256 samples) are the same shape in both modes.

### What changes with the constants

- **Frame layout.** `Decode` outputs F = 1024 samples. Mode 0 halves are
  F/2 = 512 samples; mode 2's first block covers F/2. The field order is
  LP's, with 4-index LSP sets (24 bits), 8-bit lags and SP's budgets: mode 0's
  `r = 1024 − used`, mode 1 `768 − used`, mode 2 `(1024 − used) div 2`, then
  `1280 − used`, mode 3 `1024 − used`.
- **LSP decoding, double path.** `l[j+1] = ((C3[i3][j] + C4[i4][j]) + C2[i2][j]) + C1[i1][j]`,
  j = 0..15. The upper-half limit runs j = 16 down to 9, the lower-half limit
  j = 1 up to 8 (in general: order down to order/2 + 1, and 1 up to order/2);
  the sorting and spacing passes run j = 1..16. The constants (0.5, 0.49,
  0.01) are unchanged.
- **LSP decoding, Q16 path.** `q[j+1] = s16(((D2[i2][j] + D3[i3][j] + D4[i4][j]) >> 3) + D1[i1][j])`
  (a sum of three, an arithmetic shift, then D1 added; the whole stored to an
  int16), with `D1 = rhu(C1·65536)` (Q16) and `D2..D4 = rhu(Ck·524288)` (Q19),
  verified against the DLL's copies. Limits, sort and spacing as in the double
  path, bounds by order; constants (32112, 655, 656, 654) unchanged.
- **Interpolated slot 1** (both paths) and the end-of-frame copies run over
  the 17 values.
- **Spectral envelope.** Step 1 runs i = 1..8 (order/2) over 17 cosines;
  step 2 sums 17 squares and lags m = 1..16; step 3 sums m = 1..16. The
  int16 sine tables are LP's: period 1536 for N = 1536 (step 1), period 2048
  for N = 1024 and 2048 (steps 2 and 1).
- **Ranking and allocation** run over 10 bands with SP's W, s, e, ref, sc.
  All allocation steps are unchanged (step 4 still takes the last band with
  `n4 > 0`, from band 9 down).
- **Filling.** Unchanged, over 10 bands; the noise position still wraps at 1024.
- **Inverse transform.** The sine table and stride follow N: period 1536 with
  `σ = 1536/N` when N mod 3 = 0 (N = 1536: σ = 1), else period 2048 with
  `σ = 2048/N` (N = 1024: 2, N = 2048: 1). FFT sizes are h = N/2 = 512, 768,
  1024. Sizes 512 and 1024 use the radix-2 FFT as written (stage stride
  `2048/n`). Size 768 uses the radix-3 algorithm with `m = n/3 = 256` and
  twiddle index `k = (1536/n)·j = 2j` (384 in LP: 4j); the three radix-2 FFTs
  of 256 use stride 8, and the last-stage scale is `1.0 / 768`. The
  post-twiddle `w` is the table for N (N/2 entries).
- **Overlap-add.** `v` is the **1024-point** sine window (LP's long window)
  and `v2` its square; the long window is 2048 points. Shaped (mode 0):
  outputs j < 512 with `v[512 + j]`, `v2[512 + j]`, quarter-blocks r of 128
  samples (j = 128r .. 128r + 127). Plain: mode 1 ℓ = 1024, κ = 512,
  2048-point window; mode 2 (512, 512, 1024-point) then (512, 1024,
  1024-point); mode 3 (1024, 1024, 2048-point). `O` holds 1024 samples.
- **Long-term predictor.** `H` = 256 history samples + 1024; sections are F/4,
  F/2, F/4 (256, 512, 256: slots 0, 1, 2) for modes 0 and 2 and F/2, F/2 (512,
  512: slots 0, 2) for modes 1 and 3; `x[n − 256]` as before. Lags reach 255
  (8 bits; the encoder's range is 240), which the 256-sample history still
  covers. End of frame: `H[0..255] = H[1024..1279]`.
- **LPC synthesis.** Order 16 (`y = y − y[n−m]·a_m`, m = 1..16 in order;
  memory of 16 outputs). Interpolation: 16 steps over 1024 samples, blocks of
  32 (k = 0 and 16) and 64; or, mode 0 with F = 1, two runs of 8 steps over
  512 samples (32, 64, …, 64, 32). LSP → LPC: `C[j] = fcos((l[j−1]·π_c)·2.0)`
  for j = 1..17, the recursion for i = 2, 4, …, 16, `a_m` for m = 0..16.
- **State.** Initial LSPs `i·(0.48/17)`, i = 0..16 (run-time table
  0x100628a0); Q16 copy as in LP. Everything else as in LP, with SP's sizes.

### Tables (SP)

Selected by `InitDecoder` for rate 16000 (FUN_1000df10); none are in this
repository (`tools/import_lpec_tables.py` imports dumps named `sp_*.json` into
`data/lpec_sp_tables.json`).

| Name | Address | Size × type | Derivable? |
|---|---|---|---|
| LSP codebooks `C1..C4` | 0x1001b040, 0x1001d040, 0x1001f040, 0x10021040 | 64 × 16 double each | no, extract |
| int16 LSP codebooks `D1..D4` | 0x10019040, 0x10019840, 0x1001a040, 0x1001a840 | 64 × 16 int16 each | **yes**: `rhu(C1·2^16)`, `rhu(Ck·2^19)` (0 differences) |
| pitch taps `PT` | 0x100375e8 | 64 × 3 double | no, extract |
| pitch taps Q15 `PQ` | 0x10037446 | 64 × 3 int16 | **yes**: `rhu(PT·32768)` |
| temporal shapes | 0x1003e910 | 128 × 8 double | no, extract |
| global gain `GAIN` | 0x100390d0 | 128 double | no, extract |
| band gains `BG1`, `BG2` | 0x100398d0, 0x1003acd0 | 64 × 10 double each | no, extract |
| VQ 2-, 4-, 8-dim | 0x10029440, 0x1002a440, 0x1002c440 | 256 × 2 / 4 / 8 double | no, extract |
| allocation base `AB` | 0x100383a0 | 64 × 10 int16 | no, extract |
| allocation reference R | 0x100388a0 | 1 int16 (= 432) | a constant |
| noise A, B | LP's (0x10044970, 0x10042970) | 1024 double each | no, extract (shared) |
| int16 sine tables, FFT sine tables | LP's | | as in LP |

Run-time tables (all verified bit-exact against the DLL's memory with the
x87 emulation): the 1024-point window and its square (0x1004f8a0, 0x100558a0,
LP's formula with 1/1024), the 2048-point window (0x100518a0, the same formula
with 1/2048, computed without storing the square), post-twiddles for N = 1024
(LP's 0x100598a0), 1536 (0x1005b8a0) and 2048 (0x1005e8a0), and the 17
initial LSPs (0x100628a0). The encoder-side tables the SP decoder object also
points at (0x1003e0d0, 0x1003e2d0, the window at 0x100578a0) are not read by
`Decode`.

## Open points

- **Uninitialised state.** The stage-1 LSP index of slot 1 and `H[256..767]`
  start as uninitialised heap memory. They matter only if a stream starts with
  mode-2 frames before any mode-0 frame (index) or uses lag 1 in the first
  frame (buffer). The harness always saw zeros; use 0.
- **Paths not reached by any test:** a negative sign count in the allocation
  (a block whose budget is smaller than its VQ bits) and a negative envelope
  value (the "underflow" message). The description follows the code there, but
  nothing confirmed it dynamically.
- **Other modes.** 16000 Hz / 16000 bit/s is described in
  [LPEC SP (16000 Hz)](#lpec-sp-16000-hz). Other bitrates and rates (22050 Hz
  and up) are out of scope; `InitDecoder(8000, b)` with b ≠ 6000 selects a
  different allocation table (0x10038cc0) and budgets.
- **LPEC SP:** only one real recording (A-006, mostly mode-0 and mode-3
  frames) was available; modes 1 and 2, short lags and unstable pitch filters
  are covered by random and damaged frames against the DLL, and by the
  encoder's own output. DVE's own SP `.dvf` export was not compared (the
  reference is `LPEC.dll` driven the way `lpecde.ax` drives it).
- **Encoder:** not mapped.
