//! Fluorescence OME-BigTIFF writer with SubIFD pyramid — byte-layout port of
//! `kfb/converter_fl.py::_OmeBigTiffWriter`. Top-level IFD chain = level-0
//! channel IFDs; each level-0 channel IFD carries SubIFDs (tag 330, LONG8)
//! pointing at that channel's levels 1..N. IFD file order is level-major:
//! (0,0)..(0,C−1), (1,0)..(1,C−1), … Tile payloads stream first; offset/count
//! arrays stream from per-IFD scratch records (12 B/tile) at finish time, so
//! memory stays O(levels × channels), not O(tiles).

use crate::error::{CoreError, CoreResult};
use crate::io::{RandomAccessSink, ScratchFactory, ScratchSink};

const TIFF_SHORT: u16 = 3;
const TIFF_LONG: u16 = 4;
const TIFF_RATIONAL: u16 = 5;
const TIFF_ASCII: u16 = 2;
const TIFF_LONG8: u16 = 16;

/// 12 B (offset u64, count u32) scratch record per tile.
pub const OFFCNT_REC: u64 = 12;
const STREAM_RECS: usize = 4096;

/// Round-half-to-even, matching Python `round()`.
fn round_ties_even(x: f64) -> f64 {
    x.round_ties_even()
}

/// MPP(µm/px) → TIFF RATIONAL px/cm with the oracle's denominator ladder.
pub fn px_per_cm_rational(mpp: f64) -> CoreResult<[u8; 8]> {
    if !mpp.is_finite() || mpp <= 0.0 {
        return Err(CoreError::metadata(format!("mpp={mpp} 非法")));
    }
    for den in [10000u32, 100, 1] {
        let num = round_ties_even(10000.0 / mpp * den as f64);
        if num > 0.0 && num <= 0xFFFF_FFFFu32 as f64 {
            let mut b = [0u8; 8];
            b[..4].copy_from_slice(&(num as u32).to_le_bytes());
            b[4..].copy_from_slice(&den.to_le_bytes());
            return Ok(b);
        }
    }
    Err(CoreError::metadata(format!("mpp={mpp} 超出 RATIONAL 范围")))
}

struct IfdSpec {
    width: u32,
    height: u32,
    reduced: bool,
    level_mpp: f64,
    description: Option<Vec<u8>>,
    /// SubIFD target IFD indices (into the ifds vector).
    sub_idx: Vec<usize>,
    offcnt: Box<dyn ScratchSink>,
    offcnt_bytes: u64,
    tile_count: u64,
}

/// Streaming OME-BigTIFF (SubIFD) writer.
pub struct OmeBigTiffWriter<'a> {
    sink: &'a mut dyn RandomAccessSink,
    cursor: u64,
    ifds: Vec<IfdSpec>,
}

impl<'a> OmeBigTiffWriter<'a> {
    pub fn new(sink: &'a mut dyn RandomAccessSink) -> CoreResult<Self> {
        sink.write_at(0, b"II")?;
        let mut hdr = [0u8; 14];
        hdr[0..2].copy_from_slice(&43u16.to_le_bytes());
        hdr[2..4].copy_from_slice(&8u16.to_le_bytes());
        sink.write_at(2, &hdr)?;
        Ok(OmeBigTiffWriter { sink, cursor: 16, ifds: Vec::new() })
    }

    /// Resume variant: adopt the committed cursor without rewriting the
    /// header (the host truncated the sink to `committed_output`).
    pub fn resume_new(sink: &'a mut dyn RandomAccessSink, committed_output: u64) -> CoreResult<Self> {
        if committed_output < 16 {
            return Err(CoreError::validation("resume: committed_output < 16"));
        }
        Ok(OmeBigTiffWriter { sink, cursor: committed_output, ifds: Vec::new() })
    }

    /// Begin an IFD whose offcnt stream is partially committed in scratch
    /// (preserve-open + adopt `committed_tiles`).
    pub fn begin_ifd_resume(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        width: u32,
        height: u32,
        reduced: bool,
        level_mpp: f64,
        description: Option<Vec<u8>>,
        committed_tiles: u64,
    ) -> CoreResult<()> {
        let sink = scratch
            .create_preserve(&format!("ome-offcnt-{}", self.ifds.len()))?;
        let bytes = committed_tiles
            .checked_mul(OFFCNT_REC)
            .ok_or_else(|| CoreError::validation("resume: offcnt 长度溢出"))?;
        self.ifds.push(IfdSpec {
            width,
            height,
            reduced,
            level_mpp,
            description,
            sub_idx: Vec::new(),
            offcnt: sink,
            offcnt_bytes: bytes,
            tile_count: committed_tiles,
        });
        Ok(())
    }

    /// Committed tile counts per begun IFD, in begin order.
    pub fn ifd_tile_counts(&self) -> Vec<u64> {
        self.ifds.iter().map(|l| l.tile_count).collect()
    }

    /// Begin an IFD (`key` order is the caller's responsibility: level-major,
    /// channel-minor). All tiles must be written before `begin_ifd` again.
    pub fn begin_ifd(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        width: u32,
        height: u32,
        reduced: bool,
        level_mpp: f64,
        description: Option<Vec<u8>>,
    ) -> CoreResult<()> {
        let sink = scratch.create(&format!("ome-offcnt-{}", self.ifds.len()))?;
        self.ifds.push(IfdSpec {
            width,
            height,
            reduced,
            level_mpp,
            description,
            sub_idx: Vec::new(),
            offcnt: sink,
            offcnt_bytes: 0,
            tile_count: 0,
        });
        Ok(())
    }

    pub fn cursor(&self) -> u64 {
        self.cursor
    }

    /// Append a tile payload sequentially (records offset/count).
    pub fn write_tile(&mut self, data: &[u8]) -> CoreResult<(u64, u32)> {
        let offset = self.cursor;
        self.sink.write_at(offset, data)?;
        self.cursor += data.len() as u64;
        let lv = self.ifds.last_mut().expect("begin_ifd before write_tile");
        let mut rec = [0u8; OFFCNT_REC as usize];
        rec[..8].copy_from_slice(&offset.to_le_bytes());
        rec[8..].copy_from_slice(&(data.len() as u32).to_le_bytes());
        lv.offcnt.write_at(lv.offcnt_bytes, &rec)?;
        lv.offcnt_bytes += OFFCNT_REC;
        lv.tile_count += 1;
        Ok((offset, data.len() as u32))
    }

    /// Declare the SubIFD children of the IFD begun most recently
    /// (level-0 channel IFD → its levels 1..N).
    pub fn set_subifds(&mut self, sub_idx: Vec<usize>) -> CoreResult<()> {
        let lv = self.ifds.last_mut().ok_or_else(|| CoreError::validation("无 IFD"))?;
        lv.sub_idx = sub_idx;
        Ok(())
    }

    /// Declare SubIFD children for the IFD at `ifd_index` (0-based, in begin
    /// order) — used to wire level-0 channel IFDs after all IFDs are begun.
    pub fn set_subifds_for(
        &mut self,
        ifd_index: usize,
        sub_idx: Vec<usize>,
    ) -> CoreResult<()> {
        let lv = self
            .ifds
            .get_mut(ifd_index)
            .ok_or_else(|| CoreError::validation("IFD 序号越界"))?;
        lv.sub_idx = sub_idx;
        Ok(())
    }

    /// Compute all IFD byte offsets, then write the IFD chain, patch SubIFDs
    /// and the first-IFD pointer. External segments are laid out in tag order
    /// (270 desc, 324 offsets, 325 counts, 330 SubIFDs), matching the oracle.
    pub fn finish(
        &mut self,
        first_ifd: usize,
        chain: &[usize],
    ) -> CoreResult<u64> {
        if self.ifds.is_empty() {
            return Err(CoreError::validation("无 IFD 可写"));
        }
        let mut positions: Vec<u64> = Vec::with_capacity(self.ifds.len());
        let mut pos = self.cursor;
        for ifd in &self.ifds {
            let size = 8 + 20 * entry_count(ifd) as u64 + 8;
            positions.push(pos);
            pos += size + ext_bytes(ifd);
        }
        let mut next_of = vec![0u64; self.ifds.len()];
        for (i, &key) in chain.iter().enumerate() {
            next_of[key] =
                if i + 1 < chain.len() { positions[chain[i + 1]] } else { 0 };
        }

        enum Seg {
            Bytes(Vec<u8>),
            Offsets,
            Counts,
        }

        for (i, ifd) in self.ifds.iter().enumerate() {
            let ifd_pos = positions[i];
            if self.cursor != ifd_pos {
                return Err(CoreError::validation("IFD 布局错位"));
            }
            let n = ifd.tile_count;
            let ec = entry_count(ifd);
            let size = 8 + 20 * ec as u64 + 8;
            let mut head: Vec<u8> = Vec::with_capacity(size as usize);
            head.extend_from_slice(&(ec as u64).to_le_bytes());
            let mut ext_cursor = ifd_pos + size;
            let mut segs: Vec<Seg> = Vec::new();

            // (tag, type, count, inline payload or segment marker)
            let mut emit = |head: &mut Vec<u8>,
                            segs: &mut Vec<Seg>,
                            ext_cursor: &mut u64,
                            tag: u16,
                            typ: u16,
                            count: u64,
                            inline: Option<&[u8]>,
                            seg: Option<Seg>| {
                head.extend_from_slice(&tag.to_le_bytes());
                head.extend_from_slice(&typ.to_le_bytes());
                head.extend_from_slice(&count.to_le_bytes());
                match (inline, seg) {
                    (Some(p), _) => {
                        head.extend_from_slice(p);
                        head.resize(head.len() + (8 - p.len()), 0);
                    }
                    (None, Some(sg)) => {
                        let l = match &sg {
                            Seg::Bytes(b) => b.len() as u64,
                            Seg::Offsets | Seg::Counts => n * 8,
                        };
                        head.extend_from_slice(&ext_cursor.to_le_bytes());
                        *ext_cursor += l + l % 2;
                        segs.push(sg);
                    }
                    _ => unreachable!(),
                }
            };

            emit(&mut head, &mut segs, &mut ext_cursor, 254, TIFF_LONG, 1,
                Some(&u32::from(ifd.reduced).to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 256, TIFF_LONG, 1,
                Some(&ifd.width.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 257, TIFF_LONG, 1,
                Some(&ifd.height.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 258, TIFF_SHORT, 1,
                Some(&8u16.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 259, TIFF_SHORT, 1,
                Some(&7u16.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 262, TIFF_SHORT, 1,
                Some(&1u16.to_le_bytes()), None);
            if let Some(d) = ifd.description.as_ref() {
                if d.len() <= 8 {
                    let mut d8 = d.clone();
                    d8.resize(8, 0);
                    emit(&mut head, &mut segs, &mut ext_cursor, 270, TIFF_ASCII,
                        d.len() as u64, Some(&d8), None);
                } else {
                    emit(&mut head, &mut segs, &mut ext_cursor, 270, TIFF_ASCII,
                        d.len() as u64, None, Some(Seg::Bytes(d.clone())));
                }
            }
            emit(&mut head, &mut segs, &mut ext_cursor, 277, TIFF_SHORT, 1,
                Some(&1u16.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 282, TIFF_RATIONAL, 1,
                Some(&px_per_cm_rational(ifd.level_mpp)?), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 283, TIFF_RATIONAL, 1,
                Some(&px_per_cm_rational(ifd.level_mpp)?), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 284, TIFF_SHORT, 1,
                Some(&1u16.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 296, TIFF_SHORT, 1,
                Some(&3u16.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 322, TIFF_SHORT, 1,
                Some(&256u16.to_le_bytes()), None);
            emit(&mut head, &mut segs, &mut ext_cursor, 323, TIFF_SHORT, 1,
                Some(&256u16.to_le_bytes()), None);
            if n * 8 <= 8 {
                let mut inline = read_offcnt(ifd, n as u32, true)?;
                inline.resize(8, 0);
                emit(&mut head, &mut segs, &mut ext_cursor, 324, TIFF_LONG8, n,
                    Some(&inline), None);
                let mut inline = read_offcnt(ifd, n as u32, false)?;
                inline.resize(8, 0);
                emit(&mut head, &mut segs, &mut ext_cursor, 325, TIFF_LONG8, n,
                    Some(&inline), None);
            } else {
                emit(&mut head, &mut segs, &mut ext_cursor, 324, TIFF_LONG8, n,
                    None, Some(Seg::Offsets));
                emit(&mut head, &mut segs, &mut ext_cursor, 325, TIFF_LONG8, n,
                    None, Some(Seg::Counts));
            }
            if !ifd.sub_idx.is_empty() {
                let mut sub = Vec::with_capacity(ifd.sub_idx.len() * 8);
                for &si in &ifd.sub_idx {
                    sub.extend_from_slice(&positions[si].to_le_bytes());
                }
                if sub.len() <= 8 {
                    let mut s8 = sub;
                    s8.resize(8, 0);
                    emit(&mut head, &mut segs, &mut ext_cursor, 330, TIFF_LONG8,
                        ifd.sub_idx.len() as u64, Some(&s8), None);
                } else {
                    emit(&mut head, &mut segs, &mut ext_cursor, 330, TIFF_LONG8,
                        ifd.sub_idx.len() as u64, None, Some(Seg::Bytes(sub)));
                }
            }

            head.extend_from_slice(&next_of[i].to_le_bytes());
            self.sink.write_at(ifd_pos, &head)?;
            self.cursor = ifd_pos + head.len() as u64;
            for seg in &segs {
                match seg {
                    Seg::Bytes(b) => {
                        let mut b2 = b.clone();
                        if b2.len() % 2 == 1 {
                            b2.push(0);
                        }
                        self.sink.write_at(self.cursor, &b2)?;
                        self.cursor += b2.len() as u64;
                    }
                    Seg::Offsets => {
                        stream_array(self.sink, &mut self.cursor, ifd, true)?;
                    }
                    Seg::Counts => {
                        stream_array(self.sink, &mut self.cursor, ifd, false)?;
                    }
                }
            }
        }
        self.sink.write_at(8, &positions[first_ifd].to_le_bytes())?;
        Ok(self.cursor)
    }
}

fn entry_count(ifd: &IfdSpec) -> usize {
    15 + usize::from(ifd.description.is_some()) + usize::from(!ifd.sub_idx.is_empty())
}

fn ext_bytes(ifd: &IfdSpec) -> u64 {
    let n = ifd.tile_count;
    let mut ext = 0u64;
    if let Some(d) = &ifd.description {
        let l = d.len() as u64;
        ext += l + l % 2;
    }
    if n * 8 > 8 {
        let l = n * 8;
        ext += 2 * (l + l % 2);
    }
    if !ifd.sub_idx.is_empty() {
        let l = (ifd.sub_idx.len() * 8) as u64;
        ext += l + l % 2;
    }
    ext
}

fn read_offcnt(ifd: &IfdSpec, n: u32, offsets: bool) -> CoreResult<Vec<u8>> {
    let mut out = Vec::with_capacity(n as usize * 8);
    for i in 0..n as u64 {
        let b = ifd.offcnt.read_at(i * OFFCNT_REC, OFFCNT_REC as usize)?;
        if offsets {
            out.extend_from_slice(&b[..8]);
        } else {
            let c = u32::from_le_bytes([b[8], b[9], b[10], b[11]]);
            out.extend_from_slice(&(c as u64).to_le_bytes());
        }
    }
    Ok(out)
}

/// Stream TileOffsets / TileByteCounts LONG8 arrays from scratch to sink.
fn stream_array(
    sink: &mut dyn RandomAccessSink,
    cursor: &mut u64,
    ifd: &IfdSpec,
    offsets: bool,
) -> CoreResult<()> {
    let n = ifd.tile_count;
    let mut buf: Vec<u8> = Vec::with_capacity(STREAM_RECS * 8);
    let mut i: u64 = 0;
    while i < n {
        let want = STREAM_RECS.min((n - i) as usize) as u64;
        let raw = ifd
            .offcnt
            .read_at(i * OFFCNT_REC, want as usize * OFFCNT_REC as usize)?;
        buf.clear();
        for rec in raw.chunks_exact(OFFCNT_REC as usize) {
            if offsets {
                buf.extend_from_slice(&rec[..8]);
            } else {
                let c = u32::from_le_bytes([rec[8], rec[9], rec[10], rec[11]]);
                buf.extend_from_slice(&(c as u64).to_le_bytes());
            }
        }
        sink.write_at(*cursor, &buf)?;
        *cursor += buf.len() as u64;
        i += want;
    }
    Ok(())
}
