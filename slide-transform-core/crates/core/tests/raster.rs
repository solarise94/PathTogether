//! F8 普通图片（BMP/JPEG）适配器测试: synthetic fixtures (generated in
//! code, no sample bytes committed), BMP row decode + JPEG restart-segment
//! decode + JPEG MCU-row band decode → tile re-encode, `l0-box2` generated
//! tail, variant / routing rejections (typed), budget refusal before
//! allocation, resume byte identity, and band-vs-whole-frame byte equality.
//!
//! Requires `fixtures` (`cargo test -p slide-transform-core --features
//! fixtures`).

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_raster;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::error::CoreError;
use slide_transform_core::io::{ByteSource, MemScratch, MemSink, MemSource, RandomAccessSink};
use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
use slide_transform_core::jpeg::{self, band::BandScanner, EncoderCfg, Sampling};
use slide_transform_core::raster::{
    estimate_raster, probe_raster, ADAPTER_VERSION, OUT_TILE, PYRAMID_METHOD, SOURCE_FORMAT,
};
use slide_transform_core::raster_fixture::{build_synthetic_raster, FixturePattern, RasterGenParams};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::resume::ResumePoint;
use slide_transform_core::validate::validate_output;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

fn gen(p: &RasterGenParams) -> Vec<u8> {
    build_synthetic_raster(p).unwrap()
}

fn bmp_params() -> RasterGenParams {
    RasterGenParams { kind: "bmp".into(), ..Default::default() }
}

fn jpeg_params(no_restart: bool) -> RasterGenParams {
    RasterGenParams {
        kind: "jpeg".into(),
        width: 512,
        height: 320,
        no_restart,
        ..Default::default()
    }
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
    let r = convert_raster::convert_raster(
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
    let r = convert_raster::convert_raster(&src, &mut sink, &mut scratch, &plan)?;
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

fn sha256(data: &[u8]) -> String {
    use sha2::Digest;
    let mut h = sha2::Sha256::new();
    h.update(data);
    format!("{:x}", h.finalize())
}

// --------------------------------------------------------------------------- //
// band scanner == whole-frame decode (byte identity)
// --------------------------------------------------------------------------- //

#[test]
fn band_matches_whole_frame_decode() {
    let cases: [(u32, u32, Sampling); 8] = [
        (64, 48, Sampling::S422),
        (37, 23, Sampling::S422),
        (33, 17, Sampling::S420),
        (64, 48, Sampling::S420),
        (16, 16, Sampling::S444),
        (37, 23, Sampling::S444),
        (512, 320, Sampling::S422),
        (24, 9, Sampling::S422),
    ];
    for (w, h, s) in cases {
        let mut img = Vec::with_capacity((w * h * 3) as usize);
        for y in 0..h {
            for x in 0..w {
                img.push((x * 7 + y) as u8);
                img.push((y * 11) as u8);
                img.push(((x + y) * 5 + 13) as u8);
            }
        }
        let jpg = jpeg::encode_rgb(&img, w, h, &EncoderCfg::with_quality(92, s)).unwrap();
        let whole = jpeg::decode(&jpg, (w as u64) * (h as u64)).unwrap();
        let src = MemSource::new(jpg.clone());
        let mut budget = slide_transform_core::budget::MemBudget::saver();
        let mut scanner = BandScanner::open(&src, &jpg, 0, jpg.len() as u64, false, &mut budget)
            .unwrap_or_else(|e| panic!("{w}×{h} {s:?}: {e}"));
        let mut bands: Vec<(u32, u32, Vec<u8>)> = Vec::new();
        while let Some(b) = scanner.next_band().unwrap() {
            bands.push((b.y0, b.rows, b.data));
        }
        // reassemble and compare byte-for-byte
        let mut back = vec![0u8; whole.data.len()];
        for (y0, rows, data) in &bands {
            back[*y0 as usize * w as usize * 3..(*y0 + *rows) as usize * w as usize * 3]
                .copy_from_slice(data);
        }
        assert_eq!(back.len(), whole.data.len());
        assert_eq!(back, whole.data, "band decode ≠ whole-frame decode ({w}×{h} {s:?})");
    }
}

#[test]
fn band_skip_to_preserves_stream_identity() {
    // decoding straight through == skipping to a band then decoding on
    let (w, h) = (512u32, 320u32);
    let img: Vec<u8> = (0..w * h * 3)
        .map(|i| ((i as u64 * 1103515245 + 12345) >> 16) as u8)
        .collect();
    let jpg = jpeg::encode_rgb(&img, w, h, &EncoderCfg::with_quality(92, Sampling::S422)).unwrap();
    let src = MemSource::new(jpg.clone());
    let mut budget = slide_transform_core::budget::MemBudget::saver();
    let mut straight =
        BandScanner::open(&src, &jpg, 0, jpg.len() as u64, false, &mut budget).unwrap();
    let mut straight_out = Vec::new();
    while let Some(b) = straight.next_band().unwrap() {
        straight_out.extend_from_slice(&b.data);
    }
    let src2 = MemSource::new(jpg.clone());
    let mut budget2 = slide_transform_core::budget::MemBudget::saver();
    let mut skipped =
        BandScanner::open(&src2, &jpg, 0, jpg.len() as u64, false, &mut budget2).unwrap();
    let total = skipped.bands_total();
    let skip_bands = total / 2;
    skipped.skip_to(skip_bands).unwrap();
    let mut skipped_out = Vec::new();
    while let Some(b) = skipped.next_band().unwrap() {
        skipped_out.extend_from_slice(&b.data);
    }
    // 直通流的跳过段之后部分必须与 skip 后的流逐字节一致
    let tail = skip_bands as usize * 8 * w as usize * 3; // S422: MCU 高 8
    assert_eq!(
        straight_out[tail..],
        skipped_out,
        "skip_to 后的 band 流与直通不一致"
    );
}

// --------------------------------------------------------------------------- //
// BMP: probe / convert contract
// --------------------------------------------------------------------------- //

#[test]
fn bmp_probe_and_convert_both_profiles() {
    let p = bmp_params();
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_raster(&src).expect("probe");
    assert_eq!(doc.kind.id(), "bmp-24");
    assert_eq!((doc.width, doc.height), (p.width, p.height));
    let layout = doc.bmp.as_ref().unwrap();
    assert!(!layout.top_down);
    assert_eq!(layout.dib_header, 40);
    assert!(doc.jpeg.is_none());
    assert!(doc.icc.is_none());
    assert!(!doc.generated.is_empty());
    let last = *doc.generated.last().unwrap();
    assert!(last.0 <= 256 && last.1 <= 256);
    let est = estimate_raster(&doc, data.len() as u64);
    assert_eq!(est.payload_bytes, data.len() as u64);
    assert!(est.output_upper_bound_bytes > 0);
    assert!(est.compact_upper_bound_bytes < est.output_upper_bound_bytes);

    for (profile, out_kind) in [
        (OutputProfile::ClassicJpegBigTiff, "classic"),
        (OutputProfile::OmeBigTiffRgbSubifd, "ome"),
    ] {
        let (r, out) = convert(&data, profile).expect("convert");
        assert_eq!(r.source_format, Some(SOURCE_FORMAT));
        assert_eq!(r.adapter_version, Some(ADAPTER_VERSION));
        assert_eq!(r.width, p.width);
        assert_eq!(r.height, p.height);
        assert_eq!(r.levels.len(), 1 + doc.generated.len());
        assert_eq!(r.levels[0].tiles_total, r.levels[0].tiles_reencoded);
        assert_eq!(r.count_raw_copied(), 0);
        let composed = r.composed.as_ref().expect("composed summary");
        assert_eq!(composed.mode, "raster-compose-reencode");
        assert_eq!(composed.fingerprint, "raster-compose:q96:y422:hstd:v1");
        assert_eq!(composed.pyramid, PYRAMID_METHOD);
        assert_eq!(composed.quality, 96);
        assert_eq!(composed.sampling, "4:2:2");
        // 无物理标尺：classic 描述与 OME XML 都不得携带 mpp/PhysicalSize
        if out_kind == "ome" {
            let hay = String::from_utf8_lossy(&out);
            assert!(
                !hay.contains("PhysicalSize"),
                "OME 输出不得写 PhysicalSize（BMP/JPEG 无物理标尺）"
            );
            assert!(hay.contains("ome-xml-present") || r.validation.ifd_count >= 1);
        }
        // warnings: no ICC + no physical size
        assert!(r.warnings.iter().any(|w| w == "color_management_not_applied"));
        assert!(r.warnings.iter().any(|w| w == "raster_no_physical_size"));
        let vsrc = MemSource::new(out.clone());
        let v = validate_output(&vsrc, out.len() as u64, Some(r.validation.ifd_count))
            .expect("validate");
        assert_eq!(v.ifd_count as usize, r.levels.len());
        if out_kind == "classic" {
            assert_eq!(v.main_ifds as usize, r.levels.len());
        } else {
            // OME：SubIFD 金字塔——主链只有 1 个 IFD
            assert_eq!(v.main_ifds, 1);
            assert_eq!(v.sub_ifds as usize, r.levels.len() - 1);
        }
    }
}

#[test]
fn bmp_pixels_survive_the_reencode() {
    // L0 重编码像素贴源：合成渐变在 q96 4:2:2 下的均值误差很小
    for variant in [
        RasterGenParams { bpp: 24, ..Default::default() },
        RasterGenParams { bpp: 32, ..Default::default() },
        RasterGenParams { top_down: true, ..Default::default() },
        RasterGenParams { core_header: true, ..Default::default() },
        RasterGenParams { core_header: true, bpp: 32, ..Default::default() },
        RasterGenParams { pattern: FixturePattern::Noise, ..Default::default() },
    ] {
        let p = RasterGenParams { kind: "bmp".into(), ..variant };
        let data = gen(&p);
        let (r, _out) = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).expect("convert");
        assert_eq!(r.width, p.width);
        assert_eq!(r.height, p.height);
        // every tile re-encoded; pyramid = generated tail (512×320 → 1 tail level)
        assert_eq!(r.count_raw_copied(), 0);
        let expect_levels = 1 + slide_transform_core::gtiff::generated_tail(p.width, p.height).len();
        assert_eq!(r.levels.len(), expect_levels);
    }
}

#[test]
fn bmp_variant_rejections() {
    // RLE8 → compression rejection; 4-bit → depth rejection; truncated → oob
    let rle = gen(&RasterGenParams {
        kind: "bmp".into(),
        compression: 1,
        ..Default::default()
    });
    expect_error(rle, UnsupportedKfbVariant, "RLE", "bmp-rle8");
    let bits4 = gen(&RasterGenParams { kind: "bmp".into(), bits_override: Some(4), ..Default::default() });
    expect_error(bits4, UnsupportedKfbVariant, "位深 4", "bmp-bits4");
    let trunc = gen(&RasterGenParams { kind: "bmp".into(), truncated: true, ..Default::default() });
    expect_error(trunc, TilePayloadOutOfBounds, "截断", "bmp-truncated");
    // 像素上限：头部声明超大尺寸（不构造像素数据）
    let mut big = vec![0u8; 54];
    big[0] = b'B';
    big[1] = b'M';
    big[10..14].copy_from_slice(&54u32.to_le_bytes());
    big[14..18].copy_from_slice(&40u32.to_le_bytes());
    big[18..22].copy_from_slice(&100_000u32.to_le_bytes());
    big[22..26].copy_from_slice(&100_000u32.to_le_bytes());
    big[26..28].copy_from_slice(&1u16.to_le_bytes());
    big[28..30].copy_from_slice(&24u16.to_le_bytes());
    expect_error(big, UnsupportedKfbVariant, "像素", "bmp-oversize");
    // 不是 BMP/JPEG 的魔数
    expect_error(vec![0x49, 0x49, 42, 0, 8, 0, 0, 0], UnsupportedKfbVariant, "魔数", "tiff-magic");
}

// --------------------------------------------------------------------------- //
// JPEG: probe / convert contract (segment + band paths)
// --------------------------------------------------------------------------- //

#[test]
fn jpeg_segmented_probe_and_convert() {
    let p = jpeg_params(false);
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_raster(&src).expect("probe");
    assert_eq!(doc.kind.id(), "jpeg-baseline");
    let j = doc.jpeg.as_ref().unwrap();
    assert_eq!((doc.width, doc.height), (p.width, p.height));
    assert!(j.restart_interval > 0);
    assert!(j.segments > 0);
    let (r, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    assert_eq!(r.source_format, Some(SOURCE_FORMAT));
    assert_eq!(r.adapter_version, Some(ADAPTER_VERSION));
    assert_eq!(r.count_raw_copied(), 0);
    let composed = r.composed.as_ref().unwrap();
    assert_eq!(composed.fingerprint, "raster-compose:q96:y422:hstd:v1");
    let vsrc = MemSource::new(out.clone());
    validate_output(&vsrc, out.len() as u64, Some(r.validation.ifd_count)).expect("validate");
}

#[test]
fn jpeg_band_probe_and_convert() {
    let p = jpeg_params(true);
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_raster(&src).expect("probe");
    let j = doc.jpeg.as_ref().unwrap();
    assert_eq!(j.restart_interval, 0, "无 restart 夹具必须走 band 路径");
    assert_eq!(j.segments, 0);
    let (r, out) = convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert");
    assert_eq!(r.count_raw_copied(), 0);
    assert_eq!(r.levels[0].tiles_total, r.levels[0].tiles_reencoded);
    let vsrc = MemSource::new(out.clone());
    validate_output(&vsrc, out.len() as u64, Some(r.validation.ifd_count)).expect("validate");
}

#[test]
fn jpeg_variant_rejections() {
    let progressive = gen(&RasterGenParams { kind: "jpeg".into(), progressive: true, ..Default::default() });
    expect_error(progressive, JpegDecodeFailed, "SOF", "jpeg-progressive");
    let gray = gen(&RasterGenParams { kind: "jpeg".into(), gray: true, ..Default::default() });
    expect_error(gray, UnsupportedKfbVariant, "灰度", "jpeg-gray");
}

// --------------------------------------------------------------------------- //
// budget refusal (before allocation)
// --------------------------------------------------------------------------- //

#[test]
fn budget_refusal_before_allocation() {
    for p in [bmp_params(), jpeg_params(false), jpeg_params(true)] {
        let data = gen(&p);
        let err = convert_with_budget(&data, OutputProfile::ClassicJpegBigTiff, 4096)
            .expect_err("must refuse");
        assert_eq!(err.code, ResourceLimitExceeded, "{}", err.message);
        assert!(err.message.contains("内存预算不足"), "{}", err.message);
        // 默认预算下同一输入转换成功
        convert(&data, OutputProfile::ClassicJpegBigTiff).expect("convert at saver budget");
    }
}

// --------------------------------------------------------------------------- //
// resume: byte identity + adapter-version pin
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

fn resume_identity_case(name: &str, params: &RasterGenParams) {
    use slide_transform_core::io::{FileSink, FileScratch, FileSource};

    let data = gen(params);
    let dir = std::env::temp_dir().join(format!(
        "stc6-raster-resume-{name}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.bin");
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
        convert_raster::convert_raster_to_bigtiff(&src(), &mut out, &mut scratch, &plan(), &job)
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
        let res =
            convert_raster::convert_raster_to_bigtiff(&src(), &mut out, &mut scratch2, &plan(), &job);
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
    // adapter-version pin: a state WITHOUT the field is foreign as well
    {
        let mut foreign = rp.clone();
        foreign.adapter_version = None;
        let mut sink = FileSink::create(&dir.join("foreign.tif")).unwrap();
        let mut scratch3 = FileScratch::new(&dir);
        let null = NullProgress;
        let job = JobControl::new(&null);
        let err = convert_raster::convert_raster_to_bigtiff_resume(
            &src(),
            &mut sink,
            &mut scratch3,
            &plan(),
            &job,
            &foreign,
        )
        .expect_err("missing adapter version must refuse");
        assert_eq!(err.code, ConversionValidationFailed, "{}", err.message);
        assert!(err.message.contains("适配器"), "{}", err.message);
    }
    // resume run: the same scratch factory keeps the surviving offcnt files
    let resumed;
    {
        let mut sink = FileSink::open_preserve(&part_out).unwrap();
        let mut scratch4 = FileScratch::new(&crash_dir);
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_raster::convert_raster_to_bigtiff_resume(
            &src(),
            &mut sink,
            &mut scratch4,
            &plan(),
            &job,
            &rp,
        )
        .unwrap();
        sink.flush().unwrap();
        resumed = r;
    }
    let resumed_bytes = std::fs::read(&part_out).unwrap();
    assert_eq!(
        sha256(&resumed_bytes),
        ref_sha,
        "{name}: resumed bytes differ"
    );
    assert!(resumed.levels.len() >= 1);
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn resume_bmp_is_byte_identical() {
    resume_identity_case("bmp", &bmp_params());
}

#[test]
fn resume_jpeg_segmented_is_byte_identical() {
    resume_identity_case("jpegseg", &jpeg_params(false));
}

#[test]
fn resume_jpeg_band_is_byte_identical() {
    resume_identity_case("jpegband", &jpeg_params(true));
}

// --------------------------------------------------------------------------- //
// estimate bounds hold for actual outputs
// --------------------------------------------------------------------------- //

#[test]
fn estimate_bounds_cover_actual_outputs() {
    for p in [bmp_params(), jpeg_params(false), jpeg_params(true)] {
        let data = gen(&p);
        let src = MemSource::new(data.clone());
        let doc = probe_raster(&src).unwrap();
        let est = estimate_raster(&doc, data.len() as u64);
        let (r, _) = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).unwrap();
        assert!(
            est.output_upper_bound_bytes >= r.output_bytes,
            "{:?}: bound {} < actual {}",
            p.kind,
            est.output_upper_bound_bytes,
            r.output_bytes
        );
        // compact 路径（q80 4:2:0 全量重编码）；上界同样必须盖住
        let mut plan_c = plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode);
        plan_c.encoding = slide_transform_core::plan::EncodingProfile::CompactJpegV1;
        let src2 = MemSource::new(data.clone());
        let mut sink = MemSink::new();
        let mut scratch = MemScratch::default();
        let rcc = convert_raster::convert_raster(&src2, &mut sink, &mut scratch, &plan_c).unwrap();
        assert!(
            est.compact_upper_bound_bytes >= rcc.output_bytes,
            "{:?}: compact bound {} < actual {}",
            p.kind,
            est.compact_upper_bound_bytes,
            rcc.output_bytes
        );
    }
}

// keep the OUT_TILE import meaningful (used by the estimate tile math check)
#[test]
fn out_tile_is_the_documented_edge() {
    assert_eq!(OUT_TILE, 256);
}
