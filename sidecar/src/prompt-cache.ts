/**
 * AI reading assistant sidecar — explicit Prompt Cache (Phase 3, §8/§13).
 *
 * Promotes the implicit Phase 1/2 stable-prefix reuse into an OPT-IN explicit
 * prompt-cache capability with safe runtime downgrade. This module is the
 * single source of truth for:
 *
 *   - capability resolution ({@link resolvePromptCacheCapabilities}): how the
 *     configured {@link PromptCacheMode} maps to provider-level flags, WITHOUT
 *     inferring capability from the upstream model name (§8.1: "CPA 是否透传
 *     相关字段必须通过实际请求验证，不能只根据上游模型名称推断");
 *   - the cache-key construction ({@link buildPromptCacheKey}): the §5.1
 *     `svs-viewer:{session_id}:{slide_fingerprint}:g{generation}` form, whose
 *     generation field is bumped by checkpoint version changes (§10) so the key
 *     naturally invalidates on any schema/prompt/slide/encoding change;
 *   - the OpenAI-style request-body field name ({@link PROMPT_CACHE_KEY_FIELD})
 *     we inject for explicit mode — centrally defined so a CPA field-name
 *     mismatch is a one-line fix;
 *   - rejection detection ({@link isCacheFieldRejection}) and the downgrade
 *     event ({@link CapabilityDowngradeReason}) used by the retry wrapper.
 *
 * IMPORTANT — CPA validation status:
 *   We have NO access to a real CPA gateway in this environment. Every
 *   "CPA passes `prompt_cache_key` through" claim is therefore UNVERIFIED end-
 *   to-end. The implementation is capability-detection + runtime-downgrade
 *   driven: explicit mode OPTIMISTICALLY sends the field, and if the provider
 *   rejects it (§13: "Provider 拒绝缓存字段") the retry wrapper strips it and
 *   retries once. All unverified points are flagged with
 *   {@link CPA_UNVERIFIED} in comments and in the Phase 3 deviation report.
 */
import type { PromptCacheMode } from "./metrics.js";

// =========================================================================== //
// Types (§8.1)
// =========================================================================== //

export type { PromptCacheMode };

/**
 * Resolved provider-level prompt-cache capability for one run (§8.1).
 *
 *   - `mode`: the EFFECTIVE mode after resolution (may differ from config on
 *     downgrade — see {@link CapabilityDowngradeReason}).
 *   - `supportsCacheKey`: can we send a session cache key in the request body?
 *     For `explicit` this starts OPTIMISTICALLY true and is flipped to false by
 *     the retry wrapper on a runtime rejection (§13). `auto`/`off` never set
 *     this true (we do not emit the provider-specific field).
 *   - `supportsBreakpoints`: can we tag a stable-region breakpoint? For the
 *     OpenAI-completions CPA we serve, the answer is NO — OpenAI-compatible
 *     endpoints do automatic prefix caching with no explicit breakpoint marker;
 *     the Anthropic-style `cache_control` breakpoint only fires when
 *     `compat.cacheControlFormat === "anthropic"` (pi-ai openai-completions.js
 *     getCompatCacheControl), which our CPA compat does not set. So Phase 3
 *     explicitly drops breakpoint support for the openai-completions path and
 *     leaves `supportsBreakpoints = false`. The anthropic-messages path is not
 *     wired in the sidecar yet (agent-runner.defaultStreamFnForConfig throws),
 *     so we do not handle it here.
 *   - `supportsUsageMetrics`: does the provider return cacheRead/cacheWrite in
 *     usage? Probed at runtime — defaults to false/unknown until the provider
 *     actually returns nonzero cache usage (§12: "如果 CPA 不返回缓存 usage，
 *     指标必须标记 unknown").
 */
export interface PromptCacheCapabilities {
	mode: PromptCacheMode;
	supportsCacheKey: boolean;
	supportsBreakpoints: boolean;
	supportsUsageMetrics: boolean;
}

// =========================================================================== //
// CPA field-name constants (UNVERIFIED — see module docstring)
// =========================================================================== //

/**
 * UNVERIFIED against a real CPA gateway. The OpenAI-compatible field name we
 * inject into the request body in explicit mode. OpenAI's documented field is
 * `prompt_cache_key` (≤ 64 chars, see pi-ai/dist/api/openai-prompt-cache.js
 * `clampOpenAIPromptCacheKey` / `OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH`). If a real
 * CPA turns out to expect a different field name (e.g. a vendor-prefixed form),
 * change ONLY this constant — the retry-wrapper downgrade logic keys off
 * {@link isCacheFieldRejection} which matches on this name.
 */
export const PROMPT_CACHE_KEY_FIELD = "prompt_cache_key" as const;

/**
 * Maximum length for the cache key value, matching OpenAI's documented
 * `prompt_cache_key` limit (pi-ai openai-prompt-cache.js). We truncate to this
 * length defensively; the generated key (§5.1) is well under it in practice.
 */
export const PROMPT_CACHE_KEY_MAX_LENGTH = 64;

/**
 * Marker comment used to flag CPA-unverified behaviour. Search the codebase for
 * this token to enumerate every point that needs real-gateway validation.
 */
export const CPA_UNVERIFIED = "CPA-UNVERIFIED" as const;

// =========================================================================== //
// Capability resolution (§8.1)
// =========================================================================== //

/**
 * Provider-side info available at capability-resolution time. Phase 3 keeps
 * this minimal: the protocol narrows the breakpoint support decision, and the
 * cache-key support decision is deferred to runtime (§8.1: explicit mode's
 * "启用依赖运行时验证").
 */
export interface ProviderInfo {
	/** The pi model.api we dispatch on ("openai-completions" | "anthropic-messages" | ...). */
	apiProtocol?: string;
}

/**
 * Resolve prompt-cache capabilities from the run config (§8.1).
 *
 * Resolution rules:
 *   - `off`: ALL supports* flags false. We still run image/context budgeting
 *     but make NO cache claim (§8.1: "仅执行图片和上下文预算，不宣称缓存命中收益").
 *   - `auto`: no provider-specific field is emitted, but the stable-prefix +
 *     variable-suffix STRUCTURE is preserved (Phase 2b's checkpoint + assembler
 *     already guarantees this). supportsUsageMetrics stays false/unknown until
 *     the provider returns cache usage.
 *   - `explicit`: supportsCacheKey starts OPTIMISTICALLY true (we will try to
 *     send `prompt_cache_key` and downgrade on rejection, §13).
 *     supportsBreakpoints is FALSE for openai-completions (automatic prefix
 *     caching; no breakpoint marker). supportsUsageMetrics is runtime-probed.
 *
 * Design note (§8.1): we deliberately do NOT inspect the upstream model name
 * to infer capability. The CPA gateway may swap the real upstream under us; the
 * only authoritative signal is whether the provider accepts or rejects the
 * field at runtime. So explicit mode is "try + downgrade", not "know in
 * advance".
 */
export function resolvePromptCacheCapabilities(
	configPromptCacheMode: PromptCacheMode | string | undefined,
	providerInfo: ProviderInfo,
): PromptCacheCapabilities {
	const mode = normalizePromptCacheMode(configPromptCacheMode);
	if (mode === "off") {
		return { mode: "off", supportsCacheKey: false, supportsBreakpoints: false, supportsUsageMetrics: false };
	}
	if (mode === "auto") {
		// auto: keep the stable structure, emit no provider-specific field.
		// supportsUsageMetrics is a runtime probe — starts false.
		return { mode: "auto", supportsCacheKey: false, supportsBreakpoints: false, supportsUsageMetrics: false };
	}
	// explicit: optimistic cache key; breakpoints unsupported on openai-completions.
	// The anthropic-messages path is not wired in the sidecar yet, so even if a
	// caller configured explicit + anthropic we do not claim breakpoint support
	// (the streamFn would throw before reaching here anyway).
	return {
		mode: "explicit",
		supportsCacheKey: true, // optimistic; runtime downgrade via §13
		// CPA-UNVERIFIED: openai-completions has no explicit breakpoint marker;
		// it relies on automatic prefix caching. Anthropic-style cache_control
		// breakpoints (pi getCompatCacheControl) require
		// compat.cacheControlFormat === "anthropic", which our CPA compat does
		// not set. We leave supportsBreakpoints false for BOTH protocols until
		// the anthropic-messages path is wired AND CPA-validated.
		supportsBreakpoints: false,
		supportsUsageMetrics: false, // runtime probed
	};
}

/**
 * Coerce a config value (string | undefined) into a normalized {@link
 * PromptCacheMode}. Falls back to "auto" (§11 default). The Flask + sidecar
 * run-boundary validators (Phase 1) already reject invalid enum values; this
 * is the defensive runtime normalization.
 */
export function normalizePromptCacheMode(v: string | undefined): PromptCacheMode {
	if (v === "off" || v === "auto" || v === "explicit") return v;
	return "auto";
}

// =========================================================================== //
// Cache-key construction (§5.1 / §10)
// =========================================================================== //

/**
 * Build the session-scoped prompt cache key (§5.1).
 *
 * Form: `svs-viewer:{session_id}:{slide_fingerprint}:g{generation}`
 *
 * Version invalidation (§10): the `generation` component is bumped by the
 * checkpoint builder on ANY of:
 *   - system_prompt_version change,
 *   - tool_schema_hash change,
 *   - request_schema_version change,
 *   - slide_fingerprint change (also reflected directly in the key),
 *   - overview encoding spec change,
 *   - force-compaction / summary rewrite.
 *
 * So a version-field change naturally produces a NEW cache key — the old key's
 * cached prefix is never silently reused for a different stable region (§10:
 * "版本字段变化导致 generation 变化时 key 自然失效").
 *
 * The key is truncated to {@link PROMPT_CACHE_KEY_MAX_LENGTH} defensively
 * (OpenAI's limit). In practice session_id + fingerprint + generation fit well
 * under 64 chars; truncation only matters for unusually long ids.
 */
export function buildPromptCacheKey(args: {
	sessionId: string;
	slideFingerprint: string;
	generation: number;
}): string {
	const raw = `svs-viewer:${args.sessionId}:${args.slideFingerprint}:g${args.generation}`;
	return truncateKey(raw);
}

function truncateKey(key: string): string {
	// Match pi-ai's clampOpenAIPromptCacheKey (Array.from for codepoint safety).
	const chars = Array.from(key);
	if (chars.length <= PROMPT_CACHE_KEY_MAX_LENGTH) return key;
	return chars.slice(0, PROMPT_CACHE_KEY_MAX_LENGTH).join("");
}

// =========================================================================== //
// Request-body injection (explicit mode)
// =========================================================================== //

/**
 * Build the samplingParams fragment that injects the cache key into the
 * openai-completions request body for explicit mode.
 *
 * Mechanism (pi-ai openai-completions.js:704-705): `Object.assign(params,
 * options.samplingParams)` runs LAST, AFTER the named request fields, so keys
 * here override pi's own `prompt_cache_key` (which pi leaves undefined for our
 * CPA — see buildParams at :523-525: the key is only set when
 * `baseUrl.includes("api.openai.com")` OR `cacheRetention === "long" &&
 * supportsLongCacheRetention`, neither of which holds for our CPA compat). So
 * samplingParams is the cleanest injection path that does not require forking
 * pi's streamSimple.
 *
 * Returns `null` when the capability is downgraded (supportsCacheKey=false) or
 * the mode is not explicit — the caller then passes no samplingParams
 * fragment, leaving the request body untouched (auto/off behaviour).
 *
 * CPA-UNVERIFIED: whether the CPA gateway forwards `prompt_cache_key` to the
 * real upstream (or strips/ignores it) is unverified end-to-end. The retry
 * wrapper's downgrade path ({@link isCacheFieldRejection}) handles the case
 * where the gateway rejects the field.
 */
export function buildCacheKeySamplingParams(
	capabilities: PromptCacheCapabilities,
	cacheKey: string,
): Record<string, unknown> | null {
	if (capabilities.mode !== "explicit" || !capabilities.supportsCacheKey) {
		return null;
	}
	return { [PROMPT_CACHE_KEY_FIELD]: cacheKey };
}

/**
 * Merge a cache-key samplingParams fragment into an existing options object
 * (or return a fresh one when `options` is null). Does NOT mutate the input.
 *
 * The merge precedence: existing samplingParams keys are preserved, and the
 * cache-key field is added/overwritten by ours (so the cache key wins even if
 * a caller happened to set the same field). This matches the §8.1 explicit
 * contract: "发送 session cache key".
 */
export function mergeCacheKeyOptions(
	options: Record<string, unknown> | null | undefined,
	cacheKeyParams: Record<string, unknown> | null,
): Record<string, unknown> {
	const base = options ? { ...options } : {};
	if (!cacheKeyParams) return base;
	const existingSampling = (base.samplingParams as Record<string, unknown> | undefined) ?? {};
	base.samplingParams = { ...existingSampling, ...cacheKeyParams };
	return base;
}

/**
 * Strip the cache-key field from an options object's samplingParams (the
 * downgrade-retry path, §13). Returns a NEW options object; does not mutate.
 *
 * Used by the retry wrapper when a provider rejects the cache field: we remove
 * `prompt_cache_key` from samplingParams and retry once WITHOUT consuming the
 * transient 3-attempt budget.
 */
export function stripCacheKeyOptions(
	options: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
	if (!options) return {};
	const base = { ...options };
	const sp = base.samplingParams as Record<string, unknown> | undefined;
	if (!sp) return base;
	const next = { ...sp };
	delete next[PROMPT_CACHE_KEY_FIELD];
	if (Object.keys(next).length === 0) {
		delete base.samplingParams;
	} else {
		base.samplingParams = next;
	}
	return base;
}

// =========================================================================== //
// Rejection detection + downgrade (§13)
// =========================================================================== //

/**
 * Reasons a capability was downgraded at runtime. Recorded in §12 metrics
 * (`capability_downgrade`) and emitted to console.warn so operators can see
 * when a CPA silently refuses prompt-cache fields.
 */
export type CapabilityDowngradeReason =
	| "cache_field_rejected"
	| "breakpoint_unsupported";

/**
 * Detect whether a provider error message indicates the cache field was
 * rejected (§13: "Provider 拒绝缓存字段"). The provider may return a 400/422
 * with a message mentioning the unrecognized field. We match case-insensitively
 * on the field name and common rejection phrasings, in both English and the
 * mixed Chinese/English error text a CPA might surface.
 *
 * False positives are acceptable: a false positive triggers ONE extra retry
 * without the field (which still succeeds); a false negative leaks a terminal
 * error to the user. So the matcher errs on the side of matching.
 */
export function isCacheFieldRejection(errorMessage: string): boolean {
	const lower = (errorMessage || "").toLowerCase();
	if (!lower) return false;
	// The field name itself (our centrally defined constant).
	const fieldNames = [PROMPT_CACHE_KEY_FIELD, "prompt_cache_key", "cache_key", "promptcachekey"];
	for (const fn of fieldNames) {
		if (lower.includes(fn.toLowerCase())) {
			// Confirm it looks like a REJECTION (not a successful cache hit log).
			if (looksLikeRejection(lower)) return true;
		}
	}
	// Broader: "unrecognized" / "unknown parameter" + "cache" anywhere.
	if (lower.includes("cache") && looksLikeRejection(lower)) return true;
	return false;
}

function looksLikeRejection(lower: string): boolean {
	const rejectMarkers = [
		"unrecognized", // "unrecognized request argument"
		"unknown parameter",
		"unknown argument",
		"unexpected keyword", // Python-style "unexpected keyword argument"
		"invalid request",
		"not supported",
		"unsupported",
		"unallowed", // some gateways say "unallowed extra fields"
		"additional property", // JSON-schema-style "additional properties not allowed"
		"无法识别", // Chinese: "unrecognized"
		"不支持", // Chinese: "not supported"
		"未知参数", // Chinese: "unknown parameter"
		"非法参数", // Chinese: "illegal parameter"
	];
	for (const m of rejectMarkers) {
		if (lower.includes(m)) return true;
	}
	// HTTP status hints in the 400/422 range (some clients surface these).
	if (/\b(400|422)\b/.test(lower)) return true;
	return false;
}

/**
 * Apply a cache-field downgrade to a capabilities object (§13). Returns a NEW
 * capabilities object with `supportsCacheKey = false` and the effective mode
 * recorded as "auto" (we keep the stable-prefix structure but stop claiming
 * explicit cache benefits). The original object is not mutated.
 *
 * The retry wrapper calls this once per logical call when a rejection is
 * detected; subsequent requests in the same run reuse the downgraded
 * capabilities so we do not repeatedly provoke the provider.
 */
export function downgradeCacheKeyCapability(
	capabilities: PromptCacheCapabilities,
): PromptCacheCapabilities {
	return {
		...capabilities,
		supportsCacheKey: false,
		// Effective mode is "auto" for metrics: we still have a stable prefix,
		// we just no longer emit the explicit field.
		mode: capabilities.mode === "explicit" ? "auto" : capabilities.mode,
	};
}
