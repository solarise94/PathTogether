/**
 * 归属记录读取失败绝不按现读身份建上传（P1 复核 2026-10-18）。
 *
 * 场景原样移植自 release-review 的真实复现脚本（read-error-account.mjs，
 * 只读留档 /mnt/ssd2/tests/run-review1018-Zw4iud/read-error-account.mjs 及
 * 其输出 read-error-account.log）：A 拥有的转换产物，归属复核先按 A 通过；
 * 随后页面身份在产物视图（慢操作）窗口里刷新为 B；最后一道闸的
 * inlineRunner.getJob 读取**失败**。修复前代码以 job=null 调
 * createEntryUpload → acct 回落 currentUserId = B → 以 expected_account=B
 * 建上传（A 的产物记到 B 名下，服务端新核对反而通过）。修复后：读取失败
 * = 归属证据缺失，绝不建上传；行停 upload_failed（「继续上传」重试会重新
 * 读记录复核归属）。
 *
 * 本文件按仓库 vitest 框架改加载方式（断言不改，仅 harness 路径/装配适配）：
 * 真实 app.js 的 uploadEntryArtifact/createEntryUpload 代码切片 + 真实共享
 * 引擎 static/upload/cos-uploader.js + 真实编排器 static/upload/inline-convert.js。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const app = readFileSync(resolve(root, "static/app.js"), "utf8");
const w: Record<string, unknown> = {};
for (const n of ["inline-convert", "slide-sniff", "cos-uploader"]) {
	new Function(
		"window",
		readFileSync(resolve(root, "static/upload/" + n + ".js"), "utf8"),
	)(w);
}
const file = new File(
	[Uint8Array.from([73, 73, 43, 0, 8, 0, 0, 0, 16, 0, 0, 0, 0, 0, 0, 0])],
	"patient.ome.tif",
);
const fmt = "ome-bigtiff-subifd-rgb-jpeg-pyramid";
const record = {
	id: "job-A", state: "ready",
	source: { name: "patient.kfb", size: 16 },
	result: { outputBytes: 16, format: fmt },
	intent: { channel: "workbench-inline", state: "pending", account: "account-A" },
};
let current = "account-A";
let reads = 0;
let setPageAccount: (v: string) => void = () => {};
const runner = {
	listJobs: async () => [record],
	getJob: async () => {
		reads++;
		if (reads === 2) throw Error("transient OPFS read failure");
		return record;
	},
	artifactView: async () => {
		current = "account-B";
		setPageAccount(current);
		return file;
	},
	setJobUpload: async (_id: string, patch: Record<string, unknown>) => {
		record.upload = { ...(record as { upload?: unknown }).upload, ...patch };
	},
};
const creates: Array<Record<string, unknown>> = [];
const api = async (url: string, opts: Record<string, unknown> = {}) => {
	if (url === "/api/ingestions" && opts.method === "POST") {
		const body = JSON.parse(String(opts.body));
		creates.push(body);
		if (body.expected_account !== current) {
			return { ok: false, status: 409, json: async () => ({ code: "account_mismatch" }) };
		}
		return { ok: true, status: 202, json: async () => ({ job_id: "ingestion-B", stage: "preparing" }) };
	}
	return { ok: true, status: 200, json: async () => ({ stage: "viewable", slide_id: "slide-B" }) };
};
const cfg = { max_size_bytes: 100000, formats: ["tif"] };
// 2026-10-20：createEntryUpload 的 status 分支经 cosStageView（app.js 别处
// 定义）——切片一并携带；tt 是它的文案自由变量，注入桩。
const stageView = app.slice(
	app.indexOf("  function cosStageKey("),
	app.indexOf("  function cosShowStage("),
);
const code = app.slice(
	app.indexOf("  function uploadEntryArtifact("),
	app.indexOf("  /// 完成收口（§3.5）"),
) + stageView;
const harness = new Function(
	"window", "inlineRunner", "resolveCosConfig", "apiFetch", "apiFetchNoRedirect",
	"inlineRowSetStage", "authInfoReady", "tt",
	'var currentUserId="account-A",authInfoEnabled=true;' + code +
	';return {upload:uploadEntryArtifact,setAccount:(v)=>currentUserId=v};',
)(w, runner, () => cfg, api, api, () => {}, () => Promise.resolve(),
	(k: string) => k) as {
	upload: (e: unknown, a: unknown) => Promise<unknown>;
	setAccount: (v: string) => void;
};
setPageAccount = harness.setAccount;
const orch = (w as {
	HP_INLINE_CONVERT: {
		createOrchestrator: (d: Record<string, unknown>) => {
			restore: () => Promise<Array<{ id: string; state: string }>>;
			act: (id: string, action: string) => unknown;
		};
	};
}).HP_INLINE_CONVERT.createOrchestrator({
	runner, sniff: w.HP_SLIDE_SNIFF,
	engine: { outputFileName: () => file.name },
	cos: w.HP_COS_UPLOAD,
	caps: { cosConfig: cfg, inlineConvert: { viewableFormats: [fmt] } },
	account: () => current,
	identityReady: () => Promise.resolve(),
	authEnabled: () => true,
	onArtifact: harness.upload,
});
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("归属记录读取失败（read-error-account.mjs 场景移植）", () => {
	it("最终 getJob 读取失败：绝不以刷新后的身份建上传，行停可重试态", async () => {
		const [entry] = await orch.restore();
		reads = 0;
		expect(entry!.state).toBe("upload_queued");
		orch.act(entry!.id, "continue");
		for (let n = 0; n < 150 &&
			!["done", "account_mismatch", "upload_failed"].includes(entry!.state);
			n++) {
			await sleep(10);
		}
		// 原脚本输出（修复前）：{"reads":2,"state":"done","intentAccount":"account-A",
		// "uploadAccount":"account-B","creates":[{"filename":"patient.ome.tif",
		// "declared_size":16,"expected_account":"account-B"}]}
		console.log(JSON.stringify({
			reads, state: entry!.state, intentAccount: record.intent.account,
			uploadAccount: (record as { upload?: { account?: string } }).upload?.account,
			creates,
		}));
		expect(creates.length).toBe(0,
			"Failed ownership read must not create an upload under the refreshed account");
	}, 30_000);
});
