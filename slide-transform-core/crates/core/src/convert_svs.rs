//! SVS → brightfield conversion (F1): Aperio JPEG tiles → classic multi-IFD
//! JPEG BigTIFF pyramid or RGB OME-BigTIFF (SubIFD pyramid).
//!
//! Default payload policy is **pure passthrough**: every source tile byte is
//! copied verbatim, in tile order, level by level. The shared `JPEGTables` of
//! each level are written verbatim into the output IFD (tag 347), so
//! abbreviated source streams stay abbreviated in the output — no splicing
//! into tiles, no re-encode, nothing relabelled. The output photometric is
//! the JPEG payloads' true colorspace (2 = RGB for Aperio `JPEG/RGB`
//! streams, 6 = YCbCr with the SOF subsampling), and the tile tags (322/323)
//! carry the source's own tile shape (e.g. 240).
//!
//! `compact-jpeg-v1` (U3, merged): every source tile of every level is
//! decoded with the adapter's colourspace rule (shared `JPEGTables` merged
//! in, [`crate::jpeg::decode_ex`] `force_rgb` for photometric-RGB payloads)
//! and re-encoded with the LOCKED [`crate::plan::compact_jpeg_v1_encoder_cfg`]
//! parameters at the source tile geometry (cropped tiles pasted onto a white
//! tile-sized canvas, like the KFB compact path). The output advertises
//! photometric 6 + the locked YCbCrSubsampling, writes NO tag 347 (each tile
//! is self-contained) and keeps the source tile size; the provenance and the
//! report carry the `lossy_reencode` summary exactly like the KFB path.
//! Full source resolution and coordinates are kept; the output is lossy by
//! construction and is never claimed lossless.
//!
//! Edge policy (preserve): Aperio keeps full-size tiles past the image
//! boundary; such tiles are copied as-is (standard TIFF edge-tile semantics —
//! content past the image edge is unspecified and invisible). A genuinely
//! cropped tile (JPEG smaller than the nominal tile rect) is also copied
//! verbatim and recorded in `edge_regions`; because no pixel is ever
//! re-encoded, `StrictLossless` accepts everything the default policy
//! accepts (there is no lossy step in this adapter to forbid). Under
//! `compact` every tile is re-encoded anyway, so the two policies are
//! mutually exclusive and the combination is a typed refusal.
//!
//! Label/macro/thumbnail are NOT exported — this is a main-image
//! conversion, not an archive of the source file (`aperio_associated_not_
//! exported` warning).
//!
//! Resume mirrors [`crate::convert_bf`]: checkpoints are emitted per
//! committed tile row; [`convert_svs_to_bigtiff_resume`] fast-forwards the
//! committed cells of the current level (boundary tiles re-scanned, not
//! re-decoded) and the fresh path stays byte-identical.

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::ome::{py_g17, py_repr_f64};
use crate::ome_writer::{OmeBigTiffWriter, RgbIfdExtras};
use crate::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan, COMPACT_JPEG_V1_FINGERPRINT,
    COMPACT_JPEG_V1_HUFFMAN, COMPACT_JPEG_V1_QUALITY,
};
use crate::report::{AssociatedSummary, EdgeRegion, LevelStats, LossyReencode, TransformResult};
use crate::resume::ResumePoint;
use crate::svs::{self, PayloadColor, SvsDoc, SvsLevel, ADAPTER_VERSION, SOURCE_FORMAT};
use crate::tiff_read::{self, TiffHeader};

/// Reuse the brightfield output format ids — the outputs ARE the same two
/// profiles; only the input adapter differs.
pub use crate::convert_bf::{FORMAT_CLASSIC, FORMAT_OME_RGB};

/// Warning: cropped source tiles passed through verbatim.
pub const WARN_SVS_EDGE_PASSTHROUGH: &str = "svs_cropped_edge_tile_passthrough";
/// Warning: label/macro/thumbnail detected but not exported.
pub const WARN_ASSOC_NOT_EXPORTED: &str = "aperio_associated_not_exported";
/// Warning: no ICC profile present — colour management is not applied.
pub const WARN_NO_ICC: &str = "color_management_not_applied";

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

/// The two brightfield layouts share the tile stream (see `convert_bf`).
enum SvsWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl SvsWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed_tiles: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            SvsWriter::Classic { w, .. } => match committed_tiles {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            SvsWriter::Ome { w, ome_xml, levels } => {
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
            SvsWriter::Classic { w, .. } => w.end_level_ex(
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
            SvsWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<()> {
        match self {
            SvsWriter::Classic { w, .. } => w.write_tile(data).map(|_| ()),
            SvsWriter::Ome { w, .. } => w.write_tile(data).map(|_| ()),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            SvsWriter::Classic { w, .. } => w.cursor(),
            SvsWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            SvsWriter::Classic { w, .. } => w.ifd_tile_counts(),
            SvsWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            SvsWriter::Classic { w, .. } => w.finish(),
            SvsWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

/// JSON description of the classic profile (same `json.dumps(sort_keys=True)
/// + NUL` convention as the KFB path; unknown values are `null`, never
/// invented).
fn svs_description_bytes(doc: &SvsDoc) -> Vec<u8> {
    let mpp = doc.mpp;
    let obj = doc.appmag;
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

/// OME-XML (2016-06) of the SVS-derived RGB profile: same shape as the KFB
/// `bf-ome` XML, with PhysicalSize only when the Aperio description carries
/// an MPP, NominalMagnification only for a real `AppMag`, and the adapter
/// provenance (source format + adapter version + payload policy).
fn svs_ome_xml(doc: &SvsDoc, plan: &TransformPlan) -> Vec<u8> {
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
        if doc.mpp.is_some() { "aperio-description-MPP" } else { "unknown" }.to_string(),
    ));
    provenance.push((
        "objective_source",
        if doc.appmag.is_some() { "aperio-description-AppMag" } else { "unknown" }
            .to_string(),
    ));
    provenance.push(("pyramid_levels", doc.levels.len().to_string()));
    // Encoding keys appear ONLY for compact runs: preserve outputs keep their
    // exact pre-U3 OME-XML (byte-parity gate on pinned sha256 values); a
    // missing key means preserve-source-v1, exactly like every pre-U3 file.
    if plan.encoding == EncodingProfile::CompactJpegV1 {
        provenance.push(("encoding_profile", plan.encoding.id().to_string()));
        provenance.push(("encoding_params_fingerprint", COMPACT_JPEG_V1_FINGERPRINT.to_string()));
    }
    provenance.push((
        "tile_payloads",
        if plan.encoding == EncodingProfile::CompactJpegV1 {
            format!(
                "every tile decoded (shared JPEGTables merged, adapter colourspace rule) and \
                 re-encoded at the locked compact parameters (quality {}, subsampling {}, \
                 standard Annex-K Huffman, fingerprint {}); source tile size and coordinates \
                 kept; no shared JPEGTables written; lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            "Aperio JPEG tiles copied byte-for-byte; shared JPEGTables written verbatim \
             into each level IFD (tag 347); no tile is re-encoded or relabelled"
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
    let has_objective = doc.appmag.is_some_and(|m| m.is_finite() && m > 0.0);
    let instrument = if has_objective {
        format!(
            "<Instrument ID=\"Instrument:0\"><Objective ID=\"Objective:0:0\" NominalMagnification=\"{}\"/></Instrument>",
            py_g17(doc.appmag.unwrap_or(0.0))
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
        Some(mpp) if mpp.is_finite() && mpp > 0.0 => (
            format!(
                " PhysicalSizeX=\"{}\" PhysicalSizeXUnit=\"µm\" PhysicalSizeY=\"{}\" PhysicalSizeYUnit=\"µm\"",
                py_repr_f64(mpp),
                py_repr_f64(mpp)
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

/// Per-level output metadata (tile shape, photometric, calibration,
/// JPEGTables; ICC on the main level only). Under `compact` the output
/// payload is always the locked YCbCr re-encode: photometric 6, the locked
/// subsampling, NO shared JPEGTables (each tile is self-contained); the
/// source tile geometry is kept.
fn level_meta(doc: &SvsDoc, li: usize, compact: bool) -> LevelMeta {
    let lv = &doc.levels[li];
    let main = &doc.levels[0];
    // per-level calibration from the real dimension ratio (the source
    // carries no per-level resolution tags)
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
        icc: if li == 0 { doc.icc.clone() } else { None },
    }
}

/// Verify one tile's JPEG header against the level contract (dims within
/// the nominal rect; sampling/markers consistent with the probed truth).
fn check_tile(
    payload: &[u8],
    lv: &SvsLevel,
    head: &crate::jpeg::JpegProbe,
) -> CoreResult<crate::jpeg::JpegProbe> {
    let probe = crate::jpeg::scan_jpeg(payload)?;
    if probe.sampling != Some(lv.sampling)
        || probe.jfif != head.jfif
        || probe.adobe_transform != head.adobe_transform
    {
        return Err(CoreError::validation(format!(
            "层 {} tile 与探测到的 JPEG 契约不一致（采样/标记不齐）",
            li_of(lv)
        )));
    }
    if probe.width as u64 > lv.tile_w as u64 || probe.height as u64 > lv.tile_h as u64 {
        return Err(CoreError::variant(format!(
            "层 {} tile 尺寸 {}×{} 超过标称 {}×{}",
            li_of(lv),
            probe.width,
            probe.height,
            lv.tile_w,
            lv.tile_h
        )));
    }
    Ok(probe)
}

fn li_of(lv: &SvsLevel) -> u32 {
    lv.ifd_index
}

/// Compact-jpeg-v1 re-encode of one SVS source tile (U3 merged path).
///
/// The abbreviated tile stream (shared `JPEGTables`) is merged into a
/// self-contained JPEG, decoded with the adapter's colourspace rule —
/// `force_rgb` exactly when the level's true payload colorspace is RGB (the
/// Aperio photometric-2 convention, see [`crate::jpeg::tiff_jpeg_color`]) —
/// and re-encoded with the LOCKED compact configuration at the SOURCE tile
/// geometry. A cropped tile is pasted onto a white tile-sized canvas
/// (`compact` padding semantics of the KFB path); the boolean reports the
/// padding so the caller can record the edge region.
fn compact_reencode_tile(
    payload: &[u8],
    lv: &SvsLevel,
    probe: &crate::jpeg::JpegProbe,
    cfg: &crate::jpeg::EncoderCfg,
) -> CoreResult<(Vec<u8>, bool)> {
    let merged;
    let stream: &[u8] = match &lv.jpeg_tables {
        Some(t) => {
            merged = svs::merge_tables_then_tile(t, payload);
            &merged
        }
        None => payload,
    };
    // the level contract (check_tile) guarantees the sampling matches the
    // probe, so the per-level colorspace decision applies to every tile
    let force_rgb = lv.color == PayloadColor::Rgb;
    let max_pixels = (lv.tile_w as u64) * (lv.tile_h as u64);
    let img = crate::jpeg::decode_ex(stream, max_pixels, force_rgb)?;
    if img.width != probe.width || img.height != probe.height {
        return Err(CoreError::jpeg(format!(
            "tile 解码尺寸 {}×{} 与探测 {}×{} 不符",
            img.width, img.height, probe.width, probe.height
        )));
    }
    let padded = img.width != lv.tile_w || img.height != lv.tile_h;
    if !padded {
        let jpg = crate::jpeg::encode_rgb(&img.data, img.width, img.height, cfg)?;
        return Ok((jpg, false));
    }
    // cropped source tile → white tile_w×tile_h canvas, row-strided paste
    let mut canvas = vec![255u8; (lv.tile_w as usize) * (lv.tile_h as usize) * 3];
    for y in 0..img.height as usize {
        let s = y * img.width as usize * 3;
        let d = y * lv.tile_w as usize * 3;
        canvas[d..d + img.width as usize * 3].copy_from_slice(&img.data[s..s + img.width as usize * 3]);
    }
    let jpg = crate::jpeg::encode_rgb(&canvas, lv.tile_w, lv.tile_h, cfg)?;
    Ok((jpg, true))
}

/// Re-scan the boundary cells of a level for resume bookkeeping (edge
/// regions / stats). Interior cells are nominal by the accept rules (the
/// fresh path rejects anything else before writing). Under `compact` every
/// scanned cell counts as re-encoded and cropped boundary tiles are the
/// padded edge regions (same reconstruction as the fresh compact path).
#[allow(clippy::too_many_arguments)]
fn reconstruct_cells_svs(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &tiff_read::Ifd,
    lv: &SvsLevel,
    head: &crate::jpeg::JpegProbe,
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
                record_edge(
                    lv, row, col, probe.width, probe.height, compact, edge_regions, warnings,
                );
            }
        }
        if compact {
            stats.tiles_reencoded += 1;
        } else {
            stats.tiles_raw_copied += 1;
        }
        cell += 1;
        let _ = head;
    }
    Ok(())
}

fn record_edge(
    lv: &SvsLevel,
    row: u64,
    col: u64,
    w: u32,
    h: u32,
    compact: bool,
    edge_regions: &mut Vec<EdgeRegion>,
    warnings: &mut Vec<String>,
) {
    if compact {
        // compact pads the cropped tile onto the white tile-sized canvas —
        // not a passthrough, so the passthrough warning must NOT appear
        edge_regions.push(EdgeRegion {
            level: lv.ifd_index,
            channel: None,
            x: (col * lv.tile_w as u64) as u32,
            y: (row * lv.tile_h as u64) as u32,
            source_w: w,
            source_h: h,
            canvas_w: lv.tile_w,
            canvas_h: lv.tile_h,
            reused_qtables: false, // compact never reuses source tables
        });
        return;
    }
    if !warnings.iter().any(|w| w == WARN_SVS_EDGE_PASSTHROUGH) {
        warnings.push(WARN_SVS_EDGE_PASSTHROUGH.to_string());
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

#[allow(clippy::too_many_arguments)]
pub fn convert_svs_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None)
}

/// Resume an SVS conversion from `resume` (see [`crate::resume`]).
#[allow(clippy::too_many_arguments)]
pub fn convert_svs_to_bigtiff_resume(
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
    let doc = svs::probe_svs(src)?;
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
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 SVS"));
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless
        && plan.encoding == EncodingProfile::CompactJpegV1
    {
        // compact re-encodes every tile by construction — a strict-lossless
        // request contradicts it. Typed refusal BEFORE any output byte
        // (same rule and message as the KFB path).
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
    if !doc.associated.is_empty() {
        warnings.push(WARN_ASSOC_NOT_EXPORTED.to_string());
    }
    if doc.icc.is_none() {
        warnings.push(WARN_NO_ICC.to_string());
    }

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => SvsWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: svs_description_bytes(&doc),
        },
        OutputProfile::OmeBigTiffRgbSubifd => SvsWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(svs_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, SvsWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };
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
        // per-level JPEG truth (cached probe of the first tile + tables)
        let head = {
            let first = {
                let mut c = tiff_read::TileCursor::new(src, &hdr, ifd)?;
                c.next_pair()?
                    .ok_or_else(|| CoreError::validation("层级无 tile"))?
            };
            let raw = src.read_at(first.0, first.1.min(svs::PROBE_LIMIT) as usize)?;
            let merged = match &lv.jpeg_tables {
                Some(t) => svs::merge_tables_then_tile(t, &raw),
                None => raw,
            };
            crate::jpeg::scan_jpeg(&merged)?
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
            reconstruct_cells_svs(
                src, &hdr, ifd, lv, &head, u64::MAX, compact, &mut stats, &mut edge_regions,
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
            reconstruct_cells_svs(
                src, &hdr, ifd, lv, &head, skip_until, compact, &mut stats, &mut edge_regions,
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
        while let Some((off, len)) = cur.next_pair()? {
            job.check()?;
            let row = cell / lv.tiles_across as u64;
            let col = cell % lv.tiles_across as u64;
            let payload = src.read_at(off, len as usize)?;
            let probe = check_tile(&payload, lv, &head)?;
            // interior cells must be nominal; boundary cells may be cropped
            let interior_row = row + 1 < lv.tiles_down as u64;
            let interior_col = col + 1 < lv.tiles_across as u64;
            if (interior_row && interior_col)
                && (probe.width != lv.tile_w || probe.height != lv.tile_h)
            {
                return Err(CoreError::variant(format!(
                    "层 {} 内部 tile({row},{col}) 尺寸 {}×{} ≠ 标称 {}×{}（非边界不允许残缺）",
                    li, probe.width, probe.height, lv.tile_w, lv.tile_h
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
                // compact-jpeg-v1: EVERY tile decoded (shared JPEGTables
                // merged, adapter colourspace rule) → re-encoded at the
                // LOCKED compact parameters, source tile geometry kept;
                // cropped tiles padded onto the white tile canvas (already
                // recorded as the edge region above).
                let (encoded, _) = compact_reencode_tile(&payload, lv, &probe, cfg)?;
                data = encoded;
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
                "层 {} tile 游标 {} ≠ 网格总数 {}",
                li, cell, lv.tiles_total
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
            SvsWriter::Classic { description, .. } => description.clone(),
            SvsWriter::Ome { .. } => Vec::new(),
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
    let compact_tiles_reencoded: u64 = level_stats.iter().map(|l| l.tiles_reencoded).sum();
    // under compact every padded (cropped) tile is exactly one EdgeRegion,
    // so the list length IS the padded count (same rule as the KFB path)
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

/// Convenience wrapper without progress (tests/small uses).
pub fn convert_svs(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_svs_to_bigtiff(src, sink, scratch, plan, &job)
}
