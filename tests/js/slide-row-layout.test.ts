/**
 * 工单 B：主工作台切片卡布局 + 标注徽章人数口径（改版 2026-10-08 更新）。
 *
 * 加载真实 static/app.js（bootApp 风格 harness + URL 感知 fetch stub），锁定：
 *   - 旧侧栏「切片行」（.slide-row）由文件夹浏览器的堆叠卡片（.fb-hit）承载：
 *     命中区 data-slide-id/data-name 同源；卡内标签（.fb-card-name）+ 标注
 *     徽章（.fb-card-badge）+ 选中标记（.fb-card-dot）+ 缩略图（.fb-card-img）；
 *   - 完整文件名经 title tooltip 提供（截断不丢信息）；
 *   - 打开切片后卡片带 active 类与 aria-pressed=true（选中标记，§5.2）；
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
	dispatch(type: string, evt?: unknown): void;
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
		dispatch(type, evt) {
			(el.listeners[type] || []).forEach((cb) =>
				cb(Object.assign({ stopPropagation() {}, preventDefault() {} }, evt)));
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
const LONG_ID = "sld_rowlong00001";
const B_ID = "sld_rowb00000002";
// P2：列表项带 slide_id/display_name（合同 §2 DTO）；行操作键 = data-slide-id
const SLIDES = [
	{ name: LONG_NAME, slide_id: LONG_ID, display_name: "", width: 1000, height: 1000, mpp_x: 0.5, size_bytes: 123456789 },
	{ name: "b.svs", slide_id: B_ID, display_name: "", width: 800, height: 800, mpp_x: null, mpp_source: "missing" },
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
		open() {},
		setMouseNavEnabled() {},
	};
	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		let body: unknown = [];
		if (u.includes("/api/annotations")) {
			body = { by_slide: bySlide };
		} else if (/\/info$/.test(u) && (u.includes("/api/slides/") || u.includes("/api/slide/"))) {
			// openSlide 的 info 通道（ID/legacy 两形态）：按末段 ref 回对单项
			const seg = u.includes("/api/slides/") ? u.split("/api/slides/")[1] : u.split("/api/slide/")[1];
			const ref = decodeURIComponent(seg.split("/")[0]);
			body = SLIDES.find((s) => s.slide_id === ref || s.name === ref) || SLIDES[0];
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

	const created: FakeEl[] = [];
	const doc = {
		readyState: "loading",
		cookie: "",
		title: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl(id);
			return els[id];
		},
		createElement: () => {
			const el = fakeEl();
			created.push(el);
			return el;
		},
		addEventListener(type: string, cb: Listener) {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		// openSlide 高亮走 document.querySelectorAll(".slide-row, .fb-hit")：
		// 在已创建元素里按 class 查找（支持逗号复合选择器）
		querySelectorAll: (sel: string) => {
			const classes = sel.split(",").map((s) => s.trim().replace(/^\./, ""));
			return created.filter((e) => classes.some((cls) => e.classList.contains(cls)));
		},
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

// 文件夹浏览器（改版 2026-10-08）里的卡片定位：
// 项目内切片 → 先点文件夹卡进入（fbGo 同步渲染），再按 data-slide-id 找卡；
// 未归类切片 → 根目录堆叠里直接找。
function folderCard(app: App, pid: string): FakeEl {
	const card = app.els["fb-stack"].children.find((c) =>
		c.classList.contains("fb-folder") && c.dataset.pid === pid);
	expect(card).toBeTruthy();
	return card!;
}

function projectSlideCard(app: App): FakeEl {
	folderCard(app, "p1").dispatch("click");
	const stack = app.els["fb-stack"].children.filter((c) => c.classList.contains("fb-hit"));
	expect(stack.length).toBe(1);
	return stack[0];
}

function unfiledRow(app: App, name: string): FakeEl {
	const rows = app.els["fb-stack"].children.filter((c) =>
		c.classList.contains("fb-hit"));
	// P2：操作键 data-slide-id（= slide_id）；data.name 兼容按名定位
	const row = rows.find((r) => r.dataset.slideId === name || r.dataset.name === name);
	expect(row).toBeTruthy();
	return row!;
}

function labelParts(card: FakeEl): { label: FakeEl; name: FakeEl; badge: FakeEl | null; dot: FakeEl } {
	const cardEl = card.children.find((c) => c.classList.contains("fb-card"))!;
	expect(cardEl).toBeTruthy();
	const label = cardEl.children.find((c) => c.classList.contains("fb-card-label"))!;
	const name = label.children.find((c) => c.classList.contains("fb-card-name"))!;
	const badge = label.children.find((c) => c.classList.contains("fb-card-badge")) || null;
	const dot = label.children.find((c) => c.classList.contains("fb-card-dot"))!;
	return { label, name, badge, dot };
}

function badgeOf(card: FakeEl): FakeEl {
	const badge = labelParts(card).badge;
	expect(badge).toBeTruthy();
	return badge!;
}

function imgOf(card: FakeEl): FakeEl {
	const cardEl = card.children.find((c) => c.classList.contains("fb-card"))!;
	const img = cardEl.children.find((c) => c.classList.contains("fb-card-img"));
	expect(img).toBeTruthy();
	return img!;
}

describe("切片卡布局：命中区 / 标签 / 缩略图（改版 2026-10-08）", () => {
	it("项目内切片卡：.fb-hit 命中区含 .fb-card 预览卡；标签=名称+徽章+选中标记；缩略图当前叠才创建", async () => {
		const app = await boot({
			[LONG_NAME]: [
				{ label: "肿瘤", count: 2, items: [{ visitor: "v1" }, { visitor: "v1" }] },
			],
		});
		const card = projectSlideCard(app);
		expect(card.classList.contains("fb-hit")).toBe(true);
		const parts = labelParts(card);
		// 标签内：名称 + 徽章 + 选中标记（初始未打开：空标记）
		expect(parts.name.textContent).toContain(LONG_NAME.slice(0, 4));
		expect(parts.badge).toBeTruthy();
		expect(parts.dot.textContent).toBe("");
		// 缩略图：当前叠的卡才创建（本 harness 未注入 slide_id_api → legacy
		// 通道 /api/slide/<ref>/thumbnail；ID 通道为 /api/slides/<id>/thumbnail）
		expect(imgOf(card).src).toContain("thumbnail");
		expect(imgOf(card).src).toContain(encodeURIComponent(LONG_ID));
	});

	it("未归类切片卡同构（名称+徽章+标记+缩略图）", async () => {
		const app = await boot({
			"b.svs": [
				{ label: "肿瘤", count: 1, items: [{ visitor: "v1" }] },
			],
		});
		const parts = labelParts(unfiledRow(app, "b.svs"));
		expect(parts.badge).toBeTruthy();
		expect(parts.dot.textContent).toBe("");
		const unfiledCard = unfiledRow(app, "b.svs");
		expect(imgOf(unfiledCard).src).toContain("thumbnail");
		expect(imgOf(unfiledCard).src).toContain(encodeURIComponent(B_ID));
	});

	it("完整文件名经 title tooltip 提供（截断不丢信息）；aria-label 携带打开语义", async () => {
		const app = await boot({});
		const card = projectSlideCard(app);
		const nameEl = labelParts(card).name;
		expect(nameEl.title).toBe(LONG_NAME);
		// 显示文本是截断名（含 …），完整名只经 tooltip 提供
		expect(nameEl.textContent).not.toBe(LONG_NAME);
		expect(nameEl.textContent.includes("…")).toBe(true);
		// 本 harness 的 t 不查字典：aria-label = 键文本（键名即打开语义断言）
		expect(card.getAttribute("aria-label")).toContain("fb.open.slide.aria");
	});

	it("P2：操作键 = data-slide-id（slide_id）；data-name 保留供搜索/显示", async () => {
		const app = await boot({});
		// 先取根目录未归类卡（进入文件夹后堆叠只显示该文件夹内容）
		const unfiled = unfiledRow(app, "b.svs");
		expect(unfiled.dataset.slideId).toBe(B_ID);
		expect(unfiled.dataset.name).toBe("b.svs");
		const card = projectSlideCard(app);
		expect(card.dataset.slideId).toBe(LONG_ID);
		expect(card.dataset.name).toBe(LONG_NAME);
	});

	it("打开切片：卡片 active + aria-pressed=true（选中标记 ●）", async () => {
		const app = await boot({});
		const card = unfiledRow(app, "b.svs");
		card.dispatch("click");
		await app.flush();
		const after = unfiledRow(app, "b.svs");
		expect(after.classList.contains("active")).toBe(true);
		expect(after.getAttribute("aria-pressed")).toBe("true");
		expect(labelParts(after).dot.textContent).toBe("●");
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
		expect(badgeOf(unfiledRow(app, "b.svs")).textContent)
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
		expect(badgeOf(unfiledRow(app, "b.svs")).textContent)
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
		expect(badgeOf(unfiledRow(app, "b.svs")).textContent)
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
		expect(badgeOf(unfiledRow(app, "b.svs")).textContent)
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
		expect(badgeOf(unfiledRow(app, "b.svs")).textContent)
			.toBe("badge.marks.only".split("{n}").join("2"));
	});
});
