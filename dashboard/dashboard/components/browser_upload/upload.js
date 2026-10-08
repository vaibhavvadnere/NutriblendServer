/*
 * upload.js — engine + the two modes of the browser upload component.
 * See index.html for the overview. No dependencies besides hashes.js.
 */
"use strict";

// ── Streamlit component protocol (v1) ───────────────────────────────────────
const ST = {
  send(type, extra) { window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type }, extra), "*"); },
  ready() { this.send("streamlit:componentReady", { apiVersion: 1 }); },
  height() {
    const h = document.getElementById("root").scrollHeight;
    this.send("streamlit:setFrameHeight", { height: h ? h + 4 : 0 });
  },
  value(v) { this.send("streamlit:setComponentValue", { value: v, dataType: "json" }); },
};
let seq = 0;
function emit(event, data) { ST.value(Object.assign({ seq: Date.now() + ":" + (++seq), event }, data || {})); }

// ── helpers ─────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
function fmtBytes(n) {
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n >= 100 ? 0 : 1) : n) + " " + u[i];
}
function fmtTime(s) {
  if (!isFinite(s) || s < 0) return "";
  if (s < 60) return "less than a minute";
  const m = Math.round(s / 60);
  if (m < 60) return "about " + m + " min";
  return "about " + Math.floor(m / 60) + " h " + (m % 60) + " min";
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const CHANNEL = new BroadcastChannel("nutriblend-uploads");

// ── hashing (worker, or main thread if workers are unavailable) ─────────────
let worker = null, nextId = 0;
const pending = new Map();
try {
  worker = new Worker("hash_worker.js");
  worker.onmessage = (e) => {
    const p = pending.get(e.data.id); if (!p) return;
    if (e.data.progress !== undefined) { p.onProgress && p.onProgress(e.data.progress); return; }
    pending.delete(e.data.id);
    e.data.error ? p.reject(new Error(e.data.error)) : p.resolve(e.data);
  };
  worker.onerror = () => { worker = null; };
} catch (e) { worker = null; }

function viaWorker(msg, transfer, onProgress) {
  return new Promise((resolve, reject) => {
    const id = ++nextId; pending.set(id, { resolve, reject, onProgress });
    worker.postMessage(Object.assign({ id }, msg), transfer || []);
  });
}
async function sha256File(file, onProgress, isCancelled) {
  if (worker) return (await viaWorker({ op: "sha256", file }, [], onProgress)).sha256;
  const h = new NBHashes.Sha256(); const B = 8 << 20;
  for (let off = 0; off < file.size; off += B) {
    if (isCancelled()) throw new Error("cancelled");
    h.update(new Uint8Array(await file.slice(off, Math.min(file.size, off + B)).arrayBuffer()));
    onProgress(Math.min(file.size, off + B)); await sleep(0);
  }
  return h.hex();
}
async function md5Of(buffer) {
  if (worker) return (await viaWorker({ op: "md5", buffer }, [buffer])).md5;
  return NBHashes.md5Base64(new Uint8Array(buffer));
}

// ── HTTP ────────────────────────────────────────────────────────────────────
class ApiError extends Error {
  constructor(status, body) {
    super((body && body.message) || ("HTTP " + status));
    this.status = status; this.code = body && body.error && body.error.code; this.details = (body && body.error && body.error.details) || {};
  }
}
const STOPPED = () => new ApiError(-1, { message: "Stopped" });
function xhr(method, url, { headers = {}, body = null, onProgress = null, signal = null, json = true } = {}) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open(method, url);
    for (const [k, v] of Object.entries(headers)) x.setRequestHeader(k, v);
    if (onProgress) x.upload.onprogress = (e) => onProgress(e.loaded);
    x.onload = () => {
      let data = null;
      if (json) { try { data = JSON.parse(x.responseText || "null"); } catch (e) { data = null; } }
      if (x.status >= 200 && x.status < 300) resolve(data);
      else reject(new ApiError(x.status, data || { message: (x.responseText || "").slice(0, 200) }));
    };
    x.onerror = () => reject(new ApiError(0, { message: "Network error" }));
    x.ontimeout = () => reject(new ApiError(0, { message: "Timed out" }));
    x.timeout = 300000;
    if (signal) {
      if (signal.aborted) { reject(STOPPED()); return; }
      signal.addEventListener("abort", () => { x.abort(); reject(STOPPED()); });
    }
    x.send(body);
  });
}
function api(job, method, path, body, signal) {
  const headers = { "X-Upload-Ticket": job.ticket };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return xhr(method, job.api + "/admin/videos/" + job.video_id + path,
             { headers, body: body === undefined ? null : JSON.stringify(body), signal }).then((r) => r && r.data);
}
function retryable(e) {
  return e.status === 0 || e.status >= 500 || e.status === 408 || e.status === 429
    || e.code === "UPLOAD_CHECKSUM_MISMATCH" || e.code === "UPLOAD_INCOMPLETE"
    || (e.status === 403 && !e.code) || (e.status === 400 && !e.code);     // R2: expired link / BadDigest
}
function friendly(e) {
  if (e.status === 401) return "The upload link expired. Start the upload again.";
  if (e.code === "UPLOAD_CHECKSUM_MISMATCH") return "The file changed while it was being uploaded. Make sure it's final and upload it again.";
  if (e.status === 413) return "Too large to send through the server. Check the bucket's CORS policy so it can go straight to storage.";
  return e.message || String(e);
}

// Progress -> "Uploading… 40 MB of 120 MB (33%) · 6.7 MB/s · about 2 min left"
function speedometer() {
  const samples = [];
  return (sent, total) => {
    const now = Date.now();
    samples.push([now, sent]); while (samples.length > 2 && now - samples[0][0] > 20000) samples.shift();
    const [t0, b0] = samples[0];
    const speed = now - t0 > 1500 ? (sent - b0) / ((now - t0) / 1000) : 0;
    let text = "Uploading… " + fmtBytes(sent) + " of " + fmtBytes(total) + " (" + Math.floor(100 * sent / total) + "%)";
    if (speed > 0) text += " · " + fmtBytes(speed) + "/s · " + fmtTime((total - sent) / speed) + " left";
    return text;
  };
}

// ── engine: a video, in pieces, N at a time ─────────────────────────────────
// report(sentBytes, totalBytes, phase)   phase: "upload" | "checking"
async function uploadVideo(job, file, signal, report) {
  const size = file.size, cs = job.chunk_size, total = job.total_chunks;
  const lenOf = (i) => (i < total - 1 ? cs : size - cs * (total - 1));
  const remaining = (missing) => size - missing.reduce((a, i) => a + lenOf(i), 0);
  const inflight = new Map();
  let doneBytes = 0, directFailures = 0, viaServer = false;
  const ctl = new AbortController();
  signal.addEventListener("abort", () => ctl.abort());
  const tick = setInterval(() => report(Math.min(size, doneBytes + [...inflight.values()].reduce((a, b) => a + b, 0)), size, "upload"), 500);

  async function sendPiece(i) {
    const blob = file.slice(i * cs, i * cs + lenOf(i));
    const md5 = await md5Of(await blob.arrayBuffer());
    let lastErr = null;
    for (let attempt = 0; attempt < 6; attempt++) {
      if (ctl.signal.aborted) throw STOPPED();
      try {
        const link = (await api(job, "POST", "/upload/part-urls", { parts: [{ index: i, md5 }] }, ctl.signal)).parts[0];
        const onProgress = (n) => { inflight.set(i, n); };
        if (link.url && !viaServer) {
          try {
            await xhr("PUT", link.url, { headers: link.headers, body: blob, onProgress, signal: ctl.signal, json: false });
          } catch (err) { err.direct = true; throw err; }
        } else {
          await xhr("PUT", job.api + "/admin/videos/" + job.video_id + "/upload/chunks/" + i, {
            headers: { "X-Upload-Ticket": job.ticket, "Content-Type": "application/octet-stream", "Content-MD5": md5 },
            body: blob, onProgress, signal: ctl.signal });
        }
        inflight.delete(i); doneBytes += lenOf(i);
        return;
      } catch (e) {
        inflight.delete(i);
        if (e.status === -1) throw e;
        if (e.status === 0 && e.direct && ++directFailures >= 3 && !viaServer) {
          viaServer = true;
          console.warn("Direct upload to storage is blocked (check the bucket's CORS policy); sending through the server.");
        }
        if (!retryable(e)) throw e;
        lastErr = e;
        await sleep(Math.min(16000, 1000 * 2 ** attempt));
      }
    }
    throw new Error("Piece " + i + " could not be sent: " + (lastErr ? friendly(lastErr) : "unknown error"));
  }

  async function sendAll(missing) {
    const queue = missing.slice();
    let failure = null;
    const workers = Array.from({ length: Math.max(1, job.parallel || 4) }, async () => {
      while (queue.length && !failure && !ctl.signal.aborted) {
        try { await sendPiece(queue.shift()); }
        catch (e) { if (!failure) { failure = e; if (e.status !== -1) ctl.abort(); } }
      }
    });
    await Promise.all(workers);
    if (signal.aborted) throw STOPPED();
    if (failure) throw failure;
  }

  try {
    let progress;
    for (let attempt = 0; ; attempt++) {
      try { progress = await api(job, "GET", "/upload", undefined, ctl.signal); break; }
      catch (e) {
        // Nothing left to upload: finished meanwhile (e.g. Stop was pressed during the final
        // check, which the server still completed). Python checks the video's real status.
        if (e.code === "UPLOAD_NOT_IN_PROGRESS" && attempt < 2) { await sleep(2000); continue; }
        if (e.code === "UPLOAD_NOT_IN_PROGRESS") return null;
        throw e;
      }
    }
    doneBytes = remaining(progress.missing_chunks);
    for (let round = 0; round < 4; round++) {
      for (let guard = 0; guard < 20 && !progress.complete && progress.missing_chunks.length; guard++) {
        await sendAll(progress.missing_chunks);
        progress = await api(job, "GET", "/upload", undefined, ctl.signal);
      }
      if (!progress.complete) throw new Error("Some pieces didn't arrive in storage. Try again.");
      report(size, size, "checking");
      try {
        return await api(job, "POST", "/upload/complete", undefined, ctl.signal);
      } catch (e) {
        if (e.code !== "UPLOAD_INCOMPLETE" || round === 3) throw e;
        progress = await api(job, "GET", "/upload", undefined, ctl.signal);   // re-send what the check rejected
        doneBytes = remaining(progress.missing_chunks);
      }
    }
    throw new Error("The upload could not be completed.");
  } finally {
    clearInterval(tick);
  }
}

// ── engine: a document, one PUT straight to storage (or through the server) ─
async function uploadDocument(job, file, sha, signal, report) {
  const size = file.size;
  let sent = 0, directFailures = 0;
  const tick = setInterval(() => report(sent, size, "upload"), 500);
  const onProgress = (n) => { sent = n; };
  try {
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        sent = 0;
        const link = await api(job, "POST", "/document/upload", { file_name: file.name, size, sha256: sha }, signal);
        if (link.url && directFailures < 2) {
          try {
            await xhr("PUT", link.url, { headers: link.headers, body: file, onProgress, signal, json: false });
          } catch (err) { err.direct = true; throw err; }
          report(size, size, "checking");
          return await api(job, "POST", "/document/upload/" + link.upload_id + "/complete", undefined, signal);
        }
        const r = await xhr("PUT", job.api + "/admin/videos/" + job.video_id + "/document", {
          headers: { "X-Upload-Ticket": job.ticket, "Content-Type": "application/octet-stream",
                     "X-File-Name": encodeURIComponent(file.name), "X-File-SHA256": sha },
          body: file, onProgress, signal });
        return r && r.data;
      } catch (e) {
        if (e.status === -1) throw e;
        if (e.status === 0 && e.direct && ++directFailures === 2) {
          console.warn("Direct upload to storage is blocked (check the bucket's CORS policy); sending through the server.");
        }
        if (!retryable(e) || attempt === 5) throw e;
        await sleep(Math.min(16000, 1000 * 2 ** attempt));
      }
    }
    throw new Error("The document could not be uploaded.");
  } finally {
    clearInterval(tick);
  }
}

// ════════════════════════════════════════════════════════════════════════════
// Manager (sidebar): runs the uploads
// ════════════════════════════════════════════════════════════════════════════
const Manager = (() => {
  const tasks = new Map();   // id -> {def, files: {video, document}, state, error, line, frac, roleFrac, ctl}
  const finished = new Set(); // done or dismissed: ignore until Python drops them
  let defs = [];

  function broadcast(t, role, extra) {
    CHANNEL.postMessage(Object.assign({ type: "progress", task: t.def.id, role, state: t.state,
                                        frac: t.roleFrac || 0, text: t.line || "", error: t.error || null }, extra || {}));
  }

  function draw() {
    const box = $("tasks");
    const visible = [...tasks.values()].filter((t) => t.def);
    $("manager").classList.toggle("hidden", !visible.length);
    box.innerHTML = "";
    for (const t of visible) {
      const el = document.createElement("div");
      el.className = "task";
      const needs = (!t.def.video_job || t.files.video) && (!t.def.doc_job || t.files.document);
      const status = t.state === "running" ? (t.line || "Starting…")
        : t.state === "failed" ? "" : t.state === "stopped" ? "Stopped." : needs ? "Starting…" : "Waiting for the file…";
      el.innerHTML = '<div class="name"></div><div class="bar"><div></div></div><div class="muted"></div><div class="err hidden"></div><div class="actions"></div>';
      el.querySelector(".name").textContent = t.def.label;
      el.querySelector(".bar > div").style.width = (100 * (t.frac || 0)) + "%";
      el.querySelector(".muted").textContent = status;
      if (t.error) { const e = el.querySelector(".err"); e.textContent = t.error; e.classList.remove("hidden"); }
      const actions = el.querySelector(".actions");
      const button = (text, fn) => { const b = document.createElement("button"); b.className = "small"; b.textContent = text; b.onclick = fn; actions.appendChild(b); };
      if (t.state === "running") button("Stop", () => stop(t.def.id));
      if (t.state === "failed" || t.state === "stopped") {
        if (needs) button("Retry", () => start(t, true));
        button("Dismiss", () => { finished.add(t.def.id); tasks.delete(t.def.id); draw(); emit("dismissed", { task: t.def.id }); });
      }
      box.appendChild(el);
    }
    ST.height();
  }

  function entry(id) {
    if (!tasks.has(id)) tasks.set(id, { def: null, files: {}, state: "waiting" });
    return tasks.get(id);
  }

  function stop(id) { const t = tasks.get(id); if (t && t.ctl) t.ctl.abort(); }

  function maybeStart(t) {
    if (!t.def || t.state !== "waiting") return;
    if (t.def.video_job && !t.files.video) return;
    if (t.def.doc_job && !t.files.document) return;
    start(t, false);
  }

  async function start(t, isRetry) {
    if (t.state === "running") return;
    t.state = "running"; t.error = null; t.ctl = new AbortController(); t.frac = 0;
    const def = t.def, signal = t.ctl.signal;
    draw();
    let video = null;
    let stage = "video";
    try {
      if (def.video_job && !t.videoDone) {
        const f = t.files.video;
        if (def.video_job.sha256 && f.sha && def.video_job.sha256 !== f.sha) throw new Error("This isn't the file the upload was started with. Choose the original file.");
        const speed = speedometer();
        video = await uploadVideo(def.video_job, f.file, signal, (sent, total, phase) => {
          t.roleFrac = sent / total;
          t.frac = def.doc_job ? 0.9 * sent / total : sent / total;
          t.line = phase === "checking" ? "Checking the upload…" : speed(sent, total);
          broadcast(t, "video"); draw();
        });
        t.videoDone = video || true;
        broadcast(t, "video", { state: "done", frac: 1, text: "✓ Uploaded" + (video && video.integrity_verified ? " and verified" : "") });
      }
      video = (t.videoDone && t.videoDone !== true) ? t.videoDone : video;
      let docError = null;
      if (def.doc_job) {
        stage = "document";
        const f = t.files.document;
        const speed = speedometer();
        try {
          video = await uploadDocument(def.doc_job, f.file, f.sha, signal, (sent, total, phase) => {
            t.roleFrac = sent / total;
            t.frac = (def.video_job ? 0.9 : 0) + (def.video_job ? 0.1 : 1) * sent / total;
            t.line = phase === "checking" ? "Checking the document…" : "Document: " + speed(sent, total);
            broadcast(t, "document"); draw();
          });
          broadcast(t, "document", { state: "done", frac: 1, text: "✓ Uploaded" });
        } catch (e) {
          if (e.status === -1 || !def.video_job) throw e;
          docError = friendly(e);                     // the video is in; report the document problem
          broadcast(t, "document", { state: "failed", error: docError });
        }
      }
      t.state = "done";
      finished.add(def.id);
      tasks.delete(def.id);
      draw();
      emit("done", { task: def.id, video, doc_error: docError });
    } catch (e) {
      t.state = e.status === -1 ? "stopped" : "failed";
      t.error = e.status === -1 ? null : friendly(e);
      broadcast(t, stage, { state: t.state, error: t.error });
      draw();
      emit(t.state, { task: def.id, video_id: (def.video_job || def.doc_job).video_id, stage,
                      message: t.error || "Stopped", code: e.code || null });
    }
  }

  CHANNEL.onmessage = (e) => {
    const m = e.data || {};
    if (m.type === "file") {
      if (finished.has(m.task)) return;
      const t = entry(m.task);
      t.files[m.role] = { file: m.file, sha: m.sha };
      CHANNEL.postMessage({ type: "ack", task: m.task, role: m.role });
      maybeStart(t); draw();
    } else if (m.type === "stop") {
      stop(m.task);
    } else if (m.type === "hello") {
      const t = tasks.get(m.task);
      if (t && t.def) broadcast(t, m.role);
    }
  };

  function render(args) {
    defs = args.tasks || [];
    const ids = new Set(defs.map((d) => d.id));
    for (const d of defs) { if (finished.has(d.id)) continue; const t = entry(d.id); t.def = d; maybeStart(t); }
    for (const [id, t] of tasks) if (!ids.has(id) && t.state !== "running" && t.def) tasks.delete(id);
    draw();
  }

  window.addEventListener("beforeunload", (e) => {
    if ([...tasks.values()].some((t) => t.state === "running")) { e.preventDefault(); e.returnValue = ""; }
  });
  return { render };
})();

// ════════════════════════════════════════════════════════════════════════════
// Box (on a page): pick + fingerprint a file, hand it to the manager
// ════════════════════════════════════════════════════════════════════════════
// What Python gets to know about the chosen video (small, plain values).
function probeFacts(info) {
  return {
    readable: !!info.ok, duration: info.duration, width: info.width, height: info.height,
    video_codec: info.videoCodec, video_profile: info.videoProfile, audio_codec: info.audioCodec,
    fast_start: info.fastStart, bitrate: info.bitrate ? Math.round(info.bitrate) : null,
    warnings: info.warnings,
  };
}

const Box = (() => {
  let args = {};
  let file = null, fileSha = null, hashToken = 0;
  let task = null;            // {id, role} handed over (or being handed over)
  let acked = false, busy = false;
  let probe = null;           // what probe.js read from the video (null for documents)

  function show(id, on) { $(id).classList.toggle("hidden", !on); }
  function setBar(frac) { $("bar").style.width = Math.max(0, Math.min(1, frac)) * 100 + "%"; }
  function setStatus(text, cls) { $("status").textContent = text; $("status").className = cls || "muted"; }
  function setError(text) { $("error").textContent = text || ""; show("error", !!text); ST.height(); }
  function showProbe(info) {
    probe = info;
    const line = info && info.ok ? summarize(info) : "";
    $("probe").textContent = line; show("probe", !!line);
    const box = $("notes"); box.textContent = "";
    for (const w of (info ? info.warnings : [])) {
      const d = document.createElement("div"); d.className = "note " + w.level; d.textContent = w.text; box.appendChild(d);
    }
    ST.height();
  }
  function showCover(f) {
    if (f) $("coverimg").src = f.data;
    show("cover", !!f); ST.height();
  }
  function draw() {
    show("drop", !file); show("card", !!file);
    if (file) { $("fname").textContent = file.name; $("fsize").textContent = fmtBytes(file.size); }
    $("change").disabled = busy || !!args.disabled;
    $("browse").disabled = !!args.disabled;
    show("stop", busy);
    ST.height();
  }

  function extOk(name) {
    const exts = args.extensions || [];
    return !exts.length || exts.some((e) => name.toLowerCase().endsWith(e));
  }
  function notSame(exp) {
    return "This isn't the same file as the unfinished upload" + (exp.name ? " (" + exp.name + ", " + fmtBytes(exp.size) + ")" : "") + ". Choose the original file.";
  }

  async function choose(f) {
    if (busy || args.disabled) return;
    setError("");
    if (!extOk(f.name)) { setError("Only " + args.extensions.join(" / ") + " files can be uploaded here."); return; }
    const exp = args.expect;
    if (exp && exp.size && exp.size !== f.size) {
      setError(notSame(exp));
      emit("file", { name: f.name, size: f.size, last_modified: f.lastModified, sha256: null, mismatch: true });
      return;
    }
    if (args.max_bytes && f.size > args.max_bytes) { setError("This file is " + fmtBytes(f.size) + "; the limit is " + fmtBytes(args.max_bytes) + "."); return; }
    if (f.size === 0) { setError("This file is empty."); return; }
    file = f; fileSha = null;
    const token = ++hashToken;
    showProbe(null); showCover(null);
    draw(); setBar(0); show("barwrap", true);
    setStatus("Checking file… 0%");
    const info = args.probe ? await probeVideo(f) : null;   // header only: milliseconds, even for 4 GB
    if (token !== hashToken) return;
    showProbe(info);
    const details = info ? { probe: probeFacts(info) } : {};
    // Cover picture, taken in the background while the file is fingerprinted. Python gets ONE final event
    // with the fingerprint and the picture together: several events in quick succession overwrite each
    // other when a page refresh is slow, and a late event would also swallow the admin's click on Upload.
    const framePromise = args.probe
      ? Promise.race([grabFrame(f, null).catch(() => null), sleep(10000).then(() => null)])
      : Promise.resolve(null);
    emit("file", { name: f.name, size: f.size, last_modified: f.lastModified, sha256: null, ...details });
    try {
      const sha = await sha256File(f, (done) => {
        if (token !== hashToken) return;
        setBar(done / f.size); setStatus("Checking file… " + Math.floor(100 * done / f.size) + "%");
      }, () => token !== hashToken);
      if (token !== hashToken) return;
      const frame = await framePromise;
      if (token !== hashToken) return;
      fileSha = sha;
      const mismatch = !!(exp && exp.sha256 && exp.sha256 !== sha);
      setBar(1); show("barwrap", false);
      if (args.probe) showCover(frame);
      if (mismatch) { setStatus(""); setError(notSame(exp)); }
      else setStatus("✓ Ready to upload", "ok");
      const cover = args.probe ? { data: frame ? frame.data : null, width: frame ? frame.width : null, height: frame ? frame.height : null } : undefined;
      emit("file", { name: f.name, size: f.size, last_modified: f.lastModified, sha256: sha, mismatch, ...details, ...(cover ? { cover } : {}) });
    } catch (e) {
      if (token !== hashToken) return;
      setStatus(""); setError("Could not read this file: " + e.message);
    }
    draw();
  }

  // Hand the file to the manager until it acknowledges (it may still be loading).
  async function handOver(t) {
    task = t; acked = false;
    if (!file || !fileSha) {
      setError("Choose the file again to continue."); return;
    }
    busy = true; setError(""); show("barwrap", true); setBar(0); setStatus("Starting…"); draw();
    for (let i = 0; i < 40 && !acked && task === t; i++) {
      CHANNEL.postMessage({ type: "file", task: t.id, role: t.role, file, sha: fileSha });
      await sleep(500);
    }
    if (!acked && task === t) {
      busy = false; setStatus(""); setError("The upload couldn't start (the Uploads panel in the sidebar isn't responding). Reload the page and try again."); draw();
      emit("error", { task: t.id, role: t.role, message: "handover failed" });
    }
  }

  CHANNEL.addEventListener("message", (e) => {
    const m = e.data || {};
    if (!task || m.task !== task.id || m.role !== task.role) return;
    if (m.type === "ack" && !acked) {
      acked = true;
      setStatus(task.role === "document" ? "Queued — goes up right after the video." : "Starting…");
      emit("handed", { task: task.id, role: task.role });
    } else if (m.type === "progress") {
      show("barwrap", true); setBar(m.frac);
      if (m.state === "running") { busy = true; setStatus(m.text); setError(""); }
      else if (m.state === "done") { busy = false; setBar(1); setStatus(m.text || "✓ Uploaded", "ok"); }
      else if (m.state === "failed") { busy = false; setStatus(""); setError(m.error || "Upload failed."); }
      else if (m.state === "stopped") { busy = false; setStatus("Stopped. Retry it from the Uploads panel in the sidebar."); }
      draw();
    }
  });

  function bind() {
    $("browse").onclick = () => $("picker").click();
    $("change").onclick = () => {
      if (busy) return;
      file = null; fileSha = null; hashToken++; task = null; showProbe(null); showCover(null); setError(""); setStatus(""); draw(); emit("cleared");
    };
    $("stop").onclick = () => { if (task) CHANNEL.postMessage({ type: "stop", task: task.id }); };
    $("picker").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) choose(f); };
    const drop = $("drop");
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) choose(f); });
    document.addEventListener("dragover", (e) => e.preventDefault());
    document.addEventListener("drop", (e) => e.preventDefault());
  }

  function render(a) {
    args = a;
    $("label").textContent = args.label || "Video file";
    $("picker").accept = args.accept || "";
    $("limits").textContent = (args.extensions || []).map((e) => e.slice(1).toUpperCase()).join(", ")
      + (args.max_bytes ? " · up to " + fmtBytes(args.max_bytes) : "");
    const t = args.task;
    if (t && (!task || task.id !== t.id || task.role !== t.role)) handOver(t);
    if (t) CHANNEL.postMessage({ type: "hello", task: t.id, role: t.role });
    draw();
  }
  return { bind, render };
})();

// ── render from Python ──────────────────────────────────────────────────────
let mode = null;
window.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "streamlit:render") return;
  const args = event.data.args || {};
  const theme = event.data.theme;
  if (theme) {
    const r = document.documentElement.style;
    if (theme.primaryColor) r.setProperty("--primary", theme.primaryColor);
    if (theme.textColor) r.setProperty("--text", theme.textColor);
    if (theme.backgroundColor) r.setProperty("--bg", theme.backgroundColor);
    if (theme.secondaryBackgroundColor) r.setProperty("--soft", theme.secondaryBackgroundColor);
  }
  if (!mode) {
    mode = args.mode === "manager" ? "manager" : "box";
    if (mode === "box") { $("box").classList.remove("hidden"); Box.bind(); }
  }
  (mode === "manager" ? Manager : Box).render(args);
});
new ResizeObserver(() => ST.height()).observe(document.getElementById("root"));
// Streamlit can reset a component's frame height when the admin switches pages
// (the sidebar manager isn't reloaded, so nothing else would tell it again).
setInterval(() => ST.height(), 1000);
ST.ready();
ST.height();
