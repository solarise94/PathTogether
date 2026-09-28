//! Conversion result (`TransformResult`): output descriptor, per-level /
//! per-channel stats, transformed (edge) regions, warnings and a validation
//! summary. Designed so a C2 checkpoint can reconstruct committed progress
//! from the per-tile records (committed ranges are the same stream the
//! writers already keep on scratch storage).

use crate::kfb::KfbLevel;
use crate::kfbf::KfbfLevel;

pub const WARN_EDGE_REENCODE_FALLBACK_Q95: &str = "edge_reencode_fallback_q95";
pub const WARN_SPARSE_FILL_BLACK: &str = "sparse_fill_black";
pub const WARN_EXPOSURE_UNIT_ASSUMED_MS: &str = "exposure_unit_assumed_ms";

#[derive(Debug, Clone, Default)]
pub struct LevelStats {
    pub level: u32,
    pub width: u32,
    pub height: u32,
    pub tiles_across: u32,
    pub tiles_down: u32,
    pub tiles_total: u64,
    pub tiles_raw_copied: u64,
    pub tiles_reencoded: u64,
    /// Fluorescence only: sparse cells filled with black.
    pub cells_filled_black: u64,
    /// Fluorescence only: channel index the stats belong to (None for BF).
    pub channel: Option<usize>,
}

#[derive(Debug, Clone)]
pub struct EdgeRegion {
    pub level: u32,
    pub channel: Option<usize>,
    pub x: u32,
    pub y: u32,
    pub source_w: u32,
    pub source_h: u32,
    pub canvas_w: u32,
    pub canvas_h: u32,
    pub reused_qtables: bool,
}

#[derive(Debug, Clone, Default)]
pub struct ValidationReport {
    /// Structural self-checks the converter performed on the produced bytes
    /// (header backpatch present, IFD chain length, tile counts).
    pub ifd_count: u32,
    pub tile_records_emitted: u64,
    pub output_bytes: u64,
    pub checks_passed: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct ChannelSummary {
    pub index: usize,
    pub name: String,
    pub color_rgb: (u32, u32, u32),
    /// Raw calibration value; unit is ASSUMED milliseconds.
    pub exposure: f64,
    pub gamma: f64,
    /// Display window absorbed from a channel.json companion, if any.
    pub display_window: Option<(f64, f64)>,
    pub display_window_source: Option<String>,
}

#[derive(Debug, Clone)]
pub struct TransformResult {
    pub plan_version: u32,
    pub core_version: String,
    pub format: &'static str,
    pub output_bytes: u64,
    pub output_sha256: Option<String>,
    pub width: u32,
    pub height: u32,
    pub levels: Vec<LevelStats>,
    pub edge_regions: Vec<EdgeRegion>,
    pub warnings: Vec<String>,
    pub channels: Vec<ChannelSummary>,
    pub validation: ValidationReport,
    /// Raw IFD→(level, channel) mapping for structure comparison in tests.
    pub ifd_chain: Vec<(u32, Option<usize>)>,
    /// Associated images (name, payload bytes are NOT kept; offsets/lengths
    /// refer to the source; hosts copy them to sidecars).
    pub associated: Vec<AssociatedSummary>,
    /// Elapsed wall time in seconds.
    pub elapsed_seconds: f64,
}

#[derive(Debug, Clone)]
pub struct AssociatedSummary {
    pub name: String,
    pub source_offset: u64,
    pub source_length: u32,
    pub width: u32,
    pub height: u32,
}

impl TransformResult {
    pub fn count_reencoded(&self) -> u64 {
        self.levels.iter().map(|l| l.tiles_reencoded).sum()
    }
    pub fn count_raw_copied(&self) -> u64 {
        self.levels.iter().map(|l| l.tiles_raw_copied).sum()
    }
}

pub fn level_stats_from_kfb(lv: &KfbLevel) -> LevelStats {
    LevelStats {
        level: lv.level,
        width: lv.width,
        height: lv.height,
        tiles_across: lv.tiles_across(),
        tiles_down: lv.tiles_down(),
        tiles_total: (lv.tiles_across() as u64) * (lv.tiles_down() as u64),
        ..Default::default()
    }
}

pub fn level_stats_from_kfbf(lv: &KfbfLevel, channel: usize) -> LevelStats {
    LevelStats {
        level: lv.level,
        width: lv.width,
        height: lv.height,
        tiles_across: lv.tiles_across(),
        tiles_down: lv.tiles_down(),
        tiles_total: (lv.tiles_across() as u64) * (lv.tiles_down() as u64),
        channel: Some(channel),
        ..Default::default()
    }
}
