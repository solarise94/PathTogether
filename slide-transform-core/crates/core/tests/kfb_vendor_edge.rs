//! KFB vendor-layout boundary-fill tiles (kfb-tile-index regression).
//!
//! Real KFBIO samples can store level-0 bottom-row tiles whose JPEG is a
//! few pixels taller than the header height leaves room for (scanner edge
//! fill vs header rounding: across the sample family every level's tile
//! extent matches the header dims exactly EXCEPT that sample's L0 bottom
//! row overruns by 1px). The index records the true JPEG dims; the canvas
//! stays header-authoritative. Parser contract after the fix:
//!   - a grid-aligned tile intersecting the level canvas is accepted as an
//!     edge tile; conversion crops the overrun rows/cols at paste time
//!     (payload decode stays strict: decoded dims must equal the record);
//!   - a tile fully outside the canvas is still `invalid_tile_index`.
//!
//! Synthetic fixture only — no patient data.

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_bf;
use slide_transform_core::error::{CoreError, ErrorCode};
use slide_transform_core::io::{ByteSource, MemSink, MemScratch, MemSource};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::plan::{InputIdentity, PixelPolicy, TransformPlan};
use slide_transform_core::tiff_read;

const KFB_MAGIC: [u8; 8] = [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00];
const TILE_HEAD: [u8; 4] = [0xF1, 0x04, 0xEE, 0xEE];
const TILE_TAIL: [u8; 4] = [0xFF, 0x04, 0xEE, 0xEE];

/// 320×300 → L0 grid 2×2 (bottom cell is 256×44), L1 = 160×150 (1×1 grid).
const WIDTH: u32 = 320;
const HEIGHT: u32 = 300;
/// Bottom-row JPEG height: 44 fits the canvas, 45 overruns it by 1px.
const FIT_H: u32 = 44;
const OVERRUN_H: u32 = 45;

fn put32(d: &mut [u8], at: usize, v: u32) {
    d[at..at + 4].copy_from_slice(&v.to_le_bytes());
}
fn put64(d: &mut [u8], at: usize, v: u64) {
    d[at..at + 8].copy_from_slice(&v.to_le_bytes());
}

struct Fixture {
    data: Vec<u8>,
    /// (x, y, jpeg_w, jpeg_h) per tile record, index order == file order.
    tiles: Vec<(u32, u32, u32, u32)>,
    payload_ranges: Vec<(u64, u32)>,
}

/// Build a minimal vendor-layout KFB: header + payloads + tile index.
/// `extra_outside_tile` appends a fully-outside L0 tile (corrupt-index case).
fn build_kfb(bottom_h: u32, extra_outside_tile: bool) -> Fixture {
    // (x, y, jw, jh, scale)
    let mut tiles: Vec<(u32, u32, u32, u32, f32)> = vec![
        (0, 0, 256, 256, 20.0),
        (256, 0, 64, 256, 20.0),
        (0, 256, 256, bottom_h, 20.0),
        (256, 256, 64, bottom_h, 20.0),
        (0, 0, 160, 150, 10.0), // L1
    ];
    if extra_outside_tile {
        tiles.push((1024, 0, 64, 64, 20.0)); // x ≥ WIDTH → fully outside
    }

    // payloads first (deterministic gradient; the overrun row, if any, is
    // solid red so a wrongly-pasted 45th row is unmistakable)
    let mut payloads: Vec<Vec<u8>> = Vec::new();
    for &(x, y, jw, jh, _) in &tiles {
        let mut canvas = vec![0u8; (jw * jh * 3) as usize];
        for ty in 0..jh {
            let (r, g, b) = if y + ty == HEIGHT && jh == OVERRUN_H {
                // the one row that must never reach the canvas
                (255u8, 0u8, 0u8)
            } else {
                (
                    ((ty * 37 + 3) % 256) as u8,
                    ((ty * 11 + x + 20) % 256) as u8,
                    ((ty * 5 + 90) % 256) as u8,
                )
            };
            for tx in 0..jw {
                let p = ((ty * jw + tx) * 3) as usize;
                canvas[p] = r;
                canvas[p + 1] = g;
                canvas[p + 2] = b;
            }
        }
        let cfg = slide_transform_core::jpeg::EncoderCfg::with_quality(
            90,
            slide_transform_core::jpeg::Sampling::S420,
        );
        payloads.push(slide_transform_core::jpeg::encode_rgb(&canvas, jw, jh, &cfg).unwrap());
    }

    let tile_count = tiles.len() as u32;
    let index_bytes = tile_count as u64 * 64;
    let mut data: Vec<u8> = vec![0u8; 96];
    data[0..8].copy_from_slice(&KFB_MAGIC);
    put32(&mut data, 0x08, 0); // vendor version
    put32(&mut data, 0x10, tile_count);
    put32(&mut data, 0x14, HEIGHT); // height first
    put32(&mut data, 0x18, WIDTH);
    put32(&mut data, 0x1C, 20); // objective
    data[0x20..0x24].copy_from_slice(b"JPEG");
    put32(&mut data, 0x4C, 0.5f32.to_bits()); // mpp
    put32(&mut data, 0x58, 256); // tile size

    let mut payload_ranges = Vec::new();
    for p in &payloads {
        payload_ranges.push((data.len() as u64, p.len() as u32));
        data.extend_from_slice(p);
    }
    let index_offset = data.len() as u64;
    put64(&mut data, 0x44, index_offset);

    for (i, &(_, _, _, _, scale)) in tiles.iter().enumerate() {
        let va = payload_ranges[i].0 as i64 - index_offset as i64;
        let mut rec = [0u8; 64];
        rec[0..4].copy_from_slice(&TILE_HEAD);
        put32(&mut rec, 4, tiles[i].0);
        put32(&mut rec, 8, tiles[i].1);
        put32(&mut rec, 12, tiles[i].2);
        put32(&mut rec, 16, tiles[i].3);
        put32(&mut rec, 20, scale.to_bits());
        put32(&mut rec, 32, payloads[i].len() as u32);
        put32(&mut rec, 36, va as u32);
        put32(&mut rec, 40, 0xFFFF_FFFF);
        for k in 0..4 {
            put32(&mut rec, 44 + k * 4, 20);
        }
        rec[60..64].copy_from_slice(&TILE_TAIL);
        data.extend_from_slice(&rec);
    }
    debug_assert_eq!(data.len() as u64, index_offset + index_bytes);
    Fixture {
        data,
        tiles: tiles.iter().map(|t| (t.0, t.1, t.2, t.3)).collect(),
        payload_ranges,
    }
}

/// Convert and return (stats, output bytes).
fn convert(
    fix: &Fixture,
    policy: PixelPolicy,
) -> Result<(slide_transform_core::report::TransformResult, Vec<u8>), CoreError> {
    let src = MemSource::new(fix.data.clone());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = TransformPlan::brightfield(InputIdentity::default()).with_policy(policy);
    let null = NullProgress;
    let job = JobControl::new(&null);
    let stats = convert_bf::convert_kfb_to_bigtiff(&src, &mut sink, &mut scratch, &plan, &job)?;
    Ok((stats, sink.data))
}

/// Output tile payloads in TIFF order via the test TIFF reader.
fn output_tile_payloads(out: &[u8], ifd_idx: usize) -> Vec<(u64, u64)> {
    let src = MemSource::new(out.to_vec());
    let hdr = tiff_read::read_header(&src).unwrap();
    let chain = tiff_read::ifd_chain(&src, &hdr).unwrap();
    let mut cur = tiff_read::TileCursor::new(&src, &hdr, &chain[ifd_idx]).unwrap();
    let mut pairs = Vec::new();
    while let Some(p) = cur.next_pair().unwrap() {
        pairs.push(p);
    }
    pairs
}

fn mean_abs(a: &[u8], b: &[u8]) -> f64 {
    a.iter()
        .zip(b)
        .map(|(x, y)| (*x as i32 - *y as i32).unsigned_abs() as f64)
        .sum::<f64>()
        / a.len().max(1) as f64
}

fn mean_dist_from_white(px: &[u8]) -> f64 {
    px.iter().map(|v| (255 - *v) as f64).sum::<f64>() / px.len().max(1) as f64
}

#[test]
fn vendor_fit_height_baseline_still_works() {
    let fix = build_kfb(FIT_H, false);
    let (out, _bytes) = convert(&fix, PixelPolicy::AllowEdgeReencode).expect("must succeed");
    assert_eq!((out.levels[0].width, out.levels[0].height), (320, 300));
    assert_eq!((out.levels[1].width, out.levels[1].height), (160, 150));
    assert_eq!(out.levels[0].tiles_raw_copied, 1);
    assert_eq!(out.levels[0].tiles_reencoded, 3);
}

#[test]
fn vendor_bottom_overrun_converts_and_crops() {
    let fix = build_kfb(OVERRUN_H, false);
    let (out, bytes) = convert(&fix, PixelPolicy::AllowEdgeReencode).expect("must succeed");
    // header-authoritative canvas: 320×300 (never 320×301)
    assert_eq!((out.levels[0].width, out.levels[0].height), (320, 300));
    assert_eq!((out.levels[1].width, out.levels[1].height), (160, 150));
    // full tile raw-copied; both overrun bottom tiles + right edge re-encoded
    assert_eq!(out.levels[0].tiles_raw_copied, 1);
    assert_eq!(out.levels[0].tiles_reencoded, 3);
    assert_eq!(out.levels[0].tiles_total, 4);

    // output IFD0 geometry must be the header canvas
    {
        let osrc = MemSource::new(bytes.clone());
        let hdr = tiff_read::read_header(&osrc).unwrap();
        let chain = tiff_read::ifd_chain(&osrc, &hdr).unwrap();
        assert_eq!(chain.len(), 2);
        assert_eq!(tiff_read::find_u64(&osrc, &hdr, &chain[0], 256).unwrap(), Some(320));
        assert_eq!(tiff_read::find_u64(&osrc, &hdr, &chain[0], 257).unwrap(), Some(300));
    }

    // per-tile pixel check on the two overrun tiles (records 2 and 3)
    let tiles = output_tile_payloads(&bytes, 0);
    assert_eq!(tiles.len(), 4);
    for rec in [2usize, 3] {
        let (sx, sy, jw, jh) = fix.tiles[rec];
        assert_eq!(jh, OVERRUN_H);
        let (off, len) = fix.payload_ranges[rec];
        let src = MemSource::new(fix.data.clone());
        let payload = src.read_at(off, len as usize).unwrap();
        let decoded = slide_transform_core::jpeg::decode(&payload, 512 * 512).unwrap();
        assert_eq!((decoded.width, decoded.height), (jw, jh));

        // output tile: TIFF order is row-major → record 2 → tile 2, 3 → 3
        let (toff, tlen) = tiles[rec];
        let osrc = MemSource::new(bytes.clone());
        let out_jpeg = osrc.read_at(toff, tlen as usize).unwrap();
        let out_img = slide_transform_core::jpeg::decode(&out_jpeg, 512 * 512).unwrap();
        assert_eq!((out_img.width, out_img.height), (256, 256));

        let cell_h = (HEIGHT - sy).min(256) as usize; // 44
        let row_bytes =
            |img: &slide_transform_core::jpeg::DecodedImage, y: usize| -> Vec<u8> {
                let w = img.width as usize;
                img.data[y * w * 3..(y + 1) * w * 3].to_vec()
            };
        // content rows survive (lossy re-encode → small tolerance)
        for y in [0usize, 1, cell_h / 2, cell_h - 1] {
            let d = mean_abs(&row_bytes(&decoded, y), &row_bytes(&out_img, y));
            assert!(d < 8.0, "tile{rec} content row {y} diverged: {d}");
        }
        // the overrun red row must NOT be pasted. (S420 subsampling softens
        // a 1-px solid-red row, so compare means rather than thresholds.)
        // fixture sanity: row 44 is visibly redder than row 43 in the source
        let red_mean = |r: &[u8]| r.iter().step_by(3).map(|v| *v as f64).sum::<f64>() / (r.len() / 3) as f64;
        assert!(
            red_mean(&row_bytes(&decoded, cell_h)) > red_mean(&row_bytes(&decoded, cell_h - 1)) + 10.0,
            "fixture overrun row must be redder than the row above"
        );
        // crop check: output row 44 is near-white and far from the source row
        let out_d = mean_dist_from_white(&row_bytes(&out_img, cell_h));
        assert!(out_d < 20.0, "tile{rec} overrun row is not near-white: dist={out_d}");
        let dropped = mean_abs(&row_bytes(&decoded, cell_h), &row_bytes(&out_img, cell_h));
        assert!(dropped > 25.0, "tile{rec} overrun row looks pasted: diff={dropped}");
        // rows below the cell are pure white
        for y in [cell_h + 4, 128, 255] {
            assert!(row_bytes(&out_img, y) == vec![255u8; 768],
                "tile{rec} row {y} must be exactly white");
        }
    }
}

#[test]
fn vendor_fully_outside_tile_still_rejected() {
    let fix = build_kfb(OVERRUN_H, true);
    let err = convert(&fix, PixelPolicy::AllowEdgeReencode).unwrap_err();
    assert_eq!(err.code, ErrorCode::InvalidTileIndex, "got: {err:?}");
    assert!(err.message.contains("完全越出"), "got: {}", err.message);
}

#[test]
fn vendor_overrun_refuses_strict_lossless() {
    let fix = build_kfb(OVERRUN_H, false);
    let err = convert(&fix, PixelPolicy::StrictLossless).unwrap_err();
    assert_eq!(err.code, ErrorCode::PixelPolicyViolation, "got: {err:?}");
}
