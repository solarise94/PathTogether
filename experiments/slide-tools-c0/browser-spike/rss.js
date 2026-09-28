'use strict';
// Sample the resident set size (RSS) of an entire browser process tree via
// /proc: walk /proc/<pid>/task/*/children recursively from the root browser
// PID, then sum statm resident pages. RSS double-counts shared mappings
// between chromium processes; that is the (conservative) number the plan
// asks to record. Peak per-process breakdown is kept for the report.
const fs = require('fs');
const PAGE = 4096;

function childrenOf(pid) {
  const kids = new Set();
  let tasks;
  try { tasks = fs.readdirSync(`/proc/${pid}/task`); } catch { return kids; }
  for (const t of tasks) {
    try {
      const txt = fs.readFileSync(`/proc/${pid}/task/${t}/children`, 'utf8');
      for (const k of txt.trim().split(/\s+/)) if (k) kids.add(Number(k));
    } catch { /* raced */ }
  }
  return kids;
}

function treeOf(rootPid) {
  const seen = new Set([rootPid]);
  const q = [rootPid];
  while (q.length) {
    const p = q.shift();
    for (const k of childrenOf(p)) {
      if (!seen.has(k)) { seen.add(k); q.push(k); }
    }
  }
  return [...seen];
}

function procName(pid) {
  try {
    const parts = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').filter(Boolean);
    // chromium argv[1] is usually --type=... ; keep a short tag
    return (parts[1] || parts[0] || '').slice(0, 60);
  } catch { return '?'; }
}

function sample(rootPid) {
  const t = Date.now();
  let rss = 0;
  const procs = [];
  for (const p of treeOf(rootPid)) {
    try {
      const f = fs.readFileSync(`/proc/${p}/statm`, 'utf8').trim().split(/\s+/);
      const r = Number(f[1]) * PAGE;
      rss += r;
      procs.push({ pid: p, rss: r, name: procName(p) });
    } catch { /* raced */ }
  }
  return { t, nProcs: procs.length, rss, procs };
}

module.exports = { sample, treeOf };
