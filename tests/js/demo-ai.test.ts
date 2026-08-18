/**
 * Demo AI 前端：终态 session 不得循环重连 snapshot/stream。
 *
 * 加载真实 static/demo.js（最小 DOM + fetch mock），锁定：
 *   - 页面首次 restore 会对 consumed session 开一次 stream；
 *   - finishRun 刷新 config 时不再请求 /stream 或 snapshot；
 *   - 第二次 finishRun 不会再发起恢复流。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const demoSrc = readFileSync(resolve(here, "../../static/demo.js"), "utf8");

function fakeEl() {
	const el: {
		disabled: boolean;
		textContent: string;
		innerHTML: string;
		className: string;
		value: string;
		closed: boolean;
		_rawText: string;
		parentNode: unknown;
		children: unknown[];
		scrollTop: number;
		classList: { add(): void; remove(): void };
		appendChild(child: unknown): unknown;
		addEventListener(): void;
		getContext(): { clearRect(): void; save(): void; restore(): void };
		getBoundingClientRect(): { width: number; height: number };
	} = {
		disabled: false,
		textContent: "",
		innerHTML: "",
		className: "",
		value: "",
		closed: false,
		_rawText: "",
		parentNode: null,
		children: [],
		classList: { add() {}, remove() {} },
		appendChild(child: unknown) {
			const c = child as typeof el;
			c.parentNode = el;
			el.children.push(c);
			el.innerHTML += c.innerHTML || c.textContent || "";
			return child;
		},
		scrollTop: 0,
		addEventListener() {},
		getContext() { return { clearRect() {}, save() {}, restore() {} }; },
		getBoundingClientRect() { return { width: 0, height: 0 }; },
	};
	return el;
}

function loadDemo(fetchImpl: typeof fetch) {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const listeners: Record<string, Array<() => void>> = {};
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k },
		AbortController,
		fetch: fetchImpl,
		OpenSeadragon: undefined,
	};
	(globalThis as { HP_I18N: { t: (k: string) => string } }).HP_I18N = { t: (k: string) => k };
	const doc = {
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement() { return fakeEl(); },
		addEventListener(type: string, fn: () => void) {
			(listeners[type] ||= []).push(fn);
		},
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	new Function("window", "document", "fetch", demoSrc)(w, doc, fetchImpl);
	return w as {
		HP_DEMO: {
			loadConfig: (opts?: { restore?: boolean }) => Promise<unknown>;
			finishRun: () => void;
			startRun: () => void;
			handleEvent: (type: string, payload?: Record<string, unknown>) => void;
			state: {
				terminal: boolean;
				sessionAttached: boolean;
				sessionId: string | null;
				running: boolean;
				current: { slide_id?: string } | null;
			};
		};
	};
}

function sseResponse(frames = "") {
	const encoder = new TextEncoder();
	return Promise.resolve({
		ok: true,
		status: 200,
		headers: { get: () => null },
		json: () => Promise.resolve({}),
		text: () => Promise.resolve(frames),
		body: new ReadableStream({
			start(controller) {
				if (frames) controller.enqueue(encoder.encode(frames));
				controller.close();
			},
		}),
	});
}

function jsonResponse(body: unknown, headers: Record<string, string> = {}, status = 200) {
	return Promise.resolve({
		ok: status >= 200 && status < 300,
		status,
		headers: { get: (k: string) => headers[k.toLowerCase()] || headers[k] || null },
		json: () => Promise.resolve(body),
		text: () => Promise.resolve(JSON.stringify(body)),
		body: null,
	});
}

describe("demo.js 终态 session 不循环重连", () => {
	const calls: string[] = [];

	beforeEach(() => {
		calls.length = 0;
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	function makeFetch(consumed = true) {
		return vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: consumed ? "consumed" : "available",
					histopilot_session_id: consumed ? "sess_term" : null,
					per_browser_limit: 1,
					per_browser_used: consumed ? 1 : 0,
					per_browser_remaining: consumed ? 0 : 1,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			if (u.includes("/stream") || u.includes("/api/demo/ai/session/")) {
				return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
	}

	it("页面 restore 会对 consumed session 请求一次 stream", async () => {
		const w = loadDemo(makeFetch(true));
		await w.HP_DEMO.loadConfig({ restore: true });
		expect(calls.some((u) => u.includes("/stream"))).toBe(true);
		expect(w.HP_DEMO.state.sessionAttached).toBe(true);
	});

	it("finishRun 刷新 config 不再请求 stream/snapshot", async () => {
		const w = loadDemo(makeFetch(true));
		w.HP_DEMO.state.sessionId = "sess_term";
		w.HP_DEMO.state.sessionAttached = true;
		w.HP_DEMO.state.terminal = true;
		calls.length = 0;
		w.HP_DEMO.finishRun();
		await Promise.resolve();
		await Promise.resolve();
		expect(calls.every((u) => u.includes("/api/demo/config"))).toBe(true);
		expect(calls.some((u) => u.includes("/stream"))).toBe(false);
		expect(calls.some((u) => /\/api\/demo\/ai\/session\/[^?]+$/.test(u))).toBe(false);
	});

	it("连续两次 finishRun 仍不会再开恢复流", async () => {
		const w = loadDemo(makeFetch(true));
		w.HP_DEMO.state.sessionId = "sess_term";
		w.HP_DEMO.state.sessionAttached = true;
		w.HP_DEMO.state.terminal = true;
		w.HP_DEMO.finishRun();
		await Promise.resolve();
		calls.length = 0;
		w.HP_DEMO.finishRun();
		await Promise.resolve();
		await Promise.resolve();
		expect(calls.some((u) => u.includes("/stream"))).toBe(false);
	});
});

describe("demo.js 恢复流断线后重连", () => {
	const calls: string[] = [];

	beforeEach(() => {
		calls.length = 0;
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	function makeFetch(streamFrames: string) {
		return vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: "consumed",
					histopilot_session_id: "sess_live",
					per_browser_limit: 1,
					per_browser_used: 1,
					per_browser_remaining: 0,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			if (u.includes("/stream")) {
				return sseResponse(streamFrames);
			}
			if (u.includes("/api/demo/ai/session/")) {
				return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
	}

	it("非终态恢复流断开后会再次请求 stream", async () => {
		const w = loadDemo(makeFetch("id: 1\nevent: agent_started\ndata: {}\n\n"));
		await w.HP_DEMO.loadConfig({ restore: true });
		await Promise.resolve();
		await Promise.resolve();
		expect(w.HP_DEMO.state.running).toBe(true);
		expect(calls.filter((u) => u.includes("/stream")).length).toBe(1);
		await vi.advanceTimersByTimeAsync(1600);
		expect(calls.filter((u) => u.includes("/stream")).length).toBeGreaterThanOrEqual(2);
		expect(w.HP_DEMO.state.terminal).toBe(false);
	});

	it("终态恢复流断开后不再请求 stream", async () => {
		const w = loadDemo(makeFetch("id: 1\nevent: agent_finished\ndata: {}\n\n"));
		await w.HP_DEMO.loadConfig({ restore: true });
		await Promise.resolve();
		await Promise.resolve();
		const afterRestore = calls.filter((u) => u.includes("/stream")).length;
		expect(afterRestore).toBeGreaterThanOrEqual(1);
		calls.length = 0;
		await vi.advanceTimersByTimeAsync(5000);
		expect(calls.filter((u) => u.includes("/stream")).length).toBe(0);
	});
});

describe("demo.js IP 限流 429", () => {
	it("startRun 遇到 demo_ip_rate_limited 时禁用按钮", async () => {
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/ai/run")) {
				return jsonResponse(
					{ code: "demo_ip_rate_limited", error: "ip limited" },
					{},
					429,
				);
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		w.HP_DEMO.startRun();
		await Promise.resolve();
		await Promise.resolve();
		await Promise.resolve();
		const btn = (globalThis as { document: { getElementById: (id: string) => { disabled: boolean; textContent: string } } })
			.document.getElementById("ai-run-btn");
		expect(w.HP_DEMO.state.running).toBe(false);
		expect(btn.disabled).toBe(true);
		expect(btn.textContent).toBe("demo.ai.run.ip.limited");
	});
});

describe("demo.js 展示 text_delta 并在 agent_paused 结束本轮", () => {
	function idleFetch() {
		return vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: "consumed",
					histopilot_session_id: "sess_term",
					per_browser_limit: 1,
					per_browser_used: 1,
					per_browser_remaining: 0,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
		}) as unknown as typeof fetch;
	}

	it("同一轮 text_delta 合并进一个回答区域，工具事件后另起气泡", () => {
		const w = loadDemo(idleFetch());
		w.HP_DEMO.handleEvent("text_delta", { text: "已检查" });
		w.HP_DEMO.handleEvent("text_delta", { text: "范围" });
		const trace = (globalThis as { document: { getElementById: (id: string) => { children: Array<{ textContent: string; className: string }> } } })
			.document.getElementById("ai-trace");
		expect(trace.children.length).toBe(1);
		expect(trace.children[0].className).toBe("ai-msg agent");
		expect(trace.children[0].textContent).toBe("已检查范围");
		w.HP_DEMO.handleEvent("tool_started", { tool: "snapshot" });
		w.HP_DEMO.handleEvent("text_delta", { text: "结论" });
		expect(trace.children.length).toBe(3);
		expect(trace.children[1].className).toBe("ai-row");
		expect(trace.children[2].textContent).toBe("结论");
	});

	it("agent_paused 视为终态：finishRun 且不再请求 stream", async () => {
		vi.useFakeTimers();
		const calls: string[] = [];
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: "consumed",
					histopilot_session_id: "sess_paused",
					per_browser_limit: 1,
					per_browser_used: 1,
					per_browser_remaining: 0,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		w.HP_DEMO.state.running = true;
		w.HP_DEMO.state.sessionId = "sess_paused";
		w.HP_DEMO.state.sessionAttached = true;
		calls.length = 0;
		w.HP_DEMO.handleEvent("agent_paused", { summary: "已达步数上限", can_continue: true });
		await Promise.resolve();
		await Promise.resolve();
		expect(w.HP_DEMO.state.terminal).toBe(true);
		expect(w.HP_DEMO.state.running).toBe(false);
		expect(calls.every((u) => u.includes("/api/demo/config"))).toBe(true);
		expect(calls.some((u) => u.includes("/stream"))).toBe(false);
		calls.length = 0;
		await vi.advanceTimersByTimeAsync(5000);
		expect(calls.filter((u) => u.includes("/stream")).length).toBe(0);
		vi.useRealTimers();
	});
});
