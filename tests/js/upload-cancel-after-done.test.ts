/**
 * 已收口直传行的残留「取消」必须是空操作（P3 复核 2026-10-18）。
 *
 * 场景原样移植自 dogfood 复核脚本（cancel-after-done.mjs，只读留档
 * /mnt/ssd2/tests/run-known-check-rfyRh6/cancel-after-done.mjs 及其输出
 * cancel-after-done.log）：直传的「取消」钮挂在编排器按钮宿主之外，上传
 * 完成后不随之摘除——完成后点击会把行翻回「已取消」并补发 cancel 请求
 * （服务端对 ready 资产只会拒绝，切片仍在）。修复后：引擎收口即终态
 *（settled()），点击残留钮零改行、零请求；按钮随 done 收口摘除。真实
 * cos-uploader.js 引擎 + 真实 app.js 的取消钮处理器切片（断言不改，仅
 * harness 路径适配）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");

describe("完成后点击残留「取消」钮（cancel-after-done.mjs 场景移植）", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("完成上传后点击：不改行、不发 cancel 请求", async () => {
		const w: Record<string, unknown> = {};
		new Function(
			"window",
			readFileSync(resolve(root, "static/upload/cos-uploader.js"), "utf8"),
		)(w);
		const calls: Array<{ url: string; method: string }> = [];
		const COS = (w as {
			HP_COS_UPLOAD: {
				createUpload: (o: Record<string, unknown>) => {
					done: Promise<{ ok: boolean; body?: Record<string, unknown> }>;
					settled?: () => boolean;
				};
			};
		}).HP_COS_UPLOAD;
		const upload = COS.createUpload({
			source: new File(["synthetic"], "fixture.ome.tif"),
			config: { max_size_bytes: 1000, formats: ["tif"] },
			account: "fixture-account",
			storage: {
				save: () => {}, complete: () => {}, remove: () => {},
				findResumable: () => null, readConfirmed: () => [], readRecord: () => null,
			},
			apiFetch: async (url: string, opts: Record<string, unknown> = {}) => {
				calls.push({ url, method: String(opts.method || "GET") });
				return {
					ok: true, status: 200,
					json: async () => url === "/api/ingestions"
						? { job_id: "fixture-job", stage: "preparing" }
						: { stage: "viewable", slide_id: "fixture-slide" },
				};
			},
		});
		const result = await upload.done;
		expect(result.ok).toBe(true);
		const app = readFileSync(resolve(root, "static/app.js"), "utf8");
		const begin = app.indexOf(
			'    addRowButton(row, tt("upload.cos.cancel"), function () {',
			app.indexOf("  function uploadFileCos("),
		);
		const end = app.indexOf("\n\n    // 复核 8428f7f0（双标签", begin);
		let click: (() => void) | null = null;
		const stages: string[] = [];
		new Function(
			"row", "tt", "addRowButton", "upload", "lockAbort",
			app.slice(begin, end),
		)(
			{ setStage: (s: string) => stages.push(s), finish: () => {} },
			(k: string) => k,
			(_r: unknown, _l: unknown, fn: () => void) => { click = fn; },
			upload,
			null,
		);
		click!();
		// 原脚本输出（修复前）：{"completed":true,
		// "displayAfterClick":["upload.cos.cancelled"],
		// "cancelRequests":[{"url":"/api/ingestions/fixture-job/cancel","method":"POST"}]}
		console.log(JSON.stringify({
			completed: result.ok,
			displayAfterClick: stages,
			cancelRequests: calls.filter((c) => c.url.endsWith("/cancel")),
		}));
		expect(stages.length).toBe(0,
			"Clicking a leftover button must not change a completed upload to cancelled");
		// 同一闸的引擎侧：终态后引擎 cancel 也不补发请求（按钮摘除前的窗口）
		expect(upload.settled && upload.settled()).toBe(true);
	}, 30_000);
});
