/**
 * 管理工作台 Chromium E2E 配置（一次性修复包 F，§10.1）。
 *
 * webServer 拉起 tests/e2e/e2e_server.py（临时目录 + 内嵌 PostgreSQL +
 * 一次性 owner/user 凭据，凭据只经 E2E_CREDS_FILE 文件传递，不进日志）。
 * 本地 HTTP origin 即可；公网 HTTPS 的 scheme 分离由 Python CSP 回归覆盖。
 *
 * postmaster 泄漏修复（不用 globalSetup：它在 webServer teardown 之前跑，
 * 会把 reuseExistingServer 复用的手动 server 也杀掉）：
 *   - command 用 `exec` 让 python 顶替 sh（gracefulShutdown 的 -pid 才是
 *     python 本体）；SIGTERM + 10s 宽限让 e2e_server 自己走 finally 清理；
 *   - 下方模块加载时注册的 process.on("exit") 只兜底 SIGKILL/崩溃路径：
 *     marker 里的 python 已死才收割 postmaster；还活着（reuse / 手动跑的）
 *     就立刻返回，绝不动它。
 */
import { defineConfig } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import {
	commandOf,
	pidAlive,
	stopEmbeddedPostgres,
} from "./tests/e2e/stop-embedded-postgres";

const PORT = Number(process.env.E2E_PORT || 8907);
const BASE = `http://127.0.0.1:${PORT}`;
const PG_MARKER =
	process.env.E2E_PG_MARKER || `${os.tmpdir()}/pt-e2e-pg-${PORT}.json`;

interface PgMarker {
	pgdata?: string;
	tmp?: string;
	pid?: number;
}

/**
 * 父进程兜底（同步：exit 里不能 await，绝不抛异常）：
 *   - marker 不存在 → no-op（未起过 server，或 python finally 已清理的
 *     happy path）；
 *   - marker.pid 活着且命令行是 e2e_server.py → 那是 reuseExistingServer
 *     复用（或手动跑）的 server，立即返回，绝不动 python、绝不动 postmaster；
 *   - 否则（python 已被 SIGKILL，或 pid 对不上）：stopEmbeddedPostgres 收割
 *     pgdata 的 postmaster，删 tmp，删 marker。
 */
process.on("exit", () => {
	try {
		let marker: PgMarker;
		try {
			marker = JSON.parse(fs.readFileSync(PG_MARKER, "utf8"));
		} catch {
			return;
		}
		if (
			typeof marker.pid === "number" &&
			pidAlive(marker.pid) &&
			commandOf(marker.pid).includes("e2e_server.py")
		) {
			return;
		}
		if (marker.pgdata) stopEmbeddedPostgres(marker.pgdata);
		if (marker.tmp) fs.rmSync(marker.tmp, { recursive: true, force: true });
		try {
			fs.unlinkSync(PG_MARKER);
		} catch {
			// 已不存在
		}
	} catch {
		// exit handler 绝不抛
	}
});

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
		command: `exec python3 tests/e2e/e2e_server.py --port ${PORT}`,
		url: `${BASE}/healthz`,
		reuseExistingServer: !process.env.CI,
		timeout: 180_000,
		gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
		env: {
			...process.env,
			E2E_CREDS_FILE:
				process.env.E2E_CREDS_FILE ||
				`${os.tmpdir()}/pt-e2e-creds-${PORT}.json`,
			E2E_PG_MARKER: PG_MARKER,
		} as Record<string, string>,
	},
	projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
