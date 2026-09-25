# P2 设计合同：权限关系、前端与 HistoPilot 读通道 ID 化

日期：2026-09-25。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §4 与 [P1 合同](slide-id-refactor-p1-contract-20260925.md)。
前置状态：P1 已提交（3a60933/e2df401/8a0447d/deaece4），统一门禁与回填已就位。本文固定 P2 的 API/关系/协商决策，供实现代理严格执行；偏差在 review 裁决。

## 1. 关系切换总原则（计划 §4.2 落地）

- **写**：新写入一律同时写 slide_id（权威）与原文本列（兼容快照）。rois/comments/change_log/run_grants/ai_session_principals/annotation_access_events/audit_events 全部如此。
- **读（活动查询）**：一律按 slide_id 过滤；`slide_id IS NULL` 的历史行 = unresolved——不展示在新资产下、不参与计数、不删除（仅审计保留）。
- **授权判定**：只认 slide_id（view grants 已在 P1 收口；share 成员已走 share_slides）。旧文本列永不参与授权。
- **历史文本不重写成猜测 ID**（R-08）：回填只来自 P1-B1 的可信映射；P2 不做新的猜测性回填。

## 2. API 字段约定

1. 请求：**新字段优先，旧字段显式 alias 解析**。端点接受 `slide_id`（新，权威）或 `slide`/`name`（旧，经 `resolve_legacy_alias` 进同一门禁）——两个独立字段，绝不在同一字段上依次尝试 ID/名（计划 §4.1）。两者同时出现且解析到不同资产 → 400 `slide_ref_conflict`。
2. 响应 DTO：切片对象恒定携带 `slide_id`、`display_name`、`original_filename`、`format_ext`；保留旧 `name`（=legacy_filename，兼容期）与 `alias`（=display_name 派生，R-02 过渡）。**绝不输出 storage_relpath**。
3. 标注/评论 payload：新增 `slide_id` 字段；旧 `slide` 文本字段保留为名称快照输出（annotation_access.py 白名单加 slide_id）。
4. 错误码沿用既有词表风格；新错误词：`slide_not_found`（404）、`slide_not_ready`（409/410 按端点现状）、`slide_ref_conflict`（400）。

## 3. 逐域切换清单（后端）

### 3.1 标注/评论/变更流
- `POST /api/annotation`：接受 slide_id（优先）或 slide（alias 解析）；rois 行写 slide_id + slide（快照）双列；权限判定按 ID。
- `GET /api/annotations?slide_id=`（新参数；旧 `?slide=` 走 alias）；`/api/annotations/changes`、`/api/share/rois` 同步双参。
- comments 写路径（add_comment 族）同双写；list_changes 按 slide_id 查（含 change_log.slide_id、annotation_access_events.slide_id）。
- `_bump_change_seq` 等内部写函数签名加 slide_id 形参（调用方全部显式传）。

### 3.2 项目
- `POST /api/project/create`、`POST /api/project/<pid>/slides`：接受 `slide_ids`（优先）或 `slides`（名数组，alias 解析）；project_slides 写 slide_id + 文本快照；唯一键按 (project_id, slide_id)——同名不同 ID 可并存。
- `DELETE /api/project/<pid>/slide/<name>` 保留；新增 `DELETE /api/project/<pid>/slides/<slide_id>`。
- `annotations_by_project`/`annotations_by_slide` 投影按 slide_id。
- V1 上传后自动入项目（app.py:11267）与转换/导入关联（`_associate_target_project` 族）本阶段仍按名（writer 在 P3/P4 才出 ID）——保留 alias 解析，但写入时双列。

### 3.3 run grants / AI session / 插件
- `_issue_run_grant`：run_grants 行写 slide_id + slide 快照；`_verify_run_grant`/`_plugin_slide_run_grant_gate` 校验按 slide_id（请求带 slide_id 时）；旧请求只带名 → alias 解析后仍校验 slide_id 匹配（防伪）。
- `upsert_ai_session_principal` 双写；`_internal_ai_read_subject` 读路径兼容。
- 插件桥读端点（regions/changes/annotations/slide_info）：接受 slide_id 路径参数新路由或 body 字段（见 §4）。

### 3.4 Demo / 研究 / 审计
- demo_catalog 已按 ID；`_demo_catalog_slide` 的 resolve_slide_filename 兜底分支删除（P1-B2 遗留项）：catalog 项必须 resolve_slide_id 命中且 ready，否则 404/409。
- research `slide_pseudonym`：新会话改从 slide_id 派生（`HMAC("slide-id:"+slide_id)`）；历史伪名不动（研究删除按真实 ID 验证——research_deletion 按 slide_id 关联，先核对 research_store.py:233 用法再改）。
- `record_audit` 新记录带 slide_id（detail 已有先例：slide.delete 双写）。

### 3.5 缓存键收口（R-15 全量）
- tile 缓存键、`_DEFAULT_FP_CACHE`、render 统计 scope（`_ctx_scope`）、render token 的 slide 绑定：全部改 slide_id 键（显示名修改不触发内容变化；删除按 ID 准确失效）。
- `_close_slide` 的双键 evict 收窄为 slide_id 单键（legacy 名键不再产生新条目后）。
- P1-B2 遗留的 `_visible_slide_names` N+1：列表查询收敛为「一次 ready 行查询 + 一次 share_slides/grants 集合查询 + 内存判定」（单 SQL 聚合或两次查询常量次，不再逐行）。

## 4. 读取端点全族 ID 化

- 新增 `/api/slides/<slide_id>/` 族：`info`（已有）+ `dzi`（路由形态 `/api/slides/<slide_id>.dzi` 或 `/api/slides/<slide_id>/dzi`——选后者，避免与 `.dzi` 后缀解析歧义）+ `tiles/<level>/<x>_<y>.jpeg` + `crop` + `thumbnail` + `region` + `annotations` + `changes`。全部 resolve_slide_id → authorize_read → descriptor 路径。
- 旧端点保留（legacy alias 进同一门禁），不删。
- 下载 Content-Disposition：original_filename/display_name 经既有安全编码（查 `_crop_download_name` 的编码助手复用），防 CRLF。
- share_server.py：`/s/<token>/api/slides/<slide_id>/...` 族（成员判定已在 P1-B2 切 ID）。

## 5. 前端（static/）

1. **身份切换**：`state.slide = {id, name, display_name, ...}`——`id`=slide_id 为唯一操作键；`openSlide(id)`；列表 key/DOM dataset 改 `data-slide-id`（保留 data-name 过渡用于显示搜索）；已选切片/项目成员/URL 全部 slide_id。`?slide=<slide_id>` URL 通道新建（与 /s/<token>、/login?next= 无语义冲突——前端路由参数，旧链接无此参数不受影响）。
2. **上传完成**：V1/V2/COS/转换轮询全部用响应的 slide_id 打开（P3 才由 writer 产出真实 slide_id——本阶段响应里 slide_id 已可随 legacy 行返回：上传完成响应补 slide_id 字段，后端从任务行/resolve 取）。**禁止**再用 `file.name`/`canonical_name`/`body.slide || file.name` 猜打开目标（过渡期若无 slide_id 字段才回落名，且注释标 P3 拆除）。
3. **localStorage 续传**：键改 `pt.upload.v3::<account>:<task_id>`，内容含 slide_id；文件名/大小仅帮助用户挑选；换账户不复用会话。旧 v2 键只读迁移一次后作废。COS jobs 键同理（job_id 已够，补 slide_id 字段位）。
4. **显示**：列表显示 display_name，title/辅助显示 original_filename；同名用日期等辅助区分。
5. **slide.opened 事件**：载荷 `{slide:{id, name, display_name, revision,...}}`，去重键 `id|revision`；host 桥 `slide.getCurrent` 返回 id+name；`viewer.navigate/highlight/applyRenderContext` 的 stale 比对改 id（兼容只传 name 的旧插件——按 name 解析当前 desc 比对，注释标退役条件）。
6. **能力协商**：`HP_APP_BOOTSTRAP.capabilities.slide_id_api`（服务端注入，恒 true 自本版本起）；前端检测到 false（旧后端）时回落 name 通道（双栈期逻辑，注释标退役）。
7. **tests/js**：按名断言的用例改按 ID（mock 响应补 slide_id 字段）；新增：同名两片并存列表/打开互不串片、改名后 ID 不变、续传键换账户不复用。

## 6. HistoPilot（独立仓，HEAD 26fd2a9）

1. `contract.ts`：SlideRef 联合保留；运行上下文全面切 `{kind:'slide-id', slideId}`；`legacyFilename()` 仅留显式旧分支（legacy-flask-adapter 专用）。`RunGrantRef` 加 `slide_id` 字段（PT 侧 §3.3 已双写）。
2. `agent-runner.ts`/`session-store.ts`：会话索引键改 slide_id（index.json 以 slide_id 为主键；旧记录按 name 的只读兼容——无法映射的视为历史冻结，不回绑新内容）。
3. `tools.ts`/`transform-context.ts`/`path-replay.ts`/`snapshot-attest.ts`：工具参数与写入目标用 slide_id；region LRU 键改 slide_id；revision 防替换语义保留（revision 来源已是内容指纹）。
4. `http-client.ts`：enc 已支持 ID；`verifyRunGrant/bindRunGrant` 的 slide 字段改 slide_id（配合 PT §3.3）。
5. `legacy-flask-adapter.ts`/`flask-client.ts`（/internal/ai/* loopback）：请求体加 `slide_id` 字段（PT 侧 internal 端点 §3.3 接受双字段；ID 优先）。
6. `integrations/pathtogether/ui/*`：slide.opened 载荷取 id；`/api/ai/run|continue` body 发 slide_id；localStorage 会话键 `hp.ai.sess.v3.<identity>.<slideId>`（v2 键冻结不迁移——旧会话不回绑新内容）；renderer 载荷用 id。
7. **跨仓契约测试**（`test:contract` project 新增文件）：真实起 PT flask app（PATHTOGETHER_PYTHON/PATHTOGETHER_REPO 环境），验证：以 slide_id 起跑 run → PT 授权按 ID → /internal/ai/* 回携 slide_id → region/annotate 落库 rois.slide_id；同名两资产不串；删除后 slide_id 会话失效。contract.ts 注释的 uuidv7 表述顺手修正为实际生成器（sld_+12 urlsafe）。
8. `npm run build && test:unit && test:integration && test:contract` 全绿。

## 7.  alias 停写与派生（R-02 收口）

- `set_slide_meta` 停写 alias 列（UPDATE 不再 SET alias；INSERT 时 alias 列留 ''）；所有读 alias 的出参改从 display_name 派生（`alias = display_name`）。
- `POST /api/slide/<name>/meta` 的 alias 入参映射 display_name（旧客户端兼容）；新 `PATCH /api/slides/<slide_id>` 接受 display_name/note/public。
- 冻结盘点：改动前先 `rg "alias"` 全量列出读写点对单（防漏改 UI 展示）。

## 8. 完成标准（计划 P2 原文 + 本合同）

- 两张同显示名切片能分别打开/标注/分享/关联项目；改名不串片。
- 两仓合同测试 + 真实浏览器验证通过（浏览器验证用 web-gui-tester 技能在 demo 环境执行——**只隔离测试数据，不放行生产新上传**）。
- 全量 pytest（--ignore pg_reap/e2e 口径）+ test:js + HistoPilot build/unit/integration/contract 全绿（已知无关失败例外同前）。
- 旧客户端（仅 name 字段）对历史资产仍兼容；新资产只能由支持 ID 的客户端操作。
