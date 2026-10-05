//! Leica SCN (brightfield, JPEG tiles) input adapter (F4).
//!
//! Detection contract (bounded reads only, whole file never buffered):
//!
//! - the file must be a classic TIFF or BigTIFF (either byte order);
//! - IFD 0's ImageDescription must be the Leica SCN XML (root element
//!   `<scn>` with a `http://www.leica-microsystems.com/scn…` namespace);
//! - the XML `<collection>` may hold several `<image>` entries (label /
//!   macro / preview scans next to the tissue scan). The MAIN image is the
//!   one with the largest `<pixels sizeX*sizeY>`; every other image is a
//!   detected-but-not-exported associated scan. An image without
//!   `<pixels>`/`<dimension>` entries can never be the main image;
//! - the main image's `<illuminationSource>` must be `brightfield` — a
//!   fluorescence SCN is a typed rejection BEFORE any payload is copied;
//! - the pyramid levels are the `<dimension r ifd>` entries of the main
//!   image: level `r` lives in the TIFF IFD with that chain index, and its
//!   `sizeX/sizeY` must match the IFD's own width/height exactly. Levels
//!   must form a strictly decreasing sequence;
//! - every level IFD must be tiled, compression 7 (baseline JPEG),
//!   photometric 2/6, 3 samples of 8 bits, chunky — the payloads are copied
//!   verbatim like the SVS adapter. A missing tile (TileOffsets entry with
//!   offset 0 AND length 0 — the SCN400 sparse-grid quirk) is NOT an error:
//!   the conversion fills it with a generated white tile and reports
//!   `tiles_filled` / the `scn_missing_tiles_filled` warning;
//! - MPP comes from `<view sizeX/sizeY>` (nanometres) over `<pixels
//!   sizeX/sizeY>`; the objective from `<objective>` — both stay unknown
//!   otherwise (nothing is invented).
//!
//! Label/macro/preview images are NOT exported: this is a main-image
//! conversion, not an archive of the source file.
//!
//! Routing helper: [`sniff_tiff_vendor`] classifies a TIFF container by its
//! IFD 0 description (Aperio / Leica SCN / OME-TIFF / this converter's own
//! BigTIFF / unknown) with bounded reads. OME-TIFF and converter BigTIFF
//! are NOT conversion inputs — they are already platform-readable and are
//! rejected with typed reasons before any staging.

use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;
use crate::report::AssociatedSummary;
use crate::tiff_read::{self, Ifd, TiffHeader, TiffKind};

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "leica-scn-jpeg";
/// Adapter version (bump on any output-affecting change). Enforced in the
/// core's resume entry: a checkpoint whose recorded version differs from
/// this one — or that carries NO version at all (the field has existed
/// since SCN adapter v1) — is refused, mirroring the output-profile refusal.
pub const ADAPTER_VERSION: &str = "1";

/// Sane geometric caps (a real SCN400 level 0 is ≲ 300 000 px per side).
const MAX_SIDE: u64 = 1_000_000;
/// Per-level tile grid cap (bigger grids are not SCN400 scans; also keeps
/// the offsets/counts arrays far under the tag-value read cap).
const MAX_TILES_PER_LEVEL: u64 = 4_000_000;
const MAX_LEVELS: usize = 32;
/// Cap of a single probe payload read (scan_jpeg only needs up to the SOS).
pub const PROBE_LIMIT: u64 = 256 * 1024;
/// Cap on the SCN XML description (the structural parse input; the reader's
/// own tag-value cap is 4 MiB — the XML of a pathological file cannot hide
/// a whole pyramid).
const MAX_XML_BYTES: u64 = 4 << 20;

/// True colorspace of a level's JPEG payloads (same rule as SVS).
pub use crate::jpeg::TiffJpegColor as PayloadColor;

// --------------------------------------------------------------------------- //
// TIFF vendor routing (bounded; shared by the CLI and the wasm bindings)
// --------------------------------------------------------------------------- //

/// Vendor classification of a TIFF container by its IFD 0 description.
/// This is a ROUTING decision only — the authoritative accept rules live in
/// the adapters themselves.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TiffVendor {
    /// Aperio SVS (`ImageDescription` names Aperio) → `svs.rs`.
    AperioSvs,
    /// Leica SCN (`<scn>` XML with a leica-microsystems.com/scn namespace)
    /// → this module.
    LeicaScn,
    /// OME-TIFF — already platform-readable, NOT a conversion input.
    OmeTiff,
    /// This converter's own output (description JSON with a `source_format`
    /// in the converter vocabulary) — NOT a conversion input.
    ConverterBigTiff,
    /// A structurally valid TIFF naming no known vendor.
    Unknown,
}

/// Converter output ids (the same vocabulary as `upload_direct_class.py`
/// and `static/upload/slide-sniff.js`; a description JSON carrying one of
/// these as `source_format` marks a converter-produced BigTIFF).
pub const CONVERTER_SOURCE_FORMATS: [&str; 5] = [
    "kfb_bf_v1",
    "kfb_kfbio_jpeg",
    "aperio-svs-jpeg",
    "mirax-bundle",
    SOURCE_FORMAT,
];

fn desc_is_ome(desc: &str) -> bool {
    let head = &desc[..desc.len().min(4096)];
    head.starts_with("<?xml") && head[..head.len().min(2048)].contains("OME")
}

fn desc_is_converter_json(desc: &str) -> bool {
    let body = desc.split('\0').next().unwrap_or("").trim();
    if !body.starts_with('{') {
        return false;
    }
    // bounded substring probe — no JSON parser dependency
    for id in CONVERTER_SOURCE_FORMATS {
        if body.contains(&format!("\"source_format\": \"{id}\""))
            || body.contains(&format!("\"source_format\":\"{id}\""))
        {
            return true;
        }
    }
    false
}

fn desc_is_leica_scn(desc: &str) -> bool {
    desc.contains("<scn") && desc.contains("leica-microsystems.com/scn")
}

/// Classify a TIFF/BigTIFF container by its IFD 0 ImageDescription. Reads
/// the header, IFD 0 and one bounded description value — never the whole
/// file. Non-TIFF inputs are a typed error (callers route by magic first).
pub fn sniff_tiff_vendor(src: &dyn ByteSource) -> CoreResult<TiffVendor> {
    let hdr = tiff_read::read_header(src)?;
    let first = tiff_read::read_ifd(src, &hdr, hdr.first_ifd)?;
    let desc = match first.find(270) {
        Some(e) => {
            if e.value_len().unwrap_or(u64::MAX) > MAX_XML_BYTES {
                return Err(CoreError::oob("IFD 0 描述长度异常"));
            }
            let raw = tiff_read::entry_value(src, &hdr, e)?;
            String::from_utf8_lossy(&raw).to_string()
        }
        None => String::new(),
    };
    Ok(classify_description(&desc))
}

/// Pure description classifier (unit-tested without IO).
pub fn classify_description(desc: &str) -> TiffVendor {
    if desc_is_ome(desc) {
        return TiffVendor::OmeTiff;
    }
    if desc_is_converter_json(desc) {
        return TiffVendor::ConverterBigTiff;
    }
    if desc_is_leica_scn(desc) {
        return TiffVendor::LeicaScn;
    }
    if desc.contains("Aperio") {
        return TiffVendor::AperioSvs;
    }
    TiffVendor::Unknown
}

// --------------------------------------------------------------------------- //
// bounded XML helpers (hand-rolled; the crate carries no XML dependency)
// --------------------------------------------------------------------------- //

/// Opening tag text + inner text of every non-nested `<name>…</name>` span.
/// `self_closing` spans (`<name …/>`) yield empty inner text. Matching is
/// case-sensitive exactly like the vendor's own files write them.
fn tag_spans<'a>(xml: &'a str, name: &str) -> Vec<(&'a str, &'a str)> {
    let mut out = Vec::new();
    let mut from = 0usize;
    while let Some(rel) = xml[from..].find(&format!("<{name}")) {
        let start = from + rel;
        let after = &xml[start + 1 + name.len()..];
        // "<scnX" must not match "<scn"
        if !after.starts_with('>') && !after.starts_with('/') && !after.starts_with(|c: char| c.is_whitespace())
        {
            from = start + 1 + name.len();
            continue;
        }
        let Some(gt_rel) = after.find('>') else { break };
        let open_end = start + 1 + name.len() + gt_rel + 1;
        let open_tag = &xml[start..open_end];
        if open_tag.ends_with("/>") {
            out.push((open_tag, ""));
            from = open_end;
            continue;
        }
        let close = format!("</{name}>");
        let inner_end = xml[open_end..].find(&close).map(|p| open_end + p);
        let Some(end) = inner_end else { break };
        out.push((open_tag, &xml[open_end..end]));
        from = end + close.len();
    }
    out
}

/// `k="v"` / `k='v'` attributes of one opening tag (order preserved).
fn tag_attrs(open_tag: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let bytes = open_tag.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b'=' && i + 1 < bytes.len() {
            // attribute name: backtrack over [-A-Za-z0-9_:]
            let name_start = open_tag[..i]
                .char_indices()
                .rev()
                .take_while(|(_, c)| c.is_ascii_alphanumeric() || matches!(*c, '-' | '_' | ':'))
                .last()
                .map(|(p, _)| p);
            let quote = bytes[i + 1];
            let quoted = quote == b'"' || quote == b'\'';
            if quoted && name_start.is_some() {
                let p = name_start.unwrap();
                if let Some(end_rel) = open_tag[i + 2..].find(quote as char) {
                    out.push((
                        open_tag[p..i].to_string(),
                        open_tag[i + 2..i + 2 + end_rel].to_string(),
                    ));
                    i = i + 2 + end_rel + 1;
                    continue;
                }
            }
        }
        i += 1;
    }
    out
}

fn attr<'a>(attrs: &'a [(String, String)], name: &str) -> Option<&'a str> {
    attrs
        .iter()
        .find(|(k, _)| k == name)
        .map(|(_, v)| v.as_str())
}

fn attr_u64(attrs: &[(String, String)], name: &str) -> Option<u64> {
    attr(attrs, name)?.trim().parse::<u64>().ok()
}

fn text_of(inner: &str) -> String {
    inner.trim().trim_matches('\0').to_string()
}

// --------------------------------------------------------------------------- //
// document model
// --------------------------------------------------------------------------- //

#[derive(Debug, Clone)]
pub struct ScnLevel {
    /// XML pyramid index (r), in document order (strictly increasing).
    pub r: u32,
    /// Index in the source IFD chain (reporting/provenance only).
    pub ifd_index: u32,
    pub width: u32,
    pub height: u32,
    pub tile_w: u32,
    pub tile_h: u32,
    pub tiles_across: u32,
    pub tiles_down: u32,
    /// Full grid size (across × down) — the OUTPUT tile count.
    pub tiles_total: u64,
    /// Present (non-empty) source tiles; `tiles_total - tiles_present` are
    /// filled by the conversion.
    pub tiles_present: u64,
    /// Sum of TileByteCounts over present tiles (streamed; bounds-checked).
    pub payload_bytes: u64,
    /// True JPEG colorspace determined from the payloads.
    pub color: PayloadColor,
    /// SOF sampling (the truth; the TIFF 530 tag is not trusted).
    pub sampling: (u8, u8, u8, u8, u8, u8),
    /// Tag 347 bytes (verbatim) when the level shares JPEG tables.
    pub jpeg_tables: Option<Vec<u8>>,
}

impl ScnLevel {
    pub fn tiles_missing(&self) -> u64 {
        self.tiles_total.saturating_sub(self.tiles_present)
    }
}

#[derive(Debug, Clone)]
pub struct ScnDoc {
    pub kind: TiffKind,
    pub levels: Vec<ScnLevel>,
    /// µm/px from `<view sizeX/sizeY>` (nanometres) over `<pixels>` — None
    /// when either is absent/non-positive.
    pub mpp: Option<f64>,
    /// Objective from `<scanSettings><objectiveSettings><objective>`.
    pub objective: Option<f64>,
    /// `<illuminationSource>` of the main image (lowercased; absent = None).
    pub illumination: Option<String>,
    /// Other collection images (label/macro/preview) — detected, NOT
    /// exported.
    pub associated: Vec<AssociatedSummary>,
    /// Byte length of the SCN XML description (accounting/reporting).
    pub xml_bytes: u64,
    /// The probe's charged memory account (convert re-uses the reservation).
    pub budget: MemBudget,
}

#[derive(Debug, Clone)]
struct XmlImage {
    size_x: u64,
    size_y: u64,
    dims: Vec<(u32, u32, u32, u32)>, // (r, ifd, sizeX, sizeY)
    illumination: Option<String>,
    objective: Option<f64>,
    view_x: Option<u64>,
}

fn parse_scn_xml(xml: &str) -> CoreResult<(Vec<XmlImage>, u64)> {
    let xml_len = xml.len() as u64;
    let roots = tag_spans(xml, "scn");
    let Some((root_tag, root_inner)) = roots.first().copied() else {
        return Err(CoreError::variant(
            "描述 XML 没有 <scn> 根元素：不是 Leica SCN",
        ));
    };
    let ns_ok = tag_attrs(root_tag)
        .iter()
        .any(|(k, v)| k == "xmlns" && v.contains("leica-microsystems.com/scn"))
        || root_inner.contains("leica-microsystems.com/scn");
    if !ns_ok {
        return Err(CoreError::variant(
            "描述 XML 不是 Leica SCN 命名空间（leica-microsystems.com/scn）",
        ));
    }
    let collections = tag_spans(root_inner, "collection");
    let mut images = Vec::new();
    for (coll_tag, coll_inner) in collections {
        let _ = coll_tag;
        for (img_tag, img_inner) in tag_spans(coll_inner, "image") {
            let _ = img_tag;
            let px = tag_spans(img_inner, "pixels")
                .first()
                .map(|(_, inner)| *inner)
                .unwrap_or("");
            let px_attrs = tag_spans(img_inner, "pixels")
                .first()
                .map(|(tag, _)| tag_attrs(tag))
                .unwrap_or_default();
            let dims = tag_spans(px, "dimension")
                .iter()
                .filter_map(|(tag, _)| {
                    let a = tag_attrs(tag);
                    Some((
                        u32::try_from(attr_u64(&a, "r")?).ok()?,
                        u32::try_from(attr_u64(&a, "ifd")?).ok()?,
                        u32::try_from(attr_u64(&a, "sizeX")?).ok()?,
                        u32::try_from(attr_u64(&a, "sizeY")?).ok()?,
                    ))
                })
                .collect();
            let view = tag_spans(img_inner, "view").first().map(|(t, _)| t.to_string());
            let view_x = view.as_deref().and_then(|t| attr_u64(&tag_attrs(t), "sizeX"));
            let illumination = tag_spans(img_inner, "illuminationSource")
                .first()
                .map(|(_, inner)| text_of(inner).to_lowercase());
            let objective = tag_spans(img_inner, "objective")
                .first()
                .and_then(|(_, inner)| text_of(inner).parse::<f64>().ok())
                .filter(|v| v.is_finite() && *v > 0.0);
            images.push(XmlImage {
                size_x: attr_u64(&px_attrs, "sizeX").unwrap_or(0),
                size_y: attr_u64(&px_attrs, "sizeY").unwrap_or(0),
                dims,
                illumination,
                objective,
                view_x,
            });
        }
    }
    if images.is_empty() {
        return Err(CoreError::variant(
            "SCN XML 的 collection 没有任何 <image>：无法选择主图",
        ));
    }
    Ok((images, xml_len))
}

fn get_u64(src: &dyn ByteSource, hdr: &TiffHeader, ifd: &Ifd, tag: u16) -> CoreResult<Option<u64>> {
    tiff_read::find_u64(src, hdr, ifd, tag)
}

/// Read the first present tile payload of an IFD (bounded) for the JPEG
/// truth probe. Missing (zero) tiles are skipped.
fn first_tile_head(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
) -> CoreResult<Option<Vec<u8>>> {
    let mut cur = tiff_read::TileCursor::new(src, hdr, ifd)?;
    while let Some((off, len)) = cur.next_pair_allow_zero()? {
        if off == 0 || len == 0 {
            continue;
        }
        let want = len.min(PROBE_LIMIT) as usize;
        return Ok(Some(src.read_at(off, want)?));
    }
    Ok(None)
}

/// Verify one level IFD's tile geometry and determine the JPEG truth
/// (mirrors the SVS level inspection, with SCN's missing-tile semantics).
fn inspect_level(
    src: &dyn ByteSource,
    hdr: &TiffHeader,
    ifd: &Ifd,
    ifd_index: u32,
    xml_w: u32,
    xml_h: u32,
    photometric: u64,
) -> CoreResult<ScnLevel> {
    let width = get_u64(src, hdr, ifd, 256)?.unwrap_or(0);
    let height = get_u64(src, hdr, ifd, 257)?.unwrap_or(0);
    // per-side cap (SVS parity): the 4 M tile-grid cap alone does not bound
    // a single side (u32-max × 1 passes it), so enforce MAX_SIDE explicitly
    if !(1..=MAX_SIDE).contains(&width) || !(1..=MAX_SIDE).contains(&height) {
        return Err(CoreError::variant(format!(
            "层级尺寸 {width}×{height} 越界（单边上限 {MAX_SIDE}）"
        )));
    }
    if width != xml_w as u64 || height != xml_h as u64 {
        return Err(CoreError::validation(format!(
            "层 IFD{ifd_index} 尺寸 {width}×{height} 与 XML dimension {}×{} 不符",
            xml_w, xml_h
        )));
    }
    let tw = get_u64(src, hdr, ifd, 322)?.unwrap_or(0);
    let th = get_u64(src, hdr, ifd, 323)?.unwrap_or(0);
    if !(16..=8192).contains(&tw) || !(16..=8192).contains(&th) {
        return Err(CoreError::variant(format!(
            "tile 尺寸 {tw}×{th} 不在支持范围（16–8192）"
        )));
    }
    let across = (width + tw - 1) / tw;
    let down = (height + th - 1) / th;
    let total = across
        .checked_mul(down)
        .ok_or_else(|| CoreError::variant("tile 网格数量溢出"))?;
    if total < 1 || total > MAX_TILES_PER_LEVEL {
        return Err(CoreError::variant(format!("tile 数 {total} 越界")));
    }
    let mut cur = tiff_read::TileCursor::new(src, hdr, ifd)?;
    if cur.total() != total {
        return Err(CoreError::validation(format!(
            "TileOffsets 数 {} ≠ 网格 {}×{}（Leica SCN 层必须声明完整网格；空缺 tile 用 (0,0) 表达）",
            cur.total(),
            across,
            down
        )));
    }
    // stream every (offset,count) pair: present payloads are bounds-checked
    // and summed; (0,0) entries are the sparse-grid missing tiles
    let mut payload_bytes = 0u64;
    let mut present = 0u64;
    while let Some((off, len)) = cur.next_pair_allow_zero()? {
        if off == 0 && len == 0 {
            continue;
        }
        if off == 0 || len == 0 {
            return Err(CoreError::validation(format!(
                "层 IFD{ifd_index} 存在半零 tile 记录（offset={off}, length={len}）：损坏"
            )));
        }
        let end = off.checked_add(len).ok_or_else(|| CoreError::oob("tile 区间溢出"))?;
        if end > hdr.size {
            return Err(CoreError::oob(format!(
                "tile 区间 [{off},+{len}) 超出文件 {}",
                hdr.size
            )));
        }
        payload_bytes = payload_bytes.saturating_add(len);
        present += 1;
    }
    let jpeg_tables = match ifd.find(347) {
        Some(e) => Some(tiff_read::entry_value(src, hdr, e)?),
        None => None,
    };
    let head = first_tile_head(src, hdr, ifd)?.ok_or_else(|| {
        CoreError::variant(format!("层 IFD{ifd_index} 没有任何在场的 tile"))
    })?;
    let probe_full = match &jpeg_tables {
        Some(t) => crate::svs::merge_tables_then_tile(t, &head),
        None => head.clone(),
    };
    let probe = jpeg::scan_jpeg(&probe_full).map_err(|e| {
        CoreError::jpeg(format!(
            "首 tile 不是合法 JPEG（合并 JPEGTables 后）：{}",
            e.message
        ))
    })?;
    let sampling = probe.sampling.ok_or_else(|| {
        CoreError::variant("JPEG 不是三分量（灰度/荧光 tile 不适用于明场转换）")
    })?;
    if jpeg_tables.is_none() {
        let q = jpeg::qtables_pillow_style(&probe_full);
        if q.is_none() || q.as_deref().map_or(true, |v| v.is_empty()) {
            return Err(CoreError::variant(
                "tile 流既无共享 JPEGTables 也无自带量化表，无法按原样搬运",
            ));
        }
    }
    if probe.width as u64 > tw || probe.height as u64 > th {
        return Err(CoreError::variant(format!(
            "首 tile 尺寸 {}×{} 大于 tile 标称 {}×{}",
            probe.width, probe.height, tw, th
        )));
    }
    let color = jpeg::tiff_jpeg_color(&probe, photometric);
    Ok(ScnLevel {
        r: 0, // filled by the caller
        ifd_index,
        width: width as u32,
        height: height as u32,
        tile_w: tw as u32,
        tile_h: th as u32,
        tiles_across: across as u32,
        tiles_down: down as u32,
        tiles_total: total,
        tiles_present: present,
        payload_bytes,
        color,
        sampling,
        jpeg_tables,
    })
}

/// Capability probe with an explicit host budget: every allocation of the
/// structural walk (IFD chain, XML, per-image records) is charged BEFORE it
/// is made; an over-budget charge is the stable typed refusal
/// `resource_profile_insufficient` — never an OOM after the fact.
pub fn probe_scn_with_budget(src: &dyn ByteSource, budget_bytes: u64) -> CoreResult<ScnDoc> {
    let mut budget = MemBudget::host(budget_bytes);
    // the IFD chain walk is bounded by MAX_IFDS × MAX_IFD_ENTRIES entries;
    // charge the structural worst case before it is built
    budget.charge(
        (tiff_read::MAX_IFDS * tiff_read::MAX_IFD_ENTRIES * 32) as u64,
        "IFD 链结构预留",
    )?;
    let hdr = tiff_read::read_header(src)?;
    let chain = tiff_read::ifd_chain(src, &hdr)?;

    // ---- IFD 0: the label/preview image carrying the SCN XML ----------- //
    let main_ifd = &chain[0];
    budget.charge(
        main_ifd
            .find(270)
            .and_then(|e| e.value_len())
            .unwrap_or(0)
            .min(MAX_XML_BYTES),
        "SCN XML 描述",
    )?;
    let desc_raw = match main_ifd.find(270) {
        Some(e) => tiff_read::entry_value(src, &hdr, e)?,
        None => Vec::new(),
    };
    let desc = String::from_utf8_lossy(&desc_raw).to_string();
    if classify_description(&desc) != TiffVendor::LeicaScn {
        return Err(CoreError::variant(
            "IFD 0 描述不是 Leica SCN XML（<scn> + leica-microsystems.com/scn 命名空间）",
        ));
    }
    let (images, xml_len) = parse_scn_xml(&desc)?;
    budget.charge(images.len() as u64 * 96, "SCN image 记录")?;

    // ---- main image selection ------------------------------------------ //
    let main = images
        .iter()
        .enumerate()
        .filter(|(_, im)| !im.dims.is_empty() && im.size_x > 0 && im.size_y > 0)
        .max_by_key(|(_, im)| im.size_x.saturating_mul(im.size_y))
        .map(|(i, im)| (i, im));
    let Some((_main_idx, main_img)) = main else {
        return Err(CoreError::variant(
            "SCN XML 没有带 <pixels>/<dimension> 的 image：无主图可选",
        ));
    };
    match main_img.illumination.as_deref() {
        Some("brightfield") | None => {}
        Some(other) => {
            return Err(CoreError::variant(format!(
                "主图 illuminationSource={other:?}：荧光 SCN 不在明场转换支持集（复制前拒绝）"
            )))
        }
    }

    // ---- pyramid levels of the main image ------------------------------- //
    let mut dims = main_img.dims.clone();
    dims.sort_by_key(|(r, _, _, _)| *r);
    if dims.is_empty() {
        return Err(CoreError::variant("主图没有 <dimension> 金字塔层"));
    }
    if dims.len() > MAX_LEVELS {
        return Err(CoreError::variant(format!("层级数超过 {MAX_LEVELS}")));
    }
    for (i, (r, _, _, _)) in dims.iter().enumerate() {
        if *r as usize != i {
            return Err(CoreError::variant(format!(
                "层序号 r={r} 不连续（位置 {i}）：未知金字塔布局"
            )));
        }
        if i > 0 {
            let (_, _, pw, ph) = dims[i - 1];
            let (_, _, w, h) = dims[i];
            if w >= pw || h >= ph {
                return Err(CoreError::variant(format!(
                    "层 r={r}（{w}×{h}）未小于上一層（{pw}×{ph}）：不是严格递减金字塔"
                )));
            }
            let rx = pw as f64 / w as f64;
            let ry = ph as f64 / h as f64;
            if !(1.5..=16.0).contains(&rx) || !(1.5..=16.0).contains(&ry) {
                return Err(CoreError::variant(format!(
                    "层 r={r} 降采样比异常（x={rx:.3}, y={ry:.3}）：不是已知的 SCN 层级序列",
                )));
            }
        }
    }
    let mut levels = Vec::with_capacity(dims.len());
    for (r, ifd_index, w, h) in dims {
        let Some(ifd) = chain.get(ifd_index as usize) else {
            return Err(CoreError::validation(format!(
                "XML dimension r={r} 指向 IFD{ifd_index}，但文件只有 {} 个 IFD",
                chain.len()
            )));
        };
        let compression = get_u64(src, &hdr, ifd, 259)?.unwrap_or(0);
        let photo = get_u64(src, &hdr, ifd, 262)?.unwrap_or(0);
        let samples = get_u64(src, &hdr, ifd, 277)?.unwrap_or(1);
        let planar = get_u64(src, &hdr, ifd, 284)?.unwrap_or(1);
        match compression {
            7 => {}
            33003 | 33005 => {
                return Err(CoreError::variant(format!(
                    "层 r={r} 为 JPEG 2000 压缩（{compression}）：不在支持集，复制前拒绝"
                )))
            }
            c => {
                return Err(CoreError::variant(format!(
                    "层 r={r} 压缩编码 {c} 不是基线 JPEG（259=7），无法按原样搬运"
                )))
            }
        }
        if planar != 1 {
            return Err(CoreError::variant(format!(
                "层 r={r} PlanarConfiguration={planar}（平面存储）不在支持集"
            )));
        }
        if samples != 3 {
            return Err(CoreError::variant(format!(
                "层 r={r} SamplesPerPixel={samples}（荧光/多通道或灰度不在明场支持集）"
            )));
        }
        if let Some(e) = ifd.find(258) {
            let bits = tiff_read::entry_value(src, &hdr, e)?;
            let vals: Vec<u16> = bits
                .chunks_exact(2)
                .map(|c| {
                    let v = [c[0], c[1]];
                    if hdr.little { u16::from_le_bytes(v) } else { u16::from_be_bytes(v) }
                })
                .collect();
            if vals != [8, 8, 8] {
                return Err(CoreError::variant(format!(
                    "层 r={r} BitsPerSample={vals:?} ≠ [8,8,8]"
                )));
            }
        }
        if photo != 2 && photo != 6 {
            return Err(CoreError::variant(format!(
                "层 r={r} PhotometricInterpretation={photo} 不在支持集（RGB=2 / YCbCr=6）"
            )));
        }
        let mut lv = inspect_level(src, &hdr, ifd, ifd_index, w, h, photo)?;
        lv.r = r;
        levels.push(lv);
    }

    // ---- associated: every non-main image of the collection ------------- //
    let mut associated = Vec::new();
    for (i, im) in images.iter().enumerate() {
        if i == _main_idx {
            continue;
        }
        let name = if associated.is_empty() { "label" } else { "macro" };
        associated.push(AssociatedSummary {
            name: name.to_string(),
            source_offset: 0, // not exported; offsets are not recorded
            source_length: 0,
            width: im.size_x.min(u32::MAX as u64) as u32,
            height: im.size_y.min(u32::MAX as u64) as u32,
        });
    }

    // ---- calibration ----------------------------------------------------- //
    let mpp = match (main_img.view_x, main_img.size_x) {
        (Some(vx), sx) if vx > 0 && sx > 0 => {
            let v = (vx as f64 / sx as f64) / 1000.0;
            (v.is_finite() && v > 0.0).then_some(v)
        }
        _ => None,
    };
    Ok(ScnDoc {
        kind: hdr.kind,
        levels,
        mpp,
        objective: main_img.objective,
        illumination: main_img.illumination.clone(),
        associated,
        xml_bytes: xml_len,
        budget,
    })
}

/// Capability probe at the conservative saver budget (CLI default).
pub fn probe_scn(src: &dyn ByteSource) -> CoreResult<ScnDoc> {
    probe_scn_with_budget(src, crate::budget::SAVER_BUDGET_BYTES)
}

/// Disk-precheck estimate for the SCN adapter (same shape as the SVS one).
pub fn estimate_scn(doc: &ScnDoc) -> crate::estimate::OutputEstimate {
    let mut payload = 0u64;
    let mut tiles = 0u64;
    let mut missing = 0u64;
    for lv in &doc.levels {
        payload = payload.saturating_add(lv.payload_bytes);
        tiles += lv.tiles_total;
        missing += lv.tiles_missing();
    }
    let extras = doc
        .levels
        .iter()
        .map(|l| l.jpeg_tables.as_ref().map_or(0, |t| t.len() as u64))
        .sum::<u64>();
    let ifds = doc.levels.len() as u64;
    let out = payload
        .saturating_mul(5)
        .div_ceil(4)
        .saturating_add(missing.saturating_mul(8 * 1024)) // generated fill tiles
        .saturating_add(tiles.saturating_mul(16))
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(extras)
        .saturating_add(1024 * 1024);
    let compact = payload
        .saturating_mul(3)
        .div_ceil(2)
        .saturating_add(missing.saturating_mul(8 * 1024))
        .saturating_add(tiles.saturating_mul(16))
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(1024 * 1024);
    crate::estimate::OutputEstimate {
        payload_bytes: payload,
        tiles_present: tiles - missing,
        cells_total: tiles,
        cells_missing: missing,
        edge_tiles: 0, // passthrough: edge tiles are copied, not re-encoded
        ifds,
        output_upper_bound_bytes: out,
        compact_upper_bound_bytes: compact,
    }
}
