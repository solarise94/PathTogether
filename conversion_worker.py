#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切片转换 worker（KFB Phase B）。独立进程，不占用 Gunicorn。

    python conversion_worker.py --loop
    python conversion_worker.py --once
"""

from __future__ import annotations

import argparse
import os
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
from kfb import KfbError, convert_kfb  # noqa: E402

UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(_REPO, "uploads")
POLL_SECONDS = float(os.environ.get("CONVERSION_POLL_SECONDS") or 1.5)


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
    if not os.path.isfile(source):
        conversion_store.fail_job(job["id"], worker_id, "invalid_kfb_header",
                                  "source missing")
        return False

    def on_progress(*_a):
        conversion_store.heartbeat(job["id"], worker_id)

    dest_existed = os.path.isfile(dest)
    created_dest = False
    try:
        conversion_store.mark_state(job["id"], worker_id, "converting")
        if dest_existed:
            # 目标在本轮开始前已存在：可能是他人文件，或上次本任务已 link
            # 但未 complete。仅当 sidecar 证明是本源的续跑才收口；失败不删 dest。
            if not _manifest_matches_job(dest, job):
                conversion_store.fail_job(
                    job["id"], worker_id, "name_unavailable",
                    "canonical exists")
                return False
        else:
            convert_kfb(source, dest, overwrite=False, on_progress=on_progress)
            created_dest = os.path.isfile(dest)
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
        return True
    except KfbError as e:
        conversion_store.fail_job(job["id"], worker_id, e.code, str(e))
        _cleanup_partial(dest, ours=created_dest)
        return False
    except Exception:  # noqa: BLE001
        conversion_store.fail_job(
            job["id"], worker_id, "conversion_validation_failed",
            traceback.format_exc()[-1500:])
        _cleanup_partial(dest, ours=created_dest)
        return False


def _cleanup_partial(dest, *, ours=False):
    """只清 .part；仅当产物确认是本任务写出时才删 dest（不覆盖他人文件）。"""
    try:
        if os.path.exists(dest + ".part"):
            os.unlink(dest + ".part")
    except OSError:
        pass
    if not ours:
        return
    try:
        if os.path.exists(dest):
            os.unlink(dest)
    except OSError:
        pass


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
