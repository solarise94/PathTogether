# slide ID 化重构：实施交付与门禁记录（滚动更新）

日期起：2026-09-25。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §9（Agent 最终交付格式的滚动积累）与 [P0 盘点](slide-id-refactor-p0-inventory-20260925.md) / [P1 合同](slide-id-refactor-p1-contract-20260925.md)。
分支：`slide-id-refactor`（基线 a2b0ae3）。本文每阶段一节：变更清单、提交、门禁结果、偏差与裁决。

## 阶段提交总表

| 阶段 | 提交 | 内容 |
|---|---|---|
| P0 | `3a60933` | 任务书/迁移手册/P0 盘点文档 + `scripts/audit_slide_identity.py` + 12 用例 |
| P1-A | `e2df401` | `migrations/0067_slide_asset_identity.sql`、`slide_store.py`、`slide_storage.py`、`slide_publish.py` 骨架、38 新用例（store 16 + storage 13 + migration 9）、`test_pg_infra` 清单 +0067、conftest 三新表 |
| P1-B1 | `8a0447d` | `scripts/backfill_slide_asset_state.py`（dry-run 默认、分批幂等、DB 即 checkpoint）+ 17 用例 |
| P1-B2 | `见本节末` | 读通道统一门禁 + 首个 ID 原生 API + share_server 切换 |

## P0 门禁（2026-09-25）

- 基线回归：计划基础集 167 passed；`npm run test:js` 553 passed。
- 审计工具 review：只读约束（READ ONLY 事务/不随符号链接/输出 0600+0700/token 不可逆标识）、退出码 0/1/3、四输出文件契约，均核实；12 用例绿。
- 完成标准核对：P0 文档 §5 全项勾讫（除代码旁合同随 P1-A 落位）。

## P1-A 门禁（2026-09-25）

- 全量 `pytest -q`：2612 passed / 6 skipped / 3 failed——3 个失败均判定为既有/环境问题（附证据）：
  - `test_ai_budget_wiring::test_ui_budget_card_and_max_steps_sync_present`：他人未提交 admin 插件 0.4.13 bump（P0 文档 §1.1 清单内），与本重构无关，不修。
  - `test_e2e_pg_reap` ×2：进程组回收环境用例（已知 `_ci_skip` 项，见部署 runbook 记忆）。
- review 修复（编排方直接落）：`tests/test_pg_infra.py` 迁移清单补 0067；`tests/conftest.py` `_BUSINESS_TABLES` 补 share_slides/upload_task_items/slide_delete_jobs。
- review 记录（接受，不返工）：
  - `authorize_read` 以 DB 当前行重读为准（防快照过期）——采纳为合同修订。
  - cross-volume 私有暂存放在 `objects/.publish-*`（同目录原子 rename）——采纳。
  - `os.rename` 对空目录目标的可替换性：系统内由 advisory 锁（publish/delete 第一把锁）+ storage_relpath 部分唯一 + mark_ready CAS 三层兜底；外部 actor 预建空目录属不变量破坏，审计工具可发现。记录为已知边界。
  - `admin 角色 = 'owner'`（本仓角色词表无 admin）——采纳；**后续被 P1-B2 的读隔离接线细化取代（见下）**。

## P1-B1 门禁（2026-09-25）

- 17 用例绿 + slide_store/storage/identity/audit 32 用例无回归。
- review 核实：`_READY_SQL`/`_FAILED_SQL` 列白名单（不触 legacy_filename/storage_layout、不 INSERT slides）；dry-run 会话级硬只读；MRXS 伴侣字节口径与 api_slide_delete 对齐。

## P1-B2 门禁（2026-09-25）

变更面：app.py（读端点全量切 resolver+门禁、列表 DB 化、`/api/slides/<slide_id>/info` 新端点、meta 404 收口、admin inventory DB 驱动、demo 通道直通 ID、删除直写 deleted）、share_server.py（share_slides 成员判定 + ready 门禁）、share_store_pg.py（set_slide_meta 建行写新列+alias 双写 display_name、create_share 写 share_slides）、slide_store.py（扩展 list_*/mark_deleted_compat/兼容参数）、Containerfile（+COPY 三新模块，镜像闭包测试要求）、新增 tests/test_slide_read_gate_pg.py（14 用例）。

- 编排方独立复核全量 `pytest -q --ignore=tests/test_e2e_pg_reap.py --ignore=tests/e2e`：2641 passed / 6 skipped / 2 failed——`test_ai_budget_wiring`（已知无关项 admin 插件 0.4.13）+ `test_kfbf_real_samples[ZHY-Cont]`（复核结论：环境性 flake——代理全量 run 通过、单独复跑通过、清 /tmp 残留后整文件 4/4 通过；tmpfs 残渣压力是部署 runbook 已记载的陷阱，与本次改动无关）。
- `npm run test:js`：553 passed（响应形状未破坏旧字段）。
- 新门禁测试 14 用例覆盖 P1 完成标准：随机 ID 不可越权、staging/legacy/deleting/deleted/failed 全读通道拒绝、无行文件不列出不可读、旧名映射不重绑。

### P1-B2 偏差裁决记录（编排方复核结论）

| # | 代理披露偏差 | 裁决 | 理由/收口条件 |
|---|---|---|---|
| 1 | `authorize_read` 增 `allow_public`/`allow_share` 兼容参数 | **采纳**（合同修订） | 对照基线 `can_view_slide`（升级 B R5）：认证 owner=本人∪显式授权（public/claimed 不自动计入）、user=本人∪public∪claimed∪授权、本地免认证单租户全量。我 P1 合同的"admin 全读"判定序与该仓冻结语义（test_access_control）冲突，代理的按角色接线正确。P2 权限关系统一时在合同文档回填正式表述 |
| 2 | 机器通道（internal/plugin）对"无 slides 行"文件维持旧可读 | **暂时接受**，P3 收口 | 行存在即受 ready 门禁（核心收益已得）；无行兼容只因既有夹具只放文件不建行。P3 writer 全接管后收口为"无行一律拒"，届时删 `_legacy_row_state_gate` 的无行分支与对应夹具兼容 |
| 3 | 本地免认证单租户态无行文件保持可读 | **暂时接受**，P3 收口 | 同上；认证部署下无行已一律拒（新测试覆盖） |
| 4 | `create_share` 对无行名懒建行 | **暂时接受**，P2 收口 | 保持"先建分享后放文件"旧流程；语义=set_slide_meta 同款迁移兼容 writer。P2 分享创建改显式 slide_id 后收口为"仅接受已发布资产" |
| 5 | admin inventory 孤儿文件并入 items（unregistered=true） | **采纳** | 权威面是 orphan_files（零认领）；items 并入是既有测试冻结的展示口径 |
| 6 | `set_slide_meta` UPDATE 复活 deleted/deleting→ready（同名重传复用 slide_id） | **暂时接受**，P3 强制收口 | 与基线行为等价（旧 JSONB 按名分享同样继承），未扩大风险面；但**注意**：继承通道从 JSONB 换成了 share_slides ID 关系。P3 legacy writer 退休时：删复活分支、重写 `test_slide_delete_clears_view_grants_no_orphans` 为目标不变量（重传=新 ID、旧分享/授权不继承；场景意义保留、断言换新），并核查 revive 期间 share_slides 继承向量随新 ID 自然消灭 |

### P1-B2 遗留（已登记，后续阶段消化）

- `_visible_slide_names` 逐行 authorize_read 重读 DB（N+1）：P2 列表收敛单 SQL。
- tile 缓存/render 统计 scope/render token 的 slide 绑定仍按 legacy 名（R-15 全量在 P2）；句柄缓存键已 ID 化，`_close_slide` 双键 evict。
- demo `resolve_slide_filename` 兜底分支（仅 catalog 指向无行资产的不可能形态）：P2 随 demo 目录强校验删除。
- 隐式建行残留位（demo PUT、V2 读路径校正、admin visibility 建行、create_share 懒建行）：P2/P3 收口清单。

## 下一阶段（P2）范围预告

计划 §4 全量：active 关系切 slide_id（rois/comments/change_log/run_grants/ai_session_principals/project_slides）、前端 static/* 与 HistoPilot 读通道 ID 化 + 能力协商、`/api/slides/<slide_id>/...` 读端点全族、PATCH display_name、跨仓契约测试。编排：先后端关系+API（单代理持有 app.py），再前端（static/）与 HistoPilot（独立仓）并行。
