/**
 * 标注列表加载失败不再冒充「暂无标注」（P3 2026-10-18，生产误报复盘）。
 *
 * 真实 app.js 代码切片（与 anno-panel-refresh.test.ts 同款 harness）：
 *   - 初始加载/换片（loadCurrentSlideAnnotations）：非 401 失败（网络/5xx/
 *     403/非 JSON 体）→ annoListError 显式错误态 + 面板钮放开供查看/重试；
 *     重试成功恢复列表。401(auth_required) 仍静默（apiFetch 跳登录契约由
 *     tests/js/api-fetch-401.test.ts 权威覆盖，此处只断言不闪错误 UI）。
 *   - 刷新失败（refreshCurrentAnnotations）：保留上次成功数据与已渲染行
 *     （renderAnnoPanel 不被清空调用），顶部插失效提示条；成功后标记清除。
 *   - 迟到响应防护沿用 slideAtReq 对象身份判定：换片后的旧响应整体丢弃。
 *   - openAnnoPanel：错误态/失效提示的打开入口。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const src = readFileSync(resolve(root, "static/app.js"), "utf8");

// 切片 A：加载/刷新 + 失败态渲染（annoLoadFailDetail → refreshCurrentAnnotations）
const code = src.slice(
	src.indexOf("  // ---------- 标注列表加载失败态（P3 2026-10-18） ----------"),
	src.indexOf("  // 把几何字段写回 item（rect/arrow/freehand/polygon；撤销/冲突恢复共用）"),
);
// 切片 B：openAnnoPanel 的错误态/失效提示入口
const panelCode = src.slice(
	src.indexOf("  function openAnnoPanel() {"),
	src.indexOf("  function closeAnnoPanel() {"),
);

expect(code).toContain("function loadCurrentSlideAnnotations()");
expect(code).toContain("function refreshCurrentAnnotations()");
expect(panelCode).toContain("function openAnnoPanel()");

/** 带子节点/监听的最小假元素（renderAnnoPanelError/Stale 的 DOM 语义足够） */
function makeEl(tag = "div") {
	const el: any = {
		tag,
		className: "",
		textContent: "",
		type: "",
		disabled: false,
		style: {} as Record<string, string>,
		children: [] as any[],
		listeners: {} as Record<string, Array<() => void>>,
		parent: null as any,
		appendChild(c: any) {
			c.parent = el;
			el.children.push(c);
			return c;
		},
		insertBefore(c: any, ref: any) {
			c.parent = el;
			const i = ref ? el.children.indexOf(ref) : -1;
			if (i < 0) el.children.push(c);
			else el.children.splice(i, 0, c);
			return c;
		},
		remove() {
			if (el.parent) {
				const i = el.parent.children.indexOf(el);
				if (i >= 0) el.parent.children.splice(i, 1);
			}
		},
		get firstChild() {
			return el.children.length > 0 ? el.children[0] : null;
		},
		addEventListener(type: string, fn: () => void) {
			(el.listeners[type] ||= []).push(fn);
		},
		click() {
			(el.listeners.click || []).forEach((fn) => fn());
		},
		querySelector(sel: string) {
			const cls = String(sel).replace(/^\./, "");
			const walk = (node: any): any => {
				for (const c of node.children) {
					if (String(c.className).split(" ").includes(cls)) return c;
					const hit = walk(c);
					if (hit) return hit;
				}
				return null;
			};
			return walk(el);
		},
	};
	Object.defineProperty(el, "innerHTML", {
		get: () => "",
		set: () => {
			// 生产代码只以 innerHTML="" 清空列表
			el.children.length = 0;
		},
	});
	return el;
}

interface HarnessInit {
	currentAnnotations?: any;
	annoListError?: any;
	annoListStale?: boolean;
	annoPanelOpen?: boolean;
}

function makeHarness(
	fetchImpl: (url: string) => Promise<any>,
	init: HarnessInit = {},
) {
	const state: any = {
		slide: { id: "slide-1", name: "a.svs" },
		focusAnno: null,
		showAnno: false,
		polygonVertexEdit: null,
	};
	const els: any = {
		annoBtn: makeEl("button"),
		annoAllBtn: makeEl("button"),
		annoPanelList: makeEl("div"),
		annoPanel: makeEl("div"),
		annoPanelTitle: makeEl("span"),
	};
	const t = (k: string, p?: unknown) =>
		k + (p === undefined ? "" : "(" + JSON.stringify(p) + ")");
	const harness = new Function(
		"apiFetch",
		"t",
		"document",
		"state",
		"els",
		"init",
		"counters",
		`
		 // —— 初始值（init 通道；对象引用与外部共享）——
		 var currentAnnotations = ("currentAnnotations" in init) ? init.currentAnnotations : null;
		 var annoListError = ("annoListError" in init) ? init.annoListError : null;
		 var annoListStale = !!init.annoListStale;
		 var annoPanelOpen = !!init.annoPanelOpen;
		 var editItem=null, editing=false, editDrag=null, kbEditSession=null,
		     editBeforeSnapshot=null, flatItems=[], allAnnotationsBySlide=null;
		 function toast(m){counters.toasts.push(m);}
		 function annotationsQueryFor(s){return "/api/annotations?slide_id=" + encodeURIComponent(s.id);}
		 function hpRequest(){return {catch: function(){}};}
		 function updateResearchBusy(){}
		 function slideDisplayName(s){return s && s.name;}
		 function truncateMiddle(s){return String(s == null ? "" : s);}
		 function rebuildFlatItems(){} function redrawAnnoCanvas(){}
		 function updateAnnoToolbarPos(){} function syncAnnoAllBtns(){}
		 function renderFolderBrowser(){} function findSlideInfo(){return null;}
		 function findItemByAnnotationId(){return null;} function buildEditGeom(){return null;}
		 function geomEqual(){return true;} function applyGeomToItem(){}
		 function closeAnnoToolbar(){}
		 // 生产实现以 innerHTML="" 整表清空后重画——stub 保持同一语义，
		 // 让「成功重渲后错误块/提示条消失」的断言落到真实代码上。
		 // 注意：renderAnnoPanelError/Stale 在切片 A 内有真身（函数声明提升
		 // 覆盖本区 stub），断言一律走 els.annoPanelList 的真实 DOM 效果。
		 function renderAnnoPanel(){counters.panelRender++; els.annoPanelList.children.length = 0;}
		 ${code}
		 ${panelCode}
		 return {
			 load: loadCurrentSlideAnnotations,
			 refresh: refreshCurrentAnnotations,
			 openPanel: openAnnoPanel,
			 counters: counters,
			 // 函数体作用域内原始值的读出口（切片赋值发生在本作用域）
			 read: function () {
				 return {
					 currentAnnotations: currentAnnotations,
					 annoListError: annoListError,
					 annoListStale: annoListStale,
					 annoPanelOpen: annoPanelOpen,
					 editItem: editItem,
					 editing: editing,
				 };
			 },
		 };
	`,
	)(
		fetchImpl,
		t,
		{ createElement: (tag: string) => makeEl(tag) },
		state,
		els,
		init,
		{ toasts: [] as string[], panelRender: 0, panelError: 0, panelStale: 0 },
	) as {
		load: () => Promise<void>;
		refresh: () => Promise<void>;
		openPanel: () => void;
		counters: {
			toasts: string[];
			panelRender: number;
			panelError: number;
			panelStale: number;
		};
		read: () => {
			currentAnnotations: any;
			annoListError: any;
			annoListStale: boolean;
			annoPanelOpen: boolean;
			editItem: any;
			editing: boolean;
		};
	};
	return { harness, state, els };
}

/** ok=false 的响应（5xx/403 等非 401 失败） */
const failResp = (status: number) => ({
	ok: false,
	status,
	json: () => Promise.resolve({ error: "boom" }),
});
const okResp = (body: unknown) => ({
	ok: true,
	status: 200,
	json: () => Promise.resolve(body),
});
/** 宏任务等待：一次冲干净被测切片整条 promise 链的微任务 */
const settle = () => new Promise((r) => setTimeout(r, 0));
const fresh = {
	annotations: [
		{
			label: "fixture",
			count: 2,
			items: [{ annotation_id: "f1" }, { annotation_id: "f2" }],
		},
	],
};

describe("标注列表初始加载失败（loadCurrentSlideAnnotations）", () => {
	it("5xx：不冒充空列表——记录错误态、面板钮放开供查看，错误块含状态码与重试", async () => {
		const { harness, els } = makeHarness(() => Promise.resolve(failResp(500)));
		await harness.load();
		const r = harness.read();
		expect(r.currentAnnotations).toBeNull();
		expect(r.annoListError).toEqual({ status: 500 });
		// 修复前：annoBtn 保持禁用，用户永远打不开面板，500 被当成「暂无标注」
		expect(els.annoBtn.disabled).toBe(false);
		expect(els.annoAllBtn.disabled).toBe(true);

		// 面板内渲染显式错误块（文案 + 重试按钮）
		harness.openPanel();
		expect(els.annoPanel.style.display).toBe("flex");
		const box = els.annoPanelList.querySelector(".anno-panel-error");
		expect(box).toBeTruthy();
		expect(box.children[0].textContent).toContain("anno.panel.load.error");
		expect(box.children[0].textContent).toContain("HTTP 500");
		expect(box.children[1].textContent).toBe("anno.panel.load.retry");
	});

	it("网络失败（json() 解析异常）：错误态回落网络异常文案", async () => {
		const { harness, els } = makeHarness(() =>
			Promise.resolve({
				ok: true,
				status: 200,
				json: () => Promise.reject(new Error("invalid json")),
			}));
		await harness.load();
		expect(harness.read().annoListError).toEqual({ status: 0 });
		harness.openPanel();
		const msg = els.annoPanelList.querySelector(".anno-panel-error").children[0];
		expect(msg.textContent).toContain("anno.panel.load.net");
	});

	it("401：静默退出不闪错误态（apiFetch 跳登录，页面即将离开）", async () => {
		const { harness, els } = makeHarness(() => Promise.resolve(failResp(401)));
		await harness.load();
		const r = harness.read();
		expect(r.annoListError).toBeNull();
		expect(els.annoPanelList.children).toHaveLength(0);
	});

	it("错误态点重试：走同一函数重拉，成功后错误态清除、列表与按钮恢复", async () => {
		let failNext = true;
		const { harness, els } = makeHarness(() =>
			failNext
				? Promise.resolve(failResp(503))
				: Promise.resolve(okResp(fresh)));
		await harness.load();
		expect(harness.read().annoListError).toEqual({ status: 503 });
		harness.openPanel();
		const retry = els.annoPanelList.querySelector(".anno-panel-retry");
		expect(retry).toBeTruthy();
		failNext = false;
		retry.click();
		await settle();
		const r = harness.read();
		expect(r.annoListError).toBeNull();
		expect(r.currentAnnotations).toEqual(fresh);
		expect(els.annoBtn.disabled).toBe(false);
		expect(els.annoAllBtn.disabled).toBe(false);
		// 面板开着：成功后整表重渲（错误块随之消失）
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBeNull();
	});

	it("迟到响应（换片后旧响应到达）：整体丢弃，不覆盖新切片也不误报错误", async () => {
		let resolveFetch!: (v: unknown) => void;
		const { harness, state } = makeHarness(
			() => new Promise((res) => { resolveFetch = res; }));
		const pending = harness.load();
		state.slide = { id: "slide-2", name: "b.svs" }; // 用户已换片
		resolveFetch(okResp(fresh));
		await pending;
		const r = harness.read();
		expect(r.currentAnnotations).toBeNull();
		expect(r.annoListError).toBeNull();
	});
});

describe("标注列表刷新失败（refreshCurrentAnnotations）", () => {
	it("5xx：currentAnnotations 与已渲染行保留，顶部插失效提示条", async () => {
		const prev = { annotations: [{ label: "old", count: 1, items: [{ annotation_id: "o1" }] }] };
		const { harness, els } = makeHarness(() => Promise.resolve(failResp(500)), {
			currentAnnotations: prev,
			annoPanelOpen: true,
		});
		// 模拟已渲染的两行（renderAnnoPanel 的产物）
		const rowA = makeEl("div"); rowA.className = "anno-item";
		const rowB = makeEl("div"); rowB.className = "anno-item";
		els.annoPanelList.appendChild(rowA);
		els.annoPanelList.appendChild(rowB);

		await harness.refresh();
		const r = harness.read();
		expect(r.currentAnnotations).toBe(prev); // 上次成功数据原封不动
		expect(r.annoListStale).toBe(true);
		// 修复前：.catch(function(){}) 连提示都没有；此处绝不能清空重渲
		expect(els.annoPanelList.children).toHaveLength(3);
		const stale = els.annoPanelList.children[0];
		expect(stale.className).toBe("anno-panel-stale");
		expect(stale.children[0].textContent).toContain("anno.panel.stale");
		expect(stale.children[1].textContent).toBe("anno.panel.load.retry");
		expect(els.annoPanelList.children[1]).toBe(rowA); // 旧行仍在
		expect(els.annoPanelList.children[2]).toBe(rowB);
	});

	it("刷新成功：失效标记清除，面板随新数据重渲（提示条被整表重渲带走）", async () => {
		const { harness, els } = makeHarness(() => Promise.resolve(okResp(fresh)), {
			currentAnnotations: { annotations: [] },
			annoPanelOpen: true,
		});
		await harness.refresh();
		const r = harness.read();
		expect(r.annoListStale).toBe(false);
		expect(r.currentAnnotations).toEqual(fresh);
	});

	it("失效提示条上的重试：刷新成功后提示状态复位", async () => {
		let failNext = true;
		const prev = { annotations: [{ label: "old", count: 1, items: [] }] };
		const { harness, els } = makeHarness(() =>
			failNext
				? Promise.resolve(failResp(500))
				: Promise.resolve(okResp(fresh)), {
			currentAnnotations: prev,
			annoPanelOpen: true,
		});
		await harness.refresh();
		expect(harness.read().annoListStale).toBe(true);
		const stale = els.annoPanelList.querySelector(".anno-panel-stale");
		failNext = false;
		stale.children[1].click();
		await settle();
		const r = harness.read();
		expect(r.annoListStale).toBe(false);
		expect(r.currentAnnotations).toEqual(fresh);
	});

	it("面板关着时刷新失败：记失效状态，下次打开随行渲染提示", async () => {
		const prev = { annotations: [{ label: "old", count: 1, items: [] }] };
		const { harness, els } = makeHarness(() => Promise.resolve(failResp(500)), {
			currentAnnotations: prev,
			annoPanelOpen: false,
		});
		await harness.refresh();
		const r = harness.read();
		expect(r.annoListStale).toBe(true);
		expect(r.currentAnnotations).toBe(prev);
		// 面板关着：无面板可插提示；下次打开随行补提示条（真实渲染函数落 DOM）
		expect(els.annoPanelList.querySelector(".anno-panel-stale")).toBeNull();
		harness.openPanel();
		const r2 = harness.read();
		expect(r2.annoPanelOpen).toBe(true);
		expect(els.annoPanelList.querySelector(".anno-panel-stale")).toBeTruthy();
	});

	it("401 刷新失败：静默（跳登录契约不在此重复），不标失效", async () => {
		const prev = { annotations: [{ label: "old", count: 1, items: [] }] };
		const { harness } = makeHarness(() => Promise.resolve(failResp(401)), {
			currentAnnotations: prev,
			annoPanelOpen: true,
		});
		await harness.refresh();
		const r = harness.read();
		expect(r.annoListStale).toBe(false);
		expect(r.currentAnnotations).toBe(prev);
	});

	it("迟到响应（换片后旧刷新到达）：丢弃，不给新切片标失效", async () => {
		let resolveFetch!: (v: unknown) => void;
		const prev = { annotations: [{ label: "old", count: 1, items: [] }] };
		const { harness, state } = makeHarness(
			() => new Promise((res) => { resolveFetch = res; }),
			{ currentAnnotations: prev, annoPanelOpen: true });
		const pending = harness.refresh();
		state.slide = { id: "slide-2", name: "b.svs" };
		resolveFetch(failResp(500));
		await pending;
		const r = harness.read();
		expect(r.annoListStale).toBe(false);
		expect(r.currentAnnotations).toBe(prev);
	});
});

describe("openAnnoPanel 失败态入口", () => {
	it("列表从未加载成功：面板打开并渲染错误态（不是 toast 了事）", () => {
		const { harness, els } = makeHarness(() => Promise.resolve(failResp(500)), {
			annoListError: { status: 500 },
		});
		harness.openPanel();
		const r = harness.read();
		expect(r.annoPanelOpen).toBe(true);
		expect(els.annoPanel.style.display).toBe("flex");
		expect(els.annoPanelTitle.textContent).toContain("anno.panel.title.with");
	});

	it("列表在途未加载成功（无错误态）：维持旧提示，面板不开", () => {
		const { harness, els } = makeHarness(() => Promise.resolve(okResp(fresh)));
		harness.openPanel();
		const r = harness.read();
		expect(r.annoPanelOpen).toBe(false);
		expect(els.annoPanel.style.display).not.toBe("flex");
	});

	it("有数据且曾刷新失败：打开面板渲染行 + 失效提示", () => {
		const prev = { annotations: [{ label: "old", count: 1, items: [] }] };
		const { harness, els } = makeHarness(() => Promise.resolve(okResp(fresh)), {
			currentAnnotations: prev,
			annoListStale: true,
		});
		harness.openPanel();
		const r = harness.read();
		expect(r.annoPanelOpen).toBe(true);
		expect(harness.counters.panelRender).toBe(1); // 旧行照常整表渲染
		// 真实 renderAnnoPanelStale（切片内函数）在行顶部插提示条
		const stale = els.annoPanelList.querySelector(".anno-panel-stale");
		expect(stale).toBeTruthy();
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBeNull();
	});
});
