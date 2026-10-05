//! Synthetic Hamamatsu-style NDPI fixture generator (F6 tests; `fixtures`
//! feature). Everything is generated in code — no sample bytes are
//! committed. Layout mirrors the real structure observed on the public CC0
//! OpenSlide NDPI sample:
//!
//! ```text
//! header (classic II*\0, little-endian)
//! … strip payloads of every level (whole-layer JPEG strips with restart
//!   markers: DRI + correctly numbered RSTn runs assembled from standalone
//!   per-segment encodings) … macro / focus-map payloads
//! IFD chain at the end of the file, each IFD followed by its external
//!   values AND the NDPI per-entry 4-byte extension area:
//!   IFD0  main level (Make=Hamamatsu, SourceLens=20 float, single strip),
//!   level 1.. (SourceLens ÷4 steps), macro (SourceLens=-1),
//!   focus map (SourceLens=-2)
//! ```
//!
//! The strip's SOF declares the FULL layer dims (w × h) — libjpeg/OpenSlide
//! decode it whole and crop; the entropy is assembled from row-aligned
//! restart segments, each encoded standalone (padded to the MCU grid with
//! white beyond the level edges) with reset DC predictors. Whole-strip
//! decode and the converter's segment-wise decode therefore agree by
//! construction; the converter's unaligned-segment paste path is covered
//! end-to-end by the real-sample gate (the public sample's strips are NOT
//! row-aligned).

use crate::error::{CoreError, CoreResult};
use crate::io::RandomAccessSink;
use crate::jpeg::{encode_rgb, EncoderCfg, Sampling};
use crate::ndpi::scan_strip_head;

/// Pixel content pattern.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixturePattern {
    /// Deterministic per-pixel noise (structural tests).
    Noise,
    /// Locally linear per-channel ramp (re-encode/decode tolerance tests: a
    /// box downsample stays near-exact, so segment decode vs whole decode
    /// has no quantization cliffs).
    Gradient,
}

#[derive(Debug, Clone)]
pub struct NdpiGenParams {
    pub width: u32,
    pub height: u32,
    /// MCU rows per restart segment (DRI = restart_rows × mcus_x).
    /// Default 1 (real NanoZoomer strips keep DRI ≤ MCUs per row —
    /// OpenSlide refuses larger DRIs as corrupt JPEG).
    pub restart_rows: u32,
    /// Pyramid level count (L0 + reduced, ÷4 per step like the real files).
    pub levels: u32,
    /// Include the macro page (SourceLens = -1).
    pub macro_page: bool,
    /// Include the focus-map page (SourceLens = -2).
    pub focus_map: bool,
    /// Emit the NDPI per-entry extension area after every IFD.
    pub ndpi_extensions: bool,
    /// Vendor Make string ("Hamamatsu" default; another value exercises the
    /// vendor refusal).
    pub make: String,
    /// Encode the strip WITHOUT restart markers (→ typed「无 restart
    /// marker」refusal; the strip is one plain JPEG).
    pub no_restart: bool,
    /// Compression tag override (33005 = JPEG2000 → typed rejection).
    pub compression: u16,
    /// Replace the L0 strip payload with a bare progressive-JPEG header.
    pub progressive: bool,
    /// µm/px vendor tags (None = tags absent, MPP stays unknown).
    pub mpp: Option<f64>,
    /// ExtraSamples on IFD 0 (fluorescence-ish variant → typed rejection).
    pub extra_samples: bool,
    pub pattern: FixturePattern,
    pub quality: u8,
    pub seed: u64,
}

impl Default for NdpiGenParams {
    fn default() -> Self {
        NdpiGenParams {
            width: 512,
            height: 320,
            restart_rows: 1,
            levels: 3,
            macro_page: false,
            focus_map: false,
            ndpi_extensions: true,
            make: "Hamamatsu".to_string(),
            no_restart: false,
            compression: 7,
            progressive: false,
            mpp: Some(0.4990),
            extra_samples: false,
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

fn make_pixels(w: u32, h: u32, seed: u64, p: &NdpiGenParams) -> Vec<u8> {
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

/// Assemble one whole-layer strip JPEG with row-aligned restart markers.
///
/// The pixel content is padded to the MCU grid (white beyond the level
/// edges); each segment is encoded standalone at (padded_w, seg_rows) —
/// exactly the sub-rectangle the converter's segment decode will rebuild —
/// and the strip header is segment 0's header with the SOF dims patched to
/// the declared layer dims (w × h).
fn build_strip(w: u32, h: u32, seed: u64, p: &NdpiGenParams) -> CoreResult<Vec<u8>> {
    // our encoder's S422 is (2,1): MCU = 16×8
    const MCU_W: u32 = 16;
    const MCU_H: u32 = 8;
    if w % MCU_W != 0 || h % MCU_H != 0 {
        return Err(CoreError::header(format!(
            "fixture: 尺寸 {w}×{h} 未按 MCU 网格对齐（宽需 {MCU_W} 的倍数，高需 {MCU_H} 的倍数）"
        )));
    }
    let mcus_x = w / MCU_W;
    let mcus_y = h / MCU_H;
    let padded_h = mcus_y * MCU_H;
    let mut padded = make_pixels(w, h, seed, p);
    padded.resize((w as usize) * (padded_h as usize) * 3, 255); // white pad rows

    if p.no_restart {
        // one plain whole-layer JPEG (no DRI/RST)
        let cfg = EncoderCfg::with_quality(p.quality, Sampling::S422);
        let full = encode_rgb(&padded, w, padded_h, &cfg)?;
        let (header, entropy) = split_stream(&full)?;
        // patch the SOF dims to the DECLARED level dims (w × h)
        let sh = scan_strip_head(&full)?;
        let mut out = header;
        out[sh.sof_hw_at..sh.sof_hw_at + 2].copy_from_slice(&(h as u16).to_be_bytes());
        out.extend_from_slice(&entropy);
        out.extend_from_slice(&[0xFF, 0xD9]);
        return Ok(out);
    }

    let seg_px_rows = (p.restart_rows.max(1) * MCU_H) as usize;
    let dri = p.restart_rows.max(1) * mcus_x; // MCUs per restart segment
    let mut out: Vec<u8> = Vec::new();
    let mut header: Option<Vec<u8>> = None;
    let mut rst = 0u8;
    let cfg = EncoderCfg::with_quality(p.quality, Sampling::S422);
    let mut sy = 0u32;
    while sy < mcus_y {
        let rows = seg_px_rows as u32 / MCU_H;
        let rows = rows.min(mcus_y - sy) * MCU_H;
        let row0 = (sy * MCU_H) as usize;
        let take = rows as usize * w as usize * 3;
        let full = encode_rgb(&padded[row0 * w as usize * 3..row0 * w as usize * 3 + take], w, rows, &cfg)?;
        let (seg_header, entropy) = split_stream(&full)?;
        match &header {
            None => {
                // insert the DRI segment before the SOS marker, then patch
                // the SOF dims to the declared layer dims
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
                head[sh.sof_hw_at..sh.sof_hw_at + 2]
                    .copy_from_slice(&(h as u16).to_be_bytes());
                head[sh.sof_hw_at + 2..sh.sof_hw_at + 4]
                    .copy_from_slice(&(w as u16).to_be_bytes());
                head.truncate(sh.header_len);
                out.extend_from_slice(&head[..at]);
                out.extend_from_slice(&[
                    0xFF, 0xDD, 0x00, 0x04,
                    (dri >> 8) as u8,
                    (dri & 0xFF) as u8,
                ]);
                out.extend_from_slice(&head[at..]);
                header = Some(head);
            }
            Some(_) => {
                // strip this segment's header (assembled once from segment 0;
                // only its entropy bytes join the stream — later segments
                // may legitimately differ in SOF height at the tail)
            }
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

// --------------------------------------------------------------------------- //
// minimal classic-TIFF writer (same shape as the other fixtures)
// --------------------------------------------------------------------------- //

struct IfdB {
    entries: Vec<(u16, u16, u64, Vec<u8>)>,
}

impl IfdB {
    fn new() -> Self {
        IfdB { entries: Vec::new() }
    }
    fn add(&mut self, tag: u16, typ: u16, count: u64, value: Vec<u8>) {
        self.entries.push((tag, typ, count, value));
    }
    fn add_short(&mut self, tag: u16, v: u16) {
        self.add(tag, 3, 1, v.to_le_bytes().to_vec());
    }
    fn add_long(&mut self, tag: u16, v: u32) {
        self.add(tag, 4, 1, v.to_le_bytes().to_vec());
    }
    fn add_slong(&mut self, tag: u16, v: i32) {
        self.add(tag, 9, 1, v.to_le_bytes().to_vec());
    }
    fn add_float(&mut self, tag: u16, v: f32) {
        self.add(tag, 11, 1, v.to_le_bytes().to_vec());
    }
    fn add_double(&mut self, tag: u16, v: f64) {
        self.add(tag, 12, 1, v.to_le_bytes().to_vec());
    }
    fn add_ascii(&mut self, tag: u16, s: &str) {
        let mut v = s.as_bytes().to_vec();
        v.push(0);
        let n = v.len() as u64;
        self.add(tag, 2, n, v);
    }
    fn add_bits3(&mut self) {
        self.add(258, 3, 3, [8u16, 8, 8].iter().flat_map(|v| v.to_le_bytes()).collect());
    }
    /// Serialize with external values appended; `ext_words` adds the NDPI
    /// per-entry extension area immediately after the next pointer (the
    /// layout OpenSlide reads: extensions at diroff + 12·count + 8), with
    /// the external values after it.
    fn serialize(&self, at: u64, next: u32, ext_words: bool) -> Vec<u8> {
        let mut sorted = self.entries.clone();
        sorted.sort_by_key(|e| e.0);
        let ifd_len = 2 + 12 * sorted.len() as u64 + 8;
        let ext_area = if ext_words { 4 * sorted.len() as u64 } else { 0 };
        let mut out = Vec::new();
        out.extend_from_slice(&(sorted.len() as u16).to_le_bytes());
        let mut ext_at = at + ifd_len + ext_area;
        let mut ext: Vec<Vec<u8>> = Vec::new();
        for (tag, typ, count, value) in &sorted {
            out.extend_from_slice(&tag.to_le_bytes());
            out.extend_from_slice(&typ.to_le_bytes());
            out.extend_from_slice(&(*count as u32).to_le_bytes());
            if value.len() <= 4 {
                let mut v = value.clone();
                v.resize(4, 0);
                out.extend_from_slice(&v);
            } else {
                out.extend_from_slice(&(ext_at as u32).to_le_bytes());
                let mut v = value.clone();
                if v.len() % 2 == 1 {
                    v.push(0);
                }
                ext_at += v.len() as u64;
                ext.push(v);
            }
        }
        out.extend_from_slice(&next.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes()); // 4 reserved bytes
        if ext_words {
            for _ in 0..sorted.len() {
                out.extend_from_slice(&0u32.to_le_bytes());
            }
        }
        for v in ext {
            out.extend_from_slice(&v);
        }
        out
    }
}

/// Level geometry: integer ÷4 per step while both sides stay ≥ 8 px (and
/// MCU-aligned, since the base dims are).
fn level_geometry(width: u32, height: u32, levels: u32) -> Vec<(u32, u32)> {
    let mut out = vec![(width, height)];
    while out.len() < levels as usize {
        let (w, h) = *out.last().unwrap();
        let nw = (w / 4).max(1);
        let nh = (h / 4).max(1);
        if nw < 16 || nh < 8 || (nw == w && nh == h) {
            break;
        }
        out.push((nw, nh));
    }
    out
}

/// Build the synthetic NDPI file; returns the file size. Assembled in memory
/// (fixtures are small) and written in one call.
pub fn build_synthetic_ndpi(out: &mut dyn RandomAccessSink, p: &NdpiGenParams) -> CoreResult<u64> {
    if !(16..=65536).contains(&p.width) || !(16..=65536).contains(&p.height) {
        return Err(CoreError::header("fixture: 宽/高越界"));
    }
    if p.levels == 0 || p.levels > 6 {
        return Err(CoreError::header("fixture: levels 越界"));
    }
    let levels = level_geometry(p.width, p.height, p.levels);
    let objectives: Vec<f32> = (0..levels.len())
        .map(|i| 20.0 / 4f32.powi(i as i32))
        .collect();

    // ---- strips ------------------------------------------------------------ //
    let mut strips: Vec<Vec<u8>> = Vec::new();
    for (li, &(w, h)) in levels.iter().enumerate() {
        if li == 0 && p.progressive {
            // bare progressive header (typed SOF2 rejection path)
            strips.push(vec![
                0xFF, 0xD8, 0xFF, 0xC2, 0x00, 0x11, 0x08, 0x00, 0x08, 0x01, 0x00, 0x03,
                0x01, 0x22, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01, 0xFF, 0xD9,
            ]);
            continue;
        }
        let seed = p.seed ^ (li as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
        strips.push(build_strip(w, h, seed, p)?);
    }
    let macro_jpg = if p.macro_page {
        Some(encode_rgb(
            &make_pixels(128, 48, p.seed ^ 0xABCD, p),
            128,
            48,
            &EncoderCfg::with_quality(85, Sampling::S422),
        )?)
    } else {
        None
    };
    let focus_jpg = if p.focus_map {
        Some(encode_rgb(
            &make_pixels(96, 64, p.seed ^ 0xEF01, p),
            96,
            64,
            &EncoderCfg::with_quality(85, Sampling::S422),
        )?)
    } else {
        None
    };
    const HDR_LEN: u64 = 16;
    let macro_at = HDR_LEN + strips.iter().map(|s| s.len() as u64).sum::<u64>();
    let focus_at = macro_at + macro_jpg.as_ref().map_or(0, |m| m.len() as u64);

    // ---- IFDs -------------------------------------------------------------- //
    let mut ifds: Vec<IfdB> = Vec::new();
    let strip_at = |li: usize| -> u32 {
        16 + strips[..li].iter().map(|s| s.len() as u64).sum::<u64>() as u32
    };
    for (li, &(w, h)) in levels.iter().enumerate() {
        let mut ifd = IfdB::new();
        ifd.add_long(256, w);
        ifd.add_long(257, h);
        ifd.add_bits3();
        ifd.add_short(259, p.compression);
        ifd.add_short(262, 6);
        ifd.add_ascii(271, &p.make);
        ifd.add_long(273, strip_at(li));
        ifd.add_short(277, 3);
        ifd.add_long(278, h);
        ifd.add_long(279, strips[li].len() as u32);
        ifd.add_short(284, 1);
        // NDPI_FORMAT_FLAG（65420）：OpenSlide/tifffile 用它检测 NDPI 模式
        //（每条目扩展字与 64 位首 IFD 偏移都以此为门）
        ifd.add_short(65420, 1);
        if p.extra_samples {
            // one extra sample (alpha-ish): the fluorescence-ish refusal
            ifd.add_short(277, 4);
            ifd.add_short(338, 2);
        }
        ifd.add_float(65421, objectives[li]);
        ifd.add_slong(65424, 0);
        if let Some(mpp) = p.mpp {
            ifd.add_double(65441, mpp);
            ifd.add_double(65442, mpp);
        }
        ifds.push(ifd);
    }
    if let Some(m) = &macro_jpg {
        let mut ifd = IfdB::new();
        ifd.add_long(256, 128);
        ifd.add_long(257, 48);
        ifd.add_bits3();
        ifd.add_short(259, 7);
        ifd.add_short(262, 6);
        ifd.add_ascii(271, &p.make);
        ifd.add_long(273, macro_at as u32);
        ifd.add_short(277, 3);
        ifd.add_long(278, 48);
        ifd.add_long(279, m.len() as u32);
        ifd.add_short(284, 1);
        ifd.add_float(65421, -1.0);
        ifds.push(ifd);
    }
    if let Some(f) = &focus_jpg {
        let mut ifd = IfdB::new();
        ifd.add_long(256, 96);
        ifd.add_long(257, 64);
        ifd.add_bits3();
        ifd.add_short(259, 7);
        ifd.add_short(262, 6);
        ifd.add_ascii(271, &p.make);
        ifd.add_long(273, focus_at as u32);
        ifd.add_short(277, 3);
        ifd.add_long(278, 64);
        ifd.add_long(279, f.len() as u32);
        ifd.add_short(284, 1);
        ifd.add_float(65421, -2.0);
        ifds.push(ifd);
    }

    // ---- assemble: header | payloads | IFD chain (with extension areas) ---- //
    // Real NDPI headers carry the first-IFD offset as a 64-bit value
    // (tifffile reads NDPI classic files with offsetsize 8): the header is
    // 16 bytes — u32 magic, u64 first-IFD offset (patched below), 4 bytes
    // reserved zero — and the payload area starts at 16.
    let mut buf: Vec<u8> = Vec::new();
    buf.extend_from_slice(b"II*\0");
    buf.extend_from_slice(&0u64.to_le_bytes()); // first IFD, patched below
    buf.extend_from_slice(&0u32.to_le_bytes());
    for s in &strips {
        buf.extend_from_slice(s);
    }
    if let Some(m) = &macro_jpg {
        buf.extend_from_slice(m);
    }
    if let Some(f) = &focus_jpg {
        buf.extend_from_slice(f);
    }

    let mut ifd_offsets: Vec<u32> = Vec::with_capacity(ifds.len());
    let mut at = buf.len() as u64;
    for ifd in &ifds {
        ifd_offsets.push(at as u32);
        let body = ifd.serialize(at, 0, p.ndpi_extensions);
        at += body.len() as u64;
        buf.extend_from_slice(&body);
    }
    for (i, off) in ifd_offsets.iter().enumerate() {
        let next = ifd_offsets.get(i + 1).copied().unwrap_or(0);
        let at = *off as usize + 2 + 12 * ifds[i].entries.len();
        buf[at..at + 4].copy_from_slice(&next.to_le_bytes());
    }
    let _ = &ifds;
    buf[4..12].copy_from_slice(&(ifd_offsets[0] as u64).to_le_bytes());

    out.write_at(0, &buf)?;
    out.flush()?;
    Ok(buf.len() as u64)
}
