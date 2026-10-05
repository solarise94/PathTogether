//! Shared restart-segment strip reader (NDPI whole-layer strips, F6; VMS
//! concatenated tile JPEGs): a baseline JPEG strip WITH restart markers is
//! decoded restart segment by restart segment — each segment is a
//! self-contained MCU run with reset DC predictors, the only bounded decode
//! unit such a strip offers. Moved here verbatim from `convert_ndpi` so the
//! VMS adapter consumes the exact same scanning/decoding rules.
//!
//! Bounded reads + budget charges: the marker scan reads ≤ 64 KiB chunks
//! (charged and released), a segment decode charges read + decoder transient
//! + decoded RGB (6 B/px of the sub-image grid) BEFORE either allocation.

use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;

/// Bounded RST scan chunk (bytes per source read while hunting markers).
pub const SCAN_CHUNK: u64 = 64 * 1024;

/// The geometry a segmented strip reader needs, independent of the
/// container (TIFF strip offset for NDPI, bundle member for VMS).
#[derive(Debug, Clone)]
pub struct StripGeom {
    /// Byte offset of the strip payload inside `src` (0 for a bundle member).
    pub strip_offset: u64,
    /// Byte length of the strip payload.
    pub strip_bytes: u64,
    /// MCU size in pixels (8·h_max, 8·v_max).
    pub mcu_w: u32,
    pub mcu_h: u32,
    /// MCUs per row (⌈width / mcu_w⌉).
    pub mcus_x: u32,
    /// Total MCUs in the strip (mcus_x × mcus_y).
    pub total_mcus: u64,
    /// Restart interval in MCUs (DRI; > 0 enforced by the probes).
    pub restart_interval: u32,
    /// Segments in the strip (= ⌈total_mcus / restart_interval⌉).
    pub segments: u64,
    /// Strip head bytes [SOI … SOS segment] (the sub-JPEG header source).
    pub header_bytes: Vec<u8>,
    /// Offset of the SOF height field inside `header_bytes` (width at +2).
    pub sof_hw_at: usize,
    /// [start, end) of the DRI marker segment inside `header_bytes`.
    pub dri_at: Option<(usize, usize)>,
}

impl StripGeom {
    /// MCU-grid width of one decoded segment (⌈segment MCUs / rows⌉ grid —
    /// full MCU rows wherever that fits in 256 MCUs).
    pub fn seg_grid_w(&self, mcus: u64) -> u64 {
        mcus.min(self.mcus_x.max(1) as u64).min(256)
    }

    /// Padded strip width in pixels (mcus_x × mcu_w).
    pub fn padded_w(&self) -> u64 {
        self.mcus_x as u64 * self.mcu_w as u64
    }

    /// Per-segment decode charge ceiling (read + transient + RGB), for the
    /// caller's pre-allocation working-set estimate.
    pub fn max_segment_px(&self) -> u64 {
        (self.restart_interval as u64)
            .saturating_mul(self.mcu_w as u64)
            .saturating_mul(self.mcu_h as u64)
    }
}

/// One decoded segment: an independent MCU run with reset DC predictors.
pub struct SegmentRead {
    pub img: jpeg::DecodedImage,
    /// Global MCU index of the segment's first MCU.
    pub mcu_start: u64,
    /// MCUs actually coded in this segment (the last one is partial).
    pub mcus: u64,
    /// MCUs per row of the decoded sub-image grid.
    pub grid_w: u64,
    /// Bytes charged against the budget for this decode (read + transient).
    pub charged: u64,
}

/// Forward-only restart-segment reader over one strip of a `ByteSource`.
pub struct SegmentReader<'a> {
    src: &'a dyn ByteSource,
    g: StripGeom,
    /// Byte offset just past the last consumed RST marker.
    scan_at: u64,
    /// Index of the next segment to decode.
    next_k: u64,
}

impl<'a> SegmentReader<'a> {
    pub fn new(src: &'a dyn ByteSource, g: &StripGeom) -> Self {
        // build the DRI-less header once (restart_interval 0 → no RST checks)
        let mut header = g.header_bytes.clone();
        if let Some((s, e)) = g.dri_at {
            header.drain(s..e);
        }
        debug_assert!(header.len() >= 4 && header[0..2] == [0xFF, 0xD8]);
        let mut hg = g.clone();
        hg.header_bytes = header;
        SegmentReader {
            src,
            g: hg,
            scan_at: g.strip_offset + g.header_bytes.len() as u64,
            next_k: 0,
        }
    }

    pub fn next_k(&self) -> u64 {
        self.next_k
    }

    fn sof_hw_at(&self) -> usize {
        match self.g.dri_at {
            Some((s, e)) if self.g.sof_hw_at >= e => self.g.sof_hw_at - (e - s),
            _ => self.g.sof_hw_at,
        }
    }

    fn strip_end(&self) -> u64 {
        self.g.strip_offset + self.g.strip_bytes
    }

    /// Scan [from, limit) for the next restart/EOI marker (0xFF D0–D7/D9).
    /// Bounded chunked reads; each chunk is charged and released.
    pub fn scan_marker(
        &mut self,
        budget: &mut MemBudget,
        from: u64,
        limit: u64,
    ) -> CoreResult<Option<(u64, u8)>> {
        let mut at = from;
        let mut carry: Option<u8> = None;
        let limit = limit.min(self.strip_end());
        while at < limit {
            let want = SCAN_CHUNK.min(limit - at);
            budget.charge(want, "RST 标记扫描读取")?;
            let buf = match self.src.read_at(at, want as usize) {
                Ok(b) => b,
                Err(e) => {
                    budget.release(want);
                    return Err(e);
                }
            };
            let mut i = 0usize;
            if let Some(prev) = carry {
                if buf[0] == prev && (0xD0..=0xD9).contains(&buf[1]) {
                    budget.release(want);
                    return Ok(Some((at - 1, buf[1])));
                }
            }
            while i + 1 < buf.len() {
                if buf[i] == 0xFF && (0xD0..=0xD9).contains(&buf[i + 1]) {
                    budget.release(want);
                    return Ok(Some((at + i as u64, buf[i + 1])));
                }
                i += 1;
            }
            carry = buf.last().copied();
            at += want;
            budget.release(want);
        }
        Ok(None)
    }

    /// Fast-forward the scanner past segment boundaries (resume path): scan
    /// only, no decode.
    pub fn skip_to(&mut self, budget: &mut MemBudget, k: u64) -> CoreResult<()> {
        if k > self.g.segments {
            return Err(CoreError::validation("resume: 分段游标越界"));
        }
        while self.next_k < k {
            let Some((at, m)) = self.scan_marker(budget, self.scan_at, self.strip_end())? else {
                return Err(CoreError::oob(format!(
                    "分段 {} 的 RST 标记缺失（条带截断）",
                    self.next_k
                )));
            };
            let want = 0xD0 + (self.next_k & 7) as u8;
            if m != want {
                return Err(CoreError::jpeg(format!(
                    "分段 {} 的 RST 序号错乱（0x{m:02X} ≠ 0x{want:02X}）",
                    self.next_k
                )));
            }
            self.scan_at = at + 2;
            self.next_k += 1;
        }
        Ok(())
    }

    /// Decode the NEXT segment (strictly sequential). The sub-JPEG is the
    /// strip head minus DRI (restart_interval 0 → no RST checks) with the
    /// SOF dims patched to the segment's own grid, plus the segment's
    /// entropy bytes and an EOI. DC predictors reset at every RST in the
    /// source, so each segment decodes independently.
    pub fn decode_next(&mut self, budget: &mut MemBudget) -> CoreResult<SegmentRead> {
        let k = self.next_k;
        let r = self.g.restart_interval as u64;
        let mcu_start = k * r;
        let mcus = r.min(self.g.total_mcus - mcu_start);
        if mcus == 0 {
            return Err(CoreError::validation("分段越界（空段）"));
        }
        let seg_start = self.scan_at;
        let Some((marker_at, marker)) = self.scan_marker(budget, seg_start, self.strip_end())?
        else {
            return Err(CoreError::oob(format!(
                "分段 {k} 的结束标记缺失（条带截断）"
            )));
        };
        // verify the marker that closed this segment (restart numbering
        // restarts at RST0 after SOS — the first restart is RST0)
        if k + 1 < self.g.segments {
            let want = 0xD0 + (k & 7) as u8;
            if marker != want {
                return Err(CoreError::jpeg(format!(
                    "分段 {k} 的 RST 序号错乱（0x{marker:02X} ≠ 0x{want:02X}）"
                )));
            }
        } else if marker != 0xD9 {
            return Err(CoreError::jpeg(format!(
                "末段应以 EOI 结束（读到 0x{marker:02X}）"
            )));
        }
        let seg_len = (marker_at - seg_start) as usize;
        self.scan_at = marker_at + 2;
        self.next_k += 1;

        // segment grid: full MCU rows wherever that fits in 256 MCUs
        let grid_w = self.g.seg_grid_w(mcus);
        let rows = mcus.div_ceil(grid_w);
        let sub_w = grid_w * self.g.mcu_w as u64;
        let sub_h = rows * self.g.mcu_h as u64;
        if sub_w > u32::MAX as u64 || sub_h > u32::MAX as u64 {
            return Err(CoreError::variant(format!(
                "分段 {k} 子图尺寸 {sub_w}×{sub_h} 溢出"
            )));
        }
        let sub_px = sub_w
            .checked_mul(sub_h)
            .ok_or_else(|| CoreError::resource_limit("分段子图像素溢出"))?;
        // Review §1: the charge covers the segment read + decoder transient
        // (component planes ≤ 3 B/px) + the decoded RGB (3 B/px) — refused
        // BEFORE either allocation.
        let charge = seg_len as u64 + sub_px.saturating_mul(6);
        budget.charge(charge, "restart 分段解码（读取 + 解码器临时 + RGB）")?;

        // header surgery: patch the SOF dims (height at sof_hw_at, width +2)
        let sof_at = self.sof_hw_at();
        if sof_at + 4 > self.g.header_bytes.len() {
            return Err(CoreError::jpeg("SOF 位置越界"));
        }
        let mut sub = Vec::with_capacity(self.g.header_bytes.len() + seg_len + 2);
        sub.extend_from_slice(&self.g.header_bytes);
        sub[sof_at..sof_at + 2].copy_from_slice(&(sub_h as u16).to_be_bytes());
        sub[sof_at + 2..sof_at + 4].copy_from_slice(&(sub_w as u16).to_be_bytes());
        let seg = self.src.read_at(seg_start, seg_len)?;
        sub.extend_from_slice(&seg);
        sub.extend_from_slice(&[0xFF, 0xD9]); // EOI

        let img = match jpeg::decode_ex(&sub, sub_px, false) {
            Ok(img) => img,
            Err(e) => {
                budget.release(charge);
                return Err(CoreError::jpeg(format!(
                    "分段 {k} 解码失败：{}",
                    e.message
                )));
            }
        };
        if (img.width as u64) != sub_w || (img.height as u64) != sub_h {
            budget.release(charge);
            return Err(CoreError::jpeg(format!(
                "分段 {k} 解码尺寸 {}×{} ≠ 子图 {sub_w}×{sub_h}",
                img.width, img.height
            )));
        }
        Ok(SegmentRead { img, mcu_start, mcus, grid_w, charged: charge })
    }
}
