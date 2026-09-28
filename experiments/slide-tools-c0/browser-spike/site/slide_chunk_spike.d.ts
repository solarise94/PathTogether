/* tslint:disable */
/* eslint-disable */

export class ChunkProcessor {
    free(): void;
    [Symbol.dispose](): void;
    chunks(): bigint;
    /**
     * WASM-side managed heap bytes (the two staging buffers).
     */
    heap_bytes(): number;
    /**
     * `capacity` is the maximum chunk length (bytes). The WASM heap holds
     * two buffers of this size (input staging + transformed output).
     */
    constructor(capacity: number);
    /**
     * Copies `src` into WASM staging (copy #1, done by the wasm-bindgen
     * glue when the JS buffer is outside linear memory), transforms
     * staging -> out (simulated decode/encode work), and the glue copies
     * `dst` back out to JS memory (copy #2).
     *
     * Returns the FNV-1a 64-bit hash of the *transformed output* bytes so
     * the JS side can record per-chunk checksums for later verification.
     * Offsets/lengths never go through this API as 32-bit values; the JS
     * bridge owns file offsets.
     */
    process_into(src: Uint8Array, dst: Uint8Array): bigint;
    total_processed(): bigint;
}

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_chunkprocessor_free: (a: number, b: number) => void;
    readonly chunkprocessor_chunks: (a: number) => bigint;
    readonly chunkprocessor_heap_bytes: (a: number) => number;
    readonly chunkprocessor_new: (a: number) => [number, number, number];
    readonly chunkprocessor_process_into: (a: number, b: number, c: number, d: number, e: number, f: any) => bigint;
    readonly chunkprocessor_total_processed: (a: number) => bigint;
    readonly __wbindgen_externrefs: WebAssembly.Table;
    readonly __externref_table_dealloc: (a: number) => void;
    readonly __wbindgen_malloc: (a: number, b: number) => number;
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
