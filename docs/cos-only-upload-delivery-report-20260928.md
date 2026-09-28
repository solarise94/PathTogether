# 统一 COS 上传：U0–U6 交付报告（最终版）

日期：2026-09-28。执行方案：[cos-only-upload-agent-plan-20260928.md](cos-only-upload-agent-plan-20260928.md)。
本文是完成报告——**代码与测试已完成；生产部署未执行**（授权边界内）。

## 1. 交付检查点（R16 修订：检查点 A 取消）

2026-09-29 用户裁决：**不保留检查点 A（排空兼容版）**。`9d8d2b0` 的镜像
本身缺 `upload_content.py`（R16 第 4 项；该提交上既有的
`test_containerfile_ships_app_modules` 实跑即失败——检查点提交从未单独过
门禁），且 A 只为让切换时在途的旧 V2 任务续传完成而存在；改为维护窗口内
停写后直接部署 B，旧在途任务由核账工具收口（用户需重新上传）。

| 检查点 | commit | 内容 | 构建方式 |
|---|---|---|---|
| ~~A（排空兼容版）~~ | ~~`9d8d2b0`~~ | 已取消，不构建、不部署 | — |
| **B（最终候选版）** | R16 修复提交（`slide-id-refactor` 分支 `ca0f25e` 之后） | 删除旧传输链路全部代码 + R16 修复 | 现有镜像构建；**必须在该确切提交上单独跑全量门禁并实际构建、启动镜像** |

部署顺序（运维执行，晚间低峰）：

1. 预告：维护窗口内在途的旧上传（V1/V2）会中止，需要重新上传。
2. 维护窗口：边缘停止转发上传请求 → 停旧版本 app、worker、转换子进程
   （ps/日志核对无写 `.staging` 的进程）→ 备份 DB 与 UPLOAD_DIR 并验证可恢复。
3. 部署 B：启动时应用迁移（含 slide ID 重构 0067–0074 与 0075/0076）；
   slide ID 数据迁移按 [slide-id 部署方案](slide-id-refactor-deployment-package-20260926.md)
   执行 plan/migrate/verify。
4. 上传仍关闭：`scripts/upload_drain.py audit` 列出残留旧任务 →
   `scripts/reconcile_upload_capacity.py --plan-out` → `--apply` 收口
   （stop/repair）→ 清理确认 → `scripts/upload_drain.py report` = go。
5. COS 真实桶与三 origin 验收 → capability 开启 → 开放上传。

## 2. U0–U6 执行索引

| 步 | commit | 摘要 |
|---|---|---|
| U0 基线冻结 | `826556d` | 入口/格式×身份×大小矩阵、容量缺口计算（默认池 9.5 GB < 产品 10 GiB）、锁图、语义契约（[u0-baseline](cos-upload-u0-baseline-20260928.md)） |
| U1 服务提取 | `f423a35` | `upload_content.py`（ZIP/转换/验证/格式分派，request 无关；monkeypatch 面保持）；冻结套件 179+77 过 |
| U2 形态扩展 | `0079c15` | 0075 kinds（native/zip/conversion）+ ingestion_job_items；两层大小门禁（413/503）+ 注册表派生词表；worker zip/conversion 责任链；`publish_batch_item` 注入缝；`zip_assemble_plan` 崩溃窗口修复 |
| U3 前端统一 | `be28f26` | uploadFile 恒 COS；删选路/开关/回退；排空过渡恢复旧 V2；processing/多结果展示；i18n 同步 |
| U4 检查点 A | `9d8d2b0` | （已取消）0076 冻结清单 + drain 门禁 + `upload_drain.py`；冻结清单与 `freeze` 已随 R16 删除 |
| U5 检查点 B | `ec06f84` | 删除 8181 行（六路由 + 请求路径助手 + 前端适配器/续传键 + 门禁 + edge conf）；测试迁移（服务级夹具 `publish_test_slide`；端点核心用例按清单注释删除 32 例，场景由统一链路覆盖） |
| U6 验收 | 本文 | 见 §3–§5 |

## 3. 测试与证据

- 定向回归（28 文件，U5 后）：**460 passed + 5 skipped**（r13 按处置约定
  整文件 skip——被测面 V1 早退分支已按方案删除，断言原样保留）。
- vitest：**36 文件 548 passed**（cos-upload 12 例新合同；upload-v2/
  upload-csrf 随适配器删除退役）。
- HistoPilot contract：**49 passed**（HP 无上传 API 代码调用，仅历史文档
  提及——无需 HP 侧改动）。
- 全量 pytest（串行）：见 [results-u6](review-evidence/cos-u6/results.txt)。
- 排空核验演练：`tests/test_upload_drain.py`（report no-go→go / 异常
  staging）+ `tests/test_r16_handoff.py`（终态空目录放行、扫描失败 no-go、
  转换源不阻断）+ R16 反例（已知终态残留 no-go）。

场景覆盖映射（旧→新）：ZIP 多 item/配额/恢复 → `test_cos_ingestion_kinds`；
原生验证/结算/清理 → `test_cos_ingest_worker` + `test_capacity_lifecycle_cos`；
KFB 源字节/幂等/连带作废 → kinds conversion 组；大小/格式门禁 →
`test_ingestion_api`；锁协议 → `test_r12`（2 例）+ R12 系列存量。

## 4. 容量与费用配置草案（上线前运维决策）

- 产品上限（现值）：`UPLOAD_MAX_REQUEST_BYTES` = 10 GiB。
- COS 池结构性准入 = capacity − safety。**当前默认 9.5 GB < 10 GiB → 配置
  门禁未过**（capability fail-closed + 创建 503 `cos_pool_below_product_limit`）。
- 建议（二选一，均为运维决策，代码不动）：
  1. `COS_POOL_CAPACITY_BYTES ≥ 11,237,418,240`（产品上限 + 500 MB safety）；
  2. 或经产品决策下调产品上限（不属本次执行授权）。
- 月预算/生命周期规则核对与原 Phase 5 容量故障演练仍是 capability 开启
  前置门禁（未执行，如实列待办）。

## 5. 待实际执行的生产门禁（未完成，如实标注）

1. 原方案 §8 全矩阵中的「真实桶/真实 MRXS 样本/真实 SMTP」验收。
2. 三 origin CSP/CORS 正向验收 + 费用证据（Phase 0 遗留）。
3. 停写副本演练已在测试层复跑；**生产**部署后核账收口与
   `upload_drain.py report` = go（§1 第 4 步）+ 生产独立门禁（R15 起约定）。
4. capability 仍为 `off`；本执行未触碰生产、未核账 apply、未扩池。

## 6. 保留与审计

- `upload_tasks`/`upload_reservations`/迁移回执保留为只读审计数据（不
  DROP）；`upload_task_store` 的状态机与清理原语保留（admin residue 端点
  + 核账工具消费）。原 0076 冻结清单从未部署，随检查点 A 删除。
- `scripts/upload_drain.py` 保留为 B 部署门禁（audit/report；暂存证据复用
  核账工具的逐成员扫描与阻断项）；`scripts/reconcile_upload_capacity.py`
  不变（历史任务核账）。
- 客户端合同：[cos-upload-client-api-20260928.md](cos-upload-client-api-20260928.md)。

## 7. R16 审查处置（2026-09-29）

审查文档与反例：[review-evidence/r16](review-evidence/r16/REVIEW.md)。整体
复盘结论：架构方向不变；ZIP/KFB 两种新形态把副作用（分配 item 资产、建
可领取转换子任务）放在父任务提交栅栏之前、各自独立事务，是第 1、5 项的
共同根因。修复原则：validating 内每个副作用都在**父任务行锁内**登记在
父任务名下，父任务任何终态事务同步作废它们；源文件在 intent 之后才交给
子任务。不新增补偿器，删除了原尽力而为的补偿函数。

| # | 问题 | 处置 | 验证 |
|---|---|---|---|
| 1 P1 | KFB intent 前取消留下可执行子任务 | 0076 `conversion_jobs.state='held'`（不可领取）；`worker_accept_conversion` 父行锁内建/复用 held 子任务；`_abandon_staging_asset` 终态同事务作废本任务创建的 held 子任务及其产物资产；源文件 intent 后搬入；`worker_settle_source` 同事务 held→queued（先锁子任务再锁配额，与转换 worker 同向）。删除 `ensure_conversion_job`/`enqueue_conversion`/`cancel_conversion_for_failed_upload` | R16 反例；`test_r16_handoff` 不可领取、搬源后崩溃恢复恰一次结算、提交先赢取消被拒、复用既有任务不株连 |
| 2 P1 | ZIP 先解压后补占 | `prepare_zip_bundle` 两遍：第一遍只读中央目录完成全部元数据检查，写入前补占到「压缩源 + 声明展开量」并查水位；第二遍解压以「实际 ≤ 声明」硬上限保证不超预约。先清理暂存后结算，清理失败保持 validating、预约不动 | R16 反例；`test_r16_handoff` 写前拒绝零落盘、清理失败延后结算；`test_zip_guard` 补占断言按新合同改写（注明被替代合同） |
| 3 P1 | 排空 report 漏掉已知终态残留 | 旧任务任意状态/配额身份暂存树非空即 no-go（复用核账 `scan_task_manifest`，扫描失败 no-go）；并入核账 collect/plan_actions 阻断项；转换/百度/切片暂存列为非上传域 | R16 反例；`test_r16_handoff` 空目录放行、符号链接 no-go、转换源不阻断 |
| 4 P1 | 检查点 A 镜像缺依赖 | 用户裁决取消检查点 A；冻结清单（0076 原迁移、`freeze`、`freeze_drain_list`/`is_drain_frozen`）删除；B 须在确切提交上单独过门禁 | §1 |
| 5 P2 | ZIP intent 前重试重复分配 | `worker_bind_zip_items` 父行锁内复用既有绑定、只为缺项分配；终态事务作废仍 staging 的 item 资产 | R16 反例；`test_r16_handoff` intent 前取消 item 资产全 failed |
| 6 P2 | closed 模式引导仍承诺可申请 | 「如何开始」第 1 步 closed 独立文案（中英） | R16 反例；i18n 键覆盖测试 |

R13：整文件 skip 改为只退役 V1 早退分支一例，其余 4 个核账反例恢复运行。

后续产品依赖：计划今后取消 ZIP 上传；MRXS 目前**只能**经 ZIP 上传（裸
`.mrxs` 须连同数据目录打包），取消 ZIP 前需要为 MRXS 提供替代入口或
明确停止支持。
