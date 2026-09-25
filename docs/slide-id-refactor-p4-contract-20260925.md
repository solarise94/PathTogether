# P4 设计合同：ZIP/MRXS、转换、远程导入与 COS 接入统一发布

日期：2026-09-26。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §3/§5 与 [P3 合同](slide-id-refactor-p3-contract-20260925.md)。
前置：P3 完成（V2 + V1 单文件经 slide_publish 统一发布；id_bundle 资产在生产路径上存在；删除/减账最小版可用）。**P4 完成后，所有正式支持的上传/导入入口都经统一发布服务，不存在旧旁路**（P4 完成标准）。

## 1. 范围与分包

| 子包 | 文件面 | 并行性 |
|---|---|---|
| P4-a ZIP/MRXS + 转换辅助族 + CLI/种子收口 | app.py（`_api_upload_zip`/`_prepare_zip_bundle`/`_promote_zip_bundle`/`_recognize_slide_bundle`/转换辅助族）、scripts/import_slides.py、scripts/seed_demo_tcga_catalog.py | 独占 app.py |
| P4-b COS | ingestion_store.py、cos_ingest_worker.py（+ app.py 的 ingestion 端点微调若必须——先与 P4-a 串行或协调） | 与 P4-a 并行（app.py 部分最后并入） |
| P4-c 百度远程导入 | baidu_import_store.py、baidu_ingest.py、baidu_import_http.py、scripts/baidu_import_worker.py | 与 P4-a/P4-b 并行 |

串行规则：P4-b/P4-c 先跑（不碰 app.py），P4-a 最后合入 app.py 改动（含 COS/百度端点的联动）。

## 2. ZIP/MRXS（P4-a）

1. `_prepare_zip_bundle` 解包目标改 `slide_storage.staging_dir(task_id, generation)`（不再平铺 `UPLOAD_DIR/.extracting-*`）；既有 zip-slip/炸弹/数量限制全部保留。
2. `_recognize_slide_bundle` 逻辑保留（识别逻辑切片+伴侣归组），但**每个逻辑切片预分配 slide_id**（upload_task_items(task_id, item_key, slide_id)——item_key=zip 内相对路径/包键，0067 表已就位；重试/恢复按 (task_id, item_key) 复用，绝不重新分配）。
3. 包内相互引用的文件必须归为同一 bundle（同一 slide_id 目录）；无法确定归组 → 拒绝该 item（400 指明哪个），不跨资产目录互相引用。
4. 发布：每个逻辑切片 = 一次 slide_publish.publish_slide（多文件包 manifest 列全部成员，含伴侣；完整包原子发布——不能入口先可读伴侣后到）。单任务多切片：逐个发布；单个失败按 item 失败处理（failed + 证据），不影响同任务其它 item 的已发布结果；任务级 settle_bytes = 全部已发布 item 字节合计（quota 一次性结算在任务 finish_commit——逐 item 的 accounted_bytes 在其 publish 事务内写入，任务结算汇总口径在实现时评审）。
5. MRXS 伴侣目录随主文件同包发布（bundle/ 子目录保留包内相对关系，manifest 指定唯一入口）；`slide_storage` 的多文件包路径派生（entry_relpath 之外的 bundle/ 分支）如需扩展在此实现并补测试。
6. 拆除：`_upload_name_conflict` 在 ZIP 预检的调用（目标冲突预检 `(UPLOAD_DIR / rel).exists()` 删除——ID 目录天然无冲突）；ZIP 的 `set_slide_meta` 逐切片调用删除（改 upload_task_items + publish）。

## 3. 转换（P4-a 的 app.py 辅助族 + conversion_store/worker）

1. 转换产物**预分配 slide_id**：`conversion_jobs.slide_id`（0067 已有列+部分唯一索引）在 create_job 时 allocate（产物 owner = 源切片 owner）。canonical 名唯一锁（`idx_conversion_jobs_canonical_live`/`canonical_is_live`/`NameConflict`）**拆除**——同名产物是独立新资产；转换幂等键保持 (owner, source_sha256, converter_id, converter_version)。
2. 源定位：`conversion_job_sources` 增 source_slide_id 已有列（0067）；worker 读源改经 descriptor（source_slide_id 优先；旧任务按 source_name alias 解析过渡）。
3. worker `process_job`：work 文件落 staging；完成经 publish_slide 发布产物包（含 manifest/associated——全部进 objects/<产物 slide_id>/ 内：`<slide_id>/bundle/` 或同级文件，manifest 列全）；dest 已存在的三分支（manifest 匹配复用/同 inode 补提升/name_unavailable）**全部拆除**（ID 目录无冲突）；`_retract_ours` 的同 inode 判定拆除（失败清 staging 即可）。
4. `complete_job` 结算与 publish 的同事务口径：产物的 accounted_bytes 与 used_bytes 结算并入 publish 事务（替代 `canonical_settled_bytes` 单列幂等键——评审时决定保留该列作兼容还是随 publish 事务化退役；**裁决：保留列作幂等键兼容，但结算挪进 publish 事务**，注释标注）。
5. `_cleanup_conversion_sidecars`：删除产物资产时改按 slide_id 清 objects/<id>/（连带源删除语义保持不变——源是独立资产，P5 删除编排裁决）；`_canonical_name_for`/`_owned_committed_upload`/`allow_kfb_recover` 残留分支全部拆除。
6. `set_slide_meta(canonical)` 调用删除；产物行来自 allocate。

## 4. 百度远程导入（P4-c）

1. `baidu_import_items.slide_id`（0067 已有列）在入库编排时预分配；`_phase_ingest`/`baidu_ingest.ingest_staging` 的 native 分支：复制到受管理暂存 → 验证 → publish_slide 发布；`_copy_new` 的 O_EXCL 落盘形态并入 staging（不直接写 UPLOAD_DIR 根）。
2. `_reconcile_ingest` 的按名对账认领（baidu_import_store.py:1244-1259）**拆除**：恢复只按 item 的 slide_id/ingest_token 判定（盘上文件存在性不再是归属证据）。
3. convert 分支：经 §3 的转换新链路（conversion_job_id → 产物 slide_id 回填 item.slide_id）。
4. `slide_name` 列保留为展示快照；`associate_slide`（入项目）改传 slide_ids。
5. `ingest_token` 语义保持（幂等凭证），但不再含 "slide:<name>" 形态的身份承诺——改 `item:<item_id>`（评审时核对重放语义）。

## 5. COS 直传（P4-b）

1. **拆除清单（计划 §5 + P0 §4）**：`cos_ingest_worker.py` 的 `adopted_existing`（764-782）、`promoted_ident`/`_stat_ident`（125/786/841）、`still_ours`（876-891）、`share_store.force_slide_owner_follow_file` 调用（880）与其 share_store_pg 定义+share_store 导出——**全部删除**；name_unavailable 失败族（778/789/845/893）随之消失（ID 目录无同名冲突）；`app.py:12306` 的 `_upload_name_conflict` COS 预检删除。
2. `ingestion_jobs.slide_id`（0067 已有列）在 create_waiting_job 时预分配（owner 即资产 owner）；`slide_canonical_name` 列语义退役为展示快照（评审时决定是否随 0069 标记 deprecated）。
3. 本地下载暂存（`<job_id>.part`）改 `.staging/<job_id>/<generation>/`；`process_validating` 的本地提升改 slide_publish.publish_slide（远端 key/version 与本地 slide_id 分别持久绑定——commit_intent_json 已含全部要素，补 slide_id 字段）。
4. `worker_settle_ready`：consume reservation + mark_ready 与 publish 事务合并（同事务）；ready=本地可见；completed=readiness probe（按 descriptor 路径试开，不再按 canonical 名）。
5. 池预约/远端清理/版本钉源/FIFO/对账**原样保留**（计划 §5 保留项）——只换本地身份与发布边界；COS capability 保持 off（本阶段不开）。
6. COS 完成响应的 slide_id 从任务绑定读（P2 补丁的按名 resolve 对 id_bundle 产物失效——`_ingestion_state_body` 改读 job.slide_id）。

## 6. CLI 导入与种子（P4-a 尾）

1. `scripts/import_slides.py`：**禁止与外部可写源共享 inode**——`--move` 改为复制到 `.staging/import-<ts>/` 后再发布（源删除在发布成功后）；无 --move 时复制（不硬链接）；发布经 slide_publish（owner 参数保留，空→配置 owner 解析现状不变）；ZIP/MRXS 拒收保持。
2. `scripts/seed_demo_tcga_catalog.py`：`set_slide_meta` 建行改 slide_store allocate+publish（或显式 legacy 注释的迁移路径——裁决：种子脚本面向 demo 环境，改走统一发布；若文件已在 UPLOAD_DIR 则走"回填式"登记：allocate → 复制进 objects → publish）。
3. `scripts/migrate_json_to_pg.py` 的 `_det_slide_id` 确定性推导：本阶段加退役注释（P6 删除；存量行不动）。

## 7. 强制收口（随本阶段完成）

- `set_slide_meta` 的 deleted/deleting→ready 复活分支**拆除**（最后一条 legacy writer 路径切走之后）；重写 `test_slide_delete_clears_view_grants_no_orphans` 为目标不变量（重传=新 ID；旧分享/授权/标注不继承；场景保留断言换新）。
- `_upload_name_conflict` 函数与其全部调用点删除（V1/V2/COS 已清，ZIP 在 §2 清）；`conversion_store.canonical_is_live`/`invalidate_by_canonical`/`get_job_by_canonical` 的 canonical 名占用语义退役（删除产物→按 slide_id 作废任务）。
- 全仓 `rg` 核查：无残留的"按原名定位资产"写路径（产出搜索证据进交付记录）。

## 8. 测试（计划 §8 矩阵的 P4 部分 + 既有套件）

- ZIP：多逻辑切片各得各 ID；伴侣目录同包原子发布；无法归组 item 拒绝；崩溃恢复幂等（同 item 重试复用 slide_id）；配额按全部已发布字节结算一次。
- 转换：同名源/产物不冲突（独立 ID）；failure/retry 不重复 ID/项目关联/配额；源删除后产物仍在（独立资产）；产物删除按 ID 失效。
- 百度：恢复不认领他人文件；item→slide_id 链路；项目关联按 ID。
- COS：adopted/still_ours/promoted_ident 删除后，同名并发/旧 worker 复活/两 worker 同任务的场景全部转化为"各发各的 ID"；既有 COS Fake 协议/池/恢复回归全绿（force_slide_owner_follow_file 删除后其专属测试族按「场景意义保留、废弃断言换新」重写——跨 owner 认领场景的新断言是"隔离+告警，绝不修正 owner"）。
- 门禁：全量 pytest（先清 /tmp 残渣）仅允许已知无关失败；test:js 全绿；HistoPilot contract 复跑（确认 PT 侧无回归破坏跨仓契约）。
