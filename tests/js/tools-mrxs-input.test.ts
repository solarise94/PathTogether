// F3: MRXS bundle input — manifest normalisation, pre-copy sniffing,
// missing-member refusals, traversal/case-conflict rejection, the JS
// SHA-256 and the manifest root digest (vitest, no DOM).
import { describe, expect, it } from 'vitest';
import * as E from '../../static/tools/slide-transform/engine.js';

const enc = (s: string) => new TextEncoder().encode(s);

function fakeFile(name: string, relPath: string, bytes: Uint8Array | string) {
  let data = typeof bytes === 'string' ? enc(bytes) : bytes;
  const file: any = {
    get size() { return data.length; },
    slice(a: number, b: number) {
      return { arrayBuffer: async () => data.slice(a, b).buffer.slice(0, Math.max(0, Math.min(b, data.length) - a)) };
    },
  };
  return { name, relPath, file };
}

const SLIDEDAT = [
  '[GENERAL]',
  'SLIDE_ID = 0123456789ABCDEF0123456789ABCDEF',
  'SLIDE_TYPE = SLIDE_TYPE_BRIGHTFIELD',
  '[HIERARCHICAL]',
  'INDEXFILE = Index.dat',
  'HIER_COUNT = 1',
  'NONHIER_COUNT = 2',
  'HIER_0_NAME = Slide zoom level',
  'HIER_0_COUNT = 1',
  'NONHIER_0_NAME = Scan data layer',
  'NONHIER_0_COUNT = 1',
  'NONHIER_1_NAME = VIMSLIDE_POSITION_BUFFER',
  'NONHIER_1_COUNT = 1',
  '[DATAFILE]',
  'FILE_COUNT = 2',
  'FILE_0 = Data0000.dat',
  'FILE_1 = Data0001.dat',
].join('\n');

function bundleFiles(opts: { missing?: string[]; extra?: any[] } = {}) {
  const missing = new Set(opts.missing || []);
  const mk = (n: string, rel: string, b: any = 'x') => (!missing.has(n) ? [fakeFile(n, rel, b)] : []);
  return [
    ...mk('CMU.mrxs', 'CMU/CMU.mrxs', 'entry'),
    ...mk('CMU/Slidedat.ini', 'CMU/CMU/Slidedat.ini', SLIDEDAT),
    ...mk('CMU/Index.dat', 'CMU/CMU/Index.dat', 'index'),
    ...mk('CMU/Data0000.dat', 'CMU/CMU/Data0000.dat', 'aaaa'),
    ...mk('CMU/Data0001.dat', 'CMU/CMU/Data0001.dat', 'bbbb'),
    ...(opts.extra || []),
  ];
}

const readFile = async (f: any) => f.file.bytes;

describe('sha256 (engine JS implementation)', () => {
  it('matches the FIPS vectors', () => {
    expect(E.sha256Hex(new Uint8Array(0)))
      .toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
    expect(E.sha256Hex(enc('abc')))
      .toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(E.sha256Hex(enc('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')))
      .toBe('248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1');
  });

  it('is incremental across many blocks', () => {
    const h = new E.Sha256();
    const block = enc('a'.repeat(1000));
    for (let i = 0; i < 64; i++) h.update(block);
    expect(h.digestHex()).toBe(E.sha256Hex(enc('a'.repeat(64000))));
  });
});

describe('bundle path normalisation', () => {
  it('accepts flat and nested member names', () => {
    expect(E.normaliseBundlePath('a/CMU.mrxs')).toBe('a/CMU.mrxs');
    expect(E.normaliseBundlePath('a/CMU/Slidedat.ini')).toBe('a/CMU/Slidedat.ini');
  });
  it('rejects traversal, backslashes, drive letters and empty segments', () => {
    expect(E.normaliseBundlePath('../evil.dat')).toBeNull();
    expect(E.normaliseBundlePath('a/../b.dat')).toBeNull();
    expect(E.normaliseBundlePath('a\\b.dat')).toBeNull();
    expect(E.normaliseBundlePath('C:/x.dat')).toBeNull();
    expect(E.normaliseBundlePath('a//b.dat')).toBeNull();
    expect(E.normaliseBundlePath('')).toBeNull();
  });
});

describe('Slidedat member parsing', () => {
  it('enumerates the referenced data files', () => {
    const sd = E.parseSlidedatMembers(enc(SLIDEDAT));
    expect(sd.fileCount).toBe(2);
    expect(sd.files).toEqual(['Data0000.dat', 'Data0001.dat']);
    expect(sd.indexfile).toBe('Index.dat');
  });
  it('rejects a bogus FILE_COUNT', () => {
    const bad = SLIDEDAT.replace('FILE_COUNT = 2', 'FILE_COUNT = 99999999');
    let caught: any = null;
    try { E.parseSlidedatMembers(enc(bad)); } catch (e) { caught = e; }
    expect(caught).toBeTruthy();
    expect(caught.error.code).toBe('unsupported_input');
    expect(caught.error.message).toContain('FILE_COUNT');
  });
});

describe('sniffMrxBundle (before any copy)', () => {
  it('recognises a complete bundle', () => {
    const s = E.sniffMrxBundle(bundleFiles());
    expect(s.supported).toBe(true);
  });
  it('only a .mrxs entry → typed missing-members', () => {
    const s = E.sniffMrxBundle([fakeFile('CMU.mrxs', 'CMU/CMU.mrxs', 'entry')]);
    expect(s.supported).toBe(false);
    expect(s.reason).toContain('Slidedat.ini');
  });
  it('two .mrxs entries → refused', () => {
    const s = E.sniffMrxBundle([
      fakeFile('a.mrxs', 'a/a.mrxs', 'x'), fakeFile('b.mrxs', 'b/b.mrxs', 'x'),
    ]);
    expect(s.supported).toBe(false);
    expect(s.reason).toContain('多个');
  });
  it('duplicate members → refused', () => {
    const s = E.sniffMrxBundle(bundleFiles({
      extra: [fakeFile('CMU/Data0000.dat', 'CMU/CMU/Data0000.dat', 'dup')],
    }));
    expect(s.supported).toBe(false);
    expect(s.reason).toContain('重复');
  });
  it('case conflicts → refused (flat storage collision)', () => {
    const s = E.sniffMrxBundle(bundleFiles({
      extra: [fakeFile('data0000.dat', 'CMU/CMU/data0000.dat', 'x')],
    }));
    expect(s.supported).toBe(false);
    expect(s.reason).toContain('大小写冲突');
  });
  it('oversize member counts → refused', () => {
    const many = [];
    for (let i = 0; i < E.MRXS_MAX_MEMBERS + 1; i++) many.push(fakeFile(`f${i}.dat`, `CMU/CMU/f${i}.dat`, 'x'));
    const s = E.sniffMrxBundle(many);
    expect(s.supported).toBe(false);
    expect(s.reason).toContain('上限');
  });
});

describe('planMrxBundle (complete pre-copy plan)', () => {
  it('lists exactly the required members', async () => {
    const plan = await E.planMrxBundle(bundleFiles(), readFile);
    expect(plan.stem).toBe('CMU');
    expect(plan.required).toEqual([
      'CMU.mrxs', 'CMU/Slidedat.ini', 'CMU/Index.dat', 'CMU/Data0000.dat', 'CMU/Data0001.dat',
    ]);
  });
  it('a referenced data file missing → typed error listing it, before any copy', async () => {
    await expect(E.planMrxBundle(bundleFiles({ missing: ['CMU/Data0001.dat'] }), readFile))
      .rejects.toMatchObject({ error: { code: 'unsupported_input', kind: 'mrxs-bundle' } });
  });
  it('Slidedat.ini missing → names what a full bundle needs', async () => {
    await expect(E.planMrxBundle(bundleFiles({ missing: ['CMU/Slidedat.ini'] }), readFile))
      .rejects.toMatchObject({ error: { code: 'unsupported_input' } });
  });
  it('traversal file names inside Slidedat → refused', async () => {
    const files = bundleFiles({ missing: ['CMU/Slidedat.ini'] });
    files.push(fakeFile('CMU/Slidedat.ini', 'CMU/CMU/Slidedat.ini',
      SLIDEDAT.replace('FILE_1 = Data0001.dat', 'FILE_1 = ../evil.dat')));
    await expect(E.planMrxBundle(files, readFile))
      .rejects.toMatchObject({ error: { code: 'unsupported_input' } });
  });
  it('extra unrelated files in the folder are ignored (only referenced members copy)', async () => {
    const plan = await E.planMrxBundle(bundleFiles({
      extra: [fakeFile('Thumbs.db', 'CMU/CMU/Thumbs.db', 'junk')],
    }), readFile);
    expect(plan.members.map((m: any) => m.name)).not.toContain('CMU/Thumbs.db');
  });
});

describe('manifest root digest', () => {
  it('changes when any member path, size or digest changes', () => {
    const members = [
      { path: 'a.mrxs', size: 10, sha256: 'aa' },
      { path: 'b/Slidedat.ini', size: 20, sha256: 'bb' },
    ];
    const d0 = E.bundleRootDigest(members);
    expect(E.bundleRootDigest([...members])).toBe(d0);
    expect(E.bundleRootDigest([
      { path: 'a.mrxs', size: 11, sha256: 'aa' },
      { path: 'b/Slidedat.ini', size: 20, sha256: 'bb' },
    ])).not.toBe(d0);
    expect(E.bundleRootDigest([
      { path: 'a.mrxs', size: 10, sha256: 'ac' },
      { path: 'b/Slidedat.ini', size: 20, sha256: 'bb' },
    ])).not.toBe(d0);
    expect(E.bundleRootDigest([
      { path: 'z.mrxs', size: 10, sha256: 'aa' },
      { path: 'b/Slidedat.ini', size: 20, sha256: 'bb' },
    ])).not.toBe(d0);
  });
});

describe('MRXS adapter constants mirror the core', () => {
  it('adapter id/version and the compose fingerprint match the Rust constants', () => {
    expect(E.MRXS_SOURCE_ADAPTER).toBe('mirax-bundle');
    expect(E.MRXS_ADAPTER_VERSION).toBe('1');
    expect(E.MRAX_PRESERVE_COMPOSE_FINGERPRINT).toBe('mirax-preserve-compose:q96:y422:hstd:v1');
  });
});
