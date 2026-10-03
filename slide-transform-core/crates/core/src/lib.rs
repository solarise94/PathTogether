//! `slide-transform-core` — shared transform core for browser/native slide
//! tools (C1). Pure logic; all IO goes through the traits in [`io`].
//!
//! Layout mirrors the Python oracle (`PathTogether/kfb/`):
//!   - [`kfb`]   brightfield KFB parsing (synthetic contract + KF-BIO vendor)
//!   - [`kfbf`]  fluorescence KFBF parsing (vendor layout, hybrid pyramid)
//!   - [`bigtiff`] brightfield classic multi-IFD BigTIFF writer
//!   - [`ome_writer`] fluorescence OME-BigTIFF writer (SubIFD pyramid)
//!   - [`convert_bf`] / [`convert_fl`] conversions driven by [`plan`]
//!   - [`jpeg`] hand-written libjpeg-faithful codec (decode+encode bit-exact
//!     with Pillow — the edge-tile quality gate is byte equality by
//!     construction; evidence in `docs/slide-tools/c1-core-report.md`)
//!
//! Byte-layout parity with the oracle is a hard requirement.

pub mod companion;
#[cfg(feature = "codecs")]
pub mod convert_bf;
#[cfg(feature = "codecs")]
pub mod convert_fl;
#[cfg(feature = "codecs")]
pub mod convert_svs;
pub mod error;
pub mod estimate;
pub mod io;
pub mod job;
pub mod kfb;
pub mod kfbf;
pub mod ome;
pub mod ome_writer;
pub mod paged_index;
pub mod pagereader;
pub mod plan;
pub mod report;
pub mod resume;
pub mod validate;
pub mod bigtiff;
pub mod tiff_read;
#[cfg(feature = "codecs")]
pub mod svs;

#[cfg(feature = "codecs")]
pub mod jpeg;

#[cfg(feature = "codecs")]
pub mod synth_gen;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod svs_fixture;

pub use error::{CoreError, CoreResult};
pub use plan::{OutputProfile, PixelPolicy, TransformPlan};


/// Core crate version (embedded in TransformPlan and reports).
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");
