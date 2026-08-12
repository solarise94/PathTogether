/**
 * Phase 4 A/B framework (Wave 1: data plane) — A/B arm loader + matrix builder.
 *
 * Loads the §14 two-step A/B arms (sidecar/experiments/arms/*.json), validates
 * every override key against the KNOWN TransformContextSettings field names
 * (rejecting typos loudly), and produces the ordered experiment matrix for a
 * step. Step-2 arms carry an `image_strategy: "${step1_winner}"` placeholder
 * that MUST be resolved via an explicit `--image-arm` pointing at a concrete
 * Step-1 arm id (arms.ts copies that arm's image overrides in).
 *
 * IMPORTANT — `overview_enabled` (§17 risk 2): the current assembler /
 * transform-context has NO overview on/off switch. We accept the key here as a
 * documented PLACEHOLDER; Wave 2's runner is responsible for wiring it to the
 * actual stable-region overview suppression. This file does NOT modify
 * agent-runner / assembler source (Wave 1 scope).
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

// ------------------------------------------------------------------------- //
// Known override keys (validated against typos)
// ------------------------------------------------------------------------- //

/**
 * The snake_case override keys an arm may set. Mirrors
 * sidecar/src/transform-context.ts `TransformContextConfig` (input form) plus
 * `context_window_tokens` (engine config, default 272000 — see pi-model.ts /
 * compaction.ts), plus the two placeholders documented above.
 */
export const KNOWN_OVERRIDE_KEYS = [
	// TransformContextConfig fields (transform-context.ts, snake_case input form)
	"keep_recent_images",
	"visual_working_set_max",
	"visual_context_budget_tokens",
	"overview_long_edge",
	"working_image_long_edge",
	"detail_image_long_edge",
	"image_jpeg_quality",
	"image_overlay_version",
	"region_materialize_concurrency",
	"image_derivative_cache_max_mb",
	"image_derivative_cache_ttl",
	// Engine config
	"context_window_tokens",
	// PLACEHOLDER (Wave 2 wires; see file header)
	"overview_enabled",
	// Step-2 image-strategy placeholder
	"image_strategy",
] as const;

export type KnownOverrideKey = (typeof KNOWN_OVERRIDE_KEYS)[number];

const KNOWN_OVERRIDE_SET: ReadonlySet<string> = new Set(KNOWN_OVERRIDE_KEYS);

/** Image-strategy override keys copied from the Step-1 winner into Step-2 arms. */
const IMAGE_STRATEGY_KEYS: ReadonlySet<string> = new Set([
	"overview_enabled",
	"overview_long_edge",
	"working_image_long_edge",
	"detail_image_long_edge",
]);

/** Current product default context window (pi-model.ts / compaction.ts). */
export const DEFAULT_CONTEXT_WINDOW_TOKENS = 272000;

// ------------------------------------------------------------------------- //
// Arm types
// ------------------------------------------------------------------------- //

export type ArmStep = 1 | 2;

/** The overrides object inside an arm file. Keys are validated against the known set. */
export type ArmOverrides = Partial<Record<KnownOverrideKey, number | string | boolean>>;

export interface Arm {
	arm_id: string;
	step: ArmStep;
	description?: string;
	overrides: ArmOverrides;
	prompt_cache_mode: "off" | "auto" | "explicit";
}

/** One resolved cell of the experiment matrix (explicit overrides, no placeholders). */
export interface ResolvedArm extends Arm {
	/** Resolved overrides: image_strategy placeholder expanded, no `${...}` left. */
	resolvedOverrides: ArmOverrides;
	/** Source arm id the image strategy was copied from (Step 2 only). */
	imageStrategySource?: string;
}

export interface ExperimentMatrix {
	step: ArmStep;
	/** Ordered arms; Step-2 arms have image_strategy fully resolved. */
	arms: ResolvedArm[];
}

// ------------------------------------------------------------------------- //
// Errors
// ------------------------------------------------------------------------- //

export interface ArmError {
	arm?: string;
	path: string;
	message: string;
}

export class ArmValidationError extends Error {
	readonly errors: ArmError[];
	constructor(errors: ArmError[]) {
		super(`arm validation failed:\n${errors.map((e) => `  - [${e.path}] ${e.message}`).join("\n")}`);
		this.name = "ArmValidationError";
		this.errors = errors;
	}
}

// ------------------------------------------------------------------------- //
// Validator
// ------------------------------------------------------------------------- //

function isStr(v: unknown): v is string {
	return typeof v === "string";
}

/**
 * Validate one parsed arm object. Returns typed {@link Arm} or throws
 * {@link ArmValidationError}.
 */
export function validateArm(obj: unknown): Arm {
	const errors: ArmError[] = [];
	if (!obj || typeof obj !== "object") {
		throw new ArmValidationError([{ path: "$", message: "arm must be an object" }]);
	}
	const a = obj as Record<string, unknown>;

	for (const key of ["arm_id", "step", "overrides", "prompt_cache_mode"]) {
		if (!(key in a)) errors.push({ path: "$", message: `missing required field "${key}"` });
	}
	if (!isStr(a.arm_id) || (a.arm_id as string).length === 0) {
		errors.push({ path: "$.arm_id", message: "must be a non-empty string" });
	}
	if (a.step !== 1 && a.step !== 2) {
		errors.push({ path: "$.step", message: "must be the number 1 or 2" });
	}
	if (a.prompt_cache_mode !== "off" && a.prompt_cache_mode !== "auto" && a.prompt_cache_mode !== "explicit") {
		errors.push({ path: "$.prompt_cache_mode", message: 'must be one of "off"|"auto"|"explicit"' });
	}

	if (!a.overrides || typeof a.overrides !== "object" || Array.isArray(a.overrides)) {
		errors.push({ path: "$.overrides", message: "must be an object" });
	} else {
		const ov = a.overrides as Record<string, unknown>;
		for (const key of Object.keys(ov)) {
			if (!KNOWN_OVERRIDE_SET.has(key)) {
				errors.push({
					path: `$.overrides.${key}`,
					message: `unknown override key "${key}"; must be one of ${[...KNOWN_OVERRIDE_KEYS].join(", ")}`,
					arm: isStr(a.arm_id) ? a.arm_id : undefined,
				});
			} else if (key === "image_strategy") {
				// Only the documented placeholder is accepted; any other value
				// (including a typo'd "${...}") is rejected loudly.
				if (!isStr(ov.image_strategy)) {
					errors.push({ path: "$.overrides.image_strategy", message: "must be a string placeholder" });
				} else if (ov.image_strategy !== "${step1_winner}") {
					errors.push({
						path: "$.overrides.image_strategy",
						message: `unsupported placeholder "${ov.image_strategy}"; only "\${step1_winner}" is recognized (resolved via --image-arm)`,
					});
				}
			} else if (key === "overview_enabled") {
				if (typeof ov.overview_enabled !== "boolean") {
					errors.push({ path: "$.overrides.overview_enabled", message: "must be a boolean (Wave 2 placeholder)" });
				}
			} else if (key === "image_overlay_version") {
				if (!isStr(ov.image_overlay_version)) {
					errors.push({ path: "$.overrides.image_overlay_version", message: "must be a string" });
				}
			} else {
				if (typeof ov[key] !== "number" || !Number.isFinite(ov[key] as number)) {
					errors.push({ path: `$.overrides.${key}`, message: "must be a finite number" });
				}
			}
		}
	}

	if (errors.length) throw new ArmValidationError(errors);
	return obj as Arm;
}

// ------------------------------------------------------------------------- //
// File loader(s)
// ------------------------------------------------------------------------- ...

/** Read + parse + validate a single arm file. */
export function loadArm(filePath: string): Arm {
	const text = readFileSync(filePath, "utf8");
	let obj: unknown;
	try {
		obj = JSON.parse(text);
	} catch (e) {
		throw new Error(`arm file ${filePath} is not valid JSON: ${(e as Error).message}`);
	}
	return validateArm(obj);
}

/**
 * Load every arm file in a directory, optionally filtered by step. Returns the
 * arms in stable filename order (so the matrix is deterministic).
 */
export function loadArmDir(dir: string, step?: ArmStep): Arm[] {
	const files = readdirSync(dir)
		.filter((f) => f.endsWith(".json") && !f.includes(".schema."))
		.sort();
	const arms: Arm[] = [];
	const errors: ArmError[] = [];
	for (const f of files) {
		const full = join(dir, f);
		try {
			const arm = loadArm(full);
			if (step === undefined || arm.step === step) arms.push(arm);
		} catch (e) {
			if (e instanceof ArmValidationError) {
				errors.push(...e.errors);
			} else {
				errors.push({ path: full, message: (e as Error).message });
			}
		}
	}
	if (errors.length) throw new ArmValidationError(errors);
	return arms;
}

// ------------------------------------------------------------------------- //
// Step-2 image-strategy resolution + matrix
// ------------------------------------------------------------------------- ...

/**
 * Resolve a Step-2 arm's `image_strategy: "${step1_winner}"` placeholder by
 * copying the image-related overrides from the named Step-1 arm.
 *
 * @param arm a Step-2 arm
 * @param imageArm a Step-1 arm whose image overrides (overview_enabled,
 *   overview_long_edge, working/detail tiers) are copied into the Step-2 arm
 * @throws if `arm` is not Step-2, `imageArm` is not Step-1, or the Step-2 arm
 *   still carries an unresolved `${...}` placeholder after resolution
 */
export function resolveImageStrategy(arm: Arm, imageArm: Arm): ResolvedArm {
	if (arm.step !== 2) {
		throw new Error(`resolveImageStrategy: arm "${arm.arm_id}" is not Step 2 (step=${arm.step})`);
	}
	if (imageArm.step !== 1) {
		throw new Error(`resolveImageStrategy: imageArm "${imageArm.arm_id}" is not Step 1 (step=${imageArm.step})`);
	}
	const resolved: ArmOverrides = { ...arm.overrides };
	// Only the documented placeholder is resolvable; any other image_strategy
	// value was already rejected by validateArm, but defend here too so a
	// programmatically-built arm can't slip through.
	const placeholder = (arm.overrides as Record<string, unknown>).image_strategy;
	if (placeholder !== undefined && placeholder !== "${step1_winner}") {
		throw new Error(
			`arm "${arm.arm_id}" carries unsupported image_strategy placeholder "${String(placeholder)}"; only "\${step1_winner}" is recognized`,
		);
	}
	// Drop the placeholder, copy concrete image overrides from the Step-1 winner.
	delete (resolved as Record<string, unknown>).image_strategy;
	for (const key of IMAGE_STRATEGY_KEYS) {
		if (key in imageArm.overrides) {
			resolved[key as KnownOverrideKey] = imageArm.overrides[key as KnownOverrideKey];
		}
	}
	const r = { ...arm, resolvedOverrides: resolved, imageStrategySource: imageArm.arm_id };
	assertNoPlaceholders(r);
	return r;
}

/** Reject any leftover `${...}` placeholder in a resolved arm's overrides. */
function assertNoPlaceholders(arm: ResolvedArm): void {
	for (const [key, val] of Object.entries(arm.resolvedOverrides)) {
		if (typeof val === "string" && val.includes("${")) {
			throw new Error(
				`arm "${arm.arm_id}" has unresolved placeholder at overrides.${key}="${val}"; ` +
					`pass an explicit --image-arm pointing at a Step-1 arm id`,
			);
		}
	}
}

/**
 * Build the ordered experiment matrix for one step.
 *
 * Step 1: arms are resolved as-is (no image_strategy placeholder expected).
 * Step 2: each arm's `image_strategy: "${step1_winner}"` is resolved by copying
 *   the image overrides from `imageArm`. If any Step-2 arm carries the
 *   placeholder and `imageArm` is omitted, this throws loudly.
 */
export function buildStepMatrix(arms: Arm[], step: ArmStep, opts: { imageArm?: Arm } = {}): ExperimentMatrix {
	const stepArms = arms.filter((a) => a.step === step).sort((a, b) => a.arm_id.localeCompare(b.arm_id));
	if (stepArms.length === 0) {
		throw new Error(`no Step ${step} arms found`);
	}
	const resolved: ResolvedArm[] = stepArms.map((a) => {
		if (step === 1) {
			const r: ResolvedArm = { ...a, resolvedOverrides: { ...a.overrides } };
			assertNoPlaceholders(r);
			return r;
		}
		// Step 2: needs image-arm resolution when the placeholder is present.
		const needsImageArm = Object.entries(a.overrides).some(
			([, v]) => typeof v === "string" && v.includes("${"),
		);
		if (!needsImageArm) {
			const r: ResolvedArm = { ...a, resolvedOverrides: { ...a.overrides } };
			assertNoPlaceholders(r);
			return r;
		}
		if (!opts.imageArm) {
			throw new Error(
				`Step-2 arm "${a.arm_id}" carries an image_strategy placeholder but no --image-arm was provided`,
			);
		}
		return resolveImageStrategy(a, opts.imageArm);
	});
	return { step, arms: resolved };
}
