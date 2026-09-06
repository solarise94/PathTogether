/**
 * viewer 画质档（image-transport-upgrade §3.3/§5.2）：HP_ViewerEncoding 单元。
 *
 * 加载真实 static/viewer-encoding.js（最小 DOM stub + localStorage stub），
 * 锁定：
 *   - info.display 能力缺失（旧服务端）→ available=false，tile/thumbnail
 *     query 恒 ""（旧 URL 语义），不伪装能力；
 *   - RGB 两档（standard/detail）与偏好持久化（localStorage 按模式隔离）；
 *     非法偏好回默认档；
 *   - 多通道恒 fluorescence-preserve-v1（荧光保真，无省流档），不持久化偏好；
 *   - tileQuery/thumbnailQuery 携带 profile+dv 成对参数；
 *   - render-context 的 display_versions 到达后 dv 更新（自定义 context）；
	 *   - 409 有界恢复：真实 OSD tile.getUrl() 事件形状；合并并发失败瓦片
	 *     （同窗口只诊断一次）；open 不清零；tile-loaded 才结束 episode；
	 *     第二次冲突停止自动重试；401/403 不降级。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
	resolve(here, "../../static/viewer-encoding.js"),
	"utf8",
);

type Handler = (ev?: unknown) => void;

interface FakeViewer {
	handlers: Record<string, Handler[]>;
	addHandler: (type: string, cb: Handler) => void;
	emit: (type: string, ev?: unknown) => void;
}

function fakeViewer(): FakeViewer {
	const handlers: Record<string, Handler[]> = {};
	return {
		handlers,
		addHandler(type, cb) {
			(handlers[type] = handlers[type] || []).push(cb);
		},
		emit(type, ev) {
			(handlers[type] || []).slice().forEach((cb) => cb(ev));
		},
	};
}

function loadModule(opts: {
	localStorage?: Record<string, string>;
	fetchMock?: (url: string, init?: unknown) => Promise<unknown>;
} = {}) {
	const store: Record<string, string> = opts.localStorage || {};
	const windowStub: Record<string, unknown> = {
		localStorage: {
			getItem: (k: string) => (k in store ? store[k] : null),
			setItem: (k: string, v: string) => {
				store[k] = v;
			},
		},
		document: {
			createElement: () => ({
				className: "",
				textContent: "",
				title: "",
				setAttribute: () => {},
				addEventListener: () => {},
				appendChild: () => {},
			}),
			addEventListener: () => {},
		},
		fetch: opts.fetchMock || (() => Promise.resolve({ status: 404 })),
		Date,
	};
	const fn = new Function("window", `${src}\n return window.HP_ViewerEncoding;`);
	// 模块按 window 上的字段解构（localStorage/document/fetch/Date）
	const win = windowStub as unknown as Window &
		Record<string, unknown> & { HP_ViewerEncoding?: unknown };
	fn.call(globalThis, win);
	return win.HP_ViewerEncoding as Record<string, callable> & {
		_state: Record<string, unknown>;
	};
}

const RGB_DISPLAY = {
	image_mode: "native_rgb",
	display_asset_revision: "dar1-abc",
	default_profile: "native-standard-v1",
	profiles: [
		{ profile_id: "native-standard-v1", display_version: "a".repeat(64) },
		{ profile_id: "native-detail-v1", display_version: "b".repeat(64) },
	],
	thumbnail: {
		max_edge: 400,
		profiles: [
			{ profile_id: "native-thumb-v1", display_version: "c".repeat(64) },
		],
	},
};

const MC_DISPLAY = {
	image_mode: "multichannel",
	display_asset_revision: "dar1-def",
	default_profile: "fluorescence-preserve-v1",
	profiles: [
		{
			profile_id: "fluorescence-preserve-v1",
			display_version: "d".repeat(64),
		},
	],
	thumbnail: {
		max_edge: 400,
		profiles: [
			{
				profile_id: "fluorescence-thumb-v1",
				display_version: "e".repeat(64),
			},
		],
	},
};

describe("HP_ViewerEncoding", () => {
	beforeEach(() => {
		vi.stubGlobal("window", globalThis.window);
	});

	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("旧服务端（info 无 display）→ 能力缺失，恒旧 URL", () => {
		const hp = loadModule();
		const res = hp.handleDisplay(null);
		expect(res.available).toBe(false);
		expect(hp.tileQuery()).toBe("");
		expect(hp.thumbnailQuery()).toBe("");
		const res2 = hp.handleDisplay({});
		expect(res2.available).toBe(false);
	});

	it("RGB 两档：默认标准；偏好按模式持久化；非法偏好回默认", () => {
		const store: Record<string, string> = {};
		const hp = loadModule({ localStorage: store });
		const res = hp.handleDisplay(RGB_DISPLAY);
		expect(res.available).toBe(true);
		expect(res.selected).toBe("native-standard-v1");
		// URL 成对携带 profile+dv
		expect(hp.tileQuery()).toBe(
			`?profile=native-standard-v1&dv=${"a".repeat(64)}`,
		);
		// 切精细：持久化 + query 更新
		expect(hp.setPreference("native-detail-v1")).toBe(true);
		expect(store["pt.viewerQuality.rgb"]).toBe("native-detail-v1");
		expect(hp.tileQuery()).toBe(`?profile=native-detail-v1&dv=${"b".repeat(64)}`);
		// 下一次 info（重开）记住偏好
		const res2 = hp.handleDisplay(RGB_DISPLAY);
		expect(res2.selected).toBe("native-detail-v1");
		// 非法偏好（不在白名单）回默认
		store["pt.viewerQuality.rgb"] = "native-bogus-v1";
		const res3 = hp.handleDisplay(RGB_DISPLAY);
		expect(res3.selected).toBe("native-standard-v1");
	});

	it("多通道：恒荧光保真（preserve 档），thumbnail 用 fluorescence-thumb", () => {
		const hp = loadModule();
		const res = hp.handleDisplay(MC_DISPLAY);
		expect(res.available).toBe(true);
		expect(hp.selectedProfile()).toBe("fluorescence-preserve-v1");
		// 任何 RGB 档切换在 MC 下都无效（无省流档）
		expect(hp.setPreference("native-detail-v1")).toBe(false);
		expect(hp.tileQuery()).toBe(
			`?profile=fluorescence-preserve-v1&dv=${"d".repeat(64)}`,
		);
		expect(hp.thumbnailQuery()).toBe(
			`?profile=fluorescence-thumb-v1&dv=${"e".repeat(64)}`,
		);
	});

	it("render-context display_versions 到达：dv 更新（自定义 context）", () => {
		const hp = loadModule();
		hp.handleDisplay(RGB_DISPLAY);
		const nv = "f".repeat(64);
		hp.handleDisplayVersions({
			"native-standard-v1": nv,
			"native-thumb-v1": "e".repeat(64),
		});
		expect(hp.tileQuery()).toBe(`?profile=native-standard-v1&dv=${nv}`);
	});

		function failedTile(url: string) {
			return { tile: { getUrl: () => url }, tiledImage: {}, time: 0, message: "fail", tileRequest: null };
		}

		it("409 恢复：合并并发失败（一次诊断）；第二次冲突停止自动重试", async () => {
			const fetched: string[] = [];
			let status = 409;
			const hp = loadModule({
				fetchMock: (url: string) => {
					fetched.push(url as string);
					return Promise.resolve({ status });
				},
			});
			hp.handleDisplay(RGB_DISPLAY);
			const viewer = fakeViewer();
			const onConflict = vi.fn();
			hp.installConflictRecovery({ viewer, onConflict });
			const u0 = `/api/slide/x_files/0/0_0.jpeg?profile=native-standard-v1&dv=x`;
			const u1 = `/api/slide/x_files/0/0_1.jpeg?profile=native-standard-v1&dv=x`;
			// 同窗口三张失败瓦片 → 只诊断一次
			viewer.emit("tile-load-failed", failedTile(u0));
			viewer.emit("tile-load-failed", failedTile(u1));
			await Promise.resolve();
			await Promise.resolve();
			expect(fetched.length).toBe(1);
			expect(onConflict).toHaveBeenCalledTimes(1);
			// 第二轮冲突（新窗口）：停止自动重试
			await new Promise((r) => setTimeout(r, 1600));
			viewer.emit("tile-load-failed", failedTile(u0));
			await Promise.resolve();
			await Promise.resolve();
			expect(onConflict).toHaveBeenCalledTimes(1); // 不再触发
			expect(hp._state.conflictCount).toBe(2);
		});

		it("真实 OSD 事件缺 tile.getUrl 时不诊断", async () => {
			const fetched: string[] = [];
			const hp = loadModule({
				fetchMock: (url: string) => {
					fetched.push(url as string);
					return Promise.resolve({ status: 409 });
				},
			});
			hp.handleDisplay(RGB_DISPLAY);
			const viewer = fakeViewer();
			hp.installConflictRecovery({ viewer, onConflict: vi.fn() });
			viewer.emit("tile-load-failed", {
				source: { src: `/api/slide/x_files/0/0_0.jpeg?profile=native-standard-v1&dv=x` },
			});
			await Promise.resolve();
			await Promise.resolve();
			expect(fetched.length).toBe(0);
			expect(hp._state.conflictCount).toBe(0);
		});

		it("409 → open 重开不清零；第二次冲突停止", async () => {
			const fetched: string[] = [];
			const hp = loadModule({
				fetchMock: (url: string) => {
					fetched.push(url as string);
					return Promise.resolve({ status: 409 });
				},
			});
			hp.handleDisplay(RGB_DISPLAY);
			const viewer = fakeViewer();
			const onConflict = vi.fn();
			hp.installConflictRecovery({ viewer, onConflict });
			const u = `/api/slide/x_files/0/0_0.jpeg?profile=native-standard-v1&dv=x`;
			viewer.emit("tile-load-failed", failedTile(u));
			await Promise.resolve();
			await Promise.resolve();
			expect(onConflict).toHaveBeenCalledTimes(1);
			expect(hp._state.conflictCount).toBe(1);
			viewer.emit("open", {});
			expect(hp._state.conflictCount).toBe(1);
			await new Promise((r) => setTimeout(r, 1600));
			viewer.emit("tile-load-failed", failedTile(u));
			await Promise.resolve();
			await Promise.resolve();
			expect(onConflict).toHaveBeenCalledTimes(1);
			expect(hp._state.conflictCount).toBe(2);
		});

		it("版本化 tile-loaded 结束 episode，后续冲突可再恢复一次", async () => {
			const hp = loadModule({
				fetchMock: () => Promise.resolve({ status: 409 }),
			});
			hp.handleDisplay(RGB_DISPLAY);
			const viewer = fakeViewer();
			const onConflict = vi.fn();
			hp.installConflictRecovery({ viewer, onConflict });
			const u = `/api/slide/x_files/0/0_0.jpeg?profile=native-standard-v1&dv=x`;
			viewer.emit("tile-load-failed", failedTile(u));
			await Promise.resolve();
			await Promise.resolve();
			expect(onConflict).toHaveBeenCalledTimes(1);
			viewer.emit("tile-loaded", failedTile(u));
			expect(hp._state.conflictCount).toBe(0);
			await new Promise((r) => setTimeout(r, 1600));
			viewer.emit("tile-load-failed", failedTile(u));
			await Promise.resolve();
			await Promise.resolve();
			expect(onConflict).toHaveBeenCalledTimes(2);
			expect(hp._state.conflictCount).toBe(1);
		});

		it("401/403 不降级（不触发 onConflict）", async () => {
			const fetched: string[] = [];
			const hp = loadModule({
				fetchMock: (url: string) => {
					fetched.push(url as string);
					return Promise.resolve({ status: 403 });
				},
			});
			hp.handleDisplay(RGB_DISPLAY);
			const viewer = fakeViewer();
			const onConflict = vi.fn();
			hp.installConflictRecovery({ viewer, onConflict });
			viewer.emit("tile-load-failed", {
				tile: { getUrl: () => `/api/slide/x_files/0/0_0.jpeg?profile=p&dv=x` },
			});
			await Promise.resolve();
			await Promise.resolve();
			expect(fetched.length).toBe(1);
			expect(onConflict).not.toHaveBeenCalled();
		});

		it("open 事件本身不清零连续冲突计数", () => {
			const hp = loadModule();
			hp.handleDisplay(RGB_DISPLAY);
			const viewer = fakeViewer();
			hp.installConflictRecovery({ viewer, onConflict: () => {} });
			hp._state.conflictCount = 2;
			viewer.emit("open", {});
			expect(hp._state.conflictCount).toBe(2);
		});
});
