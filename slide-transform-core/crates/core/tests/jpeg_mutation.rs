//! Deterministic mutation sweep over the baseline JPEG decoder: every mutated
//! bitstream must return (Ok or a typed error) without panicking, and the
//! pixel budget must be enforced before any allocation. Seeds come from the
//! crate's own encoder so the test needs no private samples. Debug builds
//! turn unchecked overflow into a panic, so this also exercises checked
//! arithmetic on hostile headers/entropy data.

#![cfg(feature = "codecs")]

use slide_transform_core::jpeg::decoder::{decode, extract_qtables, scan_jpeg};
use slide_transform_core::jpeg::{encode_gray, encode_rgb, qtables_pillow_style, EncoderCfg, Sampling};

fn seeds() -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    for (w, h) in [(16u32, 16u32), (37, 23), (64, 48), (129, 7)] {
        let mut rgb = Vec::with_capacity((w * h * 3) as usize);
        for i in 0..w * h {
            let (x, y) = (i % w, i / w);
            rgb.extend_from_slice(&[(x * 7) as u8, (y * 11) as u8, ((x ^ y) * 5) as u8]);
        }
        for s in [Sampling::S444, Sampling::S422, Sampling::S420] {
            out.push(encode_rgb(&rgb, w, h, &EncoderCfg::with_quality(80, s)).unwrap());
        }
        let gray: Vec<u8> = (0..w * h).map(|i| (i * 13) as u8).collect();
        out.push(encode_gray(&gray, w, h, &EncoderCfg::with_quality(60, Sampling::S444).y_q).unwrap());
    }
    out
}

#[test]
fn mutated_bitstreams_never_panic() {
    let seeds = seeds();
    let mut s: u64 = 0x2545_F491_4F6C_DD1D;
    let mut rnd = move || {
        s ^= s << 13;
        s ^= s >> 7;
        s ^= s << 17;
        s
    };
    let iters = if cfg!(debug_assertions) { 4_000 } else { 40_000 };
    for i in 0..iters {
        let mut d = seeds[(rnd() as usize) % seeds.len()].clone();
        for _ in 0..1 + rnd() % 8 {
            if d.is_empty() {
                break;
            }
            let p = (rnd() as usize) % d.len();
            match rnd() % 6 {
                0 => d[p] ^= 1 << (rnd() % 8),
                1 => d[p] = rnd() as u8,
                2 => d.truncate(p),
                3 => {
                    let m = [0xFFu8, (0xC0 + rnd() % 0x3F) as u8];
                    d.splice(p..p, m);
                }
                4 => {
                    let q = (rnd() as usize) % d.len();
                    d[p] = d[q];
                }
                _ => {
                    if p + 1 < d.len() {
                        d[p] = 0xFF;
                        d[p + 1] = 0xFF;
                    }
                }
            }
        }
        let r = std::panic::catch_unwind(|| {
            let _ = scan_jpeg(&d);
            let _ = extract_qtables(&d);
            let _ = qtables_pillow_style(&d);
            let _ = decode(&d, 1 << 20);
        });
        assert!(r.is_ok(), "iteration {i} panicked");
    }
}

#[test]
fn pixel_budget_rejects_before_decoding() {
    let seed = &seeds()[0];
    let sof = (2..seed.len() - 9)
        .find(|&i| seed[i] == 0xFF && seed[i + 1] == 0xC0)
        .unwrap();
    for (h, w) in [(0xFFFFu16, 0xFFFFu16), (4096, 4096), (0, 16), (16, 0)] {
        let mut m = seed.clone();
        m[sof + 5..sof + 7].copy_from_slice(&h.to_be_bytes());
        m[sof + 7..sof + 9].copy_from_slice(&w.to_be_bytes());
        assert!(decode(&m, 1 << 20).is_err(), "{h}x{w} must be rejected");
    }
}
