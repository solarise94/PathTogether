/**
 * 顶栏信息架构 + Beta + 账户入口 E2E（升级 Review 2026-09-09 Batch B §3.3–3.5）。
 *
 * 真实 Chromium + 生产资源（templates/_app_shell.html 原文渲染 + static/app.js +
 * static/i18n.js + static/style.css），静态 fixture（虚构数据，无患者数据）+
 * 路由拦截 mock（后端 /api/account/balance 由主代理并行实施，本 spec 只锁前端
 * 契约；§7.3 红线：性能场景不用 route mock，这里无性能场景）。
 *
 * 断言：
 *   - 账户 chip 显示邮箱 local-part（solarise94），popover 显示完整邮箱、角色、
 *     余额（精确两位 CNY）与口径（user=一次性总额度）；额度缺失（400
 *     spend_total_allowance_missing）显示「额度信息暂不可用」，绝不显示 ¥0；
 *   - 标注名称（可选）在「标注选项」popover 内，主行不再出现自由文本输入框；
 *   - 矩形尺寸为按钮下方锚定 popover；选预设后主行只显示简短摘要（6×6 mm）；
 *   - 品牌区 Beta 徽标可聚焦提示；壳内无 #current-slide；打开切片后
 *     document.title = "<切片名> · PathTogether Beta"；
 *   - 宽度断点分组：1440 全展开；1024–1439 折视图/标注组；<1024 只留当前
 *     工具、AI、倍率、账户（其余入 ⋯）；≤768 交还移动端布局；
 *   - Demo 只读壳：无账户 chip，保留 Demo 徽章与 Beta 徽标。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const here = typeof __dirname !== "undefined" ? __dirname : process.cwd();
const SHELL_HTML = readFileSync(resolve(here, "../../templates/_app_shell.html"), "utf8");
const APP_JS = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const I18N_JS = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
const PROD_CSS = readFileSync(resolve(here, "../../static/style.css"), "utf8");
const FIXTURE_HOST = "http://pt-ta-fixture.test";

// ---------------------------------------------------------------------------
// 极简 Jinja 渲染：只支持 _app_shell.html 用到的 {% if %}/{% elif %}/{% else %}
// 与 `{% set %}` 剥离。表达式经 JS 求值（or/and 转 ||/&&）。
// ---------------------------------------------------------------------------
type Node = { text: string } | { tag: string; branches: Array<{ cond: string | null; body: Node[] }> };

function tokenize(src: string): Array<{ kind: "text" | "tag"; value: string }> {
	// 先剥 {# ... #} 注释（壳模板大量使用；渲染为正文会污染布局与点击命中）
	const cleaned = src.replace(/\{#[\s\S]*?#\}/g, "");
	const out: Array<{ kind: "text" | "tag"; value: string }> = [];
	const re = /\{%\s*(.*?)\s*%\}/g;
	let last = 0;
	let m: RegExpExecArray | null;
	while ((m = re.exec(cleaned)) !== null) {
		if (m.index > last) out.push({ kind: "text", value: cleaned.slice(last, m.index) });
		out.push({ kind: "tag", value: m[1] });
		last = m.index + m[0].length;
	}
	if (last < cleaned.length) out.push({ kind: "text", value: cleaned.slice(last) });
	return out;
}

function parse(tokens: Array<{ kind: "text" | "tag"; value: string }>, start: number, stopTags: string[]): [Node[], number] {
	const nodes: Node[] = [];
	let i = start;
	while (i < tokens.length) {
		const tok = tokens[i];
		if (tok.kind === "text") {
			nodes.push({ text: tok.value });
			i += 1;
			continue;
		}
		const keyword = tok.value.split(/\s+/)[0];
		if (stopTags.includes(keyword)) return [nodes, i];
		if (keyword === "if") {
			const cond = tok.value.replace(/^if\s+/, "");
			const branches: Array<{ cond: string | null; body: Node[] }> = [];
			let cursor = i + 1;
			let currentCond: string | null = cond;
			for (;;) {
				const [body, next] = parse(tokens, cursor, ["elif", "else", "endif"]);
				branches.push({ cond: currentCond, body });
				cursor = next;
				const kw = (tokens[cursor] as { kind: "tag"; value: string } | undefined);
				if (!kw) throw new Error("unterminated if");
				const k = kw.value.split(/\s+/)[0];
				if (k === "elif") {
					currentCond = kw.value.replace(/^elif\s+/, "");
					cursor += 1;
					continue;
				}
				if (k === "else") {
					currentCond = "true";
					cursor += 1;
					continue;
				}
				// endif
				cursor += 1;
				break;
			}
			nodes.push({ tag: "if", branches });
			i = cursor;
			continue;
		}
		if (keyword === "set") {
			// set 只用于页头别名（mode/C），测试直接提供上下文 → 剥离
			i += 1;
			continue;
		}
		throw new Error(`unsupported tag: ${keyword}`);
	}
	return [nodes, i];
}

function evalCond(cond: string, ctx: Record<string, unknown>): boolean {
	const js = cond
		.replace(/\bor\b/g, "||")
		.replace(/\band\b/g, "&&")
		.replace(/'/g, '"');
	// eslint-disable-next-line @typescript-eslint/no-implied-eval, no-new-func
	return new Function(...Object.keys(ctx), `return (${js});`)(...Object.values(ctx));
}

function renderNodes(nodes: Node[], ctx: Record<string, unknown>): string {
	return nodes
		.map((n) => {
			if ("text" in n) return n.text;
			for (const b of n.branches) {
				if (b.cond === null || evalCond(b.cond, ctx)) return renderNodes(b.body, ctx);
			}
			return "";
		})
		.join("");
}

function renderShell(ctx: Record<string, unknown>): string {
	const [ast] = parse(tokenize(SHELL_HTML), 0, []);
	return renderNodes(ast, ctx);
}

const VIEWER_STUB = `
(function () {
  // 最小 viewer 桩：本 fixture 不加载 OpenSeadragon，只验证平台工具栏/DOM 逻辑
  var viewport = {
    imageToViewportRectangle: function (x, y, w, h) { return { x: x / 100000, y: y / 100000, w: w / 100000, h: h / 100000 }; },
    imageToViewerElementCoordinates: function (p) { return { x: p.x, y: p.y }; },
    viewerElementToImageCoordinates: function (p) { return { x: p.x, y: p.y }; },
    getZoom: function () { return 1; },
    getContainerSize: function () { return { x: 800, y: 600 }; },
    getFlip: function () { return false; },
    toggleFlip: function () {},
    setRotation: function () {},
    goHome: function () {},
    applyConstraints: function () {},
    zoomBy: function () {},
    zoomTo: function () {},
    fitBounds: function () {},
  };
  var viewer = {
    container: null,
    canvas: {},
    viewport: viewport,
    currentOverlays: [],
    addHandler: function () {},
    open: function () {},
    close: function () {},
    setMouseNavEnabled: function () {},
    addOverlay: function () {},
    updateOverlay: function () {},
    removeOverlay: function () {},
    getOverlayById: function () { return null; },
    forceResize: function () {},
  };
  window.HP_ViewerCore = {
    create: function (el) { viewer.container = el; return viewer; },
  };
  window.OpenSeadragon = {
    Point: function (x, y) { this.x = x; this.y = y; },
    Placement: { TOP_LEFT: "top-left" },
    OverlayRotationMode: { BOUNDING_BOX: "bounding-box" },
  };
})();
`;

function fixtureHtml(opts: { mode?: "official" | "demo" } = {}): string {
	const mode = opts.mode ?? "official";
	const demo = mode === "demo";
	const body = renderShell({
		mode,
		C: demo
			? { readonly_badge: true, login_cta: true }
			: { roi: true, annotate: true, save_image: true, mpp: true },
		logged_in: false,
		histopilot_ui_enabled: !demo,
		viewer_role: "owner",
	});
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>toolbar-account fixture（虚构数据）</title>
<link rel="stylesheet" href="/static/style.css" />
</head>
<body class="app-mode-${mode}" data-app-mode="${mode}" style="margin:0">
${body}
<script>${VIEWER_STUB}</script>
<script src="/static/i18n.js"></script>
<script>try { HP_I18N.setLang("zh"); } catch (e) {}</script>
${mode === "official" ? '<script src="/static/app.js"></script>' : ""}
</body>
</html>`;
}

const AUTH_USER = {
	auth_enabled: true,
	username: "solarise94@gmail.com",
	role: "user",
	user_id: "u-1",
	actor: { username: "solarise94@gmail.com", role: "user", user_id: "u-1" },
	preview: null,
};

const BALANCE_OK = {
	subject: {
		username: "solarise94@gmail.com",
		display_username: "solarise94",
		role: "user",
		preview: false,
	},
	currency: "CNY",
	spend_target: "total_allowance",
	limit_nano_cny: "50000000000",
	spent_nano_cny: "12000000000",
	reserved_nano_cny: "1000000000",
	remaining_nano_cny: "37000000000",
	period_start: null,
	period_end: null,
	as_of: "2026-09-09T12:00:00Z",
};

const SLIDE_INFO = {
	name: "fixture_demo.ome.tiff",
	alias: "Fixture Slide A",
	width: 100000,
	height: 100000,
	mpp_x: 0.25,
	mpp_y: 0.25,
	mpp_source: "calibrated",
	asset_revision: "rev-fix-1",
};

async function serveFixture(
	page: Page,
	opts: { mode?: "official" | "demo"; balance?: () => { status: number; body: unknown } } = {},
) {
	await page.route(FIXTURE_HOST + "/**", (route: Route) => {
		const url = new URL(route.request().url());
		const p = url.pathname;
		if (p === "/static/style.css") {
			return route.fulfill({ status: 200, contentType: "text/css; charset=utf-8", body: PROD_CSS });
		}
		if (p === "/static/i18n.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: I18N_JS });
		}
		if (p === "/static/app.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: APP_JS });
		}
		if (p === "/fixture") {
			return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: fixtureHtml({ mode: opts.mode }) });
		}
		if (p === "/api/auth/info") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(opts.mode === "demo" ? { auth_enabled: false, username: null, role: null, user_id: null, actor: {}, preview: null } : AUTH_USER) });
		}
		if (p === "/api/account/balance") {
			const r = (opts.balance ?? (() => ({ status: 200, body: BALANCE_OK })))();
			return route.fulfill({ status: r.status, contentType: "application/json", body: JSON.stringify(r.body) });
		}
		if (p === "/api/slides") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
				{ name: SLIDE_INFO.name, alias: SLIDE_INFO.alias, width: SLIDE_INFO.width, height: SLIDE_INFO.height, mpp_x: SLIDE_INFO.mpp_x, mpp_y: SLIDE_INFO.mpp_y, mpp_source: SLIDE_INFO.mpp_source },
			]) });
		}
		if (p === "/api/annotations") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ by_slide: {} }) });
		}
		if (p === "/api/projects") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
		}
		if (p === "/api/share/list") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ shares: [] }) });
		}
		if (p === "/api/slide/" + SLIDE_INFO.name + "/info") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SLIDE_INFO) });
		}
		// 缩略图 / DZI：给空响应即可（viewer 为桩，不真正加载瓦片）
		return route.fulfill({ status: 204, body: "" });
	});
}

async function openSlideRow(page: Page): Promise<void> {
	// 桌面默认侧栏收起（visibility:hidden）→ 先展开再点击切片行
	const collapsed = await page.locator("#sidebar").evaluate((el) =>
		el.classList.contains("collapsed"),
	);
	if (collapsed) {
		await page.locator("#menu-btn").click();
	}
	// 点行内名称区（避开行首复选框：其 click 有 stopPropagation）
	await page
		.locator(".slide-row", { hasText: "Fixture Slide A" })
		.first()
		.locator(".slide-mid")
		.click();
	// openSlide 完成（info 拉取并写入 document.title）后再继续
	await expect(page).toHaveTitle(/Fixture Slide A · PathTogether Beta/);
}

test.describe("账户 chip + popover（§3.5）", () => {
	test("chip 显示 local-part；popover 显示完整邮箱/角色/精确余额与口径", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");

		const chip = page.locator("#acct-btn");
		await expect(chip).toBeVisible();
		// local-part，不是完整邮箱，也不是标注名称输入框
		await expect(chip).toHaveText(/solarise94/);
		await expect(chip).not.toHaveText(/@/);

		await chip.click();
		const pop = page.locator("#acct-pop");
		await expect(pop).toBeVisible();
		await expect(page.locator("#acct-pop-email")).toHaveText("solarise94@gmail.com");
		await expect(page.locator("#acct-pop-role")).toHaveText("用户（user）");
		// user = 一次性总额度口径；nano-CNY 精确两位换算（37e9 nano = 37.00 CNY）
		await expect(page.locator("#acct-pop-scope")).toHaveText("一次性总额度");
		await expect(page.locator("#acct-pop-remaining")).toContainText("37.00 CNY");
		await expect(page.locator("#acct-pop-detail")).toContainText("50.00 CNY");
		await expect(page.locator("#acct-pop-detail")).toContainText("12.00 CNY");
		await expect(page.locator("#acct-pop-detail")).toContainText("1.00 CNY");
		await expect(chip).toHaveAttribute("aria-expanded", "true");
		// 预览标记只在预览态出现
		await expect(page.locator("#acct-pop-preview")).toBeHidden();
	});

	test("额度缺失（400 spend_total_allowance_missing）：显示「暂不可用」，绝不显示 ¥0", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page, {
			balance: () => ({ status: 400, body: { error: "spend_total_allowance_missing", code: "spend_total_allowance_missing" } }),
		});
		await page.goto(FIXTURE_HOST + "/fixture");
		await page.locator("#acct-btn").click();
		const remaining = page.locator("#acct-pop-remaining");
		await expect(remaining).toHaveText("—");
		await expect(page.locator("#acct-pop-detail")).toContainText("额度信息暂不可用");
		await expect(page.locator("#acct-pop-detail")).toContainText("未设置总额度");
	});

	test("DB 不可用（503）：显示「暂不可用（数据库暂不可用）」", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page, {
			balance: () => ({ status: 503, body: { error: "database_unavailable" } }),
		});
		await page.goto(FIXTURE_HOST + "/fixture");
		await page.locator("#acct-btn").click();
		await expect(page.locator("#acct-pop-detail")).toContainText("额度信息暂不可用");
		await expect(page.locator("#acct-pop-detail")).toContainText("数据库暂不可用");
		await expect(page.locator("#acct-pop-remaining")).toHaveText("—");
	});

	test("「账户设置」入口滚动到侧栏既有改密/改绑区并展开侧栏", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		// 桌面默认侧栏收起
		await expect(page.locator("#sidebar")).toHaveClass(/collapsed/);
		await page.locator("#acct-btn").click();
		await page.locator("#acct-settings-btn").click();
		// 侧栏被展开（沿用既有改密/改绑入口；滚动目标存在）
		await expect(page.locator("#sidebar")).not.toHaveClass(/collapsed/);
		await expect(page.locator("#changepw-btn")).toBeVisible();
		await expect(page.locator("#acct-pop")).toBeHidden();
	});
});

test.describe("标注选项 popover + 矩形摘要（§3.3）", () => {
	test("标注名称（可选）在 popover 内；主行不再出现自由文本输入框", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");

		// 结构：input 在 #anno-pop 内，不在 #tbb-context 内
		const placement = await page.evaluate(() => ({
			inPop: !!document.getElementById("anno-pop")?.contains(document.getElementById("anno-label-input")),
			inCtx: !!document.getElementById("tbb-context")?.contains(document.getElementById("anno-label-input")),
			inMainRow: !!document.getElementById("toolbar")?.contains(document.getElementById("anno-label-input")),
		}));
		expect(placement.inPop).toBe(true);
		expect(placement.inCtx).toBe(false);

		const pop = page.locator("#anno-pop");
		await expect(pop).toBeHidden();
		// 打开「标注选项」popover 后输入框与「标注名称（可选）」标签可见
		await page.locator("#anno-more-btn").click();
		await expect(pop).toBeVisible();
		await expect(page.locator(".anno-pop-label")).toHaveText("标注名称（可选）");
		const input = page.locator("#anno-label-input");
		await expect(input).toBeVisible();
		await input.fill("肿瘤区");
		expect(await input.inputValue()).toBe("肿瘤区");
		await expect(page.locator("#anno-more-btn")).toHaveAttribute("aria-expanded", "true");
		// Escape 关闭
		await page.keyboard.press("Escape");
		await expect(pop).toBeHidden();
	});

	test("矩形尺寸锚定 popover：选预设后主行只显示摘要 6×6 mm；再点矩形按钮关闭", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		await openSlideRow(page);

		const rectBtn = page.locator("#roi-rect-btn");
		await rectBtn.click();
		const settings = page.locator("#roi-settings");
		await expect(settings).toBeVisible();
		await expect(rectBtn).toHaveAttribute("aria-expanded", "true");

		// 预设 6×6 mm → 主行摘要（不显示 5 项设置本体）
		await page.locator("#roi-preset-select").selectOption("6");
		const summary = page.locator("#roi-summary");
		await expect(summary).toBeVisible();
		await expect(summary).toHaveText("6×6 mm");

		// 再次点击矩形按钮：退出工具并关闭 popover（既有交互语义）
		await rectBtn.click();
		await expect(settings).toBeHidden();
		await expect(summary).toBeHidden();
		await expect(rectBtn).toHaveAttribute("aria-expanded", "false");
	});
});

test.describe("Beta 徽标 + 切片名下架（§3.4）", () => {
	test("品牌区 Beta 徽标可聚焦提示；壳内无 #current-slide", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");

		expect(await page.locator("#current-slide").count()).toBe(0);
		const beta = page.locator(".beta-badge");
		await expect(beta).toBeVisible();
		await expect(beta).toHaveText("Beta");
		await expect(beta).toHaveAttribute("aria-label", /Beta 测试中/);
		await expect(beta).toHaveAttribute("tabindex", "0");
		// 打开切片后标题组合
		await openSlideRow(page);
		await expect(page).toHaveTitle(/Fixture Slide A · PathTogether Beta/);
	});

	test("Demo 只读壳：无账户 chip；保留 Beta 徽标与 Demo 徽章", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page, { mode: "demo" });
		await page.goto(FIXTURE_HOST + "/fixture");

		expect(await page.locator("#acct-btn").count()).toBe(0);
		expect(await page.locator("#acct-pop").count()).toBe(0);
		await expect(page.locator(".beta-badge")).toBeVisible();
		await expect(page.locator(".demo-badge").first()).toBeVisible();
		// Demo 顶栏不带标注/矩形写工具（capabilities 不渲染），无标注输入框
		expect(await page.locator("#anno-label-input").count()).toBe(0);
	});
});

test.describe("宽度断点分组（§3.3）", () => {
	async function foldedIds(page: Page): Promise<string[]> {
		return page.evaluate(() =>
			Array.from(document.querySelectorAll("#tbb-more > *")).map(
				(el) => (el as HTMLElement).id || (el as HTMLElement).className,
			),
		);
	}

	test(">=1440 全展开：视图/标注组不在 ⋯ 菜单", async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).not.toContain("view-tools-group");
		expect(ids).not.toContain("anno-tools-group");
		expect(ids).not.toContain("zoom-group");
	});

	test("1024–1439：视图组与标注组折入 ⋯；缩放组/矩形/AI/账户留主行", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).toEqual(expect.arrayContaining([
			"view-tools-group",
			"quality-control",
			"channel-btn",
			"anno-tools-group",
			"anno-btn",
			"save-anno-btn",
		]));
		expect(ids).not.toContain("zoom-group");
		expect(ids).not.toContain("save-btn");
		// 主行保留
		const mainRow = await page.evaluate(() => {
			const toolbar = document.getElementById("toolbar");
			const inside = (id: string) => !!toolbar?.contains(document.getElementById(id));
			return {
				rect: inside("roi-rect-btn"),
				ai: inside("ai-btn"),
				zoom: inside("zoom-group"),
				acct: inside("acct-wrap"),
				badge: inside("zoom-badge"),
			};
		});
		expect(mainRow).toEqual({ rect: true, ai: true, zoom: true, acct: true, badge: true });
		// 搬移的是同一 DOM 节点：菜单里的旋转钮可交互（状态机不复制）
		await page.locator("#tbb-more-btn").click();
		const rotateInMenu = page.locator("#tbb-more > #view-tools-group > #rotate-btn");
		await expect(rotateInMenu).toBeVisible();
		await expect(rotateInMenu).toBeEnabled();
		await rotateInMenu.click();
	});

	test("<1024：缩放组/保存图片/mpp/1:1 也入菜单；倍率与账户仍留主行", async ({ page }) => {
		await page.setViewportSize({ width: 1000, height: 800 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).toEqual(expect.arrayContaining([
			"zoom-group",
			"save-btn",
			"mpp-setter",
			"zoom-native",
			"view-tools-group",
			"anno-tools-group",
		]));
		expect(ids).not.toContain("zoom-badge");
		expect(ids).not.toContain("acct-wrap");
		expect(ids).not.toContain("roi-rect-btn");
		expect(ids).not.toContain("ai-btn");
	});

	test("<=768 交还移动端布局：节点全部归位（分组不搬移）", async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 844 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).not.toContain("view-tools-group");
		expect(ids).not.toContain("zoom-group");
		expect(ids).not.toContain("anno-tools-group");
	});
});
