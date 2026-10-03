//! Baseline JPEG encoder producing byte-identical output to Pillow
//! (libjpeg-turbo) for the parameter combinations this pipeline uses:
//! RGB → YCbCr with fixed-point jccolor tables, h2v1/h2v2 triangle-free
//! downsampling (`jcsample.c`, no smoothing), islow FDCT, integer quantize
//! with libjpeg's rounding, standard Annex-K Huffman tables and the exact
//! marker layout Pillow emits (SOI, JFIF APP0 v1.01, one 8-bit DQT segment
//! per table in zigzag order, SOF0, DHT pairs per component, SOS).
//!
//! With identical pixels and identical quantization tables, the encoded edge
//! tiles are byte-equal to the Python oracle's — the quality gate then holds
//! with PSNR=∞ and max-diff=0 by construction.

use super::tables::*;
use crate::error::{CoreError, CoreResult};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sampling {
    S444,
    S422,
    S420,
}

impl Sampling {
    fn max_h(self) -> u32 {
        match self {
            Sampling::S444 => 1,
            _ => 2,
        }
    }
    fn max_v(self) -> u32 {
        match self {
            Sampling::S444 | Sampling::S422 => 1,
            Sampling::S420 => 2,
        }
    }
}

/// Encoder configuration: quantization tables in **natural (row-major)**
/// order — the same contract as Pillow's `im.quantization` /
/// `save(qtables=...)`. DQT segments are emitted in zigzag order.
#[derive(Debug, Clone)]
pub struct EncoderCfg {
    pub y_q: [u16; 64],
    pub c_q: [u16; 64],
    pub sampling: Sampling,
    /// Emit a three-component **RGB** stream (Adobe APP14 transform 0,
    /// component ids 'R','G','B', no colour conversion) instead of the
    /// default JFIF YCbCr. Used by the MRXS preserve compose: TIFF
    /// photometric-2 RGB JPEG is the Aperio-style combination every reader
    /// (Bio-Formats included) decodes, while YCbCr 4:4:4 tiles render as
    /// garbage in Bio-Formats (F3 interop gate).
    pub rgb: bool,
}

impl EncoderCfg {
    pub fn with_quality(quality: u8, sampling: Sampling) -> Self {
        EncoderCfg {
            y_q: std_luma_quality(quality),
            c_q: std_chroma_quality(quality),
            sampling,
            rgb: false,
        }
    }

    /// RGB stream (forced 4:4:4 — RGB JPEG has no subsampling).
    pub fn with_rgb_quality(quality: u8) -> Self {
        EncoderCfg {
            y_q: std_luma_quality(quality),
            c_q: std_chroma_quality(quality),
            sampling: Sampling::S444,
            rgb: true,
        }
    }
}

// --------------------------------------------------------------------------- //
// RGB → YCbCr (jccolor.c rgb_ycc_start / rgb_ycc_convert)
// --------------------------------------------------------------------------- //

const J_SCALEBITS: i32 = 16;
const CBCR_OFFSET: i64 = 128i64 << J_SCALEBITS;
const J_ONE_HALF: i64 = 1 << (J_SCALEBITS - 1);

struct RgbYccTables {
    // per channel-value tables: [R_Y, G_Y, B_Y, R_CB, G_CB, B_CB, R_CR, G_CR, B_CR]
    r_y: Vec<i64>,
    g_y: Vec<i64>,
    b_y: Vec<i64>,
    r_cb: Vec<i64>,
    g_cb: Vec<i64>,
    b_cb: Vec<i64>,
    r_cr: Vec<i64>,
    g_cr: Vec<i64>,
    b_cr: Vec<i64>,
}

fn fix(x: f64) -> i64 {
    (x * (1i64 << J_SCALEBITS) as f64 + 0.5) as i64
}

fn rgb_ycc_tables() -> RgbYccTables {
    let mut t = RgbYccTables {
        r_y: vec![0; 256],
        g_y: vec![0; 256],
        b_y: vec![0; 256],
        r_cb: vec![0; 256],
        g_cb: vec![0; 256],
        b_cb: vec![0; 256],
        r_cr: vec![0; 256],
        g_cr: vec![0; 256],
        b_cr: vec![0; 256],
    };
    for i in 0..256i64 {
        t.r_y[i as usize] = fix(0.29900) * i;
        t.g_y[i as usize] = fix(0.58700) * i;
        t.b_y[i as usize] = fix(0.11400) * i + J_ONE_HALF;
        t.r_cb[i as usize] = -fix(0.16874) * i;
        t.g_cb[i as usize] = -fix(0.33126) * i;
        t.b_cb[i as usize] = fix(0.50000) * i + CBCR_OFFSET + J_ONE_HALF - 1;
        t.r_cr[i as usize] = fix(0.50000) * i;
        t.g_cr[i as usize] = -fix(0.41869) * i;
        t.b_cr[i as usize] = -fix(0.08131) * i + CBCR_OFFSET + J_ONE_HALF - 1;
    }
    t
}

// --------------------------------------------------------------------------- //
// islow FDCT (jfdctint.c): samples (already level-shifted) → coefficients
// --------------------------------------------------------------------------- //

/// Verbatim port of jfdctint.c `_jpeg_fdct_islow` (in-place row pass then
/// column pass over the same natural-order array). Input samples must
/// already be level-shifted by −128.
pub fn fdct_islow(samples: &[i32; 64], coef_nat: &mut [i32; 64]) {
    let mut ws = [0i64; 64];
    // Pass 1: process rows in place.
    for r in 0..8usize {
        let s = |c: usize| -> i64 { samples[r * 8 + c] as i64 };
        let t0 = s(0) + s(7);
        let t7 = s(0) - s(7);
        let t1 = s(1) + s(6);
        let t6 = s(1) - s(6);
        let t2 = s(2) + s(5);
        let t5 = s(2) - s(5);
        let t3 = s(3) + s(4);
        let t4 = s(3) - s(4);

        let t10 = t0 + t3;
        let t13 = t0 - t3;
        let t11 = t1 + t2;
        let t12 = t1 - t2;

        let o = r * 8;
        ws[o] = (t10 + t11) << PASS1_BITS;
        ws[o + 4] = (t10 - t11) << PASS1_BITS;

        let z1 = (t12 + t13) * FIX_0_541196100;
        ws[o + 2] = descale(z1 + t13 * FIX_0_765366865, CONST_BITS - PASS1_BITS);
        ws[o + 6] = descale(z1 - t12 * FIX_1_847759065, CONST_BITS - PASS1_BITS);

        let mut z1 = t4 + t7;
        let mut z2 = t5 + t6;
        let mut z3 = t4 + t6;
        let mut z4 = t5 + t7;
        let z5 = (z3 + z4) * FIX_1_175875602;

        let t4m = t4 * FIX_0_298631336;
        let t5m = t5 * FIX_2_053119869;
        let t6m = t6 * FIX_3_072711026;
        let t7m = t7 * FIX_1_501321110;
        z1 = -(z1 * FIX_0_899976223);
        z2 = -(z2 * FIX_2_562915447);
        z3 = -(z3 * FIX_1_961570560);
        z4 = -(z4 * FIX_0_390180644);
        z3 += z5;
        z4 += z5;

        ws[o + 7] = descale(t4m + z1 + z3, CONST_BITS - PASS1_BITS);
        ws[o + 5] = descale(t5m + z2 + z4, CONST_BITS - PASS1_BITS);
        ws[o + 3] = descale(t6m + z2 + z3, CONST_BITS - PASS1_BITS);
        ws[o + 1] = descale(t7m + z1 + z4, CONST_BITS - PASS1_BITS);
    }
    // Pass 2: process columns in place (values read with stride 8).
    for c in 0..8usize {
        let s = |r: usize| -> i64 { ws[r * 8 + c] };
        let t0 = s(0) + s(7);
        let t7 = s(0) - s(7);
        let t1 = s(1) + s(6);
        let t6 = s(1) - s(6);
        let t2 = s(2) + s(5);
        let t5 = s(2) - s(5);
        let t3 = s(3) + s(4);
        let t4 = s(3) - s(4);

        let t10 = t0 + t3;
        let t13 = t0 - t3;
        let t11 = t1 + t2;
        let t12 = t1 - t2;

        // Outputs 0/4 descale by PASS1_BITS only (verbatim jfdctint.c).
        coef_nat[c] = descale(t10 + t11, PASS1_BITS) as i32;
        coef_nat[4 * 8 + c] = descale(t10 - t11, PASS1_BITS) as i32;

        let z1 = (t12 + t13) * FIX_0_541196100;
        coef_nat[2 * 8 + c] =
            descale(z1 + t13 * FIX_0_765366865, CONST_BITS + PASS1_BITS) as i32;
        coef_nat[6 * 8 + c] =
            descale(z1 - t12 * FIX_1_847759065, CONST_BITS + PASS1_BITS) as i32;

        let mut z1 = t4 + t7;
        let mut z2 = t5 + t6;
        let mut z3 = t4 + t6;
        let mut z4 = t5 + t7;
        let z5 = (z3 + z4) * FIX_1_175875602;

        let t4m = t4 * FIX_0_298631336;
        let t5m = t5 * FIX_2_053119869;
        let t6m = t6 * FIX_3_072711026;
        let t7m = t7 * FIX_1_501321110;
        z1 = -(z1 * FIX_0_899976223);
        z2 = -(z2 * FIX_2_562915447);
        z3 = -(z3 * FIX_1_961570560);
        z4 = -(z4 * FIX_0_390180644);
        z3 += z5;
        z4 += z5;

        coef_nat[7 * 8 + c] = descale(t4m + z1 + z3, CONST_BITS + PASS1_BITS) as i32;
        coef_nat[5 * 8 + c] = descale(t5m + z2 + z4, CONST_BITS + PASS1_BITS) as i32;
        coef_nat[3 * 8 + c] = descale(t6m + z2 + z3, CONST_BITS + PASS1_BITS) as i32;
        coef_nat[8 + c] = descale(t7m + z1 + z4, CONST_BITS + PASS1_BITS) as i32;
    }
}

/// libjpeg quantize (jcdctmgr.c): symmetric round-half-away-from-zero.
#[inline]
fn quantize(temp: i32, qval: u16) -> i32 {
    // islow divisors are quantval << 3 (jcdctmgr.c start_pass_fdctmgr):
    // jfdctint leaves results scaled up by an overall factor of 8.
    let d = (qval as i32) << 3;
    if temp < 0 {
        let t = -temp + (d >> 1);
        -(t / d)
    } else {
        (temp + (d >> 1)) / d
    }
}

// --------------------------------------------------------------------------- //
// Planes + downsampling (jcsample.c, smoothing factor 0)
// --------------------------------------------------------------------------- //

struct EncPlane {
    data: Vec<u8>,
    stride: usize,
    rows: usize,
}

/// Build one padded component plane by downsampling the full-resolution
/// plane, verbatim jcsample.c semantics (smoothing factor 0):
///   - h2v1: `(a + b + bias) >> 1` with bias alternating 0,1,0,1 per column
///   - h2v2: `(a + b + c + d + bias) >> 2` with bias alternating 1,2,1,2
///   - identity: pass-through
/// Padding is computed from replicated input samples (bottom rows and right
/// columns replicate the last real sample), never by replicating already
/// averaged output, matching `expand_right_edge` + jcprepct bottom padding.
fn build_plane(
    full: &[u8],
    w: usize,
    h: usize,
    stride: usize,
    rows: usize,
    half_h: bool,
    half_v: bool,
) -> EncPlane {
    let src = |x: usize, y: usize| -> u8 {
        let yy = y.min(h - 1);
        let xx = x.min(w - 1);
        full[yy * w + xx]
    };
    let mut data = vec![0u8; stride * rows];
    let ds_h = if half_v { (h + 1) / 2 } else { h };
    if !half_h && !half_v {
        for y in 0..rows {
            for x in 0..stride {
                data[y * stride + x] = src(x, y);
            }
        }
    } else if half_h && !half_v {
        // h2v1: chroma row dy comes from input row dy
        for dy in 0..ds_h.min(rows) {
            let d = &mut data[dy * stride..][..stride];
            let mut bias = 0u32;
            for dx in 0..stride {
                let x0 = dx * 2;
                d[dx] = ((src(x0, dy) as u32 + src(x0 + 1, dy) as u32 + bias) >> 1) as u8;
                bias ^= 1;
            }
        }
    } else {
        // h2v2: chroma row dy from input rows 2dy, 2dy+1 (bottom pair repeats
        // the last real row when h is odd — jcprepct expand_bottom_edge on
        // the conversion buffer)
        for dy in 0..ds_h.min(rows) {
            let y0 = dy * 2;
            let y1 = y0 + 1;
            let d = &mut data[dy * stride..][..stride];
            let mut bias = 1u32;
            for dx in 0..stride {
                let x0 = dx * 2;
                let v = (src(x0, y0) as u32 + src(x0 + 1, y0) as u32
                    + src(x0, y1) as u32 + src(x0 + 1, y1) as u32
                    + bias)
                    >> 2;
                d[dx] = v as u8;
                bias ^= 3;
            }
        }
    }
    // Bottom padding replicates the last real downsampled row (jcprepct
    // expand_bottom_edge on the *downsampled output*, not re-averaged input).
    let last = ds_h.saturating_sub(1).min(rows.saturating_sub(1));
    for y in (ds_h).min(last + 1)..rows {
        let from = last * stride;
        data.copy_within(from..from + stride, y * stride);
    }
    EncPlane { data, stride, rows }
}

// --------------------------------------------------------------------------- //
// Entropy coder (jchuff.c semantics, standard tables only)
// --------------------------------------------------------------------------- //

struct HuffEncoder {
    out: Vec<u8>,
    acc: u32,
    nbits: u32,
}

impl HuffEncoder {
    fn new() -> Self {
        HuffEncoder { out: Vec::new(), acc: 0, nbits: 0 }
    }

    #[inline]
    fn emit_bits(&mut self, code: u32, size: u32) {
        self.acc = (self.acc << size) | (code & ((1u32 << size) - 1));
        self.nbits += size;
        while self.nbits >= 8 {
            let b = ((self.acc >> (self.nbits - 8)) & 0xFF) as u8;
            self.out.push(b);
            if b == 0xFF {
                self.out.push(0x00);
            }
            self.nbits -= 8;
        }
        self.acc &= (1u32 << self.nbits) - 1;
    }

    fn flush(&mut self) {
        // pad the final partial byte with 1-bits
        if self.nbits > 0 {
            let pad = 8 - self.nbits;
            let b = ((self.acc << pad) | ((1u32 << pad) - 1)) as u8;
            self.out.push(b);
            if b == 0xFF {
                self.out.push(0x00);
            }
            self.nbits = 0;
            self.acc = 0;
        }
    }
}

/// Standard tables for a component class (luma id 0 / chroma id 1).
struct StdHuff {
    dc: HuffTable,
    ac: HuffTable,
}

fn std_huff() -> [StdHuff; 2] {
    [
        StdHuff {
            dc: HuffTable::build(&DC_LUMA_BITS, &DC_LUMA_VALS).unwrap(),
            ac: HuffTable::build(&AC_LUMA_BITS, &AC_LUMA_VALS).unwrap(),
        },
        StdHuff {
            dc: HuffTable::build(&DC_CHROMA_BITS, &DC_CHROMA_VALS).unwrap(),
            ac: HuffTable::build(&AC_CHROMA_BITS, &AC_CHROMA_VALS).unwrap(),
        },
    ]
}

/// symbol → (code, length) map built from a HuffTable.
fn encode_map(t: &HuffTable) -> [(u16, u8); 256] {
    let mut m = [(0u16, 0u8); 256];
    let mut idx = 0usize;
    let mut acc = 0usize;
    for len in 1..=16usize {
        acc += t.bits[len - 1] as usize;
        while idx < acc && idx < t.vals.len() {
            m[t.vals[idx] as usize] = (t.codes[idx], len as u8);
            idx += 1;
        }
    }
    m
}

impl HuffTable {
    #[allow(dead_code)]
    fn bits_for_index(&self, idx: usize) -> u8 {
        let mut acc = 0usize;
        for len in 1..=16usize {
            acc += self.bits[len - 1] as usize;
            if idx < acc {
                return len as u8;
            }
        }
        0
    }
}

// --------------------------------------------------------------------------- //
// Public encode API
// --------------------------------------------------------------------------- //

fn check_input(len: usize, w: u32, h: u32, bpp: usize) -> CoreResult<()> {
    if w == 0 || h == 0 || w > 65535 || h > 65535 {
        return Err(CoreError::jpeg(format!("编码尺寸 {w}×{h} 非法")));
    }
    if len != w as usize * h as usize * bpp {
        return Err(CoreError::jpeg("编码输入长度与尺寸不符"));
    }
    Ok(())
}

/// Encode RGB (interleaved) as baseline JPEG.
pub fn encode_rgb(
    rgb: &[u8],
    w: u32,
    h: u32,
    cfg: &EncoderCfg,
) -> CoreResult<Vec<u8>> {
    check_input(rgb.len(), w, h, 3)?;
    let (w, h) = (w as usize, h as usize);
    let max_h = cfg.sampling.max_h() as usize;
    let max_v = cfg.sampling.max_v() as usize;
    let mcus_x = (w + max_h * 8 - 1) / (max_h * 8);
    let mcus_y = (h + max_v * 8 - 1) / (max_v * 8);

    // color convert into full-resolution component planes (skipped for the
    // RGB stream mode — the components ARE the channels)
    let mut y_full;
    let mut cb_full;
    let mut cr_full;
    if cfg.rgb {
        y_full = vec![0u8; w * h];
        cb_full = vec![0u8; w * h];
        cr_full = vec![0u8; w * h];
        for p in 0..w * h {
            y_full[p] = rgb[p * 3];
            cb_full[p] = rgb[p * 3 + 1];
            cr_full[p] = rgb[p * 3 + 2];
        }
    } else {
        let t = rgb_ycc_tables();
        y_full = vec![0u8; w * h];
        cb_full = vec![0u8; w * h];
        cr_full = vec![0u8; w * h];
        for p in 0..w * h {
            let r = rgb[p * 3] as usize;
            let g = rgb[p * 3 + 1] as usize;
            let b = rgb[p * 3 + 2] as usize;
            y_full[p] = ((t.r_y[r] + t.g_y[g] + t.b_y[b]) >> J_SCALEBITS) as u8;
            cb_full[p] = ((t.r_cb[r] + t.g_cb[g] + t.b_cb[b]) >> J_SCALEBITS) as u8;
            cr_full[p] = ((t.r_cr[r] + t.g_cr[g] + t.b_cr[b]) >> J_SCALEBITS) as u8;
        }
    }

    // downsample + pad into block-aligned planes
    let y_plane = build_plane(&y_full, w, h, mcus_x * max_h * 8, mcus_y * max_v * 8, false, false);
    let c_stride = mcus_x * 8;
    let c_rows = mcus_y * 8;
    let half_h = max_h == 2;
    let half_v = max_v == 2;
    let cb_plane = build_plane(&cb_full, w, h, c_stride, c_rows, half_h, half_v);
    let cr_plane = build_plane(&cr_full, w, h, c_stride, c_rows, half_h, half_v);

    // cfg tables are already natural order
    let y_q_nat = cfg.y_q;
    let c_q_nat = cfg.c_q;

    // header
    let mut out = Vec::with_capacity(w * h / 2 + 2048);
    write_header_rgb(&mut out, w as u32, h as u32, cfg);

    // entropy
    let tabs = std_huff();
    let y_dc = encode_map(&tabs[0].dc);
    let y_ac = encode_map(&tabs[0].ac);
    let c_dc = encode_map(&tabs[1].dc);
    let c_ac = encode_map(&tabs[1].ac);
    let mut he = HuffEncoder::new();
    let mut last_dc = [0i32; 3];
    let mut samples = [0i32; 64];

    // Dummy-block geometry (jccoefct.c compress_data): blocks entirely past
    // the image edge are encoded as all-zero ACs with the DC of the block to
    // the left (right edge) / of the last block of the row above (bottom).
    let y_wib_real = (w + 7) / 8;
    let y_hib_real = (h + 7) / 8;
    let y_last_col = y_wib_real - (mcus_x - 1) * max_h;
    let y_last_row = y_hib_real - (mcus_y - 1) * max_v;

    for mcu_row in 0..mcus_y {
        for mcu_col in 0..mcus_x {
            // Y: max_h × max_v blocks (with edge dummies)
            let blockcnt = if mcu_col < mcus_x - 1 { max_h } else { y_last_col };
            let mut row_end_dc = last_dc[0];
            for yindex in 0..max_v {
                let real_row = mcu_row < mcus_y - 1 || yindex < y_last_row;
                if real_row {
                    let py = (mcu_row * max_v + yindex) * 8;
                    for bx in 0..blockcnt {
                        let px = (mcu_col * max_h + bx) * 8;
                        gather_block(&y_plane, px, py, &mut samples);
                        encode_block(
                            &mut he, &samples, &y_q_nat, &y_dc, &y_ac,
                            &mut last_dc[0],
                        );
                    }
                    // right-edge dummies: DC copied from the left neighbour
                    for _ in blockcnt..max_h {
                        encode_dummy(&mut he, &y_dc, &y_ac, last_dc[0], &mut last_dc[0]);
                    }
                } else {
                    // bottom dummy rows: DC of the last block of the row above
                    for _ in 0..max_h {
                        encode_dummy(&mut he, &y_dc, &y_ac, row_end_dc, &mut last_dc[0]);
                    }
                }
                row_end_dc = last_dc[0];
            }
            // Cb/Cr: 1×1 blocks each (never dummy for 4:4:4/4:2:2/4:2:0)
            let px = mcu_col * 8;
            let py = mcu_row * 8;
            gather_block(&cb_plane, px, py, &mut samples);
            encode_block(&mut he, &samples, &c_q_nat, &c_dc, &c_ac, &mut last_dc[1]);
            gather_block(&cr_plane, px, py, &mut samples);
            encode_block(&mut he, &samples, &c_q_nat, &c_dc, &c_ac, &mut last_dc[2]);
        }
    }
    he.flush();
    out.extend_from_slice(&he.out);
    out.extend_from_slice(&[0xFF, 0xD9]);
    Ok(out)
}

/// Encode 8-bit grayscale as baseline JPEG.
pub fn encode_gray(gray: &[u8], w: u32, h: u32, q_zz: &[u16; 64]) -> CoreResult<Vec<u8>> {
    check_input(gray.len(), w, h, 1)?;
    let (w, h) = (w as usize, h as usize);
    let stride = (w + 7) / 8 * 8;
    let rows = (h + 7) / 8 * 8;
    let mut plane = vec![0u8; stride * rows];
    for y in 0..rows {
        let sy = y.min(h - 1);
        let s = &gray[sy * w..][..w];
        let d = &mut plane[y * stride..][..stride];
        d[..w].copy_from_slice(s);
        for x in w..stride {
            d[x] = s[w - 1];
        }
    }
    let plane = EncPlane { data: plane, stride, rows };
    let q_nat = *q_zz; // natural order (Pillow contract)
    let mut out = Vec::with_capacity(w * h / 2 + 1024);
    write_header_gray(&mut out, w as u32, h as u32, q_zz);
    let tabs = std_huff();
    let dc = encode_map(&tabs[0].dc);
    let ac = encode_map(&tabs[0].ac);
    let mut he = HuffEncoder::new();
    let mut last_dc = 0i32;
    let mut samples = [0i32; 64];
    for by in 0..(h + 7) / 8 {
        for bx in 0..(w + 7) / 8 {
            gather_block(&plane, bx * 8, by * 8, &mut samples);
            encode_block(&mut he, &samples, &q_nat, &dc, &ac, &mut last_dc);
        }
    }
    he.flush();
    out.extend_from_slice(&he.out);
    out.extend_from_slice(&[0xFF, 0xD9]);
    Ok(out)
}

/// Encode a dummy edge block (jccoefct.c): all ACs zero, DC = `target_dc`.
fn encode_dummy(
    he: &mut HuffEncoder,
    dc_map: &[(u16, u8); 256],
    ac_map: &[(u16, u8); 256],
    target_dc: i32,
    last_dc: &mut i32,
) {
    let temp = target_dc - *last_dc;
    *last_dc = target_dc;
    let mag = if temp < 0 { -temp } else { temp };
    let temp2 = if temp < 0 { temp - 1 } else { temp };
    let nbits = bit_category(mag) as u32;
    let (code, clen) = dc_map[nbits as usize];
    he.emit_bits(code as u32, clen as u32);
    if nbits > 0 {
        he.emit_bits((temp2 & ((1i32 << nbits) - 1)) as u32, nbits);
    }
    let (code, clen) = ac_map[0x00];
    he.emit_bits(code as u32, clen as u32);
}

fn gather_block(pl: &EncPlane, px: usize, py: usize, samples: &mut [i32; 64]) {
    for r in 0..8 {
        let row = &pl.data[(py + r) * pl.stride..][..pl.stride];
        for c in 0..8 {
            samples[r * 8 + c] = row[px + c] as i32 - 128;
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn encode_block(
    he: &mut HuffEncoder,
    samples: &[i32; 64],
    q_nat: &[u16; 64],
    dc_map: &[(u16, u8); 256],
    ac_map: &[(u16, u8); 256],
    last_dc: &mut i32,
) {
    let mut coef = [0i32; 64];
    fdct_islow(samples, &mut coef);
    // quantize
    for i in 0..64 {
        coef[i] = quantize(coef[i], q_nat[i]);
    }
    // DC: diff, one's-complement emission (jchuff.c encode_one_block)
    let temp = coef[0] - *last_dc;
    *last_dc = coef[0];
    // libjpeg: temp2 = temp; if (temp < 0) { temp = -temp; temp2--; }
    let mag = if temp < 0 { -temp } else { temp };
    let temp2 = if temp < 0 { temp - 1 } else { temp };
    let nbits = bit_category(mag) as u32;
    let (code, clen) = dc_map[nbits as usize];
    he.emit_bits(code as u32, clen as u32);
    if nbits > 0 {
        he.emit_bits((temp2 & ((1i32 << nbits) - 1)) as u32, nbits);
    }
    // AC
    let mut r: u32 = 0;
    for k in 1..64usize {
        let v = coef[NATURAL_OF_ZIGZAG[k]];
        if v == 0 {
            r += 1;
            continue;
        }
        let mag = if v < 0 { -v } else { v };
        let temp2 = if v < 0 { v - 1 } else { v };
        let nbits = bit_category(mag) as u32;
        while r > 15 {
            let (code, clen) = ac_map[0xF0];
            he.emit_bits(code as u32, clen as u32);
            r -= 16;
        }
        let (code, clen) = ac_map[((r << 4) | nbits) as usize];
        if clen == 0 {
            // symbol not in table (cannot happen with std tables)
            return;
        }
        he.emit_bits(code as u32, clen as u32);
        he.emit_bits((temp2 & ((1i32 << nbits) - 1)) as u32, nbits);
        r = 0;
    }
    if r > 0 {
        let (code, clen) = ac_map[0x00];
        he.emit_bits(code as u32, clen as u32);
    }
}

// --------------------------------------------------------------------------- //
// Marker emission (exact Pillow/libjpeg layout)
// --------------------------------------------------------------------------- //

fn push_marker(out: &mut Vec<u8>, marker: u8, payload: &[u8]) {
    out.push(0xFF);
    out.push(marker);
    let len = (payload.len() + 2) as u16;
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(payload);
}

fn write_jfif_app0(out: &mut Vec<u8>) {
    push_marker(
        out,
        0xE0,
        &[
            b'J', b'F', b'I', b'F', 0, 1, 1, 0, 0, 1, 0, 1, 0, 0,
        ],
    );
}

fn write_dqt(out: &mut Vec<u8>, id: u8, q_nat: &[u16; 64]) {
    let q_zz = zigzag_from_natural(q_nat);
    let mut p = Vec::with_capacity(65);
    p.push(id);
    for &v in &q_zz {
        p.push(v as u8); // caller guarantees 1..=255
    }
    push_marker(out, 0xDB, &p);
}

fn write_header_rgb(out: &mut Vec<u8>, w: u32, h: u32, cfg: &EncoderCfg) {
    out.extend_from_slice(&[0xFF, 0xD8]);
    if cfg.rgb {
        // Adobe APP14 (transform 0 = RGB), the marker libjpeg/Pillow emit
        // for RGB saves; component ids 'R','G','B' per jcmaster's JCS_RGB
        push_marker(out, 0xEE, &[
            b'A', b'd', b'o', b'b', b'e', 0x00, 100, 0, 0, 0, 0, 0, 0,
        ]);
    } else {
        write_jfif_app0(out);
    }
    write_dqt(out, 0, &cfg.y_q);
    write_dqt(out, 1, &cfg.c_q);
    let (hmax, vmax) = match cfg.sampling {
        Sampling::S444 => (1u8, 1u8),
        Sampling::S422 => (2, 1),
        Sampling::S420 => (2, 2),
    };
    let (id0, id1, id2) = if cfg.rgb {
        (b'R', b'G', b'B')
    } else {
        (1u8, 2u8, 3u8)
    };
    let sof = vec![
        8,
        (h >> 8) as u8, h as u8,
        (w >> 8) as u8, w as u8,
        3,
        id0, (hmax << 4) | vmax, 0,
        id1, 0x11, 1,
        id2, 0x11, 1,
    ];
    push_marker(out, 0xC0, &sof);
    // DHT: per component (DC then AC), duplicates suppressed → DC0 AC0 DC1 AC1
    let tabs = std_huff();
    write_dht(out, 0, 0, &tabs[0].dc);
    write_dht(out, 1, 0, &tabs[0].ac);
    write_dht(out, 0, 1, &tabs[1].dc);
    write_dht(out, 1, 1, &tabs[1].ac);
    let sos = [3u8, id0, 0x00, id1, 0x11, id2, 0x11, 0, 63, 0];
    push_marker(out, 0xDA, &sos);
}

fn write_header_gray(out: &mut Vec<u8>, w: u32, h: u32, q_zz: &[u16; 64]) {
    out.extend_from_slice(&[0xFF, 0xD8]);
    write_jfif_app0(out);
    write_dqt(out, 0, q_zz);
    let sof = vec![
        8u8,
        (h >> 8) as u8, h as u8,
        (w >> 8) as u8, w as u8,
        1,
        1, 0x11, 0,
    ];
    push_marker(out, 0xC0, &sof);
    let tabs = std_huff();
    write_dht(out, 0, 0, &tabs[0].dc);
    write_dht(out, 1, 0, &tabs[0].ac);
    let sos = [1u8, 1, 0x00, 0, 63, 0];
    push_marker(out, 0xDA, &sos);
}

fn write_dht(out: &mut Vec<u8>, class: u8, id: u8, t: &HuffTable) {
    let mut p = Vec::with_capacity(1 + 16 + t.vals.len());
    p.push((class << 4) | id);
    p.extend_from_slice(&t.bits);
    p.extend_from_slice(&t.vals);
    push_marker(out, 0xC4, &p);
}
