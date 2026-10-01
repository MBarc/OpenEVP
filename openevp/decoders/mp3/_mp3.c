/* C core of the MP3 decoder (openevp.decoders.mp3): minimp3 (vendor/minimp3,
 * CC0) behind a small streaming interface.
 *
 * mp3c_decode() decodes the MPEG audio frames (layers I, II and III) of a whole
 * file held by the caller, from *pos on, into an int16 buffer, a bounded number
 * of samples per call, and moves *pos past what it used. The output format
 * (sample rate, channels) is fixed by the first frame that decodes:
 *   - a later frame at another sample rate is dropped (counted in DROPPED);
 *   - a later frame with the other channel count is converted (mono to both
 *     channels; stereo to mono as (left + right) >> 1, an integer average).
 * A first frame that is a Xing/Info/VBRI tag frame (an encoder's header, no
 * audio) gives the format but no samples. Junk between frames (a damaged
 * stretch, a tag) is skipped by minimp3's own resync; every byte is consumed
 * exactly once, so a call never loops on the same data. With pcm NULL nothing
 * is decoded: the frames are only counted (their length, from the headers).
 *
 * Bounded work per call: minimp3 is shown at most WINDOW bytes from *pos (far
 * more than the ten frames it looks ahead to sync), and a call stops once it
 * has gone through BUDGET bytes, even if it wrote nothing (a long stretch of
 * junk or free-format data), so the caller polls its stop flag between calls.
 * A call that returns 0 is the end only when *pos == len. For a file of valid
 * frames the window changes nothing; inside junk it decides where minimp3
 * resyncs, the same way every time.
 *
 * Determinism: minimp3 decodes in single-precision float. On x86-64 it always
 * takes its SSE2 path (MINIMP3_ONLY_SIMD; SSE2 is part of x86-64, so there is
 * no run-time dispatch): every x86-64 CPU gives the same samples. The build
 * (tools/build_lpec_core.py) adds -ffp-contract=off -fno-fast-math, so no fused
 * multiply-add or reassociation depends on the compiler's mood. An ARM64 build
 * would take minimp3's NEON path and is not promised to match x86-64.
 */

#include <stdint.h>
#include <string.h>

#define MINIMP3_IMPLEMENTATION
#include "minimp3.h"

#define ABI_VERSION 2
#define WINDOW (256 * 1024)             /* bytes one mp3dec_decode_frame call is shown at most */
#define BUDGET (1024 * 1024)            /* bytes one mp3c_decode call goes through at most */
#define EXPORT __declspec(dllexport)

enum { F_CHANNELS, F_RATE, F_LAYER, F_FRAMES, F_DROPPED, F_COUNT };

typedef struct {
    mp3dec_t dec;
    int channels, rate, layer;          /* the output format; 0 until the first frame */
    int64_t frames, dropped;            /* frames decoded, frames dropped (another rate) */
    mp3d_sample_t frame[MINIMP3_MAX_SAMPLES_PER_FRAME];
} State;

EXPORT int mp3c_abi_version(void) { return ABI_VERSION; }
EXPORT int mp3c_state_size(void) { return (int)sizeof(State); }
EXPORT int mp3c_max_frame_samples(void) { return MINIMP3_MAX_SAMPLES_PER_FRAME / 2; }

EXPORT void mp3c_init(void *mem) {
    State *s = (State *)mem;
    memset(s, 0, sizeof(*s));
    mp3dec_init(&s->dec);
}

/* Is the frame at h (frame_bytes long) a Xing, Info or VBRI tag frame? */
static int tag_frame(const uint8_t *h, int frame_bytes) {
    int mpeg1 = (h[1] & 0x08) != 0, mono = (h[3] & 0xC0) == 0xC0, crc = !(h[1] & 1);
    int side = mpeg1 ? (mono ? 17 : 32) : (mono ? 9 : 17);
    int at = 4 + (crc ? 2 : 0) + side;
    if (((h[1] >> 1) & 3) != 1)                     /* layer III only */
        return 0;
    if (at + 4 <= frame_bytes && (!memcmp(h + at, "Xing", 4) || !memcmp(h + at, "Info", 4)))
        return 1;
    return 36 + 4 <= frame_bytes && !memcmp(h + 36, "VBRI", 4);
}

/* Decode from buf[*pos] (buf holds len bytes: the whole file) into pcm, at most
 * cap sample frames (cap must be at least mp3c_max_frame_samples()) and at most
 * about BUDGET bytes of input. Returns the sample frames written (0 with
 * *pos == len: the end), or -1 for bad arguments. fmt[F_COUNT] gets the
 * output format and the counts so far. */
EXPORT int mp3c_decode(void *mem, const uint8_t *buf, int64_t len, int64_t *pos,
                       int16_t *pcm, int cap, int64_t *fmt) {
    State *s = (State *)mem;
    int written = 0;
    int64_t start;
    if (!s || !buf || !pos || len < 0 || *pos < 0 || *pos > len || !fmt ||
        cap < MINIMP3_MAX_SAMPLES_PER_FRAME / 2)
        return -1;
    start = *pos;
    while (*pos < len && written + MINIMP3_MAX_SAMPLES_PER_FRAME / 2 <= cap && *pos - start < BUDGET) {
        mp3dec_frame_info_t info;
        int64_t left = len - *pos;
        int avail = left > WINDOW ? WINDOW : (int)left;
        const uint8_t *at = buf + *pos;
        int n = mp3dec_decode_frame(&s->dec, at, avail, pcm ? s->frame : NULL, &info);
        if (info.frame_bytes <= 0) {                /* fewer bytes than a frame header: the end */
            *pos = len;
            break;
        }
        *pos += info.frame_bytes;
        if (n <= 0)
            continue;                               /* junk skipped, or a frame without its reservoir */
        if (!s->rate) {
            s->rate = info.hz;
            s->channels = info.channels;
            s->layer = info.layer;
            if (tag_frame(at + info.frame_offset, info.frame_bytes - info.frame_offset))
                continue;                           /* the encoder's tag frame: no audio */
        }
        if (info.hz != s->rate) {
            s->dropped++;
            continue;
        }
        s->frames++;
        if (pcm) {
            int16_t *out = pcm + (int64_t)written * s->channels;
            const int16_t *in = s->frame;
            int i;
            if (info.channels == s->channels) {
                memcpy(out, in, sizeof(int16_t) * (size_t)n * (size_t)s->channels);
            } else if (s->channels == 2) {          /* a mono frame in a stereo stream */
                for (i = 0; i < n; i++)
                    out[2 * i] = out[2 * i + 1] = in[i];
            } else {                                /* a stereo frame in a mono stream */
                for (i = 0; i < n; i++)
                    out[i] = (int16_t)(((int32_t)in[2 * i] + (int32_t)in[2 * i + 1]) >> 1);
            }
        }
        written += n;
    }
    fmt[F_CHANNELS] = s->channels;
    fmt[F_RATE] = s->rate;
    fmt[F_LAYER] = s->layer;
    fmt[F_FRAMES] = s->frames;
    fmt[F_DROPPED] = s->dropped;
    return written;
}
