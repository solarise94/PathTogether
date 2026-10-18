/** AI 入口统一（2026-10-18）e2e —— 真实 Flask + 内嵌 PG + **真实 HistoPilot
 * bundle**（integrations/pathtogether 经 npm run bundle:pathtogether 装入
 * PLUGIN_BUNDLES_DIR；e2e_server._plugin_dir 命中即用真 bundle，不装桩）。
 *
 * 覆盖（handoff §7 AI 验收）：
 *  - 工具条无需点 ⋯ 即有「✨ 加入 AI 对话」；点击 → AI 面板打开、出现
 *    marker 附件卡；不发送（零 /api/ai/* 请求）、不新建会话；composer 已有
 *    文字保留；附件卡可移除；
 *  - 「让 AI 解读」interpret 预填退役：加入附件不写 composer 文本；
 *  - 只读（分享访客语义不在本 spec；这里锁 can_edit=false 的行内 chip 与
 *    工具条不因权限误禁——由单元测试覆盖，浏览器侧锁可见入口）；
 *  - ⋯ 菜单：「居中显示此标记」（仅导航）、「锁定位置」；
 *  - 标注行内不再渲染「快速问答」/「从此处深读」；行内 chip 为
 *    「✨ 加入 AI 对话」，点击同样只加附件卡（零 /api/ai/ask|branch）。
 *
 * 前置：PLUGIN_BUNDLES_DIR 指向包含真实 histopilot bundle 的目录（运行说明见
 * tests/e2e/README 或 CI cross-repo job；bundle 缺失时 e2e_server 会装桩，
 * 本 spec 首个用例会以「未检测到真实 bundle」失败——显式失败优于假通过）。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8")) as {
	userLogin: string; userPassword: string;
	rasterSlides: Record<"workbench" | "share", { slide_id: string; name: string }>;
};

const SID = CREDS.rasterSlides.workbench.slide_id;

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

async function openSlideWithAnnos(page: Page, sid: string) {
	await page.goto(`/app?slide=${sid}`);
	await expandSidebar(page);
	const row = page.locator(`.fb-hit[data-slide-id="${sid}"]`);
	await expect(row).toBeVisible();
	await row.click();
	await expect(page.locator("#viewer .openseadragon-canvas"))
		.toBeVisible({ timeout: 20_000 });
	// 标注层开启（⋯ → 显示全部标记）
	await page.locator("#tbb-more-btn").click();
	const annoAllBtn = page.locator("#anno-all-btn");
	await expect(annoAllBtn).toBeEnabled({ timeout: 15_000 });
	await annoAllBtn.click();
}

/** 标注画布内容外接框中心（CSS px）。 */
async function markerCenter(page: Page): Promise<{ x: number; y: number }> {
	return page.evaluate(() => {
		const c = document.querySelector("#anno-canvas") as HTMLCanvasElement | null;
		if (!c || !c.width) throw new Error("anno-canvas 无内容");
		const ctx = c.getContext("2d")!;
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
		if (minx === Infinity) throw new Error("anno-canvas 无内容");
		const dpr = c.width / c.clientWidth;
		const box = c.getBoundingClientRect();
		return {
			x: box.left + (minx + (maxx - minx) / 2) / dpr,
			y: box.top + (miny + (maxy - miny) / 2) / dpr,
		};
	});
}

/** 清空切片上当前用户可见的全部标注（markerCenter 假设画布只有本用例标记）。 */
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
	await expect.poll(async () => {
		const after = await pageApi(page, "GET",
			`/api/annotations?slide_id=${encodeURIComponent(sid)}`);
		const n = ((after.j.annotations || []) as unknown[]).reduce(
			(a, g) => a + ((g as { items?: unknown[] }).items || []).length, 0);
		return n;
	}, { timeout: 10_000 }).toBe(0);
}

/** 真实 bundle 必须已装入（桩的 main.js 没有附件卡实现——显式失败）。 */
async function expectRealBundle(page: Page) {
	const real = await page.evaluate(() =>
		typeof (window as { HistoPilot?: { pushPendingAttach?: unknown } }).HistoPilot?.pushPendingAttach === "function");
	expect(real, "真实 HistoPilot bundle 未装入（PLUGIN_BUNDLES_DIR？）——桩不提供附件卡").toBe(true);
}

test.describe("AI 入口统一（真实 bundle）", () => {
	test("工具条 ✨ 加入 AI 对话：加附件卡、不发送、保留文字、可移除", async ({ page }) => {
		await page.addInitScript(() => {
			try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
		});
		await page.setViewportSize({ width: 1440, height: 900 });
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", x: 60, y: 50, w: 80, h: 60,
			label: "e2e-ai-attach", shared: true, note: "e2e 备注",
		});
		await openSlideWithAnnos(page, SID);
		await expect.poll(() => markerCenter(page), { timeout: 20_000 }).toBeTruthy();
		await expectRealBundle(page);

		// 点击前先在 composer 输入文字（必须原样保留）
		await page.locator("#ai-btn").click();
		await expect(page.locator("#ai-panel")).toBeVisible();
		await page.locator("#ai-task").fill("帮我对比这个区域和周围");
		// 关面板（附件点击应重新打开）
		await page.locator("#ai-panel-close").click();
		await expect(page.locator("#ai-panel")).toBeHidden();

		const aiRequests: string[] = [];
		page.on("request", (req) => {
			const p = new URL(req.url()).pathname;
			if (p.startsWith("/api/ai/")) aiRequests.push(`${req.method()} ${p}`);
		});

		// 选中标记 → 工具条 ✨（无需点 ⋯）
		const c = await markerCenter(page);
		await page.mouse.click(c.x, c.y);
		const toolbar = page.locator("#anno-obj-toolbar");
		await expect(toolbar).toBeVisible();
		const askai = toolbar.locator(".aot-askai");
		await expect(askai).toContainText("加入 AI 对话");
		// ⋯ 菜单不再有 AI 解读项；定位项为「居中显示此标记」
		await toolbar.locator(".aot-more").click();
		const menu = toolbar.locator(".aot-menu");
		await expect(menu.locator(".aot-menu-item", { hasText: "居中显示此标记" })).toBeVisible();
		await expect(menu.locator(".aot-askai")).toHaveCount(0);
		await toolbar.locator(".aot-more").click();

		// 点击 ✨：面板重开 + marker 附件卡出现；零 /api/ai/* 请求
		await askai.click();
		await expect(page.locator("#ai-panel")).toBeVisible();
		const card = page.locator("#ai-pending-attaches .pending-attach.marker");
		await expect(card).toHaveCount(1);
		await expect(card).toContainText("标注附件");
		// interpret 预填退役：composer 文字原样保留
		await expect(page.locator("#ai-task")).toHaveValue("帮我对比这个区域和周围");
		// placeholder 提示切换
		await expect(page.locator("#ai-task")).toHaveAttribute("placeholder", /针对已加入的标记提问/);
		// 不发送、不新建会话、不启动模型
		expect(aiRequests, `点击加入不得发 AI 请求：${aiRequests.join(", ")}`).toEqual([]);

		// 附件卡可移除
		await card.locator(".ai-attach-remove").click();
		await expect(page.locator("#ai-pending-attaches .pending-attach")).toHaveCount(0);
		expect(aiRequests).toEqual([]);
	});

	test("标注行内 chip「✨ 加入 AI 对话」：不再有快速问答/从此处深读，点击只加附件", async ({ page }) => {
		await page.addInitScript(() => {
			try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
		});
		await page.setViewportSize({ width: 1440, height: 900 });
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);
		await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", x: 40, y: 40, w: 60, h: 50,
			label: "e2e-row-chip", shared: true,
		});
		await openSlideWithAnnos(page, SID);
		await expect.poll(() => markerCenter(page), { timeout: 20_000 }).toBeTruthy();
		await expectRealBundle(page);

		// 打开标注面板列表
		await page.locator("#anno-btn").click();
		const list = page.locator("#anno-panel-list");
		await expect(list.locator(".anno-item").first()).toBeVisible({ timeout: 15_000 });
		await expect(list).toBeVisible();
		// 行内有 ✨ chip（加入 AI 对话），没有 💬 快速问答 / ⑂ 从此处深读
		await expect(list).toContainText("加入 AI 对话");
		await expect(list).not.toContainText("快速问答");
		await expect(list).not.toContainText("从此处深读");

		const aiRequests: string[] = [];
		page.on("request", (req) => {
			const p = new URL(req.url()).pathname;
			if (p.startsWith("/api/ai/")) aiRequests.push(`${req.method()} ${p}`);
		});
		await list.locator(".anno-item button.ai-attach-anno").first().click();
		await expect(page.locator("#ai-panel")).toBeVisible();
		await expect(page.locator("#ai-pending-attaches .pending-attach.marker")).toHaveCount(1);
		expect(aiRequests, `行内加入不得发 AI 请求：${aiRequests.join(", ")}`).toEqual([]);
	});
});
