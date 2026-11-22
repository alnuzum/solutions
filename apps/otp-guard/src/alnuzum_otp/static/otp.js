// Layer 3 of the stand: the proof of work and the device's fingerprint, before the code is asked for.
// Find n such that SHA-256("<challenge>.<n>") starts with `bits` zero bits; the server checks it once.
(() => {
  "use strict";
  const form = document.getElementById("otp-send");
  if (!form) return;

  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const W = new Uint32Array(64);
  const H = new Uint32Array(8);

  // SHA-256 of an ASCII string, as eight 32-bit words
  function sha256(text) {
    const len = text.length;
    const blocks = ((len + 8) >> 6) + 1;
    const words = new Uint32Array(blocks * 16);
    for (let i = 0; i < len; i++) words[i >> 2] |= text.charCodeAt(i) << (24 - (i & 3) * 8);
    words[len >> 2] |= 0x80 << (24 - (len & 3) * 8);
    words[blocks * 16 - 1] = len * 8;
    H.set([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    for (let b = 0; b < blocks; b++) {
      for (let t = 0; t < 16; t++) W[t] = words[b * 16 + t];
      for (let t = 16; t < 64; t++) {
        const x = W[t - 15], y = W[t - 2];
        const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        W[t] = (W[t - 16] + s0 + W[t - 7] + s1) | 0;
      }
      let a = H[0], bb = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (let t = 0; t < 64; t++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const t1 = (h + S1 + ((e & f) ^ (~e & g)) + K[t] + W[t]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const t2 = (S0 + ((a & bb) ^ (a & c) ^ (bb & c))) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = bb; bb = a; a = (t1 + t2) | 0;
      }
      H[0] += a; H[1] += bb; H[2] += c; H[3] += d; H[4] += e; H[5] += f; H[6] += g; H[7] += h;
    }
    return H;
  }

  function zeros(h) {
    let n = 0;
    for (let i = 0; i < 8; i++) {
      const w = h[i];
      if (w === 0) { n += 32; continue; }
      return n + Math.clz32(w);
    }
    return n;
  }

  function hex(h) {
    return Array.from(h, (w) => (w >>> 0).toString(16).padStart(8, "0")).join("");
  }

  // the device as a site can see it; an antidetect browser changes all of it, which the attack shows
  const o = Intl.DateTimeFormat().resolvedOptions();
  const seen = [
    navigator.userAgent, navigator.language, (navigator.languages || []).join(","), o.timeZone,
    `${screen.width}x${screen.height}x${screen.colorDepth}`, navigator.hardwareConcurrency || 0,
    navigator.platform || "",
  ].join("|").replace(/[^\x20-\x7e]/g, "?");
  form.elements.dev.value = hex(sha256(seen)).slice(0, 32);

  const button = form.querySelector("button[type=submit]");
  const work = form.querySelector(".otp-work");
  const challenge = form.elements.pow_c.value;
  const bits = Number(form.dataset.bits);
  const started = performance.now();
  let n = 0;

  function step() {
    const until = performance.now() + 40;
    while (performance.now() < until) {
      for (let i = 0; i < 4096; i++, n++) {
        if (zeros(sha256(`${challenge}.${n}`)) >= bits) {
          form.elements.pow_n.value = String(n);
          const ms = Math.round(performance.now() - started);
          form.dataset.solvedMs = String(ms);
          work.textContent = work.dataset.done.replace("{ms}", String(ms));
          button.disabled = false;
          return;
        }
      }
    }
    setTimeout(step, 0);
  }
  setTimeout(step, 0);
})();
