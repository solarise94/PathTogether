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

## P5 门禁（2026-09-26）

合同：docs/slide-id-refactor-p5-contract-20260925.md。变更面：0070 迁移（slide_delete_jobs 补 lease 列+调度索引——任务书误称 0067 已含 lease 列，实施方更正）；`request_delete` 扩展为「ready→deleting CAS + 任务落库同一事务」；删除任务原语族（enqueue/claim 退避+SKIP LOCKED/finish/get）；app.py 执行器族（`_slide_delete_unified`/`_invalidations`/`_physical`/`_settle`/`_execute` + `run_slide_delete_worker_once` + daemon 线程 slide-delete-worker，TESTING 下停执行、测试直调单轮）；`DELETE /api/slide/<name>` legacy 端点归一进同一编排（alias 解析→同一状态机）；`mark_deleted_compat`/`_slide_delete_legacy_core` 按承诺拆除；孤儿扫描 objects/（无行/staging/failed/legacy 布局→报告，ready+id_bundle 与 deleting/deleted 不报）+ .staging/ 残留（四键空间判活，DB 异常 fail-closed 全按在途）+ 清理端点 `DELETE /api/admin/v1/slides/staging-residue`（重验活谓词，活键 409）；baidu_import_store.invalidate_items_for_slide（在途→failed 终态、ready→标注 slide_deleted）。

### P5 偏差裁决记录

| # | 偏差 | 裁决 | 理由/收口 |
|---|---|---|---|
| 1 | 新增 0070（lease 列）——任务书误称 0067 已含 | 采纳（任务书更正） | 纯 state CAS 无法安全处理 cleaning 中 worker 崩溃的死任务 |
| 2 | legacy 端点对无 slides 行的平铺文件由按名 unlink+200 改 404 | 采纳 | 无行文件=orphan_files 人口（只报告不认领）；统一编排需要资产行（状态机+账本）；物理处置走 P6 |
| 3 | legacy 端点错误路径语义（非可删态 409/清理失败 503 delete_retryable/重放 200 幂等） | 采纳 | 响应信封不变（legacy 恒 {ok:true}）；旧恒 200 的静默部分清理语义本就是债务 |
| 4 | legacy 布局资产允许 legacy/failed 态进入 deleting（id_bundle 恒严格 ready） | 采纳 | 旧端点 any-state 管理清理能力场景保留；结算幂等不受影响 |
| 5 | 孤儿 objects 谓词不按合同字面（「行非 deleting/deleted→报告」会把 ready 活包误报） | 采纳（合同表述修正） | 实现为「无行或行不可能合法持包（staging/failed/legacy 布局）」 |
| 6 | inventory 每次调用附带全树扫描 | 采纳 | admin-only；量大再分页/缓存 |
| 7 | COS 取消释放顺序（事务内释放预占、提交后清树）维持 P4-b 形态 | 登记不翻案 | P4 review 已裁决；树残留由 worker 重试/孤儿扫描兜底 |
| 8 | baidu 多条目共享产物的删除语义（P4 偏差 #3 复审义务） | **裁决成立** | 删资产本体+全部引用行失效（不删行保批次审计）；窄竞态（worker 租约写回晚于失效）由执行器可重入+读取门禁+恢复对账兜底 |

### P5 review 观察（不改代码，记录在案）

- `_upsert_delete_job` 的 `requeue_failed` 形参未进 SQL（ON CONFLICT 的 WHERE 恒为 state='failed'）——行为正确（request_delete 的冲突分支不可达：CAS 成功蕴含无既有任务行；enqueue 只需复位 failed），形参为装饰性，P6 顺手清理。
- `_slide_delete_invalidations` 各步 best-effort（单项失败记日志继续）而非字面 fail-closed——方向安全：残留授权指向 deleted 资产由读取门禁兜底（tombstone 冻结别名不重绑），不阻断物理清理。

### P5 门禁合计（编排方独立复核）

- 全量 pytest（清 /tmp 残渣 + TMPDIR 重定向）：**2726 passed / 1 已知无关失败（admin 0.4.13）/ 6 skipped**（与实施方报告一致；新增 11 用例）。
- `npm run test:js`：562/562 绿。HP 跨仓契约复跑：**49/49 绿**。
- 取消路径统一（合同 §1.4）经盘点确认 P3/P4 已就位（V1/V2 取消、ZIP 撤回、COS cancel_job、过期 sweep 均经 remove_staging_tree+failed+释放），本阶段零改动+回归断言。

### P5 遗留（P6 消化）

- baidu 在途 item 的 staging 物理残留由 item 失败收口/孤儿扫描承接。
- objects/ 孤儿与 legacy 平铺孤儿文件的物理处置 → P6 迁移工具链；`.uploading-*.lock` 平铺 sidecar 维持 P3 裁决（P6 收口）。
- daemon 退避用应用侧时钟对 DB 时钟（同宿主实践无碍；跨主机部署时评审）。

## P6 第一段门禁（2026-09-26）：迁移工具链 + 合成副本演练

合同：docs/slide-id-refactor-p6-contract-20260925.md §1/§2/§4（§3 运行时退役=第二段独立提交点）。

**交付**：`scripts/plan_slide_migration.py`（冻结审计→确定性计划；零副作用不 import 业务模块，静态契约有测试；计划头 version/env/输入 sha256，无时间戳）；`scripts/migrate_slide_storage.py`（dry-run 默认；--apply 三件套 plan-digest+env+quiesce-proof 缺一拒绝；五态 planned→copied→verified→bound→postverified 持久 journal 逐事件 fsync；私有 staging 复制不硬链接；空间不足阻塞；publish_bundle_no_clobber+新原语 `slide_store.bind_id_bundle_layout` CAS（expected layout/state 谓词、migrated/already/LayoutBindConflict 三态、relpath 强制 objects/<sid>/ 下、不动配额不动授权）；源冻结重验漂移中止；不删源）；`scripts/verify_slide_migration.py`（独立重读 DB+磁盘，journal 仅作 manifest 交叉证据不信 success 字段——篡改有专测；10 张关系表引用落点；tombstone×同名重生交叉验证；配额对账只报告不改账+合法「不等于」披露；授权差异双向 diff；incomplete 退出码 3）；`scripts/drill_slide_migration.py`（演练驱动+合成世界夹具）；`tests/test_slide_migration_tools.py` 22 用例；演练证据 `docs/drill-evidence-20260925/`（文本 6 件，无 token 明文）。

**演练**：合成世界 5 migrate（svs/tif/mrxs 伴侣/kfb 产物形态/kfbf→ome）+4 隔离+孤儿+tombstone×同名新资产+关系全谱；三处崩溃注入（copied/after_publish/bound）恢复幂等；40 断言 0 失败；verify 结论 go；授权表逐行一致、used_bytes 零变更。**编排方独立重跑演练复现通过（40/40）**。

### P6-1 偏差裁决记录

| # | 偏差 | 裁决 | 理由/收口 |
|---|---|---|---|
| 1 | MRXS 试开用进程内合成 stub | 采纳 | 真 mirax 驱动需真实厂商数据集；**生产终审须真实 MRXS 样本试开**（P7 硬门禁输入） |
| 2 | quarantine 口径：legacy 态行不动（人工决议通道依赖 backfill 重扫）、ready 态物理不可信行 force_fail 收口 | 采纳 | 两形态均满足不可读不列表；failed 无回 ready 原语，翻了会锁死人工决议 |
| 3 | NULL-alias 行不进逐项计划 | 采纳 | 无 legacy_filename=无 legacy 物理存在=不在迁移人群；header 计数披露 no_alias_rows_out_of_scope |
| 4 | kfb 派生物（.manifest.json/.associated）留置原位不迁移 | 采纳 | runbook §2.3 派生数据保留/重建分类；计划 derivatives_in_place 披露；verify 对账差异作合法原因披露 |
| 5 | 授权 diff 只对 ID 基字段（名基快照映射以信息项披露） | 采纳 | 名基 JSONB 快照与按 ID 重算不同源，diff 会假阳性 |
| 6 | 崩溃注入为进程内 SystemExit(130) | 采纳 | journal 逐事件 fsync 与 kill -9 的已落盘证据等价 |

### P6-1 门禁合计（编排方独立复核）

- 全量 pytest：**2748 passed / 1 已知无关失败 / 6 skipped**（2726+22 新增）；test:js 562/562；HP 契约 49/49。
- 演练独立重跑：40/40 断言通过（证据可复现，非一次性 artifacts）。

### P6-1 遗留（第二段与 P7 输入）

- 第二段（§3）：运行时 legacy 物理读取退役、canonical_is_live/NameConflict 壳删除、升级窗口在途分支（_upload_legacy_promote_state/_upload_v2_set_ownership）排空拆除、.uploading-*.lock 收口、share_server 残留复核、_upsert_delete_job.requeue_failed 装饰形参清理。
- P7：生产停写窗口+一致备份方案、旧平铺源+留置派生物的独立清理 manifest、真实 MRXS 样本试开、shares.slides JSONB 退役顺序、COS capability 维持 off。

## P6 第二段门禁（2026-09-26）：运行时 legacy 物理读取退役

合同：docs/slide-id-refactor-p6-contract-20260925.md §3（前置=P6-1 演练绿=66b6e18）。

**交付**：`resolve_descriptor_path` legacy 支路拆除（legacy→ValueError；唯一 legacy 物理读取=`resolve_legacy_path_for_migration`【迁移专用】，rg 证明运行时模块零调用）；`authorize_read` 判定序第 0 步加 layout 门禁（ready **且** id_bundle 才是唯一可见性开关；DB 当前行重读含 layout——单一 choke point，机器/分享/插件通道同口径）；`visible_ready_slide_ids` SQL 同口径；`canonical_is_live`/`NameConflict` 兼容壳删除（baidu_ingest 两调用点收口，磁盘 O_EXCL 检查保留）；升级窗口在途分支全拆（`_upload_legacy_promote_state`/`_upload_legacy_remove_artifacts`/`_upload_v2_set_ownership`/`_promote_no_clobber`/V2 平铺提升+按名 ownership/恢复旧提升判定/`_upload_v2_state_dict` 按名回落/`_ensure_conversion_job` 平铺探测回落）——旧形态 committing=fail-closed 保持+日志，旧形态 active 主动 commit=409 legacy_upload_task_unsupported；`.uploading-*` 平铺写入点清零（part/lock 恒在 `.staging/<uid>/`）；revision 全族统一 `_slide_revision`/`_share_revision`（id_bundle=slide_assets sha——app+share_server 约 15 处，含 X-Asset-Revision/attestation/wire-context）；`_upsert_delete_job.requeue_failed` 装饰形参清除（P5 观察收口）。

**测试**：新增 `tests/test_p6_legacy_runtime_retirement.py`（9 用例：legacy 全通道不可读/分享成员不泄露/冻结别名+已迁移行全通/旧形态恢复保持 committing/锁新位无平铺残留/兼容壳模块面断言/revision 取数）；夹具主收敛点 `tests/_pt_helpers.register_slide_row` 改 id_bundle 建仓（publish 建仓+保留冻结别名与平铺源——「register_legacy 控制动作」等效落地）；改写 24 个既有测试文件（场景保留、断言换目标不变量，逐份见实施报告）。

### P6-2 偏差裁决记录

| # | 偏差 | 裁决 | 理由/收口 |
|---|---|---|---|
| 1 | layout 门禁落 authorize_read+visible_ready_slide_ids（非逐端点 try/except） | 采纳 | 单一 choke point；legacy 行对 owner/admin 读取同拒（不泄露存在性）；管理台 inventory 经 list_all_descriptors 仍可管理 |
| 2 | revision 统一超字面清单（任务8 扩展，约 15 处） | 采纳 | 夹具迁移暴露取数分叉（id_bundle 签 sha、验签名 mtime:size→全链 409 的真缺陷）；值语义变更（mtime:size→sha256:hex16）经 HP 契约复跑验证 |
| 3 | requeue_failed 选「清形参」 | 采纳 | SQL WHERE state='failed' 本就是正确语义；接线反引入行为变化 |
| 4 | 旧形态 active 主动 commit=409 legacy_upload_task_unsupported | 采纳 | committing 恢复 fail-closed 的请求路径等价物（合同未明示，「不猜」补全） |
| 5 | conversion_worker.resolve_source 分支3保留 | 采纳 | baidu O_EXCL 源副本唯一读取方；注释改标 baidu 专用 |
| 6 | invalidate_by_slide_id(legacy_canonical=) 未拆 | 采纳列遗留 | 与 P5 删除编排/quarantine 产物行删除联动，拆除收益小破坏面大→P7 |
| 7 | 夹具走 publish 建仓路线（仓库无既存 register_legacy 动作） | 采纳 | 等效「register_legacy 控制动作」；保留冻结别名与平铺源 |

### P6-2 跨仓联动（编排方处置）

- HP 契约 6 败（预期内退役语义）：ai-drawing setup_slide 的 bare 文件+set_slide_meta（legacy 布局）与 slide-id ③ 的「legacy 可读」旧承诺。编排方迁移 HP 测试（**HP 提交 4598172**）：setup_slide 改 id_bundle 建仓+冻结别名（迁移后历史资产真实形态；同文件多 it 别名 UNIQUE 清旧再挂）、slide_revision 控制动作改 descriptor 取数；③ 场景保留断言换新（③a 迁移前机器通道 404 不泄露存在性；③b 迷你迁移 migrate_to_id_bundle=publish 建仓+bind 保留别名+revision 落账后名通道照常、rois.slide_id 正确）。复跑 **49/49 绿**。
- 值语义变更登记：id_bundle 的 `asset_revision`/`X-Asset-Revision` 族从 `` 或 mtime:size 统一为 `sha256:<hex16>`（HP 侧为 PT 下发值回显，契约验证无需 HP 代码改动）。

### P6-2 门禁合计（编排方独立复核）

- 全量 pytest：**2758 passed / 1 已知无关失败 / 6 skipped**（2748+9 新增+1 差值为改写计数浮动）；新测试文件 9/9 独立复跑；test:js 562/562；HP 契约 49/49。

### P6-2 遗留（P7 输入）

- `set_slide_meta` 懒建 legacy 行仍是 demo 目录 PUT 与 admin visibility 孤儿收录的登记通道（预标允许残留位）——P7 随 demo 目录强校验收口。
- `scripts/seed_demo_tcga_catalog.py` 平铺 demo 播种需切迁移工具或 objects 播种。
- `invalidate_by_slide_id(legacy_canonical=)` + `_cleanup_conversion_sidecars` 平铺派生物清理 → P7 独立清理 manifest 统一收口。
- `_ai_run_render_context` 名回落（descriptor 解析失败 mtime:size）保留为防御路径。
- `shares.slides` JSONB 退役顺序文档化（P7）。
- P6-1 遗留继续：生产停写窗口/旧平铺源清理 manifest/真实 MRXS 样本终审/COS capability off。

## P7 门禁（2026-09-26）：交付验收与部署包（不部署）

计划 §3.3/P7 + 手册 §5/§6。产出两份终审文档（编排方亲自撰写，无代码变更）：

- **docs/slide-id-refactor-acceptance-matrix-20260926.md**：§8 矩阵 17 行逐行映射到持久回归用例（真实 PG/目录、阶段屏障故障注入）+ 门禁执行记录（命令/计数/排除原因）+ 浏览器验收与故障注入证据索引。用例数逐一对仓核对（publish 19/read_gate 10/delete 11/migration_tools 22）。
- **docs/slide-id-refactor-deployment-package-20260926.md**：两仓提交对应与发布顺序约束（PT 先行 HP 随后、镜像摘要写死）、0067–0070 迁移序列与回填先决、手册 §5 硬门禁逐步落为生产执行单（停写→审计→回填+迁移→独立核验→开放读写）、回滚边界（含 0069 DROP 的 canonical 索引在回滚旧版前须重建的关键提示）、退役清理清单（旧平铺源 manifest/派生物/JSONB 快照/set_slide_meta 残留通道）、明确未做清单（防状态混报：生产审计/迁移/停写未执行、COS capability off、真实 MRXS 终审待生产、Playwright e2e 环境排除）。

**终验门禁（最终态 HEADs：PT 4891744 / HP 4598172）**：
- PT 全量 2758 passed / 1 已知无关失败（admin 0.4.13）/ 6 skipped；test:js 562/562。
- HP build（tsc）通过 / unit 1146 / integration 340 / contract 49——四门禁全绿。

**范围边界确认**：未执行生产部署、生产数据审计/搬迁、生产停写；COS capability 保持 off。交付状态=「工具完成 + 副本演练通过 + 方案待批准」，与「生产迁移完成」严格区分（部署包 §7）。

## 阶段总收尾（P0–P7）

- 分支：PathTogether slide-id-refactor（a2b0ae3→4891744，21 提交）+ HistoPilot slide-id-refactor（26fd2a9→4598172，4 提交）。
- 核心设计全部落地：新上传独立 slide_id；文件名仅展示；强制转移归属/同名认领/名冲突锁/平铺读写旁路全拆（rg 证据逐阶段）；统一发布（六步+PublishChannel）/统一授权（authorize_read+layout 门禁 choke point）/统一删除（slide_delete_jobs+daemon）/统一配额（预约-实占-退款幂等）；无法确认归属先隔离不猜测。
- 运行时终态：读路径只认 id_bundle；legacy_filename=冻结别名（固定 ID 查找）；迁移工具链就绪且副本演练通过（40 断言可复现）。
- 下一步在用户：批准部署包 → 晚间低峰窗口执行生产审计与迁移（[[deploy-evening-preference]]）。

## R6 独立审查处置（2026-09-26 晚）：6 项问题全部修复

审查方（用户）独立审查提出 6 项问题（5×P1+1×P2），复现脚本 7 反例。处置按
[[review-fix-workflow]]：编排方逐项亲自复现（先红）→ 修复 → 复现用例原样入仓为回归（断言不动）→ 全量回归。复现入仓：`tests/test_slide_id_review_regressions.py`（6 例）+ HP `test/session-alias-id-review.regression.test.ts`（1 例）。

| # | 问题 | 修复 | 回归 |
|---|---|---|---|
| 1 | 新 ID 资产 AI 会话按原名鉴权（legacy_filename=NULL → 合法属主 403） | `_require_ai_session_owner` 重构：新增 `_ai_session_slide_descriptor`（session.slide_id 权威→resolve_slide_id；无 ID 历史会话仅经冻结别名）+ `_ai_session_subject_can_view`（与读端点 session 通道同语义的显式 role/uid authorize_read）；detail/stream/path/cancel/archive 共用闸一次性收口 | test_new_id_session_owner_can_access |
| 2 | 迁移终验漏检 owner/public/分享成员/授权主体（计数≠授权） | audit 集合级采集（view_grant_users 按 ID∪名残留、share 成员 token **sha256[:16] 摘要**——evidence 不落 token 明文）；plan 冻结 `authorization_freeze`；verify 逐主体/成员/owner/public 重读比对（双向 diff+violation）。数量比较仅保留给内容类关系 | test_lost_share_membership_blocks_go / test_owner_transfer_blocks_go |
| 3 | 清理失败仍释放容量预约（文件残留而 reserved 归零） | `_upload_v2_cleanup_part` 返回 bool；0071 迁移建 `upload_cleanup_pending`（attempts/last_error 持久）；取消/预占失效/原生失败路径「清理确认后才释放」（`_upload_v2_cleanup_confirmed`）；重试=重复 DELETE 幂等 + 管理员 staging-residue 端点确认清理后释放；commit 后残余清理不挂预占（hold_reservation=False） | test_cleanup_failure_preserves_capacity |
| 4 | 冻结审计未绑定 MRXS 伴侣逐文件内容（等长改字节逃逸） | audit frozen 模式采集 `companion_members`（path/size/sha256；symlink/读取变化→issue+incomplete）；plan 冻结进 source；migrate `validate_source_frozen` 成员集合精确比对+逐文件 sha（清单缺失 fail-closed 拒绝迁移）+复制后 staging↔冻结清单再比对；verify 终验 bundle manifest↔冻结清单逐文件核对 | test_frozen_companion_same_size_mutation_rejected |
| 5 | 未核准配额差额仍放行 go | `verify_quotas`：delta==failed+deleted 桶**精确归因**才自动接受（机判可证来源）；其余差额（含一切负值欠账）须 `--quota-approvals` 逐项核准凭据（user_id+delta 精确匹配+reason），否则 violation 阻断 go；演练/测试两遍法演示核准流程；演练世界播种改按迁移后口径精确归因 | test_zero_used_bytes_blocks_go |
| 6 | HP 无 ID 旧会话可被同名新资产续用（复用旧 transcript） | `SessionStore.acquire`：旧会话无 slide_id 且请求携 slide_id → 同名不构成同一资产证据，缺 `aliasIdVerified`（可信冻结别名映射）一律 SessionConflict（要求新会话）；agent-runner 全部调用点走严格缺省 | HP session-alias-id-review.regression.test.ts |

**审查连带修正**：
- 部署包 §3 顺序矛盾修正（审查末段）：在线预审（不停写）→ 停写+备份 → 部署维护版本（窗口内不开放读写）→ 回填 → 冻结审计+计划 → 物理迁移 → 独立终验（含配额核准）→ 开放读写；逐 worker 停写验证入部署单。
- 既有保密断言（计划文件不含分享 token 明文）在修复#2 首版被抓（token 明文冻入计划）→ 改 sha256[:16] 摘要后通过——runbook「秘密不入 evidence」口径保住。

**演练证据刷新**：drill 40 断言重跑 0 失败（BOB 差额断言更新为精确归因不变量；证据目录无 token 明文已核）。

**R6 门禁**：审查复现 6+1 全绿；migration tools 22+6 全绿；上传/发布/删除/P6 退役套件 102 全绿；JS 562；HP build+unit 1147（+1 回归）/integration 340/contract 49。全量 pytest（修复后终态）：**2764 passed / 1 已知无关失败（admin 0.4.13）/ 6 skipped**（2755 基线 + 6 审查回归 + 夹具迁移净增；首轮全量曾现 10 失败＝5×ai_session_owner + 2×ai_proxy + 1×ai_credentials + 1×ai_budget + 1×admin——全部为会话守卫收紧暴露的 legacy 布局旧口径夹具，4 文件迁移至发布建仓后收敛）。

## R7 复核处置（2026-09-27）：3 项未闭环问题全部修复

R6 修复复核（用户）确认原 7 反例通过，扩展检查发现 3 项缺口（2×P1+1×P2）。处置同 workflow：3 反例亲自复现（先红）→ 修复 → 原样入仓（`tests/test_slide_id_review_followup.py` 前 3 例，断言不动）+ 编排方补两条完整收尾路径回归（后 2 例，标注非审查方）→ 全量回归。

| # | 问题 | 修复 |
|---|---|---|
| P1-a | 待清理预约仍被 TTL/新准入回收（`reserve_upload_locked` 惰性回收不排除 pending 引用） | 回收 SUM/UPDATE 两处加反连接 `reservation_id NOT IN (SELECT reservation_id FROM upload_cleanup_pending …)`——同事务+配额行锁内，与登记/释放不竞态；待清理容量责任持续有效（不靠续租复活），只能经「清理确认」路径释放；pending 行删除后回到正常回收口径 |
| P1-b | 授权快照漏分享领取权限（grants.user_id/active/permissions）与分享控制状态（shares.revoked/expires_at/permissions） | audit 逐资产采集 `share_states`/`claim_grants`（token 摘要关联；expires_at 冻结**存储值**——自然过期不改列值不产生假阳性，改值即漂移）；plan 冻结；verify 同口径重读逐项比对（撤权/换主体/改权限/撤销/过期时刻变化均违规） |
| P2-c | `clear_cleanup_pending` 在 dict_row 下取 row[0] 必 KeyError（事务回滚、pending 残留；管理员路径无兜底→物理清理成功仍未释放却报成功） | 改按列名取值；编排方补两条**完整收尾路径**回归：用户重复 DELETE（恢复后 200+清树+释放+消行一次完成）、管理员 staging-residue 确认清理（响应新增 additive `released_reservation` 字段；释放+消行验证） |

**R7 门禁**：3 审查反例+2 编排方收尾路径回归全绿；migration tools 22+R6 回归 6+R7 回归 5 全绿；演练 40 断言 0 失败（证据刷新，含新冻结字段）；test:js 562；全量 pytest 终态见提交信息（唯一允许失败=admin 0.4.13）。

## R8 复核处置（2026-09-27）：并发漏账与排序误报两项闭环

d9cb3f3 复核（用户）发现 2 项（P1+P2），2 反例亲自复现（先红）→ 修复 → 入仓（`tests/test_slide_id_review_r8.py`：P1 按审查记录指引改双串行胜者注入——锁协议使原同步注入不可交错且会自锁，断言语义不变；P2 审查方原样断言通过）。

| # | 问题 | 修复 |
|---|---|---|
| P1 | `record_cleanup_pending` 无配额行锁，可在准入回收 SUM/UPDATE 之间提交——UPDATE 反连接跳过该预约但减账仍按先前 SUM（READ COMMITTED 两语句非同快照）→ 漏账 | ① 回收减账改 `UPDATE ... RETURNING` 逐行合计（实际转换行，无漂移聚合）；② 登记侧统一锁协议：先取预约所属用户的配额行锁（与回收/释放同锁）再 upsert；③ 回收先赢时序（预约已 released）由登记侧**重激活**：翻回 reserved（expires 推远）+ 配额补记其 reserved_bytes——「随后才发现清理失败」的实际残留重新有容量责任（仅重激活 released；consumed/settled 是真实结算绝不复活）。释放路径本就按行转换减账（预约行锁+状态 CAS），无需改 |
| P2 | share_states/claim_grants 冻结侧按 token 摘要排序、重读侧按原文排序，摘要序≠原文序——未变授权被误报漂移（no-go 假阳性） | 两侧统一规范化排序键（`_ss_sort_key`/`_cg_sort_key`：token/控制位/主体/permissions 全键）后再比较；主体与权限变化的检测保持 |

**R8 门禁**：双串行胜者+审查方 P2 共 3 回归绿；四组回归文件合计 36 绿；test:js 562；全量 pytest 终态见提交信息（唯一允许失败=admin 0.4.13）。

## R9 复核处置（2026-09-27）：重激活并发缺陷闭环

f4e9e76 复核（用户）发现 1 项 P1（2 反例同一根因：登记在取配额行锁前读预约状态、取锁后不重读，重激活无 CAS——①锁前读到 reserved、锁内被并发回收→跳过补账漏账；②两登记方都锁前读到 released→各补记一次重复记账）。修复：

1. `record_cleanup_pending`：首查只定位 user_id（行上不可变，无 TOCTOU）；配额行锁内以 **CAS UPDATE（state='released'→reserved）RETURNING reserved_bytes** 作权威重读——仅实际转换成功的行补记，一次且仅一次（并发回收在此正确收账、并发重激活在此 CAS 落空不重复）。
2. 锁序审计与统一（审查要求）：准入回收/待清理登记为 quota 行→reservation 行；release/consume 原为反序（reservation 行→quota）——与准入回收同预约并发时存在锁序倒置死锁面。统一为 **quota 行锁 → reservation 行锁** 全序（`_lock_quota_row` helper；user_id 先无锁定位）。renew 只锁预约行不动配额，无倒置面。

**R9 门禁**：2 审查反例转绿（游标代理注入原样入仓 `tests/test_slide_id_review_r9.py`）；五组回归文件合计 38 绿；test:js 562；全量 pytest 终态见提交信息（唯一允许失败=admin 0.4.13）。

## R10 处置（2026-09-27）：预约操作全量收敛同一锁协议

审查指令五项全部落地（基线 3d14e3c）：

1. **统一顺序 quota 行 → reservation 行**：`renew_reservation_locked` 补配额锁（renew 自身不改配额，但调用方事务其后有 acquire/re-admit/release——统一先行取锁，整链不再倒置）；首查只定位 user_id（行上不可变），锁内重读状态/有效期/字节。多条预约的加锁顺序由配额行锁先行串行化（同用户准入互斥；跨用户 UPDATE 只触本用户行）。
2. **`release_reservation` 删重复实现**：公开入口只开事务委托 `release_reservation_locked`——状态转换与减账仅存一份（旧公开入口按预约→配额加锁，与准入交错真实死锁）。
3. **`topup_reservation` 修正**：定位 user_id → 配额 FOR UPDATE → 预约 FOR UPDATE（state/expiry 谓词在锁内 SQL 判定，不用锁前快照）→ 锁内验配额 → 同事务加码预约与配额；失败整笔回滚。
4. **COS 常驻续租整事务修正**：`renew_active_local_reservations` 的事务序变为 job 行 → quota 行 → reservation 行（renew/re-admit/release 内部统一先行配额锁）。锁序审计：job 行全部 FOR UPDATE 站点（发布 precheck/claim/FIFO 准入/sweep）均先锁 job 再触配额，全库无「持配额锁等 job 行」路径（docstring 记录）。
5. **CAS/RETURNING 记账保持**：重激活（released→reserved RETURNING）与释放/转实占的一次结算不变，异常整笔回滚。

**验收（tests/test_slide_id_review_r10.py，起始屏障 + 真实 PG，5×重跑扰动胜者）**：
- 释放×过期回收同预约：无死锁、恰一次 released、账本 100→50、重复释放不改账本；
- 补占×转实占同预约：无死锁、终态 consumed、失败方仅 ReservationInvalid（整笔回滚无半更新）、账本 used=100/reserved=0、重复 consume 不重复记账；
- COS 过期续租×同用户新准入：无死锁、旧预约两路径终态 released、账本不变量 `quota.reserved == SUM(reserved 行)` 恒成立、recovered(150)/skipped(50) 两合法终态、后续轮次幂等不改账本。
- 屏障只做同时起跑——锁序统一后反序交错已被协议禁止，不再中途注入强制非法交错（审查要求）。

**R10 门禁**：三场景 3 例（×5 重跑）绿；十组相关套件 133 绿；test:js 562；全量 pytest 终态见提交信息（唯一允许失败=admin 0.4.13）。

## R11 处置（2026-09-27/28）：上传容量生命周期——任务持有容量、租约控制执行

审查（c1d2b6d 复核）：锁序项通过；COS 恢复验收两项问题——P1 活跃任务被
回收后长期 uploading+released（本轮测试误当合法 skipped）；P2 R10 并发
验收把第二轮续租结果当首轮、未确定性覆盖续租先赢。处置按用户提供的
[task-capacity-lifecycle-repair-agent-plan-20260927.md](task-capacity-lifecycle-repair-agent-plan-20260927.md)
全量实施（A–E 五阶段，允许分提交、本次一并落地）：

**模型（migrations/0072）**：`upload_reservations` 增持有者三元组
（holder_kind/holder_id/purpose，全空或全有 CHECK）+ origin
（admission/reconcile）；部分唯一索引「同 (holder, purpose) 一份未结算
责任」。绑定预约**不参加 TTL 回收**——expires_at 退化为执行租约；
`ingestion_jobs` 增 local_cleanup_* 四列（本地清理与远端 cleanup_* 分列）。

**统一原语（upload_guard）**：准入即绑定（reserve 的 holder 参数 /
`bind_reservation_locked`，任务行同事务）；`renew_reservation_locked`
对绑定行**重发租约**（过期可重发、不重新准入、不换 rid；未绑定保持不
复活）；`release/consume_reservation_locked` 带持有者语境
（`expect_holder`，无语境/不匹配 → `ReservationHolderMismatch`
fail-closed）；判定拆分 `reservation_holds_capacity`（容量）vs
`reservation_is_active`（执行许可）；在途口径=未结算责任（绑定不看租约；
pending 豁免执行槽）；每小时只计 origin='admission'；
`add_used_bytes_locked` 收口 used_bytes 唯一财务 SQL（转换通道接入）。

**通道切换（C）**：
- COS：准入即绑定；续租收敛为同 rid 重发 + 不变量处置（rid 缺失/
  released/consumed/绑定不符 → `local_reservation_invalid` 终止进清理
  编排，**不再 skipped 挂死**——R11 P1）；parts/sign 增容量绑定门禁
  （409 local_reservation_invalid）；cancel/fail/sweep 不再先释放——
  先持久化 local_cleanup 责任（保留预约）→ 事务外删树 → 确认后
  `confirm_local_cleanup` 按持有者释放；失败 `record_local_cleanup_
  failure` 退避重试（调度器 `retry_local_cleanups`），耗尽转 failed 保
  容量告警；豁免身份显式 exempt。
- V1/V2/ZIP：V1 各分支 writer 启动前绑定；V2 创建事务内绑定；PUT chunk
  续租=绑定重发（过期不再 409）；受理前失败/取消/过期/失败路径全部
  「清理确认后按持有者释放」（`_upload_abandon_staging` /
  cleanup_part → cleanup_confirmed；maintain 过期与 KFB 确定性失败不再
  无条件释放）；确定性失败保留 staging 与容量待 DELETE 确认。
- 百度：建批事务内绑定（holder=baidu_batch）；闭班收口（consume/release）
  **并入终态 CAS 同事务**（消灭「终态已落、结算未发」崩溃窗口）。
- 转换：used_bytes 直更 SQL 删除，结算走 `add_used_bytes_locked`。

**拆除（D）**：TTL 回收任务持有（reclaim 加 holder_id IS NULL）+
cleanup_pending 反连接豁免；`record_cleanup_pending` 的重激活/补账/30 天
延期（只登记重试）；COS 重新准入换 rid 分支与 released=skipped 分支；
`_cleanup_staging_tree` 先释放后清理路径；slide_publish/ingestion_store
旧锁序注释；R10 场景 3 与 R8/R9 回归断言改写为「责任从未释放」目标
（各处注明被替代的状态机合同）。

**存量核账（E）**：`scripts/reconcile_upload_capacity.py`——默认只读
报告；`--apply` 幂等绑定一致项/终止异常项进清理编排；`--reattach` 按审计
字节建 origin='reconcile' 预约并原子绑定（不计每小时准入）；超额只如实
报告不调额度；mismatch 阻断人工核对（退出码 3）。生产执行另行批准。

**验收**：R11 两反例原样入仓转绿（`tests/test_slide_id_review_r11.py`）；
R10 场景 3 重写为确定性两胜者收敛断言（首轮结果独立捕获；续租先赢由
R11 以 Event 固定复跑）；新模型测试 16 例（test_capacity_lifecycle_model）；
COS 生命周期 10 例（invalid 处置/清理确认/重试/崩溃幂等/门禁/豁免）；
通道+并发 10 例（V1 abort 门/V2 维护门/百度闭班原子/转换原语/准入先赢/
取消×新准入×清理失败）；核账工具 5 例。全量门禁见提交信息
（唯一允许失败=admin 0.4.13 第三方未提交件）。

**运行边界（不变）**：COS capability 保持 off；本方案完成不自动开启；
生产审计/迁移/回滚按 runbook 独立门禁，部署晚间低峰窗口另行确认。
调用点清单与锁图：[upload-capacity-lifecycle-inventory-20260927.md](upload-capacity-lifecycle-inventory-20260927.md)。
