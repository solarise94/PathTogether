/**
 * 矩形框选指针手势回归（docs/rect-draft-ai-context-opus-handoff-20261018.md §3.3）。
 *
 * 真实 Chromium + Flask + 内嵌 PostgreSQL；JPG（无物理标尺）全量场景，
 * 既有 BMP 夹具跑核心子集（格式无关性）。锁定行为：
 *   - 一次手势只归属一个指针；其他指针 / 右键中键不下场；
 *   - 正常松开：几何保留、悬停不跟随；自由画框恰好自动保存一次；
 *   - lostpointercapture / pointercancel / blur / buttons=0 悬停 = 取消恢复，
 *     绝不保存（初始画框被打断也不保存，恢复旧草稿或空状态）；
 *   - Escape 撤稿（焦点先移出输入框）后可立即重新框选；
 *   - 已保存标记编辑、平移/缩放/旋转、就近浮条点击互不抢事件。
 * 全部断言走真实浏览器事件与后端读写，不做源码正则断言。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8")) as {
	baseUrl: string;
	userLogin: string; userPassword: string;
	rasterSlides: Record<"workbench" | "share" | "rectDraftJpg", { slide_id: string; name: string }>;
};

interface Roi { x: number; y: number; w: number; h: number }

async function roiState(page: Page): Promise<Roi> {
	return page.evaluate(() => {
		const r = (window as any).HP_PROJECT_UI.viewerState.roi;
		return { x: r.x, y: r.y, w: r.w, h: r.h };
	});
}

/** 把焦点显式移出输入框：全局快捷键对输入框内按键不劫持（正确前提，非缺陷）。 */
async function blurActive(page: Page) {
	await page.evaluate(() => {
		const a = document.activeElement as HTMLElement | null;
		if (a && typeof a.blur === "function") a.blur();
	});
}

async function login(page: Page) {
	await page.addInitScript(() => {
		(window as any).__PT_TEST_HOOKS = true; // HP_PROJECT_UI 挂载点（仅测试）
		(window as any).__lastPointerId = null;
		document.addEventListener("pointerdown", (e) => {
			(window as any).__lastPointerId = e.pointerId;
		}, true);
	});
	await page.setViewportSize({ width: 1600, height: 1000 });
	await page.goto("/login");
	await page.fill('input[name="username"]', CREDS.userLogin);
	await page.fill('input[name="password"]', CREDS.userPassword);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.click('form[action="/login"] button[type="submit"]'),
	]);
}

/** 打开切片并激活矩形工具；无物理标尺切片单位区自动落 px（Wave 3）。 */
async function openRectTool(page: Page, slideId: string, titleNeedle: string) {
	const errors: string[] = [];
	page.on("pageerror", (e) => errors.push(e.message));
	await page.goto(`/app?slide=${slideId}`);
	await page.waitForFunction((n) => document.title.includes(n), titleNeedle);
	await page.locator("#roi-rect-btn").click();
	await expect(page.locator("#roi-settings")).toBeVisible();
	return errors;
}

/** 写入尺寸并触发 Enter 应用。不依赖 popover 可见性：点击画布后尺寸 popover
 * 会按既有语义自动收起（工具保持激活），真实 fill 会因不可交互而挂起。 */
async function setPresetInputs(page: Page, w: number, h: number) {
	await page.evaluate(({ w, h }) => {
		const wi = document.getElementById("roi-w-input") as HTMLInputElement;
		const hi = document.getElementById("roi-h-input") as HTMLInputElement;
		wi.value = String(w);
		hi.value = String(h);
		hi.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
	}, { w, h });
}

async function placePreset(page: Page, w = 100, h = 80): Promise<Roi> {
	await setPresetInputs(page, w, h);
	await expect(page.locator("#roi-box")).toBeVisible();
	return roiState(page);
}

function canvasBox(page: Page) {
	return page.locator("#anno-canvas").boundingBox() as Promise<{
		x: number; y: number; width: number; height: number;
	}>;
}

/** 预设点击放置（中心 = 画布相对坐标 u,v ∈ [0,1]）。 */
async function clickPlace(page: Page, u = 0.5, v = 0.5) {
	const cb = await canvasBox(page);
	await page.mouse.click(cb.x + cb.width * u, cb.y + cb.height * v);
	await expect(page.locator("#roi-box")).toBeVisible();
	return roiState(page);
}

async function dragBy(
	page: Page, from: { x: number; y: number }, dx: number, dy: number, steps = 8,
) {
	await page.mouse.move(from.x, from.y);
	await page.mouse.down();
	await page.mouse.move(from.x + dx, from.y + dy, { steps });
	await page.mouse.up();
}

function trackAnnotationPosts(page: Page): { posts: unknown[]; bodies: Array<Record<string, unknown>> } {
	const posts: unknown[] = [];
	const bodies: Array<Record<string, unknown>> = [];
	page.on("request", (r) => {
		if (r.method() === "POST" && new URL(r.url()).pathname === "/api/annotation") {
			posts.push(r.url());
			try { bodies.push(r.postDataJSON() as Record<string, unknown>); } catch { /* noop */ }
		}
	});
	return { posts, bodies };
}

async function savedAnnotation(page: Page, slideId: string, annotationId: string) {
	const listing = await page.request.get(`/api/annotations?slide_id=${slideId}`);
	const groups = (await listing.json()).annotations || [];
	return (groups as Array<{ items?: Array<Record<string, unknown>> }>)
		.flatMap((g) => g.items || [])
		.find((a) => a.annotation_id === annotationId);
}

/** 松开后悬停穿越框体、手柄与浮条按钮：几何必须一动不动。 */
async function hoverAcrossDraft(page: Page) {
	const bb = (await page.locator("#roi-box").boundingBox())!;
	const midX = bb.x + bb.width / 2;
	const midY = bb.y + bb.height / 2;
	await page.mouse.move(midX, midY);
	await page.mouse.move(bb.x + bb.width + 20, midY, { steps: 4 });
	await page.mouse.move(midX, bb.y - 30, { steps: 4 }); // 标签/浮条方向
	const toolbar = page.locator("#anno-obj-toolbar");
	if (await toolbar.isVisible()) {
		const tb = (await toolbar.boundingBox())!;
		await page.mouse.move(tb.x + tb.width / 2, tb.y + tb.height / 2, { steps: 3 });
	}
	await page.mouse.move(midX, midY, { steps: 3 });
}

const HANDLES = ["tl", "t", "tr", "l", "r", "bl", "b", "br"] as const;

for (const fmt of [
	{ key: "rectDraftJpg", needle: "e2e-rect-draft", full: true, label: "JPG（无MPP）" },
	{ key: "workbench", needle: "e2e-raster-workbench", full: false, label: "BMP" },
] as const) {
	test.describe(`矩形指针手势：${fmt.label}`, () => {
		let errors: string[];
		let posts: unknown[];
		let bodies: Array<Record<string, unknown>>;

		test.beforeEach(async ({ page }) => {
			await login(page);
			errors = await openRectTool(page, CREDS.rasterSlides[fmt.key].slide_id, fmt.needle);
			const tracked = trackAnnotationPosts(page);
			posts = tracked.posts;
			bodies = tracked.bodies;
		});

		test.afterEach(async () => {
			expect(errors, "无未允许的页面错误").toEqual([]);
		});

		test("框内移动：松开后悬停不跟随；保存后刷新几何一致", async ({ page }) => {
			await placePreset(page);
			const before = await roiState(page);
			const bb = (await page.locator("#roi-box").boundingBox())!;
			await dragBy(page, { x: bb.x + bb.width / 2, y: bb.y + bb.height / 2 }, 60, 40);
			const moved = await roiState(page);
			expect(moved).not.toEqual(before);
			await hoverAcrossDraft(page);
			expect(await roiState(page)).toEqual(moved);

			const resp = page.waitForResponse((r) =>
				new URL(r.url()).pathname === "/api/annotation" && r.request().method() === "POST");
			await page.locator("#anno-obj-toolbar .aot-save").click();
			const saved = await resp;
			expect(saved.status()).toBe(200);
			const { annotation_id } = await saved.json();
			expect(annotation_id).toBeTruthy();
			await page.reload();
			await page.waitForFunction((n) => document.title.includes(n), fmt.needle);
			await expect.poll(async () =>
				savedAnnotation(page, CREDS.rasterSlides[fmt.key].slide_id, annotation_id))
				.toMatchObject(moved);
		});

		test("四边 + 四角缩放：每个手柄改几何；松开后悬停不跟随", async ({ page }) => {
			await placePreset(page);
			for (const handle of HANDLES) {
				const start = await roiState(page); // 逐柄基线（上一柄已改几何）
				const hd = page.locator(`#roi-box [data-handle="${handle}"]`);
				const hb = (await hd.boundingBox())!;
				const horizontal = handle.includes("l") || handle.includes("r");
				const vertical = handle.includes("t") || handle.includes("b");
				await dragBy(page, { x: hb.x + hb.width / 2, y: hb.y + hb.height / 2 },
					horizontal ? (handle.includes("l") ? -30 : 30) : 0,
					vertical ? (handle.includes("t") ? -25 : 25) : 0, 6);
				const after = await roiState(page);
				// 对应轴尺寸必须变化（单轴边柄另一轴不变；角柄双轴变化）
				if (horizontal) expect(after.w).not.toBe(start.w);
				else expect(after.w).toBe(start.w);
				if (vertical) expect(after.h).not.toBe(start.h);
				else expect(after.h).toBe(start.h);
				await hoverAcrossDraft(page);
				expect(await roiState(page)).toEqual(after);
			}
		});

		test("移动中丢失捕获 → 框外松开 → 悬停不再移动（恢复拖前几何）", async ({ page }) => {
			await placePreset(page);
			const before = await roiState(page);
			const bb = (await page.locator("#roi-box").boundingBox())!;
			await page.mouse.move(bb.x + bb.width / 2, bb.y + bb.height / 2);
			await page.mouse.down();
			await page.mouse.move(bb.x + bb.width / 2 + 50, bb.y + bb.height / 2 + 30, { steps: 5 });
			// 人为释放真实捕获：后续 pointerup 落到框外元素，roi-box 收不到
			await page.locator("#roi-box").evaluate((el) =>
				(el as HTMLElement).releasePointerCapture((window as any).__lastPointerId));
			const cb = await canvasBox(page);
			await page.mouse.move(cb.x + 8, cb.y + 8, { steps: 3 });
			await page.mouse.up();
			await expect.poll(() => roiState(page), { timeout: 3000 }).toEqual(before);
			await hoverAcrossDraft(page);
			expect(await roiState(page)).toEqual(before);
			// 草稿仍在：可正常保存（取消 ≠ 丢草稿）
			const resp = page.waitForResponse((r) =>
				new URL(r.url()).pathname === "/api/annotation" && r.request().method() === "POST");
			await page.locator("#anno-obj-toolbar .aot-save").click();
			expect((await resp).status()).toBe(200);
			expect(posts.length).toBe(1);
		});

		if (fmt.full) {
			test("缩放中丢失捕获：同上恢复，悬停不改几何", async ({ page }) => {
				await placePreset(page);
				const before = await roiState(page);
				const hd = page.locator('#roi-box [data-handle="br"]');
				const hb = (await hd.boundingBox())!;
				await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
				await page.mouse.down();
				await page.mouse.move(hb.x + 40, hb.y + 30, { steps: 5 });
				await page.locator("#roi-box").evaluate((el) =>
					(el as HTMLElement).releasePointerCapture((window as any).__lastPointerId));
				const cb = await canvasBox(page);
				await page.mouse.move(cb.x + cb.width - 8, cb.y + 8, { steps: 3 });
				await page.mouse.up();
				await expect.poll(() => roiState(page), { timeout: 3000 }).toEqual(before);
				await hoverAcrossDraft(page);
				expect(await roiState(page)).toEqual(before);
			});

			test("释放事件缺失、下一帧 buttons=0：终止陈旧手势且不保存", async ({ page }) => {
				await placePreset(page);
				const before = await roiState(page);
				const bb = (await page.locator("#roi-box").boundingBox())!;
				await page.mouse.move(bb.x + bb.width / 2, bb.y + bb.height / 2);
				await page.mouse.down();
				await page.mouse.move(bb.x + bb.width / 2 + 30, bb.y + bb.height / 2 + 20, { steps: 4 });
				// pointerup 永远不来：下一 move 已无按键（真实释放发生在浏览器外）
				await page.locator("#roi-box").dispatchEvent("pointermove", {
					pointerId: (await page.evaluate(() => (window as any).__lastPointerId)) as number,
					pointerType: "mouse", buttons: 0, bubbles: true,
					clientX: bb.x + bb.width / 2 + 200, clientY: bb.y + bb.height / 2 + 150,
				});
				await expect.poll(() => roiState(page), { timeout: 3000 }).toEqual(before);
				// 之后的悬停与迟到 up 都不能再改几何，也不能触发保存
				await page.mouse.move(bb.x + 40, bb.y + 30, { steps: 3 });
				await page.mouse.up();
				await hoverAcrossDraft(page);
				expect(await roiState(page)).toEqual(before);
				expect(posts.length).toBe(0);
			});

			test("窗口失焦收尾：恢复拖前几何；回到窗口后新手势可用", async ({ page }) => {
				await placePreset(page);
				const before = await roiState(page);
				const bb = (await page.locator("#roi-box").boundingBox())!;
				await page.mouse.move(bb.x + bb.width / 2, bb.y + bb.height / 2);
				await page.mouse.down();
				await page.mouse.move(bb.x + bb.width / 2 + 45, bb.y + bb.height / 2 + 25, { steps: 5 });
				await page.evaluate(() => window.dispatchEvent(new Event("blur")));
				await expect.poll(() => roiState(page), { timeout: 3000 }).toEqual(before);
				await page.mouse.up(); // 失焦后的迟到释放不得改几何
				await hoverAcrossDraft(page);
				expect(await roiState(page)).toEqual(before);
				// 返回窗口：同一指针可重新完整拖动
				await dragBy(page, { x: bb.x + bb.width / 2, y: bb.y + bb.height / 2 }, 50, 30);
				expect(await roiState(page)).not.toEqual(before);
			});

			test("第二指针的 down/move/up 被忽略：几何只跟随主指针", async ({ page }) => {
				await placePreset(page);
				const before = await roiState(page);
				const bb = (await page.locator("#roi-box").boundingBox())!;
				const midX = bb.x + bb.width / 2, midY = bb.y + bb.height / 2;
				await page.mouse.move(midX, midY);
				await page.mouse.down();
				// 第二指（合成事件，pointerId=7）：按下/移动不得夺取或新开手势
				await page.locator("#roi-box").dispatchEvent("pointerdown", {
					pointerId: 7, pointerType: "touch", isPrimary: false, button: 0, buttons: 1,
					bubbles: true, clientX: midX + 300, clientY: midY + 200,
				});
				await page.locator("#roi-box").dispatchEvent("pointermove", {
					pointerId: 7, pointerType: "touch", isPrimary: false, buttons: 1,
					bubbles: true, clientX: midX + 320, clientY: midY + 240,
				});
				expect(await roiState(page)).toEqual(before);
				// 主指针继续有效
				await page.mouse.move(midX + 40, midY + 25, { steps: 4 });
				const byPrimary = await roiState(page);
				expect(byPrimary).not.toEqual(before);
				// 第二指的 up 不能替主指针结束手势
				await page.locator("#roi-box").dispatchEvent("pointerup", {
					pointerId: 7, pointerType: "touch", isPrimary: false, buttons: 0, bubbles: true,
					clientX: midX + 320, clientY: midY + 240,
				});
				await page.mouse.move(midX + 70, midY + 45, { steps: 4 });
				const more = await roiState(page);
				expect(more).not.toEqual(byPrimary);
				await page.mouse.up();
				await hoverAcrossDraft(page);
				expect(await roiState(page)).toEqual(more);
			});

			test("Escape 撤稿（焦点先移出输入框）后可立即重新框选；画框中 Escape 取消绘制", async ({ page }) => {
				await placePreset(page);
				const preset = await roiState(page);
				await blurActive(page);
				await page.keyboard.press("Escape");
				await expect(page.locator("#roi-box")).toHaveCount(0);
				expect(await roiState(page)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
				// 立即重新放置/绘制：新手势完整可用
				const again = await clickPlace(page);
				expect(again).toEqual(preset);
				// 画框中 Escape：取消恢复空状态，随后可再画
				await blurActive(page);
				await page.keyboard.press("Escape");
				await expect(page.locator("#roi-box")).toHaveCount(0);
				const cb = await canvasBox(page);
				await page.mouse.move(cb.x + cb.width * 0.3, cb.y + cb.height * 0.3);
				await page.mouse.down();
				await page.mouse.move(cb.x + cb.width * 0.5, cb.y + cb.height * 0.5, { steps: 5 });
				await expect(page.locator("#roi-box")).toBeVisible();
				await blurActive(page);
				await page.keyboard.press("Escape");
				await page.mouse.up();
				await expect(page.locator("#roi-box")).toHaveCount(0);
				expect(await roiState(page)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
				expect(posts.length).toBe(0);
				// 被打断的画框会把尺寸输入同步成最后一次绘制值——重新填入后再放置
				await setPresetInputs(page, 100, 80);
				const redrawn = await clickPlace(page);
				expect(redrawn).toEqual(preset);
			});

			test("初始画框捕获丢失：不保存；空状态恢复空、旧草稿恢复旧几何", async ({ page }) => {
				// 空状态：画框被打断 → 回空、零 POST
				const cb = await canvasBox(page);
				await page.mouse.move(cb.x + cb.width * 0.3, cb.y + cb.height * 0.4);
				await page.mouse.down();
				await page.mouse.move(cb.x + cb.width * 0.5, cb.y + cb.height * 0.6, { steps: 5 });
				await expect(page.locator("#roi-box")).toBeVisible();
				await page.locator("#anno-canvas").evaluate((el) =>
					(el as HTMLElement).releasePointerCapture((window as any).__lastPointerId));
				await page.mouse.move(cb.x + 6, cb.y + 6, { steps: 3 });
				await page.mouse.up();
				await expect(page.locator("#roi-box")).toHaveCount(0);
				expect(await roiState(page)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
				expect(posts.length).toBe(0);

				// 旧草稿在场：重新画框被打断 → 恢复旧草稿，不丢不存
				const preset = await placePreset(page);
				await page.mouse.move(cb.x + cb.width * 0.35, cb.y + cb.height * 0.35);
				await page.mouse.down();
				await page.mouse.move(cb.x + cb.width * 0.6, cb.y + cb.height * 0.65, { steps: 5 });
				await page.locator("#anno-canvas").evaluate((el) =>
					(el as HTMLElement).releasePointerCapture((window as any).__lastPointerId));
				await page.mouse.move(cb.x + cb.width - 6, cb.y + 6, { steps: 3 });
				await page.mouse.up();
				await expect.poll(() => roiState(page), { timeout: 3000 }).toEqual(preset);
				expect(posts.length).toBe(0);
				// 草稿仍可正常保存
				const resp = page.waitForResponse((r) =>
					new URL(r.url()).pathname === "/api/annotation" && r.request().method() === "POST");
				await page.locator("#anno-obj-toolbar .aot-save").click();
				expect((await resp).status()).toBe(200);
				expect(posts.length).toBe(1);
			});

			test("正常自由画框：松开恰好自动保存一次（计数 POST）", async ({ page }) => {
				const cb = await canvasBox(page);
				await page.mouse.move(cb.x + cb.width * 0.2, cb.y + cb.height * 0.2);
				await page.mouse.down();
				await page.mouse.move(cb.x + cb.width * 0.45, cb.y + cb.height * 0.5, { steps: 8 });
				await page.mouse.up();
				const drawn = await roiState(page);
				expect(drawn.w).toBeGreaterThan(0);
				await expect.poll(() => posts.length, { timeout: 5000 }).toBe(1);
				expect(bodies[0]).toMatchObject({ type: "rect" });
				await hoverAcrossDraft(page);
				expect(posts.length).toBe(1);
			});

			test("已保存标记编辑、平移/缩放/旋转、浮条点击互不抢事件", async ({ page }) => {
				// 保存一个标记（保存成功后应用自动按 annotation_id 选中它）
				await placePreset(page);
				const resp = page.waitForResponse((r) =>
					new URL(r.url()).pathname === "/api/annotation" && r.request().method() === "POST");
				await page.locator("#anno-obj-toolbar .aot-save").click();
				expect((await resp).status()).toBe(200);
				const cb = await canvasBox(page);
				const cx = cb.x + cb.width / 2, cy = cb.y + cb.height / 2;
				await expect(page.locator("#anno-obj-toolbar")).toBeVisible(); // 已保存标记的浮条

				// 拖动已保存标记 = 一次编辑 PATCH（松手后悬停不再跟随）
				const patches: string[] = [];
				page.on("request", (r) => {
					if (r.method() === "PATCH" && /^\/api\/annotation\/id\//.test(new URL(r.url()).pathname)) {
						patches.push(r.url());
					}
				});
				await dragBy(page, { x: cx, y: cy }, 50, 30);
				await expect.poll(() => patches.length, { timeout: 5000 }).toBe(1);
				await page.mouse.move(cx + 200, cy + 150, { steps: 4 }); // 悬停不改已保存几何

				// 平移/缩放/旋转后，框选工具照常工作（事件不互抢）
				await page.mouse.move(cx + 300, cy + 20);
				await page.mouse.wheel(0, -240); // 缩放
				await page.locator("#rotate-btn").click(); // 旋转
				await page.locator("#roi-rect-btn").click(); // 矩形工具
				await expect(page.locator("#roi-settings")).toBeVisible();
				const placed = await clickPlace(page, 0.4, 0.4);
				expect(placed.w).toBe(100);
				const bb = (await page.locator("#roi-box").boundingBox())!;
				await dragBy(page, { x: bb.x + bb.width / 2, y: bb.y + bb.height / 2 }, 40, 20);
				const moved = await roiState(page);
				expect(moved).not.toEqual(placed);
				// 浮条菜单开合不抢草稿几何；保存可达（点保存同时收起菜单）
				await page.locator("#anno-obj-toolbar .aot-more").click();
				await expect(page.locator("#anno-obj-toolbar .aot-menu")).toBeVisible();
				const resp2 = page.waitForResponse((r) =>
					new URL(r.url()).pathname === "/api/annotation" && r.request().method() === "POST");
				await page.locator("#anno-obj-toolbar .aot-save").click();
				expect((await resp2).status()).toBe(200);
				const listing = await page.request.get("/api/annotations?slide_id=" + CREDS.rasterSlides[fmt.key].slide_id);
				const items = ((await listing.json()).annotations || [])
					.flatMap((g: { items?: Array<Record<string, unknown>> }) => g.items || []);
				expect(items.length).toBeGreaterThanOrEqual(2);
			});
		}
	});
}
