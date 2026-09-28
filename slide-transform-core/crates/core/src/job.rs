//! Job control: cooperative cancellation flag + progress callback. The
//! converters poll the cancel flag between tiles and abort with a typed
//! `conversion_timeout`/`Io`-class error; progress fires once per completed
//! output row/level unit with committed byte counts (checkpoint-friendly:
//! the same counts the writers have already flushed to the sink).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// Wall-clock source. On wasm32-unknown-unknown there is no clock primitive,
/// so elapsed() is always 0 (the timeout guard is inert and the host enforces
/// wall-time through the cancel flag); native keeps Instant semantics.
#[cfg(not(target_arch = "wasm32"))]
use std::time::Instant;

#[cfg(target_arch = "wasm32")]
#[derive(Clone, Copy)]
pub struct Instant;

#[cfg(target_arch = "wasm32")]
impl Instant {
    pub fn now() -> Self {
        Instant
    }
    pub fn elapsed(&self) -> std::time::Duration {
        std::time::Duration::from_secs(0)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProgressUnit {
    Level,
    TileRow,
}

#[derive(Debug, Clone, Copy)]
pub struct Progress {
    pub unit: ProgressUnit,
    /// Brightfield: pyramid level. Fluorescence: level (channel inner).
    pub level: u32,
    pub channel: Option<usize>,
    /// Completed units of `unit` within the level/channel.
    pub done: u64,
    pub total: u64,
    /// Bytes committed to the output sink so far.
    pub committed_bytes: u64,
}

pub trait ProgressCallback: Send + Sync {
    fn on_progress(&self, p: &Progress);
}

/// No-op callback.
pub struct NullProgress;
impl ProgressCallback for NullProgress {
    fn on_progress(&self, _p: &Progress) {}
}

/// Shared cooperative-cancel handle.
#[derive(Clone, Default)]
pub struct CancelFlag {
    inner: Arc<AtomicBool>,
}

impl CancelFlag {
    pub fn new() -> Self {
        CancelFlag { inner: Arc::new(AtomicBool::new(false)) }
    }
    pub fn cancel(&self) {
        self.inner.store(true, Ordering::SeqCst);
    }
    pub fn is_cancelled(&self) -> bool {
        self.inner.load(Ordering::SeqCst)
    }
}

/// Bundle passed through a conversion job.
pub struct JobControl<'a> {
    pub progress: &'a dyn ProgressCallback,
    pub cancel: CancelFlag,
    started: Instant,
    timeout_seconds: f64,
}

impl<'a> JobControl<'a> {
    pub fn new(progress: &'a dyn ProgressCallback) -> Self {
        JobControl {
            progress,
            cancel: CancelFlag::new(),
            started: Instant::now(),
            timeout_seconds: f64::INFINITY,
        }
    }

    pub fn with_timeout(mut self, seconds: f64) -> Self {
        self.timeout_seconds = seconds;
        self
    }

    /// Tick: cancellation + wall-clock budget. Called between tiles.
    pub fn check(&self) -> crate::error::CoreResult<()> {
        if self.cancel.is_cancelled() {
            return Err(crate::error::CoreError::io("已取消"));
        }
        if self.started.elapsed().as_secs_f64() > self.timeout_seconds {
            return Err(crate::error::CoreError::timeout(format!(
                "转换超过 {:.1}s",
                self.timeout_seconds
            )));
        }
        Ok(())
    }

    pub fn elapsed(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }
}

#[cfg(not(target_arch = "wasm32"))]
pub use std::time::Instant as WallInstant;
#[cfg(target_arch = "wasm32")]
pub use Instant as WallInstant;
