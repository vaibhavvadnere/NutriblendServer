// probe.js — reads an MP4 file's length, codecs and layout in the browser, before upload.
//
// Only box headers are read (a few KB), plus the "moov" box (the file's index, usually
// under a few MB) wherever it is in the file — so even a 4 GB video is checked in
// milliseconds. Nothing is uploaded or decoded.
//
//   const info = await probeVideo(file);
//   info = { ok, brand, duration (s) | null, width, height, videoCodec, videoProfile, avcProfile,
//            audioCodec, bitrate (bits/s) | null, fastStart (bool|null), fragmented,
//            warnings: [{ code, level: "error" | "warn" | "info", text }] }
//   "error" = the file is probably broken (not an MP4, cut short, no index, no picture).
//
// Used by the upload box (upload.js) and unit-tested with node (tests/js/probe.test.js).

(function (root) {
  "use strict";

  const MAX_MOOV = 64 * 1024 * 1024;   // a bigger index than this is not worth reading in a browser
  const MAX_TOP_BOXES = 256;

  const CODECS = {
    avc1: "H.264", avc3: "H.264",
    hvc1: "HEVC (H.265)", hev1: "HEVC (H.265)",
    av01: "AV1", vp09: "VP9", vp08: "VP8",
    mp4v: "MPEG-4 Visual", s263: "H.263", mjpa: "Motion JPEG", jpeg: "Motion JPEG",
    apch: "ProRes", apcn: "ProRes", apcs: "ProRes", apco: "ProRes", ap4h: "ProRes", ap4x: "ProRes",
    mp4a: "AAC", "ac-3": "AC-3", "ec-3": "E-AC-3", Opus: "Opus", fLaC: "FLAC", alac: "ALAC",
    ".mp3": "MP3", samr: "AMR", sowt: "PCM", twos: "PCM", lpcm: "PCM", ulaw: "PCM", alaw: "PCM",
  };

  function ascii(dv, off) {
    return String.fromCharCode(dv.getUint8(off), dv.getUint8(off + 1), dv.getUint8(off + 2), dv.getUint8(off + 3));
  }
  function u64(dv, off) { return dv.getUint32(off) * 4294967296 + dv.getUint32(off + 4); }

  async function bytes(file, start, end) {
    return new DataView(await file.slice(start, Math.min(end, file.size)).arrayBuffer());
  }

  // Boxes inside dv[start, end) → [{type, start (of content), end}]
  function children(dv, start, end) {
    const out = [];
    let pos = start;
    while (pos + 8 <= end && out.length < 512) {
      let size = dv.getUint32(pos);
      const type = ascii(dv, pos + 4);
      let head = 8;
      if (size === 1) { if (pos + 16 > end) break; size = u64(dv, pos + 8); head = 16; }
      else if (size === 0) size = end - pos;
      if (size < head || pos + size > end) break;
      out.push({ type, start: pos + head, end: pos + size });
      pos += size;
    }
    return out;
  }
  const find = (list, type) => list.find((b) => b.type === type);

  function parseTrack(dv, trak) {
    const kids = children(dv, trak.start, trak.end);
    const out = { kind: null, codec: null, fourcc: null, width: null, height: null, avcProfile: null, rotated: false };
    const tkhd = find(kids, "tkhd");
    if (tkhd && tkhd.end - tkhd.start >= 84) {
      const v = dv.getUint8(tkhd.start);
      const m = tkhd.start + (v === 1 ? 48 : 40);                  // 3x3 matrix (16.16 fixed)
      if (m + 8 <= tkhd.end) {
        const a = dv.getInt32(m), b = dv.getInt32(m + 4);
        out.rotated = a === 0 && b !== 0;                           // 90° / 270°
      }
    }
    const mdia = find(kids, "mdia");
    if (!mdia) return out;
    const mk = children(dv, mdia.start, mdia.end);
    const hdlr = find(mk, "hdlr");
    if (hdlr && hdlr.end - hdlr.start >= 12) {
      const h = ascii(dv, hdlr.start + 8);
      out.kind = h === "vide" ? "video" : h === "soun" ? "audio" : h;
    }
    const minf = find(mk, "minf");
    const stbl = minf && find(children(dv, minf.start, minf.end), "stbl");
    const stsd = stbl && find(children(dv, stbl.start, stbl.end), "stsd");
    if (!stsd || stsd.end - stsd.start < 16) return out;
    const entry = stsd.start + 8;                                   // first sample entry: size, type, ...
    const fourcc = ascii(dv, entry + 4);
    out.fourcc = fourcc;
    out.codec = CODECS[fourcc] || fourcc;
    const body = entry + 8;
    const entryEnd = Math.min(stsd.end, entry + dv.getUint32(entry));
    if (out.kind === "video" && body + 78 <= entryEnd) {
      out.width = dv.getUint16(body + 24);
      out.height = dv.getUint16(body + 26);
      const avcC = find(children(dv, body + 78, entryEnd), "avcC");
      if (avcC && avcC.end - avcC.start >= 4) out.avcProfile = dv.getUint8(avcC.start + 1);
    }
    if (out.kind === "audio" && fourcc === "mp4a") out.codec = mpegAudioName(dv, body, entryEnd);
    return out;
  }

  // "mp4a" is AAC most of the time, but MP3 (and others) are stored the same way; the esds box says which.
  function mpegAudioName(dv, body, end) {
    try {
      const version = body + 10 <= end ? dv.getUint16(body + 8) : 0;
      const head = version === 1 ? 44 : version === 2 ? 64 : 28;
      const esds = find(children(dv, body + head, end), "esds");
      if (!esds) return "AAC";
      let pos = esds.start + 4;
      const tag = () => {                       // descriptor tag + its (variable length) size
        const t = dv.getUint8(pos++);
        let len = 0, b;
        do { b = dv.getUint8(pos++); len = (len << 7) | (b & 0x7f); } while (b & 0x80 && pos < esds.end);
        return { t, len };
      };
      let d = tag();
      if (d.t === 3) {                          // ES descriptor: id(2) flags(1) [+ optional fields]
        const flags = dv.getUint8(pos + 2);
        pos += 3;
        if (flags & 0x80) pos += 2;
        if (flags & 0x40) pos += 1 + dv.getUint8(pos);
        if (flags & 0x20) pos += 2;
        d = tag();
      }
      if (d.t !== 4) return "AAC";
      const oti = dv.getUint8(pos);             // object type indication
      if (oti === 0x40 || (oti >= 0x66 && oti <= 0x68)) return "AAC";
      if (oti === 0x69 || oti === 0x6b) return "MP3";
      if (oti === 0xa5) return "AC-3";
      return "MPEG audio (0x" + oti.toString(16) + ")";
    } catch (e) { return "AAC"; }
  }

  function parseMoov(dv) {
    const top = children(dv, 0, dv.byteLength);
    const moov = find(top, "moov");
    if (!moov) return null;
    const kids = children(dv, moov.start, moov.end);
    const res = { duration: null, tracks: [], fragmented: !!find(kids, "mvex"), compressed: !!find(kids, "cmov") };
    const mvhd = find(kids, "mvhd");
    if (mvhd && mvhd.end - mvhd.start >= 20) {
      const v = dv.getUint8(mvhd.start);
      const scale = dv.getUint32(mvhd.start + (v === 1 ? 20 : 12));
      const dur = v === 1 ? u64(dv, mvhd.start + 24) : dv.getUint32(mvhd.start + 16);
      if (scale > 0 && dur > 0 && !(v === 0 && dur === 0xffffffff)) res.duration = dur / scale;
    }
    for (const t of kids.filter((b) => b.type === "trak")) res.tracks.push(parseTrack(dv, t));
    return res;
  }

  const PROFILES = { 66: "Baseline", 77: "Main", 88: "Extended", 100: "High", 110: "High 10", 122: "High 4:2:2", 244: "High 4:4:4", 44: "CAVLC 4:4:4" };

  function fmtDuration(s) {
    const t = Math.round(s), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), x = t % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(x)}` : `${m}:${pad(x)}`;
  }

  async function probeVideo(file) {
    const info = {
      ok: false, brand: null, duration: null, width: null, height: null,
      videoCodec: null, videoProfile: null, avcProfile: null, audioCodec: null,
      bitrate: null, fastStart: null, fragmented: false, warnings: [],
    };
    const warn = (code, text, level) => info.warnings.push({ code, level: level || "warn", text });
    try {
      // 1. top-level boxes (headers only)
      const boxes = [];
      let pos = 0;
      while (pos < file.size && boxes.length < MAX_TOP_BOXES) {
        const dv = await bytes(file, pos, pos + 16);
        if (dv.byteLength < 8) break;
        let size = dv.getUint32(0);
        const type = ascii(dv, 4);
        let head = 8;
        if (size === 1) { if (dv.byteLength < 16) break; size = u64(dv, 8); head = 16; }
        else if (size === 0) size = file.size - pos;
        if (size < head || pos + size > file.size + 0) {
          if (boxes.length && pos + size > file.size) boxes.push({ type, pos, size, head, truncated: true });
          break;
        }
        boxes.push({ type, pos, size, head });
        pos += size;
      }
      const first = boxes[0];
      if (!first || !["ftyp", "moov", "mdat", "free", "skip", "wide", "styp"].includes(first.type)) {
        warn("not_mp4", "This doesn't look like an MP4 file. Phones may not be able to play it — export it as MP4 (H.264 + AAC).", "error");
        return info;
      }
      const ftyp = boxes.find((b) => b.type === "ftyp");
      if (ftyp && ftyp.size >= 16) info.brand = ascii(await bytes(file, ftyp.pos + ftyp.head, ftyp.pos + ftyp.head + 4), 0);
      if (boxes.some((b) => b.truncated)) {
        warn("truncated", "This file seems to be cut short (its last part is missing). It may not play to the end — export or download it again.", "error");
      }

      // 2. fast-start: is the index (moov) before the picture data (mdat)?
      const moovBox = boxes.find((b) => b.type === "moov");
      const mdatBox = boxes.find((b) => b.type === "mdat");
      if (!moovBox) {
        warn("no_index", "The video's index (moov) is missing, so it can't be played. The recording may not have finished — export it again.", "error");
        return info;
      }
      info.fastStart = !mdatBox || moovBox.pos < mdatBox.pos;

      // 3. read the index
      if (moovBox.size > MAX_MOOV) {
        warn("big_index", "This file's index is unusually large, so its details couldn't be read here.", "info");
      } else {
        const dv = await bytes(file, moovBox.pos, moovBox.pos + moovBox.size);
        const moov = parseMoov(dv);
        if (moov && moov.compressed) {
          warn("compressed_index", "This file's index is compressed (an old QuickTime feature); details couldn't be read.", "info");
        } else if (moov) {
          info.fragmented = moov.fragmented;
          info.duration = moov.duration;
          const video = moov.tracks.find((t) => t.kind === "video");
          const audio = moov.tracks.find((t) => t.kind === "audio");
          if (video) {
            info.videoCodec = video.codec;
            info.avcProfile = video.avcProfile;
            info.videoProfile = video.avcProfile != null ? (PROFILES[video.avcProfile] || String(video.avcProfile)) : null;
            const swap = video.rotated;
            info.width = swap ? video.height : video.width;
            info.height = swap ? video.width : video.height;
          }
          if (audio) info.audioCodec = audio.codec;
          if (!video) {
            warn("no_video", "No video picture was found in this file (only sound or nothing).", "error");
          } else if (video.fourcc !== "avc1" && video.fourcc !== "avc3") {
            warn("codec", `This video uses ${video.codec}, not H.264. Many Android phones can't play it — export it as MP4 with H.264 video and AAC audio.`);
          } else if ([110, 122, 244, 44].includes(video.avcProfile)) {
            warn("profile", `This H.264 video is “${info.videoProfile}” (10-bit or 4:2:2/4:4:4 colour). Many phones can't play it — export it as “High” or “Main” profile, 8-bit.`);
          }
          if (video && !audio) warn("no_audio", "This video has no sound track.", "info");
          if (audio && audio.codec !== "AAC") {
            warn("audio_codec", `The sound is ${audio.codec}, not AAC. Some phones will play the picture without sound — use AAC audio.`);
          }
          if (video && info.duration == null && !moov.fragmented) {
            warn("no_duration", "The video's length couldn't be read. It may be damaged; check that it plays on your computer.");
          }
          if (info.duration != null && info.duration < 1) warn("short", "This video is shorter than one second.");
          if (info.duration && info.duration > 0) {
            info.bitrate = (file.size * 8) / info.duration;
            if (info.bitrate > 25e6) {
              warn("bitrate", `The bitrate is very high (${(info.bitrate / 1e6).toFixed(0)} Mbit/s). It will stutter on slow connections — a 4–8 Mbit/s export looks the same on a phone.`);
            }
          }
        }
      }
      if (info.fastStart === false) {
        warn("slow_start", "This file isn't “fast-start”: its index is at the end, so a phone can't start playing until the whole video has downloaded. " +
          "Fix it without losing quality:  ffmpeg -i input.mp4 -c copy -movflags +faststart output.mp4");
      }
      info.ok = true;
    } catch (e) {
      warn("unreadable", "Couldn't read this file's details (" + (e && e.message ? e.message : e) + "). It can still be uploaded.", "info");
    }
    return info;
  }

  /** One line for the upload box, e.g. "H.264 High · 1920×1080 · 12:34 · fast-start". */
  function summarize(info) {
    const parts = [];
    if (info.videoCodec) parts.push(info.videoCodec + (info.videoProfile && info.videoCodec === "H.264" ? " " + info.videoProfile : ""));
    if (info.width && info.height) parts.push(info.width + "×" + info.height);
    if (info.duration != null) parts.push(fmtDuration(info.duration));
    if (info.fastStart != null) parts.push(info.fastStart ? "fast-start" : "not fast-start");
    return parts.join(" · ");
  }

  const api = { probeVideo, summarize, fmtDuration };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== "undefined" ? window : globalThis);
