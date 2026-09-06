/**
 * viewer 画质档 E2E（image-transport-upgrade §8.1/§8.2 UI 行）：
 * 真实 Chromium + 生产 static 资源（i18n.js / viewer-encoding.js /
 * channel-controls.js / style.css），静态 fixture（虚构数据，无患者数据）+
 * 路由拦截（乱序/失败路径专用 mock，§7.3 红线：性能场景不用 route mock）。
 *
 * 断言：
 *   - 新 UI + 新服务端（info.display 能力）：RGB 工具栏出现「标准/精细」
 *     分段；首开 tile URL 携带 ?profile=native-standard-v1&dv=…；点击精细
 *     → 轻量重开的 TileSource URL 换 profile=native-detail-v1 且 dv 随之
 *     更新；不触发 render-context POST（画质与 context 分离，§3.3）；
 *   - 多通道：tile URL 恒 profile=fluorescence-preserve-v1（无省流档），
 *     控件显示「荧光保真」标识；RGB 档不可选；
 *   - 新 UI + 旧服务端（info 明确缺 display）：控件隐藏、tile URL 无
 *     profile/dv（旧 URL 语义）——能力降级正确；
 *   - 409 合并诊断/第二次冲突停止的细节由 tests/js/viewer-encoding.test.ts
 *     锁定（真协议缓存头由 tests/test_viewer_encoding_protocol.py 以真实
 *     Flask+PG 覆盖；线上 HTTPS 缓存迁移证据属 G9 部署后验证）。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const here = typeof __dirname !== "undefined" ? __dirname : process.cwd();
const I18N_JS = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
const VIEWER_ENC_JS = readFileSync(
	resolve(here, "../../static/viewer-encoding.js"),
	"utf8",
);
const CHANNEL_JS = readFileSync(
	resolve(here, "../../static/channel-controls.js"),
	"utf8",
);
const PROD_CSS = readFileSync(resolve(here, "../../static/style.css"), "utf8");
const FIXTURE_HOST = "http://pt-ve-fixture.test";

const DV_STD = "a".repeat(64);
const DV_DETAIL = "b".repeat(64);
const DV_PRESERVE = "c".repeat(64);

function display(mode: "native_rgb" | "multichannel"): Record<string, unknown> {
	if (mode === "multichannel") {
		return {
			image_mode: "multichannel",
			display_asset_revision: "dar1-fix-mc",
			default_profile: "fluorescence-preserve-v1",
			profiles: [
				{ profile_id: "fluorescence-preserve-v1", display_version: DV_PRESERVE },
			],
			thumbnail: {
				max_edge: 400,
				profiles: [
					{ profile_id: "fluorescence-thumb-v1", display_version: DV_PRESERVE },
				],
			},
		};
	}
	return {
		image_mode: "native_rgb",
		display_asset_revision: "dar1-fix-rgb",
		default_profile: "native-standard-v1",
		profiles: [
			{ profile_id: "native-standard-v1", display_version: DV_STD },
			{ profile_id: "native-detail-v1", display_version: DV_DETAIL },
		],
		thumbnail: {
			max_edge: 400,
			profiles: [
				{ profile_id: "native-thumb-v1", display_version: DV_STD },
			],
		},
	};
}

function info(
	variant: "rgb" | "rgb-old-server" | "mc",
): Record<string, unknown> {
	const flagOn = variant !== "rgb-old-server" ? true : false;
	const base: Record<string, unknown> = {
		name: "fixture_demo.ome.tiff",
		image_mode: variant === "mc" ? "multichannel" : "native_rgb",
		asset_revision: "rev-fix-1",
		server_capability: {
			multichannel: variant === "mc",
			render_token: flagOn,
			render_context_endpoint: flagOn,
			// 旧服务端：无 display_encoding_v1（该缺失只在成功 info 里判定）
			...(variant === "rgb-old-server" ? {} : { display_encoding_v1: true }),
		},
		deepzoom: {
			width: 20000,
			height: 16000,
			tile_size: 512,
			overlap: 1,
			min_level: 0,
			max_level: 6,
		},
	};
	if (variant === "rgb" || variant === "rgb-old-server") {
		return {
			...base,
			channels: [],
			warnings: [],
			plane: { t: 0, z: 0, size_t: 1, size_z: 1, policy: "first-plane-v1" },
			default_render_context: {
				version: "native-rgb-v1",
				asset_revision: "rev-fix-1",
				plane: { t: 0, z: 0 },
				active_channels: [],
				fingerprint: "ab12cd34".repeat(8),
			},
			default_render_token: "tok-fixture-default",
			...(variant === "rgb" ? { display: display("native_rgb") } : {}),
		};
	}
	return {
		...base,
		channels: [
			{
				index: 0,
				name: "DAPI",
				color: "#00FFFF",
				color_source: "ome",
				alpha: 1,
				default_active: true,
				intensity: { status: "ok" },
			},
		],
		warnings: [],
		plane: { t: 0, z: 0, size_t: 1, size_z: 1, policy: "first-plane-v1" },
		axes: "CYX",
		default_render_context: {
			version: "multichannel-additive-v1",
			asset_revision: "rev-fix-1",
			plane: { t: 0, z: 0 },
			active_channels: [
				{ index: 0, color: "#00FFFF", alpha: 1, black: 1, white: 100, gamma: 1 },
			],
			fingerprint: "ef789012".repeat(8),
		},
		default_render_token: "tok-fixture-default",
		display: display("multichannel"),
	};
}

function fixtureHtml(variant: "rgb" | "rgb-old-server" | "mc"): string {
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<link rel="stylesheet" href="/static/style.css" />
<title>viewer-encoding fixture（虚构数据）</title>
</head>
<body style="margin:0">
<header id="app-header" style="display:flex;gap:6px;align-items:center;padding:8px 12px;">
  <div id="toolbar" style="display:flex;gap:6px;align-items:center;">
    <span id="quality-control" class="viewer-quality-host" hidden></span>
    <span id="rgb-badge" class="rgb-mode-badge" hidden data-i18n="channel.rgb.badge">原始 RGB</span>
    <button id="channel-btn" class="tool-btn channel-btn" type="button" hidden
            data-i18n="channel.entry" aria-haspopup="dialog" aria-expanded="false">通道</button>
  </div>
</header>
<main id="viewer-wrap" style="position:relative;height:80vh;">
  <div id="viewer" style="position:absolute;inset:0;"></div>
  <div id="channel-panel" class="channel-panel" hidden role="dialog"></div>
</main>
<script>window.__FIXTURE_VARIANT__ = ${JSON.stringify(variant)};</script>
<script src="/static/i18n.js"></script>
<script src="/static/viewer-encoding.js"></script>
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
    document.getElementById("viewer-wrap").appendChild(el);
  }
  // 最小 viewer 桩：记录 open() 收到的 TileSource 供断言取 URL
  var fakeViewer = {
    viewport: null,
    container: document.getElementById("viewer"),
    close: function () {},
    open: function (ts) { window.__opens.push(ts); },
    addHandler: function () {},
    addOnceHandler: function () {},
    removeHandler: function () {},
  };
  window.__opens = [];
  window.__postCount = 0;
  var adapter = {
    tileUrl: function (id, level, x, y, token, qualityQuery) {
      var q = "";
      if (qualityQuery) q += qualityQuery;
      q += token ? (q ? "&" : "?") + "render=" + token : "";
      return "/mock/" + id + "_files/" + level + "/" + x + "_" + y + ".jpeg" + q;
    },
    thumbnailUrl: function (id, token, qualityQuery) {
      var q = qualityQuery || "";
      return "/mock/" + id + "/thumbnail" + q;
    },
    normalizeRenderContext: function (id, body) {
      window.__postCount += 1;
      return fetch("/mock/render-context", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    },
  };
  var info = ${JSON.stringify(info(variant))};
  HP_ViewerEncoding.mount({
    host: document.getElementById("quality-control"),
    t: t,
    toast: toast,
    onQualityReopen: function () {
      window.__ctrl.reopenForQuality();
    },
  });
  window.__ctrl = HP_Channels.createChannelController({
    adapter: adapter,
    viewer: fakeViewer,
    button: document.getElementById("channel-btn"),
    badge: document.getElementById("rgb-badge"),
    panelHost: document.getElementById("channel-panel"),
    t: t,
    toast: toast,
    storage: window.localStorage,
    // render 计划的打开由页面负责（与 app.js/demo.js/share.js 相同契约）
    open: function (plan) {
      if (plan && plan.tileSource) fakeViewer.open(plan.tileSource);
    },
  });
  var plan = window.__ctrl.handleInfo(info, { id: info.name, scope: "fixture" });
  window.__plan = plan || null;
  // legacy 计划由页面走原 DZI 路径（与真实页面一致）
  if (!plan || plan.kind !== "render") {
    fakeViewer.open("/mock/fixture_demo.ome.tiff.dzi");
  }
})();
</script>
</body>
</html>`;
}

async function serveFixture(
	page: Page,
	variant: "rgb" | "rgb-old-server" | "mc",
): Promise<void> {
	await page.route(FIXTURE_HOST + "/**", (route: Route) => {
		const url = new URL(route.request().url());
		if (url.pathname === "/static/style.css") {
			return route.fulfill({ status: 200, contentType: "text/css; charset=utf-8", body: PROD_CSS });
		}
		if (url.pathname === "/static/i18n.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: I18N_JS });
		}
		if (url.pathname === "/static/viewer-encoding.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: VIEWER_ENC_JS });
		}
		if (url.pathname === "/static/channel-controls.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: CHANNEL_JS });
		}
		if (url.pathname === "/fixture") {
			return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: fixtureHtml(variant) });
		}
		return route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
	});
}

function lastOpenTileUrl(page: Page): Promise<string> {
	return page.evaluate(() => {
		const opens = (
			window as unknown as {
				__opens: (string | { getTileUrl: (l: number, x: number, y: number) => string })[];
			}
		).__opens;
		const ts = opens[opens.length - 1];
		if (!ts) return "";
		return typeof ts === "string" ? ts : ts.getTileUrl(0, 0, 0);
	});
}

test.describe("viewer 画质档 UI（生产 JS/CSS，虚构数据）", () => {
	test("RGB 新服务端：标准/精细分段；tile URL 带 profile/dv；切换不 POST context", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "rgb");
		await page.goto(FIXTURE_HOST + "/fixture");

		// 控件显示：标准/精细 两档
		const ctl = page.locator("#quality-control");
		await expect(ctl).toBeVisible();
		await expect(ctl.locator(".viewer-quality-btn")).toHaveText(["标准", "精细"]);
		// 首开 tile URL：标准档 + dv（成对）；RGB 无 render token
		expect(await lastOpenTileUrl(page)).toBe(
			`/mock/fixture_demo.ome.tiff_files/0/0_0.jpeg?profile=native-standard-v1&dv=${DV_STD}`,
		);
		// 点击精细：轻量重开换 profile，不 POST render-context（§3.3 分离）
		await ctl.locator(".viewer-quality-btn", { hasText: "精细" }).click();
		expect(await lastOpenTileUrl(page)).toBe(
			`/mock/fixture_demo.ome.tiff_files/0/0_0.jpeg?profile=native-detail-v1&dv=${DV_DETAIL}`,
		);
		const posts = await page.evaluate(() => (window as unknown as { __postCount: number }).__postCount);
		expect(posts).toBe(0);
		// 偏好持久化（仅浏览器端）
		const pref = await page.evaluate(() => window.localStorage.getItem("pt.viewerQuality.rgb"));
		expect(pref).toBe("native-detail-v1");
	});

	test("多通道新服务端：恒荧光保真档；无省流切换；显示标识", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "mc");
		await page.goto(FIXTURE_HOST + "/fixture");

		const ctl = page.locator("#quality-control");
		await expect(ctl).toBeVisible();
			const badge = ctl.locator(".viewer-quality-badge");
			await expect(badge).toHaveText("荧光保真 ✓");
			await expect(badge).toHaveAttribute("role", "note");
			expect(ctl.locator(".viewer-quality-btn")).toHaveCount(0);
			await expect.poll(async () =>
				badge.evaluate((el) => getComputedStyle(el).whiteSpace)).toBe("nowrap");
			const box = await badge.boundingBox();
			expect(box, "荧光保真 chip 必须可见").toBeTruthy();
			expect(box!.height, "荧光保真不得竖排换行").toBeLessThanOrEqual(28);
		expect(await lastOpenTileUrl(page)).toBe(
			`/mock/fixture_demo.ome.tiff_files/0/0_0.jpeg?profile=fluorescence-preserve-v1&dv=${DV_PRESERVE}&render=tok-fixture-default`,
		);
	});

	test("新 UI + 旧服务端（成功 info 明确缺 display）：控件隐藏，旧 URL 语义", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await serveFixture(page, "rgb-old-server");
		await page.goto(FIXTURE_HOST + "/fixture");

		// 能力缺失：控件隐藏，handleInfo 返回 legacy（页面走原 DZI 路径，
		// tile URL 无 profile/dv——旧 URL 语义）
		await expect(page.locator("#quality-control")).toBeHidden();
		const kind = await page.evaluate(
			() => (window as unknown as { __plan: { kind: string } | null }).__plan?.kind,
		);
		expect(kind).toBe("legacy");
		expect(await lastOpenTileUrl(page)).toBe("/mock/fixture_demo.ome.tiff.dzi");
	});
});
