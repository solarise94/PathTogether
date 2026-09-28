//! Edge-tile re-encode: decode → paste on a white 256×256 canvas → re-encode
//! reusing the source JPEG's quantization tables and the level's chroma
//! subsampling (the policy of `kfb/converter.py::_reencode_edge_tile`).
//!
//! Decode: `zune-jpeg` (pure Rust, wasm32-compatible). Encode:
//! `jpeg-encoder` with `QuantizationTableType::Custom` (natural-order u16
//! tables; values are written verbatim — quality scaling does not apply to
//! custom tables in jpeg-encoder 0.6). The oracle's Pillow/libjpeg output is
//! NOT byte-reproducible with a different encoder (different Huffman coding
//! and FDCT), so parity is asserted on decoded pixels, not bytes; measured
//! differences go into the C0 ADR. When qtables cannot be parsed, the
//! fallback re-encodes with the encoder's default tables at quality 95 and
//! reports `edge_reencode_fallback_q95` (same warning code as the oracle,
//! though the fallback tables differ slightly — documented divergence).

use crate::error::{CoreError, CoreResult};
use crate::jpeg::{parse_dqt, scan_jpeg};

pub const WARN_EDGE_REENCODE_FALLBACK_Q95: &str = "edge_reencode_fallback_q95";

/// TIFF/Pillow subsampling (h1, v1) of the luma component.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Subsampling(pub u16, pub u16);

#[cfg(feature = "edge-reencode")]
fn sampling_factor(sub: Subsampling) -> jpeg_encoder::SamplingFactor {
    match sub {
        Subsampling(1, 1) => jpeg_encoder::SamplingFactor::F_1_1,
        Subsampling(2, 1) => jpeg_encoder::SamplingFactor::F_2_1,
        Subsampling(2, 2) => jpeg_encoder::SamplingFactor::F_2_2,
        Subsampling(h, v) => unreachable!("unsupported sampling {h}x{v} (checked earlier)"),
    }
}

/// Returns `(jpeg_bytes, reused_source_qtables)`.
pub fn reencode_edge_tile(
    payload: &[u8],
    jpeg_w: u16,
    jpeg_h: u16,
    sub: Subsampling,
) -> CoreResult<(Vec<u8>, bool)> {
    #[cfg(not(feature = "edge-reencode"))]
    {
        let _ = (payload, jpeg_w, jpeg_h, sub);
        return Err(CoreError::validation(
            "边缘 tile 需要重编码，但本构建未启用 edge-reencode",
        ));
    }
    #[cfg(feature = "edge-reencode")]
    {
        use zune_jpeg::JpegDecoder;

        let mut dec = JpegDecoder::new(payload);
        dec.decode_headers()
            .map_err(|e| CoreError::jpeg(format!("边缘 tile 解码失败: {e:?}")))?;
        let info = dec
            .info()
            .ok_or_else(|| CoreError::jpeg("边缘 tile 解码失败: 无头信息"))?;
        if info.width != jpeg_w || info.height != jpeg_h {
            return Err(CoreError::jpeg(format!(
                "tile 解码尺寸 {}×{} 与索引 {jpeg_w}×{jpeg_h} 不符",
                info.width, info.height
            )));
        }
        let pixels = dec
            .decode()
            .map_err(|e| CoreError::jpeg(format!("边缘 tile 解码失败: {e:?}")))?;

        // 白底 256×256 左上贴图
        let mut canvas = vec![255u8; 256 * 256 * 3];
        let stride = jpeg_w as usize * 3;
        for row in 0..jpeg_h as usize {
            let src = row * stride;
            let dst = row * 256 * 3;
            canvas[dst..dst + stride].copy_from_slice(&pixels[src..src + stride]);
        }

        // 复用源量化表（按 SOF 分量 → DQT id 映射）
        let probe = scan_jpeg(payload)?;
        let dqt = parse_dqt(payload);
        let (luma_t, chroma_t) = if probe.components.len() >= 2 {
            (dqt.get(probe.components[0].3).cloned(), dqt.get(probe.components[1].3).cloned())
        } else {
            (None, None)
        };
        let reused = luma_t.is_some() && chroma_t.is_some();

        let mut out: Vec<u8> = Vec::new();
        let mut enc = jpeg_encoder::Encoder::new(&mut out, 95);
        enc.set_sampling_factor(sampling_factor(sub));
        match (luma_t, chroma_t) {
            (Some(l), Some(c)) => {
                enc.set_quantization_tables(
                    jpeg_encoder::QuantizationTableType::Custom(Box::new(l)),
                    jpeg_encoder::QuantizationTableType::Custom(Box::new(c)),
                );
            }
            _ => {} // fallback：默认表 + q95（调用方记 warning）
        }
        enc.encode(&canvas, 256, 256, jpeg_encoder::ColorType::Rgb)
            .map_err(|e| CoreError::jpeg(format!("边缘 tile 重编码失败: {e:?}")))?;
        Ok((out, reused))
    }
}
