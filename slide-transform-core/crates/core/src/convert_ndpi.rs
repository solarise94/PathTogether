//! NDPI → brightfield conversion (F6): whole-layer restart-segmented JPEG
//! strips → classic multi-IFD JPEG tiled BigTIFF pyramid or RGB OME-BigTIFF.
//!
//! **What `preserve-source-v1` means for NDPI** (stated honestly): an NDPI
//! layer is ONE whole-layer JPEG strip — there are no source tiles to copy,
//! so no byte-passthrough exists for this format. `preserve` means: decode
//! the layer strip restart segment by restart segment (each segment is a
//! self-contained MCU run with reset DC predictors — the only bounded decode
//! unit the format offers), paste the decoded MCU rects into the output tile
//! grid and re-encode every 256×256 output tile at the documented
//! high-fidelity setting **YCbCr 4:2:2 · quality 96 · standard Huffman**
//! (fingerprint [`PRESERVE_COMPOSE_FINGERPRINT`], reported as `composed`).
//! `compact-jpeg-v1` composes identically and re-encodes at the locked U3
//! parameters.
//!
//! Reduced output levels are the `l0-box2` chain (the 2×2 area-average of
//! output level 0, read back from the committed sink): the source's own
//! reduced layers are never decoded for pixels, exactly like the MRXS v2 and
//! generic-TIFF adapters. The macro/focus-map/z-stack pages are excluded at
//! the probe.
//!
//! Strict-lossless is refused (typed, before any output byte): every output
//! tile is a re-encode by construction.
//!
//! Memory (review §1): the band buffer (⌈256/mcu_h⌉ MCU rows × padded
//! width), the tile canvas + encode buffers, and every segment decode
//! (read + decoder transient + decoded pixels) are charged against the
//! host budget BEFORE the allocation — an over-budget strip is a typed
//! `resource_profile_insufficient` refusal, never an OOM mid-decode.
//!
//! Resume mirrors the other adapters: checkpoints per committed tile row;
//! a resumed L0 fast-forwards the segment scanner to the tile row's first
//! segment (marker scan only, no decode) and the fresh path stays
//! byte-identical.

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::budget::MemBudget;
use crate::convert_bf::{compact_sampling_label, compact_sampling_tiff};
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::jpeg;
use crate::ndpi::{
    probe_ndpi_with_budget, NdpiDoc, NdpiLevel, ADAPTER_VERSION, OUT_TILE,
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

pub const WARN_ASSOC_NOT_EXPORTED: &str = "ndpi_associated_not_exported";
pub const WARN_NO_ICC: &str = "color_management_not_applied";

/// Bounded RST scan chunk (bytes per source read while hunting markers).
const SCAN_CHUNK: u64 = 64 * 1024;

fn preserve_cfg() -> jpeg::EncoderCfg {
    // Same parameters as the MRXS compose / generic-TIFF generated tiles:
    // YCbCr 4:2:2 at quality 96 (the (2,1) TIFF layout every reader decodes).
    jpeg::EncoderCfg::with_quality(PRESERVE_COMPOSE_QUALITY, PRESERVE_COMPOSE_SAMPLING)
}

// --------------------------------------------------------------------------- //
// restart-segment reader (forward-only; bounded reads + budget charges)
// --------------------------------------------------------------------------- //

struct SegmentRead {
    img: jpeg::DecodedImage,
    /// Global MCU index of the segment's first MCU.
    mcu_start: u64,
    /// MCUs actually coded in this segment (the last one is partial).
    mcus: u64,
    /// MCUs per row of the decoded sub-image grid.
    grid_w: u64,
    /// Bytes charged against the budget for this decode (read + transient).
    charged: u64,
}

struct SegmentReader<'a> {
    src: &'a dyn ByteSource,
    lv: &'a NdpiLevel,
    /// Strip head without the DRI segment (the sub-JPEG header).
    header: Vec<u8>,
    /// Byte offset just past the last consumed RST marker.
    scan_at: u64,
    /// Index of the next segment to decode.
    next_k: u64,
}

impl<'a> SegmentReader<'a> {
    fn new(src: &'a dyn ByteSource, lv: &'a NdpiLevel) -> Self {
        // build the DRI-less header once
        let mut header = lv.header_bytes.clone();
        if let Some((s, e)) = lv.dri_at {
            header.drain(s..e);
        }
        debug_assert!(header.len() >= 4 && header[0..2] == [0xFF, 0xD8]);
        SegmentReader {
            src,
            lv,
            header,
            scan_at: lv.strip_offset + lv.header_bytes.len() as u64,
            next_k: 0,
        }
    }

    fn sof_hw_at(&self) -> usize {
        match self.lv.dri_at {
            Some((s, e)) if self.lv.sof_hw_at >= e => self.lv.sof_hw_at - (e - s),
            _ => self.lv.sof_hw_at,
        }
    }

    /// Scan [from, limit) for the next restart/EOI marker (0xFF D0–D7/D9).
    /// Bounded chunked reads; each chunk is charged and released.
    fn scan_marker(
        &mut self,
        budget: &mut MemBudget,
        from: u64,
        limit: u64,
    ) -> CoreResult<Option<(u64, u8)>> {
        let mut at = from;
        let mut carry: Option<u8> = None;
        let strip_end = self.lv.strip_offset + self.lv.strip_bytes;
        let limit = limit.min(strip_end);
        while at < limit {
            let want = SCAN_CHUNK.min(limit - at);
            budget.charge(want, "RST 标记扫描读取")?;
            let buf = match self.src.read_at(at, want as usize) {
                Ok(b) => b,
                Err(e) => {
                    budget.release(want);
                    return Err(e);
                }
            };
            let mut i = 0usize;
            if let Some(prev) = carry {
                if buf[0] == prev && (0xD0..=0xD9).contains(&buf[1]) {
                    budget.release(want);
                    return Ok(Some((at - 1, buf[1])));
                }
            }
            while i + 1 < buf.len() {
                if buf[i] == 0xFF && (0xD0..=0xD9).contains(&buf[i + 1]) {
                    budget.release(want);
                    return Ok(Some((at + i as u64, buf[i + 1])));
                }
                i += 1;
            }
            carry = buf.last().copied();
            at += want;
            budget.release(want);
        }
        Ok(None)
    }

    /// Fast-forward the scanner past segment boundaries (resume path): scan
    /// only, no decode.
    fn skip_to(&mut self, budget: &mut MemBudget, k: u64) -> CoreResult<()> {
        if k > self.lv.segments {
            return Err(CoreError::validation("resume: 分段游标越界"));
        }
        while self.next_k < k {
            let Some((at, m)) = self.scan_marker(
                budget,
                self.scan_at,
                self.lv.strip_offset + self.lv.strip_bytes,
            )?
            else {
                return Err(CoreError::oob(format!(
                    "分段 {} 的 RST 标记缺失（条带截断）",
                    self.next_k
                )));
            };
            let want = 0xD0 + (self.next_k & 7) as u8;
            if m != want {
                return Err(CoreError::jpeg(format!(
                    "分段 {} 的 RST 序号错乱（0x{m:02X} ≠ 0x{want:02X}）",
                    self.next_k
                )));
            }
            self.scan_at = at + 2;
            self.next_k += 1;
        }
        Ok(())
    }

    /// Decode the NEXT segment (strictly sequential). The sub-JPEG is the
    /// strip head minus DRI (restart_interval 0 → no RST checks) with the
    /// SOF dims patched to the segment's own grid, plus the segment's
    /// entropy bytes and an EOI. DC predictors reset at every RST in the
    /// source, so each segment decodes independently.
    fn decode_next(&mut self, budget: &mut MemBudget) -> CoreResult<SegmentRead> {
        let k = self.next_k;
        let r = self.lv.restart_interval as u64;
        let mcu_start = k * r;
        let mcus = r.min(self.lv.total_mcus - mcu_start);
        if mcus == 0 {
            return Err(CoreError::validation("分段越界（空段）"));
        }
        let seg_start = self.scan_at;
        let Some((marker_at, marker)) = self.scan_marker(
            budget,
            seg_start,
            self.lv.strip_offset + self.lv.strip_bytes,
        )?
        else {
            return Err(CoreError::oob(format!(
                "分段 {k} 的结束标记缺失（条带截断）"
            )));
        };
        // verify the marker that closed this segment (restart numbering
        // restarts at RST0 after SOS — the first restart is RST0)
        if k + 1 < self.lv.segments {
            let want = 0xD0 + (k & 7) as u8;
            if marker != want {
                return Err(CoreError::jpeg(format!(
                    "分段 {k} 的 RST 序号错乱（0x{marker:02X} ≠ 0x{want:02X}）"
                )));
            }
        } else if marker != 0xD9 {
            return Err(CoreError::jpeg(format!(
                "末段应以 EOI 结束（读到 0x{marker:02X}）"
            )));
        }
        let seg_len = (marker_at - seg_start) as usize;
        self.scan_at = marker_at + 2;
        self.next_k += 1;

        // segment grid: full MCU rows wherever that fits in 256 MCUs
        let grid_w = mcus.min(self.lv.mcus_x.max(1) as u64).min(256);
        let rows = mcus.div_ceil(grid_w);
        let sub_w = grid_w * self.lv.mcu_w() as u64;
        let sub_h = rows * self.lv.mcu_h() as u64;
        if sub_w > u32::MAX as u64 || sub_h > u32::MAX as u64 {
            return Err(CoreError::variant(format!(
                "分段 {k} 子图尺寸 {sub_w}×{sub_h} 溢出"
            )));
        }
        let sub_px = sub_w.checked_mul(sub_h).ok_or_else(|| {
            CoreError::resource_limit("分段子图像素溢出")
        })?;
        // Review §1: the charge covers the segment read + decoder transient
        // (component planes ≤ 3 B/px) + the decoded RGB (3 B/px) — refused
        // BEFORE either allocation.
        let charge = seg_len as u64 + sub_px.saturating_mul(6);
        budget.charge(charge, "restart 分段解码（读取 + 解码器临时 + RGB）")?;

        // header surgery: patch the SOF dims (height at sof_hw_at, width +2)
        let sof_at = self.sof_hw_at();
        if sof_at + 4 > self.header.len() {
            return Err(CoreError::jpeg("SOF 位置越界"));
        }
        let mut sub = Vec::with_capacity(self.header.len() + seg_len + 2);
        sub.extend_from_slice(&self.header);
        sub[sof_at..sof_at + 2].copy_from_slice(&(sub_h as u16).to_be_bytes());
        sub[sof_at + 2..sof_at + 4].copy_from_slice(&(sub_w as u16).to_be_bytes());
        let seg = self.src.read_at(seg_start, seg_len)?;
        sub.extend_from_slice(&seg);
        sub.extend_from_slice(&[0xFF, 0xD9]); // EOI

        let img = match jpeg::decode_ex(&sub, sub_px, false) {
            Ok(img) => img,
            Err(e) => {
                budget.release(charge);
                return Err(CoreError::jpeg(format!(
                    "分段 {k} 解码失败：{}",
                    e.message
                )));
            }
        };
        if (img.width as u64) != sub_w || (img.height as u64) != sub_h {
            budget.release(charge);
            return Err(CoreError::jpeg(format!(
                "分段 {k} 解码尺寸 {}×{} ≠ 子图 {sub_w}×{sub_h}",
                img.width, img.height
            )));
        }
        Ok(SegmentRead { img, mcu_start, mcus, grid_w, charged: charge })
    }
}

/// Paste one decoded segment's MCU rects into the band buffer. `band`
/// covers global pixel rows `[band_y0, band_y0 + band_rows)` and all
/// columns `[0, padded_w)`; MCU rects above/below the band or beyond the
/// level height are skipped (the band stays white there and no tile reads
/// those rows).
fn paste_segment(band: &mut [u8], seg: &SegmentRead, lv: &NdpiLevel, band_y0: u64, band_rows: u64) {
    let img = &seg.img;
    let img_w = img.width as usize;
    let mcu_w = lv.mcu_w() as usize;
    let mcu_h = lv.mcu_h() as usize;
    let padded_w = (lv.mcus_x as usize) * mcu_w;
    for i in 0..seg.mcus {
        let g = seg.mcu_start + i;
        let gc = (g % lv.mcus_x as u64) as usize;
        let gr = (g / lv.mcus_x as u64) as u64;
        // MCU rect in the segment image (segment grid: grid_w MCUs/row)
        let sx = (i % seg.grid_w) as usize * mcu_w;
        let sy = (i / seg.grid_w) as usize * mcu_h;
        let rel_y = (gr * mcu_h as u64) as i64 - band_y0 as i64;
        if rel_y < 0 {
            continue; // above the band (carried segments' head)
        }
        let by0 = rel_y as usize;
        if by0 >= band_rows as usize {
            continue; // below the band
        }
        let dst_x = gc * mcu_w;
        if dst_x >= padded_w {
            continue;
        }
        for r in 0..mcu_h {
            let world_y = gr as usize * mcu_h + r;
            if world_y >= lv.height as usize {
                break; // beyond the image (MCU padding rows)
            }
            let by = by0 + r;
            if by >= band_rows as usize {
                break;
            }
            let s = (sy + r) * img_w * 3 + sx * 3;
            let d = by * padded_w * 3 + dst_x * 3;
            band[d..d + mcu_w * 3].copy_from_slice(&img.data[s..s + mcu_w * 3]);
        }
    }
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

enum NdpiWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl NdpiWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            NdpiWriter::Classic { w, .. } => match committed {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            NdpiWriter::Ome { w, ome_xml, levels } => {
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
            NdpiWriter::Classic { w, .. } => w.end_level_ex(
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
            NdpiWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<()> {
        match self {
            NdpiWriter::Classic { w, .. } => w.write_tile(data).map(|_| ()),
            NdpiWriter::Ome { w, .. } => w.write_tile(data).map(|_| ()),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            NdpiWriter::Classic { w, .. } => w.cursor(),
            NdpiWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn tile_record(&self, ifd: usize, index: usize) -> CoreResult<(u64, u32)> {
        match self {
            NdpiWriter::Classic { w, .. } => w.tile_record(ifd, index),
            NdpiWriter::Ome { w, .. } => w.tile_record(ifd, index),
        }
    }

    fn read_output_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        match self {
            NdpiWriter::Classic { w, .. } => w.read_output_at(offset, len),
            NdpiWriter::Ome { w, .. } => w.read_output_at(offset, len),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            NdpiWriter::Classic { w, .. } => w.ifd_tile_counts(),
            NdpiWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            NdpiWriter::Classic { w, .. } => w.finish(),
            NdpiWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

fn ndpi_description_bytes(doc: &NdpiDoc) -> Vec<u8> {
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

fn ndpi_ome_xml(doc: &NdpiDoc, plan: &TransformPlan) -> Vec<u8> {
    let main = &doc.levels[0];
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
            "whole-layer JPEG strip decoded restart-segment by restart segment and \
             re-encoded into 256px output tiles; every tile re-encoded (no byte \
             passthrough exists for this format)"
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
            if doc.mpp.is_some() { "ndpi-vendor-mpp-tags" } else { "unknown" }.to_string(),
        ),
        (
            "objective_source",
            if doc.objective.is_some() { "ndpi-sourcelens" } else { "unknown" }.to_string(),
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
                "decoded from restart segments, then re-encoded at the locked compact \
                 parameters (quality {}, subsampling {}, standard Annex-K Huffman, \
                 fingerprint {}); lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            format!(
                "decoded from restart segments, then re-encoded at the documented \
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

fn level_meta(doc: &NdpiDoc, li: usize, compact: bool, dims: (u32, u32)) -> LevelMeta {
    let l0 = &doc.levels[0];
    let mpp = doc.mpp.map(|(mx, my)| {
        let rx = l0.width as f64 / dims.0 as f64;
        let ry = l0.height as f64 / dims.1 as f64;
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
/// level's tiles (read back from the committed sink; same method and
/// geometry as the generic-TIFF adapter's generated tail).
#[allow(clippy::too_many_arguments)]
fn pyramid_tile_from_prev(
    writer: &NdpiWriter<'_>,
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
// conversion
// --------------------------------------------------------------------------- //

pub fn convert_ndpi_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None)
}

/// Resume an NDPI conversion from `resume` (see [`crate::resume`]).
pub fn convert_ndpi_to_bigtiff_resume(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    // Adapter-version pin, enforced in the CORE (SCN/gtiff parity): a state
    // WITHOUT the field is foreign as well — never mix two adapter
    // generations into one output.
    if resume.adapter_version.as_deref() != Some(ADAPTER_VERSION) {
        return Err(CoreError::validation(format!(
            "resume: 已提交进度属于 NDPI 适配器 v{}，当前为 v{ADAPTER_VERSION}：两种适配器配方不得混合进同一输出",
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
    let doc = probe_ndpi_with_budget(src, plan.limits.memory_budget_bytes)?;
    let mut budget: MemBudget = doc.budget.clone();
    let l0 = &doc.levels[0];
    let out_levels: Vec<(u32, u32)> = std::iter::once((l0.width, l0.height))
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
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 NDPI 输入"));
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与 NDPI 输出互斥：整层条带必须分段解码后重编码（有损），无逐字节搬运路径",
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
        warnings.push(format!("ndpi_levels_generated:{}", doc.generated.len()));
    }
    if doc.icc.is_none() {
        warnings.push(WARN_NO_ICC.to_string());
    }

    // ---- working set, charged BEFORE any allocation (review §1) ---------- //
    let band_rows_mcu = (OUT_TILE as u64).div_ceil(l0.mcu_h() as u64);
    let band_rows_px = band_rows_mcu * l0.mcu_h() as u64;
    let padded_w = l0.mcus_x as u64 * l0.mcu_w() as u64;
    let band_bytes = band_rows_px
        .saturating_mul(padded_w)
        .saturating_mul(3);
    let canvas_bytes = (OUT_TILE as u64)
        .saturating_mul(OUT_TILE as u64)
        .saturating_mul(6); // tile canvas + encode buffers
    let max_seg_px = (l0.restart_interval as u64)
        .saturating_mul(l0.mcu_w() as u64)
        .saturating_mul(l0.mcu_h() as u64);
    budget.charge(band_bytes, "L0 条带缓冲（MCU 行带 × padded 宽 × 3）")?;
    budget.charge(canvas_bytes, "tile 画布与编码缓冲")?;
    budget.charge(
        max_seg_px.saturating_mul(6),
        "单段解码峰值预留（读取 + 解码器临时 + RGB）",
    )?;
    // the generated-pyramid working set (f² decoded prev tiles + canvases)
    let pyramid_ws = 4u64
        .saturating_mul((OUT_TILE as u64) * (OUT_TILE as u64) * 6)
        .saturating_add((OUT_TILE as u64 * 2).saturating_mul(OUT_TILE as u64 * 2).saturating_mul(3));
    if !doc.generated.is_empty() {
        budget.charge(pyramid_ws, "l0-box2 合成画布与输出缓冲")?;
    }

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => NdpiWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: ndpi_description_bytes(&doc),
        },
        OutputProfile::OmeBigTiffRgbSubifd => NdpiWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(ndpi_ome_xml(&doc, plan)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, NdpiWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };
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
            // ---- level 0: restart-segmented decode → paste → re-encode -- //
            if resume_done {
                // no pixels needed; counts only — but the description must be
                // restaged for a fully-resumed level too (end_level stages it
                // into the IFD; an empty vec here would drop tag 270 from the
                // resumed output and break byte identity; convert_scn 同款)
                let desc: Vec<u8> = match &writer {
                    NdpiWriter::Classic { description, .. } => description.clone(),
                    NdpiWriter::Ome { .. } => Vec::new(),
                };
                level_stats.push(stats);
                ifd_chain.push((li as u32, None));
                writer.end(&meta, &desc)?;
                continue;
            }
            let mut reader = SegmentReader::new(src, l0);
            if resume_current && skip_until > 0 {
                reader.skip_to(&mut budget, first_band_segment(l0, skip_until / across)?)?;
            }
            let mut cell = skip_until;
            let mut band = vec![255u8; band_bytes as usize];
            let mut canvas = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
            let mut carry: Option<SegmentRead> = None;
            let mut ty = skip_until / across;
            while ty < down {
                job.check()?;
                // decode the segments covering this band's MCU rows (strictly
                // sequential; a segment crossing the band edge is carried)
                let band_y0 = ty * OUT_TILE as u64;
                let band_rows_px_u = band_rows_px.min((l0.height as u64) - band_y0);
                for px in band.iter_mut() {
                    *px = 255;
                }
                // first MCU index past the band's valid rows (MCU-row aligned)
                let mcu_end = band_y0.saturating_add(band_rows_px_u)
                    .div_ceil(l0.mcu_h() as u64)
                    * l0.mcus_x as u64;
                loop {
                    let next_mcu = match &carry {
                        Some(c) => c.mcu_start,
                        None => reader.next_k * l0.restart_interval as u64,
                    };
                    if next_mcu >= mcu_end {
                        break;
                    }
                    let seg = match carry.take() {
                        Some(c) => c,
                        None => reader.decode_next(&mut budget)?,
                    };
                    paste_segment(&mut band, &seg, l0, band_y0, band_rows_px_u);
                    let seg_bottom =
                        (seg.mcu_start + seg.mcus).div_ceil(l0.mcus_x as u64)
                            * l0.mcu_h() as u64;
                    if seg_bottom > band_y0 + band_rows_px_u && reader.next_k < l0.segments {
                        // crosses into the next band: keep the decoded pixels
                        carry = Some(seg);
                    } else {
                        budget.release(seg.charged);
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
                        let srow = r * padded_w as usize * 3 + tx0 as usize * 3;
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
            if let Some(c) = carry.take() {
                budget.release(c.charged);
            }
            drop(reader);
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
                    NdpiWriter::Classic { description, .. } => description.clone(),
                    NdpiWriter::Ome { .. } => Vec::new(),
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
            NdpiWriter::Classic { description, .. } => description.clone(),
            NdpiWriter::Ome { .. } => Vec::new(),
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
        width: l0.width,
        height: l0.height,
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
            mode: "segment-compose-reencode".to_string(),
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

/// First segment index whose MCU range intersects tile row `ty`'s MCU rows.
fn first_band_segment(l0: &NdpiLevel, ty: u64) -> CoreResult<u64> {
    let mcu_row = (ty * OUT_TILE as u64) / l0.mcu_h() as u64;
    Ok(mcu_row * l0.mcus_x as u64 / l0.restart_interval as u64)
}

/// Convenience wrapper without progress (tests).
pub fn convert_ndpi(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_ndpi_to_bigtiff(src, sink, scratch, plan, &job)
}
