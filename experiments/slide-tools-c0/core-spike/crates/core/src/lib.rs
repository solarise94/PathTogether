//! slide-transform-core-spike — C0 part ② Rust core spike.
//!
//! Pure-format crate: KFB brightfield parsing (synthetic `kfb_bf_v1` +
//! KF-BIO vendor layout) and a JPEG-passthrough tiled BigTIFF pyramid
//! writer, with all IO behind the [`io`] traits so the same code runs
//! natively (file-backed) and under wasm32 (browser adapters in part ③).
//!
//! Memory bound (spike guarantee, measured and recorded in the ADR):
//! in-memory state is O(levels), never O(tiles) or O(file size) —
//!   * tile index: per-level occupancy bitmaps ≤ 16 × 782×782 bits ≈ 1.2 MiB
//!     + per-level present counters; records live in scratch storage,
//!   * tile payloads: one ≤ 8 MiB payload buffer at a time,
//!   * IFD tile offset/count arrays: streamed from scratch at finish,
//!   * parse/convert page the index in fixed 128–256 KiB pages.
//!
//! The Python oracle (`PathTogether/kfb/`) is the behavioral reference:
//! error codes, validation order, level selection, sampling rules, edge
//! re-encode policy and the output TIFF byte layout all mirror it.

pub mod bigtiff;
pub mod convert;
pub mod edge;
pub mod error;
pub mod io;
pub mod jpeg;
pub mod kfb;
pub mod paged_index;
pub mod pagereader;
#[cfg(feature = "edge-reencode")]
pub mod synth_gen;

pub use convert::{convert_kfb, ConvertOptions, ConvertStats, LevelStat};
pub use error::{CoreError, CoreResult};
pub use io::{ByteSource, RandomAccessSink, ScratchFactory};
