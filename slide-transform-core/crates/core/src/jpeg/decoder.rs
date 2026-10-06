//! Baseline JPEG decoder mirroring libjpeg-turbo's default decode path
//! bit-for-bit for the inputs we care about (grayscale, YCbCr 4:4:4 / 4:2:2 /
//! 4:2:0): islow IDCT with the exact post-IDCT range-limit table, h2v1/h2v2
//! "fancy" upsampling (box fallback when the downsampled width is ≤ 2) and
//! fixed-point YCbCr→RGB conversion. Decoding a tile twice — here and in the
//! Python oracle (Pillow/libjpeg-turbo) — must yield identical pixels, which
//! is what makes the edge-tile quality gate provable rather than statistical.
//!
//! Supported input subset (everything else fails with a typed error):
//!   - SOF0/SOF1 (baseline, sequential), 8-bit, 1 or 3 components
//!   - optional restart intervals; progressive / arithmetic → error
//!   - 3-component YCbCr (JFIF) or Adobe-APP14 RGB

use super::tables::*;
use crate::error::{CoreError, CoreResult};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ColorKind {
    Gray,
    Rgb,
}

#[derive(Debug, Clone)]
pub struct DecodedImage {
    pub width: u32,
    pub height: u32,
    pub kind: ColorKind,
    /// Interleaved samples: 1 byte/px (Gray) or 3 bytes/px (Rgb).
    pub data: Vec<u8>,
}

/// Header probe result mirroring `kfb.parser.scan_jpeg`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JpegProbe {
    pub width: u32,
    pub height: u32,
    /// 3-component sampling (h1,v1,h2,v2,h3,v3) or `None` for grayscale.
    pub sampling: Option<(u8, u8, u8, u8, u8, u8)>,
    /// Component ids of a 3-component frame (libjpeg colorspace heuristics).
    pub comp_ids: Option<(u8, u8, u8)>,
    /// A JFIF APP0 marker was seen (3 components + JFIF ⇒ YCbCr).
    pub jfif: bool,
    /// Adobe APP14 transform flag (0 = RGB, 1 = YCbCr, 2 = YCCK; 4-comp only
    /// uses 2 — kept for reporting).
    pub adobe_transform: Option<u8>,
    /// Marker type of the first SOF (0xC0 baseline / 0xC1 extended).
    pub sof_marker: u8,
}

// --------------------------------------------------------------------------- //
// Post-IDCT range limit table (jdmaster prepare_range_limit_table semantics)
// --------------------------------------------------------------------------- //

/// `RANGE_LIMIT[v & 1023]` where `v = pixel - 128` in the IDCT output domain:
/// segments map to 128+v, clamp-high, clamp-low, and the folded tail for
/// mild undershoot (v ∈ [-128,-1]).
pub(crate) fn build_range_limit() -> [u8; 1024] {
    let mut t = [0u8; 1024];
    for (j, slot) in t.iter_mut().enumerate() {
        *slot = match j {
            0..=127 => (128 + j) as u8,
            128..=511 => 255,
            512..=895 => 0,
            _ => (j - 896) as u8,
        };
    }
    t
}

// --------------------------------------------------------------------------- //
// islow IDCT (jidctint.c), natural-order coefficients → 8×8 u8 block
// --------------------------------------------------------------------------- //

pub fn idct_islow_block(
    coef_nat: &[i32; 64],
    quant_nat: &[u16; 64],
    range_limit: &[u8; 1024],
    out: &mut [u8], // 64 bytes, row-major
) {
    // Verbatim port of jidctint.c _jpeg_idct_islow. `ws[k*8 + c]` holds the
    // pass-1 result: spatial-y row k at horizontal frequency c.
    let mut ws = [0i64; 64];
    for ctr in 0..8usize {
        // column ctr of the (natural-order) coefficient block
        let v = |k: usize| -> i64 {
            coef_nat[k * 8 + ctr] as i64 * quant_nat[k * 8 + ctr] as i64
        };
        // Even part (rotator sqrt(2)*c(-6))
        let z2 = v(2);
        let z3 = v(6);
        let ze1 = (z2 + z3) * FIX_0_541196100;
        let etmp2 = ze1 - z3 * FIX_1_847759065;
        let etmp3 = ze1 + z2 * FIX_0_765366865;
        let ez2 = v(0);
        let ez3 = v(4);
        let etmp0 = (ez2 + ez3) << CONST_BITS;
        let etmp1 = (ez2 - ez3) << CONST_BITS;
        let etmp10 = etmp0 + etmp3;
        let etmp13 = etmp0 - etmp3;
        let etmp11 = etmp1 + etmp2;
        let etmp12 = etmp1 - etmp2;
        // Odd part
        let mut t0 = v(7);
        let mut t1 = v(5);
        let mut t2 = v(3);
        let mut t3 = v(1);
        let mut z1 = t0 + t3;
        let mut z2 = t1 + t2;
        let mut z3 = t0 + t2;
        let mut z4 = t1 + t3;
        let z5 = (z3 + z4) * FIX_1_175875602;
        t0 *= FIX_0_298631336;
        t1 *= FIX_2_053119869;
        t2 *= FIX_3_072711026;
        t3 *= FIX_1_501321110;
        z1 = -(z1 * FIX_0_899976223);
        z2 = -(z2 * FIX_2_562915447);
        z3 = -(z3 * FIX_1_961570560);
        z4 = -(z4 * FIX_0_390180644);
        z3 += z5;
        z4 += z5;
        t0 += z1 + z3;
        t1 += z2 + z4;
        t2 += z2 + z3;
        t3 += z1 + z4;

        let o = ctr; // ws[ctr + 8*k]
        ws[o] = descale(etmp10 + t3, CONST_BITS - PASS1_BITS);
        ws[o + 8 * 7] = descale(etmp10 - t3, CONST_BITS - PASS1_BITS);
        ws[o + 8] = descale(etmp11 + t2, CONST_BITS - PASS1_BITS);
        ws[o + 8 * 6] = descale(etmp11 - t2, CONST_BITS - PASS1_BITS);
        ws[o + 8 * 2] = descale(etmp12 + t1, CONST_BITS - PASS1_BITS);
        ws[o + 8 * 5] = descale(etmp12 - t1, CONST_BITS - PASS1_BITS);
        ws[o + 8 * 3] = descale(etmp13 + t0, CONST_BITS - PASS1_BITS);
        ws[o + 8 * 4] = descale(etmp13 - t0, CONST_BITS - PASS1_BITS);
    }
    // Pass 2: rows.
    let shift = CONST_BITS + PASS1_BITS + 3;
    for ctr in 0..8usize {
        let w: [i64; 8] = {
            let mut a = [0i64; 8];
            a.copy_from_slice(&ws[ctr * 8..ctr * 8 + 8]);
            a
        };
        let z2 = w[2];
        let z3 = w[6];
        let ze1 = (z2 + z3) * FIX_0_541196100;
        let etmp2 = ze1 - z3 * FIX_1_847759065;
        let etmp3 = ze1 + z2 * FIX_0_765366865;
        let etmp0 = (w[0] + w[4]) << CONST_BITS;
        let etmp1 = (w[0] - w[4]) << CONST_BITS;
        let etmp10 = etmp0 + etmp3;
        let etmp13 = etmp0 - etmp3;
        let etmp11 = etmp1 + etmp2;
        let etmp12 = etmp1 - etmp2;
        let mut t0 = w[7];
        let mut t1 = w[5];
        let mut t2 = w[3];
        let mut t3 = w[1];
        let mut z1 = t0 + t3;
        let mut z2 = t1 + t2;
        let mut z3 = t0 + t2;
        let mut z4 = t1 + t3;
        let z5 = (z3 + z4) * FIX_1_175875602;
        t0 *= FIX_0_298631336;
        t1 *= FIX_2_053119869;
        t2 *= FIX_3_072711026;
        t3 *= FIX_1_501321110;
        z1 = -(z1 * FIX_0_899976223);
        z2 = -(z2 * FIX_2_562915447);
        z3 = -(z3 * FIX_1_961570560);
        z4 = -(z4 * FIX_0_390180644);
        z3 += z5;
        z4 += z5;
        t0 += z1 + z3;
        t1 += z2 + z4;
        t2 += z2 + z3;
        t3 += z1 + z4;

        let r = ctr * 8;
        let rl = |x: i64| range_limit[(x & 1023) as usize];
        out[r] = rl(descale(etmp10 + t3, shift));
        out[r + 7] = rl(descale(etmp10 - t3, shift));
        out[r + 1] = rl(descale(etmp11 + t2, shift));
        out[r + 6] = rl(descale(etmp11 - t2, shift));
        out[r + 2] = rl(descale(etmp12 + t1, shift));
        out[r + 5] = rl(descale(etmp12 - t1, shift));
        out[r + 3] = rl(descale(etmp13 + t0, shift));
        out[r + 4] = rl(descale(etmp13 - t0, shift));
    }
}

// --------------------------------------------------------------------------- //
// Fixed-point YCbCr → RGB (jdcolor.c build_ycc_rgb_table + jdcolext.c)
// --------------------------------------------------------------------------- //

pub(crate) const SCALEBITS: i32 = 16;
const ONE_HALF: i64 = 1 << (SCALEBITS - 1);
const FIX_1_40200: i64 = 91881;
const FIX_1_77200: i64 = 116130;
const FIX_0_34414: i64 = 22554;
const FIX_0_71414: i64 = 46802;

pub(crate) fn ycc_tables() -> ([i32; 256], [i32; 256], [i32; 256], [i32; 256]) {
    // (Cr_r, Cb_b, Cb_g, Cr_g)
    let mut cr_r = [0i32; 256];
    let mut cb_b = [0i32; 256];
    let mut cb_g = [0i32; 256];
    let mut cr_g = [0i32; 256];
    for i in 0..256i64 {
        let x = i - 128;
        cr_r[i as usize] = ((FIX_1_40200 * x + ONE_HALF) >> SCALEBITS) as i32;
        cb_b[i as usize] = ((FIX_1_77200 * x + ONE_HALF) >> SCALEBITS) as i32;
        cb_g[i as usize] = ((-FIX_0_34414) * x + ONE_HALF) as i32;
        cr_g[i as usize] = ((-FIX_0_71414) * x) as i32;
    }
    (cr_r, cb_b, cb_g, cr_g)
}

#[inline]
pub(crate) fn clamp_u8(v: i32) -> u8 {
    v.clamp(0, 255) as u8
}

// --------------------------------------------------------------------------- //
// Decode driver
// --------------------------------------------------------------------------- //

#[derive(Clone)]
pub(crate) struct CompSpec {
    pub(crate) id: u8,
    pub(crate) h: u8,
    pub(crate) v: u8,
    pub(crate) tq: u8,
    pub(crate) dc_tbl: u8,
    pub(crate) ac_tbl: u8,
}

pub(crate) struct Frame {
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) comps: Vec<CompSpec>,
}

fn is_sof(m: u8) -> bool {
    m == 0xC0 || m == 0xC1
}

/// One decoded component plane at padded block resolution.
struct Plane {
    stride: usize,
    rows: usize,
    data: Vec<u8>,
    h: u8,
    v: u8,
    ds_w: u32,
    ds_h: u32,
}

/// Decode a complete baseline JPEG. `max_pixels` bounds the output (tiles in
/// this codebase are ≤ 256×256; probe payloads before decoding).
pub fn decode(data: &[u8], max_pixels: u64) -> CoreResult<DecodedImage> {
    decode_ex(data, max_pixels, false)
}

/// [`decode`] with a colorspace override: `force_rgb` skips the YCbCr→RGB
/// conversion for 3-component streams whose true colorspace is RGB. Used by
/// the Aperio SVS adapter, whose tiles are tagged Photometric RGB in the
/// TIFF while carrying no JFIF/Adobe marker (libjpeg's default would wrongly
/// assume YCbCr; tifffile applies the same "photometric wins" rule —
/// `jpeg_decode_colorspace`: "RGB -> RGB, if not jfif: colorspace = 2,
/// found in Aperio SVS"). The default path (`force_rgb=false`) is unchanged,
/// so KFB decode/encode parity is untouched.
pub fn decode_ex(data: &[u8], max_pixels: u64, force_rgb: bool) -> CoreResult<DecodedImage> {
    if data.len() < 4 || data[0..2] != [0xFF, 0xD8] {
        return Err(CoreError::jpeg("payload 不是 JPEG（缺 SOI）"));
    }
    let mut quant: Vec<Option<[u16; 64]>> = vec![None; 4]; // zigzag order
    let mut dht_dc: Vec<Option<HuffTable>> = vec![None; 4];
    let mut dht_ac: Vec<Option<HuffTable>> = vec![None; 4];
    let mut frame: Option<Frame> = None;
    let mut adobe_transform: Option<u8> = None;
    let mut restart_interval: u32 = 0;
    let mut scan_pos: Option<usize> = None;

    let mut i = 2usize;
    while i + 4 <= data.len() {
        if data[i] != 0xFF {
            return Err(CoreError::jpeg("JPEG 标记流错位"));
        }
        while i < data.len() && data[i] == 0xFF {
            i += 1;
        }
        if i >= data.len() {
            break;
        }
        let marker = data[i];
        i += 1;
        if marker == 0xD9 {
            break; // EOI
        }
        if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            continue; // no payload
        }
        if i + 2 > data.len() {
            return Err(CoreError::jpeg("JPEG 标记段截断"));
        }
        let seg_len = ((data[i] as usize) << 8) | data[i + 1] as usize;
        if seg_len < 2 || i + seg_len > data.len() {
            return Err(CoreError::jpeg("JPEG 段长度非法"));
        }
        let seg = &data[i + 2..i + seg_len];
        match marker {
            0xC2 | 0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF => {
                return Err(CoreError::jpeg(format!(
                    "不支持渐进/分层/无损 JPEG（SOF FF{marker:02X}）"
                )));
            }
            0xC4 => parse_dht(seg, &mut dht_dc, &mut dht_ac)?,
            0xDB => parse_dqt(seg, &mut quant)?,
            0xC8 => return Err(CoreError::jpeg("JPG 扩展不受支持")),
            0xCC => return Err(CoreError::jpeg("算术编码不受支持")),
            0xEE => {
                if seg.len() >= 12 && seg[0..5] == *b"Adobe" {
                    adobe_transform = Some(seg[11]);
                }
            }
            0xDD => {
                if seg.len() < 2 {
                    return Err(CoreError::jpeg("DRI 段残缺"));
                }
                restart_interval = ((seg[0] as u32) << 8) | seg[1] as u32;
            }
            m if is_sof(m) => {
                if frame.is_some() {
                    return Err(CoreError::jpeg("多个 SOF"));
                }
                frame = Some(parse_sof(seg)?);
            }
            0xDA => {
                let f = frame
                    .as_mut()
                    .ok_or_else(|| CoreError::jpeg("SOS 先于 SOF"))?;
                parse_sos(seg, f)?;
                scan_pos = Some(i + seg_len);
                break;
            }
            _ => {} // APPn / COM / others: skip
        }
        i += seg_len;
    }

    let frame = frame.ok_or_else(|| CoreError::jpeg("JPEG 缺少 SOF"))?;
    let Some(scan_start) = scan_pos else {
        return Err(CoreError::jpeg("JPEG 缺少 SOS"));
    };

    // ---- geometry -------------------------------------------------------- //
    let comps = frame.comps;
    let ncomp = comps.len();
    if ncomp != 1 && ncomp != 3 {
        return Err(CoreError::jpeg(format!("不支持 {ncomp} 分量 JPEG")));
    }
    let max_h = comps.iter().map(|c| c.h).max().unwrap() as u32;
    let max_v = comps.iter().map(|c| c.v).max().unwrap() as u32;
    if max_h == 0 || max_v == 0 || max_h > 4 || max_v > 4 {
        return Err(CoreError::jpeg("采样因子非法"));
    }
    let w = frame.width;
    let h = frame.height;
    if w == 0 || h == 0 {
        return Err(CoreError::jpeg("SOF 尺寸为 0"));
    }
    if w as u64 * h as u64 > max_pixels {
        return Err(CoreError::jpeg(format!(
            "解码输出 {w}×{h} 超出上限 {max_pixels} px"
        )));
    }
    let mcus_x = u32::try_from(
        (w as u64 + (max_h * 8) as u64 - 1) / (max_h * 8) as u64,
    )
    .map_err(|_| CoreError::jpeg("MCU 数溢出"))?;
    let mcus_y =
        u32::try_from((h as u64 + (max_v * 8) as u64 - 1) / (max_v * 8) as u64)
            .map_err(|_| CoreError::jpeg("MCU 数溢出"))?;
    let mcu_total = mcus_x as u64 * mcus_y as u64;

    let mut planes: Vec<Plane> = Vec::with_capacity(ncomp);
    for c in &comps {
        let wib = mcus_x as usize * c.h as usize;
        let hib = mcus_y as usize * c.v as usize;
        let ds_w = (w as u64 * c.h as u64 + max_h as u64 - 1) / max_h as u64;
        let ds_h = (h as u64 * c.v as u64 + max_v as u64 - 1) / max_v as u64;
        let stride = wib * 8;
        let rows = hib * 8;
        if (stride as u64) * (rows as u64) > max_pixels.saturating_mul(8) {
            return Err(CoreError::jpeg("分量平面超出解码内存上限"));
        }
        planes.push(Plane {
            stride,
            rows,
            data: vec![0u8; stride * rows],
            h: c.h,
            v: c.v,
            ds_w: ds_w as u32,
            ds_h: ds_h as u32,
        });
    }

    // ---- entropy decode --------------------------------------------------- //
    // Convert quant tables to natural order once (jddctmgr semantics).
    let mut quant_nat: Vec<Option<[u16; 64]>> = vec![None; 4];
    for (tq, t) in quant.iter().enumerate() {
        if let Some(qz) = t {
            let mut qn = [0u16; 64];
            for (k, &nat) in NATURAL_OF_ZIGZAG.iter().enumerate() {
                qn[nat] = qz[k];
            }
            quant_nat[tq] = Some(qn);
        }
    }
    let range_limit = build_range_limit();
    let mut br = BitReader::new(data, scan_start);
    let mut last_dc = [0i32; 4];
    let mut rst_index: u32 = 0;
    let mut coef = [0i32; 64];
    let mut block = [0u8; 64];
    let mut mcu_index: u64 = 0;
    while mcu_index < mcu_total {
        if restart_interval > 0
            && mcu_index > 0
            && (mcu_index % restart_interval as u64) == 0
        {
            match br.take_rst() {
                Some(m) => {
                    let want = 0xD0 + (rst_index & 7) as u8;
                    if m != want {
                        return Err(CoreError::jpeg("RST 序号错乱"));
                    }
                    rst_index += 1;
                }
                None => return Err(CoreError::jpeg("缺 RST 标记")),
            }
            br.align();
            last_dc = [0i32; 4];
        }
        let mcu_col = (mcu_index % mcus_x as u64) as usize;
        let mcu_row = (mcu_index / mcus_x as u64) as usize;
        for (ci, c) in comps.iter().enumerate() {
            let Some(dc_t) = dht_dc[c.dc_tbl as usize].as_ref() else {
                return Err(CoreError::jpeg("DC Huffman 表未定义"));
            };
            let Some(ac_t) = dht_ac[c.ac_tbl as usize].as_ref() else {
                return Err(CoreError::jpeg("AC Huffman 表未定义"));
            };
            let Some(qz) = quant_nat[c.tq as usize].as_ref() else {
                return Err(CoreError::jpeg("量化表未定义"));
            };
            for by in 0..c.v as usize {
                for bx in 0..c.h as usize {
                    decode_block(&mut br, dc_t, ac_t, &mut last_dc[ci], &mut coef);
                    idct_islow_block(&coef, &qz, &range_limit, &mut block);
                    let pl = &mut planes[ci];
                    let px = (mcu_col * pl.h as usize + bx) * 8;
                    let py = (mcu_row * pl.v as usize + by) * 8;
                    for r in 0..8 {
                        let dst = (py + r) * pl.stride + px;
                        pl.data[dst..dst + 8]
                            .copy_from_slice(&block[r * 8..r * 8 + 8]);
                    }

                }
            }
        }
        mcu_index += 1;
    }

    // ---- upsample + color ------------------------------------------------- //
    let full = upsample_all(&planes, w, h, max_h, max_v)?;
    let kind = if ncomp == 1 { ColorKind::Gray } else { ColorKind::Rgb };
    let data = if ncomp == 1 {
        full.into_iter().next().unwrap()
    } else {
        let y = &full[0];
        let cb = &full[1];
        let cr = &full[2];
        let ycc = adobe_transform.map_or(true, |t| t != 0) && !force_rgb;
        let mut out = vec![0u8; (w as usize) * (h as usize) * 3];
        if ycc {
            let (cr_r, cb_b, cb_g, cr_g) = ycc_tables();
            for p in 0..(w as usize) * (h as usize) {
                let yy = y[p] as i32;
                let cbv = cb[p] as usize;
                let crv = cr[p] as usize;
                out[p * 3] = clamp_u8(yy + cr_r[crv]);
                out[p * 3 + 1] =
                    clamp_u8(yy + ((cb_g[cbv] + cr_g[crv]) >> SCALEBITS));
                out[p * 3 + 2] = clamp_u8(yy + cb_b[cbv]);
            }
        } else {
            for p in 0..(w as usize) * (h as usize) {
                out[p * 3] = y[p];
                out[p * 3 + 1] = cb[p];
                out[p * 3 + 2] = cr[p];
            }
        }
        out
    };
    Ok(DecodedImage { width: w, height: h, kind, data })
}

pub(crate) fn decode_block<B: BitSource>(
    br: &mut B,
    dc_t: &HuffTable,
    ac_t: &HuffTable,
    last_dc: &mut i32,
    coef: &mut [i32; 64],
) {
    coef.fill(0);
    let s = huff_decode(br, dc_t);
    if s > 0 {
        if s > 15 {
            // corrupt symbol: keep predictor, zero block (libjpeg warns)
            coef[0] = *last_dc;
            return;
        }
        let bits = br.get(s as u32) as i32;
        let diff = extend(bits, s);
        *last_dc = last_dc.wrapping_add(diff);
    }
    coef[0] = *last_dc;
    let mut k = 1usize;
    while k < 64 {
        let rs = huff_decode(br, ac_t);
        let r = (rs >> 4) as usize;
        let s = rs & 0x0F;
        if s == 0 {
            if r != 15 {
                break; // EOB
            }
            k += 16;
            continue;
        }
        k += r;
        if k > 63 {
            break; // corrupt run: drop the rest of the block
        }
        let bits = br.get(s as u32) as i32;
        coef[NATURAL_OF_ZIGZAG[k]] = extend(bits, s);
        k += 1;
    }
}

/// Canonical Huffman decode; an unmatchable 16-bit code decodes as 0
/// (libjpeg's warning path).
pub(crate) trait BitSource {
    fn get(&mut self, n: u32) -> u32;
}

impl BitSource for BitReader<'_> {
    fn get(&mut self, n: u32) -> u32 {
        BitReader::get(self, n)
    }
}

pub(crate) fn huff_decode<B: BitSource>(br: &mut B, t: &HuffTable) -> u8 {
    let mut code: i32 = 0;
    for len in 1..=16usize {
        code = (code << 1) | br.get(1) as i32;
        if code <= t.maxcode[len] {
            let idx = t.valptr[len] + (code - t.mincode[len]);
            if idx >= 0 && (idx as usize) < t.vals.len() {
                return t.vals[idx as usize];
            }
            return 0;
        }
    }
    0
}

// --------------------------------------------------------------------------- //
// Bit reader (FF00 unstuffing; zeros past a marker, like libjpeg)
// --------------------------------------------------------------------------- //

struct BitReader<'a> {
    data: &'a [u8],
    pos: usize,
    bits: u64,
    nbits: u32,
    hit_marker: bool,
}

impl<'a> BitReader<'a> {
    fn new(data: &'a [u8], pos: usize) -> Self {
        BitReader { data, pos, bits: 0, nbits: 0, hit_marker: false }
    }

    fn fill(&mut self) {
        while self.nbits <= 24 {
            if self.hit_marker || self.pos >= self.data.len() {
                return; // subsequent get() pads with zeros
            }
            let b = self.data[self.pos];
            if b == 0xFF {
                match self.data.get(self.pos + 1).copied() {
                    Some(0x00) => {
                        self.pos += 2;
                    }
                    Some(_) => {
                        self.hit_marker = true;
                        return;
                    }
                    None => {
                        self.pos = self.data.len();
                        self.hit_marker = true;
                        return;
                    }
                }
                self.bits = (self.bits << 8) | 0xFF;
                self.nbits += 8;
            } else {
                self.pos += 1;
                self.bits = (self.bits << 8) | b as u64;
                self.nbits += 8;
            }
        }
    }

    #[inline]
    fn get(&mut self, n: u32) -> u32 {
        if n == 0 {
            return 0;
        }
        if self.nbits < n {
            self.fill();
            if self.nbits < n {
                // exhausted: available bits, zero-padded on the right
                let have = self.nbits;
                let v = (((self.bits as u64) << (n - have)) as u32)
                    & ((1u32 << n) - 1);
                self.nbits = 0;
                self.bits = 0;
                return v;
            }
        }
        self.nbits -= n;
        ((self.bits >> self.nbits) & ((1u64 << n) - 1)) as u32
    }

    /// Skip to the next RSTn marker (scanning past stray bytes) and consume it.
    fn take_rst(&mut self) -> Option<u8> {
        self.nbits = 0;
        self.bits = 0;
        let mut p = self.pos;
        while p + 1 < self.data.len() {
            if self.data[p] == 0xFF {
                let m = self.data[p + 1];
                if (0xD0..=0xD7).contains(&m) {
                    self.pos = p + 2;
                    self.hit_marker = false;
                    return Some(m);
                }
                if m == 0x00 {
                    p += 2;
                    continue;
                }
                return None; // a different marker: not a restart
            }
            p += 1;
        }
        None
    }

    fn align(&mut self) {
        self.nbits = 0;
        self.bits = 0;
    }
}

// --------------------------------------------------------------------------- //
// Upsampling (jdsample.c): fancy h2v1 / h2v2, duplication fallback when the
// downsampled width ≤ 2, generic integer box otherwise; identity otherwise.
// --------------------------------------------------------------------------- //

fn upsample_all(
    planes: &[Plane],
    w: u32,
    h: u32,
    max_h: u32,
    max_v: u32,
) -> CoreResult<Vec<Vec<u8>>> {
    let out_len = w as usize * h as usize;
    let mut out = Vec::with_capacity(planes.len());
    for pl in planes {
        let mut full = vec![0u8; out_len];
        let ch = pl.h as u32;
        let cv = pl.v as u32;
        let ds_w = pl.ds_w as usize;
        let ds_h = pl.ds_h as usize;
        let half_h = ch * 2 == max_h;
        let half_v = cv * 2 == max_v;
        let identity = ch == max_h && cv == max_v;
        if identity {
            for y in 0..h as usize {
                let src = &pl.data[y.min(pl.rows - 1) * pl.stride..][..pl.stride];
                let dst = &mut full[y * w as usize..][..w as usize];
                let n = (w as usize).min(src.len());
                dst[..n].copy_from_slice(&src[..n]);
            }
        } else if half_h && !half_v {
            for y in 0..h as usize {
                let src = &pl.data[y.min(pl.rows - 1) * pl.stride..][..pl.stride];
                let dst = &mut full[y * w as usize..][..w as usize];
                h2v1_upsample_row(src, ds_w, dst, w as usize);
            }
        } else if half_h && half_v {
            h2v2_upsample(pl, ds_w, ds_h, &mut full, w as usize, h as usize);
        } else {
            let h_exp = (max_h / ch) as usize;
            let v_exp = (max_v / cv) as usize;
            int_upsample(pl, h_exp, v_exp, &mut full, w as usize, h as usize)?;
        }
        out.push(full);
    }
    Ok(out)
}

/// One output row of h2v1: fancy (ds_w > 2) or duplication.
pub(crate) fn h2v1_upsample_row(src: &[u8], ds_w: usize, dst: &mut [u8], out_w: usize) {
    if ds_w > 2 {
        // fancy (jdsample.c h2v1_fancy_upsample)
        let mut o = 0usize;
        let iv = src[0] as u32;
        if o < out_w {
            dst[o] = iv as u8;
            o += 1;
        }
        if o < out_w {
            dst[o] = ((iv * 3 + src[1] as u32 + 2) >> 2) as u8;
            o += 1;
        }
        for x in 1..ds_w - 1 {
            let iv = src[x] as u32 * 3;
            if o < out_w {
                dst[o] = ((iv + src[x - 1] as u32 + 1) >> 2) as u8;
                o += 1;
            }
            if o < out_w {
                dst[o] = ((iv + src[x + 1] as u32 + 2) >> 2) as u8;
                o += 1;
            }
        }
        let iv = src[ds_w - 1] as u32;
        if o < out_w {
            // verbatim jdsample.c: rounding bias 1 (not 2) on the last pair
            dst[o] = ((iv * 3 + src[ds_w - 2] as u32 + 1) >> 2) as u8;
            o += 1;
        }
        if o < out_w {
            dst[o] = iv as u8;
        }
    } else {
        // h2v1_upsample: pure duplication
        let mut o = 0usize;
        for x in 0..ds_w {
            for _ in 0..2 {
                if o < out_w {
                    dst[o] = src[x];
                    o += 1;
                }
            }
        }
    }
}

/// h2v2 upsample of a whole plane: fancy (ds_w > 2) or duplication.
fn h2v2_upsample(
    pl: &Plane,
    ds_w: usize,
    ds_h: usize,
    full: &mut [u8],
    w: usize,
    h: usize,
) {
    let row = |r: usize| -> &[u8] {
        let rr = r.min(ds_h.saturating_sub(1));
        &pl.data[rr * pl.stride..][..pl.stride]
    };
    if ds_w > 2 {
        // fancy (jdsample.c h2v2_fancy_upsample): output rows 2r (mix with
        // row above) and 2r+1 (mix with row below); edge rows replicate.
        for r in 0..ds_h {
            let cur = row(r);
            let above = row(r.saturating_sub(1));
            let below = row((r + 1).min(ds_h - 1));
            let out_y0 = 2 * r;
            let out_y1 = 2 * r + 1;
            h2v2_fancy_row(above, cur, ds_w, full, w, h, out_y0);
            h2v2_fancy_row(below, cur, ds_w, full, w, h, out_y1);
        }
    } else {
        // h2v2_upsample: duplication in both axes
        for r in 0..ds_h {
            let cur = row(r);
            let mut o = 0usize;
            for x in 0..ds_w {
                for _ in 0..2 {
                    for &yy in &[2 * r, 2 * r + 1] {
                        if yy < h && o < w {
                            full[yy * w + o] = cur[x];
                        }
                    }
                    o += 1;
                }
            }
        }
    }
}

/// One output row of the fancy h2v2: `nb` is the neighbouring chroma row
/// (above for even output rows, below for odd), `cur` the current one.
pub(crate) fn h2v2_fancy_row(
    nb: &[u8],
    cur: &[u8],
    ds_w: usize,
    full: &mut [u8],
    w: usize,
    h: usize,
    out_y: usize,
) {
    if out_y >= h {
        return;
    }
    let dst = &mut full[out_y * w..][..w];
    let mut o = 0usize;
    // first column
    let mut this = cur[0] as u32 * 3 + nb[0] as u32;
    let mut next = cur[1] as u32 * 3 + nb[1] as u32;
    if o < w {
        dst[o] = ((this * 4 + 8) >> 4) as u8;
        o += 1;
    }
    if o < w {
        dst[o] = ((this * 3 + next + 7) >> 4) as u8;
        o += 1;
    }
    let mut last = this;
    this = next;
    for x in 1..ds_w - 1 {
        next = cur[x + 1] as u32 * 3 + nb[x + 1] as u32;
        if o < w {
            dst[o] = ((this * 3 + last + 8) >> 4) as u8;
            o += 1;
        }
        if o < w {
            dst[o] = ((this * 3 + next + 7) >> 4) as u8;
            o += 1;
        }
        last = this;
        this = next;
    }
    // last column
    if o < w {
        dst[o] = ((this * 3 + last + 8) >> 4) as u8;
        o += 1;
    }
    if o < w {
        dst[o] = ((this * 4 + 7) >> 4) as u8;
    }
}

/// Generic integer box upsample (jdsample.c int_upsample).
fn int_upsample(
    pl: &Plane,
    h_exp: usize,
    v_exp: usize,
    full: &mut [u8],
    w: usize,
    h: usize,
) -> CoreResult<()> {
    if h_exp == 0 || v_exp == 0 || h_exp > 4 || v_exp > 4 {
        return Err(CoreError::jpeg("不支持的采样比"));
    }
    let numpix = (h_exp * v_exp) as u32;
    let numpix2 = numpix / 2;
    let ds_w = pl.ds_w as usize;
    let ds_h = pl.ds_h as usize;
    for y in 0..h {
        let out_row = &mut full[y * w..][..w];
        for x in 0..ds_w {
            let mut sum = numpix2 as u32;
            for r in 0..v_exp {
                let sy = (y / v_exp + r).min(ds_h.saturating_sub(1));
                let src = &pl.data[sy * pl.stride..][..pl.stride];
                for c in 0..h_exp {
                    let sx = (x * h_exp + c).min(ds_w.saturating_sub(1));
                    sum += src[sx] as u32;
                }
            }
            let v = (sum / numpix) as u8;
            for c in 0..h_exp {
                let ox = x * h_exp + c;
                if ox < w {
                    out_row[ox] = v;
                }
            }
        }
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Marker segment parsers
// --------------------------------------------------------------------------- //

pub(crate) fn parse_sos(seg: &[u8], frame: &mut Frame) -> CoreResult<()> {
    if seg.is_empty() {
        return Err(CoreError::jpeg("SOS 段残缺"));
    }
    let ns = seg[0] as usize;
    if ns == 0 || ns > 4 || seg.len() < 1 + 2 * ns + 3 {
        return Err(CoreError::jpeg("SOS 分量数非法"));
    }
    for k in 0..ns {
        let id = seg[1 + 2 * k];
        let t = seg[2 + 2 * k];
        if t >> 4 > 3 || t & 0x0F > 3 {
            return Err(CoreError::jpeg("SOS 表号非法"));
        }
        let comp = frame
            .comps
            .iter_mut()
            .find(|c| c.id == id)
            .ok_or_else(|| CoreError::jpeg("SOS 分量与 SOF 不符"))?;
        comp.dc_tbl = t >> 4;
        comp.ac_tbl = t & 0x0F;
    }
    if ns != frame.comps.len() {
        return Err(CoreError::jpeg("非单扫描 JPEG 不受支持"));
    }
    Ok(())
}

pub(crate) fn parse_sof(seg: &[u8]) -> CoreResult<Frame> {
    if seg.len() < 6 {
        return Err(CoreError::jpeg("SOF 段残缺"));
    }
    if seg[0] != 8 {
        return Err(CoreError::jpeg(format!("不支持 {} 位精度", seg[0])));
    }
    let height = ((seg[1] as u32) << 8) | seg[2] as u32;
    let width = ((seg[3] as u32) << 8) | seg[4] as u32;
    let ncomp = seg[5] as usize;
    if ncomp > 4 || seg.len() < 6 + 3 * ncomp {
        return Err(CoreError::jpeg("SOF 分量数非法"));
    }
    let mut comps = Vec::with_capacity(ncomp);
    for c in 0..ncomp {
        let b = &seg[6 + 3 * c..];
        let h = b[1] >> 4;
        let v = b[1] & 0x0F;
        if h == 0 || v == 0 || h > 4 || v > 4 || b[2] > 3 {
            return Err(CoreError::jpeg("SOF 采样/量化表字段非法"));
        }
        comps.push(CompSpec { id: b[0], h, v, tq: b[2], dc_tbl: 0, ac_tbl: 0 });
    }
    Ok(Frame { width, height, comps })
}

pub(crate) fn parse_dqt(seg: &[u8], quant: &mut [Option<[u16; 64]>]) -> CoreResult<()> {
    let mut p = 0usize;
    while p < seg.len() {
        let pq = seg[p] >> 4;
        let tq = (seg[p] & 0x0F) as usize;
        p += 1;
        if tq > 3 {
            return Err(CoreError::jpeg("DQT 表号非法"));
        }
        let mut t = [0u16; 64];
        if pq == 0 {
            if p + 64 > seg.len() {
                return Err(CoreError::jpeg("DQT 段截断"));
            }
            for (k, v) in t.iter_mut().enumerate() {
                *v = seg[p + k] as u16;
            }
            p += 64;
        } else if pq == 1 {
            if p + 128 > seg.len() {
                return Err(CoreError::jpeg("DQT 段截断"));
            }
            for (k, v) in t.iter_mut().enumerate() {
                *v = ((seg[p + 2 * k] as u16) << 8) | seg[p + 2 * k + 1] as u16;
            }
            p += 128;
        } else {
            return Err(CoreError::jpeg("DQT 精度非法"));
        }
        if t.iter().any(|&v| v == 0) {
            return Err(CoreError::jpeg("量化表含 0"));
        }
        quant[tq] = Some(t);
    }
    Ok(())
}

pub(crate) fn parse_dht(
    seg: &[u8],
    dc: &mut [Option<HuffTable>],
    ac: &mut [Option<HuffTable>],
) -> CoreResult<()> {
    let mut p = 0usize;
    while p < seg.len() {
        let tc = seg[p] >> 4;
        let th = (seg[p] & 0x0F) as usize;
        p += 1;
        if th > 3 || tc > 1 {
            return Err(CoreError::jpeg("DHT 字段非法"));
        }
        if p + 16 > seg.len() {
            return Err(CoreError::jpeg("DHT 段截断"));
        }
        let mut bits = [0u8; 16];
        bits.copy_from_slice(&seg[p..p + 16]);
        p += 16;
        let count: usize = bits.iter().map(|&b| b as usize).sum();
        if p + count > seg.len() {
            return Err(CoreError::jpeg("DHT 值区截断"));
        }
        let vals = seg[p..p + count].to_vec();
        p += count;
        let table = HuffTable::build(&bits, &vals)
            .ok_or_else(|| CoreError::jpeg("Huffman 表过订阅"))?;
        if tc == 0 {
            dc[th] = Some(table);
        } else {
            ac[th] = Some(table);
        }
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Header probe (port of kfb.parser.scan_jpeg)
// --------------------------------------------------------------------------- //

/// Parse the SOS to bind per-component Huffman tables; also validates that
/// the scan matches the frame. Returns the offset just past the SOS segment.
pub fn scan_jpeg(data: &[u8]) -> CoreResult<JpegProbe> {
    let n = data.len();
    if n < 4 || data[0..2] != [0xFF, 0xD8] {
        return Err(CoreError::jpeg("payload 不是 JPEG（缺 SOI）"));
    }
    let mut width = 0u32;
    let mut height = 0u32;
    let mut sampling = None;
    let mut comp_ids = None;
    let mut jfif = false;
    let mut adobe_transform = None;
    let mut sof_marker = 0u8;
    let mut i = 2usize;
    while i + 4 <= n {
        if data[i] != 0xFF {
            return Err(CoreError::jpeg("JPEG 标记流错位"));
        }
        while i < n && data[i] == 0xFF {
            i += 1;
        }
        if i >= n {
            break;
        }
        let marker = data[i];
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
        let seg_len = ((data[i] as usize) << 8) | data[i + 1] as usize;
        if seg_len < 2 || i + seg_len > n {
            return Err(CoreError::jpeg("JPEG 段长度非法"));
        }
        let seg = &data[i + 2..i + seg_len];
        if marker == 0xE0 && seg.len() >= 5 && &seg[..5] == b"JFIF\0" {
            jfif = true;
        }
        if marker == 0xEE && seg.len() >= 12 && seg[0..5] == *b"Adobe" {
            adobe_transform = Some(seg[11]);
        }
        if is_sof(marker) {
            if seg.len() < 6 {
                return Err(CoreError::jpeg("SOF 段残缺"));
            }
            sof_marker = marker;
            height = ((seg[1] as u32) << 8) | seg[2] as u32;
            width = ((seg[3] as u32) << 8) | seg[4] as u32;
            let ncomp = seg[5];
            if (ncomp != 1 && ncomp != 3) || seg.len() < 6 + 3 * ncomp as usize {
                return Err(CoreError::jpeg("SOF 分量数非法"));
            }
            if ncomp == 3 {
                let f = |c: usize| (seg[6 + 3 * c + 1] >> 4, seg[6 + 3 * c + 1] & 0x0F);
                let (a, b, c) = (f(0), f(1), f(2));
                sampling = Some((a.0, a.1, b.0, b.1, c.0, c.1));
                comp_ids = Some((seg[6], seg[9], seg[12]));
            }
            break; // only the first SOF
        }
        i += seg_len;
    }
    if width == 0 || height == 0 {
        return Err(CoreError::jpeg("JPEG 缺少 SOF"));
    }
    Ok(JpegProbe { width, height, sampling, comp_ids, jfif, adobe_transform, sof_marker })
}

/// Extract all DQT tables in **natural (row-major) order** (table id order) —
/// byte-compatible with Pillow's `im.quantization`, so tables can flow from a
/// decoded tile straight into `EncoderCfg`. Values outside 1..=255 (16-bit
/// tables) are reported as-is; callers decide whether they are usable.
pub fn extract_qtables(data: &[u8]) -> Option<Vec<[u16; 64]>> {
    let mut tabs: Vec<Option<(u8, [u16; 64])>> = vec![None; 4];
    let mut i = 2usize;
    let n = data.len();
    while i + 4 <= n {
        if data[i] != 0xFF {
            return None;
        }
        let marker = data[i + 1];
        if marker == 0xD8 || marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            i += 2;
            continue;
        }
        if marker == 0xD9 {
            break;
        }
        if i + 4 > n {
            return None;
        }
        let seg_len = ((data[i + 2] as usize) << 8) | data[i + 3] as usize;
        if seg_len < 2 || i + 2 + seg_len > n {
            return None;
        }
        if marker == 0xDB {
            let seg = &data[i + 4..i + 2 + seg_len];
            let mut p = 0usize;
            while p < seg.len() {
                let pq = seg[p] >> 4;
                let tq = (seg[p] & 0x0F) as usize;
                p += 1;
                if tq > 3 {
                    return None;
                }
                let mut t = [0u16; 64];
                let step = if pq == 0 { 1 } else { 2 };
                if p + 64 * step > seg.len() {
                    return None;
                }
                for (k, v) in t.iter_mut().enumerate() {
                    *v = if pq == 0 {
                        seg[p + k] as u16
                    } else {
                        ((seg[p + 2 * k] as u16) << 8) | seg[p + 2 * k + 1] as u16
                    };
                }
                p += 64 * step;
                tabs[tq] = Some((tq as u8, natural_from_zigzag(&t)));
            }
        }
        if marker == 0xDA {
            break;
        }
        i += 2 + seg_len;
    }
    let out: Vec<[u16; 64]> = tabs
        .into_iter()
        .flatten()
        .map(|(_, t)| t)
        .collect();
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}
