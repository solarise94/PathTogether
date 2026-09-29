// Review: how much does probe read, and what does it cost in RSS?
'use strict';
const fs = require('fs');
const path = require('path');
const L = require(require('path').resolve('tests/browser/slide_tools_c2/lib.js'));
const rss = require(require('path').resolve('experiments/slide-tools-c0/browser-spike/rss.js'));
const MIB = 2 ** 20;
(async () => {
  const PORT = 8952;
  const server = await L.startServer(PORT);
  const label = 'probecost';
  const { context, page } = await L.launch({ label });
  const udd = path.join(L.GATE, 'profiles', `profile-${label}`);
  let root = null;
  for (let i = 0; i < 20 && !root; i++) {
    for (const e of fs.readdirSync('/proc')) {
      if (!/^\d+$/.test(e)) continue;
      try { const c = fs.readFileSync(`/proc/${e}/cmdline`, 'utf8'); if (c.includes(`--user-data-dir=${udd}`) && !c.includes('--type=')) root = Number(e); } catch { /* */ }
    }
    if (!root) await new Promise((r) => setTimeout(r, 500));
  }
  await L.open(page, PORT);
  for (const [alias, p] of process.argv.slice(2).map((a) => a.split('='))) {
    await L.clearJobs(page);
    await L.setFile(page, p);
    await page.waitForTimeout(2000);
    const base = rss.sample(root).rss; let peak = base;
    const iv = setInterval(() => { peak = Math.max(peak, rss.sample(root).rss); }, 200);
    const n0 = await page.evaluate(() => window.__c2.events.length);
    const t = Date.now();
    await page.evaluate(() => window.__c2.probe());
    clearInterval(iv);
    const dbg = await page.evaluate((n) => window.__c2.events.slice(n).filter((e) => e.kind === 'dbg-counters').map((e) => e.data), n0);
    const c = dbg[dbg.length - 1] || {};
    console.log(`${alias}: probe ${Date.now() - t} ms, delta ${((peak - base) / MIB).toFixed(0)} MiB, reads ${c.reads}, readBytes ${((c.readBytes || 0) / MIB).toFixed(1)} MiB, sz64 ${c.sz64} sz4k ${c.sz4k} sz128k ${c.sz128k} big ${c.szbig}`);
  }
  await context.close(); server.kill('SIGKILL');
  fs.rmSync(udd, { recursive: true, force: true });
})().catch((e) => { console.error(e); process.exit(1); });
