//! Hamamatsu VMS input adapter (brightfield bundle): an INI entry
//! (`<stem>.vms`, group `[Virtual Microscope Specimen]`) plus the
//! concatenated tile JPEGs it names (`ImageFile(x,y)`), optionally a map
//! image, a macro image and a binary `.opt` optimisation file — all
//! siblings of the entry file. Processed as a multi-member bundle exactly
//! like MRXS: every access is a bounded `read_member_at`, no member is ever
//! buffered whole (see <https://openslide.org/formats/hamamatsu/>; the
//! structural model mirrors OpenSlide's documented behaviour, independently
//! implemented here with bounded reads and typed errors — no OpenSlide
//! code).
//!
//! Geometry contract (OpenSlide-faithful): the tile JPEGs abut exactly —
//! NO overlap. Level 0 width is the sum of the row-0 column widths, height
//! the sum of the column-0 row heights; tile `(col,row)` sits at the prefix
//! sum of the widths/heights before it. Per-tile JPEG dimensions may differ
//! between columns/rows (the real scanner pads the right/bottom edge
//! files), but within one grid row every height — and within one grid
//! column every width — must agree (that agreement is what makes the
//! concatenation a rectangle; anything else is a typed refusal). MPP is
//! `PhysicalWidth/(1000·L0 width)` (`PhysicalHeight` likewise, nm units);
//! the objective power is `SourceLens`.
//!
//! Decode contract: every tile JPEG must be baseline (SOF0/1), 3-component,
//! and carry restart markers (DRI > 0) — the conversion decodes them
//! restart segment by segment (each segment an independent MCU run with
//! reset DC predictors; a 64K×64K tile is ~11 GB decoded RGB, so a
//! whole-tile decode is never an option and a strip without restarts has
//! no bounded decode unit). Progressive/arithmetic variants are typed
//! rejections before any decode. The `.opt` file is parsed for PRESENCE
//! only (OpenSlide itself treats it as an untrusted hint — the converter
//! decodes every byte anyway); the map image is detected and NOT exported
//! (reduced output levels are the `l0-box2` chain of output level 0).
//!
//! VMU (the uncompressed sibling format, group
//! `[Uncompressed Virtual Microscope Specimen]`) is NOT covered by this
//! adapter — a typed refusal, never a guess.
//!
//! Brightfield only; the grid must declare exactly one focal plane
//! (`NoLayers = 1`, the only layout OpenSlide accepts either).

use crate::budget::MemBudget;
use crate::bundle::BundleFs;
use crate::error::{CoreError, CoreResult};
use crate::jpeg;
use crate::ndpi::PROBE_LIMIT;
use crate::report::AssociatedSummary;

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "hamamatsu-vms-bundle";
/// Adapter version (bump on any output-affecting change; resume refuses on
/// mismatch, mirroring the output-profile refusal).
pub const ADAPTER_VERSION: &str = "1";

/// How reduced output levels are built (MRXS v2 / NDPI / generic-TIFF id).
pub const PYRAMID_METHOD: &str = "l0-box2";
/// Locked preserve-mode compose parameters (the documented high-fidelity
/// family: same values as the MRXS/NDPI compose).
pub const PRESERVE_COMPOSE_QUALITY: u8 = 96;
pub const PRESERVE_COMPOSE_SAMPLING: jpeg::Sampling = jpeg::Sampling::S422;
pub const PRESERVE_COMPOSE_HUFFMAN: &str = "standard-annex-k";
/// Versioned fingerprint of the compose encode parameters.
pub const PRESERVE_COMPOSE_FINGERPRINT: &str = "vms-mosaic-compose:q96:y422:hstd:v1";

/// Output-tile edge of the composed pyramid.
pub const OUT_TILE: u32 = 256;
/// The `.vms` INI entry is small; anything above this cap is refused.
const ENTRY_MAX: usize = 1 << 20;
const VMS_GROUP: &str = "Virtual Microscope Specimen";
const VMU_GROUP: &str = "Uncompressed Virtual Microscope Specimen";
const MAX_GRID_SIDE: u32 = 4096;
/// Sane mosaic cap per axis (a real NanoZoomer L0 is ≲ 120 000 px per side).
const MAX_SIDE: u64 = 1_000_000;

/// True colorspace of a tile JPEG payload (same rule as SVS/NDPI).
pub use crate::jpeg::TiffJpegColor as PayloadColor;

/// One `ImageFile(col,row)` of the mosaic: a complete baseline JPEG with
/// restart markers, decoded restart segment by segment.
#[derive(Debug, Clone)]
pub struct VmsTile {
    pub col: u32,
    pub row: u32,
    /// Resolved BUNDLE member index.
    pub member: u32,
    /// INI-referenced member name (bundle-flat).
    pub name: String,
    pub size: u64,
    pub width: u32,
    pub height: u32,
    /// True JPEG colorspace determined from the stream's markers.
    pub color: PayloadColor,
    /// SOF sampling (h1,v1,h2,v2,h3,v3).
    pub sampling: (u8, u8, u8, u8, u8, u8),
    /// MCU size in pixels (8·h_max, 8·v_max).
    pub mcu: (u32, u32),
    pub mcus_x: u32,
    pub mcus_y: u32,
    pub total_mcus: u64,
    /// Restart interval in MCUs (DRI; > 0 enforced).
    pub restart_interval: u32,
    /// Restart segments in the tile (= ⌈total_mcus / restart_interval⌉).
    pub segments: u64,
    /// Tile head bytes [SOI … SOS segment] (the sub-JPEG header source).
    pub header_bytes: Vec<u8>,
    /// Offset of the SOF height field inside `header_bytes` (width at +2).
    pub sof_hw_at: usize,
    /// [start, end) of the DRI marker segment inside `header_bytes`.
    pub dri_at: Option<(usize, usize)>,
    /// Mosaic position of the tile's (0,0) (prefix sums of the grid).
    pub x0: u64,
    pub y0: u64,
}

impl VmsTile {
    pub fn mcu_w(&self) -> u32 {
        self.mcu.0
    }
    pub fn mcu_h(&self) -> u32 {
        self.mcu.1
    }
    /// The shared restart-segment reader's view of this tile
    /// (crates/core/src/segment.rs); the member source must be opened on
    /// this tile's member.
    pub fn strip_geom(&self) -> crate::segment::StripGeom {
        crate::segment::StripGeom {
            strip_offset: 0,
            strip_bytes: self.size,
            mcu_w: self.mcu.0,
            mcu_h: self.mcu.1,
            mcus_x: self.mcus_x,
            total_mcus: self.total_mcus,
            restart_interval: self.restart_interval,
            segments: self.segments,
            header_bytes: self.header_bytes.clone(),
            sof_hw_at: self.sof_hw_at,
            dri_at: self.dri_at,
        }
    }
}

#[derive(Debug, Clone)]
pub struct VmsDoc {
    pub stem: String,
    /// Grid size (NoJpegColumns × NoJpegRows).
    pub cols: u32,
    pub rows: u32,
    /// The mosaic tiles, row-major (row × cols + col).
    pub tiles: Vec<VmsTile>,
    /// Mosaic level-0 dimensions (sums of the per-column/row tile dims).
    pub width: u32,
    pub height: u32,
    /// Objective power = SourceLens.
    pub objective: Option<f64>,
    /// µm/px = PhysicalWidth/(1000·L0 width), PhysicalHeight likewise (nm).
    pub mpp: Option<(f64, f64)>,
    /// Raw PhysicalWidth/PhysicalHeight (nm), when declared positive.
    pub physical_nm: Option<(f64, f64)>,
    /// Detected and NOT exported: the macro image (main-image conversion,
    /// not a source archive).
    pub associated: Vec<AssociatedSummary>,
    /// Optional members detected by name (presence only; the map's pixels
    /// are never used — reduced levels are l0-box2; the .opt restart hints
    /// are never trusted — every byte is decoded anyway).
    pub map_present: bool,
    pub opt_present: bool,
    /// Generated tail levels (width, height) — the `l0-box2` chain the
    /// conversion appends after the re-encoded level 0.
    pub generated: Vec<(u32, u32)>,
    /// Memory account carried from the probe into the conversion (review
    /// §1): the converter keeps charging band/decode working sets against
    /// the same host budget.
    pub budget: MemBudget,
}

/// INI value → f64 (trailing `;` tolerated — Hamamatsu writes
/// `PhysicalMacroHeight=…;` on some slides).
fn ini_f64(sd: &crate::mirax::SlideDat, group: &str, key: &str) -> Option<f64> {
    sd.get(group, key)
        .map(|v| v.trim().trim_end_matches(';').trim())
        .and_then(|v| v.parse::<f64>().ok())
}

fn ini_u64(sd: &crate::mirax::SlideDat, group: &str, key: &str) -> Option<u64> {
    sd.get(group, key)
        .map(|v| v.trim().trim_end_matches(';').trim())
        .and_then(|v| v.parse::<u64>().ok())
}

fn require_u64(sd: &crate::mirax::SlideDat, key: &str) -> CoreResult<u64> {
    ini_u64(sd, VMS_GROUP, key)
        .ok_or_else(|| CoreError::metadata(format!(".vms 缺少 {key}")))
}

fn require_name(sd: &crate::mirax::SlideDat, key: &str) -> CoreResult<String> {
    sd.get(VMS_GROUP, key)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .ok_or_else(|| CoreError::metadata(format!(".vms 缺少 {key}")))
}

/// Probe with the default (saver-profile) memory budget.
pub fn probe_vms(fs: &dyn BundleFs, stem: &str) -> CoreResult<VmsDoc> {
    probe_vms_with_budget(fs, stem, crate::budget::SAVER_BUDGET_BYTES)
}

/// Probe with an explicit host memory budget (review §1): every retained
/// allocation (per-tile JPEG heads, the tile table) is estimated
/// (overflow-checked) and charged BEFORE it is made; an over-budget charge
/// is the stable typed refusal `resource_profile_insufficient`.
pub fn probe_vms_with_budget(fs: &dyn BundleFs, stem: &str, budget_bytes: u64) -> CoreResult<VmsDoc> {
    let mut budget = MemBudget::host(budget_bytes);

    // ---- entry + INI ----------------------------------------------------- //
    let entry_name = format!("{stem}.vms");
    let entry_idx = fs.find(&entry_name).ok_or_else(|| {
        CoreError::validation(format!(
            "缺少主入口 {entry_name}（VMS 以 .vms 文本入口 + 同目录 tile JPEG 组成完整包）"
        ))
    })?;
    budget.charge(ENTRY_MAX as u64, ".vms 入口读取")?;
    let entry_bytes = fs.read_small_member(entry_idx, ENTRY_MAX)?;
    budget.release(ENTRY_MAX as u64);
    budget.charge(entry_bytes.len() as u64, ".vms 入口（常驻）")?;
    let sd = crate::mirax::parse_slidedat(&entry_bytes)?;

    // VMU (the uncompressed sibling format) gets its own typed refusal —
    // never a generic "unknown group" error.
    if sd.get(VMU_GROUP, "NoLayers").is_some() || fs.find(&format!("{stem}.vmu")).is_some() {
        return Err(CoreError::variant(
            "VMU（未压缩 Virtual Microscope Specimen）不在支持集：原始未压缩数据需要独立合同，\
             本适配器只接受 VMS（[Virtual Microscope Specimen] 组）",
        ));
    }
    if sd.get(VMS_GROUP, "NoLayers").is_none() {
        return Err(CoreError::variant(
            "不是 VMS 包：入口缺少 [Virtual Microscope Specimen] 组",
        ));
    }

    // ---- grid declaration ------------------------------------------------- //
    let layers = require_u64(&sd, "NoLayers")?;
    if layers != 1 {
        return Err(CoreError::variant(format!(
            "NoLayers={layers}：多焦面/多层 VMS 不在支持集（仅单层，OpenSlide 同样只接受 1）"
        )));
    }
    let cols = require_u64(&sd, "NoJpegColumns")?;
    let rows = require_u64(&sd, "NoJpegRows")?;
    if cols == 0 || rows == 0 || cols > MAX_GRID_SIDE as u64 || rows > MAX_GRID_SIDE as u64 {
        return Err(CoreError::variant(format!(
            "网格 {cols}×{rows} 越界（NoJpegColumns/NoJpegRows 需在 1..={MAX_GRID_SIDE}）"
        )));
    }
    let grid = cols.checked_mul(rows).ok_or_else(|| CoreError::variant("网格数溢出"))?;
    if grid as usize > crate::bundle::MAX_MEMBERS {
        return Err(CoreError::variant(format!(
            "网格 tile 数 {grid} 超过包成员上限 {}",
            crate::bundle::MAX_MEMBERS
        )));
    }

    // ---- referenced tile names (collect ALL missing before refusing) ------ //
    let image_file = require_name(&sd, "ImageFile")?; // = ImageFile(0,0)
    let mut names: Vec<String> = vec![image_file];
    for row in 0..rows as u32 {
        for col in 0..cols as u32 {
            if col == 0 && row == 0 {
                continue;
            }
            names.push(require_name(&sd, &format!("ImageFile({col},{row})"))?);
        }
    }
    for n in &names {
        if !crate::bundle::valid_member_name(n) || n.contains("..") {
            return Err(CoreError::validation(format!(
                "tile 文件名 {n:?} 非法（路径穿越被拒绝）"
            )));
        }
    }
    // optional members (presence only)
    let map_name = sd.get(VMS_GROUP, "MapFile").map(|v| v.trim().to_string());
    let opt_name = sd.get(VMS_GROUP, "OptimisationFile").map(|v| v.trim().to_string());
    let macro_name = sd.get(VMS_GROUP, "MacroImage").map(|v| v.trim().to_string());
    for n in map_name.iter().chain(opt_name.iter()).chain(macro_name.iter()) {
        if !crate::bundle::valid_member_name(n) || n.contains("..") {
            return Err(CoreError::validation(format!(
                "关联文件名 {n:?} 非法（路径穿越被拒绝）"
            )));
        }
    }
    let mut missing: Vec<String> = Vec::new();
    for n in names.iter().chain(map_name.iter()).chain(opt_name.iter()).chain(macro_name.iter()) {
        if fs.find(n).is_none() {
            missing.push(n.clone());
        }
    }
    if !missing.is_empty() {
        return Err(CoreError::validation(format!(
            "包不完整，缺少成员：{}（VMS 以 .vms 入口 + 同目录 tile JPEG 组成完整包，\
             缺成员在复制/转换前拒绝）",
            missing.join("、")
        )));
    }

    // ---- per-tile JPEG head scan ------------------------------------------ //
    let mut tiles: Vec<VmsTile> = Vec::with_capacity(grid as usize);
    for (ti, name) in names.iter().enumerate() {
        let member = fs.find(name).expect("checked above") as u32;
        let size = fs.members()[member as usize].size;
        if size < 4 {
            return Err(CoreError::jpeg(format!("tile {name} 小于最小 JPEG（{size} B）")));
        }
        budget.charge(PROBE_LIMIT.min(size), "tile JPEG 头探测读取")?;
        let head = fs.read_member_at(member as usize, 0, PROBE_LIMIT.min(size) as usize)?;
        budget.release(PROBE_LIMIT.min(size));
        let sh = crate::ndpi::scan_strip_head(&head)
            .map_err(|e| CoreError::jpeg(format!("tile {name} 头：{}", e.message)))?;
        if sh.restart_interval == 0 {
            return Err(CoreError::variant(format!(
                "tile {name} 无 restart marker（DRI=0）：64K 级 tile 没有有界解码单元，\
                 无法分段解码（转换必须分段，绝不整块解码）"
            )));
        }
        let sampling = sh.sampling.ok_or_else(|| {
            CoreError::variant(format!("tile {name} 不是三分量 JPEG（灰度不在明场支持集）"))
        })?;
        let (h1, v1, h2, v2, h3, v3) = sampling;
        let hmax = h1.max(h2).max(h3) as u32;
        let vmax = v1.max(v2).max(v3) as u32;
        let mcu = (8 * hmax, 8 * vmax);
        let mcus_x = sh.width.div_ceil(mcu.0);
        let mcus_y = sh.height.div_ceil(mcu.1);
        let total_mcus = mcus_x as u64 * mcus_y as u64;
        let segments = total_mcus.div_ceil(sh.restart_interval as u64);
        let color = jpeg::tiff_jpeg_color(
            &jpeg::JpegProbe {
                width: sh.width,
                height: sh.height,
                sampling: sh.sampling,
                comp_ids: None,
                jfif: sh.jfif,
                adobe_transform: sh.adobe_transform,
                sof_marker: sh.sof_marker,
            },
            6, // VMS tile JPEGs are YCbCr JFIF; Adobe-transform-0 RGB stays detectable
        );
        // the tile head is retained for the conversion's sub-JPEG surgery:
        // charged BEFORE the copy (released with the doc)
        budget.charge(sh.header_len as u64, "tile JPEG 头（常驻）")?;
        tiles.push(VmsTile {
            col: (ti as u32) % cols as u32,
            row: (ti as u32) / cols as u32,
            member,
            name: name.clone(),
            size,
            width: sh.width,
            height: sh.height,
            color,
            sampling,
            mcu,
            mcus_x,
            mcus_y,
            total_mcus,
            restart_interval: sh.restart_interval,
            segments,
            header_bytes: head[..sh.header_len].to_vec(),
            sof_hw_at: sh.sof_hw_at,
            dri_at: sh.dri_at,
            x0: 0,
            y0: 0,
        });
    }

    // ---- mosaic geometry (OpenSlide: exact abutment, no overlap) ---------- //
    for r in 0..rows as usize {
        let h0 = tiles[r * cols as usize].height;
        for c in 1..cols as usize {
            let t = &tiles[r * cols as usize + c];
            if t.height != h0 {
                return Err(CoreError::variant(format!(
                    "tile ({c},{r}) 高 {} ≠ 本行首列 {h0}：网格行高不一致不是可拼接的 VMS",
                    t.height
                )));
            }
        }
    }
    for c in 0..cols as usize {
        let w0 = tiles[c].width;
        for r in 1..rows as usize {
            let t = &tiles[r * cols as usize + c];
            if t.width != w0 {
                return Err(CoreError::variant(format!(
                    "tile ({c},{r}) 宽 {} ≠ 本列首行 {w0}：网格列宽不一致不是可拼接的 VMS",
                    t.width
                )));
            }
        }
    }
    let mut x_acc: u64 = 0;
    for c in 0..cols as usize {
        let w = tiles[c].width as u64;
        for r in 0..rows as usize {
            tiles[r * cols as usize + c].x0 = x_acc;
        }
        x_acc += w;
    }
    let mut y_acc: u64 = 0;
    for r in 0..rows as usize {
        let h = tiles[r * cols as usize].height as u64;
        for c in 0..cols as usize {
            tiles[r * cols as usize + c].y0 = y_acc;
        }
        y_acc += h;
    }
    let width = x_acc;
    let height = y_acc;
    if width == 0 || height == 0 || width > MAX_SIDE || height > MAX_SIDE {
        return Err(CoreError::variant(format!(
            "拼接尺寸 {width}×{height} 越界"
        )));
    }

    // ---- physical calibration ---------------------------------------------- //
    let phys_w = ini_f64(&sd, VMS_GROUP, "PhysicalWidth").filter(|v| *v > 0.0 && v.is_finite());
    let phys_h = ini_f64(&sd, VMS_GROUP, "PhysicalHeight").filter(|v| *v > 0.0 && v.is_finite());
    let physical_nm = phys_w.zip(phys_h);
    let mpp = physical_nm.map(|(pw, ph)| {
        (pw / (1000.0 * width as f64), ph / (1000.0 * height as f64))
    });
    let objective = ini_f64(&sd, VMS_GROUP, "SourceLens").filter(|v| *v > 0.0 && v.is_finite());

    // ---- associated / optional members (detected, not exported) ------------ //
    let mut associated = Vec::new();
    if let Some(mname) = &macro_name {
        let mi = fs.find(mname).expect("checked above");
        let msize = fs.members()[mi].size;
        budget.charge(PROBE_LIMIT.min(msize), "macro 头探测读取")?;
        let head = fs.read_member_at(mi, 0, PROBE_LIMIT.min(msize) as usize)?;
        budget.release(PROBE_LIMIT.min(msize));
        let (w, h) = match crate::ndpi::scan_strip_head(&head) {
            Ok(sh) => (sh.width, sh.height),
            Err(_) => (0, 0), // a non-JPEG macro is reported with 0 dims
        };
        associated.push(AssociatedSummary {
            name: "macro".to_string(),
            source_offset: 0,
            source_length: 0,
            width: w,
            height: h,
        });
    }

    Ok(VmsDoc {
        stem: stem.to_string(),
        cols: cols as u32,
        rows: rows as u32,
        tiles,
        width: width as u32,
        height: height as u32,
        objective,
        mpp,
        physical_nm,
        associated,
        map_present: map_name.is_some(),
        opt_present: opt_name.is_some(),
        generated: crate::gtiff::generated_tail(width as u32, height as u32),
        budget,
    })
}

// --------------------------------------------------------------------------- //
// estimate
// --------------------------------------------------------------------------- //

/// Disk-precheck estimate for the VMS adapter. 输出 tile 全部按「保留画质」
/// 拼接重编码：源 tile 字节数只是像素代理（实测公开样本 CMU-1 VMS，4:4:4
/// q75 源 631,681,644 B → preserve 输出见 vms-adapter-report.md；与 NDPI
/// 同一保守倍数：preserve 4× payload，compact 1.5× payload，逐写盘检查
/// 与运行时输出上限是最后的硬闸）。
pub fn estimate_vms(doc: &VmsDoc) -> crate::estimate::OutputEstimate {
    let payload: u64 = doc.tiles.iter().map(|t| t.size).sum();
    let l0_tiles = (doc.width as u64).div_ceil(OUT_TILE as u64)
        * (doc.height as u64).div_ceil(OUT_TILE as u64);
    let gen_tiles: u64 = doc
        .generated
        .iter()
        .map(|(w, h)| {
            (*w as u64).div_ceil(OUT_TILE as u64) * (*h as u64).div_ceil(OUT_TILE as u64)
        })
        .sum();
    let tiles = l0_tiles + gen_tiles;
    let ifds = (1 + doc.generated.len()) as u64;
    let base = tiles
        .saturating_mul(16)
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(1024 * 1024);
    crate::estimate::OutputEstimate {
        payload_bytes: payload,
        tiles_present: tiles,
        cells_total: tiles,
        cells_missing: 0,
        edge_tiles: 0, // every tile is a full 256×256 canvas re-encode
        ifds,
        output_upper_bound_bytes: payload
            .saturating_mul(4)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
        compact_upper_bound_bytes: payload
            .saturating_mul(3)
            .div_ceil(2)
            .saturating_add(tiles.saturating_mul(16))
            .saturating_add(base),
    }
}
