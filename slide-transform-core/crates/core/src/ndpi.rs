//! Hamamatsu NDPI input adapter (F6): brightfield whole-layer JPEG strips
//! inside a classic TIFF with vendor tags.
//!
//! File model (bounded reads only; the whole strip is never buffered):
//!
//! - classic TIFF (either byte order; a BigTIFF container is a typed
//!   rejection — NDPI is always classic), IFD 0 is the main image, vendor
//!   identified by `Make` (271) = Hamamatsu (IFD 0 usually has NO
//!   ImageDescription at all, so the vendor sniff falls through to Make);
//! - every page carries the vendor `SourceLens` tag (65421, float):
//!   `> 0` is a pyramid level (the objective power — 20 / 5 / 1.25 / …),
//!   `-1` is the macro image, `-2` the focus map, any other `≤ 0` value an
//!   unknown associated page. Macro/focus-map/associated pages are detected
//!   and NOT exported (main-image conversion, not a source archive);
//!   a z-stack page (`focal plane` tag 65424 ≠ 0) is excluded the same way;
//! - each level is ONE strip (RowsPerStrip = ImageLength, StripOffsets/
//!   StripByteCounts count = 1) holding a complete baseline JPEG of the
//!   whole layer. >4 GiB files store 64-bit values as a LONG plus a
//!   per-entry 4-byte extension word written after the IFD's next pointer
//!   (the same non-standard layout OpenSlide reads) — both words are
//!   combined here, so strips beyond 2^32 resolve correctly;
//! - the layer JPEG must carry restart markers (DRI > 0): the conversion
//!   decodes it restart-segment by restart-segment (each segment is an
//!   independent MCU run with reset DC predictors) and re-encodes output
//!   tiles — a strip without restarts has no bounded decode unit and is a
//!   typed rejection. Progressive / arithmetic / JPEG2000 variants are
//!   typed rejections before any decode;
//! - level sequence: strictly decreasing dimensions with a 1.5–8× step,
//!   same rule as the SVS adapter; MPP from the vendor tags 65441/65442
//!   when present (older NDP..scan files carry none — MPP stays unknown,
//!   nothing is invented from the objective);
//! - output pyramid: level 0 re-encoded from the source segments, reduced
//!   output levels are the `l0-box2` chain (the 2×2 area-average of output
//!   level 0 — the source's own reduced layers are not used for pixels,
//!   exactly like the MRXS v2 / generic-TIFF adapters).

use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;
use crate::report::AssociatedSummary;
use crate::tiff_read::{self, Ifd, TiffHeader, TiffKind};

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "hamamatsu-ndpi-jpeg";
/// Adapter version (bump on any output-affecting change). Enforced in the
/// core's resume entry: a checkpoint that names another generation — or
/// carries no version at all — is refused (SCN/gtiff parity).
pub const ADAPTER_VERSION: &str = "1";

/// How reduced output levels are built (MRXS v2 / generic-TIFF method id).
pub const PYRAMID_METHOD: &str = "l0-box2";
/// Locked preserve-mode compose parameters (the documented high-fidelity
/// family: same values as the MRXS compose / generic-TIFF generated tiles).
pub const PRESERVE_COMPOSE_QUALITY: u8 = 96;
pub const PRESERVE_COMPOSE_SAMPLING: jpeg::Sampling = jpeg::Sampling::S422;
pub const PRESERVE_COMPOSE_HUFFMAN: &str = "standard-annex-k";
/// Versioned fingerprint of the compose encode parameters.
pub const PRESERVE_COMPOSE_FINGERPRINT: &str = "ndpi-segment-compose:q96:y422:hstd:v1";

/// TIFF tags used here.
mod tag {
    pub const WIDTH: u16 = 256;
    pub const HEIGHT: u16 = 257;
    pub const BITS: u16 = 258;
    pub const COMPRESSION: u16 = 259;
    pub const PHOTO: u16 = 262;
    pub const MAKE: u16 = 271;
    pub const STRIP_OFFSETS: u16 = 273;
    pub const SAMPLES: u16 = 277;
    pub const ROWS_PER_STRIP: u16 = 278;
    pub const STRIP_COUNTS: u16 = 279;
    pub const PLANAR: u16 = 284;
    pub const EXTRA_SAMPLES: u16 = 338;
    pub const ICC: u16 = 34675;
    /// vendor: SourceLens — float; >0 levels, -1 macro, -2 focus map
    pub const SOURCELENS: u16 = 65421;
    /// vendor: focal plane (z-stack pages, ≠ 0 → excluded)
    pub const FOCAL_PLANE: u16 = 65424;
    /// vendor: µm/px (double/float), absent on early NDP.scan files
    pub const MPP_X: u16 = 65441;
    pub const MPP_Y: u16 = 65442;
}

/// Sane geometric caps (a real NanoZoomer level 0 is ≲ 120 000 px per side).
const MAX_SIDE: u64 = 1_000_000;
const MAX_LEVELS: usize = 32;
/// Cap of a single probe payload read (the strip header scan needs only the
/// markers up to SOS; a header past this cap is a typed rejection).
pub const PROBE_LIMIT: u64 = 256 * 1024;
/// Restart interval cap (MCUs per restart segment). A real NanoZoomer strip
/// uses DRI = 256; the per-segment decode budget charge bounds anything
/// bigger, this cap refuses absurd values before the segment math runs.
pub const MAX_RESTART_INTERVAL: u64 = 1 << 24;
/// Output-tile edge of the composed pyramid.
pub const OUT_TILE: u32 = 256;

/// True colorspace of a level's JPEG payloads (same rule as SVS).
pub use crate::jpeg::TiffJpegColor as PayloadColor;

#[derive(Debug, Clone)]
pub struct NdpiLevel {
    /// Index in the source IFD chain (reporting/provenance only).
    pub ifd_index: u32,
    pub width: u32,
    pub height: u32,
    /// Strip payload interval (offset fixups applied; bounds-checked).
    pub strip_offset: u64,
    pub strip_bytes: u64,
    /// True JPEG colorspace determined from the stream's markers.
    pub color: PayloadColor,
    /// SOF sampling (h1,v1,h2,v2,h3,v3).
    pub sampling: (u8, u8, u8, u8, u8, u8),
    /// MCU size in pixels (8·h_max, 8·v_max).
    pub mcu: (u32, u32),
    pub mcus_x: u32,
    pub mcus_y: u32,
    pub total_mcus: u64,
    /// Restart interval in MCUs (DRI; > 0 enforced).
    pub restart_interval: u32,
    /// Restart segments in the strip (= ⌈total_mcus / restart_interval⌉).
    pub segments: u64,
    /// Strip head bytes [SOI … SOS segment] (the sub-JPEG header source).
    pub header_bytes: Vec<u8>,
    /// Offset of the SOF height field inside `header_bytes` (width at +2).
    pub sof_hw_at: usize,
    /// [start, end) of the DRI marker segment inside `header_bytes`.
    pub dri_at: Option<(usize, usize)>,
}

impl NdpiLevel {
    pub fn mcu_w(&self) -> u32 {
        self.mcu.0
    }
    pub fn mcu_h(&self) -> u32 {
        self.mcu.1
    }
}

#[derive(Debug, Clone)]
pub struct NdpiDoc {
    pub kind: TiffKind,
    /// Pyramid levels (SourceLens > 0, focal plane 0), L0 first.
    pub levels: Vec<NdpiLevel>,
    /// Objective power = L0's SourceLens.
    pub objective: Option<f64>,
    /// µm/px from the vendor MPP tags; None on files without them.
    pub mpp: Option<(f64, f64)>,
    /// ICC profile bytes of IFD 0 (tag 34675), carried to the output.
    pub icc: Option<Vec<u8>>,
    /// Excluded pages (macro / focus map / z-stack / unknown) — detected,
    /// NOT exported.
    pub associated: Vec<AssociatedSummary>,
    /// Generated tail levels (width, height) — the `l0-box2` chain the
    /// conversion appends after the re-encoded level 0.
    pub generated: Vec<(u32, u32)>,
    /// Memory account carried from the probe into the conversion (review
    /// §1): the converter keeps charging band/decode working sets against
    /// the same host budget.
    pub budget: MemBudget,
}

// --------------------------------------------------------------------------- //
// NDPI value extensions (the >4 GiB non-standard offsets)
// --------------------------------------------------------------------------- //

/// Read the per-entry 4-byte extension words of an NDPI IFD. Non-standard
/// layout (F6): every IFD entry's value field is a 64-bit value whose HIGH
/// 32 bits are written as one u32 per entry AFTER the IFD's next pointer,
/// in entry order (the layout OpenSlide's tifflike reads in `ndpi` mode:
/// words at diroff + 12·count + 8). Files ≤ 4 GiB carry all-zero extensions
/// (verified on the public CMU-1 sample); a truncated area reads as zeros
/// for the missing tail.
///
/// The words are only MEANINGFUL in NDPI mode (see
/// [`ndpi_extension_mode`]); callers gate on that before trusting them.
pub fn ndpi_value_extensions(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
) -> CoreResult<Vec<u32>> {
    let n = ifd.entries.len();
    let base = ifd.offset
        + 2u64
        + ifd.entries.len() as u64 * 12
        + 4; // classic: count + entries + next
    let total = n as u64 * 4;
    if base > hdr.size || total > hdr.size.saturating_sub(base) {
        // absent/truncated extension area: zeros (the 32-bit values stand)
        return Ok(vec![0u32; n]);
    }
    let raw = src.read_at(base, total as usize)?;
    Ok(raw
        .chunks_exact(4)
        .map(|c| {
            let v = [c[0], c[1], c[2], c[3]];
            if hdr.little { u32::from_le_bytes(v) } else { u32::from_be_bytes(v) }
        })
        .collect())
}

/// NDPI-extension mode detection (the same gate OpenSlide applies before it
/// trusts the per-entry extension words): the first directory must carry
/// the vendor tag 65420. A Hamamatsu container without that tag keeps its
/// plain 32-bit values (the extension area is not assumed to exist).
pub fn ndpi_extension_mode(ifd0: &Ifd) -> bool {
    ifd0.find(65420).is_some()
}

/// u64 value of a tag with the NDPI extension applied (inline LONG widened
/// by its extension word; SHORT/SLONG/LONG8 behave like [`tiff_read`]).
pub fn ndpi_find_u64(
    _src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    ext: &[u32],
    t: u16,
) -> CoreResult<Option<u64>> {
    let Some(e) = ifd.find(t) else { return Ok(None) };
    let i = ifd.entries.iter().position(|e| e.tag == t).unwrap_or(0);
    let ext_v = ext.get(i).copied().unwrap_or(0);
    let mut v = match e.typ {
        3 => u16_at_val(&e.val, hdr.little) as u64,
        4 => u32_at_val(&e.val, hdr.little) as u64,
        9 => u32_at_val(&e.val, hdr.little) as u64,
        16 | 18 => u64_at_val(&e.val, hdr.little),
        typ => {
            return Err(CoreError::variant(format!(
                "tag {t} 类型 {typ} 不是整数标量"
            )))
        }
    };
    if e.typ == 4 && e.count == 1 && ext_v != 0 {
        v |= (ext_v as u64) << 32;
    }
    Ok(Some(v))
}

fn u16_at_val(v: &[u8; 8], little: bool) -> u16 {
    let b = [v[0], v[1]];
    if little { u16::from_le_bytes(b) } else { u16::from_be_bytes(b) }
}
fn u32_at_val(v: &[u8; 8], little: bool) -> u32 {
    let mut b = [0u8; 4];
    b.copy_from_slice(&v[..4]);
    if little { u32::from_le_bytes(b) } else { u32::from_be_bytes(b) }
}
fn u64_at_val(v: &[u8; 8], little: bool) -> u64 {
    let mut b = [0u8; 8];
    b.copy_from_slice(v);
    if little { u64::from_le_bytes(b) } else { u64::from_be_bytes(b) }
}

/// Scalar value of a tag as f64: SourceLens (65421) is written as FLOAT
/// (11) on real files; DOUBLE (12), SLONG (9), LONG (4), SHORT (3) and
/// RATIONAL (5) are accepted for robustness.
pub fn ndpi_find_f64(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    t: u16,
) -> CoreResult<Option<f64>> {
    let Some(e) = ifd.find(t) else { return Ok(None) };
    if e.count != 1 {
        return Err(CoreError::metadata(format!("tag {t} 需要单值（count={}）", e.count)));
    }
    let inline: [u8; 4] = [e.val[0], e.val[1], e.val[2], e.val[3]];
    let raw: Vec<u8> = match e.typ {
        3 => inline[..2].to_vec(),
        4 | 9 | 11 => inline.to_vec(),
        5 | 12 => tiff_read::entry_value(src, hdr, e)?,
        typ => {
            return Err(CoreError::variant(format!(
                "tag {t} 类型 {typ} 不是数值标量"
            )))
        }
    };
    if raw.len() < 8 && e.typ == 5 || raw.len() < 8 && e.typ == 12 {
        return Err(CoreError::metadata(format!("tag {t} 值过短")));
    }
    Ok(Some(match e.typ {
        3 => u16::from_le_bytes([raw[0], raw[1]]) as f64,
        4 => u32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]) as f64,
        9 => i32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]) as f64,
        11 => f32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]) as f64,
        12 => f64::from_le_bytes(raw[..8].try_into().unwrap()),
        5 => {
            let num = u32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]);
            let den = u32::from_le_bytes([raw[4], raw[5], raw[6], raw[7]]);
            if den == 0 {
                return Err(CoreError::metadata(format!("tag {t} RATIONAL 分母为 0")));
            }
            num as f64 / den as f64
        }
        _ => unreachable!("类型已在上面收窄"),
    }))
}

fn find_i64(
    _src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    t: u16,
) -> CoreResult<Option<i64>> {
    let Some(e) = ifd.find(t) else { return Ok(None) };
    let v = match e.typ {
        3 => u16_at_val(&e.val, hdr.little) as i64,
        4 => u32_at_val(&e.val, hdr.little) as i64,
        9 => u32_at_val(&e.val, hdr.little) as i32 as i64,
        typ => {
            return Err(CoreError::variant(format!(
                "tag {t} 类型 {typ} 不是整数标量"
            )))
        }
    };
    Ok(Some(v))
}

fn ascii_tag(src: &dyn ByteSource, hdr: &TiffHeader, ifd: &Ifd, t: u16) -> CoreResult<String> {
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

// --------------------------------------------------------------------------- //
// strip JPEG header scan (markers up to SOS; DRI + SOF positions)
// ---------------------------------------------------------------------------

/// Parsed strip head: everything the sub-JPEG construction needs.
#[derive(Debug, Clone)]
pub struct StripHead {
    pub width: u32,
    pub height: u32,
    pub sof_marker: u8,
    pub sampling: Option<(u8, u8, u8, u8, u8, u8)>,
    pub jfif: bool,
    pub adobe_transform: Option<u8>,
    /// Restart interval in MCUs (DRI; 0 = none).
    pub restart_interval: u32,
    /// [SOI … SOS segment] length (the entropy data starts here).
    pub header_len: usize,
    /// Offset of the SOF height field within the header (width at +2).
    pub sof_hw_at: usize,
    /// [start, end) of the DRI marker segment within the header.
    pub dri_at: Option<(usize, usize)>,
}

/// Scan a strip head for the markers up to (and including) SOS. Same marker
/// walk as `jpeg::scan_jpeg`, plus DRI/SOF positions for the sub-JPEG
/// surgery (remove DRI, patch SOF dims per segment).
pub fn scan_strip_head(head: &[u8]) -> CoreResult<StripHead> {
    let n = head.len();
    if n < 4 || head[0..2] != [0xFF, 0xD8] {
        return Err(CoreError::jpeg("层 JPEG 不是合法流（缺 SOI）"));
    }
    let mut out = StripHead {
        width: 0,
        height: 0,
        sof_marker: 0,
        sampling: None,
        jfif: false,
        adobe_transform: None,
        restart_interval: 0,
        header_len: 0,
        sof_hw_at: 0,
        dri_at: None,
    };
    let mut i = 2usize;
    while i + 4 <= n {
        if head[i] != 0xFF {
            return Err(CoreError::jpeg("JPEG 标记流错位"));
        }
        while i < n && head[i] == 0xFF {
            i += 1;
        }
        if i >= n {
            break;
        }
        let marker = head[i];
        let marker_at = i - 1;
        i += 1;
        if marker == 0xD9 {
            break;
        }
        if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            continue;
        }
        if i + 2 > n {
            return Err(CoreError::jpeg("JPEG 标记段截断"));
        }
        let seg_len = ((head[i] as usize) << 8) | head[i + 1] as usize;
        if seg_len < 2 || i + seg_len > n {
            return Err(CoreError::jpeg("JPEG 段长度非法"));
        }
        let seg = &head[i + 2..i + seg_len];
        match marker {
            0xE0 if seg.len() >= 5 && &seg[..5] == b"JFIF\0" => out.jfif = true,
            0xEE if seg.len() >= 12 && &seg[..5] == b"Adobe" => {
                out.adobe_transform = Some(seg[11]);
            }
            0xDD => {
                if seg.len() < 2 {
                    return Err(CoreError::jpeg("DRI 段残缺"));
                }
                out.restart_interval = ((seg[0] as u32) << 8) | seg[1] as u32;
                out.dri_at = Some((marker_at, i + seg_len));
            }
            0xC2 | 0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF => {
                return Err(CoreError::jpeg(format!(
                    "不支持渐进/分层/无损 JPEG（SOF FF{marker:02X}）"
                )));
            }
            0xCC => return Err(CoreError::jpeg("算术编码不受支持")),
            m if m == 0xC0 || m == 0xC1 => {
                if seg.len() < 6 {
                    return Err(CoreError::jpeg("SOF 段残缺"));
                }
                out.sof_marker = m;
                out.height = ((seg[1] as u32) << 8) | seg[2] as u32;
                out.width = ((seg[3] as u32) << 8) | seg[4] as u32;
                let ncomp = seg[5];
                if (ncomp != 1 && ncomp != 3) || seg.len() < 6 + 3 * ncomp as usize {
                    return Err(CoreError::jpeg("SOF 分量数非法"));
                }
                if ncomp == 3 {
                    let f = |c: usize| (seg[6 + 3 * c + 1] >> 4, seg[6 + 3 * c + 1] & 0x0F);
                    let (a, b, c) = (f(0), f(1), f(2));
                    out.sampling = Some((a.0, a.1, b.0, b.1, c.0, c.1));
                }
                // height is the 2nd/3rd byte of the SOF segment: seg starts
                // at head[i+2] → height field at i+3
                out.sof_hw_at = i + 3;
            }
            0xDA => {
                out.header_len = i + seg_len;
                break;
            }
            _ => {}
        }
        i += seg_len;
    }
    if out.sof_marker == 0 || out.width == 0 || out.height == 0 {
        return Err(CoreError::jpeg("层 JPEG 缺少 SOF"));
    }
    if out.header_len == 0 {
        return Err(CoreError::jpeg("层 JPEG 缺少 SOS"));
    }
    Ok(out)
}

// --------------------------------------------------------------------------- //
// probe
// --------------------------------------------------------------------------- //

fn check_level_container(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    ext: &[u32],
    idx: usize,
) -> CoreResult<(u32, u32)> {
    let compression = match ifd.find(tag::COMPRESSION) {
        Some(e) => tiff_read::entry_u64(src, hdr, e)?,
        None => 0,
    };
    match compression {
        7 => {}
        33003 | 33005 => {
            return Err(CoreError::variant(format!(
                "第 {idx} 页为 JPEG 2000 压缩（{compression}）：NDPI 的 JP2K 变体不在支持集（需要独立解码器）"
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
            "第 {idx} 页 SamplesPerPixel={samples}（灰度/多通道页不在明场 NDPI 支持集）"
        )));
    }
    if ifd.find(tag::EXTRA_SAMPLES).is_some() {
        return Err(CoreError::variant(format!(
            "第 {idx} 页带 ExtraSamples（荧光/透明通道页组不在明场 NDPI 支持集）"
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
    // one strip per layer; a tiled NDPI variant does not exist
    let (off_e, cnt_e) = (ifd.find(tag::STRIP_OFFSETS), ifd.find(tag::STRIP_COUNTS));
    let (Some(off_e), Some(cnt_e)) = (off_e, cnt_e) else {
        return Err(CoreError::variant(format!(
            "第 {idx} 页不是条带存储（缺 StripOffsets/StripByteCounts）：未知 NDPI 变体"
        )));
    };
    if off_e.count != 1 || cnt_e.count != 1 {
        return Err(CoreError::variant(format!(
            "第 {idx} 页是 {} 条带存储：NDPI 的整层单条带变体之外不支持多条带",
            off_e.count
        )));
    }
    if ifd.find(322).is_some() || ifd.find(323).is_some() {
        return Err(CoreError::variant(format!(
            "第 {idx} 页带 tile 标签：分块存储不是 NDPI 布局"
        )));
    }
    let width = ndpi_find_u64(src, hdr, ifd, ext, tag::WIDTH)?.unwrap_or(0);
    let height = ndpi_find_u64(src, hdr, ifd, ext, tag::HEIGHT)?.unwrap_or(0);
    let rps = ndpi_find_u64(src, hdr, ifd, ext, tag::ROWS_PER_STRIP)?.unwrap_or(0);
    if !(1..=MAX_SIDE).contains(&width) || !(1..=MAX_SIDE).contains(&height) {
        return Err(CoreError::variant(format!(
            "第 {idx} 页尺寸 {width}×{height} 越界"
        )));
    }
    if rps != height {
        return Err(CoreError::variant(format!(
            "第 {idx} 页 RowsPerStrip={rps} ≠ 层高 {height}：不是整层单条带布局"
        )));
    }
    Ok((width as u32, height as u32))
}

/// Verify one level IFD's strip interval and JPEG header, computing the
/// segment geometry. `decoded` = the conversion will actually decode this
/// layer's pixels (only L0): only there are restart markers REQUIRED — the
/// real files' highest reduced layers carry no DRI at all, and the
/// converter never decodes them (reduced output levels are l0-box2).
fn inspect_level(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    ext: &[u32],
    idx: u32,
    decoded: bool,
    budget: &mut MemBudget,
) -> CoreResult<NdpiLevel> {
    let (width, height) = check_level_container(src, hdr, ifd, ext, idx as usize)?;
    let strip_offset =
        ndpi_find_u64(src, hdr, ifd, ext, tag::STRIP_OFFSETS)?.unwrap_or(0);
    let strip_bytes = ndpi_find_u64(src, hdr, ifd, ext, tag::STRIP_COUNTS)?.unwrap_or(0);
    if strip_bytes == 0 {
        return Err(CoreError::oob(format!("第 {idx} 层条带长度为 0")));
    }
    // the >4 GiB wrap fallback: a strip interval past the file end cannot be
    // a valid NDPI layout (the extension words carry the high bits; this is
    // a corruption, not something to guess around)
    let end = strip_offset
        .checked_add(strip_bytes)
        .ok_or_else(|| CoreError::oob(format!("第 {idx} 层条带区间溢出")))?;
    if end > hdr.size {
        return Err(CoreError::oob(format!(
            "第 {idx} 层条带区间 [{strip_offset},+{strip_bytes}) 超出文件 {} 字节（>4 GiB 偏移扩展损坏？）",
            hdr.size
        )));
    }
    // probe the strip head (bounded): charge the transient read before it
    let head_len = strip_bytes.min(PROBE_LIMIT) as usize;
    budget.charge(head_len as u64, "层 JPEG 头探测读取")?;
    let head = src.read_at(strip_offset, head_len)?;
    budget.release(head_len as u64);
    let sh = scan_strip_head(&head).map_err(|e| {
        CoreError::jpeg(format!("第 {idx} 层条带头：{}", e.message))
    })?;
    if sh.width != width || sh.height != height {
        return Err(CoreError::variant(format!(
            "第 {idx} 层 SOF 尺寸 {}×{} ≠ TIFF 标注 {width}×{height}",
            sh.width, sh.height
        )));
    }
    let sampling = sh.sampling.ok_or_else(|| {
        CoreError::variant(format!("第 {idx} 层 JPEG 不是三分量（灰度不在明场支持集）"))
    })?;
    if decoded && sh.restart_interval == 0 {
        return Err(CoreError::variant(format!(
            "第 {idx} 层 JPEG 无 restart marker（DRI=0）：没有有界解码单元，无法分段解码整层条带"
        )));
    }
    if sh.restart_interval as u64 > MAX_RESTART_INTERVAL {
        return Err(CoreError::variant(format!(
            "第 {idx} 层 restart interval {} 越界（> {MAX_RESTART_INTERVAL}）",
            sh.restart_interval
        )));
    }
    let (h1, v1, h2, v2, h3, v3) = sampling;
    let hmax = h1.max(h2).max(h3) as u32;
    let vmax = v1.max(v2).max(v3) as u32;
    let mcu_w = 8 * hmax;
    let mcu_h = 8 * vmax;
    let mcus_x = width.div_ceil(mcu_w);
    let mcus_y = height.div_ceil(mcu_h);
    let total_mcus = mcus_x as u64 * mcus_y as u64;
    let segments = if sh.restart_interval > 0 {
        total_mcus.div_ceil(sh.restart_interval as u64)
    } else {
        0
    };
    let color = jpeg::tiff_jpeg_color(
        &jpeg::JpegProbe {
            width: sh.width,
            height: sh.height,
            sampling: sh.sampling,
            comp_ids: None,
            jfif: sh.jfif,
            adobe_transform: sh.adobe_transform,
            sof_marker: sh.sof_marker,
        },
        find_i64(src, hdr, ifd, tag::PHOTO)?.map(|x| x as u64).unwrap_or(6),
    );
    Ok(NdpiLevel {
        ifd_index: idx,
        width,
        height,
        strip_offset,
        strip_bytes,
        color,
        sampling,
        mcu: (mcu_w, mcu_h),
        mcus_x,
        mcus_y,
        total_mcus,
        restart_interval: sh.restart_interval,
        segments,
        header_bytes: head[..sh.header_len].to_vec(),
        sof_hw_at: sh.sof_hw_at,
        dri_at: sh.dri_at,
    })
}

/// Capability probe with an explicit host memory budget (review §1): the
/// structural walk and the per-level header probes are charged BEFORE they
/// read/allocate; an over-budget charge is the stable typed refusal
/// `resource_profile_insufficient`.
pub fn probe_ndpi_with_budget(src: &dyn ByteSource, budget_bytes: u64) -> CoreResult<NdpiDoc> {
    let mut budget = MemBudget::host(budget_bytes);
    budget.charge(
        (tiff_read::MAX_IFDS * tiff_read::MAX_IFD_ENTRIES * 32) as u64,
        "IFD 链结构预留",
    )?;
    let hdr = tiff_read::read_header(src)?;
    if hdr.kind != TiffKind::Classic {
        return Err(CoreError::variant(
            "NDPI 是经典 TIFF（42）；BigTIFF 容器不是 NDPI 输入",
        ));
    }
    let chain = tiff_read::ifd_chain(src, &hdr)?;
    if chain.len() > MAX_LEVELS + 16 {
        return Err(CoreError::variant(format!(
            "IFD 页数 {} 超出 NDPI 布局上限",
            chain.len()
        )));
    }
    let make = ascii_tag(src, &hdr, &chain[0], tag::MAKE)?;
    if !make.contains("Hamamatsu") {
        return Err(CoreError::variant(format!(
            "Make（271）= {make:?} 未标识 Hamamatsu：不是 NDPI 输入"
        )));
    }

    // ---- IFD 0: the main image (L0; SourceLens > 0, focal plane 0) ------ //
    let ndpi_ext = ndpi_extension_mode(&chain[0]);
    let ext0 = if ndpi_ext {
        ndpi_value_extensions(src, &hdr, &chain[0])?
    } else {
        vec![0u32; chain[0].entries.len()]
    };
    let lens0 = ndpi_find_f64(src, &hdr, &chain[0], tag::SOURCELENS)?
        .ok_or_else(|| {
            CoreError::metadata("IFD 0 缺少 SourceLens（65421）：无法区分层级与关联图，拒绝猜测")
        })?;
    let focal0 = find_i64(src, &hdr, &chain[0], tag::FOCAL_PLANE)?.unwrap_or(0);
    if lens0 <= 0.0 {
        return Err(CoreError::variant(format!(
            "IFD 0 SourceLens={lens0}：主图必须是正物镜倍率（关联图在 IFD 0 的布局不是 NDPI 支持集）"
        )));
    }
    if focal0 != 0 {
        return Err(CoreError::variant(format!(
            "IFD 0 focal plane={focal0}：z-stack 页不能作主图"
        )));
    }
    let mut levels =
        vec![inspect_level(src, &hdr, &chain[0], &ext0, 0, true, &mut budget)?];

    // ---- following IFDs: reduced levels and excluded pages --------------- //
    let mut associated: Vec<AssociatedSummary> = Vec::new();
    for (idx, ifd) in chain.iter().enumerate().skip(1) {
        if levels.len() >= MAX_LEVELS {
            return Err(CoreError::variant(format!("层级数超过 {MAX_LEVELS}")));
        }
        let ext = if ndpi_ext {
            ndpi_value_extensions(src, &hdr, ifd)?
        } else {
            vec![0u32; ifd.entries.len()]
        };
        let lens = ndpi_find_f64(src, &hdr, ifd, tag::SOURCELENS)?
            .ok_or_else(|| {
                CoreError::metadata(format!(
                    "第 {idx} 页缺少 SourceLens（65421）：无法区分层级与关联图，拒绝猜测"
                ))
            })?;
        let w = ndpi_find_u64(src, &hdr, ifd, &ext, tag::WIDTH)?.unwrap_or(0);
        let h = ndpi_find_u64(src, &hdr, ifd, &ext, tag::HEIGHT)?.unwrap_or(0);
        let summary = |name: &str| AssociatedSummary {
            name: name.to_string(),
            source_offset: 0,
            source_length: 0,
            width: w as u32,
            height: h as u32,
        };
        if lens <= 0.0 {
            let name = if lens == -1.0 {
                "macro"
            } else if lens == -2.0 {
                "focusmap"
            } else {
                "associated"
            };
            associated.push(summary(name));
            continue;
        }
        let focal = find_i64(src, &hdr, ifd, tag::FOCAL_PLANE)?.unwrap_or(0);
        if focal != 0 {
            associated.push(summary("focalplane"));
            continue;
        }
        // a true reduced level: strictly smaller with a 1.5–8× step
        let prev = levels.last().expect("L0 exists");
        if w >= prev.width as u64 || h >= prev.height as u64 {
            return Err(CoreError::variant(format!(
                "第 {idx} 页（{w}×{h}）未小于上一层级（{}×{}）：未知 NDPI 页序",
                prev.width, prev.height
            )));
        }
        let rx = prev.width as f64 / w as f64;
        let ry = prev.height as f64 / h as f64;
        if !(1.5..=8.0).contains(&rx) || !(1.5..=8.0).contains(&ry) || (rx - ry).abs() > 1.5 {
            return Err(CoreError::variant(format!(
                "第 {idx} 页降采样比异常（x={rx:.3}, y={ry:.3}）：不是已知的 Hamamatsu 层级序列"
            )));
        }
        levels.push(inspect_level(src, &hdr, ifd, &ext, idx as u32, false, &mut budget)?);
    }

    let objective = (lens0 > 0.0 && lens0.is_finite()).then_some(lens0);
    let mx = ndpi_find_f64(src, &hdr, &chain[0], tag::MPP_X)?;
    let my = ndpi_find_f64(src, &hdr, &chain[0], tag::MPP_Y)?;
    let mpp = mx
        .zip(my)
        .filter(|(x, y)| x.is_finite() && *x > 0.0 && y.is_finite() && *y > 0.0);
    let icc = match chain[0].find(tag::ICC) {
        Some(e) => Some(tiff_read::entry_value(src, &hdr, e)?),
        None => None,
    };
    let generated = crate::gtiff::generated_tail(levels[0].width, levels[0].height);
    Ok(NdpiDoc {
        kind: hdr.kind,
        levels,
        objective,
        mpp,
        icc,
        associated,
        generated,
        budget,
    })
}

/// Capability probe at the conservative saver budget (CLI default).
pub fn probe_ndpi(src: &dyn ByteSource) -> CoreResult<NdpiDoc> {
    probe_ndpi_with_budget(src, crate::budget::SAVER_BUDGET_BYTES)
}

// --------------------------------------------------------------------------- //
// estimate
// --------------------------------------------------------------------------- //

/// Disk-precheck estimate for the NDPI adapter (MRXS 同族上界：输出 tile 全部
/// 按「保留画质」重编码，源条带字节数只是像素代理；运行时输出上限兜底).
pub fn estimate_ndpi(doc: &NdpiDoc) -> crate::estimate::OutputEstimate {
    let l0 = &doc.levels[0];
    let payload = l0.strip_bytes;
    let l0_tiles =
        (l0.width as u64).div_ceil(OUT_TILE as u64) * (l0.height as u64).div_ceil(OUT_TILE as u64);
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
            .saturating_mul(2)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
        compact_upper_bound_bytes: payload
            .saturating_mul(3)
            .div_ceil(2)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
    }
}
