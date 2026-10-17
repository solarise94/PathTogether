/**
 * 跨账号归属核对回归（P1 cross-account 2026-10-15）。
 *
 * 场景原样移植自 release-review 的真实浏览器复现脚本（cross-account.mjs，
 * 只读留档 /mnt/ssd2/tests/run-release-review-bxdgZu/）：A 页转换期间，另一
 * 标签页退出并登录 B；A 页仍用缓存的 A 身份通过前端归属校验，但 POST
 * /api/ingestions 带的是 B 的会话 cookie——修复前服务器按 B 建任务，上传
 * 完成、B 能读取，全程无确认。
 *
 * 本文件按仓库 vitest 框架改加载方式（断言不改）：
 *   - 真实 inline-convert.js（编排器）+ 真实 cos-uploader.js（传输引擎）+
 *     真实 slide-sniff.js（classic script，new Function 注入假 window）；
 *   - 「服务器」是 apiFetch 假实现，按**修复后的服务端合同**裁定：
 *     POST /api/ingestions 的 expected_account ≠ 当前会话账号 → 409
 *     account_mismatch、零建行（与 app.py 的核对同一语义）；b891b315 上
 *     引擎不带 expected_account → 假服务器照建任务 → 上传走完（行 'done'，
 *     正是真实复现的「B 能读取」）→ 断言红；修复后 409 → 行
 *     'account_mismatch' → 断言绿；
 *   - 「另一标签页换账号」= server.session 翻转（cookie 的唯一权威在
 *     服务器）；「/api/auth/info 重拉」= retryIdentity 镜像（app.js 的
 *     retryAuthInfoNow：更新本页缓存身份 + recheckOwnership）；
 *   - 面板→上传的粘合（uploadEntryArtifact / createEntryUpload）按
 *     static/app.js 同名函数镜像（真实 app.js 粘合由 e2e
 *     tests/e2e/upload-inline-convert.spec.ts 的同 Cookie 两页用例覆盖）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const inlineSrc = readFileSync(
	resolve(root, "static/upload/inline-convert.js"), "utf8");
const sniffSrc = readFileSync(
	resolve(root, "static/upload/slide-sniff.js"), "utf8");
const cosEngineSrc = readFileSync(
	resolve(root, "static/upload/cos-uploader.js"), "utf8");
// 工作台旧直传路径用例加载**真实 app.js**（loadApp harness）
const appSrc = readFileSync(resolve(root, "static/app.js"), "utf8");

const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 10) {
	for (let i = 0; i < n; i++) await tick();
}
async function until(proc: () => boolean, timeoutMs = 5000) {
	const deadline = Date.now() + timeoutMs;
	while (!proc()) {
		if (Date.now() > deadline) {
			throw new Error("until() 超时：" + String(proc()));
		}
		await flush(4);
	}
}

// --------------------------------------------------------------------- //
// 假 XHR（COS PUT 独立传输）：send 即 200 + ETag
// --------------------------------------------------------------------- //
class FakeXHR {
	static instances: FakeXHR[] = [];
	static onSend: ((x: FakeXHR) => void) | null = null;
	method = ""; url = ""; body: unknown = null; sent = false;
	status = 0;
	upload: Record<string, unknown> = {};
	setRequestHeader = vi.fn();
	getResponseHeader = vi.fn((_h: string) => null);
	open(method: string, url: string) { this.method = method; this.url = url; }
	send(body: unknown) {
		this.body = body; this.sent = true;
		if (FakeXHR.onSend) FakeXHR.onSend(this);
		else this.respond(200, '"etag-x"');
	}
	abort() { if (this.onabort) this.onabort(); }
	respond(status: number, etag: string | null = null) {
		this.status = status;
		this.getResponseHeader = vi.fn((h: string) =>
			h.toLowerCase() === "etag" ? etag : null);
		if (this.onload) this.onload();
	}
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;
	onabort: (() => void) | null = null;
	ontimeout: (() => void) | null = null;
	constructor() { FakeXHR.instances.push(this); }
}

function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
		headers: { get: () => null },
	} as unknown as Response;
}

// --------------------------------------------------------------------- //
// 假服务器：会话账号是唯一权威（「cookie」）；按修复后的创建合同裁定
// --------------------------------------------------------------------- //
const ARTIFACT_BYTES = 1024;   // 产物大小（服务器计划按 part_bytes=512 分两块）

	function fakeServer() {
	const s = {
		session: "acct-a",                       // 当前会话账号（cookie 权威）
		created: [] as Array<{ jobId: string; body: Record<string, unknown> }>,
		signs: 0,
		completed: [] as string[],
		viewable: new Set<string>(),
		seq: 0,
		authInfoCalls: 0,
	};
	const apiFetch = vi.fn((url: string, opts?: Record<string, unknown>) => {
		const u = String(url);
		const method = String((opts && opts.method) || "GET");
		if (u === "/api/auth/info") {
			s.authInfoCalls += 1;
			return Promise.resolve(resp({
				auth_enabled: true, user_id: s.session,
				username: s.session, role: "user",
			}));
		}
		if (u === "/api/ingestions" && method === "POST") {
			const body = JSON.parse(String(opts && opts.body)) as Record<string, unknown>;
			// 服务端跨账号核对（P1 修复合同，与 app.py 同语义）：带了且 ≠
			// 当前会话 → 409 account_mismatch，零建行（任何写入之前）。
			// 缺字段（旧客户端）保持现行为——b891b315 的引擎正是不带字段。
			if (body.expected_account !== undefined &&
				body.expected_account !== s.session) {
				return Promise.resolve(resp({
					error: "account_mismatch", code: "account_mismatch",
				}, 409));
			}
			s.seq += 1;
			const jobId = "inj_" + s.seq;
			s.created.push({ jobId, body });
			return Promise.resolve(resp({ job_id: jobId, stage: "preparing" }, 202));
		}
		const m = u.match(/^\/api\/ingestions\/(inj_\d+)(?:\/([a-z/-]+))?$/);
		if (m) {
			const jobId = m[1];
			const sub = m[2] || "";
			if (sub === "parts/sign" && method === "POST") {
				s.signs += 1;
				const nums = JSON.parse(String(opts && opts.body))
					.part_numbers as number[];
				return Promise.resolve(resp({
					job_id: jobId, upload_id: "up-1",
					transport: "presign_parts",
					urls: nums.map((n) => ({
						part_number: n, url: "http://cos.invalid/incoming/" + jobId + "/" + n,
					})),
				}));
			}
			if (sub === "upload-complete" && method === "POST") {
				s.completed.push(jobId);
				s.viewable.add(jobId);
				return Promise.resolve(resp({ job_id: jobId, stage: "validating" }, 202));
			}
			if (sub === "cancel" && method === "POST") {
				return Promise.resolve(resp({ job_id: jobId, stage: "terminal" }, 202));
			}
			if (!sub && method === "GET") {
				if (s.viewable.has(jobId)) {
					return Promise.resolve(resp({
						job_id: jobId, stage: "viewable", slide_id: "sld_" + jobId,
					}));
				}
				return Promise.resolve(resp({
					job_id: jobId, stage: "uploading",
					parts: [
						{ part_number: 1, length: ARTIFACT_BYTES / 2 },
						{ part_number: 2, length: ARTIFACT_BYTES / 2 },
					],
				}));
			}
		}
		return Promise.resolve(resp({}, 404));
	});
	return { state: s, apiFetch };
}

// --------------------------------------------------------------------- //
// 假 runner（有状态记录；getJob/setJobIntent/setJobUpload 真合并）+
// 真 engine.js 的注入子集（同 inline-convert.test.ts）
// --------------------------------------------------------------------- //
interface Rec {
	id: string; state: string;
	result: { outputBytes: number; format: string } | null;
	intent: Record<string, unknown> | null;
	upload: Record<string, unknown> | null;
	source: { name: string };
}

function fakeRunner() {
	const records = new Map<string, Rec>();
	// 产物视图挂起（cross-account.mjs 的 r.artifactView 包裹同款：
	// __artifactHeld / __releaseArtifact）
	let held = false;
	let release = () => {};
	const heldP = new Promise<void>((res) => { release = res; });
	const artifactFile = {
		name: "a.ome.tif", size: ARTIFACT_BYTES,
		slice(start: number, end: number) {
			const s = Math.max(0, start);
			const e = Math.min(end, ARTIFACT_BYTES);
			const len = Math.max(0, e - s);
			// OME-TIFF 头（II*\0 经典 TIFF）+ 零填充：嗅探/摘要/PUT 都读它
			const magic = [0x49, 0x49, 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00];
			return {
				size: len,
				slice(a: number, b: number) {
					const sub = Math.max(0, Math.min(b, len) - a);
					return { size: sub, arrayBuffer: async () => new ArrayBuffer(sub) };
				},
				arrayBuffer: async () => {
					const buf = new ArrayBuffer(len);
					const u8 = new Uint8Array(buf);
					for (let i = 0; i < len; i++) {
						u8[i] = (s + i) < magic.length ? magic[s + i] : 0;
					}
					return buf;
				},
			};
		},
	};
	const runner = {
		records,
		artifactHeld: () => held,
		releaseArtifact: () => release(),
		artifactCalls: 0,
		setJobIntent(id: string, patch: Record<string, unknown>) {
			const rec = records.get(id);
			if (!rec) return Promise.resolve();
			rec.intent = Object.assign({}, rec.intent || {}, patch);
			return Promise.resolve();
		},
		setJobUpload(id: string, patch: Record<string, unknown>) {
			const rec = records.get(id);
			if (!rec) return Promise.resolve();
			// app.js/引擎的 setJobUpload 是部分更新（null 值也语义化写入）
			rec.upload = Object.assign({}, rec.upload || {}, patch);
			return Promise.resolve();
		},
		getJob(id: string) {
			return Promise.resolve(records.get(id) || null);
		},
		listJobs() {
			return Promise.resolve(Array.from(records.values()));
		},
		discardJob() { return Promise.resolve(); },
		probe() {
			records.set("job1", {
				id: "job1", state: "ready",
				result: {
					outputBytes: ARTIFACT_BYTES,
					format: "ome-bigtiff-subifd-rgb-jpeg-pyramid",
				},
				intent: null, upload: null, source: { name: "a.kfb" },
			});
			return Promise.resolve({
				jobId: "job1",
				probe: { document: { modality: "brightfield" } },
				identity: { size: 10 },
			});
		},
		startJob(_file: unknown, opts: { jobId?: string }) {
			return Promise.resolve({
				jobId: opts.jobId,
				done: Promise.resolve({
					ok: true,
					result: { output_bytes: ARTIFACT_BYTES,
						format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
					validation: {},
				}),
			});
		},
		artifactView(_id: string) {
			runner.artifactCalls += 1;
			if (runner.artifactCalls > 1) return Promise.resolve(artifactFile);
			// 首次调用挂起：复现脚本在「转换产物视图」这一步钉住页面 A，
			// 让换账号落在转换完成之后、上传创建之前
			held = true;
			return heldP.then(() => artifactFile);
		},
	};
	return runner;
}

function engineStub() {
	return {
		isTiffHeader: (h: Uint8Array) => h[0] === 0x49 && h[1] === 0x49,
		isRasterHeader: (h: Uint8Array) => h[0] === 0xFF && h[1] === 0xD8,
		magicSupported: () => true,
		sniffTiffSlideCapability: () =>
			Promise.resolve({ supported: true, modality: "brightfield" }),
		sniffRasterCapability: () =>
			Promise.resolve({ supported: true, modality: "brightfield" }),
		OUTPUT_PROFILES: { BF_OME: "bf-ome", FL_OME: "fl-ome", BF_CLASSIC: "bf-classic" },
		ENCODING_PROFILES: { PRESERVE: "preserve-source-v1" },
		errCode: (e: unknown) =>
			((e as { error?: { code?: string } })?.error?.code) || null,
		outputFileName: (n: string) =>
			String(n).replace(/\.(kfb|kfbf|svs|bmp|jpg)$/i, "") + ".ome.tif",
		uploadLockName: (id: string) => "slide-transform:upload:" + id,
	};
}

const CFG = {
	max_size_bytes: 1_000_000, part_bytes: 512,
	formats: ["tif", "tiff", "ome.tif", "kfb", "kfbf"],
	url_ttl_seconds: 600, max_concurrent_parts: 2, sign_batch_max_parts: 2,
};

/** KFB 夹具（magic F1 01 EE EE "KFB\0"，同 inline-convert.test.ts） */
function kfbFile() {
	const bytes = Uint8Array.from(
		[0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
	return {
		name: "a.kfb", size: bytes.length,
		slice(start: number, end: number) {
			const s = Math.max(0, start);
			const e = Math.min(end, bytes.length);
			const sub = bytes.slice(s, e);
			return { arrayBuffer: async () => sub.buffer.slice(sub.byteOffset, sub.byteOffset + sub.byteLength) };
		},
	};
}

// --------------------------------------------------------------------- //
// 面板→上传粘合镜像（static/app.js uploadEntryArtifact / createEntryUpload
// 同语义：归属复核 + expected_account 按记录绑定账号）
// --------------------------------------------------------------------- //
function isAccountMismatchError(err: unknown) {
	const e = err as { status?: number; data?: { code?: string; error?: string } };
	return !!(e && e.status === 409 && e.data &&
		(e.data.code === "account_mismatch" || e.data.error === "account_mismatch"));
}

function boundAccount(job: Rec | null): string | null {
	const up = job && job.upload;
	const it = job && job.intent;
	if (up && up.ingestionId && up.state !== "published" &&
		up.state !== "failed" && up.state !== "cancelled" &&
		typeof up.account === "string" && up.account) {
		return String(up.account);
	}
	if (it && it.state === "pending" && typeof it.account === "string" &&
		it.account) {
		return String(it.account);
	}
	return null;
}

interface Page {
	identity: string;                       // 本页缓存身份（currentUserId 镜像）
}

function buildScenario() {
	const w: Record<string, unknown> = {};
	new Function("window", inlineSrc)(w);
	new Function("window", sniffSrc)(w);
	new Function("window", cosEngineSrc)(w);
	const IC = (w as { HP_INLINE_CONVERT: { createOrchestrator: (d: Record<string, unknown>) => Orch } }).HP_INLINE_CONVERT;
	const COS = (w as { HP_COS_UPLOAD: {
		createUpload: (o: Record<string, unknown>) => {
			done: Promise<{ ok: boolean; body: Record<string, unknown> }>;
			cancel: () => void;
		};
	} }).HP_COS_UPLOAD;
	vi.stubGlobal("XMLHttpRequest", FakeXHR);
	FakeXHR.instances = [];
	FakeXHR.onSend = null;

	const server = fakeServer();
	const runner = fakeRunner();
	const page: Page = { identity: "acct-a" };   // A 页：登录时的缓存身份
	const orch = IC.createOrchestrator({
		runner: runner,
		engine: engineStub(),
		sniff: (w as { HP_SLIDE_SNIFF: unknown }).HP_SLIDE_SNIFF,
		cos: (w as { HP_COS_UPLOAD: unknown }).HP_COS_UPLOAD,
		caps: {
			inlineConvert: {
				enabled: true, url: "/tools/slides",
				viewableFormats: ["ome-bigtiff-subifd-rgb-jpeg-pyramid"],
			},
			directUploadFormats: ["tif", "tiff", "ome.tif"],
			cosConfig: CFG,
		},
		ui: {
			row() {}, setStage() {}, markError() {}, buttons() {},
			toast() {}, refresh() {}, remove() {},
		},
		account: () => page.identity,
		// 身份信号已落定（A 页加载时 auth/info 已返回）
		identityReady: () => Promise.resolve(),
		authEnabled: () => true,
		t: (k: string) => k,
		typeName: (d: unknown) => String(d || ""),
		target: () => ({ pid: "p1" }),
		onArtifact: (entry: Entry, artifact: unknown) =>
			uploadEntryArtifactMirror(page, server, runner, entry, artifact, COS),
		cancelArtifact: (entry: Entry) => {
			const u = entry._upload as { cancel?: () => void } | null;
			if (u && u.cancel) u.cancel();
		},
		apiFetch: server.apiFetch,
		apiFetchNoRedirect: server.apiFetch,
		setTimer: (fn: () => void, ms: number) => setTimeout(fn, ms),
		// retryAuthInfoNow 镜像：重拉 auth/info（假服务器返回**会话**身份）
		// → applyAuthInfo 更新本页缓存 → markAuthInfoSettled → recheckOwnership
		retryIdentity: () => {
			void server.apiFetch("/api/auth/info").then((r: Response) =>
				r.json() as Promise<{ user_id?: string }>).then((info) => {
				page.identity = String(info.user_id || "");
				void orch.recheckOwnership();
			});
		},
	});
	return { orch, runner, server, page };
}

interface Entry {
	id: string; state: string; jobId: string | null;
	uploadJobId: string | null; cancelled?: boolean;
	accountOwner?: string | null; error?: { code?: string } | null;
	_upload?: unknown; resumeKind?: string; [k: string]: unknown;
}
interface Orch {
	enqueue(file: unknown, opts?: Record<string, unknown>): Entry;
	act(id: string, action: string): Entry | null;
	recheckOwnership(): Promise<Entry[]>;
	list(): Entry[];
}

/// static/app.js uploadEntryArtifact + createEntryUpload 的镜像（真实 app.js
/// 粘合由 e2e 覆盖；这里保持同一判定序列：取消边界 → 归属复核 → 按记录
/// 绑定账号建引擎 → 409 附带 expectedAccount）。
function uploadEntryArtifactMirror(
	page: Page, server: ReturnType<typeof fakeServer>,
	runner: ReturnType<typeof fakeRunner>, entry: Entry, artifact: unknown,
	cos: { createUpload: (o: Record<string, unknown>) => {
		done: Promise<{ ok: boolean; body: Record<string, unknown> }>;
		cancel: () => void;
	} },
) {
	if (entry.cancelled) return Promise.resolve({ cancelled: true });
	if (!artifact) {
		// 直传条目（无本机产物）：app.js uploadFileCos 镜像——入口**冻结**
		// 页面身份（frozenAccount = 引擎 account = expected_account 来源）
		return uploadFileCosMirror(page, server, entry, cos);
	}
	return Promise.resolve(runner.getJob(entry.jobId as string)).then(
		(job: Rec | null) => {
			if (entry.cancelled) return { cancelled: true };
			const me = page.identity;
			const owner = boundAccount(job);
			if (owner !== null && owner !== me) {
				return Promise.reject({
					__account: true, owner,
					data: { code: "account_mismatch" },
				});
			}
			return createEntryUploadMirror(page, server, runner, entry,
				artifact, job, cos);
		});
}

/// app.js uploadFileCos 镜像（面板直传条目走的同一实现）：账号在入口冻结、
/// 记录按账号绑定、409 附带 expectedAccount。
function uploadFileCosMirror(
	page: Page, server: ReturnType<typeof fakeServer>, entry: Entry,
	cos: { createUpload: (o: Record<string, unknown>) => {
		done: Promise<{ ok: boolean; body: Record<string, unknown> }>;
		cancel: () => void;
	} },
) {
	const file = entry.file as {
		name: string; size: number; slice: (s: number, e: number) => unknown;
	};
	const frozenAccount = page.identity;
	const records = new Map<string, Record<string, unknown>>();
	const upload = cos.createUpload({
		source: {
			name: file.name, size: file.size,
			slice: (s: number, e: number) => file.slice(s, e),
		},
		apiFetch: server.apiFetch,
		heartbeatFetch: server.apiFetch,
		config: CFG,
		createBody: entry.directClass
			? { direct_class: entry.directClass } : null,
		storage: {
			save: (rec: Record<string, unknown>) => {
				if (!rec || !rec.job_id) return undefined;
				records.set(String(rec.job_id), rec);
				return undefined;
			},
			complete: (id: string) => { records.delete(id); },
			remove: (id: string) => { records.delete(id); },
			readConfirmed: (id: string) => {
				const r = records.get(id);
				return (r && (r.confirmed as number[])) || [];
			},
			readRecord: (id: string) => records.get(id) || null,
		},
		account: frozenAccount,
		resumeJobId: null,
		skipConfirm: false,
		confirmResume: () => true,
		retryCompleteOnNetworkError: false,
		useResumeEndpoint: false,
		onEvent: (ev: { type: string; jobId?: string }) => {
			if (ev.type === "created") entry.uploadJobId = ev.jobId || null;
		},
	});
	entry._upload = upload;
	return upload.done.then((r) => r, (err: unknown) => {
		if (isAccountMismatchError(err)) {
			(err as { expectedAccount?: string }).expectedAccount = frozenAccount;
		}
		throw err;
	});
}

function createEntryUploadMirror(
	page: Page, server: ReturnType<typeof fakeServer>,
	runner: ReturnType<typeof fakeRunner>, entry: Entry,
	artifact: unknown, job: Rec | null,
	cos: { createUpload: (o: Record<string, unknown>) => {
		done: Promise<{ ok: boolean; body: Record<string, unknown> }>;
		cancel: () => void;
	} },
) {
	// expected_account = 记录绑定账号（intent.account / upload.account），
	// 无绑定才回本页缓存身份——与修复后的 app.js 同口径
	const acct = boundAccount(job) ?? page.identity;
	const src = artifact as {
		name: string; size: number;
		slice: (s: number, e: number) => unknown;
	};
	const upload = cos.createUpload({
		source: {
			name: entry.artifactName || src.name, size: src.size,
			slice: (s: number, e: number) => src.slice(s, e),
		},
		apiFetch: server.apiFetch,
		heartbeatFetch: server.apiFetch,
		config: CFG,
		createBody: null,
		storage: {
			save: (rec: Record<string, unknown>) => {
				if (!rec || !rec.job_id) return undefined;
				return runner.setJobUpload(entry.jobId as string, {
					ingestionId: rec.job_id, filename: src.name, size: src.size,
					state: "uploading", confirmedParts: rec.confirmed || [],
					partDigests: rec.digests || {},
					digestScheme: rec.digest_scheme || "",
					account: acct,
				});
			},
			complete: (id: string, outcome: Record<string, unknown>) =>
				runner.setJobUpload(entry.jobId as string, {
					ingestionId: id,
					state: outcome && outcome.succeeded ? "published" : "failed",
					slideId: (outcome && outcome.slide_id) || null,
				}),
			remove: (id: string) =>
				runner.setJobUpload(entry.jobId as string, {
					ingestionId: id, state: "cancelled",
				}),
			readConfirmed: (id: string) =>
				runner.getJob(entry.jobId as string).then((j) => {
					const up = j && j.upload;
					return (up && up.ingestionId === id)
						? (up.confirmedParts || []) : [];
				}),
			readRecord: (id: string) =>
				runner.getJob(entry.jobId as string).then((j) => {
					const up = j && j.upload;
					if (!up || up.ingestionId !== id) return null;
					return {
						confirmed: up.confirmedParts || [],
						digests: up.partDigests || null,
						digest_scheme: up.digestScheme || "",
						account: typeof up.account === "string" ? up.account : "",
					};
				}),
		},
		account: acct,
		resumeJobId: resumeIdFromRecord(job),
		skipConfirm: true,
		confirmResume: () => true,
		retryCompleteOnNetworkError: false,
		useResumeEndpoint: false,
		onEvent: (ev: { type: string; jobId?: string }) => {
			if (ev.type === "created") entry.uploadJobId = ev.jobId || null;
		},
	});
	entry._upload = upload;
	return upload.done.then((r) => r, (err: unknown) => {
		if (isAccountMismatchError(err)) {
			(err as { expectedAccount?: string }).expectedAccount = acct;
		}
		throw err;
	});
}

function resumeIdFromRecord(job: Rec | null): string | null {
	const up = job && job.upload;
	if (!up || !up.ingestionId) return null;
	if (up.state === "published" || up.state === "failed" ||
		up.state === "cancelled") return null;
	return String(up.ingestionId);
}

afterEach(() => {
	vi.unstubAllGlobals();
	FakeXHR.instances = [];
	FakeXHR.onSend = null;
});

// --------------------------------------------------------------------- //
// 工作台旧直传路径（真实 app.js loadApp harness，同 workbench-direct-class
// /cos-upload.test.ts）：uploadFile → uploadFileCos 的创建体带
// expected_account = 上传开始时冻结的页面身份；服务端 409 → 行级可读文案，
// 零静默建任务。
// --------------------------------------------------------------------- //

/** 元素 stub（同 workbench-direct-class.test.ts 的最小面） */
function el(tag?: string) {
	const node: Record<string, unknown> = {
		tagName: (tag || "div").toUpperCase(),
		children: [] as unknown[],
		className: "",
		textContent: "",
		hidden: false,
		style: {},
		value: "",
		title: "",
		type: "",
		_listeners: {} as Record<string, Array<() => void>>,
		appendChild(child: unknown) {
			(node.children as unknown[]).push(child);
			return child;
		},
		removeChild(child: unknown) {
			const arr = node.children as unknown[];
			const i = arr.indexOf(child);
			if (i >= 0) arr.splice(i, 1);
			return child;
		},
		insertBefore(c: unknown, _ref: unknown) {
			(node.children as unknown[]).push(c);
			return c;
		},
		addEventListener(type: string, fn: () => void) {
			(node._listeners as Record<string, Array<() => void>>)[type] = [
				...((node._listeners as Record<string, Array<() => void>>)[type] || []),
				fn,
			];
		},
		removeEventListener() {},
		setAttribute() {},
		getAttribute() { return null; },
		focus() {},
		click() {
			((node._listeners as Record<string, Array<() => void>>).click || [])
				.forEach((f) => f());
		},
	};
	return node;
}

function fakeLocalStorage() {
	const store = new Map<string, string>();
	return {
		getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
		setItem: (k: string, v: string) => void store.set(k, v),
		removeItem: (k: string) => void store.delete(k),
	};
}

const WB_CAPS = {
	available: true,
	manual_only: true,
	inline_convert: {
		enabled: false, url: "/tools/slides",
		viewable_formats: ["classic-bigtiff-jpeg-pyramid"],
	},
	formats: ["tif", "tiff", "ome.tif", "svs", "ndpi"],
	max_size_bytes: 1_000_000,
	part_bytes: 512,
	url_ttl_seconds: 600,
	max_concurrent_parts: 2,
	sign_batch_max_parts: 2,
	policy_version: "v1-manual",
};

function loadWorkbench(server: ReturnType<typeof fakeServer>) {
	const storage = fakeLocalStorage();
	const els: Record<string, ReturnType<typeof el>> = {};
	const toasts: string[] = [];
	els["toast-container"] = el();
	(els["toast-container"] as { appendChild: (c: { textContent?: string }) => unknown })
		.appendChild = (child: { textContent?: string }) => {
			toasts.push(String(child && child.textContent));
			return child;
		};
	const container = el();
	els["upload-progress-list"] = container;
	const loc = { href: "http://local/", pathname: "/" };
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				(vars && Object.keys(vars).length)
					? `${k}:${Object.values(vars).join(",")}` : k,
			getLang: () => "zh",
		},
		fetch: server.apiFetch,
		location: loc,
		confirm: vi.fn(() => true),
		open: vi.fn(() => null),
		addEventListener: vi.fn(),
	};
	w.HP_APP_BOOTSTRAP = { mode: "official", capabilities: { cos_upload: WB_CAPS } };
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		visibilityState: "visible",
		getElementById(id: string) {
			if (!els[id]) els[id] = el();
			return els[id];
		},
		createElement(tag: string) { return el(tag); },
		addEventListener() {},
		querySelector() { return el(); },
		querySelectorAll() { return []; },
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: unknown }).fetch = server.apiFetch;
	(globalThis as { location: typeof loc }).location = loc;
	(globalThis as { localStorage: typeof storage }).localStorage = storage;
	vi.stubGlobal("localStorage", storage);
	new Function("window", "document", "fetch", "location", cosEngineSrc)(
		w, doc, server.apiFetch, loc);
	new Function("window", sniffSrc)(w);
	new Function("window", "document", "fetch", "location", appSrc)(
		w, doc, server.apiFetch, loc);
	return {
		up: w.HP_UPLOAD as {
			uploadFile: (f: unknown, opts?: Record<string, unknown>) => void;
		},
		auth: w.HP_AUTH as {
			applyAuthInfo: (info: Record<string, unknown>) => void;
		},
		container,
		toasts,
	};
}

describe("工作台旧直传路径（uploadFileCos）携带 expected_account", () => {
	it("创建体带 expected_account = 上传开始时冻结的页面身份；服务端 409 → 可读文案，零静默建任务", async () => {
		const server = fakeServer();
		server.state.session = "acct-b";   // 会话已是 B（另一标签页换账号）
		const h = loadWorkbench(server);
		// A 页缓存身份 = acct-a（applyAuthInfo 后 currentUserId 冻结于此）
		h.auth.applyAuthInfo({
			auth_enabled: true, user_id: "acct-a",
			role: "user", username: "acct-a@x",
		});
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const desc = new TextEncoder().encode(ome);
		const bytes = new Uint8Array(512);
		bytes.set([0x49, 0x49, 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00], 0);
		bytes.set(desc, 64);
		h.up.uploadFile({
			name: "scan.ome.tif",
			size: bytes.length,
			slice(start: number, end: number) {
				const s = Math.max(0, start);
				const e = Math.min(end, bytes.length);
				const sub = bytes.slice(s, e);
				return {
					size: sub.length,
					slice(a: number, b: number) {
						const s2 = Math.max(0, a);
						const e2 = Math.min(b, sub.length);
						const s3 = sub.slice(s2, e2);
						return {
							size: s3.length,
							arrayBuffer: async () => s3.buffer.slice(
								s3.byteOffset, s3.byteOffset + s3.byteLength),
						};
					},
					arrayBuffer: async () => sub.buffer.slice(
						sub.byteOffset, sub.byteOffset + sub.byteLength),
				};
			},
		});
		await flush(12);
		// 创建体携带 expected_account = 冻结的 A 身份（本用例的核心钉子）
		const created = (server.apiFetch.mock.calls as unknown as Array<
			[string, Record<string, unknown>?]>).filter(([u, o]) =>
			String(u) === "/api/ingestions" &&
			String((o && o.method) || "GET") === "POST");
		expect(created).toHaveLength(1);
		const body = JSON.parse(String(created[0][1] && created[0][1].body));
		expect(body.expected_account).toBe("acct-a");
		// 服务端核对拒绝：零建行（假服务器按修复后合同裁定）
		expect(server.state.created).toHaveLength(0);
		// 行级失败给出可读文案（不再是裸机器码），点名登录身份变化
		expect(h.toasts.some((m) => m.includes("登录账号已变化"))).toBe(true);
	}, 30_000);
});

// --------------------------------------------------------------------- //
// 场景移植（断言与 cross-account.mjs 一致，仅按仓库框架改加载方式）
// --------------------------------------------------------------------- //
describe("跨账号归属核对（cross-account.mjs 场景移植）", () => {
	it("A 页转换期间另一标签页换账号：上传创建被服务器 409 拒绝，行收口 account_mismatch（改绑/删除），零静默上传", async () => {
		const { orch, runner, server } = buildScenario();

		// A 页登录 A，拖入 KFB → 面板内转换（bindIntent 写 intent.account）
		const e = orch.enqueue(kfbFile(), { assocTarget: null });
		expect(e).toBeTruthy();
		await until(() => runner.artifactHeld(), 5000);

		// 原脚本断言 1：挂起时记录的 intent.account 是 A
		const jobBefore = await runner.getJob(e!.jobId as string);
		expect(jobBefore!.intent!.account).toBe("acct-a");

		// 另一标签页：退出 A 并登录 B（会话 cookie 翻转——服务器是唯一权威）
		server.state.session = "acct-b";
		// 原脚本断言 2：B 的身份与 A 不同
		const accountB = server.state.session;
		expect(accountB).not.toBe("acct-a");

		// 释放产物视图 → A 页以缓存身份继续上传：POST /api/ingestions 带
		// expected_account=acct-a，会话已是 acct-b → 服务器 409 account_mismatch
		runner.releaseArtifact();
		await until(() =>
			["done", "upload_failed", "account_mismatch"].includes(e!.state),
			10000);
		// 原脚本断言 3（核心）：账号变化必须经确认才能上传——行收口
		// account_mismatch，绝不静默完成
		expect(e!.state).toBe("account_mismatch");

		// 服务端零建行：409 发生在任何写入之前（原脚本的「B 能读取」反过来
		// 即「无任务可读」）
		expect(server.state.created).toHaveLength(0);
		expect(server.state.signs).toBe(0);
		expect(e!.uploadJobId).toBeNull();
		expect(FakeXHR.instances).toHaveLength(0);   // 零分块 PUT
		// 行注明归属账号（显式改绑/删除两条出路由行态提供）
		expect(e!.accountOwner).toBe("acct-a");
		expect(e!.error && e!.error.code).toBe("account_mismatch");

		// 409 触发身份重拉（auth/info 返回会话身份 B）
		expect(server.state.authInfoCalls).toBeGreaterThanOrEqual(1);
	}, 30_000);

	it("改绑确认后以新账号为 expected_account 重新发起：上传走完且任务建在当前会话账号名下", async () => {
		const { orch, runner, server } = buildScenario();
		const e = orch.enqueue(kfbFile(), { assocTarget: null });
		await until(() => runner.artifactHeld(), 5000);
		server.state.session = "acct-b";
		runner.releaseArtifact();
		await until(() => e!.state === "account_mismatch", 10000);
		expect(server.state.created).toHaveLength(0);

		// 用户点「改用当前账号继续」（显式确认）→ 记录改绑当前账号 →
		// 重新发起：expected_account=acct-b 与会话一致 → 建任务 → 走完
		await orch.act(e!.id, "rebind");
		await until(() => e!.state === "done", 15000);
		expect(server.state.created).toHaveLength(1);
		expect(server.state.created[0].body.expected_account).toBe("acct-b");
		expect(e!.uploadJobId).toBe(server.state.created[0].jobId);
		// 记录归属也已改绑（upload.account = 当前账号）
		const job = await runner.getJob(e!.jobId as string);
		expect(job!.upload && job!.upload.account).toBe("acct-b");
		expect(job!.intent && job!.intent.account).toBe("acct-b");
	}, 30_000);

	it("面板直传条目（无 jobId）：创建被 409 拒绝后同样收口 account_mismatch，行注归属为 expected_account", async () => {
		const { orch, server } = buildScenario();
		// 会话先翻转到 B（直传没有转换窗口，入队即创建；「转换中途换账号」
		// 的窗口由上面两个用例覆盖）——A 页缓存身份仍是 acct-a，创建请求带
		// expected_account=acct-a 而会话已是 acct-b → 服务器 409
		server.state.session = "acct-b";
		// 直传类夹具（.ome.tif 经典 TIFF → routeFor 'upload'，无转换记录）
		const direct = orch.enqueue({
			name: "plain.ome.tif",
			size: ARTIFACT_BYTES,
			slice(start: number, end: number) {
				const s = Math.max(0, start);
				const e = Math.min(end, ARTIFACT_BYTES);
				const magic = [0x49, 0x49, 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00];
				const len = Math.max(0, e - s);
				return {
					arrayBuffer: async () => {
						const buf = new ArrayBuffer(len);
						const u8 = new Uint8Array(buf);
						for (let i = 0; i < len; i++) {
							u8[i] = (s + i) < magic.length ? magic[s + i] : 0;
						}
						return buf;
					},
				};
			},
		}, { assocTarget: null });
		await until(() => direct!.state === "account_mismatch", 10000);
		expect(direct!.state).toBe("account_mismatch");
		// 零建行（409 在任何写入之前）；行注明归属账号 = expected_account
		expect(server.state.created).toHaveLength(0);
		expect(direct!.accountOwner).toBe("acct-a");
		// 409 触发身份重拉（auth/info 返回会话身份 B）
		expect(server.state.authInfoCalls).toBeGreaterThanOrEqual(1);
	}, 30_000);
});
