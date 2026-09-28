# -*- coding: utf-8 -*-
"""R12 文件锁协议验收（保留的锁层不变量；U5 检查点 B 后仅剩模块级用例）。

历史版本经 V1/V2 端点驱动的并发/互斥用例（取消 vs writer 临界区、PUT 分片
× DELETE 互斥、清理 DB 收口失败重试）已随旧上传端点删除退役——同族场景在
COS 统一链路覆盖（tests/test_cos_ingest_worker.py、
test_capacity_lifecycle_cos.py、test_cos_ingestion_kinds.py）；本文件保留
与端点无关的锁协议不变量：

  - 锁竞争超时不是获得写入/清理权（TaskStorageLockTimeout）；
  - 锁文件 inode 跨暂存树删除稳定（锁在树外，运行期不删）；
  - 非法锁键（未登记的 kind / 路径穿越）拒绝。
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub

import pytest  # noqa: E402

import slide_storage  # noqa: E402
import task_storage_lock  # noqa: E402

UPLOAD_DIR = _bootstrap.UPLOAD_DIR


def test_lock_contention_timeout_is_not_success_and_inode_stable():
    tid = "upt_acc_lock1"
    held = threading.Event()
    release = threading.Event()

    def _holder():
        with task_storage_lock.task_storage_lock("upload_task", tid):
            held.set()
            assert release.wait(30)

    t = threading.Thread(target=_holder)
    t.start()
    assert held.wait(30)
    # 竞争者超时：TaskStorageLockTimeout——不得视为获得写入/清理权
    with pytest.raises(task_storage_lock.TaskStorageLockTimeout):
        with task_storage_lock.task_storage_lock("upload_task", tid,
                                                 timeout=0.2):
            raise AssertionError("不应获得锁")
    lock_path = task_storage_lock.task_lock_path("upload_task", tid)
    inode_before = os.stat(lock_path).st_ino
    # 删除暂存树（含整树清理）不影响锁 inode（锁在树外，运行期不删）
    staging = slide_storage.staging_task_dir(tid, root=UPLOAD_DIR)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "x").write_bytes(b"1")
    slide_storage.remove_staging_tree(tid, root=UPLOAD_DIR)
    assert os.stat(lock_path).st_ino == inode_before
    release.set()
    t.join(timeout=30)
    assert not t.is_alive()
    # 释放后可再获（晚到 writer/清理）
    with task_storage_lock.task_storage_lock("upload_task", tid):
        pass


def test_invalid_lock_keys_rejected():
    with pytest.raises(task_storage_lock.TaskStorageLockError):
        task_storage_lock.task_lock_path("workflow", "x")
    with pytest.raises(ValueError):
        task_storage_lock.task_lock_path("upload_task", "../escape")
