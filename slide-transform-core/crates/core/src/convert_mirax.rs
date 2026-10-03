//! MRXS bundle → brightfield conversion (F3): compose output tiles from the
//! source images at their true positions, then encode.
//!
//! **What `preserve-source-v1` means for MRXS** (stated honestly, unlike a
//! byte-passthrough claim): an MIRAX level is a *mosaic* of overlapping
//! camera images with fractional placement, so an output tile is never a
//! copy of one source image. `preserve` therefore means: compose each output
//! tile from the source images at their computed positions (the same model
//! OpenSlide renders), then re-encode the composed tile at the documented
//! high-fidelity setting **YCbCr 4:2:2 · quality 96 · standard Huffman**
//! (fingerprint `MRAX_PRESERVE_COMPOSE_FINGERPRINT`, reported in the result
//! as `composed`, in OME-XML provenance and the classic description JSON).
//! Passthrough-only-where-exact would apply to no tile of a real bundle
//! (camera overlaps always straddle tile boundaries), so no path claims it.
//! `compact-jpeg-v1` composes identically and re-encodes with the locked U3
//! parameters.
//!
//! Sparse/missing areas are filled with the level's `IMAGE_FILL_COLOR_BGR`
//! (never silently white: the colour comes from Slidedat, and the count is
//! reported). A bad index, a missing member or a decode failure is a typed
//! error — never a filled tile.
//!
//! Strict-lossless is refused: composition + JPEG re-encode is inherently
//! lossy for this format (typed `pixel_policy_violation` before any output
//! byte, same rule as compact+strict).
//!
//! Resume mirrors the other adapters: checkpoints per committed output tile
//! row; skipped cells are re-counted from the placement index without
//! decoding; output stays byte-identical to an uninterrupted run.

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::budget::MemBudget;
use crate::bundle::BundleFs;
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::error::{CoreError, CoreResult};
use crate::io::{RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::jpeg;
use crate::mirax::{
    probe_mirax_with_budget, MiraxDoc, Placement, ADAPTER_VERSION, SOURCE_FORMAT,
};
use crate::ome::py_repr_f64;
use crate::ome_writer::{OmeBigTiffWriter, RgbIfdExtras};
use crate::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan, COMPACT_JPEG_V1_FINGERPRINT,
    COMPACT_JPEG_V1_HUFFMAN, COMPACT_JPEG_V1_QUALITY,
};
use crate::report::{
    AssociatedSummary, ComposedSummary, LevelStats, LossyReencode, TransformResult,
};
use crate::resume::ResumePoint;

pub use crate::convert_bf::{FORMAT_CLASSIC, FORMAT_OME_RGB};

/// Row-boundary progress + checkpoint emission shared by the two tile-loop
/// arms (real tiles and deduplicated fill tiles).
macro_rules! maybe_progress_and_checkpoint {
    ($writer:expr, $job:expr, $stats:expr, $li:expr, $cell:expr, $total:expr, $row:expr) => {
        if $cell % $stats.tiles_across as u64 == 0 || $cell == $total {
            $job.progress.on_progress(&Progress {
                unit: ProgressUnit::TileRow,
                level: $li as u32,
                channel: None,
                done: $row,
                total: $stats.tiles_down as u64,
                committed_bytes: $writer.cursor(),
            });
            if $job.checkpoint_enabled() {
                $job.emit_checkpoint(
                    $li as u32,
                    None,
                    $cell,
                    $writer.cursor(),
                    $writer.ifd_tile_counts(),
                );
            }
        }
    };
}

/// Output tile edge of the composed pyramid.
pub const OUT_TILE: u32 = 256;
/// Locked preserve-mode compose parameters (documented high-fidelity).
pub const MRAX_PRESERVE_COMPOSE_QUALITY: u8 = 96;
pub const MRAX_PRESERVE_COMPOSE_SAMPLING: jpeg::Sampling = jpeg::Sampling::S422;
pub const MRAX_PRESERVE_COMPOSE_HUFFMAN: &str = "standard-annex-k";
pub const MRAX_PRESERVE_COMPOSE_FINGERPRINT: &str = "mirax-preserve-compose:q96:y422:hstd:v1";

pub const WARN_SPARSE_FILL: &str = "mirax_sparse_fill";
pub const WARN_ASSOC_NOT_EXPORTED: &str = "mirax_associated_not_exported";
pub const WARN_NO_ICC: &str = "color_management_not_applied";

/// Bounded decoded-image cache (bytes; per level).
const IMAGE_CACHE_BYTES: usize = 32 * 1024 * 1024;

fn preserve_cfg() -> jpeg::EncoderCfg {
    // YCbCr 4:2:2 at the locked quality: the (2,1) TIFF layout is the same
    // one the KFB outputs use and every reader (Bio-Formats included — the
    // F3 interop gate) decodes; only YCbCr 4:4:4 tiles rendered as garbage
    // there, which forced an earlier RGB draft.
    jpeg::EncoderCfg::with_quality(MRAX_PRESERVE_COMPOSE_QUALITY, MRAX_PRESERVE_COMPOSE_SAMPLING)
}

// --------------------------------------------------------------------------- //
// image cache
// --------------------------------------------------------------------------- //

struct ImageCache<'a> {
    fs: &'a dyn BundleFs,
    doc: &'a MiraxDoc,
    level: usize,
    entries: std::collections::HashMap<u32, jpeg::DecodedImage>,
    /// bytes charged against the budget per cached image (true-up'd to the
    /// real size after decode; released on eviction and on drop)
    charged: std::collections::HashMap<u32, u64>,
    order: std::collections::VecDeque<u32>,
    bytes: usize,
    expect_w: u32,
    expect_h: u32,
    decodes: u64,
    budget: &'a mut MemBudget,
}

impl Drop for ImageCache<'_> {
    fn drop(&mut self) {
        // whatever is still cached is freed here
        self.budget.release(self.bytes as u64);
        self.bytes = 0;
        self.charged.clear();
    }
}

impl<'a> ImageCache<'a> {
    fn new(
        fs: &'a dyn BundleFs,
        doc: &'a MiraxDoc,
        level: usize,
        budget: &'a mut MemBudget,
    ) -> Self {
        ImageCache {
            fs,
            doc,
            level,
            entries: Default::default(),
            charged: Default::default(),
            order: Default::default(),
            bytes: 0,
            expect_w: doc.levels[level].section.image_w as u32,
            expect_h: doc.levels[level].section.image_h as u32,
            decodes: 0,
            budget,
        }
    }

    fn get(&mut self, img: u32) -> CoreResult<&jpeg::DecodedImage> {
        if !self.entries.contains_key(&img) {
            let ref_ = &self.doc.levels[self.level].images[img as usize];
            let raw = self
                .fs
                .read_member_at(ref_.member as usize, ref_.offset, ref_.length as usize)?;
            let probe = jpeg::scan_jpeg(&raw).map_err(|e| {
                CoreError::jpeg(format!(
                    "层 {} 图像 {} 不是合法 JPEG：{}",
                    self.level, img, e.message
                ))
            })?;
            if probe.sof_marker != 0xC0 && probe.sof_marker != 0xC1 {
                return Err(CoreError::variant(format!(
                    "层 {} 图像 {} 是渐进式/算术 JPEG（SOF {:02X}），不在支持集",
                    self.level, img, probe.sof_marker
                )));
            }
            let max_pixels = (self.expect_w as u64) * (self.expect_h as u64);
            // Review §1: decoded-pixel budget — the charge is refused BEFORE
            // any decode allocation. Estimate = cached RGB (3 B/px) + decoder
            // transient (component planes + upsample buffers, ≤ 3 B/px). A
            // single member whose DIGITIZER-declared pixel area cannot fit
            // the budget is a typed refusal, never an OOM mid-decode.
            let est = max_pixels.saturating_mul(6);
            self.budget.charge(est, "JPEG 解码（缓存图像 + 解码器临时）")?;
            let charge_raw = raw.len() as u64;
            self.budget.charge(charge_raw, "JPEG 原始载荷读取")?;
            // true colourspace from the stream's own markers (the MIRAX
            // camera JPEGs are JFIF YCbCr — mostly 4:2:2/4:2:0); only an
            // Adobe-transform-0 stream without JFIF decodes as RGB
            let force_rgb = !probe.jfif && probe.adobe_transform == Some(0);
            let dec = match jpeg::decode_ex(&raw, max_pixels.max(1 << 16), force_rgb) {
                Ok(d) => d,
                Err(e) => {
                    self.budget.release(est + charge_raw);
                    return Err(e);
                }
            };
            if dec.width != self.expect_w || dec.height != self.expect_h {
                self.budget.release(est + charge_raw);
                return Err(CoreError::variant(format!(
                    "层 {} 图像 {} 尺寸 {}×{} ≠ DIGITIZER {}×{}",
                    self.level,
                    img,
                    dec.width,
                    dec.height,
                    self.expect_w,
                    self.expect_h
                )));
            }
            let data = match dec.kind {
                jpeg::ColorKind::Rgb => dec.data,
                jpeg::ColorKind::Gray => {
                    let mut v = Vec::with_capacity(dec.data.len() * 3);
                    for &g in &dec.data {
                        v.extend_from_slice(&[g, g, g]);
                    }
                    v
                }
            };
            self.budget.release(charge_raw);
            let sz = data.len();
            self.budget.reconcile(est, sz as u64, "缓存图像数据")?;
            while self.bytes + sz > IMAGE_CACHE_BYTES {
                let evict = self
                    .order
                    .pop_front()
                    .ok_or_else(|| CoreError::validation("图像缓存不变量破坏"))?;
                if let Some(e) = self.entries.remove(&evict) {
                    self.bytes -= e.data.len();
                    if let Some(c) = self.charged.remove(&evict) {
                        self.budget.release(c);
                    }
                }
            }
            self.bytes += sz;
            self.charged.insert(img, sz as u64);
            self.order.push_back(img);
            self.decodes += 1;
            self.entries
                .insert(img, jpeg::DecodedImage { width: dec.width, height: dec.height, kind: jpeg::ColorKind::Rgb, data });
        }
        Ok(&self.entries[&img])
    }
}

// --------------------------------------------------------------------------- //
// composition
// --------------------------------------------------------------------------- //

/// Bucketed placement index over one level's output-tile grid (CSR).
struct TileBuckets {
    /// starts.len() == tiles + 1; starts[t]..starts[t+1] range into `items`
    starts: Vec<u32>,
    items: Vec<u32>,
    across: u32,
    down: u32,
}

impl TileBuckets {
    fn build(placements: &[Placement], lv_w: u32, lv_h: u32) -> TileBuckets {
        let across = lv_w.div_ceil(OUT_TILE) as i64;
        let down = lv_h.div_ceil(OUT_TILE) as i64;
        let mut counts = vec![0u32; (across * down) as usize + 1];
        // inclusive tile-range a placement's rect intersects
        let span = |v: i64, size: u32| -> (i64, i64) {
            let lo = v.div_euclid(OUT_TILE as i64);
            let hi = (v + size as i64 - 1).div_euclid(OUT_TILE as i64);
            (lo, hi)
        };
        let mut ranges: Vec<(i64, i64, i64, i64)> = Vec::with_capacity(placements.len());
        for p in placements {
            let (x0, x1) = span(p.dst.0, p.size.0);
            let (y0, y1) = span(p.dst.1, p.size.1);
            ranges.push((x0, x1, y0, y1));
            for ty in y0.max(0)..=y1.min(down - 1) {
                for tx in x0.max(0)..=x1.min(across - 1) {
                    counts[(ty * across + tx) as usize] += 1;
                }
            }
        }
        let mut starts = counts;
        let mut acc = 0u32;
        for c in starts.iter_mut() {
            let v = *c;
            *c = acc;
            acc += v;
        }
        starts.push(acc);
        let mut fill = starts.clone();
        let mut items = vec![0u32; acc as usize];
        for (i, &(x0, x1, y0, y1)) in ranges.iter().enumerate() {
            for ty in y0.max(0)..=y1.min(down - 1) {
                for tx in x0.max(0)..=x1.min(across - 1) {
                    let t = (ty * across + tx) as usize;
                    items[fill[t] as usize] = i as u32;
                    fill[t] += 1;
                }
            }
        }
        TileBuckets { starts, items, across: across as u32, down: down as u32 }
    }

    fn tiles(&self, t: usize) -> &[u32] {
        &self.items[self.starts[t] as usize..self.starts[t + 1] as usize]
    }

    fn is_empty_tile(&self, t: usize) -> bool {
        self.starts[t] == self.starts[t + 1]
    }
}

/// Compose one output tile into `canvas` (3×OUT_TILE×OUT_TILE).
fn compose_tile(
    cache: &mut ImageCache,
    placements: &[Placement],
    buckets: &TileBuckets,
    tile: usize,
    canvas: &mut [u8],
) -> CoreResult<bool> {
    let mut any = false;
    for &pi in buckets.tiles(tile) {
        let p = placements[pi as usize];
        let img = cache.get(p.img)?;
        any = true;
        let tx = (tile as u32 % buckets.across) as i64 * OUT_TILE as i64;
        let ty = (tile as u32 / buckets.across) as i64 * OUT_TILE as i64;
        // destination rows within the canvas
        let dst_x0 = p.dst.0.max(tx);
        let dst_y0 = p.dst.1.max(ty);
        let dst_x1 = (p.dst.0 + p.size.0 as i64).min(tx + OUT_TILE as i64);
        let dst_y1 = (p.dst.1 + p.size.1 as i64).min(ty + OUT_TILE as i64);
        if dst_x0 >= dst_x1 || dst_y0 >= dst_y1 {
            continue;
        }
        let iw64 = img.width as i64;
        let ih64 = img.height as i64;
        for dy in dst_y0..dst_y1 {
            let sy = dy - p.dst.1 + p.src.1 as i64;
            if sy < 0 || sy >= ih64 {
                continue;
            }
            let sx0 = dst_x0 - p.dst.0 + p.src.0 as i64;
            let wpx = (dst_x1 - dst_x0) as usize;
            // clip at the source edges (fractional-tail overrun): paint the
            // visible part only, like cairo EXTEND_NONE
            let clip = (-sx0).max(0);
            let sx = (sx0 + clip) as i64;
            if sx >= iw64 {
                continue;
            }
            let n = wpx
                .saturating_sub(clip as usize)
                .min((iw64 - sx).max(0) as usize);
            if n == 0 {
                continue;
            }
            let srow = sy as usize * iw64 as usize * 3 + sx as usize * 3;
            let drow = ((dy - ty) as usize) * OUT_TILE as usize * 3;
            let dx = (dst_x0 - tx) as usize + clip as usize;
            canvas[drow + dx * 3..drow + (dx + n) * 3]
                .copy_from_slice(&img.data[srow..srow + n * 3]);
        }
    }
    Ok(any)
}

/// Public bounded-region composer (test/validation API): compose
/// `[x, x+w) × [y, y+h)` of `level` without any re-encode. Mirrors the
/// conversion path's placement/rounding rules exactly.
pub fn compose_region(
    fs: &dyn BundleFs,
    doc: &MiraxDoc,
    level: usize,
    x: u32,
    y: u32,
    w: u32,
    h: u32,
) -> CoreResult<Vec<u8>> {
    if level >= doc.levels.len() {
        return Err(CoreError::validation("level 越界"));
    }
    let lv = &doc.levels[level];
    if x + w > lv.width || y + h > lv.height {
        return Err(CoreError::validation("ROI 越界"));
    }
    let fill = lv.section.fill_rgb;
    let mut budget = doc.budget.clone();
    budget.charge(
        (w as u64).saturating_mul(h as u64).saturating_mul(3),
        "ROI 画布",
    )?;
    let mut canvas = vec![0u8; w as usize * h as usize * 3];
    for px in canvas.chunks_exact_mut(3) {
        px.copy_from_slice(&fill);
    }
    charge_level_index(&mut budget, lv)?;
    let placements = doc.placements(level);
    let mut cache = ImageCache::new(fs, doc, level, &mut budget);
    for p in &placements {
        let x0 = (p.dst.0).max(x as i64);
        let y0 = (p.dst.1).max(y as i64);
        let x1 = (p.dst.0 + p.size.0 as i64).min((x + w) as i64);
        let y1 = (p.dst.1 + p.size.1 as i64).min((y + h) as i64);
        if x0 >= x1 || y0 >= y1 {
            continue;
        }
        let img = cache.get(p.img)?;
        let iw = img.width as i64;
        let ih = img.height as i64;
        for dy in y0..y1 {
            let sy = dy - p.dst.1 + p.src.1 as i64;
            if sy < 0 || sy >= ih {
                continue;
            }
            let sx = x0 - p.dst.0 + p.src.0 as i64;
            if sx < 0 || sx >= iw {
                continue;
            }
            // fractional-tail subtiles can overrun the source edge by 1px:
            // paint the visible part only (cairo EXTEND_NONE leaves the rest)
            let n = ((x1 - x0).min(iw - sx)) as usize;
            if n == 0 {
                continue;
            }
            let srow = sy as usize * iw as usize * 3 + sx as usize * 3;
            let drow = ((dy - y as i64) as usize) * w as usize * 3
                + (x0 - x as i64) as usize * 3;
            canvas[drow..drow + n * 3].copy_from_slice(&img.data[srow..srow + n * 3]);
        }
    }
    Ok(canvas)
}

// --------------------------------------------------------------------------- //
// writers
// --------------------------------------------------------------------------- //

struct LevelMeta {
    width: u32,
    height: u32,
    photometric: u16,
    tiff_sub: (u16, u16),
    reduced: bool,
    mpp: Option<(f64, f64)>,
}

enum MirxWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl MirxWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            MirxWriter::Classic { w, .. } => match committed {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            MirxWriter::Ome { w, ome_xml, levels } => {
                let desc = if m.reduced { None } else { ome_xml.take() };
                *levels += 1;
                w.begin_rgb_ifd_ex(
                    scratch,
                    m.width,
                    m.height,
                    m.reduced,
                    m.mpp,
                    m.tiff_sub,
                    desc,
                    committed,
                    &RgbIfdExtras {
                        tile: (OUT_TILE, OUT_TILE),
                        photometric: m.photometric,
                        jpeg_tables: None,
                        icc: None,
                    },
                )
            }
        }
    }

    fn end(&mut self, m: &LevelMeta, description: &[u8]) -> CoreResult<()> {
        match self {
            MirxWriter::Classic { w, .. } => w.end_level_ex(
                m.width,
                m.height,
                m.tiff_sub,
                m.mpp,
                description,
                m.reduced,
                &LevelExtras {
                    tile: (OUT_TILE, OUT_TILE),
                    photometric: m.photometric,
                    jpeg_tables: None,
                    icc: None,
                },
            ),
            MirxWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<()> {
        match self {
            MirxWriter::Classic { w, .. } => w.write_tile(data).map(|_| ()),
            MirxWriter::Ome { w, .. } => w.write_tile(data).map(|_| ()),
        }
    }

    /// Record a tile that references a previously written shared payload.
    fn write_tile_ref(&mut self, offset: u64, count: u32) -> CoreResult<()> {
        match self {
            MirxWriter::Classic { w, .. } => w.write_tile_ref(offset, count),
            MirxWriter::Ome { w, .. } => w.write_tile_ref(offset, count),
        }
    }

    /// Write a raw payload without a tile record (shared-payload prologue).
    fn write_payload(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        match self {
            MirxWriter::Classic { w, .. } => w.write_payload(data),
            MirxWriter::Ome { w, .. } => w.write_payload(data),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            MirxWriter::Classic { w, .. } => w.cursor(),
            MirxWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            MirxWriter::Classic { w, .. } => w.ifd_tile_counts(),
            MirxWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            MirxWriter::Classic { w, .. } => w.finish(),
            MirxWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

fn mirax_description_bytes(doc: &MiraxDoc) -> Vec<u8> {
    let (mx, my) = doc.mpp.unwrap_or((f64::NAN, f64::NAN));
    let fmt = |v: f64| {
        if v.is_finite() {
            py_repr_f64(v)
        } else {
            "null".into()
        }
    };
    let s = format!(
        "{{\"adapter\": \"{}\", \"adapter_version\": \"{}\", \"composed\": \"{}\", \"mpp_x\": {}, \"mpp_y\": {}, \"objective\": {}, \"source_format\": \"{}\"}}\u{0}",
        SOURCE_FORMAT,
        ADAPTER_VERSION,
        MRAX_PRESERVE_COMPOSE_FINGERPRINT,
        fmt(mx),
        fmt(my),
        doc.objective.map(py_repr_f64).unwrap_or_else(|| "null".into()),
        SOURCE_FORMAT,
    );
    s.into_bytes()
}

fn xml_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(c),
        }
    }
    out
}

fn mirax_ome_xml(doc: &MiraxDoc, plan: &TransformPlan) -> Vec<u8> {
    let main = &doc.levels[0];
    let mut provenance: Vec<(&str, String)> = vec![
        ("converter", "slide-transform-core".to_string()),
        ("converter_version", plan.core_version.clone()),
        ("output_profile", plan.profile.id().to_string()),
        ("source_format", SOURCE_FORMAT.to_string()),
        ("source_adapter", SOURCE_FORMAT.to_string()),
        ("adapter_version", ADAPTER_VERSION.to_string()),
        (
            "compose_mode",
            "mosaic-composition from camera images at true positions; every output tile \
             re-encoded (no byte passthrough exists for this format)"
                .to_string(),
        ),
        (
            "preserve_compose_fingerprint",
            MRAX_PRESERVE_COMPOSE_FINGERPRINT.to_string(),
        ),
    ];
    provenance.push((
        "mpp_source",
        if doc.mpp.is_some() { "slidedat-micrometer-per-pixel" } else { "unknown" }.to_string(),
    ));
    provenance.push((
        "objective_source",
        if doc.objective.is_some() { "slidedat-objective-magnification" } else { "unknown" }
            .to_string(),
    ));
    provenance.push((
        "position_source",
        match doc.position_source {
            crate::mirax::PositionSource::VimslideBuffer => "VIMSLIDE_POSITION_BUFFER".to_string(),
            crate::mirax::PositionSource::StitchingIntensity => {
                "StitchingIntensityLayer (deflate)".to_string()
            }
            crate::mirax::PositionSource::Synthesized => "synthesized-from-overlap".to_string(),
        },
    ));
    provenance.push(("pyramid_levels", doc.levels.len().to_string()));
    if plan.encoding == EncodingProfile::CompactJpegV1 {
        provenance.push(("encoding_profile", plan.encoding.id().to_string()));
        provenance.push(("encoding_params_fingerprint", COMPACT_JPEG_V1_FINGERPRINT.to_string()));
    }
    provenance.push((
        "tile_payloads",
        if plan.encoding == EncodingProfile::CompactJpegV1 {
            format!(
                "composed from source images at true positions, then re-encoded at the locked \
                 compact parameters (quality {}, subsampling {}, standard Annex-K Huffman, \
                 fingerprint {}); lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            format!(
                "composed from source images at true positions, then re-encoded at the \
                 documented high-fidelity compose setting (YCbCr 4:2:2, quality {}, \
                 standard Annex-K Huffman, fingerprint {}); lossy generation on a lossy \
                 source, never claimed lossless",
                MRAX_PRESERVE_COMPOSE_QUALITY,
                MRAX_PRESERVE_COMPOSE_FINGERPRINT
            )
        },
    ));
    provenance.push((
        "pixel_policy",
        match plan.pixel_policy {
            PixelPolicy::AllowEdgeReencode => "preserve-source-passthrough".to_string(),
            PixelPolicy::StrictLossless => "strict-lossless".to_string(),
        },
    ));
    let has_objective = doc.objective.is_some_and(|m| m.is_finite() && m > 0.0);
    let instrument = if has_objective {
        format!(
            "<Instrument ID=\"Instrument:0\"><Objective ID=\"Objective:0:0\" NominalMagnification=\"{}\"/></Instrument>",
            crate::ome::py_g17(doc.objective.unwrap_or(0.0))
        )
    } else {
        String::new()
    };
    let refs = if has_objective {
        "<InstrumentRef ID=\"Instrument:0\"/><ObjectiveSettings ID=\"Objective:0:0\"/>"
    } else {
        ""
    };
    let mut kv = String::new();
    for (k, v) in &provenance {
        kv.push_str(&format!("<M K=\"{}\">{}</M>", xml_escape(k), xml_escape(v)));
    }
    let (phys, ann_ref, ann) = match doc.mpp {
        Some((mx, my)) if mx.is_finite() && mx > 0.0 && my.is_finite() && my > 0.0 => (
            format!(
                " PhysicalSizeX=\"{}\" PhysicalSizeXUnit=\"µm\" PhysicalSizeY=\"{}\" PhysicalSizeYUnit=\"µm\"",
                py_repr_f64(mx),
                py_repr_f64(my)
            ),
            "<AnnotationRef ID=\"Annotation:0\"/>".to_string(),
            format!(
                "<StructuredAnnotations><MapAnnotation ID=\"Annotation:0\" Namespace=\"{}\"><Value>{kv}</Value></MapAnnotation></StructuredAnnotations>",
                crate::ome::PROVENANCE_NS
            ),
        ),
        _ => (
            String::new(),
            "<AnnotationRef ID=\"Annotation:0\"/>".to_string(),
            format!(
                "<StructuredAnnotations><MapAnnotation ID=\"Annotation:0\" Namespace=\"{}\"><Value>{kv}</Value></MapAnnotation></StructuredAnnotations>",
                crate::ome::PROVENANCE_NS
            ),
        ),
    };
    let xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
<OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\" \
xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" \
xsi:schemaLocation=\"http://www.openmicroscopy.org/Schemas/OME/2016-06 http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd\">\
{instrument}<Image ID=\"Image:0\">{refs}\
<Pixels ID=\"Pixels:0\" DimensionOrder=\"XYCZT\" Type=\"uint8\" SignificantBits=\"8\" Interleaved=\"true\" \
SizeX=\"{}\" SizeY=\"{}\" SizeC=\"3\" SizeZ=\"1\" SizeT=\"1\"{phys}>\
<Channel ID=\"Channel:0:0\" SamplesPerPixel=\"3\"/>\
<TiffData IFD=\"0\" PlaneCount=\"1\"/>\
</Pixels>{ann_ref}</Image>{ann}</OME>",
        main.width, main.height,
    );
    let mut out = xml.into_bytes();
    out.push(0);
    out
}

fn level_meta(doc: &MiraxDoc, li: usize, compact: bool) -> LevelMeta {
    let lv = &doc.levels[li];
    let main = &doc.levels[0];
    let mpp = doc.mpp.map(|(mx, my)| {
        let rx = main.width as f64 / lv.width as f64;
        let ry = main.height as f64 / lv.height as f64;
        (mx * rx, my * ry)
    });
    let (photometric, tiff_sub) = if compact {
        (6u16, compact_sampling_tiff())
    } else {
        (6u16, (2u16, 1u16)) // preserve compose: YCbCr 4:2:2
    };
    LevelMeta { width: lv.width, height: lv.height, photometric, tiff_sub, reduced: li > 0, mpp }
}

// --------------------------------------------------------------------------- //
// conversion
// --------------------------------------------------------------------------- //

#[allow(clippy::too_many_arguments)]
pub fn convert_mirax_to_bigtiff(
    fs: &dyn BundleFs,
    stem: &str,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(fs, stem, sink, scratch, plan, job, None)
}

#[allow(clippy::too_many_arguments)]
pub fn convert_mirax_to_bigtiff_resume(
    fs: &dyn BundleFs,
    stem: &str,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    convert_inner(fs, stem, sink, scratch, plan, job, Some(resume))
}

/// Review §1: checked byte estimate for one level's placement/bucket
/// structures, charged BEFORE they are built. The caller releases the amount
/// when the level's structures are dropped.
fn charge_level_index(budget: &mut MemBudget, lv: &crate::mirax::MiraxLevel) -> CoreResult<u64> {
    let tpi2 =
        lv.params.tiles_per_image.saturating_mul(lv.params.tiles_per_image) as u64;
    let pl = (lv.images.len() as u64).saturating_mul(tpi2);
    let across = (lv.width as u64).div_ceil(OUT_TILE as u64);
    let down = (lv.height as u64).div_ceil(OUT_TILE as u64);
    let tiles = across.checked_mul(down).ok_or_else(|| {
        CoreError::resource_limit("输出 tile 网格数溢出")
    })?;
    // a placement intersects at most (span/256 + 2) tiles per axis
    let sw = (lv.params.tile_w.ceil().max(1.0)) as u64;
    let sh = (lv.params.tile_h.ceil().max(1.0)) as u64;
    let ix = (sw / OUT_TILE as u64 + 2).min(across).max(1);
    let iy = (sh / OUT_TILE as u64 + 2).min(down).max(1);
    // order vec element (16 B key + 40 B placement) + stable-sort temporary
    // (≤ half) + the collected Vec<Placement> (40 B)
    let est_placements = pl.saturating_mul(56 + 28 + 40);
    let est_ranges = pl.saturating_mul(32); // (x0,x1,y0,y1) i64 quad
    let est_csr = tiles.saturating_mul(12).saturating_add(4); // counts+starts+fill
    let est_items = pl.saturating_mul(ix).saturating_mul(iy).saturating_mul(4);
    budget.charge(est_placements, "placement 排序与副本")?;
    budget.charge(est_ranges, "CSR ranges")?;
    budget.charge(est_csr, "CSR counts/starts/fill")?;
    budget.charge(est_items, "CSR items")?;
    Ok(est_placements + est_ranges + est_csr + est_items)
}

#[allow(clippy::too_many_arguments)]
fn convert_inner(
    fs: &dyn BundleFs,
    stem: &str,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: Option<&ResumePoint>,
) -> CoreResult<TransformResult> {
    let started = crate::job::WallInstant::now();
    let doc = probe_mirax_with_budget(fs, stem, plan.limits.memory_budget_bytes)?;
    let levels = &doc.levels;
    // Review §1: the conversion continues the probe's memory account (the
    // probe's positions/images stay alive in `doc`) and charges every
    // per-level structure — placement sort copies, CSR bucket arrays, the
    // image cache and the decoded-pixel budget — before allocating it.
    let mut budget = doc.budget.clone();
    budget.charge(
        (OUT_TILE as u64)
            .saturating_mul(OUT_TILE as u64)
            .saturating_mul(6),
        "tile 画布与编码缓冲",
    )?;
    if plan.profile == OutputProfile::OmeBigTiffSubifd {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 MRXS 输入"));
    }
    // composition + JPEG re-encode is inherently lossy for this format:
    // both encodings refuse the strict-lossless policy before any byte
    if plan.pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与 MRXS 组合输出互斥：拼接 tile 必然重编码（有损），无逐字节搬运路径",
        ));
    }
    if let Some(r) = resume {
        if r.level >= levels.len() {
            return Err(CoreError::validation("resume: level 越界"));
        }
        let expect = if r.cell > 0 { r.level + 1 } else { r.level };
        if r.ifd_tiles.len() != expect {
            return Err(CoreError::validation(format!(
                "resume: ifd_tiles 长度 {} 与 (level={}, cell={}) 不符",
                r.ifd_tiles.len(),
                r.level,
                r.cell
            )));
        }
    }
    let compact = plan.encoding == EncodingProfile::CompactJpegV1;
    let cfg = if compact {
        crate::plan::compact_jpeg_v1_encoder_cfg()
    } else {
        preserve_cfg()
    };

    let mut warnings: Vec<String> = Vec::new();
    if !doc.associated.is_empty() {
        warnings.push(WARN_ASSOC_NOT_EXPORTED.to_string());
    }
    warnings.push(WARN_NO_ICC.to_string()); // Slidedat carries no ICC

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => MirxWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: mirax_description_bytes(&doc),
        },
        OutputProfile::OmeBigTiffRgbSubifd => MirxWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(mirax_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, MirxWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };
    let mut level_stats: Vec<LevelStats> = Vec::new();
    let mut ifd_chain: Vec<(u32, Option<usize>)> = Vec::new();
    let mut filled_total: u64 = 0;
    let mut deduped_total: u64 = 0;

    // ---- shared fill payloads (F3 size revision) ------------------------ //
    // A tile no placement touches encodes to the level's fill colour — one
    // deterministic payload per DISTINCT fill colour, written ONCE right
    // after the 16-byte BigTIFF header; every such tile's
    // TileOffsets/TileByteCounts entry references it (valid TIFF: entries
    // may repeat). Deterministic placement keeps resume byte-identical: the
    // encoder is deterministic, so on resume the same payloads imply the
    // same lengths imply the same offsets — nothing is rewritten.
    let mut fill_keys: Vec<[u8; 3]> = Vec::new();
    for lv in &doc.levels {
        if !fill_keys.contains(&lv.section.fill_rgb) {
            fill_keys.push(lv.section.fill_rgb);
        }
    }
    let mut fill_refs: Vec<(u64, u32)> = Vec::with_capacity(fill_keys.len());
    {
        let mut canvas = vec![0u8; OUT_TILE as usize * OUT_TILE as usize * 3];
        let mut at = 16u64; // both writers place the header in [0,16)
        for key in &fill_keys {
            for px in canvas.chunks_exact_mut(3) {
                px.copy_from_slice(key);
            }
            let payload = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, &cfg)?;
            if resume.is_some() {
                // derive without writing (bytes already committed)
                fill_refs.push((at, payload.len() as u32));
            } else {
                let r = match &mut writer {
                    MirxWriter::Classic { w, .. } => w.write_payload(&payload)?,
                    MirxWriter::Ome { w, .. } => w.write_payload(&payload)?,
                };
                debug_assert_eq!(r.0, at);
                fill_refs.push(r);
            }
            at += payload.len() as u64;
        }
    }
    let fill_ref_of = |li: usize| -> (u64, u32) {
        let key = doc.levels[li].section.fill_rgb;
        let idx = fill_keys.iter().position(|k| *k == key).expect("key registered");
        fill_refs[idx]
    };

    for li in 0..levels.len() {
        job.check()?;
        let lv = &levels[li];
        let meta = level_meta(&doc, li, compact);
        let tiles_total =
            (lv.width.div_ceil(OUT_TILE) as u64) * (lv.height.div_ceil(OUT_TILE) as u64);
        let mut stats = LevelStats {
            level: li as u32,
            width: lv.width,
            height: lv.height,
            tiles_across: lv.width.div_ceil(OUT_TILE),
            tiles_down: lv.height.div_ceil(OUT_TILE),
            tiles_total,
            ..Default::default()
        };
        let level_charged = charge_level_index(&mut budget, lv)?;
        let placements = doc.placements(li);
        let buckets = TileBuckets::build(&placements, lv.width, lv.height);

        let resume_done = resume.is_some_and(|r| li < r.level);
        let resume_current = resume.is_some_and(|r| li == r.level);
        if resume_done {
            let committed = resume.unwrap().ifd_tiles[li];
            if committed != tiles_total {
                return Err(CoreError::validation(format!(
                    "resume: 层 {} 已提交 {} ≠ 总 tile 数 {}（journal 与输入不符）",
                    li, committed, tiles_total
                )));
            }
            writer.begin(scratch, &meta, Some(committed))?;
            // skipped tiles re-counted from the placement index (no decode)
            let filled = (0..tiles_total).filter(|t| buckets.is_empty_tile(*t as usize)).count();
            stats.tiles_reencoded = tiles_total;
            stats.tiles_filled = filled as u64;
            stats.tiles_deduped = filled as u64;
            filled_total += filled as u64;
            deduped_total += filled as u64;
            writer.end(&meta, &[])?;
            ifd_chain.push((li as u32, None));
            level_stats.push(stats);
            budget.release(level_charged);
            continue;
        }

        let skip_until = if resume_current { resume.unwrap().cell } else { 0 };
        if resume_current && skip_until > 0 {
            let committed = resume.unwrap().ifd_tiles[li];
            writer.begin(scratch, &meta, Some(committed))?;
            let filled = (0..skip_until).filter(|t| buckets.is_empty_tile(*t as usize)).count();
            stats.tiles_reencoded = skip_until;
            stats.tiles_filled = filled as u64;
            stats.tiles_deduped = filled as u64; // committed fill tiles were shared refs
            filled_total += filled as u64;
            deduped_total += filled as u64;
        } else {
            writer.begin(scratch, &meta, None)?;
        }

        let mut cache = ImageCache::new(fs, &doc, li, &mut budget);
        let fill = lv.section.fill_rgb;
        let shared_fill = fill_ref_of(li);
        let mut canvas = vec![0u8; OUT_TILE as usize * OUT_TILE as usize * 3];
        let mut cell = skip_until;
        let fill_canvas = |c: &mut [u8]| {
            for px in c.chunks_exact_mut(3) {
                px.copy_from_slice(&fill);
            }
        };
        while cell < tiles_total {
            job.check()?;
            // every tile starts as the level's fill colour (from Slidedat —
            // never silently white); placements overwrite what they cover
            fill_canvas(&mut canvas);
            let any = compose_tile(&mut cache, &placements, &buckets, cell as usize, &mut canvas)?;
            if !any {
                // sparse area with no source coverage: counted + warned;
                // payload = the SHARED fill tile (one per distinct fill
                // colour, every such tile's record references it)
                stats.tiles_filled += 1;
                filled_total += 1;
                stats.tiles_deduped += 1;
                deduped_total += 1;
                writer.write_tile_ref(shared_fill.0, shared_fill.1)?;
                stats.tiles_reencoded += 1;
                cell += 1;
                let row_done = cell.div_ceil(stats.tiles_across as u64);
                maybe_progress_and_checkpoint!(writer, job, stats, li, cell, tiles_total, row_done);
                continue;
            }
            let enc = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, &cfg)?;
            writer.write_tile(&enc)?;
            canvas.fill(0);
            stats.tiles_reencoded += 1;
            cell += 1;
            let row_done = cell.div_ceil(stats.tiles_across as u64);
            maybe_progress_and_checkpoint!(writer, job, stats, li, cell, tiles_total, row_done);
        }
        drop(cache); // releases the cache's charged bytes back to the account
        if writer.cursor() > plan.limits.max_output_bytes {
            return Err(CoreError::too_large(format!(
                "输出已写 {} > {}",
                writer.cursor(),
                plan.limits.max_output_bytes
            )));
        }
        let desc: Vec<u8> = match &writer {
            MirxWriter::Classic { description, .. } => description.clone(),
            MirxWriter::Ome { .. } => Vec::new(),
        };
        writer.end(&meta, &desc)?;
        ifd_chain.push((li as u32, None));
        job.progress.on_progress(&Progress {
            unit: ProgressUnit::Level,
            level: li as u32,
            channel: None,
            done: li as u64 + 1,
            total: levels.len() as u64,
            committed_bytes: writer.cursor(),
        });
        level_stats.push(stats);
        // the level's placements/CSR buckets are dropped here — give the
        // account back so later levels are measured against the peak
        budget.release(level_charged);
    }
    let output_bytes = writer.finish()?;
    if filled_total > 0 && !warnings.iter().any(|w| w == WARN_SPARSE_FILL) {
        warnings.push(WARN_SPARSE_FILL.to_string());
    }
    let tiles_reencoded: u64 = level_stats.iter().map(|l| l.tiles_reencoded).sum();

    let mut result = TransformResult {
        plan_version: plan.plan_version,
        core_version: plan.core_version.clone(),
        format,
        source_format: Some(SOURCE_FORMAT),
        adapter_version: Some(ADAPTER_VERSION),
        output_bytes,
        output_sha256: None,
        width: levels[0].width,
        height: levels[0].height,
        levels: level_stats,
        edge_regions: Vec::new(),
        warnings,
        channels: Vec::new(),
        validation: crate::report::ValidationReport {
            ifd_count: ifd_chain.len() as u32,
            tile_records_emitted: 0,
            output_bytes,
            checks_passed: if format == FORMAT_CLASSIC {
                vec!["bigtiff-header".into(), "ifd-chain".into()]
            } else {
                vec!["bigtiff-header".into(), "ome-xml-present".into(), "subifd-chain".into()]
            },
        },
        ifd_chain,
        lossy_reencode: compact.then(|| LossyReencode {
            profile: EncodingProfile::CompactJpegV1.id(),
            params_fingerprint: COMPACT_JPEG_V1_FINGERPRINT.to_string(),
            quality: COMPACT_JPEG_V1_QUALITY,
            sampling: compact_sampling_label(),
            huffman: COMPACT_JPEG_V1_HUFFMAN,
            tiles_reencoded,
            tiles_padded: 0,
        }),
        composed: Some(ComposedSummary {
            mode: "mosaic-compose-reencode".to_string(),
            fingerprint: MRAX_PRESERVE_COMPOSE_FINGERPRINT.to_string(),
            quality: MRAX_PRESERVE_COMPOSE_QUALITY,
            sampling: "4:2:2".to_string(),
            huffman: MRAX_PRESERVE_COMPOSE_HUFFMAN.to_string(),
            tiles_composed: tiles_reencoded,
            tiles_filled: filled_total,
            tiles_deduped: deduped_total,
        }),
        associated: doc
            .associated
            .iter()
            .map(|a| AssociatedSummary {
                name: a.name.clone(),
                source_offset: 0,
                source_length: 0,
                width: a.width,
                height: a.height,
            })
            .collect(),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    result.validation.tile_records_emitted =
        result.levels.iter().map(|l| l.tiles_total).sum();
    Ok(result)
}

/// Convenience wrapper without progress (tests).
pub fn convert_mirax(
    fs: &dyn BundleFs,
    stem: &str,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_mirax_to_bigtiff(fs, stem, sink, scratch, plan, &job)
}
