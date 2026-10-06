//! 普通图片（BMP/JPEG）输入适配器 (F8): one plain image, no slide container.
//!
//! File model (bounded reads only; the whole image is never buffered):
//!
//! - **BMP**: `BM` magic; `BITMAPCOREHEADER` (12 B, OS/2 1.x) or
//!   `BITMAPINFOHEADER`/V4/V5 (40/108/124 B); uncompressed BI_RGB only,
//!   24 or 32 bpp. Every other variant (RLE8/4, BITFIELDS, JPEG/PNG-in-BMP,
//!   1/4/8/16 bpp, unknown DIB sizes) is a typed rejection. Rows are read
//!   one at a time (each row's length is exact from the header) — bottom-up
//!   by default, top-down when the InfoHeader height is negative; 24 bpp is
//!   BGR (channel-swapped), 32 bpp BGRA with the (BI_RGB: undefined) alpha
//!   dropped. OS/2 core-header rows pad to 2 bytes, Windows rows to 4.
//! - **JPEG baseline**: SOI … SOF0/1 … SOS. Progressive / hierarchical /
//!   lossless / arithmetic SOFs are typed rejections before any decode
//!   (marker walk). WITH restart markers (DRI > 0) the scan decodes restart
//!   segment by restart segment ([`crate::segment::SegmentReader`] — each
//!   segment is an independent MCU run with reset DC predictors); without
//!   restarts there is no byte-aligned decode unit, so the scan decodes MCU
//!   row by MCU row ([`crate::jpeg::band::BandScanner`] — the only bounded
//!   unit a restart-less scan has). Grayscale (1-component) JPEG is a typed
//!   rejection: the output is RGB brightfield. Multi-segment ICC PROFILE
//!   APP2 payloads within the bounded probe head are carried to the output.
//! - No physical scale exists in either container: MPP/objective stay
//!   unknown and the OME output deliberately writes NO PhysicalSize (an
//!   invented µm/px would be a fabricated measurement).
//! - Pixel caps: either side ≤ [`MAX_SIDE`], total pixels ≤ [`MAX_PIXELS`]
//!   — refused at the probe (the engine sniff applies the same numbers
//!   BEFORE the copy is staged).
//! - Output pyramid: level 0 re-encoded into 256 px tiles, reduced output
//!   levels are the `l0-box2` chain (2×2 area-average of output level 0).

use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "plain-image-bmp-jpeg";
/// Adapter version (bump on any output-affecting change). Enforced in the
/// core's resume entry: a checkpoint that names another generation — or
/// carries no version at all — is refused (SCN/NDPI parity).
pub const ADAPTER_VERSION: &str = "1";

/// How reduced output levels are built (MRXS v2 / generic-TIFF method id).
pub const PYRAMID_METHOD: &str = "l0-box2";
/// Locked preserve-mode compose parameters (the documented high-fidelity
/// family: same values as the MRXS compose / NDPI segment compose).
pub const PRESERVE_COMPOSE_QUALITY: u8 = 96;
pub const PRESERVE_COMPOSE_SAMPLING: jpeg::Sampling = jpeg::Sampling::S422;
pub const PRESERVE_COMPOSE_HUFFMAN: &str = "standard-annex-k";
/// Versioned fingerprint of the compose encode parameters.
pub const PRESERVE_COMPOSE_FINGERPRINT: &str = "raster-compose:q96:y422:hstd:v1";

/// Output-tile edge of the composed pyramid.
pub const OUT_TILE: u32 = 256;
/// Sane geometric caps (per side).
pub const MAX_SIDE: u64 = 1_000_000;
/// Input pixel-count cap (refused at the probe AND in the engine sniff
/// before the copy is staged).
pub const MAX_PIXELS: u64 = 1 << 32;
/// Cap of a single probe payload read (the JPEG marker walk needs only the
/// markers up to SOS plus an inline ICC; a header past this cap is a typed
/// rejection).
pub const PROBE_LIMIT: u64 = 256 * 1024;
/// Restart interval cap (MCUs per restart segment; same bound as NDPI).
pub const MAX_RESTART_INTERVAL: u64 = 1 << 24;
/// ICC payload cap (multi-segment APP2, within the probe head).
pub const MAX_ICC_BYTES: u64 = 1 << 20;
/// BMP head bound (BM + BITMAPV5INFO = 14 + 124).
pub const BMP_HEAD_BYTES: u64 = 138;

/// Detected container family.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RasterKind {
    /// Uncompressed 24-bit BMP (BGR rows).
    Bmp24,
    /// Uncompressed 32-bit BMP (BGRA rows; the undefined BI_RGB alpha is
    /// dropped).
    Bmp32,
    /// Baseline JPEG (SOI…SOF0/1…SOS), decoded restart-segment- or
    /// MCU-row-bounded.
    JpegBaseline,
}

impl RasterKind {
    pub fn id(self) -> &'static str {
        match self {
            RasterKind::Bmp24 => "bmp-24",
            RasterKind::Bmp32 => "bmp-32",
            RasterKind::JpegBaseline => "jpeg-baseline",
        }
    }
}

/// BMP row geometry resolved from the header.
#[derive(Debug, Clone)]
pub struct BmpLayout {
    /// File offset of the first (bottom-most for bottom-up) pixel row.
    pub data_offset: u64,
    /// Stride in bytes (width×bpp/8 padded to 2 (OS/2) / 4 (Windows)).
    pub row_bytes: u64,
    /// true = rows stored top-down (negative InfoHeader height).
    pub top_down: bool,
    /// DIB header size (12 core / 40 info / 108 V4 / 124 V5).
    pub dib_header: u32,
}

/// JPEG scan geometry (whole file = the strip; the head is the sub-JPEG
/// header source).
#[derive(Debug, Clone)]
pub struct RasterJpeg {
    /// True colorspace from the stream's markers (JFIF/Adobe rules).
    pub color: jpeg::TiffJpegColor,
    /// SOF sampling (h1,v1,h2,v2,h3,v3).
    pub sampling: (u8, u8, u8, u8, u8, u8),
    /// MCU size in pixels (8·h_max, 8·v_max).
    pub mcu: (u32, u32),
    pub mcus_x: u32,
    pub mcus_y: u32,
    pub total_mcus: u64,
    /// Restart interval in MCUs (DRI); 0 = the MCU-row band path.
    pub restart_interval: u32,
    /// Restart segments (DRI > 0) or 0 (band path).
    pub segments: u64,
    /// Head bytes [SOI … SOS segment] (sub-JPEG header source; band path)
    /// and their file offset (always 0 — the head starts the file).
    pub header_bytes: Vec<u8>,
    pub header_offset: u64,
    /// Offset of the SOF height field inside `header_bytes` (width at +2).
    pub sof_hw_at: usize,
    /// [start, end) of the DRI marker segment inside `header_bytes`.
    pub dri_at: Option<(usize, usize)>,
}

/// Probe result: one plain image ready for the tile compose.
#[derive(Debug, Clone)]
pub struct RasterDoc {
    pub kind: RasterKind,
    pub width: u32,
    pub height: u32,
    pub bmp: Option<BmpLayout>,
    pub jpeg: Option<RasterJpeg>,
    /// ICC profile bytes carried to the output (JPEG APP2; BMP: none — the
    /// V4/V5 embedded profile is not extracted).
    pub icc: Option<Vec<u8>>,
    /// Generated tail levels (width, height) — the `l0-box2` chain.
    pub generated: Vec<(u32, u32)>,
    /// Memory account carried from the probe into the conversion (review
    /// §1): the converter keeps charging band/decode working sets against
    /// the same host budget.
    pub budget: MemBudget,
}

fn too_big(width: u64, height: u64) -> CoreError {
    CoreError::variant(format!(
        "图片尺寸 {width}×{height} 超出普通图片支持上限（每边 ≤ {MAX_SIDE}，总数 ≤ {MAX_PIXELS} 像素）：复制前拒绝"
    ))
}

// --------------------------------------------------------------------------- //
// BMP probe
// --------------------------------------------------------------------------- //

fn le_u16(b: &[u8], at: usize) -> u64 {
    u16::from_le_bytes([b[at], b[at + 1]]) as u64
}
fn le_u32(b: &[u8], at: usize) -> u64 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]]) as u64
}
fn le_i32(b: &[u8], at: usize) -> i64 {
    i32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]]) as i64
}

/// Reject the known non-BI_RGB compression codes with per-variant reasons.
fn reject_compression(code: u64, idx: &str) -> CoreError {
    let why = match code {
        1 | 2 => "RLE 行程编码（BI_RLE8/BI_RLE4）",
        3 => "BITFIELDS 位域掩码（BI_BITFIELDS）",
        4 => "JPEG-in-BMP（BI_JPEG）",
        5 => "PNG-in-BMP（BI_PNG）",
        6 => "ALPHA 位域（BI_ALPHABITFIELDS）",
        _ => "未登记的压缩编码",
    };
    CoreError::variant(format!("BMP {idx}压缩编码 {code}（{why}）不在支持集：只支持未压缩 BI_RGB"))
}

fn probe_bmp(src: &dyn ByteSource, budget: &mut MemBudget) -> CoreResult<RasterDoc> {
    // 头读取有界：最多 BMP_HEAD_BYTES（小文件按实际大小读——合法的
    // InfoHeader BMP 总长可以小于 138 字节）
    let want = BMP_HEAD_BYTES.min(src.size());
    budget.charge(want, "BMP 头探测读取")?;
    let head = match src.read_at(0, want as usize) {
        Ok(b) => b,
        Err(e) => {
            budget.release(want);
            return Err(e);
        }
    };
    budget.release(want);
    if head.len() < 34 {
        return Err(CoreError::header("BMP 头不完整（< 34 字节）"));
    }
    let size = src.size();
    let data_offset = le_u32(&head, 10);
    let dib = le_u32(&head, 14);
    // (width, height, top_down, bpp, row_align, header_kind)
    let parsed: (u64, u64, bool, u64, u64, u32) = match dib {
        12 => {
            if head.len() < 26 {
                return Err(CoreError::header("BITMAPCOREHEADER 不完整"));
            }
            let bpp = le_u16(&head, 24);
            if bpp != 24 && bpp != 32 {
                return Err(CoreError::variant(format!(
                    "OS/2 BITMAPCOREHEADER 位深 {bpp} 不在支持集（只支持未压缩 24/32 位）"
                )));
            }
            if le_u16(&head, 22) != 1 {
                return Err(CoreError::variant("BMP 颜色平面数（planes）≠ 1：不是合法 BMP 布局"));
            }
            (
                le_u16(&head, 18),
                le_u16(&head, 20),
                false,
                bpp,
                2, // OS/2 v1 rows pad to 2 bytes
                12,
            )
        }
        40 | 52 | 56 | 108 | 124 => {
            if head.len() < 34 {
                return Err(CoreError::header("BITMAPINFOHEADER 不完整"));
            }
            let raw_h = le_i32(&head, 22);
            let top_down = raw_h < 0;
            let height = raw_h.unsigned_abs();
            let bpp = le_u16(&head, 28);
            if bpp != 24 && bpp != 32 {
                return Err(CoreError::variant(format!(
                    "BMP 位深 {bpp} 不在支持集（只支持未压缩 24/32 位；调色板/16 位/灰度变体请先转成 24/32 位 BMP）"
                )));
            }
            if le_u16(&head, 26) != 1 {
                return Err(CoreError::variant("BMP 颜色平面数（planes）≠ 1：不是合法 BMP 布局"));
            }
            let compression = le_u32(&head, 30);
            if compression != 0 {
                return Err(reject_compression(compression, "头"));
            }
            (le_u32(&head, 18), height, top_down, bpp, 4, dib as u32)
        }
        other => {
            return Err(CoreError::variant(format!(
                "未知 DIB 头尺寸 {other}：不是 BITMAPCOREHEADER/BITMAPINFOHEADER/V4/V5 BMP 变体"
            )))
        }
    };
    let (width, height, top_down, bpp, align, dib_header) = parsed;
    if width == 0 || height == 0 {
        return Err(CoreError::variant("BMP 宽/高为 0：不是合法图片"));
    }
    if width > MAX_SIDE || height > MAX_SIDE {
        return Err(too_big(width, height));
    }
    if width.checked_mul(height).unwrap_or(u64::MAX) > MAX_PIXELS {
        return Err(too_big(width, height));
    }
    let row_bytes = (width * bpp.div_ceil(8) + align - 1) / align * align;
    let px_end = data_offset
        .checked_add(row_bytes.checked_mul(height).ok_or_else(|| CoreError::oob("BMP 行区间溢出"))?)
        .ok_or_else(|| CoreError::oob("BMP 像素区间溢出"))?;
    if px_end > size {
        return Err(CoreError::oob(format!(
            "BMP 像素区间 [{data_offset},+{}) 超出文件 {size} 字节（像素数据截断）",
            row_bytes * height
        )));
    }
    if data_offset < 14 + dib as u64 {
        return Err(CoreError::variant(format!(
            "BMP 像素数据偏移 {data_offset} 落在 DIB 头内：不是合法布局"
        )));
    }
    let kind = if bpp == 24 { RasterKind::Bmp24 } else { RasterKind::Bmp32 };
    Ok(RasterDoc {
        kind,
        width: width as u32,
        height: height as u32,
        bmp: Some(BmpLayout { data_offset, row_bytes, top_down, dib_header }),
        jpeg: None,
        icc: None,
        generated: crate::gtiff::generated_tail(width as u32, height as u32),
        budget: MemBudget::with_cap(0), // replaced by the probe entry point
    })
}

// --------------------------------------------------------------------------- //
// JPEG probe
// --------------------------------------------------------------------------- //

/// Extract a multi-segment ICC PROFILE payload (APP2) from the head bytes.
/// Bounded: segments must lie inside `head`; total ≤ `MAX_ICC_BYTES`.
fn extract_icc(head: &[u8]) -> Option<Vec<u8>> {
    let mut i = 2usize;
    let mut parts: Vec<(u8, &[u8])> = Vec::new();
    let mut total = 0usize;
    while i + 4 <= head.len() {
        if head[i] != 0xFF {
            break;
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
            break;
        }
        let seg_len = ((head[i] as usize) << 8) | head[i + 1] as usize;
        if seg_len < 2 || i + seg_len > head.len() {
            break; // ICC hunt only: a truncated tail just ends the walk
        }
        let seg = &head[i + 2..i + seg_len];
        if marker == 0xE2 && seg.len() > 14 && &seg[0..11] == b"ICC_PROFILE\0" {
            let part = &seg[14..];
            if total + part.len() > MAX_ICC_BYTES as usize {
                return None; // absurd profile: ignore rather than carry
            }
            parts.push((seg[12], part));
            total += part.len();
        }
        if marker == 0xDA {
            break;
        }
        i += seg_len;
    }
    if parts.is_empty() {
        return None;
    }
    parts.sort_by_key(|(seq, _)| *seq);
    // duplicate sequence numbers → malformed profile; refuse to guess
    for w in parts.windows(2) {
        if w[0].0 == w[1].0 {
            return None;
        }
    }
    let mut out = Vec::with_capacity(total);
    for (_, p) in parts {
        out.extend_from_slice(p);
    }
    Some(out)
}

fn probe_jpeg(src: &dyn ByteSource, budget: &mut MemBudget) -> CoreResult<RasterDoc> {
    let size = src.size();
    let head_len = PROBE_LIMIT.min(size) as usize;
    budget.charge(head_len as u64, "JPEG 头探测读取")?;
    let head = match src.read_at(0, head_len) {
        Ok(b) => b,
        Err(e) => {
            budget.release(head_len as u64);
            return Err(e);
        }
    };
    budget.release(head_len as u64);
    // the NDPI strip-head walk rejects progressive/lossless/arithmetic SOFs
    // and locates SOF/DRI/SOS — the exact marker contract of a plain JPEG
    let sh = crate::ndpi::scan_strip_head(&head)
        .map_err(|e| CoreError::jpeg(format!("JPEG 头：{}", e.message)))?;
    if sh.sampling.is_none() {
        return Err(CoreError::variant(
            "JPEG 是单分量（灰度）：普通图片转换输出 RGB 明场，灰度流不在支持集",
        ));
    }
    if sh.width as u64 > MAX_SIDE || sh.height as u64 > MAX_SIDE {
        return Err(too_big(sh.width as u64, sh.height as u64));
    }
    if sh.width as u64 * sh.height as u64 > MAX_PIXELS {
        return Err(too_big(sh.width as u64, sh.height as u64));
    }
    if sh.restart_interval as u64 > MAX_RESTART_INTERVAL {
        return Err(CoreError::variant(format!(
            "JPEG restart interval {} 越界（> {MAX_RESTART_INTERVAL}）",
            sh.restart_interval
        )));
    }
    let (h1, v1, h2, v2, h3, v3) = sh.sampling.unwrap();
    let hmax = h1.max(h2).max(h3) as u32;
    let vmax = v1.max(v2).max(v3) as u32;
    let mcu = (8 * hmax, 8 * vmax);
    let mcus_x = sh.width.div_ceil(mcu.0);
    let mcus_y = sh.height.div_ceil(mcu.1);
    let total_mcus = mcus_x as u64 * mcus_y as u64;
    let segments = if sh.restart_interval > 0 {
        total_mcus.div_ceil(sh.restart_interval as u64)
    } else {
        0
    };
    let probe = jpeg::JpegProbe {
        width: sh.width,
        height: sh.height,
        sampling: sh.sampling,
        comp_ids: None,
        jfif: sh.jfif,
        adobe_transform: sh.adobe_transform,
        sof_marker: sh.sof_marker,
    };
    let color = jpeg::tiff_jpeg_color(&probe, 6);
    // carried ICC: charged against the budget (it travels into the output)
    let icc = extract_icc(&head);
    if let Some(p) = &icc {
        budget.charge(p.len() as u64, "ICC profile 载荷（随输出携带）")?;
    }
    budget.charge(sh.header_len as u64, "JPEG 头字节（随文档携带）")?;
    Ok(RasterDoc {
        kind: RasterKind::JpegBaseline,
        width: sh.width,
        height: sh.height,
        bmp: None,
        jpeg: Some(RasterJpeg {
            color,
            sampling: sh.sampling.unwrap(),
            mcu,
            mcus_x,
            mcus_y,
            total_mcus,
            restart_interval: sh.restart_interval,
            segments,
            header_bytes: head[..sh.header_len].to_vec(),
            header_offset: 0,
            sof_hw_at: sh.sof_hw_at,
            dri_at: sh.dri_at,
        }),
        icc,
        generated: crate::gtiff::generated_tail(sh.width, sh.height),
        budget: MemBudget::with_cap(0), // replaced by the probe entry point
    })
}

// --------------------------------------------------------------------------- //
// probe entry points
// --------------------------------------------------------------------------- //

fn is_bmp_magic(m: &[u8]) -> bool {
    m.len() >= 2 && m[0] == b'B' && m[1] == b'M'
}
fn is_jpeg_magic(m: &[u8]) -> bool {
    m.len() >= 3 && m[0] == 0xFF && m[1] == 0xD8 && m[2] == 0xFF
}

/// Magic gate shared with the host routers (CLI/wasm): plain-image magics
/// ONLY — a TIFF/BigTIFF, KFB or anything else is not this adapter's input.
pub fn is_raster_magic(m: &[u8; 8]) -> bool {
    is_bmp_magic(m) || is_jpeg_magic(m)
}

/// Capability probe with an explicit host memory budget (review §1): the
/// head reads and the carried payloads are charged BEFORE they are
/// read/allocated; an over-budget charge is the stable typed refusal
/// `resource_profile_insufficient`.
pub fn probe_raster_with_budget(src: &dyn ByteSource, budget_bytes: u64) -> CoreResult<RasterDoc> {
    let mut budget = MemBudget::host(budget_bytes);
    let magic = src.read_at(0, 8)?;
    let mut doc = if is_bmp_magic(&magic) {
        probe_bmp(src, &mut budget)?
    } else if is_jpeg_magic(&magic) {
        probe_jpeg(src, &mut budget)?
    } else {
        return Err(CoreError::variant(
            "不是 BMP/JPEG（魔数不符）：普通图片适配器只接受 BMP（BM）与基线 JPEG（FF D8 FF）",
        ));
    };
    doc.budget = budget;
    Ok(doc)
}

/// Capability probe at the conservative saver budget (CLI default).
pub fn probe_raster(src: &dyn ByteSource) -> CoreResult<RasterDoc> {
    probe_raster_with_budget(src, crate::budget::SAVER_BUDGET_BYTES)
}

// --------------------------------------------------------------------------- //
// estimate
// --------------------------------------------------------------------------- //

/// Disk-precheck estimate for the raster adapter. Both output profiles
/// re-encode EVERY tile, so the source byte count is only a weak proxy
/// (a BMP is 3 B/px raw while the q96 4:2:2 output is well under 1 B/px; a
/// low-quality JPEG can sit far below its re-encode). The bounds are
/// therefore PIXEL-based: preserve ≤ ~2 B/px measured on noisy q96 4:2:2
/// content → 3× per-pixel ceiling; compact (q80 4:2:0) ≤ ~1 B/px → 1.5×.
/// The runtime output cap and the per-write quota checks remain the hard
/// guards (engine.js 磁盘闸取这里的安全上界).
pub fn estimate_raster(doc: &RasterDoc, source_bytes: u64) -> crate::estimate::OutputEstimate {
    let (w, h) = (doc.width as u64, doc.height as u64);
    let l0_tiles = w.div_ceil(OUT_TILE as u64) * h.div_ceil(OUT_TILE as u64);
    let gen_tiles: u64 = doc
        .generated
        .iter()
        .map(|(gw, gh)| {
            (*gw as u64).div_ceil(OUT_TILE as u64) * (*gh as u64).div_ceil(OUT_TILE as u64)
        })
        .sum();
    let tiles = l0_tiles + gen_tiles;
    let ifds = (1 + doc.generated.len()) as u64;
    let pixels = w.saturating_mul(h);
    let base = tiles
        .saturating_mul(16)
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(1024 * 1024);
    let _ = source_bytes; // reported as the payload proxy
    crate::estimate::OutputEstimate {
        payload_bytes: source_bytes,
        tiles_present: tiles,
        cells_total: tiles,
        cells_missing: 0,
        edge_tiles: 0, // every tile is a full 256×256 canvas re-encode
        ifds,
        output_upper_bound_bytes: pixels
            .saturating_mul(3)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
        compact_upper_bound_bytes: pixels
            .saturating_mul(3)
            .div_ceil(2)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
    }
}
