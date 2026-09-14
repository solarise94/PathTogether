#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切片转换 worker（KFB Phase B）。独立进程，不占用 Gunicorn。

    python conversion_worker.py --loop
    python conversion_worker.py --once
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import time
import traceback

_REPO = os.path.dirname(os.path.abspath(__file__))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import conversion_store  # noqa: E402
import share_store  # noqa: E402
import slide_io  # noqa: E402
import user_store  # noqa: E402
from kfb import KfbError, convert_kfb, convert_kfbf  # noqa: E402

UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(_REPO, "uploads")
POLL_SECONDS = float(os.environ.get("CONVERSION_POLL_SECONDS") or 1.5)


def _convert_for_source(source, work, *, on_progress=None):
    """按 magic 分派：KFBF → 多通道 OME-TIFF；其余 → 明场 KFB 路径。

    内容嗅探优先于扩展名（改名文件按真实字节走对应转换器）。
    """
    from kfb import KFBF_MAGIC
    try:
        with open(source, "rb") as fh:
            magic = fh.read(8)
    except OSError:
        magic = b""
    if magic == bytes(KFBF_MAGIC):
        return convert_kfbf(source, work, overwrite=False,
                            on_progress=on_progress)
    return convert_kfb(source, work, overwrite=False,
                       on_progress=on_progress)


def _worker_id():
    return "cvw_%s_%d" % (socket.gethostname()[:24], os.getpid())


def _manifest_matches_job(dest, job):
    man = dest + ".manifest.json"
    if not os.path.isfile(man):
        return False
    try:
        import json
        data = json.loads(open(man, encoding="utf-8").read())
        sha = ((data.get("source") or {}).get("sha256") or "").lower()
        return sha and sha == (job.get("source_sha256") or "").lower()
    except Exception:
        return False


def _same_inode(a, b):
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def work_path_for(dest, job_id):
    return dest + ".work-" + job_id


def _discard_work(work):
    """清本任务的 work 产物（不影响 canonical dest）。"""
    if not work:
        return
    for p in (work, work + ".part", work + ".manifest.json",
              work + ".manifest.json.part"):
        try:
            os.unlink(p)
        except OSError:
            pass
    assoc = work + ".associated"
    if os.path.isdir(assoc):
        shutil.rmtree(assoc, ignore_errors=True)


def _promote_work(work, dest):
    """把已完成的 work TIFF+manifest 无覆盖提升到 canonical dest。

    dest 仅在 work 完成后经 hardlink 出现，故 dest 无 manifest 的崩溃窗口
    可用 work 与 dest 同 inode 识别并续跑。他人 dest 一律 FileExistsError。
    """
    work_man = work + ".manifest.json"
    dest_man = dest + ".manifest.json"
    work_assoc = work + ".associated"
    dest_assoc = dest + ".associated"
    if not os.path.isfile(work) or not os.path.isfile(work_man):
        raise KfbError("conversion_validation_failed", "work 产物不完整")
    if os.path.isfile(dest):
        if not _same_inode(work, dest):
            raise FileExistsError(dest)
    else:
        os.link(work, dest)
    if not os.path.isfile(dest_man):
        import json
        with open(work_man, encoding="utf-8") as fh:
            data = json.load(fh)
        canon = data.get("canonical")
        if isinstance(canon, dict):
            canon["name"] = os.path.basename(dest)
        tmp_man = dest_man + ".part"
        with open(tmp_man, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_man, dest_man)
    if os.path.isdir(work_assoc) and not os.path.isdir(dest_assoc):
        os.rename(work_assoc, dest_assoc)
    _discard_work(work)


def _validate_canonical(dest):
    slide = slide_io.open_slide(dest)
    try:
        if getattr(slide, "level_count", 0) < 1:
            raise KfbError("conversion_validation_failed", "无金字塔层")
        w, h = slide.level_dimensions[0]
        slide.read_region((0, 0), 0, (min(16, w), min(16, h)))
    finally:
        slide.close()


def process_job(job, upload_dir, worker_id):
    source = os.path.join(upload_dir, job["source_name"])
    canonical = job.get("canonical_name") or (
        os.path.splitext(job["source_name"])[0] + ".tif")
    dest = os.path.join(upload_dir, canonical)
    work = work_path_for(dest, job["id"])
    if not os.path.isfile(source):
        conversion_store.fail_job(job["id"], worker_id, "invalid_kfb_header",
                                  "source missing")
        return False

    def on_progress(*_a):
        conversion_store.heartbeat(job["id"], worker_id)

    try:
        conversion_store.mark_state(job["id"], worker_id, "converting")
        if os.path.isfile(dest) and _manifest_matches_job(dest, job):
            _discard_work(work)
        elif os.path.isfile(dest) and os.path.isfile(work) and _same_inode(
                work, dest):
            _promote_work(work, dest)
        elif os.path.isfile(dest):
            conversion_store.fail_job(
                job["id"], worker_id, "name_unavailable",
                "canonical exists")
            _discard_work(work)
            return False
        else:
            if not (os.path.isfile(work)
                    and os.path.isfile(work + ".manifest.json")):
                _discard_work(work)
                _convert_for_source(source, work, on_progress=on_progress)
            _promote_work(work, dest)
        conversion_store.heartbeat(job["id"], worker_id)
        conversion_store.mark_state(job["id"], worker_id, "validating")
        _validate_canonical(dest)
        share_store.set_slide_meta(
            canonical,
            owner_user_id=(job.get("owner_user_id") or None),
            requester_role=user_store.ROLE_OWNER)
        conversion_store.complete_job(
            job["id"], worker_id, canonical,
            owner_user_id=job.get("owner_user_id") or "",
            settle_bytes=os.path.getsize(dest))
        _associate_target_project(job, canonical)
        _discard_work(work)
        return True
    except FileExistsError:
        conversion_store.fail_job(
            job["id"], worker_id, "name_unavailable", "canonical exists")
        _discard_work(work)
        return False
    except KfbError as e:
        conversion_store.fail_job(job["id"], worker_id, e.code, str(e))
        _retract_ours(dest, work, job)
        _discard_work(work)
        return False
    except Exception:  # noqa: BLE001
        conversion_store.fail_job(
            job["id"], worker_id, "conversion_validation_failed",
            traceback.format_exc()[-1500:])
        _retract_ours(dest, work, job)
        _discard_work(work)
        return False


def _retract_ours(dest, work, job):
    """失败时只收回本任务 hardlink 出的 dest（他人文件不动）。"""
    try:
        os.unlink(dest + ".manifest.json.part")
    except OSError:
        pass
    if os.path.isfile(dest) and os.path.isfile(work) and _same_inode(
            work, dest) and not _manifest_matches_job(dest, job):
        try:
            os.unlink(dest)
        except OSError:
            pass
        try:
            os.unlink(dest + ".manifest.json")
        except OSError:
            pass


def _associate_target_project(job, canonical_name):
    """转换产物 ready 后幂等加入目标项目（C02）。失败不回滚产物。"""
    pid = job.get("target_project_id")
    if not pid:
        return
    state = "failed"
    try:
        proj = share_store.get_project(pid)
        owner = job.get("owner_user_id") or ""
        if proj and (proj.get("owner_user_id") or "") == owner \
                and not proj.get("archived"):
            slides = proj.get("slides") or []
            if canonical_name in slides:
                state = "succeeded"
            elif share_store.add_slides_to_project(pid, [canonical_name]):
                state = "succeeded"
    except Exception:
        state = "failed"
    conversion_store.set_project_associate(job["id"], pid, state)


def run_once(upload_dir=None, worker_id=None):
    upload_dir = upload_dir or UPLOAD_DIR
    worker_id = worker_id or _worker_id()
    job = conversion_store.claim_one(worker_id)
    if not job:
        return None
    process_job(job, upload_dir, worker_id)
    return job["id"]


def run_loop():
    wid = _worker_id()
    while True:
        try:
            claimed = run_once(worker_id=wid)
            if claimed is None:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            return
        except Exception:
            traceback.print_exc()
            time.sleep(POLL_SECONDS)


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv[1:])
    if args.loop:
        run_loop()
        return 0
    run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
