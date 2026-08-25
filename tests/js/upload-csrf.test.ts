/**
 * uploadFile：裸 XHR 必须携带 X-CSRF-Token（上传修复 U1 / test-review P0-4）。
 *
 * 背景：上传是唯一带请求体却绕过 apiFetch 的写通道，曾漏传 CSRF 头导致服务端
 * 400 csrf_required（大文件传完才被拒）。本文件用 logout.test.ts 同款 loadApp
 * harness 驱动**真实** uploadFile()，断言其 XHR 请求带头、且头在 open 之后
 * send 之前设置；另覆盖 csrf_required 的可读文案映射。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

function fakeEl() {
	return {
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		style: {},
		classList: { add() {}, remove() {}, contains() { return false; } },
		appendChild() {},
		addEventListener() {},
	};
}

/** 可记录 toast 文案的容器（toast() 向 els.toastContainer append 文本节点） */
function toastContainer() {
	const messages: string[] = [];
	return {
		messages,
		appendChild(child: { textContent?: string }) {
			messages.push(String(child && child.textContent));
		},
		addEventListener() {},
	};
}

/** XMLHttpRequest stub：记录 open/setRequestHeader/send 与 load 监听 */
class FakeXHR {
	static instances: FakeXHR[] = [];
	open = vi.fn();
	setRequestHeader = vi.fn();
	send = vi.fn();
	status = 0;
	responseText = "";
	private listeners: Record<string, () => void> = {};
	upload = { addEventListener() {} };

	constructor() {
		FakeXHR.instances.push(this);
	}
	addEventListener(type: string, cb: () => void) {
		this.listeners[type] = cb;
	}
	simulateLoad(status: number, body: string) {
		this.status = status;
		this.responseText = body;
		this.listeners["load"] && this.listeners["load"]();
	}
}

function loadApp() {
	const els: Record<string, ReturnType<typeof fakeEl | typeof toastContainer>> = {};
	const toast = toastContainer();
	els["toast-container"] = toast as never;
	const loc = { href: "http://local/" };
	const fetchImpl = vi.fn(() => Promise.resolve({
		ok: true, status: 200, clone() { return this; },
		json: () => Promise.resolve({}),
	})) as unknown as typeof fetch;
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string, vars?: { e?: string }) => (vars && vars.e ? `${k}:${vars.e}` : k), getLang: () => "zh" },
		fetch: fetchImpl,
		location: loc,
		OpenSeadragon: undefined,
	};
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement() { return fakeEl(); },
		addEventListener() {},
		querySelector() { return fakeEl(); },
		querySelectorAll() { return []; },
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	(globalThis as { location: typeof loc }).location = loc;
	vi.stubGlobal("XMLHttpRequest", FakeXHR);
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, loc);
	return {
		uploadFile: (w.HP_UPLOAD as { uploadFile: (f: unknown) => void }).uploadFile,
		toastMessages: toast.messages,
	};
}

describe("app.js uploadFile 必须带 X-CSRF-Token", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		FakeXHR.instances = [];
	});

	it("XHR 携带双提交头（open 后、send 前）", () => {
		const h = loadApp();
		expect(typeof h.uploadFile).toBe("function");
		h.uploadFile({ name: "a.svs", size: 3 });
		expect(FakeXHR.instances).toHaveLength(1);
		const xhr = FakeXHR.instances[0];
		expect(xhr.open).toHaveBeenCalledWith("POST", "/api/upload");
		expect(xhr.setRequestHeader).toHaveBeenCalledWith("X-CSRF-Token", "tok");
		expect(xhr.send).toHaveBeenCalledTimes(1);
		// 顺序：open → setRequestHeader → send（头必须在 open 之后设置才生效）
		const [openAt] = xhr.open.mock.invocationCallOrder;
		const [hdrAt] = xhr.setRequestHeader.mock.invocationCallOrder;
		const [sendAt] = xhr.send.mock.invocationCallOrder;
		expect(openAt).toBeLessThan(hdrAt);
		expect(hdrAt).toBeLessThan(sendAt);
	});

	it("csrf_required 映射为可读文案（不透出原始错误码）", () => {
		const h = loadApp();
		h.uploadFile({ name: "a.svs", size: 3 });
		const xhr = FakeXHR.instances[0];
		xhr.simulateLoad(400, JSON.stringify({ error: "csrf_required" }));
		expect(h.toastMessages.length).toBeGreaterThan(0);
		const msg = h.toastMessages[h.toastMessages.length - 1];
		expect(msg).toContain("upload.fail");
		expect(msg).toContain("刷新");
		expect(msg).not.toContain("csrf_required");
	});
});
