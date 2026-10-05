//! Synthetic Leica-style SCN fixture generator (F4 tests; `fixtures`
//! feature). Everything is generated in code — no sample bytes are
//! committed. Layout mirrors the real SCN400 structure (2010/10/01 schema):
//!
//! ```text
//! BigTIFF header (II+ or MM+; big_endian knob)
//! … tile payloads of every level (complete JPEG streams, no shared
//!   tables — the real files carry no tag 347) …
//! IFD chain:
//!   IFD0: label image level 0 (tiled JPEG; description = the SCN XML)
//!   IFD1..: label reduced levels
//!   then: main image levels r=0.. (tiled JPEG)
//! ```
//!
//! The XML mirrors the real vocabulary: `<scn xmlns="…/scn/2010/10/01">` →
//! `<collection>` → `<image>` (label first, main second) with `<pixels
//! sizeX sizeY>` + `<dimension r ifd/>`, `<view sizeX sizeY>` (view size is
//! `mpp_nm × pixel size` — nanometres), `<scanSettings>` with
//! `<objective>` and `<illuminationSource>`.
//!
//! Fault/variant knobs: `sparse` (missing (0,0) tiles on main level 0),
//! `fluoro` (illuminationSource=fluorescence), `non_jpeg` (main levels use
//! compression 8/deflate payloads), `desc_mode` (xml | none | ome |
//! converter | foreign) for the routing-level rejections.

use crate::error::{CoreError, CoreResult};
use crate::io::RandomAccessSink;
use crate::jpeg::{encode_rgb, EncoderCfg, Sampling};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DescMode {
    /// Real SCN XML (the default).
    Xml,
    /// No description tag at all.
    None,
    /// OME-XML description (OME-TIFF is not a conversion input).
    Ome,
    /// Converter-output JSON description (this tool's own BigTIFF).
    Converter,
    /// A foreign scanner's plain description.
    Foreign,
}

#[derive(Debug, Clone)]
pub struct ScnGenParams {
    /// Main image level-0 size.
    pub width: u32,
    pub height: u32,
    /// Nominal tile size (SCN400: 512).
    pub tile: u32,
    /// Main pyramid level count (each step ÷2, ≥ 64 px).
    pub levels: u32,
    /// Label image size (its own 2-level pyramid shares the tile size).
    pub label_w: u32,
    pub label_h: u32,
    pub big_endian: bool,
    /// Sparse grid: main level 0 loses two interior tiles ((0,0) entries).
    pub sparse: bool,
    /// Fluorescence variant (illuminationSource=fluorescence).
    pub fluoro: bool,
    /// Non-JPEG variant: main levels are deflate-marked with junk payloads.
    pub non_jpeg: bool,
    /// IFD 0 description override.
    pub desc_mode: DescMode,
    /// Nanometres per pixel written into `<view>` (mpp = nm/1000).
    pub mpp_nm: u64,
    pub objective: f64,
    pub quality: u8,
    pub seed: u64,
}

impl Default for ScnGenParams {
    fn default() -> Self {
        ScnGenParams {
            width: 520,
            height: 300,
            tile: 128,
            levels: 3,
            label_w: 128,
            label_h: 96,
            big_endian: false,
            sparse: false,
            fluoro: false,
            non_jpeg: false,
            desc_mode: DescMode::Xml,
            mpp_nm: 500,
            objective: 20.0,
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

fn encode_tile(w: u32, h: u32, seed: u64, quality: u8) -> CoreResult<Vec<u8>> {
    let mut rng = Rng(seed | 1);
    let mut px = vec![0u8; (w as usize) * (h as usize) * 3];
    for c in px.chunks_exact_mut(3) {
        c[0] = rng.byte();
        c[1] = rng.byte();
        c[2] = rng.byte();
    }
    encode_rgb(&px, w, h, &EncoderCfg::with_quality(quality, Sampling::S444))
}

/// Level geometry: integer ÷2 per step while the level stays ≥ 64 px.
fn level_geometry(w: u32, h: u32, levels: u32) -> Vec<(u32, u32)> {
    let mut out = vec![(w, h)];
    while out.len() < levels as usize {
        let (lw, lh) = *out.last().unwrap();
        let (nw, nh) = ((lw + 1) / 2, (lh + 1) / 2);
        if nw < 64 || nh < 64 || (nw == lw && nh == lh) {
            break;
        }
        out.push((nw, nh));
    }
    out
}

// --------------------------------------------------------------------------- //
// minimal BigTIFF writer for the fixture (reuses the SVS fixture's shape)
// --------------------------------------------------------------------------- //

#[derive(Clone, Copy)]
struct W {
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
        self.add(tag, 3, 1, self.w.p16(v).to_vec());
    }
    fn add_long(&mut self, tag: u16, v: u32) {
        self.add(tag, 4, 1, self.w.p32(v).to_vec());
    }
    fn serialize(&self, at: u64, next: Option<u64>) -> Vec<u8> {
        let w = &self.w;
        let mut sorted = self.entries.clone();
        sorted.sort_by_key(|e| e.0);
        let esize: u64 = 20;
        let ifd_len = 8 + esize * sorted.len() as u64 + 8;
        let mut out = Vec::new();
        out.extend_from_slice(&w.p64(sorted.len() as u64));
        let mut ext_at = at + ifd_len;
        let mut ext: Vec<Vec<u8>> = Vec::new();
        for (tag, typ, count, value) in &sorted {
            out.extend_from_slice(&w.p16(*tag));
            out.extend_from_slice(&w.p16(*typ));
            out.extend_from_slice(&w.p64(*count));
            if (value.len() as u64) <= 8 {
                let mut v = value.clone();
                v.resize(8, 0);
                out.extend_from_slice(&v);
            } else {
                out.extend_from_slice(&w.p64(ext_at));
                let mut v = value.clone();
                if v.len() % 2 == 1 {
                    v.push(0);
                }
                ext_at += v.len() as u64;
                ext.push(v);
            }
        }
        match next {
            Some(n) => out.extend_from_slice(&w.p64(n)),
            None => out.extend_from_slice(&w.p64(0)),
        }
        for v in ext {
            out.extend_from_slice(&v);
        }
        out
    }
}

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('"', "&quot;")
}

/// Build the SCN XML (the real vocabulary, deterministic values).
pub fn build_scn_xml(
    label_levels: &[(u32, u32, u32)], // (r, sizeX, sizeY)
    main_levels: &[(u32, u32, u32)],
    label_ifd0: u32,
    main_ifd0: u32,
    p: &ScnGenParams,
) -> String {
    let illum = if p.fluoro { "fluorescence" } else { "brightfield" };
    let dims = |levels: &[(u32, u32, u32)], ifd0: u32| -> String {
        levels
            .iter()
            .map(|(r, w, h)| {
                format!(
                    "<dimension sizeX=\"{w}\" sizeY=\"{h}\" r=\"{r}\" ifd=\"{}\" />",
                    ifd0 + r
                )
            })
            .collect::<String>()
    };
    let view = |(_, w, h): &(u32, u32, u32)| {
        format!(
            "<view sizeX=\"{}\" sizeY=\"{}\" offsetX=\"0\" offsetY=\"0\" spacingZ=\"0\" />",
            (*w as u64) * p.mpp_nm,
            (*h as u64) * p.mpp_nm
        )
    };
    format!(
        "<?xml version=\"1.0\"?>\
<scn xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" \
xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\" uuid=\"urn:uuid:00000000-0000-0000-0000-000000000000\" \
xmlns=\"http://www.leica-microsystems.com/scn/2010/10/01\">\
<collection name=\"ImageCollection_FIXTURE\" uuid=\"urn:uuid:11111111-1111-1111-1111-111111111111\" \
sizeX=\"{bigx}\" sizeY=\"{bigy}\"><barcode>RklYVFVSRS0wMQ==</barcode>\
<image name=\"image_label\" uuid=\"urn:uuid:22222222-2222-2222-2222-222222222222\">\
<creationDate>2026-01-01T00:00:00.0Z</creationDate>\
<device model=\"Leica SCN400;Leica SCN\" version=\"1.4.0.9691\" />\
<pixels sizeX=\"{lw}\" sizeY=\"{lh}\">{ldims}</pixels>{lview}\
<scanSettings><objectiveSettings><objective>{lo}</objective></objectiveSettings>\
<illuminationSettings><numericalAperture>0.7</numericalAperture>\
<illuminationSource>{illum}</illuminationSource></illuminationSettings></scanSettings></image>\
<image name=\"image_main\" uuid=\"urn:uuid:33333333-3333-3333-3333-333333333333\">\
<creationDate>2026-01-01T00:00:00.0Z</creationDate>\
<device model=\"Leica SCN400;Leica SCN\" version=\"1.4.0.9691\" />\
<pixels sizeX=\"{mw}\" sizeY=\"{mh}\">{mdims}</pixels>{mview}\
<scanSettings><objectiveSettings><objective>{mo}</objective></objectiveSettings>\
<illuminationSettings><numericalAperture>0.4</numericalAperture>\
<illuminationSource>{illum}</illuminationSource></illuminationSettings></scanSettings></image>\
</collection></scn> ",
        bigx = p.width * 1000,
        bigy = p.height * 1000,
        lw = label_levels[0].1,
        lh = label_levels[0].2,
        ldims = dims(label_levels, label_ifd0),
        lview = view(&label_levels[0]),
        lo = p.objective / 40.0,
        mw = main_levels[0].1,
        mh = main_levels[0].2,
        mdims = dims(main_levels, main_ifd0),
        mview = view(&main_levels[0]),
        mo = xml_escape(&format!("{}", p.objective)),
        illum = illum,
    )
}

/// Build the synthetic SCN file; returns the file size.
pub fn build_synthetic_scn(out: &mut dyn RandomAccessSink, p: &ScnGenParams) -> CoreResult<u64> {
    if !(64..=100_000).contains(&p.width) || !(64..=100_000).contains(&p.height) {
        return Err(CoreError::header("fixture: 宽/高越界"));
    }
    if !(16..=8192).contains(&p.tile) {
        return Err(CoreError::header("fixture: tile 越界"));
    }
    let w = W { little: !p.big_endian };
    let main_levels = level_geometry(p.width, p.height, p.levels);
    let label_levels = level_geometry(p.label_w, p.label_h, 2);
    let _ifd_of = |levels: &[(u32, u32)], ifd0: u32| -> Vec<(u32, u32, u32, u32)> {
        levels
            .iter()
            .enumerate()
            .map(|(r, (lw, lh))| (r as u32, ifd0 + r as u32, *lw, *lh))
            .collect()
    };
    let label_ifd0 = 0u32;
    let main_ifd0 = label_levels.len() as u32;

    // ---- encode every level's tiles (complete self-contained JPEGs) ----- //
    let mut tiles_of: Vec<Vec<Option<Vec<u8>>>> = Vec::new();
    for (li, &(lw, lh)) in main_levels.iter().enumerate() {
        let across = (lw + p.tile - 1) / p.tile;
        let down = (lh + p.tile - 1) / p.tile;
        let mut lvl = Vec::new();
        for row in 0..down {
            for col in 0..across {
                // sparse knob: two interior tiles of level 0 vanish
                let missing = p.sparse
                    && li == 0
                    && across > 2
                    && down > 2
                    && ((row == 1 && col == 1) || (row == 0 && col == across - 2));
                if missing {
                    lvl.push(None);
                    continue;
                }
                let seed = p
                    .seed
                    ^ (li as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15)
                    ^ (row as u64).wrapping_mul(0x100000001B3)
                    ^ (col as u64).wrapping_mul(7);
                lvl.push(Some(encode_tile(p.tile, p.tile, seed, p.quality)?));
            }
        }
        tiles_of.push(lvl);
    }
    let mut label_tiles: Vec<Vec<u8>> = Vec::new();
    for &(lw, lh) in &label_levels {
        label_tiles.push(encode_tile(lw, lh, 0xAB, p.quality)?);
    }

    // ---- description ------------------------------------------------------ //
    let desc: Vec<u8> = match p.desc_mode {
        DescMode::Xml => build_scn_xml(
            &label_levels
                .iter()
                .enumerate()
                .map(|(r, (lw, lh))| (r as u32, *lw, *lh))
                .collect::<Vec<_>>(),
            &main_levels
                .iter()
                .enumerate()
                .map(|(r, (lw, lh))| (r as u32, *lw, *lh))
                .collect::<Vec<_>>(),
            label_ifd0,
            main_ifd0,
            p,
        )
        .into_bytes(),
        DescMode::None => Vec::new(),
        DescMode::Ome => {
            b"<?xml version=\"1.0\" encoding=\"UTF-8\"?><OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\"></OME>\x00"
                .to_vec()
        }
        DescMode::Converter => {
            format!("{{\"adapter\": \"{}\", \"adapter_version\": \"1\", \"mpp_x\": 0.5, \"mpp_y\": 0.5, \"objective\": 20.0, \"source_format\": \"{}\"}}\u{0}", crate::scn::SOURCE_FORMAT, crate::scn::SOURCE_FORMAT).into_bytes()
        }
        DescMode::Foreign => b"Some Other Scanner v1\x00".to_vec(),
    };

    // ---- file assembly --------------------------------------------------- //
    let mut buf: Vec<u8> = vec![0u8; 16]; // BigTIFF header
    let mut lvl_offsets: Vec<Vec<Option<u64>>> = Vec::new();
    let mut lvl_counts: Vec<Vec<Option<u64>>> = Vec::new();
    for tiles in &tiles_of {
        let mut offs = Vec::new();
        let mut cnts = Vec::new();
        for t in tiles {
            match t {
                Some(payload) => {
                    let at = buf.len() as u64;
                    buf.extend_from_slice(payload);
                    offs.push(Some(at));
                    cnts.push(Some(payload.len() as u64));
                }
                None => {
                    offs.push(None);
                    cnts.push(None);
                }
            }
        }
        lvl_offsets.push(offs);
        lvl_counts.push(cnts);
    }
    let label_offsets: Vec<u64> = label_tiles
        .iter()
        .map(|payload| {
            let at = buf.len() as u64;
            buf.extend_from_slice(payload);
            at
        })
        .collect();

    // IFD bodies: label levels first (IFD0 carries the description), then
    // the main levels
    let mut bodies: Vec<IfdB> = Vec::new();
    for (i, &(lw, lh)) in label_levels.iter().enumerate() {
        let mut b = IfdB::new(w);
        b.add_long(256, lw);
        b.add_long(257, lh);
        let bits: Vec<u8> = [8u16, 8, 8].iter().flat_map(|v| w.p16(*v)).collect();
        b.add(258, 3, 3, bits);
        b.add_short(259, 7);
        b.add_short(262, 6);
        if i == 0 && !desc.is_empty() {
            b.add(270, 2, desc.len() as u64, desc.clone());
        }
        b.add_short(277, 3);
        b.add_short(284, 1);
        b.add_short(322, p.tile as u16);
        b.add_short(323, p.tile as u16);
        b.add(324, 16, 1, w.p64(label_offsets[i]).to_vec());
        b.add(325, 16, 1, w.p64(label_tiles[i].len() as u64).to_vec());
        bodies.push(b);
    }
    for (li, &(lw, lh)) in main_levels.iter().enumerate() {
        let mut b = IfdB::new(w);
        b.add_long(256, lw);
        b.add_long(257, lh);
        let bits: Vec<u8> = [8u16, 8, 8].iter().flat_map(|v| w.p16(*v)).collect();
        b.add(258, 3, 3, bits);
        b.add_short(259, if p.non_jpeg { 8 } else { 7 });
        b.add_short(262, 6);
        b.add_short(277, 3);
        b.add_short(284, 1);
        b.add_short(322, p.tile as u16);
        b.add_short(323, p.tile as u16);
        let offs = &lvl_offsets[li];
        let cnts = &lvl_counts[li];
        let off_bytes: Vec<u8> = offs
            .iter()
            .map(|o| w.p64(o.unwrap_or(0)))
            .flatten()
            .collect();
        let cnt_bytes: Vec<u8> = cnts
            .iter()
            .map(|c| w.p64(c.unwrap_or(0)))
            .flatten()
            .collect();
        b.add(324, 16, offs.len() as u64, off_bytes);
        b.add(325, 16, cnts.len() as u64, cnt_bytes);
        if p.non_jpeg {
            // junk payloads for the deflate-marked variant (each present
            // tile still has a non-empty region so the probe reaches the
            // compression check first)
            for slot in offs.iter().flatten() {
                let at = *slot as usize;
                buf[at..at + 64].copy_from_slice(&[0u8; 64]);
            }
        }
        bodies.push(b);
    }

    // two passes: chain offsets
    let mut ats: Vec<u64> = Vec::new();
    {
        let mut cursor = buf.len() as u64;
        for b in &bodies {
            ats.push(cursor);
            cursor += b.serialize(cursor, None).len() as u64;
        }
    }
    for (i, b) in bodies.iter().enumerate() {
        let next = if i + 1 < bodies.len() { Some(ats[i + 1]) } else { None };
        buf.extend_from_slice(&b.serialize(ats[i], next));
    }

    // header (BigTIFF, either byte order)
    if w.little {
        buf[0..2].copy_from_slice(b"II");
    } else {
        buf[0..2].copy_from_slice(b"MM");
    }
    buf[2..4].copy_from_slice(&w.p16(43));
    buf[4..6].copy_from_slice(&w.p16(8));
    buf[6..8].copy_from_slice(&w.p16(0));
    buf[8..16].copy_from_slice(&w.p64(ats[0]));

    out.write_at(0, &buf)?;
    out.flush()?;
    Ok(buf.len() as u64)
}
