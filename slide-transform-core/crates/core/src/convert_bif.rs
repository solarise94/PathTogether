//! Ventana BIF → brightfield conversion: overlap-stitched JPEG tiles →
//! classic multi-IFD JPEG tiled BigTIFF pyramid or RGB OME-BigTIFF.
//!
//! **What `preserve-source-v1` means for BIF** (stated honestly): the
//! scanner's tiles overlap (~113 px of 1024 in x on the public OS-2.bif),
//! so there is no byte-passthrough — an output tile always mixes ≥ 2
//! source tiles. `preserve` means: stream every referenced source tile
//! through the restart-less MCU-row band decoder ([`crate::jpeg::band`],
//! bounded 64 KiB entropy windows — the tiles carry no restart markers),
//! paste the decoded rows into the 256-row output band at the tile's
//! OpenSlide-faithful integer placement, and re-encode every 256×256
//! output tile at the documented high-fidelity setting **YCbCr 4:2:2 ·
//! quality 96 · standard Huffman** (fingerprint
//! [`PRESERVE_COMPOSE_FINGERPRINT`], reported as `composed`).
//! `compact-jpeg-v1` composes identically and re-encodes at the locked U3
//! parameters.
//!
//! Overlap precedence mirrors OpenSlide's tilemap paint order exactly: the
//! grid paints tiles in REVERSE raster order, so the tile with the
//! smallest (row, col) paints LAST and wins the overlap — this converter
//! pastes in the same reverse-raster order.
//!
//! Reduced output levels are the `l0-box2` chain (the 2×2 area-average of
//! output level 0, read back from the committed sink): the source's own
//! reduced layers are never decoded for pixels. The label / thumbnail /
//! probability pages are excluded at the probe.
//!
//! Strict-lossless is refused (typed, before any output byte): every output
//! tile is a re-encode by construction.
//!
//! Memory (review §1): the L0 band buffer (256 rows × stitched width × 3),
//! the tile canvas + encode buffers, the live per-tile scanner working
//! sets and the l0-box2 compose buffers are charged against the host
//! budget BEFORE the allocations — an over-budget input is a typed
//! `resource_profile_insufficient` refusal, never an OOM mid-decode.
//!
//! Resume mirrors the other adapters: checkpoints per committed tile row;
//! a resumed L0 recomposes the current band from its first tile row (each
//! tile scanner re-opens and fast-forwards to the band — decode-and-drop,
//! output-identical) and the fresh path stays byte-identical.

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::bif::{
    probe_bif_with_budget, BifDoc, ADAPTER_VERSION, OUT_TILE, PRESERVE_COMPOSE_FINGERPRINT,
    PRESERVE_COMPOSE_HUFFMAN, PRESERVE_COMPOSE_QUALITY, PRESERVE_COMPOSE_SAMPLING,
    PYRAMID_METHOD, SOURCE_FORMAT,
};
use crate::budget::MemBudget;
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, Progress, ProgressUnit};
use crate::jpeg;
use crate::jpeg::band::BandScanner;
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

/// Row-boundary progress + checkpoint emission (the shared macro's shape).
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

pub const WARN_ASSOC_NOT_EXPORTED: &str = "bif_associated_not_exported";
pub const WARN_NO_ICC: &str = "color_management_not_applied";

fn preserve_cfg() -> jpeg::EncoderCfg {
    // Same parameters as the NDPI/VMS compose family: YCbCr 4:2:2 at
    // quality 96 (the (2,1) TIFF layout every reader decodes).
    jpeg::EncoderCfg::with_quality(PRESERVE_COMPOSE_QUALITY, PRESERVE_COMPOSE_SAMPLING)
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
    icc: Option<Vec<u8>>,
}

enum BifWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl BifWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            BifWriter::Classic { w, .. } => match committed {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            BifWriter::Ome { w, ome_xml, levels } => {
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
                        icc: m.icc.clone(),
                    },
                )
            }
        }
    }

    fn end(&mut self, m: &LevelMeta, description: &[u8]) -> CoreResult<()> {
        match self {
            BifWriter::Classic { w, .. } => w.end_level_ex(
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
                    icc: m.icc.clone(),
                },
            ),
            BifWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<()> {
        match self {
            BifWriter::Classic { w, .. } => w.write_tile(data).map(|_| ()),
            BifWriter::Ome { w, .. } => w.write_tile(data).map(|_| ()),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            BifWriter::Classic { w, .. } => w.cursor(),
            BifWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn tile_record(&self, ifd: usize, index: usize) -> CoreResult<(u64, u32)> {
        match self {
            BifWriter::Classic { w, .. } => w.tile_record(ifd, index),
            BifWriter::Ome { w, .. } => w.tile_record(ifd, index),
        }
    }

    fn read_output_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        match self {
            BifWriter::Classic { w, .. } => w.read_output_at(offset, len),
            BifWriter::Ome { w, .. } => w.read_output_at(offset, len),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            BifWriter::Classic { w, .. } => w.ifd_tile_counts(),
            BifWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            BifWriter::Classic { w, .. } => w.finish(),
            BifWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

fn bif_description_bytes(doc: &BifDoc) -> Vec<u8> {
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

fn bif_ome_xml(doc: &BifDoc, plan: &TransformPlan) -> Vec<u8> {
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
            "overlap-stitched source tiles streamed MCU-row band by MCU-row band \
             (restart-less baseline JPEG, shared JPEGTables), pasted at their \
             OpenSlide-faithful integer placements with reverse-raster overlap \
             precedence, then re-encoded into 256px output tiles; every tile \
             re-encoded (no byte passthrough exists for this format)"
                .to_string(),
        ),
        (
            "pyramid_method",
            "l0-box2: reduced output levels are the 2x2 area-average (box) downsample \
             chain of output level 0 — the source's own reduced layers are not used \
             for pixels"
                .to_string(),
        ),
        ("preserve_compose_fingerprint", PRESERVE_COMPOSE_FINGERPRINT.to_string()),
        (
            "mpp_source",
            if doc.mpp.is_some() { "bif-iscan-scanres" } else { "unknown" }.to_string(),
        ),
        (
            "objective_source",
            if doc.objective.is_some() { "bif-iscan-magnification" } else { "unknown" }
                .to_string(),
        ),
        ("pyramid_levels", (1 + doc.generated.len()).to_string()),
        ("stitched_width", doc.width.to_string()),
        ("stitched_height", doc.height.to_string()),
    ];
    if compact {
        provenance.push(("encoding_profile", plan.encoding.id().to_string()));
        provenance.push(("encoding_params_fingerprint", COMPACT_JPEG_V1_FINGERPRINT.to_string()));
    }
    provenance.push((
        "tile_payloads",
        if compact {
            format!(
                "decoded from the stitched source tiles, then re-encoded at the locked \
                 compact parameters (quality {}, subsampling {}, standard Annex-K \
                 Huffman, fingerprint {}); lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            format!(
                "decoded from the stitched source tiles, then re-encoded at the \
                 documented high-fidelity compose setting (YCbCr 4:2:2, quality {}, \
                 standard Annex-K Huffman, fingerprint {}); lossy generation on a \
                 lossy source, never claimed lossless",
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

fn level_meta(doc: &BifDoc, li: usize, compact: bool, dims: (u32, u32)) -> LevelMeta {
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
        icc: if li == 0 { doc.icc.clone() } else { None },
    }
}

/// Compose ONE generated tile: the 2×2 area-average of the previous output
/// level's tiles (read back from the committed sink; shared method with the
/// NDPI/generic-TIFF adapters).
#[allow(clippy::too_many_arguments)]
fn pyramid_tile_from_prev(
    writer: &BifWriter<'_>,
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
    // initial content (a run resumed mid-level)
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
            let (off, cnt) =
                writer.tile_record(prev_ifd, pty * prev_across as usize + ptx)?;
            let raw = writer.read_output_at(off, cnt as usize)?;
            let img = jpeg::decode_ex(
                &raw,
                (OUT_TILE as u64) * (OUT_TILE as u64),
                false,
            )?;
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
                canvas[d * 3..(d + OUT_TILE as usize) * 3]
                    .copy_from_slice(&img.data[s..s + OUT_TILE as usize * 3]);
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
// L0 stitched-band composition
// --------------------------------------------------------------------------- //

/// One referenced source tile placement of the L0 stitch (probe geometry).
#[derive(Debug, Clone)]
struct Placement {
    /// TIFF tile index (row-major in the level-0 IFD's tile grid).
    #[allow(dead_code)]
    tile: u64,
    /// Global tile grid (col,row) — the paste-precedence key.
    #[allow(dead_code)]
    col: i64,
    #[allow(dead_code)]
    row: i64,
    /// Integer paste position (floor of the fractional OpenSlide position).
    x: i64,
    y: i64,
    /// Tile payload interval in the file.
    off: u64,
    len: u64,
}

/// Collect every scanned AOI's tile placement, bounds-checked. Sorted
/// REVERSE-raster (decreasing (row, col)) — the smallest (row,col) pastes
/// LAST and wins the overlap, mirroring OpenSlide's tilemap paint order.
fn placements(
    doc: &BifDoc,
    l0_ifd: &crate::tiff_read::Ifd,
    hdr: &crate::tiff_read::TiffHeader,
    src: &dyn ByteSource,
) -> CoreResult<Vec<Placement>> {
    let l0 = &doc.levels[0];
    let mut out: Vec<Placement> = Vec::new();
    let mut cur = crate::tiff_read::TileCursor::new(src, hdr, l0_ifd)?;
    let mut pairs: Vec<(u64, u64)> = Vec::with_capacity(cur.total() as usize);
    while let Some(p) = cur.next_pair_allow_zero()? {
        pairs.push(p);
    }
    for a in &doc.areas {
        for row in a.start_row..a.start_row + a.tiles_down {
            for col in a.start_col..a.start_col + a.tiles_across {
                let idx = row as u64 * l0.tiles_across + col as u64;
                let (off, len) = pairs
                    .get(idx as usize)
                    .copied()
                    .ok_or_else(|| CoreError::validation(format!("AOI 瓦片索引 {idx} 越界")))?;
                if off == 0 || len == 0 {
                    return Err(CoreError::variant(format!(
                        "AOI 瓦片 {idx} 无载荷：稀疏 BIF 变体（probe 应已拒绝）"
                    )));
                }
                out.push(Placement {
                    tile: idx,
                    col,
                    row,
                    x: a.tile_x(col, doc.advance_x),
                    y: a.tile_y(row, doc.advance_y),
                    off,
                    len,
                });
            }
        }
    }
    out.sort_by(|a, b| (b.row, b.col).cmp(&(a.row, a.col)));
    Ok(out)
}

// --------------------------------------------------------------------------- //
// conversion
// --------------------------------------------------------------------------- //

pub fn convert_bif_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None)
}

/// Resume a BIF conversion from `resume` (see [`crate::resume`]).
pub fn convert_bif_to_bigtiff_resume(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    if resume.adapter_version.as_deref() != Some(ADAPTER_VERSION) {
        return Err(CoreError::validation(format!(
            "resume: 已提交进度属于 BIF 适配器 v{}，当前为 v{ADAPTER_VERSION}：两种适配器配方不得混合进同一输出",
            resume.adapter_version.as_deref().unwrap_or("1（字段缺失）"),
        )));
    }
    convert_inner(src, sink, scratch, plan, job, Some(resume))
}

#[allow(clippy::too_many_arguments)]
fn convert_inner(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: Option<&ResumePoint>,
) -> CoreResult<TransformResult> {
    let started = crate::job::WallInstant::now();
    let doc = probe_bif_with_budget(src, plan.limits.memory_budget_bytes)?;
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
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 BIF 输入"));
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与 BIF 输出互斥：重叠瓦片必须拼接后重编码（有损），无逐字节搬运路径",
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
        warnings.push(format!("bif_levels_generated:{}", doc.generated.len()));
    }
    if doc.icc.is_none() {
        warnings.push(WARN_NO_ICC.to_string());
    }

    // ---- placements + tile payload head cache ----------------------------- //
    let hdr = crate::tiff_read::read_header(src)?;
    let chain_l0 = {
        // IFD of level 0 (index levels[0].ifd_index in the chain)
        let mut ifd = hdr.first_ifd;
        for _ in 0..doc.levels[0].ifd_index {
            let cur = crate::tiff_read::read_ifd(src, &hdr, ifd)?;
            ifd = cur.next;
            if ifd == 0 {
                return Err(CoreError::validation("IFD 链在层级 0 之前中断"));
            }
        }
        crate::tiff_read::read_ifd(src, &hdr, ifd)?
    };
    let mut placements = placements(&doc, &chain_l0, &hdr, src)?;
    // reverse-raster paste order: DECREASING (row, col) — the smallest
    // (row,col) pastes LAST and wins the overlap (OpenSlide paint order)
    placements.sort_by(|a, b| (b.row, b.col).cmp(&(a.row, a.col)));

    // ---- working set, charged BEFORE any allocation (review §1) ---------- //
    let band_bytes = (OUT_TILE as u64)
        .saturating_mul(doc.width as u64)
        .saturating_mul(3);
    let canvas_bytes = (OUT_TILE as u64)
        .saturating_mul(OUT_TILE as u64)
        .saturating_mul(6); // tile canvas + encode buffers
    // live per-tile scanner working sets (≈2 source tile rows) are charged
    // per open: BandScanner::open charges its own planes/window BEFORE
    // allocating and the converter adds the straddling-carry band; both are
    // released when the tile leaves the flight set. The structural bound
    // Σ_areas (⌈(tile_h+256)/advance_y⌉+1)×cols stays in the estimate as
    // documentation — the hard gate is the per-open charge (stable
    // `resource_profile_insufficient` before the allocation).
    budget.charge(band_bytes, "L0 拼接带缓冲（256 行 × 拼接宽 × 3）")?;
    budget.charge(canvas_bytes, "tile 画布与编码缓冲")?;
    // placements + tile-pair table structural overhead (the per-placement
    // synthesized header cache is charged per open and released at
    // exhaustion, like the scanners)
    budget.charge(
        (placements.len() as u64)
            .saturating_mul(72)
            .saturating_add(1024 * 1024),
        "拼接位置表与瓦片区间表",
    )?;
    // the generated-pyramid working set (f² decoded prev tiles + canvases)
    let pyramid_ws = 4u64
        .saturating_mul((OUT_TILE as u64) * (OUT_TILE as u64) * 6)
        .saturating_add((OUT_TILE as u64 * 2).saturating_mul(OUT_TILE as u64 * 2).saturating_mul(3));
    if !doc.generated.is_empty() {
        budget.charge(pyramid_ws, "l0-box2 合成画布与输出缓冲")?;
    }

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => BifWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: bif_description_bytes(&doc),
        },
        OutputProfile::OmeBigTiffRgbSubifd => BifWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(bif_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, BifWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };
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
            // ---- level 0: stitched band composition --------------------- //
            if resume_done {
                // counts only — but the description must be restaged for a
                // fully-resumed level too (end_level stages it into the IFD)
                let desc: Vec<u8> = match &writer {
                    BifWriter::Classic { description, .. } => description.clone(),
                    BifWriter::Ome { .. } => Vec::new(),
                };
                level_stats.push(stats);
                ifd_chain.push((li as u32, None));
                writer.end(&meta, &desc)?;
                continue;
            }
            compose_l0(
                src,
                &doc,
                &placements,
                &mut writer,
                &cfg,
                job,
                &mut budget,
                dims,
                across,
                down,
                skip_until,
                &mut stats,
            )?;
        } else {
            // ---- reduced level: the L0-derived pyramid (l0-box2) -------- //
            if resume_done {
                let desc: Vec<u8> = match &writer {
                    BifWriter::Classic { description, .. } => description.clone(),
                    BifWriter::Ome { .. } => Vec::new(),
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
            BifWriter::Classic { description, .. } => description.clone(),
            BifWriter::Ome { .. } => Vec::new(),
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
            mode: "stitch-compose-reencode".to_string(),
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
    result.source_format = Some(SOURCE_FORMAT);
    result.validation.tile_records_emitted =
        result.levels.iter().map(|l| l.tiles_total).sum();
    Ok(result)
}

/// Per-scanner working set (mirrors the BandScanner's own charge exactly:
/// the per-component plane slack (×3 each — `planes + RGB 带 + 余量`), the
/// upsample band, AND the StreamBits 64 KiB entropy window opened on top).
/// Must stay equal to `BandScanner::open` + `StreamBits` charges or the
/// release-on-drop under-counts and the account leaks.
fn scanner_ws_estimate(doc: &BifDoc) -> u64 {
    let (mcu_w, mcu_h) = doc.mcu;
    let tile_w = doc.levels[0].tile_w as u64;
    let mcus_x = tile_w.div_ceil(mcu_w as u64);
    let mut ws = crate::jpeg::band::SCAN_CHUNK; // StreamBits window
    for (h, v) in [
        (doc.sampling.0, doc.sampling.1),
        (doc.sampling.2, doc.sampling.3),
        (doc.sampling.4, doc.sampling.5),
    ] {
        let stride = mcus_x * h as u64 * 8;
        let rows = v as u64 * 8;
        let bytes = stride.saturating_mul(rows).saturating_mul(2) + stride;
        ws = ws.saturating_add(bytes.saturating_mul(3));
    }
    ws = ws
        .saturating_add(crate::jpeg::band::SCAN_CHUNK)
        .saturating_add(tile_w.saturating_mul(mcu_h as u64).saturating_mul(9));
    ws
}

/// Live per-placement decode state: the sequential MCU-row band scanner
/// plus the one straddling band whose rows extend past the current output
/// band (a tile-local band boundary is not MCU-aligned — the paste offset
/// is arbitrary — so the last MCU row of a band is consumed but kept).
struct LiveTile<'a> {
    scanner: BandScanner<'a>,
    /// Tile-local pixel row the scanner's NEXT band starts at.
    at: u64,
    /// Carried rows of the last pulled band (tile-local [carry_y0, …)).
    carry: Option<(u64, Vec<u8>, u32)>,
    charged: u64,
}

/// Paste one decoded tile band's rows into the output band, clipped to
/// [by0, by1) and the level's x bounds.
fn paste_rows(
    band: &mut [u8],
    band_w: usize,
    by0: i64,
    by1: i64,
    top: i64,
    left: i64,
    tile_w: u32,
    level_w: u32,
    rows: &[u8],
    rows_y0: u32,
    rows_n: u32,
) {
    let abs0 = top + rows_y0 as i64;
    let y_from = abs0.max(by0) as usize - by0 as usize;
    let y_to = ((abs0 + rows_n as i64).min(by1).max(by0)) as usize - by0 as usize;
    if y_from >= y_to {
        return;
    }
    let tx0 = left.max(0);
    let tx1 = (left + tile_w as i64).min(level_w as i64);
    if tx1 <= tx0 {
        return;
    }
    let s_off = (tx0 - left) as usize;
    let copy_w = (tx1 - tx0) as usize;
    for r in y_from..y_to {
        let srow = (r + by0 as usize - abs0 as usize) * tile_w as usize * 3;
        let src = &rows[srow + s_off * 3..srow + (s_off + copy_w) * 3];
        let drow = r * band_w * 3 + tx0 as usize * 3;
        band[drow..drow + copy_w * 3].copy_from_slice(src);
    }
}

/// L0 composition: for every output tile row, paste every intersecting
/// placement into the band (reverse-raster precedence — the placements are
/// pre-sorted), then re-encode the row's 256×256 output tiles. Placement
/// scanners stay alive across the bands their tile spans (each tile is
/// decoded exactly once, top to bottom).
#[allow(clippy::too_many_arguments)]
fn compose_l0(
    src: &dyn ByteSource,
    doc: &BifDoc,
    placements: &[Placement],
    writer: &mut BifWriter<'_>,
    cfg: &jpeg::EncoderCfg,
    job: &JobControl,
    budget: &mut MemBudget,
    dims: &(u32, u32),
    across: u64,
    down: u64,
    skip_until: u64,
    stats: &mut LevelStats,
) -> CoreResult<()> {
    let l0 = &doc.levels[0];
    let band_h = OUT_TILE as i64;
    let w = dims.0 as usize;
    let tables = doc.jpeg_tables.as_deref();
    let force_rgb = doc.color == crate::bif::PayloadColor::Rgb;
    // synthetic tile header cache (header bytes, entropy offset)
    let mut headers: Vec<Option<(Vec<u8>, u64)>> = vec![None; placements.len()];
    // uncovered in-bounds regions stay BLACK exactly like OpenSlide's
    // transparent gaps (readers drop the alpha channel); only the output
    // tiles' out-of-level padding is white (never read)
    let mut band = vec![0u8; band_h as usize * w * 3];
    let mut canvas = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
    let mut live: std::collections::HashMap<usize, LiveTile<'_>> =
        std::collections::HashMap::new();
    let mcu_h = doc.mcu.1 as u64;
    let scanner_ws = scanner_ws_estimate(doc);
    let carry_ws = (doc.mcu.1 as u64)
        .saturating_mul(l0.tile_w as u64)
        .saturating_mul(3);

    let mut cell = skip_until;
    let mut ty = skip_until / across;
    while ty < down {
        job.check()?;
        let by0 = ty as i64 * band_h;
        let by1 = by0 + band_h;
        for px in band.iter_mut() {
            *px = 0;
        }
        for (pi, p) in placements.iter().enumerate() {
            let top = p.y;
            let bottom = p.y + l0.tile_h as i64;
            if bottom <= by0 || top >= by1 {
                continue;
            }
            // tile-local rows this output band needs
            let need_from = (by0 - top).max(0) as u64;
            let need_to = (by1 - top).min(l0.tile_h as i64).max(0) as u64;
            if need_from >= need_to {
                continue;
            }
            let entry = match live.remove(&pi) {
                Some(e) => e,
                None => {
                    // first band this tile meets (or a resumed band):
                    // synthesize the header, open the scanner, fast-forward
                    let (hdr_bytes, ent_off) = match &headers[pi] {
                        Some((h, e)) => (h.clone(), *e),
                        None => {
                            let want = (p.len as usize).min(crate::bif::PROBE_LIMIT as usize);
                            budget.charge(want as u64, "瓦片头读取")?;
                            let head = src.read_at(p.off, want)?;
                            budget.release(want as u64);
                            let r = crate::bif::tile_header_synthetic(tables, &head, p.off)?;
                            budget.charge(r.0.len() as u64, "瓦片合成头缓存")?;
                            headers[pi] = Some(r.clone());
                            r
                        }
                    };
                    // the scanner's own open() charges its planes/window;
                    // this adds the straddling-carry band on top
                    budget.charge(carry_ws, "瓦片跨带 carry 缓冲")?;
                    let mut scanner = BandScanner::open(
                        src,
                        &hdr_bytes,
                        // entropy starts at ent_off; the scanner adds its
                        // own scan_pos (= header length) — offset the head
                        // so the two agree
                        ent_off - hdr_bytes.len() as u64,
                        p.off + p.len,
                        force_rgb,
                        budget,
                    )?;
                    scanner.skip_to((need_from / mcu_h) as u32)?;
                    LiveTile { scanner, at: (need_from / mcu_h) * mcu_h, carry: None, charged: scanner_ws + carry_ws }
                }
            };
            let mut entry = entry;
            // carried rows first (the straddling band of the previous pass)
            if let Some((cy0, data, crows)) = entry.carry.take() {
                paste_rows(
                    &mut band, w, by0, by1, top, p.x, l0.tile_w, dims.0, &data, cy0 as u32, crows,
                );
            }
            // pull bands while they START inside the needed range; a band
            // that straddles `need_to` has its tail kept as the carry
            while entry.at < need_to {
                let Some(b) = entry.scanner.next_band()? else { break };
                let band_y0 = b.y0 as u64;
                let band_end = band_y0 + b.rows as u64;
                let next_at = entry.scanner.next_band_y0();
                if band_end > need_to {
                    // straddles into the next output band — paste the rows
                    // this band needs, keep the rest for the next pass (the
                    // scanner cannot re-read them)
                    let mut rest = b.data;
                    let cut = (need_to - band_y0) as usize * b.width as usize * 3;
                    let kept_rows = b.rows as u64 - (need_to - band_y0);
                    let keep = rest.split_off(cut.min(rest.len()));
                    paste_rows(
                        &mut band, w, by0, by1, top, p.x, l0.tile_w, dims.0, &rest, b.y0,
                        (need_to - band_y0) as u32,
                    );
                    entry.carry = Some((need_to, keep, kept_rows as u32));
                    entry.at = next_at;
                    break;
                }
                paste_rows(
                    &mut band, w, by0, by1, top, p.x, l0.tile_w, dims.0, &b.data, b.y0, b.rows,
                );
                entry.at = next_at;
            }
            // keep the scanner alive for the next band unless exhausted
            if entry.scanner.next_band_y0() < l0.tile_h as u64 || entry.carry.is_some() {
                live.insert(pi, entry);
            } else {
                if let Some((h, _)) = headers[pi].take() {
                    budget.release(h.len() as u64);
                }
                budget.release(entry.charged);
            }
        }
        // encode the band's output tiles
        for txc in 0..across {
            let tx0 = (txc as usize) * OUT_TILE as usize;
            let valid_w = (OUT_TILE as usize).min(w.saturating_sub(tx0));
            let valid_h =
                (OUT_TILE as i64).min(dims.1 as i64 - ty as i64 * OUT_TILE as i64) as usize;
            for px in canvas.iter_mut() {
                *px = 255;
            }
            for r in 0..valid_h {
                let srow = r * w * 3 + tx0 * 3;
                let drow = r * OUT_TILE as usize * 3;
                canvas[drow..drow + valid_w * 3]
                    .copy_from_slice(&band[srow..srow + valid_w * 3]);
            }
            let enc = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, cfg)?;
            writer.write_tile(&enc)?;
            stats.tiles_reencoded += 1;
            cell += 1;
            let row_done = cell.div_ceil(across);
            let total = across * down;
            maybe_progress_and_checkpoint!(writer, job, stats, 0, cell, total, row_done);
        }
        ty += 1;
    }
    for (pi, e) in live.drain() {
        if let Some((h, _)) = headers[pi].take() {
            budget.release(h.len() as u64);
        }
        budget.release(e.charged);
    }
    if cell != across * down {
        return Err(CoreError::validation(format!(
            "层 0 tile 游标 {cell} ≠ 网格总数 {}",
            across * down
        )));
    }
    Ok(())
}
