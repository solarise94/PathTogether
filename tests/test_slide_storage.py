# -*- coding: utf-8 -*-
"""slide_storage（安全路径/包操作）测试 —— slide ID 化重构 P1-A（tmp_path，无 PG）。

对齐 docs/slide-id-refactor-p1-contract-20260925.md §3.2：
  - 路径派生全部服务端侧、无用户可控片段；
  - containment 校验拒绝 ../绝对路径/符号链接逃逸（含 manifest 相对路径）；
  - publish no-clobber（目标已存在抛 FileExistsError，绝不覆盖）；
  - 跨卷：模拟 EXDEV 强制走「目标卷私有暂存+完整复制校验+rename」分支；
  - remove_bundle 只删该 slide_id 的独占目录。

运行：.venv/bin/python -m pytest tests/test_slide_storage.py -q
"""
import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

import slide_storage


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    """每个用例独立上传根；结束后复位模块配置，避免泄漏到其他测试模块。"""
    slide_storage.configure(tmp_path)
    monkeypatch.delenv("UPLOAD_DIR", raising=False)
    yield tmp_path
    slide_storage.configure(None)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _make_staging(root, task="upt_t1", gen=1, payload=b"slide-bytes",
                  entry="data.svs", with_files=True):
    staging = slide_storage.staging_dir(task, gen, root=root)
    staging.mkdir(parents=True, exist_ok=True)
    entry_path = staging / entry
    entry_path.write_bytes(payload)
    manifest = {"entry": entry}
    if with_files:
        manifest["files"] = [{
            "path": entry,
            "size": len(payload),
            "sha256": _sha(payload),
        }]
    return staging, manifest


# --------------------------------------------------------------------------- #
# 路径派生（无用户片段）
# --------------------------------------------------------------------------- #
def test_path_derivation_server_side(root):
    assert slide_storage.staging_dir("upt_1", 2, root=root) == \
        root / ".staging" / "upt_1" / "2"
    assert slide_storage.bundle_dir("sld_abc", root=root) == \
        root / "objects" / "sld_abc"
    assert slide_storage.entry_relpath("sld_abc", "svs") == \
        "objects/sld_abc/data.svs"
    # format_ext 归一（大写带点 → 白名单小写）
    assert slide_storage.entry_relpath("sld_abc", ".SVS") == \
        "objects/sld_abc/data.svs"


def test_component_and_ext_validation(root):
    with pytest.raises(ValueError):
        slide_storage.staging_dir("a/b", 1, root=root)
    with pytest.raises(ValueError):
        slide_storage.staging_dir("..", 1, root=root)
    with pytest.raises(ValueError):
        slide_storage.staging_dir("", 1, root=root)
    with pytest.raises(ValueError):
        slide_storage.staging_dir("a\\b", 1, root=root)
    with pytest.raises(ValueError):
        slide_storage.bundle_dir("../evil", root=root)
    with pytest.raises(ValueError):
        slide_storage.entry_relpath("sld_a", "sv/s")
    with pytest.raises(ValueError):
        slide_storage.entry_relpath("../sld_a", "svs")
    with pytest.raises(ValueError):
        slide_storage.entry_relpath("sld_a", "")


# --------------------------------------------------------------------------- #
# resolve_descriptor_path + containment
# --------------------------------------------------------------------------- #
class _Desc:
    def __init__(self, **kw):
        self.storage_layout = kw.get("storage_layout")
        self.storage_relpath = kw.get("storage_relpath")
        self.legacy_filename = kw.get("legacy_filename")


def test_resolve_id_bundle_path(root):
    p = slide_storage.resolve_descriptor_path(
        _Desc(storage_layout="id_bundle",
              storage_relpath="objects/sld_x/data.svs"), root=root)
    assert p == root / "objects" / "sld_x" / "data.svs"


def test_resolve_rejects_escape(root):
    for bad in ("../outside.svs", "/abs/path.svs", "objects/../../x.svs",
                "objects//x.svs", "a\\b.svs", "C:/x.svs"):
        with pytest.raises(ValueError):
            slide_storage.resolve_descriptor_path(
                _Desc(storage_layout="id_bundle", storage_relpath=bad),
                root=root)


def test_resolve_legacy_transitional_branch(root):
    """P6 运行时退役断言换目标：legacy 布局的物理解析只在**迁移专用入口**
    （resolve_legacy_path_for_migration）保留；运行时 resolver（resolve_
    descriptor_path）对 legacy 布局一律 ValueError（读路径只认 id_bundle）。"""
    legacy = root / "legacy-old.svs"
    legacy.write_bytes(b"old")
    # 运行时 resolver：legacy 布局 fail-closed（不再有平铺物理读取旁路）
    with pytest.raises(ValueError, match="legacy 布局已退役"):
        slide_storage.resolve_descriptor_path(
            _Desc(storage_layout="legacy", legacy_filename="legacy-old.svs"),
            root=root)
    # 迁移专用入口：同一 legacy 布局仍可解析（containment 校验同源）
    p = slide_storage.resolve_legacy_path_for_migration(
        _Desc(storage_layout="legacy", legacy_filename="legacy-old.svs"),
        root=root)
    assert p == legacy
    # 迁移入口只认 legacy 布局（id_bundle/未知一律拒——不提供第二运行时通道）
    with pytest.raises(ValueError):
        slide_storage.resolve_legacy_path_for_migration(
            _Desc(storage_layout="id_bundle",
                  storage_relpath="objects/sld_x/data.svs"), root=root)
    # 迁移入口 containment：穿越 legacy_filename 拒绝
    with pytest.raises(ValueError):
        slide_storage.resolve_legacy_path_for_migration(
            _Desc(storage_layout="legacy", legacy_filename="../evil.svs"),
            root=root)
    # legacy 布局缺 legacy_filename：拒绝（新资产不走 legacy 布局）
    with pytest.raises(ValueError):
        slide_storage.resolve_legacy_path_for_migration(
            _Desc(storage_layout="legacy"), root=root)
    # 未知布局 fail-closed
    with pytest.raises(ValueError):
        slide_storage.resolve_descriptor_path(_Desc(storage_layout="weird"),
                                              root=root)
    # id_bundle 缺 storage_relpath
    with pytest.raises(ValueError):
        slide_storage.resolve_descriptor_path(_Desc(storage_layout="id_bundle"),
                                              root=root)


def test_resolve_rejects_symlink_escape(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "data.svs").write_bytes(b"x")
    objects = root / "objects"
    objects.mkdir()
    os.symlink(outside, objects / "sld_escaped")
    with pytest.raises(ValueError):
        slide_storage.resolve_descriptor_path(
            _Desc(storage_layout="id_bundle",
                  storage_relpath="objects/sld_escaped/data.svs"), root=root)


# --------------------------------------------------------------------------- #
# publish no-clobber
# --------------------------------------------------------------------------- #
def test_publish_no_clobber(root):
    staging, manifest = _make_staging(root, payload=b"hello-slide")
    target = slide_storage.publish_bundle_no_clobber(
        staging, "sld_p1", manifest, root=root)
    assert target == root / "objects" / "sld_p1"
    assert (target / "data.svs").read_bytes() == b"hello-slide"
    assert json.loads((target / "manifest.json").read_text("utf-8"))["entry"] \
        == "data.svs"
    assert not staging.exists()               # 同卷：staging 被原子 rename 移走
    # 二次发布（新 staging，manifest 与其内容一致）：目标已存在 →
    # FileExistsError，绝不覆盖
    staging2, manifest2 = _make_staging(root, task="upt_t2", gen=1,
                                        payload=b"other-content")
    with pytest.raises(FileExistsError):
        slide_storage.publish_bundle_no_clobber(staging2, "sld_p1", manifest2,
                                                root=root)
    assert (target / "data.svs").read_bytes() == b"hello-slide"   # 原内容未动


def test_publish_manifest_validation(root):
    payload = b"abc"
    # entry 穿越/绝对路径
    for bad_entry in ("../evil.svs", "/etc/passwd", "..\\evil.svs"):
        staging, _ = _make_staging(root, task="upt_m1", payload=payload)
        with pytest.raises(ValueError):
            slide_storage.publish_bundle_no_clobber(
                staging, "sld_m", {"entry": bad_entry}, root=root)
    # files[].path 穿越
    staging, _ = _make_staging(root, task="upt_m2", payload=payload)
    with pytest.raises(ValueError):
        slide_storage.publish_bundle_no_clobber(
            staging, "sld_m",
            {"entry": "data.svs",
             "files": [{"path": "../evil.svs", "size": 3}]}, root=root)
    # size/sha 不符（发布前源侧核对失败）
    staging, _ = _make_staging(root, task="upt_m3", payload=payload)
    with pytest.raises(ValueError):
        slide_storage.publish_bundle_no_clobber(
            staging, "sld_m",
            {"entry": "data.svs",
             "files": [{"path": "data.svs", "size": 999}]}, root=root)
    staging, _ = _make_staging(root, task="upt_m4", payload=payload)
    with pytest.raises(ValueError):
        slide_storage.publish_bundle_no_clobber(
            staging, "sld_m",
            {"entry": "data.svs",
             "files": [{"path": "data.svs", "sha256": _sha(b"not-it")}]},
            root=root)
    # entry 文件缺失
    staging, _ = _make_staging(root, task="upt_m5", payload=payload)
    with pytest.raises(ValueError):
        slide_storage.publish_bundle_no_clobber(
            staging, "sld_m", {"entry": "missing.svs"}, root=root)
    # staging 不是目录
    plain = root / "plain.txt"
    plain.write_bytes(b"x")
    with pytest.raises(ValueError):
        slide_storage.publish_bundle_no_clobber(plain, "sld_m",
                                                {"entry": "data.svs"},
                                                root=root)


def test_publish_companion_files_whole_bundle(root):
    """完整包发布：入口+伴侣文件一起进 objects/<id>/。"""
    staging = slide_storage.staging_dir("upt_multi", 1, root=root)
    (staging / "bundle").mkdir(parents=True)
    (staging / "bundle" / "slide.mrxs").write_bytes(b"main")
    (staging / "bundle" / "conf.dat").write_bytes(b"companion")
    manifest = {
        "entry": "bundle/slide.mrxs",
        "files": [
            {"path": "bundle/slide.mrxs", "size": 4,
             "sha256": _sha(b"main")},
            {"path": "bundle/conf.dat", "size": 9,
             "sha256": _sha(b"companion")},
        ],
    }
    target = slide_storage.publish_bundle_no_clobber(
        staging, "sld_multi", manifest, root=root)
    assert (target / "bundle" / "slide.mrxs").read_bytes() == b"main"
    assert (target / "bundle" / "conf.dat").read_bytes() == b"companion"


# --------------------------------------------------------------------------- #
# 跨卷：模拟 EXDEV 强制走复制分支
# --------------------------------------------------------------------------- #
def test_publish_cross_volume_copy_branch(root, monkeypatch):
    staging, manifest = _make_staging(root, task="upt_x1", payload=b"xvol")
    real_rename = os.rename

    def fake_rename(src, dst, *a, **kw):
        if Path(src) == staging:
            raise OSError(errno.EXDEV, "simulated cross-device link")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", fake_rename)
    target = slide_storage.publish_bundle_no_clobber(
        staging, "sld_x1", manifest, root=root)
    assert (target / "data.svs").read_bytes() == b"xvol"
    assert json.loads((target / "manifest.json").read_text("utf-8"))["entry"] \
        == "data.svs"
    # 跨卷分支不消费 staging（由任务侧按 staging 生命周期清理）
    assert staging.is_dir() and (staging / "data.svs").read_bytes() == b"xvol"
    # 私有暂存不留半成品
    leftovers = [p for p in (root / "objects").iterdir()
                 if p.name.startswith(".publish-")]
    assert leftovers == []


def test_publish_cross_volume_verify_failure_cleans_temp(root, monkeypatch):
    """跨卷复核失败：源侧核对通过、复制体被篡改 → 清私有暂存、不留目标。"""
    import shutil as _shutil
    payload = b"will-fail"
    staging, manifest = _make_staging(root, task="upt_x2", payload=payload)
    real_rename = os.rename
    real_copytree = _shutil.copytree

    def fake_rename(src, dst, *a, **kw):
        if Path(src) == staging:
            raise OSError(errno.EXDEV, "simulated cross-device link")
        return real_rename(src, dst, *a, **kw)

    def fake_copytree(src, dst, *a, **kw):
        real_copytree(src, dst, *a, **kw)
        # 复制后篡改：源侧核对已过，私有暂存内复核必须抓住
        (Path(dst) / "data.svs").write_bytes(b"tampered")

    monkeypatch.setattr(os, "rename", fake_rename)
    monkeypatch.setattr(_shutil, "copytree", fake_copytree)
    with pytest.raises(ValueError):
        slide_storage.publish_bundle_no_clobber(staging, "sld_x2", manifest,
                                                root=root)
    assert not (root / "objects" / "sld_x2").exists()
    leftovers = [p for p in (root / "objects").iterdir()
                 if p.name.startswith(".publish-")]
    assert leftovers == []


# --------------------------------------------------------------------------- #
# remove_bundle：只删该 ID 的独占目录
# --------------------------------------------------------------------------- #
def test_remove_bundle_only_target_id(root):
    for sid, task in (("sld_r1", "upt_r1"), ("sld_r2", "upt_r2")):
        staging, manifest = _make_staging(root, task=task,
                                          payload=("data-" + sid).encode())
        slide_storage.publish_bundle_no_clobber(staging, sid, manifest,
                                                root=root)
    legacy_file = root / "legacy-root.svs"
    legacy_file.write_bytes(b"legacy-neighbor")
    assert slide_storage.remove_bundle("sld_r1", root=root) is True
    assert not (root / "objects" / "sld_r1").exists()
    assert (root / "objects" / "sld_r2" / "data.svs").is_file()   # 邻居完好
    assert legacy_file.is_file()                                   # 根下文件不动
    assert slide_storage.remove_bundle("sld_r1", root=root) is False  # 幂等
    assert slide_storage.remove_bundle("sld_missing", root=root) is False


# --------------------------------------------------------------------------- #
# 根解析：configure > env > 默认
# --------------------------------------------------------------------------- #
def test_root_resolution(tmp_path, monkeypatch):
    slide_storage.configure(tmp_path / "cfg")
    assert slide_storage.upload_root() == tmp_path / "cfg"
    slide_storage.configure(None)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "env"))
    assert slide_storage.upload_root() == tmp_path / "env"
