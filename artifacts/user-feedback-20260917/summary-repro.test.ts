/**
 * §10 Bug A：SSE 断线恢复状态机（docs/agent-fix-sse-summary-and-multichannel-
 * pseudocolor-2026-09-04.md §10.3 回归矩阵）。
 *
 *   1. reader 在两个事件后 reject：重连 URL 使用最后 seq；事件不重复；最终 finished。
 *   2. POST 在响应头前断线：两次请求 request_id 相同（绝不 newRequestId）。
 *   3. 重连期间点击发送：被状态机阻止，不出现第二个 request_id。
 *   4. Abort / 切片切换：零重连。
 *   5. 五次失败后 session 仍 running：UI 仍是后台运行态，可手工重连/取消。
 *   6. 每条失败路径都不残留 thinking bubble、timer 或双 reader。
 *   7. agent_finished 长 summary 进 assistant bubble，status 行不是全文（§11）。
 *
 * 加载真实 sse.js + renderer.js + sessions.js + main.js（模板加载顺序），
 * 迷你 window/DOM shim + Fake ReadableStream reader + 可编程 HP.api（仿
 * ui-s3-s5.test.ts / stop-ai-run.test.ts 的 new Function 注入法）。
 * backoff 定时用 vi.useFakeTimers 推进（flush 用 advanceTimersByTimeAsync，
 * 不得用真实 setTimeout）。
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const uiDir = resolve(here, "../integrations/pathtogether/ui");
const rendererSrc = readFileSync(resolve(uiDir, "renderer.js"), "utf8");
const sseSrc = readFileSync(resolve(uiDir, "sse.js"), "utf8");
const sessionsSrc = readFileSync(resolve(uiDir, "sessions.js"), "utf8");
const mainSrc = readFileSync(resolve(uiDir, "main.js"), "utf8");

// ---------- 迷你 DOM（支持按 class 查询，供 thinking 气泡/动作行断言） ----------
class FakeElement {
	tagName: string;
	innerHTML = "";
	_textValue = "";
	title = "";
	children: FakeElement[] = [];
	parentNode: FakeElement | null = null;
	scrollTop = 0;
	scrollHeight = 0;
	dataset: Record<string, string> = {};
	style: Record<string, string> = {};
	disabled = false;
	value = "";
	closed = false;
	_rawText = "";
	_listeners: Record<string, Array<(ev: unknown) => void>> = {};
	_attrs: Record<string, string> = {};
	_classes = new Set<string>();
	classList: { add: (...c: string[]) => void; remove: (...c: string[]) => void; toggle: (c: string, v?: boolean) => void; contains: (c: string) => boolean };
	// className 与 classList 必须双向同步：renderer 既用 `el.className = ...`
	// 又用 `el.classList.add(...)`，按 class 查询的断言才能命中。
	get className(): string { return Array.from(this._classes).join(" "); }
	set className(v: string) {
		// 原地改写（不得替换 _classes 引用——classList 闭包持有同一个 Set）
		this._classes.clear();
		String(v).split(/\s+/).filter(Boolean).forEach((c) => this._classes.add(c));
	}
	setAttribute(name: string, v: string): void { this._attrs[name] = String(v); }
	getAttribute(name: string): string | null { return name in this._attrs ? this._attrs[name]! : null; }
	// 叶子节点存自身文本；容器节点递归聚合子节点（appendStatusRow 等写叶子）
	get textContent(): string {
		if (this.children.length) return this.children.map((c) => c.textContent).join("");
		return this._textValue;
	}
	set textContent(v: string) { this._textValue = String(v); }
	constructor(tagName: string) {
		this.tagName = tagName;
		const set = this._classes;
		this.classList = {
			add: (...cls) => cls.forEach((c) => set.add(c)),
			remove: (...cls) => cls.forEach((c) => set.delete(c)),
			toggle: (c: string, v?: boolean) => { if (v === undefined) { set.has(c) ? set.delete(c) : set.add(c); } else if (v) set.add(c); else set.delete(c); },
			contains: (c) => set.has(c),
		};
	}
	get lastElementChild(): FakeElement | null { return this.children.length ? this.children[this.children.length - 1]! : null; }
	appendChild(el: FakeElement): FakeElement {
		if (el.parentNode) el.parentNode.removeChild(el);
		el.parentNode = this;
		this.children.push(el);
		return el;
	}
	insertBefore(el: FakeElement, ref: FakeElement | null): FakeElement {
		if (!ref) return this.appendChild(el);
		if (el.parentNode) el.parentNode.removeChild(el);
		el.parentNode = this;
		const i = this.children.indexOf(ref);
		if (i >= 0) this.children.splice(i, 0, el);
		else this.children.push(el);
		return el;
	}
	removeChild(el: FakeElement): FakeElement {
		const i = this.children.indexOf(el);
		if (i >= 0) this.children.splice(i, 1);
		el.parentNode = null;
		return el;
	}
	remove(): void { if (this.parentNode) this.parentNode.removeChild(this); }
	addEventListener(type: string, fn: (ev: unknown) => void): void { (this._listeners[type] = this._listeners[type] || []).push(fn); }
	removeEventListener(): void {}
	fire(type: string, ev: unknown = { target: this, stopPropagation() {} }): void { (this._listeners[type] || []).slice().forEach((fn) => fn(ev)); }
	querySelector(sel: string): FakeElement | null {
		const all = this.querySelectorAll(sel);
		return all.length ? all[0]! : null;
	}
	querySelectorAll(sel: string): FakeElement[] {
		const out: FakeElement[] = [];
		const parts = sel.split(",").map((s) => s.trim()).filter(Boolean);
		const walk = (el: FakeElement) => {
			for (const c of el.children) {
				const hit = parts.some((part) =>
					part.split(".").every((k) => !k || c.classList.contains(k)));
				if (hit) out.push(c);
				walk(c);
			}
		};
		walk(this);
		return out;
	}
}

const documentShim = {
	readyState: "loading",
	visibilityState: "visible",
	createElement: (tag: string): FakeElement => new FakeElement(tag),
	getElementById: (_id: string): FakeElement | null => null,
	addEventListener: (): void => {},
};

// ---------- Fake SSE reader ----------
const enc = new TextEncoder();
function frame(seq: number, event: string, data: unknown): Uint8Array {
	return enc.encode("id: " + seq + "\nevent: " + event + "\ndata: " + JSON.stringify(data) + "\n\n");
}

interface FakeReader {
	read(): Promise<{ done: boolean; value?: Uint8Array }>;
	cancel(): void;
	fail(err: Error): void;
}

function makeReader(chunks: Uint8Array[], end: "done" | Error, signal?: AbortSignal): FakeReader {
	let i = 0;
	let isCancelled = false;
	let pendingReject: ((e: unknown) => void) | null = null;
	const endErr = end === "done" ? null : end;
	if (signal) {
		signal.addEventListener("abort", () => {
			if (pendingReject) {
				const r = pendingReject;
				pendingReject = null;
				r(abortErr());
			}
		});
	}
	return {
		read() {
			if (isCancelled) return Promise.resolve({ done: true, value: undefined });
			if (i < chunks.length) { const v = chunks[i++]; return Promise.resolve({ done: false, value: v }); }
			if (!endErr) return Promise.resolve({ done: true, value: undefined });
			return new Promise((_res, rej) => { pendingReject = rej; });
		},
		cancel() { isCancelled = true; if (pendingReject) { const r = pendingReject; pendingReject = null; r(abortErr()); } },
		fail(err: Error) { if (pendingReject) { const r = pendingReject; pendingReject = null; r(err); } },
	};
}

/** resp：resp.ok/status/headers.get/body.getReader 最小面（main.js/sse.js 消费）。 */
function sseResp(sid: string | null, chunks: Uint8Array[], end: "done" | Error, signal?: AbortSignal) {
	const reader = makeReader(chunks, end, signal);
	return {
		ok: true,
		status: 200,
		headers: { get: (k: string) => (String(k).toLowerCase() === "x-ai-session-id" ? sid : null) },
		body: { getReader: () => reader },
		reader,
	};
}

type ApiHandler = (call: { url: string; opts?: Record<string, unknown> }, idx: number) => unknown;

interface Loaded {
	HP: Record<string, any>;
	S: Record<string, any>;
	trace: FakeElement;
	calls: Array<{ url: string; opts?: Record<string, unknown> }>;
	toasts: Array<{ msg: string; kind: string }>;
}

function loadUi(apiHandler: ApiHandler): Loaded {
	const calls: Array<{ url: string; opts?: Record<string, unknown> }> = [];
	const toasts: Array<{ msg: string; kind: string }> = [];
	const trace = new FakeElement("div");
	const btn = () => new FakeElement("button");
	const noop = () => {};
	const w = {
		HistoPilot: {
			s: { els: {} },
			t: (k: string, vars?: { e?: string }) => (vars && vars.e ? `${k}:${vars.e}` : k),
			tt: (k: string) => k,
			esc: String,
			fmtAiMag: (m: unknown) => String(m),
			fmtNum: (v: unknown) => String(v),
			truncateStr: (s: unknown) => String(s),
			fmtMsgTs: () => "",
			toast: (msg: string, kind: string) => { toasts.push({ msg, kind }); },
			setOverlay: () => {},
			api: (url: string, opts?: Record<string, unknown>) => {
				calls.push({ url, opts });
				return apiHandler(calls[calls.length - 1]!, calls.length - 1);
			},
			bridge: { request: () => Promise.resolve({}), emit: () => {} },
			aiCredentialsReady: () => true,
			newRequestId: (() => {
				let n = 0;
				return () => "req_" + (++n);
			})(),
			aiResponseError: async (resp: { status?: number }) => `HTTP ${resp.status || 0}`,
			refreshAiSessionSwitcher: noop,
		},
	};
	const HP = w.HistoPilot as unknown as Record<string, any>;
	new Function("window", "document", rendererSrc)(w, documentShim);
	new Function("window", "document", sseSrc)(w, documentShim);
	new Function("window", "document", sessionsSrc)(w, documentShim);
	new Function("window", "document", mainSrc)(w, documentShim);
	// sessions.js 加载时会把同名导出覆盖到 HP 上：把会打网络/弹层的副作用
	// 函数重新收口为 stub（被测路径本身用真实实现）。
	HP.refreshAiSessionSwitcher = noop;
	const S = HP.s as Record<string, any>;
	S.slide = { name: "t.svs", width: 10000, height: 8000 };
	S.aiSlideEpoch = 0;
	S.aiRunning = false;
	S.aiPaused = false;
	S.aiSessionId = null;
	S.activeAiSession = null;
	S.els = {
		// 升级 D：空白输入不触发发送（不再回退默认「全片读片」任务）→
		// 恢复状态机用例统一预置非空任务文本。
		aiTask: { value: "看下边缘区域", style: {} },
		aiTrace: trace,
		aiStartBtn: btn(), aiContinueBtn: btn(), aiFreshBtn: btn(),
		aiStopBtn: btn(), aiComposerAux: btn(),
		aiSessionBar: new FakeElement("div"), aiSessionSelect: new FakeElement("select"),
		aiConfigWrap: { style: {} }, aiConfigCollapsed: { style: {} },
		aiDegradeBanner: { style: {}, textContent: "" },
	};
	S.mainAiCtx = { container: trace, bubbleEl: null, thinkingEl: null, isFork: false, lastSeq: 0 };
	S.aiSessionRefreshGen = 0;
	return { HP, S, trace, calls, toasts };
}

function streamCalls(L: Loaded) {
	return L.calls.filter((c) => c.url.includes("/stream"));
}

/** 假定时器下推进微任务（pump 链全是 promise；绝不能用真实 setTimeout）。 */
function flush(): Promise<void> {
	return vi.advanceTimersByTimeAsync(0).then(() => undefined);
}

function abortErr(): Error {
	const e = new Error("aborted");
	e.name = "AbortError";
	return e;
}

/** 推进 backoff 定时直到 phase 变化/次数上限（jitter 最多 +20%，单步 10s 覆盖 8s 档）。 */
async function advanceUntil(L: Loaded, phase: string, maxSteps = 12): Promise<void> {
	for (let i = 0; i < maxSteps && L.S.aiPhase !== phase; i++) {
		await vi.advanceTimersByTimeAsync(10_000);
		await flush();
	}
}

const SUMMARY_SHORT = "ai.finished.status";
const LONG_SUMMARY = "本视野未见明确异常细胞。".repeat(90); // ~1260 字中文

afterEach(() => {
	vi.useRealTimers();
});


describe("用户反馈：现有代码缺陷复现（通过表示缺陷存在）", () => {
 it("引导句使独立 finish 总结被跳过", () => {
  const L = loadUi(() => new Promise(() => undefined));
  L.HP.handleAiEvent("text_delta", {text:"已完成观察，下面给出总结。"});
  L.HP.handleAiEvent("agent_finished", {summary:"独立总结：证据与结论。"});
  const values = L.trace.querySelectorAll(".ai-chat-bubble.assistant").map((e:any)=>e._rawText);
  expect(values).toEqual(["已完成观察，下面给出总结。"]);
  expect(L.trace.querySelectorAll(".ai-status.finished")).toHaveLength(1);
 });
 it("回放 finish-only 消息丢失总结", () => {
  const L = loadUi(() => new Promise(() => undefined));
  L.HP.renderAiTranscript([
   {role:"assistant",content:"",tool_calls:[{id:"f1",type:"function",function:{name:"finish",arguments:JSON.stringify({summary:"独立总结：证据与结论。"})}}]},
   {role:"tool",tool_call_id:"f1",content:"已结束"}
  ], {container:L.trace,emphasis:"fork"});
  expect(L.trace.querySelectorAll(".ai-chat-bubble.assistant")).toHaveLength(0);
 });
});
