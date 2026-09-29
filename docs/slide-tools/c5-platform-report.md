# C5 平台侧实施报告（producer 导入 API）

- 日期：2026-09-29。分支 `slide-id-refactor`（基线 963ddf1 之上，工作区另有他人
  未提交改动——见 §7）。
- 合同：`docs/slide-tools/c5-producer-import-contract.md`（冻结稿 v1）。本报告
  记录平台侧（本子代理）交付物、歧义裁决与测试结果；插件包
  （`plugins/pathtogether-baidu-import/`）由并行子代理交付，不在此列。
- 复跑指南：`docs/review-evidence/slide-tools/C5/platform/RERUN.md`（结果 JSON/日志
  在同目录 `results/`）。

## 1. 交付物（files changed/added）

新增：

- `migrations/0077_producer_imports.sql` —— 三表 + 安装行列（§2）。
- `producer_import_store.py` —— 状态机/事件/授权 grant/容量编排/清理 duty/
  发布通道适配/平台自证 probe。
- `tests/_producer_import_helpers.py` —— 受控替身插件客户端（真实 token 端点）。
- `tests/test_producer_imports.py`（T2-T13/T15/T17-T19 + 补充）、
  `tests/test_producer_import_auth.py`（T8/T9/§2.1/§2.2/§2.6）、
  `tests/test_producer_import_baidu_bridge.py`（T16 + §6.3 begin 绑定）、
  `tests/test_producer_import_native_chain.py`（T1 native core 真实转换全链路）、
  `tests/test_producer_import_migration.py`（0077 幂等/约束）。

修改（平台代码）：

- `app.py` —— §3 端点 + 授权辅助 + 错误码 + write 数据面限流 + token scope 裁剪 +
  安装审批 + §2.2 用户面 grant 端点 + CSRF 豁免收回 + §6.3 驱动桥。
- `baidu_import_store.py` —— §6.3 插件桥原语包装（plugin_claim_batch /
  plugin_heartbeat_batch / plugin_report_item / plugin_get_item）。
- `upload_guard.py` —— HOLDER_KINDS += `producer_import`；
  HOLDER_PURPOSES[`producer_import`] = {scratch, final}。
- `task_storage_lock.py` —— KINDS += `producer_import`
  （`.task-locks/producer_import/<import_id>.lock`）。
- `scripts/reconcile_upload_capacity.py` —— 认得 producer_import 双预约持有者
  （collect/classify_producer/plan_actions/prestate/报告）。
- `scripts/upload_drain.py` —— audit 清单列出 producer 暂存树残留证据。
- `plugins/manifest.schema.json` + `plugins/sdk/manifest.py` —— permissions 枚举
  += `slide:import`（老插件零迁移）；新增
  `MANIFEST_APPROVAL_REQUIRED_PERMISSIONS`。
- `share_store.py` / `share_store_pg.py` / `share_shared.py` —— 安装行
  `approved_scopes`（创建/替换/导出归一）。
- `tests/conftest.py` —— TRUNCATE 清单 += 三张新表。
- `tests/test_pg_infra.py` —— 迁移文件清单 += 0077（该文件断言精确迁移列表，
  是迁移注册的既有惯例）。

## 2. 迁移 0077 摘要

- `producer_imports`：身份/授权（import_id `pim_`、installation/plugin/grant、
  owner_user_id=grant.user_id 冻结、UNIQUE(installation_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL、payload_sha256）、产物（slide_id 唯一绑定源、
  filename/format_ext/declared_size/confirmed_offset/received_bytes/sha256_actual/
  profile_json）、容量（final/scratch 两个 reservation_id + scratch_confirmed_bytes）、
  提交（commit_token/commit_intent_json/commit_started_at/write_token_hash）、双侧
  清理 duty 四列 ×2（local_/plugin_ cleanup_status/attempts/last_error/
  next_retry_at）、project_associate_state、state（CHECK 封闭 8 态）、fail_code、
  created/updated/terminal/deadline（begin+PRODUCER_IMPORT_MAX_AGE 72h）。
- `producer_import_events`：(id, import_id, kind, detail)；detail 落库前经
  `_sanitize_detail` 脱敏（禁 sign/secret/token/url/password 键——write_token/
  commit_token 明文绝不落库）。
- `plugin_import_grants`：grant_id `pig_` PK、installation/plugin/user/project、
  created/expires/revoked；UNIQUE 无（同用户可对同项目持多 grant）。
- `plugin_installations.approved_scopes TEXT[] NOT NULL DEFAULT '{}'`（存量行不
  自动获得 slide:import——防自动提权）。
- 幂等：CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DROP+ADD
  CONSTRAINT DO 块；重跑 no-op（`test_producer_import_migration.py` 实证）。
- 注册：`pg_store.ensure_schema` 按文件名序自动应用，无需 runner 改动；
  `tests/test_pg_infra.py::test_schema_migrations_recorded` 的精确清单 +1。

## 3. 端点清单（auth 列 = §2.3 校验链）

| 端点 | auth |
|---|---|
| `POST /api/plugin/v1/imports/begin` | JWT(`slide:import`, enabled) + grant 活跃/创建者/归属复核 + Idempotency-Key；返回 write_token（仅一次） |
| `POST /api/plugin/v1/imports/<id>/write` | JWT(`slide:import`) + write_token + grant 复核；write 专属限流桶（600/min，`PRODUCER_IMPORT_WRITE_RATE_LIMIT_PER_MIN`） |
| `POST /api/plugin/v1/imports/<id>/commit` | JWT + write_token + grant 复核（未受理段）；committing 重入走恢复路径 |
| `GET /api/plugin/v1/imports/<id>/status` | JWT（读回执；无需 grant/write_token） |
| `POST /api/plugin/v1/imports/<id>/cancel` | JWT + write_token（protective——不要求活跃 grant） |
| `POST /api/plugin/v1/imports/<id>/scratch` | JWT + write_token + grant 复核（新容量义务） |
| `POST /api/plugin/v1/imports/<id>/topup` | JWT + write_token + grant 复核（final 补占） |
| `POST /api/plugin/v1/imports/<id>/cleanup-confirm` | JWT（**不查 enabled**，只查安装行存在，§2.4）+ write_token；受管根非空性核验 |
| `POST/GET /api/plugin/import-grants`、`DELETE /api/plugin/import-grants/<id>` | Cookie session + CSRF（前缀豁免显式收回）+ 项目 owner only |
| `POST /api/plugin/v1/baidu/batches/claim` / `heartbeat` / `items/<id>/report` | JWT(`slide:import`, enabled) + 百度插件白名单 `_BAIDU_IMPORT_PLUGIN_IDS` |

控制面（begin/commit/status/cancel/scratch/topup/cleanup-confirm）沿用既有
`_PLUGIN_RATE_LIMITER` 120/min 桶（before_request）；write 从该桶排除、由
`_PRODUCER_IMPORT_WRITE_LIMITER`（600/min）在视图内限流。错误码 13 个新条目已进
`_PLUGIN_ERROR_RETRYABLE`（§1.8 全表；另补 `declared_checksum_mismatch`，见 §4-6）。

发布编排唯一：`slide_publish.publish_with_channel` + 新
`ProducerImportPublishChannel`（`producer_import_store.py`，逐钩子按 §3.3——
load_task/is_settled(state∈{published,done})/decode_intent/task_commit_token/
precheck_locked（行 FOR UPDATE + 代次 + owner 一致 + final 预约 renew 持有核验）/
settle（advisory → producer_imports 行 → slides CAS → record_revision → consume
final(expect_holder) → 行收口 published + 双侧 cleanup duty））。项目关联是结算后的
紧邻收敛步（§8 裁决 4 的「紧邻同锁窗口 + 幂等状态收敛」分支：pending →
succeeded/failed，失败不回滚产物）。

## 4. 合同歧义与我的读法（引用原文）

1. **§1.3 「追加写 `.staging/<import_id>/<commit_token>/data`」**——publish manifest
   的 entry 名与包内文件名必须一致（`slide_publish.build_manifest`/`verify_bundle`
   口径为 `data.<ext>`）。读法：`data` 是「数据件」的简写，实际文件名为
   `data.<format_ext>`（单一入口件）。实现按 `data.<format_ext>` 落盘
   （`producer_import_store.staging_data_path`）。
2. **§1.4 错误表未列 `declared_checksum_mismatch`，但 §1.4 正文与 §8 裁决 6 明确
   「422 declared_checksum_mismatch」**。读法：正文为准，新增该码进
   `_PLUGIN_ERROR_RETRYABLE`（HTTP 422、retryable=false）。
3. **§2.3「校验链…每个写/提交/清理操作都重跑」 vs §2.4 矩阵「cleanup-confirm
   不需要活跃 grant」「write_token 校验不查 enabled」**。读法：§2.4 是更具体的
   分层语义，按操作类别区分——begin/write/commit/scratch/topup（产生新数据或新
   容量义务）全链重跑；cancel（protective）与 cleanup-confirm（收尾）只要求
   token + 安装匹配，cleanup-confirm 额外不查 enabled。status 只读不查 grant。
4. **§2.1「scope = 既有 5 项 ∩ 安装行 approved_scopes」**——字面交集会把存量行
   （approved_scopes 缺省 `[]`）裁成空 scope，破坏老 token。读法（与同条「存量
   安装行无该字段 → 不发 slide:import（老 token 语义不变）」自洽）：基础 5 项恒
   在，approved_scopes 只**放开**扩展权限（当前仅 slide:import，不在基础 5 项
   内）——`_installation_jwt_scopes` = base ∪ approved∩扩展枚举。
5. **§6.3 begin 侧「item.slide_id 预分配绑定」的载荷形态未定**。读法：begin 体可
   选 `baidu_item_id` 字段（仅百度驱动白名单安装可携带，其它安装 400）；绑定语义
   镜像 `_allocate_item_slide`——无绑定时分配新 slide_id 并落库，已绑定且资产仍
   staging 时复用（重试不换资产），ready/failed 拒绝（不重复交付/不复活）。
6. **§1.2「scratch_bytes … 可为 0 表示先无本地副本」与 §1.7「cleanup-confirm …
   释放 scratch」**——scratch 预约为 0/无预约时 plugin_cleanup_status 置 `none`
   （无账面责任可确认，cleanup-confirm 幂等返回现状）；有预约时终态/发布后置
   `pending`，只经 cleanup-confirm 释放。done 的收口条件是双侧 cleaned/none。
7. **§6.3 白名单的 plugin_id 未冻结**。取 `dev.pathtogether.baidu-import`
   （`app._BAIDU_IMPORT_PLUGIN_IDS`）——插件侧 manifest id 必须使用该值才能过桥
   （已写入常量注释与测试；如插件子代理用了别的 id，改这一个常量即可）。
8. **§4.1「_PURPOSE/_HOLDER_ID_KEY 加映射」**——producer 同一 holder 双用途，单值
   映射不成立。读法：`_HOLDER_ID_KEY["producer_import"]="import_id"` 直接加；
   用途合法性改为按预约行逐份判定（`classify_producer` + `_KIND_PURPOSE_SETS`），
   单值 `_PURPOSE` 不动（bind/repair 后继检查不适用于该 kind——plan_actions 对
   producer 非 ok 一律阻断人工核对，镜像 baidu_batch 但不自动 stop/repair）。
9. **write 的磁盘水位检查时机**（§1.3 第 5 步「磁盘水位沿用
   check_disk_watermark」未说检查哪个目录）：按上传根所在卷判定（staging 目录可能
   尚未创建；同卷）。

## 5. 偏差

无。合同要求的行为全部按 §8 裁决实现；上述 9 条均为歧义的读法澄清，不改语义。

一个**观察**（非偏差，值得产品侧知悉）：producer 每任务最多占 2 份在途预约
（final+scratch），而 `UPLOAD_MAX_INFLIGHT` 默认 3（按预约行计数）——默认配置下
role=user 每人同时只够 1 个带 scratch 的 producer 导入任务（或 3 个无 scratch
任务）。这是共享护栏（upload_guard 唯一财务实现）的既有口径，未为本通道改护栏；
需要更高并发时调 env `UPLOAD_MAX_INFLIGHT`。

## 6. 测试矩阵覆盖（T1-T19 → 用例与结果）

新文件合计 **57 passed**（`results/pytest-c5-new.txt`；2026-09-29 实跑）：

| # | 用例（测试名） | 结果 |
|---|---|---|
| T1 | `test_producer_import_native_chain.py::test_t1_native_core_chain_visible_and_settled`（native CLI gen-kfb→convert 真实 BigTIFF；slide ready + authorize_read + 项目含 slide + final consumed 恰一次 + scratch released 恰一次 + 对账 0 差异） | PASS |
| T2 | `test_t2_begin_replay_same_payload_returns_original` / `test_t2_begin_drift_rejected_409` | PASS |
| T3 | `test_t3_commit_and_status_same_receipt_single_billing`（重复 commit/status 同回执；slides 行唯一；consume 单次） | PASS |
| T4 | `test_t4_cancel_and_cleanup_confirm_idempotent_scratch_released_once`（终态作废 failed 不可读；scratch released 恰一次；→done） | PASS |
| T5 | `test_t5_offset_conflict_and_resume_no_holes`（409+expected_offset；落后/超前；续传字节逐一致） | PASS |
| T6 | `test_t6_chunk_checksum_mismatch_offset_unchanged` / `test_t6_declared_checksum_mismatch_keeps_writing`（422、无 intent、保持 writing、纠正后可 commit） | PASS |
| T7 | `test_t7_chunk_over_limit_and_size_gate_then_topup`（>64MiB 块/declared 越界 413；topup 后续传并按新 declared 结算；>10GiB begin 413） | PASS |
| T8 | `test_t8_forged_owner_ignored_asset_owner_is_grant_user` / `test_t8_foreign_project_rejected` / `test_t8_wrong_installation_grant_rejected` / `test_t8_unknown_grant_and_revoked_reasons` / `test_no_endpoint_accepts_paths` | PASS |
| T9 | `test_t9_grant_revoked_matrix` / `test_t9_grant_expired_time_travel` / `test_t9_plugin_disabled_matrix` / `test_t9_user_disabled_matrix` / `test_t9_project_archived_rejects_new_ops`（逐格：新操作拒绝码；intent 后恢复收口；cleanup-confirm 仍可用） | PASS |
| T10 | `test_t10_intent_first_cancel_rejected`（intent 先 → commit_in_progress）/ `test_t10_cancel_first_commit_rejected`（cancel 先 → import_state_invalid；无既发布又取消态） | PASS |
| T11 | `test_t11_crash_after_intent_recovery_republishes` / `test_t11_crash_after_fs_publish_before_settle`（FS 发布后 DB 前：authorize_read 拒绝——DB ready 唯一可见开关；恢复 no-clobber+verify_bundle 收口、恰一次结算） | PASS |
| T12 | `test_t12_plugin_restart_resume`（status+write_token 续传；无重复预约/重复块） | PASS |
| T13 | `test_t13_local_cleanup_failure_backoff_keeps_published`（首删失败退避、重试 cleaned、used 不双减）/ `test_t13_managed_root_nonempty_scratch_not_released`（409+residual_bytes、scratch 不释放、published 保留、对账可见） | PASS |
| T14 | `test_t14_disabled_plugin_import_stays_in_reconcile`（任务进对账清单不消失、容量保留）+ 核心原生 COS/查看/删除回归批次（§7 批次 1/4） | PASS |
| T15 | `test_t15_staging_and_failed_invisible_cross_installation_404`（staging/failed 对任何主体不可读；他人 status 404） | PASS |
| T16 | `test_t16_plugin_and_inprocess_cannot_both_hold`（进程内/插件 SKIP LOCKED 互斥；租约被夺后 heartbeat/report 被 fence）+ `test_bridge_claim_heartbeat_report_roundtrip` / `test_bridge_requires_baidu_plugin_allowlist` / `test_begin_binds_baidu_item_slide_id` / `test_begin_baidu_item_of_other_owner_rejected` | PASS |
| T17 | `test_t17_quota_rejected_no_side_effects`（413 零外部副作用）/ `test_t17_disk_watermark_507` / `test_t17_bound_reservations_not_ttl_recycled_time_travel`（租约拨过去不回收；deadline sweep 终态化且容量保留） | PASS |
| T18 | `test_t18_write_rate_limit_429`（429+Retry-After）/ `test_t18_control_plane_uses_existing_bucket`（控制面沿用既有桶；write 不进该桶） | PASS |
| T19 | `test_t19_reconcile_knows_producer_holder`（在途不阻断、quota 0 差异、预约异常阻断）/ `test_t19_drain_audit_lists_producer_residue` + CLI 证据 `results/tools-cli-demo.txt` | PASS |

平台侧的 T1「下载」步是本地替身（真实百度下载为外部门禁，替身成功不宣称真实
账号可用——§9.1）；转换步是 native CLI 真实产物。

## 7. 回归（改动前基线 = 963ddf1 的既有套件在 HEAD 上历来全绿；本次全部实跑）

| 批次（命令见 RERUN.md） | 结果 |
|---|---|
| C5 新增（5 个文件） | **57 passed** |
| guard/锁/publish/ingestion/COS（16 文件） | **153 passed, 1 skipped** |
| baidu（7 文件） | **102 passed, 4 skipped**（末尾有一段 teardown 期 OperationalError 噪音——会话结束后残留线程重连被停库拒绝，exit 0；单文件与组合均全绿，与本次改动无关） |
| plugin/usage（9 文件） | **170 passed** |
| 核账/排空/迁移/slide 核心（8 文件） | **108 passed** |
| CSRF urlmap/访问控制/工具页/账号/dispatch/admin 插件（8 文件） | **287 passed** |
| slide-transform-core（CLI oracle） | **16 passed, 1 skipped**（一次并行负载下的瞬时失败复跑消失） |
| tests/test_pg_infra.py（迁移清单） | **14 passed**（含 0077 清单更新） |

已知无关失败（未入任何批次，他人未提交改动）：
`tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present`。

## 8. 未做 / 待他人

> **验收（2026-09-29）**：下列「常驻调度接线」已在验收中完成（`app.py`
> `_start_producer_import_sweep_thread`，`PRODUCER_IMPORT_SWEEP_INTERVAL_SECONDS` 缺省 30）。验收另修：
> 插件路径批次收口（末条终态 report / 下次 claim 触发 `_finalize_batch`）、百度条目绑定的 staging 行
> 不随 producer 终态作废（重试复用）、桥视图 `fs_id`、heartbeat `cancel_requested`。C5 新增用例 57 → 60。
> 见 `docs/review-evidence/slide-tools/C5/acceptance.md`。

- `docs/plugin-capability-layer-design.md` §6.1 的「producer 导入委托」权限行——
  该文件属用户编辑中，**本次不动**。建议行文本（列结构与该表一致）：

  | 消费主体 | 凭证 | 权限判定 | 阶段 |
  |---|---|---|---|
  | producer 导入委托（机器生产者） | 用户导入委托 grant（`pig_`）+ 安装行 approved_scopes 批准的 plugin JWT（`slide:import`）+ 任务级 write_token | grant.user 是目标项目 owner；owner 只来自 grant（body 一律忽略）；`slide:import` 只在显式批准的安装行发放，存量安装不自动获得 | C5 |

- 插件 UI 面板（grant 的一键授权/粘贴引导）归插件子代理；平台只提供
  `POST/GET/DELETE /api/plugin/import-grants`。
- 恢复/清理/过期的常驻调度接线（`producer_import_store.sweep_producer_imports`
  已就绪、测试驱动验证）：C6 把它接进常驻 worker 进程（与
  `cos_ingest_worker` 的 `retry_local_cleanups`/`sweep_expired_jobs` 同位）。
  合同未要求 C5 内建调度循环。
- C6 检查点 A 的存量百度批次移交（§6.4）：按裁决 8 维持 `baidu_batch` holder
  原样，不迁移为 producer_import——本次零改动。
