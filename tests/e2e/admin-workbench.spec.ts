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
		// 关键字段：角色（次要列）、启用状态；掩码登录账号已收进抽屉（§5.3）
		await expect(frame.locator("#adm-users-tbody")).toContainText("user");
		await expect(frame.locator("#adm-users-tbody")).toContainText("启用");
		await expect(frame.locator("#adm-users-table")).toContainText("本月剩余");
		// 详情抽屉可打开且技术细节含低频字段（余额未开户语义）
		await frame.locator("#adm-users-tbody button", { hasText: "详情" }).first().click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("未开户");
		await expect(frame.locator("#adm-drawer-body")).toContainText("技术细节");
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
		// §4.2：信息区主视图只回显 51 CNY；原始 nano 在 raw 展开区且可展开
		await expect(frame.locator("#adm-spend-info"))
			.toContainText("51 CNY", { timeout: 10_000 });
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
		// 快照 50 CNY（默认修改不追溯已开窗口，§1.1），剩余 50 CNY
		const summaries = frame.locator("#adm-window-summaries");
		await expect(summaries).toBeVisible();
		await expect(summaries).toContainText("Demo（周窗口）");
		await expect(summaries).toContainText("Owner（月窗口）");
		// 摘要卡 kv 行文本相邻拼接（dt「额度」+ dd「50 CNY」）
		await expect(summaries).toContainText("额度50 CNY");
		await expect(summaries).toContainText("剩余50 CNY");
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
			.toContainText("已消费 0 / 预占 0 不回退");
		await expect(frame.locator("#adm-win-demo-confirm"))
			.toContainText("新剩余 52 CNY");
		// 确认执行 → 成功态；摘要卡立即刷新为 52 CNY（不等下个窗口）
		await frame.locator("#adm-win-demo-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-win-demo-status"))
			.toContainText("已调整", { timeout: 10_000 });
		await expect(summaries)
			.toContainText("额度52 CNY", { timeout: 10_000 });
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
			.toContainText("额度1002 CNY", { timeout: 10_000 });
	});

	test("10d. 用户页：建号覆盖进高级折叠 + 抽屉「保存后立即改当前月」", async ({ page }) => {
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
		// §5.6：月额度覆盖在默认折叠的「高级：单独月额度」里
		await expect(frame.locator("#adm-users-new-limit")).toBeHidden();
		await frame.locator("#adm-users-new-limit-box").locator("summary").click();
		await expect(frame.locator("#adm-users-new-limit")).toBeVisible();
		// 创建带 3.5 CNY 月额度覆盖的用户（建号 + override + audit 同事务）
		await frame.locator("#adm-users-new-login").fill("e2e-limited@pt.test");
		await frame.locator("#adm-users-new-display").fill("E2E 限额用户");
		await frame.locator("#adm-users-new-password").fill("e2e-limited-pass-123456");
		await frame.locator("#adm-users-new-limit").fill("3.5");
		await frame.locator("#adm-users-create-btn").click();
		await expect(frame.locator("#adm-users-create-status"))
			.toContainText("月额度覆盖 3.5 CNY", { timeout: 10_000 });
		// 详情抽屉：金额主视图三行（剩余/额度/来源），短文案、无人话之外的英文
		const row = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("本月剩余");
		await expect(frame.locator("#adm-drawer-body")).toContainText("剩余 3.5 CNY");
		await expect(frame.locator("#adm-drawer-body")).toContainText("本月额度");
		await expect(frame.locator("#adm-drawer-body")).toContainText("3.5 CNY");
		await expect(frame.locator("#adm-drawer-body")).toContainText("单独覆盖");
		// 原始 nano 在抽屉「技术细节」折叠区（§5.4）
		await frame.locator("#adm-drawer-body details.adm-drawer-tech")
			.locator("summary").click();
		await expect(frame.locator("#adm-drawer-body details.adm-drawer-tech"))
			.toContainText("3500000000");
		// 抽屉内唯一金额动作「每月额度（CNY）」：保存 = 覆盖 + 立即改当前月
		await frame.locator("#adm-override-limit-input").fill("2.5");
		await frame.locator("#adm-drawer-body button", { hasText: "保存" }).click();
		await expect(frame.locator("#adm-drawer-confirm")).toBeVisible();
		await expect(frame.locator("#adm-drawer-confirm"))
			.toContainText("2.5 CNY（2500000000 nano）");
		await expect(frame.locator("#adm-drawer-confirm"))
			.toContainText("立即改当前月");
		await frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已设为单独每月额度 2.5 CNY", { timeout: 10_000 });
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("当前月已立即生效", { timeout: 10_000 });
		await frame.locator("#adm-drawer-close").click();
		await expect(frame.locator("#adm-user-drawer")).toBeHidden();
		// 表内剩余列同步刷新为 2.5 CNY（立即改当前窗口的结果）
		const row2 = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await expect(row2).toContainText("剩余 2.5 CNY", { timeout: 10_000 });
	});

	test("10e. 金额用尽后的可见状态：抽屉把每月额度存为 0 → 剩余显示已用尽", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("E2E 限额用户", { timeout: 10_000 });
		// 抽屉编辑器把每月额度存为 0（两步：覆盖 + 立即改当前窗口）
		const row = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await frame.locator("#adm-override-limit-input").fill("0");
		await frame.locator("#adm-drawer-body button", { hasText: "保存" }).click();
		await expect(frame.locator("#adm-drawer-confirm")).toBeVisible();
		await frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已设为单独每月额度 0 CNY", { timeout: 10_000 });
		await frame.locator("#adm-drawer-close").click();
		// 用户表「本月剩余」列显示短文案「已用尽」——不再有长拒绝说明
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
		// §4.4：创建邀请默认折叠——展开入口后填写；§5.6：月额度模板在高级折叠里
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

  test("12. overview: CNY-only KPI、身份行、legacy 折叠在后、无 nano 长串", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="overview"]').click();
    await expect(frame.locator("#adm-ov-kpis")).toBeVisible({ timeout: 10_000 });
    // §4.9：当前身份收成一行次要信息
    await expect(frame.locator("#adm-actor-line")).toContainText("当前身份：owner");
    // §4.2：KPI 主视图只有 CNY（金额 KPI 不含 nano 长串）
    await expect(frame.locator("#adm-ov-kpis")).toContainText("CNY");
    const kpiText = await frame.locator("#adm-ov-kpis").textContent();
    expect(kpiText).not.toContain("nano");
    // §4.6：turn legacy 卡在金额/调用卡之后、默认折叠、中性样式
    // details 未展开时其内容不可见（summary 可见）
    await expect(frame.locator("#adm-ov-turn")).toBeHidden();
    const html = await frame.locator("#adm-page-overview").innerHTML();
    expect(html.indexOf('id="adm-ov-billing"')).toBeLessThan(html.indexOf('id="adm-ov-turn-box"'));
    expect(html.indexOf('id="adm-ov-usage"')).toBeLessThan(html.indexOf('id="adm-ov-turn-box"'));
    expect(html).toContain("adm-legacy-card");
    await assertNoHorizontalOverflow(page, "overview-1440");
    await shot(page, "after-1440-overview.png");
  });

  test("13. users: 5 列表头 + 本月剩余单列 + 创建折叠；限额户展示已用尽", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    // 终态：ready（不残留 loading）
    await expect
      .poll(async () => frame.locator("#adm-state-users").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    const table = frame.locator("#adm-users-table");
    await expect(table).toContainText("本月剩余");
    await expect(table).not.toContainText("本月用量");
    await expect(frame.locator("#adm-users-tbody"))
      .toContainText("E2E 普通用户", { timeout: 10_000 });
    // 剩余列覆盖边界语义之一（10e 已把限额户额度存 0 → 已用尽）
    const limited = frame.locator("#adm-users-tbody tr", { hasText: "E2E 限额用户" });
    await expect(limited).toContainText("已用尽");
    // §5.3：每行只回答「本月剩余」一个数字语义；5 个单元格、无用量条、
    // 无已消费/预占双行
    const rows = await frame.locator("#adm-users-tbody").evaluate((tbody) =>
      Array.from(tbody.querySelectorAll("tr")).map((tr) => ({
        text: tr.textContent || "",
        cells: tr.querySelectorAll("td").length,
        meters: tr.querySelectorAll(".adm-usage-meter").length,
      })));
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.text, "user row must answer 本月剩余 semantics")
        .toMatch(/剩余|已用尽|超支|不可用/);
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
    // 抽屉内按钮语义：普通操作在前，危险操作独立区（§4.5）；§5.4 后
    // 不再有「打开账本」，金额主视图三行 + 折叠技术细节
    const drawerHtml = await frame.locator("#adm-drawer-body").innerHTML();
    expect(drawerHtml).not.toContain("打开账本");
    expect(drawerHtml.indexOf("身份预览")).toBeLessThan(drawerHtml.indexOf("危险操作"));
    expect(drawerHtml.indexOf("本月剩余")).toBeLessThan(drawerHtml.indexOf("技术细节"));
    expect(drawerHtml).toContain("本月额度");
    expect(drawerHtml).toContain("额度来源");
    // 技术细节默认折叠：details 未展开时其内容不可见
    await expect(frame.locator("#adm-drawer-body details.adm-drawer-tech")
      .locator("dl").first()).toBeHidden();
    // 抽屉打开状态截图（P1-5：统一 form/label、窗口边界换行、raw 详情、危险区）
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
    // §4.1：label 可定位（getByLabel 经 label[for] 命中控件）
    await expect(frame.getByLabel("Demo 每周金额（CNY，可选）")).toBeVisible();
    await expect(frame.getByLabel("注册用户默认每月金额（CNY，可选）")).toBeVisible();
    await expect(frame.getByLabel("Owner 策略额度（CNY / 月，可选）")).toBeVisible();
    await expect(frame.getByLabel("平台单任务最大步骤（可选）")).toBeVisible();
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

  test("16. billing: 第一屏供应商+人工调整、先查后启用、运维明细折叠页尾", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="billing"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-billing").getAttribute("data-page-state"))
      .toMatch(/ready|error/, { timeout: 10_000 });
    const pageHtml = await frame.locator("#adm-page-billing").innerHTML();
    // §5.7：第一屏 = 供应商余额 → 人工调整；caps/用量/账本等运维明细折叠在后
    expect(pageHtml.indexOf('id="adm-billing-provider-card"'))
      .toBeLessThan(pageHtml.indexOf('id="adm-adjust-card"'));
    expect(pageHtml.indexOf('id="adm-adjust-card"'))
      .toBeLessThan(pageHtml.indexOf('id="adm-billing-acct-box"'));
    expect(pageHtml.indexOf('id="adm-billing-acct-box"'))
      .toBeLessThan(pageHtml.indexOf('id="adm-turn-legacy-card"'));
    // caps 卡标题「已不参与授权，仅兼容」；details 未展开时内容不可见
    await expect(frame.locator("#adm-billing-acct-box summary"))
      .toContainText("已不参与授权，仅兼容");
    expect(await frame.locator("#adm-billing-acct-box").getAttribute("open")).toBeNull();
    await expect(frame.locator("#adm-acct-info")).toBeHidden();
    // turn legacy 卡：中性、默认折叠（details 未展开时其内容不可见）
    await expect(frame.locator("#adm-billing-turn")).toBeHidden();
    expect(pageHtml).toContain("adm-legacy-card");
    // 调整类型收敛：grant / manual_adjustment 两个（topup/refund 不再陈列）
    expect(await frame.locator("#adm-adjust-kind option").count()).toBe(2);
    await expect(frame.locator("#adm-adjust-kind")).toContainText("grant");
    await expect(frame.locator("#adm-adjust-kind")).toContainText("manual_adjustment");
    // §4.5：人工调整区先查后启用
    const adjustBtn = frame.locator("#adm-adjust-btn");
    await expect(adjustBtn).toBeDisabled();
    await frame.locator("#adm-acct-user").fill("no-such-user");
    // 预期失败的账户查询（404）：fetch console 报错按精确 pathname 豁免
    ignoreApi.push("/api/admin/v1/billing/accounts/no-such-user");
    await frame.locator("#adm-acct-load-btn").click();
    // 查询不存在的用户 → 服务端错误 → 调整区保持禁用（错误显式呈现不伪装）
    await expect(frame.locator("#adm-acct-info"))
      .toContainText("user_not_found", { timeout: 10_000 });
    await expect(adjustBtn).toBeDisabled();
    // 真实 user_id 从用户抽屉读取（E2E 普通用户），查询成功（account:null 也允许）
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect(frame.locator("#adm-users-tbody"))
      .toContainText("E2E 普通用户", { timeout: 10_000 });
    await frame.locator("#adm-users-tbody tr", { hasText: "E2E 普通用户" })
      .locator("button", { hasText: "详情" }).click();
    const drawerText = await frame.locator("#adm-drawer-body").textContent();
    // token_urlsafe 生成的 user_id 含 -/_，字符类必须覆盖
    const userIdMatch = drawerText?.match(/usr_[A-Za-z0-9_-]+/);
    expect(userIdMatch).toBeTruthy();
    await frame.locator("#adm-drawer-close").click();
    await frame.locator('.adm-nav-btn[data-page="billing"]').click();
    await frame.locator("#adm-acct-user").fill(userIdMatch![0]);
    await frame.locator("#adm-acct-load-btn").click();
    await expect(frame.locator("#adm-acct-info"))
      .toContainText(userIdMatch![0], { timeout: 10_000 });
    await expect(adjustBtn).toBeEnabled({ timeout: 10_000 });
    // user_id 变更 → 旧查询失效，再次禁用
    await frame.locator("#adm-acct-user").fill("changed-but-not-queried");
    await expect(adjustBtn).toBeDisabled();
    // 轮换按钮语义在插件页断言；本页确认供应商刷新为次要按钮
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

  test("19. invites: 创建折叠、列表/漏斗前置、终态非 loading", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="invites"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-invites").getAttribute("data-page-state"))
      .toMatch(/ready|empty/, { timeout: 10_000 });
    const pageHtml = await frame.locator("#adm-page-invites").innerHTML();
    // 邀请列表与来源漏斗都在创建入口之后、且创建入口默认折叠（§4.4）
    expect(pageHtml.indexOf('id="adm-invite-create-box"'))
      .toBeLessThan(pageHtml.indexOf('id="adm-invites-table"'));
    expect(pageHtml.indexOf('id="adm-invites-table"'))
      .toBeLessThan(pageHtml.indexOf('id="adm-acq-funnel-table"'));
    await expect(frame.locator("#adm-invite-create-form")).toBeHidden();
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
    // P0-1：每个导航按钮 innerText 是完整标签（概览/用户/…，无首字符重复），
    // 且 ::before 内容已按同特异性复位（平板图标字符规则不能泄漏到 390px）
    const labels = ["概览", "用户", "邀请与来源", "设置", "额度与账单", "插件", "审计"];
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
    // §5.3：关键信息在行内可见——本月剩余语义（含不可用）与详情操作
    await expect(row).toContainText(/剩余|已用尽|超支|不可用/);
    await expect(row.locator("button", { hasText: "详情" })).toBeVisible();
    // 列头（本月剩余）在表中
    await expect(frame.locator("#adm-users-table")).toContainText("本月剩余");
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
    // td[3]=本月剩余 td[4]=操作(详情)；关键单元格 right 边必须落在视口内。
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
    // 4 列布局：可见单元格恰为 4（显示名/状态/本月剩余/操作）
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
