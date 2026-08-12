/**
 * Phase 4 A/B arm loader + matrix tests (Wave 1).
 *
 * Covers: matrix generation for both steps; typo'd override key rejected
 * loudly; step-2 image-arm resolution copies image overrides + rejects an
 * unresolved placeholder.
 */
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
	buildStepMatrix,
	KNOWN_OVERRIDE_KEYS,
	loadArm,
	loadArmDir,
	resolveImageStrategy,
	validateArm,
	ArmValidationError,
	type Arm,
} from "../experiments/src/arms.js";

const ARMS_DIR = join(__dirname, "..", "experiments", "arms");

function step1Arm(): Arm {
	return {
		arm_id: "s1-x",
		step: 1,
		overrides: { overview_enabled: true, overview_long_edge: 768, context_window_tokens: 272000 },
		prompt_cache_mode: "auto",
	};
}
function step2Arm(): Arm {
	return {
		arm_id: "s2-x",
		step: 2,
		overrides: { image_strategy: "${step1_winner}", context_window_tokens: 400000 },
		prompt_cache_mode: "auto",
	};
}

describe("committed arm files", () => {
	it("all 6 arm files load and validate", () => {
		const arms = loadArmDir(ARMS_DIR);
		const ids = arms.map((a) => a.arm_id).sort();
		expect(ids).toEqual([
			"step1-overview-1024",
			"step1-overview-768",
			"step1-overview-none",
			"step2-window-272k",
			"step2-window-400k",
			"step2-window-512k",
		]);
		// step1 arms fix context window at 272k; step2 arms vary it.
		const s1 = arms.filter((a) => a.step === 1);
		const s2 = arms.filter((a) => a.step === 2);
		expect(s1.length).toBe(3);
		expect(s2.length).toBe(3);
		for (const a of s1) expect(a.overrides.context_window_tokens).toBe(272000);
	});

	it("committed step1 arm list includes the overview_enabled placeholder key", () => {
		expect(KNOWN_OVERRIDE_KEYS).toContain("overview_enabled");
		const none = loadArm(join(ARMS_DIR, "step1-overview-none.json"));
		expect(none.overrides.overview_enabled).toBe(false);
	});
});

describe("validateArm rejection", () => {
	it("rejects a typo'd override key with the known-key list in the message", () => {
		const bad = { ...step1Arm(), overrides: { overview_long_edg: 768 } };
		expect(() => validateArm(bad)).toThrow(ArmValidationError);
		try {
			validateArm(bad);
		} catch (e) {
			const msg = (e as Error).message;
			expect(msg).toContain("unknown override key");
			expect(msg).toContain("overview_long_edg");
			expect(msg).toContain("overview_long_edge");
		}
	});

	it("rejects a bad step value", () => {
		const bad = { ...step1Arm(), step: 3 as 1 };
		expect(() => validateArm(bad)).toThrow(/step.*1 or 2/);
	});

	it("rejects a non-number override value", () => {
		const bad = { ...step1Arm(), overrides: { overview_long_edge: "big" } };
		expect(() => validateArm(bad)).toThrow(/must be a finite number/);
	});
});

describe("buildStepMatrix", () => {
	it("Step 1 matrix: arms resolved as-is, no placeholders", () => {
		const arms = [
			step1Arm(),
			{ ...step1Arm(), arm_id: "s1-b", overrides: { overview_enabled: true, overview_long_edge: 1024, context_window_tokens: 272000 } },
		];
		const mx = buildStepMatrix(arms, 1);
		expect(mx.step).toBe(1);
		expect(mx.arms.length).toBe(2);
		// sorted by arm_id
		expect(mx.arms[0]!.arm_id).toBe("s1-b");
		expect(mx.arms[0]!.resolvedOverrides.overview_long_edge).toBe(1024);
	});

	it("Step 2 matrix resolves image_strategy from the --image-arm", () => {
		const imageArm = step1Arm(); // overview_long_edge 768, overview_enabled true
		const mx = buildStepMatrix([step2Arm()], 2, { imageArm });
		expect(mx.arms.length).toBe(1);
		const r = mx.arms[0]!;
		expect(r.imageStrategySource).toBe("s1-x");
		// image overrides copied from the step-1 winner
		expect(r.resolvedOverrides.overview_long_edge).toBe(768);
		expect(r.resolvedOverrides.overview_enabled).toBe(true);
		// placeholder dropped, context window kept
		expect("image_strategy" in r.resolvedOverrides).toBe(false);
		expect(r.resolvedOverrides.context_window_tokens).toBe(400000);
	});

	it("Step 2 throws loudly when a placeholder arm has no --image-arm", () => {
		expect(() => buildStepMatrix([step2Arm()], 2)).toThrow(/no --image-arm was provided/);
	});

	it("resolveImageStrategy copies image overrides and rejects a leftover placeholder", () => {
		const r = resolveImageStrategy(step2Arm(), step1Arm());
		expect(r.resolvedOverrides.overview_long_edge).toBe(768);
		expect(r.imageStrategySource).toBe("s1-x");
		// an arm still carrying a ${...} after resolution fails
		const stillBad: Arm = {
			arm_id: "s2-y",
			step: 2,
			overrides: { image_strategy: "${other}", context_window_tokens: 272000 },
			prompt_cache_mode: "auto",
		};
		expect(() => resolveImageStrategy(stillBad, step1Arm())).toThrow(/unsupported image_strategy placeholder/);
	});

	it("resolveImageStrategy rejects cross-step misuse", () => {
		expect(() => resolveImageStrategy(step1Arm(), step1Arm())).toThrow(/not Step 2/);
		expect(() => resolveImageStrategy(step2Arm(), step2Arm() as unknown as Arm)).toThrow(/not Step 1/);
	});
});
