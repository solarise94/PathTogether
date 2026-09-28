//! Hand-written classic multi-IFD tiled JPEG BigTIFF pyramid writer — a byte
//! layout port of `kfb/converter.py::_BigTiffPyramidWriter` (little-endian,
//! version 43). Layout: 16 B header → all tile payloads (sequential, offsets
//! recorded) → one IFD per level with its external arrays immediately after
//! it → first-IFD offset backpatched at byte 8. Tile offset/count arrays are
//! NOT held in memory: they stream from per-level scratch records (12 B per
//! tile) at finish time, so writer memory is O(levels), not O(tiles).

use crate::error::{CoreError, CoreResult};
use crate::io::{RandomAccessSink, ScratchFactory, ScratchSink};

const TIFF_ASCII: u16 = 2;
const TIFF_SHORT: u16 = 3;
const TIFF_LONG: u16 = 4;
const TIFF_RATIONAL: u16 = 5;
const TIFF_LONG8: u16 = 16;

/// Bytes of one (offset u64, count u32) scratch record.
pub const OFFCNT_REC: u64 = 12;
const STREAM_RECS: usize = 4096;

struct LevelIfd {
    width: u32,
    height: u32,
    sampling: (u16, u16),
    mpp_x: f64,
    mpp_y: f64,
    description: Vec<u8>,
    reduced: bool,
    offcnt: Box<dyn ScratchSink>,
    offcnt_bytes: u64,
    tile_count: u64,
}

enum EntryVal {
    Inline(Vec<u8>),
    /// External payload whose bytes stream from the level's offcnt scratch.
    Offsets,
    Counts,
    /// External payload of static bytes (the ImageDescription).
    Static(Vec<u8>),
}

struct Entry {
    tag: u16,
    typ: u16,
    count: u64,
    val: EntryVal,
}

/// MPP(µm/px) → TIFF RATIONAL (pixels per cm), num/den both u32.
/// Same preference ladder as the oracle (den 10000 → 100 → 1).
pub fn px_per_cm_rational(mpp: f64) -> CoreResult<[u8; 8]> {
    if !mpp.is_finite() || mpp <= 0.0 {
        return Err(CoreError::metadata(format!("mpp={mpp} 非法")));
    }
    for den in [10000u32, 100, 1] {
        let num = ((10000.0 / mpp) * den as f64).round();
        if num > 0.0 && num <= 0xFFFF_FFFFu32 as f64 {
            let mut b = [0u8; 8];
            b[..4].copy_from_slice(&(num as u32).to_le_bytes());
            b[4..].copy_from_slice(&den.to_le_bytes());
            return Ok(b);
        }
    }
    Err(CoreError::metadata(format!("mpp={mpp} 超出 RATIONAL 范围")))
}

pub struct BigTiffPyramidWriter<'a> {
    sink: &'a mut dyn RandomAccessSink,
    cursor: u64,
    ifds: Vec<LevelIfd>,
}

impl<'a> BigTiffPyramidWriter<'a> {
    pub fn new(sink: &'a mut dyn RandomAccessSink) -> CoreResult<Self> {
        // II + version 43 + offsetsize 8 + reserved 0 + reserved 0 + first IFD (backpatch)
        sink.write_at(0, b"II")?;
        let mut hdr = [0u8; 14];
        hdr[0..2].copy_from_slice(&43u16.to_le_bytes());
        hdr[2..4].copy_from_slice(&8u16.to_le_bytes());
        sink.write_at(2, &hdr)?;
        Ok(BigTiffPyramidWriter { sink, cursor: 16, ifds: Vec::new() })
    }

    /// Begin a level: creates the per-level offcnt scratch sink. All tiles
    /// of the level must be written before the next `begin_level`.
    pub fn begin_level(&mut self, scratch: &mut dyn ScratchFactory) -> CoreResult<()> {
        let sink = scratch.create(&format!("offcnt-l{}", self.ifds.len()))?;
        self.ifds.push(LevelIfd {
            width: 0,
            height: 0,
            sampling: (1, 1),
            mpp_x: 0.0,
            mpp_y: 0.0,
            description: Vec::new(),
            reduced: false,
            offcnt: sink,
            offcnt_bytes: 0,
            tile_count: 0,
        });
        Ok(())
    }

    /// Current output cursor (bytes written so far).
    pub fn cursor(&self) -> u64 {
        self.cursor
    }

    /// Append a tile payload sequentially; records (offset, byte count).
    pub fn write_tile(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        let offset = self.cursor;
        self.sink.write_at(offset, data)?;
        self.cursor += data.len() as u64;
        let lv = self.ifds.last_mut().expect("begin_level before write_tile");
        let mut rec = [0u8; OFFCNT_REC as usize];
        rec[..8].copy_from_slice(&offset.to_le_bytes());
        rec[8..].copy_from_slice(&(data.len() as u32).to_le_bytes());
        lv.offcnt.write_at(lv.offcnt_bytes, &rec)?;
        lv.offcnt_bytes += OFFCNT_REC;
        lv.tile_count += 1;
        Ok((offset, data.len() as u32))
    }

    /// Close the current level with its IFD metadata (Python `add_level`).
    #[allow(clippy::too_many_arguments)]
    pub fn end_level(
        &mut self,
        width: u32,
        height: u32,
        sampling: (u16, u16),
        mpp_x: f64,
        mpp_y: f64,
        description: &[u8],
        reduced: bool,
    ) -> CoreResult<()> {
        let lv = self.ifds.last_mut().expect("begin_level before end_level");
        lv.width = width;
        lv.height = height;
        lv.sampling = sampling;
        lv.mpp_x = mpp_x;
        lv.mpp_y = mpp_y;
        lv.description = description.to_vec();
        lv.reduced = reduced;
        Ok(())
    }

    /// Write all IFDs after the last tile payload and backpatch the first
    /// IFD offset. Returns the final output size.
    pub fn finish(&mut self) -> CoreResult<u64> {
        if self.ifds.is_empty() {
            return Err(CoreError::validation("无 IFD 可写"));
        }
        // 字段拆分借用：sink / cursor / ifds 互不冲突
        let BigTiffPyramidWriter { sink, cursor, ifds } = self;
        finish_with(*sink, cursor, ifds)
    }

}

fn finish_with(
    sink: &mut dyn RandomAccessSink,
    cursor: &mut u64,
    ifds: &[LevelIfd],
) -> CoreResult<u64> {
    let mut positions: Vec<(u64, u64)> = Vec::with_capacity(ifds.len()); // (ifd_pos, size)
    let mut pos = *cursor;
    for lv in ifds {
        let n = lv.tile_count;
        let entries = entries_for(lv)?;
        let size = 8 + 20 * entries.len() as u64 + 8;
        let mut ext = 0u64;
        for e in &entries {
            ext += match &e.val {
                EntryVal::Inline(_) => 0,
                EntryVal::Offsets if n * 8 > 8 => {
                    let l = n * 8;
                    l + l % 2
                }
                EntryVal::Counts if n * 8 > 8 => {
                    let l = n * 8;
                    l + l % 2
                }
                EntryVal::Offsets | EntryVal::Counts => 0, // inline (n small)
                EntryVal::Static(b) => {
                    let l = b.len() as u64;
                    l + l % 2
                }
            };
        }
        positions.push((pos, size));
        pos += size + ext;
    }

    for (i, lv) in ifds.iter().enumerate() {
        let (ifd_pos, size) = positions[i];
        if *cursor != ifd_pos {
            return Err(CoreError::validation("IFD 布局错位"));
        }
        let entries = entries_for(lv)?;
        let n = lv.tile_count;
        let mut head = Vec::with_capacity(size as usize);
        head.extend_from_slice(&(entries.len() as u64).to_le_bytes());
        let mut ext_cursor = ifd_pos + size;
        let mut ext_buf: Vec<u8> = Vec::new();
        for e in &entries {
            head.extend_from_slice(&e.tag.to_le_bytes());
            head.extend_from_slice(&e.typ.to_le_bytes());
            head.extend_from_slice(&e.count.to_le_bytes());
            match &e.val {
                EntryVal::Inline(b) => {
                    debug_assert!(b.len() <= 8);
                    head.extend_from_slice(b);
                    head.resize(head.len() + (8 - b.len()), 0);
                }
                EntryVal::Static(b) => {
                    head.extend_from_slice(&ext_cursor.to_le_bytes());
                    let l = b.len() as u64;
                    ext_buf.extend_from_slice(b);
                    if l % 2 == 1 {
                        ext_buf.push(0);
                    }
                    ext_cursor += l + l % 2;
                }
                EntryVal::Offsets => {
                    let l = n * 8;
                    if l <= 8 {
                        let inline = read_offcnt(lv, (l / 8) as u32)?
                            .iter()
                            .flat_map(|r| r.0.to_le_bytes())
                            .collect::<Vec<u8>>();
                        head.extend_from_slice(&inline);
                        head.resize(head.len() + (8 - inline.len()), 0);
                    } else {
                        head.extend_from_slice(&ext_cursor.to_le_bytes());
                        ext_cursor += l + l % 2;
                    }
                }
                EntryVal::Counts => {
                    let l = n * 8;
                    if l <= 8 {
                        let inline = read_offcnt(lv, (l / 8) as u32)?
                            .iter()
                            .map(|r| (r.1 as u64).to_le_bytes())
                            .collect::<Vec<[u8; 8]>>()
                            .concat();
                        head.extend_from_slice(&inline);
                        head.resize(head.len() + (8 - inline.len()), 0);
                    } else {
                        head.extend_from_slice(&ext_cursor.to_le_bytes());
                        ext_cursor += l + l % 2;
                    }
                }
            }
        }
        let next = positions.get(i + 1).map_or(0u64, |p| p.0);
        head.extend_from_slice(&next.to_le_bytes());
        sink.write_at(ifd_pos, &head)?;
        sink.write_at(ifd_pos + head.len() as u64, &ext_buf)?;
        *cursor = ifd_pos + head.len() as u64 + ext_buf.len() as u64;

        // 依 tag 序（270 < 324 < 325）流式写出两个 LONG8 数组
        if n > 1 {
            stream_array(sink, cursor, lv, true)?;
        }
        if n > 1 {
            stream_array(sink, cursor, lv, false)?;
        }
    }

    // 回填第一个 IFD 的偏移（字节 8）
    sink.write_at(8, &positions[0].0.to_le_bytes())?;
    Ok(*cursor)
}

fn entries_for(lv: &LevelIfd) -> CoreResult<Vec<Entry>> {
    let n = lv.tile_count;
    let mut entries = vec![
        Entry {
            tag: 254,
            typ: TIFF_LONG,
            count: 1,
            val: EntryVal::Inline(u32::from(lv.reduced).to_le_bytes().to_vec()),
        },
        Entry { tag: 256, typ: TIFF_LONG, count: 1, val: EntryVal::Inline(lv.width.to_le_bytes().to_vec()) },
        Entry { tag: 257, typ: TIFF_LONG, count: 1, val: EntryVal::Inline(lv.height.to_le_bytes().to_vec()) },
        Entry {
            tag: 258,
            typ: TIFF_SHORT,
            count: 3,
            val: EntryVal::Inline([8u16, 8, 8].iter().flat_map(|v| v.to_le_bytes()).collect()),
        },
        Entry { tag: 259, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(7u16.to_le_bytes().to_vec()) },
        Entry { tag: 262, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(6u16.to_le_bytes().to_vec()) },
        Entry { tag: 270, typ: TIFF_ASCII, count: lv.description.len() as u64, val: EntryVal::Static(lv.description.clone()) },
        Entry { tag: 277, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(3u16.to_le_bytes().to_vec()) },
        Entry { tag: 282, typ: TIFF_RATIONAL, count: 1, val: EntryVal::Inline(px_per_cm_rational(lv.mpp_x)?.to_vec()) },
        Entry { tag: 283, typ: TIFF_RATIONAL, count: 1, val: EntryVal::Inline(px_per_cm_rational(lv.mpp_y)?.to_vec()) },
        Entry { tag: 284, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(1u16.to_le_bytes().to_vec()) },
        Entry { tag: 296, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(3u16.to_le_bytes().to_vec()) },
        Entry { tag: 322, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(256u16.to_le_bytes().to_vec()) },
        Entry { tag: 323, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(256u16.to_le_bytes().to_vec()) },
        Entry { tag: 324, typ: TIFF_LONG8, count: n, val: EntryVal::Offsets },
        Entry { tag: 325, typ: TIFF_LONG8, count: n, val: EntryVal::Counts },
        Entry {
            tag: 530,
            typ: TIFF_SHORT,
            count: 2,
            val: EntryVal::Inline([lv.sampling.0, lv.sampling.1].iter().flat_map(|v| v.to_le_bytes()).collect()),
        },
    ];
    entries.sort_by_key(|e| e.tag);
    Ok(entries)
}

fn read_offcnt(lv: &LevelIfd, n: u32) -> CoreResult<Vec<(u64, u32)>> {
    let mut out = Vec::with_capacity(n as usize);
    for i in 0..n as u64 {
        let b = lv.offcnt.read_at(i * OFFCNT_REC, OFFCNT_REC as usize)?;
        let mut off = [0u8; 8];
        off.copy_from_slice(&b[..8]);
        out.push((u64::from_le_bytes(off), u32::from_le_bytes([b[8], b[9], b[10], b[11]])));
    }
    Ok(out)
}

/// Stream TileOffsets (`offsets=true`) or TileByteCounts from scratch to the
/// sink at the current cursor.
fn stream_array(
    sink: &mut dyn RandomAccessSink,
    cursor: &mut u64,
    lv: &LevelIfd,
    offsets: bool,
) -> CoreResult<()> {
    let n = lv.tile_count;
    debug_assert_eq!(lv.offcnt_bytes / OFFCNT_REC, n);
    let mut buf: Vec<u8> = Vec::with_capacity(STREAM_RECS * 8);
    let mut i: u64 = 0;
    while i < n {
        let want = STREAM_RECS.min((n - i) as usize) as u64;
        let raw = lv.offcnt.read_at(i * OFFCNT_REC, want as usize * OFFCNT_REC as usize)?;
        buf.clear();
        for rec in raw.chunks_exact(OFFCNT_REC as usize) {
            if offsets {
                buf.extend_from_slice(&rec[..8]);
            } else {
                // TileByteCounts 也是 LONG8：u32 记录拓宽为 u64
                let c = u32::from_le_bytes([rec[8], rec[9], rec[10], rec[11]]);
                buf.extend_from_slice(&(c as u64).to_le_bytes());
            }
        }
        sink.write_at(*cursor, &buf)?;
        *cursor += buf.len() as u64;
        i += want;
    }
    // n*8 恒为偶数，无需奇偶补齐（保留断言以显式化合同）
    debug_assert_eq!((if offsets { n * 8 } else { n * 8 }) % 2, 0);
    Ok(())
}
