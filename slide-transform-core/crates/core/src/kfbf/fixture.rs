//! Synthetic kfb_fl_v1 (fluorescence KFBF) fixture generator — Rust mirror
//! of `kfb/fixture_fl.py`'s disk contract. Two modes:
//!  * small fixtures for unit/malformed tests (smooth deterministic pattern)
//!  * streaming >4 GiB inputs for the 64-bit offset proof (`noisy` pattern
//!    with per-unique-size payload caching, so generation stays IO-bound)
//!
//! Deterministic content only — no patient data. Layout: header+tagged+
//! metadata+associated, then ALL tile payloads (u64 pointer blocks may
//! address beyond 4 GiB), then pointer blocks/side records/index (their
//! record fields are u32, so they must sit below 4 GiB — they do, being a
//! small fraction of total size), then the EOF thumbnail record.

use super::{KFBF_MAGIC, TILE_H, TILE_W, level_dimensions};
use crate::error::{CoreError, CoreResult};
use crate::io::RandomAccessSink;

pub const DEFAULT_WIDTH: u32 = 600;
pub const DEFAULT_HEIGHT: u32 = 400;
pub const DEFAULT_MPP: f32 = 0.2506266;
pub const DEFAULT_OBJECTIVE: u32 = 40;
pub const DEFAULT_SCANNER_ID: &[u8] = b"KFSYNTH0001";

/// (name, rgb, exposure, gamma)
pub const DEFAULT_CHANNELS: &[(&str, (u32, u32, u32), f64, f64)] = &[
    ("DAPI", (0, 0, 229), 6.0, 1.0),
    ("520", (0, 255, 0), 2.0, 1.0),
];

#[derive(Debug, Clone)]
pub struct KfbfGenParams {
    pub width: u32,
    pub height: u32,
    pub mpp: f32,
    pub objective: u32,
    pub scanner_id: Vec<u8>,
    pub channels: Vec<(String, (u32, u32, u32), f64, f64)>,
    /// level-0 cells intentionally omitted ((row, col)) → black-fill path.
    pub missing_cells: Vec<(u32, u32)>,
    /// Trim level-0 bottom-row tile JPEG height by this many px.
    pub trim_level0_bottom: u32,
    pub scanned_at: u32,
    pub quality: u8,
    /// Noisy high-entropy pattern (large payloads for the >4 GiB proof).
    pub noisy: bool,
    /// Reuse one payload per (level, channel, w, h) — keeps huge-input
    /// generation IO-bound; payload uniqueness is not part of the format.
    pub cache_payloads: bool,
}

impl Default for KfbfGenParams {
    fn default() -> Self {
        KfbfGenParams {
            width: DEFAULT_WIDTH,
            height: DEFAULT_HEIGHT,
            mpp: DEFAULT_MPP,
            objective: DEFAULT_OBJECTIVE,
            scanner_id: DEFAULT_SCANNER_ID.to_vec(),
            channels: DEFAULT_CHANNELS
                .iter()
                .map(|(n, c, e, g)| (n.to_string(), *c, *e, *g))
                .collect(),
            missing_cells: vec![(0, 1)],
            trim_level0_bottom: 44,
            scanned_at: 1_789_135_116,
            quality: 90,
            noisy: false,
            cache_payloads: false,
        }
    }
}

fn channel_pattern(
    w: u32,
    h: u32,
    channel: usize,
    level: u32,
    row: u32,
    col: u32,
    noisy: bool,
) -> Vec<u8> {
    if noisy {
        let mut state = (channel as u64)
            .wrapping_mul(0x9E37_79B9_7F4A_7C15)
            ^ (level as u64).wrapping_mul(0xBF58_476D_1CE4_E5B9)
            ^ (row as u64).wrapping_mul(0x94D0_49BB_1331_11EB)
            ^ (col as u64).wrapping_mul(0x2545_F491_4F6C_DD1D)
            | 1;
        let mut out = vec![0u8; (w * h) as usize];
        for v in out.iter_mut() {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            *v = (state >> 33) as u8;
        }
        out
    } else {
        (0..w * h)
            .map(|i| {
                let x = i % w;
                let y = i / w;
                (x * 3 + y * 5 + channel as u32 * 40 + level * 17 + row * 7 + col * 11)
                    as u8
            })
            .collect()
    }
}

fn put32(b: &mut [u8], at: usize, v: u32) {
    b[at..at + 4].copy_from_slice(&v.to_le_bytes());
}
fn put64(b: &mut [u8], at: usize, v: u64) {
    b[at..at + 8].copy_from_slice(&v.to_le_bytes());
}

fn write_at(out: &mut dyn RandomAccessSink, cur: &mut u64, bytes: &[u8]) -> CoreResult<()> {
    out.write_at(*cur, bytes)?;
    *cur += bytes.len() as u64;
    Ok(())
}

/// Streaming synthetic KFBF builder.
pub fn build_synthetic_kfbf(
    out: &mut dyn RandomAccessSink,
    p: &KfbfGenParams,
) -> CoreResult<u64> {
    let nch = p.channels.len();
    if nch == 0 || nch > 16 {
        return Err(CoreError::header("channel count"));
    }

    // ---- head（tagged + 通道元数据 + associated 占位） ------------------- //
    let mut head: Vec<u8> = vec![0u8; 0x5C];
    head[0..8].copy_from_slice(&KFBF_MAGIC);
    put32(&mut head, 0x08, 0);
    head[0x0C..0x10].copy_from_slice(&2.1f32.to_le_bytes());
    head[0x20..0x24].copy_from_slice(b"JPEG");
    head[0x4C..0x50].copy_from_slice(&p.mpp.to_le_bytes());
    put32(&mut head, 0x58, TILE_W);

    let names_blob: Vec<u8> = p
        .channels
        .iter()
        .flat_map(|(n, _, _, _)| {
            let mut b = n.as_bytes()[..n.len().min(39)].to_vec();
            b.resize(40, 0);
            b
        })
        .collect();
    let colors_blob: Vec<u8> = p
        .channels
        .iter()
        .flat_map(|(_, (r, g, b), _, _)| {
            let mut v = Vec::with_capacity(12);
            v.extend_from_slice(&r.to_le_bytes());
            v.extend_from_slice(&g.to_le_bytes());
            v.extend_from_slice(&b.to_le_bytes());
            v
        })
        .collect();
    let expo_blob: Vec<u8> =
        p.channels.iter().flat_map(|(_, _, e, _)| e.to_le_bytes()).collect();
    let gamma_blob: Vec<u8> =
        p.channels.iter().flat_map(|(_, _, _, g)| g.to_le_bytes()).collect();
    let mut nch_b = [0u8; 4];
    nch_b.copy_from_slice(&(nch as u32).to_le_bytes());
    let tag_specs: Vec<(u32, Vec<u8>)> = vec![
        (29, p.scanner_id.clone()),
        (75, nch_b.to_vec()),
        (77, Vec::new()),
        (79, Vec::new()),
        (84, Vec::new()),
        (87, Vec::new()),
    ];
    let tagged_len = 8
        + tag_specs
            .iter()
            .map(|(_, v)| 8 + if v.is_empty() { 8 } else { v.len() })
            .sum::<usize>();
    let meta_base = 0x5C + tagged_len;
    let mut cursor_meta = meta_base as u64;
    let mut ptrs = std::collections::HashMap::new();
    let mut meta_blocks: Vec<(u32, Vec<u8>)> = Vec::new();
    for (tag, blk) in [
        (77, names_blob),
        (79, colors_blob),
        (84, expo_blob),
        (87, gamma_blob),
    ] {
        ptrs.insert(tag, cursor_meta);
        cursor_meta += blk.len() as u64;
        meta_blocks.push((tag, blk));
    }
    let mut tagged: Vec<u8> = Vec::with_capacity(tagged_len);
    tagged.extend_from_slice(&[0xFF, 0x01, 0xEE, 0xEE]);
    tagged.extend_from_slice(&(tag_specs.len() as u32).to_le_bytes());
    for (tag, val) in &tag_specs {
        tagged.extend_from_slice(&tag.to_le_bytes());
        if !val.is_empty() {
            tagged.extend_from_slice(&(val.len() as u32).to_le_bytes());
            tagged.extend_from_slice(val);
        } else {
            tagged.extend_from_slice(&8u32.to_le_bytes());
            tagged.extend_from_slice(&ptrs[tag].to_le_bytes());
        }
    }
    debug_assert_eq!(tagged.len(), tagged_len);
    head.extend_from_slice(&tagged);
    for (_, b) in meta_blocks {
        head.extend_from_slice(&b);
    }

    let qtable = crate::jpeg::tables::std_luma_quality(p.quality);
    let overview = crate::jpeg::encode_rgb(
        &(0..96 * 64 * 3).map(|i| ((i * 7) % 256) as u8).collect::<Vec<_>>(),
        96,
        64,
        &crate::jpeg::EncoderCfg::with_quality(p.quality, crate::jpeg::Sampling::S420),
    )?;
    let label = crate::jpeg::encode_rgb(
        &vec![250u8; 64 * 64 * 3],
        64,
        64,
        &crate::jpeg::EncoderCfg::with_quality(p.quality, crate::jpeg::Sampling::S420),
    )?;
    let thumb = crate::jpeg::encode_gray(
        &channel_pattern(64, 48, 0, 0, 0, 0, false),
        64,
        48,
        &qtable,
    )?;
    let overview_rec_off = head.len() as u64;
    let mut rec = Vec::with_capacity(52);
    rec.extend_from_slice(&[0xF1, 0x02, 0xEE, 0xEE]);
    rec.extend_from_slice(&1u32.to_le_bytes());
    rec.extend_from_slice(&64u32.to_le_bytes());
    rec.extend_from_slice(&96u32.to_le_bytes());
    rec.extend_from_slice(&3u32.to_le_bytes());
    rec.extend_from_slice(&(overview.len() as u32).to_le_bytes());
    rec.extend_from_slice(&[0u8; 16]);
    rec.extend_from_slice(&[0xFF, 0x02, 0xEE, 0xEE]);
    head.extend_from_slice(&rec);
    head.extend_from_slice(&overview);
    let label_rec_off = head.len() as u64;
    let mut rec = Vec::with_capacity(52);
    rec.extend_from_slice(&[0xF1, 0x03, 0xEE, 0xEE]);
    rec.extend_from_slice(&1u32.to_le_bytes());
    rec.extend_from_slice(&64u32.to_le_bytes());
    rec.extend_from_slice(&64u32.to_le_bytes());
    rec.extend_from_slice(&3u32.to_le_bytes());
    rec.extend_from_slice(&(label.len() as u32).to_le_bytes());
    rec.extend_from_slice(&[0u8; 16]);
    rec.extend_from_slice(&[0xFF, 0x03, 0xEE, 0xEE]);
    head.extend_from_slice(&rec);
    head.extend_from_slice(&label);
    let thumb_payload_off = head.len() as u64;
    head.extend_from_slice(&thumb);
    let thumb_ptr_cell_off = head.len() as u64;
    head.extend_from_slice(&thumb_payload_off.to_le_bytes());

    // ---- 金字塔几何 ----------------------------------------------------- //
    let mut level_dims = Vec::new();
    for lvl in 0..24u32 {
        let (lw, lh) = level_dimensions(p.width, p.height, lvl);
        level_dims.push((lw, lh));
        if lw <= TILE_W && lh <= TILE_W {
            break;
        }
    }

    // ---- 布局规划：指针块/索引在前（u32 偏移），payloads 在后（可 >4GiB）-- //
    struct TileMeta {
        lvl: u32,
        x: u32,
        y: u32,
        jw: u32,
        jh: u32,
    }
    let mut metas: Vec<TileMeta> = Vec::new();
    let mut cache: std::collections::HashMap<(u32, usize, u32, u32), Vec<u8>> =
        std::collections::HashMap::new();
    let mut total_tiles: u32 = 0;
    let mut payload_bytes: u64 = 0;
    for (lvl, (lw, lh)) in level_dims.iter().enumerate() {
        let lvl = lvl as u32;
        let ta = (lw + TILE_W - 1) / TILE_W;
        let td = (lh + TILE_H - 1) / TILE_H;
        for row in 0..td {
            for col in 0..ta {
                if lvl == 0 && p.missing_cells.contains(&(row, col)) {
                    continue;
                }
                let jw = TILE_W.min(lw - col * TILE_W);
                let mut jh = TILE_H.min(lh - row * TILE_W);
                if lvl == 0 && p.trim_level0_bottom > 0 && row == td - 1 {
                    jh = jh.saturating_sub(p.trim_level0_bottom).max(1);
                }
                for c in 0..nch {
                    let e = cache.entry((lvl, c, jw, jh));
                    let payload = e.or_insert_with(|| {
                        crate::jpeg::encode_gray(
                            &channel_pattern(jw, jh, c, lvl, row, col, p.noisy),
                            jw,
                            jh,
                            &qtable,
                        )
                        .unwrap_or_default()
                    });
                    if payload.is_empty() {
                        return Err(CoreError::jpeg("合成通道编码失败"));
                    }
                    payload_bytes += payload.len() as u64;
                }
                metas.push(TileMeta { lvl, x: col * TILE_W, y: row * TILE_W, jw, jh });
                total_tiles += 1;
            }
        }
    }

    // 指针块 + side 记录 + 索引 + EOF 记录都位于 payload 区之前
    let ptr_side_bytes = (metas.len() as u64) * (96 + 48);
    let index_bytes = (metas.len() as u64) * 64;
    let eof_rec_bytes = 52u64;
    let payload_region = (head.len() as u64) + ptr_side_bytes + index_bytes + eof_rec_bytes;

    // 每 tile 每通道的 payload 偏移
    let mut offsets: Vec<Vec<u64>> = Vec::with_capacity(metas.len());
    let mut lengths: Vec<Vec<u64>> = Vec::with_capacity(metas.len());
    {
        let mut cur = payload_region;
        let mut side_cursor = head.len() as u64;
        let mut tile_off: Vec<Vec<u64>> = Vec::with_capacity(metas.len());
        let mut tile_len: Vec<Vec<u64>> = Vec::with_capacity(metas.len());
        for m in &metas {
            let mut offs = Vec::with_capacity(nch);
            let mut lens = Vec::with_capacity(nch);
            for c in 0..nch {
                let len = cache[&(m.lvl, c, m.jw, m.jh)].len() as u64;
                offs.push(cur);
                lens.push(len);
                cur += len;
            }
            tile_off.push(offs);
            tile_len.push(lens);
            let _ = side_cursor;
        }
        offsets = tile_off;
        lengths = tile_len;
    }

    let mut cursor = head.len() as u64;
    // 指针块 + side 记录
    let mut ptr_side: Vec<u8> = Vec::with_capacity((96 + 48) * 64);
    let mut ptr_offsets: Vec<u64> = Vec::with_capacity(metas.len());
    let mut side_offsets: Vec<u64> = Vec::with_capacity(metas.len());
    let mut layout_cursor = head.len() as u64;
    for (i, m) in metas.iter().enumerate() {
        let _ = m;
        ptr_offsets.push(layout_cursor);
        let mut ptrs12 = offsets[i].clone();
        ptrs12.resize(12, 0);
        for o in ptrs12 {
            ptr_side.extend_from_slice(&o.to_le_bytes());
        }
        side_offsets.push(layout_cursor + 96);
        let mut lens = lengths[i].clone();
        lens.resize(6, 0);
        for l in lens {
            ptr_side.extend_from_slice(&l.to_le_bytes());
        }
        layout_cursor += 96 + 48;
    }
    debug_assert_eq!(layout_cursor, head.len() as u64 + ptr_side.len() as u64);
    write_at(out, &mut cursor, &ptr_side)?;

    // 索引
    let mut index: Vec<u8> = Vec::with_capacity(metas.len() * 64);
    for (i, m) in metas.iter().enumerate() {
        let scale = p.objective as f32 / 2f32.powi(m.lvl as i32);
        let mut e = Vec::with_capacity(64);
        e.extend_from_slice(&[0xF1, 0x04, 0xEE, 0xEE]);
        e.extend_from_slice(&m.x.to_le_bytes());
        e.extend_from_slice(&m.y.to_le_bytes());
        e.extend_from_slice(&m.jw.to_le_bytes());
        e.extend_from_slice(&m.jh.to_le_bytes());
        e.extend_from_slice(&scale.to_le_bytes());
        e.extend_from_slice(&[0u8; 8]);
        e.extend_from_slice(&(lengths[i][0] as u32).to_le_bytes());
        e.extend_from_slice(&(ptr_offsets[i] as u32).to_le_bytes());
        e.extend_from_slice(&0u32.to_le_bytes());
        e.extend_from_slice(&(side_offsets[i] as u32).to_le_bytes());
        e.extend_from_slice(&[0u8; 12]);
        e.extend_from_slice(&[0xFF, 0x04, 0xEE, 0xEE]);
        debug_assert_eq!(e.len(), 64);
        index.extend_from_slice(&e);
    }
    let index_offset = cursor;
    write_at(out, &mut cursor, &index)?;

    // EOF thumbnail 记录
    let mut thumb_rec = Vec::with_capacity(52);
    thumb_rec.extend_from_slice(&[0xF1, 0x02, 0xEE, 0xEE]);
    thumb_rec.extend_from_slice(&1u32.to_le_bytes());
    thumb_rec.extend_from_slice(&48u32.to_le_bytes());
    thumb_rec.extend_from_slice(&64u32.to_le_bytes());
    thumb_rec.extend_from_slice(&1u32.to_le_bytes());
    thumb_rec.extend_from_slice(&(thumb.len() as u32).to_le_bytes());
    thumb_rec.extend_from_slice(&thumb_ptr_cell_off.to_le_bytes());
    thumb_rec.extend_from_slice(&0u64.to_le_bytes());
    thumb_rec.extend_from_slice(&[0u8; 8]);
    thumb_rec.extend_from_slice(&[0xFF, 0x02, 0xEE, 0xEE]);
    debug_assert_eq!(thumb_rec.len(), 52);
    write_at(out, &mut cursor, &thumb_rec)?;
    debug_assert_eq!(cursor, payload_region);

    // payloads（可越过 4GiB；96B 指针块里的 u64 偏移承载）
    let mut wrote: std::collections::HashSet<(u32, usize, u32, u32)> =
        std::collections::HashSet::new();
    for (i, m) in metas.iter().enumerate() {
        for c in 0..nch {
            let key = (m.lvl, c, m.jw, m.jh);
            if wrote.contains(&key) && p.cache_payloads {
                // 复用同一压缩段：直接按缓存字节再写一份
                let data = &cache[&key];
                write_at(out, &mut cursor, data)?;
                continue;
            }
            let data = cache.get(&key).cloned().unwrap_or_default();
            write_at(out, &mut cursor, &data)?;
            wrote.insert(key);
        }
    }
    let _ = payload_bytes;

    // ---- 回填 header ---------------------------------------------------- //
    put32(&mut head, 0x10, total_tiles);
    put32(&mut head, 0x14, p.height);
    put32(&mut head, 0x18, p.width);
    put32(&mut head, 0x1C, p.objective);
    put32(&mut head, 0x2C, p.scanned_at);
    put32(&mut head, 0x34, overview_rec_off as u32);
    put32(&mut head, 0x38, label_rec_off as u32);
    put64(&mut head, 0x44, index_offset);
    out.write_at(0, &head)?;
    out.flush()?;
    Ok(cursor)
}
