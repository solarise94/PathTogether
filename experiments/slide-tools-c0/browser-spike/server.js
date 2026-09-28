#!/usr/bin/env node
// Static server for the C0 spike with a CSP / COOP-COEP matrix.
// The CSP header is set on EVERY response (html, worker js, wasm, css) so
// dedicated workers also get their own policy — that is what governs WASM
// compilation inside the worker.
//
// Usage: node server.js --root <dir> --port <n> --csp <none|A|B|C|D> [--coop]
//
// Modes:
//   A: default-src 'self'                                    (expect: workers ok, WASM blocked)
//   B: A + script-src 'self' 'wasm-unsafe-eval'              (expect: full pass)
//   C: explicit hardening incl. worker-src 'self'            (expect: full pass)
//   D: C minus worker-src                                    (measured: PASSES — worker-src
//                              falls back to script-src when script-src is present)
//   E: default-src 'none' + script-src only                  (expect: wasm FETCH blocked:
//                              connect-src falls back to default-src 'none')

'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

const ROOT = path.resolve(arg('root', '.'));
const PORT = Number(arg('port', '8931'));
const CSP_MODE = arg('csp', 'none');
const COOP = process.argv.includes('--coop');

const CSPS = {
  A: "default-src 'self'",
  B: "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'",
  C: "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
  D: "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; style-src 'self'; img-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
  E: "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'",
};

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.wasm': 'application/wasm',
  '.css': 'text/css',
  '.json': 'application/json',
};

const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent(req.url.split('?')[0]);
  let p = path.normalize(path.join(ROOT, urlPath));
  if (!p.startsWith(ROOT)) { res.writeHead(403).end(); return; }
  if (urlPath === '/' || urlPath.endsWith('/')) p = path.join(p, 'index.html');
  fs.readFile(p, (err, data) => {
    if (err) { res.writeHead(404).end('not found'); return; }
    const headers = {
      'Content-Type': MIME[path.extname(p)] || 'application/octet-stream',
      'Cache-Control': 'no-store',
    };
    if (CSP_MODE !== 'none' && CSPS[CSP_MODE]) headers['Content-Security-Policy'] = CSPS[CSP_MODE];
    if (COOP) {
      headers['Cross-Origin-Opener-Policy'] = 'same-origin';
      headers['Cross-Origin-Embedder-Policy'] = 'require-corp';
    }
    res.writeHead(200, headers);
    res.end(data);
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`spike-server root=${ROOT} port=${PORT} csp=${CSP_MODE} coop=${COOP}`);
});
