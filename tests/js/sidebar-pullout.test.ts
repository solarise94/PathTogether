/**
 * 悬停抽出层（改版第四轮 §1.1）——层放置与防闪烁契约。
 *
 * 加载真实 static/app.js（假 DOM harness），经 __PT_TEST_HOOKS 的
 * HP_PROJECT_UI.fb.pullout 驱动，锁定：
 *   - 抽出层挂在 document.body（侧栏裁剪容器之外），绝不挂在 #sidebar 内；
 *   - 层内是克隆的 .fb-pullout-card；位置自命中区 rect 计算（+34/-7）并钳回视口；
 *   - 命中区 → 浮层离开走延迟收回（防闪烁）；hide 立即移除；
 *   - 触屏 pointerenter 不滑出（沿用 §5.2 契约，另见 viewer-folders.test.ts）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

interface FakeEl extends Record<string, unknown> {
	id: string;
	className: string;
	hidden: boolean;
	textContent: string;
	style: Record<string, string>;
	dataset: Record<string, string>;
	getContext(): Record<string, unknown>;
	classList: { add(...n: string[]): void; remove(...n: string[]): void; contains(n: string): boolean; toggle(n: string, f?: boolean): boolean };
	setAttribute(k: string, v: string): void;
	getAttribute(k: string): string | null;
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	appendChild(c: FakeEl): void;
	removeChild(c: FakeEl): void;
	parentNode: FakeEl | null;
	children: FakeEl[];
	querySelector(sel: string): FakeEl | null;
	querySelectorAll(sel: string): FakeEl[];
	getBoundingClientRect(): { left: number; top: number; right: number; bottom: number; width: number; height: number };
}

function bootEl(
	id = "",
	rect?: { left: number; top: number; width: number; height: number },
): FakeEl {
	const classes = new Set<string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: FakeEl[] = [];
	const el: FakeEl = {
		id,
		className: "",
		hidden: false,
		textContent: "",
		style: {},
		dataset: {},
		getContext: () => ({ setTransform() {}, clearRect() {} }),
		parentNode: null,
		children,
		classList: {
			add: (...n) => n.forEach((x) => classes.add(x)),
			remove: (...n) => n.forEach((x) => classes.delete(x)),
			contains: (n) => classes.has(n),
			toggle: (n, force) => {
				const on = force === undefined ? !classes.has(n) : !!force;
				if (on) classes.add(n);
				else classes.delete(n);
				return on;
			},
		},
		setAttribute: (k, v) => el,
		getAttribute: () => null,
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) =>
			cb(Object.assign({ stopPropagation() {}, preventDefault() {}, pointerType: "mouse" }, evt))),
		appendChild: (c) => {
			c.parentNode = el;
			children.push(c);
		},
		removeChild: (c) => {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
			c.parentNode = null;
			return c;
		},
		querySelector: (sel: string) => {
			// 命中区内的预览卡（.fb-card）：测试里直接挂为子节点
			return children.find((c) => String(c.className || "").includes(sel.replace(".", ""))) || null;
		},
		querySelectorAll: () => [],
		cloneNode(): FakeEl {
			// fbPulloutShow 克隆卡片的路径：复制 className + 子树（测试深度足够）
			const copy = bootEl();
			copy.className = el.className;
			for (const c of children) copy.appendChild(c.cloneNode());
			return copy;
		},
		getBoundingClientRect: () => rect
			? { left: rect.left, top: rect.top, right: rect.left + rect.width, bottom: rect.top + rect.height, width: rect.width, height: rect.height }
			: { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 },
	};
	Object.defineProperty(el, "className", {
		get: () => Array.from(classes).join(" "),
		set: (v: string) => {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
	});
	return el;
}

interface PulloutAPI {
	show(hit: FakeEl): void;
	hide(): void;
	scheduleHide(): void;
	current(): FakeEl | null;
	hit(): FakeEl | null;
}

function bootApp(opts: { vw?: number; vh?: number } = {}) {
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const body = bootEl("body");

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
		querySelectorAll: () => [] as FakeEl[],
		body,
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
		HP_I18N: { t: (k: string) => k, getLang: () => "zh", setLang: () => {} },
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: { slide_id_api: true } },
		matchMedia: (q: string) => ({
			matches: q.includes("max-width: 768") ? false : true,
			addEventListener() {},
			addListener() {},
		}),
		fetch: () => Promise.resolve({ ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve([]) }),
		location: { href: "http://local/app", pathname: "/app", search: "" },
		innerWidth: opts.vw ?? 1440,
		innerHeight: opts.vh ?? 900,
		requestAnimationFrame: (cb: () => void) => { cb(); return 1; },
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(winListeners[type] ||= []).push(cb);
		},
		localStorage: null,
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = w.fetch;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;

	// eslint-disable-next-line @typescript-eslint/no-explicit-any, no-new-func
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, w.fetch, w.location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());

	const UI = (w as { HP_PROJECT_UI?: { fb: { pullout: PulloutAPI } } }).HP_PROJECT_UI!;
	return { els, body, UI, winListeners, docListeners };
}

afterEach(() => {
	vi.useRealTimers();
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
});

function makeHit(els: Record<string, FakeEl>, rect: { left: number; top: number; width: number; height: number }) {
	const hit = bootEl("hit-test", rect);
	const card = bootEl("", { left: rect.left, top: rect.top, width: 224, height: 128 });
	card.className = "fb-card";
	hit.appendChild(card);
	const sidebar = els["sidebar"];
	sidebar.appendChild(hit);
	return hit;
}

describe("悬停抽出层（§1.1）", () => {
	it("抽出层挂在 body（侧栏裁剪容器之外），层内为克隆卡；hide 立即移除", () => {
		const h = bootApp();
		const hit = makeHit(h.els, { left: 0, top: 100, width: 224, height: 44 });
		h.UI.fb.pullout.show(hit);
		const pop = h.UI.fb.pullout.current();
		expect(pop).toBeTruthy();
		// 关键契约：层在 body 直挂层，不在 #sidebar 内
		expect(pop!.parentNode).toBe(h.body);
		expect(h.els["sidebar"].children.includes(pop!)).toBe(false);
		expect(String(pop!.className)).toContain("fb-pullout");
		expect(String(pop!.children[0].className)).toContain("fb-pullout-card");
		expect(String(pop!.children[0].className)).toContain("fb-card");

		h.UI.fb.pullout.hide();
		expect(h.UI.fb.pullout.current()).toBeNull();
		expect(h.body.children.includes(pop!)).toBe(false);
	});

	it("位置自命中区右缘起算（零重叠，-7 上移），越界钳回视口", () => {
		const h = bootApp({ vw: 1000, vh: 900 });
		// 正常位置：left = 命中区右缘 224；top = 100 - 7 = 93
		const hit1 = makeHit(h.els, { left: 0, top: 100, width: 224, height: 44 });
		h.UI.fb.pullout.show(hit1);
		let pop = h.UI.fb.pullout.current();
		expect(pop!.style.left).toBe("224px");
		expect(pop!.style.top).toBe("93px");
		h.UI.fb.pullout.hide();

		// 底部越界：卡片 128 高 → top = 900 - 8 - 128 = 764
		const hit2 = makeHit(h.els, { left: 0, top: 850, width: 224, height: 44 });
		h.UI.fb.pullout.show(hit2);
		pop = h.UI.fb.pullout.current();
		expect(pop!.style.top).toBe("764px");
		h.UI.fb.pullout.hide();

		// 右缘越界（窄视口）：left = 1000 - 8 - 240 = 752
		const hit3 = makeHit(h.els, { left: 900, top: 100, width: 100, height: 44 });
		h.UI.fb.pullout.show(hit3);
		pop = h.UI.fb.pullout.current();
		expect(pop!.style.left).toBe("752px");
	});

	it("指针离开命中区：延迟收回（防闪烁）；进入浮层取消；浮层离开立即收", () => {
		vi.useFakeTimers();
		const h = bootApp();
		const hit = makeHit(h.els, { left: 0, top: 100, width: 224, height: 44 });
		h.UI.fb.pullout.show(hit);
		const pop = h.UI.fb.pullout.current()!;

		// 命中区离开 → 调度收回（90ms）；期间指针进入浮层 → 取消
		h.UI.fb.pullout.scheduleHide();
		expect(h.UI.fb.pullout.current()).toBe(pop); // 仍在（延迟中）
		pop.dispatch("pointerenter");
		vi.advanceTimersByTime(200);
		expect(h.UI.fb.pullout.current()).toBe(pop); // 已取消

		// 浮层 pointerleave → 立即移除
		pop.dispatch("pointerleave");
		expect(h.UI.fb.pullout.current()).toBeNull();
	});

	it("show 换卡时旧层立即替换（不叠加）", () => {
		const h = bootApp();
		const hitA = makeHit(h.els, { left: 0, top: 100, width: 224, height: 44 });
		const hitB = makeHit(h.els, { left: 0, top: 200, width: 224, height: 44 });
		h.UI.fb.pullout.show(hitA);
		const first = h.UI.fb.pullout.current();
		h.UI.fb.pullout.show(hitB);
		expect(h.UI.fb.pullout.current()).not.toBe(first);
		expect(h.body.children.includes(first!)).toBe(false);
		expect(h.UI.fb.pullout.hit()).toBe(hitB);
	});
});
