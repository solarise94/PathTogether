# -*- coding: utf-8 -*-
"""为已迁移的 legacy 资产补写内容 revision（slide_assets；2026-10-02 生产修复）。

migrate_slide_storage 1.0.0-p6 在 bound 步只翻转布局，没有写 slide_assets，迁移
资产的内容 revision 为空：渲染令牌、render-context、Demo 与 AI 快照通道对其全部
拒绝（AI slide_info 500）。本工具按发布路径同口径补写
``sha256:<入口文件 sha256 前 16 位>``。

范围（只碰同时满足的行）：asset_state=ready、storage_layout=id_bundle、
legacy_filename 非空（迁移资产）、slide_assets 无行。

证据：入口 sha 取自 bundle 内 manifest.json；``--apply`` 要求 ``--journal``，且
该项在迁移 journal 中已 postverified、journal 记录的入口 sha 与 bundle manifest
一致——任一不符即跳过并记入报告，不猜。

dry-run 默认（会话只读）；``--apply`` 每行一个短事务（advisory 锁 slide:<sid>
→ 复查无行 → 插入）；可重跑（已有行即跳过）。不改 slides 行、配额与授权。

用法::

    python scripts/record_migrated_revisions.py [--apply --journal PATH]
        [--upload-dir DIR] [--database-url URL] [--report PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

import pg_store  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402

_CANDIDATES_SQL = (
    "SELECT s.slide_id FROM slides s WHERE s.asset_state='ready' "
    "AND s.storage_layout='id_bundle' AND s.legacy_filename IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM slide_assets a WHERE a.slide_id=s.slide_id) "
    "ORDER BY s.slide_id")


def _entry_sha(manifest):
    return next((str(f.get("sha256") or "").lower() for f in manifest["files"]
                 if f.get("path") == manifest["entry"]), "")


def _journal_evidence(path):
    """item_id → {postverified: bool, entry_sha: str}（取最近一次 copied 的 manifest）。"""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # 截断尾行（journal 容忍口径）
        if rec.get("record") == "apply_header":
            continue
        ev = out.setdefault(rec.get("item_id"), {"postverified": False, "entry_sha": ""})
        if rec.get("phase") == "postverified" and rec.get("result") == "ok":
            ev["postverified"] = True
        m = (rec.get("detail") or {}).get("manifest")
        if rec.get("phase") == "copied" and rec.get("result") == "ok" and m:
            ev["entry_sha"] = _entry_sha(m)
    return out


def run(*, apply=False, journal=None, upload_dir=None, database_url=None):
    if apply and not journal:
        raise SystemExit("--apply 需要 --journal（迁移证据）")
    root = Path(upload_dir or os.environ.get("UPLOAD_DIR") or "/data/uploads")
    evidence = _journal_evidence(journal) if journal else {}
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    report = {"mode": "apply" if apply else "dry-run", "candidates": 0,
              "recorded": 0, "would_record": 0, "already": 0, "skipped": []}
    try:
        if not apply:
            conn.execute("SET SESSION default_transaction_read_only = on")
        with pg_store.transaction(conn):
            ids = [r["slide_id"] for r in conn.execute(_CANDIDATES_SQL).fetchall()]
        report["candidates"] = len(ids)
        for sid in ids:
            try:
                manifest = slide_storage.validate_manifest(json.loads(
                    (slide_storage.bundle_dir(sid, root=root) / "manifest.json")
                    .read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                report["skipped"].append({"slide_id": sid, "reason": "manifest_unreadable",
                                          "error": str(e)[:120]})
                continue
            sha = _entry_sha(manifest)
            if len(sha) != 64:
                report["skipped"].append({"slide_id": sid, "reason": "entry_sha_missing"})
                continue
            if journal:
                ev = evidence.get(sid)
                if not ev or not ev["postverified"]:
                    report["skipped"].append({"slide_id": sid, "reason": "not_postverified_in_journal"})
                    continue
                if ev["entry_sha"] and ev["entry_sha"] != sha:
                    report["skipped"].append({"slide_id": sid, "reason": "journal_manifest_mismatch"})
                    continue
            if not apply:
                report["would_record"] += 1
                continue
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    slide_store.acquire_slide_lock(cur, sid)
                    cur.execute("SELECT asset_state, storage_layout FROM slides "
                                "WHERE slide_id=%s", (sid,))
                    row = cur.fetchone()
                    cur.execute("SELECT 1 FROM slide_assets WHERE slide_id=%s LIMIT 1", (sid,))
                    if cur.fetchone() is not None:
                        report["already"] += 1
                        continue
                    if not row or row["asset_state"] != "ready" or row["storage_layout"] != "id_bundle":
                        report["skipped"].append({"slide_id": sid, "reason": "row_drift"})
                        continue
                slide_store.record_revision(sid, "sha256:%s" % sha[:16], conn=conn)
            report["recorded"] += 1
    finally:
        conn.close()
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--journal", default=None, help="migrate_slide_storage 的 journal（apply 必需）")
    p.add_argument("--upload-dir", default=None)
    p.add_argument("--database-url", default=None)
    p.add_argument("--report", default=None)
    a = p.parse_args(argv)
    rep = run(apply=a.apply, journal=a.journal, upload_dir=a.upload_dir,
              database_url=a.database_url)
    text = json.dumps(rep, ensure_ascii=False, indent=1)
    if a.report:
        Path(a.report).write_text(text, encoding="utf-8")
    print("record_migrated_revisions: %s candidates=%d recorded=%d would_record=%d "
          "already=%d skipped=%d" % (rep["mode"], rep["candidates"], rep["recorded"],
                                     rep["would_record"], rep["already"], len(rep["skipped"])))
    return 0 if not rep["skipped"] else 4


if __name__ == "__main__":
    sys.exit(main())
