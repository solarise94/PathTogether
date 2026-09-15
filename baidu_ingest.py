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
import time
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


#: 复用 running（他人转换中）任务的等待参数：轮询间隔与总超时（env 可
#: 覆盖，测试可 monkeypatch）。超时抛 ``conversion_busy``——不在
#: baidu_import_store.NON_RETRYABLE_ERROR_CODES 中，条目保持可重试。
RUNNING_POLL_SECONDS = float(
    os.environ.get("BAIDU_INGEST_RUNNING_POLL_SECONDS") or 1.0)
RUNNING_TIMEOUT_SECONDS = float(
    os.environ.get("BAIDU_INGEST_RUNNING_TIMEOUT_SECONDS") or 900)


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


def _copy_new(src, dst):
    """以 O_EXCL 独占创建 dst 并分块复制 src 内容。

    check-then-copy 的竞态兜底：预检查与复制之间并发方落盘同名文件时，
    ``O_EXCL`` 让本次 open 失败（FileExistsError），绝不会覆盖他人内容。
    写入中途异常必须删掉自己刚创建的半成品再抛（O_EXCL 保证 dst 只可能
    是本调用创建的，清理不会误删他人文件）；复制完成后 flush + fsync，
    保证后续探测/转换读到完整落盘内容。
    """
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as fdst:
            with open(str(src), "rb") as fsrc:
                shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
            fdst.flush()
            os.fsync(fdst.fileno())
    except BaseException:
        try:
            os.unlink(str(dst))
        except OSError:
            pass
        raise


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


def _unlink_quiet(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _release_staged_source(source_dest, job, name):
    """尽力而为删除本次上传复制的源文件副本（失败/复用收口路径）。

    - 任务源名 == 本次上传名且任务仍 open（``conversion_store.STATES_OPEN``）
      时说明并发 worker 可能正用这份副本转换/重试，保留不动；
    - 其余（复用既有任务的副本、任务已终态失败、任务未知）一律删除，
      避免孤立文件。
    """
    if job is not None \
            and (job.get("source_name") or "") == str(name) \
            and (job.get("state") or "") in conversion_store.STATES_OPEN:
        return
    _unlink_quiet(source_dest)


def _await_terminal_job(job_id):
    """轮询等待他人持有的任务到终态（ready/failed/cancelled）。

    超时抛 ``conversion_busy``（可重试稳定码，见模块常量说明）。
    """
    deadline = time.monotonic() + max(RUNNING_TIMEOUT_SECONDS, 0.0)
    while True:
        job = conversion_store.get_job(job_id)
        if job is None:
            raise IngestError("conversion_failed", "转换任务丢失")
        if (job.get("state") or "") in ("ready", "failed", "cancelled"):
            return job
        if time.monotonic() >= deadline:
            raise IngestError("conversion_busy", "同内容转换仍在进行，等待超时")
        time.sleep(max(RUNNING_POLL_SECONDS, 0.0))


def _finish_ready_job(job, owner_user_id, target_project_id):
    """ready 收口：产物已在（owner+sha 幂等），slide_name 用任务原
    canonical——同内容换名复用原产物是 conversion_store 的既定语义，
    不得改绑。"""
    visible = job.get("canonical_name") or job.get("source_name") or ""
    assoc = job.get("project_associate_state") or "not_needed"
    if assoc == "not_needed" and target_project_id:
        assoc = associate_slide(owner_user_id, target_project_id, visible)
        conversion_store.set_project_associate(
            job["id"], target_project_id, assoc)
    return {
        "ingest_token": "cvj:" + job["id"],
        "slide_name": visible,
        "conversion_job_id": job["id"],
        "project_associate_state": assoc,
    }


def _ingest_convert(*, owner_user_id, name, staging, digest, dest_dir,
                    source_dest, target_project_id):
    """convert-required 收口：create_job（owner+sha 幂等）后按 job state 分派。

    - ``ready``：产物已在（同内容换名也复用原任务），**不 claim/process**；
      按原 canonical 收口并删除本次复制的源文件副本（别名登记已由
      create_job 完成）；
    - ``converting``/``validating``（他人正在转换同一内容）：轮询等待至
      终态，不抢租约、不重复转换；
    - ``queued``：本进程 claim + 同步转换（既有路径）；claim 落空说明
      并发 worker 抢先领走，同样等待其收口。

    源文件复制落盘之后的所有异常都收敛为 IngestError 并尽力清理
    source_dest：条目级失败优于批次级崩溃。
    """
    visible = canonical_name_for(name)
    canon_path = dest_dir / visible
    if source_dest.exists() or canon_path.exists() \
            or conversion_store.canonical_is_live(visible) \
            or conversion_store.canonical_is_live(name):
        raise IngestError("name_unavailable")
    source_format = _probe_convert(str(staging))
    try:
        _copy_new(str(staging), str(source_dest))
    except FileExistsError:
        # 预检查后的窗口期被并发同名上传抢先占用
        raise IngestError("name_unavailable")

    job = None
    try:
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
        state = job.get("state") or ""
        if state in ("converting", "validating"):
            # 同一内容他人转换中：等它收口，绝不抢租约
            job = _await_terminal_job(job["id"])
            state = job.get("state") or ""
        if state == "queued":
            claimed = conversion_store.claim_job(job["id"], worker_id)
            if claimed is not None:
                # 领取成功（claim_job 返回行 state 已置 converting 且租约
                # 归本进程）：同步转换（既有路径）
                conversion_worker.process_job(
                    claimed, str(dest_dir), worker_id)
                job = conversion_store.get_job(job["id"]) or claimed
            else:
                # 领取落空：并发 worker 抢先持有租约（converting/validating）
                # → 等它收口；罕见态（queued 但租约未过期）按忙重试
                fresh = conversion_store.get_job(job["id"])
                if fresh is None:
                    raise IngestError("conversion_failed", "转换任务丢失")
                state = fresh.get("state") or ""
                if state in ("converting", "validating"):
                    fresh = _await_terminal_job(job["id"])
                elif state == "queued":
                    raise IngestError(
                        "conversion_busy", "转换任务暂不可领取")
                job = fresh
            state = job.get("state") or ""
        if state != "ready":
            raise IngestError(job.get("error_code") or "conversion_failed")
        if (job.get("source_name") or "") != name:
            # 复用既有任务（同内容换名/他人产物）：转换用不上本次副本
            _unlink_quiet(source_dest)
        return _finish_ready_job(job, owner_user_id, target_project_id)
    except IngestError:
        _release_staged_source(source_dest, job, name)
        raise
    except conversion_store.NameConflict as e:
        _release_staged_source(source_dest, job, name)
        raise IngestError("name_unavailable", "canonical 名已被占用") from e
    except conversion_store.ConversionError as e:
        _release_staged_source(source_dest, job, name)
        raise IngestError(
            "conversion_failed", "转换任务状态异常: %s" % (e.code,)) from e
    except Exception as e:  # noqa: BLE001
        _release_staged_source(source_dest, job, name)
        raise IngestError(
            "conversion_failed",
            "转换任务系统异常: %s" % type(e).__name__) from e


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

    if cap == slide_format_registry.CAP_CONVERT_REQUIRED:
        return _ingest_convert(
            owner_user_id=owner_user_id, name=name, staging=staging,
            digest=digest, dest_dir=dest_dir, source_dest=source_dest,
            target_project_id=target_project_id)

    if source_dest.exists() or conversion_store.canonical_is_live(name):
        raise IngestError("name_unavailable")
    try:
        _copy_new(str(staging), str(source_dest))
    except FileExistsError:
        # 预检查后的窗口期被并发同名上传抢先占用
        raise IngestError("name_unavailable")
    try:
        # source_dest 一定由上方 _copy_new 独占创建，失败清理 unlink
        # 不会误删并发上传者的文件
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
