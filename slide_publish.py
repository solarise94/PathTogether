# -*- coding: utf-8 -*-
"""slide_publish —— 统一发布编排骨架（slide ID 化重构 P1-A；P3 起接 writer）。

本模块是「代码旁合同」：发布流程、锁顺序、取消/恢复裁决镜像自
docs/slide-id-refactor-p1-contract-20260925.md §3.3/§4/§5 与
docs/slide-id-storage-refactor-agent-plan-20260925.md §3。
worker 可 import 本模块（不得 import app）；文件系统操作全部委托
slide_storage，DB 状态/CAS 全部委托 slide_store——本模块只做编排。

publish_slide 合同（步骤顺序，P3 接线时必须逐步落实现）：

  0. 前置验证：task_ref 指向的任务行存在且归属本 owner；generation 是任务
     当前有效代次（旧 generation 不得写新 generation staging、发布、撤销
     文件或结算——plan §3.2）；slide_id 处于 staging 且 storage_relpath 与
     staged 入口一致；资产 owner 与任务 owner 一致（元数据 owner 不匹配
     说明不变量被破坏，应隔离并告警，**绝不自动修正 owner**，plan §3.2）。
  1. 持久化 publish intent：task_ref、generation、slide_id、owner、manifest、
     哈希、实际结算字节随任务行落库（不能仅保存目标文件名）——保证崩溃
     恢复可判定「全未发布（回滚）/ 已发布（幂等收口）/ 证据冲突
     （fail-closed 告警）」。
  2. 取锁（锁序第一把）：``pg_advisory_xact_lock(hashtext('slide:' ||
     slide_id))``（slide_store.acquire_slide_lock）——所有 publish/delete/
     recovery 的跨进程仲裁点；进程内 mutex 不够（plan §3.1-4）。
  3. 锁内重验：任务 generation 未变、资产仍 staging、任务未取消、预约仍
     有效。取消先赢则不能发布；提交已进入不可撤销段返回稳定
     ``commit_in_progress`` 并继续完成/恢复（plan §3.2）。
  4. 文件系统发布：slide_storage.publish_bundle_no_clobber（同卷原子
     rename / 跨卷私有暂存+完整复制校验；fsync 文件与目录；目标已存在即
     FileExistsError，绝不覆盖）。完整包发布，不能入口先可读、伴侣后到。
  5. 短事务 CAS + 结算（plan §3.1-6；锁序：advisory → 任务行 FOR UPDATE
     → slides 行 → upload_reservations → upload_user_quotas（→ cos_pool_state））：
     slide_store.mark_ready(expected_state=staging, accounted_bytes=…) 与
     consume reservation、标记任务本地提交完成**同一事务**；任一步失败整体
     回滚。COS 远端删除不在这个事务内。
  6. 第 5 步提交后资产才可见：DB ready 是唯一可见性开关；FS 已发布、DB
     未提交的内容依然不可读（authorize_read 拒绝）。恢复按既有任务/ID
     重试，不按名称或 SHA 收养别人的资产。

配额与回收口径（plan §3.3，随 P3/P5 落地）：ready 发布与 used_bytes 增加
同事务且只发生一次（accounted_bytes 幂等键）；删除先置 deleting 再物理
清理，清理成功后按 accounted_bytes 幂等减少实占；重复调用/worker 重启
不得重复减账。

P1 状态：仅本骨架与合同注释；真实接线在 P3（V2/单文件 V1）与 P4
（ZIP/MRXS/转换/导入/COS）。接线前任何 writer 不得绕过本合同直接把文件
提升进 objects/。
"""

from __future__ import annotations


def publish_slide(task_ref, generation, slide_id, manifest, *,
                  owner_user_id=None, accounted_bytes=None, conn=None):
    """按模块 docstring 合同编排发布（P3 接线；P1 占位）。

    参数（接线时的稳定签名）：
      - task_ref：任务引用（upload_tasks.upload_id / ingestion_jobs.job_id /
        conversion_jobs.job_id——按通道映射到各自任务行锁）。
      - generation：任务代次（崩溃恢复/重试的 fencing 键；旧代次拒绝）。
      - slide_id：预分配资产 ID（slide_store.allocate_slide 产物）。
      - manifest：包清单（slide_storage.validate_manifest 形态；entry/files
        相对路径均受 containment 校验）。
      - owner_user_id：任务归属（与资产行 owner 交叉验证）。
      - accounted_bytes：实际结算字节（与 mark_ready 同事务写入）。
      - conn：可选调用方事务（步骤 1/5 的短事务默认自管）。

    P1 不实现——返回 NotImplementedError（合同 §3.3：真实接线在 P3/P4）。
    """
    raise NotImplementedError(
        "publish_slide 按 P3/P4 计划接线（合同 §3.3）；P1 仅落骨架与合同注释："
        "task_ref=%r generation=%r slide_id=%r" % (task_ref, generation, slide_id))
