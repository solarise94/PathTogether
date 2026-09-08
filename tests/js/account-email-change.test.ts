/**
 * 更换邮箱前端入口闭环（review-2026-09-08 P2-2；P1-3 身份收口）。
 *
 * 与 logout.test.ts / upload-csrf.test.ts 同款 loadApp harness：app.js 以函数体
 * 加载，注入 window/document/fetch/location。被测挂载点 window.HP_AUTH 的
 * applyAuthInfo（入口可见性）与 changeemailSubmit（提交流程）。覆盖：
 *   - 成功 200：POST /api/account/email/change/start 携 X-CSRF-Token 双提交头 +
 *     JSON body {new_email}（trim 后）；toast 展示服务端掩码邮箱；弹窗关闭
 *   - 提交期间按钮禁用（防重复），结束后恢复
 *   - 409 email_taken / 429 rate_limited / 503 email_channel_unavailable /
 *     400 invalid_request → 按服务端 code 映射可读文案（机器码不原样透出）
 *   - 空输入不发请求；网络失败走兜底文案
 *   - 预览态隐藏入口（与改密/登出同级约定）；正常登录态显示；认证关闭不出现
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

// 与 static/i18n.js zh 表同口径的最小字典（断言落在我们自己的文案上，而非 key）
const I18N_ZH: Record<string, string> = {
	"toast.logout": "退出登录",
	"preview.banner": "正在预览 {user}（{role}）的身份，只读 · 剩余约 {mins} 分钟",
	"acct.changeemail.ok": "确认邮件已发送至 {email}，请查收邮件完成改绑",
	"acct.changeemail.err.required": "请输入新的邮箱地址",
	"acct.changeemail.err.invalid": "请输入有效的邮箱地址",
	"acct.changeemail.err.taken": "该邮箱已被占用，无法改绑",
	"acct.changeemail.err.locked": "尝试过于频繁，请稍后再试",
	"acct.changeemail.err.channel": "邮件通道未配置，暂时无法发起改绑",
	"acct.changeemail.err.generic": "改绑失败，请稍后重试",
};

function makeI18N() {
	return {
		t: (key: string, vars?: Record<string, unknown>) => {
			const s = I18N_ZH[key] != null ? I18N_ZH[key] : key;
			if (!vars) return s;
			return s.replace(/\{(\w+)\}/g, (_m, k: string) =>
				vars[k] != null ? String(vars[k]) : `{${k}}`,
			);
		},
		getLang: () => "zh",
	};
}

function fakeEl() {
	return {
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		style: {} as Record<string, string>,
		focus() {},
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

function loadApp(fetchImpl: typeof fetch) {
	const els: Record<string, ReturnType<typeof fakeEl> | ReturnType<typeof toastContainer>> = {};
	els["toast-container"] = toastContainer() as never;
	const loc = { href: "http://local/" };
	const i18n = makeI18N();
	const w: Record<string, unknown> = {
		HP_I18N: i18n,
		fetch: fetchImpl,
		location: loc,
		OpenSeadragon: undefined,
	};
	const doc = {
		readyState: "loading", // init 延迟到 DOMContentLoaded（harness 不触发）
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
	const auth = w.HP_AUTH as {
		changeemailSubmit: () => void;
		applyAuthInfo: (info: unknown) => unknown;
	};
	return {
		changeemailSubmit: auth.changeemailSubmit,
		applyAuthInfo: auth.applyAuthInfo,
		fetchImpl: fetchImpl as unknown as vi.Mock,
		els: els as Record<string, ReturnType<typeof fakeEl>>,
		toastMessages: (els["toast-container"] as ReturnType<typeof toastContainer>).messages,
	};
}

function jsonResp(status: number, body: unknown) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
	} as unknown as Response;
}

async function flush() {
	await new Promise<void>((r) => { setTimeout(r, 0); });
}

function errText(h: ReturnType<typeof loadApp>) {
	return h.els["changeemail-error"].textContent;
}

describe("changeemailSubmit：成功契约（CSRF 头 + JSON body + 掩码邮箱回显）", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("POST /api/account/email/change/start 携 X-CSRF-Token；toast 含掩码邮箱；弹窗关闭", async () => {
		const fetchImpl = vi.fn(() =>
			Promise.resolve(jsonResp(200, { ok: true, email_masked: "new***@example.com" })),
		) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		expect(typeof h.changeemailSubmit).toBe("function");
		// 输入带首尾空白：提交前必须 trim（服务端同样规范化，但这里不发脏数据）
		h.els["changeemail-new"].value = "  new@example.com ";
		h.els["changeemail-mask"].style.display = ""; // 模拟弹窗已打开
		h.changeemailSubmit();

		expect(h.fetchImpl).toHaveBeenCalledTimes(1);
		const [url, opts] = h.fetchImpl.mock.calls[0] as [string, RequestInit];
		expect(url).toBe("/api/account/email/change/start");
		expect((opts.method as string).toUpperCase()).toBe("POST");
		const headers = opts.headers as Record<string, string>;
		// 双提交 CSRF：cookie csrf_token=tok → X-CSRF-Token 头
		expect(headers["X-CSRF-Token"]).toBe("tok");
		expect(headers["Content-Type"]).toBe("application/json");
		expect(JSON.parse(String(opts.body))).toEqual({ new_email: "new@example.com" });

		await flush();
		const toast = h.toastMessages[h.toastMessages.length - 1] || "";
		// 成功提示展示服务端掩码邮箱（绝不明文/绝不出现 token）
		expect(toast).toContain("new***@example.com");
		expect(toast).toContain("确认邮件已发送");
		expect(h.els["changeemail-mask"].style.display).toBe("none");
		expect(h.els["changeemail-submit"].disabled).toBe(false);
	});

	it("提交期间按钮禁用（防重复提交），结束后恢复", async () => {
		let resolveFetch!: (r: Response) => void;
		const fetchImpl = vi.fn(() =>
			new Promise<Response>((res) => { resolveFetch = res; }),
		) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.els["changeemail-new"].value = "new@example.com";
		h.changeemailSubmit();
		expect(h.fetchImpl).toHaveBeenCalledTimes(1);
		expect(h.els["changeemail-submit"].disabled).toBe(true);
		// 在途时再次触发（双击）：入口仍处于同一请求，不重复发
		h.changeemailSubmit();
		expect(h.fetchImpl).toHaveBeenCalledTimes(1);
		resolveFetch(jsonResp(400, { error: "x", code: "invalid_request" }));
		await flush();
		expect(h.els["changeemail-submit"].disabled).toBe(false);
	});
});

describe("changeemailSubmit：错误码映射（机器码不透出）", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	async function submitAndGetError(status: number, body: unknown) {
		const fetchImpl = vi.fn(() => Promise.resolve(jsonResp(status, body))) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.els["changeemail-new"].value = "new@example.com";
		h.changeemailSubmit();
		await flush();
		return h;
	}

	it("409 email_taken → 邮箱被占文案", async () => {
		const h = await submitAndGetError(409, { error: "该邮箱已被占用，无法改绑", code: "email_taken" });
		expect(errText(h)).toContain("已被占用");
		expect(errText(h)).not.toContain("email_taken");
		expect(h.els["changeemail-error"].hidden).toBe(false);
	});

	it("429 rate_limited → 频率限制文案", async () => {
		const h = await submitAndGetError(429, { error: "尝试过于频繁，请稍后再试", code: "rate_limited" });
		expect(errText(h)).toContain("频繁");
		expect(errText(h)).not.toContain("rate_limited");
	});

	it("503 email_channel_unavailable → 邮件通道文案", async () => {
		const h = await submitAndGetError(503, { error: "邮件通道未配置，暂时无法发起改绑", code: "email_channel_unavailable" });
		expect(errText(h)).toContain("邮件通道");
		expect(errText(h)).not.toContain("email_channel_unavailable");
	});

	it("400 invalid_request → 邮箱格式文案", async () => {
		const h = await submitAndGetError(400, { error: "请输入有效的邮箱地址", code: "invalid_request" });
		expect(errText(h)).toContain("有效的邮箱地址");
		expect(errText(h)).not.toContain("invalid_request");
	});

	it("未知错误码 → 兜底文案（不展示原始错误）", async () => {
		const h = await submitAndGetError(500, { error: "boom", code: "weird_code" });
		expect(errText(h)).toBe("改绑失败，请稍后重试");
	});

	it("网络失败 → 兜底文案，按钮恢复", async () => {
		const fetchImpl = vi.fn(() => Promise.reject(new Error("offline"))) as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.els["changeemail-new"].value = "new@example.com";
		h.changeemailSubmit();
		await flush();
		expect(errText(h)).toBe("改绑失败，请稍后重试");
		expect(h.els["changeemail-submit"].disabled).toBe(false);
	});

	it("空输入：不发请求，前端必填校验", () => {
		const fetchImpl = vi.fn() as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.els["changeemail-new"].value = "   ";
		h.changeemailSubmit();
		expect(h.fetchImpl).not.toHaveBeenCalled();
		expect(errText(h)).toContain("请输入新的邮箱地址");
	});
});

describe("更换邮箱入口可见性（预览态/未登录态不得出现）", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("正常登录态显示；预览态隐藏（与改密/登出同级）", () => {
		const fetchImpl = vi.fn() as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.applyAuthInfo({
			auth_enabled: true,
			role: "user",
			user_id: "u1",
			username: "u1@example.com",
			preview: null,
		});
		expect(h.els["changeemail-btn"].hidden).toBe(false);
		h.applyAuthInfo({
			auth_enabled: true,
			role: "user",
			user_id: "u1",
			username: "u1@example.com",
			preview: {
				subject_user_id: "u1",
				subject_username: "u1@example.com",
				subject_role: "user",
				expires_at: Math.floor(Date.now() / 1000) + 300,
			},
		});
		expect(h.els["changeemail-btn"].hidden).toBe(true);
		expect(h.els["changepw-btn"].hidden).toBe(true);
		expect(h.els["logout-btn"].hidden).toBe(true);
	});

	it("认证关闭（未登录态）：applyAuthInfo 提前返回，入口保持 hidden", () => {
		const fetchImpl = vi.fn() as unknown as typeof fetch;
		const h = loadApp(fetchImpl);
		h.applyAuthInfo({ auth_enabled: false });
		expect(h.els["changeemail-btn"].hidden).toBe(true);
	});
});
