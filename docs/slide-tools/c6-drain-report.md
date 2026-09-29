# C6 实施报告：转换/百度旧链路排空审计工具 + 本地排空/恢复演练

- 日期：2026-09-29。工具版本 `c6.1`。
- 范围（退役对象）：LEGACY 后台切片转换链路（`conversion_jobs` /
  `conversion_worker` 进程内 KFB 转换 worker）与 LEGACY 百度分享导入
  in-process 执行器（`scripts/baidu_import_worker.py` → `claim_batch` /
  `run_claimed_batch`）。插件执行器（worker_id 前缀 `plugin:`，经
  `plugin_claim_batch` 同一领取原语）**不在退役范围**。
- 本阶段只加工具与测试，**不改任何运行时行为**（app.py /
  conversion_store.py / conversion_worker.py / baidu_*.py /
  ingestion_store.py / upload_guard.py / cos_ingest_worker.py 零改动）。
- 复跑指南：`docs/review-evidence/slide-tools/C6/RERUN.md`（实跑输出在同
  目录 `results/`）。

> **验收修订（2026-09-29，主代理）**：
> - 盘点连接改为单个 `REPEATABLE READ READ ONLY` 事务：各 section 同一时点，写入由数据库拒绝
>   （原为 autocommit 逐条 SELECT）。
> - `UPLOAD_DIR` 不存在 → exit 2（原先按「无文件」处理会得出假 GO）。
> - `compare` 预约变化计数打印缺 `%` 操作数，已修。
> - 新决策项：`failed_source_retained`（退役后无重试入口，失败任务保留源=清理义务，带字节）、
>   `failed_product_bundle_present`（F3 形态下作废产物包仍占盘）、`baidu_staging_dir_missing`
>   （有批次但本地暂存根不可见——旧容器 /tmp 不在卷上）。
> - 演练暴露的 C5 缺陷已另行修复：插件路径批次收口曾对「已由 producer 导入计费的条目」再按源字节
>   consume 批次预算（双重计费）；现只对非 producer 发布的 ready 条目计费（进程内交接场景仍计）。
> - 迁移/排空计划与 A 构建判定见 [c6-migration-drain-plan.md](c6-migration-drain-plan.md)：0067 不回填
>   旧转换任务 `slide_id`，新 worker 对其 fail-closed ⇒ 旧任务只能在旧镜像排空，A 不需要保留执行能力。

## 1. 交付物

新增：

- `scripts/conversion_drain.py` —— 排空审计工具（inventory / report /
  compare 三个子命令；严格只读，见 §2）。
- `tests/test_conversion_drain.py` —— 7 个用例（分类矩阵 / 排空演练 /
  恢复演练 / worker 重领冲突 finding / 只读证明 / 旧 schema / CLI 子进程）。
- `docs/review-evidence/slide-tools/C6/RERUN.md` + `results/*.txt`。
- 本报告。

## 2. 工具契约（scripts/conversion_drain.py）

### 2.1 只读性

- DB：`psycopg.connect(autocommit=True)`，只发 `SELECT`（含
  `reconcile_upload_capacity.collect()` / `plan_actions()`——两者本身只读）。
- 文件：只 `stat` / `listdir` / 读尺寸；`--probe-locks` 对**既有**锁文件以
  `O_RDONLY` + `flock(LOCK_EX|LOCK_NB)` 即取即释探测活跃持有者
  （`EWOULDBLOCK → live_holder`），**绝不创建/写任何文件**（不用
  task_storage_lock 的 O_CREAT 路径）。测试 §4 证明：表正则化转储 +
  UPLOAD_DIR/百度暂存递归清单（path,size,mtime_ns）在工具运行前后逐位
  一致。
- 退出码：`0` = GO/通过；`3` = NO-GO/漂移；`2` = 用法错误（与
  scripts/upload_drain.py 同风格）。

### 2.2 子命令

```
python3 scripts/conversion_drain.py inventory [--upload-dir DIR]
    [--baidu-staging-dir DIR] [--json PATH] [--probe-locks]
    [--database-url URL]
python3 scripts/conversion_drain.py report   [同上选项]      # 排空裁决
python3 scripts/conversion_drain.py compare  BEFORE.json AFTER.json
```

### 2.3 inventory 各 section 与推导口径

| section | 内容与推导 |
| --- | --- |
| `meta` | tool_version / generated_at / schema（`schema_migrations` 最大文件名 + information_schema 列/表 + `pg_get_constraintdef` 探测 held CHECK——以**实际 schema** 为准）/ 目录 |
| `conversion` | 按 state 计数；每个 open 任务（held/queued/converting/validating）带 owner/state/attempt/lease_owner/lease 存活或过期/has_intent/slide_id/upload_id（upload_id 命中 ingestion_jobs 时给出父任务 state）/created_at/age_seconds；ready 任务的 project_associate_state pending/failed（关联义务，0052 前的库按 not_applicable_schema 报告） |
| `intents` | ① `commit_intent_json` 非空且 state≠ready 的转换任务（0069 前的库 not_applicable_schema）；② conversion 形态 ingestion_jobs（0075 kind 列）在途且携带 intent / conversion_job_id 链接 |
| `baidu` | 按 state 计数；活跃批次（queued/running）的执行者分类：`plugin`（lease_owner 前缀 `plugin:`）/ `in_process`（其余非空）/ `unclaimed`；租约存活/过期；cancel_requested；非终态条目数；终态批次 cleanup_state pending/failed（远端副本清理义务）；baidu_batch 预约按 state 分组（0072 前按 `quota_reservation_id` 连接降级）；异常：reserved 预约绑定终态批次 |
| `files` | (a) 每任务 `.staging/<cvj_id>/`：source/ 存在与字节、attempt 目录/杂散文件与字节、分类（见 §2.4）；(b) 引用的平铺源（`conversion_job_sources` ∪ `conversion_jobs.source_name`）存在性/字节/引用状态；(c) 百度 STAGING_ROOT/<batch_id>/ 字节与批次状态；(d) `.staging` 下不归属任何已知表 id（upload_task/ingestion_job/producer_import/conversion_job/baidu_item/slide_id）的目录；(e) `.task-locks/{conversion_job,baidu_batch,ingestion_job}/*.lock`（--probe-locks 时探测活跃持有者） |
| `retained_sources` | （C7 输入①）ready 任务仍在盘的源（staging source/ 或平铺源，字节=盘上 stat）+ 产物资产状态 + 每用户合计 |
| `source_charges` | （C7 输入②）每用户源字节计费，按推导通道拆分（docstring 逐条注明口径与下界性）：`ing:<job_id>`（COS conversion 形态预约 consumed 的 settled_bytes，缺失回退 declared source_size_bytes=下界）、`upt:<upload_id>`（旧上传任务 finish_commit 结算的 settled_bytes；预约缺失不计入=下界）、`bib:<batch_id>`（批次终态 consume 的 settled_bytes，回退 Σ ready 条目 source_size=下界；`conversion_item_bytes` 给出其中指向转换任务的条目源字节）。live / charged_never_refundable 拆分：任务 ready 且产物 ready/deleting = live；任务 cancelled/failed 或产物 deleted/failed = **永不退款**（删除只按产物 accounted_bytes 退款——app.py `_slide_delete_settle` 只退 id_bundle 产物字节）；百度侧仅 conversion_job_id 非空条目可拆（插件上报条目 → undetermined，下界） |
| `ledger` | 每用户 used/reserved + Σ(slides.accounted_bytes where asset_state IN (ready,deleting) AND storage_layout='id_bundle')（0067 前降级为 used_bytes）+ residual = used − 该和；预约行清单与按 holder_kind/purpose/state 分组 |
| `reconcile` | schema 支持时运行 `reconcile_upload_capacity.collect()+plan_actions()`（只读）；conversion/baidu item/slide id 命中的 unknown_staging_dir 与 upload_drain 同口径过滤（归本工具裁决）；证据扫描失败 → `reconcile_scan_failed`；生产 0065 旧库 not_applicable_schema（缺 ingestion_jobs/producer_imports/holder 绑定/quota_mode 等） |
| `ops_checklist` | 无法服务端证明项：转换 worker 停止 / BAIDU_IMPORT_WORKER=0 且无 in-process worker 进程 / 无 kfb 转换子进程写 .staging / 窗口内无新 conversion 形态流量 |

### 2.4 文件分类（files.conversion_staging[].classification）

| 分类 | 判定 |
| --- | --- |
| `expected_open` | open 任务（held 除外）且源在（staging source/ 或平铺源） |
| `held_in_handoff` / `held_handoff_pending` | held 任务有源 / 无源（交接中） |
| `source_missing_open` | open 任务源副本缺失且无平铺源（**阻断**——open 任务本已阻断） |
| `expected_source_retained` | ready/failed 任务源保留（重试/证据需要）、无代次残留 |
| `residue_attempt_dir` | 终态任务的工作代次目录/杂散文件残留（**阻断**） |
| `residue_cancelled_job` | cancelled 任务树任何内容（源/代次/杂散；**阻断**） |
| `source_missing_failed` / `source_missing_ready` | 终态任务源缺失（failed=决策项非阻断；ready=决策项） |
| `clean` | 无内容（如 cancelled 任务只剩空目录——与 R16 空目录语义一致） |

平铺源：只被 cancelled 任务引用且在盘 = `residue_flat_source`（**阻断**）；
否则 `retained_or_inflight_flat_source` / `absent`。百度暂存：终态批次有
文件 = `baidu_staging_residue`（**阻断**）；无批次行 = `baidu_staging_unknown`
（**阻断**）；活跃批次 = `inflight_work`。

### 2.5 report 阻断码（每项打印 reason code 与 id 清单）

`conversion_job_open`（含 held——held 是 COS conversion 形态在父任务行锁内
创建的不可领取子任务，未收口）｜`intent_unresolved`（validating+intent：
发布可能只进行到一半）｜`ingest_intent_unresolved`（conversion 形态
ingestion 处于提交临界段 completing/validating 且带 intent/链接；ready
只列清单不阻断——源已结算，转换子任务责任由 conversion_job_open 裁决）｜
`baidu_in_process_live_lease`｜`baidu_handoff_pending`（in-process 租约过期
+非终态条目——插件必须接管）｜`baidu_reservation_terminal_bound`｜
`residue_attempt_dir`｜`residue_cancelled_job`｜`residue_flat_source`｜
`baidu_staging_residue`｜`baidu_staging_unknown`｜`unknown_staging_dir`｜
`source_missing_open`｜`reconcile_blocker`｜`reconcile_scan_failed`｜
`live_lock_holder`（--probe-locks）。

**非阻断**（列出、不裁决）：`plugin_work`（queued/unclaimed/plugin: 租约批
次——插件工作）；`decision_items`（`source_missing_failed`、
`source_missing_ready`、`intent_lingering_failed`（failed 任务残留 intent）、
`association_pending|failed`、`baidu_remote_cleanup_pending|failed`）；
`c7_inputs`（retained_sources + source_charges——decision required）。

### 2.6 compare 台账漂移不变量

不变量：产物结算/删除使 used_bytes 与「ready/deleting 的 id_bundle slides
accounted_bytes 之和」**等量**移动，残差（residual = used − 该和）只被**源
字节结算**移动。因此

```
drift(user) = (residual_after − residual_before) − Σ(快照间新结算的源字节事件)
```

事件按稳定 key 对账（`ing:<job_id>` / `upt:<upload_id>` / `bib:<batch_id>`）：
AFTER 新增 key 计全额；同 key 字节变化计差量。drift≠0 → exit 3。预约行的
消失/状态变化单独列出并给出持有者解释（baidu_batch 终态 consume、
ingestion 清理后 release 等）。

## 3. 排空演练结果（tests/test_conversion_drain.py::test_drain_rehearsal）

NO-GO 世界（真实执行器构造，无 SQL 伪造状态）：

- 真实 COS conversion 形态（真实合成 KFB 580×300，37221 B）推进到
  validating，真实结算 → held 子任务激活 queued、源字节 consume 37221 B
  （used=37221, reserved=0，恰一次）；
- queued 转换任务（真实 KFB 源）；
- validating+intent 崩溃残留（FS 发布前注入临时故障——worker 临时故障分支
  保持 validating）+ 租约拨过期；
- 百度批次：in-process 执行器领取后租约过期、条目全部非终态
  （handoff_pending）；
- cancelled 任务树残留。

BEFORE inventory：report exit 3，阻断码恰为
`{conversion_job_open, intent_unresolved, baidu_handoff_pending,
residue_cancelled_job}`。

排空（全部走**真实既有执行器**）：

1. `conversion_store.claim_one` + `conversion_worker.process_job` 循环至无
   open 任务（崩溃任务经重领恢复：重转 → 新 intent →
   `publish_with_channel` → 结算恰一次；held 子任务同一循环收口）；
   `ciw.process_ready` 把父 ingestion 推进 completed；
2. `plugin_claim_batch` + `plugin_report_item`（stage=ready +
   ingest_token）→ 批次 succeeded，`plugin_finalize_if_settled` 一次性
   consume（18752 B）；
3. 残留收口：cancelled 任务树**无 store 级清理路径**（finding F2）→ 演练中
   直接 `shutil.rmtree`（`_cleanup_work_dirs` 只清非 source 子项，source/
   无 store 层入口；运行时唯一路径是 app 删除端点的
   `_cleanup_conversion_sidecars`，见 §5-F2）。

AFTER inventory：report exit 0（零阻断）；compare(BEFORE, AFTER) exit 0
（零未解释漂移；残差增量 37221+18752 B 全部由 `ing:` / `bib:` 事件解释）。
产物字节恰结算一次：`used_after = used_before + Σ(三个产物 accounted_bytes)
+ 18752`，`slide_accounted_live_bytes = Σ(产物)`，`residual = 37221 + 18752`。

## 4. 恢复演练结果

- `test_recovery_rehearsal`：intent 持久化 + FS 包已发布 + 结算前崩溃
  （注入 `PublishError(staging_io_error, deterministic=False)`——与
  conversion_worker 临时故障分支同口径）→ inventory 见
  `intent_unresolved`、report 3 → 以 slide_publish 合同的崩溃恢复路径重放
  （`publish_with_channel(manifest=None)`：intent 是发布的权威证据，恢复不
  重新构造；fencing 按代次，过期租约不阻塞）→ ready、产物字节恰结算一次
  （used == accounted）→ report 0、compare 0。
- `test_recovery_worker_reclaim_path_hits_manifest_conflict`：同一崩溃窗口
  走 **worker 重领**路径的真实行为 = `publish_conflict` fail-closed（finding
  F3，§5）。

## 5. Findings（须评审裁决；本阶段不改运行时）

- **F1 源字节计费永不退款（既有缺口，工具量化）**：COS conversion 形态源
  字节在 `worker_settle_source` consume、百度批次在终态 consume、旧上传
  任务在 finish_commit 结算；删除产物时 `app._slide_delete_settle`
  （app.py ~12131-12170）只退 id_bundle 产物的 accounted_bytes——源侧字节
  **从不退款**。分类矩阵中量化：上传 300 B 源 + 产物 ready 后删产物 →
  `source_charges.per_user.charged_never_refundable = 300`；排空演练中残差
  = 源 37221 + 批次 18752 B（永不回收）。retained_sources / source_charges
  作为 C7 输入（decision required），不是排空阻断。
- **F2 `no_runtime_cleanup_path`（cancelled 任务树 / 百度本地暂存）**：
  - cancelled 转换任务的 `.staging/<cvj_id>/` 整树：store 层无清理入口
    （`conversion_worker._cleanup_work_dirs` 只清非 source 子项；
    `void_held_for_upload_locked` / `invalidate_by_slide_id` 只改状态）。
    运行时唯一路径是 app 删除端点 → `_cleanup_conversion_sidecars`
    （app.py ~286-322，删保留源目录+平铺源）。演练中直接 rmtree 并在此
    记录。
  - 百度 in-process 路径**从不清理**本地下载暂存（模块 docstring 明示）；
    插件路径源副本清理归插件（C5 合同 §6.2）。终态批次暂存残留是阻断码
    `baidu_staging_residue`，无平台侧清理函数。
- **F3 worker 重领恢复在「FS 已发布、结算前崩溃」窗口 fail-closed**：
  重领（attempt+1）后 worker **重转**并构造新 manifest——kfb 产物包成员
  `data.tif.manifest.json` 内嵌 `created_at`（kfb/manifest.py:61
  `datetime.now(timezone.utc).isoformat()`），重转产物与已发布包**非逐字节
  相同** → `verify_bundle` 不吻合 → `PublishConflict`（deterministic）→
  任务 failed、产物资产 failed、包与源保留证据。恢复只能走 intent 权威重放
  （§4，测试验证可用）或人工核对。**不改运行时**（超出 C6 授权）；建议
  C7/后续阶段二选一：① `build_manifest` 去掉 created_at（或改为内容派生）
  使重转 byte 级确定性；② conversion_worker 恢复分支检测
  `bundle_dir.exists()` 时改用 intent manifest 重放
  （`publish_with_channel(manifest=None)`）。
- **F4 ingest intent 阻断口径**：conversion 形态 ingestion 的
  commit_intent_json 在 ready 后保留（快照语义），conversion_job_id 链接
  在 ready 上是正常交接痕迹。工具把 `ingest_intent_unresolved` 阻断限定在
  提交临界段（completing/validating）；ready 只列清单。与
  `reconcile_upload_capacity._commit_intent_open`（intent 或
  completing/validating）的核账口径相容——核账对 ready+consumed 无残留
  不阻断。
- **F5 旧 schema（生产 0065）降级验证**：另建库应用 ≤0065 迁移 + 纯 SQL
  种子：inventory 成功，applied_max=0065_*（65 个），缺失特性
  （conversion_slide_id / commit_intent / held CHECK / slides 记账列 /
  holder 绑定 / ingestion_jobs / producer_imports）全部 False，对应 section
  输出 `not_applicable_schema` + 原因；转换/百度计数正确；report 仍出裁决
  （阻断 = conversion_job_open + source_missing_open +
  baidu_in_process_live_lease）。ledger 退化为 used_bytes（residual 语义在
  0067 前不可计算——文档已注明）。

## 6. 开放问题

1. F1 的源字节退款缺口：C7 是按「接受既成计费」还是引入源侧退款？若退款，
   旧库（0065）无 slides 记账列，退款口径只能按预约 settled_bytes。
2. F3 的两个修复方向（① manifest 去时间戳 / ② worker intent 重放）取哪个？
   ① 是运行时改动（converter/manifest），② 是 worker 改动——都超出 C6
   「只加工具与测试」授权。
3. `baidu_remote_cleanup_pending|failed`（终态批次远端副本义务）在 in-process
   执行器退役后由谁执行：插件是否需要接 `retry_cleanup` 等价能力？
4. held 任务在父 ingestion 已终态时的收口：当前唯一路径是
   `void_held_for_upload_locked`（父终态事务内）；若父已终态而子仍 held
   （理论不可达，矩阵未造出该形态），是否需要排空工具给出更强信号？
5. `residue_flat_source` 的清理在 app 删除端点外无入口（与 F2 同源）；C7
   若裁决清理保留源，需新增运维清理路径（本工具只盘点不清理）。

## 7. 测试与复跑

见 `docs/review-evidence/slide-tools/C6/RERUN.md`。本阶段实跑：
新文件 7 passed；相关既有套件 73 + 105(+4 skipped) + 29 passed，全绿。
验收修订后：新文件 **10 passed**（+失败源决策断言、UPLOAD_DIR 缺失 exit 2、库强制只读、compare
计数输出、作废产物包决策）。
