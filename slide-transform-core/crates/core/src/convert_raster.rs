//! 普通图片（BMP/JPEG）→ brightfield conversion (F8): one plain image →
//! classic multi-IFD JPEG tiled BigTIFF pyramid or RGB OME-BigTIFF.
//!
//! **What `preserve-source-v1` means for raster inputs** (stated honestly):
//! a plain image has no tiles to copy, so no byte-passthrough exists.
//! `preserve` means: decode the image through its bounded unit (BMP: exact
//! rows; JPEG with restart markers: restart segments; JPEG without
//! restarts: MCU-row bands) and re-encode every 256×256 output tile at the
//! documented high-fidelity setting **YCbCr 4:2:2 · quality 96 · standard
//! Huffman** (fingerprint [`PRESERVE_COMPOSE_FINGERPRINT`], reported as
//! `composed`). `compact-jpeg-v1` composes identically and re-encodes at
//! the locked U3 parameters.
//!
//! Reduced output levels are the `l0-box2` chain (the 2×2 area-average of
//! output level 0, read back from the committed sink).
//!
//! No physical scale exists in a BMP/JPEG: MPP stays unknown and the OME
//! output deliberately writes NO PhysicalSize (nothing is invented).
//!
//! Strict-lossless is refused (typed, before any output byte): every output
//! tile is a re-encode by construction.
//!
//! Memory (review §1): band buffers, tile canvases and every decode
//! (segment decode / band scan / BMP row read) are charged against the host
//! budget BEFORE the allocation — an over-budget input is a typed
//! `resource_profile_insufficient` refusal, never an OOM mid-decode.
//!
//! Resume mirrors the other adapters: checkpoints per committed tile row;
//! a resumed JPEG fast-forwards its decoder to the tile row's first decode
//! unit (segment scan-only / band decode-and-drop) and the fresh path stays
//! byte-identical.

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::budget::MemBudget;
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::jpeg;
use crate::jpeg::band::BandScanner;
use crate::ome_writer::{OmeBigTiffWriter, RgbIfdExtras};
use crate::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan, COMPACT_JPEG_V1_FINGERPRINT,
    COMPACT_JPEG_V1_HUFFMAN, COMPACT_JPEG_V1_QUALITY,
};
use crate::raster::{
    probe_raster_with_budget, BmpLayout, RasterDoc, RasterJpeg, ADAPTER_VERSION, OUT_TILE,
    PRESERVE_COMPOSE_FINGERPRINT, PRESERVE_COMPOSE_HUFFMAN, PRESERVE_COMPOSE_QUALITY,
    PRESERVE_COMPOSE_SAMPLING, PYRAMID_METHOD, SOURCE_FORMAT,
};
use crate::report::{ComposedSummary, LevelStats, LossyReencode, TransformResult};
use crate::resume::ResumePoint;
use crate::segment::{SegmentReader, StripGeom};

pub use crate::convert_bf::{FORMAT_CLASSIC, FORMAT_OME_RGB};

/// Row-boundary progress + checkpoint emission (the MRXS macro's shape).
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

pub const WARN_NO_ICC: &str = "color_management_not_applied";
pub const WARN_NO_PHYSICAL_SIZE: &str = "raster_no_physical_size";

fn preserve_cfg() -> jpeg::EncoderCfg {
    // Same parameters as the MRXS compose / NDPI segment compose:
    // YCbCr 4:2:2 at quality 96.
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
    icc: Option<Vec<u8>>,
}

enum RasterWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl RasterWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            RasterWriter::Classic { w, .. } => match committed {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            RasterWriter::Ome { w, ome_xml, levels } => {
                let desc = if m.reduced { None } else { ome_xml.take() };
                *levels += 1;
                w.begin_rgb_ifd_ex(
                    scratch,
                    m.width,
                    m.height,
                    m.reduced,
                    None, // no MPP: a plain image carries no physical scale
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
            RasterWriter::Classic { w, .. } => w.end_level_ex(
                m.width,
                m.height,
                m.tiff_sub,
                None,
                description,
                m.reduced,
                &LevelExtras {
                    tile: (OUT_TILE, OUT_TILE),
                    photometric: m.photometric,
                    jpeg_tables: None,
                    icc: m.icc.clone(),
                },
            ),
            RasterWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<()> {
        match self {
            RasterWriter::Classic { w, .. } => w.write_tile(data).map(|_| ()),
            RasterWriter::Ome { w, .. } => w.write_tile(data).map(|_| ()),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            RasterWriter::Classic { w, .. } => w.cursor(),
            RasterWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn tile_record(&self, ifd: usize, index: usize) -> CoreResult<(u64, u32)> {
        match self {
            RasterWriter::Classic { w, .. } => w.tile_record(ifd, index),
            RasterWriter::Ome { w, .. } => w.tile_record(ifd, index),
        }
    }

    fn read_output_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        match self {
            RasterWriter::Classic { w, .. } => w.read_output_at(offset, len),
            RasterWriter::Ome { w, .. } => w.read_output_at(offset, len),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            RasterWriter::Classic { w, .. } => w.ifd_tile_counts(),
            RasterWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            RasterWriter::Classic { w, .. } => w.finish(),
            RasterWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

fn raster_description_bytes() -> Vec<u8> {
    // MPP/objective are always null: nothing in a BMP/JPEG names a physical
    // scale and none is invented (the OME output writes no PhysicalSize).
    let s = format!(
        "{{\"adapter\": \"{}\", \"adapter_version\": \"{}\", \"composed\": \"{}\", \"pyramid\": \"{}\", \"mpp_x\": null, \"mpp_y\": null, \"objective\": null, \"source_format\": \"{}\"}}\u{0}",
        SOURCE_FORMAT,
        ADAPTER_VERSION,
        PRESERVE_COMPOSE_FINGERPRINT,
        PYRAMID_METHOD,
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

fn raster_ome_xml(doc: &RasterDoc, plan: &TransformPlan) -> Vec<u8> {
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
            format!(
                "plain image decoded through its bounded unit ({}) and re-encoded \
                 into 256px output tiles; every tile re-encoded (no byte passthrough \
                 exists for this format)",
                match doc.kind {
                    crate::raster::RasterKind::Bmp24 => "BMP rows",
                    crate::raster::RasterKind::Bmp32 => "BMP rows",
                    crate::raster::RasterKind::JpegBaseline => {
                        if doc.jpeg.as_ref().is_some_and(|j| j.restart_interval > 0) {
                            "JPEG restart segments"
                        } else {
                            "JPEG MCU-row bands"
                        }
                    }
                }
            ),
        ),
        (
            "pyramid_method",
            "l0-box2: reduced output levels are the 2x2 area-average (box) downsample \
             chain of output level 0"
                .to_string(),
        ),
        ("preserve_compose_fingerprint", PRESERVE_COMPOSE_FINGERPRINT.to_string()),
        // 无物理标尺：BMP/JPEG 不携带 µm/px，OME 刻意不写 PhysicalSize
        ("mpp_source", "none (plain image: no physical scale in BMP/JPEG)".to_string()),
        ("physical_size_written", "false".to_string()),
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
                "decoded from source pixels, then re-encoded at the locked compact \
                 parameters (quality {}, subsampling {}, standard Annex-K Huffman, \
                 fingerprint {}); lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            format!(
                "decoded from source pixels, then re-encoded at the documented \
                 high-fidelity compose setting (YCbCr 4:2:2, quality {}, standard \
                 Annex-K Huffman, fingerprint {}); lossy generation on a lossy \
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
    let mut kv = String::new();
    for (k, v) in &provenance {
        kv.push_str(&format!("<M K=\"{}\">{}</M>", xml_escape(k), xml_escape(v)));
    }
    let ann_ref = "<AnnotationRef ID=\"Annotation:0\"/>";
    let ann = format!(
        "<StructuredAnnotations><MapAnnotation ID=\"Annotation:0\" Namespace=\"{}\"><Value>{kv}</Value></MapAnnotation></StructuredAnnotations>",
        crate::ome::PROVENANCE_NS
    );
    let xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
<OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\" \
xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" \
xsi:schemaLocation=\"http://www.openmicroscopy.org/Schemas/OME/2016-06 http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd\">\
<Image ID=\"Image:0\">{refs}\
<Pixels ID=\"Pixels:0\" DimensionOrder=\"XYCZT\" Type=\"uint8\" SignificantBits=\"8\" Interleaved=\"true\" \
SizeX=\"{}\" SizeY=\"{}\" SizeC=\"3\" SizeZ=\"1\" SizeT=\"1\">\
<Channel ID=\"Channel:0:0\" SamplesPerPixel=\"3\"/>\
<TiffData IFD=\"0\" PlaneCount=\"1\"/>\
</Pixels>{ann_ref}</Image>{ann}</OME>",
        doc.width, doc.height,
        refs = "",
    );
    let mut out = xml.into_bytes();
    out.push(0);
    out
}

fn level_meta(doc: &RasterDoc, li: usize, compact: bool, dims: (u32, u32)) -> LevelMeta {
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
        icc: if li == 0 { doc.icc.clone() } else { None },
    }
}

/// Compose ONE generated tile: the 2×2 area-average of the previous output
/// level's tiles (read back from the committed sink; same method and
/// geometry as the generic-TIFF adapter's generated tail).
#[allow(clippy::too_many_arguments)]
fn pyramid_tile_from_prev(
    writer: &RasterWriter<'_>,
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
// row feeds: the three bounded decode units
// --------------------------------------------------------------------------- //

/// Fill `band[rows × width × 3]` with output pixel rows `[y0, y0 + rows)` of
/// the BMP (exact per-row reads; 24 bpp BGR / 32 bpp BGRA → RGB).
fn bmp_fill_band(
    src: &dyn ByteSource,
    layout: &BmpLayout,
    bpp32: bool,
    width: u32,
    height: u32,
    y0: u64,
    rows: u64,
    band: &mut [u8],
) -> CoreResult<()> {
    let w = width as usize;
    let step = if layout.top_down { 1i64 } else { -1i64 };
    let first = if layout.top_down {
        y0 as i64
    } else {
        height as i64 - 1 - y0 as i64
    };
    let row_bytes = layout.row_bytes as usize;
    for r in 0..rows as usize {
        let src_row_idx = first + step * r as i64;
        if src_row_idx < 0 || src_row_idx >= height as i64 {
            return Err(CoreError::oob(format!("BMP 行 {src_row_idx} 越界")));
        }
        let off = layout.data_offset + src_row_idx as u64 * layout.row_bytes;
        let raw = src.read_at(off, row_bytes)?;
        let d = &mut band[r * w * 3..][..w * 3];
        if bpp32 {
            for x in 0..w {
                d[x * 3] = raw[x * 4 + 2];
                d[x * 3 + 1] = raw[x * 4 + 1];
                d[x * 3 + 2] = raw[x * 4];
            }
        } else {
            for x in 0..w {
                d[x * 3] = raw[x * 3 + 2];
                d[x * 3 + 1] = raw[x * 3 + 1];
                d[x * 3 + 2] = raw[x * 3];
            }
        }
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// conversion
// --------------------------------------------------------------------------- //

pub fn convert_raster_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None)
}

/// Resume a raster conversion from `resume` (see [`crate::resume`]).
pub fn convert_raster_to_bigtiff_resume(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    // Adapter-version pin, enforced in the CORE (SCN/NDPI parity): a state
    // WITHOUT the field is foreign as well — never mix two adapter
    // generations into one output.
    if resume.adapter_version.as_deref() != Some(ADAPTER_VERSION) {
        return Err(CoreError::validation(format!(
            "resume: 已提交进度属于普通图片适配器 v{}，当前为 v{ADAPTER_VERSION}：两种适配器配方不得混合进同一输出",
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
    let doc = probe_raster_with_budget(src, plan.limits.memory_budget_bytes)?;
    let mut budget: MemBudget = doc.budget.clone();
    let (w, h) = (doc.width, doc.height);
    let out_levels: Vec<(u32, u32)> = std::iter::once((w, h))
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
        return Err(CoreError::variant("荧光 OME profile 不适用于明场普通图片输入"));
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与普通图片输出互斥：像素必须解码后重编码（有损），无逐字节搬运路径",
        ));
    }
    let compact = plan.encoding == EncodingProfile::CompactJpegV1;
    let cfg = if compact {
        crate::plan::compact_jpeg_v1_encoder_cfg()
    } else {
        preserve_cfg()
    };

    let mut warnings: Vec<String> = Vec::new();
    if !doc.generated.is_empty() {
        warnings.push(format!("raster_levels_generated:{}", doc.generated.len()));
    }
    if doc.icc.is_none() {
        warnings.push(WARN_NO_ICC.to_string());
    }
    warnings.push(WARN_NO_PHYSICAL_SIZE.to_string());

    // ---- working set, charged BEFORE any allocation (review §1) ---------- //
    let canvas_bytes = (OUT_TILE as u64)
        .saturating_mul(OUT_TILE as u64)
        .saturating_mul(6); // tile canvas + encode buffers
    budget.charge(canvas_bytes, "tile 画布与编码缓冲")?;
    let band_bytes: u64;
    match (&doc.bmp, &doc.jpeg) {
        (Some(_), _) => {
            band_bytes = w as u64 * OUT_TILE as u64 * 3;
            budget.charge(band_bytes, "BMP 行带缓冲（宽 × 256 行 × 3）")?;
            budget.charge(
                doc.bmp.as_ref().map(|b| b.row_bytes).unwrap_or(0),
                "BMP 单行读取窗口",
            )?;
        }
        (_, Some(j)) => {
            if j.restart_interval > 0 {
                // segment path: the band buffer is MCU-padded like NDPI's
                let band_rows_mcu = (OUT_TILE as u64).div_ceil(j.mcu.1 as u64);
                let band_rows_px = band_rows_mcu * j.mcu.1 as u64;
                let padded_w = j.mcus_x as u64 * j.mcu.0 as u64;
                band_bytes = band_rows_px.saturating_mul(padded_w).saturating_mul(3);
                budget.charge(band_bytes, "JPEG 条带缓冲（MCU 行带 × padded 宽 × 3）")?;
                let max_seg_px = (j.restart_interval as u64)
                    .saturating_mul(j.mcu.0 as u64)
                    .saturating_mul(j.mcu.1 as u64);
                budget.charge(
                    max_seg_px.saturating_mul(6),
                    "单段解码峰值预留（读取 + 解码器临时 + RGB）",
                )?;
            } else {
                // band path: BandScanner charges its own working set at open
                band_bytes = w as u64 * OUT_TILE as u64 * 3;
                budget.charge(band_bytes, "JPEG band 缓冲（宽 × 256 行 × 3）")?;
            }
        }
        _ => return Err(CoreError::header("probe 文档既非 BMP 也非 JPEG")),
    }
    // the generated-pyramid working set (f² decoded prev tiles + canvases)
    let pyramid_ws = 4u64
        .saturating_mul((OUT_TILE as u64) * (OUT_TILE as u64) * 6)
        .saturating_add((OUT_TILE as u64 * 2).saturating_mul(OUT_TILE as u64 * 2).saturating_mul(3));
    if !doc.generated.is_empty() {
        budget.charge(pyramid_ws, "l0-box2 合成画布与输出缓冲")?;
    }

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => RasterWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: raster_description_bytes(),
        },
        OutputProfile::OmeBigTiffRgbSubifd => RasterWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(raster_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, RasterWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };
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
            if resume_done {
                // no pixels needed; counts only — but the description must be
                // restaged for a fully-resumed level too (convert_ndpi 同款)
                let desc: Vec<u8> = match &writer {
                    RasterWriter::Classic { description, .. } => description.clone(),
                    RasterWriter::Ome { .. } => Vec::new(),
                };
                level_stats.push(stats);
                ifd_chain.push((li as u32, None));
                writer.end(&meta, &desc)?;
                continue;
            }
            match (&doc.bmp, &doc.jpeg) {
                (Some(layout), _) => {
                    l0_bmp(
                        src,
                        layout,
                        doc.kind == crate::raster::RasterKind::Bmp32,
                        w,
                        h,
                        across,
                        down,
                        &mut writer,
                        job,
                        &mut stats,
                        skip_until,
                        &cfg,
                    )?;
                }
                (_, Some(j)) if j.restart_interval > 0 => {
                    l0_jpeg_segments(
                        src, j, doc.kind, w, h, across, down, &mut writer, job, &mut budget,
                        &mut stats, skip_until, &cfg,
                    )?;
                }
                (_, Some(j)) => {
                    l0_jpeg_bands(
                        src, j, w, h, across, down, &mut writer, job, &mut budget, &mut stats,
                        skip_until, &cfg,
                    )?;
                }
                _ => return Err(CoreError::header("probe 文档既非 BMP 也非 JPEG")),
            }
            if stats.tiles_reencoded != tiles_total {
                return Err(CoreError::validation(format!(
                    "层 0 tile 游标 {} ≠ 网格总数 {tiles_total}",
                    stats.tiles_reencoded
                )));
            }
            budget.release(band_bytes);
        } else {
            // ---- reduced level: the L0-derived pyramid (l0-box2) -------- //
            if resume_done {
                let desc: Vec<u8> = match &writer {
                    RasterWriter::Classic { description, .. } => description.clone(),
                    RasterWriter::Ome { .. } => Vec::new(),
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
            RasterWriter::Classic { description, .. } => description.clone(),
            RasterWriter::Ome { .. } => Vec::new(),
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
        width: w,
        height: h,
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
            mode: "raster-compose-reencode".to_string(),
            fingerprint: PRESERVE_COMPOSE_FINGERPRINT.to_string(),
            quality: PRESERVE_COMPOSE_QUALITY,
            sampling: "4:2:2".to_string(),
            huffman: PRESERVE_COMPOSE_HUFFMAN.to_string(),
            tiles_composed: tiles_reencoded,
            tiles_filled: 0,
            tiles_deduped: 0,
            pyramid: PYRAMID_METHOD.to_string(),
        }),
        associated: Vec::new(),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    result.validation.tile_records_emitted =
        result.levels.iter().map(|l| l.tiles_total).sum();
    Ok(result)
}

/// L0 loop, BMP feed: exact row reads → band → tiles.
#[allow(clippy::too_many_arguments)]
fn l0_bmp(
    src: &dyn ByteSource,
    layout: &BmpLayout,
    bpp32: bool,
    w: u32,
    h: u32,
    across: u64,
    down: u64,
    writer: &mut RasterWriter<'_>,
    job: &JobControl,
    stats: &mut LevelStats,
    skip_until: u64,
    cfg: &jpeg::EncoderCfg,
) -> CoreResult<()> {
    let band_rows = OUT_TILE as u64;
    let mut band = vec![255u8; w as usize * band_rows as usize * 3];
    let mut canvas = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
    let mut cell = skip_until;
    let mut ty = skip_until / across;
    while ty < down {
        job.check()?;
        let y0 = ty * band_rows;
        let rows = band_rows.min(h as u64 - y0);
        for px in band.iter_mut() {
            *px = 255;
        }
        bmp_fill_band(src, layout, bpp32, w, h, y0, rows, &mut band)?;
        for txc in 0..across {
            let tx0 = txc * OUT_TILE as u64;
            let valid_w = (OUT_TILE as u64).min(w as u64 - tx0) as usize;
            let valid_h = (OUT_TILE as u64).min(h as u64 - ty * OUT_TILE as u64) as usize;
            for px in canvas.iter_mut() {
                *px = 255;
            }
            for r in 0..valid_h {
                let srow = (r as u64 * w as u64 + tx0) as usize * 3;
                let drow = r * OUT_TILE as usize * 3;
                canvas[drow..drow + valid_w * 3]
                    .copy_from_slice(&band[srow..srow + valid_w * 3]);
            }
            let enc = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, cfg)?;
            writer.write_tile(&enc)?;
            stats.tiles_reencoded += 1;
            cell += 1;
            let row_done = cell.div_ceil(across);
            maybe_progress_and_checkpoint!(writer, job, stats, 0, cell, across * down, row_done);
        }
        ty += 1;
    }
    Ok(())
}

/// L0 loop, JPEG feed with restart markers: restart-segment decode → paste
/// → tiles (the NDPI band loop, over the whole file as one strip).
#[allow(clippy::too_many_arguments)]
fn l0_jpeg_segments(
    src: &dyn ByteSource,
    j: &RasterJpeg,
    kind: crate::raster::RasterKind,
    w: u32,
    h: u32,
    across: u64,
    down: u64,
    writer: &mut RasterWriter<'_>,
    job: &JobControl,
    budget: &mut MemBudget,
    stats: &mut LevelStats,
    skip_until: u64,
    cfg: &jpeg::EncoderCfg,
) -> CoreResult<()> {
    let _ = kind;
    let geom = StripGeom {
        strip_offset: 0,
        strip_bytes: src.size(),
        mcu_w: j.mcu.0,
        mcu_h: j.mcu.1,
        mcus_x: j.mcus_x,
        total_mcus: j.total_mcus,
        restart_interval: j.restart_interval,
        segments: j.segments,
        header_bytes: j.header_bytes.clone(),
        sof_hw_at: j.sof_hw_at,
        dri_at: j.dri_at,
    };
    let band_rows_mcu = (OUT_TILE as u64).div_ceil(j.mcu.1 as u64);
    let band_rows_px = band_rows_mcu * j.mcu.1 as u64;
    let padded_w = j.mcus_x as u64 * j.mcu.0 as u64;
    let mut reader = SegmentReader::new(src, &geom);
    if skip_until > 0 {
        let ty = skip_until / across;
        let first_seg = ty * (OUT_TILE as u64) / j.mcu.1 as u64 * j.mcus_x as u64
            / j.restart_interval as u64;
        reader.skip_to(budget, first_seg)?;
    }
    let mut band = vec![255u8; band_rows_px as usize * padded_w as usize * 3];
    let mut canvas = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
    let mut carry: Option<crate::segment::SegmentRead> = None;
    let mut cell = skip_until;
    let mut ty = skip_until / across;
    while ty < down {
        job.check()?;
        let band_y0 = ty * OUT_TILE as u64;
        let band_rows_u = band_rows_px.min(h as u64 - band_y0);
        for px in band.iter_mut() {
            *px = 255;
        }
        let mcu_end = band_y0.saturating_add(band_rows_u)
            .div_ceil(j.mcu.1 as u64)
            * j.mcus_x as u64;
        loop {
            let next_mcu = match &carry {
                Some(c) => c.mcu_start,
                None => reader.next_k() * j.restart_interval as u64,
            };
            if next_mcu >= mcu_end {
                break;
            }
            let seg = match carry.take() {
                Some(c) => c,
                None => reader.decode_next(budget)?,
            };
            crate::segment::paste_segment_rects(
                &mut band,
                &seg,
                j.mcu.0,
                j.mcu.1,
                j.mcus_x,
                h,
                band_y0,
                band_rows_u,
            );
            let seg_bottom =
                (seg.mcu_start + seg.mcus).div_ceil(j.mcus_x as u64) * j.mcu.1 as u64;
            if seg_bottom > band_y0 + band_rows_u && reader.next_k() < j.segments {
                carry = Some(seg);
            } else {
                budget.release(seg.charged);
            }
        }
        for txc in 0..across {
            let tx0 = txc * OUT_TILE as u64;
            let valid_w = (OUT_TILE as u64).min(w as u64 - tx0) as usize;
            let valid_h = (OUT_TILE as u64).min(h as u64 - ty * OUT_TILE as u64) as usize;
            for px in canvas.iter_mut() {
                *px = 255;
            }
            for r in 0..valid_h {
                let srow = r * padded_w as usize * 3 + tx0 as usize * 3;
                let drow = r * OUT_TILE as usize * 3;
                canvas[drow..drow + valid_w * 3]
                    .copy_from_slice(&band[srow..srow + valid_w * 3]);
            }
            let enc = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, cfg)?;
            writer.write_tile(&enc)?;
            stats.tiles_reencoded += 1;
            cell += 1;
            let row_done = cell.div_ceil(across);
            maybe_progress_and_checkpoint!(writer, job, stats, 0, cell, across * down, row_done);
        }
        ty += 1;
    }
    if let Some(c) = carry.take() {
        budget.release(c.charged);
    }
    Ok(())
}

/// L0 loop, JPEG feed WITHOUT restart markers: MCU-row band decode
/// ([`BandScanner`]) → band → tiles.
#[allow(clippy::too_many_arguments)]
fn l0_jpeg_bands(
    src: &dyn ByteSource,
    j: &RasterJpeg,
    w: u32,
    h: u32,
    across: u64,
    down: u64,
    writer: &mut RasterWriter<'_>,
    job: &JobControl,
    budget: &mut MemBudget,
    stats: &mut LevelStats,
    skip_until: u64,
    cfg: &jpeg::EncoderCfg,
) -> CoreResult<()> {
    let force_rgb = j.color == jpeg::TiffJpegColor::Rgb;
    let mut scanner =
        BandScanner::open(src, &j.header_bytes, j.header_offset, src.size(), force_rgb, budget)?;
    if skip_until > 0 {
        let ty = skip_until / across;
        let first_band = ty * OUT_TILE as u64 / j.mcu.1 as u64;
        scanner.skip_to(first_band as u32)?;
    }
    let band_rows = OUT_TILE as u64;
    let mut band = vec![255u8; w as usize * band_rows as usize * 3];
    let mut canvas = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
    let mut cell = skip_until;
    let mut ty = skip_until / across;
    while ty < down {
        job.check()?;
        let y0 = ty * band_rows;
        let rows = band_rows.min(h as u64 - y0);
        for px in band.iter_mut() {
            *px = 255;
        }
        // decode the MCU-row bands covering this tile row
        while scanner.next_band_y0() < y0 + rows {
            let b = scanner
                .next_band()?
                .ok_or_else(|| CoreError::jpeg("JPEG 扫描在 tile 行结束前耗尽（熵数据截断）"))?;
            let dst0 = (b.y0 as u64 - y0) as usize;
            for r in 0..b.rows as usize {
                let s = r * w as usize * 3;
                let d = (dst0 + r) * w as usize * 3;
                band[d..d + w as usize * 3].copy_from_slice(&b.data[s..s + w as usize * 3]);
            }
        }
        for txc in 0..across {
            let tx0 = txc * OUT_TILE as u64;
            let valid_w = (OUT_TILE as u64).min(w as u64 - tx0) as usize;
            let valid_h = (OUT_TILE as u64).min(h as u64 - ty * OUT_TILE as u64) as usize;
            for px in canvas.iter_mut() {
                *px = 255;
            }
            for r in 0..valid_h {
                let srow = (r as u64 * w as u64 + tx0) as usize * 3;
                let drow = r * OUT_TILE as usize * 3;
                canvas[drow..drow + valid_w * 3]
                    .copy_from_slice(&band[srow..srow + valid_w * 3]);
            }
            let enc = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, cfg)?;
            writer.write_tile(&enc)?;
            stats.tiles_reencoded += 1;
            cell += 1;
            let row_done = cell.div_ceil(across);
            maybe_progress_and_checkpoint!(writer, job, stats, 0, cell, across * down, row_done);
        }
        ty += 1;
    }
    Ok(())
}

/// Convenience wrapper without progress (tests).
pub fn convert_raster(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_raster_to_bigtiff(src, sink, scratch, plan, &job)
}
