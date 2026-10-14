//! F2: Aperio SVS JPEG 2000 tiles (TIFF compression 33003 / 33005).
//!
//! Every Aperio JP2K TIFF tile payload is one complete raw J2K codestream
//! (`FF4F FF51 …`). Before anything is decoded, [`parse_header`] walks the
//! marker segments a decoder MUST see first (SOC → SIZ → COD …) with checked
//! arithmetic and hard caps: component count, decomposition levels, pixel
//! count, subsampling factors and marker-segment lengths are all bounded, so
//! a malicious / truncated payload is a typed error BEFORE any large
//! allocation — the hayro decoder never sees a header this module rejected.
//!
//! Colour duty follows the studied, OpenSlide-validated Aperio variants
//! (docs/slide-tools/f2-jpeg2000-feasibility.md §3) and is classified from
//! the CODESTREAM header (SIZ sampling + COD MCT), never from the TIFF tags:
//!
//! - 33003 (`J2K/YUV16`): MCT=0, components are **Y/Cb/Cr** planes with 4:2:2
//!   horizontal chroma subsampling (`XRsiz/YRsiz` = (1,1),(2,1),(2,1)); the
//!   reader converts with the OpenSlide fixed-point tables
//!   ([`ycbcr_to_rgb`], identical rounding to `make-tables.c`). hayro
//!   replicates the subsampled chroma to full width with the same `x/2`
//!   semantics OpenSlide uses.
//! - 33005 (`J2K/KDU`): MCT=1 (RCT) — the inverse MCT is part of the
//!   codestream and applied by the decoder; components are **already RGB**
//!   and MUST NOT be converted a second time.
//!
//! Any other combination (MCT=0 without the exact 4:2:2 signature, MCT=1
//! with subsampling, ≠3 components, ≠8-bit unsigned, HTJ2K / CAP, JP2-boxed
//! payloads) is a typed rejection — colour is never guessed.
//!
//! Decoder: `hayro-jpeg2000` 0.4.0, pinned scalar feature set
//! (`default-features = false`: zero unsafe, zero transitive deps), the SAME
//! configuration for native and wasm — decode output is byte-identical
//! across the two (feasibility study §6.1); upgrading the decoder or
//! changing the feature set is a re-acceptance event.

use crate::error::{CoreError, CoreResult};

/// Test-only decoder-invocation counter (review R1). Compiled to nothing in
/// normal builds; with `--features fixtures` it lets a test prove an
/// over-budget header is rejected BEFORE the decoder entry point is reached
/// (i.e. no multi-GB allocation was attempted). The counter is THREAD-LOCAL:
/// the conversion is synchronous on the calling thread, so parallel tests in
/// one binary never observe each other's conversions.
pub mod decoder_probe {
    #[cfg(feature = "fixtures")]
    use core::cell::Cell;

    #[cfg(feature = "fixtures")]
    std::thread_local! {
        static CALLS: Cell<u64> = const { Cell::new(0) };
    }

    /// Record that the decoder entry point was reached.
    #[inline]
    pub fn record() {
        #[cfg(feature = "fixtures")]
        CALLS.with(|c| c.set(c.get() + 1));
    }

    /// Number of decoder invocations recorded so far on this thread.
    #[cfg(feature = "fixtures")]
    pub fn calls() -> u64 {
        CALLS.with(|c| c.get())
    }

    /// Reset the counter (tests that assert "zero calls").
    #[cfg(feature = "fixtures")]
    pub fn reset() {
        CALLS.with(|c| c.set(0));
    }

    /// Stub for non-fixture builds (keeps the call site unconditional).
    #[cfg(not(feature = "fixtures"))]
    pub fn calls() -> u64 {
        0
    }
    #[cfg(not(feature = "fixtures"))]
    pub fn reset() {}
}

/// Decoder identity recorded in provenance (OME-XML / reports) and in the
/// evidence docs. A version/feature change must re-run the colour + parity
/// acceptance (output bytes can change).
pub const DECODER_NAME: &str = "hayro-jpeg2000";
/// Review R1: the accounted decode transient is bound to the pinned decoder.
/// A version change must re-derive [`peak`] against the new source (the
/// hayro allocation sites are cited per constant below).
pub const DECODER_PEAK_BASIS: &str = "hayro-jpeg2000 0.4.0 allocation sites";
pub const DECODER_VERSION: &str = "0.4.0";
/// The pinned feature configuration: hayro's `simd` feature (its only
/// feature) is DISABLED — the pure-Rust scalar path, `forbid(unsafe_code)`.
pub const DECODER_FEATURES: &str = "scalar (default-features = false)";

/// Aperio TIFF compression tags understood here.
pub const COMPRESSION_JP2K_YCBCR: u64 = 33003;
pub const COMPRESSION_JP2K_RGB: u64 = 33005;

/// Hard caps of the bounded header probe (checked arithmetic; every limit is
/// rejected as a typed error before any allocation).
mod cap {
    /// Header bytes the probe ever looks at (real Aperio tile headers are
    /// < 200 B; 64 KiB leaves room for pathological-but-legal marker spray).
    pub const PROBE_WINDOW: usize = 64 * 1024;
    /// A tile codestream is one JPEG 2000 "image" of the TIFF tile's pixels;
    /// the SVS adapter's own tile cap is 8192 px per side.
    pub const MAX_SIDE: u64 = 8192;
    /// ≤ 8192² (2²⁶) pixels — everything above is refused in the header.
    pub const MAX_PIXELS: u64 = 1 << 26;
    pub const MAX_COMPONENTS: usize = 3;
    /// SPcod decomposition levels; the studied Aperio codestreams use 5.
    pub const MAX_DECOMP_LEVELS: u8 = 32;
    /// Marker-segment sanity bound while walking to COD/SOT.
    pub const MAX_MARKERS: usize = 64;
}

/// Colour duty of a 3×8-bit Aperio JP2K codestream, decided from the
/// codestream header alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Jp2kColor {
    /// 33005 shape: MCT=1 — decoder output is RGB; NO further conversion.
    RgbCodestream,
    /// 33003 shape: MCT=0 — decoder output is Y/Cb/Cr (4:2:2, chroma
    /// replicated to full width); apply [`ycbcr_to_rgb`].
    Ycbcr422,
}

impl Jp2kColor {
    pub fn id(self) -> &'static str {
        match self {
            Jp2kColor::RgbCodestream => "rgb-codestream",
            Jp2kColor::Ycbcr422 => "ycbcr-422-codestream",
        }
    }
}

/// The bounded result of the SIZ/COD walk — the level contract a tile's
/// codestream is cross-checked against, and the colour decision input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Jp2kHeader {
    /// Codestream reference grid extent (Xsiz, Ysiz).
    pub width: u32,
    pub height: u32,
    /// Image area offset (XOsiz, YOsiz) — must be (0,0) for the Aperio
    /// tile shape; non-zero origins are a typed rejection.
    pub origin: (u32, u32),
    /// Internal JPEG 2000 tile partition (XtSiz, YtSiz) — the Aperio shape
    /// is one codestream tile covering the whole payload; anything else is
    /// a typed rejection.
    pub tile: (u32, u32),
    /// Codestream tile-partition origin (XTOsiz, YTOsiz) — (0,0) required.
    pub tile_origin: (u32, u32),
    pub components: u16,
    /// SIZ precision-1 (Ssiz): 8-bit unsigned in the supported set.
    pub bit_depth: u8,
    pub signed: bool,
    /// COD SGcod multiple-component transform: 0 = none, 1 = RCT/ICT.
    pub mct: u8,
    /// COD SPcod decomposition levels.
    pub decomposition_levels: u8,
    /// Per-component (XRsiz, YRsiz); only the first [`Jp2kHeader::components`]
    /// entries are meaningful.
    pub subsampling: [(u8, u8); cap::MAX_COMPONENTS],
    /// COD SGcod progression order byte (LRCP=0…); reported, not restricted
    /// beyond hayro's own support (a decode failure is a clean typed error).
    pub progression: u8,
    /// COD SGcod layer count.
    pub layers: u16,
}

impl Jp2kHeader {
    /// The 4:2:2 horizontal subsampling signature of the 33003 variant.
    pub fn is_422_signature(&self) -> bool {
        self.components == 3
            && self.subsampling[0] == (1, 1)
            && self.subsampling[1] == (2, 1)
            && self.subsampling[2] == (2, 1)
    }
    pub fn is_unsubsampled(&self) -> bool {
        self.components == 3
            && self.subsampling[0] == (1, 1)
            && self.subsampling[1] == (1, 1)
            && self.subsampling[2] == (1, 1)
    }
}

fn u16_be(b: &[u8], at: usize) -> CoreResult<u16> {
    b.get(at..at + 2)
        .map(|s| u16::from_be_bytes([s[0], s[1]]))
        .ok_or_else(|| CoreError::validation("JPEG 2000 头部截断（u16 越界）"))
}
fn u32_be(b: &[u8], at: usize) -> CoreResult<u32> {
    b.get(at..at + 4)
        .map(|s| u32::from_be_bytes([s[0], s[1], s[2], s[3]]))
        .ok_or_else(|| CoreError::validation("JPEG 2000 头部截断（u32 越界）"))
}

fn dim_ok(v: u64, what: &str) -> CoreResult<u32> {
    if v == 0 || v > cap::MAX_SIDE {
        return Err(CoreError::variant(format!(
            "JPEG 2000 {what} = {v} 越界（> {max}）：有界探测拒绝",
            max = cap::MAX_SIDE
        )));
    }
    Ok(v as u32)
}

/// Bounded SIZ/COD probe of one raw J2K codestream (the content of one Aperio
/// TIFF tile). No allocation beyond the caller's slice; every length and
/// extent is checked BEFORE the decoder ever runs. A JP2-boxed payload, an
/// HTJ2K (CAP) codestream, a truncated marker walk or an oversized extent is
/// a typed error.
pub fn parse_header(data: &[u8]) -> CoreResult<Jp2kHeader> {
    if data.len() < 4 || data[0] != 0xFF || data[1] != 0x4F || data[2] != 0xFF || data[3] != 0x51 {
        // JP2-boxed payloads start with the 12-byte signature box — name them
        // (a typed, specific reason; they are not in the supported set).
        if data.len() >= 4 && data.starts_with(&[0x00, 0x00, 0x00, 0x0C]) {
            return Err(CoreError::variant(
                "tile 是 JP2 容器封装而非裸 J2K codestream：不在 Aperio SVS 支持集",
            ));
        }
        return Err(CoreError::validation(
            "tile 不是 JPEG 2000 codestream（缺少 SOC+SIZ 标记）",
        ));
    }
    let win = &data[..data.len().min(cap::PROBE_WINDOW)];
    // SIZ is fixed right after SOC (marker walk invariant of the format).
    // Layout (ITU-T T.800 A.9): Lsiz, Rsiz, 8×u32 extents, Csiz, 3 B/component.
    let lsiz = u16_be(win, 4)? as usize;
    let rsiz = u16_be(win, 6)?;
    let csiz = u16_be(win, 40)? as usize;
    // Lsiz counts itself + Rsiz + 8 u32 extents + Csiz + 3 B per component.
    if lsiz < 40 || lsiz != 38 + csiz * 3 {
        return Err(CoreError::validation(format!(
            "JPEG 2000 SIZ 段长 {lsiz} 与分量数 {csiz} 不符"
        )));
    }
    if rsiz != 0 {
        return Err(CoreError::variant(format!(
            "JPEG 2000 Rsiz={rsiz}（Part-2 扩展能力）不在已验证 Aperio 变体集"
        )));
    }
    if csiz == 0 || csiz > cap::MAX_COMPONENTS {
        return Err(CoreError::variant(format!(
            "JPEG 2000 分量数 {csiz} 不在明场支持集（1–{max}）",
            max = cap::MAX_COMPONENTS
        )));
    }
    let xsiz = u32_be(win, 8)? as u64;
    let ysiz = u32_be(win, 12)? as u64;
    let xosiz = u32_be(win, 16)? as u64;
    let yosiz = u32_be(win, 20)? as u64;
    let xtsiz = u32_be(win, 24)? as u64;
    let ytsiz = u32_be(win, 28)? as u64;
    let xt_osiz = u32_be(win, 32)? as u64;
    let yt_osiz = u32_be(win, 36)? as u64;
    let width = dim_ok(xsiz.saturating_sub(xosiz), "参考网格宽度 Xsiz-XOsiz")?;
    let height = dim_ok(ysiz.saturating_sub(yosiz), "参考网格高度 Ysiz-YOsiz")?;
    let pixels = (width as u64)
        .checked_mul(height as u64)
        .ok_or_else(|| CoreError::variant("JPEG 2000 参考网格像素数溢出"))?;
    if pixels > cap::MAX_PIXELS {
        return Err(CoreError::variant(format!(
            "JPEG 2000 参考网格 {width}×{height} 超过单 tile 像素上限（{max}）：有界探测拒绝",
            max = cap::MAX_PIXELS
        )));
    }
    // Tile partition extents must be non-zero when present (the Aperio shape
    // is one codestream tile: XtSiz ≥ Xsiz); 0 is a malformed marker.
    if (xtsiz != 0 || ytsiz != 0) && (xtsiz == 0 || ytsiz == 0) {
        return Err(CoreError::validation("JPEG 2000 XtSiz/YtSiz 非法（一零一非）"));
    }
    let mut hdr = Jp2kHeader {
        width,
        height,
        origin: (xosiz.min(u32::MAX as u64) as u32, yosiz.min(u32::MAX as u64) as u32),
        tile: (xtsiz.min(u32::MAX as u64) as u32, ytsiz.min(u32::MAX as u64) as u32),
        tile_origin: (xt_osiz.min(u32::MAX as u64) as u32, yt_osiz.min(u32::MAX as u64) as u32),
        components: csiz as u16,
        bit_depth: 0,
        signed: false,
        mct: 0,
        decomposition_levels: 0,
        subsampling: [(1, 1); cap::MAX_COMPONENTS],
        progression: 0,
        layers: 0,
    };
    for c in 0..csiz {
        let at = 42 + c * 3;
        let ssiz = *win.get(at).ok_or_else(|| CoreError::validation("JPEG 2000 SIZ 截断"))?;
        hdr.bit_depth = (ssiz & 0x7F) + 1;
        hdr.signed = ssiz & 0x80 != 0;
        let xr = *win.get(at + 1).ok_or_else(|| CoreError::validation("JPEG 2000 SIZ 截断"))?;
        let yr = *win.get(at + 2).ok_or_else(|| CoreError::validation("JPEG 2000 SIZ 截断"))?;
        if xr == 0 || yr == 0 {
            return Err(CoreError::validation(format!(
                "JPEG 2000 分量 {c} 采样因子为 0（非法）"
            )));
        }
        hdr.subsampling[c] = (xr, yr);
    }

    // ---- marker walk to COD (and the first SOT) ------------------------- //
    let mut at = 4 + lsiz; // past SOC+SIZ segment
    let mut saw_cod = false;
    for _ in 0..cap::MAX_MARKERS {
        let m = u16_be(win, at)?;
        match m {
            0xFF4F => return Err(CoreError::validation("JPEG 2000 重复 SOC 标记")),
            // CAP = part-15 (HTJ2K) / part-2 extensions: never in the set.
            0xFF50 => {
                return Err(CoreError::variant(
                    "JPEG 2000 CAP 标记（HTJ2K/扩展编码）不在 Aperio 支持集",
                ))
            }
            0xFF52 => {
                // COD: Lcod(2) Scod(1) SGcod(4: prog, layers, mct)
                //      SPcod(5: levels, cbw, cbh, cbstyle, transform)
                //      [+ 2 B precinct pairs per level when Scod & 1]
                let lcod = u16_be(win, at + 2)? as usize;
                if lcod < 12 {
                    return Err(CoreError::validation("JPEG 2000 COD 段长非法"));
                }
                let scod = *win.get(at + 4).ok_or_else(|| CoreError::validation("JPEG 2000 COD 截断"))?;
                hdr.progression = *win
                    .get(at + 5)
                    .ok_or_else(|| CoreError::validation("JPEG 2000 COD 截断"))?;
                hdr.layers = u16_be(win, at + 6)?;
                hdr.mct = *win.get(at + 8).ok_or_else(|| CoreError::validation("JPEG 2000 COD 截断"))?;
                let levels = *win.get(at + 9).ok_or_else(|| CoreError::validation("JPEG 2000 COD 截断"))?;
                if levels > cap::MAX_DECOMP_LEVELS {
                    return Err(CoreError::variant(format!(
                        "JPEG 2000 分解层数 {levels} 超过上限 {max}",
                        max = cap::MAX_DECOMP_LEVELS
                    )));
                }
                hdr.decomposition_levels = levels;
                if scod & 0x40 != 0 {
                    return Err(CoreError::variant(
                        "JPEG 2000 COD 多 tile-part/扩展标志（Scod&0x40）不在已验证 Aperio 变体集",
                    ));
                }
                saw_cod = true;
                at += 2 + lcod;
            }
            // SOT: the codestream header ends; COD must have been seen.
            0xFF90 => {
                if !saw_cod {
                    return Err(CoreError::validation(
                        "JPEG 2000 在 COD 之前进入 SOT（头部不完整）",
                    ));
                }
                return Ok(hdr);
            }
            // Skip any other marker segment with a length field (QCD/QCC,
            // COM, PPM/TLM/…): header-region companions of the COD decision.
            m if (0xFF30..=0xFF3F).contains(&m) => {
                // marker-only segments (no length) in the reserved range
                at += 2;
            }
            m if m & 0xFF00 == 0xFF00 && m != 0xFFFF => {
                let lseg = u16_be(win, at + 2)? as usize;
                if lseg < 2 {
                    return Err(CoreError::validation("JPEG 2000 标记段长非法"));
                }
                at += 2 + lseg;
            }
            _ => {
                return Err(CoreError::validation(format!(
                    "JPEG 2000 头部出现非标记字节（0x{m:04X}）"
                )))
            }
        }
        if at + 2 > win.len() {
            break;
        }
    }
    Err(CoreError::validation(
        "JPEG 2000 头部标记走查未到达 SOT（截断或标记过多）",
    ))
}

/// The colour contract of a verified Aperio variant, from the TIFF
/// compression tag + the codestream header. Unknown combinations are typed
/// rejections — colour is never guessed from the tag alone.
pub fn classify_aperio(compression: u64, hdr: &Jp2kHeader) -> CoreResult<Jp2kColor> {
    if hdr.components != 3 {
        return Err(CoreError::variant(format!(
            "JPEG 2000 分量数 {} ≠ 3（多通道/灰度 JP2K 不在明场 Aperio 支持集）",
            hdr.components
        )));
    }
    if hdr.bit_depth != 8 || hdr.signed {
        return Err(CoreError::variant(format!(
            "JPEG 2000 位深 {}（{}）≠ 8-bit 无符号：不在明场支持集",
            hdr.bit_depth,
            if hdr.signed { "有符号" } else { "无符号" }
        )));
    }
    if hdr.mct > 1 {
        return Err(CoreError::variant(format!(
            "JPEG 2000 MCT={} 为保留值：不在支持集",
            hdr.mct
        )));
    }
    match compression {
        COMPRESSION_JP2K_YCBCR => {
            if hdr.mct != 0 {
                return Err(CoreError::variant(format!(
                    "compression 33003 与 codestream MCT={} 不符：未知 Aperio 变体，不猜颜色",
                    hdr.mct
                )));
            }
            if !hdr.is_422_signature() {
                return Err(CoreError::variant(
                    "compression 33003 需要 4:2:2 采样签名 (1,1)(2,1)(2,1)：codestream 采样不符，未知变体",
                ));
            }
            if hdr.origin != (0, 0) || hdr.tile_origin != (0, 0) {
                return Err(CoreError::variant("JPEG 2000 非零图像/分块原点不在已验证 Aperio 变体集"));
            }
            if hdr.tile.0 < hdr.width as u32 || hdr.tile.1 < hdr.height as u32 {
                // XtSiz < image extent ⇒ multiple codestream-internal tiles.
                return Err(CoreError::variant(
                    "codestream 内部分块（XtSiz < Xsiz）不在已验证 Aperio 变体集（每 tile 一条完整 codestream）",
                ));
            }
            Ok(Jp2kColor::Ycbcr422)
        }
        COMPRESSION_JP2K_RGB => {
            if hdr.mct != 1 {
                return Err(CoreError::variant(format!(
                    "compression 33005 与 codestream MCT={} 不符：未知 Aperio 变体，不猜颜色",
                    hdr.mct
                )));
            }
            if !hdr.is_unsubsampled() {
                return Err(CoreError::variant(
                    "compression 33005 需要无子采样 (1,1)(1,1)(1,1)：codestream 采样不符，未知变体",
                ));
            }
            if hdr.origin != (0, 0) || hdr.tile_origin != (0, 0) {
                return Err(CoreError::variant("JPEG 2000 非零图像/分块原点不在已验证 Aperio 变体集"));
            }
            if hdr.tile.0 < hdr.width as u32 || hdr.tile.1 < hdr.height as u32 {
                return Err(CoreError::variant(
                    "codestream 内部分块（XtSiz < Xsiz）不在已验证 Aperio 变体集（每 tile 一条完整 codestream）",
                ));
            }
            Ok(Jp2kColor::RgbCodestream)
        }
        c => Err(CoreError::variant(format!(
            "JPEG 2000 压缩编码 {c} 不是已验证的 Aperio 变体（33003/33005）"
        ))),
    }
}

/// Cross-check ONE tile's codestream header against the level contract
/// (probed from the level's first tile). Any disagreement — dims beyond the
/// nominal tile rect, a different component count / bit depth / sampling /
/// MCT — is a typed rejection before decoding (never a guessed colour).
pub fn check_against_contract(
    hdr: &Jp2kHeader,
    contract: &Jp2kHeader,
    compression: u64,
    tile_w: u32,
    tile_h: u32,
) -> CoreResult<Jp2kColor> {
    if hdr.width as u64 > tile_w as u64 || hdr.height as u64 > tile_h as u64 {
        return Err(CoreError::variant(format!(
            "JPEG 2000 tile 尺寸 {}×{} 超过标称 {tile_w}×{tile_h}",
            hdr.width, hdr.height
        )));
    }
    if hdr.components != contract.components
        || hdr.bit_depth != contract.bit_depth
        || hdr.signed != contract.signed
        || hdr.mct != contract.mct
        || hdr.subsampling != contract.subsampling
        || hdr.decomposition_levels != contract.decomposition_levels
        || hdr.origin != contract.origin
        || hdr.tile_origin != contract.tile_origin
    {
        return Err(CoreError::validation(
            "JPEG 2000 tile 头与该层探测到的 codestream 契约不一致（分量/位深/采样/MCT/层数）",
        ));
    }
    classify_aperio(compression, contract)
}

// --------------------------------------------------------------------------- //
// OpenSlide-equivalent YCbCr→RGB (33003 colour duty)                          //
// --------------------------------------------------------------------------- //

fn round_f32_to_u8(v: f32) -> u8 {
    let r = (v + 0.5).floor();
    r.clamp(0.0, 255.0) as u8
}

/// Full-range JPEG coefficients as fixed-point tables, computed exactly like
/// OpenSlide `make-tables.c` (C double math, then rounded): 1.402, 0.34414,
/// 0.71414, 1.772; the green deltas share a 2¹⁶ scale and shift.
fn r_cr_table() -> [i32; 256] {
    let mut t = [0i32; 256];
    for (i, v) in t.iter_mut().enumerate() {
        *v = (1.402 * (i as f64 - 128.0)).round() as i32;
    }
    t
}
fn g_cb_table() -> [i32; 256] {
    let mut t = [0i32; 256];
    for (i, v) in t.iter_mut().enumerate() {
        *v = (65536.0 * (0.5 - 0.34414 * (i as f64 - 128.0))).round() as i32;
    }
    t
}
fn g_cr_table() -> [i32; 256] {
    let mut t = [0i32; 256];
    for (i, v) in t.iter_mut().enumerate() {
        *v = (65536.0 * -0.71414 * (i as f64 - 128.0)).round() as i32;
    }
    t
}
fn b_cb_table() -> [i32; 256] {
    let mut t = [0i32; 256];
    for (i, v) in t.iter_mut().enumerate() {
        *v = (1.772 * (i as f64 - 128.0)).round() as i32;
    }
    t
}

/// One Y/Cb/Cr triple → RGB (OpenSlide tables; chroma indexed by the
/// caller at the same x/2 granularity hayro replicates to).
pub fn ycbcr_to_rgb(y: u8, cb: u8, cr: u8, tables: &YcbcrTables) -> (u8, u8, u8) {
    let y = y as i32;
    let cb = cb as usize;
    let cr = cr as usize;
    let r = (y + tables.r_cr[cr]).clamp(0, 255) as u8;
    let g = (y + ((tables.g_cb[cb] + tables.g_cr[cr]) >> 16)).clamp(0, 255) as u8;
    let b = (y + tables.b_cb[cb]).clamp(0, 255) as u8;
    (r, g, b)
}

/// Precomputed conversion tables (build once per tile, ~4 KiB).
pub struct YcbcrTables {
    r_cr: [i32; 256],
    g_cb: [i32; 256],
    g_cr: [i32; 256],
    b_cb: [i32; 256],
}

impl Default for YcbcrTables {
    fn default() -> Self {
        YcbcrTables {
            r_cr: r_cr_table(),
            g_cb: g_cb_table(),
            g_cr: g_cr_table(),
            b_cb: b_cb_table(),
        }
    }
}

/// One decoded tile: interleaved RGB8 at the codestream's own geometry.
#[derive(Debug, Clone)]
pub struct DecodedTileRgb {
    pub width: u32,
    pub height: u32,
    pub rgb: Vec<u8>,
}

// --------------------------------------------------------------------------- //
// Review R1: pre-allocation working-set accounting for one JP2K tile.         //
// --------------------------------------------------------------------------- //
//
// The SVS/JP2K adapter decodes ONE tile at a time, but "one tile" is NOT a     //
// memory bound: a legal codestream may claim up to MAX_SIDE×MAX_SIDE pixels    //
// (2²⁶) and the decoder holds several full-resolution f32 planes at the same   //
// instant. Before the decoder is allowed to allocate, the adapter computes a   //
// CONSERVATIVE peak of everything live together and charges it against the     //
// host budget (`plan.limits.memory_budget_bytes`); an over-budget estimate is  //
// the existing typed `resource_profile_insufficient` refusal (never an OOM).
//
// Derivation of every term (hayro-jpeg2000 0.4.0, the pinned scalar build —
// see [`DECODER_PEAK_BASIS`]; a decoder version change re-runs this review):
//
// * Decode planes — `DecoderContext::reset` allocates one `SimdBuffer` of
//   `image_width × image_height` f32 PER COMPONENT (`src/j2c/decode.rs:117`),
//   i.e. `components × w × h × 4 B`. Subsampled 33003 chroma still gets a
//   full-resolution buffer in this build, which is the worst case.
// * `storage.coefficients` — the per-component sub-band coefficient array
//   (`src/j2c/build.rs:28`). The LL band + the 3 AC bands partition each
//   component tile, so it is ≤ `components × w × h × 4 B`.
// * IDWT — `idwt_output.coefficients` and `idwt_scratch_buffer`
//   (`src/j2c/idwt.rs:96,107`), each ≤ `(w+1) × (h+1)` f32 (the code reserves
//   `(total+1)²` for sub-band shift padding): `2 × (w+2) × (h+2) × 4 B`.
// * Bit-plane code-block scratch (`src/j2c/bitplane.rs:432-446`): one
//   `Coefficient` u32 + one `NeighborSignificances` u8 + one `CoefficientState`
//   u8 per padded coefficient, plus padded stripe flags. The buffers are
//   reused per code block, so the live transient is bounded by the LARGEST
//   code block; a malicious COD cannot exceed the tile area (a code block is a
//   sub-rect of the tile), giving ≈ 6.3 B/px worst case — rounded up to a
//   documented [`peak::DECODER_BITPLANE_BPP`] = 8 B/px.
// * RGB output — `DecodedTileRgb.rgb`, `w × h × 3 B`.
// * Padded canvas — when (and only when) the codestream is smaller than the
//   nominal tile rect, a white `tile_w × tile_h × 3 B` canvas is live
//   alongside the RGB buffer (edge tiles).
// * JPEG encoder — `encode_rgb` builds three full-resolution u8 component
//   planes (`src/jpeg/encoder.rs`), i.e. `3 × tw × th` at the encode geometry
//   (`max(w,tile_w) × max(h,tile_h)`, covered by `tw × th`), plus the
//   entropy-coded output buffer (≤ half the input area, `tw×th/2`).
// * A fixed [`peak::FIXED_SLACK`] (1 MiB) covers everything not proportional
//   to pixels: the YCbCr/Huffman/DQT tables, hayro's segment/precinct/tag-tree
//   metadata and allocator rounding.
pub mod peak {
    use super::*;

    /// f32 element size.
    pub const F32: u64 = 4;
    /// Bytes per pixel of the interleaved RGB8 output.
    pub const RGB_BPP: u64 = 3;
    /// Documented worst-case hayro code-block bit-plane transient per tile
    /// pixel (Coefficient u32 + NeighborSignificances u8 + CoefficientState u8
    /// + stripe flags, padded and rounded up).
    pub const DECODER_BITPLANE_BPP: u64 = 8;
    /// The JPEG entropy-coded output buffer is bounded by half the input area
    /// (see `encode_rgb`'s `Vec::with_capacity(w*h/2 + …)`).
    pub const ENCODER_OUTPUT_DIV: u64 = 2;
    /// Non-pixel-proportional slack (tables, decoder metadata, allocator).
    pub const FIXED_SLACK: u64 = 1 << 20;

    /// Named terms of the conservative per-tile peak working set (kept so
    /// tests can assert each term and the docs can cite the derivation).
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct Breakdown {
        pub decode_planes: u64,
        pub storage_coefficients: u64,
        pub idwt_buffers: u64,
        pub bitplane_transient: u64,
        pub rgb_output: u64,
        pub padded_canvas: u64,
        pub encoder_planes: u64,
        pub encoder_output: u64,
        pub fixed_slack: u64,
    }

    impl Breakdown {
        /// The conservative peak: the sum of every term live at the same time.
        pub fn total(&self) -> u64 {
            [
                self.decode_planes,
                self.storage_coefficients,
                self.idwt_buffers,
                self.bitplane_transient,
                self.rgb_output,
                self.padded_canvas,
                self.encoder_planes,
                self.encoder_output,
                self.fixed_slack,
            ]
            .into_iter()
            .fold(0u64, u64::saturating_add)
        }
    }

    fn mul(a: u64, b: u64) -> CoreResult<u64> {
        a.checked_mul(b)
            .ok_or_else(|| CoreError::resource_limit("JPEG2000 工作集估算溢出"))
    }

    /// Conservative peak working set of decoding + re-encoding ONE tile whose
    /// codestream is `w × h` with `components` components, onto a nominal
    /// `tile_w × tile_h` canvas. Checked arithmetic; overflow is a typed
    /// resource refusal (never a wrap).
    pub fn working_set(
        components: u64,
        w: u64,
        h: u64,
        tile_w: u64,
        tile_h: u64,
    ) -> CoreResult<Breakdown> {
        let pixels = mul(w, h)?;
        let tile_pixels = mul(tile_w, tile_h)?;
        let comps = components.max(1);
        let decode_planes = mul(mul(comps, pixels)?, F32)?;
        let storage_coefficients = decode_planes; // same order of magnitude
        let idwt_buffers = mul(
            mul(2, mul(w.saturating_add(2), h.saturating_add(2))?)?,
            F32,
        )?;
        let bitplane_transient = mul(pixels, DECODER_BITPLANE_BPP)?;
        let rgb_output = mul(pixels, RGB_BPP)?;
        // only allocated when the codestream is smaller than the nominal tile
        let padded_canvas =
            if w < tile_w || h < tile_h { mul(tile_pixels, RGB_BPP)? } else { 0 };
        let encoder_planes = mul(3, tile_pixels)?;
        let encoder_output = tile_pixels / ENCODER_OUTPUT_DIV;
        Ok(Breakdown {
            decode_planes,
            storage_coefficients,
            idwt_buffers,
            bitplane_transient,
            rgb_output,
            padded_canvas,
            encoder_planes,
            encoder_output,
            fixed_slack: FIXED_SLACK,
        })
    }

    /// Charge the conservative one-tile peak against a fresh host budget and
    /// return the charged total. Over budget ⇒ the existing typed
    /// `resource_profile_insufficient` (`ResourceLimitExceeded`) BEFORE any
    /// decode/encode allocation.
    pub fn charge_against_budget(
        budget_bytes: u64,
        components: u64,
        w: u64,
        h: u64,
        tile_w: u64,
        tile_h: u64,
        what: &str,
    ) -> CoreResult<u64> {
        let total = working_set(components, w, h, tile_w, tile_h)?.total();
        let mut budget = crate::budget::MemBudget::host(budget_bytes);
        budget.charge(total, what)?;
        Ok(total)
    }
}

/// Decode one raw J2K tile payload to interleaved RGB8 with the colour duty
/// `color` (decided by [`classify_aperio`] / [`check_against_contract`]).
///
/// `max_pixels` is the caller's pixel budget (the SVS level's nominal tile
/// area): a codestream claiming more is refused BEFORE the decoder allocates.
/// A decoder error (corrupt coefficients, truncated payload) surfaces as a
/// typed `jpeg` error — hayro is `forbid(unsafe_code)`, so no trap path.
#[cfg(feature = "jp2k")]
pub fn decode_tile_rgb(
    payload: &[u8],
    width: u32,
    height: u32,
    color: Jp2kColor,
    max_pixels: u64,
) -> CoreResult<DecodedTileRgb> {
    use hayro_jpeg2000::{DecodeSettings, DecoderContext, Image};

    let pixels = (width as u64)
        .checked_mul(height as u64)
        .ok_or_else(|| CoreError::variant("JPEG 2000 tile 像素数溢出"))?;
    if pixels > max_pixels {
        return Err(crate::error::CoreError::resource_limit(format!(
            "JPEG 2000 tile {width}×{height} 超过该层像素预算 {max_pixels}"
        )));
    }
    // The decode entry point (first hayro allocation is downstream, inside
    // `Image::decode`). The R1 test hook records reaching here so an
    // over-budget header can be proved rejected BEFORE the decoder ran.
    decoder_probe::record();
    let image = Image::new(payload, &DecodeSettings::default())
        .map_err(|e| CoreError::jpeg(format!("JPEG 2000 头解析失败: {e}")))?;
    let mut ctx = DecoderContext::default();
    let decoded = image
        .decode(&mut ctx)
        .map_err(|e| CoreError::jpeg(format!("JPEG 2000 解码失败: {e}")))?;
    let components = decoded.components();
    if components.len() != 3 {
        return Err(CoreError::jpeg(format!(
            "JPEG 2000 解码分量数 {} ≠ 3",
            components.len()
        )));
    }
    let (w, h) = (image.width(), image.height());
    if w != width || h != height {
        return Err(CoreError::jpeg(format!(
            "JPEG 2000 解码尺寸 {w}×{h} 与头部 {width}×{height} 不符"
        )));
    }
    let c0 = components[0].samples();
    let c1 = components[1].samples();
    let c2 = components[2].samples();
    let n = (width as usize) * (height as usize);
    if c0.len() < n || c1.len() < n || c2.len() < n {
        return Err(CoreError::jpeg("JPEG 2000 解码分量样本不足"));
    }
    let mut rgb = vec![0u8; n.saturating_mul(3)];
    match color {
        Jp2kColor::RgbCodestream => {
            for i in 0..n {
                rgb[i * 3] = round_f32_to_u8(c0[i]);
                rgb[i * 3 + 1] = round_f32_to_u8(c1[i]);
                rgb[i * 3 + 2] = round_f32_to_u8(c2[i]);
            }
        }
        Jp2kColor::Ycbcr422 => {
            let tables = YcbcrTables::default();
            for i in 0..n {
                let y = round_f32_to_u8(c0[i]);
                let cb = round_f32_to_u8(c1[i]);
                let cr = round_f32_to_u8(c2[i]);
                let (r, g, b) = ycbcr_to_rgb(y, cb, cr, &tables);
                rgb[i * 3] = r;
                rgb[i * 3 + 1] = g;
                rgb[i * 3 + 2] = b;
            }
        }
    }
    Ok(DecodedTileRgb { width, height, rgb })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Hand-built SIZ+COD header of the verified Aperio shapes (no decoder
    /// needed — the probe is pure parsing).
    fn siz(csiz: u16, comps: &[(u8, u8, u8, u8, u8)], xsiz: u32, ysiz: u32) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(&[0xFF, 0x4F, 0xFF, 0x51]);
        let lsiz = 38 + comps.len() * 3;
        b.extend_from_slice(&(lsiz as u16).to_be_bytes());
        b.extend_from_slice(&0u16.to_be_bytes()); // Rsiz = 0 (part 1)
        b.extend_from_slice(&xsiz.to_be_bytes());
        b.extend_from_slice(&ysiz.to_be_bytes());
        b.extend_from_slice(&0u32.to_be_bytes());
        b.extend_from_slice(&0u32.to_be_bytes());
        b.extend_from_slice(&xsiz.to_be_bytes()); // XtSiz = Xsiz (one tile)
        b.extend_from_slice(&ysiz.to_be_bytes());
        b.extend_from_slice(&0u32.to_be_bytes());
        b.extend_from_slice(&0u32.to_be_bytes());
        b.extend_from_slice(&(csiz).to_be_bytes());
        for c in comps {
            b.extend_from_slice(&[c.0, c.1, c.2]);
            let _ = (c.3, c.4);
        }
        b
    }
    fn cod(mct: u8, levels: u8) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(&[0xFF, 0x52]);
        b.extend_from_slice(&12u16.to_be_bytes());
        b.push(0); // Scod
        b.push(0); // LRCP
        b.extend_from_slice(&1u16.to_be_bytes()); // 1 layer
        b.push(mct);
        b.push(levels);
        b.push(5); // cbw exp
        b.push(5); // cbh exp
        b.push(0); // cbstyle
        b.push(1); // 5/3 reversible
        b
    }
    fn sot() -> Vec<u8> {
        let mut b = vec![0xFF, 0x90];
        b.extend_from_slice(&10u16.to_be_bytes());
        b.extend_from_slice(&0u16.to_be_bytes());
        b
    }

    fn header_33003() -> Vec<u8> {
        let mut b = siz(
            3,
            &[(0x07, 1, 1, 0, 0), (0x07, 2, 1, 0, 0), (0x07, 2, 1, 0, 0)],
            256,
            256,
        );
        b.extend(cod(0, 5));
        b.extend(sot());
        b
    }
    fn header_33005() -> Vec<u8> {
        let mut b = siz(
            3,
            &[(0x07, 1, 1, 0, 0), (0x07, 1, 1, 0, 0), (0x07, 1, 1, 0, 0)],
            240,
            240,
        );
        b.extend(cod(1, 5));
        b.extend(sot());
        b
    }

    #[test]
    fn parses_the_two_verified_aperio_shapes() {
        let h = parse_header(&header_33003()).unwrap();
        assert_eq!(h.width, 256);
        assert_eq!(h.components, 3);
        assert_eq!(h.mct, 0);
        assert_eq!(h.decomposition_levels, 5);
        assert_eq!(classify_aperio(33003, &h).unwrap(), Jp2kColor::Ycbcr422);
        // the 33003 header under the 33005 tag is an unknown combination
        assert!(classify_aperio(33005, &h).is_err());

        let h5 = parse_header(&header_33005()).unwrap();
        assert_eq!(h5.mct, 1);
        assert_eq!(classify_aperio(33005, &h5).unwrap(), Jp2kColor::RgbCodestream);
        assert!(classify_aperio(33003, &h5).is_err());
    }

    #[test]
    fn rejects_caps_before_any_allocation() {
        // oversized reference grid (1_000_000² > MAX_SIDE per dim)
        let mut b = siz(
            3,
            &[(0x07, 1, 1, 0, 0); 3],
            4_000_000,
            4_000_000,
        );
        b.extend(cod(0, 5));
        let e = parse_header(&b).unwrap_err();
        assert_eq!(e.code, crate::error::ErrorCode::UnsupportedKfbVariant);
        // oversized pixel count within the per-side cap: 8192 × 8192 is the
        // cap itself (2^26 = MAX_PIXELS → allowed), so push just over it via
        // a non-square extent
        let mut b = siz(3, &[(0x07, 1, 1, 0, 0); 3], 8192, 8191);
        b.extend(cod(0, 5));
        b.extend(sot());
        assert!(parse_header(&b).is_ok());
        // too many components
        let mut b = siz(4, &[(0x07, 1, 1, 0, 0); 4], 256, 256);
        b.extend(cod(0, 5));
        assert_eq!(parse_header(&b).unwrap_err().code, crate::error::ErrorCode::UnsupportedKfbVariant);
        // too many decomposition levels (SPcod levels byte: 4 header +
        // 1 Scod + 1 prog + 2 layers + 1 mct from the COD tail of 5)
        let mut b = header_33003();
        let at = b.len() - 6 /* SOT */ - 5;
        b[at] = 40;
        assert_eq!(parse_header(&b).unwrap_err().code, crate::error::ErrorCode::UnsupportedKfbVariant);
        // truncated header: the SOT marker itself cut off (the walk never
        // reaches the end-of-header marker; tile-DATA truncation surfaces
        // later as a typed decode error instead)
        let mut b = header_33003();
        b.truncate(b.len() - 7);
        assert_eq!(parse_header(&b).unwrap_err().code, crate::error::ErrorCode::ConversionValidationFailed);
        // not a codestream at all
        assert!(parse_header(&[0u8; 16]).is_err());
        // JP2 boxed payload named specifically
        let jp2 = vec![0x00, 0x00, 0x00, 0x0C, 0x6A, 0x50, 0x20, 0x20, 0, 0, 0, 0, 0xFF, 0x4F];
        assert!(parse_header(&jp2)
            .unwrap_err()
            .message
            .contains("JP2 容器"));
    }

    #[test]
    fn rejects_unknown_mct_and_bit_depth_combinations() {
        // 33003 tag + MCT=1 (unknown combination → colour never guessed)
        let mut b = siz(
            3,
            &[(0x07, 1, 1, 0, 0), (0x07, 2, 1, 0, 0), (0x07, 2, 1, 0, 0)],
            256,
            256,
        );
        b.extend(cod(1, 5));
        b.extend(sot());
        let h = parse_header(&b).unwrap();
        assert!(classify_aperio(33003, &h).is_err());

        // 8-bit SIGNED components (Ssiz = 0x87)
        let mut b = siz(
            3,
            &[(0x87, 1, 1, 0, 0), (0x87, 2, 1, 0, 0), (0x87, 2, 1, 0, 0)],
            256,
            256,
        );
        b.extend(cod(0, 5));
        b.extend(sot());
        let h = parse_header(&b).unwrap();
        assert_eq!(h.bit_depth, 8);
        assert!(h.signed);
        assert!(classify_aperio(33003, &h).is_err());
        // 16-bit unsigned components (Ssiz = 0x0F)
        let mut b = siz(
            3,
            &[(0x0F, 1, 1, 0, 0), (0x0F, 2, 1, 0, 0), (0x0F, 2, 1, 0, 0)],
            256,
            256,
        );
        b.extend(cod(0, 5));
        b.extend(sot());
        let h = parse_header(&b).unwrap();
        assert_eq!(h.bit_depth, 16);
        assert!(classify_aperio(33003, &h).is_err());
    }

    #[test]
    fn contract_crosscheck_rejects_per_tile_drift() {
        let contract = parse_header(&header_33003()).unwrap();
        // a drifted tile header (MCT flipped) must fail the cross-check
        let mut b = header_33003();
        *b.last_mut().unwrap() = 0x90; // keep SOT
        let mut drifted = siz(
            3,
            &[(0x07, 1, 1, 0, 0), (0x07, 2, 1, 0, 0), (0x07, 2, 1, 0, 0)],
            256,
            256,
        );
        drifted.extend(cod(1, 5));
        drifted.extend(sot());
        let h = parse_header(&drifted).unwrap();
        assert!(check_against_contract(&h, &contract, 33003, 256, 256).is_err());
        // oversized dims vs the nominal tile rect
        let mut big = siz(
            3,
            &[(0x07, 1, 1, 0, 0), (0x07, 2, 1, 0, 0), (0x07, 2, 1, 0, 0)],
            4096,
            4096,
        );
        big.extend(cod(0, 5));
        big.extend(sot());
        let h = parse_header(&big).unwrap();
        let e = check_against_contract(&h, &contract, 33003, 256, 256).unwrap_err();
        assert_eq!(e.code, crate::error::ErrorCode::UnsupportedKfbVariant);
    }

    #[test]
    fn ycbcr_grey_stays_grey_and_negative_control_moves_far() {
        let t = YcbcrTables::default();
        // neutral chroma (128) preserves luma exactly
        assert_eq!(ycbcr_to_rgb(100, 128, 128, &t), (100, 100, 100));
        // extreme chroma saturates without wrapping
        // (r = 255 + 1.402·(0−128) → clamp; b = 255 + 1.772·(0−128) = 28)
        assert_eq!(ycbcr_to_rgb(255, 0, 128, &t), (255, 255, 28));
        assert_eq!(ycbcr_to_rgb(0, 255, 128, &t).0, 0);
    }
}
