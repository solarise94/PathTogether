//! F4 Leica SCN adapter tests: synthetic SCN400-style fixtures (generated
//! in code, no sample bytes committed), conversion to both brightfield
//! output profiles, tile passthrough byte equality, sparse-grid fill
//! semantics, variant/routing rejections, budget refusal and resume.
//!
//! Requires `fixtures` (`cargo test -p slide-transform-core --features
//! fixtures`).

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_scn;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::error::{CoreError, CoreResult};
use slide_transform_core::io::{ByteSource, MemScratch, MemSink, MemSource, RandomAccessSink};
use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::scn::{self, estimate_scn, probe_scn, TiffVendor};
use slide_transform_core::scn_fixture::{build_synthetic_scn, DescMode, ScnGenParams};
use slide_transform_core::tiff_read;
use slide_transform_core::validate::validate_output;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

fn gen(p: &ScnGenParams) -> Vec<u8> {
    let mut sink = MemSink::new();
    build_synthetic_scn(&mut sink, p).unwrap();
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
    let r = convert_scn::convert_scn(
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
    let r = convert_scn::convert_scn(&src, &mut sink, &mut scratch, &plan)?;
    Ok((r, sink.data))
}

fn expect_scn_error(data: Vec<u8>, code: slide_transform_core::error::ErrorCode, name: &str) {
    match convert(&data, OutputProfile::ClassicJpegBigTiff) {
        Err(e) => assert_eq!(
            e.code, code,
            "{name}: got {} ({})",
            e.code.stable_code(),
            e.message
        ),
        Ok(_) => panic!("{name}: expected error {:#?}", code),
    }
    let _ = code;
}

fn default_params() -> ScnGenParams {
    ScnGenParams::default()
}

// --------------------------------------------------------------------------- //
// probe + passthrough conversion (classic profile)
// --------------------------------------------------------------------------- //

#[test]
fn probe_reports_the_main_pyramid_not_the_label() {
    let p = default_params();
    let data = gen(&p);
    let src = MemSource::new(data);
    let doc = probe_scn(&src).unwrap();
    assert_eq!(scn::SOURCE_FORMAT, "leica-scn-jpeg");
    assert_eq!(scn::ADAPTER_VERSION, "1");
    assert_eq!(doc.levels.len(), 3, "three main levels (fixture geometry)");
    assert_eq!(doc.levels[0].width, p.width);
    assert_eq!(doc.levels[0].height, p.height);
    assert_eq!(doc.levels[0].tile_w, p.tile);
    // strictly decreasing
    for w in doc.levels.windows(2) {
        assert!(w[1].width < w[0].width && w[1].height < w[0].height);
        assert!(w[1].r == w[0].r + 1);
    }
    // the label image is detected and NOT the main pyramid
    assert_eq!(doc.associated.len(), 1);
    assert_eq!(doc.associated[0].name, "label");
    // mpp from view(nm)/pixels = 500/1000 = 0.5 µm
    let mpp = doc.mpp.expect("mpp from the fixture's view/pixels");
    assert!((mpp - 0.5).abs() < 1e-9);
    assert_eq!(doc.objective, Some(20.0));
    assert_eq!(doc.illumination.as_deref(), Some("brightfield"));
    // every level of the dense fixture is fully present
    for lv in &doc.levels {
        assert_eq!(lv.tiles_present, lv.tiles_total);
        assert_eq!(lv.tiles_missing(), 0);
    }
}

#[test]
fn classic_output_tiles_are_the_source_bytes_in_order() {
    let p = default_params();
    let data = gen(&p);
    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert_eq!(out.source_format, Some(scn::SOURCE_FORMAT));
    assert_eq!(out.adapter_version, Some(scn::ADAPTER_VERSION));
    assert_eq!(out.width, p.width);
    assert_eq!(out.height, p.height);
    assert!(out.lossy_reencode.is_none());
    assert!(out
        .warnings
        .iter()
        .any(|w| w == "scn_associated_not_exported"));

    // walk the OUTPUT with the core's own bounded reader and compare every
    // output tile byte-for-byte with the source tile of the same cell
    let src = MemSource::new(data);
    let hdr = tiff_read::read_header(&src).unwrap();
    let chain = tiff_read::ifd_chain(&src, &hdr).unwrap();
    let doc = probe_scn(&src).unwrap();
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();
    assert_eq!(ochain.len(), doc.levels.len(), "one IFD per level");
    for (li, lv) in doc.levels.iter().enumerate() {
        let mut s = tiff_read::TileCursor::new(&src, &hdr, &chain[lv.ifd_index as usize]).unwrap();
        let mut o =
            tiff_read::TileCursor::new(&osrc, &ohdr, &ochain[li]).unwrap();
        let mut cell = 0u64;
        loop {
            let sp = s.next_pair_allow_zero().unwrap();
            let op = o.next_pair().unwrap();
            assert_eq!(sp.is_some(), op.is_some(), "level {li} tile count");
            let (Some((so, sl)), Some((oo, ol))) = (sp, op) else { break };
            if so == 0 && sl == 0 {
                // the fill cell: the output carries the generated payload —
                // byte-checked by the sparse test below
                cell += 1;
                continue;
            }
            let a = src.read_at(so, sl as usize).unwrap();
            let b = osrc.read_at(oo, ol as usize).unwrap();
            assert_eq!(a, b, "level {li} cell {cell} payload must be verbatim");
            cell += 1;
        }
        assert_eq!(cell, lv.tiles_total);
    }
}

#[test]
fn ome_output_profile_converts_and_validates() {
    let p = default_params();
    let data = gen(&p);
    let (out, out_bytes) = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).unwrap();
    assert_eq!(out.format, "ome-bigtiff-subifd-rgb-jpeg-pyramid");
    assert_eq!(out.width, p.width);
    let src = MemSource::new(out_bytes);
    let v = validate_output(&src, src.size(), Some(out.validation.ifd_count)).unwrap();
    assert!(v.checks.iter().any(|c| *c == "subifd-tree"));
}

#[test]
fn fluorescence_scn_is_a_typed_rejection() {
    let mut p = default_params();
    p.fluoro = true;
    let data = gen(&p);
    expect_scn_error(data, UnsupportedKfbVariant, "fluorescent SCN");
}

#[test]
fn non_jpeg_scn_is_a_typed_rejection() {
    let mut p = default_params();
    p.non_jpeg = true;
    let data = gen(&p);
    expect_scn_error(data, UnsupportedKfbVariant, "deflate SCN");
}

#[test]
fn routing_descriptions_are_rejected_before_any_copy() {
    for mode in [DescMode::None, DescMode::Ome, DescMode::Converter, DescMode::Foreign] {
        let mut p = default_params();
        p.desc_mode = mode;
        let data = gen(&p);
        expect_scn_error(
            data,
            UnsupportedKfbVariant,
            &format!("desc_mode {mode:?}"),
        );
    }
}

// --------------------------------------------------------------------------- //
// sparse grid: fill semantics
// --------------------------------------------------------------------------- //

#[test]
fn sparse_tiles_are_filled_and_counted() {
    let mut p = default_params();
    p.sparse = true;
    let data = gen(&p);
    let src = MemSource::new(data.clone());
    let doc = probe_scn(&src).unwrap();
    assert_eq!(doc.levels[0].tiles_missing(), 2, "two vanished cells");
    assert!(doc.levels[1..].iter().all(|l| l.tiles_missing() == 0));

    let (out, out_bytes) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    let l0 = out.levels.first().unwrap();
    assert_eq!(l0.tiles_filled, 2);
    assert_eq!(l0.tiles_raw_copied, doc.levels[0].tiles_present);
    assert!(out.warnings.iter().any(|w| w == "scn_missing_tiles_filled"));

    // every filled cell carries the SAME generated white tile: decode it and
    // check all-255 pixels
    let osrc = MemSource::new(out_bytes);
    let ohdr = tiff_read::read_header(&osrc).unwrap();
    let ochain = tiff_read::ifd_chain(&osrc, &ohdr).unwrap();
    let mut cur = tiff_read::TileCursor::new(&osrc, &ohdr, &ochain[0]).unwrap();
    let across = l0.tiles_across as u64;
    let mut fills = 0u64;
    while let Some((o, c)) = cur.next_pair().unwrap() {
        let cell = cur.done() - 1;
        let row = cell / across;
        let col = cell % across;
        let missing = (row == 1 && col == 1) || (row == 0 && col == across - 2);
        let bytes = osrc.read_at(o, c as usize).unwrap();
        if missing {
            fills += 1;
            let img = slide_transform_core::jpeg::decode_ex(&bytes, u64::MAX, false).unwrap();
            assert_eq!(img.width as u64, doc.levels[0].tile_w as u64);
            assert!(img.data.iter().all(|&b| b == 255), "fill tile must be white");
        } else {
            assert_ne!(
                bytes,
                slide_transform_core::jpeg::encode_rgb(
                    &vec![255u8; (doc.levels[0].tile_w * doc.levels[0].tile_h * 3) as usize],
                    doc.levels[0].tile_w,
                    doc.levels[0].tile_h,
                    &slide_transform_core::jpeg::EncoderCfg::with_quality(90, slide_transform_core::jpeg::Sampling::S444),
                )
                .unwrap(),
                "present cells must not collide with the fill payload"
            );
        }
    }
    assert_eq!(fills, 2);
}

#[test]
fn estimate_accounts_missing_tiles() {
    let mut p = default_params();
    p.sparse = true;
    let data = gen(&p);
    let src = MemSource::new(data);
    let doc = probe_scn(&src).unwrap();
    let est = estimate_scn(&doc);
    assert_eq!(est.cells_missing, 2);
    assert_eq!(est.tiles_present, est.cells_total - 2);
    assert_eq!(est.ifds, doc.levels.len() as u64);
    assert!(est.output_upper_bound_bytes > est.payload_bytes);
    assert!(est.compact_upper_bound_bytes >= est.output_upper_bound_bytes);
}

// --------------------------------------------------------------------------- //
// budget: charged BEFORE allocation
// --------------------------------------------------------------------------- //

#[test]
fn tiny_budget_is_a_typed_refusal_before_allocation() {
    let data = gen(&default_params());
    // far below the IFD-chain structural reserve → refused up front
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
    // a generous budget converts fine
    let ok = convert_with_budget(&data, OutputProfile::ClassicJpegBigTiff, 192 * 1024 * 1024);
    assert!(ok.is_ok(), "{:?}", ok.err().map(|e| e.message));
}

// --------------------------------------------------------------------------- //
// resume: byte-identical after a mid-payload crash
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
    // review #4: the core enforces the adapter-version pin the doc claims
    // (mrxs parity) — a state journalled by another SCN adapter generation
    // (or with the field stripped) must not continue
    let data = gen(&ScnGenParams { width: 300, height: 200, ..default_params() });
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
        let e = convert_scn::convert_scn_to_bigtiff_resume(&src, &mut sink, &mut scratch, &plan, &job, &rp)
            .err()
            .expect("version-mismatched resume must refuse");
        assert_eq!(e.code, slide_transform_core::error::ErrorCode::ConversionValidationFailed);
        assert!(e.message.contains("适配器"), "{}", e.message);
    }
}

#[test]
fn resume_from_mid_level_checkpoint_is_byte_identical() {
    use slide_transform_core::io::{FileScratch, FileSink, FileSource};
    use slide_transform_core::resume::ResumePoint;

    let mut p = default_params();
    p.sparse = true; // fills cross the resume path too
    let data = gen(&p);

    let dir = std::env::temp_dir().join(format!(
        "stc4-scn-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.scn");
    std::fs::write(&src_path, &data).unwrap();
    let plan = || {
        let mut pl = TransformPlan::brightfield(InputIdentity::default())
            .with_policy(PixelPolicy::AllowEdgeReencode);
        pl.profile = OutputProfile::ClassicJpegBigTiff;
        pl
    };
    let src = || FileSource::open(&src_path).unwrap();

    // reference run
    let ref_out = dir.join("ref.tif");
    let mut scratch = FileScratch::new(&dir);
    {
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_scn::convert_scn_to_bigtiff(&src(), &mut out, &mut scratch, &plan(), &job)
            .unwrap();
        out.flush().unwrap();
        let v = validate_output(&FileSource::open(&ref_out).unwrap(), r.output_bytes, None).unwrap();
        assert_eq!(v.ifd_count as u32, r.validation.ifd_count);
    }
    let ref_sha = sha256(&std::fs::read(&ref_out).unwrap());
    let (reference, _) = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    let ref_filled: u64 = reference.levels.iter().map(|l| l.tiles_filled).sum();

    // crashed run: cancel from inside checkpoint 2
    let crash_dir = dir.join("crash");
    std::fs::create_dir_all(&crash_dir).unwrap();
    let part_out = dir.join("part.tif");
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: Some(2),
        cancel: cancel.clone(),
        count: AtomicUsize::new(0),
    };
    let mut scratch2 = FileScratch::new(&crash_dir);
    {
        let mut out = FileSink::create(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
        let res = convert_scn::convert_scn_to_bigtiff(&src(), &mut out, &mut scratch2, &plan(), &job);
        match res {
            Err(e) => assert!(e.message.contains("已取消"), "unexpected err {e:?}"),
            Ok(_) => panic!("expected the injected cancel to abort"),
        }
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
    assert!(!states.is_empty(), "no checkpoints recorded");
    let last = states.last().unwrap();
    let rp = ResumePoint {
        level: last.level as usize,
        channel: last.channel.unwrap_or(0),
        cell: last.cell_done,
        committed_output: last.committed_output,
        ifd_tiles: last.ifd_tiles.clone(),
        adapter_version: Some(scn::ADAPTER_VERSION.to_string()),
    };
    // crash aftermath the host guarantees: output + offcnt scratch truncated
    // to committed
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
        let r = convert_scn::convert_scn_to_bigtiff_resume(
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
    assert_eq!(resumed.levels.len(), reference.levels.len());
    let filled: u64 = resumed.levels.iter().map(|l| l.tiles_filled).sum();
    assert_eq!(filled, ref_filled, "fill counts must survive resume");
}

/// Minimal little-endian BigTIFF: header | desc@16 | IFD (tags 256/257/259/
/// 262/270/277/284/322/323/324/325; one 16×16 white JPEG tile when `tiles`).
/// Used for the hand-built geometry/routing rejections the fixture knobs
/// cannot express.
fn hand_bigtiff(desc: &str, width: u32, height: u32, tiles: bool) -> Vec<u8> {
    use slide_transform_core::jpeg::{encode_rgb, EncoderCfg, Sampling};
    let desc = desc.as_bytes();
    let payload = tiles.then(|| {
        encode_rgb(
            &[255u8; 16 * 16 * 3],
            16,
            16,
            &EncoderCfg::with_quality(90, Sampling::S444),
        )
        .unwrap()
    });
    let payload_at = 16 + desc.len() as u64 + 1;
    let mut entries: Vec<(u16, u16, u64, Vec<u8>)> = vec![
        (256, 4, 1, width.to_le_bytes().to_vec()),
        (257, 4, 1, height.to_le_bytes().to_vec()),
        (259, 3, 1, 7u16.to_le_bytes().to_vec()),
        (262, 3, 1, 6u16.to_le_bytes().to_vec()),
        (270, 2, desc.len() as u64 + 1, 16u64.to_le_bytes().to_vec()),
        (277, 3, 1, 3u16.to_le_bytes().to_vec()),
        (284, 3, 1, 1u16.to_le_bytes().to_vec()),
    ];
    if let Some(p) = &payload {
        entries.push((322, 3, 1, 16u16.to_le_bytes().to_vec()));
        entries.push((323, 3, 1, 16u16.to_le_bytes().to_vec()));
        entries.push((324, 16, 1, payload_at.to_le_bytes().to_vec()));
        entries.push((325, 16, 1, (p.len() as u64).to_le_bytes().to_vec()));
    }
    entries.sort_by_key(|e| e.0);
    let ifd_at = payload_at + payload.as_ref().map_or(0, |p| p.len() as u64);
    let mut buf: Vec<u8> = Vec::new();
    buf.extend_from_slice(b"II");
    buf.extend_from_slice(&43u16.to_le_bytes());
    buf.extend_from_slice(&8u16.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&ifd_at.to_le_bytes());
    buf.extend_from_slice(desc);
    buf.push(0);
    if let Some(p) = payload {
        buf.extend_from_slice(&p);
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

fn scn_xml_desc(size_x: u64, size_y: u64) -> String {
    format!(
        "<?xml version=\"1.0\"?><scn xmlns=\"http://www.leica-microsystems.com/scn/2010/10/01\">\
<collection><image><pixels sizeX=\"{sx}\" sizeY=\"{sy}\">\
<dimension sizeX=\"{sx}\" sizeY=\"{sy}\" r=\"0\" ifd=\"0\" /></pixels>\
<view sizeX=\"{vx}\" sizeY=\"{vy}\" /><scanSettings><illuminationSettings>\
<illuminationSource>brightfield</illuminationSource></illuminationSettings>\
</scanSettings></image></collection></scn>",
        sx = size_x,
        sy = size_y,
        vx = size_x * 500,
        vy = size_y * 500,
    )
}

#[test]
fn absurd_level_side_is_a_typed_rejection() {
    // review #3: MAX_SIDE must bound each side (SVS parity) — a
    // u32-max × 1 level fits the 4 M tile-grid cap but is not a slide
    let data = hand_bigtiff(&scn_xml_desc(4_294_967_295, 1), 4_294_967_295, 1, true);
    let src = MemSource::new(data);
    let e = probe_scn(&src).unwrap_err();
    assert_eq!(e.code, UnsupportedKfbVariant, "{}", e.message);
    assert!(e.message.contains("越界"), "{}", e.message);
}

#[test]
fn vendor_routing_on_hand_built_containers() {
    // SCN XML routes to the SCN adapter (probe succeeds on the mini slide)
    let ok = MemSource::new(hand_bigtiff(&scn_xml_desc(16, 16), 16, 16, true));
    let doc = probe_scn(&ok).unwrap();
    assert_eq!(doc.levels[0].width, 16);
    // OME description → sniff_tiff_vendor classifies OmeTiff (the CLI/wasm
    // layers turn that into the typed "OME-TIFF 不是转换输入" refusal before
    // any adapter walk); probe_scn itself answers with the SCN adapter's own
    // typed rejection — also before any payload is copied
    let ome_bytes = hand_bigtiff(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\"></OME>",
        520, 300, false,
    );
    let ome = MemSource::new(ome_bytes.clone());
    assert_eq!(
        scn::sniff_tiff_vendor(&ome).unwrap(),
        TiffVendor::OmeTiff
    );
    let e = probe_scn(&MemSource::new(ome_bytes)).unwrap_err();
    assert_eq!(e.code, UnsupportedKfbVariant);
    assert!(e.message.contains("不是 Leica SCN XML"), "{}", e.message);
}

// --------------------------------------------------------------------------- //
// vendor routing classifier
// --------------------------------------------------------------------------- //

#[test]
fn vendor_classifier_routes_by_description() {
    assert_eq!(
        scn::classify_description("Aperio Image Library v11.2.1"),
        TiffVendor::AperioSvs
    );
    assert_eq!(
        scn::classify_description(
            "<?xml version=\"1.0\"?><scn xmlns=\"http://www.leica-microsystems.com/scn/2010/10/01\"></scn>"
        ),
        TiffVendor::LeicaScn
    );
    assert_eq!(
        scn::classify_description(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?><OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\"></OME>"
        ),
        TiffVendor::OmeTiff
    );
    assert_eq!(
        scn::classify_description(
            "{\"adapter\": \"aperio-svs-jpeg\", \"source_format\": \"aperio-svs-jpeg\"}\u{0}"
        ),
        TiffVendor::ConverterBigTiff
    );
    assert_eq!(
        scn::classify_description(
            "{\"adapter\": \"leica-scn-jpeg\", \"source_format\": \"leica-scn-jpeg\"}\u{0}"
        ),
        TiffVendor::ConverterBigTiff
    );
    assert_eq!(scn::classify_description("Some Other Scanner v1"), TiffVendor::Unknown);
    assert_eq!(scn::classify_description(""), TiffVendor::Unknown);
    // OME wins over a coincidental Aperio mention (it is checked first)
    assert_eq!(
        scn::classify_description("<?xml?><OME xmlns=\"…OME…\">Aperio</OME>"),
        TiffVendor::OmeTiff
    );
}

// --------------------------------------------------------------------------- //
// bounded-cursor semantics: (0,0) pairs
// --------------------------------------------------------------------------- //

#[test]
fn tile_cursor_zero_semantics() {
    let mut p = default_params();
    p.sparse = true;
    let data = gen(&p);
    let src = MemSource::new(data);
    let hdr = tiff_read::read_header(&src).unwrap();
    let chain = tiff_read::ifd_chain(&src, &hdr).unwrap();
    let doc = probe_scn(&src).unwrap();
    let l0 = &doc.levels[0];
    let ifd = &chain[l0.ifd_index as usize];
    let mut raw = tiff_read::TileCursor::new(&src, &hdr, ifd).unwrap();
    let mut zeros = 0u64;
    let mut present = 0u64;
    while let Some((o, c)) = raw.next_pair_allow_zero().unwrap() {
        if o == 0 && c == 0 {
            zeros += 1;
        } else {
            present += 1;
        }
    }
    assert_eq!(zeros, 2);
    assert_eq!(present, l0.tiles_present);
    // the strict cursor must reject the zero entries
    let mut strict = tiff_read::TileCursor::new(&src, &hdr, ifd).unwrap();
    let r: CoreResult<()> = loop {
        match strict.next_pair() {
            Ok(Some((0, _))) | Ok(Some((_, 0))) => break Err(CoreError::io("zero leaked")),
            Ok(Some(_)) => continue,
            Ok(None) => break Ok(()),
            Err(_) => break Ok(()), // zero rejected as an error is fine too
        }
    };
    assert!(r.is_ok(), "strict cursor must never return a zero pair");
}
