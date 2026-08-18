/**
 * doLogout：POST 失败不得跳转登录页（服务端 session 可能仍有效）。
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

function loadApp(fetchImpl: typeof fetch) {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const loc = { href: "http://local/" };
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
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, loc);
	return {
		doLogout: (w.HP_AUTH as { doLogout: () => void }).doLogout,
		location: loc,
		toastContainer: els["toast-container"],
	};
}

describe("app.js doLogout 失败不得跳登录页", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("fetch reject 后 location 不变", async () => {
		const fetchImpl = vi.fn(() => Promise.reject(new Error("offline"))) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		expect(typeof h.doLogout).toBe("function");
		h.doLogout();
		await Promise.resolve();
		await Promise.resolve();
		expect(h.location.href).toBe("http://local/");
	});

	it("HTTP 非 2xx 后 location 不变", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve({
			ok: false,
			status: 500,
			clone() { return this; },
			json: () => Promise.resolve({ error: "boom" }),
		})) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.doLogout();
		await Promise.resolve();
		await Promise.resolve();
		expect(h.location.href).toBe("http://local/");
	});

	it("POST 成功才跳 /login", async () => {
		const fetchImpl = vi.fn(() => Promise.resolve({
			ok: true,
			status: 302,
			clone() { return this; },
			json: () => Promise.resolve({}),
		})) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.doLogout();
		await Promise.resolve();
		await Promise.resolve();
		expect(h.location.href).toBe("/login");
	});
});
