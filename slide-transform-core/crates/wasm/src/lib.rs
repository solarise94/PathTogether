//! wasm32 bindings over the transform core: `probe`, `convert`, and the C2
//! additions `convertResume`, `finalizeValidate`, `sha256Source`,
//! `enableSourceHash`/`sourceSha256`, `coreVersion`, and the output-profile
//! entry points `convertProfile`/`convertResumeProfile` — driven by
//! JS-provided IO callbacks with bounded chunks (≤1 MiB per host call).
//!
//! `convert`/`convertResume` keep their original meaning (KFB → classic,
//! KFBF → fluorescence OME) for hosts that predate output profiles.
//!
//! Host contract (globals; the browser runner in
//! `static/tools/slide-transform/worker.js` implements them over
//! FileReaderSync / OPFS sync access handles):
//!
//!   stHostSourceSize() -> number
//!   stHostReadInto(offset, len, ptr) -> null/undefined ok | string error
//!       (preferred: fills wasm linear memory directly, zero buffer crossing;
//!        enabled via configure(1); ptr is a u32 offset into wasm memory)
//!   stHostRead(offset, len) -> Uint8Array          (legacy fallback, C1)
//!   stHostWrite(offset, bytes) -> null/undefined ok | string error
//!   stHostTruncate(len) / stHostFlush() -> null/undefined ok | string error
//!   stHostScratchOpen(name, preserve?)             (preserve=true keeps
//!                                                   committed bytes: resume)
//!   stHostScratchRead(name, offset, len) -> Uint8Array
//!   stHostScratchWrite/Truncate/Flush(name, ...) -> null/undefined ok | error
//!   stHostOutSize() -> number ; stHostOutReadInto(offset, len, ptr) -> err?
//!       (output read-back for finalizeValidate)
//!   stHostProgress(json) ; stHostCancelled() -> bool
//!   stHostCheckpoint(json)   (committed-state tap; enable via
//!                             enableCheckpoint())
//!
//! Error convention: a callback returning `null`/`undefined` (or nothing,
//! which keeps C1 hosts source-compatible) means success; returning a truthy
//! value is an error whose message is the value stringified. JS exceptions
//! are caught (`catch`) and mapped to typed `io_error`s — a quota failure
//! mid-write must be a recoverable error, never a wasm abort.

use slide_transform_core::error::{CoreError, CoreResult};
use slide_transform_core::io::{ByteSource, RandomAccessSink, ScratchFactory, ScratchSink};
use slide_transform_core::job::{JobControl, Progress, ProgressCallback};
use slide_transform_core::plan::{InputIdentity, OutputProfile, TransformPlan};
use slide_transform_core::resume::{parse_resume_json, ResumePoint};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use wasm_bindgen::prelude::*;

/// Upper bound on a single host read/write chunk (1 MiB).
pub const MAX_CHUNK: usize = 1 << 20;

static READ_INTO: AtomicBool = AtomicBool::new(false);
static CHECKPOINT_ENABLED: AtomicBool = AtomicBool::new(false);
static SOURCE_HASH_ENABLED: AtomicBool = AtomicBool::new(false);

fn hash_state() -> &'static Mutex<Option<sha2::Sha256>> {
    static STATE: Mutex<Option<sha2::Sha256>> = Mutex::new(None);
    &STATE
}

fn js_err(v: JsValue) -> Option<CoreError> {
    let undef = v.is_undefined();
    let null = v.is_null();
    if undef || null {
        return None;
    }
    if let Some(s) = v.as_string() {
        return Some(CoreError::io(s));
    }
    Some(CoreError::io(format!("宿主回调错误: {v:?}")))
}

#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(js_name = "stHostSourceSize")]
    fn host_source_size() -> f64;
    #[wasm_bindgen(js_name = "stHostReadInto", catch)]
    fn host_read_into(offset: f64, len: u32, ptr: u32) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostRead")]
    fn host_read(offset: f64, len: u32) -> Vec<u8>;
    #[wasm_bindgen(js_name = "stHostWrite", catch)]
    fn host_write(offset: f64, data: &[u8]) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostTruncate", catch)]
    fn host_truncate(len: f64) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostFlush", catch)]
    fn host_flush() -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostScratchOpen", catch)]
    fn host_scratch_open(name: &str, preserve: bool) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostScratchRead")]
    fn host_scratch_read(name: &str, offset: f64, len: u32) -> Vec<u8>;
    #[wasm_bindgen(js_name = "stHostScratchWrite", catch)]
    fn host_scratch_write(name: &str, offset: f64, data: &[u8]) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostScratchTruncate", catch)]
    fn host_scratch_truncate(name: &str, len: f64) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostScratchFlush", catch)]
    fn host_scratch_flush(name: &str) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostOutSize")]
    fn host_out_size() -> f64;
    #[wasm_bindgen(js_name = "stHostOutReadInto", catch)]
    fn host_out_read_into(offset: f64, len: u32, ptr: u32) -> Result<JsValue, JsValue>;
    #[wasm_bindgen(js_name = "stHostProgress")]
    fn host_progress(json: &str);
    #[wasm_bindgen(js_name = "stHostCancelled")]
    fn host_cancelled() -> bool;
    #[wasm_bindgen(js_name = "stHostCheckpoint")]
    fn host_checkpoint(json: &str);
}

/// Host capability flags (call before convert): bit 0 = stHostReadInto.
#[wasm_bindgen(js_name = "configure")]
pub fn configure(read_into: bool) {
    READ_INTO.store(read_into, Ordering::SeqCst);
}

#[wasm_bindgen(js_name = "enableCheckpoint")]
pub fn enable_checkpoint() {
    CHECKPOINT_ENABLED.store(true, Ordering::SeqCst);
}

/// Piggyback a sha256 over every source byte read through `ByteSource`
/// during the next conversion (identity capture without an extra pass).
#[wasm_bindgen(js_name = "enableSourceHash")]
pub fn enable_source_hash() {
    use sha2::Digest;
    let mut st = hash_state().lock().unwrap();
    *st = Some(sha2::Sha256::new());
    SOURCE_HASH_ENABLED.store(true, Ordering::SeqCst);
}

#[wasm_bindgen(js_name = "sourceSha256")]
pub fn source_sha256() -> String {
    use sha2::Digest;
    let mut st = hash_state().lock().unwrap();
    match st.take() {
        Some(h) => format!("{:x}", h.finalize()),
        None => String::new(),
    }
}

#[wasm_bindgen(js_name = "coreVersion")]
pub fn core_version() -> String {
    slide_transform_core::CORE_VERSION.to_string()
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
        let mut out = vec![0u8; len];
        let mut done = 0usize;
        while done < len {
            let want = MAX_CHUNK.min(len - done) as u32;
            if READ_INTO.load(Ordering::Relaxed) {
                let ptr = out.as_mut_ptr() as usize + done;
                let r = host_read_into((offset + done as u64) as f64, want, ptr as u32)
                    .map_err(|e| CoreError::io(format!("宿主 read 异常: {e:?}")))?;
                if let Some(err) = js_err(r) {
                    return Err(err);
                }
            } else {
                let chunk = host_read((offset + done as u64) as f64, want);
                if chunk.len() != want as usize {
                    return Err(CoreError::oob("宿主 read 返回长度不足"));
                }
                out[done..done + want as usize].copy_from_slice(&chunk);
            }
            done += want as usize;
        }
        if SOURCE_HASH_ENABLED.load(Ordering::Relaxed) {
            use sha2::Digest;
            if let Some(h) = hash_state().lock().unwrap().as_mut() {
                h.update(&out);
            }
        }
        Ok(out)
    }
}

/// Read-back adapter over the output sink (validation phase).
struct OutReader {
    size: u64,
}

impl ByteSource for OutReader {
    fn size(&self) -> u64 {
        self.size
    }
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let mut out = vec![0u8; len];
        let mut done = 0usize;
        while done < len {
            let want = MAX_CHUNK.min(len - done) as u32;
            let ptr = out.as_mut_ptr() as usize + done;
            let r = host_out_read_into((offset + done as u64) as f64, want, ptr as u32)
                .map_err(|e| CoreError::io(format!("宿主 out-read 异常: {e:?}")))?;
            if let Some(err) = js_err(r) {
                return Err(err);
            }
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
            let r = host_write((offset + done as u64) as f64, &data[done..end])
                .map_err(|e| CoreError::io(format!("宿主 write 异常: {e:?}")))?;
            if let Some(err) = js_err(r) {
                return Err(err);
            }
            done = end;
        }
        Ok(())
    }
    fn truncate(&mut self, size: u64) -> CoreResult<()> {
        let r = host_truncate(size as f64)
            .map_err(|e| CoreError::io(format!("宿主 truncate 异常: {e:?}")))?;
        if let Some(err) = js_err(r) {
            return Err(err);
        }
        Ok(())
    }
    fn flush(&mut self) -> CoreResult<()> {
        let r = host_flush()
            .map_err(|e| CoreError::io(format!("宿主 flush 异常: {e:?}")))?;
        if let Some(err) = js_err(r) {
            return Err(err);
        }
        Ok(())
    }
}

struct HostScratchFactory;

impl ScratchFactory for HostScratchFactory {
    fn create(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>> {
        let r = host_scratch_open(name, false)
            .map_err(|e| CoreError::io(format!("宿主 scratch open 异常: {e:?}")))?;
        if let Some(err) = js_err(r) {
            return Err(err);
        }
        Ok(Box::new(HostScratchSink { name: name.to_string() }))
    }
    fn create_preserve(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>> {
        let r = host_scratch_open(name, true)
            .map_err(|e| CoreError::io(format!("宿主 scratch open(preserve) 异常: {e:?}")))?;
        if let Some(err) = js_err(r) {
            return Err(err);
        }
        Ok(Box::new(HostScratchSink { name: name.to_string() }))
    }
}

struct HostScratchSink {
    name: String,
}

impl RandomAccessSink for HostScratchSink {
    fn write_at(&mut self, offset: u64, data: &[u8]) -> CoreResult<()> {
        let mut done = 0usize;
        while done < data.len() {
            let end = (done + MAX_CHUNK).min(data.len());
            let r =
                host_scratch_write(&self.name, (offset + done as u64) as f64, &data[done..end])
                    .map_err(|e| CoreError::io(format!("宿主 scratch write 异常: {e:?}")))?;
            if let Some(err) = js_err(r) {
                return Err(err);
            }
            done = end;
        }
        Ok(())
    }
    fn truncate(&mut self, size: u64) -> CoreResult<()> {
        let r = host_scratch_truncate(&self.name, size as f64)
            .map_err(|e| CoreError::io(format!("宿主 scratch truncate 异常: {e:?}")))?;
        if let Some(err) = js_err(r) {
            return Err(err);
        }
        Ok(())
    }
    fn flush(&mut self) -> CoreResult<()> {
        let r = host_scratch_flush(&self.name)
            .map_err(|e| CoreError::io(format!("宿主 scratch flush 异常: {e:?}")))?;
        if let Some(err) = js_err(r) {
            return Err(err);
        }
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

/// Emits committed states; the output profile and the encoding profile
/// travel with every state so a journal can never be resumed under a
/// different layout or a different quality mode.
struct HostCheckpoint {
    profile: OutputProfile,
    encoding: slide_transform_core::plan::EncodingProfile,
}

impl slide_transform_core::job::CheckpointCallback for HostCheckpoint {
    fn on_checkpoint(&self, c: &slide_transform_core::job::CheckpointState) {
        if !CHECKPOINT_ENABLED.load(Ordering::Relaxed) {
            return;
        }
        let ifds: Vec<String> = c.ifd_tiles.iter().map(|t| t.to_string()).collect();
        let json = format!(
            "{{\"level\":{},\"channel\":{},\"cell\":{},\"out\":{},\"ifds\":[{}],\"profile\":\"{}\",\"encoding\":\"{}\"}}",
            c.level,
            c.channel.map(|v| v.to_string()).unwrap_or_else(|| "0".into()),
            c.cell_done,
            c.committed_output,
            ifds.join(","),
            self.profile.id(),
            self.encoding.id()
        );
        host_checkpoint(&json);
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

fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn json_num(v: f64) -> String {
    if v.is_finite() {
        format!("{v}")
    } else {
        "null".to_string()
    }
}

fn channels_report_json(channels: &[slide_transform_core::report::ChannelSummary]) -> String {
    let items: Vec<String> = channels
        .iter()
        .map(|c| {
            let dw = c
                .display_window
                .map(|(lo, hi)| format!("[{},{}]", json_num(lo), json_num(hi)))
                .unwrap_or_else(|| "null".into());
            let dws = c
                .display_window_source
                .as_deref()
                .map(json_str)
                .unwrap_or_else(|| "null".into());
            format!(
                "{{\"index\":{},\"name\":{},\"color_rgb\":[{},{},{}],\"exposure\":{},\"exposure_unit\":\"ms(assumed)\",\"gamma\":{},\"display_window\":{},\"display_window_source\":{}}}",
                c.index,
                json_str(&c.name),
                c.color_rgb.0,
                c.color_rgb.1,
                c.color_rgb.2,
                json_num(c.exposure),
                json_num(c.gamma),
                dw,
                dws
            )
        })
        .collect();
    format!("[{}]", items.join(","))
}

fn detect(src: &dyn ByteSource) -> CoreResult<[u8; 8]> {
    let head = src.read_at(0, 8)?;
    let mut m = [0u8; 8];
    m.copy_from_slice(&head);
    Ok(m)
}

/// Probe the input through host reads; returns a JSON string. Includes the
/// C2 disk-precheck estimate (`estimate.output_upper_bound_bytes` etc.).
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
            let est = slide_transform_core::estimate::estimate_fl(&doc, src.size());
            let channels: Vec<String> = doc
                .channels
                .iter()
                .map(|c| {
                    format!(
                        "{{\"index\":{},\"name\":{},\"exposure\":{},\"exposure_unit\":\"ms(assumed)\"}}",
                        c.index,
                        json_str(&c.name),
                        json_num(c.exposure)
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
                "{{\"format\":\"kfbf_kfbio_jpeg\",\"modality\":\"fluorescence\",\"width\":{},\"height\":{},\"mpp\":{},\"channels\":[{}],\"levels\":[{}],\"estimate\":{}}}",
                doc.header.width_px,
                doc.header.height_px,
                doc.header.mpp,
                channels.join(","),
                levels.join(","),
                estimate_json(&est)
            )
        })
    } else {
        slide_transform_core::kfb::parse_kfb(&src, &mut scratch).map(|doc| {
            let est = slide_transform_core::estimate::estimate_bf(&doc, src.size())
                .unwrap_or(DEFAULT_EST);
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
                "{{\"format\":\"{}\",\"modality\":\"brightfield\",\"width\":{},\"height\":{},\"mpp_x\":{},\"mpp_y\":{},\"levels\":[{}],\"estimate\":{}}}",
                if doc.header.version != 1 { "kfb_kfbio_jpeg" } else { "kfb_bf_v1" },
                doc.header.width_px,
                doc.header.height_px,
                doc.header.mpp_x,
                doc.header.mpp_y,
                levels.join(","),
                estimate_json(&est)
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

const DEFAULT_EST: slide_transform_core::estimate::OutputEstimate =
    slide_transform_core::estimate::OutputEstimate {
        payload_bytes: 0,
        tiles_present: 0,
        cells_total: 0,
        cells_missing: 0,
        edge_tiles: 0,
        ifds: 0,
        output_upper_bound_bytes: 0,
        compact_upper_bound_bytes: 0,
    };

fn estimate_json(e: &slide_transform_core::estimate::OutputEstimate) -> String {
    format!(
        "{{\"payload_bytes\":{},\"tiles_present\":{},\"cells_total\":{},\"cells_missing\":{},\"edge_tiles\":{},\"ifds\":{},\"output_upper_bound_bytes\":{},\"compact_upper_bound_bytes\":{}}}",
        e.payload_bytes,
        e.tiles_present,
        e.cells_total,
        e.cells_missing,
        e.edge_tiles,
        e.ifds,
        e.output_upper_bound_bytes,
        e.compact_upper_bound_bytes
    )
}

/// `"profile":"…"` of a checkpoint state; absent in states journalled
/// before output profiles existed.
fn resume_profile_field(resume_json: &str) -> Option<String> {
    resume_string_field(resume_json, "profile")
}

/// `"encoding":"…"` of a checkpoint state; absent in states journalled
/// before encoding profiles existed (U3) — those mean preserve.
fn resume_encoding_field(resume_json: &str) -> Option<String> {
    resume_string_field(resume_json, "encoding")
}

fn resume_string_field(resume_json: &str, key: &str) -> Option<String> {
    let needle = format!("\"{key}\"");
    let at = resume_json.find(&needle)? + needle.len();
    let rest = resume_json[at..].trim_start().strip_prefix(':')?.trim_start();
    let rest = rest.strip_prefix('"')?;
    Some(rest[..rest.find('"')?].to_string())
}

/// Resolve the requested profile against the input kind. `None` = the
/// pre-profile default for the input (classic / fluorescence OME).
fn resolve_profile(is_fl: bool, requested: Option<&str>) -> CoreResult<OutputProfile> {
    let p = match requested {
        None | Some("") => {
            if is_fl { OutputProfile::OmeBigTiffSubifd } else { OutputProfile::ClassicJpegBigTiff }
        }
        Some(id) => OutputProfile::from_id(id)
            .ok_or_else(|| CoreError::validation(format!("未知输出 profile {id}")))?,
    };
    if p.is_brightfield() == is_fl {
        return Err(CoreError::variant(format!(
            "输出 profile {} 与输入类型（{}）不符",
            p.id(),
            if is_fl { "荧光 KFBF" } else { "明场 KFB" }
        )));
    }
    Ok(p)
}

/// Resolve the requested encoding profile. `None`/empty and every legacy
/// state without the field mean preserve-source-v1 (pre-U3 semantics).
/// Compact is brightfield-only.
fn resolve_encoding(is_fl: bool, requested: Option<&str>) -> CoreResult<EncodingProfileW> {
    let e = match requested {
        None | Some("") | Some("preserve-source-v1") => EncodingProfileW::PreserveSource,
        Some("compact-jpeg-v1") => EncodingProfileW::CompactJpegV1,
        Some(id) => {
            return Err(CoreError::validation(format!(
                "未知编码 profile {id}（preserve-source-v1|compact-jpeg-v1）"
            )))
        }
    };
    if e == EncodingProfileW::CompactJpegV1 && is_fl {
        return Err(CoreError::variant(
            "compact-jpeg-v1 编码仅适用于明场；荧光不支持有损重编码",
        ));
    }
    Ok(e)
}

use slide_transform_core::plan::EncodingProfile as EncodingProfileW;

fn run_convert(
    profile: Option<&str>,
    encoding: Option<&str>,
    strict_lossless: bool,
    channel_json: &str,
    resume: Option<(ResumePoint, Option<String>, Option<String>)>,
) -> String {
    let src = HostSource::open();
    let magic = match detect(&src) {
        Ok(m) => m,
        Err(e) => return err_json(&e),
    };
    let is_fl = magic == slide_transform_core::kfbf::KFBF_MAGIC;
    let out_profile = match resolve_profile(is_fl, profile) {
        Ok(p) => p,
        Err(e) => return err_json(&e),
    };
    let enc_profile = match resolve_encoding(is_fl, encoding) {
        Ok(p) => p,
        Err(e) => return err_json(&e),
    };
    if enc_profile == EncodingProfileW::CompactJpegV1 && strict_lossless {
        return err_json(&CoreError::policy(
            "compact-jpeg-v1 与 strict-lossless 互斥：逐 tile 重编码必然有损",
        ));
    }
    // a committed state belongs to the layout AND the encoding that wrote
    // it: never continue a partial output under another profile or quality
    // mode (legacy states = the pre-profile/pre-encoding defaults)
    let resume = match resume {
        None => None,
        Some((rp, journalled, journalled_enc)) => {
            let committed_under = match resolve_profile(is_fl, journalled.as_deref()) {
                Ok(p) => p,
                Err(e) => return err_json(&e),
            };
            if committed_under != out_profile {
                return err_json(&CoreError::validation(format!(
                    "resume: 已提交进度属于输出 profile {}，拒绝以 {} 续跑",
                    committed_under.id(),
                    out_profile.id()
                )));
            }
            let committed_enc = match resolve_encoding(is_fl, journalled_enc.as_deref()) {
                Ok(p) => p,
                Err(e) => return err_json(&e),
            };
            if committed_enc != enc_profile {
                return err_json(&CoreError::validation(format!(
                    "resume: 已提交进度属于编码 profile {}，拒绝以 {} 续跑（不混合两种画质）",
                    committed_enc.id(),
                    enc_profile.id()
                )));
            }
            Some(rp)
        }
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
    let checkpoint = HostCheckpoint { profile: out_profile, encoding: enc_profile };
    let mut job = JobControl::new(&progress);
    if CHECKPOINT_ENABLED.load(Ordering::Relaxed) {
        job = job.with_checkpoint(&checkpoint);
    }
    let result = if is_fl {
        let plan = TransformPlan::fluorescence(identity).with_policy(policy);
        match resume.as_ref() {
            Some(rp) => slide_transform_core::convert_fl::convert_kfbf_to_ome_resume(
                &src, &mut sink, &mut scratch, &plan, &job, companion.as_ref(), rp,
            ),
            None => slide_transform_core::convert_fl::convert_kfbf_to_ome(
                &src, &mut sink, &mut scratch, &plan, &job, companion.as_ref(),
            ),
        }
    } else {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(policy)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        match resume.as_ref() {
            Some(rp) => slide_transform_core::convert_bf::convert_kfb_to_bigtiff_resume(
                &src, &mut sink, &mut scratch, &plan, &job, rp,
            ),
            None => slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
                &src, &mut sink, &mut scratch, &plan, &job,
            ),
        }
    };
    // the job borrowed the checkpoint handle; drop it before reusing fields
    drop(job);
    match result {
        Ok(r) => {
            let warnings: Vec<String> =
                r.warnings.iter().map(|w| format!("\"{w}\"")).collect();
            let (lossy_flag, lossy_json) = match &r.lossy_reencode {
                Some(l) => (
                    "true",
                    format!(
                        "{{\"profile\":\"{}\",\"params_fingerprint\":\"{}\",\"quality\":{},\"sampling\":\"{}\",\"huffman\":\"{}\",\"tiles_reencoded\":{},\"tiles_padded\":{}}}",
                        l.profile, l.params_fingerprint, l.quality, l.sampling, l.huffman,
                        l.tiles_reencoded, l.tiles_padded
                    ),
                ),
                None => ("false", "null".to_string()),
            };
            format!(
                "{{{}\"format\":\"{}\",\"output_profile\":\"{}\",\"encoding\":\"{}\",\"lossy_reencode\":{},\"lossy_reencode_params\":{},\"output_bytes\":{},\"width\":{},\"height\":{},\"ifd_count\":{},\"tiles_raw_copied\":{},\"tiles_reencoded\":{},\"resumed\":{},\"channels\":{},\"warnings\":[{}]}}",
                companion_json_warning,
                r.format,
                out_profile.id(),
                enc_profile.id(),
                lossy_flag,
                lossy_json,
                r.output_bytes,
                r.width,
                r.height,
                r.validation.ifd_count,
                r.count_raw_copied(),
                r.count_reencoded(),
                resume.is_some(),
                channels_report_json(&r.channels),
                warnings.join(",")
            )
        }
        Err(e) => err_json(&e),
    }
}

/// Run a conversion writing to the host sink. `strict_lossless` toggles the
/// pixel policy; `channel_json` may be empty (no companion).
#[wasm_bindgen(js_name = "convert")]
pub fn convert(strict_lossless: bool, channel_json: &str) -> String {
    run_convert(None, None, strict_lossless, channel_json, None)
}

/// Run a conversion with an explicit output profile id (`bf-classic`,
/// `bf-ome`, `fl-ome`; empty = the input's pre-profile default). Encoding is
/// preserve-source-v1 (pre-U3 behaviour kept bit-for-bit).
#[wasm_bindgen(js_name = "convertProfile")]
pub fn convert_profile(profile: &str, strict_lossless: bool, channel_json: &str) -> String {
    run_convert(Some(profile), None, strict_lossless, channel_json, None)
}

/// Run a conversion with explicit output AND encoding profile ids (U3).
/// `encoding`: `preserve-source-v1` (default) or `compact-jpeg-v1`
/// (brightfield only).
#[wasm_bindgen(js_name = "convertProfileEncoded")]
pub fn convert_profile_encoded(
    profile: &str,
    encoding: &str,
    strict_lossless: bool,
    channel_json: &str,
) -> String {
    run_convert(Some(profile), Some(encoding), strict_lossless, channel_json, None)
}

/// Resume under an explicit output profile; refused when the checkpoint
/// state was committed under a different one.
#[wasm_bindgen(js_name = "convertResumeProfile")]
pub fn convert_resume_profile(
    resume_json: &str,
    profile: &str,
    strict_lossless: bool,
    channel_json: &str,
) -> String {
    match parse_resume_json(resume_json) {
        Ok(rp) => run_convert(
            Some(profile),
            None,
            strict_lossless,
            channel_json,
            Some((rp, resume_profile_field(resume_json), None)),
        ),
        Err(e) => err_json(&e),
    }
}

/// Resume under explicit output AND encoding profiles (U3); refused when the
/// checkpoint state was committed under a different combination — including
/// a compact request against a legacy (preserve, no field) state.
#[wasm_bindgen(js_name = "convertResumeProfileEncoded")]
pub fn convert_resume_profile_encoded(
    resume_json: &str,
    profile: &str,
    encoding: &str,
    strict_lossless: bool,
    channel_json: &str,
) -> String {
    match parse_resume_json(resume_json) {
        Ok(rp) => run_convert(
            Some(profile),
            Some(encoding),
            strict_lossless,
            channel_json,
            Some((
                rp,
                resume_profile_field(resume_json),
                resume_encoding_field(resume_json),
            )),
        ),
        Err(e) => err_json(&e),
    }
}

/// Resume a conversion from a checkpoint state (the same JSON
/// `stHostCheckpoint` emits; journal-recorded by the runner).
#[wasm_bindgen(js_name = "convertResume")]
pub fn convert_resume(resume_json: &str, strict_lossless: bool, channel_json: &str) -> String {
    match parse_resume_json(resume_json) {
        Ok(rp) => run_convert(
            None,
            None,
            strict_lossless,
            channel_json,
            Some((rp, resume_profile_field(resume_json), None)),
        ),
        Err(e) => err_json(&e),
    }
}

/// Re-open + validate the finished output (streamed sha256 + structural
/// IFD walk) through the host read-back callbacks. Only a passing result
/// may be marked `ready`. `expect_ifd` is the converter's `ifd_count` (main
/// chain + SubIFDs, every profile); 0 skips the equality.
#[wasm_bindgen(js_name = "finalizeValidate")]
pub fn finalize_validate(expect_ifd: u32) -> String {
    let size = host_out_size() as u64;
    let reader = OutReader { size };
    let expect = if expect_ifd == 0 { None } else { Some(expect_ifd) };
    match slide_transform_core::validate::validate_output(&reader, size, expect) {
        Ok(v) => format!(
            "{{\"ok\":true,\"sha256\":\"{}\",\"size\":{},\"ifd_count\":{},\"main_ifds\":{},\"sub_ifds\":{},\"tile_records\":{},\"checks\":[{}]}}",
            v.sha256,
            v.size,
            v.ifd_count,
            v.main_ifds,
            v.sub_ifds,
            v.tile_records,
            v.checks.iter().map(|c| format!("\"{c}\"")).collect::<Vec<_>>().join(",")
        ),
        Err(e) => err_json(&e),
    }
}

/// One dedicated pass: sha256 of the whole source through bounded host
/// reads (resume identity verification; hashing flag stays off).
#[wasm_bindgen(js_name = "sha256Source")]
pub fn sha256_source() -> String {
    let src = HostSource::open();
    match slide_transform_core::validate::stream_sha256(&src, src.size()) {
        Ok(h) => format!("{{\"sha256\":\"{}\",\"size\":{}}}", h, src.size()),
        Err(e) => err_json(&e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn journalled_profile_is_read_from_checkpoint_states() {
        let st = r#"{"level":1,"channel":0,"cell":3,"out":4096,"ifds":[9,3],"profile":"bf-ome"}"#;
        assert_eq!(resume_profile_field(st).as_deref(), Some("bf-ome"));
        // states journalled before output profiles existed carry none
        let legacy = r#"{"level":1,"channel":0,"cell":3,"out":4096,"ifds":[9,3]}"#;
        assert_eq!(resume_profile_field(legacy), None);
    }

    #[test]
    fn journalled_encoding_is_read_from_checkpoint_states() {
        let st = r#"{"level":1,"channel":0,"cell":3,"out":4096,"ifds":[9,3],"profile":"bf-ome","encoding":"compact-jpeg-v1"}"#;
        assert_eq!(resume_profile_field(st).as_deref(), Some("bf-ome"));
        assert_eq!(resume_encoding_field(st).as_deref(), Some("compact-jpeg-v1"));
        // states journalled before U3 carry no encoding → preserve
        let legacy = r#"{"level":1,"channel":0,"cell":3,"out":4096,"ifds":[9,3],"profile":"bf-ome"}"#;
        assert_eq!(resume_encoding_field(legacy), None);
        // unknown ids must be rejected, never silently coerced
        assert_eq!(resume_encoding_field(r#"{"encoding":"compact"}"#).as_deref(), Some("compact"));
    }

    #[test]
    fn legacy_states_resolve_to_the_pre_profile_layouts() {
        assert_eq!(resolve_profile(false, None).unwrap(), OutputProfile::ClassicJpegBigTiff);
        assert_eq!(resolve_profile(true, None).unwrap(), OutputProfile::OmeBigTiffSubifd);
        assert_eq!(resolve_profile(false, Some("bf-ome")).unwrap(), OutputProfile::OmeBigTiffRgbSubifd);
        assert!(resolve_profile(false, Some("fl-ome")).is_err());
        assert!(resolve_profile(true, Some("bf-ome")).is_err());
        assert!(resolve_profile(false, Some("ome")).is_err());
    }

    #[test]
    fn encoding_resolution_rules() {
        // legacy / empty / explicit preserve all mean preserve
        assert_eq!(resolve_encoding(false, None).unwrap(), EncodingProfileW::PreserveSource);
        assert_eq!(resolve_encoding(false, Some("")).unwrap(), EncodingProfileW::PreserveSource);
        assert_eq!(
            resolve_encoding(false, Some("preserve-source-v1")).unwrap(),
            EncodingProfileW::PreserveSource
        );
        assert_eq!(
            resolve_encoding(false, Some("compact-jpeg-v1")).unwrap(),
            EncodingProfileW::CompactJpegV1
        );
        // fluorescence refuses the lossy mode; unknown ids refuse
        assert!(resolve_encoding(true, Some("compact-jpeg-v1")).is_err());
        assert!(resolve_encoding(false, Some("compact")).is_err());
        assert!(resolve_encoding(true, None).is_ok()); // preserve is fine for FL
    }
}
