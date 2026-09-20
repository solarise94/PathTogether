/**
 * 工单 B：主工作台切片行布局 + 标注徽章人数口径。
 *
 * 加载真实 static/app.js（bootApp 风格 harness + URL 感知 fetch stub），锁定：
 *   - 切片行（项目内 + 未归类同构）：名称独占第一行（.slide-top 内只有
 *     .slide-name，无徽章）；标注 pill 在独立第二行（.slide-badges）；meta
 *     为第三行；
 *   - 完整文件名经 title tooltip 与 aria-label 提供（截断不丢信息）；
 *   - annoBadgeText 人数 = 唯一作者数（不按 label 组数计）：
 *     同一 visitor 跨多个 label 组只算 1 人；source=ai 不算人；缺身份不虚构；
 *     author_key/author_kind 优先（author_kind=unknown 跳过）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

// ---------- 假元素（className/innerHTML 同步 class/children） ----------
type Listener = (e?: unknown) => void;

interface FakeEl {
	id: string;
	textContent: string;
	title: string;
	value: string;
	hidden: boolean;
	style: Record<string, string>;
	dataset: Record<string, string>;
	attrs: Record<string, string>;
	children: FakeEl[];
	parentNode: FakeEl | null;
	listeners: Record<string, Listener[]>;
	className: string;
	innerHTML: string;
	classList: {
		add: (n: string) => void;
		remove: (n: string) => void;
		contains: (n: string) => boolean;
		toggle: (n: string, force?: boolean) => boolean;
	};
	setAttribute(k: string, v: string): void;
	getAttribute(k: string): string | null;
	addEventListener(type: string, cb: Listener): void;
	appendChild(c: FakeEl): FakeEl;
	querySelector(sel: string): FakeEl | null;
	querySelectorAll(sel: string): FakeEl[];
	closest(): null;
	contains(other: FakeEl): boolean;
	focus(): void;
	getBoundingClientRect(): { width: number; height: number };
	getContext(): Record<string, unknown>;
}

function fakeEl(id = ""): FakeEl {
	const classes = new Set<string>();
	let rawHtml = "";
	const el: FakeEl = {
		id,
		textContent: "",
		title: "",
		value: "",
		hidden: false,
		style: {},
		dataset: {},
		attrs: {},
		children: [],
		parentNode: null,
		listeners: {},
		get className() {
			return [...classes].join(" ");
		},
		set className(v: string) {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
		get innerHTML() {
			return rawHtml;
		},
		set innerHTML(v: string) {
			rawHtml = String(v);
			el.children.length = 0; // 真实 DOM：innerHTML="" 移除子节点
		},
		classList: {
			add: (n) => void classes.add(n),
			remove: (n) => void classes.delete(n),
			contains: (n) => classes.has(n),
			toggle: (n, force) => {
				const on = force === undefined ? !classes.has(n) : !!force;
				if (on) classes.add(n);
				else classes.delete(n);
				return on;
			},
		},
		setAttribute(k, v) {
			el.attrs[k] = String(v);
		},
		getAttribute(k) {
			return k in el.attrs ? el.attrs[k] : null;
		},
		addEventListener(type, cb) {
			(el.listeners[type] ||= []).push(cb);
		},
		appendChild(c) {
			c.parentNode = el;
			el.children.push(c);
			return c;
		},
		querySelector(sel) {
			return findByClass(el, sel.replace(/^\./, ""))[0] || null;
		},
		querySelectorAll(sel) {
			return findByClass(el, sel.replace(/^\./, ""));
		},
		closest: () => null,
		contains: (other) => el.children.includes(other),
		focus() {},
		getBoundingClientRect: () => ({ width: 320, height: 600 }),
		getContext: () => ({ setTransform() {}, clearRect() {} }),
	};
	return el;
}

// 按 class 深度查找后代（仅支持 ".cls" 形态，覆盖本测试选择器）
function findByClass(root: FakeEl, cls: string): FakeEl[] {
	const out: FakeEl[] = [];
	const walk = (node: FakeEl) => {
		for (const c of node.children) {
			if (c.classList.contains(cls)) out.push(c);
			walk(c);
		}
	};
	walk(root);
	return out;
}

// ---------- URL 感知 fetch + 插值 t 的 bootApp ----------
const LONG_NAME = "TCGA-49-AAR4-01Z-00-DX1.EDB32358-AF23-4F81-A99F-15574A2DE28E.svs";
const SLIDES = [
	{ name: LONG_NAME, width: 1000, height: 1000, mpp_x: 0.5, size_bytes: 123456789 },
	{ name: "b.svs", width: 800, height: 800, mpp_x: null, mpp_source: "missing" },
];
const PROJECTS = [
	{ pid: "p1", name: "项目A", slides: [LONG_NAME], note: "", roi_count: 0 },
];

interface App {
	els: Record<string, FakeEl>;
	flush(): Promise<void>;
}

function bootApp(bySlide: Record<string, unknown>): App {
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Listener[]> = [];
	const rafCbs: Listener[] = [];
	const fakeViewer = {
		container: {
			style: {} as Record<string, string>,
			getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
			insertBefore() {},
		},
		canvas: {},
		viewport: null,
		addHandler(type: string, fn: Listener) {
			rafCbs.push(); // no-op 记录（viewer 事件不驱动本测试）
			void type;
			void fn;
		},
		forceResize: vi.fn(),
	};
	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		let body: unknown = [];
		if (u.includes("/api/annotations")) {
			body = { by_slide: bySlide };
		} else if (u.includes("/api/projects")) {
			body = PROJECTS;
		} else if (u.includes("/api/slides")) {
			body = SLIDES;
		} else if (u.includes("/api/share/list")) {
			body = { shares: [] };
		}
		return Promise.resolve({
			ok: true,
			status: 200,
			clone() {
				return this;
			},
			json: () => Promise.resolve(body),
		});
	}) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "",
		title: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl(id);
			return els[id];
		},
		createElement: () => fakeEl(),
		addEventListener(type: string, cb: Listener) {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		querySelectorAll: () => [] as FakeEl[],
		body: fakeEl("body"),
	};
	const w: Record<string, unknown> = {
		// t 带 {n}/{m} 插值（badge 断言需要真实数字）
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				Object.entries(vars || {}).reduce(
					(s, [key, v]) => s.split(`{${key}}`).join(String(v)),
					k,
				),
			getLang: () => "zh",
		},
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
		requestAnimationFrame: (cb: Listener) => {
			rafCbs.push(cb);
			return rafCbs.length;
		},
		addEventListener() {},
		localStorage: null,
	};
	(globalThis as { document: unknown }).document = doc;
	(globalThis as { window: unknown }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = { create: () => fakeViewer };
	new Function("window", "document", "fetch", "location", appSrc)(
		w, doc, fetchImpl, (w as { location: unknown }).location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	const flush = async () => {
		for (let i = 0; i < 12; i++) await Promise.resolve();
		while (rafCbs.length) (rafCbs.shift() as Listener)();
	};
	return { els, flush };
}

afterEach(() => {
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
});

async function boot(bySlide: Record<string, unknown>): Promise<App> {
	const app = bootApp(bySlide);
	await app.flush();
	return app;
}

function projectSlideRow(app: App): FakeEl {
	const projRow = app.els["project-list"].children.find((c) =>
		c.classList.contains("proj-row"));
	expect(projRow).toBeTruthy();
	const body = projRow!.children.find((c) => c.classList.contains("proj-body"));
	expect(body).toBeTruthy();
	return body!.children[0];
}

function unfiledRow(app: App, name: string): FakeEl {
	const rows = app.els["unfiled-list"].children.filter((c) =>
		c.classList.contains("slide-row"));
	const row = rows.find((r) => r.dataset.name === name);
	expect(row).toBeTruthy();
	return row!;
}

function rowKinds(row: FakeEl): string[] {
	const mid = row.children.find((c) => c.classList.contains("slide-mid"))!;
	expect(mid).toBeTruthy();
	return mid.children.map((c) =>
		c.classList.contains("slide-top") ? "top" :
		c.classList.contains("slide-badges") ? "badges" :
		c.classList.contains("slide-meta") ? "meta" : "other");
}

function pillOf(row: FakeEl): FakeEl {
	const pill = row.querySelectorAll(".anno-pill")[0];
	expect(pill).toBeTruthy();
	return pill!;
}

describe("切片行布局：名称独占首行 / 徽章次行 / meta 第三行（工单 B）", () => {
	it("项目内切片行：.slide-top 只含 .slide-name（无徽章）；徽章在 .slide-badges 次行；meta 第三行", async () => {
		const app = await boot({
			[LONG_NAME]: [
				{ label: "肿瘤", count: 2, items: [{ visitor: "v1" }, { visitor: "v1" }] },
			],
		});
		const row = projectSlideRow(app);
		expect(row.classList.contains("slide-row")).toBe(true);
		// 次序：名称行 → 徽章行 → meta 行
		expect(rowKinds(row)).toEqual(["top", "badges", "meta"]);
		const mid = row.children.find((c) => c.classList.contains("slide-mid"))!;
		const top = mid.children[0];
		// 名称独占第一行：.slide-top 内没有标注徽章
		expect(top.querySelectorAll(".anno-pill")).toEqual([]);
		expect(top.children[0].classList.contains("slide-name")).toBe(true);
		// 徽章独立次行
		expect(mid.children[1].querySelectorAll(".anno-pill").length).toBe(1);
	});

	it("未归类切片行同构（中英共用结构）", async () => {
		const app = await boot({
			"b.svs": [
				{ label: "肿瘤", count: 1, items: [{ visitor: "v1" }] },
			],
		});
		expect(rowKinds(unfiledRow(app, "b.svs"))).toEqual(["top", "badges", "meta"]);
	});

	it("完整文件名经 title tooltip 与 aria-label 提供（截断不丢信息）", async () => {
		const app = await boot({});
		const row = projectSlideRow(app);
		const mid = row.children.find((c) => c.classList.contains("slide-mid"))!;
		const top = mid.children.find((c) => c.classList.contains("slide-top"))!;
		const nameEl = top.children.find((c) => c.classList.contains("slide-name"))!;
		expect(nameEl.title).toBe(LONG_NAME);
		expect(nameEl.getAttribute("aria-label")).toBe(LONG_NAME);
		// 显示文本是截断名（含 …），完整名只经 tooltip/aria 提供
		expect(nameEl.textContent).not.toBe(LONG_NAME);
		expect(nameEl.textContent.includes("…")).toBe(true);
	});
});

describe("annoBadgeText 人数 = 唯一作者（不按 label 组计）", () => {
	it("同一 visitor 跨 2 个 label 组 → 4 标记 · 1 人（不再按组计 2 人）", async () => {
		const app = await boot({
			"b.svs": [
				{ label: "肿瘤", count: 2, items: [{ visitor: "v1" }, { visitor: "v1" }] },
				{ label: "坏死", count: 2, items: [{ visitor: "v1" }, { visitor: "v1" }] },
			],
		});
		expect(pillOf(unfiledRow(app, "b.svs")).textContent)
			.toBe("badge.marks".split("{n}").join("4").split("{m}").join("1"));
	});

	it("visitor 去重 + AI 不算人：4 标记 · 2 人", async () => {
		const app = await boot({
			"b.svs": [
				{ label: "肿瘤", count: 3, items: [
					{ visitor: "v1" }, { visitor: "v2" }, { source: "ai", visitor: "v9" },
				] },
				{ label: "坏死", count: 1, items: [{ visitor: "v1" }] },
			],
		});
		expect(pillOf(unfiledRow(app, "b.svs")).textContent)
			.toBe("badge.marks".split("{n}").join("4").split("{m}").join("2"));
	});

	it("owner_user_id 回退（旧数据无 author_key）：去重计人", async () => {
		const app = await boot({
			"b.svs": [
				{ label: "肿瘤", count: 2, items: [
					{ owner_user_id: "usr_1" }, { owner_user_id: "usr_1" },
				] },
				{ label: "坏死", count: 1, items: [{ owner_user_id: "usr_2" }] },
			],
		});
		expect(pillOf(unfiledRow(app, "b.svs")).textContent)
			.toBe("badge.marks".split("{n}").join("3").split("{m}").join("2"));
	});

	it("author_key/author_kind 优先；author_kind=unknown 与缺身份不计人", async () => {
		const app = await boot({
			"b.svs": [
				{ label: "肿瘤", count: 4, items: [
					{ author_key: "u1", author_kind: "user" },
					{ author_key: "u2", author_kind: "user" },
					{ author_key: "x", author_kind: "unknown" },
					{}, // 缺身份：不虚构人
				] },
			],
		});
		expect(pillOf(unfiledRow(app, "b.svs")).textContent)
			.toBe("badge.marks".split("{n}").join("4").split("{m}").join("2"));
	});

	it("全部匿名/AI：只显示标记数（badge.marks.only，不显示 0 人）", async () => {
		const app = await boot({
			"b.svs": [
				{ label: "肿瘤", count: 2, items: [
					{ source: "ai", visitor: "v9" }, {},
				] },
			],
		});
		expect(pillOf(unfiledRow(app, "b.svs")).textContent)
			.toBe("badge.marks.only".split("{n}").join("2"));
	});
});
