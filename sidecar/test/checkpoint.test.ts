/**
 * Phase 2a tests: context checkpoint data structures, atomic CAS commit,
 * canonical serialization, lazy generation-1 materialization, and staleness.
 *
 * See docs/ai-context-cache-visual-workspace-upgrade.md §3.2/§5.3/§10/§13.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { SessionStore, assignMessageSeqs, type SessionData, type PersistedAgentMessage } from "../src/session-store.js";
import {
	REQUEST_SCHEMA_VERSION,
	canonicalSerialize,
	checkpointStale,
	computeSystemPromptVersion,
	computeToolSchemaHash,
	ensureCheckpoint,
	generationMatches,
	stablePrefixHash,
	type CheckpointEnv,
	type ContextCheckpoint,
} from "../src/checkpoint.js";

// Per-test temp directory tree.
let rootTmp = "";
beforeAll(async () => {
	rootTmp = await fs.mkdtemp(join(tmpdir(), "svs-checkpoint-"));
});
afterAll(async () => {
	await fs.rm(rootTmp, { recursive: true, force: true });
});

let dirCounter = 0;
async function newStoreDir(): Promise<string> {
	const p = join(rootTmp, `d${dirCounter++}`);
	await fs.mkdir(p, { recursive: true });
	return p;
}

const SLIDE = "slide-checkpoint.svs";
const FP = "fp-abcdef0123";

/** Build a minimal SessionData with N messages and no checkpoint (legacy). */
function legacySession(id: string, n: number): SessionData {
	const store = new SessionStore({ sessionsDir: "/tmp/unused" });
	const d = store.emptySession(SLIDE, "main", id);
	d.slide = SLIDE;
	const msgs: PersistedAgentMessage[] = [];
	for (let i = 0; i < n; i++) {
		msgs.push({
			role: "user",
			content: `legacy msg ${i}`,
			timestamp: Date.now(),
		} as PersistedAgentMessage);
	}
	d.messages = msgs;
	// Simulate legacy: strip next_message_seq so migration runs.
	delete (d as SessionData & { next_message_seq?: number }).next_message_seq;
	// Strip any _context_meta the emptySession path might not have added anyway.
	return d;
}

// =========================================================================== //
// Canonical serialization (§10)
// =========================================================================== //
describe("canonicalSerialize (§10)", () => {
	it("sorts object keys recursively (dictionary order)", () => {
		const a = canonicalSerialize({ b: 1, a: 2, c: { z: 1, a: 0 } });
		const b = canonicalSerialize({ a: 2, b: 1, c: { a: 0, z: 1 } });
		expect(a).toBe(b);
		// Keys appear in sorted order.
		expect(a).toBe('{"a":2,"b":1,"c":{"a":0,"z":1}}');
	});

	it("preserves array element order (NOT sorted)", () => {
		const a = canonicalSerialize([3, 1, 2]);
		expect(a).toBe("[3,1,2]");
		// Different order → different output.
		expect(canonicalSerialize([1, 2, 3])).not.toBe(a);
	});

	it("omits undefined values (object keys)", () => {
		const a = canonicalSerialize({ a: 1, b: undefined, c: 3 });
		expect(a).toBe('{"a":1,"c":3}');
	});

	it("converts undefined array slots to null (JSON.stringify parity)", () => {
		expect(canonicalSerialize([1, undefined, 3])).toBe("[1,null,3]");
	});

	it("rejects NaN", () => {
		expect(() => canonicalSerialize({ x: Number.NaN })).toThrow(/non-finite/i);
	});

	it("rejects Infinity and -Infinity", () => {
		expect(() => canonicalSerialize(Number.POSITIVE_INFINITY)).toThrow(/non-finite/i);
		expect(() => canonicalSerialize(Number.NEGATIVE_INFINITY)).toThrow(/non-finite/i);
	});

	it("serializes -0 as 0 (JSON.stringify parity)", () => {
		expect(canonicalSerialize(-0)).toBe("0");
		expect(canonicalSerialize(0)).toBe("0");
	});

	it("is deterministic for nested mixed structures", () => {
		const obj1 = { tools: [{ name: "b", args: { y: 1, x: 2 } }, { name: "a", args: {} }], prompt: "hi" };
		const obj2 = { prompt: "hi", tools: [{ name: "b", args: { x: 2, y: 1 } }, { name: "a", args: {} }] };
		expect(canonicalSerialize(obj1)).toBe(canonicalSerialize(obj2));
	});

	it("handles null, booleans, strings, numbers", () => {
		expect(canonicalSerialize(null)).toBe("null");
		expect(canonicalSerialize(true)).toBe("true");
		expect(canonicalSerialize(false)).toBe("false");
		expect(canonicalSerialize("héllo")).toBe('"héllo"');
		expect(canonicalSerialize(42)).toBe("42");
	});

	it("uses toJSON for exotic objects (e.g. Date)", () => {
		const d = new Date(0);
		// Date.toJSON() returns the ISO string; canonicalSerialize should use it.
		const out = canonicalSerialize({ ts: d });
		expect(out).toContain('"1970-01-01T00:00:00.000Z"');
	});
});

describe("stablePrefixHash (§10)", () => {
	it("is stable for key-order-independent inputs", () => {
		const a = stablePrefixHash({ b: 2, a: 1 });
		const b = stablePrefixHash({ a: 1, b: 2 });
		expect(a).toBe(b);
		expect(a).toMatch(/^[0-9a-f]{64}$/); // sha256 hex
	});

	it("changes when content changes", () => {
		const a = stablePrefixHash({ summary: "v1" });
		const b = stablePrefixHash({ summary: "v2" });
		expect(a).not.toBe(b);
	});
});

// =========================================================================== //
// Version derivation (§10)
// =========================================================================== //
describe("computeSystemPromptVersion / computeToolSchemaHash (§10)", () => {
	it("computeSystemPromptVersion is stable for the same prompt", () => {
		const v1 = computeSystemPromptVersion("you are an assistant");
		const v2 = computeSystemPromptVersion("you are an assistant");
		expect(v1).toBe(v2);
		expect(v1).toMatch(/^[0-9a-f]{64}$/);
	});

	it("computeSystemPromptVersion changes when the prompt changes", () => {
		expect(computeSystemPromptVersion("a")).not.toBe(computeSystemPromptVersion("b"));
	});

	it("computeToolSchemaHash is stable regardless of tool order", () => {
		const toolA = { name: "a", label: "A", description: "desc a", parameters: { type: "object" } };
		const toolB = { name: "b", label: "B", description: "desc b", parameters: { type: "object" } };
		const h1 = computeToolSchemaHash([toolA, toolB] as never);
		const h2 = computeToolSchemaHash([toolB, toolA] as never);
		expect(h1).toBe(h2);
		expect(h1).toMatch(/^[0-9a-f]{64}$/);
	});

	it("computeToolSchemaHash changes when a tool is added/removed/renamed", () => {
		const toolA = { name: "a", label: "A", description: "d", parameters: { type: "object" } };
		const toolB = { name: "b", label: "B", description: "d", parameters: { type: "object" } };
		const one = computeToolSchemaHash([toolA] as never);
		const two = computeToolSchemaHash([toolA, toolB] as never);
		expect(one).not.toBe(two);
		// Rename changes the hash.
		const toolARenamed = { ...toolA, name: "a2" };
		expect(computeToolSchemaHash([toolARenamed] as never)).not.toBe(one);
	});
});

// =========================================================================== //
// generationMatches (§5.3)
// =========================================================================== //
describe("generationMatches (§5.3)", () => {
	it("undefined === undefined is the first-commit semantic", () => {
		expect(generationMatches(undefined, undefined)).toBe(true);
	});
	it("matches equal concrete generations", () => {
		expect(generationMatches(1, 1)).toBe(true);
		expect(generationMatches(2, 2)).toBe(true);
	});
	it("rejects mismatched generations", () => {
		expect(generationMatches(1, 2)).toBe(false);
		expect(generationMatches(2, 1)).toBe(false);
		expect(generationMatches(undefined, 1)).toBe(false);
		expect(generationMatches(1, undefined)).toBe(false);
	});
});

// =========================================================================== //
// commitCheckpoint CAS (§5.3)
// =========================================================================== //
describe("SessionStore.commitCheckpoint (§5.3 CAS)", () => {
	it("commits a first generation when expectedGeneration is undefined", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		const cp: ContextCheckpoint = {
			version: 1,
			generation: 1,
			created_at: Date.now(),
			slide_fingerprint: FP,
			through_message_seq: 0,
			summary: "g1",
			annotation_index: "",
			overview_derivative: null,
			system_prompt_version: "spv1",
			tool_schema_hash: "tsh1",
			request_schema_version: REQUEST_SCHEMA_VERSION,
			stable_prefix_hash: "sph1",
		};
		const res = await store.commitCheckpoint(s.id, undefined, FP, (d) => {
			d.context_checkpoint = cp;
		});
		expect(res.ok).toBe(true);
		if (res.ok) {
			expect(res.data.context_checkpoint).toEqual(cp);
		}
		// Persisted.
		const back = await store.readSession(s.id);
		expect(back?.context_checkpoint?.generation).toBe(1);
	});

	it("rejects a stale generation (another op bumped it)", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		// Establish generation 1.
		await store.commitCheckpoint(s.id, undefined, FP, (d) => {
			d.context_checkpoint = { ...makeCp(1, FP), summary: "g1" };
		});
		// A concurrent op bumps to generation 2.
		await store.commitCheckpoint(s.id, 1, FP, (d) => {
			d.context_checkpoint = { ...makeCp(2, FP), summary: "g2" };
		});
		// Now a stale candidate expecting generation 1 must be rejected.
		const res = await store.commitCheckpoint(s.id, 1, FP, (d) => {
			d.context_checkpoint = { ...makeCp(3, FP), summary: "g3-stale" };
		});
		expect(res.ok).toBe(false);
		if (!res.ok) {
			expect(res.reason).toMatch(/generation mismatch/i);
		}
		// Old generation 2 intact.
		const back = await store.readSession(s.id);
		expect(back?.context_checkpoint?.generation).toBe(2);
		expect(back?.context_checkpoint?.summary).toBe("g2");
	});

	it("rejects when the slide fingerprint changed", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		await store.commitCheckpoint(s.id, undefined, FP, (d) => {
			d.context_checkpoint = makeCp(1, FP);
		});
		const res = await store.commitCheckpoint(s.id, 1, "fp-CHANGED", (d) => {
			d.context_checkpoint = makeCp(2, "fp-CHANGED");
		});
		expect(res.ok).toBe(false);
		if (!res.ok) {
			expect(res.reason).toMatch(/fingerprint mismatch/i);
		}
		const back = await store.readSession(s.id);
		expect(back?.context_checkpoint?.generation).toBe(1);
		expect(back?.context_checkpoint?.slide_fingerprint).toBe(FP);
	});

	it("does not corrupt old data when writeSession throws", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		await store.commitCheckpoint(s.id, undefined, FP, (d) => {
			d.context_checkpoint = makeCp(1, FP);
		});
		// Sabotage writeSession so the NEXT commit's write fails, but the
		// readSession inside withLock still works. We replace writeSession with
		// a throwing stub for the duration of the failing commit.
		const origWrite = store.writeSession;
		store.writeSession = async () => {
			throw new Error("mock disk full");
		};
		const res = await store.commitCheckpoint(s.id, 1, FP, (d) => {
			d.context_checkpoint = makeCp(2, FP);
		});
		// Restore so the assertion read works.
		store.writeSession = origWrite;
		expect(res.ok).toBe(false);
		if (!res.ok) {
			expect(res.reason).toMatch(/write failed/i);
		}
		// Generation 1 intact on disk (the failing commit never renamed).
		const back = await store.readSession(s.id);
		expect(back?.context_checkpoint?.generation).toBe(1);
	});

	it("rejects when the session does not exist", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const res = await store.commitCheckpoint("sess_missing", undefined, FP, () => {});
		expect(res.ok).toBe(false);
		if (!res.ok) {
			expect(res.reason).toMatch(/not found/i);
		}
	});
});

/** Helper: build a minimal ContextCheckpoint at a given generation. */
function makeCp(generation: number, fingerprint: string): ContextCheckpoint {
	return {
		version: 1,
		generation,
		created_at: Date.now(),
		slide_fingerprint: fingerprint,
		through_message_seq: 0,
		summary: `g${generation}`,
		annotation_index: "",
		overview_derivative: null,
		system_prompt_version: "spv1",
		tool_schema_hash: "tsh1",
		request_schema_version: REQUEST_SCHEMA_VERSION,
		stable_prefix_hash: `sph-g${generation}`,
	};
}

// =========================================================================== //
// ensureCheckpoint (§10 lazy generation 1)
// =========================================================================== //
describe("ensureCheckpoint (§10 lazy g1)", () => {
	it("returns the existing checkpoint unchanged when present", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = store.emptySession(SLIDE, "main", "sess_x");
		const existing = makeCp(5, FP);
		d.context_checkpoint = existing;
		const out = ensureCheckpoint(d, {
			system_prompt_version: "spv1",
			tool_schema_hash: "tsh1",
			slide_fingerprint: FP,
		});
		expect(out).toBe(existing); // same reference
		expect(out.generation).toBe(5);
	});

	it("builds generation 1 for a legacy session with no checkpoint", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = legacySession("sess_legacy", 3);
		// Run migration so messages get seqs.
		const migrated = store.emptySession(SLIDE, "main", "sess_legacy");
		migrated.messages = d.messages;
		delete (migrated as SessionData & { next_message_seq?: number }).next_message_seq;
		assignMessageSeqs(migrated);
		// Add a compaction entry so summary is non-empty.
		migrated.compaction_entries = [
			{ seq: 1, tokens_before: 1000, tokens_after: 500, ts: 1, summary: "prior summary" },
		] as never;
		migrated.observations = [{ bbox: { x: 1, y: 2, w: 3, h: 4 }, note: "obs1" }];

		const out = ensureCheckpoint(migrated, {
			system_prompt_version: "spv1",
			tool_schema_hash: "tsh1",
			slide_fingerprint: FP,
		});
		expect(out.generation).toBe(1);
		expect(out.version).toBe(1);
		expect(out.slide_fingerprint).toBe(FP);
		expect(out.summary).toBe("prior summary");
		expect(out.system_prompt_version).toBe("spv1");
		expect(out.tool_schema_hash).toBe("tsh1");
		expect(out.request_schema_version).toBe(REQUEST_SCHEMA_VERSION);
		// through_message_seq = max assigned seq (3 messages → seq 3).
		expect(out.through_message_seq).toBe(3);
		// overview_derivative is null in Phase 2a (degraded).
		expect(out.overview_derivative).toBeNull();
		// annotation_index derived from observations.
		expect(out.annotation_index).toContain("obs1");
		// stable_prefix_hash is a valid sha256.
		expect(out.stable_prefix_hash).toMatch(/^[0-9a-f]{64}$/);
		// Stored on data.
		expect(migrated.context_checkpoint).toBe(out);
	});

	it("computes through_message_seq=0 when there are no messages", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d = store.emptySession(SLIDE, "main", "sess_empty");
		const out = ensureCheckpoint(d, {
			system_prompt_version: "spv1",
			tool_schema_hash: "tsh1",
			slide_fingerprint: FP,
		});
		expect(out.through_message_seq).toBe(0);
		expect(out.summary).toBe("");
		expect(out.annotation_index).toBe("");
	});

	it("stable_prefix_hash changes when summary changes", () => {
		const store = new SessionStore({ sessionsDir: "/tmp/unused" });
		const d1 = store.emptySession(SLIDE, "main", "sess_a");
		d1.compaction_entries = [
			{ seq: 1, tokens_before: 100, tokens_after: 50, ts: 1, summary: "sum A" },
		] as never;
		const d2 = store.emptySession(SLIDE, "main", "sess_b");
		d2.compaction_entries = [
			{ seq: 1, tokens_before: 100, tokens_after: 50, ts: 1, summary: "sum B" },
		] as never;
		const deps = { system_prompt_version: "spv1", tool_schema_hash: "tsh1", slide_fingerprint: FP };
		const h1 = ensureCheckpoint(d1, deps).stable_prefix_hash;
		const h2 = ensureCheckpoint(d2, deps).stable_prefix_hash;
		expect(h1).not.toBe(h2);
	});
});

// =========================================================================== //
// checkpointStale (§10 version invalidation)
// =========================================================================== //
describe("checkpointStale (§10 version invalidation)", () => {
	function makeEnv(over: Partial<CheckpointEnv> = {}): CheckpointEnv {
		return {
			system_prompt_version: "spv1",
			tool_schema_hash: "tsh1",
			request_schema_version: REQUEST_SCHEMA_VERSION,
			slide_fingerprint: FP,
			overview_target_long_edge: 1024,
			overview_jpeg_quality: 85,
			overview_overlay_version: "v1",
			overview_resize_algorithm: "LANCZOS",
			overview_encoder_id: "pillow",
			...over,
		};
	}
	function makeCpWithOverview(over: Partial<ContextCheckpoint> = {}): ContextCheckpoint {
		return {
			version: 1,
			generation: 1,
			created_at: 0,
			slide_fingerprint: FP,
			through_message_seq: 0,
			summary: "",
			annotation_index: "",
			overview_derivative: {
				ref_id: "ref_overview",
				target_long_edge: 1024,
				jpeg_quality: 85,
				overlay_version: "v1",
				resize_algorithm: "LANCZOS",
				encoder_id: "pillow",
				encoder_version: "unknown-phase2a",
				mime_type: "image/jpeg",
				content_sha256: "abc",
			},
			system_prompt_version: "spv1",
			tool_schema_hash: "tsh1",
			request_schema_version: REQUEST_SCHEMA_VERSION,
			stable_prefix_hash: "sph",
			...over,
		};
	}

	it("returns null when all version fields match", () => {
		expect(checkpointStale(makeCpWithOverview(), makeEnv())).toBeNull();
	});

	it("detects system_prompt_version change", () => {
		const reason = checkpointStale(makeCpWithOverview(), makeEnv({ system_prompt_version: "spv2" }));
		expect(reason).toMatch(/system_prompt_version/i);
	});

	it("detects tool_schema_hash change", () => {
		const reason = checkpointStale(makeCpWithOverview(), makeEnv({ tool_schema_hash: "tsh2" }));
		expect(reason).toMatch(/tool_schema_hash/i);
	});

	it("detects request_schema_version change", () => {
		const reason = checkpointStale(makeCpWithOverview(), makeEnv({ request_schema_version: 999 }));
		expect(reason).toMatch(/request_schema_version/i);
	});

	it("detects slide_fingerprint change", () => {
		const reason = checkpointStale(makeCpWithOverview(), makeEnv({ slide_fingerprint: "fp-other" }));
		expect(reason).toMatch(/slide_fingerprint/i);
	});

	it("detects overview encoding spec changes (target_long_edge, jpeg_quality, overlay, resize, encoder)", () => {
		expect(checkpointStale(makeCpWithOverview(), makeEnv({ overview_target_long_edge: 768 }))).toMatch(/target_long_edge/i);
		expect(checkpointStale(makeCpWithOverview(), makeEnv({ overview_jpeg_quality: 82 }))).toMatch(/jpeg_quality/i);
		expect(checkpointStale(makeCpWithOverview(), makeEnv({ overview_overlay_version: "v2" }))).toMatch(/overlay_version/i);
		expect(checkpointStale(makeCpWithOverview(), makeEnv({ overview_resize_algorithm: "NEAREST" }))).toMatch(/resize_algorithm/i);
		expect(checkpointStale(makeCpWithOverview(), makeEnv({ overview_encoder_id: "libjpeg" }))).toMatch(/encoder_id/i);
	});

	it("does NOT check encoding spec when overview_derivative is null (degraded gen)", () => {
		const cp = makeCpWithOverview({ overview_derivative: null });
		// Even with mismatched encoding env, a null-overview checkpoint is not stale.
		expect(checkpointStale(cp, makeEnv({ overview_target_long_edge: 768 }))).toBeNull();
	});
});

// =========================================================================== //
// ensureCheckpointRun — stale rebuild CAS + monotonic generation (P1-2)
// =========================================================================== //
//
// P1-2: the stale-rebuild path must (a) commit a generation N+1 (NOT reset to
// 1), (b) pass the OLD fingerprint as the CAS expectedFingerprint so a slide
// change does not always fail, (c) NOT backfill against an un-committed
// candidate when the CAS is rejected, and (d) keep the committed checkpoint
// intact on rejection.
import { AgentRunner } from "../src/agent-runner.js";
import { SessionEventBus } from "../src/events.js";
import type { FlaskClient } from "../src/flask-client.js";
import type { SlideInfo } from "../src/tools.js";

const SLIDE_INFO_BASE: SlideInfo = { width: 10000, height: 8000, levelDownsamples: [1, 2, 4, 8], mpp: 0.5, fingerprint: FP };
const SYSTEM_PROMPT_V1 = "system-v1";
const SYSTEM_PROMPT_V2 = "system-v2";

/** A no-op flask whose region() is never reached in these tests (no overview). */
function noopFlask(): Pick<FlaskClient, "region"> {
	return {
		region: async () => {
			throw new Error("region should not be called by ensureCheckpointRun in these tests");
		},
	} as Pick<FlaskClient, "region">;
}

describe("ensureCheckpointRun — stale rebuild CAS + monotonic generation (P1-2)", () => {
	it("bumps generation to N+1 (NOT reset to 1) and commits the new slide fingerprint when the slide changes", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		// Seed a generation-3 checkpoint at the OLD fingerprint.
		const oldFp = "fp-old";
		await store.commitCheckpoint(s.id, undefined, oldFp, (d) => {
			d.context_checkpoint = { ...makeCp(3, oldFp), summary: "g3-old" };
		});
		const bus = new SessionEventBus(store);
		const runner = new AgentRunner(store, bus, noopFlask() as FlaskClient);
		// activeRunConfig is private; set it via cast so resolveTransformSettings works.
		(runner as unknown as { activeRunConfig: unknown }).activeRunConfig = {};
		const newFp = "fp-new";
		const newSlideInfo: SlideInfo = { ...SLIDE_INFO_BASE, fingerprint: newFp };
		await (runner as unknown as { ensureCheckpointRun: (a: unknown) => Promise<void> }).ensureCheckpointRun({
			sessionId: s.id,
			slide: SLIDE,
			slideInfo: newSlideInfo,
			systemPrompt: SYSTEM_PROMPT_V1,
			tools: [],
			firstSnapshotToolCallIdRef: { value: null },
		});
		const back = await store.readSession(s.id);
		// P1-2: generation = 3 + 1 = 4 (monotonic), not 1.
		expect(back?.context_checkpoint?.generation).toBe(4);
		// P1-2: the new fingerprint is committed.
		expect(back?.context_checkpoint?.slide_fingerprint).toBe(newFp);
	});

	it("bumps generation monotonically when the prompt version changes (no fingerprint change)", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		// Seed a generation-5 checkpoint at the current fingerprint.
		await store.commitCheckpoint(s.id, undefined, FP, (d) => {
			d.context_checkpoint = { ...makeCp(5, FP), system_prompt_version: "spv-old" as never, summary: "g5" };
		});
		const bus = new SessionEventBus(store);
		const runner = new AgentRunner(store, bus, noopFlask() as FlaskClient);
		(runner as unknown as { activeRunConfig: unknown }).activeRunConfig = {};
		// prompt version differs → stale; fingerprint unchanged.
		await (runner as unknown as { ensureCheckpointRun: (a: unknown) => Promise<void> }).ensureCheckpointRun({
			sessionId: s.id,
			slide: SLIDE,
			slideInfo: { ...SLIDE_INFO_BASE, fingerprint: FP },
			systemPrompt: SYSTEM_PROMPT_V2, // different prompt → different version
			tools: [],
			firstSnapshotToolCallIdRef: { value: null },
		});
		const back = await store.readSession(s.id);
		// P1-2: generation = 5 + 1 = 6 (monotonic +1, not reset to 1).
		expect(back?.context_checkpoint?.generation).toBe(6);
		expect(back?.context_checkpoint?.slide_fingerprint).toBe(FP);
	});

	it("does NOT backfill and keeps the old checkpoint intact when the CAS is rejected (concurrent bump)", async () => {
		const store = new SessionStore({ sessionsDir: await newStoreDir() });
		const s = await store.createSession({ slide: SLIDE, kind: "main" });
		const oldFp = "fp-old";
		await store.commitCheckpoint(s.id, undefined, oldFp, (d) => {
			d.context_checkpoint = { ...makeCp(3, oldFp), summary: "g3" };
		});
		// Simulate a concurrent bump: between our read and our CAS, another op
		// bumps the generation to 9 (so our CAS expecting generation 3 is rejected).
		// We do this by patching store.commitCheckpoint to first bump then delegate.
		const origCommit = store.commitCheckpoint.bind(store);
		let injectedBump = false;
		store.commitCheckpoint = (async (id: string, expectedGen: number | undefined, expectedFp: string, mutate: (d: SessionData) => void) => {
			if (!injectedBump && expectedGen === 3) {
				injectedBump = true;
				// Concurrent op bumps to generation 9 with a different fingerprint.
				await origCommit(id, 3, oldFp, (d) => {
					d.context_checkpoint = { ...makeCp(9, "fp-concurrent"), summary: "g9-concurrent" };
				});
			}
			return origCommit(id, expectedGen, expectedFp, mutate);
		}) as typeof store.commitCheckpoint;

		const bus = new SessionEventBus(store);
		const runner = new AgentRunner(store, bus, noopFlask() as FlaskClient);
		(runner as unknown as { activeRunConfig: unknown }).activeRunConfig = {};
		await (runner as unknown as { ensureCheckpointRun: (a: unknown) => Promise<void> }).ensureCheckpointRun({
			sessionId: s.id,
			slide: SLIDE,
			slideInfo: { ...SLIDE_INFO_BASE, fingerprint: "fp-new" },
			systemPrompt: SYSTEM_PROMPT_V1,
			tools: [],
			firstSnapshotToolCallIdRef: { value: null },
		});
		const back = await store.readSession(s.id);
		// P1-2: the CAS was rejected → the concurrent generation 9 is intact,
		// and our stale candidate (generation 4) was NOT written.
		expect(back?.context_checkpoint?.generation).toBe(9);
		expect(back?.context_checkpoint?.summary).toBe("g9-concurrent");
		expect(back?.context_checkpoint?.slide_fingerprint).toBe("fp-concurrent");
	});
});
