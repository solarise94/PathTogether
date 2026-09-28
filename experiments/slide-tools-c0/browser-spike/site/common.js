// Shared byte-pattern / transform / hash contract between:
//   - make-input.js  (generates the >4 GiB input file on disk)
//   - compute-worker.js (records checksums of what WASM produced)
//   - engine.js + verification (recomputes expected bytes after reopen)
// and the Rust side (rust/src/lib.rs). Change one -> change all.
//
// Input byte at absolute offset i is defined by a position-dependent mix of
// the word index floor(i/4); offsets are handled as ECMAScript Numbers that
// are safe integers (< 2^53). NOTE: bitwise ops truncate to 32 bits, so the
// word index uses Math.floor(i/4), never (i >>> 2) -- that would alias
// offsets >= 2^32 onto low offsets and silently pass a broken 4 GiB test.

export const XOR_MASK = 0x5a;

export function h32(x) {
  x >>>= 0;
  x ^= x >>> 16; x = Math.imul(x, 0x85ebca6b) >>> 0;
  x ^= x >>> 13; x = Math.imul(x, 0xc2b2ae35) >>> 0;
  x ^= x >>> 16;
  return x >>> 0;
}

// Deterministic input byte at absolute offset i (safe integer, i < 2^53).
export function inputByteAt(i) {
  const w = h32(Math.floor(i / 4) ^ 0x5bf03635);
  return (w >>> ((i % 4) * 8)) & 0xff;
}

// Expected OUTPUT byte for the input byte at offset i: rotl8(b,3) ^ 0x5A
// (mirrors Rust `b.rotate_left(3) ^ XOR_MASK`).
export function transformByte(b) {
  return ((((b << 3) | (b >>> 5)) & 0xff) ^ XOR_MASK) >>> 0;
}

export function expectedOutputByteAt(inputOffset) {
  return transformByte(inputByteAt(inputOffset));
}

// Fill u8 (length len) with expected output bytes for input [inOff, inOff+len).
export function fillExpectedOutput(u8, inOff) {
  for (let k = 0; k < u8.length; k++) u8[k] = transformByte(inputByteAt(inOff + k));
  return u8;
}

// FNV-1a 64 over bytes; BigInt, for small cross-checks only (slow on purpose
// of not adding dependencies; matches Rust process_into return value).
export function fnv1a64(u8) {
  let h = 0xcbf29ce484222325n;
  for (let i = 0; i < u8.length; i++) {
    h ^= BigInt(u8[i]);
    h = (h * 0x100000001b3n) & 0xffffffffffffffffn;
  }
  return h;
}

// Deterministic xorshift32 PRNG so chunk plans are reproducible from a seed.
export function makeRng(seed) {
  let s = seed >>> 0;
  return function next() {
    s ^= s << 13; s >>>= 0;
    s ^= s >>> 17;
    s ^= s << 5; s >>>= 0;
    return s;
  };
}

// Assert a file offset stays a safe integer (no 32-bit truncation anywhere).
export function assertSafeOffset(n, what) {
  if (!Number.isInteger(n) || n < 0 || n > Number.MAX_SAFE_INTEGER) {
    throw new Error(`unsafe offset for ${what}: ${n}`);
  }
  return n;
}
