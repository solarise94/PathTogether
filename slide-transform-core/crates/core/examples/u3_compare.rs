//! U3 step-2 parameter comparison driver (not shipped). For each candidate
//! (quality × sampling) it re-encodes every level-0 tile of a KFB (real or
//! synthetic) — plus optional synthetic texture tiles (low-contrast /
//! high-saturation, which the gen-kfb noise generator cannot produce) — and
//! reports bytes/tile, PSNR, per-channel mean-abs-diff, max diff (vs the
//! decoded source, i.e. vs what `preserve` hands to the viewer) and the same
//! metrics restricted to "tissue" (non-background) pixels.
//!
//! Usage:
//!   u3_compare <input.kfb> [--tiles N] [--texture lc|hs|both]

use std::path::Path;
use std::time::Instant;

use slide_transform_core::io::{ByteSource, FileScratch, FileSource};
use slide_transform_core::jpeg::{decode, encode_rgb, EncoderCfg, Sampling};
use slide_transform_core::kfb::parse_kfb;

fn sampling_of(s: &str) -> Sampling {
    match s {
        "444" => Sampling::S444,
        "420" => Sampling::S420,
        _ => Sampling::S422,
    }
}

fn rgb_to_luma(px: &[u8]) -> Vec<u8> {
    px.chunks_exact(3)
        .map(|c| {
            // ITU-R BT.601 integer luma (viewer-equivalent weighting)
            ((c[0] as u32 * 77 + c[1] as u32 * 150 + c[2] as u32 * 29 + 128) >> 8) as u8
        })
        .collect()
}

#[derive(Default, Clone, Copy)]
struct Metrics {
    n: u64,
    se_l: f64,
    se: [f64; 3],
    abs_l: f64,
    abs: [f64; 3],
    max_l: u32,
    max: u32,
    // tissue-restricted accumulators (background = near-white, low sat)
    n_t: u64,
    se_l_t: f64,
    abs_l_t: f64,
    se_t: [f64; 3],
    abs_t: [f64; 3],
    max_l_t: u32,
}

fn is_tissue(px: &[u8]) -> bool {
    // H&E tissue: notably darker or more saturated than the white ground
    let r = px[0] as i32;
    let g = px[1] as i32;
    let b = px[2] as i32;
    let mx = r.max(g).max(b);
    let mn = r.min(g).min(b);
    mx < 235 || (mx - mn) > 24
}

fn accumulate(a: &mut Metrics, src: &[u8], enc: &[u8]) {
    let l_s = rgb_to_luma(src);
    let l_e = rgb_to_luma(enc);
    a.n += src.len() as u64 / 3;
    let mut tissue = Vec::with_capacity(8);
    for (i, (s, e)) in src.chunks_exact(3).zip(enc.chunks_exact(3)).enumerate() {
        let dl = (l_s[i] as i32 - l_e[i] as i32).abs();
        a.se_l += (dl * dl) as f64;
        a.abs_l += dl as f64;
        a.max_l = a.max_l.max(dl as u32);
        let mut t = false;
        for c in 0..3 {
            let d = (s[c] as i32 - e[c] as i32).abs();
            a.se[c] += (d * d) as f64;
            a.abs[c] += d as f64;
            a.max = a.max.max(d as u32);
            t = t || is_tissue(s);
        }
        if t {
            a.n_t += 1;
            a.se_l_t += (dl * dl) as f64;
            a.abs_l_t += dl as f64;
            a.max_l_t = a.max_l_t.max(dl as u32);
            for c in 0..3 {
                let d = (s[c] as i32 - e[c] as i32).abs();
                a.se_t[c] += (d * d) as f64;
                a.abs_t[c] += d as f64;
            }
            tissue.push(i);
        }
    }
    let _ = tissue;
}

fn psnr(se: f64, n: u64) -> f64 {
    let mse = se / n.max(1) as f64;
    if mse <= 0.0 {
        f64::INFINITY
    } else {
        10.0 * (255.0f64 * 255.0 / mse).log10()
    }
}

fn print_metrics(tag: &str, q: u8, s: &str, bytes: u64, tiles: u64, a: &Metrics, src_bytes: u64) {
    let n = a.n.max(1) as f64;
    let nt = a.n_t.max(1) as f64;
    println!(
        "{tag}\tq{s}{q}\t{:.0}\t{:.2}\t{:.2}\t{:.2}\t{:.2}\t{:.3}\t{}\t{}\t{}\t{:.2}\t{:.3}\t{:.3}\t{:.1}",
        bytes as f64 / tiles as f64,      // re-encoded bytes per tile
        psnr(a.se_l, a.n),                // luma PSNR (all pixels)
        psnr(a.se[0], a.n),
        psnr(a.se[1], a.n),
        psnr(a.se[2], a.n),
        a.abs_l / n,                      // luma mean abs diff
        a.max,                            // max per-channel diff
        a.max_l,                          // max luma diff
        a.n_t * 100 / a.n,                // tissue pixel share
        psnr(a.se_l_t, a.n_t),            // luma PSNR on tissue
        a.abs_l_t / nt,                   // tissue luma MAD
        a.abs_t.iter().cloned().fold(f64::INFINITY, f64::min) / nt, // best-channel tissue MAD
        100.0 * bytes as f64 / src_bytes as f64, // size vs source payload
    );
}

/// Low-contrast texture: gentle blobs on a near-uniform ground (Δ ≈ 6 levels)
fn texture_low_contrast(seed: u64) -> Vec<u8> {
    let mut px = vec![0u8; 256 * 256 * 3];
    let mut rng = Rng(seed | 1);
    for y in 0..256usize {
        for x in 0..256usize {
            let o = (y * 256 + x) * 3;
            let mut v = 205.0f64;
            for (cx, cy, r) in [(70.0, 60.0, 40.0), (180.0, 120.0, 55.0), (120.0, 210.0, 35.0)] {
                let d2 = (x as f64 - cx).powi(2) + (y as f64 - cy).powi(2);
                v += 6.0 * (-(d2 / (r * r))).exp();
            }
            v += (rng.byte() as f64 - 127.5) * 0.02; // faint grain
            let b = v.round().clamp(0.0, 255.0) as u8;
            px[o] = b;
            px[o + 1] = (b as i16 + 1).clamp(0, 255) as u8;
            px[o + 2] = (b as i16 - 1).clamp(0, 255) as u8;
        }
    }
    px
}

/// High-saturation texture: vivid H&E-like hues at full chroma + structure
fn texture_high_saturation(seed: u64) -> Vec<u8> {
    let mut px = vec![0u8; 256 * 256 * 3];
    let mut rng = Rng(seed | 1);
    for y in 0..256usize {
        for x in 0..256usize {
            let o = (y * 256 + x) * 3;
            // two saturated families: magenta/purple nuclei, pink cytoplasm
            let nucleus = ((x as f64 - 128.0).powi(2) + (y as f64 - 128.0).powi(2)) < 2500.0;
            let grain = (rng.byte() as i16 - 128) / 8;
            if nucleus {
                px[o] = (120 + grain).clamp(0, 255) as u8;
                px[o + 1] = (30 + grain).clamp(0, 255) as u8;
                px[o + 2] = (160 + grain).clamp(0, 255) as u8;
            } else {
                px[o] = (235 + grain).clamp(0, 255) as u8;
                px[o + 1] = (110 + grain).clamp(0, 255) as u8;
                px[o + 2] = (160 + grain).clamp(0, 255) as u8;
            }
        }
    }
    px
}

struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
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

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut want_tiles: usize = 512;
    let mut texture = String::new();
    let mut input: Option<String> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--tiles" => {
                i += 1;
                want_tiles = args[i].parse().expect("--tiles");
            }
            "--texture" => {
                i += 1;
                texture = args[i].clone();
            }
            _ => input = Some(args[i].clone()),
        }
        i += 1;
    }

    println!("set\tcfg\tB/tile\tPSNR_l\tPSNR_r\tPSNR_g\tPSNR_b\tMAD_l\tmax_rgb\tmax_l\ttissue%\tPSNR_l_t\tMAD_l_t\tMAD_c_best\tsize%");

    // source tiles: level-0 payloads of the KFB, each decoded once (this is
    // exactly the pixel stream the preserve mode hands to viewers)
    let mut sources: Vec<(Vec<u8>, u64)> = Vec::new(); // (decoded rgb, source jpeg len)
    let mut tag = String::new();
    let has_input = input.is_some();
    if let Some(path) = input {
        let src = FileSource::open(Path::new(&path)).unwrap();
        let dir = Path::new(&path).parent().map(|p| p.to_path_buf()).unwrap_or_default();
        let mut scratch = FileScratch::new(&dir);
        let doc = parse_kfb(&src, &mut scratch).unwrap();
        doc.grids.for_each_cell(0, |_cell, rec| {
            if let Some(rec) = rec {
                if sources.len() < want_tiles {
                    let payload =
                        src.read_at(rec.payload_offset, rec.payload_length as usize).unwrap();
                    let len = payload.len() as u64;
                    let img = decode(&payload, 256 * 256 * 4).unwrap();
                    // paste on the white canvas exactly like both modes do
                    let mut canvas = vec![255u8; 256 * 256 * 3];
                    for y in 0..img.height as usize {
                        let s = &img.data[y * img.width as usize * 3..(y + 1) * img.width as usize * 3];
                        let off = y * 256 * 3;
                        canvas[off..off + s.len()].copy_from_slice(s);
                    }
                    sources.push((canvas, len));
                }
            }
            Ok(())
        })
        .unwrap();
        tag = "kfb".to_string();
        eprintln!("kfb: {} level-0 tiles", sources.len());
    }
    for (name, mk) in [
        ("lc", texture_low_contrast as fn(u64) -> Vec<u8>),
        ("hs", texture_high_saturation as fn(u64) -> Vec<u8>),
    ] {
        if texture != name && texture != "both" {
            continue;
        }
        // the synthetic texture plays the role of the SOURCE pixels: encode
        // it once at q90 4:2:2 (scanner-like), decode, and use that as the
        // source stream — one prior lossy generation, like real scanners
        let raw = mk(7);
        let src_cfg = EncoderCfg::with_quality(90, Sampling::S422);
        let jpg = encode_rgb(&raw, 256, 256, &src_cfg).unwrap();
        let img = decode(&jpg, 256 * 256 * 4).unwrap();
        sources.push((img.data, jpg.len() as u64));
        tag = if tag.is_empty() { name.into() } else { tag };
        eprintln!("texture {name}: source q90 4:2:2 {} B", jpg.len());
    }
    if sources.is_empty() {
        eprintln!("usage: u3_compare <in.kfb> [--tiles N] [--texture lc|hs|both]");
        std::process::exit(2);
    }

    let candidates: Vec<(u8, &str)> = vec![
        (80, "420"), (80, "422"),
        (85, "420"), (85, "422"),
        (90, "420"), (90, "422"),
        (85, "444"),
    ];
    // per-set evaluation: the kfb set (real/synthetic scanner tiles) and each
    // synthetic texture get their own table rows
    let mut groups: Vec<(String, Vec<(Vec<u8>, u64)>)> = Vec::new();
    if has_input {
        // kfb tiles (may include the textures appended above → split them)
        let kfb_n = sources.len() - if texture == "both" { 2 } else if texture.is_empty() { 0 } else { 1 };
        groups.push(("kfb".to_string(), sources[..kfb_n].to_vec()));
    }
    if texture == "lc" || texture == "both" {
        let n = sources.len();
        groups.push(("lc".to_string(), vec![sources[n - if texture == "both" { 2 } else { 1 }].clone()]));
    }
    if texture == "hs" || texture == "both" {
        groups.push(("hs".to_string(), vec![sources[sources.len() - 1].clone()]));
    }
    if groups.is_empty() {
        groups.push((tag.clone(), sources.clone()));
    }

    for (gname, tiles) in &groups {
        for (q, s) in &candidates {
            let cfg = EncoderCfg::with_quality(*q, sampling_of(s));
            let mut total_bytes = 0u64;
            let mut src_bytes = 0u64;
            let t0 = Instant::now();
            let mut m = Metrics::default();
            for (src, slen) in tiles {
                let enc = encode_rgb(src, 256, 256, &cfg).unwrap();
                total_bytes += enc.len() as u64;
                src_bytes += slen;
                let dec = decode(&enc, 256 * 256 * 4).unwrap();
                accumulate(&mut m, src, &dec.data);
            }
            let ms = t0.elapsed().as_secs_f64() * 1000.0 / tiles.len() as f64;
            print_metrics(gname, *q, s, total_bytes, tiles.len() as u64, &m, src_bytes);
            eprintln!("{gname} q{s}{q}: {ms:.2} ms/tile encode");
        }
    }
}
