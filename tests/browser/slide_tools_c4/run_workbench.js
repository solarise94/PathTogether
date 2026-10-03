#!/usr/bin/env node
// C4 工作台原生直传回归（真实 Flask /app + page.route 假 ingestion/COS）：
// 重构前后各跑一次，比较请求序列必须逐条一致（Item 2：原生直传不回退、
// 同请求、同阶段文案——此处锁定请求面；阶段文案由 vitest cos-upload 锁定）。
//
//   node tests/browser/slide_tools_c4/run_workbench.js --out <json>
//     [--creds <json>] [--port 8953] [--keep]   # --keep：复用已运行的服务
'use strict';
const fs = require('fs');
const path = require('path');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8953'));
const CREDS = L.arg('creds', path.join(L.GATE, 'creds.json'));
const OUT = L.arg('out', path.join(L.GATE, 'workbench-seq.json'));

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  let server = null;
  if (!process.argv.includes('--reuse-server')) server = await L.startServer(PORT, CREDS);
  const creds = L.readCreds(CREDS);

  let context = null;
  let page = null;
  ({ context, page } = await L.C3.launch('wb'));
  const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partBytes: 8 });
  const requests = [];
  context.on('request', (r) => {
    const u = new URL(r.url());
    if (u.origin === creds.cosOrigin || u.pathname.startsWith('/api/ingestions')) {
      requests.push({
        method: r.method(),
        path: u.pathname,
        partNumber: u.origin === creds.cosOrigin ? u.searchParams.get('partNumber') : null,
        partNumbers: r.method() === 'POST' && u.pathname.endsWith('/parts/sign')
          ? (JSON.parse(r.postData() || '{}').part_numbers || null)
          : null,
      });
    }
  });

  try {
    await L.login(page, PORT, creds, 'user', '/app');
    if (!page.url().includes('/app')) throw new Error(`login landed at ${page.url()}`);
    await page.waitForFunction(() => !!(window.HP_UPLOAD && window.HP_APP_BOOTSTRAP
      && window.HP_APP_BOOTSTRAP.capabilities
      && window.HP_APP_BOOTSTRAP.capabilities.cos_upload
      && window.HP_APP_BOOTSTRAP.capabilities.cos_upload.available === true),
      null, { timeout: 30000 });

    // 64 B 假 .tif（假后端 8 B/片 → 8 片）：驱动真实 uploadFile → uploadFileCos
    await page.evaluate(() => {
      const bytes = new Uint8Array(64);
      for (let i = 0; i < bytes.length; i++) bytes[i] = i;
      const f = new File([bytes], 'wb-regression.tif', { type: 'image/tiff' });
      window.HP_UPLOAD.uploadFile(f);
    });

    // 完成 = 行文案切「入库完成」（upload.stage.done → i18n 键在测试替身外，
    // 真页面经 HP_I18N；等待 toast 或 complete+viewable 序列出现）
    await (async () => {
      const deadline = Date.now() + 60000;
      for (;;) {
        const done = await page.evaluate(() => {
          const rows = document.querySelectorAll('.upload-item-status');
          for (const r of rows) {
            const txt = r.textContent || '';
            if (txt.includes('入库完成') || txt.includes('Done') || txt.includes('published')) return true;
            if (txt.includes('上传失败') || txt.includes('failed')) return 'failed';
          }
          return false;
        });
        if (done === true) break;
        if (done === 'failed') throw new Error('workbench upload row failed');
        if (Date.now() > deadline) throw new Error('workbench upload did not finish');
        await new Promise((r) => setTimeout(r, 300));
      }
    })();

    const cosJobs = await page.evaluate(() => localStorage.getItem('pt.cos.jobs'));
    const result = {
      when: new Date().toISOString(),
      sequence: requests,
      cosJobsAfter: JSON.parse(cosJobs || '[]'),
      fake: {
        creates: fake.st.creates,
        signs: fake.st.signs,
        puts: fake.st.puts.map((p) => ({ partNumber: p.partNumber, bytes: p.bytes })),
        completeReqs: fake.st.completeReqs,
      },
    };
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
    console.log(`WORKBENCH SEQ captured: ${requests.length} requests -> ${OUT}`);
    console.log(JSON.stringify(result.fake, null, 2));
    await context.close();

    // ------------------------------------------------------------------ //
    // U1：工作台行字节进度。page.route 拦截下请求体不走网络，XHR upload
    // progress 无从回调——把假 COS 域名映射到本地 HTTPS 假 COS（真实
    // socket）+ CDP 上行限速：上传行的字节文本/条宽在 PUT 完成前按字节移动。
    // ------------------------------------------------------------------ //
    const cosHost = new URL(creds.cosOrigin).host;
    const cos = await L.startLocalCos(cosHost);
    const wb = await L.C3.launch('wb-bytes', [], [
      `--host-resolver-rules=MAP ${cosHost}:443 127.0.0.1:${cos.port}`,
      '--ignore-certificate-errors',
    ]);
    try {
      const fake2 = await L.fakeUploadRoutes(wb.page, creds.cosOrigin,
        { partBytes: 65536, skipCosRoute: true });
      await L.login(wb.page, PORT, creds, 'user', '/app');
      await wb.page.waitForFunction(() => !!(window.HP_UPLOAD && window.HP_APP_BOOTSTRAP
        && window.HP_APP_BOOTSTRAP.capabilities
        && window.HP_APP_BOOTSTRAP.capabilities.cos_upload
        && window.HP_APP_BOOTSTRAP.capabilities.cos_upload.available === true),
        null, { timeout: 30000 });
      const cdp = await wb.context.newCDPSession(wb.page);
      await cdp.send('Network.enable');
      await cdp.send('Network.emulateNetworkConditions', {
        offline: false, latency: 0,
        downloadThroughput: 1024 * 1024,
        uploadThroughput: 32 * 1024,   // 32 KB/s：192 KiB 上传 ≈ 6s
      });
      await wb.page.evaluate(() => {
        const bytes = new Uint8Array(192 * 1024);
        for (let i = 0; i < bytes.length; i++) bytes[i] = i & 0xff;
        const f = new File([bytes], 'wb-byte-progress.tif', { type: 'image/tiff' });
        window.HP_UPLOAD.uploadFile(f);
      });
      const byteTexts = [];
      const widths = [];
      const deadline = Date.now() + 90000;
      for (;;) {
        const snap = await wb.page.evaluate(() => {
          const rows = document.querySelectorAll('.upload-item');
          const row = rows[rows.length - 1];
          if (!row) return { done: false };
          const bytesEl = row.querySelector('.upload-item-bytes');
          const bar = row.querySelector('.upload-item-bar-fill');
          const status = row.querySelector('.upload-item-status');
          const txt = (status && status.textContent) || '';
          return {
            done: /入库完成|Done/.test(txt),
            failed: /上传失败|failed/.test(txt),
            bytes: (bytesEl && bytesEl.textContent) || '',
            width: (bar && bar.style && bar.style.width) || '',
          };
        });
        if (snap.bytes) byteTexts.push(snap.bytes);
        if (snap.width) widths.push(snap.width);
        if (snap.done || snap.failed || Date.now() > deadline) break;
        await new Promise((r) => setTimeout(r, 250));
      }
      const midBytes = [...new Set(byteTexts)]
        .filter((t) => /已传输|transferred/i.test(t) && !/192\.0 KB \/ 192\.0 KB/.test(t));
      const midWidths = [...new Set(widths)].filter((w) => w !== '100%' && w !== '0%');
      if (midBytes.length < 3) {
        throw new Error(`workbench row byte movement insufficient: ${JSON.stringify([...new Set(byteTexts)].slice(0, 6))}`);
      }
      const putBytes2 = cos.st.puts.reduce((s, p) => s + p.bytes, 0);
      if (putBytes2 !== 192 * 1024) throw new Error(`local COS PUT bytes ${putBytes2}`);
      if (fake2.st.creates.length !== 1) throw new Error(`creates=${fake2.st.creates.length}`);
      console.log('PASS [wb-byte-progress] ' + JSON.stringify({
        midByteTexts: midBytes.length, midWidths: midWidths.length,
        puts: cos.st.puts.map((p) => p.bytes),
      }));
    } finally {
      await wb.context.close();
      cos.server.close();
    }
  } finally {
    if (context) await context.close().catch(() => {});
    if (server) server.kill('SIGTERM');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
