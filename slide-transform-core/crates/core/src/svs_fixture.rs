//! Synthetic Aperio-style SVS fixture generator (F1 tests; `fixtures`
//! feature). Everything is generated in code — no sample bytes are
//! committed. Layout mirrors the real Aperio structure observed on the CC0
//! OpenSlide samples:
//!
//! ```text
//! header (classic II*\0 or II+\0; MM variants via big_endian)
//! … tile payloads of every level (abbreviated JPEG streams; the DQT/DHT
//!   segments live in the per-level JPEGTables tag) … associated payloads
//! IFD chain, each IFD followed by its external values (description,
//! JPEGTables, TileOffsets/TileByteCounts):
//!   main (tiled; description "Aperio Image Library v…" with |MPP|AppMag),
//!   thumbnail (stripped), level 1.. (tiled, ~4× steps), label, macro
//! ```
//!
//! Tiles are produced with the crate's own encoder and then split into the
//! Aperio abbreviated form. For [`FixtureColor::Rgb`] the SOF component ids
//! are rewritten to Aperio's 0,1,2 and the JFIF APP0 is dropped — exactly
//! the ambiguous stream whose true colorspace only the TIFF photometric
//! tag disambiguates (see `svs.rs` / `jpeg::tiff_jpeg_color`).

use crate::error::{CoreError, CoreResult};
use crate::io::RandomAccessSink;
use crate::jpeg::{encode_rgb, EncoderCfg, Sampling};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixtureColor {
    /// Aperio `JPEG/RGB`: no JFIF, component ids 0,1,2, 4:4:4, TIFF
    /// photometric 2 (the CMU layout).
    Rgb,
    /// YCbCr payloads: component ids 1,2,3, 4:2:2, TIFF photometric 6 with
    /// matching YCbCrSubSampling.
    YCbCr,
}

#[derive(Debug, Clone)]
pub struct SvsGenParams {
    pub width: u32,
    pub height: u32,
    /// Nominal tile size (Aperio: 240 or 256).
    pub tile: u32,
    pub bigtiff: bool,
    pub big_endian: bool,
    /// Reduction factor per level step (Aperio: 4).
    pub downsample: u32,
    /// Include thumbnail/label/macro pages.
    pub include_associated: bool,
    /// Description `MPP = …` (None = token omitted entirely).
    pub mpp: Option<f64>,
    /// Description `AppMag = …` (None = token omitted).
    pub appmag: Option<f64>,
    /// Attach a small synthetic ICC profile to the main IFD.
    pub icc: bool,
    /// Crop the last-column/last-row tiles to the image edge instead of the
    /// Aperio full-tile style (exercises the cropped-edge passthrough path).
    pub crop_tail_tiles: bool,
    pub color: FixtureColor,
    pub quality: u8,
    pub seed: u64,
}

impl Default for SvsGenParams {
    fn default() -> Self {
        SvsGenParams {
            width: 580,
            height: 300,
            tile: 256,
            bigtiff: false,
            big_endian: false,
            downsample: 4,
            include_associated: false,
            mpp: Some(0.4990),
            appmag: Some(20.0),
            icc: false,
            crop_tail_tiles: false,
            color: FixtureColor::Rgb,
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

fn encode_tile(w: u32, h: u32, seed: u64, quality: u8, color: FixtureColor) -> CoreResult<Vec<u8>> {
    let mut rng = Rng(seed | 1);
    let mut px = vec![0u8; (w as usize) * (h as usize) * 3];
    for c in px.chunks_exact_mut(3) {
        c[0] = rng.byte();
        c[1] = rng.byte();
        c[2] = rng.byte();
    }
    let sf = match color {
        FixtureColor::Rgb => Sampling::S444,
        FixtureColor::YCbCr => Sampling::S422,
    };
    encode_rgb(&px, w, h, &EncoderCfg::with_quality(quality, sf))
}

/// Split one complete JPEG (core encoder output) into the Aperio abbreviated
/// pair (JPEGTables stream, tile stream). `ids` rewrites the SOF component
/// ids and the SOS selectors; APPn markers are dropped from the tile.
fn split_aperio(jpeg: &[u8], ids: [u8; 3]) -> CoreResult<(Vec<u8>, Vec<u8>)> {
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
            0xC0 | 0xC1 => {
                // segment layout: FF C0 len(2) prec h(2) w(2) nc comps(3×nc)
                let mut sof = jpeg[i..i + 2 + ln].to_vec();
                let nc = jpeg[i + 9] as usize;
                for c in 0..nc {
                    sof[i + 10 + c * 3 - i] = ids[c.min(2)];
                }
                // absolute: marker(1)+len(2)+prec(1)+h(2)+w(2)+nc(1) = 9 from i
                for c in 0..nc {
                    sof[10 + c * 3] = ids[c.min(2)];
                }
                tile.extend_from_slice(&sof);
            }
            0xDA => {
                let mut sos = jpeg[i..i + 2 + ln].to_vec();
                // FF DA len(2) ns selectors(2×ns) …
                let ns = jpeg[i + 4] as usize;
                for c in 0..ns {
                    sos[5 + c * 2] = ids[c.min(2)];
                }
                tile.extend_from_slice(&sos);
                tile.extend_from_slice(&jpeg[i + 2 + ln..]);
                break;
            }
            _ => {} // APP0/COM dropped
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

/// Level geometry: integer reduction by `down` per step while the level
/// stays ≥ 8 px (Aperio keeps a handful of 4× levels).
fn level_geometry(width: u32, height: u32, down: u32, tile: u32) -> Vec<(u32, u32)> {
    let down = down.max(2);
    let mut out = vec![(width, height)];
    while out.len() < 8 {
        let (w, h) = *out.last().unwrap();
        let nw = (w + down - 1) / down;
        let nh = (h + down - 1) / down;
        if nw < 8 || nh < 8 || (nw == w && nh == h) {
            break;
        }
        out.push((nw, nh));
    }
    let _ = tile;
    out
}

// --------------------------------------------------------------------------- //
// minimal classic/BigTIFF writer for the fixture (file-order layouts only)
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
/// Values longer than the inline capacity (4 classic / 8 BigTIFF) are
/// automatically written as externals directly after the IFD.
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
    /// Serialize the IFD placed at `at`; externals follow it. `next` is the
    /// offset of the following IFD in the chain (None = 0).
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

/// Build the synthetic SVS file; returns the file size. The file is
/// assembled in memory (fixtures are small) and written in one call.
pub fn build_synthetic_svs(out: &mut dyn RandomAccessSink, p: &SvsGenParams) -> CoreResult<u64> {
    if !(1..=100_000).contains(&p.width) || !(1..=100_000).contains(&p.height) {
        return Err(CoreError::header("fixture: 宽/高越界"));
    }
    if !(16..=8192).contains(&p.tile) {
        return Err(CoreError::header("fixture: tile 越界"));
    }
    let ids: [u8; 3] = match p.color {
        FixtureColor::Rgb => [0, 1, 2],
        FixtureColor::YCbCr => [1, 2, 3],
    };
    let photo: u16 = match p.color {
        FixtureColor::Rgb => 2,
        FixtureColor::YCbCr => 6,
    };
    let sampling: (u16, u16) = match p.color {
        FixtureColor::Rgb => (1, 1),
        FixtureColor::YCbCr => (2, 1),
    };
    let w = W { bigtiff: p.bigtiff, little: !p.big_endian };
    let levels = level_geometry(p.width, p.height, p.downsample, p.tile);

    // ---- encode every level's tiles (shared tables per level) ----------- //
    let mut tables_of: Vec<Vec<u8>> = Vec::new();
    let mut tiles_of: Vec<Vec<Vec<u8>>> = Vec::new();
    for (li, &(lw, lh)) in levels.iter().enumerate() {
        let across = (lw + p.tile - 1) / p.tile;
        let down = (lh + p.tile - 1) / p.tile;
        let mut lvl_tables: Option<Vec<u8>> = None;
        let mut lvl_tiles = Vec::new();
        for row in 0..down {
            for col in 0..across {
                let tw = if p.crop_tail_tiles && col + 1 == across {
                    (lw - col * p.tile).min(p.tile)
                } else {
                    p.tile
                };
                let th = if p.crop_tail_tiles && row + 1 == down {
                    (lh - row * p.tile).min(p.tile)
                } else {
                    p.tile
                };
                let seed = p
                    .seed
                    ^ (li as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15)
                    ^ (row as u64).wrapping_mul(0x100000001B3)
                    ^ (col as u64).wrapping_mul(7);
                let full = encode_tile(tw, th, seed, p.quality, p.color)?;
                let (tables, tile) = split_aperio(&full, ids)?;
                match &lvl_tables {
                    None => lvl_tables = Some(tables),
                    Some(t0) => assert_eq!(t0, &tables, "fixture: 表应一致"),
                }
                lvl_tiles.push(tile);
            }
        }
        tables_of.push(lvl_tables.unwrap_or_default());
        tiles_of.push(lvl_tiles);
    }

    // associated payloads (complete JPEG streams; single strip each)
    let mut assoc: Vec<(&'static str, u32, u32, u64, String, Vec<u8>)> = Vec::new();
    if p.include_associated {
        assoc.push((
            "thumbnail",
            128,
            96,
            0,
            format!("Aperio Image Library v11.2.1 \r\n{}x{} -> 128x96 - ", p.width, p.height),
            encode_tile(128, 96, 0xAA, p.quality, p.color)?,
        ));
        assoc.push((
            "label",
            64,
            64,
            1,
            "Aperio Image Library v11.2.1 \r\nlabel 64x64".to_string(),
            encode_tile(64, 64, 0xBB, p.quality, p.color)?,
        ));
        assoc.push((
            "macro",
            160,
            48,
            9,
            "Aperio Image Library v11.2.1 \r\nmacro 160x431".to_string(),
            encode_tile(160, 48, 0xCC, p.quality, p.color)?,
        ));
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
    let assoc_offsets: Vec<u64> = assoc
        .iter()
        .map(|(_, _, _, _, _, payload)| {
            let at = buf.len() as u64;
            buf.extend_from_slice(payload);
            at
        })
        .collect();
    let icc_at: u64 = if p.icc {
        let at = buf.len() as u64;
        buf.extend_from_slice(&icc);
        at
    } else {
        0
    };

    let mut desc_main = format!(
        "Aperio Image Library v11.2.1 \r\n46920x33014 [0,100 {}x{}] ({}x{}) JPEG/{} Q=30|StripeWidth = 2040|ScanScope ID = PTGFIX|Filename = FIXTURE|Date = 12/29/09",
        p.width,
        p.height,
        p.tile,
        p.tile,
        match p.color {
            FixtureColor::Rgb => "RGB",
            FixtureColor::YCbCr => "YCbCr",
        }
    );
    if let Some(m) = p.mpp {
        desc_main.push_str(&format!("|MPP = {m:.4}"));
    }
    if let Some(a) = p.appmag {
        desc_main.push_str(&format!("|AppMag = {a}"));
    }
    let desc_level = "Aperio Image Library v10.0.51\r".to_string();

    // IFD order (CMU): main, thumbnail, levels 1.., label, macro
    enum Page {
        Level(usize),
        Assoc(usize),
    }
    let mut pages: Vec<Page> = vec![Page::Level(0)];
    if p.include_associated {
        pages.push(Page::Assoc(0));
    }
    for li in 1..levels.len() {
        pages.push(Page::Level(li));
    }
    if p.include_associated {
        pages.push(Page::Assoc(1));
        pages.push(Page::Assoc(2));
    }

    let mut bodies: Vec<IfdB> = Vec::new();
    for page in pages.iter() {
        let mut b = IfdB::new(w);
        match page {
            Page::Level(li) => {
                let (lw, lh) = levels[*li];
                b.add_long(256, lw);
                b.add_long(257, lh);
                let bits: Vec<u8> = [8u16, 8, 8].iter().flat_map(|v| w.p16(*v)).collect();
                b.add(258, 3, 3, bits);
                b.add_short(259, 7);
                b.add_short(262, photo);
                let desc = if *li == 0 { desc_main.clone() } else { desc_level.clone() };
                b.add(270, 2, desc.len() as u64 + 1, {
                    let mut d = desc.into_bytes();
                    d.push(0);
                    d
                });
                b.add_short(277, 3);
                b.add_short(284, 1);
                b.add_short(322, p.tile as u16);
                b.add_short(323, p.tile as u16);
                let offs = &lvl_offsets[*li];
                let cnts = &lvl_counts[*li];
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
                // the (2,2) tag lie of real files is deliberately NOT
                // reproduced; the fixture writes the true SOF sampling
                let sub: Vec<u8> = [sampling.0, sampling.1]
                    .iter()
                    .flat_map(|v| w.p16(*v))
                    .collect();
                b.add(530, 3, 2, sub);
                let tables = &tables_of[*li];
                b.add(347, 7, tables.len() as u64, tables.clone());
                if *li == 0 && p.icc {
                    b.add(34675, 7, icc.len() as u64, icc.clone());
                    let _ = icc_at;
                }
            }
            Page::Assoc(ai) => {
                let (name, aw, ah, nsf, desc, payload) = &assoc[*ai];
                b.add_long(254, *nsf as u32);
                b.add_long(256, *aw);
                b.add_long(257, *ah);
                let bits: Vec<u8> = [8u16, 8, 8].iter().flat_map(|v| w.p16(*v)).collect();
                b.add(258, 3, 3, bits);
                b.add_short(259, 7);
                b.add_short(262, 2);
                let mut d = desc.as_bytes().to_vec();
                d.push(0);
                b.add(270, 2, d.len() as u64, d);
                b.add_short(277, 3);
                b.add_long(278, *ah); // one strip of the full height
                let cnt: Vec<u8> = if p.bigtiff {
                    w.p64(payload.len() as u64).to_vec()
                } else {
                    w.p32(payload.len() as u32).to_vec()
                };
                let off: Vec<u8> = if p.bigtiff {
                    w.p64(assoc_offsets[*ai]).to_vec()
                } else {
                    w.p32(assoc_offsets[*ai] as u32).to_vec()
                };
                b.add(273, if p.bigtiff { 16 } else { 4 }, 1, off);
                b.add(279, if p.bigtiff { 16 } else { 4 }, 1, cnt);
                b.add_short(284, 1);
                let _ = name;
            }
        }
        bodies.push(b);
    }

    // two passes: first compute each IFD's serialized length with next=0,
    // then rewrite with the real chain offsets (lengths are next-independent)
    let mut lens: Vec<u64> = Vec::new();
    let mut ats: Vec<u64> = Vec::new();
    {
        let mut cursor = buf.len() as u64;
        for b in &bodies {
            ats.push(cursor);
            let bytes = b.serialize(cursor, None);
            lens.push(bytes.len() as u64);
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
