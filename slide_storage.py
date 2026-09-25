# -*- coding: utf-8 -*-
"""slide_storage —— 切片资产的安全路径派生与包操作（slide ID 化重构 P1-A）。

本模块是「代码旁合同」：路径布局、containment 校验、no-clobber 发布语义
镜像自 docs/slide-id-refactor-p1-contract-20260925.md §3.2 与
docs/slide-id-storage-refactor-agent-plan-20260925.md §2.2/§3.1-5。
worker 可 import 本模块（不得 import app；本模块不依赖 app/PG——UPLOAD_DIR
根由 ``configure()`` 或 ``UPLOAD_DIR`` env 或函数参数提供，与
cos_ingest_worker.upload_dir 同款解析顺序）。

磁盘布局（plan §2.2）::

    UPLOAD_DIR/
      .staging/<task_id>/<generation>/...      # 任务专属，不提供静态访问
      objects/<slide_id>/data.svs              # 单文件示例
      objects/<slide_id>/bundle/...            # 多文件格式，入口见 manifest
      objects/<slide_id>/manifest.json
      <历史文件及伴侣目录>                      # 仅迁移过渡（legacy 布局，R-16）

不变量
  - 新物理目录全部由服务端 slide_id 派生，**无用户可控片段**；扩展名来自
    格式判定的白名单（``^[a-z0-9]{1,16}$`` 归一，与 slide_store 同口径）。
  - 不同资产绝不共享可写目标；不按 SHA 跨用户去重；同内容不代表同身份。
  - containment 校验（本模块所有相对路径统一执行）：解析后必须位于
    UPLOAD_DIR 内；拒绝 ``..``、绝对路径、符号链接逃逸（resolve 后比对根）。
    manifest 中的相对路径同样验证，不从展示名拼路径。
  - no-clobber：目标已存在即 FileExistsError，**绝不覆盖**；同一 slide_id
    不允许覆盖内容（换内容 = 新 slide_id，合同 §4）。
  - 耐久合同（plan §3.1-5）：按发布合同 fsync 文件与相关目录；正常同卷优先
    原子目录 rename；跨卷先目标卷私有暂存、完整复制校验后再发布，不直接
    复制到可用目标。
  - ``remove_bundle`` 仅清理该 ID 的独占目录；绝不按显示名扫描删除。

legacy 布局过渡分支（resolve_descriptor_path 的 legacy 支路）
  - 退役条件（R-16 / 合同 §8）：P6 历史资产物理迁移完成后，运行时 legacy
    读取分支移除——legacy resolver 仅在受限迁移工具/过渡版本保留；全部
    可服务资产迁入 ``objects/<slide_id>/`` 且验收通过后，本分支删除。
"""

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path

#: format_ext 白名单归一（与 slide_store.normalize_format_ext 同口径；本模块
#: 不 import slide_store，保持零依赖——单一来源是合同 §2.1 的白名单语义）。
_FORMAT_EXT_RE = re.compile(r"^[a-z0-9]{1,16}$")

#: ID/任务键等路径组件的白名单：服务端生成的 token 形态（sld_/upt_/inj_…）。
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

#: 模块级根（configure() 设置；None → env UPLOAD_DIR → ~/svs-viewer/uploads，
#: 与 app.py / cos_ingest_worker.upload_dir 同款解析）。
_CONFIGURED_ROOT: Path | None = None

_STAGING_DIRNAME = ".staging"
_OBJECTS_DIRNAME = "objects"
_MANIFEST_FILENAME = "manifest.json"
_ENTRY_BASENAME = "data"


# --------------------------------------------------------------------------- #
# 根目录解析（不从 app import；configure 或参数传根）
# --------------------------------------------------------------------------- #
def configure(upload_dir):
    """设置模块级 UPLOAD_DIR 根（app/worker 启动期调用一次）。

    测试用例可用它指向 tmp 目录；重复调用以最后一次为准。传 None 复位为
    env/默认解析。
    """
    global _CONFIGURED_ROOT
    _CONFIGURED_ROOT = Path(upload_dir) if upload_dir is not None else None


def upload_root() -> Path:
    """解析上传根：configure() > ``UPLOAD_DIR`` env > ``~/svs-viewer/uploads``。"""
    if _CONFIGURED_ROOT is not None:
        return _CONFIGURED_ROOT
    env = os.environ.get("UPLOAD_DIR")
    if env:
        return Path(env)
    return Path.home() / "svs-viewer" / "uploads"


def _root(root=None) -> Path:
    return Path(root) if root is not None else upload_root()


# --------------------------------------------------------------------------- #
# 组件与相对路径校验（containment 前置）
# --------------------------------------------------------------------------- #
def _safe_component(value, what: str) -> str:
    """路径组件白名单：服务端生成的 ID/任务键形态。

    拒绝空串、``.``/``..``、分隔符（``/`` ``\\``）、控制字符与超长（>128）。
    """
    if not isinstance(value, str):
        value = str(value)
    if (not value or value in (".", "..") or "/" in value or "\\" in value
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
            or not _COMPONENT_RE.match(value)):
        raise ValueError("非法路径组件（%s）：%r" % (what, value))
    return value


def check_relpath(rel, *, what: str = "相对路径") -> str:
    """校验 bundle/manifest 内相对路径：拒绝绝对路径与 ``..``/``.``/空段。

    同时拒绝 Windows 盘符与反斜杠分隔，防止跨平台形态绕过。
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("非法%s：空值" % what)
    r = rel.strip()
    if r.startswith("/") or r.startswith("\\") or os.path.isabs(r) or \
            re.match(r"^[A-Za-z]:", r):
        raise ValueError("非法%s（绝对路径）：%r" % (what, rel))
    if "\\" in r:
        # 服务端生成的路径恒为 POSIX 分隔；反斜杠一律视为可疑形态拒绝
        #（不静默归一——避免 Windows 形态绕过审计）
        raise ValueError("非法%s（反斜杠）：%r" % (what, rel))
    segments = r.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise ValueError("非法%s（穿越/空段）：%r" % (what, rel))
    return "/".join(segments)


def _resolve_within(root: Path, rel: str, *, what: str) -> Path:
    """root + rel 的绝对路径，执行统一 containment 校验。

    拒绝 ``..``/绝对路径（静态检查）与符号链接逃逸（resolve 后必须仍在
    root 的 resolve 结果之内；strict=False——目标文件可尚未存在）。
    """
    clean = check_relpath(rel, what=what)
    root_resolved = root.resolve(strict=False)
    candidate = (root / clean).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise ValueError("%s 逃逸上传根（拒绝）：%r" % (what, rel)) from None
    return candidate


# --------------------------------------------------------------------------- #
# 路径派生（全部服务端侧，无用户可控片段；合同 §3.2）
# --------------------------------------------------------------------------- #
def staging_dir(task_id, generation, *, root=None) -> Path:
    """任务专属暂存目录：``UPLOAD_DIR/.staging/<task_id>/<generation>/``。

    不提供静态访问；task_id/generation 均为服务端生成的组件（白名单校验）。
    generation 是崩溃恢复/重试的代次（int 或安全字符串）。
    """
    task = _safe_component(task_id, "task_id")
    gen = _safe_component(generation, "generation")
    return _root(root) / _STAGING_DIRNAME / task / gen


def bundle_dir(slide_id, *, root=None) -> Path:
    """资产独占包目录：``UPLOAD_DIR/objects/<slide_id>/``。"""
    sid = _safe_component(slide_id, "slide_id")
    return _root(root) / _OBJECTS_DIRNAME / sid


def entry_relpath(slide_id, format_ext) -> str:
    """包入口相对路径（相对 UPLOAD_DIR，POSIX 分隔）：``objects/<slide_id>/data.<ext>``。

    唯一且不可变（R-20）：slide_id 白名单组件 + format_ext 白名单小写扩展名
    ——无任何用户可控片段；多文件包的入口由 manifest 指定（P4 扩展，仍位于
    objects/<slide_id>/ 之内）。slide_store.allocate_slide 用本函数生成
    slides.storage_relpath（单一来源）。
    """
    sid = _safe_component(slide_id, "slide_id")
    ext = format_ext if isinstance(format_ext, str) else ""
    ext = ext.strip().lower().lstrip(".")
    if not _FORMAT_EXT_RE.match(ext):
        raise ValueError("format_ext 非白名单小写扩展名：%r" % (format_ext,))
    return "%s/%s/%s.%s" % (_OBJECTS_DIRNAME, sid, _ENTRY_BASENAME, ext)


def resolve_descriptor_path(desc, *, root=None) -> Path:
    """descriptor → 入口文件绝对路径（统一 containment 校验）。

    - id_bundle：``UPLOAD_DIR / storage_relpath``（objects/<slide_id>/data.<ext>）。
    - legacy：``UPLOAD_DIR / legacy_filename`` —— **仅过渡分支**（退役条件见
      模块 docstring：P6 历史资产物理迁移完成并验收后移除；legacy 布局只允许
      受限迁移工具/过渡版本读取）。legacy_filename 缺失（新资产恒 NULL）即
      ValueError——新资产不存在按名定位的路径。
    - 其他 layout 值 ValueError（fail-closed，不猜）。
    """
    layout = getattr(desc, "storage_layout", None)
    base = _root(root)
    if layout == "id_bundle":
        rel = getattr(desc, "storage_relpath", None)
        if not rel:
            raise ValueError("id_bundle 资产缺少 storage_relpath")
        return _resolve_within(base, rel, what="storage_relpath")
    if layout == "legacy":
        rel = getattr(desc, "legacy_filename", None)
        if not rel:
            raise ValueError("legacy 资产缺少 legacy_filename（新资产不走 legacy 布局）")
        return _resolve_within(base, rel, what="legacy_filename")
    raise ValueError("未知 storage_layout：%r" % (layout,))


# --------------------------------------------------------------------------- #
# manifest 校验
# --------------------------------------------------------------------------- #
def validate_manifest(manifest) -> dict:
    """校验 manifest 形态与全部相对路径的 containment（静态层）。

    形态（P1 单文件包；P4 多文件包沿用同规则）::

        {
          "entry": "data.svs",            # bundle 内入口相对路径（必填）
          "files": [                       # 逐文件校验清单（可选，发布前/复制后核对）
              {"path": "data.svs", "size": 123, "sha256": "<hex>"},
          ],
        }

    entry 与 files[].path 均按 bundle 内相对路径校验（拒绝绝对路径/../
    空段）；不在此做存在性/大小核对——那是 publish 的职责（源与副本两侧
    各核一次）。返回归一后的 manifest dict（浅拷贝，path 归一为 POSIX）。
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest 必须是 dict")
    entry = check_relpath(manifest.get("entry"), what="manifest.entry")
    files = manifest.get("files") or []
    if not isinstance(files, list):
        raise ValueError("manifest.files 必须是数组")
    norm_files = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("manifest.files[] 必须是对象")
        path = check_relpath(item.get("path"), what="manifest.files[].path")
        norm_files.append({
            "path": path,
            "size": item.get("size"),
            "sha256": item.get("sha256"),
        })
    out = dict(manifest)
    out["entry"] = entry
    out["files"] = norm_files
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_file_against(base: Path, item: dict):
    """按 manifest 条目核对 base 下的文件（存在性/size/sha256）。"""
    path = base / item["path"]
    if not path.is_file():
        raise ValueError("包文件缺失：%s" % item["path"])
    if item.get("size") is not None:
        actual = path.stat().st_size
        if actual != int(item["size"]):
            raise ValueError("包文件大小不符：%s（%d != %s）"
                             % (item["path"], actual, item["size"]))
    if item.get("sha256"):
        actual = _sha256_file(path)
        if actual != str(item["sha256"]).lower():
            raise ValueError("包文件 sha256 不符：%s" % item["path"])


# --------------------------------------------------------------------------- #
# fsync（耐久合同：fsync 文件与相关目录）
# --------------------------------------------------------------------------- #
def _fsync_fd_dir(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(directory: Path):
    """递归 fsync 目录树内全部文件与目录（先文件后目录，自底向上）。"""
    for cur, _dirs, files in os.walk(directory, topdown=False):
        cur_path = Path(cur)
        for name in files:
            _fsync_file(cur_path / name)
        _fsync_fd_dir(cur_path)


# --------------------------------------------------------------------------- #
# no-clobber 发布（合同 §3.2；plan §3.1-5）
# --------------------------------------------------------------------------- #
def publish_bundle_no_clobber(staging, slide_id, manifest, *, root=None) -> Path:
    """把完整 staging 包发布为 ``objects/<slide_id>/`` 独占目录（no-clobber）。

    步骤（plan §3.1-5 的文件系统部分；DB 侧 CAS+结算由 slide_publish 编排）：
      1. 校验 manifest 结构（entry/files 相对路径 containment）；目标
         ``objects/<slide_id>`` 已存在 → **FileExistsError（绝不覆盖）**。
      2. 源侧核对 staging 内逐文件 size/sha256；把权威 manifest 写入
         ``manifest.json``。
      3. fsync 包内全部文件与 staging 目录（耐久合同）。
      4. 同卷（st_dev 相同）：优先原子目录 rename（os.rename；并发竞争下
         EEXIST/ENOTEMPTY 亦转 FileExistsError）。
      5. 跨卷（EXDEV）：目标卷私有暂存 ``objects/.publish-<slide_id>-<rand>``
         （隐藏名，与目标同卷同父目录，最后一步同目录 rename）→ 完整复制 →
         逐文件 size/sha256 复核 → fsync → rename 到目标。任一步失败清理
         私有暂存（best-effort），不留半成品在目标位。
      6. fsync ``objects/`` 与上传根目录。

    发布后 staging 目录不再存在（同卷被 rename 移走；跨卷由调用方按任务
    staging 生命周期清理）。返回最终 bundle 目录绝对路径。
    """
    base = _root(root)
    staging_path = Path(staging)
    if not staging_path.is_dir():
        raise ValueError("staging 不是目录：%s" % staging_path)
    norm_manifest = validate_manifest(manifest)
    target = bundle_dir(slide_id, root=base)

    # 0. no-clobber 前置检查（rename 是原子守卫，这里先给出明确错误信号）
    if os.path.lexists(target):
        raise FileExistsError("目标包已存在（no-clobber）：%s" % target)

    # 1. 源侧核对 + 写权威 manifest
    for item in norm_manifest["files"]:
        _verify_file_against(staging_path, item)
    entry_abs = staging_path / norm_manifest["entry"]
    if not entry_abs.is_file():
        raise ValueError("manifest.entry 在 staging 内缺失：%s"
                         % norm_manifest["entry"])
    manifest_path = staging_path / _MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(norm_manifest, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")

    # 2. 耐久：fsync 文件与目录
    _fsync_tree(staging_path)

    target.parent.mkdir(parents=True, exist_ok=True)

    # 4/5. 同卷原子 rename；跨卷走目标卷私有暂存+完整复制校验
    try:
        os.rename(staging_path, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise FileExistsError(
                    "目标包已存在（no-clobber）：%s" % target) from exc
            raise
        _publish_cross_volume(staging_path, target, norm_manifest)

    # 6. 目录耐久
    _fsync_fd_dir(target.parent)
    _fsync_fd_dir(base)
    return target


def _publish_cross_volume(staging_path: Path, target: Path, manifest: dict):
    """跨卷发布：目标卷私有暂存 → 完整复制校验 → 同卷 rename（§3.2）。

    私有暂存是目标父目录（objects/）下的隐藏名 ``.publish-<slide_id>-<rand>``
    ——不直接复制到可用目标，半成品只存在于私有暂存名下，失败即清理。
    """
    temp = target.parent / (
        ".publish-%s-%s" % (_safe_component(target.name, "slide_id"),
                            secrets.token_urlsafe(6)))
    try:
        shutil.copytree(staging_path, temp)
        for item in manifest["files"]:
            _verify_file_against(temp, item)
        if not (temp / manifest["entry"]).is_file():
            raise ValueError("跨卷复制后 manifest.entry 缺失")
        _fsync_tree(temp)
        try:
            os.rename(temp, target)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise FileExistsError(
                    "目标包已存在（no-clobber）：%s" % target) from exc
            raise
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- #
# 删除（合同 §3.2：仅清理该 ID 的独占目录）
# --------------------------------------------------------------------------- #
def remove_bundle(slide_id, *, root=None) -> bool:
    """删除 ``objects/<slide_id>/`` 独占目录；返回是否实际删除。

    **绝不按显示名扫描删除**——只认服务端 slide_id 派生的独占目录；目录
    不存在返回 False（幂等）。调用方（P5 删除编排）负责先 CAS 置 deleting
    并在清理确认后结算（合同 §4/R-12）。
    """
    target = bundle_dir(slide_id, root=root)
    if not os.path.lexists(target):
        return False
    if target.is_symlink() or not target.is_dir():
        # 独占目录位被非目录占用：不变量破坏，fail-closed 拒绝盲删
        raise ValueError("包路径不是目录（拒绝删除）：%s" % target)
    shutil.rmtree(target)
    if target.parent.is_dir():
        _fsync_fd_dir(target.parent)
    return True
