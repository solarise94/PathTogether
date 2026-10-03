//! SVS → brightfield conversion (F1): Aperio JPEG tiles → classic multi-IFD
//! JPEG BigTIFF pyramid or RGB OME-BigTIFF (SubIFD pyramid).
//!
//! Payload policy is **pure passthrough**: every source tile byte is copied
//! verbatim, in tile order, level by level. The shared `JPEGTables` of each
//! level are written verbatim into the output IFD (tag 347), so abbreviated
//! source streams stay abbreviated in the output — no splicing into tiles,
//! no re-encode, nothing relabelled. The output photometric is the JPEG
//! payloads' true colorspace (2 = RGB for Aperio `JPEG/RGB` streams, 6 =
//! YCbCr with the SOF subsampling), and the tile tags (322/323) carry the
//! source's own tile shape (e.g. 240).
//!
//! Edge policy: Aperio keeps full-size tiles past the image boundary; such
//! tiles are copied as-is (standard TIFF edge-tile semantics — content past
//! the image edge is unspecified and invisible). A genuinely cropped tile
//! (JPEG smaller than the nominal tile rect) is also copied verbatim and
//! recorded in `edge_regions`; because no pixel is ever re-encoded,
//! `StrictLossless` accepts everything the default policy accepts (there is
//! no lossy step in this adapter to forbid).
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
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::ome::{py_g17, py_repr_f64};
use crate::ome_writer::{OmeBigTiffWriter, RgbIfdExtras};
use crate::plan::{OutputProfile, PixelPolicy, TransformPlan};
use crate::report::{AssociatedSummary, EdgeRegion, LevelStats, TransformResult};
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
    provenance.push((
        "tile_payloads",
        "Aperio JPEG tiles copied byte-for-byte; shared JPEGTables written verbatim \
         into each level IFD (tag 347); no tile is re-encoded or relabelled"
            .to_string(),
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
/// JPEGTables; ICC on the main level only).
fn level_meta(doc: &SvsDoc, li: usize) -> LevelMeta {
    let lv = &doc.levels[li];
    let main = &doc.levels[0];
    // per-level calibration from the real dimension ratio (the source
    // carries no per-level resolution tags)
    let mpp = doc.mpp.map(|m| {
        let rx = main.width as f64 / lv.width as f64;
        let ry = main.height as f64 / lv.height as f64;
        (m * rx, m * ry)
    });
    let (photometric, tiff_sub) = match lv.color {
        PayloadColor::Rgb => (2u16, (1u16, 1u16)),
        PayloadColor::YCbCr => (6u16, (lv.sampling.0 as u16, lv.sampling.1 as u16)),
    };
    LevelMeta {
        width: lv.width,
        height: lv.height,
        tile: (lv.tile_w, lv.tile_h),
        photometric,
        tiff_sub,
        reduced: li > 0,
        mpp,
        jpeg_tables: lv.jpeg_tables.clone(),
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

/// Re-scan the boundary cells of a level for resume bookkeeping (edge
/// regions / stats). Interior cells are nominal by the accept rules (the
/// fresh path rejects anything else before writing).
fn reconstruct_cells_svs(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &tiff_read::Ifd,
    lv: &SvsLevel,
    head: &crate::jpeg::JpegProbe,
    up_to: u64,
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
                record_edge(lv, row, col, probe.width, probe.height, edge_regions, warnings);
            }
        }
        stats.tiles_raw_copied += 1;
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
    edge_regions: &mut Vec<EdgeRegion>,
    warnings: &mut Vec<String>,
) {
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
    // PixelPolicy: this adapter never re-encodes, so StrictLossless has
    // nothing to reject (documented in the report).

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
        let meta = level_meta(&doc, li);
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
                src, &hdr, ifd, lv, &head, u64::MAX, &mut stats, &mut edge_regions,
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
                src, &hdr, ifd, lv, &head, skip_until, &mut stats, &mut edge_regions,
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
                record_edge(lv, row, col, probe.width, probe.height, &mut edge_regions, &mut warnings);
            }
            writer.write_tile(&payload)?;
            stats.tiles_raw_copied += 1;
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
