# -*- coding: utf-8 -*-
"""测试专用 fake 源（C5-B；绝不触网）。

平台 ``FakeBaiduAdapter`` 语义的插件侧等价物：内存分享树 + 批次副本 +
计数器；``PT_ENV=production`` 时 capabilities 恒 unavailable（防测试通道
漏进生产）。真实百度账号属外部门禁——fake 成功不宣称真实可用（§9.1）。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from .. import errors

PAGE_SIZE_MAX = 100


class FakeSource:
    """内存版源适配器：entries 构造分享树；transfers/downloads/deletes 记账。"""

    def __init__(self, entries=None, extraction_code=None, page_size=100,
                 *, now=None):
        #: entries: [{"path": "/a/b.svs", "size": N, "fs_id": "..."?,
        #:            "content": bytes?}]；目录由路径隐式派生
        self._files = {}
        self._next_fsid = 900000000000000
        for e in (entries or []):
            path = str(e["path"]).replace("\\", "/").lstrip("/")
            if not path or path.endswith("/"):
                raise ValueError("fake entry path 必须是文件路径")
            fs_id = str(e.get("fs_id") or self._next_fsid)
            self._next_fsid += 7
            self._files[path] = {
                "fs_id": fs_id,
                "path": path,
                "name": path.rsplit("/", 1)[-1],
                "size": int(e.get("size") or 0),
                "content": e.get("content"),
            }
        self.extraction_code = extraction_code
        self.page_size = page_size
        self._now = now if now is not None else time.monotonic
        self._counters = {"list": 0, "transfer": 0, "download": 0,
                          "delete": 0, "probe": 0, "retry": 0,
                          "rate_wait": 0}
        self.transfers = []      # [(batch_id, share, code, fs_ids)]
        self.downloads = []      # [(remote_path, dest)]
        self.deletes = []        # [remote_path]
        self.copies = {}         # batch_id -> {name: file dict}
        self.tasks = {}          # task_id -> {"state", "batch_id"}
        self.fail_cleanup = False
        self.fail_download_names = set()
        self.fail_download_times = {}   # name -> 剩余失败次数（耗尽后成功）
        self.disable_task_registry = False

    # -- 基础 ------------------------------------------------------------ #

    def capabilities(self):
        import os
        if (os.environ.get("PT_ENV") or "").lower() == "production":
            return {
                "enumeration_available": False,
                "import_available": False,
                "reason_code": "fake_adapter_forbidden_in_production",
                "limits": {"page_size": PAGE_SIZE_MAX},
                "connector_version": None,
            }
        return {
            "enumeration_available": True,
            "import_available": True,
            "reason_code": None,
            "limits": {"page_size": PAGE_SIZE_MAX},
            "connector_version": "fake",
        }

    def counters(self):
        return dict(self._counters)

    def _check_code(self, extraction_code):
        if self.extraction_code is not None and \
                (extraction_code or "").lower() != self.extraction_code:
            raise errors.SourceError("share_password_error", "提取码错误或缺失")

    @staticmethod
    def _norm_dir(path):
        return "" if not path else str(path).replace("\\", "/").strip("/")

    def _children(self, dirpath):
        prefix = dirpath + "/" if dirpath else ""
        children = {}
        for path, f in self._files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if "/" in rest:
                d = rest.split("/", 1)[0]
                children[d] = {"name": d, "is_dir": True, "size": 0,
                               "fs_id": "dir-" + hashlib.sha1(
                                   (prefix + d).encode()).hexdigest()[:12]}
            else:
                children[rest] = {"name": rest, "is_dir": False,
                                  "size": f["size"], "fs_id": f["fs_id"]}
        return [children[k] for k in sorted(children)]

    # -- 接口 ------------------------------------------------------------ #

    def list_share_page(self, share, extraction_code, path, cursor, limit):
        self._counters["list"] += 1
        self._check_code(extraction_code)
        dirpath = self._norm_dir(path)
        page = int(cursor) if cursor else 1
        try:
            req_limit = int(limit) if limit else self.page_size
        except (TypeError, ValueError):
            raise errors.SourceError("invalid_cursor", "limit 形态非法") from None
        eff = max(1, min(PAGE_SIZE_MAX, self.page_size, req_limit))
        items_all = self._children(dirpath)
        start = (page - 1) * eff
        window = items_all[start:start + eff]
        has_more = start + eff < len(items_all)
        return {
            "items": [{
                "fs_id": c["fs_id"],
                "name": c["name"],
                "path": "/" + (dirpath + "/" if dirpath else "") + c["name"],
                "relative_path": (dirpath + "/" if dirpath else "") + c["name"],
                "size": c["size"],
                "is_dir": c["is_dir"],
            } for c in window],
            "has_more": has_more,
            "next_cursor": str(page + 1) if has_more else None,
        }

    def transfer_selected(self, batch_id, share, extraction_code, fs_ids):
        self._counters["transfer"] += 1
        self._check_code(extraction_code)
        if not isinstance(fs_ids, list) or not fs_ids:
            raise errors.SourceError("invalid_fs_id", "fs_id 列表为空")
        known = {f["fs_id"]: f for f in self._files.values()}
        for fs_id in fs_ids:
            if fs_id not in known:
                raise errors.SourceError("invalid_fs_id", "fs_id 不在分享清单中")
        batch = self.copies.setdefault(batch_id, {})
        for fs_id in fs_ids:
            f = known[fs_id]
            batch[f["name"]] = dict(f)
        self.transfers.append(
            (batch_id, share, extraction_code, list(fs_ids)))
        task_id = "btt_fake_" + secrets.token_hex(8)
        self.tasks[task_id] = {"state": "succeeded", "batch_id": batch_id}
        return {"task_id": task_id}

    def poll_transfer(self, task_id):
        if self.disable_task_registry:
            return {"state": "unknown"}
        rec = self.tasks.get(task_id)
        if rec is None:
            return {"state": "unknown"}
        return {"state": rec["state"]}

    def download_to(self, remote_path, dest_dir):
        self._counters["download"] += 1
        parts = str(remote_path).replace("\\", "/").split("/")
        if len(parts) < 2:
            raise errors.SourceError("cleanup_path_rejected", "远程路径越界")
        batch_id, name = parts[0], parts[-1]
        f = self.copies.get(batch_id, {}).get(name)
        if f is None:
            raise errors.SourceError("connector_failed", "副本不存在")
        remaining = self.fail_download_times.get(name, 0)
        if name in self.fail_download_names or remaining > 0:
            if remaining > 0:
                self.fail_download_times[name] = remaining - 1
            raise errors.SourceError("download_failed", "下载失败（注入）")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        content = f.get("content")
        if content is None:
            base = ("fake:%s:%s:" % (f["fs_id"], name)).encode("utf-8")
            size = int(f["size"])
            content = (base * (size // len(base) + 1))[:size] if size else b""
        (dest / name).write_bytes(content)
        self.downloads.append((str(remote_path), str(dest)))
        return None

    def list_batch_copies(self, batch_id):
        self._counters["list"] += 1
        return [{
            "fs_id": f["fs_id"],
            "name": name,
            "relative_path": "%s/%s" % (batch_id, name),
            "size": f["size"],
            "is_dir": False,
        } for name, f in sorted(self.copies.get(batch_id, {}).items())]

    def cleanup_batch_copies(self, batch_id, allowed_relpaths):
        for relpath in (allowed_relpaths or []):
            parts = str(relpath).replace("\\", "/").split("/")
            if len(parts) < 2 or parts[0] != batch_id or any(
                    p in ("", ".", "..") for p in parts):
                raise errors.SourceError(
                    "cleanup_path_rejected", "非本批次路径，拒绝清理")
        if self.fail_cleanup:
            raise errors.SourceError("cleanup_failed", "清理失败（注入）")
        for relpath in allowed_relpaths:
            name = str(relpath).rsplit("/", 1)[-1]
            self.copies.get(batch_id, {}).pop(name, None)
            self._counters["delete"] += 1
            self.deletes.append(str(relpath))
        return None
