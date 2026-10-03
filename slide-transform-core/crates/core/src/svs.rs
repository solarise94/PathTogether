//! Aperio SVS (brightfield, JPEG tiles) input adapter (F1).
//!
//! Detection contract (bounded reads only, whole file never buffered):
//!
//! - the file must be a classic TIFF or BigTIFF (either byte order);
//! - IFD 0 must be the main image: **tiled**, compression 7 (baseline JPEG),
//!   photometric 2 (RGB) or 6 (YCbCr), 3 samples of 8 bits, chunky planar;
//!   its ImageDescription must identify Aperio;
//! - the true reduced-resolution levels are the *tiled JPEG* pages after
//!   IFD 0 that form a strictly decreasing ~2–8× sequence (Aperio: 4×).
//!   Stripped pages are associated images: thumbnail / label / macro
//!   (label and macro are recognised by their description markers or
//!   NewSubfileType bits; every other small stripped page is the thumbnail).
//!   A stripped page as large as the main image, a second tiled series, an
//!   ExtraSamples/fluorescence page set, planar (interleave=2) data or a
//!   non-JPEG codec is a typed rejection — never a best-effort copy;
//! - the JPEG payloads' **true** colorspace comes from the JPEG itself
//!   (JFIF/Adobe markers, then the TIFF photometric for the ambiguous
//!   Aperio `JPEG/RGB` case; see [`crate::jpeg::tiff_jpeg_color`]) — the
//!   source's own YCbCrSubSampling tag is known to lie (CMU-1 says (2,2)
//!   while every SOF is 4:4:4) and is never trusted;
//! - shared `JPEGTables` (tag 347) are carried into the output IFD verbatim
//!   when present; when absent every tile must be a complete JPEG stream;
//! - MPP comes from the description `MPP = …` and the objective from
//!   `AppMag = …` when present; both stay unknown otherwise (nothing is
//!   invented);
//! - an ICC profile (tag 34675) on the main IFD is carried into the output.
//!
//! Label/macro/thumbnail are NOT exported: this is a main-image conversion,
//! not an archive of the source file.

use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;
use crate::report::AssociatedSummary;
use crate::tiff_read::{self, Ifd, TiffHeader, TiffKind, TileCursor};

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "aperio-svs-jpeg";
/// Adapter version (bump on any output-affecting change; resume refuses on
/// mismatch, mirroring the output-profile refusal).
pub const ADAPTER_VERSION: &str = "1";

/// TIFF tags used here.
mod tag {
    pub const NEW_SUBFILE_TYPE: u16 = 254;
    pub const WIDTH: u16 = 256;
    pub const HEIGHT: u16 = 257;
    pub const BITS: u16 = 258;
    pub const COMPRESSION: u16 = 259;
    pub const PHOTO: u16 = 262;
    pub const DESCRIPTION: u16 = 270;
    pub const STRIP_OFFSETS: u16 = 273;
    pub const SAMPLES: u16 = 277;
    pub const ROWS_PER_STRIP: u16 = 278;
    pub const STRIP_BYTE_COUNTS: u16 = 279;
    pub const PLANAR: u16 = 284;
    pub const TILE_W: u16 = 322;
    pub const TILE_H: u16 = 323;
    pub const TILE_OFFSETS: u16 = 324;
    pub const TILE_COUNTS: u16 = 325;
    pub const EXTRA_SAMPLES: u16 = 338;
    pub const JPEG_TABLES: u16 = 347;
    pub const ICC: u16 = 34675;
}

/// Sane geometric caps (a real Aperio level 0 is ≲ 300 000 px per side).
const MAX_SIDE: u64 = 1_000_000;
const MAX_TILES_PER_LEVEL: u64 = 100_000_000;
const MAX_LEVELS: usize = 32;
/// Cap of a single probe payload read (scan_jpeg only needs up to the SOS).
pub const PROBE_LIMIT: u64 = 256 * 1024;

/// True colorspace of a level's JPEG payloads.
pub use crate::jpeg::TiffJpegColor as PayloadColor;

#[derive(Debug, Clone)]
pub struct SvsLevel {
    /// Index in the source IFD chain (reporting/provenance only).
    pub ifd_index: u32,
    pub width: u32,
    pub height: u32,
    pub tile_w: u32,
    pub tile_h: u32,
    pub tiles_across: u32,
    pub tiles_down: u32,
    pub tiles_total: u64,
    /// Tag 347 bytes (verbatim) when the level shares JPEG tables.
    pub jpeg_tables: Option<Vec<u8>>,
    /// Sum of TileByteCounts (streamed; every interval bounds-checked).
    pub payload_bytes: u64,
    /// True JPEG colorspace determined from the payloads.
    pub color: PayloadColor,
    /// SOF sampling (the truth; the TIFF 530 tag is not trusted).
    pub sampling: (u8, u8, u8, u8, u8, u8),
}

impl SvsLevel {
    pub fn tiles_across(&self) -> u32 {
        self.tiles_across
    }
    pub fn tiles_down(&self) -> u32 {
        self.tiles_down
    }
}

#[derive(Debug, Clone)]
pub struct SvsDoc {
    pub kind: TiffKind,
    pub levels: Vec<SvsLevel>,
    /// µm/px from `MPP = …` (main IFD description).
    pub mpp: Option<f64>,
    /// Objective from `AppMag = …`.
    pub appmag: Option<f64>,
    /// ICC profile bytes of the main IFD (tag 34675), carried to the output.
    pub icc: Option<Vec<u8>>,
    /// Associated images (thumbnail/label/macro) — detected, NOT exported.
    pub associated: Vec<AssociatedSummary>,
}

fn get_u64(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    tag: u16,
) -> CoreResult<Option<u64>> {
    tiff_read::find_u64(src, hdr, ifd, tag)
}

/// Merge `JPEGTables` (SOI…tables…EOI) with an abbreviated tile stream
/// (SOI…SOF…SOS…EOI) into one self-contained JPEG. Tables minus EOI + tile
/// minus SOI, the convention every TIFF reader uses.
pub fn merge_tables_then_tile(tables: &[u8], tile: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(tables.len() + tile.len());
    out.extend_from_slice(&tables[..tables.len().saturating_sub(2)]);
    out.extend_from_slice(&tile[2.min(tile.len())..]);
    out
}

/// Read the first tile payload of an IFD (bounded) for the JPEG truth probe.
fn first_tile_head(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
) -> CoreResult<Vec<u8>> {
    let mut cur = TileCursor::new(src, hdr, ifd)?;
    let (off, len) = cur
        .next_pair()?
        .ok_or_else(|| CoreError::validation("层级无任何 tile"))?;
    let want = len.min(PROBE_LIMIT) as usize;
    src.read_at(off, want)
}

/// Aperio description properties: `MPP = 0.4990`, `AppMag = 20` after `|`.
fn description_property(desc: &str, key: &str) -> Option<f64> {
    for part in desc.split('|') {
        let part = part.trim().trim_matches('\0');
        let Some((k, v)) = part.split_once('=') else { continue };
        if k.trim() == key {
            if let Ok(v) = v.trim().trim_matches('\0').parse::<f64>() {
                if v.is_finite() {
                    return Some(v);
                }
            }
        }
    }
    None
}

fn is_aperio(desc: &[u8]) -> bool {
    // the first line is "Aperio Image Library v…"; tolerate any position of
    // the vendor marker inside the description
    let s = String::from_utf8_lossy(desc);
    s.contains("Aperio")
}

/// Verify one level IFD's tile geometry and determine the JPEG truth.
fn inspect_level(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    ifd_index: u32,
    photometric: u64,
) -> CoreResult<SvsLevel> {
    let width = get_u64(src, hdr, ifd, tag::WIDTH)?.unwrap_or(0);
    let height = get_u64(src, hdr, ifd, tag::HEIGHT)?.unwrap_or(0);
    let tw = get_u64(src, hdr, ifd, tag::TILE_W)?.unwrap_or(0);
    let th = get_u64(src, hdr, ifd, tag::TILE_H)?.unwrap_or(0);
    if !(1..=MAX_SIDE).contains(&width) || !(1..=MAX_SIDE).contains(&height) {
        return Err(CoreError::variant(format!(
            "层级尺寸 {width}×{height} 越界"
        )));
    }
    if !(16..=8192).contains(&tw) || !(16..=8192).contains(&th) {
        return Err(CoreError::variant(format!(
            "tile 尺寸 {tw}×{th} 不在支持范围（16–8192）"
        )));
    }
    let across = (width + tw - 1) / tw;
    let down = (height + th - 1) / th;
    let total = across
        .checked_mul(down)
        .ok_or_else(|| CoreError::variant("tile 网格数量溢出"))?;
    if total < 1 || total > MAX_TILES_PER_LEVEL {
        return Err(CoreError::variant(format!("tile 数 {total} 越界")));
    }
    let mut cur = TileCursor::new(src, hdr, ifd)?;
    if cur.total() != total {
        return Err(CoreError::validation(format!(
            "TileOffsets 数 {} ≠ 网格 {}×{}",
            cur.total(),
            across,
            down
        )));
    }
    // stream every (offset,count) so the whole grid is bounds-checked and the
    // payload sum is exact (memory stays O(chunk))
    let mut payload_bytes = 0u64;
    while let Some((_, c)) = cur.next_pair()? {
        payload_bytes = payload_bytes.saturating_add(c);
    }
    let jpeg_tables = match ifd.find(tag::JPEG_TABLES) {
        Some(e) => Some(tiff_read::entry_value(src, hdr, e)?),
        None => None,
    };
    let head = first_tile_head(src, hdr, ifd)?;
    let probe_full = match &jpeg_tables {
        Some(t) => merge_tables_then_tile(t, &head),
        None => head.clone(),
    };
    let probe = jpeg::scan_jpeg(&probe_full).map_err(|e| {
        CoreError::jpeg(format!(
            "首 tile 不是合法 JPEG（合并 JPEGTables 后）：{}",
            e.message
        ))
    })?;
    let sampling = probe.sampling.ok_or_else(|| {
        CoreError::variant("JPEG 不是三分量（灰度/荧光 tile 不适用于明场转换）")
    })?;
    // abbreviated stream without tables must carry its own DQT/DHT
    if jpeg_tables.is_none() {
        let q = jpeg::qtables_pillow_style(&probe_full);
        if q.is_none() || q.as_deref().map_or(true, |v| v.is_empty()) {
            return Err(CoreError::variant(
                "tile 流既无共享 JPEGTables 也无自带量化表，无法按原样搬运",
            ));
        }
    }
    if probe.width as u64 > tw || probe.height as u64 > th {
        return Err(CoreError::variant(format!(
            "首 tile 尺寸 {}×{} 大于 tile 标称 {}×{}",
            probe.width, probe.height, tw, th
        )));
    }
    let color = jpeg::tiff_jpeg_color(&probe, photometric);
    Ok(SvsLevel {
        ifd_index,
        width: width as u32,
        height: height as u32,
        tile_w: tw as u32,
        tile_h: th as u32,
        tiles_across: across as u32,
        tiles_down: down as u32,
        tiles_total: total,
        jpeg_tables,
        payload_bytes,
        color,
        sampling,
    })
}

/// Capability probe: recognised + convertible, or a typed rejection.
pub fn probe_svs(src: &dyn ByteSource) -> CoreResult<SvsDoc> {
    let hdr = tiff_read::read_header(src)?;
    let chain = tiff_read::ifd_chain(src, &hdr)?;

    // ---- IFD 0: the main image ---------------------------------------- //
    let main = &chain[0];
    let compression = get_u64(src, &hdr, main, tag::COMPRESSION)?;
    let photo = get_u64(src, &hdr, main, tag::PHOTO)?.unwrap_or(0);
    let samples = get_u64(src, &hdr, main, tag::SAMPLES)?.unwrap_or(1);
    let planar = get_u64(src, &hdr, main, tag::PLANAR)?.unwrap_or(1);
    let tiled = main.find(tag::TILE_W).is_some() && main.find(tag::TILE_H).is_some();
    let desc = match main.find(tag::DESCRIPTION) {
        Some(e) => tiff_read::entry_value(src, &hdr, e)?,
        None => Vec::new(),
    };
    if main.find(tag::EXTRA_SAMPLES).is_some() {
        return Err(CoreError::variant(
            "主图带 ExtraSamples（荧光/透明通道页组不在明场 SVS 支持集）",
        ));
    }
    if !tiled {
        return Err(CoreError::variant(
            "主图不是分块（tiled）存储：带状 Aperio 变体不在支持集",
        ));
    }
    match compression.unwrap_or(0) {
        7 => {}
        33003 | 33005 => {
            return Err(CoreError::variant(format!(
                "JPEG 2000 压缩（{}）不在 F1 支持集（需要独立解码器，见计划 §5.3）",
                compression.unwrap_or(0)
            )))
        }
        c => {
            return Err(CoreError::variant(format!(
                "压缩编码 {c} 不是基线 JPEG（259=7），无法按原样搬运"
            )))
        }
    }
    if planar != 1 {
        return Err(CoreError::variant(format!(
            "PlanarConfiguration={planar}（平面存储）不在支持集（仅 chunky/1）"
        )));
    }
    if samples != 3 {
        return Err(CoreError::variant(format!(
            "SamplesPerPixel={samples}（荧光/多通道或灰度页组不在明场 SVS 支持集）"
        )));
    }
    if let Some(e) = main.find(tag::BITS) {
        if e.typ != 3 || e.count != 3 {
            return Err(CoreError::variant(format!(
                "BitsPerSample 类型/数量异常（type={} count={}）",
                e.typ, e.count
            )));
        }
        let bits = tiff_read::entry_value(src, &hdr, e)?;
        let vals: Vec<u16> = bits
            .chunks_exact(2)
            .map(|c| {
                let v = [c[0], c[1]];
                if hdr.little { u16::from_le_bytes(v) } else { u16::from_be_bytes(v) }
            })
            .collect();
        if vals != [8, 8, 8] {
            return Err(CoreError::variant(format!(
                "BitsPerSample={vals:?} ≠ [8,8,8]"
            )));
        }
    }
    if photo != 2 && photo != 6 {
        return Err(CoreError::variant(format!(
            "PhotometricInterpretation={photo} 不在支持集（RGB=2 / YCbCr=6）"
        )));
    }
    if !is_aperio(&desc) {
        return Err(CoreError::variant(
            "TIFF 结构合法但 ImageDescription 未标识 Aperio：未知厂商变体不猜",
        ));
    }

    let mut levels = vec![inspect_level(src, &hdr, main, 0, photo)?];
    let mut associated: Vec<AssociatedSummary> = Vec::new();

    // ---- following IFDs: levels and associated images ------------------- //
    for (idx, ifd) in chain.iter().enumerate().skip(1) {
        if levels.len() >= MAX_LEVELS {
            return Err(CoreError::variant(format!("层级数超过 {MAX_LEVELS}")));
        }
        let c = get_u64(src, &hdr, ifd, tag::COMPRESSION)?.unwrap_or(0);
        let p = get_u64(src, &hdr, ifd, tag::PHOTO)?.unwrap_or(0);
        let spp = get_u64(src, &hdr, ifd, tag::SAMPLES)?.unwrap_or(1);
        let w = get_u64(src, &hdr, ifd, tag::WIDTH)?.unwrap_or(0);
        let h = get_u64(src, &hdr, ifd, tag::HEIGHT)?.unwrap_or(0);
        let tiled = ifd.find(tag::TILE_W).is_some() && ifd.find(tag::TILE_H).is_some();
        let d = match ifd.find(tag::DESCRIPTION) {
            Some(e) => tiff_read::entry_value(src, &hdr, e)?,
            None => Vec::new(),
        };
        let ds = String::from_utf8_lossy(&d).to_lowercase();
        // Aperio marks the associated pages on their own description line
        // ("label 387x463", "macro 1280x431") and/or NewSubfileType bits.
        let line_marker = |m: &str| {
            ds.split(['\n', '\r'])
                .any(|ln| ln.trim_start().starts_with(m))
        };
        let nsf = get_u64(src, &hdr, ifd, tag::NEW_SUBFILE_TYPE)?.unwrap_or(0);
        if ifd.find(tag::EXTRA_SAMPLES).is_some() || spp > 3 {
            return Err(CoreError::variant(format!(
                "第 {idx} 页 SamplesPerPixel={spp}/ExtraSamples（荧光或多通道页组不在支持集）"
            )));
        }
        if tiled {
            // a true reduced level: tiled JPEG, strictly smaller, 1.5–8× per step
            if c != 7 {
                return Err(CoreError::variant(format!(
                    "第 {idx} 页为分块存储但压缩编码 {c} 不是基线 JPEG",
                )));
            }
            let prev = levels.last().expect("level 0 exists");
            if w >= prev.width as u64 || h >= prev.height as u64 {
                return Err(CoreError::variant(format!(
                    "第 {idx} 页是第二个分块序列（{w}×{h} 未小于上一层级 {}×{}）：未知多序列布局",
                    prev.width, prev.height
                )));
            }
            let rx = prev.width as f64 / w as f64;
            let ry = prev.height as f64 / h as f64;
            if !(1.5..=8.0).contains(&rx) || !(1.5..=8.0).contains(&ry) || (rx - ry).abs() > 1.5 {
                return Err(CoreError::variant(format!(
                    "第 {idx} 页降采样比异常（x={rx:.3}, y={ry:.3}）：不是已知的 Aperio 层级序列",
                )));
            }
            if p != 2 && p != 6 {
                return Err(CoreError::variant(format!(
                    "第 {idx} 页 PhotometricInterpretation={p} 不在支持集",
                )));
            }
            levels.push(inspect_level(src, &hdr, ifd, idx as u32, p)?);
        } else {
            // stripped page: associated image (thumbnail / label / macro)
            let name = if line_marker("label") {
                "label"
            } else if line_marker("macro") || (nsf & 8) != 0 {
                "macro"
            } else {
                "thumbnail"
            };
            if w > levels[0].width as u64 || h > levels[0].height as u64 {
                return Err(CoreError::variant(format!(
                    "第 {idx} 页为带状存储且尺寸 {w}×{h} 不小于主图：未知 series 布局",
                )));
            }
            associated.push(AssociatedSummary {
                name: name.to_string(),
                source_offset: 0, // not exported; offsets are not recorded
                source_length: 0,
                width: w as u32,
                height: h as u32,
            });
        }
    }

    let desc_s = String::from_utf8_lossy(&desc).to_string();
    let mpp = description_property(&desc_s, "MPP").filter(|v| *v > 0.0);
    let appmag = description_property(&desc_s, "AppMag").filter(|v| *v > 0.0);
    let icc = match main.find(tag::ICC) {
        Some(e) => Some(tiff_read::entry_value(src, &hdr, e)?),
        None => None,
    };
    Ok(SvsDoc { kind: hdr.kind, levels, mpp, appmag, icc, associated })
}

/// Disk-precheck estimate for the SVS adapter (same shape as the KFB ones).
pub fn estimate_svs(doc: &SvsDoc) -> crate::estimate::OutputEstimate {
    let mut payload = 0u64;
    let mut tiles = 0u64;
    for lv in &doc.levels {
        payload = payload.saturating_add(lv.payload_bytes);
        tiles += lv.tiles_total;
    }
    let extras = doc
        .levels
        .iter()
        .map(|l| l.jpeg_tables.as_ref().map_or(0, |t| t.len() as u64))
        .sum::<u64>()
        .saturating_add(doc.icc.as_ref().map_or(0, |i| i.len() as u64));
    let ifds = doc.levels.len() as u64;
    let out = payload
        .saturating_mul(5)
        .div_ceil(4)
        .saturating_add(tiles.saturating_mul(16))
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(extras)
        .saturating_add(1024 * 1024);
    // compact (U3): every tile is re-encoded at the locked compact
    // parameters — pixel-bound, not byte-bound; same 1.5× slack as the KFB
    // estimate (the tables/ICC extras vanish, the slack covers them)
    let compact = payload
        .saturating_mul(3)
        .div_ceil(2)
        .saturating_add(tiles.saturating_mul(16))
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(1024 * 1024);
    crate::estimate::OutputEstimate {
        payload_bytes: payload,
        tiles_present: tiles,
        cells_total: tiles,
        cells_missing: 0,
        edge_tiles: 0, // passthrough: edge tiles are copied, not re-encoded
        ifds,
        output_upper_bound_bytes: out,
        compact_upper_bound_bytes: compact,
    }
}
