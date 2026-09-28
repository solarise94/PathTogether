//! Streaming synthetic `kfb_bf_v1` generator (same disk contract as
//! `kfb/fixture.py`, scaled to >4 GiB inputs). Tile payloads are encoded
//! once per unique size from a deterministic seeded noise image and then
//! reused, so generation is IO-bound, not encode-bound. No patient data.

use crate::error::{CoreError, CoreResult};
use crate::io::RandomAccessSink;
use crate::kfb::{KfbLevel, MAGIC, TILE_W};

#[derive(Debug, Clone)]
pub struct GenParams {
    pub width: u32,
    pub height: u32,
    pub quality: u8,
    /// "422" (like the real brightfield sample), "420", or "444".
    pub sampling: &'static str,
    pub mpp: f64,
    pub objective: f32,
    pub scanner_id: String,
    pub seed: u64,
}

impl Default for GenParams {
    fn default() -> Self {
        GenParams {
            width: 580,
            height: 300,
            quality: 90,
            sampling: "422",
            mpp: 0.4841049,
            objective: 20.0,
            scanner_id: "PTSYNTH0001".into(),
            seed: 0,
        }
    }
}

struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        // xorshift64*
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545F4914F6CDD1D)
    }
    fn byte(&mut self) -> u8 {
        (self.next_u64() >> 33) as u8
    }
}

fn encode_tile(
    w: u16,
    h: u16,
    seed: u64,
    quality: u8,
    sf: crate::jpeg::Sampling,
) -> CoreResult<Vec<u8>> {
    let mut rng = Rng(seed | 1);
    let mut px = vec![0u8; w as usize * h as usize * 3];
    for c in px.chunks_exact_mut(3) {
        c[0] = rng.byte();
        c[1] = rng.byte();
        c[2] = rng.byte();
    }
    let cfg = crate::jpeg::EncoderCfg::with_quality(quality, sf);
    crate::jpeg::encode_rgb(&px, w as u32, h as u32, &cfg)
}

fn level_geometry(width: u32, height: u32) -> Vec<KfbLevel> {
    let mut levels = Vec::new();
    for lvl in 0..16u32 {
        let w = (width >> lvl).max(1);
        let h = (height >> lvl).max(1);
        levels.push(KfbLevel { level: lvl, width: w, height: h });
        if (w + TILE_W - 1) / TILE_W == 1 && (h + TILE_W - 1) / TILE_W == 1 {
            break;
        }
    }
    levels
}

/// Write the synthetic KFB; returns the file size.
pub fn build_synthetic_kfb(out: &mut dyn RandomAccessSink, p: &GenParams) -> CoreResult<u64> {
    if !(1..=200_000).contains(&p.width) || !(1..=200_000).contains(&p.height) {
        return Err(CoreError::header("宽/高越界"));
    }
    let sf = match p.sampling {
        "444" => crate::jpeg::Sampling::S444,
        "420" => crate::jpeg::Sampling::S420,
        _ => crate::jpeg::Sampling::S422,
    };
    let levels = level_geometry(p.width, p.height);

    // 每个唯一 tile 尺寸编码一次；层间用不同 seed 区分
    let mut cache: std::collections::HashMap<(u16, u16, u32), Vec<u8>> =
        std::collections::HashMap::new();
    let mut get_payload = |w: u16, h: u16, level: u32| -> CoreResult<Vec<u8>> {
        match cache.get(&(w, h, level)) {
            Some(v) => Ok(v.clone()),
            None => {
                let v = encode_tile(
                    w,
                    h,
                    (p.seed ^ (level as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15)) | 1,
                    p.quality,
                    sf,
                )?;
                cache.insert((w, h, level), v.clone());
                Ok(v)
            }
        }
    };

    const HEADER_BYTES: u64 = 96;
    let mut cursor = HEADER_BYTES;
    let mut entries: Vec<[u8; 32]> = Vec::new();

    for lv in &levels {
        let (ny, nx) = (lv.tiles_down(), lv.tiles_across());
        for row in 0..ny {
            for col in 0..nx {
                let x0 = col * TILE_W;
                let y0 = row * TILE_W;
                let tw = TILE_W.min(lv.width - x0) as u16;
                let th = TILE_W.min(lv.height - y0) as u16;
                let data = get_payload(tw, th, lv.level)?;
                out.write_at(cursor, &data)?;
                let mut e = [0u8; 32];
                e[0..4].copy_from_slice(&lv.level.to_le_bytes());
                e[4..8].copy_from_slice(&x0.to_le_bytes());
                e[8..12].copy_from_slice(&y0.to_le_bytes());
                e[12..14].copy_from_slice(&tw.to_le_bytes());
                e[14..16].copy_from_slice(&th.to_le_bytes());
                e[16..24].copy_from_slice(&cursor.to_le_bytes());
                e[24..28].copy_from_slice(&(data.len() as u32).to_le_bytes());
                entries.push(e);
                cursor += data.len() as u64;
            }
        }
    }
    let index_offset = cursor;
    if entries.len() > 2_000_000 {
        return Err(CoreError::index("合成 tile 数超出 parser 上限"));
    }
    let mut idx = Vec::with_capacity(entries.len() * 32);
    for e in &entries {
        idx.extend_from_slice(e);
    }
    out.write_at(index_offset, &idx)?;
    cursor += idx.len() as u64;

    // header
    let mut header = [0u8; 96];
    fn put32(h: &mut [u8; 96], at: usize, v: u32) {
        h[at..at + 4].copy_from_slice(&v.to_le_bytes());
    }
    header[0..8].copy_from_slice(&MAGIC);
    put32(&mut header, 0x08, 1); // version
    put32(&mut header, 0x0C, 96); // header_bytes
    put32(&mut header, 0x10, p.width);
    put32(&mut header, 0x14, p.height);
    put32(&mut header, 0x18, 256);
    put32(&mut header, 0x1C, 256);
    put32(&mut header, 0x20, levels.len() as u32);
    put32(&mut header, 0x24, entries.len() as u32);
    header[0x28..0x30].copy_from_slice(&p.mpp.to_le_bytes());
    header[0x30..0x38].copy_from_slice(&p.mpp.to_le_bytes());
    header[0x38..0x3C].copy_from_slice(&p.objective.to_le_bytes());
    let scanner = p.scanner_id.as_bytes();
    let n = scanner.len().min(15);
    header[0x3C..0x3C + n].copy_from_slice(&scanner[..n]);
    put32(&mut header, 0x4C, 0); // associated_count
    header[0x50..0x58].copy_from_slice(&index_offset.to_le_bytes());
    put32(&mut header, 0x58, 1); // flags: brightfield
    out.write_at(0, &header)?;
    out.flush()?;
    Ok(cursor)
}
