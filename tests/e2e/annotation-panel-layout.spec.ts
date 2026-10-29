/** 标注面板布局回归 + 单条导出 e2e（真实 Flask + 内嵌 PG + Chromium）。
 *
 * 布局回归（生产 2026-10-29 截图复现）：标注行的文字按钮沿用 26×26 图标钮
 * 固定宽度，内容向两侧溢出，遮挡 ✎/👁 并被面板左边缘裁掉；长名称与箭头
 * 的长坐标串不断行，压到 👁 钮；面板标题没有省略规则而换成两行。
 *
 * 本 spec 通过 API 造三条真实标注，然后拦截 /api/annotations 响应，把列表
 * 改成生产截图的同形数据（全部私有、来源 admin、前两条 source=ai/pending、
 * 箭头用超长 level-0 坐标）。这是纯 UI 布局测试：几何/审核状态只影响按钮
 * 组合与文字长度，用拦截避免构造真实 AI 书写链路与超大坐标几何。
 *
 * 断言（桌面 1440×900 中文、桌面英文、手机 390×844 中文各跑一遍）：
 *  - 每条标注的可见直接子元素两两不重叠（元素框 ∪ 文本 Range 框）；
 *  - 没有子元素越出 #anno-panel 左右边界；
 *  - 文字操作钮 scrollWidth 不超过 clientWidth；
 *  - 面板标题单行省略（nowrap、一段文本只占一行、不与头部按钮重叠）；
 *  - 头部导出钮显示「导出全部」/「Export all」。
 *
 * 单条导出：点多边形行的「导出」，下载文件只含该条 Feature 且
 * pathtogether.annotation_id 正确；再点头部导出仍得到两条。
 *
 * 登录一律键盘逐字输入（不用 fill）；凭据只来自 E2E_CREDS_FILE，不打印。
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8")) as {
	baseUrl: string; shareBaseUrl?: string;
	ownerLogin: string; ownerPassword: string;
	userLogin: string; userPassword: string;
	rasterSlides: Record<"workbench" | "share", { slide_id: string; name: string }>;
};

const SID = CREDS.rasterSlides.workbench.slide_id;
const LONG_LABEL = "肺泡腔内异型上皮细胞团-待鉴别";
const SECOND_LABEL = "管理员";

/** 页内 fetch（带 CSRF 头）。 */
async function pageApi(page: Page, method: string, path: string, body?: unknown) {
	return page.evaluate(async ({ method, path, body }) => {
		const csrf = decodeURIComponent(document.cookie.split("; ")
			.find((s) => s.startsWith("csrf_token="))?.split("=").slice(1).join("=") || "");
		const r = await fetch(path, {
			method,
			headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
			body: body === undefined ? undefined : JSON.stringify(body),
		});
		let j: Record<string, unknown> = {};
		try { j = await r.json(); } catch { /* 空体 */ }
		return { status: r.status, j };
	}, { method, path, body });
}

/** 清空切片上当前用户可见的全部标注（用例前后保持隔离）。 */
async function wipeAnnotations(page: Page, sid: string) {
	const listing = await pageApi(page, "GET",
		`/api/annotations?slide_id=${encodeURIComponent(sid)}`);
	const items: Array<Record<string, unknown>> = [];
	((listing.j.annotations || []) as Array<{ items?: Array<Record<string, unknown>> }>)
		.forEach((grp) => (grp.items || []).forEach((it) => items.push(it)));
	for (const it of items) {
		const aid = it.annotation_id as string;
		const idx = it.index as number;
		if (aid) {
			await pageApi(page, "DELETE", `/api/annotation/id/${aid}`);
		} else {
			await pageApi(page, "DELETE", `/api/annotation/admin/${idx}`);
		}
	}
	await expect.poll(async () => {
		const after = await pageApi(page, "GET",
			`/api/annotations?slide_id=${encodeURIComponent(sid)}`);
		return ((after.j.annotations || []) as unknown[]).reduce(
			(a, g) => a + ((g as { items?: unknown[] }).items || []).length, 0);
	}, { timeout: 10_000 }).toBe(0);
}

/** 键盘逐字输入凭据（不用 fill；失败信息也不回显凭据）。 */
async function loginByTyping(page: Page, loginId: string, password: string) {
	await page.goto("/login");
	const user = page.locator('form[action="/login"] input[name="username"]');
	const pass = page.locator('form[action="/login"] input[name="password"]');
	await user.click();
	await page.keyboard.type(loginId);
	await pass.click();
	await page.keyboard.type(password);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.locator('form[action="/login"] button[type="submit"]').click(),
	]);
}

async function expandSidebar(page: Page) {
	const menuBtn = page.locator("#menu-btn");
	const marker = page.locator("#fb-location");
	await expect(menuBtn).toHaveAttribute("aria-expanded", /^(true|false)$/);
	if ((await menuBtn.getAttribute("aria-expanded")) === "true") {
		await expect(marker).toBeVisible({ timeout: 5_000 });
		return;
	}
	await menuBtn.click();
	await expect(menuBtn).toHaveAttribute("aria-expanded", "true", { timeout: 5_000 });
	await expect(marker).toBeVisible({ timeout: 5_000 });
}

/** 打开工作台切片（桌面走侧栏；手机端由 /app?slide= 直接打开）。 */
async function openSlide(page: Page, mobile: boolean) {
	await page.goto(`/app?slide=${SID}`);
	if (!mobile) {
		await expandSidebar(page);
		const row = page.locator(`.fb-hit[data-slide-id="${SID}"]`);
		await expect(row).toBeVisible();
		await row.click();
	}
	await expect(page.locator("#viewer .openseadragon-canvas"))
		.toBeVisible({ timeout: 20_000 });
}

/** 打开标注面板（手机端入口可能收在工具栏「更多」菜单里）。 */
async function openAnnoPanel(page: Page, mobile: boolean) {
	const annoBtn = page.locator("#anno-btn");
	if (mobile && !(await annoBtn.isVisible())) {
		await page.locator("#tbb-more-btn").click();
	}
	await expect(annoBtn).toBeVisible({ timeout: 20_000 });
	await expect(annoBtn).toBeEnabled({ timeout: 20_000 });
	await annoBtn.click();
	await expect(page.locator("#anno-panel")).toBeVisible();
	await expect(page.locator("#anno-panel .anno-item").first())
		.toBeVisible({ timeout: 15_000 });
}

/** 把真实标注列表改造成生产截图同形（原因见文件头注释）。 */
async function shapeAnnotationsLikeProduction(page: Page) {
	await page.route("**/api/annotations?slide_id=*", async (route) => {
		const resp = await route.fetch();
		const j = await resp.json() as {
			annotations?: Array<{ label?: string; items?: Array<Record<string, unknown>> }>;
		};
		for (const grp of j.annotations || []) {
			for (const it of grp.items || []) {
				it.shared = false;
				it.token = "admin";
				if (grp.label === LONG_LABEL || it.type === "arrow") {
					it.source = "ai";
					it.review_status = "pending";
					// 2026-10-19 起查看器按服务端 author_kind 判定 AI（source=ai
					// 但无 AI 会话/溯源的存量人工行 author_kind=user，不再渲染
					// 接受/驳回按钮）。这里模拟真 AI 待审行，布局用例继续覆盖
					// 带审核按钮的拥挤行；下面的新增断言守住这一覆盖面。
					it.author_kind = "ai";
					it.author_key = "ai";
				}
				if (it.type === "arrow") {
					it.x1 = 37454; it.y1 = 23909;
					it.x2 = 35316; it.y2 = 22266;
				}
			}
		}
		await route.fulfill({ response: resp, json: j });
	});
}

async function createLayoutAnnotations(page: Page) {
	await wipeAnnotations(page, SID);
	const created = [
		{ type: "rect", label: LONG_LABEL, x: 40, y: 30, w: 90, h: 70 },
		{ type: "arrow", label: SECOND_LABEL, x1: 200, y1: 150, x2: 120, y2: 90 },
		{ type: "rect", label: SECOND_LABEL, x: 220, y: 200, w: 120, h: 80 },
	];
	for (const body of created) {
		const r = await pageApi(page, "POST", "/api/annotation",
			{ slide_id: SID, ...body });
		expect(r.status, JSON.stringify(r.j)).toBe(200);
	}
}

interface RowLayout {
	text: string;
	overlaps: string[];
	clipped: string[];
	overflowing: string[];
}

interface LayoutMetrics {
	panel: { left: number; right: number };
	title: {
		whiteSpace: string;
		lines: number;
		height: number;
		overlapsActions: boolean;
	};
	actionsVisible: boolean;
	exportText: string;
	rows: RowLayout[];
}

/** 行内直接子元素两两不重叠、不越界、不横向溢出（与生产探针同口径）。 */
async function collectLayoutMetrics(page: Page): Promise<LayoutMetrics> {
	return page.evaluate(() => {
		const panel = document.getElementById("anno-panel")!;
		const pr = panel.getBoundingClientRect();
		const inter = (a: DOMRect, b: DOMRect) =>
			Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 &&
			Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
		const union = (a: DOMRect, b: DOMRect): DOMRect => new DOMRect(
			Math.min(a.left, b.left), Math.min(a.top, b.top),
			Math.max(a.right, b.right) - Math.min(a.left, b.left),
			Math.max(a.bottom, b.bottom) - Math.min(a.top, b.top));
		const contentRect = (el: Element): DOMRect => {
			const range = document.createRange();
			range.selectNodeContents(el);
			const rects = [...range.getClientRects()]
				.filter((r) => r.width > 0 && r.height > 0);
			if (!rects.length) return el.getBoundingClientRect();
			return new DOMRect(
				Math.min(...rects.map((r) => r.left)),
				Math.min(...rects.map((r) => r.top)),
				Math.max(...rects.map((r) => r.right)) -
					Math.min(...rects.map((r) => r.left)),
				Math.max(...rects.map((r) => r.bottom)) -
					Math.min(...rects.map((r) => r.top)));
		};
		const rows = [...document.querySelectorAll("#anno-panel-list .anno-item")]
			.map((row) => {
				const kids = ([...row.querySelectorAll(":scope > *")] as HTMLElement[])
					.filter((el) => {
						const cs = getComputedStyle(el);
						return cs.display !== "none" && cs.visibility !== "hidden" &&
							el.getBoundingClientRect().width > 0;
					});
				const boxes = kids.map((el) => el.getBoundingClientRect());
				const names = kids.map((el) => String(el.className));
				const overlaps: string[] = [];
				for (let i = 0; i < kids.length; i++) {
					const a = union(boxes[i], contentRect(kids[i]));
					for (let j = 0; j < kids.length; j++) {
						if (i === j) continue;
						if (inter(a, boxes[j])) {
							overlaps.push(names[i] + " ⟂ " + names[j]);
						}
					}
				}
				const clipped = kids.filter((_el, i) =>
					boxes[i].left < pr.left - 0.5 || boxes[i].right > pr.right + 0.5 ||
					contentRect(kids[i]).left < pr.left - 0.5 ||
					contentRect(kids[i]).right > pr.right + 0.5).map((el) => String(el.className));
				const overflowing = kids.filter((el) =>
					el.scrollWidth > el.clientWidth + 1).map((el) => String(el.className));
				return {
					text: (row as HTMLElement).innerText.replace(/\s+/g, " ").slice(0, 120),
					overlaps: [...new Set(overlaps)],
					clipped: [...new Set(clipped)],
					overflowing: [...new Set(overflowing)],
				};
			});
		const title = document.getElementById("anno-panel-title")!;
		const titleStyle = getComputedStyle(title);
		const titleLineHeight = parseFloat(titleStyle.lineHeight) || 0;
		const actions = document.querySelector(".anno-panel-header-actions")!;
		const ar = actions.getBoundingClientRect();
		const tr = title.getBoundingClientRect();
		const actionsVisible = getComputedStyle(actions).display !== "none" &&
			ar.width > 0 && ar.height > 0;
		return {
			panel: { left: pr.left, right: pr.right },
			title: {
				whiteSpace: titleStyle.whiteSpace,
				lines: titleLineHeight > 0
					? Math.max(1, Math.round(tr.height / titleLineHeight)) : 1,
				height: tr.height,
				overlapsActions: actionsVisible &&
					Math.min(tr.right, ar.right) - Math.max(tr.left, ar.left) > 0.5 &&
					Math.min(tr.bottom, ar.bottom) - Math.max(tr.top, ar.top) > 0.5,
			},
			actionsVisible,
			exportText: (document.querySelector(
				"#anno-export-btn .panel-btn-tx") as HTMLElement | null)?.textContent || "",
			rows,
		};
	});
}

interface LayoutCase {
	name: string;
	lang: "zh" | "en";
	mobile: boolean;
	viewport: { width: number; height: number };
	exportAllText: string;
}

const LAYOUT_CASES: LayoutCase[] = [
	{ name: "桌面中文", lang: "zh", mobile: false,
		viewport: { width: 1440, height: 900 }, exportAllText: "导出全部" },
	{ name: "桌面英文", lang: "en", mobile: false,
		viewport: { width: 1440, height: 900 }, exportAllText: "Export all" },
	{ name: "手机中文", lang: "zh", mobile: true,
		viewport: { width: 390, height: 844 }, exportAllText: "导出全部" },
];

async function newSessionPage(browser: Browser, cfg: LayoutCase): Promise<Page> {
	const context = await browser.newContext({
		viewport: cfg.viewport,
		...(cfg.mobile ? { isMobile: true, hasTouch: true } : {}),
	});
	await context.addInitScript((lang) => {
		try { window.localStorage.setItem("hp_lang", lang); } catch { /* 忽略 */ }
	}, cfg.lang);
	return context.newPage();
}

for (const cfg of LAYOUT_CASES) {
	test(`标注面板布局：${cfg.name}不遮挡、不溢出、标题单行`, async ({ browser }) => {
		const page = await newSessionPage(browser, cfg);
		try {
			await loginByTyping(page, CREDS.userLogin, CREDS.userPassword);
			await createLayoutAnnotations(page);
			await shapeAnnotationsLikeProduction(page);
			await openSlide(page, cfg.mobile);
			await openAnnoPanel(page, cfg.mobile);

			await expect(page.locator("#anno-panel .anno-item")).toHaveCount(3);
			// 显示规则改为 author_kind 口径后，拦截数据必须显式 author_kind=ai
			// 才会渲染接受/驳回按钮；守住「两条真 AI 待审行」的布局覆盖面。
			const reviewRows = page.locator("#anno-panel .anno-item").filter({
				has: page.locator(".ai-review-accept"),
			});
			await expect(reviewRows).toHaveCount(2);
			for (let i = 0; i < 2; i++) {
				await expect(reviewRows.nth(i).locator(".ai-review-accept")).toHaveCount(1);
				await expect(reviewRows.nth(i).locator(".ai-review-reject")).toHaveCount(1);
			}
			await expect(page.locator("#anno-export-btn")).toBeEnabled();
			await expect(page.locator("#anno-export-btn .panel-btn-tx"))
				.toHaveText(cfg.exportAllText);

			const metrics = await collectLayoutMetrics(page);
			expect(metrics.rows).toHaveLength(3);
			for (const row of metrics.rows) {
				expect(row.overlaps, `${cfg.name} 行重叠：${row.text}`).toEqual([]);
				expect(row.clipped, `${cfg.name} 行越界：${row.text}`).toEqual([]);
				expect(row.overflowing, `${cfg.name} 行横向溢出：${row.text}`).toEqual([]);
			}
			expect(metrics.title.whiteSpace).toBe("nowrap");
			expect(metrics.title.lines).toBe(1);
			expect(metrics.title.height).toBeLessThan(30);
			expect(metrics.actionsVisible).toBe(true);
			expect(metrics.title.overlapsActions).toBe(false);
			expect(metrics.exportText.trim()).toBe(cfg.exportAllText);
		} finally {
			await wipeAnnotations(page, SID);
			await page.context().close();
		}
	});
}

test("单条导出：只下载该条 Feature，头部导出仍为全部", async ({ page }, testInfo) => {
	await page.addInitScript(() => {
		try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
	});
	await page.setViewportSize({ width: 1440, height: 900 });
	await loginByTyping(page, CREDS.userLogin, CREDS.userPassword);
	await wipeAnnotations(page, SID);

	const rect = { x: 60, y: 50, w: 80, h: 60 };
	const createdRect = await pageApi(page, "POST", "/api/annotation", {
		slide_id: SID, type: "rect", label: "e2e-single-rect", ...rect,
	});
	expect(createdRect.status, JSON.stringify(createdRect.j)).toBe(200);

	const points = [[200, 180], [360, 200], [300, 340]];
	const createdPoly = await pageApi(page, "POST", "/api/annotation", {
		slide_id: SID, type: "polygon", points, label: "e2e-single-polygon",
	});
	expect(createdPoly.status, JSON.stringify(createdPoly.j)).toBe(200);
	const polyId = (createdPoly.j as { annotation_id?: string }).annotation_id;
	expect(polyId).toBeTruthy();

	try {
		await openSlide(page, false);
		await openAnnoPanel(page, false);

		const polyRow = page.locator("#anno-panel .anno-item")
			.filter({ hasText: "e2e-single-polygon" });
		await expect(polyRow).toHaveCount(1);
		const exportBtn = polyRow.locator(".ai-export");
		await expect(exportBtn).toBeVisible();

		const oneDownload = page.waitForEvent("download");
		await exportBtn.click();
		const one = await oneDownload;
		expect(one.suggestedFilename()).toMatch(/\.geojson$/);
		expect(one.suggestedFilename()).toContain("e2e-single-polygon");

		const onePath = testInfo.outputPath("single-annotation.geojson");
		await one.saveAs(onePath);
		const parsedOne = JSON.parse(readFileSync(onePath, "utf8")) as {
			features: Array<{
				id?: string;
				geometry: { type: string; coordinates: unknown };
				properties: Record<string, unknown>;
			}>;
			pathtogether: Record<string, unknown>;
		};
		expect(parsedOne.features).toHaveLength(1);
		expect(parsedOne.features[0].id).toBe(polyId);
		expect(parsedOne.pathtogether.annotation_id).toBe(polyId);
		expect(parsedOne.pathtogether.feature_count).toBe(1);
		expect(parsedOne.features[0].geometry.type).toBe("Polygon");
		expect(parsedOne.features[0].geometry.coordinates).toEqual([
			[...points, [...points[0]]],
		]);

		const allDownload = page.waitForEvent("download");
		await page.locator("#anno-export-btn").click();
		const all = await allDownload;
		expect(all.suggestedFilename()).toContain("_annotations.geojson");
		const allPath = testInfo.outputPath("all-annotations.geojson");
		await all.saveAs(allPath);
		const parsedAll = JSON.parse(readFileSync(allPath, "utf8")) as {
			features: unknown[];
			pathtogether: Record<string, unknown>;
		};
		expect(parsedAll.features).toHaveLength(2);
		expect(parsedAll.pathtogether.feature_count).toBe(2);
		expect(parsedAll.pathtogether.annotation_id).toBeUndefined();
	} finally {
		await wipeAnnotations(page, SID);
	}
});
