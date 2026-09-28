//! wasm32 build proof for the core spike (browser runner is C0 part ③).
//! Exposes a minimal probe so the bindgen glue is real; conversion in the
//! browser will use OPFS-backed ByteSource/Sink adapters, not these entry
//! points.

use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn core_version() -> String {
    format!(
        "slide-transform-core-spike {} (edge-reencode={})",
        env!("CARGO_PKG_VERSION"),
        cfg!(feature = "edge-reencode")
    )
}

/// Probe a KFB header/level geometry from a byte prefix (≥ 96 B header).
/// Returns a compact summary or a stable error code string.
#[wasm_bindgen]
pub fn kfb_probe_prefix(prefix: &[u8]) -> String {
    let src = slide_transform_core_spike::io::MemSource::new(prefix.to_vec());
    let mut scratch = slide_transform_core_spike::io::MemScratch::default();
    match slide_transform_core_spike::kfb::parse_kfb(&src, &mut scratch) {
        Ok(doc) => {
            let mut out = format!(
                "version={} {}x{} levels={} tiles={}",
                doc.header.version,
                doc.header.width_px,
                doc.header.height_px,
                doc.header.level_count,
                doc.header.tile_count
            );
            for lv in &doc.levels {
                out.push_str(&format!(
                    "|l{} {}x{}",
                    lv.level,
                    lv.width,
                    lv.height
                ));
            }
            out
        }
        Err(e) => format!("error[{}]", e.code.stable_code()),
    }
}

/// Memory-backed full-pipeline smoke entry: converts a complete (small)
/// KFB held in memory and returns the BigTIFF bytes, or throws on error.
///
/// NOT the browser IO path for real slides — the C0 ③ runner will stream
/// through File.slice/OPFS adapters instead of materializing files. This
/// entry exists so the wasm artifact links the whole convert pipeline
/// (JPEG codecs included) and its size can be measured honestly.
#[wasm_bindgen]
pub fn convert_mem(kfb: &[u8]) -> Result<Vec<u8>, JsError> {
    let src = slide_transform_core_spike::io::MemSource::new(kfb.to_vec());
    let mut sink = slide_transform_core_spike::io::MemSink::new();
    let mut scratch = slide_transform_core_spike::io::MemScratch::default();
    slide_transform_core_spike::convert::convert_kfb(
        &src,
        &mut sink,
        &mut scratch,
        &slide_transform_core_spike::convert::ConvertOptions::default(),
    )
    .map_err(|e| JsError::new(&format!("{}: {}", e.code.stable_code(), e.message)))?;
    Ok(sink.data)
}
