//! Bounded TIFF/BigTIFF reader (F1): the input side for SVS conversion.
//!
//! Everything is random-access through [`ByteSource`] with explicit-length
//! reads — never a whole-file buffer — so the browser source (OPFS sync
//! handle) and the native file source behave identically. Hard bounds:
//!
//! - byte order (`II`/`MM`) and version (42 classic / 43 BigTIFF) must match;
//! - every IFD entry table, value and array must sit inside the file;
//! - per-IFD entry count ≤ [`MAX_IFD_ENTRIES`], walked IFDs ≤ [`MAX_IFDS`]
//!   with duplicate-offset detection (pointer loops are a typed error, not a
//!   hang);
//! - every multiplication of (count × element size) is checked;
//! - dimension/tile-count products are checked before use by the caller.
//!
//! The reader resolves only tags the caller asks for; unknown tags with odd
//! types are skipped (their length is unknown), known tags with wrong types
//! are typed errors.

use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;

/// Defensive caps (structural walk mirrors `validate.rs`). MAX_IFDS was
/// raised 64 → 256 for the Leica SCN adapter (F4): a multi-ROI SCN file
/// chains every ROI's label + pyramid levels into ONE main IFD chain, which
/// can pass ~100 IFDs on a 2010/10/01 slide; the walk stays bounded and
/// loop-checked exactly as before.
pub const MAX_IFD_ENTRIES: usize = 512;
pub const MAX_IFDS: usize = 256;
/// Entries read per bounded source read while scanning an entry table.
const ENTRY_CHUNK: usize = 128;
/// Tile (offset,count) pairs buffered per source read.
const TILE_PAIR_CHUNK: usize = 512;
/// Cap on a single resolved tag value (descriptions, ICC, tables).
const MAX_TAG_VALUE_BYTES: u64 = 4 << 20;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TiffKind {
    /// Version 42, 16-byte header, 4-byte offsets and entry counts.
    Classic,
    /// Version 43, 16-byte header, 8-byte offsets and entry counts.
    BigTiff,
}

#[derive(Debug, Clone, Copy)]
pub struct TiffHeader {
    /// Little-endian file?
    pub little: bool,
    pub kind: TiffKind,
    /// Offset of the first IFD (header field, validated lazily by read_ifd).
    pub first_ifd: u64,
    /// File size (kept for bounds checks).
    pub size: u64,
}

fn type_size(typ: u16) -> Option<u64> {
    Some(match typ {
        1 | 2 | 6 | 7 => 1, // BYTE ASCII SBYTE UNDEFINED
        3 | 8 => 2,        // SHORT SSHORT
        4 | 9 | 13 => 4,   // LONG SLONG IFD
        5 | 10 | 11 | 12 | 16 | 17 | 18 => 8, // RATIONAL SRATIONAL FLOAT DOUBLE LONG8 SLONG8 IFD8
        _ => return None,
    })
}

#[derive(Debug, Clone, Copy)]
pub struct IfdEntry {
    pub tag: u16,
    pub typ: u16,
    pub count: u64,
    /// Raw value-or-offset field (4 B classic / 8 B BigTIFF, file order).
    pub val: [u8; 8],
}

impl IfdEntry {
    /// Byte length of the value if the type is known.
    pub fn value_len(&self) -> Option<u64> {
        let ts = type_size(self.typ)?;
        self.count.checked_mul(ts)
    }
}

#[derive(Debug, Clone)]
pub struct Ifd {
    /// File offset of this IFD (SubIFD resolution / reporting).
    pub offset: u64,
    pub entries: Vec<IfdEntry>,
    /// Next IFD in the main chain (0 = end).
    pub next: u64,
}

impl Ifd {
    pub fn find(&self, tag: u16) -> Option<&IfdEntry> {
        self.entries.iter().find(|e| e.tag == tag)
    }
}

/// Read a u16 at `at` in the file's byte order.
fn u16_at(b: &[u8], at: usize, little: bool) -> u16 {
    let v = [b[at], b[at + 1]];
    if little { u16::from_le_bytes(v) } else { u16::from_be_bytes(v) }
}

/// Read a u32 at `at` in the file's byte order.
fn u32_at(b: &[u8], at: usize, little: bool) -> u32 {
    let mut v = [0u8; 4];
    v.copy_from_slice(&b[at..at + 4]);
    if little { u32::from_le_bytes(v) } else { u32::from_be_bytes(v) }
}

fn u64_at(b: &[u8], at: usize, little: bool) -> u64 {
    let mut v = [0u8; 8];
    v.copy_from_slice(&b[at..at + 8]);
    if little { u64::from_le_bytes(v) } else { u64::from_be_bytes(v) }
}

/// Parse the TIFF/BigTIFF header. `size` is the source size.
pub fn read_header(src: &dyn ByteSource) -> CoreResult<TiffHeader> {
    let size = src.size();
    if size < 8 {
        return Err(CoreError::header("文件小于 8 字节，不是 TIFF"));
    }
    let b = src.read_at(0, 8)?;
    let little = match &b[0..2] {
        b"II" => true,
        b"MM" => false,
        _ => return Err(CoreError::variant("TIFF 字节序标记既不是 II 也不是 MM")),
    };
    let version = u16_at(&b, 2, little);
    match version {
        42 => {
            let first = u32_at(&b, 4, little) as u64;
            Ok(TiffHeader { little, kind: TiffKind::Classic, first_ifd: first, size })
        }
        43 => {
            if size < 16 {
                return Err(CoreError::header("BigTIFF 头截断（<16 字节）"));
            }
            let b = src.read_at(0, 16)?;
            let offsize = u16_at(&b, 4, little);
            if offsize != 8 {
                return Err(CoreError::variant(format!(
                    "BigTIFF offset size {offsize} ≠ 8"
                )));
            }
            if u16_at(&b, 6, little) != 0 {
                return Err(CoreError::variant("BigTIFF reserved 字段非 0"));
            }
            let first = u64_at(&b, 8, little);
            Ok(TiffHeader { little, kind: TiffKind::BigTiff, first_ifd: first, size })
        }
        v => Err(CoreError::variant(format!(
            "TIFF 版本 {v} 既不是 42（classic）也不是 43（BigTIFF）"
        ))),
    }
}

/// Read one IFD at `at`. Bounds-checked entry table, bounded entry count.
pub fn read_ifd(src: &dyn ByteSource, hdr: &TiffHeader, at: u64) -> CoreResult<Ifd> {
    if at == 0 {
        return Err(CoreError::validation("IFD 偏移为 0"));
    }
    let esize: u64 = match hdr.kind {
        TiffKind::Classic => 2,  // u16 entry count
        TiffKind::BigTiff => 8, // u64 entry count
    };
    let ent_size: u64 = match hdr.kind {
        TiffKind::Classic => 12,
        TiffKind::BigTiff => 20,
    };
    let count_off = at
        .checked_add(esize)
        .ok_or_else(|| CoreError::validation("IFD 偏移溢出"))?;
    if count_off > hdr.size {
        return Err(CoreError::oob(format!("IFD 偏移 {at} 越界（文件 {} 字节）", hdr.size)));
    }
    let hb = src.read_at(at, esize as usize)?;
    let n = match hdr.kind {
        TiffKind::Classic => u16_at(&hb, 0, hdr.little) as u64,
        TiffKind::BigTiff => u64_at(&hb, 0, hdr.little),
    };
    if n == 0 || n > MAX_IFD_ENTRIES as u64 {
        return Err(CoreError::validation(format!("IFD 条目数 {n} 异常")));
    }
    let table_len = n
        .checked_mul(ent_size)
        .and_then(|l| l.checked_add(esize))
        .and_then(|l| l.checked_add(match hdr.kind { TiffKind::Classic => 4, TiffKind::BigTiff => 8 }))
        .ok_or_else(|| CoreError::validation("IFD 表长度溢出"))?;
    if at.checked_add(table_len).is_none_or(|e| e > hdr.size) {
        return Err(CoreError::oob("IFD 条目表越界"));
    }
    let mut entries = Vec::with_capacity(n as usize);
    let mut pos = at + esize;
    let mut remaining = n as usize;
    while remaining > 0 {
        let take = remaining.min(ENTRY_CHUNK);
        let buf = src.read_at(pos, take * ent_size as usize)?;
        for i in 0..take {
            let e = i * ent_size as usize;
            let (tag, typ, count, val) = match hdr.kind {
                TiffKind::Classic => {
                    let tag = u16_at(&buf, e, hdr.little);
                    let typ = u16_at(&buf, e + 2, hdr.little);
                    let count = u32_at(&buf, e + 4, hdr.little) as u64;
                    let mut val = [0u8; 8];
                    val[..4].copy_from_slice(&buf[e + 8..e + 12]);
                    (tag, typ, count, val)
                }
                TiffKind::BigTiff => {
                    let tag = u16_at(&buf, e, hdr.little);
                    let typ = u16_at(&buf, e + 2, hdr.little);
                    let count = u64_at(&buf, e + 4, hdr.little);
                    let mut val = [0u8; 8];
                    val.copy_from_slice(&buf[e + 12..e + 20]);
                    (tag, typ, count, val)
                }
            };
            entries.push(IfdEntry { tag, typ, count, val });
        }
        pos += take as u64 * ent_size;
        remaining -= take;
    }
    let next = match hdr.kind {
        TiffKind::Classic => u32_at(&src.read_at(pos, 4)?, 0, hdr.little) as u64,
        TiffKind::BigTiff => u64_at(&src.read_at(pos, 8)?, 0, hdr.little),
    };
    Ok(Ifd { offset: at, entries, next })
}

/// Walk the main IFD chain from `first`; rejects loops and overlong chains.
pub fn ifd_chain(src: &dyn ByteSource, hdr: &TiffHeader) -> CoreResult<Vec<Ifd>> {
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut next = hdr.first_ifd;
    while next != 0 {
        if out.len() >= MAX_IFDS {
            return Err(CoreError::validation("IFD 数超出上限（疑似链环）"));
        }
        if !seen.insert(next) {
            return Err(CoreError::validation(format!(
                "IFD 偏移 {next} 被重复引用（指针环）"
            )));
        }
        let ifd = read_ifd(src, hdr, next)?;
        next = ifd.next;
        out.push(ifd);
    }
    if out.is_empty() {
        return Err(CoreError::validation("无 IFD"));
    }
    Ok(out)
}

/// Resolve an entry's value bytes (inline value or external array), bounded
/// by [`MAX_TAG_VALUE_BYTES`] and the file size.
pub fn entry_value(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    e: &IfdEntry,
) -> CoreResult<Vec<u8>> {
    let ts = type_size(e.typ).ok_or_else(|| {
        CoreError::variant(format!("tag {} 类型 {} 未知", e.tag, e.typ))
    })?;
    let len = e
        .count
        .checked_mul(ts)
        .ok_or_else(|| CoreError::oob(format!("tag {} 值长度溢出", e.tag)))?;
    if len > MAX_TAG_VALUE_BYTES {
        return Err(CoreError::oob(format!(
            "tag {} 值 {len} 字节超出读取上限",
            e.tag
        )));
    }
    let inline = match hdr.kind {
        TiffKind::Classic => 4,
        TiffKind::BigTiff => 8,
    };
    if len as u64 <= inline as u64 {
        return Ok(e.val[..len as usize].to_vec());
    }
    let off = match hdr.kind {
        TiffKind::Classic => u32_at(&e.val, 0, hdr.little) as u64,
        TiffKind::BigTiff => u64_at(&e.val, 0, hdr.little),
    };
    let end = off
        .checked_add(len)
        .ok_or_else(|| CoreError::oob(format!("tag {} 值区间溢出", e.tag)))?;
    if end > hdr.size {
        return Err(CoreError::oob(format!(
            "tag {} 值 [{off},+{len}) 超出文件 {} 字节",
            e.tag, hdr.size
        )));
    }
    src.read_at(off, len as usize)
}

/// Scalar integer value of an entry: SHORT/LONG/LONG8 (and IFD types).
pub fn entry_u64(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    e: &IfdEntry,
) -> CoreResult<u64> {
    if e.count != 1 {
        return Err(CoreError::validation(format!(
            "tag {} 需要单值（count={}）",
            e.tag, e.count
        )));
    }
    match e.typ {
        3 => Ok(u16_at(&e.val, 0, hdr.little) as u64),
        4 | 13 => Ok(u32_at(&e.val, 0, hdr.little) as u64),
        16 | 18 => Ok(u64_at(&e.val, 0, hdr.little)),
        t => Err(CoreError::variant(format!(
            "tag {} 类型 {t} 不是整数标量",
            e.tag
        ))),
    }
}

/// Convenience: optional scalar with an expected type set.
pub fn find_u64(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    tag: u16,
) -> CoreResult<Option<u64>> {
    match ifd.find(tag) {
        None => Ok(None),
        Some(e) => entry_u64(src, hdr, e).map(Some),
    }
}

/// SubIFD offsets (tag 330): LONG/IFD or LONG8/IFD8, count bounded.
pub fn subifd_offsets(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
) -> CoreResult<Vec<u64>> {
    let Some(e) = ifd.find(330) else { return Ok(Vec::new()) };
    if e.count > MAX_IFDS as u64 {
        return Err(CoreError::validation(format!("SubIFDs 数 {} 异常", e.count)));
    }
    let bytes = entry_value(src, hdr, e)?;
    Ok(match e.typ {
        4 | 13 => bytes
            .chunks_exact(4)
            .map(|c| {
                let v = [c[0], c[1], c[2], c[3]];
                if hdr.little { u32::from_le_bytes(v) as u64 } else { u32::from_be_bytes(v) as u64 }
            })
            .collect(),
        16 | 18 => bytes
            .chunks_exact(8)
            .map(|c| {
                let mut v = [0u8; 8];
                v.copy_from_slice(c);
                if hdr.little { u64::from_le_bytes(v) } else { u64::from_be_bytes(v) }
            })
            .collect(),
        t => {
            return Err(CoreError::variant(format!(
                "SubIFDs 类型 {t} 非法"
            )))
        }
    })
}

/// Streaming reader over the TileOffsets (324) / TileByteCounts (325) arrays
/// of one IFD. Memory is O([`TILE_PAIR_CHUNK`]) pairs, never the whole array;
/// source reads are chunked so a wasm host sees few, bounded calls.
pub struct TileCursor<'a> {
    src: &'a dyn ByteSource,
    hdr: &'a TiffHeader,
    /// (array data offset, count, element type); `None` data offset marks the
    /// inline case (value bytes live in the entry's own value field).
    offs: (Option<u64>, u64, u16),
    cnts: (Option<u64>, u64, u16),
    /// Copies of the two entries for the inline case.
    off_e: IfdEntry,
    cnt_e: IfdEntry,
    total: u64,
    done: u64,
    buf: Vec<(u64, u64)>,
    buf_at: usize,
}

/// Decode `n` elements of `typ` from `raw` (file byte order).
fn decode_ints(raw: &[u8], typ: u16, little: bool, n: usize) -> CoreResult<Vec<u64>> {
    let ts = type_size(typ).unwrap() as usize;
    if raw.len() < n * ts {
        return Err(CoreError::oob("tile 数组读取长度不足"));
    }
    Ok((0..n)
        .map(|i| {
            let at = i * ts;
            match ts {
                2 => u16_at(raw, at, little) as u64,
                4 => u32_at(raw, at, little) as u64,
                _ => u64_at(raw, at, little),
            }
        })
        .collect())
}

impl<'a> TileCursor<'a> {
    /// Build from tags 324/325; counts must be ≥ 1 and equal for both.
    pub fn new(
        src: &'a dyn ByteSource,
        hdr: &'a TiffHeader,
        ifd: &Ifd,
    ) -> CoreResult<TileCursor<'a>> {
        let (oe, ce) = (ifd.find(324), ifd.find(325));
        let (Some(oe), Some(ce)) = (oe, ce) else {
            return Err(CoreError::validation("IFD 缺少 TileOffsets/TileByteCounts"));
        };
        if oe.count != ce.count || oe.count == 0 {
            return Err(CoreError::validation(format!(
                "TileOffsets/TileByteCounts 数不一致（{}/{}）",
                oe.count, ce.count
            )));
        }
        // guard: tile count itself bounded (per-IFD tile grids are ≤ ~2^31)
        if oe.count > 2_000_000_000 {
            return Err(CoreError::oob(format!("tile 数 {} 越界", oe.count)));
        }
        let arr = |e: &IfdEntry| -> CoreResult<(Option<u64>, u64, u16)> {
            if ![3u16, 4, 16].contains(&e.typ) {
                return Err(CoreError::variant(format!(
                    "tile 数组类型 {} 非法（需 SHORT/LONG/LONG8）",
                    e.typ
                )));
            }
            let ts = type_size(e.typ).unwrap();
            let len = e
                .count
                .checked_mul(ts)
                .ok_or_else(|| CoreError::oob("tile 数组长度溢出"))?;
            let inline = match hdr.kind {
                TiffKind::Classic => 4,
                TiffKind::BigTiff => 8,
            };
            if len <= inline as u64 {
                return Ok((None, e.count, e.typ));
            }
            let off = match hdr.kind {
                TiffKind::Classic => u32_at(&e.val, 0, hdr.little) as u64,
                TiffKind::BigTiff => u64_at(&e.val, 0, hdr.little),
            };
            let end = off
                .checked_add(len)
                .ok_or_else(|| CoreError::oob("tile 数组区间溢出"))?;
            if end > hdr.size {
                return Err(CoreError::oob(format!(
                    "tile 数组 [{off},+{len}) 越界（文件 {}）",
                    hdr.size
                )));
            }
            Ok((Some(off), e.count, e.typ))
        };
        Ok(TileCursor {
            src,
            hdr,
            offs: arr(oe)?,
            cnts: arr(ce)?,
            off_e: *oe,
            cnt_e: *ce,
            total: oe.count,
            done: 0,
            buf: Vec::new(),
            buf_at: 0,
        })
    }

    pub fn total(&self) -> u64 {
        self.total
    }

    pub fn done(&self) -> u64 {
        self.done
    }

    /// Skip forward without materialising pairs (resume fast-forward).
    pub fn seek(&mut self, to: u64) -> CoreResult<()> {
        if to > self.total {
            return Err(CoreError::validation("tile 游标 seek 越界"));
        }
        self.done = to;
        self.buf.clear();
        self.buf_at = 0;
        Ok(())
    }

    fn fill(&mut self) -> CoreResult<()> {
        let want = ((self.total - self.done) as usize).min(TILE_PAIR_CHUNK);
        if want == 0 {
            return Ok(());
        }
        let read_arr = |data: Option<u64>, e: &IfdEntry, typ: u16| -> CoreResult<Vec<u64>> {
            let ts = type_size(typ).unwrap();
            match data {
                None => {
                    // inline: elements sit in the entry's value field
                    decode_ints(&e.val, typ, self.hdr.little, want)
                }
                Some(off) => {
                    let at = off
                        .checked_add(self.done * ts)
                        .ok_or_else(|| CoreError::oob("tile 数组元素偏移溢出"))?;
                    let raw = self.src.read_at(at, want * ts as usize)?;
                    decode_ints(&raw, typ, self.hdr.little, want)
                }
            }
        };
        let offs = read_arr(self.offs.0, &self.off_e, self.offs.2)?;
        let cnts = read_arr(self.cnts.0, &self.cnt_e, self.cnts.2)?;
        let mut buf = Vec::with_capacity(want);
        for i in 0..want {
            buf.push((offs[i], cnts[i]));
        }
        self.buf = buf;
        self.buf_at = 0;
        Ok(())
    }

    /// Next (payload offset, payload length) pair; `None` at the end. Every
    /// returned interval is bounds-checked against the file size.
    pub fn next_pair(&mut self) -> CoreResult<Option<(u64, u64)>> {
        match self.next_pair_allow_zero()? {
            Some((o, c)) if o == 0 || c == 0 => Err(CoreError::oob(format!(
                "tile {} 偏移/长度为 0（{o}/{c}）",
                self.done - 1
            ))),
            other => Ok(other),
        }
    }

    /// Next (offset,count) pair WITHOUT the zero rejection — the Leica SCN
    /// sparse-grid quirk (F4) encodes a missing tile as the (0, 0) entry.
    /// Non-zero intervals are still bounds-checked against the file size;
    /// half-zero records ((0,c)/(o,0)) pass through so the caller decides
    /// (the SCN adapter treats them as corruption, not absence).
    pub fn next_pair_allow_zero(&mut self) -> CoreResult<Option<(u64, u64)>> {
        if self.buf_at >= self.buf.len() {
            if self.done >= self.total {
                return Ok(None);
            }
            self.fill()?;
            if self.buf.is_empty() {
                return Ok(None);
            }
        }
        let (o, c) = self.buf[self.buf_at];
        self.buf_at += 1;
        self.done += 1;
        if o == 0 && c == 0 {
            return Ok(Some((0, 0)));
        }
        let end = o
            .checked_add(c)
            .ok_or_else(|| CoreError::oob(format!("tile {} 区间溢出", self.done - 1)))?;
        if end > self.hdr.size {
            return Err(CoreError::oob(format!(
                "tile {} 区间 [{o},+{c}) 超出文件 {}",
                self.done - 1,
                self.hdr.size
            )));
        }
        Ok(Some((o, c)))
    }
}

/// Materialise all (offset,count) pairs of an IFD (probe estimates, tests).
/// Streaming conversion uses [`TileCursor`] instead; the materialised path
/// exists for bounded uses and is capped by the file itself.
pub fn tile_pairs(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
) -> CoreResult<Vec<(u64, u64)>> {
    let mut cur = TileCursor::new(src, hdr, ifd)?;
    let mut out = Vec::with_capacity(cur.total().min(1 << 22) as usize);
    while let Some(p) = cur.next_pair()? {
        out.push(p);
    }
    Ok(out)
}
