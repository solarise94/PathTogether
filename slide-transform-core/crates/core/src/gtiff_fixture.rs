//! Synthetic generic-TIFF fixture generator (F5 tests; `fixtures` feature).
//! Everything is generated in code — no sample bytes are committed. Layout
//! mirrors what a vips/tiffcp-style converter writes (the OpenSlide
//! `generic-tiff` family):
//!
//! ```text
//! header (classic II*\0 / MM\0*, or BigTIFF II+\0 / MM\0+)
//! … tile payloads of every level (complete JPEG streams; optional shared
//!   JPEGTables) … ICC …
//! IFD chain: one IFD per pyramid level (tiled, baseline JPEG, no vendor
//!   description; optional XResolution/YResolution in px/cm)
//! ```
//!
//! Fault/variant knobs (each exercises one typed「暂时直传」refusal):
//! `stripped` (level 0 stored as strips), `deflate` / `lzw` (non-JPEG
//! compression with junk payloads), `gray` (1 sample), `bits16`, `planar2`,
//! `tile_mismatch` (levels disagree on the tile geometry), `desc_mode`
//! (none | ome | converter | aperio | scn | foreign — the routing-level
//! rejections), `shared_tables` (tag 347 per level).

use crate::error::{CoreError, CoreResult};
use crate::io::RandomAccessSink;
use crate::jpeg::{encode_rgb, EncoderCfg, Sampling};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DescMode {
    /// No description tag at all (the generic-TIFF default; vips writes none).
    None,
    /// OME-XML description (OME-TIFF is not a conversion input).
    Ome,
    /// This converter's own output JSON (not a conversion input).
    Converter,
    /// Aperio description (routes to the SVS adapter, not this one).
    Aperio,
    /// Leica SCN XML (routes to the SCN adapter, not this one).
    ScnXml,
    /// A foreign scanner's plain description (unconvertible by contract).
    Foreign,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixtureColor {
    /// RGB payloads (4:4:4, TIFF photometric 2).
    Rgb,
    /// YCbCr payloads (4:2:2, TIFF photometric 6 + YCbCrSubSampling (2,1) —
    /// the OpenSlide generic-tiff sample layout).
    YCbCr,
}

/// Tile pixel content. `Noise` is the general-purpose fixture texture;
/// `Gradient` is locally linear ((x+y)&0xFF per channel) so the 4:2:2/DCT
/// round-trip error of a re-encode stays tiny — the generated-level pixel
/// assertions use it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixturePattern {
    Noise,
    Gradient,
}

#[derive(Debug, Clone)]
pub struct GtiffGenParams {
    pub width: u32,
    pub height: u32,
    /// Nominal tile size (the sample family: 256).
    pub tile: u32,
    /// Tile height override (non-square 128×256 geometry; None = square).
    pub tile_h: Option<u32>,
    /// Source pyramid level count (each step ÷2, ≥ 8 px).
    pub levels: u32,
    pub bigtiff: bool,
    pub big_endian: bool,
    pub color: FixtureColor,
    /// IFD 0 description override.
    pub desc_mode: DescMode,
    /// Level 0 stored as strips (273/279) instead of tiles.
    pub stripped: bool,
    /// Deflate-marked (259=8) levels with junk payloads.
    pub deflate: bool,
    /// LZW-marked (259=5) levels with junk payloads.
    pub lzw: bool,
    /// Single-sample (gray) variant.
    pub gray: bool,
    /// 16-bit variant.
    pub bits16: bool,
    /// PlanarConfiguration=2 variant.
    pub planar2: bool,
    /// Level 1.. use tile/2 (min 16) — the shared-geometry refusal.
    pub tile_mismatch: bool,
    /// Shared JPEGTables (tag 347) per level.
    pub shared_tables: bool,
    /// XResolution/YResolution in px/cm (296=3); None = tags absent.
    pub xres: Option<f64>,
    /// Attach a small synthetic ICC profile to IFD 0.
    pub icc: bool,
    /// Tile pixel content (Noise default; Gradient for re-encode tests).
    pub pattern: FixturePattern,
    pub quality: u8,
    pub seed: u64,
}

impl Default for GtiffGenParams {
    fn default() -> Self {
        GtiffGenParams {
            width: 520,
            height: 300,
            tile: 128,
            tile_h: None,
            levels: 3,
            bigtiff: false,
            big_endian: false,
            color: FixtureColor::YCbCr,
            desc_mode: DescMode::None,
            stripped: false,
            deflate: false,
            lzw: false,
            gray: false,
            bits16: false,
            planar2: false,
            tile_mismatch: false,
            shared_tables: false,
            xres: Some(10.0),
            icc: false,
            pattern: FixturePattern::Noise,
            quality: 85,
            seed: 0,
        }
    }
}

struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545F4914F6CDD1D)
    }
    fn byte(&mut self) -> u8 {
        (self.next_u64() >> 33) as u8
    }
}

fn encode_tile(
    w: u32,
    h: u32,
    seed: u64,
    quality: u8,
    color: FixtureColor,
    pattern: FixturePattern,
) -> CoreResult<Vec<u8>> {
    let sf = match color {
        FixtureColor::Rgb => Sampling::S444,
        FixtureColor::YCbCr => Sampling::S422,
    };
    match pattern {
        FixturePattern::Gradient => {
            // locally linear per channel (a gentle diagonal ramp, no wrap or
            // saturation inside a level): a box downsample of it stays
            // near-exact, so a re-encode's error is the DCT/4:2:2 floor only
            let b = (seed.wrapping_mul(0x9E37_79B9) >> 56) as i32 % 7;
            let mut px = vec![0u8; (w as usize) * (h as usize) * 3];
            for y in 0..h as usize {
                for x in 0..w as usize {
                    let v = 100 + ((x as i32 - y as i32) / 8).clamp(-90, 90) + b;
                    let o = (y * w as usize + x) * 3;
                    px[o] = v as u8;
                    px[o + 1] = (v + 17) as u8;
                    px[o + 2] = (v + 43) as u8;
                }
            }
            encode_rgb(&px, w, h, &EncoderCfg::with_quality(quality, sf))
        }
        FixturePattern::Noise => {
            let mut rng = Rng(seed | 1);
            let mut px = vec![0u8; (w as usize) * (h as usize) * 3];
            for c in px.chunks_exact_mut(3) {
                c[0] = rng.byte();
                c[1] = rng.byte();
                c[2] = rng.byte();
            }
            encode_rgb(&px, w, h, &EncoderCfg::with_quality(quality, sf))
        }
    }
}

/// Level geometry: integer ÷2 per step while the level stays ≥ 8 px.
fn level_geometry(width: u32, height: u32, levels: u32) -> Vec<(u32, u32)> {
    let mut out = vec![(width, height)];
    while out.len() < levels as usize {
        let (w, h) = *out.last().unwrap();
        let nw = (w / 2).max(1);
        let nh = (h / 2).max(1);
        if nw < 8 || nh < 8 || (nw == w && nh == h) {
            break;
        }
        out.push((nw, nh));
    }
    out
}

// --------------------------------------------------------------------------- //
// minimal classic/BigTIFF writer for the fixture (file-order layouts only;
// same shape as the SVS fixture's)
// --------------------------------------------------------------------------- //

#[derive(Clone, Copy)]
struct W {
    bigtiff: bool,
    little: bool,
}

impl W {
    fn p16(&self, v: u16) -> [u8; 2] {
        if self.little { v.to_le_bytes() } else { v.to_be_bytes() }
    }
    fn p32(&self, v: u32) -> [u8; 4] {
        if self.little { v.to_le_bytes() } else { v.to_be_bytes() }
    }
    fn p64(&self, v: u64) -> [u8; 8] {
        if self.little { v.to_le_bytes() } else { v.to_be_bytes() }
    }
}

/// One IFD under construction; entries are (tag, type, count, value bytes).
struct IfdB {
    w: W,
    entries: Vec<(u16, u16, u64, Vec<u8>)>,
}

impl IfdB {
    fn new(w: W) -> Self {
        IfdB { w, entries: Vec::new() }
    }
    fn add(&mut self, tag: u16, typ: u16, count: u64, value: Vec<u8>) {
        self.entries.push((tag, typ, count, value));
    }
    fn add_short(&mut self, tag: u16, v: u16) {
        let b = self.w.p16(v);
        self.add(tag, 3, 1, b.to_vec());
    }
    fn add_long(&mut self, tag: u16, v: u32) {
        let b = self.w.p32(v);
        self.add(tag, 4, 1, b.to_vec());
    }
    fn serialize(&self, at: u64, next: Option<u64>) -> Vec<u8> {
        let w = &self.w;
        let mut sorted = self.entries.clone();
        sorted.sort_by_key(|e| e.0);
        let esize: u64 = if w.bigtiff { 20 } else { 12 };
        let count_len: u64 = if w.bigtiff { 8 } else { 2 };
        let next_len: u64 = if w.bigtiff { 8 } else { 4 };
        let ifd_len = count_len + esize * sorted.len() as u64 + next_len;
        let inline_cap: u64 = if w.bigtiff { 8 } else { 4 };
        let mut out = Vec::new();
        if w.bigtiff {
            out.extend_from_slice(&w.p64(sorted.len() as u64));
        } else {
            out.extend_from_slice(&w.p16(sorted.len() as u16));
        }
        let mut ext_at = at + ifd_len;
        let mut ext: Vec<Vec<u8>> = Vec::new();
        for (tag, typ, count, value) in &sorted {
            out.extend_from_slice(&w.p16(*tag));
            out.extend_from_slice(&w.p16(*typ));
            if w.bigtiff {
                out.extend_from_slice(&w.p64(*count));
            } else {
                out.extend_from_slice(&w.p32(*count as u32));
            }
            if (value.len() as u64) <= inline_cap {
                let mut v = value.clone();
                v.resize(inline_cap as usize, 0);
                out.extend_from_slice(&v);
            } else {
                if w.bigtiff {
                    out.extend_from_slice(&w.p64(ext_at));
                } else {
                    out.extend_from_slice(&w.p32(ext_at as u32));
                }
                let mut v = value.clone();
                if v.len() % 2 == 1 {
                    v.push(0);
                }
                ext_at += v.len() as u64;
                ext.push(v);
            }
        }
        match next {
            Some(n) if w.bigtiff => out.extend_from_slice(&w.p64(n)),
            Some(n) => out.extend_from_slice(&w.p32(n as u32)),
            None if w.bigtiff => out.extend_from_slice(&w.p64(0)),
            None => out.extend_from_slice(&w.p32(0)),
        }
        for v in ext {
            out.extend_from_slice(&v);
        }
        out
    }
}

/// Split one complete JPEG (core encoder output) into (tables, tile) — the
/// abbreviated pair every tiled-JPEG reader assembles (`JPEGTables` + the
/// SOF/SOS-bearing tile stream).
fn split_tables(jpeg: &[u8]) -> CoreResult<(Vec<u8>, Vec<u8>)> {
    let mut tables = Vec::new();
    let mut tile: Vec<u8> = vec![0xFF, 0xD8];
    let mut i = 2usize; // past SOI
    while i + 4 <= jpeg.len() {
        if jpeg[i] != 0xFF {
            return Err(CoreError::validation("fixture: 标记流错位"));
        }
        let m = jpeg[i + 1];
        if m == 0xD9 {
            break;
        }
        let ln = ((jpeg[i + 2] as usize) << 8) | jpeg[i + 3] as usize;
        if i + 2 + ln > jpeg.len() {
            return Err(CoreError::validation("fixture: 段长越界"));
        }
        match m {
            0xDB | 0xC4 => tables.extend_from_slice(&jpeg[i..i + 2 + ln]),
            0xDA => {
                tile.extend_from_slice(&jpeg[i..]);
                break;
            }
            _ => tile.extend_from_slice(&jpeg[i..i + 2 + ln]),
        }
        i += 2 + ln;
    }
    let mut tables = {
        tables.insert(0, 0xD8);
        tables.insert(0, 0xFF);
        tables
    };
    tables.push(0xFF);
    tables.push(0xD9);
    Ok((tables, tile))
}

fn desc_bytes(mode: DescMode, p: &GtiffGenParams) -> Vec<u8> {
    let s = match mode {
        DescMode::None => return Vec::new(),
        DescMode::Ome => "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
<OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\"></OME>"
            .to_string(),
        DescMode::Converter => format!(
            "{{\"adapter\": \"{}\", \"adapter_version\": \"1\", \"mpp_x\": null, \
             \"mpp_y\": null, \"objective\": null, \"source_format\": \"{}\"}}",
            crate::gtiff::SOURCE_FORMAT,
            crate::gtiff::SOURCE_FORMAT
        ),
        DescMode::Aperio => format!(
            "Aperio Image Library v11.2.1 \r\n{}x{} ({}x{}) JPEG/RGB Q=30|MPP = 0.4990",
            p.width, p.height, p.tile, p.tile
        ),
        DescMode::ScnXml => format!(
            "<?xml version=\"1.0\"?><scn xmlns=\"http://www.leica-microsystems.com/scn/2010/10/01\">\
<collection><image><pixels sizeX=\"{w}\" sizeY=\"{h}\">\
<dimension sizeX=\"{w}\" sizeY=\"{h}\" r=\"0\" ifd=\"0\" /></pixels>\
<view sizeX=\"{vw}\" sizeY=\"{vh}\" /><scanSettings><illuminationSettings>\
<illuminationSource>brightfield</illuminationSource></illuminationSettings>\
</scanSettings></image></collection></scn>",
            w = p.width,
            h = p.height,
            vw = p.width * 500,
            vh = p.height * 500,
        ),
        DescMode::Foreign => "Some Other Scanner v1".to_string(),
    };
    let mut d = s.into_bytes();
    d.push(0);
    d
}

/// Build the synthetic generic-TIFF file; returns the file size. Assembled
/// in memory (fixtures are small) and written in one call.
pub fn build_synthetic_gtiff(out: &mut dyn RandomAccessSink, p: &GtiffGenParams) -> CoreResult<u64> {
    if !(1..=100_000).contains(&p.width) || !(1..=100_000).contains(&p.height) {
        return Err(CoreError::header("fixture: 宽/高越界"));
    }
    if !(16..=8192).contains(&p.tile) {
        return Err(CoreError::header("fixture: tile 越界"));
    }
    let w = W { bigtiff: p.bigtiff, little: !p.big_endian };
    let levels = level_geometry(p.width, p.height, p.levels);
    let photo: u16 = match p.color {
        FixtureColor::Rgb => 2,
        FixtureColor::YCbCr => 6,
    };

    // ---- encode every level's tiles ------------------------------------- //
    let mut tables_of: Vec<Vec<u8>> = Vec::new();
    let mut tiles_of: Vec<Vec<Vec<u8>>> = Vec::new();
    let tile_dims = |li: usize| -> (u32, u32) {
        let tw = if p.tile_mismatch && li > 0 { (p.tile / 2).max(16) } else { p.tile };
        let th = if p.tile_mismatch && li > 0 {
            (p.tile_h.unwrap_or(p.tile) / 2).max(16)
        } else {
            p.tile_h.unwrap_or(p.tile)
        };
        (tw, th)
    };
    for (li, &(lw, lh)) in levels.iter().enumerate() {
        let (tile, tile_h) = tile_dims(li);
        let across = (lw + tile - 1) / tile;
        let down = (lh + tile_h - 1) / tile_h;
        let mut lvl_tables: Option<Vec<u8>> = None;
        let mut lvl_tiles = Vec::new();
        for row in 0..down {
            for col in 0..across {
                let tw = tile.min(lw - col * tile);
                let th = tile_h.min(lh - row * tile_h);
                let seed = p
                    .seed
                    ^ (li as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15)
                    ^ (row as u64).wrapping_mul(0x100000001B3)
                    ^ (col as u64).wrapping_mul(7);
                let full = encode_tile(tw, th, seed, p.quality, p.color, p.pattern)?;
                if p.shared_tables {
                    let (tables, tile_data) = split_tables(&full)?;
                    match &lvl_tables {
                        None => lvl_tables = Some(tables),
                        Some(t0) => assert_eq!(t0, &tables, "fixture: 表应一致"),
                    }
                    lvl_tiles.push(tile_data);
                } else {
                    lvl_tiles.push(full);
                }
            }
        }
        tables_of.push(lvl_tables.unwrap_or_default());
        tiles_of.push(lvl_tiles);
    }

    let icc: Vec<u8> = if p.icc {
        let mut v = b"PTGFIXICC-PROFILE!".to_vec();
        v.extend_from_slice(&[7u8; 16]);
        v
    } else {
        Vec::new()
    };

    // ---- file assembly --------------------------------------------------- //
    let hdr_len: u64 = if p.bigtiff { 16 } else { 8 };
    let mut buf: Vec<u8> = Vec::new();
    buf.resize(hdr_len as usize, 0);

    // payloads: per-level tiles are stored at their natural byte size even
    // for the variant knobs (the probe rejects at the metadata, not payload)
    let mut lvl_offsets: Vec<Vec<u64>> = Vec::new();
    let mut lvl_counts: Vec<Vec<u64>> = Vec::new();
    for tiles in &tiles_of {
        let mut offs = Vec::new();
        let mut cnts = Vec::new();
        for t in tiles {
            let at = buf.len() as u64;
            buf.extend_from_slice(t);
            offs.push(at);
            cnts.push(t.len() as u64);
        }
        lvl_offsets.push(offs);
        lvl_counts.push(cnts);
    }
    let icc_at: u64 = if p.icc {
        let at = buf.len() as u64;
        buf.extend_from_slice(&icc);
        at
    } else {
        0
    };

    let mut bodies: Vec<IfdB> = Vec::new();
    for (li, &(lw, lh)) in levels.iter().enumerate() {
        let mut b = IfdB::new(w);
        b.add_long(256, lw);
        b.add_long(257, lh);
        if p.gray {
            b.add_short(258, 8);
            b.add_short(259, if p.deflate { 8 } else if p.lzw { 5 } else { 7 });
            b.add_short(262, 1);
        } else {
            let bits: Vec<u8> = if p.bits16 {
                [16u16, 16, 16].iter().flat_map(|v| w.p16(*v)).collect()
            } else {
                [8u16, 8, 8].iter().flat_map(|v| w.p16(*v)).collect()
            };
            b.add(258, 3, 3, bits);
            b.add_short(259, if p.deflate { 8 } else if p.lzw { 5 } else { 7 });
            b.add_short(262, photo);
        }
        let desc = if li == 0 { desc_bytes(p.desc_mode, p) } else { Vec::new() };
        if !desc.is_empty() {
            b.add(270, 2, desc.len() as u64, desc);
        }
        b.add_short(277, if p.gray { 1 } else { 3 });
        b.add_short(284, if p.planar2 { 2 } else { 1 });
        if li == 0 && p.stripped {
            // one strip covering the full image height
            let off: Vec<u8> = if p.bigtiff {
                w.p64(lvl_offsets[0][0]).to_vec()
            } else {
                w.p32(lvl_offsets[0][0] as u32).to_vec()
            };
            let cnt: Vec<u8> = if p.bigtiff {
                w.p64(lvl_counts[0][0]).to_vec()
            } else {
                w.p32(lvl_counts[0][0] as u32).to_vec()
            };
            b.add_long(278, lh);
            b.add(273, if p.bigtiff { 16 } else { 4 }, 1, off);
            b.add(279, if p.bigtiff { 16 } else { 4 }, 1, cnt);
        } else {
            let (tile, tile_h) = tile_dims(li);
            b.add_short(322, tile as u16);
            b.add_short(323, tile_h as u16);
            let offs = &lvl_offsets[li];
            let cnts = &lvl_counts[li];
            let (typ, ob, cb): (u16, Vec<u8>, Vec<u8>) = if p.bigtiff {
                (
                    16,
                    offs.iter().flat_map(|o| w.p64(*o)).collect(),
                    cnts.iter().flat_map(|c| w.p64(*c)).collect(),
                )
            } else {
                (
                    4,
                    offs.iter().flat_map(|o| w.p32(*o as u32)).collect(),
                    cnts.iter().flat_map(|c| w.p32(*c as u32)).collect(),
                )
            };
            b.add(324, typ, offs.len() as u64, ob);
            b.add(325, typ, cnts.len() as u64, cb);
        }
        if !tables_of[li].is_empty() {
            b.add(347, 7, tables_of[li].len() as u64, tables_of[li].clone());
        }
        if li == 0 {
            if let Some(xres) = p.xres {
                // RATIONAL num/den px/cm with ResolutionUnit=3
                let den = 1_000_000u32;
                let num = (xres * den as f64).round() as u32;
                let mut r = Vec::new();
                r.extend_from_slice(&w.p32(num));
                r.extend_from_slice(&w.p32(den));
                b.add(282, 5, 1, r.clone());
                b.add(283, 5, 1, r);
                b.add_short(296, 3);
            }
            if p.icc {
                b.add(34675, 7, icc.len() as u64, {
                    let _ = icc_at;
                    icc.clone()
                });
            }
        }
        bodies.push(b);
    }

    // two passes: first compute each IFD's serialized length with next=0,
    // then rewrite with the real chain offsets
    let mut ats: Vec<u64> = Vec::new();
    {
        let mut cursor = buf.len() as u64;
        for b in &bodies {
            ats.push(cursor);
            let bytes = b.serialize(cursor, None);
            cursor += bytes.len() as u64;
        }
    }
    for (i, b) in bodies.iter().enumerate() {
        let next = if i + 1 < bodies.len() { Some(ats[i + 1]) } else { None };
        let bytes = b.serialize(ats[i], next);
        buf.extend_from_slice(&bytes);
    }

    // ---- header ---------------------------------------------------------- //
    let first_ifd = ats.first().copied().unwrap_or(0);
    if p.bigtiff {
        if w.little {
            buf[0..2].copy_from_slice(b"II");
        } else {
            buf[0..2].copy_from_slice(b"MM");
        }
        buf[2..4].copy_from_slice(&w.p16(43));
        buf[4..6].copy_from_slice(&w.p16(8));
        buf[6..8].copy_from_slice(&w.p16(0));
        buf[8..16].copy_from_slice(&w.p64(first_ifd));
    } else {
        if w.little {
            buf[0..2].copy_from_slice(b"II");
        } else {
            buf[0..2].copy_from_slice(b"MM");
        }
        buf[2..4].copy_from_slice(&w.p16(42));
        buf[4..8].copy_from_slice(&w.p32(first_ifd as u32));
    }

    out.write_at(0, &buf)?;
    out.flush()?;
    Ok(buf.len() as u64)
}
