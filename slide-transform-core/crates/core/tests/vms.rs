//! Hamamatsu VMS bundle adapter tests: synthetic fixtures (probe geometry,
//! missing members, variant refusals, budget refusal, pixel identity with
//! the fixture's global pattern, l0-box2 pyramid consistency, resume
//! byte-identity).

use slide_transform_core::bundle::MemBundle;
use slide_transform_core::convert_vms::{
    convert_vms_to_bigtiff, convert_vms_to_bigtiff_resume,
};
use slide_transform_core::vms::{
    OUT_TILE, PRESERVE_COMPOSE_FINGERPRINT, PYRAMID_METHOD,
};
use slide_transform_core::io::{MemScratch, MemSink};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan,
};
use slide_transform_core::resume::ResumePoint;
use slide_transform_core::vms::{estimate_vms, probe_vms, probe_vms_with_budget};
use slide_transform_core::vms_fixture::{build_synthetic_vms, VmsGenParams};

fn plan_for(profile: OutputProfile, policy: PixelPolicy) -> TransformPlan {
    let mut p = TransformPlan::brightfield(Default::default()).with_policy(policy);
    p.profile = profile;
    p
}

fn convert(
    fs: &MemBundle,
    profile: OutputProfile,
) -> slide_transform_core::error::CoreResult<(Vec<u8>, slide_transform_core::report::TransformResult)>
{
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_for(profile, PixelPolicy::AllowEdgeReencode);
    let r = convert_vms_to_bigtiff(fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress))?;
    Ok((sink.data, r))
}

fn code_of(r: &slide_transform_core::error::CoreError) -> String {
    r.code.stable_code().to_string()
}

fn default_params() -> VmsGenParams {
    VmsGenParams {
        macro_image: true,
        map_file: true,
        opt_file: true,
        ..Default::default()
    }
}

// ------------------------------------------------------------------ probe --

#[test]
fn probe_geometry_metadata_and_optional_members() {
    let fs = build_synthetic_vms(&default_params()).unwrap();
    let doc = probe_vms(&fs, "synthetic").unwrap();
    // mosaic: cols [96, 64] → 160; rows [80, 48] → 128
    assert_eq!((doc.width, doc.height), (160, 128));
    assert_eq!((doc.cols, doc.rows), (2, 2));
    assert_eq!(doc.tiles.len(), 4);
    let t10 = doc.tiles.iter().find(|t| t.col == 1 && t.row == 0).unwrap();
    assert_eq!((t10.width, t10.height), (64, 80));
    assert_eq!((t10.x0, t10.y0), (96, 0));
    let t01 = doc.tiles.iter().find(|t| t.col == 0 && t.row == 1).unwrap();
    assert_eq!((t01.x0, t01.y0), (0, 80));
    // MCU geometry: S422 → 16×8; DRI = restart_rows × mcus_x
    assert_eq!((t10.mcu_w(), t10.mcu_h()), (16, 8));
    assert_eq!(t10.mcus_x, 64 / 16);
    assert_eq!(t10.restart_interval, t10.mcus_x);
    assert!(t10.segments > 0);
    // calibration: mpp = PhysicalWidth/(1000·width)
    let (mw, mh) = default_params().mosaic_dims();
    let (pw, ph) = default_params().physical_nm.unwrap();
    assert!((doc.mpp.unwrap().0 - pw / (1000.0 * mw as f64)).abs() < 1e-12);
    assert!((doc.mpp.unwrap().1 - ph / (1000.0 * mh as f64)).abs() < 1e-12);
    assert_eq!(doc.objective, Some(40.0));
    // optional members detected: macro associated (not exported), map/opt
    // presence reported
    assert_eq!(doc.associated.len(), 1);
    assert_eq!(doc.associated[0].name, "macro");
    assert!(doc.map_present && doc.opt_present);
    // output pyramid tail = l0-box2 generated chain; a 160×128 mosaic is
    // already ≤ 256 per side → no generated levels
    assert!(doc.generated.is_empty());
    // a larger mosaic gets the ÷2 chain with the last level ≤ 256 per side
    let big = build_synthetic_vms(&VmsGenParams {
        widths: vec![256, 256],
        heights: vec![256, 256],
        cols: 2,
        rows: 2,
        ..default_params()
    })
    .unwrap();
    let big_doc = probe_vms(&big, "synthetic").unwrap();
    assert_eq!(big_doc.generated, vec![(256, 256)]);
}

#[test]
fn missing_entry_or_members_listed_before_any_conversion() {
    // entry missing
    let mut fs = build_synthetic_vms(&default_params()).unwrap();
    fs.remove("synthetic.vms");
    let e = probe_vms(&fs, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "conversion_validation_failed");
    assert!(e.message.contains("synthetic.vms"));

    // one tile member missing → the name is LISTED (not a generic failure)
    let mut fs = build_synthetic_vms(&default_params()).unwrap();
    fs.remove("synthetic-1-1.jpg");
    let e = probe_vms(&fs, "synthetic").unwrap_err();
    assert!(e.message.contains("缺少成员"), "{}", e.message);
    assert!(e.message.contains("synthetic-1-1.jpg"), "{}", e.message);

    // two members missing → both listed in one refusal
    let mut fs = build_synthetic_vms(&default_params()).unwrap();
    fs.remove("synthetic-1-1.jpg");
    fs.remove("synthetic-0-1.jpg");
    let e = probe_vms(&fs, "synthetic").unwrap_err();
    assert!(e.message.contains("synthetic-1-1.jpg") && e.message.contains("synthetic-0-1.jpg"));

    // the conversion refuses identically (before any output byte)
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    let e = convert_vms_to_bigtiff(&fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap_err();
    assert!(e.message.contains("缺少成员"));
}

#[test]
fn entry_without_vms_group_is_typed() {
    // a bare INI with neither group → "不是 VMS 包"
    let mut fs = MemBundle::new();
    fs.push("s.vms", b"[Other]\nNoLayers=1\n".to_vec());
    let e = probe_vms(&fs, "s").unwrap_err();
    assert_eq!(code_of(&e), "unsupported_kfb_variant");
    assert!(e.message.contains("不是 VMS 包"), "{}", e.message);

    // the VMU group → the dedicated VMU refusal
    let mut fs = MemBundle::new();
    fs.push("s.vms", b"[Uncompressed Virtual Microscope Specimen]\nNoLayers=1\n".to_vec());
    let e = probe_vms(&fs, "s").unwrap_err();
    assert!(e.message.contains("VMU"), "{}", e.message);

    // a .vmu ENTRY file is not a VMS entry either (the bundle layer wants
    // <stem>.vms; the browser layer routes .vmu to the same typed refusal
    // before any copy)
    let mut fs2 = MemBundle::new();
    fs2.push("s.vmu", b"[Virtual Microscope Specimen]\nNoLayers=1\n".to_vec());
    let e = probe_vms(&fs2, "s").unwrap_err();
    assert!(e.message.contains("缺少主入口"), "{}", e.message);
}

#[test]
fn variant_refusals_before_decode() {
    // no restart markers → no bounded decode unit
    let fs = build_synthetic_vms(&VmsGenParams { no_restart: true, ..default_params() }).unwrap();
    let e = probe_vms(&fs, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "unsupported_kfb_variant");
    assert!(e.message.contains("restart marker"), "{}", e.message);

    // progressive SOF on tile (0,0)
    let fs = build_synthetic_vms(&VmsGenParams { progressive: true, ..default_params() }).unwrap();
    let e = probe_vms(&fs, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "jpeg_decode_failed");
    assert!(e.message.contains("SOF"), "{}", e.message);

    // multi-layer (NoLayers=2) — OpenSlide only accepts 1 either
    let fs = build_synthetic_vms(&VmsGenParams { multi_layer: true, ..default_params() }).unwrap();
    let e = probe_vms(&fs, "synthetic").unwrap_err();
    assert!(e.message.contains("NoLayers=2"), "{}", e.message);

    // traversal file name in the INI
    let fs = build_synthetic_vms(&VmsGenParams { traversal_name: true, ..default_params() }).unwrap();
    let e = probe_vms(&fs, "synthetic").unwrap_err();
    assert!(e.message.contains("路径穿越"), "{}", e.message);
}

#[test]
fn memory_budget_refusal_before_allocation() {
    let fs = build_synthetic_vms(&default_params()).unwrap();
    // a budget below the per-tile JPEG head retention refuses BEFORE any
    // decode (stable resource_profile_insufficient)
    let e = probe_vms_with_budget(&fs, "synthetic", 1024).unwrap_err();
    assert_eq!(code_of(&e), "resource_profile_insufficient");
    assert!(e.message.contains("内存预算不足"), "{}", e.message);
    // …and the conversion entry carries the same refusal
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let mut plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    plan.limits.memory_budget_bytes = 1024;
    let e = convert_vms_to_bigtiff(&fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap_err();
    assert_eq!(code_of(&e), "resource_profile_insufficient");
    // default budget converts fine
    let (bytes, _) = convert(&fs, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert!(!bytes.is_empty());
}

// --------------------------------------------------------------- estimate --

#[test]
fn estimate_bounds_cover_a_real_conversion() {
    let fs = build_synthetic_vms(&default_params()).unwrap();
    let doc = probe_vms(&fs, "synthetic").unwrap();
    let est = estimate_vms(&doc);
    assert!(est.output_upper_bound_bytes > est.payload_bytes);
    assert!(est.compact_upper_bound_bytes > est.payload_bytes);
    let (_, r) = convert(&fs, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert!(
        est.output_upper_bound_bytes >= r.output_bytes,
        "preserve 上界 {} < 实际 {}",
        est.output_upper_bound_bytes,
        r.output_bytes
    );
}

// ------------------------------------------------------------- conversion --

mod tiffwalk {
    pub struct Ifd {
        pub offsets: Vec<u64>,
        pub counts: Vec<u32>,
        pub width: u32,
        pub height: u32,
    }
    fn u16_at(b: &[u8], at: usize) -> u16 { u16::from_le_bytes([b[at], b[at + 1]]) }
    fn u32_at(b: &[u8], at: usize) -> u32 {
        u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
    }
    fn u64_at(b: &[u8], at: usize) -> u64 {
        let mut v = [0u8; 8];
        v.copy_from_slice(&b[at..at + 8]);
        u64::from_le_bytes(v)
    }
    fn read_int_array<T: From<u64> + Copy>(
        data: &[u8],
        e: usize,
        typ: u16,
        count: usize,
    ) -> Vec<T> {
        let type_size = if typ == 16 { 8usize } else { 4 };
        let total = count * type_size;
        let base = if total <= 8 { e + 12 } else { u64_at(data, e + 12) as usize };
        (0..count)
            .map(|k| {
                if type_size == 8 {
                    T::from(u64_at(data, base + k * 8))
                } else {
                    T::from(u32_at(data, base + k * 4) as u64)
                }
            })
            .collect()
    }

    pub fn classic_chain(data: &[u8]) -> Vec<Ifd> {
        assert_eq!(&data[0..2], b"II");
        assert_eq!(u16_at(data, 2), 43);
        let mut at = u64_at(data, 8) as usize;
        let mut out = Vec::new();
        while at != 0 {
            let n = u64_at(data, at) as usize;
            let mut ifd = Ifd { offsets: Vec::new(), counts: Vec::new(), width: 0, height: 0 };
            for i in 0..n {
                let e = at + 8 + i * 20;
                let tag = u16_at(data, e);
                let typ = u16_at(data, e + 2);
                let count = u64_at(data, e + 4) as usize;
                match tag {
                    256 => ifd.width = u32_at(data, e + 12),
                    257 => ifd.height = u32_at(data, e + 12),
                    324 => ifd.offsets = read_int_array::<u64>(data, e, typ, count),
                    325 => {
                        ifd.counts = read_int_array::<u64>(data, e, typ, count)
                            .into_iter().map(|v| v as u32).collect();
                    }
                    _ => {}
                }
            }
            at = u64_at(data, at + 8 + n * 20) as usize;
            out.push(ifd);
        }
        out
    }
}

/// Decode one tile payload of a classic-profile output.
fn decode_tile(data: &[u8], li: usize, idx: usize) -> (u32, u32, Vec<u8>) {
    let ifd = &tiffwalk::classic_chain(data)[li];
    let off = ifd.offsets[idx];
    let cnt = ifd.counts[idx] as usize;
    let img = slide_transform_core::jpeg::decode_ex(
        &data[off as usize..off as usize + cnt],
        (OUT_TILE as u64) * (OUT_TILE as u64),
        false,
    )
    .unwrap();
    let rgb = match img.kind {
        slide_transform_core::jpeg::ColorKind::Rgb => img.data,
        slide_transform_core::jpeg::ColorKind::Gray => {
            let mut v = Vec::with_capacity(img.data.len() * 3);
            for &g in &img.data {
                v.extend_from_slice(&[g, g, g]);
            }
            v
        }
    };
    (img.width, img.height, rgb)
}

fn box2_tile(w: usize, rgb: &[u8]) -> Vec<u8> {
    // 512×512 → 256×256 floor box (the converter's exact arithmetic)
    let mut out = vec![0u8; 256 * 256 * 3];
    for y in 0..256 {
        for x in 0..256 {
            for c in 0..3 {
                let acc = rgb[(2 * y * w + 2 * x) * 3 + c] as u32
                    + rgb[(2 * y * w + 2 * x + 1) * 3 + c] as u32
                    + rgb[((2 * y + 1) * w + 2 * x) * 3 + c] as u32
                    + rgb[((2 * y + 1) * w + 2 * x + 1) * 3 + c] as u32;
                out[(y * 256 + x) * 3 + c] = (acc >> 2) as u8;
            }
        }
    }
    out
}

/// The fixture's GLOBAL level-0 pattern (vms_fixture::tile_pixels maths) —
/// the mosaic content is known exactly by construction.
fn pattern_at(x: i64, y: i64) -> [u8; 3] {
    let g = 40 + ((x + y) / 8).clamp(0, 120);
    let dx = (x.rem_euclid(32) - 16).abs();
    let dy = (y.rem_euclid(32) - 16).abs();
    if dx + dy <= 2 {
        [255, 255, 255]
    } else {
        let b = g as u8;
        [b, (b as u16 + 17).min(255) as u8, (b as u16 + 43).min(255) as u8]
    }
}

#[test]
fn convert_both_profiles_report_contract_and_pixels() {
    let fs = build_synthetic_vms(&default_params()).unwrap();
    let (bytes, r) = convert(&fs, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert_eq!(r.source_format, Some("hamamatsu-vms-bundle"));
    assert_eq!(r.adapter_version, Some("1"));
    assert_eq!(r.width, 160);
    assert_eq!(r.height, 128);
    // every output tile is a compose re-encode; the composed summary carries
    // the fingerprint and the l0-box2 pyramid id
    assert_eq!(r.count_raw_copied(), 0);
    assert_eq!(
        r.count_reencoded(),
        r.levels.iter().map(|l| l.tiles_total).sum::<u64>()
    );
    let composed = r.composed.as_ref().unwrap();
    assert_eq!(composed.mode, "mosaic-compose-reencode");
    assert_eq!(composed.fingerprint, PRESERVE_COMPOSE_FINGERPRINT);
    assert_eq!(composed.pyramid, PYRAMID_METHOD);
    // L0 only for a 160×128 mosaic (≤ 256 per side → no generated tail)
    assert_eq!(r.levels[0].width, 160);
    assert_eq!(r.levels.len(), 1);
    // macro detected, not exported
    assert!(r.associated.iter().any(|a| a.name == "macro"));

    // ---- L0 pixels match the fixture's global pattern (q96 4:2:2 noise) -- //
    let ifds = tiffwalk::classic_chain(&bytes);
    assert_eq!(ifds.len(), r.levels.len());
    let (across, down) = (r.levels[0].tiles_across as usize, r.levels[0].tiles_down as usize);
    let mut worst = 0i64;
    for cell in 0..(across * down) {
        let (tw, _th, t) = decode_tile(&bytes, 0, cell);
        let (tx, ty) = ((cell % across) * 256, (cell / across) * 256);
        for y in 4..128usize.min(r.height as usize - ty as usize) {
            for x in 4..160usize.min(r.width as usize - tx as usize) {
                let ax = (tx + x) as i64;
                let ay = (ty + y) as i64;
                // exclude the crosses' neighbourhood (a full-contrast step
                // feature: ringing inside its 16×8 MCU is JPEG behaviour,
                // not a placement error — a mis-stitched tile shows up far
                // from any cross as a ≥ 30-level misalignment)
                let dx = (ax.rem_euclid(32) - 16).abs();
                let dy = (ay.rem_euclid(32) - 16).abs();
                if dx <= 9 && dy <= 9 {
                    continue;
                }
                let pat = pattern_at(ax, ay);
                let o = (y * tw as usize + x) * 3;
                for c in 0..3 {
                    let d = (t[o + c] as i64 - pat[c] as i64).abs();
                    worst = worst.max(d);
                }
            }
        }
    }
    // a misplaced/mis-stitched tile would misalign the gradient by tens of
    // levels; q96 4:2:2 re-encode noise stays far below
    assert!(worst <= 12, "L0 pattern deviation {worst} exceeds re-encode noise");

    // ---- OME profile: report contract holds ------------------------------ //
    let (_, r_ome) = convert(&fs, OutputProfile::OmeBigTiffRgbSubifd).unwrap();
    assert_eq!(r_ome.source_format, Some("hamamatsu-vms-bundle"));
    assert_eq!(r_ome.composed.as_ref().unwrap().fingerprint, PRESERVE_COMPOSE_FINGERPRINT);

    // ---- compact: the lossy summary is present --------------------------- //
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let mut plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    plan.encoding = EncodingProfile::CompactJpegV1;
    let r_c = convert_vms_to_bigtiff(&fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap();
    assert_eq!(r_c.lossy_reencode.as_ref().unwrap().profile, "compact-jpeg-v1");

    // ---- strict-lossless is a typed refusal (before any output byte) ----- //
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::StrictLossless);
    let e = convert_vms_to_bigtiff(&fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap_err();
    assert_eq!(code_of(&e), "pixel_policy_violation");

    // ---- fluorescence profile is refused --------------------------------- //
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_for(OutputProfile::OmeBigTiffSubifd, PixelPolicy::AllowEdgeReencode);
    let e = convert_vms_to_bigtiff(&fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap_err();
    assert!(e.message.contains("荧光"), "{}", e.message);
}

#[test]
fn reduced_level_is_box2_of_the_output_l0_within_jpeg_noise() {
    // a mosaic of exactly 4 L0 output tiles: 512×512. The reduced level is
    // the box downsample of the DECODED L0 payloads, re-encoded once more —
    // the only allowed difference is that final JPEG generation error.
    let fs = build_synthetic_vms(&VmsGenParams {
        widths: vec![256, 256],
        heights: vec![256, 256],
        cols: 2,
        rows: 2,
        ..default_params()
    })
    .unwrap();
    let doc = probe_vms(&fs, "synthetic").unwrap();
    assert_eq!((doc.width, doc.height), (512, 512));
    let (bytes, r) = convert(&fs, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert!(r.levels.len() >= 2);
    let l1 = &r.levels[1];
    assert_eq!((l1.width, l1.height), (256, 256));
    let mut canvas = vec![0u8; 512 * 512 * 3];
    for pty in 0..2usize {
        for ptx in 0..2usize {
            let (_, _, t) = decode_tile(&bytes, 0, pty * 2 + ptx);
            for row in 0..256 {
                let s = row * 256 * 3;
                let d = (pty * 256 + row) * 512 * 3 + ptx * 256 * 3;
                canvas[d..d + 256 * 3].copy_from_slice(&t[s..s + 256 * 3]);
            }
        }
    }
    let want = box2_tile(512, &canvas);
    let mut worst = 0i64;
    for cell in 0..l1.tiles_total as usize {
        let (_, _, got) = decode_tile(&bytes, 1, cell);
        for y in 0..256usize {
            for x in 0..256usize {
                // exclude the crosses' neighbourhood (in L1 coordinates the
                // L0 crosses sit at ≡ 8 mod 16): a full-contrast step's
                // chroma subsampling rings on its final encode too
                let dx = (((x as i64) * 2 + 16).rem_euclid(32) - 16).abs();
                let dy = (((y as i64) * 2 + 16).rem_euclid(32) - 16).abs();
                if dx <= 18 && dy <= 18 {
                    continue;
                }
                for c in 0..3 {
                    let got_v = got[(y * 256 + x) * 3 + c] as i64;
                    let want_v = want[(y * 256 + x) * 3 + c] as i64;
                    worst = worst.max((got_v - want_v).abs());
                }
            }
        }
    }
    // one extra q96 4:2:2 generation on an already-encoded source, away
    // from the step features: a wrong pyramid source (scanner map image or
    // a misaligned L0) diverges by far more
    assert!(worst <= 4, "L1 vs L0-box2 deviation {worst}");
}

// ------------------------------------------------------------------ resume --

#[test]
fn resume_cuts_are_byte_identical() {
    use slide_transform_core::io::{FileScratch, FileSink};
    use slide_transform_core::job::{CancelFlag, CheckpointState};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    struct Collector {
        states: Mutex<Vec<CheckpointState>>,
        cancel_after: Option<usize>,
        cancel: CancelFlag,
        count: AtomicUsize,
    }
    impl slide_transform_core::job::CheckpointCallback for Collector {
        fn on_checkpoint(&self, c: &CheckpointState) {
            let n = self.count.fetch_add(1, Ordering::SeqCst) + 1;
            self.states.lock().unwrap().push(c.clone());
            if let Some(k) = self.cancel_after {
                if n >= k {
                    self.cancel.cancel();
                }
            }
        }
    }

    let dir = std::env::temp_dir().join(format!(
        "stf-vms-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    // 512×768 mosaic → 2×3 L0 output tiles + a 256×384 l0-box2 level
    // (the reduced level needs ≥ 2 tile rows so the last cut still leaves a
    // job.check() boundary to cancel at)
    let fs = build_synthetic_vms(&VmsGenParams {
        widths: vec![256, 256],
        heights: vec![256, 256, 256],
        cols: 2,
        rows: 3,
        ..default_params()
    })
    .unwrap();
    let plan = plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode);
    let ref_out = dir.join("ref.ome.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        convert_vms_to_bigtiff(&fs, "synthetic", &mut out, &mut scratch, &plan,
            &JobControl::new(&NullProgress)).unwrap();
        use slide_transform_core::io::RandomAccessSink;
        out.flush().unwrap();
    }
    let full = std::fs::read(&ref_out).unwrap();

    // pass 1: collect the checkpoint layout (no cancellation)
    let probe = Collector {
        states: Mutex::new(Vec::new()),
        cancel_after: None,
        cancel: CancelFlag::new(),
        count: AtomicUsize::new(0),
    };
    {
        let pdir = dir.join("probe");
        std::fs::create_dir_all(&pdir).unwrap();
        let mut scratch = FileScratch::new(&pdir);
        let mut out = FileSink::create(&dir.join("probe.tif")).unwrap();
        let job = JobControl::new(&NullProgress).with_checkpoint(&probe);
        convert_vms_to_bigtiff(&fs, "synthetic", &mut out, &mut scratch, &plan, &job).unwrap();
    }
    let states = probe.states.lock().unwrap().clone();
    let n_l0 = states.iter().take_while(|s| s.level == 0).count();
    assert!(n_l0 >= 2 && states.len() > n_l0, "checkpoint layout too small");
    let cuts: Vec<(&str, usize)> = vec![
        ("l0-mid", 1),         // inside level 0
        ("l0-boundary", n_l0), // exactly the last level-0 row
        ("reduced", n_l0 + 1), // inside the first reduced level
    ];
    for (name, cancel_at) in cuts {
        let crash_dir = dir.join(format!("crash-{name}"));
        std::fs::create_dir_all(&crash_dir).unwrap();
        let part_out = dir.join(format!("part-{name}.ome.tif"));
        let cancel = CancelFlag::new();
        let collector = Collector {
            states: Mutex::new(Vec::new()),
            cancel_after: Some(cancel_at),
            cancel: cancel.clone(),
            count: AtomicUsize::new(0),
        };
        // ONE scratch instance for the crashed AND the resumed run (the
        // crashed run's offcnt streams must survive into the resume)
        let mut crash_scratch = FileScratch::new(&crash_dir);
        {
            let mut out = FileSink::create(&part_out).unwrap();
            let job = JobControl::new(&NullProgress).with_cancel(cancel).with_checkpoint(&collector);
            let res = convert_vms_to_bigtiff(&fs, "synthetic", &mut out, &mut crash_scratch, &plan, &job);
            assert!(res.unwrap_err().message.contains("已取消"), "{name}: expected cancel");
            use slide_transform_core::io::RandomAccessSink;
            out.flush().unwrap();
        }
        let st = {
            let got = collector.states.lock().unwrap();
            let st = &got[cancel_at - 1];
            assert!(st.cell_done > 0, "{name}: nothing committed");
            st.clone()
        };
        // crash aftermath: truncate output + begun offcnt streams (worker.js)
        {
            let f = std::fs::OpenOptions::new().write(true).open(&part_out).unwrap();
            f.set_len(st.committed_output).unwrap();
        }
        for (i, &tiles) in st.ifd_tiles.iter().enumerate() {
            let f = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .open(crash_dir.join(format!(".kfb2tiff-scratch-offcnt-l{i}")))
                .unwrap();
            f.set_len(tiles * 12).unwrap();
        }
        {
            let mut out = FileSink::open_preserve(&part_out).unwrap();
            let resume = ResumePoint {
                level: st.level as usize,
                channel: 0,
                cell: st.cell_done,
                committed_output: st.committed_output,
                ifd_tiles: st.ifd_tiles.clone(),
                adapter_version: Some("1".to_string()),
            };
            resume.validate().unwrap();
            convert_vms_to_bigtiff_resume(&fs, "synthetic", &mut out, &mut crash_scratch, &plan,
                &JobControl::new(&NullProgress), &resume).unwrap();
            use slide_transform_core::io::RandomAccessSink;
            out.flush().unwrap();
        }
        drop(crash_scratch);
        let resumed = std::fs::read(&part_out).unwrap();
        assert_eq!(resumed.len(), full.len(), "{name}: length mismatch");
        let first = resumed.iter().zip(full.iter()).position(|(a, b)| a != b);
        assert!(first.is_none(), "{name}: first diff at {first:?} of {}", full.len());
        let _ = std::fs::remove_file(&part_out);
        let _ = std::fs::remove_dir_all(&crash_dir);
    }

    // a checkpoint without the adapter-version field (a foreign generation)
    // is refused (never mix two adapter generations into one output)
    let states = probe.states.lock().unwrap().clone();
    let last = states.last().expect("checkpoint").clone();
    let mut scratch = FileScratch::new(&dir);
    let mut out = FileSink::create(&dir.join("foreign.ome.tif")).unwrap();
    let foreign = ResumePoint {
        level: last.level as usize,
        channel: 0,
        cell: last.cell_done,
        committed_output: last.committed_output,
        ifd_tiles: last.ifd_tiles.clone(),
        adapter_version: None,
    };
    let e = convert_vms_to_bigtiff_resume(&fs, "synthetic", &mut out, &mut scratch, &plan,
        &JobControl::new(&NullProgress), &foreign).unwrap_err();
    assert!(e.message.contains("适配器"), "{}", e.message);
}
