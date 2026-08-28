/**
 * 管理员身份预览：/api/auth/info 的 effective subject 驱动模块可见性，
 * 预览 banner 展示并可用 stop。
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

function loadApp() {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const loc = { href: "http://local/", pathname: "/" };
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) => {
				if (!vars) return k;
				return k + ":" + JSON.stringify(vars);
			},
			getLang: () => "zh",
			setRole() {},
		},
		fetch: vi.fn(() => Promise.resolve({
			ok: true,
			status: 200,
			clone() { return this; },
			json: () => Promise.resolve({ users: [] }),
		})),
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
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, w.fetch, loc);
	return {
		els,
		applyAuthInfo: (w.HP_AUTH as { applyAuthInfo: (info: unknown) => void }).applyAuthInfo,
	};
}

describe("admin identity preview UI", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("preview 态展示 banner，effective role 不是 owner", () => {
		const h = loadApp();
		h.applyAuthInfo({
			auth_enabled: true,
			username: "user@x.com",
			role: "user",
			user_id: "usr_b",
			actor: { username: "owner@x.com", role: "owner", user_id: "usr_a" },
			preview: {
				subject_user_id: "usr_b",
				subject_role: "user",
				subject_username: "user@x.com",
				expires_at: Date.now() / 1000 + 900,
				actor_user_id: "usr_a",
			},
		});
		expect(h.els["preview-banner"].hidden).toBe(false);
		expect(h.els["preview-banner-text"].textContent).toContain("user@x.com");
		expect(h.els["logout-btn"].hidden).toBe(true);
	});

	it("非预览 owner 隐藏 banner（用户管理 UI 已迁入 admin 插件，PR5）", () => {
		const h = loadApp();
		h.applyAuthInfo({
			auth_enabled: true,
			username: "owner@x.com",
			role: "owner",
			user_id: "usr_a",
			actor: { username: "owner@x.com", role: "owner", user_id: "usr_a" },
			preview: null,
		});
		expect(h.els["preview-banner"].hidden).toBe(true);
	});
});
