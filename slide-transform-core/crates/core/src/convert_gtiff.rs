//! Generic tiled JPEG TIFF → brightfield conversion (F5): baseline-JPEG
//! tiles → classic multi-IFD JPEG BigTIFF pyramid or RGB OME-BigTIFF
//! (SubIFD pyramid).
//!
//! Payload policy for PRESENT source levels is the SVS **pure passthrough**:
//! every tile is copied verbatim, in tile order, level by level (self-
//! contained JPEG streams; an optional shared `JPEGTables` tag 347 rides
//! along). Edge policy is SVS semantics: interior non-nominal tiles are a
//! typed refusal, boundary tiles smaller than the nominal rect pass through
//! and are recorded in `edge_regions`.
//!
//! MISSING reduced levels (the doc's `generated` tail) are the `l0-box2`
//! chain: each generated tile is the 2×2 area-average of the PREVIOUS
//! OUTPUT level's tiles, read back from the committed sink (mirroring the
//! MRXS adapter) — decode, paste onto a white tile-sized canvas, average
//! the valid region, re-encode at the locked [`GEN_QUALITY`]/[`GEN_SAMPLING`]
//! parameters (`compact-jpeg-v1` swaps in the locked compact parameters).
//! Out-of-image canvas stays white (the brightfield padding convention);
//! edge averages include that padding by construction (documented).
//!
//! `compact-jpeg-v1` (U3): source tiles are decoded (adapter colourspace
//! rule) and re-encoded at the LOCKED compact parameters; generated tiles
//! use the same locked parameters. The whole output is lossy then.
//!
//! Resume mirrors [`crate::convert_svs`]: checkpoints per committed tile
//! row; the resumed output is byte-identical (generated tiles re-compose
//! deterministically from the committed previous level).

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::budget::MemBudget;
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::convert_svs::{FORMAT_CLASSIC, FORMAT_OME_RGB};
use crate::error::{CoreError, CoreResult};
use crate::gtiff::{
    self, GtiffDoc, GtiffLevel, PayloadColor, ADAPTER_VERSION, GEN_FINGERPRINT, GEN_QUALITY,
    GEN_SAMPLING, PYRAMID_METHOD, SOURCE_FORMAT,
};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::ome::py_repr_f64;
use crate::ome_writer::{OmeBigTiffWriter, RgbIfdExtras};
use crate::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan, COMPACT_JPEG_V1_FINGERPRINT,
    COMPACT_JPEG_V1_HUFFMAN, COMPACT_JPEG_V1_QUALITY,
};
use crate::report::{EdgeRegion, LevelStats, LossyReencode, TransformResult};
use crate::resume::ResumePoint;
use crate::tiff_read::{self, TiffHeader};

/// Warning: the source pyramid ended above the 256-px side threshold and
/// the `l0-box2` tail levels were generated.
pub const WARN_GTIFF_LEVELS_GENERATED: &str = "gtiff_missing_levels_generated";
/// Warning: cropped source tiles passed through verbatim.
pub const WARN_GTIFF_EDGE_PASSTHROUGH: &str = "gtiff_cropped_edge_tile_passthrough";
/// Warning: no ICC profile present — colour management is not applied.
pub const WARN_NO_ICC: &str = "color_management_not_applied";

/// One output level: a verbatim source level or a generated `l0-box2` one.
#[derive(Debug, Clone, Copy)]
enum OutLevel {
    Source(usize),
    Generated(usize),
}

struct LevelMeta {
    width: u32,
    height: u32,
    tile: (u32, u32),
    photometric: u16,
    tiff_sub: (u16, u16),
    reduced: bool,
    mpp: Option<(f64, f64)>,
    jpeg_tables: Option<Vec<u8>>,
    icc: Option<Vec<u8>>,
}

/// The two brightfield layouts share the tile stream (see `convert_svs`).
enum GtiffWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl GtiffWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed_tiles: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            GtiffWriter::Classic { w, .. } => match committed_tiles {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            GtiffWriter::Ome { w, ome_xml, levels } => {
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
                    committed_tiles,
                    &RgbIfdExtras {
                        tile: m.tile,
                        photometric: m.photometric,
                        jpeg_tables: m.jpeg_tables.clone(),
                        icc: m.icc.clone(),
                    },
                )
            }
        }
    }

    fn end(&mut self, m: &LevelMeta, description: &[u8]) -> CoreResult<()> {
        match self {
            GtiffWriter::Classic { w, .. } => w.end_level_ex(
                m.width,
                m.height,
                m.tiff_sub,
                m.mpp,
                description,
                m.reduced,
                &LevelExtras {
                    tile: m.tile,
                    photometric: m.photometric,
                    jpeg_tables: m.jpeg_tables.clone(),
                    icc: m.icc.clone(),
                },
            ),
            GtiffWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        match self {
            GtiffWriter::Classic { w, .. } => w.write_tile(data),
            GtiffWriter::Ome { w, .. } => w.write_tile(data),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            GtiffWriter::Classic { w, .. } => w.cursor(),
            GtiffWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            GtiffWriter::Classic { w, .. } => w.ifd_tile_counts(),
            GtiffWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            GtiffWriter::Classic { w, .. } => w.finish(),
            GtiffWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }

    /// (offset, count) of one committed tile of begun IFD `ifd`.
    fn tile_record(&self, ifd: usize, index: usize) -> CoreResult<(u64, u32)> {
        match self {
            GtiffWriter::Classic { w, .. } => w.tile_record(ifd, index),
            GtiffWriter::Ome { w, .. } => w.tile_record(ifd, index),
        }
    }

    fn read_output_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        match self {
            GtiffWriter::Classic { w, .. } => w.read_output_at(offset, len),
            GtiffWriter::Ome { w, .. } => w.read_output_at(offset, len),
        }
    }
}

/// JSON description of the classic profile (same `json.dumps(sort_keys=True)
/// + NUL` convention as the SVS/SCN paths; unknown values are `null`, never
/// invented).
fn gtiff_description_bytes(doc: &GtiffDoc) -> Vec<u8> {
    let mpp = doc.mpp;
    let s = format!(
        "{{\"adapter\": \"{}\", \"adapter_version\": \"{}\", \"mpp_x\": {}, \"mpp_y\": {}, \"objective\": null, \"pyramid_method\": \"{}\", \"source_levels\": {}, \"generated_levels\": {}, \"source_format\": \"{}\"}}\u{0}",
        SOURCE_FORMAT,
        ADAPTER_VERSION,
        mpp.map(py_repr_f64).unwrap_or_else(|| "null".into()),
        mpp.map(py_repr_f64).unwrap_or_else(|| "null".into()),
        PYRAMID_METHOD,
        doc.levels.len(),
        doc.generated.len(),
        SOURCE_FORMAT,
    );
    s.into_bytes()
}

/// OME-XML (2016-06) of the generic-TIFF-derived RGB profile — same shape
/// as the SVS/SCN ones, with l0-box2 provenance.
fn gtiff_ome_xml(doc: &GtiffDoc, plan: &TransformPlan) -> Vec<u8> {
    let main = &doc.levels[0];
    let mut provenance: Vec<(&str, String)> = vec![
        ("converter", "slide-transform-core".to_string()),
        ("converter_version", plan.core_version.clone()),
        ("output_profile", plan.profile.id().to_string()),
        ("source_format", SOURCE_FORMAT.to_string()),
        ("source_adapter", SOURCE_FORMAT.to_string()),
        ("adapter_version", ADAPTER_VERSION.to_string()),
    ];
    provenance.push((
        "mpp_source",
        if doc.mpp.is_some() { "tiff-resolution-tags" } else { "unknown" }.to_string(),
    ));
    provenance.push(("pyramid_method", PYRAMID_METHOD.to_string()));
    provenance.push(("source_levels", doc.levels.len().to_string()));
    provenance.push(("generated_levels", doc.generated.len().to_string()));
    if plan.encoding == EncodingProfile::CompactJpegV1 {
        provenance.push(("encoding_profile", plan.encoding.id().to_string()));
        provenance.push(("encoding_params_fingerprint", COMPACT_JPEG_V1_FINGERPRINT.to_string()));
    }
    provenance.push((
        "tile_payloads",
        if plan.encoding == EncodingProfile::CompactJpegV1 {
            format!(
                "every source tile decoded (shared JPEGTables merged, adapter colourspace \
                 rule) and re-encoded at the locked compact parameters (quality {}, subsampling \
                 {}, standard Annex-K Huffman, fingerprint {}); generated l0-box2 tail levels \
                 re-encoded with the same parameters; lossy",
                COMPACT_JPEG_V1_QUALITY, compact_sampling_label(), COMPACT_JPEG_V1_FINGERPRINT
            )
            .to_string()
        } else {
            format!(
                "source JPEG tiles copied byte-for-byte; generated reduced levels are the 2x2 \
                 area-average (box) chain of the last present source level ({PYRAMID_METHOD}) \
                 re-encoded at quality {GEN_QUALITY}, subsampling 4:2:2, standard Annex-K \
                 Huffman ({GEN_FINGERPRINT}); no source tile is re-encoded or relabelled"
            )
        },
    ));
    provenance.push((
        "pixel_policy",
        match plan.pixel_policy {
            PixelPolicy::AllowEdgeReencode => "preserve-source-passthrough",
            PixelPolicy::StrictLossless => "strict-lossless",
        }
        .to_string(),
    ));
    let mut kv = String::new();
    for (k, v) in &provenance {
        kv.push_str(&format!("<M K=\"{}\">{}</M>", xml_escape(k), xml_escape(v)));
    }
    let phys = match doc.mpp {
        Some(mpp) if mpp.is_finite() && mpp > 0.0 => format!(
            " PhysicalSizeX=\"{}\" PhysicalSizeXUnit=\"µm\" PhysicalSizeY=\"{}\" PhysicalSizeYUnit=\"µm\"",
            py_repr_f64(mpp),
            py_repr_f64(mpp)
        ),
        _ => String::new(),
    };
    let ann = format!(
        "<StructuredAnnotations><MapAnnotation ID=\"Annotation:0\" Namespace=\"{}\"><Value>{kv}</Value></MapAnnotation></StructuredAnnotations>",
        crate::ome::PROVENANCE_NS
    );
    let xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
<OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\" \
xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" \
xsi:schemaLocation=\"http://www.openmicroscopy.org/Schemas/OME/2016-06 http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd\">\
<Image ID=\"Image:0\">\
<Pixels ID=\"Pixels:0\" DimensionOrder=\"XYCZT\" Type=\"uint8\" SignificantBits=\"8\" Interleaved=\"true\" \
SizeX=\"{}\" SizeY=\"{}\" SizeC=\"3\" SizeZ=\"1\" SizeT=\"1\"{phys}>\
<Channel ID=\"Channel:0:0\" SamplesPerPixel=\"3\"/>\
<TiffData IFD=\"0\" PlaneCount=\"1\"/>\
</Pixels><AnnotationRef ID=\"Annotation:0\"/></Image>{ann}</OME>",
        main.width, main.height,
    );
    let mut out = xml.into_bytes();
    out.push(0);
    out
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

/// Per-level output metadata (SVS semantics; generated levels carry the
/// locked generated-tile parameters).
fn level_meta(doc: &GtiffDoc, out: OutLevel, compact: bool) -> LevelMeta {
    let main = &doc.levels[0];
    let (width, height, reduced, src_level) = match out {
        OutLevel::Source(i) => {
            let lv = &doc.levels[i];
            (lv.width, lv.height, i > 0, Some(i))
        }
        OutLevel::Generated(i) => {
            let (w, h) = doc.generated[i];
            (w, h, true, None)
        }
    };
    let mpp = doc.mpp.map(|m| {
        let (rx, ry) = (
            main.width as f64 / width as f64,
            main.height as f64 / height as f64,
        );
        (m * rx, m * ry)
    });
    match out {
        OutLevel::Source(i) => {
            let lv = &doc.levels[i];
            let (photometric, tiff_sub, jpeg_tables) = if compact {
                (6u16, compact_sampling_tiff(), None)
            } else {
                match lv.color {
                    PayloadColor::Rgb => (2u16, (1u16, 1u16), lv.jpeg_tables.clone()),
                    PayloadColor::YCbCr => (
                        6u16,
                        (lv.sampling.0 as u16, lv.sampling.1 as u16),
                        lv.jpeg_tables.clone(),
                    ),
                }
            };
            LevelMeta {
                width,
                height,
                tile: (lv.tile_w, lv.tile_h),
                photometric,
                tiff_sub,
                reduced,
                mpp,
                jpeg_tables,
                icc: if src_level == Some(0) { doc.icc.clone() } else { None },
            }
        }
        OutLevel::Generated(_) => LevelMeta {
            width,
            height,
            tile: (main.tile_w, main.tile_h),
            photometric: 6,
            tiff_sub: if compact { compact_sampling_tiff() } else { (2, 1) },
            reduced,
            mpp,
            jpeg_tables: None,
            icc: None,
        },
    }
}

/// Verify one present source tile's JPEG header against the level contract.
fn check_tile(payload: &[u8], lv: &GtiffLevel) -> CoreResult<crate::jpeg::JpegProbe> {
    let probe = crate::jpeg::scan_jpeg(payload)?;
    if probe.sampling != Some(lv.sampling) {
        return Err(CoreError::validation(format!(
            "层 ifd={} tile 与探测到的 JPEG 契约不一致（采样不齐）",
            lv.ifd_index
        )));
    }
    if probe.width as u64 > lv.tile_w as u64 || probe.height as u64 > lv.tile_h as u64 {
        return Err(CoreError::variant(format!(
            "层 ifd={} tile 尺寸 {}×{} 超过标称 {}×{}",
            lv.ifd_index, probe.width, probe.height, lv.tile_w, lv.tile_h
        )));
    }
    Ok(probe)
}

/// Compact-jpeg-v1 re-encode of one source tile (U3 merged path). A cropped
/// tile is pasted onto a white tile-sized canvas.
fn compact_reencode_tile(
    payload: &[u8],
    lv: &GtiffLevel,
    probe: &crate::jpeg::JpegProbe,
    cfg: &crate::jpeg::EncoderCfg,
) -> CoreResult<Vec<u8>> {
    let merged;
    let stream: &[u8] = match &lv.jpeg_tables {
        Some(t) => {
            merged = gtiff::merge_tables_then_tile(t, payload);
            &merged
        }
        None => payload,
    };
    let force_rgb = lv.color == PayloadColor::Rgb;
    let max_pixels = (lv.tile_w as u64) * (lv.tile_h as u64);
    let img = crate::jpeg::decode_ex(stream, max_pixels, force_rgb)?;
    if img.width != probe.width || img.height != probe.height {
        return Err(CoreError::jpeg(format!(
            "tile 解码尺寸 {}×{} 与探测 {}×{} 不符",
            img.width, img.height, probe.width, probe.height
        )));
    }
    if img.width == lv.tile_w && img.height == lv.tile_h {
        crate::jpeg::encode_rgb(&img.data, img.width, img.height, cfg)
    } else {
        let mut canvas = vec![255u8; (lv.tile_w as usize) * (lv.tile_h as usize) * 3];
        for y in 0..img.height as usize {
            let s = y * img.width as usize * 3;
            let d = y * lv.tile_w as usize * 3;
            canvas[d..d + img.width as usize * 3]
                .copy_from_slice(&img.data[s..s + img.width as usize * 3]);
        }
        crate::jpeg::encode_rgb(&canvas, lv.tile_w, lv.tile_h, cfg)
    }
}

fn record_edge(
    lv: &GtiffLevel,
    row: u64,
    col: u64,
    w: u32,
    h: u32,
    compact: bool,
    edge_regions: &mut Vec<EdgeRegion>,
    warnings: &mut Vec<String>,
) {
    if compact {
        edge_regions.push(EdgeRegion {
            level: lv.ifd_index,
            channel: None,
            x: (col * lv.tile_w as u64) as u32,
            y: (row * lv.tile_h as u64) as u32,
            source_w: w,
            source_h: h,
            canvas_w: lv.tile_w,
            canvas_h: lv.tile_h,
            reused_qtables: false,
        });
        return;
    }
    if !warnings.iter().any(|w| w == WARN_GTIFF_EDGE_PASSTHROUGH) {
        warnings.push(WARN_GTIFF_EDGE_PASSTHROUGH.to_string());
    }
    edge_regions.push(EdgeRegion {
        level: lv.ifd_index,
        channel: None,
        x: (col * lv.tile_w as u64) as u32,
        y: (row * lv.tile_h as u64) as u32,
        source_w: w,
        source_h: h,
        canvas_w: lv.tile_w,
        canvas_h: lv.tile_h,
        reused_qtables: true, // nothing was re-encoded
    });
}

/// Re-scan the boundary cells of a committed source level for resume
/// bookkeeping (no decode of interior cells). Present interior cells are
/// nominal by the probe's accept rules.
#[allow(clippy::too_many_arguments)]
fn reconstruct_cells_source(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &tiff_read::Ifd,
    lv: &GtiffLevel,
    up_to: u64,
    compact: bool,
    stats: &mut LevelStats,
    edge_regions: &mut Vec<EdgeRegion>,
    warnings: &mut Vec<String>,
) -> CoreResult<()> {
    let mut cur = tiff_read::TileCursor::new(src, hdr, ifd)?;
    let mut cell = 0u64;
    while cell < up_to {
        let Some((off, len)) = cur.next_pair()? else { break };
        let row = cell / lv.tiles_across as u64;
        let col = cell % lv.tiles_across as u64;
        let is_edge = row + 1 == lv.tiles_down as u64 || col + 1 == lv.tiles_across as u64;
        if is_edge {
            let payload = src.read_at(off, len as usize)?;
            let probe = crate::jpeg::scan_jpeg(&payload)?;
            if probe.width as u32 != lv.tile_w || probe.height as u32 != lv.tile_h {
                record_edge(lv, row, col, probe.width, probe.height, compact, edge_regions, warnings);
            }
        }
        if compact {
            stats.tiles_reencoded += 1;
        } else {
            stats.tiles_raw_copied += 1;
        }
        cell += 1;
    }
    Ok(())
}

/// Compose ONE generated tile: the 2×2 area-average of the previous output
/// level's tiles (read back from the committed sink). `canvas`/`out` are
/// reused scratch buffers (both sized for the composition side / the tile).
/// Out-of-image regions stay white; a cropped previous tile pastes only its
/// valid rect.
#[allow(clippy::too_many_arguments)]
fn generated_tile_from_prev(
    writer: &GtiffWriter<'_>,
    prev_ifd: usize,
    prev_across: u32,
    prev_down: u32,
    cur_w: u32,
    cur_h: u32,
    cur_across: u64,
    tile_w: u32,
    tile_h: u32,
    cell: u64,
    out: &mut [u8],
    canvas: &mut [u8],
    prev_tables: Option<&[u8]>,
) -> CoreResult<()> {
    const F: usize = 2;
    // non-square tiles: the canvas pitch is 2·tile_w COLUMNS but 2·tile_h
    // ROWS (the review #1 out-of-bounds was a square-canvas assumption)
    let pitch = (tile_w as usize) * F;
    let canvas_rows = (tile_h as usize) * F;
    let canvas = &mut canvas[..pitch * canvas_rows * 3];
    let out = &mut out[..(tile_w as usize) * (tile_h as usize) * 3];
    // the level's extent clips the last tile row/column: out-of-level canvas
    // stays white (brightfield padding), never stale bytes from a previous
    // composition
    for px in canvas.chunks_exact_mut(3) {
        px.copy_from_slice(&[255, 255, 255]);
    }
    let tx = (cell % cur_across) as usize;
    let ty = (cell / cur_across) as usize;
    let valid_w = (tile_w as usize).min(cur_w as usize - tx * tile_w as usize);
    let valid_h = (tile_h as usize).min(cur_h as usize - ty * tile_h as usize);
    for pty in (ty * F)..((ty + 1) * F) {
        for ptx in (tx * F)..((tx + 1) * F) {
            if ptx >= prev_across as usize || pty >= prev_down as usize {
                continue; // beyond the previous level's edge: never read below
            }
            let (off, cnt) = writer.tile_record(prev_ifd, pty * prev_across as usize + ptx)?;
            let raw = writer.read_output_at(off, cnt as usize)?;
            // a PASSTHROUGH source level's committed tiles are abbreviated
            // JPEG streams (shared JPEGTables live in the level's tag 347) —
            // merge them back in before the decode, exactly like readers do
            let merged;
            let stream: &[u8] = match prev_tables {
                Some(t) => {
                    merged = gtiff::merge_tables_then_tile(t, &raw);
                    &merged
                }
                None => &raw,
            };
            let img = crate::jpeg::decode_ex(
                stream,
                (tile_w as u64) * (tile_h as u64),
                false,
            )?;
            if img.width as u64 > tile_w as u64 || img.height as u64 > tile_h as u64 {
                return Err(CoreError::variant(format!(
                    "金字塔读取：层 {prev_ifd} tile {} 尺寸 {}×{} 超过标称 {tile_w}×{tile_h}",
                    pty * prev_across as usize + ptx,
                    img.width,
                    img.height
                )));
            }
            let bx = (ptx - tx * F) * tile_w as usize;
            let by = (pty - ty * F) * tile_h as usize;
            for row in 0..img.height as usize {
                let s = row * img.width as usize * 3;
                let d = (by + row) * pitch + bx;
                match img.kind {
                    crate::jpeg::ColorKind::Rgb => {
                        canvas[d * 3..(d + img.width as usize) * 3]
                            .copy_from_slice(&img.data[s..s + img.width as usize * 3]);
                    }
                    crate::jpeg::ColorKind::Gray => {
                        for col in 0..img.width as usize {
                            let g = img.data[s + col];
                            let o = (d + col) * 3;
                            canvas[o..o + 3].copy_from_slice(&[g, g, g]);
                        }
                    }
                }
            }
        }
    }
    // 2×2 area-average over the valid region (each sample sits inside the
    // previous level's extent by the floor-size convention)
    let log = 2 * F.trailing_zeros();
    for oy in 0..valid_h {
        for ox in 0..valid_w {
            for c in 0..3 {
                let mut acc = 0u32;
                for dy in 0..F {
                    let row = (oy * F + dy) * pitch + ox * F;
                    for dx in 0..F {
                        acc += canvas[(row + dx) * 3 + c] as u32;
                    }
                }
                out[(oy * tile_w as usize + ox) * 3 + c] = (acc >> log) as u8;
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn convert_gtiff_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None)
}

/// Resume a generic-TIFF conversion from `resume` (see [`crate::resume`]).
#[allow(clippy::too_many_arguments)]
pub fn convert_gtiff_to_bigtiff_resume(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    // Adapter-version pin, enforced in the CORE (SCN/MRXS parity): a state
    // WITHOUT the field is foreign as well — never mix two adapter
    // generations into one output.
    if resume.adapter_version.as_deref() != Some(ADAPTER_VERSION) {
        return Err(CoreError::validation(format!(
            "resume: 已提交进度属于通用 TIFF 适配器 v{}，当前为 v{ADAPTER_VERSION}：两种适配器配方不得混合进同一输出",
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
    // the probe charges its allocations against the plan's memory budget
    // BEFORE making them (review §1; typed resource_profile_insufficient)
    let doc = gtiff::probe_gtiff_with_budget(src, plan.limits.memory_budget_bytes)?;
    let mut budget: MemBudget = doc.budget.clone();
    let hdr = tiff_read::read_header(src)?;
    let chain = tiff_read::ifd_chain(src, &hdr)?;
    let levels = &doc.levels;
    let out_levels: Vec<OutLevel> = levels
        .iter()
        .enumerate()
        .map(|(i, _)| OutLevel::Source(i))
        .chain((0..doc.generated.len()).map(OutLevel::Generated))
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
        return Err(CoreError::variant("荧光 OME profile 不适用于明场通用 TIFF"));
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless
        && (plan.encoding == EncodingProfile::CompactJpegV1 || !doc.generated.is_empty())
    {
        return Err(CoreError::policy(
            "strict-lossless 无法达成：该输入包含重编码路径（compact 画质或生成的 l0-box2 降采样层）",
        ));
    }
    let compact = plan.encoding == EncodingProfile::CompactJpegV1;
    let compact_cfg = if compact {
        Some(crate::plan::compact_jpeg_v1_encoder_cfg())
    } else {
        None
    };
    let gen_cfg = crate::jpeg::EncoderCfg::with_quality(
        if compact { COMPACT_JPEG_V1_QUALITY } else { GEN_QUALITY },
        if compact {
            crate::plan::COMPACT_JPEG_V1_SAMPLING
        } else {
            GEN_SAMPLING
        },
    );

    let mut warnings: Vec<String> = Vec::new();
    if !doc.generated.is_empty() {
        warnings.push(format!("{WARN_GTIFF_LEVELS_GENERATED}:{}", doc.generated.len()));
    }
    // SVS 同款条件式：ICC 实际带入输出 level 0 时不报（SCN/MRXS 无条件是
    // 因为它们没有 ICC 概念——本适配器有）
    if doc.icc.is_none() {
        warnings.push(WARN_NO_ICC.to_string());
    }

    // composition working set: the padded canvas ((2·tw)×(2·th) — tiles may
    // be non-square) + the out tile + one decoded previous tile — charged
    // ONCE before the first composition
    let tile = (levels[0].tile_w, levels[0].tile_h);
    let side_x = (tile.0 as u64) * 2;
    let side_y = (tile.1 as u64) * 2;
    let compose_bytes = side_x
        .saturating_mul(side_y)
        .saturating_mul(3)
        .saturating_add((tile.0 as u64) * (tile.1 as u64) * 3 * 2);
    if !doc.generated.is_empty() {
        budget.charge(compose_bytes, "l0-box2 合成画布与输出缓冲")?;
    }

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => GtiffWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: gtiff_description_bytes(&doc),
        },
        OutputProfile::OmeBigTiffRgbSubifd => GtiffWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(gtiff_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, GtiffWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };

    let mut level_stats: Vec<LevelStats> = Vec::new();
    let mut edge_regions: Vec<EdgeRegion> = Vec::new();
    let mut ifd_chain: Vec<(u32, Option<usize>)> = Vec::new();
    // composition scratch is only ever touched when a generated tail exists
    // (charged above first — a pathological tile geometry refuses before
    // this allocation, never OOMs after it)
    let mut compose_scratch: Option<(Vec<u8>, Vec<u8>)> = if doc.generated.is_empty() {
        None
    } else {
        Some((
            vec![0u8; compose_bytes as usize],
            vec![0u8; compose_bytes as usize],
        ))
    };

    for (li, out) in out_levels.iter().enumerate() {
        job.check()?;
        let meta = level_meta(&doc, *out, compact);
        let (width, height, across, down, total) = match out {
            OutLevel::Source(i) => {
                let lv = &levels[*i];
                (lv.width, lv.height, lv.tiles_across, lv.tiles_down, lv.tiles_total)
            }
            OutLevel::Generated(i) => {
                let (w, h) = doc.generated[*i];
                let a = (w as u64).div_ceil(tile.0 as u64);
                let d = (h as u64).div_ceil(tile.1 as u64);
                (w, h, a as u32, d as u32, a * d)
            }
        };
        let mut stats = LevelStats {
            level: li as u32,
            width,
            height,
            tiles_across: across,
            tiles_down: down,
            tiles_total: total,
            ..Default::default()
        };

        let resume_done = resume.is_some_and(|r| li < r.level);
        let resume_current = resume.is_some_and(|r| li == r.level);

        match out {
            OutLevel::Source(i) => {
                let lv = &levels[*i];
                let ifd = &chain[lv.ifd_index as usize];
                if resume_done {
                    let committed = resume.unwrap().ifd_tiles[li];
                    if committed != lv.tiles_total {
                        return Err(CoreError::validation(format!(
                            "resume: 层 {li} 已提交 {committed} ≠ 总 tile 数 {}（journal 与输入不符）",
                            lv.tiles_total
                        )));
                    }
                    writer.begin(scratch, &meta, Some(committed))?;
                    reconstruct_cells_source(
                        src, &hdr, ifd, lv, u64::MAX, compact, &mut stats, &mut edge_regions,
                        &mut warnings,
                    )?;
                } else {
                    let skip_until = if resume_current { resume.unwrap().cell } else { 0 };
                    if resume_current && skip_until > 0 {
                        let committed = resume.unwrap().ifd_tiles[li];
                        writer.begin(scratch, &meta, Some(committed))?;
                        reconstruct_cells_source(
                            src, &hdr, ifd, lv, skip_until, compact, &mut stats,
                            &mut edge_regions, &mut warnings,
                        )?;
                    } else {
                        writer.begin(scratch, &meta, None)?;
                    }
                    let mut cur = tiff_read::TileCursor::new(src, &hdr, ifd)?;
                    if resume_current && skip_until > 0 {
                        cur.seek(skip_until)?;
                    }
                    let mut row_done: u64 = skip_until / lv.tiles_across as u64;
                    let mut cell = skip_until;
                    while let Some((off, len)) = cur.next_pair()? {
                        job.check()?;
                        let row = cell / lv.tiles_across as u64;
                        let col = cell % lv.tiles_across as u64;
                        let payload = src.read_at(off, len as usize)?;
                        let probe = check_tile(&payload, lv)?;
                        let interior_row = row + 1 < lv.tiles_down as u64;
                        let interior_col = col + 1 < lv.tiles_across as u64;
                        if (interior_row && interior_col)
                            && (probe.width != lv.tile_w || probe.height != lv.tile_h)
                        {
                            return Err(CoreError::variant(format!(
                                "层 ifd={} 内部 tile({row},{col}) 尺寸 {}×{} ≠ 标称 {}×{}（非边界不允许残缺）",
                                lv.ifd_index, probe.width, probe.height, lv.tile_w, lv.tile_h
                            )));
                        }
                        if probe.width != lv.tile_w || probe.height != lv.tile_h {
                            record_edge(
                                lv, row, col, probe.width, probe.height, compact,
                                &mut edge_regions, &mut warnings,
                            );
                        }
                        let data: Vec<u8>;
                        if let Some(cfg) = &compact_cfg {
                            data = compact_reencode_tile(&payload, lv, &probe, cfg)?;
                            stats.tiles_reencoded += 1;
                        } else {
                            data = payload;
                            stats.tiles_raw_copied += 1;
                        }
                        writer.write_tile(&data)?;
                        cell += 1;
                        if row + 1 > row_done {
                            row_done = row + 1;
                            job.progress.on_progress(&Progress {
                                unit: ProgressUnit::TileRow,
                                level: li as u32,
                                channel: None,
                                done: row_done,
                                total: lv.tiles_down as u64,
                                committed_bytes: writer.cursor(),
                            });
                            if job.checkpoint_enabled() {
                                job.emit_checkpoint(
                                    li as u32,
                                    None,
                                    cell,
                                    writer.cursor(),
                                    writer.ifd_tile_counts(),
                                );
                            }
                        }
                    }
                    if cell != lv.tiles_total {
                        return Err(CoreError::validation(format!(
                            "层 ifd={} tile 游标 {cell} ≠ 网格总数 {}",
                            lv.ifd_index, lv.tiles_total
                        )));
                    }
                }
            }
            OutLevel::Generated(_) => {
                // the l0-box2 tail: every tile is the box downsample of the
                // PREVIOUS OUTPUT level's committed tiles (deterministic on
                // a resume too — the previous level is fully committed)
                let (pw, ph) = match out_levels[li - 1] {
                    OutLevel::Source(i) => (levels[i].width, levels[i].height),
                    OutLevel::Generated(i) => doc.generated[i],
                };
                let prev_across = pw.div_ceil(tile.0) as u32;
                let prev_down = ph.div_ceil(tile.1) as u32;
                let skip_until = if resume_current { resume.unwrap().cell } else { 0 };
                if resume_done {
                    // fully committed: reopen, count, never rewrite
                    let committed = resume.unwrap().ifd_tiles[li];
                    if committed != total {
                        return Err(CoreError::validation(format!(
                            "resume: 生成层 {li} 已提交 {committed} ≠ 总 tile 数 {total}"
                        )));
                    }
                    writer.begin(scratch, &meta, Some(committed))?;
                    stats.tiles_reencoded = total;
                } else {
                    if resume_current && skip_until > 0 {
                        let committed = resume.unwrap().ifd_tiles[li];
                        writer.begin(scratch, &meta, Some(committed))?;
                    } else {
                        writer.begin(scratch, &meta, None)?;
                    }
                    let (canvas_buf, out_buf) =
                        compose_scratch.as_mut().expect("generated tail implies compose scratch");
                    // the previous level's shared JPEGTables (when the source
                    // level stores abbreviated tiles) ride into every decode
                    let prev_tables = level_meta(&doc, out_levels[li - 1], compact)
                        .jpeg_tables;
                    // committed cells count like the reconstructed path does
                    stats.tiles_reencoded = skip_until;
                    let mut row_done: u64 = skip_until / across as u64;
                    let mut cell = skip_until;
                    while cell < total {
                        job.check()?;
                        generated_tile_from_prev(
                            &writer,
                            li - 1,
                            prev_across,
                            prev_down,
                            width,
                            height,
                            across as u64,
                            tile.0,
                            tile.1,
                            cell,
                            out_buf,
                            canvas_buf,
                            prev_tables.as_deref(),
                        )?;
                        let enc = crate::jpeg::encode_rgb(
                            &out_buf[..(tile.0 as usize) * (tile.1 as usize) * 3],
                            tile.0,
                            tile.1,
                            &gen_cfg,
                        )?;
                        writer.write_tile(&enc)?;
                        stats.tiles_reencoded += 1;
                        cell += 1;
                        let row = (cell - 1) / across as u64;
                        if row + 1 > row_done {
                            row_done = row + 1;
                            job.progress.on_progress(&Progress {
                                unit: ProgressUnit::TileRow,
                                level: li as u32,
                                channel: None,
                                done: row_done,
                                total: down as u64,
                                committed_bytes: writer.cursor(),
                            });
                            if job.checkpoint_enabled() {
                                job.emit_checkpoint(
                                    li as u32,
                                    None,
                                    cell,
                                    writer.cursor(),
                                    writer.ifd_tile_counts(),
                                );
                            }
                        }
                    }
                }
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
            GtiffWriter::Classic { description, .. } => description.clone(),
            GtiffWriter::Ome { .. } => Vec::new(),
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
    if !doc.generated.is_empty() {
        budget.release(compose_bytes);
    }
    let compact_tiles_reencoded: u64 =
        if compact { level_stats.iter().map(|l| l.tiles_reencoded).sum() } else { 0 };
    let compact_tiles_padded: u64 = if compact { edge_regions.len() as u64 } else { 0 };

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
        edge_regions,
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
            tiles_reencoded: compact_tiles_reencoded,
            tiles_padded: compact_tiles_padded,
        }),
        composed: None,
        associated: Vec::new(),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    result.validation.tile_records_emitted =
        result.levels.iter().map(|l| l.tiles_total).sum();
    Ok(result)
}

/// Convenience wrapper without progress (tests/small uses).
pub fn convert_gtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_gtiff_to_bigtiff(src, sink, scratch, plan, &job)
}
