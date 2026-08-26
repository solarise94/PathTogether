# -*- coding: utf-8 -*-
"""U4 管理员离线导入：文件名校验、拒绝 zip/mrxs、dry-run、link 提升。"""
import sys
from pathlib import Path

import pytest

import app as app_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import import_slides as imp  # noqa: E402


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    from _pt_helpers import isolate_app
    import upload_guard
    isolate_app(monkeypatch, tmp_path, tmp_path / "uploads")
    monkeypatch.setattr(app_mod, "_validate_slide_file", lambda p: True)
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


def test_link_promote_and_name_conflict(tmp_path, monkeypatch):
    src_dir = tmp_path / "incoming"
    src_dir.mkdir()
    src = src_dir / "a.svs"
    src.write_bytes(b"slide-bytes")
    upload = tmp_path / "uploads"
    monkeypatch.setattr("share_store.set_slide_meta", lambda *a, **k: None)
    r = imp.run(src_dir, upload_dir=upload, move=False)
    assert r["ok"] == ["a.svs"]
    dest = upload / "a.svs"
    assert dest.is_file()
    assert dest.read_bytes() == b"slide-bytes"
    # 源仍在（未 --move）
    assert src.is_file()
    r2 = imp.run(src_dir, upload_dir=upload)
    assert r2["ok"] == []
    assert any("名称不可用" in f["error"] for f in r2["failed"])
