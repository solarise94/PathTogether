//! F2 review control (dev-only): isolate the JPEG generation loss of the
//! locked preserve parameters (q96) at 4:2:2 vs 4:4:4 on reference pixels.
use slide_transform_core::jpeg::{decode_ex, encode_rgb, EncoderCfg, Sampling};

fn metrics(tag: &str, ref_px: &[u8], got: &[u8], w: u32, h: u32) {
    let mut sum = [0f64; 3];
    let mut n = 0f64;
    let mut all = Vec::new();
    for i in 0..(w as usize * h as usize) {
        let (r, g, b) = (ref_px[i * 3], ref_px[i * 3 + 1], ref_px[i * 3 + 2]);
        let mx = r.max(g).max(b) as f64;
        let mn = r.min(g).min(b) as f64;
        let sat = (mx - mn) / mx.max(1.0);
        if sat > 0.10 && mx < 250.0 {
            for c in 0..3 {
                sum[c] += (got[i * 3 + c] as i64 - ref_px[i * 3 + c] as i64).abs() as f64;
            }
            n += 1.0;
            all.push((
                (got[i * 3] as i64 - r as i64).abs(),
                (got[i * 3 + 1] as i64 - g as i64).abs(),
                (got[i * 3 + 2] as i64 - b as i64).abs(),
            ));
        }
    }
    let mut diffs: Vec<f64> = all.iter().map(|(r, g, b)| (*r + *g + *b) as f64 / 3.0).collect();
    diffs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p = |q: f64| diffs[((diffs.len() as f64) * q) as usize].min(diffs[diffs.len() - 1]);
    println!(
        "{tag}: tissue_px={n} mean={:.3} (R={:.3} G={:.3} B={:.3}) p99={:.1} p99.9={:.1}",
        sum.iter().sum::<f64>() / (3.0 * n),
        sum[0] / n, sum[1] / n, sum[2] / n,
        p(0.99), p(0.999),
    );
    let _ = w;
    let _ = h;
}

fn main() {
    let w = 1024u32;
    let h = 1024u32;
    let reference = std::fs::read(
        std::env::var("CTRL_REF").unwrap_or_else(|_| "/mnt/ssd2/tests/run-j2k-w7fDae/ctrl-ref.rgb".into()),
    )
    .expect("ctrl-ref.rgb");
    let got = std::fs::read(
        std::env::var("CTRL_GOT").unwrap_or_else(|_| "/mnt/ssd2/tests/run-j2k-w7fDae/ctrl-got.rgb".into()),
    )
    .expect("ctrl-got.rgb");
    metrics("full-pipeline (openslide src vs our output)", &reference, &got, w, h);

    for (name, sampling) in [("q96 4:2:2 (locked preserve)", Sampling::S422), ("q96 4:4:4", Sampling::S444)] {
        let cfg = EncoderCfg::with_quality(96, sampling);
        let jpg = encode_rgb(&reference, w, h, &cfg).unwrap();
        let img = decode_ex(&jpg, (w as u64) * (h as u64), false).unwrap();
        metrics(name, &reference, &img.data, w, h);
    }
}
