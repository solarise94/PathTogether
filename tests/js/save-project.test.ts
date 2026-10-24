/**
 * 「保存到我的项目」（转换器页 → 工作台移交，2026-10-22）的页面级测试。
 *
 * 覆盖：
 *   - 转换器页：persist 退役（按钮/处理函数/i18n 键）；新按钮与对话框接线；
 *     身份只在点击时查；未登录 → 登录深链接（?next= 带查询串）；?save= 回跳
 *     的处理（摘参数、找任务、开对话框、缺失/被清理的显式说明）；确认时写
 *     runner intent（channel/account/target/via 与工作台 restore 的合同一致，
 *     新文件夹带 createKey 幂等键）后跳 /app?save-job=<jobId>；
 *   - 工作台：?save-job= 移交标记只对这一个 jobId 自动续传一次（其余恢复行
 *     照旧停放），标记用完即摘（resumeHandoffJob，经 HP_UPLOAD 测试面驱动）。
 * e2e（真实 WASM + 假 COS）见 tests/e2e/save-to-project.spec.ts。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const read = (rel: string) => readFileSync(resolve(root, rel), "utf8");

const toolsPageSrc = read("static/tools/tools-slides.js");
const toolsShellSrc = read("templates/tools_slides.html");
const i18nSrc = read("static/i18n.js");
const appSrc = read("static/app.js");

type W = Record<string, unknown>;

function el(tag?: string) {
	const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
	const node: Record<string, unknown> = {
		tagName: (tag || "div").toUpperCase(),
		hidden: true, textContent: "", innerHTML: "", value: "", disabled: false,
		checked: false, type: "", title: "", name: "", className: "",
		style: {}, children: [] as unknown[],
		classList: { add() { }, remove() { }, contains() { return false; } },
		parentNode: null,
		appendChild(c: unknown) { return c; },
		removeChild(c: unknown) { return c; },
		insertBefore(c: unknown) { return c; },
		replaceChildren() { },
		remove() { },
		addEventListener(_type: string, _fn: (ev?: unknown) => void) { },
		removeEventListener() { },
		setAttribute() { }, getAttribute() { return null; },
		removeAttribute() { },
		focus() { },
		click() { },
	};
	return node as Record<string, unknown>;
}

/// 与 inline-convert.test.ts 的 loadApp 同源的 app.js 装载面（window 上带
/// ?save-job= 查询串与 history 替身，供 resumeHandoffJob 驱动）。
function loadAppWith(opts: { search?: string } = {}) {
	const store = new Map<string, string>();
	const els: Record<string, ReturnType<typeof el>> = {};
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		getElementById(id: string) { if (!els[id]) els[id] = el(); return els[id]; },
		createElement(tag: string) { return el(tag); },
		addEventListener() { },
		querySelector() { return el(); },
		querySelectorAll() { return []; },
		body: el("body"),
		documentElement: el("html"),
	};
	const replaces: string[] = [];
	const search = opts.search || "";
	const w: W = {
		__PT_TEST_HOOKS: true,
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				vars && Object.keys(vars).length
					? `${k}:${Object.values(vars).join(",")}` : k,
			getLang: () => "zh",
		},
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: {} },
		fetch: (() => Promise.resolve({ ok: true, clone() { return this; },
			json: () => Promise.resolve({}) })),
		location: {
			href: "http://local/app" + search,
			pathname: "/app",
			origin: "http://local",
			search,
		},
		history: {
			replaceState(_s: unknown, _t: unknown, url: string) { replaces.push(String(url)); },
		},
		confirm: () => true,
		open: () => null,
		addEventListener: () => { },
		innerWidth: 1280,
		setTimeout: () => 0 as unknown,
		clearTimeout: () => { },
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = w.fetch;
	(globalThis as { location?: unknown }).location = w.location;
	(globalThis as { localStorage?: unknown }).localStorage = {
		getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
		setItem: (k: string, v: string) => void store.set(k, String(v)),
		removeItem: (k: string) => void store.delete(k),
	};
	new Function("window", "document", "fetch", "location",
		read("static/app.js"))(w, doc, w.fetch, w.location);
	return { w, replaces };
}

/** 把假 orchestrator 挂到 HP_UPLOAD.inlineOrch（resumeHandoffJob 的依赖）。 */
function withFakeOrch(w: W, made: Array<Record<string, unknown>>,
	acts: Array<[string, string]>) {
	(w.HP_UPLOAD as unknown as {
		inlineOrch: () => unknown;
	}).inlineOrch = () => ({
		list: () => made,
		act: (id: string, action: string) => { acts.push([id, action]); },
	});
}

describe("工作台：?save-job= 移交标记（复核 2026-10-22）", () => {
	it("只有标记命中的 upload_queued 行自动续传一次；其余恢复行保持停放；标记即摘", () => {
		const made = [
			{ id: "e1", jobId: "job-handoff", state: "upload_queued" },
			{ id: "e2", jobId: "job-other", state: "upload_queued" },   // 其他恢复行
			{ id: "e3", jobId: "job-mm", state: "account_mismatch" },   // 归属不符：绝不自动跑
		];
		const acts: Array<[string, string]> = [];
		const h = loadAppWith({ search: "?save-job=job-handoff" });
		withFakeOrch(h.w, made, acts);
		(h.w.HP_UPLOAD as unknown as {
			resumeHandoffJob(m: unknown): void;
		}).resumeHandoffJob(made);
		expect(acts).toEqual([["e1", "continue"]]);
		expect(h.replaces.length).toBe(1);
	});

	it("无标记 → 什么都不做", () => {
		const acts: Array<[string, string]> = [];
		const h = loadAppWith({ search: "" });
		withFakeOrch(h.w, [
			{ id: "e1", jobId: "j1", state: "upload_queued" },
		], acts);
		(h.w.HP_UPLOAD as unknown as {
			resumeHandoffJob(m: unknown): void;
		}).resumeHandoffJob([{ id: "e1", jobId: "j1", state: "upload_queued" }]);
		expect(acts).toEqual([]);
	});

	it("标记的 job 没有恢复出行 → 只摘标记，绝不静默上传别的行", () => {
		const acts: Array<[string, string]> = [];
		const h = loadAppWith({ search: "?save-job=gone" });
		withFakeOrch(h.w, [
			{ id: "e1", jobId: "other", state: "upload_queued" },
		], acts);
		(h.w.HP_UPLOAD as unknown as {
			resumeHandoffJob(m: unknown): void;
		}).resumeHandoffJob([{ id: "e1", jobId: "other", state: "upload_queued" }]);
		expect(acts).toEqual([]);
		expect(h.replaces.length).toBe(1);
	});
});

// ---------------------------------------------------------------- 转换器页 --

describe("转换器页：保存到我的项目（persist 退役 + 移交）", () => {
	it("persist 已退役：按钮/处理函数/i18n 键全部移除", () => {
		expect(toolsShellSrc).not.toContain("persist-btn");
		expect(toolsPageSrc).not.toContain("onPersist");
		expect(i18nSrc).not.toContain("tools.result.persist");
		expect(i18nSrc).not.toContain("tools.result.persist.granted");
		// 保存到电脑保持不变
		expect(toolsShellSrc).toContain('id="save-btn"');
		expect(toolsShellSrc).toContain('data-i18n="tools.result.save"');
	});

	it("新按钮与对话框存在；确认/取消是原生 dialog 按钮（焦点圈由 showModal 管理）", () => {
		expect(toolsShellSrc).toContain('id="save-project-btn"');
		expect(toolsShellSrc).toContain('data-i18n="tools.result.saveProject"');
		expect(toolsShellSrc).toContain('id="save-project-dialog"');
		expect(toolsShellSrc).toContain('id="save-project-dest"');
		expect(toolsShellSrc).toContain('id="save-project-new-name"');
		// 无内联样式/脚本（CSP）
		expect(toolsShellSrc).not.toMatch(/\sstyle="/);
		// 接线：按钮 → saveToProject；目标下拉切换；?save= 回跳处理
		expect(toolsPageSrc).toMatch(/saveProjectBtn\.addEventListener\('click'/);
		expect(toolsPageSrc).toContain("void saveToProject(jobId)");
		expect(toolsPageSrc).toContain("void handleSaveParam()");
		expect(toolsPageSrc).toContain("onSaveProjectDestChange()");
	});

	it("身份只在点击时查；未登录 → 登录/注册/取消对话框（复核 r2 #1）", () => {
		// 点击前没有任何页面加载期的 /api/auth/info 调用
		expect(toolsPageSrc).not.toMatch(/init\(\)[\s\S]{0,400}fetchAuthInfo/);
		expect(toolsPageSrc).toContain("async function saveToProject(jobId)");
		expect(toolsPageSrc).toMatch(
			/if \(info && info\.auth_enabled && !info\.user_id\) \{\s*\n\s*showLoginHandoff\(jobId\);/);
		// 无自动跳转计时器（复核 r2 #1：状态行 + 2.5s 自动跳转已退役）
		expect(toolsPageSrc).not.toContain("loginHandoffTimer");
		// 对话框三选接线；登录写标记后带 next 跳转；注册不带 next
		expect(toolsPageSrc).toContain("els.saveLoginForm.addEventListener('submit', onSaveLoginFormSubmit)");
		expect(toolsPageSrc).toMatch(/pendingSaveWrite\(saveLoginJobId\);[\s\S]{0,400}'\/login\?next=' \+ encodeURIComponent\('\/tools\/slides\?save=' \+ jobId\)/);
		expect(toolsPageSrc).toContain(": '/register';");
		// 任务行（有「保存」的行）也有同一出口；面板内上传关闭时不渲染
		expect(toolsPageSrc).toMatch(/primary === 'export' && inlineUploadEnabled\(\)\) \{\s*\n\s*\/\/ 「保存到我的项目」[\s\S]{0,400}saveToProject\(job\.id\)/);
		// 结果卡按钮：能力关 → 禁用 + 原因
		expect(toolsPageSrc).toMatch(/const inlineOff = !inlineUploadEnabled\(\);/);
		expect(toolsPageSrc).toMatch(/els\.saveProjectBtn\.title = t\('tools\.saveproj\.inline\.disabled'\)/);
		// 待保存标记的清除点：intent 写入成功 / 对话框取消 / 任务缺失 / 产物
		// 被清理 / ?save= 回跳
		const clears = toolsPageSrc.split("pendingSaveClear()").length - 1;
		expect(clears, "pendingSaveClear 调用点").toBeGreaterThanOrEqual(5);
	});

	it("能力关：结果卡按钮禁用、任务行不渲染保存到我的项目（复核 r2 #2）", () => {
		// 载荷字段与工作台同源（body 属性由服务端渲染）+ 渲染/禁用接线
		expect(toolsShellSrc).toMatch(
			/<body id="top" data-inline-convert-enabled="\{\{ 'true' if inline_convert_enabled else 'false' \}\}">/);
		expect(toolsPageSrc).toContain("function inlineUploadEnabled()");
		expect(toolsPageSrc).toMatch(/els\.saveProjectBtn\.disabled = !page\.readyInfo \|\| artifactGone \|\| inlineOff;/);
	});

	it("确认：写 runner intent（工作台 restore 的合同字段），然后跳 /app?save-job=", () => {
		expect(toolsPageSrc).toMatch(/await page\.runner\.setJobIntent\(saveProject\.jobId, \{/);
		expect(toolsPageSrc).toContain("channel: 'workbench-inline'");
		expect(toolsPageSrc).toContain("state: 'pending'");
		expect(toolsPageSrc).toContain("account: String(saveProject.identity.user_id || '')");
		expect(toolsPageSrc).toContain("accountLabel: String(saveProject.identity.username || '')");
		expect(toolsPageSrc).toContain("via: 'converter-save'");
		// 目标快照与工作台 importTargetSnapshot 同形（新文件夹带幂等键）
		expect(toolsPageSrc).toMatch(/pid: els\.saveProjectDest\.value === 'new' \? '' : /);
		expect(toolsPageSrc).toMatch(/createKey: \(crypto && crypto\.randomUUID\)/);
		// 移交导航
		expect(toolsPageSrc).toMatch(
			/window\.location\.href = '\/app\?save-job=' \+ encodeURIComponent\(saveProject\.jobId\)/);
	});

	it("?save= 回跳：摘参数（replaceState）→ 找任务 → 产物被清理/任务缺失显式说明", () => {
		expect(toolsPageSrc).toMatch(/handleSaveParam\(\) \{[\s\S]*?search\.match\(\/\[\?&\]save=\(\[\^&\]\+\)\/\)/);
		expect(toolsPageSrc).toMatch(/searchParams\.delete\('save'\)/);
		expect(toolsPageSrc).toContain("tools.saveproj.job.missing");
		expect(toolsPageSrc).toContain("tools.saveproj.artifact.missing");
		// 回跳时仍未登录：给说明与链接，绝不循环跳转
		expect(toolsPageSrc).toMatch(/handleSaveParam[\s\S]*?仍未登录/);
	});

	it("复核 2026-10-23：#save-project-error 不再嵌在隐藏的 #save-project-fields 里", () => {
		// 错误行的可见性错误（评审 sw5H5x）：任务缺失 / 产物被清理 /
		// /api/projects 失败都发生在 fields 仍 hidden 时——错误行必须移出
		// 该容器，否则 setSaveProjectError 只改 DOM 用户看不见。
		const fieldsAt = toolsShellSrc.indexOf('<div id="save-project-fields"');
		const fieldsBlock = toolsShellSrc.slice(
			fieldsAt, toolsShellSrc.indexOf("</div>", fieldsAt));
		expect(fieldsBlock).not.toContain('id="save-project-error"');
		// 仍在 #save-project-form 内：字段容器之后、.dialog-actions 之前
		const formAt = toolsShellSrc.indexOf(
			'<form method="dialog" class="dialog-form" id="save-project-form"');
		const formBlock = toolsShellSrc.slice(
			formAt, toolsShellSrc.indexOf("</form>", formAt));
		const fieldsEnd = formBlock.indexOf("</div>");
		const errorAt = formBlock.indexOf('id="save-project-error"');
		const actionsAt = formBlock.indexOf('<div class="dialog-actions">');
		expect(errorAt).toBeGreaterThan(fieldsEnd);
		expect(errorAt).toBeLessThan(actionsAt);
		// id/class/role/hidden 契约原样保留（仅移出层级，JS 逻辑不变）
		expect(toolsShellSrc).toContain(
			'<p id="save-project-error" class="saveproj-error" role="alert" hidden></p>');
	});

	it("i18n：tools.result.saveProject 与 tools.saveproj.* zh/en 成对", () => {
		for (const key of [
			"tools.result.saveProject",
			"tools.saveproj.title", "tools.saveproj.account", "tools.saveproj.dest",
			"tools.saveproj.unfiled", "tools.saveproj.new", "tools.saveproj.new.label",
			"tools.saveproj.new.name.ph", "tools.saveproj.confirm", "tools.saveproj.cancel",
			"tools.saveproj.checking", "tools.saveproj.loading", "tools.saveproj.handoff",
			"tools.saveproj.inline.disabled", "tools.saveproj.login.title",
			"tools.saveproj.login.body", "tools.saveproj.login.go",
			"tools.saveproj.login.register", "tools.saveproj.job.missing",
			"tools.saveproj.artifact.missing", "tools.saveproj.new.required",
			"tools.saveproj.load.failed", "tools.saveproj.projects.failed",
			"tools.saveproj.intent.failed",
		]) {
			const pattern = new RegExp(`"${key}"\\s*:`, "g");
			const hits = i18nSrc.match(pattern) || [];
			expect(hits.length, key).toBe(2);   // zh 表 + en 表各一条
		}
		for (const key of [
			"upload.saveproj.notice.text", "upload.saveproj.notice.continue",
			"upload.saveproj.notice.dismiss", "upload.saveproj.fail.prefix",
			"upload.saveproj.reason.capabilityOff", "upload.saveproj.reason.jobMissing",
			"upload.saveproj.reason.notReady", "upload.saveproj.reason.alreadyUploaded",
		]) {
			const pattern = new RegExp(`"${key}"\\s*:`, "g");
			const hits = i18nSrc.match(pattern) || [];
			expect(hits.length, key).toBe(2);   // zh 表 + en 表各一条
		}
	});
});
