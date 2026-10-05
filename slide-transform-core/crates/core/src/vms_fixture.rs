//! Synthetic Hamamatsu VMS bundle generator (tests; `fixtures` feature).
//!
//! Builds a complete flat bundle (the `.vms` INI entry + sibling tile
//! JPEGs, optionally macro/map/.opt members) with knobbed geometry:
//! per-column widths / per-row heights (real VMS pads the right/bottom
//! edge files), restart rows, optional macro/map/.opt members, and fault
//! knobs (no restart markers, progressive header, missing member, VMU
//! group, multi-layer, traversal names). Deterministic pixel patterns only.
//!
//! Tile JPEGs are assembled from standalone restart-segment encodes (the
//! exact sub-rectangles the converter's segment decode rebuilds — the
//! segment roundtrip is pixel-exact by construction), with a GLOBAL level-0
//! pattern `f(ax, ay)` of absolute mosaic coordinates so the composed L0 is
//! coherent across tile seams and feature coordinates are known exactly.

use crate::bundle::MemBundle;
use crate::error::{CoreError, CoreResult};
use crate::jpeg::{encode_rgb, EncoderCfg, Sampling};
use crate::ndpi::scan_strip_head;

#[derive(Debug, Clone)]
pub struct VmsGenParams {
    pub stem: String,
    /// NoJpegColumns / NoJpegRows.
    pub cols: u32,
    pub rows: u32,
    /// Per-column tile width (MCU-aligned; edge columns may differ).
    pub widths: Vec<u32>,
    /// Per-row tile height (MCU-aligned; edge rows may differ).
    pub heights: Vec<u32>,
    /// MCU rows per restart segment (DRI = restart_rows × mcus_x).
    pub restart_rows: u32,
    pub quality: u8,
    /// Optional members generated and referenced.
    pub macro_image: bool,
    pub map_file: bool,
    pub opt_file: bool,
    /// PhysicalWidth/PhysicalHeight (nm) written to the INI.
    pub physical_nm: Option<(f64, f64)>,
    /// SourceLens written to the INI.
    pub source_lens: f64,
    // fault knobs
    /// Encode tile (0,0) WITHOUT restart markers (→ typed「无 restart」).
    pub no_restart: bool,
    /// Replace tile (0,0) with a bare progressive-JPEG header (typed SOF2).
    pub progressive: bool,
    /// Drop one referenced tile member from the bundle (缺成员列出拒绝).
    pub missing_member: bool,
    /// Write the VMU group ([Uncompressed Virtual Microscope Specimen]).
    pub vmu: bool,
    /// NoLayers=2 (多焦面，OpenSlide 同样只接受 1).
    pub multi_layer: bool,
    /// ImageFile(1,0) points outside the bundle (路径穿越被拒绝).
    pub traversal_name: bool,
}

impl Default for VmsGenParams {
    fn default() -> Self {
        VmsGenParams {
            stem: "synthetic".to_string(),
            cols: 2,
            rows: 2,
            widths: vec![96, 64],
            heights: vec![80, 48],
            restart_rows: 1,
            quality: 90,
            macro_image: false,
            map_file: false,
            opt_file: false,
            physical_nm: Some((100_000_000.0, 80_000_000.0)),
            source_lens: 40.0,
            no_restart: false,
            progressive: false,
            missing_member: false,
            vmu: false,
            multi_layer: false,
            traversal_name: false,
        }
    }
}

impl VmsGenParams {
    fn width_of(&self, col: u32) -> u32 {
        self.widths[col as usize % self.widths.len()]
    }
    fn height_of(&self, row: u32) -> u32 {
        self.heights[row as usize % self.heights.len()]
    }
    pub fn mosaic_dims(&self) -> (u64, u64) {
        (
            (0..self.cols).map(|c| self.width_of(c) as u64).sum(),
            (0..self.rows).map(|r| self.height_of(r) as u64).sum(),
        )
    }
    fn tile_name(&self, col: u32, row: u32) -> String {
        format!("{}-{}-{}.jpg", self.stem, col, row)
    }
}

/// Deterministic per-tile pixel content drawn from the GLOBAL level-0
/// pattern: `ax = tile x0 + x`, `ay = tile y0 + y` — identical whether a
/// pixel comes from this tile or its neighbour, so the composed L0 is
/// seamless across tile seams by construction and feature coordinates are
/// exact (a 5-px plus at every (ax ≡ 16, ay ≡ 16) mod 32).
fn tile_pixels(p: &VmsGenParams, col: u32, row: u32, w: u32, h: u32) -> Vec<u8> {
    let x0: u64 = (0..col).map(|c| p.width_of(c) as u64).sum();
    let y0: u64 = (0..row).map(|r| p.height_of(r) as u64).sum();
    let mut v = Vec::with_capacity((w * h * 3) as usize);
    for y in 0..h {
        for x in 0..w {
            let ax = (x0 + x as u64) as i64;
            let ay = (y0 + y as u64) as i64;
            // smooth diagonal gradient: ≤ 1 level per pixel step, no wrap
            let g = 40 + ((ax + ay) / 8).clamp(0, 120);
            let dx = (ax.rem_euclid(32) - 16).abs();
            let dy = (ay.rem_euclid(32) - 16).abs();
            if dx + dy <= 2 {
                v.extend_from_slice(&[255, 255, 255]);
            } else {
                let b = g as u8;
                v.extend_from_slice(&[b, (b as u16 + 17).min(255) as u8, (b as u16 + 43).min(255) as u8]);
            }
        }
    }
    v
}

/// Split an encoded JPEG at the SOS marker: (header [SOI…SOS], entropy […EOI))
fn split_stream(full: &[u8]) -> CoreResult<(Vec<u8>, Vec<u8>)> {
    let mut i = 2usize;
    let mut header_end = 0usize;
    while i + 4 <= full.len() {
        if full[i] != 0xFF {
            break;
        }
        while i < full.len() && full[i] == 0xFF {
            i += 1;
        }
        let m = full[i];
        i += 1;
        if m == 0xD9 || m == 0x01 || (0xD0..=0xD7).contains(&m) {
            continue;
        }
        if i + 2 > full.len() {
            return Err(CoreError::validation("fixture: 段截断"));
        }
        let ln = ((full[i] as usize) << 8) | full[i + 1] as usize;
        if m == 0xDA {
            header_end = i + ln;
            break;
        }
        i += ln;
    }
    if header_end == 0 || full.len() < 2 || &full[full.len() - 2..] != [0xFF, 0xD9] {
        return Err(CoreError::validation("fixture: 编码器输出缺 SOS/EOI"));
    }
    Ok((full[..header_end].to_vec(), full[header_end..full.len() - 2].to_vec()))
}

/// Encode one tile JPEG with row-aligned restart markers (S422: MCU 16×8;
/// the encoder's MCU layout matches the ndpi fixture's strip assembly).
/// Returns the stream plus the entropy-start byte offset of every MCU row
/// (the `.opt` records — the offset AFTER each row's RST marker, row 0's
/// being the header end, exactly what OpenSlide validates against).
fn build_tile(w: u32, h: u32, p: &VmsGenParams, col: u32, row: u32) -> CoreResult<(Vec<u8>, Vec<u64>)> {
    const MCU_W: u32 = 16;
    const MCU_H: u32 = 8;
    if w % MCU_W != 0 || h % MCU_H != 0 {
        return Err(CoreError::validation(format!(
            "fixture: tile 尺寸 {w}×{h} 未按 MCU 网格对齐（宽需 {MCU_W} 的倍数，高需 {MCU_H} 的倍数）"
        )));
    }
    let px = tile_pixels(p, col, row, w, h);
    if p.no_restart && col == 0 && row == 0 {
        let cfg = EncoderCfg::with_quality(p.quality, Sampling::S422);
        return Ok((encode_rgb(&px, w, h, &cfg)?, Vec::new()));
    }
    let mcus_x = w / MCU_W;
    let mcus_y = h / MCU_H;
    let seg_rows_mcu = p.restart_rows.max(1);
    let dri = seg_rows_mcu * mcus_x; // MCUs per restart segment
    let seg_px_rows = seg_rows_mcu * MCU_H;
    let cfg = EncoderCfg::with_quality(p.quality, Sampling::S422);
    let mut out: Vec<u8> = Vec::new();
    let mut row_offsets: Vec<u64> = Vec::new();
    let mut header_done = false;
    let mut rst = 0u8;
    let mut sy = 0u32;
    while sy < mcus_y {
        // seg_px_rows is in PIXEL rows: convert to MCU rows first (mirrors
        // the ndpi fixture's strip assembly)
        let rows = (seg_px_rows / MCU_H).min(mcus_y - sy) * MCU_H;
        let row0 = (sy * MCU_H) as usize;
        let take = rows as usize * w as usize * 3;
        let full = encode_rgb(
            &px[row0 * w as usize * 3..row0 * w as usize * 3 + take],
            w,
            rows,
            &cfg,
        )?;
        let (seg_header, entropy) = split_stream(&full)?;
        if !header_done {
            // insert the DRI segment before the SOS marker, then patch the
            // SOF dims to the tile's own dims
            let sh = scan_strip_head(&full)?;
            let mut at = None;
            for i in 0..seg_header.len().saturating_sub(1) {
                if seg_header[i] == 0xFF && seg_header[i + 1] == 0xDA {
                    at = Some(i);
                    break;
                }
            }
            let at = at.ok_or_else(|| CoreError::validation("fixture: 缺 SOS"))?;
            let mut head = seg_header.clone();
            head[sh.sof_hw_at..sh.sof_hw_at + 2].copy_from_slice(&(h as u16).to_be_bytes());
            head[sh.sof_hw_at + 2..sh.sof_hw_at + 4].copy_from_slice(&(w as u16).to_be_bytes());
            head.truncate(sh.header_len);
            out.extend_from_slice(&head[..at]);
            out.extend_from_slice(&[0xFF, 0xDD, 0x00, 0x04, (dri >> 8) as u8, (dri & 0xFF) as u8]);
            out.extend_from_slice(&head[at..]);
            header_done = true;
        }
        if sy > 0 {
            out.extend_from_slice(&[0xFF, 0xD0 + (rst & 7)]);
            rst += 1;
        }
        row_offsets.push(out.len() as u64);
        out.extend_from_slice(&entropy);
        sy += rows / MCU_H;
    }
    out.extend_from_slice(&[0xFF, 0xD9]);
    if p.progressive && col == 0 && row == 0 {
        // bare progressive header (typed SOF2 rejection path)
        return Ok((vec![
            0xFF, 0xD8, 0xFF, 0xC2, 0x00, 0x11, 0x08, 0x00, 0x08, 0x01, 0x00, 0x03, 0x01,
            0x11, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01, 0xFF, 0xD9,
        ], Vec::new()));
    }
    Ok((out, row_offsets))
}

/// Assemble the bundle (members: `<stem>.vms`, `<stem>-<c>-<r>.jpg` per
/// tile, optional `<stem>_macro.jpg` / `<stem>_map.jpg` / `<stem>.opt`).
/// All names are FLAT — the VMS layout is the entry's sibling files.
pub fn build_synthetic_vms(p: &VmsGenParams) -> CoreResult<MemBundle> {
    if p.cols == 0 || p.rows == 0 || p.cols > 64 || p.rows > 64 {
        return Err(CoreError::validation("fixture: 网格参数需在 1..=64"));
    }
    let group = if p.vmu {
        "Uncompressed Virtual Microscope Specimen"
    } else {
        "Virtual Microscope Specimen"
    };
    let mut ini = String::new();
    ini.push_str(&format!("[{group}]\n"));
    ini.push_str(&format!("NoLayers={}\n", if p.multi_layer { 2 } else { 1 }));
    ini.push_str(&format!("NoJpegColumns={}\n", p.cols));
    ini.push_str(&format!("NoJpegRows={}\n", p.rows));
    for r in 0..p.rows {
        for c in 0..p.cols {
            let name = if p.traversal_name && c == 1 && r == 0 {
                "../evil.jpg".to_string()
            } else {
                p.tile_name(c, r)
            };
            if c == 0 && r == 0 {
                ini.push_str(&format!("ImageFile={name}\n"));
            } else {
                ini.push_str(&format!("ImageFile({c},{r})={name}\n"));
            }
        }
    }
    let macro_name = format!("{}_macro.jpg", p.stem);
    let map_name = format!("{}_map.jpg", p.stem);
    let opt_name = format!("{}.opt", p.stem);
    if p.macro_image {
        ini.push_str(&format!("MacroImage={macro_name}\n"));
    }
    if p.map_file {
        ini.push_str(&format!("MapFile={map_name}\n"));
    }
    if p.opt_file {
        ini.push_str(&format!("OptimisationFile={opt_name}\n"));
    }
    ini.push_str(&format!("SourceLens={:.6}\n", p.source_lens));
    if let Some((pw, ph)) = p.physical_nm {
        ini.push_str(&format!("PhysicalWidth={pw:.0}\n"));
        ini.push_str(&format!("PhysicalHeight={ph:.0}\n"));
    }
    ini.push_str("LayerSpacing=2032236150\nAuthCode=635108335\n");

    let mut out = MemBundle::new();
    out.push(&format!("{}.vms", p.stem), ini.into_bytes());
    let mut opt_records: Vec<u64> = Vec::new();
    for r in 0..p.rows {
        for c in 0..p.cols {
            let name = p.tile_name(c, r);
            if p.traversal_name && c == 1 && r == 0 {
                continue; // never staged: the probe must list it as missing
            }
            if p.missing_member && c == p.cols - 1 && r == p.rows - 1 {
                continue; // drop the LAST tile (a non-(0,0) member)
            }
            let (jpg, row_offsets) = build_tile(p.width_of(c), p.height_of(r), p, c, r)?;
            opt_records.extend_from_slice(&row_offsets);
            out.push(&name, jpg);
        }
    }
    if p.macro_image {
        // small deterministic macro (64×48 = MCU-aligned; OpenSlide's VMS
        // driver validates the macro JPEG as well, so it carries restart
        // markers like every other image in the bundle)
        let mp = VmsGenParams { quality: 85, ..p.clone() };
        let (jpg, _) = build_tile(64, 48, &mp, 0, 0)?;
        out.push(&macro_name, jpg);
    }
    if p.map_file {
        // the scanner's own map/reduced image (OpenSlide validates it too;
        // the converter never decodes it — reduced output levels are l0-box2)
        let mp = VmsGenParams { quality: 85, ..p.clone() };
        let (jpg, _) = build_tile(128, 64, &mp, 0, 1)?;
        out.push(&map_name, jpg);
    }
    if p.opt_file && opt_records.len() >= 2 {
        // a FAITHFUL .opt: 40-byte records (int64-LE entropy-start offset of
        // each MCU row, tiles ordered left-to-right top-to-bottom) with the
        // documented quirk that the LAST row of the entire file is missing
        // (the real sample's 762840 B = 19071 records = Σmcus_y − 1).
        // OpenSlide validates these against the streams; the adapter itself
        // reports presence only and decodes every byte.
        let n = opt_records.len();
        opt_records.truncate(n - 1);
        let mut opt: Vec<u8> = Vec::with_capacity((n - 1) * 40);
        for off in &opt_records {
            opt.extend_from_slice(&off.to_le_bytes());
            opt.extend_from_slice(&[0u8; 32]);
        }
        out.push(&opt_name, opt);
    }
    Ok(out)
}
