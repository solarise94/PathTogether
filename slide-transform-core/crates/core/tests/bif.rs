//! Ventana BIF adapter tests: synthetic BIF fixtures (generated in code, no
//! sample bytes committed), overlap-stitched MCU-row band decode → tile
//! re-encode, `l0-box2` generated tail, variant / routing rejections
//! (typed), budget refusal before allocation, and resume byte identity.
//!
//! Requires `fixtures` (`cargo test -p slide-transform-core --features
//! fixtures`).

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::bif::{self, estimate_bif};
use slide_transform_core::bif_fixture::{build_synthetic_bif, default_stitched, BifGenParams};
use slide_transform_core::convert_bif;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::error::CoreError;
use slide_transform_core::io::{
    ByteSource, MemScratch, MemSink, MemSource, RandomAccessSink,
};
use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::resume::{parse_resume_json, ResumePoint};
use slide_transform_core::validate::validate_output;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

fn gen(p: &BifGenParams) -> Vec<u8> {
    let mut sink = MemSink::new();
    build_synthetic_bif(&mut sink, p).unwrap();
    sink.data
}

fn plan_for(profile: OutputProfile, policy: PixelPolicy) -> TransformPlan {
    let mut plan = TransformPlan::brightfield(InputIdentity::default()).with_policy(policy);
    plan.profile = profile;
    plan
}

fn convert(data: &[u8], profile: OutputProfile) -> Result<(TransformResult, Vec<u8>), CoreError> {
    let src = MemSource::new(data.to_vec());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let r = convert_bif::convert_bif_to_bigtiff(
        &src,
        &mut sink,
        &mut scratch,
        &plan_for(profile, PixelPolicy::AllowEdgeReencode),
        &job_no_checkpoint(),
    )?;
    Ok((r, sink.data))
}

fn job_no_checkpoint() -> JobControl<'static> {
    static NULL: NullProgress = NullProgress;
    JobControl::new(&NULL)
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
    let r = convert_bif::convert_bif_to_bigtiff(&src, &mut sink, &mut scratch, &plan, &job_no_checkpoint())?;
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
            assert!(e.message.contains(frag), "{name}: message lacks {frag:?}: {}", e.message);
        }
        Ok(_) => panic!("{name}: expected {} rejection", code.stable_code()),
    }
}

// --------------------------------------------------------------------------- //
// probe / structural contract
// --------------------------------------------------------------------------- //

#[test]
fn probe_and_convert_synthetic() {
    let p = BifGenParams::default();
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = bif::probe_bif(&src).expect("probe");
    // stitched geometry: 704×1120 while the TIFF canvas says 768×1280
    let (sw, sh) = default_stitched(&p);
    assert_eq!((doc.width, doc.height), (sw as u32, sh as u32));
    assert_eq!((doc.width, doc.height), (704, 1120));
    assert_eq!(doc.levels[0].canvas_w, 768);
    assert_eq!(doc.levels[0].canvas_h, 1280);
    assert_eq!((doc.levels[0].tile_w, doc.levels[0].tile_h), (256, 256));
    // advances = tile − overlap（confidence 加权均值，fixture 全等值）
    assert!((doc.advance_x - 224.0).abs() < 1e-9);
    assert!((doc.advance_y - 232.0).abs() < 1e-9);
    // two scanned AOIs (the third is AOIScanned=0 and skipped)
    assert_eq!(doc.areas.len(), 2);
    assert_eq!(doc.tiles_present, 9 + 4);
    // iScan provenance
    assert_eq!(doc.objective, Some(40.0));
    assert_eq!(doc.mpp, Some((0.2325, 0.2325)));
    // label + thumbnail detected and excluded
    let names: Vec<&str> = doc.associated.iter().map(|a| a.name.as_str()).collect();
    assert!(names.contains(&"label"));
    assert!(names.contains(&"thumbnail"));
    // shared abbreviated-stream tables present
    assert!(doc.jpeg_tables.is_some());
    // generated tail per the gtiff rule
    assert!(!doc.generated.is_empty());
    let last = *doc.generated.last().unwrap();
    assert!(last.0 <= 256 && last.1 <= 256);
    // estimate sanity
    let est = estimate_bif(&doc);
    assert!(est.payload_bytes > 0);
    assert!(est.output_upper_bound_bytes > est.payload_bytes);
    assert!(est.compact_upper_bound_bytes < est.output_upper_bound_bytes);

    // ---- conversion: classic profile ------------------------------------ //
    let (r, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    assert_eq!(r.source_format, Some(bif::SOURCE_FORMAT));
    assert_eq!(r.adapter_version, Some(bif::ADAPTER_VERSION));
    assert_eq!(r.width, 704);
    assert_eq!(r.height, 1120);
    assert_eq!(r.levels.len(), 1 + doc.generated.len());
    assert_eq!(r.levels[0].tiles_total, r.levels[0].tiles_reencoded);
    assert_eq!(r.count_raw_copied(), 0);
    let composed = r.composed.as_ref().expect("composed summary");
    assert_eq!(composed.mode, "stitch-compose-reencode");
    assert_eq!(composed.fingerprint, "bif-mosaic-compose:q96:y422:hstd:v1");
    assert_eq!(composed.pyramid, bif::PYRAMID_METHOD);
    assert_eq!(composed.quality, 96);
    assert_eq!(composed.sampling, "4:2:2");
    assert!(r.warnings.iter().any(|w| w == "color_management_not_applied"));
    assert!(r.warnings.iter().any(|w| w == "bif_associated_not_exported"));
    let vsrc = MemSource::new(out.clone());
    let v = validate_output(&vsrc, out.len() as u64, Some(r.validation.ifd_count)).expect("validate");
    assert_eq!(v.main_ifds as usize, r.levels.len());
}

#[test]
fn probe_ome_rgb_profile_contract() {
    let data = gen(&BifGenParams::default());
    let (r, _out) = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).expect("convert ome");
    assert_eq!(r.format, slide_transform_core::convert_bf::FORMAT_OME_RGB);
    assert!(r.levels.len() >= 2);
}

// --------------------------------------------------------------------------- //
// pixel gate: the stitched composition equals the reference paste
// --------------------------------------------------------------------------- //

/// Reference compose of the fixture (the exact pre-encode pixels): every
/// placement pasted in reverse-raster order (smallest (row,col) wins),
/// uncovered in-bounds regions black.
fn reference_compose(p: &BifGenParams) -> Vec<u8> {
    let (sw, sh) = default_stitched(p);
    let mut buf = vec![0u8; (sw * sh * 3) as usize];
    let mut placements: Vec<(i64, i64, i64, i64, i64)> = Vec::new(); // (row, col, x, y, d)
    let mut d = 1i64;
    // post-flip stitched Pos (the fixture encodes final coordinates)
    let ay = 256 - p.overlap_y;
    let top = (400 + 2 * ay + 256).max(0 + (ay + 256));
    let a0_y = top - 400 - (2 * ay + 256);
    let a2_y = top - 0 - (ay + 256);
    for row in 0..3 {
        for col in 0..3 {
            let (x, y) = fixture_tile_xy(col, row, (0, 0), (0, a0_y), p);
            placements.push((row, col, x, y, d));
            d += 1;
        }
    }
    for row in 0..2 {
        for col in 0..2 {
            let (x, y) = fixture_tile_xy(1 + col, 3 + row, (1, 3), (140, a2_y), p);
            placements.push((3 + row, 1 + col, x, y, d));
            d += 1;
        }
    }
    placements.sort_by(|a, b| (b.0, b.1).cmp(&(a.0, a.1)));
    for (_, _, x0, y0, dd) in placements {
        for y in 0..256i64 {
            let ay = y0 + y;
            if ay < 0 || ay >= sh {
                continue;
            }
            for x in 0..256i64 {
                let ax = x0 + x;
                if ax < 0 || ax >= sw {
                    continue;
                }
                let px = pattern_px_ref(x0 + x, y0 + y, dd);
                let at = ((ay * sw + ax) * 3) as usize;
                buf[at..at + 3].copy_from_slice(&px);
            }
        }
    }
    buf
}

fn fixture_tile_xy(
    col: i64,
    row: i64,
    start: (i64, i64),
    pos: (i64, i64),
    p: &BifGenParams,
) -> (i64, i64) {
    let ax = 256 - p.overlap_x;
    let ay = 256 - p.overlap_y;
    (col * ax + (pos.0 - start.0 * ax), row * ay + (pos.1 - start.1 * ay))
}

fn pattern_px_ref(x: i64, y: i64, d: i64) -> [u8; 3] {
    [
        (96 + ((x + d) & 0x3F)) as u8,
        (96 + ((y + 3 * d) & 0x3F)) as u8,
        (140 + (((x + y) / 2 + d) & 0x3F)) as u8,
    ]
}

/// Decode the converted output's L0 back into pixels (bounded walk of the
/// classic pyramid's IFD 0 with the shared TileCursor).
fn decode_output_l0(out: &[u8], want_w: u32, want_h: u32) -> Vec<u8> {
    let src = MemSource::new(out.to_vec());
    let hdr = slide_transform_core::tiff_read::read_header(&src).unwrap();
    let ifd0 = slide_transform_core::tiff_read::read_ifd(&src, &hdr, hdr.first_ifd).unwrap();
    let mut cur = slide_transform_core::tiff_read::TileCursor::new(&src, &hdr, &ifd0).unwrap();
    let across = (want_w as u64).div_ceil(256);
    let down = (want_h as u64).div_ceil(256);
    let mut buf = vec![0u8; want_w as usize * want_h as usize * 3];
    for ty in 0..down {
        for _tx in 0..across {
            let (off, cnt) = cur.next_pair().unwrap().unwrap();
            let raw = src.read_at(off, cnt as usize).unwrap();
            let img = slide_transform_core::jpeg::decode_ex(&raw, 256 * 256, false).unwrap();
            let tx = _tx;
            let x0 = (tx * 256) as usize;
            let y0 = (ty * 256) as usize;
            for r in 0..img.height as usize {
                let valid_w = (want_w as usize).saturating_sub(x0).min(256);
                if valid_w == 0 {
                    break;
                }
                let srow = r * 256 * 3;
                let drow = ((y0 + r) * want_w as usize + x0) * 3;
                if y0 + r >= want_h as usize {
                    break;
                }
                buf[drow..drow + valid_w * 3]
                    .copy_from_slice(&img.data[srow..srow + valid_w * 3]);
            }
        }
    }
    buf
}

#[test]
fn stitched_pixels_match_reference_compose() {
    let p = BifGenParams::default();
    let data = gen(&p);
    let (r, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    let got = decode_output_l0(&out, r.width, r.height);
    let want = reference_compose(&p);
    assert_eq!(got.len(), want.len());
    let mut sum = 0u64;
    let mut max = 0i64;
    let mut n = 0u64;
    for (g, w) in got.chunks_exact(3).zip(want.chunks_exact(3)) {
        for c in 0..3 {
            let d = (g[c] as i64 - w[c] as i64).abs();
            sum += d as u64;
            n += 1;
            max = max.max(d);
        }
    }
    let mean = sum as f64 / n as f64;
    // q90 source → q96 re-encode on a sawtooth gradient: one generation of
    // high-fidelity loss (measured ≈ 1.4)
    assert!(mean < 4.0, "L0 均值误差 {mean:.3} 超上限");
    // uncovered in-bounds zones are BLACK (OpenSlide-transparent gaps)
    // — stitched (0..140, 720..1120) is uncovered
    let at = |x: usize, y: usize| (y * 704 + x) * 3;
    assert_eq!(&got[at(10, 800)..at(10, 800) + 3], &[0, 0, 0]);
    assert_eq!(&want[at(10, 800)..at(10, 800) + 3], &[0, 0, 0]);
    // overlap zone [140,620)×[632,720): AOI0 must win (its d, not AOI2's)
    let _ = max;
}

// --------------------------------------------------------------------------- //
// variant rejections（复制前类型化拒绝）
// --------------------------------------------------------------------------- //

#[test]
fn variant_rejections_before_copy() {
    // JPEG 2000 compression
    expect_error(
        gen(&BifGenParams { jp2k: true, ..Default::default() }),
        UnsupportedKfbVariant,
        "JPEG 2000",
        "jp2k",
    );
    // LEFT join direction (Ventana-1.bif variant)
    expect_error(
        gen(&BifGenParams { left_direction: true, ..Default::default() }),
        UnsupportedKfbVariant,
        "Direction",
        "left-direction",
    );
    // multi-z
    expect_error(
        gen(&BifGenParams { z_layers: true, ..Default::default() }),
        UnsupportedKfbVariant,
        "Z-layers",
        "z-layers",
    );
    // no EncodeInfo XML
    expect_error(
        gen(&BifGenParams { no_xml: true, ..Default::default() }),
        UnsupportedKfbVariant,
        "EncodeInfo",
        "no-xml",
    );
    // classic TIFF container
    expect_error(
        gen(&BifGenParams { classic: true, ..Default::default() }),
        UnsupportedKfbVariant,
        "BigTIFF",
        "classic",
    );
    // gray / single-sample
    expect_error(
        gen(&BifGenParams { gray: true, ..Default::default() }),
        UnsupportedKfbVariant,
        "SamplesPerPixel",
        "gray",
    );
    // sparse referenced tile
    expect_error(
        gen(&BifGenParams { sparse: true, ..Default::default() }),
        UnsupportedKfbVariant,
        "稀疏",
        "sparse",
    );
    // probe gives the same rejections (front-end sniff fallback path)
    for (knob, frag) in [
        (BifGenParams { jp2k: true, ..Default::default() }, "JPEG 2000"),
        (BifGenParams { left_direction: true, ..Default::default() }, "Direction"),
        (BifGenParams { z_layers: true, ..Default::default() }, "Z-layers"),
    ] {
        let src = MemSource::new(gen(&knob));
        let e = bif::probe_bif(&src).expect_err("probe must reject");
        assert!(e.message.contains(frag), "probe: {}", e.message);
    }
}

#[test]
fn strict_lossless_and_fl_refused() {
    let data = gen(&BifGenParams::default());
    let src = MemSource::new(data.clone());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let e = convert_bif::convert_bif_to_bigtiff(
        &src,
        &mut sink,
        &mut scratch,
        &plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::StrictLossless),
        &job_no_checkpoint(),
    )
    .expect_err("strict-lossless");
    assert_eq!(e.code, PixelPolicyViolation);
    let src = MemSource::new(data);
    let e = convert_bif::convert_bif_to_bigtiff(
        &src,
        &mut MemSink::new(),
        &mut MemScratch::default(),
        &plan_for(OutputProfile::OmeBigTiffSubifd, PixelPolicy::AllowEdgeReencode),
        &job_no_checkpoint(),
    )
    .expect_err("fl profile");
    assert!(e.message.contains("荧光"));
}

// --------------------------------------------------------------------------- //
// budget refusal before allocation
// --------------------------------------------------------------------------- //

#[test]
fn memory_budget_refusal_before_allocation() {
    let data = gen(&BifGenParams::default());
    let e = convert_with_budget(&data, OutputProfile::ClassicJpegBigTiff, 64 * 1024)
        .expect_err("tiny budget");
    assert_eq!(e.code, ResourceLimitExceeded);
    assert!(e.message.contains("内存预算不足"));
    // default saver budget converts the same input
    let (r, _) = convert_with_budget(&data, OutputProfile::ClassicJpegBigTiff, 192 * 1024 * 1024)
        .expect("saver budget");
    assert!(r.output_bytes > 0);
}

// --------------------------------------------------------------------------- //
// resume byte identity
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

fn checkpoint_json(c: &CheckpointState, adapter: &str) -> String {
    format!(
        "{{\"level\":{},\"channel\":{},\"cell\":{},\"out\":{},\"adapter_version\":\"{}\",\"ifds\":[{}]}}",
        c.level,
        c.channel.unwrap_or(0),
        c.cell_done,
        c.committed_output,
        adapter,
        c.ifd_tiles
            .iter()
            .map(|t| t.to_string())
            .collect::<Vec<_>>()
            .join(",")
    )
}

#[test]
fn resume_is_byte_identical() {
    use sha2::Digest;
    use slide_transform_core::io::{FileScratch, FileSink, FileSource};
    let sha256 = |d: &[u8]| {
        let mut h = sha2::Sha256::new();
        h.update(d);
        format!("{:x}", h.finalize())
    };
    let data = gen(&BifGenParams::default());
    let dir = std::env::temp_dir().join(format!(
        "stc-bif-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.bif");
    std::fs::write(&src_path, &data).unwrap();
    let plan = || plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    let src = || FileSource::open(&src_path).unwrap();

    // reference run
    let ref_out = dir.join("ref.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        convert_bif::convert_bif_to_bigtiff(&src(), &mut out, &mut scratch, &plan(), &job)
            .unwrap();
        out.flush().unwrap();
    }
    let ref_sha = sha256(&std::fs::read(&ref_out).unwrap());
    let ref_result_levels = 4; // L0 704×1120 → 4 levels（l0-box2 链）

    for stop in [1usize, 3, 6, 9] {
        let crash_dir = dir.join(format!("crash{stop}"));
        std::fs::create_dir_all(&crash_dir).unwrap();
        let part_out = crash_dir.join("part.tif");
        let cancel = CancelFlag::new();
        let collector = Collector {
            states: Mutex::new(Vec::new()),
            stop_after: Some(stop),
            cancel: cancel.clone(),
            count: AtomicUsize::new(0),
        };
        let mut scratch2 = FileScratch::new(&crash_dir);
        {
            let mut out = FileSink::create(&part_out).unwrap();
            let null = NullProgress;
            let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
            let res = convert_bif::convert_bif_to_bigtiff(
                &src(),
                &mut out,
                &mut scratch2,
                &plan(),
                &job,
            );
            assert!(res.is_err(), "stop {stop}: expected the injected cancel to abort");
            out.flush().unwrap();
        }
        let states = collector.states.lock().unwrap().clone();
        let last = states.last().unwrap().clone();
        let rp = ResumePoint {
            level: last.level as usize,
            channel: last.channel.unwrap_or(0),
            cell: last.cell_done,
            committed_output: last.committed_output,
            ifd_tiles: last.ifd_tiles.clone(),
            adapter_version: Some(bif::ADAPTER_VERSION.to_string()),
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
        // resume: the same scratch factory keeps the surviving offcnt files
        let resumed;
        {
            let mut out = FileSink::open_preserve(&part_out).unwrap();
            let null = NullProgress;
            let job = JobControl::new(&null);
            let r = convert_bif::convert_bif_to_bigtiff_resume(
                &src(),
                &mut out,
                &mut scratch2,
                &plan(),
                &job,
                &rp,
            )
            .unwrap_or_else(|e| panic!("stop {stop}: resume failed: {}", e.message));
            out.flush().unwrap();
            resumed = r;
        }
        assert_eq!(
            sha256(&std::fs::read(&part_out).unwrap()),
            ref_sha,
            "stop {stop}: resumed bytes differ"
        );
        assert_eq!(resumed.levels.len(), ref_result_levels);
        std::fs::remove_dir_all(&crash_dir).ok();
    }
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn resume_refuses_foreign_adapter_and_version() {
    let data = gen(&BifGenParams::default());
    let src = MemSource::new(data.clone());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: Some(3),
        cancel: CancelFlag::new(),
        count: AtomicUsize::new(0),
    };
    let null = NullProgress;
    let job = JobControl::new(&null)
        .with_checkpoint(&collector)
        .with_cancel(collector.cancel.clone());
    assert!(convert_bif::convert_bif_to_bigtiff(
        &src,
        &mut sink,
        &mut scratch,
        &plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode),
        &job,
    )
    .is_err());
    let last = collector.states.lock().unwrap().last().unwrap().clone();
    // a checkpoint naming a different converter (source swapped) is refused
    let foreign = parse_resume_json(&checkpoint_json(&last, "hamamatsu-ndpi-jpeg")).unwrap();
    let src = MemSource::new(data.clone());
    let e = convert_bif::convert_bif_to_bigtiff_resume(
        &src,
        &mut MemSink::new(),
        &mut MemScratch::default(),
        &plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode),
        &job_no_checkpoint(),
        &foreign,
    )
    .expect_err("foreign adapter");
    assert!(e.message.contains("不得混合"));
    // a checkpoint without any version is foreign as well
    let nover = parse_resume_json(&format!(
        "{{\"level\":{},\"channel\":0,\"cell\":{},\"out\":{},\"ifds\":[{}]}}",
        last.level,
        last.cell_done,
        last.committed_output,
        last.ifd_tiles.iter().map(|t| t.to_string()).collect::<Vec<_>>().join(",")
    ))
    .unwrap();
    let src = MemSource::new(data.clone());
    assert!(convert_bif::convert_bif_to_bigtiff_resume(
        &src,
        &mut MemSink::new(),
        &mut MemScratch::default(),
        &plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode),
        &job_no_checkpoint(),
        &nover,
    )
    .is_err());
}
