self.onmessage = async (e) => {
  const { file, mode } = e.data;
  const B = 4 << 20; let x = 0; const t = Date.now();
  if (mode === 'frs4m' || mode === 'frs4m-yield') {
    const r = new FileReaderSync();
    for (let off = 0, n = 0; off < file.size; off += B, n++) {
      x ^= new Uint8Array(r.readAsArrayBuffer(file.slice(off, Math.min(off + B, file.size))))[0];
      if (mode === 'frs4m-yield' && n % 16 === 0) await new Promise((res) => setTimeout(res, 0));
    }
  } else if (mode === 'frs1m') {
    const r = new FileReaderSync(); const b = 1 << 20;
    for (let off = 0; off < file.size; off += b) x ^= new Uint8Array(r.readAsArrayBuffer(file.slice(off, Math.min(off + b, file.size))))[0];
  } else if (mode === 'async') {
    for (let off = 0; off < file.size; off += B) x ^= new Uint8Array(await file.slice(off, Math.min(off + B, file.size)).arrayBuffer())[0];
  } else if (mode === 'byob') {
    const rd = file.stream().getReader({ mode: 'byob' }); let buf = new ArrayBuffer(B);
    for (;;) { const { value, done } = await rd.read(new Uint8Array(buf)); if (done) break; x ^= value[0]; buf = value.buffer; }
  } else if (mode === 'opfs') {
    // cost of the alternative: OPFS sync handle read into ONE reused buffer (source already in OPFS)
    const root = await navigator.storage.getDirectory(); const fh = await root.getFileHandle('src.bin', { create: true });
    const h = await fh.createSyncAccessHandle(); const u = new Uint8Array(B);
    if (h.getSize() !== file.size) { h.truncate(0); const rd = file.stream().getReader({ mode: 'byob' }); let buf = new ArrayBuffer(B); let at = 0;
      for (;;) { const { value, done } = await rd.read(new Uint8Array(buf)); if (done) break; h.write(value, { at }); at += value.length; buf = value.buffer; } }
    const t2 = Date.now();
    for (let off = 0; off < file.size; off += B) { h.read(u, { at: off }); x ^= u[0]; }
    h.close(); postMessage({ x, ms: Date.now() - t2, copyMs: t2 - t }); return;
  }
  postMessage({ x, ms: Date.now() - t });
};
