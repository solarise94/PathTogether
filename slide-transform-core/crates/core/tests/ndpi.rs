//! F6 Hamamatsu NDPI adapter tests: synthetic NDPI fixtures (generated in
//! code, no sample bytes committed), whole-layer restart-segment decode →
//! tile re-encode, `l0-box2` generated tail, variant / routing rejections
//! (typed), budget refusal before allocation, and resume byte identity.
//!
//! Requires `fixtures` (`cargo test -p slide-transform-core --features
//! fixtures`).

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_ndpi;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::error::{CoreError, CoreResult};
use slide_transform_core::io::{ByteSource, MemScratch, MemSink, MemSource, RandomAccessSink};
use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
use slide_transform_core::ndpi::{
    self, estimate_ndpi, probe_ndpi, ADAPTER_VERSION, OUT_TILE, PYRAMID_METHOD, SOURCE_FORMAT,
};
use slide_transform_core::ndpi_fixture::{build_synthetic_ndpi, FixturePattern, NdpiGenParams};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::resume::{parse_resume_json, ResumePoint};
use slide_transform_core::validate::validate_output;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

fn gen(p: &NdpiGenParams) -> Vec<u8> {
    let mut sink = MemSink::new();
    build_synthetic_ndpi(&mut sink, p).unwrap();
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
    let r = convert_ndpi::convert_ndpi(
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
    let r = convert_ndpi::convert_ndpi(&src, &mut sink, &mut scratch, &plan)?;
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

fn default_params() -> NdpiGenParams {
    NdpiGenParams {
        levels: 2, // keep the fixture fast: L0 + one reduced layer
        ..Default::default()
    }
}

// --------------------------------------------------------------------------- //
// probe / structural contract
// --------------------------------------------------------------------------- //

#[test]
fn probe_and_convert_synthetic() {
    let p = default_params();
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_ndpi(&src).expect("probe");
    assert_eq!(doc.levels.len(), 2);
    let l0 = &doc.levels[0];
    assert_eq!((l0.width, l0.height), (p.width, p.height));
    assert_eq!(doc.kind, slide_transform_core::tiff_read::TiffKind::Classic);
    assert_eq!(doc.objective, Some(20.0));
    assert_eq!(doc.mpp, Some((0.4990, 0.4990)));
    // single strip, restarts present, segments = ceil(total/DRI)
    let mcus_x = (l0.width / l0.mcu_w()) as u64;
    let mcus_y = (l0.height / l0.mcu_h()) as u64;
    // default restart_rows = 1 → DRI = MCUs per row（真实条带的约定）
    assert_eq!(u64::from(l0.restart_interval), mcus_x);
    assert_eq!(l0.total_mcus, mcus_x * mcus_y);
    assert_eq!(u64::from(l0.segments), l0.total_mcus.div_ceil(u64::from(l0.restart_interval)));
    // generated tail per the gtiff rule
    assert!(!doc.generated.is_empty());
    let last = *doc.generated.last().unwrap();
    assert!(last.0 <= 256 && last.1 <= 256);
    // estimate: bounded by 2× payload + per-tile records
    let est = estimate_ndpi(&doc);
    assert_eq!(est.payload_bytes, l0.strip_bytes);
    assert!(est.output_upper_bound_bytes > l0.strip_bytes);
    assert!(est.compact_upper_bound_bytes < est.output_upper_bound_bytes);

    // ---- conversion: classic profile ------------------------------------ //
    let (r, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    assert_eq!(r.source_format, Some(SOURCE_FORMAT));
    assert_eq!(r.adapter_version, Some(ADAPTER_VERSION));
    assert_eq!(r.width, p.width);
    assert_eq!(r.height, p.height);
    // output levels = re-encoded L0 + the generated tail
    assert_eq!(r.levels.len(), 1 + doc.generated.len());
    assert_eq!(r.levels[0].tiles_total, r.levels[0].tiles_reencoded);
    assert_eq!(r.count_raw_copied(), 0);
    let composed = r.composed.as_ref().expect("composed summary");
    assert_eq!(composed.mode, "segment-compose-reencode");
    assert_eq!(composed.fingerprint, "ndpi-segment-compose:q96:y422:hstd:v1");
    assert_eq!(composed.pyramid, PYRAMID_METHOD);
    assert_eq!(composed.quality, 96);
    assert_eq!(composed.sampling, "4:2:2");
    // warnings: generated tail + no ICC; nothing associated in this fixture
    assert!(r.warnings.iter().any(|w| w == "color_management_not_applied"));
    assert!(!r.warnings.iter().any(|w| w == "ndpi_associated_not_exported"));
    assert_eq!(r.associated.len(), 0);
    // the output validates structurally
    let vsrc = MemSource::new(out.clone());
    let v = validate_output(&vsrc, out.len() as u64, Some(r.validation.ifd_count)).expect("validate");
    // classic profile: every pyramid level is a main IFD in the chain
    assert_eq!(v.main_ifds as usize, r.levels.len());
    assert_eq!(v.ifd_count as usize, r.levels.len());
}

#[test]
fn probe_ome_rgb_profile_contract() {
    let p = default_params();
    let data = gen(&p);
    let (r, _out) = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).expect("convert ome");
    assert_eq!(r.format, slide_transform_core::convert_bf::FORMAT_OME_RGB);
    assert_eq!(r.levels.len(), 1 + 1); // L0 + ≥1 generated tail level
}

// --------------------------------------------------------------------------- //
// pixel gate: segment decode == whole-strip decode (OpenSlide convention)
// --------------------------------------------------------------------------- //

#[test]
fn l0_tiles_match_whole_strip_decode() {
    // The composed L0 tiles must be the whole-strip decode of the source
    // re-encoded at q96 4:2:2 — verify by decoding the source strip whole
    // (jpeg::decode on the assembled strip), cutting 256-px tiles, and
    // comparing against the converted output tiles decoded back.
    let p = NdpiGenParams { pattern: FixturePattern::Gradient, ..default_params() };
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_ndpi(&src).expect("probe");
    let l0 = &doc.levels[0];

    let (r, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    // read output tile (0,0) payload back from the sink
    let out_src = MemSource::new(out.clone());
    let hdr = slide_transform_core::tiff_read::read_header(&out_src).unwrap();
    let chain = slide_transform_core::tiff_read::ifd_chain(&out_src, &hdr).unwrap();
    let mut cur = slide_transform_core::tiff_read::TileCursor::new(&out_src, &hdr, &chain[0]).unwrap();
    let (off, len) = cur.next_pair().unwrap().unwrap();

    // whole-strip decode of the source layer
    let strip = src.read_at(l0.strip_offset, l0.strip_bytes as usize).unwrap();
    let whole = slide_transform_core::jpeg::decode(&strip, (l0.width as u64) * (l0.height as u64))
        .expect("whole strip decode");
    assert_eq!(whole.width, l0.width);

    // the output tile is the q96 4:2:2 re-encode of the same pixels: decode
    // it and require a small mean error (JPEG generation loss only)
    let tile = out_src.read_at(off, len as usize).unwrap();
    let img = slide_transform_core::jpeg::decode(&tile, (OUT_TILE as u64) * (OUT_TILE as u64))
        .expect("tile decode");
    let mut acc = 0u64;
    for y in 0..img.height as usize {
        for x in 0..img.width as usize {
            let t = (y * img.width as usize + x) * 3;
            let s = (y * whole.width as usize + x) * 3;
            for c in 0..3 {
                acc += (img.data[t + c] as i32 - whole.data[s + c] as i32).unsigned_abs() as u64;
            }
        }
    }
    let mean = acc as f64 / ((img.width as u64 * img.height as u64 * 3) as f64);
    assert!(mean < 3.0, "tile(0,0) vs whole-strip decode mean abs diff {mean}");
}

#[test]
fn l0_carry_path_multi_band_segments() {
    // restart_rows=40 → 每段 40 MCU 行（320 px），横跨 256 px 行带边界：
    // 触发段的跨带携带（carry）路径，输出仍必须与整层解码一致
    let p = NdpiGenParams {
        restart_rows: 40,
        levels: 1,
        pattern: FixturePattern::Gradient,
        ..Default::default()
    };
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_ndpi(&src).expect("probe");
    let l0 = &doc.levels[0];
    assert_eq!(l0.segments, (l0.height as u64).div_ceil(320));
    let (r, _out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    assert_eq!(r.levels[0].tiles_reencoded, r.levels[0].tiles_total);
    // pixel spot check: tile(0,0) against a whole-strip decode
    let strip = src.read_at(l0.strip_offset, l0.strip_bytes as usize).unwrap();
    let whole = slide_transform_core::jpeg::decode(&strip, (l0.width as u64) * (l0.height as u64))
        .expect("whole strip decode");
    let (_r2, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    let out_src = MemSource::new(out);
    let hdr = slide_transform_core::tiff_read::read_header(&out_src).unwrap();
    let chain = slide_transform_core::tiff_read::ifd_chain(&out_src, &hdr).unwrap();
    let mut cur = slide_transform_core::tiff_read::TileCursor::new(&out_src, &hdr, &chain[0]).unwrap();
    let (off, len) = cur.next_pair().unwrap().unwrap();
    let img = slide_transform_core::jpeg::decode(
        &out_src.read_at(off, len as usize).unwrap(),
        (OUT_TILE as u64) * (OUT_TILE as u64),
    )
    .expect("tile decode");
    let mut acc = 0u64;
    for y in 0..img.height as usize {
        for x in 0..img.width as usize {
            let t = (y * img.width as usize + x) * 3;
            let s = (y * whole.width as usize + x) * 3;
            for c in 0..3 {
                acc += (img.data[t + c] as i32 - whole.data[s + c] as i32).unsigned_abs() as u64;
            }
        }
    }
    let mean = acc as f64 / ((img.width as u64 * img.height as u64 * 3) as f64);
    assert!(mean < 3.0, "carry path tile(0,0) mean abs diff {mean}");
}

// --------------------------------------------------------------------------- //
// variant / routing rejections (typed)
// --------------------------------------------------------------------------- //

#[test]
fn rejects_non_hamamatsu_make() {
    let p = NdpiGenParams { make: "OtherScanner".to_string(), ..default_params() };
    expect_error(gen(&p), UnsupportedKfbVariant, "Make", "non-hamamatsu make");
}

#[test]
fn rejects_no_restart_markers() {
    let p = NdpiGenParams { no_restart: true, ..default_params() };
    expect_error(
        gen(&p),
        UnsupportedKfbVariant,
        "restart marker",
        "strip without DRI",
    );
}

#[test]
fn rejects_progressive_sof2() {
    let p = NdpiGenParams { progressive: true, ..default_params() };
    expect_error(gen(&p), JpegDecodeFailed, "SOF", "progressive strip");
}

#[test]
fn rejects_jpeg2000_compression() {
    let p = NdpiGenParams { compression: 33005, ..default_params() };
    expect_error(
        gen(&p),
        UnsupportedKfbVariant,
        "JPEG 2000",
        "jp2k compression tag",
    );
}

#[test]
fn rejects_extrasamples_fluoro_variant() {
    let p = NdpiGenParams { extra_samples: true, ..default_params() };
    expect_error(
        gen(&p),
        UnsupportedKfbVariant,
        "ExtraSamples",
        "extra samples page",
    );
}

#[test]
fn rejects_bigtiff_container() {
    // a BigTIFF header routed here is a typed rejection (NDPI is classic)
    let mut data = gen(&default_params());
    data[2] = 0x2B;
    data[3] = 0x00;
    expect_error(data, UnsupportedKfbVariant, "BigTIFF", "bigtiff container");
}

#[test]
fn macro_and_focusmap_detected_not_exported() {
    let p = NdpiGenParams {
        macro_page: true,
        focus_map: true,
        levels: 2,
        ..Default::default()
    };
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_ndpi(&src).expect("probe");
    assert_eq!(doc.levels.len(), 2);
    let names: Vec<&str> = doc.associated.iter().map(|a| a.name.as_str()).collect();
    assert!(names.contains(&"macro"));
    assert!(names.contains(&"focusmap"));
    // the conversion reports them but exports nothing
    let (r, _out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    assert_eq!(r.associated.len(), 2);
    assert!(r.warnings.iter().any(|w| w == "ndpi_associated_not_exported"));
    assert!(r.associated.iter().all(|a| a.source_length == 0));
    // the macro/focus pages never contribute output levels
    assert_eq!(r.levels.len(), 1 + 1);
}

#[test]
fn mpp_absent_stays_unknown() {
    let p = NdpiGenParams { mpp: None, ..default_params() };
    let data = gen(&p);
    let src = MemSource::new(data);
    let doc = probe_ndpi(&src).expect("probe");
    assert_eq!(doc.mpp, None, "MPP is never invented from the objective");
}

// --------------------------------------------------------------------------- //
// NDPI value extensions (>4 GiB offsets): combining arithmetic
// --------------------------------------------------------------------------- //

#[test]
fn value_extensions_widen_inline_longs() {
    // hand-assemble a tiny IFD: 2 entries (WIDTH=0x12345678 with ext 0xAB,
    // HEIGHT=64 ext 0), the NDPI extension area after the next pointer.
    let mut b: Vec<u8> = Vec::new();
    b.extend_from_slice(b"II*\0");
    b.extend_from_slice(&8u32.to_le_bytes()); // first IFD at 8
    let ifd_at = 8u64;
    b.extend_from_slice(&2u16.to_le_bytes());
    let mut put = |tag: u16, val: u32| {
        b.extend_from_slice(&tag.to_le_bytes());
        b.extend_from_slice(&4u16.to_le_bytes()); // LONG
        b.extend_from_slice(&1u32.to_le_bytes());
        b.extend_from_slice(&val.to_le_bytes());
    };
    put(256, 0x1234_5678);
    put(257, 64);
    b.extend_from_slice(&0u32.to_le_bytes()); // next
    b.extend_from_slice(&0u32.to_le_bytes()); // 4 reserved bytes
    // extension words in entry order: 0xAB for WIDTH, 0 for HEIGHT
    b.extend_from_slice(&0xABu32.to_le_bytes());
    b.extend_from_slice(&0u32.to_le_bytes());
    let src = MemSource::new(b);
    let hdr = slide_transform_core::tiff_read::read_header(&src).unwrap();
    let ifd = slide_transform_core::tiff_read::read_ifd(&src, &hdr, ifd_at).unwrap();
    let ext = ndpi::ndpi_value_extensions(&src, &hdr, &ifd).unwrap();
    assert_eq!(ext, vec![0xAB, 0]);
    let w = ndpi::ndpi_find_u64(&src, &hdr, &ifd, &ext, 256).unwrap().unwrap();
    assert_eq!(w, 0x00AB_1234_5678, "inline LONG widened by its extension word");
    let h = ndpi::ndpi_find_u64(&src, &hdr, &ifd, &ext, 257).unwrap().unwrap();
    assert_eq!(h, 64);
}

// --------------------------------------------------------------------------- //
// budget refusal BEFORE allocation
// --------------------------------------------------------------------------- //

#[test]
fn budget_refusal_is_typed_and_prealloc() {
    let data = gen(&default_params());
    // a budget far below the band/segment working set refuses in the probe
    // and conversion with the stable code, never OOMs
    expect_error_budget(&data, 1024 * 1024, "内存预算不足");
}

fn expect_error_budget(data: &[u8], budget: u64, frag: &str) {
    match convert_with_budget(data, OutputProfile::ClassicJpegBigTiff, budget) {
        Err(e) => {
            assert_eq!(e.code, ResourceLimitExceeded, "got: {}", e.message);
            assert!(e.message.contains(frag), "message lacks {frag:?}: {}", e.message);
        }
        Ok(_) => panic!("expected resource_profile_insufficient"),
    }
}

#[test]
fn probe_refuses_under_budget() {
    let data = gen(&default_params());
    let src = MemSource::new(data);
    match ndpi::probe_ndpi_with_budget(&src, 64 * 1024) {
        Err(e) => {
            assert_eq!(e.code, ResourceLimitExceeded);
            assert!(e.message.contains("内存预算不足"));
        }
        Ok(_) => panic!("expected probe budget refusal"),
    }
}

// --------------------------------------------------------------------------- //
// resume: byte identity (crash inside L0), adapter-version pin
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
fn resume_from_a_crash_inside_l0_is_byte_identical() {
    use slide_transform_core::io::{FileSink, FileScratch, FileSource};
    use slide_transform_core::resume::ResumePoint;

    let data = gen(&default_params()); // L0 = 2×2 tiles = 2 checkpoint rows
    let dir = std::env::temp_dir().join(format!(
        "stc6-ndpi-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.ndpi");
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
        convert_ndpi::convert_ndpi_to_bigtiff(&src(), &mut out, &mut scratch, &plan(), &job)
            .unwrap();
        out.flush().unwrap();
    }
    let ref_sha = sha256(&std::fs::read(&ref_out).unwrap());

    // crashed run: cancel from checkpoint 1 (inside L0, after its first row)
    let crash_dir = dir.join("crash");
    std::fs::create_dir_all(&crash_dir).unwrap();
    let part_out = dir.join("part.tif");
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: Some(1),
        cancel: cancel.clone(),
        count: AtomicUsize::new(0),
    };
    let mut scratch2 = FileScratch::new(&crash_dir);
    {
        let mut out = FileSink::create(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
        let res = convert_ndpi::convert_ndpi_to_bigtiff(&src(), &mut out, &mut scratch2, &plan(), &job);
        match res {
            Err(e) => assert!(e.message.contains("已取消"), "unexpected err {e:?}"),
            Ok(_) => panic!("expected the injected cancel to abort"),
        }
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
    assert!(!states.is_empty(), "no checkpoints recorded");
    let last = states.last().unwrap();
    assert_eq!(last.level, 0, "the crash must sit inside L0 for this test");
    assert!(last.cell_done > 0);
    let rp = ResumePoint {
        level: last.level as usize,
        channel: last.channel.unwrap_or(0),
        cell: last.cell_done,
        committed_output: last.committed_output,
        ifd_tiles: last.ifd_tiles.clone(),
        adapter_version: Some(ADAPTER_VERSION.to_string()),
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
        let r = convert_ndpi::convert_ndpi_to_bigtiff_resume(
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
    assert_eq!(resumed.levels.len(), 2);
}

#[test]
fn resume_from_a_crash_inside_a_generated_level_is_byte_identical() {
    // 审查回归（high）：崩溃落在生成层（level ≥ 1）时，resume 对已完成层
    // 必须 restage 描述 JSON——空描述会丢 tag 270，产物与不中断运行不再
    // 逐字节一致（convert_scn.rs 同一坑的显式预防）。
    use slide_transform_core::io::{FileSink, FileScratch, FileSource};
    use slide_transform_core::resume::ResumePoint;

    // 1024×640：L0 = 4×3 tiles = 3 checkpoint 行；生成层 512×320 = 2×2
    // tiles = 2 行 → 崩溃点 4 落在生成层的第一行（cell=2 未提交）
    let data = gen(&NdpiGenParams { width: 1024, height: 640, levels: 1, ..Default::default() });
    let dir = std::env::temp_dir().join(format!(
        "stc6-ndpi-resume-gen-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.ndpi");
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
        convert_ndpi::convert_ndpi_to_bigtiff(&src(), &mut out, &mut scratch, &plan(), &job)
            .unwrap();
        out.flush().unwrap();
    }
    let ref_sha = sha256(&std::fs::read(&ref_out).unwrap());

    // crashed run: cancel from checkpoint 4（L0 有 3 行 → 必落在生成层）
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
        let res = convert_ndpi::convert_ndpi_to_bigtiff(&src(), &mut out, &mut scratch2, &plan(), &job);
        match res {
            Err(e) => assert!(e.message.contains("已取消"), "unexpected err {e:?}"),
            Ok(_) => panic!("expected the injected cancel to abort"),
        }
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
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
        adapter_version: Some(ADAPTER_VERSION.to_string()),
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

    // resume run
    let resumed;
    {
        let mut out = FileSink::open_preserve(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_ndpi::convert_ndpi_to_bigtiff_resume(
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
        "resumed bytes differ (description restage / out-of-level determinism)"
    );
    // 1024×640 → L0 + 生成尾 (512×320) + (256×160) = 3 层
    assert_eq!(resumed.levels.len(), 3);
}

/// 审查回归（medium）：本转换器自己的 classic BigTIFF 产物（描述 JSON 带
/// "source_format": "hamamatsu-ndpi-jpeg"）必须被厂商嗅探识别为转换器输出，
/// 绝不能落进通用 TIFF 适配器做第二次有损重编码。
#[test]
fn converter_output_is_typed_rejected_not_reconvertible() {
    let data = gen(&default_params());
    let (_r, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    let out_src = MemSource::new(out);
    use slide_transform_core::scn::{classify_description, sniff_tiff_vendor, TiffVendor};
    assert_eq!(
        sniff_tiff_vendor(&out_src).unwrap(),
        TiffVendor::ConverterBigTiff,
        "NDPI converter output must be recognized as converter output"
    );
    // 纯描述分类器同一判定（词表逐条核对）
    let hdr = slide_transform_core::tiff_read::read_header(&out_src).unwrap();
    let chain = slide_transform_core::tiff_read::ifd_chain(&out_src, &hdr).unwrap();
    let desc = match chain[0].find(270) {
        Some(e) => String::from_utf8_lossy(&slide_transform_core::tiff_read::entry_value(&out_src, &hdr, e).unwrap())
            .to_string(),
        None => String::new(),
    };
    assert!(desc.contains("\"source_format\": \"hamamatsu-ndpi-jpeg\""), "{desc}");
    assert_eq!(classify_description(&desc), TiffVendor::ConverterBigTiff);
    assert!(slide_transform_core::scn::CONVERTER_SOURCE_FORMATS
        .contains(&"hamamatsu-ndpi-jpeg"));
}

/// 审查回归（medium）：ndpi_find_f64 必须按文件字节序解析（SourceLens /
/// MPP），大端文件曾因无条件 from_le_bytes 解析出错误值（macro 的
/// SourceLens=-1 会被读成正数）。
#[test]
fn f64_tags_parse_per_byte_order() {
    use slide_transform_core::ndpi::ndpi_find_f64;
    use slide_transform_core::tiff_read::{read_header, read_ifd};

    // 手工构造一条 IFD：FLOAT SourceLens=20.0、FLOAT macro-lens=-1.0、
    // DOUBLE MPP=0.499、RATIONAL 1/2（DOUBLE/RATIONAL 为外联值）——
    // LE/BE 两种字节序编码，解析出的数值必须一致。
    fn build(little: bool) -> (MemSource, u64) {
        let p16 = |v: u16| if little { v.to_le_bytes().to_vec() } else { v.to_be_bytes().to_vec() };
        let p32 = |v: u32| if little { v.to_le_bytes().to_vec() } else { v.to_be_bytes().to_vec() };
        let f32b = |v: f32| if little { v.to_le_bytes().to_vec() } else { v.to_be_bytes().to_vec() };
        let f64b = |v: f64| if little { v.to_le_bytes().to_vec() } else { v.to_be_bytes().to_vec() };
        let ifd_at: u64 = 8;
        let heap_at: u64 = ifd_at + 2 + 4 * 12 + 4; // count + 4 entries + next
        let mut b: Vec<u8> = Vec::new();
        b.extend_from_slice(if little { b"II" } else { b"MM" });
        b.extend_from_slice(&p16(42));
        b.extend_from_slice(&p32(ifd_at as u32));
        b.extend_from_slice(&p16(4));
        let mut entry = |tag: u16, typ: u16, val: &[u8]| {
            b.extend_from_slice(&p16(tag));
            b.extend_from_slice(&p16(typ));
            b.extend_from_slice(&p32(1));
            if typ == 12 || typ == 5 {
                // 外联：值域字段是绝对偏移（DOUBBLE 8 B / RATIONAL 8 B）
                let off = heap_at
                    + if tag == 65441 { 0 } else { 8 };
                b.extend_from_slice(&p32(off as u32));
            } else {
                let mut v = val.to_vec();
                v.resize(4, 0);
                b.extend_from_slice(&v);
            }
        };
        entry(65421, 11, &f32b(20.0));
        entry(65422, 11, &f32b(-1.0));
        entry(65441, 12, &f64b(0.499));
        entry(65442, 5, &[&p32(1)[..], &p32(2)[..]].concat());
        b.extend_from_slice(&p32(0)); // next
        b.extend_from_slice(&f64b(0.499)); // heap: DOUBLE
        b.extend_from_slice(&p32(1));
        b.extend_from_slice(&p32(2)); // heap: RATIONAL 1/2
        (MemSource::new(b), ifd_at)
    }
    for little in [true, false] {
        let (src, ifd_at) = build(little);
        let hdr = read_header(&src).unwrap();
        let ifd = read_ifd(&src, &hdr, ifd_at).unwrap();
        let ext: Vec<u32> = vec![0; ifd.entries.len()];
        let lens = ndpi_find_f64(&src, &hdr, &ifd, 65421).unwrap().unwrap();
        assert_eq!(lens, 20.0, "little={little}: SourceLens");
        let macro_lens = ndpi_find_f64(&src, &hdr, &ifd, 65422).unwrap().unwrap();
        assert_eq!(macro_lens, -1.0, "little={little}: negative SourceLens");
        let mpp = ndpi_find_f64(&src, &hdr, &ifd, 65441).unwrap().unwrap();
        assert!((mpp - 0.499).abs() < 1e-12, "little={little}: MPP {mpp}");
        let rat = ndpi_find_f64(&src, &hdr, &ifd, 65442).unwrap().unwrap();
        assert_eq!(rat, 0.5, "little={little}: RATIONAL");
    }
}

#[test]
fn resume_refuses_a_foreign_or_absent_adapter_version() {
    use slide_transform_core::io::{FileScratch, FileSink, FileSource};
    use slide_transform_core::resume::ResumePoint;

    let data = gen(&default_params());
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    for version in [Some("0".to_string()), None] {
        let src = MemSource::new(data.clone());
        let mut sink = MemSink::new();
        let mut scratch = MemScratch::default();
        let null = NullProgress;
        let job = JobControl::new(&null);
        // a checkpoint that names another generation (or none) is refused
        // BEFORE anything is decoded or written
        let rp = ResumePoint {
            level: 0,
            channel: 0,
            cell: 512,
            committed_output: 1 << 20,
            ifd_tiles: vec![512],
            adapter_version: version.clone(),
        };
        let e = convert_ndpi::convert_ndpi_to_bigtiff_resume(
            &src, &mut sink, &mut scratch, &plan, &job, &rp,
        )
        .err()
        .expect("version-mismatched resume must refuse");
        assert_eq!(e.code, ConversionValidationFailed, "{}", e.message);
        assert!(e.message.contains("适配器"), "{}", e.message);
        let _ = FileSink::create; let _ = FileScratch::new; let _ = FileSource::open;
    }
}

#[test]
fn resume_json_carries_the_adapter_version_field() {
    // parse_resume_json must carry the adapter_version field through
    let rp = parse_resume_json(
        r#"{"level":0,"channel":0,"cell":512,"out":4096,"ifds":[512],"adapter_version":"9"}"#,
    )
    .unwrap();
    assert_eq!(rp.adapter_version.as_deref(), Some("9"));
}
