// F3 preserve-encode bench: for each candidate encoder cfg, compose the
// L0 tissue ROIs of a bundle, split into 256x256 tiles, encode+decode each
// tile and measure per-channel error vs the composed source (compose == the
// OpenSlide rendering at L0, proven in tests/mirax.rs), plus byte totals.
use slide_transform_core::bundle::{BundleFs, DirBundle};
use slide_transform_core::convert_mirax::{compose_region, OUT_TILE};
use slide_transform_core::jpeg::{self, EncoderCfg, Sampling};
use slide_transform_core::mirax::probe_mirax;

struct Roi {
    id: String,
    x: u32,
    y: u32,
    w: u32,
    h: u32,
    expect: Vec<u8>,
    mask: Vec<u8>,
}

fn read_rois(dir: &str) -> Vec<Roi> {
    let list = std::fs::read_to_string(format!("{dir}/tissue-rois.json")).unwrap();
    let mut out = Vec::new();
    let mut cur = std::string::String::new();
    let mut depth = 0usize;
    let mut objs: Vec<String> = Vec::new();
    for ch in list.chars() {
        if depth > 0 {
            cur.push(ch);
        }
        match ch {
            '{' => {
                depth += 1;
                if depth == 1 { cur.clear(); cur.push(ch); }
            }
            '}' => {
                depth -= 1;
                if depth == 0 { objs.push(cur.clone()); }
            }
            _ => {}
        }
    }
    for o in objs {
        let field = |k: &str| -> String {
            let n = format!("\"{k}\"");
            let at = o.find(&n).unwrap() + n.len();
            let rest = &o[at..];
            let s = rest.find(':').unwrap() + 1;
            let rest = rest[s..].trim_start();
            let e = rest.find([',', '}']).unwrap();
            rest[..e].trim().trim_matches('"').to_string()
        };
        let id = field("id");
        let x: u32 = field("x").parse().unwrap();
        let y: u32 = field("y").parse().unwrap();
        let w: u32 = field("w").parse().unwrap();
        let h: u32 = field("h").parse().unwrap();
        let expect = std::fs::read(format!("{dir}/roi-l0-{id}.raw")).unwrap();
        let mask = std::fs::read(format!("{dir}/roi-l0-{id}.mask")).unwrap_or_default();
        out.push(Roi { id, x, y, w, h, expect, mask });
    }
    out
}

fn cfg_of(name: &str) -> EncoderCfg {
    match name {
        "rgb95" => EncoderCfg::with_rgb_quality(95),
        "y422q90" => EncoderCfg::with_quality(90, Sampling::S422),
        "y422q92" => EncoderCfg::with_quality(92, Sampling::S422),
        "y420q90" => EncoderCfg::with_quality(90, Sampling::S420),
        "y422q95" => EncoderCfg::with_quality(95, Sampling::S422),
        "y422q96" => EncoderCfg::with_quality(96, Sampling::S422),
        "y422q97" => EncoderCfg::with_quality(97, Sampling::S422),
        "rgb92" => EncoderCfg::with_rgb_quality(92),
        "rgb90" => EncoderCfg::with_rgb_quality(90),
        _ => panic!("cfg {name}"),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let dir = &args[1];
    let stem = &args[2];
    let rois_dir = &args[3];
    let cfgs: Vec<String> = args[4..].to_vec();
    let fs = DirBundle::open(std::path::Path::new(dir), stem).unwrap();
    let doc = probe_mirax(&fs, stem).unwrap();
    let rois = read_rois(rois_dir);
    for name in cfgs {
        let cfg = cfg_of(&name);
        let mut bytes = 0u64;
        let mut tiles = 0u64;
        let mut sum = [0u64; 3];
        let mut cnt = [0u64; 3];
        let mut mx = [0u32; 3];
        let mut p99_acc: Vec<Vec<u32>> = vec![Vec::new(), Vec::new(), Vec::new()];
        for r in &rois {
            let composed = compose_region(&fs, &doc, 0, r.x, r.y, r.w, r.h).unwrap();
            // per 256-tile round trip
            let tx = (r.w / OUT_TILE) as usize;
            let ty = (r.h / OUT_TILE) as usize;
            for tj in 0..ty {
                for ti in 0..tx {
                    let mut tile = vec![0u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];
                    for row in 0..OUT_TILE as usize {
                        let s = (tj * OUT_TILE as usize + row) * r.w as usize * 3
                            + ti * OUT_TILE as usize * 3;
                        let d = row * OUT_TILE as usize * 3;
                        tile[d..d + OUT_TILE as usize * 3]
                            .copy_from_slice(&composed[s..s + OUT_TILE as usize * 3]);
                    }
                    let enc = jpeg::encode_rgb(&tile, OUT_TILE, OUT_TILE, &cfg).unwrap();
                    bytes += enc.len() as u64;
                    tiles += 1;
                    let force_rgb = name.starts_with("y");
                    let dec = jpeg::decode_ex(&enc, 1 << 22, !force_rgb).unwrap();
                    for row in 0..OUT_TILE as usize {
                      for col in 0..OUT_TILE as usize {
                        let p = row * OUT_TILE as usize + col;
                        let gi = (tj * OUT_TILE as usize + row) * r.w as usize
                            + ti * OUT_TILE as usize + col;
                        let mi = r.mask.is_empty()
                            || (r.mask[gi / 8] & (1 << (7 - gi % 8)) != 0);
                        if !mi { continue; }
                        for c in 0..3 {
                            let d = (dec.data[p * 3 + c] as i64 - tile[p * 3 + c] as i64).unsigned_abs();
                            sum[c] += d;
                            cnt[c] += 1;
                            mx[c] = mx[c].max(d as u32);
                            p99_acc[c].push(d as u32);
                        }
                      }
                    }
                }
            }
            // full-ROI error vs the OpenSlide ground truth (masked)
            let _ = &r.expect;
        }
        let mut p99 = [0u32; 3];
        for c in 0..3 {
            let mut v = std::mem::take(&mut p99_acc[c]);
            v.sort_unstable();
            p99[c] = *v.get((v.len() * 99) / 100).unwrap_or(&0);
        }
        let mean: Vec<f64> = (0..3).map(|c| sum[c] as f64 / cnt[c].max(1) as f64).collect();
        println!(
            "{name}: tiles={tiles} bytes={bytes} B/tile={:.0} mean=[{:.3} {:.3} {:.3}] p99=[{} {} {}] max=[{} {} {}]",
            bytes as f64 / tiles as f64,
            mean[0], mean[1], mean[2],
            p99[0], p99[1], p99[2],
            mx[0], mx[1], mx[2],
        );
    }
}
