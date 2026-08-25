# -*- coding: utf-8 -*-
"""Upload V2 分片续传任务存储（docs/upload-resumable-fix-plan.md §3.1，U2）。

双后端（dual-backend 约定，同 user_store/share_store）：

  - ``STORAGE_BACKEND=postgres``：``upload_tasks`` 表（migrations/0017）。
    状态转移在**短事务**内 ``SELECT ... FOR UPDATE`` 锁任务行（§3.1 跨 worker
    锁）；整文件哈希 / OpenSlide 验证 / 文件提升**不在行锁内**（§3.2.5 三段式，
    由 app.py 编排，本模块只提供状态原语）。
  - ``json`` / ``dual``：等价文件记录 ``SHARE_DATA_DIR/upload_tasks.json``
    （0600，fcntl 排他文件锁，写盘 fsync，风格同 user_store_json）。json 形态
    的等价锁 = 同一把文件锁（跨进程互斥；进程内请求线程天然需先抢锁）。
    AUTH_ENABLED=False 的本地上传跑 json 后端：owner / 免认证跳过配额
    （upload_guard.quota_applies），但任务状态机照常工作、可测。

状态机（§3.1，严格串行 offset）：

    active   -- PUT chunk offset==confirmed_offset --> active（confirmed_offset 前进）
    active   -- POST commit 受理 --> committing（短事务：commit_token + 续租）
    committing -- 收尾成功 --> committed
    committing -- 临时基础设施故障 --> active（可重试 commit）
    committing -- 确定性失败（哈希不匹配/非法切片） --> failed
    active   -- DELETE / TTL --> cancelled / expired
    failed   -- DELETE --> cancelled

公开 API（backend 无关，返回普通 dict；时间戳一律 epoch 秒 float）：
  create_task / get_task / append_chunk / begin_commit / finish_commit /
  fail_commit / rollback_committing / cancel_task / expire_task

错误类型（路由映射稳定错误码）：
  TaskNotFound / StateConflict / OffsetMismatch / ChunkConflict / SizeMismatch
  （后四者携带 .task 快照，供 409 响应回当前 confirmed_offset 等进度字段）。
"""

import fcntl
import json
import os
import secrets
import time

import pg_store
import platform_features

# --------------------------------------------------------------------------- #
# env 可调常量（import 期一次性读取；测试用 monkeypatch 改模块属性）
# --------------------------------------------------------------------------- #
def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


#: 任务 TTL（§3.2.4）：默认 24h；每次成功 PUT chunk 刷新 expires_at 并对
#: reservation 续租（upload_guard.renew_reservation）。
UPLOAD_TASK_TTL_SECONDS = _env_int("UPLOAD_TASK_TTL", 24 * 3600)

#: 服务端选定的分片大小（POST /api/uploads 返回给客户端）。
UPLOAD_CHUNK_SIZE = _env_int("UPLOAD_CHUNK_SIZE", 16 * 1024 * 1024)

#: 单片接收上限（防恶意客户端把一个 PUT 当无限大请求用；计数流截停）。
UPLOAD_CHUNK_MAX_BYTES = _env_int("UPLOAD_CHUNK_MAX_BYTES", 64 * 1024 * 1024)

#: commit 受理超时（§3.2.5 崩溃恢复）：committing 停留超过该秒数后惰性恢复。
UPLOAD_COMMIT_TIMEOUT_SECONDS = _env_int("UPLOAD_COMMIT_TIMEOUT", 600)


# --------------------------------------------------------------------------- #
# 状态常量与业务异常
# --------------------------------------------------------------------------- #
STATE_ACTIVE = "active"
STATE_COMMITTING = "committing"
STATE_COMMITTED = "committed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"

ALL_STATES = (STATE_ACTIVE, STATE_COMMITTING, STATE_COMMITTED, STATE_FAILED,
              STATE_CANCELLED, STATE_EXPIRED)


class UploadTaskError(Exception):
    """上传任务存储业务异常基类。"""

    code = "upload_task_error"


class TaskNotFound(UploadTaskError):
    """任务不存在。"""

    code = "upload_not_found"


class StateConflict(UploadTaskError):
    """状态机不允许该转移（携带 .task 快照供响应回进度）。"""

    code = "upload_state_conflict"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


class OffsetMismatch(UploadTaskError):
    """offset 与服务端确认点不对齐（串行模型只接受 ==confirmed_offset）。

    携带 .task 快照：409 响应回当前 confirmed_offset 供客户端对齐（§3.2.2）。
    """

    code = "offset_mismatch"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


class ChunkConflict(UploadTaskError):
    """最后分片重放但 (length, sha256) 不一致（§3.2.1 幂等键冲突）。"""

    code = "chunk_conflict"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


class SizeMismatch(UploadTaskError):
    """confirmed_offset != declared_size（commit 前置）或单片越过 declared_size。"""

    code = "size_mismatch"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


# --------------------------------------------------------------------------- #
# 纯决策函数（双后端共享的状态机判定；输入 task dict）
# --------------------------------------------------------------------------- #
def _or0(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1  # 无 last_chunk_* 时不与任何合法 offset 相等


def _decide_append(task, offset, length, sha256):
    """PUT chunk 的串行 offset / 幂等判定（§3.2.1/§3.2.2）。

    返回 (action, fields)：
      - ``advanced``：offset == confirmed_offset 的新片，fields 为推进字段
        （confirmed_offset / last_chunk_*；expires_at 由调用方补）；
      - ``idempotent``：最后分片同键重放，不重复写（fields=None）；
      - ``progressed``：更早分片（非最后一片），直接回当前进度（fields=None）。
    抛 OffsetMismatch / ChunkConflict / StateConflict / SizeMismatch。
    """
    if task["state"] != STATE_ACTIVE:
        raise StateConflict("任务状态 %r 不可写入分片" % task["state"], task)
    confirmed = int(task["confirmed_offset"])
    if offset > confirmed:
        raise OffsetMismatch(
            "offset=%d 超前于服务端确认点 confirmed_offset=%d（严格串行）"
            % (offset, confirmed), task)
    if offset == confirmed:
        if offset + int(length) > int(task["declared_size"]):
            raise SizeMismatch(
                "分片写越界（offset+length=%d > declared_size=%d）"
                % (offset + int(length), int(task["declared_size"])), task)
        return "advanced", {
            "confirmed_offset": offset + int(length),
            "last_chunk_offset": offset,
            "last_chunk_length": int(length),
            "last_chunk_sha256": sha256,
        }
    # offset < confirmed_offset：幂等 / 冲突 / 更早分片三分支（§3.2.1）
    last_off = _or0(task.get("last_chunk_offset"))
    if offset == last_off:
        same = (int(length) == _or0(task.get("last_chunk_length"))
                and sha256 == (task.get("last_chunk_sha256") or ""))
        if same:
            return "idempotent", None
        raise ChunkConflict(
            "与最后已确认分片（offset=%d）的 length/sha256 不一致，拒绝幂等重放"
            % offset, task)
    if offset < last_off:
        # 更早的分片：直接返回当前进度，不声称完成哈希比对（§3.2.1）
        return "progressed", None
    # last_off < offset < confirmed：重叠分片（客户端中途改分片大小）→ 对齐重传
    raise OffsetMismatch(
        "offset=%d 与最后分片边界不对齐（confirmed_offset=%d，请从确认点续传）"
        % (offset, confirmed), task)


# --------------------------------------------------------------------------- #
# 字段表（两后端同序；PG INSERT 显式列序依赖它）
# --------------------------------------------------------------------------- #
_TASK_FIELDS = (
    "upload_id", "owner_user_id", "filename", "safe_name", "declared_size",
    "chunk_size", "confirmed_offset", "last_chunk_offset", "last_chunk_length",
    "last_chunk_sha256", "sha256_expected", "sha256_actual", "reservation_id",
    "state", "commit_token", "commit_started_at", "expires_at", "created_at",
    "updated_at",
)

# epoch 秒（float/int）入参 → timestamptz 的键
_TS_KEYS = ("commit_started_at", "expires_at", "created_at", "updated_at")


def _new_task_id():
    return "upt_" + secrets.token_hex(12)


def _use_pg():
    return platform_features.current_backend() == "postgres"


def _epoch_to_dt(v):
    import datetime
    return datetime.datetime.fromtimestamp(float(v), tz=datetime.timezone.utc)


# --------------------------------------------------------------------------- #
# json 文件后端（等价文件记录；fcntl 排他锁 = 状态转移互斥）
# --------------------------------------------------------------------------- #
#: json 后端数据文件路径（monkeypatch 目标；None = 按 SHARE_DATA_DIR env 现算）
UPLOAD_TASK_FILE = None


def _task_file():
    """json 后端数据文件路径（UPLOAD_TASK_FILE 未设时按 env 现算，测试友好）。"""
    base = os.environ.get("SHARE_DATA_DIR")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "svs-viewer", "share-data")
    return os.path.join(base, "upload_tasks.json")


def _path():
    return UPLOAD_TASK_FILE or _task_file()


def _with_lock(fn):
    """打开 upload_tasks.json 加排他锁后执行 fn(file_obj)（user_store_json 同款）。"""
    p = _path()
    os.makedirs(os.path.dirname(str(p)) or ".", exist_ok=True)
    if not os.path.exists(p):
        with open(p, "a", encoding="utf-8"):
            pass
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    with open(p, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            return fn(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _load_locked(f):
    f.seek(0)
    raw = f.read()
    if not raw:
        return {"tasks": {}, "meta": {"schema_version": 1}}
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
        raise UploadTaskError("upload_tasks.json 结构损坏")
    return data


def _save_locked(f, data):
    f.seek(0)
    f.truncate()
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.flush()
    os.fsync(f.fileno())


def _json_apply(upload_id, mutate):
    """文件锁内：读任务 → mutate(task)→(fields, result) → 落盘。

    返回 (task_after, result)；任务不存在返回 (None, None)。
    """

    def _do(f):
        data = _load_locked(f)
        task = data["tasks"].get(upload_id)
        if task is None:
            return None, None
        fields, result = mutate(task)
        if fields:
            task.update(fields)
            task["updated_at"] = time.time()
            _save_locked(f, data)
        return task, result

    return _with_lock(_do)


# --------------------------------------------------------------------------- #
# PG 后端（upload_tasks 表；FOR UPDATE 短事务）
# --------------------------------------------------------------------------- #
_PG_COLS = ", ".join(_TASK_FIELDS)


def _pg_connect():
    import psycopg
    platform_features.require_pg_backend("upload_tasks")
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _norm_row(row):
    """PG 行 → 归一化 task dict（timestamptz → epoch 秒 float；数值 int 化）。"""
    if row is None:
        return None
    out = dict(row)
    for k in ("declared_size", "chunk_size", "confirmed_offset",
              "last_chunk_offset", "last_chunk_length"):
        if out.get(k) is not None:
            out[k] = int(out[k])
    for k in _TS_KEYS:
        v = out.get(k)
        out[k] = v.timestamp() if hasattr(v, "timestamp") else (float(v) if v else None)
    return out


def _pg_update(conn, upload_id, fields):
    """短事务内（调用方已持 FOR UPDATE）按 fields UPDATE 并重选行。"""
    sets, params = [], []
    for k, v in fields.items():
        sets.append("%s = %%s" % k)
        params.append(_epoch_to_dt(v) if (k in _TS_KEYS and isinstance(v, (int, float)))
                      else v)
    sets.append("updated_at = now()")
    params.append(upload_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE upload_tasks SET %s WHERE upload_id = %%s"
                    % ", ".join(sets), tuple(params))
        cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s" % _PG_COLS,
                    (upload_id,))
        return cur.fetchone()


def _pg_apply(upload_id, mutate):
    """FOR UPDATE 短事务：锁行 → mutate(row)→(fields, result) → UPDATE。

    返回 (row_after_norm, result)；任务不存在返回 (None, None)。
    事务边界即锁边界：mutate/UPDATE 内不做任何重 IO（§3.2.5）。
    """
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s "
                            "FOR UPDATE" % _PG_COLS, (upload_id,))
                row = cur.fetchone()
            if row is None:
                return None, None
            task = _norm_row(row)
            fields, result = mutate(task)
            new_row = _pg_update(conn, upload_id, fields) if fields else row
            return _norm_row(new_row), result
    finally:
        conn.close()


def _apply(upload_id, mutate):
    """双后端统一的「锁内读-判-写」入口（见 _json_apply/_pg_apply 契约）。"""
    if _use_pg():
        return _pg_apply(upload_id, mutate)
    return _json_apply(upload_id, mutate)


# --------------------------------------------------------------------------- #
# 公共 API（双后端统一）
# --------------------------------------------------------------------------- #
def create_task(owner_user_id, filename, safe_name, declared_size, chunk_size,
                sha256_expected=None, reservation_id=None, ttl_seconds=None):
    """创建任务（state=active，confirmed_offset=0）。返回新任务 dict。"""
    ttl = UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    now = time.time()
    task = {
        "upload_id": _new_task_id(),
        "owner_user_id": str(owner_user_id or ""),
        "filename": str(filename),
        "safe_name": str(safe_name),
        "declared_size": int(declared_size),
        "chunk_size": int(chunk_size),
        "confirmed_offset": 0,
        "last_chunk_offset": None,
        "last_chunk_length": None,
        "last_chunk_sha256": None,
        "sha256_expected": (str(sha256_expected) if sha256_expected else None),
        "sha256_actual": None,
        "reservation_id": (str(reservation_id) if reservation_id else None),
        "state": STATE_ACTIVE,
        "commit_token": None,
        "commit_started_at": None,
        "expires_at": now + ttl,
        "created_at": now,
        "updated_at": now,
    }
    if _use_pg():
        conn = _pg_connect()
        try:
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO upload_tasks (%s) VALUES (%s)"
                        % (_PG_COLS, ", ".join(["%s"] * len(_TASK_FIELDS))),
                        tuple(_epoch_to_dt(task[k])
                              if k in _TS_KEYS and task[k] is not None
                              else task[k]
                              for k in _TASK_FIELDS))
        finally:
            conn.close()
        return task

    def _do(f):
        data = _load_locked(f)
        data["tasks"][task["upload_id"]] = task
        _save_locked(f, data)
        return task

    return _with_lock(_do)


def get_task(upload_id):
    """读任务（不加锁的快照读）。不存在返回 None。"""
    if _use_pg():
        conn = _pg_connect()
        try:
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s"
                                % _PG_COLS, (upload_id,))
                    return _norm_row(cur.fetchone())
        finally:
            conn.close()

    def _do(f):
        return _load_locked(f)["tasks"].get(upload_id)

    return _with_lock(_do)


def append_chunk(upload_id, offset, length, sha256, *, ttl_seconds=None):
    """PUT chunk 的状态转移（短事务/文件锁内判定 + 推进）。

    分片字节的 pwrite 由调用方在**本调用之前**完成（锁外，§3.2.5 同精神：
    重 IO 不入锁）；本调用只做对齐判定与 confirmed_offset 推进。

    返回 (action, task)：action ∈ advanced / idempotent / progressed。
    advanced 与 idempotent 刷新任务 expires_at（§3.2.4；progressed 只读回进度）。
    """
    offset, length = int(offset), int(length)
    ttl = UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)

    def _mutate(task):
        action, fields = _decide_append(task, offset, length, sha256)
        if action == "advanced":
            fields = dict(fields)
            fields["expires_at"] = time.time() + ttl
        return fields, action

    task, action = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return action, task


def begin_commit(upload_id, *, ttl_seconds=None):
    """commit 三段式的短事务 A：active → committing，写 commit_token。§3.2.5

    前置（锁内权威）：state=active 且 confirmed_offset == declared_size。
    返回 (commit_token, task)。
    """
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))
    # 任务 TTL 至少盖过 commit 超时窗口，避免受理后任务先于恢复判定过期
    ttl = max(ttl, 2 * UPLOAD_COMMIT_TIMEOUT_SECONDS)

    def _mutate(task):
        if task["state"] != STATE_ACTIVE:
            raise StateConflict(
                "任务状态 %r 不可受理 commit（仅 active）" % task["state"], task)
        if int(task["confirmed_offset"]) != int(task["declared_size"]):
            raise SizeMismatch(
                "confirmed_offset=%d 未达 declared_size=%d，未传完不可 commit"
                % (int(task["confirmed_offset"]), int(task["declared_size"])), task)
        now = time.time()
        token = "uct_" + secrets.token_hex(16)
        fields = {"state": STATE_COMMITTING, "commit_token": token,
                  "commit_started_at": now, "expires_at": now + ttl}
        return fields, token

    task, token = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return token, task


def finish_commit(upload_id, commit_token, sha256_actual):
    """commit 三段式的短事务 B：token 匹配且仍 committing → committed。§3.2.5

    token 不匹配 / 状态已变 → StateConflict（进程外崩溃后的惰性恢复凭同一
    token 收口；过时 worker 的收口被拒）。
    """

    def _mutate(task):
        if (task["state"] != STATE_COMMITTING
                or task.get("commit_token") != commit_token):
            raise StateConflict(
                "commit 收口失败：任务已不在受理态或 token 不匹配（state=%r）"
                % task["state"], task)
        return {"state": STATE_COMMITTED,
                "sha256_actual": sha256_actual or None}, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def fail_commit(upload_id, commit_token, *, permanent, sha256_actual=None,
                ttl_seconds=None):
    """committing 的失败转移（§3.1 失败类型区分）。

    - ``permanent=True``（确定性失败：整文件哈希不匹配 / _validate_slide_file
      判非法）→ failed：原内容不可改，只能 DELETE 取消后重新上传；
    - ``permanent=False``（临时基础设施故障）→ active：清 token，可重试 commit。
    幂等保护：仅当 state=committing 且 commit_token 匹配时生效（过时 worker
    不得覆盖新一次 commit 受理）。
    """
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))

    def _mutate(task):
        if (task["state"] != STATE_COMMITTING
                or task.get("commit_token") != commit_token):
            raise StateConflict(
                "commit 失败转移被拒：任务已不在受理态或 token 不匹配（state=%r）"
                % task["state"], task)
        if permanent:
            fields = {"state": STATE_FAILED}
            if sha256_actual:
                fields["sha256_actual"] = sha256_actual
        else:
            now = time.time()
            fields = {"state": STATE_ACTIVE, "commit_token": None,
                      "commit_started_at": None, "expires_at": now + ttl}
        return fields, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def rollback_committing(upload_id, *, ttl_seconds=None):
    """惰性恢复原语：committing（已超时）→ active，清 commit 凭据。§3.2.5

    与 fail_commit(permanent=False) 的区别：不校验 token（恢复由当前访问者
    发起，旧 token 无意义），仅要求仍处 committing。
    """
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))

    def _mutate(task):
        if task["state"] != STATE_COMMITTING:
            raise StateConflict(
                "仅 committing 可回滚（当前 %r）" % task["state"], task)
        now = time.time()
        return {"state": STATE_ACTIVE, "commit_token": None,
                "commit_started_at": None, "expires_at": now + ttl}, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def cancel_task(upload_id):
    """DELETE 取消：active|failed → cancelled；cancelled|expired 幂等返回。

    committing / committed → StateConflict（路由映射 409：不阻塞等待长事务，
    也不允许撤回已完成的入库，§3.2.5）。
    """

    def _mutate(task):
        s = task["state"]
        if s in (STATE_ACTIVE, STATE_FAILED):
            return {"state": STATE_CANCELLED, "commit_token": None}, s
        if s in (STATE_CANCELLED, STATE_EXPIRED):
            return None, s  # 幂等：不写
        raise StateConflict("任务状态 %r 不可取消" % s, task)

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def expire_task(upload_id):
    """TTL 到期（惰性触发）：active → expired；其余状态原样返回（不写）。"""

    def _mutate(task):
        if task["state"] == STATE_ACTIVE:
            return {"state": STATE_EXPIRED}, None
        return None, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task
