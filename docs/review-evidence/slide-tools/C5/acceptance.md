# C5 producer 导入 + 百度导入插件 — 验收记录

日期：2026-09-29。验收方：主代理。合同：[c5-producer-import-contract.md](../../../slide-tools/c5-producer-import-contract.md)
（e77ad5e，八项裁决已冻结，本次不重开）。被验收物：
[c5-platform-report.md](../../../slide-tools/c5-platform-report.md)（平台 producer 导入 API + 百度驱动桥）、
[c5-baidu-plugin-report.md](../../../slide-tools/c5-baidu-plugin-report.md)（`plugins/pathtogether-baidu-import/`）。

结论：**C5 通过（本机可验部分）。** 子代理交付的矩阵 T1–T19 全部有实跑证据；验收中发现并修正
8 处缺陷（§1），其中 3 处会让百度插件路径在生产中卡死（批次永不收口、失败条目永远无法重试）。
每处修复都有回归用例；#1、#2、#4 的用例已确认在修复前失败（#1 集成重试报
`batch_running`，#2 去掉修复即失败，#4 修复前得 `failed`）。真实百度账号下载、生产 PG 迁移、插件安装批准
与来源策略 pin 是外部门禁 / 待他人动作（§4、§5），不宣称。

## 1. 验收中修正的缺陷

| # | 缺陷 | 修复 | 回归 |
|---|---|---|---|
| 1 | **插件路径批次永不收口**：桥只有 claim/heartbeat/report，平台从不对插件执行的批次调 `_finalize_batch`——条目全终态后批次停在 running，批次预算不 consume/release，租约过期后被反复领取，用户 `retry_items`（要求批次非 queued/running）永远被拒 | `plugin_report_item`：终态 report 后调 `plugin_finalize_if_settled`（同一 `_finalize_batch`：租约 fence + 批次终态与预算收口同事务；源副本清理归插件 §6.2，平台不调 adapter）；`plugin_claim_batch`：领到「条目已全终态」的批次（末条 report 后、收口前崩溃）即以新租约补做收口并继续找下一条 | `test_batch_finalized_after_last_report_and_on_reclaim`；集成 `test_worker_full_chain…` 追加批次 succeeded + 预算 consumed 断言 |
| 2 | **失败条目重试被 begin 拒绝**：producer 终态作废（`_void_staging_asset_locked`）把百度条目持久绑定的 staging 行置 failed；重试时 begin 复用该绑定 → `_require_baidu_item_slide_reusable` 400 | 条目绑定的 staging 行归条目所有：终态作废跳过（记 `void_skipped_baidu_bound` 事件），与原生 worker 失败不作废、重试复用同一 slide_id（P4-c §4.1）一致；非百度 producer 任务作废语义不变 | `test_retry_after_cancelled_attempt_reuses_bound_staging_slide`（去掉修复即 `'failed' == 'staging'` 失败） |
| 3 | **插件重试重放旧任务**：幂等键只由 (安装, 批次, 条目) 派生；retry 后同键 begin 重放已终态任务、拿不回 write_token → `write_token_lost` | 键加尝试序号后缀（`-r<N>`，N = 本地已收口的失败/取消记录数；首次尝试键不变）；崩溃续跑沿用先行记录里的键 | 插件 `test_retry_after_local_failure_starts_fresh_attempt`；集成 `test_worker_retry_after_failed_item_against_real_app`（真实 app：失败 → retry_items → 新任务发布到同一 slide_id、批次 failed→succeeded） |
| 4 | **本地已发布但回写丢失的条目重跑判失败**：journal `by_item` 只索引未终态记录，重跑找不到已发布记录 → 重新 begin → 同键重放 → `write_token_lost` | 重跑先查本地已收口记录：有已发布回执 → 只重放结果（必要时续做未确认清理）；否则先补完旧尝试未确认的取消/清理，再开新尝试 | `test_rerun_of_locally_finished_item_replays_result`；`test_rerun_after_crash_in_failure_cleanup_finishes_it_first` |
| 5 | **心跳瞬时错误被当成租约丢失**，且在途条目随即主动取消平台任务（应只在租约确被夺时安静放弃，且不碰平台任务） | 心跳区分 `LeaseLostError`（立即置 lease_lost）与其它错误（租期内重试）；租约丢失经独立 `should_stop` 在阶段/分块边界停下，不取消、不清理 | `test_driver_transient_heartbeat_error_does_not_cancel`；`test_driver_lease_lost_mid_item_stops_without_touching_import` |
| 6 | **批次取消时把已完成条目再报成 cancelled** | 取消补报循环跳过本轮已回写的条目 | `test_driver_cancel_mid_batch_keeps_finished_item_ready` |
| 7 | **平台缺口（插件报告 §5）**：桥条目视图缺 `fs_id`（无副本时无法转存）；heartbeat 不回 `cancel_requested`；恢复/清理/过期 sweep 无常驻调度 | `_plugin_item_view` 加 `fs_id`；heartbeat 返回 `{ok, cancel_requested}`；`app.py` 起 `producer-import-sweep` daemon（`PRODUCER_IMPORT_SWEEP_INTERVAL_SECONDS`，缺省 30，≤0 关闭；TESTING 下跳过；pytest 会话经 conftest 关闭） | `test_claim_exposes_fs_id_and_heartbeat_reports_cancel` |
| 8 | **镜像缺模块**：`Containerfile` 未 COPY 新模块 `producer_import_store.py`——部署镜像内 `import app` 即 ModuleNotFoundError（全量门禁 `test_containerfile_ships_app_modules` 捕获） | 加 `COPY producer_import_store.py ./` | `tests/test_stage2_ui.py::test_containerfile_ships_app_modules` |

## 2. 核对项

- `app.py` diff 逐 hunk 审阅：全部属 C5（import、write 数据面限流豁免、grant 用户面 CSRF 收回、
  JWT scope 按 approved_scopes 裁剪、安装批准 `approvePermissions`、sweep daemon、端点与桥）。
  共享模块（share_store*/share_shared/sdk manifest/schema/upload_guard/task_storage_lock/
  scripts/conftest/test_pg_infra）同样只含 C5 改动。
- 来源策略 pin 只覆盖 `manifest.json`；验收修改未触及 manifest，pin 不变：
  `7c6ea101584b09670b95c8096369df7021b34812fd5fbc888d15f25d5167fa20`。
- 私有样本：C5 全部用例用原生 CLI 合成 KFB/KFBF 与 fake 源；未使用真实百度账号、未触网。

## 3. 合同勘误（不改语义，只对齐名称）

- §6.3 写作 `batches/…/items/<id>/report`；as-built 为 `POST /api/plugin/v1/baidu/items/<id>/report`
  （claim/heartbeat 在 `/api/plugin/v1/baidu/batches/` 下）。插件按 as-built 实现。
- §6.3 未写批次收口：as-built 为「末条终态 report 触发平台 `_finalize_batch`（同 fence、同事务预算收口）
  + claim 时补做」，没有新增端点，冻结的名称不变。

## 4. 已知限制（不阻断 C5）

- 插件路径的源副本清理由插件逐条目执行，失败不回写平台：批次 `cleanup_state` 保持 `not_needed`，
  用户面「重试清理」不可用于插件批次（副本残留 = 用户网盘内 `/apps/bdpan/<batch>/`，不占平台容量）。
- KFBF 伴随 channel.json 平台无下发字段（插件已支持 `companion_fs_id/companion_name`）；过渡期伴随
  文件可作为独立候选入批。
- begin 请求已被平台受理、响应丢失（插件崩溃窗口）：同键重放拿不回 write_token，该任务的预约由
  72h 绝对期限 sweep 收口（合同 §3.1 口径）；插件以 `write_token_lost` 失败，用户可重试。
- `UPLOAD_MAX_INFLIGHT` 缺省 3（按预约行计）：每用户同时约 1 个带 scratch 的 producer 任务。
- 插件 retry 时批次预算已在首次收口释放/消费，重试不再补占批次预算（与原生 worker 重试同口径；
  producer 任务自身 final/scratch 预约照常准入）。

## 5. 外部门禁 / 待他人

1. `docs/plugin-capability-layer-design.md` §6.1 权限行（用户编辑中，本次不动），建议文本：
   `| producer 导入委托（机器生产者） | 用户导入委托 grant（pig_）+ approved_scopes 批准的 plugin JWT（slide:import）+ 任务级 write_token | grant.user 是目标项目 owner；owner 只来自 grant；slide:import 仅显式批准安装 | C5 |`
2. `plugins/source-policy.json`（他人在途）加入
   `"pathtogether-baidu-import": "7c6ea101584b09670b95c8096369df7021b34812fd5fbc888d15f25d5167fa20"`；
   安装时 admin 以 `approvePermissions: ["slide:import"]` 显式批准。
3. 生产迁移 0077 + 部署：需用户批准、晚间低峰执行。
4. 真实百度账号（bdpan CLI）转存/下载、真实 4GB 设备：外部门禁，替身成功不宣称真实可用。

## 6. 复跑结果（2026-09-29，验收修复后）

| 套件 | 结果 |
|---|---|
| 插件本地套件（stub 平台） | **83 passed**（原 77 + 验收新增 6） |
| C5 平台新增（5 文件） | **60 passed**（原 57 + 桥 3） |
| 插件 ↔ 真实 app 集成 | **3 passed**（原 2 + 重试链路 1） |
| baidu + producer 全部平台用例 | **165 passed, 4 skipped**（skip = 需真实百度环境） |
| 全量 pytest（串行，TMPDIR 指大盘；deselect 他人在途无关失败 1 条） | **2846 passed, 8 skipped**（首跑以 `-x` 在 Containerfile 用例停下 = §1 #8，修复后全量重跑；`results/pytest-full.txt`） |

复跑命令见 `platform/RERUN.md`、`plugin/RERUN.md`。

## 7. 补记：验收后发现的双重计费（2026-09-29，C6 演练中发现并修复）

§1 #1 的修复（插件路径批次收口调用 `_finalize_batch`）引入了**双重计费**：插件路径每个条目的产物
已由 producer 任务自身的 final 预约计费（`used_bytes += 产物字节`），批次收口又按 Σ ready 条目
`source_size` consume 批次预算——native TIFF 经插件导入即被计两次。§6 集成用例当时断言批次预约
`consumed`，把缺陷固化成了预期。

修复：批次预算结算额只计**未经 producer 导入发布**的 ready 条目（`_chargeable_ready_bytes`；已发布
producer 任务 = state published/done 且 `terminal_at` 为空）。进程内路径（native 产物不单独计费、转换
路径源字节只在批次收口计费）与「进程内已 ready、插件接管收口」的交接场景仍按批次 consume。
`_apply_cancel` 同口径，并修正「有 ready 但结算额为 0 时既不 consume 也不 release」的悬空分支。

回归：集成 `test_worker_full_chain_against_real_app` 改为断言批次预约 `released`、用户
`used_bytes == 产物 accounted_bytes`、`reserved_bytes == 0`（去掉修复即 `'consumed' == 'released'`
失败）；新增 `test_plugin_finalize_charges_only_items_not_published_by_producer`（交接条目仍按源字节
consume）。
