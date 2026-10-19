/**
 * 标注列表加载失败不再冒充「暂无标注」（P3 2026-10-19，生产误报复盘）。
 *
 * 真实 app.js 代码切片（与 anno-panel-refresh.test.ts 同款 harness）：
 *   - 初始加载/换片（loadCurrentSlideAnnotations）：非 401 失败（网络/5xx/
 *     403/非 JSON 体）→ annoListError 显式错误态 + 面板钮放开供查看/重试；
 *     重试成功恢复列表。401(auth_required) 仍静默（apiFetch 跳登录契约由
 *     tests/js/api-fetch-401.test.ts 权威覆盖，此处只断言不闪错误 UI）。
 *   - 刷新失败（refreshCurrentAnnotations）：保留上次成功数据与已渲染行
 *     （renderAnnoPanel 不被清空调用），顶部插失效提示条；成功后标记清除；
 *     刷新成功同时清错误态（先于 editDrag 早退）；currentAnnotations 为空
 *     （仍在错误态）时刷新失败维持错误态、不标失效（review e64acaf3 #1/#2）。
 *   - 迟到响应防护（P2 复核 2026-10-18）：load/refresh 共用 annoListReqGen
 *     世代——同一切片内的反序响应（晚到旧 load 200/500、晚到旧 refresh
 *     200/500）在响应到达、JSON 解析后与 catch 内都按世代+切片身份复核，
 *     旧世代整体静默丢弃；换片后的旧响应同样丢弃（跨切片防护保持）。
 *   - 当前世代 load 失败但本切片已有成功数据：保留数据走失效提示，绝不
 *     置 null（review 047 #1）；数据属于旧切片时仍清空进错误态。
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
	src.indexOf("  // ---------- 标注列表加载失败态（P3 2026-10-19） ----------"),
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
	currentAnnotationsSlide?: any;
	annoListError?: any;
	annoListStale?: boolean;
	annoPanelOpen?: boolean;
	editDrag?: any;
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
		 // annoListReqGen/currentAnnotationsSlide 在生产 app.js 是模块级变量
		 // （被测切片之外），此处按 init 通道补声明：已有数据默认归属当前
		 // 切片，可用 init.currentAnnotationsSlide 指定为别的切片对象。
		 var annoListReqGen = 0;
		 var currentAnnotationsSlide = ("currentAnnotationsSlide" in init)
			 ? init.currentAnnotationsSlide
			 : (init.currentAnnotations ? state.slide : null);
		 var currentAnnotations = ("currentAnnotations" in init) ? init.currentAnnotations : null;
		 var annoListError = ("annoListError" in init) ? init.annoListError : null;
		 var annoListStale = !!init.annoListStale;
		 var annoPanelOpen = !!init.annoPanelOpen;
		 var editItem=null, editing=false,
		     editDrag=("editDrag" in init) ? init.editDrag : null,
		     kbEditSession=null,
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
					 currentAnnotationsSlide: currentAnnotationsSlide,
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
			currentAnnotationsSlide: any;
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
/** 逐调用挂起的 fetch 桩：calls[i](response) 放行第 i+1 次请求（世代反序用） */
function deferredFetch() {
	const calls: Array<(v: unknown) => void> = [];
	const fetchImpl = () =>
		new Promise((res) => { calls.push(res); });
	return { calls, fetchImpl };
}
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

// —— review 047 #1 回归：同一切片内 load/refresh 反序（共用请求世代）——
// 切片对象身份判不了同切片两个请求的新旧：初始加载 500 → 重试挂起 → 保存
// 触发刷新已落地 → 放行旧重试，晚到旧 200 把列表回退、晚到旧 500 把已有
// 数据清成错误页。共用 annoListReqGen 后两种反序都必须整体丢弃。
describe("同切片请求世代：反序响应整体丢弃（review 047 #1）", () => {
	const old3 = { annotations: [
		{ label: "old", count: 2, items: [{ annotation_id: "a" }, { annotation_id: "b" }] },
	] };
	const new4 = { annotations: [
		{ label: "old", count: 2, items: [{ annotation_id: "a" }, { annotation_id: "b" }] },
		{ label: "new", count: 1, items: [{ annotation_id: "c" }] },
	] };

	it("晚到旧 load 200：较新的 refresh 已落地 → 旧响应整体丢弃，列表不回退", async () => {
		const { calls, fetchImpl } = deferredFetch();
		const { harness } = makeHarness(fetchImpl, { annoPanelOpen: true });
		const load1 = harness.load();      // 世代1：初始加载/重试挂起
		const refresh1 = harness.refresh(); // 世代2：保存后的刷新先落地
		calls[1](okResp(new4));
		await refresh1;
		expect(harness.read().currentAnnotations).toEqual(new4);
		// 放行旧 load（200 + 旧 3 行体）：整体丢弃——数据不回退、面板不随旧
		// 数据重渲、也不误标失效；且连旧响应的 json() 都不再消费
		let jsonRead = false;
		calls[0]({ ok: true, status: 200, json: () => {
			jsonRead = true;
			return Promise.resolve(old3);
		} });
		await load1;
		const r = harness.read();
		expect(jsonRead).toBe(false);
		expect(r.currentAnnotations).toEqual(new4);
		expect(r.annoListError).toBeNull();
		expect(r.annoListStale).toBe(false);
		expect(harness.counters.panelRender).toBe(1); // 只有刷新那一次整表重渲
	});

	it("晚到旧 load 500：不清空已落地数据、不进错误页、不误标失效", async () => {
		const { calls, fetchImpl } = deferredFetch();
		const { harness, els } = makeHarness(fetchImpl, { annoPanelOpen: true });
		const load1 = harness.load();
		const refresh1 = harness.refresh();
		calls[1](okResp(new4));
		await refresh1;
		calls[0](failResp(500));
		await load1;
		const r = harness.read();
		expect(r.currentAnnotations).toEqual(new4);
		expect(r.annoListError).toBeNull();
		expect(r.annoListStale).toBe(false);
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBeNull();
	});

	it("晚到旧 refresh 200：其后较新的 load 已落地 → 丢弃，不覆盖新数据", async () => {
		const { calls, fetchImpl } = deferredFetch();
		const { harness } = makeHarness(fetchImpl, { annoPanelOpen: true });
		const refresh1 = harness.refresh(); // 世代1挂起
		const load2 = harness.load();       // 世代2（重试/重开同切片的加载）
		calls[1](okResp(new4));
		await load2;
		calls[0](okResp(old3));
		await refresh1;
		const r = harness.read();
		expect(r.currentAnnotations).toEqual(new4);
		expect(r.annoListError).toBeNull();
		expect(harness.counters.panelRender).toBe(1);
	});

	it("晚到旧 refresh 500：不给较新 load 已落地的数据标失效/进错误页", async () => {
		const { calls, fetchImpl } = deferredFetch();
		const { harness, els } = makeHarness(fetchImpl, { annoPanelOpen: true });
		const refresh1 = harness.refresh();
		const load2 = harness.load();
		calls[1](okResp(new4));
		await load2;
		calls[0](failResp(500));
		await refresh1;
		const r = harness.read();
		expect(r.currentAnnotations).toEqual(new4);
		expect(r.annoListStale).toBe(false);
		expect(r.annoListError).toBeNull();
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBeNull();
		expect(els.annoPanelList.querySelector(".anno-panel-stale")).toBeNull();
	});

	it("catch 分支世代复核：旧 load 200 不能冲掉较新 refresh 的失败态", async () => {
		const { calls, fetchImpl } = deferredFetch();
		const { harness } = makeHarness(fetchImpl);
		const load1 = harness.load(); // 世代1挂起
		const refresh1 = harness.refresh(); // 世代2
		calls[1](failResp(500)); // 刷新失败（无已落地数据 → 错误态落地）
		await refresh1;
		expect(harness.read().annoListError).toEqual({ status: 500 });
		calls[0](okResp(old3)); // 旧 load 200 晚到 → 丢弃，错误态保持
		await load1;
		const r = harness.read();
		expect(r.annoListError).toEqual({ status: 500 });
		expect(r.currentAnnotations).toBeNull();
	});
});

// —— review 047 #1 第二项：当前世代 load 失败但已有本切片成功数据 ——
describe("load 失败而已有本切片成功数据：保留数据走失效提示", () => {
	it("重试/初始加载失败：数据保留、顶部插失效条，绝不置 null", async () => {
		const prev = { annotations: [
			{ label: "old", count: 1, items: [{ annotation_id: "o1" }] },
		] };
		const { harness, state, els } = makeHarness(() => Promise.resolve(failResp(500)), {
			currentAnnotations: prev, // 默认归属当前切片
			annoPanelOpen: true,
		});
		const rowA = makeEl("div"); rowA.className = "anno-item";
		const rowB = makeEl("div"); rowB.className = "anno-item";
		els.annoPanelList.appendChild(rowA);
		els.annoPanelList.appendChild(rowB);

		await harness.load();
		const r = harness.read();
		expect(r.currentAnnotations).toBe(prev); // 上次成功数据原封不动
		expect(r.currentAnnotationsSlide).toBe(state.slide); // 归属不变
		expect(r.annoListError).toBeNull();
		expect(r.annoListStale).toBe(true);
		expect(els.annoBtn.disabled).toBe(false); // 面板钮不滞留禁用
		// 失效条 + 旧行照旧（绝不整表清空）
		expect(els.annoPanelList.children).toHaveLength(3);
		expect(els.annoPanelList.children[0].className).toBe("anno-panel-stale");
		expect(els.annoPanelList.children[1]).toBe(rowA);
		expect(els.annoPanelList.children[2]).toBe(rowB);
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBeNull();
	});

	it("数据属于旧切片（跨切片残留）：失败仍清空进错误态，不冒充当前切片", async () => {
		const prev = { annotations: [
			{ label: "prev-slide", count: 1, items: [] },
		] };
		const { harness } = makeHarness(() => Promise.resolve(failResp(500)), {
			currentAnnotations: prev,
			currentAnnotationsSlide: { id: "slide-0", name: "prev.svs" },
		});
		await harness.load();
		const r = harness.read();
		expect(r.currentAnnotations).toBeNull();
		expect(r.currentAnnotationsSlide).toBeNull();
		expect(r.annoListError).toEqual({ status: 500 });
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

// —— review e64acaf3 回归：错误态 × 刷新结果的状态机 ——
describe("刷新成功/失败与错误态的交互（review e64acaf3 #1/#2）", () => {
	it("#1 初始失败后刷新成功：annoListError 清除，重开面板渲染行而非错误块", async () => {
		// 复现路径：初始加载失败（错误态）→ 用户保存新标记触发
		// refreshCurrentAnnotations 成功 → 关/重开面板——修复前
		// openAnnoPanel 仍因 annoListError 渲染错误块
		const { harness, els } = makeHarness(() => Promise.resolve(okResp(fresh)), {
			annoListError: { status: 500 },
			currentAnnotations: null,
			annoPanelOpen: false,
		});
		await harness.refresh();
		const r = harness.read();
		expect(r.annoListError).toBeNull();
		expect(r.currentAnnotations).toEqual(fresh);
		expect(r.annoListStale).toBe(false);
		harness.openPanel();
		expect(harness.counters.panelRender).toBe(1);
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBeNull();
	});

	it("#1 面板开着且在错误态时刷新成功：错误块被真实列表整表重渲替换", async () => {
		const { harness, els } = makeHarness(() => Promise.resolve(okResp(fresh)), {
			annoListError: { status: 500 },
			currentAnnotations: null,
			annoPanelOpen: true,
		});
		// 模拟面板当前显示的错误块（初始失败时 renderAnnoPanelError 的产物）
		const errBox = makeEl("div");
		errBox.className = "anno-panel-error";
		els.annoPanelList.appendChild(errBox);

		await harness.refresh();
		const r = harness.read();
		expect(r.annoListError).toBeNull();
		expect(r.currentAnnotations).toEqual(fresh);
		expect(harness.counters.panelRender).toBe(1); // 整表重渲
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBeNull();
	});

	it("#1 editDrag 手势中的刷新成功：错误态仍清除（先于早退），数据按手势语义不落地", async () => {
		const { harness, els } = makeHarness(() => Promise.resolve(okResp(fresh)), {
			annoListError: { status: 500 },
			currentAnnotations: null,
			annoPanelOpen: true,
			editDrag: { active: true },
		});
		// 面板当前显示的错误块
		const errBox = makeEl("div");
		errBox.className = "anno-panel-error";
		els.annoPanelList.appendChild(errBox);

		await harness.refresh();
		const r = harness.read();
		expect(r.annoListError).toBeNull(); // 先于早退清除，不滞留到下轮
		expect(r.currentAnnotations).toBeNull(); // 手势中不重建（既有契约）
		expect(harness.counters.panelRender).toBe(0);
		// 手势中未重渲：错误块暂留 DOM，但状态已清——手势结束后的保存路径
		// 会再刷新，届时整表重渲为真实列表
		expect(els.annoPanelList.querySelector(".anno-panel-error")).toBe(errBox);
	});

	it("#2 错误态下刷新失败（面板开着）：保持错误态并更新状态码，不标失效", async () => {
		// 修复前：annoListStale=true + 失效条叠在错误块上（自相矛盾：
		// 「上次成功数据」并不存在）
		const { harness, els } = makeHarness(() => Promise.resolve(failResp(503)), {
			annoListError: { status: 500 },
			currentAnnotations: null,
			annoPanelOpen: true,
		});
		const errBox = makeEl("div");
		errBox.className = "anno-panel-error";
		els.annoPanelList.appendChild(errBox);

		await harness.refresh();
		const r = harness.read();
		expect(r.annoListStale).toBe(false);
		expect(r.annoListError).toEqual({ status: 503 }); // 详情更新为最新失败
		expect(els.annoPanelList.querySelector(".anno-panel-stale")).toBeNull();
		const box = els.annoPanelList.querySelector(".anno-panel-error");
		expect(box).toBeTruthy();
		expect(box!.children[0].textContent).toContain("HTTP 503");
		expect(box!.children[1].textContent).toBe("anno.panel.load.retry");
	});

	it("#2 错误态下刷新失败（面板关着）：只更新错误状态，不标失效", async () => {
		const { harness, els } = makeHarness(() => Promise.resolve(failResp(503)), {
			annoListError: { status: 500 },
			currentAnnotations: null,
			annoPanelOpen: false,
		});
		await harness.refresh();
		const r = harness.read();
		expect(r.annoListStale).toBe(false);
		expect(r.annoListError).toEqual({ status: 503 });
		expect(els.annoPanelList.querySelector(".anno-panel-stale")).toBeNull();
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
