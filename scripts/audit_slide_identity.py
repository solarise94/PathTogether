# -*- coding: utf-8 -*-
"""只读迁移审计工具（slide ID 化重构 P0）。

对 slides 表与 UPLOAD_DIR 根目录做**双向只读核对**，产出迁移前证据：身份
（slide_id / legacy_filename / owner）、文件存在性与 inode 形态（symlink /
hardlink / 特殊文件）、MRXS 伴侣目录与转换 sidecar、授权与业务引用计数、
活跃任务、每 owner 容量对账。对应文档：

  - docs/slide-storage-migration-audit-runbook-20260925.md §2.3（必查项）、
    §3（工具职责与输出契约）
  - docs/slide-id-refactor-p0-inventory-20260925.md §2.5（schema）、§3（裁决口径）
  - docs/slide-id-storage-refactor-agent-plan-20260925.md §6 P0、§7.1（迁移分类）

安全约束（违反即返工）：

  - **只读**：DB 每个事务首句 ``SET TRANSACTION ... READ ONLY``（frozen 模式
    REPEATABLE READ 取一致快照；online 模式 READ COMMITTED，明确非一致快照）。
    文件系统只 lstat / 读：绝不写 UPLOAD_DIR、不跟随符号链接（os.scandir +
    entry.is_symlink() / lstat）、只对 regular file 打开读（不打开设备/FIFO）、
    不执行任何清理。
  - **不 import app / share_store_pg / pg_store 等业务模块**（import app 会起
    worker/建表，业务模块有 probe 副作用）；直接用 psycopg（psycopg3）+
    自写 SQL。连接串解析语义对齐 ``pg_store.get_conninfo``（--database-url →
    DATABASE_URL → PG* 组合），但实现自带、不 import。
  - 输出只落 ``--out-dir``（不存在则创建，目录 0700、文件 0600）；报告**绝不**
    出现分享 token 明文、数据库密码、COS 密钥——授权引用只记条数，不复制 token。

用法::

    python scripts/audit_slide_identity.py --out-dir /var/audit/out \\
        [--mode online|frozen] [--database-url URL] [--upload-dir DIR] \\
        [--page-size 500] [--rate-limit 0] [--quota-tolerance-bytes 0]

模式：

  - online（默认）：浅审——不做全文件哈希，记录扫描起止时间，
    ``consistent_snapshot: false``（READ COMMITTED，逐语句快照，非一致）。
  - frozen：全审——逐 regular file 流式 SHA-256，哈希前后各 lstat 一次比对
    mtime/size 防读取期间变化；``consistent_snapshot: true`` 仅在完全无
    读取期变化且无 incomplete 项时成立（REPEATABLE READ 一致 DB 快照）。

已知限制（P0 口径，终审前须补）：

  - frozen 哈希只覆盖**资产入口文件与孤儿 regular file**；MRXS 伴侣目录 /
    转换 associated 目录只汇总字节数，不做包内逐文件哈希（终审须全包校验）。
  - 容量对账用「slides.owner_user_id 名下入口文件字节合计」近似实际占用
    （ready 语义未存在）；伴侣目录字节另列不计入该对账。

退出码：``0``=正常完成（允许存在 issues）；``1``=工具自身错误（参数/连接/
输出目录故障）；``3``=扫描 incomplete（扫描错误 / 无权限 / 读取期间文件变化 /
分页遗漏）——此时**不得**把结果当全量通过。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat as stat_mod
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

TOOL_VERSION = "1.0.0-p0"

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_INCOMPLETE = 3

# psycopg 延迟探测（缺依赖给中文错误，而非裸 ImportError）。
try:
    import psycopg  # noqa: E402
    import psycopg.rows  # noqa: E402
    _PG_ERRORS = (psycopg.Error,)
except ImportError:  # pragma: no cover - 仅缺依赖时触发
    psycopg = None  # type: ignore
    _PG_ERRORS = ()

# --------------------------------------------------------------------------- #
# 常量：白名单与暂存命名（来源均已注释；调整须同步 docs）
# --------------------------------------------------------------------------- #

#: UPLOAD_DIR 根下**不属于任何资产**的合法路径名，不参与孤儿判定。
#: - ``objects``：计划 §2.2 目标布局的 ID 包根（``objects/<slide_id>/``，P1 落地后出现）；
#: - ``.staging``：计划 §2.2 目标布局的任务暂存根（P3 起落 ``.staging/<task_id>/``）。
WHITELIST_EXACT_NAMES = frozenset({"objects", ".staging"})

#: 后缀级白名单：转换 sidecar（app.py ``_cleanup_conversion_sidecars`` 的
#: ``<canonical>.manifest.json`` 与 ``<canonical>.associated/`` 命名；属可重建
#: 派生数据，runbook §2.3「派生数据」按保留/重建分类，不视为资产本体）。
WHITELIST_SUFFIXES = (".manifest.json", ".associated")

#: 以 ``.`` 开头的根条目视为暂存残留（app.py V1 ``.uploading-*`` / zip
#: ``.extracting-*`` / V2 ``.part-<id>`` / COS ``<job>.part``，见 P0 盘点
#: §2.3.7——暂存目前全部平铺在 UPLOAD_DIR 根且均以点开头）。
#: 记 staging_leftover（info 级），不参与孤儿判定。
STAGING_PREFIX = "."

#: MRXS 伴侣目录后缀（app.py ``api_slide_delete``：``x.mrxs`` 的伴侣目录是
#: 同 stem 目录 ``x/``）。
MRXS_EXT = ".mrxs"

#: 活跃任务状态口径（runbook §2.3「任务」行：在途任务须排空或明确处置）。
UPLOAD_ACTIVE_STATES = ("active", "committing")  # upload_tasks（0017 状态机）
INGESTION_ACTIVE_STATES = ("waiting_capacity", "preparing", "uploading",
                           "completing", "queued", "downloading", "validating")
CONVERSION_ACTIVE_STATES = ("queued", "converting", "validating")
BAIDU_ACTIVE_STAGES = ("queued", "transferring", "downloading",
                       "validating", "converting", "ingesting")

#: 引用计数种类（inventory.references 的键）。按 legacy 名聚合；demo_catalog
#: 与 view_grants_by_id 按 slide_id 聚合（资产装配时另填）。
NAME_REF_KINDS = (
    "shares",                  # shares.slides JSONB 含该名的分享条数
    "share_grants_active",     # 经 shares token 领取且 active 的 grants 条数
    "view_grants_by_name",     # slide_view_grants 按 slide_name 的条数
    "run_grants",              # run_grants.slide 名引用条数
    "ai_session_principals",   # ai_session_principals.slide 名引用条数
    "project_slides",          # project_slides.slide 条数
    "rois",                    # rois.slide 未删条数
    "comments",                # comments.slide 未删条数
    "change_log",              # change_log.slide 条数
    "annotation_access_events",
    "upload_tasks_active",     # 活跃 upload_tasks 按 safe_name
    "ingestion_jobs_active",   # 活跃 ingestion_jobs 按 safe_name/slide_canonical_name
    "conversion_jobs_active",  # 活跃 conversion_jobs 按 source_name/canonical_name
    "baidu_import_items_active",  # 活跃 baidu_import_items 按 slide_name/name
)
ID_REF_KINDS = ("view_grants_by_id", "demo_catalog")
TASK_KINDS = ("upload_tasks_active", "ingestion_jobs_active",
              "conversion_jobs_active", "baidu_import_items_active")
#: 授权类引用（悬空 → unresolved_grant / unresolved）；其余业务引用悬空 →
#: dangling_reference / retain_history（plan §4.2 与 R-04/R-06/R-08 口径）。
GRANT_REF_KINDS = frozenset(("shares", "share_grants_active", "run_grants",
                             "ai_session_principals"))

_HASH_CHUNK = 1 << 20  # 流式哈希读块 1MiB


class AuditError(Exception):
    """工具自身错误（参数 / 连接 / 输出目录）→ 退出码 1。"""


def _err(msg):
    sys.stderr.write("audit_slide_identity: %s\n" % msg)


def _info(msg):
    sys.stdout.write("audit_slide_identity: %s\n" % msg)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mtime_iso(st):
    return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
        timespec="seconds")


def _pg_err(e):
    """PG 错误信息脱敏（优先 diag.message_primary，不回显连接串/密码）。"""
    primary = None
    diag = getattr(e, "diag", None)
    if diag is not None:
        primary = getattr(diag, "message_primary", None)
    return "%s: %s" % (type(e).__name__, primary or str(e).split("\n")[0])


# --------------------------------------------------------------------------- #
# 连接（语义对齐 pg_store.get_conninfo，自带实现，不 import pg_store）
# --------------------------------------------------------------------------- #

def resolve_conninfo(database_url=None):
    """解析 libpq 连接串：--database-url → DATABASE_URL → PG* 组合。"""
    if database_url:
        return database_url
    url = os.environ.get("DATABASE_URL")
    if url and url.strip():
        return url.strip()
    env_map = (("PGHOST", "host"), ("PGPORT", "port"), ("PGUSER", "user"),
               ("PGPASSWORD", "password"), ("PGDATABASE", "dbname"))
    pairs = []
    for env_key, libpq_key in env_map:
        val = os.environ.get(env_key)
        if val:
            escaped = "'" + val.replace("'", "''") + "'"
            pairs.append("%s=%s" % (libpq_key, escaped))
    if not pairs:
        raise AuditError(
            "未配置 PostgreSQL 连接信息：请传 --database-url，或设置 "
            "DATABASE_URL，或 PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE。")
    return " ".join(pairs)


def _connect_readonly(conninfo, mode):
    """建立只读会话并开启主审计事务。

    主事务首句 ``SET TRANSACTION ISOLATION LEVEL <iso> READ ONLY``（模块头
    硬性约束）。在此之前先用一个独立的 READ ONLY 小事务探测
    ``pg_current_snapshot()``（PG13+；旧版失败只损失该证据字段，重开主事务，
    不影响审计本身）。
    """
    if psycopg is None:  # pragma: no cover - 仅缺依赖时触发
        raise AuditError("缺少 psycopg 依赖：请安装 psycopg[binary]>=3.2")
    iso = "REPEATABLE READ" if mode == "frozen" else "READ COMMITTED"
    conn = psycopg.connect(conninfo, autocommit=False)
    conn.row_factory = psycopg.rows.dict_row
    cur = conn.cursor()
    snapshot = None
    try:
        cur.execute("SET TRANSACTION ISOLATION LEVEL %s READ ONLY" % iso)
        cur.execute("SELECT pg_current_snapshot()::text AS snap")
        row = cur.fetchone()
        snapshot = row["snap"] if row else None
        conn.rollback()  # 结束探测小事务
    except psycopg.Error:
        try:
            conn.rollback()
        except psycopg.Error:
            pass
    # 主审计事务（数据读取全部在此事务内）。
    cur.execute("SET TRANSACTION ISOLATION LEVEL %s READ ONLY" % iso)
    return conn, cur, snapshot


# --------------------------------------------------------------------------- #
# 限速器（文件/秒；0=不限）
# --------------------------------------------------------------------------- #

class _RateLimiter:
    def __init__(self, rate):
        self.rate = float(rate or 0.0)
        self._next_at = 0.0

    def tick(self):
        if self.rate <= 0:
            return
        now = time.monotonic()
        wait = self._next_at - now
        if wait > 0:
            time.sleep(wait)
        self._next_at = time.monotonic() + 1.0 / self.rate


# --------------------------------------------------------------------------- #
# 输出（目录 0700、文件 0600；恰好四个文件）
# --------------------------------------------------------------------------- #

def _open_private(path):
    """以 0600 打开输出文件（显式 chmod，不依赖 umask）。"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(str(path), 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


class _JsonlWriter:
    def __init__(self, path):
        self._path = path
        self._f = None

    def __enter__(self):
        self._f = _open_private(self._path)
        return self

    def write(self, obj):
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def __exit__(self, *exc):
        if self._f is not None:
            self._f.close()
        return False


# --------------------------------------------------------------------------- #
# issue 记录（稳定 ID：ISS-<TYPE>-<序号>；序号按确定性扫描顺序分配）
# --------------------------------------------------------------------------- #

class _IssueLog:
    def __init__(self):
        self.items = []
        self._counters = {}

    def add(self, type_, severity, disposition, detail, *,
            slide_id=None, legacy_filename=None, path=None):
        key = type_.upper()
        n = self._counters.get(key, 0) + 1
        self._counters[key] = n
        rec = {
            "issue_id": "ISS-%s-%04d" % (key, n),
            "type": type_,
            "severity": severity,
            "slide_id": slide_id,
            "legacy_filename": legacy_filename,
            "path": path,
            "detail": detail,
            "disposition": disposition,
        }
        self.items.append(rec)
        return rec

    def count(self, severity):
        return sum(1 for i in self.items if i["severity"] == severity)

    def by(self, field):
        out = {}
        for i in self.items:
            out[i[field]] = out.get(i[field], 0) + 1
        return out


# --------------------------------------------------------------------------- #
# 文件系统辅助（只 lstat/读；不跟随符号链接；只打开 regular file）
# --------------------------------------------------------------------------- #

def _stream_sha256(path):
    """流式读 regular file 算 SHA-256（调用方已保证非符号链接的 regular）。"""
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        while True:
            block = f.read(_HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _hash_with_change_check(path, st_before):
    """哈希前后各 lstat 一次，比对 mtime/size 防读取期间变化。

    返回 (sha256|None, changed: bool, error: str|None)。
    """
    try:
        digest = _stream_sha256(path)
    except OSError as e:
        return None, False, "打开/读取失败：%s" % e
    try:
        st_after = os.lstat(path)
    except OSError as e:
        return None, False, "哈希后 lstat 失败：%s" % e
    changed = (st_after.st_mtime_ns != st_before.st_mtime_ns
               or st_after.st_size != st_before.st_size)
    return digest, changed, None


def _scan_root_entries(upload_dir, pacer):
    """os.scandir 根目录：只取 lstat 事实，不跟随符号链接。

    返回 (index, errors)；index: name -> {stat, is_symlink, kind}，
    kind ∈ dir|reg|other（符号链接归 other——不读目标）。
    """
    index = {}
    errors = []
    raw = []
    try:
        with os.scandir(upload_dir) as it:
            try:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        errors.append("lstat %s 失败：%s" % (entry.name, e))
                        continue
                    raw.append((entry.name, entry.is_symlink(), st))
            except OSError as e:
                errors.append("枚举 UPLOAD_DIR 条目失败：%s" % e)
    except OSError as e:
        errors.append("os.scandir(UPLOAD_DIR) 失败：%s" % e)
        return index, errors
    for name, is_symlink, st in raw:
        pacer.tick()
        if stat_mod.S_ISLNK(st.st_mode):
            kind = "other"
        elif stat_mod.S_ISDIR(st.st_mode):
            kind = "dir"
        elif stat_mod.S_ISREG(st.st_mode):
            kind = "reg"
        else:
            kind = "other"  # FIFO/socket/设备——不打开，只记录
        index[name] = {"stat": st, "is_symlink": bool(is_symlink), "kind": kind}
    return index, errors


def _walk_dir_bytes(path, pacer):
    """递归汇总目录字节（scandir + lstat；跳过符号链接；不打开文件）。

    返回 (total_bytes, file_count, errors)。
    """
    total = 0
    count = 0
    errors = []
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    pacer.tick()
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        errors.append("lstat %s 失败：%s" % (entry.name, e))
                        continue
                    if stat_mod.S_ISLNK(st.st_mode):
                        continue  # 不跟随符号链接
                    if stat_mod.S_ISDIR(st.st_mode):
                        stack.append(os.path.join(d, entry.name))
                    elif stat_mod.S_ISREG(st.st_mode):
                        total += st.st_size
                        count += 1
        except OSError as e:
            errors.append("scandir %s 失败：%s" % (d, e))
    return total, count, errors


def _tuple_literal(values):
    """IN (...) 字面量（仅用于上方常量白名单，非用户输入）。"""
    return "(%s)" % ", ".join("'%s'" % v for v in values)


# --------------------------------------------------------------------------- #
# 主审计
# --------------------------------------------------------------------------- #

class _Audit:
    def __init__(self, args):
        self.args = args
        self.mode = args.mode
        self.pacer = _RateLimiter(args.rate_limit)
        self.issues = _IssueLog()
        self.incomplete_reasons = []
        self.snapshot_broken = False  # DB 一致快照被查询故障破坏
        self.counters = {
            "slides": 0, "slides_with_file": 0, "slides_missing_file": 0,
            "orphan_files": 0, "companion_dirs": 0, "staging_leftover": 0,
            "whitelisted_entries": 0, "special_files": 0, "unknown_dirs": 0,
            "root_symlinks": 0, "distinct_owners": 0,
            "slide_logical_bytes": 0, "slide_inode_dedup_bytes": 0,
            "orphan_bytes": 0, "companion_bytes": 0,
        }

    # ---------------- incomplete ---------------- #

    def _incomplete(self, reason):
        if reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)

    # ---------------- DB 采集 ---------------- #

    def collect_db(self, cur, pg_snapshot):
        """DB 侧采集（slides 分页 + 引用聚合 + 配额）。

        事务内任一查询故障：PG 要求先 rollback 才能继续，rollback 即破坏
        frozen 一致快照 → 记 scan_error、标 incomplete、放弃余下 DB 查询
        （已采集部分保留），文件侧仍继续。
        """
        data = {
            "slides": [], "slides_total": None, "id_dups": False,
            "users": {}, "name_aggs": {k: {} for k in NAME_REF_KINDS},
            "view_grants": {}, "vg_by_id": {}, "demo_by_id": {},
            "quotas": {}, "db_name": None, "pg_snapshot": pg_snapshot,
        }
        try:
            cur.execute("SELECT current_database() AS db")
            row = cur.fetchone()
            data["db_name"] = row["db"] if row else None

            # 身份唯一性 + 总数（分页核对基准）。
            cur.execute(
                "SELECT count(*) AS n, count(DISTINCT slide_id) AS d, "
                "count(legacy_filename) AS ln, "
                "count(DISTINCT legacy_filename) AS ld FROM slides")
            stats = cur.fetchone()
            data["slides_total"] = int(stats["n"])
            data["id_dups"] = (int(stats["n"]) != int(stats["d"])
                               or int(stats["ln"]) != int(stats["ld"]))
            last = ""
            while True:
                cur.execute(
                    "SELECT slide_id, legacy_filename, owner_user_id, public "
                    "FROM slides WHERE slide_id > %s "
                    "ORDER BY slide_id LIMIT %s", (last, self.args.page_size))
                page = cur.fetchall()
                if not page:
                    break
                data["slides"].extend(page)
                last = page[-1]["slide_id"]
                if len(page) < self.args.page_size:
                    break
            if len(data["slides"]) != data["slides_total"]:
                # online（READ COMMITTED）翻页期间行集变化 / 快照被破坏。
                self.issues.add(
                    "pagination_gap", "review", "manual_review",
                    "slides 分页取得 %d 行，与总计数 %d 不一致（分页遗漏）"
                    % (len(data["slides"]), data["slides_total"]))
                self._incomplete("pagination_gap")

            # users（owner 存在性 / 禁用判定）。
            cur.execute("SELECT user_id, disabled FROM users")
            for u in cur.fetchall():
                data["users"][u["user_id"]] = bool(u["disabled"])

            # 引用聚合（全部只取条数，不取 token/密钥）。
            sqls = {
                "shares": (
                    "SELECT n.name AS name, count(*) AS cnt "
                    "FROM shares s CROSS JOIN LATERAL "
                    "jsonb_array_elements_text(s.slides) AS n(name) "
                    "GROUP BY n.name"),
                "share_grants_active": (
                    "SELECT n.name AS name, count(*) AS cnt "
                    "FROM grants g JOIN shares s ON s.token = g.token "
                    "CROSS JOIN LATERAL "
                    "jsonb_array_elements_text(s.slides) AS n(name) "
                    "WHERE g.active GROUP BY n.name"),
                "run_grants": ("SELECT slide AS name, count(*) AS cnt "
                               "FROM run_grants GROUP BY slide"),
                "ai_session_principals": (
                    "SELECT slide AS name, count(*) AS cnt "
                    "FROM ai_session_principals "
                    "WHERE slide IS NOT NULL AND slide <> '' GROUP BY slide"),
                "project_slides": ("SELECT slide AS name, count(*) AS cnt "
                                   "FROM project_slides GROUP BY slide"),
                "rois": ("SELECT slide AS name, count(*) AS cnt FROM rois "
                         "WHERE NOT deleted GROUP BY slide"),
                "comments": ("SELECT slide AS name, count(*) AS cnt "
                             "FROM comments WHERE NOT deleted AND slide <> '' "
                             "GROUP BY slide"),
                "change_log": ("SELECT slide AS name, count(*) AS cnt "
                               "FROM change_log GROUP BY slide"),
                "annotation_access_events": (
                    "SELECT slide AS name, count(*) AS cnt "
                    "FROM annotation_access_events GROUP BY slide"),
                "upload_tasks_active": (
                    "SELECT safe_name AS name, count(*) AS cnt "
                    "FROM upload_tasks WHERE state IN %s GROUP BY safe_name"
                    % _tuple_literal(UPLOAD_ACTIVE_STATES)),
                "ingestion_jobs_active": (
                    "SELECT n.name AS name, count(*) AS cnt "
                    "FROM ingestion_jobs j CROSS JOIN LATERAL "
                    "(VALUES (j.safe_name), (j.slide_canonical_name)) AS n(name) "
                    "WHERE j.state IN %s AND n.name IS NOT NULL AND n.name <> '' "
                    "GROUP BY n.name"
                    % _tuple_literal(INGESTION_ACTIVE_STATES)),
                "conversion_jobs_active": (
                    "SELECT n.name AS name, count(*) AS cnt "
                    "FROM conversion_jobs j CROSS JOIN LATERAL "
                    "(VALUES (j.source_name), (j.canonical_name)) AS n(name) "
                    "WHERE j.state IN %s AND n.name IS NOT NULL AND n.name <> '' "
                    "GROUP BY n.name"
                    % _tuple_literal(CONVERSION_ACTIVE_STATES)),
                "baidu_import_items_active": (
                    "SELECT n.name AS name, count(*) AS cnt "
                    "FROM baidu_import_items i CROSS JOIN LATERAL "
                    "(VALUES (i.slide_name), (i.name)) AS n(name) "
                    "WHERE i.stage IN %s AND n.name IS NOT NULL AND n.name <> '' "
                    "GROUP BY n.name"
                    % _tuple_literal(BAIDU_ACTIVE_STAGES)),
            }
            for kind in sorted(sqls):
                cur.execute(sqls[kind])
                data["name_aggs"][kind] = {
                    r["name"]: int(r["cnt"]) for r in cur.fetchall()}

            # slide_view_grants 专项：总量 + 生代绑定异常计数（0035 口径）。
            cur.execute(
                "SELECT g.slide_name AS name, count(*) AS cnt, "
                "count(*) FILTER (WHERE s.slide_id IS NULL "
                "AND g.slide_id IS NULL) AS orphan_null_cnt, "
                "count(*) FILTER (WHERE s.slide_id IS NULL "
                "AND g.slide_id IS NOT NULL) AS orphan_id_cnt, "
                "count(*) FILTER (WHERE s.slide_id IS NOT NULL "
                "AND g.slide_id IS DISTINCT FROM s.slide_id) AS stale_cnt "
                "FROM slide_view_grants g "
                "LEFT JOIN slides s ON s.legacy_filename = g.slide_name "
                "GROUP BY g.slide_name")
            for r in cur.fetchall():
                data["view_grants"][r["name"]] = {
                    "cnt": int(r["cnt"]),
                    "orphan_null": int(r["orphan_null_cnt"]),
                    "orphan_id": int(r["orphan_id_cnt"]),
                    "stale": int(r["stale_cnt"]),
                }
                data["name_aggs"]["view_grants_by_name"][r["name"]] = \
                    int(r["cnt"])
            cur.execute(
                "SELECT slide_id, count(*) AS cnt FROM slide_view_grants "
                "WHERE slide_id IS NOT NULL GROUP BY slide_id")
            for r in cur.fetchall():
                data["vg_by_id"][r["slide_id"]] = int(r["cnt"])

            # demo_catalog（按 slide_id）。
            cur.execute(
                "SELECT slide_id, count(*) AS cnt FROM demo_catalog "
                "GROUP BY slide_id")
            for r in cur.fetchall():
                data["demo_by_id"][r["slide_id"]] = int(r["cnt"])

            # 容量账本。
            cur.execute(
                "SELECT user_id, quota_bytes, used_bytes, reserved_bytes "
                "FROM upload_user_quotas")
            for r in cur.fetchall():
                data["quotas"][r["user_id"]] = {
                    "quota_bytes": int(r["quota_bytes"]),
                    "used_bytes": int(r["used_bytes"]),
                    "reserved_bytes": int(r["reserved_bytes"]),
                }
        except _PG_ERRORS as e:
            self.issues.add(
                "scan_error", "review", "manual_review",
                "数据库采集失败（余下 DB 查询跳过）：%s" % _pg_err(e))
            self._incomplete("db_query_error")
            self.snapshot_broken = True
        return data

    # ---------------- 文件侧装配 ---------------- #

    def emit_inventory(self, inv, data, index, upload_dir):
        """逐 slides 行核对文件；再处理根下孤儿/暂存/特殊/未知目录。

        返回 owner_actual: owner_user_id → 名下资产入口文件字节合计。
        """
        legacy_index = {}
        for row in data["slides"]:
            if row["legacy_filename"]:
                legacy_index[row["legacy_filename"]] = row

        # --- 资产通过（slides 分页序=slide_id 序，确定性） --- #
        inode_map = {}    # (dev, ino) -> [(slide_id, owner, name, size), ...]
        owner_actual = {}
        owners_seen = set()
        for row in data["slides"]:
            sid = row["slide_id"]
            name = row["legacy_filename"]
            owner = row["owner_user_id"]
            self.counters["slides"] += 1
            if owner:
                owners_seen.add(owner)
            if self.mode == "frozen":
                db_ev = ("slides 表键集分页（ORDER BY slide_id；REPEATABLE READ "
                         "READ ONLY 一致快照）")
            else:
                db_ev = ("slides 表键集分页（ORDER BY slide_id；READ COMMITTED "
                         "READ ONLY，非一致快照）")
            rec = {
                "record_type": "slide",
                "slide_id": sid,
                "legacy_filename": name,
                "owner_user_id": owner,
                "public": bool(row["public"]),
                "file_exists": None,
                "file_size": None,
                "file_mtime": None,
                "file_nlink": None,
                "is_symlink": False,
                "sha256": None,
                "has_manifest": False,
                "has_associated_dir": False,
                "has_companion_dir": False,
                "file_dev": None,
                "file_ino": None,
                "references": self._asset_refs(data, sid, name),
                "evidence": {"db": db_ev, "fs": "UPLOAD_DIR 根 lstat"},
            }
            # owner 判定（空 / 不在 users / 已禁用 → 人工确认，不回落认领）。
            if not owner:
                self.issues.add(
                    "owner_missing", "review", "manual_review",
                    "slides 行 owner_user_id 为空（不回落认领，须人工确认）",
                    slide_id=sid, legacy_filename=name)
            elif owner not in data["users"]:
                self.issues.add(
                    "owner_missing", "review", "manual_review",
                    "owner_user_id 不在 users 表（不存在）",
                    slide_id=sid, legacy_filename=name)
            elif data["users"][owner]:
                self.issues.add(
                    "owner_missing", "review", "manual_review",
                    "owner 已被禁用（disabled=true）",
                    slide_id=sid, legacy_filename=name)

            # 文件双向核对（slides.legacy_filename → UPLOAD_DIR/<name>）。
            if name is None:
                self.issues.add(
                    "no_legacy_filename", "review", "manual_review",
                    "slides 行 legacy_filename 为 NULL（新 ID 资产或脏数据，"
                    "本工具无法按名定位文件）", slide_id=sid)
            elif name not in index:
                rec["file_exists"] = False
                self.counters["slides_missing_file"] += 1
                self.issues.add(
                    "missing_file", "blocker", "retain_history",
                    "元数据在、文件不在（UPLOAD_DIR 根下未找到）；保留原 ID/"
                    "旧 alias，不允许新上传接管",
                    slide_id=sid, legacy_filename=name,
                    path=str(upload_dir / name))
            else:
                ent = index[name]
                st = ent["stat"]
                rec.update({
                    "file_exists": True,
                    "file_size": int(st.st_size),
                    "file_mtime": _mtime_iso(st),
                    "file_nlink": int(st.st_nlink),
                    "is_symlink": ent["is_symlink"],
                    "file_dev": int(st.st_dev),
                    "file_ino": int(st.st_ino),
                })
                if ent["is_symlink"]:
                    self.issues.add(
                        "symlink", "blocker", "manual_review",
                        "资产入口是符号链接（不跟随；迁移须复制为隔离受管字节，"
                        "无法确定依赖则阻塞该项）",
                        slide_id=sid, legacy_filename=name,
                        path=str(upload_dir / name))
                elif ent["kind"] != "reg":
                    self.issues.add(
                        "special_file", "blocker", "manual_review",
                        "资产入口不是 regular 文件（kind=%s），不可直接迁移"
                        % ent["kind"],
                        slide_id=sid, legacy_filename=name,
                        path=str(upload_dir / name))
                else:
                    self.counters["slides_with_file"] += 1
                    self.counters["slide_logical_bytes"] += int(st.st_size)
                    if owner:
                        owner_actual[owner] = (owner_actual.get(owner, 0)
                                               + int(st.st_size))
                    inode_map.setdefault((st.st_dev, st.st_ino), []).append(
                        (sid, owner, name, int(st.st_size)))
                    if st.st_nlink > 1:
                        self.issues.add(
                            "hardlink_multi", "review", "manual_review",
                            "入口文件 nlink=%d（与其他路径共享 inode；迁移时须"
                            "复制隔离，不得共享外部可写源 inode）" % st.st_nlink,
                            slide_id=sid, legacy_filename=name,
                            path=str(upload_dir / name))
                    if self.mode == "frozen":
                        digest, changed, err = _hash_with_change_check(
                            str(upload_dir / name), st)
                        if err is not None:
                            self.issues.add(
                                "scan_error", "review", "manual_review",
                                "frozen 哈希失败：%s" % err,
                                slide_id=sid, legacy_filename=name,
                                path=str(upload_dir / name))
                            self._incomplete("scan_error")
                        else:
                            rec["sha256"] = digest
                            if changed:
                                self.issues.add(
                                    "changed_during_read", "review",
                                    "manual_review",
                                    "哈希期间 mtime/size 发生变化，读到的字节与"
                                    "任一端点状态不可证一致；该资产本轮结果作废",
                                    slide_id=sid, legacy_filename=name,
                                    path=str(upload_dir / name))
                                self._incomplete("changed_during_read")

            # 伴侣 / 转换 sidecar 存在性。
            if name:
                rec["has_manifest"] = (name + ".manifest.json") in index
                rec["has_associated_dir"] = (name + ".associated") in index
                if name.lower().endswith(MRXS_EXT):
                    stem = name[:-len(MRXS_EXT)]
                    rec["has_companion_dir"] = (
                        stem in index and index[stem]["kind"] == "dir")
            self._active_task_issue(sid, name, rec["references"])
            inv.write(rec)

        # --- 根下非资产条目（按名排序，确定性） --- #
        companion_stems = {}
        for nm, ent in index.items():
            if ent["kind"] == "reg" and nm.lower().endswith(MRXS_EXT):
                companion_stems[nm[:-len(MRXS_EXT)]] = nm
        for name in sorted(index):
            ent = index[name]
            if name in legacy_index:
                continue  # 资产已在上文处理
            if name in WHITELIST_EXACT_NAMES:
                self.counters["whitelisted_entries"] += 1
                continue
            if name.lower().endswith(WHITELIST_SUFFIXES):
                self.counters["whitelisted_entries"] += 1
                continue
            if name.startswith(STAGING_PREFIX):
                # 暂存残留（.uploading-/.extracting-/.part- 等）：只计数+info，
                # 不参与孤儿判定；处置属迁移窗口清理计划，本工具不删除。
                self.counters["staging_leftover"] += 1
                self.issues.add(
                    "staging_leftover", "info", "quarantine",
                    "暂存命名残留（点开头）；迁移窗口按清理计划处置，本工具"
                    "不自动删除", path=str(upload_dir / name))
                continue
            if ent["kind"] == "dir" and not ent["is_symlink"] \
                    and name in companion_stems:
                slide_file = companion_stems[name]
                asset = legacy_index.get(slide_file)
                self._emit_companion(
                    inv, upload_dir, name, index,
                    slide_id=asset["slide_id"] if asset else None,
                    owner=asset["owner_user_id"] if asset else None,
                    via=slide_file)
                continue
            if ent["is_symlink"]:
                self.counters["root_symlinks"] += 1
                self.issues.add(
                    "symlink", "review", "quarantine",
                    "根下符号链接（不跟随，不猜目标归属）；隔离报告",
                    path=str(upload_dir / name))
                continue
            if ent["kind"] == "dir":
                self.counters["unknown_dirs"] += 1
                self.issues.add(
                    "orphan_dir", "review", "manual_review",
                    "根下目录不属于任何资产伴侣/白名单（未知派生数据或人工放置，"
                    "须分类保留/重建/独立导出）",
                    path=str(upload_dir / name))
                continue
            if ent["kind"] != "reg":
                self.counters["special_files"] += 1
                self.issues.add(
                    "special_file", "review", "manual_review",
                    "根下特殊文件（FIFO/socket/设备；不打开，仅记录）",
                    path=str(upload_dir / name))
                continue
            # 孤儿 regular file：文件在、元数据不在 → 隔离。
            st = ent["stat"]
            self.counters["orphan_files"] += 1
            self.counters["orphan_bytes"] += int(st.st_size)
            stem = name[:-len(MRXS_EXT)] if name.lower().endswith(MRXS_EXT) \
                else None
            rec = {
                "record_type": "orphan_file",
                "slide_id": None,
                "legacy_filename": name,
                "owner_user_id": None,  # 未知归属——不猜（plan §7.1）
                "public": None,
                "file_exists": True,
                "file_size": int(st.st_size),
                "file_mtime": _mtime_iso(st),
                "file_nlink": int(st.st_nlink),
                "is_symlink": False,
                "sha256": None,
                "has_manifest": (name + ".manifest.json") in index,
                "has_associated_dir": (name + ".associated") in index,
                "has_companion_dir": (
                    stem is not None and stem in index
                    and index[stem]["kind"] == "dir"),
                "file_dev": int(st.st_dev),
                "file_ino": int(st.st_ino),
                "references": {k: data["name_aggs"][k].get(name, 0)
                               for k in TASK_KINDS},
                "evidence": {
                    "fs": "os.scandir 根枚举 + lstat（不在 slides."
                          "legacy_filename、非白名单、非暂存命名）",
                },
            }
            if st.st_nlink > 1:
                self.issues.add(
                    "hardlink_multi", "review", "manual_review",
                    "孤儿文件 nlink=%d（与其他路径共享 inode）" % st.st_nlink,
                    legacy_filename=name, path=str(upload_dir / name))
                inode_map.setdefault((st.st_dev, st.st_ino), []).append(
                    (None, None, name, int(st.st_size)))
            if self.mode == "frozen":
                digest, changed, err = _hash_with_change_check(
                    str(upload_dir / name), st)
                if err is not None:
                    self.issues.add(
                        "scan_error", "review", "manual_review",
                        "frozen 哈希失败：%s" % err,
                        legacy_filename=name, path=str(upload_dir / name))
                    self._incomplete("scan_error")
                else:
                    rec["sha256"] = digest
                    if changed:
                        self.issues.add(
                            "changed_during_read", "review", "manual_review",
                            "哈希期间 mtime/size 发生变化",
                            legacy_filename=name, path=str(upload_dir / name))
                        self._incomplete("changed_during_read")
            self.issues.add(
                "orphan_file", "review", "quarantine",
                "文件在、元数据不在（隔离，不猜 owner，不自动进入用户列表）",
                legacy_filename=name, path=str(upload_dir / name))
            if any(rec["references"].get(k, 0) for k in TASK_KINDS):
                self._active_task_issue(None, name, rec["references"])
            inv.write(rec)

        # --- inode 共享 / 归属歧义 --- #
        for (dev, ino), members in sorted(
                inode_map.items(), key=lambda kv: sorted(kv[1])[0][2]):
            if len(members) < 2:
                continue
            owners = {m[1] for m in members if m[1] is not None}
            sids = sorted(m[0] for m in members if m[0])
            if len(owners) > 1 and len(sids) > 1:
                self.issues.add(
                    "owner_ambiguous", "blocker", "manual_review",
                    "多个 slides 行的入口文件共享同一 inode (dev=%d, ino=%d)"
                    "且 owner 不同（%d 方）——字节归属矛盾，须人工确认，"
                    "不回落认领" % (dev, ino, len(owners)),
                    slide_id=sids[0])
        # inode 去重物理量（runbook §3：hardlink 去重数字不得当用户配额）。
        dedup = 0
        for members in inode_map.values():
            if any(m[0] for m in members):  # 组内含资产入口才计入资产口径
                dedup += members[0][3]
        self.counters["slide_inode_dedup_bytes"] = dedup
        self.counters["distinct_owners"] = len(owners_seen)
        return owner_actual

    def _emit_companion(self, inv, upload_dir, name, index, *,
                        slide_id, owner, via):
        total, count, errors = _walk_dir_bytes(upload_dir / name, self.pacer)
        for e in errors:
            self.issues.add(
                "scan_error", "review", "manual_review",
                "伴侣目录遍历失败：%s" % e, slide_id=slide_id,
                path=str(upload_dir / name))
            self._incomplete("scan_error")
        self.counters["companion_dirs"] += 1
        self.counters["companion_bytes"] += total
        st = index[name]["stat"]
        inv.write({
            "record_type": "companion_dir",
            "slide_id": slide_id,
            "legacy_filename": name,
            "owner_user_id": owner,
            "public": None,
            "file_exists": True,
            "file_size": total,        # 包内 regular file 字节合计
            "file_mtime": _mtime_iso(st),
            "file_nlink": None,
            "is_symlink": False,
            "sha256": None,            # 伴侣目录不做逐文件哈希（见模块头限制）
            "has_manifest": False,
            "has_associated_dir": False,
            "has_companion_dir": False,
            "file_dev": int(st.st_dev),
            "file_ino": int(st.st_ino),
            "companion_file_count": count,
            "companion_of": via,
            "references": {},
            "evidence": {
                "fs": "MRXS 同 stem 伴侣目录（app.py api_slide_delete 命名）；"
                      "scandir 递归汇总（follow_symlinks=False）",
            },
        })

    def _asset_refs(self, data, sid, name):
        refs = {}
        for kind in NAME_REF_KINDS:
            refs[kind] = data["name_aggs"][kind].get(name, 0) if name else 0
        refs["view_grants_by_id"] = data["vg_by_id"].get(sid, 0)
        refs["demo_catalog"] = data["demo_by_id"].get(sid, 0)
        return refs

    def _active_task_issue(self, sid, name, refs):
        active = {k: refs.get(k, 0) for k in TASK_KINDS
                  if refs.get(k, 0) > 0}
        if not active:
            return
        self.issues.add(
            "active_task", "review", "manual_review",
            "存在活跃任务引用该名（%s）；停写窗口须排空或明确取消并完成清理"
            % ", ".join("%s=%d" % kv for kv in sorted(active.items())),
            slide_id=sid, legacy_filename=name)

    # ---------------- 引用悬空 / 授权绑定 ---------------- #

    def emit_reference_issues(self, data, disk_names):
        """名字引用悬空 + slide_view_grants 生代异常 + demo_catalog 悬空。

        disk_names：UPLOAD_DIR 根下实际存在的名字集合——活跃任务引用的悬空
        名若在盘（孤儿），已在孤儿通过中报告，这里不重复。
        """
        legacy_names = {r["legacy_filename"] for r in data["slides"]
                        if r["legacy_filename"]}
        sid_index = {r["slide_id"] for r in data["slides"]}
        for kind in sorted(NAME_REF_KINDS):
            if kind == "view_grants_by_name":
                continue  # 专项查询单独处理
            agg = data["name_aggs"][kind]
            for name in sorted(agg):
                if not name or name in legacy_names:
                    continue
                cnt = agg[name]
                if kind in GRANT_REF_KINDS:
                    self.issues.add(
                        "unresolved_grant", "review", "unresolved",
                        "%s 引用 %d 条指向无 slides 行的旧名；无法唯一对应可信"
                        "历史资产，不根据当前同名文件自动补绑（token 不入报告）"
                        % (kind, cnt), legacy_filename=name)
                elif kind in TASK_KINDS:
                    if name in disk_names:
                        continue  # 孤儿文件已报
                    self.issues.add(
                        "active_task", "review", "manual_review",
                        "%s 有 %d 条活跃任务引用无 slides 行的名字（且盘上无"
                        "该文件）" % (kind, cnt), legacy_filename=name)
                else:
                    self.issues.add(
                        "dangling_reference", "info", "retain_history",
                        "%s 引用 %d 条指向无 slides 行的旧名（历史文本保留原文，"
                        "不重写成猜测 ID）" % (kind, cnt),
                        legacy_filename=name)
        # slide_view_grants 生代绑定异常（0035 语义）。
        for name in sorted(data["view_grants"]):
            info = data["view_grants"][name]
            if name in legacy_names:
                if info["stale"] > 0:
                    self.issues.add(
                        "unresolved_grant", "review", "unresolved",
                        "slide_view_grants 有 %d 条的 slide_id 与当前资产生代不"
                        "一致（stale 绑定）" % info["stale"],
                        legacy_filename=name)
            else:
                if info["orphan_null"] > 0:
                    self.issues.add(
                        "unresolved_grant", "review", "unresolved",
                        "slide_view_grants 有 %d 条孤儿授权（无 meta 行且 "
                        "slide_id 为 NULL，当前仍生效）；迁移须逐条决议"
                        % info["orphan_null"], legacy_filename=name)
                if info["orphan_id"] > 0:
                    self.issues.add(
                        "unresolved_grant", "review", "unresolved",
                        "slide_view_grants 有 %d 条指向的 slide_id 无对应 "
                        "slides 行" % info["orphan_id"], legacy_filename=name)
        # demo_catalog 悬空（按 slide_id）。
        for sid in sorted(data["demo_by_id"]):
            if sid not in sid_index:
                self.issues.add(
                    "unresolved_grant", "review", "unresolved",
                    "demo_catalog 成员的 slide_id 无对应 slides 行（%d 条）"
                    % data["demo_by_id"][sid], slide_id=sid)

    # ---------------- 容量对账 ---------------- #

    def emit_quota_issues(self, data, owner_actual):
        """used_bytes vs 名下资产入口文件字节合计（近似口径，见模块头）。"""
        tolerance = int(self.args.quota_tolerance_bytes)
        owners = sorted(set(data["quotas"]) | {o for o in owner_actual if o})
        for owner in owners:
            used = data["quotas"].get(owner, {}).get("used_bytes", 0)
            reserved = data["quotas"].get(owner, {}).get("reserved_bytes", 0)
            actual = owner_actual.get(owner, 0)
            diff = used - actual
            if abs(diff) > tolerance:
                self.issues.add(
                    "quota_mismatch", "review", "manual_review",
                    "owner=%s used_bytes=%d，名下资产入口文件合计=%d，差值=%+d"
                    "（reserved=%d；0013 口径删除不回退 used，差异须单独核准，"
                    "不静默归零；伴侣目录字节另列不计入）"
                    % (owner, used, actual, diff, reserved))

    # ---------------- verification / summary ---------------- #

    def consistent_snapshot(self):
        if self.mode != "frozen":
            return False
        return not self.incomplete_reasons and not self.snapshot_broken

    def verification(self, args, started, finished, scan_id, upload_dir,
                     data, conninfo_source):
        return {
            "tool_version": TOOL_VERSION,
            "mode": self.mode,
            "scan_version": "audit/%s/%s/%s" % (TOOL_VERSION, self.mode,
                                                scan_id),
            "scan_id": scan_id,
            "started_at": started,
            "finished_at": finished,
            "incomplete": bool(self.incomplete_reasons),
            "incomplete_reasons": list(self.incomplete_reasons),
            "consistent_snapshot": self.consistent_snapshot(),
            "db": {
                "isolation": ("REPEATABLE READ READ ONLY"
                              if self.mode == "frozen"
                              else "READ COMMITTED READ ONLY（非一致快照）"),
                "current_database": data.get("db_name"),
                "pg_snapshot": data.get("pg_snapshot"),
                "conninfo_source": conninfo_source,
                "page_size": args.page_size,
            },
            "upload_dir": str(upload_dir),
            "params": {
                "rate_limit_files_per_sec": args.rate_limit,
                "quota_tolerance_bytes": args.quota_tolerance_bytes,
            },
            "counts": dict(self.counters),
            "issues_total": len(self.issues.items),
            "issues_by_severity": {
                s: self.issues.count(s)
                for s in ("blocker", "review", "info")},
            "issues_by_type": self.issues.by("type"),
            "issues_by_disposition": self.issues.by("disposition"),
        }

    def summary_md(self, verif):
        c = verif["counts"]
        sev = verif["issues_by_severity"]
        by_type = verif["issues_by_type"]
        lines = []
        lines.append("# 切片身份迁移审计摘要（P0 只读预审）")
        lines.append("")
        lines.append("- 工具版本：`%s`（scan `%s`）"
                     % (verif["tool_version"], verif["scan_version"]))
        lines.append("- 模式：`%s`（%s）"
                     % (verif["mode"],
                        "一致快照" if verif["consistent_snapshot"]
                        else "非一致快照（online 浅审或存在 incomplete）"))
        lines.append("- 扫描起止：%s → %s"
                     % (verif["started_at"], verif["finished_at"]))
        lines.append("- incomplete：**%s**%s"
                     % ("是" if verif["incomplete"] else "否",
                        ("（原因：%s）"
                         % ", ".join(verif["incomplete_reasons"]))
                        if verif["incomplete_reasons"] else ""))
        lines.append("")
        lines.append("## 资产与文件计数（脱敏，仅计数）")
        lines.append("")
        lines.append("| 项 | 数量 |")
        lines.append("|---|---|")
        lines.append("| 逻辑资产（slides 行） | %d |" % c["slides"])
        lines.append("| 其中：入口文件在 | %d |" % c["slides_with_file"])
        lines.append("| 其中：缺文件（missing_file） | %d |"
                     % c["slides_missing_file"])
        lines.append("| 孤儿文件（无元数据） | %d |" % c["orphan_files"])
        lines.append("| MRXS 伴侣目录 | %d |" % c["companion_dirs"])
        lines.append("| 未知目录（非伴侣/非白名单） | %d |" % c["unknown_dirs"])
        lines.append("| 暂存残留（点开头） | %d |" % c["staging_leftover"])
        lines.append("| 白名单条目（目标布局根/转换 sidecar） | %d |"
                     % c["whitelisted_entries"])
        lines.append("| 根下符号链接 | %d |" % c["root_symlinks"])
        lines.append("| 特殊文件（FIFO/设备等） | %d |" % c["special_files"])
        lines.append("| 涉及 owner 数（非空，去重） | %d |"
                     % c["distinct_owners"])
        lines.append("")
        lines.append("## 字节口径（分开列报，不得混用）")
        lines.append("")
        lines.append("| 口径 | 字节 |")
        lines.append("|---|---|")
        lines.append("| 资产入口文件逻辑字节（每链接计） | %d |"
                     % c["slide_logical_bytes"])
        lines.append("| 资产入口文件 inode 去重字节 | %d |"
                     % c["slide_inode_dedup_bytes"])
        lines.append("| 伴侣目录包内字节 | %d |" % c["companion_bytes"])
        lines.append("| 孤儿文件字节 | %d |" % c["orphan_bytes"])
        lines.append("")
        lines.append("## 问题计数")
        lines.append("")
        lines.append("- 总数：%d（blocker %d / review %d / info %d）"
                     % (verif["issues_total"], sev["blocker"], sev["review"],
                        sev["info"]))
        if by_type:
            lines.append("")
            lines.append("| issue 类型 | 数量 |")
            lines.append("|---|---|")
            for t in sorted(by_type):
                lines.append("| %s | %d |" % (t, by_type[t]))
        lines.append("")
        lines.append("## go / no-go 提示")
        lines.append("")
        if verif["incomplete"]:
            lines.append("- **no-go（本轮无效）**：扫描 incomplete，结果不得"
                         "当作全量通过，须修复后重审。")
        elif sev["blocker"] > 0:
            lines.append("- **no-go**：存在 %d 项阻断（missing_file / symlink /"
                         " owner_ambiguous / 非常规入口等）；须清零或形成书面"
                         "隔离决议后重审。" % sev["blocker"])
        elif sev["review"] > 0:
            lines.append("- **暂缓**：%d 项复核项须逐项决议（隔离/保留/核准）"
                         "后方可进入停写终审。" % sev["review"])
        else:
            lines.append("- **预审通过（无阻断/复核项）**。在线预审不能替代"
                         "停写终审：迁移须以 frozen 全审 + 独立验证为准。")
        lines.append("")
        lines.append("> 本摘要为脱敏计数，不含文件名 / 用户 ID / token；逐项"
                     "证据见同目录 inventory.jsonl 与 issues.jsonl（0600）。")
        lines.append("")
        return "\n".join(lines)


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="只读迁移审计：slides↔UPLOAD_DIR 双向核对、引用/任务/配额"
                    "对账（不写库、不写源目录、不清理）")
    p.add_argument("--database-url", default=None,
                   help="PG 连接串；缺省用 DATABASE_URL / PG* env")
    p.add_argument("--upload-dir", default=None,
                   help="UPLOAD_DIR；缺省用 UPLOAD_DIR env 或 /data/uploads")
    p.add_argument("--mode", choices=("online", "frozen"), default="online",
                   help="online=浅审（无哈希，非一致快照）；frozen=全审"
                        "（逐文件 SHA-256，REPEATABLE READ 一致快照）")
    p.add_argument("--out-dir", required=True,
                   help="输出目录（不存在则创建；目录 0700、文件 0600）")
    p.add_argument("--page-size", type=int, default=500,
                   help="slides 键集分页页大小（默认 500）")
    p.add_argument("--rate-limit", type=float, default=0.0,
                   help="文件扫描限速（文件/秒；0=不限）")
    p.add_argument("--quota-tolerance-bytes", type=int, default=0,
                   help="used_bytes 与实际字节的容差（默认 0，超出即记 "
                        "quota_mismatch）")
    return p.parse_args(argv)


def _run(args):
    started = _now_iso()
    scan_id = uuid.uuid4().hex[:12]

    upload_dir = Path(args.upload_dir
                      or os.environ.get("UPLOAD_DIR") or "/data/uploads")
    if not upload_dir.is_dir():
        raise AuditError("UPLOAD_DIR 不存在或不是目录：%s" % upload_dir)

    out_dir = Path(args.out_dir)
    created = not out_dir.exists()
    try:
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            os.chmod(str(out_dir), 0o700)
    except OSError as e:
        raise AuditError("创建输出目录失败：%s" % e)

    conninfo = resolve_conninfo(args.database_url)
    conninfo_source = ("--database-url" if args.database_url
                       else ("env:DATABASE_URL"
                             if (os.environ.get("DATABASE_URL") or "").strip()
                             else "env:PG*"))
    try:
        conn, cur, pg_snapshot = _connect_readonly(conninfo, args.mode)
    except _PG_ERRORS as e:
        raise AuditError("数据库连接失败：%s" % _pg_err(e))

    audit = _Audit(args)
    try:
        try:
            data = audit.collect_db(cur, pg_snapshot)
        finally:
            try:
                conn.rollback()  # 只读会话收尾（不 commit 任何东西）
            except _PG_ERRORS:
                pass
            conn.close()

        index, scan_errors = _scan_root_entries(upload_dir, audit.pacer)
        for e in scan_errors:
            audit.issues.add("scan_error", "review", "manual_review",
                             "根目录扫描：%s" % e, path=str(upload_dir))
            audit._incomplete("scan_error")
        if data.get("id_dups"):
            audit.issues.add(
                "identity_collision", "blocker", "manual_review",
                "slide_id 或 legacy_filename 存在重复（PK/UNIQUE 之外的异常，"
                "人工核查数据库一致性）")

        with _JsonlWriter(out_dir / "inventory.jsonl") as inv, \
                _JsonlWriter(out_dir / "issues.jsonl") as iss_writer:
            owner_actual = audit.emit_inventory(inv, data, index, upload_dir)
            audit.emit_reference_issues(data, set(index))
            audit.emit_quota_issues(data, owner_actual)
            for rec in audit.issues.items:
                iss_writer.write(rec)

        finished = _now_iso()
        verif = audit.verification(args, started, finished, scan_id,
                                   upload_dir, data, conninfo_source)
        with _open_private(out_dir / "verification.json") as f:
            f.write(json.dumps(verif, ensure_ascii=False, indent=2) + "\n")
        with _open_private(out_dir / "summary.md") as f:
            f.write(audit.summary_md(verif))
    except OSError as e:
        raise AuditError("输出写入失败（%s）：%s" % (out_dir, e))

    sev = verif["issues_by_severity"]
    _info("%s 模式审计完成：slides=%d 孤儿=%d issues=%d（blocker=%d "
          "review=%d info=%d）incomplete=%s → %s"
          % (audit.mode, audit.counters["slides"],
             audit.counters["orphan_files"], verif["issues_total"],
             sev["blocker"], sev["review"], sev["info"],
             verif["incomplete"], out_dir))
    if audit.incomplete_reasons:
        return EXIT_INCOMPLETE
    return EXIT_OK


def main(argv=None):
    args = _parse_args(argv)
    if args.page_size < 1:
        _err("--page-size 必须 ≥ 1")
        return EXIT_TOOL_ERROR
    if args.rate_limit < 0:
        _err("--rate-limit 不能为负")
        return EXIT_TOOL_ERROR
    if args.quota_tolerance_bytes < 0:
        _err("--quota-tolerance-bytes 不能为负")
        return EXIT_TOOL_ERROR
    try:
        return _run(args)
    except AuditError as e:
        _err(str(e))
        return EXIT_TOOL_ERROR
    except _PG_ERRORS as e:  # DB 协议层故障 = 工具错误
        _err("数据库错误：%s" % _pg_err(e))
        return EXIT_TOOL_ERROR


if __name__ == "__main__":
    sys.exit(main())
