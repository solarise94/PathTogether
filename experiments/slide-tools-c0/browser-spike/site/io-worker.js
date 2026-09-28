// IO worker: owns the input File (structured-cloned in, bytes never copied
// to the main thread), serves bounded random reads on request and TRANSFERS
// each ArrayBuffer to the compute worker (zero-copy ownership handoff).
let file = null;

self.onmessage = (e) => {
  const m = e.data;
  if (m.type === 'init') {
    file = m.file;
    const port = m.port;
    port.onmessage = async (ev) => {
      const r = ev.data;
      if (r.type !== 'read') return;
      try {
        // File.slice is metadata-only; the actual bounded disk read happens
        // in arrayBuffer(). Offsets are safe integers (asserted by sender).
        const buf = await file.slice(r.offset, r.offset + r.length).arrayBuffer();
        port.postMessage(
          { type: 'chunk', id: r.id, offset: r.offset, buffer: buf },
          [buf] // transferable: sender's view is neutered, no copy
        );
      } catch (err) {
        port.postMessage({ type: 'chunk-error', id: r.id, error: String(err) });
      }
    };
    port.postMessage({ type: 'ready' });
  }
};
