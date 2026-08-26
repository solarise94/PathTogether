/**
 * apiFetch 401 → /login?next=... 跳转契约（test-review P3-18-1）。
 *
 * 与 logout.test.ts 同款 loadApp harness：app.js 以函数体加载，注入
 * window/document/fetch/location。覆盖：
 *   - 401 + body {error:"auth_required"} → location 跳 /login?next=<当前路径>
 *   - 401 + 其它 body → 原样返回 Response，不跳转
 *   - 非 401 → 原样返回
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

function loadApp(fetchImpl: typeof fetch, pathname = "/slides/a.svs") {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const loc = { href: "http://local" + pathname, pathname };
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
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
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, loc);
	return {
		apiFetch: (w.HP_AUTH as { apiFetch: (u: string, o?: object) => Promise<Response> }).apiFetch,
		location: loc,
	};
}

function resp(status: number, body: unknown) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
	} as unknown as Response;
}

describe("app.js apiFetch 401 处理", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("401 + auth_required → 跳 /login?next=<当前路径>", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve(resp(401, { error: "auth_required" }))) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		expect(typeof h.apiFetch).toBe("function");
		const r = await h.apiFetch("/api/annotations");
		expect(r.status).toBe(401);
		// 跳转发生在 json() 解析之后
		await Promise.resolve();
		await Promise.resolve();
		expect(h.location.href).toBe("/login?next=" + encodeURIComponent("/slides/a.svs"));
	});

	it("401 但 body 非 auth_required → 不跳转，原样返回", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve(resp(401, { error: "other" }))) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		const r = await h.apiFetch("/api/annotations");
		expect(r.status).toBe(401);
		await Promise.resolve();
		await Promise.resolve();
		expect(h.location.href).toBe("http://local/slides/a.svs");
	});

	it("非 401 → 原样返回，不跳转", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve(resp(200, { ok: true }))) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		const r = await h.apiFetch("/api/auth/info");
		expect(r.status).toBe(200);
		await Promise.resolve();
		expect(h.location.href).toBe("http://local/slides/a.svs");
	});
});
