# slide ID 化重构：验收矩阵执行记录

日期：2026-09-26。依据[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §8 的 17 行矩阵逐行映射到持久回归用例与实际门禁证据。执行分支：PathTogether `slide-id-refactor`（HEAD=4891744）、HistoPilot `slide-id-refactor`（HEAD=4598172）。

**执行口径**：全部 SQL 并发/发布/删除/配额断言跑在真实临时 PG（pgserver）与真实目录上（非 Fake）；故障注入为阶段屏障（monkeypatch 钩子/控制动作崩溃点/journal 崩溃注入），不依赖 sleep。排除项：完整 `tests/e2e`（Playwright）与 `tests/test_e2e_pg_reap.py`（环境依赖，conftest `_ci_skip` 标记）——原因：本机无浏览器 e2e 常驻环境与 reap 场景专属环境，浏览器侧验收以 P2 隔离实例实测（交付日志 P2 节）与 JS 套件承担。

## 1. 矩阵逐行映射

| §8 场景 | 覆盖用例（持久回归） | 结果 |
|---|---|---|
| 甲乙均上传 a.svs；同账号再传同名同字节 | `test_slide_publish_pg.py`（同名并发各得各 ID）、`test_zip_slide_id_pg.py::test_multi_items_each_get_own_slide_id`、`test_conversion_slide_id_pg.py::test_same_name_source_and_product_independent_ids`、浏览器实测（P2 日志：同名并存 data-slide-id 各异） | ✅ 不同 slide_id/objects 目录，各自归属，无名称占用错误 |
| 同一任务重复创建/commit/恢复，响应丢失后重试 | `test_upload_v2.py`、`test_upload_accounting_recovery.py`、`test_zip_slide_id_pg.py::test_crash_after_intent_recovery_reuses_slide_ids`、`test_conversion_slide_id_pg.py::test_failure_retry_same_id_and_single_settlement` | ✅ 同一 slide_id、单次结算、无重复资产 |
| 删除 A 后上传同名 B | `test_slide_publish_pg.py::test_delete_then_reupload_new_id_no_inheritance`、`test_owner_workspace_upgrade.py::test_slide_delete_clears_view_grants_no_orphans`（P4 C5 改写）、HP 契约②（删除隔离）、`test_slide_delete_pg.py::test_delete_a_during_same_name_reupload_b_unaffected` | ✅ B 新 ID；旧分享/授权/标注/AI/Demo 不继承 |
| 显示名修改、Unicode 原名、目录/控制字符输入 | `test_slide_store_pg.py`（元数据编辑不动文件/ID）、`_sanitize_name`/`check_relpath`  containment 族（`test_slide_storage.py`）、PATCH by ID 用例 | ✅ 改名不移文件；非法输入 400；ID/路径不受影响 |
| 旧 metadata 属甲、文件缺失，乙新上传同名 | `test_zip_slide_id_pg.py`/同名各得各 ID；backfill 的 manual_review 通道（`test_backfill_slide_asset_state.py`）；`force_slide_owner_follow_file` 定义/导出拆除（rg 零残留） | ✅ 乙新 ID；甲旧记录不转移 owner/授权/别名/note |
| 同 SHA 的他人文件、平台 owner 文件 | `test_cos_slide_id_pg.py`（adopted_existing/still_ours/promoted_ident 族拆除后语义）、conversion 幂等键=(owner,sha,converter) 同主复用（`test_conversion_task_api.py`） | ✅ 不认领不删除不重新归属；哈希仅完整性证据 |
| stat 后替换/旧 worker 复活/同任务两 worker | 发布 fencing 族：`test_slide_publish_pg.py`（代次/token 拒绝）、conversion/baidu/COS worker 租约 fencing（`test_conversion_slide_id_pg.py`、`test_baidu_slide_id_pg.py`、`test_cos_slide_id_pg.py`） | ✅ 旧 generation 不发布不结算；无按名删除/转 owner |
| intent 前后、包发布前后、DB commit 前后崩溃 | `test_slide_publish_pg.py`（19 用例全崩溃矩阵）、`test_upload_accounting_recovery.py`、`test_zip_slide_id_pg.py::test_crash_after_intent_recovery_reuses_slide_ids`、P6-1 演练三处崩溃注入（drill §2-3） | ✅ 未 ready 不可访问；恢复收口一次；字节有预约或实占 |
| 数据库结算失败、预约过期、磁盘满、清理失败 | `test_slide_publish_pg.py::test_reservation_expired_fail_closed_no_double_charge`、`test_conversion_slide_id_pg.py` 三例（P4 review F1/F2 回归）、`test_zip_slide_id_pg.py::test_reservation_expired_mid_publish_withdraws_published`（F0/F3）、`test_slide_delete_pg.py::test_delete_cleanup_failure_retry_settles_once`、水位 507 族 | ✅ 不可读、不漏账、不重复收费；可恢复/安全清理 |
| 取消和发布、删除和发布、删除和新上传并发 | `test_slide_publish_pg.py`（committing 取消拒绝/胜者规则）、`test_slide_delete_pg.py`（daemon 单轮/双实例 SKIP LOCKED） | ✅ 明确胜者；新 ID 资产不受旧任务清理影响 |
| 所有读取通道 | `test_slide_read_gate_pg.py`（10 用例全通道族）、`test_p6_legacy_runtime_retirement.py`（9 用例：legacy 全通道拒+不泄露存在性）、share_server `_require_slide` 族 | ✅ 列表/info/DZI/tile/thumbnail/crop/download/share/grant/Demo/plugin/internal AI 均拒未发布/删除/legacy 资产 |
| 旧分享保留 token | `test_slide_read_gate_pg.py`、分享成员 tombstone 用例、P6-1 演练 §2-5（旧领取人不继承同名新资产）、HP 契约（分享 chips 按 ID） | ✅ 仅访问已确认旧 ID；missing 拒绝；新同名无继承 |
| ZIP/MRXS/转换伴侣目录、包内引用 | `test_zip_slide_id_pg.py::test_mrxs_companion_same_bundle_atomic`、`test_zip_guard.py`（改写族）、`test_conversion_slide_id_pg.py::test_product_bundle_contains_all_members` | ✅ 入口+伴侣同包原子；manifest 唯一入口；真实解码/tile 正常 |
| 转换/远程导入自动关联目标项目 | `test_conversion_slide_id_pg.py`（项目关联按 slide_id 恰一次）、`test_baidu_ingest.py::test_b06/test_c02`（按 ID 关联） | ✅ 关联正确 ID；不因同名重复关联 |
| 旧前端/新前端、旧 sidecar 会话/新 ID 会话 | 能力旗标 `slide_id_api`（c1389b5）+ `slideIdApiOn()`、HP session-store v3 键/v2 冻结只读（unit 套件）、HP 契约①（slide-id ref 仅发 slide_id） | ✅ 能力协商明确；旧映射只读旧资产不猜 fallback |
| 删除重放/回收重试/服务器重启 | `test_slide_delete_pg.py::test_delete_cleanup_failure_retry_settles_once`、`test_daemon_picks_up_interrupted_delete`、`test_worker_concurrent_claim_no_double_execution` | ✅ deleting 门禁持续；清理与减账各一次（CAS 幂等键） |
| 迁移重跑、缺文件、无元数据、权限歧义 | `test_slide_migration_tools.py`（22 用例：确定性/三件套/中断重跑/冲突中止/隔离口径/篡改抓出/不计配额/授权零变更）、P6-1 演练 40 断言（§2-4 隔离族） | ✅ 无重绑无自动认领；隔离报告准确；回滚边界经演练 |

## 2. 门禁执行记录（命令与计数，最终态）

```bash
# PathTogether（HEAD=4891744）
rm -rf /tmp/pytest-of-solarise
TMPDIR=<仓库上层>/.gate-tmp .venv/bin/python -m pytest -q \
  --ignore=tests/test_e2e_pg_reap.py --ignore=tests/e2e
# → 2758 passed, 1 failed, 6 skipped（27:24）
#   唯一失败：tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present
#   ——他人未提交 admin 插件 0.4.13 bump 的工作区断言（P0 §1.1 登记，非本重构
#     改动面；每阶段独立复核均仅此一例）
npm run test:js            # → 38 files / 562 passed

# 迁移演练（可复现证据）
.venv/bin/python scripts/drill_slide_migration.py --evidence-dir <dir>
# → 40 断言 0 失败（编排方独立重跑复现一致）

# HistoPilot（HEAD=4598172）
npm run build              # → tsc 通过
npm run test:unit          # → 60 files / 1146 passed
npm run test:integration   # → 18 files / 340 passed
PATHTOGETHER_PYTHON=../PathTogether/.venv/bin/python \
PATHTOGETHER_REPO=../PathTogether npm run test:contract
# → 5 files / 49 passed
```

历史门禁轨迹（每阶段独立复核）：P3=2685、P4=2714、P5=2726、P6-1=2748、P6-2=2758 passed（均同口径唯一已知失败）；JS 恒 562；HP 契约恒 49/49（P3 与 P6-2 两次跨仓语义迁移后复跑）。

## 3. 浏览器验收（P2 隔离实例实测，交付日志 P2 节证据）

同名双片并存列表/打开、?slide=<id> URL 通道、PATCH 改名不串、分享 chips 按 ID 键控真实渲染、删除后主站 403/分享列表 exists=False/邻片 200。上传与标注的浏览器路径由 JS 套件（562）与跨仓契约（49）承担（IAB 不支持文件选择器上传，上传经 API 注入）。

## 4. 故障注入与配额/清理收口证据

- 崩溃矩阵：intent 前/发布后/DB commit 前（`test_slide_publish_pg.py` 19）；ZIP 受理后崩溃恢复复用 ID 单次结算；转换失败重试同 ID 单结算。
- P4 review 修复 F0–F3 的复现用例原样入仓（先红后绿）：ready 撤回破态、KFB 恢复死循环、悬挂转换 job、ZIP 撤回遗漏。
- 配额：used/reserved 逐用例核对（不漏账不双扣）；删除减账幂等键=deleting→deleted CAS（同事务）；R-12 legacy 不退款过渡口径有专测。
- 迁移演练 journal/verification/summary 落 `docs/drill-evidence-20260925/`（无 token 明文）。
