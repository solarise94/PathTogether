//! Multi-member random-access bundle source (F3: standard MRXS).
//!
//! A "bundle" is a fixed set of named members (`CMU-1.mrxs` +
//! `CMU-1/Slidedat.ini`, `Index.dat`, `Data0000.dat`…) — never one whole
//! file. Every access is `read_member_at(member, offset, len)` with explicit
//! lengths; no member is ever buffered in full. Implementations:
//!
//! - native CLI: [`DirBundle`] over a directory (the `.mrxs` entry plus the
//!   same-name directory's files, flattened to `name` / `dir/name`);
//! - wasm/browser: the callbacks in `crates/wasm` over OPFS members;
//! - tests: [`MemBundle`].
//!
//! Path-traversal safety is by construction: members are matched by their
//! exact flat name, never joined onto a filesystem path, so a hostile
//! `FILE_0 = ../evil.dat` in Slidedat.ini can only produce a typed
//! "missing member" error.

use crate::error::{CoreError, CoreResult};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// Upper bound on member count of one bundle (browsers cap the manifest
/// identically — see `static/tools/slide-transform/engine.js`).
pub const MAX_MEMBERS: usize = 8192;

/// One member of a bundle: flat, normalised name + size.
#[derive(Debug, Clone)]
pub struct MemberInfo {
    /// Flat name: `slide.mrxs`, `slide/Slidedat.ini`, `slide/Data0000.dat`.
    /// No `\`, no drive colon, no `..` segment (implementations reject).
    pub name: String,
    pub size: u64,
}

/// Read-only multi-member source with explicit-offset reads.
pub trait BundleFs {
    fn members(&self) -> &[MemberInfo];
    fn read_member_at(&self, member: usize, offset: u64, len: usize) -> CoreResult<Vec<u8>>;

    /// Index of the member with exactly this flat name.
    fn find(&self, name: &str) -> Option<usize> {
        self.members().iter().position(|m| m.name == name)
    }

    /// Read a whole *small* member (bounded by `cap`; used for Slidedat.ini
    /// only — data members are always accessed by explicit ranges).
    fn read_small_member(&self, member: usize, cap: usize) -> CoreResult<Vec<u8>> {
        let size = self.members()[member].size;
        if size > cap as u64 {
            return Err(CoreError::validation(format!(
                "成员 {} 大小 {} 超过允许的上限 {}",
                self.members()[member].name, size, cap
            )));
        }
        self.read_member_at(member, 0, size as usize)
    }
}

/// Reject a member name that is not a safe flat bundle path.
pub fn valid_member_name(name: &str) -> bool {
    if name.is_empty() || name.len() > 255 || name.contains('\\') || name.contains(':') {
        return false;
    }
    let mut depth = 0usize;
    for seg in name.split('/') {
        if seg.is_empty() || seg == "." || seg == ".." || seg.contains('\0') {
            return false;
        }
        depth += 1;
        if depth > 4 {
            return false;
        }
    }
    true
}

// --------------------------------------------------------------------------- //
// In-memory bundle (unit tests, fixtures)
// --------------------------------------------------------------------------- //

#[derive(Default)]
pub struct MemBundle {
    members: Vec<MemberInfo>,
    data: Vec<Vec<u8>>,
}

impl MemBundle {
    pub fn new() -> Self {
        MemBundle::default()
    }
    pub fn push(&mut self, name: &str, data: Vec<u8>) {
        assert!(valid_member_name(name), "fixture member {name}");
        self.members.push(MemberInfo { name: name.to_string(), size: data.len() as u64 });
        self.data.push(data);
    }
    pub fn remove(&mut self, name: &str) -> bool {
        match self.find(name) {
            Some(i) => {
                self.members.remove(i);
                self.data.remove(i);
                true
            }
            None => false,
        }
    }
}

impl BundleFs for MemBundle {
    fn members(&self) -> &[MemberInfo] {
        &self.members
    }
    fn read_member_at(&self, member: usize, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let d = self
            .data
            .get(member)
            .ok_or_else(|| CoreError::oob(format!("成员序号 {member} 不存在")))?;
        let off = usize::try_from(offset)
            .map_err(|_| CoreError::oob(format!("offset {offset} > usize")))?;
        let end = off
            .checked_add(len)
            .ok_or_else(|| CoreError::oob("read length overflow"))?;
        if end > d.len() {
            return Err(CoreError::oob(format!(
                "成员 {} 读取 [{offset},+{len}) 越界（大小 {}）",
                self.members[member].name,
                d.len()
            )));
        }
        Ok(d[off..end].to_vec())
    }
}

// --------------------------------------------------------------------------- //
// Directory-backed bundle (native CLI)
// --------------------------------------------------------------------------- //

struct DirMember {
    info: MemberInfo,
    path: PathBuf,
    file: Mutex<fs::File>,
}

pub struct DirBundle {
    members: Vec<DirMember>,
    infos: Vec<MemberInfo>,
    by_name: HashMap<String, usize>,
}

impl DirBundle {
    /// Open `<dir>/<stem>.mrxs` plus every regular file directly inside
    /// `<dir>/<stem>/`, flattened to `<stem>.mrxs` / `<stem>/<file>`.
    /// `stem` must not contain path separators.
    pub fn open(dir: &Path, stem: &str) -> CoreResult<Self> {
        if stem.is_empty()
            || stem.contains('/')
            || stem.contains('\\')
            || stem == "."
            || stem == ".."
        {
            return Err(CoreError::validation(format!("非法包名 {stem:?}")));
        }
        let mut members: Vec<DirMember> = Vec::new();
        let mut add = |name: String, path: PathBuf| -> CoreResult<()> {
            if !valid_member_name(&name) {
                return Err(CoreError::validation(format!("成员名非法：{name}")));
            }
            let meta = fs::metadata(&path)
                .map_err(|e| CoreError::io(format!("读取成员 {} 失败: {e}", name)))?;
            if !meta.is_file() {
                return Err(CoreError::validation(format!("成员 {} 不是普通文件", name)));
            }
            let file = fs::File::open(&path)
                .map_err(|e| CoreError::io(format!("打开成员 {} 失败: {e}", name)))?;
            members.push(DirMember { info: MemberInfo { name, size: meta.len() }, path, file: Mutex::new(file) });
            Ok(())
        };
        let entry = dir.join(format!("{stem}.mrxs"));
        if !entry.exists() {
            return Err(CoreError::validation(format!(
                "缺少主入口 {}（MRXS 需要同名目录）",
                entry.display()
            )));
        }
        add(format!("{stem}.mrxs"), entry)?;
        let inner = dir.join(stem);
        let rd = fs::read_dir(&inner)
            .map_err(|e| CoreError::io(format!("打开目录 {} 失败: {e}", inner.display())))?;
        let mut names: Vec<(String, PathBuf)> = Vec::new();
        for ent in rd {
            let ent = ent.map_err(|e| CoreError::io(format!("列目录失败: {e}")))?;
            let Ok(name) = ent.file_name().into_string() else {
                return Err(CoreError::validation("目录内含非 UTF-8 文件名"));
            };
            if name.starts_with('.') {
                continue; // editor/cruft
            }
            names.push((name, ent.path()));
        }
        names.sort();
        if names.len() + 1 > MAX_MEMBERS {
            return Err(CoreError::validation(format!(
                "成员数 {} 超过上限 {MAX_MEMBERS}",
                names.len() + 1
            )));
        }
        for (name, path) in names {
            add(format!("{stem}/{name}"), path)?;
        }
        let by_name = members
            .iter()
            .enumerate()
            .map(|(i, m)| (m.info.name.clone(), i))
            .collect();
        let infos = members.iter().map(|m| m.info.clone()).collect();
        Ok(DirBundle { members, infos, by_name })
    }
}

impl BundleFs for DirBundle {
    fn members(&self) -> &[MemberInfo] {
        &self.infos
    }
    fn read_member_at(&self, member: usize, offset: u64, len: usize) -> CoreResult<Vec<u8>> {
        let m = self
            .members
            .get(member)
            .ok_or_else(|| CoreError::oob(format!("成员序号 {member} 不存在")))?;
        let end = offset
            .checked_add(len as u64)
            .ok_or_else(|| CoreError::oob("read length overflow"))?;
        if end > m.info.size {
            return Err(CoreError::oob(format!(
                "成员 {} 读取 [{offset},+{len}) 越界（大小 {}）",
                m.info.name, m.info.size
            )));
        }
        use std::io::{Read, Seek, SeekFrom};
        let mut f = m.file.lock().expect("DirBundle mutex poisoned");
        f.seek(SeekFrom::Start(offset))
            .map_err(|e| CoreError::io(format!("成员 {} seek 失败: {e}", m.info.name)))?;
        let mut buf = vec![0u8; len];
        f.read_exact(&mut buf)
            .map_err(|e| CoreError::io(format!("成员 {} read 失败: {e}", m.info.name)))?;
        Ok(buf)
    }
    fn find(&self, name: &str) -> Option<usize> {
        self.by_name.get(name).copied()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn member_name_rules() {
        assert!(valid_member_name("a.mrxs"));
        assert!(valid_member_name("a/Slidedat.ini"));
        assert!(!valid_member_name(""));
        assert!(!valid_member_name("../evil.dat"));
        assert!(!valid_member_name("a/../b.dat"));
        assert!(!valid_member_name("a\\b.dat"));
        assert!(!valid_member_name("C:/x.dat"));
        assert!(!valid_member_name("a//b.dat"));
    }

    #[test]
    fn mem_bundle_basics() {
        let mut b = MemBundle::new();
        b.push("s/Slidedat.ini", b"[X]\n".to_vec());
        let i = b.find("s/Slidedat.ini").unwrap();
        assert_eq!(b.read_small_member(i, 1024).unwrap(), b"[X]\n");
        assert!(b.read_member_at(i, 5, 1).is_err()); // beyond
        assert_eq!(b.members().len(), 1);
    }
}
