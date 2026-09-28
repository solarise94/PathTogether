//! wasm32 bindings over the transform core: `probe` and `convert` driven by
//! JS-provided IO callbacks with bounded chunks (≤1 MiB per host call).
//!
//! The host supplies global functions:
//!   stHostSourceSize(), stHostRead(offset, len), stHostWrite(offset, bytes),
//!   stHostTruncate(len), stHostFlush(), stHostScratchOpen/Read/Write/
//!   Truncate/Flush(name, ...), stHostProgress(json), stHostCancelled()
//! (the browser runner in `static/tools/slide-transform/` implements them
//! over File.slice / OPFS sync access handles).

use slide_transform_core::error::{CoreError, CoreResult};
use slide_transform_core::io::{ByteSource, RandomAccessSink, ScratchFactory, ScratchSink};
use slide_transform_core::job::{JobControl, Progress, ProgressCallback};
use slide_transform_core::plan::{InputIdentity, TransformPlan};
use wasm_bindgen::prelude::*;

/// Upper bound on a single host read/write chunk (1 MiB).
pub const MAX_CHUNK: usize = 1 << 20;

#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(js_name = "stHostSourceSize")]
    fn host_source_size() -> f64;
    #[wasm_bindgen(js_name = "stHostRead")]
    fn host_read(offset: f64, len: u32) -> Vec<u8>;
    #[wasm_bindgen(js_name = "stHostWrite")]
    fn host_write(offset: f64, data: &[u8]);
    #[wasm_bindgen(js_name = "stHostTruncate")]
    fn host_truncate(len: f64);
    #[wasm_bindgen(js_name = "stHostFlush")]
    fn host_flush();
    #[wasm_bindgen(js_name = "stHostScratchOpen")]
    fn host_scratch_open(name: &str);
    #[wasm_bindgen(js_name = "stHostScratchRead")]
    fn host_scratch_read(name: &str, offset: f64, len: u32) -> Vec<u8>;
    #[wasm_bindgen(js_name = "stHostScratchWrite")]
    fn host_scratch_write(name: &str, offset: f64, data: &[u8]);
    #[wasm_bindgen(js_name = "stHostScratchTruncate")]
    fn host_scratch_truncate(name: &str, len: f64);
    #[wasm_bindgen(js_name = "stHostScratchFlush")]
    fn host_scratch_flush(name: &str);
    #[wasm_bindgen(js_name = "stHostProgress")]
    fn host_progress(json: &str);
    #[wasm_bindgen(js_name = "stHostCancelled")]
    fn host_cancelled() -> bool;
}

struct HostSource {
    size: u64,
}

impl HostSource {
    fn open() -> HostSource {
        HostSource { size: host_source_size() as u64 }
    }
}

impl ByteSource for HostSource {
    fn size(&self) -> u64 {
        self.size
    }
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let mut out = Vec::with_capacity(len);
        let mut done = 0usize;
        while done < len {
            let want = MAX_CHUNK.min(len - done) as u32;
            let chunk = host_read((offset + done as u64) as f64, want);
            if chunk.len() != want as usize {
                return Err(CoreError::oob("宿主 read 返回长度不足"));
            }
            out.extend_from_slice(&chunk);
            done += want as usize;
        }
        Ok(out)
    }
}

struct HostSink;

impl RandomAccessSink for HostSink {
    fn write_at(&mut self, offset: u64, data: &[u8]) -> CoreResult<()> {
        let mut done = 0usize;
        while done < data.len() {
            let end = (done + MAX_CHUNK).min(data.len());
            host_write((offset + done as u64) as f64, &data[done..end]);
            done = end;
        }
        Ok(())
    }
    fn truncate(&mut self, size: u64) -> CoreResult<()> {
        host_truncate(size as f64);
        Ok(())
    }
    fn flush(&mut self) -> CoreResult<()> {
        host_flush();
        Ok(())
    }
}

struct HostScratchFactory;

impl ScratchFactory for HostScratchFactory {
    fn create(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>> {
        host_scratch_open(name);
        Ok(Box::new(HostScratchSink { name: name.to_string() }))
    }
}

struct HostScratchSink {
    name: String,
}

impl RandomAccessSink for HostScratchSink {
    fn write_at(&mut self, offset: u64, data: &[u8]) -> CoreResult<()> {
        host_scratch_write(&self.name, offset as f64, data);
        Ok(())
    }
    fn truncate(&mut self, size: u64) -> CoreResult<()> {
        host_scratch_truncate(&self.name, size as f64);
        Ok(())
    }
    fn flush(&mut self) -> CoreResult<()> {
        host_scratch_flush(&self.name);
        Ok(())
    }
}

impl slide_transform_core::io::ReadBack for HostScratchSink {
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let mut out = Vec::with_capacity(len);
        let mut done = 0usize;
        while done < len {
            let want = MAX_CHUNK.min(len - done) as u32;
            let chunk =
                host_scratch_read(&self.name, (offset + done as u64) as f64, want);
            if chunk.len() != want as usize {
                return Err(CoreError::io("宿主 scratch read 返回长度不足"));
            }
            out.extend_from_slice(&chunk);
            done += want as usize;
        }
        Ok(out)
    }
}

struct HostProgress;

impl ProgressCallback for HostProgress {
    fn on_progress(&self, p: &Progress) {
        let json = format!(
            "{{\"unit\":\"{}\",\"level\":{},\"channel\":{},\"done\":{},\"total\":{},\"committed_bytes\":{}}}",
            match p.unit {
                slide_transform_core::job::ProgressUnit::Level => "level",
                slide_transform_core::job::ProgressUnit::TileRow => "tile_row",
            },
            p.level,
            p.channel.map(|c| c.to_string()).unwrap_or_else(|| "null".into()),
            p.done,
            p.total,
            p.committed_bytes
        );
        host_progress(&json);
    }
}

fn err_json(e: &CoreError) -> String {
    let msg = e
        .message
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', " ");
    format!(
        "{{\"error\":{{\"code\":\"{}\",\"message\":\"{}\"}}}}",
        e.code.stable_code(),
        msg
    )
}

fn detect(src: &dyn ByteSource) -> CoreResult<[u8; 8]> {
    let head = src.read_at(0, 8)?;
    let mut m = [0u8; 8];
    m.copy_from_slice(&head);
    Ok(m)
}

/// Probe the input through host reads; returns a JSON string.
#[wasm_bindgen(js_name = "probe")]
pub fn probe() -> String {
    let src = HostSource::open();
    let mut scratch = HostScratchFactory;
    let magic = match detect(&src) {
        Ok(m) => m,
        Err(e) => return err_json(&e),
    };
    let res = if magic == slide_transform_core::kfbf::KFBF_MAGIC {
        slide_transform_core::kfbf::parse_kfbf(&src, &mut scratch).map(|doc| {
            let channels: Vec<String> = doc
                .channels
                .iter()
                .map(|c| {
                    format!(
                        "{{\"index\":{},\"name\":\"{}\",\"exposure\":{},\"exposure_unit\":\"ms(assumed)\"}}",
                        c.index, c.name, c.exposure
                    )
                })
                .collect();
            let levels: Vec<String> = doc
                .levels
                .iter()
                .map(|lv| {
                    format!(
                        "{{\"level\":{},\"width\":{},\"height\":{}}}",
                        lv.level, lv.width, lv.height
                    )
                })
                .collect();
            format!(
                "{{\"format\":\"kfbf_kfbio_jpeg\",\"modality\":\"fluorescence\",\"width\":{},\"height\":{},\"mpp\":{},\"channels\":[{}],\"levels\":[{}]}}",
                doc.header.width_px,
                doc.header.height_px,
                doc.header.mpp,
                channels.join(","),
                levels.join(",")
            )
        })
    } else {
        slide_transform_core::kfb::parse_kfb(&src, &mut scratch).map(|doc| {
            let levels: Vec<String> = doc
                .levels
                .iter()
                .map(|lv| {
                    format!(
                        "{{\"level\":{},\"width\":{},\"height\":{}}}",
                        lv.level, lv.width, lv.height
                    )
                })
                .collect();
            format!(
                "{{\"format\":\"{}\",\"modality\":\"brightfield\",\"width\":{},\"height\":{},\"mpp_x\":{},\"mpp_y\":{},\"levels\":[{}]}}",
                if doc.header.version != 1 { "kfb_kfbio_jpeg" } else { "kfb_bf_v1" },
                doc.header.width_px,
                doc.header.height_px,
                doc.header.mpp_x,
                doc.header.mpp_y,
                levels.join(",")
            )
        })
    };
    match res {
        Ok(doc_json) => format!(
            "{{\"core_version\":\"{}\",\"size\":{},\"document\":{}}}",
            slide_transform_core::CORE_VERSION,
            src.size(),
            doc_json
        ),
        Err(e) => err_json(&e),
    }
}

/// Run a conversion writing to the host sink. `strict_lossless` toggles the
/// pixel policy; `channel_json` may be empty (no companion).
#[wasm_bindgen(js_name = "convert")]
pub fn convert(strict_lossless: bool, channel_json: &str) -> String {
    let src = HostSource::open();
    let magic = match detect(&src) {
        Ok(m) => m,
        Err(e) => return err_json(&e),
    };
    let identity = InputIdentity {
        name: "browser-input".to_string(),
        size: src.size(),
        sha256: None,
    };
    let policy = if strict_lossless {
        slide_transform_core::plan::PixelPolicy::StrictLossless
    } else {
        slide_transform_core::plan::PixelPolicy::AllowEdgeReencode
    };
    let mut companion_json_warning = String::new();
    let companion = if channel_json.trim().is_empty() {
        None
    } else {
        match slide_transform_core::companion::parse_channel_json(channel_json.as_bytes()) {
            Ok(c) => Some(c),
            Err(e) => {
                companion_json_warning =
                    format!("\"companion_warning\":\"{}\",", e.message.replace('"', "'"));
                None
            }
        }
    };
    let mut sink = HostSink;
    let mut scratch = HostScratchFactory;
    let progress = HostProgress;
    let job = JobControl::new(&progress);
    let result = if magic == slide_transform_core::kfbf::KFBF_MAGIC {
        let plan = TransformPlan::fluorescence(identity).with_policy(policy);
        slide_transform_core::convert_fl::convert_kfbf_to_ome(
            &src,
            &mut sink,
            &mut scratch,
            &plan,
            &job,
            companion.as_ref(),
        )
    } else {
        let plan = TransformPlan::brightfield(identity).with_policy(policy);
        slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
            &src,
            &mut sink,
            &mut scratch,
            &plan,
            &job,
        )
    };
    match result {
        Ok(r) => {
            let warnings: Vec<String> =
                r.warnings.iter().map(|w| format!("\"{w}\"")).collect();
            format!(
                "{{{}\"format\":\"{}\",\"output_bytes\":{},\"width\":{},\"height\":{},\"tiles_raw_copied\":{},\"tiles_reencoded\":{},\"warnings\":[{}]}}",
                companion_json_warning,
                r.format,
                r.output_bytes,
                r.width,
                r.height,
                r.count_raw_copied(),
                r.count_reencoded(),
                warnings.join(",")
            )
        }
        Err(e) => err_json(&e),
    }
}
