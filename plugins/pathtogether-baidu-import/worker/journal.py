# -*- coding: utf-8 -*-
"""每任务持久日志（T12：重启续跑的唯一事实来源）。

- 位置：``<work_root>/<installation_id>/journal/<import_id>.json``——**不在**
  受管任务根 ``…/imports/<import_id>/`` 内（清理确认要求该根为空；日志是
  安装级簿记，不属任务文件）。
- 权限 0600（write_token 属任务级凭证，合同 §2.3 第 3 层；只存活跃任务，
  终态即抹除——绝不进日志/报告/异常文本）。
- 写入协议：tmp + fsync + os.replace 原子替换（读者要么见旧要么见新）。
- ``begin_pending`` 态：begin 发出前先落一条（含幂等键、无 write_token），
  把「begin 响应到达但日志未写」的崩溃窗口收窄为可判定：重启时同键同载荷
  重发 begin——平台首次受理则返回新 write_token；幂等重放（write_token 不
  重发）则本任务不可续（本地无凭证）→ 条目失败 ``write_token_lost``，
  绝不凭空再 begin 第二个任务。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import states

_SECRET_FIELDS = ("write_token",)

#: 记录字段（缺省形态；save 合并式更新）
_FIELDS = (
    "import_id", "item_id", "batch_id", "grant_id", "project_id",
    "idempotency_key", "filename", "format_ext", "declared_size",
    "write_token", "stage", "source_name", "source_size", "needs_convert",
    "artifact_rel", "artifact_size", "artifact_sha256", "delivered_offset",
    "chunk_max_bytes", "slide_id", "artifact_sniff", "declared_sha256",
    "receipt", "cleanup_status", "terminal", "error_code", "updated_at",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    """import_id → 记录 dict（load 全量缓存；save 即时落盘）。"""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._by_item = None  # item_id -> record（惰性）

    # -- 路径 ------------------------------------------------------------ #

    def path_for(self, import_id):
        return self.dir / ("%s.json" % import_id)

    # -- 读 -------------------------------------------------------------- #

    def load_all(self):
        """扫描 journal 目录 → {import_id: record}（损坏文件跳过并保留）。"""
        out = {}
        for p in sorted(self.dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("import_id"):
                out[str(rec["import_id"])] = rec
        return out

    def by_item(self, item_id):
        """item_id → 活跃（未终态）记录（重启续跑的入口索引）。"""
        if self._by_item is None:
            self._by_item = {}
            for rec in self.load_all().values():
                if rec.get("item_id") and not rec.get("terminal"):
                    self._by_item[str(rec["item_id"])] = rec
        rec = self._by_item.get(str(item_id))
        return dict(rec) if rec else None

    def finished_for_item(self, item_id):
        """item_id → 已收口（带 terminal）的历次记录，按 updated_at 升序。

        重跑时据此区分「本地已发布、仅回写丢失」（重放）与「平台
        retry_items 重新排队的失败/取消项」（新一次尝试）。"""
        out = [dict(rec) for rec in self.load_all().values()
               if str(rec.get("item_id") or "") == str(item_id)
               and rec.get("terminal")]
        out.sort(key=lambda r: (str(r.get("updated_at") or ""),
                                str(r.get("import_id"))))
        return out

    # -- 写 -------------------------------------------------------------- #

    def save(self, record):
        """合并式落盘（传入字段覆盖既有值；None 值删除键）。"""
        import_id = str(record.get("import_id") or "")
        if not import_id:
            raise ValueError("journal 记录缺 import_id")
        path = self.path_for(import_id)
        current = {}
        if path.is_file():
            try:
                current = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                current = {}
        merged = dict(current)
        for key in _FIELDS:
            if key in record:
                val = record[key]
                if val is None:
                    merged.pop(key, None)
                else:
                    merged[key] = val
        merged["import_id"] = import_id
        merged["updated_at"] = _now_iso()
        self._atomic_write(path, merged)
        if self._by_item is not None:
            if merged.get("terminal"):
                self._by_item.pop(str(merged.get("item_id")), None)
            else:
                self._by_item[str(merged.get("item_id"))] = dict(merged)
        return dict(merged)

    def begin_pending(self, *, item_id, batch_id, grant_id, project_id,
                      idempotency_key, filename, format_ext, declared_size,
                      source_name, source_size, needs_convert):
        """begin 发出前的先行记录（无 write_token；重启按幂等键重发）。"""
        return self.save({
            "import_id": "pending-%s" % item_id,
            "item_id": item_id, "batch_id": batch_id, "grant_id": grant_id,
            "project_id": project_id, "idempotency_key": idempotency_key,
            "filename": filename, "format_ext": format_ext,
            "declared_size": int(declared_size), "stage": states.QUEUED,
            "source_name": source_name, "source_size": int(source_size),
            "needs_convert": bool(needs_convert), "cleanup_status": "none",
        })

    def attach_import(self, pending_import_id, *, import_id, write_token,
                      chunk_max_bytes, slide_id):
        """begin 成功：挂真实 import_id + write_token（一次性迁移动作）。"""
        path = self.path_for(pending_import_id)
        rec = {}
        if path.is_file():
            try:
                rec = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                rec = {}
        rec.update({"import_id": str(import_id),
                    "write_token": str(write_token),
                    "chunk_max_bytes": int(chunk_max_bytes),
                    "slide_id": slide_id})
        self._atomic_write(self.path_for(import_id), rec)
        if pending_import_id != import_id:
            try:
                path.unlink()
            except OSError:
                pass
        if self._by_item is not None and rec.get("item_id"):
            self._by_item[str(rec["item_id"])] = dict(rec)
        return dict(rec)

    def strip_secret(self, import_id):
        """终态：抹除 write_token（保留其余记录供审计）。"""
        path = self.path_for(import_id)
        if not path.is_file():
            return
        try:
            rec = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        changed = False
        for f in _SECRET_FIELDS:
            if rec.pop(f, None) is not None:
                changed = True
        if changed:
            rec["updated_at"] = _now_iso()
            self._atomic_write(path, rec)

    def active_records(self):
        """未终态记录（重启扫描续跑清单）。"""
        return {iid: dict(rec) for iid, rec in self.load_all().items()
                if not rec.get("terminal")}

    # -- 原子写 ---------------------------------------------------------- #

    @staticmethod
    def _atomic_write(path, payload):
        path = Path(path)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".jnl-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(path))
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
