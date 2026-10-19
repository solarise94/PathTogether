/**
 * HistoPilot 历史 branch 续聊 + 410 迟到正文回归（2026-10-18 review P2/P3）。
 *
 * 真实 Chromium + 真实 Flask/内嵌 PG + 真实 HistoPilot bundle
 * （PLUGIN_BUNDLES_DIR 指向 npm run bundle:pathtogether 产物的解压目录；
 * e2e_server._plugin_dir 命中即用真 bundle，不装桩）。AI 配置/历史列表/
 * 历史 transcript/AI 响应在测试浏览器内打桩（不调用真实模型），打桩范围
 * 与 2026-10-18 独立复核脚本（check-history-branch / check-branch-410-race）
 * 完全一致。
 *
 * 用例 1（P2 回归）：从标题栏对话列表选中历史 branch（Historical deep read）
 *   → composer 输入问题 → 点击发送。0.4.7 及更早的 bundle 在此同步抛
 *   TypeError: setThinkingRow is not a function，POST /api/ai/branch 为 0；
 *   修复后必须真正发出一次 branch POST 且无该 pageerror。
 *
 * 用例 2（P3 回归）：branch 410 的 resp.json() 是响应头之外的新异步边界。
 *   正文读取期间用真实 enterAiDraft 切入新对话（推进 selectionGeneration、
 *   abort 旧 runCtrl）→ 释放正文。修复前迟到的退役错误写进新对话；修复后
 *   必须静默丢弃（新对话 trace 无该错误、session 仍为 null、草稿文字原样）。
 *
 * 用例 3（P3 对照）：同样的 410 与挂起正文，但**不**切换会话 → 退役错误
 *   必须照常展示并正常收尾（防复核误伤正常路径）。
 *
 * 前置：PLUGIN_BUNDLES_DIR 指向真实 histopilot bundle（bundle 缺失时
 * e2e_server 会装桩，本 spec 首个用例会以「未检测到真实 bundle」失败——
 * 显式失败优于假通过）。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8",
)) as {
	userLogin: string;
	userPassword: string;
	rasterSlides: Record<"workbench", { slide_id: string; name: string }>;
};

const SID = CREDS.rasterSlides.workbench.slide_id;
const RETIRED_ERROR = "OLD BRANCH RETIRED RESPONSE";

async function login(page: Page) {
	await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
	await page.goto("/login");
	await page.fill('input[name="username"]', CREDS.userLogin);
	await page.fill('input[name="password"]', CREDS.userPassword);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.click('button[type="submit"]'),
	]);
}

/** reviewer 打桩口径：AI 配置 / 会话目录（含历史 branch）/ 其余 /api/ai/* 空应答。 */
async function stubAiBasics(page: Page) {
	await page.route("**/api/ai/**", (route) => {
		const p = new URL(route.request().url()).pathname;
		if (p === "/api/ai/config") {
			return route.fulfill({ json: { using: "platform", platform_configured: true, api_key_set: true, model: "test" } });
		}
		if (p === "/api/ai/sessions") {
			return route.fulfill({ json: { conversations: [{
				id: "sess-history-main", title: "Previous conversation", status: "finished", active: true,
				branches: [{ id: "sess-history-branch", title: "Historical deep read", annotation_id: "history-marker", status: "finished" }],
			}] } });
		}
		return route.fulfill({ json: {} });
	});
}

async function openAppAndAiPanel(page: Page) {
	await page.goto(`/app?slide=${SID}`);
	await expect(page.locator("#viewer .openseadragon-canvas")).toBeVisible({ timeout: 20_000 });
	await page.locator("#ai-btn").click();
	await expect.poll(() =>
		page.evaluate(() => !!((window as any).HistoPilot?.s?.identityLoaded)),
	).toBe(true);
}

// 默认 e2e 不装真实 bundle（e2e_server 装桩）：未指定 PLUGIN_BUNDLES_DIR 时跳过；
// 指定了却仍落到桩，首个用例照常显式失败
test.skip(!process.env.PLUGIN_BUNDLES_DIR, "需要 PLUGIN_BUNDLES_DIR 指向真实 HistoPilot bundle");

test("历史 branch 续聊：composer 发送真正产生 POST /api/ai/branch，无 setThinkingRow TypeError（P2）", async ({ page }) => {
	const errors: string[] = [];
	const posts: Array<Record<string, unknown>> = [];
	page.on("pageerror", (e) => errors.push(String(e?.message ?? e)));
	await login(page);
	await stubAiBasics(page);
	await page.route("**/api/ai/branch", (route) => {
		posts.push(route.request().postDataJSON() as Record<string, unknown>);
		return route.fulfill({
			status: 200,
			headers: { "X-AI-Session-ID": "sess-history-branch" },
			contentType: "text/event-stream",
			body: 'id: 1\nevent: branch_resumed\ndata: {"session_id":"sess-history-branch"}\n\n'
				+ 'id: 2\nevent: session_ended\ndata: {"id":"sess-history-branch","status":"finished"}\n\n',
		});
	});
	await page.route("**/api/ai/session/**", (route) => {
		const p = new URL(route.request().url()).pathname;
		const id = p.split("/").at(-1)!;
		return route.fulfill({ json: { session: { id, status: "finished", last_event_seq: 0, kind: p.endsWith("branch") ? "branch" : "main" }, transcript: [] } });
	});

	await openAppAndAiPanel(page);
	// 会话切换器已列出历史 branch（目录打桩可见）
	await expect(page.locator('#ai-session-select option[value="sess-history-branch"]')).toHaveCount(1);
	// 从标题栏对话列表选中历史 branch（reviewer 复现路径）
	await page.locator("#ai-conversation-toggle").click();
	await page.locator("#ai-conversation-list .ai-session-row")
		.filter({ hasText: "Historical deep read" }).click();
	await expect.poll(() =>
		page.evaluate(() => (window as any).HistoPilot.s.activeAiSession?.kind),
	).toBe("branch");

	await page.locator("#ai-task").fill("Continue this historical branch");
	await page.locator("#ai-start-btn").click();

	// 0.4.7 bundle：同步 TypeError: setThinkingRow is not a function，POST 恒 0
	//（本用例双断言均失败）；修复后：恰一次 POST，携带问题与标注 id，无该错误。
	await expect.poll(() => posts.length).toBe(1);
	expect(posts[0]!.annotation_id).toBe("history-marker");
	expect(posts[0]!.question).toBe("Continue this historical branch");
	expect(errors.filter((e) => e.includes("setThinkingRow"))).toEqual([]);
});

test("branch 410：resp.json() 读取期间切入新对话 → 迟到退役错误静默丢弃（P3）", async ({ page }) => {
	await login(page);
	await stubAiBasics(page);
	await openAppAndAiPanel(page);

	// 拦截 branch POST：响应头 410 立即返回，json() 挂起（模拟正文读取中）。
	await page.evaluate(() => {
		const HP = (window as any).HistoPilot;
		const original = HP.api;
		HP.api = function (url: string, options?: unknown) {
			if (url !== "/api/ai/branch") return original(url, options);
			return Promise.resolve({
				status: 410, ok: false,
				json: () => {
					(window as any).reviewWaitingForBody = true;
					return new Promise((resolve) => {
						(window as any).reviewReleaseBody = () =>
							resolve({ code: "branch_retired", error: "OLD BRANCH RETIRED RESPONSE" });
					});
				},
			});
		};
		(window as any).reviewBranchStarted =
			HP.startBranchRun("review-history-branch-root", "question in old branch");
	});
	await expect.poll(() =>
		page.evaluate(() => !!(window as any).reviewWaitingForBody),
	).toBe(true);

	// 用真实会话切换（「新会话」入口）：推进 selectionGeneration 并 abort 旧 runCtrl。
	await page.evaluate(() => (window as any).HistoPilot.enterAiDraft());
	await page.locator("#ai-task").fill("question for new conversation");
	await page.evaluate(() => (window as any).reviewReleaseBody());
	await page.waitForTimeout(300);

	// 0.4.7（P2 修复后）bundle：迟到的退役错误写进新对话 → 本断言失败；
	// 修复后：静默丢弃，新对话轨迹无该错误、仍无 session、草稿文字原样。
	const trace = await page.locator("#ai-trace").innerText();
	expect(trace).not.toContain(RETIRED_ERROR);
	const st = await page.evaluate(() => ({
		selectionGeneration: (window as any).HistoPilot.s.selectionGeneration,
		session: (window as any).HistoPilot.s.aiSessionId,
		text: (document.querySelector("#ai-task") as HTMLTextAreaElement).value,
	}));
	expect(st.session).toBe(null);
	expect(st.text).toBe("question for new conversation");
	expect(st.selectionGeneration).toBeGreaterThan(0);
});

test("branch 410 对照：不切换会话 → 退役错误照常展示（正常路径不受复核影响）", async ({ page }) => {
	await login(page);
	await stubAiBasics(page);
	await openAppAndAiPanel(page);

	await page.evaluate(() => {
		const HP = (window as any).HistoPilot;
		const original = HP.api;
		HP.api = function (url: string, options?: unknown) {
			if (url !== "/api/ai/branch") return original(url, options);
			return Promise.resolve({
				status: 410, ok: false,
				json: () => {
					(window as any).reviewWaitingForBody = true;
					return new Promise((resolve) => {
						(window as any).reviewReleaseBody = () =>
							resolve({ code: "branch_retired", error: "OLD BRANCH RETIRED RESPONSE" });
					});
				},
			});
		};
		HP.startBranchRun("review-history-branch-root", "question in old branch");
	});
	await expect.poll(() =>
		page.evaluate(() => !!(window as any).reviewWaitingForBody),
	).toBe(true);
	await page.evaluate(() => (window as any).reviewReleaseBody());

	// 未切换：服务端退役原因照常进当前对话轨迹。
	await expect(page.locator("#ai-trace")).toContainText(RETIRED_ERROR);
});
