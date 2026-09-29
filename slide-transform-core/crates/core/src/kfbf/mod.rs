//! KFBF (fluorescence) vendor-layout parser — port of `kfb/vendor_kfbf.py`
//! (kfb_fl_v1). Little-endian tagged header + 64 B tile records; tiles are
//! **per grid cell × channel** grayscale JPEGs located via a 96 B pointer
//! block + 48 B side record. Pyramid geometry differs from brightfield:
//! L1..L3 floor-halving, L4..L8 anchored at ceil(L0/256)·2^(8−L), L9+ floor
//! halving. Unknown variants fail closed; all offsets/lengths bounds-checked.

#[cfg(feature = "fixtures")]
pub mod fixture;

use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, ScratchFactory};
use crate::kfb::{ascii_replace, check_jpeg_bounds, check_payload};

pub const TILE_W: u32 = 256;
pub const TILE_H: u32 = 256;

pub const KFBF_MAGIC: [u8; 8] = [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x46];
pub const FORMAT_VERSION: f32 = 2.1;
const FORMAT_VERSION_EPS: f32 = 1e-4;

pub const MAX_CHANNEL_COUNT: usize = 16;
pub const MAX_LEVEL_COUNT: u32 = 24;
pub const MAX_TAG_COUNT: u32 = 64;
pub const MAX_TAG_VALUE: u32 = 4096;
pub const MIN_PAYLOAD_LENGTH: u32 = 1;
pub const MAX_PAYLOAD_LENGTH: u32 = 8 * 1024 * 1024;
pub const MAX_DIM_PX: u32 = 200_000;
pub const MIN_TILE_COUNT: u32 = 1;
pub const MAX_TILE_COUNT: u32 = 2_000_000;

const TILE_REC: u64 = 64;
const PTR_BLOCK_BYTES: u64 = 96;
const ASSOC_REC_BYTES: usize = 52;

/// Paged cell-store record: 24 B per grid cell
/// (`ptr_offset` u64, `side_off` u64, `jpeg_w` u16, `jpeg_h` u16, 4 B pad).
/// A zeroed record marks an empty (sparse) cell.
pub const CELL_REC: usize = 24;

#[derive(Debug, Clone)]
pub struct KfbfChannel {
    pub index: usize,
    pub name: String,
    pub color_rgb: (u32, u32, u32),
    /// Raw calibration value; interpreted as milliseconds (ASSUMED unit).
    pub exposure: f64,
    pub gamma: f64,
}

#[derive(Debug, Clone)]
pub struct KfbfHeader {
    pub width_px: u32,
    pub height_px: u32,
    pub objective: f64,
    pub tile_count: u32,
    pub channel_count: usize,
    pub mpp: f64,
    pub scanner_id: String,
    pub index_offset: u64,
    pub scanned_at: u32,
}

#[derive(Debug, Clone, Copy)]
pub struct KfbfLevel {
    pub level: u32,
    pub width: u32,
    pub height: u32,
}

impl KfbfLevel {
    pub fn tiles_across(&self) -> u32 {
        (self.width + TILE_W - 1) / TILE_W
    }
    pub fn tiles_down(&self) -> u32 {
        (self.height + TILE_H - 1) / TILE_H
    }
}

#[derive(Debug, Clone)]
pub struct KfbfAssociated {
    pub name: String,
    pub payload_offset: u64,
    pub payload_length: u32,
    pub width: u32,
    pub height: u32,
}

/// A tile grid cell as stored in the paged index.
#[derive(Debug, Clone, Copy)]
pub struct KfbfCell {
    pub level: u8,
    pub x: u32,
    pub y: u32,
    pub jpeg_w: u32,
    pub jpeg_h: u32,
    pub ptr_offset: u64,
    pub side_off: u64,
}

impl KfbfCell {
    pub fn is_full_cell(&self, lv: &KfbfLevel) -> bool {
        let (cw, ch) = self.cell_size(lv);
        self.jpeg_w == cw && self.jpeg_h == ch
    }
    /// TIFF-canvas size of this cell (right/bottom cropped).
    pub fn cell_size(&self, lv: &KfbfLevel) -> (u32, u32) {
        (
            TILE_W.min(lv.width.saturating_sub(self.x)),
            TILE_H.min(lv.height.saturating_sub(self.y)),
        )
    }
}

pub struct KfbfDocument {
    pub header: KfbfHeader,
    pub channels: Vec<KfbfChannel>,
    /// Consecutive 0..=max_level.
    pub levels: Vec<KfbfLevel>,
    /// Paged per-level cell store (scratch-backed, 24 B/cell).
    pub cells: PagedCells,
    pub associated: Vec<KfbfAssociated>,
    pub source_size: u64,
}

impl KfbfDocument {
    /// Read the cell record at `cell` (row-major), or `None` if sparse.
    pub fn cell_at(&self, level: u32, cell: u32) -> CoreResult<Option<KfbfCell>> {
        self.cells.read_cell(level, cell)
    }

    /// Channel payload (offset, length) via pointer block + side record.
    pub fn channel_payload(
        &self,
        src: &dyn ByteSource,
        cell: &KfbfCell,
        channel: usize,
    ) -> CoreResult<(u64, u32)> {
        let nch = self.header.channel_count;
        if channel >= nch {
            return Err(CoreError::index("通道号越界"));
        }
        let side = src.read_at(cell.side_off, nch * 8)?;
        let len =
            u64::from_le_bytes(side[channel * 8..channel * 8 + 8].try_into().unwrap());
        let ptrs = src.read_at(cell.ptr_offset, nch * 8)?;
        let off =
            u64::from_le_bytes(ptrs[channel * 8..channel * 8 + 8].try_into().unwrap());
        let len = u32::try_from(len)
            .map_err(|_| CoreError::index("通道 payload 长度超出 u32"))?;
        Ok((off, len))
    }
}

/// Paged 24 B/cell grid store (one scratch sink per level + occupancy bitmap).
pub struct PagedCells {
    stores: Vec<Box<dyn crate::io::ScratchSink>>,
    occupied: Vec<Vec<u64>>,
    cells: Vec<u32>,
    levels: Vec<u32>,
    tiles_across: Vec<u32>,
}

impl PagedCells {
    fn store_index(&self, level: u32) -> CoreResult<usize> {
        self.levels
            .iter()
            .position(|&l| l == level)
            .ok_or_else(|| CoreError::index(format!("level={level} 越界")))
    }

    pub fn put(&mut self, rec: KfbfCell) -> CoreResult<()> {
        let si = self.store_index(rec.level as u32)?;
        let row = rec.y / TILE_H;
        let col = rec.x / TILE_W;
        let ta = self.tiles_across[si] as u64;
        let bit = row as u64 * ta + col as u64;
        if bit >= self.cells[si] as u64 {
            return Err(CoreError::index(format!(
                "cell ({row},{col}) 超出层 {} 网格 {}",
                rec.level, self.cells[si]
            )));
        }
        let bit = bit as usize;
        if self.occupied[si][bit / 64] & (1u64 << (bit % 64)) != 0 {
            return Err(CoreError::index(format!(
                "tile 网格单元 ({},{},{}) 重复",
                rec.level, row, col
            )));
        }
        self.occupied[si][bit / 64] |= 1u64 << (bit % 64);
        let mut b = [0u8; CELL_REC];
        b[..8].copy_from_slice(&rec.ptr_offset.to_le_bytes());
        b[8..16].copy_from_slice(&rec.side_off.to_le_bytes());
        b[16..18].copy_from_slice(&(rec.jpeg_w as u16).to_le_bytes());
        b[18..20].copy_from_slice(&(rec.jpeg_h as u16).to_le_bytes());
        self.stores[si].write_at(bit as u64 * CELL_REC as u64, &b)?;
        Ok(())
    }

    pub fn read_cell(&self, level: u32, cell: u32) -> CoreResult<Option<KfbfCell>> {
        let si = self.store_index(level)?;
        if cell >= self.cells[si] {
            return Err(CoreError::index("cell 越界"));
        }
        if self.occupied[si][cell as usize / 64] & (1u64 << (cell as usize % 64)) == 0 {
            return Ok(None);
        }
        let buf = self.stores[si].read_at(cell as u64 * CELL_REC as u64, CELL_REC)?;
        let ptr_offset = u64::from_le_bytes(buf[..8].try_into().unwrap());
        let side_off = u64::from_le_bytes(buf[8..16].try_into().unwrap());
        let jpeg_w = u16::from_le_bytes([buf[16], buf[17]]) as u32;
        let jpeg_h = u16::from_le_bytes([buf[18], buf[19]]) as u32;
        let ta = self.tiles_across[si];
        let row = cell / ta.max(1);
        let col = cell % ta.max(1);
        Ok(Some(KfbfCell {
            level: level as u8,
            x: col * TILE_W,
            y: row * TILE_W,
            jpeg_w,
            jpeg_h,
            ptr_offset,
            side_off,
        }))
    }

    pub fn present_count(&self, level: u32) -> CoreResult<u32> {
        let si = self.store_index(level)?;
        Ok(self.occupied[si].iter().map(|w| w.count_ones()).sum())
    }
}

/// kfb_fl_v1 level canvas dimensions（公式见 vendor_kfbf.level_dimensions）.
pub fn level_dimensions(width: u32, height: u32, level: u32) -> (u32, u32) {
    if level == 0 {
        return (width, height);
    }
    if level <= 3 {
        return ((width >> level).max(1), (height >> level).max(1));
    }
    let w8 = (width + TILE_W - 1) / TILE_W;
    let h8 = (height + TILE_H - 1) / TILE_H;
    if level <= 8 {
        return (w8 << (8 - level), h8 << (8 - level));
    }
    let (mut lw, mut lh) = (w8, h8);
    for _ in 0..(level - 8) {
        lw = (lw >> 1).max(1);
        lh = (lh >> 1).max(1);
    }
    (lw, lh)
}

fn le_u32(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}
fn le_u64(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}

/// Parse entry point (port of `vendor_kfbf.parse_kfbf`).
pub fn parse_kfbf(
    src: &dyn ByteSource,
    scratch: &mut dyn ScratchFactory,
) -> CoreResult<KfbfDocument> {
    let size = src.size();
    if size < 96 {
        return Err(CoreError::header(format!("文件过小（{size} < 96）")));
    }
    let head = src.read_at(0, 0x5C as usize)?;
    if head[0..8] != KFBF_MAGIC {
        return Err(CoreError::variant("未知 KFBF magic"));
    }
    let version = le_u32(&head, 0x08);
    if version != 0 {
        return Err(CoreError::variant(format!(
            "不支持的 KFBF version={version}（仅支持 0）"
        )));
    }
    let fmt = f32::from_bits(le_u32(&head, 0x0C));
    if (fmt - FORMAT_VERSION).abs() > FORMAT_VERSION_EPS {
        return Err(CoreError::variant(format!(
            "不支持的 KFBF 格式版本 {fmt}（标定 {FORMAT_VERSION}）"
        )));
    }
    let tile_count = le_u32(&head, 0x10);
    let height = le_u32(&head, 0x14);
    let width = le_u32(&head, 0x18);
    let objective = le_u32(&head, 0x1C);
    if !(MIN_TILE_COUNT..=MAX_TILE_COUNT).contains(&tile_count) {
        return Err(CoreError::header(format!("tile_count={tile_count} 越界")));
    }
    if !(1..=MAX_DIM_PX).contains(&width) || !(1..=MAX_DIM_PX).contains(&height) {
        return Err(CoreError::header(format!("宽/高越界 ({width},{height})")));
    }
    if !(1..=100).contains(&objective) {
        return Err(CoreError::header(format!("objective={objective} 非法")));
    }
    if &head[0x20..0x24] != b"JPEG" {
        return Err(CoreError::variant("非 JPEG 荧光 KFBF"));
    }
    let scanned_at = le_u32(&head, 0x2C);
    let overview_off = le_u32(&head, 0x34) as u64;
    let label_off = le_u32(&head, 0x38) as u64;
    let index_offset = le_u64(&head, 0x44);
    let mpp = f32::from_bits(le_u32(&head, 0x4C)) as f64;
    if !mpp.is_finite() || mpp <= 0.0 {
        return Err(CoreError::header(format!("mpp={mpp} 非法")));
    }
    if le_u32(&head, 0x58) != TILE_W {
        return Err(CoreError::header("tile 尺寸必须为 256"));
    }
    let index_bytes = (tile_count as u64)
        .checked_mul(TILE_REC)
        .ok_or_else(|| CoreError::index("index_bytes 乘法溢出"))?;
    if index_offset < 96 || index_offset + index_bytes > size {
        return Err(CoreError::index(format!("index_offset={index_offset} 非法")));
    }

    let tags = parse_tagged(src, size)?;
    let channels = parse_channels(src, size, &tags)?;
    let nch = channels.len();

    // ---- tile 索引（pass 1：记录结构与几何） ---------------------------- //
    struct RawRec {
        level: u32,
        x: u32,
        y: u32,
        jpeg_w: u32,
        jpeg_h: u32,
        ptr: u64,
        side: u64,
        len_ch0: u32,
    }
    let mut recs: Vec<RawRec> = Vec::new();
    let mut max_level: i64 = -1;
    {
        let mut pager = crate::pagereader::PageReader::new(src);
        let region_end = index_offset + index_bytes;
        const PAGE_ENTRIES: u64 = 512;
        for i in 0..tile_count as u64 {
            let rec_off = index_offset + i * TILE_REC;
            let want =
                ((PAGE_ENTRIES * TILE_REC) as u64).min(region_end - rec_off) as usize;
            if !pager.covers(rec_off, TILE_REC) {
                pager.ensure(rec_off, want)?;
            }
            let e = pager.slice(rec_off, TILE_REC as usize);
            if e[0..4] != [0xF1, 0x04, 0xEE, 0xEE]
                || e[60..64] != [0xFF, 0x04, 0xEE, 0xEE]
            {
                return Err(CoreError::index(format!("tile[{i}] 记录 magic 非法")));
            }
            let x = le_u32(e, 4);
            let y = le_u32(e, 8);
            let jpeg_w = le_u32(e, 12);
            let jpeg_h = le_u32(e, 16);
            let scale = f32::from_bits(le_u32(e, 20));
            if e[24..32].iter().any(|&b| b != 0) {
                return Err(CoreError::index(format!("tile[{i}] 保留字段非零")));
            }
            if !scale.is_finite() || scale <= 0.0 {
                return Err(CoreError::index(format!("tile[{i}] scale={scale}")));
            }
            let level = ((objective as f64) / (scale as f64)).log2().round() as i64;
            if level < 0
                || level > MAX_LEVEL_COUNT as i64
                || (objective as f64 / 2f64.powi(level as i32) - scale as f64).abs()
                    > (1e-7f64).max(scale as f64 * 1e-5)
            {
                return Err(CoreError::index(format!(
                    "tile[{i}] scale={scale} 不是 objective/2^level"
                )));
            }
            let level = level as u32;
            let (lw, lh) = level_dimensions(width, height, level);
            if x % TILE_W != 0 || y % TILE_H != 0 {
                return Err(CoreError::index(format!(
                    "tile[{i}] 坐标未按 {TILE_W} 网格对齐"
                )));
            }
            if !(1..=TILE_W).contains(&jpeg_w) || !(1..=TILE_H).contains(&jpeg_h) {
                return Err(CoreError::index(format!(
                    "tile[{i}] jpeg 尺寸 {jpeg_w}×{jpeg_h} 非法"
                )));
            }
            if x as u64 + jpeg_w as u64 > lw as u64
                || y as u64 + jpeg_h as u64 > lh as u64
            {
                return Err(CoreError::index(format!(
                    "tile[{i}] {jpeg_w}×{jpeg_h} 越出层 {level} 边界 {lw}×{lh}"
                )));
            }
            let len_ch0 = le_u32(e, 32);
            // va / side_off 是 u32（scale 之后 9×u32，同 oracle 的 struct
            // 布局）；96B 指针块内才是 u64 payload 偏移。
            let va = le_u32(e, 36) as u64;
            let side_off = le_u32(e, 44) as u64;
            if le_u32(e, 40) != 0 {
                return Err(CoreError::index(format!("tile[{i}] 哨兵位非零")));
            }
            if e[48..60].iter().any(|&b| b != 0) {
                return Err(CoreError::index(format!("tile[{i}] 保留字段非零")));
            }
            recs.push(RawRec {
                level,
                x,
                y,
                jpeg_w,
                jpeg_h,
                ptr: va,
                side: side_off,
                len_ch0,
            });
            if level as i64 > max_level {
                max_level = level as i64;
            }
        }
    }
    if max_level < 0 {
        return Err(CoreError::index("无任何 tile"));
    }

    let mut levels: Vec<KfbfLevel> = Vec::new();
    let mut levels_present = vec![false; (max_level + 1) as usize];
    for r in &recs {
        levels_present[r.level as usize] = true;
    }
    for lvl in 0..=max_level as u32 {
        let (lw, lh) = level_dimensions(width, height, lvl);
        levels.push(KfbfLevel { level: lvl, width: lw, height: lh });
    }

    // paged cell stores
    let tas: Vec<u32> = levels.iter().map(|l| l.tiles_across()).collect();
    let tds: Vec<u32> = levels.iter().map(|l| l.tiles_down()).collect();
    let mut stores: Vec<Box<dyn crate::io::ScratchSink>> = Vec::new();
    let mut occupied: Vec<Vec<u64>> = Vec::new();
    let mut cell_counts = Vec::new();
    for (i, lv) in levels.iter().enumerate() {
        let cells = tas[i] as u64 * tds[i] as u64;
        let cells =
            u32::try_from(cells).map_err(|_| CoreError::index("grid cells 超出 u32"))?;
        let mut sink = scratch.create(&format!("kfbf-cells-l{}", lv.level))?;
        sink.truncate(cells as u64 * CELL_REC as u64)?;
        stores.push(sink);
        occupied.push(vec![0u64; cells as usize / 64 + 1]);
        cell_counts.push(cells);
    }
    let mut cells_store = PagedCells {
        stores,
        occupied,
        cells: cell_counts,
        levels: levels.iter().map(|l| l.level).collect(),
        tiles_across: tas,
    };

    // pass 2：side/指针块/payload 校验 + 散落写盘 + extent 统计
    let mut extents: Vec<(u32, u32)> = vec![(0, 0); levels.len()];
    for (i, r) in recs.iter().enumerate() {
        check_payload(size, r.side as i64, (nch * 8) as u32)?;
        let side = src.read_at(r.side, nch * 8)?;
        let len0 = le_u64(&side, 0);
        if len0 != r.len_ch0 as u64 {
            return Err(CoreError::index(format!(
                "tile[{i}] len_ch0={} 与 side[0]={len0} 不一致",
                r.len_ch0
            )));
        }
        for c in 0..nch {
            let ln = le_u64(&side, c * 8);
            if !(MIN_PAYLOAD_LENGTH as u64..=MAX_PAYLOAD_LENGTH as u64).contains(&ln) {
                return Err(CoreError::index(format!(
                    "tile[{i}] 通道 {c} 长度 {ln} 非法"
                )));
            }
        }
        check_payload(size, r.ptr as i64, PTR_BLOCK_BYTES as u32)?;
        if nch * 8 > PTR_BLOCK_BYTES as usize {
            return Err(CoreError::index("channel_count 超出指针块容量"));
        }
        let ptrs = src.read_at(r.ptr, nch * 8)?;
        for c in 0..nch {
            let off = le_u64(&ptrs, c * 8);
            let len = le_u64(&side, c * 8) as u32;
            let (ok_end, _) = check_payload(size, off as i64, len)?;
            check_jpeg_bounds(src, ok_end, len)?;
        }
        let si = levels.iter().position(|l| l.level == r.level).unwrap();
        let (ew, eh) = extents[si];
        extents[si] = (ew.max(r.x + r.jpeg_w), eh.max(r.y + r.jpeg_h));
        cells_store.put(KfbfCell {
            level: r.level as u8,
            x: r.x,
            y: r.y,
            jpeg_w: r.jpeg_w,
            jpeg_h: r.jpeg_h,
            ptr_offset: r.ptr,
            side_off: r.side,
        })?;
    }
    // 层存在性 + L>=1 extent 必须与公式精确一致
    for (si, lv) in levels.iter().enumerate() {
        if !levels_present[si] {
            return Err(CoreError::validation(format!(
                "层 {} 在索引中无任何 tile",
                lv.level
            )));
        }
        if lv.level == 0 {
            continue;
        }
        let (ew, eh) = extents[si];
        if (ew, eh) != (lv.width, lv.height) {
            return Err(CoreError::index(format!(
                "层 {} 网格 extent {ew}×{eh} 与公式 {}×{} 不一致",
                lv.level, lv.width, lv.height
            )));
        }
    }

    let associated = parse_associated(src, size, overview_off, label_off)?;
    let header = KfbfHeader {
        width_px: width,
        height_px: height,
        objective: objective as f64,
        tile_count,
        channel_count: nch,
        mpp,
        scanner_id: scanner_id_of(&tags),
        index_offset,
        scanned_at,
    };
    Ok(KfbfDocument {
        header,
        channels,
        levels,
        cells: cells_store,
        associated,
        source_size: size,
    })
}

fn parse_tagged(src: &dyn ByteSource, size: u64) -> CoreResult<Vec<(u32, Vec<u8>)>> {
    if size < 0x68 {
        return Err(CoreError::header("缺 tagged 段"));
    }
    if src.read_at(0x5C, 4)? != [0xFF, 0x01, 0xEE, 0xEE] {
        return Err(CoreError::header("缺 tagged 段（0x5c ff01eeee）"));
    }
    let count = le_u32(&src.read_at(0x60, 4)?, 0);
    if count > MAX_TAG_COUNT {
        return Err(CoreError::header(format!("tagged 段过长（{count}）")));
    }
    let mut tags = Vec::with_capacity(count as usize);
    let mut off: u64 = 0x64;
    for _ in 0..count {
        if off + 8 > size {
            return Err(CoreError::header("tagged 段截断"));
        }
        let pair = src.read_at(off, 8)?;
        let tag = le_u32(&pair, 0);
        let length = le_u32(&pair, 4);
        off += 8;
        if length > MAX_TAG_VALUE || off + length as u64 > size {
            return Err(CoreError::header("tagged 值越界"));
        }
        let val = src.read_at(off, length as usize)?;
        off += length as u64;
        tags.push((tag, val));
    }
    Ok(tags)
}

fn tag_ptr(
    src: &dyn ByteSource,
    size: u64,
    tags: &[(u32, Vec<u8>)],
    tag: u32,
    want_len: usize,
) -> CoreResult<Vec<u8>> {
    let raw = tags
        .iter()
        .rev()
        .find(|(t, _)| *t == tag)
        .map(|(_, v)| v.clone())
        .ok_or_else(|| CoreError::metadata(format!("tag {tag} 缺失/形态非法")))?;
    if raw.len() != 8 {
        return Err(CoreError::metadata(format!("tag {tag} 缺失/形态非法")));
    }
    let ptr = le_u64(&raw, 0);
    if ptr + want_len as u64 > size {
        return Err(CoreError::header(format!(
            "tag {tag} 数据区 [{ptr},{}) 越界",
            ptr + want_len as u64
        )));
    }
    src.read_at(ptr, want_len)
}

fn parse_channels(
    src: &dyn ByteSource,
    size: u64,
    tags: &[(u32, Vec<u8>)],
) -> CoreResult<Vec<KfbfChannel>> {
    let raw_nch = tags
        .iter()
        .rev()
        .find(|(t, _)| *t == 75)
        .map(|(_, v)| v.clone())
        .ok_or_else(|| CoreError::header("tag 75（channel_count）缺失"))?;
    if raw_nch.len() != 4 {
        return Err(CoreError::header("tag 75（channel_count）缺失"));
    }
    let nch = le_u32(&raw_nch, 0) as usize;
    if !(1..=MAX_CHANNEL_COUNT).contains(&nch) {
        return Err(CoreError::header(format!("channel_count={nch} 越界")));
    }
    const NAME_BYTES: usize = 40;
    const COLOR_BYTES: usize = 12;
    let names = tag_ptr(src, size, tags, 77, nch * NAME_BYTES)?;
    let colors = tag_ptr(src, size, tags, 79, nch * COLOR_BYTES)?;
    let expo = tag_ptr(src, size, tags, 84, nch * 8)?;
    let gammas: Option<Vec<u8>> = tags
        .iter()
        .rev()
        .find(|(t, v)| *t == 87 && v.len() == 8)
        .map(|(_, v)| v.clone());
    let gammas = match gammas {
        Some(raw) => {
            let ptr = le_u64(&raw, 0);
            if ptr + (nch * 8) as u64 <= size {
                Some(src.read_at(ptr, nch * 8)?)
            } else {
                None
            }
        }
        None => None,
    };
    let mut channels = Vec::with_capacity(nch);
    for c in 0..nch {
        let name_raw = &names[c * NAME_BYTES..(c + 1) * NAME_BYTES];
        let name_len = name_raw.iter().position(|&b| b == 0).unwrap_or(NAME_BYTES);
        let name = String::from_utf8_lossy(&name_raw[..name_len]).into_owned();
        if name.is_empty() {
            return Err(CoreError::metadata(format!("通道 {c} 名为空")));
        }
        let r = le_u32(&colors, c * COLOR_BYTES);
        let g = le_u32(&colors, c * COLOR_BYTES + 4);
        let b = le_u32(&colors, c * COLOR_BYTES + 8);
        for v in [r, g, b] {
            if v > 255 {
                return
                    Err(CoreError::header(format!("通道 {c} 颜色分量 {v} 越界")));
            }
        }
        let bytes: [u8; 8] = expo[c * 8..(c + 1) * 8].try_into().unwrap();
        let exposure = f64::from_le_bytes(bytes);
        if !exposure.is_finite() || exposure <= 0.0 {
            return Err(CoreError::metadata(format!(
                "通道 {c} 曝光 {exposure} 非法"
            )));
        }
        let mut gamma = 1.0;
        if let Some(gs) = &gammas {
            let bytes: [u8; 8] = gs[c * 8..(c + 1) * 8].try_into().unwrap();
            let g = f64::from_le_bytes(bytes);
            if g.is_finite() && g > 0.0 {
                gamma = g;
            }
        }
        channels.push(KfbfChannel {
            index: c,
            name,
            color_rgb: (r, g, b),
            exposure,
            gamma,
        });
    }
    Ok(channels)
}

fn parse_associated(
    src: &dyn ByteSource,
    size: u64,
    overview_off: u64,
    label_off: u64,
) -> CoreResult<Vec<KfbfAssociated>> {
    let mut out = Vec::new();
    for (name, rec_off) in [("overview", overview_off), ("label", label_off)] {
        if let Some(item) = parse_inline_assoc(src, size, rec_off) {
            out.push(KfbfAssociated {
                name: name.to_string(),
                payload_offset: item.0,
                payload_length: item.1,
                width: item.2,
                height: item.3,
            });
        }
    }
    if let Some(item) = parse_eof_thumbnail(src, size) {
        out.push(KfbfAssociated {
            name: "thumbnail".to_string(),
            payload_offset: item.0,
            payload_length: item.1,
            width: item.2,
            height: item.3,
        });
    }
    Ok(out)
}

type AssocTuple = (u64, u32, u32, u32);

fn parse_inline_assoc(
    src: &dyn ByteSource,
    size: u64,
    rec_off: u64,
) -> Option<AssocTuple> {
    if rec_off < 96 || rec_off + ASSOC_REC_BYTES as u64 > size {
        return None;
    }
    let hdr = src.read_at(rec_off, ASSOC_REC_BYTES).ok()?;
    if hdr[0..4] != [0xF1, 0x02, 0xEE, 0xEE] && hdr[0..4] != [0xF1, 0x03, 0xEE, 0xEE] {
        return None;
    }
    let tail = &hdr[48..52];
    if tail != [0xFF, 0x02, 0xEE, 0xEE] && tail != [0xFF, 0x03, 0xEE, 0xEE] {
        return None;
    }
    let height = le_u32(&hdr, 8);
    let width = le_u32(&hdr, 12);
    let jlen = le_u32(&hdr, 20);
    let payload_off = rec_off + ASSOC_REC_BYTES as u64;
    if !(1..=200_000).contains(&width)
        || !(1..=200_000).contains(&height)
        || !(MIN_PAYLOAD_LENGTH..=MAX_PAYLOAD_LENGTH).contains(&jlen)
        || payload_off + jlen as u64 > size
    {
        return None;
    }
    if check_jpeg_bounds(src, payload_off, jlen).is_err() {
        return None;
    }
    Some((payload_off, jlen, width, height))
}

fn parse_eof_thumbnail(src: &dyn ByteSource, size: u64) -> Option<AssocTuple> {
    if size < ASSOC_REC_BYTES as u64 {
        return None;
    }
    let rec_off = size - ASSOC_REC_BYTES as u64;
    let hdr = src.read_at(rec_off, ASSOC_REC_BYTES).ok()?;
    if hdr[0..4] != [0xF1, 0x02, 0xEE, 0xEE]
        || hdr[48..52] != [0xFF, 0x02, 0xEE, 0xEE]
    {
        return None;
    }
    let height = le_u32(&hdr, 8);
    let width = le_u32(&hdr, 12);
    let jlen = le_u32(&hdr, 20);
    let p1 = le_u64(&hdr, 24);
    if !(1..=200_000).contains(&width)
        || !(1..=200_000).contains(&height)
        || !(MIN_PAYLOAD_LENGTH..=MAX_PAYLOAD_LENGTH).contains(&jlen)
        || p1 + 8 > size
    {
        return None;
    }
    let payload_off = le_u64(&src.read_at(p1, 8).ok()?, 0);
    if payload_off + jlen as u64 > size {
        return None;
    }
    if check_jpeg_bounds(src, payload_off, jlen).is_err() {
        return None;
    }
    Some((payload_off, jlen, width, height))
}

/// scanner_id via tag 29（ASCII，NUL 截断）。
pub fn scanner_id_of(tags: &[(u32, Vec<u8>)]) -> String {
    tags.iter()
        .rev()
        .find(|(t, _)| *t == 29)
        .map(|(_, v)| {
            let end = v.iter().position(|&b| b == 0).unwrap_or(v.len());
            ascii_replace(&v[..end])
        })
        .unwrap_or_default()
}
