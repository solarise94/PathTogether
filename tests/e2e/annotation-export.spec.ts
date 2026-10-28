/** 标注导出（QuPath GeoJSON）端到端：真实 Flask + 内嵌 PG（playwright.config.ts
 * 的 webServer）+ 真实 Chromium 下载事件。
 *
 * 覆盖：
 *  - 通过 API 在 workbench 切片上创建一条人工矩形与一条多边形；
 *  - 打开查看器与标注面板，点击「导出标注」，捕获浏览器下载；
 *  - 保存到 Playwright output dir，解析 JSON：FeatureCollection 含两条
 *    Feature，geometry 类型与 level-0 坐标正确，objectType = annotation。
 *
 * 登录一律用键盘输入凭据（不 fill）；凭据只来自 E2E_CREDS_FILE，不打印。
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

/** 键盘逐字输入凭据（不用 fill；测试失败信息也不回显凭据）。 */
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

test.describe("标注导出（QuPath GeoJSON，真实后端）", () => {
	test("面板导出按钮下载当前切片标注并在 GeoJSON 中包含矩形与多边形", async ({
		page,
	}, testInfo) => {
		const SID = CREDS.rasterSlides.workbench.slide_id;
		await initSession(page);
		await loginByTyping(page, CREDS.userLogin, CREDS.userPassword);
		await wipeAnnotations(page, SID);

		const rect = { x: 60, y: 50, w: 80, h: 60 };
		const createdRect = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "rect", label: "e2e-export-rect",
			note: "human note", ...rect,
		});
		expect(createdRect.status, JSON.stringify(createdRect.j)).toBe(200);

		const points = [[200, 180], [360, 200], [300, 340]];
		const createdPoly = await pageApi(page, "POST", "/api/annotation", {
			slide_id: SID, type: "polygon",
			points, label: "e2e-export-polygon", note: "poly note",
		});
		expect(createdPoly.status, JSON.stringify(createdPoly.j)).toBe(200);

		await page.goto(`/app?slide=${SID}`);
		await expandSidebar(page);
		const row = page.locator(`.fb-hit[data-slide-id="${SID}"]`);
		await expect(row).toBeVisible();
		await row.click();
		await expect(page.locator("#viewer .openseadragon-canvas"))
			.toBeVisible({ timeout: 20_000 });

		const annoBtn = page.locator("#anno-btn");
		await expect(annoBtn).toBeEnabled({ timeout: 20_000 });
		await annoBtn.click();
		await expect(page.locator("#anno-panel")).toBeVisible();

		const exportBtn = page.locator("#anno-export-btn");
		await expect(exportBtn).toBeEnabled();
		const downloadPromise = page.waitForEvent("download");
		await exportBtn.click();
		const download = await downloadPromise;
		expect(download.suggestedFilename()).toContain("_annotations.geojson");

		const outPath = testInfo.outputPath("annotations.geojson");
		await download.saveAs(outPath);
		const parsed = JSON.parse(readFileSync(outPath, "utf8")) as {
			type: string;
			features: Array<{
				id?: string;
				geometry: { type: string; coordinates: unknown };
				properties: Record<string, any>;
			}>;
			pathtogether: Record<string, any>;
		};

		expect(parsed.type).toBe("FeatureCollection");
		expect(parsed.features).toHaveLength(2);
		expect(parsed.pathtogether.feature_count).toBe(2);
		for (const f of parsed.features) {
			expect(f.properties.objectType).toBe("annotation");
			expect(f.geometry.type).toBe("Polygon");
		}

		const rectFeature = parsed.features.find(
			(f) => f.properties.name === "e2e-export-rect");
		expect(rectFeature, JSON.stringify(parsed.features)).toBeTruthy();
		expect(rectFeature!.geometry.coordinates).toEqual([[
			[rect.x, rect.y],
			[rect.x + rect.w, rect.y],
			[rect.x + rect.w, rect.y + rect.h],
			[rect.x, rect.y + rect.h],
			[rect.x, rect.y],
		]]);

		const polyFeature = parsed.features.find(
			(f) => f.properties.name === "e2e-export-polygon");
		expect(polyFeature, JSON.stringify(parsed.features)).toBeTruthy();
		expect(polyFeature!.geometry.coordinates).toEqual([
			[...points, [...points[0]]],
		]);
		expect(polyFeature!.properties.source).toBe("human");

		await wipeAnnotations(page, SID);
	});
});
