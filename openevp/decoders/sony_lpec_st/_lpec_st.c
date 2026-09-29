/* Optional C core for the LPEC ST decoder (openevp.decoders.sony_lpec_st).
 *
 * Runs the synthesis of dsp.py (dequantisation, power compensation, stereo,
 * IMDCT, gain compensation, tones, noise, the 16-band filter bank) and the
 * int16 conversion of decoder.to_pcm, bit for bit like the pure-Python
 * modules. Python still parses the bitstream (bitstream.py) and hands each
 * parsed frame over as one packed int32 record (_core.pack_unit).
 *
 * Arithmetic (DECODER.md, "Arithmetic"): every operation dsp.py does on a
 * Python float is one IEEE double operation here, in the same order, and
 * every f32()/_r() rounding is a (float) conversion. So this must be built
 * without fused multiply-add and without fast-math reassociation:
 * tools/build_lpec_core.py passes -ffp-contract=off -fno-fast-math, and
 * x86-64 GCC does double arithmetic in SSE2 (no x87 excess precision).
 *
 * The tables come from Python (tables.load(): the extracted ones and the
 * two generated at load time), copied into the decoder state by lst_init.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define ABI_VERSION 1
#define EXPORT __declspec(dllexport)

/* ---- tables (order: the D_* / I_* lists in _core.py) ------------------- */
enum {
    D_SF, D_WL_MANT, D_PWR_LEVELS, D_IMDCT_C, D_IMDCT_S, D_WINDOWS, D_GAIN_STEP,
    D_TONE_RAMP, D_AMP_SF, D_AMP_IDX, D_QMF_PRE, D_QMF_DCT, D_QMF_WIN, D_TONE_SINE,
    D_TONE_WINDOW, D_SCALARS, D_COUNT
};
static const int D_LEN[D_COUNT] = {64, 8, 16, 127, 127, 1024, 96, 4, 64, 16, 16, 19, 192,
                                   2048, 256, 4};
enum {
    I_QU_START, I_QU_LEN, I_SB_QU, I_SB_REVERSE, I_GAIN_EXP, I_SB_POWGRP, I_PWR_SB_QU,
    I_NOISE, I_NOISE_OFS, I_IMDCT_ORDER, I_COUNT
};
static const int I_LEN[I_COUNT] = {33, 32, 17, 16, 16, 16, 16, 1024, 48, 128};

typedef struct {
    double SF[64], MANT[8], PWR[16], IC[127], IS[127], WIN[4][256], STEP[96], RAMP[4];
    double AMP_SF[64], AMP_IDX[16], PRE[16], K[19], QW[192], SINE[2048], TWIN[256];
    double Q15, HALF, MINUS_ONE, ZERO;
    int QS[33], QL[32], SBQU[17], SBREV[16], GE[16], POWGRP[16], PWRQU[16];
    int NOISE[1024], NOISE_OFS[48], ORDER[128];
    /* IMDCT plan: 6 stages of 32 butterflies (a, b, cos, sin) */
    int pa[6][32], pb[6][32];
    double pc[6][32], ps[6][32];
} Tables;

/* ---- the packed frame record (_core.pack_unit) -------------------------- */
#define MAXW 48                     /* waves per band: at most 48 in a frame (bitstream.py) */
#define U_HDR 9                     /* ncoded_qu ncoded_sb nsb mute noise_present
                                       noise_level noise_table tones_present amp_mode */
#define U_FLAGS (4 * 16)            /* swap, negate, tone_negate, share_prev_env */
#define G_REC 15                    /* npoints, loc[7], lev[7] */
#define T_REC (6 + 4 * MAXW)        /* has_start has_stop start_pos stop_pos nwavs nw,
                                       then amp_sf, amp_idx, phase, freq [MAXW] each */
#define C_REC (32 * 3 + 5 + 16 + 16 * G_REC + 16 * T_REC + 2048)
#define RECORD (U_HDR + U_FLAGS + 2 * C_REC)

typedef struct { int npoints, loc[7], lev[7]; } Gain;
typedef struct {
    int has_start, has_stop, start_pos, stop_pos, nwavs, nw;
    int amp_sf[MAXW], amp_idx[MAXW], phase[MAXW], freq[MAXW];
    int w_start_on, w_stop_on, w_start, w_stop;
} Tone;
typedef struct {
    float ovl[16][128];
    float x_last[16];
    float b_hist[11][8][2];
    Gain gain[16];
    int wnd[16];
    Tone tones[16];
} ChannelState;
typedef struct { int present, amp_mode, negate[16]; } ToneInfo;

typedef struct {
    int wl[32], sf[32], ct[32], power[5], wnd[16];
    Gain gain[16];
    Tone tones[16];
    int spec[2048];
} Chan;
typedef struct {
    int ncoded_qu, ncoded_sb, nsb, mute, noise_present, noise_level, noise_table;
    int tones_present, amp_mode;
    int swap[16], negate[16], tone_negate[16], share[16];
    int noise_counter;
    Chan ch[2];
} Unit;

typedef struct {
    Tables t;
    ChannelState st[2];
    ToneInfo prevT;
    Unit u;                         /* scratch: the frame being decoded */
    double spectra[2][2048];        /* scratch: its two spectra */
    double X[16][128];              /* scratch: filter-bank butterfly output */
} Decoder;

static inline double R(double x) { return (double)(float)x; }

/* error codes (negative returns of lst_frame) */
#define E_RECORD -1                 /* the record has the wrong length or a field out of range */
#define E_RANGE -2                  /* an index Python would have rejected (IndexError) */

/* ---- init / reset ------------------------------------------------------- */

static void tone_init(Tone *b) {
    memset(b, 0, sizeof *b);
    b->stop_pos = 0x20;
}

static void reset(Decoder *d) {
    for (int c = 0; c < 2; c++) {
        ChannelState *s = &d->st[c];
        memset(s, 0, sizeof *s);
        for (int b = 0; b < 16; b++) tone_init(&s->tones[b]);
    }
    memset(&d->prevT, 0, sizeof d->prevT);
}

static void imdct_plan(Tables *t) {
    int tw = 63;
    int si = 0;
    for (int st = 5; st >= 0; st--, si++) {
        int half = 1 << st, span = 1 << (st + 1);
        int a = 0, b = span, k = 0;
        for (int g = 0; g < 128 / (4 * half); g++) {
            for (int j = 0; j < half; j++) {
                t->pa[si][k] = a;
                t->pb[si][k] = b;
                t->pc[si][k] = t->IC[tw - half + j];
                t->ps[si][k] = t->IS[tw - half + j];
                k++;
                a += 2;
                b += 2;
            }
            a += span;
            b += span;
        }
        tw -= half;
    }
}

EXPORT int lst_abi_version(void) { return ABI_VERSION; }
EXPORT int lst_state_size(void) { return (int)sizeof(Decoder); }
EXPORT int lst_record_size(void) { return RECORD; }

/* Copy the tables into the state at ``mem`` (lst_state_size() bytes) and
 * reset it. Returns 0, or -1 when the table lists do not have the expected
 * count or an index table holds an out-of-range entry. */
EXPORT int lst_init(void *mem, const double *const *dt, const int *dlen, int nd,
                    const int32_t *const *it, const int *ilen, int ni) {
    if (nd != D_COUNT || ni != I_COUNT) return -1;
    for (int i = 0; i < D_COUNT; i++) if (dlen[i] != D_LEN[i]) return -1;
    for (int i = 0; i < I_COUNT; i++) if (ilen[i] != I_LEN[i]) return -1;
    Decoder *d = (Decoder *)mem;
    Tables *t = &d->t;
    memcpy(t->SF, dt[D_SF], sizeof t->SF);
    memcpy(t->MANT, dt[D_WL_MANT], sizeof t->MANT);
    memcpy(t->PWR, dt[D_PWR_LEVELS], sizeof t->PWR);
    memcpy(t->IC, dt[D_IMDCT_C], sizeof t->IC);
    memcpy(t->IS, dt[D_IMDCT_S], sizeof t->IS);
    memcpy(t->WIN, dt[D_WINDOWS], sizeof t->WIN);
    memcpy(t->STEP, dt[D_GAIN_STEP], sizeof t->STEP);
    memcpy(t->RAMP, dt[D_TONE_RAMP], sizeof t->RAMP);
    memcpy(t->AMP_SF, dt[D_AMP_SF], sizeof t->AMP_SF);
    memcpy(t->AMP_IDX, dt[D_AMP_IDX], sizeof t->AMP_IDX);
    memcpy(t->PRE, dt[D_QMF_PRE], sizeof t->PRE);
    memcpy(t->K, dt[D_QMF_DCT], sizeof t->K);
    memcpy(t->QW, dt[D_QMF_WIN], sizeof t->QW);
    memcpy(t->SINE, dt[D_TONE_SINE], sizeof t->SINE);
    memcpy(t->TWIN, dt[D_TONE_WINDOW], sizeof t->TWIN);
    t->Q15 = dt[D_SCALARS][0];
    t->HALF = dt[D_SCALARS][1];
    t->MINUS_ONE = dt[D_SCALARS][2];
    t->ZERO = dt[D_SCALARS][3];
    for (int i = 0; i < 33; i++) t->QS[i] = it[I_QU_START][i];
    for (int i = 0; i < 32; i++) t->QL[i] = it[I_QU_LEN][i];
    for (int i = 0; i < 17; i++) t->SBQU[i] = it[I_SB_QU][i];
    for (int i = 0; i < 16; i++) {
        t->SBREV[i] = it[I_SB_REVERSE][i];
        t->GE[i] = it[I_GAIN_EXP][i];
        t->POWGRP[i] = it[I_SB_POWGRP][i];
        t->PWRQU[i] = it[I_PWR_SB_QU][i];
    }
    for (int i = 0; i < 1024; i++) t->NOISE[i] = it[I_NOISE][i];
    for (int i = 0; i < 48; i++) t->NOISE_OFS[i] = it[I_NOISE_OFS][i];
    for (int i = 0; i < 128; i++) t->ORDER[i] = it[I_IMDCT_ORDER][i];
    /* every index the synthesis takes from these tables must stay in bounds */
    for (int i = 0; i < 33; i++) if (t->QS[i] < 0 || t->QS[i] > 2048) return -1;
    for (int i = 0; i < 32; i++) {
        if (t->QS[i] > t->QS[i + 1] || t->QL[i] < 0 || t->QS[i] + t->QL[i] > 2048) return -1;
    }
    for (int i = 0; i < 17; i++) if (t->SBQU[i] < 0 || t->SBQU[i] > 32) return -1;
    for (int i = 0; i < 16; i++) {
        if (t->POWGRP[i] < 0 || t->POWGRP[i] > 4) return -1;
        if (t->PWRQU[i] < 0 || t->PWRQU[i] > 32) return -1;
    }
    for (int i = 0; i < 48; i++) if (t->NOISE_OFS[i] < 0 || t->NOISE_OFS[i] + 128 > 1024) return -1;
    for (int i = 0; i < 128; i++) if (t->ORDER[i] < 0 || t->ORDER[i] > 127) return -1;
    imdct_plan(t);
    reset(d);
    return 0;
}

EXPORT void lst_reset(void *mem) { reset((Decoder *)mem); }

/* ---- unpacking ------------------------------------------------------------ */

static int in(int v, int lo, int hi) { return v >= lo && v <= hi; }

static int unpack(Unit *u, const int32_t *r) {
    u->ncoded_qu = *r++;
    u->ncoded_sb = *r++;
    u->nsb = *r++;
    u->mute = *r++;
    u->noise_present = *r++;
    u->noise_level = *r++;
    u->noise_table = *r++;
    u->tones_present = *r++;
    u->amp_mode = *r++;
    if (!in(u->ncoded_qu, 0, 32) || !in(u->ncoded_sb, 0, 16) || !in(u->nsb, 0, 16) ||
        !in(u->noise_level, 0, 30) || !in(u->noise_table, 0, 15)) return E_RECORD;
    for (int i = 0; i < 16; i++) u->swap[i] = *r++;
    for (int i = 0; i < 16; i++) u->negate[i] = *r++;
    for (int i = 0; i < 16; i++) u->tone_negate[i] = *r++;
    for (int i = 0; i < 16; i++) u->share[i] = *r++;
    for (int c = 0; c < 2; c++) {
        Chan *ch = &u->ch[c];
        for (int i = 0; i < 32; i++) { ch->wl[i] = *r++; if (!in(ch->wl[i], 0, 7)) return E_RECORD; }
        for (int i = 0; i < 32; i++) { ch->sf[i] = *r++; if (!in(ch->sf[i], 0, 63)) return E_RECORD; }
        for (int i = 0; i < 32; i++) ch->ct[i] = *r++;
        for (int i = 0; i < 5; i++) { ch->power[i] = *r++; if (!in(ch->power[i], 0, 15)) return E_RECORD; }
        for (int i = 0; i < 16; i++) ch->wnd[i] = *r++;
        for (int b = 0; b < 16; b++) {
            Gain *g = &ch->gain[b];
            g->npoints = *r++;
            if (!in(g->npoints, 0, 7)) return E_RECORD;
            for (int i = 0; i < 7; i++) { g->loc[i] = *r++; if (!in(g->loc[i], 0, 31)) return E_RECORD; }
            for (int i = 0; i < 7; i++) { g->lev[i] = *r++; if (!in(g->lev[i], 0, 15)) return E_RECORD; }
        }
        for (int b = 0; b < 16; b++) {
            Tone *tb = &ch->tones[b];
            tone_init(tb);
            tb->has_start = *r++;
            tb->has_stop = *r++;
            tb->start_pos = *r++;
            tb->stop_pos = *r++;
            tb->nwavs = *r++;
            tb->nw = *r++;
            if (!in(tb->start_pos, -1, 31) || !in(tb->stop_pos, 0, 32) || !in(tb->nw, 0, MAXW))
                return E_RECORD;
            for (int i = 0; i < MAXW; i++) { tb->amp_sf[i] = *r++; if (!in(tb->amp_sf[i], 0, 63)) return E_RECORD; }
            for (int i = 0; i < MAXW; i++) { tb->amp_idx[i] = *r++; if (!in(tb->amp_idx[i], 0, 15)) return E_RECORD; }
            for (int i = 0; i < MAXW; i++) tb->phase[i] = *r++;
            for (int i = 0; i < MAXW; i++) tb->freq[i] = *r++;
        }
        for (int i = 0; i < 2048; i++) ch->spec[i] = *r++;
    }
    return 0;
}

/* ---- dequantisation and power compensation (dsp.dequantize) ------------- */

static int s8(int v) {
    v &= 0xFF;
    return (v & 0x80) ? v - 256 : v;
}

static int gain_shift(const Gain *prev, const Gain *cur, const int *GE) {
    int base = cur->npoints >= 1 ? -GE[cur->lev[0]] : 0;
    int m = 0;
    for (int i = 0; i < prev->npoints; i++) {
        int v = s8(base - GE[prev->lev[i]]);
        if (v > m) m = v;
    }
    for (int i = 0; i < cur->npoints; i++) {
        int v = s8(-GE[cur->lev[i]]);
        if (v > m) m = v;
    }
    return m;
}

static void power_comp(const Unit *u, int ci, double *spec, int pos, int sb,
                       ChannelState *const st, const Tables *t) {
    int src = ci;
    if (u->swap[sb] != 0) src = 1 - ci;
    const Chan *sc = &u->ch[src];
    int level = sc->power[t->POWGRP[sb]];
    double pwr = t->PWR[level];
    if (!(pwr > t->ZERO)) return;
    double tmp[128];
    for (int i = 0; i < 128; i++) tmp[i] = R((double)t->NOISE[(pos + i) & 0x3FF] * t->Q15);
    int m = gain_shift(&st[src].gain[sb], &sc->gain[sb], t->GE);
    double pw = R(pwr / ldexp(1.0, m));
    const Chan *c = &u->ch[ci];
    for (int qu = t->PWRQU[sb]; qu < t->SBQU[sb + 1]; qu++) {
        int wl = c->wl[qu];
        if (wl <= 0) continue;
        int sf = c->sf[qu];
        double scale = R(((t->SF[sf] * t->MANT[wl]) * pw) / (double)(1 << wl));
        int a = t->QS[qu], b = t->QS[qu + 1];
        for (int j = 0; j < b - a; j++) spec[a + j] = R(scale * tmp[j] + spec[a + j]);
    }
}

/* spectra: [2][2048] */
static void dequantize(Unit *u, ChannelState *st, const Tables *t, double spectra[2][2048]) {
    Chan *ch0 = &u->ch[0], *ch1 = &u->ch[1];
    int n = u->ncoded_qu;
    unsigned acc = 0;
    for (int c = 0; c < 2; c++)
        for (int i = 0; i < n; i++) acc = (acc + (unsigned)u->ch[c].sf[i]) & 0xFFFF;
    int npos[16];
    acc &= 0x3FC;
    for (int sb = 0; sb < u->ncoded_sb; sb++) {
        npos[sb] = (int)acc;
        acc = (acc + 0x80) & 0x3FC;
    }
    for (int qu = 0; qu < n; qu++) {
        if (ch1->wl[qu] == 0 && ch0->wl[qu] > 0 && ch1->ct[qu] == 0) {
            int a = t->QS[qu];
            memcpy(&ch1->spec[a], &ch0->spec[a], sizeof(int) * (size_t)t->QL[qu]);
            ch1->wl[qu] = ch0->wl[qu];
        }
    }
    for (int ci = 0; ci < 2; ci++) {
        Chan *c = &u->ch[ci];
        double *spec = spectra[ci];
        for (int i = 0; i < 2048; i++) spec[i] = 0.0;
        for (int qu = 0; qu < n; qu++) {
            int wl = c->wl[qu];
            if (wl > 0) {
                double scale = R(t->SF[c->sf[qu]] * t->MANT[wl]);
                int a = t->QS[qu];
                for (int i = a; i < a + t->QL[qu]; i++) spec[i] = R((double)c->spec[i] * scale);
            }
        }
        for (int sb = 0; sb < u->ncoded_sb; sb++) power_comp(u, ci, spec, npos[sb], sb, st, t);
    }
    double *s0 = spectra[0], *s1 = spectra[1];
    for (int sb = 0; sb < u->ncoded_sb; sb++) {
        if (u->swap[sb] == 1) {
            for (int i = sb * 128; i < sb * 128 + 128; i++) {
                double x = s0[i];
                s0[i] = s1[i];
                s1[i] = x;
            }
        }
        if (u->negate[sb] == 1) {
            for (int qu = t->SBQU[sb]; qu < t->SBQU[sb + 1]; qu++)
                for (int i = t->QS[qu]; i < t->QS[qu + 1]; i++) s1[i] = -s1[i];
        }
    }
    if (u->mute == 1) {
        for (int i = 0; i < 2048; i++) s0[i] = s1[i] = 0.0;
    }
}

/* ---- IMDCT (dsp.imdct) -------------------------------------------------- */

static void imdct(const double *X, const double *W, int rev, const Tables *t, double *Y) {
    double Z[128], N[128];
    for (int k = 0; k < 64; k++) {
        double uu, v;
        if (rev) { uu = X[127 - 2 * k]; v = X[2 * k]; }
        else { uu = X[2 * k]; v = X[127 - 2 * k]; }
        double c = t->IC[63 + k], s = t->IS[63 + k];
        Z[2 * k] = R(uu * c + v * s);
        Z[2 * k + 1] = R(uu * s - v * c);
    }
    for (int si = 0; si < 6; si++) {
        for (int i = 0; i < 128; i++) N[i] = 0.0;
        for (int k = 0; k < 32; k++) {
            int a = t->pa[si][k], b = t->pb[si][k];
            double c = t->pc[si][k], s = t->ps[si][k];
            double za = Z[a], za1 = Z[a + 1], zb = Z[b], zb1 = Z[b + 1];
            double t2 = za - zb;
            double t3 = za1 - zb1;
            N[a] = R(zb + za);
            N[a + 1] = R(zb1 + za1);
            N[b] = R(t2 * c + t3 * s);
            N[b + 1] = R(t2 * s - t3 * c);
        }
        memcpy(Z, N, sizeof Z);
    }
    const int *O = t->ORDER;
    for (int k = 0; k < 64; k++) {
        Y[k] = R(Z[O[k + 64]] * W[k]);
        Y[192 + k] = R(-(Z[O[k]] * W[192 + k]));
    }
    for (int j = 0; j < 128; j++) Y[64 + j] = R(-(Z[O[127 - j]] * W[64 + j]));
}

/* ---- gain compensation curve (dsp.gain_curve) --------------------------- */

static double pow2neg(int e) {
    if (-e >= 0) return (double)((uint64_t)1 << (-e & 31));
    return 1.0 / (double)((uint64_t)1 << (e & 31));
}

/* Returns the first-change index (0xFF if none), or E_RANGE. */
static int gain_curve(const Gain *prev, const Gain *cur, const Tables *t, double *gc) {
    const int *GE = t->GE;
    const double *STEP = t->STEP;
    int E[64];
    memset(E, 0, sizeof E);
    int fill = 0;
    for (int p = 0; p < cur->npoints; p++) {
        int end = cur->loc[p] + 0x20;
        int val = GE[cur->lev[p]];
        if (fill <= end) {
            for (int i = fill; i <= end; i++) E[i] = val;
            fill = end + 1;
        }
    }
    int k = 0;
    for (int p = 0; p < prev->npoints; p++) {
        int end = prev->loc[p];
        int val = GE[prev->lev[p]];
        while (k <= end) {
            E[k] += val;
            k++;
        }
    }
    int level = 0;
    double st = pow2neg(E[63]);
    int e = 0xFF;
    int first = 0x100;
    for (int idx = 63; idx >= 0; idx--) {
        int v = E[idx];
        if (v == level) {
            double fs = R(st);
            gc[e] = fs;
            gc[e - 1] = fs;
            gc[e - 2] = fs;
            e -= 3;
        } else {
            if (first == 0x100) first = e;
            if (v < level) {
                int d = (level - v - 1) * 3;
                if (d + 2 >= 96) return E_RANGE;
                st = pow2neg(v);
                gc[e] = R(st * STEP[d]);
                gc[e - 1] = R(st * STEP[d + 1]);
                gc[e - 2] = R(st * STEP[d + 2]);
            } else {
                int d = (v - level - 1) * 3;
                if (d + 2 >= 96) return E_RANGE;
                st = pow2neg(level);
                gc[e] = R(st * STEP[d + 2]);
                gc[e - 1] = R(st * STEP[d + 1]);
                gc[e - 2] = R(st * STEP[d]);
                st = pow2neg(v);
            }
            e -= 3;
            level = v;
        }
        gc[e] = R(st);
        e -= 1;
    }
    return first == 0x100 ? 0xFF : first;
}

/* ---- tones (dsp.tones) --------------------------------------------------- */

static void tone_window_params(Tone *cur, const Tone *prev) {
    if (cur->has_start && cur->start_pos < cur->stop_pos) {
        cur->w_start_on = 1;
        cur->w_start = cur->start_pos * 4 + 0x80;
    } else if (prev->has_start) {
        cur->w_start_on = 1;
        cur->w_start = prev->start_pos * 4;
    } else {
        cur->w_start = 0;
        cur->w_start_on = 0;
    }
    if (prev->has_stop && cur->w_start <= prev->stop_pos * 4) {
        cur->w_stop = prev->stop_pos * 4;
        cur->w_stop_on = 1;
    } else if (cur->has_stop) {
        cur->w_stop_on = 1;
        cur->w_stop = cur->stop_pos * 4 + 0x80;
    } else {
        cur->w_stop = 0x100;
        cur->w_stop_on = 0;
    }
    cur->w_stop += 4;
    if (cur->w_stop > 0x100) cur->w_stop = 0x100;
}

static int tone_env(const Tone *band, const Tables *t, double *w) {
    for (int i = 0; i < 256; i++) w[i] = 1.0;
    if (band->w_start_on) {
        int s = band->w_start;
        if (s < 0 || s > 252) return E_RANGE;
        for (int i = 0; i < s; i++) w[i] = 0.0;
        for (int i = 0; i < 4; i++) w[s + i] = t->RAMP[i];
    }
    if (band->w_stop_on) {
        int e = band->w_stop;
        if (e < 4 || e > 256) return E_RANGE;
        w[e - 4] = t->RAMP[3];
        w[e - 3] = t->RAMP[2];
        w[e - 2] = t->RAMP[1];
        w[e - 1] = t->RAMP[0];
        for (int i = e; i < 256; i++) w[i] = 0.0;
    }
    return 0;
}

static int tone_gen(const Tone *band, int start, int amp_mode, int negate, int ci,
                    const Tables *t, double *out) {
    double buf[128];
    for (int i = 0; i < 128; i++) buf[i] = 0.0;
    int nw = band->nwavs < band->nw ? band->nwavs : band->nw;
    if (nw < 0) nw = 0;
    for (int k = 0; k < nw; k++) {
        double a = t->AMP_SF[band->amp_sf[k]];
        if (amp_mode == 0) a = R(a * t->AMP_IDX[band->amp_idx[k]]);
        int step = band->freq[k];
        int pos = ((start - 0x80) * step + ((band->phase[k] & 0x1F) << 6)) & 0x7FF;
        for (int i = 0; i < 128; i++) buf[i] = R(a * t->SINE[(pos + i * step) & 0x7FF] + buf[i]);
    }
    if (ci == 1 && negate != 0) {
        for (int i = 0; i < 128; i++) buf[i] = R(buf[i] * t->MINUS_ONE);
    }
    double env[256];
    int rc = tone_env(band, t, env);
    if (rc) return rc;
    for (int i = 0; i < 128; i++) out[i] = R(buf[i] * env[start + i]);
    return 0;
}

static int tones(double sbs[16][128], int ci, Tone *prev_bands, Tone *cur_bands,
                 const ToneInfo *prevT, const ToneInfo *curT, const Tables *t) {
    if (!prevT->present && !curT->present) return 0;
    const double *WIN = t->TWIN;
    double A[128], B[128];
    for (int b = 0; b < 16; b++) {
        Tone *pb = &prev_bands[b], *cb = &cur_bands[b];
        tone_window_params(cb, pb);
        if (cb->nwavs == 0 && pb->nwavs == 0) continue;
        int rc = tone_gen(pb, 0x80, prevT->amp_mode, prevT->negate[b], ci, t, A);
        if (rc) return rc;
        rc = tone_gen(cb, 0, curT->amp_mode, curT->negate[b], ci, t, B);
        if (rc) return rc;
        int done = 0;
        if (pb->nwavs > 0) {
            if (cb->nwavs > 0 && pb->w_stop - 0x80 >= cb->w_start) {
                for (int i = 0; i < 128; i++) A[i] = R(A[i] * WIN[128 + i]);
                for (int i = 0; i < 128; i++) B[i] = R(B[i] * WIN[i]);
                done = 1;
            } else if (pb->w_stop_on == 0) {
                for (int i = 0; i < 128; i++) A[i] = R(A[i] * WIN[128 + i]);
            }
        }
        if (!done && cb->nwavs > 0 && cb->w_start_on == 0) {
            for (int i = 0; i < 128; i++) B[i] = R(B[i] * WIN[i]);
        }
        for (int i = 0; i < 128; i++) {
            double ab = R(A[i] + B[i]);
            sbs[b][i] = R(sbs[b][i] + ab);
        }
    }
    return 0;
}

/* ---- 16-band synthesis filter bank (dsp.qmf, dsp._qmf_butterfly_v) ------ */

/* One sample of the butterfly network: x[16] -> X[16]. */
static void butterfly(const double *x, const double *K, double H, double *X) {
    double K0 = K[0], K1 = K[1], K2 = K[2], K3 = K[3], K4 = K[4], K5 = K[5], K6 = K[6];
    double K7 = K[7], K8 = K[8], K9 = K[9], K10 = K[10], K11 = K[11], K12 = K[12];
    double K13 = K[13], K16 = K[16], K17 = K[17], K18 = K[18];
    double x0 = x[0], x1 = x[1], x2 = x[2], x3 = x[3], x4 = x[4], x5 = x[5], x6 = x[6];
    double x7 = x[7], x8 = x[8], x9 = x[9], x10 = x[10], x11 = x[11], x12 = x[12];
    double x13 = x[13], x14 = x[14], x15 = x[15];
    double v1 = R(x7 + x8), v2 = R(x6 + x9), v3 = R(x5 + x10), v4 = R(x4 + x11);
    double v5 = R(x0 + x15), v6 = R(x1 + x14), v7 = R(x2 + x13), v8 = R(x3 + x12);
    double v9 = R(v5 - v1), v10 = R(v6 - v2), v11 = R(v7 - v3), v12 = R(v8 - v4);
    double v13 = R(v5 + v1), v14 = R(v6 + v2), v15 = R(v7 + v3), v16 = R(v8 + v4);
    double d17 = v16 + v15;
    double d18 = d17 + v14;
    double v19 = R(K8 * v9), v20 = R(K9 * v10), v21 = R(K10 * v11), v22 = R(K11 * v12);
    double v23 = R(x7 - x8), v24 = R(x6 - x9), v25 = R(x5 - x10), v26 = R(x4 - x11);
    double d27 = d18 + v13;
    double v28 = R(x0 - x15), v29 = R(x1 - x14), v30 = R(x2 - x13), v31 = R(x3 - x12);
    double d32 = d27 * H;
    double v33 = R(K7 * v23), v34 = R(K6 * v24), v35 = R(K5 * v25), v36 = R(K4 * v26);
    double v37 = R(K0 * v28), v38 = R(K1 * v29), v39 = R(K2 * v30), v40 = R(K3 * v31);
    double f41 = R(d32);
    double d42 = v13 - v14;
    double v43 = R(v37 - v33), v44 = R(v38 - v34), v45 = R(v39 - v35), v46 = R(v40 - v36);
    double v47 = R(v37 + v33), v48 = R(v38 + v34), v49 = R(v39 + v35), v50 = R(v40 + v36);
    double v51 = R(K8 * v43), v52 = R(K9 * v44), v53 = R(K10 * v45), v54 = R(K11 * v46);
    double d55 = d42 - v15;
    double d56 = d55 + v16;
    double d57 = d56 * K16;
    double d58 = v13 - v16;
    double d59 = d58 * K12;
    double d60 = v14 - v15;
    double d61 = d60 * K13;
    double d62 = K17 * d59;
    double d63 = K18 * d61;
    double d64 = d62 - d63;
    double f65 = R(d64);
    double d66 = v19 - v22;
    double d67 = d66 * K12;
    double d68 = v20 - v21;
    double d69 = d68 * K13;
    double d70 = v21 + v20;
    double d71 = d70 + v22;
    double d72 = d71 + v19;
    double d73 = d72 * H;
    double f74 = R(d73);
    double d75 = d69 + d67;
    double d76 = d75 * H;
    double d77 = d76 - f74;
    double f78 = R(d77);
    double d79 = v19 - v20;
    double d80 = d79 - v21;
    double d81 = d80 + v22;
    double d82 = d81 * K16;
    double d83 = d82 - f78;
    double f84 = R(d83);
    double d85 = K17 * d67;
    double d86 = d69 * K18;
    double d87 = d85 - d86;
    double d88 = d87 - f84;
    double f89 = R(d88);
    double d90 = v50 + v49;
    double d91 = d90 + v48;
    double d92 = d91 + v47;
    double d93 = d92 * H;
    double d94 = d93 - f41;
    double f95 = R(d94);
    double d96 = f74 - f95;
    double f97 = R(d96);
    double d98 = v47 - v48;
    double d99 = d98 - v49;
    double d100 = d99 + v50;
    double d101 = d100 * K16;
    double f102 = R(d101);
    double d103 = v47 - v50;
    double d104 = d103 * K12;
    double d105 = v48 - v49;
    double d106 = d105 * K13;
    double d107 = d104 * K17;
    double d108 = d106 * K18;
    double d109 = d107 - d108;
    double f110 = R(d109);
    double d111 = v54 + v53;
    double d112 = d111 + v52;
    double d113 = d112 + v51;
    double d114 = d113 * H;
    double d115 = d114 - d93;
    double d116 = d115 - f97;
    double f117 = R(d116);
    double d118 = d106 + d104;
    double d119 = d118 * H;
    double d120 = d119 - d115;
    double f121 = R(d120);
    double d122 = d61 + d59;
    double d123 = d122 * H;
    double d124 = d123 - f117;
    double f125 = R(d124);
    double d126 = v51 - v54;
    double d127 = d126 * K12;
    double d128 = f121 - f125;
    double f129 = R(d128);
    double d130 = v52 - v53;
    double d131 = d130 * K13;
    double d132 = f78 - f129;
    double f133 = R(d132);
    double d134 = d131 + d127;
    double d135 = d134 - v51;
    double d136 = d135 - v52;
    double d137 = d136 - v53;
    double d138 = d137 - v54;
    double d139 = d138 * H;
    double f140 = R(d139);
    double d141 = v51 - v52;
    double d142 = d141 - v53;
    double d143 = d142 + v54;
    double d144 = d143 * K16;
    double d145 = d144 - f140;
    double f146 = R(d145);
    double d147 = d127 * K17;
    double d148 = d131 * K18;
    double d149 = d147 - d148;
    double d150 = d149 - f146;
    double d151 = f140 - f121;
    double d152 = d151 - f133;
    double f153 = R(d152);
    double d154 = d57 - f153;
    double f155 = R(d154);
    double d156 = f102 - d151;
    double d157 = d156 - f155;
    double f158 = R(d157);
    double d159 = f84 - f158;
    double f160 = R(d159);
    double d161 = f146 - d156;
    double d162 = d161 - f160;
    double f163 = R(d162);
    double d164 = f65 - f163;
    double f165 = R(d164);
    double d166 = f110 - d161;
    double d167 = d166 - f165;
    double f168 = R(d167);
    double d169 = f89 - f168;
    double f170 = R(d169);
    double d171 = d150 - d166;
    double d172 = d171 - f170;
    double f173 = R(d172);
    X[0] = f173; X[1] = f170; X[2] = f168; X[3] = f165;
    X[4] = f163; X[5] = f160; X[6] = f158; X[7] = f155;
    X[8] = f153; X[9] = f133; X[10] = f129; X[11] = f125;
    X[12] = f117; X[13] = f97; X[14] = f95; X[15] = f41;
}

static void qmf(double sbs[16][128], ChannelState *state, const Tables *t, double X[16][128],
                float *pcm) {
    const double *PRE = t->PRE, *Wt = t->QW;
    const double *Kx = Wt, *Ky = Wt + 0x60, *Q = Wt + 0x68, *Qef = Wt + 0xB8;
    double Xm1[16][128];
    double xs[16], Xs[16];
    for (int n = 0; n < 128; n++) {
        for (int k = 0; k < 16; k++) xs[k] = R(sbs[k][n] * PRE[k]);
        butterfly(xs, t->K, t->HALF, Xs);
        for (int k = 0; k < 16; k++) X[k][n] = Xs[k];
    }
    for (int k = 0; k < 16; k++) {
        Xm1[k][0] = state->x_last[k];
        for (int n = 1; n < 128; n++) Xm1[k][n] = X[k][n - 1];
    }
    /* A[m][n] (128) and Bf[m][j] (130: j = 0, 1 the carried B of the last two
     * samples, then this frame's 128) */
    double A[8][128], Bf[8][130], An[8][128], Bn[8][130];
    for (int m = 0; m < 8; m++) {
        for (int n = 0; n < 128; n++) {
            double pa = R(Kx[m] * X[8 + m][n]);
            double pb = R(Ky[m] * Xm1[7 - m][n]);
            A[m][n] = R(pa + pb);
        }
        Bf[m][0] = state->b_hist[0][m][0];
        Bf[m][1] = state->b_hist[0][m][1];
        for (int n = 0; n < 128; n++) {
            double pa = R(Ky[7 - m] * X[15 - m][n]);
            double pb = R(Kx[7 - m] * Xm1[m][n]);
            Bf[m][2 + n] = R(pa - pb);
        }
    }
    float new_hist[11][8][2];
    for (int m = 0; m < 8; m++) {
        new_hist[0][m][0] = (float)Bf[m][128];
        new_hist[0][m][1] = (float)Bf[m][129];
    }
    for (int i = 0; i < 10; i++) {
        int q = 8 * i;
        for (int m = 0; m < 8; m++) {
            for (int n = 0; n < 128; n++) {
                double p = R(Q[q + m] * Bf[7 - m][n]);
                An[m][n] = R(A[m][n] + p);
            }
            Bn[m][0] = state->b_hist[i + 1][m][0];
            Bn[m][1] = state->b_hist[i + 1][m][1];
            for (int n = 0; n < 128; n++) {
                double p = R(Q[q + 7 - m] * A[7 - m][n]);
                Bn[m][2 + n] = R(p - Bf[m][n]);
            }
        }
        memcpy(A, An, sizeof A);
        memcpy(Bf, Bn, sizeof Bf);
        for (int m = 0; m < 8; m++) {
            new_hist[i + 1][m][0] = (float)Bf[m][128];
            new_hist[i + 1][m][1] = (float)Bf[m][129];
        }
    }
    for (int m = 0; m < 8; m++) {
        for (int n = 0; n < 128; n++) {
            double p = R(Qef[m] * Bf[7 - m][n]);
            pcm[16 * n + m] = (float)R(A[m][n] + p);
        }
    }
    for (int m = 0; m < 8; m++) {
        for (int n = 0; n < 128; n++) {
            double p = R(Qef[7 - m] * A[7 - m][n]);
            pcm[16 * n + 8 + m] = (float)R(p - Bf[m][n]);
        }
    }
    for (int k = 0; k < 16; k++) state->x_last[k] = (float)X[k][127];
    memcpy(state->b_hist, new_hist, sizeof new_hist);
}

/* ---- per-channel synthesis (dsp.synthesize) ----------------------------- */

static int synthesize(ChannelState *state, Chan *c, int ci, Unit *u, const double *spec,
                      const ToneInfo *prevT, const ToneInfo *curT, const Tables *t,
                      double X[16][128], float *pcm) {
    int nsb = u->nsb;
    int cur_wnd[16] = {0};
    for (int sb = 0; sb < nsb; sb++) cur_wnd[sb] = c->wnd[sb];
    double sbs[16][128];
    double Y[256], gc[256];
    for (int sb = 0; sb < nsb; sb++) {
        int pw = state->wnd[sb], cw = cur_wnd[sb];
        const double *W;
        if (pw == 0) W = cw == 0 ? t->WIN[0] : t->WIN[1];
        else W = cw == 0 ? t->WIN[2] : t->WIN[3];
        imdct(spec + sb * 128, W, t->SBREV[sb], t, Y);
        float *ovl = state->ovl[sb];
        const Gain *pg = &state->gain[sb], *cg = &c->gain[sb];
        if (pg->npoints == 0 && cg->npoints == 0) {
            for (int i = 0; i < 128; i++) sbs[sb][i] = R((double)ovl[i] + Y[i]);
        } else {
            int first = gain_curve(pg, cg, t, gc);
            if (first < 0) return first;
            if (first >= 128) {
                for (int i = 128; i <= first; i++) Y[i] = R(gc[i] * Y[i]);
            }
            for (int i = 0; i < 128; i++) {
                double p = R(Y[i] * gc[i]);
                sbs[sb][i] = R((double)ovl[i] + p);
            }
        }
        for (int i = 0; i < 128; i++) ovl[i] = (float)Y[128 + i];
    }
    for (int sb = nsb; sb < 16; sb++) {
        for (int i = 0; i < 128; i++) {
            sbs[sb][i] = 0.0;
            state->ovl[sb][i] = 0.0f;
        }
    }
    int rc = tones(sbs, ci, state->tones, c->tones, prevT, curT, t);
    if (rc) return rc;
    if (u->noise_present) {
        double scale = (double)(1 << u->noise_level) * t->Q15;
        for (int sb = 0; sb < 16; sb++) {
            int ofs = t->NOISE_OFS[u->noise_counter];
            u->noise_counter++;
            for (int i = 0; i < 128; i++) sbs[sb][i] = R((double)t->NOISE[ofs + i] * scale + sbs[sb][i]);
        }
    }
    qmf(sbs, state, t, X, pcm);
    memcpy(state->gain, c->gain, sizeof state->gain);
    memcpy(state->wnd, cur_wnd, sizeof state->wnd);
    memcpy(state->tones, c->tones, sizeof state->tones);
    return 0;
}

/* ---- int16 output (decoder.to_pcm) ---------------------------------------- */

static int16_t to_int16(double v) {
    if (v != v) return -32768;                  /* NaN: fistp's integer indefinite */
    if (v >= 32767.5) return 32767;
    if (v <= -32768.5) return -32768;
    double r = nearbyint(v);                    /* round half to even */
    if (r > 32767.0) return 32767;
    if (r < -32768.0) return -32768;
    return (int16_t)r;
}

/* ---- one frame ------------------------------------------------------------ */

/* Decode one parsed frame (``rec``: lst_record_size() int32s, _core.pack_unit)
 * and advance the state. Writes the two float32 channels to ``fout`` (4096:
 * left then right) and/or the interleaved int16 PCM to ``pcm`` (4096), either
 * may be NULL. Returns 0, or a negative error code (then the state may be
 * partly advanced; the caller stops decoding). Everything lives in the state
 * at ``mem``, so separate states can decode on separate threads at once. */
EXPORT int lst_frame(void *mem, const int32_t *rec, int reclen, float *fout, int16_t *pcm) {
    Decoder *d = (Decoder *)mem;
    const Tables *t = &d->t;
    Unit *u = &d->u;
    if (reclen != RECORD) return E_RECORD;
    int rc = unpack(u, rec);
    if (rc) return rc;
    ChannelState *st0 = &d->st[0], *st1 = &d->st[1];
    for (int b = 0; b < 16; b++) {
        if (u->share[b]) {
            st1->tones[b].has_start = st0->tones[b].has_start;
            st1->tones[b].has_stop = st0->tones[b].has_stop;
            st1->tones[b].start_pos = st0->tones[b].start_pos;
            st1->tones[b].stop_pos = st0->tones[b].stop_pos;
        }
    }
    dequantize(u, d->st, t, d->spectra);
    ToneInfo curT;
    curT.present = u->tones_present;
    curT.amp_mode = u->amp_mode;
    for (int b = 0; b < 16; b++) curT.negate[b] = u->tone_negate[b];
    u->noise_counter = u->noise_table;
    float out[2][2048];
    for (int ci = 0; ci < 2; ci++) {
        rc = synthesize(&d->st[ci], &u->ch[ci], ci, u, d->spectra[ci], &d->prevT, &curT, t, d->X,
                        out[ci]);
        if (rc) return rc;
    }
    d->prevT = curT;
    if (fout) {
        memcpy(fout, out[0], sizeof out[0]);
        memcpy(fout + 2048, out[1], sizeof out[1]);
    }
    if (pcm) {
        for (int i = 0; i < 2048; i++) {
            pcm[2 * i] = to_int16(out[0][i]);
            pcm[2 * i + 1] = to_int16(out[1][i]);
        }
    }
    return 0;
}
