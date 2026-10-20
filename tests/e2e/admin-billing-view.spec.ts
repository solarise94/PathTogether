/**
 * 费用页聚合视图 E2E（2026-10-20 review-for-opus 一.1/一.2/一.3/二）。
 *
 * 真实 Flask + PostgreSQL + 仓库内 admin bundle + 真实 Chromium：把复核脚本
 * check-admin-ui.mjs 的场景**按正确行为**重写为断言——
 *   1) 直接深链 /admin#billing（未访问用户页）显示邮箱：身份由服务端批投影，
 *      不再依赖用户页缓存；Demo 显示 Demo；未知身份显式降级、原始 ID 只在
 *      技术详情；
 *   2) 聚合请求失败后筛选 select 的 option 保持（重复 DOM id 的回归）；
 *   3) DOM id 唯一：adm-usage-status-filter / adm-usage-status-message；
 *   4) 「按任务」视图 + 任务面板（step A 预算：已用/待结算预占/账户剩余及
 *      其类型/未设置单次上限）+ 原始调用展开。
 *
 * 身份/聚合响应经 page.route 打桩（只替换 HTTP 响应；宿主桥、opaque iframe、
 * CSP、插件 HTML/JS 全部真实）——服务端投影本身由 pytest 覆盖
 *（tests/test_admin_billing_usage_summary.py）。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
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

const HOST_READY = "#admin-host-state-ready-marker";
function hostStatus(page: Page) {
	return page.locator("#admin-host-status");
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

async function gotoAdminReady(page: Page, hash = "") {
	await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
	await page.goto("/admin" + hash);
	await expect(hostStatus(page)).toHaveAttribute(
		"data-admin-host-state", "ready", { timeout: 5000 });
	void HOST_READY;
}

const HOUR_ITEM = {
	group_key: "hour:1792400000|user|usr_e2e",
	hour_start: 1792400000, hour_end: 1792403600,
	subject_type: "user", subject_id: "usr_e2e",
	identity: "review@example.invalid", identity_source: "email",
	identity_user_id: "usr_e2e",
	task_count: 2, call_count: 41, unpriced_call_count: 1,
	provider_cost_nano_cny: "1950000000", charge_nano_cny: "391205560",
	charge_cny: "0.39", provider_cost_cny: "1.95",
	tasks: [
		{ request_id: "req_e2e_41", subject_type: "user", subject_id: "usr_e2e",
		  task_key: "req_e2e_41", unattributed: false, user_id: "usr_e2e",
		  identity: "review@example.invalid", identity_source: "email",
		  started_at: 1792400100, last_at: 1792400900,
		  call_count: 41, unpriced_call_count: 1,
		  provider_cost_nano_cny: "1950000000",
		  charge_nano_cny: "391205560", session_id: "sess_e2e",
		  status: "unknown", status_source: null },
		{ request_id: null, subject_type: "user", subject_id: "usr_e2e",
		  task_key: "~sess_hist|user|usr_e2e", unattributed: true,
		  task_label: "历史未归属调用", user_id: null,
		  identity: null, identity_source: "unknown",
		  started_at: 1792400200, last_at: 1792400200,
		  call_count: 1, unpriced_call_count: 0,
		  provider_cost_nano_cny: "0", charge_nano_cny: "100000000",
		  session_id: "sess_hist", status: "unknown", status_source: null },
	],
	tasks_truncated: false,
};

const TASK_ITEMS = [
	{ request_id: "req_e2e_new", subject_type: "user", subject_id: "usr_e2e",
	  task_key: "req_e2e_new", unattributed: false, lifecycle_only: true,
	  user_id: "usr_e2e", identity: "review@example.invalid",
	  identity_source: "email", started_at: 1792400000, last_at: 1792400000,
	  call_count: 0, unpriced_call_count: 0,
	  provider_cost_nano_cny: "0", charge_nano_cny: "0",
	  session_id: null, status: "accepted", status_source: "ai_run_bindings",
	  status_detail: "pending_session" },
	{ request_id: null, subject_type: "user", subject_id: "usr_e2e",
	  task_key: "~sess_hist|user|usr_e2e", unattributed: true,
	  user_id: null, identity: null, identity_source: "unknown",
	  started_at: 1792400100, last_at: 1792400100,
	  call_count: 3, unpriced_call_count: 0,
	  provider_cost_nano_cny: "0", charge_nano_cny: "1",
	  session_id: "sess_hist", status: "unknown", status_source: null },
	{ request_id: "req_e2e_demo", subject_type: "demo", subject_id: "cap_e2e",
	  task_key: "req_e2e_demo", unattributed: false, user_id: null,
	  identity: "Demo", identity_source: "demo",
	  started_at: 1792400200, last_at: 1792400200,
	  call_count: 5, unpriced_call_count: 0,
	  provider_cost_nano_cny: "0", charge_nano_cny: "2",
	  session_id: "sess_demo", status: "finished",
	  status_source: "demo_runs" },
];

const TOTALS = {
	task_count: 2, call_count: 41, priced_call_count: 40,
	unpriced_call_count: 1, void_excluded_count: 0,
	provider_cost_nano_cny: "1950000000", charge_nano_cny: "391205560",
	unpriced_amount: "unknown", amount_scope: "priced_only",
	ledger_included: false,
};

function summaryBody(view: "hour" | "task") {
	return {
		view, sort: view === "hour" ? "hour_desc" : "activity_desc",
		items: view === "hour" ? [HOUR_ITEM] : TASK_ITEMS,
		totals: TOTALS, next_cursor: null, limit: 50,
		range: { since: 1791800000, until: 1792403600,
			timezone: "Asia/Shanghai", default_applied: false },
		identity_search_truncated: false, identity_search: null,
	};
}

async function routeSummary(page: Page, view: "hour" | "task" | "error") {
	await page.route("**/api/admin/v1/billing/usage-summary*", async (route: Route) => {
		if (view === "error") {
			await route.fulfill({
				status: 500, contentType: "application/json",
				body: JSON.stringify({ error: { code: "internal",
					message: "simulated billing failure" } }),
			});
			return;
		}
		const url = new URL(route.request().url());
		const requested = url.searchParams.get("view") === "task" ? "task" : "hour";
		await route.fulfill({
			status: 200, contentType: "application/json",
			body: JSON.stringify(summaryBody(view === "hour" ? "hour" : requested)),
		});
	});
}

test.describe.configure({ mode: "serial" });

test.describe("费用页聚合视图（2026-10-20 review）", () => {
	test.use({ viewport: { width: 1440, height: 900 } });

	test("1. 深链 #billing 直接显示邮箱（未访问用户页）；Demo/未知身份显式降级", async ({ page }) => {
		const userApiCalls: string[] = [];
		page.on("request", (req) => {
			const url = new URL(req.url());
			if (url.pathname.startsWith("/api/admin/v1/users")) {
				userApiCalls.push(url.pathname);
			}
		});
		await routeSummary(page, "hour");
		await gotoAdminReady(page, "#billing");
		const frame = page.frameLocator("#admin-plugin-frame");
		// 深链直接落在费用页（不是概览）
		await expect(frame.locator("#adm-page-billing")).toBeVisible();
		await expect
			.poll(async () => frame.locator("#adm-state-billing")
				.getAttribute("data-page-state"))
			.toMatch(/ready|error/, { timeout: 10_000 });
		// 服务端投影的邮箱直出主列；原始 user_id 不出现
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("review@example.invalid", { timeout: 10_000 });
		await expect(frame.locator("#adm-usage-tbody")).not.toContainText("usr_e2e");
		// 首屏没有请求过用户列表（身份不依赖用户页缓存）
		expect(userApiCalls).toEqual([]);
		// 展开小时行：任务行可见；历史未归属行显式标注；未知状态不臆测
		await frame.locator("#adm-usage-tbody button", { hasText: "展开任务" })
			.first().click();
		await expect(frame.locator("#adm-usage-tbody")).toContainText("req_e2e_41");
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("历史未归属调用");
		await expect(frame.locator("#adm-usage-tbody")).toContainText("状态未知");
		// 任务行不展示「进行中/已结束」（无权威生命周期来源时）
		await expect(frame.locator("#adm-usage-tbody")).not.toContainText("进行中");
		// 未计价金额未知（不是 0 元）
		await expect(frame.locator("#adm-usage-tbody")).toContainText("（金额未知）");
		// 展开后的嵌套表不造成横向溢出（1440 桌面）
		const overflow = await frame.locator("html").evaluate((el) =>
			(el as HTMLElement).scrollWidth - (el as HTMLElement).clientWidth);
		expect(overflow, "plugin iframe horizontal overflow").toBeLessThanOrEqual(1);
	}, 60000);

	test("2. DOM id 唯一：筛选 select 与消息行分离，且 select 选项齐全", async ({ page }) => {
		await routeSummary(page, "hour");
		await gotoAdminReady(page, "#billing");
		const frame = page.frameLocator("#admin-plugin-frame");
		await expect(frame.locator("#adm-usage-status-filter")).toBeVisible();
		await expect(frame.locator("#adm-usage-status-message")).toBeVisible();
		// 旧共用 id 不再存在（重复 DOM id 已修复）
		expect(await frame.locator("#adm-usage-status").count()).toBe(0);
		// 筛选 select 是真正的 select，带 3 个 option
		const tag = await frame.locator("#adm-usage-status-filter")
			.evaluate((el) => el.tagName);
		expect(tag).toBe("SELECT");
		await expect(frame.locator("#adm-usage-status-filter option")).toHaveCount(3);
		const messageTag = await frame.locator("#adm-usage-status-message")
			.evaluate((el) => el.tagName);
		expect(messageTag).toBe("SPAN");
	}, 60000);

	test("3. 聚合请求失败：错误只进消息行，筛选 select 的 option 保持", async ({ page }) => {
		const requests: string[] = [];
		page.on("request", (req) => {
			if (req.url().includes("/api/admin/")) requests.push(req.url());
		});
		await routeSummary(page, "error");
		await gotoAdminReady(page, "#billing");
		const frame = page.frameLocator("#admin-plugin-frame");
		// 失败文案落在消息行
		await expect(frame.locator("#adm-usage-status-message"))
			.toContainText("internal", { timeout: 10_000 });
		// 复核复现的旧 bug：失败清空状态筛选 select 的 option（实测 3 → 0）
		await expect(frame.locator("#adm-usage-status-filter option")).toHaveCount(3);
		// 筛选控件仍可用：状态 select 变更立即生效（带 status 参数再发一次请求）
		await frame.locator("#adm-usage-status-filter").selectOption("unpriced");
		await expect.poll(() => requests.filter((u) => u.includes("status=unpriced")).length)
			.toBeGreaterThan(0);
		// 邮箱搜索同样生效（错误后筛选不被卡死）
		await frame.locator("#adm-usage-email").fill("debug@x.com");
		await frame.locator("#adm-usage-search-btn").click();
		await expect.poll(() => requests.filter((u) => u.includes("email=debug%40x.com")).length)
			.toBeGreaterThan(0);
		await expect(frame.locator("#adm-usage-status-filter option")).toHaveCount(3);
		await expect(frame.locator("#adm-usage-status-filter")).toHaveValue("unpriced");
	}, 60000);

	test("4. 切「按任务」视图：进行中任务（无用量）+ 任务面板 step A 预算 + 原始调用", async ({ page }) => {
		await routeSummary(page, "task");
		// 任务面板的两条请求也打桩（只读投影的真实形状）
		await page.route("**/api/admin/v1/billing/task-detail*", async (route) => {
			await route.fulfill({
				status: 200, contentType: "application/json",
				body: JSON.stringify({
					request_id: "req_e2e_41", subject_type: "user",
					subject_id: "usr_e2e", user_id: "usr_e2e",
					identity: "review@example.invalid", identity_source: "email",
					identity_user_id: "usr_e2e", session_id: "sess_e2e",
					in_range: { call_count: 41, unpriced_call_count: 1,
						provider_cost_nano_cny: "1950000000",
						charge_nano_cny: "391205560",
						started_at: 1792400100, last_at: 1792400900 },
					lifetime: { call_count: 42, unpriced_call_count: 1,
						provider_cost_nano_cny: "2000000000",
						charge_nano_cny: "400000000",
						started_at: 1792000000, last_at: 1792400900 },
					outside_range: { call_count: 1, unpriced_call_count: 0,
						provider_cost_nano_cny: "50000000",
						charge_nano_cny: "8794440" },
					status: "unknown", status_source: null, status_detail: null,
					budget: { spent_nano_cny: "400000000",
						unpriced_call_count: 1, unpriced_amount: "unknown",
						reserved_nano_cny: "7000000", open_holds_count: 1,
						reserved_scope: "session",
						reserved_scope_note: "预占按会话统计",
						per_task_cap_nano_cny: null,
						per_task_cap_configured: false,
						note: "未设置单次上限，按账户剩余额度控制",
						ledger_included: false },
					account: { subject_type: "user", subject_id: "usr_e2e",
						limit_type: "user_total_allowance",
						limit_type_label: "注册用户一次性总额度",
						remaining_nano_cny: "46000000000",
						limit_nano_cny: "50000000000",
						spent_nano_cny: "400000000", reserved_nano_cny: "7000000",
						error: null },
				}),
			});
		});
		await page.route("**/api/admin/v1/billing/usage-events*", async (route) => {
			await route.fulfill({
				status: 200, contentType: "application/json",
				body: JSON.stringify({ items: [{ event_id: "use_e2e",
					request_id: "req_e2e_41", session_id: "sess_e2e",
					occurred_at: 1792400100, model: "deepseek-flash",
					status: "priced", charge_nano_cny: "9500000",
					provider_cost_nano_cny: "4750000",
					cache_hit_input_tokens: 1, cache_miss_input_tokens: 2,
					output_tokens: 3, identity: "review@example.invalid",
					identity_source: "email", identity_user_id: "usr_e2e" }],
					next_cursor: null }),
			});
		});
		await gotoAdminReady(page, "#billing");
		const frame = page.frameLocator("#admin-plugin-frame");
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("review@example.invalid", { timeout: 10_000 });
		await frame.locator("#adm-usage-view").selectOption("task");
		// 任务视图：进行中（权威来源）与「尚无用量」显式标注；Demo=Demo
		await expect(frame.locator("#adm-usage-tbody")).toContainText("req_e2e_new");
		await expect(frame.locator("#adm-usage-tbody")).toContainText("（尚无用量）");
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("已起跑（等待会话绑定）");
		await expect(frame.locator("#adm-usage-tbody")).toContainText("历史未归属调用");
		await expect(frame.locator("#adm-usage-tbody")).toContainText("已结束");
		const demoRow = frame.locator("#adm-usage-tbody tr", { hasText: "req_e2e_demo" });
		await expect(demoRow).toContainText("Demo");
		// 未知身份行主列不泄露原始 ID（usr_e2e 只在其它行/详情里）
		const unknownRow = frame.locator("#adm-usage-tbody tr",
			{ hasText: "历史未归属调用" });
		await expect(unknownRow).toContainText("未知身份");
		// 任务面板：step A 预算投影（已用 / 待结算预占 / 账户剩余及其类型 /
		// 未设置单次上限）+ 原始调用（旧明细接口保留兼容）
		await frame.locator("#adm-usage-tbody button",
			{ hasText: "预算与原始调用" }).first().click();
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("本任务已用（全量）", { timeout: 10_000 });
		await expect(frame.locator("#adm-usage-tbody")).toContainText("待结算预占");
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("未设置单次上限，按账户剩余额度控制");
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("注册用户一次性总额度");
		await expect(frame.locator("#adm-usage-tbody")).toContainText("46.00 CNY");
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("范围外金额（同一任务）");
		await expect(frame.locator("#adm-usage-tbody"))
			.toContainText("原始模型调用");
		await expect(frame.locator("#adm-usage-tbody")).toContainText("deepseek-flash");
	}, 60000);
});
