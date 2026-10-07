//! Synthetic Ventana BIF fixture generator (tests; `fixtures` feature).
//! Everything is generated in code — no sample bytes are committed. Layout
//! mirrors what the iScan `StitchEncode.exe` writer produces:
//!
//! ```text
//! BigTIFF header (II+)
//! … tile payloads (abbreviated streams: SOI/SOF/SOS/EOI — the DQT/DHT
//!   tables live in a shared JPEGTables tag; unreferenced grid slots all
//!   point at one filler stream exactly like the public OS-2.bif) …
//! IFD 0  label ("Label Image", one complete stream, XMP = compact iScan)
//! IFD 1  thumbnail ("Thumbnail", one complete stream)
//! IFD 2  level=0 mag=40 (tiled grid, XMP = EncodeInfo stitch XML)
//! IFD 3+ level=1.. mag=… (tiled grids, halved canvases)
//! ```
//!
//! Default geometry (integers throughout so floor() placement == the true
//! fractional placement): tile 256×256, uniform overlaps OverlapX=32 /
//! OverlapY=24 → advances 224/232. Two scanned AOIs —
//!   AOI0: 3×3 tiles, origin (0,0), Pos (0,400) → stitched (0,0), covers
//!     704×720;
//!   AOI2: 2×2 tiles, origin (256,768) → grid (1,3), Pos (140,0) →
//!     stitched (140,632), covers 480×488;
//! plus one `AOIScanned="0"` AOI (skipped exactly like the real files).
//! The AOIs overlap pixel-wise ([140,620)×[632,720)) and AOI0 (smaller grid
//! rows) must win the overlap — the per-tile pattern offset makes a
//! precedence error visible against OpenSlide's own rendering. Stitched
//! L0 = 704×1120.
//!
//! Fault/variant knobs (each exercises one typed「暂时直传」refusal):
//! `jp2k` (33003 compression), `left_direction` (Direction='LEFT' — the
//! Ventana-1.bif variant OpenSlide rejects too), `z_layers` (iScan
//! Z-layers=3), `no_xml` (level 0 without the EncodeInfo XMLPacket),
//! `classic` (classic-TIFF container), `gray` (1 sample), `sparse` (a
//! referenced tile with count 0).

use crate::error::{CoreError, CoreResult};
use crate::io::RandomAccessSink;
use crate::jpeg::{encode_rgb, EncoderCfg, Sampling};

#[derive(Debug, Clone)]
pub struct BifGenParams {
    pub overlap_x: i64,
    pub overlap_y: i64,
    pub big_endian: bool,
    pub jp2k: bool,
    pub left_direction: bool,
    pub z_layers: bool,
    pub no_xml: bool,
    pub classic: bool,
    pub gray: bool,
    pub sparse: bool,
    /// 变体：ImageInfo 声明远超 tile 数组条目数的网格（canvas 同步拉满
    /// 以使网格仍在 canvas 内）——probe 必须在遍历前按条目数上限类型化
    /// 拒绝。
    pub grid_bomb: bool,
    /// 变体：只有 level=0 一层（OpenSlide 接受；适配器同样接受）。
    pub single_level: bool,
    /// 变体：AOI2 的 AOIScanned 写 "2"（非 0 即扫描，与 OpenSlide 一致；
    /// 旧代码 !="1" 会把整个区域跳掉）。
    pub aoi_scanned_alt: bool,
    /// 变体：AOI2 的 Pos-X 写小数 "140.9"（与 OpenSlide 一致按整数截断）。
    pub fractional_pos: bool,
    pub quality: u8,
    pub seed: u64,
}

impl Default for BifGenParams {
    fn default() -> Self {
        BifGenParams {
            overlap_x: 32,
            overlap_y: 24,
            big_endian: false,
            jp2k: false,
            left_direction: false,
            z_layers: false,
            no_xml: false,
            classic: false,
            gray: false,
            sparse: false,
            grid_bomb: false,
            single_level: false,
            aoi_scanned_alt: false,
            fractional_pos: false,
            quality: 90,
            seed: 7,
        }
    }
}

const TILE: i64 = 256;
const A0_COLS: i64 = 3;
const A0_ROWS: i64 = 3;
const A1_COLS: i64 = 2;
const A1_ROWS: i64 = 2;
/// AOI2's first tile in the TIFF tile grid (origin 256,768 / tile).
const A2_START: (i64, i64) = (1, 3);
/// Pos as written into the XML (Pos-X, Pos-Y); the adapter's Y-flip maps
/// them onto the stitched plane.
const A0_POS: (i64, i64) = (0, 400);
const A2_POS: (i64, i64) = (140, 0);

fn advance_x(p: &BifGenParams) -> i64 {
    TILE - p.overlap_x
}
fn advance_y(p: &BifGenParams) -> i64 {
    TILE - p.overlap_y
}

/// Stitched L0 size of the default layout (704×888 by default).
pub fn default_stitched(p: &BifGenParams) -> (i64, i64) {
    let ax = advance_x(p);
    let ay = advance_y(p);
    let a0_w = (A0_COLS - 1) * ax + TILE;
    let a0_h = (A0_ROWS - 1) * ay + TILE;
    let a2_w = (A1_COLS - 1) * ax + TILE;
    let a2_h = (A1_ROWS - 1) * ay + TILE;
    let a2_x = tile_xy(A2_START.0, A2_START.1, A2_START, A2_POS, p).0;
    let top = (A0_POS.1 + a0_h).max(A2_POS.1 + a2_h);
    let y0 = top - A0_POS.1 - a0_h;
    let y2 = top - A2_POS.1 - a2_h;
    let w = a0_w.max(a2_x + a2_w);
    let h = (y0 + a0_h).max(y2 + a2_h);
    (w, h)
}

/// Absolute stitched placement of tile (col,row) of the AOI with grid start
/// `start` and recorded Pos (the adapter's own formula).
fn tile_xy(col: i64, row: i64, start: (i64, i64), pos: (i64, i64), p: &BifGenParams) -> (i64, i64) {
    let ax = advance_x(p);
    let ay = advance_y(p);
    (
        col * ax + (pos.0 - start.0 * ax),
        row * ay + (pos.1 - start.1 * ay),
    )
}

/// Post-flip stitched Pos-Y of an AOI (the adapter's `y' = top − y − H`
/// with H = (rows−1)·advance_y + tile): the recorded Pos as written maps
/// onto this stitched plane, so the tile patterns encode final coordinates.
fn stitched_pos(pos: (i64, i64), rows: i64, p: &BifGenParams) -> (i64, i64) {
    let ay = advance_y(p);
    let h0 = (A0_ROWS - 1) * ay + TILE;
    let h2 = (A1_ROWS - 1) * ay + TILE;
    let top = (A0_POS.1 + h0).max(A2_POS.1 + h2);
    let h = (rows - 1) * ay + TILE;
    (pos.0, top - pos.1 - h)
}

/// Vendor tile number (1-based boustrophedon: rows bottom-to-top, odd rows
/// right-to-left) of grid (col,row) — the inverse of the adapter's
/// `tile_coordinates`.
fn tile_no(col: i64, row: i64, cols: i64, rows: i64) -> i64 {
    let r = rows - 1 - row;
    let c = if r % 2 == 1 { cols - col - 1 } else { col };
    r * cols + c + 1
}

// --------------------------------------------------------------------------- //
// minimal BigTIFF/classic writer for the fixture (file-order layouts only)
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

struct IfdB {
    w: W,
    entries: Vec<(u16, u16, u64, Vec<u8>)>,
}

impl IfdB {
    fn new(w: W) -> Self {
        IfdB { w, entries: Vec::new() }
    }
    fn add_short(&mut self, tag: u16, v: u16) {
        self.entries.push((tag, 3, 1, self.w.p16(v).to_vec()));
    }
    fn add_long(&mut self, tag: u16, v: u32) {
        self.entries.push((tag, 4, 1, self.w.p32(v).to_vec()));
    }
    fn add(&mut self, tag: u16, typ: u16, count: u64, value: Vec<u8>) {
        self.entries.push((tag, typ, count, value));
    }
    /// Serialize at file offset `at` with next-IFD pointer `next`. Value
    /// bytes longer than the inline capacity are placed in the external
    /// area right after the table and referenced by offset.
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
/// abbreviated pair the vendor writes (`JPEGTables` + SOF/SOS-bearing tile).
fn split_tables(jpeg: &[u8]) -> CoreResult<(Vec<u8>, Vec<u8>)> {
    let mut tables = Vec::new();
    let mut tile: Vec<u8> = vec![0xFF, 0xD8];
    let mut i = 2usize;
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

/// Per-tile pattern: a gentle global gradient plus a per-tile DC offset —
/// misplacement shifts the gradient at the seams and a wrong overlap
/// precedence swaps the offset, both visible against OpenSlide.
fn pattern_px(x: i64, y: i64, d: i64) -> [u8; 3] {
    [
        (96 + ((x + d) & 0x3F)) as u8,
        (96 + ((y + 3 * d) & 0x3F)) as u8,
        (140 + (((x + y) / 2 + d) & 0x3F)) as u8,
    ]
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

/// Encode one source tile whose top-left sits at stitched (x0,y0).
fn encode_src_tile(x0: i64, y0: i64, d: i64, p: &BifGenParams) -> CoreResult<Vec<u8>> {
    let mut img = vec![0u8; (TILE * TILE * 3) as usize];
    for y in 0..TILE {
        for x in 0..TILE {
            let px = pattern_px(x0 + x, y0 + y, d);
            let at = ((y * TILE + x) * 3) as usize;
            img[at..at + 3].copy_from_slice(&px);
        }
    }
    encode_rgb(&img, TILE as u32, TILE as u32, &EncoderCfg::with_quality(p.quality, Sampling::S422))
}

/// The TileJointInfo block of one scanned AOI (RIGHT + UP joints between
/// adjacent tiles; `left_direction` writes one LEFT joint instead — enough
/// for the typed refusal).
fn joints_xml(cols: i64, rows: i64, p: &BifGenParams) -> String {
    let mut s = String::new();
    if p.left_direction {
        s.push_str(&format!(
            "<TileJointInfo FlagJoined='1' Confidence='98' Direction='LEFT' Tile1='{}' Tile2='{}' OverlapX='{}' OverlapY='{}'/>\n",
            tile_no(0, 0, cols, rows),
            tile_no(1, 0, cols, rows),
            p.overlap_x,
            p.overlap_y
        ));
        return s;
    }
    for row in 0..rows {
        for col in 0..cols - 1 {
            s.push_str(&format!(
                "<TileJointInfo FlagJoined='1' Confidence='98' Direction='RIGHT' Tile1='{}' Tile2='{}' OverlapX='{}' OverlapY='{}'/>\n",
                tile_no(col, row, cols, rows),
                tile_no(col + 1, row, cols, rows),
                p.overlap_x,
                p.overlap_y
            ));
        }
    }
    for row in 1..rows {
        for col in 0..cols {
            s.push_str(&format!(
                "<TileJointInfo FlagJoined='1' Confidence='98' Direction='UP' Tile1='{}' Tile2='{}' OverlapX='{}' OverlapY='{}'/>\n",
                tile_no(col, row, cols, rows),
                tile_no(col, row - 1, cols, rows),
                p.overlap_x,
                p.overlap_y
            ));
        }
    }
    s
}

/// The EncodeInfo stitch XML of the default layout.
fn encode_info_xml(p: &BifGenParams) -> String {
    let z = if p.z_layers { 3 } else { 1 };
    // 变体：AOIScanned="2"（非 0 即扫描）；Pos-X 写小数（截断语义）
    let scanned2 = if p.aoi_scanned_alt { "2" } else { "1" };
    let a1px: std::borrow::Cow<str> = if p.fractional_pos {
        format!("{}.9", A2_POS.0).into()
    } else {
        A2_POS.0.to_string().into()
    };
    // 变体：AOI0 网格炸弹（远超 tile 数组条目数，canvas 同步拉满）；
    // 炸弹布局不写 TileJointInfo（真实布局只有 3×3 的邻接数据）
    let (a0_rows, a0_cols) = if p.grid_bomb {
        (BOMB_GRID, BOMB_GRID)
    } else {
        (A0_ROWS, A0_COLS)
    };
    let a0_joints = if p.grid_bomb {
        String::new()
    } else {
        joints_xml(A0_COLS, A0_ROWS, p)
    };
    format!(
        "<?xml version=\"1.0\"?>\n<EncodeInfo Ver='2'>\n\
<SlideInfo Rack=\"0\" Slot=\"16\" BaseName=\"synthetic-bif\">\n\
<iScan Magnification='40' ScanRes='0.5000' UnitNumber='FIXTURE' UserName='fixture' BuildVersion='1' Z-layers='{z}' Z-spacing='1'/>\n\
</SlideInfo>\n\
<SlideStitchInfo Left=\"-1\" Top=\"-1\" Right=\"-1\" Bottom=\"-1\">\n\
<ImageInfo AOIScanned=\"1\" Width=\"{T}\" Height=\"{T}\" NumRows=\"{a0_rows}\" NumCols=\"{a0_cols}\" Pos-X=\"{A0PX}\" Pos-Y=\"{A0PY}\">\n{A0J}</ImageInfo>\n\
<ImageInfo AOIScanned=\"0\" Width=\"{T}\" Height=\"{T}\" NumRows=\"1\" NumCols=\"1\" Pos-X=\"640\" Pos-Y=\"8\">\n</ImageInfo>\n\
<ImageInfo AOIScanned=\"{scanned2}\" Width=\"{T}\" Height=\"{T}\" NumRows=\"{A1_ROWS}\" NumCols=\"{A1_COLS}\" Pos-X=\"{A1PX}\" Pos-Y=\"{A1PY}\">\n{A1J}</ImageInfo>\n\
</SlideStitchInfo>\n\
<AoiOrigin>\n<AOI0 OriginX=\"0\" OriginY=\"0\"/>\n<AOI1 OriginX=\"0\" OriginY=\"1024\"/>\n<AOI2 OriginX=\"{A2OX}\" OriginY=\"{A2OY}\"/>\n</AoiOrigin>\n\
</EncodeInfo>\n",
        T = TILE,
        A0PX = A0_POS.0,
        A0PY = A0_POS.1,
        A0J = a0_joints,
        A1PX = a1px,
        A1PY = A2_POS.1,
        A1J = joints_xml(A1_COLS, A1_ROWS, p),
        A2OX = A2_START.0 * TILE,
        A2OY = A2_START.1 * TILE,
    )
}

/// 网格炸弹变体的 AOI0 尺寸（3907×3907 ≈ 15.3M 格，canvas 1e6² 时仍在
/// canvas 网格内；tile 数组只有 15 条——probe 必须在遍历前拒绝）。
const BOMB_GRID: i64 = 3907;

/// The compact iScan XML of IFD 0 (the vendor sniff input).
fn iscan_xml(p: &BifGenParams) -> String {
    let z = if p.z_layers { 3 } else { 1 };
    format!(
        "<iScan Magnification=\"40\" ScanRes=\"0.232500\" UnitNumber=\"FIXTURE\" UserName=\"fixture\" BuildVersion=\"3.3.1.1\" Z-layers=\"{z}\" Z-spacing=\"1\">\n<AOI0 Left=\"0\" Top=\"0\" Right=\"63\" Bottom=\"63\"/>\n</iScan>\n"
    )
}

/// One image IFD under construction. `tiles` are (offset,count) payload
/// pairs; `tables` the shared JPEGTables bytes; `xmp` the XMLPacket bytes.
#[allow(clippy::too_many_arguments)]
fn build_image_ifd(
    w: &W,
    desc: &str,
    canvas_w: i64,
    canvas_h: i64,
    tiles: &[(u64, u64)],
    tables: Option<&[u8]>,
    xmp: Option<&[u8]>,
    photo: u16,
    samples: u16,
    compression: u16,
    next: Option<u64>,
    at: u64,
) -> Vec<u8> {
    let mut b = IfdB::new(*w);
    b.add_long(256, canvas_w as u32);
    b.add_long(257, canvas_h as u32);
    if samples == 3 {
        let mut v = Vec::new();
        for c in [8u16, 8, 8] {
            v.extend_from_slice(&w.p16(c));
        }
        b.add(258, 3, 3, v);
    } else {
        b.add(258, 3, 1, w.p16(8).to_vec());
    }
    b.add_short(259, compression);
    b.add_short(262, photo);
    let mut d = desc.as_bytes().to_vec();
    d.push(0);
    b.add(270, 2, d.len() as u64, d);
    b.add_short(277, samples);
    b.add_short(284, 1);
    b.add_long(322, TILE as u32);
    b.add_long(323, TILE as u32);
    let mut offs = Vec::new();
    let mut cnts = Vec::new();
    for (o, c) in tiles {
        offs.extend_from_slice(&w.p64(*o));
        cnts.extend_from_slice(&w.p64(*c));
    }
    b.add(324, 16, tiles.len() as u64, offs);
    b.add(325, 16, tiles.len() as u64, cnts);
    if let Some(t) = tables {
        b.add(347, 7, t.len() as u64, t.to_vec());
    }
    if let Some(x) = xmp {
        b.add(700, 1, x.len() as u64, x.to_vec());
    }
    b.serialize(at, next)
}

/// Build the synthetic BIF; returns the file size. Assembled in memory
/// (fixtures are small) and written in one call.
pub fn build_synthetic_bif(out: &mut dyn RandomAccessSink, p: &BifGenParams) -> CoreResult<u64> {
    let w = W { bigtiff: !p.classic, little: !p.big_endian };
    let (canvas0_w, canvas0_h) = if p.grid_bomb {
        (1_000_000i64, 1_000_000)
    } else {
        (3 * TILE, 5 * TILE)
    };

    // ---- payloads -------------------------------------------------------- //
    let mut rng = Rng(p.seed);
    let header_len: u64 = if w.bigtiff { 16 } else { 8 };
    let mut blob: Vec<u8> = Vec::new();
    let mut push = |blob: &mut Vec<u8>, data: &[u8]| -> u64 {
        let at = header_len + blob.len() as u64;
        blob.extend_from_slice(data);
        at
    };

    // label (IFD 0): 96×64 complete stream
    let label = {
        let mut img = vec![0u8; 96 * 64 * 3];
        for (i, px) in img.chunks_exact_mut(3).enumerate() {
            let v = ((i as u64 * 1103515245) as u8) ^ rng.byte();
            px.copy_from_slice(&[v, 200u8.wrapping_sub(v / 3), 128]);
        }
        encode_rgb(&img, 96, 64, &EncoderCfg::with_quality(90, Sampling::S444))?
    };
    let label_off = push(&mut blob, &label);
    let label_pair = (label_off, label.len() as u64);

    // thumbnail (IFD 1): 128×80 complete stream
    let thumb = {
        let mut img = vec![0u8; 128 * 80 * 3];
        for (i, px) in img.chunks_exact_mut(3).enumerate() {
            px.copy_from_slice(&[(i % 97) as u8, (i % 71) as u8, 180]);
        }
        encode_rgb(&img, 128, 80, &EncoderCfg::with_quality(90, Sampling::S444))?
    };
    let thumb_off = push(&mut blob, &thumb);
    let thumb_pair = (thumb_off, thumb.len() as u64);

    // level 0: one abbreviated tile per REFERENCED grid slot (pattern from
    // the tile's true stitched position) + shared tables + a filler stream
    let mut referenced: std::collections::HashMap<(i64, i64), (u64, u64)> =
        std::collections::HashMap::new();
    let mut d = 1i64;
    let mut a0_tables: Option<Vec<u8>> = None;
    // post-flip stitched Pos of each AOI (the adapter's Y-flip result) —
    // the tile patterns are functions of the FINAL stitched coordinates
    let a0_stitched = stitched_pos(A0_POS, A0_ROWS, p);
    let a2_stitched = stitched_pos(A2_POS, A1_ROWS, p);
    for row in 0..A0_ROWS {
        for col in 0..A0_COLS {
            let (x, y) = tile_xy(col, row, (0, 0), a0_stitched, p);
            let j = encode_src_tile(x, y, d, p)?;
            let (tables, tile) = split_tables(&j)?;
            if a0_tables.is_none() {
                a0_tables = Some(tables);
            }
            let at = push(&mut blob, &tile);
            referenced.insert((col, row), (at, tile.len() as u64));
            d += 1;
        }
    }
    // AOI2 的图案偏移与 AOI0 拉开（d=100..）：重叠带（AOI2 行号更大、按
    // OpenSlide 优先级胜出）内容与 AOI0 明显不同——优先级翻转会在合成
    // 像素门的重叠带里产生大误差
    let mut d_a2 = 100i64;
    for row in 0..A1_ROWS {
        for col in 0..A1_COLS {
            let (x, y) = tile_xy(A2_START.0 + col, A2_START.1 + row, A2_START, a2_stitched, p);
            let j = encode_src_tile(x, y, d_a2, p)?;
            let (_tables, tile) = split_tables(&j)?;
            let at = push(&mut blob, &tile);
            referenced.insert((A2_START.0 + col, A2_START.1 + row), (at, tile.len() as u64));
            d_a2 += 1;
        }
    }
    let tables_bytes = a0_tables.unwrap_or_default();
    let filler = {
        let j = encode_src_tile(0, 0, 0, p)?;
        split_tables(&j)?.1
    };
    let filler_pair = (push(&mut blob, &filler), filler.len() as u64);
    let tables_at = push(&mut blob, &tables_bytes);

    // sparse variant: one referenced tile loses its payload
    if p.sparse {
        if let Some(e) = referenced.get_mut(&(0, 0)) {
            *e = (e.0, 0);
        }
    }

    // reduced levels: halved canvases, one real tile each + shared tables
    let levels_meta: Vec<(i64, i64, u32)> = {
        let mut v = Vec::new();
        let mut cw = canvas0_w;
        let mut ch = canvas0_h;
        let mut mag = 20u32;
        while cw > 16 && ch > 16 && v.len() < 3 {
            v.push((cw, ch, mag));
            cw /= 2;
            ch /= 2;
            mag /= 2;
        }
        v
    };
    // reduced levels: COMPLETE tile grids (OpenSlide reads them; the
    // converter itself never decodes them for pixels — l0-box2). The grid
    // bomb only exercises the probe's pre-traversal bound — its canvas is
    // huge, so skip the (billion-tile) reduced levels and emit a single
    // level IFD instead.
    let skip_reduced = p.single_level || p.grid_bomb;
    let mut red_payloads: Vec<Vec<(u64, u64)>> = Vec::new();
    let mut red_tables: Option<Vec<u8>> = None;
    for (li, &(cw, ch, _mag)) in levels_meta.iter().enumerate() {
        if skip_reduced {
            break;
        }
        let across = (cw + TILE - 1) / TILE;
        let down = (ch + TILE - 1) / TILE;
        let mut tiles = Vec::new();
        for row in 0..down {
            for col in 0..across {
                let mut img = vec![0u8; (TILE * TILE * 3) as usize];
                for y in 0..TILE {
                    for x in 0..TILE {
                        let px = pattern_px(
                            col * TILE + x / (1 << li.min(4)),
                            row * TILE + y / (1 << li.min(4)),
                            2 + li as i64,
                        );
                        let at = ((y * TILE + x) * 3) as usize;
                        img[at..at + 3].copy_from_slice(&px);
                    }
                }
                let j = encode_rgb(&img, TILE as u32, TILE as u32, &EncoderCfg::with_quality(p.quality, Sampling::S422))?;
                let (t, tile) = split_tables(&j)?;
                if red_tables.is_none() {
                    red_tables = Some(t);
                }
                let at = push(&mut blob, &tile);
                tiles.push((at, tile.len() as u64));
            }
        }
        red_payloads.push(tiles);
    }

    // ---- IFDs: pass 1 = sizes, pass 2 = real offsets -------------------- //
    let mut ifd_at = header_len + blob.len() as u64;
    ifd_at = (ifd_at + 7) & !7;
    let xmp0 = iscan_xml(p).into_bytes();
    let xmp2 = encode_info_xml(p).into_bytes();
    let samples = if p.gray { 1 } else { 3 };
    let photo: u16 = if p.gray { 1 } else { 6 };
    let compression: u16 = if p.jp2k { 33003 } else { 7 };

    let mut ifd_specs: Vec<(String, i64, i64, Vec<(u64, u64)>, Option<Vec<u8>>, Option<Vec<u8>>, u16, u16, u16)> = Vec::new();
    {
        let mut l0_tiles = Vec::new();
        for row in 0..5 {
            for col in 0..3 {
                l0_tiles.push(*referenced.get(&(col, row)).unwrap_or(&filler_pair));
            }
        }
        ifd_specs.push((
            "Label Image".into(),
            96,
            64,
            vec![label_pair],
            None,
            Some(xmp0),
            6,
            3,
            7,
        ));
        ifd_specs.push((
            "Thumbnail".into(),
            128,
            80,
            vec![thumb_pair],
            None,
            None,
            6,
            3,
            7,
        ));
        ifd_specs.push((
            "level=0 mag=40 quality=90".into(),
            canvas0_w,
            canvas0_h,
            l0_tiles,
            Some(tables_bytes.clone()),
            if p.no_xml { None } else { Some(xmp2.clone()) },
            photo,
            samples,
            compression,
        ));
        for (li, &(cw, ch, mag)) in levels_meta.iter().enumerate() {
            if skip_reduced {
                break;
            }
            ifd_specs.push((
                format!("level={} mag={} quality=90", li + 1, mag),
                cw,
                ch,
                red_payloads[li].clone(),
                red_tables.clone(),
                None,
                photo,
                samples,
                compression,
            ));
        }
    }
    let mut at = ifd_at;
    let mut sizes = Vec::new();
    for spec in &ifd_specs {
        let (desc, cwid, chgt, tiles, tables, xmp, ph, sp, comp) = spec;
        let n = build_image_ifd(&w, desc, *cwid, *chgt, tiles, tables.as_deref(), xmp.as_deref(), *ph, *sp, *comp, None, 0).len() as u64;
        sizes.push(n);
        at += n;
    }
    let mut file: Vec<u8> = Vec::new();
    if w.bigtiff {
        file.extend_from_slice(&w.p16(if w.little { 0x4949 } else { 0x4D4D }));
        file.extend_from_slice(&w.p16(43));
        file.extend_from_slice(&w.p16(8));
        file.extend_from_slice(&w.p16(0));
        file.extend_from_slice(&w.p64(ifd_at));
    } else {
        file.extend_from_slice(&w.p16(if w.little { 0x4949 } else { 0x4D4D }));
        file.extend_from_slice(&w.p16(42));
        file.extend_from_slice(&w.p32(ifd_at as u32));
    }
    debug_assert_eq!(file.len() as u64, header_len);
    file.extend_from_slice(&blob);
    while (file.len() as u64) < ifd_at {
        file.push(0);
    }
    let mut cur = ifd_at;
    for (i, spec) in ifd_specs.iter().enumerate() {
        let (desc, cwid, chgt, tiles, tables, xmp, ph, sp, comp) = spec;
        let next = if i + 1 < ifd_specs.len() { Some(cur + sizes[i]) } else { None };
        let bytes =
            build_image_ifd(&w, desc, *cwid, *chgt, tiles, tables.as_deref(), xmp.as_deref(), *ph, *sp, *comp, next, cur);
        debug_assert_eq!(bytes.len() as u64, sizes[i]);
        file.extend_from_slice(&bytes);
        cur += sizes[i];
    }
    out.write_at(0, &file)?;
    Ok(file.len() as u64)
}
