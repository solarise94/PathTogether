//! Malformed-input corpus (C1 item 7): every case must fail with a typed
//! error and never panic. Runs on `MemSource`/`MemSink`; debug builds turn
//! any unchecked overflow into a panic, so passing these tests also proves
//! checked arithmetic on the hostile paths. Requires `fixtures` (default-on
//! for the CLI feature set; run with `cargo test -p slide-transform-core
//! --features fixtures`).

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_bf;
use slide_transform_core::convert_fl;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::io::{MemScratch, MemSink, MemSource};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::kfbf::fixture::{KfbfGenParams, build_synthetic_kfbf};
use slide_transform_core::plan::TransformPlan;
use slide_transform_core::synth_gen::{self, GenParams};

const KFB_MAGIC: [u8; 8] = [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00];
const KFBF_MAGIC: [u8; 8] = [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x46];

fn put32(b: &mut [u8], at: usize, v: u32) {
    b[at..at + 4].copy_from_slice(&v.to_le_bytes());
}
fn put64(b: &mut [u8], at: usize, v: u64) {
    b[at..at + 8].copy_from_slice(&v.to_le_bytes());
}

// --------------------------------------------------------------------------- //
// KFB (synthetic contract)
// --------------------------------------------------------------------------- //

fn valid_kfb(w: u32, h: u32) -> Vec<u8> {
    let mut sink = MemSink::new();
    let p = GenParams { width: w, height: h, ..Default::default() };
    synth_gen::build_synthetic_kfb(&mut sink, &p).unwrap();
    sink.data
}

fn expect_kfb_error(data: Vec<u8>, code: slide_transform_core::error::ErrorCode, name: &str) {
    let src = MemSource::new(data);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = TransformPlan::brightfield(Default::default());
    let r = convert_bf::convert_kfb(&src, &mut sink, &mut scratch, &plan);
    match r {
        Err(e) => assert_eq!(
            e.code, code,
            "{name}: got {} ({})",
            e.code.stable_code(),
            e.message
        ),
        Ok(_) => panic!("{name}: expected error {:#?}", code),
    }
}

#[test]
fn kfb_truncated() {
    expect_kfb_error(vec![0u8; 50], InvalidKfbHeader, "truncated");
}

#[test]
fn kfb_bad_magic() {
    let mut d = valid_kfb(256, 256);
    d[0] = 0xEE;
    expect_kfb_error(d, UnsupportedKfbVariant, "bad magic");
}

#[test]
fn kfb_unknown_version() {
    let mut d = valid_kfb(256, 256);
    put32(&mut d, 0x08, 7);
    d[0x20..0x24].copy_from_slice(b"RAWW");
    expect_kfb_error(d, UnsupportedKfbVariant, "unknown version");
}

#[test]
fn kfb_index_offset_beyond_eof() {
    let mut d = valid_kfb(256, 256);
    put64(&mut d, 0x50, 1 << 40);
    expect_kfb_error(d, InvalidTileIndex, "index offset beyond eof");
}

#[test]
fn kfb_huge_tile_count() {
    let mut d = valid_kfb(256, 256);
    put32(&mut d, 0x24, 0xFFFF_FFF0);
    expect_kfb_error(d, InvalidKfbHeader, "huge tile count");
}

#[test]
fn kfb_header_bytes_out_of_range() {
    let mut d = valid_kfb(256, 256);
    put32(&mut d, 0x0C, 5000);
    expect_kfb_error(d, InvalidKfbHeader, "header_bytes too big");
}

#[test]
fn kfb_undefined_flag_bits() {
    let mut d = valid_kfb(256, 256);
    put32(&mut d, 0x58, 0x6);
    expect_kfb_error(d, InvalidKfbHeader, "undefined flags");
}

#[test]
fn kfb_non_brightfield() {
    let mut d = valid_kfb(256, 256);
    put32(&mut d, 0x58, 0);
    expect_kfb_error(d, UnsupportedKfbVariant, "not brightfield");
}

#[test]
fn kfb_payload_offset_overflow() {
    let mut d = valid_kfb(256, 256);
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    assert_eq!(index_offset, d.len() - 32);
    put64(&mut d, index_offset + 16, u64::MAX);
    expect_kfb_error(d, TilePayloadOutOfBounds, "offset u64::MAX");
}

#[test]
fn kfb_payload_offset_just_past_eof() {
    let mut d = valid_kfb(256, 256);
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    let near_end = d.len() as u64 - 10;
    put64(&mut d, index_offset + 16, near_end);
    expect_kfb_error(d, TilePayloadOutOfBounds, "payload past eof");
}

#[test]
fn kfb_misaligned_coords() {
    let mut d = valid_kfb(256, 256);
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 4, 13);
    expect_kfb_error(d, InvalidTileIndex, "misaligned x");
}

#[test]
fn kfb_reserved_nonzero() {
    let mut d = valid_kfb(256, 256);
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 28, 5);
    expect_kfb_error(d, InvalidTileIndex, "reserved != 0");
}

#[test]
fn kfb_bad_jpeg_dims_in_index() {
    let mut d = valid_kfb(256, 256);
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    d[index_offset + 12] = 0xFF;
    d[index_offset + 13] = 0x01;
    expect_kfb_error(d, InvalidTileIndex, "jpeg dims");
}

#[test]
fn kfb_duplicate_grid_cell() {
    let mut d = valid_kfb(256, 256);
    let tile_count = u32::from_le_bytes(d[0x24..0x28].try_into().unwrap());
    put32(&mut d, 0x24, tile_count + 1);
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    let dup = d[index_offset..index_offset + 32].to_vec();
    d.extend_from_slice(&dup);
    expect_kfb_error(d, InvalidTileIndex, "duplicate cell");
}

#[test]
fn kfb_corrupt_edge_tile_jpeg() {
    let mut sink = MemSink::new();
    let p = GenParams { width: 300, height: 300, ..Default::default() };
    synth_gen::build_synthetic_kfb(&mut sink, &p).unwrap();
    let mut d = sink.data;
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    let tile_count = u32::from_le_bytes(d[0x24..0x28].try_into().unwrap()) as usize;
    let mut edge = None;
    for i in 0..tile_count {
        let e = index_offset + i * 32;
        let jw = u16::from_le_bytes([d[e + 12], d[e + 13]]);
        if jw < 256 {
            let off =
                u64::from_le_bytes(d[e + 16..e + 24].try_into().unwrap()) as usize;
            let len = u32::from_le_bytes(d[e + 24..e + 28].try_into().unwrap()) as usize;
            edge = Some((off, len));
            break;
        }
    }
    let (off, len) = edge.expect("fixture has an edge tile");
    for b in d.iter_mut().take(off + len - 3).skip(off + 100) {
        *b ^= 0x5A;
    }
    expect_kfb_error(d, JpegDecodeFailed, "corrupt edge jpeg");
}

#[test]
fn kfb_level_with_no_tiles() {
    let mut sink = MemSink::new();
    let p = GenParams { width: 1024, height: 1024, ..Default::default() };
    synth_gen::build_synthetic_kfb(&mut sink, &p).unwrap();
    let mut d = sink.data;
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    let tile_count = u32::from_le_bytes(d[0x24..0x28].try_into().unwrap()) as usize;
    let mut kept = Vec::new();
    for i in 0..tile_count {
        let e = index_offset + i * 32;
        if u32::from_le_bytes(d[e..e + 4].try_into().unwrap()) == 0 {
            kept.extend_from_slice(&d[e..e + 32]);
        }
    }
    let new_len = index_offset + kept.len();
    d.truncate(new_len);
    d[index_offset..new_len].copy_from_slice(&kept);
    put32(&mut d, 0x20, 2);
    put32(&mut d, 0x24, (kept.len() / 32) as u32);
    expect_kfb_error(d, ConversionValidationFailed, "missing level");
}

#[test]
fn kfb_vendor_truncated_index() {
    let mut d = vec![0u8; 96];
    d[0..8].copy_from_slice(&KFB_MAGIC);
    put32(&mut d, 0x08, 0);
    put32(&mut d, 0x10, 100);
    put32(&mut d, 0x14, 1024);
    put32(&mut d, 0x18, 1024);
    put32(&mut d, 0x1C, 20);
    d[0x20..0x24].copy_from_slice(b"JPEG");
    put64(&mut d, 0x44, 96);
    d[0x4C..0x50].copy_from_slice(&0.5f32.to_le_bytes());
    put32(&mut d, 0x58, 256);
    expect_kfb_error(d, InvalidTileIndex, "vendor index beyond eof");
}

#[test]
fn kfb_vendor_bad_record_magic() {
    let mut d = vec![0u8; 96 + 64];
    d[0..8].copy_from_slice(&KFB_MAGIC);
    put32(&mut d, 0x08, 0);
    put32(&mut d, 0x10, 1);
    put32(&mut d, 0x14, 1024);
    put32(&mut d, 0x18, 1024);
    put32(&mut d, 0x1C, 20);
    d[0x20..0x24].copy_from_slice(b"JPEG");
    put64(&mut d, 0x44, 96);
    d[0x4C..0x50].copy_from_slice(&0.5f32.to_le_bytes());
    put32(&mut d, 0x58, 256);
    expect_kfb_error(d, InvalidTileIndex, "vendor bad record magic");
}

#[test]
fn kfb_wellformed_roundtrip_smoke() {
    let d = valid_kfb(256, 256);
    let src = MemSource::new(d);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = TransformPlan::brightfield(Default::default());
    let stats = convert_bf::convert_kfb(&src, &mut sink, &mut scratch, &plan).unwrap();
    assert_eq!(stats.levels.len(), 1);
    assert_eq!(stats.levels[0].tiles_raw_copied, 1);
    assert_eq!(sink.data[0..2], *b"II");
    assert_eq!(u16::from_le_bytes([sink.data[2], sink.data[3]]), 43);
    let ifd = u64::from_le_bytes(sink.data[8..16].try_into().unwrap());
    assert!(ifd > 0 && ifd < sink.data.len() as u64);
}

// --------------------------------------------------------------------------- //
// KFBF (fluorescence vendor layout)
// --------------------------------------------------------------------------- //

fn valid_kfbf() -> Vec<u8> {
    let mut sink = MemSink::new();
    build_synthetic_kfbf(&mut sink, &KfbfGenParams::default()).unwrap();
    sink.data
}

fn expect_kfbf_error(data: Vec<u8>, code: slide_transform_core::error::ErrorCode, name: &str) {
    let src = MemSource::new(data);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = TransformPlan::fluorescence(Default::default());
    let r = convert_fl::convert_kfbf(&src, &mut sink, &mut scratch, &plan, None);
    match r {
        Err(e) => assert_eq!(
            e.code, code,
            "{name}: got {} ({})",
            e.code.stable_code(),
            e.message
        ),
        Ok(_) => panic!("{name}: expected error {:#?}", code),
    }
}

#[test]
fn kfbf_bad_magic() {
    let mut d = valid_kfbf();
    d[7] = 0x00; // KFBF → KFB magic
    expect_kfbf_error(d, UnsupportedKfbVariant, "bad magic");
}

#[test]
fn kfbf_unknown_version() {
    let mut d = valid_kfbf();
    put32(&mut d, 0x08, 2);
    expect_kfbf_error(d, UnsupportedKfbVariant, "version != 0");
}

#[test]
fn kfbf_bad_format_version() {
    let mut d = valid_kfbf();
    d[0x0C..0x10].copy_from_slice(&3.0f32.to_le_bytes());
    expect_kfbf_error(d, UnsupportedKfbVariant, "format != 2.1");
}

#[test]
fn kfbf_huge_tile_count() {
    let mut d = valid_kfbf();
    put32(&mut d, 0x10, 0xFFFF_FFF0);
    expect_kfbf_error(d, InvalidKfbHeader, "huge tile count");
}

#[test]
fn kfbf_index_beyond_eof() {
    let mut d = valid_kfbf();
    put64(&mut d, 0x44, 1 << 40);
    expect_kfbf_error(d, InvalidTileIndex, "index beyond eof");
}

#[test]
fn kfbf_record_magic_broken() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    d[index_offset] ^= 0xFF;
    expect_kfbf_error(d, InvalidTileIndex, "record magic");
}

#[test]
fn kfbf_reserved_nonzero() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 24, 1); // z0
    expect_kfbf_error(d, InvalidTileIndex, "reserved z0");
}

#[test]
fn kfbf_scale_not_power_of_two() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    d[index_offset + 20..index_offset + 24]
        .copy_from_slice(&3.3f32.to_le_bytes());
    expect_kfbf_error(d, InvalidTileIndex, "scale mismatch");
}

#[test]
fn kfbf_misaligned_coords() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 4, 13);
    expect_kfbf_error(d, InvalidTileIndex, "misaligned x");
}

#[test]
fn kfbf_jpeg_dims_out_of_range() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 12, 999);
    expect_kfbf_error(d, InvalidTileIndex, "jpeg dims");
}

#[test]
fn kfbf_len_ch0_mismatch() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 32, 0x7F000000);
    expect_kfbf_error(d, InvalidTileIndex, "len_ch0 mismatch");
}

#[test]
fn kfbf_side_beyond_eof() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    let va = u32::from_le_bytes(d[index_offset + 36..index_offset + 40].try_into().unwrap());
    d[index_offset + 44..index_offset + 48].copy_from_slice(&u32::MAX.to_le_bytes());
    let _ = va;
    expect_kfbf_error(d, TilePayloadOutOfBounds, "side beyond eof");
}

#[test]
fn kfbf_pointer_block_beyond_eof() {
    let mut d = valid_kfbf();
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    d[index_offset + 36..index_offset + 40].copy_from_slice(&u32::MAX.to_le_bytes());
    expect_kfbf_error(d, TilePayloadOutOfBounds, "pointer block beyond eof");
}

#[test]
fn kfbf_duplicate_cell() {
    let mut d = valid_kfbf();
    let tile_count = u32::from_le_bytes(d[0x10..0x14].try_into().unwrap());
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    let dup = d[index_offset..index_offset + 64].to_vec();
    // 末尾 52B 是 thumbnail 记录：在索引区后插入重复条目
    let thumb_at = d.len() - 52;
    let mut next = Vec::with_capacity(d.len() + 64);
    next.extend_from_slice(&d[..thumb_at]);
    next.extend_from_slice(&dup);
    next.extend_from_slice(&d[thumb_at..]);
    d = next;
    put32(&mut d, 0x10, tile_count + 1);
    put64(&mut d, 0x44, index_offset as u64);
    expect_kfbf_error(d, InvalidTileIndex, "duplicate cell");
}

#[test]
fn kfbf_extent_mismatch() {
    // 把 level1 一条记录的 jpeg_w 改小 → L1 extent 不再等于公式 300
    let mut d = valid_kfbf();
    let tile_count = u32::from_le_bytes(d[0x10..0x14].try_into().unwrap());
    let index_offset = u64::from_le_bytes(d[0x44..0x4C].try_into().unwrap()) as usize;
    let mut hit = false;
    for i in 0..tile_count as usize {
        let e = index_offset + i * 64;
        let scale = f32::from_le_bytes(d[e + 20..e + 24].try_into().unwrap());
        let lvl = ((40.0f32 / scale).log2().round()) as u32;
        let x = u32::from_le_bytes(d[e + 4..e + 8].try_into().unwrap());
        if lvl == 1 && x == 256 {
            // 这是 L1 右缘 tile（extent 的右边界）
            put32(&mut d, e + 12, 200); // jpeg_w 200 < 256 → extent 456 ≠ 512? 见公式
            hit = true;
            break;
        }
    }
    assert!(hit, "fixture has an L1 right-edge tile");
    expect_kfbf_error(d, InvalidTileIndex, "extent mismatch");
}

#[test]
fn kfbf_wellformed_roundtrip() {
    let d = valid_kfbf();
    let src = MemSource::new(d);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = TransformPlan::fluorescence(Default::default());
    let r = convert_fl::convert_kfbf(&src, &mut sink, &mut scratch, &plan, None)
        .expect("valid kfbf converts");
    assert_eq!(r.levels.len(), 6); // 3 levels × 2 channels
    assert!(r.levels.iter().any(|l| l.cells_filled_black > 0));
    assert!(r.edge_regions.iter().any(|e| !e.reused_qtables || e.reused_qtables));
    assert_eq!(sink.data[0..2], *b"II");
    // first IFD backpatched
    let ifd = u64::from_le_bytes(sink.data[8..16].try_into().unwrap());
    assert!(ifd > 0 && ifd < sink.data.len() as u64);
    // OME description present in IFD(0,0)
    let xml = String::from_utf8_lossy(&sink.data).into_owned();
    assert!(xml.contains("openmicroscopy.org/Schemas/OME/2016-06"));
    assert!(xml.contains("ExposureTimeUnit=\"ms\""));
}

#[test]
fn kfbf_strict_policy_rejects_cropped_bottom() {
    let d = valid_kfbf(); // has trimmed bottom row + missing cell
    let src = MemSource::new(d);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan =
        TransformPlan::fluorescence(Default::default()).with_policy(
            slide_transform_core::plan::PixelPolicy::StrictLossless,
        );
    let r = convert_fl::convert_kfbf(&src, &mut sink, &mut scratch, &plan, None);
    match r {
        Err(e) => assert_eq!(e.code, PixelPolicyViolation),
        Ok(_) => panic!("strict policy must reject edge/cropped input"),
    }
    assert!(sink.data.is_empty(), "strict rejection must precede any output");
}

#[test]
fn kfb_timeout_guard() {
    let d = valid_kfb(256, 256);
    let src = MemSource::new(d);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = TransformPlan::brightfield(Default::default()).with_limits(
        slide_transform_core::plan::ResourceLimits {
            timeout_seconds: 0.0,
            ..Default::default()
        },
    );
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(0.0);
    let r = convert_bf::convert_kfb_to_bigtiff(&src, &mut sink, &mut scratch, &plan, &job);
    match r {
        Err(e) => assert_eq!(e.code, ConversionTimeout),
        Ok(_) => panic!("expected timeout"),
    }
}
