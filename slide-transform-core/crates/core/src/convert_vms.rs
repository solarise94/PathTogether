//! VMS bundle → brightfield conversion: concatenated tile JPEGs stitched at
//! their true mosaic positions, re-encoded into the output pyramid.
//!
//! **What `preserve-source-v1` means for VMS** (stated honestly, unlike a
//! byte-passthrough claim): a VMS level-0 is a *mosaic* of huge (≈64K px)
//! baseline JPEGs abutting exactly. Only output tiles at the source-tile
//! grid could ever be byte copies — and the output grid is 256 px, so an
//! output tile straddles source tiles for all but degenerate grids.
//! `preserve` means: decode each tile JPEG restart segment by restart
//! segment (each segment an independent MCU run with reset DC predictors —
//! a 64K×64K tile is ~11 GB decoded RGB, so bounded segments are the only
//! decode unit), paste the decoded MCU rects at the tile's mosaic position
//! and re-encode every 256×256 output tile at the documented high-fidelity
//! setting **YCbCr 4:2:2 · quality 96 · standard Huffman** (fingerprint
//! [`PRESERVE_COMPOSE_FINGERPRINT`], reported as `composed`). `compact
//! -jpeg-v1` composes identically and re-encodes at the locked U3
//! parameters.
//!
//! Reduced output levels are the `l0-box2` chain (the 2×2 area-average of
//! output level 0, read back from the committed sink): the source's map
//! image is never decoded for pixels, exactly like the MRXS v2 / NDPI /
//! generic-TIFF adapters. The macro image is excluded at the probe.
//!
//! Sparse areas do not exist in a well-formed VMS grid (the tiles abut and
//! cover the whole mosaic), so there is no fill-tile path — a decode
//! failure is a typed error, never a filled tile.
//!
//! Strict-lossless is refused (typed, before any output byte): every output
//! tile is a re-encode by construction.
//!
//! Memory (review §1): the band buffer (256 mosaic rows × full width), the
//! tile canvas + encode buffers, and every segment decode (read + decoder
//! transient + decoded pixels) are charged against the host budget BEFORE
//! the allocation — an over-budget mosaic is a typed
//! `resource_profile_insufficient` refusal, never an OOM mid-decode.
//!
//! Resume mirrors the other adapters: checkpoints per committed tile row; a
//! resumed L0 fast-forwards every intersecting tile's segment scanner to
//! the band's first segment (marker scan only, no decode) and the fresh
//! path stays byte-identical.

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::budget::MemBudget;
use crate::bundle::BundleFs;
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::jpeg;
use crate::segment::{SegmentRead, SegmentReader};
use crate::vms::{
    probe_vms_with_budget, VmsDoc, VmsTile, ADAPTER_VERSION, OUT_TILE,
    PRESERVE_COMPOSE_FINGERPRINT, PRESERVE_COMPOSE_HUFFMAN, PRESERVE_COMPOSE_QUALITY,
    PRESERVE_COMPOSE_SAMPLING, PYRAMID_METHOD, SOURCE_FORMAT,
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

/// Row-boundary progress + checkpoint emission (the MRXS/NDPI macro shape).
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

pub const WARN_ASSOC_NOT_EXPORTED: &str = "vms_associated_not_exported";
pub const WARN_NO_ICC: &str = "color_management_not_applied";

fn preserve_cfg() -> jpeg::EncoderCfg {
    // Same parameters as the MRXS/NDPI compose / generic-TIFF generated
    // tiles: YCbCr 4:2:2 at quality 96 (the (2,1) TIFF layout every reader
    // decodes).
    jpeg::EncoderCfg::with_quality(PRESERVE_COMPOSE_QUALITY, PRESERVE_COMPOSE_SAMPLING)
}

// --------------------------------------------------------------------------- //
// bundle-member source (one tile JPEG = one strip of a member)
// --------------------------------------------------------------------------- //

/// A read-only view of ONE bundle member as a `ByteSource` (offsets are
/// member-relative; the segment reader's strip_offset is 0).
struct MemberSource<'a> {
    fs: &'a dyn BundleFs,
    member: usize,
    size: u64,
}

impl<'a> MemberSource<'a> {
    fn new(fs: &'a dyn BundleFs, member: usize) -> CoreResult<Self> {
        let size = fs
            .members()
            .get(member)
            .map(|m| m.size)
            .ok_or_else(|| CoreError::oob(format!("成员序号 {member} 不存在")))?;
        Ok(MemberSource { fs, member, size })
    }
}

impl ByteSource for MemberSource<'_> {
    fn size(&self) -> u64 {
        self.size
    }
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        self.fs.read_member_at(self.member, offset, len)
    }
}

// --------------------------------------------------------------------------- //
// mosaic paste (clip at the tile's own extent AND the band window)
// --------------------------------------------------------------------------- //

/// Paste one decoded segment's MCU rects into the band buffer at the tile's
/// mosaic position. `band` covers mosaic rows `[band_y0, band_y0 +
/// band_rows)` and ALL mosaic columns `[0, band_w)`; MCU rects outside the
/// tile's true pixel extent (padded MCU columns/rows of the source JPEG) or
/// outside the band window are clipped.
#[allow(clippy::too_many_arguments)]
fn paste_segment_mosaic(
    band: &mut [u8],
    band_w: usize,
    band_rows: usize,
    seg: &SegmentRead,
    t: &VmsTile,
    band_y0: u64,
) {
    let img = &seg.img;
    let img_w = img.width as usize;
    let mcu_w = t.mcu_w() as usize;
    let mcu_h = t.mcu_h() as usize;
    let mcus_x = t.mcus_x as usize;
    let file_w = t.width as usize;
    let file_h = t.height as usize;
    for i in 0..seg.mcus {
        let g = seg.mcu_start + i;
        let gc = (g % mcus_x as u64) as usize;
        let gr = (g / mcus_x as u64) as u64;
        // MCU rect in the segment image (segment grid: seg.grid_w MCUs/row)
        let sx = (i % seg.grid_w) as usize * mcu_w;
        let sy = (i / seg.grid_w) as usize * mcu_h;
        // the tile's MCU row in BAND rows (tiles below the band start paint
        // at a positive offset; rows above the band window go negative)
        let rel_y = (t.y0 + gr * mcu_h as u64) as i64 - band_y0 as i64;
        if rel_y < 0 {
            continue; // above the band (carried segments' head)
        }
        let by0 = rel_y as usize;
        if by0 >= band_rows {
            continue; // below the band
        }
        let dst_x = t.x0 as usize + gc * mcu_w;
        if dst_x >= band_w {
            continue;
        }
        // clip the MCU rect at the file's true extent and the band width
        let pw = mcu_w
            .min(file_w.saturating_sub(gc * mcu_w))
            .min(band_w - dst_x);
        if pw == 0 {
            continue;
        }
        for r in 0..mcu_h {
            let world_y = t.y0 as usize + gr as usize * mcu_h + r;
            if world_y >= t.y0 as usize + file_h {
                break; // beyond the tile's true height (MCU padding rows)
            }
            let by = by0 + r;
            if by >= band_rows {
                break;
            }
            let s = (sy + r) * img_w * 3 + sx * 3;
            let d = by * band_w * 3 + dst_x * 3;
            band[d..d + pw * 3].copy_from_slice(&img.data[s..s + pw * 3]);
        }
    }
}

/// First segment index of tile `t` whose MCU rows intersect the band row
/// `ty` (resume fast-forward; marker scan only).
fn first_band_segment(t: &VmsTile, band_y0: u64) -> u64 {
    let local_y0 = band_y0.saturating_sub(t.y0);
    let mcu_row = local_y0 / t.mcu_h() as u64;
    mcu_row * t.mcus_x as u64 / t.restart_interval as u64
}

// --------------------------------------------------------------------------- //
// writers (same shape as the other brightfield adapters)
// --------------------------------------------------------------------------- //

struct LevelMeta {
    width: u32,
    height: u32,
    photometric: u16,
    tiff_sub: (u16, u16),
    reduced: bool,
    mpp: Option<(f64, f64)>,
}

enum VmsWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl VmsWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            VmsWriter::Classic { w, .. } => match committed {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            VmsWriter::Ome { w, ome_xml, levels } => {
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
            VmsWriter::Classic { w, .. } => w.end_level_ex(
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
            VmsWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<()> {
        match self {
            VmsWriter::Classic { w, .. } => w.write_tile(data).map(|_| ()),
            VmsWriter::Ome { w, .. } => w.write_tile(data).map(|_| ()),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            VmsWriter::Classic { w, .. } => w.cursor(),
            VmsWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn tile_record(&self, ifd: usize, index: usize) -> CoreResult<(u64, u32)> {
        match self {
            VmsWriter::Classic { w, .. } => w.tile_record(ifd, index),
            VmsWriter::Ome { w, .. } => w.tile_record(ifd, index),
        }
    }

    fn read_output_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        match self {
            VmsWriter::Classic { w, .. } => w.read_output_at(offset, len),
            VmsWriter::Ome { w, .. } => w.read_output_at(offset, len),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            VmsWriter::Classic { w, .. } => w.ifd_tile_counts(),
            VmsWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            VmsWriter::Classic { w, .. } => w.finish(),
            VmsWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

fn vms_description_bytes(doc: &VmsDoc) -> Vec<u8> {
    let (mx, my) = doc.mpp.unwrap_or((f64::NAN, f64::NAN));
    let fmt = |v: f64| {
        if v.is_finite() {
            py_repr_f64(v)
        } else {
            "null".into()
        }
    };
    let s = format!(
        "{{\"adapter\": \"{}\", \"adapter_version\": \"{}\", \"composed\": \"{}\", \"pyramid\": \"{}\", \"mpp_x\": {}, \"mpp_y\": {}, \"objective\": {}, \"source_format\": \"{}\"}}\u{0}",
        SOURCE_FORMAT,
        ADAPTER_VERSION,
        PRESERVE_COMPOSE_FINGERPRINT,
        PYRAMID_METHOD,
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

fn vms_ome_xml(doc: &VmsDoc, plan: &TransformPlan) -> Vec<u8> {
    let compact = plan.encoding == EncodingProfile::CompactJpegV1;
    let mut provenance: Vec<(&str, String)> = vec![
        ("converter", "slide-transform-core".to_string()),
        ("converter_version", plan.core_version.clone()),
        ("output_profile", plan.profile.id().to_string()),
        ("source_format", SOURCE_FORMAT.to_string()),
        ("source_adapter", SOURCE_FORMAT.to_string()),
        ("adapter_version", ADAPTER_VERSION.to_string()),
        (
            "compose_mode",
            "concatenated tile JPEGs decoded restart-segment by restart segment and \
             stitched at their true mosaic positions (exact abutment, no overlap), then \
             re-encoded into 256px output tiles; every tile re-encoded (no byte \
             passthrough exists for this format)"
                .to_string(),
        ),
        (
            "pyramid_method",
            "l0-box2: reduced output levels are the 2x2 area-average (box) downsample \
             chain of output level 0 — the source's map image is not used for pixels"
                .to_string(),
        ),
        ("preserve_compose_fingerprint", PRESERVE_COMPOSE_FINGERPRINT.to_string()),
        (
            "mpp_source",
            if doc.mpp.is_some() { "vms-physicalwidth-nm" } else { "unknown" }.to_string(),
        ),
        (
            "objective_source",
            if doc.objective.is_some() { "vms-sourcelens" } else { "unknown" }.to_string(),
        ),
        ("pyramid_levels", (1 + doc.generated.len()).to_string()),
    ];
    if compact {
        provenance.push(("encoding_profile", plan.encoding.id().to_string()));
        provenance.push(("encoding_params_fingerprint", COMPACT_JPEG_V1_FINGERPRINT.to_string()));
    }
    provenance.push((
        "tile_payloads",
        if compact {
            format!(
                "decoded from restart segments, stitched at true positions, then re-encoded \
                 at the locked compact parameters (quality {}, subsampling {}, standard \
                 Annex-K Huffman, fingerprint {}); lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            format!(
                "decoded from restart segments, stitched at true positions, then re-encoded \
                 at the documented high-fidelity compose setting (YCbCr 4:2:2, quality {}, \
                 standard Annex-K Huffman, fingerprint {}); lossy generation on a lossy \
                 source, never claimed lossless",
                PRESERVE_COMPOSE_QUALITY,
                PRESERVE_COMPOSE_FINGERPRINT
            )
        },
    ));
    provenance.push((
        "pixel_policy",
        match plan.pixel_policy {
            PixelPolicy::AllowEdgeReencode => "preserve-source-reencode".to_string(),
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
        doc.width, doc.height,
    );
    let mut out = xml.into_bytes();
    out.push(0);
    out
}

fn level_meta(doc: &VmsDoc, li: usize, compact: bool, dims: (u32, u32)) -> LevelMeta {
    let mpp = doc.mpp.map(|(mx, my)| {
        let rx = doc.width as f64 / dims.0 as f64;
        let ry = doc.height as f64 / dims.1 as f64;
        (mx * rx, my * ry)
    });
    let (photometric, tiff_sub) = if compact {
        (6u16, compact_sampling_tiff())
    } else {
        (6u16, (2u16, 1u16)) // preserve compose: YCbCr 4:2:2
    };
    LevelMeta {
        width: dims.0,
        height: dims.1,
        photometric,
        tiff_sub,
        reduced: li > 0,
        mpp,
    }
}

/// Compose ONE generated tile: the 2×2 area-average of the previous output
/// level's tiles (read back from the committed sink; same method and
/// geometry as the NDPI/generic-TIFF adapters' generated tail).
#[allow(clippy::too_many_arguments)]
fn pyramid_tile_from_prev(
    writer: &VmsWriter<'_>,
    prev_ifd: usize,
    prev_across: u32,
    prev_down: u32,
    cur_w: u32,
    cur_h: u32,
    cur_across: u64,
    tile: usize,
    out: &mut [u8],
    canvas: &mut [u8],
) -> CoreResult<()> {
    const F: usize = 2;
    let side = OUT_TILE as usize * F;
    for px in canvas.iter_mut() {
        *px = 255;
    }
    // rows ≥ valid (out-of-level) must be DETERMINISTIC white — never stale
    // bytes from a previously composed cell (fresh run) or the buffer's
    // initial content (a run resumed mid-level): those rows are never read
    // by readers, but byte identity between fresh and resumed runs does not
    // forgive them.
    for px in out.iter_mut() {
        *px = 255;
    }
    let tx = tile % cur_across as usize;
    let ty = tile / cur_across as usize;
    let valid_w = (OUT_TILE as usize).min(cur_w as usize - tx * OUT_TILE as usize);
    let valid_h = (OUT_TILE as usize).min(cur_h as usize - ty * OUT_TILE as usize);
    for pty in (ty * F)..((ty + 1) * F) {
        for ptx in (tx * F)..((tx + 1) * F) {
            if ptx >= prev_across as usize || pty >= prev_down as usize {
                continue; // beyond the previous level's edge: never read below
            }
            let (off, cnt) = writer.tile_record(prev_ifd, pty * prev_across as usize + ptx)?;
            let raw = writer.read_output_at(off, cnt as usize)?;
            let img = jpeg::decode_ex(&raw, (OUT_TILE as u64) * (OUT_TILE as u64), false)?;
            if img.width != OUT_TILE || img.height != OUT_TILE {
                return Err(CoreError::variant(format!(
                    "金字塔读取：层 {prev_ifd} tile {} 尺寸 {}×{} ≠ {OUT_TILE}",
                    pty * prev_across as usize + ptx,
                    img.width,
                    img.height
                )));
            }
            let bx = (ptx - tx * F) * OUT_TILE as usize;
            let by = (pty - ty * F) * OUT_TILE as usize;
            for row in 0..OUT_TILE as usize {
                let s = row * OUT_TILE as usize * 3;
                let d = (by + row) * side + bx;
                match img.kind {
                    jpeg::ColorKind::Rgb => {
                        canvas[d * 3..(d + OUT_TILE as usize) * 3]
                            .copy_from_slice(&img.data[s..s + OUT_TILE as usize * 3]);
                    }
                    jpeg::ColorKind::Gray => {
                        for col in 0..OUT_TILE as usize {
                            let g = img.data[s + col];
                            let o = (d + col) * 3;
                            canvas[o..o + 3].copy_from_slice(&[g, g, g]);
                        }
                    }
                }
            }
        }
    }
    let log = 2 * F.trailing_zeros();
    for oy in 0..valid_h {
        for ox in 0..valid_w {
            for c in 0..3 {
                let mut acc = 0u32;
                for dy in 0..F {
                    let row = (oy * F + dy) * side + ox * F;
                    for dx in 0..F {
                        acc += canvas[(row + dx) * 3 + c] as u32;
                    }
                }
                out[(oy * OUT_TILE as usize + ox) * 3 + c] = (acc >> log) as u8;
            }
        }
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// conversion
// --------------------------------------------------------------------------- //

pub fn convert_vms_to_bigtiff(
    fs: &dyn BundleFs,
    stem: &str,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(fs, stem, sink, scratch, plan, job, None)
}

/// Resume a VMS conversion from `resume` (see [`crate::resume`]).
pub fn convert_vms_to_bigtiff_resume(
    fs: &dyn BundleFs,
    stem: &str,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    // Adapter-version pin, enforced in the CORE (SCN/gtiff/NDPI parity): a
    // state WITHOUT the field is foreign as well — never mix two adapter
    // generations into one output.
    if resume.adapter_version.as_deref() != Some(ADAPTER_VERSION) {
        return Err(CoreError::validation(format!(
            "resume: 已提交进度属于 VMS 适配器 v{}，当前为 v{ADAPTER_VERSION}：两种适配器配方不得混合进同一输出",
            resume.adapter_version.as_deref().unwrap_or("1（字段缺失）"),
        )));
    }
    convert_inner(fs, stem, sink, scratch, plan, job, Some(resume))
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
    let doc = probe_vms_with_budget(fs, stem, plan.limits.memory_budget_bytes)?;
    let mut budget: MemBudget = doc.budget.clone();
    let out_levels: Vec<(u32, u32)> = std::iter::once((doc.width, doc.height))
        .chain(doc.generated.iter().copied())
        .collect();
    if let Some(r) = resume {
        if r.level >= out_levels.len() {
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
    if plan.profile == OutputProfile::OmeBigTiffSubifd {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 VMS 输入"));
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与 VMS 输出互斥：拼接 tile 必然分段解码后重编码（有损），无逐字节搬运路径",
        ));
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
    if !doc.generated.is_empty() {
        warnings.push(format!("vms_levels_generated:{}", doc.generated.len()));
    }
    if doc.map_present {
        warnings.push("vms_map_not_used".to_string());
    }
    warnings.push(WARN_NO_ICC.to_string()); // the .vms INI carries no ICC

    // ---- working set, charged BEFORE any allocation (review §1) ---------- //
    let band_bytes = (OUT_TILE as u64)
        .saturating_mul(doc.width as u64)
        .saturating_mul(3);
    let canvas_bytes = (OUT_TILE as u64)
        .saturating_mul(OUT_TILE as u64)
        .saturating_mul(6); // tile canvas + encode buffers
    let max_seg_px = doc.tiles.iter().map(|t| t.strip_geom().max_segment_px()).max().unwrap_or(0);
    budget.charge(band_bytes, "L0 拼接条带缓冲（256 行 × 拼接宽 × 3）")?;
    budget.charge(canvas_bytes, "tile 画布与编码缓冲")?;
    budget.charge(
        max_seg_px.saturating_mul(6),
        "单段解码峰值预留（读取 + 解码器临时 + RGB）",
    )?;
    // the generated-pyramid working set (f² decoded prev tiles + canvases)
    let pyramid_ws = 4u64
        .saturating_mul((OUT_TILE as u64) * (OUT_TILE as u64) * 6)
        .saturating_add(
            (OUT_TILE as u64 * 2).saturating_mul(OUT_TILE as u64 * 2).saturating_mul(3),
        );
    if !doc.generated.is_empty() {
        budget.charge(pyramid_ws, "l0-box2 合成画布与输出缓冲")?;
    }

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => VmsWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: vms_description_bytes(&doc),
        },
        OutputProfile::OmeBigTiffRgbSubifd => VmsWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(vms_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, VmsWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };
    let mut level_stats: Vec<LevelStats> = Vec::new();
    let mut ifd_chain: Vec<(u32, Option<usize>)> = Vec::new();

    for (li, dims) in out_levels.iter().enumerate() {
        job.check()?;
        let meta = level_meta(&doc, li, compact, *dims);
        let across = (dims.0 as u64).div_ceil(OUT_TILE as u64);
        let down = (dims.1 as u64).div_ceil(OUT_TILE as u64);
        let tiles_total = across * down;
        let mut stats = LevelStats {
            level: li as u32,
            width: dims.0,
            height: dims.1,
            tiles_across: across as u32,
            tiles_down: down as u32,
            tiles_total,
            ..Default::default()
        };

        let resume_done = resume.is_some_and(|r| li < r.level);
        let resume_current = resume.is_some_and(|r| li == r.level);
        let skip_until = if resume_current { resume.unwrap().cell } else { 0 };
        if resume_current && skip_until > 0 {
            let committed = resume.unwrap().ifd_tiles[li];
            writer.begin(scratch, &meta, Some(committed))?;
            stats.tiles_reencoded = skip_until;
        } else if resume_done {
            let committed = resume.unwrap().ifd_tiles[li];
            if committed != tiles_total {
                return Err(CoreError::validation(format!(
                    "resume: 层 {li} 已提交 {committed} ≠ 总 tile 数 {tiles_total}（journal 与输入不符）"
                )));
            }
            writer.begin(scratch, &meta, Some(committed))?;
            stats.tiles_reencoded = tiles_total;
        } else {
            writer.begin(scratch, &meta, None)?;
        }

        if li == 0 {
            // ---- level 0: per-tile restart-segmented decode → mosaic paste
            // → re-encode ----------------------------------------------------
            if resume_done {
                // no pixels needed; counts only — but the description must be
                // restaged for a fully-resumed level too (end_level stages it
                // into the IFD; an empty vec here would drop tag 270 from the
                // resumed output and break byte identity; convert_scn 同款)
                let desc: Vec<u8> = match &writer {
                    VmsWriter::Classic { description, .. } => description.clone(),
                    VmsWriter::Ome { .. } => Vec::new(),
                };
                level_stats.push(stats);
                ifd_chain.push((li as u32, None));
                writer.end(&meta, &desc)?;
                continue;
            }
            let mut ty = skip_until / across;
            // member sources live for the whole level (readers borrow them);
            // one forward-only reader + carry per tile, created lazily as
            // bands reach the tile and persisted across bands
            let srcs: Vec<MemberSource> = doc
                .tiles
                .iter()
                .map(|t| MemberSource::new(fs, t.member as usize))
                .collect::<CoreResult<Vec<_>>>()?;
            let mut readers: Vec<Option<SegmentReader>> =
                (0..doc.tiles.len()).map(|_| None).collect();
            let mut carries: Vec<Option<SegmentRead>> =
                (0..doc.tiles.len()).map(|_| None).collect();
            let mut band = vec![255u8; band_bytes as usize];
            let mut canvas = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
            let mut cell = skip_until;
            while ty < down {
                job.check()?;
                let band_y0 = ty * OUT_TILE as u64;
                let band_rows_u = (OUT_TILE as u64).min((doc.height as u64) - band_y0);
                for px in band.iter_mut() {
                    *px = 255;
                }
                // decode + paste every tile whose rows intersect the band
                for (ti, t) in doc.tiles.iter().enumerate() {
                    // the tile's rows inside THIS band, both in band space
                    // ([band_lo, band_hi)) and tile space ([local_y0,
                    // local_y0 + local_rows))
                    let band_lo = band_y0.max(t.y0);
                    let band_hi = (band_y0 + band_rows_u).min(t.y0 + t.height as u64);
                    if band_lo >= band_hi {
                        continue; // this tile never reaches the band
                    }
                    let local_y0 = band_lo - t.y0;
                    let local_rows = band_hi - band_lo;
                    if readers[ti].is_none() {
                        let mut r = SegmentReader::new(&srcs[ti], &t.strip_geom());
                        // fast-forward past the tile's segments above this
                        // band (marker scan only, no decode): a no-op on the
                        // fresh path's first band, and what makes a resumed
                        // run skip the already-committed rows
                        let want = first_band_segment(t, band_y0);
                        if want > 0 {
                            r.skip_to(&mut budget, want)?;
                        }
                        readers[ti] = Some(r);
                    }
                    // first MCU index past the band's painted rows of this tile
                    let mcu_end = (local_y0 + local_rows)
                        .div_ceil(t.mcu_h() as u64)
                        * t.mcus_x as u64;
                    let reader = readers[ti].as_mut().expect("just created");
                    loop {
                        let next_mcu = match &carries[ti] {
                            Some(c) => c.mcu_start,
                            None => reader.next_k() * t.restart_interval as u64,
                        };
                        if next_mcu >= mcu_end {
                            break;
                        }
                        let seg = match carries[ti].take() {
                            Some(c) => c,
                            None => reader.decode_next(&mut budget)?,
                        };
                        paste_segment_mosaic(
                            &mut band,
                            doc.width as usize,
                            band_rows_u as usize,
                            &seg,
                            t,
                            band_y0,
                        );
                        let seg_bottom = (seg.mcu_start + seg.mcus)
                            .div_ceil(t.mcus_x as u64)
                            * t.mcu_h() as u64;
                        if seg_bottom > local_y0 + local_rows && reader.next_k() < t.segments {
                            // crosses into the next band: keep the decoded pixels
                            carries[ti] = Some(seg);
                        } else {
                            budget.release(seg.charged);
                        }
                    }
                }
                // tiles of this row: canvas from the band (row 0 of the band
                // IS the tile row's first pixel row), clipped at the level
                for txc in 0..across {
                    let tx0 = txc * OUT_TILE as u64;
                    let valid_w = (OUT_TILE as u64).min(dims.0 as u64 - tx0) as usize;
                    let valid_h =
                        (OUT_TILE as u64).min(dims.1 as u64 - ty * OUT_TILE as u64) as usize;
                    for px in canvas.iter_mut() {
                        *px = 255;
                    }
                    for r in 0..valid_h {
                        let srow = r * doc.width as usize * 3 + tx0 as usize * 3;
                        let drow = r * OUT_TILE as usize * 3;
                        canvas[drow..drow + valid_w * 3]
                            .copy_from_slice(&band[srow..srow + valid_w * 3]);
                    }
                    let enc = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, &cfg)?;
                    writer.write_tile(&enc)?;
                    stats.tiles_reencoded += 1;
                    cell += 1;
                    let row_done = cell.div_ceil(across);
                    maybe_progress_and_checkpoint!(writer, job, stats, li, cell, tiles_total, row_done);
                }
                ty += 1;
            }
            for c in carries.into_iter().flatten() {
                budget.release(c.charged);
            }
            drop(readers);
            if cell != tiles_total {
                return Err(CoreError::validation(format!(
                    "层 0 tile 游标 {cell} ≠ 网格总数 {tiles_total}"
                )));
            }
            budget.release(band_bytes);
        } else {
            // ---- reduced level: the L0-derived pyramid (l0-box2) -------- //
            if resume_done {
                // the description must be restaged for a fully-resumed level
                // too (see the L0 resume_done arm; convert_scn 同款预防)
                let desc: Vec<u8> = match &writer {
                    VmsWriter::Classic { description, .. } => description.clone(),
                    VmsWriter::Ome { .. } => Vec::new(),
                };
                level_stats.push(stats);
                ifd_chain.push((li as u32, None));
                writer.end(&meta, &desc)?;
                continue;
            }
            let prev_w = out_levels[li - 1].0;
            let prev_h = out_levels[li - 1].1;
            let prev_across = (prev_w as u64).div_ceil(OUT_TILE as u64) as u32;
            let prev_down = (prev_h as u64).div_ceil(OUT_TILE as u64) as u32;
            let side = OUT_TILE as usize * 2;
            let mut canvas = vec![255u8; side * side * 3];
            let mut out = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
            let mut cell = skip_until;
            while cell < tiles_total {
                job.check()?;
                pyramid_tile_from_prev(
                    &writer,
                    li - 1,
                    prev_across,
                    prev_down,
                    dims.0,
                    dims.1,
                    across,
                    cell as usize,
                    &mut out,
                    &mut canvas,
                )?;
                let enc = jpeg::encode_rgb(&out, OUT_TILE, OUT_TILE, &cfg)?;
                writer.write_tile(&enc)?;
                stats.tiles_reencoded += 1;
                cell += 1;
                let row_done = cell.div_ceil(across);
                maybe_progress_and_checkpoint!(writer, job, stats, li, cell, tiles_total, row_done);
            }
        }
        if writer.cursor() > plan.limits.max_output_bytes {
            return Err(CoreError::too_large(format!(
                "输出已写 {} > {}",
                writer.cursor(),
                plan.limits.max_output_bytes
            )));
        }
        let desc: Vec<u8> = match &writer {
            VmsWriter::Classic { description, .. } => description.clone(),
            VmsWriter::Ome { .. } => Vec::new(),
        };
        writer.end(&meta, &desc)?;
        ifd_chain.push((li as u32, None));
        job.progress.on_progress(&Progress {
            unit: ProgressUnit::Level,
            level: li as u32,
            channel: None,
            done: li as u64 + 1,
            total: out_levels.len() as u64,
            committed_bytes: writer.cursor(),
        });
        level_stats.push(stats);
    }
    let output_bytes = writer.finish()?;
    let tiles_reencoded: u64 = level_stats.iter().map(|l| l.tiles_reencoded).sum();

    let mut result = TransformResult {
        plan_version: plan.plan_version,
        core_version: plan.core_version.clone(),
        format,
        source_format: Some(SOURCE_FORMAT),
        adapter_version: Some(ADAPTER_VERSION),
        output_bytes,
        output_sha256: None,
        width: doc.width,
        height: doc.height,
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
            fingerprint: PRESERVE_COMPOSE_FINGERPRINT.to_string(),
            quality: PRESERVE_COMPOSE_QUALITY,
            sampling: "4:2:2".to_string(),
            huffman: PRESERVE_COMPOSE_HUFFMAN.to_string(),
            tiles_composed: tiles_reencoded,
            tiles_filled: 0,
            tiles_deduped: 0,
            pyramid: PYRAMID_METHOD.to_string(),
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
pub fn convert_vms(
    fs: &dyn BundleFs,
    stem: &str,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_vms_to_bigtiff(fs, stem, sink, scratch, plan, &job)
}
