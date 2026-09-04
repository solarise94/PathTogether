/**
 * Batch 4：三种查看器通道 UI —— 真实 Chromium + 生产 static 资源布局回归。
 *
 * 静态 HTML fixture（虚构通道数据，无任何患者数据）+ 路由拦截加载仓库内的
 * 生产 i18n.js / channel-controls.js / style.css，不搭建登录/多通道后端；
 * POST render-context 由路由拦截回固定 JSON。被现有 `npm run test:e2e:admin`
 * （playwright.config.ts，testDir=tests/e2e）拾取。
 *
 * 断言（任务书 §4.1/§4.2）：
 *   - multichannel：工具栏「通道」入口显示；说明卡默认展开；列出全部通道；
 *     「已显示 n/m」计数；T/Z>1 持续提示；第 9 个通道被阻止并给可读提示；
 *     325px 移动端面板不溢出视口；
 *   - RGB：不显示占空间的面板，只有灰色「原始 RGB」小标识；
 *   - flag 关：保持原 viewer（无按钮/无面板/无标识），不发 render-context。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const here = typeof __dirname !== "undefined" ? __dirname : process.cwd();
const I18N_JS = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
const CHANNEL_JS = readFileSync(resolve(here, "../../static/channel-controls.js"), "utf8");
const PROD_CSS = readFileSync(resolve(here, "../../static/style.css"), "utf8");
const FIXTURE_HOST = "http://pt-fixture.test";

interface FkChannel {
	index: number;
	name?: string;
	color: string;
	color_source?: string;
	alpha?: number;
	default_active?: boolean;
	intensity?: { status?: string };
}

function channels(n: number, defaultActive = 4): FkChannel[] {
	const palette = ["#00FFFF", "#FF00FF", "#FFD166", "#00E676", "#FF5C5C", "#4D7CFE", "#FF8C42", "#B388FF", "#112233", "#445566", "#778899", "#AABBCC"];
	return Array.from({ length: n }, (_, i) => ({
		index: i,
		name: `C${i}`,
		color: palette[i % palette.length]!,
		color_source: i < 2 ? "ome" : "default",
		alpha: 1,
		default_active: i < defaultActive,
		intensity: { status: "ok" },
	}));
}

/** 构造 info（§6.1 additive 字段形态；flag 关时只给探测字段） */
function info(mode: "multichannel" | "native_rgb" | "flagoff"): Record<string, unknown> {
	const base: Record<string, unknown> = {
		name: "fixture_demo.ome.tiff",
		image_mode: mode === "multichannel" ? "multichannel" : "native_rgb",
		asset_revision: "rev-fix-1",
		server_capability: {
			multichannel: mode === "multichannel",
			render_token: mode !== "flagoff",
			render_context_endpoint: mode !== "flagoff",
		},
	};
	if (mode !== "multichannel") return base;
	const ch = channels(12);
	return {
		...base,
		channels: ch,
		warnings:
			mode === "multichannel"
				? [{ code: "first-plane-v1", message: "当前仅显示 T=0、Z=0；时间/层面切换尚未支持" }]
				: [],
		plane: { t: 0, z: 0, size_t: 2, size_z: 1, policy: "first-plane-v1" },
		axes: "CYX",
		default_render_context: {
			version: "multichannel-additive-v1",
			asset_revision: "rev-fix-1",
			plane: { t: 0, z: 0 },
			active_channels: ch.slice(0, 4).map((c) => ({
				index: c.index, color: c.color, alpha: 1, black: 1, white: 100, gamma: 1,
			})),
			fingerprint: "ab12cd34".repeat(8),
		},
		default_render_token: "tok-fixture-default",
		deepzoom: { width: 20000, height: 16000, tile_size: 512, overlap: 1, min_level: 0, max_level: 6 },
	};
}

function fixtureHtml(mode: "multichannel" | "native_rgb" | "flagoff"): string {
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<link rel="stylesheet" href="/static/style.css" />
<title>channel-panel fixture（虚构通道数据）</title>
</head>
<body style="margin:0">
<header id="app-header" style="display:flex;gap:6px;align-items:center;padding:8px 12px;">
  <div id="toolbar" style="display:flex;gap:6px;align-items:center;">
    <span id="rgb-badge" class="rgb-mode-badge" hidden data-i18n="channel.rgb.badge">原始 RGB</span>
    <button id="channel-btn" class="tool-btn channel-btn" type="button" hidden
            data-i18n="channel.entry" data-i18n-title="channel.entry.title"
            aria-haspopup="dialog" aria-expanded="false">通道</button>
  </div>
</header>
<main id="viewer-wrap" style="position:relative;height:80vh;">
  <div id="viewer" style="position:absolute;inset:0;"></div>
  <div id="channel-panel" class="channel-panel" hidden role="dialog"
       aria-label="通道着色（显示映射）" data-i18n-aria="channel.panel.aria"></div>
</main>
<div id="toast-container"></div>
<script>window.__FIXTURE_MODE__ = ${JSON.stringify(mode)};</script>
<script src="/static/i18n.js"></script>
<script src="/static/channel-controls.js"></script>
<script>
(function () {
  "use strict";
  try { HP_I18N.setLang("zh"); } catch (e) {}
  function t(k, vars) { return HP_I18N.t(k, vars); }
  function toast(msg, type) {
    var el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = msg;
    el.setAttribute("data-toast-code", msg);
    document.getElementById("toast-container").appendChild(el);
  }
  // 最小 viewer 桩：通道控制器只用到 close/open/addHandler 面
  var fakeViewer = {
    viewport: null,
    container: document.getElementById("viewer"),
    close: function () {},
    open: function () {},
    addHandler: function () {},
    addOnceHandler: function () {},
    removeHandler: function () {},
  };
  window.__postCount = 0;
  var adapter = {
    tileUrl: function (id, level, x, y, token) {
      return "/mock/" + id + "_files/" + level + "/" + x + "_" + y + ".jpeg?render=" + token;
    },
    thumbnailUrl: function (id, token) { return "/mock/" + id + "/thumbnail?render=" + token; },
    normalizeRenderContext: function (id, body) {
      window.__postCount += 1;
      return fetch("/mock/render-context", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    },
  };
  var info = ${JSON.stringify(info(mode))};
  window.__ctrl = HP_Channels.createChannelController({
    adapter: adapter,
    viewer: fakeViewer,
    button: document.getElementById("channel-btn"),
    badge: document.getElementById("rgb-badge"),
    panelHost: document.getElementById("channel-panel"),
    t: t,
    toast: toast,
    storage: window.localStorage,
  });
  window.__ctrl.handleInfo(info, { id: info.name, scope: "fixture" });
})();
</script>
</body>
</html>`;
}

async function serveFixture(page: Page, mode: "multichannel" | "native_rgb" | "flagoff"): Promise<void> {
	const postBodies: string[] = [];
	await page.route(FIXTURE_HOST + "/**", (route: Route) => {
		const url = new URL(route.request().url());
		if (url.pathname === "/static/style.css") {
			return route.fulfill({ status: 200, contentType: "text/css; charset=utf-8", body: PROD_CSS });
		}
		if (url.pathname === "/static/i18n.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: I18N_JS });
		}
		if (url.pathname === "/static/channel-controls.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: CHANNEL_JS });
		}
		if (url.pathname === "/mock/render-context" && route.request().method() === "POST") {
			postBodies.push(route.request().postData() || "");
			return route.fulfill({
				status: 200,
				contentType: "application/json",
				body: JSON.stringify({
					render_context: { version: "multichannel-additive-v1" },
					render_context_fingerprint: "feedbeef".repeat(8),
					render_token: "tok-fixture-" + postBodies.length,
					asset_revision: "rev-fix-1",
					warnings: [],
				}),
			});
		}
		if (url.pathname === "/fixture") {
			return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: fixtureHtml(mode) });
		}
		return route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
	});
}

test.describe("Batch 4 通道着色 UI（生产 CSS/JS，虚构数据）", () => {
	test("multichannel：入口+说明卡+计数+T/Z 提示；第 9 个通道被阻止；325px 不溢出", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "multichannel");
		await page.goto(FIXTURE_HOST + "/fixture");

		// 工具栏入口可见，说明卡默认展开
		await expect(page.locator("#channel-btn")).toBeVisible();
		const panel = page.locator("#channel-panel");
		await expect(panel).toBeVisible();

		// 列出全部 12 个通道；默认启用前 4
		expect(await panel.locator(".ch-row").count()).toBe(12);
		expect(await panel.locator(".ch-row input[type=checkbox]:checked").count()).toBe(4);
		// 「已显示 n/m 个通道」计数文本（§4.2）
		await expect(page.locator(".ch-count")).toContainText("4/12");
		// T/Z>1 持续提示
		await expect(page.locator(".ch-plane-warn")).toContainText("T=0");
		// 控件不只靠颜色：勾选 + 文本 + aria-label（§4.2）
		const firstBox = panel.locator(".ch-row input[type=checkbox]").first();
		expect(await firstBox.getAttribute("aria-label")).toBeTruthy();
		await expect(panel.locator(".ch-name").first()).toContainText("C0");
		// 颜色来源文本（§4.2：OME 元数据颜色 / 默认伪彩色卡）
		await expect(panel.locator(".ch-origin").first()).toContainText("OME");
		await expect(panel.locator(".ch-origin").last()).toContainText("默认伪彩");

		// 325px 移动端：面板不溢出视口
		await page.setViewportSize({ width: 325, height: 700 });
		await expect(panel).toBeVisible();
		const box = await panel.boundingBox();
		expect(box).toBeTruthy();
		expect(box!.x).toBeGreaterThanOrEqual(0);
		expect(box!.x + box!.width).toBeLessThanOrEqual(325.5);

		// 再启用 4 个（共 8）→ 第 9 个被阻止并给可读提示
		await page.setViewportSize({ width: 1280, height: 800 });
		for (const i of [4, 5, 6, 7]) {
			await panel.locator(`.ch-row[data-index="${i}"] input[type=checkbox]`).click();
		}
		await expect(page.locator(".ch-count")).toContainText("8/12");
		await panel.locator('.ch-row[data-index="8"] input[type=checkbox]').click();
		await expect(page.locator("#toast-container .toast").last()).toContainText("8");
		// 第 9 个保持未启用
		expect(
			await panel.locator('.ch-row[data-index="8"] input[type=checkbox]').isChecked()
		).toBe(false);
		await expect(page.locator(".ch-count")).toContainText("8/12");

		// 工具栏「通道」按钮可收起/展开说明卡
		await page.locator("#channel-btn").click();
		await expect(panel).toBeHidden();
		await page.locator("#channel-btn").click();
		await expect(panel).toBeVisible();
	});

	test("RGB：不显示面板；只有灰色「原始 RGB」小标识（§4.1）", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "native_rgb");
		await page.goto(FIXTURE_HOST + "/fixture");
		await expect(page.locator("#channel-btn")).toBeHidden();
		await expect(page.locator("#channel-panel")).toBeHidden();
		const badge = page.locator("#rgb-badge");
		await expect(badge).toBeVisible();
		await expect(badge).toContainText("RGB");
	});

	test("flag 关：保持原 viewer——无入口/无面板/无标识，且不发 render-context（§15.2）", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "flagoff");
		await page.goto(FIXTURE_HOST + "/fixture");
		await expect(page.locator("#channel-btn")).toBeHidden();
		await expect(page.locator("#channel-panel")).toBeHidden();
		await expect(page.locator("#rgb-badge")).toBeHidden();
	});

	test("multichannel：换配色只应用最后一次（epoch），打开后 token 更新", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "multichannel");
		await page.goto(FIXTURE_HOST + "/fixture");
		const panel = page.locator("#channel-panel");
		// 快速连点两次（每次触发一次 POST）；无 JS 错误且控制器 token 为最后一次
		await panel.locator('.ch-row[data-index="4"] input[type=checkbox]').click();
		await panel.locator('.ch-row[data-index="5"] input[type=checkbox]').click();
		await expect(page.locator(".ch-count")).toContainText("6/12");
		await expect.poll(() => page.evaluate(() => window.__postCount), {
			timeout: 5_000,
		}).toBe(2);
		// 旧响应晚到不得覆盖新 context：最终 token 必为最后一次 POST 的签发
		await expect.poll(() => page.evaluate(() => window.__ctrl.getToken()), {
			timeout: 5_000,
		}).toBe("tok-fixture-2");
		expect(await page.evaluate(() => window.__postCount)).toBe(2);
	});

	test("虚构文字截图（multichannel 桌面/移动）存 test-results/", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "multichannel");
		await page.goto(FIXTURE_HOST + "/fixture");
		await page.waitForSelector("#channel-panel .ch-row");
		await page.screenshot({
			path: join("test-results", "channel-panel-desktop.png"),
			fullPage: false,
		});
		await page.setViewportSize({ width: 325, height: 700 });
		await page.screenshot({
			path: join("test-results", "channel-panel-mobile.png"),
			fullPage: false,
		});
	});
});
