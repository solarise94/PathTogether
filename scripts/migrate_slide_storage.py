#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_slide_storage —— legacy 平铺布局 → objects/<slide_id>/ 物理迁移执行器
（slide ID 化重构 P6，合同 §1.2；算法与崩溃恢复五态见 runbook §4）。

本模块是「代码旁合同」：镜像 docs/slide-id-refactor-p6-contract-20260925.md
§1.2 与 docs/slide-storage-migration-audit-runbook-20260925.md §4。冲突以
手册为准。

合同要点（§1.2 逐条）
  - **默认 dry-run**：只读计划+只读 DB 展示将做什么；不写 journal、不复制。
  - ``--apply`` **三件套缺一不可**：``--plan`` + ``--plan-digest``（与计划文件
    sha256 一致才执行）+ ``--env``（与计划头一致）+ ``--quiesce-proof``
    （停写证据字符串，非空，记录进 journal——演练环境同样强制，防把测试
    计划打到生产）。
  - 逐项五态状态机 ``planned → copied → verified → bound → postverified``，
    持久 journal（migration-journal.jsonl，逐事件 append+fsync）；崩溃后
    重跑从 **journal+manifest 共同续判**，不以单文件存在为成功依据。
  - copied：目标卷私有 staging（``UPLOAD_DIR/.staging/migrate-<plan>/<sid>/``）
    复制全包（入口 + MRXS 伴侣目录）；**复制不硬链接**（新 inode 隔离，
    shutil.copyfile/copytree 语义）；空间不足阻塞（--free-margin-bytes），
    不降级删源。
  - verified：逐文件 sha256+大小全对 + ``slide_io.open_slide`` 代表性试开
    （格式真实可读）+ fsync（slide_storage._fsync_tree 同源实现）。
  - bound：``slide_storage.publish_bundle_no_clobber`` 入 objects/<sid>/ +
    短事务 CAS（advisory 锁 ``slide:<sid>`` 第一把 → slides 行；经
    ``slide_store.bind_id_bundle_layout`` 原语翻转 storage_layout→id_bundle、
    storage_relpath→新位、accounted_bytes 校准）。**不重复计上传配额**
    （used_bytes 不动，R-12 过渡口径）；**授权映射零变更**。
  - postverified：独立重读 DB+磁盘确认绑定与引用。
  - **不删源**：旧平铺文件保留原位（只读备份语义；清理属上线计划的独立
    可审查 manifest）。
  - 已存在目标：仅当 journal+manifest 证明同一迁移项才幂等复用；否则报
    冲突中止（不依内容相同认领）。
  - 计划项动作口径：migrate 走五态；retain_history（缺文件）→ 行翻 failed
    （mark_failed expected=legacy，与 P1-B1 幂等同参；ready 态走 force_fail）
    ；quarantine（隔离）→ **legacy 态行不动**（保持 manual_review 可经
    backfill 重扫的人工决议通道），ready 态行（物理形态不可信却被回填成
    ready 的存量）force_fail 收口不可读；孤儿/无主伴侣目录无行可动，披露。

worker 安全：只 import pg_store / slide_store / slide_storage / slide_io
（worker 安全模块），不 import app。

用法::

    python scripts/migrate_slide_storage.py --plan migration-plan.jsonl \\
        [--apply --plan-digest <sha256> --env drill-local \\
         --quiesce-proof "<停写证据>"] \\
        [--upload-dir DIR] [--journal PATH] [--database-url URL] \\
        [--free-margin-bytes N]

退出码：``0``=全部迁移项到达终态（postverified / 显式 skip，无失败）；
``1``=工具错误（参数/摘要不符/连接失败）；``2``=存在迁移项失败/冲突
（可修复后重跑——状态机幂等）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat as stat_mod
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
EXIT_ITEM_FAILURE = 2

DEFAULT_FREE_MARGIN_BYTES = 64 * 1024 * 1024  # 64MiB 安全余量

#: 五态阶段序（runbook §4 状态机；数值用于 journal 续判比较）。
_PHASES = ("planned", "copied", "verified", "bound", "postverified")
_PHASE_IDX = {name: i for i, name in enumerate(_PHASES, start=1)}

#: 演练/测试用的崩溃注入点（正常 CLI 不暴露；run_migrate 的 crash_after）。
_CRASH_POINTS = frozenset(_PHASES) | {"after_publish"}

_HASH_CHUNK = 1 << 20


class MigrateError(Exception):
    """工具自身错误（参数/计划摘要/连接/journal 不一致）→ 退出码 1。"""


class ItemFailure(Exception):
    """单迁移项失败（保留失败阶段与原因；不拖垮其余项，可重跑）。"""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _err(msg):
    sys.stderr.write("migrate_slide_storage: %s\n" % msg)


def _info(msg):
    sys.stdout.write("migrate_slide_storage: %s\n" % msg)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        while True:
            block = f.read(_HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 计划与 journal
# --------------------------------------------------------------------------- #
def load_plan(plan_path) -> tuple:
    """读计划文件 → (header, items_by_id, plan_sha256)。"""
    path = Path(plan_path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise MigrateError("读取计划失败（%s）：%s" % (path, e)) from e
    digest = hashlib.sha256(raw).hexdigest()
    header, items = None, {}
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise MigrateError("计划第 %d 行不是合法 JSON：%s"
                               % (lineno, e)) from e
        rtype = rec.get("record_type")
        if rtype == "plan_header":
            if header is not None:
                raise MigrateError("计划含多个 plan_header")
            header = rec
        elif rtype == "plan_item":
            item_id = rec.get("item_id")
            if not item_id or item_id in items:
                raise MigrateError("计划项 item_id 缺失/重复：%r" % item_id)
            items[item_id] = rec
    if header is None:
        raise MigrateError("计划缺 plan_header（不是 plan_slide_migration 输出）")
    if header.get("tool_version", "").split("-")[0] != TOOL_VERSION.split("-")[0]:
        raise MigrateError("计划工具版本不兼容：%r（本工具 %s）"
                           % (header.get("tool_version"), TOOL_VERSION))
    return header, items, digest


class Journal:
    """持久 journal：逐事件 append + flush + fsync（崩溃后续判的权威证据）。

    不读回自己的 success 字段当成功——重跑方按「journal 事件 + 磁盘/DB 实际
    状态」共同续判（verify 工具则完全独立重核）。
    """

    def __init__(self, path: Path, plan_sha256: str):
        self.path = path
        self.plan_sha256 = plan_sha256
        self._f = None
        self.events = {}   # item_id -> [event, ...]
        self.header = None
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise MigrateError("读取 journal 失败（%s）：%s"
                               % (self.path, e)) from e
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                # 尾部半行（崩溃时写了一半）：截断警告，保留此前完整事件。
                _info("journal 第 %d 行损坏（截断尾行按不存在处理）：%s"
                      % (lineno, e))
                continue
            if rec.get("record") == "apply_header":
                if rec.get("plan_sha256") != self.plan_sha256:
                    raise MigrateError(
                        "journal 属于另一个计划（journal plan=%s，当前=%s）——"
                        "换计划必须换 journal 文件"
                        % (rec.get("plan_sha256"), self.plan_sha256))
                self.header = rec
                continue
            item_id = rec.get("item_id")
            if item_id:
                self.events.setdefault(item_id, []).append(rec)

    def open_append(self, env, quiesce_proof):
        """打开 append 句柄；首次写 apply 头（含停写证据）。"""
        fresh = not self.path.exists()
        parent = self.path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")
        if fresh or self.header is None:
            self._write({
                "ts": _now_iso(), "record": "apply_header",
                "tool_version": TOOL_VERSION,
                "plan_sha256": self.plan_sha256, "env": env,
                "quiesce_proof": quiesce_proof,
            })
            self.header = {"plan_sha256": self.plan_sha256}

    def _write(self, rec):
        self._f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def emit(self, item_id, phase, result, *, detail=None, resumed=False):
        rec = {
            "ts": _now_iso(), "plan_sha256": self.plan_sha256,
            "item_id": item_id, "phase": phase, "result": result,
            "resumed": bool(resumed), "detail": detail or {},
        }
        self._write(rec)
        self.events.setdefault(item_id, []).append(rec)

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None

    # ------- 续判查询（journal+manifest 共同续判的 journal 侧） -------

    def item_state(self, item_id):
        """item 的已确认阶段（1..5；0=未开始）+ 最近一次成功 manifest。

        只统计 result=ok 的阶段推进事件；failed/conflict 事件不推进状态
        （重跑从上一确认阶段续）。manifest 取最近 copied 事件携带的包清单。
        """
        idx = 0
        manifest = None
        for rec in self.events.get(item_id, ()):
            if rec.get("result") != "ok":
                continue
            phase = rec.get("phase")
            if phase in _PHASE_IDX:
                idx = max(idx, _PHASE_IDX[phase])
                if phase == "copied":
                    m = (rec.get("detail") or {}).get("manifest")
                    if m:
                        manifest = m
        return idx, manifest


# --------------------------------------------------------------------------- #
# 源冻结校验（runbook §4.1：核实 plan 摘要、源冻结状态）
# --------------------------------------------------------------------------- #
def _walk_files(base: Path):
    """目录内全部常规文件（相对路径 POSIX、大小、sha256）；不跟随符号链接。"""
    out = []
    stack = [base]
    while stack:
        d = stack.pop()
        entries = sorted(os.scandir(d), key=lambda e: e.name)
        for entry in entries:
            st = entry.stat(follow_symlinks=False)
            if entry.is_symlink():
                raise ItemFailure(
                    "symlink_in_package",
                    {"path": str(Path(entry.path).relative_to(base))})
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                rel = Path(entry.path).relative_to(base).as_posix()
                out.append({"path": rel, "size": int(st.st_size),
                            "sha256": _sha256_file(Path(entry.path))})
    return out


def validate_source_frozen(upload_dir: Path, item) -> int:
    """源重验：入口/伴侣与冻结计划一致（身份、尺寸、内容）。

    返回实测包字节（入口+伴侣常规文件合计）。任何漂移抛 ItemFailure
    （source_changed——中止该项并重新审计，runbook §2.2）。
    """
    src = item["source"]
    name = item["legacy_filename"]
    entry_path = upload_dir / name
    try:
        st = entry_path.lstat()
    except FileNotFoundError:
        raise ItemFailure("source_missing", {"path": str(entry_path)}) from None
    if stat_is_link(st):
        raise ItemFailure("symlink_source", {"path": str(entry_path)})
    if not entry_path.is_file():
        raise ItemFailure("source_not_regular", {"path": str(entry_path)})
    if src.get("entry_size") is not None and \
            int(st.st_size) != int(src["entry_size"]):
        raise ItemFailure("source_size_changed",
                          {"path": str(entry_path),
                           "plan": src.get("entry_size"),
                           "actual": int(st.st_size)})
    if src.get("entry_sha256"):
        actual = _sha256_file(entry_path)
        if actual != str(src["entry_sha256"]).lower():
            raise ItemFailure("source_sha_changed",
                              {"path": str(entry_path)})
    total = int(st.st_size)
    comp = src.get("companion_dir")
    if comp:
        comp_path = upload_dir / comp
        if comp_path.is_dir():
            members = _walk_files(comp_path)
            comp_bytes = sum(m["size"] for m in members)
            if src.get("companion_bytes") is not None and \
                    comp_bytes != int(src["companion_bytes"]):
                raise ItemFailure(
                    "source_companion_changed",
                    {"path": str(comp_path), "plan": src.get("companion_bytes"),
                     "actual": comp_bytes})
            total += comp_bytes
            # R6 审查修复（问题 4）：逐文件冻结清单比对——总字节相同不能证
            # 明内容相同（等长改一字节旧校验抓不住）。成员集合必须与冻结
            # 清单完全一致（拒绝增删），逐文件 size+sha256 全对；冻结清单
            # 缺失（有伴侣却无 frozen 清单）fail-closed 拒绝迁移。
            frozen_members = src.get("companion_members")
            if frozen_members is None:
                raise ItemFailure(
                    "companion_freeze_missing",
                    {"path": str(comp_path),
                     "note": "冻结审计未采集逐文件清单——重跑 frozen 审计"})
            frozen = {m["path"]: m for m in frozen_members}
            actual = {m["path"]: m for m in members}
            if set(frozen) != set(actual):
                raise ItemFailure(
                    "companion_member_set_changed",
                    {"path": str(comp_path),
                     "plan_only": sorted(set(frozen) - set(actual)),
                     "actual_only": sorted(set(actual) - set(frozen))})
            for rel, fm in sorted(frozen.items()):
                am = actual[rel]
                if int(am["size"]) != int(fm["size"]):
                    raise ItemFailure(
                        "companion_size_changed",
                        {"path": str(comp_path / rel),
                         "plan": fm["size"], "actual": am["size"]})
                fsha = (fm.get("sha256") or "").lower()
                if not fsha or ("sha256" not in am) or \
                        str(am.get("sha256") or "").lower() != fsha:
                    raise ItemFailure(
                        "companion_sha_changed",
                        {"path": str(comp_path / rel), "plan": fsha,
                         "actual": am.get("sha256")})
        elif src.get("companion_bytes"):
            raise ItemFailure("source_companion_missing",
                              {"path": str(comp_path)})
    return total


def stat_is_link(st) -> bool:
    return stat_mod.S_ISLNK(st.st_mode)


# --------------------------------------------------------------------------- #
# 复制（合同 §1.2 copied：复制不硬链接；新 inode 隔离）
# --------------------------------------------------------------------------- #
def copy_package_to_staging(upload_dir: Path, item, staging: Path) -> dict:
    """源包 → 私有 staging 完整复制；返回 journal manifest（含逐文件 sha）。

    入口文件复制为 staging/<entry>；MRXS 伴侣目录整树复制为 staging/<stem>/。
    shutil.copyfile/copytree 均为「读字节→写新文件」语义（新 inode），绝无
    硬链接；伴侣树内的符号链接在复制前由 _walk_files 校验拒绝。
    """
    if staging.exists():
        shutil.rmtree(staging)   # 半成品重拷（私有 staging，安全）
    staging.mkdir(parents=True)
    name = item["legacy_filename"]
    entry_dst_name = item["target"]["entry"]
    shutil.copyfile(upload_dir / name, staging / entry_dst_name)
    comp = item["source"].get("companion_dir")
    if comp and (upload_dir / comp).is_dir():
        # 复制前 _walk_files 已拒绝目录树内任何符号链接（合同：不跟随
        # 未知链接；symlinks=False 保证即便竞态出现也只复制受管字节）。
        shutil.copytree(upload_dir / comp, staging / comp, symlinks=False)
    files = _walk_files(staging)
    entry_rel = entry_dst_name
    if not any(f["path"] == entry_rel for f in files):
        raise ItemFailure("copy_entry_missing", {"entry": entry_rel})
    # R6 审查修复（问题 4）复制后绑定：staging 伴侣成员与冻结清单逐文件
    # 比对（复制期间源被改的 TOCTOU 防线——复制出的字节必须是冻结字节）。
    frozen_members = (item.get("source") or {}).get("companion_members")
    if comp and frozen_members:
        staged = {f["path"]: f for f in files if f["path"].startswith(comp + "/")}
        frozen = {"%s/%s" % (comp, m["path"]): m for m in frozen_members}
        if set(staged) != set(frozen):
            raise ItemFailure(
                "companion_drift_during_copy",
                {"frozen_only": sorted(set(frozen) - set(staged)),
                 "copied_only": sorted(set(staged) - set(frozen))})
        for rel, fm in sorted(frozen.items()):
            sm = staged[rel]
            if int(sm["size"]) != int(fm["size"]) or \
                    str(sm.get("sha256") or "").lower() != \
                    (fm.get("sha256") or "").lower():
                raise ItemFailure(
                    "companion_drift_during_copy",
                    {"path": rel, "plan": (fm.get("size"), fm.get("sha256")),
                     "copied": (sm.get("size"), sm.get("sha256"))})
    return {"entry": entry_rel, "files": files,
            "format_hint": name}


def verify_staging(staging: Path, manifest: dict) -> bool:
    """staging 副本逐文件 sha256+大小核对（journal manifest 为基准）。"""
    try:
        norm = slide_storage.validate_manifest(manifest)
    except ValueError:
        return False
    try:
        for f in norm["files"]:
            p = staging / f["path"]
            if not p.is_file() or p.stat().st_size != int(f.get("size") or -1):
                return False
            if f.get("sha256") and _sha256_file(p) != str(f["sha256"]).lower():
                return False
        return (staging / norm["entry"]).is_file()
    except OSError:
        return False


def open_test(staging_or_bundle: Path, manifest: dict):
    """代表性试开（verified 阶段合同项）：slide_io.open_slide 真实可读。"""
    entry = staging_or_bundle / manifest["entry"]
    slide_io.open_slide(entry, format_hint=manifest.get("format_hint"))


# --------------------------------------------------------------------------- #
# DB 辅助
# --------------------------------------------------------------------------- #
_ROW_SQL = ("SELECT slide_id, owner_user_id, legacy_filename, asset_state, "
            "storage_layout, storage_relpath, accounted_bytes FROM slides "
            "WHERE slide_id = %s")


def fetch_row(cur, slide_id):
    cur.execute(_ROW_SQL, (slide_id,))
    return cur.fetchone()


def _connect(database_url=None):
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    try:
        conn = pg_store.connect()
    except Exception as exc:  # noqa: BLE001
        raise MigrateError("数据库连接失败：%s" % exc) from exc
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 单项五态推进
# --------------------------------------------------------------------------- #
def migrate_one_item(conn, journal: Journal, item, *, upload_dir: Path,
                     plan_sha256: str, apply: bool, dry_rows: dict,
                     free_margin_bytes: int, crash_after=None):
    """推进单个 migrate 项到 postverified；返回终态字符串。

    dry-run（apply=False）：只读校验（DB 行 + 源冻结 + 目标现状），把
    「将要做什么」写进 dry_rows，不写 journal、不复制。
    """
    item_id = item["item_id"]
    slide_id = item["slide_id"]
    target = item["target"]
    staging = slide_storage.staging_dir(
        "migrate-%s" % plan_sha256[:16], slide_id, root=upload_dir)
    bundle = slide_storage.bundle_dir(slide_id, root=upload_dir)
    state_idx, manifest = journal.item_state(item_id)

    # ---- 行现状（每次重跑都重读；不以历史快照为准） ----
    with pg_store.transaction(conn):
        with conn.cursor() as cur:
            row = fetch_row(cur, slide_id)
    if row is None:
        raise ItemFailure("row_missing", {"slide_id": slide_id})
    if row["owner_user_id"] != item["owner_user_id"]:
        raise ItemFailure("owner_drift",
                          {"plan": item["owner_user_id"],
                           "actual": row["owner_user_id"]})
    if row["legacy_filename"] != item["legacy_filename"]:
        raise ItemFailure("alias_drift",
                          {"plan": item["legacy_filename"],
                           "actual": row["legacy_filename"]})
    layout = row["storage_layout"]
    relpath = row["storage_relpath"]
    if layout == "id_bundle":
        # 已绑定：仅当 journal+manifest 证明同一迁移项才幂等复用
        if state_idx >= _PHASE_IDX["copied"] and manifest and \
                relpath == target["storage_relpath"] and \
                slide_storage.verify_bundle(slide_id, manifest,
                                            root=upload_dir):
            state_idx = max(state_idx, _PHASE_IDX["bound"])
        else:
            raise ItemFailure("target_conflict_no_journal_proof", {
                "slide_id": slide_id, "storage_relpath": relpath,
                "journal_state": state_idx})
    elif row["asset_state"] != slide_store.SlideState.READY:
        # 状态机主口径：只有 ready 资产可迁移（failed/deleted=已隔离/已删除，
        # deleting=删除在途——一律 skip 披露，不猜）
        journal_skip(journal, item, apply,
                     reason="asset_state_%s" % row["asset_state"])
        return "skipped"

    # ---- planned：源冻结重验 + 空间水位 + 目标无未证先占 ----
    measured_bytes = validate_source_frozen(upload_dir, item)
    if state_idx < _PHASE_IDX["copied"]:
        if bundle.exists() and state_idx < _PHASE_IDX["verified"]:
            raise ItemFailure("target_exists_no_journal_proof",
                              {"bundle": str(bundle)})
        usage = shutil.disk_usage(upload_dir)
        if usage.free < measured_bytes + int(free_margin_bytes):
            raise ItemFailure("insufficient_space",
                              {"free": usage.free, "need": measured_bytes,
                               "margin": int(free_margin_bytes)})
        if not apply:
            dry_rows[item_id] = {
                "action": "copy+verify+publish+bind",
                "package_bytes": measured_bytes}
            return "dry"
        journal.emit(item_id, "planned", "ok")
        _crash_maybe(crash_after, item_id, "planned")

    # ---- copied：私有 staging 完整复制（或幂等复用） ----
    staging_ok = manifest is not None and staging.exists() and \
        verify_staging(staging, manifest)
    if state_idx >= _PHASE_IDX["copied"] and manifest and staging_ok:
        pass  # 幂等复用（journal+staging 双证）
    elif state_idx >= _PHASE_IDX["verified"] and manifest and \
            slide_storage.verify_bundle(slide_id, manifest, root=upload_dir):
        pass  # publish 已发生（staging 已被 rename 走）——bound 段续判
    else:
        if not apply:
            dry_rows[item_id] = {"action": "copy+verify+publish+bind",
                                 "package_bytes": measured_bytes}
            return "dry"
        manifest = copy_package_to_staging(upload_dir, item, staging)
        journal.emit(item_id, "copied", "ok", detail={
            "manifest": manifest, "package_bytes": measured_bytes,
            "reused": bool(state_idx >= _PHASE_IDX["copied"])})
        _crash_maybe(crash_after, item_id, "copied")

    if manifest is None:
        raise ItemFailure("manifest_missing", {"item": item_id})

    # ---- verified：逐文件哈希 + 代表性试开 + fsync ----
    basis = staging if staging.exists() and verify_staging(staging, manifest) \
        else bundle
    if not slide_storage.verify_bundle(slide_id, manifest, root=upload_dir) \
            and not (staging.exists() and verify_staging(staging, manifest)):
        raise ItemFailure("verify_failed",
                          {"basis": str(basis)})
    try:
        open_test(staging if staging.exists() else bundle, manifest)
    except slide_io.SlideValidationError as e:
        raise ItemFailure("open_test_failed", {"code": e.code}) from e
    if staging.exists():
        slide_storage._fsync_tree(staging)
    if state_idx < _PHASE_IDX["verified"]:
        if not apply:
            dry_rows[item_id] = {"action": "publish+bind",
                                 "package_bytes": measured_bytes}
            return "dry"
        journal.emit(item_id, "verified", "ok",
                     detail={"files": len(manifest["files"])})
        _crash_maybe(crash_after, item_id, "verified")

    # ---- bound：no-clobber 发布 + 短事务 CAS 翻转 ----
    if state_idx < _PHASE_IDX["bound"]:
        if not apply:
            dry_rows[item_id] = {"action": "publish+bind",
                                 "package_bytes": measured_bytes}
            return "dry"
        if bundle.exists():
            if not slide_storage.verify_bundle(slide_id, manifest,
                                               root=upload_dir):
                raise ItemFailure("target_conflict_manifest_mismatch",
                                  {"bundle": str(bundle)})
            # 幂等复用：journal+manifest 证明同一迁移项（上方 layout 分支
            # 已拒掉无证据的先占）
        else:
            if not staging.exists():
                raise ItemFailure("staging_missing_before_publish",
                                  {"staging": str(staging)})
            try:
                slide_storage.publish_bundle_no_clobber(
                    staging, slide_id, manifest, root=upload_dir)
            except FileExistsError:
                if not slide_storage.verify_bundle(
                        slide_id, manifest, root=upload_dir):
                    raise ItemFailure(
                        "target_conflict_no_clobber",
                        {"bundle": str(bundle)}) from None
            _crash_maybe(crash_after, item_id, "after_publish")
        accounted = sum(int(f.get("size") or 0) for f in manifest["files"])
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)  # 第一把锁
                outcome = slide_store.bind_id_bundle_layout(
                    slide_id, target["storage_relpath"],
                    accounted_bytes=accounted,
                    expected_state=slide_store.SlideState.READY, conn=conn)
        journal.emit(item_id, "bound", "ok", detail={
            "storage_relpath": target["storage_relpath"],
            "accounted_bytes": accounted, "bind_outcome": outcome})
        _crash_maybe(crash_after, item_id, "bound")
    else:
        # 已 bound（重跑只补 postverify）：行必须已是同参 id_bundle
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                row2 = fetch_row(cur, slide_id)
        if row2["storage_layout"] != "id_bundle" or \
                row2["storage_relpath"] != target["storage_relpath"]:
            raise ItemFailure("bound_row_drift", {
                "layout": row2["storage_layout"],
                "relpath": row2["storage_relpath"]})

    # ---- postverified：独立重读 DB+磁盘 ----
    if not apply:
        dry_rows[item_id] = {"action": "postverify",
                             "package_bytes": measured_bytes}
        return "dry"
    desc = slide_store.resolve_slide_id(slide_id)
    if desc is None:
        raise ItemFailure("postverify_row_missing", {})
    if desc.storage_layout != "id_bundle" or \
            desc.storage_relpath != target["storage_relpath"] or \
            desc.asset_state != slide_store.SlideState.READY or \
            desc.owner_user_id != item["owner_user_id"] or \
            desc.legacy_filename != item["legacy_filename"]:
        raise ItemFailure("postverify_descriptor_mismatch", {
            "layout": desc.storage_layout,
            "relpath": desc.storage_relpath,
            "state": desc.asset_state})
    entry_abs = slide_storage.resolve_descriptor_path(desc, root=upload_dir)
    if not entry_abs.is_file():
        raise ItemFailure("postverify_entry_missing",
                          {"path": str(entry_abs)})
    if not slide_storage.verify_bundle(slide_id, manifest, root=upload_dir):
        raise ItemFailure("postverify_bundle_mismatch", {})
    if not slide_store.authorize_read(desc, actor_user_id=desc.owner_user_id):
        raise ItemFailure("postverify_owner_read_denied", {})
    if state_idx < _PHASE_IDX["postverified"]:
        journal.emit(item_id, "postverified", "ok", detail={
            "entry": str(entry_abs.relative_to(upload_dir))})
        _crash_maybe(crash_after, item_id, "postverified")
    return "postverified"


def journal_skip(journal, item, apply, *, reason):
    if apply:
        journal.emit(item["item_id"], "skipped", "skip",
                     detail={"reason": reason,
                             "action": item.get("action")})


def _crash_maybe(crash_after, item_id, point):
    if crash_after and crash_after[0] == item_id and crash_after[1] == point:
        raise SystemExit(130)  # 模拟 kill（journal 已 fsync；无清理路径）


# --------------------------------------------------------------------------- #
# 非 migrate 项处置（retain_history / quarantine）
# --------------------------------------------------------------------------- #
def settle_non_migrate(conn, journal: Journal, item, *, apply: bool,
                       dry_rows: dict):
    """retain_history：行翻 failed（P1 状态机「验证失败」；与 backfill 幂等）。

    quarantine：legacy 行不动（manual_review 人工决议通道——backfill 重扫
    依赖 legacy 态）；ready 行（物理形态不可信却被回填 ready 的存量）
    force_fail 收口不可读；无行（孤儿/无主伴侣）只披露。
    """
    item_id = item["item_id"]
    action = item["action"]
    slide_id = item.get("slide_id")
    if not slide_id:
        journal_skip(journal, item, apply, reason="no_row_orphan")
        if not apply:
            dry_rows[item_id] = {"action": action, "note": "无行（只披露）"}
        return "skipped"
    with pg_store.transaction(conn):
        with conn.cursor() as cur:
            row = fetch_row(cur, slide_id)
    if row is None:
        journal_skip(journal, item, apply, reason="row_missing")
        return "skipped"
    if row["storage_layout"] == "id_bundle":
        # 已是 id_bundle（新资产/此前已迁移）：不属于 legacy 迁移人群
        journal_skip(journal, item, apply, reason="already_id_bundle")
        return "skipped"
    state = row["asset_state"]

    if action == "retain_history":
        if state == slide_store.SlideState.FAILED:
            journal_skip(journal, item, apply, reason="already_failed")
            return "already"
        if not apply:
            dry_rows[item_id] = {"action": "retain_history",
                                 "row_state": state,
                                 "will": "mark_failed"}
            return "dry"
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)
                if state == slide_store.SlideState.LEGACY:
                    moved = slide_store.mark_failed(
                        slide_id, expected_state=slide_store.SlideState.LEGACY,
                        conn=conn)
                else:
                    moved = slide_store.force_fail(slide_id, conn=conn)
        journal.emit(item_id, "retain_history", "ok" if moved else "skip",
                     detail={"reason": item.get("reason"),
                             "row_state": state, "moved": bool(moved)})
        return "retain_history"

    # quarantine
    if state == slide_store.SlideState.LEGACY:
        journal_skip(journal, item, apply,
                     reason="quarantine_manual_review_pending")
        if not apply:
            dry_rows[item_id] = {"action": "quarantine",
                                 "row_state": state,
                                 "will": "保持 legacy（人工决议后再处置）"}
        return "skipped"
    if state == slide_store.SlideState.READY:
        # 物理形态不可信却被回填 ready（如 symlink 入口）：收口不可读
        if not apply:
            dry_rows[item_id] = {"action": "quarantine",
                                 "row_state": state, "will": "force_fail"}
            return "dry"
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)
                moved = slide_store.force_fail(slide_id, conn=conn)
        journal.emit(item_id, "quarantine", "ok" if moved else "skip",
                     detail={"reason": item.get("reason"),
                             "row_state": state, "moved": bool(moved)})
        return "quarantine"
    journal_skip(journal, item, apply, reason="row_state_%s" % state)
    return "skipped"


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def run_migrate(*, plan_path, apply=False, plan_digest=None, env=None,
                quiesce_proof=None, upload_dir=None, journal_path=None,
                database_url=None, free_margin_bytes=DEFAULT_FREE_MARGIN_BYTES,
                crash_after=None) -> dict:
    """执行迁移；返回摘要 dict。工具错误抛 MigrateError；项失败计 failures。

    crash_after=(item_id, point) 仅供演练/测试注入崩溃（point ∈ _CRASH_POINTS，
    以 SystemExit(130) 模拟 kill——journal 逐事件 fsync 保证续判证据完好）。
    """
    upload_root = Path(upload_dir or os.environ.get("UPLOAD_DIR")
                       or "/data/uploads")
    if not upload_root.is_dir():
        raise MigrateError("UPLOAD_DIR 不存在或不是目录：%s" % upload_root)

    header, items, digest = load_plan(plan_path)
    if apply:
        missing = []
        if not plan_digest:
            missing.append("--plan-digest")
        if not env:
            missing.append("--env")
        if not (quiesce_proof and quiesce_proof.strip()):
            missing.append("--quiesce-proof")
        if missing:
            raise MigrateError(
                "--apply 三件套缺一不可：缺少 %s（防把测试计划打到生产；"
                "停写证据会记录进 journal）" % ", ".join(missing))
        if plan_digest.strip().lower() != digest:
            raise MigrateError(
                "计划摘要不符：--plan-digest=%s，实际 sha256=%s（计划文件"
                "与摘要必须来自同一冻结副本）" % (plan_digest, digest))
        if env.strip() != header.get("env"):
            raise MigrateError(
                "环境标识不符：--env=%s，计划头 env=%s（计划属于 %s）"
                % (env, header.get("env"), header.get("env")))

    journal_file = Path(journal_path) if journal_path else \
        Path(plan_path).parent / "migration-journal.jsonl"
    journal = Journal(journal_file, digest)
    if apply:
        journal.open_append(env.strip(), quiesce_proof.strip())

    conn = _connect(database_url)
    dry_rows = {}
    outcomes = {"postverified": 0, "retain_history": 0, "quarantine": 0,
                "skipped": 0, "already": 0, "dry": 0}
    failures = []
    try:
        for item_id in sorted(items):
            item = items[item_id]
            action = item.get("action")
            try:
                if action == "migrate":
                    if item.get("target") is None:
                        raise ItemFailure("plan_item_missing_target", {})
                    result = migrate_one_item(
                        conn, journal, item, upload_dir=upload_root,
                        plan_sha256=digest, apply=apply, dry_rows=dry_rows,
                        free_margin_bytes=free_margin_bytes,
                        crash_after=crash_after)
                else:
                    result = settle_non_migrate(
                        conn, journal, item, apply=apply, dry_rows=dry_rows)
            except SystemExit:
                raise  # 演练崩溃注入：不写失败事件（模拟被 kill）
            except ItemFailure as exc:
                failures.append({"item_id": item_id, "action": action,
                                 "reason": exc.reason, "detail": exc.detail})
                if apply:
                    journal.emit(item_id, "failed", "error", detail={
                        "reason": exc.reason, "detail": exc.detail})
                continue
            outcomes[result] = outcomes.get(result, 0) + 1
    finally:
        journal.close()
        conn.close()

    summary = {
        "tool": "migrate_slide_storage", "version": TOOL_VERSION,
        "mode": "apply" if apply else "dry-run",
        "plan": str(plan_path), "plan_sha256": digest,
        "env": header.get("env"), "upload_dir": str(upload_root),
        "journal": str(journal_file) if apply else None,
        "outcomes": outcomes, "failures": failures,
        "source_retained": True,
        "quota_touched": False,
    }
    if apply and not failures and outcomes.get("postverified", 0) > 0:
        # 全部到达终态且无失败：清理本计划私有 staging 树（manifest 证据
        # 已持久在 journal；staging 内容已发布/不再需要）
        try:
            removed = slide_storage.remove_staging_tree(
                "migrate-%s" % digest[:16], root=upload_root)
            summary["staging_tree_removed"] = bool(removed)
        except (OSError, ValueError) as e:
            summary["staging_tree_removed"] = False
            summary["staging_cleanup_error"] = str(e)
    return summary


def print_summary(summary: dict):
    o = summary["outcomes"]
    _info("模式=%s 计划=%s（sha256=%s…）env=%s"
          % (summary["mode"], summary["plan"],
             (summary["plan_sha256"] or "")[:16], summary["env"]))
    _info("项结果：%s" % json.dumps(o, ensure_ascii=False, sort_keys=True))
    if summary["failures"]:
        for f in summary["failures"]:
            _info("[fail] %s action=%s reason=%s detail=%s"
                  % (f["item_id"], f["action"], f["reason"],
                     json.dumps(f["detail"], ensure_ascii=False,
                                sort_keys=True)))
        _info("失败 %d 项——修复后重跑（状态机幂等；journal=%s）"
              % (len(summary["failures"]), summary["journal"]))
    _info("不删源：旧平铺文件保留原位；used_bytes 未动（R-12 过渡口径）")


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="legacy 平铺 → objects/<slide_id>/ 物理迁移（默认 dry-run）")
    p.add_argument("--plan", required=True, help="migration-plan.jsonl")
    p.add_argument("--apply", action="store_true",
                   help="实际执行（需 --plan-digest + --env + --quiesce-proof）")
    p.add_argument("--plan-digest", default=None,
                   help="计划文件 sha256（与实际一致才执行）")
    p.add_argument("--env", default=None, help="目标环境标识（须与计划头一致）")
    p.add_argument("--quiesce-proof", default=None,
                   help="停写证据字符串（非空；记录进 journal）")
    p.add_argument("--upload-dir", default=None, help="UPLOAD_DIR 根")
    p.add_argument("--journal", default=None,
                   help="journal 路径（默认与计划同目录 migration-journal.jsonl）")
    p.add_argument("--database-url", default=None, help="PG 连接串")
    p.add_argument("--free-margin-bytes", type=int,
                   default=DEFAULT_FREE_MARGIN_BYTES,
                   help="空间水位安全余量（默认 %d）" % DEFAULT_FREE_MARGIN_BYTES)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.free_margin_bytes < 0:
        _err("--free-margin-bytes 不能为负")
        return EXIT_TOOL_ERROR
    try:
        summary = run_migrate(
            plan_path=args.plan, apply=args.apply,
            plan_digest=args.plan_digest, env=args.env,
            quiesce_proof=args.quiesce_proof, upload_dir=args.upload_dir,
            journal_path=args.journal, database_url=args.database_url,
            free_margin_bytes=args.free_margin_bytes)
    except MigrateError as exc:
        _err(str(exc))
        return EXIT_TOOL_ERROR
    except psycopg.Error as exc:
        _err("数据库错误：%s" % str(exc).split("\n")[0])
        return EXIT_TOOL_ERROR
    print_summary(summary)
    if summary["failures"]:
        return EXIT_ITEM_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
