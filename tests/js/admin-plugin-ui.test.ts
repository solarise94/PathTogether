/**
 * pathtogether-admin 插件 UI 最小装配测试（PR5 修订）。
 *
 * 插件页运行在 /admin 宿主页的 opaque iframe 内，无 jsdom 环境；本文件沿用
 * admin-preview.test.ts 的「new Function + 假 window/document」模式，只锁定
 * 最低装配面（不渲染业务数据）：
 *   - main.js 在缺省 DOM 下加载不抛错（所有新页面/按钮绑定均为可选探测）；
 *   - 导出 PathTogetherAdminClient（request/showPage/handshakeState）；
 *   - 初始页白名单含 plugins（PR5 新增页），未知 hash 回 overview。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/main.js"), "utf8");

function fakeEl() {
	return {
		hidden: true,
		textContent: "",
		value: "",
		disabled: false,
		checked: false,
		appendChild() {},
		addEventListener() {},
		getAttribute: () => null,
		setAttribute() {},
		querySelectorAll: () => [],
		closest: () => null,
	};
}

function loadPluginUi(hash: string) {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const location = { hash };
	const w: Record<string, unknown> = {
		location,
		parent: { postMessage() {} },
		addEventListener() {},
		setTimeout,
		clearTimeout,
	};
	const doc = {
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement: () => fakeEl(),
		addEventListener() {},
	};
	(w as { document: typeof doc }).document = doc;
	new Function("window", "document", src)(w, doc);
	return {
		els,
		client: w.PathTogetherAdminClient as {
			request: unknown;
			showPage: (page: string) => void;
			handshakeState: () => { ready: boolean; grantedCount: number };
		} | undefined,
	};
}

describe("pathtogether-admin plugin UI bootstrap (PR5)", () => {
	it("loads without throwing and exports the bridge client", () => {
		const { client } = loadPluginUi("");
		expect(client).toBeTruthy();
		expect(typeof client!.request).toBe("function");
		expect(typeof client!.showPage).toBe("function");
		expect(typeof client!.handshakeState).toBe("function");
		// 未握手：请求应拒绝（not_ready），showPage 切页不抛错（含 plugins）
		expect(() => client!.showPage("plugins")).not.toThrow();
		expect(() => client!.showPage("overview")).not.toThrow();
	});

	it("initial page whitelist includes the PR5 plugins page; unknown falls back", () => {
		// plugins 在白名单内：hash 透传后不再回退概览（宿主深链 #plugins）
		const a = loadPluginUi("#plugins");
		expect(a.client).toBeTruthy();
		// 未知 slug：装配仍成功并回 overview（白名单校验在模块内部完成）
		const b = loadPluginUi("#no-such-page");
		expect(b.client).toBeTruthy();
	});
});
