//! Lightweight JPEG header inspection, a byte-for-byte port of
//! `kfb/parser.py::scan_jpeg` plus a DQT reader needed to reuse the source
//! quantization tables for edge-tile re-encoding (the Python oracle gets
//! those from Pillow's `im.quantization`; no Rust decoder crate exposes
//! them, so we parse the DQT markers directly — reason recorded in the ADR).

use crate::error::{CoreError, CoreResult};

/// (h1, v1, h2, v2, h3, v3) sampling factors of a 3-component JPEG.
pub type Sampling6 = (u8, u8, u8, u8, u8, u8);

fn is_sof(marker: u8) -> bool {
    (0xC0..=0xCF).contains(&marker) && marker != 0xC4 && marker != 0xC8 && marker != 0xCC
}

#[derive(Debug, Clone)]
pub struct JpegProbe {
    pub width: u32,
    pub height: u32,
    /// `None` for grayscale (1-component) JPEG.
    pub sampling: Option<Sampling6>,
    /// Per-component (id, h, v, tq) from the SOF; used to map DQT ids.
    pub components: Vec<(u8, u8, u8, u8)>,
}

/// Port of `kfb/parser.py::scan_jpeg` (same structure, same error class).
pub fn scan_jpeg(data: &[u8]) -> CoreResult<JpegProbe> {
    let n = data.len();
    if n < 4 || data[0..2] != [0xFF, 0xD8] {
        return Err(CoreError::jpeg("payload 不是 JPEG（缺 SOI）"));
    }
    let mut width = 0u32;
    let mut height = 0u32;
    let mut sampling: Option<Sampling6> = None;
    let mut components = Vec::new();
    let mut i = 2usize;
    while i + 4 <= n {
        if data[i] != 0xFF {
            return Err(CoreError::jpeg("JPEG 标记流错位"));
        }
        // 跳过填充 FF
        while i < n && data[i] == 0xFF {
            i += 1;
        }
        if i >= n {
            break;
        }
        let marker = data[i];
        i += 1;
        if marker == 0xD9 {
            // EOI：头扫描止于 EOI（SOF 必在其前）
            break;
        }
        if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            continue; // 无长度段
        }
        if i + 2 > n {
            return Err(CoreError::jpeg("JPEG 标记段截断"));
        }
        let seg_len = ((data[i] as usize) << 8) | data[i + 1] as usize;
        if seg_len < 2 || i + seg_len > n {
            return Err(CoreError::jpeg("JPEG 段长度非法"));
        }
        if is_sof(marker) {
            let seg = &data[i + 2..i + seg_len];
            if seg.len() < 6 {
                return Err(CoreError::jpeg("SOF 段残缺"));
            }
            height = ((seg[1] as u32) << 8) | seg[2] as u32;
            width = ((seg[3] as u32) << 8) | seg[4] as u32;
            let ncomp = seg[5];
            if (ncomp != 1 && ncomp != 3)
                || seg.len() < 6 + 3 * ncomp as usize
            {
                return Err(CoreError::jpeg("SOF 分量数非法"));
            }
            for c in 0..ncomp as usize {
                let id = seg[6 + 3 * c];
                let hv = seg[6 + 3 * c + 1];
                let tq = seg[6 + 3 * c + 2];
                components.push((id, hv >> 4, hv & 0x0F, tq));
            }
            if ncomp == 3 {
                sampling = Some((
                    components[0].1, components[0].2,
                    components[1].1, components[1].2,
                    components[2].1, components[2].2,
                ));
            }
            break; // 只取首个 SOF
        }
        i += seg_len;
    }
    if width == 0 || height == 0 {
        return Err(CoreError::jpeg("JPEG 缺少 SOF"));
    }
    Ok(JpegProbe { width, height, sampling, components })
}

/// JPEG zigzag index map (natural-order index for each zigzag position).
pub const ZIGZAG: [usize; 64] = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27,
    20, 13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58,
    59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
];

/// Quantization tables parsed from DQT markers, keyed by table id.
/// Values are in **natural order** (already de-zigzagged), as expected by
/// `jpeg_encoder::QuantizationTableType::Custom`.
#[derive(Debug, Clone, Default)]
pub struct DqtSet {
    pub tables: [Option<[u16; 64]>; 4],
}

impl DqtSet {
    pub fn get(&self, id: u8) -> Option<&[u16; 64]> {
        self.tables.get(id as usize).and_then(|t| t.as_ref())
    }
}

/// Walk markers up to SOS and collect DQT tables (8-bit precision only;
/// 16-bit tables are treated as absent → edge re-encode falls back to q95,
/// matching the oracle's fallback warning path).
pub fn parse_dqt(data: &[u8]) -> DqtSet {
    let mut out = DqtSet::default();
    let n = data.len();
    if n < 4 || data[0..2] != [0xFF, 0xD8] {
        return out;
    }
    let mut i = 2usize;
    while i + 4 <= n {
        while i < n && data[i] == 0xFF {
            i += 1;
        }
        if i >= n {
            break;
        }
        let marker = data[i];
        i += 1;
        if marker == 0xD9 || marker == 0xDA {
            break; // EOI 或 SOS：量化表必在其前
        }
        if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            continue;
        }
        if i + 2 > n {
            break;
        }
        let seg_len = ((data[i] as usize) << 8) | data[i + 1] as usize;
        if seg_len < 2 || i + seg_len > n {
            break;
        }
        if marker == 0xDB {
            // 一个 DQT 段可含多张表
            let mut p = i + 2;
            let seg_end = i + seg_len;
            while p < seg_end {
                let pq_tq = data[p];
                let precision = pq_tq >> 4;
                let id = pq_tq & 0x0F;
                p += 1;
                if precision == 0 && p + 64 <= seg_end {
                    let mut natural = [0u16; 64];
                    for (zig_pos, &v) in data[p..p + 64].iter().enumerate() {
                        natural[ZIGZAG[zig_pos]] = v as u16;
                    }
                    out.tables[id as usize] = Some(natural);
                    p += 64;
                } else {
                    break; // 16-bit 表或不完整：放弃（fallback 路径）
                }
            }
        }
        i += seg_len;
    }
    out
}
