/* tslint:disable */
/* eslint-disable */

/**
 * Host capability flags (call before convert): bit 0 = stHostReadInto.
 */
export function configure(read_into: boolean): void;

/**
 * Run a conversion writing to the host sink. `strict_lossless` toggles the
 * pixel policy; `channel_json` may be empty (no companion).
 */
export function convert(strict_lossless: boolean, channel_json: string): string;

/**
 * Run a conversion with an explicit output profile id (`bf-classic`,
 * `bf-ome`, `fl-ome`; empty = the input's pre-profile default). Encoding is
 * preserve-source-v1 (pre-U3 behaviour kept bit-for-bit).
 */
export function convertProfile(profile: string, strict_lossless: boolean, channel_json: string): string;

/**
 * Run a conversion with explicit output AND encoding profile ids (U3).
 * `encoding`: `preserve-source-v1` (default) or `compact-jpeg-v1`
 * (brightfield only).
 */
export function convertProfileEncoded(profile: string, encoding: string, strict_lossless: boolean, channel_json: string): string;

/**
 * Bundle conversion with explicit output AND encoding profile ids (F3).
 */
export function convertProfileEncodedBundle(profile: string, encoding: string, strict_lossless: boolean, channel_json: string): string;

/**
 * Resume a conversion from a checkpoint state (the same JSON
 * `stHostCheckpoint` emits; journal-recorded by the runner).
 */
export function convertResume(resume_json: string, strict_lossless: boolean, channel_json: string): string;

/**
 * Resume under an explicit output profile; refused when the checkpoint
 * state was committed under a different one.
 */
export function convertResumeProfile(resume_json: string, profile: string, strict_lossless: boolean, channel_json: string): string;

/**
 * Resume under explicit output AND encoding profiles (U3); refused when the
 * checkpoint state was committed under a different combination — including
 * a compact request against a legacy (preserve, no field) state.
 */
export function convertResumeProfileEncoded(resume_json: string, profile: string, encoding: string, strict_lossless: boolean, channel_json: string): string;

/**
 * Bundle resume under explicit profiles (F3); refused when the checkpoint
 * state was committed under another profile/encoding/adapter combination.
 */
export function convertResumeProfileEncodedBundle(resume_json: string, profile: string, encoding: string, strict_lossless: boolean, channel_json: string): string;

export function coreVersion(): string;

export function enableCheckpoint(): void;

/**
 * Piggyback a sha256 over every source byte read through `ByteSource`
 * during the next conversion (identity capture without an extra pass).
 */
export function enableSourceHash(): void;

/**
 * Re-open + validate the finished output (streamed sha256 + structural
 * IFD walk) through the host read-back callbacks. Only a passing result
 * may be marked `ready`. `expect_ifd` is the converter's `ifd_count` (main
 * chain + SubIFDs, every profile); 0 skips the equality.
 */
export function finalizeValidate(expect_ifd: number): string;

/**
 * Probe the input through host reads; returns a JSON string. Includes the
 * C2 disk-precheck estimate (`estimate.output_upper_bound_bytes` etc.).
 */
export function probe(): string;

/**
 * Probe a bundle input (F3 MRXS) through the bundle host callbacks.
 */
export function probeBundle(): string;

/**
 * One dedicated pass: sha256 of the whole source through bounded host
 * reads (resume identity verification; hashing flag stays off).
 */
export function sha256Source(): string;

export function sourceSha256(): string;

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly configure: (a: number) => void;
    readonly convert: (a: number, b: number, c: number) => [number, number];
    readonly convertProfile: (a: number, b: number, c: number, d: number, e: number) => [number, number];
    readonly convertProfileEncoded: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => [number, number];
    readonly convertProfileEncodedBundle: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => [number, number];
    readonly convertResume: (a: number, b: number, c: number, d: number, e: number) => [number, number];
    readonly convertResumeProfile: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => [number, number];
    readonly convertResumeProfileEncoded: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number, i: number) => [number, number];
    readonly convertResumeProfileEncodedBundle: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number, i: number) => [number, number];
    readonly coreVersion: () => [number, number];
    readonly enableCheckpoint: () => void;
    readonly enableSourceHash: () => void;
    readonly finalizeValidate: (a: number) => [number, number];
    readonly probe: () => [number, number];
    readonly probeBundle: () => [number, number];
    readonly sha256Source: () => [number, number];
    readonly sourceSha256: () => [number, number];
    readonly __wbindgen_malloc: (a: number, b: number) => number;
    readonly __wbindgen_realloc: (a: number, b: number, c: number, d: number) => number;
    readonly __wbindgen_exn_store: (a: number) => void;
    readonly __externref_table_alloc: () => number;
    readonly __wbindgen_externrefs: WebAssembly.Table;
    readonly __wbindgen_free: (a: number, b: number, c: number) => void;
    readonly __wbindgen_start: () => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
