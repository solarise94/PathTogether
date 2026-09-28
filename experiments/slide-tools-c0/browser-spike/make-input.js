#!/usr/bin/env node
// Deterministic >4 GiB input generator. Byte contract (must match
// site/common.js inputByteAt and rust/src/lib.rs):
//   input byte at absolute offset i (i a safe integer):
//     w = h32(floor(i/4) ^ 0x5bf03635); byte = (w >>> ((i % 4) * 8)) & 0xff
// The generator writes whole 32-bit little-endian words.
//
// Usage: node make-input.js <sizeBytes> <outPath>

'use strict';
const fs = require('fs');

function h32(x) {
  x >>>= 0;
  x ^= x >>> 16; x = Math.imul(x, 0x85ebca6b) >>> 0;
  x ^= x >>> 13; x = Math.imul(x, 0xc2b2ae35) >>> 0;
  x ^= x >>> 16;
  return x >>> 0;
}
function inputByteAt(i) {
  const w = h32(Math.floor(i / 4) ^ 0x5bf03635);
  return (w >>> ((i % 4) * 8)) & 0xff;
}

const size = Number(process.argv[2]);
const out = process.argv[3];
if (!size || !out) { console.error('usage: node make-input.js <sizeBytes> <outPath>'); process.exit(2); }

const CH = 1 << 20; // 1 MiB
const buf = Buffer.alloc(CH);
const fd = fs.openSync(out, 'w');
const t0 = Date.now();
let off = 0;
while (off < size) {
  const n = Math.min(CH, size - off);
  const w0 = off / 4; // offsets are multiples of 4
  for (let w = w0, end = (off + n) / 4; w < end; w++) {
    buf.writeUInt32LE(h32(w ^ 0x5bf03635), (w - w0) * 4);
  }
  fs.writeSync(fd, buf, 0, n);
  off += n;
}
fs.closeSync(fd);
const ms = Date.now() - t0;

// self-check at spot offsets incl. beyond 2^32
const fd2 = fs.openSync(out, 'r');
const spots = [0, 12345, 2 ** 32 + 2048, size - 16];
const chk = Buffer.alloc(16);
let spotOk = true;
for (const s of spots) {
  fs.readSync(fd2, chk, 0, 16, s);
  for (let k = 0; k < 16; k++) {
    if (chk[k] !== inputByteAt(s + k)) { spotOk = false; console.error(`MISMATCH at ${s}+${k}`); }
  }
}
fs.closeSync(fd2);
console.log(JSON.stringify({ out, size, ms, spotOk }));
