# slide ID 化重构：实施交付与门禁记录（滚动更新）

日期起：2026-09-25。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §9（Agent 最终交付格式的滚动积累）与 [P0 盘点](slide-id-refactor-p0-inventory-20260925.md) / [P1 合同](slide-id-refactor-p1-contract-20260925.md)。
分支：`slide-id-refactor`（基线 a2b0ae3）。本文每阶段一节：变更清单、提交、门禁结果、偏差与裁决。

## 阶段提交总表

| 阶段 | 提交 | 内容 |
|---|---|---|
| P0 | `3a60933` | 任务书/迁移手册/P0 盘点文档 + `scripts/audit_slide_identity.py` + 12 用例 |
| P1-A | `e2df401` | `migrations/0067_slide_asset_identity.sql`、`slide_store.py`、`slide_storage.py`、`slide_publish.py` 骨架、38 新用例（store 16 + storage 13 + migration 9）、`test_pg_infra` 清单 +0067、conftest 三新表 |
| P1-B1 | `8a0447d` | `scripts/backfill_slide_asset_state.py`（dry-run 默认、分批幂等、DB 即 checkpoint）+ 17 用例 |
| P1-B2 | `deaece4` | 读通道统一门禁 + 首个 ID 原生 API + share_server 切换 |

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

## P2 门禁（2026-09-26 凌晨）

合同：docs/slide-id-refactor-p2-contract-20260925.md。

**P2-HP1（HistoPilot 仓，提交 a07850c）**：运行通道 slide_id 化（slideIndexKey/ToolContext.slide_id/run body slide_id 优先/v3-v2 会话键分域冻结/uuidv7 误注修正）。独立复核：build + unit 1145 + integration 340 全绿。遗留回落面清单已收编（PT 代理转发 slide_id、§6.5 双字段、§6.7 跨仓契约测试——分别由 P2 后端/P2-HP2 消化）。

**P2 后端（本仓）**：活动关系全量切 slide_id（rois/comments/change_log/access_events/run_grants/ai_session_principals/audit 双写+按 ID 活动查询）、`/api/slides/<id>/` 读端点全族（app 8 + share_server 5）、双字段 slide_id/slide 解析（冲突 400）、项目 slide_ids、run grant 按 ID 校验（防伪比对）、机器通道双字段、R-15 缓存键收口 + 列表 N+1 收敛、alias 停写（R-02）、PATCH /api/slides/<id>、demo 兜底分支删除、研究伪名 ID 派生、create_share 拒绝未注册名（P1-B2 偏差 #4 收口）。

- 编排方独立门禁：第一次跑遇 tmpfs 残渣雪崩（340 errors——部署 runbook 已记载的 `/tmp/pytest-of-solarise` 堆积陷阱，佐证：清理后 /tmp 74%→20%）；**清渣后干净复跑：2658 passed / 6 skipped / 1 failed**（唯一失败=已知无关 admin 0.4.13）；`npm run test:js` 553 passed；新测试 14 用例绿。**门禁纪律修订：全量套件运行前必须先清 `/tmp/pytest-of-solarise`**。

### P2 后端偏差裁决记录

| # | 偏差 | 裁决 | 理由/收口 |
|---|---|---|---|
| 1 | annotations_by_slide 分组键保持名称快照 | **暂时接受**，P3 门禁项 | legacy_filename UNIQUE 使 legacy 资产按名分组不串；但 id_bundle 资产可同 original_filename——**P3 上线 id_bundle writer 前必须验证同名 id_bundle 资产的标注分组不串（或先把分组键切 ID）** |
| 2 | 历史 NULL-ID run grant 保留名比对兜底 | 采纳 | R-09 冻结快照语义，随 grant TTL 自然退役 |
| 3 | list_changes 名入参解析不到回退名查询 | 采纳 | 仅服务机器通道无行兼容分支（P1-B2 #2/#3，P3 收口） |
| 4 | share_server by-id 未知 ID → 403 而非 404 | 采纳 | 与名通道「不泄露存在性差异」一致 |
| 5 | /api/ai/* 接受 slide_id（合同 §3.3 未列） | 采纳 | HP §6.5 的后端前提，additive |
| 6 | force_slide_owner_follow_file 改清 display_name | 采纳 | R-02 停写必然；该函数仍是 P4 拆除对象 |
| 7 | 项目幂等路径库层不改、app 层补写双列 | 采纳 | project_idempotency_store.py 未开放；崩溃窗口残留按 name-only 行→后续回填兜底，风险低 |
| 8-⑤ | test_share_unregistered_name_lazy_row_readable 语义收紧 | **确认采纳** | 原断言保留（注册后分享可读）+ 新增收紧断言（未注册名→ValueError）；符合「保留场景意义、替换废弃断言」 |

### P2 后端遗留

- shares.slides JSONB 快照仍照写（授权判定已全部走 share_slides）——P6 退役。
- 机器通道无行兼容分支、set_slide_meta 同名复活（P1-B2 #6）→ P3 强制收口。
- 内容 revision 来源仍是 mtime:size；slide_assets.legacy_revision 消费在 P3/P4。
- 前端 static/ 切 ID（下一阶段）；浏览器实测（P2 完成标准）待前端落地后执行。

## P2 前端 + HP2 + 收口（2026-09-26）

**P2 前端（提交 8a724c8）**：state.slide 唯一操作键=id；openSlide/列表/URL 通道（新增 ?slide=<id>）/上传完成打开/项目分享标注载荷全部 ID 化；localStorage 续传 v3 键（账户域+upload_id，v2 一次性迁移）；slideIdApiOn() 能力协商 helper；slide.opened 载荷带 id。test:js 562 passed（+9 合同用例）。

**P2-HP2（HistoPilot f78f7c3）**：flask-client/legacy-flask-adapter 双字段（slide-id ref 仅发 slide_id、不发名）；跨仓契约测试 slide-id-contract.integration.test.ts 三场景（同显示名双资产全链路不串/删除失效/旧名兼容+tombstone 不复活）——真实 PT flask + pgserver + openslide。四门禁 build+unit 1146+integration 340+contract 49 全绿（编排方独立复核一致）。

**前端发现的后端五缺口收口（提交 b972ea5）**：share 列表带 slide_id、分享 ROI 双字段、research 白名单放开 slide_id、render-context by-id（主站+分享端）、conversions GET 带 slide_id；5 个回归用例入 test_slide_id_relations_pg.py（19 total）。修复过程把 `_reject_preset_rect*`/`_slide_dims_and_mpp` 增 path 透传（7 处 monkeypatch lambda 签名随适配，断言不动；修复中误吞行尾闭括号已当场修复并复跑验证）。

**浏览器实测（隔离实例：pgserver + tmp UPLOAD_DIR，app 8123 + share_server 38000）**：
- 列表两片同显示名「同名切片QA」并存，行 `data-slide-id` 各异（sld_iJvBYLjDgzaN 96×64 / sld_73HQTw7UpBUX 160×128）。
- `?slide=<id>` URL 通道打开；info 请求走 `/api/slides/<id>/info`（ID 原生端点）；两片打开内容尺寸正确不串。
- PATCH display_name by ID 生效、ID 不变；列表/标题显示新名。
- 分享（slide_ids 创建）页 chips 按 data-slide-id 键控、同名并存；chip 打开渲染真实内容（截图证据）；分享列表带 slide_id（缺口①线上验证）。
- 删除 alpha：主站 info 403（tombstone 门禁）、beta 照常；分享列表 alpha exists=False；分享 DZI alpha 403 / beta 200。
- 结论：P2 完成标准（同显示名分别打开/标注/分享/关联项目、改名不串片、两仓合同测试、真实浏览器验证）全部通过。上传/标注的浏览器路径由 JS 测试与跨仓契约测试覆盖（IAB 不支持文件选择器上传，上传经 API 注入）。

**P2 门禁合计**：PT 全量 2664 passed / 1 已知无关失败（admin 0.4.13）；JS 562；HP 四门禁全绿。

## P3 门禁（2026-09-26）

合同：docs/slide-id-refactor-p3-contract-20260925.md。变更面：0068 迁移（upload_tasks.commit_intent_json）、slide_publish 真实接线（六步编排）、V2+V1 原生单文件切新管线（创建即 allocate_slide+任务绑定同事务、暂存 .staging/、同名冲突检查拆除）、`DELETE /api/slides/<slide_id>`（四阶段：门禁 CAS→授权联动→物理清理→同事务减账）、机器通道/本地态无行兼容分支删除（P1-B2 #2/#3 收口）、annotations_by_slide 分组键切 ID（P2 偏差 #1 收口）、`_legacy_slide_revision` 对 id_bundle 改取 slide_assets。

- 编排方独立门禁（先清 /tmp 残渣）：**2685 passed / 1 已知无关失败**；`npm run test:js` 562 全绿；新测试 test_slide_publish_pg.py 19/19。
- **跨仓联动**：PT P3 落地使 HP contract 8 用例失败（裸文件播种不再可读 + 本地态 owner 强制 + 新资产无冻结别名）——编排方迁移两仓测试至新语义（HP 提交 ed8a30f）：ai-drawing 播种补注册行、slide-id 契约的 owner 注入 + ①②③ 断言换新 + register_legacy 夹具 + 冲突负例改真实 legacy 别名。复跑 **contract 49/49 绿**。

### P3 偏差裁决记录

| # | 偏差 | 裁决 | 理由/收口 |
|---|---|---|---|
| 1 | publish_slide 的 FS 发布在 advisory 锁之前（合同顺序是锁→重验→发布） | **接受**（记录偏差） | 安全性由状态机不变量保证（committing 态 cancel 拒、staging 态 delete 拒、代次 fencing 先于 FS、no-clobber+verify_bundle 幂等兜底、settle 事务再全量重验）；动机是避免长 IO 持事务锁。**P4 义务**：COS/conversion writer 接入同一 publish 路径时按其 worker lease 模型重审该顺序 |
| 2 | 语义退役断言改写族（同名冲突 409→各得各 ID、取消胜者规则重写、崩溃屏障换 settle/consume 等） | **采纳** | 逐份核对：场景意义保留（同名并发/竞态/崩溃恢复），断言换目标不变量；test_slide_publish_pg.py 19 用例覆盖 §6 全列 |
| 3 | flock sidecar `.uploading-<id>.lock` 保留平铺 | 采纳 | 纯进程间协调原语，非暂存内容；P6 收口 |
| 4 | /api/annotations 默认列表展示键投影回名称快照（同名 id_bundle 组在该列表展示级合并） | **暂时接受** | 单切片查询/项目详情/store 级全部按 ID 隔离（有专测）；前端切 ID 索引后收紧（列入 P4 前端项） |
| 5 | 本地免认证态夹具注入配置 owner（p3-local-owner） | 采纳 | 与生产「配置 owner」语义一致 |
| 6 | convert-required V1/V2 保留旧冲突检查 + 转换/COS/ZIP 响应仍按名出 slide_id | 采纳 | 合同明示 P4 随转换链/COS/ZIP 整体拆除 |

### P3 遗留（P4/P5/P6 消化）

- `set_slide_meta` 复活分支 + `test_slide_delete_clears_view_grants_no_orphans` 改写义务 → P4（ZIP 切走时）。
- `_upload_name_conflict` 余下调用（convert-required 分支）+ conversion canonical 名锁 + COS adopted/still_ours/force-owner + 百度按名对账 → P4 拆除。
- run grant 撤销的 sidecar 会话取消对 id_bundle 只撤 grant 行（按名查询运行会话不适用）→ P4/P5 补 ID 化联动。
- 升级窗口在途旧任务排空与新旧物理布局并存 → P6。
- share_server `_get_slide` 仍以 legacy 名为主键（id_bundle 资产 None 键）→ P4 收口为 slide_id 键。

## P4 门禁（2026-09-26）

合同：docs/slide-id-refactor-p4-contract-20260925.md（P4-a app.py / P4-b COS / P4-c 百度三包分工）。

**P4-c 百度远程导入（提交 9b86b4f）**：baidu_import_store 逐 item 预分配 slide_id（同事务绑定 + 租约 fence + `slide_id IS NULL` 谓词）；`_reconcile_ingest` 重写（磁盘同名不再作归属证据）；native 分支经 `.staging/<item>/1/` + 统一发布；ingest_token 改 `item:<item_id>`；associate_slide 按 slide_id。门禁：baidu 相关 94 全绿。

**P4-b COS 直传（提交 7c54d00）**：cos_ingest_worker 的 adopted_existing/still_ours/promoted_ident/`force_slide_owner_follow_file` 调用族全拆；staging 归 `.staging/<job_id>/<worker_generation>/`（跨代次 `_adopt_staged_data` 收养）；process_validating 走 `publish_with_channel`（slide_publish 新增 PublishChannel 六 hook 协议——worker 任务族复用同一发布编排，ingestion/conversion 不再各抄一份）；ingestion_store.create_waiting_job 同事务预分配 slide_id + IngestionPublishChannel（fencing=worker_generation）+ worker_settle_ready 扩展（slides CAS + accounted_bytes + revision + consume 同事务）；空 owner 建任务拒绝（IngestionStateError）。P3 偏差 #1（FS 发布先于 advisory 锁）按 worker lease 模型重审成立（validating+intent 为不可撤销提交段，fencing 拒旧代次结算）。门禁：COS/publish 相关 116 全绿。

**P4-app ZIP/转换/app 联动**：V1 ZIP 切统一发布（`_prepare_zip_bundle` 解包入任务 staging + `_zip_group_items` 逻辑切片分组 + 逐 item `allocate_slide`+`bind_upload_task_item` 同事务受理 + `publish_batch_item` 逐 item 原子发布 + `finish_commit` 按已发布合计一次结算；item 级失败隔离不拖累同包其它切片；崩溃恢复 `_upload_legacy_recover_zip` 幂等补发）；V1/V2 convert-required 切新链（源副本归 `.staging/<job_id>/source/`、create_job 即预分配产物 slide_id、worker 经 ConversionPublishChannel 发布全成员包、dest 三分支/`_retract_ours`/`_promote_work` 全拆）；`_upload_name_conflict`/`allow_kfb_recover`/`_owned_committed_upload` 定义+调用全拆；`force_slide_owner_follow_file` 定义+导出拆除（调用方已零）；`set_slide_meta` deleted/deleting→ready 复活分支拆除（P1-B2 #6 收口）+ `test_slide_delete_clears_view_grants_no_orphans` 按不变量改写（重传=新 ID、旧授权/分享不继承、显式重授权才可见）；CLI import_slides.py（复制入受管理 staging——不硬链接外部可写源——+ publish_standalone）+ seed_demo_tcga_catalog.py（回填式登记）+ migrate_json_to_pg.py 退役注释；COS 建任务名占用预检拆除 + 空 owner 映射 500；`canonical_is_live`/`NameConflict` 退役为兼容壳（P6 删）。

### P4 偏差裁决记录

| # | 偏差 | 裁决 | 理由/收口 |
|---|---|---|---|
| 1 | 新增迁移 0069（DROP idx_conversion_jobs_canonical_live + conversion_jobs.commit_intent_json） | 采纳 | 合同 §3 强制项；DB 级唯一索引只能经迁移拆除；intent 列镜像 0068 |
| 2 | upload_task_store 扩展 items 访问器三函数 | 采纳 | 0067 表归属该模块（共享 _pg_connect/事务约定），app.py 裸 SQL 违反封装惯例 |
| 3 | DROP uq_baidu_import_items_slide_id | **采纳（P5 复审义务）** | convert 幂等键（owner+sha+converter）共享同一产物资产是 create_job 既有设计语义——强制一 item 一产物是超范围行为变更；native「一 item 一资产」由分配侧保证。**P5 删除编排必须裁决多条目共享产物的删除/退款耦合** |
| 4 | 源 KFB 不落资产（无 slides 行、不对 Viewer 可见） | 采纳 | 现状语义保持；源副本归任务 staging；「源是否独立资产+独立配额/清理编排」交 P5 |
| 5 | ZIP 无法归组整体 400 指名 + item 级失败隔离在发布阶段生效 | 采纳 | 合同 §2.3/§2.4 的实现读法（prepare 无部分状态） |
| 6 | `_canonical_name_for` 保留 | 采纳 | 仅展示快照推导；路径/冲突/占用用途清零 |

### P4 review 门禁发现（编排方修复，复现用例原样入仓为回归）

- **F0** `_zip_abort_published` 对已 ready item 调 `mark_failed`（仅 staging 源）→ 静默 no-op，留「ready 行 + 无包」破态。修复：`slide_store.force_fail`（staging/ready→failed 撤回原语）+ 撤回改 DB 先行（先收口可见性再撤包，fail-closed）。回归：test_zip_slide_id_pg.py::test_reservation_expired_mid_publish_withdraws_published。
- **F1** `_upload_legacy_recover_conversion` 未显式捕 `ReservationInvalid`（不属 StateConflict 子类）→ 预占过期的 KFB committing 任务每次恢复扫描反复 finish_commit 反复抛 → **死循环保持 committing**。修复：显式捕获 → 连带作废 + `_upload_legacy_fail(permanent=True)`。回归：test_conversion_slide_id_pg.py::test_recovery_reservation_expired_cancels_job_no_livelock（两次扫描幂等断言）。
- **F2** commit 期建 job 后 finish_commit 预占失效 → 悬挂 job（用户收「文件未入账」而产物稍后被 worker 发布上线）。修复：新增 `_cancel_conversion_for_failed_upload`（只收口**本上传创建**的 job——`jobs.upload_id` 直等，幂等复用前序上传的 job 不株连；产物 ready 则 advisory 锁内同事务退款+force_fail+撤包；清 job 任务 staging），接线 V1 KFB / V2 commit 的 ReservationInvalid 处理与 F1 恢复路径。回归：test_reservation_expired_at_commit_cancels_job_and_fails_asset（窄窗注入：finish_commit 边界置过期）+ test_recovery_withdraws_settled_product_and_refunds（已结算产物撤回退款）。
- **F3** ZIP 撤回原按 ready 过滤 → 漏撤「FS 已发布、DB 因 ReservationInvalid 未收口」的本 item 包（publish_batch_item 先 FS 后 DB）。修复：请求/恢复两处撤回均改全量 plans（force_fail 覆盖 staging/ready；remove_bundle 对无包 item no-op）。

### P4 门禁合计（编排方独立复核，修复后全量）

- 全量 pytest（先清 /tmp 残渣 + TMPDIR 重定向大分区）：**2714 passed / 1 已知无关失败**（admin 插件 0.4.13 bump）/ 6 skipped；实施方报告的 2 例时序 flake 与 3 例 test_raster_wiring 在本独立运行均未复现（干净环境复核结论成立）。
- `npm run test:js`：562/562 绿。
- HP 跨仓契约（PATHTOGETHER_PYTHON/REPO 指向本树）：**49/49 绿**。
- 新增回归 4 用例（F0–F2）先红后绿（红：4 failed 复现，含死循环 traceback 证据）。

### P4 遗留（P5/P6 消化）

- 升级窗口在途旧 ZIP/KFB 平铺任务的三态恢复与 `set_slide_meta` 补归属分支（app.py:11839/:13082 一带）待 P6 排空；`canonical_is_live`/`NameConflict` 兼容壳随 baidu convert 收口删除。
- 源 KFB 生命周期编排（产物 ready 后源副本保留至产物删除的现状）+ 多条目共享产物的删除/退款耦合（偏差 #3）→ P5。
- demo 种子/CLI 导入的源平铺文件保留（P6 物理迁移统一搬运）；`.uploading-*.lock` sidecar 平铺维持 P3 裁决。
- ZIP 响应 `failures` 为新增 additive 字段（前端未消费）；`b.slide` 类展示快照回落 P6 清理。
