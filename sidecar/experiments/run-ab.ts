#!/usr/bin/env tsx
/**
 * Phase 4 A/B framework (Wave 2) — CLI entry for the execution runner.
 *
 * Drives a scripted A/B matrix end-to-end: ensure fixtures → spawn Flask → pin
 * manifest → run every (arm, task) cell through the real AgentRunner → write
 * metrics.jsonl / rubric.json / report.md / run.json.
 *
 * Run (from the sidecar dir):
 *   npx tsx experiments/run-ab.ts --step 1
 *   npx tsx experiments/run-ab.ts --step 2 --image-arm step1-overview-1024
 *   PHASE4_CPA_VERIFIED=1 CPA_API_KEY=sk-... npx tsx experiments/run-ab.ts --step 1 --mode real-model --dry-run
 *
 * Real-model mode is gated (gate.ts): requires PHASE4_CPA_VERIFIED=1 AND
 * CPA_API_KEY. Cache conclusions only apply to the openai-protocol path
 * (gemini path is cache-unobservable, #592). See experiments/README.md.
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";

import { FlaskClient } from "../src/flask-client.js";
import {
	runExperiment,
	ensureSlides,
	pinManifest,
	RunnerArgumentError,
	type FixtureEnv,
	type RunOptions,
} from "./src/run.js";
import { loadManifest } from "./src/manifest.js";
import { spawnFlask } from "./src/flask-process.js";

const HERE = dirname(fileURLToPath(import.meta.url));
// sidecar/experiments → sidecar → repo root
const SIDECAR_DIR = dirname(HERE);
const REPO_ROOT = dirname(SIDECAR_DIR);
const EXperiments_DIR = HERE;

function utcStampForRunId(d = new Date()): string {
	// Compact UTC: 20260812T143051Z — safe for directory names.
	const pad = (n: number): string => String(n).padStart(2, "0");
	return (
		`${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
		`T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`
	);
}

function help(): string {
	return [
		"Phase 4 A/B runner (Wave 2)",
		"",
		"Usage: npx tsx experiments/run-ab.ts --step <1|2> [options]",
		"",
		"Options:",
		"  --step <1|2>              Experiment step (required).",
		"  --image-arm <id>          Step 2: Step-1 arm id whose image overrides are copied in (required for step 2).",
		"  --mode <mode>             scripted | real-model (default scripted). real-model is gated (PHASE4_CPA_VERIFIED=1 + CPA_API_KEY).",
		"  --taskset <path>          Taskset JSON (default experiments/tasksets/reading-v1.json).",
		"  --arms-dir <path>         Arms directory (default experiments/arms).",
		"  --fixtures-dir <path>     Fixtures directory (default experiments/fixtures).",
		"  --out <dir>               Output directory (default experiments/results/<run-id>).",
		"  --cell-gap-ms <ms>        Cooldown gap between cells (upstream rate-limit protection, default 0).",
		"  --keep-flask              Leave Flask running after the run (debug).",
		"  --dry-run                 Resolve + print the matrix + config (key redacted); do NOT execute or spawn Flask.",
		"  --max-cells <N>           Cost guard: cap the number of (arm, task) cells executed.",
		"  --settle-timeout-ms <ms>  Per-turn settle timeout (default: scripted 30000, real-model 300000).",
		"  --help                    Show this help.",
		"",
	].join("\n");
}

async function main(): Promise<void> {
	const { values } = parseArgs({
		options: {
			step: { type: "string" },
			"image-arm": { type: "string" },
			mode: { type: "string" },
			taskset: { type: "string" },
			"arms-dir": { type: "string" },
			"fixtures-dir": { type: "string" },
			out: { type: "string" },
			"keep-flask": { type: "boolean", default: false },
			"dry-run": { type: "boolean", default: false },
			"max-cells": { type: "string" },
			"settle-timeout-ms": { type: "string" },
			"cell-gap-ms": { type: "string" },
			help: { type: "boolean", default: false },
		},
		strict: true,
		allowPositionals: false,
	});

	if (values.help) {
		process.stdout.write(help());
		return;
	}

	const stepNum = Number(values.step);
	if (values.step === undefined || (stepNum !== 1 && stepNum !== 2)) {
		process.stderr.write(help());
		throw new RunnerArgumentError("--step must be 1 or 2");
	}
	const step = stepNum as 1 | 2;
	const mode = (values.mode ?? "scripted") as "scripted" | "real-model";
	if (mode !== "scripted" && mode !== "real-model") {
		throw new RunnerArgumentError("--mode must be scripted or real-model");
	}

	const fixturesDir = resolve(values["fixtures-dir"] ?? join(EXperiments_DIR, "fixtures"));
	const slidesDir = join(fixturesDir, "slides");
	const tasksetPath = resolve(values.taskset ?? join(EXperiments_DIR, "tasksets", "reading-v1.json"));
	const armsDir = resolve(values["arms-dir"] ?? join(EXperiments_DIR, "arms"));
	const runId = `step${step}-${mode}-${utcStampForRunId()}`;
	const outDir = resolve(values.out ?? join(EXperiments_DIR, "results", runId));

	const maxCellsRaw = values["max-cells"];
	const maxCells = maxCellsRaw != null && maxCellsRaw !== "" ? Number(maxCellsRaw) : undefined;
	if (maxCells !== undefined && (!Number.isFinite(maxCells) || maxCells <= 0 || !Number.isInteger(maxCells))) {
		throw new RunnerArgumentError("--max-cells must be a positive integer");
	}
	const settleTimeoutRaw = values["settle-timeout-ms"];
	const settleTimeoutMs = settleTimeoutRaw != null && settleTimeoutRaw !== "" ? Number(settleTimeoutRaw) : undefined;
	if (settleTimeoutMs !== undefined && (!Number.isFinite(settleTimeoutMs) || settleTimeoutMs <= 0 || !Number.isInteger(settleTimeoutMs))) {
		throw new RunnerArgumentError("--settle-timeout-ms must be a positive integer");
	}
	const cellGapRaw = values["cell-gap-ms"];
	const cellGapMs = cellGapRaw != null && cellGapRaw !== "" ? Number(cellGapRaw) : undefined;
	if (cellGapMs !== undefined && (!Number.isFinite(cellGapMs) || cellGapMs < 0 || !Number.isInteger(cellGapMs))) {
		throw new RunnerArgumentError("--cell-gap-ms must be a non-negative integer");
	}

	const opts: RunOptions = {
		mode,
		step,
		imageArmId: values["image-arm"],
		tasksetPath,
		armsDir,
		outDir,
		runId,
		repoRoot: REPO_ROOT,
		fixturesDir,
		keepFlask: !!values["keep-flask"],
		dryRun: !!values["dry-run"],
		maxCells,
		settleTimeoutMs,
		cellGapMs,
	};

	// Real acquireEnv: ensure slides → spawn Flask → pin manifest → FlaskClient.
	const acquireEnv = async (): Promise<FixtureEnv> => {
		// (a) Generate slides if the slides dir is empty.
		await ensureSlides(REPO_ROOT, fixturesDir, slidesDir);
		// (b) Spawn Flask with the slides dir as UPLOAD_DIR + a known token.
		const handle = await spawnFlask({ repoRoot: REPO_ROOT, uploadDir: slidesDir });
		// (c) Pin the manifest against the live Flask (token inherited via env).
		const manifestDir = await mkdtemp(join(tmpdir(), "svs-ab-manifest-"));
		const manifestPath = join(manifestDir, "manifest.json");
		try {
			await pinManifest({
				repoRoot: REPO_ROOT,
				fixturesDir,
				slidesDir,
				flaskUrl: handle.url,
				manifestPath,
				token: handle.token,
			});
			const manifest = loadManifest(manifestPath);
			// (d) Real FlaskClient over HTTP (NOT the in-memory mock).
			const flask = new FlaskClient({ baseUrl: handle.url, token: handle.token });
			return {
				flask,
				manifest,
				teardown: async () => {
					await handle.stop();
				},
			};
		} catch (e) {
			await handle.stop().catch(() => undefined);
			throw e;
		}
	};

	const result = await runExperiment(opts, { acquireEnv });
	if (opts.dryRun) {
		process.stdout.write(`\n[dry-run] ${result.runId}: resolved ${result.armIds.length} arm(s); no execution performed.\n`);
	} else {
		process.stdout.write(
			`\n[done] run=${result.runId} arms=[${result.armIds.join(", ")}] ` +
				`rows=${result.rows.length} rubric=${result.rubricOutcomes.length}` +
				(result.cellErrors.length ? ` cell_errors=${result.cellErrors.length}` : "") +
				` → ${result.outDir}\n`,
		);
	}
}

main().catch((e) => {
	process.stderr.write(`\n[FATAL] ${(e as Error)?.message || String(e)}\n`);
	process.exit(1);
});
