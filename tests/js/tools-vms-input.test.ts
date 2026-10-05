/**
 * Hamamatsu VMS 输入侧约定（engine.js + slide-sniff.js）：
 *
 *  - 平铺束包：.vms INI 入口 + 同目录 tile JPEG（无同名子目录，区别于
 *    MRXS 的 <stem>/ 布局）；成员名 = 相对入口目录的平铺名；
 *  - .vms INI 解析只取束包计划需要的键（tile 网格 + 可选 map/opt/macro）；
 *    VMU 组/入口 → 专门的类型化拒绝；NoLayers ≠ 1 拒绝；网格越界拒绝；
 *  - 缺成员 → 一条类型化错误列出全部缺的名字，任何复制之前拒绝；
 *  - 权威的变体终审（DRI、渐进 SOF、列宽/行高一致性）在 wasm 核心的
 *    探测里——页面嗅探只做复制前分流；
 *  - outputFileName 剥掉 .vms；适配器常量与核心一致（v1 / l0-box2 /
 *    mosaic 拼接指纹），转换器产物词表含 hamamatsu-vms-bundle；
 *  - slide-sniff：.vms 从「暂时直传」改为「需要转换」（bundle 标记），
 *    .vmu 维持「暂时直传」。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

// ---- in-memory VMS bundle rows (the folder-picker shape) ----------------- //

function iniText(opts: { vmu?: boolean; noLayers?: number; cols?: number; rows?: number } = {}) {
  const group = opts.vmu
    ? "Uncompressed Virtual Microscope Specimen"
    : "Virtual Microscope Specimen";
  const cols = opts.cols ?? 2;
  const rows = opts.rows ?? 2;
  const lines = [
    `[${group}]`,
    `NoLayers=${opts.noLayers ?? 1}`,
    `NoJpegColumns=${cols}`,
    `NoJpegRows=${rows}`,
  ];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      lines.push(c === 0 && r === 0 ? `ImageFile=s-${c}-${r}.jpg` : `ImageFile(${c},${r})=s-${c}-${r}.jpg`);
    }
  }
  lines.push("OptimisationFile=s.opt", "MacroImage=s_macro.jpg", "SourceLens=40.000000");
  return lines.join("\n") + "\n";
}

const ENTRY = iniText();

function bundleFiles(opts: { drop?: string[]; entry?: string } = {}) {
  const entryName = opts.entry ?? "s.vms";
  const names = [entryName, "s-0-0.jpg", "s-1-0.jpg", "s-0-1.jpg", "s-1-1.jpg", "s.opt", "s_macro.jpg"];
  const rows = [] as { name: string; relPath: string; file: File }[];
  for (const n of names) {
    if (opts.drop?.includes(n)) continue;
    const bytes = n === entryName
      ? new TextEncoder().encode(ENTRY)
      : new TextEncoder().encode(`bytes-of-${n}`);
    rows.push({
      name: n,
      relPath: `CMU-1/${n}`,
      file: new File([bytes], n, { type: "application/octet-stream" }),
    });
  }
  return rows;
}

const readFile = (f: File) => f.slice(0, (1 << 20)).arrayBuffer();

describe("parseVmsMembers（有界 INI 解析）", () => {
  it("枚举 tile 网格与可选成员", () => {
    const ini = E.parseVmsMembers(new TextEncoder().encode(ENTRY));
    expect(ini.cols).toBe(2);
    expect(ini.rows).toBe(2);
    expect(ini.tiles).toEqual(["s-0-0.jpg", "s-1-0.jpg", "s-0-1.jpg", "s-1-1.jpg"]);
    expect(ini.optional).toContain("s.opt");
    expect(ini.optional).toContain("s_macro.jpg");
  });

  it("VMU 组 → 类型化拒绝", () => {
    const bytes = new TextEncoder().encode(iniText({ vmu: true }));
    let caught: any = null;
    try { E.parseVmsMembers(bytes); } catch (e) { caught = e; }
    expect(caught?.error.code).toBe("unsupported_input");
    expect(caught?.error.message).toContain("VMU");
  });

  it("NoLayers ≠ 1 → 拒绝（OpenSlide 同样只接受 1）", () => {
    const bytes = new TextEncoder().encode(iniText({ noLayers: 2 }));
    let caught: any = null;
    try { E.parseVmsMembers(bytes); } catch (e) { caught = e; }
    expect(caught?.error.message).toContain("NoLayers=2");
  });

  it("网格越界 → 拒绝", () => {
    const bytes = new TextEncoder().encode(iniText({ cols: 0 }));
    let caught: any = null;
    try { E.parseVmsMembers(bytes); } catch (e) { caught = e; }
    expect(caught?.error.message).toContain("网格");
  });
});

describe("sniffVmsBundle（任何复制之前）", () => {
  it("识别完整平铺包（入口 + 同目录成员）", () => {
    const s = E.sniffVmsBundle(bundleFiles());
    expect(s.supported).toBe(true);
    if (s.supported) {
      expect(s.stem).toBe("s");
      expect(s.entryName).toBe("s.vms");
      expect(s.files.has("s-1-1.jpg")).toBe(true);
    }
  });

  it(".vmu 入口 → 专门的类型化拒绝", () => {
    const s = E.sniffVmsBundle(bundleFiles({ entry: "s.vmu" }));
    expect(s.supported).toBe(false);
    if (!s.supported) expect(s.reason).toContain("VMU");
  });

  it("只有散装 tile 没有 .vms 入口 → 列出缺的主入口", () => {
    const s = E.sniffVmsBundle(bundleFiles({ drop: ["s.vms"] }));
    expect(s.supported).toBe(false);
    if (!s.supported) expect(s.missing).toEqual(["<slide>.vms"]);
  });

  it("两个 .vms 入口 → 拒绝", () => {
    const rows = [...bundleFiles(), ...bundleFiles({ entry: "t.vms" })];
    const s = E.sniffVmsBundle(rows);
    expect(s.supported).toBe(false);
    if (!s.supported) expect(s.reason).toContain("多个 .vms 主入口");
  });

  it("重复/大小写冲突成员 → 拒绝（平铺存储碰撞）", () => {
    const rows = [...bundleFiles()];
    rows.push(rows[1]);
    expect(E.sniffVmsBundle(rows).supported).toBe(false);
    const rows2 = [...bundleFiles(),
      { name: "S-1-0.jpg", relPath: "CMU-1/S-1-0.jpg", file: rows[1].file }];
    expect(E.sniffVmsBundle(rows2).supported).toBe(false);
  });
});

describe("planVmsBundle / planBundle（完整复制前计划）", () => {
  it("必需成员集恰好是入口 + tile + 可选成员", async () => {
    const plan = await E.planVmsBundle(bundleFiles(), readFile);
    expect(plan.stem).toBe("s");
    expect(plan.adapter).toBe("hamamatsu-vms-bundle");
    expect(plan.required).toEqual([
      "s.vms", "s-0-0.jpg", "s-1-0.jpg", "s-0-1.jpg", "s-1-1.jpg", "s.opt", "s_macro.jpg",
    ]);
    expect(plan.members).toHaveLength(7);
  });

  it("缺 tile → 一条错误列出全部缺的名字，任何复制之前", async () => {
    try {
      await E.planVmsBundle(bundleFiles({ drop: ["s-1-1.jpg", "s-0-1.jpg"] }), readFile);
      expect.unreachable("planVmsBundle should throw");
    } catch (e: any) {
      expect(e.error.code).toBe("unsupported_input");
      expect(e.error.message).toContain("缺少成员");
      expect(e.error.message).toContain("s-1-1.jpg");
      expect(e.error.message).toContain("s-0-1.jpg");
    }
  });

  it("planBundle 按入口类型分派：.vms → VMS 计划；无 .vms → MRXS 计划", async () => {
    const plan = await E.planBundle(bundleFiles(), readFile);
    expect(plan.adapter).toBe("hamamatsu-vms-bundle");
    // 没有 .vms 入口时回落到 MRXS planner（缺主入口的 MRXS 类型化错误）
    let caught: any = null;
    try { await E.planBundle(bundleFiles({ drop: ["s.vms"] }), readFile); } catch (e) { caught = e; }
    expect(caught?.error.message).toContain(".mrxs");
  });

  it(".vmu → planBundle 直接给出专门的类型化拒绝", async () => {
    let caught: any = null;
    try { await E.planBundle(bundleFiles({ entry: "s.vmu" }), readFile); } catch (e) { caught = e; }
    expect(caught?.error.message).toContain("VMU");
  });
});

describe("VMS 命名与常量", () => {
  it("outputFileName 剥掉 .vms 并按 profile 命名", () => {
    const ome = { outputProfile: E.OUTPUT_PROFILES.BF_OME };
    const classic = { outputProfile: E.OUTPUT_PROFILES.BF_CLASSIC };
    expect(E.outputFileName("CMU-1-40x - 2010-01-12 13.24.05.vms", ome)).toBe(
      "CMU-1-40x - 2010-01-12 13.24.05.ome.tif");
    expect(E.outputFileName("CMU-1.vms", classic)).toBe("CMU-1.tif");
  });

  it("适配器常量与核心一致（v1 / l0-box2 / mosaic 拼接指纹）", () => {
    expect(E.VMS_SOURCE_ADAPTER).toBe("hamamatsu-vms-bundle");
    expect(E.VMS_ADAPTER_VERSION).toBe("1");
    expect(E.VMS_PYRAMID_METHOD).toBe("l0-box2");
    expect(E.VMS_PRESERVE_COMPOSE_FINGERPRINT).toBe("vms-mosaic-compose:q96:y422:hstd:v1");
    // 审查回归：VMS 转换器自己的产物必须进「转换器输出」词表——否则其
    // classic BigTIFF 会被当普通输入做第二次有损重编码
    expect(E.CONVERTER_SOURCE_FORMATS).toContain("hamamatsu-vms-bundle");
  });

  it("入口提示表覆盖 .vms/.vmu（bundle kind）", () => {
    expect(E.inputExtensionHint("slide.vms")).toBe("bundle");
    expect(E.inputExtensionHint("slide.vmu")).toBe("bundle");
    expect(E.inputExtensionHint("slide.ndpi")).toBeNull();
  });
});

// ---- slide-sniff：.vms 从「暂时直传」改为「需要转换」 -------------------- //

const here = dirname(fileURLToPath(import.meta.url));
const sniffSrc = readFileSync(resolve(here, "../../static/upload/slide-sniff.js"), "utf8");
const w: { HP_SLIDE_SNIFF?: {
  CLS: Record<string, string>;
  classifyExt: (n: string) => { cls?: string; route?: string; ext?: string; bundle?: boolean };
} } = {};
new Function("window", sniffSrc)(w);
const S = w.HP_SLIDE_SNIFF!;

describe("slide-sniff：VMS 分流", () => {
  it(".vms → convert（bundle 标记，工作台交接整文件夹）", () => {
    const r = S.classifyExt("scan.vms");
    expect(r.cls).toBe(S.CLS.CONVERT);
    expect(r.bundle).toBe(true);
  });

  it(".vmu 维持 temporary（VMU 不在本次转换器范围）", () => {
    const r = S.classifyExt("scan.vmu");
    expect(r.cls).toBe(S.CLS.TEMPORARY);
  });
});
