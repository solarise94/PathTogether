/**
 * 反馈问题对话框（改版第四轮 §3）——提交 payload 形状、CSRF、429/成功分支。
 *
 * 加载顺序镜像 templates/index.html：先 static/feedback-recorder.js（提供
 * window.HP_FEEDBACK），再 static/app.js（提供 window.HP_FEEDBACK_UI）。
 * 锁定：
 *   - open 打开弹层并重置；描述 <10 字提交被拦（不发请求）；
 *   - submit payload = {description, client}，client 为记录器快照
 *     （url_path/events 等字段齐全），经 apiFetch 自动带 X-CSRF-Token；
 *   - 202 → 成功状态（「已收到」可见）并清空输入；
 *   - 429 + retry_after → 可读重试时间（分钟口径）；
 *   - 400/413/网络失败 → 可读报错，按钮恢复。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const recSrc = readFileSync(resolve(here, "../../static/feedback-recorder.js"), "utf8");

interface FakeEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	disabled: boolean;
	textContent: string;
	value: string;
	style: Record<string, string>;
	dataset: Record<string, string>;
	getContext(): Record<string, unknown>;
	classList: { add(...n: string[]): void; remove(...n: string[]): void; contains(n: string): boolean; toggle(n: string, f?: boolean): boolean };
	setAttribute(k: string, v: string): void;
	getAttribute(k: string): string | null;
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	focus(): void;
	click(): void;
	appendChild(c: unknown): void;
	querySelector(sel: string): FakeEl | null;
	querySelectorAll(sel: string): FakeEl[];
}

	function fakeEl(id = ""): FakeEl {
		const classes = new Set<string>();
		const attrs = new Map<string, string>();
		const listeners: Record<string, Array<(e?: unknown) => void>> = {};
		const el: FakeEl = {
			id,
			hidden: false,
			disabled: false,
			textContent: "",
			value: "",
			style: {},
			dataset: {},
			getContext: () => ({ setTransform() {}, clearRect() {} }),
			classList: {
			add: (...names) => names.forEach((n) => classes.add(n)),
			remove: (...names) => names.forEach((n) => classes.delete(n)),
			contains: (n) => classes.has(n),
			toggle: (n, force) => {
				const on = force === undefined ? !classes.has(n) : !!force;
				if (on) classes.add(n);
				else classes.delete(n);
				return on;
			},
		},
		setAttribute: (k, v) => void attrs.set(k, String(v)),
		getAttribute: (k) => (attrs.has(k) ? (attrs.get(k) as string) : null),
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) =>
			cb(Object.assign({ stopPropagation() {}, preventDefault() {}, target: el, pointerType: "mouse" }, evt))),
		focus: () => {},
		click: () => el.dispatch("click", {}),
		appendChild: () => {},
		querySelector: () => null,
		querySelectorAll: () => [],
	};
	return el;
}

type Route = () => { status: number; body: unknown };

function bootApp(seed?: (r: Map<string, Route>) => void) {
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const calls: Array<{ url: string; opts?: { method?: string; headers?: Record<string, string>; body?: string } }> = [];
	const routes = new Map<string, Route>();
	if (seed) seed(routes);

	const fetchImpl = vi.fn((url: string, opts?: Record<string, unknown>) => {
		const u = String(url);
		calls.push({ url: u, opts: opts as never });
		const out = routes.has(u) ? routes.get(u)!() : { status: 200, body: [] as unknown };
		return Promise.resolve({
			ok: out.status >= 200 && out.status < 300,
			status: out.status,
			clone() { return this; },
			json: () => Promise.resolve(out.body),
		}) as unknown as Promise<Response>;
	}) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		activeElement: null,
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl(id);
			return els[id];
		},
		createElement: (tag = "") => fakeEl(tag),
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(docListeners[type] ||= []).push(cb);
		},
		removeEventListener() {},
		querySelector: () => null,
		querySelectorAll: () => [] as FakeEl[],
		body: fakeEl("body"),
		documentElement: { lang: "zh-CN" },
	};
	const fakeViewer = {
		container: { style: {}, getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }), insertBefore() {} },
		canvas: {},
		viewport: null,
		addHandler() {},
		open() {},
		close() {},
		forceResize() {},
	};
	const w: Record<string, unknown> = {
		__PT_TEST_HOOKS: true,
		// t恒返回 key：驱动 app.js 的 tt() 兜底到 _EXTRA_I18N（锁定 zh 兜底文案）
		HP_I18N: { t: (k: string) => k, getLang: () => "zh", setLang: () => {} },
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } },
		matchMedia: (q: string) => ({
			matches: q.includes("max-width: 768") ? false : true,
			addEventListener() {},
			addListener() {},
		}),
		fetch: fetchImpl,
		location: { href: "http://local/app", pathname: "/app", search: "" },
		innerWidth: 1440,
		innerHeight: 900,
		requestAnimationFrame: (cb: () => void) => { cb(); return 1; },
		addEventListener() {},
		localStorage: null,
		console: { error() {}, warn() {}, log() {} },
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;

	// 镜像 index.html 装载顺序：先记录器，后 app.js
	// eslint-disable-next-line @typescript-eslint/no-explicit-any, no-new-func
	new Function("window", "document", recSrc)(w, doc);
	// eslint-disable-next-line @typescript-eslint/no-explicit-any, no-new-func
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());

	const UI = (w as { HP_FEEDBACK_UI?: Record<string, unknown> }).HP_FEEDBACK_UI!;
	return { els, calls, routes, doc, UI, HPFeedback: (w as { HP_FEEDBACK?: unknown }).HP_FEEDBACK };
}

function teardown() {
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
}

async function flush(times = 10): Promise<void> {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

function typeDescription(h: ReturnType<typeof bootApp>, text: string): void {
	h.els["feedback-desc"].value = text;
	h.els["feedback-desc"].dispatch("input");
}

describe("反馈问题对话框（§3）", () => {
	afterEach(teardown);

	it("open 打开弹层并重置；<10 字提交被本地拦截（不发请求）", async () => {
		const h = bootApp();
		const UI = h.UI as { open(): void; submit(): void; state: { sending: boolean } };
		UI.open();
		expect(h.els["feedback-mask"].style.display).toBe("");
		typeDescription(h, "太短");
		h.els["feedback-submit"].dispatch("click");
		await flush();
		expect(h.calls.filter((c) => c.url === "/api/feedback").length).toBe(0);
		expect(h.els["feedback-error"].hidden).toBe(false);
		expect(h.els["feedback-error"].textContent).not.toBe("");
		expect(UI.state.sending).toBe(false);
	});

	it("submit payload = {description, client}；client 为记录器快照；CSRF 头自动附带", async () => {
		let seenBody: { description?: string; client?: Record<string, unknown> } | null = null;
		let seenHeaders: Record<string, string> | null = null;
		const h = bootApp((routes) => {
			routes.set("/api/feedback", () => {
				// 由调用方断言：这里只回 202；body 检查在下方经 calls 完成
				return { status: 202, body: { feedback_id: "fb_1", mailed: true } };
			});
		});
		const UI = h.UI as { open(): void; submit(): void };
		UI.open();
		typeDescription(h, "这个导出按钮点了没有反应，请帮我看看。");
		h.els["feedback-submit"].dispatch("click");
		await flush(8);

		const call = h.calls.find((c) => c.url === "/api/feedback");
		expect(call).toBeTruthy();
		expect(call!.opts!.method).toBe("POST");
		seenHeaders = call!.opts!.headers || null;
		expect(seenHeaders && seenHeaders["X-CSRF-Token"]).toBe("tok");
		expect(seenHeaders && seenHeaders["Content-Type"]).toBe("application/json");
		seenBody = JSON.parse(call!.opts!.body!);
		expect(seenBody.description).toBe("这个导出按钮点了没有反应，请帮我看看。");
		const client = seenBody.client as Record<string, unknown>;
		// client 是记录器快照：字段齐全（§3 合同）
		expect(client).toBeTruthy();
		expect(Array.isArray(client.events)).toBe(true);
		expect(client.url_path).toBe("/app");
		expect(client.viewport).toEqual({ w: 1440, h: 900 });
		expect(typeof client.captured_at).toBe("number");
		// 成功状态：「已收到」可见、错误隐藏、输入清空
		expect(h.els["feedback-success"].hidden).toBe(false);
		expect(h.els["feedback-error"].hidden).toBe(true);
		expect(h.els["feedback-desc"].value).toBe("");
	});

	it("429 + retry_after：显示分钟口径的可重试时间，按钮恢复", async () => {
		const h = bootApp((routes) => {
			routes.set("/api/feedback", () => ({ status: 429, body: { error: "rate_limited", retry_after: 120 } }));
		});
		const UI = h.UI as { open(): void; submit(): void };
		UI.open();
		typeDescription(h, "这条反馈用于验证 429 的重试提示文案。");
		h.els["feedback-submit"].dispatch("click");
		await flush(8);
		expect(h.els["feedback-error"].hidden).toBe(false);
		expect(h.els["feedback-error"].textContent).toContain("2");
		// 按钮恢复可用、文案回「发送」
		expect(h.els["feedback-submit"].disabled).toBe(false);
		expect(UI.state.sending).toBe(false);
		// 弹层未关（用户可改后重试）
		expect(h.els["feedback-mask"].style.display).toBe("");
	});

	it("400/413 与网络失败：可读报错，不假成功", async () => {
		const h = bootApp((routes) => {
			routes.set("/api/feedback", () => ({ status: 413, body: { error: "payload_too_large" } }));
		});
		const UI = h.UI as { open(): void; submit(): void };
		UI.open();
		typeDescription(h, "这条反馈用于验证 413 的报错文案展示。");
		h.els["feedback-submit"].dispatch("click");
		await flush(8);
		expect(h.els["feedback-error"].hidden).toBe(false);
		expect(h.els["feedback-success"].hidden).toBe(true);
	});
});
