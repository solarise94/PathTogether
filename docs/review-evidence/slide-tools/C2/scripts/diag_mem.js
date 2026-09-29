// Review diagnostic: per-process, per-phase RSS breakdown (RssAnon/RssFile/RssShmem).
'use strict';
const fs = require('fs');
const path = require('path');
const L = require(require('path').resolve('tests/browser/slide_tools_c2/lib.js'));
const rss = require(require('path').resolve('experiments/slide-tools-c0/browser-spike/rss.js'));
const MIB = 2 ** 20;
const input = process.argv[2];
const label = process.argv[3] || 'diag';
const profile = process.argv[4] || 'saver';
const out = path.join(__dirname, `${label}.json`);

function status(pid) {
  const o = {};
  try {
    for (const line of fs.readFileSync(`/proc/${pid}/status`, 'utf8').split('\n')) {
      const m = line.match(/^(RssAnon|RssFile|RssShmem):\s+(\d+)/);
      if (m) o[m[1]] = Number(m[2]) * 1024;
    }
  } catch { /* raced */ }
  return o;
}
function kind(pid) {
  try {
    const c = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8');
    const t = c.match(/--type=([\w-]+)/); const u = c.match(/--utility-sub-type=([\w.]+)/);
    return (t ? t[1] : 'browser') + (u ? ':' + u[1].split('.').pop() : '') + (t && t[1] === 'renderer' ? ':' + pid : '');
  } catch { return '?'; }
}
function snap(root) {
  const by = {};
  let tot = { RssAnon: 0, RssFile: 0, RssShmem: 0 };
  for (const p of rss.treeOf(root)) {
    const s = status(p); const k = kind(p);
    by[k] = by[k] || { RssAnon: 0, RssFile: 0, RssShmem: 0 };
    for (const f of Object.keys(tot)) { by[k][f] += s[f] || 0; tot[f] += s[f] || 0; }
  }
  return { tot, by };
}

(async () => {
  const PORT = 8951;
  const server = await L.startServer(PORT);
  const { context, page } = await L.launch({ label });
  const udd = path.join(L.GATE, 'profiles', `profile-${label}`);
  let root = null;
  for (let i = 0; i < 20 && !root; i++) {
    for (const e of fs.readdirSync('/proc')) {
      if (!/^\d+$/.test(e)) continue;
      try {
        const c = fs.readFileSync(`/proc/${e}/cmdline`, 'utf8');
        if (c.includes(`--user-data-dir=${udd}`) && !c.includes('--type=')) root = Number(e);
      } catch { /* */ }
    }
    if (!root) await new Promise((r) => setTimeout(r, 500));
  }
  await L.open(page, PORT);
  await page.waitForTimeout(3000);
  const base = snap(root);
  await L.clearJobs(page);
  await L.setFile(page, input);
  let phase = 'start';
  const rows = [];
  const peakBy = {};
  const iv = setInterval(async () => {
    const s = snap(root);
    const sum = s.tot.RssAnon + s.tot.RssFile + s.tot.RssShmem;
    rows.push({ t: Date.now(), phase, ...s.tot, sum });
    if (!peakBy[phase] || sum > peakBy[phase].sum) peakBy[phase] = { sum, tot: s.tot, by: s.by };
  }, 500);
  const pv = setInterval(async () => {
    try {
      phase = await page.evaluate(() => {
        const ev = window.__c2.events.filter((e) => e.kind === 'phase' || e.kind === 'state');
        const e = ev[ev.length - 1];
        return e ? (e.data.phase || e.data.to || e.kind) : 'start';
      });
    } catch { /* */ }
  }, 1000);
  const jobId = await page.evaluate((p) => window.__c2.start({ profileId: p, skipDiskPrecheck: true }), profile);
  const done = await page.evaluate(() => window.__c2.awaitDone(90 * 60 * 1000));
  clearInterval(iv); clearInterval(pv);
  const baseSum = base.tot.RssAnon + base.tot.RssFile + base.tot.RssShmem;
  const report = { label, input: path.basename(input), profile, base, done: { ok: done.ok, sha256: done.sha256 },
    peakByPhase: Object.fromEntries(Object.entries(peakBy).map(([k, v]) => [k, {
      deltaMiB: Math.round((v.sum - baseSum) / MIB),
      anonMiB: Math.round(v.tot.RssAnon / MIB), fileMiB: Math.round(v.tot.RssFile / MIB), shmemMiB: Math.round(v.tot.RssShmem / MIB),
      by: Object.fromEntries(Object.entries(v.by).map(([p, x]) => [p, `anon ${Math.round(x.RssAnon / MIB)} file ${Math.round(x.RssFile / MIB)} shm ${Math.round(x.RssShmem / MIB)}`])),
    }])),
    events: await page.evaluate(() => window.__c2.events.filter((e) => e.kind === 'phase').map((e) => [e.t, e.data.phase])) };
  fs.writeFileSync(out, JSON.stringify(report, null, 1));
  fs.writeFileSync(out.replace('.json', '-timeline.json'), JSON.stringify(rows));
  console.log(JSON.stringify(report.peakByPhase, null, 1));
  await context.close(); server.kill('SIGKILL');
  fs.rmSync(udd, { recursive: true, force: true });
})().catch((e) => { console.error(e); process.exit(1); });
