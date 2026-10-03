/* tslint:disable */
/* eslint-disable */
export const memory: WebAssembly.Memory;
export const configure: (a: number) => void;
export const convert: (a: number, b: number, c: number) => [number, number];
export const convertProfile: (a: number, b: number, c: number, d: number, e: number) => [number, number];
export const convertProfileEncoded: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => [number, number];
export const convertResume: (a: number, b: number, c: number, d: number, e: number) => [number, number];
export const convertResumeProfile: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => [number, number];
export const convertResumeProfileEncoded: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number, i: number) => [number, number];
export const coreVersion: () => [number, number];
export const enableCheckpoint: () => void;
export const enableSourceHash: () => void;
export const finalizeValidate: (a: number) => [number, number];
export const probe: () => [number, number];
export const sha256Source: () => [number, number];
export const sourceSha256: () => [number, number];
export const __wbindgen_malloc: (a: number, b: number) => number;
export const __wbindgen_realloc: (a: number, b: number, c: number, d: number) => number;
export const __wbindgen_exn_store: (a: number) => void;
export const __externref_table_alloc: () => number;
export const __wbindgen_externrefs: WebAssembly.Table;
export const __wbindgen_free: (a: number, b: number, c: number) => void;
export const __wbindgen_start: () => void;
