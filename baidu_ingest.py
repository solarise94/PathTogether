# -*- coding: utf-8 -*-
"""百度导入条目的真实校验/转换/入库（W5 B06）。

不 import Flask app。上传目录取 ``UPLOAD_DIR``；转换复用
``conversion_store`` + ``conversion_worker.process_job``；归属与项目关联
走 ``share_store``。ingest_token 是幂等凭证：已有 token 的条目不得再拷贝
或再建切片。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import conversion_store
import conversion_worker
import share_store
import slide_format_registry
import slide_io
import user_store


class IngestError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def _upload_dir():
    d = os.environ.get("UPLOAD_DIR")
    if not d:
        raise IngestError("upload_dir_missing", "UPLOAD_DIR 未配置")
    Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d)


def _safe_basename(name):
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base or "\x00" in base or base in (".", "..") or "/" in base:
        raise IngestError("invalid_name", "非法文件名")
    return base


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_name_for(source_safe):
    info = slide_format_registry.lookup(source_safe)
    ext = info.get("canonical_ext")
    if not ext:
        return source_safe
    if not str(ext).startswith("."):
        ext = "." + ext
    stem = source_safe.rsplit(".", 1)[0]
    return stem + ext


def associate_slide(owner_user_id, project_id, slide_name):
    """幂等加入项目。项目缺失/非本人/归档 → failed（产物不回滚）。"""
    if not project_id:
        return "not_needed"
    proj = share_store.get_project(project_id)
    if not proj:
        return "failed"
    if (proj.get("owner_user_id") or "") != (owner_user_id or ""):
        return "failed"
    if proj.get("archived"):
        return "failed"
    slides = proj.get("slides") or []
    if slide_name in slides:
        return "succeeded"
    out = share_store.add_slides_to_project(project_id, [slide_name])
    return "succeeded" if out is not None else "failed"


def _probe_convert(path):
    from kfb import KFBF_MAGIC, KfbError, parse_kfb, parse_kfbf
    try:
        with open(path, "rb") as fh:
            magic = fh.read(8)
    except OSError as e:
        raise IngestError("invalid_slide", "无法读取暂存文件") from e
    try:
        if magic == bytes(KFBF_MAGIC):
            doc = parse_kfbf(path)
            try:
                return "kfbf_kfbio_jpeg"
            finally:
                doc.close()
        doc = parse_kfb(path)
        try:
            return ("kfb_kfbio_jpeg" if doc.header.version != 1 else "kfb_bf_v1")
        finally:
            doc.close()
    except KfbError as e:
        raise IngestError(e.code or "invalid_kfb_header", str(e)) from e


def _probe_native(path):
    try:
        slide = slide_io.open_slide(str(path))
    except Exception as e:
        raise IngestError("invalid_slide", type(e).__name__) from e
    try:
        if getattr(slide, "level_count", 0) < 1:
            raise IngestError("invalid_slide", "无金字塔层")
    finally:
        slide.close()


def ingest_staging(*, owner_user_id, original_name, staging_path,
                   source_sha256, source_size, target_project_id=None):
    """把已下载的暂存文件收口进工作区。返回 dict。

    调用方负责：暂存文件已按 source_size 校验；本函数再核 SHA、格式、
    名称占用，然后 native 直接入库或 convert-required 走转换 worker。
    """
    name = _safe_basename(original_name)
    staging = Path(staging_path)
    if not staging.is_file():
        raise IngestError("download_output_missing", "暂存文件缺失")
    size = staging.stat().st_size
    if int(source_size or 0) and size != int(source_size):
        raise IngestError("size_mismatch", "下载大小与枚举声明不一致")
    digest = _sha256_file(staging)
    if source_sha256 and digest != source_sha256.lower():
        raise IngestError("source_changed", "暂存摘要与下载记录不一致")

    info = slide_format_registry.lookup(name)
    cap = info.get("capability")
    if cap == slide_format_registry.CAP_UNSUPPORTED:
        raise IngestError("unsupported_format")
    if cap == slide_format_registry.CAP_NATIVE_BUNDLE:
        raise IngestError("baidu_bundle_unsupported")

    dest_dir = _upload_dir()
    source_dest = dest_dir / name
    visible = name
    conversion_job_id = None

    if cap == slide_format_registry.CAP_CONVERT_REQUIRED:
        visible = canonical_name_for(name)
        canon_path = dest_dir / visible
        if source_dest.exists() or canon_path.exists() \
                or conversion_store.canonical_is_live(visible) \
                or conversion_store.canonical_is_live(name):
            raise IngestError("name_unavailable")
        source_format = _probe_convert(str(staging))
        shutil.copy2(str(staging), str(source_dest))
        job = conversion_store.create_job(
            owner_user_id=owner_user_id or "",
            upload_id=None,
            source_name=name,
            source_sha256=digest,
            source_format=source_format,
            canonical_name=visible,
            product_exists=False,
            target_project_id=target_project_id)
        worker_id = "baidu_ingest_%s" % (job["id"][:16],)
        claimed = conversion_store.claim_job(job["id"], worker_id)
        if claimed is None:
            claimed = conversion_store.get_job(job["id"])
        conversion_worker.process_job(
            claimed, str(dest_dir), worker_id)
        job = conversion_store.get_job(job["id"])
        conversion_job_id = job["id"]
        if job.get("state") != "ready":
            try:
                source_dest.unlink()
            except OSError:
                pass
            raise IngestError(job.get("error_code") or "conversion_failed")
        assoc = job.get("project_associate_state") or "not_needed"
        if assoc == "not_needed" and target_project_id:
            assoc = associate_slide(owner_user_id, target_project_id, visible)
            conversion_store.set_project_associate(
                job["id"], target_project_id, assoc)
        token = "cvj:" + job["id"]
        return {
            "ingest_token": token,
            "slide_name": visible,
            "conversion_job_id": conversion_job_id,
            "project_associate_state": assoc,
        }

    if source_dest.exists() or conversion_store.canonical_is_live(name):
        raise IngestError("name_unavailable")
    shutil.copy2(str(staging), str(source_dest))
    try:
        _probe_native(source_dest)
        share_store.set_slide_meta(
            name, owner_user_id=owner_user_id or None,
            requester_role=user_store.ROLE_USER)
    except IngestError:
        try:
            source_dest.unlink()
        except OSError:
            pass
        raise
    except PermissionError as e:
        try:
            source_dest.unlink()
        except OSError:
            pass
        raise IngestError("forbidden", "无权登记切片") from e
    assoc = associate_slide(owner_user_id, target_project_id, name)
    return {
        "ingest_token": "slide:" + name,
        "slide_name": name,
        "conversion_job_id": None,
        "project_associate_state": assoc,
    }
