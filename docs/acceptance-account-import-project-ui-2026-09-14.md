# 验收报告：账户状态、导入与项目 UI 升级（2026-09-14）

> 对应规格：[agent-implementation-account-import-project-ui-2026-09-14.md](agent-implementation-account-import-project-ui-2026-09-14.md)（W1–W6 / §8 测试矩阵 / §10 交付清单）。
> 状态：**进行中（草稿）**。本报告由验证/测试轨道在 2026-09-14 填写；未运行的项目如实标注，不虚构 PASS。统一验收入口：`scripts/verify_import_project_upgrade.sh`。

## 1. 基线

- 开始时 HEAD：`998e638`（`test: 插件 bundle fileHashes 防漂移门禁（review 2026-09-14）`）
- 分支：`wip/ser8-dev...origin/wip/ser8-dev [领先 2]`
- 已有工作区修改（开始时，摘要）：`app.py`、`conversion_store.py`、`conversion_worker.py`、`slide_format_registry.py`、`static/app.js`、`static/i18n.js`、`templates/_app_shell.html`、`templates/admin_host.html`、`templates/entry.html`、`tests/conftest.py` 等（M），以及未跟踪的 `baidu_adapter.py`、`baidu_import_http.py`、`baidu_import_store.py`、`baidu_share_parser.py`、`conversion_http.py`、`format_request_http.py`、`format_request_store.py`、`project_create_http.py`、`project_idempotency_store.py`、`migrations/0049–0051`、新测试文件等。
- 说明：实施期间多轨道并行修改（app.py 接线、static/app.js UI、store 层），本报告记录的是**报告填写时刻**的状态；`git status` 快照以统一验收脚本产物 `artifacts/import-project-upgrade-*/env.txt` 为准。
- 实现完成时 HEAD/工作区状态：首跑统一验收时 HEAD 仍为 `998e6382b8b2`（Python 3.14.4 / Node v22.23.2，见 `artifacts/import-project-upgrade-20260914T161016Z/env.txt`）；工作区含大量并行轨道未提交修改（尚未到「实现完成」终态，后续重跑时更新）。

## 2. 工作包

| 工作包 | 实际文件 / 功能 | 状态 |
|---|---|---|
| W1 账户激活状态与额度展示 | 已随提交 `e281be5`（站点统计 + 待激活用户额度展示语义）与 `998e638` 落库；管理列表 `spend.status`（not_provisioned/available/unavailable）见 `tests/test_admin_batch_d.py::test_users_list_pending_activation_not_provisioned` 等用例 | 已提交（e281be5 / 998e638） |
| W2 格式申请 PG 持久化 | `format_request_store.py`、`format_request_http.py`、`scripts/format_request_worker.py`、`scripts/migrate_format_requests.py`、`migrations/0049_format_requests.sql`；`tests/test_format_request_concurrency.py`、`tests/test_format_request_migration.py` | 后端已就绪（app.py 路由接线待协调者） |
| W3 项目创建对话框 + 幂等 | `project_idempotency_store.py`、`project_create_http.py`、`migrations/0050_project_create_idempotency.sql`；前端对话框 `#project-create-dialog`（`templates/_app_shell.html` + `static/app.js` `openProjectDialog/submitProjectDialog`：R3 empty/selection 分离、R4 提交锁 + Idempotency-Key） | 后端已就绪；前端迁移中（bindEvents 旧 np-* 引用待切换） |
| W4 统一导入与后台任务 | `conversion_store.py`、`conversion_http.py`、`conversion_worker.py`、`slide_format_registry.py`（public_catalog）、`migrations/0046–0048_conversion_jobs*.sql`；导入抽屉 `#import-drawer`（本地/百度页签 + 目标位置 + 格式目录 + 任务列表） | 后端已就绪（`GET /api/slide-formats`、`GET /api/conversions` 路由接线待协调者）；前端迁移中 |
| W5 百度适配器/枚举/导入 | `baidu_adapter.py`、`baidu_share_parser.py`、`baidu_import_store.py`、`baidu_import_http.py`、`scripts/baidu_import_worker.py`、`migrations/0051_baidu_remote_imports.sql`；`tests/test_baidu_adapter.py`、`tests/test_baidu_imports.py`、`tests/test_baidu_import_recovery.py` | 后端已就绪（`/api/remote-imports/baidu/*` 路由接线待协调者） |
| W6 界面验收与集成 | `tests/e2e/import-project-upgrade.spec.ts`（本轮新增，见 §3）；`tests/js/project-import-upgrade.test.ts`（本轮新增） | 进行中（UI 接线未完成前 fail-closed） |

未完成/待接线清单（诚实记录，非「后续优化」）：

1. `app.py` 尚未接线：`GET /api/slide-formats`、`GET/POST /api/format-requests`（列表/详情新契约）、`GET /api/conversions`（列表）、`POST /api/project/create` 的 `Idempotency-Key` 分支、`/api/remote-imports/baidu/*` 全部路由。HTTP 处理器模块（`*_http.py`）已就绪，接线由协调者执行。
2. `static/app.js` 迁移中：`bindEvents` 仍引用已删除的 `els.npName/npConfirm/...`（新对话框为 `pcd-*`），当前浏览器加载会抛 TypeError——`tests/js/project-import-upgrade.test.ts` 已锁定该缺口（fail-closed）。

## 3. 测试映射（§8 矩阵 ID → 文件::测试 → 状态 → 证据）

状态说明：PASS = 本轮实际运行通过；NOT RUN = 用例存在但本轮未运行（待统一验收脚本）；待补 = 尚无专用用例映射。证据路径：`artifacts/import-project-upgrade-<时间戳>/`（统一验收脚本产物）。

| ID | 文件::测试 | 状态 | 证据 |
|---|---|---|---|
| A01 | tests/test_admin_batch_d.py::test_users_list_pending_activation_not_provisioned；tests/test_email_verify_activation.py::test_verify_creates_pending_user_email_as_login_id、test_pending_account_blocked_from_business_api | PASS（pytest 全量 20260914T161016Z） | artifacts/import-project-upgrade-20260914T161016Z/backend.xml |
| A02 | tests/test_admin_batch_d.py::test_users_list_spend_total_mode_missing_row_reports_stable_error、test_users_list_spend_display_single_track_locked；tests/test_account_balance.py::test_balance_user_total_allowance、test_balance_owner_month_window_peek_only | PASS（同上） | 同上 |
| A03 | tests/test_admin_batch_d.py::test_invite_redeem_creates_total_allowance_same_transaction；tests/test_email_verify_activation.py::test_already_active_second_code_not_consumed、test_same_invite_two_pending_users_single_winner | PASS（同上） | 同上 |
| A04 | tests/test_account_balance.py::test_balance_user_missing_allowance_is_stable_error、test_balance_requires_login；tests/test_admin_batch_d.py::test_users_list_pending_activation_not_provisioned（矛盾数据分支） | PASS（同上） | 同上 |
| F01 | tests/test_format_request_concurrency.py::test_f01_two_processes_submit_20_unique | PASS（同上） | 同上 |
| F02 | tests/test_format_request_concurrency.py::test_f02_same_user_limit_race | PASS（同上） | 同上 |
| F03 | tests/test_format_request_concurrency.py::test_f03_two_workers_single_claim | PASS（同上） | 同上 |
| F04 | tests/test_format_request_concurrency.py::test_f04_crash_before_and_after_send | PASS（同上） | 同上 |
| F05 | tests/test_format_request_concurrency.py::test_f05_submit_during_drain_no_sender | PASS（同上） | 同上 |
| F06 | tests/test_format_request_migration.py::test_f06_preflight_no_writes、test_f06_apply_idempotent_preserves_status_no_enqueue、test_f06_bad_line_or_conflict_rejects_whole_file | PASS（同上） | 同上 |
| F07 | tests/test_format_request.py::test_f07_oversize_and_txn_failure_leave_no_orphan、test_f07_owner_scoped_get_hides_other_user、test_f07_cas_mismatch_version_conflict_and_view_hygiene | PASS（同上） | 同上 |
| P01 | tests/test_project_creation_upgrade.py::test_p01_same_user_same_key_concurrent_one_project；e2e U03-API（tests/e2e/import-project-upgrade.spec.ts） | PASS（pytest）；e2e FAIL（app.py 未接线，符合预期 fail-closed） | backend.xml；playwright-report/ |
| P02 | tests/test_project_creation_upgrade.py::test_p02_invalid_input_and_same_name_different_keys；e2e P02-API | PASS（pytest）；e2e FAIL（同上） | 同上 |
| C01 | tests/test_conversion_task_api.py::test_list_jobs_owner_isolation、test_list_jobs_pagination_cursor_no_dup_no_missing、test_retry_failed_requeues_same_job 等 | PASS（同上） | backend.xml |
| C02 | 本地导入目标关联（native/转换后各关联一次；目标删除/权限撤销） | **待补**（未见专用用例） | — |
| B01 | tests/test_baidu_adapter.py::test_b01_*（8 项：解析/冲突/恶意参数/argv） | PASS（同上） | backend.xml |
| B02 | tests/test_baidu_adapter.py::test_b02_*（fixture 契约/超时/非零退出/未知 JSON/越界路径） | PASS（同上） | 同上 |
| B03 | tests/test_baidu_imports.py::test_b03_nested_dir_enumeration_zero_side_effects、test_b03_duplicate_cursor_fails_explicitly、test_b03_over_max_entries_incomplete_import_rejected | PASS（同上） | 同上 |
| B04 | tests/test_baidu_imports.py::test_b04_mixed_format_selectability、test_b04_empty_selection_400、test_b04_expired_enumeration_409、test_b04_other_owner_404、test_b04_extraction_code_roundtrip | PASS（同上） | 同上 |
| B05 | tests/test_baidu_imports.py::test_b05_same_key_same_digest_same_batch、test_b05_same_key_different_selection_409、test_b05_concurrent_same_key_single_batch、test_b05_quota_insufficient_* | PASS（同上） | 同上 |
| B06 | PG+真实收口+fake 传输：native/KFB/KFBF 全链路 | **待补**（本地转换收口由 tests/test_kfb_upload.py、tests/test_kfbf_upload.py 覆盖且全量通过；与 fake 传输+PG 组合的专用用例未见） | — |
| B07 | tests/test_baidu_import_recovery.py::test_b07_crash_after_transfer_no_double_transfer、test_b07_crash_after_transfer_reconcile_via_copies、test_b07_crash_after_download_no_double_download、test_b07_crash_after_ingest_no_duplicate_ingest | PASS（同上） | backend.xml |
| B08 | 断流/来源变化/名称冲突 | **待补**（未见专用用例） | — |
| B09 | tests/test_baidu_import_recovery.py::test_b09_partial_fail_retry_only_failed、test_b09_cancel_releases_unconsumed_reservation、test_b09_cancel_after_ready_keeps_products、test_b09_cancel_terminal_batch_noop | PASS（同上） | 同上 |
| B10 | tests/test_baidu_import_recovery.py::test_b10_cleanup_only_batch_paths、test_b10_cleanup_foreign_path_rejected、test_b10_cleanup_failure_keeps_ready、test_b10_flags_disabled_503_existing_rows_visible | PASS（同上） | 同上 |
| B11 | tests/test_baidu_imports.py::test_b11_no_secrets_in_public_views、test_b11_owner_isolation_lists、test_b11_pagination_cursor | PASS（同上） | 同上 |
| U01 | 管理端桌面/手机 pending 标签可见（扩充 tests/e2e/admin-workbench.spec.ts） | **待补**（本轮 admin spec 因并行迁移中的模板改动失败/中断，pending 专用断言待扩） | — |
| U02 | tests/e2e/import-project-upgrade.spec.ts：`U02-API`（slides=[] 保留）+ `U02-UI`（对话框空项目） | FAIL（U02-API：POST /api/project/create 幂等分支未接线/GET /api/projects 断言失败；U02-UI：app.js bindEvents 迁移中断）——预期 fail-closed | artifacts/import-project-upgrade-20260914T161016Z/playwright.log |
| U03 | 同上：`U03/P01-API`（幂等键）+ `U03-UI`（慢响应双击单 POST，route.fetch 透传真实 Flask） | FAIL（同上，404/选择器缺失） | 同上 |
| U04 | 选择列表/关闭/权限错误 | **待补**（本轮 spec 未覆盖，见 §8） | — |
| U05 | 同上：`U05-API`（/api/slide-formats 目录 + 申请回执/列表/详情/越权 404）+ `U05-UI`（label 持久可见） | FAIL（GET /api/slide-formats 未接线 → 404；UI 迁移中） | 同上 |
| U06 | 转换观察 16 分钟/断网/刷新（fake timer） | **待补** | — |
| U07 | 同上：`U07-API`（GET /api/conversions 隔离/分页/非法 group 400）；401/403/404 分支 | FAIL（GET /api/conversions 未接线 → 404） | 同上 |
| U08 | 同上：`U08-API`（capabilities 默认关 + reason + 无秘密）+ `U08-UI`（百度页签原因/禁用） | FAIL（/api/remote-imports/baidu/capabilities 未接线 → 404；UI 迁移中） | 同上 |
| U09 | 百度全流程（粘贴→列表→跨页选中→确认→worker→ready） | **待补**（需 e2e_server 受控 fake adapter 注入） | — |
| U10 | 百度部分失败/超限/过期/重试/取消 | **待补** | — |
| U11 | 双尺寸×中英文无溢出/焦点 | **待补** | — |
| L01 | tests/test_baidu_live.py（RUN_BAIDU_LIVE_TESTS=1 时执行） | NOT RUN — 缺少专用测试分享与连接器 | — |
| L02 | 同上 | NOT RUN — 同上 | — |
| L03 | 同上 | NOT RUN — 同上 | — |
| L04 | 同上 | NOT RUN — 同上 | — |

## 4. 命令结果

统一入口（产物：`artifacts/import-project-upgrade-<UTC 时间戳>/`，含 `backend.xml` JUnit、`vitest.log`、`playwright.log`、`playwright-report/` HTML+trace、`env.txt`、`summary.txt`）：

```bash
scripts/verify_import_project_upgrade.sh
```

本轮已实际运行的命令（测试轨道，2026-09-14/15）：

| 命令 | 退出码 | 结果 | 运行时间 |
|---|---|---|---|
| `scripts/verify_import_project_upgrade.sh`（全量首跑，产物 `artifacts/import-project-upgrade-20260914T161016Z/`） | 1（fail-closed，符合当前阶段预期） | pytest **exit=0，294 passed / 0 failed / 0 skipped**（17 文件全量，junit 294 项）；`npm run test:js` exit=1（379 例中 53 failed——主因 static/app.js 迁移中断，波及 toolbar-account-upgrade/sidebar-layout/slide-opened-* 等 7 文件；本轨道新增 tests/js/project-import-upgrade.test.ts 为 1 passed / 2 failed，失败即该迁移缺口）；playwright exit=1（18 passed / 25 failed / 27 did not run；chromium 缺失时自动 `npx playwright install chromium` 后重试的路径已被本次实际执行验证）；hygiene exit=1（`git diff --check` 报 `tests/js/admin-plugin-ui.test.ts:2567: new blank line at EOF`，属并行轨道的已跟踪文件） | 全程 ~3.5 分钟（pytest 33.8s，playwright 1.5m） |
| `.venv/bin/python -m pytest tests/test_slide_format_registry.py tests/test_project_creation_upgrade.py -q` | 0 | 29 passed | ~1s |
| `.venv/bin/python -m pytest tests/test_conversion_task_api.py tests/test_baidu_adapter.py -q` | 0 | 40 passed | ~1.3s |
| `npx playwright test tests/e2e/import-project-upgrade.spec.ts --list` | 0 | 12 tests 收集成功 | <1s |

勘误与修正记录：首跑 hygiene 阶段对未跟踪文件报出的 24 处「trailing whitespace」为脚本缺陷（grep -E 括号表达式 `[ \t]` 被按字面 {空格, 反斜杠, 字母 t} 解析，误报所有以 t/续行反斜杠结尾的行）；已修正为 POSIX 类 `[[:blank:]]+$` 并复扫：**未跟踪源码文件 0 处真实行尾空白、0 冲突标记**。当前 hygiene 阶段唯一真实问题是上表的 `git diff --check` EOF 空行（其他轨道文件）。

## 5. 截图

- 桌面 1440×900 / 手机 390×844、中英文、成功/空/错误/处理中/部分失败：**TODO（待 e2e 全量可运行后由 Playwright `testInfo.attach` 与 `playwright-report/` 补齐；`artifacts/import-project-upgrade-*/playwright-report/` 为证据路径）**。

## 6. 迁移演练（JSONL → PG）

| 步骤 | 结果 |
|---|---|
| `scripts/migrate_format_requests.py --mode preflight` | 逻辑由 F06 用例覆盖（test_f06_preflight_no_writes，PASS，见 §3）；对真实历史 JSONL 的实际演练 **pending**（待维护窗口执行并回填） |
| `--mode apply`（首次） | 同上：用例 PASS（test_f06_apply_idempotent_preserves_status_no_enqueue）；实际演练 pending |
| 重复 apply（幂等） | 同上（用例内两次 apply 断言无新增/无状态回退）；实际演练 pending |
| 坏行/同 ID 冲突整体拒绝、缺样本显式标识 | 用例 PASS（test_f06_bad_line_or_conflict_rejects_whole_file）；实际演练 pending |

## 7. 真实连接器（百度）

- 环境：**未配置**（无 `BAIDU_CONNECTOR_BIN` / 认证目录 / 专用测试分享）。
- 能力核验：NOT RUN（无本机连接器可核验 version/help/JSON 输出）。
- L01–L04：`NOT RUN — 缺少具体条件`（tests/test_baidu_live.py 默认整模块 skip，需 `RUN_BAIDU_LIVE_TESTS=1` + 专用凭据）。
- 未操作任何历史文档中出现过的真实分享；未发送真实邮件。

## 8. 遗留问题

1. **app.py 路由接线未完成**（影响：U02/U03/U05/U07/U08 的 e2e API 层与前端抽屉数据全部 404）。缺少条件：协调者将 `project_create_http` / `format_request_http` / `conversion_http` / `baidu_import_http` / `slide_format_registry.public_catalog` 接入 Flask 路由（含 CSRF/身份守卫）。
2. **static/app.js bindEvents 迁移中断**（影响：/app 页面加载即抛 `TypeError: Cannot read properties of undefined (reading 'addEventListener')`，els.npName 等已删条目仍被引用；统一验收中 vitest 共 53 例失败，除本轨道 2 例外其余 7 个既有 spec/单测文件同因中断）。缺少条件：UI 轨道完成 pcd-* 新绑定与导入抽屉事件（`#import-slides-btn` 开合、页签切换、baidu 能力探测渲染、`#baidu-list-btn` 禁用态）。
3. **测试映射缺口**：C02（本地导入目标关联）、B06（fake 传输+真实探测/转换组合）、B08（断流/来源漂移/名称冲突）、U01（admin pending 可见性扩充）、U04/U06/U09/U10/U11（e2e 交互层）尚无专用用例。影响：§8「代码验收通过定义」尚不满足；不得宣称完成。
4. **干净 diff 未达门槛**：`git diff --check` 报 `tests/js/admin-plugin-ui.test.ts:2567: new blank line at EOF`（并行轨道的已跟踪文件，需其作者修复后统一验收才能整体转绿）。修正后的未跟踪文件空白/冲突标记扫描为 0 问题。
5. **真实百度链路**：L01–L04 NOT RUN（见 §7）；真实导入开关保持关闭。

## 9. 最终结论

- 代码验收：**未完成（进行中）**——后端全量回归绿（统一验收脚本首跑：17 文件 294 passed / 0 failed / 0 skipped，A/F/P/C01/B 系存储层与并发/恢复用例全部 PASS，证据 `artifacts/import-project-upgrade-20260914T161016Z/backend.xml`）；但 app.py 路由接线、static/app.js 迁移未完成（vitest 53 failed / e2e 25 failed，均集中于此），且 C02/B06/B08/U01/U04/U06/U09–U11 用例映射缺口待补，`scripts/verify_import_project_upgrade.sh` 整体 exit=1（fail-closed，符合当前阶段）。接线与映射补齐后重跑统一脚本并回填本报告。
- 真实百度验收：**NOT RUN**（缺少专用测试分享与连接器环境）。
- 默认开关：`BAIDU_ENUMERATION_ENABLED=false`、`BAIDU_IMPORT_ENABLED=false`（fail-closed；关闭态仍可查询既有任务与原因，见 B10 用例）。
