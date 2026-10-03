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
    mpp_x: Option<f64>,
    mpp_y: Option<f64>,
    description: Vec<u8>,
    reduced: bool,
    /// F1 generalisation: the source's own tile shape (KFB: 256×256).
    tile: (u32, u32),
    /// PhotometricInterpretation (KFB: 6 YCbCr; Aperio RGB JPEG: 2).
    photometric: u16,
    /// Shared JPEG tables (tag 347) written verbatim when the source uses
    /// abbreviated streams (Aperio). `None` for KFB.
    jpeg_tables: Option<Vec<u8>>,
    /// ICC profile (tag 34675); written on the IFDs the caller declares
    /// (Aperio: main level only). `None` for KFB.
    icc: Option<Vec<u8>>,
    offcnt: Box<dyn ScratchSink>,
    offcnt_bytes: u64,
    tile_count: u64,
}

/// Extra level tags the F1 SVS adapter needs (all fields default to the
/// historical KFB layout, so `end_level` stays byte-identical).
#[derive(Debug, Clone)]
pub struct LevelExtras {
    /// TileWidth/TileLength (322/323).
    pub tile: (u32, u32),
    /// PhotometricInterpretation (262); 530 is only written for 6 (YCbCr).
    pub photometric: u16,
    /// JPEGTables (347) bytes, written verbatim into the IFD.
    pub jpeg_tables: Option<Vec<u8>>,
    /// InterColorProfile (34675) bytes.
    pub icc: Option<Vec<u8>>,
}

impl Default for LevelExtras {
    fn default() -> Self {
        LevelExtras { tile: (256, 256), photometric: 6, jpeg_tables: None, icc: None }
    }
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

    /// Resume variant: the host already truncated the sink to
    /// `committed_output` (the 16-byte header is part of it); do NOT rewrite
    /// the header, just adopt the cursor.
    pub fn resume_new(sink: &'a mut dyn RandomAccessSink, committed_output: u64) -> CoreResult<Self> {
        if committed_output < 16 {
            return Err(CoreError::validation("resume: committed_output < 16"));
        }
        Ok(BigTiffPyramidWriter { sink, cursor: committed_output, ifds: Vec::new() })
    }

    /// Begin a level whose offset/count stream is partially committed in
    /// scratch: preserve-open the scratch sink and adopt `committed_tiles`.
    pub fn begin_level_resume(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        committed_tiles: u64,
    ) -> CoreResult<()> {
        let sink = scratch
            .create_preserve(&format!("offcnt-l{}", self.ifds.len()))?;
        let bytes = committed_tiles
            .checked_mul(OFFCNT_REC)
            .ok_or_else(|| CoreError::validation("resume: offcnt 长度溢出"))?;
        self.ifds.push(LevelIfd {
            width: 0,
            height: 0,
            sampling: (1, 1),
            mpp_x: None,
            mpp_y: None,
            description: Vec::new(),
            reduced: false,
            tile: (256, 256),
            photometric: 6,
            jpeg_tables: None,
            icc: None,
            offcnt: sink,
            offcnt_bytes: bytes,
            tile_count: committed_tiles,
        });
        Ok(())
    }

    /// Committed tile counts per begun IFD, in begin order (checkpoint
    /// emission; O(levels) bytes).
    pub fn ifd_tile_counts(&self) -> Vec<u64> {
        self.ifds.iter().map(|l| l.tile_count).collect()
    }

    /// Begin a level: creates the per-level offcnt scratch sink. All tiles
    /// of the level must be written before the next `begin_level`.
    pub fn begin_level(&mut self, scratch: &mut dyn ScratchFactory) -> CoreResult<()> {
        let sink = scratch.create(&format!("offcnt-l{}", self.ifds.len()))?;
        self.ifds.push(LevelIfd {
            width: 0,
            height: 0,
            sampling: (1, 1),
            mpp_x: None,
            mpp_y: None,
            description: Vec::new(),
            reduced: false,
            tile: (256, 256),
            photometric: 6,
            jpeg_tables: None,
            icc: None,
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

    /// One committed (TileOffsets, TileByteCounts) record of IFD `ifd`
    /// (review §4 pyramid: the next level reads the previous level's
    /// payloads back through these records).
    pub fn tile_record(&self, ifd: usize, index: usize) -> CoreResult<(u64, u32)> {
        let lv = self
            .ifds
            .get(ifd)
            .ok_or_else(|| CoreError::validation("tile_record: IFD 越界"))?;
        let b = lv.offcnt.read_at(index as u64 * OFFCNT_REC, OFFCNT_REC as usize)?;
        let mut off = [0u8; 8];
        off.copy_from_slice(&b[..8]);
        Ok((
            u64::from_le_bytes(off),
            u32::from_le_bytes([b[8], b[9], b[10], b[11]]),
        ))
    }

    /// Bounded read-back of already-written output bytes (review §4: the
    /// pyramid decodes the previous level's encoded tiles from the sink).
    pub fn read_output_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        self.sink.read_at(offset, len)
    }

    /// Append a tile payload sequentially; records (offset, byte count).
    pub fn write_tile(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        let offset = self.cursor;
        self.sink.write_at(offset, data)?;
        self.cursor += data.len() as u64;
        self.record_tile(offset, data.len() as u32)?;
        Ok((offset, data.len() as u32))
    }

    /// Append a RAW payload at the cursor WITHOUT recording a tile — the
    /// caller becomes responsible for referencing it (see
    /// [`Self::write_tile_ref`]). F3 fill-tile dedupe: an identical payload
    /// written once and referenced by every tile that contains exactly it.
    pub fn write_payload(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        let offset = self.cursor;
        self.sink.write_at(offset, data)?;
        self.cursor += data.len() as u64;
        Ok((offset, data.len() as u32))
    }

    /// Record a tile whose payload lives at a previously written offset
    /// (shared payload). Valid TIFF: TileOffsets entries may repeat.
    pub fn write_tile_ref(&mut self, offset: u64, count: u32) -> CoreResult<()> {
        self.record_tile(offset, count)
    }

    fn record_tile(&mut self, offset: u64, count: u32) -> CoreResult<()> {
        let lv = self.ifds.last_mut().expect("begin_level before write_tile");
        let mut rec = [0u8; OFFCNT_REC as usize];
        rec[..8].copy_from_slice(&offset.to_le_bytes());
        rec[8..].copy_from_slice(&count.to_le_bytes());
        lv.offcnt.write_at(lv.offcnt_bytes, &rec)?;
        lv.offcnt_bytes += OFFCNT_REC;
        lv.tile_count += 1;
        Ok(())
    }

    /// Close the current level with its IFD metadata (Python `add_level`).
    /// Historical signature: YCbCr 256-tile levels with a known MPP — kept
    /// so the KFB path is untouched.
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
        self.end_level_ex(
            width,
            height,
            sampling,
            Some((mpp_x, mpp_y)),
            description,
            reduced,
            &LevelExtras::default(),
        )
    }

    /// F1 generalisation of [`Self::end_level`]: arbitrary tile shape,
    /// photometric (2 RGB / 6 YCbCr), optional JPEGTables/ICC and optional
    /// calibration (unknown MPP ⇒ tags 282/283/296 are omitted, not faked).
    #[allow(clippy::too_many_arguments)]
    pub fn end_level_ex(
        &mut self,
        width: u32,
        height: u32,
        sampling: (u16, u16),
        mpp: Option<(f64, f64)>,
        description: &[u8],
        reduced: bool,
        extras: &LevelExtras,
    ) -> CoreResult<()> {
        let lv = self.ifds.last_mut().expect("begin_level before end_level");
        if extras.tile.0 == 0 || extras.tile.1 == 0 || extras.tile.0 > 65535 || extras.tile.1 > 65535
        {
            return Err(CoreError::validation(format!(
                "tile 尺寸 {:?} 超出 TIFF SHORT 范围",
                extras.tile
            )));
        }
        if extras.photometric != 2 && extras.photometric != 6 {
            return Err(CoreError::validation(format!(
                "photometric {} 不在支持集（2 RGB / 6 YCbCr）",
                extras.photometric
            )));
        }
        lv.width = width;
        lv.height = height;
        lv.sampling = sampling;
        lv.mpp_x = mpp.map(|m| m.0);
        lv.mpp_y = mpp.map(|m| m.1);
        lv.description = description.to_vec();
        lv.reduced = reduced;
        lv.tile = extras.tile;
        lv.photometric = extras.photometric;
        lv.jpeg_tables = extras.jpeg_tables.clone();
        lv.icc = extras.icc.clone();
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
        *cursor = ifd_pos + head.len() as u64;

        // External segments are laid out in ENTRY ORDER — a Static value may
        // sort after the tile arrays (347 JPEGTables / 34675 ICC), so the
        // arrays cannot simply be appended after every static value. (The
        // KFB layout — one static 270 then 324/325 — produces the very same
        // byte stream as before.)
        for e in &entries {
            match &e.val {
                EntryVal::Static(b) => {
                    let l = b.len() as u64;
                    let mut padded = b.clone();
                    if l % 2 == 1 {
                        padded.push(0);
                    }
                    sink.write_at(*cursor, &padded)?;
                    *cursor += l + l % 2;
                }
                EntryVal::Offsets | EntryVal::Counts => {
                    let l = n * 8;
                    if l > 8 {
                        stream_array(sink, cursor, lv, matches!(e.val, EntryVal::Offsets))?;
                    }
                }
                EntryVal::Inline(_) => {}
            }
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
        Entry { tag: 262, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(lv.photometric.to_le_bytes().to_vec()) },
        Entry { tag: 270, typ: TIFF_ASCII, count: lv.description.len() as u64, val: EntryVal::Static(lv.description.clone()) },
        Entry { tag: 277, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(3u16.to_le_bytes().to_vec()) },
        Entry { tag: 284, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(1u16.to_le_bytes().to_vec()) },
        Entry { tag: 322, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline((lv.tile.0 as u16).to_le_bytes().to_vec()) },
        Entry { tag: 323, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline((lv.tile.1 as u16).to_le_bytes().to_vec()) },
        Entry { tag: 324, typ: TIFF_LONG8, count: n, val: EntryVal::Offsets },
        Entry { tag: 325, typ: TIFF_LONG8, count: n, val: EntryVal::Counts },
    ];
    // calibration: a level without a trustworthy MPP omits 282/283/296
    // rather than inventing one (F1 rule). The KFB path always writes them,
    // in the historical position (between 277 and 284 after sorting).
    if let (Some(mx), Some(my)) = (lv.mpp_x, lv.mpp_y) {
        entries.push(Entry { tag: 282, typ: TIFF_RATIONAL, count: 1, val: EntryVal::Inline(px_per_cm_rational(mx)?.to_vec()) });
        entries.push(Entry { tag: 283, typ: TIFF_RATIONAL, count: 1, val: EntryVal::Inline(px_per_cm_rational(my)?.to_vec()) });
        entries.push(Entry { tag: 296, typ: TIFF_SHORT, count: 1, val: EntryVal::Inline(3u16.to_le_bytes().to_vec()) });
    }
    // YCbCrSubSampling only for YCbCr payloads; the value is the JPEG SOF
    // truth (never the source tag, which Aperio files mislabel)
    if lv.photometric == 6 {
        entries.push(Entry {
            tag: 530,
            typ: TIFF_SHORT,
            count: 2,
            val: EntryVal::Inline([lv.sampling.0, lv.sampling.1].iter().flat_map(|v| v.to_le_bytes()).collect()),
        });
    }
    if let Some(icc) = &lv.icc {
        entries.push(Entry {
            tag: 34675,
            typ: 7, // UNDEFINED
            count: icc.len() as u64,
            val: EntryVal::Static(icc.clone()),
        });
    }
    if let Some(t) = &lv.jpeg_tables {
        entries.push(Entry {
            tag: 347,
            typ: 7, // UNDEFINED
            count: t.len() as u64,
            val: EntryVal::Static(t.clone()),
        });
    }
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
