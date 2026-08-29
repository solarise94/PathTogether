# -*- coding: utf-8 -*-
"""稳定 lock file + tmp/fsync/replace 的原子 JSON 落盘原语（review 10.2）。

只负责 IO 与锁，不接管各 store 的 schema、异常或迁移（抽象门槛见规格
10.2：至少两个真实调用者——shares 与 upload_tasks——且只共享 IO/锁语义）。

核心不变量：

1. **锁在稳定 lock file 上**（``<data>.lock``，只追加创建、从不 replace）。
   旧实现把 flock 加在数据文件 inode 上，一旦数据写盘改为 tmp+``os.replace``，
   写方之间的互斥就会因 inode 更换而失效（app 与 share_server 两进程共写）。
   锁文件路径由数据文件路径派生，双进程各持一个 fd flock 同一文件即可互斥。

2. **写盘原子性**：同目录 0600 tmp（``tempfile.mkstemp`` 保证权限与唯一名）
   → write → flush → fsync → ``os.replace`` → fsync 目录。任何时刻崩溃，数据
   文件只能是完整的旧版或完整的新版，不存在半截 JSON。

3. **读在锁内**：``read_bytes`` 每次现读当前文件（不缓存），调用方在锁内
   parse/校验/迁移，语义由各 store 自定。

公共 API（窄接口，供 store 内部使用）：

- ``with_locked_file(data_path, fn)``：在稳定锁内执行 ``fn(session)``；
  ``session.read_bytes()`` / ``session.write_bytes(data)`` 即上述读写语义。
- ``corrupt_backup_path(data_path, raw)``：损坏备份唯一名（时间戳 + 原文
  sha256 前缀），供 store 的损坏分支使用。
- ``failpoint(name)``：阶段钩子，生产恒 no-op；测试 monkeypatch 后在
  ``after_write``/``after_fsync``/``before_replace``/``after_replace`` 等
  阶段注入崩溃，验证 failpoint 只留完整旧/新版。

不抛自定义异常：IO 失败按 ``OSError`` 原样上抛，由各 store 分类成自己的
Corrupt/Unavailable 语义（本模块不做错误语义接管）。
"""

import fcntl
import hashlib
import os
import tempfile
import time
from pathlib import Path

__all__ = [
    "with_locked_file",
    "LockedFileSession",
    "corrupt_backup_path",
    "failpoint",
    "lock_path_for",
]


def lock_path_for(data_path) -> Path:
    """数据文件的稳定锁路径：``shares.json`` → ``shares.json.lock``。

    锁文件只被 open("a+") 追加创建、从不被 replace，保证 flock 的 inode
    稳定（跨进程互斥的前提）。
    """
    p = Path(data_path)
    return p.with_name(p.name + ".lock")


def failpoint(name):  # pragma: no cover - 测试钩子，生产恒 no-op
    """崩溃注入钩子（测试 monkeypatch 用）。生产路径直接返回。"""
    return None


class LockedFileSession:
    """稳定锁内的数据文件读写会话（锁由 ``with_locked_file`` 持有）。

    ``read_bytes``/``write_bytes`` 均为纯 IO，不再获取锁（重入会死锁）；
    生命周期内的串行化由外层 flock 保证。
    """

    def __init__(self, data_path):
        self.path = Path(data_path)

    def read_bytes(self) -> bytes:
        """读当前文件全部字节；文件不存在返回 b""（空库由调用方映射）。"""
        try:
            with open(self.path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return b""

    def write_bytes(self, data: bytes) -> None:
        """原子写入：同目录 0600 tmp → fsync → replace → fsync 目录。

        任何阶段失败都会清理 tmp 并原样上抛 OSError；数据文件保持旧版。
        """
        directory = str(self.path.parent) or "."
        # mkstemp 以 O_EXCL + 0600 创建（不受 umask 影响），且与数据文件
        # 同目录 → 与 replace 同一文件系统，rename 原子。
        fd, tmp_name = tempfile.mkstemp(
            prefix="." + self.path.name + ".", suffix=".tmp", dir=directory)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tf:
                failpoint("before_write")
                tf.write(data)
                failpoint("after_write")
                tf.flush()
                os.fsync(tf.fileno())
                failpoint("after_fsync")
            failpoint("before_replace")
            os.replace(tmp_path, self.path)
            failpoint("after_replace")
        except BaseException:
            # 崩溃/失败路径：尽量清掉 tmp（replace 已发生则 unlink 报错忽略）
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _fsync_dir(directory)


def _fsync_dir(directory: str) -> None:
    """fsync 目录，使 replace 的目录项变更持久化。

    个别文件系统不支持对目录 fsync（EINVAL/EPERM）；这是持久化加固而非
    正确性前提（replace 本身已原子），失败按 best-effort 吞掉。
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def with_locked_file(data_path, fn):
    """在数据文件的稳定锁内执行 fn(session)，返回 fn 的返回值。

    - 锁文件 ``<data>.lock`` 以 "a+" 打开（不存在则创建），flock 排他；
    - fn 抛错时锁在 finally 中释放，异常原样上抛；
    - 数据文件本身不要求存在（read_bytes 对 ENOENT 返回 b""）。
    """
    lock_path = lock_path_for(data_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    session = LockedFileSession(data_path)
    with open(lock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            return fn(session)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def corrupt_backup_path(data_path, raw: bytes) -> Path:
    """损坏备份唯一名：``<name>.corrupt-<UTC时间戳>-<sha256[:12]>.bak``。

    时间戳 + 原文内容 hash 前缀保证：同一损坏文件重复备份不互相覆盖，
    不同时间的损坏也可区分（review G1：不得用固定 .bak 名丢掉更早的证据）。
    """
    p = Path(data_path)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return p.with_name("%s.corrupt-%s-%s.bak" % (p.name, stamp, digest))
