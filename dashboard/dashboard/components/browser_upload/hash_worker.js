/*
 * hash_worker.js — Hashing off the page's main thread, so the upload box stays
 * responsive while a multi-GB file is fingerprinted.
 *
 *   {id, op: "sha256", file}   -> {id, progress: bytesDone} ... {id, sha256}
 *   {id, op: "md5", buffer}    -> {id, md5}   (base64, for Content-MD5)
 *   any failure                -> {id, error}
 */
importScripts("hashes.js");

const BLOCK = 8 * 1024 * 1024;

self.onmessage = async (event) => {
  const { id, op } = event.data;
  try {
    if (op === "sha256") {
      const file = event.data.file;
      const h = new NBHashes.Sha256();
      let lastReport = 0;
      for (let off = 0; off < file.size; off += BLOCK) {
        const buf = new Uint8Array(await file.slice(off, Math.min(file.size, off + BLOCK)).arrayBuffer());
        h.update(buf);
        const done = Math.min(file.size, off + BLOCK);
        const now = Date.now();
        if (now - lastReport > 150) { self.postMessage({ id, progress: done }); lastReport = now; }
      }
      self.postMessage({ id, sha256: h.hex() });
    } else if (op === "md5") {
      self.postMessage({ id, md5: NBHashes.md5Base64(new Uint8Array(event.data.buffer)) });
    } else {
      throw new Error("unknown op " + op);
    }
  } catch (err) {
    self.postMessage({ id, error: String((err && err.message) || err) });
  }
};
