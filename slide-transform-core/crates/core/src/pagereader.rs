//! Sequential page reader over a `ByteSource`: keeps one fixed-size page in
//! memory for streaming fixed-record index regions.

use crate::error::CoreResult;
use crate::io::ByteSource;

pub struct PageReader<'a> {
    src: &'a dyn ByteSource,
    page: Vec<u8>,
    page_start: u64,
}

impl<'a> PageReader<'a> {
    pub fn new(src: &'a dyn ByteSource) -> Self {
        PageReader { src, page: Vec::new(), page_start: 0 }
    }

    /// Whether `[off, off+len)` is already inside the current page.
    pub fn covers(&self, off: u64, len: u64) -> bool {
        let end = off + len as u64;
        !self.page.is_empty()
            && off >= self.page_start
            && end <= self.page_start + self.page.len() as u64
    }

    /// Make sure `[off, off+len)` is inside the current page, refilling the
    /// page (exactly covering the request, sized by the caller) if not.
    pub fn ensure(&mut self, off: u64, len: usize) -> CoreResult<()> {
        let end = off + len as u64;
        if off >= self.page_start
            && end <= self.page_start + self.page.len() as u64
            && !self.page.is_empty()
        {
            return Ok(());
        }
        self.page = self.src.read_at(off, len)?;
        self.page_start = off;
        Ok(())
    }

    /// Slice of `len` bytes at `off` (caller must have called `ensure` with
    /// a covering request).
    pub fn slice(&self, off: u64, len: usize) -> &[u8] {
        let b = (off - self.page_start) as usize;
        &self.page[b..b + len]
    }
}
