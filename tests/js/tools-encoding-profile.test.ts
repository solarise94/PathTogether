/**
 * 明场「更小文件（有损）」编码档位（compact-jpeg-v1，U3）的引擎/runner 侧约定：
 *
 *  - 画质独立于输出格式（bf-ome / bf-classic × preserve / compact 四种组合）；
 *    默认 preserve-source-v1；没有 encodingProfile 字段的旧任务记录一律按
 *    preserve 对待——部分写出的 preserve 产物绝不会被当成 compact 续写；
 *  - compact 仅适用于明场（荧光拒绝 unsupported_input/encoding-profile）；
 *  - setPreparedEncodingProfile 只在 prepared 状态可改（之后 resume_refused/
 *    encoding-profile），且必须与模态匹配；
 *  - 指纹常量必须与 Rust 核心的 COMPACT_JPEG_V1_FINGERPRINT 一致（改参数必须
 *    换指纹版本，否则旧半成品会被新参数续跑）；
 *  - 磁盘预估按编码档位选择上限（compact 用 compact_upper_bound_bytes）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

const here = dirname(fileURLToPath(import.meta.url));
const runnerSrc = readFileSync(
  resolve(here, "../../static/tools/slide-transform/runner.js"), "utf8");
const workerSrc = readFileSync(
  resolve(here, "../../static/tools/slide-transform/worker.js"), "utf8");
const planSrc = readFileSync(
  resolve(here, "../../slide-transform-core/crates/core/src/plan.rs"), "utf8");

describe("encoding profile helpers (engine.js)", () => {
  it("default is preserve for every modality", () => {
    expect(E.defaultEncodingProfile("brightfield")).toBe("preserve-source-v1");
    expect(E.defaultEncodingProfile("fluorescence")).toBe("preserve-source-v1");
    expect(E.defaultEncodingProfile(undefined)).toBe("preserve-source-v1");
  });

  it("records without encodingProfile predate U3 → preserve", () => {
    expect(E.recordEncodingProfile({ modality: "brightfield", state: "paused" }))
      .toBe("preserve-source-v1");
    expect(E.recordEncodingProfile({ encodingProfile: "compact-jpeg-v1" }))
      .toBe("compact-jpeg-v1");
    expect(E.recordEncodingProfile(null)).toBe("preserve-source-v1");
  });

  it("compact fits brightfield only", () => {
    expect(E.encodingFitsModality("compact-jpeg-v1", "brightfield")).toBe(true);
    expect(E.encodingFitsModality("compact-jpeg-v1", "fluorescence")).toBe(false);
    expect(E.encodingFitsModality("preserve-source-v1", "fluorescence")).toBe(true);
    expect(E.encodingFitsModality("preserve-source-v1", "brightfield")).toBe(true);
    expect(E.encodingFitsModality("compact", "brightfield")).toBe(false);
  });

  it("finished jobs: the core-reported encoding wins, then the record", () => {
    expect(E.jobEncodingProfile({ result: { encoding: "compact-jpeg-v1" } }))
      .toBe("compact-jpeg-v1");
    expect(E.jobEncodingProfile({ encodingProfile: "compact-jpeg-v1" }))
      .toBe("compact-jpeg-v1");
    expect(E.jobEncodingProfile({ modality: "brightfield" }))
      .toBe("preserve-source-v1");
  });

  it("the JS fingerprint mirror matches the Rust core constant", () => {
    const rust = planSrc.match(/COMPACT_JPEG_V1_FINGERPRINT: &str = "([^"]+)"/);
    expect(rust).toBeTruthy();
    expect(E.COMPACT_JPEG_V1_FINGERPRINT).toBe(rust![1]);
    // the fingerprint names the locked quality and sampling (see plan.rs)
    const q = planSrc.match(/COMPACT_JPEG_V1_QUALITY: u8 = (\d+)/);
    const sampling = planSrc.match(/COMPACT_JPEG_V1_SAMPLING: crate::jpeg::Sampling =\s*\n?\s*crate::jpeg::Sampling::S(\d+)/);
    expect(E.COMPACT_JPEG_V1_FINGERPRINT).toContain(`q${q![1]}`);
    expect(E.COMPACT_JPEG_V1_FINGERPRINT).toContain(sampling![1]);
  });

  it("disk precheck picks the compact upper bound for compact jobs", () => {
    const est = {
      output_upper_bound_bytes: 1000,
      compact_upper_bound_bytes: 1200,
      cells_total: 10,
      tiles_present: 10,
    };
    const preserve = E.diskNeedBytes(est, {});
    const compact = E.diskNeedBytes(est, { encoding: "compact-jpeg-v1" });
    expect(compact.output).toBe(1200);
    expect(preserve.output).toBe(1000);
    expect(compact.total).toBeGreaterThan(preserve.total);
    // legacy estimates without the compact field fall back to the preserve bound
    const legacy = E.diskNeedBytes({ output_upper_bound_bytes: 900, cells_total: 0, tiles_present: 0 },
      { encoding: "compact-jpeg-v1" });
    expect(legacy.output).toBe(900);
  });
});

describe("runner encoding persistence (source-level)", () => {
  it("setPreparedEncodingProfile: prepared-only and modality-checked", () => {
    expect(runnerSrc).toContain("async setPreparedEncodingProfile(jobId, encoding)");
    expect(runnerSrc)
      .toMatch(/setPreparedEncodingProfile\(jobId, encoding\)[\s\S]*?rec\.state !== 'prepared'/);
    expect(runnerSrc)
      .toMatch(/setPreparedEncodingProfile\(jobId, encoding\)[\s\S]*?checkedEncoding\(encoding, rec\.modality\)/);
    // the refusal is typed with kind encoding-profile
    expect(runnerSrc).toMatch(/'任务已开始，不能再更改画质',\s*\n\s*\{ kind: 'encoding-profile' \}/);
  });

  it("resumes refuse an encoding change and disagreeing journals", () => {
    // requested-vs-record refusal
    expect(runnerSrc).toMatch(/画质已改变：任务 \$\{committedEncoding\}，请求 \$\{opts\.encodingProfile\}/);
    // journal-generation-vs-record refusal (missing field = preserve)
    expect(runnerSrc).toContain("st.gen.encodingProfile || E.ENCODING_PROFILES.PRESERVE");
    expect(runnerSrc).toMatch(/进度记录的画质（\$\{journalledEnc\}）与任务记录不符/);
    // both refusals carry kind encoding-profile
    expect(runnerSrc.match(/kind: 'encoding-profile'/g)?.length)
      .toBeGreaterThanOrEqual(3);
  });

  it("prepare persists the encoding and gates the disk check on it", () => {
    expect(runnerSrc).toMatch(/encodingProfile: preparedEncoding/);
    expect(runnerSrc).toMatch(/checkedEncoding\(\s*opts\.encodingProfile \|\| E\.defaultEncodingProfile\(\), doc0\.modality\)/);
    expect(runnerSrc).toMatch(/\{ encoding: encodingProfile \}/);
  });

  it("start/resume ladder: resumes keep the recorded encoding; fresh take explicit or prepared", () => {
    expect(runnerSrc).toMatch(/const encodingProfile = checkedEncoding\(resumeJobId\s*\?\s*E\.recordEncodingProfile\(record\)\s*:\s*\(opts\.encodingProfile \|\| record\.encodingProfile \|\| E\.defaultEncodingProfile\(\)\),\s*modality\)/);
    expect(runnerSrc).toContain("encodingProfile,");
    expect(runnerSrc).toContain("encoding: encodingProfile,");
  });

  it("job summaries expose the encoding", () => {
    expect(runnerSrc).toMatch(/encodingProfile: rec && !\['staging', 'prepared'\]\.includes\(state\)\s*\?\s*E\.recordEncodingProfile\(rec\) : \(rec && rec\.encodingProfile\) \|\| null/);
  });

  it("worker journals the encoding and calls the encoded wasm exports", () => {
    expect(workerSrc).toContain("encodingProfile: opts.encoding || 'preserve-source-v1',");
    expect(workerSrc).toContain("convertProfileEncoded(outputProfile, encoding, strict, channelJson)");
    expect(workerSrc).toContain("convertResumeProfileEncoded(JSON.stringify(resume.st), outputProfile, encoding, strict, channelJson)");
    expect(workerSrc).not.toMatch(/convertProfile\((?!Encoded)/);
  });
});

// --------------------------------------------------------------------------- //
// merged U3 × F1: three INDEPENDENT per-job fields (output profile, encoding
// profile, source adapter) — combination rules and legacy-record meanings.
// --------------------------------------------------------------------------- //

describe("three-field combination rules (merged U3×F1)", () => {
  it("a legacy record with NONE of the fields keeps every legacy meaning", () => {
    const legacy = { modality: "brightfield", state: "paused" };
    // layout: classic brightfield (pre-profile default), never reinterpreted
    expect(E.recordOutputProfile(legacy)).toBe("bf-classic");
    // payload: preserve (pre-U3 default), never reinterpreted as compact
    expect(E.recordEncodingProfile(legacy)).toBe("preserve-source-v1");
    // adapter: absent for KFB records (null), never claimed to be SVS
    expect((legacy as Record<string, unknown>).sourceAdapter ?? null).toBeNull();
    const legacyFl = { modality: "fluorescence", state: "paused" };
    expect(E.recordOutputProfile(legacyFl)).toBe("fl-ome");
    expect(E.recordEncodingProfile(legacyFl)).toBe("preserve-source-v1");
  });

  it("SVS × compact is a legal combination; fluorescence × compact stays refused", () => {
    // the SVS adapter is brightfield, so the lossy mode applies to it
    expect(E.encodingFitsModality("compact-jpeg-v1", "brightfield")).toBe(true);
    expect(E.encodingFitsModality("compact-jpeg-v1", "fluorescence")).toBe(false);
    // and compact pairs with BOTH brightfield output layouts
    expect(E.profileFitsModality("bf-ome", "brightfield")).toBe(true);
    expect(E.profileFitsModality("bf-classic", "brightfield")).toBe(true);
  });

  it("each field is refused independently with its own kind", () => {
    // output layout change → output-profile
    expect(runnerSrc).toMatch(/画质已改变|输出格式已改变/);
    expect(runnerSrc.match(/kind: 'output-profile'/g)?.length).toBeGreaterThanOrEqual(2);
    // encoding change → encoding-profile
    expect(runnerSrc.match(/kind: 'encoding-profile'/g)?.length).toBeGreaterThanOrEqual(3);
    // adapter change → source-adapter (journal-vs-record AND staged-copy-vs-record)
    expect(runnerSrc).toContain("const journalledAdapter = st.gen.sourceAdapter || null;");
    expect(runnerSrc.match(/kind: 'source-adapter'/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("the SVS adapter id matches the core constant and rides on every record", () => {
    expect(E.SVS_SOURCE_ADAPTER).toBe("aperio-svs-jpeg");
    // worker journals all three fields in one generation record
    expect(workerSrc).toContain("outputProfile: opts.outputProfile,");
    expect(workerSrc).toContain("sourceAdapter: opts.sourceAdapter || null,");
    expect(workerSrc).toContain("encodingProfile: opts.encoding || 'preserve-source-v1',");
    // the resume refusal ladder reads the staged copy's true adapter
    expect(runnerSrc).toContain("const sourceAdapter = doc.adapter || record.sourceAdapter || null;");
  });

  it("core plan.rs keeps the three fields independent", () => {
    // the encoding profile is documented as INDEPENDENT of the output layout
    expect(planSrc).toContain("Tile-payload encoding strategy — INDEPENDENT of the output profile");
    // the SVS converter refuses compact + strict-lossless with the same
    // typed message as the KFB path (before any output byte)
    expect(planSrc).toContain("pub fn compact_jpeg_v1_encoder_cfg()");
  });
});
