# -*- coding: utf-8 -*-
"""百度网盘受限连接器适配层（W5，spec §6.1）。

生产实现 :class:`ProductionBaiduAdapter` 经子进程调用**已核验**的
``bdpan`` CLI（本机 3.8.7，argv 均来自实际 ``--help`` 输出与官方
``skills/baidu-drive/reference/bdpan-commands.md``；未从分享页抓 Cookie，
未使用任何未核验参数）。测试注入用 :class:`FakeBaiduAdapter`
（``BAIDU_ADAPTER=fake``；生产容器**绝不**设置该变量）。

argv 表（``<bin>`` = ``BAIDU_CONNECTOR_BIN``，默认 ``bdpan``；全部为
参数数组，``subprocess.run(argv)`` 无 shell；``--json`` / ``--no-check-update``
为全局 flag）：

======================  =======================================================
用途                    argv（顺序：bin → 子命令 → 位置参数 → flags）
======================  =======================================================
能力探测                ``[bin, "version"]``
分享内分页列表          ``[bin, "transfer", "list", <share_url>, "--json",
                        "--no-check-update", "--pwd", <code>?,
                        "--source-dir", <dir>?, "--page", <N>,
                        "--page-size", <K>]``
按选中项转存            ``[bin, "transfer", "select", <share_url>, "--json",
                        "--no-check-update", "--fsid", <fs_id>...,
                        "--dir", <batch_id>, "--pwd", <code>?]``
下载批次副本到暂存      ``[bin, "download", <batch_id>/<name>, <dest_dir>,
                        "--json", "--no-check-update"]``
列本批副本              ``[bin, "ls", <batch_id>, "--json",
                        "--no-check-update"]``
删除本批副本            ``[bin, "rm", "-f", <batch_id>/<name>, "--json",
                        "--no-check-update"]``（逐文件一次调用）
======================  =======================================================

已核验的 JSON 输出契约（官方 bdpan-commands.md + docs/baidu-share-ingestion-open-source-review.md §7.1）：

- ``transfer list --json``：``{"list": [<entry>...], "has_more": <bool>}``；
  entry 含字符串 ``fs_id``、``path``（``/<分享内相对路径>``）、
  ``server_filename``、``size``、``isdir``（缺 ``list``/``has_more`` 视为
  输出无效——缺页必须显式失败，不能当空分享）。
- ``ls --json``：裸 JSON 数组，entry 同上但 ``fs_id`` 为整数（Python
  json 解析为任意精度 int，无 float 精度损失；适配器统一转字符串）。
- ``transfer select --json`` / ``download --json`` / ``rm --json``：
  ``{"status": "success"|"ok", ...}``。

尚未核验（记录为 BLOCKED，L01–L04 真实门槛补验）：
``transfer list`` 顶层容器的精确键名与 ``transfer select`` 是否存在异步
task id。适配器按保守策略处理：select 输出无任务号 → 进程内登记
task_id，``poll_transfer`` 对未知 task_id 返回 ``{"state": "unknown"}``，
由 store 侧用 ``list_batch_copies`` 对账后再决定是否重转存。

安全规则（spec §6.1/§6.3）：

- 子进程只用参数数组、白名单子命令、超时、退出码检查，绝不
  ``shell=True``；
- 异常/日志不含完整分享 URL、提取码、token 或未脱敏 CLI stdout
  （:func:`_redact` 统一脱敏 + 截断）；
- 下载只接受 ``<batch_id>/<name>`` 形态的**本批副本**相对路径（相对
  网盘应用根 ``/apps/bdpan/``），拒绝 URL、绝对路径与 ``..``；绝不使用
  ``bdpan download <分享链接>`` 整份转存路径；
- 清理只接受 ``<batch_id>/<name>`` 前缀路径，越界路径拒绝
  （``cleanup_path_rejected``），删除失败不回滚 ready（store 侧职责）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import secrets
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------- #
# env 配置（默认关闭真实外部动作；生产二进制/认证目录仅由部署配置给定）
# --------------------------------------------------------------------------- #

#: 只读枚举开关（默认 false）
ENV_ENUMERATION_ENABLED = "BAIDU_ENUMERATION_ENABLED"
#: 正式导入（转存/下载/删除）开关（默认 false）
ENV_IMPORT_ENABLED = "BAIDU_IMPORT_ENABLED"
#: 连接器二进制路径（默认 PATH 上的 bdpan）
ENV_CONNECTOR_BIN = "BAIDU_CONNECTOR_BIN"
#: 连接器认证目录（映射为 BDPAN_CONFIG_DIR；认证文件不复制入库）
ENV_CONNECTOR_HOME = "BAIDU_CONNECTOR_HOME"
#: 分享秘密加密密钥（BAIDU_SHARE_SECRET_KEY；缺省则适配器不可用）
ENV_SHARE_SECRET_KEY = "BAIDU_SHARE_SECRET_KEY"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: 子命令白名单（argv 校验用；出现其他子命令直接拒绝）
ALLOWED_SUBCOMMANDS = frozenset(
    {"version", "transfer", "download", "ls", "rm"})

#: 子进程超时（秒；env 可调，测试 monkeypatch 常量）
LIST_TIMEOUT_SECONDS = float(
    os.environ.get("BAIDU_LIST_TIMEOUT_SECONDS") or 60)
TRANSFER_TIMEOUT_SECONDS = float(
    os.environ.get("BAIDU_TRANSFER_TIMEOUT_SECONDS") or 600)
DOWNLOAD_TIMEOUT_SECONDS = float(
    os.environ.get("BAIDU_DOWNLOAD_TIMEOUT_SECONDS") or 86400)
PROBE_TIMEOUT_SECONDS = float(
    os.environ.get("BAIDU_PROBE_TIMEOUT_SECONDS") or 30)

#: 单页条目上限（CLI --page-size 1-100）
PAGE_SIZE_MAX = 100

#: 能力 limits 快照（与 store/迁移默认一致；env 覆盖见 baidu_import_store）
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


class AdapterError(Exception):
    """适配层业务失败。``code`` 稳定（connector_missing /
    connector_unusable / connector_timeout / connector_failed /
    connector_output_invalid / invalid_share / invalid_fs_id /
    invalid_batch_id / invalid_cursor / cleanup_path_rejected /
    cleanup_failed / share_password_error ...）；``message`` 已脱敏，
    内部细节只放 ``detail_internal``（不入普通日志/响应）。"""

    def __init__(self, code, message, detail_internal=None):
        super().__init__(message)
        self.code = code
        self.detail_internal = detail_internal


def _redact(text, *secrets):
    """输出脱敏：剔除敏感子串、控制字符，截断到 300 字符。"""
    out = "" if text is None else str(text)
    for s in secrets:
        if s:
            out = out.replace(str(s), "***")
    out = re.sub(r"[\x00-\x1f\x7f]", " ", out)
    return out[:300]


# --------------------------------------------------------------------------- #
# JSON 输出 schema 校验（fail-closed：未知结构 → connector_output_invalid）
# --------------------------------------------------------------------------- #

def _coerce_fs_id(value):
    """fs_id → 字符串（绝不 float：JSON 浮点即视为无效输出）。"""
    if isinstance(value, bool) or isinstance(value, float):
        raise AdapterError("connector_output_invalid", "fs_id 形态非法")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise AdapterError("connector_output_invalid", "fs_id 形态非法")


def _coerce_size(value):
    """size → int（接受非负 int 或纯数字字符串；float 拒绝）。"""
    if isinstance(value, bool) or isinstance(value, float):
        raise AdapterError("connector_output_invalid", "size 形态非法")
    if isinstance(value, int):
        if value < 0:
            raise AdapterError("connector_output_invalid", "size 为负")
        return value
    if isinstance(value, str) and re.match(r"^\d+$", value.strip() or "x"):
        return int(value.strip())
    raise AdapterError("connector_output_invalid", "size 形态非法")


def _parse_entry(raw):
    """单个文件/目录 entry → 规范化 dict（相对路径不含首 /）。"""
    if not isinstance(raw, dict):
        raise AdapterError("connector_output_invalid", "条目不是对象")
    fs_id = _coerce_fs_id(raw.get("fs_id"))
    is_dir = raw.get("isdir")
    if is_dir is None:
        is_dir = raw.get("is_dir")
    if not isinstance(is_dir, bool):
        raise AdapterError("connector_output_invalid", "isdir 缺失或形态非法")
    size = _coerce_size(raw.get("size", 0))
    name = raw.get("server_filename") or raw.get("name")
    path = raw.get("path")
    if not name and isinstance(path, str) and path:
        name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not isinstance(name, str) or not name:
        raise AdapterError("connector_output_invalid", "文件名缺失")
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
    """transfer/download/rm 的 ``{"status": ...}`` 校验。"""
    if not isinstance(payload, dict):
        raise AdapterError("connector_output_invalid", "输出不是对象")
    status = payload.get("status")
    if status not in allow_status:
        raise AdapterError(
            "connector_output_invalid", "status 缺失或非成功形态")
    return payload


def _parse_listing_payload(payload):
    """``ls --json``（裸数组，官方文档形态）或防御性兼容 ``{"list": []}``。"""
    if isinstance(payload, list):
        return [_parse_entry(e) for e in payload]
    if isinstance(payload, dict):
        rows = payload.get("items")
        if not isinstance(rows, list):
            rows = payload.get("list")
        if isinstance(rows, list):
            return [_parse_entry(e) for e in rows]
    raise AdapterError("connector_output_invalid", "列表输出形态未知")


# --------------------------------------------------------------------------- #
# 生产适配器
# --------------------------------------------------------------------------- #

class ProductionBaiduAdapter:
    """经核验 bdpan CLI 的生产适配器（无网页 Cookie 抓取，无 fake 路径）。"""

    def __init__(self, bin_path=None, home=None):
        self._bin = bin_path or os.environ.get(ENV_CONNECTOR_BIN) or "bdpan"
        self._home = home if home is not None else os.environ.get(
            ENV_CONNECTOR_HOME)
        self._probe = None  # None=未探测；("ok", ver) / ("fail", None)
        self._tasks = {}  # task_id -> {"state", "batch_id"}（进程内；重启丢失→poll unknown）
        self._counters = {"list": 0, "transfer": 0, "download": 0,
                          "delete": 0, "probe": 0}

    # -- 基础设施 ---------------------------------------------------------- #

    def _env(self):
        env = os.environ.copy()
        if self._home:
            env.setdefault("BDPAN_CONFIG_DIR", self._home)
        return env

    def _run(self, argv, timeout, secrets=()):
        """子进程执行（参数数组、无 shell、超时、退出码检查）。"""
        if not argv or not isinstance(argv, list) \
                or not all(isinstance(a, str) for a in argv):
            raise AdapterError("invalid_argv", "argv 必须为字符串数组")
        if len(argv) > 1 and argv[1] not in ALLOWED_SUBCOMMANDS:
            raise AdapterError(
                "invalid_argv", "子命令不在白名单：%r" % argv[1])
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout,
                env=self._env(),
                cwd=self._home if self._home and os.path.isdir(self._home)
                else None)
        except FileNotFoundError:
            raise AdapterError("connector_missing", "连接器二进制不存在") \
                from None
        except subprocess.TimeoutExpired:
            raise AdapterError("connector_timeout", "连接器执行超时") from None
        except PermissionError:
            raise AdapterError("connector_unusable", "连接器不可执行") \
                from None
        if proc.returncode != 0:
            tail = _redact((proc.stderr or "")[-500:], *secrets)
            raise AdapterError(
                "connector_failed", "连接器退出码 %d：%s"
                % (proc.returncode, tail))
        return proc.stdout or ""

    def _run_json(self, argv, timeout, secrets=()):
        stdout = self._run(argv, timeout, secrets)
        try:
            return json.loads(stdout)
        except ValueError:
            raise AdapterError(
                "connector_output_invalid", "CLI stdout 不是合法 JSON",
                detail_internal=_redact(stdout[:500], *secrets)) from None

    # -- 校验辅助 ---------------------------------------------------------- #

    @staticmethod
    def _validate_share(share):
        if not isinstance(share, str) or not _SHARE_URL_RE.match(share):
            raise AdapterError("invalid_share", "分享 URL 未通过白名单校验")

    @staticmethod
    def _validate_code(code):
        if code is None:
            return None
        if not isinstance(code, str) or not _CODE_RE.match(code):
            raise AdapterError("invalid_share", "提取码形态非法")
        return code.lower()

    @staticmethod
    def _validate_batch_id(batch_id):
        if not isinstance(batch_id, str) or not _BATCH_ID_RE.match(batch_id):
            raise AdapterError("invalid_batch_id", "批次目录名形态非法")
        return batch_id

    def counters(self):
        """副作用计数（枚举阶段 transfer/download/delete 必须为 0 的断言源）。"""
        return dict(self._counters)

    # -- 接口 ------------------------------------------------------------ #

    def capabilities(self) -> dict:
        """能力快照（不返回认证细节）。

        reason_code 优先级：``secret_unconfigured`` > ``connector_missing``
        > ``connector_unusable`` > 开关原因（枚举/导入各自独立判定；
        双 unavailable 时给枚举开关原因为主因）。
        """
        enumeration_flag = (os.environ.get(ENV_ENUMERATION_ENABLED) or
                            "").strip().lower() in _TRUTHY
        import_flag = (os.environ.get(ENV_IMPORT_ENABLED) or
                       "").strip().lower() in _TRUTHY
        secret_ok = bool(
            (os.environ.get(ENV_SHARE_SECRET_KEY) or "").strip())
        out = {
            "enumeration_available": False,
            "import_available": False,
            "reason_code": None,
            "limits": dict(CAPABILITY_LIMITS),
            "connector_version": None,
        }
        if not secret_ok:
            out["reason_code"] = "secret_unconfigured"
            return out
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
        if not enumeration_flag:
            out["reason_code"] = "enumeration_disabled"
        elif not import_flag:
            out["reason_code"] = "import_disabled"
        else:
            out["reason_code"] = None
        return out

    def _probe_connector(self):
        """``[bin, "version"]`` 只读探测；返回 (ok, 简短版本号)。"""
        self._counters["probe"] += 1
        try:
            stdout = self._run([self._bin, "version"], PROBE_TIMEOUT_SECONDS)
        except AdapterError:
            return (False, None)
        m = re.search(r"bdpan[:：]\s*([0-9][0-9A-Za-z.\-]*)", stdout or "")
        return (True, m.group(1) if m else "unknown")

    def list_share_page(self, share, extraction_code, path, cursor,
                        limit) -> dict:
        """分享内只读分页列表（绝不转存/下载/删除）。"""
        self._validate_share(share)
        code = self._validate_code(extraction_code)
        if cursor in (None, ""):
            page = 1
        else:
            if not isinstance(cursor, str) or not cursor.isdigit() \
                    or int(cursor) < 1:
                raise AdapterError("invalid_cursor", "游标形态非法")
            page = int(cursor)
        try:
            limit = max(1, min(PAGE_SIZE_MAX, int(limit or PAGE_SIZE_MAX)))
        except (TypeError, ValueError):
            raise AdapterError("invalid_cursor", "limit 形态非法") from None
        if path not in (None, "") and not isinstance(path, str):
            raise AdapterError("invalid_cursor", "分享目录形态非法")
        argv = [self._bin, "transfer", "list", share, "--json",
                "--no-check-update"]
        if code:
            argv += ["--pwd", code]
        if path:
            argv += ["--source-dir", path]
        argv += ["--page", str(page), "--page-size", str(limit)]
        self._counters["list"] += 1
        payload = self._run_json(argv, LIST_TIMEOUT_SECONDS, (share, code))
        if not isinstance(payload, dict):
            raise AdapterError(
                "connector_output_invalid",
                "分享列表输出形态未知（缺 items/list+has_more 不能当空分享）")
        rows = payload.get("items")
        if not isinstance(rows, list):
            rows = payload.get("list")
        if not isinstance(rows, list):
            raise AdapterError(
                "connector_output_invalid",
                "分享列表输出形态未知（缺 items/list+has_more 不能当空分享）")
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise AdapterError("connector_output_invalid", "has_more 缺失")
        items = [_parse_entry(e) for e in rows]
        return {
            "items": items,
            "has_more": has_more,
            "next_cursor": str(page + 1) if has_more else None,
        }

    def transfer_selected(self, batch_id, share, extraction_code,
                          fs_ids) -> dict:
        """仅转存所选 fs_id 到 ``/apps/bdpan/<batch_id>/``（不整份转存）。"""
        batch_id = self._validate_batch_id(batch_id)
        self._validate_share(share)
        code = self._validate_code(extraction_code)
        if not isinstance(fs_ids, list) or not fs_ids:
            raise AdapterError("invalid_fs_id", "fs_id 列表为空")
        norm = []
        for fs_id in fs_ids:
            if not isinstance(fs_id, str) or not _FS_ID_RE.match(fs_id):
                raise AdapterError("invalid_fs_id", "fs_id 形态非法")
            norm.append(fs_id)
        argv = [self._bin, "transfer", "select", share, "--json",
                "--no-check-update"]
        for fs_id in norm:
            argv += ["--fsid", fs_id]
        argv += ["--dir", batch_id]
        if code:
            argv += ["--pwd", code]
        self._counters["transfer"] += 1
        payload = self._run_json(
            argv, TRANSFER_TIMEOUT_SECONDS, (share, code))
        _parse_status_payload(payload)
        task_id = "btt_" + secrets.token_hex(8)
        self._tasks[task_id] = {"state": "succeeded", "batch_id": batch_id}
        return {"task_id": task_id}

    def poll_transfer(self, task_id) -> dict:
        """转存状态查询。CLI select 输出无已核验的异步任务号：本进程内
        登记为 succeeded；跨进程未知 task_id 返回 ``{"state": "unknown"}``，
        调用方（store worker）必须先 ``list_batch_copies`` 对账再决定是否
        重转存，不得无条件二次转存。"""
        if not isinstance(task_id, str):
            raise AdapterError("invalid_cursor", "task_id 形态非法")
        rec = self._tasks.get(task_id)
        if rec is None:
            return {"state": "unknown"}
        return {"state": rec["state"]}

    def download_to(self, remote_path, dest_path) -> None:
        """下载**本批副本**（``<batch_id>/<name>``）到本地目录 dest_path。"""
        rel = self._validate_batch_relpath(remote_path)
        if not isinstance(dest_path, (str, os.PathLike)):
            raise AdapterError("invalid_argv", "目标路径形态非法")
        dest = Path(dest_path)
        dest.mkdir(parents=True, exist_ok=True)
        argv = [self._bin, "download", rel, str(dest), "--json",
                "--no-check-update"]
        self._counters["download"] += 1
        payload = self._run_json(argv, DOWNLOAD_TIMEOUT_SECONDS)
        _parse_status_payload(payload)
        expected = dest / rel.rsplit("/", 1)[-1]
        if not expected.is_file():
            raise AdapterError(
                "connector_output_invalid", "下载后目标文件缺失",
                detail_internal=_redact(str(expected)))

    def list_batch_copies(self, batch_id) -> list:
        """列出 ``/apps/bdpan/<batch_id>/`` 下本批副本。"""
        batch_id = self._validate_batch_id(batch_id)
        argv = [self._bin, "ls", batch_id, "--json", "--no-check-update"]
        self._counters["list"] += 1
        payload = self._run_json(argv, LIST_TIMEOUT_SECONDS)
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

    def cleanup_batch_copies(self, batch_id, allowed_relpaths) -> None:
        """仅删除 ``<batch_id>/<name>`` 前缀的本批副本（越界路径拒绝）。"""
        batch_id = self._validate_batch_id(batch_id)
        if not isinstance(allowed_relpaths, (list, tuple)):
            raise AdapterError("cleanup_path_rejected", "清理路径列表形态非法")
        for relpath in allowed_relpaths:
            rel = self._validate_batch_relpath(relpath, batch_id=batch_id)
            argv = [self._bin, "rm", "-f", rel, "--json",
                    "--no-check-update"]
            self._counters["delete"] += 1
            payload = self._run_json(argv, TRANSFER_TIMEOUT_SECONDS)
            _parse_status_payload(payload)

    @staticmethod
    def _validate_batch_relpath(remote_path, batch_id=None):
        """远程路径必须恰好形如 ``<batch_id>/<name>``（相对 /apps/bdpan）。

        拒绝 URL、``/apps/bdpan/`` 之外的绝对路径、多余/缺失路径段与
        ``..``；batch_id 给定时还要求前缀匹配。批次目录是扁平的（只
        转存单文件），嵌套/越界路径一律拒绝。
        """
        if not isinstance(remote_path, str) or not remote_path:
            raise AdapterError("cleanup_path_rejected", "远程路径为空")
        if "://" in remote_path:
            raise AdapterError(
                "cleanup_path_rejected", "拒绝下载/删除分享 URL（仅本批副本）")
        rel = remote_path.replace("\\", "/")
        if rel.startswith("/"):
            if rel.startswith("/apps/bdpan/"):
                rel = rel[len("/apps/bdpan/"):]
            else:
                raise AdapterError(
                    "cleanup_path_rejected", "绝对路径越界，仅限本批副本")
        parts = rel.split("/")
        if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):
            raise AdapterError(
                "cleanup_path_rejected", "远程路径必须为 <批次>/<文件名>")
        if batch_id is not None and parts[0] != batch_id:
            raise AdapterError(
                "cleanup_path_rejected", "非本批次路径，拒绝清理")
        return "/".join(parts)


# --------------------------------------------------------------------------- #
# 测试专用 fake（生产容器绝不设置 BAIDU_ADAPTER=fake）
# --------------------------------------------------------------------------- #

class FakeBaiduAdapter:
    """内存版适配器：分享树 + 分页 + 副本/计数器，绝不触网。

    仅供测试装配（``BAIDU_ADAPTER=fake`` 或直接构造/monkeypatch
    ``get_adapter``）；``capabilities()`` 在 ``PT_ENV=production`` 时恒
    unavailable，防测试通道漏进生产。
    """

    def __init__(self, entries=None, extraction_code=None, page_size=100):
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
        self._counters = {"list": 0, "transfer": 0, "download": 0,
                          "delete": 0, "probe": 0}
        self.transfers = []      # [(batch_id, share, code, fs_ids)]
        self.downloads = []      # [(remote_path, dest)]
        self.deletes = []        # [remote_path]
        self.copies = {}         # batch_id -> {name: file dict}
        self.tasks = {}          # task_id -> {"state", "batch_id"}
        self._issued_cursors = {}  # (dirpath) -> set(cursor)
        self.duplicate_cursor_from_page = None  # 注入重复游标
        self.fail_cleanup = False
        self.fail_download_names = set()
        self.disable_task_registry = False  # 模拟 worker 重启丢 task 登记

    # -- 基础 ------------------------------------------------------------ #

    def capabilities(self):
        if (os.environ.get("PT_ENV") or "").lower() == "production":
            return {
                "enumeration_available": False,
                "import_available": False,
                "reason_code": "fake_adapter_forbidden_in_production",
                "limits": dict(CAPABILITY_LIMITS),
                "connector_version": None,
            }
        return {
            "enumeration_available": True,
            "import_available": True,
            "reason_code": None,
            "limits": dict(CAPABILITY_LIMITS),
            "connector_version": "fake",
        }

    def counters(self):
        return dict(self._counters)

    def _check_code(self, extraction_code):
        if self.extraction_code is not None and \
                (extraction_code or "").lower() != self.extraction_code:
            raise AdapterError("share_password_error", "提取码错误或缺失")

    @staticmethod
    def _norm_dir(path):
        p = "" if not path else str(path).replace("\\", "/").strip("/")
        return p

    def _children(self, dirpath):
        """dirpath（""=根）下的直接子项（文件 + 隐式目录）。"""
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
        # fake 的 page_size 是硬上限（构造注入；请求 limit 不能放大它）
        try:
            req_limit = int(limit) if limit else self.page_size
        except (TypeError, ValueError):
            raise AdapterError("invalid_cursor", "limit 形态非法") from None
        limit = max(1, min(PAGE_SIZE_MAX, self.page_size, req_limit))
        issued = self._issued_cursors.setdefault(dirpath, set())
        issued.add(str(page))
        items_all = self._children(dirpath)
        start = (page - 1) * limit
        window = items_all[start:start + limit]
        has_more = start + limit < len(items_all)
        next_cursor = str(page + 1) if has_more else None
        # 注入重复游标：指定页起的 next_cursor 原地重复（store 必须显式失败）
        if self.duplicate_cursor_from_page is not None \
                and page >= self.duplicate_cursor_from_page and has_more:
            next_cursor = str(page)
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
            "next_cursor": next_cursor,
        }

    def transfer_selected(self, batch_id, share, extraction_code, fs_ids):
        self._counters["transfer"] += 1
        self._check_code(extraction_code)
        if not isinstance(fs_ids, list) or not fs_ids:
            raise AdapterError("invalid_fs_id", "fs_id 列表为空")
        known = {f["fs_id"]: f for f in self._files.values()}
        for fs_id in fs_ids:
            if fs_id not in known:
                raise AdapterError("invalid_fs_id", "fs_id 不在分享清单中")
        batch = self.copies.setdefault(batch_id, {})
        for fs_id in fs_ids:
            f = known[fs_id]
            batch[f["name"]] = dict(f)
        self.transfers.append((batch_id, share, extraction_code, list(fs_ids)))
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

    def download_to(self, remote_path, dest_path):
        self._counters["download"] += 1
        parts = str(remote_path).replace("\\", "/").split("/")
        if len(parts) < 2:
            raise AdapterError("cleanup_path_rejected", "远程路径越界")
        batch_id, name = parts[0], parts[-1]
        f = self.copies.get(batch_id, {}).get(name)
        if f is None:
            raise AdapterError("connector_failed", "副本不存在")
        if name in self.fail_download_names:
            raise AdapterError("download_failed", "下载失败（注入）")
        dest = Path(dest_path)
        dest.mkdir(parents=True, exist_ok=True)
        content = f.get("content")
        if content is None:
            # 默认内容精确等于声明 size（store 侧下载后校验大小一致）
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
                raise AdapterError(
                    "cleanup_path_rejected", "非本批次路径，拒绝清理")
        if self.fail_cleanup:
            raise AdapterError("cleanup_failed", "清理失败（注入）")
        for relpath in allowed_relpaths:
            name = str(relpath).rsplit("/", 1)[-1]
            self.copies.get(batch_id, {}).pop(name, None)
            self._counters["delete"] += 1
            self.deletes.append(str(relpath))
        return None


#: fake 单例（BAIDU_ADAPTER=fake 时跨 get_adapter() 调用保计数器）
_fake_instance = None


def get_adapter():
    """适配器注入点：``BAIDU_ADAPTER=fake`` → 共享 FakeBaiduAdapter 单例
    （仅测试进程允许设置；生产镜像/部署配置绝不设置该变量），
    否则返回 :class:`ProductionBaiduAdapter`。"""
    global _fake_instance
    if (os.environ.get("BAIDU_ADAPTER") or "").strip().lower() == "fake":
        if _fake_instance is None:
            _fake_instance = FakeBaiduAdapter()
        return _fake_instance
    return ProductionBaiduAdapter()
