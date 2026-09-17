/**
 * BMP 普通图片兼容 真实浏览器 E2E（raster-image-compatibility-agent-plan §6
 * 「产品」行：至少完成一次主站及分享页浏览器查看和标注回放）。
 *
 * 与单元层（tests/js/raster-image-compat.test.ts 契约、tests/test_raster_slide.py
 * 真实 Pillow 解码）职责不同：这里用真实 Chromium + 真实 Flask（主站 app.py +
 * 独立 share_server.py 第二端口）+ 内嵌 PostgreSQL 验证端到端产品语义：
 *
 *   1. 主站：user 登录 → #file-input 真实 UI 上传 TS 现场构造的 24-bit BMP →
 *      上传行三段状态到「入库完成」→ 自动打开：DZI 真实请求（.dzi 200 +
 *      _files/ 瓦片 200）+ OSD 画布出图 → 缺物理标尺语义（提示元素可见 /
 *      mm、µm 选项 disabled / 单位落 px / 预设禁用 / 侧栏「无物理标尺」 /
 *      info mpp_*=null、mpp_source="missing"）→ 视图中心拖出像素 rect 并
 *      保存（POST body 为 level-0 像素 x/y/w/h）→ reload 重开，回放像素
 *      坐标逐项一致 + 画布重绘。
 *   2. 分享页：user 经真实 UI（标注权限勾选 + 切片勾选 + 「分享选中」）
 *      创建对该 BMP 的分享（/api/share/create：user 只能分享自有切片，
 *      无需公网 host/邮件）→ 浏览器打开 /s/<token>（share_server 第二端口）
 *      → 真实瓦片 + 画布出图 + mm 预设按钮禁用带「无物理标尺」提示 +
 *      mpp 手动输入区不显示 → 像素（arrow）标注完成即保存 → reload 后
 *      /api/rois 像素坐标一致 + 画布重绘。
 *
 * 纪律同 admin-workbench.spec.ts：每用例收集未允许浏览器错误（console
 * error / pageerror / requestfailed），用例结束断言为空；语言经
 * hp_lang=zh 固定，文案断言确定。BMP 在 TS 里现场构造（24-bit 无压缩
 * BITMAPINFOHEADER + 渐变像素），不引入 Python fixture。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as {
	baseUrl: string; shareBaseUrl?: string;
	ownerLogin: string; ownerPassword: string;
	userLogin: string; userPassword: string;
};
// 分享服务第二端口（e2e_server.py --share-port，缺省主端口 + 1）
const SHARE_BASE = CREDS.shareBaseUrl || `http://127.0.0.1:${PORT + 1}`;

// 文件名按次运行唯一：reuseExistingServer 复用长命服务器时，同名重传会被
// 服务端拒绝（一次性凭据 + 唯一名保证每次运行都是干净上传）
const BMP_NAME = `e2e-raster-${Date.now().toString(36)}-320x240.bmp`;
const BMP_W = 320;
const BMP_H = 240;

/** 24-bit 无压缩 BMP（BITMAPINFOHEADER + 自下而上 BGR 渐变像素）。
 * 像素值随 x/y 变化（非对称内容），位置错位时内容可区分。 */
function makeBmp24(width: number, height: number): Buffer {
	const rowSize = Math.ceil((width * 3) / 4) * 4;
	const pixelBytes = rowSize * height;
	const dataOffset = 54; // BITMAPCORE 头 + BITMAPINFOHEADER
	const buf = Buffer.alloc(dataOffset + pixelBytes);
	buf.write("BM", 0, "ascii");
	buf.writeUInt32LE(buf.length, 2);  // 文件大小
	buf.writeUInt32LE(dataOffset, 10); // 像素数据偏移
	buf.writeUInt32LE(40, 14);         // BITMAPINFOHEADER 大小
	buf.writeInt32LE(width, 18);
	buf.writeInt32LE(height, 22);
	buf.writeUInt16LE(1, 26);          // planes
	buf.writeUInt16LE(24, 28);         // bpp：24-bit
	buf.writeUInt32LE(0, 30);          // BI_RGB 无压缩
	buf.writeUInt32LE(pixelBytes, 34); // 像素字节数
	for (let y = 0; y < height; y++) {
		for (let x = 0; x < width; x++) {
			const o = dataOffset + y * rowSize + x * 3;
			buf[o] = (x * 7) % 256;               // B
			buf[o + 1] = (y * 5) % 256;           // G
			buf[o + 2] = (x * 3 + y * 11) % 256;  // R
		}
	}
	return buf;
}

/** 收集未允许的浏览器错误（console error / pageerror / 失败请求）。
 * favicon 是浏览器内置请求，403/404 属平台预期，不计入。 */
function attachErrorCollectors(page: Page): string[] {
	const problems: string[] = [];
	const isFavicon = (url: string) => {
		try { return new URL(url).pathname === "/favicon.ico"; } catch { return false; }
	};
	page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
	page.on("console", (msg) => {
		if (msg.type() === "error" && !/favicon/i.test(msg.text())) {
			problems.push(`console.error: ${msg.text()}`);
		}
	});
	page.on("requestfailed", (req) => {
		if (isFavicon(req.url())) return;
		problems.push(`requestfailed: ${req.url()} (${req.failure()?.errorText})`);
	});
	return problems;
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

/** 桌面端侧栏首入默认收起（app.js init 即应用 collapsed 类 + 0.18s 宽度
 * 过渡）。加载早期 isVisible 对过渡中的 visibility:hidden 元素可能误报
 * true——状态判定只用 menu-btn 的 aria-expanded（JS 权威口径），false 就
 * 点开，并以「aria-expanded=true + 元素真实可见」双确认收尾。 */
async function expandSidebar(page: Page) {
	const menuBtn = page.locator("#menu-btn");
	const marker = page.locator("#import-slides-btn");
	await expect(menuBtn).toHaveAttribute("aria-expanded", /^(true|false)$/);
	if ((await menuBtn.getAttribute("aria-expanded")) === "true") {
		await expect(marker).toBeVisible({ timeout: 5_000 });
		return;
	}
	await menuBtn.click();
	await expect(menuBtn).toHaveAttribute("aria-expanded", "true", { timeout: 5_000 });
	await expect(marker).toBeVisible({ timeout: 5_000 });
}

/** 画布（或画布容器内第一个 canvas）中心 64×64 区域是否已有非透明像素。
 * 仅同源画布（无跨域污染），getImageData 可用。 */
async function canvasCenterHasPixels(page: Page, selector: string): Promise<boolean> {
	return page.evaluate((sel) => {
		const root = document.querySelector(sel);
		if (!root) return false;
		const canvas = (root.tagName === "CANVAS" ? root : root.querySelector("canvas")) as
			HTMLCanvasElement | null;
		if (!canvas) return false;
		const w = canvas.width;
		const h = canvas.height;
		if (!w || !h) return false;
		const ctx = canvas.getContext("2d");
		if (!ctx) return false;
		const cx = Math.floor(w / 2);
		const cy = Math.floor(h / 2);
		const x0 = Math.max(0, cx - 32);
		const y0 = Math.max(0, cy - 32);
		const data = ctx.getImageData(x0, y0, Math.min(64, w - x0), Math.min(64, h - y0)).data;
		for (let i = 3; i < data.length; i += 4) {
			if (data[i] > 0) return true;
		}
		return false;
	}, selector);
}

test.describe.configure({ mode: "serial" });

test.describe("BMP 普通图片兼容（主站 + 分享页真实浏览器走查）", () => {

	test("主站：BMP 上传 → DZI 查看 → 缺标尺语义 → 像素 rect 标注 → reload 回放一致", async ({ page }) => {
		const problems = attachErrorCollectors(page);
		// 真实 DZI 请求收集（.dzi 与 _files/ 瓦片，含状态码）
		const hits: Array<{ path: string; status: number }> = [];
		page.on("response", (resp) => {
			try {
				const p = new URL(resp.url()).pathname;
				if (p.includes("_files/") || p.endsWith(`/${BMP_NAME}.dzi`)) {
					hits.push({ path: p, status: resp.status() });
				}
			} catch { /* 无法解析的 URL 不计入 */ }
		});
		// 语言固定 zh（localStorage 键 hp_lang，i18n.js 契约）→ 文案断言确定
		await page.addInitScript(() => {
			try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
		});
		await page.setViewportSize({ width: 1440, height: 900 });

		await login(page, CREDS.userLogin, CREDS.userPassword);
		await page.goto("/app");
		await expandSidebar(page);

		// 真实 UI 上传：#file-input change → uploadFile（小文件 legacy /api/upload）
		// → 服务端真实 Pillow 全量解码校验 → 入库 → 自动打开
		await page.setInputFiles("#file-input", {
			name: BMP_NAME,
			mimeType: "image/bmp",
			buffer: makeBmp24(BMP_W, BMP_H),
		});
		const uploadRow = page.locator("#upload-progress-list .upload-item", { hasText: BMP_NAME });
		await expect(uploadRow).toBeVisible();
		await expect(uploadRow.locator(".upload-item-status"))
			.toContainText("入库完成", { timeout: 30_000 });

		// 上传成功自动打开：侧栏出现该切片行，元信息显示「无物理标尺」
		const slideRow = page.locator(`.slide-row[data-name="${BMP_NAME}"]`);
		await expect(slideRow).toBeVisible();
		await expect(slideRow.locator(".slide-meta")).toContainText("无物理标尺");

		// 真实瓦片请求：至少一张 `_files/` 瓦片 200（RGB 画质传输路径首开为
		// 内联 TileSource，直接请求瓦片、不发 .dzi XML——`.dzi` 出现与否都
		// 不作为闸；瓦片才是「真实出图请求」的权威口径）
		await expect.poll(() => hits.filter(
			(h) => h.status === 200 && h.path.includes(`${BMP_NAME}_files/`)).length,
			{ timeout: 20_000 }).toBeGreaterThan(0);

		// 画布出图：OSD 画布中心出现非透明像素
		await expect(page.locator("#viewer .openseadragon-canvas")).toBeVisible({ timeout: 20_000 });
		await expect.poll(() => canvasCenterHasPixels(page, "#viewer .openseadragon-canvas"),
			{ timeout: 20_000 }).toBe(true);

		// 缺物理标尺语义（info 接口真实返回）：尺寸 + mpp/objective null + mpp_source=missing
		const info = await page.evaluate(async (name) => {
			const r = await fetch("/api/slide/" + encodeURIComponent(name) + "/info");
			return r.json();
		}, BMP_NAME);
		expect(info.width).toBe(BMP_W);
		expect(info.height).toBe(BMP_H);
		expect(info.mpp_x).toBeNull();
		expect(info.mpp_y).toBeNull();
		expect(info.objective).toBeNull();
		expect(info.mpp_source).toBe("missing");

		// 缺物理标尺语义（UI）：矩形工具打开尺寸区 → 提示可见、单位落 px、
		// mm/µm 选项与 mm 预设禁用
		await page.locator("#roi-rect-btn").click();
		await expect(page.locator("#roi-settings")).toBeVisible();
		await expect(page.locator("#roi-no-scale-hint")).toBeVisible();
		await expect(page.locator("#roi-no-scale-hint")).toContainText("无物理标尺");
		const unitSelect = page.locator("#roi-unit-select");
		await expect(unitSelect).toHaveValue("px");
		await expect(unitSelect.locator("option[value=\"mm\"]")).toBeDisabled();
		await expect(unitSelect.locator("option[value=\"um\"]")).toBeDisabled();
		await expect(page.locator("#roi-preset-select")).toBeDisabled();

		// 画像素 rect：视图中心拖出矩形（fit 缩放下约 20+ 像素）→ 保存标注
		const canvasBox = await page.locator("#anno-canvas").boundingBox();
		expect(canvasBox).toBeTruthy();
		const cx = canvasBox!.x + canvasBox!.width / 2;
		const cy = canvasBox!.y + canvasBox!.height / 2;
		await page.mouse.move(cx - 40, cy - 30);
		await page.mouse.down();
		await page.mouse.move(cx + 40, cy + 30, { steps: 8 });
		await page.mouse.up();
		const saveBtn = page.locator("#save-anno-btn");
		await expect(saveBtn).toBeEnabled();
		const annoReqPromise = page.waitForRequest((req) =>
			req.method() === "POST" && new URL(req.url()).pathname === "/api/annotation");
		const annoRespPromise = page.waitForResponse((resp) =>
			resp.request().method() === "POST" && new URL(resp.url()).pathname === "/api/annotation");
		await saveBtn.click();
		const annoResp = await annoRespPromise;
		expect(annoResp.status()).toBe(200);
		const saved = (await annoReqPromise).postDataJSON() as {
			slide: string; type: string; x: number; y: number; w: number; h: number;
		};
		// 保存 body 是 level-0 像素 x/y/w/h（不依赖 mpp）
		expect(saved.slide).toBe(BMP_NAME);
		expect(saved.type).toBe("rect");
		for (const v of [saved.x, saved.y, saved.w, saved.h]) {
			expect(Number.isInteger(v)).toBe(true);
		}
		expect(saved.x).toBeGreaterThanOrEqual(0);
		expect(saved.y).toBeGreaterThanOrEqual(0);
		expect(saved.w).toBeGreaterThan(0);
		expect(saved.h).toBeGreaterThan(0);
		expect(saved.x + saved.w).toBeLessThanOrEqual(BMP_W);
		expect(saved.y + saved.h).toBeLessThanOrEqual(BMP_H);

		// reload 后回放：重开切片 → 「⋯ → 显示全部标记」→ 画布重绘 +
		// /api/annotations 像素坐标逐项一致
		await page.reload();
		await expandSidebar(page);
		const row2 = page.locator(`.slide-row[data-name="${BMP_NAME}"]`);
		await expect(row2).toBeVisible();
		await row2.click();
		await expect(page.locator("#viewer .openseadragon-canvas")).toBeVisible({ timeout: 20_000 });
		await page.locator("#tbb-more-btn").click();
		await expect(page.locator("#tbb-more")).toBeVisible();
		const annoAllBtn = page.locator("#anno-all-btn");
		await expect(annoAllBtn).toBeEnabled({ timeout: 15_000 });
		await annoAllBtn.click();
		await expect.poll(() => canvasCenterHasPixels(page, "#anno-canvas"),
			{ timeout: 15_000 }).toBe(true);
		const replay = await page.evaluate(async (name) => {
			const r = await fetch("/api/annotations?slide=" + encodeURIComponent(name));
			return r.json();
		}, BMP_NAME);
		const items: Array<Record<string, unknown>> = [];
		((replay.annotations || []) as Array<{ items?: Array<Record<string, unknown>> }>)
			.forEach((grp) => (grp.items || []).forEach((it) => items.push(it)));
		expect(items.filter((it) => it.type === "rect")).toContainEqual(
			expect.objectContaining({ x: saved.x, y: saved.y, w: saved.w, h: saved.h }));

		expect(problems, problems.join("\n")).toEqual([]);
	});

	test("分享页：普通图片查看 + mm 预设禁用提示 + 像素标注回放", async ({ page }) => {
		const problems = attachErrorCollectors(page);
		// 分享服务瓦片请求收集（/s/<token>/api/slide/..._files/...）
		const shareHits: Array<{ path: string; status: number }> = [];
		page.on("response", (resp) => {
			try {
				const p = new URL(resp.url()).pathname;
				if (p.startsWith("/s/") && (p.includes("_files/") || p.endsWith(`/${BMP_NAME}.dzi`))) {
					shareHits.push({ path: p, status: resp.status() });
				}
			} catch { /* 无法解析的 URL 不计入 */ }
		});
		await page.addInitScript(() => {
			try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
		});
		await page.setViewportSize({ width: 1440, height: 900 });

		// user 登录主站，经真实 UI 建立对该 BMP 的分享：
		// 勾选「允许标注」→ 勾选切片 → 「分享选中切片」（POST /api/share/create）。
		//（#unfiled-share 在 app.js 未绑定监听，可用入口是分享管理区按钮）
		await login(page, CREDS.userLogin, CREDS.userPassword);
		await page.goto("/app");
		await expandSidebar(page);
		const slideRow = page.locator(`.slide-row[data-name="${BMP_NAME}"]`);
		await expect(slideRow).toBeVisible();
		await page.locator("#share-perm-annotate").check();
		await slideRow.locator(".slide-check").check();
		await page.locator("#share-create-btn").click();
		let shareUrl = "";
		await expect.poll(async () => {
			shareUrl = await page.locator("#share-result-url").inputValue();
			return shareUrl.length > 0;
		}, { timeout: 15_000 }).toBe(true);
		expect(shareUrl).toContain("/s/");

		// 浏览器打开分享页（share_server 第二端口真实服务，同一 UPLOAD_DIR/PG）
		const token = new URL(shareUrl).pathname.split("/").filter(Boolean)[1];
		expect(token, `分享 URL 应含 token：${shareUrl}`).toBeTruthy();
		await page.goto(shareUrl);
		await expect(page.locator("#viewer .openseadragon-canvas")).toBeVisible({ timeout: 20_000 });
		await expect(page.locator("#current-slide")).toContainText(
			BMP_NAME.replace(/\.bmp$/, ""), { timeout: 20_000 });

		// 查看：真实瓦片 + 画布出图
		await expect.poll(() => shareHits.filter(
			(h) => h.status === 200 && h.path.includes(`${BMP_NAME}_files/`)).length,
			{ timeout: 20_000 }).toBeGreaterThan(0);
		await expect.poll(() => canvasCenterHasPixels(page, "#viewer .openseadragon-canvas"),
			{ timeout: 20_000 }).toBe(true);

		// 缺物理标尺语义：mm 预设按钮禁用 + title「无物理标尺」提示；
		// mpp 手动输入区不显示（不暗示「填了就能标」）
		const roi6 = page.locator("#roi-6");
		await expect(roi6).toBeDisabled();
		await expect(page.locator("#roi-6-5")).toBeDisabled();
		await expect(roi6).toHaveAttribute("title", /无物理标尺/);
		await expect(page.locator("#mpp-setter")).toBeHidden();

		// 像素标注（arrow）：label 必填 → 进入绘制 → 拖出箭头 → 完成即保存
		//（POST /s/<token>/api/roi，body 为 level-0 像素 x1/y1/x2/y2）
		await page.fill("#roi-label", "e2e-share-annotator");
		const roiReqPromise = page.waitForRequest((req) =>
			req.method() === "POST" && /^\/s\/[^/]+\/api\/roi$/.test(new URL(req.url()).pathname));
		const roiRespPromise = page.waitForResponse((resp) =>
			resp.request().method() === "POST" && /^\/s\/[^/]+\/api\/roi$/.test(new URL(resp.url()).pathname));
		await page.locator("#anno-arrow-btn").click();
		const canvasBox = await page.locator("#anno-canvas").boundingBox();
		expect(canvasBox).toBeTruthy();
		const scx = canvasBox!.x + canvasBox!.width / 2;
		const scy = canvasBox!.y + canvasBox!.height / 2;
		await page.mouse.move(scx - 50, scy - 40);
		await page.mouse.down();
		await page.mouse.move(scx + 50, scy + 40, { steps: 8 });
		await page.mouse.up();
		const roiResp = await roiRespPromise;
		expect(roiResp.status()).toBe(200);
		const savedArrow = (await roiReqPromise).postDataJSON() as {
			slide: string; type: string; x1: number; y1: number; x2: number; y2: number;
		};
		expect(savedArrow.slide).toBe(BMP_NAME);
		expect(savedArrow.type).toBe("arrow");
		for (const v of [savedArrow.x1, savedArrow.y1, savedArrow.x2, savedArrow.y2]) {
			expect(Number.isInteger(v) && v >= 0).toBe(true);
		}
		expect(Math.max(savedArrow.x1, savedArrow.x2)).toBeLessThanOrEqual(BMP_W);
		expect(Math.max(savedArrow.y1, savedArrow.y2)).toBeLessThanOrEqual(BMP_H);
		expect(Math.hypot(savedArrow.x2 - savedArrow.x1, savedArrow.y2 - savedArrow.y1))
			.toBeGreaterThanOrEqual(10);

		// reload 回放：/api/rois 返回同一像素几何 + 画布重绘
		await page.reload();
		await expect(page.locator("#viewer .openseadragon-canvas")).toBeVisible({ timeout: 20_000 });
		let replayArrows: Array<Record<string, number>> = [];
		await expect.poll(async () => {
			replayArrows = await page.evaluate(async ({ token, name }) => {
				const r = await fetch(`/s/${token}/api/rois`);
				if (!r.ok) return [];
				const rois = await r.json();
				return (rois || []).filter((x: { slide?: string; type?: string }) =>
					x.slide === name && x.type === "arrow");
			}, { token, name: BMP_NAME });
			return replayArrows.length;
		}, { timeout: 15_000 }).toBeGreaterThan(0);
		expect(replayArrows).toContainEqual(expect.objectContaining({
			x1: savedArrow.x1, y1: savedArrow.y1, x2: savedArrow.x2, y2: savedArrow.y2,
		}));
		await expect.poll(() => canvasCenterHasPixels(page, "#anno-canvas"),
			{ timeout: 15_000 }).toBe(true);

		expect(problems, problems.join("\n")).toEqual([]);
	});
});
