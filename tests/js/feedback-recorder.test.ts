/**
 * 用户反馈客户端记录器（改版第四轮 §3）——隐私红线与环形缓冲。
 *
 * 加载真实 static/feedback-recorder.js（最小 window/document 注入），锁定：
 *   - api 事件：查询串剥除；/s/<token> → /s/***；状态码/耗时/method 在场；
 *     绝不记录请求/响应正文（响应只经克隆读 code 字段）；
 *   - 高频媒体端点（瓦片/DZI/缩略图/region/渲染输出）与非 /api/ 路径不记录；
 *   - action 事件：只记控件标识（id/data-action/data-i18n/role/tag:type），
 *     输入框内容（value）绝不落事件；
 *   - error / console.error / console.warn：前 500 字截断；console 原样透传；
 *   - 环形缓冲：>300 条丢最旧；
 *   - snapshot 形状：{captured_at, url_path, lang, viewport, user_agent,
 *     current_slide_id, events}；fetch 包装透明（原响应原样返回、失败原样抛）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const recSrc = readFileSync(resolve(here, "../../static/feedback-recorder.js"), "utf8");

interface RecEvt { t: number; kind: string; [k: string]: unknown }
interface RecAPI {
	log(kind: string, data?: Record<string, unknown>): void;
	setCurrentSlide(id: string | null): void;
	snapshot(): Record<string, unknown> & { events: RecEvt[] };
	events(): RecEvt[];
	maskPath(p: string): string;
}

type FetchStub = (input: unknown, init?: RequestInit) => Promise<Record<string, unknown>>;

interface Harness {
	HP: RecAPI;
	win: {
		fetch: FetchStub;
		console: { error: (...a: unknown[]) => void; warn: (...a: unknown[]) => void };
		[x: string]: unknown;
	};
	listeners: Record<string, Array<(e?: unknown) => void>>;
	consoleCalls: Array<{ level: string; args: unknown[] }>;
	clickCapture: (evt: unknown) => void;
	emitError: (e: unknown) => void;
	emitRejection: (e: unknown) => void;
	pushState: (u: string) => void;
}

interface FetchOutcome { status: number; json?: unknown; contentType?: string; throwErr?: unknown }

function bootRecorder(opts: {
	url?: string;
	fetchOutcome?: () => FetchOutcome;
} = {}): Harness {
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const consoleCalls: Array<{ level: string; args: unknown[] }> = [];
	const outcome = opts.fetchOutcome || (() => ({ status: 200, json: { ok: true }, contentType: "application/json" }));

	const fetchStub: FetchStub = function (input, init) {
		const r = outcome();
		if (r.throwErr) return Promise.reject(r.throwErr);
		return Promise.resolve({
			status: r.status,
			ok: r.status >= 200 && r.status < 300,
			headers: { get: (k: string) => (String(k).toLowerCase() === "content-type" ? (r.contentType || "application/json") : null) },
			clone() { return this; },
			json: () => Promise.resolve(r.json),
		});
	};

	const win: Record<string, unknown> = {
		fetch: fetchStub,
		location: new URL(opts.url || "http://local/app"),
		innerWidth: 1440,
		innerHeight: 900,
		navigator: { userAgent: "vitest-recorder" },
		HP_I18N: { getLang: () => "zh" },
		history: {
			pushState: function () {},
			replaceState: function () {},
		},
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(listeners[type] ||= []).push(cb);
		},
		console: {
			error: (...a: unknown[]) => void consoleCalls.push({ level: "error", args: a }),
			warn: (...a: unknown[]) => void consoleCalls.push({ level: "warn", args: a }),
			log: () => {},
		},
	};
	const doc = {
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(listeners[type] ||= []).push(cb);
		},
		documentElement: { lang: "zh-CN" },
	};

	// eslint-disable-next-line @typescript-eslint/no-explicit-any, no-new-func
	new Function("window", "document", recSrc)(win, doc);
	const HP = (win as { HP_FEEDBACK?: RecAPI }).HP_FEEDBACK as RecAPI;
	expect(HP).toBeTruthy();
	return {
		HP,
		win: win as Harness["win"],
		listeners,
		consoleCalls,
		clickCapture: (evt: unknown) =>
			(listeners.click || []).forEach((cb) => cb(evt)),
		emitError: (e: unknown) => (listeners.error || []).forEach((cb) => cb(e)),
		emitRejection: (e: unknown) => (listeners.unhandledrejection || []).forEach((cb) => cb(e)),
		pushState: (u: string) => {
			(win.location as URL).pathname = u;
			(listeners.popstate || []).forEach((cb) => cb({}));
		},
	};
}

function lastEvent(h: Harness, kind: string): RecEvt | undefined {
	const evts = h.HP.events().filter((e) => e.kind === kind);
	return evts[evts.length - 1];
}

async function microtasks(times = 6): Promise<void> {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

function fakeEl(spec: { tagName: string; id?: string; getAttribute: (k: string) => string | null }): unknown {
	return spec;
}

describe("HP_FEEDBACK.maskPath（路径规整纯逻辑）", () => {
	it("查询串与 hash 剥除", () => {
		const h = bootRecorder();
		expect(h.HP.maskPath("/api/slides?a=1&token=x")).toBe("/api/slides");
		expect(h.HP.maskPath("/app#section")).toBe("/app");
		expect(h.HP.maskPath("/s/tok123?v=2")).toBe("/s/***");
	});

	it("/s/<token> 记为 /s/***（分享令牌是凭证）", () => {
		const h = bootRecorder();
		expect(h.HP.maskPath("/s/AbC123")).toBe("/s/***");
		expect(h.HP.maskPath("/s/AbC123/")).toBe("/s/***/");
		expect(h.HP.maskPath("/api/x")).toBe("/api/x");
	});
});

describe("HP_FEEDBACK api 事件（fetch 包装）", () => {
	it("方法/去查询串路径/状态码/耗时；请求体与响应体绝不入事件（code 经克隆读出）", async () => {
		const h = bootRecorder({
			fetchOutcome: () => ({ status: 429, json: { error: "rate", code: "feedback_rate_limited", big: "SECRET-RESPONSE" } }),
		});
		await h.win.fetch("/api/feedback?debug=1", { method: "POST", body: "SECRET-BODY" });
		await microtasks();
		const api = lastEvent(h, "api");
		expect(api).toBeTruthy();
		expect(api!.method).toBe("POST");
		expect(api!.path).toBe("/api/feedback");
		expect(api!.status).toBe(429);
		expect(typeof api!.ms).toBe("number");
		const dump = JSON.stringify(h.HP.events());
		expect(dump).not.toContain("SECRET-BODY");
		expect(dump).not.toContain("SECRET-RESPONSE");
		const code = h.HP.events().find((e) => e.kind === "api_code");
		expect(code && code.code).toBe("feedback_rate_limited");
	});

	it("非 JSON 响应不读 code；fetch 失败原样抛出并记 status 0", async () => {
		let mode: "ok" | "fail" = "ok";
		const h = bootRecorder({
			fetchOutcome: () => mode === "fail"
				? { status: 0, throwErr: new Error("boom-net") }
				: { status: 200, json: { code: "x" }, contentType: "image/jpeg" },
		});
		await h.win.fetch("/api/slides");
		await microtasks();
		expect(h.HP.events().some((e) => e.kind === "api_code")).toBe(false);
		mode = "fail";
		await expect(h.win.fetch("/api/slides")).rejects.toThrow("boom-net");
		await microtasks();
		const api = lastEvent(h, "api");
		expect(api!.status).toBe(0);
		expect(api!.error).toBe("boom-net");
	});

	it("查询串剥除 + /s/<token> 掩码；瓦片/DZI/缩略图/静态资源/非 /api/ 不记录", async () => {
		const h = bootRecorder();
		await h.win.fetch("http://local/api/share/s/secretToken/preview?sig=abc");
		await h.win.fetch("http://local/api/slides/sld_1/tiles/0/0_0.jpg?t=123");
		await h.win.fetch("http://local/api/slides/sld_1/thumbnail?token=abc");
		await h.win.fetch("http://local/api/slides/sld_1/dzi");
		await h.win.fetch("http://local/static/app.js");
		await h.win.fetch("http://local/login?next=/app");
		await microtasks();
		const apiEvents = h.HP.events().filter((e) => e.kind === "api");
		expect(apiEvents.length).toBe(1);
		expect(apiEvents[0].path).toBe("/api/share/s/***/preview");
	});

	it("包装透明：原响应对象原样返回", async () => {
		const h = bootRecorder();
		const out = await h.win.fetch("/api/projects");
		expect(out.status).toBe(200);
	});
});

describe("HP_FEEDBACK action 事件（点击控件标识，绝不记输入内容）", () => {
	it("记录 id / data-action / data-i18n；输入框只记 tag:type，value 绝不落事件", () => {
		const h = bootRecorder();
		const input = fakeEl({
			tagName: "INPUT",
			getAttribute: (k: string) => (k === "type" ? "password" : null),
		});
		h.clickCapture({ target: input });
		let evt = lastEvent(h, "action");
		expect((evt!.control as Record<string, unknown>).tag).toBe("input:password");

		const btn = fakeEl({
			tagName: "BUTTON",
			getAttribute: (k: string) => (k === "data-action" ? "slide.open" : null),
		});
		h.clickCapture({ target: btn });
		evt = lastEvent(h, "action");
		expect(evt!.control).toMatchObject({ action: "slide.open", tag: "button" });

		const labelled = fakeEl({
			tagName: "DIV",
			getAttribute: (k: string) => (k === "data-i18n" ? "fbk.entry" : null),
		});
		h.clickCapture({ target: labelled });
		evt = lastEvent(h, "action");
		expect(evt!.control).toMatchObject({ i18n: "fbk.entry" });

		(input as unknown as { value: string }).value = "hunter2";
		h.clickCapture({ target: input });
		expect(JSON.stringify(h.HP.events())).not.toContain("hunter2");
	});
});

describe("HP_FEEDBACK error / console / nav", () => {
	it("window error 与 unhandledrejection 记录（500 字截断）", () => {
		const h = bootRecorder();
		const long = "x".repeat(900);
		h.emitError({ message: long, filename: "/static/app.js" });
		const evt = lastEvent(h, "error");
		expect(String(evt!.message).length).toBe(500);
		expect(evt!.source).toBe("/static/app.js");
		h.emitRejection({ reason: new Error("rej-msg") });
		const rej = lastEvent(h, "error");
		expect(rej!.rejection).toBe(true);
		expect(rej!.message).toBe("rej-msg");
	});

	it("console.error/warn 记录且原样透传", () => {
		const h = bootRecorder();
		h.win.console.error("boom", { a: 1 });
		h.win.console.warn("careful");
		const err = h.HP.events().filter((e) => e.kind === "console" && e.level === "error")[0];
		const warn = h.HP.events().filter((e) => e.kind === "console" && e.level === "warn")[0];
		expect(String(err.message)).toContain("boom");
		expect(warn.message).toBe("careful");
		expect(h.consoleCalls.length).toBe(2); // 原实现仍被调用
	});

	it("nav：load 时记页面路径（无查询串）；setCurrentSlide 进快照", () => {
		const h = bootRecorder({ url: "http://local/app?slide=abc" });
		const nav = h.HP.events().find((e) => e.kind === "nav");
		expect(nav!.path).toBe("/app");
		h.HP.setCurrentSlide("sld_1");
		expect(h.HP.snapshot().current_slide_id).toBe("sld_1");
	});
});

describe("HP_FEEDBACK 环形缓冲与快照", () => {
	it("超过 300 条丢最旧；事件保持 {t, kind} 形状", () => {
		const h = bootRecorder();
		for (let i = 0; i < 320; i++) h.HP.log("action", { n: i });
		const evts = h.HP.events();
		expect(evts.length).toBe(300);
		expect(evts[0].n).toBe(20);
		expect(evts[299].n).toBe(319);
		expect(typeof evts[0].t).toBe("number");
	});

	it("snapshot 含 url_path/lang/viewport/user_agent/events", () => {
		const h = bootRecorder({ url: "http://local/app" });
		h.HP.log("action", { n: 1 });
		const snap = h.HP.snapshot();
		expect(snap.url_path).toBe("/app");
		expect(snap.lang).toBe("zh");
		expect(snap.viewport).toEqual({ w: 1440, h: 900 });
		expect(snap.user_agent).toBe("vitest-recorder");
		expect(snap.events.length).toBeGreaterThanOrEqual(2);
	});
});
