#!/usr/bin/env node
// C0 browser spike runner (Playwright, persistent context).
//
// Phases:
//   blank     — baseline RSS of the whole browser tree on blank.html only
//   full      — WASM chunk pipeline: >4 GiB input -> WASM -> OPFS >4 GiB
//               random-write sink, reopen+verify, export proxy; RSS sampled
//               throughout; baseline sampled first in the same browser.
//   terminate — kill both workers mid-write (worker.terminate()), then probe
//               OPFS from the page main thread (size + chunk-0 bytes).
//   reload    — page.reload() mid-write, then probe OPFS persistence.
//
// Browsers: --browser chromium (Playwright bundled headless) | chrome
// (channel "chrome", Google Chrome 153; add --headed for headed mode).
//
// All scratch (profile dirs) lives under PathTogether/.gate-tmp — never
// /tmp (RAM-backed tmpfs).
//
// For cgroup-simulated 4 GB / 8 GB runs, wrap THIS script:
//   systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \
//     --unit slide-c0-<name> node run-spike.js --label <name> ...
// (the node runner + chromium tree then share the constrained scope; set
//  CGROUP_DESC to label the report).

'use strict';
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require('playwright');
const rss = require('./rss.js');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}
const has = (name) => process.argv.includes('--' + name);

const SPIKE_DIR = __dirname;
const REPO = path.resolve(SPIKE_DIR, '../../../..'); // histopilot-suite root
const PT = path.join(REPO, 'PathTogether');
const GATE = path.join(PT, '.gate-tmp/slide-tools-c0/browser');

const LABEL = arg('label', 'run');
const BROWSER = arg('browser', 'chromium'); // chromium | chrome
const HEADED = has('headed');
const PHASE = arg('phase', 'full'); // blank | full | terminate | reload
const CSP = arg('csp', 'none'); // none | A | B | C | D
const COOP = has('coop');
const PORT = Number(arg('port', '8931'));
const INPUT = arg('input', path.join(GATE, 'input.bin'));
const OUT_GIB = Number(arg('out-gib', '4.5'));
const IN_MIN_GIB = Number(arg('in-min-gib', '4.4'));
const WINDOW_MB = Number(arg('window-mb', '192'));
const CHUNK_MIN_MB = Number(arg('chunk-min-mb', '1'));
const CHUNK_MAX_MB = Number(arg('chunk-max-mb', '4'));
const SEED = Number(arg('seed', '42'));
const SAMPLES = Number(arg('samples', '20'));
const KILL_AT_GIB = Number(arg('kill-at-gib', '1.5'));
const RELOAD_AT_GIB = Number(arg('reload-at-gib', '1.5'));
const RUN_EXPORT = !has('no-export');
const BASELINE_S = Number(arg('baseline-seconds', '5'));
const LOG_DIR = arg('log-dir', path.join(GATE, 'logs'));

fs.mkdirSync(LOG_DIR, { recursive: true });
fs.mkdirSync(path.join(GATE, 'profiles'), { recursive: true });

function log(...a) { console.log(`[${LABEL}]`, ...a); }

function statsFrom(timeline) {
  if (!timeline.length) return null;
  const arr = timeline.map((s) => s.rss);
  const mean = arr.reduce((x, y) => x + y, 0) / arr.length;
  const peak = Math.max(...arr);
  return {
    n: arr.length,
    mean, peak,
    peakSample: timeline[arr.indexOf(peak)],
    maxProcs: Math.max(...timeline.map((s) => s.nProcs)),
  };
}

// Playwright 1.62 persistent-context Browser facade has no process(); find
// the root browser pid by scanning /proc for our unique --user-data-dir.
// NOTE: chromium rewrites argv with spaces (proctitle), so match on the raw
// cmdline with NULs normalized, and confirm the exe is a chrome binary.
function findBrowserPid(userDataDir) {
  const needle = `--user-data-dir=${userDataDir}`;
  const pids = [];
  for (const ent of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(ent)) continue;
    try {
      const raw = fs.readFileSync(`/proc/${ent}/cmdline`, 'utf8');
      if (!raw.includes(userDataDir)) continue;
      const norm = raw.replace(/\0/g, ' ');
      if (!norm.includes(needle)) continue;
      let exe = '';
      try { exe = fs.readlinkSync(`/proc/${ent}/exe`) || ''; } catch { /* permission */ }
      const cmd0 = norm.split(' ')[0] || '';
      if (!/chrome|headless/i.test(exe + ' ' + cmd0)) continue;
      pids.push(Number(ent));
    } catch { /* raced */ }
  }
  return pids.length ? Math.min(...pids) : null;
}

function pidAlive(pid) {
  if (!pid) return false;
  try { fs.readFileSync(`/proc/${pid}/statm`); return true; } catch { return false; }
}

async function findBrowserPidRetry(userDataDir, logFn) {
  for (let i = 0; i < 12; i++) {
    const pid = findBrowserPid(userDataDir);
    if (pid) return pid;
    if (logFn && i === 4) logFn('browser pid not found yet, retrying...');
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

async function main() {
  const server = spawn(process.execPath, [
    path.join(SPIKE_DIR, 'server.js'),
    '--root', path.join(SPIKE_DIR, 'site'),
    '--port', String(PORT),
    '--csp', CSP,
    ...(COOP ? ['--coop'] : []),
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  server.stderr.on('data', (d) => log('server stderr:', String(d).trim()));
  // belt-and-braces: never leak the static server even on hard crashes
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });

  const userDataDir = path.join(GATE, 'profiles', `profile-${LABEL}`);
  fs.rmSync(userDataDir, { recursive: true, force: true });

  const report = {
    label: LABEL, phase: PHASE, browser: BROWSER, headed: HEADED,
    csp: CSP, coop: COOP, port: PORT,
    config: {
      outSize: Math.round(OUT_GIB * 2 ** 30), windowBytes: WINDOW_MB * 2 ** 20,
      chunkMin: CHUNK_MIN_MB * 2 ** 20, chunkMax: CHUNK_MAX_MB * 2 ** 20, seed: SEED,
      runExport: RUN_EXPORT, input: INPUT, inputBytes: null,
    },
    cgroup: process.env.CGROUP_DESC || null,
    startedAt: new Date().toISOString(),
  };

  let context = null;
  let browserExit = null;
  let closingContext = false;
  let browserPid = null;
  try {
    await new Promise((res, rej) => {
      server.stdout.on('data', (d) => { log('server:', String(d).trim()); res(); });
      server.on('error', rej);
      const t = setTimeout(() => rej(new Error('server start timeout')), 8000);
      server.on('close', () => { clearTimeout(t); });
    });

    const launchOpts = {
      headless: !HEADED,
      viewport: { width: 800, height: 600 },
      // NOTE: do NOT pass `env` here. An explicit env object (even a plain
      // copy of process.env) makes Google Chrome stable self-close during
      // launchPersistentContext on this host (Playwright 1.62 + Chrome 153);
      // bulk browser data (profile + OPFS) lives under GATE on disk anyway,
      // so no TMPDIR override is needed.
    };
    if (BROWSER === 'chrome') launchOpts.channel = 'chrome';

    context = await chromium.launchPersistentContext(userDataDir, launchOpts);
    const browser = context.browser();
    browserPid = await findBrowserPidRetry(userDataDir, log);
    context.on('close', () => { if (!closingContext) browserExit = { code: null, signal: 'context-closed' }; });
    const browserVersion = browser ? await browser.version() : 'unknown';
    report.browserVersion = browserVersion;
    report.browserPid = browserPid;
    log(`browser=${BROWSER} headed=${HEADED} version=${browserVersion} pid=${browserPid}`);

    const page = context.pages()[0] || (await context.newPage());
    page.on('console', (m) => { if (m.type() === 'error') log('console.error:', m.text()); });
    page.on('pageerror', (e) => log('pageerror:', String(e).slice(0, 300)));

    // ---- baseline on blank page ----
    await page.goto(`http://127.0.0.1:${PORT}/blank.html`, { waitUntil: 'load' });
    await page.waitForTimeout(2500);
    const baselineSamples = [];
    const tBaseEnd = Date.now() + BASELINE_S * 1000;
    while (Date.now() < tBaseEnd) {
      if (browserPid) baselineSamples.push(rss.sample(browserPid));
      await page.waitForTimeout(400);
    }
    report.baseline = statsFrom(baselineSamples) || { n: 0, mean: 0, peak: 0, maxProcs: 0, empty: true };
    if (!browserPid) throw new Error('could not locate browser pid for RSS sampling');
    log(`baseline rss mean=${(report.baseline.mean / 1048576).toFixed(0)} MiB peak=${(report.baseline.peak / 1048576).toFixed(0)} MiB procs=${report.baseline.maxProcs}`);

    if (PHASE !== 'blank') {
      await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'load' });
      report.coopCoep = await page.evaluate(() => window.coopCoepInfo());
      report.pageWasmProbe = await page.evaluate(() => window.probePageWasm());
      const inStat = fs.statSync(INPUT);
      report.config.inputBytes = inStat.size;
      if (inStat.size < IN_MIN_GIB * 2 ** 30) throw new Error(`input too small: ${inStat.size}`);
      await page.setInputFiles('input#file', INPUT);

      const cfg = {
        inSize: inStat.size,
        outSize: Math.round(OUT_GIB * 2 ** 30),
        windowBytes: WINDOW_MB * 2 ** 20,
        chunkMin: CHUNK_MIN_MB * 2 ** 20,
        chunkMax: CHUNK_MAX_MB * 2 ** 20,
        seed: SEED,
        samples: SAMPLES,
        runExport: RUN_EXPORT,
      };
      if (PHASE === 'terminate') { cfg.killAtBytes = KILL_AT_GIB * 2 ** 30; cfg.stopAfterWrite = true; }
      if (PHASE === 'reload') { cfg.reloadAtBytes = RELOAD_AT_GIB * 2 ** 30; cfg.stopAfterWrite = true; }

      const timeline = [];
      const sampler = setInterval(() => { if (browserPid) timeline.push(rss.sample(browserPid)); }, 400);

      const watchdog = new Promise((_, rej) => setTimeout(() => rej(new Error('job watchdog 25min')), 25 * 60 * 1000));
      const jobP = page.evaluate((c) => window.runSpike(c), cfg);
      jobP.catch(() => { /* expected in terminate/reload phases; errors
                            surfaced via the phase-specific result below */ });

      let jobResult = null, jobError = null;
      if (PHASE === 'terminate') {
        const deadline = Date.now() + 25 * 60 * 1000;
        while (Date.now() < deadline) {
          const st = await page.evaluate(() => window.__spikeState);
          if (st.killReport) break;
          await page.waitForTimeout(300);
        }
        const st = await page.evaluate(() => ({
          status: window.__spikeState.status,
          killReport: window.__spikeState.killReport,
          written: window.__spikeState.written,
        }));
        jobResult = { terminated: true, state: st };
        log('killReport:', JSON.stringify(st.killReport));
      } else if (PHASE === 'reload') {
        const deadline = Date.now() + 25 * 60 * 1000;
        let reloadDone = false;
        while (Date.now() < deadline && !reloadDone) {
          const st = await page.evaluate(() => ({ reloadAt: window.__spikeState.reloadAt, written: window.__spikeState.written }));
          if (st.reloadAt) {
            const minBytes = st.reloadAt - 64 * 1024 * 1024; // in-flight slack
            await page.reload({ waitUntil: 'load' });
            const persisted = await page.evaluate((mb) => window.checkOpfsPersisted(mb), minBytes);
            jobResult = { reloaded: true, writtenAtReload: st.reloadAt, persisted };
            reloadDone = true;
            log('persisted:', JSON.stringify(persisted));
          } else {
            await page.waitForTimeout(300);
          }
        }
        if (!reloadDone) jobError = 'reload threshold never reached';
      } else {
        try { jobResult = await Promise.race([jobP, watchdog]); }
        catch (e) { jobError = String((e && e.message) || e); }
      }
      clearInterval(sampler);

      report.result = jobResult;
      report.error = jobError;
      report.job = statsFrom(timeline);
      if (report.job) {
        report.job.deltaPeakVsBaselineMean = report.job.peak - report.baseline.mean;
        report.job.deltaPeakVsBaselinePeak = report.job.peak - report.baseline.peak;
        log(`job rss peak=${(report.job.peak / 1048576).toFixed(0)} MiB delta(vs base mean)=${(report.job.deltaPeakVsBaselineMean / 1048576).toFixed(0)} MiB procs=${report.job.maxProcs}`);
      }
      if (jobResult && !jobResult.terminated && !jobResult.reloaded) {
        log('result ok=', jobResult.ok, 'writeMiBs=', jobResult.writeMiBs,
          'verify failures=', JSON.stringify(jobResult.verify && jobResult.verify.failures));
      }
    }
    report.browserExit = browserExit;
    report.aliveAtEnd = !browserExit && pidAlive(browserPid);
    closingContext = true;
    await context.close();
  } catch (e) {
    report.fatal = String((e && e.stack) || e);
    report.browserExit = browserExit;
    report.aliveAtEnd = !browserExit && pidAlive(browserPid);
    log('FATAL', report.fatal);
    try { if (context) await context.close(); } catch { /* already gone */ }
  } finally {
    server.kill('SIGTERM');
  }

  report.finishedAt = new Date().toISOString();
  const outPath = path.join(LOG_DIR, `${LABEL}.json`);
  fs.writeFileSync(outPath, JSON.stringify(report, null, 2));
  log('report written:', outPath, 'ok=', report.result ? report.result.ok !== false && !report.error : null);
  process.exit(report.fatal ? 3 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
