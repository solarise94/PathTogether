/**
 * 等待页轮询控制器（static/activate-waiting.js，R7 2026-09-19 修复：
 * 轮询上限失效）：加载真实源码（注入假定时器 / 假 loadState / 假
 * document.hidden），锁定：
 *   - 连续 pending 响应下自动请求恰为 maxRequests（20）次后停止；
 *   - 预算耗尽后再次收到 pending 不重置预算（定时器不重启）；
 *   - 已在 pending 态的刷新回调不重置预算、不重启定时器；
 *   - 页面 hidden 时不发请求（预算不动），恢复可见后继续；
 *   - pagehide（stop）后定时器不再触发；
 *   - 离开等待态后再次进入是新等待回合（重新武装预算）。
 * 手动「刷新状态」语义：只发单次请求、不重新武装预算——由
 * 「耗尽后 enterPending 不重置」覆盖（见 activate-waiting.js 文件头注释）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(resolve(here, "../../static/activate-waiting.js"), "utf8");

interface FakeTimer {
	fn: () => void;
	at: number;
	interval: number;
}

interface FakeClock {
	calls: { setInterval: number; clearInterval: number };
	setInterval(fn: () => void, ms: number): number;
	clearInterval(id: number): void;
	advance(ms: number): void;
}

/** 最小假定时器：按到期时间逐个触发（tick 内部 clearInterval 安全）。 */
function makeClock(): FakeClock {
	let nextId = 1;
	let now = 0;
	const timers = new Map<number, FakeTimer>();
	const clock: FakeClock = {
		calls: { setInterval: 0, clearInterval: 0 },
		setInterval(fn, ms) {
			const id = nextId++;
			clock.calls.setInterval += 1;
			timers.set(id, { fn, at: now + ms, interval: ms });
			return id;
		},
		clearInterval(id) {
			clock.calls.clearInterval += 1;
			timers.delete(id);
		},
		advance(ms) {
			const target = now + ms;
			for (;;) {
				let dueId: number | null = null;
				let dueAt = Infinity;
				timers.forEach((t, id) => {
					if (t.at < dueAt) { dueAt = t.at; dueId = id; }
				});
				if (dueId === null || dueAt > target) break;
				now = dueAt;
				const t = timers.get(dueId)!;
				t.at = now + t.interval;
				t.fn();
			}
			now = target;
		},
	};
	return clock;
}

interface Controller {
	enterPending(): void;
	stop(): void;
	isPolling(): boolean;
	requestsLeft(): number;
}

interface Env {
	clock: FakeClock;
	ctrl: Controller;
	loadState: { calls: number };
}

function makeEnv(opts: { hidden?: () => boolean } = {}): Env {
	const clock = makeClock();
	const loadState = { calls: 0 };
	const win: Record<string, unknown> = {};
	new Function("window", src)(win);
	const api = win.HPActivateWaiting as {
		createWaitingController(deps: Record<string, unknown>): Controller;
	};
	const ctrl = api.createWaitingController({
		loadState: () => { loadState.calls += 1; },
		intervalMs: 15000,
		maxRequests: 20,
		isHidden: opts.hidden || (() => false),
		setInterval: (fn: () => void, ms: number) => clock.setInterval(fn, ms),
		clearInterval: (id: number) => clock.clearInterval(id),
	});
	return { clock, ctrl, loadState };
}

describe("activate-waiting（等待页轮询控制器）", () => {
	it("连续 pending 响应下自动请求恰为 20 次后停止（不再触发）", () => {
		const env = makeEnv();
		env.ctrl.enterPending();  // 页面加载后首次进入等待态
		expect(env.clock.calls.setInterval).toBe(1);
		// 40 个周期，每个周期模拟响应 pending → showApplyBlock → enterPending
		for (let i = 0; i < 40; i++) {
			env.clock.advance(15000);
			env.ctrl.enterPending();
		}
		expect(env.loadState.calls).toBe(20);
		expect(env.ctrl.isPolling()).toBe(false);
		expect(env.ctrl.requestsLeft()).toBe(0);
		// 预算耗尽：再推进也不再发请求
		env.clock.advance(15000 * 5);
		expect(env.loadState.calls).toBe(20);
	});

	it("预算耗尽后再次收到 pending 不重置预算、不重启定时器", () => {
		const env = makeEnv();
		env.ctrl.enterPending();
		for (let i = 0; i < 40; i++) {
			env.clock.advance(15000);
			env.ctrl.enterPending();
		}
		expect(env.loadState.calls).toBe(20);
		// 再次收到 pending（含手动刷新后的响应）：保持停止
		env.ctrl.enterPending();
		env.clock.advance(15000 * 10);
		expect(env.loadState.calls).toBe(20);
		expect(env.ctrl.isPolling()).toBe(false);
		// 定时器自首次武装以来从未重启（修复前每次 pending 都 startPolling 重置 20）
		expect(env.clock.calls.setInterval).toBe(1);
	});

	it("已在 pending 态的刷新回调不重置预算、不重启定时器", () => {
		const env = makeEnv();
		env.ctrl.enterPending();
		env.clock.advance(15000 * 3);  // 3 次请求，剩 17
		expect(env.loadState.calls).toBe(3);
		// pending 响应反复到达（普通轮询/焦点刷新回调）
		env.ctrl.enterPending();
		env.ctrl.enterPending();
		expect(env.ctrl.requestsLeft()).toBe(17);
		expect(env.clock.calls.setInterval).toBe(1);
		// 预算总量仍是最初武装的 20（3 + 17）
		env.clock.advance(15000 * 30);
		expect(env.loadState.calls).toBe(20);
	});

	it("页面 hidden 时不发请求（预算不动）；恢复可见后继续", () => {
		let hidden = true;
		const env = makeEnv({ hidden: () => hidden });
		env.ctrl.enterPending();
		env.clock.advance(15000 * 5);
		expect(env.loadState.calls).toBe(0);
		expect(env.ctrl.requestsLeft()).toBe(20);  // 预算未被隐藏周期消耗
		hidden = false;  // 恢复可见：继续轮询，剩余预算保留
		env.clock.advance(15000 * 2);
		expect(env.loadState.calls).toBe(2);
		expect(env.ctrl.requestsLeft()).toBe(18);
	});

	it("pagehide（stop）停止且不再触发", () => {
		const env = makeEnv();
		env.ctrl.enterPending();
		env.clock.advance(15000 * 2);
		expect(env.loadState.calls).toBe(2);
		env.ctrl.stop();  // pagehide / beforeunload 接线
		expect(env.ctrl.isPolling()).toBe(false);
		expect(env.ctrl.requestsLeft()).toBe(0);
		expect(env.clock.calls.clearInterval).toBeGreaterThanOrEqual(1);
		env.clock.advance(15000 * 10);
		expect(env.loadState.calls).toBe(2);  // 停止后不再触发
	});

	it("离开等待态后再次进入是新等待回合（重新武装预算）", () => {
		const env = makeEnv();
		env.ctrl.enterPending();
		env.clock.advance(15000 * 2);  // 2 次请求
		env.ctrl.stop();               // 切到 load-error / auth 等非 pending 区块
		env.ctrl.enterPending();       // 又切回 pending：新等待回合
		expect(env.ctrl.requestsLeft()).toBe(20);
		expect(env.ctrl.isPolling()).toBe(true);
		env.clock.advance(15000 * 3);
		expect(env.loadState.calls).toBe(5);  // 2 + 3
	});
});
