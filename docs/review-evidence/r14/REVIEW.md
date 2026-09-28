# R14：9826d60 独立复核

R13 定点修复及相关回归独立验证：四组共 30 passed，34.96s。新增三个生命周期/核账反例在真实临时 PostgreSQL 上均失败（3 failed，0.42s）。未修改仓库实现、既有测试或生产；未重跑全量 Python/JS/HP。R13 定点测试通过不代表完整维护核账门禁已满足。

## 1. [P1] 已知终态任务的无预约残留被排除出审计集合

定位：scripts/reconcile_upload_capacity.py:205–218，关联 :243–249。

反例：普通用户 failed 上传任务，无 reservation_id、无 upload_cleanup_pending，但 .staging/<该任务>/transfer/data.svs 实有 100 字节。collect 只在终态有 rid/pending 时加入 items；unknown_dirs 又以“存在任务行”为已知，排除该目录。最终 actions/blockers/pending 全空，dry-run 返回 0。

这不是未知孤儿目录，也不是应豁免用户，恰是必须补齐责任的已知残留；当前审计漏掉它，不能保证迁移后残留均有账。COS 终态只有 local_cleanup_status=pending/failed 才入 items，存在同类静态缺口。

修复要求：先建立 DB 任务/预约/清理记录与实际目录的完整并集，再分类；“已知 ID”只能确定归属候选，不能成为免检证据。终态已知残留而责任不明须 no-go，明确 owner/计量/未结算事实后才能生成补记及清理动作。空目录、豁免身份与真正无责任残留分别判定。新增终态有/无 rid、有/无 pending、有/无字节的矩阵，避免只把本例的 if 条件放宽。

## 2. [P1] 未核对 quota.reserved 与责任行合计，漏账仍返回 go

定位：scripts/reconcile_upload_capacity.py:251–267。

反例：普通用户 active 任务正常绑定 reserved=100 的预约，将 upload_user_quotas.reserved_bytes 置为 0，模拟存量账本漂移。任务归属/绑定没有异常，工具只查询是否超过额度，不计算预约 SUM 与配额账本差额，返回 0 且没有 blocker。

容量账本错误会被原样带过迁移；后续正常准入按低报的账本继续分配空间。核账工具不能因为每个任务都能找到 rid 就认为账平。

修复要求：逐用户双向核对 SUM(state=reserved) 与 quota.reserved_bytes，包含零预约用户、缺配额行、多记和少记、正常 admission 与 reconcile 来源。解释不清的任意差额为 blocker；不直接把账本覆盖为 SUM 或再加一次残留责任掩盖差额。冻结计划纳入原账本及可解释调整，应用事务按 quota→reservation 协议校验，应用后重复验证。新增检测用例首先要求退出 3 且零写入，不要求工具猜测如何自动修复。

## 3. [P1] 核账可把持久 commit intent 的任务强制降级并排入清理

定位：scripts/reconcile_upload_capacity.py:336–350、:599–622；_TARGET_SQL 和 collect 均不读取 commit_intent_json。

反例使用实际 allocate_slide、build_manifest/build_intent、begin_legacy_commit 创建 staging 资产与 committing 任务，真实暂存文件 100 字节，预约缺失。运行 --plan-out --repair-residuals 及 --apply --plan，均通过；任务变 failed，upload_cleanup_pending 新增一行，而 commit intent 仍留在任务上。

核账没有核实资产是否已提升、是否需要提交恢复，就绕过不可取消边界，将恢复所需文件交给清理器。脚本仅在维护环境执行并不能证明该中间态可以安全删除。反例未实际运行清理器，确认的是危险终态与清理工作已经提交。

修复要求：commit intent/commit_token、资产状态及已发布证据进入扫描和冻结前态。有未解决 intent 时优先 blocker，禁止生成 stop/repair，不把其当作普通失去预约的上传。只有独立提交恢复/对账流程确认状态后才能收口；不要在核账脚本中新增盲目撤销或重做发布。覆盖 V1/V2 committing 与 COS validating+intent，断言目标 state、intent、资产、pending、余额与回执均未被阻断操作改变。

## 复现

PathTogether 根目录运行：

```bash
PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest /tmp/slide-id-review-r14/test_r14.py -q --tb=short
```

同目录 conftest 是当前 tests/conftest.py 副本，单独收集该外部文件。原始输出 results.txt；测试源码 test_r14.py。普通用户和任务由真实 store 创建，故障初态在独立 PG 中构造，不连接生产。

整体修复应把“扫描完整集合 → 责任/账本/提交状态分类 → 计划 → 原子应用 → 完整终验”作为同一闭环。不能只增加上述三条特判后仍以既有通过数字宣布全部迁移合同完成。
