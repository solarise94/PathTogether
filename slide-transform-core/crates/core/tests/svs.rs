//! F1 SVS adapter tests: synthetic Aperio-style fixtures (generated in
//! code, no sample bytes committed), conversion to both brightfield output
//! profiles, passthrough/edge semantics, resume, and the malformed corpus.
//!
//! Requires `fixtures` (`cargo test -p slide-transform-core --features
//! fixtures`).

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_svs;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::io::{ByteSource, MemScratch, MemSink, MemSource, RandomAccessSink};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::svs;
use slide_transform_core::svs_fixture::{build_synthetic_svs, FixtureColor, SvsGenParams};
use slide_transform_core::validate::validate_output;

fn gen(p: &SvsGenParams) -> Vec<u8> {
    let mut sink = MemSink::new();
    build_synthetic_svs(&mut sink, p).unwrap();
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
) -> Result<TransformResult, slide_transform_core::error::CoreError> {
    let src = MemSource::new(data.to_vec());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    convert_svs::convert_svs(&src, &mut sink, &mut scratch, &plan_for(profile, PixelPolicy::AllowEdgeReencode))
}

fn expect_svs_error(
    data: Vec<u8>,
    profile: OutputProfile,
    code: slide_transform_core::error::ErrorCode,
    name: &str,
) {
    match convert(&data, profile) {
        Err(e) => assert_eq!(
            e.code, code,
            "{name}: got {} ({})",
            e.code.stable_code(),
            e.message
        ),
        Ok(_) => panic!("{name}: expected error {:#?}", code),
    }
}

/// Minimal TIFF walker for assertions on the OUTPUT (independent of the
/// converter's own reader): parse the BigTIFF the writers emit.
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
            .map(|(_, _, c, b)| {
                (0..*c as usize).map(|i| u64_at(b, i * 8)).collect()
            })
            .unwrap_or_default();
        Ifd { entries, next, sub }
    }
    pub fn type_size(t: u16) -> u64 {
        match t {
            1 | 2 | 7 => 1,
            3 | 8 => 2,
            4 | 9 | 13 => 4,
            _ => 8,
        }
    }
    pub fn entry_u64(ifd: &Ifd, tag: u16) -> Option<u64> {
        ifd.entries.iter().find(|(t, ..)| *t == tag).map(|(_, _, c, b)| {
            let raw = if *c >= 1 && b.len() >= 8 {
                u64_at(b, 0)
            } else if b.len() >= 4 {
                u32::from_le_bytes([b[0], b[1], b[2], b[3]]) as u64
            } else if b.len() >= 2 {
                u16::from_le_bytes([b[0], b[1]]) as u64
            } else {
                0
            };
            raw
        })
    }
    pub fn entry_bytes<'a>(ifd: &'a Ifd, tag: u16) -> Option<&'a [u8]> {
        ifd.entries.iter().find(|(t, ..)| *t == tag).map(|(_, _, _, b)| b.as_slice())
    }
}

// --------------------------------------------------------------------------- //
// probe + conversion basics
// --------------------------------------------------------------------------- //

#[test]
fn probe_classic_fixture_levels_and_metadata() {
    let p = SvsGenParams { width: 580, height: 300, include_associated: true, ..Default::default() };
    let data = gen(&p);
    let src = MemSource::new(data);
    let doc = svs::probe_svs(&src).unwrap();
    assert_eq!(doc.levels.len(), 3, "580x300/4/4/4 stops above the 8px floor");
    assert_eq!(doc.levels[0].width, 580);
    assert_eq!(doc.levels[0].tile_w, 256);
    assert_eq!(doc.levels[1].width, 145);
    assert_eq!(doc.levels[1].height, 75);
    assert_eq!(doc.levels[2].width, 37);
    assert_eq!(doc.levels[2].height, 19);
    assert_eq!(doc.mpp, Some(0.4990));
    assert_eq!(doc.appmag, Some(20.0));
    assert!(doc.levels.iter().all(|l| l.color == FixtureColor::Rgb.into_color()));
    let names: Vec<&str> = doc.associated.iter().map(|a| a.name.as_str()).collect();
    assert_eq!(names, vec!["thumbnail", "label", "macro"]);
}

trait ColorExt {
    fn into_color(self) -> svs::PayloadColor;
}
impl ColorExt for FixtureColor {
    fn into_color(self) -> svs::PayloadColor {
        match self {
            FixtureColor::Rgb => svs::PayloadColor::Rgb,
            FixtureColor::YCbCr => svs::PayloadColor::YCbCr,
        }
    }
}

#[test]
fn convert_classic_profiles_structure() {
    for bigtiff in [false, true] {
        for tile in [240u32, 256] {
            let p = SvsGenParams {
                width: 500,
                height: 260,
                tile,
                bigtiff,
                include_associated: true,
                icc: true,
                ..Default::default()
            };
            let data = gen(&p);
            let r = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
            assert_eq!(r.format, "classic-bigtiff-jpeg-pyramid");
            assert_eq!(r.source_format, Some("aperio-svs-jpeg"));
            assert_eq!(r.adapter_version, Some("1"));
            assert_eq!(r.levels.len(), 3);
            // passthrough: every tile copied, none re-encoded
            assert_eq!(r.count_reencoded(), 0);
            let expected_tiles: u64 = r.levels.iter().map(|l| l.tiles_total).sum();
            assert_eq!(r.count_raw_copied(), expected_tiles);

            // structural validation via the shared walker
            let out = outread::Out { data: conv_bytes(&data, OutputProfile::ClassicJpegBigTiff) };
            let v = validate_output(&out, out.size(), Some(r.validation.ifd_count)).unwrap();
            assert_eq!(v.main_ifds, 3);
            assert_eq!(v.sub_ifds, 0);
            let ifd0 = outread::read_ifd(&out, outread::first_ifd(&out));
            assert_eq!(outread::entry_u64(&ifd0, 256), Some(500));
            assert_eq!(outread::entry_u64(&ifd0, 262), Some(2), "RGB JPEG payloads tagged 2");
            assert_eq!(outread::entry_u64(&ifd0, 322), Some(tile as u64));
            assert_eq!(outread::entry_u64(&ifd0, 323), Some(tile as u64));
            assert!(outread::entry_bytes(&ifd0, 530).is_none(), "no YCbCrSubSampling for RGB");
            assert!(outread::entry_bytes(&ifd0, 347).is_some(), "JPEGTables carried");
            assert!(outread::entry_bytes(&ifd0, 34675).is_some(), "ICC carried");
            let desc = outread::entry_bytes(&ifd0, 270).unwrap();
            assert!(desc.starts_with(b"{\"adapter\": \"aperio-svs-jpeg\""));
            // reduced levels: own tables, no ICC; chain ends after the last
            let mut ifd1 = outread::read_ifd(&out, ifd0.next);
            assert_eq!(outread::entry_u64(&ifd1, 254), Some(1));
            assert!(outread::entry_bytes(&ifd1, 34675).is_none());
            assert!(outread::entry_bytes(&ifd1, 347).is_some());
            let mut reduced = 0;
            let mut cur = ifd1;
            loop {
                reduced += 1;
                if cur.next == 0 {
                    break;
                }
                cur = outread::read_ifd(&out, cur.next);
            }
            assert_eq!(reduced, 2, "two reduced levels after the main IFD");
        }
    }
}

fn conv_bytes(data: &[u8], profile: OutputProfile) -> Vec<u8> {
    let src = MemSource::new(data.to_vec());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    convert_svs::convert_svs(&src, &mut sink, &mut scratch, &plan_for(profile, PixelPolicy::AllowEdgeReencode))
        .unwrap();
    sink.data
}

#[test]
fn convert_ome_profile_subifds_and_xml() {
    let p = SvsGenParams { width: 500, height: 260, include_associated: true, ..Default::default() };
    let data = gen(&p);
    let r = convert(&data, OutputProfile::OmeBigTiffRgbSubifd).unwrap();
    assert_eq!(r.format, "ome-bigtiff-subifd-rgb-jpeg-pyramid");
    let out = outread::Out { data: conv_bytes(&data, OutputProfile::OmeBigTiffRgbSubifd) };
    let v = validate_output(&out, out.size(), Some(r.validation.ifd_count)).unwrap();
    assert_eq!(v.main_ifds, 1);
    assert_eq!(v.sub_ifds, 2);
    let ifd0 = outread::read_ifd(&out, outread::first_ifd(&out));
    assert_eq!(ifd0.sub.len(), 2, "levels 1..2 hang off level 0");
    assert_eq!(outread::entry_u64(&ifd0, 322), Some(256));
    let xml = outread::entry_bytes(&ifd0, 270).unwrap();
    let xml = String::from_utf8_lossy(&xml[..xml.len() - 1]);
    assert!(xml.contains("PhysicalSizeX=\"0.499\""), "MPP from description: {xml}");
    assert!(xml.contains("NominalMagnification=\"20\""), "AppMag as objective");
    assert!(xml.contains("aperio-svs-jpeg"));
    assert!(xml.contains("adapter_version"));
    assert!(xml.contains("TiffData IFD=\"0\""));
    let sub = outread::read_ifd(&out, ifd0.sub[0]);
    assert_eq!(outread::entry_u64(&sub, 254), Some(1));
    assert!(outread::entry_bytes(&sub, 270).is_none(), "SubIFDs carry no description");
}

#[test]
fn missing_mpp_and_appmag_stay_unknown() {
    let p = SvsGenParams { mpp: None, appmag: None, ..Default::default() };
    let data = gen(&p);
    let doc = {
        let src = MemSource::new(data.clone());
        svs::probe_svs(&src).unwrap()
    };
    assert_eq!(doc.mpp, None);
    assert_eq!(doc.appmag, None);
    let out = outread::Out { data: conv_bytes(&data, OutputProfile::ClassicJpegBigTiff) };
    let ifd0 = outread::read_ifd(&out, outread::first_ifd(&out));
    assert!(outread::entry_bytes(&ifd0, 282).is_none(), "no invented XResolution");
    assert!(outread::entry_bytes(&ifd0, 283).is_none());
    assert!(outread::entry_bytes(&ifd0, 296).is_none());
    let desc = String::from_utf8_lossy(outread::entry_bytes(&ifd0, 270).unwrap());
    assert!(desc.contains("\"mpp_x\": null"));
    assert!(desc.contains("\"objective\": null"));

    let ome = outread::Out { data: conv_bytes(&data, OutputProfile::OmeBigTiffRgbSubifd) };
    let ifd0 = outread::read_ifd(&ome, outread::first_ifd(&ome));
    let xml = outread::entry_bytes(&ifd0, 270).unwrap();
    let xml = String::from_utf8_lossy(&xml[..xml.len() - 1]);
    assert!(!xml.contains("PhysicalSizeX"), "no invented physical size");
    assert!(!xml.contains("NominalMagnification"), "no invented objective");
    assert!(xml.contains("mpp_source>unknown") || xml.contains("unknown"));
}

#[test]
fn ycbcr_variant_tags_and_subsampling() {
    let p = SvsGenParams { color: FixtureColor::YCbCr, ..Default::default() };
    let data = gen(&p);
    let doc = {
        let src = MemSource::new(data.clone());
        svs::probe_svs(&src).unwrap()
    };
    assert!(doc.levels.iter().all(|l| l.color == svs::PayloadColor::YCbCr));
    let out = outread::Out { data: conv_bytes(&data, OutputProfile::ClassicJpegBigTiff) };
    let ifd0 = outread::read_ifd(&out, outread::first_ifd(&out));
    assert_eq!(outread::entry_u64(&ifd0, 262), Some(6));
    let sub = outread::entry_bytes(&ifd0, 530).unwrap();
    let h = u16::from_le_bytes([sub[0], sub[1]]);
    let v = u16::from_le_bytes([sub[2], sub[3]]);
    assert_eq!((h, v), (2, 1), "SOF truth (4:2:2), not the tag");
}

#[test]
fn cropped_tail_tiles_pass_through_and_are_recorded() {
    let p = SvsGenParams { width: 500, height: 260, crop_tail_tiles: true, ..Default::default() };
    let data = gen(&p);
    let r = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert_eq!(r.count_reencoded(), 0);
    assert!(!r.edge_regions.is_empty(), "cropped tiles recorded");
    assert!(r.warnings.iter().any(|w| w == "svs_cropped_edge_tile_passthrough"));
    // strict-lossless accepts: no pixel is ever re-encoded in this adapter
    let src = MemSource::new(data.clone());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    convert_svs::convert_svs(
        &src,
        &mut sink,
        &mut scratch,
        &plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::StrictLossless),
    )
    .unwrap();
    // byte-identical between policies
    let a = conv_bytes(&data, OutputProfile::ClassicJpegBigTiff);
    assert_eq!(sink.data, a);
}

#[test]
fn payloads_are_byte_identical_to_the_source_tiles() {
    let p = SvsGenParams { width: 500, height: 260, ..Default::default() };
    let data = gen(&p);
    let out = outread::Out { data: conv_bytes(&data, OutputProfile::ClassicJpegBigTiff) };
    // source doc tiles
    let src = MemSource::new(data);
    let doc = svs::probe_svs(&src).unwrap();
    let hdr = slide_transform_core::tiff_read::read_header(&src).unwrap();
    let chain = slide_transform_core::tiff_read::ifd_chain(&src, &hdr).unwrap();
    let mut src_tiles: Vec<Vec<u8>> = Vec::new();
    for lv in &doc.levels {
        let ifd = &chain[lv.ifd_index as usize];
        let pairs = slide_transform_core::tiff_read::tile_pairs(&src, &hdr, ifd).unwrap();
        for (o, c) in pairs {
            src_tiles.push(src.read_at(o, c as usize).unwrap());
        }
    }
    // output tiles in IFD order
    let mut got: Vec<Vec<u8>> = Vec::new();
    let mut at = outread::first_ifd(&out);
    while at != 0 {
        let ifd = outread::read_ifd(&out, at);
        let (_, typ, cnt, ob) = ifd
            .entries
            .iter()
            .find(|(t, ..)| *t == 324)
            .cloned()
            .unwrap();
        assert_eq!(typ, 16);
        let offs: Vec<u64> = (0..cnt as usize)
            .map(|i| u64::from_le_bytes(ob[i * 8..(i + 1) * 8].try_into().unwrap()))
            .collect();
        let (_, _, _, cb) = ifd
            .entries
            .iter()
            .find(|(t, ..)| *t == 325)
            .cloned()
            .unwrap();
        let counts: Vec<u64> = (0..cnt as usize)
            .map(|i| u64::from_le_bytes(cb[i * 8..(i + 1) * 8].try_into().unwrap()))
            .collect();
        for (o, c) in offs.iter().zip(counts.iter()) {
            got.push(out.read_at(*o, *c as usize).unwrap());
        }
        at = ifd.next;
    }
    assert_eq!(got, src_tiles, "every output tile byte equals its source tile");
}

#[test]
fn big_endian_and_bigtiff_sources_read() {
    for (be, bt) in [(true, false), (false, true), (true, true)] {
        let p = SvsGenParams {
            width: 300,
            height: 200,
            big_endian: be,
            bigtiff: bt,
            ..Default::default()
        };
        let data = gen(&p);
        let r = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap();
        assert_eq!(r.levels.len(), 3, "300x200 -> 75x50 -> 19x13");
        assert_eq!(r.count_reencoded(), 0);
    }
}

// --------------------------------------------------------------------------- //
// variant rejections
// --------------------------------------------------------------------------- //

#[test]
fn jpeg2000_rejected_with_specific_reason() {
    let mut data = gen(&SvsGenParams::default());
    // Compression tag (259) lives in IFD 0's entry table; find and rewrite it
    // to 33003. Layout: header 8, payloads, IFDs. Walk to IFD 0 first.
    let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
    let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
    for i in 0..n {
        let e = first + 2 + i * 12;
        let tag = u16::from_le_bytes([data[e], data[e + 1]]);
        if tag == 259 {
            let typ = u16::from_le_bytes([data[e + 2], data[e + 3]]);
            assert_eq!(typ, 3);
            data[e + 8] = (33003 & 0xFF) as u8;
            data[e + 9] = (33003 >> 8) as u8;
        }
    }
    let err = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap_err();
    assert_eq!(err.code, UnsupportedKfbVariant);
    assert!(err.message.contains("JPEG 2000"), "{}", err.message);
    assert!(err.message.contains("33003"), "{}", err.message);
}

#[test]
fn non_aperio_tiff_rejected() {
    let p = SvsGenParams::default();
    let mut data = gen(&p);
    // scrub the vendor marker from the main description: overwrite bytes of
    // the description payload (find "Aperio" after the header/payload area)
    let pos = data
        .windows(6)
        .position(|w| w == b"Aperio")
        .expect("description contains Aperio");
    for i in 0..6 {
        data[pos + i] = b'X';
    }
    expect_svs_error(data, OutputProfile::ClassicJpegBigTiff, UnsupportedKfbVariant, "non-aperio");
}

#[test]
fn planar_and_fluorescence_pages_rejected() {
    // planar=2
    let mut data = gen(&SvsGenParams::default());
    {
        let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
        let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
        for i in 0..n {
            let e = first + 2 + i * 12;
            let tag = u16::from_le_bytes([data[e], data[e + 1]]);
            if tag == 284 {
                data[e + 8] = 0;
                data[e + 9] = 2;
            }
        }
    }
    let err = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap_err();
    assert_eq!(err.code, UnsupportedKfbVariant);
    assert!(err.message.contains("PlanarConfiguration"), "{}", err.message);

    // SamplesPerPixel = 4 (fluorescence page set)
    let mut data = gen(&SvsGenParams::default());
    {
        let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
        let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
        for i in 0..n {
            let e = first + 2 + i * 12;
            let tag = u16::from_le_bytes([data[e], data[e + 1]]);
            if tag == 277 {
                data[e + 8] = 0;
                data[e + 9] = 4;
            }
        }
    }
    let err = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap_err();
    assert_eq!(err.code, UnsupportedKfbVariant);
    assert!(err.message.contains("SamplesPerPixel"), "{}", err.message);
}

#[test]
fn fl_profile_refused_for_svs() {
    let data = gen(&SvsGenParams::default());
    expect_svs_error(data, OutputProfile::OmeBigTiffSubifd, UnsupportedKfbVariant, "fl-profile");
}

// --------------------------------------------------------------------------- //
// malformed corpus (bounded reader)
// --------------------------------------------------------------------------- //

#[test]
fn truncated_header() {
    expect_svs_error(vec![0x49, 0x49, 0x2A], OutputProfile::ClassicJpegBigTiff, InvalidKfbHeader, "short");
}

#[test]
fn bad_version() {
    let mut data = gen(&SvsGenParams::default());
    data[2] = 0x2B; // 43 without the BigTIFF header layout
    data[3] = 0x00;
    expect_svs_error(data, OutputProfile::ClassicJpegBigTiff, UnsupportedKfbVariant, "version");
}

#[test]
fn ifd_pointer_loop() {
    let mut data = gen(&SvsGenParams::default());
    // point the first IFD at itself
    let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as u64;
    data[4..8].copy_from_slice(&(first as u32).to_le_bytes()); // unchanged value; instead loop the chain tail:
    let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
    let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
    let next_at = first + 2 + n * 12;
    let next = u32::from_le_bytes([data[next_at], data[next_at + 1], data[next_at + 2], data[next_at + 3]]) as u32;
    assert_ne!(next, 0);
    let nn = u16::from_le_bytes([data[next as usize], data[next as usize + 1]]) as usize;
    let tail_next = next as usize + 2 + nn * 12;
    data[tail_next..tail_next + 4].copy_from_slice(&(first as u32).to_le_bytes());
    expect_svs_error(data, OutputProfile::ClassicJpegBigTiff, ConversionValidationFailed, "loop");
}

#[test]
fn out_of_bounds_tile_offset() {
    let mut data = gen(&SvsGenParams::default());
    let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
    let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
    let mut tile_offs_val = 0u64;
    let mut tile_cnt = 0u64;
    for i in 0..n {
        let e = first + 2 + i * 12;
        let tag = u16::from_le_bytes([data[e], data[e + 1]]);
        let count = u32::from_le_bytes([data[e + 4], data[e + 5], data[e + 6], data[e + 7]]) as u64;
        let val = u32::from_le_bytes([data[e + 8], data[e + 9], data[e + 10], data[e + 11]]) as u64;
        if tag == 324 {
            tile_offs_val = val;
            tile_cnt = count;
        }
    }
    assert!(tile_cnt * 4 > 4);
    let at = tile_offs_val as usize; // first tile offset element
    let huge = 1u32 << 30;
    data[at..at + 4].copy_from_slice(&huge.to_le_bytes());
    expect_svs_error(data, OutputProfile::ClassicJpegBigTiff, TilePayloadOutOfBounds, "oob-tile");
}

#[test]
fn huge_dimensions_rejected() {
    let mut data = gen(&SvsGenParams::default());
    let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
    let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
    for i in 0..n {
        let e = first + 2 + i * 12;
        let tag = u16::from_le_bytes([data[e], data[e + 1]]);
        if tag == 256 {
            let v = 1_500_000u32; // above the 1e6 side cap
            data[e + 8..e + 12].copy_from_slice(&v.to_le_bytes());
        }
    }
    expect_svs_error(data, OutputProfile::ClassicJpegBigTiff, UnsupportedKfbVariant, "huge-dims");
}

#[test]
fn wrong_tag_type_rejected() {
    let mut data = gen(&SvsGenParams::default());
    let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
    let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
    for i in 0..n {
        let e = first + 2 + i * 12;
        let tag = u16::from_le_bytes([data[e], data[e + 1]]);
        if tag == 259 {
            // make Compression an ASCII count of 4 → scalar read must fail
            data[e + 2] = 0;
            data[e + 3] = 2; // ASCII
        }
    }
    let err = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap_err();
    assert!(
        err.code == UnsupportedKfbVariant || err.code == ConversionValidationFailed,
        "{}",
        err.code.stable_code()
    );
}

#[test]
fn tile_count_mismatch_rejected() {
    let mut data = gen(&SvsGenParams::default());
    let first = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
    let n = u16::from_le_bytes([data[first], data[first + 1]]) as usize;
    for i in 0..n {
        let e = first + 2 + i * 12;
        let tag = u16::from_le_bytes([data[e], data[e + 1]]);
        if tag == 325 {
            // drop one byte count → count mismatch with the grid
            let count = u32::from_le_bytes([data[e + 4], data[e + 5], data[e + 6], data[e + 7]]);
            data[e + 4..e + 8].copy_from_slice(&(count - 1).to_le_bytes());
        }
    }
    expect_svs_error(data, OutputProfile::ClassicJpegBigTiff, ConversionValidationFailed, "count-mismatch");
}

#[test]
fn corrupt_tile_payload_rejected() {
    let mut data = gen(&SvsGenParams::default());
    // smash the first tile payload after the 8-byte header
    for i in 8..8 + 64 {
        data[i] ^= 0xFF;
    }
    let err = convert(&data, OutputProfile::ClassicJpegBigTiff).unwrap_err();
    assert!(err.code == JpegDecodeFailed || err.code == UnsupportedKfbVariant, "{}", err.code.stable_code());
}

// --------------------------------------------------------------------------- //
// resume (byte-identical continuation)
// --------------------------------------------------------------------------- //

#[test]
fn resume_mid_level_is_byte_identical() {
    use slide_transform_core::io::{FileScratch, FileSink, FileSource};
    use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
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

    let dir = std::env::temp_dir().join(format!(
        "stf1-svs-resume-{}-{}",
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
        build_synthetic_svs(
            &mut sink,
            &SvsGenParams { width: 1024, height: 520, ..Default::default() },
        )
        .unwrap();
        sink.flush().unwrap();
    }
    let plan = || plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode);

    // reference run
    let ref_out = dir.join("ref.ome.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_svs::convert_svs(
            &FileSource::open(&in_path).unwrap(),
            &mut out,
            &mut scratch,
            &plan(),
        )
        .unwrap();
        out.flush().unwrap();
        let v = validate_output(&FileSource::open(&ref_out).unwrap(), r.output_bytes, None)
            .unwrap();
        assert_eq!(v.ifd_count as u32, r.validation.ifd_count);
    }
    let full = std::fs::read(&ref_out).unwrap();

    // crashed run: cancel from inside checkpoint k
    let stop = 3usize;
    let part_out = dir.join("part.ome.tif");
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: Some(stop),
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
    assert!(states.len() >= stop, "checkpoints recorded");
    let st = &states[stop - 1];
    let rp = slide_transform_core::resume::ResumePoint {
        level: st.level as usize,
        channel: 0,
        cell: st.cell_done,
        committed_output: st.committed_output,
        ifd_tiles: st.ifd_tiles.clone(),
    };
    rp.validate().unwrap();
    assert!(rp.cell > 0 || rp.level > 0, "a genuine mid-run state");

    // crash aftermath the host guarantees: output truncated to committed,
    // offcnt scratch streams truncated to 12 B per committed tile
    {
        let f = std::fs::OpenOptions::new().write(true).open(&part_out).unwrap();
        f.set_len(rp.committed_output).unwrap();
    }
    for (i, &tiles) in rp.ifd_tiles.iter().enumerate() {
        let f = std::fs::OpenOptions::new()
            .write(true)
            .open(crash_dir.join(format!(".kfb2tiff-scratch-offcnt-l{i}")))
            .unwrap();
        f.set_len(tiles * 12).unwrap();
    }

    // resume with the same (preserving) scratch factory
    let r = {
        let mut out = FileSink::open_preserve(&part_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let r = convert_svs::convert_svs_to_bigtiff_resume(
            &FileSource::open(&in_path).unwrap(),
            &mut out,
            &mut scratch2,
            &plan(),
            &job,
            &rp,
        )
        .unwrap();
        out.flush().unwrap();
        r
    };
    let got = std::fs::read(&part_out).unwrap();
    assert_eq!(got, full, "resumed output byte-identical to a fresh run");
    assert_eq!(r.output_bytes as usize, full.len());
    assert_eq!(r.count_reencoded(), 0);
    assert_eq!(r.source_format, Some("aperio-svs-jpeg"));
}
