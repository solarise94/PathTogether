#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""COS 直传摄取 worker（Phase 2，docs/cos-direct-upload-audit-plan.md §3.1/§3.3/§4/§6/§10）。

独立进程（docker_entry.sh 以 ``COS_INGEST_WORKER=1`` 拉起；capability 未上线前
默认关，§8）：

    python cos_ingest_worker.py --loop
    python cos_ingest_worker.py --once

分工边界（§3.1/§1.1）：控制 API（app.py）只做签名，请求内无 COS 网络调用；
一切 COS 网络操作（Initiate/ListParts/Complete/Abort/HEAD/版本化 Range GET/
Delete/List 分页）只发生在本 worker。对象 key 由服务端生成
``incoming/<owner-user-id|anon>/<job_id>/<rand>``，拒绝任何客户端提供的 key。

主循环 duties（按序执行，单 duty 失败只记日志不炸循环）：

1. ``scheduler_tick``（节流 ``COS_SCHEDULER_INTERVAL_SECONDS``，须 < 600s）：
   等待超期 sweep → 已准入超期 sweep → 活跃本地预约续租 → 磁盘水位 → FIFO 准入；
2. ``process_preparing``：生成 key + 分块计划（declared × COS_PART_BYTES）→
   Initiate → ``worker_begin_uploading`` 冻结 key/uploadId/计划；
3. ``process_completing``：ListParts 核对（连续编号 1..N、每块 size==计划长、
   总长==declared；浏览器 ETag 只是提示不采用）→ Complete（必须 200 且拿到
   versionId）→ HEAD 核 size → ``worker_pin_source`` 钉源；
4. ``process_queued``：queued → downloading；
5. ``process_downloading``：版本化 Range GET 断点续传——只采纳 206 且
   Content-Range 起点与请求一致的响应；200 整对象/错 Range 排干计入重传
   预算、不采用其数据（§3.3）；wire 预算 = declared × multiplier，超限
   硬停；读满后流式 SHA-256 → validating；
6. ``process_validating``：落盘前重查水位 → 大小/open_slide 校验 → 持久化
   commit intent（§4 提交恢复栅栏）→ no-clobber 提升（hardlink 优先，绝不
   os.replace）→ metadata → 配额一次结算（ready）；
7. ``process_ready``：Viewer readiness probe（照 conversion_worker 的代表性
   tile 探针）→ completed；暂时失败只记 retry 事件并释放租约，不重下载、
   不删本地副本（§4）；
8. ``process_cleanup``：Abort（幂等）+ 全版本删除（含 delete marker）+ 分页
   复查 → ``finalize_cleanup`` 释放池预约（§6.2）；任何 CosClientError →
   ``record_cleanup_failure`` 指数退避；
9. ``reconcile_tick``（节流 max(60, 调度间隔)）：分页列举 ``incoming/`` 全部
   版本 + 进行中 multipart，observed 超池/超预约+safety → drift_pause 写回
   ``record_observation``（fail-closed 暂停准入）；同时做孤儿识别（key 对应
   job 已终态且未清理 → cleanup 置 pending；库中无此 job → 只记模块告警，
   绝不猜测删除，§6.2）。

日志纪律：绝不打印签名 URL / SecretId / SecretKey / STS（cos_client 已脱敏；
本模块只记 job_id / 状态 / 错误类别 / 字节数）。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import secrets
import shutil
import sys
import time

import psycopg.rows

if __package__ in (None, ""):  # 脚本直跑（docker_entry.sh：python3 /app/...）
    _REPO = os.path.dirname(os.path.abspath(__file__))
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)

import cos_client  # noqa: E402  # 默认注入点：单测以 fake 替换
import cos_config  # noqa: E402
import cos_pool_store  # noqa: E402
import ingestion_store as ist  # noqa: E402
import pg_store  # noqa: E402
import share_store  # noqa: E402
import share_store_pg  # noqa: E402  # 归属终检读 _OWNER_USER_ID（匿名回落）
import slide_io  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402

_log = logging.getLogger("svs.cos_ingest")

#: 单次 Range GET 请求块大小（§3.3 断点续传粒度；进度按块尾持久化，
#: 即每 ~8MiB 一个 checkpoint——满足「每 ~64MiB 或块尾」的下限要求）。
DOWNLOAD_CHUNK_BYTES = 8_000_000

#: 单次 duty 最多传输的块数（防一次 duty 长跑超过租约 TTL 后无谓失租；
#: 到限即持久化进度并释放租约，下轮从 checkpoint 续传）。
MAX_CHUNKS_PER_INVOCATION = 128

#: readiness 暂时失败的重试间隔（§4：保持 ready 重试，不降级不删副本）。
READINESS_RETRY_SECONDS = 60

#: 流式读写的缓冲粒度。
_IO_BUF_BYTES = 1024 * 1024

#: 对账/清理分页的防失控上限（真实桶 incoming/ 下分页数远低于此）。
_MAX_PAGES = 10_000

#: 模块级节流状态（--loop 进程内复用；测试可注入独立 dict）。
_STATE = {"scheduler_last": 0.0, "reconcile_last": 0.0, "download_token": None}

_ContentRangeRe = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 基础助手
# --------------------------------------------------------------------------- #
def upload_dir() -> str:
    """UPLOAD_DIR 解析（app.py:204-206 同款写法；测试 monkeypatch env 即可）。"""
    return os.environ.get("UPLOAD_DIR") or os.path.join(
        os.path.expanduser("~"), "svs-viewer", "uploads")


def _ensure_upload_dir() -> str:
    """确保上传目录存在后返回其路径（part 文件与提升目标同卷，§3.1）。"""
    path = upload_dir()
    os.makedirs(path, exist_ok=True)
    return path


def part_name(job_id: str) -> str:
    """同卷暂存 part 文件名（UPLOAD_DIR 下隐藏文件，含 job_id 便于归因）。"""
    return ".ingesting-%s.part" % job_id


def _stat_ident(path):
    """路径的文件身份 (st_dev, st_ino)；stat 失败返回 None。

    归属拒绝分支的**诊断**用（review 第四轮 P1 后不再据此删除）：判定
    dest 是否仍为本任务提升的那份，供人工清理滞留文件时参考。stat 与
    unlink 之间无原子性，任何「检查后再删」都有误删并发替换文件的窗口。
    """
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _file_sha256_matches(path, expected_hex) -> bool:
    """流式比对文件 SHA-256（提交恢复的内容级归属核实，P1-2）。

    expected_hex 非法/为空一律 False（fail-closed，不猜）。
    """
    if not expected_hex or len(expected_hex) != 64:
        return False
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(_IO_BUF_BYTES)
                if not buf:
                    break
                h.update(buf)
    except OSError:
        return False
    return h.hexdigest() == expected_hex.lower()


def make_object_key(job) -> str:
    """服务端生成对象 key（§3.1）：incoming/<owner|anon>/<job_id>/<rand>。

    不含患者信息或原文件名；owner 缺失（免登录共享身份）用 anon 段。
    """
    owner = (job.get("owner_user_id") or "").strip() or "anon"
    return "incoming/%s/%s/%s" % (owner, job["job_id"], secrets.token_hex(16))


def compute_part_plan(declared_size: int, part_bytes: int):
    """按 declared_size 与 COS_PART_BYTES 计算分块计划。

    编号从 1 起、最后一块收尾；总长恒等于 declared（worker_begin_uploading
    会再校验一次，不信任调用方拼装）。declared 必须为正。
    """
    declared_size = int(declared_size)
    part_bytes = int(part_bytes)
    if declared_size <= 0 or part_bytes <= 0:
        raise ValueError("compute_part_plan 参数非法（declared=%s part=%s）"
                         % (declared_size, part_bytes))
    plan = []
    offset = 0
    number = 1
    while offset < declared_size:
        length = min(part_bytes, declared_size - offset)
        plan.append({"part_number": number, "offset": offset, "length": length})
        offset += length
        number += 1
    return plan


def _sha256_file(path, chunk=_IO_BUF_BYTES) -> str:
    """流式复算整文件 SHA-256（app.py:_sha256_file 同款，不 import app）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _promote_no_clobber(src, dest) -> str:
    """原子 no-clobber 提升（逐句照 app.py:_promote_no_clobber，不 import app）。

    优先 hardlink（源仍在，失败可回滚）；``os.replace`` 会覆盖并发出现的
    同名目标并把源移走，破坏 no-clobber，禁止使用。跨设备时复制到 dest
    同目录唯一临时名再 link，绝不 replace。目标已存在 → FileExistsError。
    """
    try:
        os.link(src, dest)
        return "link"
    except FileExistsError:
        raise
    except OSError:
        tmp = os.path.join(
            os.path.dirname(dest), ".promoting-%s-%s"
            % (os.path.basename(dest), secrets.token_hex(8)))
        try:
            shutil.copy2(src, tmp)
            os.link(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "copy-link"


def _unlink_quiet(path):
    """尽力删除文件（终态清理路径；失败只记 debug，不阻断收口）。"""
    try:
        os.unlink(path)
    except OSError:
        pass


def _release_lease(job_id, token, state=None):
    """duty 收尾释放执行租约：同 cycle 的后继 duty / 其它 worker 可立即接手。

    CAS 失败（已被重领）说明新纪元已开始，静默即可。不释放的话，任务要等
    租约 TTL（默认 900s）过期才能被下一次 claim，链式推进会退化成分钟级。
    """
    if state is not None:
        state.pop("download_token", None)
    if not token:
        return
    try:
        ist.release_worker_lease(job_id, token)
    except Exception:  # noqa: BLE001
        _log.debug("release_worker_lease 失败（job=%s）", job_id)


# --------------------------------------------------------------------------- #
# 1) 容量调度器 duty（§6.3：sweep + 续租 + 水位 + FIFO 准入）
# --------------------------------------------------------------------------- #
def scheduler_tick(cos=None, state=None, *, force=False):
    """容量调度器单步（节流 COS_SCHEDULER_INTERVAL_SECONDS，非 COS 网络调用）。

    顺序固定：过期 sweep（waiting / 已准入）→ 活跃本地预约续租 → 磁盘水位
    → FIFO 准入（水位不过则 disk_watermark_ok=False，任务保持等待）。
    返回 True 表示本轮实际执行了调度。
    """
    state = _STATE if state is None else state
    now = time.monotonic()
    interval = max(1, int(cos_config.COS_SCHEDULER_INTERVAL_SECONDS))
    if not force and now - float(state.get("scheduler_last") or 0.0) < interval:
        return False
    state["scheduler_last"] = now
    expired_waiting = ist.sweep_expired_waiting()
    expired_jobs = ist.sweep_expired_jobs()
    if expired_waiting or expired_jobs:
        _log.info("scheduler sweep：waiting 超期 %d 条，job 超期 %d 条",
                  len(expired_waiting), len(expired_jobs))
    renew = ist.renew_active_local_reservations()
    if any(v != "skipped" for v in renew.values()):
        _log.info("scheduler renew：%s", renew)
    try:
        upload_guard.check_disk_watermark(_ensure_upload_dir())
        watermark_ok = True
    except upload_guard.DiskWatermarkExceeded as exc:
        watermark_ok = False
        _log.warning("磁盘水位不过，本轮暂停准入：%s", exc)
    admitted = ist.admit_waiting_fifo(disk_watermark_ok=watermark_ok)
    if admitted:
        _log.info("FIFO 准入 %d 条：%s", len(admitted), admitted)
    return True


# --------------------------------------------------------------------------- #
# 2) preparing → uploading（生成 key + 计划 + Initiate，一次冻结）
# --------------------------------------------------------------------------- #
def process_preparing(cos=None, state=None):
    """领取 PREPARING：生成 key/分块计划 → Initiate → worker_begin_uploading。

    CosConfigMissing：释放租约空转返回（capability off，主循环继续非 COS
    duty）；Initiate 成功但 store 收口失败（崩溃窗口）：uploadId 未落库即
    孤儿，由 reconcile 的孤儿识别 + 桶 7 天生命周期兜底，不本地补记。
    """
    cos = cos_client if cos is None else cos
    job = ist.claim_next_job_for_worker([ist.PREPARING])
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    try:
        key = make_object_key(job)
        plan = compute_part_plan(job["declared_size"],
                                 cos_config.COS_PART_BYTES)
        upload_id = cos.initiate_multipart(key)
    except cos_client.CosConfigMissing:
        _release_lease(job_id, token, state)
        _log.warning("COS 配置缺失，preparing 空转（job=%s）", job_id)
        return None
    except cos_client.CosClientError as exc:
        # 网络级失败：释放租约，下轮重试（幂等：重复 Initiate 只会多一个
        # 孤儿 uploadId，由对账/生命周期回收）。
        _release_lease(job_id, token, state)
        _log.warning("Initiate 失败，下轮重试（job=%s）：%s", job_id, exc)
        return None
    try:
        ist.worker_begin_uploading(
            job_id, gen, bucket=cos_config.COS_BUCKET, object_key=key,
            upload_id=upload_id, part_plan=plan)
        _log.info("preparing→uploading（job=%s parts=%d）", job_id, len(plan))
    except ist.StaleLease:
        pass  # 失租：新 worker 已接管，静默放弃
    except ist.IngestionStateError:
        # 状态已被并发推进（如取消）：uploadId 成为孤儿，靠生命周期兜底。
        _log.warning("begin_uploading 收口被拒（job=%s），uploadId 交由孤儿回收",
                     job_id)
    finally:
        _release_lease(job_id, token, state)
    return job_id


# --------------------------------------------------------------------------- #
# 3) completing：可信 ListParts 核对 → Complete → HEAD → 钉源
# --------------------------------------------------------------------------- #
def _verify_parts_against_plan(parts, plan, declared_size) -> str:
    """ListParts 结果与冻结计划核对；返回空串表示通过，否则为拒绝 reason。

    只信服务端 ListParts 的编号与 size；浏览器上报的 ETag 只是提示（§3.1）。
    """
    numbers = sorted(int(p["part_number"]) for p in parts)
    if numbers != list(range(1, len(plan) + 1)):
        return "part_numbers_incomplete"
    by_number = {int(p["part_number"]): int(p["size"]) for p in parts}
    total = 0
    for spec in plan:
        actual = by_number.get(int(spec["part_number"]))
        if actual is None or actual != int(spec["length"]):
            return "part_size_mismatch"
        total += actual
    if total != int(declared_size):
        return "total_size_mismatch"
    return ""


class _RecoveryTransient(Exception):
    """Complete 恢复路径的瞬态网络失败——保持 completing 下轮重试
    （review 第二轮 P1：首次 HEAD 超时被误当永久失败）。"""


def _is_no_such_upload(exc) -> bool:
    """ListParts 异常是否为「uploadId 已消耗/不存在」（404 NoSuchUpload）。

    cos_client 的脱敏消息固定含「HTTP <code> <Code>」，按 404+NoSuchUpload
    双特征识别；宁可把可疑错误当瞬态重试（下轮仍会走到这里），不误判恢复。
    """
    text = str(exc)
    return "404" in text and "NoSuchUpload" in text


def _recover_completed_head(cos, job_id, gen, key, declared_size):
    """Complete 响应丢失后的恢复：版本列举证明 Complete 属于本任务。

    对象 key ``incoming/<owner>/<job>/<rand>`` 由服务端为本任务独占生成，
    浏览器只有绑定该 key+uploadId 的 UploadPart 授权——该 key 下存在
    size==declared 的对象版本即可证明它是本任务的 Complete 产物（不存在
    他人代写或部分上传成整对象的路径）。用 ListObjectVersions（而非
    HEAD latest：部分实现/桩不支持无版本号的 latest 语义）取精确 key 的
    版本。

    返回 version_id；**不可恢复的否定证据**（key 下无整对象/大小不符）
    返回 None → 调用方 fail；**瞬态错误**（列举失败）抛 _RecoveryTransient
    → 调用方保持 completing 下轮重试——网络抖动永不构成失败依据。
    """
    try:
        items, truncated, _marker = cos.list_object_versions_page(prefix=key)
    except cos_client.CosClientError as exc:
        raise _RecoveryTransient(str(exc)) from exc
    matches = [v for v in items
               if v.get("key") == key and not v.get("is_delete_marker")
               and int(v.get("size") or 0) == int(declared_size)]
    if not matches:
        _log.warning("Complete 恢复：key 下无大小相符的整对象（job=%s）",
                     job_id)
        return None
    latest = next((v for v in matches if v.get("is_latest")), matches[0])
    version = latest.get("version_id") or ""
    if not version:
        return None
    ist.worker_record_complete(job_id, gen, version_id=version,
                               etag=latest.get("etag") or "",
                               size_bytes=int(declared_size))
    _log.info("Complete 响应丢失，已按版本列举恢复（job=%s）", job_id)
    return version


def process_completing(cos=None, state=None):
    """领取 COMPLETING：核对 → Complete（必须拿到 versionId）→ HEAD 核 size → 钉源。

    缺块/长度不符 → ``worker_back_to_uploading``（浏览器按可信 ListParts
    续传）；HEAD size != declared → fail_job('source_size_mismatch')——已
    Complete 的对象不能 Abort，转 cleanup 路径（fail_job 自动置 pending）。
    """
    cos = cos_client if cos is None else cos
    job = ist.claim_next_job_for_worker([ist.COMPLETING])
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    key = job.get("object_key") or ""
    upload_id = job.get("upload_id") or ""
    plan = job.get("part_plan_json")
    try:
        if not key or not upload_id or not plan:
            # JSONB 损坏 fail-closed（store 侧已把坏 JSON 归 None）：不猜计划。
            ist.fail_job(job_id, gen, "part_plan_lost")
            return None
        version_id = job.get("cos_version_id") or ""
        if not version_id:
            # 尚未 Complete：核对 → Complete → **立即落库**（review 740e823
            # P1-1：Complete 与 HEAD 之间崩溃会让 uploadId 被消耗而库内无
            # 版本，下轮 ListParts 永远 NoSuchUpload）。
            try:
                parts = cos.list_parts(key, upload_id)
            except cos_client.CosConfigMissing:
                _release_lease(job_id, token, state)
                _log.warning("COS 配置缺失，completing 空转（job=%s）", job_id)
                return None
            except cos_client.CosClientError as exc:
                if _is_no_such_upload(exc):
                    # Complete 已发生但响应丢失（或上传被 Abort）：uploadId 已
                    # 消耗。恢复路径——对象 key 为本任务独占，HEAD latest
                    # 且大小==declared 即可证明 Complete 属于本任务并补记版本。
                    try:
                        recovered = _recover_completed_head(
                            cos, job_id, gen, key, job["declared_size"])
                    except _RecoveryTransient as exc:
                        # 瞬态网络错误不构成失败依据：保持 completing 重试
                        _release_lease(job_id, token, state)
                        _log.warning("Complete 恢复 HEAD 瞬态失败，下轮重试"
                                     "（job=%s）：%s", job_id, exc)
                        return None
                    if recovered is None:
                        ist.fail_job(job_id, gen, "upload_lost_after_complete")
                        return job_id
                    version_id = recovered
                else:
                    _release_lease(job_id, token, state)
                    _log.warning("ListParts 失败，下轮重试（job=%s）：%s",
                                 job_id, exc)
                    return None
            else:
                reason = _verify_parts_against_plan(
                    parts, plan, job["declared_size"])
                if reason:
                    ist.worker_back_to_uploading(job_id, gen, reason=reason)
                    _log.info("complete 核对未过，回 uploading"
                              "（job=%s reason=%s）", job_id, reason)
                    return job_id
                try:
                    result = cos.complete_multipart(key, upload_id, parts)
                except cos_client.CosConfigMissing:
                    _release_lease(job_id, token, state)
                    return None
                except cos_client.CosClientError as exc:
                    _release_lease(job_id, token, state)
                    _log.warning("Complete 失败，下轮重试（job=%s）：%s",
                                 job_id, exc)
                    return job_id
                version_id = (result or {}).get("version_id") or ""
                if not version_id:
                    # §3.1：必须钉死 versionId（否则下载窗口内同 key 可被替换）。
                    ist.fail_job(job_id, gen, "source_version_missing")
                    return job_id
                # Complete 成功 → 立即持久化版本（此后任何崩溃都从 HEAD 续起）
                ist.worker_record_complete(
                    job_id, gen, version_id=version_id,
                    etag=(result or {}).get("etag") or "",
                    size_bytes=job["declared_size"])
        try:
            head = cos.head_object(key, version_id)
        except cos_client.CosClientError as exc:
            # Complete 结果已落库，HEAD 幂等可重试。
            _release_lease(job_id, token, state)
            _log.warning("HEAD 失败，下轮重试（job=%s）：%s", job_id, exc)
            return job_id
        size = int(head.get("size") or 0)
        if size != int(job["declared_size"]):
            # 已完成对象不可 Abort：fail → cleanup_pending 删确切版本。
            ist.fail_job(job_id, gen, "source_size_mismatch")
            _log.warning("HEAD size 与 declared 不符（job=%s %d!=%d）",
                         job_id, size, job["declared_size"])
            return job_id
        ist.worker_pin_source(job_id, gen, version_id=version_id,
                              etag=(head.get("etag") or ""),
                              size_bytes=size)
        _log.info("source 已钉死（job=%s size=%d）", job_id, size)
        return job_id
    except ist.StaleLease:
        return None  # 失租：新 worker 接管，静默放弃
    finally:
        _release_lease(job_id, token, state)


# --------------------------------------------------------------------------- #
# 4) queued → downloading
# --------------------------------------------------------------------------- #
def process_queued(cos=None, state=None):
    """领取 QUEUED：转入 downloading（下载循环由 process_downloading 推进）。"""
    job = ist.claim_next_job_for_worker([ist.QUEUED])
    if job is None:
        return None
    job_id = job["job_id"]
    try:
        ist.worker_begin_download(job_id, job["worker_generation"])
    except ist.StaleLease:
        return None
    finally:
        _release_lease(job_id, job["worker_lease_token"], state)
    return job_id


# --------------------------------------------------------------------------- #
# 5) downloading：版本化 Range GET 断点续传（§3.3）
# --------------------------------------------------------------------------- #
def _parse_content_range(header: str):
    """解析 Content-Range（``bytes a-b/total``）→ (start, end, total) 或 None。"""
    if not header:
        return None
    m = _ContentRangeRe.match(str(header).strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _fetch_range(cos, key, version_id, start, end, declared_size,
                 wire_cap=None):
    """执行一次 Range GET 并严格执行 §3.3 采纳条件。

    只采纳 HTTP 206 且 Content-Range 的 start/end/total 与请求一致（total 必
    须等于 declared——源身份变化按失败处理）的响应；200 整对象/错 Range/
    早 EOF 一律不采纳其数据，但**已传输字节仍计入 wire**（排干响应体计数，
    重传预算的意义正在于此）。wire_cap 为本次请求可传输的字节硬顶（§6.3
    按实际字节硬截断）：到达即停止读取并关闭连接，防坏响应把整对象拉满。
    返回 (data|None, wire_bytes)。
    """
    expected = end - start + 1
    wire = 0

    def _capped():
        return wire_cap is not None and wire >= wire_cap

    resp = cos.get_object(key, version_id,
                          range_header="bytes=%d-%d" % (start, end))
    try:
        status = int(getattr(resp, "status", 0) or 0)
        headers = getattr(resp, "headers", None) or {}
        content_range = _parse_content_range(headers.get("Content-Range") or "")
        if status != 206 or content_range is None or \
                content_range[0] != start or content_range[1] != end or \
                content_range[2] != int(declared_size):
            # 不采纳：排干已传输字节计入 wire（不采用其数据），预算封顶即停。
            while not _capped():
                buf = resp.read(_IO_BUF_BYTES)
                if not buf:
                    break
                wire += len(buf)
            return None, wire
        chunks = []
        while wire < expected and not _capped():
            want = min(_IO_BUF_BYTES, expected - wire)
            buf = resp.read(want)
            if not buf:
                break  # 早 EOF：网络失败口径，不采纳
            chunks.append(buf)
            wire += len(buf)
        if wire != expected:
            return None, wire
        return b"".join(chunks), wire
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def process_downloading(cos=None, state=None):
    """领取 DOWNLOADING：断点续传至读满 declared → 流式 SHA-256 → validating。

    - checkpoint（download_checkpoint_json.next_offset）为已确认偏移；part
      文件先 ftruncate 到该偏移，丢弃上次崩溃的未确认尾部（不删文件）；
    - part 文件丢失而 checkpoint>0 时归零重下（已确认数据不可信）；
    - 每块尾持久化进度（wire_delta=实际传输，logical_delta=新增唯一字节）；
    - wire 预算 = declared × COS_DOWNLOAD_WIRE_BUDGET_MULTIPLIER，超限
      fail_job('download_budget_exceeded') 并删 part；
    - 网络错/坏响应：worker_download_retry 回队（checkpoint 回退到已确认
      offset），wire 记账先持久化再回队；
    - 同一 worker 跨轮续传用 holding_token（state['download_token']）。
    """
    cos = cos_client if cos is None else cos
    state = _STATE if state is None else state
    job = ist.claim_next_job_for_worker(
        [ist.DOWNLOADING], holding_token=state.get("download_token"))
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    state["download_token"] = token
    declared = int(job["declared_size"])
    key = job.get("object_key") or ""
    version_id = job.get("cos_version_id") or ""
    budget = declared * max(1, int(cos_config.COS_DOWNLOAD_WIRE_BUDGET_MULTIPLIER))
    checkpoint = job.get("download_checkpoint_json") or {}
    next_offset = int(checkpoint.get("next_offset") or 0)
    wire_total = int(job.get("wire_download_bytes") or 0)
    directory = _ensure_upload_dir()
    part_path = os.path.join(directory, part_name(job_id))

    def _fail(code):
        _unlink_quiet(part_path)
        state.pop("download_token", None)
        try:
            ist.fail_job(job_id, gen, code)
        except ist.StaleLease:
            pass
        _log.warning("下载失败终态（job=%s code=%s）", job_id, code)

    def _persist(downloaded, cp, wire_delta, logical_delta):
        """持久化进度；失租抛 StaleLease 由调用方静默放弃。"""
        ist.worker_update_download_progress(
            job_id, gen, downloaded_bytes=int(downloaded), checkpoint=cp,
            wire_delta=int(wire_delta), logical_delta=int(logical_delta))

    if not key or not version_id:
        _fail("source_not_pinned")
        return None
    if os.path.exists(part_path):
        fd = os.open(part_path, os.O_RDWR)
    else:
        # 文件丢失：已确认字节无从保证，归零重下（预算已有 wire 记账）。
        next_offset = 0
        fd = os.open(part_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.ftruncate(fd, next_offset)  # 丢弃未确认尾部，checkpoint 为权威
        if next_offset < declared and wire_total >= budget:
            # 剩余字节已不可能在预算内取回（任何请求都会新增 wire）。
            _fail("download_budget_exceeded")
            return None
        chunks_done = 0
        while next_offset < declared:
            if chunks_done >= MAX_CHUNKS_PER_INVOCATION:
                # 单次到限：持久化已含块尾，释放租约下轮续传（防长跑失租）。
                _release_lease(job_id, token, state)
                return job_id
            req_end = min(next_offset + DOWNLOAD_CHUNK_BYTES, declared) - 1
            try:
                data, wire_added = _fetch_range(
                    cos, key, version_id, next_offset, req_end, declared,
                    wire_cap=max(0, budget - wire_total))
            except cos_client.CosConfigMissing:
                _release_lease(job_id, token, state)
                _log.warning("COS 配置缺失，downloading 空转（job=%s）", job_id)
                return None
            except cos_client.CosClientError as exc:
                # 网络错：回队重试（不删文件；checkpoint 停在已确认 offset）。
                _log.warning("GET 网络错误，回队重试（job=%s）：%s", job_id, exc)
                try:
                    ist.worker_download_retry(job_id, gen)
                except ist.StaleLease:
                    pass
                _release_lease(job_id, token, state)
                return job_id
            wire_total += wire_added
            if data is None:
                # 200/错 Range/早 EOF：计入 wire、不采数据。预算已烧尽时任何
                # 后续请求都只可能更超——直接硬停（防不可满足的无限重试）。
                try:
                    _persist(next_offset,
                             {"next_offset": next_offset}, wire_added, 0)
                    if wire_total >= budget:
                        _fail("download_budget_exceeded")
                        return None
                    ist.worker_download_retry(job_id, gen)
                except ist.StaleLease:
                    return None
                _release_lease(job_id, token, state)
                return job_id
            os.pwrite(fd, data, next_offset)
            next_offset += len(data)
            chunks_done += 1
            try:
                _persist(next_offset, {"next_offset": next_offset},
                         wire_added, len(data))
            except ist.StaleLease:
                return None  # 已写字节在下次领取时被 ftruncate 收口
            if wire_total > budget:
                _fail("download_budget_exceeded")
                return None
        # 读满 declared：流式 SHA-256（本地权威，§3.3）后转 validating。
        h = hashlib.sha256()
        offset = 0
        while True:
            buf = os.pread(fd, _IO_BUF_BYTES, offset)
            if not buf:
                break
            h.update(buf)
            offset += len(buf)
        sha = h.hexdigest()
        try:
            _persist(next_offset,
                     {"next_offset": next_offset, "sha256": sha}, 0, 0)
            ist.worker_begin_validating(job_id, gen)
        except ist.StaleLease:
            return None
        _log.info("下载完成（job=%s bytes=%d）", job_id, next_offset)
        _release_lease(job_id, token, state)
        return job_id
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# 6) validating：校验 + commit intent + no-clobber 提升 + metadata + 结算
# --------------------------------------------------------------------------- #
def process_validating(cos=None, state=None):
    """领取 VALIDATING：本地校验 → commit intent → 提升 → metadata → ready。

    §4 提交恢复栅栏：intent 已持久化（崩溃恢复重跑）时幂等推进——dest 已
    存在且大小==declared 则跳过提升直接结算；dest 缺失则从 intent 记录的
    part 文件补提升。配额一次结算在 worker_settle_ready 内（consume）。
    """
    job = ist.claim_next_job_for_worker([ist.VALIDATING])
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    declared = int(job["declared_size"])
    safe_name = job["safe_name"]
    directory = _ensure_upload_dir()
    dest = os.path.join(directory, safe_name)
    intent = job.get("commit_intent_json")
    adopted_existing = False
    promoted_ident = None  # 本任务提升出的 dest 的 (dev, ino)（若有）
    if intent:
        # 崩溃恢复：sha 以 intent 为权威（提升前已算好），不重算 9.5GB。
        sha = intent.get("sha256") or ""
        part_path = os.path.join(
            directory, intent.get("part") or part_name(job_id))
        if os.path.exists(dest) and os.path.getsize(dest) == declared:
            # review 740e823 P1-2：大小一致≠归属本任务——崩溃窗口内其它上
            # 传可能创建同名同大小文件。恢复采纳前必须内容级核实（流式
            # 比对 intent.sha256，仅在恢复路径付一次全读）；不符按 no-clobber
            # 语义失败（目标名已被他人占用），绝不猜、不覆盖。
            if not _file_sha256_matches(dest, sha):
                _unlink_quiet(part_path)
                ist.fail_job(job_id, gen, "name_unavailable")
                _log.warning("commit 恢复：目标文件内容与本任务不符，"
                             "按名称占用失败（job=%s）", job_id)
                return None
            adopted_existing = True  # 内容确属本任务，但文件可能非本任务落盘
        elif os.path.exists(part_path):
            try:
                _promote_no_clobber(part_path, dest)
                promoted_ident = _stat_ident(dest)  # 归属拒绝时诊断用基准
            except FileExistsError:
                _unlink_quiet(part_path)
                ist.fail_job(job_id, gen, "name_unavailable")
                return None
        else:
            # intent 在、dest 与 part 都不在：理论不可达（link 原子 + part 在
            # 结算后才删），fail-closed 交人工。
            _log.error("commit 恢复失败：dest 与 part 均缺失（job=%s）", job_id)
            ist.fail_job(job_id, gen, "commit_recovery_failed")
            return None
    else:
        part_path = os.path.join(directory, part_name(job_id))
        try:
            # 落盘（copy-link 兜底）前重查水位（§6.3：创建时和落盘前都查）。
            upload_guard.check_disk_watermark(directory, need_bytes=declared)
        except upload_guard.DiskWatermarkExceeded as exc:
            _release_lease(job_id, token, state)  # 瞬态：保持 validating 下轮再试
            _log.warning("水位不足，validating 暂停（job=%s）：%s", job_id, exc)
            return None
        if not os.path.exists(part_path):
            ist.fail_job(job_id, gen, "part_missing")
            return None
        if os.path.getsize(part_path) != declared:
            _unlink_quiet(part_path)
            ist.fail_job(job_id, gen, "local_size_mismatch")
            return None
        try:
            # open_slide 试开+关（app.py:_validate_slide_file 同口径；format_hint
            # 用 safe_name——.part 后缀不参与逻辑格式判定）。
            opened = slide_io.open_slide(part_path, format_hint=safe_name)
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            _unlink_quiet(part_path)
            ist.fail_job(job_id, gen, "validation_failed")
            _log.warning("open_slide 校验失败（job=%s）：%s", job_id,
                         type(exc).__name__)
            return None
        sha = (job.get("download_checkpoint_json") or {}).get("sha256") or ""
        if not sha:
            sha = _sha256_file(part_path)
        try:
            ist.worker_persist_commit_intent(job_id, gen, {
                "target": safe_name,
                "source_version": job.get("cos_version_id") or "",
                "sha256": sha,
                "declared_size": declared,
                "part": os.path.basename(part_path)})
        except ist.StaleLease:
            return None
        try:
            _promote_no_clobber(part_path, dest)
            promoted_ident = _stat_ident(dest)  # 删除守卫的同一性基准
        except FileExistsError:
            # 同名已存在他人文件：no-clobber 拒绝（绝不 os.replace 覆盖）。
            _unlink_quiet(part_path)
            ist.fail_job(job_id, gen, "name_unavailable")
            return None
    try:
        meta = share_store.set_slide_meta(
            safe_name,
            owner_user_id=(job.get("owner_user_id") or None),
            requester_role=user_store.ROLE_OWNER)
        # 归属终检（review 第二轮 P1）：同名 slides 行已有**其它** owner 时
        # set_slide_meta 不覆盖、只把现存 owner 返回——内容相同（sha 恰好
        # 一致）的他人上传也会走到这里。不核返回值就会出现「本任务结算
        # 成功、切片却归属他人」。匿名回落到平台 owner（_OWNER_USER_ID）
        # 视为一致。
        meta_owner = ((meta or {}).get("owner_user_id") or "").strip()
        ours = (job.get("owner_user_id") or "").strip()
        # 平台 owner 回落等价**仅限匿名任务**：实名任务要求精确归属
        # （review 第三轮 P1：普通用户任务不能认领平台 owner 的同名文件）。
        platform_owner = (share_store_pg._OWNER_USER_ID or "").strip()
        anonymous_took_platform = (ours == "" and platform_owner
                                   and meta_owner == platform_owner)
        if meta_owner and meta_owner != ours and not anonymous_took_platform:
            # 名称已被他人持有：恢复认领场景 dest 是对方文件（绝不动）；
            # 本任务提升场景 dest 是本任务字节——但**不做任何按路径的删除**
            # （review 第四轮 P1）：同名文件的提升/替换/删除没有跨进程互斥，
            # 「检查身份 → unlink」两步之间路径可被并发者先删后建，按路径
            # unlink 必然存在误删他人新文件的窗口（inode 守卫收窄但不消除）。
            # 残局交人工：日志给出 still_ours 判定，为真时需人工移除本任务
            # 滞留在他人名下的文件；fail-closed 不猜、不破坏。
            if not adopted_existing:
                still_ours = (promoted_ident is not None
                              and _stat_ident(dest) == promoted_ident)
                _log.error(
                    "归属终检拒绝：dest 不按路径删除（still_ours=%s；为 true "
                    "时需人工清理本任务滞留文件）（job=%s name=%s）",
                    still_ours, job_id, safe_name)
            _unlink_quiet(part_path)
            ist.fail_job(job_id, gen, "name_unavailable")
            _log.warning("归属终检失败：名称已被其它账号持有（job=%s）",
                         job_id)
            return None
        ist.worker_settle_ready(
            job_id, gen, slide_canonical_name=safe_name, sha256_actual=sha,
            settle_bytes=os.path.getsize(dest))
    except ist.StaleLease:
        return None
    _unlink_quiet(part_path)  # 提升成功后清理同卷暂存（dest 已独立存在）
    _log.info("本地入库完成（job=%s slide=%s）", job_id, safe_name)
    _release_lease(job_id, token, state)
    return job_id


# --------------------------------------------------------------------------- #
# 7) ready → completed：Viewer readiness probe（§4）
# --------------------------------------------------------------------------- #
def _probe_viewer_ready(path):
    """代表性 tile 探针（conversion_worker._validate_canonical 同款）。

    任何失败（含「文件打不开」类确定性失败）都按暂时失败处理——本地副本已
    成功提交，重试+人工处置入口，不自动 fail、不重下载、不删副本（§4）。
    """
    slide = slide_io.open_slide(path)
    try:
        if getattr(slide, "level_count", 0) < 1:
            raise ValueError("level_count<1")
        width, height = slide.level_dimensions[0]
        slide.read_region((0, 0), 0, (min(16, width), min(16, height)))
    finally:
        try:
            slide.close()
        except Exception:  # noqa: BLE001
            pass


def process_ready(cos=None, state=None):
    """领取 READY：readiness probe → completed；失败记 retry 事件并释放租约。"""
    job = ist.claim_next_job_for_worker([ist.READY])
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    canonical = job.get("slide_canonical_name") or ""
    try:
        _probe_viewer_ready(os.path.join(upload_dir(), canonical))
    except Exception as exc:  # noqa: BLE001  确定性失败也只重试（§4）
        try:
            ist.worker_note_readiness_retry(
                job_id, gen, error="%s:%s" % (type(exc).__name__, exc),
                next_retry_at=time.time() + READINESS_RETRY_SECONDS)
        except ist.StaleLease:
            return None
        _release_lease(job_id, token, state)
        _log.warning("readiness probe 失败，保持 ready 重试（job=%s）：%s",
                     job_id, type(exc).__name__)
        return None
    try:
        ist.worker_mark_viewer_ready(job_id, gen)
    except ist.StaleLease:
        return None
    _release_lease(job_id, token, state)
    _log.info("viewer readiness 通过（job=%s）", job_id)
    return job_id


# --------------------------------------------------------------------------- #
# 8) cleanup：Abort + 全版本删除 + 复查 → 释放池预约（§6.2）
# --------------------------------------------------------------------------- #
def _list_key_versions(cos, key):
    """分页列举 prefix=key 的全部版本项，过滤 key 精确相等（含 delete marker）。"""
    out = []
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_object_versions_page(
            prefix=key, key_marker=marker)
        out.extend(i for i in items if i.get("key") == key)
        if not truncated or not next_marker:
            return out
        marker = next_marker
    raise cos_client.CosClientError("ListObjectVersions 分页超过上限")


def _upload_still_listed(cos, key, upload_id) -> bool:
    """复查 uploadId 是否仍在进行中 multipart 列表（分页遍历）。"""
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_multipart_uploads_page(
            prefix=key, key_marker=marker)
        if any(i.get("key") == key and i.get("upload_id") == upload_id
               for i in items):
            return True
        if not truncated or not next_marker:
            return False
        marker = next_marker
    raise cos_client.CosClientError("ListMultipartUploads 分页超过上限")


def process_cleanup(cos=None, state=None):
    """领取清理任务：Abort（幂等）→ 删全版本（含 delete marker）→ 复查 → finalize。

    「一次列表为空」不构成提前释放的充分条件（§6.2）：必须复查该 key 版本
    数为 0 且 uploadId 不在 multipart 列表，才 finalize_cleanup 释放池预约。
    清理器不接受客户端任意 key：object_key 必须 startswith('incoming/')。
    """
    cos = cos_client if cos is None else cos
    job = ist.claim_cleanup_job()
    if job is None:
        return None
    job_id = job["job_id"]
    token = job.get("cleanup_lease_token")
    key = job.get("object_key") or ""
    try:
        if not key and not job.get("upload_id"):
            # 已准入但从未发起远端会话（worker 未及 prepare 即取消/超期）：
            # 本任务名下没有任何远端对象/multipart，直接释放池预约即可。
            # （worker 在 initiate 与登记之间的崩溃窗口产生的未记名 upload
            # 属 reconciler 孤儿域（§6.2 集合 3），不由本任务清理兜底。）
            ist.finalize_cleanup(job_id, token)
            _log.info("无远端会话任务直接释放池预约（job=%s）", job_id)
            return job_id
        if not key.startswith("incoming/"):
            # 越界 key 一律拒绝清理并按失败退避（绝不猜测删除，§6.2）。
            ist.record_cleanup_failure(job_id, token, "bad_object_key")
            _log.error("cleanup 拒绝越界 key（job=%s）", job_id)
            return None
        try:
            if job.get("upload_id"):
                cos.abort_multipart(key, job["upload_id"])  # 404 幂等
            for item in _list_key_versions(cos, key):
                cos.delete_object_version(key, item["version_id"])
            residual = _list_key_versions(cos, key)
            if residual:
                raise cos_client.CosClientError(
                    "清理复查仍有 %d 个版本" % len(residual))
            if job.get("upload_id") and \
                    _upload_still_listed(cos, key, job["upload_id"]):
                raise cos_client.CosClientError("清理复查 multipart 仍在列表")
            ist.finalize_cleanup(job_id, token)
            _log.info("远端清理完成（job=%s）", job_id)
            return job_id
        except cos_client.CosClientError as exc:
            # 删除失败只重试删除，不重拉（§6.2）；指数退避由 store 负责。
            ist.record_cleanup_failure(job_id, token, str(exc))
            _log.warning("清理失败，退避重试（job=%s）：%s", job_id, exc)
            return None
    except ist.StaleLease:
        return None  # cleanup lease 已易主，静默放弃


# --------------------------------------------------------------------------- #
# 9) reconcile：远端分页对账 + 孤儿识别（§6.1/§6.2）
# --------------------------------------------------------------------------- #
def _list_all_versions(cos, prefix):
    """分页列举 prefix 下全部对象版本（对账口径：Σ非删除标记 size）。"""
    out = []
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_object_versions_page(
            prefix=prefix, key_marker=marker)
        out.extend(items)
        if not truncated or not next_marker:
            return out
        marker = next_marker
    raise cos_client.CosClientError("ListObjectVersions 分页超过上限")


def _list_all_uploads(cos, prefix):
    """分页列举 prefix 下全部进行中 multipart。"""
    out = []
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_multipart_uploads_page(
            prefix=prefix, key_marker=marker)
        out.extend(items)
        if not truncated or not next_marker:
            return out
        marker = next_marker
    raise cos_client.CosClientError("ListMultipartUploads 分页超过上限")


def _pause_pool_reconcile():
    """列表失败/无法对账时的 fail-closed：置 reconcile_required 暂停准入（§6.1）。

    观测不可信时不得 fail-open——继续下载/取消/清理，但新准入与新凭证暂停。
    """
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE cos_pool_state SET reconcile_status="
                    "'reconcile_required', reconciled_at=now(), "
                    "updated_at=now() WHERE id=1")
    except Exception:  # noqa: BLE001
        _log.exception("对账失败暂停写入 pool 状态失败（fail-closed 告警）")
    finally:
        conn.close()


def _mark_orphans(cur, version_items, upload_items):
    """孤儿识别（§6.2 可回收集合 3）：key 归属 job 已终态且未清理 → pending。

    库中无此 job 的 key：只记模块告警日志（ingestion_events 挂 job_id，无法
    落事件），绝不猜测删除；drift 判定仍按容量算术独立执行。
    """
    keys = {i.get("key") for i in version_items if i.get("key")}
    keys |= {i.get("key") for i in upload_items if i.get("key")}
    candidates = {}
    for key in keys:
        parts = str(key).split("/")
        if len(parts) != 4:  # incoming/<owner>/<job_id>/<rand> 之外告警不猜
            _log.warning("对账发现非任务形状 key（不删除，需人工核查）：%s",
                         "/".join(parts[:2]) + "/…")
            continue
        candidates.setdefault(parts[2], key)
    if not candidates:
        return
    cur.execute(
        "SELECT job_id, state, cleanup_status FROM ingestion_jobs "
        "WHERE job_id = ANY(%s)", (sorted(candidates),))
    known = {r["job_id"]: r for r in cur.fetchall()}
    for job_id, key in candidates.items():
        row = known.get(job_id)
        if row is None:
            # 无法映射到任务的远端对象：暂停猜测，告警人工（§6.2 禁删集合）。
            _log.warning("对账发现无法映射到任务的 COS 对象（告警不删，需人工）："
                         "job 段=%s", job_id)
            continue
        if row["state"] in ist.TERMINAL_STATES and \
                row["cleanup_status"] not in (ist.CLEANUP_PENDING,
                                              ist.CLEANUP_CLEANED):
            cur.execute(
                "UPDATE ingestion_jobs SET cleanup_status=%s, updated_at=now() "
                "WHERE job_id=%s AND cleanup_status NOT IN (%s, %s)",
                (ist.CLEANUP_PENDING, job_id, ist.CLEANUP_PENDING,
                 ist.CLEANUP_CLEANED))
            _log.warning("对账把终态未清理 job 置 cleanup_pending（job=%s）",
                         job_id)


def reconcile_tick(cos=None, state=None, *, force=False):
    """远端对账单步（节流 max(60, COS_SCHEDULER_INTERVAL_SECONDS) 秒）。

    observed = Σ非删除标记版本 size；与池行比对：observed > capacity 或
    observed > reserved + safety → drift_pause=True 写 record_observation
    （暂停新准入/新凭证，下载/取消/清理继续）。列表分页失败 → fail-closed
    直接置 reconcile_required。返回 True 表示本轮实际对账。
    """
    state = _STATE if state is None else state
    interval = max(60, int(cos_config.COS_SCHEDULER_INTERVAL_SECONDS))
    now = time.monotonic()
    if not force and now - float(state.get("reconcile_last") or 0.0) < interval:
        return False
    state["reconcile_last"] = now
    cos = cos_client if cos is None else cos
    try:
        versions = _list_all_versions(cos, "incoming/")
        uploads = _list_all_uploads(cos, "incoming/")
    except cos_client.CosConfigMissing:
        _log.warning("COS 配置缺失，reconcile 空转")
        return False
    except cos_client.CosClientError as exc:
        _log.warning("对账列举失败，fail-closed 暂停准入：%s", exc)
        _pause_pool_reconcile()
        return False
    observed = sum(int(v.get("size") or 0) for v in versions
                   if not v.get("is_delete_marker"))
    # review 740e823 P1-4：未完成 multipart 的已传分块同样占据暂存池容量
    # （碎片收费且计入 10 GB 预约口径，§6.1 observed_remote_bytes 的定义是
    # 「对象版本和 multipart 碎片」）。逐 upload ListParts 求和；任何一次
    # 读取失败保持 fail-closed——暂停准入，不得带着低估的观测值放行。
    for up in uploads:
        try:
            parts = cos.list_parts(up["key"], up["upload_id"])
        except cos_client.CosClientError as exc:
            _log.warning("对账读取未完成分块失败，fail-closed 暂停准入"
                         "（key=%s）：%s", up.get("key"), exc)
            _pause_pool_reconcile()
            return False
        observed += sum(int(p.get("size") or 0) for p in parts)
    pool = cos_pool_store.get_pool_state()
    if pool is None:
        _pause_pool_reconcile()
        return False
    drift = (observed > int(pool["capacity_bytes"]) or
             observed > int(pool["reserved_bytes"]) + int(pool["safety_bytes"]))
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row  # _mark_orphans 按 dict 取行
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cos_pool_store.record_observation(cur, observed, drift)
                _mark_orphans(cur, versions, uploads)
    finally:
        conn.close()
    if drift:
        _log.warning("对账漂移：observed=%d reserved=%d capacity=%d → 暂停准入",
                     observed, int(pool["reserved_bytes"]),
                     int(pool["capacity_bytes"]))
    return True


# --------------------------------------------------------------------------- #
# 主循环与入口
# --------------------------------------------------------------------------- #
_DUTIES = (
    scheduler_tick,
    process_preparing,
    process_completing,
    process_queued,
    process_downloading,
    process_validating,
    process_ready,
    process_cleanup,
    reconcile_tick,
)


def run_cycle(cos=None, state=None):
    """单轮 duties：按序执行，单 duty 异常只记日志，不炸循环。"""
    for duty in _DUTIES:
        try:
            duty(cos=cos, state=state)
        except Exception:  # noqa: BLE001
            _log.exception("duty %s 异常（跳过继续）", duty.__name__)


def main(argv=None):
    """CLI 入口：--loop 常驻 / --once 单轮 / --interval 轮询间隔（秒）。"""
    argv = sys.argv if argv is None else argv
    parser = argparse.ArgumentParser(
        description="COS 直传摄取 worker（ingestion_jobs 排水）")
    parser.add_argument("--loop", action="store_true",
                        help="常驻循环（docker_entry.sh 用）")
    parser.add_argument("--once", action="store_true", help="单轮后退出")
    parser.add_argument(
        "--interval", type=float, default=None,
        help="轮询间隔秒数（默认 COS_WORKER_INTERVAL_SECONDS）")
    args = parser.parse_args(argv[1:])
    logging.basicConfig(
        level=(os.environ.get("COS_WORKER_LOG_LEVEL") or logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    scheduler_interval = int(cos_config.COS_SCHEDULER_INTERVAL_SECONDS)
    if not 0 < scheduler_interval < 600:
        # §6.3：调度间隔必须 < reservation TTL 的 1/3（1800/3=600），否则
        # 续租出现空窗、活跃预约会被惰性回收——配置错误直接拒启。
        raise SystemExit(
            "COS_SCHEDULER_INTERVAL_SECONDS=%s 非法：必须 > 0 且 < 600"
            % scheduler_interval)
    interval = (float(cos_config.COS_WORKER_INTERVAL_SECONDS)
                if args.interval is None else args.interval)
    if interval <= 0:
        raise SystemExit("--interval 必须为正数：%r" % interval)
    _sid, _skey, credentials_ok = cos_config.cos_credentials()
    if not credentials_ok:
        # 配置缺失只告警一次：循环继续跑非 COS duty（sweep/续租/FIFO/queued），
        # COS 网络 duty 各自空转返回（capability 保持 off）。
        _log.warning("COS_INGEST 配置缺失（bucket/region/secret）——COS duty "
                     "空转，capability 保持 off")
    if args.loop:
        while True:
            try:
                run_cycle()
            except KeyboardInterrupt:
                return 0
            except Exception:  # noqa: BLE001
                _log.exception("cycle 异常（继续循环）")
            time.sleep(interval)
    else:
        run_cycle()  # --once（或缺省）单轮
    return 0


if __name__ == "__main__":
    sys.exit(main())
