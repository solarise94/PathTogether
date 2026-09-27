# -*- coding: utf-8 -*-
"""任务存储文件锁（R12 §3.1/§3.2：写入与清理共用暂存树外的稳定任务锁）。

合同（docs/r12-capacity-lifecycle-fix-agent-plan-20260927.md）：

- 锁路径 ``UPLOAD_DIR/.task-locks/<kind>/<task_id>.lock``——**在 ``.staging``
  树之外**、稳定 inode（运行期不 unlink；删除暂存树不影响锁）。kind 为
  有限枚举，task_id 走 slide_storage 同款组件白名单。
- 任务的所有文件写（mkdir/open/write/truncate/rename/adopt/promote）与
  清理（删整树/删代次）共用这把锁；writer 获锁后**从 DB 重读任务状态/
  代次**再决定是否写（claim 时读过不算——清理可能在 claim 之后提交）。
- 锁序：文件锁在最外层；持 DB 行锁/事务级 advisory 期间**不得**等待文件
  锁；持文件锁时沿用各通道既有 DB 锁序（task/job → quota → reservation）。
- fd 在释放前关闭；清理等待超时不视为删除成功（调用方保留重试责任）。
- **不做隐式可重入**：持锁流程需要触发清理时只能调用 ``*_under_storage_
  lock`` 内部操作（记录待清理），退出最外层锁后再进清理入口——否则
  ``flock`` 第二个 open-file-description 会自等待。
- 部署文件系统必须支持跨进程 ``flock``；本方案不回退到仅 DB generation
  检查（不满足的部署不能上本协议）。
"""

import contextlib
import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

import slide_storage
# 组件白名单与 slide_storage 同源（同仓内部耦合，见 slide_publish 对
# upload_task_store._PG_COLS 的既有先例）；锁键与暂存键同一校验口径。
from slide_storage import _safe_component  # noqa: PLC2701

#: 持有者类型枚举（锁目录 <kind> 段；与 upload_guard.HOLDER_KINDS 对齐，
#: 另含无容量预约但写任务暂存树的转换通道）。
KINDS = frozenset({
    "upload_task",     # V1/V2/ZIP（.staging/<upt_id>/）
    "ingestion_job",   # COS 下载/发布（.staging/<inj_id>/）
    "conversion_job",  # 转换源副本/work（.staging/<cvj_id>/）
    "baidu_batch",     # 百度批次本地暂存（staging_root/<bib_id>/）
})

_LOCK_DIRNAME = ".task-locks"


class TaskStorageLockError(RuntimeError):
    """任务存储锁协议错误（非法键/文件系统不支持等）。"""


class TaskStorageLockTimeout(TaskStorageLockError):
    """等待文件锁超时——**不是**删除/写入成功；调用方保留重试责任。"""


def task_lock_path(kind, task_id, *, root=None) -> Path:
    """锁文件路径（不创建；纯派生 + 键白名单校验）。"""
    if kind not in KINDS:
        raise TaskStorageLockError("未知任务锁类型：%r（合法：%s）"
                                   % (kind, sorted(KINDS)))
    base = Path(root) if root is not None else slide_storage.upload_root()
    return (base / _LOCK_DIRNAME / _safe_component(kind, "lock_kind")
            / (_safe_component(str(task_id), "task_id") + ".lock"))


@contextmanager
def task_storage_lock(kind, task_id, *, root=None, timeout=None,
                      poll_interval=0.01):
    """跨进程任务文件锁（阻塞 flock；``timeout`` 给出时到点抛
    :class:`TaskStorageLockTimeout`）。

    fd 在进入前打开、退出时先解锁再关闭（异常/返回路径一致）。锁目录
    惰性创建；锁文件创建后**不删除**（稳定 inode——清理暂存树不会换锁）。
    """
    path = task_lock_path(kind, task_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise TaskStorageLockError(
                        "任务存储锁 flock 失败（%s/%s）：%s"
                        % (kind, task_id, exc)) from exc
                if deadline is not None and time.monotonic() >= deadline:
                    raise TaskStorageLockTimeout(
                        "等待任务存储锁超时（%s/%s，timeout=%s）——未获得"
                        "写入/清理权，不得视为完成" % (kind, task_id, timeout))
                time.sleep(poll_interval)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def try_task_storage_lock(kind, task_id, *, root=None):
    """非阻塞尝试：获得则 yield True，否则 yield False（不等待）。

    供「可放弃」的观测/维护路径（拿到了才动手，拿不到报告冲突）。"""
    path = task_lock_path(kind, task_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                yield False
                return
            raise TaskStorageLockError(
                "任务存储锁 flock 失败（%s/%s）：%s" % (kind, task_id, exc)
            ) from exc
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
