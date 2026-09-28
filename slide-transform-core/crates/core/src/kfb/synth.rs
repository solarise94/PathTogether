//! Synthetic `kfb_bf_v1` contract parser — port of `kfb/parser.py`
//! (`_parse_header` / `_parse_tile_index` / `_parse_associated_index`).
//!
//! Layout (little-endian):
//!   0x00 8s magic, 0x08 u32 version=1, 0x0C u32 header_bytes (96..4096),
//!   0x10 u32 width, 0x14 u32 height, 0x18 u32 tile_w=256, 0x1C u32 tile_h=256,
//!   0x20 u32 level_count, 0x24 u32 tile_count, 0x28 f64 mpp_x, 0x30 f64 mpp_y,
//!   0x38 f32 objective, 0x3C 16s scanner_id, 0x4C u32 associated_count,
//!   0x50 u64 index_offset, 0x58 u32 flags (bit0 brightfield, rest must be 0).
//! Tile index: 32 B entries; associated index: 48 B entries.

use super::*;
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, ScratchFactory};
use crate::paged_index::{PagedGridIndex, TileRec};
use crate::pagereader::PageReader;

const TILE_ENTRY: u64 = 32;
const ASSOC_ENTRY: usize = 48;
const INDEX_PAGE_ENTRIES: usize = 4096;

pub fn parse_synth(
    src: &dyn ByteSource,
    scratch: &mut dyn ScratchFactory,
) -> CoreResult<KfbDocument> {
    let size = src.size();
    let head = src.read_at(0, HEADER_MIN_BYTES as usize)?;

    let version = read_u32(&head, 0x08);
    if version != VERSION {
        return Err(CoreError::variant(format!(
            "不支持的 KFB version={version}（仅支持 {VERSION}）"
        )));
    }
    let header_bytes = read_u32(&head, 0x0C) as u64;
    if !(HEADER_MIN_BYTES..=HEADER_MAX_BYTES).contains(&header_bytes) {
        return Err(CoreError::header(format!(
            "header_bytes={header_bytes} 越界 [{HEADER_MIN_BYTES},{HEADER_MAX_BYTES}]"
        )));
    }
    if header_bytes > size {
        return Err(CoreError::header("header_bytes 超出文件长度"));
    }
    let width = read_u32(&head, 0x10);
    let height = read_u32(&head, 0x14);
    let tile_w = read_u32(&head, 0x18);
    let tile_h = read_u32(&head, 0x1C);
    let level_count = read_u32(&head, 0x20);
    let tile_count = read_u32(&head, 0x24);
    if !(1..=MAX_DIM_PX).contains(&width) || !(1..=MAX_DIM_PX).contains(&height) {
        return Err(CoreError::header(format!("宽/高越界 ({width},{height})")));
    }
    if tile_w != TILE_W || tile_h != TILE_H {
        return Err(CoreError::header(format!(
            "tile 尺寸必须为 {TILE_W}×{TILE_H}（得 {tile_w}×{tile_h}）"
        )));
    }
    if !(1..=MAX_LEVEL_COUNT).contains(&level_count) {
        return Err(CoreError::header(format!("level_count={level_count} 越界")));
    }
    if !(1..=MAX_TILE_COUNT).contains(&tile_count) {
        return Err(CoreError::header(format!("tile_count={tile_count} 越界")));
    }
    let mpp_x = read_f64(&head, 0x28);
    let mpp_y = read_f64(&head, 0x30);
    let objective = read_f32(&head, 0x38) as f64;
    let scanner_raw = &head[0x3C..0x4C];
    let associated_count = read_u32(&head, 0x4C);
    if associated_count > MAX_ASSOCIATED_COUNT {
        return Err(CoreError::header(format!(
            "associated_count={associated_count} 越界"
        )));
    }
    let index_offset = read_u64(&head, 0x50);
    let flags = read_u32(&head, 0x58);
    if flags & !0x1 != 0 {
        return Err(CoreError::header(format!("flags=0x{flags:x} 含未定义位")));
    }
    if !mpp_x.is_finite() || mpp_x <= 0.0 {
        return Err(CoreError::header(format!("mpp_x={mpp_x} 非法")));
    }
    if !mpp_y.is_finite() || mpp_y <= 0.0 {
        return Err(CoreError::header(format!("mpp_y={mpp_y} 非法")));
    }
    if !objective.is_finite() || objective < 0.0 {
        return Err(CoreError::header(format!("objective={objective} 非法")));
    }
    let scanner_id = ascii_replace(scanner_raw);
    if index_offset < header_bytes || index_offset >= size {
        return Err(CoreError::index(format!("index_offset={index_offset} 非法")));
    }
    let index_end = index_offset
        .checked_add((tile_count as u64) * TILE_ENTRY)
        .and_then(|v| v.checked_add((associated_count as u64) * ASSOC_ENTRY as u64))
        .ok_or_else(|| CoreError::index("索引区大小加法溢出"))?;
    if index_end > size {
        return Err(CoreError::index("索引区超出文件长度"));
    }

    let header = KfbHeader {
        version,
        width_px: width,
        height_px: height,
        tile_w,
        tile_h,
        level_count,
        tile_count,
        mpp_x,
        mpp_y,
        objective,
        scanner_id,
        associated_count,
        index_offset,
        brightfield: flags & 0x1 != 0,
    };

    // 层级几何：level L 尺寸 = level0 按 2^L 向下取整（奇数边 floor）
    let levels: Vec<KfbLevel> = (0..level_count)
        .map(|lvl| KfbLevel {
            level: lvl,
            width: (width >> lvl).max(1),
            height: (height >> lvl).max(1),
        })
        .collect();

    let level_dims: Vec<(u32, u32, u32)> =
        levels.iter().map(|l| (l.level, l.width, l.height)).collect();
    let tas: Vec<u32> = levels.iter().map(|l| l.tiles_across()).collect();
    let tds: Vec<u32> = levels.iter().map(|l| l.tiles_down()).collect();
    let mut grids = PagedGridIndex::create(&level_dims, &tas, &tds, scratch)?;
    let level_by_idx: std::collections::HashMap<u32, &KfbLevel> =
        levels.iter().map(|l| (l.level, l)).collect();

    // pass 1：校验每个索引条目并按网格 cell 散落写盘（与 Python 逐条校验同序）
    let mut pager = PageReader::new(src);
    let region_end = index_offset + (tile_count as u64) * TILE_ENTRY;
    for i in 0..tile_count as u64 {
        let rec_off = index_offset + i * TILE_ENTRY;
        let want = ((INDEX_PAGE_ENTRIES as u64) * TILE_ENTRY)
            .min(region_end - rec_off) as usize;
        pager.ensure(rec_off, want)?;
        let e = pager.slice(rec_off, TILE_ENTRY as usize);
        let lvl = read_u32(e, 0);
        let x_px = read_u32(e, 4);
        let y_px = read_u32(e, 8);
        let jpeg_w = read_u16(e, 12);
        let jpeg_h = read_u16(e, 14);
        let payload_offset = read_u64(e, 16);
        let payload_length = read_u32(e, 24);
        let reserved = read_u32(e, 28);

        let lv = level_by_idx.get(&lvl).ok_or_else(|| {
            CoreError::index(format!(
                "tile[{i}] level={lvl} 越界（level_count={level_count}）"
            ))
        })?;
        if x_px % TILE_W != 0 || y_px % TILE_H != 0 {
            return Err(CoreError::index(format!(
                "tile[{i}] 坐标未按 {TILE_W} 网格对齐"
            )));
        }
        if jpeg_w < 1 || jpeg_w > 256 || jpeg_h < 1 || jpeg_h > 256 {
            return Err(CoreError::index(format!(
                "tile[{i}] jpeg 尺寸 {jpeg_w}×{jpeg_h} 非法"
            )));
        }
        if x_px as u64 + jpeg_w as u64 > lv.width as u64
            || y_px as u64 + jpeg_h as u64 > lv.height as u64
        {
            return Err(CoreError::index(format!(
                "tile[{i}] 尺寸 {jpeg_w}×{jpeg_h} 越出层 {lvl} 边界 {}×{}",
                lv.width, lv.height
            )));
        }
        if !(MIN_PAYLOAD_LENGTH..=MAX_PAYLOAD_LENGTH).contains(&payload_length) {
            return Err(CoreError::index(format!(
                "tile[{i}] payload_length={payload_length} 非法"
            )));
        }
        if reserved != 0 {
            return Err(CoreError::index(format!("tile[{i}] reserved!=0")));
        }
        grids.put(TileRec {
            level: lvl as u8,
            x: x_px,
            y: y_px,
            jpeg_w,
            jpeg_h,
            payload_offset,
            payload_length,
        })?;
    }

    // pass 2：全部条目结构合法后，按条目顺序做 payload 边界与 SOI/EOI 检查
    // （与 Python 的第二循环同序；索引错误优先于 payload 错误）
    let mut pager2 = PageReader::new(src);
    for i in 0..tile_count as u64 {
        let rec_off = index_offset + i * TILE_ENTRY;
        let want =
            ((INDEX_PAGE_ENTRIES as u64) * TILE_ENTRY).min(region_end - rec_off) as usize;
        pager2.ensure(rec_off, want)?;
        let e = pager2.slice(rec_off, TILE_ENTRY as usize);
        let payload_offset = read_u64(e, 16);
        let payload_length = read_u32(e, 24);
        let (off, _) = check_payload(size, payload_offset as i64, payload_length)?;
        check_jpeg_bounds(src, off, payload_length)?;
    }

    // associated 索引
    let assoc_offset = index_offset + (tile_count as u64) * TILE_ENTRY;
    let mut associated = Vec::new();
    if associated_count > 0 {
        let blob = src.read_at(assoc_offset, associated_count as usize * ASSOC_ENTRY)?;
        for i in 0..associated_count as usize {
            let base = i * ASSOC_ENTRY;
            let e = &blob[base..base + ASSOC_ENTRY];
            let name_raw = &e[0..16];
            let payload_offset = read_u64(e, 16);
            let payload_length = read_u32(e, 24);
            let width = read_u16(e, 28);
            let height = read_u16(e, 30);
            let reserved = read_u32(e, 32);
            let name = ascii_replace(name_raw);
            if !["label", "overview", "thumbnail"].contains(&name.as_str()) {
                return Err(CoreError::header(format!(
                    "associated[{i}] 名 {name:?} 不在 [label, overview, thumbnail]"
                )));
            }
            if reserved != 0 {
                return Err(CoreError::header(format!(
                    "associated[{i}] reserved!=0"
                )));
            }
            if width < 1
                || height < 1
                || width as u32 > MAX_DIM_PX
                || height as u32 > MAX_DIM_PX
            {
                return Err(CoreError::header(format!(
                    "associated[{i}] 尺寸 {width}×{height} 非法"
                )));
            }
            if !(MIN_PAYLOAD_LENGTH..=MAX_PAYLOAD_LENGTH).contains(&payload_length) {
                return Err(CoreError::header(format!(
                    "associated[{i}] payload_length={payload_length} 非法"
                )));
            }
            associated.push(KfbAssociated {
                name,
                payload_offset,
                payload_length,
                width: width as u32,
                height: height as u32,
            });
        }
    }
    for item in &associated {
        let (off, _) = check_payload(size, item.payload_offset as i64, item.payload_length)?;
        check_jpeg_bounds(src, off, item.payload_length)?;
    }

    Ok(KfbDocument { header, levels, grids, associated, source_size: size })
}
