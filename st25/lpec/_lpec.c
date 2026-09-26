/*
 * LPEC decoder core: the per-frame parameter-to-PCM pipeline in C.
 *
 * An exact port of st25/lpec/params.py, synthesis.py, x87.py (fcos) and the
 * per-frame part of decoder.py; see docs/lpec.md. The frame fields come
 * already parsed (st25/lpec/bitstream.py, packed by st25/lpec/_core.py),
 * and the tables come from st25/lpec/tables.py.
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
#define ABI_VERSION 2

typedef int64_t i64;
typedef unsigned __int128 u128;
typedef __int128 s128;

/* ------------------------------------------------------------------------
 * x87 fcos emulation (st25/lpec/x87.py)
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
 * Tables (order fixed by st25/lpec/_core.py)
 * ---------------------------------------------------------------------- */

enum {
    T_C1, T_C2, T_C3, T_PT, T_SHAPES, T_GAIN, T_BG1, T_BG2, T_VQ2, T_VQ4,
    T_VQ8, T_NA, T_NB, T_WIN512, T_WIN512_SQ, T_WIN1024, T_FFT_SIN_2048,
    T_FFT_SIN_1536, T_POST256, T_POST384, T_POST512, T_LSP_INIT,
    T_DEFAULT_SHAPE,    /* 8 doubles, all 1.0: Tables.DEFAULT_SHAPE */
    N_DTABLES
};
enum { T_D1, T_D2, T_D3, T_PQ, T_S2048, T_S1536, N_ITABLES };

typedef struct {
    const double *d[N_DTABLES];
    const int32_t *i[N_ITABLES];
} Tables;

/* ------------------------------------------------------------------------
 * Configuration constants (params.py)
 * ---------------------------------------------------------------------- */

#define BANDS 8
#define ORDER 10
static const int TRANSFORM_N[4] = {512, 768, 768, 1024};
static const int BAND_WIDTH[4] = {28, 42, 42, 56};
static const int FIRST_CODED[4] = {2, 3, 3, 4};
static const int END_CODED[4] = {226, 339, 339, 452};
static const int OVERLAP[4] = {256, 256, 512, 512};
static const double Q15 = 3.0517578125e-05;
static const double PI_C = 3.14159265359;

/* Table row counts (st25/lpec/tables.py: C1/C2/C3/PT/PQ 64 rows, SHAPES/GAIN
 * 128, BG1/BG2 64, VQ2/VQ4/VQ8 256), i.e. the bitstream field widths that
 * index them (bitstream.py: 6-bit LSP/pgidx/band-gain indices, 7-bit
 * lag/global-gain/shape indices, 8-bit VQ indices). Frame records come from
 * this repo's own parser (st25/lpec/bitstream.py via _core.py's packing),
 * but decode_frame validates them defensively rather than trusting the
 * packed ints to stay in range.
 */
#define N_LSP 64        /* C1/C2/C3/D1/D2/D3 rows: LSP set indices i1/i2/i3 */
#define N_PT 64         /* PT/PQ rows: pgidx */
#define N_SHAPES 128    /* SHAPES rows: shape index */
#define N_GAIN 128      /* GAIN entries: global_gain */
#define N_BG 64         /* BG1/BG2 rows: band_gain1/band_gain2 */
#define N_VQ 256        /* VQ2/VQ4/VQ8 rows: raw 8-bit VQ indices */
#define MAX_LAG 127     /* 7-bit pitch lag field */

/* ------------------------------------------------------------------------
 * LSP decoding (params.py)
 * ---------------------------------------------------------------------- */

static void sort_pass_d(double *v)
{
    for (int j = 1; j < 11; j++) {
        if (v[j] < v[j - 1]) { double t = v[j]; v[j] = v[j - 1]; v[j - 1] = t; }
        if (j >= 2 && v[j - 2] > v[j - 1]) { double t = v[j - 2]; v[j - 2] = v[j - 1]; v[j - 1] = t; }
    }
}

static void sort_pass_i(i64 *v)
{
    for (int j = 1; j < 11; j++) {
        if (v[j] < v[j - 1]) { i64 t = v[j]; v[j] = v[j - 1]; v[j - 1] = t; }
        if (j >= 2 && v[j - 2] > v[j - 1]) { i64 t = v[j - 2]; v[j - 2] = v[j - 1]; v[j - 1] = t; }
    }
}

static void lsp_double(const Tables *T, int i1, int i2, int i3, double *l)
{
    const double *c1 = T->d[T_C1] + 10 * i1, *c2 = T->d[T_C2] + 10 * i2,
                 *c3 = T->d[T_C3] + 10 * i3;
    l[0] = 0.0;
    for (int j = 0; j < 10; j++)
        l[j + 1] = (c3[j] + c2[j]) + c1[j];

    double lim = 0.49;
    for (int j = 10; j > 5; j--) {
        if (l[j] >= 0.5)
            l[j] = lim;
        lim = l[j] - 0.01;
    }
    lim = 0.01;
    for (int j = 1; j < 6; j++) {
        if (l[j] < 0.0)
            l[j] = lim;
        lim = l[j] + 0.01;
    }
    l[0] = 0.0;
    sort_pass_d(l);
    for (int j = 1; j < 11; j++) {
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

static void lsp_q16(const Tables *T, int i1, int i2, int i3, i64 *q)
{
    const int32_t *d1 = T->i[T_D1] + 10 * i1, *d2 = T->i[T_D2] + 10 * i2,
                  *d3 = T->i[T_D3] + 10 * i3;
    q[0] = 0;
    for (int j = 0; j < 10; j++)
        q[j + 1] = s16(((i64)d3[j] + 2 * (i64)d2[j] + 4 * (i64)d1[j]) >> 2);

    i64 lim = 32112;
    for (int j = 10; j > 5; j--) {
        if (q[j] < 0)
            q[j] = s16(lim);
        lim = q[j] - 655;
    }
    lim = 655;
    for (int j = 1; j < 6; j++) {
        if (q[j] < 0)
            q[j] = s16(lim);
        lim = q[j] + 655;
    }
    q[0] = 0;
    sort_pass_i(q);
    if (q[1] < 655)
        q[1] = 655;
    for (int j = 2; j < 11; j++) {
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

static void lpc_q(const Tables *T, const i64 *q, i64 *a16)
{
    const int32_t *S = T->i[T_S2048];
    i64 c[11];
    for (int j = 0; j < 11; j++) {
        i64 e = i32(shl(q[j], 11));
        i64 f = (e & 0xFFFF) >> 1;
        i64 i = e >> 16;
        i64 u = pmod(i + 512, 2048);
        c[j] = s16(((32768 - f) * S[u] + S[u + 1] * f) >> 15);
    }

    i64 Q[11] = {0}, P[11] = {0}, TQ[11] = {0}, TP[11] = {0};
    Q[0] = -((i64)1 << 23);
    Q[1] = (i64)1 << 23;
    P[0] = -((i64)1 << 23);
    P[1] = -((i64)1 << 23);
    for (int i = 1; i < 6; i++) {
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

    i64 R[11];
    i64 A = 0;
    for (int j = 0; j < 11; j++) {
        R[j] = i32(-(P[j] + Q[j]));
        A |= R[j] < 0 ? -R[j] : R[j];
    }
    int d = 0;
    while (d < 16 && !(u32(shl(A, d)) & 0x40000000))
        d++;
    i64 rnd = d <= 15 ? ((i64)1 << (15 - d)) : 0;
    for (int j = 0; j < 11; j++)
        a16[j] = s16((R[j] + rnd) >> (16 - d));
}

static void autocorrelation(const i64 *a16, i64 *E0, i64 *E1, i64 *r)
{
    i64 lo = 0, hi = 0;
    for (int i = 0; i < 11; i++) {
        i64 sq = a16[i] * a16[i];
        lo += sq & 0x7FFF;
        hi += sq >> 15;
    }
    i64 l = lo & 0x7FFF;
    i64 h = hi + (u32(lo) >> 15);

    int s = 0;
    i64 ah = h < 0 ? -h : h;
    for (int n = 0; n < 20; n++) {
        if (shl(ah, n) & ((i64)1 << 18)) {
            s = 19 - n;
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
    for (int m = 1; m < 11; m++) {
        i64 slo = 0, shi = 0;
        for (int i = 0; i < 11 - m; i++) {
            i64 p = a16[i] * a16[i + m];
            slo += p & mask;
            shi += p >> s;
        }
        r[m] = s16(shi + (u32(slo) >> s));
    }
}

static void envelope(const Tables *T, const i64 *q, int lag, double pg, int N, i64 *w)
{
    int M = N / 2;
    i64 a16[11], E0, E1, r[11];
    lpc_q(T, q, a16);
    autocorrelation(a16, &E0, &E1, r);

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
        for (int m = 1; m < 11; m++) {
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
    int n4[BANDS], n2[BANDS], n1[BANDS], ns[BANDS];
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

static void coefficients(const Tables *T, const Block *blk, const i64 *q, int lag,
                         const int32_t *pq, int *noise_z, double *X)
{
    int t = blk->type;
    int N = TRANSFORM_N[t];
    int M = N / 2;
    int W = BAND_WIDTH[t];
    int s = FIRST_CODED[t];
    int e = END_CODED[t];

    double G = T->d[T_GAIN][blk->gg];
    const double *bg1 = T->d[T_BG1] + 8 * blk->bg1;
    const double *bg2 = T->d[T_BG2] + 8 * blk->bg2;
    double bg[BANDS];
    for (int b = 0; b < BANDS; b++) {
        double v = bg2[b] + bg1[b];
        if (v < 1e-05)
            v = 1e-05;
        bg[b] = v;
    }
    double pg = (double)pq[1] * Q15;

    i64 w[512];
    envelope(T, q, lag, pg, N, w);

    VQStream vq2 = {T->d[T_VQ2], blk->vq2, 2, 0, 0};
    VQStream vq4 = {T->d[T_VQ4], blk->vq4, 4, 0, 0};
    VQStream vq8 = {T->d[T_VQ8], blk->vq8, 8, 0, 0};
    int sign_i = 0;
    const double *NA = T->d[T_NA], *NB = T->d[T_NB];
    int z = *noise_z;

    for (int i = 0; i < M; i++)
        X[i] = 0.0;
    for (int b = 0; b < BANDS; b++) {
        int base = s + b * W;
        Rank order[56];
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
    for (int b = 0; b < BANDS; b++) {
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

static void fft384(double *re, double *im, const double *S, const double *U, double s3)
{
    const int m = 128;
    for (int j = 0; j < m; j++) {
        double x0 = re[j], x1 = re[j + m], x2 = re[j + 2 * m];
        double y0 = im[j], y1 = im[j + m], y2 = im[j + 2 * m];
        int k = 4 * j;
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

    double c = 1.0 / 384;
    double tr[3][128], ti[3][128];
    for (int p = 0; p < 3; p++) {
        memcpy(tr[p], re + p * m, sizeof tr[p]);
        memcpy(ti[p], im + p * m, sizeof ti[p]);
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
                              double s3, double *Y)
{
    int h = N / 2;
    int q = N / 4;
    const double *S;
    int sigma;
    if (N == 768) {
        S = T->d[T_FFT_SIN_1536];
        sigma = 2;
    } else if (N == 512) {
        S = T->d[T_FFT_SIN_2048];
        sigma = 4;
    } else {
        S = T->d[T_FFT_SIN_2048];
        sigma = 2;
    }

    double re[512], im[512];
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

    if (h == 384)
        fft384(re, im, T->d[T_FFT_SIN_2048], T->d[T_FFT_SIN_1536], s3);
    else
        fft_radix2(re, im, h, 1.0 / h, T->d[T_FFT_SIN_2048]);

    const double *w = N == 512 ? T->d[T_POST256] : N == 768 ? T->d[T_POST384] : T->d[T_POST512];
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

/* Returns -1 (Python would raise ZeroDivisionError, or later fail casting a
 * non-finite sample to int16) instead of letting a non-finite result run
 * through the rest of the pipeline undetected. */
static int overlap_add_shaped(double *O, const double *Y, int fp, const double *Ap,
                              int fc, const double *Ac, const double *v,
                              const double *v2, double *out)
{
    if (fp == 0 && fc == 0) {
        for (int j = 0; j < 256; j++) {
            out[j] = (O[j] * v[256 + j]) + (v[j] * Y[j]);
            if (!isfinite(out[j]))
                return -1;
        }
    } else {
        for (int r = 0; r < 4; r++) {
            double ap = Ap[7 - r];
            double ac = Ac[3 - r];
            double rho1 = ap / Ap[4 + r];
            double rho2 = ac / Ac[r];
            for (int j = 64 * r; j < 64 * r + 64; j++) {
                out[j] = ((O[j] * (ap * v[256 + j])) + ((ac * v[j]) * Y[j])) /
                         ((v2[256 + j] * rho1) + (v2[j] * rho2));
                if (!isfinite(out[j]))
                    return -1;
            }
        }
    }
    memcpy(O, Y + 256, 256 * sizeof(double));
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
    double c[130], r[130], nr[130];
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
            H[n] = x[n - 256];
        return;
    }
    double b0 = taps[0], b1 = taps[1], b2 = taps[2];
    if (!taps_stable(lag, taps)) {
        b0 = 0.0;
        b2 = 0.0;
    }
    for (int n = start; n < start + length; n++)
        H[n] = (((b2 * H[n - lag - 1]) + x[n - 256]) + (b1 * H[n - lag])) + (H[n - lag + 1] * b0);
}

/* ------------------------------------------------------------------------
 * LPC synthesis (synthesis.py)
 * ---------------------------------------------------------------------- */

static void lsp_to_lpc(const double *l, double *a)
{
    double C[12] = {0};
    for (int j = 1; j < 12; j++)
        C[j] = lpec_fcos((l[j - 1] * PI_C) * 2.0);
    double P[11] = {0}, Q[11] = {0}, TQ[11] = {0}, TP[11] = {0};
    P[0] = -1.0;
    P[1] = -1.0;
    Q[0] = -1.0;
    Q[1] = 1.0;
    for (int i = 2; i <= 10; i += 2) {
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
    for (int m = 0; m < 11; m++)
        a[m] = (Q[m] + P[m]) * (-0.5);
}

static void interpolate_lsp(const double *lA, const double *lB, double t, double *l)
{
    for (int j = 0; j < 11; j++)
        l[j] = (lA[j] * (1.0 - t)) + (lB[j] * t);
}

/* y[k] for k = count..count+length-1; y[count-10..count-1] is the memory. */
static void lpc_filter(const double *e, int start, int length, const double *a,
                       double *y, int *count)
{
    int k = *count;
    for (int n = start; n < start + length; n++, k++) {
        double v = e[n];
        v = v - (y[k - 1] * a[1]);
        v = v - (y[k - 2] * a[2]);
        v = v - (y[k - 3] * a[3]);
        v = v - (y[k - 4] * a[4]);
        v = v - (y[k - 5] * a[5]);
        v = v - (y[k - 6] * a[6]);
        v = v - (y[k - 7] * a[7]);
        v = v - (y[k - 8] * a[8]);
        v = v - (y[k - 9] * a[9]);
        v = v - (y[k - 10] * a[10]);
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
    double H[768];
    double lsp0[11];
    i64 q0[11];
    int lag0;
    double taps0[3];
    int flags[3];
    const double *shapes[3];
    double mem[ORDER];
    double O[512];
} State;

static void state_init(State *st, const Tables *T)
{
    memset(st, 0, sizeof *st);
    memcpy(st->lsp0, T->d[T_LSP_INIT], sizeof st->lsp0);
    for (int j = 0; j < 11; j++)
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

/* Frame record layout: see st25/lpec/_core.py (pack_frame). */
#define HDR 17

static int decode_frame(State *st, const Tables *T, const int32_t *f, int avail,
                        int16_t *samples, double s3)
{
    if (avail < HDR)
        return -1;
    int mode = f[0], F = f[1];
    int nblocks = f[16];
    if (mode < 0 || mode > 3 || (F != 0 && F != 1))
        return -1;
    /* mode<->nblocks: modes 0 and 2 always carry a slot-1 and a slot-2
     * block, modes 1 and 3 only slot-2 (bitstream.py: _parse_mode0/2 append
     * two blocks, _parse_mode1/3 one) -- the only shapes pack_frame emits. */
    int expected_blocks = (mode == 0 || mode == 2) ? 2 : 1;
    if (nblocks != expected_blocks)
        return -1;

    /* LSP set indices (i1/i2/i3): 6-bit fields, table rows 0..N_LSP-1.
     * f[5..7] (slot A) are always present in the record, defaulted to
     * (0, 0, 0) by pack_frame when the frame has no slot-1 LSP set, so
     * checking them unconditionally matches what a real frame ever holds. */
    if (f[2] < 0 || f[2] >= N_LSP || f[3] < 0 || f[3] >= N_LSP ||
        f[4] < 0 || f[4] >= N_LSP)
        return -1;
    if (f[5] < 0 || f[5] >= N_LSP || f[6] < 0 || f[6] >= N_LSP ||
        f[7] < 0 || f[7] >= N_LSP)
        return -1;

    /* Pitch lag: 7-bit field. pgidx is a 6-bit field only actually read
     * (into a table index) when lag != 0 -- _read_pitch never reads it
     * otherwise, and pack_frame stores 0 for it either way. */
    int lag1 = f[8], lag2 = f[10];
    if (lag1 < 0 || lag1 > MAX_LAG || lag2 < 0 || lag2 > MAX_LAG)
        return -1;
    if (lag1 != 0 && (f[9] < 0 || f[9] >= N_PT))
        return -1;
    if (lag2 != 0 && (f[11] < 0 || f[11] >= N_PT))
        return -1;

    /* Shape flags/indices: mode 0 only; the index is only read (and only
     * meaningful) when its flag is set (_parse_mode0). */
    if (mode == 0) {
        if ((f[12] != 0 && f[12] != 1) || (f[13] != 0 && f[13] != 1))
            return -1;
        if (f[12] && (f[14] < 0 || f[14] >= N_SHAPES))
            return -1;
        if (f[13] && (f[15] < 0 || f[15] >= N_SHAPES))
            return -1;
    }

    int used = HDR;
    Block blocks[2];
    for (int bi = 0; bi < nblocks; bi++) {
        Block *b = &blocks[bi];
        if (avail - used < 41)
            return -1;
        const int32_t *p = f + used;
        b->slot = p[0];
        b->type = p[1];
        b->gg = p[2];
        b->bg1 = p[3];
        b->bg2 = p[4];
        for (int k = 0; k < BANDS; k++) {
            b->n4[k] = p[5 + k];
            b->n2[k] = p[13 + k];
            b->n1[k] = p[21 + k];
            b->ns[k] = p[29 + k];
        }
        b->k0 = p[37];
        b->k1 = p[38];
        b->k2 = p[39];
        b->ks = p[40];
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
        if (b->gg < 0 || b->gg >= N_GAIN || b->bg1 < 0 || b->bg1 >= N_BG ||
            b->bg2 < 0 || b->bg2 >= N_BG)
            return -1;
        /* Per band, n4+n2+n1+ns must not exceed the band width W: coefficients()
         * ranks exactly W positions per band into a W-sized prefix of order[56],
         * so an over-budget band would read past the entries it filled (and,
         * for W < 56, could still write X[] through garbage stack indices).
         * The totals must also match k0/k1/k2 exactly (VQ2 dim 2, VQ4 dim 4,
         * VQ8 dim 8; bitstream.bit_allocation always produces this): each
         * band's n4/n2/n1 quota is exactly what advances vq_next() by, so a
         * mismatch would run a VQStream past the vq2/vq4/vq8 arrays the
         * record actually provided (already bounds-checked above only for
         * their combined length, not per-class use). */
        int W = BAND_WIDTH[b->type];
        i64 sum_n4 = 0, sum_n2 = 0, sum_n1 = 0, sum_ns = 0;
        for (int k = 0; k < BANDS; k++) {
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
        used += 41;
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
    double l1[11], l2[11];
    i64 q1[11], q2[11];
    lsp_double(T, f[2], f[3], f[4], l2);
    lsp_q16(T, f[2], f[3], f[4], q2);
    if (mode == 0 && F == 1) {
        lsp_double(T, f[5], f[6], f[7], l1);
        lsp_q16(T, f[5], f[6], f[7], q1);
    } else if (mode == 0 || mode == 2) {
        for (int j = 0; j < 11; j++) {
            l1[j] = (l2[j] + st->lsp0[j]) * 0.5;
            q1[j] = s16(cdiv(st->q0[j] + q2[j], 2));
        }
    }

    double taps1[3], taps2[3];
    const int32_t *pq1, *pq2;
    pitch_params(T, lag1, f[9], taps1, &pq1);
    pitch_params(T, lag2, f[11], taps2, &pq2);

    /* --- Shape state --- */
    if (mode == 0) {
        for (int h = 0; h < 2; h++) {
            st->flags[1 + h] = f[12 + h];
            st->shapes[1 + h] = f[12 + h] ? T->d[T_SHAPES] + 8 * f[14 + h]
                                          : T->d[T_DEFAULT_SHAPE];
        }
    } else if (mode == 2) {
        st->flags[1] = st->flags[2] = 0;
        st->shapes[1] = st->shapes[2] = T->d[T_DEFAULT_SHAPE];
    } else if (mode == 3) {
        st->shapes[1] = st->shapes[2] = T->d[T_DEFAULT_SHAPE];
    }

    /* --- Coefficient blocks -> transforms --- */
    double Ys[2][1024];
    for (int bi = 0; bi < nblocks; bi++) {
        const Block *b = &blocks[bi];
        double X[512];
        int N = TRANSFORM_N[b->type];
        if (b->slot == 1)
            coefficients(T, b, q1, lag1, pq1, &st->noise_z, X);
        else
            coefficients(T, b, q2, lag2, pq2, &st->noise_z, X);
        inverse_transform(T, X, N, OVERLAP[b->type], s3, Ys[bi]);
    }

    double x[512];
    double *O = st->O;
    if (mode == 0) {
        for (int h = 0; h < 2; h++)
            if (overlap_add_shaped(O, Ys[h], st->flags[h], st->shapes[h], st->flags[h + 1],
                                   st->shapes[h + 1], T->d[T_WIN512], T->d[T_WIN512_SQ],
                                   x + 256 * h) < 0)
                return -1;
    } else if (mode == 1) {
        overlap_add_plain(O, Ys[0], 512, 256, T->d[T_WIN1024], x);
    } else if (mode == 2) {
        overlap_add_plain(O, Ys[0], 256, 256, T->d[T_WIN512], x);
        overlap_add_plain(O, Ys[1], 256, 512, T->d[T_WIN512], x + 256);
    } else {
        overlap_add_plain(O, Ys[0], 512, 512, T->d[T_WIN1024], x);
    }

    /* --- Long-term predictor over H[256..767] --- */
    static const int SECTIONS[4][3][2] = {
        {{128, 0}, {256, 1}, {128, 2}},
        {{256, 0}, {256, 2}, {0, 0}},
        {{128, 0}, {256, 1}, {128, 2}},
        {{256, 0}, {256, 2}, {0, 0}},
    };
    int n = 256;
    for (int si = 0; si < 3; si++) {
        int length = SECTIONS[mode][si][0], slot = SECTIONS[mode][si][1];
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
    double y[ORDER + 512];
    memcpy(y, st->mem, sizeof st->mem);
    int count = ORDER;
    const double *runA[2], *runB[2];
    int runs;
    if (mode == 0 && F == 1) {
        runA[0] = st->lsp0; runB[0] = l1;
        runA[1] = l1; runB[1] = l2;
        runs = 2;
    } else {
        runA[0] = st->lsp0; runB[0] = l2;
        runs = 1;
    }
    int steps = runs == 2 ? 8 : 16;
    int pos = 256;
    for (int ri = 0; ri < runs; ri++) {
        double inv = 1.0 / steps;
        for (int k = 0; k <= steps; k++) {
            double tk = k * inv;
            int block_len = (k == 0 || k == steps) ? 16 : 32;
            double l[11], a[11];
            interpolate_lsp(runA[ri], runB[ri], tk, l);
            lsp_to_lpc(l, a);
            lpc_filter(st->H, pos, block_len, a, y, &count);
            pos += block_len;
        }
    }
    for (int i = 0; i < 512; i++)
        if (to_int16(y[ORDER + i], &samples[i]) < 0)
            return -1;

    /* --- End of frame --- */
    memcpy(st->H, st->H + 512, 256 * sizeof(double));
    memcpy(st->mem, y + 512, ORDER * sizeof(double));
    memcpy(st->lsp0, l2, sizeof l2);
    memcpy(st->q0, q2, sizeof q2);
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
 * freshly initialised decoder into out[nframes * 512].
 * Returns the number of frames decoded, or -1 on a malformed record or a
 * table count mismatch.
 */
EXPORT int lpec_decode(const double *const *dtables, int n_dtables,
                       const int32_t *const *itables, int n_itables,
                       const int32_t *frames, int nints, int nframes,
                       int16_t *out)
{
    if (n_dtables != N_DTABLES || n_itables != N_ITABLES)
        return -1;
    Tables T;
    memcpy(T.d, dtables, sizeof T.d);
    memcpy(T.i, itables, sizeof T.i);
    State *st = malloc(sizeof *st);
    if (!st)
        return -1;
    state_init(st, &T);
    double s3 = sqrt(3.0) * 0.5;
    int pos = 0, fi;
    for (fi = 0; fi < nframes; fi++) {
        int used = decode_frame(st, &T, frames + pos, nints - pos, out + (ptrdiff_t)fi * 512, s3);
        if (used < 0) {
            fi = -1;
            break;
        }
        pos += used;
    }
    free(st);
    return fi;
}
