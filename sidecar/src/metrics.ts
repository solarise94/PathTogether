/**
 * AI reading assistant sidecar — structured metrics sink (Phase 2b, §12).
 *
 * Provides a lightweight, content-free structured-metrics channel for per-model-
 * request observability (§12). The sink accepts a metrics record and emits it
 * as a JSON line on `console.info` by default, or forwards it to an injected
 * sink. Metrics NEVER include image content, base64 payloads, or API keys.
 *
 * The metrics fields mirror §12's required set:
 *
 *   session_id, checkpoint_generation, stable_prefix_hash prefix, prompt_cache_mode,
 *   input_tokens, cached/cacheWrite ("unknown" when the provider returns none),
 *   selected_images, materialized_images, evicted_image_refs, image_lru_hits/misses,
 *   overview_image_bytes_sent, working_set_image_bytes_sent, prepared_request_bytes,
 *   transform_ms, region_fetch_ms, compaction_reason, derivative_hash_mismatch,
 *   checkpoint_rebuild_reason.
 *
 * Derived ratios (prompt_cache_hit_ratio, image_lru_hit_ratio, etc.) are computed
 * downstream from these fields.
 */
export type PromptCacheMode = "off" | "auto" | "explicit";

/**
 * One per-request metrics record (§12). All token counts are best-effort
 * estimates; provider-dependent fields (cached_tokens / cacheWrite) are
 * "unknown" when the provider returns no cache usage.
 */
export interface RequestMetrics {
	session_id: string;
	checkpoint_generation: number;
	stable_prefix_hash_prefix: string;
	prompt_cache_mode: PromptCacheMode;
	input_tokens: number | "unknown";
	cached_tokens: number | "unknown";
	cache_write_tokens: number | "unknown";
	selected_images: number;
	materialized_images: number;
	evicted_image_refs: string[];
	image_lru_hits: number;
	image_lru_misses: number;
	overview_image_bytes_sent: number;
	working_set_image_bytes_sent: number;
	prepared_request_bytes: number;
	transform_ms: number;
	region_fetch_ms: number;
	compaction_reason: string | null;
	derivative_hash_mismatch: number;
	checkpoint_rebuild_reason: string | null;
}

/**
 * Sink function type. The default emits a JSON line on console.info; tests can
 * inject a capturing sink.
 */
export type MetricsSink = (metrics: RequestMetrics) => void;

/**
 * The default sink: a single JSON line on `console.info`, prefixed with a
 * stable tag so log shippers can route it. Never throws.
 */
export const defaultMetricsSink: MetricsSink = (metrics) => {
	try {
		console.info(`[ai-metrics] ${JSON.stringify(metrics)}`);
	} catch {
		// best-effort; never crash a request on a logging failure
	}
};

/**
 * Merge assembler-side and provider-side metrics into a complete record (§12).
 * The assembler produces the image/transform/region fields; the provider/usage
 * path produces the token/cache fields. This helper fills the gaps with
 * "unknown" / 0 so the sink always receives a well-formed record.
 */
export function buildRequestMetrics(args: {
	session_id: string;
	checkpoint_generation: number;
	stable_prefix_hash: string;
	prompt_cache_mode: PromptCacheMode;
	transform_ms: number;
	region_fetch_ms: number;
	selected_images: number;
	materialized_images: number;
	evicted_image_refs: string[];
	image_lru_hits: number;
	image_lru_misses: number;
	overview_image_bytes_sent: number;
	working_set_image_bytes_sent: number;
	prepared_request_bytes: number;
	compaction_reason?: string | null;
	checkpoint_rebuild_reason?: string | null;
	derivative_hash_mismatch?: number;
	usage?: {
		input?: number;
		cacheRead?: number;
		cacheWrite?: number;
	} | null;
}): RequestMetrics {
	const u = args.usage || null;
	return {
		session_id: args.session_id,
		checkpoint_generation: args.checkpoint_generation,
		stable_prefix_hash_prefix: (args.stable_prefix_hash || "").slice(0, 16),
		prompt_cache_mode: args.prompt_cache_mode,
		input_tokens: u && typeof u.input === "number" ? u.input : "unknown",
		cached_tokens: u && typeof u.cacheRead === "number" ? u.cacheRead : "unknown",
		cache_write_tokens: u && typeof u.cacheWrite === "number" ? u.cacheWrite : "unknown",
		selected_images: args.selected_images,
		materialized_images: args.materialized_images,
		evicted_image_refs: args.evicted_image_refs,
		image_lru_hits: args.image_lru_hits,
		image_lru_misses: args.image_lru_misses,
		overview_image_bytes_sent: args.overview_image_bytes_sent,
		working_set_image_bytes_sent: args.working_set_image_bytes_sent,
		prepared_request_bytes: args.prepared_request_bytes,
		transform_ms: args.transform_ms,
		region_fetch_ms: args.region_fetch_ms,
		compaction_reason: args.compaction_reason ?? null,
		derivative_hash_mismatch: args.derivative_hash_mismatch ?? 0,
		checkpoint_rebuild_reason: args.checkpoint_rebuild_reason ?? null,
	};
}
