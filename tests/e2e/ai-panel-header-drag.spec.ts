/** AI 标题整栏拖动（Viewer 交互升级 §3）——真实 Flask + PG + 已发布
 * HistoPilot bundle（integrations/pathtogether 装入 plugins/histopilot）。
 *
 * 覆盖（§8 标题栏行）：
 *   - 从标题、空白（grip/header padding）、新对话、⋯、关闭按钮起拖：
 *     面板移动且拖完不执行按钮动作（不开弹层、不关面板、不新建会话）；
 *   - 未超过阈值（~6 CSS px）的按下-松开 = 普通点击，各按钮功能照常；
 *   - Esc 取消进行中的拖动：回到起点，随后的点击不受抑制；
 *   - 拖动位置持久化：刷新后（重开面板）位置保持；
 *   - ⋯ →「重置面板位置」：清偏好、恢复默认锚定（不再占用标题双击）；
 *   - ≤768px：固定面板，触摸/拖动手势不移动面板。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as {
	userLogin: string; userPassword: string;
	rasterSlides: Record<"workbench" | "share", { slide_id: string; name: string }>;
};

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
	await expect(menuBtn).toHaveAttribute("aria-expanded", /^(true|false)$/);
	if ((await menuBtn.getAttribute("aria-expanded")) !== "true") {
		await menuBtn.click();
		await expect(menuBtn).toHaveAttribute("aria-expanded", "true", { timeout: 5_000 });
	}
}

async function openSlideRow(page: Page, sid: string) {
	const hit = page.locator(`.fb-hit[data-slide-id="${sid}"]`);
	await expect(page.locator(".fb-folder, .fb-hit").first()).toBeVisible();
	for (let n = 0; n < 20 && !await hit.count(); n++) {
		const next = page.locator("#fb-next-btn");
		if (await next.isDisabled()) break;
		await next.click();
	}
	await hit.click();
}

const SID = CREDS.rasterSlides.workbench.slide_id;

/** 打开 AI 面板（需要切片已开：ai-btn 打开后由 onViewerOpen 启用） */
async function openAIPanel(page: Page, sid: string) {
	await login(page, CREDS.userLogin, CREDS.userPassword);
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.addInitScript(() => {
		try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
	});
	await page.goto("/app");
	await expandSidebar(page);
	await openSlideRow(page, SID);
	await expect(page.locator("#ai-btn")).toBeEnabled({ timeout: 20_000 });
	await page.locator("#ai-btn").click();
	await expect(page.locator("#ai-panel")).toBeVisible();
	return page.locator("#ai-panel");
}

/**
 * 面板被钳回 #viewer-wrap 内（clampAIPanelBox）：期望位置 = 起点位移后夹进
 * 视框。这里按同一几何计算钳制后的期望值，避免断言被边界钳制误判。
 */
async function clampedTarget(page: Page, before: { x: number; y: number }, dx: number, dy: number) {
	return page.evaluate(({ bx, by, dx, dy }) => {
		const panel = document.getElementById("ai-panel")!;
		const frame = document.getElementById("viewer-wrap")!;
		const f = frame.getBoundingClientRect();
		const w = panel.offsetWidth;
		const h = panel.offsetHeight;
		const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), Math.max(lo, hi));
		return {
			x: clamp(bx + dx, f.left, f.left + f.width - w),
			y: clamp(by + dy, f.top, f.top + f.height - h),
		};
	}, { bx: before.x, by: before.y, dx, dy });
}

async function panelBox(page: Page) {
	return (await page.locator("#ai-panel").boundingBox())!;
}

/** 从 (sx,sy) 拖到 (+dx,+dy)：pointerdown → 多步 pointermove → pointerup */
async function dragMouse(page: Page, sx: number, sy: number, dx: number, dy: number, steps = 10) {
	await page.mouse.move(sx, sy);
	await page.mouse.down();
	for (let i = 1; i <= steps; i++) {
		await page.mouse.move(sx + dx * i / steps, sy + dy * i / steps);
	}
	await page.mouse.up();
}

async function dragFrom(page: Page, origin: { x: number; y: number }, dx: number, dy: number) {
	await dragMouse(page, origin.x, origin.y, dx, dy);
}

/** 统计某按钮收到的原生 click 次数（验证「拖完不执行按钮动作」） */
async function countClicksOn(page: Page, selector: string) {
	await page.evaluate((sel) => {
		const w = window as unknown as Record<string, number>;
		w.__ptClicks = w.__ptClicks || {};
		w.__ptClicks[sel] = 0;
		document.getElementById(sel.replace(/^#/, ""))?.addEventListener("click", () => {
			(window as unknown as Record<string, number>).__ptClicks[sel]! += 1;
		});
	}, selector);
	return () => page.evaluate((sel) =>
		((window as unknown as Record<string, Record<string, number>>).__ptClicks || {})[sel] ?? -1, selector);
}

async function savedPrefs(page: Page): Promise<Record<string, string>> {
	return page.evaluate(() => {
		const out: Record<string, string> = {};
		for (let i = 0; i < localStorage.length; i++) {
			const k = localStorage.key(i)!;
			if (k.startsWith("pt.aip.v1|")) out[k] = localStorage.getItem(k)!;
		}
		return out;
	});
}

test.describe("AI 标题整栏拖动（§3）", () => {

	test("从标题按钮起拖：面板移动、会话列表弹层不开、位置持久化", async ({ page }) => {
		const panel = await openAIPanel(page, SID);
		const before = await panelBox(page);
		const toggle = await page.locator("#ai-conversation-toggle").boundingBox();
		await dragFrom(page, { x: toggle!.x + 30, y: toggle!.y + toggle!.height / 2 }, -180, 90);
		const target = await clampedTarget(page, before!, -180, 90);
		const after = await panelBox(page);
		expect(after!.x).toBeCloseTo(target.x, 0);
		expect(after!.y).toBeCloseTo(target.y, 0);
		// 拖完不执行按钮动作：标题的会话列表不被打开
		await expect(page.locator("#ai-conversations")).toBeHidden();
		// 位置持久化：刷新后重开面板位置保持（§3.2.4/§8）
		const prefs = await savedPrefs(page);
		expect(Object.keys(prefs).length).toBe(1);
		const frameLeft = await page.evaluate(() =>
			document.getElementById("viewer-wrap")!.getBoundingClientRect().left);
		const saved = JSON.parse(Object.values(prefs)[0]!) as { left: number; top: number };
		expect(saved.left).toBeCloseTo(after!.x - frameLeft, 0);
		await page.reload();
		await expandSidebar(page);
		await openSlideRow(page, SID);
		await expect(page.locator("#ai-btn")).toBeEnabled({ timeout: 20_000 });
		await page.locator("#ai-btn").click();
		await expect(panel).toBeVisible();
		const reopened = await panelBox(page);
		expect(reopened!.x).toBeCloseTo(after!.x, 0);
		expect(reopened!.y).toBeCloseTo(after!.y, 0);
	});

	test("阈值以下点击保持原功能：标题开列表、⋯ 开选项、关闭关面板", async ({ page }) => {
		const panel = await openAIPanel(page, SID);
		const before = await panelBox(page);
		// 标题：普通点击打开会话列表
		await page.locator("#ai-conversation-toggle").click();
		await expect(page.locator("#ai-conversations")).toBeVisible();
		// Esc 关闭弹层后测 ⋯
		await page.keyboard.press("Escape");
		await expect(page.locator("#ai-conversations")).toBeHidden();
		await page.locator("#ai-options-toggle").click();
		await expect(page.locator("#ai-options")).toBeVisible();
		// 面板没被这些小位移点按移动
		const afterClicks = await panelBox(page);
		expect(afterClicks!.x).toBeCloseTo(before!.x, 0);
		expect(afterClicks!.y).toBeCloseTo(before!.y, 0);
	});

	test("从空白 header（grip/顶部 padding）起拖：面板移动", async ({ page }) => {
		const panel = await openAIPanel(page, SID);
		const before = await panelBox(page);
		const header = await page.locator("#ai-panel-header").boundingBox();
		// grip（✦ span，非按钮）与 header 上 padding 都属于起拖区
		await dragFrom(page, { x: header!.x + 8, y: header!.y + 3 }, -140, -40);
		const target = await clampedTarget(page, before!, -140, -40);
		const after = await panelBox(page);
		expect(after!.x).toBeCloseTo(target.x, 0);
		expect(after!.y).toBeCloseTo(target.y, 0);
	});

	test("从新对话/⋯/关闭按钮起拖：拖完不执行按钮动作", async ({ page }) => {
		const panel = await openAIPanel(page, SID);
		const before = await panelBox(page);
		const newCount = await countClicksOn(page, "#ai-new-conversation");
		const moreCount = await countClicksOn(page, "#ai-options-toggle");
		const closeCount = await countClicksOn(page, "#ai-panel-close");

		// 新对话按钮起拖（按钮启用态）：面板移动，但不新建会话
		const newBox = await page.locator("#ai-new-conversation").boundingBox();
		await dragFrom(page, { x: newBox!.x + newBox!.width / 2, y: newBox!.y + newBox!.height / 2 }, -140, 60);
		expect(await newCount()).toBe(0);
		// ⋯ 按钮起拖：选项弹层不开
		const moreBox = await page.locator("#ai-options-toggle").boundingBox();
		await dragFrom(page, { x: moreBox!.x + moreBox!.width / 2, y: moreBox!.y + moreBox!.height / 2 }, -100, 80);
		expect(await moreCount()).toBe(0);
		await expect(page.locator("#ai-options")).toBeHidden();
		// 关闭按钮起拖：面板不关（拖完突然关闭是 §3.1 明确禁止的回归）
		const closeBox = await page.locator("#ai-panel-close").boundingBox();
		await dragFrom(page, { x: closeBox!.x + closeBox!.width / 2, y: closeBox!.y + closeBox!.height / 2 }, -160, -40);
		expect(await closeCount()).toBe(0);
		await expect(panel).toBeVisible();

		const after = await panelBox(page);
		expect(after!.x).not.toBeCloseTo(before!.x, 0);
		// 拖动释放后按钮照常可点（抑制只属于那次手势）：关闭面板
		await page.locator("#ai-panel-close").click();
		await expect(panel).toBeHidden();
	});

	test("Esc 取消拖动：回到起点、不写偏好，随后点击不受抑制", async ({ page }) => {
		const panel = await openAIPanel(page, SID);
		const before = await panelBox(page);
		const toggle = await page.locator("#ai-conversation-toggle").boundingBox();
		await page.mouse.move(toggle!.x + 30, toggle!.y + toggle!.height / 2);
		await page.mouse.down();
		await page.mouse.move(toggle!.x + 30 - 120, toggle!.y + toggle!.height / 2 + 60, { steps: 6 });
		await page.keyboard.press("Escape");
		await page.mouse.up();
		const after = await panelBox(page);
		expect(after!.x).toBeCloseTo(before!.x, 0);
		expect(after!.y).toBeCloseTo(before!.y, 0);
		expect(Object.keys(await savedPrefs(page))).toHaveLength(0);
		// 抑制只属于被取消的手势：随后的普通点击照常
		await page.locator("#ai-conversation-toggle").click();
		await expect(page.locator("#ai-conversations")).toBeVisible();
	});

	test("⋯ →「重置面板位置」：恢复默认锚定并清除偏好", async ({ page }) => {
		const panel = await openAIPanel(page, SID);
		// 先拖离默认位置
		const toggle = await page.locator("#ai-conversation-toggle").boundingBox();
		await dragFrom(page, { x: toggle!.x + 30, y: toggle!.y + toggle!.height / 2 }, -200, 120);
		expect(Object.keys(await savedPrefs(page))).toHaveLength(1);
		// ⋯ → 重置面板位置
		await page.locator("#ai-options-toggle").click();
		await expect(page.locator("#ai-options")).toBeVisible();
		await expect(page.locator("#ai-reset-position")).toContainText("重置面板位置");
		await page.locator("#ai-reset-position").click();
		await expect(page.locator("#ai-options")).toBeHidden();
		const inlineStyle = await panel.getAttribute("style");
		expect(inlineStyle || "").not.toContain("left:");   // 内联几何清空 → CSS 默认锚定
		expect(Object.keys(await savedPrefs(page))).toHaveLength(0);
	});

	test("≤768px：header 拖动手势不移动固定面板", async ({ page }) => {
		const panel = await openAIPanel(page, SID);
		await page.setViewportSize({ width: 390, height: 844 });
		await expect(page.locator("#ai-panel")).toBeVisible();
		// 断点切换后面板回到固定布局，再取基准位置
		await page.waitForTimeout(300);
		const before = await panelBox(page);
		const header = await page.locator("#ai-panel-header").boundingBox();
		await dragFrom(page, { x: header!.x + 60, y: header!.y + 10 }, -60, 40);
		const after = await panelBox(page);
		expect(after!.x).toBeCloseTo(before!.x, 0);
		expect(after!.y).toBeCloseTo(before!.y, 0);
		expect(Object.keys(await savedPrefs(page))).toHaveLength(0);
	});
});
