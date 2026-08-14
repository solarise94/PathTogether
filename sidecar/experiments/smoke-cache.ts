/**
 * Phase 4 cache smoke: drive the REAL AgentRunner against gpt-5.6-luna via the
 * CPA openai path with prompt_cache_mode=explicit. Verifies the full chain:
 * sidecar prompt_cache_key injection → CPA passthrough → upstream cache hit →
 * non-zero cached_tokens in the second+ request's metrics.
 *
 * Two short turns over the same slide (same checkpoint generation → same cache
 * key). The first request warms the cache; the second should observe a hit.
 *
 * Gateway note: the CPA gateway is FLAKY — it intermittently (and in bursts)
 * returns a bodyless HTTP 400 for requests that combine a large inline image
 * with `prompt_cache_key`, and intermittently drops the trailing finish_reason
 * chunk. The sidecar handles both (supportsFinishReason:false synthesizes a
 * stop reason; the bodyless 400 is retried as transient; terminal errors surface
 * as agent_error instead of being masked as "finished"). Because the 400 bursts
 * can outlast the 3-attempt transient budget, this smoke test RETRIES the whole
 * flow a few times to land a cooperative gateway window. Each attempt's outcome
 * is logged so a real code regression is not hidden behind a gateway hiccup.
 */
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";

import { SessionStore } from "../src/session-store.js";
import { SessionEventBus } from "../src/events.js";
import { AgentRunner, type RunConfig } from "../src/agent-runner.js";
import { FlaskClient } from "../src/flask-client.js";
import { LegacyFlaskPlatformAdapter } from "../src/platform/legacy-flask-adapter.js";
import type { RequestMetrics } from "../src/metrics.js";
import { ensureSlides, fakeCompactionModels } from "./src/run.js";
import { spawnFlask } from "./src/flask-process.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SIDECAR = dirname(HERE);
const REPO = dirname(SIDECAR);

const CPA_URL = process.env.CPA_BASE_URL ?? "http://localhost:46450/v1";
const CPA_KEY = process.env.CPA_API_KEY ?? "";
const MODEL = "gpt-5.6-luna";

if (!CPA_KEY) {
	console.error("CPA_API_KEY env is required (never hardcode keys in the repo). Export it, then re-run.");
	process.exit(2);
}
const TASK = "把视野移到 (2000,1500) level 1，抓一张快照，一句话描述，然后 finish。";
const MAX_ATTEMPTS = 3;

async function waitForSettle(store: SessionStore, sid: string, ms = 300_000): Promise<string> {
	const dl = Date.now() + ms;
	for (;;) {
		const d = await store.readSession(sid);
		if (d && d.status !== "running" && d.status !== "idle") return d.status;
		if (Date.now() > dl) throw new Error(`timeout; status=${d?.status}`);
		await new Promise((r) => setTimeout(r, 1000));
	}
}

interface AttemptResult {
	pass: boolean;
	reason: string;
	collected: RequestMetrics[];
	turn1Status: string;
	turn2Status: string;
	turn1Events: string[];
}

function num(v: number | "unknown"): number {
	return typeof v === "number" ? v : 0;
}

/**
 * Evaluate the §4 success criteria against the collected metrics + event log.
 * `rows1` is the number of metrics rows collected during turn 1 (turn 2 rows
 * are the remainder). Returns { pass, reason }.
 */
function evaluate(
	collected: RequestMetrics[],
	rows1: number,
	turn1Events: string[],
	turn2Events: string[],
): { pass: boolean; reason: string } {
	const failures: string[] = [];
	if (collected.length === 0) return { pass: false, reason: "no metrics rows (stream never reached done)" };
	// Full tool chain each turn: snapshot_captured + agent_finished.
	const t1Chain = turn1Events.includes("snapshot_captured") && turn1Events.includes("agent_finished");
	const t2Chain = turn2Events.includes("snapshot_captured") && turn2Events.includes("agent_finished");
	if (!t1Chain) failures.push("turn1 tool chain incomplete");
	if (!t2Chain) failures.push("turn2 tool chain incomplete");
	// Image actually flowed on a snapshot turn.
	if (!collected.some((m) => num(m.working_set_image_bytes_sent) > 0)) {
		failures.push("no image bytes sent (snapshot image did not flow)");
	}
	// Turn-2 cache hit.
	const turn2Rows = collected.slice(rows1);
	if (!turn2Rows.some((m) => num(m.cached_tokens) > 0)) failures.push("no turn-2 cached_tokens>0");
	// Explicit mode retained (no downgrade).
	if (collected.some((m) => m.prompt_cache_mode !== "explicit")) {
		failures.push("prompt_cache_mode downgraded (expected explicit)");
	}
	return { pass: failures.length === 0, reason: failures.join("; ") || "all criteria met" };
}

async function runOnce(
	slide: string,
	config: RunConfig,
	flaskUrl: string,
	flaskToken: string,
): Promise<AttemptResult> {
	const dir = await mkdtemp(join(tmpdir(), "svs-cache-smoke-"));
	const store = new SessionStore({ sessionsDir: dir });
	const bus = new SessionEventBus(store);
	const client = new FlaskClient({ baseUrl: flaskUrl, token: flaskToken });
	const platform = new LegacyFlaskPlatformAdapter({ flask: client });
	const collected: RequestMetrics[] = [];
	const runner = new AgentRunner(store, bus, platform, {
		metricsSink: (m) => { collected.push(m); },
		compactionModels: fakeCompactionModels("[smoke]") as never,
	});
	const turn1Events: string[] = [];
	const turn2Events: string[] = [];
	let inTurn2 = false;

	// Turn 1 — warms the cache (cold write).
	console.log("[cache] turn 1 (warm cache)…");
	const { sessionId } = await runner.runMain({ slide, config, task: TASK, fresh: true });
	const unsub1 = bus.subscribe(sessionId, (_seq, type) => { turn1Events.push(type); });
	const turn1Status = await waitForSettle(store, sessionId);
	unsub1();
	const rows1 = collected.length;
	console.log(`[cache] turn 1 done, status=${turn1Status}, ${rows1} requests, events=[${dedupe(turn1Events).join(",")}]`);

	// Turn 2 — should observe cache hits (same checkpoint gen → same cache key).
	console.log("[cache] turn 2 (expect cache hit)…");
	await store.withLock(sessionId, async (d) => {
		if (!d) return null;
		d.messages.push({ role: "user", content: "回到 (2000,1500) 再抓一张快照确认，然后 finish。", timestamp: Date.now() } as never);
		d.updated_at = Math.floor(Date.now() / 1000);
		await store.writeSession(sessionId, d);
		return d;
	});
	inTurn2 = true;
	const unsub2 = bus.subscribe(sessionId, (_seq, type) => { if (inTurn2) turn2Events.push(type); });
	await runner.continueMain({ slide, config });
	const turn2Status = await waitForSettle(store, sessionId);
	inTurn2 = false;
	unsub2();
	console.log(`[cache] turn 2 done, status=${turn2Status}, ${collected.length} total requests, events=[${dedupe(turn2Events).join(",")}]`);

	// Log every metrics row.
	for (let i = 0; i < collected.length; i++) {
		const m = collected[i]!;
		const turn = i < rows1 ? 1 : 2;
		console.log(`  req${i} (turn${turn}): input=${m.input_tokens} cached=${m.cached_tokens} mode=${m.prompt_cache_mode} imgBytes=${num(m.working_set_image_bytes_sent)}`);
	}
	const evalRes = evaluate(collected, rows1, turn1Events, turn2Events);
	void dir;
	return { pass: evalRes.pass, reason: evalRes.reason, collected, turn1Status, turn2Status, turn1Events };
}

function dedupe(events: string[]): string[] {
	const seen = new Set<string>();
	const out: string[] = [];
	for (const e of events) {
		if (!seen.has(e)) { seen.add(e); out.push(e); }
	}
	return out;
}

async function main(): Promise<void> {
	const slidesDir = join(HERE, "fixtures", "slides");
	await ensureSlides(REPO, join(HERE, "fixtures"), slidesDir);
	const flask = await spawnFlask({ repoRoot: REPO, uploadDir: slidesDir });
	console.log(`[cache] flask up ${flask.url}`);
	const config = {
		base_url: CPA_URL, api_key: CPA_KEY, model: MODEL,
		max_tokens: 2048, context_window_tokens: 272000,
		api_protocol: "openai" as const,
		prompt_cache_mode: "explicit",
	};
	let passed = false;
	let lastResult: AttemptResult | null = null;
	try {
		for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
			console.log(`\n[cache] ===== attempt ${attempt}/${MAX_ATTEMPTS} =====`);
			const res = await runOnce("synth-dense.tiff", config, flask.url, flask.token).catch((e) => {
				console.log(`[cache] attempt ${attempt} threw: ${(e as Error)?.message || e}`);
				return null;
			});
			if (res) {
				lastResult = res;
				console.log(`[cache] attempt ${attempt}: pass=${res.pass} reason=${res.reason}`);
				if (res.pass) { passed = true; break; }
			}
			if (attempt < MAX_ATTEMPTS) {
				console.log(`[cache] gateway may be flaky; retrying in 15s…`);
				await new Promise((r) => setTimeout(r, 15_000));
			}
		}
	} finally {
		await flask.stop();
	}

	console.log(`\n[cache] cache_hit_observed=${passed}`);
	console.log(passed ? "[cache] PASS" : "[cache] FAIL (see per-attempt logs; the CPA gateway intermittently rejects image+prompt_cache_key with a bodyless 400)");
	process.exitCode = passed ? 0 : 1;
	void lastResult;
}
main().catch((e) => { console.error("[cache] FATAL", (e as Error)?.message || e); process.exit(1); });
