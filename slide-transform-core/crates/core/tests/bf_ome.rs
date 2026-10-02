//! Brightfield RGB OME-BigTIFF profile: layout, metadata and pixel
//! preservation against the classic profile on the same synthetic input.
//! Whole-file equality with classic is NOT expected (the IFD graph differs by
//! design); tile payload bytes, tile order, geometry and calibration are.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use slide_transform_core::error::ErrorCode;
use slide_transform_core::io::{FileScratch, FileSink, FileSource, RandomAccessSink};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::synth_gen::{build_synthetic_kfb, GenParams};
use slide_transform_core::validate::validate_output;

fn tmpdir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "stbfome-{}-{}-{}",
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

fn gen_kfb(dir: &Path, p: &GenParams) -> PathBuf {
    let path = dir.join("in.kfb");
    let mut sink = FileSink::create(&path).unwrap();
    build_synthetic_kfb(&mut sink, p).unwrap();
    sink.flush().unwrap();
    path
}

fn convert(
    src: &Path,
    out: &Path,
    profile: OutputProfile,
    policy: PixelPolicy,
) -> Result<TransformResult, slide_transform_core::error::CoreError> {
    let sdir = out.with_extension("scratch");
    std::fs::create_dir_all(&sdir).unwrap();
    let mut scratch = FileScratch::new(&sdir);
    let mut sink = FileSink::create(out).unwrap();
    let null = NullProgress;
    let job = JobControl::new(&null);
    let mut plan = TransformPlan::brightfield(InputIdentity::default()).with_policy(policy);
    plan.profile = profile;
    let r = slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
        &FileSource::open(src).unwrap(),
        &mut sink,
        &mut scratch,
        &plan,
        &job,
    );
    sink.flush().unwrap();
    r
}

// ---- minimal little-endian BigTIFF reader (test-only) ----------------------

#[derive(Debug, Clone)]
struct Entry {
    typ: u16,
    count: u64,
    raw: [u8; 8],
}

#[derive(Debug, Clone)]
struct Ifd {
    entries: BTreeMap<u16, Entry>,
    order: Vec<u16>,
    next: u64,
}

fn u16_at(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}
fn u32_at(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn u64_at(b: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

fn read_ifd(f: &[u8], at: u64) -> Ifd {
    let at = at as usize;
    let n = u64_at(f, at) as usize;
    let mut entries = BTreeMap::new();
    let mut order = Vec::new();
    for i in 0..n {
        let e = at + 8 + i * 20;
        let mut raw = [0u8; 8];
        raw.copy_from_slice(&f[e + 12..e + 20]);
        order.push(u16_at(f, e));
        entries.insert(u16_at(f, e), Entry { typ: u16_at(f, e + 2), count: u64_at(f, e + 4), raw });
    }
    Ifd { entries, order, next: u64_at(f, at + 8 + n * 20) }
}

fn type_size(t: u16) -> usize {
    match t {
        1 | 2 | 7 => 1,
        3 => 2,
        4 | 13 => 4,
        5 | 16 | 18 => 8,
        _ => panic!("type {t}"),
    }
}

/// Raw value bytes of a tag (inline or external).
fn value_bytes(f: &[u8], e: &Entry) -> Vec<u8> {
    let len = type_size(e.typ) * e.count as usize;
    if len <= 8 {
        e.raw[..len].to_vec()
    } else {
        let o = u64::from_le_bytes(e.raw) as usize;
        f[o..o + len].to_vec()
    }
}

fn shorts(f: &[u8], ifd: &Ifd, tag: u16) -> Vec<u16> {
    value_bytes(f, &ifd.entries[&tag]).chunks_exact(2).map(|c| u16_at(c, 0)).collect()
}
fn scalar(f: &[u8], ifd: &Ifd, tag: u16) -> u64 {
    let e = &ifd.entries[&tag];
    let b = value_bytes(f, e);
    match e.typ {
        3 => u16_at(&b, 0) as u64,
        4 => u32_at(&b, 0) as u64,
        16 => u64_at(&b, 0),
        t => panic!("scalar type {t}"),
    }
}
fn long8s(f: &[u8], ifd: &Ifd, tag: u16) -> Vec<u64> {
    value_bytes(f, &ifd.entries[&tag]).chunks_exact(8).map(|c| u64_at(c, 0)).collect()
}

fn chain(f: &[u8]) -> Vec<Ifd> {
    let mut out = Vec::new();
    let mut next = u64_at(f, 8);
    while next != 0 {
        let ifd = read_ifd(f, next);
        next = ifd.next;
        out.push(ifd);
    }
    out
}

/// Tile payloads of one IFD, in tile order.
fn tiles<'a>(f: &'a [u8], ifd: &Ifd) -> Vec<&'a [u8]> {
    let offs = long8s(f, ifd, 324);
    let cnts = long8s(f, ifd, 325);
    offs.iter().zip(cnts.iter()).map(|(&o, &c)| &f[o as usize..(o + c) as usize]).collect()
}

fn payload_end(f: &[u8], ifds: &[Ifd]) -> usize {
    ifds.iter()
        .flat_map(|ifd| tiles(f, ifd).into_iter().map(|t| t.as_ptr() as usize - f.as_ptr() as usize + t.len()))
        .max()
        .unwrap()
}

/// Level IFDs of an OME output: IFD 0 then its SubIFDs in order.
fn ome_levels(f: &[u8]) -> (Vec<Ifd>, Vec<Ifd>) {
    let main = chain(f);
    let mut levels = vec![main[0].clone()];
    if main[0].entries.contains_key(&330) {
        for off in long8s(f, &main[0], 330) {
            levels.push(read_ifd(f, off));
        }
    }
    (main, levels)
}

fn ome_xml(f: &[u8], ifd: &Ifd) -> String {
    let b = value_bytes(f, &ifd.entries[&270]);
    assert_eq!(*b.last().unwrap(), 0, "ImageDescription NUL-terminated");
    String::from_utf8(b[..b.len() - 1].to_vec()).unwrap()
}

// ---- tests -----------------------------------------------------------------

/// 1300×900 (edge tiles on both axes), 4:2:2 — four selected levels.
fn sample() -> GenParams {
    GenParams { width: 1300, height: 900, ..Default::default() }
}

#[test]
fn layout_is_one_main_ifd_with_reduced_subifds() {
    let dir = tmpdir("layout");
    let src = gen_kfb(&dir, &sample());
    let ome = dir.join("o.ome.tif");
    let r = convert(&src, &ome, OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode)
        .unwrap();
    assert_eq!(r.format, "ome-bigtiff-subifd-rgb-jpeg-pyramid");
    let f = std::fs::read(&ome).unwrap();
    assert_eq!(&f[0..4], &[b'I', b'I', 43, 0]);

    let (main, levels) = ome_levels(&f);
    assert_eq!(main.len(), 1, "reduced levels must not be in the main IFD chain");
    assert_eq!(levels.len(), r.levels.len());
    assert!(levels.len() >= 3);
    assert_eq!(main[0].entries[&330].typ, 16, "SubIFDs as LONG8");
    assert_eq!(main[0].entries[&330].count as usize, levels.len() - 1);

    for (i, ifd) in levels.iter().enumerate() {
        assert_eq!(scalar(&f, ifd, 254), u64::from(i > 0), "NewSubfileType of level {i}");
        assert_eq!(scalar(&f, ifd, 256) as u32, r.levels[i].width);
        assert_eq!(scalar(&f, ifd, 257) as u32, r.levels[i].height);
        assert_eq!(shorts(&f, ifd, 258), vec![8, 8, 8], "BitsPerSample");
        assert_eq!(scalar(&f, ifd, 259), 7, "JPEG");
        assert_eq!(scalar(&f, ifd, 262), 6, "YCbCr: the JPEG data is YCbCr, not relabelled");
        assert_eq!(scalar(&f, ifd, 277), 3, "SamplesPerPixel");
        assert_eq!(scalar(&f, ifd, 284), 1, "PlanarConfiguration chunky");
        assert_eq!(scalar(&f, ifd, 322), 256);
        assert_eq!(scalar(&f, ifd, 323), 256);
        assert_eq!(shorts(&f, ifd, 530), vec![2, 1], "4:2:2 YCbCrSubSampling");
        if i > 0 {
            assert!(!ifd.entries.contains_key(&270), "only IFD 0 carries OME-XML");
            assert!(!ifd.entries.contains_key(&330));
            assert_eq!(ifd.next, 0);
        }
        let mut sorted = ifd.order.clone();
        sorted.sort();
        assert_eq!(ifd.order, sorted, "tags ascending");
    }

    let xml = ome_xml(&f, &levels[0]);
    assert!(xml.starts_with(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\""
    ));
    assert!(xml.contains("SizeX=\"1300\" SizeY=\"900\" SizeC=\"3\" SizeZ=\"1\" SizeT=\"1\""));
    assert!(xml.contains("Interleaved=\"true\""));
    assert_eq!(xml.matches("<Channel ").count(), 1, "one RGB channel, not three");
    assert!(xml.contains("SamplesPerPixel=\"3\""));
    assert!(xml.contains("<TiffData IFD=\"0\" PlaneCount=\"1\"/>"));
    assert!(xml.contains("PhysicalSizeX=\"0.4841049\""));
    assert!(xml.contains("NominalMagnification=\"20\""));
    assert!(xml.contains("<M K=\"output_profile\">bf-ome</M>"));
    assert!(!xml.contains("in.kfb"), "no source file name in metadata");

    let v = validate_output(&FileSource::open(&ome).unwrap(), r.output_bytes, Some(r.validation.ifd_count))
        .unwrap();
    assert_eq!(v.main_ifds, 1);
    assert_eq!(v.sub_ifds, levels.len() - 1);
    assert_eq!(v.tile_records, r.validation.tile_records_emitted);
    assert!(v.checks.contains(&"subifd-tree"));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn tile_payloads_and_calibration_equal_classic() {
    for sampling in ["422", "420", "444"] {
        let dir = tmpdir("payload");
        let src = gen_kfb(&dir, &GenParams { sampling, ..sample() });
        let ome = dir.join("o.ome.tif");
        let classic = dir.join("o.tif");
        let ro = convert(&src, &ome, OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode)
            .unwrap();
        let rc = convert(&src, &classic, OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode)
            .unwrap();
        let fo = std::fs::read(&ome).unwrap();
        let fc = std::fs::read(&classic).unwrap();
        let (_, lo) = ome_levels(&fo);
        let lc = chain(&fc);
        assert_eq!(lo.len(), lc.len());
        // identical tile byte stream from the header up to the last payload
        let end = payload_end(&fc, &lc);
        assert_eq!(end, payload_end(&fo, &lo));
        assert_eq!(&fo[16..end], &fc[16..end], "{sampling}: payload stream");
        for (i, (a, b)) in lo.iter().zip(lc.iter()).enumerate() {
            assert_eq!(tiles(&fo, a), tiles(&fc, b), "{sampling}: level {i} tiles");
            for tag in [256u16, 257, 258, 259, 262, 277, 282, 283, 284, 296, 322, 323, 530] {
                assert_eq!(
                    value_bytes(&fo, &a.entries[&tag]),
                    value_bytes(&fc, &b.entries[&tag]),
                    "{sampling}: level {i} tag {tag}"
                );
            }
        }
        assert_eq!(ro.count_raw_copied(), rc.count_raw_copied());
        assert_eq!(ro.count_reencoded(), rc.count_reencoded());
        assert!(ro.count_reencoded() > 0, "fixture must exercise edge tiles");
        assert_eq!(ro.edge_regions.len(), rc.edge_regions.len());
        assert_eq!(ro.warnings, rc.warnings);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn full_tiles_are_source_jpeg_bytes() {
    // every raw-copied tile is byte-identical to a source KFB payload (no
    // recompression); edge tiles are the only re-encoded ones
    let dir = tmpdir("copy");
    let src = gen_kfb(&dir, &sample());
    let ome = dir.join("o.ome.tif");
    let r = convert(&src, &ome, OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode)
        .unwrap();
    let fo = std::fs::read(&ome).unwrap();
    let fs = std::fs::read(&src).unwrap();
    let (_, lo) = ome_levels(&fo);
    let mut found = 0u64;
    for ifd in &lo {
        for t in tiles(&fo, ifd) {
            if fs.windows(t.len()).any(|w| w == t) {
                found += 1;
            }
        }
    }
    assert_eq!(found, r.count_raw_copied(), "raw-copied tiles must appear verbatim in the source");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn strict_lossless_rejects_edges_before_writing() {
    let dir = tmpdir("strict");
    let src = gen_kfb(&dir, &sample());
    let ome = dir.join("o.ome.tif");
    let e = convert(&src, &ome, OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::StrictLossless)
        .unwrap_err();
    assert!(matches!(e.code, ErrorCode::PixelPolicyViolation), "{e:?}");
    assert_eq!(std::fs::metadata(&ome).unwrap().len(), 0, "nothing written");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn fluorescence_profile_refused_for_brightfield_input() {
    let dir = tmpdir("wrongprofile");
    let src = gen_kfb(&dir, &sample());
    let e = convert(&src, &dir.join("o.tif"), OutputProfile::OmeBigTiffSubifd, PixelPolicy::AllowEdgeReencode)
        .unwrap_err();
    assert!(matches!(e.code, ErrorCode::UnsupportedKfbVariant), "{e:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn validator_rejects_broken_subifd_trees() {
    let dir = tmpdir("val");
    let src = gen_kfb(&dir, &sample());
    let ome = dir.join("o.ome.tif");
    let r = convert(&src, &ome, OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode)
        .unwrap();
    let f = std::fs::read(&ome).unwrap();
    let main = chain(&f);
    let sub_offsets = long8s(&f, &main[0], 330);
    let sub_arr = u64::from_le_bytes(main[0].entries[&330].raw) as usize;
    let entry_at = |b: &[u8], ifd: u64, tag: u16| -> usize {
        let s = ifd as usize;
        let n = u64_at(b, s) as usize;
        (0..n).map(|i| s + 8 + i * 20).find(|&o| u16_at(b, o) == tag).unwrap()
    };
    let check = |bytes: &[u8], what: &str| {
        let p = dir.join("bad.tif");
        std::fs::write(&p, bytes).unwrap();
        let e = validate_output(&FileSource::open(&p).unwrap(), bytes.len() as u64, None)
            .expect_err(what);
        assert!(matches!(e.code, ErrorCode::ConversionValidationFailed), "{what}: {e:?}");
    };

    // (a) a SubIFD not flagged as a reduced image
    let mut b = f.clone();
    let e = entry_at(&b, sub_offsets[0], 254);
    b[e + 12..e + 16].copy_from_slice(&0u32.to_le_bytes());
    check(&b, "unflagged SubIFD");

    // (b) a SubIFD pointing back at the main IFD
    let mut b = f.clone();
    let first = u64_at(&f, 8);
    b[sub_arr..sub_arr + 8].copy_from_slice(&first.to_le_bytes());
    check(&b, "SubIFD aliasing the main chain");

    // (c) a SubIFD tile beyond EOF
    let mut b = f.clone();
    let last_off = *sub_offsets.last().unwrap();
    let last = read_ifd(&f, last_off);
    let at = if last.entries[&324].count == 1 {
        entry_at(&b, last_off, 324) + 12
    } else {
        u64::from_le_bytes(last.entries[&324].raw) as usize
    };
    b[at..at + 8].copy_from_slice(&(f.len() as u64 + 10).to_le_bytes());
    check(&b, "SubIFD tile out of bounds");

    // (d) SubIFD offset out of bounds
    let mut b = f.clone();
    b[sub_arr..sub_arr + 8].copy_from_slice(&(f.len() as u64 * 2).to_le_bytes());
    check(&b, "SubIFD offset out of bounds");

    // intact file still passes with the reported count
    validate_output(&FileSource::open(&ome).unwrap(), r.output_bytes, Some(r.validation.ifd_count))
        .unwrap();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn fluorescence_report_count_matches_full_walk() {
    // the walker counts SubIFDs too, so the fluorescence report's ifd_count
    // (levels × channels) is verifiable instead of skipped
    let dir = tmpdir("fl");
    let src = dir.join("in.kfbf");
    let mut sink = FileSink::create(&src).unwrap();
    slide_transform_core::kfbf::fixture::build_synthetic_kfbf(
        &mut sink,
        &slide_transform_core::kfbf::fixture::KfbfGenParams::default(),
    )
    .unwrap();
    sink.flush().unwrap();
    let out = dir.join("o.ome.tif");
    let mut scratch = FileScratch::new(&dir);
    let mut osink = FileSink::create(&out).unwrap();
    let null = NullProgress;
    let r = slide_transform_core::convert_fl::convert_kfbf_to_ome(
        &FileSource::open(&src).unwrap(),
        &mut osink,
        &mut scratch,
        &TransformPlan::fluorescence(InputIdentity::default()),
        &JobControl::new(&null),
        None,
    )
    .unwrap();
    osink.flush().unwrap();
    let v = validate_output(&FileSource::open(&out).unwrap(), r.output_bytes, Some(r.validation.ifd_count))
        .unwrap();
    assert!(v.sub_ifds > 0);
    assert_eq!(v.tile_records, r.validation.tile_records_emitted);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn single_subifd_trees_lay_out_correctly() {
    // a two-level pyramid gives every full-resolution IFD exactly one SubIFD,
    // whose 8-byte offset is stored inline; the writer used to reserve
    // external space for it anyway and refused with "IFD 布局错位"
    // (fluorescence 300×200 and brightfield 300×300 both hit it)
    let dir = tmpdir("single-sub");
    let src = dir.join("in.kfbf");
    let mut sink = FileSink::create(&src).unwrap();
    slide_transform_core::kfbf::fixture::build_synthetic_kfbf(
        &mut sink,
        &slide_transform_core::kfbf::fixture::KfbfGenParams {
            width: 300,
            height: 200,
            ..Default::default()
        },
    )
    .unwrap();
    sink.flush().unwrap();
    let out = dir.join("fl.ome.tif");
    let mut scratch = FileScratch::new(&dir);
    let mut osink = FileSink::create(&out).unwrap();
    let null = NullProgress;
    let r = slide_transform_core::convert_fl::convert_kfbf_to_ome(
        &FileSource::open(&src).unwrap(),
        &mut osink,
        &mut scratch,
        &TransformPlan::fluorescence(InputIdentity::default()),
        &JobControl::new(&null),
        None,
    )
    .unwrap();
    osink.flush().unwrap();
    let v = validate_output(&FileSource::open(&out).unwrap(), r.output_bytes, Some(r.validation.ifd_count))
        .unwrap();
    assert_eq!(v.sub_ifds, v.main_ifds, "one SubIFD per channel");

    let bsrc = gen_kfb(&dir, &GenParams { width: 300, height: 300, ..Default::default() });
    let bout = dir.join("bf.ome.tif");
    let r = convert(&bsrc, &bout, OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode)
        .unwrap();
    let v = validate_output(&FileSource::open(&bout).unwrap(), r.output_bytes, Some(r.validation.ifd_count))
        .unwrap();
    assert_eq!((v.main_ifds, v.sub_ifds), (1, 1));
    let _ = std::fs::remove_dir_all(&dir);
}
