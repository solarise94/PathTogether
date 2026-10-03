//! Bounded random-access IO traits (the only IO surface of the core crate).
//!
//! Native CLI binds these to files; the wasm/browser runner (C0 part ③) will
//! bind `ByteSource` to File.slice reads and `RandomAccessSink` to an OPFS
//! sync access handle. Offsets are `u64` end to end; reads/writes are always
//! explicit-length and bounded by the caller, never whole-file.

use crate::error::{CoreError, CoreResult};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// Read-only byte source with explicit-offset reads.
pub trait ByteSource {
    fn size(&self) -> u64;
    /// Read exactly `len` bytes at `offset`. Out-of-bounds requests are an
    /// error (never a short read).
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>>;

    /// Optional identity string for manifests/logs.
    fn identity(&self) -> String {
        "bytesource".to_string()
    }
}

/// Positional-write sink with truncate/flush/read-back. A `truncate` may only
/// grow the logical size in this spike (the writer never shrinks output).
///
/// `read_at` (review §4): the L0-derived pyramid composes reduced output
/// levels from the ENCODED tiles of the previous level, which live in this
/// sink — the only bytes that survive a crash at every checkpoint, so a
/// resumed reduced level continues from the same pixels without recomposing.
/// Readers are bounded explicit-length reads of already-written bytes.
pub trait RandomAccessSink {
    fn write_at(&mut self, offset: u64, data: &[u8]) -> CoreResult<()>;
    fn truncate(&mut self, size: u64) -> CoreResult<()>;
    fn flush(&mut self) -> CoreResult<()>;
    /// Read back `len` bytes written at `offset` (must be `offset+len ≤` the
    /// committed cursor the caller tracks; out-of-bounds is an error).
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>>;
}

/// Factory for scratch sinks (paged tile-index spill files). The browser
/// runner hands out OPFS scratch files; tests hand out memory.
///
/// Scratch sinks must be readable back (the converter streams spilled index
/// records and — review §4 — the pyramid reads the previous level's tile
/// records), so they implement read/write on the one `RandomAccessSink`
/// surface.
pub trait ScratchFactory {
    /// Create fresh (truncate any prior content).
    fn create(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>>;
    /// Open WITHOUT truncating (resume: keep already-committed bytes).
    /// Memory-backed factories have no persistence and simply create empty.
    fn create_preserve(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>> {
        self.create(name)
    }
}

/// A scratch sink: a `RandomAccessSink` (which includes bounded read-back).
pub trait ScratchSink: RandomAccessSink {}
impl<T: RandomAccessSink + ?Sized> ScratchSink for T {}

// --------------------------------------------------------------------------- //
// In-memory impls (unit tests, wasm smoke, fuzz-ish malformed inputs)
// --------------------------------------------------------------------------- //

pub struct MemSource {
    pub data: Vec<u8>,
}

impl MemSource {
    pub fn new(data: Vec<u8>) -> Self {
        MemSource { data }
    }
}

impl ByteSource for MemSource {
    fn size(&self) -> u64 {
        self.data.len() as u64
    }
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let off = usize::try_from(offset)
            .map_err(|_| CoreError::oob(format!("offset {offset} > usize")))?;
        let end = off.checked_add(len).ok_or_else(|| CoreError::oob("length overflow"))?;
        if end > self.data.len() {
            return Err(CoreError::oob(format!(
                "read [{offset},+{len}) beyond source size {}",
                self.data.len()
            )));
        }
        Ok(self.data[off..end].to_vec())
    }
    fn identity(&self) -> String {
        "mem".to_string()
    }
}

pub struct MemSink {
    pub data: Vec<u8>,
}

impl MemSink {
    pub fn new() -> Self {
        MemSink { data: Vec::new() }
    }
}

impl Default for MemSink {
    fn default() -> Self {
        Self::new()
    }
}

impl RandomAccessSink for MemSink {
    fn write_at(&mut self, offset: u64, data: &[u8]) -> CoreResult<()> {
        let off = usize::try_from(offset)
            .map_err(|_| CoreError::io(format!("offset {offset} > usize")))?;
        let end = off.checked_add(data.len()).ok_or_else(|| CoreError::io("length overflow"))?;
        if end > self.data.len() {
            self.data.resize(end, 0);
        }
        self.data[off..end].copy_from_slice(data);
        Ok(())
    }
    fn truncate(&mut self, size: u64) -> CoreResult<()> {
        let n = usize::try_from(size).map_err(|_| CoreError::io(format!("size {size} > usize")))?;
        if n > self.data.len() {
            self.data.resize(n, 0);
        }
        Ok(())
    }
    fn flush(&mut self) -> CoreResult<()> {
        Ok(())
    }
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let off = usize::try_from(offset).map_err(|_| CoreError::io("offset > usize"))?;
        let end = off.checked_add(len).ok_or_else(|| CoreError::io("len overflow"))?;
        if end > self.data.len() {
            return Err(CoreError::io("read beyond scratch"));
        }
        Ok(self.data[off..end].to_vec())
    }
}

/// Scratch factory over memory sinks (tests only; not for large inputs).
#[derive(Default)]
pub struct MemScratch {
    pub created: Vec<String>,
}

impl ScratchFactory for MemScratch {
    fn create(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>> {
        self.created.push(name.to_string());
        Ok(Box::new(MemSink::new()))
    }
}

// --------------------------------------------------------------------------- //
// File-backed impls (native CLI)
// --------------------------------------------------------------------------- //

pub struct FileSource {
    file: Mutex<File>,
    size: u64,
    path: PathBuf,
}

impl FileSource {
    pub fn open(path: &Path) -> CoreResult<Self> {
        let file = File::open(path)
            .map_err(|e| CoreError::io(format!("open {} 失败: {e}", path.display())))?;
        let size = file
            .metadata()
            .map_err(|e| CoreError::io(format!("metadata 失败: {e}")))?
            .len();
        Ok(FileSource { file: Mutex::new(file), size, path: path.to_path_buf() })
    }
}

impl ByteSource for FileSource {
    fn size(&self) -> u64 {
        self.size
    }
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let end = offset
            .checked_add(len as u64)
            .ok_or_else(|| CoreError::oob("read length overflow"))?;
        if end > self.size {
            return Err(CoreError::oob(format!(
                "read [{offset},+{len}) beyond file size {}",
                self.size
            )));
        }
        let mut f = self.file.lock().expect("FileSource mutex poisoned");
        f.seek(SeekFrom::Start(offset))
            .map_err(|e| CoreError::io(format!("seek 失败: {e}")))?;
        let mut buf = vec![0u8; len];
        f.read_exact(&mut buf)
            .map_err(|e| CoreError::io(format!("read 失败: {e}")))?;
        Ok(buf)
    }
    fn identity(&self) -> String {
        self.path.display().to_string()
    }
}

pub struct FileSink {
    file: Mutex<File>,
}

impl FileSink {
    pub fn create(path: &Path) -> CoreResult<Self> {
        let file = OpenOptions::new()
            .write(true)
            .read(true)
            .create(true)
            .truncate(true)
            .open(path)
            .map_err(|e| CoreError::io(format!("create {} 失败: {e}", path.display())))?;
        Ok(FileSink { file: Mutex::new(file) })
    }
    /// Open without truncating (resume path: committed bytes stay).
    pub fn open_preserve(path: &Path) -> CoreResult<Self> {
        let file = OpenOptions::new()
            .write(true)
            .read(true)
            .create(true)
            .truncate(false)
            .open(path)
            .map_err(|e| CoreError::io(format!("open {} 失败: {e}", path.display())))?;
        Ok(FileSink { file: Mutex::new(file) })
    }
}

impl RandomAccessSink for FileSink {
    fn write_at(&mut self, offset: u64, data: &[u8]) -> CoreResult<()> {
        let mut f = self.file.lock().expect("FileSink mutex poisoned");
        f.seek(SeekFrom::Start(offset))
            .map_err(|e| CoreError::io(format!("seek 失败: {e}")))?;
        f.write_all(data).map_err(|e| CoreError::io(format!("write 失败: {e}")))?;
        Ok(())
    }
    fn truncate(&mut self, size: u64) -> CoreResult<()> {
        let f = self.file.lock().expect("FileSink mutex poisoned");
        f.set_len(size).map_err(|e| CoreError::io(format!("set_len 失败: {e}")))?;
        Ok(())
    }
    fn flush(&mut self) -> CoreResult<()> {
        let f = self.file.lock().expect("FileSink mutex poisoned");
        f.sync_all().map_err(|e| CoreError::io(format!("fsync 失败: {e}")))?;
        Ok(())
    }
    fn read_at(&self, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let mut f = self.file.lock().expect("FileSink mutex poisoned");
        f.seek(SeekFrom::Start(offset))
            .map_err(|e| CoreError::io(format!("seek 失败: {e}")))?;
        let mut buf = vec![0u8; len];
        f.read_exact(&mut buf).map_err(|e| CoreError::io(format!("read 失败: {e}")))?;
        Ok(buf)
    }
}

/// Scratch factory writing real files under a scratch directory; files are
/// best-effort removed on drop (the CLI points this at the output directory
/// so scratch lives on the same filesystem as the destination).
pub struct FileScratch {
    pub dir: PathBuf,
    pub created: Vec<PathBuf>,
}

impl FileScratch {
    pub fn new(dir: &Path) -> Self {
        FileScratch { dir: dir.to_path_buf(), created: Vec::new() }
    }
}

impl ScratchFactory for FileScratch {
    fn create(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>> {
        let path = self.dir.join(format!(".kfb2tiff-scratch-{name}"));
        let sink = FileSink::create(&path)?;
        self.created.push(path);
        Ok(Box::new(sink))
    }
    fn create_preserve(&mut self, name: &str) -> CoreResult<Box<dyn ScratchSink>> {
        let path = self.dir.join(format!(".kfb2tiff-scratch-{name}"));
        let sink = FileSink::open_preserve(&path)?;
        self.created.push(path);
        Ok(Box::new(sink))
    }
}

impl Drop for FileScratch {
    fn drop(&mut self) {
        for p in &self.created {
            let _ = std::fs::remove_file(p);
        }
    }
}
