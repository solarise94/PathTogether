# 统一 COS 上传：U0–U6 交付报告（最终版）

日期：2026-09-28。执行方案：[cos-only-upload-agent-plan-20260928.md](cos-only-upload-agent-plan-20260928.md)。
本文是完成报告——**代码与测试已完成；生产部署未执行**（授权边界内）。

## 1. 两个交付检查点

| 检查点 | commit | 内容 | 构建方式 |
|---|---|---|---|
| **A（排空兼容版）** | `9d8d2b0`（其上叠加 U0–U3：`826556d`/`f423a35`/`0079c15`/`be28f26`） | 新上传统一 COS（全部形态）；V1/V2 停止新建（`PT_UPLOAD_LEGACY_MODE=drain` + 0076 冻结清单），清单内旧任务可恢复/提交/取消 | 现有镜像构建（docker_entry 不变；迁移 0075/0076 随启动应用） |
| **B（最终候选版）** | `ec06f84`（HEAD） | 删除旧传输链路全部代码（V1/V2 端点、前端适配器、排空门禁、edge 上传 location） | 同上；**仅在生产排空证明通过后部署** |

部署顺序（§9，运维执行）：在线只读预审（`upload_drain.py audit`）→ 维护
窗口 → 停旧新建（部署 A + `freeze` + drain 重启）→ 等待/停止旧请求与
worker → 核账/迁移 → COS 真实桶与三 origin 验收 → 开放新上传并排空 →
`upload_drain.py report` = go → 部署 B。

## 2. U0–U6 执行索引

| 步 | commit | 摘要 |
|---|---|---|
| U0 基线冻结 | `826556d` | 入口/格式×身份×大小矩阵、容量缺口计算（默认池 9.5 GB < 产品 10 GiB）、锁图、语义契约（[u0-baseline](cos-upload-u0-baseline-20260928.md)） |
| U1 服务提取 | `f423a35` | `upload_content.py`（ZIP/转换/验证/格式分派，request 无关；monkeypatch 面保持）；冻结套件 179+77 过 |
| U2 形态扩展 | `0079c15` | 0075 kinds（native/zip/conversion）+ ingestion_job_items；两层大小门禁（413/503）+ 注册表派生词表；worker zip/conversion 责任链；`publish_batch_item` 注入缝；`zip_assemble_plan` 崩溃窗口修复 |
| U3 前端统一 | `be28f26` | uploadFile 恒 COS；删选路/开关/回退；排空过渡恢复旧 V2；processing/多结果展示；i18n 同步 |
| U4 检查点 A | `9d8d2b0` | 0076 冻结清单 + drain 门禁（V1 无任务号即 410；V2 清单资格）+ `upload_drain.py`（audit/freeze/report 七维核验 no-go 语义） |
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
- 排空副本演练：`tests/test_upload_drain.py`（freeze 幂等 / report
  no-go→go / 异常 staging）即可复跑演练；HTTP 门禁用例的历史版本在
  检查点 A 构建（`9d8d2b0` 的 `tests/test_upload_drain.py`）。

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
3. 停写副本演练已在测试层复跑；**生产**排空证明（audit/freeze/report 于
   生产副本）+ 生产独立门禁（R15 起约定）。
4. capability 仍为 `off`；本执行未触碰生产、未核账 apply、未扩池。

## 6. 保留与审计

- `upload_tasks`/`upload_reservations`/`upload_drain_freeze`/迁移回执保留为
  只读审计数据（不 DROP）；`upload_task_store` 的状态机与清理原语保留
  （admin residue 端点 + 核账工具消费）。
- `scripts/upload_drain.py` 保留（审计/冻结/排空核验）；
  `scripts/reconcile_upload_capacity.py` 不变（历史任务核账）。
- 客户端合同：[cos-upload-client-api-20260928.md](cos-upload-client-api-20260928.md)。
