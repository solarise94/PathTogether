# -*- coding: utf-8 -*-
"""U4 管理员离线导入（P4-app 断言换新）：文件名校验、拒绝 zip/mrxs、
dry-run、**复制进受管理 staging → 统一发布**（不硬链接外部可写源；
--move 发布成功后删源；同名重复导入=独立新资产，无名称冲突面）。"""
import sys
from pathlib import Path

import pytest

import app as app_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import import_slides as imp  # noqa: E402


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    from _pt_helpers import isolate_app
    import share_store
    import upload_guard
    import user_store
    isolate_app(monkeypatch, tmp_path, tmp_path / "uploads")
    # P4-app：allocate_slide 需要非空 owner——本地态注入配置 owner。
    share_store.set_owner_user_id(
        user_store.create_user("imp-local-owner@x.com",
                               "implocalpass12345", role="user")["user_id"])
    # A0 异常契约：放行 stub 返回 None，签名兼容 format_hint 关键字
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p, **_: None)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    return tmp_path


def test_sanitize_and_dry_run(tmp_path):
    src = tmp_path / "incoming"
    src.mkdir()
    (src / "ok.svs").write_bytes(b"fake")
    (src / ".hidden.svs").write_bytes(b"x")
    upload = tmp_path / "uploads"
    r = imp.run(src, upload_dir=upload, dry_run=True)
    assert r["ok"] == ["ok.svs"]
    assert r["failed"] == []
    assert not (upload / "ok.svs").exists()


def test_rejects_zip_and_unknown_ext(tmp_path):
    src = tmp_path / "incoming"
    src.mkdir()
    (src / "bundle.zip").write_bytes(b"PK")
    (src / "notes.txt").write_bytes(b"x")
    (src / "slide.mrxs").write_bytes(b"x")
    upload = tmp_path / "uploads"
    r = imp.run(src, upload_dir=upload, dry_run=True)
    assert r["ok"] == []
    errs = {f["file"]: f["error"] for f in r["failed"]}
    assert "bundle.zip" in errs and "ZIP" in errs["bundle.zip"]
    assert "slide.mrxs" in errs
    assert "notes.txt" in errs


def test_copy_publish_move_and_reimport_independent(tmp_path, monkeypatch):
    """P4-app：复制进受管理 staging → 统一发布（不硬链接、不平铺
    UPLOAD_DIR）；--move 发布成功后删源；重复导入=独立新资产（无名称冲突）。"""
    import slide_storage
    import slide_store
    src_dir = tmp_path / "incoming"
    src_dir.mkdir()
    src = src_dir / "a.svs"
    src.write_bytes(b"slide-bytes")
    upload = tmp_path / "uploads"
    r = imp.run(src_dir, upload_dir=upload, move=False)
    assert r["ok"] == ["a.svs"]
    # 产物在 objects/<slide_id>/（不平铺、不与源共享 inode）
    rows = slide_store.list_ready_descriptors()
    assert len(rows) == 1
    sid_a = rows[0].slide_id
    dest = slide_storage.resolve_descriptor_path(rows[0], root=upload)
    assert dest.read_bytes() == b"slide-bytes"
    assert not (upload / "a.svs").exists()
    assert src.is_file()
    assert dest.stat().st_ino != src.stat().st_ino  # 复制而非硬链接
    # 无 --move 残留 staging 清理
    assert not (upload / ".staging").exists() or not any(
        (upload / ".staging").iterdir())
    # 重复导入：独立新资产（同名同内容不再构成冲突——各得各 ID）
    r2 = imp.run(src_dir, upload_dir=upload)
    assert r2["ok"] == ["a.svs"]
    rows2 = slide_store.list_ready_descriptors()
    assert {d.slide_id for d in rows2} == {sid_a, rows2[1].slide_id}
    # --move：发布成功后删源
    r3 = imp.run(src_dir, upload_dir=upload, move=True)
    assert r3["ok"] == ["a.svs"]
    assert not src.exists()
