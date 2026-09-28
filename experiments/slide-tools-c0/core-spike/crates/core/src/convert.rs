//! Conversion pipeline (port of `kfb/converter.py::convert_kfb`, minus the
//! oracle's tifffile re-validation pass which the differential harness
//! performs externally): full 256×256 JPEG tiles are copied byte-for-byte;
//! edge tiles are re-encoded per `edge::reencode_edge_tile`; output is the
//! same classic multi-IFD JPEG tiled BigTIFF layout.

use crate::bigtiff::BigTiffPyramidWriter;
use crate::edge::{reencode_edge_tile, Subsampling, WARN_EDGE_REENCODE_FALLBACK_Q95};
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::jpeg::{scan_jpeg, Sampling6};
use crate::kfb::{parse_kfb, KfbDocument, KfbLevel};

pub const DEFAULT_MAX_OUTPUT_BYTES: u64 = 64 * 1024 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct ConvertOptions {
    pub max_output_bytes: u64,
}

impl Default for ConvertOptions {
    fn default() -> Self {
        ConvertOptions { max_output_bytes: DEFAULT_MAX_OUTPUT_BYTES }
    }
}

#[derive(Debug, Clone)]
pub struct LevelStat {
    pub level: u32,
    pub width: u32,
    pub height: u32,
    pub tiles_across: u32,
    pub tiles_down: u32,
    pub tiles_total: u64,
    pub tiles_raw_copied: u64,
    pub tiles_reencoded: u64,
}

#[derive(Debug, Clone, Default)]
pub struct ConvertStats {
    pub levels: Vec<LevelStat>,
    pub warnings: Vec<String>,
    pub output_bytes: u64,
    pub source_format: String,
    pub scanner_id: String,
    pub mpp_x: f64,
    pub mpp_y: f64,
    pub objective: f64,
}

/// Python `json.dumps(..., ensure_ascii=True, sort_keys=True)` compatible
/// float repr: shortest round-trip, always with a decimal point or
/// exponent. Divergence from CPython: CPython switches to exponent notation
/// outside [1e-4, 1e16); this helper never does. MPP/objective values are
/// physically ~0.1–100, where both notations agree.
fn py_repr_f64(v: f64) -> String {
    let mut s = format!("{v}");
    if !s.contains('.') && !s.contains('e') && !s.contains('E') {
        s.push_str(".0");
    }
    s
}

fn json_escape_ascii(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7E => {
                out.push_str(&format!("\\u{:04x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out
}

pub fn build_description(
    source_format: &str,
    scanner_id: &str,
    mpp_x: f64,
    mpp_y: f64,
    objective: f64,
) -> Vec<u8> {
    format!(
        "{{\"mpp_x\": {}, \"mpp_y\": {}, \"objective\": {}, \"scanner_id\": \"{}\", \"source_format\": \"{}\"}}\u{0}",
        py_repr_f64(mpp_x),
        py_repr_f64(mpp_y),
        py_repr_f64(objective),
        json_escape_ascii(scanner_id),
        source_format,
    )
    .into_bytes()
}

/// 选要写入的层：升序遍历，包含首个 1×1 网格层（含）为止；中间缺整层
/// （网格 >1×1 却无 tile）→ conversion_validation_failed。
fn select_levels(doc: &KfbDocument) -> CoreResult<Vec<KfbLevel>> {
    let mut selected = Vec::new();
    let mut stopped = false;
    for lv in &doc.levels {
        if stopped {
            break;
        }
        let present = doc.grids.present_count(lv.level);
        let single = lv.tiles_across() == 1 && lv.tiles_down() == 1;
        if present == 0 {
            if single {
                break; // 文件本身没有该冗余层：正常停止
            }
            return Err(CoreError::validation(format!(
                "层 {}（{}×{}）在索引中无任何 tile",
                lv.level, lv.width, lv.height
            )));
        }
        selected.push(*lv);
        if single {
            stopped = true;
        }
    }
    if selected.is_empty() {
        return Err(CoreError::validation("无可写入的层级"));
    }
    Ok(selected)
}

/// 采样映射（Python `_PILLOW_SUBSAMPLING` / `_TIFF_SUBSAMPLING` 支持集）。
fn tiff_subsampling(s: Sampling6) -> Option<(u16, u16)> {
    match s {
        (1, 1, 1, 1, 1, 1) => Some((1, 1)),
        (2, 1, 1, 1, 1, 1) => Some((2, 1)),
        (2, 2, 1, 1, 1, 1) => Some((2, 2)),
        _ => None,
    }
}

/// 网格覆盖完整性 + 单 tile 尺寸上限（Python `_level_grid` 的校验部分）。
/// 覆盖缺失在 present_count 与网格 cell 数不一致时即判定（与 Python 的
/// `len(grid) != expected` 同等判定），首缺坐标从占用位图推出。
fn validate_grid(doc: &KfbDocument, lv: &KfbLevel) -> CoreResult<()> {
    let expected = lv.tiles_across() as u64 * lv.tiles_down() as u64;
    let present = doc.grids.present_count(lv.level) as u64;
    if present != expected {
        let mut missing = Vec::new();
        let ta = lv.tiles_across();
        doc.grids.for_each_cell(lv.level, |cell, rec| {
            if rec.is_none() && missing.len() < 3 {
                missing.push((cell / ta, cell % ta));
            }
            Ok(())
        })?;
        let miss_n = expected - present;
        return Err(CoreError::validation(format!(
            "层 {} 网格覆盖不全：缺 {}/{}（首缺 {:?}）",
            lv.level, miss_n, expected, missing
        )));
    }
    doc.grids.for_each_cell(lv.level, |_cell, rec| {
        if let Some(t) = rec {
            let row = t.y / 256;
            let col = t.x / 256;
            let want_w = 256.min(lv.width.saturating_sub(col * 256));
            let want_h = 256.min(lv.height.saturating_sub(row * 256));
            if t.jpeg_w as u32 > want_w || t.jpeg_h as u32 > want_h {
                return Err(CoreError::validation(format!(
                    "层 {} tile({},{}) 尺寸 {}×{} 超出网格 {}×{}",
                    lv.level, row, col, t.jpeg_w, t.jpeg_h, want_w, want_h
                )));
            }
        }
        Ok(())
    })
}

/// 该层 IFD 的采样：第一个完整 tile 的 SOF；全部完整 tile 必须一致；
/// 无完整 tile 时取首个有采样的 tile。
fn level_sampling(
    doc: &KfbDocument,
    src: &dyn ByteSource,
    lv: &KfbLevel,
) -> CoreResult<(u16, u16)> {
    let mut sampling: Option<Sampling6> = None;
    doc.grids.for_each_cell(lv.level, |_cell, rec| {
        let t = match rec {
            Some(t) => t,
            None => return Ok(()),
        };
        if t.is_full_tile() {
            let payload = src.read_at(t.payload_offset, t.payload_length as usize)?;
            let probe = scan_jpeg(&payload)?;
            let s = probe.sampling.ok_or_else(|| {
                CoreError::validation(format!(
                    "层 {} tile({},{}) 不是三分量 JPEG",
                    lv.level, t.y / 256, t.x / 256
                ))
            })?;
            match sampling {
                None => sampling = Some(s),
                Some(prev) if prev != s => {
                    return Err(CoreError::validation(format!(
                        "层 {} tile 采样不一致：{:?} vs {:?}",
                        lv.level, prev, s
                    )));
                }
                _ => {}
            }
        }
        Ok(())
    })?;
    if sampling.is_none() {
        // 整层没有完整 tile（如 1×1 小层）：用重编码 tile 的采样
        let mut found: Option<Sampling6> = None;
        doc.grids.for_each_cell(lv.level, |_cell, rec| {
            if found.is_none() {
                if let Some(t) = rec {
                    let payload = src.read_at(t.payload_offset, t.payload_length as usize)?;
                    if let Some(s) = scan_jpeg(&payload)?.sampling {
                        found = Some(s);
                    }
                }
            }
            Ok(())
        })?;
        sampling = found;
    }
    let s = sampling.ok_or_else(|| {
        CoreError::validation(format!("层 {} 无可用采样", lv.level))
    })?;
    tiff_subsampling(s).ok_or_else(|| {
        CoreError::validation(format!(
            "层 {} JPEG 采样 {:?} 不在支持集（4:4:4/4:2:2/4:2:0）",
            lv.level, s
        ))
    })
}

pub fn convert_kfb(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    opts: &ConvertOptions,
) -> CoreResult<ConvertStats> {
    let doc = parse_kfb(src, scratch)?;
    convert_document(&doc, src, sink, scratch, opts)
}

pub fn convert_document(
    doc: &KfbDocument,
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    opts: &ConvertOptions,
) -> CoreResult<ConvertStats> {
    let header = &doc.header;
    if !header.brightfield {
        return Err(CoreError::variant("非明场 KFB（flags bit0=0）"));
    }
    if !header.mpp_x.is_finite() || header.mpp_x <= 0.0 || !header.mpp_y.is_finite() || header.mpp_y <= 0.0 {
        return Err(CoreError::metadata("MPP 缺失/非法"));
    }

    let levels = select_levels(doc)?;
    let source_format = if header.version != 1 { "kfb_kfbio_jpeg" } else { "kfb_bf_v1" };
    let description =
        build_description(source_format, &header.scanner_id, header.mpp_x, header.mpp_y, header.objective);

    let mut stats = ConvertStats {
        source_format: source_format.to_string(),
        scanner_id: header.scanner_id.clone(),
        mpp_x: header.mpp_x,
        mpp_y: header.mpp_y,
        objective: header.objective,
        ..Default::default()
    };

    let mut writer = BigTiffPyramidWriter::new(sink)?;
    for (idx, lv) in levels.iter().enumerate() {
        validate_grid(doc, lv)?;
        let sub = level_sampling(doc, src, lv)?;

        writer.begin_level(scratch)?;
        let mut raw_copied = 0u64;
        let mut reencoded = 0u64;
        let mut tiles_total = 0u64;
        doc.grids.for_each_cell(lv.level, |_cell, rec| {
            let t = rec.ok_or_else(|| CoreError::validation("网格覆盖不全（内部错误）"))?;
            let payload = src.read_at(t.payload_offset, t.payload_length as usize)?;
            let data: Vec<u8>;
            if t.is_full_tile() {
                data = payload;
                raw_copied += 1;
            } else {
                let (bytes, reused) =
                    reencode_edge_tile(&payload, t.jpeg_w, t.jpeg_h, Subsampling(sub.0, sub.1))?;
                if !reused && !stats.warnings.contains(&WARN_EDGE_REENCODE_FALLBACK_Q95.to_string()) {
                    stats.warnings.push(WARN_EDGE_REENCODE_FALLBACK_Q95.to_string());
                }
                data = bytes;
                reencoded += 1;
            }
            writer.write_tile(&data)?;
            tiles_total += 1;
            Ok(())
        })?;
        if writer.cursor() > opts.max_output_bytes {
            return Err(CoreError::too_large(format!(
                "输出已写 {} > {}",
                writer.cursor(),
                opts.max_output_bytes
            )));
        }
        writer.end_level(
            lv.width,
            lv.height,
            sub,
            header.mpp_x,
            header.mpp_y,
            &description,
            idx > 0,
        )?;
        stats.levels.push(LevelStat {
            level: lv.level,
            width: lv.width,
            height: lv.height,
            tiles_across: lv.tiles_across(),
            tiles_down: lv.tiles_down(),
            tiles_total,
            tiles_raw_copied: raw_copied,
            tiles_reencoded: reencoded,
        });
    }
    stats.output_bytes = writer.finish()?;
    sink.flush()?;
    Ok(stats)
}
