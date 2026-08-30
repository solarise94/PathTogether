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
import { join } from "node:path";

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
		// 关键字段：掩码登录账号、角色、启用状态
		await expect(frame.locator("#adm-users-tbody")).toContainText("e***@pt.test");
		await expect(frame.locator("#adm-users-tbody")).toContainText("user");
		await expect(frame.locator("#adm-users-tbody")).toContainText("启用");
		// 详情抽屉可打开且包含低频字段（余额未开户语义）
		await frame.locator("#adm-users-tbody button", { hasText: "详情" }).first().click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("未开户");
		await frame.locator("#adm-drawer-close").click();
		await expect(frame.locator("#adm-user-drawer")).toBeHidden();
	});

	test("7. acquisition zero data reaches terminal empty state, long copy exactly once", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="invites"]').click();
		// 终态断言（复核 P2 2026-08-29：此前只断言空文案可见，页面永久
		// 停留 loading 也会通过）：限定时间内离开 loading 进入 empty
		await expect
			.poll(async () =>
				frame.locator("#adm-state-invites").getAttribute("data-page-state"))
			.toBe("empty", { timeout: 10_000 });
		// 加载文案必须消失
		await expect(frame.locator("#adm-state-invites")).not.toContainText("加载中");
		// 完整空态说明（历史用户尚未回填）只在页级状态条出现一次，
		// 子区块是简短提示（不重复长文案）
		await expect(frame.locator("#adm-state-invites")).toContainText("暂无来源归因数据");
		await expect(frame.locator("#adm-state-invites")).toContainText("历史用户尚未回填");
		expect(
			await frame.locator("text=历史用户尚未回填").count(),
		).toBe(1);
		await expect(frame.locator("#adm-acq-empty")).toContainText("本周期暂无来源访问记录");
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

	test("10b. 设置页：金额策略保存（CNY 输入 → nano wire）与 enforcement 展示", async ({ page }) => {
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
		// 种子 50 CNY 回显；enforcement 恒 shadow（批次 C2 验收前）
		await expect(frame.locator("#adm-spend-demo-week")).toHaveValue("50");
		await expect(frame.locator("#adm-spend-mode")).toHaveValue("shadow");
		// 保存 51 CNY（wire 十进制字符串 51000000000 nano）
		await frame.locator("#adm-spend-demo-week").fill("51");
		await frame.locator("#adm-spend-save-btn").click();
		await expect(frame.locator("#adm-spend-status"))
			.toContainText("已保存 1 项", { timeout: 10_000 });
		// 信息区回显 51 CNY + nano 值
		await expect(frame.locator("#adm-spend-info"))
			.toContainText("51 CNY（51000000000 nano）", { timeout: 10_000 });
	});

	test("10c. 设置页：调整当前窗口（独立按钮 + 二次确认 + 影响展示）", async ({ page }) => {
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
		// Demo 主体（第一项）：当前窗口额度仍是策略更新前的快照 50 CNY
		// （默认修改不追溯已开窗口，§1.1），剩余 50 CNY
		await expect(frame.locator("#adm-window-info"))
			.toContainText("Demo（全站共享周窗口）");
		await expect(frame.locator("#adm-window-info")).toContainText("50 CNY");
		// 新额度 52 → 独立按钮触发页内二次确认（影响：已消费/预占/新剩余）
		await frame.locator("#adm-window-newlimit").fill("52");
		await frame.locator("#adm-window-adjust-btn").click();
		await expect(frame.locator("#adm-window-confirm")).toBeVisible();
		await expect(frame.locator("#adm-window-confirm"))
			.toContainText("已消费 0 / 预占 0 不回退");
		await expect(frame.locator("#adm-window-confirm"))
			.toContainText("新剩余 52 CNY");
		// 确认执行 → 成功态 + 信息区 52 CNY（立即生效，不等下个窗口）
		await frame.locator("#adm-window-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-window-status"))
			.toContainText("已调整", { timeout: 10_000 });
		await expect(frame.locator("#adm-window-info"))
			.toContainText("52 CNY（52000000000 nano）", { timeout: 10_000 });
	});

	test("10d. 用户页：新增用户带月额度覆盖 + 详情窗口 + 覆盖设置/清除", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 普通用户", { timeout: 10_000 });
		// 创建带 3.5 CNY 月额度覆盖的用户（建号 + override + audit 同事务）
		await frame.locator("#adm-users-new-login").fill("e2e-limited@pt.test");
		await frame.locator("#adm-users-new-display").fill("E2E 限额用户");
		await frame.locator("#adm-users-new-password").fill("e2e-limited-pass-123456");
		await frame.locator("#adm-users-new-limit").fill("3.5");
		await frame.locator("#adm-users-create-btn").click();
		await expect(frame.locator("#adm-users-create-status"))
			.toContainText("月额度覆盖 3.5 CNY", { timeout: 10_000 });
		// 详情抽屉：当前月 limit/spent/reserved/remaining + 覆盖状态
		const row = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("本月额度（limit snapshot）");
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("3.5 CNY（3500000000 nano）");
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("用户覆盖（user_override");
		// 旧 lifetime caps 标注为兼容字段（不是当前月额度）
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("兼容字段，非当前月额度");
		// 抽屉内覆盖编辑：改为 2.5 CNY（下个窗口起生效）
		await frame.locator("#adm-drawer-body input[placeholder*='新覆盖额度']")
			.fill("2.5");
		await frame.locator("#adm-drawer-body button", { hasText: "设置/更新覆盖" }).click();
		await expect(frame.locator("#adm-users-status"))
			.toContainText("已设置月额度覆盖 2.5 CNY", { timeout: 10_000 });
		// 覆盖清除（DELETE；下个窗口回退全局默认）
		const row2 = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await row2.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("用户覆盖（user_override", { timeout: 10_000 });
		await frame.locator("#adm-drawer-body button", { hasText: "清除覆盖" }).click();
		await expect(frame.locator("#adm-users-status"))
			.toContainText("已清除月额度覆盖", { timeout: 10_000 });
	});

	test("10e. 金额用尽后的可见状态：当前窗口调到 0 → 剩余显示已用尽", async ({ page }) => {
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
		// 选「E2E 限额用户」月窗口，把当前窗口额度调到 0（二次确认含影响）
		await frame.locator("#adm-window-subject")
			.selectOption({ label: "E2E 限额用户（月窗口）" });
		await expect(frame.locator("#adm-window-info"))
			.toContainText("E2E 限额用户");
		await frame.locator("#adm-window-newlimit").fill("0");
		await frame.locator("#adm-window-adjust-btn").click();
		await expect(frame.locator("#adm-window-confirm")).toBeVisible();
		await expect(frame.locator("#adm-window-confirm"))
			.toContainText("新剩余 0 CNY（已用尽");
		await frame.locator("#adm-window-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-window-status"))
			.toContainText("已调整", { timeout: 10_000 });
		// 用户详情抽屉展示「已用尽」可见状态
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 限额用户", { timeout: 10_000 });
		const row = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("0 CNY（已用尽：下次预占将被拒绝）", { timeout: 10_000 });
	});

	test("10f. 邀请页：月额度模板创建 + 明文码仅一次 + 列表展示", async ({ page }) => {
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
		await frame.locator("#adm-invite-login").fill("e2e-inv@pt.test");
		await frame.locator("#adm-invite-limit").fill("2.5");
		await frame.locator("#adm-invite-create-btn").click();
		// 明文邀请码仅此一次展示（token box 可见且非空）
		await expect(frame.locator("#adm-invite-create-status"))
			.toContainText("明文邀请码只显示这一次", { timeout: 10_000 });
		await expect(frame.locator("#adm-invite-token-box")).toBeVisible();
		await expect(frame.locator("#adm-invite-token")).not.toBeEmpty();
		// 列表：月额度模板列 2.5 CNY（十进制字符串 → CNY 精确换算）
		await expect(frame.locator("#adm-invites-tbody"))
			.toContainText("2.5 CNY", { timeout: 10_000 });
		// 注册模式状态展示保持
		await expect(frame.locator("#adm-acq-mode")).toContainText("closed");
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
