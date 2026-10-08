/*
 * hashes.js — SHA-256 (incremental, for whole files of any size) and MD5 (one
 * chunk at a time, for the Content-MD5 header). Plain JavaScript, no
 * dependencies; used by hash_worker.js (and by the tests under node).
 * The browser's crypto.subtle has no MD5 and can't hash a file in pieces.
 */
(function (root) {
  "use strict";

  // ── SHA-256 ───────────────────────────────────────────────────────────────
  const K256 = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  class Sha256 {
    constructor() {
      this.h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
      this.buf = new Uint8Array(64);
      this.bufLen = 0;
      this.bytes = 0;
      this.w = new Uint32Array(64);
    }

    _block(d, off) {
      const w = this.w, h = this.h;
      for (let i = 0; i < 16; i++) {
        const j = off + i * 4;
        w[i] = (d[j] << 24) | (d[j + 1] << 16) | (d[j + 2] << 8) | d[j + 3];
      }
      for (let i = 16; i < 64; i++) {
        const x = w[i - 15], y = w[i - 2];
        const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      let a = h[0], b = h[1], c = h[2], dd = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
      for (let i = 0; i < 64; i++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const ch = (e & f) ^ (~e & g);
        const t1 = (hh + S1 + ch + K256[i] + w[i]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) | 0;
        hh = g; g = f; f = e; e = (dd + t1) | 0; dd = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h[0] += a; h[1] += b; h[2] += c; h[3] += dd; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }

    /** data: Uint8Array */
    update(data) {
      let i = 0;
      this.bytes += data.length;
      if (this.bufLen) {
        const n = Math.min(64 - this.bufLen, data.length);
        this.buf.set(data.subarray(0, n), this.bufLen);
        this.bufLen += n;
        i = n;
        if (this.bufLen === 64) { this._block(this.buf, 0); this.bufLen = 0; }
      }
      for (; i + 64 <= data.length; i += 64) this._block(data, i);
      if (i < data.length) { this.buf.set(data.subarray(i), 0); this.bufLen = data.length - i; }
      return this;
    }

    /** Finish and return the lowercase hex digest. */
    hex() {
      const bytes = this.bytes;
      const padLen = (this.bufLen < 56 ? 64 : 128) - this.bufLen;
      const pad = new Uint8Array(padLen);
      pad[0] = 0x80;
      const hi = Math.floor(bytes / 0x20000000), lo = (bytes * 8) >>> 0;
      pad[padLen - 8] = hi >>> 24; pad[padLen - 7] = hi >>> 16; pad[padLen - 6] = hi >>> 8; pad[padLen - 5] = hi;
      pad[padLen - 4] = lo >>> 24; pad[padLen - 3] = lo >>> 16; pad[padLen - 2] = lo >>> 8; pad[padLen - 1] = lo;
      this.update(pad);
      let out = "";
      for (let i = 0; i < 8; i++) out += (this.h[i] >>> 0).toString(16).padStart(8, "0");
      return out;
    }
  }

  // ── MD5 ───────────────────────────────────────────────────────────────────
  const S = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
             5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
             4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
             6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21];
  const KM = new Int32Array(64);
  for (let i = 0; i < 64; i++) KM[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) | 0;

  function _md5Block(M, off, st) {
    let a = st[0], b = st[1], c = st[2], d = st[3];
    for (let i = 0; i < 64; i++) {
      let f, g;
      if (i < 16) { f = (b & c) | (~b & d); g = i; }
      else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) & 15; }
      else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) & 15; }
      else { f = c ^ (b | ~d); g = (7 * i) & 15; }
      const tmp = d; d = c; c = b;
      const x = (a + f + KM[i] + M[off + g]) | 0;
      b = (b + ((x << S[i]) | (x >>> (32 - S[i])))) | 0;
      a = tmp;
    }
    st[0] = (st[0] + a) | 0; st[1] = (st[1] + b) | 0; st[2] = (st[2] + c) | 0; st[3] = (st[3] + d) | 0;
  }

  function _le32(bytes, i) {
    return bytes[i] | (bytes[i + 1] << 8) | (bytes[i + 2] << 16) | (bytes[i + 3] << 24);
  }

  /** MD5 of a Uint8Array -> 16 raw bytes (Uint8Array). */
  function md5(data) {
    const st = new Int32Array([0x67452301, 0xefcdab89 | 0, 0x98badcfe | 0, 0x10325476]);
    const n = data.length;
    const M = new Int32Array(16);
    let i = 0;
    for (; i + 64 <= n; i += 64) {
      for (let k = 0; k < 16; k++) M[k] = _le32(data, i + 4 * k);
      _md5Block(M, 0, st);
    }
    // tail + padding (+ length in bits, little-endian 64-bit)
    const restLen = n - i;
    const tail = new Uint8Array(restLen < 56 ? 64 : 128);
    tail.set(data.subarray(i));
    tail[restLen] = 0x80;
    const lo = (n * 8) >>> 0, hi = Math.floor(n / 0x20000000);
    const L = tail.length;
    tail[L - 8] = lo; tail[L - 7] = lo >>> 8; tail[L - 6] = lo >>> 16; tail[L - 5] = lo >>> 24;
    tail[L - 4] = hi; tail[L - 3] = hi >>> 8; tail[L - 2] = hi >>> 16; tail[L - 1] = hi >>> 24;
    for (let j = 0; j < L; j += 64) {
      for (let k = 0; k < 16; k++) M[k] = _le32(tail, j + 4 * k);
      _md5Block(M, 0, st);
    }
    const out = new Uint8Array(16);
    for (let k = 0; k < 4; k++) {
      out[4 * k] = st[k]; out[4 * k + 1] = st[k] >>> 8; out[4 * k + 2] = st[k] >>> 16; out[4 * k + 3] = st[k] >>> 24;
    }
    return out;
  }

  function toBase64(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  /** base64 MD5 — the value of a Content-MD5 header. */
  function md5Base64(data) { return toBase64(md5(data)); }

  const api = { Sha256, md5, md5Base64 };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.NBHashes = api;
})(typeof self !== "undefined" ? self : this);
