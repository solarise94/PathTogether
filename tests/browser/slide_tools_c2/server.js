#!/usr/bin/env node
// C2 test harness static server.
// Serves the harness page(s) from this directory and the production runner
// from static/tools/slide-transform at /tools/, with the ADR mode-C CSP on
// EVERY response (html, module worker js, wasm) — the product page will use
// the same policy; tests must not run under anything weaker.
//
//   node server.js --port 8941 [--csp C|off]
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

const HERE = __dirname;
const REPO = path.resolve(HERE, '../../..');
const TOOLS = path.join(REPO, 'static/tools/slide-transform');
const PORT = Number(arg('port', '8941'));
const CSP_MODE = arg('csp', 'C');

// ADR mode C (c0-adr-browser.md §10) — explicit minimal surface.
const CSP_C = "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; " +
  "worker-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self'; " +
  "base-uri 'none'; object-src 'none'; frame-ancestors 'none'";

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.wasm': 'application/wasm',
  '.css': 'text/css',
  '.json': 'application/json',
  '.png': 'image/png',
};

const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent(req.url.split('?')[0]);
  let p;
  if (urlPath.startsWith('/tools/')) {
    p = path.normalize(path.join(TOOLS, urlPath.slice('/tools/'.length)));
    if (!p.startsWith(TOOLS)) { res.writeHead(403).end(); return; }
  } else {
    p = path.normalize(path.join(HERE, urlPath));
    if (!p.startsWith(HERE)) { res.writeHead(403).end(); return; }
    if (urlPath === '/' || urlPath.endsWith('/')) p = path.join(p, 'harness.html');
  }
  fs.readFile(p, (err, data) => {
    if (err) { res.writeHead(404).end('not found'); return; }
    const headers = {
      'Content-Type': MIME[path.extname(p)] || 'application/octet-stream',
      'Cache-Control': 'no-store',
    };
    if (CSP_MODE !== 'off') headers['Content-Security-Policy'] = CSP_C;
    res.writeHead(200, headers);
    res.end(data);
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`c2-harness root=${HERE} tools=${TOOLS} port=${PORT} csp=${CSP_MODE}`);
});
