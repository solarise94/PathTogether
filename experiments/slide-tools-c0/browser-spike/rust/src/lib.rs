//! C0 browser spike: minimal single-threaded WASM chunk processor.
//!
//! Realistic per-chunk work for measuring the WASM heap / copy path of the
//! future slide-transform engine: the JS side hands us a chunk read from the
//! input File, wasm-bindgen copies it into WASM linear memory, we transform it
//! into a second in-heap buffer (simulating tile staging), and the JS side
//! copies the result back out and writes it to OPFS. All three copies count
//! against the engine memory budget and are therefore deliberately kept.
//!
//! No threads, no SharedArrayBuffer: COOP/COEP must NOT be required.

use wasm_bindgen::prelude::*;

const XOR_MASK: u8 = 0x5A;

#[wasm_bindgen]
pub struct ChunkProcessor {
    staging: Vec<u8>,
    out: Vec<u8>,
    total_processed: u64,
    chunks: u64,
}

#[wasm_bindgen]
impl ChunkProcessor {
    /// `capacity` is the maximum chunk length (bytes). The WASM heap holds
    /// two buffers of this size (input staging + transformed output).
    #[wasm_bindgen(constructor)]
    pub fn new(capacity: u32) -> Result<ChunkProcessor, JsError> {
        if capacity == 0 || capacity > (1 << 30) {
            return Err(JsError::new("capacity out of range"));
        }
        let capacity = capacity as usize;
        Ok(ChunkProcessor {
            staging: vec![0u8; capacity],
            out: vec![0u8; capacity],
            total_processed: 0,
            chunks: 0,
        })
    }

    /// Copies `src` into WASM staging (copy #1, done by the wasm-bindgen
    /// glue when the JS buffer is outside linear memory), transforms
    /// staging -> out (simulated decode/encode work), and the glue copies
    /// `dst` back out to JS memory (copy #2).
    ///
    /// Returns the FNV-1a 64-bit hash of the *transformed output* bytes so
    /// the JS side can record per-chunk checksums for later verification.
    /// Offsets/lengths never go through this API as 32-bit values; the JS
    /// bridge owns file offsets.
    pub fn process_into(&mut self, src: &[u8], dst: &mut [u8]) -> u64 {
        let n = src.len().min(dst.len()).min(self.staging.len());
        self.staging[..n].copy_from_slice(&src[..n]);
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        for i in 0..n {
            let b = self.staging[i].rotate_left(3) ^ XOR_MASK;
            self.out[i] = b;
            h ^= b as u64;
            h = h.wrapping_mul(0x100_0000_01b3);
        }
        dst[..n].copy_from_slice(&self.out[..n]);
        self.total_processed += n as u64;
        self.chunks += 1;
        h
    }

    /// WASM-side managed heap bytes (the two staging buffers).
    pub fn heap_bytes(&self) -> u32 {
        (self.staging.len() + self.out.len()) as u32
    }

    pub fn total_processed(&self) -> u64 {
        self.total_processed
    }

    pub fn chunks(&self) -> u64 {
        self.chunks
    }
}

// The FNV-1a 64 transform contract is mirrored in site/common.js
// (transformByte/fnv1a64) and in the input generator (make-input.js);
// changing one requires changing all three.
