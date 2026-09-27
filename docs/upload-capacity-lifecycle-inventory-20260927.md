# 上传容量生命周期：调用点清单与锁图（0072 实施基线）

日期：2026-09-27。代码基线：R11 修复实施（0072 迁移配套）。方案：
[task-capacity-lifecycle-repair-agent-plan-20260927.md](task-capacity-lifecycle-repair-agent-plan-20260927.md) §6-A 产物。
R11 审查记录原文副本见 /tmp/slide-id-review-r11/REVIEW.md（审查方维护）；
反例已原样入仓：tests/test_slide_id_review_r11.py。

## 1. 模型

「任务持有容量，租约控制执行」：

- `upload_reservations` 增持有者三元组（holder_kind/holder_id/purpose，
  0072）+ origin（admission/reconcile）。绑定后**不参加 TTL 回收**；
  `expires_at` 退化为执行租约（绑定预约过期可重发；未绑定期保持不复活
  语义）。`upload_reservations_one_open_duty` 部分唯一索引保证同
  (holder, purpose) 至多一份未结算责任。
- 三个操作（plan §4.2）：**准入并绑定**（reserve_upload[_locked] 的
  holder 参数 / bind_reservation_locked）；**核验并续执行租约**
  （renew_reservation_locked：绑定行重发租约；发布 precheck 额外核验
  归属）；**结算或确认清理后释放**（consume_reservation_locked 带持有者
  语境；release_reservation_locked 要求 expect_holder）。
- 判定拆分：`reservation_holds_capacity`（容量责任）vs
  `reservation_is_active`（执行许可）。

## 2. 通道调用点（实施后状态）

| 通道 | 任务表/绑定 | 准入 | 续租约 | 结算 | 放弃/清理 |
|---|---|---|---|---|---|
| V1 单文件（app.py `_api_upload_native_single`） | upload_tasks，`_upload_bind_reservation`（writer 启动前） | `reserve_upload`（hint 预占） | precheck renew | `_settle_publish` consume（holder） | `_upload_abandon_staging` / `_upload_native_fail` → cleanup_part → cleanup_confirmed |
| V1 KFB（api_upload 内联） | 同上 | 同上 | 同上 | `finish_commit`（holder） | `_abort_kfb` → abandon_staging；`_upload_legacy_fail` → cleanup 门 |
| V1 ZIP（`_api_upload_zip`） | upload_tasks + upload_task_items | reserve + `topup_reservation` | `publish_batch_item` per-item renew | 单次 `finish_commit`（Σ已发布，holder） | `_abort_pre` / `_zip_fail_task` → cleanup 门；`_zip_abort_published` 撤包 |
| V2（`api_uploads_*`） | upload_tasks，创建事务内 `bind_reservation_locked` | `reserve_upload`（精确） | 每 PUT chunk `renew_reservation`（绑定重发） | `_settle_publish` / `finish_commit`（holder） | DELETE 取消 / maintain 过期：cleanup_part → cleanup_confirmed（清理确认后释放） |
| COS（ingestion_jobs） | holder=ingestion_job/ingest_local，**准入即绑定** | `_try_admit_txn` reserve_upload_locked(holder) | `renew_active_local_reservations`（同 rid 重发；invalid → 终止进清理） | `worker_settle_ready` consume（holder） | cancel/fail/sweep：先持久化 local_cleanup 责任（保留预约）→ `_local_cleanup_finish` → `confirm_local_cleanup` 释放；失败 `record_local_cleanup_failure` 重试；远端池独立 `finalize_cleanup` |
| 百度批次（baidu_import_batches） | holder=baidu_batch/baidu_import，建批事务内绑定 | `quota_hook`（reserve_upload） | 无（绑定不回收，无需续租） | **闭班事务内** `_consume_reservation`（holder；lease-fenced CAS 恰一次） | 无 ready → 同事务 `_release_reservation`（holder）；远端副本清理独立 |
| 转换（conversion_jobs） | 无预约（配额豁免通道） | — | — | `worker_settle_ready` → `upload_guard.add_used_bytes_locked`（唯一财务 SQL；canonical_settled_bytes 幂等键） | 产物删除 `refund_used_bytes_locked` |

维护/管理路径：`upload_cleanup_pending`（V1/V2 清理重试）＋
`ingestion_jobs.local_cleanup_*`（COS 本地清理重试，与远端 cleanup_*
分列）；admin staging-residue 端点对两类责任统一「清理确认 → 按持有者
释放」收口；`record_cleanup_pending` 只登记不动账（复活补账已拆除）。

## 3. 锁图（全部事务的实际加锁顺序）

```
统一财务锁序（R10 保留）：upload_user_quotas 行 → upload_reservations 行

V2 结算（slide_publish._settle_publish）：
  advisory slide:<sid> → upload_tasks 行 → slides 行 → quota 行 → reservation 行
COS 结算（ingestion_store.worker_settle_ready）：
  advisory slide:<sid> → ingestion_jobs 行 → slides 行 → quota 行 → reservation 行
转换结算（conversion_store.worker_settle_ready）：
  advisory slide:<sid> → conversion_jobs 行 → slides 行 → quota 行（add_used_bytes）
COS 准入（_try_admit_txn）：
  ingestion_jobs 行 → quota 行 → reservation 行 → cos_pool_state 行
COS 续租/异常处置（renew_active_local_reservations）：
  ingestion_jobs 行 → quota 行 → reservation 行
COS 取消/失败/清理确认（cancel_job/fail_job/confirm_local_cleanup）：
  ingestion_jobs 行 → quota 行 → reservation 行
V2 PUT chunk：upload_tasks 行（append_chunk 短事务）→ [renew: quota → reservation]
  （文件侧：每任务 flock chunk.lock 在事务外）
百度闭班（_apply_cancel/_finalize_batch）：
  baidu_import_batches 行 → baidu_import_items 聚合 → quota 行 → reservation 行
删除结算（_slide_delete_settle）：slides deleting CAS → quota 行（refund）
```

审计结论（R10 复核后复核一遍）：

- 无任何路径在持有 quota 行锁后等待 task/job 行（job/batch/task 行总是
  先于财务锁取得；发布路径的 advisory → task 行 → … → quota → reservation
  与准入/续租/清理的 task 行 → quota → reservation 同序）。
- 无路径持数据库锁等待网络或文件树删除：COS 远端清理（worker 网络调用）
  在 finalize_cleanup 短事务之外；本地暂存删除（remove_staging_tree）在
  状态事务提交之后（`_local_cleanup_finish` / `_upload_v2_cleanup_part`）。
- 唯一的 reservation 行 → quota 行旧描述残留已在 slide_publish.py /
  ingestion_store.py 注释中修正（R11 附带清理项）。

## 4. 拆除清单（plan §6-D 落实状态）

- [x] 任务持有预约的 TTL 自动回收（reclaim 加 `holder_id IS NULL`）；
      `upload_cleanup_pending` 反连接豁免一并删除（绑定行天然排除）。
- [x] `record_cleanup_pending` 的 released→reserved 重激活 + 配额补记 +
      30 天延期（函数只登记重试工作）。
- [x] COS 心跳经 `reserve_upload_locked` 重新准入、换 rid 的恢复分支
      （`renew_active_local_reservations` 收敛为重发租约 + 不变量处置）。
- [x] 「活跃任务 + released/missing 记 conflict 后 skipped」分支
      （改 `local_reservation_invalid` 终止进清理编排，R11 P1）。
- [x] COS cancel/fail/sweep 的「先释放后尽力清理」（`_cleanup_staging_tree`
      已删除，统一 local_cleanup 确认后释放）。
- [x] 重复财务 SQL：公开 release 委托 locked（R10）；conversion 的
      used_bytes 直更收口到 `add_used_bytes_locked`；baidu 闭班消费收口到
      带持有者语境的 locked 原语。
- [x] 旧锁序注释（slide_publish.py 三处、ingestion_store.py 头部与
      worker_settle_ready docstring）。
- [x] 以「预约合计相等」替代任务完整性验收的断言（R10 场景 3 与
      R8/R9 回归改写为「责任从未释放」目标，均注明被替代合同）。

## 5. 存量核账

`scripts/reconcile_upload_capacity.py`（0072 配套）：默认只读报告；
`--apply` 在停写维护窗口内幂等应用（绑定一致项 / 异常项终止进清理 /
`--reattach` 对审计过字节的缺失责任建 origin='reconcile' 预约并原子
绑定）。生产执行另行批准。
