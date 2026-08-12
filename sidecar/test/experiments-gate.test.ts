/**
 * Phase 4 NO-GO gate tests (Wave 1).
 *
 * scripted mode always allowed; real-model throws without the env var (citing
 * CPA-UNVERIFIED + §14 Phase 3) and passes when PHASE4_CPA_VERIFIED=1.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
	assertDataCollectionAllowed,
	DataCollectionGateError,
	isDataCollectionAllowed,
	PHASE4_CPA_VERIFIED_ENV,
} from "../experiments/src/gate.js";

const ENV = { [PHASE4_CPA_VERIFIED_ENV]: "1" };
const EMPTY = {};

describe("assertDataCollectionAllowed", () => {
	it("scripted mode is always allowed (no env needed)", () => {
		expect(() => assertDataCollectionAllowed("scripted", EMPTY)).not.toThrow();
		expect(isDataCollectionAllowed("scripted", EMPTY)).toBe(true);
	});

	it("real-model throws without the env var and cites §14 Phase 3 + CPA-UNVERIFIED", () => {
		expect(() => assertDataCollectionAllowed("real-model", EMPTY)).toThrow(DataCollectionGateError);
		try {
			assertDataCollectionAllowed("real-model", EMPTY);
			expect.fail("should have thrown");
		} catch (e) {
			const msg = (e as Error).message;
			expect(msg).toContain("CPA-UNVERIFIED");
			expect(msg).toContain("prompt_cache_key");
			expect(msg).toContain("§14 Phase 3");
			expect(msg).toContain(PHASE4_CPA_VERIFIED_ENV);
		}
		expect(isDataCollectionAllowed("real-model", EMPTY)).toBe(false);
	});

	it("real-model passes when PHASE4_CPA_VERIFIED=1", () => {
		expect(() => assertDataCollectionAllowed("real-model", ENV)).not.toThrow();
		expect(isDataCollectionAllowed("real-model", ENV)).toBe(true);
	});

	it("real-model rejects any value other than exactly '1'", () => {
		expect(() => assertDataCollectionAllowed("real-model", { [PHASE4_CPA_VERIFIED_ENV]: "true" })).toThrow();
		expect(() => assertDataCollectionAllowed("real-model", { [PHASE4_CPA_VERIFIED_ENV]: "0" })).toThrow();
	});
});

describe("assertDataCollectionAllowed does not depend on process.env", () => {
	afterEach(() => {
		delete process.env[PHASE4_CPA_VERIFIED_ENV];
	});

	it("uses the explicit env arg, not process.env", () => {
		process.env[PHASE4_CPA_VERIFIED_ENV] = "1";
		// explicit empty env still blocks real-model even though process.env has it.
		expect(() => assertDataCollectionAllowed("real-model", EMPTY)).toThrow();
	});
});
