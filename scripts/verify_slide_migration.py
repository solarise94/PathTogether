#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_slide_migration —— 迁移后独立核验（slide ID 化重构 P6，合同 §1.3）。

本模块是「代码旁合同」：镜像 docs/slide-id-refactor-p6-contract-20260925.md
§1.3 与 docs/slide-storage-migration-audit-runbook-20260925.md §3（verification
输出契约）/§5（硬门禁口径）。冲突以手册为准。

独立核验纪律（合同 §1.3 逐条）
  - **不读迁移日志的 success 字段**：独立重读实际 DB/磁盘。journal 仅作
    可选的**交叉证据**（bundle manifest 与 journal 记录的 manifest 逐文件
    对照），其 result/success 字段不参与判定。
  - 每 ready 资产：descriptor 可解析可读（resolve_descriptor_path）+
    包完整性（bundle 内 manifest.json 逐文件 sha/大小）+ 代表性试开
    （slide_io.open_slide）。
  - 引用逐条落点：share_slides / slide_view_grants / project_slides / rois /
    comments / change_log / run_grants / ai_session_principals /
    annotation_access_events / demo_catalog 的 slide_id 逐条解析到存活行。
  - 同名 tombstone 不复活：deleted 行保留 legacy_filename；对每个
    「tombstone 名 L + 新资产 original_filename==L」组合，逐个旧分享领取人
    验证对新资产 authorize_read=False（意外新增可见性检查的最尖锐面）。
  - 配额账本对账（R-12 口径）：每 owner used_bytes 与名下资产 accounted
    合计的关系**报告**（含「不等于」的合法原因披露：0013 删除不回退、
    failed 撤回未退款、历史口径差等）——**只对账不改账**。
  - 授权差异（对照计划 authorization_summary）：ID 基字段双向 diff——
    意外增加与**意外缩小**同时报告。
  - 任何扫描错误/权限失败/读取变化 → ``incomplete``，**不得报告全量通过**。

输出（0700 目录 / 0600 文件）：``verification.json`` + 人读 ``summary.md``
（脱敏计数、incomplete 项、go/no-go 建议）。

用法::

    python scripts/verify_slide_migration.py --upload-dir /data/uploads \\
        --out-dir /var/verify/out [--plan migration-plan.jsonl] \\
        [--journal migration-journal.jsonl] [--database-url URL]

退出码：``0``=核验通过；``1``=工具错误；``2``=核验失败（存在违规项）；
``3``=incomplete（扫描错误/无权限/读取变化——不得当全量通过）。

worker 安全：只 import pg_store / slide_store / slide_storage / slide_io
（worker 安全模块）+ 只读查询，不 import app。
"""
from __future__ import annotations

import argparse
import hashlib


def _canon_perms(raw):
    """permissions JSONB 文本 → 规范化排序表（与 audit 侧同口径）。"""
    import json as _json
    try:
        val = _json.loads(raw) if raw else []
        if isinstance(val, list):
            return sorted(str(x) for x in val)
        return [repr(val)]
    except (TypeError, ValueError):
        return [str(raw)]
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg  # noqa: E402

import pg_store  # noqa: E402
import slide_io  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402

TOOL_VERSION = "1.0.0-p6"

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_FAILED = 2
EXIT_INCOMPLETE = 3

_HASH_CHUNK = 1 << 20

#: ID 基引用表清单（slide_id 列名，0067 schema；SQL 拼接仅限此白名单）。
_REFERENCE_TABLES = (
    ("share_slides", "slide_id"),
    ("slide_view_grants", "slide_id"),
    ("project_slides", "slide_id"),
    ("rois", "slide_id"),
    ("comments", "slide_id"),
    ("change_log", "slide_id"),
    ("run_grants", "slide_id"),
    ("ai_session_principals", "slide_id"),
    ("annotation_access_events", "slide_id"),
    ("demo_catalog", "slide_id"),
)

#: 授权差异可比字段（计划 authorization_summary ↔ 现库按 slide_id 重算；
#: 名基字段 shares/share_grants_active/view_grants_by_name 是快照口径，
#: 单列为信息项不参与 diff）。
_AUTH_DIFF_FIELDS = (
    "view_grants_by_id", "project_slides", "rois", "comments", "change_log",
    "run_grants", "ai_session_principals", "demo_catalog",
)

_AUTH_DIFF_SQL = {
    "view_grants_by_id": ("SELECT count(*) AS n FROM slide_view_grants "
                          "WHERE slide_id=%s"),
    "project_slides": ("SELECT count(*) AS n FROM project_slides "
                       "WHERE slide_id=%s"),
    "rois": ("SELECT count(*) AS n FROM rois WHERE slide_id=%s "
             "AND NOT deleted"),
    "comments": ("SELECT count(*) AS n FROM comments WHERE slide_id=%s "
                 "AND NOT deleted"),
    "change_log": ("SELECT count(*) AS n FROM change_log WHERE slide_id=%s"),
    "run_grants": ("SELECT count(*) AS n FROM run_grants WHERE slide_id=%s"),
    "ai_session_principals": (
        "SELECT count(*) AS n FROM ai_session_principals WHERE slide_id=%s"),
    "demo_catalog": ("SELECT count(*) AS n FROM demo_catalog WHERE slide_id=%s"),
}


class VerifyError(Exception):
    """工具自身错误（参数/连接/输出）→ 退出码 1。"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _err(msg):
    sys.stderr.write("verify_slide_migration: %s\n" % msg)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        while True:
            block = f.read(_HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _open_private(path: Path):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(str(path), 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


# --------------------------------------------------------------------------- #
# 主核验器
# --------------------------------------------------------------------------- #
class Verifier:
    def __init__(self, conn, upload_dir: Path, plan_items=None,
                 journal_manifests=None, quota_approvals=None):
        self.conn = conn
        self.upload_dir = upload_dir
        self.plan_items = plan_items or {}        # item_id -> plan item
        self.journal_manifests = journal_manifests or {}  # slide_id -> manifest
        # R6 审查修复（问题 5）：逐 owner 的配额差额核准凭据
        # {user_id: [{"delta": int, "reason": str}, ...]}
        self.quota_approvals = quota_approvals or {}
        self.violations = []      # 硬违规（exit 2）
        self.warnings = []        # 披露项（不阻断）
        self.incomplete_reasons = []
        self.assets = []
        self.counters = {
            "slides": 0, "ready": 0, "ready_id_bundle_ok": 0,
            "ready_id_bundle_bad": 0, "ready_legacy_remaining": 0,
            "non_ready": 0,
        }

    # ---------------- incomplete ---------------- #
    def _incomplete(self, reason):
        if reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)

    def _violation(self, check, detail):
        self.violations.append({"check": check, "detail": detail})

    def _warn(self, check, detail):
        self.warnings.append({"check": check, "detail": detail})

    # ---------------- 资产与包完整性 ---------------- #
    def verify_assets(self):
        descs = slide_store.list_all_descriptors()
        self.counters["slides"] = len(descs)
        for desc in descs:
            if desc.asset_state != slide_store.SlideState.READY:
                self.counters["non_ready"] += 1
                continue
            self.counters["ready"] += 1
            if desc.storage_layout != "id_bundle":
                self.counters["ready_legacy_remaining"] += 1
                self._warn(
                    "ready_asset_not_migrated",
                    {"slide_id": desc.slide_id,
                     "note": "ready+legacy 平铺布局（隔离未决议或迁移未覆盖；"
                             "上线硬门禁 §5.1 要求全量 id_bundle 或明确隔离决议）"})
                continue
            entry = {
                "slide_id": desc.slide_id,
                "storage_relpath": desc.storage_relpath,
                "accounted_bytes": desc.accounted_bytes,
                "bundle_files": None,
                "manifest_entry": None,
                "open_test": None,
                "ok": True,
            }
            # descriptor 路径可解析可读（containment 校验在 resolver 内）
            try:
                entry_abs = slide_storage.resolve_descriptor_path(
                    desc, root=self.upload_dir)
                if not entry_abs.is_file():
                    raise FileNotFoundError(str(entry_abs))
            except (ValueError, OSError) as e:
                entry["ok"] = False
                self.counters["ready_id_bundle_bad"] += 1
                self._violation("descriptor_path_unreadable",
                                {"slide_id": desc.slide_id, "error": str(e)})
                self.assets.append(entry)
                continue
            # 包完整性：bundle 内 manifest.json 逐文件 sha/大小
            bundle = slide_storage.bundle_dir(desc.slide_id,
                                              root=self.upload_dir)
            manifest_path = bundle / "manifest.json"
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8"))
                norm = slide_storage.validate_manifest(manifest)
            except (OSError, ValueError) as e:
                entry["ok"] = False
                self.counters["ready_id_bundle_bad"] += 1
                self._violation("bundle_manifest_unreadable",
                                {"slide_id": desc.slide_id, "error": str(e)})
                self.assets.append(entry)
                continue
            entry["manifest_entry"] = norm["entry"]
            bad_files = []
            for f in norm["files"]:
                p = bundle / f["path"]
                try:
                    if not p.is_file():
                        bad_files.append({"path": f["path"],
                                          "error": "missing"})
                        continue
                    if f.get("size") is not None and \
                            p.stat().st_size != int(f["size"]):
                        bad_files.append({"path": f["path"],
                                          "error": "size_mismatch"})
                        continue
                    if f.get("sha256") and \
                            _sha256_file(p) != str(f["sha256"]).lower():
                        bad_files.append({"path": f["path"],
                                          "error": "sha_mismatch"})
                except OSError as e:
                    bad_files.append({"path": f["path"], "error": str(e)})
                    self._incomplete("scan_error")
            if bad_files:
                entry["ok"] = False
                entry["bundle_files"] = bad_files
                self.counters["ready_id_bundle_bad"] += 1
                self._violation("bundle_integrity",
                                {"slide_id": desc.slide_id,
                                 "bad_files": bad_files})
            else:
                entry["bundle_files"] = len(norm["files"])
            # 入口与 storage_relpath 一致（manifest.entry = relpath basename）
            entry_basename = (desc.storage_relpath or "").split("/")[-1]
            if not desc.storage_relpath or norm["entry"] != entry_basename:
                entry["ok"] = False
                self._violation("entry_relpath_mismatch", {
                    "slide_id": desc.slide_id,
                    "storage_relpath": desc.storage_relpath,
                    "manifest_entry": norm["entry"]})
            # 交叉证据（可选）：bundle manifest 与 journal 记录的 manifest
            if desc.slide_id in self.journal_manifests:
                jm = self.journal_manifests[desc.slide_id]
                jmap = {f["path"]: f.get("sha256") for f in jm.get("files", [])}
                bmap = {f["path"]: f.get("sha256") for f in norm["files"]}
                if jmap != bmap:
                    entry["ok"] = False
                    self._violation("journal_manifest_crosscheck", {
                        "slide_id": desc.slide_id,
                        "note": "bundle manifest 与 journal 记录的逐文件 "
                                "sha 不一致（独立核验发现，不信 success 字段）"})
            # 代表性试开
            try:
                slide_io.open_slide(
                    bundle / norm["entry"],
                    format_hint=(desc.original_filename
                                 or norm["entry"]))
                entry["open_test"] = "ok"
            except slide_io.SlideValidationError as e:
                entry["ok"] = False
                entry["open_test"] = e.code
                self.counters["ready_id_bundle_bad"] += 1
                self._violation("open_test_failed",
                                {"slide_id": desc.slide_id, "code": e.code})
            except Exception as e:  # noqa: BLE001 - 试开的未知故障=不完整
                entry["open_test"] = "error"
                self._incomplete("open_test_error")
                self._warn("open_test_error",
                           {"slide_id": desc.slide_id, "error": str(e)})
            if entry["ok"]:
                self.counters["ready_id_bundle_ok"] += 1
            self.assets.append(entry)

    # ---------------- 引用逐条落点 ---------------- #
    def verify_references(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT slide_id FROM slides")
            live = {r["slide_id"] for r in cur.fetchall()}
        refs = {}
        for table, col in _REFERENCE_TABLES:
            dangling = []
            try:
                with self.conn.cursor() as cur:
                    cur.execute(
                        "SELECT %s AS slide_id FROM %s WHERE %s IS NOT NULL"
                        % (col, table, col))
                    rows = cur.fetchall()
            except psycopg.Error as e:
                self._incomplete("db_query_error")
                self._warn("reference_query_failed",
                           {"table": table, "error": str(e).split("\n")[0]})
                continue
            for r in rows:
                sid = r["slide_id"]
                if sid not in live:
                    dangling.append(sid)
            refs[table] = {"total": len(rows), "dangling": dangling}
            if dangling:
                self._violation("dangling_slide_reference", {
                    "table": table, "slide_ids": sorted(set(dangling))})
        return refs

    # ---------------- tombstone 不复活 ---------------- #
    def verify_tombstones(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT slide_id, legacy_filename, deleted_at FROM slides "
                "WHERE asset_state=%s AND legacy_filename IS NOT NULL",
                (slide_store.SlideState.DELETED,))
            tombstones = cur.fetchall()
        result = {"tombstones": len(tombstones), "alias_unique": True,
                  "crossread_checks": 0, "crossread_violations": []}
        if not tombstones:
            return result
        # 冻结别名不重绑（UNIQUE 兜底；独立重证）
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT legacy_filename, count(*) AS n FROM slides "
                "WHERE legacy_filename IS NOT NULL "
                "GROUP BY legacy_filename HAVING count(*) > 1")
            dup = [r["legacy_filename"] for r in cur.fetchall()]
        if dup:
            result["alias_unique"] = False
            self._violation("legacy_alias_rebound", {"duplicates": dup})
        for tomb in tombstones:
            name = tomb["legacy_filename"]
            with self.conn.cursor() as cur:
                # 同名新资产（original_filename 展示快照相同）。public 新资产
                # 对所有人可读属全局语义，不算旧分享继承——跳过但计数披露。
                cur.execute(
                    "SELECT slide_id, public FROM slides "
                    "WHERE original_filename=%s AND slide_id <> %s "
                    "AND asset_state=%s",
                    (name, tomb["slide_id"], slide_store.SlideState.READY))
                reborn = [(r["slide_id"], bool(r["public"]))
                          for r in cur.fetchall()]
                if not reborn:
                    continue
                # 旧分享的已领取用户（grant 行，token 不入报告）
                cur.execute(
                    "SELECT DISTINCT g.user_id FROM grants g "
                    "JOIN share_slides ss ON ss.token = g.token "
                    "WHERE ss.slide_id=%s AND g.active",
                    (tomb["slide_id"],))
                members = [r["user_id"] for r in cur.fetchall()]
            for new_id, is_public in reborn:
                for user in members:
                    result["crossread_checks"] += 1
                    if is_public:
                        result.setdefault("public_reborn_skipped", []).append(
                            new_id)
                        continue
                    allowed = slide_store.authorize_read(
                        new_id, actor_user_id=user)
                    if allowed:
                        result["crossread_violations"].append(
                            {"tombstone": tomb["slide_id"], "reborn": new_id})
                        self._violation("tombstone_share_crossread", {
                            "tombstone_slide_id": tomb["slide_id"],
                            "reborn_slide_id": new_id,
                            "note": "旧分享领取人可读同展示名新资产——旧授权"
                                    "意外继承新内容（R-04 违规）"})
        return result

    # ---------------- 配额对账（只报告不改账） ---------------- #
    def verify_quotas(self):
        """配额对账（R6 审查修复问题 5：差额未核准 → 阻断）。

        delta==0 通过；delta!=0 必须有逐项人工核准凭据（--quota-approvals
        JSON：[{user_id, delta, reason}]，delta 精确匹配）——未核准差额
        一律 violation（go 变 no-go），reasons 仅作披露不构成核准。"""
        with self.conn.cursor() as cur:
            cur.execute("SELECT user_id, quota_bytes, used_bytes, "
                        "reserved_bytes FROM upload_user_quotas")
            quotas = {r["user_id"]: dict(r) for r in cur.fetchall()}
            cur.execute(
                "SELECT owner_user_id, asset_state, "
                "COALESCE(sum(accounted_bytes),0)::bigint AS bytes, "
                "count(*) AS n FROM slides WHERE owner_user_id IS NOT NULL "
                "AND accounted_bytes IS NOT NULL "
                "GROUP BY owner_user_id, asset_state")
            rows = cur.fetchall()
        per_owner = {}
        for r in rows:
            per_owner.setdefault(r["owner_user_id"], {})[r["asset_state"]] = \
                {"bytes": int(r["bytes"]), "n": int(r["n"])}
        report = {}
        owners = sorted(set(quotas) | set(per_owner))
        for owner in owners:
            q = quotas.get(owner, {"quota_bytes": 0, "used_bytes": 0,
                                   "reserved_bytes": 0})
            by_state = per_owner.get(owner, {})
            # 结算责任口径：ready/deleting 的 accounted 合计（deleting 结算
            # 未完成仍占账；staging 未定值不计；failed/deleted 按 R-12/0013
            # 口径属「合法不等于」来源）
            responsible = sum(v["bytes"] for st, v in by_state.items()
                              if st in ("ready", "deleting"))
            used = int(q["used_bytes"])
            delta = used - responsible
            reasons = []
            if delta == 0:
                reasons.append("一致")
            else:
                # 合法原因披露（runbook §2.3 容量行 / R-12 过渡口径）
                if by_state.get("failed"):
                    reasons.append(
                        "failed 资产 accounted=%d（撤回/验证失败不退款或"
                        "从未入账——历史责任项，须单独核准）"
                        % by_state["failed"]["bytes"])
                if by_state.get("deleted"):
                    reasons.append(
                        "deleted tombstone accounted=%d（0013 口径删除不"
                        "回退 used——合法不等于）"
                        % by_state["deleted"]["bytes"])
                if by_state.get("staging"):
                    reasons.append("staging 在途 %d 项（accounted 未定值）"
                                   % by_state["staging"]["n"])
                if int(q["reserved_bytes"]):
                    reasons.append("reserved=%d（在途预占，与 used 分列）"
                                   % int(q["reserved_bytes"]))
                if delta > 0 and not reasons:
                    reasons.append("used 高于责任合计 %d 字节（历史口径差/"
                                   "0013 删除不回退——R-12 过渡期合法差异，"
                                   "迁移不改账）" % delta)
                if self._owner_has_derivative_items(owner):
                    reasons.append(
                        "迁移 accounted 校准以包内字节为准（.manifest.json/"
                        ".associated 派生物留置原位不计——计划 "
                        "derivatives_in_place 披露）")
                if delta < 0 and not reasons:
                    reasons.append("used 低于责任合计 %d 字节（历史欠账——"
                                   "须单独核准，不静默归零）" % (-delta))
            if delta != 0 and not reasons:
                reasons.append("未解释差 %d 字节——须人工核准（不得静默归零）"
                               % delta)
            approved = None
            if delta != 0:
                # 机判归因：delta 恰等于 failed+deleted 两桶 accounted 合计
                # （撤回不退款/删除不回退——R-12/0013 的状态机可证来源）时
                # 自动接受并披露；其余差额（含一切负值欠账）须逐项核准。
                failed_bytes = int(by_state.get("failed", {}).get("bytes", 0))
                deleted_bytes = int(by_state.get("deleted", {}).get("bytes", 0))
                attributed = delta == failed_bytes + deleted_bytes
                if attributed:
                    reasons.append(
                        "差额 %d 已机判归因（failed=%d + deleted=%d，状态桶"
                        "精确匹配）" % (delta, failed_bytes, deleted_bytes))
                else:
                    for entry in self.quota_approvals.get(owner, []):
                        try:
                            if int(entry.get("delta")) == int(delta):
                                approved = entry
                                break
                        except (TypeError, ValueError):
                            continue
                if approved is None and not attributed:
                    self._violation("quota_delta_unapproved", {
                        "user_id": owner, "delta": delta,
                        "used_bytes": used,
                        "responsible_ready_deleting_bytes": responsible,
                        "reasons": reasons,
                        "note": "配额差额未经逐项核准（--quota-approvals "
                                "须精确匹配 delta 并附理由）——不得静默按"
                                "历史口径差异放行"})
                    reasons.append("**未核准差额 %d 字节（阻断项）**" % delta)
                elif approved is not None:
                    reasons.append(
                        "已核准差额 %d 字节：%s" % (delta, approved.get("reason")))
            report[owner] = {
                "quota_bytes": int(q["quota_bytes"]),
                "used_bytes": used,
                "reserved_bytes": int(q["reserved_bytes"]),
                "accounted_by_state": {st: v["bytes"]
                                       for st, v in sorted(by_state.items())},
                "responsible_ready_deleting_bytes": responsible,
                "delta_used_minus_responsible": delta,
                "approved": bool(approved) if delta != 0 else None,
                "reasons": reasons,
            }
        return report

    def _owner_has_derivative_items(self, owner) -> bool:
        """计划中该 owner 的迁移项是否有留置派生物（校准差披露依据）。"""
        for item in self.plan_items.values():
            if item.get("owner_user_id") != owner:
                continue
            deriv = (item.get("source") or {}).get("derivatives_in_place") or {}
            if deriv.get("manifest_json") or deriv.get("associated_dir"):
                return True
        return False

    # ---------------- 授权差异（对照计划；双向） ---------------- #
    def verify_authorization_diff(self):
        if not self.plan_items:
            return {"note": "未提供 --plan：跳过授权差异对照"}
        diffs = []
        for item_id in sorted(self.plan_items):
            item = self.plan_items[item_id]
            if item.get("action") != "migrate" or not item.get("slide_id"):
                continue
            sid = item["slide_id"]
            # R6 审查修复（问题 2）：集合级比对（owner/public/view 授权主体/
            # 分享成员）——数量相同不构成授权相同；双向差异均违规。
            freeze = item.get("authorization_freeze") or {}
            if freeze:
                try:
                    with self.conn.cursor() as cur:
                        cur.execute(
                            "SELECT owner_user_id, public FROM slides "
                            "WHERE slide_id=%s", (sid,))
                        row = cur.fetchone()
                        cur.execute(
                            "SELECT DISTINCT COALESCE(user_id,'') AS u "
                            "FROM slide_view_grants WHERE slide_id=%s "
                            "OR (slide_id IS NULL AND slide_name=%s)",
                            (sid, item.get("legacy_filename") or ""))
                        grants = sorted(r["u"] for r in cur.fetchall())
                        cur.execute(
                            "SELECT token FROM share_slides WHERE "
                            "slide_id=%s", (sid,))
                        # token 摘要比对（与冻结侧同口径：sha256 前 16 hex）
                        share = sorted(
                            hashlib.sha256(
                                str(r["token"]).encode("utf-8")
                            ).hexdigest()[:16] for r in cur.fetchall())
                except psycopg.Error as e:
                    self._incomplete("db_query_error")
                    self._warn("auth_set_query_failed",
                               {"slide_id": sid,
                                "error": str(e).split("\n")[0]})
                    row, grants, share = None, None, None
                if row is not None:
                    f_owner = freeze.get("owner_user_id") or None
                    a_owner = row["owner_user_id"] or None
                    if f_owner != a_owner:
                        diffs.append({
                            "slide_id": sid, "field": "owner_user_id",
                            "expected": f_owner, "actual": a_owner,
                            "direction": "unexpected_change"})
                        self._violation("authorization_drift", {
                            "slide_id": sid, "field": "owner_user_id",
                            "expected": f_owner, "actual": a_owner,
                            "note": "迁移不得改归属（owner 转移须先有决议）"})
                    if bool(freeze.get("public")) != bool(row["public"]):
                        diffs.append({
                            "slide_id": sid, "field": "public",
                            "expected": bool(freeze.get("public")),
                            "actual": bool(row["public"]),
                            "direction": (
                                "unexpected_increase"
                                if row["public"] else "unexpected_shrink")})
                        self._violation("authorization_drift", {
                            "slide_id": sid, "field": "public",
                            "expected": freeze.get("public"),
                            "actual": row["public"],
                            "note": "迁移不得改变 public 可见性（授权只能"
                                    "来自已审核计划）"})
                if grants is not None:
                    f_g = sorted(freeze.get("view_grant_users") or [])
                    if f_g != grants:
                        diffs.append({
                            "slide_id": sid, "field": "view_grant_users",
                            "expected": f_g, "actual": grants,
                            "direction": (
                                "unexpected_increase"
                                if set(grants) - set(f_g)
                                else "unexpected_shrink")})
                        self._violation("authorization_drift", {
                            "slide_id": sid, "field": "view_grant_users",
                            "expected": f_g, "actual": grants,
                            "note": "view 授权主体集合漂移（双向均违规）"})
                if share is not None:
                    f_s = sorted(freeze.get("share_member_tokens") or [])
                    if f_s != share:
                        diffs.append({
                            "slide_id": sid, "field": "share_member_tokens",
                            "expected": f_s, "actual": share,
                            "direction": (
                                "unexpected_increase"
                                if set(share) - set(f_s)
                                else "unexpected_shrink")})
                        self._violation("authorization_drift", {
                            "slide_id": sid, "field": "share_member_tokens",
                            "expected": f_s, "actual": share,
                            "note": "分享成员关系漂移（token 不新增不丢失；"
                                    "双向均违规）"})
                # R7 复核修复 P1：分享控制状态 + 领取权限比对——token 集合
                # 相同不构成授权相同（撤销/过期时刻/权限/领取主体与 active
                # 才是实际控制访问的字段）。expires_at 比对**冻结存储值**
                # （时间自然推进不改列值，不产生假阳性；改值即漂移）。
                try:
                    with self.conn.cursor() as cur:
                        cur.execute(
                            "SELECT s.token, s.revoked, "
                            "extract(epoch from s.expires_at)::float8 "
                            "AS exp, s.permissions::text AS perms "
                            "FROM shares s JOIN share_slides m "
                            "ON m.token = s.token WHERE m.slide_id=%s "
                            "ORDER BY s.token", (sid,))
                        share_states = [
                            {"token": hashlib.sha256(
                                str(r["token"]).encode("utf-8")
                            ).hexdigest()[:16],
                             "revoked": bool(r["revoked"]),
                             "expires_at": (float(r["exp"])
                                            if r["exp"] is not None
                                            else None),
                             "permissions": _canon_perms(r["perms"])}
                            for r in cur.fetchall()]
                        cur.execute(
                            "SELECT g.token, g.user_id, g.active, "
                            "g.permissions::text AS perms FROM grants g "
                            "JOIN share_slides m ON m.token = g.token "
                            "WHERE m.slide_id=%s "
                            "ORDER BY g.token, g.user_id", (sid,))
                        claim_grants = [
                            {"token": hashlib.sha256(
                                str(r["token"]).encode("utf-8")
                            ).hexdigest()[:16],
                             "user_id": str(r["user_id"] or ""),
                             "active": bool(r["active"]),
                             "permissions": _canon_perms(r["perms"])}
                            for r in cur.fetchall()]
                except psycopg.Error as e:
                    self._incomplete("db_query_error")
                    self._warn("auth_state_query_failed",
                               {"slide_id": sid,
                                "error": str(e).split("\n")[0]})
                    share_states = claim_grants = None
                if share_states is not None:
                    f_ss = sorted(freeze.get("share_states") or [],
                                  key=lambda d: d.get("token"))
                    if f_ss != share_states:
                        diffs.append({
                            "slide_id": sid, "field": "share_states",
                            "expected": f_ss, "actual": share_states,
                            "direction": "unexpected_change"})
                        self._violation("authorization_drift", {
                            "slide_id": sid, "field": "share_states",
                            "expected": f_ss, "actual": share_states,
                            "note": "分享控制状态漂移（revoked/expires_at/"
                                    "permissions——撤权、过期时刻与权限变化"
                                    "均违规）"})
                if claim_grants is not None:
                    f_cg = sorted(freeze.get("claim_grants") or [],
                                  key=lambda d: (d.get("token"),
                                                 d.get("user_id")))
                    if f_cg != claim_grants:
                        diffs.append({
                            "slide_id": sid, "field": "claim_grants",
                            "expected": f_cg, "actual": claim_grants,
                            "direction": "unexpected_change"})
                        self._violation("authorization_drift", {
                            "slide_id": sid, "field": "claim_grants",
                            "expected": f_cg, "actual": claim_grants,
                            "note": "分享领取权限漂移（领取主体/active/"
                                    "permissions——换主体、撤权、改权限均"
                                    "违规）"})
            expected = item.get("authorization_summary") or {}
            actual = {}
            for field, sql in _AUTH_DIFF_SQL.items():
                try:
                    with self.conn.cursor() as cur:
                        cur.execute(sql, (sid,))
                        actual[field] = int(cur.fetchone()["n"])
                except psycopg.Error as e:
                    self._incomplete("db_query_error")
                    self._warn("auth_diff_query_failed",
                               {"slide_id": sid, "field": field,
                                "error": str(e).split("\n")[0]})
                    continue
            for field in _AUTH_DIFF_FIELDS:
                exp = int(expected.get(field, 0) or 0)
                act = actual.get(field)
                if act is None:
                    continue
                if act != exp:
                    diffs.append({
                        "slide_id": sid, "field": field,
                        "expected": exp, "actual": act,
                        "direction": "unexpected_increase"
                                     if act > exp else "unexpected_shrink",
                    })
                    self._violation("authorization_drift", {
                        "slide_id": sid, "field": field,
                        "expected": exp, "actual": act,
                        "direction": "unexpected_increase"
                                     if act > exp else "unexpected_shrink",
                        "note": "迁移不得新增权限；意外缩小同样违规（runbook"
                                " §5.2 授权差异双向报告）"})
        return {"compared_items": sum(
            1 for i in self.plan_items.values()
            if i.get("action") == "migrate"),
            "diffs": diffs}

    # ---------------- 计划 migrate 项落点（有计划时逐项硬检） ---------------- #
    def verify_plan_migration_coverage(self):
        """计划动作=migrate 的项必须全部 ready+id_bundle（有计划时）。

        journal 声称成功不算数——这里独立重读行状态（「篡改 journal 的
        success 仍被独立核验抓出」的判定点之一）。"""
        if not self.plan_items:
            return {"note": "未提供 --plan：跳过计划覆盖检查"}
        not_migrated = []
        checked = 0
        for item_id in sorted(self.plan_items):
            # R6 审查修复（问题 4）终验侧：bundle 内伴侣成员内容与冻结清单
            # 逐文件比对（size+sha256；成员集合增删同样违规）。
            item = self.plan_items[item_id]
            frozen_members = ((item.get("source") or {})
                              .get("companion_members"))
            comp = (item.get("source") or {}).get("companion_dir")
            if (item.get("action") == "migrate" and comp
                    and frozen_members is not None
                    and item.get("slide_id")):
                bundle = self.upload_dir / "objects" / item["slide_id"]
                mpath = bundle / "manifest.json"
                try:
                    with open(mpath, "r", encoding="utf-8") as f:
                        bm = json.load(f)
                    files = {f["path"]: f
                             for f in bm.get("files") or []}
                except (OSError, ValueError, KeyError, TypeError) as e:
                    self._violation("companion_freeze_read_failed", {
                        "slide_id": item["slide_id"],
                        "error": str(e)})
                    bm = None
                if bm is not None:
                    frozen = {"%s/%s" % (comp, m["path"]): m
                              for m in frozen_members}
                    staged = {p: f for p, f in files.items()
                              if p.startswith(comp + "/")}
                    if set(frozen) != set(staged):
                        self._violation("companion_content_drift", {
                            "slide_id": item["slide_id"],
                            "frozen_only": sorted(
                                set(frozen) - set(staged)),
                            "bundle_only": sorted(
                                set(staged) - set(frozen))})
                    else:
                        for rel, fm in sorted(frozen.items()):
                            sf = staged[rel]
                            if int(sf.get("size") or -1) != int(
                                    fm.get("size") or -2) or \
                                    str(sf.get("sha256") or "").lower() != \
                                    str(fm.get("sha256") or "").lower():
                                self._violation(
                                    "companion_content_drift", {
                                        "slide_id": item["slide_id"],
                                        "path": rel,
                                        "frozen": (fm.get("size"),
                                                   fm.get("sha256")),
                                        "bundle": (sf.get("size"),
                                                   sf.get("sha256"))})
                                break
            item = self.plan_items[item_id]
            if item.get("action") != "migrate":
                continue
            checked += 1
            sid = item.get("slide_id")
            desc = slide_store.resolve_slide_id(sid) if sid else None
            if (desc is None
                    or desc.asset_state != slide_store.SlideState.READY
                    or desc.storage_layout != "id_bundle"):
                not_migrated.append({"slide_id": sid, "item": item_id,
                                     "state": desc and desc.asset_state,
                                     "layout": desc and desc.storage_layout})
                self._violation("plan_item_not_migrated", {
                    "slide_id": sid, "item_id": item_id,
                    "note": "计划 migrate 项未到达 ready+id_bundle（独立"
                            "重读；journal 的 success 字段不作数）"})
        return {"checked": checked, "not_migrated": not_migrated}

    # ---------------- 隔离口径（计划 quarantine/retain 不可读） ---------------- #
    def verify_quarantine_discipline(self):
        if not self.plan_items:
            return {"note": "未提供 --plan：跳过隔离口径检查"}
        readable = []
        checked = 0
        for item_id in sorted(self.plan_items):
            item = self.plan_items[item_id]
            if item.get("action") not in ("quarantine", "retain_history"):
                continue
            sid = item.get("slide_id")
            if not sid:
                continue
            checked += 1
            desc = slide_store.resolve_slide_id(sid)
            if desc is None:
                continue
            if desc.storage_layout == "id_bundle":
                # 新资产/已迁行不在 legacy 迁移人群（计划可能把它误记为
                # no_legacy_alias 隔离——下游一致豁免）
                continue
            if desc.asset_state == slide_store.SlideState.READY:
                readable.append({"slide_id": sid, "item": item_id})
                self._violation("isolated_item_readable", {
                    "slide_id": sid, "item_id": item_id,
                    "action": item.get("action"),
                    "note": "计划隔离项仍处 ready（可读可列表）——隔离未收口"})
        return {"checked": checked, "readable": readable}


# --------------------------------------------------------------------------- #
# summary.md（脱敏；runbook §3 人读摘要）
# --------------------------------------------------------------------------- #
def summary_md(verif) -> str:
    c = verif["counts"]
    lines = []
    lines.append("# 切片存储迁移独立核验摘要（P6）")
    lines.append("")
    lines.append("- 工具版本：`%s`" % verif["tool_version"])
    lines.append("- 核验起止：%s → %s" % (verif["started_at"],
                                         verif["finished_at"]))
    lines.append("- incomplete：**%s**%s"
                 % ("是" if verif["incomplete"] else "否",
                    ("（原因：%s）" % ", ".join(verif["incomplete_reasons"]))
                    if verif["incomplete_reasons"] else ""))
    lines.append("- 违规项：%d；披露项：%d"
                 % (len(verif["violations"]), len(verif["warnings"])))
    lines.append("")
    lines.append("## 资产计数（脱敏，仅计数）")
    lines.append("")
    lines.append("| 项 | 数量 |")
    lines.append("|---|---|")
    lines.append("| slides 行 | %d |" % c["slides"])
    lines.append("| ready 资产 | %d |" % c["ready"])
    lines.append("| ready + id_bundle 核验通过 | %d |"
                 % c["ready_id_bundle_ok"])
    lines.append("| ready + id_bundle 核验失败 | %d |"
                 % c["ready_id_bundle_bad"])
    lines.append("| ready + legacy 未迁移（披露） | %d |"
                 % c["ready_legacy_remaining"])
    lines.append("| 非 ready（隔离/tombstone/在途） | %d |" % c["non_ready"])
    lines.append("")
    lines.append("## 引用落点")
    lines.append("")
    lines.append("| 表 | 带 slide_id 行 | 悬空 |")
    lines.append("|---|---|---|")
    for table, info in sorted(verif["references"].items()):
        lines.append("| %s | %d | %d |"
                     % (table, info["total"], len(info["dangling"])))
    lines.append("")
    lines.append("## tombstone 不复活")
    lines.append("")
    t = verif["tombstones"]
    lines.append("- tombstone（deleted 且保留冻结别名）：%d 个；别名唯一：**%s**"
                 % (t["tombstones"], "是" if t["alias_unique"] else "否"))
    lines.append("- 同名重生×旧分享领取人交叉验证：%d 次；违规 %d 次"
                 % (t["crossread_checks"], len(t["crossread_violations"])))
    lines.append("")
    lines.append("## 配额对账（只报告不改账；「不等于」须有合法原因）")
    lines.append("")
    lines.append("| owner | used | ready+deleting 合计 | 差值 | 原因 |")
    lines.append("|---|---|---|---|---|")
    for owner, q in sorted(verif["quota"].items()):
        lines.append("| %s | %d | %d | %+d | %s |"
                     % (owner, q["used_bytes"],
                        q["responsible_ready_deleting_bytes"],
                        q["delta_used_minus_responsible"],
                        "；".join(q["reasons"])))
    lines.append("")
    auth = verif["authorization_diff"]
    if "diffs" in auth:
        lines.append("## 授权差异（对照计划；双向）")
        lines.append("")
        lines.append("- 对照迁移项 %d 个；差异 %d 处（意外增加与意外缩小均计）"
                     % (auth["compared_items"], len(auth["diffs"])))
        lines.append("")
    iso = verif["quarantine_discipline"]
    if "checked" in iso:
        lines.append("## 隔离口径")
        lines.append("")
        lines.append("- 计划隔离项检查 %d 个；仍可读 %d 个"
                     % (iso["checked"], len(iso["readable"])))
        lines.append("")
    lines.append("## go / no-go 建议")
    lines.append("")
    if verif["incomplete"]:
        lines.append("- **no-go（本轮无效）**：核验 incomplete，不得当作全量"
                     "通过，须修复后重验。")
    elif verif["violations"]:
        lines.append("- **no-go**：%d 项违规须清零（包完整性/引用落点/授权"
                     "差异/tombstone 复活/隔离可读）。" % len(verif["violations"]))
    elif c["ready_legacy_remaining"]:
        lines.append("- **暂缓**：仍有 %d 个 ready+legacy 资产未迁移（隔离"
                     "决议或补迁移后重验）。" % c["ready_legacy_remaining"])
    else:
        lines.append("- **go（核验通过）**：独立重读 DB/磁盘全项通过。")
    lines.append("")
    lines.append("> 本摘要为脱敏计数；逐项证据见同目录 verification.json（0600）。")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def _load_plan_items(plan_path):
    items = {}
    for line in Path(plan_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("record_type") == "plan_item":
            items[rec["item_id"]] = rec
    return items


def _load_journal_manifests(journal_path):
    """journal → {slide_id: manifest}（**仅**取 copied 事件的 manifest 载荷
    作交叉证据；result/success 字段不参与任何判定）。"""
    out = {}
    for line in Path(journal_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 崩溃截断尾行：交叉证据按缺失处理
        if rec.get("phase") != "copied":
            continue
        manifest = (rec.get("detail") or {}).get("manifest") or {}
        slide_id = manifest.get("slide_id")
        if not slide_id:
            # copied 事件以 item_id（=slide_id）为键
            slide_id = rec.get("item_id")
            if not str(slide_id or "").startswith("sld_"):
                continue
        if manifest.get("files"):
            out[str(slide_id)] = manifest
    return out


def _load_quota_approvals(path):
    """--quota-approvals JSON → {user_id: [{delta, reason}]}（缺文件报错，
    不静默当作无核准）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except OSError as e:
        raise VerifyError("配额核准文件读取失败：%s" % e) from e
    except json.JSONDecodeError as e:
        raise VerifyError("配额核准文件不是合法 JSON：%s" % e) from e
    if not isinstance(entries, list):
        raise VerifyError("配额核准文件必须是条目数组")
    out = {}
    for ent in entries:
        if not isinstance(ent, dict) or "user_id" not in ent \
                or "delta" not in ent:
            raise VerifyError(
                "核准条目缺少 user_id/delta：%r" % (ent,))
        out.setdefault(str(ent["user_id"]), []).append(ent)
    return out


def run_verify(*, upload_dir, out_dir, plan_path=None, journal_path=None,
               database_url=None, quota_approvals_path=None) -> dict:
    started = _now_iso()
    upload_root = Path(upload_dir or os.environ.get("UPLOAD_DIR")
                       or "/data/uploads")
    if not upload_root.is_dir():
        raise VerifyError("UPLOAD_DIR 不存在或不是目录：%s" % upload_root)
    plan_items = _load_plan_items(plan_path) if plan_path else {}
    journal_manifests = (_load_journal_manifests(journal_path)
                         if journal_path else {})

    if database_url:
        os.environ["DATABASE_URL"] = database_url
    try:
        conn = pg_store.connect()
    except Exception as exc:  # noqa: BLE001
        raise VerifyError("数据库连接失败：%s" % exc) from exc
    conn.row_factory = psycopg.rows.dict_row

    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(out), 0o700)
    except OSError as e:
        raise VerifyError("创建输出目录失败：%s" % e) from e

    quota_approvals = (_load_quota_approvals(quota_approvals_path)
                       if quota_approvals_path else {})
    verifier = Verifier(conn, upload_root, plan_items, journal_manifests,
                        quota_approvals=quota_approvals)
    try:
        verifier.verify_assets()
        references = verifier.verify_references()
        tombstones = verifier.verify_tombstones()
        quota = verifier.verify_quotas()
        auth_diff = verifier.verify_authorization_diff()
        coverage = verifier.verify_plan_migration_coverage()
        isolation = verifier.verify_quarantine_discipline()
    except psycopg.Error as e:
        verifier._incomplete("db_query_error")
        references, tombstones, quota, auth_diff = {}, {}, {}, {}
        coverage = isolation = {}
        verifier._warn("db_query_failed", {"error": str(e).split("\n")[0]})
    finally:
        conn.close()

    finished = _now_iso()
    verif = {
        "tool_version": TOOL_VERSION,
        "mode": "post-migration",
        "started_at": started,
        "finished_at": finished,
        "upload_dir": str(upload_root),
        "plan": str(plan_path) if plan_path else None,
        "journal_crosscheck": str(journal_path) if journal_path else None,
        "incomplete": bool(verifier.incomplete_reasons),
        "incomplete_reasons": list(verifier.incomplete_reasons),
        "counts": verifier.counters,
        "violations": verifier.violations,
        "warnings": verifier.warnings,
        "references": references,
        "tombstones": tombstones,
        "quota": quota,
        "authorization_diff": auth_diff,
        "plan_migration_coverage": coverage,
        "quarantine_discipline": isolation,
    }
    go = ("no-go(incomplete)" if verif["incomplete"]
          else ("no-go" if verifier.violations
                or verifier.counters["ready_id_bundle_bad"]
                else ("hold" if verifier.counters["ready_legacy_remaining"]
                      else "go")))
    verif["go_no_go"] = go
    try:
        with _open_private(out / "verification.json") as f:
            f.write(json.dumps(verif, ensure_ascii=False, indent=2) + "\n")
        with _open_private(out / "summary.md") as f:
            f.write(summary_md(verif))
    except OSError as e:
        raise VerifyError("输出写入失败（%s）：%s" % (out, e)) from e
    return verif


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="迁移后独立核验（不信迁移日志 success；失败非零）")
    p.add_argument("--upload-dir", default=None, help="UPLOAD_DIR 根")
    p.add_argument("--out-dir", required=True,
                   help="输出目录（0700；verification.json + summary.md）")
    p.add_argument("--plan", default=None,
                   help="可选：migration-plan.jsonl（授权差异对照/隔离口径）")
    p.add_argument("--journal", default=None,
                   help="可选：migration-journal.jsonl（仅作 manifest 交叉证据）")
    p.add_argument("--database-url", default=None, help="PG 连接串")
    p.add_argument(
        "--quota-approvals", default=None,
        help="配额差额核准凭据 JSON（[{user_id, delta, reason}]；R6 审查"
             "修复：未核准差额一律阻断 go）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        verif = run_verify(upload_dir=args.upload_dir, out_dir=args.out_dir,
                           plan_path=args.plan, journal_path=args.journal,
                           database_url=args.database_url,
                           quota_approvals_path=args.quota_approvals)
    except VerifyError as e:
        _err(str(e))
        return EXIT_TOOL_ERROR
    sys.stdout.write(
        "verify_slide_migration: %s —— ready=%d（id_bundle ok=%d bad=%d "
        "legacy 剩余=%d）violations=%d warnings=%d incomplete=%s → %s\n"
        % (verif["go_no_go"], verif["counts"]["ready"],
           verif["counts"]["ready_id_bundle_ok"],
           verif["counts"]["ready_id_bundle_bad"],
           verif["counts"]["ready_legacy_remaining"],
           len(verif["violations"]), len(verif["warnings"]),
           verif["incomplete"], args.out_dir))
    if verif["incomplete"]:
        return EXIT_INCOMPLETE
    if verif["violations"] or verif["counts"]["ready_id_bundle_bad"]:
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
