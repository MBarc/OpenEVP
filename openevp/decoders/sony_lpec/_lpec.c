/*
 * LPEC decoder core: the per-frame parameter-to-PCM pipeline in C.
 *
 * An exact port of openevp/decoders/sony_lpec/params.py, synthesis.py, x87.py (fcos) and the
 * per-frame part of decoder.py; see docs/lpec.md. The frame fields come
 * already parsed (openevp/decoders/sony_lpec/bitstream.py, packed by openevp/decoders/sony_lpec/_core.py),
 * and the tables come from openevp/decoders/sony_lpec/tables.py. The
 * configuration (LPEC LP, 8000 Hz; LPEC SP, 16000 Hz: config.py) is passed in
 * with every decode.
 *
 * Bit-exactness rules (docs/lpec.md, "Arithmetic"):
 * - every real is an IEEE double (x64 SSE2); no long double, no FMA
 *   (build with -O2 -ffp-contract=off -fno-fast-math);
 * - every expression keeps the Python code's operation order, which is the
 *   doc's order;
 * - integer code reproduces Python's unbounded ints: int64 throughout,
 *   explicit s16/i32/u32 wrap-arounds, arithmetic (floor) right shifts
 *   (gcc's >> on signed values), Python's non-negative modulo where the
 *   Python code uses %, C division only where it uses cdiv.
 *
 * Build: python tools/build_lpec_core.py. Needs GCC (MinGW-w64 on Windows):
 * the fcos emulation uses GCC's unsigned __int128, which MSVC does not have.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __declspec(dllexport)
#define ABI_VERSION 3

typedef int64_t i64;
typedef unsigned __int128 u128;
typedef __int128 s128;

/* ------------------------------------------------------------------------
 * x87 fcos emulation (openevp/decoders/sony_lpec/x87.py)
 *
 * Reduce the exact input by the nearest multiple of pi/2, pi rounded to 66
 * bits; evaluate sin/cos of the remainder to ~120 bits in fixed point;
 * round to a 64-bit mantissa (ties to even), then to a double (ties to
 * even): the same two roundings the emulation does.
 * ---------------------------------------------------------------------- */

/* pi rounded to 66 bits = PI66 * 2^-64; pi/2 = PI66 * 2^-65. */
static const u128 PI66 = ((u128)3 << 64) | (u128)0x243f6a8885a308d3ULL;

#define FIX 126                        /* fixed point: value = v * 2^-126 */
static const u128 FIX_ONE = (u128)1 << FIX;

/* (a * b) >> 126, a and b < 2^127, truncated. */
static u128 fmul(u128 a, u128 b)
{
    uint64_t a1 = (uint64_t)(a >> 64), a0 = (uint64_t)a;
    uint64_t b1 = (uint64_t)(b >> 64), b0 = (uint64_t)b;
    u128 p00 = (u128)a0 * b0;
    u128 p01 = (u128)a0 * b1;
    u128 p10 = (u128)a1 * b0;
    u128 p11 = (u128)a1 * b1;
    /* 256-bit sum: hi:lo */
    u128 mid = (p00 >> 64) + (uint64_t)p01 + (uint64_t)p10;
    u128 lo = (mid << 64) | (uint64_t)p00;
    u128 hi = p11 + (p01 >> 64) + (p10 >> 64) + (mid >> 64);
    return (hi << (128 - FIX)) | (lo >> FIX);
}

static int bitlen128(u128 v)
{
    int n = 0;
    uint64_t h = (uint64_t)(v >> 64);
    if (h) { n = 64; v = h; }
    uint64_t l = (uint64_t)v;
    while (l) { n++; l >>= 1; }
    return n;
}

/* Round mant * 2^exp (mant > 0) to `bits` significant bits, ties to even. */
static void round_bits(u128 *mant, int *exp, int bits)
{
    int len = bitlen128(*mant);
    if (len <= bits)
        return;
    int shift = len - bits;
    u128 keep = *mant >> shift;
    u128 rem = *mant & (((u128)1 << shift) - 1);
    u128 half = (u128)1 << (shift - 1);
    if (rem > half || (rem == half && (keep & 1)))
        keep++;
    if (keep >> bits) {                /* carried to 2^bits */
        keep >>= 1;
        shift++;
    }
    *mant = keep;
    *exp += shift;
}

/* sign * mant * 2^exp -> double, via a 64-bit mantissa (x87 register),
 * then a 53-bit one (the store to a double). */
static double to_double(int neg, u128 mant, int exp)
{
    if (mant == 0)
        return neg ? -0.0 : 0.0;
    round_bits(&mant, &exp, 64);
    round_bits(&mant, &exp, 53);
    double d = ldexp((double)(uint64_t)mant, exp);
    return neg ? -d : d;
}

/*
 * Valid for |x| < 2^61 (the reduction works in units of 2^-65 in 128 bits);
 * returns NaN for larger |x|, for NaN and for infinities. The decoder only
 * calls it with (l * PI_C) * 2.0 for an LSP 0 <= l < 0.5, i.e. 0 <= x < pi,
 * so the NaN cases are unreachable from lpec_decode.
 */
EXPORT double lpec_fcos(double x)
{
    if (x != x || isinf(x))
        return NAN;
    x = fabs(x);                       /* the emulation is even in x */
    if (x == 0.0)
        return 1.0;
    int e2;
    double f = frexp(x, &e2);          /* x = f * 2^e2, 0.5 <= f < 1 */
    u128 m = (u128)(uint64_t)ldexp(f, 53);
    int e = e2 - 53;                   /* x = m * 2^e exactly */

    /* Remainder r = rm * 2^re (sign rneg) and quadrant q. */
    u128 rm;
    int re, rneg = 0;
    unsigned q = 0;
    if (x < 0.78) {                    /* below pi/4: n = 0, r = x */
        rm = m;
        re = e;
    } else {
        if (e + 65 > 73)               /* x >= 2^61: out of range */
            return NAN;
        u128 X = m << (e + 65);        /* x in units of 2^-65 */
        u128 n = X / PI66;
        u128 rem = X - n * PI66;
        if (2 * rem > PI66 || (2 * rem == PI66 && (n & 1)))
            n++;
        s128 R = (s128)X - (s128)(n * PI66);
        q = (unsigned)(n & 3);
        if (R < 0) {
            rneg = 1;
            R = -R;
        }
        rm = (u128)R;
        re = -65;
    }

    u128 cosr, sinm;                   /* cos r = cosr * 2^-126 */
    int sine;                          /* |sin r| = sinm * 2^sine */
    if (rm == 0) {
        cosr = FIX_ONE;
        sinm = 0;
        sine = 0;
    } else {
        /* |r| = rf * 2^(E - 126), rf in [2^125, 2^126). */
        int len = bitlen128(rm);
        u128 rf = rm << (FIX - len);
        int E = re + len;              /* |r| = (rf * 2^-126) * 2^E, E <= 0 */
        u128 z = fmul(rf, rf);         /* (rf^2) * 2^-126, then * 2^(2E) */
        int zs = -2 * E;
        z = zs >= 128 ? 0 : z >> zs;

        /* cos r = sum (-1)^k z^k/(2k)!, S = sum (-1)^k z^k/(2k+1)! */
        s128 c = (s128)FIX_ONE, s = (s128)FIX_ONE;
        u128 tc = FIX_ONE, ts = FIX_ONE;
        for (unsigned k = 1; tc | ts; k++) {
            tc = fmul(tc, z) / ((2 * k - 1) * (2 * k));
            ts = fmul(ts, z) / ((2 * k) * (2 * k + 1));
            if (k & 1) {
                c -= (s128)tc;
                s -= (s128)ts;
            } else {
                c += (s128)tc;
                s += (s128)ts;
            }
        }
        cosr = (u128)c;
        sinm = fmul(rf, (u128)s);      /* |sin r| = sinm * 2^(E - 126) */
        sine = E - FIX;
    }

    /* cos x by quadrant: 0: cos r, 1: -sin r, 2: -cos r, 3: sin r. */
    switch (q) {
    case 0: return to_double(0, cosr, -FIX);
    case 1: return to_double(!rneg, sinm, sine);
    case 2: return to_double(1, cosr, -FIX);
    default: return to_double(rneg, sinm, sine);
    }
}

/* ------------------------------------------------------------------------
 * Integer helpers (docs/lpec.md, "Conventions"; params.py)
 * ---------------------------------------------------------------------- */

static i64 s16(i64 x) { return (int16_t)(uint16_t)(x & 0xFFFF); }
static i64 i32(i64 x) { return (int32_t)(uint32_t)(x & 0xFFFFFFFF); }
static i64 u32(i64 x) { return x & 0xFFFFFFFF; }
static i64 shl(i64 x, int k) { return (i64)((uint64_t)x << k); }
static i64 pmod(i64 a, i64 b) { i64 r = a % b; return r < 0 ? r + b : r; }
static i64 cdiv(i64 a, i64 b) { return a / b; }   /* C: toward zero */

/* ------------------------------------------------------------------------
 * Tables (order fixed by openevp/decoders/sony_lpec/_core.py)
 * ---------------------------------------------------------------------- */

enum {
    T_C1, T_C2, T_C3, T_C4,   /* LSP codebooks by stage; T_C4 unused with 3 stages */
    T_PT, T_SHAPES, T_GAIN, T_BG1, T_BG2, T_VQ2, T_VQ4, T_VQ8, T_NA, T_NB,
    T_WIN,              /* F-point sine window v (F = frame) */
    T_WIN_SQ,           /* its square v2 */
    T_WIN_LONG,         /* 2F-point sine window */
    T_FFT_SIN_2048, T_FFT_SIN_1536,
    T_POST0, T_POST1, T_POST2, T_POST3,   /* post-twiddle by transform type */
    T_LSP_INIT,
    T_DEFAULT_SHAPE,    /* 8 doubles, all 1.0: Tables.DEFAULT_SHAPE */
    N_DTABLES
};
enum { T_D1, T_D2, T_D3, T_D4, T_PQ, T_S2048, T_S1536, N_ITABLES };

typedef struct {
    const double *d[N_DTABLES];
    const int32_t *i[N_ITABLES];
} Tables;

/* ------------------------------------------------------------------------
 * Configuration (openevp/decoders/sony_lpec/config.py; the layout of the
 * int32 array _core.py passes)
 * ---------------------------------------------------------------------- */

enum {
    CFG_FRAME, CFG_ORDER, CFG_BANDS, CFG_STAGES, CFG_LAG_BITS,
    CFG_N = 5,          /* 4 entries each, by transform type t */
    CFG_W = 9,
    CFG_S = 13,
    CFG_E = 17,
    CFG_OVERLAP = 21,
    N_CFG = 25
};

typedef struct {
    int frame, order, bands, stages, max_lag;
    int N[4], W[4], s[4], e[4], overlap[4];
} Config;

#define MAX_ORDER 16
#define MAX_BANDS 10
#define MAX_FRAME 1024
#define MAX_N 2048          /* longest transform (2 frames) */
#define MAX_W 96            /* widest band */
#define MAX_LAG 255         /* 8-bit lag field */
#define HISTORY 256         /* pitch history kept between frames (either rate) */

static const double Q15 = 3.0517578125e-05;
static const double PI_C = 3.14159265359;

/* Checks a configuration against the sizes this file's arrays allow.
 * Returns 0 when usable. */
static int config_from(const int32_t *c, int n, Config *cfg)
{
    if (n != N_CFG)
        return -1;
    cfg->frame = c[CFG_FRAME];
    cfg->order = c[CFG_ORDER];
    cfg->bands = c[CFG_BANDS];
    cfg->stages = c[CFG_STAGES];
    if (c[CFG_LAG_BITS] < 1 || c[CFG_LAG_BITS] > 8)
        return -1;
    cfg->max_lag = (1 << c[CFG_LAG_BITS]) - 1;
    if ((cfg->frame != 512 && cfg->frame != 1024) || cfg->order < 2 || cfg->order > MAX_ORDER ||
        cfg->order % 2 || cfg->bands < 1 || cfg->bands > MAX_BANDS ||
        (cfg->stages != 3 && cfg->stages != 4))
        return -1;
    for (int t = 0; t < 4; t++) {
        cfg->N[t] = c[CFG_N + t];
        cfg->W[t] = c[CFG_W + t];
        cfg->s[t] = c[CFG_S + t];
        cfg->e[t] = c[CFG_E + t];
        cfg->overlap[t] = c[CFG_OVERLAP + t];
        int N = cfg->N[t];
        if (N < 8 || N > MAX_N || N % 8 || (N % 3 != 0 && 2048 % N) || (N % 3 == 0 && 1536 % N) ||
            cfg->W[t] < 1 || cfg->W[t] > MAX_W || cfg->s[t] < 0 ||
            cfg->e[t] != cfg->s[t] + cfg->bands * cfg->W[t] || cfg->e[t] > N / 2 ||
            cfg->overlap[t] < 0 || cfg->overlap[t] > N || cfg->overlap[t] % 2)
            return -1;
    }
    /* The frame layout (decode_frame) assumes type 0 is one frame long and
     * type 3 two, with the overlaps InitDecoder gives them. */
    if (cfg->N[0] != cfg->frame || cfg->N[3] != 2 * cfg->frame ||
        cfg->overlap[0] != cfg->frame / 2 || cfg->overlap[2] != cfg->frame)
        return -1;
    return 0;
}

/* Table row counts (openevp/decoders/sony_lpec/tables.py: C1..C4/D1..D4/PT/PQ
 * 64 rows, SHAPES/GAIN 128, BG1/BG2 64, VQ2/VQ4/VQ8 256), i.e. the bitstream
 * field widths that index them (bitstream.py: 6-bit LSP/pgidx/band-gain
 * indices, 7-bit global-gain/shape indices, 8-bit VQ indices). Frame records
 * come from this repo's own parser (openevp/decoders/sony_lpec/bitstream.py
 * via _core.py's packing), but decode_frame validates them defensively
 * rather than trusting the packed ints to stay in range.
 */
#define N_LSP 64        /* C1..C4/D1..D4 rows: LSP set indices */
#define N_PT 64         /* PT/PQ rows: pgidx */
#define N_SHAPES 128    /* SHAPES rows: shape index */
#define N_GAIN 128      /* GAIN entries: global_gain */
#define N_BG 64         /* BG1/BG2 rows: band_gain1/band_gain2 */
#define N_VQ 256        /* VQ2/VQ4/VQ8 rows: raw 8-bit VQ indices */

/* ------------------------------------------------------------------------
 * LSP decoding (params.py)
 * ---------------------------------------------------------------------- */

static void sort_pass_d(double *v, int order)
{
    for (int j = 1; j <= order; j++) {
        if (v[j] < v[j - 1]) { double t = v[j]; v[j] = v[j - 1]; v[j - 1] = t; }
        if (j >= 2 && v[j - 2] > v[j - 1]) { double t = v[j - 2]; v[j - 2] = v[j - 1]; v[j - 1] = t; }
    }
}

static void sort_pass_i(i64 *v, int order)
{
    for (int j = 1; j <= order; j++) {
        if (v[j] < v[j - 1]) { i64 t = v[j]; v[j] = v[j - 1]; v[j - 1] = t; }
        if (j >= 2 && v[j - 2] > v[j - 1]) { i64 t = v[j - 2]; v[j - 2] = v[j - 1]; v[j - 1] = t; }
    }
}

static void lsp_double(const Tables *T, const Config *cfg, const int *idx, double *l)
{
    int order = cfg->order, half = order / 2;
    const double *c1 = T->d[T_C1] + order * idx[0], *c2 = T->d[T_C2] + order * idx[1],
                 *c3 = T->d[T_C3] + order * idx[2];
    l[0] = 0.0;
    if (cfg->stages == 4) {
        const double *c4 = T->d[T_C4] + order * idx[3];
        for (int j = 0; j < order; j++)
            l[j + 1] = ((c3[j] + c4[j]) + c2[j]) + c1[j];
    } else {
        for (int j = 0; j < order; j++)
            l[j + 1] = (c3[j] + c2[j]) + c1[j];
    }

    double lim = 0.49;
    for (int j = order; j > half; j--) {
        if (l[j] >= 0.5)
            l[j] = lim;
        lim = l[j] - 0.01;
    }
    lim = 0.01;
    for (int j = 1; j <= half; j++) {
        if (l[j] < 0.0)
            l[j] = lim;
        lim = l[j] + 0.01;
    }
    l[0] = 0.0;
    sort_pass_d(l, order);
    for (int j = 1; j <= order; j++) {
        if (l[j] - l[j - 1] < 0.01) {
            if (j == 1) {
                l[1] = 0.01;
            } else {
                double s = l[j - 1] + l[j];
                l[j] = (0.01 + s) * 0.5;
                l[j - 1] = (s - 0.01) * 0.5;
            }
        }
    }
    l[0] = 0.0;
}

static void lsp_q16(const Tables *T, const Config *cfg, const int *idx, i64 *q)
{
    int order = cfg->order, half = order / 2;
    const int32_t *d1 = T->i[T_D1] + order * idx[0], *d2 = T->i[T_D2] + order * idx[1],
                  *d3 = T->i[T_D3] + order * idx[2];
    q[0] = 0;
    if (cfg->stages == 4) {
        /* D1 is Q16, D2..D4 Q19 */
        const int32_t *d4 = T->i[T_D4] + order * idx[3];
        for (int j = 0; j < order; j++)
            q[j + 1] = s16((((i64)d2[j] + d3[j] + d4[j]) >> 3) + d1[j]);
    } else {
        for (int j = 0; j < order; j++)
            q[j + 1] = s16(((i64)d3[j] + 2 * (i64)d2[j] + 4 * (i64)d1[j]) >> 2);
    }

    i64 lim = 32112;
    for (int j = order; j > half; j--) {
        if (q[j] < 0)
            q[j] = s16(lim);
        lim = q[j] - 655;
    }
    lim = 655;
    for (int j = 1; j <= half; j++) {
        if (q[j] < 0)
            q[j] = s16(lim);
        lim = q[j] + 655;
    }
    q[0] = 0;
    sort_pass_i(q, order);
    if (q[1] < 655)
        q[1] = 655;
    for (int j = 2; j <= order; j++) {
        i64 a = q[j], b = q[j - 1];
        if (a - b < 655) {
            q[j] = s16(cdiv(b + 656 + a, 2));
            q[j - 1] = s16(cdiv(a + b - 654, 2));
        }
    }
    q[0] = 0;
}

/* ------------------------------------------------------------------------
 * Spectral envelope (params.py)
 * ---------------------------------------------------------------------- */

static i64 qmul(i64 x, i64 c)
{
    i64 lo = x & 0xFFFF;
    i64 hi = x >> 16;
    return i32(2 * (((lo * c) >> 15) + 2 * hi * c));
}

static void lpc_q(const Tables *T, const i64 *q, int order, i64 *a16)
{
    const int32_t *S = T->i[T_S2048];
    int n = order + 1;
    i64 c[MAX_ORDER + 1];
    for (int j = 0; j < n; j++) {
        i64 e = i32(shl(q[j], 11));
        i64 f = (e & 0xFFFF) >> 1;
        i64 i = e >> 16;
        i64 u = pmod(i + 512, 2048);
        c[j] = s16(((32768 - f) * S[u] + S[u + 1] * f) >> 15);
    }

    i64 Q[MAX_ORDER + 1] = {0}, P[MAX_ORDER + 1] = {0}, TQ[MAX_ORDER + 1] = {0},
        TP[MAX_ORDER + 1] = {0};
    Q[0] = -((i64)1 << 23);
    Q[1] = (i64)1 << 23;
    P[0] = -((i64)1 << 23);
    P[1] = -((i64)1 << 23);
    for (int i = 1; i <= order / 2; i++) {
        i64 a = c[2 * i];
        i64 b = c[2 * i - 1];
        TQ[1] = qmul(Q[0], a);
        TP[1] = qmul(P[0], b);
        for (int m = 2; m <= i; m++) {
            TQ[m] = i32(qmul(Q[m - 1], a) - Q[m - 2]);
            TP[m] = i32(qmul(P[m - 1], b) - P[m - 2]);
        }
        for (int m = 1; m <= i; m++) {
            Q[m] = i32(Q[m] - TQ[m]);
            Q[2 * i + 1 - m] = i32(-Q[m]);
            P[m] = i32(P[m] - TP[m]);
            P[2 * i + 1 - m] = P[m];
        }
    }

    i64 R[MAX_ORDER + 1];
    i64 A = 0;
    for (int j = 0; j < n; j++) {
        R[j] = i32(-(P[j] + Q[j]));
        A |= R[j] < 0 ? -R[j] : R[j];
    }
    int d = 0;
    while (d < 16 && !(u32(shl(A, d)) & 0x40000000))
        d++;
    i64 rnd = d <= 15 ? ((i64)1 << (15 - d)) : 0;
    for (int j = 0; j < n; j++)
        a16[j] = s16((R[j] + rnd) >> (16 - d));
}

static void autocorrelation(const i64 *a16, int order, i64 *E0, i64 *E1, i64 *r)
{
    int n = order + 1;
    i64 lo = 0, hi = 0;
    for (int i = 0; i < n; i++) {
        i64 sq = a16[i] * a16[i];
        lo += sq & 0x7FFF;
        hi += sq >> 15;
    }
    i64 l = lo & 0x7FFF;
    i64 h = hi + (u32(lo) >> 15);

    int s = 0;
    i64 ah = h < 0 ? -h : h;
    for (int k = 0; k < 20; k++) {
        if (shl(ah, k) & ((i64)1 << 18)) {
            s = 19 - k;
            break;
        }
    }

    int x = s - 10;
    if (x > 0) {
        l = l | shl(h & (((i64)1 << x) - 1), 15);
        if (s - 14 > 0)
            l = u32(l) >> (s - 14);
        h = h >> x;
    } else if (x < 0) {
        h = shl(h, -x) | (l >> (s + 5));
        l = l & (((i64)1 << (s + 5)) - 1);
    }
    *E0 = h + 2;
    *E1 = l;

    r[0] = 0;
    i64 mask = ((i64)1 << s) - 1;
    for (int m = 1; m < n; m++) {
        i64 slo = 0, shi = 0;
        for (int i = 0; i < n - m; i++) {
            i64 p = a16[i] * a16[i + m];
            slo += p & mask;
            shi += p >> s;
        }
        r[m] = s16(shi + (u32(slo) >> s));
    }
}

static void envelope(const Tables *T, const i64 *q, int order, int lag, double pg, int N, i64 *w)
{
    int M = N / 2;
    i64 a16[MAX_ORDER + 1], E0, E1, r[MAX_ORDER + 1];
    lpc_q(T, q, order, a16);
    autocorrelation(a16, order, &E0, &E1, r);

    const int32_t *table;
    int step;
    if (N % 3 == 0) {
        table = T->i[T_S1536];
        step = 1536 / N;
    } else {
        table = T->i[T_S2048];
        step = 2048 / N;
    }
#define TT(p) ((i64)table[step * ((p) % N)])

    int quarter = N / 4;
    i64 gq = 0;
    if (lag != 0)
        gq = s16((i64)(32768 * pg));
    for (int k = 0; k < M; k++) {
        i64 accH = E0, accL = E1;
        for (int m = 1; m <= order; m++) {
            i64 v = r[m] * TT(quarter + m * k);
            accL += v & 0x7FFFF;
            accH += v >> 19;
        }
        if (lag == 0) {
            w[k] = i32(shl(accH, 13) + (u32(accL) >> 6));
        } else {
            i64 c = TT(quarter + lag * k);
            i64 f = (gq * ((gq - 2 * c) >> 2) + ((i64)1 << 28)) >> 15;
            i64 part1 = u32(((accL & 0x7FFFF) >> 4) * f) >> 14;
            i64 part2 = i32(i32((accH + (u32(accL) >> 19)) * f) * 2);
            w[k] = i32(part1 + part2);
        }
    }
#undef TT
}

/* ------------------------------------------------------------------------
 * Coefficient block (params.py)
 * ---------------------------------------------------------------------- */

typedef struct {
    int slot, type, gg, bg1, bg2;
    int n4[MAX_BANDS], n2[MAX_BANDS], n1[MAX_BANDS], ns[MAX_BANDS];
    int k0, k1, k2, ks;
    const int32_t *vq2, *vq4, *vq8, *signs;
} Block;

typedef struct {
    const double *codebook;
    const int32_t *indices;
    int dim, vec, pos;
} VQStream;

static double vq_next(VQStream *v)
{
    if (v->pos == v->dim) {
        v->vec++;
        v->pos = 0;
    }
    double value = v->codebook[v->indices[v->vec] * v->dim + v->pos];
    v->pos++;
    return value;
}

typedef struct { i64 key; int x; } Rank;

static int rank_cmp(const void *a, const void *b)
{
    i64 ka = ((const Rank *)a)->key, kb = ((const Rank *)b)->key;
    return ka < kb ? -1 : ka > kb;
}

static void coefficients(const Tables *T, const Config *cfg, const Block *blk, const i64 *q,
                         int lag, const int32_t *pq, int *noise_z, double *X)
{
    int bands = cfg->bands;
    int t = blk->type;
    int N = cfg->N[t];
    int M = N / 2;
    int W = cfg->W[t];
    int s = cfg->s[t];
    int e = cfg->e[t];

    double G = T->d[T_GAIN][blk->gg];
    const double *bg1 = T->d[T_BG1] + bands * blk->bg1;
    const double *bg2 = T->d[T_BG2] + bands * blk->bg2;
    double bg[MAX_BANDS];
    for (int b = 0; b < bands; b++) {
        double v = bg2[b] + bg1[b];
        if (v < 1e-05)
            v = 1e-05;
        bg[b] = v;
    }
    double pg = (double)pq[1] * Q15;

    i64 w[MAX_N / 2];
    envelope(T, q, cfg->order, lag, pg, N, w);

    VQStream vq2 = {T->d[T_VQ2], blk->vq2, 2, 0, 0};
    VQStream vq4 = {T->d[T_VQ4], blk->vq4, 4, 0, 0};
    VQStream vq8 = {T->d[T_VQ8], blk->vq8, 8, 0, 0};
    int sign_i = 0;
    const double *NA = T->d[T_NA], *NB = T->d[T_NB];
    int z = *noise_z;

    for (int i = 0; i < M; i++)
        X[i] = 0.0;
    for (int b = 0; b < bands; b++) {
        int base = s + b * W;
        Rank order[MAX_W];
        for (int j = 0; j < W; j++) {
            order[j].key = (w[base + j] & ~(i64)0x7F) | j;
            order[j].x = base + j;
        }
        qsort(order, W, sizeof(Rank), rank_cmp);
        int idx = 0;
        for (int n = 0; n < blk->n4[b]; n++)
            X[order[idx++].x] = vq_next(&vq2);
        for (int n = 0; n < blk->n2[b]; n++)
            X[order[idx++].x] = vq_next(&vq4);
        for (int n = 0; n < blk->n1[b]; n++)
            X[order[idx++].x] = vq_next(&vq8);
        for (int n = 0; n < blk->ns[b]; n++)
            X[order[idx++].x] = blk->signs[sign_i++] ? -0.6 : 0.6;
        while (idx < W) {
            X[order[idx++].x] = ((NB[z] * 0.9) * pg) + ((NA[z] * (1.0 - pg)) * 0.9);
            z = (z + 1) % 1024;
        }
    }
    *noise_z = z;

    for (int i = 0; i < s; i++)
        X[i] = 0.0;
    for (int i = e; i < M; i++)
        X[i] = 0.0;
    for (int b = 0; b < bands; b++) {
        int base = s + b * W;
        double g = bg[b];
        for (int x = base; x < base + W; x++)
            X[x] = (X[x] * g) * G;
    }
}

/* ------------------------------------------------------------------------
 * FFT and inverse transform (synthesis.py)
 * ---------------------------------------------------------------------- */

static void fft_radix2(double *re, double *im, int n, double c, const double *S)
{
    int size = n;
    int stride = 2048 / n;
    while (size > 4) {
        int half = size / 2;
        for (int k = 0; k < half; k++) {
            double ws = S[k * stride];
            double wc = S[k * stride + 512];
            for (int a = k; a < n; a += size) {
                int b = a + half;
                double re_a = re[a], re_b = re[b], im_a = im[a], im_b = im[b];
                re[a] = re_a + re_b;
                im[a] = im_b + im_a;
                double dr = re_a - re_b;
                double di = im_a - im_b;
                re[b] = (dr * wc) - (di * ws);
                im[b] = (di * wc) + (dr * ws);
            }
        }
        stride *= 2;
        size = half;
    }

    for (int g = 0; g < n; g += 4) {
        double r0 = re[g], r1 = re[g + 1], r2 = re[g + 2], r3 = re[g + 3];
        double i0 = im[g], i1 = im[g + 1], i2 = im[g + 2], i3 = im[g + 3];
        re[g] = ((r3 + r1) + (r2 + r0)) * c;
        re[g + 1] = ((r2 + r0) - (r3 + r1)) * c;
        re[g + 2] = ((r0 - r2) - (i1 - i3)) * c;
        re[g + 3] = ((i1 - i3) + (r0 - r2)) * c;
        im[g] = ((i1 + i3) + (i0 + i2)) * c;
        im[g + 1] = ((i0 + i2) - (i1 + i3)) * c;
        im[g + 2] = ((i0 - i2) + (r1 - r3)) * c;
        im[g + 3] = ((i0 - i2) - (r1 - r3)) * c;
    }

    int j = 0;
    for (int i = 0; i < n - 1; i++) {
        if (i < j) {
            double t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
        int k = n / 2;
        while (k <= j) {
            j -= k;
            k /= 2;
        }
        j += k;
    }
}

/* Size n = 3m (384 or 768): one radix-3 stage, three radix-2 FFTs of m. */
static void fft_radix3(double *re, double *im, int n, const double *S, const double *U, double s3)
{
    const int m = n / 3;
    const int step = 1536 / n;
    for (int j = 0; j < m; j++) {
        double x0 = re[j], x1 = re[j + m], x2 = re[j + 2 * m];
        double y0 = im[j], y1 = im[j + m], y2 = im[j + 2 * m];
        int k = step * j;
        re[j] = x0 + (x2 + x1);
        im[j] = y0 + (y2 + y1);
        double alpha = x0 - (0.5 * (x2 + x1));
        double beta = ((y2 + y1) * 0.5) + (-y0);
        double gamma = (x1 - x2) * s3;
        double delta = (y1 - y2) * s3;
        double u1 = alpha - delta;
        double v1 = beta - gamma;
        double u2 = delta + alpha;
        double v2 = gamma + beta;
        re[j + m] = (U[k] * v1) + (U[k + 384] * u1);
        im[j + m] = (u1 * U[k]) - (v1 * U[k + 384]);
        re[j + 2 * m] = (U[2 * k] * v2) + (U[2 * k + 384] * u2);
        im[j + 2 * m] = (u2 * U[2 * k]) - (v2 * U[2 * k + 384]);
    }

    double c = 1.0 / n;
    double tr[3][MAX_N / 6], ti[3][MAX_N / 6];
    for (int p = 0; p < 3; p++) {
        memcpy(tr[p], re + p * m, m * sizeof(double));
        memcpy(ti[p], im + p * m, m * sizeof(double));
        fft_radix2(tr[p], ti[p], m, c, S);
    }
    for (int j = 0; j < m; j++) {
        for (int p = 0; p < 3; p++) {
            re[3 * j + p] = tr[p][j];
            im[3 * j + p] = ti[p][j];
        }
    }
}

static void inverse_transform(const Tables *T, const double *X, int N, int overlap,
                              const double *w, double s3, double *Y)
{
    int h = N / 2;
    int q = N / 4;
    const double *S;
    int sigma;
    if (N % 3 == 0) {
        S = T->d[T_FFT_SIN_1536];
        sigma = 1536 / N;
    } else {
        S = T->d[T_FFT_SIN_2048];
        sigma = 2048 / N;
    }

    double re[MAX_N / 2], im[MAX_N / 2];
    for (int i = 0; i < q; i++) {
        double x = X[2 * i];
        re[i] = x * S[(q - i) * sigma];
        im[i] = x * S[i * sigma];
    }
    for (int i = q; i < h; i++) {
        double x = X[N - 1 - 2 * i];
        re[i] = x * S[(i - q) * sigma];
        im[i] = x * S[(N - i) * sigma];
    }

    if (h % 3 == 0)
        fft_radix3(re, im, h, T->d[T_FFT_SIN_2048], T->d[T_FFT_SIN_1536], s3);
    else
        fft_radix2(re, im, h, 1.0 / h, T->d[T_FFT_SIN_2048]);

    int half_lambda = overlap / 2;
    int top = N - half_lambda - 1;
    for (int i = 0; i < h; i++)
        Y[top - i] = (((-re[i]) * w[h - 1 - i]) + (im[i] * w[i])) * 2.0;
    for (int j = 0; j < half_lambda; j++)
        Y[N - half_lambda + j] = Y[N - half_lambda - 1 - j];
    for (int j = 0; j < (N - overlap) / 2; j++)
        Y[j] = -Y[N - overlap - 1 - j];
}

/* ------------------------------------------------------------------------
 * Overlap-add (synthesis.py)
 * ---------------------------------------------------------------------- */

static void overlap_add_plain(double *O, const double *Y, int length, int carry,
                              const double *v, double *out)
{
    for (int i = 0; i < length; i++)
        out[i] = (O[i] * v[length - 1 - i]) + (Y[i] * v[i]);
    memcpy(O, Y + length, carry * sizeof(double));
}

/* The mode-0 half: `half` outputs from the F-point Y (F = 2 * half).
 * Returns -1 (Python would raise ZeroDivisionError, or later fail casting a
 * non-finite sample to int16) instead of letting a non-finite result run
 * through the rest of the pipeline undetected. */
static int overlap_add_shaped(double *O, const double *Y, int half, int fp, const double *Ap,
                              int fc, const double *Ac, const double *v,
                              const double *v2, double *out)
{
    int part = half / 4;
    if (fp == 0 && fc == 0) {
        for (int j = 0; j < half; j++) {
            out[j] = (O[j] * v[half + j]) + (v[j] * Y[j]);
            if (!isfinite(out[j]))
                return -1;
        }
    } else {
        for (int r = 0; r < 4; r++) {
            double ap = Ap[7 - r];
            double ac = Ac[3 - r];
            double rho1 = ap / Ap[4 + r];
            double rho2 = ac / Ac[r];
            for (int j = part * r; j < part * r + part; j++) {
                out[j] = ((O[j] * (ap * v[half + j])) + ((ac * v[j]) * Y[j])) /
                         ((v2[half + j] * rho1) + (v2[j] * rho2));
                if (!isfinite(out[j]))
                    return -1;
            }
        }
    }
    memcpy(O, Y + half, half * sizeof(double));
    return 0;
}

/* ------------------------------------------------------------------------
 * Long-term (pitch) predictor (synthesis.py)
 * ---------------------------------------------------------------------- */

/* floor(v) * 2^-15 with Python's int semantics (math.floor gives an int,
 * so a -0.0 comes back as +0.0). */
static double q15_floor(double v)
{
    return (floor(v) + 0.0) * 3.0517578125e-05;
}

static int taps_stable(int lag, const double *taps)
{
    double b0 = taps[0], b1 = taps[1], b2 = taps[2];
    double c[MAX_LAG + 2], r[MAX_LAG + 2], nr[MAX_LAG + 2];
    int len = lag + 2;
    for (int j = 0; j < len; j++)
        c[j] = 0.0;
    c[0] = 1.0;
    c[lag - 1] = -b0;
    c[lag] = -b1;
    c[lag + 1] = -b2;
    for (int j = 0; j < len; j++)
        r[j] = q15_floor((c[j] * 0.125) * 32768.0);
    for (int m = lag + 1; m > 0; m--) {
        double rm = r[m];
        if (fabs(rm) > 0.1225)
            return 0;
        double kappa = rm * 8.0;
        double denom = 1.0 - (kappa * kappa);
        for (int j = 0; j <= m; j++)
            nr[j] = q15_floor(((r[j] - ((rm * r[m - j]) * 8.0)) / denom) * 32768.0);
        memcpy(r, nr, (m + 1) * sizeof(double));
    }
    return 1;
}

static void pitch_section(double *H, const double *x, int start, int length, int lag,
                          const double *taps)
{
    if (lag == 0) {
        for (int n = start; n < start + length; n++)
            H[n] = x[n - HISTORY];
        return;
    }
    double b0 = taps[0], b1 = taps[1], b2 = taps[2];
    if (!taps_stable(lag, taps)) {
        b0 = 0.0;
        b2 = 0.0;
    }
    for (int n = start; n < start + length; n++)
        H[n] = (((b2 * H[n - lag - 1]) + x[n - HISTORY]) + (b1 * H[n - lag])) + (H[n - lag + 1] * b0);
}

/* ------------------------------------------------------------------------
 * LPC synthesis (synthesis.py)
 * ---------------------------------------------------------------------- */

static void lsp_to_lpc(const double *l, int order, double *a)
{
    int n = order + 1;
    double C[MAX_ORDER + 2] = {0};
    for (int j = 1; j <= n; j++)
        C[j] = lpec_fcos((l[j - 1] * PI_C) * 2.0);
    double P[MAX_ORDER + 1] = {0}, Q[MAX_ORDER + 1] = {0}, TQ[MAX_ORDER + 1] = {0},
           TP[MAX_ORDER + 1] = {0};
    P[0] = -1.0;
    P[1] = -1.0;
    Q[0] = -1.0;
    Q[1] = 1.0;
    for (int i = 2; i <= order; i += 2) {
        double u = C[i];
        double v = C[i + 1];
        TQ[1] = (v * 2.0) * Q[0];
        TP[1] = (u * 2.0) * P[0];
        for (int m = 2; m <= i / 2; m++) {
            TQ[m] = ((Q[m - 1] * v) * 2.0) - Q[m - 2];
            TP[m] = ((P[m - 1] * u) * 2.0) - P[m - 2];
        }
        for (int m = 1; m <= i / 2; m++) {
            Q[m] = Q[m] - TQ[m];
            Q[i + 1 - m] = -Q[m];
            P[m] = P[m] - TP[m];
            P[i + 1 - m] = P[m];
        }
    }
    for (int m = 0; m < n; m++)
        a[m] = (Q[m] + P[m]) * (-0.5);
}

static void interpolate_lsp(const double *lA, const double *lB, double t, int n, double *l)
{
    for (int j = 0; j < n; j++)
        l[j] = (lA[j] * (1.0 - t)) + (lB[j] * t);
}

/* y[k] for k = count..count+length-1; y[count-order..count-1] is the memory. */
static void lpc_filter(const double *e, int start, int length, const double *a, int order,
                       double *y, int *count)
{
    int k = *count;
    for (int n = start; n < start + length; n++, k++) {
        double v = e[n];
        for (int m = 1; m <= order; m++)
            v = v - (y[k - m] * a[m]);
        y[k] = v;
    }
    *count = k;
}

/* floor(y + 0.5), clamped to int16. Python's to_int16 raises on a non-finite
 * y (math.floor(nan)/math.floor(inf)); a NaN cast to int16 is UB in C, so we
 * fail the same way here: -1 (via *out left unset) instead of casting. */
static int to_int16(double y, int16_t *out)
{
    if (!isfinite(y))
        return -1;
    double v = floor(y + 0.5);
    if (v > 32767)
        *out = 32767;
    else if (v < -32768)
        *out = -32768;
    else
        *out = (int16_t)v;
    return 0;
}

/* ------------------------------------------------------------------------
 * Decoder state and one frame (decoder.py)
 * ---------------------------------------------------------------------- */

typedef struct {
    int noise_z;
    double H[HISTORY + MAX_FRAME];
    double lsp0[MAX_ORDER + 1];
    i64 q0[MAX_ORDER + 1];
    int lag0;
    double taps0[3];
    int flags[3];
    const double *shapes[3];
    double mem[MAX_ORDER];
    double O[MAX_FRAME];
} State;

static void state_init(State *st, const Tables *T, const Config *cfg)
{
    memset(st, 0, sizeof *st);
    memcpy(st->lsp0, T->d[T_LSP_INIT], (cfg->order + 1) * sizeof(double));
    for (int j = 0; j <= cfg->order; j++)
        st->q0[j] = s16((i64)floor(st->lsp0[j] * 65536.0));
    for (int i = 0; i < 3; i++)
        st->shapes[i] = T->d[T_DEFAULT_SHAPE];
}

static void pitch_params(const Tables *T, int lag, int pgidx, double *taps, const int32_t **pq)
{
    static const int32_t ZERO_Q15[3] = {0, 0, 0};
    if (lag == 0) {
        taps[0] = taps[1] = taps[2] = 0.0;
        *pq = ZERO_Q15;
    } else {
        memcpy(taps, T->d[T_PT] + 3 * pgidx, 3 * sizeof(double));
        *pq = T->i[T_PQ] + 3 * pgidx;
    }
}

/* Frame record layout: see openevp/decoders/sony_lpec/_core.py (pack_frame).
 * Header: mode, F, lsp_b[stages], lsp_a[stages], lag_a, pgidx_a, lag_b,
 * pgidx_b, shape flag 0, flag 1, shape index 0, index 1, number of blocks.
 * Block: slot, type, global gain, band gain 1, 2, n4/n2/n1/ns[bands],
 * k0, k1, k2, ks, then the indices and sign bits. */
static int decode_frame(State *st, const Tables *T, const Config *cfg, const int32_t *f,
                        int avail, int16_t *samples, double s3)
{
    const int stages = cfg->stages, bands = cfg->bands, order = cfg->order;
    const int F = cfg->frame, half = F / 2;
    const int hdr = 11 + 2 * stages;
    const int brec = 9 + 4 * bands;
    if (avail < hdr)
        return -1;
    const int32_t *lsp_b = f + 2, *lsp_a = f + 2 + stages, *g = f + 2 + 2 * stages;
    int mode = f[0], Fl = f[1];
    int nblocks = g[8];
    if (mode < 0 || mode > 3 || (Fl != 0 && Fl != 1))
        return -1;
    /* mode<->nblocks: modes 0 and 2 always carry a slot-1 and a slot-2
     * block, modes 1 and 3 only slot-2 (bitstream.py: _parse_mode0/2 append
     * two blocks, _parse_mode1/3 one) -- the only shapes pack_frame emits. */
    int expected_blocks = (mode == 0 || mode == 2) ? 2 : 1;
    if (nblocks != expected_blocks)
        return -1;

    /* LSP set indices: 6-bit fields, table rows 0..N_LSP-1. Slot A's are
     * always present in the record, defaulted to zeros by pack_frame when
     * the frame has no slot-1 LSP set, so checking them unconditionally
     * matches what a real frame ever holds. */
    int ib[4], ia[4];
    for (int k = 0; k < stages; k++) {
        ib[k] = lsp_b[k];
        ia[k] = lsp_a[k];
        if (ib[k] < 0 || ib[k] >= N_LSP || ia[k] < 0 || ia[k] >= N_LSP)
            return -1;
    }

    /* Pitch lag: a 7-bit (SP: 8-bit) field. pgidx is a 6-bit field only
     * actually read (into a table index) when lag != 0 -- _read_pitch never
     * reads it otherwise, and pack_frame stores 0 for it either way. */
    int lag1 = g[0], lag2 = g[2];
    if (lag1 < 0 || lag1 > cfg->max_lag || lag2 < 0 || lag2 > cfg->max_lag)
        return -1;
    if (lag1 != 0 && (g[1] < 0 || g[1] >= N_PT))
        return -1;
    if (lag2 != 0 && (g[3] < 0 || g[3] >= N_PT))
        return -1;

    /* Shape flags/indices: mode 0 only; the index is only read (and only
     * meaningful) when its flag is set (_parse_mode0). */
    if (mode == 0) {
        if ((g[4] != 0 && g[4] != 1) || (g[5] != 0 && g[5] != 1))
            return -1;
        if (g[4] && (g[6] < 0 || g[6] >= N_SHAPES))
            return -1;
        if (g[5] && (g[7] < 0 || g[7] >= N_SHAPES))
            return -1;
    }

    int used = hdr;
    Block blocks[2];
    for (int bi = 0; bi < nblocks; bi++) {
        Block *b = &blocks[bi];
        if (avail - used < brec)
            return -1;
        const int32_t *p = f + used;
        b->slot = p[0];
        b->type = p[1];
        b->gg = p[2];
        b->bg1 = p[3];
        b->bg2 = p[4];
        for (int k = 0; k < bands; k++) {
            b->n4[k] = p[5 + k];
            b->n2[k] = p[5 + bands + k];
            b->n1[k] = p[5 + 2 * bands + k];
            b->ns[k] = p[5 + 3 * bands + k];
        }
        b->k0 = p[5 + 4 * bands];
        b->k1 = p[6 + 4 * bands];
        b->k2 = p[7 + 4 * bands];
        b->ks = p[8 + 4 * bands];
        if (b->type < 0 || b->type > 3 ||
            b->k0 < 0 || b->k1 < 0 || b->k2 < 0 || b->ks < 0)
            return -1;
        /* Slot order is fixed by the parser: the first (only) block of a
         * one-block mode is always slot 2; a two-block mode's first block
         * is always slot 1 and its second always slot 2 (bitstream.py
         * _parse_mode0/_parse_mode2 read block0 with slot=1, block1 with
         * slot=2). No frame the parser can produce has any other slot in
         * any other position. */
        int expected_slot = (nblocks == 2 && bi == 0) ? 1 : 2;
        if (b->slot != expected_slot)
            return -1;
        /* ... and so is each block's transform type (mode 0: 0, 0; mode 1:
         * 1; mode 2: 0, 2; mode 3: 3), which the frame layout below relies on. */
        int expected_type = mode == 0 ? 0 : mode == 2 ? (bi == 0 ? 0 : 2) : mode;
        if (b->type != expected_type)
            return -1;
        if (b->gg < 0 || b->gg >= N_GAIN || b->bg1 < 0 || b->bg1 >= N_BG ||
            b->bg2 < 0 || b->bg2 >= N_BG)
            return -1;
        /* Per band, n4+n2+n1+ns must not exceed the band width W: coefficients()
         * ranks exactly W positions per band into a W-sized prefix of order[],
         * so an over-budget band would read past the entries it filled (and,
         * for W < MAX_W, could still write X[] through garbage stack indices).
         * The totals must also match k0/k1/k2 exactly (VQ2 dim 2, VQ4 dim 4,
         * VQ8 dim 8; bitstream.bit_allocation always produces this): each
         * band's n4/n2/n1 quota is exactly what advances vq_next() by, so a
         * mismatch would run a VQStream past the vq2/vq4/vq8 arrays the
         * record actually provided (already bounds-checked above only for
         * their combined length, not per-class use). */
        int W = cfg->W[b->type];
        i64 sum_n4 = 0, sum_n2 = 0, sum_n1 = 0, sum_ns = 0;
        for (int k = 0; k < bands; k++) {
            if (b->n4[k] < 0 || b->n2[k] < 0 || b->n1[k] < 0 || b->ns[k] < 0)
                return -1;
            i64 band_sum = (i64)b->n4[k] + b->n2[k] + b->n1[k] + b->ns[k];
            if (band_sum > W)
                return -1;
            sum_n4 += b->n4[k];
            sum_n2 += b->n2[k];
            sum_n1 += b->n1[k];
            sum_ns += b->ns[k];
        }
        if (sum_n4 != 2 * (i64)b->k0 || sum_n2 != 4 * (i64)b->k1 ||
            sum_n1 != 8 * (i64)b->k2 || sum_ns != (i64)b->ks)
            return -1;
        used += brec;
        if (avail - used < b->k0 + b->k1 + b->k2 + b->ks)
            return -1;
        b->vq2 = f + used; used += b->k0;
        b->vq4 = f + used; used += b->k1;
        b->vq8 = f + used; used += b->k2;
        b->signs = f + used; used += b->ks;
        /* VQ indices are 8-bit fields (bitstream.py _read_coefficient_block);
         * each one indexes a 256-row codebook directly (params.coefficients /
         * VQStream.next). */
        for (int i = 0; i < b->k0; i++)
            if (b->vq2[i] < 0 || b->vq2[i] >= N_VQ)
                return -1;
        for (int i = 0; i < b->k1; i++)
            if (b->vq4[i] < 0 || b->vq4[i] >= N_VQ)
                return -1;
        for (int i = 0; i < b->k2; i++)
            if (b->vq8[i] < 0 || b->vq8[i] >= N_VQ)
                return -1;
    }

    /* --- Parameter sets --- */
    double l1[MAX_ORDER + 1], l2[MAX_ORDER + 1];
    i64 q1[MAX_ORDER + 1], q2[MAX_ORDER + 1];
    lsp_double(T, cfg, ib, l2);
    lsp_q16(T, cfg, ib, q2);
    if (mode == 0 && Fl == 1) {
        lsp_double(T, cfg, ia, l1);
        lsp_q16(T, cfg, ia, q1);
    } else if (mode == 0 || mode == 2) {
        for (int j = 0; j <= order; j++) {
            l1[j] = (l2[j] + st->lsp0[j]) * 0.5;
            q1[j] = s16(cdiv(st->q0[j] + q2[j], 2));
        }
    }

    double taps1[3], taps2[3];
    const int32_t *pq1, *pq2;
    pitch_params(T, lag1, g[1], taps1, &pq1);
    pitch_params(T, lag2, g[3], taps2, &pq2);

    /* --- Shape state --- */
    if (mode == 0) {
        for (int h = 0; h < 2; h++) {
            st->flags[1 + h] = g[4 + h];
            st->shapes[1 + h] = g[4 + h] ? T->d[T_SHAPES] + 8 * g[6 + h]
                                         : T->d[T_DEFAULT_SHAPE];
        }
    } else if (mode == 2) {
        st->flags[1] = st->flags[2] = 0;
        st->shapes[1] = st->shapes[2] = T->d[T_DEFAULT_SHAPE];
    } else if (mode == 3) {
        st->shapes[1] = st->shapes[2] = T->d[T_DEFAULT_SHAPE];
    }

    /* --- Coefficient blocks -> transforms --- */
    static const int POST[4] = {T_POST0, T_POST1, T_POST2, T_POST3};
    double Ys[2][MAX_N];
    for (int bi = 0; bi < nblocks; bi++) {
        const Block *b = &blocks[bi];
        double X[MAX_N / 2];
        int t = b->type;
        if (b->slot == 1)
            coefficients(T, cfg, b, q1, lag1, pq1, &st->noise_z, X);
        else
            coefficients(T, cfg, b, q2, lag2, pq2, &st->noise_z, X);
        inverse_transform(T, X, cfg->N[t], cfg->overlap[t], T->d[POST[t]], s3, Ys[bi]);
    }

    double x[MAX_FRAME];
    double *O = st->O;
    if (mode == 0) {
        for (int h = 0; h < 2; h++)
            if (overlap_add_shaped(O, Ys[h], half, st->flags[h], st->shapes[h], st->flags[h + 1],
                                   st->shapes[h + 1], T->d[T_WIN], T->d[T_WIN_SQ],
                                   x + half * h) < 0)
                return -1;
    } else if (mode == 1) {
        overlap_add_plain(O, Ys[0], F, half, T->d[T_WIN_LONG], x);
    } else if (mode == 2) {
        overlap_add_plain(O, Ys[0], half, half, T->d[T_WIN], x);
        overlap_add_plain(O, Ys[1], half, F, T->d[T_WIN], x + half);
    } else {
        overlap_add_plain(O, Ys[0], F, F, T->d[T_WIN_LONG], x);
    }

    /* --- Long-term predictor over H[HISTORY..HISTORY+F-1] --- */
    static const int SECTIONS[4][3][2] = {   /* (length in quarter frames, lag slot) */
        {{1, 0}, {2, 1}, {1, 2}},
        {{2, 0}, {2, 2}, {0, 0}},
        {{1, 0}, {2, 1}, {1, 2}},
        {{2, 0}, {2, 2}, {0, 0}},
    };
    int n = HISTORY;
    for (int si = 0; si < 3; si++) {
        int length = SECTIONS[mode][si][0] * (F / 4), slot = SECTIONS[mode][si][1];
        if (length == 0)
            break;
        if (slot == 0)
            pitch_section(st->H, x, n, length, st->lag0, st->taps0);
        else if (slot == 1)
            pitch_section(st->H, x, n, length, lag1, taps1);
        else
            pitch_section(st->H, x, n, length, lag2, taps2);
        n += length;
    }

    /* --- LPC synthesis with interpolated coefficients --- */
    double y[MAX_ORDER + MAX_FRAME];
    memcpy(y, st->mem, order * sizeof(double));
    int count = order;
    const double *runA[2], *runB[2];
    int runs;
    if (mode == 0 && Fl == 1) {
        runA[0] = st->lsp0; runB[0] = l1;
        runA[1] = l1; runB[1] = l2;
        runs = 2;
    } else {
        runA[0] = st->lsp0; runB[0] = l2;
        runs = 1;
    }
    int steps = runs == 2 ? 8 : 16;
    int step_len = (runs == 2 ? half : F) / steps;
    int pos = HISTORY;
    for (int ri = 0; ri < runs; ri++) {
        double inv = 1.0 / steps;
        for (int k = 0; k <= steps; k++) {
            double tk = k * inv;
            int block_len = (k == 0 || k == steps) ? step_len / 2 : step_len;
            double l[MAX_ORDER + 1], a[MAX_ORDER + 1];
            interpolate_lsp(runA[ri], runB[ri], tk, order + 1, l);
            lsp_to_lpc(l, order, a);
            lpc_filter(st->H, pos, block_len, a, order, y, &count);
            pos += block_len;
        }
    }
    for (int i = 0; i < F; i++)
        if (to_int16(y[order + i], &samples[i]) < 0)
            return -1;

    /* --- End of frame --- */
    memcpy(st->H, st->H + F, HISTORY * sizeof(double));
    memcpy(st->mem, y + F, order * sizeof(double));
    memcpy(st->lsp0, l2, (order + 1) * sizeof(double));
    memcpy(st->q0, q2, (order + 1) * sizeof(i64));
    st->lag0 = lag2;
    memcpy(st->taps0, taps2, sizeof taps2);
    st->flags[0] = st->flags[2];
    st->shapes[0] = st->shapes[2];
    return used;
}

/* ------------------------------------------------------------------------
 * Exports
 * ---------------------------------------------------------------------- */

EXPORT int lpec_abi_version(void)
{
    return ABI_VERSION;
}

/*
 * Decode `nframes` packed frame records (`nints` int32s in all) from a
 * freshly initialised decoder of configuration `config` (`nconfig` int32s,
 * the layout of the CFG_* enum) into out[nframes * frame].
 * Returns the number of frames decoded, or -1 on a malformed record, an
 * unusable configuration or a table count mismatch.
 */
EXPORT int lpec_decode(const int32_t *config, int nconfig,
                       const double *const *dtables, int n_dtables,
                       const int32_t *const *itables, int n_itables,
                       const int32_t *frames, int nints, int nframes,
                       int16_t *out)
{
    Config cfg;
    if (config_from(config, nconfig, &cfg) < 0)
        return -1;
    if (n_dtables != N_DTABLES || n_itables != N_ITABLES)
        return -1;
    Tables T;
    memcpy(T.d, dtables, sizeof T.d);
    memcpy(T.i, itables, sizeof T.i);
    State *st = malloc(sizeof *st);
    if (!st)
        return -1;
    state_init(st, &T, &cfg);
    double s3 = sqrt(3.0) * 0.5;
    int pos = 0, fi;
    for (fi = 0; fi < nframes; fi++) {
        int used = decode_frame(st, &T, &cfg, frames + pos, nints - pos,
                                out + (ptrdiff_t)fi * cfg.frame, s3);
        if (used < 0) {
            fi = -1;
            break;
        }
        pos += used;
    }
    free(st);
    return fi;
}
