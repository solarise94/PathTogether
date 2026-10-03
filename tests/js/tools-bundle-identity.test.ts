// F3 review §2 fix (bundle identity on resume): the pure helpers that pin
// the expected identity (job record + journal generation, legacy-tolerant)
// and compare it with the identity re-derived from the staged OPFS bytes.
// The manifest's self-reported digests must never be the expectation.
// vitest, no DOM.
import { describe, expect, it } from 'vitest';
import * as E from '../../static/tools/slide-transform/engine.js';

const sha = (s: string) => E.sha256Hex(new TextEncoder().encode(s));

function memberBundle() {
  // canonical order as at prepare: entry, Slidedat, Index, FILE_i …
  const members = [
    { path: 'synthetic.mrxs', size: 5, sha256: sha('entry') },
    { path: 'synthetic/Slidedat.ini', size: 12, sha256: sha('slidedat') },
    { path: 'synthetic/Index.dat', size: 5, sha256: sha('index') },
    { path: 'synthetic/Data0000.dat', size: 4, sha256: sha('pix0') },
    { path: 'synthetic/Data0001.dat', size: 4, sha256: sha('pix1') },
  ];
  const manifest = {
    v: 1,
    adapter: E.MRXS_SOURCE_ADAPTER,
    adapterVersion: E.MRXS_ADAPTER_VERSION,
    entry: 'synthetic.mrxs',
    stem: 'synthetic',
    members,
    memberCount: members.length,
    totalBytes: members.reduce((a, m) => a + m.size, 0),
    rootDigest: E.bundleRootDigest(members),
    createdAt: '2026-10-03T00:00:00.000Z',
  };
  return { members, manifest };
}

function bundleRecord(manifest: any, overrides: any = {}) {
  return {
    bundle: true,
    identity: {
      name: manifest.entry,
      folderName: null,
      size: manifest.totalBytes,
      lastModified: null,
      sha256: manifest.rootDigest,
    },
    bundleManifest: manifest,
    ...overrides,
  };
}

// a re-derived (actual) identity over the same members
function actualOf(members: any[]) {
  return {
    members,
    memberCount: members.length,
    totalBytes: members.reduce((a: any, m: any) => a + m.size, 0),
    rootDigest: E.bundleRootDigest(members),
  };
}

describe('recordBundleIdentity (pinned expectation from the job record)', () => {
  it('returns null for non-bundle records', () => {
    expect(E.recordBundleIdentity(null)).toBeNull();
    expect(E.recordBundleIdentity({ bundle: false, identity: { sha256: 'x', size: 1 } })).toBeNull();
  });

  it('errors when a bundle record cannot pin a source', () => {
    for (const rec of [
      { bundle: true },
      { bundle: true, identity: { size: 10 } },
      { bundle: true, identity: { sha256: 'abc' } },
      { bundle: true, identity: { sha256: 'abc', size: -1 } },
    ]) {
      const r = E.recordBundleIdentity(rec as any);
      expect(r && r.error, JSON.stringify(rec)).toBeTruthy();
    }
  });

  it('exposes root digest + total size + canonical member list, never member digests as the pin', () => {
    const { manifest } = memberBundle();
    const r = E.recordBundleIdentity(bundleRecord(manifest) as any) as any;
    expect(r.error).toBeUndefined();
    expect(r.sha256).toBe(manifest.rootDigest);
    expect(r.size).toBe(manifest.totalBytes);
    expect(r.memberCount).toBe(5);
    expect(r.memberPaths).toEqual(manifest.members.map((m) => m.path));
    expect(r.memberSizes).toEqual(manifest.members.map((m) => m.size));
    expect(r.manifestShell).toEqual({
      v: 1, adapter: 'mirax-bundle', adapterVersion: E.MRXS_ADAPTER_VERSION,
      entry: 'synthetic.mrxs', stem: 'synthetic', createdAt: manifest.createdAt,
    });
  });

  it('is legacy-tolerant about the saved member list (paths optional)', () => {
    const { manifest } = memberBundle();
    const rec = bundleRecord(manifest);
    delete (rec as any).bundleManifest;
    const r = E.recordBundleIdentity(rec as any) as any;
    expect(r.error).toBeUndefined();
    expect(r.sha256).toBe(manifest.rootDigest);
    expect(r.memberPaths).toBeNull();
    expect(r.memberCount).toBeNull();
  });
});

describe('expectedBundleIdentity (record + journal generation merge)', () => {
  it('accepts a journal generation that agrees with the record', () => {
    const { manifest } = memberBundle();
    const r = E.expectedBundleIdentity(bundleRecord(manifest) as any,
      { sha256: manifest.rootDigest, size: manifest.totalBytes }) as any;
    expect(r.error).toBeUndefined();
    expect(r.expected.sha256).toBe(manifest.rootDigest);
  });

  it('accepts legacy journals without an identity (the record alone decides)', () => {
    const { manifest } = memberBundle();
    for (const j of [null, undefined, {}, { sha256: null, size: null }]) {
      const r = E.expectedBundleIdentity(bundleRecord(manifest) as any, j as any) as any;
      expect(r.error).toBeUndefined();
      expect(r.expected.sha256).toBe(manifest.rootDigest);
    }
  });

  it('refuses a journal generation that contradicts the record', () => {
    const { manifest } = memberBundle();
    const r = E.expectedBundleIdentity(bundleRecord(manifest) as any,
      { sha256: sha('other'), size: manifest.totalBytes }) as any;
    expect(r.error).toContain('进度记录的源身份');
    const r2 = E.expectedBundleIdentity(bundleRecord(manifest) as any,
      { sha256: manifest.rootDigest, size: manifest.totalBytes + 1 }) as any;
    expect(r2.error).toContain('进度记录的源身份');
  });
});

describe('compareBundleIdentity (re-derived vs pinned)', () => {
  const { manifest, members } = memberBundle();
  const expected = E.recordBundleIdentity(bundleRecord(manifest) as any) as any;

  it('accepts the unchanged bundle', () => {
    expect(E.compareBundleIdentity(expected, actualOf(members))).toBeNull();
  });

  it('treats a different member order as a mismatch (order is canonical)', () => {
    const swapped = [...members.slice(1), members[0]];
    expect(E.compareBundleIdentity(expected, actualOf(swapped))).toContain('不在任务记录的成员表');
  });

  it('refuses a same-size byte change (root digest mismatch)', () => {
    // the reviewer's original case: OBJECTIVE_MAGNIFICATION 20 → 40 keeps
    // the length but flips bytes; a self-consistent manifest must not help
    const changed = members.map((m) => m.path.endsWith('Slidedat.ini')
      ? { ...m, sha256: sha('slidedat-v2') } : m);
    // the per-member message comes first (the saved manifest pins digests)…
    expect(E.compareBundleIdentity(expected, actualOf(changed)))
      .toContain('Slidedat.ini 摘要与任务记录不符');
    // …and a legacy record without member digests still catches the byte
    // change at the root digest
    const legacy = { ...expected, memberPaths: null, memberSizes: null, memberSha256: null };
    expect(E.compareBundleIdentity(legacy, actualOf(changed)))
      .toContain('包摘要与任务记录不符');
  });

  it('refuses a replaced pixel member', () => {
    const changed = members.map((m) => m.path.endsWith('Data0000.dat')
      ? { ...m, sha256: sha('evil pixels') } : m);
    expect(E.compareBundleIdentity(expected, actualOf(changed)))
      .toContain('Data0000.dat 摘要与任务记录不符');
  });

  it('refuses a size change and a total-length change', () => {
    const grown = members.map((m) => ({ ...m,
      size: m.path.endsWith('Data0001.dat') ? m.size + 7 : m.size }));
    expect(E.compareBundleIdentity(expected, actualOf(grown)))
      .toContain('Data0001.dat 大小 11 ≠ 记录 4');
    // legacy expectation without member sizes still catches the total
    const legacy = { ...expected, memberPaths: null, memberSizes: null, memberSha256: null };
    expect(E.compareBundleIdentity(legacy, actualOf(grown))).toContain('包总长度');
  });

  it('refuses an added and a removed member', () => {
    const added = [...members, { path: 'synthetic/Extra.dat', size: 9, sha256: sha('extra') }];
    expect(E.compareBundleIdentity(expected, actualOf(added))).toContain('包成员数量 6 ≠ 任务记录 5');
    expect(E.compareBundleIdentity(expected, actualOf(members.slice(1))))
      .toContain('包成员数量 4 ≠ 任务记录 5');
  });

  it('refuses a member count mismatch even when the legacy record has no member list', () => {
    const legacy = { ...expected, memberPaths: null, memberSizes: null,
      memberSha256: null, memberCount: 5 };
    expect(E.compareBundleIdentity(legacy, actualOf(members.slice(0, 4))))
      .toContain('包成员数量 4 ≠ 任务记录 5');
  });
});

describe('bundleRootDigest (the pinned identity itself)', () => {
  it('flips on any path, size or byte change and on reordering', () => {
    const { members } = memberBundle();
    const base = E.bundleRootDigest(members);
    expect(E.bundleRootDigest(members)).toBe(base);
    expect(E.bundleRootDigest(members.map((m) => ({ ...m, sha256: sha('x' + m.path) }))))
      .not.toBe(base);
    expect(E.bundleRootDigest(members.map((m) => ({ ...m, size: m.size + 1 }))))
      .not.toBe(base);
    expect(E.bundleRootDigest([...members].reverse())).not.toBe(base);
    expect(E.bundleRootDigest([{ path: 'other.mrxs', size: 5, sha256: sha('entry') },
      ...members.slice(1)])).not.toBe(base);
  });
});
