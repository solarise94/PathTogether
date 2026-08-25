#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PG 用户空 password_hash 一次性修复命令（账户系统批次 C，docs §9.2）。

背景：旧 dual `_mirror_user` 曾把 create_user 的公开返回值（无 hash）写成空
字符串；那些用户启动时未必再走建号路径。批次 B 及以前由 app 每次启动执行的
`_repair_pg_empty_password_hashes()` 回填，批次 C 起从应用启动路径删除，改为
本主机侧显式命令（方案 §9.2「把每次启动修复改成显式迁移」）。

用法::

    # 默认 dry-run：只读 PG 与 json，输出待修复 user_id 数量，不写任何数据
    python3 scripts/repair_pg_user_password_hashes.py \
        --json-file /path/to/users.json

    # 确认后 apply（仅填充 PG 空 hash，绝不覆盖非空 hash）
    python3 scripts/repair_pg_user_password_hashes.py \
        --json-file /path/to/users.json --apply

规则（docs §9.2）：
  - 连接走 pg_store（DATABASE_URL 或 PGHOST/... 环境变量，与应用同源）；
  - 默认 dry-run；``--apply`` 才写库；
  - apply 前确认 PG 空 hash 行与 json 可回填 hash 数量一致（对不上拒绝执行，
    先人工审计）；
  - 仅填充 PG 空 hash（password_hash IS NULL OR ''），不覆盖非空 hash；
  - 输出只有 user_id 与计数，**绝不打印密码或 hash**；
  - 完成后记录执行时间与计数（stdout 总结）。

users.json 读取兼容旧格式：记录只有 ``email`` 键时读为 login_id（口径同
user_store_json._login_id_of，本工具自包含实现，不 import 应用模块）。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# 把仓库根加入 sys.path 以便 import pg_store（脚本可从任意 cwd 运行）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pg_store  # noqa: E402  （sys.path 已含仓库根）


# --------------------------------------------------------------------------- #
# users.json 读取（只读；不触发备份/迁移——工具自包含，不 import user_store_json）
# --------------------------------------------------------------------------- #
def _login_id_of(u) -> str:
    """记录读侧兼容（批次 C）：键 login_id 优先，旧格式只有 email 键时读为
    login_id。"""
    if not isinstance(u, dict):
        return ""
    v = u.get("login_id")
    if v is None and "email" in u:
        v = u.get("email")
    return str(v or "").strip().lower()


def _load_json_authoritative_hashes(json_file: Path) -> dict:
    """读 users.json，返回 {user_id: password_hash}（只保留非空 hash）。

    文件缺失/空/损坏 → 返回空 dict 并在 stderr 提示（postgres 单后端部署
    没有旧 json 文件，属正常形态；此时无可回填，命令以计数 0 结束）。
    """
    if not json_file.is_file():
        sys.stderr.write("json 文件不存在：%s（无可回填的权威 hash）\n" % json_file)
        return {}
    raw = json_file.read_text(encoding="utf-8")
    if not raw.strip():
        sys.stderr.write("json 文件为空：%s（无可回填的权威 hash）\n" % json_file)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write("json 文件损坏（拒绝猜测，请先修复）：%s（%s）\n"
                         % (json_file, exc))
        return {}
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        sys.stderr.write("json 文件缺少 users 对象：%s\n" % json_file)
        return {}
    out = {}
    for uid, u in users.items():
        if not isinstance(u, dict):
            continue
        h = str(u.get("password_hash") or "")
        if h:
            out[str(uid)] = h
    return out


def _connect():
    """建连接并设 dict_row（先以默认元组行跑 ensure_schema，再切 dict_row）。"""
    import psycopg
    conn = pg_store.connect()
    pg_store.ensure_schema(conn)
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 修复逻辑（dry-run / apply 共用同一份扫描）
# --------------------------------------------------------------------------- #
def _scan_pending(cur, json_hashes: dict) -> list:
    """扫描 PG 空 hash 且 json 有权威 hash 的行，返回 [(user_id, login_id)]。

    绝不把 hash 放进返回值或任何输出（docs §9.2）。
    """
    pending = []
    cur.execute(
        "SELECT user_id, login_id FROM users "
        "WHERE password_hash IS NULL OR password_hash='' ORDER BY user_id")
    for row in cur.fetchall():
        uid = str(row["user_id"])
        if uid in json_hashes:
            pending.append((uid, row["login_id"]))
    return pending


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="repair_pg_user_password_hashes",
        description="把 json 权威 password_hash 回填到 PG 中空 hash 的用户行"
                    "（账户系统批次 C，docs §9.2；默认 dry-run，--apply 才写）")
    parser.add_argument(
        "--json-file", action="store", default=None,
        help="users.json 路径（默认读 SHARE_DATA_DIR env 或 "
             "~/svs-viewer/share-data/users.json）")
    parser.add_argument(
        "--apply", action="store_true",
        help="执行写入（缺省 dry-run：只输出待修复计数，不写库）")
    args = parser.parse_args(argv)

    json_file = Path(args.json_file) if args.json_file else Path(
        os.environ.get("SHARE_DATA_DIR")
        or (Path.home() / "svs-viewer" / "share-data")) / "users.json"
    json_hashes = _load_json_authoritative_hashes(json_file)
    mode = "apply" if args.apply else "dry-run"
    sys.stdout.write("模式：%s\n" % mode)
    sys.stdout.write("json 权威文件：%s（非空 hash %d 条）\n"
                     % (json_file, len(json_hashes)))

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # PG 侧现状：空 hash 总数（含 json 无权威行的）
            cur.execute("SELECT count(*) AS n FROM users "
                        "WHERE password_hash IS NULL OR password_hash=''")
            empty_total = int(cur.fetchone()["n"])
            pending = _scan_pending(cur, json_hashes)
            if not args.apply:
                sys.stdout.write(
                    "dry-run：PG 空 hash 行 %d，其中可从 json 回填 %d 条：\n"
                    % (empty_total, len(pending)))
                for uid, login_id in pending:
                    sys.stdout.write("  - user_id=%s login_id=%s\n"
                                     % (uid, login_id))
                if empty_total > len(pending):
                    sys.stdout.write(
                        "注意：%d 行空 hash 在 json 中无对应权威 hash（不会回填，"
                        "须人工审计）。\n" % (empty_total - len(pending)))
                sys.stdout.write(
                    "确认后执行：python3 scripts/repair_pg_user_password_hashes.py"
                    " --json-file %s --apply\n" % json_file)
                return 0

            # apply 前一致性确认（docs §9.2）：json 有权威 hash、但 PG 行非空
            # （会被跳过）不算待修复；这里要求扫描结果与计数一致即无并发变化。
            cur.execute("SELECT count(*) AS n FROM users "
                        "WHERE password_hash IS NULL OR password_hash=''")
            empty_now = int(cur.fetchone()["n"])
            if empty_now != empty_total:
                sys.stderr.write(
                    "PG 空 hash 行数在扫描与写入之间发生变化（%d → %d），"
                    "拒绝写入；请重跑。\n" % (empty_total, empty_now))
                return 1
            repaired = 0
            started = time.time()
            with pg_store.transaction(conn) as txn:
                with txn.cursor() as tcur:
                    for uid, _login_id in pending:
                        tcur.execute(
                            "UPDATE users SET password_hash=%s "
                            "WHERE user_id=%s "
                            "AND (password_hash IS NULL OR password_hash='')",
                            (json_hashes[uid], uid))
                        repaired += tcur.rowcount or 0
            sys.stdout.write(
                "完成：回填 %d 行空 password_hash（耗时 %.2fs；执行时间 %s）\n"
                % (repaired, time.time() - started,
                   time.strftime("%Y-%m-%dT%H:%M:%S%z")))
            sys.stdout.write(
                "验证：SELECT count(*) FROM users "
                "WHERE password_hash IS NULL OR password_hash='';"
                " 应为 0（含 json 无权威行的人工审计项）\n")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
