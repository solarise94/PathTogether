//! KF-BIO (江丰/KFBIO) brightfield vendor layout parser — port of
//! `kfb/vendor_kfbio.py`. Same magic as the synthetic contract but a tagged
//! header + 64 B tile records; unknown variants fail closed.
//!
//! Layout (little-endian):
//!   0x00 8s magic, 0x08 u32 version (!= 1 for this layout),
//!   0x10 u32 tile_count, 0x14 u32 height_px, 0x18 u32 width_px,
//!   0x1C u32 objective (integer magnification), 0x20 4s codec "JPEG",
//!   0x44 u64 tile_index_offset, 0x4C f32 mpp, 0x58 u32 tile_w=tile_h=256.
//! Scanner id: tagged segment `ff01eeee` at 0x5C, tag 29 (16s ASCII).
//! Tile record (64 B): f104eeee, u32 x, y, jpeg_w, jpeg_h, f32 scale,
//!   u32 z0=0, z1=0, u32 payload_length, i32 va (phys = index_offset + va),
//!   u32 0xffffffff, u32 20×4, ff04eeee.
//! Associated: f102/f103 records (52 B head + JPEG) found by scanning the
//! file; mapped by area to overview/label/thumbnail.

use super::*;
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, ScratchFactory};
use crate::paged_index::{PagedGridIndex, TileRec};
use crate::pagereader::PageReader;

const TILE_REC: u64 = 64;
const INDEX_PAGE_ENTRIES: usize = 1024;

const TILE_HEAD: [u8; 4] = [0xF1, 0x04, 0xEE, 0xEE];
const TILE_TAIL: [u8; 4] = [0xFF, 0x04, 0xEE, 0xEE];
const ASSOC_MAGICS: [[u8; 4]; 2] = [[0xF1, 0x02, 0xEE, 0xEE], [0xF1, 0x03, 0xEE, 0xEE]];

/// `looks_like_vendor`：magic 已匹配后，version≠1 且 codec=JPEG、tile=256
/// 视为厂商布局。
pub fn looks_like_vendor(head96: &[u8]) -> bool {
    let version = read_u32(head96, 0x08);
    if version == 1 {
        return false;
    }
    let codec = &head96[0x20..0x24];
    let tile = read_u32(head96, 0x58);
    codec == b"JPEG" && tile == TILE_W
}

pub fn parse_vendor(
    src: &dyn ByteSource,
    scratch: &mut dyn ScratchFactory,
) -> CoreResult<KfbDocument> {
    let size = src.size();
    let head = src.read_at(0, HEADER_MIN_BYTES as usize)?;

    let tile_count = read_u32(&head, 0x10);
    let height = read_u32(&head, 0x14);
    let width = read_u32(&head, 0x18);
    let objective = read_u32(&head, 0x1C);
    if !(1..=MAX_TILE_COUNT).contains(&tile_count) {
        return Err(CoreError::header(format!("tile_count={tile_count} 越界")));
    }
    if !(1..=MAX_DIM_PX).contains(&width) || !(1..=MAX_DIM_PX).contains(&height) {
        return Err(CoreError::header(format!("宽/高越界 ({width},{height})")));
    }
    if !(1..=100).contains(&objective) {
        return Err(CoreError::header(format!("objective={objective} 非法")));
    }
    if &head[0x20..0x24] != b"JPEG" {
        return Err(CoreError::variant("非 JPEG 明场 KFB"));
    }
    let tile_size = read_u32(&head, 0x58);
    if tile_size != TILE_W {
        return Err(CoreError::header(format!("tile 尺寸必须为 {TILE_W}")));
    }
    let mpp = read_f32(&head, 0x4C);
    if !mpp.is_finite() || mpp <= 0.0 {
        return Err(CoreError::header(format!("mpp={mpp} 非法")));
    }
    let index_offset = read_u64(&head, 0x44);
    let index_bytes = (tile_count as u64)
        .checked_mul(TILE_REC)
        .ok_or_else(|| CoreError::index("index_bytes 乘法溢出"))?;
    if index_offset < HEADER_MIN_BYTES || index_offset + index_bytes > size {
        return Err(CoreError::index(format!("index_offset={index_offset} 非法")));
    }

    let scanner_id = read_scanner_id(src, size)?;
    let useful_levels = pyramid_levels(width, height);
    let level_count = useful_levels.len() as u32;
    if !(1..=MAX_LEVEL_COUNT).contains(&level_count) {
        return Err(CoreError::header(format!("level_count={level_count} 越界")));
    }

    let level_dims: Vec<(u32, u32, u32)> = useful_levels
        .iter()
        .map(|l| (l.level, l.width, l.height))
        .collect();
    let tas: Vec<u32> = useful_levels.iter().map(|l| l.tiles_across()).collect();
    let tds: Vec<u32> = useful_levels.iter().map(|l| l.tiles_down()).collect();
    let mut grids = PagedGridIndex::create(&level_dims, &tas, &tds, scratch)?;
    let level_by_idx: std::collections::HashMap<u32, &KfbLevel> =
        useful_levels.iter().map(|l| (l.level, l)).collect();

    let mut pager = PageReader::new(src);
    let region_end = index_offset + index_bytes;
    for i in 0..tile_count as u64 {
        let rec_off = index_offset + i * TILE_REC;
        let want = ((INDEX_PAGE_ENTRIES as u64) * TILE_REC).min(region_end - rec_off) as usize;
        if !pager.covers(rec_off, TILE_REC) {
            pager.ensure(rec_off, want)?;
        }
        let e = pager.slice(rec_off, TILE_REC as usize);
        let x_px = read_u32(e, 4);
        let y_px = read_u32(e, 8);
        let jpeg_w = read_u32(e, 12);
        let jpeg_h = read_u32(e, 16);
        let scale = read_f32(e, 20);
        let z0 = read_u32(e, 24);
        let z1 = read_u32(e, 28);
        let length = read_u32(e, 32);
        let va = read_u32(e, 36) as i32;
        let sent = read_u32(e, 40);
        let tail = &e[60..64];
        if e[0..4] != TILE_HEAD || tail != TILE_TAIL {
            return Err(CoreError::index(format!("tile[{i}] 记录 magic 非法")));
        }
        if z0 != 0 || z1 != 0 || sent != 0xFFFFFFFF {
            return Err(CoreError::index(format!("tile[{i}] 保留字段非法")));
        }
        if !scale.is_finite() || scale <= 0.0 {
            return Err(CoreError::index(format!("tile[{i}] scale={scale}")));
        }
        let level = ((objective as f64) / (scale as f64)).log2().round() as i64;
        let level = u32::try_from(level).ok().filter(|l| level_by_idx.contains_key(l));
        let lv = match level {
            Some(l) => level_by_idx[&l],
            None => continue, // 1×1 之后的冗余单 tile 层不纳入 canonical
        };
        if x_px % TILE_W != 0 || y_px % TILE_H != 0 {
            return Err(CoreError::index(format!("tile[{i}] 坐标未按网格对齐")));
        }
        if !(1..=TILE_W).contains(&jpeg_w) || !(1..=TILE_H).contains(&jpeg_h) {
            return Err(CoreError::index(format!(
                "tile[{i}] jpeg 尺寸 {jpeg_w}×{jpeg_h} 非法"
            )));
        }
        if x_px as u64 + jpeg_w as u64 > lv.width as u64
            || y_px as u64 + jpeg_h as u64 > lv.height as u64
        {
            return Err(CoreError::index(format!(
                "tile[{i}] 越出层 {} 边界",
                lv.level
            )));
        }
        if !(MIN_PAYLOAD_LENGTH..=MAX_PAYLOAD_LENGTH).contains(&length) {
            return Err(CoreError::index(format!(
                "tile[{i}] payload_length={length} 非法"
            )));
        }
        let phys = index_offset as i64 + va as i64;
        let (off, _) = check_payload(size, phys, length)?;
        check_jpeg_bounds(src, off, length)?;
        grids.put(TileRec {
            level: lv.level as u8,
            x: x_px,
            y: y_px,
            jpeg_w: jpeg_w as u16,
            jpeg_h: jpeg_h as u16,
            payload_offset: off,
            payload_length: length,
        })?;
    }

    let associated = parse_associated(src, size)?;
    let header = KfbHeader {
        version: 0,
        width_px: width,
        height_px: height,
        tile_w: TILE_W,
        tile_h: TILE_H,
        level_count,
        tile_count: grids.total_kept as u32,
        mpp_x: mpp as f64,
        mpp_y: mpp as f64,
        objective: objective as f64,
        scanner_id,
        associated_count: associated.len() as u32,
        index_offset,
        brightfield: true,
    };
    Ok(KfbDocument {
        header,
        levels: useful_levels,
        grids,
        associated,
        source_size: size,
    })
}

/// 层级：从 level0 逐层减半直到（含）首个 1×1 网格层，最多 16 层。
fn pyramid_levels(width: u32, height: u32) -> Vec<KfbLevel> {
    let mut levels = Vec::new();
    let (mut w, mut h) = (width, height);
    for lvl in 0..MAX_LEVEL_COUNT {
        let lv = KfbLevel { level: lvl, width: w.max(1), height: h.max(1) };
        levels.push(lv);
        if lv.tiles_across() == 1 && lv.tiles_down() == 1 {
            break;
        }
        w = (w >> 1).max(1);
        h = (h >> 1).max(1);
    }
    levels
}

/// tagged 段 ff01eeee 中 tag 29（16s ASCII）。缺失则空串。
fn read_scanner_id(src: &dyn ByteSource, size: u64) -> CoreResult<String> {
    if size < 0x70 {
        return Ok(String::new());
    }
    if src.read_at(0x5C, 4)? != [0xFF, 0x01, 0xEE, 0xEE] {
        return Ok(String::new());
    }
    let count = read_u32(&src.read_at(0x60, 4)?, 0);
    if count > 32 {
        return Err(CoreError::header("tagged 段过长"));
    }
    let mut off: u64 = 0x64;
    let mut scanner = String::new();
    for _ in 0..count {
        if off + 8 > size {
            return Err(CoreError::header("tagged 段截断"));
        }
        let pair = src.read_at(off, 8)?;
        let tag = read_u32(&pair, 0);
        let length = read_u32(&pair, 4);
        off += 8;
        if length > 256 || off + length as u64 > size {
            return Err(CoreError::header("tagged 值越界"));
        }
        let val = src.read_at(off, length as usize)?;
        off += length as u64;
        if tag == 29 {
            let end = val.iter().position(|&b| b == 0).unwrap_or(val.len());
            scanner = ascii_replace(&val[..end]);
        }
    }
    Ok(scanner)
}

/// 扫描 f102/f103 记录。按面积：最大 overview、次大 label、最小 thumbnail。
fn parse_associated(src: &dyn ByteSource, size: u64) -> CoreResult<Vec<KfbAssociated>> {
    let mut found: Vec<(u64, u32, u32, u64, u32)> = Vec::new();
    let mut seen: std::collections::HashSet<u64> = std::collections::HashSet::new();
    for magic in &ASSOC_MAGICS {
        let mut pos: u64 = 0;
        loop {
            let Some(i) = find_magic(src, size, magic, pos)? else { break };
            pos = i + 4;
            if i + 52 > size {
                continue;
            }
            let hdr = src.read_at(i, 24)?;
            // magic(4) id(4) height(4) width(4) type(4) jpeg_len(4)
            let height = read_u32(&hdr, 8);
            let width = read_u32(&hdr, 12);
            let jpeg_len = read_u32(&hdr, 20);
            let jpeg_off = i + 52;
            if seen.contains(&jpeg_off) {
                continue;
            }
            let dims_ok = (1..=MAX_DIM_PX).contains(&width)
                && (1..=MAX_DIM_PX).contains(&height)
                && (MIN_PAYLOAD_LENGTH..=MAX_PAYLOAD_LENGTH).contains(&jpeg_len)
                && jpeg_off + jpeg_len as u64 <= size;
            if !dims_ok {
                continue;
            }
            if check_jpeg_bounds(src, jpeg_off, jpeg_len).is_err() {
                continue;
            }
            seen.insert(jpeg_off);
            found.push((width as u64 * height as u64, width, height, jpeg_off, jpeg_len));
            if found.len() >= 8 {
                break;
            }
        }
    }
    // 稳定排序按面积降序（与 Python sort(key=area, reverse=True) 同语义）
    found.sort_by(|a, b| b.0.cmp(&a.0));
    let names = ["overview", "label", "thumbnail"];
    // 3 张：大/中/小；不足则按顺序截断
    let mut picked: Vec<&(u64, u32, u32, u64, u32)> = found.iter().take(2).collect();
    if found.len() >= 3 {
        picked.push(found.last().unwrap());
    }
    Ok(picked
        .iter()
        .enumerate()
        .map(|(i, item)| KfbAssociated {
            name: names[i].to_string(),
            payload_offset: item.3,
            payload_length: item.4,
            width: item.1,
            height: item.2,
        })
        .collect())
}

/// 分块扫描 4 字节 magic（等价 `mmap.find`，含跨块边界）。
fn find_magic(
    src: &dyn ByteSource,
    size: u64,
    magic: &[u8; 4],
    from: u64,
) -> CoreResult<Option<u64>> {
    const CHUNK: u64 = 4 << 20;
    let mut base = from;
    let mut tail: Vec<u8> = Vec::new();
    while base < size {
        let len = CHUNK.min(size - base) as usize;
        let chunk = src.read_at(base, len)?;
        let mut buf = std::mem::take(&mut tail);
        buf.extend_from_slice(&chunk);
        let buf_base = base - (buf.len() - chunk.len()) as u64;
        let mut i = 0usize;
        while i + 4 <= buf.len() {
            if &buf[i..i + 4] == magic && buf_base + i as u64 >= from {
                return Ok(Some(buf_base + i as u64));
            }
            i += 1;
        }
        let cut = buf.len().saturating_sub(3);
        tail = buf[cut..].to_vec();
        base += len as u64;
    }
    Ok(None)
}
