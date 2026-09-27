# R12：8d49fcb 独立复核

结论：绑定预约不被 TTL 回收的方向正确，相关五组回归独立运行 43 passed（7.21s）。但 A–E 尚不能判定全部完成：新增四个反例均失败。未修改仓库实现、既有测试或生产状态；测试使用独立临时 PG，没有真实 COS 网络操作。

## P1：清理释放前没有排除已领取任务的旧 writer

定位：`ingestion_store.py:1638–1655`，关联 `cos_ingest_worker.py:635–692`。

真实序列：worker 已 claim downloading → 尚未创建文件时另一事务 cancel → 清理看见空树，标 cleaned 并释放预约 → 原 worker 创建目录和文件，pwrite → DB 拒绝迟到的进度更新。结果文件仍存在、预约 released、quota reserved=0、local_cleanup_status=cleaned，不再进入清理重试。

反例在 claim 后的路径派生点注入正常 cancel 调用，用独立数据库事务形成合法交错。只 stub Range 网络返回，不模拟任务状态或修改账本；捕获迟到进度的 IngestionStateError 后，检查实际文件和账本。数据库 generation 校验只能拦住 DB 提交，不能撤销之前的文件写入。

修复要求：建立实际文件 writer 与清理之间的互斥/退出确认协议。清理不能只凭 pending 状态或空目录释放。若采用跨进程任务文件锁，锁锚点必须在待删树之外，writer 获锁后重验任务和 generation，清理等待 writer 退出后重验并删树、收口；不要在持 DB 行锁时等待该文件锁。必须同时覆盖已打开文件句柄和尚未 mkdir 的晚到 writer。对 V1/V2 同类边界按同一协议核查。

## P1：--reattach 恢复活跃 COS 任务却保留本地和远端清理标记

定位：`scripts/reconcile_upload_capacity.py:192–220`。

missing 先被写成 failed + cleanup_status=pending + local_cleanup_status=pending，随后补建预约时仅将 state 改回 uploading。运行一次 retry_local_cleanups，刚恢复的暂存文件被删除，新预约被释放，任务还保持 uploading。实测终态为 uploading + cleaned + released，账本 0；远端同样仍可被清理器领取（此项仅静态核对，未调用真实 COS）。

修复要求：把“补记残留责任”与“允许任务恢复执行”拆开。默认核账只补责任，任务继续停止/清理；若明确允许恢复，须核实原阶段、版本/checkpoint、预算和 commit intent，在一个事务内恢复正确阶段、绑定足够容量、撤销不再适用的清理工作及 token。不能把所有下载/校验阶段都强行改成 uploading，不能仅补两个 status='none' 就认为合同完整。

## P1：终态 pending 存量未迁移，切换后又能被 TTL 回收

定位：`scripts/reconcile_upload_capacity.py:122–136`，关联 `collect` 的活跃任务过滤与 `upload_guard.py:392–396`。

旧版合法存量：failed 上传任务 + 未绑定 reserved 预约 + upload_cleanup_pending + 100 字节残留。新工具 --apply 返回 0，只把 pending 列在报告里，不纳入绑定/核账动作。之后同用户准入 50 字节，旧预约被新回收 SQL 释放；文件仍在，账本只有 50。

修复要求：迁移集合覆盖活跃任务、终态清理责任以及反向悬挂预约，而非只把 pending 当附录。核实 owner/用途/字节后对合法 reserved 责任补绑定；released/missing 残留进入显式补账，consumed 与重复/跨 owner 项阻断人工核实。迁移终验必须证明每份残留都有持续责任。处理不完返回 no-go，不能靠删除旧 pending 豁免后让它自动消失。

## P2：超额已有字节仍走正常准入，核账无法完成

定位：`scripts/reconcile_upload_capacity.py:201–207`，关联 `upload_guard.py:410–434`。

实测：额度 50、实际暂存 100、预约缺失，运行 --apply --reattach 直接抛 QuotaExceeded，整事务回滚。origin='reconcile' 只影响新行来源，不跳过正常准入的容量/并发/每小时限制；现有“超额只报告”测试仅覆盖已在账内的超额，没有覆盖补账导致的超额。

修复要求：增加维护专用的核账补记入口，保留统一财务 SQL、quota→reservation 锁序及幂等键，但不把既有字节视为新上传申请；仅在冻结审计证据匹配且任务停止时补记真实责任。不得简单让任意运行时调用传 origin 即绕过准入。补记后的超额阻止新上传，报告必须重新查询应用后状态；额度不变。普通准入继续严格受限。

## 重跑

在 PathTogether 根目录：

```bash
PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest tests/test_capacity_lifecycle_model.py tests/test_capacity_lifecycle_cos.py tests/test_capacity_lifecycle_channels.py tests/test_reconcile_upload_capacity.py tests/test_slide_id_review_r11.py -q --tb=short
PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest /tmp/slide-id-review-r12/test_r12.py -q --tb=short
```

第二条仅收集外部测试，避免重复加载两个 conftest。外部 conftest 是当前仓库 tests/conftest.py 的副本。结果见同目录 `results.txt`。四例分别验证物理写入与清理、补记恢复、本地旧 pending 迁移、超额补账。

未重跑全量 Python/JS/HP，未将用户报告的全量结果表述为本轮独立验证。COS capability 与生产迁移门禁保持原状。
