#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSONL → PostgreSQL 一次性导入（W2：format_requests PG 权威化）。

    python scripts/migrate_format_requests.py --mode preflight --source <JSONL>
    python scripts/migrate_format_requests.py --mode apply --source <JSONL>

旧 JSONL 形态（每行一条）::

    {"id": ..., "created_at": ISO8601, "user_id": ..., "format_ext": ...,
     "message": ..., "contact": ...,
     "sample": {"name":..., "path":..., "size":..., "sha256":...} | null,
     "mail": {"status": queued|sent|failed|uncertain, "attempts": n,
              "last_error": ..., "updated_at": ...}}

语义：
  - preflight：零业务写入。输出总行数 / 唯一 ID / 坏行 / 同 ID 异内容
    冲突 / 邮件状态分布 / 已在库 ID / 样本缺失清单；
  - apply：先备份源文件（copy 到 ``<source>.bak.<utc>``），按 ID 幂等导入
    （已存在行一律跳过——重复 apply 零新行、零状态回退）。整文件原子：
    任一坏行或同 ID 异内容 → 拒绝整个 apply（exit 1，零部分导入）；
  - 邮件语义：sent/uncertain 原样保留（绝不重发）；queued/failed 导入为
    failed + attempts=MAX（**不排队发送**——历史通知是否补发由人工决定）；
  - 样本缺失：请求照常导入，sample_missing=true。
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import format_request_store as frs  # noqa: E402
import pg_store  # noqa: E402
import psycopg  # noqa: E402

_MAIL_KEEP = ("sent", "uncertain")


def _parse_line(line, lineno):
    """单行 → 记录 dict；坏行抛 ValueError（附行号）。"""
    try:
        rec = json.loads(line)
    except ValueError as e:
        raise ValueError("第 %d 行不是合法 JSON: %s" % (lineno, e))
    if not isinstance(rec, dict) or not rec.get("id"):
        raise ValueError("第 %d 行缺 id 或不是对象" % lineno)
    return rec


def load_source(path):
    """读源文件：返回 (records, bad_lines, conflicts)。

    conflicts = [(id, lineno1, lineno2)]（同 ID 且归一化内容不同）。
    """
    records = {}
    content_by_id = {}
    bad_lines = []
    conflicts = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = _parse_line(line, lineno)
            except ValueError as e:
                bad_lines.append((lineno, str(e)))
                continue
            rid = str(rec["id"])
            fingerprint = json.dumps(rec, ensure_ascii=False, sort_keys=True)
            if rid in records:
                if content_by_id[rid] != fingerprint:
                    conflicts.append((rid, lineno))
                continue
            records[rid] = rec
            content_by_id[rid] = fingerprint
    return records, bad_lines, conflicts


def _existing_ids(conn):
    """库内已有请求 ID 集合；表不存在（迁移未应用）→ 空集。"""
    with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:
        cur.execute("SELECT to_regclass('format_requests')")
        if cur.fetchone()[0] is None:
            return set()
        cur.execute("SELECT id FROM format_requests")
        return {r[0] for r in cur.fetchall()}


def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def preflight(path, out=sys.stdout):
    records, bad_lines, conflicts = load_source(path)
    existing = None
    conn = psycopg.connect(pg_store.get_conninfo())
    try:
        existing = _existing_ids(conn)
    finally:
        conn.close()
    mail_stat = {}
    missing_samples = []
    for rid, rec in sorted(records.items()):
        mail = rec.get("mail") or {}
        status = str(mail.get("status") or "queued")
        mail_stat[status] = mail_stat.get(status, 0) + 1
        sample = rec.get("sample") or {}
        spath = sample.get("path")
        if spath and not os.path.isfile(str(spath)):
            missing_samples.append((rid, str(spath)))
    dup_ids = 0  # 同 ID 同内容重复行不构成冲突（导入天然去重）
    out.write("preflight 源: %s\n" % path)
    out.write("  可解析记录: %d（唯一 ID: %d）\n"
              % (sum(1 for _ in records), len(records)))
    out.write("  坏行: %d\n" % len(bad_lines))
    for lineno, why in bad_lines[:10]:
        out.write("    line %d: %s\n" % (lineno, why))
    out.write("  同 ID 异内容冲突: %d\n" % len(conflicts))
    for rid, lineno in conflicts[:10]:
        out.write("    id=%s (line %d)\n" % (rid, lineno))
    out.write("  邮件状态分布: %s\n"
              % json.dumps(mail_stat, ensure_ascii=False, sort_keys=True))
    out.write("  已在库 ID: %d / %d\n"
              % (len(existing & set(records)) if existing is not None else 0,
                 len(records)))
    out.write("  样本缺失: %d\n" % len(missing_samples))
    for rid, spath in missing_samples[:10]:
        out.write("    id=%s -> %s\n" % (rid, spath))
    ok = not bad_lines and not conflicts and not dup_ids
    out.write("preflight 结论: %s\n" % ("可导入" if ok else "不可导入"))
    return 0 if ok else 1


def apply_import(path, out=sys.stdout):
    records, bad_lines, conflicts = load_source(path)
    if bad_lines or conflicts:
        out.write("拒绝导入：坏行 %d，同 ID 异内容冲突 %d（零部分导入）\n"
                  % (len(bad_lines), len(conflicts)))
        return 1
    # 备份源文件（apply 幂等，但备份是物理保险）
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = "%s.bak.%s" % (path, stamp)
    shutil.copy2(path, backup)
    out.write("已备份源文件: %s\n" % backup)

    conn = psycopg.connect(pg_store.get_conninfo())
    conn.row_factory = psycopg.rows.dict_row
    inserted = skipped = 0
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                existing = _existing_ids(conn)
                for rid, rec in sorted(records.items()):
                    if rid in existing:
                        skipped += 1  # 幂等：已存在行不动（零状态回退）
                        continue
                    sample = rec.get("sample") or {}
                    spath = sample.get("path")
                    sample_missing = bool(spath) and not os.path.isfile(str(spath))
                    mail = rec.get("mail") or {}
                    mail_status = str(mail.get("status") or "queued")
                    attempts = int(mail.get("attempts") or 0)
                    if mail_status not in _MAIL_KEEP:
                        # queued/failed：不排队发送（attempts 顶格 → 领取
                        # 范围排除），补发与否留人工
                        mail_status = "failed"
                        attempts = frs.MAX_SEND_ATTEMPTS
                    created = _parse_ts(rec.get("created_at"))
                    mail_updated = _parse_ts(mail.get("updated_at"))
                    cur.execute(
                        "INSERT INTO format_requests "
                        "(id, owner_user_id, format_ext, message, contact, "
                        " sample_name, sample_size, sample_sha256, "
                        " sample_internal_ref, sample_missing, "
                        " business_status, created_at, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                        "'submitted', COALESCE(%s, now()), "
                        "COALESCE(%s, %s, now())) "
                        "ON CONFLICT (id) DO NOTHING",
                        (rid, str(rec.get("user_id") or ""),
                         str(rec.get("format_ext") or ""),
                         str(rec.get("message") or ""),
                         str(rec.get("contact") or ""),
                         sample.get("name") or None,
                         int(sample["size"]) if sample.get("size") is not None
                         else None,
                         sample.get("sha256") or None,
                         str(spath) if spath else None,
                         sample_missing,
                         created, mail_updated, created))
                    cur.execute(
                        "INSERT INTO format_request_mail_jobs "
                        "(job_id, request_id, mail_status, attempts, "
                        " last_error, scheduled_at, sent_at, created_at) "
                        "VALUES (%s,%s,%s,%s,%s,now(),%s,"
                        "COALESCE(%s, now())) "
                        "ON CONFLICT (request_id) DO NOTHING",
                        ("frm_m_" + hashlib.sha256(
                            rid.encode("utf-8")).hexdigest()[:24], rid,
                         mail_status, attempts,
                         str(mail.get("last_error"))[:400]
                         if mail.get("last_error") else None,
                         mail_updated if mail_status == "sent" else None,
                         created))
                    inserted += 1
    finally:
        conn.close()
    out.write("apply 完成: 新导入 %d，跳过（已在库）%d\n"
              % (inserted, skipped))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True,
                        choices=("preflight", "apply"))
    parser.add_argument("--source", required=True,
                        help="旧 JSONL 文件路径")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.source):
        sys.stderr.write("源文件不存在: %s\n" % args.source)
        return 1
    if args.mode == "preflight":
        return preflight(args.source)
    return apply_import(args.source)


if __name__ == "__main__":
    sys.exit(main())
