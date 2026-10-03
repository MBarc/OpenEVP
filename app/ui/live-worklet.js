"use strict";
// The Live view's capture (app/ui/live.js), on the audio thread: the input's samples as 16-bit
// PCM (interleaved when stereo), in batches of about 2048 frames, with the batch's peak and
// sum of squares for the level meter. Chromium delivers a 16-bit input as floats of x / 32768,
// so x * 32768, rounded, gives back exactly the samples Windows delivered.
class OpenEVPCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.channels = o.channels === 2 ? 2 : 1;
    this.batch = o.batch || 2048;
    this.buf = new Int16Array(this.batch * this.channels);
    this.n = 0; this.peak = 0; this.sumsq = 0;
    // Stop: the page asks for the samples that do not fill a batch yet; they are sent, then
    // the answer, on the same port (so the page has them all when the answer comes).
    this.port.onmessage = (e) => {
      const id = e.data && e.data.flush;
      if (id === undefined) return;
      if (this.n) this.flush();
      this.port.postMessage({ flushed: id });
    };
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;          // no input connected (yet)
    const ch = this.channels, len = input[0].length;
    for (let i = 0; i < len; i++) {
      for (let c = 0; c < ch; c++) {
        const v = (input[c] || input[0])[i];
        let s = Math.round(v * 32768);
        if (s > 32767) s = 32767; else if (s < -32768) s = -32768;
        this.buf[this.n * ch + c] = s;
        const a = v < 0 ? -v : v;
        if (a > this.peak) this.peak = a;
        this.sumsq += v * v;
      }
      if (++this.n === this.batch) this.flush();
    }
    return true;
  }
  flush() {
    const pcm = this.buf.slice(0, this.n * this.channels);
    this.port.postMessage({ pcm: pcm.buffer, frames: this.n, peak: this.peak, sumsq: this.sumsq }, [pcm.buffer]);
    this.n = 0; this.peak = 0; this.sumsq = 0;
  }
}
registerProcessor("openevp-capture", OpenEVPCapture);
