//! Standard MRXS (3DHISTECH MIRAX) brightfield bundle adapter (F3).
//!
//! A logical slide is a *bundle*: `<stem>.mrxs` + the same-name directory
//! holding `Slidedat.ini`, `Index.dat` and `DataNNNN.dat` (see
//! <https://openslide.org/formats/mirax/>; the structural model mirrors
//! OpenSlide's documented behaviour, independently implemented here with
//! bounded reads and typed errors — no OpenSlide code).
//!
//! Parsing contract (all of it bounded; no member is ever buffered whole):
//!
//! - `Slidedat.ini` ≤ 1 MiB (OpenSlide's cap): INI groups/keys, `HIER_0`
//!   must be `Slide zoom level`, per-level `LAYER_0_LEVEL_i_SECTION` gives
//!   `IMAGE_CONCAT_FACTOR`, `OVERLAP_X/Y`, `MICROMETER_PER_PIXEL_X/Y`,
//!   `DIGITIZER_WIDTH/HEIGHT`, `IMAGE_FORMAT` (JPEG only), fill colour.
//! - `Index.dat`: 5-byte version `01.02`, the `SLIDE_ID`, then 32-bit LE
//!   integers. The int at `hier_root` points at the per-zoom-level pointer
//!   table; each level's record is `0, page_ptr` and each data page is
//!   `[len][next][len × (image_index, offset, length, fileno)]`. Page chains
//!   are walked with a visited set — loops are a typed error. Every
//!   pointer, every image interval and every `fileno` is bounds-checked.
//! - nonhierarchical records locate the camera-position buffer
//!   (`VIMSLIDE_POSITION_BUFFER`, plain 9-byte entries) or
//!   `StitchingIntensityLayer` (zlib/DEFLATE, CURRENT_SLIDE_VERSION ≥ 2.2);
//!   with neither, positions are synthesised from the nominal overlap.
//! - geometry (level dims, subtile grid, placements) follows OpenSlide
//!   exactly, including its integer truncation when summing fractional
//!   overlaps into the base extents — verified level-0-exact against
//!   OpenSlide on the public CMU-1 / CMU-1-Saved-1_16 samples.
//!
//! Brightfield only: `SLIDE_TYPE_BRIGHTFIELD` required; a fluorescence or
//! multi-channel slide is a typed refusal.

use crate::budget::{elem, MemBudget};
use crate::bundle::{BundleFs, MAX_MEMBERS};
use crate::error::{CoreError, CoreResult};
use crate::inflate;
use crate::report::AssociatedSummary;

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "mirax-bundle";
/// Adapter version (bump on any output-affecting change; resume refuses on
/// mismatch, mirroring the output-profile refusal). v2 = review §4: reduced
/// output levels are the L0-derived box pyramid (`l0-box2`) instead of the
/// scanner reduced-level images composed at integer-snapped positions —
/// committed pixels change on every reduced level, so v1 checkpoints must
/// never be mixed into a v2 output.
pub const ADAPTER_VERSION: &str = "2";

/// OpenSlide's Slidedat size cap.
const SLIDEDAT_MAX: usize = 1 << 20;
const INDEX_VERSION: &[u8; 5] = b"01.02";
const MAX_ZOOM_LEVELS: usize = 32;
const MAX_IMAGES_PER_AXIS: u64 = 65_536;
const MAX_TOTAL_IMAGES: u64 = 2_000_000;
const MAX_DATA_FILES: usize = 4_096;
const MAX_PAGE_ITEMS: u32 = 1 << 20;
/// Total placement cap across all levels (each is 16 bytes packed; 2M
/// placements ≈ 32 MiB — beyond that the bundle is refused as
/// unreasonably dense, a scanner never approaches it).
const MAX_TOTAL_PLACEMENTS: u64 = 2_000_000;
/// Pages of one record chain (loop guard; 1 MiB pages × real chains ≪ this).
const MAX_PAGES_PER_RECORD: u32 = 1 << 22;

// --------------------------------------------------------------------------- //
// Slidedat.ini (bounded INI subset)
// --------------------------------------------------------------------------- //

#[derive(Default, Debug, Clone)]
pub struct SlideDat {
    pub groups: Vec<(String, Vec<(String, String)>)>,
}

impl SlideDat {
    pub fn get(&self, group: &str, key: &str) -> Option<&str> {
        self.groups
            .iter()
            .find(|(g, _)| g == group)
            .and_then(|(_, kv)| kv.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str()))
    }
    pub fn i64(&self, group: &str, key: &str) -> Option<i64> {
        self.get(group, key).and_then(|v| v.trim().parse::<i64>().ok())
    }
    pub fn u64(&self, group: &str, key: &str) -> Option<u64> {
        self.get(group, key).and_then(|v| v.trim().parse::<u64>().ok())
    }
    pub fn f64(&self, group: &str, key: &str) -> Option<f64> {
        self.get(group, key).and_then(|v| v.trim().parse::<f64>().ok())
    }
    fn require_i64(&self, group: &str, key: &str) -> CoreResult<i64> {
        self.i64(group, key).ok_or_else(|| {
            CoreError::metadata(format!("Slidedat.ini 缺少 [{group}] {key}"))
        })
    }
    fn require_f64(&self, group: &str, key: &str) -> CoreResult<f64> {
        self.f64(group, key).ok_or_else(|| {
            CoreError::metadata(format!("Slidedat.ini 缺少 [{group}] {key}"))
        })
    }
}

pub fn parse_slidedat(bytes: &[u8]) -> CoreResult<SlideDat> {
    // UTF-8 with optional BOM; CRLF/LF; `;` comments
    let b = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF][..]).unwrap_or(bytes);
    let text = std::str::from_utf8(b)
        .map_err(|_| CoreError::metadata("Slidedat.ini 不是合法 UTF-8"))?;
    let mut sd = SlideDat::default();
    let mut cur: Option<usize> = None;
    for raw in text.split(['\n', '\r']) {
        let line = raw.trim();
        if line.is_empty() || line.starts_with(';') || line.starts_with('#') {
            continue;
        }
        if line.len() > 4096 {
            return Err(CoreError::metadata("Slidedat.ini 行过长"));
        }
        if line.starts_with('[') && line.ends_with(']') {
            let g = &line[1..line.len() - 1];
            if g.is_empty() || g.len() > 128 {
                return Err(CoreError::metadata("Slidedat.ini 组名非法"));
            }
            sd.groups.push((g.to_string(), Vec::new()));
            cur = Some(sd.groups.len() - 1);
            continue;
        }
        let Some((k, v)) = line.split_once('=') else {
            return Err(CoreError::metadata(format!(
                "Slidedat.ini 行无法解析：{}…",
                &line[..line.len().min(40)]
            )));
        };
        let gi = cur.ok_or_else(|| CoreError::metadata("Slidedat.ini 键出现在组外"))?;
        sd.groups[gi].1.push((k.trim().to_string(), v.trim().to_string()));
        if sd.groups[gi].1.len() > 4096 {
            return Err(CoreError::metadata("Slidedat.ini 单组键数过多"));
        }
    }
    if sd.groups.len() > 4096 {
        return Err(CoreError::metadata("Slidedat.ini 组数过多"));
    }
    Ok(sd)
}

// --------------------------------------------------------------------------- //
// geometry model (OpenSlide-faithful)
// --------------------------------------------------------------------------- //

#[derive(Debug, Clone)]
pub struct LevelSection {
    /// cumulative concat exponent → images per side concatenated at this level
    pub concat: u64,
    pub overlap_x: f64,
    pub overlap_y: f64,
    pub mpp_x: f64,
    pub mpp_y: f64,
    pub image_w: i64,
    pub image_h: i64,
    /// BGR int from Slidedat → RGB bytes
    pub fill_rgb: [u8; 3],
}

#[derive(Debug, Clone, Copy)]
pub struct LevelParams {
    pub concat: u64,
    pub tiles_per_image: u64,
    pub tile_w: f64,
    pub tile_h: f64,
    pub positions_per_tile: u64,
}

/// One image record from Index.dat: placement in a data member.
#[derive(Debug, Clone, Copy)]
pub struct ImageRef {
    /// grid x/y in level-0 IMAGE units (x % concat == 0 enforced)
    pub x: u64,
    pub y: u64,
    /// resolved BUNDLE member index (not the Slidedat FILE_N number)
    pub member: u32,
    pub offset: u64,
    pub length: u32,
}

#[derive(Debug, Clone)]
pub struct MiraxLevel {
    pub width: u32,
    pub height: u32,
    pub params: LevelParams,
    pub section: LevelSection,
    pub images: Vec<ImageRef>,
    pub payload_bytes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PositionSource {
    VimslideBuffer,
    StitchingIntensity,
    Synthesized,
}

#[derive(Debug, Clone)]
pub struct MiraxDoc {
    pub slide_id: String,
    pub levels: Vec<MiraxLevel>,
    pub images_x: u64,
    pub images_y: u64,
    pub divisions: u64,
    pub mpp: Option<(f64, f64)>,
    pub objective: Option<f64>,
    pub position_source: PositionSource,
    /// camera positions, level-0 pixel units, ×concat0 already applied
    positions: Vec<(i64, i64)>,
    /// camera positions with a level-0 image (activity set, OpenSlide rule)
    active: Vec<bool>,
    pub associated: Vec<AssociatedSummary>,
    /// Memory account carried from the probe into the conversion (review §1):
    /// already holds the probe's persistent allocations; the converter keeps
    /// charging placements/CSR/cache/decode against the same host budget.
    pub budget: MemBudget,
}

impl MiraxDoc {
    pub fn position_count(&self) -> usize {
        self.positions.len()
    }
}

/// Base extent along one axis, mirroring OpenSlide's integer accumulation
/// (`base_w += image_w - overlap_x` truncates the double per column).
fn base_extent(n: u64, isize_: i64, overlap: f64, divisions: u64) -> i64 {
    // OpenSlide accumulates into an int64 while the per-column contribution
    // is a double (image_w - overlap), i.e. each fractional column truncates
    // toward zero — mirrored exactly so the level dims agree byte-for-byte.
    let mut base: i64 = 0;
    for i in 0..n {
        if (i % divisions) != (divisions - 1) || i == n - 1 {
            base += isize_;
        } else {
            base += (isize_ as f64 - overlap) as i64;
        }
    }
    base
}

fn level_params(sections: &[LevelSection], divisions: u64, li: usize) -> LevelParams {
    let concat = sections[li].concat;
    let positions_per_image = std::cmp::max(1, concat / divisions);
    // we always keep the subtile-splitting branch (OpenSlide branch 1);
    // with nominal synthesised positions it reduces to branch 2
    LevelParams {
        concat,
        tiles_per_image: positions_per_image,
        tile_w: sections[li].image_w as f64 / positions_per_image as f64,
        tile_h: sections[li].image_h as f64 / positions_per_image as f64,
        positions_per_tile: 1,
    }
}

// --------------------------------------------------------------------------- //
// Index.dat reader (bounded, checked)
// --------------------------------------------------------------------------- //

struct IndexReader<'a> {
    fs: &'a dyn BundleFs,
    member: usize,
    size: u64,
    cache: Vec<u8>,
    cache_at: u64,
}

const INDEX_CACHE: u64 = 1 << 20;

impl<'a> IndexReader<'a> {
    fn new(fs: &'a dyn BundleFs, member: usize) -> CoreResult<Self> {
        let size = fs.members()[member].size;
        if size < 64 || size > 1 << 32 {
            return Err(CoreError::index(format!("Index.dat 大小 {size} 异常")));
        }
        Ok(IndexReader { fs, member, size, cache: Vec::new(), cache_at: 0 })
    }
    fn i32_at(&mut self, at: u64) -> CoreResult<i32> {
        let b = self.read_at(at, 4)?;
        Ok(i32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }
    fn read_at(&mut self, at: u64, len: usize) -> CoreResult<&[u8]> {
        let end = at
            .checked_add(len as u64)
            .ok_or_else(|| CoreError::index("Index.dat 读取长度溢出"))?;
        if end > self.size {
            return Err(CoreError::index(format!(
                "Index.dat 读取 [{at},+{len}) 越界（大小 {}）",
                self.size
            )));
        }
        if self.cache.is_empty() || at < self.cache_at || end > self.cache_at + self.cache.len() as u64
        {
            let want = INDEX_CACHE.min(self.size - at);
            self.cache = self.fs.read_member_at(self.member, at, want as usize)?;
            self.cache_at = at;
        }
        let off = (at - self.cache_at) as usize;
        Ok(&self.cache[off..off + len])
    }
}

/// Walk one hier level's data pages, validating and appending image refs.
/// Every page visited and every image record appended is charged against the
/// memory budget BEFORE it is materialised (review §1).
#[allow(clippy::too_many_arguments)]
fn walk_hier_level(
    r: &mut IndexReader,
    record_ptr: u64,
    images_x: u64,
    images_y: u64,
    concat: u64,
    member_sizes: &[u64],
    data_members: &[usize],
    images: &mut Vec<ImageRef>,
    payload: &mut u64,
    budget: &mut MemBudget,
) -> CoreResult<()> {
    if r.i32_at(record_ptr)? != 0 {
        return Err(CoreError::index("层级记录首整数非 0"));
    }
    let mut page = r.i32_at(record_ptr + 4)? as u64;
    if page == 0 {
        return Ok(()); // empty level is legal (nothing stored)
    }
    if page < 8 || page >= r.size {
        return Err(CoreError::index(format!("数据页指针 {page} 越界")));
    }
    let mut visited: std::collections::HashSet<u64> = std::collections::HashSet::new();
    let mut pages = 0u32;
    loop {
        if !visited.insert(page) {
            return Err(CoreError::index(format!(
                "数据页链表回环（页 {page} 已访问过）"
            )));
        }
        budget.charge(elem::PAGE_VISIT, "Index.dat 数据页链表（visited 集）")?;
        pages += 1;
        if pages > MAX_PAGES_PER_RECORD {
            return Err(CoreError::index("数据页数超过上限（链表异常）"));
        }
        let len = r.i32_at(page)?;
        if len < 0 || len as u32 > MAX_PAGE_ITEMS {
            return Err(CoreError::index(format!("页长度 {len} 异常")));
        }
        let next = r.i32_at(page + 4)? as i64;
        if next < 0 {
            return Err(CoreError::index("页 next 指针为负"));
        }
        let mut at = page + 8;
        for _ in 0..len {
            let image_index = r.i32_at(at)? as i64;
            let offset = r.i32_at(at + 4)? as i64;
            let length = r.i32_at(at + 8)? as i64;
            let fileno = r.i32_at(at + 12)? as i64;
            at += 16;
            if image_index < 0 {
                return Err(CoreError::index("image_index 为负"));
            }
            if offset < 0 || length < 0 {
                return Err(CoreError::index("图像 offset/length 为负"));
            }
            if fileno < 0 || fileno as usize >= member_sizes.len() {
                return Err(CoreError::index(format!("fileno {fileno} 越界")));
            }
            let idx = image_index as u64;
            let x = idx % images_x;
            let y = idx / images_x;
            if y >= images_y {
                return Err(CoreError::index(format!(
                    "image_index {} → y {y} 超出网格（{}×{}）",
                    idx, images_x, images_y
                )));
            }
            if concat > 0 && (x % concat != 0 || y % concat != 0) {
                return Err(CoreError::index(format!(
                    "image_index {} → ({x},{y}) 不是本层 concat {concat} 的倍数",
                    idx
                )));
            }
            let (end, over) = (offset as u64).overflowing_add(length as u64);
            if over || end > member_sizes[fileno as usize] {
                return Err(CoreError::oob(format!(
                    "图像区间 [{offset},+{length}) 越界（成员 {} 大小 {}）",
                    fileno, member_sizes[fileno as usize]
                )));
            }
            budget.charge(elem::IMAGE_REF, "图像记录表")?;
            images.push(ImageRef {
                x,
                y,
                member: data_members[fileno as usize] as u32,
                offset: offset as u64,
                length: length as u32,
            });
            *payload = payload.saturating_add(length as u64);
            if images.len() as u64 > MAX_TOTAL_IMAGES {
                return Err(CoreError::index(format!(
                    "单层图像数超过上限 {MAX_TOTAL_IMAGES}"
                )));
            }
        }
        if next == 0 {
            break;
        }
        let np = next as u64;
        if np < 8 || np >= r.size {
            return Err(CoreError::index(format!("next 页指针 {np} 越界")));
        }
        page = np;
    }
    Ok(())
}

/// Nonhierarchical record → (member, offset, length) of its first data item.
fn read_nonhier_record(
    r: &mut IndexReader,
    nonhier_root: u64,
    record: usize,
    member_sizes: &[u64],
) -> CoreResult<Option<(u32, u64, u32)>> {
    let table = r.i32_at(nonhier_root)? as u64;
    if table == 0 || table + 4 * record as u64 + 4 > r.size {
        return Err(CoreError::index("非层级记录表越界"));
    }
    let ptr = r.i32_at(table + 4 * record as u64)? as i64;
    if ptr <= 0 {
        return Ok(None); // no such record
    }
    let ptr = ptr as u64;
    if r.i32_at(ptr)? != 0 {
        return Err(CoreError::index("非层级记录首整数非 0"));
    }
    let page = r.i32_at(ptr + 4)? as i64;
    if page <= 0 {
        return Ok(None);
    }
    let page = page as u64;
    let n = r.i32_at(page)?;
    if n < 1 {
        return Err(CoreError::index("非层级页长度 < 1"));
    }
    // [n][next(任意)][0][0][offset][size][fileno]
    let _next = r.i32_at(page + 4)?;
    if r.i32_at(page + 8)? != 0 || r.i32_at(page + 12)? != 0 {
        return Err(CoreError::index("非层级页缺少两个 0 值"));
    }
    let offset = r.i32_at(page + 16)? as i64;
    let size = r.i32_at(page + 20)? as i64;
    let fileno = r.i32_at(page + 24)? as i64;
    if offset < 0 || size < 0 {
        return Err(CoreError::index("非层级记录 offset/size 为负"));
    }
    if fileno < 0 || fileno as usize >= member_sizes.len() {
        return Err(CoreError::index(format!("非层级 fileno {fileno} 越界")));
    }
    let (end, over) = (offset as u64).overflowing_add(size as u64);
    if over || end > member_sizes[fileno as usize] {
        return Err(CoreError::oob("非层级记录区间越界"));
    }
    Ok(Some((fileno as u32, offset as u64, size as u32)))
}

/// `NONHIER_i_NAME == target` → val offset accumulated over counts.
fn nonhier_name_offset(sd: &SlideDat, target: &str) -> CoreResult<Option<usize>> {
    let count = sd
        .u64("HIERARCHICAL", "NONHIER_COUNT")
        .ok_or_else(|| CoreError::metadata("Slidedat.ini 缺少 NONHIER_COUNT"))?;
    let mut off = 0usize;
    for i in 0..count as usize {
        let name = sd
            .get("HIERARCHICAL", &format!("NONHIER_{i}_NAME"))
            .ok_or_else(|| CoreError::metadata(format!("缺少 NONHIER_{i}_NAME")))?;
        let cnt = sd
            .i64("HIERARCHICAL", &format!("NONHIER_{i}_COUNT"))
            .filter(|v| *v > 0)
            .ok_or_else(|| CoreError::metadata(format!("NONHIER_{i}_COUNT 非法")))?;
        if name == target {
            return Ok(Some(off));
        }
        off += cnt as usize;
    }
    Ok(None)
}

// --------------------------------------------------------------------------- //
// camera positions
// --------------------------------------------------------------------------- //

fn read_position_buffer(
    fs: &dyn BundleFs,
    data_members: &[usize],
    loc: (u32, u64, u32),
    expected: usize,
    compressed: bool,
    concat0: u64,
) -> CoreResult<Vec<(i64, i64)>> {
    // loc.0 is a Slidedat FILE_N number, not a bundle member index
    let member = data_members
        .get(loc.0 as usize)
        .copied()
        .ok_or_else(|| CoreError::index(format!("位置缓冲 fileno {} 越界", loc.0)))?;
    let raw = fs.read_member_at(member, loc.1, loc.2 as usize)?;
    let buf = if compressed {
        // zlib per the real ≥2.2 samples; raw deflate tolerated as fallback
        match inflate::zlib_inflate_to_exact(&raw, expected) {
            Ok(v) => v,
            Err(_) => inflate::inflate_to_exact(&raw, expected)?,
        }
    } else {
        if raw.len() != expected {
            return Err(CoreError::validation(format!(
                "位置缓冲长度 {} ≠ 预期 {}",
                raw.len(),
                expected
            )));
        }
        raw
    };
    let n = buf.len() / 9;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let flag = buf[i * 9];
        if flag & 0xFE != 0 {
            return Err(CoreError::validation(format!(
                "位置标志字节 {flag} 异常（仅 0/1 合法）"
            )));
        }
        let x = i32::from_le_bytes(buf[i * 9 + 1..i * 9 + 5].try_into().unwrap()) as i64;
        let y = i32::from_le_bytes(buf[i * 9 + 5..i * 9 + 9].try_into().unwrap()) as i64;
        // stored in original camera units → level-0 pixel units
        out.push((x.saturating_mul(concat0 as i64), y.saturating_mul(concat0 as i64)));
    }
    Ok(out)
}

// --------------------------------------------------------------------------- //
// probe
// --------------------------------------------------------------------------- //

/// Probe with the default (saver-profile) memory budget.
pub fn probe_mirax(fs: &dyn BundleFs, stem: &str) -> CoreResult<MiraxDoc> {
    probe_mirax_with_budget(fs, stem, crate::budget::SAVER_BUDGET_BYTES)
}

/// Probe with an explicit host memory budget (review §1): every large
/// allocation below (position table, image records, page-chain visited set)
/// is estimated (overflow-checked) and charged BEFORE it is allocated; an
/// over-budget charge is a typed `resource_profile_insufficient` refusal.
pub fn probe_mirax_with_budget(
    fs: &dyn BundleFs,
    stem: &str,
    budget_bytes: u64,
) -> CoreResult<MiraxDoc> {
    let mut budget = MemBudget::host(budget_bytes);
    probe_inner(fs, stem, &mut budget)
}

fn probe_inner(fs: &dyn BundleFs, stem: &str, budget: &mut MemBudget) -> CoreResult<MiraxDoc> {
    // ---- members ------------------------------------------------------- //
    let entry = format!("{stem}.mrxs");
    let Some(entry_idx) = fs.find(&entry) else {
        return Err(CoreError::validation(format!(
            "缺少主入口 {entry}（MRXS 需要同名目录）"
        )));
    };
    let _ = entry_idx; // the entry file itself is not read by the adapter
    let Some(sd_idx) = fs.find(&format!("{stem}/Slidedat.ini")) else {
        return Err(CoreError::validation(format!(
            "缺少成员 {stem}/Slidedat.ini（MRXS 需要完整包：{stem}.mrxs + 同名目录）"
        )));
    };
    let sd_bytes = fs.read_small_member(sd_idx, SLIDEDAT_MAX)?;
    let sd = parse_slidedat(&sd_bytes)?;

    // ---- brightfield / general ------------------------------------------ //
    match sd.get("GENERAL", "SLIDE_TYPE") {
        Some("SLIDE_TYPE_BRIGHTFIELD") => {}
        Some(other) => {
            return Err(CoreError::variant(format!(
                "SLIDE_TYPE={other}：荧光/多通道 MRXS 不在明场支持集（需要独立通道合同）"
            )))
        }
        None => {
            return Err(CoreError::variant(
                "Slidedat.ini 缺少 SLIDE_TYPE：无法确认明场，拒绝猜测",
            ))
        }
    }
    let slide_id = sd
        .get("GENERAL", "SLIDE_ID")
        .ok_or_else(|| CoreError::metadata("缺少 SLIDE_ID"))?
        .to_string();
    if slide_id.is_empty() || slide_id.len() > 128 {
        return Err(CoreError::metadata("SLIDE_ID 长度异常"));
    }
    let images_x = sd.require_i64("GENERAL", "IMAGENUMBER_X")? as u64;
    let images_y = sd.require_i64("GENERAL", "IMAGENUMBER_Y")? as u64;
    if images_x == 0 || images_y == 0 || images_x > MAX_IMAGES_PER_AXIS || images_y > MAX_IMAGES_PER_AXIS
    {
        return Err(CoreError::variant(format!(
            "IMAGENUMBER {images_x}×{images_y} 越界"
        )));
    }
    if images_x.saturating_mul(images_y) > MAX_TOTAL_IMAGES * 16 {
        return Err(CoreError::variant("图像网格总数过大"));
    }
    let divisions = sd
        .i64("GENERAL", "CameraImageDivisionsPerSide")
        .filter(|v| *v > 0)
        .unwrap_or(1) as u64;
    if divisions > 64 {
        return Err(CoreError::variant(format!("CameraImageDivisionsPerSide={divisions} 越界")));
    }
    let objective = sd
        .get("GENERAL", "OBJECTIVE_MAGNIFICATION")
        .and_then(|v| v.trim_end_matches('x').parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v > 0.0);

    // ---- hierarchy ------------------------------------------------------ //
    if sd.get("HIERARCHICAL", "HIER_0_NAME") != Some("Slide zoom level") {
        return Err(CoreError::variant(
            "HIER_0 不是 Slide zoom level：未知层级布局（OpenSlide 同样拒绝）",
        ));
    }
    let zoom_levels = sd
        .i64("HIERARCHICAL", "HIER_0_COUNT")
        .filter(|v| *v > 0 && *v as usize <= MAX_ZOOM_LEVELS)
        .ok_or_else(|| CoreError::metadata("HIER_0_COUNT 非法"))? as usize;
    let indexfile = sd
        .get("HIERARCHICAL", "INDEXFILE")
        .ok_or_else(|| CoreError::metadata("缺少 INDEXFILE"))?
        .to_string();
    if !crate::bundle::valid_member_name(&format!("{stem}/{indexfile}"))
        || indexfile.contains("..")
    {
        return Err(CoreError::validation(format!(
            "INDEXFILE {indexfile:?} 不是安全的成员名（路径穿越被拒绝）"
        )));
    }

    // ---- data files ------------------------------------------------------ //
    let file_count = sd
        .i64("DATAFILE", "FILE_COUNT")
        .filter(|v| *v > 0 && *v as usize <= MAX_DATA_FILES)
        .ok_or_else(|| CoreError::metadata("FILE_COUNT 非法"))? as usize;
    if file_count + 2 > MAX_MEMBERS {
        return Err(CoreError::validation(format!("FILE_COUNT {file_count} 超过成员上限")));
    }
    let mut data_members: Vec<usize> = Vec::with_capacity(file_count);
    let mut member_sizes: Vec<u64> = Vec::with_capacity(file_count);
    for i in 0..file_count {
        let name = sd
            .get("DATAFILE", &format!("FILE_{i}"))
            .ok_or_else(|| CoreError::metadata(format!("缺少 FILE_{i}")))?;
        let full = format!("{stem}/{name}");
        if !crate::bundle::valid_member_name(&full) || name.contains("..") || name.contains('/') {
            return Err(CoreError::validation(format!(
                "数据文件名 {name:?} 非法（路径穿越/嵌套路径被拒绝）"
            )));
        }
        let Some(idx) = fs.find(&full) else {
            return Err(CoreError::validation(format!(
                "缺少成员 {full}（Slidedat.ini 引用的数据文件必须在包内）"
            )));
        };
        data_members.push(idx);
        member_sizes.push(fs.members()[idx].size);
    }

    // ---- level sections --------------------------------------------------- //
    let mut sections: Vec<LevelSection> = Vec::with_capacity(zoom_levels);
    let mut total_exp: u32 = 0;
    for i in 0..zoom_levels {
        let gname = sd
            .get("HIERARCHICAL", &format!("HIER_0_VAL_{i}_SECTION"))
            .ok_or_else(|| CoreError::metadata(format!("缺少 HIER_0_VAL_{i}_SECTION")))?;
        let concat_exp = sd.require_i64(&gname, "IMAGE_CONCAT_FACTOR")?;
        if (i == 0 && concat_exp < 0) || (i > 0 && concat_exp <= 0) || concat_exp > 30 {
            return Err(CoreError::variant(format!(
                "第 {i} 层 IMAGE_CONCAT_FACTOR={concat_exp} 非法"
            )));
        }
        total_exp = total_exp
            .checked_add(concat_exp as u32)
            .ok_or_else(|| CoreError::variant("concat 指数溢出"))?;
        if total_exp > 30 {
            return Err(CoreError::variant(format!("累计 concat 指数 {total_exp} 过大")));
        }
        let image_w = sd.require_i64(&gname, "DIGITIZER_WIDTH")?;
        let image_h = sd.require_i64(&gname, "DIGITIZER_HEIGHT")?;
        if image_w <= 0 || image_h <= 0 || image_w > 65535 || image_h > 65535 {
            return Err(CoreError::variant(format!(
                "第 {i} 层 DIGITIZER {image_w}×{image_h} 越界"
            )));
        }
        let overlap_x = sd.require_f64(&gname, "OVERLAP_X")?;
        let overlap_y = sd.require_f64(&gname, "OVERLAP_Y")?;
        if !(overlap_x.is_finite() && overlap_y.is_finite())
            || overlap_x < 0.0
            || overlap_y < 0.0
            || overlap_x > image_w as f64
            || overlap_y > image_h as f64
        {
            return Err(CoreError::variant(format!(
                "第 {i} 层 OVERLAP {overlap_x}/{overlap_y} 非法"
            )));
        }
        let format = sd
            .get(&gname, "IMAGE_FORMAT")
            .ok_or_else(|| CoreError::metadata(format!("[{gname}] 缺少 IMAGE_FORMAT")))?;
        if format != "JPEG" {
            return Err(CoreError::variant(format!(
                "IMAGE_FORMAT={format}：仅支持 JPEG 数据（PNG/BMP24 需要独立解码器）"
            )));
        }
        let bgr = sd.require_i64(&gname, "IMAGE_FILL_COLOR_BGR")?;
        let fill_rgb = [
            ((bgr >> 16) & 0xFF) as u8,
            ((bgr >> 8) & 0xFF) as u8,
            (bgr & 0xFF) as u8,
        ];
        let mpp_x = sd.require_f64(&gname, "MICROMETER_PER_PIXEL_X")?;
        let mpp_y = sd.require_f64(&gname, "MICROMETER_PER_PIXEL_Y")?;
        sections.push(LevelSection {
            concat: 1u64 << total_exp,
            overlap_x,
            overlap_y,
            mpp_x,
            mpp_y,
            image_w,
            image_h,
            fill_rgb,
        });
    }
    // strictly-decreasing true downsamples: each level's mpp doubles
    for w in sections.windows(2) {
        let ratio = w[1].mpp_x / w[0].mpp_x;
        if !(1.9..=2.2).contains(&ratio) {
            return Err(CoreError::variant(format!(
                "层间 MPP 比 {ratio:.3} 不是已知的 2 倍降采样序列"
            )));
        }
    }

    let mpp = sections[0]
        .mpp_x
        .is_finite()
        .then_some((sections[0].mpp_x, sections[0].mpp_y))
        .filter(|(x, y)| *x > 0.0 && *y > 0.0);

    // ---- level dims ------------------------------------------------------ //
    let base_w = base_extent(images_x, sections[0].image_w, sections[0].overlap_x, divisions);
    let base_h = base_extent(images_y, sections[0].image_h, sections[0].overlap_y, divisions);
    if base_w <= 0 || base_h <= 0 || base_w > 1_000_000 * 4 || base_h > 1_000_000 * 4 {
        return Err(CoreError::variant(format!("基准尺寸 {base_w}×{base_h} 越界")));
    }

    // ---- index.dat -------------------------------------------------------- //
    let index_member = fs
        .find(&format!("{stem}/{indexfile}"))
        .ok_or_else(|| {
            CoreError::validation(format!(
                "缺少成员 {stem}/{indexfile}（MRXS 需要完整包：{stem}.mrxs + 同名目录）"
            ))
        })?;
    let mut r = IndexReader::new(fs, index_member)?;
    let head = r.read_at(0, 5 + slide_id.len())?.to_vec();
    if &head[..5] != INDEX_VERSION {
        return Err(CoreError::index(format!(
            "Index.dat 版本 {:?} 不是 01.02",
            &head[..5.min(head.len())]
        )));
    }
    if &head[5..] != slide_id.as_bytes() {
        return Err(CoreError::index("Index.dat 的 SLIDE_ID 与 Slidedat.ini 不一致"));
    }
    let hier_root = (5 + slide_id.len()) as u64;
    let nonhier_root = hier_root + 4;
    let hier_table = r.i32_at(hier_root)? as i64;
    if hier_table <= 0 || hier_table as u64 + 4 * zoom_levels as u64 > r.size {
        return Err(CoreError::index("层级指针表越界"));
    }

    // ---- camera positions -------------------------------------------------- //
    // npositions comes from the DECLARED camera grid (IMAGENUMBER ÷ divisions)
    // and is what every branch below allocates for — the reviewer's negative
    // (a 51 KB bundle declaring 5000×5000 cameras without a position buffer)
    // must be refused HERE, before any of it is materialised.
    let npositions = (images_x / divisions) as usize * (images_y / divisions) as usize;
    let npositions64 = npositions as u64;
    let expected_buf = npositions * 9;
    let vimslide = nonhier_name_offset(&sd, "VIMSLIDE_POSITION_BUFFER")?;
    let stitching = if vimslide.is_none() {
        nonhier_name_offset(&sd, "StitchingIntensityLayer")?
    } else {
        None
    };
    let (positions, position_source) = match (vimslide, stitching) {
        (Some(off), _) => {
            let loc = read_nonhier_record(&mut r, nonhier_root, off, &member_sizes)?;
            if let Some(loc) = &loc {
                // raw member slice (loc.2) + tuple table
                budget.charge(loc.2 as u64, "位置缓冲原始读取")?;
            }
            budget.charge_mul(
                npositions64,
                elem::POSITION_RAW + elem::POSITION,
                "VIMSLIDE 位置表（原始缓冲 + 坐标元组）",
            )?;
            (
                loc.map(|loc| {
                    read_position_buffer(fs, &data_members, loc, expected_buf, false, sections[0].concat)
                })
                .transpose()?
                .unwrap_or_default(),
                PositionSource::VimslideBuffer,
            )
        }
        (None, Some(off)) => {
            let loc = read_nonhier_record(&mut r, nonhier_root, off, &member_sizes)?;
            if let Some(loc) = &loc {
                budget.charge(loc.2 as u64, "位置缓冲原始读取")?;
            }
            budget.charge_mul(
                npositions64,
                elem::POSITION_RAW + elem::POSITION,
                "StitchingIntensity 位置表（解码缓冲 + 坐标元组）",
            )?;
            (
                loc.map(|loc| {
                    read_position_buffer(fs, &data_members, loc, expected_buf, true, sections[0].concat)
                })
                .transpose()?
                .unwrap_or_default(),
                PositionSource::StitchingIntensity,
            )
        }
        _ => {
            // synthesise nominal positions (OpenSlide's fallback)
            budget.charge_mul(npositions64, elem::POSITION, "合成位置表（坐标元组）")?;
            let positions_x = images_x / divisions;
            let mut v = Vec::with_capacity(npositions);
            for i in 0..npositions as u64 {
                let adv_x =
                    sections[0].image_w as f64 * divisions as f64 - sections[0].overlap_x;
                let adv_y =
                    sections[0].image_h as f64 * divisions as f64 - sections[0].overlap_y;
                v.push((
                    ((i % positions_x) as f64 * adv_x) as i64,
                    ((i / positions_x) as f64 * adv_y) as i64,
                ));
            }
            (v, PositionSource::Synthesized)
        }
    };
    if positions.len() != npositions {
        return Err(CoreError::validation(format!(
            "位置条目 {} ≠ 相机位置数 {npositions}",
            positions.len()
        )));
    }

    // ---- associated images (detected, not exported) ------------------------
    let mut associated = Vec::new();
    let assoc = |sd: &SlideDat, val: &str, key: &str, name: &str, out: &mut Vec<AssociatedSummary>| {
        let Ok(Some(off)) = nonhier_name_offset(sd, "Scan data layer") else {
            return;
        };
        // find the val index of `val` under NONHIER_0
        let count = sd.u64("HIERARCHICAL", "NONHIER_0_COUNT").unwrap_or(0);
        for i in 0..count {
            if sd.get("HIERARCHICAL", &format!("NONHIER_0_VAL_{i}")) == Some(val) {
                let section = sd
                    .get("HIERARCHICAL", &format!("NONHIER_0_VAL_{i}_SECTION"))
                    .unwrap_or("");
                if sd.get(section, key) == Some("JPEG") {
                    out.push(AssociatedSummary {
                        name: name.to_string(),
                        source_offset: 0,
                        source_length: 0,
                        width: sd.u64(section, &key.replace("TYPE", "WIDTH")).unwrap_or(0)
                            as u32,
                        height: sd.u64(section, &key.replace("TYPE", "HEIGHT")).unwrap_or(0)
                            as u32,
                    });
                }
                break;
            }
        }
        let _ = off;
    };
    assoc(&sd, "ScanDataLayer_SlideThumbnail", "THUMBNAIL_IMAGE_TYPE", "macro", &mut associated);
    assoc(&sd, "ScanDataLayer_SlideBarcode", "BARCODE_IMAGE_TYPE", "label", &mut associated);
    assoc(&sd, "ScanDataLayer_SlidePreview", "PREVIEW_IMAGE_TYPE", "thumbnail", &mut associated);

    // ---- per-level image walk ----------------------------------------------
    let mut levels: Vec<MiraxLevel> = Vec::with_capacity(zoom_levels);
    let mut total_placements: u64 = 0;
    budget.charge_mul(npositions64, elem::ACTIVE, "位置活跃标记表")?;
    let mut active: Vec<bool> = vec![false; npositions];
    for li in 0..zoom_levels {
        let record_ptr = r.i32_at(hier_table as u64 + 4 * li as u64)? as i64;
        if record_ptr <= 0 || record_ptr as u64 + 8 > r.size {
            return Err(CoreError::index(format!("第 {li} 层记录指针越界")));
        }
        let params = level_params(&sections, divisions, li);
        let mut images = Vec::new();
        let mut payload = 0u64;
        walk_hier_level(
            &mut r,
            record_ptr as u64,
            images_x,
            images_y,
            params.concat,
            &member_sizes,
            &data_members,
            &mut images,
            &mut payload,
            budget,
        )?;
        total_placements = total_placements.saturating_add(
            (images.len() as u64).saturating_mul(params.tiles_per_image * params.tiles_per_image),
        );
        if total_placements > MAX_TOTAL_PLACEMENTS {
            return Err(CoreError::variant(format!(
                "拼接单元总数超过上限 {MAX_TOTAL_PLACEMENTS}"
            )));
        }
        // activity marks from the level-0 walk (OpenSlide's rule)
        if li == 0 {
            for img in &images {
                for pi in 0..params.tiles_per_image {
                    for pj in 0..params.tiles_per_image {
                        let xx = img.x + pi * divisions;
                        let yy = img.y + pj * divisions;
                        if xx >= images_x || yy >= images_y {
                            continue;
                        }
                        let xp = (xx / divisions) as usize;
                        let yp = (yy / divisions) as usize;
                        let cp = yp * (images_x / divisions) as usize + xp;
                        if let Some(p) = positions.get(cp) {
                            if p.0 == 0 && p.1 == 0 && (xp != 0 || yp != 0) {
                                continue; // would break the tilemap grid
                            }
                            active[cp] = true;
                        }
                    }
                }
            }
        }
        levels.push(MiraxLevel {
            width: (base_w / params.concat as i64) as u32,
            height: (base_h / params.concat as i64) as u32,
            params,
            section: sections[li].clone(),
            images,
            payload_bytes: payload,
        });
    }

    Ok(MiraxDoc {
        slide_id,
        levels,
        images_x,
        images_y,
        divisions,
        mpp,
        objective,
        position_source,
        positions,
        active,
        associated,
        budget: budget.clone(),
    })
}

// --------------------------------------------------------------------------- //
// placements (pure arithmetic over the probe results)
// --------------------------------------------------------------------------- //

/// One subtile placement: paste `size` pixels of image `img` (of the level's
/// image list) from `src` at level pixel `dst`.
#[derive(Debug, Clone, Copy)]
pub struct Placement {
    pub img: u32,
    /// level pixel position (rounded from the true fractional position)
    pub dst: (i64, i64),
    pub src: (u32, u32),
    pub size: (u32, u32),
}

impl MiraxDoc {
    /// Compute the placements of one level, in OpenSlide's paint order
    /// (grid order: tile_y, then tile_x, then record order).
    pub fn placements(&self, li: usize) -> Vec<Placement> {
        let lv = &self.levels[li];
        let p = lv.params;
        let iw0 = self.levels[0].section.image_w;
        let ih0 = self.levels[0].section.image_h;
        let div = self.divisions;
        let positions_x = (self.images_x / div) as usize;
        let mut out: Vec<((u64, u64), Placement)> = Vec::with_capacity(lv.images.len());
        let sw = p.tile_w.ceil() as u32;
        let sh = p.tile_h.ceil() as u32;
        for (ii, img) in lv.images.iter().enumerate() {
            for yi in 0..p.tiles_per_image {
                let yy = img.y + yi * div;
                if yy >= self.images_y {
                    break;
                }
                for xi in 0..p.tiles_per_image {
                    let xx = img.x + xi * div;
                    if xx >= self.images_x {
                        break;
                    }
                    let xp = (xx / div) as usize;
                    let yp = (yy / div) as usize;
                    let cp = yp * positions_x + xp;
                    let Some(&(px, py)) = self.positions.get(cp) else {
                        continue;
                    };
                    if li == 0 {
                        // OpenSlide: (0,0) coordinates off-origin break the grid
                        if px == 0 && py == 0 && (xp != 0 || yp != 0) {
                            continue;
                        }
                    } else if !self.position_active_around(cp, p.positions_per_tile, positions_x)
                    {
                        continue;
                    }
                    let pos0x = px as f64 + iw0 as f64 * (xx - xp as u64 * div) as f64;
                    let pos0y = py as f64 + ih0 as f64 * (yy - yp as u64 * div) as f64;
                    let dx = round_half_away(pos0x / p.concat as f64);
                    let dy = round_half_away(pos0y / p.concat as f64);
                    let sx = round_half_away(p.tile_w * xi as f64) as u32;
                    let sy = round_half_away(p.tile_h * yi as f64) as u32;
                    // grid coordinates for paint ordering (OpenSlide's
                    // tile_count_divisor = min(concat, divisions))
                    let tcd = p.concat.min(div).max(1);
                    let gx = img.x / tcd + xi;
                    let gy = img.y / tcd + yi;
                    out.push(((gy, gx), Placement {
                        img: ii as u32,
                        dst: (dx, dy),
                        src: (sx, sy),
                        size: (sw, sh),
                    }));
                }
            }
        }
        out.sort_by_key(|(g, _)| *g);
        out.into_iter().map(|(_, pl)| pl).collect()
    }

    fn position_active_around(&self, cp: usize, _per_tile: u64, _positions_x: usize) -> bool {
        // with the subtile branch, positions_per_tile == 1 → check cp itself
        self.active.get(cp).copied().unwrap_or(false)
    }
}

fn round_half_away(v: f64) -> i64 {
    if v >= 0.0 {
        (v + 0.5).floor() as i64
    } else {
        (v - 0.5).ceil() as i64
    }
}

// --------------------------------------------------------------------------- //
// estimate
// --------------------------------------------------------------------------- //

pub fn estimate_mirax(doc: &MiraxDoc) -> crate::estimate::OutputEstimate {
    let mut payload = 0u64;
    for lv in &doc.levels {
        payload = payload.saturating_add(lv.payload_bytes);
    }
    // preserve (composed + re-encoded at q95 4:4:4): the source payloads are
    // scanner-JPEG; a same-resolution q95 4:4:4 re-encode can exceed their
    // size — 2× the payload covers every measured case and stays
    // byte-bound; the runtime output cap is the hard guard.
    let tiles: u64 = doc
        .levels
        .iter()
        .map(|lv| {
            (lv.width.div_ceil(256) as u64) * (lv.height.div_ceil(256) as u64)
        })
        .sum();
    let ifds = doc.levels.len() as u64;
    let base = tiles
        .saturating_mul(16)
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(1024 * 1024);
    // measured on the public samples: the compose re-encode can inflate a
    // low-quality scanner payload ~2–3.6× (worst: the first q95 RGB draft,
    // CMU-1 746 MB payload → 2.67 GB). The y422 q96 compose plus fill-tile
    // dedupe (one shared payload per fill colour, every sparse tile then
    // costs only its 12-byte record) measures ~1.4×; the bound keeps the
    // 2× payload multiple plus the per-tile record floor, and the runtime
    // output cap remains the hard guard.
    let per_tile_floor = tiles.saturating_mul(16);
    crate::estimate::OutputEstimate {
        payload_bytes: payload,
        tiles_present: tiles,
        cells_total: tiles,
        cells_missing: 0,
        edge_tiles: 0,
        ifds,
        output_upper_bound_bytes: payload
            .saturating_mul(2)
            .saturating_add(per_tile_floor)
            .saturating_add(base),
        compact_upper_bound_bytes: payload
            .saturating_mul(3)
            .div_ceil(2)
            .saturating_add(per_tile_floor)
            .saturating_add(base),
    }
}
