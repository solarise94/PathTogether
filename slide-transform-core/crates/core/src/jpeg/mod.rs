//! Baseline JPEG codec hand-written to be bit-compatible with
//! libjpeg-turbo/Pillow for both decode and encode (see `decoder.rs` /
//! `encoder.rs` module docs). No third-party JPEG dependency is used — the
//! license rationale is recorded in `docs/slide-tools/c1-core-report.md`.

pub mod decoder;
pub mod encoder;
pub mod tables;

pub use decoder::{decode, decode_ex, scan_jpeg, ColorKind, DecodedImage, JpegProbe};
pub use encoder::{encode_gray, encode_rgb, EncoderCfg, Sampling};

/// True colorspace decision for a 3-component JPEG inside a TIFF, mirroring
/// tifffile's `jpeg_decode_colorspace` (the rule that decodes Aperio SVS
/// correctly, verified pixel-exact against OpenSlide):
///
/// - JFIF APP0 present ⇒ YCbCr (JFIF 3-component is YCbCr by definition);
/// - Adobe APP14: transform 0 ⇒ RGB, anything else ⇒ YCbCr;
/// - neither marker ⇒ the stream alone is ambiguous (libjpeg would default
///   to YCbCr); the TIFF PhotometricInterpretation decides: 2 (RGB) ⇒ RGB —
///   the Aperio `JPEG/RGB` convention with component ids 0,1,2 — while 6
///   (YCbCr) ⇒ YCbCr.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TiffJpegColor {
    Rgb,
    YCbCr,
}

pub fn tiff_jpeg_color(
    probe: &JpegProbe,
    tiff_photometric: u64,
) -> TiffJpegColor {
    if probe.jfif {
        return TiffJpegColor::YCbCr;
    }
    if let Some(t) = probe.adobe_transform {
        return if t == 0 { TiffJpegColor::Rgb } else { TiffJpegColor::YCbCr };
    }
    if tiff_photometric == 2 {
        TiffJpegColor::Rgb
    } else {
        TiffJpegColor::YCbCr
    }
}

/// Pillow `im.quantization` equivalent: all DQT tables in table-id order,
/// zigzag order, validated to 1..=255 (like the oracle's `_extract_qtables`).
/// Returns `None` when absent or malformed.
pub fn qtables_pillow_style(data: &[u8]) -> Option<Vec<[u16; 64]>> {
    let tabs = decoder::extract_qtables(data)?;
    for t in &tabs {
        if t.iter().any(|&v| !(1..=255).contains(&v)) {
            return None;
        }
    }
    Some(tabs)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn luma_pattern(w: u32, h: u32, seed: u8) -> Vec<u8> {
        (0..w * h)
            .map(|i| ((i as u64 * 1103515245 + seed as u64 * 12345) >> 16) as u8)
            .collect()
    }

    #[test]
    fn gray_roundtrip_shapes() {
        for (w, h) in [(1usize, 1), (8, 8), (23, 37), (256, 256)] {
            let img = luma_pattern(w as u32, h as u32, 3);
            let jpg =
                encode_gray(&img, w as u32, h as u32, &tables::std_luma_quality(90))
                    .unwrap();
            let dec = decode(&jpg, 1 << 20).unwrap();
            assert_eq!((dec.width, dec.height), (w as u32, h as u32));
            assert_eq!(dec.kind, ColorKind::Gray);
            assert_eq!(dec.data.len(), w * h);
        }
    }

    #[test]
    fn rgb_roundtrip_and_probe() {
        let (w, h) = (37u32, 23u32);
        let mut img = Vec::new();
        for y in 0..h {
            for x in 0..w {
                img.push((x * 7) as u8);
                img.push((y * 11) as u8);
                img.push(((x + y) * 5) as u8);
            }
        }
        for s in [Sampling::S444, Sampling::S422, Sampling::S420] {
            let cfg = EncoderCfg::with_quality(95, s);
            let jpg = encode_rgb(&img, w, h, &cfg).unwrap();
            let dec = decode(&jpg, 1 << 20).unwrap();
            assert_eq!((dec.width, dec.height), (w, h));
            assert_eq!(dec.kind, ColorKind::Rgb);
            assert_eq!(dec.data.len(), (w * h * 3) as usize);
            let probe = scan_jpeg(&jpg).unwrap();
            assert_eq!((probe.width, probe.height), (w, h));
            let want = match s {
                Sampling::S444 => (1, 1, 1, 1, 1, 1),
                Sampling::S422 => (2, 1, 1, 1, 1, 1),
                Sampling::S420 => (2, 2, 1, 1, 1, 1),
            };
            assert_eq!(probe.sampling, Some(want));
            let q = qtables_pillow_style(&jpg).unwrap();
            assert_eq!(q.len(), 2);
        }
    }

    #[test]
    fn marker_layout_matches_pillow_probe() {
        // SOI, APP0(JFIF 1.01 1x1), DQT×2, SOF0, DHT(DC0 AC0 DC1 AC1), SOS
        let mut img = vec![128u8; 16 * 16 * 3];
        img[3] = 200;
        let cfg = EncoderCfg::with_quality(95, Sampling::S422);
        let jpg = encode_rgb(&img, 16, 16, &cfg).unwrap();
        assert_eq!(&jpg[0..2], &[0xFF, 0xD8]);
        assert_eq!(
            &jpg[2..20],
            &[0xFF, 0xE0, 0, 16, b'J', b'F', b'I', b'F', 0, 1, 1, 0, 0, 1, 0, 1, 0, 0]
        );
        let i = 20;
        assert_eq!(&jpg[i..i + 4], &[0xFF, 0xDB, 0, 67]);
        assert_eq!(jpg[i + 4], 0);
        assert_eq!(&jpg[i + 69..i + 73], &[0xFF, 0xDB, 0, 67]);
        assert_eq!(jpg[i + 73], 1);
        let j = i + 138;
        assert_eq!(&jpg[j..j + 2], &[0xFF, 0xC0]);
        assert_eq!(jpg[j + 2], 0);
        assert_eq!(jpg[j + 3], 17); // SOF len for 3 comps
        assert_eq!(jpg[j + 4], 8);
        assert_eq!(&jpg[j + 5..j + 7], &[0, 16]); // height
        assert_eq!(&jpg[j + 7..j + 9], &[0, 16]); // width
        assert_eq!(jpg[j + 9], 3);
        assert_eq!(jpg[j + 10], 1); // comp id
        assert_eq!(jpg[j + 11], 0x21); // 4:2:2 sampling byte (2x1)
        // DHT sizes in emission order: DC0=31, AC0=181, DC1=31, AC1=181
        let k = j + 2 + 17;
        let expect: [(u8, u16); 4] = [(0x00, 31), (0x10, 181), (0x01, 31), (0x11, 181)];
        let mut p = k;
        for (id, len) in expect {
            assert_eq!(&jpg[p..p + 2], &[0xFF, 0xC4]);
            assert_eq!(u16::from_be_bytes([jpg[p + 2], jpg[p + 3]]), len);
            assert_eq!(jpg[p + 4], id);
            p += 2 + len as usize;
        }
        assert_eq!(&jpg[p..p + 2], &[0xFF, 0xDA]);
        assert_eq!(jpg[p + 3], 12); // SOS len for 3 comps
        assert_eq!(&jpg[jpg.len() - 2..], &[0xFF, 0xD9]);
    }

    #[test]
    fn rejects_progressive_and_truncated() {
        let jpg = vec![
            0xFF, 0xD8, 0xFF, 0xC2, 0, 11, 8, 0, 8, 0, 8, 3, 1, 0x22, 0, 2, 0x11,
            1, 3, 0x11, 1, 0xFF, 0xD9,
        ];
        assert!(decode(&jpg, 1 << 20).is_err());
        assert!(decode(&[0xFF, 0xD8, 0xFF, 0xD9], 1 << 20).is_err());
        assert!(scan_jpeg(&[0x00, 0x01, 0x02, 0x03]).is_err());
    }

    #[test]
    fn constant_block_entropy_matches_pillow() {
        // All-192 8×8 gray at quality 100 (quant=1): Pillow emits
        // SOI..SOS then FE 80 2B FF FF..? (verified empirically):
        // DC cat 10 (code 11111110), extra bits 1000000000, EOB 1010,
        // pad 11 → FE 80 2B, EOI.
        let img = vec![192u8; 64];
        let jpg = encode_gray(&img, 8, 8, &tables::std_luma_quality(100)).unwrap();
        // file ends: ..SOS(01 01 00 00 3F 00) + entropy + EOI
        assert_eq!(&jpg[jpg.len() - 5..], &[0xFE, 0x80, 0x2B, 0xFF, 0xD9][..],
            "tail = {:02x?}", &jpg[jpg.len() - 12..]);
    }

    #[test]
    fn black_canvas_byte_stability() {
        let a = encode_gray(&vec![0u8; 100 * 40], 100, 40, &tables::std_luma_quality(90))
            .unwrap();
        let b = encode_gray(&vec![0u8; 100 * 40], 100, 40, &tables::std_luma_quality(90))
            .unwrap();
        assert_eq!(a, b);
    }
}
