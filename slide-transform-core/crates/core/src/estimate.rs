//! Output-size estimation for the host's disk precheck (plan §5): an upper
//! bound, not a promise — compression ratios are unknown, so lossless-safe
//! slack is included. Brightfield sums real payload lengths from the paged
//! index; fluorescence bounds payloads by the source size (channel payload
//! regions of real files are disjoint; a 1.25× slack covers overlaps).

use crate::error::CoreResult;
use crate::kfb::KfbDocument;
use crate::kfbf::KfbfDocument;

const TILE: u32 = 256;

#[derive(Debug, Clone, Copy)]
pub struct OutputEstimate {
    pub payload_bytes: u64,
    pub tiles_present: u64,
    pub cells_total: u64,
    pub cells_missing: u64,
    pub edge_tiles: u64,
    pub ifds: u64,
    /// Upper bound of the final output file size.
    pub output_upper_bound_bytes: u64,
}

fn bound(e: &mut OutputEstimate) {
    let tiles = e.cells_total.max(e.tiles_present);
    e.output_upper_bound_bytes = e
        .payload_bytes
        .saturating_mul(5)
        .div_ceil(4) // 1.25× slack on payload copies
        .saturating_add(e.cells_missing.saturating_mul(8 * 1024)) // black fills
        .saturating_add(e.edge_tiles.saturating_mul(200 * 1024)) // re-encode bound
        .saturating_add(tiles.saturating_mul(16)) // offset+count arrays
        .saturating_add(e.ifds.saturating_mul(4 * 1024)) // IFD bodies
        .saturating_add(1024 * 1024);
}

pub fn estimate_bf(doc: &KfbDocument, size: u64) -> CoreResult<OutputEstimate> {
    let mut e = OutputEstimate {
        payload_bytes: 0,
        tiles_present: 0,
        cells_total: 0,
        cells_missing: 0,
        edge_tiles: 0,
        ifds: 0,
        output_upper_bound_bytes: 0,
    };
    for lv in &doc.levels {
        let present = doc.grids.present_count(lv.level);
        if present == 0 {
            continue; // levels after the first missing one are not selected
        }
        let cells = lv.tiles_across() as u64 * lv.tiles_down() as u64;
        e.cells_total += cells;
        e.ifds += 1;
        let mut payload = 0u64;
        doc.grids.for_each_cell(lv.level, |_cell, rec| {
            if let Some(rec) = rec {
                payload += rec.payload_length as u64;
                if !rec.is_full_tile() {
                    e.edge_tiles += 1;
                }
            } else {
                e.cells_missing += 1;
            }
            Ok(())
        })?;
        e.payload_bytes = e.payload_bytes.saturating_add(payload);
        e.tiles_present += present as u64;
        if lv.tiles_across() == 1 && lv.tiles_down() == 1 {
            break; // selection rule of convert_bf::select_levels
        }
    }
    bound(&mut e);
    let _ = size;
    Ok(e)
}

pub fn estimate_fl(doc: &KfbfDocument, size: u64) -> OutputEstimate {
    let mut e = OutputEstimate {
        payload_bytes: size, // channel payloads bounded by source size + slack
        tiles_present: 0,
        cells_total: 0,
        cells_missing: 0,
        edge_tiles: 0,
        ifds: 0,
        output_upper_bound_bytes: 0,
    };
    let nch = doc.header.channel_count as u64;
    for lv in &doc.levels {
        let cells = lv.tiles_across() as u64 * lv.tiles_down() as u64;
        e.cells_total += cells * nch;
        e.ifds += nch;
        let present = doc.cells.present_count(lv.level).unwrap_or(0) as u64;
        e.tiles_present += present * nch;
        e.cells_missing += cells.saturating_sub(present) * nch;
        // cropped cells: index dims < full cell → re-encode path; edge count
        // needs per-cell dims, which read_cell serves from the paged store
        for cell in 0..cells {
            if let Ok(Some(rec)) = doc.cells.read_cell(lv.level, cell as u32) {
                let col = cell % lv.tiles_across() as u64;
                let row = cell / lv.tiles_across() as u64;
                let cw = TILE.min(lv.width - (col as u32 * TILE));
                let ch = TILE.min(lv.height - (row as u32 * TILE));
                if rec.jpeg_w != cw || rec.jpeg_h != ch {
                    e.edge_tiles += nch;
                }
            }
        }
    }
    bound(&mut e);
    e
}
