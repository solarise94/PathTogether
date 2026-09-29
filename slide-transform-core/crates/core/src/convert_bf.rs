//! Brightfield conversion: KFB → classic multi-IFD JPEG tiled BigTIFF
//! (byte-layout port of `kfb/converter.py`). Full 256×256 tiles are copied
//! byte-for-byte; edge tiles are decoded (libjpeg-faithful codec), pasted on
//! a white 256×256 canvas and re-encoded with the source quantization tables
//! (fallback: std q95 + warning) — byte-equal to the Pillow oracle. Under
//! `StrictLossless` any re-encode requirement is a typed policy error raised
//! before the first output byte is written.
//!
//! C2 resume: [`convert_kfb_to_bigtiff_resume`] reconstructs the writer from
//! a [`crate::resume::ResumePoint`] (committed output cursor + per-IFD
//! committed tile counts, offcnt streams preserve-opened from scratch) and
//! fast-forwards already-committed cells, reconstructing their report side
//! effects (stats / edge regions / warnings) from the index without writing.
//! The fresh path is untouched and stays byte-identical.

use crate::bigtiff::BigTiffPyramidWriter;
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::kfb::{KfbDocument, KfbLevel, parse_kfb};
use crate::ome::py_repr_f64;
use crate::plan::{PixelPolicy, TransformPlan};
use crate::report::{
    EdgeRegion, LevelStats, TransformResult, WARN_EDGE_REENCODE_FALLBACK_Q95,
    level_stats_from_kfb,
};
use crate::resume::ResumePoint;

const TILE: u32 = 256;
/// Sampling tuple (h1,v1,h2,v2,h3,v3) → (pillow-equivalent enum, TIFF (h,v)).
fn supported_sampling(
    s: (u8, u8, u8, u8, u8, u8),
) -> Option<(crate::jpeg::Sampling, (u16, u16))> {
    match s {
        (1, 1, 1, 1, 1, 1) => Some((crate::jpeg::Sampling::S444, (1, 1))),
        (2, 1, 1, 1, 1, 1) => Some((crate::jpeg::Sampling::S422, (2, 1))),
        (2, 2, 1, 1, 1, 1) => Some((crate::jpeg::Sampling::S420, (2, 2))),
        _ => None,
    }
}

fn json_escape_ascii(s: &str) -> String {
    // json.dumps(ensure_ascii=True)
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
            c if (c as u32) < 0x20 || (c as u32) >= 0x7F => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

fn description_bytes(
    source_format: &str,
    scanner_id: &str,
    mpp_x: f64,
    mpp_y: f64,
    objective: f64,
) -> Vec<u8> {
    // json.dumps(..., ensure_ascii=True, sort_keys=True) + "\0", ascii
    let s = format!(
        "{{\"mpp_x\": {}, \"mpp_y\": {}, \"objective\": {}, \"scanner_id\": \"{}\", \"source_format\": \"{}\"}}\u{0}",
        py_repr_f64(mpp_x),
        py_repr_f64(mpp_y),
        py_repr_f64(objective),
        json_escape_ascii(scanner_id),
        source_format,
    );
    s.into_bytes()
}

/// Select levels: ascending, include the first 1×1-grid level, stop after.
/// A missing non-single level is a hard validation failure (oracle parity).
fn select_levels(doc: &KfbDocument) -> CoreResult<Vec<&KfbLevel>> {
    let mut selected: Vec<&KfbLevel> = Vec::new();
    let mut stopped = false;
    for lv in &doc.levels {
        if stopped {
            break;
        }
        let present = doc.grids.present_count(lv.level) > 0;
        let single = lv.tiles_across() == 1 && lv.tiles_down() == 1;
        if !present {
            if single {
                break; // file simply lacks the redundant single-tile level
            }
            return Err(CoreError::validation(format!(
                "层 {}（{}×{}）在索引中无任何 tile",
                lv.level, lv.width, lv.height
            )));
        }
        selected.push(lv);
        if single {
            stopped = true;
        }
    }
    if selected.is_empty() {
        return Err(CoreError::validation("无可写入的层级"));
    }
    Ok(selected)
}

/// Strict-policy precheck: fail with `pixel_policy_violation` BEFORE writing
/// anything if any selected level has an edge tile (jpeg dims ≠ full tile)
/// or a grid cell whose jpeg is smaller than its cell.
fn strict_lossless_precheck(
    src: &dyn ByteSource,
    doc: &KfbDocument,
    levels: &[&KfbLevel],
) -> CoreResult<()> {
    for lv in levels {
        let mut edge = 0u64;
        doc.grids.for_each_cell(lv.level, |_cell, rec| {
            if let Some(rec) = rec {
                if rec.jpeg_w as u32 != TILE || rec.jpeg_h as u32 != TILE {
                    edge += 1;
                }
            } else {
                edge += 1; // missing cell → white fill → not lossless either
            }
            Ok(())
        })?;
        if edge > 0 {
            return Err(CoreError::policy(format!(
                "层 {} 有 {} 个需要重编码/填充的非完整 tile（StrictLossless）",
                lv.level, edge
            )));
        }
        let _ = src;
    }
    Ok(())
}

/// Per-level JPEG sampling. Full mode scans every full tile for consistency
/// (the original oracle-parity behavior). Quick mode (resume of an already
/// fully-committed level, whose input the host re-verified by content hash)
/// probes only the first full tile.
fn level_sampling(
    src: &dyn ByteSource,
    doc: &KfbDocument,
    lv: &KfbLevel,
    full_check: bool,
) -> CoreResult<(u8, u8, u8, u8, u8, u8)> {
    let mut sampling: Option<(u8, u8, u8, u8, u8, u8)> = None;
    doc.grids.for_each_cell(lv.level, |_cell, rec| {
        if !full_check && sampling.is_some() {
            return Ok(());
        }
        if let Some(rec) = rec {
            if rec.is_full_tile() || !full_check {
                let payload = src.read_at(rec.payload_offset, rec.payload_length as usize)?;
                let probe = crate::jpeg::scan_jpeg(&payload)?;
                let s = probe
                    .sampling
                    .ok_or_else(|| CoreError::validation(format!(
                        "层 {} tile({},{}) 不是三分量 JPEG",
                        lv.level, rec.y / TILE, rec.x / TILE
                    )))?;
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
        }
        Ok(())
    })?;
    if sampling.is_none() {
        doc.grids.for_each_cell(lv.level, |_cell, rec| {
            if sampling.is_some() {
                return Ok(());
            }
            if let Some(rec) = rec {
                let payload = src.read_at(rec.payload_offset, rec.payload_length as usize)?;
                if let Ok(probe) = crate::jpeg::scan_jpeg(&payload) {
                    sampling = probe.sampling;
                }
            }
            Ok(())
        })?;
    }
    sampling.ok_or_else(|| {
        CoreError::validation(format!("层 {} 无法确定 JPEG 采样", lv.level))
    })
}

/// Reconstruct the report side effects of already-committed cells (stats,
/// edge regions, warnings) from the index — payload is read only for edge
/// tiles (to recover the quantization-table reuse flag exactly like the
/// write path). `up_to` bounds the cells; `u64::MAX` = whole level.
fn reconstruct_cells_bf(
    src: &dyn ByteSource,
    doc: &KfbDocument,
    lv: &KfbLevel,
    up_to: u64,
    stats: &mut LevelStats,
    edge_regions: &mut Vec<EdgeRegion>,
    warnings: &mut Vec<String>,
) -> CoreResult<()> {
    doc.grids.for_each_cell(lv.level, |cell, rec| {
        if cell as u64 >= up_to {
            return Ok(());
        }
        let row = cell as u64 / lv.tiles_across() as u64;
        let col = cell as u64 % lv.tiles_across() as u64;
        let want_w = TILE.min(lv.width - (col as u32 * TILE));
        let want_h = TILE.min(lv.height - (row as u32 * TILE));
        let rec = match rec {
            Some(r) => r,
            None => {
                return Err(CoreError::validation(format!(
                    "层 {} 网格覆盖不全：缺 ({row},{col})（明场不允许稀疏）",
                    lv.level
                )));
            }
        };
        if rec.jpeg_w as u32 > want_w || rec.jpeg_h as u32 > want_h {
            return Err(CoreError::validation(format!(
                "层 {} tile({row},{col}) 尺寸 {}×{} 超出网格 {want_w}×{want_h}",
                lv.level, rec.jpeg_w, rec.jpeg_h
            )));
        }
        if rec.is_full_tile() {
            stats.tiles_raw_copied += 1;
        } else {
            let payload = src.read_at(rec.payload_offset, rec.payload_length as usize)?;
            let qtables = crate::jpeg::qtables_pillow_style(&payload).unwrap_or_default();
            let reused = qtables.len() >= 2;
            if !reused {
                warnings.push(WARN_EDGE_REENCODE_FALLBACK_Q95.to_string());
            }
            edge_regions.push(EdgeRegion {
                level: lv.level,
                channel: None,
                x: rec.x,
                y: rec.y,
                source_w: rec.jpeg_w as u32,
                source_h: rec.jpeg_h as u32,
                canvas_w: TILE,
                canvas_h: TILE,
                reused_qtables: reused,
            });
            stats.tiles_reencoded += 1;
        }
        Ok(())
    })
}

#[allow(clippy::too_many_arguments)]
pub fn convert_kfb_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None)
}

/// Resume a brightfield conversion from `resume` (see [`crate::resume`]).
#[allow(clippy::too_many_arguments)]
pub fn convert_kfb_to_bigtiff_resume(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, Some(resume))
}

#[allow(clippy::too_many_arguments)]
fn convert_inner(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: Option<&ResumePoint>,
) -> CoreResult<TransformResult> {
    let started = crate::job::WallInstant::now();
    let doc = parse_kfb(src, scratch)?;
    if !doc.header.brightfield {
        return Err(CoreError::variant(
            "非明场 KFB（flags bit0=0）；该 profile 仅转换明场",
        ));
    }
    if !doc.header.mpp_x.is_finite()
        || doc.header.mpp_x <= 0.0
        || !doc.header.mpp_y.is_finite()
        || doc.header.mpp_y <= 0.0
    {
        return Err(CoreError::metadata("MPP 缺失/非法"));
    }
    let levels = select_levels(&doc)?;
    if let Some(r) = resume {
        if r.level >= levels.len() {
            return Err(CoreError::validation("resume: level 越界"));
        }
        // (level, 0) states are never journalled, but accept them defensively.
        let expect = if r.cell > 0 { r.level + 1 } else { r.level };
        if r.ifd_tiles.len() != expect {
            return Err(CoreError::validation(format!(
                "resume: ifd_tiles 长度 {} 与 (level={}, cell={}) 不符",
                r.ifd_tiles.len(),
                r.level,
                r.cell
            )));
        }
    }
    if plan.pixel_policy == PixelPolicy::StrictLossless {
        strict_lossless_precheck(src, &doc, &levels)?;
    }
    let source_format = if doc.header.version != 1 { "kfb_kfbio_jpeg" } else { "kfb_bf_v1" };
    let description =
        description_bytes(source_format, &doc.header.scanner_id, doc.header.mpp_x,
            doc.header.mpp_y, doc.header.objective);

    let mut writer = match resume {
        None => BigTiffPyramidWriter::new(sink)?,
        Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
    };
    let mut level_stats: Vec<LevelStats> = Vec::new();
    let mut edge_regions: Vec<EdgeRegion> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
    let mut ifd_chain: Vec<(u32, Option<usize>)> = Vec::new();

    for (li, lv) in levels.iter().enumerate() {
        job.check()?;
        let resume_done = resume.is_some_and(|r| li < r.level);
        let resume_current = resume.is_some_and(|r| li == r.level);

        // ---- level sampling (full consistency unless resuming past this level)
        let sampling = level_sampling(src, &doc, lv, !resume_done)?;
        let (enc_sampling, tiff_sub) = supported_sampling(sampling).ok_or_else(|| {
            CoreError::validation(format!(
                "层 {} JPEG 采样 {sampling:?} 不在支持集（4:4:4/4:2:2/4:2:0）",
                lv.level
            ))
        })?;

        // ---- pass 1: any edge tiles? (progress + policy already done)
        let mut stats = level_stats_from_kfb(lv);
        if resume_done {
            // Fully committed level: rebuild IFD state + report side effects.
            let committed = resume.unwrap().ifd_tiles[li];
            let total = lv.tiles_across() as u64 * lv.tiles_down() as u64;
            if committed != total {
                return Err(CoreError::validation(format!(
                    "resume: 层 {} 已提交 {} ≠ 总 tile 数 {}（journal 与输入不符）",
                    lv.level, committed, total
                )));
            }
            writer.begin_level_resume(scratch, committed)?;
            reconstruct_cells_bf(src, &doc, lv, u64::MAX, &mut stats, &mut edge_regions,
                &mut warnings)?;
            writer.end_level(
                lv.width,
                lv.height,
                tiff_sub,
                doc.header.mpp_x,
                doc.header.mpp_y,
                &description,
                li > 0,
            )?;
            ifd_chain.push((lv.level, None));
            level_stats.push(stats);
            continue;
        }

        let skip_until = if resume_current { resume.unwrap().cell } else { 0 };
        if resume_current && skip_until > 0 {
            let committed = resume.unwrap().ifd_tiles[li];
            writer.begin_level_resume(scratch, committed)?;
            reconstruct_cells_bf(src, &doc, lv, skip_until, &mut stats, &mut edge_regions,
                &mut warnings)?;
        } else {
            writer.begin_level(scratch)?;
        }
        let mut row_done: u64 = skip_until / lv.tiles_across() as u64;
        doc.grids.for_each_cell(lv.level, |cell, rec| {
            job.check()?;
            if resume_current && (cell as u64) < skip_until {
                return Ok(()); // already committed; side effects reconstructed
            }
            let row = cell as u64 / lv.tiles_across() as u64;
            let col = cell as u64 % lv.tiles_across() as u64;
            let want_w = TILE.min(lv.width - (col as u32 * TILE));
            let want_h = TILE.min(lv.height - (row as u32 * TILE));
            let rec = match rec {
                Some(r) => r,
                None => {
                    return Err(CoreError::validation(format!(
                        "层 {} 网格覆盖不全：缺 ({row},{col})（明场不允许稀疏）",
                        lv.level
                    )));
                }
            };
            if rec.jpeg_w as u32 > want_w || rec.jpeg_h as u32 > want_h {
                return Err(CoreError::validation(format!(
                    "层 {} tile({row},{col}) 尺寸 {}×{} 超出网格 {want_w}×{want_h}",
                    lv.level, rec.jpeg_w, rec.jpeg_h
                )));
            }
            let payload = src.read_at(rec.payload_offset, rec.payload_length as usize)?;
            let data: Vec<u8>;
            if rec.is_full_tile() {
                data = payload;
                stats.tiles_raw_copied += 1;
            } else {
                let (encoded, region) = reencode_edge_tile(
                    &payload,
                    rec.jpeg_w,
                    rec.jpeg_h,
                    enc_sampling,
                )?;
                if !region.reused_qtables {
                    warnings.push(WARN_EDGE_REENCODE_FALLBACK_Q95.to_string());
                }
                edge_regions.push(EdgeRegion {
                    level: lv.level,
                    channel: None,
                    x: rec.x,
                    y: rec.y,
                    source_w: rec.jpeg_w as u32,
                    source_h: rec.jpeg_h as u32,
                    canvas_w: TILE,
                    canvas_h: TILE,
                    reused_qtables: region.reused_qtables,
                });
                data = encoded;
                stats.tiles_reencoded += 1;
            }
            writer.write_tile(&data)?;
            if row + 1 > row_done {
                row_done = row + 1;
                job.progress.on_progress(&Progress {
                    unit: ProgressUnit::TileRow,
                    level: lv.level,
                    channel: None,
                    done: row_done,
                    total: lv.tiles_down() as u64,
                    committed_bytes: writer.cursor(),
                });
                if job.checkpoint_enabled() {
                    job.emit_checkpoint(
                        lv.level,
                        None,
                        cell as u64 + 1,
                        writer.cursor(),
                        writer.ifd_tile_counts(),
                    );
                }
            }
            Ok(())
        })?;
        if writer.cursor() > plan.limits.max_output_bytes {
            return Err(CoreError::too_large(format!(
                "输出已写 {} > {}",
                writer.cursor(), plan.limits.max_output_bytes
            )));
        }
        writer.end_level(
            lv.width,
            lv.height,
            tiff_sub,
            doc.header.mpp_x,
            doc.header.mpp_y,
            &description,
            li > 0,
        )?;
        ifd_chain.push((lv.level, None));
        job.progress.on_progress(&Progress {
            unit: ProgressUnit::Level,
            level: lv.level,
            channel: None,
            done: li as u64 + 1,
            total: levels.len() as u64,
            committed_bytes: writer.cursor(),
        });
        level_stats.push(stats);
    }
    let output_bytes = writer.finish()?;

    let mut result = TransformResult {
        plan_version: plan.plan_version,
        core_version: plan.core_version.clone(),
        format: "classic-bigtiff-jpeg-pyramid",
        output_bytes,
        output_sha256: None,
        width: doc.header.width_px,
        height: doc.header.height_px,
        levels: level_stats,
        edge_regions,
        warnings,
        channels: Vec::new(),
        validation: crate::report::ValidationReport {
            ifd_count: ifd_chain.len() as u32,
            tile_records_emitted: 0,
            output_bytes,
            checks_passed: vec![
                "bigtiff-header".into(),
                "ifd-chain".into(),
            ],
        },
        ifd_chain,
        associated: doc
            .associated
            .iter()
            .map(|a| crate::report::AssociatedSummary {
                name: a.name.clone(),
                source_offset: a.payload_offset,
                source_length: a.payload_length,
                width: a.width,
                height: a.height,
            })
            .collect(),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    result.validation.tile_records_emitted =
        result.levels.iter().map(|l| l.tiles_total).sum();
    Ok(result)
}

struct ReencodeOutcome {
    reused_qtables: bool,
}

/// Decode → white 256×256 canvas → re-encode (converter.py::_reencode_edge_tile).
fn reencode_edge_tile(
    payload: &[u8],
    jpeg_w: u16,
    jpeg_h: u16,
    sampling: crate::jpeg::Sampling,
) -> CoreResult<(Vec<u8>, ReencodeOutcome)> {
    let img = crate::jpeg::decode(payload, (TILE as u64) * (TILE as u64) * 4)?;
    if img.width != jpeg_w as u32 || img.height != jpeg_h as u32 {
        return Err(CoreError::jpeg(format!(
            "tile 解码尺寸 {}×{} 与索引 {jpeg_w}×{jpeg_h} 不符",
            img.width, img.height
        )));
    }
    // canvas RGB 255 with the decoded pixels pasted at (0,0) — row-strided
    // paste (a w×h image onto the 256-wide canvas, NOT a packed copy)
    let mut canvas = vec![255u8; (TILE as usize) * (TILE as usize) * 3];
    if img.kind == crate::jpeg::ColorKind::Gray {
        for y in 0..img.height as usize {
            let src = &img.data[y * img.width as usize..(y + 1) * img.width as usize];
            let dst = y * TILE as usize * 3;
            for (k, px) in src.iter().enumerate() {
                canvas[dst + k * 3] = *px;
                canvas[dst + k * 3 + 1] = *px;
                canvas[dst + k * 3 + 2] = *px;
            }
        }
    } else {
        for y in 0..img.height as usize {
            let src = &img.data[y * img.width as usize * 3..(y + 1) * img.width as usize * 3];
            let dst = y * TILE as usize * 3;
            canvas[dst..dst + src.len()].copy_from_slice(src);
        }
    }
    // Pillow im.quantization: dict of tables in id order; reuse needs ≥2
    let qtables = crate::jpeg::qtables_pillow_style(payload).unwrap_or_default();
    let (cfg, reused) = if qtables.len() >= 2 {
        (
            crate::jpeg::EncoderCfg {
                y_q: qtables[0],
                c_q: qtables[1],
                sampling,
            },
            true,
        )
    } else {
        (
            crate::jpeg::EncoderCfg::with_quality(95, sampling),
            false,
        )
    };
    let out = crate::jpeg::encode_rgb(&canvas, TILE, TILE, &cfg)?;
    Ok((out, ReencodeOutcome { reused_qtables: reused }))
}

/// Convenience wrapper without progress (tests/small uses).
pub fn convert_kfb(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_kfb_to_bigtiff(src, sink, scratch, plan, &job)
}

/// Probe-only helper shared with the CLI: format identity + geometry.
pub fn probe_kfb(
    src: &dyn ByteSource,
    scratch: &mut dyn ScratchFactory,
) -> CoreResult<KfbDocument> {
    parse_kfb(src, scratch)
}
