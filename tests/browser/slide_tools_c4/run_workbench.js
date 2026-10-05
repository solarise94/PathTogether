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

    // ------------------------------------------------------------------ //
    // 阶段 1（先转换后上传）：整页拖放分流。document 级 drop（真实
    // DataTransfer）：
    //   A. 拖入 OME-TIFF → 直接创建 ingestion（创建体带 direct_class）；
    //   B. 拖入 JPEG 编码 SVS（TIFF 头压缩=7）→ 不建任务，行内给「在本机
    //      转换并上传」；
    //   C. 拖入 MRXS 文件夹（fake 目录 entry 走真实 importDroppedDirectory
    //      遍历）→ 交接消息携带 bundle 成员数组 + folderName。
    // ------------------------------------------------------------------ //
    const wb2 = await L.C3.launch('wb-direct');
    try {
      const fake3 = await L.fakeUploadRoutes(wb2.page, creds.cosOrigin,
        { partBytes: 64 });
      await L.login(wb2.page, PORT, creds, 'user', '/app');
      await wb2.page.waitForFunction(() => !!(window.HP_UPLOAD
        && window.HP_SLIDE_SNIFF && window.HP_APP_BOOTSTRAP
        && window.HP_APP_BOOTSTRAP.capabilities
        && window.HP_APP_BOOTSTRAP.capabilities.cos_upload
        && window.HP_APP_BOOTSTRAP.capabilities.cos_upload.available === true),
        null, { timeout: 30000 });

      // File 不能跨 evaluate 序列化：字节进页面后再构造 File 并派发 drop
      const dropFile = (name, bytes) => wb2.page.evaluate(
        ([n, b]) => {
          const f = new File([new Uint8Array(b)], n, { type: 'image/tiff' });
          const dt = new DataTransfer();
          dt.items.add(f);
          document.dispatchEvent(new DragEvent('drop', {
            dataTransfer: dt, bubbles: true, cancelable: true,
          }));
        }, [name, Array.from(bytes)]);

      const mkTiff = (compression, description) => {
        // 最小小端 classic TIFF：IFD0 = Compression(259)+ImageDescription(270)
        const desc = new TextEncoder().encode(description || '');
        const ifdSize = 2 + 12 * (compression ? 2 : 1) + 4;
        const heapAt = 8 + ifdSize;
        const total = heapAt + desc.length;
        const buf = new ArrayBuffer(total);
        const dv = new DataView(buf);
        dv.setUint8(0, 0x49); dv.setUint8(1, 0x49);
        dv.setUint16(2, 42, true);
        dv.setUint32(4, 8, true);
        const entries = compression ? 2 : 1;
        let at = 8;
        dv.setUint16(at, entries, true); at += 2;
        const write = (tag, type, count, value) => {
          dv.setUint16(at, tag, true);
          dv.setUint16(at + 2, type, true);
          dv.setUint32(at + 4, count, true);
          dv.setUint32(at + 8, value, true);
          at += 12;
        };
        if (compression) write(259, 3, 1, compression);
        if (desc.length) {
          write(270, 2, desc.length, desc.length <= 4 ? 0 : heapAt);
          new Uint8Array(buf, heapAt, desc.length).set(desc);
        }
        dv.setUint32(at, 0, true);
        return new Uint8Array(buf);
      };
      const OME_DESC = '<?xml version="1.0"?><OME xmlns=' +
        '"http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\0';

      // A：整页拖入 OME-TIFF → 直接上传（direct_class 声明）
      await dropFile('drop-ome.tif', mkTiff(0, OME_DESC));
      const deadlineA = Date.now() + 60000;
      for (;;) {
        const state = await wb2.page.evaluate(() => {
          const rows = document.querySelectorAll('.upload-item-status');
          const txt = rows.length ? rows[rows.length - 1].textContent : '';
          return { done: /入库完成|Done/.test(txt), failed: /上传失败|failed/.test(txt) };
        });
        if (state.done) break;
        if (state.failed) throw new Error('A: OME drop upload row failed');
        if (Date.now() > deadlineA) throw new Error('A: OME drop upload not finished');
        await new Promise((r) => setTimeout(r, 250));
      }
      const omeCreate = fake3.st.creates.find((c) => c.filename === 'drop-ome.tif');
      if (!omeCreate) throw new Error('A: OME drop did not create ingestion');
      if (omeCreate.direct_class !== 'ome-tiff') {
        throw new Error(`A: direct_class=${omeCreate.direct_class}`);
      }
      console.log('PASS [wb-drop-ome] ' + JSON.stringify(omeCreate));

      // B：整页拖入 JPEG 编码 SVS → 走「本机转换并上传」（零 ingestion）
      await dropFile('aperio.svs', mkTiff(7, ''));
      await wb2.page.waitForFunction(() => {
        const rows = document.querySelectorAll('.upload-item');
        const row = rows[rows.length - 1];
        if (!row) return false;
        const btns = [...row.querySelectorAll('button.upload-item-btn')];
        return btns.some((b) => /在本机转换并上传/.test(b.textContent || ''));
      }, null, { timeout: 15000 });
      if (fake3.st.creates.some((c) => c.filename === 'aperio.svs')) {
        throw new Error('B: SVS drop should not create ingestion');
      }
      console.log('PASS [wb-drop-svs-convert] rows offer browser convert');

      // C：拖入 MRXS 文件夹（fake 目录 entry → 真实 importDroppedDirectory
      // 遍历）→ 弹窗交接消息带 bundle 成员数组 + folderName
      await wb2.page.evaluate(() => {
        window.__handoffMsgs = [];
        window.open = () => ({
          closed: false,
          postMessage: (m) => { window.__handoffMsgs.push(m); },
        });
      });
      await wb2.page.evaluate(() => {
        const bytesOf = (s) => new TextEncoder().encode(s);
        const mk = (name, text) => new File([bytesOf(text)], name);
        // 最小 FileSystemDirectoryEntry 形状（importDroppedDirectory 只用
        // isFile/file()/isDirectory/createReader/readEntries）
        const fileEntry = (name, file) => ({
          isFile: true, isDirectory: false, name,
          file: (ok) => ok(file),
        });
        const dirEntry = {
          isFile: false, isDirectory: true, name: 'CMU-1',
          createReader() {
            let done = false;
            return {
              readEntries(ok) {
                if (done) return ok([]);
                done = true;
                ok([fileEntry('CMU-1.mrxs', mk('CMU-1.mrxs', 'mrxs')),
                fileEntry('Slidedat.ini', mk('Slidedat.ini', 'ini'))]);
              },
            };
          },
        };
        window.HP_UPLOAD.importDroppedDirectory(dirEntry);
      });
      await wb2.page.waitForFunction(() => {
        const rows = document.querySelectorAll('.upload-item');
        const row = rows[rows.length - 1];
        if (!row) return false;
        return [...row.querySelectorAll('button.upload-item-btn')]
          .some((b) => /在本机转换并上传/.test(b.textContent || ''));
      }, null, { timeout: 15000 });
      await wb2.page.evaluate(() => {
        const rows = document.querySelectorAll('.upload-item');
        const row = rows[rows.length - 1];
        const btn = [...row.querySelectorAll('button.upload-item-btn')]
          .find((b) => /在本机转换并上传/.test(b.textContent || ''));
        btn.click();
      });
      await wb2.page.waitForFunction(
        () => (window.__handoffMsgs || []).some(
          (m) => m && m.type === 'pt:convert-upload-handoff' && m.bundle),
        null, { timeout: 15000 });
      const msg = await wb2.page.evaluate(() => window.__handoffMsgs
        .find((m) => m.type === 'pt:convert-upload-handoff' && m.bundle));
      if (!Array.isArray(msg.bundle) || msg.bundle.length !== 2) {
        throw new Error(`C: bundle members ${JSON.stringify(msg.bundle)}`);
      }
      if (msg.bundle[0].relPath !== 'CMU-1/CMU-1.mrxs' || !msg.bundle[0].file) {
        throw new Error(`C: member[0] ${JSON.stringify(msg.bundle[0])}`);
      }
      if (msg.folderName !== 'CMU-1' || msg.file !== null) {
        throw new Error(`C: folderName=${msg.folderName} file=${msg.file}`);
      }
      if (fake3.st.creates.some((c) => /\.mrxs/.test(c.filename || ''))) {
        throw new Error('C: MRXS folder should not create ingestion');
      }
      console.log('PASS [wb-drop-mrxs-folder] ' + JSON.stringify({
        members: msg.bundle.map((m) => m.relPath), folderName: msg.folderName,
      }));
    } finally {
      await wb2.context.close();
    }
  } finally {
    if (context) await context.close().catch(() => {});
    if (server) server.kill('SIGTERM');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
