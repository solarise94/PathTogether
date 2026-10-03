//! Native resume-correctness tests (C2): for a spread of crash points, the
//! output after resuming from the last committed checkpoint must be
//! byte-identical (whole-file sha256) to an uninterrupted run, and the
//! TransformResult must match modulo elapsed time. The "crash" is a cancel
//! fired from a CheckpointCallback; the committed state is the checkpoint
//! recorded at that instant (not the cancel point — exactly what a journal
//! would hold after a real kill between data flush and journal commit).

use slide_transform_core::convert_bf::convert_kfb_to_bigtiff_resume;
use slide_transform_core::convert_fl::convert_kfbf_to_ome_resume;
use slide_transform_core::error::CoreError;
use slide_transform_core::io::{
    ByteSource, FileScratch, FileSink, FileSource, RandomAccessSink, ScratchFactory,
};
use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
use slide_transform_core::kfb::MAGIC as KFB_MAGIC;
use slide_transform_core::plan::{InputIdentity, OutputProfile, TransformPlan};
use slide_transform_core::resume::{parse_resume_json, ResumePoint};
use slide_transform_core::synth_gen::build_synthetic_kfb;
use slide_transform_core::validate::validate_output;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

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

/// Stop::At(k) cancels from inside checkpoint k (crash mid-payload);
/// Stop::Never lets the run finish and the caller resumes from the LAST
/// checkpoint (crash between the final data flush and finalize).

fn tmpdir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "stc2-resume-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn sha256_file(p: &std::path::Path) -> String {
    let src = FileSource::open(p).unwrap();
    slide_transform_core::validate::stream_sha256(&src, src.size()).unwrap()
}

fn truncate_file(p: &std::path::Path, len: u64) {
    let f = std::fs::OpenOptions::new().write(true).open(p).unwrap();
    f.set_len(len).unwrap();
}

/// Drive one BF conversion, cancelling after checkpoint `stop` (None =
/// complete, then resume from the last checkpoint = finalize-phase crash);
/// the resumed output must equal the uninterrupted bytes.
fn bf_case(width: u32, height: u32, stop: Option<usize>) {
    bf_case_full(width, height, stop, OutputProfile::ClassicJpegBigTiff,
        slide_transform_core::plan::EncodingProfile::PreserveSource);
}

fn bf_case_profile(width: u32, height: u32, stop: Option<usize>, profile: OutputProfile) {
    bf_case_full(width, height, stop, profile,
        slide_transform_core::plan::EncodingProfile::PreserveSource);
}

/// U3: the same crash/resume equality under the compact encoding — a resumed
/// compact job must equal an uninterrupted compact run byte-for-byte and
/// report-identically (never mixing encode generations).
fn bf_case_encoding(width: u32, height: u32, stop: Option<usize>,
    encoding: slide_transform_core::plan::EncodingProfile) {
    bf_case_full(width, height, stop, OutputProfile::OmeBigTiffRgbSubifd, encoding);
}

fn bf_case_full(width: u32, height: u32, stop: Option<usize>, profile: OutputProfile,
    encoding: slide_transform_core::plan::EncodingProfile) {
    let dir = tmpdir("bf");
    let src_path = dir.join("in.kfb");
    let mut sink = FileSink::create(&src_path).unwrap();
    let p = slide_transform_core::synth_gen::GenParams {
        width,
        height,
        ..Default::default()
    };
    build_synthetic_kfb(&mut sink, &p).unwrap();
    sink.flush().unwrap();
    drop(sink);

    let plan = || {
        let mut p = TransformPlan::brightfield(InputIdentity::default())
            .with_encoding(encoding);
        p.profile = profile;
        p
    };

    // reference run
    let ref_out = dir.join("ref.tif");
    let mut scratch = FileScratch::new(&dir);
    {
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
            &FileSource::open(&src_path).unwrap(),
            &mut out,
            &mut scratch,
            &plan(),
            &job,
        )
        .unwrap();
        out.flush().unwrap();
        // sanity: our own structural validator accepts the reference
        let v = validate_output(&FileSource::open(&ref_out).unwrap(), r.output_bytes, None)
            .unwrap();
        assert_eq!(v.ifd_count as u32, r.validation.ifd_count);
    }
    let ref_sha = sha256_file(&ref_out);

    // crashed run: cancel from inside checkpoint k
    let part_out = dir.join("part.tif");
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: stop,
        cancel: cancel.clone(),
        count: AtomicUsize::new(0),
    };
    let mut scratch2 = FileScratch::new(&dir.join("crash"));
    std::fs::create_dir_all(&dir.join("crash")).unwrap();
    {
        let mut out = FileSink::create(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
        let res = slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
            &FileSource::open(&src_path).unwrap(),
            &mut out,
            &mut scratch2,
            &plan(),
            &job,
        );
        match (&res, stop) {
            (Err(e), Some(_)) => {
                assert!(e.message.contains("已取消"), "unexpected err {e:?}")
            }
            (Ok(_), None) => {}
            _ => panic!("unexpected outcome {:?} for stop={stop:?}", res.map(|r| r.output_bytes)),
        }
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
    assert!(!states.is_empty(), "no checkpoints recorded");
    let st = &states[stop.map(|k| k - 1).unwrap_or(states.len() - 1)];
    let rp = ResumePoint {
        level: st.level as usize,
        channel: 0,
        cell: st.cell_done,
        committed_output: st.committed_output,
        ifd_tiles: st.ifd_tiles.clone(),
    };

    // simulate the crash aftermath the host guarantees:
    // output truncated to committed, offcnt scratch truncated to 12×count
    truncate_file(&part_out, rp.committed_output);
    for (i, &tiles) in rp.ifd_tiles.iter().enumerate() {
        truncate_file(&dir.join(format!("crash/.kfb2tiff-scratch-offcnt-l{i}")), tiles * 12);
    }

    // resume run: same scratch factory instance keeps the surviving files
    {
        let mut out = slide_transform_core::io::FileSink::open_preserve(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_kfb_to_bigtiff_resume(
            &FileSource::open(&src_path).unwrap(),
            &mut out,
            &mut scratch2,
            &plan(),
            &job,
            &rp,
        )
        .unwrap();
        out.flush().unwrap();
        assert_eq!(r.output_bytes, std::fs::metadata(&part_out).unwrap().len());

        // report parity (except elapsed): compare against a fresh reference
        let mut out2 = FileSink::create(&dir.join("ref2.tif")).unwrap();
        let mut scratch3 = FileScratch::new(&dir.join("r2"));
        std::fs::create_dir_all(&dir.join("r2")).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r2 = slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
            &FileSource::open(&src_path).unwrap(),
            &mut out2,
            &mut scratch3,
            &plan(),
            &job,
        )
        .unwrap();
        assert_eq!(r.output_bytes, r2.output_bytes);
        assert_eq!(r.format, r2.format);
        assert_eq!(r.levels.len(), r2.levels.len());
        for (a, b) in r.levels.iter().zip(r2.levels.iter()) {
            assert_eq!(a.tiles_raw_copied, b.tiles_raw_copied);
            assert_eq!(a.tiles_reencoded, b.tiles_reencoded);
        }
        assert_eq!(r.edge_regions.len(), r2.edge_regions.len());
        assert_eq!(r.warnings.len(), r2.warnings.len());
        assert_eq!(r.ifd_chain, r2.ifd_chain);
        assert_eq!(r.validation.ifd_count, r2.validation.ifd_count);
        match (&r.lossy_reencode, &r2.lossy_reencode) {
            (Some(a), Some(b)) => {
                assert_eq!(a.params_fingerprint, b.params_fingerprint);
                assert_eq!(a.tiles_reencoded, b.tiles_reencoded);
                assert_eq!(a.tiles_padded, b.tiles_padded);
            }
            (None, None) => {}
            _ => panic!("lossy_reencode presence differs after resume"),
        }
    }
    assert_eq!(sha256_file(&part_out), ref_sha, "resumed bytes differ");

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn bf_resume_matches_uninterrupted() {
    // 300×300 → edge tiles, 2 selected levels, 3 checkpoints; crash at 1/2
    for k in [1usize, 2] {
        bf_case(300, 300, Some(k));
    }
}

#[test]
fn bf_resume_finalize_phase_crash() {
    // complete payload phase, "crash" during finalize: resume from the last
    // checkpoint must re-run finish() and stay byte-identical
    bf_case(300, 300, None);
}

#[test]
fn bf_ome_resume_matches_uninterrupted() {
    // RGB OME profile: same crash points; the SubIFD layout is rebuilt at
    // finish from the committed per-level streams
    for k in [1usize, 2] {
        bf_case_profile(300, 300, Some(k), OutputProfile::OmeBigTiffRgbSubifd);
    }
    bf_case_profile(300, 300, None, OutputProfile::OmeBigTiffRgbSubifd);
    bf_case_profile(700, 500, Some(2), OutputProfile::OmeBigTiffRgbSubifd);
    bf_case_profile(700, 500, Some(3), OutputProfile::OmeBigTiffRgbSubifd);
}

#[test]
fn bf_compact_resume_matches_uninterrupted() {
    // U3: compact-jpeg-v1 crashes mid level-0 / mid level-1 / finalize and
    // resumes to the byte-identical uninterrupted compact output
    for k in [1usize, 2] {
        bf_case_encoding(300, 300, Some(k),
            slide_transform_core::plan::EncodingProfile::CompactJpegV1);
    }
    bf_case_encoding(300, 300, None, slide_transform_core::plan::EncodingProfile::CompactJpegV1);
    bf_case_encoding(700, 500, Some(2), slide_transform_core::plan::EncodingProfile::CompactJpegV1);
    bf_case_encoding(700, 500, Some(3), slide_transform_core::plan::EncodingProfile::CompactJpegV1);
}

#[test]
fn bf_compact_resume_output_differs_from_preserve() {
    // guard against a resume that silently re-encodes with preserve
    // semantics: the compact artifact (fresh OR resumed) must never equal
    // the preserve artifact of the same input
    let dir = tmpdir("bf-ne");
    let src_path = dir.join("in.kfb");
    let mut sink = FileSink::create(&src_path).unwrap();
    build_synthetic_kfb(
        &mut sink,
        &slide_transform_core::synth_gen::GenParams { width: 300, height: 300, ..Default::default() },
    )
    .unwrap();
    sink.flush().unwrap();
    let mut pout = FileSink::create(&dir.join("p.tif")).unwrap();
    let mut scratch = FileScratch::new(&dir);
    let null = NullProgress;
    let job = JobControl::new(&null);
    let mut plan = TransformPlan::brightfield(InputIdentity::default());
    plan.profile = OutputProfile::OmeBigTiffRgbSubifd;
    slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
        &FileSource::open(&src_path).unwrap(), &mut pout, &mut scratch, &plan, &job,
    )
    .unwrap();
    pout.flush().unwrap();
    drop(pout);
    let mut cout = FileSink::create(&dir.join("c.tif")).unwrap();
    let mut scratch2 = FileScratch::new(&dir.join("c"));
    std::fs::create_dir_all(&dir.join("c")).unwrap();
    let null = NullProgress;
    let job = JobControl::new(&null);
    let plan2 = TransformPlan::brightfield(InputIdentity::default())
        .with_encoding(slide_transform_core::plan::EncodingProfile::CompactJpegV1);
    let r = slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
        &FileSource::open(&src_path).unwrap(), &mut cout, &mut scratch2, &plan2, &job,
    )
    .unwrap();
    cout.flush().unwrap();
    assert!(r.lossy_reencode.is_some());
    assert_ne!(
        sha256_file(&dir.join("p.tif")),
        sha256_file(&dir.join("c.tif")),
        "compact must not reproduce the preserve bytes"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn bf_resume_bigger_grid() {
    // 700×500 → 3 selected levels, 4 checkpoints; crash mid level-0 and mid
    // level-1 (a "fully committed level" + "partial level" mix)
    bf_case(700, 500, Some(2));
    bf_case(700, 500, Some(3));
}

fn fl_case(stop: Option<usize>) {
    let dir = tmpdir("fl");
    let src_path = dir.join("in.kfbf");
    let mut sink = FileSink::create(&src_path).unwrap();
    let n = slide_transform_core::kfbf::fixture::build_synthetic_kfbf(
        &mut sink,
        &slide_transform_core::kfbf::fixture::KfbfGenParams::default(),
    )
    .unwrap();
    sink.flush().unwrap();
    assert!(n > 0);

    let plan = || TransformPlan::fluorescence(InputIdentity::default());

    let ref_out = dir.join("ref.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        slide_transform_core::convert_fl::convert_kfbf_to_ome(
            &FileSource::open(&src_path).unwrap(),
            &mut out,
            &mut scratch,
            &plan(),
            &job,
            None,
        )
        .unwrap();
        out.flush().unwrap();
    }
    let ref_sha = sha256_file(&ref_out);

    let part_out = dir.join("part.tif");
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: stop,
        cancel: cancel.clone(),
        count: AtomicUsize::new(0),
    };
    let crash_dir = dir.join("crash");
    std::fs::create_dir_all(&crash_dir).unwrap();
    let mut scratch2 = FileScratch::new(&crash_dir);
    {
        let mut out = FileSink::create(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
        let res = slide_transform_core::convert_fl::convert_kfbf_to_ome(
            &FileSource::open(&src_path).unwrap(),
            &mut out,
            &mut scratch2,
            &plan(),
            &job,
            None,
        );
        match (&res, stop) {
            (Err(e), Some(_)) => {
                assert!(e.message.contains("已取消"), "unexpected err {e:?}")
            }
            (Ok(_), None) => {}
            _ => panic!("unexpected outcome {:?} for stop={stop:?}", res.map(|r| r.output_bytes)),
        }
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
    assert!(!states.is_empty(), "no checkpoints recorded");
    let st = &states[stop.map(|k| k - 1).unwrap_or(states.len() - 1)];
    let rp = ResumePoint {
        level: st.level as usize,
        channel: st.channel.unwrap_or(0),
        cell: st.cell_done,
        committed_output: st.committed_output,
        ifd_tiles: st.ifd_tiles.clone(),
    };
    truncate_file(&part_out, rp.committed_output);
    for (i, &tiles) in rp.ifd_tiles.iter().enumerate() {
        truncate_file(&crash_dir.join(format!(".kfb2tiff-scratch-ome-offcnt-{i}")), tiles * 12);
    }
    {
        let mut out = FileSink::open_preserve(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_kfbf_to_ome_resume(
            &FileSource::open(&src_path).unwrap(),
            &mut out,
            &mut scratch2,
            &plan(),
            &job,
            None,
            &rp,
        )
        .unwrap();
        out.flush().unwrap();
        assert_eq!(r.output_bytes, std::fs::metadata(&part_out).unwrap().len());
    }
    assert_eq!(sha256_file(&part_out), ref_sha, "resumed FL bytes differ");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn fl_resume_matches_uninterrupted() {
    // fixture: 600×400, 2 channels, one sparse cell + cropped bottom rows;
    // 3 levels × 2 channels = 6 IFDs, 8 checkpoints: crash mid channel 0 of
    // level 0 (k=3), mid channel 1 (k=5), and past level 0 (k=7)
    for k in [1usize, 3, 5, 7] {
        fl_case(Some(k));
    }
}

#[test]
fn fl_resume_finalize_phase_crash() {
    fl_case(None);
}

#[test]
fn resume_wire_json_roundtrip_and_rejects() {
    let j = r#"{"level":2,"channel":1,"cell":128,"out":999983,"ifds":[4,4,2]}"#;
    let rp = parse_resume_json(j).unwrap();
    assert_eq!(rp.level, 2);
    assert_eq!(rp.channel, 1);
    assert_eq!(rp.cell, 128);
    assert_eq!(rp.committed_output, 999983);
    assert_eq!(rp.ifd_tiles, vec![4, 4, 2]);

    assert!(parse_resume_json(r#"{"level":2}"#).is_err());
    assert!(parse_resume_json(r#"not json"#).is_err());
    assert!(parse_resume_json(r#"{"level":0,"channel":0,"cell":5,"out":99,"ifds":[]}"#)
        .is_err());

    // lying journal: ifd_tiles length vs (level, cell) mismatch
    let dir = tmpdir("bf-bad");
    let src_path = dir.join("in.kfb");
    let mut sink = FileSink::create(&src_path).unwrap();
    build_synthetic_kfb(
        &mut sink,
        &slide_transform_core::synth_gen::GenParams { width: 300, height: 300, ..Default::default() },
    )
    .unwrap();
    sink.flush().unwrap();
    let bad = ResumePoint {
        level: 1,
        channel: 0,
        cell: 3,
        committed_output: 1 << 20,
        ifd_tiles: vec![1, 2, 3], // 3 IFDs for (level 1, cell 3) is wrong
    };
    let mut out = FileSink::create(&dir.join("o.tif")).unwrap();
    let mut scratch = FileScratch::new(&dir);
    let null = NullProgress;
    let job = JobControl::new(&null);
    let e = convert_kfb_to_bigtiff_resume(
        &FileSource::open(&src_path).unwrap(),
        &mut out,
        &mut scratch,
        &TransformPlan::brightfield(InputIdentity::default()),
        &job,
        &bad,
    )
    .unwrap_err();
    assert!(matches!(e.code, slide_transform_core::error::ErrorCode::ConversionValidationFailed));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn validator_rejects_truncated_and_corrupt() {
    let dir = tmpdir("val");
    let src_path = dir.join("in.kfb");
    let mut sink = FileSink::create(&src_path).unwrap();
    build_synthetic_kfb(
        &mut sink,
        &slide_transform_core::synth_gen::GenParams { width: 300, height: 300, ..Default::default() },
    )
    .unwrap();
    sink.flush().unwrap();
    let out_path = dir.join("o.tif");
    let mut out = FileSink::create(&out_path).unwrap();
    let mut scratch = FileScratch::new(&dir);
    let null = NullProgress;
    let job = JobControl::new(&null);
    let r = slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
        &FileSource::open(&src_path).unwrap(),
        &mut out,
        &mut scratch,
        &TransformPlan::brightfield(InputIdentity::default()),
        &job,
    )
    .unwrap();
    out.flush().unwrap();

    // valid
    let v = validate_output(&FileSource::open(&out_path).unwrap(), r.output_bytes, Some(r.validation.ifd_count)).unwrap();
    assert_eq!(v.sha256, sha256_file(&out_path));

    // truncated tail → structure/sha mismatch must be caught
    truncate_file(&out_path, r.output_bytes - 4096);
    assert!(validate_output(&FileSource::open(&out_path).unwrap(), r.output_bytes, None).is_err());

    // corrupt IFD chain pointer (byte 8) → caught
    let mut out2 = FileSink::create(&dir.join("o2.tif")).unwrap();
    let mut scratch2 = FileScratch::new(&dir);
    let null = NullProgress;
    let job = JobControl::new(&null);
    let r2 = slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
        &FileSource::open(&src_path).unwrap(),
        &mut out2,
        &mut scratch2,
        &TransformPlan::brightfield(InputIdentity::default()),
        &job,
    )
    .unwrap();
    out2.flush().unwrap();
    let mut bytes = std::fs::read(&dir.join("o2.tif")).unwrap();
    assert_eq!(bytes.len() as u64, r2.output_bytes);
    bytes[8] = 0xFF; // wild first-IFD offset
    bytes[9] = 0xFF;
    std::fs::write(&dir.join("o3.tif"), &bytes).unwrap();
    let e = validate_output(
        &FileSource::open(&dir.join("o3.tif")).unwrap(),
        bytes.len() as u64,
        None,
    )
    .unwrap_err();
    assert!(matches!(e.code, slide_transform_core::error::ErrorCode::ConversionValidationFailed));
    let _ = std::fs::remove_dir_all(&dir);
}

/// Regression (C2): `(size - off) as usize` truncated to 0 on wasm32 once
/// `size ≥ 2^32`, spinning the hash loop forever. A zero-source of exactly
/// 2^32 + 1 MiB must stream through with every read bounded and none empty.
struct ZeroSource {
    size: u64,
    reads: std::sync::atomic::AtomicU64,
    short_reads: std::sync::atomic::AtomicU64,
}

impl ByteSource for ZeroSource {
    fn size(&self) -> u64 {
        self.size
    }
    fn read_at(&self, offset: u64, len: usize) -> Result<Vec<u8>, CoreError> {
        assert!(len > 0, "zero-length read at {offset} (the 2^32 bug)");
        assert!(len <= 1 << 20, "read over chunk bound");
        self.reads.fetch_add(1, Ordering::Relaxed);
        if offset + len as u64 > self.size {
            self.short_reads.fetch_add(1, Ordering::Relaxed);
        }
        Ok(vec![0u8; len])
    }
}

#[test]
fn stream_sha256_over_4gib_boundary() {
    let src = ZeroSource {
        size: u32::MAX as u64 + 1 + (1 << 20),
        reads: std::sync::atomic::AtomicU64::new(0),
        short_reads: std::sync::atomic::AtomicU64::new(0),
    };
    let h = slide_transform_core::validate::stream_sha256(
        &src, src.size()).unwrap();
    let n = src.reads.load(Ordering::Relaxed);
    assert!(n >= 4097, "expected ≥4097 chunk reads, got {n}");
    assert_eq!(src.short_reads.load(Ordering::Relaxed), 0);
    assert_eq!(h.len(), 64); // sha256 hex
}

#[test]
fn kfb_magic_sanity() {
    // documents an assumption the resume tests rely on (synthetic BF input)
    let dir = tmpdir("magic");
    let src_path = dir.join("in.kfb");
    let mut sink = FileSink::create(&src_path).unwrap();
    build_synthetic_kfb(
        &mut sink,
        &slide_transform_core::synth_gen::GenParams::default(),
    )
    .unwrap();
    sink.flush().unwrap();
    let src = FileSource::open(&src_path).unwrap();
    let head = src.read_at(0, 8).unwrap();
    assert_ne!(&head[0..8], b"KFBF0000");
    assert_eq!(&head[0..8], &KFB_MAGIC[0..8]);
    let _ = std::fs::remove_dir_all(&dir);
    let _: Option<CoreError> = None;
    let _: Option<&dyn RandomAccessSink> = None;
    let _: Option<&dyn ScratchFactory> = None;
}
