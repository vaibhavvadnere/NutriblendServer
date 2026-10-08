// frame.js — takes one picture from a video file in the browser, for the cover (thumbnail).
//
//   const f = await grabFrame(file, seconds);   // {data: "data:image/jpeg;base64,…", width, height, at} | null
//
// The video is decoded by the browser from the local file (nothing is uploaded). Returns null
// when the browser can't decode this video (e.g. HEVC in some browsers) — the cover is then
// simply not made; it never blocks an upload.

(function (root) {
  "use strict";

  function once(el, event, ms, what) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { cleanup(); reject(new Error(what + " timed out")); }, ms);
      const ok = () => { cleanup(); resolve(); };
      const bad = () => { cleanup(); reject(new Error("the browser can't decode this video")); };
      function cleanup() { clearTimeout(timer); el.removeEventListener(event, ok); el.removeEventListener("error", bad); }
      el.addEventListener(event, ok, { once: true });
      el.addEventListener("error", bad, { once: true });
    });
  }

  function toDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => reject(r.error);
      r.readAsDataURL(blob);
    });
  }

  /** Where to look: 10% in (past fade-ins), at least 1 s, never the very end. */
  function pickTime(duration) {
    if (!duration || !isFinite(duration) || duration <= 0) return 0;
    return Math.min(Math.max(duration * 0.1, Math.min(1, duration / 2)), Math.max(0, duration - 0.2));
  }

  async function grabFrame(file, seconds, opts) {
    const maxWidth = (opts && opts.maxWidth) || 960;
    const quality = (opts && opts.quality) || 0.82;
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.muted = true; video.preload = "auto"; video.playsInline = true;
    try {
      video.src = url;
      await once(video, "loadeddata", 20000, "loading the video");
      const at = seconds != null ? seconds : pickTime(video.duration);
      if (at > 0) { video.currentTime = at; await once(video, "seeked", 20000, "finding the picture"); }
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return null;
      const scale = Math.min(1, maxWidth / w);
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", quality));
      if (!blob || blob.size < 500) return null;
      return { data: await toDataUrl(blob), width: canvas.width, height: canvas.height, at, bytes: blob.size };
    } catch (e) {
      return null;
    } finally {
      URL.revokeObjectURL(url);
      video.removeAttribute("src");
      try { video.load(); } catch (e) { /* nothing to release */ }
    }
  }

  const api = { grabFrame, pickTime };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== "undefined" ? window : globalThis);
