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

/** 收集未允许的浏览器错误（§10.2-10：console error / pageerror / CSP violation / 失败请求）。 */
function attachErrorCollectors(page: Page) {
	const problems: string[] = [];
	// favicon 是浏览器内置请求、不属于工作台资源面（403/404 属平台预期）
	const isFavicon = (url: string) => new URL(url).pathname === "/favicon.ico";
	page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
	page.on("console", (msg) => {
		if (msg.type() === "error") {
			const text = msg.text();
			if (/favicon/i.test(text)) return;
			// 主文档导航自身的非 2xx（如 test 2 预期的 403）由用例断言响应状态，
			// 不计入「未允许错误」；子资源的 403/404 仍算
			const loc = msg.location()?.url || "";
			if (/Failed to load resource/.test(text) && loc) {
				try {
					if (new URL(loc).pathname === new URL(page.url()).pathname) return;
				} catch { /* URL 解析失败按未允许处理 */ }
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
		// 390px 手机：导航抽屉可展开、概览可达
		await page.setViewportSize({ width: 390, height: 844 });
		await frame.locator("#adm-nav-toggle").click();
		await expect(frame.locator("#adm-nav.adm-nav--open")).toBeVisible();
		await testInfo.attach("admin-mobile-390", {
			body: await page.screenshot({ fullPage: false }),
			contentType: "image/png",
		});
	});
});
