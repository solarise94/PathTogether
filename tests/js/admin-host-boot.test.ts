/**
 * /admin 宿主页 boot / 生命周期状态机回归（一次性修复包 A/D）。
 *
 * docs/admin-workbench-ci-one-shot-remediation-plan.md §7（bootstrap JSON v1）与
 * §8.1（宿主状态机）——以下用例在旧实现上必须失败（先红后绿的根因证据）：
 *   - bootstrap 从不可执行 JSON 节点读取，损坏时进入可见 error 态且不建桥
 *     （旧实现读 data-admin-permissions 属性，被 tojson 双引号截断后静默回退
 *     空权限数组并照常发 init）；
 *   - iframe 不预先设置业务 src：boot 先装 load/message 监听器再赋 src，
 *     消除初次 load race（旧实现 src 静态存在于 HTML，脚本晚于 load 即死锁）；
 *   - init 携带 bootstrap 声明的权限（旧实现恒为截断后的 []）；
 *   - 5 秒无握手 → 可见 error 态（handshake_timeout）+ 可恢复 reload；
 *   - 状态变化以 DOM 属性 + 事件表达（旧实现只改一段文本）。
 *
 * 模式：new Function + 假 window/document（与 admin-bridge.test.ts 同款；
 * 真实浏览器行为由 tests/e2e/ Playwright 层覆盖，两层职责不同）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(resolve(here, "../../static/admin-host.js"), "utf8");

interface Posted {
	env: Record<string, unknown>;
	targetOrigin: string;
}

/** 假 iframe：属性存储 + load 监听器捕获（src 赋值时可断言监听器是否已就位）。 */
function fakeIframe(entryUrl: string) {
	const listeners: Record<string, Array<() => void>> = {};
	const attrs: Record<string, string> = {
		"data-entry-url": entryUrl,
		"data-protocol-version": "1.0.0",
		// 旧实现读取的 data 属性（模拟生产截断形态——真实浏览器解析
		// data-admin-permissions="["a","b"]" 后值只剩 "["）
		"data-admin-permissions": "[",
	};
	const contentWindow = {
		postMessage() {},
	};
	const state = { srcSetWithLoadListeners: null as boolean | null };
	const frame = {
		contentWindow,
		src: "",
		addEventListener(type: string, handler: () => void) {
			(listeners[type] ||= []).push(handler);
		},
		getAttribute(name: string) {
			return attrs[name] ?? null;
		},
		setAttribute(name: string, value: string) {
			attrs[name] = value;
			if (name === "src") {
				frame.src = value;
				state.srcSetWithLoadListeners = (listeners.load || []).length > 0;
			}
		},
	};
	return {
		frame,
		contentWindow,
		state,
		fireLoad() {
			for (const h of listeners.load || []) h();
		},
	};
}

function fakeEl() {
	return {
		hidden: true,
		textContent: "",
		appendChild() {},
		addEventListener() {},
		getAttribute: () => null,
		setAttribute(_n: string, v: string) {
			(this as { dataset?: Record<string, string> }).dataset ||= {};
		},
		dispatchEvent() {
			return true;
		},
	};
}

/**
 * 装配宿主页：#admin-bootstrap JSON 节点 + #admin-plugin-frame（data-entry-url）
 * + #admin-host-status 状态容器 + reload 按钮。
 */
function loadHostPage(opts: {
	bootstrapJson?: string;
	entryUrl?: string;
	hash?: string;
	handshakeTimeoutMs?: number;
}) {
	const entryUrl = opts.entryUrl || "/admin/plugin-assets/pathtogether-admin/ui/index.html";
	const iframe = fakeIframe(entryUrl);
	const statusEl = {
		...fakeEl(),
		attributes: {} as Record<string, string>,
		setAttribute(name: string, value: string) {
			this.attributes[name] = value;
		},
		getAttribute(name: string) {
			return this.attributes[name] ?? null;
		},
	};
	const reloadBtn = { ...fakeEl(), clickHandlers: [] as Array<() => void>, addEventListener(_t: string, h: () => void) { this.clickHandlers.push(h); } };
	const posted: Posted[] = [];
	const events: Array<{ type: string; detail: unknown }> = [];
	const crypto = {
		// 计数式确定性熵源：每次 getRandomValues 输出不同（nonce 轮换可断言）
		calls: 0,
		getRandomValues(buf: Uint8Array) {
			this.calls += 1;
			for (let i = 0; i < buf.length; i++) buf[i] = (i * 11 + 5 + this.calls * 29) % 256;
			return buf;
		},
	};
	const win: Record<string, unknown> = {
		crypto,
		location: { hash: opts.hash || "" },
		fetch: async () => {
			throw new Error("raw fetch must not be used");
		},
		console,
		setTimeout,
		clearTimeout,
		addEventListener(type: string, handler: (event: unknown) => void) {
			(winListeners[type] ||= []).push(handler);
		},
		CustomEvent: class {
			type: string;
			detail: unknown;
			constructor(type: string, init?: { detail?: unknown }) {
				this.type = type;
				this.detail = init?.detail;
			}
		},
		dispatchEvent(ev: { type: string; detail?: unknown }) {
			events.push({ type: ev.type, detail: ev.detail });
			return true;
		},
	};
	const winListeners: Record<string, Array<(event: unknown) => void>> = {};
	const doc = {
		readyState: "complete",
		getElementById(id: string) {
			if (id === "admin-plugin-frame") return iframe.frame;
			if (id === "admin-bootstrap") return {
				textContent: opts.bootstrapJson ?? JSON.stringify({
					schemaVersion: 1,
					protocolVersion: "1.0.0",
					permissions: ["admin:overview:read", "admin:users:read"],
					assetUrl: entryUrl,
				}),
			};
			if (id === "admin-host-status") return statusEl;
			if (id === "admin-reload-btn") return reloadBtn;
			return null;
		},
	};
	(win as { document: unknown }).document = doc;
	new Function("window", "document", src)(win, doc);
	const AdminBridgeHost = win.AdminBridgeHost as {
		boot: (win: unknown, doc: unknown, opts?: Record<string, unknown>) => unknown;
	};
	const handle = AdminBridgeHost.boot(win, doc, {
		handshakeTimeoutMs: opts.handshakeTimeoutMs ?? 60,
	}) as {
		_handleIframeLoad: () => void;
		_handleWindowMessage: (event: { source: unknown; data: unknown }) => void;
		reloadPlugin: () => void;
	} | null;
	// boot 装配后把宿主 contentWindow.postMessage 换成可观测（init 经此发出）
	iframe.contentWindow.postMessage = (env: Record<string, unknown>, targetOrigin: string) => {
		posted.push({ env, targetOrigin });
	};
	return { iframe, statusEl, reloadBtn, posted, events, winListeners, win, handle, crypto,
		get messageHandlers() { return winListeners.message || []; } };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
const ticks = async (n = 3) => {
	for (let i = 0; i < n; i++) await tick();
};

describe("admin host boot — bootstrap JSON v1（包 C 回归）", () => {
	it("reads permissions from the #admin-bootstrap JSON node, not the data attribute", () => {
		const page = loadHostPage({});
		page.iframe.fireLoad();
		const init = page.posted.find((p) => p.env.kind === "init");
		expect(init).toBeTruthy();
		// bootstrap 声明的权限完整下发（旧实现读截断的 data 属性 → 恒 []）
		expect(init!.env.adminPermissions).toEqual(["admin:overview:read", "admin:users:read"]);
	});

	it("corrupt bootstrap enters visible error state and never posts init", async () => {
		const page = loadHostPage({ bootstrapJson: "{broken json" });
		expect(page.handle).toBeNull();
		page.iframe.fireLoad();
		await ticks();
		expect(page.posted.filter((p) => p.env.kind === "init")).toHaveLength(0);
		// 可见 error 状态 + 诊断码 bootstrap_invalid（不静默回退空权限）
		expect(page.statusEl.getAttribute("data-admin-host-state")).toBe("error");
		expect(page.statusEl.textContent).toContain("bootstrap_invalid");
	});

	it("bootstrap with unknown schemaVersion or bad assetUrl is rejected", () => {
		const a = loadHostPage({
			bootstrapJson: JSON.stringify({
				schemaVersion: 99, protocolVersion: "1.0.0",
				permissions: [], assetUrl: "/admin/plugin-assets/pathtogether-admin/ui/index.html",
			}),
		});
		expect(a.handle).toBeNull();
		expect(a.statusEl.getAttribute("data-admin-host-state")).toBe("error");

		const b = loadHostPage({
			bootstrapJson: JSON.stringify({
				schemaVersion: 1, protocolVersion: "1.0.0",
				permissions: ["admin:overview:read"],
				// assetUrl 必须是站内允许路径：外域拒绝
				assetUrl: "https://evil.example/ui/index.html",
			}),
		});
		expect(b.handle).toBeNull();
		expect(b.statusEl.getAttribute("data-admin-host-state")).toBe("error");
	});
});

describe("admin host boot — iframe src race（包 D 回归）", () => {
	it("assigns iframe src only after load/message listeners are installed", () => {
		const page = loadHostPage({});
		// boot 完成：业务 src 已按 bootstrap.assetUrl 赋值，且赋值发生时
		// load 监听器已就位（旧实现 src 静态存在于 HTML，脚本晚于 load 即死锁）
		expect(page.iframe.frame.src).toBe(entryUrlOf(page));
		expect(page.iframe.state.srcSetWithLoadListeners).toBe(true);
	});

	it("an iframe load that already finished before boot still completes init", () => {
		// 模拟 load 极快：boot 之后立刻 fireLoad（监听器已就位 → init 必达）
		const page = loadHostPage({});
		page.iframe.fireLoad();
		const init = page.posted.find((p) => p.env.kind === "init");
		expect(init).toBeTruthy();
		expect(init!.env.nonce).toMatch(/^[0-9a-f]{64}$/);
	});
});

function entryUrlOf(page: ReturnType<typeof loadHostPage>) {
	return "/admin/plugin-assets/pathtogether-admin/ui/index.html";
}

describe("admin host boot — lifecycle state machine（包 D 回归）", () => {
	it("handshake timeout after 5s default (configurable) enters visible error state", async () => {
		const page = loadHostPage({ handshakeTimeoutMs: 40 });
		expect(page.statusEl.getAttribute("data-admin-host-state")).toBe("loading");
		page.iframe.fireLoad();
		expect(page.statusEl.getAttribute("data-admin-host-state")).toBe("waiting_handshake");
		await new Promise((r) => setTimeout(r, 80));
		expect(page.statusEl.getAttribute("data-admin-host-state")).toBe("error");
		expect(page.statusEl.textContent).toContain("handshake_timeout");
		// 状态变化派发可测试事件
		expect(page.events.some((e) => e.type === "adminhoststatechange")).toBe(true);
	});

	it("reload rotates nonce: stale responses from the old load are dropped", async () => {
		const page = loadHostPage({ handshakeTimeoutMs: 5000 });
		page.iframe.fireLoad();
		const firstInit = page.posted.find((p) => p.env.kind === "init")!;
		// reload（新 load → 新 nonce；旧 nonce 请求必须被拒绝）
		page.iframe.fireLoad();
		const secondInit = page.posted
			.filter((p) => p.env.kind === "init")
			.at(-1)!;
		expect(secondInit.env.nonce).not.toBe(firstInit.env.nonce);
		const staleRequest = {
			kind: "request", bridge: "admin", protocolVersion: "1.0.0",
			nonce: firstInit.env.nonce, requestId: "r1",
			method: "admin.overview.get", payload: {},
		};
		page.messageHandlers.forEach((h) =>
			h({ source: page.iframe.contentWindow, data: staleRequest }));
		await ticks();
		// 旧 nonce 的请求不得收到任何响应（response 数为 0）
		expect(page.posted.filter((p) => p.env.kind === "response")).toHaveLength(0);
	});

	it("pagehide moves host to disposed state", () => {
		const page = loadHostPage({});
		page.iframe.fireLoad();
		expect(page.statusEl.getAttribute("data-admin-host-state")).toBe("waiting_handshake");
		(page.winListeners.pagehide || []).forEach((h) => h({}));
		expect(page.statusEl.getAttribute("data-admin-host-state")).toBe("disposed");
	});
});
