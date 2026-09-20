/**
 * 工单 G：首页「更新内容」。
 * 锁定：只渲染 published 条目、按日期新→旧、最新展开/历史折叠、中英条目、
 * 产品版本不是 PathTogether package 0.1.0。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(here, "../../templates/entry.html"), "utf8");
const js = readFileSync(resolve(here, "../../static/entry-releases.js"), "utf8");
const i18n = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
const manifest = JSON.parse(
  readFileSync(resolve(here, "../../static/releases.json"), "utf8"),
) as {
  releases: Array<{
    version: string;
    date: string;
    published: boolean;
    items: { zh: string[]; en: string[] };
  }>;
};
const pkg = JSON.parse(readFileSync(resolve(here, "../../package.json"), "utf8")) as { version: string };

describe("首页更新内容契约", () => {
  it("Hero 之后、#product 之前有 #whats-new，顶栏有锚点", () => {
    expect(html).toMatch(/id="whats-new"/);
    expect(html).toMatch(/href="#whats-new"/);
    const heroAt = html.indexOf('class="hero"');
    const newsAt = html.indexOf('id="whats-new"');
    const productAt = html.indexOf('id="product"');
    expect(heroAt).toBeGreaterThan(-1);
    expect(newsAt).toBeGreaterThan(heroAt);
    expect(productAt).toBeGreaterThan(newsAt);
  });

  it("中英导航与空态文案齐全", () => {
    expect(i18n).toMatch(/"entry.nav.whatsnew": "更新内容"/);
    expect(i18n).toMatch(/"entry.nav.whatsnew": "What's new"/);
    expect(i18n).toMatch(/"entry.whatsnew.title": "更新内容"/);
    expect(i18n).toMatch(/"entry.whatsnew.title": "What's new"/);
  });

  it("产品版本不是 PathTogether 测试包 0.1.0，且按日期排序只取 published", () => {
    expect(pkg.version).toBe("0.1.0");
    for (const r of manifest.releases) {
      expect(r.version).not.toBe(pkg.version);
      expect(r.items.zh.length).toBeGreaterThanOrEqual(3);
      expect(r.items.zh.length).toBeLessThanOrEqual(6);
      expect(r.items.en.length).toBe(r.items.zh.length);
    }
    expect(js).toContain("published === true");
    expect(js).toContain("localeCompare");
    expect(js).toContain("details");
    expect(js).toContain("is-latest");
  });

  it("本批次文案含隔离 / 100 步 / 全片概览", () => {
    const latest = manifest.releases.find((r) => r.version === "2026.09.20");
    expect(latest?.published).toBe(true);
    expect(latest?.items.zh.join(" ")).toMatch(/隔离/);
    expect(latest?.items.en.join(" ")).toMatch(/100 steps/);
    expect(latest?.items.en.join(" ")).toMatch(/whole-slide overview/);
  });
});
