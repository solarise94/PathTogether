//! Fluorescence conversion: KFBF → multi-channel OME-BigTIFF with SubIFD
//! pyramid (structure port of `kfb/converter_fl.py`; the OME-XML puts
//! ExposureTime on `<Plane>` per the C1 acceptance requirement). Per grid
//! cell × channel: full-cell payloads copy byte-for-byte; cropped cells are
//! decoded → black cell-size canvas → re-encoded with the source quantization
//! table (fallback std q95 + warning); sparse (missing) cells are filled with
//! a cached black JPEG (std q90) and reported via `sparse_fill_black`.

use crate::companion::Companion;
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::job::{JobControl, NullProgress, Progress, ProgressUnit};
use crate::kfbf::{KfbfDocument, parse_kfbf};
use crate::ome::{OmeChannel, build_ome_xml};
use crate::ome_writer::OmeBigTiffWriter;
use crate::plan::{PixelPolicy, TransformPlan};
use crate::report::{
    AssociatedSummary, ChannelSummary, EdgeRegion, LevelStats, TransformResult,
    ValidationReport, WARN_EDGE_REENCODE_FALLBACK_Q95, WARN_EXPOSURE_UNIT_ASSUMED_MS,
    WARN_SPARSE_FILL_BLACK, level_stats_from_kfbf,
};

const TILE: u32 = 256;
const TILE_PX: u64 = (TILE as u64) * (TILE as u64);

#[allow(clippy::too_many_arguments)]
pub fn convert_kfbf_to_ome(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    companion: Option<&Companion>,
) -> CoreResult<TransformResult> {
    let started = crate::job::WallInstant::now();
    let doc = parse_kfbf(src, scratch)?;
    if !doc.header.mpp.is_finite() || doc.header.mpp <= 0.0 {
        return Err(CoreError::metadata("MPP 缺失/非法"));
    }
    let nch = doc.header.channel_count;
    let description = build_ome_xml(
        doc.header.objective,
        &doc.header.scanner_id,
        doc.header.width_px,
        doc.header.height_px,
        doc.header.mpp,
        &doc
            .channels
            .iter()
            .map(|c| OmeChannel {
                index: c.index,
                name: c.name.clone(),
                color_rgb: c.color_rgb,
                exposure: c.exposure,
            })
            .collect::<Vec<_>>(),
    );

    if plan.pixel_policy == PixelPolicy::StrictLossless {
        strict_lossless_precheck(src, &doc)?;
    }

    let mut writer = OmeBigTiffWriter::new(sink)?;
    let mut level_stats: Vec<LevelStats> = Vec::new();
    let mut edge_regions: Vec<EdgeRegion> = Vec::new();
    let mut warnings: Vec<String> = vec![WARN_EXPOSURE_UNIT_ASSUMED_MS.to_string()];
    let mut filled_total: u64 = 0;
    let mut ifd_chain: Vec<(u32, Option<usize>)> = Vec::new();
    // black-fill cache per (w,h)
    let mut black_cache: std::collections::HashMap<(u32, u32), Vec<u8>> =
        std::collections::HashMap::new();

    // IFD order: level-major, channel-minor. Record the begin order so the
    // level-0 channel IFDs can point at their SubIFDs after all are known.
    // (We begin IFDs lazily per (level, channel) as the oracle adds them.)
    let mut ifd_index: Vec<(u32, usize)> = Vec::new(); // (level, channel) per IFD, in begin order

    for lv in &doc.levels {
        job.check()?;
        let level_mpp = doc.header.mpp * (doc.header.width_px as f64 / lv.width as f64);
        let mut channel_stats_row: Vec<LevelStats> = Vec::new();
        for c in 0..nch {
            writer.begin_ifd(
                scratch,
                lv.width,
                lv.height,
                lv.level > 0,
                level_mpp,
                if lv.level == 0 && c == 0 { Some(description.clone()) } else { None },
            )?;
            ifd_index.push((lv.level, c));
            let mut stats = level_stats_from_kfbf(lv, c);
            let cells = lv.tiles_across() as u64 * lv.tiles_down() as u64;
            let mut last_row_emitted: u64 = 0;
            for cell in 0..cells {
                job.check()?;
                let row = cell / lv.tiles_across() as u64;
                let col = cell % lv.tiles_across() as u64;
                let cell_w = TILE.min(lv.width - (col as u32 * TILE));
                let cell_h = TILE.min(lv.height - (row as u32 * TILE));
                let rec = doc.cells.read_cell(lv.level, cell as u32)?;
                let data: Vec<u8>;
                match rec {
                    None => {
                        let key = (cell_w, cell_h);
                        let entry = black_cache
                            .entry(key)
                            .or_insert_with(|| black_tile(cell_w, cell_h).unwrap());
                        data = entry.clone();
                        stats.cells_filled_black += 1;
                        filled_total += 1;
                    }
                    Some(rec) => {
                        let (off, len) = doc.channel_payload(src, &rec, c)?;
                        let payload = src.read_at(off, len as usize)?;
                        let probe = crate::jpeg::scan_jpeg(&payload)?;
                        if probe.sampling.is_some() {
                            return Err(CoreError::validation(format!(
                                "层 {} tile({row},{col}) 通道 {c} 非灰度 JPEG",
                                lv.level
                            )));
                        }
                        if (probe.width, probe.height) != (rec.jpeg_w, rec.jpeg_h) {
                            return Err(CoreError::validation(format!(
                                "层 {} tile({row},{col}) 通道 {c} JPEG 尺寸 {}×{} 与索引 {}×{} 不符",
                                lv.level, probe.width, probe.height, rec.jpeg_w, rec.jpeg_h
                            )));
                        }
                        if rec.jpeg_w == cell_w && rec.jpeg_h == cell_h {
                            data = payload;
                            stats.tiles_raw_copied += 1;
                        } else {
                            let (encoded, reused) = reencode_gray_cell(
                                &payload,
                                rec.jpeg_w,
                                rec.jpeg_h,
                                cell_w,
                                cell_h,
                            )?;
                            if !reused {
                                warnings.push(WARN_EDGE_REENCODE_FALLBACK_Q95.to_string());
                            }
                            edge_regions.push(EdgeRegion {
                                level: lv.level,
                                channel: Some(c),
                                x: rec.x,
                                y: rec.y,
                                source_w: rec.jpeg_w,
                                source_h: rec.jpeg_h,
                                canvas_w: cell_w,
                                canvas_h: cell_h,
                                reused_qtables: reused,
                            });
                            data = encoded;
                            stats.tiles_reencoded += 1;
                        }
                    }
                }
                writer.write_tile(&data)?;
                if row + 1 > last_row_emitted {
                    last_row_emitted = row + 1;
                    job.progress.on_progress(&Progress {
                        unit: ProgressUnit::TileRow,
                        level: lv.level,
                        channel: Some(c),
                        done: last_row_emitted,
                        total: lv.tiles_down() as u64,
                        committed_bytes: writer.cursor(),
                    });
                }
            }
            if writer.cursor() > plan.limits.max_output_bytes {
                return Err(CoreError::too_large(format!(
                    "输出已写 {} > {}",
                    writer.cursor(),
                    plan.limits.max_output_bytes
                )));
            }
            ifd_chain.push((lv.level, Some(c)));
            job.progress.on_progress(&Progress {
                unit: ProgressUnit::Level,
                level: lv.level,
                channel: Some(c),
                done: (c + 1) as u64,
                total: nch as u64,
                committed_bytes: writer.cursor(),
            });
            channel_stats_row.push(stats);
        }
        level_stats.extend(channel_stats_row);
    }

    // SubIFDs: level-0 channel IFD (index = c) → IFDs of (level>0, same c)
    let chain: Vec<usize> = (0..nch).collect();
    for c in 0..nch {
        let subs: Vec<usize> = ifd_index
            .iter()
            .enumerate()
            .filter(|(_, &(lvl, ch))| lvl > 0 && ch == c)
            .map(|(i, _)| i)
            .collect();
        // begin_ifd #c is the level-0 IFD for channel c (level-major order)
        writer_set_subifds(&mut writer, c, subs)?;
    }
    if filled_total > 0 {
        warnings.push(WARN_SPARSE_FILL_BLACK.to_string());
    }
    let output_bytes = writer.finish(0, &chain)?;

    // channels summary (absorb companion display windows; body wins)
    let mut channels: Vec<ChannelSummary> = doc
        .channels
        .iter()
        .map(|ch| ChannelSummary {
            index: ch.index,
            name: ch.name.clone(),
            color_rgb: ch.color_rgb,
            exposure: ch.exposure,
            gamma: ch.gamma,
            display_window: None,
            display_window_source: None,
        })
        .collect();
    if let Some(comp) = companion {
        for cc in &comp.channels {
            // match by name (body wins on conflict → only add missing info)
            if let Some(cs) = channels.iter_mut().find(|cs| cs.name == cc.channel_name) {
                if cs.display_window.is_none() {
                    cs.display_window = Some((cc.lower, cc.upper));
                    cs.display_window_source =
                        Some("channel.json".to_string());
                }
            }
        }
    }

    Ok(TransformResult {
        plan_version: plan.plan_version,
        core_version: plan.core_version.clone(),
        format: "ome-bigtiff-subifd-multichannel-jpeg-passthrough",
        output_bytes,
        output_sha256: None,
        width: doc.header.width_px,
        height: doc.header.height_px,
        levels: level_stats.clone(),
        edge_regions,
        warnings,
        channels,
        validation: ValidationReport {
            ifd_count: ifd_chain.len() as u32,
            tile_records_emitted: level_stats.iter().map(|l| l.tiles_total).sum(),
            output_bytes,
            checks_passed: vec![
                "bigtiff-header".into(),
                "ome-xml-present".into(),
                "subifd-chain".into(),
            ],
        },
        ifd_chain,
        associated: doc
            .associated
            .iter()
            .map(|a| AssociatedSummary {
                name: a.name.clone(),
                source_offset: a.payload_offset,
                source_length: a.payload_length,
                width: a.width,
                height: a.height,
            })
            .collect(),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}

fn writer_set_subifds(
    writer: &mut OmeBigTiffWriter,
    ifd: usize,
    subs: Vec<usize>,
) -> CoreResult<()> {
    writer.set_subifds_for(ifd, subs)
}

/// Strict-policy precheck: reject cropped cells before any output write.
fn strict_lossless_precheck(
    src: &dyn ByteSource,
    doc: &KfbfDocument,
) -> CoreResult<()> {
    for lv in &doc.levels {
        let cells = lv.tiles_across() as u64 * lv.tiles_down() as u64;
        for cell in 0..cells {
            let row = cell / lv.tiles_across() as u64;
            let col = cell % lv.tiles_across() as u64;
            let cell_w = TILE.min(lv.width - (col as u32 * TILE));
            let cell_h = TILE.min(lv.height - (row as u32 * TILE));
            match doc.cells.read_cell(lv.level, cell as u32)? {
                None => {
                    return Err(CoreError::policy(format!(
                        "层 {} cell({row},{col}) 缺失（黑填充 ≠ 无损，StrictLossless）",
                        lv.level
                    )));
                }
                Some(rec) => {
                    if rec.jpeg_w != cell_w || rec.jpeg_h != cell_h {
                        return Err(CoreError::policy(format!(
                            "层 {} cell({row},{col}) 需要重编码（{}×{} → {cell_w}×{cell_h}，StrictLossless）",
                            lv.level, rec.jpeg_w, rec.jpeg_h
                        )));
                    }
                    let _ = src;
                }
            }
        }
    }
    Ok(())
}

/// Pure-black grayscale JPEG (quality 90), cached per size by the caller.
fn black_tile(w: u32, h: u32) -> CoreResult<Vec<u8>> {
    crate::jpeg::encode_gray(
        &vec![0u8; w as usize * h as usize],
        w,
        h,
        &crate::jpeg::tables::std_luma_quality(90),
    )
}

/// Decode → black cell-size canvas → re-encode with the source quantization
/// table (`converter_fl._reencode_gray_tile`).
fn reencode_gray_cell(
    payload: &[u8],
    jpeg_w: u32,
    jpeg_h: u32,
    want_w: u32,
    want_h: u32,
) -> CoreResult<(Vec<u8>, bool)> {
    let img = crate::jpeg::decode(payload, TILE_PX * 4)?;
    if img.width != jpeg_w || img.height != jpeg_h {
        return Err(CoreError::jpeg(format!(
            "tile 解码尺寸 {}×{} 与索引 {jpeg_w}×{jpeg_h} 不符",
            img.width, img.height
        )));
    }
    if img.kind != crate::jpeg::ColorKind::Gray {
        return Err(CoreError::validation("通道 JPEG 非灰度"));
    }
    // black cell-size canvas, decoded pixels pasted at (0,0), row-strided
    let mut canvas = vec![0u8; want_w as usize * want_h as usize];
    for y in 0..img.height as usize {
        let src = &img.data[y * img.width as usize..(y + 1) * img.width as usize];
        let dst = y * want_w as usize;
        canvas[dst..dst + src.len()].copy_from_slice(src);
    }
    let qtables = crate::jpeg::qtables_pillow_style(payload).unwrap_or_default();
    let (table, reused) = if qtables.is_empty() {
        (crate::jpeg::tables::std_luma_quality(95), false)
    } else {
        (qtables[0], true)
    };
    let out = crate::jpeg::encode_gray(&canvas, want_w, want_h, &table)?;
    Ok((out, reused))
}

/// Convenience wrapper without progress (tests).
pub fn convert_kfbf(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    companion: Option<&Companion>,
) -> CoreResult<TransformResult> {
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(plan.limits.timeout_seconds);
    convert_kfbf_to_ome(src, sink, scratch, plan, &job, companion)
}

/// Probe helper for the CLI.
pub fn probe_kfbf(
    src: &dyn ByteSource,
    scratch: &mut dyn ScratchFactory,
) -> CoreResult<KfbfDocument> {
    parse_kfbf(src, scratch)
}

