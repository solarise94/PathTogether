#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plan_slide_migration —— 冻结审计产物 → 确定性迁移计划（slide ID 化重构 P6）。

本模块是「代码旁合同」：镜像
docs/slide-id-refactor-p6-contract-20260925.md §1.1 与
docs/slide-storage-migration-audit-runbook-20260925.md §3（逐项计划字段）、
§7.1（迁移分类）。冲突以手册为准。

合同要点（§1.1 逐条）
  - **纯计划，零副作用**：只读输入文件（P0 审计产物 inventory.jsonl /
    issues.jsonl 的冻结副本），不连 DB、不碰 UPLOAD_DIR、不写除 --out 外的
    任何文件。
  - 输出 ``migration-plan.jsonl``：首行计划头（version/env/输入摘要 sha256/
    输入记录数/issues 处置计数），其后逐资产一项，按 item_id 排序——
    **同输入同输出**（无随机、无时间戳、无环境噪声参与内容）。
  - **绝不重新分配已有 slide_id**（冻结裁决）：本工具只读取审计产物中已有
    的 slide_id，不生成、不改写、不复用别名。
  - 动作口径（手册 §7.1）：
      * 文件+元数据一致且 owner 明确 → ``migrate``；
      * 文件缺失 → ``retain_history``（failed+reason，不迁授权）；
      * 文件在元数据不在（孤儿）/ owner 矛盾或空 / 物理形态不可信
        （symlink/特殊文件/读取期变化/扫描错误/活跃任务在途/后缀不在
        format 白名单）→ ``quarantine``（隔离报告，不猜不绑）。
    阻断证据来源是 issues.jsonl 按 slide_id 关联的 blocker/review issue；
    孤儿文件/无主伴侣目录是 inventory 的 orphan_file / companion_dir
    （slide_id=NULL）记录。
  - 逐项字段（runbook §3「逐项计划至少记录」）：保留的 slide_id、冻结旧
    alias、owner 及证据来源、源相对路径/大小/sha256（frozen 审计才有 sha；
    伴侣目录为字节/文件数汇总——P0 已知限制）、目标 ``objects/<slide_id>/``
    布局与 entry、格式、动作、授权映射摘要（share/grant/项目等**计数**，
    不含秘密/token）、回滚定位（源路径保留位=原平铺位置，迁移不删源）。
  - 目标 entry 派生（确定性、服务端侧）：
      * 单文件包 → ``objects/<sid>/data.<format_ext>``（与新资产
        slide_storage.entry_relpath 同构）；
      * MRXS 多文件包 → ``objects/<sid>/<legacy_basename>`` + 同 stem 伴侣
        目录（.mrxs 头按 stem 耦合同名伴侣目录，保名迁移；storage_relpath
        仍位于 objects/<sid>/ 之内，满足 R-20 containment）。
  - 转换派生物（``<name>.manifest.json`` / ``<name>.associated/``，P0 白名单
    口径=可重建派生数据）**不迁移**：保留原位（只读备份语义），计划项以
    ``derivatives_in_place`` 披露；accounted_bytes 校准以迁移包（入口+伴侣）
    为准，派生物留在旧根由上线计划的清理 manifest 处置。

用法::

    python scripts/plan_slide_migration.py \\
        --inventory /var/audit/out/inventory.jsonl \\
        --issues /var/audit/out/issues.jsonl \\
        --env drill-local --out /var/plan/migration-plan.jsonl

退出码：``0``=完成（允许存在 quarantine/retain_history 项——它们是计划的
合法组成）；``1``=工具错误（输入缺失/损坏/输出写失败）。

worker 安全：不 import app；不 import 任何业务模块（纯文件变换，与 P0 审计
工具同款纪律）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

TOOL_VERSION = "1.0.0-p6"

EXIT_OK = 0
EXIT_TOOL_ERROR = 1

#: MRXS 伴侣目录口径（audit_slide_identity.MRXS_EXT / backfill 同源）。
MRXS_SUFFIX = ".mrxs"

#: format_ext 白名单（slide_store.normalize_format_ext 同口径；本工具不
#: import 业务模块，自带同一正则——DB 侧兜底在 slide_store）。
_FORMAT_EXT_RE_SRC = r"^[a-z0-9]{1,16}$"

#: quarantine 阻断证据：issues.jsonl 中按 slide_id 关联的 issue 类型 →
#: 计划 reason。优先级=清单序（先物理形态后归属：同命多证取首证）。
_QUARANTINE_ISSUE_REASONS = (
    ("no_legacy_filename", "no_legacy_filename"),
    ("symlink", "symlink_entry"),
    ("special_file", "special_entry"),
    ("owner_ambiguous", "owner_ambiguous"),
    ("owner_missing", "owner_unresolvable"),
    ("changed_during_read", "source_unstable"),
    ("scan_error", "scan_error"),
    ("active_task", "active_task_in_flight"),
    ("identity_collision", "identity_collision"),
)

#: 授权映射摘要的键（inventory.references 的子集——只保留授权/关系类计数；
#: 全部是**计数**，不含 token/用户明文以外的秘密……计数本身不含任何秘密）。
_AUTH_SUMMARY_KEYS = (
    "shares", "share_grants_active", "view_grants_by_name", "view_grants_by_id",
    "project_slides", "rois", "comments", "change_log",
    "annotation_access_events", "run_grants", "ai_session_principals",
    "demo_catalog",
)


class PlanError(Exception):
    """工具自身错误（输入/输出/参数）→ 退出码 1。"""


def _err(msg):
    sys.stderr.write("plan_slide_migration: %s\n" % msg)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_jsonl(path: Path) -> list:
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise PlanError(
                        "%s 第 %d 行不是合法 JSON：%s" % (path, lineno, e)) from e
    except OSError as e:
        raise PlanError("读取输入失败（%s）：%s" % (path, e)) from e
    return records


def _format_ext_of(legacy_filename):
    """legacy_filename 后缀 → 白名单 format_ext；不在白名单返回 None。

    与 slide_store.normalize_format_ext 同口径（去点、小写、
    ``^[a-z0-9]{1,16}$``）。多段后缀（.ome.tiff）按最长逻辑后缀处理——
    与 slide_io.LOGICAL_EXTS 的最长匹配语义一致（仅取白名单判定）。
    """
    if not isinstance(legacy_filename, str) or not legacy_filename:
        return None
    lower = legacy_filename.lower()
    for logical in (".ome.tiff", ".ome.tif"):
        if lower.endswith(logical):
            return logical[1:]
    dot = legacy_filename.rfind(".")
    if dot <= 0:
        return None
    ext = legacy_filename[dot + 1:].strip().lower()
    if not re.match(_FORMAT_EXT_RE_SRC, ext):
        return None
    return ext


def _is_mrxs(legacy_filename) -> bool:
    return isinstance(legacy_filename, str) and \
        legacy_filename.lower().endswith(MRXS_SUFFIX)


def _quarantine_reason_for(slide_id, issues_by_slide):
    """issues.jsonl 证据 → quarantine reason；无阻断证据返回 None。"""
    for issue in issues_by_slide.get(slide_id, ()):
        for type_, reason in _QUARANTINE_ISSUE_REASONS:
            if issue.get("type") == type_:
                return reason
    return None


def _target_for(slide_id, legacy_filename, format_ext):
    """目标布局派生（确定性；服务端侧，无用户可控片段）。

    单文件包 → data.<ext>（与新资产 entry_relpath 同构）；MRXS → 保留
    legacy basename（伴侣目录 stem 耦合）。返回 (entry_basename, relpath)。
    """
    if _is_mrxs(legacy_filename):
        entry = legacy_filename  # 单段名（审计已保证是根下平铺名）
        relpath = "objects/%s/%s" % (slide_id, entry)
    else:
        entry = "data.%s" % format_ext
        relpath = "objects/%s/%s" % (slide_id, entry)
    return entry, relpath


def build_plan(inventory_records, issues_records, env,
               input_digests=None):
    """冻结审计产物 → 计划记录列表（纯函数；确定性输出的核心）。

    input_digests 是 {inventory, issues} 的文件级 sha256（run_plan 计算；
    直接调用 build_plan 的测试可省略——头部以 null 占位）。返回
    [header, item, item, ...]；item 按 item_id 排序。任何输入形态异常抛
    PlanError（不静默丢弃——丢弃会破坏「计划=冻结输入的完整投影」）。
    """
    issues_by_slide = {}
    issues_by_name = {}
    issue_dispositions = {}
    for issue in issues_records:
        issue_dispositions[issue.get("disposition") or "unknown"] = \
            issue_dispositions.get(issue.get("disposition") or "unknown", 0) + 1
        sid = issue.get("slide_id")
        if sid:
            issues_by_slide.setdefault(sid, []).append(issue)
        name = issue.get("legacy_filename")
        if name:
            issues_by_name.setdefault(name, []).append(issue)

    slides = {}
    orphans = []
    companions = {}   # companion_of（mrxs 名）→ record
    for rec in inventory_records:
        rtype = rec.get("record_type")
        if rtype == "slide":
            sid = rec.get("slide_id")
            if not sid:
                raise PlanError("inventory slide 记录缺 slide_id：%r" % rec)
            if sid in slides:
                raise PlanError("inventory 中 slide_id 重复：%s" % sid)
            slides[sid] = rec
        elif rtype == "orphan_file":
            orphans.append(rec)
        elif rtype == "companion_dir":
            via = rec.get("companion_of")
            if via:
                if via in companions:
                    raise PlanError("companion_dir 重复指向 %s" % via)
                companions[via] = rec

    items = []
    no_alias_rows = 0
    for sid in sorted(slides):
        rec = slides[sid]
        name = rec.get("legacy_filename")
        owner = rec.get("owner_user_id")
        if not name:
            # legacy_filename NULL = 新 ID 资产（P1 合同：新资产恒 NULL）或
            # 脏数据（审计已另发 no_legacy_filename issue 人工核对）。两者都
            # 没有 legacy 物理存在，不属于本迁移人群——header 计数披露，
            # 不进逐项计划（三动作口径只覆盖有 legacy 物理存在的行）。
            no_alias_rows += 1
            continue
        item = {
            "record_type": "plan_item",
            "item_id": sid,
            "kind": "slide",
            "slide_id": sid,
            "legacy_filename": name,
            "owner_user_id": owner,
            "owner_evidence": (
                "slides.owner_user_id（P0 冻结审计快照）" if owner else None),
            "source": {
                "entry_relpath": name,
                "companion_dir": (name[:-len(MRXS_SUFFIX)]
                                  if _is_mrxs(name) else None),
                "entry_size": rec.get("file_size"),
                "entry_sha256": rec.get("sha256"),
                "companion_bytes": None,
                "companion_file_count": None,
                "package_bytes": None,
                "derivatives_in_place": {
                    "manifest_json": bool(rec.get("has_manifest")),
                    "associated_dir": bool(rec.get("has_associated_dir")),
                },
            },
            "format_ext": None,
            "action": None,
            "reason": None,
            "authorization_summary": {
                k: int((rec.get("references") or {}).get(k, 0) or 0)
                for k in _AUTH_SUMMARY_KEYS
            },
            "rollback": {
                "source_retained": True,
                "source_relpath": name,
                "note": "迁移不删源；旧平铺文件保留原位（只读备份语义），"
                        "清理属上线计划的独立可审查 manifest（runbook §4）",
            },
        }

        # 动作裁决（合同 §1.1 口径；判定序=证据优先级）
        blocker = _quarantine_reason_for(sid, issues_by_slide)
        if blocker is None and name:
            # 名字级证据兜底（issue 可能只挂了 legacy_filename）
            for issue in issues_by_name.get(name, ()):
                if issue.get("slide_id") is None:
                    for type_, reason in _QUARANTINE_ISSUE_REASONS:
                        if issue.get("type") == type_:
                            blocker = reason
                            break
                if blocker:
                    break
        if rec.get("file_exists") is False:
            item["action"] = "retain_history"
            item["reason"] = "missing_file"
        elif blocker is not None:
            item["action"] = "quarantine"
            item["reason"] = blocker
        elif not owner:
            item["action"] = "quarantine"
            item["reason"] = "owner_unresolvable"
        else:
            ext = _format_ext_of(name)
            if ext is None:
                item["action"] = "quarantine"
                item["reason"] = "bad_format_ext"
            else:
                item["format_ext"] = ext
                item["action"] = "migrate"
                item["reason"] = None

        # 伴侣目录事实并入源包描述
        if name and name in companions:
            comp = companions[name]
            item["source"]["companion_bytes"] = comp.get("file_size")
            item["source"]["companion_file_count"] = \
                comp.get("companion_file_count")
        entry_size = item["source"]["entry_size"]
        if entry_size is not None and item["action"] == "migrate":
            item["source"]["package_bytes"] = int(entry_size) + int(
                item["source"]["companion_bytes"] or 0)
            entry, relpath = _target_for(sid, name, item["format_ext"])
            item["target"] = {
                "layout": "id_bundle",
                "bundle_dir": "objects/%s/" % sid,
                "entry": entry,
                "storage_relpath": relpath,
                "bundle_bytes": item["source"]["package_bytes"],
            }
        else:
            item["target"] = None
        items.append(item)

    # 孤儿文件 / 无主伴侣目录 → quarantine（文件在、元数据不在；不猜 owner）
    for rec in sorted(orphans, key=lambda r: r.get("legacy_filename") or ""):
        name = rec.get("legacy_filename")
        items.append({
            "record_type": "plan_item",
            "item_id": "orphan:%s" % name,
            "kind": "orphan_file",
            "slide_id": None,
            "legacy_filename": name,
            "owner_user_id": None,
            "owner_evidence": None,
            "source": {
                "entry_relpath": name,
                "companion_dir": None,
                "entry_size": rec.get("file_size"),
                "entry_sha256": rec.get("sha256"),
                "companion_bytes": None,
                "companion_file_count": None,
                "package_bytes": None,
                "derivatives_in_place": {
                    "manifest_json": bool(rec.get("has_manifest")),
                    "associated_dir": bool(rec.get("has_associated_dir")),
                },
            },
            "format_ext": None,
            "action": "quarantine",
            "reason": "orphan_no_metadata",
            "authorization_summary": {},
            "target": None,
            "rollback": {
                "source_retained": True,
                "source_relpath": name,
                "note": "隔离报告，不猜 owner，不自动进入用户列表（§7.1）",
            },
        })
    for via, comp in sorted(companions.items()):
        if comp.get("slide_id") is not None:
            continue  # 已并入对应资产的源包
        items.append({
            "record_type": "plan_item",
            "item_id": "companion:%s" % comp.get("legacy_filename"),
            "kind": "unbound_companion_dir",
            "slide_id": None,
            "legacy_filename": comp.get("legacy_filename"),
            "owner_user_id": None,
            "owner_evidence": None,
            "source": {
                "entry_relpath": comp.get("legacy_filename"),
                "companion_dir": comp.get("legacy_filename"),
                "entry_size": None,
                "entry_sha256": None,
                "companion_bytes": comp.get("file_size"),
                "companion_file_count": comp.get("companion_file_count"),
                "package_bytes": None,
                "derivatives_in_place": {"manifest_json": False,
                                         "associated_dir": False},
            },
            "format_ext": None,
            "action": "quarantine",
            "reason": "unbound_companion_dir",
            "authorization_summary": {},
            "target": None,
            "rollback": {
                "source_retained": True,
                "source_relpath": comp.get("legacy_filename"),
                "note": "伴侣目录无对应资产行——人工分类（§2.3 派生数据）",
            },
        })

    items.sort(key=lambda it: it["item_id"])
    counts = _counts_of(items)
    counts["no_alias_rows_out_of_scope"] = no_alias_rows
    header = {
        "record_type": "plan_header",
        "plan_version": 1,
        "tool_version": TOOL_VERSION,
        "env": env,
        # 确定性合同：计划头**不含时间戳/随机值**；输入摘要使「计划↔冻结输入」
        # 可追溯，migrate --apply 用整个计划文件的 sha256 做执行摘要。
        "inputs": {
            "inventory": {
                "sha256": (input_digests or {}).get("inventory"),
                "records": len(inventory_records)},
            "issues": {
                "sha256": (input_digests or {}).get("issues"),
                "records": len(issues_records),
                "by_disposition": dict(sorted(
                    issue_dispositions.items()))},
        },
        "counts": counts,
    }
    return [header] + items


def _counts_of(items):
    counts = {"items": len(items), "migrate": 0, "quarantine": 0,
              "retain_history": 0}
    for it in items:
        counts[it["action"]] = counts.get(it["action"], 0) + 1
    return counts


def _open_private(path: Path):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(str(path), 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def run_plan(*, inventory_path, issues_path, env, out_path) -> dict:
    """生成计划并落盘；返回摘要 dict（含计划文件 sha256）。"""
    if not env or not env.strip():
        raise PlanError("--env 不能为空（目标环境标识，防把测试计划打到生产）")
    inv_path, iss_path = Path(inventory_path), Path(issues_path)
    inventory = _read_jsonl(inv_path)
    issues = _read_jsonl(iss_path)
    records = build_plan(
        inventory, issues, env.strip(),
        input_digests={"inventory": _sha256_file(inv_path),
                       "issues": _sha256_file(iss_path)})

    out = Path(out_path)
    if out.parent and not out.parent.exists():
        try:
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as e:
            raise PlanError("创建输出目录失败：%s" % e) from e
    digest = None
    try:
        with _open_private(out) as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True)
                        + "\n")
        digest = _sha256_file(out)
    except OSError as e:
        raise PlanError("计划写入失败（%s）：%s" % (out, e)) from e
    return {"out": str(out), "plan_sha256": digest,
            "counts": records[0]["counts"], "env": env.strip()}


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="冻结审计产物 → 确定性迁移计划（纯计划，零副作用）")
    p.add_argument("--inventory", required=True,
                   help="P0 审计产物 inventory.jsonl（冻结副本）")
    p.add_argument("--issues", required=True,
                   help="P0 审计产物 issues.jsonl（冻结副本）")
    p.add_argument("--env", required=True,
                   help="目标环境标识（写入计划头；migrate --apply 必须同值）")
    p.add_argument("--out", required=True,
                   help="输出 migration-plan.jsonl（0600）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_plan(inventory_path=args.inventory,
                           issues_path=args.issues, env=args.env,
                           out_path=args.out)
    except PlanError as e:
        _err(str(e))
        return EXIT_TOOL_ERROR
    sys.stdout.write(
        "plan_slide_migration: env=%s 计划=%s sha256=%s 项=%d "
        "(migrate=%d quarantine=%d retain_history=%d)\n"
        % (summary["env"], summary["out"], summary["plan_sha256"],
           summary["counts"]["items"], summary["counts"]["migrate"],
           summary["counts"]["quarantine"], summary["counts"]["retain_history"]))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
