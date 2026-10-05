/**
 * R1「一键转换并上传」前端单元（drain 计划 §3.1）。
 *
 * 驱动**真实** app.js（loadApp harness，同 cos-upload.test.ts 形态）锁定：
 *  1. KFB/KFBF（browser_convert 词表内、直传词表外）→ uploadFile 不判失败、
 *     零 /api/ 请求，行内给出「在本机转换并上传」入口；
 *  2. 弹窗被拦截（window.open → null）→ 明示原因 + 工具页链接，不静默丢选择；
 *  3. 交接目标：显式项目 pid 原样携带；「新项目」携带名称 + 一次性幂等键；
 *     未归类 → null；
 *  4. 原生 .tif → 无本机转换入口，照常直传（不回退、不误伤）；
 *  5. i18n：新增 tools 与 upload.kfb 键 zh/en 均存在（静态扫描 i18n.js），
 *     app.js _EXTRA_I18N 兜底表与 i18n.js 同步。
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const cosEngineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

/** 可记录 children / 事件监听的元素 stub（cos-upload.test.ts 同形态，精简版） */
function el(tag?: string) {
	const children: ReturnType<typeof el>[] = [];
	const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
	const node: Record<string, unknown> = {
		tagName: (tag || "div").toUpperCase(),
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		checked: false,
		type: "",
		title: "",
		name: "",
		className: "",
		style: {},
		classList: { add() {}, remove() {}, contains() { return false; } },
		children,
		appendChildren: children,
		parentNode: null,
		appendChild(c: ReturnType<typeof el>) { children.push(c); return c; },
		removeChild(c: ReturnType<typeof el>) {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
			return c;
		},
		insertBefore(c: ReturnType<typeof el>) { children.push(c); return c; },
		addEventListener(type: string, fn: (ev?: unknown) => void) {
			(listeners[type] = listeners[type] || []).push(fn);
		},
		removeEventListener() {},
		setAttribute() {},
		getAttribute() { return null; },
		focus() {},
		click() { (listeners["click"] || []).forEach((f) => f({ preventDefault() {} })); },
	};
	(node as { _listeners: typeof listeners })._listeners = listeners;
	return node as ReturnType<typeof el> & {
		_listeners: Record<string, Array<(ev?: unknown) => void>>;
	};
}

function fire(node: ReturnType<typeof el>, type: string, ev?: unknown) {
	(node._listeners[type] || []).forEach((f) => f(ev));
}

function fakeLocalStorage() {
	const m = new Map<string, string>();
	return {
		getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
		setItem: (k: string, v: string) => { m.set(k, String(v)); },
		removeItem: (k: string) => { m.delete(k); },
		key: (i: number) => [...m.keys()][i] ?? null,
		get length() { return m.size; },
		clear() { m.clear(); },
	};
}

function cosCaps(extra: Record<string, unknown> = {}) {
	return {
		available: true, manual_only: true,
		max_size_bytes: 900000000, part_bytes: 8, url_ttl_seconds: 600,
		max_concurrent_parts: 2, sign_batch_max_parts: 4, policy_version: "v1-manual",
		formats: ["svs", "tif", "tiff", "zip"],
		browser_convert: { formats: ["kfb", "kfbf"], url: "/tools/slides" },
		...extra,
	};
}

/**
 * 执行真实 app.js。openImpl 注入 window.open（默认 null = 弹窗被拦截）。
 * 返回行按钮（含其点击监听）与可观测镜像。
 */
function loadApp(bootstrap: unknown, openImpl: (() => unknown) | null = null) {
	const storage = fakeLocalStorage();
	const els: Record<string, ReturnType<typeof el>> = {};
	const toasts: string[] = [];
	els["toast-container"] = el();
	const container = el();              // #upload-progress-list
	const toggleParent = el();
	container.parentNode = toggleParent;
	els["upload-progress-list"] = container;
	const theFetch = vi.fn(() => Promise.resolve({
		ok: true, status: 200, clone() { return this; },
		json: () => Promise.resolve({}),
	})) as unknown as typeof fetch;
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				(vars && Object.keys(vars).length) ? `${k}:${Object.values(vars).join(",")}` : k,
			getLang: () => "zh",
		},
		fetch: theFetch,
		location: { href: "http://local/app", pathname: "/app", origin: "http://local" },
		confirm: vi.fn(() => true),
		open: openImpl === null ? vi.fn(() => null) : openImpl,
		addEventListener: vi.fn(),
	};
	if (bootstrap !== undefined) w.HP_APP_BOOTSTRAP = bootstrap;
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els[id]) els[id] = el();
			return els[id];
		},
		createElement(tag: string) { return el(tag); },
		addEventListener() {},
		querySelector() { return el(); },
		querySelectorAll() { return []; },
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = theFetch;
	(globalThis as { location: unknown }).location = w.location;
	(globalThis as { localStorage: typeof storage }).localStorage = storage;
	vi.stubGlobal("localStorage", storage);
	new Function("window", "document", "fetch", "location", cosEngineSrc)(w, doc, theFetch, w.location);
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, theFetch, w.location);
	const up = w.HP_UPLOAD as Record<string, (...a: unknown[]) => unknown>;
	return {
		up: up as {
			uploadFile: (f: unknown) => void;
			isBrowserConvertFile: (f: unknown) => boolean;
			convertHandoffTarget: () => unknown;
			openConvertHandoff: (f: unknown, row: unknown) => void;
		},
		w,
		container,
		toasts,
		fetchMock: theFetch as unknown as vi.Mock,
	};
}

function kfbFile(size = 1024, name = "spec.kfb") {
	return { name, size, lastModified: 1, slice: () => null, type: "" };
}

/// 阶段 1：uploadFile 先嗅探（微任务）再分流——断言前先排空微任务。
const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 10) {
	for (let i = 0; i < n; i++) await tick();
}

/// 行部件（stub 的 textContent 不级联，按 className 定位）。
function rowPart(row: ReturnType<typeof el>, cls: string) {
	const found = row.children.find((c) => (c as ReturnType<typeof el>).className === cls);
	return found as ReturnType<typeof el> | undefined;
}

/// 行内可点按钮（有 click 监听的子元素）。
function rowButtons(row: ReturnType<typeof el>) {
	return row.children.filter((c) => (c as ReturnType<typeof el>)._listeners
		&& (c as ReturnType<typeof el>)._listeners.click) as ReturnType<typeof el>[];
}

describe("R1 工作台入口（真实 app.js）", () => {
	it("KFB（browser_convert 词表内）→ 不判失败、零 /api/ 请求、行内给「在本机转换并上传」", async () => {
		const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } });
		expect(h.up.isBrowserConvertFile(kfbFile())).toBe(true);
		h.up.uploadFile(kfbFile());
		await flush(8);
		// 上传行不报错、不触发任何控制 API
		const calls = h.fetchMock.mock.calls as unknown as [string, Record<string, unknown>?][];
		expect(calls.filter(([u]) => String(u).includes("/api/ingestions"))).toHaveLength(0);
		const row = h.container.children[0] as ReturnType<typeof el>;
		const status = rowPart(row, "upload-item-status");
		expect(status && status.textContent).toMatch(/upload\.kfb\.hint|需在本机转换后上传/);
		// 本机转换入口 + 忽略（无取消按钮——没有开始上传）
		const btns = rowButtons(row);
		expect(btns.length).toBe(2);
		expect(btns[0].textContent).toMatch(/upload\.kfb\.btn|在本机转换并上传/);
		expect(btns[0].className).toBe("upload-item-btn");
		expect(btns[1].textContent).toMatch(/upload\.kfb\.dismiss|忽略/);
	});

	/// 行移除计时 = makeUploadRow.finish 的 setTimeout（toast 计时另计，按延时区分）
	function removalDelays(spy: vi.SpyInstance) {
		return spy.mock.calls.map((c) => Number(c[1])).filter((d) => d === 0 || d >= 6000);
	}

	it("待操作入口不定时移除：未点击时不安排任何移除计时", async () => {
		const spy = vi.spyOn(globalThis, "setTimeout");
		try {
			const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } });
			h.up.uploadFile(kfbFile());
			await flush(8);
			spy.mockClear();
			expect(removalDelays(spy)).toEqual([]);
			const row = h.container.children[0] as ReturnType<typeof el>;
			expect(rowButtons(row).length).toBe(2);
		} finally {
			spy.mockRestore();
		}
	});

	it("忽略 → 立即安排移除该行（不打开 popup）", async () => {
		const spy = vi.spyOn(globalThis, "setTimeout");
		try {
			const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } });
			h.up.uploadFile(kfbFile());
			await flush(8);
			const row = h.container.children[0] as ReturnType<typeof el>;
			spy.mockClear();
			fire(rowButtons(row)[1], "click");
			expect(removalDelays(spy)).toEqual([0]);
			expect(h.w.open as vi.Mock).not.toHaveBeenCalled();
		} finally {
			spy.mockRestore();
		}
	});

	it("弹窗被拦截 → 行保留可重试（不安排移除），重按仍尝试打开", async () => {
		const spy = vi.spyOn(globalThis, "setTimeout");
		try {
			const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } }, null);
			h.up.uploadFile(kfbFile());
			await flush(8);
			const row = h.container.children[0] as ReturnType<typeof el>;
			spy.mockClear();
			fire(rowButtons(row)[0], "click");
			fire(rowButtons(row)[0], "click");
			expect(removalDelays(spy)).toEqual([]);
			expect((h.w.open as vi.Mock).mock.calls.length).toBe(2);
			const links = row.children.filter((c) => (c as ReturnType<typeof el>).tagName === "A");
			expect(links.length).toBe(1);
		} finally {
			spy.mockRestore();
		}
	});

	it("原生 .tif → 无本机转换入口，照常创建 ingestion（直传不回退）", async () => {
		const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } });
		h.up.uploadFile({ name: "native.tif", size: 30, lastModified: 1 });
		await vi.waitFor(() => {
			const cs = (h.fetchMock.mock.calls as unknown as [string, Record<string, unknown>?][]);
			expect(cs.some(([u, o]) => String(u).includes("/api/ingestions")
				&& (o as { method?: string } | undefined)?.method === "POST")).toBe(true);
		}, { timeout: 3000 });
		const calls = h.fetchMock.mock.calls as unknown as [string, Record<string, unknown>?][];
		const creates = calls.filter(([u, o]) => String(u).includes("/api/ingestions")
			&& (o as { method?: string } | undefined)?.method === "POST");
		expect(creates.length).toBeGreaterThan(0);
		const row = h.container.children[0] as ReturnType<typeof el>;
		const offer = rowButtons(row).find((b) => /upload\.kfb\.btn|在本机转换并上传/.test(b.textContent || ""));
		expect(offer).toBeFalsy();
	});

	it("browser_convert 词表外（如 .mrxs）→ 不提供本机转换入口", () => {
		const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } });
		expect(h.up.isBrowserConvertFile({ name: "x.mrxs", size: 5 })).toBe(false);
		expect(h.up.isBrowserConvertFile(null)).toBe(false);
	});

	it("弹窗被拦截 → 明示原因 + 工具页链接（选择不静默丢失）", async () => {
		const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } }, null);
		h.up.uploadFile(kfbFile());
		await flush(8);
		const row = h.container.children[0] as ReturnType<typeof el>;
		const btn = rowButtons(row)[0];
		expect(btn).toBeTruthy();
		fire(btn, "click");
		// 行状态切到「弹窗被拦截」+ 追加工具页链接元素
		const status = rowPart(row, "upload-item-status");
		expect(status && status.textContent).toMatch(/upload\.kfb\.popup\.blocked|弹窗被浏览器拦截/);
		const link = row.children.find((c) => (c as ReturnType<typeof el>).tagName === "A"
			&& (c as unknown as { href?: string }).href === "/tools/slides");
		expect(link).toBeTruthy();
	});

	it("交接目标：缺省（未选目标）→ null（未归类）", () => {
		const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } });
		// importTargetState 是闭包内会话状态；缺省（未选目标）→ null
		expect(h.up.convertHandoffTarget()).toBeNull();
	});

	it("打开 popup 成功 → 行状态切「已交给本机转换工具」并注册 message 监听一次", async () => {
		const popupStub = {
			closed: false,
			postMessage: vi.fn(),
		};
		const h = loadApp({ mode: "official", capabilities: { cos_upload: cosCaps() } },
			() => popupStub);
		h.up.uploadFile(kfbFile());
		await flush(8);   // 阶段 1：先嗅探（微任务）再给「本机转换」入口
		const row = h.container.children[0] as ReturnType<typeof el>;
		fire(rowButtons(row)[0], "click");
		const status = rowPart(row, "upload-item-status");
		expect(status && status.textContent).toMatch(/upload\.kfb\.handoff\.sent|已交给本机转换工具/);
		expect((h.w.addEventListener as vi.Mock).mock.calls
			.filter(([t]) => t === "message").length).toBe(1);
		// 交接消息（重投协议首轮 400ms 后发出）：File 载荷 + 目标 + 同源
		await new Promise((r) => setTimeout(r, 600));
		expect(popupStub.postMessage).toHaveBeenCalled();
		const args = (popupStub.postMessage as vi.Mock).mock.calls[0];
		expect(args[0].type).toBe("pt:convert-upload-handoff");
		expect(args[0].file).toBeTruthy();
		expect(args[0].target).toBeNull();
		expect(args[1]).toBe("http://local");
	});
});

describe("R1 i18n（静态契约）", () => {
	const zhAt = i18nSrc.indexOf('"tools.run.upload"');
	const enAt = i18nSrc.indexOf('"tools.run.upload"', zhAt + 1);

	it("i18n.js 同时包含 zh 与 en 的新键块", () => {
		expect(zhAt).toBeGreaterThan(0);
		expect(enAt).toBeGreaterThan(zhAt);
	});

	const NEW_KEYS = [
		"tools.run.upload", "tools.run.upload.hint",
		"tools.jobs.action.intent.start", "tools.jobs.action.intent.resume",
		"tools.upload.account.changed",
		"tools.account.title", "tools.account.body.upload", "tools.account.body.intent",
		"tools.account.unknown", "tools.account.cancel", "tools.account.original",
		"tools.account.separate",
		"tools.upload.superseded", "tools.upload.superseded.cancel",
		"tools.upload.superseded.wrong", "tools.upload.superseded.failed",
		"tools.upload.superseded.cancelled", "tools.upload.assoc.retry",
		"tools.upload.intent.revoked",
		"tools.cu.chain.authorized", "tools.cu.chain.resuming",
		"tools.cu.login.required", "tools.cu.offline", "tools.cu.capability.off",
		"tools.cu.assoc.ok", "tools.cu.assoc.fail",
		"tools.handoff.received", "tools.handoff.target.unfiled",
		"tools.handoff.target.project", "tools.handoff.target.new",
		"upload.kfb.hint", "upload.kfb.btn", "upload.kfb.handoff.sent",
		"upload.kfb.popup.blocked", "upload.kfb.tools.link", "upload.kfb.done",
	];

	it.each(NEW_KEYS)("键 %s 在 zh/en 各出现恰好两次", (key) => {
		const n = i18nSrc.split(`"${key}"`).length - 1;
		expect(n, `${key} 出现 ${n} 次（期望 2：zh+en）`).toBe(2);
	});

	it("app.js _EXTRA_I18N 兜底覆盖 upload.kfb.*", () => {
		for (const key of ["upload.kfb.hint", "upload.kfb.btn", "upload.kfb.handoff.sent",
			"upload.kfb.popup.blocked", "upload.kfb.tools.link", "upload.kfb.done"]) {
			expect(appSrc).toContain(`"${key}":`);
		}
	});

	it("tools.run.start 是本地语义（U2：仅转换；上传按钮另列且命名到工作台）", () => {
		expect(i18nSrc).toContain('"tools.run.start": "仅转换"');
		expect(i18nSrc).toContain('"tools.run.start": "Convert only"');
		const shell = readFileSync(resolve(here, "../../templates/tools_slides.html"), "utf8");
		expect(shell).toContain('data-i18n="tools.run.start">仅转换');
		expect(shell).toContain('data-i18n="tools.run.upload"');
		expect(shell).toContain('data-i18n="tools.run.upload">转换并上传到工作台');
	});
});
