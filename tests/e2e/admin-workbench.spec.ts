/**
 * 管理工作台真实 Chromium E2E（一次性修复包 F，§10.2 必测流程 1–11）。
 *
 * 与 Vitest 单元层职责不同：这里起真实 Flask + PostgreSQL + 仓库内 admin
 * bundle，用真实浏览器验证 opaque sandbox iframe、CSP、HTML 解析、postMessage
 * 握手与六个页面的端到端行为。凭据经 E2E_CREDS_FILE（runner 进程环境/
 * 临时文件），不出现在日志、断言与截图之外。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as {
	baseUrl: string; ownerLogin: string; ownerPassword: string;
	userLogin: string; userPassword: string;
};

/** 收集未允许的浏览器错误（§10.2-10：console error / pageerror / CSP violation / 失败请求）。
 * ignoreApi（模块级、beforeEach 重置）：用例可声明「预期失败」的 admin API
 * 路径（如前置条件 400 的错误态用例）——该 fetch 的 Failed to load resource
 * 不计入问题清单，错误本身由用例的状态文案断言覆盖。 */
const ignoreApi: string[] = [];
function attachErrorCollectors(page: Page) {
	const problems: string[] = [];
	// favicon 是浏览器内置请求、不属于工作台资源面（403/404 属平台预期）
	const isFavicon = (url: string) => new URL(url).pathname === "/favicon.ico";
	const isIgnoredApi = (url: string) => {
		try {
			return ignoreApi.includes(new URL(url).pathname);
		} catch {
			return false;
		}
	};
	page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
	page.on("console", (msg) => {
		if (msg.type() === "error") {
			const text = msg.text();
			if (/favicon/i.test(text)) return;
			// 主文档导航自身的非 2xx（如 test 2 预期的 403）由用例断言响应状态，
			// 不计入「未允许错误」；子资源的 403/404 仍算（预期失败的 admin API
			// fetch 由 ignoreApi 显式豁免）
			const loc = msg.location()?.url || "";
			if (/Failed to load resource/.test(text) && loc) {
				try {
					if (new URL(loc).pathname === new URL(page.url()).pathname) return;
				} catch { /* URL 解析失败按未允许处理 */ }
				if (isIgnoredApi(loc)) return;
			}
			problems.push(`console.error: ${text}`);
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
	await page.fill("#username", loginId);
	await page.fill("#password", password);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.click("#login-submit"),
	]);
}

const HOST_READY = `#admin-host-state-ready-marker`;
// 宿主 ready 的稳定选择器：状态容器 data 属性（§8.1 状态机）
function hostStatus(page: Page) {
	return page.locator("#admin-host-status");
}

test.describe.configure({ mode: "serial" });

test.describe("管理工作台 Chromium E2E（§10.2）", () => {
	let problems: string[] = [];

	test.beforeAll(async ({ browser }) => {
		void browser;
	});

	test.beforeEach(async ({ page }) => {
		ignoreApi.length = 0; // 每用例重置「预期失败」豁免清单
		problems = attachErrorCollectors(page);
	});

	test.afterEach(async () => {
		// §10.2-10：每个用例后断言无未允许错误（失败资源/console/pageerror/CSP）
		expect(problems, problems.join("\n")).toEqual([]);
	});

	test("1. anonymous /admin redirects to login", async ({ page }) => {
		await page.goto("/admin");
		await expect(page).toHaveURL(/\/login\?next=%2Fadmin|\/login\?next=/);
	});

	test("2. plain user gets 403 without admin content", async ({ page }) => {
		await login(page, CREDS.userLogin, CREDS.userPassword);
		const resp = await page.goto("/admin");
		expect(resp?.status()).toBe(403);
		expect(await page.locator("#admin-plugin-frame").count()).toBe(0);
		await expect(page.locator("body")).toContainText("403");
	});

	test("3-4. owner enters /admin; host reaches ready with granted permission count", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		// 显示已授予权限数（manifest 全量申请 > 0）
		await expect(hostStatus(page)).toContainText(/管理能力 \d+ 项/);
		const match = (await hostStatus(page).textContent())?.match(/管理能力 (\d+) 项/);
		expect(Number(match?.[1])).toBeGreaterThan(0);
		void HOST_READY;
	});

	test("5. iframe CSS actually applies (computed style, not native buttons)", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		const activeNav = frame.locator(".adm-nav-btn--active");
		await expect(activeNav).toBeVisible();
		// CSP 生效且样式表已应用：激活导航按钮是品牌蓝圆角，而非默认 button
		await expect
			.poll(async () => activeNav.evaluate((el) => getComputedStyle(el).backgroundColor))
			.toBe("rgb(0, 122, 255)");
		const card = frame.locator(".adm-card").first();
		await expect
			.poll(async () => card.evaluate((el) => getComputedStyle(el).borderRadius))
			.toBe("10px");
	});

	test("6. users page lists the seeded user with key fields", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody")).toContainText(
			"E2E 普通用户", { timeout: 10_000 });
		// 关键字段：角色（次要列）、启用状态；掩码登录账号已收进抽屉（§4.3）
		await expect(frame.locator("#adm-users-tbody")).toContainText("user");
		await expect(frame.locator("#adm-users-tbody")).toContainText("启用");
		await expect(frame.locator("#adm-users-table")).toContainText("额度剩余");
		// 详情抽屉可打开：额度主视图（total/window 形态）+ 技术细节折叠区
		await frame.locator("#adm-users-tbody button", { hasText: "详情" }).first().click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("额度来源");
		await expect(frame.locator("#adm-drawer-body")).toContainText("技术细节");
		await frame.locator("#adm-drawer-close").click();
		await expect(frame.locator("#adm-user-drawer")).toBeHidden();
	});

	test("7. invites page reaches terminal state with no attribution content (wave 2 §4.4)", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		// wave 2：导航名为「邀请」（slug 保持 invites）
		await expect(frame.locator('.adm-nav-btn[data-page="invites"]'))
			.toHaveText("邀请");
		await frame.locator('.adm-nav-btn[data-page="invites"]').click();
		// 终态断言：限定时间内离开 loading 进入 ready（注册模式摘要 + 邀请列表）
		await expect
			.poll(async () =>
				frame.locator("#adm-state-invites").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		await expect(frame.locator("#adm-state-invites")).not.toContainText("加载中");
		// 注册模式只读摘要 + 「去设置修改」跳转（无第二套写控件）
		await expect(frame.locator("#adm-invite-mode")).toContainText("closed");
		await expect(frame.locator("#adm-invite-goto-settings-btn")).toBeVisible();
		// 断言限定在邀请页 section（设置页 section 仍在 DOM 中但 hidden）
		const invitesHtml = await frame.locator("#adm-page-invites").innerHTML();
		expect(invitesHtml).not.toContain('id="adm-regmode-select"');
		expect(invitesHtml).not.toContain("adm-regmode-save-btn");
		// 来源/归因内容整体退役：漏斗、用户来源明细、campaign/source 输入不存在
		expect(await frame.locator("#adm-acq-funnel-table").count()).toBe(0);
		expect(await frame.locator("#adm-acq-users-table").count()).toBe(0);
		expect(await frame.locator("#adm-invite-cohort").count()).toBe(0);
		expect(await frame.locator("#adm-invite-source").count()).toBe(0);
		expect(await frame.locator("#adm-invite-campaign").count()).toBe(0);
		const pageText = await frame.locator("#adm-page-invites").textContent();
		expect(pageText).not.toContain("来源漏斗");
		expect(pageText).not.toContain("first touch");
		expect(pageText).not.toContain("首次 AI");
	});

	const bridgePages: Array<[string, RegExp]> = [
		["billing", /ready|error/],
		["plugins", /ready/],
		["audit", /ready|empty/],
		["settings", /ready|error/],
	];
	for (const [pageKey, allowedState] of bridgePages) {
		test(`8. ${pageKey} page switches and completes a real bridge request`, async ({ page }) => {
			await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
			await page.goto("/admin");
			await expect(hostStatus(page)).toHaveAttribute(
				"data-admin-host-state", "ready", { timeout: 5000 });
			const frame = page.frameLocator("#admin-plugin-frame");
			await frame.locator(`.adm-nav-btn[data-page="${pageKey}"]`).click();
			// 至少一次真实 bridge 请求完成：页级状态离开 loading 进入终态
			await expect
				.poll(async () =>
					frame.locator(`#adm-state-${pageKey}`).getAttribute("data-page-state"))
				.toMatch(allowedState);
		});
	}

	test("10a. 设置页：注册模式保存与前置条件错误态（批次 D §6.1）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// 注册模式展示（closed / invite_only；public 灰显不可选）
		await expect(frame.locator("#adm-regmode-info")).toContainText("closed");
		await expect(frame.locator("#adm-regmode-select option[value=public]"))
			.toBeDisabled();
		// 保存 closed：成功态（服务端校验 + 写审计）；保存后页面自动刷新，
		// 等刷新收敛再操作下拉（renderSettings 会以服务端值回填 select）
		await frame.locator("#adm-regmode-select").selectOption("closed");
		await frame.locator("#adm-regmode-save-btn").click();
		await expect(frame.locator("#adm-regmode-status"))
			.toContainText("注册模式已提交为 closed", { timeout: 10_000 });
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// invite_only：本地 HTTP 不满足前置条件 → 可见错误态（fail-closed）。
		// 该 400 是用例预期的错误态：fetch 的 console 报错按 ignoreApi 豁免，
		// 错误本身由下方状态文案断言覆盖。
		ignoreApi.push("/api/admin/v1/settings/registration");
		await frame.locator("#adm-regmode-select").selectOption("invite_only");
		await frame.locator("#adm-regmode-save-btn").click();
		await expect(frame.locator("#adm-regmode-status"))
			.toContainText("前置条件", { timeout: 10_000 });
		await expect(frame.locator("#adm-regmode-status"))
			.toContainText("registration_preconditions_failed");
	});

	test("10b. 设置页：消费额度策略保存（三键拆分，CNY 输入 → nano wire）与 enforcement 展示", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// wave 2 三键拆分：注册用户默认总额度 / Demo 每周 / Owner 每月；
		// Demo 默认 50 CNY 回显（来自 spend 设置响应新键 *_nano_cny）；
		// enforcement 恒 shadow（批次 C2 验收前）
		await expect(frame.getByLabel("注册用户默认总额度（CNY，可选）")).toBeVisible();
		await expect(frame.locator("#adm-spend-demo-week")).toHaveValue("50");
		await expect(frame.locator("#adm-spend-mode")).toHaveValue("shadow");
		// 保存 51 CNY（wire 十进制字符串 51000000000 nano）
		await frame.locator("#adm-spend-demo-week").fill("51");
		await frame.locator("#adm-spend-save-btn").click();
		await expect(frame.locator("#adm-spend-status"))
			.toContainText("已保存 1 项", { timeout: 10_000 });
		// §4.7：信息区主视图只回显两位小数 51.00 CNY；原始 nano 在 raw 展开区
		await expect(frame.locator("#adm-spend-info"))
			.toContainText("51.00 CNY", { timeout: 10_000 });
		const spendRaw = frame.locator("#adm-spend-raw")
			.locator("details.adm-raw-values").first();
		await spendRaw.locator("summary").click();
		await expect(spendRaw).toContainText("51000000000");
	});

	test("10c. 设置页：Demo/Owner 立即调整当前周期（固定主体 + 二次确认 + 影响展示）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// 窗口摘要只有 Demo/Owner 两张：Demo 当前窗口额度仍是策略更新前的
		// 快照 50 CNY（默认修改不追溯已开窗口，§1.1）；§4.7 两位小数显示
		const summaries = frame.locator("#adm-window-summaries");
		await expect(summaries).toBeVisible();
		await expect(summaries).toContainText("Demo（周窗口）");
		await expect(summaries).toContainText("Owner（月窗口）");
		// 摘要卡 kv 行文本相邻拼接（dt「额度」+ dd「50.00 CNY」）
		await expect(summaries).toContainText("额度50.00 CNY");
		await expect(summaries).toContainText("剩余50.00 CNY");
		// Demo「立即调整当前周期」默认折叠：展开后填新额度 52 → 页内二次确认
		await expect(frame.locator("#adm-win-demo-form")).toBeHidden();
		await frame.locator("#adm-win-demo-box").locator("summary").click();
		await expect(frame.locator("#adm-win-demo-form")).toBeVisible();
		await frame.locator("#adm-win-demo-limit").fill("52");
		await frame.locator("#adm-win-demo-adjust-btn").click();
		await expect(frame.locator("#adm-win-demo-confirm")).toBeVisible();
		await expect(frame.locator("#adm-win-demo-confirm"))
			.toContainText("Demo（全站共享周窗口）");
		await expect(frame.locator("#adm-win-demo-confirm"))
			.toContainText("已消费 0.00 CNY / 预占 0.00 CNY 不回退");
		await expect(frame.locator("#adm-win-demo-confirm"))
			.toContainText("新剩余 52.00 CNY");
		// 确认执行 → 成功态；摘要卡立即刷新为 52 CNY（不等下个窗口）
		await frame.locator("#adm-win-demo-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-win-demo-status"))
			.toContainText("已调整", { timeout: 10_000 });
		await expect(summaries)
			.toContainText("额度52.00 CNY", { timeout: 10_000 });
		// Owner 同一桥方法：展开 Owner 折叠区，调 1002 → 确认 → 成功
		await frame.locator("#adm-win-owner-box").locator("summary").click();
		await frame.locator("#adm-win-owner-limit").fill("1002");
		await frame.locator("#adm-win-owner-adjust-btn").click();
		await expect(frame.locator("#adm-win-owner-confirm")).toBeVisible();
		await expect(frame.locator("#adm-win-owner-confirm"))
			.toContainText("Owner（月窗口）");
		await frame.locator("#adm-win-owner-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-win-owner-status"))
			.toContainText("已调整", { timeout: 10_000 });
		await expect(summaries)
			.toContainText("额度1002.00 CNY", { timeout: 10_000 });
	});

	test("10d. 用户页：初始总额度进高级折叠 + 抽屉「设置总额度」（CAS、不重置已用）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 普通用户", { timeout: 10_000 });
		// §4.4：创建用户默认折叠——入口 summary 可见、表单隐藏，展开后填写
		const createBox = frame.locator("#adm-users-create-box");
		await expect(createBox.locator("summary").first()).toBeVisible();
		await expect(frame.locator("#adm-users-create-form")).toBeHidden();
		await createBox.locator("summary").first().click();
		await expect(frame.locator("#adm-users-create-form")).toBeVisible();
		// wave 2：初始总额度在默认折叠的「高级：单独总额度」里
		await expect(frame.locator("#adm-users-new-limit")).toBeHidden();
		await frame.locator("#adm-users-new-limit-box").locator("summary").click();
		await expect(frame.locator("#adm-users-new-limit")).toBeVisible();
		// 创建带 3.5 CNY 初始总额度的用户（建号 + total allowance + audit 同事务）
		await frame.locator("#adm-users-new-login").fill("e2e-limited@pt.test");
		await frame.locator("#adm-users-new-display").fill("E2E 限额用户");
		await frame.locator("#adm-users-new-password").fill("e2e-limited-pass-123456");
		await frame.locator("#adm-users-new-limit").fill("3.5");
		await frame.locator("#adm-users-create-btn").click();
		await expect(frame.locator("#adm-users-create-status"))
			.toContainText("初始总额度 3.50 CNY", { timeout: 10_000 });
		// 详情抽屉：金额主视图（总额度/累计已用/可用金额/额度来源），两位小数
		const row = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("总额度");
		await expect(frame.locator("#adm-drawer-body")).toContainText("3.50 CNY");
		await expect(frame.locator("#adm-drawer-body")).toContainText("累计已用");
		await expect(frame.locator("#adm-drawer-body")).toContainText("可用金额");
		await expect(frame.locator("#adm-drawer-body")).toContainText("剩余 3.50 CNY");
		// 原始 nano 在抽屉「技术细节」折叠区（§4.3）
		await frame.locator("#adm-drawer-body details.adm-drawer-tech")
			.locator("summary").click();
		await expect(frame.locator("#adm-drawer-body details.adm-drawer-tech"))
			.toContainText("3500000000");
		// 抽屉内唯一金额动作「设置总额度（CNY）」：单次 CAS（expected_version）
		await frame.locator("#adm-total-limit-input").fill("2.5");
		await frame.locator("#adm-drawer-body button", { hasText: "设置总额度" }).click();
		await expect(frame.locator("#adm-drawer-confirm")).toBeVisible();
		await expect(frame.locator("#adm-drawer-confirm"))
			.toContainText("2.50 CNY（2500000000 nano）");
		await expect(frame.locator("#adm-drawer-confirm"))
			.toContainText("不清零、不重置");
		await frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已设置总额度 2.50 CNY", { timeout: 10_000 });
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("不重置已用金额", { timeout: 10_000 });
		await frame.locator("#adm-drawer-close").click();
		await expect(frame.locator("#adm-user-drawer")).toBeHidden();
		// 表内剩余列同步刷新为 2.50 CNY（绝对总上限已更新）
		const row2 = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await expect(row2).toContainText("剩余 2.50 CNY", { timeout: 10_000 });
	});

	test("10e. 金额用尽后的可见状态：抽屉把总额度存为 0 → 剩余显示已用尽", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 限额用户", { timeout: 10_000 });
		// 抽屉编辑器把总额度存为 0（单次 CAS：只改 limit，不清零已用）
		const row = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await frame.locator("#adm-total-limit-input").fill("0");
		await frame.locator("#adm-drawer-body button", { hasText: "设置总额度" }).click();
		await expect(frame.locator("#adm-drawer-confirm")).toBeVisible();
		await frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已设置总额度 0.00 CNY", { timeout: 10_000 });
		await frame.locator("#adm-drawer-close").click();
		// 用户表「额度剩余」列显示短文案「已用尽」——不再有长拒绝说明
		const row2 = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await expect(row2).toContainText("已用尽", { timeout: 10_000 });
		// 抽屉金额主视图同样「已用尽」
		await row2.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已用尽", { timeout: 10_000 });
		await expect(frame.locator("#adm-drawer-body"))
			.not.toContainText("下次预占将被拒绝");
		await frame.locator("#adm-drawer-close").click();
	});

	test("10f. 邀请页：初始总额度模板创建 + 明文码仅一次 + 列表展示（新契约字段）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="invites"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-invites").getAttribute("data-page-state"))
			.not.toBe("loading", { timeout: 10_000 });
		// §4.4：创建邀请默认折叠——展开入口后填写；初始总额度在高级折叠里
		const inviteBox = frame.locator("#adm-invite-create-box");
		await expect(frame.locator("#adm-invite-create-form")).toBeHidden();
		await inviteBox.locator("summary").first().click();
		await expect(frame.locator("#adm-invite-create-form")).toBeVisible();
		await expect(frame.locator("#adm-invite-limit")).toBeHidden();
		await frame.locator("#adm-invite-limit-box").locator("summary").click();
		await expect(frame.locator("#adm-invite-limit")).toBeVisible();
		await frame.locator("#adm-invite-login").fill("e2e-inv@pt.test");
		await frame.locator("#adm-invite-limit").fill("2.5");
		await frame.locator("#adm-invite-create-btn").click();
		// 明文邀请码仅此一次展示（token box 可见且非空）
		await expect(frame.locator("#adm-invite-create-status"))
			.toContainText("明文邀请码只显示这一次", { timeout: 10_000 });
		await expect(frame.locator("#adm-invite-token-box")).toBeVisible();
		await expect(frame.locator("#adm-invite-token")).not.toBeEmpty();
		// 列表：初始总额度列 2.50 CNY（两位小数）
		await expect(frame.locator("#adm-invites-tbody"))
			.toContainText("2.50 CNY", { timeout: 10_000 });
		// 注册模式状态展示保持（只读摘要卡）
		await expect(frame.locator("#adm-invite-mode")).toContainText("closed");
	});

	test("9. plugin reload re-establishes ready with a fresh nonce", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody")).toContainText(
			"E2E 普通用户", { timeout: 10_000 });
		// reload：宿主状态回 loading → waiting_handshake → ready（新 nonce；
		// 旧请求/旧 nonce 由宿主侧桥校验丢弃，单元层已锁定）。新 iframe 文档
		// 回初始页（overview），重新进入用户页完成一次全新加载。
		await page.click("#admin-reload-btn");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 10_000 });
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody")).toContainText(
			"E2E 普通用户", { timeout: 10_000 });
	});

	test("11. desktop + mobile screenshots (CI artifacts, test-only data)", async ({ page }, testInfo) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="overview"]').click();
		await frame.locator("#adm-ov-kpis").waitFor({ timeout: 10_000 });
		await testInfo.attach("admin-desktop-1440", {
			body: await page.screenshot({ fullPage: false }),
			contentType: "image/png",
		});
		// 设置页桌面截图（批次 D：注册模式/金额策略/运行时参数/当前窗口）
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		await testInfo.attach("admin-settings-desktop-1440", {
			body: await page.screenshot({ fullPage: true }),
			contentType: "image/png",
		});
		// 用户详情抽屉（当前月 limit/spent/reserved/remaining + 覆盖编辑器）
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 普通用户", { timeout: 10_000 });
		await frame.locator("#adm-users-tbody tr", { hasText: "E2E 普通用户" })
			.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await testInfo.attach("admin-user-drawer-desktop-1440", {
			body: await page.screenshot({ fullPage: false }),
			contentType: "image/png",
		});
		await frame.locator("#adm-drawer-close").click();
		// 390px 手机：导航抽屉可展开、设置页可达（窄屏无截断/重叠的目检材料）
		await page.setViewportSize({ width: 390, height: 844 });
		await frame.locator("#adm-nav-toggle").click();
		await expect(frame.locator("#adm-nav.adm-nav--open")).toBeVisible();
		await testInfo.attach("admin-mobile-390", {
			body: await page.screenshot({ fullPage: false }),
			contentType: "image/png",
		});
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		await testInfo.attach("admin-settings-mobile-390", {
			body: await page.screenshot({ fullPage: true }),
			contentType: "image/png",
		});
	});
});

// --------------------------------------------------------------------------- //
// 2026-09-01 管理工作台 UI 升级 Chromium 验收（review-2026-09-01-admin-ui.md
// 批次 E）。真实 Chromium 渲染下断言语义/DOM/终态/computed style；截图保存到
// 评审目录作为附加证据（after-*.png，测试数据本身已脱敏：掩码账号、临时凭据）。
// --------------------------------------------------------------------------- //

const REVIEW_DIR = join(dirname(fileURLToPathSafe()), "..", "..",
  "review-2026-09-01-admin-ui");

/** CJS 转译下 __dirname 可用；兜底 process.cwd()（playwright 从仓库根启动）。 */
function fileURLToPathSafe(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return typeof __dirname !== "undefined" ? __dirname : process.cwd();
  } catch {
    return process.cwd();
  }
}

/** §4.3/批次 E：宿主文档 + 插件 iframe 文档都无水平溢出——390px 适配不靠
 *  横向滚动。iframe 是 sandbox opaque origin，宿主 JS 读不到 contentDocument，
 *  因此经 Playwright 的 frame 隔离世界进入 iframe 文档测量（P0-2）。 */
async function assertNoHorizontalOverflow(page: Page, label: string) {
  const hostOverflow = await page.evaluate(() =>
    document.documentElement.scrollWidth -
    document.documentElement.clientWidth);
  expect(hostOverflow,
    `${label}: host horizontal overflow ${hostOverflow}px`).toBeLessThanOrEqual(1);
  const frameOverflow = await page.frameLocator("#admin-plugin-frame")
    .locator("html").evaluate((el) =>
      (el as HTMLElement).scrollWidth - (el as HTMLElement).clientWidth);
  expect(frameOverflow,
    `${label}: plugin iframe horizontal overflow ${frameOverflow}px`)
    .toBeLessThanOrEqual(1);
}

/** 保存脱敏截图到评审目录（after 前缀 = 升级后证据）。 */
async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(REVIEW_DIR, name), fullPage: false });
}

async function gotoAdminReady(page: Page) {
  await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
  await page.goto("/admin");
  await expect(hostStatus(page)).toHaveAttribute(
    "data-admin-host-state", "ready", { timeout: 5000 });
}

test.describe("UI 升级 2026-09-01 — 桌面 1440×900（批次 E）", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

	test("12. overview: 两位小数 CNY KPI、身份行、无 turn 卡、站点卡条件渲染", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="overview"]').click();
		await expect(frame.locator("#adm-ov-kpis")).toBeVisible({ timeout: 10_000 });
		// §4.9：当前身份收成一行次要信息
		await expect(frame.locator("#adm-actor-line")).toContainText("当前身份：owner");
		// §4.7：KPI 主视图只有两位小数 CNY（金额 KPI 不含 nano 长串）
		await expect(frame.locator("#adm-ov-kpis")).toContainText("CNY");
		const kpiText = await frame.locator("#adm-ov-kpis").textContent();
		expect(kpiText).not.toContain("nano");
		// wave 2：turn 冻结历史卡（概览与费用页）整体不存在
		expect(await frame.locator("#adm-ov-turn-box").count()).toBe(0);
		expect(await frame.locator("#adm-ov-turn").count()).toBe(0);
		// 正常状态无告警卡（unpriced=0 / 余额可用时）
		const overviewHtml = await frame.locator("#adm-page-overview").innerHTML();
		expect(overviewHtml).not.toContain("adm-ov-turn-box");
		expect(overviewHtml).not.toContain("已退役 · 冻结历史");
		// 站点访问卡：D2 已发布且 siteStats 可达 → 可见；否则整卡隐藏
		//（两种都是合法终态，不允许出现「卡可见但报错」）
		const siteCard = frame.locator("#adm-site-card");
		const siteCount = await siteCard.count();
		if (siteCount > 0 && (await siteCard.isVisible())) {
			// 指标命名必须是「日去重次数」口径（帮助文案须明确否定「独立用户数」）
			await expect(siteCard).toContainText("匿名访客日去重次数");
			await expect(siteCard).toContainText("不是独立用户数");
		}
		await assertNoHorizontalOverflow(page, "overview-1440");
		await shot(page, "after-1440-overview.png");
	});

	test("13. users: 5 列表头（额度剩余）+ 创建折叠；限额户展示已用尽", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		// 终态：ready（不残留 loading）
		await expect
			.poll(async () => frame.locator("#adm-state-users").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		const table = frame.locator("#adm-users-table");
		await expect(table).toContainText("额度剩余");
		await expect(table).not.toContainText("本月用量");
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 普通用户", { timeout: 10_000 });
		// 剩余列覆盖边界语义之一（10e 已把限额户总额度存 0 → 已用尽）
		const limited = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await expect(limited).toContainText("已用尽");
		// §4.3：每行只回答「额度剩余」一个数字语义；5 个单元格、无用量条、
		// 无已消费/预占双行
		const rows = await frame.locator("#adm-users-tbody").evaluate((tbody) =>
			Array.from(tbody.querySelectorAll("tr")).map((tr) => ({
				text: tr.textContent || "",
				cells: tr.querySelectorAll("td").length,
				meters: tr.querySelectorAll(".adm-usage-meter").length,
			})));
		expect(rows.length).toBeGreaterThan(0);
		for (const row of rows) {
			expect(row.text, "user row must answer 额度剩余 semantics")
				.toMatch(/剩余|已用尽|超支|不可用|契约错误/);
			expect(row.cells, "desktop users table has exactly 5 columns").toBe(5);
			expect(row.meters, "no usage meter in simplified table").toBe(0);
			expect(row.text).not.toContain("已消费");
			expect(row.text).not.toContain("预占");
			expect(row.text).not.toContain("下次预占将被拒绝");
		}
		// 创建用户默认折叠
		await expect(frame.locator("#adm-users-create-form")).toBeHidden();
		await assertNoHorizontalOverflow(page, "users-1440");
		await shot(page, "after-1440-users.png");
	});

	test("14. drawer: 焦点进关闭钮、Tab 圈定、遮罩/Esc 关闭、焦点恢复详情按钮", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 普通用户", { timeout: 10_000 });
		const detailBtn = frame.locator("#adm-users-tbody tr", { hasText: "E2E 普通用户" })
			.locator("button", { hasText: "详情" });
		await detailBtn.click();
		const drawer = frame.locator("#adm-user-drawer");
		await expect(drawer).toBeVisible();
		// §4.10：打开后焦点进入关闭按钮
		await expect(frame.locator("#adm-drawer-close")).toBeFocused();
		// §4.10：Tab/Shift+Tab 圈定在抽屉内（多轮）
		for (let i = 0; i < 12; i++) {
			await page.keyboard.press(i % 2 ? "Shift+Tab" : "Tab");
			const inside = await drawer.evaluate((el) => {
				const d = el.ownerDocument;
				return d !== null && d.activeElement !== null &&
					el.contains(d.activeElement);
			});
			expect(inside, `tab cycle ${i}`).toBe(true);
		}
		// 抽屉内按钮语义：普通操作在前，危险操作独立区（§4.5）；金额主视图
		// 在技术细节之前；余额/caps 一律不出现（wave 2 §4.3）
		const drawerHtml = await frame.locator("#adm-drawer-body").innerHTML();
		expect(drawerHtml).not.toContain("打开账本");
		expect(drawerHtml).not.toContain("金额余额");
		expect(drawerHtml.toLowerCase()).not.toContain("cap");
		expect(drawerHtml.indexOf("身份预览")).toBeLessThan(drawerHtml.indexOf("危险操作"));
		expect(drawerHtml.indexOf("额度来源")).toBeLessThan(drawerHtml.indexOf("技术细节"));
		// 技术细节默认折叠：details 未展开时其内容不可见
		await expect(frame.locator("#adm-drawer-body details.adm-drawer-tech")
			.locator("dl").first()).toBeHidden();
		// 抽屉打开状态截图（统一 form/label、raw 详情、危险区）
		await assertNoHorizontalOverflow(page, "drawer-1440");
		await shot(page, "after-1440-user-drawer.png");
		// 遮罩点击可关闭
		await frame.locator("#adm-drawer-mask").click({ position: { x: 200, y: 300 } });
		await expect(drawer).toBeHidden();
		// §4.10：焦点恢复到触发「详情」按钮
		await expect(detailBtn).toBeFocused();
		// Esc 关闭（再开一次验证）
		await detailBtn.click();
		await expect(drawer).toBeVisible();
		await page.keyboard.press("Escape");
		await expect(drawer).toBeHidden();
		await expect(detailBtn).toBeFocused();
	});

	test("15. settings: label 可定位、enforcement 折叠危险开关、窗口摘要卡、保存置底", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () => frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// §4.1：label 可定位（getByLabel 经 label[for] 命中控件）；wave 2 改名
		await expect(frame.getByLabel("注册用户默认总额度（CNY，可选）")).toBeVisible();
		await expect(frame.getByLabel("Demo 每周金额（CNY，可选）")).toBeVisible();
		await expect(frame.getByLabel("Owner 每月金额（CNY，可选）")).toBeVisible();
		await expect(frame.getByLabel("注册用户单任务安全上限（可选）")).toBeVisible();
		// 自带 API 步数上限已从 UI 删除
		expect(await frame.locator("#adm-rt-ownsteps").count()).toBe(0);
		// §5.5：Demo/Owner「立即调整当前周期」折叠入口（默认折叠，无用户下拉）
		await expect(frame.locator("#adm-win-demo-box summary")).toBeVisible();
		await expect(frame.locator("#adm-win-owner-box summary")).toBeVisible();
		await expect(frame.locator("#adm-win-demo-form")).toBeHidden();
		await expect(frame.locator("#adm-win-owner-form")).toBeHidden();
		expect(await frame.locator("#adm-window-subject").count()).toBe(0);
		// §5.5：enforcement 危险开关默认折叠，展开后才可见
		await expect(frame.getByLabel("enforcement 模式（危险开关）")).toBeHidden();
		await frame.locator("#adm-spend-enforcement-box").locator("summary").click();
		await expect(frame.getByLabel("enforcement 模式（危险开关）")).toBeVisible();
		// 切到 all 出警示卡、切回 shadow 隐藏
		await frame.locator("#adm-spend-mode").selectOption("all");
		await expect(frame.locator("#adm-spend-mode-warning")).toBeVisible();
		await frame.locator("#adm-spend-mode").selectOption("shadow");
		await expect(frame.locator("#adm-spend-mode-warning")).toBeHidden();
		// §4.7：保存按钮在分组底部（独立动作行），不与输入同排
		await expect(frame.locator("#adm-spend-save-btn")).toBeVisible();
		const formHtml = await frame.locator("#adm-spend-form").innerHTML();
		expect(formHtml).not.toContain("adm-spend-save-btn");
		await assertNoHorizontalOverflow(page, "settings-1440");
		await shot(page, "after-1440-settings.png");
	});

	test("16. billing（费用页）：KPI + [仅异常]告警 + Demo 卡 + 三页内标签；误导入口不存在", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="billing"]').click();
		await expect
			.poll(async () => frame.locator("#adm-state-billing").getAttribute("data-page-state"))
			.toMatch(/ready|error/, { timeout: 10_000 });
		// wave 2 结构：KPI 行 + Demo 消耗卡 + 费用明细（三标签）
		await expect(frame.locator("#adm-bill-kpis")).toBeVisible();
		await expect(frame.locator("#adm-bill-kpis")).toContainText("供应商余额");
		await expect(frame.locator("#adm-bill-kpis")).toContainText("User 累计已用");
		await expect(frame.locator("#adm-bill-kpis")).toContainText("Demo 本周已用");
		await expect(frame.locator("#adm-bill-kpis")).toContainText("未计价");
		await expect(frame.locator("#adm-demo-card")).toBeVisible();
		await expect(frame.locator("#adm-tab-usage")).toBeVisible();
		await expect(frame.locator("#adm-tab-ledger")).toBeVisible();
		await expect(frame.locator("#adm-tab-unpriced")).toBeVisible();
		expect(await frame.locator("#adm-tabpanel-detail").count()).toBe(1);
		// 误导入口整体不存在（人工调整/caps/历史影子/turn legacy）
		for (const gone of ["#adm-adjust-card", "#adm-acct-user", "#adm-caps-form",
			"#adm-legacy-card", "#adm-turn-legacy-card"]) {
			expect(await frame.locator(gone).count(), gone).toBe(0);
		}
		// unpriced=0（本仓种子数据）时无红框告警卡
		await expect(frame.locator("#adm-bill-alert")).toBeHidden();
		// 页内标签键盘可达：默认模型调用 → ArrowRight 切到账务流水
		await expect(frame.locator("#adm-tab-usage")).toHaveAttribute("aria-selected", "true");
		await frame.locator("#adm-tab-usage").click();
		await frame.locator("#adm-tab-usage").press("ArrowRight");
		await expect(frame.locator("#adm-tab-ledger")).toHaveAttribute("aria-selected", "true");
		await expect(frame.locator("#adm-ledger-section")).toBeVisible();
		await expect(frame.locator("#adm-usage-section")).toBeHidden();
		// 切到计费异常：0 条时中性空态（无红框语义）
		await frame.locator("#adm-tab-ledger").press("ArrowRight");
		await expect(frame.locator("#adm-tab-unpriced")).toHaveAttribute("aria-selected", "true");
		await expect(frame.locator("#adm-unpriced-section")).toContainText("当前没有未计价事件");
		// 供应商余额刷新为次要按钮
		await expect(frame.locator("#adm-balance-refresh-btn")).toHaveClass(/adm-btn-secondary/);
		await assertNoHorizontalOverflow(page, "billing-1440");
		await shot(page, "after-1440-billing.png");
	});

  test("17. plugins: 每行至多一个实心 danger、轮换凭证 danger-outline", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="plugins"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-plugins").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    const row = frame.locator("#adm-plugins-tbody tr").first();
    await expect(row.locator("button", { hasText: "轮换凭证" }))
      .toHaveClass(/adm-btn-danger-outline/);
    // 实心 danger 每行至多一个（启用安装行：停用 = 唯一实心 danger）
    const solidDanger = await row.locator("button.adm-btn-danger").count();
    expect(solidDanger).toBeLessThanOrEqual(1);
    await assertNoHorizontalOverflow(page, "plugins-1440");
    await shot(page, "after-1440-plugins.png");
  });

  test("18. audit: 终态 ready/empty + 人类摘要 + 原始详情不丢", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="audit"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-audit").getAttribute("data-page-state"))
      .toMatch(/ready|empty/, { timeout: 10_000 });
    await expect(frame.locator("#adm-state-audit")).not.toContainText("加载中");
    // 前面用例已产生审计（用户创建/设置保存）→ ready 且 detail 有摘要 + 原始详情
    const tbody = frame.locator("#adm-audit-tbody");
    await expect(tbody).toContainText("GMT+8", { timeout: 10_000 });
    await expect(tbody).toContainText("原始详情");
    await tbody.locator("details.adm-raw-values summary").first().click();
    await expect(tbody.locator("details.adm-raw-values code").first()).not.toBeEmpty();
    await assertNoHorizontalOverflow(page, "audit-1440");
    await shot(page, "after-1440-audit.png");
  });

	test("19. invites: 创建折叠、列表前置、无来源漏斗、终态非 loading", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="invites"]').click();
		await expect
			.poll(async () => frame.locator("#adm-state-invites").getAttribute("data-page-state"))
			.toMatch(/ready|empty/, { timeout: 10_000 });
		const pageHtml = await frame.locator("#adm-page-invites").innerHTML();
		// 注册模式摘要卡与创建入口在前、邀请列表随后（§4.4）
		expect(pageHtml.indexOf('id="adm-invite-create-box"'))
			.toBeLessThan(pageHtml.indexOf('id="adm-invites-table"'));
		await expect(frame.locator("#adm-invite-create-form")).toBeHidden();
		// wave 2：来源漏斗/用户来源明细不存在
		expect(pageHtml).not.toContain("adm-acq-funnel-table");
		expect(pageHtml).not.toContain("adm-acq-users-table");
		await assertNoHorizontalOverflow(page, "invites-1440");
		await shot(page, "after-1440-invites.png");
	});

  // P0-3 回归：抽屉发起的危险操作确认条必须挂抽屉内的 #adm-drawer-confirm
  // （旧实现挂页级 #adm-users-confirm——被遮罩挡住且不在 Tab 圈定内）
  test("23. drawer 危险操作确认：确认条在对话框内、有焦点、可点击、Esc 可关", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect(frame.locator("#adm-users-tbody"))
      .toContainText("E2E 普通用户", { timeout: 10_000 });
    await frame.locator("#adm-users-tbody tr", { hasText: "E2E 普通用户" })
      .locator("button", { hasText: "详情" }).click();
    const drawer = frame.locator("#adm-user-drawer");
    await expect(drawer).toBeVisible();
    // 点击禁用 → 确认条出现在抽屉内部（非页级确认条）
    await drawer.locator("#adm-drawer-body button", { hasText: "禁用" }).click();
    const confirmBox = frame.locator("#adm-drawer-confirm");
    await expect(confirmBox).toBeVisible();
    await expect(confirmBox).toContainText("确认禁用用户");
    await expect(confirmBox).toContainText("确认执行");
    const okBtn = confirmBox.locator("button", { hasText: "确认执行" });
    // 几何断言（iframe 文档坐标）：确认按钮在抽屉内、出现在视口内、
    // 命中测试打在按钮自身而非遮罩、出现即获得焦点
    const geo = await okBtn.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {
        inDrawer: el.closest("#adm-user-drawer") !== null,
        focused: document.activeElement === el,
        hitInside: hit !== null && (hit === el || el.contains(hit)),
        right: r.right,
        vw: document.documentElement.clientWidth,
      };
    });
    expect(geo.inDrawer, "确认执行在 #adm-user-drawer 内").toBe(true);
    expect(geo.focused, "确认条出现即聚焦确认按钮").toBe(true);
    expect(geo.hitInside, "elementFromPoint 命中确认按钮而非遮罩").toBe(true);
    expect(geo.right).toBeLessThanOrEqual(geo.vw + 1);
    // Tab 圈定仍覆盖确认条：Tab 循环后焦点都留在抽屉内
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press(i % 2 ? "Shift+Tab" : "Tab");
      const inside = await drawer.evaluate((el) =>
        el.ownerDocument !== null && el.contains(el.ownerDocument.activeElement));
      expect(inside, `tab cycle ${i}`).toBe(true);
    }
    // 取消（比真实禁用更安全的回归路径）→ 确认条清空、用户未被禁用
    await confirmBox.locator("button", { hasText: "取消" }).click();
    await expect(confirmBox).toBeHidden();
    await expect(frame.locator("#adm-users-tbody tr", { hasText: "E2E 普通用户" }))
      .toContainText("启用", { timeout: 10_000 });
    // Esc 仍可关闭抽屉
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
  });
});

test.describe("UI 升级 2026-09-01 — 移动 390×844（批次 E）", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("20. 390 overview: 导航闭合 CNY KPI 无溢出；导航打开无首字符重复（P0-1）", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await expect(frame.locator("#adm-ov-kpis")).toBeVisible({ timeout: 10_000 });
    // P1-5：概览截图为导航闭合状态（CNY KPI、身份行、无重复）
    await expect(frame.locator("#adm-actor-line")).toContainText("当前身份：", { timeout: 10_000 });
    await assertNoHorizontalOverflow(page, "overview-390");
    await shot(page, "after-390-overview.png");
    // 导航抽屉打开
    await frame.locator("#adm-nav-toggle").click();
    await expect(frame.locator("#adm-nav.adm-nav--open")).toBeVisible();
    // P0-1：每个导航按钮 innerText 是完整标签（概览/用户/邀请/…，wave 2
    // 改名后无首字符重复），且 ::before 内容已按同特异性复位
    const labels = ["概览", "用户", "邀请", "设置", "费用", "插件", "审计"];
    const navBtns = frame.locator(".adm-nav-btn");
    expect(await navBtns.count()).toBe(labels.length);
    for (let i = 0; i < labels.length; i++) {
      await expect(navBtns.nth(i)).toHaveText(labels[i]);
    }
    const beforeContents = await navBtns.evaluateAll(
      (els) => els.map((el) => getComputedStyle(el, "::before").content));
    for (const c of beforeContents) {
      expect(["none", "normal", ""].includes(c), `::before content: ${c}`).toBe(true);
    }
    await assertNoHorizontalOverflow(page, "nav-open-390");
    await shot(page, "after-390-nav-open.png");
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect(frame.locator("#adm-page-users")).toBeVisible();
  });

  test("21. 390 users: 4 列布局（名/状态/剩余/详情）、剩余在视口内、无水平溢出", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('#adm-nav-toggle').click();
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-users").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    const row = frame.locator("#adm-users-tbody tr", { hasText: "E2E 普通用户" });
    await expect(row).toBeVisible();
    // §4.3：关键信息在行内可见——额度剩余语义（含不可用）与详情操作
    await expect(row).toContainText(/剩余|已用尽|超支|不可用|契约错误/);
    await expect(row.locator("button", { hasText: "详情" })).toBeVisible();
    // 列头（额度剩余）在表中
    await expect(frame.locator("#adm-users-table")).toContainText("额度剩余");
    // 次要列（角色）在 390px 隐藏；角色已无独立桌面列
    const hiddenCols = await frame.locator("#adm-users-table").evaluate((t) => {
      const disp = (sel: string) => {
        const th = t.querySelector(sel);
        return th ? getComputedStyle(th).display === "none" : false;
      };
      return {
        secondary: disp("th.adm-col-secondary"),
        desktop: t.querySelector("th.adm-col-desktop") === null,
      };
    });
    expect(hiddenCols.secondary).toBe(true);
    expect(hiddenCols.desktop).toBe(true);
    // P0-2：几何断言（iframe 文档视口）——toBeVisible 不足以防截断。
    // DOM 列序固定：td[0]=显示名 td[1]=角色(隐藏) td[2]=状态(+AI 堆叠)
    // td[3]=额度剩余 td[4]=操作(详情)；关键单元格 right 边必须落在视口内。
    const geo = await row.evaluate((tr) => {
      const vw = document.documentElement.clientWidth;
      const box = (el: Element | null | undefined) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { left: r.left, right: r.right, width: r.width };
      };
      const tds = Array.from(tr.querySelectorAll("td"));
      const stacks = Array.from(tr.querySelectorAll(".adm-stack-mobile")).map((el) => ({
        text: el.textContent || "",
        display: getComputedStyle(el).display,
        box: box(el),
      }));
      return {
        vw,
        iframeOverflow: document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
        name: box(tds[0]),
        status: box(tds[2]),
        remaining: box(tds[3]),
        detail: box(Array.from(tr.querySelectorAll("button"))
          .find((b) => (b.textContent || "").includes("详情"))),
        stacks,
        visibleCells: tds.filter((td) => td.getBoundingClientRect().width > 0).length,
      };
    });
    expect(geo.iframeOverflow, "users-390 iframe overflow").toBeLessThanOrEqual(1);
    for (const [key, b] of [["name", geo.name], ["status", geo.status],
      ["remaining", geo.remaining], ["detail", geo.detail]] as const) {
      expect(b && b.width, `${key} cell rendered`).toBeGreaterThan(0);
      expect(b && b.right !== undefined && b.right <= geo.vw + 1,
        `${key} cell right edge within iframe viewport`).toBe(true);
    }
    // 4 列布局：可见单元格恰为 4（显示名/状态/额度剩余/操作）
    expect(geo.visibleCells, "390px users table shows exactly 4 columns").toBe(4);
    // 状态格堆叠了 AI 补行（窄屏堆叠补行唯一保留处）
    const aiStack = geo.stacks.find((s) => /AI/.test(s.text));
    expect(aiStack && aiStack.display).toBe("block");
    // 创建表单折叠
    await expect(frame.locator("#adm-users-create-form")).toBeHidden();
    await assertNoHorizontalOverflow(page, "users-390");
    await shot(page, "after-390-users.png");
  });

  test("22. 390 settings + drawer: label 可见、抽屉可开关键盘可用、无水平溢出", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator("#adm-nav-toggle").click();
    await frame.locator('.adm-nav-btn[data-page="settings"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-settings").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    await expect(frame.getByLabel("Demo 每周金额（CNY，可选）")).toBeVisible();
    await expect(frame.locator("#adm-window-summaries")).toBeVisible();
    await assertNoHorizontalOverflow(page, "settings-390");
    await shot(page, "after-390-settings.png");
    // 抽屉：390px 可打开、焦点管理可用
    await frame.locator("#adm-nav-toggle").click();
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect(frame.locator("#adm-users-tbody"))
      .toContainText("E2E 普通用户", { timeout: 10_000 });
    const detailBtn = frame.locator("#adm-users-tbody tr", { hasText: "E2E 普通用户" })
      .locator("button", { hasText: "详情" });
    await detailBtn.click();
    await expect(frame.locator("#adm-user-drawer")).toBeVisible();
    await expect(frame.locator("#adm-drawer-close")).toBeFocused();
    // P1-5：390px 抽屉打开且可用（关闭前截图）
    await shot(page, "after-390-user-drawer.png");
    await page.keyboard.press("Escape");
    await expect(frame.locator("#adm-user-drawer")).toBeHidden();
    await expect(detailBtn).toBeFocused();
    await assertNoHorizontalOverflow(page, "drawer-390");
  });
});
