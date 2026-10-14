//! size cost of 4:4:4 vs 4:2:2 at q96 on the tissue control region
use slide_transform_core::jpeg::{encode_rgb, EncoderCfg, Sampling};

fn main() {
    let w = 1024u32;
    let h = 1024u32;
    let reference = std::fs::read("/mnt/ssd2/tests/run-j2k-w7fDae/ctrl-ref.rgb").unwrap();
    for (name, sampling) in [("q96 4:2:2", Sampling::S422), ("q96 4:4:4", Sampling::S444)] {
        let jpg = encode_rgb(&reference, w, h, &EncoderCfg::with_quality(96, sampling)).unwrap();
        println!("{name}: {} bytes ({:.1} KiB)", jpg.len(), jpg.len() as f64 / 1024.0);
    }
}
