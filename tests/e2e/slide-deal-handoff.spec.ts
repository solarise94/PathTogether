/** 翻牌无黑闪：连续图像交接的逐帧检测（Viewer 交互升级 §4）。
 *
 * 方法（§4.2 验收要求）：CDP Page.startScreencast 逐帧抓取真实合成帧，
 * 在已知明亮测试图（320×240 全画幅渐变 BMP）的组织区域（图像矩形的中央
 * 一半，不含黑边）采样像素；「黑帧」= 该区域 ≥98% 像素命中 viewer 背景
 * #262a30（±14/通道）。不用整帧平均亮度——原始黑边/留白不会误报。
 *
 * 场景：暖缓存重开、冷缓存首开、扣住 info、扣住首瓦片、缩略图 decode
 * 延迟、A→B→C 快速选片。
 *
 * BASELINE_PROOF=1 时只输出测量结果到 .gate-tmp/vi-chrome-evidence/
 * （证明检测在基线 5328fefc 上抓得到黑帧）；默认模式断言修复后黑帧为 0。
 */
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as {
	userLogin: string; userPassword: string;
	rasterSlides: Record<"workbench" | "share", { slide_id: string; name: string }>;
};
const BASELINE_PROOF = process.env.BASELINE_PROOF === "1";
const EVIDENCE_DIR = BASELINE_PROOF
	? resolve(__dirname, "../../.gate-tmp/vi-chrome-evidence/baseline")
	: resolve(__dirname, "../../.gate-tmp/vi-chrome-evidence/fixed");

const IMG_W = 320;
const IMG_H = 240;
// viewer 空画布底色（app.js 初始化设的 container 背景）
const BG = { r: 0x26, g: 0x2a, b: 0x30 };
const TOL = 14;

interface FrameScore { i: number; bgFraction: number; meanLum: number }
interface ScenarioReport {
	scenario: string;
	frameCount: number;
	darkFrames: FrameScore[];
	worst: FrameScore | null;
	scores: FrameScore[];
}

async function login(page: Page) {
	await page.goto("/login");
	await page.fill('form[action="/login"] input[name="username"]', CREDS.userLogin);
	await page.fill('form[action="/login"] input[name="password"]', CREDS.userPassword);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.click('form[action="/login"] button[type="submit"]'),
	]);
}

async function gotoAppAndExpand(page: Page) {
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto("/app");
	const menuBtn = page.locator("#menu-btn");
	await expect(menuBtn).toHaveAttribute("aria-expanded", /^(true|false)$/);
	if ((await menuBtn.getAttribute("aria-expanded")) !== "true") {
		await menuBtn.click();
		await expect(menuBtn).toHaveAttribute("aria-expanded", "true", { timeout: 5_000 });
	}
}

async function slideRow(page: Page, sid: string) {
	const hit = page.locator(`.fb-hit[data-slide-id="${sid}"]`);
	await expect(page.locator(".fb-folder, .fb-hit").first()).toBeVisible();
	for (let n = 0; n < 20 && !await hit.count(); n++) {
		const next = page.locator("#fb-next-btn");
		if (await next.isDisabled()) break;
		await next.click();
	}
	await expect(hit).toBeVisible();
	return hit;
}

interface CapturedFrame { data: string; w: number; h: number; ts: number }
interface DealMarks { landingAt: number; dealGoneAt: number }

/** OSD 画布（.openseadragon-canvas 是 div，内含真 canvas）中心是否有非透明像素 */
async function canvasCenterHasPixels(page: Page): Promise<boolean> {
	return page.evaluate(() => {
		const root = document.querySelector("#viewer .openseadragon-canvas");
		const canvas = (root && root.tagName === "CANVAS" ? root : root?.querySelector("canvas")) as
			HTMLCanvasElement | null;
		if (!canvas || !canvas.width) return false;
		const d = canvas.getContext("2d")!.getImageData(
			canvas.width / 2 - 8, canvas.height / 2 - 8, 16, 16).data;
		for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true;
		return false;
	});
}

/** 逐帧抓取 + DOM 里程碑（铺开层出现/飞牌结束）：黑帧断言按里程碑门控——
 * 首次开片的空态底色（铺开前的暗帧）是 §4.1 明确允许的状态。 */
async function startFrameCapture(page: Page) {
	const cdp = await page.context().newCDPSession(page);
	const frames: CapturedFrame[] = [];
	let active = true;
	cdp.on("Page.screencastFrame", (ev) => {
		if (!active) return;
		frames.push({ data: ev.data, w: ev.metadata.deviceWidth, h: ev.metadata.deviceHeight, ts: Date.now() });
		cdp.send("Page.screencastFrameAck", { sessionId: ev.sessionId }).catch(() => {});
	});
	await page.evaluate(() => {
		const w = window as unknown as Record<string, unknown>;
		const marks: DealMarks = { landingAt: 0, dealGoneAt: 0 };
		w.__dealMarks = marks;
		const obs = new MutationObserver(() => {
			const landing = document.querySelector(".slide-deal-landing");
			const deal = document.querySelector(".slide-deal");
			const now = Date.now();
			if (landing && !marks.landingAt) marks.landingAt = now;
			if (!deal && !marks.dealGoneAt) marks.dealGoneAt = now;
		});
		obs.observe(document.body, { subtree: true, childList: true });
		w.__dealObserver = obs;
	});
	await cdp.send("Page.startScreencast", { format: "png", everyNthFrame: 1 });
	return {
		frames,
		marks: async () => (await page.evaluate(() =>
			(window as unknown as { __dealMarks: DealMarks }).__dealMarks)),
		stop: async () => {
			active = false;
			try { await cdp.send("Page.stopScreencast"); } catch { /* 已停止 */ }
			// 最后一帧的 ack/传输落地
			await page.waitForTimeout(250);
			return frames;
		},
	};
}

/** 在页面内解码帧并对组织区域采样：黑帧 = ≥98% 像素命中背景色。
 * 分批送回浏览器解码（浏览器有 PNG 解码器；Node 侧无额外依赖）。 */
async function scoreFrames(page: Page, frames: CapturedFrame[]) {
	const view = await page.evaluate(({ imgW, imgH }) => {
		const v = document.getElementById("viewer")!.getBoundingClientRect();
		// 首页 fit：320×240 contain 进 #viewer，取中央一半为组织采样区
		const s = Math.min(v.width / imgW, v.height / imgH);
		const w = imgW * s, h = imgH * s;
		const left = v.left + (v.width - w) / 2, top = v.top + (v.height - h) / 2;
		return { left: left + w * 0.25, top: top + h * 0.25, width: w * 0.5, height: h * 0.5, cssW: window.innerWidth };
	}, { imgW: IMG_W, imgH: IMG_H });
	const scores: FrameScore[] = [];
	const BATCH = 24;
	for (let start = 0; start < frames.length; start += BATCH) {
		const batch = frames.slice(start, start + BATCH).map(f => f.data);
		const part = await page.evaluate(async ({ b64s, region, cssW, bg, tol, offset }) => {
			const out: Array<{ i: number; bgFraction: number; meanLum: number }> = [];
			for (let b = 0; b < b64s.length; b++) {
				const resp = await fetch("data:image/png;base64," + b64s[b]);
				const bitmap = await createImageBitmap(await resp.blob());
				const scale = bitmap.width / cssW;
				const cnv = new OffscreenCanvas(bitmap.width, bitmap.height);
				const ctx = cnv.getContext("2d")!;
				ctx.drawImage(bitmap, 0, 0);
				const rx = Math.round(region.left * scale), ry = Math.round(region.top * scale);
				const rw = Math.max(4, Math.round(region.width * scale));
				const rh = Math.max(4, Math.round(region.height * scale));
				const img = ctx.getImageData(rx, ry, rw, rh);
				let match = 0, lumSum = 0, n = 0;
				for (let y = 0; y < rh; y += 3) {
					for (let x = 0; x < rw; x += 3) {
						const k = (y * rw + x) * 4;
						const r = img.data[k], g = img.data[k + 1], bl = img.data[k + 2];
						if (Math.abs(r - bg.r) <= tol && Math.abs(g - bg.g) <= tol && Math.abs(bl - bg.b) <= tol) match++;
						lumSum += 0.2126 * r + 0.7152 * g + 0.0722 * bl;
						n++;
					}
				}
				out.push({ i: offset + b, bgFraction: match / n, meanLum: lumSum / n });
				bitmap.close();
			}
			return out;
		}, {
			b64s: batch,
			region: { left: view.left, top: view.top, width: view.width, height: view.height },
			cssW: view.cssW,
			bg: BG,
			tol: TOL,
			offset: start,
		});
		scores.push(...part);
	}
	return scores;
}

interface GatedReport extends ScenarioReport {
	preSpreadDark: FrameScore[];   // 铺开层就位前的暗帧（首次开片=允许的空态底色）
}

/** allowEmptyStateDark=false（暖重开/快速选片：上一张图必须全程可见）时，
 * 任何组织区黑帧都计为失败；=true（冷缓存首开）时，只统计铺开层就位
 * （landingAt+420ms，含 340ms 铺开动画+余量）之后的黑帧。 */
function summarize(scenario: string, scores: FrameScore[],
	frames: CapturedFrame[], marks: DealMarks | null,
	allowEmptyStateDark: boolean): GatedReport {
	const isDark = (s: FrameScore) => s.bgFraction >= 0.98;
	const dark = scores.filter(isDark);
	let effective = dark;
	let preSpreadDark: FrameScore[] = [];
	if (allowEmptyStateDark && marks && marks.landingAt) {
		const cutoff = marks.landingAt + 420;
		effective = dark.filter(s => frames[s.i]!.ts >= cutoff);
		preSpreadDark = dark.filter(s => frames[s.i]!.ts < cutoff);
	}
	const worst = effective.length
		? effective.reduce((a, b) => (b.bgFraction > a.bgFraction ? b : a))
		: (scores.length ? scores.reduce((a, b) => (b.bgFraction > a.bgFraction ? b : a)) : null);
	return { scenario, frameCount: scores.length, darkFrames: effective, preSpreadDark, worst, scores };
}

async function writeEvidence(report: ScenarioReport, frames: CapturedFrame[]) {
	fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
	fs.writeFileSync(join(EVIDENCE_DIR, `${report.scenario}.json`),
		JSON.stringify({ ...report, scores: report.scores.filter(s => s.bgFraction > 0.5) }, null, 1));
	// 保存最接近黑的一帧与中段一帧供人工核验
	if (report.worst && frames.length) {
		const idx = Math.min(report.worst.i, frames.length - 1);
		fs.writeFileSync(join(EVIDENCE_DIR, `${report.scenario}-worst.png`),
			Buffer.from(frames[idx]!.data, "base64"));
		const mid = frames[Math.floor(frames.length * 0.4)] ?? frames[0]!;
		fs.writeFileSync(join(EVIDENCE_DIR, `${report.scenario}-mid.png`),
			Buffer.from(mid.data, "base64"));
	}
}

/** 打开一个切片并逐帧记录从点击到交接完成的全过程 */
async function captureOpen(
	page: Page, sid: string, name: string,
	opts: { timeout?: number; allowEmptyStateDark?: boolean } = {},
): Promise<{ report: GatedReport; frames: CapturedFrame[] }> {
	const hit = await slideRow(page, sid);
	const cap = await startFrameCapture(page);
	await hit.click();
	await expect(page.locator(".slide-deal")).toHaveCount(0, { timeout: opts.timeout ?? 15_000 });
	const frames = await cap.stop();
	const marks = await cap.marks();
	const scores = await scoreFrames(page, frames);
	const report = summarize(name, scores, frames, marks, opts.allowEmptyStateDark !== false);
	return { report, frames };
}

test.describe("翻牌交接逐帧黑闪检测（§4）", () => {
	const A = CREDS.rasterSlides.workbench.slide_id;
	const B = CREDS.rasterSlides.share.slide_id;

	test("黑帧采样校准：侧栏存在时仍采到指定的白色组织区", async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		// Known geometry: viewer=(224,60,1216,840), fitted image=(272,60,1120,840).
		// Its central half is exactly (552,270,560,420). Everything else is dark.
		await page.setContent(`<style>body { margin:0; background:#262a30 }
			#viewer { position:fixed; left:224px; top:60px; width:1216px; height:840px }
			#sample { position:fixed; left:552px; top:270px; width:560px; height:420px; background:white }
			</style><div id="viewer"></div><div id="sample"></div>`);
		const png = await page.screenshot();
		const scores = await scoreFrames(page, [{ data: png.toString("base64"), w: 1440, h: 900, ts: Date.now() }]);
		expect(scores[0].bgFraction).toBe(0);
		expect(scores[0].meanLum).toBeCloseTo(255, 1);
	});

	test("暖缓存重开：已有切片保持可见，全程无组织区黑帧", async ({ page }) => {
		await login(page);
		await gotoAppAndExpand(page);
		// 预热：先完整打开一次（缩略图/DZI/瓦片进缓存、画布出图）
		const warmRow = await slideRow(page, A);
		await warmRow.click();
		await expect(page.locator(".slide-deal")).toHaveCount(0, { timeout: 20_000 });
		await expect.poll(() => canvasCenterHasPixels(page)).toBe(true);
		// 上一张切片全程可见：不允许任何组织区黑帧（含铺开前）
		const { report, frames } = await captureOpen(page, A, "warm-reopen", { allowEmptyStateDark: false });
		await writeEvidence(report, frames);
		if (!BASELINE_PROOF) {
			expect(report.frameCount, "应抓到翻牌帧").toBeGreaterThan(5);
			expect(report.darkFrames, `黑帧: ${JSON.stringify(report.darkFrames)}`).toHaveLength(0);
		}
	});

	test("冷缓存首开：空态为底色，铺开后预览连续", async ({ page }) => {
		await login(page);
		await gotoAppAndExpand(page);
		const { report, frames } = await captureOpen(page, A, "cold-first");
		await writeEvidence(report, frames);
		if (!BASELINE_PROOF) {
			expect(report.frameCount).toBeGreaterThan(5);
			// 铺开层就位后组织区不得再出现黑帧；之前的暗帧=空态底色（§4.1 允许）
			expect(report.darkFrames, `黑帧: ${JSON.stringify(report.darkFrames)}`).toHaveLength(0);
		}
	});

	test("扣住 info 1.2s：飞牌照常铺开，交接无黑帧", async ({ page }) => {
		await login(page);
		await page.route(`**/api/slides/${A}/info`, async route => {
			await new Promise(r => setTimeout(r, 1200));
			await route.continue();
		});
		await gotoAppAndExpand(page);
		const { report, frames } = await captureOpen(page, A, "info-delay", { timeout: 20_000 });
		await writeEvidence(report, frames);
		if (!BASELINE_PROOF) {
			expect(report.frameCount).toBeGreaterThan(5);
			expect(report.darkFrames, `黑帧: ${JSON.stringify(report.darkFrames)}`).toHaveLength(0);
		}
	});

	test("info 超过 8 秒：退出装饰后预览不偏移，迟到响应不留下重复底图", async ({ page }) => {
		await login(page);
		let release!: () => void;
		const gate = new Promise<void>(resolve => { release = resolve; });
		await page.route(`**/api/slides/${A}/info`, async route => {
			await gate;
			await route.continue();
		});
		try {
			await gotoAppAndExpand(page);
			const row = await slideRow(page, A);
			await expect.poll(() => row.locator("img").evaluate((e: HTMLImageElement) => e.complete && e.naturalWidth > 0)).toBe(true);
			await row.focus();
			await page.keyboard.press("Enter");
			await expect(page.locator('.slide-deal[data-phase="preview"]')).toBeAttached();
			const landing = await page.locator(".slide-deal-landing").boundingBox();
			await expect(page.locator(".slide-deal")).toHaveCount(0, { timeout: 12_000 });
			const base = page.locator("#viewer .osd-base-thumb");
			await expect(base).toHaveCount(1);
			const actual = await base.boundingBox();
			expect(Math.abs(actual!.x - landing!.x), "timeout must preserve page-space X").toBeLessThan(2);
			expect(Math.abs(actual!.y - landing!.y), "timeout must preserve page-space Y").toBeLessThan(2);
			release();
			await expect.poll(() => canvasCenterHasPixels(page)).toBe(true);
			await expect(base).toHaveCount(1);
		} finally { release(); }
	});

	test("扣住首瓦片 1.5s：已解码预览保持，无组织区黑帧", async ({ page }) => {
		await login(page);
		await page.route(`**/api/slides/${A}/tiles/**`, async route => {
			await new Promise(r => setTimeout(r, 1500));
			await route.continue();
		});
		await gotoAppAndExpand(page);
		const { report, frames } = await captureOpen(page, A, "tile-delay", { timeout: 20_000 });
		await writeEvidence(report, frames);
		if (!BASELINE_PROOF) {
			expect(report.frameCount).toBeGreaterThan(5);
			expect(report.darkFrames, `黑帧: ${JSON.stringify(report.darkFrames)}`).toHaveLength(0);
		}
	});

	test("缩略图 decode 延迟 1.2s：桥接/铺开层覆盖组织区", async ({ page }) => {
		await login(page);
		await gotoAppAndExpand(page);
		// 先让列表缩略图加载完成（飞牌/桥接的同一 URL），再扣住该 URL 的
		// 新请求：底图预览迟到，桥接层必须撑住交接
		await slideRow(page, A);
		const hit = page.locator(`.fb-hit[data-slide-id="${A}"]`);
		await expect.poll(() => hit.locator("img").evaluate(
			(el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
		await page.route(`**/api/slides/${A}/thumbnail*`, async route => {
			await new Promise(r => setTimeout(r, 1200));
			await route.continue();
		});
		const { report, frames } = await captureOpen(page, A, "thumb-decode-delay", { timeout: 20_000 });
		await writeEvidence(report, frames);
		if (!BASELINE_PROOF) {
			expect(report.frameCount).toBeGreaterThan(5);
			expect(report.darkFrames, `黑帧: ${JSON.stringify(report.darkFrames)}`).toHaveLength(0);
		}
	});

	test("A→B→C 快速选片：迟到结果不揭黑幕，终点为最后一次选择", async ({ page }) => {
		await login(page);
		await gotoAppAndExpand(page);
		const rowA = await slideRow(page, A);
		const rowB = await slideRow(page, B);
		// 预热 A/B 的缩略图（列表已加载）并让 A 完整打开一次
		await rowA.click();
		await expect(page.locator(".slide-deal")).toHaveCount(0, { timeout: 20_000 });
		const cap = await startFrameCapture(page);
		await rowB.click();
		await page.waitForTimeout(120);
		await rowA.click();
		await page.waitForTimeout(120);
		await rowB.click();
		await expect(page.locator(".slide-deal")).toHaveCount(0, { timeout: 20_000 });
		// 终点 = B（最后一次选择）：画布内容确为打开中的切片
		await expect(page.locator(".fb-hit[data-slide-id=\"" + B + "\"]")).toHaveAttribute("aria-pressed", "true");
		const frames = await cap.stop();
		const marks = await cap.marks();
		const scores = await scoreFrames(page, frames);
		const report = summarize("a-b-c-rapid", scores, frames, marks, false);
		await writeEvidence(report, frames);
		if (!BASELINE_PROOF) {
			expect(report.frameCount).toBeGreaterThan(10);
			expect(report.darkFrames, `黑帧: ${JSON.stringify(report.darkFrames)}`).toHaveLength(0);
		}
	});
});
