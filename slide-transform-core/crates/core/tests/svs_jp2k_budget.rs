//! Review R1 boundary cases for the JPEG 2000 per-tile working-set budget.
//!
//! The reviewer's pre-allocation regression lives in `tests/review_budget.rs`
//! and is kept verbatim. This file adds the required boundaries:
//! both Aperio variants (33003/33005), full vs padded edge tiles, the
//! saver/normal profile budgets, and a huge malicious codestream header that
//! must be refused BEFORE the decoder entry point is reached (proved with the
//! `decoder_probe` invocation counter — no multi-GB allocation is attempted).
//!
//! Requires the `fixtures` + `jp2k` features:
//! `cargo test -p slide-transform-core --features "fixtures jp2k" --test svs_jp2k_budget`.

#![cfg(all(feature = "codecs", feature = "fixtures", feature = "jp2k"))]

use slide_transform_core::budget::ADAPTER_RESERVE_BYTES;
use slide_transform_core::error::ErrorCode;
use slide_transform_core::io::{MemScratch, MemSink, MemSource};
use slide_transform_core::jp2k::{self, peak};
use slide_transform_core::plan::{InputIdentity, TransformPlan};
use slide_transform_core::svs_fixture::{build_synthetic_svs_jp2k, SvsJp2kFixture};

/// The browser resource profiles' budgets (engine.js PROFILES): saver = 192
/// MiB, balanced ("normal") = 384 MiB, faster = 768 MiB.
const SAVER: u64 = 192 * 1024 * 1024;
const BALANCED: u64 = 384 * 1024 * 1024;

fn fixture(n: &str) -> Vec<u8> {
    std::fs::read(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/jp2k")
            .join(n),
    )
    .unwrap()
}

/// 33005 container: 480×480 over 240 px tiles (2×2), 4 levels.
fn container_33005() -> Vec<u8> {
    let t = fixture("cc0-33005-l0.j2k");
    let l1 = fixture("cc0-33005-l1.j2k");
    let f = SvsJp2kFixture {
        compression: 33005,
        photometric: 2,
        tile: 240,
        width: 480,
        height: 480,
        downsample: 4,
        mpp: Some(0.499),
        appmag: Some(20.0),
        levels: vec![
            vec![t.clone(), t.clone(), t.clone(), t],
            vec![l1.clone()],
            vec![l1.clone()],
            vec![l1],
        ],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
    sink.data
}

/// 33003 container: 600×300 over 256 px tiles, 3 levels. With
/// `padded_edge`, the last-column L0 tile is the cropped 200×256 codestream
/// (the codestream-smaller-than-tile-rect / padded-canvas case).
fn container_33003(padded_edge: bool) -> Vec<u8> {
    let full = fixture("synth-33003-full.j2k");
    let last = if padded_edge {
        fixture("synth-33003-cropped.j2k")
    } else {
        full.clone()
    };
    let f = SvsJp2kFixture {
        compression: 33003,
        photometric: 2,
        tile: 256,
        width: 600,
        height: 300,
        downsample: 4,
        mpp: Some(0.499),
        appmag: Some(20.0),
        levels: vec![
            vec![full.clone(), full.clone(), last, full.clone(), full.clone(), full.clone()],
            vec![full.clone()],
            vec![full],
        ],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
    sink.data
}

fn convert(input: &[u8], budget: u64) -> (Result<(), ErrorCode>, usize) {
    let src = MemSource::new(input.to_vec());
    let mut out = MemSink::new();
    let mut scratch = MemScratch::default();
    let mut plan = TransformPlan::brightfield(InputIdentity::default());
    plan.limits.memory_budget_bytes = budget;
    let r = slide_transform_core::convert_svs::convert_svs(&src, &mut out, &mut scratch, &plan);
    let code = r.as_ref().err().map(|e| e.code);
    (code.map_or(Ok(()), Err), out.data.len())
}

/// Patch the SIZ reference-grid / codestream-tile extents of a real tile
/// codestream to `side × side`. SIZ file offsets: Xsiz=8, Ysiz=12, XtSiz=24,
/// YtSiz=28.
fn patch_siz_side(mut d: Vec<u8>, side: u32) -> Vec<u8> {
    for at in [8usize, 12, 24, 28] {
        d[at..at + 4].copy_from_slice(&side.to_be_bytes());
    }
    d
}

// ------------------------------------------------------------------ //
// The peak formula itself: each term is asserted, not just the total. //
// ------------------------------------------------------------------ //

#[test]
fn peak_terms_cover_the_documented_decode_and_encode_working_set() {
    // a nominal 8192² 3-component tile (the SVS tile cap)
    let b = peak::working_set(3, 8192, 8192, 8192, 8192).unwrap();
    assert_eq!(b.decode_planes, 3 * 8192 * 8192 * 4); // 768 MiB
    assert_eq!(b.storage_coefficients, 3 * 8192 * 8192 * 4);
    assert_eq!(b.idwt_buffers, 2 * (8192 + 2) * (8192 + 2) * 4);
    assert_eq!(b.bitplane_transient, 8192 * 8192 * peak::DECODER_BITPLANE_BPP);
    assert_eq!(b.rgb_output, 8192 * 8192 * 3); // 192 MiB
    assert_eq!(b.padded_canvas, 0); // nominal tile ⇒ no canvas
    assert_eq!(b.encoder_planes, 3 * 8192 * 8192);
    assert_eq!(b.encoder_output, 8192 * 8192 / 2);
    assert_eq!(b.fixed_slack, peak::FIXED_SLACK);
    let expected: u64 = [
        b.decode_planes,
        b.storage_coefficients,
        b.idwt_buffers,
        b.bitplane_transient,
        b.rgb_output,
        b.padded_canvas,
        b.encoder_planes,
        b.encoder_output,
        b.fixed_slack,
    ]
    .into_iter()
    .sum();
    assert_eq!(b.total(), expected);
    assert!(b.total() > SAVER, "8192² peak {} must exceed the saver budget", b.total());
}

#[test]
fn peak_adds_the_padded_canvas_only_for_edge_tiles() {
    // same codestream (200×256), tile rect equal ⇒ no canvas; a larger 256×256
    // nominal rect ⇒ the full tile-sized white canvas is live alongside RGB.
    let nominal = peak::working_set(3, 200, 256, 200, 256).unwrap();
    let padded = peak::working_set(3, 200, 256, 256, 256).unwrap();
    assert_eq!(nominal.padded_canvas, 0);
    assert_eq!(padded.padded_canvas, 256 * 256 * 3);
    // the canvas term alone is what a nominal tile never pays; the total grows
    // by at least the canvas (the larger tile rect also grows the encoder).
    assert!(padded.total() >= nominal.total() + padded.padded_canvas);
}

// ------------------------------------------------------------------ //
// Real fixtures convert under both real profile budgets.              //
// ------------------------------------------------------------------ //

#[test]
fn both_variants_convert_under_the_saver_and_balanced_budgets() {
    for (name, input) in [
        ("33005 full", container_33005()),
        ("33003 full", container_33003(false)),
        ("33003 padded edge", container_33003(true)),
    ] {
        for (profile, budget) in [("saver", SAVER), ("balanced", BALANCED)] {
            let (r, bytes) = convert(&input, budget);
            assert!(
                r.is_ok(),
                "{name} under {profile} must convert, got {r:?} (only {bytes} B out)"
            );
            assert!(bytes > 0, "{name} {profile}: expected output");
        }
    }
}

// ------------------------------------------------------------------ //
// An insufficient budget is refused BEFORE any output byte.           //
// ------------------------------------------------------------------ //

#[test]
fn insufficient_budget_is_refused_at_plan_time_with_zero_output() {
    for input in [container_33005(), container_33003(false)] {
        let (r, bytes) = convert(&input, 1);
        assert_eq!(r.unwrap_err(), ErrorCode::ResourceLimitExceeded);
        assert_eq!(bytes, 0, "refusal must happen before any output byte");
    }
}

/// A realistic mid-range budget that fits a 256² tile but not an 8192² one.
/// (256² peak ≈ 3.9 MiB; 8192² peak ≈ 2.9 GiB — the 3 f32 decode planes alone
/// are 768 MiB, plus the same-order coefficient storage, IDWT, bit-plane
/// transient, RGB and encoder buffers.)
#[test]
fn budget_between_the_two_tile_sizes_admits_small_and_rejects_large() {
    let small = peak::working_set(3, 256, 256, 256, 256).unwrap().total();
    let large = peak::working_set(3, 8192, 8192, 8192, 8192).unwrap().total();
    let mid = 64 * 1024 * 1024; // 64 MiB
    assert!(small < mid && mid < large);
    assert!(peak::charge_against_budget(mid, 3, 256, 256, 256, 256, "small").is_ok());
    assert!(peak::charge_against_budget(mid, 3, 8192, 8192, 8192, 8192, "large").is_err());
}

// ------------------------------------------------------------------ //
// A huge malicious header: refused before the decoder is invoked.     //
// ------------------------------------------------------------------ //

#[test]
fn huge_header_is_rejected_before_the_decoder_runs() {
    // The patched 8192² header passes the bounded probe (per-side cap 8192),
    // but is not a legal tile of a 240-rect level: it is refused per tile by
    // the contract check with the decoder never invoked (before any
    // multi-GB allocation).
    let good = fixture("cc0-33005-l0.j2k");
    let huge = patch_siz_side(good.clone(), 8192);
    let hdr = jp2k::parse_header(&huge).unwrap();
    assert_eq!((hdr.width, hdr.height), (8192, 8192));
    // first tile is the malicious one; the rest nominal
    let l0 = vec![huge, good.clone(), good.clone(), good.clone()];
    let f = SvsJp2kFixture {
        compression: 33005,
        photometric: 2,
        tile: 240,
        width: 480,
        height: 480,
        downsample: 4,
        mpp: None,
        appmag: None,
        levels: vec![l0, vec![good.clone()], vec![good.clone()], vec![good]],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
    jp2k::decoder_probe::reset();
    let (r, bytes) = convert(&sink.data, SAVER);
    assert!(r.is_err(), "the 8192² first-tile header must be refused");
    assert_eq!(bytes, 0, "refused before any output byte");
    assert_eq!(
        jp2k::decoder_probe::calls(),
        0,
        "the decoder must NOT have been invoked for the huge header"
    );
}

/// A nominal-but-over-budget level: an 8192² tile in an 8192-px grid. The
/// fixture builder only lays out headers (no pixel allocation), so this is
/// cheap even with the huge declared geometry. The plan-time budget check
/// refuses it before the writer or any tile decode.
#[test]
fn oversized_real_tile_is_refused_at_plan_time() {
    let big = patch_siz_side(fixture("cc0-33005-l0.j2k"), 8192);
    // geometry: 8192×8192, downsample 4096 ⇒ exactly one level and one tile
    let f = SvsJp2kFixture {
        compression: 33005,
        photometric: 2,
        tile: 8192,
        width: 8192,
        height: 8192,
        downsample: 4096,
        mpp: None,
        appmag: None,
        levels: vec![vec![big]],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
    jp2k::decoder_probe::reset();
    let (r, bytes) = convert(&sink.data, SAVER);
    assert_eq!(r.unwrap_err(), ErrorCode::ResourceLimitExceeded);
    assert_eq!(bytes, 0);
    assert_eq!(jp2k::decoder_probe::calls(), 0, "plan-time refusal must not decode");
    // the same input under the faster (768 MiB) budget is STILL refused — the
    // peak is ~2.9 GiB, so raising the profile cannot mask it.
    assert_eq!(convert(&sink.data, 768 * 1024 * 1024).0.unwrap_err(), ErrorCode::ResourceLimitExceeded);
}

/// The per-tile gate runs from the REAL codestream header, not just the level
/// contract: a container whose FIRST tile is a smaller 200×256 cropped
/// codestream (the contract) but whose SECOND tile is a full 256×256 nominal
/// codestream passes the plan-time (contract) charge — a cropped tile is
/// CHEAPER than a nominal one — yet the larger second tile must be stopped by
/// the per-tile charge before its allocation. A single-column grid keeps every
/// tile a boundary cell (cropped tiles are only legal on the boundary).
#[test]
fn per_tile_gate_uses_the_real_header_not_only_the_contract() {
    let full = fixture("synth-33003-full.j2k");
    let cropped = fixture("synth-33003-cropped.j2k");
    // 256 wide × 600 tall over 256 px tiles ⇒ 1 column × 3 rows, all boundary.
    let f = SvsJp2kFixture {
        compression: 33003,
        photometric: 2,
        tile: 256,
        width: 256,
        height: 600,
        downsample: 4,
        mpp: None,
        appmag: None,
        levels: vec![
            // tile 0 cropped (the level contract), tiles 1–2 nominal 256×256
            vec![cropped.clone(), full.clone(), full.clone()],
            vec![cropped.clone()],
            vec![cropped],
        ],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();

    // plan-time peak is the CONTRACT (cropped 200×256) — the smaller figure;
    // the nominal 256×256 tile costs more, so only the per-tile charge stops it.
    let contract_peak = peak::working_set(3, 200, 256, 256, 256).unwrap().total();
    let nominal_peak = peak::working_set(3, 256, 256, 256, 256).unwrap().total();
    assert!(nominal_peak > contract_peak, "the nominal tile must cost more than the cropped one");
    // `MemBudget::host` subtracts ADAPTER_RESERVE_BYTES; add it back to place
    // the effective cap strictly between the two peaks.
    let cap = contract_peak + 64 * 1024; // plan passes; nominal tile does not
    assert!(cap < nominal_peak, "cap {cap} must sit below the nominal peak {nominal_peak}");
    let budget = cap + ADAPTER_RESERVE_BYTES;

    jp2k::decoder_probe::reset();
    let (r, bytes) = convert(&sink.data, budget);
    assert_eq!(r.unwrap_err(), ErrorCode::ResourceLimitExceeded);
    // the cropped first tile was decoded; the nominal second tile was refused
    // BEFORE its decode — exactly one decoder invocation.
    assert_eq!(
        jp2k::decoder_probe::calls(),
        1,
        "only the cropped first tile may reach the decoder ({bytes} B out)"
    );
}



