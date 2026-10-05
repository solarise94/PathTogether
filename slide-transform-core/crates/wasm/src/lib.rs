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

use slide_transform_core::bundle::BundleFs;
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
    // F3: multi-member bundle reads (MRXS). The host owns the member list
    // (OPFS files staged by prepareBundle); the core resolves members by
    // their flat name and never reads one whole.
    #[wasm_bindgen(js_name = "stHostBundleCount", catch)]
    fn host_bundle_count() -> Result<u32, JsValue>;
    #[wasm_bindgen(js_name = "stHostBundleName", catch)]
    fn host_bundle_name(i: u32) -> Result<String, JsValue>;
    #[wasm_bindgen(js_name = "stHostBundleSize", catch)]
    fn host_bundle_size(i: u32) -> Result<f64, JsValue>;
    #[wasm_bindgen(js_name = "stHostBundleReadInto", catch)]
    fn host_bundle_read_into(i: u32, offset: f64, len: u32, ptr: u32) -> Result<JsValue, JsValue>;
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

/// Multi-member bundle source over the host callbacks (F3 MRXS).
struct HostBundle {
    infos: Vec<slide_transform_core::bundle::MemberInfo>,
}

impl HostBundle {
    fn open() -> CoreResult<HostBundle> {
        let n = host_bundle_count()
            .map_err(|e| CoreError::io(format!("宿主 bundle count 异常: {e:?}")))?;
        if n == 0 || n as usize > slide_transform_core::bundle::MAX_MEMBERS {
            return Err(CoreError::io(format!("宿主 bundle 成员数 {n} 异常")));
        }
        let mut infos = Vec::with_capacity(n as usize);
        for i in 0..n {
            let name = host_bundle_name(i)
                .map_err(|e| CoreError::io(format!("宿主 bundle name 异常: {e:?}")))?;
            let size = host_bundle_size(i)
                .map_err(|e| CoreError::io(format!("宿主 bundle size 异常: {e:?}")))?;
            if !slide_transform_core::bundle::valid_member_name(&name) {
                return Err(CoreError::io(format!("宿主 bundle 成员名 {name:?} 非法")));
            }
            infos.push(slide_transform_core::bundle::MemberInfo { name, size: size as u64 });
        }
        Ok(HostBundle { infos })
    }
}

impl slide_transform_core::bundle::BundleFs for HostBundle {
    fn members(&self) -> &[slide_transform_core::bundle::MemberInfo] {
        &self.infos
    }
    fn read_member_at(&self, member: usize, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let end = offset
            .checked_add(len as u64)
            .ok_or_else(|| CoreError::oob("bundle read length overflow"))?;
        if member >= self.infos.len() || end > self.infos[member].size {
            return Err(CoreError::oob("bundle 成员读取越界"));
        }
        let mut out = vec![0u8; len];
        let mut done = 0usize;
        while done < len {
            let want = MAX_CHUNK.min(len - done) as u32;
            let ptr = out.as_mut_ptr() as usize + done;
            let r = host_bundle_read_into(member as u32, (offset + done as u64) as f64, want, ptr as u32)
                .map_err(|e| CoreError::io(format!("宿主 bundle read 异常: {e:?}")))?;
            if let Some(err) = js_err(r) {
                return Err(err);
            }
            done += want as usize;
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
    /// Bounded read-back of committed output bytes (review §4: the pyramid
    /// decodes the previous level's encoded tiles mid-conversion; the host
    /// reads through the same open sync-access handle it writes through).
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        OutReader { size: offset + len as u64 }.read_at(offset, len)
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

/// Emits committed states; the output profile, the encoding profile and the
/// input adapter travel with every state so a journal can never be resumed
/// under a different layout, a different quality mode or a different source
/// adapter.
struct HostCheckpoint {
    profile: OutputProfile,
    encoding: slide_transform_core::plan::EncodingProfile,
    adapter: Option<&'static str>,
    /// The adapter's OWN version (F4 fix: this used to hardcode the MRXS
    /// adapter's version for every adapter; each adapter journals its own
    /// generation now — the runner pins it next to the job record).
    adapter_version: Option<&'static str>,
}

impl slide_transform_core::job::CheckpointCallback for HostCheckpoint {
    fn on_checkpoint(&self, c: &slide_transform_core::job::CheckpointState) {
        if !CHECKPOINT_ENABLED.load(Ordering::Relaxed) {
            return;
        }
        let ifds: Vec<String> = c.ifd_tiles.iter().map(|t| t.to_string()).collect();
        let adapter = match (&self.adapter, &self.adapter_version) {
            (Some(a), Some(v)) => format!(
                ",\"adapter\":\"{a}\",\"adapter_version\":\"{v}\""
            ),
            (Some(a), None) => format!(",\"adapter\":\"{a}\""),
            _ => String::new(),
        };
        let json = format!(
            "{{\"level\":{},\"channel\":{},\"cell\":{},\"out\":{},\"ifds\":[{}],\"profile\":\"{}\",\"encoding\":\"{}\"{}}}",
            c.level,
            c.channel.map(|v| v.to_string()).unwrap_or_else(|| "0".into()),
            c.cell_done,
            c.committed_output,
            ifds.join(","),
            self.profile.id(),
            self.encoding.id(),
            adapter
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

/// Input kind by container signature: TIFF/BigTIFF headers start on the SVS
/// route and are refined by the bounded vendor sniff into SCN (F4) / the
/// generic tiled-JPEG adapter (F5).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum InputKind {
    Kfb,
    Kfbf,
    Svs,
    /// Leica SCN (F4): a TIFF container whose IFD 0 description is the SCN
    /// XML — decided by the bounded vendor sniff, never by extension.
    Scn,
    /// Generic tiled JPEG TIFF/BigTIFF (F5): a TIFF container whose IFD 0
    /// description names NO known vendor — decided by the same sniff.
    Gtiff,
}

fn input_kind(magic: &[u8; 8]) -> InputKind {
    if *magic == slide_transform_core::kfbf::KFBF_MAGIC {
        InputKind::Kfbf
    } else if is_tiff_magic(magic) {
        InputKind::Svs
    } else {
        InputKind::Kfb
    }
}

fn is_tiff_magic(m: &[u8; 8]) -> bool {
    let bo = &m[0..2];
    if bo != b"II" && bo != b"MM" {
        return false;
    }
    let v = if bo == b"II" {
        u16::from_le_bytes([m[2], m[3]])
    } else {
        u16::from_be_bytes([m[2], m[3]])
    };
    v == 42 || v == 43
}

/// Source adapter id of an input kind (journal/checkpoint identity; `None`
/// for the KFB/KFBF readers, which predate adapters).
fn adapter_of(kind: InputKind) -> Option<&'static str> {
    match kind {
        InputKind::Svs => Some(slide_transform_core::svs::SOURCE_FORMAT),
        InputKind::Scn => Some(slide_transform_core::scn::SOURCE_FORMAT),
        InputKind::Gtiff => Some(slide_transform_core::gtiff::SOURCE_FORMAT),
        _ => None,
    }
}

/// Adapter VERSION of an input kind (journal/checkpoint identity; `None`
/// for the KFB/KFBF readers, which predate adapters).
fn adapter_version_of(kind: InputKind) -> Option<&'static str> {
    match kind {
        InputKind::Svs => Some(slide_transform_core::svs::ADAPTER_VERSION),
        InputKind::Scn => Some(slide_transform_core::scn::ADAPTER_VERSION),
        InputKind::Gtiff => Some(slide_transform_core::gtiff::ADAPTER_VERSION),
        _ => None,
    }
}

/// Vendor-aware classification of a TIFF-magic source. OME-TIFF and this
/// converter's own BigTIFF are NOT conversion inputs — typed rejection
/// before anything is staged or written. An unknown vendor (no known
/// vendor description) routes to the F5 generic tiled-JPEG adapter, whose
/// own structural walk types the「暂时直传」variants.
fn tiff_route(src: &dyn ByteSource) -> CoreResult<InputKind> {
    use slide_transform_core::scn::TiffVendor;
    match slide_transform_core::scn::sniff_tiff_vendor(src)? {
        TiffVendor::LeicaScn => Ok(InputKind::Scn),
        TiffVendor::Unknown => Ok(InputKind::Gtiff),
        TiffVendor::OmeTiff => Err(CoreError::variant(
            "OME-TIFF 不是转换输入：平台可直接读取 OME-TIFF，请直接上传该文件",
        )),
        TiffVendor::ConverterBigTiff => Err(CoreError::variant(
            "本工具导出的 BigTIFF 不是转换输入：请直接上传该产物（或选择原始切片）",
        )),
        _ => Ok(InputKind::Svs),
    }
}

/// `"adapter":"…"` of a checkpoint state; absent in states journalled by
/// pre-adapter cores (KFB/KFBF) — a mismatch with the current input's
/// adapter is refused, mirroring the output-profile refusal.
fn resume_adapter_field(resume_json: &str) -> Option<String> {
    let key = "\"adapter\"";
    let at = resume_json.find(key)? + key.len();
    let rest = resume_json[at..].trim_start().strip_prefix(':')?.trim_start();
    let rest = rest.strip_prefix('"')?;
    Some(rest[..rest.find('"')?].to_string())
}

/// SVS capability document (probe result), mirroring the CLI's report.
fn svs_doc_json(doc: &slide_transform_core::svs::SvsDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            format!(
                "{{\"level\":{},\"width\":{},\"height\":{},\"tile_w\":{},\"tile_h\":{},\"tiles_across\":{},\"tiles_down\":{},\"color\":\"{}\",\"jpeg_tables\":{}}}",
                lv.ifd_index,
                lv.width,
                lv.height,
                lv.tile_w,
                lv.tile_h,
                lv.tiles_across,
                lv.tiles_down,
                match lv.color {
                    slide_transform_core::svs::PayloadColor::Rgb => "rgb",
                    slide_transform_core::svs::PayloadColor::YCbCr => "ycbcr",
                },
                lv.jpeg_tables.is_some()
            )
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| format!("{{\"name\":\"{}\",\"width\":{},\"height\":{}}}", a.name, a.width, a.height))
        .collect();
    let mpp = doc
        .mpp
        .map(|v| json_num(v))
        .unwrap_or_else(|| "null".into());
    let obj = doc
        .appmag
        .map(|v| json_num(v))
        .unwrap_or_else(|| "null".into());
    format!(
        "{{\"format\":\"{}\",\"adapter\":\"{}\",\"adapter_version\":\"{}\",\"modality\":\"brightfield\",\"tiff_kind\":\"{}\",\"width\":{},\"height\":{},\"mpp_x\":{},\"mpp_y\":{},\"objective\":{},\"mpp_source\":\"{}\",\"objective_source\":\"{}\",\"levels\":[{}],\"associated\":[{}],\"icc_profile\":{},\"codec\":\"jpeg-baseline-passthrough\",\"estimate\":{}}}",
        slide_transform_core::svs::SOURCE_FORMAT,
        slide_transform_core::svs::SOURCE_FORMAT,
        slide_transform_core::svs::ADAPTER_VERSION,
        match doc.kind {
            slide_transform_core::tiff_read::TiffKind::Classic => "classic",
            slide_transform_core::tiff_read::TiffKind::BigTiff => "bigtiff",
        },
        doc.levels[0].width,
        doc.levels[0].height,
        mpp,
        mpp,
        obj,
        if doc.mpp.is_some() { "aperio-description" } else { "unknown" },
        if doc.appmag.is_some() { "aperio-description-AppMag" } else { "unknown" },
        levels.join(","),
        assoc.join(","),
        doc.icc.is_some(),
        estimate_json(&slide_transform_core::svs::estimate_svs(doc))
    )
}

/// SCN capability document (probe result), mirroring the CLI's report.
fn scn_doc_json(doc: &slide_transform_core::scn::ScnDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            format!(
                "{{\"r\":{},\"ifd\":{},\"width\":{},\"height\":{},\"tile_w\":{},\"tile_h\":{},\"tiles_across\":{},\"tiles_down\":{},\"tiles_total\":{},\"tiles_present\":{},\"tiles_missing\":{},\"color\":\"{}\"}}",
                lv.r,
                lv.ifd_index,
                lv.width,
                lv.height,
                lv.tile_w,
                lv.tile_h,
                lv.tiles_across,
                lv.tiles_down,
                lv.tiles_total,
                lv.tiles_present,
                lv.tiles_missing(),
                match lv.color {
                    slide_transform_core::scn::PayloadColor::Rgb => "rgb",
                    slide_transform_core::scn::PayloadColor::YCbCr => "ycbcr",
                }
            )
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| format!("{{\"name\":\"{}\",\"width\":{},\"height\":{}}}", a.name, a.width, a.height))
        .collect();
    let mpp = doc
        .mpp
        .map(|v| json_num(v))
        .unwrap_or_else(|| "null".into());
    let obj_v = doc
        .objective
        .map(|v| json_num(v))
        .unwrap_or_else(|| "null".into());
    format!(
        "{{\"format\":\"{}\",\"adapter\":\"{}\",\"adapter_version\":\"{}\",\"modality\":\"brightfield\",\"tiff_kind\":\"{}\",\"width\":{},\"height\":{},\"mpp_x\":{},\"mpp_y\":{},\"objective\":{},\"illumination\":\"{}\",\"mpp_source\":\"{}\",\"objective_source\":\"{}\",\"levels\":[{}],\"associated\":[{}],\"xml_bytes\":{},\"codec\":\"jpeg-baseline-passthrough\",\"estimate\":{}}}",
        slide_transform_core::scn::SOURCE_FORMAT,
        slide_transform_core::scn::SOURCE_FORMAT,
        slide_transform_core::scn::ADAPTER_VERSION,
        match doc.kind {
            slide_transform_core::tiff_read::TiffKind::Classic => "classic",
            slide_transform_core::tiff_read::TiffKind::BigTiff => "bigtiff",
        },
        doc.levels[0].width,
        doc.levels[0].height,
        mpp,
        mpp,
        obj_v,
        doc.illumination.as_deref().unwrap_or("unknown"),
        if doc.mpp.is_some() { "scn-view-nanometers" } else { "unknown" },
        if doc.objective.is_some() { "scn-scanSettings-objective" } else { "unknown" },
        levels.join(","),
        assoc.join(","),
        doc.xml_bytes,
        estimate_json(&slide_transform_core::scn::estimate_scn(doc))
    )
}

/// Generic tiled JPEG TIFF capability document (probe result), mirroring
/// the CLI's report.
fn gtiff_doc_json(doc: &slide_transform_core::gtiff::GtiffDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            format!(
                "{{\"ifd\":{},\"width\":{},\"height\":{},\"tile_w\":{},\"tile_h\":{},\"tiles_across\":{},\"tiles_down\":{},\"color\":\"{}\",\"jpeg_tables\":{}}}",
                lv.ifd_index,
                lv.width,
                lv.height,
                lv.tile_w,
                lv.tile_h,
                lv.tiles_across,
                lv.tiles_down,
                match lv.color {
                    slide_transform_core::gtiff::PayloadColor::Rgb => "rgb",
                    slide_transform_core::gtiff::PayloadColor::YCbCr => "ycbcr",
                },
                lv.jpeg_tables.is_some()
            )
        })
        .collect();
    let generated: Vec<String> = doc
        .generated
        .iter()
        .map(|(w, h)| format!("{{\"width\":{w},\"height\":{h}}}"))
        .collect();
    let mpp = doc
        .mpp
        .map(|v| json_num(v))
        .unwrap_or_else(|| "null".into());
    format!(
        "{{\"format\":\"{}\",\"adapter\":\"{}\",\"adapter_version\":\"{}\",\"modality\":\"brightfield\",\"tiff_kind\":\"{}\",\"width\":{},\"height\":{},\"mpp_x\":{},\"mpp_y\":{},\"mpp_source\":\"{}\",\"pyramid_method\":\"{}\",\"levels\":[{}],\"generated_levels\":[{}],\"icc_profile\":{},\"codec\":\"jpeg-baseline-passthrough\",\"estimate\":{}}}",
        slide_transform_core::gtiff::SOURCE_FORMAT,
        slide_transform_core::gtiff::SOURCE_FORMAT,
        slide_transform_core::gtiff::ADAPTER_VERSION,
        match doc.kind {
            slide_transform_core::tiff_read::TiffKind::Classic => "classic",
            slide_transform_core::tiff_read::TiffKind::BigTiff => "bigtiff",
        },
        doc.levels[0].width,
        doc.levels[0].height,
        mpp,
        mpp,
        if doc.mpp.is_some() { "tiff-resolution-tags" } else { "unknown" },
        slide_transform_core::gtiff::PYRAMID_METHOD,
        levels.join(","),
        generated.join(","),
        doc.icc.is_some(),
        estimate_json(&slide_transform_core::gtiff::estimate_gtiff(doc))
    )
}

/// TIFF-container probe dispatch (host-free so it is unit-testable):
/// SCN vendor → the SCN adapter, unknown vendors → the generic tiled-JPEG
/// adapter, Aperio → the SVS adapter, and a routing Err (OME-TIFF /
/// converter BigTIFF / structural failure) is returned VERBATIM — never
/// masked by another adapter's message.
fn probe_tiff_doc(src: &dyn ByteSource) -> CoreResult<String> {
    match tiff_route(src)? {
        InputKind::Scn => {
            slide_transform_core::scn::probe_scn(src).map(|doc| scn_doc_json(&doc))
        }
        InputKind::Gtiff => {
            slide_transform_core::gtiff::probe_gtiff(src).map(|doc| gtiff_doc_json(&doc))
        }
        _ => slide_transform_core::svs::probe_svs(src).map(|doc| svs_doc_json(&doc)),
    }
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
    let res = if is_tiff_magic(&magic) {
        // F1/F4: bounded TIFF walk + vendor dispatch (typed rejections for
        // OME-TIFF / converter BigTIFF inside tiff_route — an Err must
        // surface as-is, never fall through to the SVS probe; review 2026-10-05 #2)
        probe_tiff_doc(&src)
    } else if magic == slide_transform_core::kfbf::KFBF_MAGIC {
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
    resume: Option<(ResumePoint, Option<String>, Option<String>, Option<String>)>,
    bundle: bool,
    budget_bytes: Option<f64>,
) -> String {
    if bundle {
        return run_convert_bundle(profile, encoding, strict_lossless, channel_json, resume, budget_bytes);
    }
    let src = HostSource::open();
    let magic = match detect(&src) {
        Ok(m) => m,
        Err(e) => return err_json(&e),
    };
    let mut kind = input_kind(&magic);
    if kind == InputKind::Svs {
        // F4: vendor dispatch inside the TIFF container; OME-TIFF and this
        // converter's own BigTIFF are typed rejections before any output
        kind = match tiff_route(&src) {
            Ok(k) => k,
            Err(e) => return err_json(&e),
        };
    }
    let is_fl = kind == InputKind::Kfbf;
    let adapter = adapter_of(kind);
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
    // a committed state belongs to the layout, the encoding AND the input
    // adapter that wrote it: never continue a partial output under another
    // profile or quality mode (legacy states = the pre-profile/pre-encoding
    // defaults), nor under a different source adapter (KFB state + SVS copy)
    let resume = match resume {
        None => None,
        Some((rp, journalled, journalled_enc, journalled_adapter)) => {
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
            let state_adapter = journalled_adapter;
            let same = match (&state_adapter, adapter) {
                (None, None) => true,
                (Some(a), Some(b)) => a == b,
                _ => false,
            };
            if !same {
                return err_json(&CoreError::validation(format!(
                    "resume: 已提交进度属于输入适配器 {}，拒绝以 {} 续跑",
                    state_adapter.as_deref().unwrap_or("kfb"),
                    adapter.unwrap_or("kfb")
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
    let checkpoint = HostCheckpoint {
        profile: out_profile,
        encoding: enc_profile,
        adapter,
        adapter_version: adapter_version_of(kind),
    };
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
    } else if kind == InputKind::Scn {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(policy)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        match resume.as_ref() {
            Some(rp) => slide_transform_core::convert_scn::convert_scn_to_bigtiff_resume(
                &src, &mut sink, &mut scratch, &plan, &job, rp,
            ),
            None => slide_transform_core::convert_scn::convert_scn_to_bigtiff(
                &src, &mut sink, &mut scratch, &plan, &job,
            ),
        }
    } else if kind == InputKind::Gtiff {
        // Review §1 parity: the adapter bounds its probe/composition
        // working set by the host's memory budget (the single-file path
        // keeps the conservative saver default, like SCN).
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(policy)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        plan.limits.memory_budget_bytes = budget_bytes
            .filter(|v| v.is_finite() && *v > 0.0)
            .map(|v| v as u64)
            .unwrap_or(slide_transform_core::budget::SAVER_BUDGET_BYTES);
        match resume.as_ref() {
            Some(rp) => slide_transform_core::convert_gtiff::convert_gtiff_to_bigtiff_resume(
                &src, &mut sink, &mut scratch, &plan, &job, rp,
            ),
            None => slide_transform_core::convert_gtiff::convert_gtiff_to_bigtiff(
                &src, &mut sink, &mut scratch, &plan, &job,
            ),
        }
    } else if kind == InputKind::Svs {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(policy)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        match resume.as_ref() {
            Some(rp) => slide_transform_core::convert_svs::convert_svs_to_bigtiff_resume(
                &src, &mut sink, &mut scratch, &plan, &job, rp,
            ),
            None => slide_transform_core::convert_svs::convert_svs_to_bigtiff(
                &src, &mut sink, &mut scratch, &plan, &job,
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
                "{{{}\"format\":\"{}\",\"source_format\":{},\"adapter_version\":{},\"output_profile\":\"{}\",\"encoding\":\"{}\",\"lossy_reencode\":{},\"lossy_reencode_params\":{},\"output_bytes\":{},\"width\":{},\"height\":{},\"ifd_count\":{},\"tiles_raw_copied\":{},\"tiles_reencoded\":{},\"resumed\":{},\"channels\":{},\"warnings\":[{}]}}",
                companion_json_warning,
                r.format,
                r.source_format.map(|f| format!("\"{f}\"")).unwrap_or_else(|| "null".into()),
                r.adapter_version.map(|v| format!("\"{v}\"")).unwrap_or_else(|| "null".into()),
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

/// Bundle (F3 MRXS) conversion path: the host owns the member files; the
/// adapter resolves them by flat name. The output/encoding refusals mirror
/// the single-file path; committed progress belongs to the `mirax-bundle`
/// adapter and a state journalled under another adapter is refused.
fn run_convert_bundle(
    profile: Option<&str>,
    encoding: Option<&str>,
    strict_lossless: bool,
    _channel_json: &str,
    resume: Option<(ResumePoint, Option<String>, Option<String>, Option<String>)>,
    budget_bytes: Option<f64>,
) -> String {
    let fs = match HostBundle::open() {
        Ok(f) => f,
        Err(e) => return err_json(&e),
    };
    // the stem comes from the entry member name (probe_bundle does the same)
    let stem = fs
        .members()
        .iter()
        .find(|m| m.name.rsplit('/').next().map(|n| n.to_ascii_lowercase().ends_with(".mrxs")).unwrap_or(false))
        .and_then(|m| {
            let leaf = m.name.rsplit('/').next().unwrap_or(&m.name);
            leaf.strip_suffix(".mrxs").or_else(|| {
                leaf.char_indices().rfind(|(_, c)| *c == '.').map(|(i, _)| &leaf[..i])
            })
        })
        .map(|s| s.to_string())
        .unwrap_or_else(|| "slide".to_string());
    let adapter = Some(slide_transform_core::mirax::SOURCE_FORMAT);
    let out_profile = match resolve_profile(false, profile) {
        Ok(p) => p,
        Err(e) => return err_json(&e),
    };
    let enc_profile = match resolve_encoding(false, encoding) {
        Ok(p) => p,
        Err(e) => return err_json(&e),
    };
    if enc_profile == EncodingProfileW::CompactJpegV1 && strict_lossless {
        return err_json(&CoreError::policy(
            "compact-jpeg-v1 与 strict-lossless 互斥：逐 tile 重编码必然有损",
        ));
    }
    if strict_lossless {
        // composition + JPEG re-encode is inherently lossy for this format
        return err_json(&CoreError::policy(
            "strict-lossless 与 MRXS 组合输出互斥：拼接 tile 必然重编码（有损），无逐字节搬运路径",
        ));
    }
    let resume = match resume {
        None => None,
        Some((rp, journalled, journalled_enc, journalled_adapter)) => {
            let committed_under = match resolve_profile(false, journalled.as_deref()) {
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
            let committed_enc = match resolve_encoding(false, journalled_enc.as_deref()) {
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
            let state_adapter = journalled_adapter;
            let same = match (&state_adapter, adapter) {
                (None, None) => true,
                (Some(a), Some(b)) => a == b,
                _ => false,
            };
            if !same {
                return err_json(&CoreError::validation(format!(
                    "resume: 已提交进度属于输入适配器 {}，拒绝以 {} 续跑",
                    state_adapter.as_deref().unwrap_or("kfb"),
                    adapter.unwrap_or("kfb")
                )));
            }
            Some(rp)
        }
    };
    let identity = InputIdentity {
        name: "browser-bundle".to_string(),
        size: fs.members().iter().map(|m| m.size).sum(),
        sha256: None,
    };
    let policy = if strict_lossless {
        slide_transform_core::plan::PixelPolicy::StrictLossless
    } else {
        slide_transform_core::plan::PixelPolicy::AllowEdgeReencode
    };
    let mut plan = TransformPlan::brightfield(identity)
        .with_policy(policy)
        .with_encoding(enc_profile);
    plan.profile = out_profile;
    // Review §1: the browser resource profile's budget — the MRXS adapter
    // charges its metadata/decode working set against it and refuses with a
    // typed `resource_profile_insufficient` BEFORE allocating.
    plan.limits.memory_budget_bytes = budget_bytes
        .filter(|v| v.is_finite() && *v > 0.0)
        .map(|v| v as u64)
        .unwrap_or(slide_transform_core::budget::SAVER_BUDGET_BYTES);

    let mut sink = HostSink;
    let mut scratch = HostScratchFactory;
    let progress = HostProgress;
    let checkpoint = HostCheckpoint {
        profile: out_profile,
        encoding: enc_profile,
        adapter,
        adapter_version: Some(slide_transform_core::mirax::ADAPTER_VERSION),
    };
    let mut job = JobControl::new(&progress);
    if CHECKPOINT_ENABLED.load(Ordering::Relaxed) {
        job = job.with_checkpoint(&checkpoint);
    }
    let result = slide_transform_core::convert_mirax::convert_mirax_to_bigtiff(
        &fs, &stem, &mut sink, &mut scratch, &plan, &job,
    );
    // the job borrowed the checkpoint handle; drop it before reusing fields
    drop(job);
    match result {
        Ok(r) => {
            let warnings: Vec<String> = r.warnings.iter().map(|w| format!("\"{w}\"")).collect();
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
            let composed_json = match &r.composed {
                Some(c) => format!(
                    "{{\"mode\":\"{}\",\"fingerprint\":\"{}\",\"quality\":{},\"sampling\":\"{}\",\"huffman\":\"{}\",\"tiles_composed\":{},\"tiles_filled\":{},\"tiles_deduped\":{},\"pyramid\":\"{}\"}}",
                    c.mode, c.fingerprint, c.quality, c.sampling, c.huffman,
                    c.tiles_composed, c.tiles_filled, c.tiles_deduped, c.pyramid
                ),
                None => "null".to_string(),
            };
            format!(
                "{{\"format\":\"{}\",\"source_format\":\"{}\",\"adapter_version\":\"{}\",\"output_profile\":\"{}\",\"encoding\":\"{}\",\"lossy_reencode\":{},\"lossy_reencode_params\":{},\"composed\":{},\"output_bytes\":{},\"width\":{},\"height\":{},\"ifd_count\":{},\"tiles_raw_copied\":{},\"tiles_reencoded\":{},\"tiles_filled\":{},\"resumed\":{},\"channels\":[],\"warnings\":[{}]}}",
                r.format,
                r.source_format.unwrap_or(""),
                r.adapter_version.unwrap_or(""),
                out_profile.id(),
                enc_profile.id(),
                lossy_flag,
                lossy_json,
                composed_json,
                r.output_bytes,
                r.width,
                r.height,
                r.validation.ifd_count,
                r.count_raw_copied(),
                r.count_reencoded(),
                r.levels.iter().map(|l| l.tiles_filled).sum::<u64>(),
                resume.is_some(),
                warnings.join(",")
            )
        }
        Err(e) => err_json(&e),
    }
}

/// Probe a bundle input (F3 MRXS) through the bundle host callbacks.
/// `budget_bytes` is the browser resource profile's budget (review §1): the
/// probe refuses with `resource_profile_insufficient` when its metadata
/// working set would exceed it — before any large allocation. `undefined`
/// keeps the conservative saver default (192 MiB).
#[wasm_bindgen(js_name = "probeBundle")]
pub fn probe_bundle(budget_bytes: Option<f64>) -> String {
    let fs = match HostBundle::open() {
        Ok(f) => f,
        Err(e) => return err_json(&e),
    };
    // the entry name ends with .mrxs; resolve the stem from the members
    let entry = fs
        .members()
        .iter()
        .find(|m| m.name.rsplit('/').next().map(|n| n.to_ascii_lowercase().ends_with(".mrxs")).unwrap_or(false));
    let Some(entry) = entry else {
        return err_json(&CoreError::validation(
            "包内没有 .mrxs 主入口（MRXS 需要完整包）",
        ));
    };
    let stem = entry.name.trim_end_matches(".mrxs").to_string();
    let budget = budget_bytes
        .filter(|v| v.is_finite() && *v > 0.0)
        .map(|v| v as u64)
        .unwrap_or(slide_transform_core::budget::SAVER_BUDGET_BYTES);
    match slide_transform_core::mirax::probe_mirax_with_budget(&fs, &stem, budget) {
        Ok(doc) => {
            let levels: Vec<String> = doc
                .levels
                .iter()
                .enumerate()
                .map(|(li, lv)| {
                    format!(
                        "{{\"level\":{},\"width\":{},\"height\":{},\"images\":{},\"payload_bytes\":{}}}",
                        li, lv.width, lv.height, lv.images.len(), lv.payload_bytes
                    )
                })
                .collect();
            let (mx, my) = doc.mpp.unwrap_or((f64::NAN, f64::NAN));
            let est = slide_transform_core::mirax::estimate_mirax(&doc);
            let doc_json = format!(
                "{{\"format\":\"{}\",\"adapter\":\"{}\",\"adapter_version\":\"{}\",\"modality\":\"brightfield\",\"width\":{},\"height\":{},\"mpp_x\":{},\"mpp_y\":{},\"objective\":{},\"position_source\":\"{}\",\"levels\":[{}],\"codec\":\"mosaic-compose-reencode\",\"estimate\":{}}}",
                slide_transform_core::mirax::SOURCE_FORMAT,
                slide_transform_core::mirax::SOURCE_FORMAT,
                slide_transform_core::mirax::ADAPTER_VERSION,
                doc.levels[0].width,
                doc.levels[0].height,
                json_num(mx),
                json_num(my),
                doc.objective.map(json_num).unwrap_or_else(|| "null".into()),
                match doc.position_source {
                    slide_transform_core::mirax::PositionSource::VimslideBuffer => "VIMSLIDE_POSITION_BUFFER",
                    slide_transform_core::mirax::PositionSource::StitchingIntensity => "StitchingIntensityLayer(deflate)",
                    slide_transform_core::mirax::PositionSource::Synthesized => "synthesized-from-overlap",
                },
                levels.join(","),
                estimate_json(&est)
            );
            format!(
                "{{\"core_version\":\"{}\",\"size\":{},\"document\":{}}}",
                slide_transform_core::CORE_VERSION,
                fs.members().iter().map(|m| m.size).sum::<u64>(),
                doc_json
            )
        }
        Err(e) => err_json(&e),
    }
}

/// Bundle conversion with explicit output AND encoding profile ids (F3).
/// `budget_bytes`: the browser resource profile's budget (review §1;
/// `undefined` = the conservative saver default).
#[wasm_bindgen(js_name = "convertProfileEncodedBundle")]
pub fn convert_profile_encoded_bundle(
    profile: &str,
    encoding: &str,
    strict_lossless: bool,
    channel_json: &str,
    budget_bytes: Option<f64>,
) -> String {
    run_convert(Some(profile), Some(encoding), strict_lossless, channel_json, None, true, budget_bytes)
}

/// Bundle resume under explicit profiles (F3); refused when the checkpoint
/// state was committed under another profile/encoding/adapter combination.
#[wasm_bindgen(js_name = "convertResumeProfileEncodedBundle")]
pub fn convert_resume_profile_encoded_bundle(
    resume_json: &str,
    profile: &str,
    encoding: &str,
    strict_lossless: bool,
    channel_json: &str,
    budget_bytes: Option<f64>,
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
                resume_adapter_field(resume_json),
            )),
            true,
            budget_bytes,
        ),
        Err(e) => err_json(&e),
    }
}

/// Run a conversion writing to the host sink. `strict_lossless` toggles the
/// pixel policy; `channel_json` may be empty (no companion).
#[wasm_bindgen(js_name = "convert")]
pub fn convert(strict_lossless: bool, channel_json: &str) -> String {
    run_convert(None, None, strict_lossless, channel_json, None, false, None)
}

/// Run a conversion with an explicit output profile id (`bf-classic`,
/// `bf-ome`, `fl-ome`; empty = the input's pre-profile default). Encoding is
/// preserve-source-v1 (pre-U3 behaviour kept bit-for-bit).
#[wasm_bindgen(js_name = "convertProfile")]
pub fn convert_profile(profile: &str, strict_lossless: bool, channel_json: &str) -> String {
    run_convert(Some(profile), None, strict_lossless, channel_json, None, false, None)
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
    run_convert(Some(profile), Some(encoding), strict_lossless, channel_json, None, false, None)
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
            Some((
                rp,
                resume_profile_field(resume_json),
                resume_encoding_field(resume_json),
                resume_adapter_field(resume_json),
            )),
            false,
            None,
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
                resume_adapter_field(resume_json),
            )),
            false,
            None,
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
            Some((
                rp,
                resume_profile_field(resume_json),
                resume_encoding_field(resume_json),
                resume_adapter_field(resume_json),
            )),
            false,
            None,
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
    fn journalled_adapter_is_read_from_checkpoint_states() {
        let st = r#"{"level":0,"channel":0,"cell":3,"out":4096,"ifds":[3],"profile":"bf-ome","adapter":"aperio-svs-jpeg"}"#;
        assert_eq!(resume_adapter_field(st).as_deref(), Some("aperio-svs-jpeg"));
        // KFB/KFBF states (and pre-adapter cores) carry no adapter field
        let legacy = r#"{"level":1,"channel":0,"cell":3,"out":4096,"ifds":[9,3],"profile":"bf-ome"}"#;
        assert_eq!(resume_adapter_field(legacy), None);
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
    fn all_three_fields_are_read_from_one_state() {
        // a merged-core SVS compact state carries all three independent
        // fields at once; each is parsed independently of the others
        let st = r#"{"level":0,"channel":0,"cell":3,"out":4096,"ifds":[3],"profile":"bf-classic","encoding":"compact-jpeg-v1","adapter":"aperio-svs-jpeg"}"#;
        assert_eq!(resume_profile_field(st).as_deref(), Some("bf-classic"));
        assert_eq!(resume_encoding_field(st).as_deref(), Some("compact-jpeg-v1"));
        assert_eq!(resume_adapter_field(st).as_deref(), Some("aperio-svs-jpeg"));
        // a legacy state with NONE of the fields (pre-profile/pre-U3/
        // pre-adapter core) parses to None for all three — meaning the
        // legacy defaults (classic layout, preserve bytes, KFB adapter)
        let legacy = r#"{"level":1,"channel":0,"cell":3,"out":4096,"ifds":[9,3]}"#;
        assert_eq!(resume_profile_field(legacy), None);
        assert_eq!(resume_encoding_field(legacy), None);
        assert_eq!(resume_adapter_field(legacy), None);
    }

    /// Minimal little-endian BigTIFF with one IFD (16×16 single 16×16 tile
    /// for the SCN-positive case; other cases only need the description so
    /// the vendor routing fires before any payload is touched).
    fn one_ifd_bigtiff(
        desc: &str,
        jpeg_payload: bool,
        compression: u16,
        photo: u16,
        width: u32,
        height: u32,
    ) -> Vec<u8> {
        let desc = desc.as_bytes();
        let payload = jpeg_payload.then(|| {
            slide_transform_core::jpeg::encode_rgb(
                &[255u8; 16 * 16 * 3],
                16,
                16,
                &slide_transform_core::jpeg::EncoderCfg::with_quality(
                    90,
                    slide_transform_core::jpeg::Sampling::S444,
                ),
            )
            .unwrap()
        });
        let payload_at = 16 + desc.len() as u64 + 1;
        let mut entries: Vec<(u16, u16, u64, Vec<u8>)> = vec![
            (256, 4, 1, width.to_le_bytes().to_vec()),
            (257, 4, 1, height.to_le_bytes().to_vec()),
            (259, 3, 1, compression.to_le_bytes().to_vec()),
            (262, 3, 1, photo.to_le_bytes().to_vec()),
            // description is EXTERNAL: it sits at offset 16 (right after
            // the header); the entry carries its length + offset
            (270, 2, desc.len() as u64 + 1, 16u64.to_le_bytes().to_vec()),
            (277, 3, 1, 3u16.to_le_bytes().to_vec()),
            (284, 3, 1, 1u16.to_le_bytes().to_vec()),
        ];
        if let Some(p) = &payload {
            entries.push((322, 3, 1, 16u16.to_le_bytes().to_vec()));
            entries.push((323, 3, 1, 16u16.to_le_bytes().to_vec()));
            entries.push((324, 16, 1, payload_at.to_le_bytes().to_vec()));
            entries.push((325, 16, 1, (p.len() as u64).to_le_bytes().to_vec()));
        }
        entries.sort_by_key(|e| e.0);
        // layout: header [0,16) | description [16, …) | payload | IFD last
        let ifd_at = payload_at + payload.as_ref().map_or(0, |p| p.len() as u64);
        let mut buf: Vec<u8> = Vec::new();
        buf.extend_from_slice(b"II");
        buf.extend_from_slice(&43u16.to_le_bytes());
        buf.extend_from_slice(&8u16.to_le_bytes());
        buf.extend_from_slice(&0u16.to_le_bytes());
        buf.extend_from_slice(&ifd_at.to_le_bytes());
        buf.extend_from_slice(desc);
        buf.push(0);
        if let Some(p) = payload {
            buf.extend_from_slice(&p);
        }
        buf.extend_from_slice(&(entries.len() as u64).to_le_bytes());
        for (tag, typ, count, val) in &entries {
            buf.extend_from_slice(&tag.to_le_bytes());
            buf.extend_from_slice(&typ.to_le_bytes());
            buf.extend_from_slice(&count.to_le_bytes());
            let mut v = val.clone();
            v.resize(8, 0);
            buf.extend_from_slice(&v);
        }
        buf.extend_from_slice(&0u64.to_le_bytes());
        buf
    }

    const SCN_XML_DESC: &str = "<?xml version=\"1.0\"?><scn xmlns=\"http://www.leica-microsystems.com/scn/2010/10/01\"><collection><image><pixels sizeX=\"16\" sizeY=\"16\"><dimension sizeX=\"16\" sizeY=\"16\" r=\"0\" ifd=\"0\" /></pixels><view sizeX=\"8000\" sizeY=\"8000\" /><scanSettings><illuminationSettings><illuminationSource>brightfield</illuminationSource></illuminationSettings></scanSettings></image></collection></scn>";
    const OME_XML_DESC: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\"></OME>";
    const CONVERTER_DESC: &str = "{\"adapter\": \"aperio-svs-jpeg\", \"adapter_version\": \"1\", \"mpp_x\": 0.5, \"mpp_y\": 0.5, \"objective\": 20.0, \"source_format\": \"aperio-svs-jpeg\"}";

    #[test]
    fn probe_tiff_doc_routes_by_vendor_and_surfaces_rejections() {
        use slide_transform_core::error::ErrorCode::*;
        use slide_transform_core::io::MemSource;

        // SCN XML → the SCN adapter document (16×16 single-tile mini slide)
        let doc = probe_tiff_doc(&MemSource::new(one_ifd_bigtiff(
            SCN_XML_DESC, true, 7, 6, 16, 16,
        )))
        .unwrap();
        assert!(doc.contains("\"format\":\"leica-scn-jpeg\""), "{doc}");

        // OME-TIFF → the typed routing refusal, NEVER the SVS "未标识
        // Aperio" fallback (review #2 regression)
        let e = probe_tiff_doc(&MemSource::new(one_ifd_bigtiff(
            OME_XML_DESC, false, 7, 2, 520, 300,
        )))
        .unwrap_err();
        assert_eq!(e.code, UnsupportedKfbVariant);
        assert!(e.message.contains("OME-TIFF 不是转换输入"), "{}", e.message);
        assert!(!e.message.contains("未标识 Aperio"));

        // converter BigTIFF → same
        let e = probe_tiff_doc(&MemSource::new(one_ifd_bigtiff(
            CONVERTER_DESC, false, 7, 2, 520, 300,
        )))
        .unwrap_err();
        assert!(e.message.contains("不是转换输入"), "{}", e.message);
    }

    #[test]
    fn scn_input_kind_carries_its_own_adapter_identity() {
        assert_eq!(adapter_of(InputKind::Scn), Some("leica-scn-jpeg"));
        assert_eq!(adapter_version_of(InputKind::Scn), Some("1"));
        assert_eq!(adapter_version_of(InputKind::Svs), Some("1"));
        assert_eq!(adapter_version_of(InputKind::Kfb), None);
    }

    #[test]
    fn unknown_vendor_tiff_routes_to_the_generic_adapter() {
        use slide_transform_core::io::MemSource;
        // no description at all → vendor Unknown → the F5 generic adapter
        let doc = probe_tiff_doc(&MemSource::new(one_ifd_bigtiff(
            "", true, 7, 6, 16, 16,
        )))
        .unwrap();
        assert!(
            doc.contains("\"format\":\"generic-tiled-jpeg-tiff\""),
            "{doc}"
        );
        assert!(doc.contains("\"adapter_version\":\"1\""), "{doc}");
        assert_eq!(adapter_of(InputKind::Gtiff), Some("generic-tiled-jpeg-tiff"));
        assert_eq!(adapter_version_of(InputKind::Gtiff), Some("1"));
        // OME / converter BigTIFF still refuse with their typed reasons,
        // never the generic adapter's structural messages
        let e = probe_tiff_doc(&MemSource::new(one_ifd_bigtiff(
            OME_XML_DESC, false, 7, 2, 520, 300,
        )))
        .unwrap_err();
        assert!(e.message.contains("OME-TIFF 不是转换输入"), "{}", e.message);
    }

    #[test]
    fn tiff_magics_route_to_the_svs_adapter() {
        assert_eq!(input_kind(&[0x49, 0x49, 42, 0, 8, 0, 0, 0]), InputKind::Svs);
        assert_eq!(input_kind(&[0x4D, 0x4D, 0, 42, 0, 0, 0, 8]), InputKind::Svs);
        assert_eq!(input_kind(&[0x49, 0x49, 43, 0, 8, 0, 0, 0]), InputKind::Svs);
        assert_eq!(input_kind(&[0x4D, 0x4D, 0, 43, 0, 0, 0, 16]), InputKind::Svs);
        assert_eq!(input_kind(&[0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00]), InputKind::Kfb);
        assert_eq!(input_kind(&[0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x46]), InputKind::Kfbf);
        // II + wrong version is neither
        assert_eq!(input_kind(&[0x49, 0x49, 45, 0, 8, 0, 0, 0]), InputKind::Kfb);
        assert_eq!(adapter_of(InputKind::Svs), Some("aperio-svs-jpeg"));
        assert_eq!(adapter_of(InputKind::Kfb), None);
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
