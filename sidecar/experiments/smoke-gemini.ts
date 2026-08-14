/**
 * Phase 4 CPA gemini smoke: drive the REAL AgentRunner against the CPA
 * gateway's gemini-compatible endpoint (gemini-3.6-flash-high) with the real
 * Flask (synthetic fixtures) as the slide backend. Verifies:
 *   - protocol wiring (api_protocol: "gemini" → google-generative-ai provider);
 *   - vision (snapshot image blocks flow through the assembler);
 *   - tool calling (goto/snapshot/finish round-trip through pi's function
 *     calling over the CPA gemini endpoint);
 *   - metrics: cache read/write fields observable (0 is fine — CPA-UNVERIFIED).
 *
 * Run from sidecar/:  npx tsx experiments/smoke-gemini.ts
 */
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";

import { SessionStore } from "../src/session-store.js";
import { SessionEventBus } from "../src/events.js";
import { AgentRunner } from "../src/agent-runner.js";
import { FlaskClient } from "../src/flask-client.js";
import { LegacyFlaskPlatformAdapter } from "../src/platform/legacy-flask-adapter.js";
import type { RequestMetrics } from "../src/metrics.js";

import { ensureSlides } from "./src/run.js";
import { spawnFlask } from "./src/flask-process.js";
import { fakeCompactionModels } from "./src/run.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SIDECAR_DIR = dirname(HERE);
const REPO_ROOT = dirname(SIDECAR_DIR);

const CPA_BASE_URL = process.env.CPA_BASE_URL ?? "http://localhost:46450/v1beta";
const CPA_KEY = process.env.CPA_API_KEY ?? "";
const MODEL = "gemini-3.6-flash-high";

if (!CPA_KEY) {
	console.error("CPA_API_KEY env is required (never hardcode keys in the repo). Export it, then re-run.");
	process.exit(2);
}

async function waitForSettle(store: SessionStore, sessionId: string, timeoutMs = 300_000): Promise<string> {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const d = await store.readSession(sessionId);
		if (d && d.status !== "running" && d.status !== "idle") return d.status;
		if (Date.now() > deadline) throw new Error(`session did not settle; last status=${d?.status}`);
		await new Promise((r) => setTimeout(r, 1000));
	}
}

async function main(): Promise<void> {
	const fixturesDir = join(HERE, "fixtures");
	const slidesDir = join(fixturesDir, "slides");
	await ensureSlides(REPO_ROOT, fixturesDir, slidesDir);
	const flask = await spawnFlask({ repoRoot: REPO_ROOT, uploadDir: slidesDir });
	console.log(`[smoke] flask up at ${flask.url}`);
	try {
		const storeDir = await mkdtemp(join(tmpdir(), "svs-gemini-smoke-"));
		const store = new SessionStore({ sessionsDir: storeDir });
		const bus = new SessionEventBus(store);
		const client = new FlaskClient({ baseUrl: flask.url, token: flask.token });
		// DEBUG: log region request bodies + failures to locate the 400 cause.
		const origRegion = client.region.bind(client);
		client.region = (async (args: Record<string, unknown>) => {
			try {
				return await origRegion(args as never);
			} catch (e) {
				console.log("[smoke] region FAILED body=", JSON.stringify(args), "err=", (e as Error).message);
				throw e;
			}
		}) as never;
		const collected: RequestMetrics[] = [];
		const runner = new AgentRunner(store, bus, new LegacyFlaskPlatformAdapter({ flask: client }), {
			metricsSink: (m) => collected.push(m),
			compactionModels: fakeCompactionModels("[smoke]") as never,
		});

		const config = {
			base_url: CPA_BASE_URL,
			api_key: CPA_KEY,
			model: MODEL,
			max_tokens: 4096,
			context_window_tokens: 272000,
			api_protocol: "gemini" as const,
			prompt_cache_mode: "auto",
		};

		const task =
			"请把视野移动到坐标 (1000, 800) 附近（level 1），抓一张快照，简单描述你看到的内容，然后 finish。";
		console.log("[smoke] runMain …");
		const { sessionId } = await runner.runMain({ slide: "synth-dense.tiff", config, task, fresh: true });
		const status = await waitForSettle(store, sessionId);
		console.log(`[smoke] settled status=${status}`);

		const d = await store.readSession(sessionId);
		const msgs = (d?.messages || []) as Array<{ role?: string; content?: unknown }>;
		const roles = msgs.map((m) => m.role);
		const toolNames: string[] = [];
		let imageRefs = 0;
		for (const m of msgs) {
			const c = m.content;
			if (!Array.isArray(c)) continue;
			for (const b of c as Array<{ type?: string; name?: string }>) {
				if (b?.type === "toolCall" && b.name) toolNames.push(b.name);
				// Canonical transcript stores DEHYDRATED image_ref blocks (the
				// assembler materializes them into image bytes only inside the
				// provider payload — §16.1: provider payload image_ref count is 0).
				if (b?.type === "image_ref") imageRefs += 1;
			}
		}
		console.log("[smoke] roles:", roles.join(","));
		console.log("[smoke] toolCalls:", toolNames.join(","));
		console.log("[smoke] image_ref blocks in canonical transcript:", imageRefs);
		const lastAssistantText = JSON.stringify(
			(msgs.filter((m) => m.role === "assistant").slice(-1)[0]?.content as Array<{ type: string; text?: string }> | undefined)
				?.filter((b) => b.type === "text")
				.map((b) => (b.text || "").slice(0, 200)),
		);
		console.log("[smoke] last assistant text (truncated):", lastAssistantText);
		console.log("[smoke] metrics rows:", collected.length);
		for (const m of collected) {
			console.log(
				`  input=${m.input_tokens} cacheRead=${m.cached_tokens} cacheWrite=${m.cache_write_tokens} ` +
					`sel=${m.selected_images} wsBytes=${m.working_set_image_bytes_sent} mode=${m.prompt_cache_mode}`,
			);
		}
		const ok =
			// "finished" = the agent called finish (terminal). "completed" is NOT
			// used by this runner's state machine.
			status === "finished" &&
			toolNames.includes("goto") &&
			toolNames.includes("snapshot") &&
			imageRefs > 0 &&
			collected.length > 0 &&
			collected.some((m) => (m.working_set_image_bytes_sent as number) > 0) &&
			collected.some((m) => (m.input_tokens as number) > 0);
		console.log(ok ? "[smoke] PASS" : "[smoke] FAIL");
		process.exitCode = ok ? 0 : 1;
	} finally {
		await flask.stop();
	}
}

main().catch((e) => {
	console.error("[smoke] FATAL", (e as Error)?.message || e);
	process.exit(1);
});
