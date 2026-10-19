/**
 * 上传流程修复（review 2026-10-20 三处 P2 + 五步文案）单元：
 *
 *  P2-1 等内容锁期间取消：uploadFileCos 的**唯一结果通道**恰好收口一次
 *       （等锁取消 / 拿锁后发现已取消 / 引擎缺失三个建引擎前出口）；
 *  P2-2 直传失败恢复：classifyUploadError + uploadRecoveryKinds 按
 *       「失败类型 + 服务器任务」给恢复动作（不按本机 jobId）；条目携带
 *       服务器任务 id（entry.uploadJobId），续传同任务、显式重传才新建；
 *  编排器：迟到成功回执不得清掉新条目的上传槽（恒等守卫）；
 *  五步文案：zh/en 精确串 + 服务端阶段映射 + 转换阶段五步化；
 *  P2-3：抽屉宿主 + 合并空态的源码级接线。
 *
 * 加载面：inline-convert.js / i18n.js 是 classic script（new Function）；
 * uploadFileCos 从 app.js 源码切片（自由变量全部显式注入，workbench-
 * direct-class.test.ts 同形态）。
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const appSrc = readFileSync(resolve(root, "static/app.js"), "utf8");
const inlineSrc = readFileSync(resolve(root, "static/upload/inline-convert.js"), "utf8");
const i18nSrc = readFileSync(resolve(root, "static/i18n.js"), "utf8");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function flush(n = 12) {
	for (let i = 0; i < n; i++) await sleep(0);
}

type W = Record<string, unknown>;
function loadInline() {
	const w: W = {};
	new Function("window", inlineSrc)(w);
	return w;
}
const IC = () => loadInline().HP_INLINE_CONVERT as {
	classifyUploadError(err: unknown): { kind: string; code: string; detail: unknown };
	uploadRecoveryKinds(entry: unknown): string[];
	TERMINAL_RETRYABLE: Record<string, number>;
	createOrchestrator(deps: Record<string, unknown>): Orch;
};

interface Entry {
	id: string; state: string; name: string; size: number; jobId: string | null;
	uploadJobId?: string | null; cancelled?: boolean; error?: unknown;
	[key: string]: unknown;
}
interface Orch {
	enqueue(file: unknown, opts?: unknown): Entry;
	act(id: string, action: string): unknown;
	cancel(e: Entry): void;
	list(): Entry[];
	busy(): { converting: boolean; uploading: boolean };
	[key: string]: unknown;
}

// --------------------------------------------------------------------- //
// P2-2：失败分类 + 恢复动作矩阵（唯一权威表）
// --------------------------------------------------------------------- //
describe("P2-2 uploadRecoveryKinds：按失败类型 + 服务器任务决定恢复动作", () => {
	const I = IC();

	it("分块/网络失败 → 同任务续传（continueUpload），不再只剩申请支持", () => {
		expect(I.classifyUploadError({ part: 3 })).toMatchObject({ kind: "part" });
		expect(I.uploadRecoveryKinds({ error: { kind: "part" } }))
			.toEqual(["continueUpload"]);
		expect(I.classifyUploadError({ network: true })).toMatchObject({ kind: "network" });
		expect(I.uploadRecoveryKinds({ error: { kind: "network" } }))
			.toEqual(["continueUpload"]);
	});

	it("状态轮询失败 → 只重查同一任务（recheckUpload），绝不重建/重传", () => {
		expect(I.classifyUploadError({ recheck: true })).toMatchObject({ kind: "recheck" });
		expect(I.uploadRecoveryKinds({ error: { kind: "recheck" } }))
			.toEqual(["recheckUpload"]);
	});

	it("明确服务器终态 → 显式重新上传；validation_failed 同排支持申请", () => {
		// 服务器判文件未通过结构校验：真不可读的文件重传只会再失败 →
		// 「重新上传」+「申请支持该格式」两条出路同排
		expect(I.uploadRecoveryKinds({
			uploadJobId: "ing-vf",
			error: { kind: "terminal", code: "validation_failed" },
		})).toEqual(["reupload", "support"]);
		// 哈希/计划不匹配是传输完整性问题：重传即修复，保持仅重传
		for (const code of ["client_gone", "client_idle_timeout",
			"hash_mismatch", "plan_size_mismatch"]) {
			expect(I.TERMINAL_RETRYABLE[code]).toBe(1);
			expect(I.uploadRecoveryKinds({
				uploadJobId: "ing-1",
				error: { kind: "terminal", code },
			})).toEqual(["reupload"]);
		}
		expect(I.uploadRecoveryKinds({
			uploadJobId: "ing-1", error: { kind: "terminal", code: "upload_too_large" },
		})).toEqual([]);
		expect(I.uploadRecoveryKinds({
			uploadJobId: "ing-1",
			error: { kind: "terminal", code: "cos_pool_below_product_limit" },
		})).toEqual([]);
	});

	it("格式失败 → 只给申请支持该格式（支持申请只服务格式失败）", () => {
		expect(I.classifyUploadError({ magicMismatch: { detected: "png" } }))
			.toMatchObject({ kind: "format" });
		expect(I.classifyUploadError({ data: { code: "cos_format_unsupported" } }))
			.toMatchObject({ kind: "format" });
		expect(I.uploadRecoveryKinds({ error: { kind: "format" } }))
			.toEqual(["support"]);
	});

	it("未知错误：有任务 → 同任务续传；无任务 → 全新重试（建任务前失败无重复风险）", () => {
		// 有任务：安全续传同一 ingestion
		expect(I.uploadRecoveryKinds({
			uploadJobId: "ing-9", error: { kind: "generic", code: "weird_thing" },
		})).toEqual(["continueUpload"]);
		// 无任务（POST /api/ingestions 5xx/网络失败、能力拉取失败等）：
		// 全新重试安全（没有可复用也不会重复建任务）——不再是「无恢复」
		expect(I.uploadRecoveryKinds({ uploadJobId: null,
			error: { kind: "generic", code: "weird_thing" } }))
			.toEqual(["reupload"]);
		expect(I.uploadRecoveryKinds({ error: { kind: "generic", code: "" } }))
			.toEqual(["reupload"]);
		// 格式样错误码（无任务）：重试之外同排支持申请
		expect(I.uploadRecoveryKinds({ uploadJobId: null,
			error: { kind: "generic", code: "cos_format_unsupported" } }))
			.toEqual(["reupload", "support"]);
		// 直接条目（无本机转换 jobId）同样适用
		expect(I.uploadRecoveryKinds({ jobId: null, uploadJobId: null,
			error: { kind: "generic", code: "some_5xx_code" } }))
			.toEqual(["reupload"]);
	});

	it("直传条目（无本机转换 jobId）与转换产物得到同一套恢复动作", () => {
		// 直传：jobId（本机转换任务）恒为 null——恢复入口不依赖它
		expect(I.uploadRecoveryKinds({
			jobId: null, uploadJobId: "ing-direct",
			error: { kind: "part", code: "part_failed" },
		})).toEqual(["continueUpload"]);
		// 转换产物：jobId 存在，同判
		expect(I.uploadRecoveryKinds({
			jobId: "opfs-1", uploadJobId: "ing-conv",
			error: { kind: "part", code: "part_failed" },
		})).toEqual(["continueUpload"]);
	});
});

describe("P2-2 编排器：显式重传清任务 id；续传保留同任务", () => {
	function fakeFile(name: string, size: number) {
		return { name, size, slice: () => new ArrayBuffer(0) };
	}
	function makeOrch() {
		const w = loadInline();
		const job = {
			id: "opfs-1", state: "ready",
			source: { name: "a.kfb", size: 10 },
			intent: { channel: "workbench-inline", state: "pending", account: "u1" },
			result: { outputBytes: 5, format: "fmt" },
		};
		let posted = 0;
		const orch = (w.HP_INLINE_CONVERT as {
			createOrchestrator: (d: Record<string, unknown>) => Orch;
		}).createOrchestrator({
			runner: {
				listJobs: async () => [],
				getJob: async () => job,
				setJobUpload: async () => undefined,
				setJobIntent: async () => undefined,
				artifactView: async () => fakeFile("a.ome.tif", 5),
			},
			engine: { outputFileName: () => "a.ome.tif", errCode: () => null },
			sniff: null,
			cos: {},
			caps: { inlineConvert: { viewableFormats: ["fmt"] },
				cosConfig: { max_size_bytes: 1000 } },
			account: () => "u1",
			apiFetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
			onArtifact: (e: Entry) => {
				posted += 1;
				(e as { resumeSeen?: unknown }).resumeSeen = (e as { resumeJobIdPassed?: unknown }).resumeJobIdPassed ?? null;
				return { cancelled: false, body: { slide_id: "sld-1" } };
			},
			ui: {},
		});
		return { orch, posted: () => posted };
	}

	it("act('reupload') 清掉 uploadJobId 并置 _freshUpload（下一轮绝不带旧任务 id）", () => {
		const { orch } = makeOrch();
		const w = loadInline();
		// 条目构造：直接注入终态失败条目（upload_failed + 任务 id）
		const e = (orch as { _entries: Entry[] })._entries[0] || null;
		expect(e).toBeNull();
		void w;
		const entry: Entry = {
			id: "x", state: "upload_failed", name: "a", size: 1, jobId: null,
			uploadJobId: "ing-old",
			error: { kind: "terminal", code: "client_gone" },
		};
		(orch as { _entries: Entry[] })._entries.push(entry);
		orch.act("x", "reupload");
		expect(entry.state).toBe("upload_queued");
		expect(entry.uploadJobId).toBeNull();
		expect(entry._freshUpload).toBe(true);
		// recheckUpload 保留任务 id（同任务重查）
		const entry2: Entry = {
			id: "y", state: "upload_failed", name: "b", size: 1, jobId: null,
			uploadJobId: "ing-keep",
			error: { kind: "recheck", code: "recheck" },
		};
		(orch as { _entries: Entry[] })._entries.push(entry2);
		orch.act("y", "recheckUpload");
		expect(entry2.state).toBe("upload_queued");
		expect(entry2.uploadJobId).toBe("ing-keep");
		expect(entry2._freshUpload).toBeUndefined();
	});

	it("uploadEntryArtifact 按条目上的任务 id 续传（源码级：resumeJobId 传 entry.uploadJobId）", () => {
		expect(appSrc).toContain(
			"resumeJobId: entry._freshUpload ? null : (entry.uploadJobId || null)");
		// 显式重传不复活旧任务；任务建成即回填条目（onJobCreated）
		expect(appSrc).toContain("onJobCreated: function (id) {");
		expect(appSrc).toContain("entry.uploadJobId = id;");
	});
});

// --------------------------------------------------------------------- //
// P2-1：uploadFileCos 唯一结果通道（建引擎前出口恰好收口一次）
// --------------------------------------------------------------------- //
describe("P2-1 uploadFileCos：唯一结果通道恰好收口一次", () => {
	function rowStub() {
		const calls: Array<[string, unknown[]]> = [];
		const btns: Array<() => void> = [];
		return {
			calls, btns,
			setStage: (...a: unknown[]) => void calls.push(["setStage", a]),
			markError: () => void calls.push(["markError", []]),
			finish: (...a: unknown[]) => void calls.push(["finish", a]),
			_row: { appendChild: () => {} },
		};
	}
	function loadUploadFileCos(w: W, overrides: Record<string, unknown> = {}) {
		const begin = appSrc.indexOf("  function uploadFileCos(");
		const end = appSrc.indexOf("  function cosUploadSucceeded(");
		const src = appSrc.slice(begin, end);
		const fn = new Function(
			"window", "tt", "t", "addRowButton", "magicTypeName", "cosErrorMessage",
			"toast", "cosJobSave", "cosJobRemove", "cosFindResumableJob", "cosJobsRead",
			"cosReadJobRecord", "COS_UPLOAD_CONFIG", "fbUpAssocSave",
			"cosUploadSucceeded", "cosShowStage", "uploadFile",
			"isAccountMismatchError", "apiFetch", "apiFetchNoRedirect",
			"cosInflightJobs", "humanSize", "cosStageView", "currentUserId",
			src + ";return uploadFileCos;",
		);
		return fn(
			w,
			overrides.tt || ((k: string) => k),
			overrides.t || ((k: string, v?: unknown) => String(k)),
			overrides.addRowButton ||
				((_r: unknown, _l: string, onClick: () => void) => { void onClick; return {}; }),
			overrides.magicTypeName || ((d: unknown) => String(d)),
			overrides.cosErrorMessage || ((s: number, d: unknown) => String(d)),
			overrides.toast || (() => {}),
			overrides.cosJobSave || (() => {}),
			overrides.cosJobRemove || (() => {}),
			overrides.cosFindResumableJob || (() => null),
			overrides.cosJobsRead || (() => []),
			overrides.cosReadJobRecord || (() => Promise.resolve(null)),
			overrides.COS_UPLOAD_CONFIG ?? { max_size_bytes: 1000, formats: ["tif"] },
			overrides.fbUpAssocSave || (() => {}),
			overrides.cosUploadSucceeded || (() => {}),
			overrides.cosShowStage || (() => {}),
			overrides.uploadFile || (() => {}),
			overrides.isAccountMismatchError || (() => false),
			overrides.apiFetch || (async () => ({})),
			overrides.apiFetchNoRedirect || (async () => ({})),
			overrides.cosInflightJobs || {},
			overrides.humanSize || ((n: number) => String(n)),
			overrides.cosStageView || (() => ({ key: "k", frac: null, indeterminate: true, note: "" })),
			overrides.currentUserId ?? "u1",
		) as (file: unknown, row: unknown, opts: Record<string, unknown>) => void;
	}

	it("等内容锁期间取消（abort）→ onOutcome 恰好一次取消回执，不建引擎", async () => {
		let releaseLock: (() => void) | null = null;
		const lockRejected = new Promise<never>((_res, rej) => {
			releaseLock = () => rej(Object.assign(new Error("abort"), { name: "AbortError" }));
		});
		let engineCreated = 0;
		const w: W = {
			HP_COS_UPLOAD: {
				contentLockName: () => "lock",
				acquireContentLock: () => lockRejected,
				createUpload: () => { engineCreated += 1; return { done: Promise.resolve({}) }; },
			},
		};
		const uploadFileCos = loadUploadFileCos(w);
		const outcomes: Array<[unknown, unknown]> = [];
		const row = rowStub();
		uploadFileCos({ name: "a.tif", size: 1 }, row, {
			isCancelled: () => false,
			onOutcome: (b: unknown, e: unknown) => outcomes.push([b, e]),
		});
		// 行上的「取消」按钮被挂上：点击 → abort → 锁等待 reject → 收口
		expect(releaseLock).toBeTruthy();
		(releaseLock as () => void)();
		await flush(6);
		expect(outcomes.length).toBe(1);
		expect(outcomes[0][1]).toMatchObject({ cancelled: true, __cancelled: true });
		expect(engineCreated).toBe(0);
		// 行切「已取消」且取消按钮被摘除（settleOutcome → dropRowCancelBtn）
		expect(row.calls.some((c) => c[0] === "setStage" &&
			c[1][0] === "upload.cos.cancelled")).toBe(true);
	});

	it("迟到锁回执不得二次收口（exactly-once 闸）", async () => {
		let lockResolve: ((v: { held: boolean; release: () => void }) => void) | null = null;
		const lockP = new Promise<{ held: boolean; release: () => void }>((res) => {
			lockResolve = res;
		});
		let released = 0;
		const w: W = {
			HP_COS_UPLOAD: {
				contentLockName: () => "lock",
				acquireContentLock: () => lockP,
				createUpload: () => ({ done: Promise.resolve({ cancelled: true }) }),
			},
		};
		const uploadFileCos = loadUploadFileCos(w);
		const outcomes: unknown[] = [];
		uploadFileCos({ name: "a.tif", size: 1 }, rowStub(), {
			isCancelled: () => true,   // 编排器已取消
			onOutcome: (_b: unknown, e: unknown) => outcomes.push(e),
		});
		// 迟到：锁此时才拿到（取消后）；成功分支必须就地释放并保持唯一收口
		lockResolve!({ held: true, release: () => { released += 1; } });
		await flush(6);
		expect(outcomes.length).toBe(1);
		expect(outcomes[0]).toMatchObject({ __cancelled: true });
		expect(released).toBe(1);
	});

	it("引擎缺失（建引擎前失败）→ onOutcome 恰好一次 cos_unavailable，不悬挂", async () => {
		const w: W = {};   // 无 HP_COS_UPLOAD
		const uploadFileCos = loadUploadFileCos(w);
		const outcomes: Array<[unknown, unknown]> = [];
		const row = rowStub();
		uploadFileCos({ name: "a.tif", size: 1 }, row, {
			onOutcome: (b: unknown, e: unknown) => outcomes.push([b, e]),
		});
		await flush(4);
		expect(outcomes.length).toBe(1);
		expect(outcomes[0][1]).toMatchObject({ data: { code: "cos_unavailable" } });
		expect(row.calls.some((c) => c[0] === "markError")).toBe(true);
	});
});

// --------------------------------------------------------------------- //
// 编排器：迟到成功回执不清新条目的上传槽（P2-1 的编排器侧恒等守卫）
// --------------------------------------------------------------------- //
describe("P2-1 编排器：迟到的成功回执不清新条目的槽、不复活已取消条目", () => {
	function fakeFile(name: string, size: number) {
		return { name, size, slice: () => new ArrayBuffer(0) };
	}
	it("取消 A 后 B 占槽；A 的迟到成功不改状态、不释放 B 的槽", async () => {
		const w = loadInline();
		let resolveA: ((v: unknown) => void) | null = null;
		let resolveB: ((v: unknown) => void) | null = null;
		const started: string[] = [];
		const orch = (w.HP_INLINE_CONVERT as {
			createOrchestrator: (d: Record<string, unknown>) => Orch;
		}).createOrchestrator({
			runner: { listJobs: async () => [], getJob: async () => null },
			engine: {}, sniff: null, cos: {},
			caps: { cosConfig: { max_size_bytes: 1000, formats: ["tif"] } },
			account: () => "u1",
			apiFetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
			onArtifact: (e: Entry) => {
				// 首个启动的是 A，第二个是 B（entry.id 是生成的 uuid）
				const isA = started.length === 0;
				started.push(isA ? "A" : "B");
				return new Promise((res) => {
					if (isA) resolveA = res;
					else resolveB = res;
				});
			},
			ui: {},
		});
		const a = orch.enqueue(fakeFile("a.tif", 1));
		await flush(8);
		expect(started.length).toBe(1);
		orch.cancel(a);
		expect(a.cancelled).toBe(true);
		expect(orch.busy().uploading).toBe(false);   // 槽已随取消释放
		const b = orch.enqueue(fakeFile("b.tif", 1));
		await flush(8);
		expect(started.length).toBe(2);
		expect(orch.busy().uploading).toBe(true);    // B 正在上传
		// A 的迟到成功回执：A 保持已取消；B 的槽不被清（仍 uploading）
		resolveA!({ body: { slide_id: "sld-a" } });
		await flush(8);
		expect(a.state).toBe("cancelled");
		expect(orch.busy().uploading).toBe(true);
		// B 照常完成
		resolveB!({ body: { slide_id: "sld-b" } });
		await flush(8);
		expect(b.state).toBe("done");
		expect(orch.busy().uploading).toBe(false);
	});
});

// --------------------------------------------------------------------- //
// 五步文案：精确串（zh/en）+ 服务端/转换阶段映射
// --------------------------------------------------------------------- //
describe("五步文案：用户可见状态 zh/en", () => {
	function dict(lang: "zh" | "en"): Map<string, string> {
		const marker = lang === "zh" ? 'zh: {' : "en: {";
		const start = i18nSrc.indexOf(marker);
		const block = lang === "zh"
			? i18nSrc.slice(start, i18nSrc.indexOf("en: {"))
			: i18nSrc.slice(start);
		const out = new Map<string, string>();
		const re = /"([^"\\]+)":\s*"((?:[^"\\]|\\.)*)"/g;
		let m: RegExpExecArray | null;
		while ((m = re.exec(block)) !== null) out.set(m[1], m[2]);
		return out;
	}

	it("五步精确串存在（zh + en）：选择上传/准备/正在上传/服务器正在处理/上传成功", () => {
		const zh = dict("zh");
		const en = dict("en");
		// 步 2（准备；仅确在转换时带「（格式转换中）」括注——direct 文件与
		// 排队态都不显示转换状态；「排队中…」旧文案不复活）
		expect(zh.get("upload.step.prepare")).toBe("正在准备上传文件");
		expect(zh.get("upload.step.converting")).toBe("正在准备上传文件（格式转换中）");
		expect(en.get("upload.step.prepare")).toBe("Preparing upload");
		expect(en.get("upload.step.converting")).toBe("Preparing upload (converting)");
		expect(zh.get("upload.inline.stage.queued")).toBe("正在准备上传文件");
		expect(en.get("upload.inline.stage.queued")).toBe("Preparing upload");
		// 必要停止态保留自己的显式文案：跨标签槽等待（可取消）与身份确认等待
		expect(zh.get("upload.cos.other.tab")).toContain("另一个标签页");
		expect(zh.get("upload.cos.other.tab")).toContain("可取消");
		expect(zh.get("upload.inline.identity.waiting")).toContain("正在确认登录身份");
		// 步 3（正在上传（xx%）——百分比由真实字节分母驱动，并入主标签）
		expect(zh.get("upload.cos.stage.uploading")).toBe("正在上传");
		// 步 4（服务器正在处理（xx%）——仅服务器接收 checkpoint 有真实分母；
		// 无分母用 … 活动指示；readiness/viewable 检查也属于本步——
		// 直到 viewable 才切「上传成功」，行内不再显示「准备打开」）
		expect(zh.get("upload.cos.stage.awaiting_server")).toBe("服务器正在处理…");
		expect(zh.get("upload.cos.stage.downloading")).toBe("服务器正在处理");
		expect(zh.get("upload.cos.stage.validating")).toBe("服务器正在处理…");
		expect(zh.get("upload.cos.stage.processing")).toBe("服务器正在处理…");
		expect(zh.get("upload.cos.stage.readiness")).toBe("服务器正在处理…");
		expect(zh.get("upload.cos.stage.viewable")).toBe("服务器正在处理…");
		expect(en.get("upload.cos.stage.awaiting_server")).toBe("Server processing…");
		expect(en.get("upload.cos.stage.downloading")).toBe("Server processing");
		expect(en.get("upload.cos.stage.readiness")).toBe("Server processing…");
		expect(en.get("upload.cos.stage.viewable")).toBe("Server processing…");
		// 步 5：只有 viewable 才是成功（upload.stage.done 的判定不变；
		// 该键的主源是 app.js 的 _EXTRA 兜底表——i18n.js 无此键，两处一致）
		expect(zh.has("upload.stage.done")).toBe(false);
		expect(appSrc).toContain('"upload.stage.done": { zh: "上传成功", en: "Upload successful" }');
		expect(appSrc).toContain(
			'"upload.cos.stage.awaiting_server": { zh: "服务器正在处理…", en: "Server processing…" }');
		// 百分比并入主阶段标签（用户字面格式；zh 全角括号 / en 半角）
		expect(zh.get("upload.stage.pct")).toBe("（{pct}%）");
		expect(en.get("upload.stage.pct")).toBe(" ({pct}%)");
		expect(appSrc).toContain('"upload.stage.pct": { zh: "（{pct}%）", en: " ({pct}%)" }');
		// 仅「正在上传 / 服务器接收 checkpoint」两个真实分母阶段并入标签；
		// 上传成功（done）与步 2 准备态绝不带百分比
		expect(appSrc).toContain("var PCT_IN_LABEL_KEYS = {");
		expect(appSrc).toContain('"upload.cos.stage.uploading": 1,');
		expect(appSrc).toContain('"upload.cos.stage.downloading": 1,');
		const pctKeys = appSrc.slice(appSrc.indexOf("var PCT_IN_LABEL_KEYS"),
			appSrc.indexOf("};", appSrc.indexOf("var PCT_IN_LABEL_KEYS")));
		expect(pctKeys).not.toContain("upload.stage.done");
		expect(pctKeys).not.toContain("upload.step.prepare");
		// 行渲染经 tt("upload.stage.pct") 组装（源码级）
		expect(appSrc).toContain('tt("upload.stage.pct", { pct: pct })');
	});

	it("普通进度行不暴露内部术语（COS/工作台接收/发布/收口/词表）", () => {
		const zh = dict("zh");
		const rowKeys = [
			"upload.cos.stage.waiting_space", "upload.cos.stage.uploading",
			"upload.cos.stage.awaiting_server", "upload.cos.stage.downloading",
			"upload.cos.stage.validating", "upload.cos.stage.processing",
			"upload.cos.stage.readiness", "upload.cos.stage.viewable",
			"upload.inline.stage.queued", "upload.inline.stage.notAccepted",
			"upload.inline.stage.settled", "upload.stage.done",
			"upload.step.prepare", "upload.step.converting",
		];
		const banned = [/腾讯云/, /COS/, /工作台接收/, /发布/, /收口/, /词表/];
		for (const k of rowKeys) {
			const v = zh.get(k) || "";
			for (const b of banned) {
				expect(b.test(v), `${k} 含内部术语 ${b}: ${v}`).toBe(false);
			}
		}
		// zip 逐项计数也改为工作台结果措辞（copy 审计第 3 项）
		expect(zh.get("upload.cos.items")).toBe("已加入工作台 {n}/{m} 项");
	});

	it("转换阶段映射到五步：running/finalizing → （格式转换中），probing/validating → 准备", () => {
		const I = IC();
		expect(I.PHASE_KEYS.running).toBe("upload.step.converting");
		expect(I.PHASE_KEYS.finalizing).toBe("upload.step.converting");
		expect(I.PHASE_KEYS.probing).toBe("upload.step.prepare");
		expect(I.PHASE_KEYS.validating).toBe("upload.step.prepare");
		expect(I.PHASE_KEYS.planned).toBe("upload.step.prepare");
		// 进度事件同映射（level/tile-row = 转换中；stage 复制 = 准备）
		expect((I.progressStage({ unit: "level", done: 1, total: 2 }) as { key: string }).key)
			.toBe("upload.step.converting");
		expect((I.progressStage({ unit: "stage", done: 1, total: 2 }) as { key: string }).key)
			.toBe("upload.step.prepare");
	});

	it("i18n zh/en 成对：本轮新增/改动键两语言都在", () => {
		const keys = [
			"upload.step.prepare", "upload.step.converting",
			"upload.inline.action.reupload", "imp.uploads.title",
			"fbk.privacy.hint", "fbk.privacy.details",
			"fr.sample.max", "upload.kfb.unavailable",
			"tools.privacy.brief", "tools.privacy.details",
			"tools.direct.hint", "tools.to.workbench",
			"tools.run.start", "upload.cos.err.convert_in_browser",
			"upload.inline.stage.notAccepted", "upload.inline.stage.settled",
		];
		const zh = dict("zh");
		const en = dict("en");
		for (const k of keys) {
			expect(zh.has(k), `zh 缺 ${k}`).toBe(true);
			expect(en.has(k), `en 缺 ${k}`).toBe(true);
			expect(zh.get(k)).not.toBe(en.get(k), `${k} zh/en 同文`);
		}
	});
});

// --------------------------------------------------------------------- //
// P2-3 + 回退语义：接线源码级断言
// --------------------------------------------------------------------- //
describe("P2-3 抽屉上传队列 + inline_convert 关闭回退（源码级）", () => {
	it("行容器在抽屉/侧栏之间移动（同一份 DOM，不建第二份列表）", () => {
		expect(appSrc).toContain("function syncUploadRowLocation()");
		expect(appSrc).toContain("function initUploadRowHostSync()");
		expect(appSrc).toContain("host.appendChild(list)");
		// 抽屉开合两处都接线
		expect(appSrc.match(/syncUploadRowLocation\(\);/g)!.length).toBeGreaterThanOrEqual(2);
	});

	it("后台任务空态包含本机转换与 COS 条目（不只查 /api/conversions）", () => {
		expect(appSrc).toContain("function localUploadTaskSummaries(");
		expect(appSrc).toContain("renderImportTasks(items, localUploadTaskSummaries())");
		// 空态只在服务器与本机都为空时出现
		const render = appSrc.slice(
			appSrc.indexOf("function renderImportTasks("),
			appSrc.indexOf("function startImportTaskPolling("));
		expect(render).toContain("if ((!items || !items.length) && !locals.length)");
	});

	it("inline_convert 关闭：需要转换的文件行内说明 + 重试/支持，零 window.open", () => {
		expect(appSrc).toContain("convertNeededUnavailable(file, file && file.name");
		expect(appSrc).toContain("upload.kfb.unavailable");
		expect(appSrc).not.toContain("window.open(");
		expect(appSrc).not.toContain("HANDOFF_MSG");
		// 直传在关态照常可用（uploadFileCos 直传状态机不经面板）
		expect(appSrc).toContain("function uploadFileCos(file, row, opts)");
	});
});
