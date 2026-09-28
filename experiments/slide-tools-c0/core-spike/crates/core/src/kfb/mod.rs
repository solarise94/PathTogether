//! KFB brightfield parsing: shared types + dispatch, ports of
//! `kfb/parser.py` (synthetic `kfb_bf_v1` contract) and
//! `kfb/vendor_kfbio.py` (KF-BIO/江丰 vendor layout used by real samples).

pub mod synth;
pub mod vendor;

use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, ScratchFactory};
use crate::paged_index::PagedGridIndex;

pub const MAGIC: [u8; 8] = [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00];
pub const VERSION: u32 = 1;
pub const TILE_W: u32 = 256;
pub const TILE_H: u32 = 256;

pub const HEADER_MIN_BYTES: u64 = 96;
pub const HEADER_MAX_BYTES: u64 = 4096;
pub const MAX_DIM_PX: u32 = 200_000;
pub const MAX_LEVEL_COUNT: u32 = 16;
pub const MAX_TILE_COUNT: u32 = 2_000_000;
pub const MAX_ASSOCIATED_COUNT: u32 = 8;
pub const MIN_PAYLOAD_LENGTH: u32 = 1;
pub const MAX_PAYLOAD_LENGTH: u32 = 8 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct KfbHeader {
    pub version: u32,
    pub width_px: u32,
    pub height_px: u32,
    pub tile_w: u32,
    pub tile_h: u32,
    pub level_count: u32,
    pub tile_count: u32,
    pub mpp_x: f64,
    pub mpp_y: f64,
    pub objective: f64,
    pub scanner_id: String,
    pub associated_count: u32,
    pub index_offset: u64,
    pub brightfield: bool,
}

#[derive(Debug, Clone, Copy)]
pub struct KfbLevel {
    pub level: u32,
    pub width: u32,
    pub height: u32,
}

impl KfbLevel {
    pub fn tiles_across(&self) -> u32 {
        (self.width + TILE_W - 1) / TILE_W
    }
    pub fn tiles_down(&self) -> u32 {
        (self.height + TILE_H - 1) / TILE_H
    }
}

#[derive(Debug, Clone)]
pub struct KfbAssociated {
    pub name: String,
    pub payload_offset: u64,
    pub payload_length: u32,
    pub width: u32,
    pub height: u32,
}

/// Parsed document. Tile records live in the paged grid index (on scratch
/// storage), not in memory; payload bytes are fetched on demand from `src`.
pub struct KfbDocument {
    pub header: KfbHeader,
    pub levels: Vec<KfbLevel>,
    pub grids: PagedGridIndex,
    pub associated: Vec<KfbAssociated>,
    pub source_size: u64,
}

/// Checked payload bound (port of `parser._check_payload`): `offset+length`
/// must fall entirely inside the file; the addition itself is checked, so a
/// u64 near `MAX` fails cleanly instead of wrapping.
pub fn check_payload(size: u64, offset: i64, length: u32) -> CoreResult<(u64, u64)> {
    if offset < 0 || length == 0 {
        return Err(CoreError::oob(format!("负 offset/length（{offset},{}）", length)));
    }
    let off = offset as u64;
    let end = off
        .checked_add(length as u64)
        .ok_or_else(|| CoreError::oob(format!("payload [{off},+{length}) 加法溢出")))?;
    if off >= size || end > size {
        return Err(CoreError::oob(format!(
            "payload [{off},{end}) 越出文件长度 {size}"
        )));
    }
    Ok((off, end))
}

/// SOI..EOI skeleton check (port of `parser._check_jpeg_bounds`); reads only
/// the first/last two bytes of the payload.
pub fn check_jpeg_bounds(src: &dyn ByteSource, off: u64, len: u32) -> CoreResult<()> {
    if len < 4 {
        return Err(CoreError::jpeg("payload JPEG 骨架残缺（SOI/EOI）"));
    }
    let head = src.read_at(off, 2)?;
    let tail = src.read_at(off + len as u64 - 2, 2)?;
    if head != [0xFF, 0xD8] || tail != [0xFF, 0xD9] {
        return Err(CoreError::jpeg("payload JPEG 骨架残缺（SOI/EOI）"));
    }
    Ok(())
}

pub fn le_u16(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}
pub fn le_u32(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}
pub fn le_u64(b: &[u8], at: usize) -> u64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[at..at + 8]);
    u64::from_le_bytes(a)
}
pub fn le_f32(b: &[u8], at: usize) -> f32 {
    f32::from_bits(le_u32(b, at))
}
pub fn le_f64(b: &[u8], at: usize) -> f64 {
    f64::from_bits(le_u64(b, at))
}

pub use le_f32 as read_f32;
pub use le_f64 as read_f64;
pub use le_u16 as read_u16;
pub use le_u32 as read_u32;
pub use le_u64 as read_u64;

/// ASCII NUL-terminated string, non-ASCII bytes replaced (Python
/// `decode("ascii", "replace")` → U+FFFD per bad byte).
pub fn ascii_replace(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len());
    match bytes.iter().position(|&b| b == 0) {
        Some(n) => push_ascii_lossy(&mut s, &bytes[..n]),
        None => push_ascii_lossy(&mut s, bytes),
    }
    s
}

fn push_ascii_lossy(s: &mut String, bytes: &[u8]) {
    for &b in bytes {
        if b < 0x80 {
            s.push(b as char);
        } else {
            s.push('\u{FFFD}');
        }
    }
}

/// Parse entry point (port of `parser.parse_kfb`): dispatches on vendor
/// layout, validates everything the Python oracle validates, and spills the
/// tile grid to scratch storage. All integer arithmetic on offsets/lengths
/// is checked; caps on tile/level counts bound the in-memory state.
pub fn parse_kfb(
    src: &dyn ByteSource,
    scratch: &mut dyn ScratchFactory,
) -> CoreResult<KfbDocument> {
    let size = src.size();
    if size < HEADER_MIN_BYTES {
        return Err(CoreError::header(format!(
            "文件过小（{size} < {HEADER_MIN_BYTES}）"
        )));
    }
    let head = src.read_at(0, HEADER_MIN_BYTES as usize)?;
    if head[0..8] != MAGIC {
        return Err(CoreError::variant("未知 KFB magic"));
    }
    if vendor::looks_like_vendor(&head) {
        vendor::parse_vendor(src, scratch)
    } else {
        synth::parse_synth(src, scratch)
    }
}
