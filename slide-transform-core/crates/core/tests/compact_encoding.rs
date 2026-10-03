//! `compact-jpeg-v1` (U3 「更小文件（有损）」): every tile of every level is
//! decoded and re-encoded with the LOCKED compact parameters at full source
//! resolution. These tests pin the output contract for BOTH brightfield
//! layouts (bf-classic + bf-ome → four encoding×layout combinations with
//! preserve), the geometry preservation, the TIFF tag consistency with the
//! encoded data, the report fields, and the typed refusals
//! (fluorescence / strict-lossless).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use slide_transform_core::error::ErrorCode;
use slide_transform_core::io::{FileScratch, FileSink, FileSource, RandomAccessSink};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::plan::{
    EncodingProfile, InputIdentity, OutputProfile, PixelPolicy, TransformPlan,
    COMPACT_JPEG_V1_FINGERPRINT, COMPACT_JPEG_V1_QUALITY, COMPACT_JPEG_V1_SAMPLING,
    compact_jpeg_v1_encoder_cfg,
};
use slide_transform_core::jpeg::tables::{std_chroma_quality, std_luma_quality};
use slide_transform_core::report::TransformResult;
use slide_transform_core::synth_gen::{build_synthetic_kfb, GenParams};
use slide_transform_core::validate::validate_output;

fn tmpdir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "stu3-{}-{}-{}",
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
    encoding: EncodingProfile,
) -> Result<TransformResult, slide_transform_core::error::CoreError> {
    let sdir = out.with_extension("scratch");
    std::fs::create_dir_all(&sdir).unwrap();
    let mut scratch = FileScratch::new(&sdir);
    let mut sink = FileSink::create(out).unwrap();
    let null = NullProgress;
    let job = JobControl::new(&null);
    let mut plan = TransformPlan::brightfield(InputIdentity::default())
        .with_policy(policy)
        .with_encoding(encoding);
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

// ---- minimal little-endian BigTIFF reader (test-only, same as bf_ome.rs) --

#[derive(Debug, Clone)]
struct Entry {
    typ: u16,
    count: u64,
    raw: [u8; 8],
}

#[derive(Debug, Clone)]
struct Ifd {
    entries: BTreeMap<u16, Entry>,
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
    for i in 0..n {
        let e = at + 8 + i * 20;
        let mut raw = [0u8; 8];
        raw.copy_from_slice(&f[e + 12..e + 20]);
        entries.insert(u16_at(f, e), Entry { typ: u16_at(f, e + 2), count: u64_at(f, e + 4), raw });
    }
    Ifd { entries, next: u64_at(f, at + 8 + n * 20) }
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

fn ome_levels(f: &[u8]) -> Vec<Ifd> {
    let main = chain(f);
    let mut levels = vec![main[0].clone()];
    if main[0].entries.contains_key(&330) {
        for off in long8s(f, &main[0], 330) {
            levels.push(read_ifd(f, off));
        }
    }
    levels
}

fn level_ifds(f: &[u8], ome: bool) -> Vec<Ifd> {
    if ome { ome_levels(f) } else { chain(f) }
}

fn ome_xml(f: &[u8], ifd: &Ifd) -> String {
    let b = value_bytes(f, &ifd.entries[&270]);
    String::from_utf8(b[..b.len() - 1].to_vec()).unwrap()
}

// ---- metric helpers --------------------------------------------------------

fn mse_psnr(a: &[u8], b: &[u8]) -> (f64, u8, f64) {
    assert_eq!(a.len(), b.len());
    let mut se = 0.0f64;
    let mut abs_sum = 0.0f64;
    let mut max = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        let d = (*x as i32 - *y as i32).abs();
        se += (d * d) as f64;
        abs_sum += d as f64;
        max = max.max(d as u8);
    }
    let mse = se / a.len() as f64;
    let psnr = if mse == 0.0 { f64::INFINITY } else { 10.0 * (255.0f64 * 255.0 / mse).log10() };
    (psnr, max, abs_sum / a.len() as f64)
}

// ---- tests -----------------------------------------------------------------

/// The locked fingerprint must describe the actual encoder configuration:
/// changing a parameter without bumping the fingerprint would let an old
/// half-written output resume under new bytes.
#[test]
fn fingerprint_matches_locked_parameters() {
    let cfg = compact_jpeg_v1_encoder_cfg();
    assert_eq!(cfg.y_q, std_luma_quality(COMPACT_JPEG_V1_QUALITY));
    assert_eq!(cfg.c_q, std_chroma_quality(COMPACT_JPEG_V1_QUALITY));
    assert_eq!(cfg.sampling, COMPACT_JPEG_V1_SAMPLING);
    let sampling_str = match COMPACT_JPEG_V1_SAMPLING {
        slide_transform_core::jpeg::Sampling::S444 => "444",
        slide_transform_core::jpeg::Sampling::S422 => "422",
        slide_transform_core::jpeg::Sampling::S420 => "420",
    };
    assert!(COMPACT_JPEG_V1_FINGERPRINT.contains(&format!("q{COMPACT_JPEG_V1_QUALITY}")),
        "fingerprint must name the quality");
    assert!(COMPACT_JPEG_V1_FINGERPRINT.contains(sampling_str),
        "fingerprint must name the sampling");
}

/// Four combinations × source samplings × odd/edge geometries: structure,
/// tags, counts, geometry equality with preserve, and pixel fidelity.
#[test]
fn compact_output_contract_all_samplings_and_layouts() {
    for sampling in ["422", "420", "444"] {
        for (w, h) in [(580u32, 300u32), (300, 300), (1024, 777), (613, 355)] {
            let dir = tmpdir("contract");
            let src = gen_kfb(&dir, &GenParams { width: w, height: h, sampling, ..Default::default() });
            // preserve references (both layouts) for geometry + pixel deltas
            let (rp_c, rp_o) = (
                dir.join(format!("preserve-{sampling}-{w}x{h}.tif")),
                dir.join(format!("preserve-{sampling}-{w}x{h}.ome.tif")),
            );
            let rp_classic = convert(&src, &rp_c, OutputProfile::ClassicJpegBigTiff,
                PixelPolicy::AllowEdgeReencode, EncodingProfile::PreserveSource).unwrap();
            let rp_ome = convert(&src, &rp_o, OutputProfile::OmeBigTiffRgbSubifd,
                PixelPolicy::AllowEdgeReencode, EncodingProfile::PreserveSource).unwrap();
            assert!(rp_classic.lossy_reencode.is_none());
            assert!(rp_ome.lossy_reencode.is_none());

            for (profile, ome, out) in [
                (OutputProfile::ClassicJpegBigTiff, false,
                    dir.join(format!("compact-{sampling}-{w}x{h}.tif"))),
                (OutputProfile::OmeBigTiffRgbSubifd, true,
                    dir.join(format!("compact-{sampling}-{w}x{h}.ome.tif"))),
            ] {
                let r = convert(&src, &out, profile, PixelPolicy::AllowEdgeReencode,
                    EncodingProfile::CompactJpegV1).unwrap();

                // report contract: everything re-encoded, nothing copied
                assert_eq!(r.count_raw_copied(), 0, "{sampling} {w}x{h}: nothing is copied verbatim");
                let total: u64 = r.levels.iter().map(|l| l.tiles_total).sum();
                assert_eq!(r.count_reencoded(), total, "{sampling} {w}x{h}: every tile re-encoded");
                let lossy = r.lossy_reencode.as_ref().unwrap();
                assert_eq!(lossy.profile, "compact-jpeg-v1");
                assert_eq!(lossy.params_fingerprint, COMPACT_JPEG_V1_FINGERPRINT);
                assert_eq!(lossy.quality, COMPACT_JPEG_V1_QUALITY);
                assert_eq!(lossy.tiles_reencoded, total);
                assert_eq!(lossy.tiles_padded, r.edge_regions.len() as u64,
                    "{sampling} {w}x{h}: padded count == listed padded regions");

                // geometry: same level count and dimensions as preserve
                let rp = if ome { &rp_ome } else { &rp_classic };
                assert_eq!(r.levels.len(), rp.levels.len());
                for (a, b) in r.levels.iter().zip(rp.levels.iter()) {
                    assert_eq!((a.width, a.height), (b.width, b.height));
                    assert_eq!(a.tiles_total, b.tiles_total);
                }
                assert_eq!(r.width, rp.width);
                assert_eq!(r.height, rp.height);

                // structure: the built-in validator accepts the compact bytes
                let v = validate_output(&FileSource::open(&out).unwrap(), r.output_bytes,
                    Some(r.validation.ifd_count)).unwrap();
                assert_eq!(v.tile_records, r.validation.tile_records_emitted);

                let f = std::fs::read(&out).unwrap();
                let lv = level_ifds(&f, ome);
                assert_eq!(lv.len(), r.levels.len());
                let (sub_h, sub_v) = match COMPACT_JPEG_V1_SAMPLING {
                    slide_transform_core::jpeg::Sampling::S444 => (1u16, 1u16),
                    slide_transform_core::jpeg::Sampling::S422 => (2, 1),
                    slide_transform_core::jpeg::Sampling::S420 => (2, 2),
                };
                for (i, ifd) in lv.iter().enumerate() {
                    // tags must match the ENCODED data (color interpretation
                    // stays YCbCr JPEG; sampling advertises the compact one)
                    assert_eq!(scalar(&f, ifd, 259), 7, "JPEG compression");
                    assert_eq!(scalar(&f, ifd, 262), 6, "YCbCr photometric");
                    assert_eq!(scalar(&f, ifd, 277), 3);
                    assert_eq!(shorts(&f, ifd, 530), vec![sub_h, sub_v],
                        "{sampling} {w}x{h} level {i}: YCbCrSubSampling must match the encoded data");
                    assert_eq!(scalar(&f, ifd, 256) as u32, r.levels[i].width);
                    assert_eq!(scalar(&f, ifd, 257) as u32, r.levels[i].height);
                    // every encoded tile is a decodable 256×256 canvas JPEG
                    for t in tiles(&f, ifd) {
                        let probe = slide_transform_core::jpeg::scan_jpeg(t).unwrap();
                        assert_eq!((probe.width, probe.height), (256, 256));
                        let want = match COMPACT_JPEG_V1_SAMPLING {
                            slide_transform_core::jpeg::Sampling::S444 => (1, 1, 1, 1, 1, 1),
                            slide_transform_core::jpeg::Sampling::S422 => (2, 1, 1, 1, 1, 1),
                            slide_transform_core::jpeg::Sampling::S420 => (2, 2, 1, 1, 1, 1),
                        };
                        assert_eq!(probe.sampling, Some(want),
                            "encoded sampling must be the locked one");
                    }
                }

                // compact output is lossy-but-faithful: decode and compare
                // with the preserve output tile-by-tile (both are canvases)
                let fp = std::fs::read(if ome { &rp_o } else { &rp_c }).unwrap();
                let flv = level_ifds(&fp, ome);
                let mut worst_psnr = f64::INFINITY;
                for (ci, pi) in lv.iter().zip(flv.iter()) {
                    let ct = tiles(&f, ci);
                    let pt = tiles(&fp, pi);
                    assert_eq!(ct.len(), pt.len());
                    for (a, b) in ct.iter().zip(pt.iter()) {
                        let da = slide_transform_core::jpeg::decode(a, 256 * 256 * 4).unwrap();
                        let db = slide_transform_core::jpeg::decode(b, 256 * 256 * 4).unwrap();
                        assert_eq!((da.width, da.height), (db.width, db.height));
                        let (psnr, _max, _mad) = mse_psnr(&da.data, &db.data);
                        worst_psnr = worst_psnr.min(psnr);
                    }
                }
                // the synthetic fixture is UNIFORM NOISE — the adversarial
                // floor of the locked parameters (no spatial redundancy, up
                // to two extra lossy generations, and for a 4:4:4 source the
                // chroma is newly halved in both axes, so per-pixel MAD can
                // be large while structure survives). Real/smooth content
                // measures far higher — the meaningful fidelity bounds are
                // the 38 dB / MAD≤2 smooth-content test below plus the
                // real-sample table in the u3 report; this only pins the
                // adversarial floor.
                assert!(worst_psnr >= 12.0,
                    "{sampling} {w}x{h} {ome}: worst tile PSNR {worst_psnr:.2} < 12 dB");
            }

            // both layouts write the identical compact tile byte stream
            let fc = std::fs::read(&dir.join(format!("compact-{sampling}-{w}x{h}.tif"))).unwrap();
            let fo = std::fs::read(&dir.join(format!("compact-{sampling}-{w}x{h}.ome.tif"))).unwrap();
            let lc = chain(&fc);
            let lo = ome_levels(&fo);
            let end = payload_end(&fc, &lc);
            assert_eq!(end, payload_end(&fo, &lo));
            assert_eq!(&fc[16..end], &fo[16..end], "{sampling} {w}x{h}: compact payload stream equal across layouts");

            // compact is genuinely different from preserve (regression: the
            // mode must not silently fall back to copy semantics)
            assert_ne!(&fc[16..end],
                &std::fs::read(&rp_c).unwrap()[16..payload_end(&std::fs::read(&rp_c).unwrap(), &chain(&std::fs::read(&rp_c).unwrap()))]);

            let _ = std::fs::remove_dir_all(&dir);
        }
    }
}

/// Odd geometries: tiles whose source is smaller than the canvas are counted
/// as padded, listed as edge regions, and white-padded exactly like preserve.
#[test]
fn compact_edge_tiles_padded_like_preserve() {
    let dir = tmpdir("edges");
    let src = gen_kfb(&dir, &GenParams { width: 580, height: 300, ..Default::default() });
    let rp = convert(&src, &dir.join("p.tif"), OutputProfile::ClassicJpegBigTiff,
        PixelPolicy::AllowEdgeReencode, EncodingProfile::PreserveSource).unwrap();
    let rc = convert(&src, &dir.join("c.tif"), OutputProfile::ClassicJpegBigTiff,
        PixelPolicy::AllowEdgeReencode, EncodingProfile::CompactJpegV1).unwrap();
    // same source-geometry padding set
    assert_eq!(rp.edge_regions.len(), rc.edge_regions.len());
    for (a, b) in rp.edge_regions.iter().zip(rc.edge_regions.iter()) {
        assert_eq!((a.level, a.x, a.y, a.source_w, a.source_h, a.canvas_w, a.canvas_h),
                   (b.level, b.x, b.y, b.source_w, b.source_h, b.canvas_w, b.canvas_h));
        assert!(!b.reused_qtables, "compact never reuses source tables");
    }
    // the padding is WHITE in both modes: decode one padded compact tile and
    // check the region beyond (source_w, source_h) is (255,255,255)
    let fc = std::fs::read(&dir.join("c.tif")).unwrap();
    let lc = chain(&fc);
    let er = &rc.edge_regions[0];
    let ifd = &lc[er.level as usize];
    let across = tiles_count_across(&fc, ifd);
    let idx = (er.y / 256) as usize * across + (er.x / 256) as usize;
    assert!(worst_padding_is_white(tiles(&fc, ifd)[idx], er.source_w, er.source_h),
        "padding far from the boundary must be pure white (chroma bleed at the seam is expected)");
    let _ = std::fs::remove_dir_all(&dir);
}

/// White-canvas padding contract: 8+ px beyond the source geometry the
/// padding is (near-)white — flat 255 quantizes to ~253 at q85 and ~254 at
/// q90 (inherent baseline-JPEG DC quantization; the preserve edge path and
/// the Pillow oracle behave identically), and the first 1–2 px at the seam
/// carry chroma bleed from subsampling. Assert the floor, not exactness.
fn worst_padding_is_white(tile: &[u8], source_w: u32, source_h: u32) -> bool {
    let img = slide_transform_core::jpeg::decode(tile, 256 * 256 * 4).unwrap();
    let mut min = 255u8;
    let mut check = |x: usize, y: usize| {
        let o = (y * img.width as usize + x) * 3;
        for c in 0..3 {
            min = min.min(img.data[o + c]);
        }
    };
    for y in [0usize, 128, 255] {
        for x in ((source_w as usize + 8)..256).step_by(8) {
            check(x, y);
        }
    }
    for x in [0usize, 128, 255] {
        for y in ((source_h as usize + 8)..256).step_by(8) {
            check(x, y);
        }
    }
    min >= 240
}

fn tiles_count_across(f: &[u8], ifd: &Ifd) -> usize {
    ((scalar(f, ifd, 256) as u64 + scalar(f, ifd, 322) as u64 - 1)
        / scalar(f, ifd, 322) as u64) as usize
}

/// The OME provenance records the encoding profile and parameters for
/// compact runs only — preserve OME-XML keeps its pre-U3 bytes.
#[test]
fn ome_provenance_records_encoding() {
    let dir = tmpdir("prov");
    let src = gen_kfb(&dir, &GenParams { width: 700, height: 500, ..Default::default() });
    let cp = dir.join("c.ome.tif");
    let pp = dir.join("p.ome.tif");
    convert(&src, &cp, OutputProfile::OmeBigTiffRgbSubifd,
        PixelPolicy::AllowEdgeReencode, EncodingProfile::CompactJpegV1).unwrap();
    convert(&src, &pp, OutputProfile::OmeBigTiffRgbSubifd,
        PixelPolicy::AllowEdgeReencode, EncodingProfile::PreserveSource).unwrap();
    let fc = std::fs::read(&cp).unwrap();
    let fp = std::fs::read(&pp).unwrap();
    let xmlc = ome_xml(&fc, &ome_levels(&fc)[0]);
    let xmlp = ome_xml(&fp, &ome_levels(&fp)[0]);
    assert!(xmlc.contains("<M K=\"encoding_profile\">compact-jpeg-v1</M>"), "{xmlc}");
    assert!(xmlc.contains(&format!("<M K=\"encoding_params_fingerprint\">{COMPACT_JPEG_V1_FINGERPRINT}</M>")));
    assert!(xmlc.contains("lossy"));
    assert!(!xmlc.to_lowercase().contains("lossless"), "never claim lossless");
    // preserve keeps the exact pre-U3 provenance (byte-compat gate)
    assert!(!xmlp.contains("encoding_profile"));
    assert!(xmlp.contains("copied byte-for-byte"));
    let _ = std::fs::remove_dir_all(&dir);
}

/// Fluorescence refuses compact BEFORE any output byte (typed error).
#[test]
fn fluorescence_refuses_compact() {
    let dir = tmpdir("flref");
    let src = dir.join("in.kfbf");
    let mut sink = FileSink::create(&src).unwrap();
    let n = slide_transform_core::kfbf::fixture::build_synthetic_kfbf(
        &mut sink,
        &slide_transform_core::kfbf::fixture::KfbfGenParams::default(),
    )
    .unwrap();
    sink.flush().unwrap();
    assert!(n > 0);
    let out = dir.join("o.tif");
    let sdir = dir.join("s");
    std::fs::create_dir_all(&sdir).unwrap();
    let mut scratch = FileScratch::new(&sdir);
    let mut sink = FileSink::create(&out).unwrap();
    let null = NullProgress;
    let job = JobControl::new(&null);
    let plan = TransformPlan::fluorescence(InputIdentity::default())
        .with_encoding(EncodingProfile::CompactJpegV1);
    let e = slide_transform_core::convert_fl::convert_kfbf_to_ome(
        &FileSource::open(&src).unwrap(),
        &mut sink,
        &mut scratch,
        &plan,
        &job,
        None,
    )
    .unwrap_err();
    assert!(matches!(e.code, ErrorCode::UnsupportedKfbVariant), "{e:?}");
    assert!(e.message.contains("compact-jpeg-v1"));
    sink.flush().unwrap();
    assert_eq!(std::fs::metadata(&out).unwrap().len(), 0, "no output byte on refusal");
    let _ = std::fs::remove_dir_all(&dir);
}

/// compact + strict-lossless is a contradiction — typed policy error before
/// any output byte.
#[test]
fn strict_lossless_compact_refused() {
    let dir = tmpdir("strict");
    let src = gen_kfb(&dir, &GenParams { width: 580, height: 300, ..Default::default() });
    let out = dir.join("o.tif");
    let e = convert(&src, &out, OutputProfile::ClassicJpegBigTiff,
        PixelPolicy::StrictLossless, EncodingProfile::CompactJpegV1).unwrap_err();
    assert!(matches!(e.code, ErrorCode::PixelPolicyViolation), "{e:?}");
    assert_eq!(std::fs::metadata(&out).unwrap().len(), 0, "no output byte on refusal");
    let _ = std::fs::remove_dir_all(&dir);
}

/// Default plans mean preserve: pre-U3 callers construct plans without the
/// field and must keep their exact behavior (report shows no lossy summary).
#[test]
fn default_encoding_is_preserve() {
    let p = TransformPlan::brightfield(InputIdentity::default());
    assert_eq!(p.encoding, EncodingProfile::PreserveSource);
    let p = TransformPlan::fluorescence(InputIdentity::default());
    assert_eq!(p.encoding, EncodingProfile::PreserveSource);
    assert_eq!(EncodingProfile::from_id("preserve-source-v1"), Some(EncodingProfile::PreserveSource));
    assert_eq!(EncodingProfile::from_id("compact-jpeg-v1"), Some(EncodingProfile::CompactJpegV1));
    assert_eq!(EncodingProfile::from_id("compact"), None);
}

/// Compact re-encoded tiles are smaller than the preserve payload stream on
/// the synthetic q90 4:2:2 fixture (the mode's purpose; not a guarantee for
/// arbitrary inputs, but the default fixture must show it).
#[test]
fn compact_output_is_smaller_on_default_fixture() {
    let dir = tmpdir("smaller");
    let src = gen_kfb(&dir, &GenParams::default()); // 580×300 q90 4:2:2
    let rp = convert(&src, &dir.join("p.tif"), OutputProfile::ClassicJpegBigTiff,
        PixelPolicy::AllowEdgeReencode, EncodingProfile::PreserveSource).unwrap();
    let rc = convert(&src, &dir.join("c.tif"), OutputProfile::ClassicJpegBigTiff,
        PixelPolicy::AllowEdgeReencode, EncodingProfile::CompactJpegV1).unwrap();
    assert!(rc.output_bytes < rp.output_bytes,
        "compact {} !< preserve {}", rc.output_bytes, rp.output_bytes);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Codec-level fidelity on SMOOTH content (representative of tissue's
/// low-contrast regions, as opposed to the uniform-noise fixture above):
/// source q90 4:2:2 → decode → locked compact re-encode → decode must stay
/// well above 38 dB against the source decode.
#[test]
fn compact_fidelity_on_smooth_content() {
    let w = 256u32;
    let h = 256u32;
    let mut img = vec![0u8; (w * h * 3) as usize];
    for y in 0..h {
        for x in 0..w {
            let o = ((y * w + x) * 3) as usize;
            // H&E-like palette: pink base, a soft purple nucleus blob, gentle
            // gradients — all band-limited (no pixel noise)
            let dx = x as f64 - 128.0;
            let dy = y as f64 - 96.0;
            let r2 = (dx * dx + dy * dy) / 900.0;
            let blob = (-r2).exp();
            img[o] = (230.0 - 20.0 * (x as f64 / w as f64) - 70.0 * blob) as u8;
            img[o + 1] = (170.0 - 15.0 * (y as f64 / h as f64) - 60.0 * blob) as u8;
            img[o + 2] = (190.0 - 10.0 * ((x + y) as f64 / (w + h) as f64) - 40.0 * blob) as u8;
        }
    }
    let src_cfg = slide_transform_core::jpeg::EncoderCfg::with_quality(
        90, slide_transform_core::jpeg::Sampling::S422);
    let source = slide_transform_core::jpeg::encode_rgb(&img, w, h, &src_cfg).unwrap();
    let decoded = slide_transform_core::jpeg::decode(&source, 256 * 256 * 4).unwrap();
    let compact = slide_transform_core::jpeg::encode_rgb(
        &decoded.data, w, h, &compact_jpeg_v1_encoder_cfg()).unwrap();
    let red = slide_transform_core::jpeg::decode(&compact, 256 * 256 * 4).unwrap();
    let (psnr, max, mad) = mse_psnr(&decoded.data, &red.data);
    assert!(psnr >= 38.0, "smooth-content PSNR {psnr:.2} < 38 dB");
    assert!(mad <= 2.0, "smooth-content MAD {mad:.2}");
    assert!(max <= 60);
    assert!(compact.len() < source.len(), "compact {} !< source {}", compact.len(), source.len());
}
