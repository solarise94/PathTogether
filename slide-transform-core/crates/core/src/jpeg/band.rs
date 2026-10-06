//! Streaming MCU-row band decoder for baseline JPEG scans WITHOUT restart
//! markers (F8 raster adapter).
//!
//! A whole-image JPEG without restart markers offers no byte-aligned decode
//! unit: entropy segments are bit-packed and cannot be located by scanning.
//! This module decodes the scan MCU row by MCU row (each band = one MCU row
//! = `max_v × 8` pixel rows — the only bounded unit a restart-less scan
//! has) while pulling entropy bytes from a [`ByteSource`] in bounded 64 KiB
//! windows, so neither the compressed input nor the decompressed working set
//! is ever the whole image.
//!
//! Byte identity with [`super::decoder::decode_ex`]: the entropy decode
//! reuses the shared `decode_block` / `idct_islow_block` kernels with the
//! same predictors and zero-padding semantics, and the band upsamplers are
//! the whole-frame kernels applied to a sliding window of component rows
//! (current MCU row + one decoded-ahead MCU row + the previous MCU row's
//! last row — exactly the neighbourhoods `upsample_all` reads, with the same
//! frame-edge clamps). `tests::band_matches_whole_frame_decode` pins this
//! byte-for-byte across samplings and odd sizes.
//!
//! Resume: bands are strictly sequential; `skip_to` decodes (and drops) the
//! intervening bands so a resumed run reproduces the same bit position and
//! predictor state as an uninterrupted one — output stays byte-identical.

use super::decoder::{
    build_range_limit, clamp_u8, decode_block, h2v1_upsample_row, h2v2_fancy_row, parse_dht,
    parse_dqt, parse_sof, parse_sos, ycc_tables, BitSource, CompSpec, Frame, SCALEBITS,
};
use super::tables::HuffTable;
use super::tables::NATURAL_OF_ZIGZAG;
use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;

/// Bounded entropy window (bytes pulled from the source per read).
pub const SCAN_CHUNK: u64 = 64 * 1024;

/// One decoded band: `rows` valid pixel rows starting at global row `y0`,
/// interleaved RGB, `width` columns (`rows × width × 3` bytes); `y0 + rows`
/// is always within the frame.
pub struct JpegBand {
    pub y0: u32,
    pub rows: u32,
    pub width: u32,
    pub data: Vec<u8>,
}

// --------------------------------------------------------------------------- //
// streaming bit reader (BitReader semantics over a bounded ByteSource window)
// --------------------------------------------------------------------------- //

/// Bit-level reader mirroring `decoder::BitReader` (FF00 unstuffing; zeros
/// past the scan end / after a marker) but sourcing bytes from `src` in
/// bounded [`SCAN_CHUNK`] windows. A read error mid-scan is treated as
/// exhaustion (the same zero-padding the in-memory reader applies past its
/// buffer end) — the staged source copy is fully verified by the host before
/// conversion, so a mid-scan read failure cannot corrupt a checked identity.
struct StreamBits<'a> {
    src: &'a dyn ByteSource,
    /// Next file offset to pull into the window.
    at: u64,
    /// One past the last byte the scan may read.
    end: u64,
    buf: Vec<u8>,
    pos: usize,
    /// A non-FF00 marker stopped the stream (BitReader.hit_marker).
    hit: bool,
    bits: u64,
    nbits: u32,
}

impl<'a> StreamBits<'a> {
    fn open(src: &'a dyn ByteSource, at: u64, end: u64, budget: &mut MemBudget) -> CoreResult<Self> {
        budget.charge(SCAN_CHUNK, "无 restart 扫描的熵字节窗口（64 KiB）")?;
        Ok(StreamBits {
            src,
            at,
            end,
            buf: Vec::new(),
            pos: 0,
            hit: false,
            bits: 0,
            nbits: 0,
        })
    }

    fn pull(&mut self) -> bool {
        if self.at >= self.end {
            return false;
        }
        let want = SCAN_CHUNK.min(self.end - self.at) as usize;
        match self.src.read_at(self.at, want) {
            Ok(b) => {
                self.at += want as u64;
                self.pos = 0;
                self.buf = b;
                true
            }
            Err(_) => false,
        }
    }

    /// Mirror of `BitReader::fill` with window pulls (identical bitstream
    /// semantics, including the marker stop and the FF-at-window-edge case).
    fn fill(&mut self) {
        while self.nbits <= 24 {
            if self.hit {
                return;
            }
            if self.pos >= self.buf.len() && !self.pull() {
                return; // exhausted: get() zero-pads (BitReader semantics)
            }
            let b = self.buf[self.pos];
            if b != 0xFF {
                self.pos += 1;
                self.bits = (self.bits << 8) | b as u64;
                self.nbits += 8;
                continue;
            }
            // 0xFF: the following byte decides (00 → literal FF, anything
            // else → a marker stops the stream)
            let push_ff = if self.pos + 1 < self.buf.len() {
                let m = self.buf[self.pos + 1];
                if m == 0x00 {
                    self.pos += 2;
                    true
                } else {
                    false
                }
            } else {
                // the FF sits on the window's last byte: pull, then decide
                if !self.pull() || self.buf.is_empty() {
                    self.hit = true;
                    return;
                }
                let m = self.buf[0];
                if m == 0x00 {
                    self.pos = 1;
                    true
                } else {
                    false
                }
            };
            if push_ff {
                self.bits = (self.bits << 8) | 0xFF;
                self.nbits += 8;
            } else {
                self.hit = true;
                return;
            }
        }
    }
}

impl BitSource for StreamBits<'_> {
    fn get(&mut self, n: u32) -> u32 {
        if n == 0 {
            return 0;
        }
        if self.nbits < n {
            self.fill();
            if self.nbits < n {
                // exhausted: available bits, zero-padded on the right
                let have = self.nbits;
                let v = (((self.bits as u64) << (n - have)) as u32) & ((1u32 << n) - 1);
                self.nbits = 0;
                self.bits = 0;
                return v;
            }
        }
        self.nbits -= n;
        ((self.bits >> self.nbits) & ((1u64 << n) - 1)) as u32
    }
}

// --------------------------------------------------------------------------- //
// band scanner
// --------------------------------------------------------------------------- //

struct BandComp {
    c: CompSpec,
    /// Padded plane row width in px (mcus_x × h × 8).
    stride: usize,
    /// TRUE subsampled row width (⌈w·h/max_h⌉) — the value the whole-frame
    /// upsamplers use for their edge handling.
    ds_w: usize,
    /// Plane rows per MCU row (v × 8).
    rows: usize,
    /// Current MCU row's plane.
    data: Vec<u8>,
    /// Decoded-ahead MCU row's plane (lookahead for the v-neighbourhood).
    next: Vec<u8>,
    has_next: bool,
    /// Last plane row of the previous MCU row (the h2v2 "above" neighbour).
    carry: Vec<u8>,
    has_carry: bool,
}

/// Total subsampled plane rows of a component over the whole frame
/// (⌈h·v/max_v⌉ — the `ds_h` the whole-frame upsamplers clamp against).
fn ds_total(height: u32, v: u8, max_v: u32) -> usize {
    (height as usize * v as usize + max_v as usize - 1) / max_v as usize
}

/// Sequential MCU-row band decoder over one baseline scan without restart
/// markers (`restart_interval == 0`; scans WITH restarts go through
/// [`crate::segment::SegmentReader`]).
pub struct BandScanner<'a> {
    bits: StreamBits<'a>,
    comps: Vec<BandComp>,
    dc_tbl: [Option<HuffTable>; 4],
    ac_tbl: [Option<HuffTable>; 4],
    quant_nat: Vec<Option<[u16; 64]>>,
    range_limit: [u8; 1024],
    width: u32,
    height: u32,
    mcus_x: u32,
    mcus_y: u32,
    mcu_h: u32,
    max_h: u32,
    max_v: u32,
    /// false = YCbCr→RGB conversion (default); true = channel copy.
    force_rgb: bool,
    last_dc: [i32; 4],
    /// MCU row currently held in `data` (the one being output).
    row: u32,
    /// Reused RGB output buffer of `upsample_band`.
    out: Vec<u8>,
}

impl<'a> BandScanner<'a> {
    /// Parse `head` ([SOI … SOS segment], as captured by the probe) and open
    /// the streaming scan at its end. The head must already be validated
    /// (baseline SOF, 3 components, no restart interval, dims within caps —
    /// the raster probe's contract); this re-walk parses the DQT/DHT tables
    /// the entropy decoder needs.
    pub fn open(
        src: &'a dyn ByteSource,
        head: &[u8],
        head_offset: u64,
        file_end: u64,
        force_rgb: bool,
        budget: &mut MemBudget,
    ) -> CoreResult<Self> {
        if head.len() < 4 || head[0..2] != [0xFF, 0xD8] {
            return Err(CoreError::jpeg("band 扫描：JPEG 头缺少 SOI"));
        }
        let mut quant: Vec<Option<[u16; 64]>> = vec![None; 4];
        let mut dht_dc: [Option<HuffTable>; 4] = [None, None, None, None];
        let mut dht_ac: [Option<HuffTable>; 4] = [None, None, None, None];
        let mut frame: Option<Frame> = None;
        let mut restart_interval: u32 = 0;
        let mut scan_pos = 0usize;
        let mut i = 2usize;
        while i + 4 <= head.len() {
            if head[i] != 0xFF {
                return Err(CoreError::jpeg("JPEG 标记流错位"));
            }
            while i < head.len() && head[i] == 0xFF {
                i += 1;
            }
            if i >= head.len() {
                break;
            }
            let marker = head[i];
            i += 1;
            if marker == 0xD9 {
                break;
            }
            if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
                continue;
            }
            if i + 2 > head.len() {
                return Err(CoreError::jpeg("JPEG 标记段截断"));
            }
            let seg_len = ((head[i] as usize) << 8) | head[i + 1] as usize;
            if seg_len < 2 || i + seg_len > head.len() {
                return Err(CoreError::jpeg("JPEG 段长度非法（头超出探测上限？）"));
            }
            let seg = &head[i + 2..i + seg_len];
            match marker {
                0xC2 | 0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF => {
                    return Err(CoreError::jpeg(format!(
                        "不支持渐进/分层/无损 JPEG（SOF FF{marker:02X}）"
                    )));
                }
                0xC4 => parse_dht(seg, &mut dht_dc, &mut dht_ac)?,
                0xDB => parse_dqt(seg, &mut quant)?,
                0xC8 | 0xCC => return Err(CoreError::jpeg("JPG/算术编码扩展不受支持")),
                0xDD => {
                    if seg.len() < 2 {
                        return Err(CoreError::jpeg("DRI 段残缺"));
                    }
                    restart_interval = ((seg[0] as u32) << 8) | seg[1] as u32;
                }
                m if m == 0xC0 || m == 0xC1 => frame = Some(parse_sof(seg)?),
                0xDA => {
                    let f = frame
                        .as_mut()
                        .ok_or_else(|| CoreError::jpeg("SOS 先于 SOF"))?;
                    parse_sos(seg, f)?;
                    scan_pos = i + seg_len;
                    break;
                }
                _ => {}
            }
            i += seg_len;
        }
        let frame = frame.ok_or_else(|| CoreError::jpeg("JPEG 缺少 SOF"))?;
        if scan_pos == 0 {
            return Err(CoreError::jpeg("JPEG 缺少 SOS"));
        }
        if restart_interval != 0 {
            return Err(CoreError::variant(
                "band 扫描只接受无 restart interval 的 JPEG（带 DRI 的流走分段路径）",
            ));
        }
        // ---- geometry (mirror decode_ex) ---------------------------------- //
        let comps = frame.comps;
        if comps.len() != 3 {
            return Err(CoreError::jpeg("band 扫描只接受三分量 JPEG"));
        }
        let max_h = comps.iter().map(|c| c.h).max().unwrap() as u32;
        let max_v = comps.iter().map(|c| c.v).max().unwrap() as u32;
        if max_h == 0 || max_v == 0 || max_h > 4 || max_v > 4 {
            return Err(CoreError::jpeg("采样因子非法"));
        }
        let (w, h) = (frame.width, frame.height);
        let mcus_x = u32::try_from((w as u64 + (max_h * 8) as u64 - 1) / (max_h * 8) as u64)
            .map_err(|_| CoreError::jpeg("MCU 数溢出"))?;
        let mcus_y = u32::try_from((h as u64 + (max_v * 8) as u64 - 1) / (max_v * 8) as u64)
            .map_err(|_| CoreError::jpeg("MCU 数溢出"))?;

        // planes + band buffers, charged BEFORE any allocation (review §1)
        let mut bcomps: Vec<BandComp> = Vec::with_capacity(3);
        let mut ws = SCAN_CHUNK;
        for c in &comps {
            let stride = mcus_x as usize * c.h as usize * 8;
            let rows = c.v as usize * 8;
            let bytes = (stride.saturating_mul(rows).saturating_mul(2) + stride) as u64;
            ws = ws.saturating_add(bytes.saturating_mul(3)); // planes + RGB 带 + 余量
            bcomps.push(BandComp {
                c: c.clone(),
                stride,
                ds_w: (((w as u64 * c.h as u64) + max_h as u64 - 1) / max_h as u64) as usize,
                rows,
                data: vec![0u8; stride * rows],
                next: vec![0u8; stride * rows],
                has_next: false,
                carry: vec![0u8; stride],
                has_carry: false,
            });
        }
        ws = ws.saturating_add(
            (w as u64)
                .saturating_mul(self_max_v8(max_v))
                .saturating_mul(9), // 上采样平面 ×3 + RGB 带余量
        );
        budget.charge(ws, "band 扫描工作集（MCU 行平面 ×2 + 上采样带 + 64 KiB 窗口）")?;

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
        let bits = StreamBits::open(src, head_offset + scan_pos as u64, file_end, budget)?;
        Ok(BandScanner {
            bits,
            comps: bcomps,
            dc_tbl: dht_dc,
            ac_tbl: dht_ac,
            quant_nat,
            range_limit: build_range_limit(),
            width: w,
            height: h,
            mcus_x,
            mcus_y,
            mcu_h: max_v * 8,
            max_h,
            max_v,
            force_rgb,
            last_dc: [0; 4],
            row: 0,
            out: Vec::new(),
        })
    }

    pub fn width(&self) -> u32 {
        self.width
    }
    pub fn height(&self) -> u32 {
        self.height
    }
    /// Bands = MCU rows.
    pub fn bands_total(&self) -> u32 {
        self.mcus_y
    }
    /// Pixel rows per band (the MCU height; the last band is clipped).
    pub fn band_rows(&self) -> u32 {
        self.mcu_h
    }
    /// Global pixel row the NEXT band starts at (tile-row fast-forward math).
    pub fn next_band_y0(&self) -> u64 {
        self.row as u64 * self.mcu_h as u64
    }

    /// Decode the next band (one MCU row). Returns `None` after the last.
    pub fn next_band(&mut self) -> CoreResult<Option<JpegBand>> {
        if self.row >= self.mcus_y {
            return Ok(None);
        }
        let r = self.row;
        if self.comps[0].has_next {
            // the decoded-ahead row becomes the current one
            for c in self.comps.iter_mut() {
                std::mem::swap(&mut c.data, &mut c.next);
                c.has_next = false;
            }
        } else {
            self.decode_mcu_row(false)?;
        }
        if r + 1 < self.mcus_y {
            self.decode_mcu_row(true)?;
            for c in self.comps.iter_mut() {
                c.has_next = true;
            }
        }
        let band = self.upsample_band(r)?;
        // carry: the last plane row of the row just consumed
        for c in self.comps.iter_mut() {
            let last = (c.rows - 1) * c.stride;
            c.carry.copy_from_slice(&c.data[last..last + c.stride]);
            c.has_carry = true;
        }
        self.row += 1;
        Ok(Some(band))
    }

    /// Fast-forward (decode-and-drop) until band `band` becomes the next
    /// output. Predictors/bit position advance exactly as in an uninterrupted
    /// run, so a resumed run's output is byte-identical.
    pub fn skip_to(&mut self, band: u32) -> CoreResult<()> {
        if band > self.mcus_y {
            return Err(CoreError::validation("resume: band 游标越界"));
        }
        while self.row < band {
            self.next_band()?;
        }
        Ok(())
    }

    /// Decode one MCU row into the current (`false`) or lookahead (`true`)
    /// planes. Mirrors the decode_ex entropy loop with band-local py.
    fn decode_mcu_row(&mut self, into_next: bool) -> CoreResult<()> {
        let mcus_x = self.mcus_x as usize;
        let mut coef = [0i32; 64];
        let mut block = [0u8; 64];
        let mut last_dc = self.last_dc;
        for mcu_col in 0..mcus_x {
            for ci in 0..self.comps.len() {
                let c = &self.comps[ci];
                let Some(dc_t) = self.dc_tbl[c.c.dc_tbl as usize].as_ref() else {
                    return Err(CoreError::jpeg("DC Huffman 表未定义"));
                };
                let Some(ac_t) = self.ac_tbl[c.c.ac_tbl as usize].as_ref() else {
                    return Err(CoreError::jpeg("AC Huffman 表未定义"));
                };
                let Some(qz) = self.quant_nat[c.c.tq as usize].as_ref() else {
                    return Err(CoreError::jpeg("量化表未定义"));
                };
                let stride = c.stride;
                let (c_h, c_v) = (c.c.h as usize, c.c.v as usize);
                let plane: &mut [u8] = if into_next {
                    &mut self.comps[ci].next
                } else {
                    &mut self.comps[ci].data
                };
                for by in 0..c_v {
                    for bx in 0..c_h {
                        decode_block(&mut self.bits, dc_t, ac_t, &mut last_dc[ci], &mut coef);
                        super::decoder::idct_islow_block(&coef, qz, &self.range_limit, &mut block);
                        let px = (mcu_col * c_h + bx) * 8;
                        let py = by * 8;
                        for r in 0..8 {
                            let dst = (py + r) * stride + px;
                            plane[dst..dst + 8].copy_from_slice(&block[r * 8..r * 8 + 8]);
                        }
                    }
                }
            }
        }
        self.last_dc = last_dc;
        Ok(())
    }

    /// Upsample + color-convert the band's output rows (identical rules to
    /// the whole-frame `upsample_all` + YCbCr→RGB loop, applied to the
    /// sliding window of plane rows).
    fn upsample_band(&mut self, r: u32) -> CoreResult<JpegBand> {
        let band_y0 = r * self.mcu_h;
        let rows_out = (self.height - band_y0).min(self.mcu_h);
        let w = self.width as usize;
        let out_px = rows_out as usize * w;
        self.out.clear();
        self.out.resize(out_px * 3, 0);
        let mut planes: Vec<Vec<u8>> = Vec::with_capacity(3);
        for (ci, c) in self.comps.iter().enumerate() {
            let ch = c.c.h as u32;
            let cv = c.c.v as u32;
            let identity = ch == self.max_h && cv == self.max_v;
            let half_h = ch * 2 == self.max_h;
            let half_v = cv * 2 == self.max_v;
            let mut pl = vec![0u8; out_px];
            for row_in_band in 0..rows_out as usize {
                let y = band_y0 as usize + row_in_band; // global output row
                if identity || (half_h && !half_v) {
                    let src_row = self
                        .comp_row(ci, y as i64)
                        .ok_or_else(|| CoreError::jpeg("band 行越界"))?;
                    let dst = &mut pl[row_in_band * w..][..w];
                    if identity {
                        let n = w.min(src_row.len());
                        dst[..n].copy_from_slice(&src_row[..n]);
                    } else {
                        h2v1_upsample_row(src_row, c.ds_w, dst, w);
                    }
                } else if half_h && half_v {
                    let total = ds_total(self.height, c.c.v, self.max_v);
                    let rr = y / 2; // global ds row
                    let cur = self
                        .comp_row(ci, rr as i64)
                        .ok_or_else(|| CoreError::jpeg("band ds 行越界"))?;
                    // whole-frame rule: output row 2r mixes the row ABOVE,
                    // 2r+1 the row BELOW (edge rows replicate via clamp)
                    let nb = if y % 2 == 0 {
                        self.comp_row(ci, rr as i64 - 1).unwrap_or(cur)
                    } else if rr + 1 <= total.saturating_sub(1) {
                        self.comp_row(ci, rr as i64 + 1).unwrap_or(cur)
                    } else {
                        self.comp_row(ci, total as i64 - 1).unwrap_or(cur)
                    };
                    if c.ds_w > 2 {
                        h2v2_fancy_row(
                            nb,
                            cur,
                            c.ds_w,
                            &mut pl,
                            w,
                            rows_out as usize,
                            row_in_band,
                        );
                    } else {
                        // h2v2_upsample duplication branch (both axes)
                        let dst = &mut pl[row_in_band * w..][..w];
                        let mut o = 0usize;
                        for x in 0..c.ds_w {
                            for _ in 0..2 {
                                if o < w {
                                    dst[o] = cur[x];
                                    o += 1;
                                }
                            }
                        }
                    }
                } else {
                    // integer expansion (mixed sampling ratios)
                    let h_exp = (self.max_h / ch) as usize;
                    let v_exp = (self.max_v / cv) as usize;
                    if h_exp == 0 || v_exp == 0 || h_exp > 4 || v_exp > 4 {
                        return Err(CoreError::jpeg("不支持的采样比"));
                    }
                    let numpix = (h_exp * v_exp) as u32;
                    let numpix2 = numpix / 2;
                    let total = ds_total(self.height, c.c.v, self.max_v);
                    let sy0 = y / v_exp;
                    let dst = &mut pl[row_in_band * w..][..w];
                    for x in 0..c.ds_w {
                        let mut sum = numpix2 as u32;
                        for rr in 0..v_exp {
                            let sy = (sy0 + rr).min(total.saturating_sub(1));
                            let src = self
                                .comp_row(ci, sy as i64)
                                .ok_or_else(|| CoreError::jpeg("band int 行越界"))?;
                            for cc in 0..h_exp {
                                let sx = (x * h_exp + cc).min(c.ds_w - 1);
                                sum += src[sx] as u32;
                            }
                        }
                        let v = (sum / numpix) as u8;
                        for cc in 0..h_exp {
                            let ox = x * h_exp + cc;
                            if ox < w {
                                dst[ox] = v;
                            }
                        }
                    }
                }
            }
            planes.push(pl);
        }
        // ---- color (the whole-frame loop, band rows only) ---------------- //
        let out = &mut self.out;
        if self.force_rgb {
            for p in 0..out_px {
                out[p * 3] = planes[0][p];
                out[p * 3 + 1] = planes[1][p];
                out[p * 3 + 2] = planes[2][p];
            }
        } else {
            let (cr_r, cb_b, cb_g, cr_g) = ycc_tables();
            let y = &planes[0];
            let cb = &planes[1];
            let cr = &planes[2];
            for p in 0..out_px {
                let yy = y[p] as i32;
                let cbv = cb[p] as usize;
                let crv = cr[p] as usize;
                out[p * 3] = clamp_u8(yy + cr_r[crv]);
                out[p * 3 + 1] = clamp_u8(yy + ((cb_g[cbv] + cr_g[crv]) >> SCALEBITS));
                out[p * 3 + 2] = clamp_u8(yy + cb_b[cbv]);
            }
        }
        Ok(JpegBand {
            y0: band_y0,
            rows: rows_out,
            width: self.width,
            data: std::mem::take(&mut self.out),
        })
    }

    /// Global plane row `r` of component `ci`: from the carry (r = base−1),
    /// the current MCU row's plane, or the lookahead plane. `None` = outside
    /// the available window (callers replicate/clamp exactly like the
    /// whole-frame upsamplers at the frame edges).
    fn comp_row<'c>(&'c self, ci: usize, r: i64) -> Option<&'c [u8]> {
        if r < 0 {
            return None;
        }
        let c = &self.comps[ci];
        let r = r as usize;
        let base = self.row as usize * c.rows;
        if r < base {
            return if c.has_carry && r == base - 1 {
                Some(&c.carry)
            } else {
                None
            };
        }
        let within = r - base;
        if within < c.rows {
            return Some(&c.data[within * c.stride..][..c.stride]);
        }
        let within_next = within - c.rows;
        if c.has_next && within_next < c.rows {
            return Some(&c.next[within_next * c.stride..][..c.stride]);
        }
        None
    }
}

fn self_max_v8(max_v: u32) -> u64 {
    u64::from(max_v) * 8
}
