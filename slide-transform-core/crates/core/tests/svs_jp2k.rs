//! F2 SVS JPEG 2000 adapter tests: Aperio 33003/33005 containers over the
//! committed fixture codestreams (see `fixtures/jp2k/README.md` — synthetic
//! 33003 + CC0 33005 tiles; the non-CC0 33003 slides stay local-only),
//! conversion to both brightfield output profiles, edge/padding semantics,
//! strict-lossless refusal, resume, and the corrupt-input corpus.
//!
//! Requires the `fixtures` + `jp2k` features:
//! `cargo test -p slide-transform-core --features "fixtures jp2k" --test svs_jp2k`.

#![cfg(all(feature = "codecs", feature = "fixtures", feature = "jp2k"))]

use slide_transform_core::convert_svs;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::io::{ByteSource, MemSink, MemSource, RandomAccessSink};
use slide_transform_core::jp2k::{self, Jp2kColor};
use slide_transform_core::plan::{
    EncodingProfile, InputIdentity, OutputProfile, PixelPolicy, TransformPlan,
    COMPACT_JPEG_V1_FINGERPRINT,
};
use slide_transform_core::report::TransformResult;
use slide_transform_core::svs::{self, SourceCodec};
use slide_transform_core::svs_fixture::{build_synthetic_svs_jp2k, SvsJp2kFixture};

/// Pre-declared acceptance thresholds (F2 evidence doc; the study measured
/// hayro-vs-OpenSlide 33003 mean ≈ 3 / max ≤ 25 and 33005 mean ≤ 0.1 /
/// max ≤ 2 on real tiles — the committed fixtures stay far below these).
const FIXTURE_33003_MAX_DIFF: i64 = 1; // lossless codestream + identical tables
const FIXTURE_33005_OSR_MAX: i64 = 8;
const FIXTURE_33005_OSR_MEAN: f64 = 0.5;
/// q96 4:2:2 JPEG generation loss over the decoded RGB (output tile vs the
/// fixture's decoded pixels). The synthetic fixtures are WORST-CASE content
/// for 4:2:2 (lossless JP2K of strong colour patches + high-frequency noise
/// — real Aperio sources are lossy Q≈30/70 and smoother), so the bound is
/// wider than the real-sample colour gate.
const REENCODE_MAX_DIFF: i64 = 48;
const REENCODE_MEAN_DIFF: f64 = 3.0;

fn fixture_dir() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/jp2k")
}

fn load_fixture(name: &str) -> Vec<u8> {
    std::fs::read(fixture_dir().join(name)).unwrap()
}

fn plan_for(profile: OutputProfile, policy: PixelPolicy) -> TransformPlan {
    let mut plan = TransformPlan::brightfield(InputIdentity::default()).with_policy(policy);
    plan.profile = profile;
    plan
}

fn plan_enc(profile: OutputProfile, policy: PixelPolicy, enc: EncodingProfile) -> TransformPlan {
    let mut plan = plan_for(profile, policy);
    plan.encoding = enc;
    plan
}

fn convert(
    data: &[u8],
    profile: OutputProfile,
) -> Result<TransformResult, slide_transform_core::error::CoreError> {
    convert_enc(data, profile, PixelPolicy::AllowEdgeReencode, EncodingProfile::PreserveSource)
}

fn convert_enc(
    data: &[u8],
    profile: OutputProfile,
    policy: PixelPolicy,
    enc: EncodingProfile,
) -> Result<TransformResult, slide_transform_core::error::CoreError> {
    let src = MemSource::new(data.to_vec());
    let mut sink = MemSink::new();
    let mut scratch = slide_transform_core::io::MemScratch::default();
    convert_svs::convert_svs(&src, &mut sink, &mut scratch, &plan_enc(profile, policy, enc))
}

fn expect_error(
    data: Vec<u8>,
    profile: OutputProfile,
    code: slide_transform_core::error::ErrorCode,
    msg_contains: &str,
    name: &str,
) {
    match convert(&data, profile) {
        Err(e) => {
            assert_eq!(e.code, code, "{name}: got {} ({})", e.code.stable_code(), e.message);
            assert!(e.message.contains(msg_contains), "{name}: {}", e.message);
        }
        Ok(_) => panic!("{name}: expected error {code:?}"),
    }
}

/// 33003 container: 600×300 level 0 over 256 px tiles (3×2), level 1 150×75
/// (4×4 reduction). The last-column codestream of level 0 is the CROPPED
/// 200×256 fixture — the codestream-smaller-than-tile-rect edge case.
fn container_33003() -> Vec<u8> {
    let full = load_fixture("synth-33003-full.j2k");
    let cropped = load_fixture("synth-33003-cropped.j2k");
    // 600×300 → 150×75 → 37×18 (3 levels at the Aperio 4× step)
    let f = SvsJp2kFixture {
        compression: 33003,
        photometric: 2, // the real files' tag "lie"; the codestream decides
        tile: 256,
        width: 600,
        height: 300,
        downsample: 4,
        mpp: Some(0.4990),
        appmag: Some(20.0),
        levels: vec![
            vec![full.clone(), full.clone(), cropped, full.clone(), full.clone(), full.clone()],
            vec![full.clone()],
            vec![full],
        ],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
    sink.data
}

/// 33005 container: 480×480 over 240 px tiles (2×2), level 1 120×120.
fn container_33005() -> Vec<u8> {
    let t = load_fixture("cc0-33005-l0.j2k");
    let l1 = load_fixture("cc0-33005-l1.j2k");
    // 480×480 → 120×120 → 30×30 → 8×8
    let f = SvsJp2kFixture {
        compression: 33005,
        photometric: 2,
        tile: 240,
        width: 480,
        height: 480,
        downsample: 4,
        mpp: Some(0.4990),
        appmag: Some(20.0),
        levels: vec![
            vec![t.clone(), t.clone(), t.clone(), t.clone()],
            vec![l1.clone()],
            vec![l1.clone()],
            vec![l1],
        ],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
    sink.data
}

/// Independent decode of a fixture codestream (the expected pixels the
/// conversion must reproduce before its own re-encode).
fn fixture_expected(name: &str, w: u32, h: u32, compression: u64) -> Vec<u8> {
    let data = load_fixture(name);
    let hdr = jp2k::parse_header(&data).unwrap();
    let color = jp2k::classify_aperio(compression, &hdr).unwrap();
    jp2k::decode_tile_rgb(&data, hdr.width, hdr.height, color, (w as u64) * (h as u64))
        .unwrap()
        .rgb
}

fn diff_stats(got: &[u8], expected: &[u8]) -> (f64, i64) {
    assert_eq!(got.len(), expected.len());
    let mut sum = 0u64;
    let mut max = 0i64;
    for (a, b) in got.iter().zip(expected.iter()) {
        let d = (*a as i64 - *b as i64).abs();
        sum += d as u64;
        max = max.max(d);
    }
    (sum as f64 / expected.len() as f64, max)
}

// --------------------------------------------------------------------------- //
// probe                                                                       //
// --------------------------------------------------------------------------- //

#[test]
fn probe_classifies_both_aperio_jp2k_variants() {
    for (data, codec) in [
        (container_33003(), SourceCodec::Jpeg2000Ycbcr422),
        (container_33005(), SourceCodec::Jpeg2000Rgb),
    ] {
        let src = MemSource::new(data);
        let doc = svs::probe_svs(&src).unwrap();
        assert_eq!(doc.codec, codec);
        assert_eq!(doc.codec.source_format(), codec.source_format());
        assert!(doc.levels.len() >= 2);
        assert_eq!(doc.mpp, Some(0.4990));
        assert_eq!(doc.appmag, Some(20.0));
        for lv in &doc.levels {
            let c = lv.jp2k_contract.as_ref().expect("jp2k contract");
            assert_eq!(c.components, 3);
            assert_eq!(c.bit_depth, 8);
            assert!(!c.signed);
            assert_eq!(c.decomposition_levels, 5);
        }
        // the codestream contract carries the colour decision per variant
        let c0 = doc.levels[0].jp2k_contract.as_ref().unwrap();
        assert_eq!(
            jp2k::classify_aperio(codec.aperio_compression(), c0).unwrap(),
            if codec == SourceCodec::Jpeg2000Ycbcr422 { Jp2kColor::Ycbcr422 } else { Jp2kColor::RgbCodestream }
        );
    }
}

#[test]
fn sniff_main_codec_routes_both_tags_without_a_full_probe() {
    for (data, codec) in [
        (container_33003(), SourceCodec::Jpeg2000Ycbcr422),
        (container_33005(), SourceCodec::Jpeg2000Rgb),
    ] {
        let src = MemSource::new(data);
        assert_eq!(svs::sniff_main_codec(&src).unwrap(), codec);
    }
}

// --------------------------------------------------------------------------- //
// conversion + colour + edges                                                 //
// --------------------------------------------------------------------------- //

/// Minimal BigTIFF walker for OUTPUT assertions (same shape as svs.rs's).
mod outread {
    use slide_transform_core::io::ByteSource;

    pub struct Out {
        pub data: Vec<u8>,
    }
    impl Out {
        pub fn size(&self) -> u64 {
            self.data.len() as u64
        }
    }
    fn u16_at(b: &[u8], at: usize) -> u16 {
        u16::from_le_bytes([b[at], b[at + 1]])
    }
    fn u64_at(b: &[u8], at: usize) -> u64 {
        let mut v = [0u8; 8];
        v.copy_from_slice(&b[at..at + 8]);
        u64::from_le_bytes(v)
    }
    impl ByteSource for Out {
        fn size(&self) -> u64 {
            self.data.len() as u64
        }
        fn read_at(&self, offset: u64, len: usize) -> slide_transform_core::error::CoreResult<Vec<u8>> {
            let o = offset as usize;
            Ok(self.data[o..o + len].to_vec())
        }
    }
    pub struct Ifd {
        pub entries: Vec<(u16, u16, u64, Vec<u8>)>,
        pub next: u64,
        pub sub: Vec<u64>,
    }
    pub fn first_ifd(out: &Out) -> u64 {
        u64_at(&out.data, 8)
    }
    pub fn read_ifd(out: &Out, at: u64) -> Ifd {
        let n = u64_at(&out.data, at as usize) as usize;
        let mut entries = Vec::new();
        for i in 0..n {
            let e = at as usize + 8 + i * 20;
            let tag = u16_at(&out.data, e);
            let typ = u16_at(&out.data, e + 2);
            let count = u64_at(&out.data, e + 4);
            let val = u64_at(&out.data, e + 12);
            let ts = type_size(typ);
            let bytes = if (count * ts) <= 8 {
                out.data[e + 12..e + 12 + (count * ts) as usize].to_vec()
            } else {
                out.read_at(val, (count * ts) as usize).unwrap()
            };
            entries.push((tag, typ, count, bytes));
        }
        let next = u64_at(&out.data, at as usize + 8 + n * 20);
        let sub = entries
            .iter()
            .find(|(t, ..)| *t == 330)
            .map(|(_, _, _, v)| {
                v.chunks_exact(8)
                    .map(|c| u64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]))
                    .collect()
            })
            .unwrap_or_default();
        Ifd { entries, next, sub }
    }
    pub fn entry<'a>(ifd: &'a Ifd, tag: u16) -> Option<&'a (u16, u16, u64, Vec<u8>)> {
        ifd.entries.iter().find(|(t, ..)| *t == tag)
    }
    pub fn u32_val(e: &(u16, u16, u64, Vec<u8>)) -> u64 {
        let mut b = [0u8; 8];
        for (i, v) in e.3.iter().take(8).enumerate() {
            b[i] = *v;
        }
        u64::from_le_bytes(b)
    }
    fn type_size(t: u16) -> u64 {
        match t {
            1 | 2 | 6 | 7 => 1,
            3 | 8 => 2,
            4 | 9 | 11 => 4,
            5 | 10 | 12 | 16 | 17 => 8,
            _ => 1,
        }
    }
    pub fn ifd_tile_offsets(_out: &Out, ifd: &Ifd) -> Vec<u64> {
        let e = entry(ifd, 324).expect("tile offsets");
        let n = e.2 as usize;
        (0..n)
            .map(|i| u64::from_le_bytes(e.3[i * 8..i * 8 + 8].try_into().unwrap()))
            .collect()
    }
    pub fn ifd_tile_counts(ifd: &Ifd) -> Vec<u64> {
        let e = entry(ifd, 325).expect("tile byte counts");
        let n = e.2 as usize;
        (0..n)
            .map(|i| u64::from_le_bytes(e.3[i * 8..i * 8 + 8].try_into().unwrap()))
            .collect()
    }
}

#[test]
fn convert_33003_to_both_profiles_reencodes_and_keeps_geometry() {
    let data = container_33003();
    let expected = fixture_expected("synth-33003-full.j2k", 256, 256, 33003);
    let cropped_expected = fixture_expected("synth-33003-cropped.j2k", 200, 256, 33003);

    for profile in [OutputProfile::ClassicJpegBigTiff, OutputProfile::OmeBigTiffRgbSubifd] {
        let r = convert(&data, profile).unwrap();
        // every tile was decoded and re-encoded (lossy, in BOTH modes)
        assert_eq!(r.count_raw_copied(), 0, "{profile:?}: JP2K has no passthrough");
        assert_eq!(r.count_reencoded(), 6 + 1 + 1);
        let lossy = r.lossy_reencode.as_ref().expect("lossy summary in preserve mode");
        assert_eq!(lossy.profile, "preserve-source-v1");
        assert_eq!(lossy.params_fingerprint, "aperio-jp2k-reencode:q96:y422:hstd:v1");
        assert_eq!(lossy.quality, 96);
        assert!(r.warnings.iter().any(|w| w == convert_svs::WARN_JP2K_REENCODED));
        assert_eq!(r.source_format, Some("aperio-svs-jp2k"));
        assert_eq!(r.width, 600);
        assert_eq!(r.height, 300);
        // one padded edge region: the cropped codestream cell
        assert_eq!(r.edge_regions.len(), 1, "{:?}", r.edge_regions);
        assert_eq!(r.edge_regions[0].source_w, 200);
        assert_eq!(r.edge_regions[0].canvas_w, 256);

        let out = outread::Out { data: {
            let src = MemSource::new(data.clone());
            let mut sink = MemSink::new();
            let mut scratch = slide_transform_core::io::MemScratch::default();
            convert_svs::convert_svs(&src, &mut sink, &mut scratch, &plan_for(profile, PixelPolicy::AllowEdgeReencode)).unwrap();
            sink.data
        } };

        // walk the output IFDs: level 0 + level 1 (classic chains them;
        // the OME profile hangs the reduced level off IFD 0 as a SubIFD)
        let mut level_ifds = vec![outread::read_ifd(&out, outread::first_ifd(&out))];
        if !level_ifds[0].sub.is_empty() {
            for sub_at in level_ifds[0].sub.clone() {
                level_ifds.push(outread::read_ifd(&out, sub_at));
            }
        } else {
            let mut at = level_ifds[0].next;
            while at != 0 {
                let ifd = outread::read_ifd(&out, at);
                at = ifd.next;
                level_ifds.push(ifd);
            }
        }
        assert_eq!(level_ifds.len(), 3, "{profile:?}: one output IFD per level");
        let l0 = &level_ifds[0];
        // output tiles are baseline JPEG, photometric 6, no shared tables
        assert_eq!(outread::u32_val(outread::entry(l0, 259).unwrap()), 7);
        assert_eq!(outread::u32_val(outread::entry(l0, 262).unwrap()), 6);
        assert!(outread::entry(l0, 347).is_none(), "JP2K re-encode writes no tag 347");
        let sub = outread::entry(l0, 530).unwrap();
        let h = u16::from_le_bytes([sub.3[0], sub.3[1]]);
        let v = u16::from_le_bytes([sub.3[2], sub.3[3]]);
        assert_eq!((h, v), (2, 1), "q96 4:2:2");
        let (w_e, h_e) = (outread::u32_val(outread::entry(l0, 256).unwrap()), outread::u32_val(outread::entry(l0, 257).unwrap()));
        assert_eq!((w_e, h_e), (600, 300));
        let offs = outread::ifd_tile_offsets(&out, l0);
        let cnts = outread::ifd_tile_counts(l0);
        assert_eq!(offs.len(), 6);
        // level 1 keeps the reduced geometry
        let l1 = &level_ifds[1];
        let (w1, h1) = (outread::u32_val(outread::entry(l1, 256).unwrap()), outread::u32_val(outread::entry(l1, 257).unwrap()));
        assert_eq!((w1, h1), (150, 75));

        // tile pixels: interior + cropped edge cell, decoded from the OUTPUT
        for (idx, expected_px) in [(0usize, &expected), (2usize, &cropped_expected)] {
            let tile = crate_decode_jpeg(&out.data[offs[idx] as usize..(offs[idx] + cnts[idx]) as usize]);
            let (row, col) = (idx / 3, idx % 3);
            if idx == 0 {
                let (mean, max) = diff_stats(&tile, expected_px);
                assert!(max <= REENCODE_MAX_DIFF && mean <= REENCODE_MEAN_DIFF, "tile 0: mean={mean} max={max}");
                // no colour bias: per-channel means must stay ≈ 0
                for c in 0..3 {
                    let m: f64 = tile.iter().skip(c).step_by(3).zip(expected_px.iter().skip(c).step_by(3))
                        .map(|(a, b)| *a as i64 - *b as i64).sum::<i64>() as f64
                        / (tile.len() / 3) as f64;
                    assert!(m.abs() <= 1.0, "channel {c} bias {m}");
                }
            } else {
                // cropped codestream: left 200 columns carry the pixels, the
                // rest is white canvas; the VALID region (88 px) is exact-ish
                for y in 0..256usize {
                    for x in 0..200usize {
                        let g = (y * 256 + x) * 3;
                        let e = (y * 200 + x) * 3;
                        for c in 0..3 {
                            let d = (tile[g + c] as i64 - cropped_expected[e + c] as i64).abs();
                            assert!(d <= REENCODE_MAX_DIFF, "edge tile ({y},{x},{c}): {d}");
                        }
                    }
                    for x in 208..256usize {
                        let g = (y * 256 + x) * 3;
                        // columns 200..207 share DCT blocks and 4:2:2 chroma
                        // pairs with the content seam (x=200 is not block
                        // aligned), so only the padding BEYOND the mixed block
                        // is checked: it must settle back to white
                        for c in 0..3 {
                            assert!((tile[g + c] as i64 - 255).abs() <= 10, "padding ≈white at {y},{x}");
                        }
                    }
                }
                let _ = (row, col);
            }
        }
    }
}

/// Decode a JPEG payload with the crate's own codec (independent of the JP2K
/// path under test).
fn crate_decode_jpeg(payload: &[u8]) -> Vec<u8> {
    let probe = slide_transform_core::jpeg::scan_jpeg(payload).unwrap();
    let img = slide_transform_core::jpeg::decode_ex(payload, (probe.width as u64) * (probe.height as u64), false).unwrap();
    img.data
}

#[test]
fn convert_33005_stays_rgb_and_matches_osr_reference() {
    let data = container_33005();
    let expected = fixture_expected("cc0-33005-l0.j2k", 240, 240, 33005);
    // sanity: the fixture itself matches the OpenSlide reference
    let osr = load_fixture("cc0-33005-l0.osr.rgb");
    let (mean, max) = diff_stats(&expected, &osr);
    assert!(max <= FIXTURE_33005_OSR_MAX && mean <= FIXTURE_33005_OSR_MEAN, "fixture vs OSR: mean={mean} max={max}");

    let r = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).unwrap();
    assert_eq!(r.count_reencoded(), 4 + 1 + 1 + 1);
    assert_eq!(r.edge_regions.len(), 0, "33005 edge cells are full-size codestreams");

    let src = MemSource::new(data);
    let mut sink = MemSink::new();
    let mut scratch = slide_transform_core::io::MemScratch::default();
    convert_svs::convert_svs(&src, &mut sink, &mut scratch, &plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode)).unwrap();
    let out = outread::Out { data: sink.data };
    let l0 = outread::read_ifd(&out, outread::first_ifd(&out));
    let offs = outread::ifd_tile_offsets(&out, &l0);
    let cnts = outread::ifd_tile_counts(&l0);
    let tile = crate_decode_jpeg(&out.data[offs[0] as usize..(offs[0] + cnts[0]) as usize]);
    let (mean, max) = diff_stats(&tile, &expected);
    assert!(max <= REENCODE_MAX_DIFF && mean <= REENCODE_MEAN_DIFF, "33005 tile: mean={mean} max={max}");
}

#[test]
fn output_ome_xml_records_jp2k_provenance() {
    let data = container_33005();
    let src = MemSource::new(data);
    let mut sink = MemSink::new();
    let mut scratch = slide_transform_core::io::MemScratch::default();
    convert_svs::convert_svs(&src, &mut sink, &mut scratch, &plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode)).unwrap();
    // find the OME-XML (Image Description of IFD 0)
    let xml = String::from_utf8_lossy(&sink.data).to_string();
    assert!(xml.contains("aperio-svs-jp2k"), "source_format id");
    assert!(xml.contains("jpeg2000-33005"), "source codec");
    assert!(xml.contains("hayro-jpeg2000 0.4.0"), "decoder identity");
    assert!(xml.contains("aperio-jp2k-reencode:q96:y422:hstd:v1") || xml.contains("NOT-preserved"), "re-encode story");
    assert!(xml.contains("the source codestream is NOT preserved"), "honesty");
}

// --------------------------------------------------------------------------- //
// negative controls: the colour classification is load-bearing                //
// --------------------------------------------------------------------------- //

#[test]
fn negative_control_wrong_colour_assignment_fails_loudly() {
    // 33005 (RGB codestream) treated as YCbCr must move pixels far away
    let data = load_fixture("cc0-33005-l0.j2k");
    let correct = jp2k::decode_tile_rgb(&data, 240, 240, Jp2kColor::RgbCodestream, 240 * 240).unwrap();
    let wrong = jp2k::decode_tile_rgb(&data, 240, 240, Jp2kColor::Ycbcr422, 240 * 240).unwrap();
    let (mean, _) = diff_stats(&wrong.rgb, &correct.rgb);
    assert!(mean > 20.0, "33005 as YCbCr: mean={mean} (must be loud)");

    // 33003 (YCbCr codestream) treated as RGB must fail the same way
    let data = load_fixture("synth-33003-full.j2k");
    let correct = jp2k::decode_tile_rgb(&data, 256, 256, Jp2kColor::Ycbcr422, 256 * 256).unwrap();
    let wrong = jp2k::decode_tile_rgb(&data, 256, 256, Jp2kColor::RgbCodestream, 256 * 256).unwrap();
    let (mean, _) = diff_stats(&wrong.rgb, &correct.rgb);
    assert!(mean > 20.0, "33003 as RGB: mean={mean} (must be loud)");
}

#[test]
fn committed_fixtures_meet_the_predeclared_thresholds() {
    let expected = load_fixture("synth-33003-full.expected.rgb");
    let decoded = fixture_expected("synth-33003-full.j2k", 256, 256, 33003);
    let (mean, max) = diff_stats(&decoded, &expected);
    assert!(max <= FIXTURE_33003_MAX_DIFF && mean == 0.0, "33003: mean={mean} max={max}");

    let cropped_expected = load_fixture("synth-33003-cropped.expected.rgb");
    let decoded = fixture_expected("synth-33003-cropped.j2k", 200, 256, 33003);
    let (mean, max) = diff_stats(&decoded, &cropped_expected);
    assert!(max <= FIXTURE_33003_MAX_DIFF && mean == 0.0, "33003 cropped: mean={mean} max={max}");

    for name in ["cc0-33005-l0.j2k", "cc0-33005-l1.j2k"] {
        let osr = load_fixture(&name.replace(".j2k", ".osr.rgb"));
        let decoded = fixture_expected(name, 240, 240, 33005);
        let (mean, max) = diff_stats(&decoded, &osr);
        assert!(
            max <= FIXTURE_33005_OSR_MAX && mean <= FIXTURE_33005_OSR_MEAN,
            "{name}: mean={mean} max={max}"
        );
    }
}

// --------------------------------------------------------------------------- //
// strict-lossless / compact policies                                          //
// --------------------------------------------------------------------------- //

#[test]
fn strict_lossless_rejects_jp2k_with_zero_output() {
    for data in [container_33003(), container_33005()] {
        let src = MemSource::new(data.clone());
        let mut sink = MemSink::new();
        let mut scratch = slide_transform_core::io::MemScratch::default();
        let e = convert_svs::convert_svs(
            &src,
            &mut sink,
            &mut scratch,
            &plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::StrictLossless),
        )
        .err()
        .expect("strict-lossless must refuse JP2K");
        assert_eq!(e.code, PixelPolicyViolation, "{}", e.message);
        assert!(e.message.contains("strict-lossless"), "{}", e.message);
        assert!(e.message.contains("JPEG2000") || e.message.contains("JPEG 2000"), "{}", e.message);
        assert_eq!(sink.data.len(), 0, "zero output bytes before the refusal");
        // compact + strict is refused too (the JP2K-specific reason wins —
        // it names the source codec, the more specific rejection)
        let e = convert_enc(
            &data,
            OutputProfile::OmeBigTiffRgbSubifd,
            PixelPolicy::StrictLossless,
            EncodingProfile::CompactJpegV1,
        )
        .err()
        .expect("strict + compact must refuse");
        assert!(e.message.contains("JPEG2000"), "{}", e.message);
    }
}

#[test]
fn compact_jp2k_uses_the_locked_compact_fingerprint() {
    let data = container_33005();
    let r = convert_enc(
        &data,
        OutputProfile::ClassicJpegBigTiff,
        PixelPolicy::AllowEdgeReencode,
        EncodingProfile::CompactJpegV1,
    )
    .unwrap();
    let lossy = r.lossy_reencode.as_ref().unwrap();
    assert_eq!(lossy.profile, "compact-jpeg-v1");
    assert_eq!(lossy.params_fingerprint, COMPACT_JPEG_V1_FINGERPRINT);
    assert_eq!(lossy.quality, 80);
    // compact output advertises 4:2:0 (2,2) subsampling
    let src = MemSource::new(data);
    let mut sink = MemSink::new();
    let mut scratch = slide_transform_core::io::MemScratch::default();
    convert_svs::convert_svs(&src, &mut sink, &mut scratch, &plan_enc(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode, EncodingProfile::CompactJpegV1)).unwrap();
    let out = outread::Out { data: sink.data };
    let l0 = outread::read_ifd(&out, outread::first_ifd(&out));
    let sub = outread::entry(&l0, 530).unwrap();
    let h = u16::from_le_bytes([sub.3[0], sub.3[1]]);
    let v = u16::from_le_bytes([sub.3[2], sub.3[3]]);
    assert_eq!((h, v), (2, 2), "compact 4:2:0");
}

// --------------------------------------------------------------------------- //
// corrupt / malicious inputs (typed errors, no success leftovers)             //
// --------------------------------------------------------------------------- //

#[test]
fn corrupted_and_oversized_tiles_are_typed_rejections() {
    let good_33003 = load_fixture("synth-33003-full.j2k");
    let good_33005 = load_fixture("cc0-33005-l0.j2k");

    let build = |l0_tiles: Vec<Vec<u8>>| {
        let f = SvsJp2kFixture {
            compression: 33003,
            photometric: 2,
            tile: 256,
            width: 256,
            height: 256,
            downsample: 4,
            mpp: Some(0.5),
            appmag: None,
            levels: vec![l0_tiles, vec![good_33003.clone()], vec![good_33003.clone()]],
        };
        let mut sink = MemSink::new();
        build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
        sink.data
    };
    let build_33005 = |l0_tiles: Vec<Vec<u8>>| {
        let f = SvsJp2kFixture {
            compression: 33005,
            photometric: 2,
            tile: 240,
            width: 240,
            height: 240,
            downsample: 4,
            mpp: Some(0.5),
            appmag: None,
            levels: vec![l0_tiles, vec![good_33005.clone()], vec![good_33005.clone()]],
        };
        let mut sink = MemSink::new();
        build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
        sink.data
    };

    // 1. truncated codestream (header intact, tile data cut mid-stream)
    let mut trunc = good_33003.clone();
    trunc.truncate(trunc.len() / 2);
    expect_error(
        build(vec![trunc]),
        OutputProfile::ClassicJpegBigTiff,
        JpegDecodeFailed,
        "JPEG 2000",
        "truncated codestream",
    );

    // 2. random garbage payload
    let garbage = vec![0xA5u8; 4096];
    expect_error(
        build(vec![garbage]),
        OutputProfile::ClassicJpegBigTiff,
        ConversionValidationFailed,
        "JPEG 2000",
        "garbage payload",
    );

    // 3. oversized header: SIZ Xsiz patched 256 → 4000 (a 4096²-class pixel
    //    bomb must die in the bounded probe, before any decode allocation)
    let mut bomb = good_33003.clone();
    let xsiz_at = 4 + 2 + 2 + 8; // SOC+SIZ+Lsiz+Rsiz
    let v = 4000u32;
    bomb[xsiz_at..xsiz_at + 4].copy_from_slice(&v.to_be_bytes());
    expect_error(
        build(vec![bomb]),
        OutputProfile::ClassicJpegBigTiff,
        UnsupportedKfbVariant,
        "有界探测拒绝",
        "oversized SIZ",
    );

    // 4. unknown colour combination: MCT flipped 0 → 1 in a 33003 codestream
    //    (never a guessed colour)
    let mut mct = good_33003.clone();
    // COD: find FF52; SGcod MCT byte = +2(marker)+2(Lcod)+1(Scod)+1(prog)+2(layers)
    let cod_at = mct.windows(2).position(|w| w == [0xFF, 0x52]).unwrap();
    mct[cod_at + 2 + 2 + 1 + 1 + 2] = 1;
    expect_error(
        build(vec![mct]),
        OutputProfile::ClassicJpegBigTiff,
        UnsupportedKfbVariant,
        "不猜颜色",
        "MCT flip",
    );

    // 5. 16-bit components in a 33005 codestream (all three Ssiz entries)
    let mut deep = good_33005.clone();
    let siz_at = deep.windows(2).position(|w| w == [0xFF, 0x51]).unwrap();
    // Ssiz^c = marker + Lsiz(2) + Rsiz(2) + 8 u32 extents + Csiz(2) + 3c
    let ssiz0 = siz_at + 2 + 2 + 2 + 32 + 2;
    for c in 0..3 {
        assert_eq!(deep[ssiz0 + c * 3], 0x07, "fixture must start 8-bit");
        deep[ssiz0 + c * 3] = 0x0F;
    }
    expect_error(
        build_33005(vec![deep]),
        OutputProfile::ClassicJpegBigTiff,
        UnsupportedKfbVariant,
        "位深",
        "16-bit",
    );

    // 6. a JP2-boxed payload is named, never decoded
    let mut jp2 = vec![0x00, 0x00, 0x00, 0x0C, 0x6A, 0x50, 0x20, 0x20, 0x0D, 0x0A, 0x87, 0x0A];
    jp2.extend_from_slice(&good_33005);
    expect_error(
        build_33005(vec![jp2]),
        OutputProfile::ClassicJpegBigTiff,
        UnsupportedKfbVariant,
        "JP2 容器",
        "JP2 boxed",
    );
}

#[test]
fn per_tile_contract_drift_is_rejected_before_decode() {
    // the level probe sees a clean 33003 header; a later tile drifts
    // (decomposition levels 5 → 3) — the cross-check refuses it
    let good = load_fixture("synth-33003-full.j2k");
    let mut drifted = good.clone();
    let cod_at = drifted.windows(2).position(|w| w == [0xFF, 0x52]).unwrap();
    drifted[cod_at + 9] = 3; // SPcod decomposition levels byte
    let f = SvsJp2kFixture {
        compression: 33003,
        photometric: 2,
        tile: 256,
        width: 512,
        height: 256,
        downsample: 4,
        mpp: Some(0.5),
        appmag: None,
        levels: vec![
            vec![good, drifted],
            vec![load_fixture("synth-33003-full.j2k")],
            vec![load_fixture("synth-33003-full.j2k")],
        ],
    };
    let mut sink = MemSink::new();
    build_synthetic_svs_jp2k(&mut sink, &f).unwrap();
    expect_error(
        sink.data,
        OutputProfile::ClassicJpegBigTiff,
        ConversionValidationFailed,
        "契约不一致",
        "contract drift",
    );
}

// --------------------------------------------------------------------------- //
// resume                                                                      //
// --------------------------------------------------------------------------- //

#[test]
fn jp2k_resume_matches_uninterrupted_output() {
    use slide_transform_core::io::{FileSink, FileSource, FileScratch};
    use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    struct Collector {
        states: Mutex<Vec<CheckpointState>>,
        stop_after: usize,
        cancel: CancelFlag,
        count: AtomicUsize,
    }
    impl slide_transform_core::job::CheckpointCallback for Collector {
        fn on_checkpoint(&self, c: &CheckpointState) {
            let n = self.count.fetch_add(1, Ordering::SeqCst) + 1;
            self.states.lock().unwrap().push(c.clone());
            if n >= self.stop_after {
                self.cancel.cancel();
            }
        }
    }

    let dir = std::env::temp_dir().join(format!(
        "stf1-svs-jp2k-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let in_path = dir.join("in.svs");
    {
        let mut sink = FileSink::create(&in_path).unwrap();
        build_synthetic_svs_jp2k(&mut sink, &SvsJp2kFixture {
            compression: 33005,
            photometric: 2,
            tile: 240,
            width: 480,
            height: 480,
            downsample: 4,
            mpp: Some(0.4990),
            appmag: Some(20.0),
            levels: vec![
                vec![load_fixture("cc0-33005-l0.j2k").clone(); 4],
                vec![load_fixture("cc0-33005-l1.j2k").clone()],
                vec![load_fixture("cc0-33005-l1.j2k").clone()],
                vec![load_fixture("cc0-33005-l1.j2k")],
            ],
        })
        .unwrap();
        sink.flush().unwrap();
    }
    let plan = || plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode);

    let ref_out = dir.join("ref.ome.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_svs::convert_svs(&FileSource::open(&in_path).unwrap(), &mut out, &mut scratch, &plan()).unwrap();
        out.flush().unwrap();
        assert_eq!(r.count_raw_copied(), 0);
        assert!(r.lossy_reencode.is_some());
    }
    let full = std::fs::read(&ref_out).unwrap();

    let stop = 2usize;
    let part_out = dir.join("part.ome.tif");
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
        let res = convert_svs::convert_svs_to_bigtiff(
            &FileSource::open(&in_path).unwrap(),
            &mut out,
            &mut scratch2,
            &plan(),
            &job,
        );
        assert!(res.unwrap_err().message.contains("已取消"));
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
    let st = &states[stop - 1];
    let rp = slide_transform_core::resume::ResumePoint {
        level: st.level as usize,
        channel: 0,
        cell: st.cell_done,
        committed_output: st.committed_output,
        ifd_tiles: st.ifd_tiles.clone(),
        adapter_version: Some("1".to_string()),
    };
    rp.validate().unwrap();
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
    let mut scratch3 = FileScratch::new(&crash_dir);
    {
        let mut out = FileSink::open_preserve(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_svs::convert_svs_to_bigtiff_resume(
            &FileSource::open(&in_path).unwrap(),
            &mut out,
            &mut scratch3,
            &plan(),
            &job,
            &rp,
        )
        .unwrap();
        out.flush().unwrap();
        assert_eq!(r.count_raw_copied(), 0);
    }
    let got = std::fs::read(&part_out).unwrap();
    assert_eq!(got, full, "resumed JP2K output byte-identical to a fresh run");
    let _ = scratch3;
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn jp2k_source_never_reports_passthrough_stats() {
    let data = container_33003();
    let r = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    for l in &r.levels {
        assert_eq!(l.tiles_raw_copied, 0, "level {}: JP2K cells are never raw-copied", l.level);
        assert_eq!(l.tiles_reencoded, l.tiles_total);
    }
    assert_eq!(r.validation.tile_records_emitted, 8);
}
