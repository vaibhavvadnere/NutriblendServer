// node run_probe.mjs <path/to/probe.js> <video files…>  →  JSON {fileName: probe result}
// Used by tests/test_probe_js.py to run the browser's MP4 reader on real files.
import { createRequire } from "node:module";
import { openAsBlob } from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const { probeVideo, summarize } = require(path.resolve(process.argv[2]));
const out = {};
for (const file of process.argv.slice(3)) {
  const info = await probeVideo(await openAsBlob(file));   // lazy Blob: only the bytes it reads
  out[path.basename(file)] = { ...info, summary: summarize(info), codes: info.warnings.map((w) => w.code) };
}
console.log(JSON.stringify(out));
