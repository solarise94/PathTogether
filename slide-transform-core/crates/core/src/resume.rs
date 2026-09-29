//! Checkpoint / resume contract (C2).
//!
//! A conversion emits [`crate::job::CheckpointState`] after every committed
//! tile row (data already handed to the sink; the host flushes and journals).
//! A [`ResumePoint`] captures one such state; `convert_*_resume` reconstructs
//! the writers from it (per-IFD committed tile counts + committed output
//! cursor, offset/count streams reopened from scratch) and continues, so the
//! final output is byte-identical to an uninterrupted run.
//!
//! Semantics of `ResumePoint`:
//! - `level` indexes the converter's own iteration order (brightfield: index
//!   into the *selected* levels; fluorescence: index into `doc.levels`).
//! - `channel` is 0 for brightfield and the channel index for fluorescence.
//! - `cell` is the first NOT-yet-committed cell of that (level, channel);
//!   cells `[0, cell)` are committed and must already exist in the output.
//! - `ifd_tiles[i]` is the committed tile count of the i-th begun IFD in
//!   begin order; `ifd_tiles.len()` IFDs have been begun. When `cell > 0` the
//!   last entry is the currently-partial IFD. When `cell == 0` the IFD for
//!   (level, channel) has NOT been begun (the host must not preserve-open a
//!   scratch file for it).
//! - `committed_output` is the sink cursor after the committed cells; the
//!   host must have truncated the output to exactly this length.

use crate::error::{CoreError, CoreResult};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResumePoint {
    pub level: usize,
    pub channel: usize,
    pub cell: u64,
    pub committed_output: u64,
    pub ifd_tiles: Vec<u64>,
}

impl ResumePoint {
    /// Validate internal consistency (host-supplied value must be sane
    /// before the writers trust it).
    pub fn validate(&self) -> CoreResult<()> {
        if self.ifd_tiles.is_empty() && (self.cell > 0 || self.level > 0 || self.channel > 0) {
            return Err(CoreError::validation(
                "resume: ifd_tiles 为空但进度非零（journal 损坏）",
            ));
        }
        for &t in &self.ifd_tiles {
            if t > u32::MAX as u64 {
                return Err(CoreError::validation("resume: 单 IFD tile 数超出 u32"));
            }
        }
        Ok(())
    }
}

/// Parse the wire form emitted by `stHostCheckpoint`:
/// `{"level":L,"channel":C,"cell":X,"out":N,"ifds":[a,b,...]}`
/// (`channel` may be `0`/absent for brightfield).
pub fn parse_resume_json(s: &str) -> CoreResult<ResumePoint> {
    let mut level = None;
    let mut channel = 0usize;
    let mut cell = None;
    let mut out = None;
    let mut ifds: Vec<u64> = Vec::new();

    let mut i = 0usize;
    let b = s.as_bytes();
    let skip_ws = |i: &mut usize, b: &[u8]| {
        while *i < b.len() && (b[*i] as char).is_whitespace() {
            *i += 1;
        }
    };
    let read_num = |i: &mut usize, b: &[u8]| -> Option<u64> {
        let start = *i;
        while *i < b.len() && b[*i].is_ascii_digit() {
            *i += 1;
        }
        if *i == start {
            return None;
        }
        std::str::from_utf8(&b[start..*i]).ok()?.parse().ok()
    };
    skip_ws(&mut i, b);
    if i >= b.len() || b[i] != b'{' {
        return Err(CoreError::validation("resume json: 非 object"));
    }
    i += 1;
    loop {
        skip_ws(&mut i, b);
        if i < b.len() && b[i] == b'}' {
            break;
        }
        // key
        if i >= b.len() || b[i] != b'"' {
            return Err(CoreError::validation("resume json: 缺 key"));
        }
        let ks = i + 1;
        let mut ke = ks;
        while ke < b.len() && b[ke] != b'"' {
            ke += 1;
        }
        if ke >= b.len() {
            return Err(CoreError::validation("resume json: key 未闭合"));
        }
        let key = std::str::from_utf8(&b[ks..ke])
            .map_err(|_| CoreError::validation("resume json: key 编码"))?;
        i = ke + 1;
        skip_ws(&mut i, b);
        if i >= b.len() || b[i] != b':' {
            return Err(CoreError::validation("resume json: 缺冒号"));
        }
        i += 1;
        skip_ws(&mut i, b);
        match key {
            "level" => level = read_num(&mut i, b).map(|v| v as usize),
            "channel" => channel = read_num(&mut i, b).unwrap_or(0) as usize,
            "cell" => cell = read_num(&mut i, b),
            "out" => out = read_num(&mut i, b),
            "ifds" => {
                if i >= b.len() || b[i] != b'[' {
                    return Err(CoreError::validation("resume json: ifds 非数组"));
                }
                i += 1;
                loop {
                    skip_ws(&mut i, b);
                    if i < b.len() && b[i] == b']' {
                        i += 1;
                        break;
                    }
                    let v = read_num(&mut i, b)
                        .ok_or_else(|| CoreError::validation("resume json: ifds 元素"))?;
                    ifds.push(v);
                    skip_ws(&mut i, b);
                    if i < b.len() && b[i] == b',' {
                        i += 1;
                    }
                }
            }
            _ => {
                // skip unknown scalar (string/number/bool/null)
                while i < b.len() && b[i] != b',' && b[i] != b'}' {
                    i += 1;
                }
            }
        }
        skip_ws(&mut i, b);
        if i < b.len() && b[i] == b',' {
            i += 1;
            continue;
        }
        if i < b.len() && b[i] == b'}' {
            break;
        }
        if i >= b.len() {
            return Err(CoreError::validation("resume json: 截断"));
        }
    }
    let level = level.ok_or_else(|| CoreError::validation("resume json: 缺 level"))?;
    let cell = cell.ok_or_else(|| CoreError::validation("resume json: 缺 cell"))?;
    let out = out.ok_or_else(|| CoreError::validation("resume json: 缺 out"))?;
    let rp = ResumePoint {
        level,
        channel,
        cell,
        committed_output: out,
        ifd_tiles: ifds,
    };
    rp.validate()?;
    Ok(rp)
}
