#!/usr/bin/env node
// Gate: no whole-file materialization anywhere in the production runner.
// Greps the shipped sources for every known whole-file read pattern; the
// only allowed arrayBuffer() calls are on explicitly bounded slices, and
// readAsArrayBuffer only ever sees File.slice() results inside the worker's
// ≤1 MiB chunk bridge. This test FAILS the gate if any forbidden pattern
// appears (plan §3/§5, C2 exit condition "never whole-file in memory").
'use strict';
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '../../..');
const FILES = [
  'static/tools/slide-transform/engine.js',
  'static/tools/slide-transform/worker.js',
  'static/tools/slide-transform/runner.js',
  // C4 upload path: the artifact goes to COS as per-part slice() blobs only
  'static/upload/cos-uploader.js',
  'static/tools/tools-slides-upload.js',
  'static/tools/tools-slides.js',
];

// [pattern, why-forbidden]
const FORBIDDEN = [
  [/\.getFile\(\)\s*\.\s*arrayBuffer\(\)/, 'getFile().arrayBuffer() materializes the whole file'],

  [/new\s+Response\(\s*[\w.$]+\s*\)/, 'new Response(file) buffers the body'],
  [/MEMFS/i, 'MEMFS whole-file wasm filesystem'],
  [/writeToBuffer/i, 'writeToBuffer whole-file API'],
  [/createObjectURL\(\s*[\w.$]+\s*\)\s*\.\s*click/,
    'whole-file download fallback (forbidden for large exports)'],
];

let failures = 0;
for (const rel of FILES) {
  const p = path.join(REPO, rel);
  const src = fs.readFileSync(p, 'utf8');
  const lines = src.split('\n');
  lines.forEach((line, i) => {
    for (const [re, why] of FORBIDDEN) {
      if (re && re.test(line)) {
        console.error(`FORBIDDEN ${rel}:${i + 1}: ${why}\n    ${line.trim()}`);
        failures += 1;
      }
    }
    if (/readAsArrayBuffer\(/.test(line)) {
      const ctx = lines.slice(Math.max(0, i - 3), i + 1).join(' ');
      if (!/\.slice\s*\(/.test(ctx)) {
        console.error(`FORBIDDEN ${rel}:${i + 1}: readAsArrayBuffer without a bounded slice in scope\n    ${line.trim()}`);
        failures += 1;
      }
    }
  });
  // every arrayBuffer() must be on a bounded slice (≤16 MiB literal or
  // min(...) bound) — crude but grep-auditable
  lines.forEach((line, i) => {
    if (/\.arrayBuffer\(\)/.test(line)) {
      const ctx = lines.slice(Math.max(0, i - 3), i + 1).join(' ');
      if (!/\.slice\s*\(/.test(ctx)) {
        console.error(`SUSPECT ${rel}:${i + 1}: arrayBuffer() without a visible bounded slice:\n    ${line.trim()}`);
        failures += 1;
      }
    }
  });
}

// the worker's sync bridge reads the staged OPFS copy straight into wasm
// memory; FileReaderSync is banned outright (its per-call ArrayBuffers pile
// up during long synchronous wasm calls — c2-runner-report §4.3)
const worker = fs.readFileSync(path.join(REPO, FILES[1]), 'utf8');
if (!/srcHandle\.read\(view\(ptr, len\), \{ at: offset \}\)/.test(worker)) {
  console.error('FORBIDDEN: worker sync-read bridge is not the staged sync-handle path');
  failures += 1;
}
for (const rel of FILES) {
  const src = fs.readFileSync(path.join(REPO, rel), 'utf8').replace(/\/\/.*$/gm, '');
  if (/FileReaderSync/.test(src)) {
    console.error(`FORBIDDEN ${rel}: FileReaderSync in code`);
    failures += 1;
  }
}
// streaming copies (staging, export) must refill one BYOB buffer
for (const rel of [FILES[1], FILES[2]]) {
  const src = fs.readFileSync(path.join(REPO, rel), 'utf8');
  if (!/getReader\(\{ mode: 'byob' \}\)/.test(src)) {
    console.error(`FORBIDDEN ${rel}: streaming copy is not a BYOB reused-buffer reader`);
    failures += 1;
  }
}

if (failures) {
  console.error(`\nNO-WHOLE-FILE GATE FAILED: ${failures} violation(s)`);
  process.exit(1);
}
console.log('no-whole-file gate: PASS (bounded slices only in ' + FILES.join(', ') + ')');
