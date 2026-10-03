//! Synthetic MRXS bundle generator (F3 tests; `fixtures` feature).
//!
//! Builds a complete bundle (entry + Slidedat.ini + Index.dat + data
//! members) with knobbed geometry: overlaps, camera-position jitter
//! (odd original-unit values make reduced-level placements fractional),
//! sparse empty camera positions, concat factors, arbitrary level counts,
//! and fault knobs (index loops, OOB page pointers, missing members,
//! traversal names, corrupt payloads). Deterministic pixel patterns only.

use crate::bundle::MemBundle;
use crate::error::{CoreError, CoreResult};
use crate::jpeg::{encode_rgb, EncoderCfg, Sampling};

#[derive(Debug, Clone)]
pub struct MrxsGenParams {
    pub stem: String,
    pub slide_id: String,
    pub images_x: u64,
    pub images_y: u64,
    pub divisions: u64,
    /// per-level (concat_exponent, overlap_x, overlap_y)
    pub levels: Vec<(u32, f64, f64)>,
    pub image_w: u32,
    pub image_h: u32,
    /// deterministic camera-position jitter in original units (odd values
    /// make reduced-level placements fractional)
    pub position_jitter: i64,
    /// camera positions (row-major index) with NO images (sparse holes)
    pub skip_positions: Vec<u32>,
    pub quality: u8,
    /// Review §4 geometry fixture: level-0 images are drawn from the GLOBAL
    /// level-0 pattern `f(ax, ay)` (smooth diagonal gradient + a 5-px cross
    /// at every (ax ≡ 16, ay ≡ 16) mod 32) instead of the per-image base
    /// pattern — content is then coherent ACROSS camera-image seams (a cross
    /// straddling a boundary is composed consistently from both images), and
    /// feature L0 coordinates are known exactly by construction.
    pub features: bool,
    // fault knobs
    pub index_loop: bool,
    pub oob_page_ptr: bool,
    pub corrupt_payload: bool,
    pub png_format: bool,
    pub missing_data_member: bool,
    pub traversal_data_name: bool,
}

impl Default for MrxsGenParams {
    fn default() -> Self {
        MrxsGenParams {
            stem: "synthetic".to_string(),
            slide_id: "0123456789ABCDEF0123456789ABCDEF".to_string(),
            images_x: 8,
            images_y: 6,
            divisions: 2,
            levels: vec![(0, 12.0, 12.0), (1, 6.0, 6.0), (1, 3.0, 3.0)],
            image_w: 64,
            image_h: 48,
            position_jitter: 0,
            skip_positions: vec![],
            quality: 90,
            features: false,
            index_loop: false,
            oob_page_ptr: false,
            corrupt_payload: false,
            png_format: false,
            missing_data_member: false,
            traversal_data_name: false,
        }
    }
}

/// Deterministic per-image pixel content.
fn image_pixels(
    level: usize,
    gx: u64,
    gy: u64,
    w: u32,
    h: u32,
    l0_origin: Option<(i64, i64)>,
) -> Vec<u8> {
    let mut v = Vec::with_capacity((w * h * 3) as usize);
    let base = ((level as u64 * 37 + gx * 11 + gy * 23) % 200) as u32;
    for y in 0..h {
        for x in 0..w {
            if level == 0 {
                if let Some((ox, oy)) = l0_origin {
                    // review §4 geometry fixture: the GLOBAL level-0 pattern.
                    // `ax`/`ay` are the pixel's absolute L0 coordinates —
                    // identical whether the pixel comes from this image or
                    // the overlapping neighbour, so the composed L0 (and
                    // therefore the whole L0-derived pyramid) is seamless
                    // across camera-image boundaries by construction.
                    let ax = ox + x as i64;
                    let ay = oy + y as i64;
                    // smooth diagonal gradient: ≤ 1 level per pixel step, no wrap
                    let g = 40 + ((ax + ay) / 8).min(120);
                    // a 5-px plus (dx + dy ≤ 2) centred on every
                    // (ax ≡ 16 mod 32, ay ≡ 16 mod 32): known absolute L0
                    // coordinates, straddling seams for some crosses
                    let dx = (ax.rem_euclid(32) - 16).abs();
                    let dy = (ay.rem_euclid(32) - 16).abs();
                    if dx + dy <= 2 {
                        v.extend_from_slice(&[255, 255, 255]);
                    } else {
                        let b = g as u8;
                        v.extend_from_slice(&[b, b, b]);
                    }
                    continue;
                }
            }
            let b = (base + x / 8 + y / 8) % 256;
            v.push((b % 251) as u8);
            v.push((b.wrapping_mul(3) % 255) as u8);
            v.push((b.wrapping_mul(7) % 255) as u8);
        }
    }
    v
}

/// Build the bundle (members: `<stem>.mrxs`, `<stem>/Slidedat.ini`,
/// `<stem>/Index.dat`, `<stem>/Data0000.dat`, `<stem>/Data0001.dat`).
pub fn build_synthetic_mrxs(p: &MrxsGenParams) -> CoreResult<MemBundle> {
    if p.levels.is_empty() || p.levels.len() > 32 {
        return Err(CoreError::validation("层数需在 1..=32"));
    }
    if p.images_x == 0 || p.images_y == 0 || p.divisions == 0 {
        return Err(CoreError::validation("网格参数需为正"));
    }
    let stem = &p.stem;
    let npos_x = p.images_x / p.divisions;
    let npos_y = p.images_y / p.divisions;
    let npos = (npos_x * npos_y) as usize;
    let iw0 = p.image_w as i64;
    let ih0 = p.image_h as i64;
    let div = p.divisions as i64;

    let mut active: Vec<bool> = vec![false; npos];
    for cp in 0..npos {
        active[cp] = !p.skip_positions.contains(&(cp as u32));
    }
    // camera positions, ORIGINAL units (the adapter ×concat0 on read).
    // Inactive positions carry (0,0) coordinates — the real scanner files
    // do the same, and OpenSlide/the adapter skip those subtiles at zoom 0.
    let mut positions: Vec<(i64, i64)> = Vec::with_capacity(npos);
    for i in 0..npos as i64 {
        let xp = i % npos_x as i64;
        let yp = i / npos_x as i64;
        let jit = if i % 3 == 0 { p.position_jitter } else { 0 };
        if !active[i as usize] {
            positions.push((0, 0));
        } else {
            positions.push((
                xp * (iw0 * div - p.levels[0].1 as i64) + jit,
                yp * (ih0 * div - p.levels[0].2 as i64) + jit,
            ));
        }
    }

    // ---- image payloads (Data0000.dat) -------------------------------- //
    let cfg = EncoderCfg::with_quality(p.quality, Sampling::S444);
    let mut data0: Vec<u8> = Vec::new();
    // items per level: (image_index, offset, length, fileno=0)
    let mut image_items: Vec<Vec<(u64, u64, u32, u32)>> = vec![Vec::new(); p.levels.len()];
    let mut total_concat = 0u32;
    for (li, &(exp, _, _)) in p.levels.iter().enumerate() {
        total_concat += exp;
        let concat = 1u64 << total_concat;
        let per_pos = (concat / p.divisions).max(1); // positions per image side
        let mut gy = 0u64;
        while gy < p.images_y {
            let mut gx = 0u64;
            while gx < p.images_x {
                let mut has = false;
                for py in 0..per_pos {
                    for px in 0..per_pos {
                        let cxp = gx / p.divisions + px;
                        let cyp = gy / p.divisions + py;
                        if cxp < npos_x && cyp < npos_y && active[(cyp * npos_x + cxp) as usize]
                        {
                            has = true;
                        }
                    }
                }
            if has {
                // level-0 images (concat exponent 0) sit at exactly ONE
                // camera position whose L0 destination is the position the
                // fixture wrote into the buffer — that is the image's L0
                // origin for the global pattern
                let l0_origin = if p.features && li == 0 && p.levels[0].0 == 0 {
                    let xp = (gx / p.divisions) as i64;
                    let yp = (gy / p.divisions) as i64;
                    let cp = (yp as u64 * npos_x + xp as u64) as usize;
                    let adv_x = iw0 * div - p.levels[0].1 as i64;
                    let adv_y = ih0 * div - p.levels[0].2 as i64;
                    let jit = if cp % 3 == 0 { p.position_jitter } else { 0 };
                    let (px, py) = positions
                        .get(cp)
                        .copied()
                        .unwrap_or((xp * adv_x, yp * adv_y));
                    Some((px, py))
                } else {
                    None
                };
                let px = image_pixels(li, gx, gy, p.image_w, p.image_h, l0_origin);
                    let mut jpg = encode_rgb(&px, p.image_w, p.image_h, &cfg)?;
                    if p.corrupt_payload && li == 0 && image_items[0].is_empty() {
                        jpg = jpg[..jpg.len() / 2].to_vec();
                    }
                    let off = data0.len() as u64;
                    data0.extend_from_slice(&jpg);
                    // image_index = image_y * IMAGENUMBER_X + image_x
                    image_items[li].push((gy * p.images_x + gx, off, jpg.len() as u32, 0));
                }
                gx += concat.max(1);
            }
            gy += concat.max(1);
        }
    }

    // ---- position buffer (Data0001.dat) -------------------------------- //
    let mut data1: Vec<u8> = Vec::new();
    for (i, &(x, y)) in positions.iter().enumerate() {
        data1.push(active[i] as u8);
        data1.extend_from_slice(&(x as i32).to_le_bytes());
        data1.extend_from_slice(&(y as i32).to_le_bytes());
    }
    let pos_off = 0u64;
    let pos_len = data1.len() as u32;

    // ---- Index.dat ------------------------------------------------------ //
    fn push_i32(index: &mut Vec<u8>, v: i32) {
        index.extend_from_slice(&v.to_le_bytes());
    }
    fn set_i32(index: &mut [u8], at: usize, v: i32) {
        index[at..at + 4].copy_from_slice(&v.to_le_bytes());
    }
    let mut index: Vec<u8> = Vec::new();
    index.extend_from_slice(b"01.02");
    index.extend_from_slice(p.slide_id.as_bytes());
    let hier_root = index.len();
    push_i32(&mut index, 0); // patched
    let nonhier_root = index.len();
    push_i32(&mut index, 0); // patched
    const PER_PAGE: usize = 128;
    let mut level_records: Vec<usize> = Vec::new();
    for items in &image_items {
        let rec = index.len();
        push_i32(&mut index, 0);
        let first_page_slot = index.len();
        push_i32(&mut index, 0);
        let mut page_ptrs: Vec<usize> = Vec::new();
        for chunk in items.chunks(PER_PAGE) {
            let page = index.len();
            page_ptrs.push(page);
            push_i32(&mut index, chunk.len() as i32);
            push_i32(&mut index, 0); // next (patched)
            for &(image_index, off, len, fileno) in chunk {
                push_i32(&mut index, image_index as i32);
                push_i32(&mut index, off as i32);
                push_i32(&mut index, len as i32);
                push_i32(&mut index, fileno as i32);
            }
        }
        for w in page_ptrs.windows(2) {
            set_i32(&mut index, w[0] + 4, w[1] as i32);
        }
        if let Some(&last) = page_ptrs.last() {
            set_i32(&mut index, last + 4, 0);
            if p.index_loop && page_ptrs.len() >= 2 {
                set_i32(&mut index, page_ptrs[1] + 4, page_ptrs[0] as i32);
            }
            if p.oob_page_ptr {
                set_i32(&mut index, page_ptrs[0] + 4, 0x7FFF_0000);
            }
            set_i32(&mut index, first_page_slot, page_ptrs[0] as i32);
        } else {
            set_i32(&mut index, first_page_slot, 0); // empty level
        }
        level_records.push(rec);
    }
    // nonhier record 1 = position buffer (name offsets: Scan data layer
    // COUNT=1 → rec 0; VIMSLIDE_POSITION_BUFFER → rec 1)
    let nh_rec1 = index.len();
    push_i32(&mut index, 0);
    let page_slot = index.len();
    push_i32(&mut index, 0);
    let nh_page = index.len();
    push_i32(&mut index, 1); // one item
    push_i32(&mut index, 0); // next
    push_i32(&mut index, 0);
    push_i32(&mut index, 0);
    push_i32(&mut index, pos_off as i32);
    push_i32(&mut index, pos_len as i32);
    push_i32(&mut index, 1); // fileno 1 = Data0001.dat
    set_i32(&mut index, page_slot, nh_page as i32);
    // nonhier table: entry 1 (of the two name slots) → position record
    let nh_table = index.len();
    push_i32(&mut index, 0); // entry 0 unused
    push_i32(&mut index, nh_rec1 as i32);
    // hier table lives at the end (its offset is back-patched)
    let hier_table = index.len();
    for &rec in &level_records {
        push_i32(&mut index, rec as i32);
    }
    set_i32(&mut index, hier_root, hier_table as i32);
    set_i32(&mut index, nonhier_root, nh_table as i32);

    // ---- Slidedat.ini ---------------------------------------------------- //
    let data0_name = if p.traversal_data_name { "../evil.dat" } else { "Data0000.dat" };
    let mut sd = String::new();
    sd.push_str("[GENERAL]\nSLIDE_VERSION = 01.03\n");
    sd.push_str(&format!("SLIDE_ID = {}\n", p.slide_id));
    sd.push_str(&format!("IMAGENUMBER_X = {}\n", p.images_x));
    sd.push_str(&format!("IMAGENUMBER_Y = {}\n", p.images_y));
    sd.push_str("CURRENT_SLIDE_VERSION = 1.9\nSLIDE_TYPE = SLIDE_TYPE_BRIGHTFIELD\n");
    sd.push_str("OBJECTIVE_MAGNIFICATION = 20\n");
    sd.push_str(&format!("CameraImageDivisionsPerSide = {}\n", p.divisions));
    sd.push_str("[HIERARCHICAL]\nINDEXFILE = Index.dat\nHIER_COUNT = 1\nNONHIER_COUNT = 2\n");
    sd.push_str("HIER_0_NAME = Slide zoom level\n");
    sd.push_str(&format!("HIER_0_COUNT = {}\n", p.levels.len()));
    for li in 0..p.levels.len() {
        sd.push_str(&format!(
            "HIER_0_VAL_{li} = ZoomLevel_{li}\nHIER_0_VAL_{li}_SECTION = LAYER_0_LEVEL_{li}_SECTION\n"
        ));
    }
    sd.push_str("NONHIER_0_NAME = Scan data layer\nNONHIER_0_COUNT = 1\n");
    sd.push_str("NONHIER_0_VAL_0 = ScanDataLayer_SlideThumbnail\n");
    sd.push_str("NONHIER_0_VAL_0_SECTION = NONHIERLAYER_0_LEVEL_0_SECTION\n");
    sd.push_str("NONHIER_1_NAME = VIMSLIDE_POSITION_BUFFER\nNONHIER_1_COUNT = 1\n");
    sd.push_str("NONHIER_1_VAL_0 = default\n");
    sd.push_str("NONHIER_1_VAL_0_SECTION = NONHIERLAYER_1_LEVEL_0_SECTION\n");
    sd.push_str("[DATAFILE]\nFILE_COUNT = 2\n");
    sd.push_str(&format!("FILE_0 = {data0_name}\n"));
    sd.push_str("FILE_1 = Data0001.dat\n");
    let mut mpp = 0.25f64;
    let mut texp = 0u32;
    for (li, &(exp, ox, oy)) in p.levels.iter().enumerate() {
        texp += exp;
        let _ = texp;
        sd.push_str(&format!("[LAYER_0_LEVEL_{li}_SECTION]\n"));
        sd.push_str("IMAGE_FILL_COLOR_BGR = 16777215\n");
        sd.push_str(&format!("MICROMETER_PER_PIXEL_X = {mpp}\n"));
        sd.push_str(&format!("MICROMETER_PER_PIXEL_Y = {mpp}\n"));
        sd.push_str(&format!("DIGITIZER_WIDTH = {}\n", p.image_w));
        sd.push_str(&format!("DIGITIZER_HEIGHT = {}\n", p.image_h));
        sd.push_str(&format!("OVERLAP_X = {ox}\n"));
        sd.push_str(&format!("OVERLAP_Y = {oy}\n"));
        sd.push_str(&format!("IMAGE_CONCAT_FACTOR = {exp}\n"));
        sd.push_str(if p.png_format && li == 0 {
            "IMAGE_FORMAT = PNG\n"
        } else {
            "IMAGE_FORMAT = JPEG\n"
        });
        sd.push_str("IMAGE_COMPRESSION_FACTOR = 90\n");
        mpp *= 2.0;
    }
    sd.push_str(
        "[NONHIERLAYER_0_LEVEL_0_SECTION]\nTHUMBNAIL_IMAGE_TYPE = JPEG\nTHUMBNAIL_IMAGE_WIDTH = 64\nTHUMBNAIL_IMAGE_HEIGHT = 64\n",
    );
    sd.push_str("[NONHIERLAYER_1_LEVEL_0_SECTION]\nVIMSLIDE_POSITION_DATA_FORMAT_VERSION = 257\n");

    // ---- assemble ---------------------------------------------------------- //
    let mut out = MemBundle::new();
    out.push(&format!("{stem}.mrxs"), b"synthetic entry".to_vec());
    out.push(&format!("{stem}/Slidedat.ini"), sd.into_bytes());
    out.push(&format!("{stem}/Index.dat"), index);
    if !p.missing_data_member && !p.traversal_data_name {
        out.push(&format!("{stem}/Data0000.dat"), data0);
    }
    out.push(&format!("{stem}/Data0001.dat"), data1);
    Ok(out)
}
