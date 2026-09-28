import { readFileSync, openSync, writeSync, closeSync, readdirSync } from "node:fs";
import crypto from "node:crypto";
const base = "/home/solarise/ZCodeProject/histopilot-suite/PathTogether/static/tools/slide-transform/";
const glue = await import(base + "slide_transform.js");
glue.initSync(readFileSync(base + "slide_transform_bg.wasm"));
const dir = process.argv[2];
const src = readFileSync(dir + "/" + readdirSync(dir).filter(f => f.endsWith(".kfb")).sort()[0]);
const outPath = process.argv[3];
const fd = openSync(outPath, "w");
globalThis.stHostSourceSize = () => src.length;
globalThis.stHostRead = (o, l) => src.subarray(Number(o), Number(o) + l);
globalThis.stHostWrite = (o, d) => { writeSync(fd, d, 0, d.length, Number(o)); };
globalThis.stHostTruncate = () => {};
globalThis.stHostFlush = () => {};
const sc = {};
class MemScratch { constructor() { this.buf = new Uint8Array(1 << 20); }
  ensure(n) { if (n <= this.buf.length) return; let c = this.buf.length; while (c < n) c *= 2; const x = new Uint8Array(c); x.set(this.buf); this.buf = x; }
  write(o, d) { o = Number(o); this.ensure(o + d.length); this.buf.set(d, o); }
  read(o, l) { o = Number(o); return this.buf.slice(o, o + l); }
  truncate(n) { this.ensure(Number(n)); } }
globalThis.stHostScratchOpen = (n) => { sc[n] = new MemScratch(); };
globalThis.stHostScratchRead = (n, o, l) => sc[n].read(o, l);
globalThis.stHostScratchWrite = (n, o, d) => sc[n].write(o, d);
globalThis.stHostScratchTruncate = (n, len) => sc[n].truncate(len ?? 0);
globalThis.stHostScratchFlush = () => {};
globalThis.stHostProgress = () => {};
globalThis.stHostCancelled = () => false;
const t = Date.now();
const r = JSON.parse(glue.convert(false, ""));
closeSync(fd);
const h = crypto.createHash("sha256").update(readFileSync(outPath)).digest("hex");
console.log(JSON.stringify({ output_bytes: r.output_bytes, sha256: h, secs: (Date.now() - t) / 1000, rss_mb: Math.round(process.memoryUsage().rss / 1048576) }));
