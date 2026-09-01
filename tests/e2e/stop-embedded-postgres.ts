/**
 * 收割内嵌 PostgreSQL postmaster（tests/e2e/pg_reap.py 的 TS 同语义实现）。
 *
 * postmaster 由 pg_ctl 守护化（自成进程组），playwright webServer 的
 * SIGKILL 打不到它；本模块按 pgdata/postmaster.pid 定点收割，先校验命令行
 * 防 PID 复用，绝不误杀无关 postgres。
 *
 * 全同步（Atomics.wait 轮询）：调用方 playwright.config.ts 的
 * process.on("exit") 兜底里不能 await。语义与 HistoPilot 的
 * test/stop-embedded-postgres.ts 对齐：SIGTERM -> 等待 -> 复验命令行 ->
 * SIGKILL 本体 + 整个进程组（残留 backend 跟着组长走，非组长 → ESRCH 忽略）。
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { join, resolve } from "node:path";

const DEFAULT_TIMEOUT_MS = 5_000;
const POLL_INTERVAL_MS = 50;

/** `ps -p <pid> -o command=`；查不到（已死/僵尸/ps 不可用）返回空串。 */
export function commandOf(pid: number): string {
	const r = spawnSync("ps", ["-p", String(pid), "-o", "command="], {
		encoding: "utf8",
	});
	if (r.error || r.status !== 0) return "";
	return r.stdout.trim();
}

/**
 * 进程存在性探测（signal 0 不实际发信号；EPERM = 存在但不可信号）。
 * signal 0 对 zombie 也成功（僵尸已退出、待父进程 reap），按已死处理。
 */
export function pidAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
	} catch (e) {
		return (e as NodeJS.ErrnoException).code === "EPERM";
	}
	const stat = spawnSync("ps", ["-p", String(pid), "-o", "stat="], {
		encoding: "utf8",
	});
	return !(
		stat.status === 0 &&
		stat.stdout.trim().toUpperCase().startsWith("Z")
	);
}

function sleepSync(ms: number): void {
	Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** 读 `<pgdata>/postmaster.pid` 首行 PID；文件缺失/解析失败/pid<=1 → null。 */
function readPostmasterPid(pgdata: string): number | null {
	try {
		const first = fs
			.readFileSync(join(pgdata, "postmaster.pid"), "utf8")
			.split("\n")[0]
			.trim();
		const pid = Number(first);
		return Number.isInteger(pid) && pid > 1 ? pid : null;
	} catch {
		return null;
	}
}

function tryKill(target: number, signal: NodeJS.Signals): void {
	try {
		process.kill(target, signal);
	} catch {
		// ESRCH（进程/进程组已不存在）等：幂等收敛，忽略
	}
}

/** 命令行确实指向我们的 postgres（防 PID 复用误杀）。 */
function looksLikeOurPostmaster(command: string, pgdata: string): boolean {
	return command.includes("postgres") || command.includes(resolve(pgdata));
}

/**
 * 停掉 pgdata 所属的嵌入式 postmaster（若还活着）。同步、幂等，对环境性
 * 情况（无 pid 文件、进程已死、ps 不可用……）不抛异常。
 */
export function stopEmbeddedPostgres(
	pgdata: string,
	opts: { timeoutMs?: number } = {},
): void {
	const pid = readPostmasterPid(pgdata);
	if (pid === null) return;

	const command = commandOf(pid);
	if (!command) return; // 已死（ps 不可用时也不动作）
	if (!looksLikeOurPostmaster(command, pgdata)) return; // PID 被无关进程复用：绝不杀

	try {
		process.kill(pid, "SIGTERM");
	} catch {
		return; // SIGTERM 前恰已退出（ESRCH）等：无事可做
	}

	const deadline = Date.now() + (opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);
	while (pidAlive(pid) && Date.now() < deadline) {
		sleepSync(POLL_INTERVAL_MS);
	}
	if (!pidAlive(pid)) return;

	// 仍活着 → SIGKILL。发 SIGKILL 前再核验一次命令行：等待窗口内原进程
	// 退出且 PID 被复用时，不能杀掉接盘的无辜进程。
	const commandNow = commandOf(pid);
	if (!commandNow || !looksLikeOurPostmaster(commandNow, pgdata)) return;
	tryKill(pid, "SIGKILL");
	// postmaster 常为自己的进程组组长：连组一起收掉残留 backend
	//（非组长 → ESRCH，忽略）。
	tryKill(-pid, "SIGKILL");
}
