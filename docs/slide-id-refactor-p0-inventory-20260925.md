# P0 基线记录与依赖盘点（slide ID 化重构）

日期：2026-09-25。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md)第 6 节 P0 阶段。
本文是实施过程的冻结记录；盘点结果由只读 explore 代理按当前 HEAD 逐点核实（行号为 HEAD 实测值，执行时复核）。

## 1. 基线冻结

| 项 | 值 |
|---|---|
| PathTogether HEAD | `a2b0ae302260346e3f3d834bc23cd42ecfd91046`（COS 直传第五轮 review 修复） |
| PathTogether 工作分支 | `slide-id-refactor`（自 a2b0ae3 切出） |
| HistoPilot HEAD | `26fd2a9`（附件冻结管线 + 跨语言渲染上下文指纹对齐） |
| HistoPilot 工作区 | 干净 |
| 下一个空闲迁移号 | 0067 |
| 基线回归 | 计划基础回归集 167 passed（17.5s）；`npm run test:js` 553 passed（5.3s）；全绿 |
| 历史 ID 生成器核对 | 现行生成器是 `sld_` + `secrets.token_urlsafe(9)`（12 位 urlsafe，`share_store_pg.py:1834`），**不是** HistoPilot `contract.ts` 注释提到的 uuidv7。裁决：不为统一格式重置旧 ID，新资产继续用现行 `sld_` 生成器（满足"随机、服务端生成、DB 主键唯一"），HistoPilot 侧注释在 P2 修正 |

### 1.1 他人未提交改动（不带入本重构任何提交）

以下文件在基线工作区已被他人修改或为未跟踪文件，本重构的所有提交**不得**包含它们，也不得在实现中依赖其未提交行为：

- `plugins/pathtogether-admin/manifest.json`
- `plugins/pathtogether-admin/ui/index.html`
- `plugins/pathtogether-admin/ui/main.js`
- `plugins/source-policy.json`
- `registration_mail_worker.py`
- `templates/_login_dialog.html`
- `templates/verify_email.html`
- `tests/js/admin-plugin-ui.test.ts`
- `tests/test_admin_plugin.py`
- `tests/test_email_verify_activation.py`
- `tests/test_public_registration.py`
- `deploy/histopilot-cn/`（未跟踪目录）
- `docs/slide-id-storage-refactor-agent-plan-20260925.md`、`docs/slide-storage-migration-audit-runbook-20260925.md`（任务书与手册，随首个 P0 提交入库）

与 admin 插件 UI 的协调点（计划 1.2 节"管理/维护"行）：实现涉及 `plugins/pathtogether-admin` 时只在其当前 HEAD 版本上叠加，若必须触碰上述已修改文件，先与用户确认。

### 1.2 提交纪律

- 每阶段（P0/P1/…）独立可审查提交；提交只 `git add` 本阶段实际拥有的文件，禁止 `git add -A` / `git commit -a`。
- 复现用例原样入仓为回归（不改断言），修复 + 全量回归 + evidence 后才进下一门禁。
- 本重构不开启 COS capability、不做生产部署、不做生产数据搬迁。

## 2. 依赖盘点

### 2.1 上传/读取/删除路径（app.py、upload_task_store.py）

#### 2.1.1 本地读取链（全部按 filename 定位，需改 ID resolver）

| 锚点 | 函数/路由 | 行为 |
|---|---|---|
| `app.py:1829` | `_safe_name` | `_sanitize_name` 后拼 `UPLOAD_DIR / safe` 并 `is_file()` 校验，不存在 404；所有读取入口的统一"文件名=资产"闸门 |
| `app.py:1840` | `_get_slide` | `slide_cache.get_slide(safe, UPLOAD_DIR / safe)`，句柄按 filename 缓存 |
| `app.py:1960` | `_slide_info_dict` | 拼路径 stat + `_read_metadata` + alias/note 合并 |
| `app.py:7080` | `_visible_slide_names` | `UPLOAD_DIR.iterdir()` 目录扫描出候选，再按 owner/public/claim/grant 过滤；**目录扫描当切片**的核心点 |
| `app.py:10253` | `api_slides`（`/api/slides`） | 再次 `iterdir()` ∩ 可见集；列表来自磁盘而非 DB |
| `app.py:6915/6975/6881` | `can_view_slide` / `can_delete_slide` / `_slide_owner` | 全部按 filename 查 `get_slide_meta_full` 判权 |
| `app.py:12553` | `api_slide_info`（`/api/slide/<name>/info`） | 试开切片读元数据 |
| `app.py:12577` | `api_slide_render_context` | 渲染上下文规范化 |
| `app.py:12591` | `api_slide_dzi`（`/api/slide/<name>.dzi`） | DZI XML，URL 回拼 filename |
| `app.py:12656` | `api_slide_tile`（`..._files/<level>/<x>_<y>.jpeg`） | 瓦片；render token 绑 `_legacy_slide_revision(safe)` |
| `app.py:12812` | `api_slide_crop` | 裁剪 PNG；`_crop_download_name`(1783) 用 filename 拼下载名 |
| `app.py:12933` | `api_slide_thumbnail` | 缩略图 |
| `app.py:16218` | `api_slide_region`（`/api/slide/<name>/region`） | level-0 区域图 |
| `app.py:17517` | `internal_ai_slide_info`（`/internal/ai/slide_info`） | `_get_slide` + 拼路径(17492) |
| `app.py:17114` | `internal_ai_region`（`/internal/ai/region`） | 同上(17181) |
| `app.py:16578` | `_ai_slide_ctx` | AI 上下文拼路径(16595) |
| `app.py:16637/16645` | `_slide_fingerprint` / `_legacy_slide_revision` | mtime:size 指纹/revision（AI image_ref 防伪、render token rev、快照 attestation、demo run 预约 `app.py:2510/6131/6333`）；ID 化后须换 rev 来源 |
| `app.py:17556` | `_plugin_resolve_slide` | 插件路由共享的 `(UPLOAD_DIR / safe).is_file()` 检查 |
| `app.py:17746/17831` | `plugin_v1_slide_info` / `plugin_v1_region` | 路径参数注释明言仍是 legacy filename |
| `app.py:5981-6212` | Demo 读通道（`api_demo_slides/info/dzi/tile`） | 对外用 slide_id，但经 `_demo_catalog_slide`(5777) `demo_store.resolve_slide_filename` 回落 filename 再 `_safe_name/_get_slide` |
| `share_server.py:161/338/1024` | 分享进程独立 `_get_slide` 与 `/s/<token>/api/slide/<name>/*` 全套路由 | **app 之外第二个按名读文件进程**；`_require_slide`(761) 按 shares.slides 名数组鉴权 |

目录扫描（把磁盘文件当切片）全部位置：`app.py:7094`（`_visible_slide_names`）、`app.py:9784`（admin inventory）、`app.py:10262`（`api_slides`）。zip 暂存内的 iterdir/rglob（10509/10641/10658）是解包识别，不属资产枚举。

#### 2.1.2 V1 上传（`/api/upload`）

| 锚点 | 函数 | 行为 |
|---|---|---|
| `app.py:11090` | `api_upload` | 流式上传：`_sanitize_name`→`dest = UPLOAD_DIR / safe`(11144)→同名冲突 409(11148)→`.uploading-*.part`→验证→intent→promote→`set_slide_meta`(11251)→`finish_commit`；上传后 `add_slides_to_project(target_pid, [safe])`(11267) |
| `app.py:11148` | 同名冲突判定 | `dest.exists() or _upload_name_conflict(safe)` → 409 name_unavailable；`allow_kfb_recover`(11147) 按同名+同 SHA 恢复原 conversion 任务（11200 `_owned_committed_upload`）——**同名/同内容认领补丁** |
| `app.py:11322` | `_api_upload_zip` | zip 分支：`_prepare_zip_bundle`→`_upload_legacy_intent`→`_promote_zip_bundle`→逐切片 `set_slide_meta`(11383)→`finish_commit` |
| `app.py:10354` | `_prepare_zip_bundle` | 解压到 `UPLOAD_DIR/.extracting-*`(10383)、zip-slip/炸弹防护、目标冲突预检 `(UPLOAD_DIR / rel).exists()`(10527)、配额 top-up(10531) |
| `app.py:10582` | `_promote_zip_bundle` | `os.link` no-clobber 提升到 `UPLOAD_DIR / rel`(10607) |
| `app.py:10628` | `_recognize_slide_bundle` | 识别切片+同 stem 伴侣目录 |
| `app.py:10893` | `_upload_legacy_intent` | `upload_task_store.begin_legacy_commit`(10899) 持久化 manifest（键=filename） |
| `app.py:10732/10740/10806/10863` | V1 崩溃恢复族 | 按 manifest `a["name"]` 拼 `UPLOAD_DIR` 判 promoted/absent/conflict；恢复时 `set_slide_meta(a["name"], owner=…)`(10840) 补归属 |
| `app.py:10769/10795` | `_upload_legacy_fail` / `_upload_legacy_remove_artifacts` | 失败按 manifest name unlink |
| `upload_task_store.py:407` | `begin_legacy_commit` | V1 直入 committing：一次 INSERT 任务 + commit_token + `v1_artifacts` manifest |
| `app.py:324` | `_upload_name_conflict` | 磁盘存在 ∨ `conversion_store.canonical_is_live` ∨ canonical 名占用；调用点：`app.py:11148`（V1）、`app.py:11791`（V2 create）、`app.py:12306`（COS create） |
| `app.py:339/375/361/421` | 转换辅助族（`_enqueue_conversion`/`_ensure_conversion_job`/`_owned_committed_upload`/`_cleanup_conversion_sidecars`）+ `_canonical_name_for`(277) | 源/canonical 均按文件名定位 |

#### 2.1.3 V2 上传（`/api/uploads`）

| 锚点 | 函数 | 行为 |
|---|---|---|
| `app.py:11745` | `api_uploads_create` | 建 active 任务；预检 `(UPLOAD_DIR / safe).exists() or _upload_name_conflict(safe)`(11791)→409；`reserve_upload(declared_size)`(11795)；`create_task(owner, filename, safe_name,…)`(11800)——**任务目标=safe_name，未绑 slide_id** |
| `app.py:11822` | `api_uploads_status` | `_upload_v2_fetch`→`_upload_v2_maintain`→进度快照 |
| `app.py:11837` | `api_uploads_put_chunk` | sidecar flock 串行 offset；pwrite 到 `UPLOAD_DIR/.uploading-<upload_id>.part`(11443) |
| `app.py:11993` | `api_uploads_commit` | 三段式：`begin_commit`(12025)→复算 SHA/大小/格式校验→`_promote_no_clobber(part, dest=UPLOAD_DIR/task["safe_name"])`(12036/12102)→`_upload_v2_set_ownership`(12111)→`finish_commit(settle_bytes)`(12122)；canonical 冲突再查 12082 |
| `app.py:12149` | `api_uploads_cancel` | `cancel_task` + 清 part + 释放 reservation |
| `app.py:11637` | `_upload_v2_maintain` | 惰性维护：TTL 过期→expire；committing 超时→V1/V2 恢复；**committed 但缺 owner 时 `set_slide_meta` 校正(11659-11661)——读路径隐式补写**；恢复判定按 `UPLOAD_DIR / task["safe_name"]`(11590) 存在+大小 |
| `app.py:11468` | `_promote_no_clobber` | hard-link/copy-link 原子提升 |
| `app.py:11575` | `_upload_v2_set_ownership` | `share_store.set_slide_meta(task["safe_name"], owner=…)` |
| `upload_task_store.py` | `create_task`:376、`begin_legacy_commit`:407、`list_tasks`:453、`get_task`:480、`append_chunk`:493、`begin_commit`:519、`finish_commit`:549（`_pg_finish_commit`:564 内 `consume_reservation_locked` 同事务）、`fail_commit`:598、`rollback_committing`:632、`cancel_task`:654、`expire_task`:675 | 表 `upload_tasks` 键=upload_id；**无 slide_id 列**（`_TASK_FIELDS`:252），`safe_name` 是唯一资产引用；无 (owner, safe_name) 唯一约束 |

#### 2.1.4 删除/取消/reaper

| 锚点 | 函数/路由 | 行为 |
|---|---|---|
| `app.py:12493` | `api_slide_delete`（`DELETE /api/slide/<name>`） | `get_slide_id(safe)`→撤 view grants(12514，name+slide_id)→`_revoke_demo_slide`(12521)→unlink(12527)→`_cleanup_conversion_sidecars`(12531)→MRXS 伴侣目录 rmtree(12538)。**删除不回退 used_bytes**（0013 口径，见 2.5） |
| `app.py:12366` | `api_ingestion_cancel` | 取消 COS 任务；CommitInProgress→409 |
| `app.py:11070` | `api_baidu_import_cancel` | 代理取消远端导入 |
| reaper 现状 | 无独立本地孤儿 reaper | ① `upload_guard.reserve_upload_locked`(299-316) 惰性回收过期 reservation（不动文件、不结算）；② committing 任务靠请求路径惰性恢复；③ admin overview 只观测。本地 UPLOAD_DIR 清理全部挂在具体任务上；**`objects/` 化后的"按 ID 清理"入口不存在，需新建** |

#### 2.1.5 `force_slide_owner_follow_file`（拆除对象）

- 定义：`share_store_pg.py:3105`；导出：`share_store.py:122`。
- 唯一调用点：`cos_ingest_worker.py:880`（配合 `_stat_ident` 的 `adopted_existing/still_ours/promoted_ident` 分支 864-896）。app.py 与 upload_task_store.py 内无调用。

#### 2.1.6 配额与预约（上传路径）

`upload_guard.py`：`reserve_upload`:355（`reserve_upload_locked`:270，含惰性过期回收 299-316）、`topup_reservation`:373、`renew_reservation`:470、`release_reservation`:520、`consume_reservation_locked`:552（**used_bytes 唯一同事务入账点**）、`add_used_bytes`:604（转换产出直入账）。app.py 侧：V1 `_upload_acquire_reservation`:10679；V2 `_upload_acquire_reservation_exact`:11717；COS 准入经 `ingestion_store._try_admit_txn`。

app.py 对 slides 表**无直接 SQL**；所有读写经 `share_store`。`set_slide_meta` 调用点：`app.py:7698、10840、11251、11383、11577、20470`；`get_slide_id`：`app.py:7695/9897/12508`。

### 2.2 元数据入口与引用关系

#### 2.2.1 share_store_pg.py 关键函数

- `share_store.py` 是纯 re-export 垫片（`STORAGE_BACKEND` 仅接受 postgres）。
- `_new_slide_id`:1834（sld_+12 urlsafe）；`set_slide_meta`:1838（**按名懒建行**；owner 仅 NULL 时回填）；`get_slide_id`:1977；`resolve_slide_ref`:1991（**sld_ 前缀直返，无存在性/状态检查；当前无生产调用方**，仅导出+测试）；`record_slide_asset`:2000（生产无 writer，slide_assets 运行时是只进不出的孤岛；content_sha256 无写入方）。
- `set_slide_meta` 全部写调用点：`app.py:7698`（demo catalog PUT 隐式建行、无 owner 参数）、`app.py:10840`（V1 恢复补归属）、`app.py:11251`（V1 commit）、`app.py:11383`（V1 ZIP）、`app.py:11577`（V2 归属）、`app.py:11655-11664`（V2 committed 读路径校正）、`app.py:20470`（`POST /api/slide/<name>/meta`，**无文件存在性检查，可为任意名建行**）、`cos_ingest_worker.py:848`、`baidu_ingest.py:344`、`scripts/import_slides.py:122`、`scripts/seed_demo_tcga_catalog.py:85`、`scripts/migrate_json_to_pg.py:411`（`_det_slide_id(name)` 从名字确定性推导 slide_id，退役清单候选）。

#### 2.2.2 分享与领取

- `shares.slides` JSONB 名数组：写 `create_share`:352；鉴权 `share_server.py:761 _require_slide`、`add_roi`:791；展开 `claimed_active_slides_for_user`:513。路由：`app.py:19918` create（逐名 `_sanitize_name`+扩展名+**磁盘存在**校验 `_validate_slide_names`:20132、`can_manage_share`:7071）、`20026` list、`20043` revoke、`20070` share/rois、`20101` claim。
- `grants`：token 级领取（`claim_share`:448），无 slide 维度；可见性消费 `app.py:6940/7002`（经 `_claimed_slides`:6899）。
- `slide_view_grants`（0034/0035）：PK `(slide_name, user_id)`；`slide_id` 列 = 资产生代绑定（`IS NOT DISTINCT FROM` 校验，`slide_view_grants_for_user`:702-724）；孤儿授权（无 slides 行、双方 NULL）照常生效；`grant_slide_view`:596 ON CONFLICT 重绑当前代；`revoke_slide_view_grants_for_slide`:671。路由：`app.py:9864` admin visibility（grant:9897 带当前 slide_id；revoke 联动 `_cancel_sidecar_runs_for_owners`:17695 + `_revoke_stale_run_grants`:17683）；`app.py:12514` 删除联动。

#### 2.2.3 项目/标注/变更流

- `project_slides.slide`（legacy 名，无 FK，无 (project_id, slide) 唯一约束，应用层 `_dedupe`:2022）：写 `create_project`:2056、`update_project`:2135（整删重插）、`add_slides_to_project`:2154、`remove_slide_from_project`:2182、`project_idempotency_store.py:257`；读 `archived_slide_names`:2616、`annotations_by_project`:2281。路由：`app.py:20157/20258/20274`。
- `rois.slide`：写 `_insert_roi`:193（入口 `add_roi`:761、internal AI `app.py:17371`、插件 `app.py:18276`）；读 `list_changes`:1359、`annotations_by_slide`:2220、`list_shared_rois_for_slides`:1800。路由：`app.py:20357/20481/20070`。
- `comments.slide`：写 `_insert_comment`:2304；读 `list_changes`:1363、`list_comments`:2368。路由：`app.py:20943/20955`。
- `change_log.slide`：写 `_bump_change_seq`:318、`_record_access_event_tx`:1511（同写 annotation_access_events）；读 `current_change_seq`:1430。对外：`app.py:20402/18081/17442`。
- `annotation_access.py:340/370/402`：ROI/comment/access event 字段白名单均含 `"slide"` 名直出（DTO 契约需补 ID 字段语义）。

#### 2.2.4 AI/插件/Demo

- `run_grants.slide`（名）：写 `create_run_grant`:2813（发放点 `app.py:14006 _issue_run_grant`）；校验 `_verify_run_grant`/`_plugin_slide_run_grant_gate` app.py:17785-17832（含 `_run_grant_creator_allowed`:17588 实时复查）；撤销 `app.py:17664/17683/18326/19444`；`billing_store.py:701` usage 主体冲突检测按 session 读（不读 slide）。
- `ai_session_principals.slide`（名，可空）：写 `upsert`:1294（调用点 `app.py:14710/17468`）；读 `app.py:17413`。
- `/internal/ai/*`：`app.py:17113` region、`17222` annotate、`17442` spots、`17516` slide_info（`_ai_slide_info_payload`:17482 带 `_legacy_slide_revision` 作 asset_revision）——全部按名。
- 插件桥：`app.py:17745/17830/18081/18111`；`_plugin_resolve_slide`:17554；`_slide_in_demo_catalog`:17766（slide_id→legacy 名逐一反查比对）。
- `demo_catalog` 已按 slide_id（demo_store.catalog_add:905 校验存在、resolve_slide_filename:1009 是 id→filename 反查枢纽）；`demo_runs.slide_id` + `asset_revision`=legacy mtime:size（demo_store.reserve_run:445，app.py:6333）。admin 写路由 `app.py:7676`（按名建行）/`7737`（删除，名或 id）。
- `baidu_import_store.py:1253`（重放按名验证）、`baidu_import_items.slide_name`、`baidu_ingest.py:1266`（ingest_token=`"slide:"+name`）。

#### 2.2.5 研究/审计

- `research_store.py:233` `slide_pseudonym` = HMAC("slide:"+legacy_name)——**ID 化后伪名输入源必须改**（否则同资产换名产生不同研究伪名）；`create_viewing_session`:521 落库。
- `research_deletion_worker.py:150-200` 仅 DELETE research_* 表，不触 slides/rois/文件。
- `record_audit`：多点按 safe 名记录；`slide.delete`:12551 detail 同时带 slide_id（罕见双写点）。
- `site_visit_events`：只存 path 模板（`/demo/<slide>` 模板值为 slide_id），不存文件名，无需改绑。

#### 2.2.6 缓存/sidecar

- `slide_cache.py:209 get_slide(name, path)` 句柄池以名为键（`share_server.py:161` 同型独立实例）；`app.py:1851 _close_slide`。
- 瓦片缓存键 `(safe, generation, fingerprint, level, x, y, format, quality)`（app.py:1645）；`_tile_cache_purge`:1625 按键首段=名匹配。
- 渲染统计缓存 `slide_render.purge_stats_for`（scope=`<safe>#<generation>`，`_ctx_scope`:1671）。
- manifest/associated：`_cleanup_conversion_sidecars`:421（删 `<canonical>.manifest.json`、`<canonical>.associated/`、按 job 源名清 KFB、作废 conversion_jobs）——键=conversion canonical 名（`conversion_store.canonical_name` 唯一锁:237-251）。
- 内容 revision 现算 `_legacy_slide_revision`（mtime:size），不经 DB。

### 2.3 后台 writer 与管理入口

#### 2.3.1 COS 直传（全部待拆除点集中在 `cos_ingest_worker.py:746 process_validating`）

- `:764-782` `adopted_existing`——intent 崩溃恢复时 dest 已存在且大小相符，流式比对 sha256 后"采纳"该文件（跨任务同名认领）。
- `:786/841` `promoted_ident = _stat_ident(dest)`（定义 `:125`）——记录本任务提升的 (dev,ino) 作归属守卫基准。
- `:876-891` `still_ours` 判定——dest 仍是本任务提升的那份才动元数据，否则"留在他人路径"仅记日志。
- `:880` `force_slide_owner_follow_file`（见 2.1.5）。
- `:848-851` `set_slide_meta(safe_name, owner)` 归属登记+终检（853-864）。
- `:778/789/845/893` `name_unavailable` 失败族——同名冲突按名称占用拒绝。
- `:831 worker_persist_commit_intent`（intent 落库，target=safe_name）；`:897 worker_settle_ready` 结算。
- `process_ready`:930 readiness probe 按 `UPLOAD_DIR/<slide_canonical_name>` 打开（940）。
- `_promote_no_clobber`:202（os.link 同卷/copy-link 跨卷）；`process_preparing`:297（`make_object_key`:159 key=`incoming/<owner>/<job_id>/<rand>`）；`process_completing`:416；`process_downloading`:607；`process_cleanup`:993（key 必须 `incoming/` 前缀）；`scheduler_tick`:261；`reconcile_tick`:1136/`_mark_orphans`:1095（未知 key 只告警不删）。
- `ingestion_store.py`：`create_waiting_job`:206（幂等键 (owner,idempotency_key)，**无 slide_id 字段**）；`try_admit_job`/`_try_admit_txn`:274/312（本地配额+池预约同事务）；`worker_persist_commit_intent`:700；`worker_settle_ready`:731（validating→ready 同事务 consume + 写 slide_canonical_name/sha256_actual）；`cancel_job`:917；`fail_job`:967（**释放预约但未结算 used_bytes 的缺口路径**）；`sweep_expired_*`:997/1017；`renew_active_local_reservations`:1061；`admit_waiting_fifo`:1138；`claim_cleanup_job`/`finalize_cleanup`/`record_cleanup_failure`:1174/1211/1247（finalize 才释放池预约）。
- `cos_pool_store.py`：`ensure_pool_state`:37、`lock_pool`:81、`reserve_locked`:90、`release_locked`:107、`admission_paused`:115、`record_observation`:125。
- `cos_client.py`：远端薄适配（initiate/presign/list_parts/complete/abort/head/get/delete_version/list_versions/list_multipart）——保留项，不触本地身份。

#### 2.3.2 转换

- `conversion_worker.py:141 process_job`：source=`UPLOAD_DIR/<source_name>`、canonical=job.canonical_name 或 `splitext(source)+'.tif'`(143)、work=`dest+'.work-<job_id>'`(76)。`:157-167` dest 已存在三分支（manifest sha 匹配→复用；同 inode→补提升；否则 name_unavailable）——**转换侧同名/同 inode 认领**。`:177 set_slide_meta(canonical, owner)`；`:181 complete_job`（结算）；`:185 _associate_target_project`（`add_slides_to_project` 按名）。
- `_promote_work`:95（hardlink work→dest、写 manifest、rename associated）；`_retract_ours`:207（只撤同 inode 本任务 dest）；`_discard_work`:80。
- `conversion_store.py`：`create_job`:84（owner+sha+converter 幂等；**canonical_name UNIQUE 即 canonical 名唯一锁**，NameConflict:141——拆除对象）；`canonical_is_live`:237/`invalidate_by_canonical`:254/`get_job_by_canonical`:273；`complete_job`:390（`canonical_settled_bytes` 幂等结算 used_bytes:409-418）；`claim_one`:308/`heartbeat`:335/`fail_job`:384/`retry_job`:530；`conversion_job_sources`（0048）多源别名。

#### 2.3.3 远程导入（百度）

- `scripts/baidu_import_worker.py:48 drain_once`；staging 在 `$TMPDIR/baidu-import-staging`（与 UPLOAD_DIR 分离）。
- `baidu_import_store.py`：`create_import`:719（quota_hook 预占）；`_phase_transfer`:1108/`_phase_download`:1150；**`_reconcile_ingest`:1209——按名对账/隐式认领点**：native 分支 1244-1259 查 `UPLOAD_DIR/<item.name>` 在盘+meta owner 匹配+sha 一致→直接置 ready（COS adopted_existing 的百度版，owner 不匹配不认领）；`_phase_ingest`:1273（ingest_token 幂等）；`_finalize_batch`:1625；`_cleanup_copies`:1700/`retry_cleanup`:1765。
- `baidu_ingest.py`：`ingest_staging`:299（native 入库：`_copy_new`:336 O_EXCL 独占复制→`_probe_native`:343→`set_slide_meta`:344；`:333` 同名冲突检查 `source_dest.exists() or canonical_is_live(name)`）；`_ingest_convert`:209（`canonical_name_for`:224、名占用四重检查 226-229、`_finish_ready_job`:191 复用 ready 任务原 canonical 名）；`associate_slide`:99（按名入项目）。
- `baidu_import_http.py`：纯装配层；`_quota_hook`:26 走 `upload_guard.reserve_upload`。

#### 2.3.4 CLI 导入

- `scripts/import_slides.py:76 import_one`：`dest = upload_dir/<safe>`(93)；dest.exists()→拒(94)；**在原外部源上原地校验**(109)；`app._promote_no_clobber(src, dest)`(115)——**同卷 os.link 硬链接，与外部可写源共享 inode**（计划 §2.2 明令禁止的形态）；`set_slide_meta`:122；`--move` 成功后 unlink 源(131)。owner 显式参数 `resolve_owner`:53（空→平台 owner 回落）。ZIP/MRXS 拒收(88)。

#### 2.3.5 管理/维护

- `app.py:9765 admin_v1_slides_inventory`：`iterdir()` 扫描(9784) 生成磁盘清单与 meta 拼归属（只读，但"磁盘文件=库存"口径要改成 DB 资产列表+差异报告）。
- `app.py:9865 admin_v1_slide_visibility`：按名授权/收回 view grant。
- `app.py:7675 api_admin_demo_catalog_put`：`:7698 set_slide_meta(slide)` 无 owner 参数隐式建行。
- `plugins/pathtogether-admin`：`ui/main.js:3196-3271` 可见性页经 AdminBridge 映射 inventory/visibility 接口，操作键=文件名（插件无自有后端写逻辑）。
- 其它按名建行工具：`scripts/seed_demo_tcga_catalog.py:85`、`scripts/migrate_json_to_pg.py:411`。
- `format_request_http.py:67 _save_sample` 写 `FORMAT_REQUEST_DIR`（非切片资产，路径规范可一并约束）。

#### 2.3.6 目录扫描→隐式认领/建行位置汇总

1. `app.py:7093 _visible_slide_names`；2. `app.py:9784` admin inventory；3. `app.py:10262 api_slides`；4. `app.py:7698` demo catalog PUT；5. `app.py:11655` V2 committed meta 校正；6. `app.py:10840` V1 恢复补归属；7. `scripts/seed_demo_tcga_catalog.py:85`；8. `baidu_import_store.py:1244-1259 _reconcile_ingest` native 分支。

#### 2.3.7 暂存命名点（全部平铺在 UPLOAD_DIR 根，需换 `.staging/<task_id>/<generation>/`）

`.extracting-*`（zip 解包 `app.py:10383`）、`.uploading-*`（V1 单文件 11153）、`.part-<upload_id>`（V2 11443）、`<job_id>.part`（COS `cos_ingest_worker.py:120`）。

### 2.4 前端与 HistoPilot 通道

#### 2.4.1 PathTogether 前端（static/）

- 读通道主键：`openSlide(name)` `app.js:1485`；`state.slide = {name: info.name, ...}`:1496；`refreshInfo()` 按 name:1469；**已有 id 优先点**：`channelCtrl.handleInfo(info, {id: info.slide_id || info.name})`:1535、`channel-controls.js:97/163`（tile/thumbnail URL 与配色 storageKey 已 id 优先回退 name）。
- 列表：`loadAll()` `fetch("/api/slides")` 以 `.name` 为主键消费:2337/2355；`it.dataset.name === name` 高亮:1552；`findSlideInfo/renderUnfiled/annoBadgeText` 按 name:2536/2730。
- 事件/桥：`emitSlideOpened()` 载荷 `{slide:{name,...,revision}}` 去重键 `name|revision`:1272；host 桥 `slide.getCurrent` 返回 name:8921；`viewer.navigate/highlight/applyRenderContext` 按 name 做 stale_slide 拒绝:8950/8969/8991；`attachIntent` 冻结 `slide: state.slide.name`:9324/9343；研究遥测 `slide: state.slide.name`:1185。
- 上传完成回调（全部按名猜打开目标）：V2 commit `body.canonical_name ? ... : file.name`→openSlide:6370；conversion 轮询 `res.body.canonical_name || canonical`:6410-6465；V1 `data.canonical_name : data.name`:6529；COS `b.slide || file.name`:7009；`importAssociateUploaded(openName)` 按名入项目:7297。
- localStorage：`pt.upload.v2::<name>:<size>:<lastModified>`（内容 `{upload_id, declared_size, chunk_size}`，无账户域无 slide_id）:6216/6344；`pt.cos.jobs` 按 filename+size 匹配续传:6569/6653。
- 操作标识：项目 create/add `slides:[names]`:2936/3107；share create `slides:[names]`:3176；`POST /api/slide/<name>/meta`:2703；`DELETE /api/slide/<name>`:6072；`POST /api/annotation {slide:<name>}`:4524/9110；admin demo-catalog `slide=<name>`:2650；crop `/api/slide/<name>/crop`:2268。
- `share.js` 整页按名（110/318/384/847/892）；`demo.js` 已是 slide_id（441/461，对照样板）；`admin-host.js:816` `admin.slides.setVisibility` path 段=name。
- 现状**无 `?slide=` URL 通道**（4.1 的 URL 定位是新建）；templates 无内嵌 name 表单，迁移面全在 JS。
- tests/js 按名断言 14 个文件（upload-v2/cos-upload/slide-row-layout/slide-opened-channel-reopen/share-write/toolbar-draw-roi/toolbar-account-upgrade/research-viewer-telemetry/conversation-attach-intent/viewer-get-viewport/plugin-sdk-bridge/api-fetch-401 等）；tests/e2e 5 个 spec 按名。

#### 2.4.2 HistoPilot（HEAD 26fd2a9）

- `src/platform/contract.ts`：`LegacySlideRef{kind:"legacy-filename"}`:50、`SlideIdRef{kind:"slide-id"}`:60（联合已就位）；`legacyFilename()`:74 对 slide-id 抛 ContractError；`RoiDict.slide`/`RunGrantRef.slide` 是 name 裸字符串:511/556；`RegionRequest/CreateAnnotationRequest/EventStreamRequest.slide: SlideRef`:375/602/816。
- `legacy-flask-adapter.ts`：slideInfo:138、region:156（body.slide=filename）、spots:212、annotate:224——全 name。
- `http-client.ts`：`enc(ref)`:920 ID 可进 path（序列化能力已具备）；slideInfo:328、region:350、spots:473、annotate:488；`verifyRunGrant/bindRunGrant` body `{grant_id, slide}` slide 是裸 name:555/594。
- `flask-client.ts`（loopback `/internal/ai/*`）：region:241、annotate:282、spots:304、slideInfo:313——全 name。
- `agent-runner.ts`：run/fork/branch/会话索引全按 slide name（384/853-1160）；`cancel({sessionId?|slide})` 按 name 反解:1503；run-grant fail-closed `config.run_grant.slide` 是 name:1554。
- `tools.ts`：`ToolContext.slide: string`:331；快照 region `legacySlide(ctx.slide)`+expectedAssetRevision:963；annotate:1781/1989；远程 dispatch body `{slide: ctx.slide}`:2565。
- `path-replay.ts`：waypoint 不含 slide 标识，仅 snapshot_id+slide_revision(mtime:size):44-80。`snapshot-attest.ts:37/73` 同绑 revision。
- `transform-context.ts`：SlideSpec/LRU 键含 slide name:348/463。
- `session-store.ts:2592` index.json 以 slide name 为主键。
- `integrations/pathtogether/ui/*`：slide.opened 写 `S.slide={name,...}`:21；`/api/ai/run|continue` body `{slide: slideName}`:191/213/262/332；localStorage `hp.ai.sess.v2.<identity>.<slideName>`:104、draftKey:126；renderer navigate/highlight/setOverlay 载荷 name:214/235/374；`slide_revision` 防替换:62。
- 能力协商现状：bridge major 版本协商（`static/bridge-version.js:53`）、`HP_APP_BOOTSTRAP.capabilities` 下发（`app-mode.js:14`，样板）、channel-controls per-slide 能力字段、http-client Accept 协商、ContractErrorCode 词表——**没有 slide-id 模式协商，需新建**。
- 测试：`test/platform-contract.test.ts`（SlideRef 行为）在 unit project；contract project 现仅 4 个跨仓文件，无 slide-id 契约测试。脚本：build/test:unit/test:integration/test:contract。

### 2.5 当前 schema 与账本语义（P1 设计依据）

#### 2.5.1 核心表（HEAD 终态，0067 的底座）

- **slides**（0001 建，无后续 ALTER）：`slide_id` PK、`legacy_filename` UNIQUE 可空、`display_name` NOT NULL DEFAULT ''（**零读写死列**）、`alias` NOT NULL DEFAULT ''、`note`、`owner_user_id` 可空（无索引）、`public` DEFAULT FALSE、`roi_sizes` JSONB、created/updated_at。无 original_filename/storage_*/format_ext/asset_state/published_at/deleted_at/accounted_bytes——全部 0067 新增。
- **slide_assets**（0001）：`asset_id` PK、`slide_id` FK CASCADE、`content_sha256`（无写入方）、`legacy_revision`（mtime:size 指纹）。
- **shares**：`token` PK、`slides` JSONB 名数组（**无 share_slides 表，拆表是全新 DDL**）、`permissions` JSONB、`roi_sizes`、`expires_at`、`revoked`、`creator_user_id`、`rect_policy`(0036)。
- **grants**：`id` PK、`token` FK CASCADE、`user_id`、`permissions`、`claimed_at`、`active`；无 slide 维度。
- **project_slides**：`project_id` FK CASCADE、`slide` 名、`position`；无 (project_id, slide) 唯一约束。
- **rois**：`id` PK、`token`、`slide` 名、`annotation_id` UNIQUE、`label/type/geom/size_mm/shared/note/visitor/owner_user_id/deleted/data JSONB/insert_seq BIGSERIAL/visibility_status(0056)/client_action_id`(0056 部分唯一索引)。
- **comments**（0003）：`comment_id` PK、`annotation_id`、`slide` 名 DEFAULT ''、`token`、`author_*`、`body`、`parent_id`、`resolved/deleted`、`data` JSONB。
- **change_log**：`seq` BIGSERIAL PK、`slide` 名、`token`、`annotation_id`、`op`（无 CHECK）、`at`。
- **run_grants**（0005）：`grant_id` PK、`installation_id`、`slide` 名、`session_id`、`created_by_user_id`、`expires_at`、`revoked`。
- **ai_session_principals**（0059）：`session_id` PK、`user_id`、`slide` 名可空。
- **demo_catalog**（0006/0057）：`slide_id` PK（应用层校验存在性）；`demo_sessions`/`demo_runs`(0026) 已含 slide_id + asset_revision。
- **slide_view_grants**（0034/0035）：PK `(slide_name, user_id)`、`granted_by/granted_at`、`slide_id` 可空（资产生代，`IS NOT DISTINCT FROM` 校验）。
- **audit_events**（0004）：`slide` 名可空上下文。`annotation_access_events`（0059）：`slide` 名。`annotation_grants`（0056）：按 annotation_id，无 slide 列。
- **upload_tasks**（0017/0021）：无 slide_id 列；`safe_name` 是唯一资产引用；状态机 active→committing→committed/failed/cancelled/expired；`v1_artifacts` JSON manifest。
- **ingestion_jobs**（0066）：无 slide_id 列；`safe_name`/`slide_canonical_name`；13 态状态机；部分唯一索引（每 owner 单 waiting、单 active、idempotency live）。
- **conversion_jobs**（0046-0048/0052）：`source_name`/`canonical_name`（live 唯一索引=canonical 名锁）、`canonical_settled_bytes` 幂等结算键、`target_project_id`/`project_associate_state`(0052)。
- **baidu_import_items**（0051/0052）：`slide_name` 可空（入库后可见名）、`ingest_token`、`quota_reservation_id`；批次表 UNIQUE(owner, idempotency_key)。

#### 2.5.2 配额/账本

- `upload_user_quotas`（0013）：user_id PK、`quota_bytes`、`used_bytes`、`reserved_bytes`。**0013 口径：删除切片不回退 used_bytes**——与计划 §3.3"删除按 accounted_bytes 幂等减少实占"存在张力；裁决见第 3 节 R-12。
- `upload_reservations`（0013）：state reserved/consumed/released；consume 幂等（已 consumed 再 consume 返回现状）；released/过期拒绝转实占（fail-closed）。
- `cos_pool_state`（0066）：单行池账本；pool 预约仅在 `finalize_cleanup` 释放。
- 一次结算保证：任务终转与 consume 同事务（upload_tasks `_pg_finish_commit`:564、ingestion `worker_settle_ready`:731）+ consume 幂等 + conversion `canonical_settled_bytes IS NULL` 幂等键 + quota 行 FOR UPDATE 串行化。
- **全仓锁序唯一口径**：`ingestion_jobs 行 → upload_reservations 行 → upload_user_quotas 行 → cos_pool_state 行`（0066/cos_pool_store 头注释）。新发布管线必须沿用同一顺序并审计无环。
- 迁移机制（pg_store.py）：`schema_migrations` 按 filename 登记、无内容哈希、每文件单事务、约定幂等；启动期 `app.py:669 _PG_SCHEMA_LOCK` 会话级 advisory lock 串行化；advisory lock 已有使用先例（ingestion `pg_advisory_xact_lock(hashtext(job_id))`:898 等）。

## 3. 历史引用裁决表（映射 / 隔离 / 仅保留审计）

每类引用一个裁决；"映射"= 迁移期绑定到已确认 slide_id；"冻结"= 保留原文仅作历史快照/兼容输出，不参与新授权；"隔离"= unresolved，拒绝访问不猜归属。

| # | 引用 | 裁决 |
|---|---|---|
| R-01 | `slides.legacy_filename` | **冻结别名**：新资产写 NULL；旧值永不重绑新 ID；仅经显式 legacy alias resolver 进入同一 ID 门禁 |
| R-02 | `slides.alias` vs `display_name` | **归并到 display_name**：alias 停写；回填规则 非空 alias → display_name，否则现有 display_name，否则 legacy_filename；过渡 API 输出 alias 仅从 display_name 派生；旧 PATCH alias 映射到 display_name；旧列兼容期结束再删 |
| R-03 | `slide_assets.content_sha256`（无写入方孤岛） | **保留列不动**；先查清语义；新发布写明确命名的新列（`file_sha256` = 原文件 SHA-256），不与"解码后内容指纹"混写；现有 revision 校验与 AI render fingerprint 不删 |
| R-04 | `shares.slides` JSONB 名数组 | **映射**：新建 `share_slides(token, slide_id)` 约束关系；迁移期把每个旧名绑定到**迁移时已确认的旧 slide_id**；旧 JSON 仅兼容输出快照，不参与授权判定；原文件删除后旧 token 不因同名新上传恢复访问 |
| R-05 | `grants` 领取 | **保留 token 级领取**；可见性消费改从 share_slides ID 关系展开 |
| R-06 | `slide_view_grants` | **映射**：主关联改 (slide_id, user_id)；slide_name 保留为历史快照列；取消 NULL-ID 退回名称匹配（0035 的 `IS NOT DISTINCT FROM` 双 NULL 生效语义随之退役）；无法唯一映射的旧记录**隔离**（unresolved，拒绝访问） |
| R-07 | `project_slides.slide` | **映射**：加 slide_id 列，迁移绑定；唯一键 (project_id, slide_id)；同名不同 ID 可同时入项目；旧 slide 文本列保留为兼容快照 |
| R-08 | `rois.slide` / `comments.slide` / `change_log.slide` | **活动查询改 slide_id**（新列，annotation_id 不变）；历史文本列**不重写成猜测 ID**；无法映射的记录标 unresolved，不展示在新资产下；仅保留审计 |
| R-09 | `run_grants.slide` / `ai_session_principals.slide` | **新记录显式绑 slide_id**；旧记录冻结为快照；插件 JWT 范围、后台执行请求显式绑 ID；billing_store:701 按 session 读的语义不变 |
| R-10 | `demo_catalog` / `demo_sessions` / `demo_runs` | **已按 slide_id，保持**；`demo_store.resolve_slide_filename` 的 id→filename 回落改经统一 resolver；`asset_revision`(mtime:size) 改经新 revision 来源；删除后 capability 不能读 |
| R-11 | `research_store.slide_pseudonym` | **改从 slide_id 派生**（新会话）；历史伪名记录不可变保留；研究删除按真实 ID 关联验证 |
| R-12 | `upload_user_quotas.used_bytes` "删除不回退"（0013 口径） | **裁决更新**：新 ID 资产按计划 §3.3 执行——`accounted_bytes` 与 ready 同事务设置，删除清理成功后按其幂等减少 used_bytes（防重复减账用 deleting/deleted 状态机 + 幂等键）；legacy 资产在迁移演练前维持旧口径，迁移时按手册 §2.3 容量项逐 owner 对账后统一 |
| R-13 | `upload_tasks` / `ingestion_jobs` / `conversion_jobs` / `baidu_import_items` | **任务表加显式 slide_id 列**；批量任务用既有 artifacts/items manifest 表达 task→多 slide_id；为"同一任务、同一逻辑 item"建库层唯一约束；不得因刷新/worker 重领重新分配 ID |
| R-14 | `audit_events` / `annotation_access_events` / `site_visit_events` | **历史不可变保留原样**；新记录同时记 ID 与名称快照；`/demo/<slide>` 模板值已是 slide_id 无需改 |
| R-15 | 缓存键（slide_cache/tile_cache/render stats/AI replay） | **改 ID+revision 键**；显示名修改不触发内容变化；删除准确失效该 ID；旧进程内缓存随发布失效 |
| R-16 | legacy 物理布局（UPLOAD_DIR 根下历史文件+伴侣目录） | **迁移期过渡**：legacy resolver 仅在受限迁移工具/过渡版本保留；历史资产迁入 `objects/<slide_id>/` 后保留原 slide_id；无法确认归属的**隔离**不猜测；验收后按保留期退役 |
| R-17 | `slides.display_name` 零读写死列 | 启用于 R-02；初始=original_filename |
| R-18 | `scripts/migrate_json_to_pg.py` 的 `_det_slide_id(name)` 确定性推导 | **退役候选**：与"现有 ID 不改号"冲突的重放路径在 P6 删除；存量已导入行保留原 ID 不动 |
| R-19 | `original_filename` | 原始 basename 展示快照：不接受目录语义、限制长度/控制字符、输出转义；下载 Content-Disposition 经安全编码，防 CRLF 注入 |
| R-20 | `storage_relpath` | 服务端生成、唯一、不可变；客户端不可设置；路径仅内部使用不返回浏览器 |

## 4. 拆除/保留清单的责任归属（计划 §5 落地）

| 项 | 责任阶段 | 落点 |
|---|---|---|
| `force_slide_owner_follow_file` + share_store 导出 + `cos_ingest_worker.py:880` 调用 | P4 拆除（P1 冻结新调用） | share_store_pg.py:3105、share_store.py:122、cos_ingest_worker.py:864-896 |
| COS `adopted_existing/still_ours/promoted_ident` 跨 owner 认领族 | P4 拆除 | cos_ingest_worker.py:764-896、`_stat_ident`:125 |
| `_upload_name_conflict` 原名冲突（3 调用点） | P3 拆除（V1/V2）、P4 拆除（COS 预检 `app.py:12306`） | app.py:324 |
| 转换 canonical 名唯一锁 + 同名/同 inode 复用分支 | P4 收敛 | conversion_store.py:84/141/237、conversion_worker.py:157-167 |
| V1 `allow_kfb_recover` 同名同 SHA 恢复 | P3/P4 拆除 | app.py:11147/11199-11223、`_owned_committed_upload`:361 |
| 百度 `_reconcile_ingest` 按名对账认领 | P4 拆除 | baidu_import_store.py:1209-1259 |
| `set_slide_meta(name)` 自动建行 + `resolve_slide_ref` 前缀直返 | P1 限制为迁移兼容层专用 | share_store_pg.py:1838/1991 |
| 目录扫描=资产（3 处） | P2 改 DB 资产查询 | app.py:7094/9784/10262 |
| 隐式建行旁路（demo PUT、V2 读路径校正、`api_slide_meta` 无存在性检查） | P2-P3 收口 | app.py:7698/11655/20470 |
| CLI 导入硬链接外部源 | P4 改复制隔离 | scripts/import_slides.py:115 |
| 暂存平铺 UPLOAD_DIR 根 | P3 起改 `.staging/<task_id>/<generation>/` | 2.3.7 全部命名点 |
| 保留：COS SDK 薄适配/预签名/版本钉源/Complete 恢复、容量池/FIFO/对账、V2 offset/哈希/幂等、格式校验、磁盘水位、CSRF/身份隔离、no-clobber/真实 CAS/lease fencing/readiness/一次结算/崩溃恢复 | 全程 | 不随 ID 化删除 |

## 5. P0 完成标准核对

- [x] 两仓 HEAD、他人改动、迁移号、基线回归已记录（第 1 节）
- [x] 每条入口都有迁移责任归属（第 2、4 节）
- [x] 每类历史引用有"映射/隔离/仅保留审计"裁决（第 3 节，R-01~R-20）
- [x] 历史 ID 生成器与 uuidv7 注释不一致已核对（第 1 节：不重置旧 ID，P2 修正注释）
- [ ] `scripts/audit_slide_identity.py` 只读审计工具实现并带测试（下一步）
- [ ] legacy resolver / ID DTO / asset_state / 发布删除状态图 / 锁顺序的代码旁合同落位（随 P1 代码提交）
