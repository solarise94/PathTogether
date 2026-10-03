// tools-slides-bundle.js — MRXS 完整包（文件夹）输入的入口辅助（F3 页面接线）。
// 本模块刻意不触碰 DOM/i18n：目录 drop 遍历与路由判定在这里以纯/可注入
// 的形式实现，vitest（tests/js/tools-u2-page.test.ts）用假 entry 直接驱动；
// 页面（tools-slides.js）负责把结果交给 runner.prepareBundle 并显示文案。
//
// 合同（计划 §5.4 / F3 报告 §1.4）：
//  - 文件夹选择与目录 drop 都进入同一 prepareBundleSource →
//    runner.prepareBundle（缺成员 → 类型化信息，任何大复制之前拒绝）；
//  - 遍历有成员数上限，镜像 engine.MRXS_MAX_MEMBERS；
//  - readEntries 每批最多返回 100 项，必须循环到空批为止。
'use strict';

/// 拖入/选择的散文件里是否有 MRXS 成员（.mrxs 主入口或 .dat 数据文件）。
/// 只是提示：接受与否由 engine.planMrxBundle 决定（缺成员 → 类型化信息）。
export function looksLikeBundleMember(name) {
  return /\.(mrxs|dat)$/i.test(String(name || ''));
}

/// 散文件路由判定（纯函数）：
///   'empty'    没有文件
///   'bundle'   含 .mrxs/.dat → 交给 planner（单独/散装文件会得到缺失成员
///              信息；恰好组成完整包则接受——绝不静默丢弃）
///   'multiple' 多个普通文件 → 「一次只处理一个切片文件」
///   'single'   单个普通切片文件 → 单文件流程
export function bundleRoute(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return { route: 'empty', files };
  if (files.some((f) => looksLikeBundleMember(f && f.name))) {
    return { route: 'bundle', files };
  }
  if (files.length > 1) return { route: 'multiple', files };
  return { route: 'single', files, file: files[0] };
}

/// webkitRelativePath（'CMU-1/CMU-1.mrxs'）→ 文件夹名（'CMU-1'）；
/// 无目录层（散文件）→ null。
export function folderNameFromRelPath(relPath) {
  const segs = String(relPath || '').split('/');
  return segs.length > 1 && segs[0] ? segs[0] : null;
}

/// 把选择/遍历结果统一成 {name, relPath, file} 行（engine.sniffMrxBundle
/// 认这个形状，与 webkitRelativePath 的 File 等价）。纯函数。
export function bundleRows(files) {
  return Array.from(files || [])
    .map((f) => {
      if (!f) return null;
      if (typeof f === 'object' && typeof f.file !== 'undefined') {
        return { name: f.name, relPath: f.relPath || f.name, file: f.file };
      }
      return {
        name: f.name,
        relPath: f.webkitRelativePath || f.relPath || f.name,
        file: f,
      };
    })
    .filter((r) => r && r.file);
}

/// 目录 entry（FileSystemDirectoryEntry）→ 成员行。readEntries 每批 ≤100，
/// 循环到空批；成员总数超过 maxMembers 时抛错（镜像 engine 上限，复制前
/// 拒绝）。任何一步不可用（旧浏览器没有 createReader/readEntries、读取
/// 失败）都会 reject —— 页面据此提示改用「选择文件夹（MRXS）」按钮。
/// deps 可注入（测试用假 entry 驱动真实遍历逻辑）。
export async function collectEntryFiles(entry, { maxMembers = 8192 } = {}, deps = {}) {
  const fileOf = deps.fileOf || ((e) => new Promise((resolve, reject) => {
    if (typeof e.file !== 'function') {
      reject(new TypeError('entry.file unavailable'));
      return;
    }
    e.file(resolve, reject);
  }));
  const rows = [];
  await walkEntry(entry, '', rows, { maxMembers, fileOf });
  return rows;
}

async function walkEntry(entry, prefix, rows, caps) {
  if (!entry) return;
  if (entry.isFile) {
    const file = await caps.fileOf(entry);
    rows.push({
      name: entry.name,
      relPath: prefix ? `${prefix}/${entry.name}` : entry.name,
      file,
    });
    if (rows.length > caps.maxMembers) {
      const e = new Error(`目录成员数超过上限 ${caps.maxMembers}`);
      e.code = 'too_many_members';
      throw e;
    }
    return;
  }
  if (!entry.isDirectory) return;
  if (typeof entry.createReader !== 'function') {
    throw new TypeError('directory entry cannot be traversed (createReader unavailable)');
  }
  const reader = entry.createReader();
  if (!reader || typeof reader.readEntries !== 'function') {
    throw new TypeError('directory entry cannot be traversed (readEntries unavailable)');
  }
  const dirPath = prefix ? `${prefix}/${entry.name}` : entry.name;
  // readEntries 每次最多给 100 项：必须循环调用到空批才能拿全目录
  for (;;) {
    const batch = await new Promise((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (!batch || !batch.length) break;
    for (const e of batch) {
      await walkEntry(e, dirPath, rows, caps);
    }
  }
}
