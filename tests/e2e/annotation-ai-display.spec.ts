/** 查看器 AI 显示规则 e2e（2026-10-19 存量人工标注回归，真实 Flask + 内嵌 PG）。
 *
 * 2026-10-19 以前工作台把用户自己画的人工标注落库成 source="ai" /
 * review_status="pending"（add_roi 按 admin token 推断），但没有 AI 会话与
 * provenance。服务端 annotation_access.is_ai_generated 只认「source=ai 且带
 * 会话/溯源」为真 AI，并在列表条目投影 author_kind；导出已按该口径把这些行
 * 导成人工，查看器改为同一判定（itemIsAi）。
 *
 * e2e 无法造出无会话的 source=ai 存量行（/api/annotation 已显式写
 * source=human，真 AI 需要 internal/plugin 通道），因此这里用生产同形数据测
 * 客户端显示规则：造两条真实人工矩形，拦截列表响应把两条都改成
 * source=ai/review_status=pending；`e2e-historical-human` 保留服务端给的
 * author_kind（应为 user），`e2e-real-ai` 改成 author_kind=ai。
 *
 * 断言：
 *  - historical 行没有「接受」「驳回」按钮与审核徽章；real-ai 行两者都有；
 *  - 选中 historical 行：工具条无「✓ 接受」、⋯ 菜单无「驳回」；
 *    选中 real-ai 行：工具条含「✓ 接受」；
 *  - 画布配色：real-ai 矩形内部是 AI 橙色填充（rgba(255,149,0,0.16)），
 *    historical 内部不是该橙色调（其 label 色为蓝紫系）。
 *
 * 登录一律键盘逐字输入（不用 fill）；凭据只来自 E2E_CREDS_FILE，不打印。
 */
import { expect, test, type Page } from "@playwright/test";
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
const HIST_LABEL = "e2e-historical-human";
const AI_LABEL = "e2e-real-ai";

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

/** 打开工作台切片并显示标记层（⋯ → 显示全部标记），等画布内容稳定。 */
async function openSlideWithAnnos(page: Page, sid: string) {
	await page.goto(`/app?slide=${sid}`);
	await expandSidebar(page);
	const row = page.locator(`.fb-hit[data-slide-id="${sid}"]`);
	await expect(row).toBeVisible();
	await row.click();
	await expect(page.locator("#viewer .openseadragon-canvas"))
		.toBeVisible({ timeout: 20_000 });
	await expect.poll(() => page.evaluate(() => {
		const base = document.querySelector("#viewer .openseadragon-canvas canvas") as
			HTMLCanvasElement | null;
		if (!base || !base.width) return false;
		const ctx = base.getContext("2d");
		if (!ctx) return false;
		const d = ctx.getImageData(0, 0, base.width, base.height).data;
		for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true;
		return false;
	}), { timeout: 20_000 }).toBe(true);
	await page.locator("#tbb-more-btn").click();
	const annoAllBtn = page.locator("#anno-all-btn");
	await expect(annoAllBtn).toBeEnabled({ timeout: 15_000 });
	await annoAllBtn.click();
	await expect.poll(() => annoCanvasBBox(page), { timeout: 15_000 }).not.toBeNull();
}

/** 标注画布内容外接框（CSS px；device 像素归一）。无内容返回 null。 */
async function annoCanvasBBox(page: Page): Promise<
	{ x: number; y: number; w: number; h: number; cx: number; cy: number } | null> {
	return page.evaluate(() => {
		const c = document.querySelector("#anno-canvas") as HTMLCanvasElement | null;
		if (!c || !c.width) return null;
		const ctx = c.getContext("2d");
		if (!ctx) return null;
		const d = ctx.getImageData(0, 0, c.width, c.height).data;
		let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
		for (let y = 0; y < c.height; y++) {
			for (let x = 0; x < c.width; x++) {
				if (d[(y * c.width + x) * 4 + 3] > 0) {
					if (x < minx) minx = x;
					if (y < miny) miny = y;
					if (x > maxx) maxx = x;
					if (y > maxy) maxy = y;
				}
			}
		}
		if (minx === Infinity) return null;
		const dpr = c.width / c.clientWidth;
		return {
			x: minx / dpr, y: miny / dpr,
			w: (maxx - minx) / dpr, h: (maxy - miny) / dpr,
			cx: c.getBoundingClientRect().left,
			cy: c.getBoundingClientRect().top,
		};
	});
}

/** 等画布内容外接框稳定（OSD 动画结束 + 标签文字补绘）。 */
async function waitForStableBBox(page: Page): Promise<
	NonNullable<Awaited<ReturnType<typeof annoCanvasBBox>>> > {
	let prev = await annoCanvasBBox(page);
	expect(prev, "标注画布应有内容").toBeTruthy();
	for (let i = 0; i < 40; i++) {
		await page.waitForTimeout(300);
		const cur = await annoCanvasBBox(page);
		if (prev && cur && Math.abs(cur.x - prev.x) < 0.5 &&
			Math.abs(cur.y - prev.y) < 0.5 && Math.abs(cur.w - prev.w) < 0.5 &&
			Math.abs(cur.h - prev.h) < 0.5) {
			return cur!;
		}
		prev = cur;
	}
	return prev!;
}

interface FillCluster {
	count: number;
	cx: number;
	cy: number;
	rgb: [number, number, number, number];
}

interface FillScan {
	orange: FillCluster | null;
	bluePurple: FillCluster | null;
	dpr: number;
}

/**
 * 画布填充色聚类（device 像素）：矩形内部是纯半透明填充，alpha 落在
 * 20..80。AI 填充 = rgba(255,149,0,0.16)；人工填充 = hsla(hue,70%,55%,0.18)
 * （本 spec 两个 label 的哈希色为蓝紫系）。分别取两类像素的质心并读取样本色。
 */
async function scanAnnotationFills(page: Page): Promise<FillScan | null> {
	return page.evaluate(() => {
		const c = document.querySelector("#anno-canvas") as HTMLCanvasElement | null;
		if (!c || !c.width) return null;
		const ctx = c.getContext("2d");
		if (!ctx) return null;
		const d = ctx.getImageData(0, 0, c.width, c.height).data;
		const acc = {
			orange: { n: 0, sx: 0, sy: 0 },
			bluePurple: { n: 0, sx: 0, sy: 0 },
		};
		for (let y = 0; y < c.height; y++) {
			for (let x = 0; x < c.width; x++) {
				const i = (y * c.width + x) * 4;
				const r = d[i], g = d[i + 1], b = d[i + 2], a = d[i + 3];
				if (a < 20 || a > 80) continue;           // 只看半透明填充
				if (r >= 235 && g >= 120 && g <= 185 && b <= 30) {
					acc.orange.n++; acc.orange.sx += x; acc.orange.sy += y;
				} else if (b >= 170 && b > r + 40 && r > g && g < 110) {
					acc.bluePurple.n++; acc.bluePurple.sx += x; acc.bluePurple.sy += y;
				}
			}
		}
		const dump = (a: { n: number; sx: number; sy: number }) => {
			if (!a.n) return null;
			const x = Math.round(a.sx / a.n), y = Math.round(a.sy / a.n);
			const i = (y * c.width + x) * 4;
			return {
				count: a.n, cx: x, cy: y,
				rgb: [d[i], d[i + 1], d[i + 2], d[i + 3]] as [number, number, number, number],
			};
		};
		return {
			orange: dump(acc.orange),
			bluePurple: dump(acc.bluePurple),
			dpr: c.width / c.clientWidth,
		};
	});
}

test("存量 source=ai 人工行（author_kind=user）不显示 AI 审核 UI/配色；真 AI 行照常", async ({ page }) => {
	test.setTimeout(120_000);
	await page.addInitScript(() => {
		try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
	});
	await page.setViewportSize({ width: 1440, height: 900 });
	await loginByTyping(page, CREDS.userLogin, CREDS.userPassword);
	await wipeAnnotations(page, SID);

	const created = [
		{ type: "rect", label: HIST_LABEL, x: 20, y: 20, w: 80, h: 60 },
		{ type: "rect", label: AI_LABEL, x: 180, y: 120, w: 100, h: 80 },
	];
	for (const body of created) {
		const r = await pageApi(page, "POST", "/api/annotation",
			{ slide_id: SID, ...body });
		expect(r.status, JSON.stringify(r.j)).toBe(200);
	}

	// 生产同形数据：e2e 造不出无会话的 source=ai 存量行，用拦截改写列表
	// （见文件头注释）。记录服务端真实 author_kind 供断言。
	const serverFacts: Record<string, { source?: string; author_kind?: string }> = {};
	await page.route("**/api/annotations?slide_id=*", async (route) => {
		const resp = await route.fetch();
		const j = await resp.json() as {
			annotations?: Array<{ label?: string; items?: Array<Record<string, unknown>> }>;
		};
		for (const grp of j.annotations || []) {
			for (const it of grp.items || []) {
				const label = String(grp.label || "");
				if (label !== HIST_LABEL && label !== AI_LABEL) continue;
				serverFacts[label] = {
					source: it.source as string | undefined,
					author_kind: it.author_kind as string | undefined,
				};
				it.source = "ai";
				it.review_status = "pending";
				if (label === AI_LABEL) {
					it.author_kind = "ai";
					it.author_key = "ai";
					it.author_label_safe = "AI";
				}
			}
		}
		await route.fulfill({ response: resp, json: j });
	});

	try {
		await openSlideWithAnnos(page, SID);
		await waitForStableBBox(page);

		// --- 画布配色（选中前采样：选中会改画蓝色轮廓/填充） ---
		const fills = await scanAnnotationFills(page);
		expect(fills, "标注画布应有内容").toBeTruthy();
		const orange = fills!.orange;
		const bluePurple = fills!.bluePurple;
		expect(orange?.count || 0, "real-ai 矩形应有 AI 橙色填充")
			.toBeGreaterThan(500);
		expect(bluePurple?.count || 0, "historical 矩形应有非橙色（蓝紫）填充")
			.toBeGreaterThan(500);
		// real-ai 内部样本 = AI 填充色 rgba(255,149,0,0.16)
		expect(orange!.rgb[0]).toBeGreaterThanOrEqual(240);
		expect(orange!.rgb[1]).toBeGreaterThanOrEqual(125);
		expect(orange!.rgb[1]).toBeLessThanOrEqual(180);
		expect(orange!.rgb[2]).toBeLessThanOrEqual(30);
		expect(orange!.rgb[3]).toBeGreaterThanOrEqual(25);
		expect(orange!.rgb[3]).toBeLessThanOrEqual(60);
		// historical 内部样本不是该橙色调（蓝分量占优）
		expect(bluePurple!.rgb[2]).toBeGreaterThan(bluePurple!.rgb[0]);
		expect(bluePurple!.rgb[2]).toBeGreaterThan(bluePurple!.rgb[1]);
		expect(bluePurple!.rgb[0]).toBeLessThanOrEqual(180);

		// --- 面板行审核 UI ---
		await page.locator("#anno-btn").click();
		await expect(page.locator("#anno-panel")).toBeVisible();
		const histRow = page.locator("#anno-panel .anno-item").filter({
			has: page.locator(".ai-label", { hasText: HIST_LABEL }),
		});
		const aiRow = page.locator("#anno-panel .anno-item").filter({
			has: page.locator(".ai-label", { hasText: AI_LABEL }),
		});
		await expect(histRow).toHaveCount(1);
		await expect(aiRow).toHaveCount(1);
		await expect(histRow.locator(".ai-review-accept")).toHaveCount(0);
		await expect(histRow.locator(".ai-review-reject")).toHaveCount(0);
		await expect(histRow.locator(".anno-review-badge")).toHaveCount(0);
		await expect(aiRow.locator(".ai-review-accept")).toHaveCount(1);
		await expect(aiRow.locator(".ai-review-reject")).toHaveCount(1);

		// 服务端给的真实人工行应为 source=human、author_kind=user
		expect(serverFacts[HIST_LABEL]?.source).toBe("human");
		expect(serverFacts[HIST_LABEL]?.author_kind).toBe("user");

		// --- 选中 historical 行：工具条无「接受」，⋯ 菜单无「驳回」 ---
		const toolbar = page.locator("#anno-obj-toolbar");
		await histRow.locator(".ai-label").click();
		await expect(toolbar).toBeVisible();
		await expect(toolbar.locator(".aot-accept")).toHaveCount(0);
		await toolbar.locator(".aot-more").click();
		const menu = toolbar.locator(".aot-menu");
		await expect(menu).toBeVisible();
		await expect(menu.locator(".aot-menu-item", { hasText: "驳回" })).toHaveCount(0);

		// --- 选中 real-ai 行：工具条含「✓ 接受」 ---
		await aiRow.locator(".ai-label").click();
		await expect(toolbar).toBeVisible();
		await expect(toolbar.locator(".aot-accept")).toBeVisible();
	} finally {
		await wipeAnnotations(page, SID);
	}
});
