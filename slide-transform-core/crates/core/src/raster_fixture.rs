//! Synthetic plain-image (BMP/JPEG) fixture generator (F8 tests;
//! `fixtures` feature). Everything is generated in code — no sample bytes
//! are committed. Variants cover the accepted contracts (uncompressed
//! 24/32-bit BMP bottom-up/top-down, OS/2 core header; baseline JPEG with
//! row-aligned restart segments or without any restarts) AND the typed
//! rejections (RLE8, 4-bit palette, progressive stub, grayscale JPEG,
//! truncated pixel data).

use crate::error::{CoreError, CoreResult};
use crate::jpeg::{encode_gray, encode_rgb, EncoderCfg, Sampling};
use crate::ndpi::scan_strip_head;

/// Pixel content pattern.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixturePattern {
    /// Deterministic per-pixel noise.
    Noise,
    /// Locally linear per-channel ramp (re-encode/decode tolerance tests).
    Gradient,
}

#[derive(Debug, Clone)]
pub struct RasterGenParams {
    pub width: u32,
    pub height: u32,
    /// "bmp" | "jpeg".
    pub kind: String,
    /// BMP bits per pixel (24 | 32).
    pub bpp: u32,
    /// BMP InfoHeader height < 0 (top-down rows).
    pub top_down: bool,
    /// BMP OS/2 BITMAPCOREHEADER (12 B) instead of BITMAPINFOHEADER.
    pub core_header: bool,
    /// BMP compression code override (1 = RLE8 → typed rejection).
    pub compression: u32,
    /// BMP bits override (4 → typed rejection).
    pub bits_override: Option<u16>,
    /// Truncate the pixel data (declared rows extend past the file end).
    pub truncated: bool,
    /// JPEG without restart markers (→ the MCU-row band path).
    pub no_restart: bool,
    /// MCU rows per restart segment (DRI = restart_rows × mcus_x).
    pub restart_rows: u32,
    /// Replace the JPEG payload with a bare progressive-JPEG header.
    pub progressive: bool,
    /// Encode a 1-component (grayscale) JPEG instead (typed rejection).
    pub gray: bool,
    pub pattern: FixturePattern,
    pub quality: u8,
    pub seed: u64,
}

impl Default for RasterGenParams {
    fn default() -> Self {
        RasterGenParams {
            width: 512,
            height: 320,
            kind: "bmp".to_string(),
            bpp: 24,
            top_down: false,
            core_header: false,
            compression: 0,
            bits_override: None,
            truncated: false,
            no_restart: false,
            restart_rows: 1,
            progressive: false,
            gray: false,
            pattern: FixturePattern::Gradient,
            quality: 90,
            seed: 0,
        }
    }
}

struct Rng(u64);
impl Rng {
    fn byte(&mut self) -> u8 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        ((self.0.wrapping_mul(0x2545F4914F6CDD1D)) >> 33) as u8
    }
}

fn make_pixels(w: u32, h: u32, seed: u64, p: &RasterGenParams) -> Vec<u8> {
    let mut px = vec![0u8; (w as usize) * (h as usize) * 3];
    match p.pattern {
        FixturePattern::Gradient => {
            let b = (seed.wrapping_mul(0x9E37_79B9) >> 56) as i32 % 7;
            for y in 0..h as usize {
                for x in 0..w as usize {
                    let v = 110 + ((x as i32 - y as i32) / 8).clamp(-80, 80) + b;
                    let o = (y * w as usize + x) * 3;
                    px[o] = v.clamp(0, 255) as u8;
                    px[o + 1] = (v + 17).clamp(0, 255) as u8;
                    px[o + 2] = (v + 43).clamp(0, 255) as u8;
                }
            }
        }
        FixturePattern::Noise => {
            let mut rng = Rng(seed | 1);
            for c in px.chunks_exact_mut(3) {
                c[0] = rng.byte();
                c[1] = rng.byte();
                c[2] = rng.byte();
            }
        }
    }
    px
}

/// Split a complete JPEG into (header up to and incl. the SOS segment,
/// entropy bytes before the EOI).
fn split_stream(full: &[u8]) -> CoreResult<(Vec<u8>, Vec<u8>)> {
    let mut i = 2usize;
    let mut header_end = 0usize;
    while i + 4 <= full.len() {
        if full[i] != 0xFF {
            return Err(CoreError::validation("fixture: 标记流错位"));
        }
        while i < full.len() && full[i] == 0xFF {
            i += 1;
        }
        if i >= full.len() {
            break;
        }
        let m = full[i];
        i += 1;
        if m == 0xD9 {
            break;
        }
        if m == 0x01 || (0xD0..=0xD7).contains(&m) {
            continue;
        }
        if i + 2 > full.len() {
            return Err(CoreError::validation("fixture: 段截断"));
        }
        let ln = ((full[i] as usize) << 8) | full[i + 1] as usize;
        if m == 0xDA {
            header_end = i + ln;
            break;
        }
        i += ln;
    }
    if header_end == 0 || full.len() < 2 || &full[full.len() - 2..] != [0xFF, 0xD9] {
        return Err(CoreError::validation("fixture: 编码器输出缺 SOS/EOI"));
    }
    Ok((full[..header_end].to_vec(), full[header_end..full.len() - 2].to_vec()))
}

/// Build a baseline JPEG of the whole image. With restarts, the entropy is
/// assembled from row-aligned restart segments, each encoded standalone at
/// (padded_w, seg_rows) — exactly the sub-rectangles the converter's segment
/// decode rebuilds (the ndpi_fixture assembly, applied to a plain file).
fn build_jpeg(w: u32, h: u32, seed: u64, p: &RasterGenParams) -> CoreResult<Vec<u8>> {
    if p.gray {
        // 1-component JPEG (typed rejection path): a luma ramp
        let mut gray = vec![0u8; (w as usize) * (h as usize)];
        for y in 0..h as usize {
            for x in 0..w as usize {
                gray[y * w as usize + x] =
                    (110 + ((x as i32 - y as i32) / 8).clamp(-80, 80)) as u8;
            }
        }
        return encode_gray(&gray, w, h, &crate::jpeg::tables::std_luma_quality(p.quality));
    }
    if p.progressive {
        // bare progressive header (typed rejection path)
        return Ok(vec![
            0xFF, 0xD8, 0xFF, 0xC2, 0x00, 0x11, 0x08, 0x00, 0x40, 0x02, 0x00, 0x03, 0x01,
            0x22, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01, 0xFF, 0xD9,
        ]);
    }
    if p.no_restart {
        let px = make_pixels(w, h, seed, p);
        let cfg = EncoderCfg::with_quality(p.quality, Sampling::S422);
        return encode_rgb(&px, w, h, &cfg);
    }
    // restart-segmented assembly (the encoder's S422: MCU = 16×8)
    const MCU_W: u32 = 16;
    const MCU_H: u32 = 8;
    if w % MCU_W != 0 || h % MCU_H != 0 {
        return Err(CoreError::header(format!(
            "fixture: 带 restart 的夹具尺寸 {w}×{h} 需按 MCU 网格对齐（宽 16 的倍数、高 8 的倍数）"
        )));
    }
    let mcus_x = w / MCU_W;
    let mcus_y = h / MCU_H;
    let padded_h = mcus_y * MCU_H;
    let mut padded = make_pixels(w, h, seed, p);
    padded.resize((w as usize) * (padded_h as usize) * 3, 255);
    let seg_px_rows = (p.restart_rows.max(1) * MCU_H) as usize;
    let dri = p.restart_rows.max(1) * mcus_x;
    let mut out: Vec<u8> = Vec::new();
    let mut header: Option<Vec<u8>> = None;
    let mut rst = 0u8;
    let cfg = EncoderCfg::with_quality(p.quality, Sampling::S422);
    let mut sy = 0u32;
    while sy < mcus_y {
        let rows_mcus = (seg_px_rows as u32 / MCU_H).min(mcus_y - sy);
        let rows = rows_mcus * MCU_H;
        let row0 = (sy * MCU_H) as usize;
        let take = rows as usize * w as usize * 3;
        let full = encode_rgb(
            &padded[row0 * w as usize * 3..row0 * w as usize * 3 + take],
            w,
            rows,
            &cfg,
        )?;
        let (seg_header, entropy) = split_stream(&full)?;
        if header.is_none() {
            // insert the DRI segment before SOS, patch SOF dims to (w × h)
            let sh = scan_strip_head(&full)?;
            let mut at = None;
            for i in 0..seg_header.len().saturating_sub(1) {
                if seg_header[i] == 0xFF && seg_header[i + 1] == 0xDA {
                    at = Some(i);
                    break;
                }
            }
            let at = at.ok_or_else(|| CoreError::validation("fixture: 缺 SOS"))?;
            let mut head = seg_header.clone();
            head[sh.sof_hw_at..sh.sof_hw_at + 2].copy_from_slice(&(h as u16).to_be_bytes());
            head[sh.sof_hw_at + 2..sh.sof_hw_at + 4].copy_from_slice(&(w as u16).to_be_bytes());
            head.truncate(sh.header_len);
            out.extend_from_slice(&head[..at]);
            out.extend_from_slice(&[
                0xFF,
                0xDD,
                0x00,
                0x04,
                (dri >> 8) as u8,
                (dri & 0xFF) as u8,
            ]);
            out.extend_from_slice(&head[at..]);
            header = Some(head);
        }
        if sy > 0 {
            out.extend_from_slice(&[0xFF, 0xD0 + (rst & 7)]);
            rst += 1;
        }
        out.extend_from_slice(&entropy);
        sy += rows / MCU_H;
    }
    out.extend_from_slice(&[0xFF, 0xD9]);
    Ok(out)
}

/// Build the BMP bytes (header + rows, bottom-up unless `top_down`).
fn build_bmp(w: u32, h: u32, seed: u64, p: &RasterGenParams) -> CoreResult<Vec<u8>> {
    let px = make_pixels(w, h, seed, p);
    let bpp = p.bits_override.unwrap_or(p.bpp as u16);
    if bpp != 24 && bpp != 32 && p.compression == 0 {
        // palette-ish variants: 4-bit indexed rows built from the pattern
        let row_bytes = ((w as usize * bpp as usize) + 31) / 32 * 4;
        let dib: u32 = if p.core_header { 12 } else { 40 };
        let data_offset = 14 + dib as usize + 64; // 16-colour palette
        let height_field = if p.top_down { -(h as i32) } else { h as i32 };
        let mut out = Vec::new();
        out.extend_from_slice(b"BM");
        out.extend_from_slice(&((data_offset + row_bytes * h as usize) as u32).to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&(data_offset as u32).to_le_bytes());
        if p.core_header {
            out.extend_from_slice(&12u32.to_le_bytes());
            out.extend_from_slice(&(w as u16).to_le_bytes());
            out.extend_from_slice(&(height_field as i16).to_le_bytes());
            out.extend_from_slice(&1u16.to_le_bytes());
            out.extend_from_slice(&bpp.to_le_bytes());
        } else {
            out.extend_from_slice(&dib.to_le_bytes());
            out.extend_from_slice(&(w as i32).to_le_bytes());
            out.extend_from_slice(&height_field.to_le_bytes());
            out.extend_from_slice(&1u16.to_le_bytes());
            out.extend_from_slice(&bpp.to_le_bytes());
            out.extend_from_slice(&p.compression.to_le_bytes());
            out.extend_from_slice(&((row_bytes * h as usize) as u32).to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
        }
        out.extend_from_slice(&[0u8; 64]); // palette
        out.resize(data_offset + row_bytes * h as usize, 0x11);
        return Ok(out);
    }
    let px_bytes = (bpp as usize) / 8;
    let align = if p.core_header { 2usize } else { 4usize };
    let row_bytes = (w as usize * px_bytes + align - 1) / align * align;
    let dib: u32 = if p.core_header { 12 } else { 40 };
    let data_offset = 14 + dib as usize;
    let mut rows: Vec<u8> = Vec::with_capacity(row_bytes * h as usize);
    // bottom-up: row 0 in the file is the BOTTOM image row
    for fy in 0..h as usize {
        let y = if p.top_down { fy } else { h as usize - 1 - fy };
        let mut row = vec![0u8; row_bytes];
        for x in 0..w as usize {
            let s = (y * w as usize + x) * 3;
            if bpp == 32 {
                row[x * 4] = px[s + 2];
                row[x * 4 + 1] = px[s + 1];
                row[x * 4 + 2] = px[s];
                row[x * 4 + 3] = 255;
            } else {
                row[x * 3] = px[s + 2];
                row[x * 3 + 1] = px[s + 1];
                row[x * 3 + 2] = px[s];
            }
        }
        rows.extend_from_slice(&row);
    }
    if p.truncated {
        rows.truncate(rows.len() / 2); // declared rows extend past EOF
    }
    let height_field = if p.core_header {
        (h as i16).to_le_bytes().to_vec()
    } else if p.top_down {
        (-(h as i32)).to_le_bytes().to_vec()
    } else {
        (h as i32).to_le_bytes().to_vec()
    };
    let mut out = Vec::new();
    out.extend_from_slice(b"BM");
    out.extend_from_slice(&((data_offset + rows.len()) as u32).to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&(data_offset as u32).to_le_bytes());
    if p.core_header {
        out.extend_from_slice(&12u32.to_le_bytes());
        out.extend_from_slice(&(w as u16).to_le_bytes());
        out.extend_from_slice(&(h as i16).to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&bpp.to_le_bytes());
    } else {
        out.extend_from_slice(&dib.to_le_bytes());
        out.extend_from_slice(&(w as i32).to_le_bytes());
        out.extend_from_slice(&height_field);
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&bpp.to_le_bytes());
        out.extend_from_slice(&p.compression.to_le_bytes());
        out.extend_from_slice(&((row_bytes * h as usize) as u32).to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
    }
    out.extend_from_slice(&rows);
    Ok(out)
}

/// Build one synthetic plain-image file.
pub fn build_synthetic_raster(p: &RasterGenParams) -> CoreResult<Vec<u8>> {
    match p.kind.as_str() {
        "jpeg" => build_jpeg(p.width, p.height, p.seed, p),
        "bmp" => build_bmp(p.width, p.height, p.seed, p),
        other => Err(CoreError::validation(format!(
            "fixture: 未知 kind {other}（bmp|jpeg）"
        ))),
    }
}
