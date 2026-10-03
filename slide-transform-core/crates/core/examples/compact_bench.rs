//! U3 feasibility bench (not shipped): measures the in-core JPEG codec as a
//! GENERAL encoder — full-tile decode + re-encode per 256×256 tile over a
//! real or synthetic KFB, per candidate parameter set, plus the per-tile
//! scratch-memory peak via a counting global allocator.
//!
//! Usage:
//!   compact_bench <input.kfb> [--tiles N] [--level L]
//!
//! Reports, per (quality × sampling): ms/tile decode, ms/tile encode,
//! output bytes/tile, and the allocator peak of one decode+encode cycle.

use std::alloc::{GlobalAlloc, Layout, System};
use std::path::Path;
use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::time::Instant;

use slide_transform_core::io::{ByteSource, FileScratch, FileSource};
use slide_transform_core::jpeg::{decode, encode_rgb, EncoderCfg, Sampling};
use slide_transform_core::kfb::parse_kfb;

// ------------------------------------------------------- tracking allocator --

static LIVE: AtomicI64 = AtomicI64::new(0);
static PEAK: AtomicI64 = AtomicI64::new(0);
static ALLOCS: AtomicU64 = AtomicU64::new(0);

struct Tracking;

unsafe impl GlobalAlloc for Tracking {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let p = System.alloc(layout);
        if !p.is_null() {
            let live = LIVE.fetch_add(layout.size() as i64, Ordering::Relaxed) + layout.size() as i64;
            ALLOCS.fetch_add(1, Ordering::Relaxed);
            PEAK.fetch_max(live, Ordering::Relaxed);
        }
        p
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        LIVE.fetch_sub(layout.size() as i64, Ordering::Relaxed);
        System.dealloc(ptr, layout)
    }
}

#[global_allocator]
static GLOBAL: Tracking = Tracking;

// ------------------------------------------------------------------- bench --

fn sampling_of(s: &str) -> Sampling {
    match s {
        "444" => Sampling::S444,
        "420" => Sampling::S420,
        _ => Sampling::S422,
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let path = args.first().expect("usage: compact_bench <in.kfb> [--tiles N] [--level L]").clone();
    let mut want_tiles: usize = 512;
    let mut level: u32 = 0;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--tiles" => {
                i += 1;
                want_tiles = args[i].parse().expect("--tiles N");
            }
            "--level" => {
                i += 1;
                level = args[i].parse().expect("--level L");
            }
            _ => {}
        }
        i += 1;
    }

    let src = FileSource::open(Path::new(&path)).unwrap();
    let dir = Path::new(&path).parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let mut scratch = FileScratch::new(&dir);
    let doc = parse_kfb(&src, &mut scratch).unwrap();

    // collect level-0 full-tile payloads (the preserve mode copies these
    // verbatim; compact must decode + re-encode each of them)
    let mut payloads: Vec<Vec<u8>> = Vec::new();
    doc.grids.for_each_cell(level, |_cell, rec| {
        if let Some(rec) = rec {
            if rec.is_full_tile() && payloads.len() < want_tiles {
                payloads.push(src.read_at(rec.payload_offset, rec.payload_length as usize).unwrap());
            }
        }
        Ok(())
    })
    .unwrap();
    eprintln!(
        "input: {} bytes, level {} full tiles sampled: {}",
        src.size(),
        level,
        payloads.len()
    );

    let candidates: Vec<(u8, &str)> = vec![
        (80, "420"), (80, "422"),
        (85, "420"), (85, "422"),
        (90, "420"), (90, "422"),
        (85, "444"),
    ];

    println!("cfg\tdecode_ms/tile\tencode_ms/tile\tout_bytes/tile");
    for (q, s) in &candidates {
        let sf = sampling_of(s);
        let t0 = Instant::now();
        let mut decoded = Vec::with_capacity(payloads.len());
        for p in &payloads {
            decoded.push(decode(p, 256 * 256 * 4).unwrap());
        }
        let dec_ms = t0.elapsed().as_secs_f64() / payloads.len() as f64 * 1000.0;
        let t1 = Instant::now();
        let mut total = 0u64;
        let cfg = EncoderCfg::with_quality(*q, sf);
        for img in &decoded {
            total += encode_rgb(&img.data, img.width, img.height, &cfg).unwrap().len() as u64;
        }
        let enc_ms = t1.elapsed().as_secs_f64() / payloads.len() as f64 * 1000.0;
        println!(
            "q{s}{q}\t{dec_ms:.3}\t{enc_ms:.3}\t{}",
            total / payloads.len() as u64
        );
    }

    // per-tile scratch peak: one isolated decode+encode cycle with the
    // allocator counters reset (canvas included, like the converter does)
    LIVE.store(0, Ordering::Relaxed);
    PEAK.store(0, Ordering::Relaxed);
    ALLOCS.store(0, Ordering::Relaxed);
    let a0 = ALLOCS.load(Ordering::Relaxed);
    let cfg = EncoderCfg::with_quality(85, Sampling::S422);
    let img = decode(&payloads[0], 256 * 256 * 4).unwrap();
    let mut canvas = vec![255u8; 256 * 256 * 3];
    for y in 0..img.height as usize {
        let s = &img.data[y * img.width as usize * 3..(y + 1) * img.width as usize * 3];
        canvas[y * 256 * 3..y * 256 * 3 + s.len()].copy_from_slice(s);
    }
    let out = encode_rgb(&canvas, 256, 256, &cfg).unwrap();
    let peak = PEAK.load(Ordering::Relaxed) as u64;
    eprintln!(
        "scratch peak (decode+canvas+encode, 1 tile): {} B ({:.1} KiB), encoded {} B, allocs {}",
        peak,
        peak as f64 / 1024.0,
        out.len(),
        ALLOCS.load(Ordering::Relaxed) - a0
    );
}
