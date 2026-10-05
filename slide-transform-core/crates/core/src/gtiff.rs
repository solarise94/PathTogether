//! Generic tiled JPEG TIFF/BigTIFF input adapter (F5): brightfield
//! pyramids with NO vendor description — the OpenSlide `generic-tiff`
//! family (e.g. a vips/tiffcp-converted `CMU-1.tiff`).
//!
//! Detection contract (bounded reads only, whole file never buffered):
//!
//! - the file must be a classic TIFF or BigTIFF (either byte order) whose
//!   IFD 0 description names NO known vendor — the routing layers
//!   (`sniff_tiff_vendor`) send Aperio/Leica SCN/OME-TIFF/converter-BigTIFF
//!   elsewhere, and this adapter re-checks the classification BEFORE any
//!   payload is copied (a vendor file reaching this probe is a typed
//!   rejection, never a best-effort parse);
//! - EVERY IFD of the main chain is a pyramid level: **tiled**, compression
//!   7 (baseline JPEG), photometric 2 (RGB) / 6 (YCbCr), 3 samples of 8
//!   bits, chunky planar, single shared tile geometry across levels. The
//!   unconvertible variants are typed rejections classified as「暂时直传」
//!   upstream (stripped storage, LZW/deflate/anything-not-7, JPEG 2000,
//!   non-8-bit, multi-channel/gray, planar) — never a best-effort copy;
//! - levels form a strictly decreasing sequence with 1.5–16× steps and
//!   matching x/y ratios;
//! - MISSING reduced levels (a source that ends above the 256-px side
//!   threshold — typically a single-IFD conversion output) are GENERATED:
//!   each output tail level is the 2×2 area-average (box) downsample chain
//!   continued from the last PRESENT level (for a level-0-only source this
//!   is exactly the `l0-box2` pyramid of output level 0, the same method id
//!   as the MRXS adapter). Generated tiles are re-encoded at the locked
//!   parameters of [`GEN_QUALITY`]/[`GEN_SAMPLING`] (compact encoding swaps
//!   in the locked compact-jpeg-v1 parameters); the count is warned
//!   (`gtiff_missing_levels_generated`);
//! - the JPEG payloads' true colorspace comes from the JPEG itself (same
//!   rule as SVS/SCN); shared `JPEGTables` (tag 347) ride along verbatim;
//! - MPP comes from XResolution/YResolution + ResolutionUnit (inch/cm) when
//!   BOTH resolutions are present and agree within 1%; otherwise unknown —
//!   nothing is invented;
//! - an ICC profile (tag 34675) on IFD 0 is carried into the output.
//!
//! There is no associated-image concept here: a main-chain IFD that is not
//! a level (stripped thumbnail, second series) is a typed rejection —
//! a generic TIFF container gives no vendor vocabulary to tell them apart.

use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;
use crate::scn::{classify_description, TiffVendor};
use crate::tiff_read::{self, Ifd, TiffHeader, TiffKind};

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "generic-tiled-jpeg-tiff";
/// Adapter version (bump on any output-affecting change). Enforced in the
/// core's resume entry: a checkpoint whose recorded version differs from
/// this one — or that carries NO version at all — is refused, mirroring the
/// SCN adapter's pin.
pub const ADAPTER_VERSION: &str = "1";

/// How reduced output levels are built (the MRXS adapter's method id: every
/// generated level is the 2×2 area-average chain continued from the last
/// present source level; for a level-0-only source that chain starts AT
/// level 0 — genuinely L0-derived).
pub const PYRAMID_METHOD: &str = "l0-box2";
/// Reduced-level generation stops once both sides fit within one 256-px
/// tile (the convention the OpenSlide generic-tiff sample itself follows:
/// 46000 px wide → 9 levels down to 179×128).
pub const GEN_MIN_SIDE: u64 = 256;
/// Locked preserve-mode encode parameters of generated tiles (documented
/// high-fidelity; same family as the MRXS compose parameters).
pub const GEN_QUALITY: u8 = 96;
pub const GEN_SAMPLING: jpeg::Sampling = jpeg::Sampling::S422;
pub const GEN_HUFFMAN: &str = "standard-annex-k";
/// Versioned fingerprint of the generated-tile encode parameters.
pub const GEN_FINGERPRINT: &str = "gtiff-l0-box2:q96:y422:hstd:v1";
/// Disk-precheck proxy per generated tile: a 256×256 tissue tile at q80–96
/// measures ~8–30 KiB; 64 KiB keeps the estimate an upper bound.
pub const GEN_TILE_BYTES_PROXY: u64 = 64 * 1024;

/// Sane geometric caps (SVS/SCN parity: the per-side cap alone bounds a
/// u32-max × 1 level).
const MAX_SIDE: u64 = 1_000_000;
const MAX_TILES_PER_LEVEL: u64 = 4_000_000;
const MAX_LEVELS: usize = 32;
/// Cap of a single probe payload read (scan_jpeg only needs up to the SOS).
pub const PROBE_LIMIT: u64 = 256 * 1024;

/// True colorspace of a level's JPEG payloads (same rule as SVS).
pub use crate::jpeg::TiffJpegColor as PayloadColor;

#[derive(Debug, Clone)]
pub struct GtiffLevel {
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

#[derive(Debug, Clone)]
pub struct GtiffDoc {
    pub kind: TiffKind,
    pub levels: Vec<GtiffLevel>,
    /// Generated tail levels (width, height) — the `l0-box2` chain the
    /// conversion will append after the last present source level.
    pub generated: Vec<(u32, u32)>,
    /// µm/px from XResolution/YResolution + ResolutionUnit (both present,
    /// same value within 1%); None otherwise.
    pub mpp: Option<f64>,
    /// ICC profile bytes of IFD 0 (tag 34675), carried to the output.
    pub icc: Option<Vec<u8>>,
    /// The probe's charged memory account (convert re-uses the reservation).
    pub budget: MemBudget,
}

fn get_u64(src: &dyn ByteSource, hdr: &TiffHeader, ifd: &Ifd, tag: u16) -> CoreResult<Option<u64>> {
    tiff_read::find_u64(src, hdr, ifd, tag)
}

/// Read one RATIONAL (type 5, num/den) array of `count` elements.
fn rational_values(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    tag: u16,
) -> CoreResult<Option<Vec<f64>>> {
    let Some(e) = ifd.find(tag) else { return Ok(None) };
    if e.typ != 5 {
        return Err(CoreError::variant(format!(
            "tag {tag} 类型 {} 不是 RATIONAL（5）",
            e.typ
        )));
    }
    let raw = tiff_read::entry_value(src, hdr, e)?;
    let vals = raw
        .chunks_exact(8)
        .map(|c| {
            let (n, d) = if hdr.little {
                (
                    u32::from_le_bytes([c[0], c[1], c[2], c[3]]),
                    u32::from_le_bytes([c[4], c[5], c[6], c[7]]),
                )
            } else {
                (
                    u32::from_be_bytes([c[0], c[1], c[2], c[3]]),
                    u32::from_be_bytes([c[4], c[5], c[6], c[7]]),
                )
            };
            if d == 0 {
                f64::NAN
            } else {
                n as f64 / d as f64
            }
        })
        .collect::<Vec<_>>();
    Ok(Some(vals))
}

/// MPP from the resolution tags: `ResolutionUnit` 2 = inch (25 400 µm),
/// 3 = cm (10 000 µm); both X and Y must be present, finite, positive and
/// agree within 1% — anything else stays unknown (nothing is invented).
fn mpp_from_resolution(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
) -> CoreResult<Option<f64>> {
    let unit = get_u64(src, hdr, ifd, 296)?;
    let xres = rational_values(src, hdr, ifd, 282)?;
    let yres = rational_values(src, hdr, ifd, 283)?;
    let (Some(xs), Some(ys)) = (xres, yres) else { return Ok(None) };
    let (Some(x), Some(y)) = (xs.first().copied(), ys.first().copied()) else {
        return Ok(None);
    };
    let um_per_unit = match unit {
        Some(2) => 25_400.0,
        Some(3) => 10_000.0,
        _ => return Ok(None),
    };
    if !x.is_finite() || !y.is_finite() || x <= 0.0 || y <= 0.0 {
        return Ok(None);
    }
    let (mx, my) = (um_per_unit / x, um_per_unit / y);
    if (mx - my).abs() > 0.01 * mx.max(my) {
        return Ok(None);
    }
    Ok(mx.is_finite().then_some(mx))
}

/// Merge `JPEGTables` with an abbreviated tile stream (SVS convention).
pub use crate::svs::merge_tables_then_tile;

/// Read the first tile payload of an IFD (bounded) for the JPEG truth probe.
fn first_tile_head(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
) -> CoreResult<Vec<u8>> {
    let mut cur = tiff_read::TileCursor::new(src, hdr, ifd)?;
    let (off, len) = cur
        .next_pair()?
        .ok_or_else(|| CoreError::validation("层级无任何 tile"))?;
    let want = len.min(PROBE_LIMIT) as usize;
    src.read_at(off, want)
}

/// The strict IFD-0 re-check: this adapter only accepts containers whose
/// description names NO known vendor (routing already dispatches the rest —
/// this is the defence-in-depth copy gate).
fn require_unknown_vendor(src: &dyn ByteSource, hdr: &TiffHeader, ifd: &Ifd) -> CoreResult<()> {
    let desc = match ifd.find(270) {
        Some(e) => {
            if e.value_len().unwrap_or(0) > 4 << 20 {
                return Err(CoreError::oob("IFD 0 描述长度异常"));
            }
            let raw = tiff_read::entry_value(src, hdr, e)?;
            String::from_utf8_lossy(&raw).to_string()
        }
        None => String::new(),
    };
    match classify_description(&desc) {
        TiffVendor::Unknown => Ok(()),
        TiffVendor::AperioSvs => Err(CoreError::variant(
            "Aperio SVS 不走通用 TIFF 适配器（应由 SVS 适配器按原样搬运）",
        )),
        TiffVendor::LeicaScn => Err(CoreError::variant(
            "Leica SCN XML 不走通用 TIFF 适配器（应由 SCN 适配器处理）",
        )),
        TiffVendor::OmeTiff => Err(CoreError::variant(
            "OME-TIFF 不是转换输入：平台可直接读取 OME-TIFF，请直接上传该文件",
        )),
        TiffVendor::ConverterBigTiff => Err(CoreError::variant(
            "本工具导出的 BigTIFF 不是转换输入：请直接上传该产物（或选择原始切片）",
        )),
    }
}

/// The structural accept rules shared by every level IFD (the typed「暂时
/// 直传」rejections live here — variant, never corruption).
fn check_level_variant(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    idx: usize,
) -> CoreResult<u64> {
    let tiled = ifd.find(322).is_some() && ifd.find(323).is_some();
    let stripped = ifd.find(273).is_some();
    if !tiled {
        if stripped {
            return Err(CoreError::variant(format!(
                "第 {idx} 页是带状存储（stripped）：该通用 TIFF 变体不在支持集，暂时直传"
            )));
        }
        return Err(CoreError::variant(format!(
            "第 {idx} 页既非分块也非带状存储：未知 TIFF 变体，不支持"
        )));
    }
    let compression = get_u64(src, hdr, ifd, 259)?.unwrap_or(0);
    match compression {
        7 => {}
        33003 | 33005 => {
            return Err(CoreError::variant(format!(
                "第 {idx} 页为 JPEG 2000 压缩（{compression}）：不在支持集，暂时直传"
            )))
        }
        5 => {
            return Err(CoreError::variant(format!(
                "第 {idx} 页为 LZW 压缩：不是基线 JPEG（259=7），无法按原样搬运，暂时直传"
            )))
        }
        8 | 32946 => {
            return Err(CoreError::variant(format!(
                "第 {idx} 页为 deflate 压缩：不是基线 JPEG（259=7），无法按原样搬运，暂时直传"
            )))
        }
        c => {
            return Err(CoreError::variant(format!(
                "第 {idx} 页压缩编码 {c} 不是基线 JPEG（259=7），无法按原样搬运，暂时直传"
            )))
        }
    }
    let planar = get_u64(src, hdr, ifd, 284)?.unwrap_or(1);
    if planar != 1 {
        return Err(CoreError::variant(format!(
            "第 {idx} 页 PlanarConfiguration={planar}（平面存储）不在支持集，暂时直传"
        )));
    }
    let samples = get_u64(src, hdr, ifd, 277)?.unwrap_or(1);
    if samples != 3 {
        return Err(CoreError::variant(format!(
            "第 {idx} 页 SamplesPerPixel={samples}（灰度/多通道不在明场支持集），暂时直传"
        )));
    }
    if let Some(e) = ifd.find(258) {
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
                "第 {idx} 页 BitsPerSample={vals:?} ≠ [8,8,8]（非 8 位不在支持集），暂时直传"
            )));
        }
    }
    let photo = get_u64(src, hdr, ifd, 262)?.unwrap_or(0);
    if photo != 2 && photo != 6 {
        return Err(CoreError::variant(format!(
            "第 {idx} 页 PhotometricInterpretation={photo} 不在支持集（RGB=2 / YCbCr=6），暂时直传"
        )));
    }
    Ok(photo)
}

/// Verify one level IFD's tile geometry and determine the JPEG truth
/// (SVS semantics: the payload stream is what ships, the TIFF tags lie).
fn inspect_level(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    ifd_index: u32,
    photometric: u64,
) -> CoreResult<GtiffLevel> {
    let width = get_u64(src, hdr, ifd, 256)?.unwrap_or(0);
    let height = get_u64(src, hdr, ifd, 257)?.unwrap_or(0);
    if !(1..=MAX_SIDE).contains(&width) || !(1..=MAX_SIDE).contains(&height) {
        return Err(CoreError::variant(format!(
            "层级尺寸 {width}×{height} 越界（单边上限 {MAX_SIDE}）"
        )));
    }
    let tw = get_u64(src, hdr, ifd, 322)?.unwrap_or(0);
    let th = get_u64(src, hdr, ifd, 323)?.unwrap_or(0);
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
    let mut cur = tiff_read::TileCursor::new(src, hdr, ifd)?;
    if cur.total() != total {
        return Err(CoreError::validation(format!(
            "TileOffsets 数 {} ≠ 网格 {}×{}",
            cur.total(),
            across,
            down
        )));
    }
    // stream every (offset,count) so the whole grid is bounds-checked and the
    // payload sum is exact (memory stays O(chunk)); a (0,0) entry is a typed
    // error — the generic TIFF family has no sparse-grid convention
    let mut payload_bytes = 0u64;
    while let Some((_, c)) = cur.next_pair()? {
        payload_bytes = payload_bytes.saturating_add(c);
    }
    let jpeg_tables = match ifd.find(347) {
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
    Ok(GtiffLevel {
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

/// The generated tail: the 2×2 box chain continued from `last` until both
/// sides fit [`GEN_MIN_SIDE`] (or the level cap). Floor semantics with a
/// 1-px floor; a degenerate (same-size) step stops the chain.
pub fn generated_tail(width: u32, height: u32) -> Vec<(u32, u32)> {
    let mut out = Vec::new();
    let (mut w, mut h) = (width as u64, height as u64);
    while out.len() < MAX_LEVELS && (w > GEN_MIN_SIDE || h > GEN_MIN_SIDE) {
        let (nw, nh) = ((w / 2).max(1), (h / 2).max(1));
        if nw == w && nh == h {
            break;
        }
        out.push((nw as u32, nh as u32));
        w = nw as u64;
        h = nh as u64;
    }
    out
}

/// Capability probe with an explicit host budget: the structural walk is
/// charged BEFORE it is built; an over-budget charge is the stable typed
/// refusal `resource_profile_insufficient` — never an OOM after the fact.
pub fn probe_gtiff_with_budget(src: &dyn ByteSource, budget_bytes: u64) -> CoreResult<GtiffDoc> {
    let mut budget = MemBudget::host(budget_bytes);
    budget.charge(
        (tiff_read::MAX_IFDS * tiff_read::MAX_IFD_ENTRIES * 32) as u64,
        "IFD 链结构预留",
    )?;
    let hdr = tiff_read::read_header(src)?;
    let chain = tiff_read::ifd_chain(src, &hdr)?;
    require_unknown_vendor(src, &hdr, &chain[0])?;
    if chain.len() > MAX_LEVELS {
        return Err(CoreError::variant(format!("层级数超过 {MAX_LEVELS}")));
    }

    let mut levels: Vec<GtiffLevel> = Vec::with_capacity(chain.len());
    for (idx, ifd) in chain.iter().enumerate() {
        let photo = check_level_variant(src, &hdr, ifd, idx)?;
        let lv = inspect_level(src, &hdr, ifd, idx as u32, photo)?;
        if let Some(prev) = levels.last() {
            if prev.width <= lv.width || prev.height <= lv.height {
                return Err(CoreError::variant(format!(
                    "第 {idx} 页（{}×{}）未小于上一页（{}×{}）：不是严格递减金字塔",
                    lv.width, lv.height, prev.width, prev.height
                )));
            }
            let rx = prev.width as f64 / lv.width as f64;
            let ry = prev.height as f64 / lv.height as f64;
            if !(1.5..=16.0).contains(&rx) || !(1.5..=16.0).contains(&ry) || (rx - ry).abs() > 1.5 {
                return Err(CoreError::variant(format!(
                    "第 {idx} 页降采样比异常（x={rx:.3}, y={ry:.3}）：不是受支持的层级序列"
                )));
            }
        }
        levels.push(lv);
    }
    // one shared tile geometry across levels — the generated pyramid and the
    // box2 composition both assume it
    let (tw, th) = (levels[0].tile_w, levels[0].tile_h);
    if levels.iter().any(|l| l.tile_w != tw || l.tile_h != th) {
        return Err(CoreError::variant(
            "各层 tile 尺寸不一致：通用 TIFF 适配器要求整座金字塔共享一个 tile 几何，暂时直传",
        ));
    }

    let last = levels.last().expect("chain non-empty");
    let generated = generated_tail(last.width, last.height);
    let mpp = mpp_from_resolution(src, &hdr, &chain[0])?;
    let icc = match chain[0].find(34675) {
        Some(e) => Some(tiff_read::entry_value(src, &hdr, e)?),
        None => None,
    };
    Ok(GtiffDoc { kind: hdr.kind, levels, generated, mpp, icc, budget })
}

/// Capability probe at the conservative saver budget (CLI default).
pub fn probe_gtiff(src: &dyn ByteSource) -> CoreResult<GtiffDoc> {
    probe_gtiff_with_budget(src, crate::budget::SAVER_BUDGET_BYTES)
}

/// Disk-precheck estimate for the generic TIFF adapter. Generated tail
/// levels contribute a per-tile byte proxy (they are re-encoded); the
/// standard 1.25×/1.5× slack of the shared `bound` covers the rest.
pub fn estimate_gtiff(doc: &GtiffDoc) -> crate::estimate::OutputEstimate {
    let mut payload = 0u64;
    let mut tiles = 0u64;
    for lv in &doc.levels {
        payload = payload.saturating_add(lv.payload_bytes);
        tiles += lv.tiles_total;
    }
    let gen_tiles: u64 = doc
        .generated
        .iter()
        .map(|&(w, h)| {
            let across = (w as u64).div_ceil(doc.levels[0].tile_w as u64);
            let down = (h as u64).div_ceil(doc.levels[0].tile_h as u64);
            across.saturating_mul(down)
        })
        .sum();
    payload = payload.saturating_add(gen_tiles.saturating_mul(GEN_TILE_BYTES_PROXY));
    tiles += gen_tiles;
    let extras = doc
        .levels
        .iter()
        .map(|l| l.jpeg_tables.as_ref().map_or(0, |t| t.len() as u64))
        .sum::<u64>()
        .saturating_add(doc.icc.as_ref().map_or(0, |i| i.len() as u64));
    let ifds = (doc.levels.len() + doc.generated.len()) as u64;
    let out = payload
        .saturating_mul(5)
        .div_ceil(4)
        .saturating_add(tiles.saturating_mul(16))
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(extras)
        .saturating_add(1024 * 1024);
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
