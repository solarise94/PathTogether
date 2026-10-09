/** 用户反馈端到端：真实登录 → 账户弹层「反馈问题」→ 提交 → 真实后端 202。
 * 校验发出的附带信息遵守隐私约定（无查询串、无输入内容）。 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as { userLogin: string; userPassword: string };

async function login(page: Page, loginId: string, password: string) {
	await page.goto("/login");
	await page.fill('form[action="/login"] input[name="username"]', loginId);
	await page.fill('form[action="/login"] input[name="password"]', password);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.click('form[action="/login"] button[type="submit"]'),
	]);
}

test("账户弹层提交反馈：真实后端受理，附带记录不含查询串与输入内容", async ({ page }) => {
	await page.addInitScript(() => {
		try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
	});
	await page.setViewportSize({ width: 1440, height: 900 });
	await login(page, CREDS.userLogin, CREDS.userPassword);
	await page.goto("/app");
	// 产生一条带查询串的接口调用，供记录器记录
	await page.evaluate(() => fetch("/api/slides?secret_probe=1"));
	await page.evaluate(() => {
		console.warn("PRIVATE-FEEDBACK-PROBE", { password: "PRIVATE-FEEDBACK-PROBE" });
		window.dispatchEvent(new ErrorEvent("error", {
			message: "PRIVATE-FEEDBACK-PROBE",
			filename: location.origin + "/static/app.js?token=PRIVATE-FEEDBACK-PROBE",
		}));
	});

	await page.locator("#acct-btn").click();
	await page.locator("#acct-feedback-btn").click();
	await expect(page.locator("#feedback-mask")).toBeVisible();
	const text = "端到端反馈：点击导出没有反应，请帮忙看看。";
	await page.locator("#feedback-desc").fill(text);

	const reqP = page.waitForRequest((r) =>
		r.method() === "POST" && new URL(r.url()).pathname === "/api/feedback");
	const respP = page.waitForResponse((r) =>
		new URL(r.url()).pathname === "/api/feedback");
	await page.locator("#feedback-submit").click();
	const req = await reqP;
	const resp = await respP;

	expect(resp.status()).toBe(202);
	const out = await resp.json();
	expect(typeof out.feedback_id).toBe("string");
	await expect(page.locator("#feedback-success")).toBeVisible();

	const body = req.postDataJSON() as { description: string; client: { events: Array<Record<string, unknown>> } };
	expect(body.description).toBe(text);
	const serialized = JSON.stringify(body.client);
	expect(serialized).not.toContain("secret_probe");
	expect(serialized).not.toContain(text);
	expect(serialized).not.toContain("PRIVATE-FEEDBACK-PROBE");
	expect(body.client.events.some((e) => e.kind === "api" && String(e.path) === "/api/slides"))
		.toBe(true);
});
