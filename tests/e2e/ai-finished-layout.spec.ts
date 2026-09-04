/**
 * §11 Bug B：长总结覆盖下一条消息 —— 真实 Chromium + 生产 static/style.css 布局回归。
 *
 * 静态 HTML fixture（虚构中文文本，无任何患者数据）+ 路由拦截注入仓库内的
 * 生产 style.css，不搭建登录/AI 后端；被现有 `npm run test:e2e:admin`
 * （playwright.config.ts，testDir=tests/e2e）拾取。
 *
 * 断言（任务书 §11.2，325×700 与桌面各跑一遍）：
 *   finishedBubble.boundingBox().bottom <= nextBubble.boundingBox().top
 *   finishedBubble.scrollHeight <= finishedBubble.clientHeight
 * 另断言短状态行（.ai-status.finished）不被 flex 压扁、不与下一条消息重叠，
 * 滚动到底后复测仍不重叠；虚构文字截图存 test-results/（Playwright 默认）。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

// CJS 转译下 __dirname 可用（同 admin-workbench.spec.ts）；兜底 process.cwd()
// （playwright 从仓库根启动）。
const here = typeof __dirname !== "undefined" ? __dirname : process.cwd();
const PROD_CSS = readFileSync(resolve(here, "../../static/style.css"), "utf8");
const FIXTURE_HOST = "http://pt-fixture.test";

// 虚构中文总结（纯演示文本）：>1,200 字，模拟 agent_finished 的 p.summary
function fictionalSummary(): string {
	const paras = [
		"本视野背景干净，红细胞形态大致在正常范围内，未见明显聚集。",
		"低倍扫读全片未见异常细胞浸润区，组织结构边界清晰、染色均匀。",
		"高倍复核三处可疑点，均为染色假象或组织折叠，不值得标注。",
		"该区域边缘略深染，建议复检时结合临床信息再判断是否需要随访。",
		"全片检查路径已记录，可回放每一步取景位置，便于复核与教学演示。",
	];
	let out = "";
	let i = 0;
	while (out.length < 1400) {
		out += "（" + (i + 1) + "）" + paras[i % paras.length]!;
		i += 1;
	}
	return out;
}

function fixtureHtml(): string {
	const summary = fictionalSummary();
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<link rel="stylesheet" href="/static/style.css" />
<title>ai-finished-layout fixture（虚构文本）</title>
</head>
<body style="margin:0">
<div style="position:relative;width:100vw;height:100vh">
  <div id="ai-panel" class="ai-panel" style="display:flex;">
    <div class="ai-panel-header"><span id="ai-panel-title">AI 读片助手（布局测试 fixture，虚构内容）</span></div>
    <div id="ai-trace" class="ai-trace">
      <div class="ai-msg-ts">9月4日 周五 10:00</div>
      <div class="ai-chat-bubble user tail">请客观扫读这张演示切片并总结镜下所见。</div>
      <div id="finished-summary" class="ai-chat-bubble assistant ai-md tail"><p>${summary}</p></div>
      <div id="finished-status" class="ai-status finished">本次读片已完成</div>
      <div id="next-user" class="ai-chat-bubble user tail">下一条消息：请把刚才提到的第二处可疑点和当前视野对比一下。</div>
      <div class="ai-chat-bubble assistant ai-md tail"><p>好的，下面用整片快照对比说明（虚构回复）。</p></div>
    </div>
    <div class="ai-composer"><div class="ai-composer-bar"><textarea class="ai-task" rows="1"></textarea></div></div>
  </div>
</div>
</body>
</html>`;
}

/** 静态 fixture 源：/fixture → HTML；/static/style.css → 仓库生产 CSS。 */
async function serveFixture(page: Page): Promise<void> {
	await page.route(FIXTURE_HOST + "/**", (route: Route) => {
		const url = new URL(route.request().url());
		if (url.pathname === "/static/style.css") {
			return route.fulfill({ status: 200, contentType: "text/css; charset=utf-8", body: PROD_CSS });
		}
		if (url.pathname === "/fixture") {
			return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: fixtureHtml() });
		}
		return route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
	});
}

interface LayoutMetrics {
	finished: { top: number; bottom: number };
	next: { top: number };
	status: { top: number; bottom: number; height: number };
	finishedScroll: number;
	finishedClient: number;
}

async function readMetrics(page: Page): Promise<LayoutMetrics> {
	const fb = await page.locator("#finished-summary").boundingBox();
	const nb = await page.locator("#next-user").boundingBox();
	const sb = await page.locator("#finished-status").boundingBox();
	const sc = await page.locator("#finished-summary").evaluate(
		(el) => ({ scroll: el.scrollHeight, client: el.clientHeight }),
	);
	expect(fb).toBeTruthy();
	expect(nb).toBeTruthy();
	expect(sb).toBeTruthy();
	return {
		finished: { top: fb!.y, bottom: fb!.y + fb!.height },
		next: { top: nb!.y },
		status: { top: sb!.y, bottom: sb!.y + sb!.height, height: sb!.height },
		finishedScroll: sc.scroll,
		finishedClient: sc.client,
	};
}

function expectNoOverlap(m: LayoutMetrics): void {
	// 任务书断言 1：完整总结气泡不得覆盖下一条消息
	expect(m.finished.bottom).toBeLessThanOrEqual(m.next.top + 0.5);
	// 任务书断言 2：总结气泡内部不得溢出（scrollHeight <= clientHeight）
	expect(m.finishedScroll).toBeLessThanOrEqual(m.finishedClient);
	// 短状态行不被压扁（一行 12px×1.6≈19px）、不与下一条消息重叠
	expect(m.status.height).toBeGreaterThanOrEqual(14);
	expect(m.status.bottom).toBeLessThanOrEqual(m.next.top + 0.5);
}

test.describe("§11 长总结布局（生产 CSS，虚构文本）", () => {
	for (const [name, viewport] of [
		["mobile-325x700", { width: 325, height: 700 }],
		["desktop-1280x800", { width: 1280, height: 800 }],
	] as const) {
		test(`完成总结不覆盖下一条消息（${name}）`, async ({ page }) => {
			await page.setViewportSize(viewport);
			await serveFixture(page);
			await page.goto(FIXTURE_HOST + "/fixture");
			await page.waitForSelector("#finished-summary");

			expectNoOverlap(await readMetrics(page));

			// 滚动到底复测（真实滚动状态下仍不重叠）
			await page.locator("#ai-trace").evaluate((el) => {
				el.scrollTop = el.scrollHeight;
			});
			await page.waitForTimeout(50);
			expectNoOverlap(await readMetrics(page));

			// 虚构文字截图存 test-results/（Playwright 默认目录，不入库患者数据）
			await page.screenshot({
				path: join("test-results", `ai-finished-layout-${name}.png`),
				fullPage: false,
			});
		});
	}
});
