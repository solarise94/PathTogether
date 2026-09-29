# -*- coding: utf-8 -*-
"""bdpan CLI 生产源适配器（自平台 baidu_adapter.py 移植，C5-B §6.2）。

移植保真点（与平台 ProductionBaiduAdapter 逐条对应，评审可对照）：

- 子进程只用**参数数组**（无 shell）、子命令白名单、超时、退出码检查；
- ``_redact`` 输出脱敏（剔除敏感子串/控制字符，截断 300 字符）；
- JSON 输出 fail-closed 校验（fs_id 绝不 float、缺 list/has_more 不当空、
  status 形态、未知结构 → connector_output_invalid）；
- 下载/清理只接受 ``<batch_id>/<name>`` 形态**本批副本**相对路径（拒绝
  URL/绝对路径/``..``/前缀不匹配）；绝不 ``bdpan download <分享链接>``；
- 退出码文本 → 稳定错码映射（login > 提取码 > 分享失效 > connector_failed）。

插件侧新增（平台版没有的职责，§6.2）：

- 瞬态码退避重试（``SOURCE_RETRYABLE_CODES``）；
- 相邻源操作最小间隔（客户端限速）。

凭证：``BDPAN_CONFIG_DIR``（经 ``BAIDU_CONNECTOR_HOME`` 注入子进程 env），
认证文件绝不复制入库、绝不进日志。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from .. import errors

ALLOWED_SUBCOMMANDS = frozenset(
    {"version", "transfer", "download", "ls", "rm"})

PAGE_SIZE_MAX = 100

CAPABILITY_LIMITS = {
    "max_depth": 32,
    "max_entries": 10000,
    "page_size": PAGE_SIZE_MAX,
    "enumeration_timeout_seconds": 600,
    "share_ttl_hours": 24,
}

_SHARE_URL_RE = re.compile(r"^https://pan\.baidu\.com/s/[A-Za-z0-9_-]+$")
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,8}$")
_FS_ID_RE = re.compile(r"^[0-9]{1,32}$")
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

#: 非零退出/业务错误脱敏文本 → 稳定错码（顺序即优先级；保守：未知保持
#: connector_failed，不把未知失败误伤成业务码）
_EXIT_TEXT_CODES = (
    (("请先执行", "login"), "connector_unusable"),
    (("提取码", "密码", "password"), "share_password_error"),
    (("分享不存在", "已失效", "已取消"), "share_invalid"),
)


def _redact(text, *secrets):
    out = "" if text is None else str(text)
    for s in secrets:
        if s:
            out = out.replace(str(s), "***")
    out = re.sub(r"[\x00-\x1f\x7f]", " ", out)
    return out[:300]


def _classify_cli_failure(redacted_tail):
    low = redacted_tail.lower()
    for needles, code in _EXIT_TEXT_CODES:
        if any(n.lower() in low for n in needles):
            return code
    return "connector_failed"


# --------------------------------------------------------------------------- #
# JSON 输出 schema 校验（fail-closed）
# --------------------------------------------------------------------------- #

def _coerce_fs_id(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise errors.SourceError("connector_output_invalid", "fs_id 形态非法")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise errors.SourceError("connector_output_invalid", "fs_id 形态非法")


def _coerce_size(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise errors.SourceError("connector_output_invalid", "size 形态非法")
    if isinstance(value, int):
        if value < 0:
            raise errors.SourceError("connector_output_invalid", "size 为负")
        return value
    if isinstance(value, str) and re.match(r"^\d+$", value.strip() or "x"):
        return int(value.strip())
    raise errors.SourceError("connector_output_invalid", "size 形态非法")


def _parse_entry(raw):
    if not isinstance(raw, dict):
        raise errors.SourceError("connector_output_invalid", "条目不是对象")
    fs_id = _coerce_fs_id(raw.get("fs_id"))
    is_dir = raw.get("isdir")
    if is_dir is None:
        is_dir = raw.get("is_dir")
    if not isinstance(is_dir, bool):
        raise errors.SourceError("connector_output_invalid",
                                 "isdir 缺失或形态非法")
    size = _coerce_size(raw.get("size", 0))
    name = raw.get("server_filename") or raw.get("name")
    path = raw.get("path")
    if not name and isinstance(path, str) and path:
        name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not isinstance(name, str) or not name:
        raise errors.SourceError("connector_output_invalid", "文件名缺失")
    if isinstance(path, str) and path:
        rel = path.replace("\\", "/").lstrip("/")
    else:
        rel = name
    return {
        "fs_id": fs_id,
        "name": name,
        "path": path if isinstance(path, str) else "/" + rel,
        "relative_path": rel,
        "size": size,
        "is_dir": is_dir,
    }


def _parse_status_payload(payload, *, allow_status=("success", "ok")):
    if not isinstance(payload, dict):
        raise errors.SourceError("connector_output_invalid", "输出不是对象")
    if payload.get("status") not in allow_status:
        raise errors.SourceError("connector_output_invalid",
                                 "status 缺失或非成功形态")
    return payload


def _parse_listing_payload(payload):
    if isinstance(payload, list):
        return [_parse_entry(e) for e in payload]
    if isinstance(payload, dict):
        rows = payload.get("items")
        if not isinstance(rows, list):
            rows = payload.get("list")
        if isinstance(rows, list):
            return [_parse_entry(e) for e in rows]
    raise errors.SourceError("connector_output_invalid", "列表输出形态未知")


def validate_batch_relpath(remote_path, batch_id=None):
    """远程路径必须恰好 ``<batch_id>/<name>``（相对 /apps/bdpan）。

    （平台 ``_validate_batch_relpath`` 原样语义；独立成模块级函数供单测。）
    """
    if not isinstance(remote_path, str) or not remote_path:
        raise errors.SourceError("cleanup_path_rejected", "远程路径为空")
    if "://" in remote_path:
        raise errors.SourceError(
            "cleanup_path_rejected", "拒绝下载/删除分享 URL（仅本批副本）")
    rel = remote_path.replace("\\", "/")
    if rel.startswith("/"):
        if rel.startswith("/apps/bdpan/"):
            rel = rel[len("/apps/bdpan/"):]
        else:
            raise errors.SourceError(
                "cleanup_path_rejected", "绝对路径越界，仅限本批副本")
    parts = rel.split("/")
    if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):
        raise errors.SourceError(
            "cleanup_path_rejected", "远程路径必须为 <批次>/<文件名>")
    if batch_id is not None and parts[0] != batch_id:
        raise errors.SourceError("cleanup_path_rejected", "非本批次路径，拒绝清理")
    return "/".join(parts)


def normalize_source_dir(path):
    """``--source-dir`` 合同归一（平台同款：``/<分享内相对路径>`` 或 None）。"""
    if path in (None, ""):
        return None
    p = str(path).replace("\\", "/").rstrip("/")
    if not p:
        return None
    return p if p.startswith("/") else "/" + p


class BdpanSource:
    """经核验 bdpan CLI 的生产源适配器 + 插件侧重试/限速。"""

    def __init__(self, bin_path=None, home=None, *, list_timeout=60,
                 transfer_timeout=600, download_timeout=86400,
                 probe_timeout=30, max_attempts=4, backoff_base=2.0,
                 min_interval=0.0, sleep=None, now=None):
        self._bin = bin_path or "bdpan"
        self._home = home
        self._timeouts = {
            "list": float(list_timeout),
            "transfer": float(transfer_timeout),
            "download": float(download_timeout),
            "probe": float(probe_timeout),
        }
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base = max(0.0, float(backoff_base))
        self._min_interval = max(0.0, float(min_interval))
        self._sleep = sleep if sleep is not None else time.sleep
        self._now = now if now is not None else time.monotonic
        self._last_op_at = 0.0
        self._probe = None
        self._tasks = {}  # 进程内 task 登记；重启丢失 → poll unknown
        self._counters = {"list": 0, "transfer": 0, "download": 0,
                          "delete": 0, "probe": 0, "retry": 0,
                          "rate_wait": 0}

    # -- 基础设施 -------------------------------------------------------- #

    def _env(self):
        env = os.environ.copy()
        if self._home:
            env.setdefault("BDPAN_CONFIG_DIR", self._home)
        return env

    def _throttle(self):
        """客户端限速：相邻源操作最小间隔。"""
        if self._min_interval <= 0:
            return
        wait = self._last_op_at + self._min_interval - self._now()
        if wait > 0:
            self._counters["rate_wait"] += 1
            self._sleep(wait)

    def _run(self, argv, timeout, secrets=()):
        if not argv or not isinstance(argv, list) \
                or not all(isinstance(a, str) for a in argv):
            raise errors.SourceError("invalid_argv", "argv 必须为字符串数组")
        if len(argv) > 1 and argv[1] not in ALLOWED_SUBCOMMANDS:
            raise errors.SourceError("invalid_argv",
                                     "子命令不在白名单：%r" % argv[1])
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout,
                env=self._env(),
                cwd=self._home if self._home and os.path.isdir(self._home)
                else None)
        except FileNotFoundError:
            raise errors.SourceError("connector_missing",
                                     "连接器二进制不存在") from None
        except subprocess.TimeoutExpired:
            raise errors.SourceError("connector_timeout",
                                     "连接器执行超时") from None
        except PermissionError:
            raise errors.SourceError("connector_unusable",
                                     "连接器不可执行") from None
        if proc.returncode != 0:
            tail = _redact((proc.stderr or "")[-500:], *secrets)
            raise errors.SourceError(
                _classify_cli_failure(tail),
                "连接器退出码 %d：%s" % (proc.returncode, tail))
        return proc.stdout or ""

    def _run_json(self, argv, timeout, secrets=()):
        stdout = self._run(argv, timeout, secrets)
        try:
            payload = json.loads(stdout)
        except ValueError:
            raise errors.SourceError(
                "connector_output_invalid", "CLI stdout 不是合法 JSON",
                detail_internal=_redact(stdout[:500], *secrets)) from None
        if isinstance(payload, dict) and "code" in payload:
            code = payload["code"]
            if type(code) is not int:
                raise errors.SourceError("connector_output_invalid",
                                         "CLI code 形态非法")
            if code != 0:
                error = payload.get("error")
                # 新批次目录不存在是正常状态；仅 ls 的已核验 -9 可当空列表
                if (argv[1] == "ls" and code == 1
                        and payload.get("data") is None
                        and error == "找不到指定的文件或目录（错误码 -9），"
                                     "请检查路径是否正确。"):
                    return []
                safe = _redact(error, *secrets)
                raise errors.SourceError(_classify_cli_failure(safe),
                                         "连接器业务错误：" + safe)
        return payload

    def _with_retry(self, op, argv, timeout, secrets=()):
        """瞬态码退避重试（§6.2 插件侧重试职责）。"""
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            self._last_op_at = self._now()
            try:
                return op()
            except errors.SourceError as e:
                if not e.retryable or attempt >= self._max_attempts:
                    raise
                self._counters["retry"] += 1
                self._sleep(self._backoff_base * (2 ** (attempt - 1)))

    def _backoff(self, attempt):
        return self._backoff_base * (2 ** max(0, attempt - 1))

    # -- 校验 ------------------------------------------------------------ #

    @staticmethod
    def _validate_share(share):
        if not isinstance(share, str) or not _SHARE_URL_RE.match(share):
            raise errors.SourceError("invalid_share", "分享 URL 未通过白名单校验")

    @staticmethod
    def _validate_code(code):
        if code is None:
            return None
        if not isinstance(code, str) or not _CODE_RE.match(code):
            raise errors.SourceError("invalid_share", "提取码形态非法")
        return code.lower()

    @staticmethod
    def _validate_batch_id(batch_id):
        if not isinstance(batch_id, str) or not _BATCH_ID_RE.match(batch_id):
            raise errors.SourceError("invalid_batch_id", "批次目录名形态非法")
        return batch_id

    def counters(self):
        return dict(self._counters)

    # -- 接口 ------------------------------------------------------------ #

    def capabilities(self):
        """能力快照（不返回认证细节；枚举/导入开关沿用平台 env 约定）。"""
        enumeration_flag = (os.environ.get("BAIDU_ENUMERATION_ENABLED") or
                            "").strip().lower() in ("1", "true", "yes", "on")
        import_flag = (os.environ.get("BAIDU_IMPORT_ENABLED") or
                       "").strip().lower() in ("1", "true", "yes", "on")
        out = {
            "enumeration_available": False,
            "import_available": False,
            "reason_code": None,
            "limits": dict(CAPABILITY_LIMITS),
            "connector_version": None,
        }
        resolved = shutil.which(self._bin) if not os.path.isabs(
            self._bin) else (self._bin if os.path.exists(self._bin) else None)
        if resolved is None:
            out["reason_code"] = "connector_missing"
            return out
        if self._probe is None:
            self._probe = self._probe_connector()
        ok, version = self._probe
        if not ok:
            out["reason_code"] = "connector_unusable"
            return out
        out["connector_version"] = version
        out["enumeration_available"] = enumeration_flag
        out["import_available"] = import_flag
        if not import_flag:
            out["reason_code"] = "import_disabled"
        return out

    def _probe_connector(self):
        self._counters["probe"] += 1
        try:
            stdout = self._run([self._bin, "version"],
                               self._timeouts["probe"])
        except errors.SourceError:
            return (False, None)
        m = re.search(r"bdpan[:：]\s*([0-9][0-9A-Za-z.\-]*)", stdout or "")
        return (True, m.group(1) if m else "unknown")

    def list_share_page(self, share, extraction_code, path, cursor, limit):
        """分享内只读分页列表（绝不转存/下载/删除）。"""
        self._validate_share(share)
        code = self._validate_code(extraction_code)
        if cursor in (None, ""):
            page = 1
        else:
            if not isinstance(cursor, str) or not cursor.isdigit() \
                    or int(cursor) < 1:
                raise errors.SourceError("invalid_cursor", "游标形态非法")
            page = int(cursor)
        try:
            limit = max(1, min(PAGE_SIZE_MAX, int(limit or PAGE_SIZE_MAX)))
        except (TypeError, ValueError):
            raise errors.SourceError("invalid_cursor", "limit 形态非法") from None
        source_dir = normalize_source_dir(path)
        argv = [self._bin, "transfer", "list", share, "--json",
                "--no-check-update"]
        if code:
            argv += ["--pwd", code]
        if source_dir:
            argv += ["--source-dir", source_dir]
        argv += ["--page", str(page), "--page-size", str(limit)]
        self._counters["list"] += 1
        payload = self._with_retry(
            lambda: self._run_json(argv, self._timeouts["list"], (share, code)),
            argv, self._timeouts["list"], (share, code))
        if not isinstance(payload, dict):
            raise errors.SourceError(
                "connector_output_invalid",
                "分享列表输出形态未知（缺 items/list+has_more 不能当空分享）")
        rows = payload.get("items")
        if not isinstance(rows, list):
            rows = payload.get("list")
        if not isinstance(rows, list):
            raise errors.SourceError(
                "connector_output_invalid",
                "分享列表输出形态未知（缺 items/list+has_more 不能当空分享）")
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise errors.SourceError("connector_output_invalid", "has_more 缺失")
        return {
            "items": [_parse_entry(e) for e in rows],
            "has_more": has_more,
            "next_cursor": str(page + 1) if has_more else None,
        }

    def transfer_selected(self, batch_id, share, extraction_code, fs_ids):
        """仅转存所选 fs_id 到 ``/apps/bdpan/<batch_id>/``。"""
        batch_id = self._validate_batch_id(batch_id)
        self._validate_share(share)
        code = self._validate_code(extraction_code)
        if not isinstance(fs_ids, list) or not fs_ids:
            raise errors.SourceError("invalid_fs_id", "fs_id 列表为空")
        norm = []
        for fs_id in fs_ids:
            if not isinstance(fs_id, str) or not _FS_ID_RE.match(fs_id):
                raise errors.SourceError("invalid_fs_id", "fs_id 形态非法")
            norm.append(fs_id)
        argv = [self._bin, "transfer", "select", share, "--json",
                "--no-check-update"]
        for fs_id in norm:
            argv += ["--fsid", fs_id]
        argv += ["--dir", batch_id]
        if code:
            argv += ["--pwd", code]
        self._counters["transfer"] += 1
        payload = self._with_retry(
            lambda: self._run_json(argv, self._timeouts["transfer"],
                                   (share, code)),
            argv, self._timeouts["transfer"], (share, code))
        _parse_status_payload(payload)
        task_id = "btt_" + secrets.token_hex(8)
        self._tasks[task_id] = {"state": "succeeded", "batch_id": batch_id}
        return {"task_id": task_id}

    def poll_transfer(self, task_id):
        """转存状态查询（CLI 无已核验异步任务号：进程内登记；跨进程 unknown）。"""
        if not isinstance(task_id, str):
            raise errors.SourceError("invalid_cursor", "task_id 形态非法")
        rec = self._tasks.get(task_id)
        if rec is None:
            return {"state": "unknown"}
        return {"state": rec["state"]}

    def download_to(self, remote_path, dest_dir):
        """下载**本批副本**（``<batch_id>/<name>``）到本地目录。"""
        rel = validate_batch_relpath(remote_path)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        argv = [self._bin, "download", rel, str(dest), "--json",
                "--no-check-update"]
        self._counters["download"] += 1
        payload = self._with_retry(
            lambda: self._run_json(argv, self._timeouts["download"]),
            argv, self._timeouts["download"])
        _parse_status_payload(payload)
        expected = dest / rel.rsplit("/", 1)[-1]
        if not expected.is_file():
            raise errors.SourceError(
                "connector_output_invalid", "下载后目标文件缺失",
                detail_internal=_redact(str(expected)))

    def list_batch_copies(self, batch_id):
        """列出 ``/apps/bdpan/<batch_id>/`` 下本批副本。"""
        batch_id = self._validate_batch_id(batch_id)
        argv = [self._bin, "ls", batch_id, "--json", "--no-check-update"]
        self._counters["list"] += 1
        payload = self._with_retry(
            lambda: self._run_json(argv, self._timeouts["list"]),
            argv, self._timeouts["list"])
        entries = _parse_listing_payload(payload)
        out = []
        for e in entries:
            if e["is_dir"]:
                continue
            out.append({
                "fs_id": e["fs_id"],
                "name": e["name"],
                "relative_path": "%s/%s" % (batch_id, e["name"]),
                "size": e["size"],
                "is_dir": False,
            })
        return out

    def cleanup_batch_copies(self, batch_id, allowed_relpaths):
        """仅删除 ``<batch_id>/<name>`` 前缀的本批副本（越界路径拒绝）。"""
        batch_id = self._validate_batch_id(batch_id)
        if not isinstance(allowed_relpaths, (list, tuple)):
            raise errors.SourceError("cleanup_path_rejected",
                                     "清理路径列表形态非法")
        for relpath in allowed_relpaths:
            rel = validate_batch_relpath(relpath, batch_id=batch_id)
            argv = [self._bin, "rm", "-f", rel, "--json", "--no-check-update"]
            self._counters["delete"] += 1
            payload = self._with_retry(
                lambda argv=argv: self._run_json(
                    argv, self._timeouts["transfer"]),
                argv, self._timeouts["transfer"])
            _parse_status_payload(payload)
