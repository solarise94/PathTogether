/** Viewer 标注交互 e2e（design viewer-interaction-skills-review-20261009 §5.4/§6）。
 * 真实 Flask + 内嵌 PG（playwright.config.ts webServer；后端含 vi-lock 86148e5b）。
 *
 * 覆盖：
 *  - 未选中对象拖动 = 平移切片（零 PATCH）；单击选中 → 就近工具条；
 *  - 已选中对象拖动 = 一次 PATCH（geom + expected_revision，无 note 夹带；
 *    失败时「未保存 · 点击重试」轻量状态，§6.4）；
 *  - 编辑态拖右下角缩放（四角双轴；PATCH 载荷 w/h 变大）；
 *  - polygon 真实渲染（R3：基线不画）、命中选中、⋯ 编辑轮廓点
 *    （类型保留，序列化为 geom.points，顶点数不变）；
 *  - 位置锁真实后端闭环：token 路由上锁（200）→ 读取接口带 geometry_locked
 *    → 工具条「解锁位置」→ 锁定拖动=平移（零几何 PATCH）→ Delete 零 DELETE
 *    → 解锁请求单独发（geometry_locked，无 geom 键）；
 *  - 分享页：polygon 分享投影可见；只读标记拖动零 PATCH。
 *
 *  - by-id 写路径（松手保存、工具条锁定）在 id_bundle 切片上按 slide_id
 *    鉴权，200 后刷新保持几何与锁状态（文件尾用例）。
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

const EVIDENCE_DIR = process.env.ANNO_EVIDENCE_DIR ||
	".gate-tmp/vi-anno-evidence";

/** 页内 fetch（带 CSRF 头）。登录后的 cookie 含非 HttpOnly csrf_token。 */
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

/** 清空切片上当前用户可见的全部标注（画布内容 bbox 辅助函数假设
 * 「只有本用例创建的标记」；锁定的先经 token 路由解锁再删）。 */
async function wipeAnnotations(page: Page, sid: string) {
	const listing = await pageApi(page, "GET",
		`/api/annotations?slide_id=${encodeURIComponent(sid)}`);
	const items: Array<Record<string, unknown>> = [];
	((listing.j.annotations || []) as Array<{ items?: Array<Record<string, unknown>> }>)
		.forEach((grp) => (grp.items || []).forEach((it) => items.push(it)));
	for (const it of items) {
		const aid = it.annotation_id as string;
		const idx = it.index as number;
		const rev = Number(it.revision) > 0 ? Number(it.revision) : null;
		// 按稳定 annotation_id 解锁/删除：列表 index 与 token 路由的 index
		// 在存在 tombstone 时不一致（全量套件里前序用例会留下已删条目）
		let curRev = rev;
		if (aid && it.geometry_locked === true) {
			const un = await pageApi(page, "PATCH", `/api/annotation/id/${aid}`,
				{ geometry_locked: false, expected_revision: curRev });
			curRev = Number((un.j as { revision?: number }).revision) || curRev;
		}
		if (aid) {
			await pageApi(page, "DELETE", `/api/annotation/id/${aid}`,
				curRev != null ? { expected_revision: curRev } : undefined);
		} else {
			await pageApi(page, "DELETE", `/api/annotation/admin/${idx}`,
				rev != null ? { expected_revision: rev } : undefined);
		}
	}
	// 清点必须归零（静默失败会让画布 bbox 假设失效）
	await expect.poll(async () => {
		const after = await pageApi(page, "GET",
			`/api/annotations?slide_id=${encodeURIComponent(sid)}`);
		const n = ((after.j.annotations || []) as unknown[]).reduce(
			(a, g) => a + ((g as { items?: unknown[] }).items || []).length, 0);
		return n;
	}, { timeout: 10_000 }).toBe(0);
}

async function initSession(page: Page) {
	await page.addInitScript(() => {
		try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
	});
	await page.setViewportSize({ width: 1440, height: 900 });
}

async function login(page: Page, loginId: string, password: string) {
	await page.goto("/login");
	await page.fill('form[action="/login"] input[name="username"]', loginId);
	await page.fill('form[action="/login"] input[name="password"]', password);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.click('form[action="/login"] button[type="submit"]'),
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

/** 打开工作台切片并显示标记层（⋯ → 显示全部标记）。
 * 前置：调用方已完成登录（已登录访问 /login 会被重定向，不能二次 login）。 */
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

/** 等画布内容外接框稳定（OSD 动画结束 + 标签文字补绘），避免在动画帧上
 * 测量（动画期间标签不画，bbox 会差一个标签高度）。 */
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

async function markerCenter(page: Page): Promise<{ x: number; y: number }> {
	const bb = await annoCanvasBBox(page);
	expect(bb, "标注画布应有内容").toBeTruthy();
	return { x: bb!.cx + bb!.x + bb!.w / 2, y: bb!.cy + bb!.y + bb!.h / 2 };
}

/** 从列表响应解析 annotation 的 live index（create 响应的 index 含
 * tombstone 计数，与 token/index 路由的定位语义不一致）。 */
async function liveIndexOf(page: Page, sid: string, annoId: string): Promise<number> {
	const listing = await pageApi(page, "GET",
		`/api/annotations?slide_id=${encodeURIComponent(sid)}`);
	const items: Array<Record<string, unknown>> = [];
	((listing.j.annotations || []) as Array<{ items?: Array<Record<string, unknown>> }>)
		.forEach((grp) => (grp.items || []).forEach((it) => items.push(it)));
	const mine = items.find((it) => it.annotation_id === annoId);
	expect(mine, `标注 ${annoId} 应在列表中`).toBeTruthy();
	return mine!.index as number;
}

const annoPatchUrl = (url: URL) =>
	/^\/api\/annotation\/id\/[^/]+$/.test(url.pathname) ||
	/^\/s\/[^/]+\/api\/roi\/\d+$/.test(url.pathname);

test.describe("viewer 标注交互（§5.4/§6，真实后端）", () => {
	test("未选中拖动=平移切片（零 PATCH）；单击选中出工具条；选中拖动=一次差量 PATCH", async ({ page }) => {
		const SID = CREDS.rasterSlides.workbench.slide_id;
		await initSession(page);
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		const created = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", x: 60, y: 50, w: 80, h: 60,
			label: "e2e-move", shared: true,
		});
		expect(created.status, JSON.stringify(created.j)).toBe(200);

		await openSlideWithAnnos(page, SID);

		const patchBodies: Array<Record<string, unknown>> = [];
		page.on("request", (req) => {
			if (req.method() === "PATCH" && annoPatchUrl(new URL(req.url()))) {
				patchBodies.push(req.postDataJSON() as Record<string, unknown>);
			}
		});

		// --- 未选中：拖动 = 平移切片（标记随内容整体移动，零 PATCH） ---
		const before = await annoCanvasBBox(page);
		const canvasBox = await page.locator("#anno-canvas").boundingBox();
		const empty = { x: canvasBox!.x + 40, y: canvasBox!.y + canvasBox!.height - 40 };
		await page.mouse.move(empty.x, empty.y);
		await page.mouse.down();
		await page.mouse.move(empty.x + 60, empty.y - 20, { steps: 6 });
		await page.mouse.up();
		await page.waitForTimeout(500);
		expect(patchBodies, "未选中拖动不得发 PATCH").toEqual([]);
		const panned = await annoCanvasBBox(page);
		expect(panned).toBeTruthy();
		expect(Math.abs((panned!.x - before!.x) - 60),
			`平移位移 dx=${panned!.x - before!.x}`).toBeLessThanOrEqual(4);
		expect(Math.abs((panned!.y - before!.y) + 20),
			`平移位移 dy=${panned!.y - before!.y}`).toBeLessThanOrEqual(4);

		// --- 单击选中：就近工具条（编辑备注 / 锁定位置 / ⋯；单一蓝轮廓无黄角点） ---
		const c1 = await markerCenter(page);
		await page.mouse.click(c1.x, c1.y);
		const toolbar = page.locator("#anno-obj-toolbar");
		await expect(toolbar).toBeVisible();
		await expect(toolbar).toContainText("编辑备注");
		await expect(toolbar).toContainText("锁定位置");
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-selected-toolbar.png` });

		// --- 已选中：按住拖动 = 移动整个对象（一次 PATCH；差量、带 CAS） ---
		// 选中基线（含手柄范围）：与拖动后的量测同态，位移对比不受
		// 「选中态画柄/未选中态无柄」的外扩差影响
		const selBb = await annoCanvasBBox(page);
		// 首个 PATCH 模拟服务端失败，用于 §6.4「未保存 · 重试」路径
		let failNext = true;
		await page.route((url) => annoPatchUrl(url), async (route) => {
			if (route.request().method() === "PATCH" && failNext) {
				failNext = false;
				await route.fulfill({ status: 503, contentType: "application/json",
					body: JSON.stringify({ error: "unavailable" }) });
				return;
			}
			await route.continue();
		});
		const c2 = await markerCenter(page);
		await page.mouse.move(c2.x, c2.y);
		await page.mouse.down();
		await page.mouse.move(c2.x + 40, c2.y + 30, { steps: 8 });
		await page.mouse.up();
		await expect.poll(() => patchBodies.length,
			"松手必须恰好发一次几何 PATCH").toBe(1);
		expect(patchBodies[0]).toHaveProperty("geom");
		expect(patchBodies[0]).toHaveProperty("expected_revision");
		expect(patchBodies[0]).not.toHaveProperty("note"); // 差量：备注不夹带
		const moved = await annoCanvasBBox(page);
		expect(Math.abs((moved!.x - selBb!.x) - 40),
			`拖动位移 dx=${moved!.x - selBb!.x}`).toBeLessThanOrEqual(3);
		expect(Math.abs((moved!.y - selBb!.y) - 30),
			`拖动位移 dy=${moved!.y - selBb!.y}`).toBeLessThanOrEqual(3);

		// --- §6.4：保存失败时保留草稿并显示「未保存 · 点击重试」，不显示成功；
		//        重试走真实后端 200 ---
		const unsaved = page.locator(".anno-save-status.unsaved");
		await expect(unsaved).toBeVisible({ timeout: 10_000 });
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-unsaved-chip.png` });
		const [retry] = await Promise.all([
			page.waitForResponse((r) => r.request().method() === "PATCH"
				&& annoPatchUrl(new URL(r.url()))),
			unsaved.click(),
		]);
		expect(retry.status()).toBe(200);
		await expect(unsaved).toHaveCount(0);
	});

	test("⋯ 再点收起菜单；菜单删除后工具条消失（不残留可点的旧对象）", async ({ page }) => {
		const SID = CREDS.rasterSlides.workbench.slide_id;
		await initSession(page);
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		const created = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", x: 60, y: 50, w: 80, h: 60,
			label: "e2e-delete", shared: true,
		});
		expect(created.status, JSON.stringify(created.j)).toBe(200);
		await openSlideWithAnnos(page, SID);

		const c = await markerCenter(page);
		await page.mouse.click(c.x, c.y);
		const toolbar = page.locator("#anno-obj-toolbar");
		const more = toolbar.locator(".aot-more");
		const menu = toolbar.locator(".aot-menu");
		await expect(toolbar).toBeVisible();
		await more.click();
		await expect(menu).toBeVisible();
		await more.click();
		await expect(menu).toHaveCount(0);
		await more.click();
		await expect(menu).toBeVisible();

		const [del] = await Promise.all([
			page.waitForResponse((r) => r.request().method() === "DELETE"
				&& r.url().includes("/api/annotation/")),
			menu.locator(".aot-menu-item", { hasText: "删除" }).click(),
		]);
		expect(del.status()).toBe(200);
		await expect(toolbar).toBeHidden();
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-after-delete.png` });
		const after = await pageApi(page, "GET",
			`/api/annotations?slide_id=${encodeURIComponent(SID)}`);
		const live = ((after.j.annotations as Array<{ items: unknown[] }>) || [])
			.flatMap((g) => g.items);
		expect(live).toHaveLength(0);
	});

	test("选中后直接拖右下角缩放（§6.2 可直接拖控制点，无需先点编辑备注）", async ({ page }) => {
		const SID = CREDS.rasterSlides.workbench.slide_id;
		await initSession(page);
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		const created = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", x: 40, y: 40, w: 60, h: 50,
			label: "e2e-resize", shared: true,
		});
		expect(created.status, JSON.stringify(created.j)).toBe(200);

		await openSlideWithAnnos(page, SID);
		const patchBodies: Array<Record<string, unknown>> = [];
		page.on("request", (req) => {
			if (req.method() === "PATCH" && annoPatchUrl(new URL(req.url()))) {
				patchBodies.push(req.postDataJSON() as Record<string, unknown>);
			}
		});

		// 单击选中：手柄立即显示（§6.2/§6.1「只有真正可编辑时才显示调整柄」
		// ——选中即可拖控制点，「编辑备注」只是备注卡入口）
		const c = await markerCenter(page);
		await page.mouse.click(c.x, c.y);
		const toolbar = page.locator("#anno-obj-toolbar");
		await expect(toolbar).toBeVisible();
		await expect(toolbar).toContainText("编辑备注"); // 备注卡入口仍在
		await page.waitForTimeout(400); // 重绘选中轮廓 + 手柄

		// 直接拖右下角控制点（外接框右下角内 2px；不点「编辑备注」）
		const bb = await annoCanvasBBox(page);
		expect(bb).toBeTruthy();
		const hx = bb!.cx + bb!.x + bb!.w - 2;
		const hy = bb!.cy + bb!.y + bb!.h - 2;
		await page.mouse.move(hx, hy);
		await page.mouse.down();
		await page.mouse.move(hx + 45, hy + 35, { steps: 8 });
		await page.mouse.up();
		await expect.poll(() => patchBodies.length, "缩放松手发一次 PATCH").toBe(1);
		const geom = patchBodies[0].geom as { x: number; y: number; w: number; h: number };
		expect(geom.w).toBeGreaterThan(60);
		expect(geom.h).toBeGreaterThan(50);
		expect(geom.x).toBeLessThanOrEqual(40);
		expect(geom.y).toBeLessThanOrEqual(40);
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-resize-handle.png` });
	});

	test("polygon：真实渲染、命中选中、⋯ 编辑轮廓点（类型保留 points 序列化）", async ({ page }) => {
		const SID = CREDS.rasterSlides.workbench.slide_id;
		await initSession(page);
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		const created = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "polygon", points: [[40, 40], [170, 60], [90, 150]],
			label: "e2e-polygon", shared: true,
		});
		expect(created.status, JSON.stringify(created.j)).toBe(200);

		await openSlideWithAnnos(page, SID);
		// R3 回归：polygon 在原生标记层真实画出（基线 drawAnnoItem 无 polygon
		// 分支 → 画布全空）
		const bb = await waitForStableBBox(page);
		expect(bb, "polygon 必须被绘制").toBeTruthy();
		expect(bb!.w).toBeGreaterThan(20);
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-polygon-render.png` });

		const patchBodies: Array<Record<string, unknown>> = [];
		page.on("request", (req) => {
			if (req.method() === "PATCH" && annoPatchUrl(new URL(req.url()))) {
				patchBodies.push(req.postDataJSON() as Record<string, unknown>);
			}
		});

		// 单击内部选中（三角形 bbox 中心在形内）→ 工具条
		const c = await markerCenter(page);
		await page.mouse.click(c.x, c.y);
		await expect(page.locator("#anno-obj-toolbar")).toBeVisible();

		// ⋯ →「编辑轮廓点」→ 顶点柄出现
		await page.locator("#anno-obj-toolbar .aot-more").click();
		const menu = page.locator("#anno-obj-toolbar .aot-menu");
		await expect(menu).toBeVisible();
		await menu.locator(".aot-menu-item", { hasText: "编辑轮廓点" }).click();
		await expect(menu).toBeHidden();
		await page.waitForTimeout(300);
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-polygon-vertex-edit.png` });

		// 拖动 v0（图像 (40,40)）：标签画在 v0 正上方（框底 = v0.y - 2），
		// 稳定 bbox 顶 = v0.y - 18 → v0 屏幕 = (bb.x, bb.y + 18)
		const vx = bb!.cx + bb!.x + 2;
		const vy = bb!.cy + bb!.y + 18;
		await page.mouse.move(vx, vy);
		await page.mouse.down();
		await page.mouse.move(vx + 28, vy + 14, { steps: 6 });
		await page.mouse.up();
		await page.waitForTimeout(200);

		// ⋯ →「完成轮廓编辑」→ 一次 PATCH（geom.points；非自交校验通过）
		await page.locator("#anno-obj-toolbar .aot-more").click();
		await expect(menu).toBeVisible();
		await menu.locator(".aot-menu-item", { hasText: "完成轮廓编辑" }).click();
		await expect.poll(() => patchBodies.length, "完成轮廓编辑发一次 PATCH").toBe(1);
		const geom = patchBodies[0].geom as { points?: number[][] };
		expect(Array.isArray(geom.points)).toBe(true);
		expect(geom.points!.length).toBe(3); // 仍是 3 顶点 polygon（类型/点列保留）
		expect(geom.points![0]).not.toEqual([40, 40]); // v0 确实被拖动
		expect(geom.points![2]).toEqual([90, 150]);    // 其余顶点不动
	});

	test("位置锁（真实后端）：上锁 200 → 读取带锁 → 锁定拖动零 PATCH、Delete 零 DELETE → 解锁请求单独发", async ({ page }) => {
		const SID = CREDS.rasterSlides.workbench.slide_id;
		await initSession(page);
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		const created = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", x: 70, y: 60, w: 70, h: 50,
			label: "e2e-lock", shared: true,
		});
		expect(created.status, JSON.stringify(created.j)).toBe(200);
		const annoId = created.j.annotation_id as string;
		// 真实后端上锁（token 路由 200；by-id 路由受已知 id_bundle 名通道缺口
		// 影响见文件头，不影响锁语义本身的端到端验证）
		const idx = await liveIndexOf(page, SID, annoId);
		const locked = await pageApi(page, "PATCH", `/api/annotation/admin/${idx}`,
			{ geometry_locked: true, expected_revision: created.j.revision });
		expect(locked.status, JSON.stringify(locked.j)).toBe(200);
		expect(locked.j.geometry_locked).toBe(true);

		await openSlideWithAnnos(page, SID);

		// 读取接口真实返回锁状态（§6.3 契约：条目顶层 geometry_locked）
		const listing = await pageApi(page, "GET",
			`/api/annotations?slide_id=${encodeURIComponent(SID)}`);
		const items: Array<Record<string, unknown>> = [];
		((listing.j.annotations || []) as Array<{ items?: Array<Record<string, unknown>> }>)
			.forEach((grp) => (grp.items || []).forEach((it) => items.push(it)));
		const mine = items.find((it) => it.annotation_id === annoId);
		expect(mine, JSON.stringify(items)).toBeTruthy();
		expect(mine!.geometry_locked).toBe(true);

		const patchBodies: Array<Record<string, unknown>> = [];
		const deleteCount = { n: 0 };
		page.on("request", (req) => {
			const u = new URL(req.url());
			if (req.method() === "PATCH" && annoPatchUrl(u)) {
				patchBodies.push(req.postDataJSON() as Record<string, unknown>);
			}
			if (req.method() === "DELETE" && u.pathname.includes(annoId)) deleteCount.n++;
		});

		// 选中：工具条显示「解锁位置」（锁定态真实驱动 UI）
		const c = await markerCenter(page);
		await page.mouse.click(c.x, c.y);
		const toolbar = page.locator("#anno-obj-toolbar");
		await expect(toolbar).toBeVisible();
		await expect(toolbar).toContainText("解锁位置");
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-locked-toolbar.png` });

		// 锁定对象按住拖动 → 平移切片（零几何 PATCH，绝不偷偷移动对象）
		const before = await annoCanvasBBox(page);
		await page.mouse.move(c.x, c.y);
		await page.mouse.down();
		await page.mouse.move(c.x + 50, c.y + 20, { steps: 8 });
		await page.mouse.up();
		await page.waitForTimeout(500);
		expect(patchBodies.filter((b) => "geom" in b).length,
			"锁定对象拖动不得发几何 PATCH").toBe(0);
		const panned = await annoCanvasBBox(page);
		expect(Math.abs((panned!.x - before!.x) - 50),
			`锁定拖动应为平移 dx=${panned!.x - before!.x}`).toBeLessThanOrEqual(4);

		// Delete 键对锁定无效（零 DELETE 请求）
		const c2 = await markerCenter(page);
		await page.mouse.click(c2.x, c2.y);
		await expect(toolbar).toBeVisible();
		await page.keyboard.press("Delete");
		await page.waitForTimeout(400);
		expect(deleteCount.n, "锁定对象 Delete 不得发 DELETE").toBe(0);

		// 工具条「解锁位置」：单独 PATCH（只带 geometry_locked + CAS，无 geom 键）
		const c3 = await markerCenter(page);
		await page.mouse.click(c3.x, c3.y);
		await expect(toolbar).toBeVisible();
		await toolbar.locator(".aot-lock").click();
		await expect.poll(() => patchBodies.filter((b) => "geometry_locked" in b).length,
			"解锁必须发出锁切换请求").toBe(1);
		const lockPatch = patchBodies.find((b) => "geometry_locked" in b)!;
		expect(lockPatch).not.toHaveProperty("geom"); // 锁与几何绝不同请求
		expect(lockPatch).toHaveProperty("expected_revision");
	});

	test("分享页：polygon 分享投影可见；只读标记不可移动（零 PATCH）", async ({ page }) => {
		const { slide_id: SID, name: BMP_NAME } = CREDS.rasterSlides.share;
		await initSession(page);
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		const created = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "polygon", points: [[50, 60], [180, 80], [100, 170]],
			label: "e2e-share-poly", shared: true,
		});
		expect(created.status, JSON.stringify(created.j)).toBe(200);

		await page.goto(`/app?slide=${SID}`);
		await expect(page.locator("#canvas-slide-label")).toContainText(BMP_NAME);
		await expandSidebar(page);
		await page.locator("#tb-share-btn").click();
		await page.locator("#share-perm-annotate").check();
		const shareRequest = page.waitForRequest((req) =>
			req.method() === "POST" &&
			new URL(req.url()).pathname === "/api/share/create");
		await page.locator("#share-create-btn").click();
		await shareRequest;
		let shareUrl = "";
		await expect.poll(async () => {
			shareUrl = await page.locator("#share-result-url").inputValue();
			return shareUrl.length > 0;
		}, { timeout: 15_000 }).toBe(true);

		// 0056 语义：token=admin 上的 shared=true 不再自动对分享访客可见——
		// 经真实授权原语把该标注授予本分享链接（只读）
		const shareToken = new URL(shareUrl).pathname.split("/").filter(Boolean)[1];
		const granted = await pageApi(page, "PATCH",
			`/api/annotation/admin/${await liveIndexOf(page, SID, created.j.annotation_id as string)}`,
			{ grantee_kind: "share_token", grantee_id: shareToken });
		expect(granted.status, JSON.stringify(granted.j)).toBe(200);

		// 访客打开分享页：polygon 分享投影真实渲染（§5.4 分享投影）
		const pageErrors: string[] = [];
		page.on("pageerror", (e) => pageErrors.push(e.message));
		await page.goto(shareUrl);
		await expect(page.locator("#viewer .openseadragon-canvas"))
			.toBeVisible({ timeout: 20_000 });
		// 访客 rois 列表必须先看到这条共享 polygon（读路径）
		await expect.poll(async () => {
			const rois = await page.evaluate(async (t) => {
				const r = await fetch(`/s/${t}/api/rois`);
				return r.json();
			}, shareToken);
			return (rois as unknown[]).length;
		}, { timeout: 15_000 }).toBeGreaterThan(0);
		await expect.poll(() => annoCanvasBBox(page), { timeout: 15_000 })
			.not.toBeNull();
		expect(pageErrors, pageErrors.join("\n")).toEqual([]);
		await page.screenshot({ path: `${EVIDENCE_DIR}/e2e-share-polygon.png` });

		const patchBodies: string[] = [];
		page.on("request", (req) => {
			if (req.method() === "PATCH" && annoPatchUrl(new URL(req.url()))) {
				patchBodies.push(new URL(req.url()).pathname);
			}
		});

		// 单击选中：只读徽章（无编辑手柄）
		const c = await markerCenter(page);
		await page.mouse.click(c.x, c.y);
		const toolbar = page.locator("#anno-obj-toolbar");
		await expect(toolbar).toBeVisible();
		await expect(toolbar).toContainText("只读");

		// 只读对象拖动 = 平移切片（零 PATCH）
		await page.mouse.move(c.x, c.y);
		await page.mouse.down();
		await page.mouse.move(c.x + 45, c.y + 25, { steps: 8 });
		await page.mouse.up();
		await page.waitForTimeout(500);
		expect(patchBodies, "只读分享标记拖动不得发 PATCH").toEqual([]);
	});

	// ------------------------------------------------------------------
	// 工作台 by-id 写路径端到端：松手一次 PATCH、工具条锁定、刷新后保持。
	// ------------------------------------------------------------------
	test("松手 PATCH 200 + 刷新保持（by-id 写路径按 slide_id 鉴权）", async ({ page }) => {
		const SID = CREDS.rasterSlides.workbench.slide_id;
		await initSession(page);
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		const created = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", x: 60, y: 50, w: 80, h: 60, label: "e2e-persist",
		});
		const annoId = created.j.annotation_id as string;
		await openSlideWithAnnos(page, SID);
		const c1 = await markerCenter(page);
		await page.mouse.click(c1.x, c1.y);
		const c2 = await markerCenter(page);
		await page.mouse.move(c2.x, c2.y);
		await page.mouse.down();
		await page.mouse.move(c2.x + 40, c2.y + 30, { steps: 8 });
		await page.mouse.up();
		const resp = await page.waitForResponse((r) =>
			r.request().method() === "PATCH" && annoPatchUrl(new URL(r.url())));
		expect(resp.status()).toBe(200);
		const body = (resp.request().postDataJSON() as { geom: Record<string, number> }).geom;
		// 真实工具条「锁定位置」：单独请求、无 geom、带 CAS，服务端 200
		const lockBtn = page.locator("#anno-obj-toolbar .aot-lock");
		await expect(lockBtn).toBeVisible();
		const [lockResp] = await Promise.all([
			page.waitForResponse((r) => r.request().method() === "PATCH"
				&& annoPatchUrl(new URL(r.url()))
				&& "geometry_locked" in ((r.request().postDataJSON() || {}) as object)),
			lockBtn.click(),
		]);
		const lockReq = lockResp.request().postDataJSON() as Record<string, unknown>;
		expect(lockReq.geometry_locked).toBe(true);
		expect(lockReq).not.toHaveProperty("geom");
		expect(typeof lockReq.expected_revision).toBe("number");
		expect(lockResp.status()).toBe(200);
		await page.reload();
		await expandSidebar(page);
		await page.locator(`.fb-hit[data-slide-id="${SID}"]`).click();
		const replay = await pageApi(page, "GET",
			`/api/annotations?slide_id=${encodeURIComponent(SID)}`);
		const items: Array<Record<string, unknown>> = [];
		((replay.j.annotations || []) as Array<{ items?: Array<Record<string, unknown>> }>)
			.forEach((grp) => (grp.items || []).forEach((it) => items.push(it)));
		expect(items.find((it) => it.annotation_id === annoId))
			.toMatchObject({ ...body, geometry_locked: true });
	});
});
