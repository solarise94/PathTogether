//! SCN → brightfield conversion (F4): Leica JPEG tiles → classic multi-IFD
//! JPEG BigTIFF pyramid or RGB OME-BigTIFF (SubIFD pyramid).
//!
//! Payload policy is the SVS **pure passthrough**: every PRESENT source tile
//! is copied verbatim, in tile order, level by level (SCN tiles are
//! self-contained JPEG streams; an optional shared `JPEGTables` tag 347 is
//! carried verbatim like the SVS adapter). MISSING tiles — the SCN400
//! sparse-grid quirk, `(0,0)` TileOffsets entries — are filled with ONE
//! generated white tile per level (quality 90, 4:4:4), written once and
//! referenced by every filled record; the fill is counted in
//! `tiles_filled` and warned once (`scn_missing_tiles_filled`).
//!
//! `compact-jpeg-v1` (U3, merged): identical to the SVS compact path —
//! every present tile decoded (adapter colourspace rule) and re-encoded at
//! the LOCKED compact parameters; missing tiles stay the generated fill.
//!
//! Edge policy (preserve): SCN400 keeps full-size tiles past the image
//! boundary; boundary tiles smaller than the nominal rect are copied
//! verbatim and recorded in `edge_regions`, interior non-nominal tiles are
//! a typed refusal — same rules as SVS.
//!
//! The label/macro/preview images of the collection are NOT exported
//! (`scn_associated_not_exported` warning). Resume mirrors
//! [`crate::convert_svs`]: checkpoints per committed tile row; the resumed
//! output is byte-identical (fill refs reconstructed deterministically).

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::budget::MemBudget;
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::convert_svs::{FORMAT_CLASSIC, FORMAT_OME_RGB};
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::ome::{py_g17, py_repr_f64};
use crate::ome_writer::{OmeBigTiffWriter, RgbIfdExtras};
use crate::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan, COMPACT_JPEG_V1_FINGERPRINT,
    COMPACT_JPEG_V1_HUFFMAN, COMPACT_JPEG_V1_QUALITY,
};
use crate::report::{EdgeRegion, LevelStats, LossyReencode, TransformResult};
use crate::resume::ResumePoint;
use crate::scn::{self, PayloadColor, ScnDoc, ScnLevel, ADAPTER_VERSION, SOURCE_FORMAT};
use crate::tiff_read::{self, TiffHeader};

/// Warning: the SCN400 sparse grid had missing (0,0) tiles — filled.
pub const WARN_SCN_MISSING_TILES_FILLED: &str = "scn_missing_tiles_filled";
/// Warning: label/macro/preview images detected but not exported.
pub const WARN_SCN_ASSOC_NOT_EXPORTED: &str = "scn_associated_not_exported";
/// Warning: cropped source tiles passed through verbatim.
pub const WARN_SCN_EDGE_PASSTHROUGH: &str = "scn_cropped_edge_tile_passthrough";
/// Warning: no ICC profile present — colour management is not applied.
pub const WARN_NO_ICC: &str = "color_management_not_applied";
/// The generated fill tile's parameters (deterministic, documented).
pub const FILL_QUALITY: u8 = 90;

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
enum ScnWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl ScnWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed_tiles: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            ScnWriter::Classic { w, .. } => match committed_tiles {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            ScnWriter::Ome { w, ome_xml, levels } => {
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
            ScnWriter::Classic { w, .. } => w.end_level_ex(
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
            ScnWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        match self {
            ScnWriter::Classic { w, .. } => w.write_tile(data),
            ScnWriter::Ome { w, .. } => w.write_tile(data),
        }
    }

    fn write_tile_ref(&mut self, offset: u64, count: u32) -> CoreResult<()> {
        match self {
            ScnWriter::Classic { w, .. } => w.write_tile_ref(offset, count),
            ScnWriter::Ome { w, .. } => w.write_tile_ref(offset, count),
        }
    }

    fn write_payload(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        match self {
            ScnWriter::Classic { w, .. } => w.write_payload(data),
            ScnWriter::Ome { w, .. } => w.write_payload(data),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            ScnWriter::Classic { w, .. } => w.cursor(),
            ScnWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            ScnWriter::Classic { w, .. } => w.ifd_tile_counts(),
            ScnWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            ScnWriter::Classic { w, .. } => w.finish(),
            ScnWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

/// JSON description of the classic profile (same `json.dumps(sort_keys=True)
/// + NUL` convention as the SVS path; unknown values are `null`, never
/// invented).
fn scn_description_bytes(doc: &ScnDoc) -> Vec<u8> {
    let mpp = doc.mpp;
    let obj = doc.objective;
    let s = format!(
        "{{\"adapter\": \"{}\", \"adapter_version\": \"{}\", \"mpp_x\": {}, \"mpp_y\": {}, \"objective\": {}, \"source_format\": \"{}\"}}\u{0}",
        SOURCE_FORMAT,
        ADAPTER_VERSION,
        mpp.map(py_repr_f64).unwrap_or_else(|| "null".into()),
        mpp.map(py_repr_f64).unwrap_or_else(|| "null".into()),
        obj.map(py_repr_f64).unwrap_or_else(|| "null".into()),
        SOURCE_FORMAT,
    );
    s.into_bytes()
}

/// OME-XML (2016-06) of the SCN-derived RGB profile — same shape as the SVS
/// one, with SCN provenance.
fn scn_ome_xml(doc: &ScnDoc, plan: &TransformPlan) -> Vec<u8> {
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
        if doc.mpp.is_some() { "scn-view-nanometers" } else { "unknown" }.to_string(),
    ));
    provenance.push((
        "objective_source",
        if doc.objective.is_some() { "scn-scanSettings-objective" } else { "unknown" }.to_string(),
    ));
    provenance.push(("pyramid_levels", doc.levels.len().to_string()));
    if let Some(illu) = &doc.illumination {
        provenance.push(("illumination_source", illu.clone()));
    }
    if plan.encoding == EncodingProfile::CompactJpegV1 {
        provenance.push(("encoding_profile", plan.encoding.id().to_string()));
        provenance.push(("encoding_params_fingerprint", COMPACT_JPEG_V1_FINGERPRINT.to_string()));
    }
    provenance.push((
        "tile_payloads",
        if plan.encoding == EncodingProfile::CompactJpegV1 {
            format!(
                "every present tile decoded (shared JPEGTables merged, adapter colourspace \
                 rule) and re-encoded at the locked compact parameters (quality {}, subsampling \
                 {}, standard Annex-K Huffman, fingerprint {}); missing sparse-grid tiles stay \
                 generated fill; lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            "Leica JPEG tiles copied byte-for-byte; sparse-grid missing tiles (0,0) filled with \
             one generated white tile per level (quality 90, 4:4:4) and referenced; no tile of \
             the source is re-encoded or relabelled"
                .to_string()
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
    let has_objective = doc.objective.is_some_and(|m| m.is_finite() && m > 0.0);
    let instrument = if has_objective {
        format!(
            "<Instrument ID=\"Instrument:0\"><Objective ID=\"Objective:0:0\" NominalMagnification=\"{}\"/></Instrument>",
            py_g17(doc.objective.unwrap_or(0.0))
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
{instrument}<Image ID=\"Image:0\">{refs}\
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

/// Per-level output metadata (SVS semantics; calibration from the real
/// dimension ratio).
fn level_meta(doc: &ScnDoc, li: usize, compact: bool) -> LevelMeta {
    let lv = &doc.levels[li];
    let main = &doc.levels[0];
    let mpp = doc.mpp.map(|m| {
        let rx = main.width as f64 / lv.width as f64;
        let ry = main.height as f64 / lv.height as f64;
        (m * rx, m * ry)
    });
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
        width: lv.width,
        height: lv.height,
        tile: (lv.tile_w, lv.tile_h),
        photometric,
        tiff_sub,
        reduced: li > 0,
        mpp,
        jpeg_tables,
        icc: None, // SCN files carry no ICC; nothing is invented
    }
}

/// Verify one present tile's JPEG header against the level contract.
fn check_tile(payload: &[u8], lv: &ScnLevel) -> CoreResult<crate::jpeg::JpegProbe> {
    let probe = crate::jpeg::scan_jpeg(payload)?;
    if probe.sampling != Some(lv.sampling) {
        return Err(CoreError::validation(format!(
            "层 r={} tile 与探测到的 JPEG 契约不一致（采样不齐）",
            lv.r
        )));
    }
    if probe.width as u64 > lv.tile_w as u64 || probe.height as u64 > lv.tile_h as u64 {
        return Err(CoreError::variant(format!(
            "层 r={} tile 尺寸 {}×{} 超过标称 {}×{}",
            lv.r, probe.width, probe.height, lv.tile_w, lv.tile_h
        )));
    }
    Ok(probe)
}

/// Compact-jpeg-v1 re-encode of one SCN source tile (U3 merged path; the
/// SCN tiles are self-contained streams, an optional shared JPEGTables is
/// merged like the SVS path). A cropped tile is pasted onto a white
/// tile-sized canvas.
fn compact_reencode_tile(
    payload: &[u8],
    lv: &ScnLevel,
    probe: &crate::jpeg::JpegProbe,
    cfg: &crate::jpeg::EncoderCfg,
) -> CoreResult<Vec<u8>> {
    let merged;
    let stream: &[u8] = match &lv.jpeg_tables {
        Some(t) => {
            merged = crate::svs::merge_tables_then_tile(t, payload);
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

/// The generated fill tile: one deterministic white JPEG per level tile
/// geometry (quality 90, 4:4:4, standard tables).
fn fill_tile(tile_w: u32, tile_h: u32) -> CoreResult<Vec<u8>> {
    let px = (tile_w as usize) * (tile_h as usize) * 3;
    let white = vec![255u8; px];
    crate::jpeg::encode_rgb(
        &white,
        tile_w,
        tile_h,
        &crate::jpeg::EncoderCfg::with_quality(FILL_QUALITY, crate::jpeg::Sampling::S444),
    )
}

fn record_edge(
    lv: &ScnLevel,
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
            level: lv.r,
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
    if !warnings.iter().any(|w| w == WARN_SCN_EDGE_PASSTHROUGH) {
        warnings.push(WARN_SCN_EDGE_PASSTHROUGH.to_string());
    }
    edge_regions.push(EdgeRegion {
        level: lv.r,
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

/// Re-scan the boundary cells / fill counts of a level for resume
/// bookkeeping (no decode). Present interior cells are nominal by the
/// probe's accept rules; (0,0) cells are the reconstructed fills.
#[allow(clippy::too_many_arguments)]
fn reconstruct_cells_scn(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &tiff_read::Ifd,
    lv: &ScnLevel,
    up_to: u64,
    compact: bool,
    stats: &mut LevelStats,
    edge_regions: &mut Vec<EdgeRegion>,
    warnings: &mut Vec<String>,
) -> CoreResult<()> {
    let mut cur = tiff_read::TileCursor::new(src, hdr, ifd)?;
    let mut cell = 0u64;
    while cell < up_to {
        let Some((off, len)) = cur.next_pair_allow_zero()? else { break };
        let row = cell / lv.tiles_across as u64;
        let col = cell % lv.tiles_across as u64;
        if off == 0 && len == 0 {
            stats.tiles_filled += 1;
        } else {
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
        }
        cell += 1;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn convert_scn_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None)
}

/// Resume an SCN conversion from `resume` (see [`crate::resume`]).
#[allow(clippy::too_many_arguments)]
pub fn convert_scn_to_bigtiff_resume(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
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
    let doc = scn::probe_scn_with_budget(src, plan.limits.memory_budget_bytes)?;
    let mut budget: MemBudget = doc.budget.clone();
    let hdr = tiff_read::read_header(src)?;
    let chain = tiff_read::ifd_chain(src, &hdr)?;
    let levels = &doc.levels;
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
    if plan.profile == OutputProfile::OmeBigTiffSubifd {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 SCN"));
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless
        && plan.encoding == EncodingProfile::CompactJpegV1
    {
        return Err(CoreError::policy(
            "compact-jpeg-v1 与 strict-lossless 互斥：逐 tile 重编码必然有损",
        ));
    }
    let compact = plan.encoding == EncodingProfile::CompactJpegV1;
    let compact_cfg = if compact {
        Some(crate::plan::compact_jpeg_v1_encoder_cfg())
    } else {
        None
    };

    let mut warnings: Vec<String> = Vec::new();
    let any_missing = levels.iter().any(|l| l.tiles_missing() > 0);
    if any_missing {
        warnings.push(WARN_SCN_MISSING_TILES_FILLED.to_string());
    }
    if !doc.associated.is_empty() {
        warnings.push(WARN_SCN_ASSOC_NOT_EXPORTED.to_string());
    }
    warnings.push(WARN_NO_ICC.to_string());

    // fill-tile working set: the encoded canvas + the encoded payload,
    // charged once before the first fill is generated
    let max_tile = levels
        .iter()
        .map(|l| (l.tile_w as u64) * (l.tile_h as u64) * 3)
        .max()
        .unwrap_or(0);
    budget.charge(max_tile * 2, "填充块画布与编码缓冲")?;

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => ScnWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: scn_description_bytes(&doc),
        },
        OutputProfile::OmeBigTiffRgbSubifd => ScnWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(scn_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, ScnWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };

    // ---- shared fill payloads (F4) -------------------------------------- //
    // One deterministic white tile per DISTINCT level tile geometry that
    // actually has missing (sparse-grid) cells, written ONCE right after the
    // 16-byte BigTIFF header; every filled cell's TileOffsets entry
    // references it (valid TIFF: entries may repeat). Levels without
    // missing tiles get no fill payload at all. On resume the payloads are
    // DERIVED at the same fixed offsets without rewriting (they are part of
    // the committed prefix) — the same scheme that keeps the MRXS adapter's
    // resume byte-identical.
    let mut fill_geoms: Vec<(u32, u32)> = Vec::new();
    for lv in levels {
        if lv.tiles_missing() > 0 && !fill_geoms.contains(&(lv.tile_w, lv.tile_h)) {
            fill_geoms.push((lv.tile_w, lv.tile_h));
        }
    }
    let mut fill_refs: Vec<(u64, u32)> = Vec::with_capacity(fill_geoms.len());
    {
        let mut at = 16u64; // both writers place the header in [0, 16)
        for (tw, th) in &fill_geoms {
            let payload = fill_tile(*tw, *th)?;
            if resume.is_some() {
                // derive without writing (bytes already committed)
                fill_refs.push((at, payload.len() as u32));
            } else {
                let r = writer.write_payload(&payload)?;
                debug_assert_eq!(r.0, at, "fill payload must sit at the fixed offset");
                fill_refs.push(r);
            }
            at += payload.len() as u64;
        }
    }
    let fill_ref_of = |lv: &ScnLevel| -> CoreResult<(u64, u32)> {
        let idx = fill_geoms
            .iter()
            .position(|g| *g == (lv.tile_w, lv.tile_h))
            .ok_or_else(|| CoreError::validation("层无缺失 tile 却请求填充引用"))?;
        Ok(fill_refs[idx])
    };
    let mut level_stats: Vec<LevelStats> = Vec::new();
    let mut edge_regions: Vec<EdgeRegion> = Vec::new();
    let mut ifd_chain: Vec<(u32, Option<usize>)> = Vec::new();

    for (li, lv) in levels.iter().enumerate() {
        job.check()?;
        let ifd = &chain[lv.ifd_index as usize];
        let meta = level_meta(&doc, li, compact);
        let mut stats = LevelStats {
            level: li as u32,
            width: lv.width,
            height: lv.height,
            tiles_across: lv.tiles_across,
            tiles_down: lv.tiles_down,
            tiles_total: lv.tiles_total,
            ..Default::default()
        };

        let resume_done = resume.is_some_and(|r| li < r.level);
        let resume_current = resume.is_some_and(|r| li == r.level);
        if resume_done {
            let committed = resume.unwrap().ifd_tiles[li];
            if committed != lv.tiles_total {
                return Err(CoreError::validation(format!(
                    "resume: 层 {} 已提交 {} ≠ 总 tile 数 {}（journal 与输入不符）",
                    li, committed, lv.tiles_total
                )));
            }
            writer.begin(scratch, &meta, Some(committed))?;
            reconstruct_cells_scn(
                src, &hdr, ifd, lv, u64::MAX, compact, &mut stats, &mut edge_regions,
                &mut warnings,
            )?;
            writer.end(&meta, &[])?;
            ifd_chain.push((li as u32, None));
            level_stats.push(stats);
            continue;
        }

        let skip_until = if resume_current { resume.unwrap().cell } else { 0 };
        if resume_current && skip_until > 0 {
            let committed = resume.unwrap().ifd_tiles[li];
            writer.begin(scratch, &meta, Some(committed))?;
            reconstruct_cells_scn(
                src, &hdr, ifd, lv, skip_until, compact, &mut stats, &mut edge_regions,
                &mut warnings,
            )?;
        } else {
            writer.begin(scratch, &meta, None)?;
        }

        let mut cur = tiff_read::TileCursor::new(src, &hdr, ifd)?;
        if resume_current && skip_until > 0 {
            cur.seek(skip_until)?;
        }
        let mut row_done: u64 = skip_until / lv.tiles_across as u64;
        let mut cell: u64 = skip_until;
        while let Some((off, len)) = cur.next_pair_allow_zero()? {
            job.check()?;
            let row = cell / lv.tiles_across as u64;
            let col = cell % lv.tiles_across as u64;
            if off == 0 && len == 0 {
                // missing sparse-grid tile → the shared generated fill
                stats.tiles_filled += 1;
                let (fo, fc) = fill_ref_of(lv)?;
                writer.write_tile_ref(fo, fc)?;
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
                continue;
            }
            let payload = src.read_at(off, len as usize)?;
            let probe = check_tile(&payload, lv)?;
            let interior_row = row + 1 < lv.tiles_down as u64;
            let interior_col = col + 1 < lv.tiles_across as u64;
            if (interior_row && interior_col)
                && (probe.width != lv.tile_w || probe.height != lv.tile_h)
            {
                return Err(CoreError::variant(format!(
                    "层 r={} 内部 tile({row},{col}) 尺寸 {}×{} ≠ 标称 {}×{}（非边界不允许残缺）",
                    lv.r, probe.width, probe.height, lv.tile_w, lv.tile_h
                )));
            }
            if probe.width != lv.tile_w || probe.height != lv.tile_h {
                record_edge(
                    lv, row, col, probe.width, probe.height, compact, &mut edge_regions,
                    &mut warnings,
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
                "层 r={} tile 游标 {} ≠ 网格总数 {}",
                lv.r, cell, lv.tiles_total
            )));
        }
        if writer.cursor() > plan.limits.max_output_bytes {
            return Err(CoreError::too_large(format!(
                "输出已写 {} > {}",
                writer.cursor(),
                plan.limits.max_output_bytes
            )));
        }
        let desc: Vec<u8> = match &writer {
            ScnWriter::Classic { description, .. } => description.clone(),
            ScnWriter::Ome { .. } => Vec::new(),
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
    }
    let output_bytes = writer.finish()?;
    budget.release(max_tile * 2);
    let compact_tiles_reencoded: u64 = level_stats.iter().map(|l| l.tiles_reencoded).sum();
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
        associated: doc.associated.clone(),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    result.validation.tile_records_emitted =
        result.levels.iter().map(|l| l.tiles_total).sum();
    Ok(result)
}

/// Convenience wrapper without progress (tests/small uses).
pub fn convert_scn(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_scn_to_bigtiff(src, sink, scratch, plan, &job)
}
