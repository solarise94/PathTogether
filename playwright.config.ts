/**
 * 管理工作台 Chromium E2E 配置（一次性修复包 F，§10.1）。
 *
 * webServer 拉起 tests/e2e/e2e_server.py（临时目录 + 内嵌 PostgreSQL +
 * 一次性 owner/user 凭据，凭据只经 E2E_CREDS_FILE 文件传递，不进日志）。
 * 本地 HTTP origin 即可；公网 HTTPS 的 scheme 分离由 Python CSP 回归覆盖。
 */
import { defineConfig } from "@playwright/test";

const PORT = Number(process.env.E2E_PORT || 8907);
const BASE = `http://127.0.0.1:${PORT}`;

export default defineConfig({
	testDir: "tests/e2e",
	timeout: 60_000,
	expect: { timeout: 10_000 },
	fullyParallel: false,
	workers: 1,
	reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
	use: {
		baseURL: process.env.E2E_BASE_URL || BASE,
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
	},
	webServer: {
		command: `python3 tests/e2e/e2e_server.py --port ${PORT}`,
		url: `${BASE}/healthz`,
		reuseExistingServer: !process.env.CI,
		timeout: 180_000,
		env: {
			...process.env,
			E2E_CREDS_FILE:
				process.env.E2E_CREDS_FILE ||
				`${require("node:os").tmpdir()}/pt-e2e-creds-${PORT}.json`,
		} as Record<string, string>,
	},
	projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
