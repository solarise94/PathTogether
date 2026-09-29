const http = require('http'), fs = require('fs'), path = require('path');
const { chromium } = require(require.resolve('playwright', { paths: [path.resolve('tests/browser/slide_tools_c2')] }));
const rss = require(path.resolve('experiments/slide-tools-c0/browser-spike/rss.js'));
const D = __dirname;
const srv = http.createServer((q, s) => { const f = path.join(D, q.url === '/' ? 'index.html' : q.url.slice(1)); s.setHeader('content-type', f.endsWith('.js') ? 'text/javascript' : 'text/html'); s.end(fs.readFileSync(f)); }).listen(8961);
(async () => {
  const udd = path.join(D, 'prof'); fs.rmSync(udd, { recursive: true, force: true });
  const ctx = await chromium.launchPersistentContext(udd, { headless: true, args: ['--disable-dev-shm-usage'] });
  const page = ctx.pages()[0];
  await page.goto('http://127.0.0.1:8961/'); await page.setInputFiles('#f', process.argv[2]);
  let root = null;
  for (const e of fs.readdirSync('/proc')) { try { const c = fs.readFileSync(`/proc/${e}/cmdline`, 'utf8'); if (c.includes(`--user-data-dir=${udd}`) && !c.includes('--type=')) root = Number(e); } catch {} }
  await page.waitForTimeout(2000);
  for (const ye of ['frs4m', 'frs4m-yield', 'frs1m', 'async', 'byob', 'opfs']) {
    await page.waitForTimeout(3000);
    const base = rss.sample(root).rss; let peak = base;
    const iv = setInterval(() => { peak = Math.max(peak, rss.sample(root).rss); }, 200);
    const r = await page.evaluate((y) => window.run(y), ye);
    clearInterval(iv);
    console.log(`${ye}: delta peak ${((peak - base) / 2 ** 20).toFixed(0)} MiB, ${r.ms} ms read${r.copyMs ? ', copy ' + r.copyMs + ' ms' : ''}`);
  }
  await ctx.close(); srv.close(); fs.rmSync(udd, { recursive: true, force: true });
})();
