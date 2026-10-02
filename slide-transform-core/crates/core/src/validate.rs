//! Post-finalize validation (C2): stream the finished output back through
//! a [`ByteSource`] and check (a) whole-file sha256 and (b) a bounded
//! structural walk of the BigTIFF/OME-BigTIFF IFD graph, before the runner
//! may mark a job `ready`. Nothing is buffered whole-file; every read is
//! bounded and offsets are u64.

use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;

/// Reads bounded chunks (≤1 MiB) so a wasm host never sees a huge request.
const CHUNK: usize = 1 << 20;
/// Defensive caps for the structural walk.
const MAX_IFDS: usize = 4096;
const MAX_ENTRIES: usize = 256;

#[derive(Debug, Clone)]
pub struct ValidationOutcome {
    pub sha256: String,
    pub size: u64,
    /// Every walked IFD (main chain + SubIFDs) — the converter's
    /// `validation.ifd_count` for all profiles.
    pub ifd_count: usize,
    pub main_ifds: usize,
    pub sub_ifds: usize,
    pub tile_records: u64,
    pub checks: Vec<&'static str>,
}

fn rd_u16(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}
fn rd_u64(b: &[u8], at: usize) -> u64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[at..at + 8]);
    u64::from_le_bytes(a)
}

/// Streamed sha256 over `[0, size)` in bounded chunks. The remaining-length
/// clamp stays in u64 until the final (always ≤ CHUNK) cast — a direct
/// `as usize` truncates to 0 once `size - off ≥ 2^32` on wasm32, making
/// `want = 0` and spinning the loop forever (found by the C2 ≥4 GiB runs).
pub fn stream_sha256(src: &dyn ByteSource, size: u64) -> CoreResult<String> {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    let mut off = 0u64;
    while off < size {
        let remaining = size - off;
        let want = (CHUNK as u64).min(remaining) as usize; // ≤ CHUNK
        let buf = src.read_at(off, want)?;
        hasher.update(&buf);
        off += want as u64;
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// Result of the structural walk.
#[derive(Debug, Clone, Default)]
pub struct StructureSummary {
    /// IFDs reached through the main next-IFD chain.
    pub main_ifds: usize,
    /// IFDs reached only through SubIFDs (tag 330).
    pub sub_ifds: usize,
    /// Tile records over every walked IFD.
    pub tile_records: u64,
}

/// One parsed IFD: what the walk needs to continue and to check tiles.
struct IfdInfo {
    next: u64,
    subifds: Vec<u64>,
    subfile_type: u64,
    tile_records: u64,
}

/// Read one IFD at `at`: entry table bounds, TileOffsets/TileByteCounts
/// array and per-tile bounds, SubIFD offsets (tag 330, LONG/IFD or
/// LONG8/IFD8), NewSubfileType (tag 254).
fn read_ifd(src: &dyn ByteSource, size: u64, at: u64) -> CoreResult<IfdInfo> {
    if at.checked_add(8).is_none_or(|e| e > size) {
        return Err(CoreError::validation(format!("IFD 偏移 {at} 越界")));
    }
    // check the u64 BEFORE any usize cast (wasm32 truncation would
    // otherwise let 2^32+n masquerade as a small entry count)
    let n64 = rd_u64(&src.read_at(at, 8)?, 0);
    if n64 == 0 || n64 > MAX_ENTRIES as u64 {
        return Err(CoreError::validation(format!("IFD 条目数 {n64} 异常")));
    }
    let n = n64 as usize;
    let ifd_body = at + 8;
    let table_len = n * 20;
    let table_end = ifd_body
        .checked_add(table_len as u64)
        .and_then(|e| e.checked_add(8))
        .ok_or_else(|| CoreError::validation("IFD 表长度溢出"))?;
    if table_end > size {
        return Err(CoreError::validation("IFD 表越界"));
    }
    let mut off_buf: Option<(u64, u32)> = None; // (array offset, count) tag 324
    let mut cnt_buf: Option<(u64, u32)> = None; // tag 325
    let mut sub_buf: Option<(u16, u64, u64)> = None; // tag 330 (type, count, value)
    let mut subfile_type = 0u64;
    let mut pos = ifd_body;
    let mut remaining = n;
    while remaining > 0 {
        let take = remaining.min(64);
        let buf = src.read_at(pos, take * 20)?;
        for i in 0..take {
            let e = i * 20;
            let tag = rd_u16(&buf, e);
            let typ = rd_u16(&buf, e + 2);
            let count = rd_u64(&buf, e + 4);
            let val = rd_u64(&buf, e + 12);
            if count > u32::MAX as u64 {
                return Err(CoreError::validation(format!("tag {tag} count 溢出")));
            }
            match tag {
                254 => subfile_type = val & 0xFFFF_FFFF,
                324 => off_buf = Some((val, count as u32)),
                325 => cnt_buf = Some((val, count as u32)),
                330 => sub_buf = Some((typ, count, val)),
                _ => {}
            }
        }
        pos += take as u64 * 20;
        remaining -= take;
    }
    let mut tile_records = 0u64;
    if let (Some((oa, oc)), Some((ca, cc))) = (off_buf, cnt_buf) {
        if oc != cc {
            return Err(CoreError::validation("TileOffsets/TileByteCounts count 不一致"));
        }
        // inline (oc <= 1) values sit in the value field; arrays are external
        let read_arr = |base: u64, cnt: u32, inline: u64| -> CoreResult<Vec<u64>> {
            if cnt == 0 {
                return Ok(Vec::new());
            }
            if cnt == 1 {
                return Ok(vec![inline]);
            }
            let end = base
                .checked_add(cnt as u64 * 8)
                .ok_or_else(|| CoreError::validation("tile 数组长度溢出"))?;
            if end > size {
                return Err(CoreError::validation("tile 数组越界"));
            }
            let mut out = Vec::with_capacity(cnt as usize);
            let mut o = base;
            while o < end {
                out.push(rd_u64(&src.read_at(o, 8)?, 0));
                o += 8;
            }
            Ok(out)
        };
        let offs = read_arr(oa, oc, oa)?;
        let cnts = read_arr(ca, cc, ca)?;
        tile_records += oc as u64;
        for (i, &to) in offs.iter().enumerate() {
            let tc = cnts[i];
            let end = to
                .checked_add(tc)
                .ok_or_else(|| CoreError::validation("tile 区间溢出"))?;
            if end > size {
                return Err(CoreError::validation(format!(
                    "tile {i} 区间 [{to}, {end}) 超出文件 {size}"
                )));
            }
        }
    }
    let mut subifds = Vec::new();
    if let Some((typ, count, val)) = sub_buf {
        let width: u64 = match typ {
            4 | 13 => 4,
            16 | 18 => 8,
            _ => return Err(CoreError::validation(format!("SubIFDs 类型 {typ} 非法"))),
        };
        if count == 0 || count > MAX_IFDS as u64 {
            return Err(CoreError::validation(format!("SubIFDs 数 {count} 异常")));
        }
        let bytes = if count * width <= 8 {
            val.to_le_bytes()[..(count * width) as usize].to_vec()
        } else {
            let end = val
                .checked_add(count * width)
                .ok_or_else(|| CoreError::validation("SubIFDs 数组长度溢出"))?;
            if end > size {
                return Err(CoreError::validation("SubIFDs 数组越界"));
            }
            src.read_at(val, (count * width) as usize)?
        };
        for c in bytes.chunks_exact(width as usize) {
            subifds.push(if width == 8 {
                rd_u64(c, 0)
            } else {
                u32::from_le_bytes([c[0], c[1], c[2], c[3]]) as u64
            });
        }
    }
    // next IFD pointer sits after the entry table
    let next = rd_u64(&src.read_at(ifd_body + table_len as u64, 8)?, 0);
    Ok(IfdInfo { next, subifds, subfile_type, tile_records })
}

/// Structural walk: BigTIFF little-endian header (II, version 43, 8-byte
/// offsets), the main IFD chain via next-IFD pointers, and every SubIFD
/// tree hanging off it (≤ MAX_IFDS in total, no IFD reached twice), with
/// per-IFD entry count ≤ MAX_ENTRIES and — for tiled IFDs — the
/// TileOffsets/TileByteCounts arrays and every tile inside the file. A
/// SubIFD must be flagged as a reduced image (NewSubfileType bit 0) and may
/// not also sit in the main chain.
pub fn validate_structure(src: &dyn ByteSource, size: u64) -> CoreResult<StructureSummary> {
    if size < 16 {
        return Err(CoreError::validation("输出小于 16 字节"));
    }
    let head = src.read_at(0, 16)?;
    if &head[0..2] != b"II" {
        return Err(CoreError::validation("非 little-endian TIFF"));
    }
    if rd_u16(&head, 2) != 43 {
        return Err(CoreError::validation("非 BigTIFF（version != 43）"));
    }
    if rd_u16(&head, 4) != 8 {
        return Err(CoreError::validation("BigTIFF offset size != 8"));
    }
    if rd_u16(&head, 6) != 0 {
        return Err(CoreError::validation("BigTIFF reserved != 0"));
    }

    let mut out = StructureSummary::default();
    let mut seen = std::collections::HashSet::new();
    let mut pending_subs: Vec<u64> = Vec::new();
    let mut next = rd_u64(&head, 8);
    while next != 0 {
        if seen.len() >= MAX_IFDS {
            return Err(CoreError::validation("IFD 数超出上限（疑似环）"));
        }
        if !seen.insert(next) {
            return Err(CoreError::validation("IFD 偏移重复（环引用）"));
        }
        let info = read_ifd(src, size, next)?;
        out.main_ifds += 1;
        out.tile_records += info.tile_records;
        pending_subs.extend(info.subifds);
        next = info.next;
    }
    if out.main_ifds == 0 {
        return Err(CoreError::validation("无 IFD"));
    }
    while let Some(at) = pending_subs.pop() {
        if seen.len() >= MAX_IFDS {
            return Err(CoreError::validation("IFD 数超出上限（疑似环）"));
        }
        if !seen.insert(at) {
            return Err(CoreError::validation(format!(
                "SubIFD {at} 重复引用（或同时位于主链）"
            )));
        }
        let info = read_ifd(src, size, at)?;
        if info.subfile_type & 1 == 0 {
            return Err(CoreError::validation(format!(
                "SubIFD {at} 未标记为降采样图（NewSubfileType）"
            )));
        }
        out.sub_ifds += 1;
        out.tile_records += info.tile_records;
        pending_subs.extend(info.subifds);
        if info.next != 0 {
            pending_subs.push(info.next);
        }
    }
    Ok(out)
}

/// Full validation: sha256 + structure. `expect_ifd` (when provided) must
/// match the walked count.
pub fn validate_output(
    src: &dyn ByteSource,
    size: u64,
    expect_ifd: Option<u32>,
) -> CoreResult<ValidationOutcome> {
    let sha256 = stream_sha256(src, size)?;
    let st = validate_structure(src, size)?;
    let ifd_count = st.main_ifds + st.sub_ifds;
    if let Some(e) = expect_ifd {
        if ifd_count != e as usize {
            return Err(CoreError::validation(format!(
                "IFD 数 {ifd_count} ≠ 转换报告 {e}"
            )));
        }
    }
    Ok(ValidationOutcome {
        sha256,
        size,
        ifd_count,
        main_ifds: st.main_ifds,
        sub_ifds: st.sub_ifds,
        tile_records: st.tile_records,
        checks: if st.sub_ifds > 0 {
            vec!["sha256-streamed", "bigtiff-header", "ifd-chain", "subifd-tree", "tile-bounds"]
        } else {
            vec!["sha256-streamed", "bigtiff-header", "ifd-chain", "tile-bounds"]
        },
    })
}
