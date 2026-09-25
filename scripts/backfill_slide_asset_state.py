#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_slide_asset_state —— 旧数据回填脚本（slide ID 化重构 P1）。

本模块是「代码旁合同」：以下逐条镜像
docs/slide-id-refactor-p1-contract-20260925.md §6（回填合同）与
docs/slide-id-refactor-p0-inventory-20260925.md §3 裁决表（R-01~R-20）；
偏离需在 review 中显式裁决。schema 底座是
migrations/0067_slide_asset_identity.sql；状态语义权威是 slide_store.py。

回填合同（§6 逐条）
  1. 资产行翻转（asset_state='legacy' 的 slides 行，分批处理）：
     - 文件存在（UPLOAD_DIR/legacy_filename；accounted_bytes 计入 MRXS 同 stem
       伴侣目录、<name>.manifest.json、<name>.associated/ 派生物字节合计）且
       owner 明确（owner_user_id 非空、在 users 表存在且未禁用——0001 的
       users.disabled 语义）→ 同事务 UPDATE 为 ready：
       asset_state='ready'、published_at（入口文件 mtime 的 TIMESTAMPTZ；
       无则 now()）、accounted_bytes（实际字节合计）、original_filename=
       legacy_filename、display_name（R-02：非空 alias → alias，否则现有
       display_name，否则 legacy_filename）、format_ext（从 legacy_filename
       后缀归一，白名单 ^[a-z0-9]{1,16}$——slide_store.normalize_format_ext
       同口径；不在白名单 → 转 manual_review 不翻转）。storage_layout 保持
       'legacy' 不动（物理迁移是 P6）。
     - 文件缺失 → failed（保留证据：UPDATE asset_state='failed'，输出
       missing_file 报告行）。
     - owner 空 / owner 不在 users / 其它歧义 → **保持 legacy 不动**，输出
       manual_review 报告行（不回落认领平台 owner）。
  2. 关系回填（可信映射部分；全部幂等 UPDATE/INSERT ... WHERE slide_id IS
     NULL）：
     - project_slides / rois / comments / change_log / run_grants /
       ai_session_principals / annotation_access_events / audit_events 的
       slide_id：按 slides.legacy_filename → slide_id 当前映射回填（任何
       asset_state 的行都算映射源——tombstone 成员关系也保留，授权门禁按
       state 拒绝）；映射不到的名保持 NULL = unresolved，计数报告。
     - shares.slides JSONB → share_slides：逐 token 展开名数组，映射到当前
       slide_id（含 tombstone 行）；映射不到的名跳过并计数；position 按数组
       序；INSERT ... ON CONFLICT DO NOTHING。
     - slide_view_grants.slide_id 已有 0035 回填；本脚本只统计仍 NULL 的行数
       （unresolved 孤儿授权），**不再按名匹配**（R-06：旧名授权不重绑）。
  3. 输出：stdout 人读摘要 + --report <path> 可选落 JSON（计数：
     flipped_ready / failed / manual_review / unresolved 各分类、各关系表
     回填/跳过数）。--apply 下每批一个事务；单资产失败不拖垮批次（SAVEPOINT
     回滚到资产级，记录后继续）。退出码：0=完成（允许有 manual_review/
     unresolved 计数）；1=工具错误。
  4. 安全：**绝不 UPDATE legacy_filename；绝不 INSERT slides 行；不改
     storage_layout**（三条硬约束见 _READY_SQL/_FAILED_SQL 的列清单）。

运行形态
  - 默认 **dry-run**（只读统计与计划输出；会话级
    ``SET default_transaction_read_only = on`` 硬保证不写库）；``--apply``
    才写库。
  - 可重跑、分批（--batch-size，默认 500）；**checkpoint 即 DB 状态本身**：
    每批按 slide_id 键集分页扫 asset_state='legacy' 的行，翻转后不再被扫到，
    中断可续；manual_review 行保持 legacy（每次重跑都会重新报告——无副作用，
    报告计数为当次扫描结果）。
  - UPLOAD_DIR 用 --upload-dir 或 env UPLOAD_DIR（两者都缺 → 工具错误）。
  - 只 stat/读文件，**不移动文件**（物理迁移是 P6）；入口文件按 is_file()
    判存在（跟随符号链接——符号链接形态的存量问题由 P0 审计工具另报，
    本脚本不因此拒绝）。
  - 并发安全：翻转是带 ``asset_state='legacy'`` 谓词的真实 CAS（与
    slide_store 状态机同口径），不与 publish/delete 的 advisory 锁互斥也能
    安全重入；CAS 未命中（行已被并发迁移）计 concurrent_skips，不猜状态。

用法::

    python scripts/backfill_slide_asset_state.py --upload-dir /data/uploads \\
        [--apply] [--batch-size 500] [--report /var/backfill/report.json] \\
        [--database-url postgresql://...]

worker 安全：本脚本只 import pg_store / slide_store（worker 安全模块），
不得 import app。

退出码：0=完成（允许存在 manual_review/unresolved 计数）；1=工具错误
（参数/连接/文件系统/报告落盘故障）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 直接运行 scripts/xxx.py 时仓根不在 sys.path——先补齐再 import 业务安全模块
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg  # noqa: E402  worker 安全依赖（pg_store 同款）
import pg_store  # noqa: E402  worker 安全模块（连接/事务）
import slide_store  # noqa: E402  worker 安全模块（SlideState/normalize_format_ext）

TOOL_VERSION = "1.0.0-p1"

EXIT_OK = 0
EXIT_TOOL_ERROR = 1

DEFAULT_BATCH_SIZE = 500

#: MRXS 伴侣目录口径：``x.mrxs`` 的伴侣是同 stem 目录 ``x/``
#: （app.py api_slide_delete 同款；audit 工具 MRXS_EXT 同源）。
MRXS_SUFFIX = ".mrxs"

#: 转换 sidecar 命名（app.py _cleanup_conversion_sidecars 同源；属可重建
#: 派生数据，但占物理字节 → 计入 accounted_bytes）。
MANIFEST_SUFFIX = ".manifest.json"
ASSOCIATED_SUFFIX = ".associated"

#: 关系回填表清单：(表名, legacy 名列)。表/列名来自 0067 已有 schema 的
#: 固定白名单——SQL 拼接仅限此处，不接受外部输入。
RELATION_TABLES = (
    ("project_slides", "slide"),
    ("rois", "slide"),
    ("comments", "slide"),
    ("change_log", "slide"),
    ("run_grants", "slide"),
    ("ai_session_principals", "slide"),
    ("annotation_access_events", "slide"),
    ("audit_events", "slide"),
)

#: 资产翻转 UPDATE 的列清单（安全约束 §6.4：只此几列——
#: 绝不触碰 legacy_filename / storage_layout；绝不 INSERT slides）。
_READY_SQL = (
    "UPDATE slides SET asset_state=%s, published_at=COALESCE(%s, now()), "
    "accounted_bytes=%s, original_filename=%s, display_name=%s, "
    "format_ext=%s, updated_at=now() "
    "WHERE slide_id=%s AND asset_state=%s"
)
_FAILED_SQL = (
    "UPDATE slides SET asset_state=%s, updated_at=now() "
    "WHERE slide_id=%s AND asset_state=%s"
)


class BackfillError(Exception):
    """工具自身错误（参数 / 连接 / 文件系统 / 报告落盘）→ 退出码 1。"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 文件侧：只 stat/读，绝不移动（物理迁移是 P6）
# --------------------------------------------------------------------------- #
def _dir_bytes(path: Path) -> int:
    """目录内全部常规文件的字节合计（不跟随目录符号链接，防环）。"""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for fname in files:
            total += (Path(root) / fname).stat().st_size
    return total


def stat_asset(upload_dir: Path, legacy_filename: str) -> dict:
    """入口文件 + MRXS 伴侣目录 + 转换 sidecar 的 stat 汇总（只读）。

    返回 ``{"accounted_bytes": int, "mtime": float}``；入口文件缺失抛
    FileNotFoundError；其它文件系统故障抛 OSError（调用方按 asset_error
    记录，资产保持 legacy）。
    """
    entry = upload_dir / legacy_filename
    if not entry.is_file():
        raise FileNotFoundError(str(entry))
    st = entry.stat()
    total = st.st_size
    if legacy_filename.lower().endswith(MRXS_SUFFIX):
        companion = upload_dir / legacy_filename[: -len(MRXS_SUFFIX)]
        if companion.is_dir():
            total += _dir_bytes(companion)
    man = upload_dir / (legacy_filename + MANIFEST_SUFFIX)
    if man.is_file():
        total += man.stat().st_size
    assoc = upload_dir / (legacy_filename + ASSOCIATED_SUFFIX)
    if assoc.is_dir():
        total += _dir_bytes(assoc)
    return {"accounted_bytes": total, "mtime": st.st_mtime}


def _unsafe_filename(name) -> bool:
    """legacy_filename 的路径安全闸门：拒绝空值、目录语义与穿越片段。"""
    return (not name) or name in (".", "..") or "/" in name or "\\" in name


def _resolve_display_name(alias, display_name, legacy_filename) -> str:
    """R-02 回填规则：非空 alias → alias，否则现有 display_name，否则
    legacy_filename。值按原样回写（存量数据保全，不做截断/改写）。"""
    if (alias or "").strip():
        return alias
    if (display_name or "").strip():
        return display_name
    return legacy_filename


def classify_asset(row, users_by_id, upload_dir: Path) -> dict:
    """单个 legacy 行 → 处置裁决（纯判定 + 文件 stat；不写库）。

    返回 ``{"action": "ready"|"failed"|"manual_review", "reason": str|None,
    "plan": dict|None}``；ready 的 plan 含全部回填列值。判定序（合同 §6.1
    行文序）：缺文件 → failed 优先于 owner 歧义；owner 歧义与非法 format_ext
    → manual_review（保持 legacy 不动）。
    """
    legacy_filename = row["legacy_filename"]
    if _unsafe_filename(legacy_filename):
        return {"action": "manual_review",
                "reason": "no_legacy_filename" if not legacy_filename
                else "unsafe_legacy_filename",
                "plan": None}
    try:
        files = stat_asset(upload_dir, legacy_filename)
    except FileNotFoundError:
        return {"action": "failed", "reason": "missing_file", "plan": None}

    owner = row["owner_user_id"]
    if not owner:
        return {"action": "manual_review", "reason": "owner_missing",
                "plan": None}
    user = users_by_id.get(owner)
    if user is None:
        return {"action": "manual_review", "reason": "owner_unknown",
                "plan": None}
    if user["disabled"]:
        return {"action": "manual_review", "reason": "owner_disabled",
                "plan": None}

    try:
        ext = slide_store.normalize_format_ext(Path(legacy_filename).suffix)
    except ValueError:
        return {"action": "manual_review", "reason": "bad_format_ext",
                "plan": None}

    return {
        "action": "ready",
        "reason": None,
        "plan": {
            "slide_id": row["slide_id"],
            "published_at": datetime.fromtimestamp(
                files["mtime"], tz=timezone.utc),
            "accounted_bytes": int(files["accounted_bytes"]),
            "original_filename": legacy_filename,
            "display_name": _resolve_display_name(
                row["alias"], row["display_name"], legacy_filename),
            "format_ext": ext,
        },
    }


# --------------------------------------------------------------------------- #
# 阶段 1：资产行翻转（键集分页；checkpoint 即 DB 状态）
# --------------------------------------------------------------------------- #
def _fetch_users_by_id(cur, user_ids):
    if not user_ids:
        return {}
    cur.execute("SELECT user_id, disabled FROM users WHERE user_id = ANY(%s)",
                (sorted(user_ids),))
    return {row["user_id"]: row for row in cur.fetchall()}


def _apply_asset_cas(cur, sql, params) -> int:
    """单资产 CAS UPDATE（SAVEPOINT 包裹：失败回滚到资产级，不拖垮批次）。"""
    cur.execute("SAVEPOINT sp_backfill_asset")
    try:
        cur.execute(sql, params)
        affected = cur.rowcount
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT sp_backfill_asset")
        cur.execute("RELEASE SAVEPOINT sp_backfill_asset")
        raise
    cur.execute("RELEASE SAVEPOINT sp_backfill_asset")
    return affected


def _backfill_assets(conn, upload_dir: Path, apply: bool, batch_size: int,
                     report: dict):
    cur = conn.cursor()
    last_key = ""
    while True:
        cur.execute(
            "SELECT slide_id, legacy_filename, alias, display_name, "
            "owner_user_id FROM slides "
            "WHERE asset_state=%s AND slide_id > %s "
            "ORDER BY slide_id LIMIT %s",
            (slide_store.SlideState.LEGACY, last_key, batch_size))
        rows = cur.fetchall()
        if not rows:
            break
        users_by_id = _fetch_users_by_id(
            cur, {r["owner_user_id"] for r in rows if r["owner_user_id"]})
        for row in rows:
            report["assets"]["scanned"] += 1
            try:
                verdict = classify_asset(row, users_by_id, upload_dir)
            except OSError as exc:
                report["assets"]["asset_errors"] += 1
                report["issues"].append({
                    "kind": "asset_error", "slide_id": row["slide_id"],
                    "legacy_filename": row["legacy_filename"],
                    "reason": "stat_failed", "detail": str(exc)})
                continue
            if verdict["action"] == "manual_review":
                report["assets"]["manual_review"] += 1
                reasons = report["assets"]["manual_review_reasons"]
                reasons[verdict["reason"]] = reasons.get(verdict["reason"], 0) + 1
                report["issues"].append({
                    "kind": "manual_review", "slide_id": row["slide_id"],
                    "legacy_filename": row["legacy_filename"],
                    "reason": verdict["reason"]})
                continue
            if not apply:
                if verdict["action"] == "ready":
                    report["assets"]["flipped_ready"] += 1
                else:
                    report["assets"]["failed_missing_file"] += 1
                    report["issues"].append({
                        "kind": "missing_file", "slide_id": row["slide_id"],
                        "legacy_filename": row["legacy_filename"],
                        "reason": "missing_file"})
                continue
            try:
                if verdict["action"] == "ready":
                    p = verdict["plan"]
                    affected = _apply_asset_cas(cur, _READY_SQL, (
                        slide_store.SlideState.READY, p["published_at"],
                        p["accounted_bytes"], p["original_filename"],
                        p["display_name"], p["format_ext"], p["slide_id"],
                        slide_store.SlideState.LEGACY))
                else:
                    affected = _apply_asset_cas(cur, _FAILED_SQL, (
                        slide_store.SlideState.FAILED, row["slide_id"],
                        slide_store.SlideState.LEGACY))
            except psycopg.Error as exc:
                report["assets"]["asset_errors"] += 1
                report["issues"].append({
                    "kind": "asset_error", "slide_id": row["slide_id"],
                    "legacy_filename": row["legacy_filename"],
                    "reason": "sql_failed", "detail": str(exc).split("\n")[0]})
                continue
            if affected != 1:
                # CAS 未命中：行已被并发进程迁移（publish/delete/上一批重放）
                report["assets"]["concurrent_skips"] += 1
                continue
            if verdict["action"] == "ready":
                report["assets"]["flipped_ready"] += 1
            else:
                report["assets"]["failed_missing_file"] += 1
                report["issues"].append({
                    "kind": "missing_file", "slide_id": row["slide_id"],
                    "legacy_filename": row["legacy_filename"],
                    "reason": "missing_file"})
        if apply:
            conn.commit()  # 每批一个事务（合同 §6.3）；dry-run 会话只读，无需收尾
        last_key = rows[-1]["slide_id"]


# --------------------------------------------------------------------------- #
# 阶段 2：关系回填（可信映射；全部幂等）
# --------------------------------------------------------------------------- #
def _backfill_relations(conn, apply: bool, report: dict):
    cur = conn.cursor()
    for table, name_col in RELATION_TABLES:
        key = table
        if apply:
            try:
                with pg_store.transaction(conn):
                    cur.execute(
                        "UPDATE %s t SET slide_id = s.slide_id "
                        "FROM slides s "
                        "WHERE t.slide_id IS NULL "
                        "AND s.legacy_filename = t.%s" % (table, name_col))
                    backfilled = cur.rowcount
                    cur.execute(
                        "SELECT count(*) AS n FROM %s "
                        "WHERE slide_id IS NULL" % table)
                    unresolved = cur.fetchone()["n"]
            except psycopg.Error as exc:
                raise BackfillError("关系回填失败（%s）：%s"
                                    % (table, str(exc).split("\n")[0])) from exc
        else:
            cur.execute(
                "SELECT count(*) AS n FROM %s t "
                "JOIN slides s ON s.legacy_filename = t.%s "
                "WHERE t.slide_id IS NULL" % (table, name_col))
            backfilled = cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM %s WHERE slide_id IS NULL" % table)
            unresolved = cur.fetchone()["n"]
        report["relations"][key] = {"backfilled": backfilled,
                                    "unresolved": unresolved}


def _backfill_share_slides(conn, apply: bool, batch_size: int, report: dict):
    """shares.slides JSONB → share_slides（逐 token 展开名数组）。

    映射源是 slides.legacy_filename → slide_id 的**当前全量映射**（任何
    asset_state——tombstone/deleted 的成员关系也保留，授权门禁 authorize_read
    按 state 拒绝，R-04）；映射不到的名跳过并计数；position 按数组序；
    INSERT ... ON CONFLICT DO NOTHING 幂等。
    """
    cur = conn.cursor()
    cur.execute("SELECT legacy_filename, slide_id FROM slides "
                "WHERE legacy_filename IS NOT NULL")
    name_map = {r["legacy_filename"]: r["slide_id"] for r in cur.fetchall()}
    cur.execute("SELECT token, slide_id FROM share_slides")
    existing = {(r["token"], r["slide_id"]) for r in cur.fetchall()}

    stats = report["share_slides"]
    last_key = ""
    while True:
        cur.execute("SELECT token, slides FROM shares "
                    "WHERE token > %s ORDER BY token LIMIT %s",
                    (last_key, batch_size))
        rows = cur.fetchall()
        if not rows:
            break
        pending = []  # (token, slide_id, position)——本批待插入
        for row in rows:
            stats["tokens_scanned"] += 1
            names = row["slides"] if isinstance(row["slides"], list) else []
            for position, name in enumerate(names):
                stats["names_seen"] += 1
                slide_id = name_map.get(name) if isinstance(name, str) else None
                if slide_id is None:
                    stats["names_unmapped"] += 1
                    continue
                stats["names_mapped"] += 1
                pending.append((row["token"], slide_id, position))
        if apply and pending:
            try:
                with pg_store.transaction(conn):
                    for token, slide_id, position in pending:
                        cur.execute(
                            "INSERT INTO share_slides (token, slide_id, "
                            "position) VALUES (%s,%s,%s) "
                            "ON CONFLICT DO NOTHING", (token, slide_id,
                                                       position))
                        stats["members_inserted"] += cur.rowcount
                        existing.add((token, slide_id))
            except psycopg.Error as exc:
                raise BackfillError("share_slides 回填失败：%s"
                                    % str(exc).split("\n")[0]) from exc
        elif not apply:
            for token, slide_id, _pos in pending:
                if (token, slide_id) not in existing:
                    stats["members_inserted"] += 1  # dry-run：计划插入数
        last_key = rows[-1]["token"]


def _count_view_grant_orphans(conn, report: dict):
    """slide_view_grants：只统计仍 NULL 的行（0035 已回填；不再按名匹配）。"""
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM slide_view_grants "
                "WHERE slide_id IS NULL")
    report["slide_view_grants"]["unresolved_null_slide_id"] = \
        cur.fetchone()["n"]


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def run_backfill(*, upload_dir, apply=False, batch_size=DEFAULT_BATCH_SIZE,
                 report_path=None, database_url=None) -> dict:
    """执行回填并返回报告 dict（工具错误抛 BackfillError → 退出码 1）。"""
    started = _now_iso()
    up = Path(upload_dir) if upload_dir else None
    if up is None:
        raise BackfillError("缺少 UPLOAD_DIR：请传 --upload-dir 或设 "
                            "UPLOAD_DIR 环境变量")
    if not up.is_dir():
        raise BackfillError("UPLOAD_DIR 不存在或不是目录：%s" % up)

    if database_url:
        # 供显式指定连接串（语义对齐 pg_store.get_conninfo 的 DATABASE_URL
        # 优先级；只设 env，不改 pg_store）
        os.environ["DATABASE_URL"] = database_url

    report = {
        "tool": "backfill_slide_asset_state",
        "version": TOOL_VERSION,
        "mode": "apply" if apply else "dry-run",
        "started_at": started,
        "upload_dir": str(up),
        "batch_size": batch_size,
        "assets": {
            "scanned": 0,
            "flipped_ready": 0,
            "failed_missing_file": 0,
            "manual_review": 0,
            "manual_review_reasons": {},
            "asset_errors": 0,
            "concurrent_skips": 0,
        },
        "relations": {},
        "share_slides": {
            "tokens_scanned": 0,
            "names_seen": 0,
            "names_mapped": 0,
            "names_unmapped": 0,
            "members_inserted": 0,
        },
        "slide_view_grants": {"unresolved_null_slide_id": 0},
        "issues": [],
    }

    try:
        conn = pg_store.connect()
    except Exception as exc:  # noqa: BLE001 - 连接层任何故障=工具错误
        raise BackfillError("数据库连接失败：%s" % exc) from exc
    conn.row_factory = psycopg.rows.dict_row
    try:
        if not apply:
            # dry-run 硬只读：会话级 default_transaction_read_only，
            # 任何写语句直接报错（不只是代码路径不写）
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET default_transaction_read_only = on")
        _backfill_assets(conn, up, apply, batch_size, report)
        _backfill_relations(conn, apply, report)
        _backfill_share_slides(conn, apply, batch_size, report)
        _count_view_grant_orphans(conn, report)
    except BackfillError:
        raise
    except psycopg.Error as exc:
        raise BackfillError("数据库错误：%s" % str(exc).split("\n")[0]) from exc
    except OSError as exc:
        raise BackfillError("文件系统错误：%s" % exc) from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    report["finished_at"] = _now_iso()
    if report_path:
        try:
            path = Path(report_path)
            if path.parent and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2)
                            + "\n", encoding="utf-8")
            report["report_path"] = str(path)
        except OSError as exc:
            raise BackfillError("报告写入失败（%s）：%s"
                                % (report_path, exc)) from exc
    return report


# --------------------------------------------------------------------------- #
# stdout 人读摘要
# --------------------------------------------------------------------------- #
def _emit(msg):
    sys.stdout.write("backfill_slide_asset_state: %s\n" % msg)


def print_summary(report: dict):
    a = report["assets"]
    _emit("模式=%s UPLOAD_DIR=%s batch-size=%d"
          % (report["mode"], report["upload_dir"], report["batch_size"]))
    for issue in report["issues"]:
        if issue["kind"] in ("missing_file", "manual_review"):
            _emit("[%s] %s %s reason=%s"
                  % (issue["kind"], issue["slide_id"],
                     issue.get("legacy_filename"), issue["reason"]))
        elif issue["kind"] == "asset_error":
            _emit("[asset_error] %s %s reason=%s"
                  % (issue["slide_id"], issue.get("legacy_filename"),
                     issue["reason"]))
    _emit("资产：扫描 %d → ready %d / failed(missing_file) %d / "
          "manual_review %d（原因 %s）/ 错误 %d / 并发跳过 %d"
          % (a["scanned"], a["flipped_ready"], a["failed_missing_file"],
             a["manual_review"],
             json.dumps(a["manual_review_reasons"], ensure_ascii=False,
                        sort_keys=True),
             a["asset_errors"], a["concurrent_skips"]))
    for table, counts in report["relations"].items():
        _emit("关系 %s：回填 %d / unresolved %d"
              % (table, counts["backfilled"], counts["unresolved"]))
    s = report["share_slides"]
    _emit("share_slides：token %d 名 %d 映射 %d 未映射 %d %s %d"
          % (s["tokens_scanned"], s["names_seen"], s["names_mapped"],
             s["names_unmapped"],
             "计划插入" if report["mode"] == "dry-run" else "插入",
             s["members_inserted"]))
    _emit("slide_view_grants：NULL slide_id（unresolved 孤儿授权）= %d"
          % report["slide_view_grants"]["unresolved_null_slide_id"])
    _emit("完成（%s）%s"
          % (report["mode"],
             " → %s" % report["report_path"]
             if report.get("report_path") else ""))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="slide ID 化重构 P1 旧数据回填（合同 §6；默认 dry-run）")
    p.add_argument("--apply", action="store_true",
                   help="实际写库（默认 dry-run：只读统计与计划输出）")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help="资产行翻转分批大小（默认 %d；checkpoint 即 DB 状态，"
                        "中断可续）" % DEFAULT_BATCH_SIZE)
    p.add_argument("--upload-dir",
                   help="UPLOAD_DIR 根（缺省用 env UPLOAD_DIR）")
    p.add_argument("--report",
                   help="可选：把计数报告落为 JSON 文件")
    p.add_argument("--database-url",
                   help="可选：PG 连接串（缺省用 env DATABASE_URL / PG* 组合）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.batch_size < 1:
        sys.stderr.write("backfill_slide_asset_state: --batch-size 必须 ≥ 1\n")
        return EXIT_TOOL_ERROR
    try:
        report = run_backfill(
            upload_dir=args.upload_dir or os.environ.get("UPLOAD_DIR"),
            apply=args.apply,
            batch_size=args.batch_size,
            report_path=args.report,
            database_url=args.database_url)
    except BackfillError as exc:
        sys.stderr.write("backfill_slide_asset_state: %s\n" % exc)
        return EXIT_TOOL_ERROR
    print_summary(report)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
