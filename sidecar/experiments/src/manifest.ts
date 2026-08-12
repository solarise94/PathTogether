/**
 * Phase 4 A/B framework (Wave 2: execution runner) — pinned manifest loader.
 *
 * Reads + validates a `manifest.json` produced by `fixtures/generate.py --pin`
 * against the manifest.schema.json shape (hand-rolled, no ajv runtime dep —
 * mirrors taskset.ts). The runner uses the manifest to:
 *   - cross-check every task's `fixture_id` against the pinned fixture set;
 *   - resolve ground-truth region bboxes for `bbox_revisit` rubric assertions;
 *   - read each fixture's slide filename + fingerprint for the real FlaskClient.
 *
 * NOTE: experiment data plane only — NOT built into the shipped sidecar bundle.
 */
import { readFileSync } from "node:fs";

// ------------------------------------------------------------------------- //
// Types (mirror manifest.schema.json)
// ------------------------------------------------------------------------- //

export interface ManifestRegion {
	label: string;
	x: number;
	y: number;
	w: number;
	h: number;
	density?: string;
}

export interface ManifestFixture {
	fixture_id: string;
	file: string;
	size_bytes: number;
	sha256: string;
	fingerprint: string;
	width: number;
	height: number;
	level_downsamples: number[];
	mpp: number | null;
	regions: ManifestRegion[];
	tags: string[];
}

export interface Manifest {
	manifest_version: 1;
	generated_at: string;
	fixtures: ManifestFixture[];
}

// ------------------------------------------------------------------------- //
// Error
// ------------------------------------------------------------------------- //

export interface ManifestError {
	path: string;
	message: string;
}

export class ManifestValidationError extends Error {
	readonly errors: ManifestError[];
	constructor(errors: ManifestError[]) {
		super(`manifest validation failed:\n${errors.map((e) => `  - [${e.path}] ${e.message}`).join("\n")}`);
		this.name = "ManifestValidationError";
		this.errors = errors;
	}
}

// ------------------------------------------------------------------------- //
// Validator
// ------------------------------------------------------------------------- //

function isStr(v: unknown): v is string {
	return typeof v === "string";
}
function isNum(v: unknown): v is number {
	return typeof v === "number" && Number.isFinite(v);
}
function isStrArray(v: unknown): v is string[] {
	return Array.isArray(v) && v.every(isStr);
}
function isNumArray(v: unknown): v is number[] {
	return Array.isArray(v) && v.every(isNum);
}

/**
 * Validate a parsed manifest object. Returns the typed {@link Manifest} or
 * throws {@link ManifestValidationError} with every located problem.
 *
 * A placeholder manifest (fingerprints `PIN-REQUIRED:0`) is REJECTED here —
 * the runner requires a real pinned manifest so the Flask fingerprint matches.
 */
export function validateManifest(obj: unknown): Manifest {
	const errors: ManifestError[] = [];
	if (!obj || typeof obj !== "object") {
		throw new ManifestValidationError([{ path: "$", message: "manifest must be an object" }]);
	}
	const root = obj as Record<string, unknown>;

	if (root.manifest_version !== 1) errors.push({ path: "$.manifest_version", message: "must be the number 1" });
	if (!isStr(root.generated_at)) errors.push({ path: "$.generated_at", message: "must be a string" });

	const fixtures = root.fixtures;
	if (!Array.isArray(fixtures) || fixtures.length === 0) {
		errors.push({ path: "$.fixtures", message: "must be a non-empty array" });
		if (errors.length) throw new ManifestValidationError(errors);
	}

	const seenIds = new Set<string>();
	(fixtures as unknown[]).forEach((fx, i) => {
		const fpath = `$.fixtures[${i}]`;
		if (!fx || typeof fx !== "object") {
			errors.push({ path: fpath, message: "fixture must be an object" });
			return;
		}
		const f = fx as Record<string, unknown>;
		for (const key of ["fixture_id", "file", "sha256", "fingerprint", "regions", "tags"]) {
			if (!(key in f)) errors.push({ path: fpath, message: `missing required field "${key}"` });
		}
		for (const key of ["size_bytes", "width", "height"]) {
			if (!(key in f)) errors.push({ path: fpath, message: `missing required field "${key}"` });
		}
		if (!isStr(f.fixture_id)) {
			errors.push({ path: `${fpath}.fixture_id`, message: "must be a non-empty string" });
		} else if (seenIds.has(f.fixture_id)) {
			errors.push({ path: `${fpath}.fixture_id`, message: `duplicate fixture_id "${f.fixture_id}"` });
		} else {
			seenIds.add(f.fixture_id);
		}
		if (!isStr(f.file)) errors.push({ path: `${fpath}.file`, message: "must be a non-empty string" });
		if (!isStr(f.fingerprint)) {
			errors.push({ path: `${fpath}.fingerprint`, message: "must be a string" });
		} else if (f.fingerprint.startsWith("PIN-REQUIRED")) {
			// A pinned manifest carries real fingerprints (mtime_ns:size from
			// Flask). The placeholder example is rejected so the runner fails
			// loudly instead of running against a mismatched Flask.
			errors.push({ path: `${fpath}.fingerprint`, message: "placeholder fingerprint (PIN-REQUIRED) — run generate.py --pin against the live Flask first" });
		}
		if (!isStr(f.sha256)) errors.push({ path: `${fpath}.sha256`, message: "must be a string" });
		if (!isNum(f.size_bytes) || f.size_bytes < 0) errors.push({ path: `${fpath}.size_bytes`, message: "must be a non-negative number" });
		if (!isNum(f.width) || f.width < 1) errors.push({ path: `${fpath}.width`, message: "must be a number >= 1" });
		if (!isNum(f.height) || f.height < 1) errors.push({ path: `${fpath}.height`, message: "must be a number >= 1" });
		if (!isNumArray(f.level_downsamples) || f.level_downsamples.length === 0) {
			errors.push({ path: `${fpath}.level_downsamples`, message: "must be a non-empty array of numbers" });
		}
		if (f.mpp !== null && !isNum(f.mpp)) errors.push({ path: `${fpath}.mpp`, message: "must be a number or null" });
		if (!isStrArray(f.tags)) errors.push({ path: `${fpath}.tags`, message: "must be an array of strings" });
		if (!Array.isArray(f.regions)) {
			errors.push({ path: `${fpath}.regions`, message: "must be an array" });
		} else {
			f.regions.forEach((r, j) => {
				const rpath = `${fpath}.regions[${j}]`;
				if (!r || typeof r !== "object") {
					errors.push({ path: rpath, message: "region must be an object" });
					return;
				}
				const rr = r as Record<string, unknown>;
				if (!isStr(rr.label) || rr.label.length === 0) errors.push({ path: `${rpath}.label`, message: "must be a non-empty string" });
				for (const key of ["x", "y", "w", "h"]) {
					if (!isNum(rr[key]) || (rr[key] as number) < 0) errors.push({ path: `${rpath}.${key}`, message: "must be a non-negative number" });
				}
				if (rr.density !== undefined && !isStr(rr.density)) errors.push({ path: `${rpath}.density`, message: "must be a string when present" });
			});
		}
	});

	if (errors.length) throw new ManifestValidationError(errors);
	return obj as Manifest;
}

// ------------------------------------------------------------------------- //
// File loader + helpers
// ------------------------------------------------------------------------- //

/** Read + JSON.parse + validate a manifest file. Throws on any failure. */
export function loadManifest(filePath: string): Manifest {
	const text = readFileSync(filePath, "utf8");
	let obj: unknown;
	try {
		obj = JSON.parse(text);
	} catch (e) {
		throw new Error(`manifest file ${filePath} is not valid JSON: ${(e as Error).message}`);
	}
	return validateManifest(obj);
}

/** Map of fixture_id → ManifestFixture for O(1) lookup. */
export function indexManifest(manifest: Manifest): Map<string, ManifestFixture> {
	const idx = new Map<string, ManifestFixture>();
	for (const f of manifest.fixtures) idx.set(f.fixture_id, f);
	return idx;
}

/**
 * Ground-truth region bboxes (label → bbox) for one fixture, used by the
 * rubric's `bbox_revisit` assertions.
 */
export function groundTruthRegions(fixture: ManifestFixture): Record<string, { x: number; y: number; w: number; h: number }> {
	const out: Record<string, { x: number; y: number; w: number; h: number }> = {};
	for (const r of fixture.regions) {
		out[r.label] = { x: r.x, y: r.y, w: r.w, h: r.h };
	}
	return out;
}
