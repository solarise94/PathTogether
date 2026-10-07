//! Ventana BIF input adapter (brightfield): a BigTIFF whose IFD 0 carries
//! the `iScan` vendor XML in XMLPacket (700) and whose level 0 carries the
//! `EncodeInfo` stitch XML (AOI origins, per-AOI tile grids and pairwise
//! `TileJointInfo` overlaps). The scanner writes tiles WITH overlaps — the
//! converter must stitch them at their recorded fractional positions before
//! re-encoding (the structural model mirrors OpenSlide's documented
//! `ventana.c` behaviour, independently implemented here with bounded reads
//! and typed errors — no OpenSlide code).
//!
//! Geometry contract (OpenSlide-faithful, verified against the public
//! OS-2.bif sample: 128000×82960 tiles canvas → 114943×76349 stitched):
//!
//! - level IFDs are identified by an `ImageDescription` containing `level=`;
//!   they must appear as `level=0,1,2,…` with strictly decreasing `mag=`;
//!   every level shares the L0 tile size (OpenSlide: "Inconsistent TIFF
//!   tile sizes");
//! - `Label Image`/`Label_Image`/`Thumbnail` IFDs are associated images
//!   (label/thumbnail; detected and NOT exported); anything else that is
//!   not a level (e.g. `Probability_Image`) is excluded the same way;
//! - level 0's XMLPacket holds `/EncodeInfo/SlideStitchInfo/ImageInfo`
//!   (one per AOI) paired with `/EncodeInfo/AoiOrigin/*`; `AOIScanned != 1`
//!   AOIs are skipped; `OriginX/Y` must be multiples of the tile size and
//!   name the AOI's first tile's GLOBAL (col,row) in the TIFF tile grid;
//! - every AOI's `TileJointInfo` children must be `Direction` RIGHT (tile2
//!   is tile1's right neighbour) or UP (tile2 ABOVE tile1) with
//!   boustrophedon tile numbers (odd rows from the bottom numbered
//!   right-to-left, rows bottom-to-top). LEFT/DOWN directions and
//!   non-adjacent joins are typed rejections (the public Ventana-1.bif
//!   sample is LEFT — OpenSlide rejects it too; 「暂时直传」variant);
//! - the tile advance is the confidence-weighted mean of the recorded
//!   overlaps: `advance = tile + Σ(confidence·(-Overlap))/Σ(confidence)`;
//!   the Pos-Y axis is flipped (Pos-Y measures from a point BELOW all
//!   areas: `y' = top - y - height` where
//!   `height = (rows-1)·advance_y + tile_h`, `top = max(y+height)`);
//! - the stitched level size is the ceiling of the bounding box's right /
//!   bottom edge over every placed tile (OpenSlide `_openslide_grid_get_
//!   bounds` → `ceil(x + w)`): a real BIF L0 is 114943×76349 while the
//!   TIFF canvas tags say 128000×82960;
//! - the output pyramid re-encodes L0 from the stitched tiles (the source's
//!   own reduced levels are never decoded for pixels); reduced output
//!   levels are the `l0-box2` chain, exactly like the NDPI/VMS adapters.
//!
//! Tile payloads are baseline JPEG WITH a shared `JPEGTables` (347) and NO
//! restart markers — the only bounded decode unit of a restart-less scan
//! is the MCU row, so the conversion streams every tile through
//! [`crate::jpeg::band::BandScanner`] (bounded 64 KiB entropy windows).
//! JPEG 2000 compression (33003/33005), multi-z (`Z-layers > 1`),
//! non-BigTIFF containers, gray/multi-sample and no-XML (non-stitched
//! `ventana tif`) variants are typed rejections before any copy.

use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;
use crate::report::AssociatedSummary;
use crate::tiff_read::{self, Ifd, TiffHeader, TiffKind};

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "ventana-bif-jpeg";
/// Adapter version (bump on any output-affecting change). Enforced in the
/// core's resume entry: a checkpoint that names another generation — or
/// carries no version at all — is refused (SCN/NDPI/VMS parity).
pub const ADAPTER_VERSION: &str = "1";

/// How reduced output levels are built (NDPI/VMS/generic-TIFF method id).
pub const PYRAMID_METHOD: &str = "l0-box2";
/// Locked preserve-mode compose parameters (the documented high-fidelity
/// family: same values as the NDPI/VMS compose).
pub const PRESERVE_COMPOSE_QUALITY: u8 = 96;
pub const PRESERVE_COMPOSE_SAMPLING: jpeg::Sampling = jpeg::Sampling::S422;
pub const PRESERVE_COMPOSE_HUFFMAN: &str = "standard-annex-k";
/// Versioned fingerprint of the compose encode parameters.
pub const PRESERVE_COMPOSE_FINGERPRINT: &str = "bif-mosaic-compose:q96:y422:hstd:v1";

/// Output-tile edge of the composed pyramid.
pub const OUT_TILE: u32 = 256;

/// Sane geometric caps (a real Ventana L0 is ≲ 130 000 px per side).
const MAX_SIDE: u64 = 1_000_000;
const MAX_LEVELS: usize = 32;
/// Cap of the EncodeInfo XML payload (a real OS-2.bif carries 1.7 MB; the
/// tiff reader itself caps tag values at 4 MiB — same order).
const MAX_XML_BYTES: u64 = 4 << 20;
/// Cap of a single tile payload probe read (marker scan up to SOS).
pub const PROBE_LIMIT: u64 = 256 * 1024;

/// TIFF tags used here.
mod tag {
    pub const WIDTH: u16 = 256;
    pub const HEIGHT: u16 = 257;
    pub const BITS: u16 = 258;
    pub const COMPRESSION: u16 = 259;
    pub const PHOTO: u16 = 262;
    pub const DESCRIPTION: u16 = 270;
    pub const SAMPLES: u16 = 277;
    pub const PLANAR: u16 = 284;
    pub const TILE_WIDTH: u16 = 322;
    pub const TILE_LENGTH: u16 = 323;
    pub const TILE_OFFSETS: u16 = 324;
    pub const TILE_COUNTS: u16 = 325;
    pub const ICC: u16 = 34675;
    /// vendor: JPEGTables (shared DQT/DHT of the abbreviated tile streams)
    pub const JPEGTABLES: u16 = 347;
    /// vendor: XMLPacket (iScan on IFD 0; EncodeInfo on level 0)
    pub const XMLPACKET: u16 = 700;
}

/// True colorspace of the tile payloads (same rule as SVS/NDPI).
pub use crate::jpeg::TiffJpegColor as PayloadColor;

/// One scanned AOI of the stitch layout (post Y-flip).
#[derive(Debug, Clone)]
pub struct BifArea {
    /// First tile's global column/row in the TIFF tile grid.
    pub start_col: i64,
    pub start_row: i64,
    pub tiles_across: i64,
    pub tiles_down: i64,
    /// Absolute stitched x of the first tile's left edge (fractional).
    pub x: f64,
    /// Absolute stitched y of the first tile's TOP edge (fractional,
    /// after the Pos-Y flip).
    pub y: f64,
}

impl BifArea {
    /// Fractional x of tile (col)'s left edge (the OpenSlide grid position).
    pub fn tile_x_f(&self, col: i64, advance_x: f64) -> f64 {
        let off = self.x - self.start_col as f64 * advance_x;
        col as f64 * advance_x + off
    }
    /// Fractional y of tile (row)'s top edge (after the Pos-Y flip).
    pub fn tile_y_f(&self, row: i64, advance_y: f64) -> f64 {
        let off = self.y - self.start_row as f64 * advance_y;
        row as f64 * advance_y + off
    }
    /// Integer paste offset of tile (col,row) — ROUND to the nearest pixel
    /// of the fractional position. Deterministic (browser == native), and
    /// measurably closer to OpenSlide's sub-pixel rendering than floor:
    /// with the OpenSlide precedence fixed, the zero-reencode ROI error on
    /// OS-2.bif is 3.77/2.10/4.01 (floor) vs 1.73/1.88/3.69 (round); the
    /// earlier "round only helps ~15%" claim was measured under the flipped
    /// precedence and is retracted.
    pub fn tile_x(&self, col: i64, advance_x: f64) -> i64 {
        self.tile_x_f(col, advance_x).round() as i64
    }
    pub fn tile_y(&self, row: i64, advance_y: f64) -> i64 {
        self.tile_y_f(row, advance_y).round() as i64
    }
}

/// One pyramid level IFD.
#[derive(Debug, Clone)]
pub struct BifLevel {
    pub ifd_index: u32,
    /// IFD-declared canvas size (NOT the stitched size; OS-2: 128000×82960).
    pub canvas_w: u32,
    pub canvas_h: u32,
    pub tile_w: u32,
    pub tile_h: u32,
    /// TIFF tile grid of the IFD (ceil(canvas/tile)).
    pub tiles_across: u64,
    pub tiles_down: u64,
    /// Objective magnification from the `mag=` description field.
    pub magnification: f64,
}

#[derive(Debug, Clone)]
pub struct BifDoc {
    pub kind: TiffKind,
    /// Level IFDs, L0 first (level=0,1,2,…).
    pub levels: Vec<BifLevel>,
    /// Stitched L0 size (== OpenSlide's reported dimensions).
    pub width: u32,
    pub height: u32,
    /// Scanned AOIs (post Y-flip).
    pub areas: Vec<BifArea>,
    /// Tile advances (tile size + confidence-weighted mean overlap).
    pub advance_x: f64,
    pub advance_y: f64,
    /// µm/px from the iScan `ScanRes` attribute; None when absent.
    pub mpp: Option<(f64, f64)>,
    /// Objective power from the iScan `Magnification` attribute.
    pub objective: Option<f64>,
    /// ICC profile bytes of IFD 0 (tag 34675), carried to the output.
    pub icc: Option<Vec<u8>>,
    /// Shared JPEGTables of level 0's abbreviated tile streams.
    pub jpeg_tables: Option<Vec<u8>>,
    /// True colorspace of the L0 tile payloads.
    pub color: PayloadColor,
    /// SOF sampling (h1,v1,h2,v2,h3,v3) of the L0 tiles.
    pub sampling: (u8, u8, u8, u8, u8, u8),
    /// MCU size in pixels (8·h_max, 8·v_max).
    pub mcu: (u32, u32),
    /// Number of tile payloads referenced by scanned AOIs.
    pub tiles_present: u64,
    /// Sum of the referenced tile payload bytes (estimate input).
    pub payload_bytes: u64,
    /// Excluded pages (label / thumbnail / probability / …).
    pub associated: Vec<AssociatedSummary>,
    /// Generated tail levels (width, height) — the `l0-box2` chain.
    pub generated: Vec<(u32, u32)>,
    /// Memory account carried from the probe into the conversion.
    pub budget: MemBudget,
}

// --------------------------------------------------------------------------- //
// bounded XML helpers (same shape as scn.rs; the crate carries no XML dep)
// --------------------------------------------------------------------------- //

/// `<name …>…</name>` element spans: (opening tag text, inner body). The
/// scan is nesting-free for the elements this adapter reads (they never
/// nest same-name); matching is case-sensitive exactly like the vendor's
/// own files write them.
fn tag_spans<'a>(xml: &'a str, name: &str) -> Vec<(&'a str, &'a str)> {
    let mut out = Vec::new();
    let mut from = 0usize;
    while let Some(rel) = xml[from..].find(&format!("<{name}")) {
        let start = from + rel;
        let after = &xml[start + 1 + name.len()..];
        // "<ImageInfoX" must not match "ImageInfo"
        if !after.starts_with(|c: char| c.is_whitespace())
            && !after.starts_with('>')
            && !after.starts_with('/')
        {
            from = start + 1 + name.len();
            continue;
        }
        let Some(gt) = after.find('>') else { break };
        let open_end = start + 1 + name.len() + gt + 1;
        let open_tag = &xml[start..open_end];
        if open_tag.ends_with("/>") {
            out.push((open_tag, ""));
            from = open_end;
            continue;
        }
        let close = format!("</{name}>");
        let Some(rel_end) = xml[open_end..].find(&close) else { break };
        let end = open_end + rel_end;
        out.push((open_tag, &xml[open_end..end]));
        from = end + close.len();
    }
    out
}

/// `<AOIn OriginX=… OriginY=…/>` children of one `<AoiOrigin>` body, in
/// AOI order (AOI0, AOI1, … — the pairing order of the ImageInfo set).
fn aoi_origin_children(body: &str) -> Vec<&str> {
    let mut out = Vec::new();
    for n in 0..32 {
        let name = format!("AOI{n}");
        let hits = tag_spans(body, &name);
        if hits.is_empty() {
            continue;
        }
        for (open, _) in hits {
            out.push(open);
        }
    }
    out
}

/// `k="v"` / `k='v'` attribute of one opening tag (None when absent). A
/// substring hit that is not followed by `=` (e.g. `Tile1` inside a longer
/// key) is skipped, not treated as absent-or-present.
fn attr<'a>(open_tag: &'a str, key: &str) -> Option<&'a str> {
    let b = open_tag.as_bytes();
    let mut i = 0usize;
    while let Some(rel) = open_tag[i..].find(key) {
        let at = i + rel;
        // word boundary before the key
        if at > 0 {
            let prev = b[at - 1];
            if prev.is_ascii_alphanumeric() || matches!(prev, b'-' | b'_' | b':') {
                i = at + key.len();
                continue;
            }
        }
        let rest = &open_tag[at + key.len()..];
        let rest = rest.trim_start();
        let Some(rest) = rest.strip_prefix('=') else {
            i = at + key.len();
            continue;
        };
        let rest = rest.trim_start();
        let quote = rest.chars().next()?;
        if quote != '"' && quote != '\'' {
            return None;
        }
        let inner = &rest[1..];
        let end = inner.find(quote)?;
        return Some(&inner[..end]);
    }
    None
}

fn attr_f64(open_tag: &str, key: &str) -> Option<f64> {
    attr(open_tag, key).and_then(|v| vtrim(v).parse::<f64>().ok()).filter(|v| v.is_finite())
}

fn attr_i64(open_tag: &str, key: &str) -> Option<i64> {
    attr(open_tag, key).and_then(|v| vtrim(v).parse::<i64>().ok())
}

/// Trailing junk tolerance ("115.0" / " 115"): parse the leading number.
fn vtrim(v: &str) -> &str {
    v.trim()
}

// --------------------------------------------------------------------------- //
// iScan (IFD 0) + EncodeInfo (level 0) parsing
// --------------------------------------------------------------------------- //

/// Read tag `t`'s value bytes with a size guard.
fn tag_bytes(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    t: u16,
    cap: u64,
    budget: &mut MemBudget,
    what: &str,
) -> CoreResult<Option<Vec<u8>>> {
    let Some(e) = ifd.find(t) else { return Ok(None) };
    if e.value_len().unwrap_or(u64::MAX) > cap {
        return Err(CoreError::oob(format!(
            "tag {t} 值 {} 字节超出上限 {cap}",
            e.value_len().unwrap_or(u64::MAX)
        )));
    }
    let raw = tiff_read::entry_value(src, hdr, e)?;
    budget.charge(raw.len() as u64, what)?;
    Ok(Some(raw))
}

fn ascii_tag(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    t: u16,
) -> CoreResult<String> {
    match ifd.find(t) {
        Some(e) => {
            if e.value_len().unwrap_or(u64::MAX) > 64 * 1024 {
                return Err(CoreError::oob(format!("tag {t} 值过长")));
            }
            let raw = tiff_read::entry_value(src, hdr, e)?;
            Ok(String::from_utf8_lossy(&raw).trim_end_matches('\0').to_string())
        }
        None => Ok(String::new()),
    }
}

fn find_i64(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    t: u16,
) -> CoreResult<Option<i64>> {
    Ok(match tiff_read::find_u64(src, hdr, ifd, t)? {
        Some(v) => Some(v as i64),
        None => None,
    })
}

/// Ventana tile-number → global (col,row), mirroring ventana.c
/// `get_tile_coordinates`: 1-based boustrophedon numbers — odd rows from
/// the bottom run left-to-right, even rows right-to-left, rows numbered
/// bottom-to-top.
fn tile_coordinates(
    tiles_across: i64,
    tiles_down: i64,
    tile_no: i64,
) -> CoreResult<(i64, i64)> {
    if tile_no < 1 || tile_no > tiles_across.checked_mul(tiles_down).unwrap_or(i64::MAX) {
        return Err(CoreError::variant(format!(
            "TileJointInfo 瓦片号 {tile_no} 越界（{}×{tiles_down} AOI）",
            tiles_across
        )));
    }
    let t = tile_no - 1;
    let mut col = t % tiles_across;
    let mut row = t / tiles_across;
    if row % 2 == 1 {
        col = tiles_across - col - 1;
    }
    row = tiles_down - row - 1;
    Ok((col, row))
}

/// One parsed ImageInfo/AoiOrigin pair (before the Y-flip).
struct RawArea {
    start_col: i64,
    start_row: i64,
    tiles_across: i64,
    tiles_down: i64,
    x: f64,
    y: f64,
}

/// Parse the level-0 EncodeInfo XML into areas + tile advances; mirrors
/// ventana.c `parse_level0_xml` (confidence-weighted overlap means, Pos-Y
/// flip, tile-size and origin divisibility checks, RIGHT/UP-only joins).
fn parse_encode_info(
    xml: &str,
    tile_w: i64,
    tile_h: i64,
) -> CoreResult<(Vec<BifArea>, f64, f64)> {
    // /EncodeInfo/SlideStitchInfo/ImageInfo — one per AOI (body carries the
    // AOI's TileJointInfo children)
    let infos = tag_spans(xml, "ImageInfo");
    // /EncodeInfo/AoiOrigin/* — one origin child per AOI (AOI0/AOI1/…)
    let origins = tag_spans(xml, "AoiOrigin")
        .iter()
        .flat_map(|(_, body)| aoi_origin_children(body))
        .collect::<Vec<&str>>();
    if infos.is_empty() || infos.len() != origins.len() {
        return Err(CoreError::variant(format!(
            "EncodeInfo 区域元数据缺失或不一致（ImageInfo {} 个 / AoiOrigin {} 个）",
            infos.len(),
            origins.len()
        )));
    }

    let mut raw: Vec<RawArea> = Vec::new();
    let mut tot_ox = 0.0f64;
    let mut tot_oy = 0.0f64;
    let mut tot_wx = 0i64;
    let mut tot_wy = 0i64;
    for ((info, info_body), aoi) in infos.iter().zip(origins.iter()) {
        // ventana.c：AOIScanned 非 0 即扫描（缺属性是其硬错误）；只有
        // 显式 0 才跳过
        match attr_i64(info, "AOIScanned") {
            Some(0) => continue, // ignored AOI (ventana.c skips them the same way)
            Some(_) => {}
            None => {
                return Err(CoreError::metadata(
                    "ImageInfo 缺少 AOIScanned 属性（OpenSlide 同样要求在场）",
                ))
            }
        }
        let start_col = attr_i64(aoi, "OriginX")
            .ok_or_else(|| CoreError::metadata("AoiOrigin 缺少 OriginX"))?;
        let start_row = attr_i64(aoi, "OriginY")
            .ok_or_else(|| CoreError::metadata("AoiOrigin 缺少 OriginY"))?;
        let w = attr_i64(info, "Width")
            .ok_or_else(|| CoreError::metadata("ImageInfo 缺少 Width"))?;
        let h = attr_i64(info, "Height")
            .ok_or_else(|| CoreError::metadata("ImageInfo 缺少 Height"))?;
        if w != tile_w || h != tile_h {
            return Err(CoreError::variant(format!(
                "AOI 瓦片尺寸 {w}×{h} ≠ TIFF 瓦片 {tile_w}×{tile_h}"
            )));
        }
        if start_col % tile_w != 0 || start_row % tile_h != 0 {
            return Err(CoreError::variant(format!(
                "AOI 原点 ({start_col},{start_row}) 不是瓦片尺寸的整数倍"
            )));
        }
        let tiles_across = attr_i64(info, "NumCols")
            .ok_or_else(|| CoreError::metadata("ImageInfo 缺少 NumCols"))?;
        let tiles_down = attr_i64(info, "NumRows")
            .ok_or_else(|| CoreError::metadata("ImageInfo 缺少 NumRows"))?;
        if tiles_across < 1 || tiles_down < 1 {
            return Err(CoreError::variant(format!(
                "AOI 瓦片数 {tiles_across}×{tiles_down} 非法"
            )));
        }
        // Pos 有时写成小数；ventana.c 把 double 存进 int64_t（向零截断）
        // ——这里在同一位置截断，保证边界盒/落位与其逐位一致
        let x = attr_f64(info, "Pos-X")
            .ok_or_else(|| CoreError::metadata("ImageInfo 缺少 Pos-X"))?
            .trunc();
        let y = attr_f64(info, "Pos-Y")
            .ok_or_else(|| CoreError::metadata("ImageInfo 缺少 Pos-Y"))?
            .trunc();
        raw.push(RawArea { start_col: start_col / tile_w, start_row: start_row / tile_h, tiles_across, tiles_down, x, y });

        for tj in tag_spans(info_body, "TileJointInfo") {
            let (tj_open, _) = tj;
            let direction = attr(tj_open, "Direction").unwrap_or("");
            let t1 = attr_i64(tj_open, "Tile1")
                .ok_or_else(|| CoreError::metadata("TileJointInfo 缺少 Tile1"))?;
            let t2 = attr_i64(tj_open, "Tile2")
                .ok_or_else(|| CoreError::metadata("TileJointInfo 缺少 Tile2"))?;
            let (c1, r1) = tile_coordinates(tiles_across, tiles_down, t1)?;
            let (c2, r2) = tile_coordinates(tiles_across, tiles_down, t2)?;
            let confidence = attr_i64(tj_open, "Confidence")
                .ok_or_else(|| CoreError::metadata("TileJointInfo 缺少 Confidence"))?;
            let overlap_x = attr_f64(tj_open, "OverlapX")
                .ok_or_else(|| CoreError::metadata("TileJointInfo 缺少 OverlapX"))?;
            let overlap_y = attr_f64(tj_open, "OverlapY")
                .ok_or_else(|| CoreError::metadata("TileJointInfo 缺少 OverlapY"))?;
            let direction_y = match direction {
                "RIGHT" => {
                    if !(c2 == c1 + 1 && r2 == r1) {
                        return Err(CoreError::variant(format!(
                            "TileJointInfo Direction=RIGHT 但瓦片 ({c1},{r1})/({c2},{r2}) 不相邻"
                        )));
                    }
                    false
                }
                "UP" => {
                    if !(c2 == c1 && r2 == r1 - 1) {
                        return Err(CoreError::variant(format!(
                            "TileJointInfo Direction=UP 但瓦片 ({c1},{r1})/({c2},{r2}) 不相邻"
                        )));
                    }
                    true
                }
                other => {
                    return Err(CoreError::variant(format!(
                        "TileJointInfo Direction={other:?} 不在支持集（仅 RIGHT/UP；\
                         LEFT/DOWN 走向的 BIF 是 OpenSlide 也不读取的变体，暂按直传处理）"
                    )))
                }
            };
            if direction_y {
                tot_oy += confidence as f64 * (-overlap_y);
                tot_wy += confidence;
            } else {
                tot_ox += confidence as f64 * (-overlap_x);
                tot_wx += confidence;
            }
        }
    }
    if raw.is_empty() {
        return Err(CoreError::variant("EncodeInfo 没有任何 AOIScanned=1 的扫描区域"));
    }
    let adv_x = tile_w as f64 + if tot_wx > 0 { tot_ox / tot_wx as f64 } else { 0.0 };
    let adv_y = tile_h as f64 + if tot_wy > 0 { tot_oy / tot_wy as f64 } else { 0.0 };
    if !(adv_x.is_finite() && adv_y.is_finite()) || adv_x <= 0.0 || adv_y <= 0.0 {
        return Err(CoreError::variant(format!(
            "拼接步进非法（{adv_x:.3}, {adv_y:.3}）：重叠记录损坏"
        )));
    }

    // Pos-Y flip: y' = top - y - height
    let heights: Vec<f64> = raw
        .iter()
        .map(|a| (a.tiles_down as f64 - 1.0) * adv_y + tile_h as f64)
        .collect();
    let top = raw
        .iter()
        .zip(heights.iter())
        .map(|(a, h)| a.y + h)
        .fold(f64::NEG_INFINITY, f64::max);
    if !top.is_finite() {
        return Err(CoreError::variant("EncodeInfo 位置坐标非法"));
    }
    let areas: Vec<BifArea> = raw
        .iter()
        .zip(heights.iter())
        .map(|(a, h)| BifArea {
            start_col: a.start_col,
            start_row: a.start_row,
            tiles_across: a.tiles_across,
            tiles_down: a.tiles_down,
            x: a.x,
            y: top - a.y - h,
        })
        .collect();
    Ok((areas, adv_x, adv_y))
}

// --------------------------------------------------------------------------- //
// probe
// --------------------------------------------------------------------------- //

/// Structural checks of one level IFD (compression/samples/photo/planar/
/// bits/tiled); returns its geometry.
#[allow(clippy::too_many_arguments)]
fn check_level_container(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    idx: usize,
    budget: &mut MemBudget,
) -> CoreResult<(u32, u32, u32, u32, u64, u64)> {
    let compression = match ifd.find(tag::COMPRESSION) {
        Some(e) => tiff_read::entry_u64(src, hdr, e)?,
        None => 0,
    };
    match compression {
        7 => {}
        33003 | 33005 => {
            return Err(CoreError::variant(format!(
                "第 {idx} 页为 JPEG 2000 压缩（{compression}）：BIF 的 JP2K 变体不在支持集（需要独立解码器）"
            )))
        }
        c => {
            return Err(CoreError::variant(format!(
                "第 {idx} 页压缩编码 {c} 不是基线 JPEG（259=7）"
            )))
        }
    }
    let samples = find_i64(src, hdr, ifd, tag::SAMPLES)?.unwrap_or(1);
    if samples != 3 {
        return Err(CoreError::variant(format!(
            "第 {idx} 页 SamplesPerPixel={samples}（灰度/荧光/多通道页组不在明场 BIF 支持集）"
        )));
    }
    let planar = find_i64(src, hdr, ifd, tag::PLANAR)?.unwrap_or(1);
    if planar != 1 {
        return Err(CoreError::variant(format!(
            "第 {idx} 页 PlanarConfiguration={planar}（平面存储）不在支持集"
        )));
    }
    if let Some(e) = ifd.find(tag::BITS) {
        let bits = tiff_read::entry_value(src, hdr, e)?;
        let vals: Vec<u16> = bits
            .chunks_exact(2)
            .map(|c| {
                let v = [c[0], c[1]];
                if hdr.little { u16::from_le_bytes(v) } else { u16::from_be_bytes(v) }
            })
            .collect();
        if vals != [8, 8, 8] {
            return Err(CoreError::variant(format!(
                "第 {idx} 页 BitsPerSample={vals:?} ≠ [8,8,8]"
            )));
        }
    }
    let photo = find_i64(src, hdr, ifd, tag::PHOTO)?.unwrap_or(0);
    if photo != 2 && photo != 6 {
        return Err(CoreError::variant(format!(
            "第 {idx} 页 PhotometricInterpretation={photo} 不在支持集（RGB=2 / YCbCr=6）"
        )));
    }
    let (Some(tw_e), Some(th_e)) = (ifd.find(tag::TILE_WIDTH), ifd.find(tag::TILE_LENGTH)) else {
        return Err(CoreError::variant(format!(
            "第 {idx} 页不是分块（tiled）存储：BIF 层级页必须是瓦片布局"
        )));
    };
    let tile_w = tiff_read::entry_u64(src, hdr, tw_e)?;
    let tile_h = tiff_read::entry_u64(src, hdr, th_e)?;
    if !(1..=MAX_SIDE).contains(&tile_w) || !(1..=MAX_SIDE).contains(&tile_h) {
        return Err(CoreError::variant(format!(
            "第 {idx} 页瓦片尺寸 {tile_w}×{tile_h} 越界"
        )));
    }
    if ifd.find(tag::TILE_OFFSETS).is_none() || ifd.find(tag::TILE_COUNTS).is_none() {
        return Err(CoreError::variant(format!(
            "第 {idx} 页缺少 TileOffsets/TileByteCounts"
        )));
    }
    let width = find_i64(src, hdr, ifd, tag::WIDTH)?.unwrap_or(0);
    let height = find_i64(src, hdr, ifd, tag::HEIGHT)?.unwrap_or(0);
    if !(1..=MAX_SIDE as i64).contains(&width) || !(1..=MAX_SIDE as i64).contains(&height) {
        return Err(CoreError::variant(format!(
            "第 {idx} 页画布尺寸 {width}×{height} 越界"
        )));
    }
    // structural tile-array walk (bounds + zero-count referenced tiles)
    let across = (width as u64).div_ceil(tile_w);
    let down = (height as u64).div_ceil(tile_h);
    let _ = budget;
    Ok((width as u32, height as u32, tile_w as u32, tile_h as u32, across, down))
}

/// Parse a `level=N mag=M` description (ventana.c `parse_level_info`).
fn parse_level_desc(desc: &str) -> CoreResult<(i64, f64)> {
    let mut level = None;
    let mut mag = None;
    for part in desc.trim_matches('\0').split(' ') {
        if let Some((k, v)) = part.split_once('=') {
            if k == "level" {
                level = v.trim().parse::<i64>().ok();
            } else if k == "mag" {
                mag = v.trim().parse::<f64>().ok().filter(|m| m.is_finite());
            }
        }
    }
    match (level, mag) {
        (Some(l), Some(m)) => Ok((l, m)),
        _ => Err(CoreError::metadata(format!(
            "层级描述 {desc:?} 缺少 level=/mag= 字段"
        ))),
    }
}

/// Capability probe with an explicit host memory budget (review §1): the
/// structural walk, the XML payloads and the tile-array scans are charged
/// BEFORE they read/allocate; an over-budget charge is the stable typed
/// refusal `resource_profile_insufficient`.
pub fn probe_bif_with_budget(src: &dyn ByteSource, budget_bytes: u64) -> CoreResult<BifDoc> {
    let mut budget = MemBudget::host(budget_bytes);
    budget.charge(
        (tiff_read::MAX_IFDS * tiff_read::MAX_IFD_ENTRIES * 32) as u64,
        "IFD 链结构预留",
    )?;
    let hdr = tiff_read::read_header(src)?;
    if hdr.kind != TiffKind::BigTiff {
        return Err(CoreError::variant(
            "Ventana BIF 是 BigTIFF（43）容器；经典 TIFF 的 ventana tif 变体不在本适配器支持集",
        ));
    }
    let chain = tiff_read::ifd_chain(src, &hdr)?;
    if chain.len() > MAX_LEVELS + 16 {
        return Err(CoreError::variant(format!(
            "IFD 页数 {} 超出 BIF 布局上限",
            chain.len()
        )));
    }

    // ---- IFD 0 XMLPacket: the iScan vendor block ------------------------ //
    let iscan_xml = tag_bytes(
        src,
        &hdr,
        &chain[0],
        tag::XMLPACKET,
        MAX_XML_BYTES,
        &mut budget,
        "iScan XMLPacket 读取",
    )?
    .ok_or_else(|| {
        CoreError::variant("IFD 0 缺少 XMLPacket（700）：不是 Ventana BIF 输入")
    })?;
    let iscan = String::from_utf8_lossy(&iscan_xml[..iscan_xml.len().min(1 << 20)]).to_string();
    let iscan_block = if tag_spans(&iscan, "iScan").is_empty() {
        // alternate root: <Metadata><iScan …/></Metadata>
        let meta = tag_spans(&iscan, "Metadata");
        if meta.is_empty() {
            return Err(CoreError::variant(
                "XMLPacket 根不是 iScan（也不是 Metadata/iScan）：不是 Ventana BIF 输入",
            ));
        }
        meta.iter()
            .flat_map(|(_, body)| tag_spans(body, "iScan"))
            .map(|(open, _)| open.to_string())
            .next()
            .ok_or_else(|| {
                CoreError::variant("Metadata XML 中没有 iScan 元素：不是 Ventana BIF 输入")
            })?
    } else {
        tag_spans(&iscan, "iScan")[0].0.to_string()
    };
    // multi-z variant: Z-layers > 1
    if attr_i64(&iscan_block, "Z-layers").unwrap_or(1) > 1 {
        return Err(CoreError::variant(format!(
            "iScan Z-layers={}：多 z 层 BIF 变体不在明场支持集（z 轴页组未导出）",
            attr_i64(&iscan_block, "Z-layers").unwrap_or(0)
        )));
    }
    let mpp = attr_f64(&iscan_block, "ScanRes").filter(|v| *v > 0.0).map(|v| (v, v));
    let objective = attr_f64(&iscan_block, "Magnification").filter(|m| *m > 0.0);

    // ---- walk the chain: levels / label / thumbnail / other pages ------- //
    let mut levels: Vec<BifLevel> = Vec::new();
    let mut associated: Vec<AssociatedSummary> = Vec::new();
    let mut next_level: i64 = 0;
    let mut prev_mag = f64::INFINITY;
    let mut level0_xml: Option<String> = None;
    let mut encode_xml_bytes = 0u64;
    for (idx, ifd) in chain.iter().enumerate() {
        let desc = ascii_tag(src, &hdr, ifd, tag::DESCRIPTION)?;
        if desc.contains("level=") {
            let (level, mag) = parse_level_desc(&desc)?;
            if level != next_level {
                return Err(CoreError::variant(format!(
                    "层级序号 {level} 应为 {next_level}：BIF 层级页序损坏"
                )));
            }
            if mag >= prev_mag {
                return Err(CoreError::variant(format!(
                    "层级 {level} mag={mag} 未严格小于上一层级：BIF 层级页序损坏"
                )));
            }
            next_level += 1;
            prev_mag = mag;
            let (canvas_w, canvas_h, tile_w, tile_h, across, down) =
                check_level_container(src, &hdr, ifd, idx, &mut budget)?;
            if let Some(l0) = levels.first() {
                if l0.tile_w != tile_w || l0.tile_h != tile_h {
                    return Err(CoreError::variant(format!(
                        "层级 {level} 瓦片尺寸 {tile_w}×{tile_h} ≠ 层级 0 的 {}×{}：\
                         OpenSlide 同样拒绝的不一致布局",
                        l0.tile_w, l0.tile_h
                    )));
                }
            }
            if levels.len() >= MAX_LEVELS {
                return Err(CoreError::variant(format!("层级数超过 {MAX_LEVELS}")));
            }
            if level == 0 {
                let xml_bytes = tag_bytes(
                    src,
                    &hdr,
                    ifd,
                    tag::XMLPACKET,
                    MAX_XML_BYTES,
                    &mut budget,
                    "EncodeInfo XMLPacket 读取",
                )?;
                match xml_bytes {
                    Some(b) => {
                        encode_xml_bytes = b.len() as u64;
                        // the XML is padded after the closing tag — cut there
                        let s = String::from_utf8_lossy(&b).to_string();
                        level0_xml = Some(match s.rfind("</EncodeInfo>") {
                            Some(p) => s[..p + "</EncodeInfo>".len()].to_string(),
                            None => s,
                        });
                    }
                    None => {
                        return Err(CoreError::variant(
                            "层级 0 没有 EncodeInfo XMLPacket：无 AOI/重叠记录的 \
                             ventana tif 简单网格变体不在支持集",
                        ))
                    }
                }
            }
            levels.push(BifLevel {
                ifd_index: idx as u32,
                canvas_w,
                canvas_h,
                tile_w,
                tile_h,
                tiles_across: across,
                tiles_down: down,
                magnification: mag,
            });
        } else {
            let w = find_i64(src, &hdr, ifd, tag::WIDTH)?.unwrap_or(0) as u32;
            let h = find_i64(src, &hdr, ifd, tag::HEIGHT)?.unwrap_or(0) as u32;
            let name = if desc == "Label Image" || desc == "Label_Image" {
                "label"
            } else if desc == "Thumbnail" {
                "thumbnail"
            } else if desc.is_empty() {
                "associated"
            } else {
                // Probability_Image and other vendor pages: excluded by their
                // own description (lowercased, reported for provenance)
                "associated"
            };
            associated.push(AssociatedSummary {
                name: if desc == "Probability_Image" {
                    "probability".to_string()
                } else {
                    name.to_string()
                },
                source_offset: 0,
                source_length: 0,
                width: w,
                height: h,
            });
        }
    }
    let Some(l0) = levels.first() else {
        return Err(CoreError::variant("BIF 没有任何 level= 层级页"));
    };
    // 单层（只有 level=0）文件照常接受：输出的全部降采样层由 l0-box2 生
    // 成，源层从不解码像素（OpenSlide 同样接受单层 BIF）
    let Some(xml) = level0_xml else { unreachable!("level=0 checked above") };

    // ---- stitch geometry ------------------------------------------------- //
    let (areas, adv_x, adv_y) =
        parse_encode_info(&xml, l0.tile_w as i64, l0.tile_h as i64)?;
    let mut seen_charge = 0u64;
    // 审查（medium）：AOI 网格遍历必须有上限——构造文件（小瓦片 + 大画布
    // + 大 NumCols/NumRows）可让遍历做 ~1e12 次 HashSet 插入。两条硬界都
    // 在任何遍历之前：每个 AOI 的网格必须完整落在 canvas 网格内（算术检
    // 查，不逐格）；AOI 网格总格数 ≤ TileOffsets 实际条目数（每个引用格
    // 都要有一个真实瓦片，且 (col,row) 不得重复 → 总数严格不超）。
    {
        let total_entries = {
            let l0_ifd0 = &chain[l0.ifd_index as usize];
            let Some(oe) = l0_ifd0.find(tag::TILE_OFFSETS) else {
                return Err(CoreError::variant("层级 0 缺少 TileOffsets"));
            };
            oe.count
        };
        let mut total_cells: u64 = 0;
        for a in &areas {
            let end_col = a.start_col.checked_add(a.tiles_across);
            let end_row = a.start_row.checked_add(a.tiles_down);
            let (Some(end_col), Some(end_row)) = (end_col, end_row) else {
                return Err(CoreError::variant("AOI 瓦片数算术溢出：拒绝猜测"));
            };
            if a.start_col < 0
                || a.start_row < 0
                || end_col as u64 > l0.tiles_across
                || end_row as u64 > l0.tiles_down
            {
                return Err(CoreError::variant(format!(
                    "AOI 网格 [{},+{})×[{},+{}) 超出 canvas 瓦片网格 {}×{}：损坏的 EncodeInfo",
                    a.start_col, end_col, a.start_row, end_row, l0.tiles_across, l0.tiles_down
                )));
            }
            total_cells = total_cells
                .saturating_add((a.tiles_across as u64).saturating_mul(a.tiles_down as u64));
        }
        if total_cells > total_entries {
            return Err(CoreError::variant(format!(
                "AOI 网格总格数 {total_cells} 超出 TileOffsets 条目数 {total_entries}：\
                 声明的网格没有对应瓦片，拒绝（遍历前上限）"
            )));
        }
        // HashSet 工作集先计费后分配（review §1；15.3M 格的构造文件是
        // ~1 GB 的 HashSet——必须在分配前按预算拒绝）
        seen_charge = total_cells.saturating_mul(16);
        budget.charge(seen_charge, "AOI 网格去重表")?;
    }
    // bounding box right/bottom edges over every placed tile
    let mut max_x = f64::NEG_INFINITY;
    let mut max_y = f64::NEG_INFINITY;
    let mut seen: std::collections::HashSet<(i64, i64)> = std::collections::HashSet::new();
    let seen_cap: u64 = areas
        .iter()
        .map(|a| (a.tiles_across.max(0) as u64).saturating_mul(a.tiles_down.max(0) as u64))
        .fold(0u64, |acc, v| acc.saturating_add(v))
        .min(1 << 22);
    seen.reserve(seen_cap as usize);
    let mut tiles_present = 0u64;
    for a in &areas {
        for row in a.start_row..a.start_row + a.tiles_down {
            for col in a.start_col..a.start_col + a.tiles_across {
                if !seen.insert((col, row)) {
                    return Err(CoreError::variant(format!(
                        "AOI 瓦片网格在全局 ({col},{row}) 重叠：不明确的拼接布局，拒绝猜测"
                    )));
                }
                if col < 0
                    || row < 0
                    || col as u64 >= l0.tiles_across
                    || row as u64 >= l0.tiles_down
                {
                    return Err(CoreError::variant(format!(
                        "AOI 瓦片 ({col},{row}) 越出 TIFF 瓦片网格 {}×{}",
                        l0.tiles_across, l0.tiles_down
                    )));
                }
                tiles_present += 1;
                // bounds use the UNFLOORED edges (OpenSlide:
                // ceil(x + w) of the grid bounding box)
                let x = a.tile_x_f(col, adv_x) + l0.tile_w as f64;
                let y = a.tile_y_f(row, adv_y) + l0.tile_h as f64;
                max_x = max_x.max(x);
                max_y = max_y.max(y);
            }
        }
    }
    let width = max_x.ceil().max(0.0) as i64;
    let height = max_y.ceil().max(0.0) as i64;
    if !(1..=MAX_SIDE as i64).contains(&width) || !(1..=MAX_SIDE as i64).contains(&height) {
        return Err(CoreError::variant(format!(
            "拼接后尺寸 {width}×{height} 越界"
        )));
    }

    // ---- tile payload truth probe (first referenced tile of L0) --------- //
    let jpeg_tables = tag_bytes(
        src,
        &hdr,
        &chain[l0.ifd_index as usize],
        tag::JPEGTABLES,
        1 << 20,
        &mut budget,
        "JPEGTables 读取",
    )?;
    let l0_ifd = &chain[l0.ifd_index as usize];
    let first_area = &areas[0];
    let first_idx = (first_area.start_row.max(0) as u64) * l0.tiles_across
        + first_area.start_col.max(0) as u64;
    let mut cur = tiff_read::TileCursor::new(src, &hdr, l0_ifd)?;
    let mut pair = None;
    for _ in 0..=first_idx {
        pair = cur.next_pair_allow_zero()?;
    }
    let (off, cnt) = pair.ok_or_else(|| CoreError::validation("首个 AOI 瓦片缺失"))?;
    if off == 0 || cnt == 0 {
        return Err(CoreError::variant(format!(
            "首个 AOI 瓦片（tile {first_idx}）无载荷：稀疏 BIF 变体不在支持集"
        )));
    }
    budget.charge(cnt.min(PROBE_LIMIT), "首个瓦片 JPEG 头探测读取")?;
    let head = src.read_at(off, cnt.min(PROBE_LIMIT) as usize)?;
    budget.release(cnt.min(PROBE_LIMIT));
    let merged;
    let stream: &[u8] = match &jpeg_tables {
        Some(t) => {
            merged = crate::svs::merge_tables_then_tile(t, &head);
            &merged
        }
        None => &head,
    };
    let probe = jpeg::scan_jpeg(stream)
        .map_err(|e| CoreError::jpeg(format!("层级 0 瓦片 JPEG：{}", e.message)))?;
    if (probe.width, probe.height) != (l0.tile_w, l0.tile_h) {
        return Err(CoreError::variant(format!(
            "层级 0 瓦片 SOF 尺寸 {}×{} ≠ TIFF 瓦片 {}×{}",
            probe.width, probe.height, l0.tile_w, l0.tile_h
        )));
    }
    let sampling = probe.sampling.ok_or_else(|| {
        CoreError::variant("层级 0 瓦片 JPEG 不是三分量（灰度不在明场支持集）")
    })?;
    let (h1, v1, h2, v2, h3, v3) = sampling;
    let mcu_w = 8 * h1.max(h2).max(h3) as u32;
    let mcu_h = 8 * v1.max(v2).max(v3) as u32;
    let color = jpeg::tiff_jpeg_color(
        &probe,
        find_i64(src, &hdr, l0_ifd, tag::PHOTO)?.map(|x| x as u64).unwrap_or(6),
    );

    // ---- payload bytes of the referenced tiles (estimate input) --------- //
    let mut payload_bytes = 0u64;
    {
        let mut cur = tiff_read::TileCursor::new(src, &hdr, l0_ifd)?;
        let mut idx = 0u64;
        while let Some(p) = cur.next_pair_allow_zero()? {
            if seen_contains(&seen, idx, l0.tiles_across) {
                if p.1 == 0 {
                    return Err(CoreError::variant(format!(
                        "引用瓦片 {idx} 无载荷（TileByteCounts=0）：稀疏 BIF 变体不在支持集"
                    )));
                }
                payload_bytes += p.1;
            }
            idx += 1;
        }
    }

    let icc = match chain[0].find(tag::ICC) {
        Some(e) if e.value_len().unwrap_or(u64::MAX) <= 4 << 20 => {
            // ICC 随产物携带（保留在 doc 里直到写出）：先计费后读取
            let n = e.value_len().unwrap_or(0);
            budget.charge(n, "ICC profile 读取")?;
            Some(tiff_read::entry_value(src, &hdr, e)?)
        }
        _ => None,
    };
    let generated = crate::gtiff::generated_tail(width as u32, height as u32);
    drop(seen);
    budget.release(seen_charge);
    budget.release(iscan_xml.len() as u64);
    budget.release(encode_xml_bytes);
    Ok(BifDoc {
        kind: hdr.kind,
        levels,
        width: width as u32,
        height: height as u32,
        areas,
        advance_x: adv_x,
        advance_y: adv_y,
        mpp,
        objective,
        icc,
        jpeg_tables,
        color,
        sampling,
        mcu: (mcu_w, mcu_h),
        tiles_present,
        payload_bytes,
        associated,
        generated,
        budget,
    })
}

/// Synthesize a self-contained JPEG header for one abbreviated tile stream
/// (shared JPEGTables + the tile's own SOF/SOS markers): returns
/// (header bytes [SOI … SOS end], entropy-data file offset). The
/// BandScanner is then opened with `head_offset = entropy − header.len()`
/// so its internal `scan_pos` lands exactly on the entropy data.
pub fn tile_header_synthetic(
    tables: Option<&[u8]>,
    head: &[u8],
    tile_off: u64,
) -> CoreResult<(Vec<u8>, u64)> {
    if head.len() < 4 || head[0..2] != [0xFF, 0xD8] {
        return Err(CoreError::jpeg("瓦片流缺少 SOI"));
    }
    let mut i = 2usize;
    let mut sos_end = 0usize;
    while i + 4 <= head.len() {
        if head[i] != 0xFF {
            return Err(CoreError::jpeg("瓦片标记流错位"));
        }
        while i < head.len() && head[i] == 0xFF {
            i += 1;
        }
        if i >= head.len() {
            break;
        }
        let marker = head[i];
        i += 1;
        if marker == 0xD9 || marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            continue;
        }
        if i + 2 > head.len() {
            return Err(CoreError::jpeg("瓦片标记段截断"));
        }
        let seg_len = ((head[i] as usize) << 8) | head[i + 1] as usize;
        if seg_len < 2 || i + seg_len > head.len() {
            return Err(CoreError::jpeg("瓦片段长度非法"));
        }
        if marker == 0xDA {
            sos_end = i + seg_len;
            break;
        }
        i += seg_len;
    }
    if sos_end == 0 {
        return Err(CoreError::jpeg("瓦片流缺少 SOS"));
    }
    let mut hdr = Vec::with_capacity(sos_end + tables.map_or(0, <[u8]>::len));
    match tables {
        Some(t) => {
            // tables minus EOI + tile minus SOI (the merge convention)
            hdr.extend_from_slice(&t[..t.len().saturating_sub(2)]);
            hdr.extend_from_slice(&head[2.min(head.len())..sos_end]);
        }
        None => hdr.extend_from_slice(&head[..sos_end]),
    }
    Ok((hdr, tile_off + sos_end as u64))
}

/// Is TIFF tile index `idx` referenced by a scanned AOI?
fn seen_contains(seen: &std::collections::HashSet<(i64, i64)>, idx: u64, across: u64) -> bool {
    seen.contains(&((idx % across) as i64, (idx / across) as i64))
}

/// Capability probe at the conservative saver budget (CLI default).
pub fn probe_bif(src: &dyn ByteSource) -> CoreResult<BifDoc> {
    probe_bif_with_budget(src, crate::budget::SAVER_BUDGET_BYTES)
}

// --------------------------------------------------------------------------- //
// estimate
// --------------------------------------------------------------------------- //

/// Disk-precheck estimate for the BIF adapter. 输出 tile 全部按「保留画质」
/// 重编码，引用瓦片载荷字节只是像素代理：公开样本（OS-2.bif，q90 4:2:2
/// 源，引用载荷 2,112,442,968 B）的 preserve 实测输出 4,083,541,625 B ≈
/// 1.93× 载荷，4× 上界覆盖（余量 ≈2.07×）；compact 按 1.5×（NDPI 实测
/// 1.10× 同族）。病态低画质源仍可能超出倍数外推——运行时输出上限与逐写
/// 盘检查是最后的硬闸（engine.js 磁盘闸取这里的安全上界）。
pub fn estimate_bif(doc: &BifDoc) -> crate::estimate::OutputEstimate {
    let payload = doc.payload_bytes;
    let l0_tiles = (doc.width as u64).div_ceil(OUT_TILE as u64)
        * (doc.height as u64).div_ceil(OUT_TILE as u64);
    let gen_tiles: u64 = doc
        .generated
        .iter()
        .map(|(w, h)| {
            (*w as u64).div_ceil(OUT_TILE as u64) * (*h as u64).div_ceil(OUT_TILE as u64)
        })
        .sum();
    let tiles = l0_tiles + gen_tiles;
    let ifds = (1 + doc.generated.len()) as u64;
    let base = tiles
        .saturating_mul(16)
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(1024 * 1024);
    crate::estimate::OutputEstimate {
        payload_bytes: payload,
        tiles_present: tiles,
        cells_total: tiles,
        cells_missing: 0,
        edge_tiles: 0, // every tile is a full 256×256 canvas re-encode
        ifds,
        output_upper_bound_bytes: payload
            .saturating_mul(4)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
        compact_upper_bound_bytes: payload
            .saturating_mul(3)
            .div_ceil(2)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
    }
}
