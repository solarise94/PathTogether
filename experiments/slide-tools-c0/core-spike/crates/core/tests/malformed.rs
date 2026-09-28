//! Malformed-input tests (C0 task 7): each case must fail with a typed
//! error and bounded memory, never panic. Cases run on `MemSource` so no
//! scratch IO is involved; debug build arithmetic panics any unchecked
//! overflow.
//!
//! The in-memory fixture builder uses `synth_gen`, which requires the
//! `edge-reencode` feature (default on). `cargo test --no-default-features`
//! therefore compiles the core without these tests; the no-codec build is
//! covered by `cargo build --no-default-features` in the rerun scripts.
#![cfg(feature = "edge-reencode")]

use slide_transform_core_spike::convert::ConvertOptions;
use slide_transform_core_spike::error::ErrorCode::*;
use slide_transform_core_spike::io::{MemScratch, MemSink, MemSource};
use slide_transform_core_spike::{convert_kfb, CoreResult};

const MAGIC: [u8; 8] = [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00];

/// Minimal valid v1 KFB: 1 level, 1 full 256×256 tile (JPEG = tiny gray
/// image re-encoded from the synth generator's contract — here we borrow an
/// inline 8×8-ish valid JPEG built with jpeg-encoder through synth_gen).
fn build_valid() -> Vec<u8> {
    let mut sink = MemSink::new();
    let p = slide_transform_core_spike::synth_gen::GenParams {
        width: 256,
        height: 256,
        ..Default::default()
    };
    let n = slide_transform_core_spike::synth_gen::build_synthetic_kfb(&mut sink, &p).unwrap();
    assert_eq!(n as usize, sink.data.len());
    sink.data
}

fn expect_code(data: Vec<u8>, code: slide_transform_core_spike::error::ErrorCode, name: &str) {
    let src = MemSource::new(data);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let r: CoreResult<_> = convert_kfb(&src, &mut sink, &mut scratch, &ConvertOptions::default());
    match r {
        Err(e) => assert_eq!(e.code, code, "{name}: got {} ({})", e.code.stable_code(), e.message),
        Ok(_) => panic!("{name}: expected error {:#?}, got Ok", code),
    }
}

fn put32(b: &mut [u8], at: usize, v: u32) {
    b[at..at + 4].copy_from_slice(&v.to_le_bytes());
}
fn put64(b: &mut [u8], at: usize, v: u64) {
    b[at..at + 8].copy_from_slice(&v.to_le_bytes());
}

#[test]
fn malformed_truncated_file() {
    expect_code(vec![0u8; 50], InvalidKfbHeader, "truncated");
}

#[test]
fn malformed_bad_magic() {
    let mut d = build_valid();
    d[0] = 0xEE;
    expect_code(d, UnsupportedKfbVariant, "bad magic");
}

#[test]
fn malformed_unknown_version() {
    // version != 1 且 codec != JPEG → 不像 vendor → synth 报不支持版本
    let mut d = build_valid();
    put32(&mut d, 0x08, 7);
    d[0x20..0x24].copy_from_slice(b"RAWW");
    expect_code(d, UnsupportedKfbVariant, "unknown version");
}

#[test]
fn malformed_index_offset_beyond_eof() {
    let mut d = build_valid();
    put64(&mut d, 0x50, 1 << 40);
    expect_code(d, InvalidTileIndex, "index offset beyond eof");
}

#[test]
fn malformed_huge_tile_count() {
    let mut d = build_valid();
    put32(&mut d, 0x24, 0xFFFF_FFF0);
    expect_code(d, InvalidKfbHeader, "huge tile count");
}

#[test]
fn malformed_header_bytes_out_of_range() {
    let mut d = build_valid();
    put32(&mut d, 0x0C, 5000);
    expect_code(d, InvalidKfbHeader, "header_bytes too big");
}

#[test]
fn malformed_undefined_flag_bits() {
    let mut d = build_valid();
    put32(&mut d, 0x58, 0x6);
    expect_code(d, InvalidKfbHeader, "undefined flags");
}

#[test]
fn malformed_non_brightfield() {
    let mut d = build_valid();
    put32(&mut d, 0x58, 0);
    expect_code(d, UnsupportedKfbVariant, "not brightfield");
}

#[test]
fn malformed_payload_offset_overflow() {
    // payload_offset = u64::MAX：checked 加法必须报越界而非回绕
    let mut d = build_valid();
    let size = d.len() as u64;
    // 索引在文件尾：直接找 tile 条目（level0 单 tile，32B 对齐于 index_offset）
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    assert_eq!(index_offset, size as usize - 32);
    put64(&mut d, index_offset + 16, u64::MAX);
    expect_code(d, TilePayloadOutOfBounds, "offset u64::MAX");
}

#[test]
fn malformed_payload_offset_just_past_eof() {
    let mut d = build_valid();
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    let near_end = d.len() as u64 - 10;
    put64(&mut d, index_offset + 16, near_end);
    expect_code(d, TilePayloadOutOfBounds, "payload past eof");
}

#[test]
fn malformed_misaligned_coords() {
    let mut d = build_valid();
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 4, 13); // x_px 非网格对齐
    expect_code(d, InvalidTileIndex, "misaligned x");
}

#[test]
fn malformed_reserved_nonzero() {
    let mut d = build_valid();
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    put32(&mut d, index_offset + 28, 5);
    expect_code(d, InvalidTileIndex, "reserved != 0");
}

#[test]
fn malformed_bad_jpeg_dims_in_index() {
    let mut d = build_valid();
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    d[index_offset + 12] = 0xFF; // jpeg_w = 0x... → >256 or 0
    d[index_offset + 13] = 0x01;
    expect_code(d, InvalidTileIndex, "jpeg dims");
}

#[test]
fn malformed_duplicate_grid_cell() {
    // 两份相同的 tile 条目（第二个条目落在文件尾部追加的 32B）
    let mut d = build_valid();
    let tile_count = u32::from_le_bytes(d[0x24..0x28].try_into().unwrap());
    put32(&mut d, 0x24, tile_count + 1);
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    let dup = d[index_offset..index_offset + 32].to_vec();
    d.extend_from_slice(&dup);
    expect_code(d, InvalidTileIndex, "duplicate cell");
}

#[test]
fn malformed_corrupt_edge_tile_jpeg() {
    // 边缘 tile 的 JPEG 中段损坏（SOI/EOI 保留）→ 解码失败
    // （完整 tile 不解码即直拷——与 oracle 的搬运路径一致，损坏不在此暴露）
    let mut sink = MemSink::new();
    let p = slide_transform_core_spike::synth_gen::GenParams {
        width: 300,
        height: 300,
        ..Default::default()
    };
    slide_transform_core_spike::synth_gen::build_synthetic_kfb(&mut sink, &p).unwrap();
    let mut d = sink.data;
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    let tile_count = u32::from_le_bytes(d[0x24..0x28].try_into().unwrap()) as usize;
    let mut edge = None;
    for i in 0..tile_count {
        let e = index_offset + i * 32;
        let jw = u16::from_le_bytes([d[e + 12], d[e + 13]]);
        if jw < 256 {
            let off = u64::from_le_bytes(d[e + 16..e + 24].try_into().unwrap()) as usize;
            let len = u32::from_le_bytes(d[e + 24..e + 28].try_into().unwrap()) as usize;
            edge = Some((off, len));
            break;
        }
    }
    let (off, len) = edge.expect("fixture has an edge tile");
    for b in d.iter_mut().take(off + len - 3).skip(off + 100) {
        *b ^= 0x5A;
    }
    expect_code(d, JpegDecodeFailed, "corrupt edge jpeg");
}

#[test]
fn malformed_level_with_no_tiles() {
    // 1024×1024（level1 为 2×2 网格，非 single）：只保留 level0 的条目，
    // level_count=2 → 中间层缺失 → conversion_validation_failed
    let mut sink = MemSink::new();
    let p = slide_transform_core_spike::synth_gen::GenParams {
        width: 1024,
        height: 1024,
        ..Default::default()
    };
    slide_transform_core_spike::synth_gen::build_synthetic_kfb(&mut sink, &p).unwrap();
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
    put32(&mut d, 0x20, 2); // level_count（level1=512×512 非单网格却无 tile）
    put32(&mut d, 0x24, (kept.len() / 32) as u32);
    expect_code(d, ConversionValidationFailed, "missing level");
}

#[test]
fn malformed_vendor_truncated_index() {
    // vendor 布局：tile_count 大但索引区越出文件
    let mut d = vec![0u8; 96];
    d[0..8].copy_from_slice(&MAGIC);
    put32(&mut d, 0x08, 0); // version != 1
    put32(&mut d, 0x10, 100); // tile_count
    put32(&mut d, 0x14, 1024); // height
    put32(&mut d, 0x18, 1024); // width
    put32(&mut d, 0x1C, 20); // objective
    d[0x20..0x24].copy_from_slice(b"JPEG");
    put64(&mut d, 0x44, 96); // index_offset
    d[0x4C..0x50].copy_from_slice(&0.5f32.to_le_bytes()); // mpp
    put32(&mut d, 0x58, 256); // tile size
    expect_code(d, InvalidTileIndex, "vendor index beyond eof");
}

#[test]
fn malformed_vendor_bad_record_magic() {
    // vendor 布局：1 条记录但 magic 不对
    let mut d = vec![0u8; 96 + 64];
    d[0..8].copy_from_slice(&MAGIC);
    put32(&mut d, 0x08, 0);
    put32(&mut d, 0x10, 1); // tile_count
    put32(&mut d, 0x14, 1024);
    put32(&mut d, 0x18, 1024);
    put32(&mut d, 0x1C, 20);
    d[0x20..0x24].copy_from_slice(b"JPEG");
    put64(&mut d, 0x44, 96);
    d[0x4C..0x50].copy_from_slice(&0.5f32.to_le_bytes());
    put32(&mut d, 0x58, 256);
    // 记录头/尾 magic 均为 0 → 非法
    expect_code(d, InvalidTileIndex, "vendor bad record magic");
}

#[test]
fn malformed_associated_bad_name() {
    let mut d = build_valid();
    // 构造 1 条 associated 条目，名字非法
    let index_offset = u64::from_le_bytes(d[0x50..0x58].try_into().unwrap()) as usize;
    put32(&mut d, 0x4C, 1);
    let mut e = vec![0u8; 48];
    e[0..6].copy_from_slice(b"weird!");
    // payload 指向某个真实 JPEG（复用 tile payload）
    let off = u64::from_le_bytes(d[index_offset + 16..index_offset + 24].try_into().unwrap());
    let len = u32::from_le_bytes(d[index_offset + 24..index_offset + 28].try_into().unwrap());
    e[16..24].copy_from_slice(&off.to_le_bytes());
    e[24..28].copy_from_slice(&len.to_le_bytes());
    e[28..30].copy_from_slice(&96u16.to_le_bytes());
    e[30..32].copy_from_slice(&72u16.to_le_bytes());
    d.extend_from_slice(&e);
    expect_code(d, InvalidKfbHeader, "associated name");
}

#[test]
fn wellformed_roundtrip_smoke() {
    // 正例：合成 v1 → 转换成功，输出 BigTIFF 头与 IFD 数正确
    let d = build_valid();
    let src = MemSource::new(d);
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let stats = convert_kfb(&src, &mut sink, &mut scratch, &ConvertOptions::default())
        .expect("valid input converts");
    assert_eq!(stats.levels.len(), 1);
    assert_eq!(stats.levels[0].tiles_raw_copied, 1);
    assert_eq!(sink.data[0..2], *b"II");
    assert_eq!(u16::from_le_bytes([sink.data[2], sink.data[3]]), 43);
    assert_eq!(sink.data[4], 8);
    // first IFD offset backpatched at byte 8, non-zero, within file
    let ifd = u64::from_le_bytes(sink.data[8..16].try_into().unwrap());
    assert!(ifd > 0 && ifd < sink.data.len() as u64);
}
