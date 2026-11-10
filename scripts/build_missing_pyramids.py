#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_missing_pyramids —— 为已入库的单层大切片补建服务端金字塔（运维工具）。

背景：摄取期金字塔（``cos_ingest_worker.process_ready`` → ``slide_pyramid``）
上线前已入库的资产仍只有单一分辨率层，viewer/AI 只能全量解码 level 0。
本工具用**同一构建/校验/记账原语**离线回填；默认 dry-run，只有显式
``--apply --slide-id <id>`` 才改写磁盘与账本。

用法::

    # 只列出可回填的 ready 资产（slide_id / 尺寸 / 当前字节数）
    python scripts/build_missing_pyramids.py

    # 限制条数 / 只输出 JSON Lines（便于审计）
    python scripts/build_missing_pyramids.py --limit 50 --json

    # 回填单个 slide（必须显式给出 ID；不会批量改写）
    python scripts/build_missing_pyramids.py --apply --slide-id sld_xxx

安全边界（与摄取期一致）：

  - 只处理 ``asset_state='ready'`` 且 ``storage_layout='id_bundle'`` 的资产；
  - 构建前重新确认：资产仍 ready、无 ``slide_delete_jobs``、无同 slide_id
    的非终态摄取任务；任一不满足 → 拒绝（不碰文件）；
  - 构建到同目录临时文件 → ``slide_io`` 校验（层数/尺寸/mpp/level-0 像素）
    → 用户配额可吸收增长 → 硬链接备份 → 原子 ``os.replace`` → 同一事务
    更新 ``slides.accounted_bytes`` + 用户 ``used_bytes`` + 写新内容
    revision（viewer render token / AI snapshot attestation / 切片缓存
    generation 都以它失效）；DB 失败自动回滚原文件；
  - 任何失败保留/恢复原单层文件，不删除资产、不改授权；
  - 绝不访问 COS，不处理 legacy 布局（不可读）资产。

退出码：``0`` 成功/无需处理；``1`` 工具错误/拒绝；``2`` 构建或校验失败。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg  # noqa: E402

import ingestion_store as ist  # noqa: E402
import pg_store  # noqa: E402
import slide_io  # noqa: E402
import slide_pyramid  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_FAILED = 2


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _slide_size(path):
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def _probe(path):
    slide = slide_io.open_slide(path)
    try:
        return {"level_count": int(getattr(slide, "level_count", 0) or 0),
                "dimensions": tuple(int(v) for v in slide.dimensions)}
    finally:
        try:
            slide.close()
        except Exception:  # noqa: BLE001
            pass


def _eligible_candidates(limit=None):
    """遍历 ready id_bundle 资产，产出可回填项（不含 build）。"""
    count = 0
    for desc in slide_store.list_ready_descriptors():
        if desc.storage_layout != slide_store.StorageLayout.ID_BUNDLE:
            continue
        path = slide_storage.resolve_descriptor_path(desc)
        if path is None or not os.path.isfile(path):
            continue
        if not slide_pyramid.is_buildable_source(path):
            continue
        try:
            info = _probe(path)
        except Exception:  # noqa: BLE001  打不开的资产交给人工，不猜
            continue
        if not slide_pyramid.needs_pyramid(info):
            continue
        item = {
            "slide_id": desc.slide_id,
            "width": int(info["dimensions"][0]),
            "height": int(info["dimensions"][1]),
            "bytes": _slide_size(path),
            "format_ext": desc.format_ext or "",
            "accounted_bytes": desc.accounted_bytes,
        }
        yield item
        count += 1
        if limit is not None and count >= int(limit):
            return


def _write_refusal(desc):
    """返回拒绝原因（None=可写）。"""
    if desc is None:
        return "slide_not_found"
    if desc.asset_state != slide_store.SlideState.READY:
        return "not_ready:%s" % desc.asset_state
    if desc.storage_layout != slide_store.StorageLayout.ID_BUNDLE:
        return "legacy_layout"
    job = slide_store.get_delete_job(desc.slide_id)
    if job is not None:
        return "delete_job_exists:%s" % (job.get("state") or "")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM ingestion_jobs WHERE slide_id=%s "
                "AND NOT (state = ANY(%s)) LIMIT 1",
                (desc.slide_id, sorted(ist.TERMINAL_STATES)))
            if cur.fetchone() is not None:
                return "ingestion_active"
    finally:
        conn.close()
    return None


def _quota_check(desc, growth):
    if int(growth) <= 0:
        return True, ""
    ident = {"user_id": (desc.owner_user_id or "").strip(), "role": "user"}
    if not upload_guard.quota_applies(ident):
        # owner_user_id 非空即需要配额行；无行时与 worker 同口径（静默放行）。
        return True, ""
    try:
        row = upload_guard.peek_quota_row(ident["user_id"])
    except Exception:  # noqa: BLE001
        return False, "quota_unreadable"
    if row is None:
        return True, ""
    headroom = (int(row["quota_bytes"]) - int(row["used_bytes"])
                - int(row["reserved_bytes"]))
    if int(growth) > headroom:
        return False, "quota_exceeded"
    return True, ""


def rebuild_one(slide_id):
    """回填单个 slide；返回 (exit_code, payload dict)。"""
    desc = slide_store.resolve_slide_id(slide_id)
    refusal = _write_refusal(desc)
    if refusal:
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": refusal}
    path = slide_storage.resolve_descriptor_path(desc)
    if path is None or not os.path.isfile(path):
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": "asset_path_missing"}
    if not slide_pyramid.is_buildable_source(path):
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": "unsupported_format_ext"}
    try:
        info = _probe(path)
    except Exception as exc:  # noqa: BLE001
        return EXIT_FAILED, {"slide_id": slide_id, "ok": False,
                             "error_type": type(exc).__name__}
    if not slide_pyramid.needs_pyramid(info):
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": "not_eligible"}

    bytes_before = _slide_size(path)
    if desc.accounted_bytes is None:
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": "accounted_bytes_missing"}
    if int(desc.accounted_bytes) != bytes_before:
        # 记账与磁盘证据不一致：不猜，交人工对账。
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": "accounted_bytes_mismatch"}

    parent = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".pyramid-", suffix=".tiff",
                                    dir=parent)
    os.close(fd)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    backup = None
    replaced = False
    try:
        result = slide_pyramid.build_pyramid(
            path, tmp_path, scratch_dir=parent)
        slide_pyramid.validate_pyramid(
            path, tmp_path, lossless=result.source_lossless,
            expected_dims=result.level_dimensions)
        bytes_after = int(os.path.getsize(tmp_path))
        ok, reason = _quota_check(desc, bytes_after - bytes_before)
        if not ok:
            return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                                  "refused": reason,
                                  "bytes_before": bytes_before,
                                  "bytes_after": bytes_after}
        digest = slide_pyramid.file_sha256(tmp_path)
        revision = "sha256:%s" % digest[:16]

        backup = os.path.join(
            parent, ".pyramid-backup-%s-%s"
            % (slide_id, secrets.token_hex(4)))
        os.link(path, backup)
        os.replace(tmp_path, path)
        replaced = True

        conn = _connect()
        try:
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    slide_store.acquire_slide_lock(cur, slide_id)
                    # 锁内重验：并发删除/状态漂移一律拒绝（先回滚文件）。
                    cur.execute(
                        "SELECT asset_state, accounted_bytes FROM slides "
                        "WHERE slide_id=%s FOR UPDATE", (slide_id,))
                    row = cur.fetchone()
                    if (row is None
                            or row["asset_state"] != slide_store.SlideState.READY
                            or row["accounted_bytes"] is None
                            or int(row["accounted_bytes"]) != bytes_before):
                        raise slide_store.PyramidAccountingConflict(
                            "锁内重验失败（state=%r accounted=%r）"
                            % (row and row["asset_state"],
                               row and row["accounted_bytes"]))
                    slide_store.record_pyramid_rebuild(
                        slide_id, expected_bytes=bytes_before,
                        new_bytes=bytes_after, revision=revision, conn=conn)
        finally:
            conn.close()
        return EXIT_OK, {
            "slide_id": slide_id, "ok": True,
            "levels": result.levels,
            "level_dimensions": [list(d) for d in result.level_dimensions],
            "seconds": result.seconds,
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "revision": revision,
            "level0_compression": result.level0_compression,
            "sublevel_compression": result.sublevel_compression,
        }
    except slide_pyramid.PyramidSkip as exc:
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": exc.reason}
    except slide_store.PyramidAccountingConflict as exc:
        return EXIT_REFUSED, {"slide_id": slide_id, "ok": False,
                              "refused": "accounting_conflict",
                              "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        if replaced and backup and os.path.exists(backup):
            try:
                os.replace(backup, path)
                replaced = False
            except OSError:
                pass
        return EXIT_FAILED, {"slide_id": slide_id, "ok": False,
                             "error_type": type(exc).__name__,
                             "error": str(exc)}
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if backup and os.path.exists(backup):
            try:
                os.unlink(backup)
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="为单层大切片补建服务端金字塔（默认 dry-run）")
    parser.add_argument("--apply", action="store_true",
                        help="执行回填（必须同时给 --slide-id）")
    parser.add_argument("--slide-id", default="",
                        help="要回填的 slide_id（--apply 时必填且只处理一个）")
    parser.add_argument("--limit", type=int, default=None,
                        help="dry-run 最多列出条数")
    parser.add_argument("--json", action="store_true",
                        help="JSON Lines 输出（机器可读）")
    args = parser.parse_args(argv)

    if args.apply:
        sid = (args.slide_id or "").strip()
        if not sid:
            print("--apply 需要 --slide-id（本工具不做批量改写）",
                  file=sys.stderr)
            return EXIT_REFUSED
        code, payload = rebuild_one(sid)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        elif payload.get("ok"):
            print("OK %s levels=%d %dx%d -> %d bytes in %.2fs revision=%s"
                  % (payload["slide_id"], payload["levels"],
                     payload["level_dimensions"][0][0],
                     payload["level_dimensions"][0][1],
                     payload["bytes_after"], payload["seconds"],
                     payload["revision"]))
        else:
            print("REFUSED/FAILED %s: %s" % (
                payload.get("slide_id"),
                payload.get("refused") or payload.get("error_type")
                or payload.get("error")), file=sys.stderr)
        return code

    count = 0
    for item in _eligible_candidates(limit=args.limit):
        count += 1
        if args.json:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
        else:
            print("%s\t%dx%d\t%d" % (item["slide_id"], item["width"],
                                     item["height"], item["bytes"]))
    if not args.json and count == 0:
        print("no candidates")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
