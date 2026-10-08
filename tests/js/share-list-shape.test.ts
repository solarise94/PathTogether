/**
 * DEF-5（2026-10-08 集成验收）：/api/share/list 返回裸数组，Viewer 读
 * data.shares → 分享列表恒为「暂无分享」。修复：shareListFrom 兼容两种形状
 * （裸数组 / {shares:[...]}）。加载真实 static/app.js，锁定真实裸数组形状下
 * 列表渲染出条目（状态点 + token + 撤销钮），且 {shares:[...]} 旧形状不受影响。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

interface BootEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	textContent: string;
	innerHTML: string;
	value: string;
	children: BootEl[];
	parentNode?: BootEl;
	dataset: Record<string, string>;
	style: Record<string, string>;
	classList: { add(...n: string[]): void; remove(...n: string[]): void; contains(n: string): boolean; toggle(n: string, f?: boolean): boolean };
	setAttribute(k: string, v: string): void;
	getAttribute(k: string): string | null;
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	focus(): void;
	appendChild(c: unknown): void;
	closest(): null;
	getBoundingClientRect(): { left: number; top: number; right: number; bottom: number; width: number; height: number };
}

function bootEl(id = ""): BootEl {
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: BootEl[] = [];
	const el: BootEl = {
		id,
		get className() {
			return Array.from(classes).join(" ");
		},
		set className(v: string) {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
		hidden: false,
		textContent: "",
		innerHTML: "",
		value: "",
		children,
		dataset: {},
		style: {},
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
			cb(Object.assign({ stopPropagation() {}, preventDefault() {} }, evt))),
		focus: () => {},
		appendChild: (c) => {
			if (c && typeof c === "object" && "dispatch" in (c as object)) {
				const child = c as BootEl;
				child.parentNode = el;
				children.push(child);
			}
		},
		closest: () => null,
		getBoundingClientRect: () => ({ left: 10, top: 20, right: 40, bottom: 48, width: 30, height: 28 }),
		getContext: () => new Proxy({}, { get: (t, k) => (k in t ? (t as Record<string, unknown>)[k] : () => undefined) }),
	};
	// textContent 聚合（渲染断言用）
	Object.defineProperty(el, "textContent", {
		get: () => {
			const own = (el as unknown as { _text?: string })._text || "";
			return own + children.map((c) => c.textContent).join("");
		},
		set: (v: string) => {
			(el as unknown as { _text?: string })._text = String(v);
			children.length = 0;
		},
		configurable: true,
	});
	// innerHTML 赋值清空 children
	let innerHTML = "";
	Object.defineProperty(el, "innerHTML", {
		get: () => innerHTML,
		set: (v: string) => {
			innerHTML = String(v);
			if (!innerHTML) children.length = 0;
		},
		configurable: true,
	});
	return el;
}

const SHARE = {
	token: "tok1234567890",
	url: "http://127.0.0.1:8908/s/tok1234567890",
	slides: ["a.svs"],
	status: "active",
	expires_at: 1900000000,
	roi_count: 0,
	roi_sizes: [6, 6.5],
};

function bootApp(shareListBody: unknown) {
	const els: Record<string, BootEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		let body: unknown = [];
		if (u.includes("/api/annotations")) body = { by_slide: {} };
		else if (u.includes("/api/projects")) body = [];
		else if (u.includes("/api/slides")) body = [];
		else if (u.includes("/api/share/list")) body = shareListBody;
		else if (u.includes("/api/auth/info")) body = { auth_enabled: false };
		return Promise.resolve({
			ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve(body),
		}) as Promise<Response>;
	}) as unknown as typeof fetch;
	const doc = {
		readyState: "loading",
		cookie: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = bootEl(id);
			return els[id];
		},
		createElement: (tag = "") => bootEl(tag),
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(docListeners[type] ||= []).push(cb);
		},
		removeEventListener() {},
		querySelector: () => null,
		querySelectorAll: () => [] as BootEl[],
		body: bootEl("body"),
		documentElement: { lang: "zh-CN" },
	};
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: {
			create: () => ({
				container: { style: {}, getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }), insertBefore() {} },
				canvas: {}, viewport: null, addHandler() {}, setMouseNavEnabled() {}, open() {}, close() {}, forceResize() {},
			}),
		},
		HP_API: {},
		matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		requestAnimationFrame: (cb: () => void) => { cb(); return 1; },
		addEventListener() {},
		localStorage: null,
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	return {
		els,
		async flush(times = 12) { for (let i = 0; i < times; i++) await Promise.resolve(); },
	};
}

function teardown() {
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
}

describe("DEF-5：/api/share/list 形状兼容（裸数组 / {shares}）", () => {
	afterEach(teardown);

	it("真实集成形状（裸数组）：列表渲染条目而非「暂无分享」", async () => {
		const app = bootApp([SHARE]);
		await app.flush();
		const list = app.els["share-list"];
		expect(list.children.length).toBeGreaterThan(0);
		const text = list.children.map((c) => c.textContent).join("|");
		expect(text).toContain("tok12345");
	});

	it("旧契约形状（{shares:[...]}）：渲染不受影响", async () => {
		const app = bootApp({ shares: [SHARE] });
		await app.flush();
		const list = app.els["share-list"];
		expect(list.children.length).toBeGreaterThan(0);
		expect(list.children.map((c) => c.textContent).join("|")).toContain("tok12345");
	});

	it("空列表两种形状：显示 暂无分享", async () => {
		for (const body of [[], { shares: [] }]) {
			const app = bootApp(body);
			await app.flush();
			expect(app.els["share-list"].children.length).toBe(1); // .share-empty 提示
			expect(app.els["share-list"].children[0].textContent).toContain("share.empty");
		}
	});
});
