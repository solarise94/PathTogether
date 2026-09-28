//! Paged, spill-to-scratch tile index.
//!
//! The Python oracle materializes every tile entry as a Python object
//! (`doc.tiles` list). This spike instead scatters validated tile records
//! into per-level fixed-record grid files (32 B/cell) obtained from a
//! `ScratchFactory`, keeping in memory only:
//!   - one occupancy bit per grid cell (duplicate/coverage checks),
//!   - a per-level present count (u32),
//! which is bounded by the format caps (levels ≤ 16, dims ≤ 200_000 px →
//! ≤ 782×782 cells → ≤ 1.2 MiB of bitmaps total) regardless of file size.
//! Conversion streams the grid files back sequentially in fixed pages, so
//! tile-count-proportional memory is never required.

use crate::error::{CoreError, CoreResult};
use crate::io::ScratchSink;

pub const REC_BYTES: usize = 32;
const EMPTY_X: u32 = u32::MAX;

/// Sequential page size used when streaming the grid back (records per page).
pub const PAGE_RECS: usize = 512;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TileRec {
    pub level: u8,
    pub x: u32,
    pub y: u32,
    pub jpeg_w: u16,
    pub jpeg_h: u16,
    pub payload_offset: u64,
    pub payload_length: u32,
}

impl TileRec {
    pub fn is_full_tile(&self) -> bool {
        self.jpeg_w == 256 && self.jpeg_h == 256
    }
    fn encode(&self) -> [u8; REC_BYTES] {
        let mut b = [0u8; REC_BYTES];
        b[0] = self.level;
        b[1..5].copy_from_slice(&self.x.to_le_bytes());
        b[5..9].copy_from_slice(&self.y.to_le_bytes());
        b[9..11].copy_from_slice(&self.jpeg_w.to_le_bytes());
        b[11..13].copy_from_slice(&self.jpeg_h.to_le_bytes());
        b[13..21].copy_from_slice(&self.payload_offset.to_le_bytes());
        b[21..25].copy_from_slice(&self.payload_length.to_le_bytes());
        b
    }
    fn decode(b: &[u8]) -> Option<TileRec> {
        if b.len() < REC_BYTES {
            return None;
        }
        let x = u32::from_le_bytes([b[1], b[2], b[3], b[4]]);
        if x == EMPTY_X {
            return None;
        }
        Some(TileRec {
            level: b[0],
            x,
            y: u32::from_le_bytes([b[5], b[6], b[7], b[8]]),
            jpeg_w: u16::from_le_bytes([b[9], b[10]]),
            jpeg_h: u16::from_le_bytes([b[11], b[12]]),
            payload_offset: u64::from_le_bytes([
                b[13], b[14], b[15], b[16], b[17], b[18], b[19], b[20],
            ]),
            payload_length: u32::from_le_bytes([b[21], b[22], b[23], b[24]]),
        })
    }
}

struct LevelStore {
    sink: Box<dyn ScratchSink>,
    cells: u32,
    /// One bit per grid cell; set = tile present.
    occupied: Vec<u64>,
    present: u32,
}

impl LevelStore {
    fn is_set(&self, bit: usize) -> bool {
        self.occupied[bit / 64] & (1u64 << (bit % 64)) != 0
    }
    fn set(&mut self, bit: usize) {
        self.occupied[bit / 64] |= 1u64 << (bit % 64);
    }
}

/// Per-level sparse-in-file grid index over validated tile records.
pub struct PagedGridIndex {
    stores: Vec<LevelStore>,
    /// (level, width, height), ascending level order.
    levels: Vec<(u32, u32, u32)>,
    tiles_across: Vec<u32>,
    pub total_kept: u64,
}

impl PagedGridIndex {
    /// One scratch sink is created per level (named `grid-l{level}`) and
    /// pre-sized to `cells * 32` bytes (zero-filled by truncate).
    pub fn create(
        levels: &[(u32, u32, u32)],
        tiles_across: &[u32],
        tiles_down: &[u32],
        scratch: &mut dyn crate::io::ScratchFactory,
    ) -> CoreResult<Self> {
        assert_eq!(levels.len(), tiles_across.len());
        assert_eq!(levels.len(), tiles_down.len());
        let mut stores = Vec::with_capacity(levels.len());
        for (i, &(level, _w, _h)) in levels.iter().enumerate() {
            let cells = (tiles_across[i] as u64) * (tiles_down[i] as u64);
            let cells =
                u32::try_from(cells).map_err(|_| CoreError::index("grid cells 超出 u32"))?;
            let mut sink = scratch.create(&format!("grid-l{level}"))?;
            sink.truncate(cells as u64 * REC_BYTES as u64)?;
            let words = cells as usize / 64 + 1;
            stores.push(LevelStore {
                sink,
                cells,
                occupied: vec![0u64; words],
                present: 0,
            });
        }
        Ok(PagedGridIndex {
            stores,
            levels: levels.to_vec(),
            tiles_across: tiles_across.to_vec(),
            total_kept: 0,
        })
    }

    fn store_index(&self, level: u8) -> CoreResult<usize> {
        self.levels
            .iter()
            .position(|&(l, _, _)| l == level as u32)
            .ok_or_else(|| CoreError::index(format!("level={level} 越界")))
    }

    /// Insert a validated record; duplicate grid cells are rejected (same
    /// error class as the oracle's `tile[%d] 网格单元重复`).
    pub fn put(&mut self, rec: TileRec) -> CoreResult<()> {
        let si = self.store_index(rec.level)?;
        let row = rec.y / 256;
        let col = rec.x / 256;
        let ta = self.tiles_across[si] as u64;
        let bit = (row as u64) * ta + col as u64;
        let bit = usize::try_from(bit).map_err(|_| CoreError::index("cell index overflow"))?;
        let store = &mut self.stores[si];
        if bit >= store.cells as usize {
            return Err(CoreError::index(format!(
                "cell ({row},{col}) 超出层 {} 网格 {}",
                rec.level, store.cells
            )));
        }
        if store.is_set(bit) {
            return Err(CoreError::index(format!(
                "网格单元 ({},{},{}) 重复",
                rec.level, row, col
            )));
        }
        store.set(bit);
        store.present += 1;
        store.sink.write_at(bit as u64 * REC_BYTES as u64, &rec.encode())?;
        self.total_kept += 1;
        Ok(())
    }

    pub fn present_count(&self, level: u32) -> u32 {
        self.store_index(level as u8)
            .map(|i| self.stores[i].present)
            .unwrap_or(0)
    }

    pub fn cells(&self, level: u32) -> CoreResult<u32> {
        let si = self.store_index(level as u8)?;
        Ok(self.stores[si].cells)
    }

    /// Read the record at `cell`, or `None` if the cell has no tile.
    pub fn read_cell(&self, level: u32, cell: u32) -> CoreResult<Option<TileRec>> {
        let si = self.store_index(level as u8)?;
        if cell >= self.stores[si].cells {
            return Err(CoreError::index("cell 越界"));
        }
        let buf = self.stores[si]
            .sink
            .read_at(cell as u64 * REC_BYTES as u64, REC_BYTES)?;
        Ok(TileRec::decode(&buf))
    }

    /// Stream all cells of a level in (row, col) order, page by page.
    /// `f(cell_index, record_or_none)` sees every grid slot exactly once.
    pub fn for_each_cell(
        &self,
        level: u32,
        mut f: impl FnMut(u32, Option<TileRec>) -> CoreResult<()>,
    ) -> CoreResult<()> {
        let si = self.store_index(level as u8)?;
        let store = &self.stores[si];
        let mut cell: u32 = 0;
        while cell < store.cells {
            let n = PAGE_RECS.min((store.cells - cell) as usize);
            let buf =
                store.sink.read_at(cell as u64 * REC_BYTES as u64, n * REC_BYTES)?;
            for (j, chunk) in buf.chunks_exact(REC_BYTES).enumerate() {
                f(cell + j as u32, TileRec::decode(chunk))?;
            }
            cell += n as u32;
        }
        Ok(())
    }
}
