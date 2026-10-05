//! F5 generic tiled JPEG TIFF adapter tests: synthetic generic-TIFF
//! fixtures (generated in code, no sample bytes committed), source-tile
//! passthrough byte equality, generated `l0-box2` tail levels, variant /
//! routing rejections (typed「暂时直传」refusals), budget refusal and
//! resume byte identity.
//!
//! Requires `fixtures` (`cargo test -p slide-transform-core --features
//! fixtures`).

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_gtiff;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::error::{CoreError, CoreResult};
use slide_transform_core::gtiff::{
    self, estimate_gtiff, generated_tail, probe_gtiff, GEN_FINGERPRINT, GEN_MIN_SIDE,
    PYRAMID_METHOD, SOURCE_FORMAT,
};
use slide_transform_core::gtiff_fixture::{
    build_synthetic_gtiff, DescMode, FixtureColor, FixturePattern, GtiffGenParams,
};
use slide_transform_core::io::{ByteSource, MemScratch, MemSink, MemSource, RandomAccessSink};
use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::scn::{classify_description, TiffVendor};
use slide_transform_core::tiff_read;
use slide_transform_core::validate::validate_output;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

fn gen(p: &GtiffGenParams) -> Vec<u8> {
    let mut sink = MemSink::new();
    build_synthetic_gtiff(&mut sink, p).unwrap();
    sink.data
}

fn plan_for(profile: OutputProfile, policy: PixelPolicy) -> TransformPlan {
    let mut plan = TransformPlan::brightfield(InputIdentity::default()).with_policy(policy);
    plan.profile = profile;
    plan
}

fn convert(
    data: &[u8],
    profile: OutputProfile,
) -> Result<(TransformResult, Vec<u8>), CoreError> {
    let src = MemSource::new(data.to_vec());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let r = convert_gtiff::convert_gtiff(
        &src,
        &mut sink,
        &mut scratch,
        &plan_for(profile, PixelPolicy::AllowEdgeReencode),
    )?;
    Ok((r, sink.data))
}

fn convert_with_budget(
    data: &[u8],
    profile: OutputProfile,
    budget: u64,
) -> Result<(TransformResult, Vec<u8>), CoreError> {
    let src = MemSource::new(data.to_vec());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let mut plan = plan_for(profile, PixelPolicy::AllowEdgeReencode);
    plan.limits.memory_budget_bytes = budget;
    let r = convert_gtiff::convert_gtiff(&src, &mut sink, &mut scratch, &plan)?;
    Ok((r, sink.data))
}

fn expect_error(data: Vec<u8>, code: slide_transform_core::error::ErrorCode, frag: &str, name: &str) {
    match convert(&data, OutputProfile::ClassicJpegBigTiff) {
        Err(e) => {
            assert_eq!(
                e.code, code,
                "{name}: got {} ({})",
                e.code.stable_code(),
                e.message
            );
            assert!(e.message.contains(frag), "{name}: {} missing {frag}", e.message);
        }
        Ok(_) => panic!("{name}: expected error {:#?}", code),
    }
}

fn default_params() -> GtiffGenParams {
    GtiffGenParams::default()
}

/// Single-level source (the case the generated tail exists for).
fn single_level_params() -> GtiffGenParams {
    GtiffGenParams { levels: 1, ..default_params() }
}

// --------------------------------------------------------------------------- //
// probe: source levels + generated l0-box2 tail
// --------------------------------------------------------------------------- //

#[test]
fn probe_reports_source_levels_and_the_generated_tail() {
    let p = single_level_params();
    let data = gen(&p);
    let src = MemSource::new(data);
    let doc = probe_gtiff(&src).unwrap();
    assert_eq!(gtiff::SOURCE_FORMAT, "generic-tiled-jpeg-tiff");
    assert_eq!(gtiff::ADAPTER_VERSION, "1");
    assert_eq!(doc.levels.len(), 1, "single source level");
    assert_eq!(doc.levels[0].width, p.width);
    assert_eq!(doc.levels[0].tile_w, p.tile);
    // 520×300 → l0-box2 chain: 260×150, 130×75 (both sides ≤ 256 stop)
    assert_eq!(doc.generated, vec![(260, 150), (130, 75)]);
    // mpp from the fixture's 10 px/cm resolution tags = 1000 µm/px
    let mpp = doc.mpp.expect("mpp from resolution tags");
    assert!((mpp - 1000.0).abs() < 1e-6);
}

#[test]
fn probe_full_pyramid_generates_nothing() {
    let p = default_params(); // 3 source levels: 520×300, 260×150, 130×75
    let data = gen(&p);
    let src = MemSource::new(data);
    let doc = probe_gtiff(&src).unwrap();
    assert_eq!(doc.levels.len(), 3);
    assert!(doc.generated.is_empty(), "last level is below the side cap");
    for w in doc.levels.windows(2) {
        assert!(w[1].width < w[0].width && w[1].height < w[0].height);
    }
}

#[test]
fn generated_tail_follows_the_side_cap() {
    assert_eq!(generated_tail(520, 300), vec![(260, 150), (130, 75)]);
    assert!(generated_tail(256, 256).is_empty(), "at the cap: nothing to add");
    assert_eq!(generated_tail(257, 256), vec![(128, 128)]);
    assert_eq!(generated_tail(46000, 32914).last(), Some(&(179, 128)),
        "the OpenSlide generic-tiff sample convention (9 levels to 179×128)");
    assert!(generated_tail(1, 1).is_empty());
    assert_eq!(PYRAMID_METHOD, "l0-box2");
    assert_eq!(GEN_MIN_SIDE, 256);
}

#[test]
fn big_endian_and_bigtiff_layouts_probe_the_same() {
    for bigtiff in [false, true] {
        for big_endian in [false, true] {
            let p = GtiffGenParams { bigtiff, big_endian, ..single_level_params() };
            let data = gen(&p);
            let doc = probe_gtiff(&MemSource::new(data)).unwrap();
            assert_eq!(doc.levels[0].width, 520, "bigtiff={bigtiff} be={big_endian}");
            assert_eq!(doc.generated.len(), 2);
        }
    }
}

// --------------------------------------------------------------------------- //
// conversion: passthrough + generated levels
// --------------------------------------------------------------------------- //

#[test]
fn classic_source_tiles_are_the_source_bytes_in_order() {
    let p = single_level_params();
    let data = gen(&p);
    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert_eq!(out.source_format, Some(SOURCE_FORMAT));
    assert_eq!(out.adapter_version, Some("1"));
    assert_eq!(out.width, p.width);
    assert!(out.lossy_reencode.is_none());

    // output IFD count = source levels + generated tail
    assert_eq!(out.levels.len(), 3);
    assert_eq!(out.levels[1].width, 260);
    assert_eq!(out.levels[2].width, 130);
    assert!(out.warnings.iter().any(|w| w.starts_with("gtiff_missing_levels_generated")));

    // walk the OUTPUT with the core's own bounded reader and compare every
    // level-0 tile byte-for-byte with the source tile of the same cell
    let src = MemSource::new(data);
    let hdr = tiff_read::read_header(&src).unwrap();
    let chain = tiff_read::ifd_chain(&src, &hdr).unwrap();
    let doc = probe_gtiff(&src).unwrap();
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();
    assert_eq!(ochain.len(), 3, "one IFD per output level");
    let lv = &doc.levels[0];
    let mut s = tiff_read::TileCursor::new(&src, &hdr, &chain[0]).unwrap();
    let mut o = tiff_read::TileCursor::new(&osrc, &ohdr, &ochain[0]).unwrap();
    let mut cell = 0u64;
    loop {
        let sp = s.next_pair().unwrap();
        let op = o.next_pair().unwrap();
        assert_eq!(sp.is_some(), op.is_some());
        let (Some((so, sl)), Some((oo, ol))) = (sp, op) else { break };
        let a = src.read_at(so, sl as usize).unwrap();
        let b = osrc.read_at(oo, ol as usize).unwrap();
        assert_eq!(a, b, "level 0 cell {cell} payload must be verbatim");
        cell += 1;
    }
    assert_eq!(cell, lv.tiles_total);
    assert_eq!(out.levels[0].tiles_raw_copied, lv.tiles_total);
    assert_eq!(out.levels[1].tiles_reencoded, 6, "generated L1 (3×2 grid of 128 px tiles)");
    assert_eq!(out.levels[2].tiles_reencoded, 2, "generated L2 (2×1 grid)");
}

#[test]
fn generated_levels_are_the_box2_average_of_the_previous_level() {
    // gradient content keeps the 4:2:2/DCT re-encode error at the floor, so
    // the assertion pins the COMPOSITION (cell indexing) not the codec
    let p = GtiffGenParams { pattern: FixturePattern::Gradient, ..single_level_params() };
    let data = gen(&p);
    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();

    // decode the committed previous level (output level 0) tile by tile
    let decode_tile =
        |ifd_idx: usize, cell: usize| -> CoreResult<slide_transform_core::jpeg::DecodedImage> {
            let mut cur = tiff_read::TileCursor::new(&osrc, &ohdr, &ochain[ifd_idx]).unwrap();
            let mut i = 0usize;
            while let Some((off, len)) = cur.next_pair()? {
                if i == cell {
                    return slide_transform_core::jpeg::decode_ex(
                        &osrc.read_at(off, len as usize).unwrap(),
                        u64::MAX,
                        false,
                    );
                }
                i += 1;
            }
            Err(CoreError::validation("tile?"))
        };

    assert!(
        out.warnings.iter().any(|w| *w == "gtiff_missing_levels_generated:2"),
        "warnings: {:?}",
        out.warnings
    );

    // level 1 = box2(level 0): compose the expected tile pixels from the
    // decoded previous level and compare against the decoded generated tile
    let lv0 = &out.levels[0];
    let (tw, th) = (128usize, 128usize);
    let gen1 = &out.levels[1];
    for cell in 0..gen1.tiles_total as usize {
        let tx = cell % gen1.tiles_across as usize;
        let ty = cell / gen1.tiles_across as usize;
        // paste the (up to) four previous tiles onto a white canvas
        let mut canvas = vec![255u8; tw * 2 * th * 2 * 3];
        for pty in (ty * 2)..(ty * 2 + 2) {
            for ptx in (tx * 2)..(tx * 2 + 2) {
                if ptx >= lv0.tiles_across as usize || pty >= lv0.tiles_down as usize {
                    continue;
                }
                let img = decode_tile(0, pty * lv0.tiles_across as usize + ptx).unwrap();
                let bx = (ptx - tx * 2) * tw;
                let by = (pty - ty * 2) * th;
                for row in 0..img.height as usize {
                    for col in 0..img.width as usize {
                        let s = (row * img.width as usize + col) * 3;
                        let d = ((by + row) * tw * 2 + bx + col) * 3;
                        canvas[d..d + 3].copy_from_slice(&img.data[s..s + 3]);
                    }
                }
            }
        }
        // 2×2 area-average over the current level's valid region
        let valid_w = tw.min(260 - tx * tw);
        let valid_h = th.min(150 - ty * th);
        let gen_img = decode_tile(1, cell).unwrap();
        let mut total_abs = 0u64;
        let mut max_abs = 0u64;
        for oy in 0..valid_h {
            for ox in 0..valid_w {
                for c in 0..3 {
                    let mut acc = 0u32;
                    for dy in 0..2 {
                        for dx in 0..2 {
                            acc += canvas[((oy * 2 + dy) * tw * 2 + ox * 2 + dx) * 3 + c] as u32;
                        }
                    }
                    let expect = (acc >> 2) as u8;
                    let got = gen_img.data[(oy * tw + ox) * 3 + c];
                    let d = (expect as i32 - got as i32).unsigned_abs() as u64;
                    total_abs += d;
                    max_abs = max_abs.max(d);
                }
            }
        }
        let n = (valid_w * valid_h * 3) as u64;
        assert!(
            total_abs * 100 / n < 100 && max_abs <= 12,
            "generated tile {cell}: mean {:.2} max {max_abs} (encode loss only)",
            total_abs as f64 / n as f64
        );
    }
}

#[test]
fn shared_jpeg_tables_ride_along_and_tiles_stay_verbatim() {
    let p = GtiffGenParams { shared_tables: true, ..single_level_params() };
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_gtiff(&src).unwrap();
    assert!(doc.levels[0].jpeg_tables.is_some(), "fixture wrote tag 347");
    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert_eq!(out.levels[0].tiles_raw_copied, doc.levels[0].tiles_total);
    // byte-for-byte equality of the abbreviated tile streams
    let isrc = MemSource::new(data);
    let ihdr = tiff_read::read_header(&isrc).unwrap();
    let ichain = tiff_read::ifd_chain(&isrc, &ihdr).unwrap();
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();
    let mut s = tiff_read::TileCursor::new(&isrc, &ihdr, &ichain[0]).unwrap();
    let mut o = tiff_read::TileCursor::new(&osrc, &ohdr, &ochain[0]).unwrap();
    while let (Some((so, sl)), Some((oo, ol))) = (s.next_pair().unwrap(), o.next_pair().unwrap()) {
        assert_eq!(
            isrc.read_at(so, sl as usize).unwrap(),
            osrc.read_at(oo, ol as usize).unwrap()
        );
    }
}

#[test]
fn ome_output_profile_converts_and_validates() {
    let p = single_level_params();
    let data = gen(&p);
    let (out, out_bytes) = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).unwrap();
    assert_eq!(out.format, "ome-bigtiff-subifd-rgb-jpeg-pyramid");
    assert_eq!(out.width, p.width);
    assert_eq!(out.levels.len(), 3);
    assert!(out
        .validation
        .checks_passed
        .iter()
        .any(|c| *c == "ome-xml-present"));
    let src = MemSource::new(out_bytes);
    let v = validate_output(&src, src.size(), Some(out.validation.ifd_count)).unwrap();
    assert!(v.checks.iter().any(|c| *c == "subifd-tree"));
}

#[test]
fn mpp_absent_when_resolution_tags_absent() {
    let p = GtiffGenParams { xres: None, ..single_level_params() };
    let data = gen(&p);
    let doc = probe_gtiff(&MemSource::new(data)).unwrap();
    assert_eq!(doc.mpp, None, "nothing is invented");
    let p2 = GtiffGenParams { xres: Some(2.5), ..single_level_params() };
    let doc2 = probe_gtiff(&MemSource::new(gen(&p2))).unwrap();
    assert!((doc2.mpp.unwrap() - 4000.0).abs() < 1e-6, "10 000 / 2.5 px/cm");
}

// --------------------------------------------------------------------------- //
// variant rejections: typed「暂时直传」refusals BEFORE any copy
// --------------------------------------------------------------------------- //

#[test]
fn stripped_variant_is_a_typed_rejection() {
    let data = gen(&GtiffGenParams { stripped: true, ..default_params() });
    expect_error(data, UnsupportedKfbVariant, "带状存储", "stripped");
}

#[test]
fn deflate_and_lzw_variants_are_typed_rejections() {
    let d = gen(&GtiffGenParams { deflate: true, ..default_params() });
    expect_error(d, UnsupportedKfbVariant, "deflate", "deflate");
    let l = gen(&GtiffGenParams { lzw: true, ..default_params() });
    expect_error(l, UnsupportedKfbVariant, "LZW", "lzw");
}

#[test]
fn jpeg2000_marker_is_a_typed_rejection() {
    // hand-built: compression 33005 with tiled tags (the fixture cannot
    // express JP2K payloads; the refusal fires at the compression check)
    let data = hand_bigtiff(33005, 3, 8, 6, 1, false, 520, 300, None, "");
    expect_error(data, UnsupportedKfbVariant, "JPEG 2000", "jp2k");
}

#[test]
fn gray_and_16bit_and_planar_variants_are_typed_rejections() {
    let g = gen(&GtiffGenParams { gray: true, ..default_params() });
    expect_error(g, UnsupportedKfbVariant, "SamplesPerPixel=1", "gray");
    let b = gen(&GtiffGenParams { bits16: true, ..default_params() });
    expect_error(b, UnsupportedKfbVariant, "BitsPerSample", "bits16");
    let p = gen(&GtiffGenParams { planar2: true, ..default_params() });
    expect_error(p, UnsupportedKfbVariant, "PlanarConfiguration=2", "planar2");
}

#[test]
fn mixed_tile_geometry_is_a_typed_rejection() {
    let data = gen(&GtiffGenParams { tile_mismatch: true, ..default_params() });
    expect_error(data, UnsupportedKfbVariant, "tile 尺寸不一致", "tile-mismatch");
}

#[test]
fn vendor_descriptions_route_away_and_are_refused_here() {
    // OME-TIFF / converter BigTIFF are not conversion inputs at all
    let ome = gen(&GtiffGenParams { desc_mode: DescMode::Ome, ..single_level_params() });
    expect_error(ome, UnsupportedKfbVariant, "OME-TIFF 不是转换输入", "ome");
    let conv = gen(&GtiffGenParams { desc_mode: DescMode::Converter, ..single_level_params() });
    expect_error(conv, UnsupportedKfbVariant, "不是转换输入", "converter");
    // vendor files must not enter the generic adapter (defence in depth)
    let ap = gen(&GtiffGenParams { desc_mode: DescMode::Aperio, ..single_level_params() });
    expect_error(ap, UnsupportedKfbVariant, "不走通用 TIFF 适配器", "aperio");
    let scn = gen(&GtiffGenParams { desc_mode: DescMode::ScnXml, ..single_level_params() });
    expect_error(scn, UnsupportedKfbVariant, "不走通用 TIFF 适配器", "scn-xml");
}

#[test]
fn foreign_plain_description_is_the_accept_case() {
    // the generic family is DEFINED by an unnamed/foreign description with
    // convertible structure
    let data = gen(&GtiffGenParams { desc_mode: DescMode::Foreign, ..single_level_params() });
    let (out, _) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert_eq!(out.levels.len(), 3);
    assert_eq!(out.levels[0].tiles_raw_copied, out.levels[0].tiles_total);
}

#[test]
fn zero_tile_entries_are_rejected_not_filled() {
    // the generic family has no sparse-grid convention: hand-build a file
    // whose second tile is (0, 0)
    let mut data = gen(&single_level_params());
    // find the TileOffsets array of IFD 0 through the reader and zero the
    // second entry's stored offset+count pair
    let src = MemSource::new(data.clone());
    let hdr = tiff_read::read_header(&src).unwrap();
    let chain = tiff_read::ifd_chain(&src, &hdr).unwrap();
    let e = chain[0].find(324).unwrap().clone();
    let arr_off = if hdr.kind == tiff_read::TiffKind::BigTiff {
        u64::from_le_bytes(e.val.try_into().unwrap())
    } else {
        u32::from_le_bytes(e.val[..4].try_into().unwrap()) as u64
    };
    drop(src);
    let little = data[0] == b'I';
    let put = |buf: &mut Vec<u8>, at: usize, v: u64| {
        let b = if little {
            (v as u32).to_le_bytes()
        } else {
            (v as u32).to_be_bytes()
        };
        buf[at..at + 4].copy_from_slice(&b);
    };
    // entry 1 lives 8 bytes (LONG pair: offset+count) into the offsets array
    put(&mut data, (arr_off + 8) as usize, 0);
    // and the same index in the byte-counts array
    let ce = {
        let src = MemSource::new(data.clone());
        let hdr = tiff_read::read_header(&src).unwrap();
        let chain = tiff_read::ifd_chain(&src, &hdr).unwrap();
        let e = chain[0].find(325).unwrap().clone();
        if hdr.kind == tiff_read::TiffKind::BigTiff {
            u64::from_le_bytes(e.val.try_into().unwrap())
        } else {
            u32::from_le_bytes(e.val[..4].try_into().unwrap()) as u64
        }
    };
    put(&mut data, (ce + 8) as usize, 0);
    expect_error(data, TilePayloadOutOfBounds, "为 0", "zero-tile");
}

// --------------------------------------------------------------------------- //
// budget: charged BEFORE allocation
// --------------------------------------------------------------------------- //

#[test]
fn tiny_budget_is_a_typed_refusal_before_allocation() {
    let data = gen(&single_level_params());
    let r = convert_with_budget(&data, OutputProfile::ClassicJpegBigTiff, 64 * 1024);
    match r {
        Err(e) => assert_eq!(
            e.code,
            ResourceLimitExceeded,
            "got {} ({})",
            e.code.stable_code(),
            e.message
        ),
        Ok(_) => panic!("expected resource_profile_insufficient"),
    }
    let ok = convert_with_budget(&data, OutputProfile::ClassicJpegBigTiff, 192 * 1024 * 1024);
    assert!(ok.is_ok(), "{:?}", ok.err().map(|e| e.message));
}

// --------------------------------------------------------------------------- //
// estimate
// --------------------------------------------------------------------------- //

#[test]
fn estimate_accounts_generated_tiles() {
    let p = single_level_params();
    let doc = probe_gtiff(&MemSource::new(gen(&p))).unwrap();
    let est = estimate_gtiff(&doc);
    assert_eq!(est.ifds, 3, "source + generated");
    assert_eq!(est.cells_total, doc.levels[0].tiles_total + 6 + 2);
    assert!(est.output_upper_bound_bytes > est.payload_bytes);
    assert!(est.compact_upper_bound_bytes >= est.output_upper_bound_bytes);
    assert_eq!(GEN_FINGERPRINT, "gtiff-l0-box2:q96:y422:hstd:v1");
}

// --------------------------------------------------------------------------- //
// resume: byte-identical after a mid-generated-level crash
// --------------------------------------------------------------------------- //

struct Collector {
    states: Mutex<Vec<CheckpointState>>,
    stop_after: Option<usize>,
    cancel: CancelFlag,
    count: AtomicUsize,
}

impl slide_transform_core::job::CheckpointCallback for Collector {
    fn on_checkpoint(&self, c: &CheckpointState) {
        let n = self.count.fetch_add(1, Ordering::SeqCst) + 1;
        self.states.lock().unwrap().push(c.clone());
        if let Some(k) = self.stop_after {
            if n >= k {
                self.cancel.cancel();
            }
        }
    }
}

fn sha256(data: &[u8]) -> String {
    use sha2::Digest;
    let mut h = sha2::Sha256::new();
    h.update(data);
    format!("{:x}", h.finalize())
}

#[test]
fn resume_refuses_a_foreign_or_absent_adapter_version() {
    let data = gen(&single_level_params());
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    for version in [Some("2".to_string()), None] {
        let src = MemSource::new(data.clone());
        let mut sink = MemSink::new();
        let mut scratch = MemScratch::default();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let rp = slide_transform_core::resume::ResumePoint {
            level: 0,
            channel: 0,
            cell: 0,
            committed_output: 0,
            ifd_tiles: vec![],
            adapter_version: version.clone(),
        };
        let e = convert_gtiff::convert_gtiff_to_bigtiff_resume(
            &src, &mut sink, &mut scratch, &plan, &job, &rp,
        )
        .err()
        .expect("version-mismatched resume must refuse");
        assert_eq!(e.code, slide_transform_core::error::ErrorCode::ConversionValidationFailed);
        assert!(e.message.contains("适配器"), "{}", e.message);
    }
}

#[test]
fn resume_from_a_crash_inside_a_generated_level_is_byte_identical() {
    use slide_transform_core::io::{FileScratch, FileSink, FileSource};
    use slide_transform_core::resume::ResumePoint;

    let data = gen(&single_level_params()); // 1 source level + 2 generated

    let dir = std::env::temp_dir().join(format!(
        "stc5-gtiff-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.tiff");
    std::fs::write(&src_path, &data).unwrap();
    let plan = || plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    let src = || FileSource::open(&src_path).unwrap();

    // reference run
    let ref_out = dir.join("ref.tif");
    let mut scratch = FileScratch::new(&dir);
    {
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_gtiff::convert_gtiff_to_bigtiff(&src(), &mut out, &mut scratch, &plan(), &job)
            .unwrap();
        out.flush().unwrap();
        let v = validate_output(&FileSource::open(&ref_out).unwrap(), r.output_bytes, None).unwrap();
        assert_eq!(v.ifd_count as u32, r.validation.ifd_count);
    }
    let ref_sha = sha256(&std::fs::read(&ref_out).unwrap());

    // crashed run: cancel from inside checkpoint 4 (inside generated level 1:
    // level 0 has 3 tile rows, so checkpoints 1-3 sit in level 0)
    let crash_dir = dir.join("crash");
    std::fs::create_dir_all(&crash_dir).unwrap();
    let part_out = dir.join("part.tif");
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: Some(4),
        cancel: cancel.clone(),
        count: AtomicUsize::new(0),
    };
    let mut scratch2 = FileScratch::new(&crash_dir);
    {
        let mut out = FileSink::create(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
        let res = convert_gtiff::convert_gtiff_to_bigtiff(&src(), &mut out, &mut scratch2, &plan(), &job);
        match res {
            Err(e) => assert!(e.message.contains("已取消"), "unexpected err {e:?}"),
            Ok(_) => panic!("expected the injected cancel to abort"),
        }
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
    assert!(!states.is_empty(), "no checkpoints recorded");
    let last = states.last().unwrap();
    assert!(
        last.level >= 1,
        "the crash must sit inside a GENERATED level for this test to mean anything (got level {})",
        last.level
    );
    let rp = ResumePoint {
        level: last.level as usize,
        channel: last.channel.unwrap_or(0),
        cell: last.cell_done,
        committed_output: last.committed_output,
        ifd_tiles: last.ifd_tiles.clone(),
        adapter_version: Some(gtiff::ADAPTER_VERSION.to_string()),
    };
    // crash aftermath the host guarantees: output + offcnt scratch truncated
    {
        let f = std::fs::OpenOptions::new().write(true).open(&part_out).unwrap();
        f.set_len(rp.committed_output).unwrap();
    }
    for (i, &tiles) in rp.ifd_tiles.iter().enumerate() {
        let p = crash_dir.join(format!(".kfb2tiff-scratch-offcnt-l{i}"));
        if p.exists() {
            let f = std::fs::OpenOptions::new().write(true).open(&p).unwrap();
            f.set_len(tiles * 12).unwrap();
        }
    }

    // resume run: the same scratch factory keeps the surviving offcnt files
    let resumed;
    {
        let mut out = FileSink::open_preserve(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_gtiff::convert_gtiff_to_bigtiff_resume(
            &src(),
            &mut out,
            &mut scratch2,
            &plan(),
            &job,
            &rp,
        )
        .unwrap();
        out.flush().unwrap();
        assert_eq!(r.output_bytes, std::fs::metadata(&part_out).unwrap().len());
        resumed = r;
    }
    assert_eq!(
        sha256(&std::fs::read(&part_out).unwrap()),
        ref_sha,
        "resumed bytes differ"
    );
    assert_eq!(resumed.levels.len(), 3);
    assert_eq!(resumed.levels[1].tiles_reencoded, 6);
    assert_eq!(resumed.levels[2].tiles_reencoded, 2);
}

// --------------------------------------------------------------------------- //
// vendor routing classifier interplay
// --------------------------------------------------------------------------- //

#[test]
fn routing_sends_the_generic_container_to_this_adapter() {
    // the vendor classifier's Unknown is THIS adapter's route; the vendor
    // ids keep their meanings
    assert_eq!(classify_description(""), TiffVendor::Unknown);
    assert_eq!(classify_description("Some Other Scanner v1"), TiffVendor::Unknown);
    assert_eq!(classify_description("Aperio ..."), TiffVendor::AperioSvs);
    // the converter's own generic-TIFF output is recognised as such
    assert_eq!(
        classify_description(
            "{\"adapter\": \"generic-tiled-jpeg-tiff\", \"source_format\": \"generic-tiled-jpeg-tiff\"}\u{0}"
        ),
        TiffVendor::ConverterBigTiff
    );
    assert!(slide_transform_core::scn::CONVERTER_SOURCE_FORMATS
        .contains(&"generic-tiled-jpeg-tiff"));
}

/// Minimal little-endian BigTIFF with one tiled IFD and explicit
/// compression/samples/bits/planar — for the hand-built variant rejections
/// the fixture knobs cannot express.
#[allow(clippy::too_many_arguments)]
fn hand_bigtiff(
    compression: u16,
    samples: u16,
    _bits: u16,
    photo: u16,
    planar: u16,
    stripped: bool,
    width: u32,
    height: u32,
    jpeg_tables: Option<&str>,
    desc: &str,
) -> Vec<u8> {
    use slide_transform_core::jpeg::{encode_rgb, EncoderCfg, Sampling};
    let desc = desc.as_bytes();
    let payload = if !stripped {
        Some(
            encode_rgb(
                &[255u8; 16 * 16 * 3],
                16,
                16,
                &EncoderCfg::with_quality(90, Sampling::S444),
            )
            .unwrap(),
        )
    } else {
        None
    };
    let payload_at = 16 + desc.len() as u64 + 1;
    let mut entries: Vec<(u16, u16, u64, Vec<u8>)> = vec![
        (256, 4, 1, width.to_le_bytes().to_vec()),
        (257, 4, 1, height.to_le_bytes().to_vec()),
        (259, 3, 1, compression.to_le_bytes().to_vec()),
        (262, 3, 1, photo.to_le_bytes().to_vec()),
        (270, 2, desc.len() as u64 + 1, 16u64.to_le_bytes().to_vec()),
        (277, 3, 1, samples.to_le_bytes().to_vec()),
        (284, 3, 1, planar.to_le_bytes().to_vec()),
    ];
    if stripped {
        entries.push((273, 16, 1, payload_at.to_le_bytes().to_vec()));
        entries.push((278, 4, 1, height.to_le_bytes().to_vec()));
        entries.push((279, 16, 1, (payload.as_ref().map_or(0u64, |p| p.len() as u64)).to_le_bytes().to_vec()));
    } else if let Some(pl) = &payload {
        entries.push((322, 3, 1, 16u16.to_le_bytes().to_vec()));
        entries.push((323, 3, 1, 16u16.to_le_bytes().to_vec()));
        entries.push((324, 16, 1, payload_at.to_le_bytes().to_vec()));
        entries.push((325, 16, 1, (pl.len() as u64).to_le_bytes().to_vec()));
    }
    if let Some(t) = jpeg_tables {
        let tb = t.as_bytes();
        let t_at = payload_at + payload.as_ref().map_or(0u64, |p| p.len() as u64) + 1;
        entries.push((347, 2, tb.len() as u64 + 1, t_at.to_le_bytes().to_vec()));
    }
    entries.sort_by_key(|e| e.0);
    let ifd_at =
        payload_at + payload.as_ref().map_or(0u64, |p| p.len() as u64) + jpeg_tables.map_or(0u64, |t| t.len() as u64 + 1);
    let mut buf: Vec<u8> = Vec::new();
    buf.extend_from_slice(b"II");
    buf.extend_from_slice(&43u16.to_le_bytes());
    buf.extend_from_slice(&8u16.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&ifd_at.to_le_bytes());
    buf.extend_from_slice(desc);
    buf.push(0);
    if let Some(pl) = payload {
        buf.extend_from_slice(&pl);
    }
    if let Some(t) = jpeg_tables {
        buf.push(0);
        buf.extend_from_slice(t.as_bytes());
        buf.push(0);
    }
    buf.extend_from_slice(&(entries.len() as u64).to_le_bytes());
    for (tag, typ, count, val) in &entries {
        buf.extend_from_slice(&tag.to_le_bytes());
        buf.extend_from_slice(&typ.to_le_bytes());
        buf.extend_from_slice(&count.to_le_bytes());
        let mut v = val.clone();
        v.resize(8, 0);
        buf.extend_from_slice(&v);
    }
    buf.extend_from_slice(&0u64.to_le_bytes());
    buf
}

#[test]
fn hand_built_lzw_gray_and_planar_refuse() {
    for (data, frag) in [
        (hand_bigtiff(5, 3, 8, 6, 1, false, 520, 300, None, ""), "LZW"),
        (hand_bigtiff(8, 3, 8, 6, 1, false, 520, 300, None, ""), "deflate"),
        (hand_bigtiff(7, 1, 8, 1, 1, false, 520, 300, None, ""), "SamplesPerPixel=1"),
        (hand_bigtiff(7, 3, 8, 6, 2, false, 520, 300, None, ""), "PlanarConfiguration=2"),
        (hand_bigtiff(7, 3, 8, 6, 1, true, 520, 300, None, ""), "带状存储"),
        (
            hand_bigtiff(7, 3, 8, 6, 1, false, 520, 300, None,
                "OME-TIFF probe text that is not xml"),
            "",
        ), // plain non-vendor desc with valid structure converts → covered elsewhere
    ] {
        if frag.is_empty() {
            continue;
        }
        let src = MemSource::new(data);
        let e = probe_gtiff(&src).unwrap_err();
        assert_eq!(e.code, UnsupportedKfbVariant, "{frag}: {}", e.message);
        assert!(e.message.contains(frag), "{}: {}", frag, e.message);
    }
}

// --------------------------------------------------------------------------- //
// 审查回归：非方形 tile（tile_h > tile_w）
// --------------------------------------------------------------------------- //

#[test]
fn non_square_tiles_probe_and_convert_without_panic() {
    // 复现（审查 #1）：probe 接受 tile 128×256 的合法 TIFF，转换在 l0-box2
    // 合成时 canvas 越界 panic（`range end index … out of range`）——probe
    // 判定与转换崩溃自相矛盾。合成画布按 (2·tile_w)×(2·tile_h) 计。
    let p = GtiffGenParams {
        tile: 128,
        tile_h: Some(256),
        levels: 1,
        xres: None,
        ..default_params()
    };
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_gtiff(&src).unwrap();
    assert_eq!(doc.levels[0].tile_w, 128);
    assert_eq!(doc.levels[0].tile_h, 256);
    assert_eq!(doc.levels[0].tiles_total, 5 * 2, "520/128=5 × ceil(300/256)=2");
    assert_eq!(doc.generated, vec![(260, 150), (130, 75)]);

    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert_eq!(out.width, p.width);
    // L0 tile 仍原样搬运
    let isrc = MemSource::new(data);
    let ihdr = tiff_read::read_header(&isrc).unwrap();
    let ichain = tiff_read::ifd_chain(&isrc, &ihdr).unwrap();
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();
    let mut s = tiff_read::TileCursor::new(&isrc, &ihdr, &ichain[0]).unwrap();
    let mut o = tiff_read::TileCursor::new(&osrc, &ohdr, &ochain[0]).unwrap();
    let mut cell = 0u64;
    while let (Some((so, sl)), Some((oo, ol))) = (s.next_pair().unwrap(), o.next_pair().unwrap()) {
        assert_eq!(
            isrc.read_at(so, sl as usize).unwrap(),
            osrc.read_at(oo, ol as usize).unwrap(),
            "level 0 cell {cell} must be verbatim"
        );
        cell += 1;
    }
    // 生成层几何：tile 128×256 → L1 (260×150) = 3×1、L2 (130×75) = 2×1
    assert_eq!(out.levels[1].tiles_across, 3);
    assert_eq!(out.levels[1].tiles_down, 1);
    assert_eq!(out.levels[1].tiles_total, 3);
    assert_eq!(out.levels[2].tiles_total, 2);
}

#[test]
fn non_square_generated_tiles_match_the_box2_average() {
    // 非方形 tile 下生成层的像素内容也要对：256×512 画布、行距 2·tile_w
    let p = GtiffGenParams {
        tile: 128,
        tile_h: Some(256),
        levels: 1,
        xres: None,
        pattern: FixturePattern::Gradient,
        ..default_params()
    };
    let data = gen(&p);
    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();
    let decode_tile = |ifd_idx: usize, cell: usize| -> CoreResult<slide_transform_core::jpeg::DecodedImage> {
        let mut cur = tiff_read::TileCursor::new(&osrc, &ohdr, &ochain[ifd_idx]).unwrap();
        let mut i = 0usize;
        while let Some((off, len)) = cur.next_pair()? {
            if i == cell {
                return slide_transform_core::jpeg::decode_ex(
                    &osrc.read_at(off, len as usize).unwrap(),
                    u64::MAX,
                    false,
                );
            }
            i += 1;
        }
        Err(CoreError::validation("tile?"))
    };
    let lv0 = &out.levels[0];
    let (tw, th) = (128usize, 256usize);
    let gen1 = &out.levels[1];
    for cell in 0..gen1.tiles_total as usize {
        let tx = cell % gen1.tiles_across as usize;
        let ty = cell / gen1.tiles_across as usize;
        let mut canvas = vec![255u8; tw * 2 * th * 2 * 3];
        for pty in (ty * 2)..(ty * 2 + 2) {
            for ptx in (tx * 2)..(tx * 2 + 2) {
                if ptx >= lv0.tiles_across as usize || pty >= lv0.tiles_down as usize {
                    continue;
                }
                let img = decode_tile(0, pty * lv0.tiles_across as usize + ptx).unwrap();
                let bx = (ptx - tx * 2) * tw;
                let by = (pty - ty * 2) * th;
                for row in 0..img.height as usize {
                    for col in 0..img.width as usize {
                        let s = (row * img.width as usize + col) * 3;
                        let d = ((by + row) * tw * 2 + bx + col) * 3;
                        canvas[d..d + 3].copy_from_slice(&img.data[s..s + 3]);
                    }
                }
            }
        }
        let valid_w = tw.min(260 - tx * tw);
        let valid_h = th.min(150 - ty * th);
        let gen_img = decode_tile(1, cell).unwrap();
        let mut total_abs = 0u64;
        let mut max_abs = 0u64;
        for oy in 0..valid_h {
            for ox in 0..valid_w {
                for c in 0..3 {
                    let mut acc = 0u32;
                    for dy in 0..2 {
                        for dx in 0..2 {
                            acc += canvas[((oy * 2 + dy) * tw * 2 + ox * 2 + dx) * 3 + c] as u32;
                        }
                    }
                    let d = ((acc >> 2) as i32 - gen_img.data[(oy * tw + ox) * 3 + c] as i32)
                        .unsigned_abs() as u64;
                    total_abs += d;
                    max_abs = max_abs.max(d);
                }
            }
        }
        let n = (valid_w * valid_h * 3) as u64;
        assert!(
            total_abs * 100 / n < 100 && max_abs <= 12,
            "generated tile {cell}: mean {:.2} max {max_abs}",
            total_abs as f64 / n as f64
        );
    }
}

// --------------------------------------------------------------------------- //
// 审查回归：ICC 带入输出且 WARN_NO_ICC 条件化
// --------------------------------------------------------------------------- //

#[test]
fn icc_profile_is_carried_and_the_warning_is_conditional() {
    // 复现（审查 #2/#3）：源带 ICC（tag 34675）时警告仍无条件推
    // color_management_not_applied，与「ICC 已带入输出」矛盾；
    // 且该行为此前零测试。
    let with_icc = GtiffGenParams { icc: true, ..single_level_params() };
    let data = gen(&with_icc);
    let src = MemSource::new(data.clone());
    let doc = probe_gtiff(&src).unwrap();
    assert!(doc.icc.is_some(), "fixture wrote tag 34675 on IFD 0");
    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert!(
        !out.warnings.iter().any(|w| w == "color_management_not_applied"),
        "带 ICC 的源不得再报 color_management_not_applied: {:?}",
        out.warnings
    );
    // ICC 确实带到输出 level 0
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();
    let icc_entry = ochain[0].find(34675);
    assert!(icc_entry.is_some(), "output IFD 0 must carry tag 34675");
    assert_eq!(
        tiff_read::entry_value(&osrc, &ohdr, icc_entry.unwrap()).unwrap(),
        doc.icc.unwrap(),
        "output ICC bytes must be the source's, verbatim"
    );
    // OME profile 同样携带
    let (ome_out, _) = convert(&gen(&with_icc), OutputProfile::OmeBigTiffRgbSubifd).unwrap();
    assert!(!ome_out.warnings.iter().any(|w| w == "color_management_not_applied"));

    // 无 ICC 的源维持原警告
    let without = GtiffGenParams { icc: false, ..single_level_params() };
    let (out2, _) = convert(&gen(&without), OutputProfile::ClassicJpegBigTiff).unwrap();
    assert!(out2.warnings.iter().any(|w| w == "color_management_not_applied"));
}
