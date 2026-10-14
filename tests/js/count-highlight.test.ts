/**
 * count_cells 计数范围高亮（方形取景 2026-10-14；平台侧 viewer 叠加）。
 *
 * 加载真实 static/app.js（与 account-email-change.test.ts 同款 loadApp 函数
 * 体注入 harness），锁定：
 *   - scanText（纯函数）：JSON / 双重编码（transcript 里的字符串化工具结果）/
 *     SSE data 帧（手动按帧切）三种形态里的 counted_region.bbox 提取；无
 *     counted_region、坏 JSON、非法 bbox（0/负/非数）一律 null；
 *   - show/clear/current：合法 bbox 记入高亮并派发 pt:count-highlight 事件，
 *     非法几何被忽略（不覆盖旧高亮），clear 幂等；
 *   - fetch tap（install）：AI 路径 JSON 响应被 tee——插件拿到的 Response 仍
 *     支持 json()/clone()（body 内容不变），扫描支命中 counted_region 后设置
 *     高亮；非 AI 路径不触碰；run 发起（/api/ai/run）先清除上一轮高亮。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

function fakeEl() {
	const listeners: Record<string, Set<(e?: unknown) => void>> = {};
	const el: Record<string, unknown> = {
		style: {},
		classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
		dataset: {},
		setAttribute() {},
		getAttribute: () => null,
		removeAttribute() {},
		appendChild() {},
		contains: () => false,
		focus() {},
		addEventListener(type: string, fn: (e?: unknown) => void) {
			(listeners[type] ||= new Set()).add(fn);
		},
		removeEventListener() {},
	};
	el._listeners = listeners;
	return el;
}

function loadApp(fetchImpl: typeof fetch) {
	const loc = { href: "http://local/" };
	const w: Record<string, unknown> = {
		fetch: fetchImpl,
		location: loc,
		HP_I18N: { getLang: () => "zh", t: (k: string) => k },
		HP_APP_MODE: "official",
		HP_CAPABILITIES: {},
		HP_can: () => false,
	};
	const doc = {
		readyState: "loading", // init 延迟到 DOMContentLoaded（harness 不触发）
		cookie: "csrf_token=tok",
		getElementById: () => fakeEl(),
		createElement: () => fakeEl(),
		addEventListener() {},
		removeEventListener() {},
		querySelector() { return fakeEl(); },
		querySelectorAll() { return []; },
		dispatchEvent() { return true; },
		documentElement: fakeEl(),
		body: fakeEl(),
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: unknown }).document = doc;
	(globalThis as { window: unknown }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	(globalThis as { location: unknown }).location = loc;
	(globalThis as { CustomEvent: unknown }).CustomEvent =
		(globalThis as { CustomEvent?: unknown }).CustomEvent ??
		class CustomEvent {
			constructor(public type: string, public init?: { detail?: unknown }) {}
		};
	new Function("window", "document", "fetch", "location", appSrc)(
		w, doc, fetchImpl, loc);
	const mod = w.PTCountHighlight as {
		show: (b: unknown) => boolean;
		clear: () => boolean;
		current: () => unknown;
		scanText: (t: string) => unknown;
		handle: (cr: unknown) => boolean;
		noteOpenSlide: (id: string) => void;
		install: () => void;
	};
	expect(mod).toBeTruthy();
	return { mod, win: w };
}

function sseFrame(data: string) {
	return `id: 1\nevent: x\ndata: ${data}\n\n`;
}

async function flush() {
	await new Promise<void>((r) => { setTimeout(r, 0); });
}

function textStream(chunks: string[]): ReadableStream<Uint8Array> {
	const enc = new TextEncoder();
	return new ReadableStream<Uint8Array>({
		start(controller) {
			for (const c of chunks) controller.enqueue(enc.encode(c));
			controller.close();
		},
	});
}

function makeResp(body: ReadableStream<Uint8Array>, ctype: string): Response {
	return new Response(body, {
		status: 200,
		headers: { "Content-Type": ctype },
	});
}

const BBOX = { x: 10, y: 20, w: 300, h: 300 };
const SLIDE_ID = "sld_a";

function dispatchResult(slideId = SLIDE_ID) {
	return JSON.stringify({
		result: {
			capability: "count_cells",
			counted_region: { bbox: BBOX, slide_id: slideId },
		},
	});
}

afterEach(() => {
	vi.restoreAllMocks();
});

describe("count highlight scanText", () => {
	it("extracts bbox + slide_id from a dispatch result JSON", () => {
		const { mod } = loadApp(vi.fn());
		expect(mod.scanText(dispatchResult())).toEqual({
			bbox: BBOX, slide_id: SLIDE_ID,
		});
	});

	it("unwraps stringified tool results in transcripts (double-encoded)", () => {
		const { mod } = loadApp(vi.fn());
		const transcript = JSON.stringify({
			session: { id: "s" },
			transcript: [
				{ role: "user", content: "hi" },
				{
					role: "tool",
					tool_call_id: "t1",
					content: [{ type: "text", text: dispatchResult() }],
				},
			],
		});
		expect(mod.scanText(transcript)).toEqual({
			bbox: BBOX, slide_id: SLIDE_ID,
		});
	});

	it("multiple counts in one transcript → the latest one wins", () => {
		const { mod } = loadApp(vi.fn());
		const later = { x: 900, y: 900, w: 120, h: 120 };
		const transcript = JSON.stringify({
			transcript: [
				{ role: "tool", content: [{ type: "text", text: dispatchResult() }] },
				{
					role: "tool",
					content: [{ type: "text", text: JSON.stringify({
						result: { counted_region: { bbox: later, slide_id: SLIDE_ID } },
					}) }],
				},
			],
		});
		expect(mod.scanText(transcript)).toEqual({ bbox: later, slide_id: SLIDE_ID });
	});

	it("extracts counted_region carried on the live extra_tool_called event", () => {
		const { mod } = loadApp(vi.fn());
		const data = JSON.stringify({
			tool: "dev_pathsegmentor_measure__count_cells",
			ok: true,
			status: 200,
			counted_region: { bbox: BBOX, slide_id: SLIDE_ID, snapshot_id: "snap-1" },
		});
		expect(mod.scanText(data)).toEqual({ bbox: BBOX, slide_id: SLIDE_ID });
	});

	it("extracts bbox from a single SSE data frame payload", () => {
		const { mod } = loadApp(vi.fn());
		const frame = sseFrame(JSON.stringify({
			type: "extra_tool_called", payload: {
				result: { counted_region: { bbox: BBOX, slide_id: SLIDE_ID } },
			},
		}));
		const dataLine = frame.split("\n").find((l) => l.startsWith("data:"))!
			.slice(5).trim();
		expect(mod.scanText(dataLine)).toEqual({
			bbox: BBOX, slide_id: SLIDE_ID,
		});
	});

	it("returns null for non-JSON / bad JSON / invalid bbox", () => {
		const { mod } = loadApp(vi.fn());
		expect(mod.scanText("plain text counted_region")) === null;
		expect(mod.scanText('{"counted_region": broken')).toBeNull();
		expect(mod.scanText(JSON.stringify({
			counted_region: { bbox: { x: 0, y: 0, w: 0, h: 5 } },
		}))).toBeNull();
		expect(mod.scanText(JSON.stringify({
			counted_region: { bbox: { x: -3, y: 0, w: 5, h: 5 } },
		}))).toBeNull();
		expect(mod.scanText('{"other": 1}')).toBeNull();
	});
});

describe("count highlight show/clear", () => {
	it("show records the box, clear removes it; invalid geometry is ignored", () => {
		const { mod } = loadApp(vi.fn());
		expect(mod.show(BBOX)).toBe(true);
		expect(mod.current()).toEqual(BBOX);
		// 非法几何不覆盖旧高亮
		expect(mod.show({ x: 0, y: 0, w: 0, h: 10 })).toBe(false);
		expect(mod.show(null)).toBe(false);
		expect(mod.current()).toEqual(BBOX);
		expect(mod.clear()).toBe(true);
		expect(mod.current()).toBeNull();
		expect(mod.clear()).toBe(false); // 幂等
	});
});

describe("count highlight slide binding", () => {
	it("draws when counted_region.slide_id matches the open slide", () => {
		const { mod } = loadApp(vi.fn());
		mod.noteOpenSlide(SLIDE_ID);
		expect(mod.handle({ bbox: BBOX, slide_id: SLIDE_ID })).toBe(true);
		expect(mod.current()).toEqual(BBOX);
	});

	it("does not draw for another slide's result", () => {
		const { mod } = loadApp(vi.fn());
		mod.noteOpenSlide(SLIDE_ID);
		expect(mod.handle({ bbox: BBOX, slide_id: "sld_other" })).toBe(false);
		expect(mod.current()).toBeNull();
	});

	it("does not draw when slide_id is missing or empty", () => {
		const { mod } = loadApp(vi.fn());
		mod.noteOpenSlide(SLIDE_ID);
		expect(mod.handle({ bbox: BBOX })).toBe(false);
		expect(mod.handle({ bbox: BBOX, slide_id: "" })).toBe(false);
		expect(mod.current()).toBeNull();
	});

	it("does not draw when no slide is open", () => {
		const { mod } = loadApp(vi.fn());
		expect(mod.handle({ bbox: BBOX, slide_id: SLIDE_ID })).toBe(false);
		expect(mod.current()).toBeNull();
	});

	it("noteOpenSlide with empty id re-binds (slide switch) so the old result no longer draws", () => {
		const { mod } = loadApp(vi.fn());
		mod.noteOpenSlide(SLIDE_ID);
		expect(mod.handle({ bbox: BBOX, slide_id: SLIDE_ID })).toBe(true);
		mod.clear();
		mod.noteOpenSlide("");
		expect(mod.handle({ bbox: BBOX, slide_id: SLIDE_ID })).toBe(false);
	});
});

describe("fetch tap on AI responses", () => {
	it("scans AI JSON responses and sets the highlight; body stays usable", async () => {
		const original = makeResp(textStream([dispatchResult()]),
			"application/json");
		// 构造的 Response 的 url 恒为空串——以自有属性遮蔽模拟真实 fetch 的
		// 非 url 属性，锁定重建后保真（生产路径读 resp.url / resp.redirected）。
		Object.defineProperty(original, "url",
			{ value: "http://local/api/ai/session/s1", configurable: true });
		const fetchImpl = vi.fn(() => Promise.resolve(original)
		) as unknown as typeof fetch;
		const { mod } = loadApp(fetchImpl);
		mod.noteOpenSlide(SLIDE_ID);
		mod.install();
		const resp = await (window as unknown as { fetch: typeof fetch })
			.fetch("/api/ai/session/s1");
		expect(resp).not.toBe(original);  // 插件拿到的是 tee 后重建的 Response
		// 插件视图：url/redirected 保真；json() 仍可用且内容不变
		expect(resp.url).toBe("http://local/api/ai/session/s1");
		expect(resp.redirected).toBe(false);
		const body = await resp.json();
		expect(body.result.counted_region.bbox).toEqual(BBOX);
		await flush();
		expect(mod.current()).toEqual(BBOX);
	});

	it("scans SSE run streams frame by frame", async () => {
		const noise = JSON.stringify({ type: "text_delta", text: "hello" });
		const hit = sseFrame(JSON.stringify({
			type: "extra_tool_called",
			payload: { result: { counted_region: { bbox: BBOX, slide_id: SLIDE_ID } } },
		}));
		const fetchImpl = vi.fn(() => Promise.resolve(makeResp(
			textStream([noise, hit]),
			"text/event-stream"))) as unknown as typeof fetch;
		const { mod } = loadApp(fetchImpl);
		mod.noteOpenSlide(SLIDE_ID);
		mod.install();
		const resp = await (window as unknown as { fetch: typeof fetch })
			.fetch("/api/ai/run");
		const reader = resp.body!.getReader();
		// 原样消费（插件视角流未变）
		let total = 0;
		for (;;) {
			const { done, value } = await reader.read();
			if (done) break;
			total += value.length;
		}
		expect(total).toBeGreaterThan(0);
		await flush();
		expect(mod.current()).toEqual(BBOX);
	});

	it("clears the previous highlight when a new run starts", async () => {
		let mode = "json";
		const fetchImpl = vi.fn(() => Promise.resolve(mode === "run"
			? makeResp(textStream(["{}"]), "application/json")
			: makeResp(textStream([dispatchResult()]), "application/json")
		)) as unknown as typeof fetch;
		const { mod } = loadApp(fetchImpl);
		mod.noteOpenSlide(SLIDE_ID);
		mod.install();
		const wfetch = (window as unknown as { fetch: typeof fetch }).fetch;
		void wfetch("/api/ai/session/s1");
		await flush();
		expect(mod.current()).toEqual(BBOX);
		mode = "run";
		void wfetch("/api/ai/run");
		await flush();
		expect(mod.current()).toBeNull();
	});

	it("leaves non-AI URLs untouched", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve(makeResp(
			textStream([dispatchResult()]), "application/json")
		)) as unknown as typeof fetch;
		const { mod } = loadApp(fetchImpl);
		mod.install();
		await (window as unknown as { fetch: typeof fetch })
			.fetch("/api/slides");
		await flush();
		expect(mod.current()).toBeNull();
	});
});
