// Review §1: MRXS probe/convert must run under the browser resource
// profile's budget — the worker passes `profile.budgetBytes` into the wasm
// bundle entry points and the runner supplies the profile id to the
// probe-bundle request; the wasm surface accepts the budget argument.
// (Source-wiring contract, same style as tools-output-profile.test.ts —
// the negative VALUE behaviour is covered by the Rust unit test and the
// native CLI cgroup script `scripts/test_mrxs_memory_budget.sh`.)
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import * as E from '../../static/tools/slide-transform/engine.js';

const here = new URL('.', import.meta.url).pathname;
const src = (rel: string) => readFileSync(resolve(here, '../..', rel), 'utf8');

describe('mrxs memory budget wiring (review §1)', () => {
  it('saver profile carries the 192 MiB budget the adapter is held to', () => {
    expect(E.PROFILES.saver.budgetBytes).toBe(192 * 2 ** 20);
    expect(E.PROFILES.balanced.budgetBytes).toBe(384 * 2 ** 20);
    expect(E.PROFILES.faster.budgetBytes).toBe(768 * 2 ** 20);
  });

  it('worker forwards budgetBytes into probeBundle and the bundle converts', () => {
    const worker = src('static/tools/slide-transform/worker.js');
    expect(worker).toMatch(/probeBundle\(\s*pf\.budgetBytes\s*\)/);
    expect(worker).toContain('channelJson, profile.budgetBytes)');
    // the probe-bundle request resolves a profile even when the caller
    // omits one (saver fallback) before probing
    expect(worker).toMatch(/getProfile\(m\.profileId \|\| 'saver'\)/);
  });

  it('runner sends the resource profile id with every probe-bundle request', () => {
    const runner = src('static/tools/slide-transform/runner.js');
    const sites = [...runner.matchAll(/_request\(/g)]
      .filter((m) => runner.slice(m.index ?? 0, (m.index ?? 0) + 120).includes("'probe-bundle'"));
    expect(sites.length).toBe(2); // prepareBundle + startOrResume
    for (const st of sites) {
      const seg = runner.slice(st.index ?? 0, (st.index ?? 0) + 400);
      expect(seg).toContain('profileId');
    }
  });

  it('generated wasm surface accepts the budget argument', () => {
    const dts = src('static/tools/slide-transform/slide_transform.d.ts');
    expect(dts).toMatch(/probeBundle\(budget_bytes\?: number \| null\s*\)/);
    expect(dts).toMatch(
      /convertProfileEncodedBundle\(\s*profile: string,\s*encoding: string,\s*strict_lossless: boolean,\s*channel_json: string,\s*budget_bytes\?: number \| null\s*\)/,
    );
    expect(dts).toMatch(/budget_bytes\?: number \| null[^)]*\)\s*:\s*string/);
  });
});
