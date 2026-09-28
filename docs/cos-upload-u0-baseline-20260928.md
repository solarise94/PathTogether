# U0 基线：上传统一 COS 改造前的全景冻结

日期：2026-09-28。核对基线：PathTogether `a668580`（slide-id-refactor 本地分支）。
本文是 [统一 COS 上传执行方案](cos-only-upload-agent-plan-20260928.md) U0 的产物：
反例验证结论、入口/调用点清单、格式×身份×大小矩阵、容量冲突计算、锁图与容量
转移图、以及 ZIP/转换语义的契约冻结（以既有测试为权威）。后续步骤（U1–U6）
以本文为基线，不重新猜测。

## 1. R15 反例验证（U0 第一项）

- `TMPDIR=<suite>/.gate-tmp .venv/bin/python -m pytest -q tests/test_slide_id_review_r15.py tests/test_reconcile_upload_capacity.py`
  → **32 passed**（2026-09-28 复跑，HEAD=a668580）。
- 两条 R15 反例（owner 无预约上传被误停、终态残留 repair 无持久清理工作）在
  HEAD 已修复并带有原样入仓回归（`tests/test_slide_id_review_r15.py` 断言未改）。
- 全量门禁基线见 [r15 证据](review-evidence/r15/results.txt)：全量 pytest
  2867 passed / 1 known（admin 插件版本断言，属工作区第三方未提交文件演进）、
  js 562、HP contract 49。U0 不重跑全量（HEAD 未动）；U6 在最终态串行重跑。

## 2. 入口与调用点清单（盘点结论）

### 2.1 字节入口（全仓仅三处；grep `request.stream/get_data/files` 已核）

| 入口 | 位置 | 形态 | 检查点 B 去向 |
|---|---|---|---|
| V1 `POST /api/upload` | app.py:12645 `api_upload` | multipart 单请求（ZIP/原生/KFB 分派） | 删除接收；排空期只恢复既有任务 |
| V2 `PUT /api/uploads/<id>/chunk` | app.py:13781 `api_uploads_put_chunk` | 裸流分片 | 删除接收 |
| COS 浏览器 PUT | 不经 Flask（直传 COS） | presign UploadPart | 保留（唯一传输） |

唯一非上传 body 读取：`/api/research/viewer-events`（JSON 遥测，app.py:5932）。

### 2.2 任务创建/控制入口

- `POST /api/uploads`（V2 建，app.py:13628；ZIP/MRXS 400 `use_legacy_upload`）；
- `POST /api/ingestions`（COS 建，app.py:14352；格式白名单 `_COS_NATIVE_EXTS`
  app.py:3042、超准入 422 `cos_exceeds_admission` + `fallback_transport="v2"`
  app.py:14389–14396——两者本次统一改造的对象）；
- 控制面：`GET /api/uploads/<id>`、`/commit`、`DELETE`（取消）、`GET /api/ingestions/<id>`、
  `/upload-complete`、`/cancel`、`/parts/sign`、`/resume`；`/api/conversions*`。
- capability 下发：`_app_capabilities`（app.py:2985，含 `upload_v2_threshold_bytes`
  与 `cos_upload` payload `_cos_upload_capability_payload` app.py:3046）。

### 2.3 前端（static/app.js）

- 选路：`shouldChunkUpload` 6449（ZIP/MRXS 恒 V1，否则 ≥ 阈值走 V2）、
  `resolveUploadV2Threshold` 6438、传输选择器 `uploadFile` 6857。
- 适配器：`uploadFileLegacy` 6879、`uploadFileV2` 6650（+ resume keys 6466–6546）、
  `uploadFileCos` 7160（localStorage 分块确认、签名循环、状态轮询）。
- COS 手动开关 `cosManual` 6988 + `initCosUploadUi` 7470；「改用平台上传」
  按钮 7452（`upload.cos.retry_platform`）；`cosUploadEligible` 6990（前端
  白名单 + max_size 判定）；`cosErrorMessage` 7079（错误映射）。
- i18n：`upload.cos.*`（app.js 62–86 fallback 表 + static/i18n.js）。

### 2.4 服务端 worker 与服务模块

- `cos_ingest_worker.py`：单对象下载 `data.<ext>`（122）→ open_slide 验证 →
  `publish_with_channel`（946）→ `worker_settle_ready`（consume 同事务）→
  远端清理（Abort+删全部版本+释放池）。**当前无 ZIP/伴侣/转换处理**。
- `slide_publish.py`：六步统一发布（`publish_with_channel` 352、
  `publish_slide` 506、`publish_batch_item` 616、`publish_standalone` 713；
  通道：UploadTask 152 / Ingestion 955（ingestion_store）/ Conversion
  （conversion_store 648））。
- `upload_task_store.py` / `upload_guard.py` / `slide_storage.py` /
  `conversion_store.py`+`conversion_worker.py` / `ingestion_store.py`。

### 2.5 内部来源（不强制绕传 COS，共用容量/锁/发布原语）

- 百度导入 `baidu_ingest.py`（adapter over publish_slide）、CLI
  `scripts/import_slides.py` / `seed_demo_tcga_catalog.py`（publish_standalone）、
  转换 worker、demo。HistoPilot 侧真实调用方在 U3 盘点核实（HP contract 49 项
  为回归基线）。

## 3. 格式 × 身份 × 大小矩阵（现状冻结）

格式权威 = `slide_format_registry`（`CAP_NATIVE_SINGLE_FILE`：svs/tif/tiff/ndpi/
vms/vmu/scn/bif/svslide/bmp/jpg/jpeg；`CAP_NATIVE_BUNDLE`：mrxs；`CAP_CONVERT_REQUIRED`：
kfb/kfbf；archive：zip；其余 unsupported）。`app.SUPPORTED_EXTS`（含 bmp/jpg/jpeg/mrxs）
与 registry 由 tests/test_slide_format_registry.py 断言一致。

| 形态 | V1 现状 | V2 现状 | COS 现状 | 统一目标（U2 后） |
|---|---|---|---|---|
| 原生单文件（svs/tif/…/bmp/jpg/jpeg） | 接收 | 接收（≥阈值默认走 V2） | 仅 `_COS_NATIVE_EXTS`（缺 bmp/jpg/jpeg） | 全部原生单文件 |
| ZIP（单/多 MRXS bundle、多切片） | 唯一接收方 | 400 `use_legacy_upload` | 422 `cos_format_unsupported` | 接收（包语义原样迁移） |
| 裸 .mrxs | 验证后拒绝（提示打包 zip） | create 即拒 | 拒 | 保持拒绝 + 提示 |
| KFB/KFBF | 接收（commit 期建转换任务） | 接收（三段 commit） | 拒 | 接收（下载后交转换） |
| 未知/KFBF 未验收等 | 拒绝 | 拒绝 | 拒绝 | 拒绝（错误码说明原因） |

身份矩阵（准入/核账合同，0074 后）：

| 身份 | 本地配额责任 | 现状通道 |
|---|---|---|
| role=user（实名） | duty（reserve/bind/topup/consume/release） | V1/V2/COS 三通道一致 |
| owner（role=owner） | exempt | V1/V2 按 `quota_applies` 豁免；COS 按 owner_role 判定 |
| 本地免登录（空 owner_user_id） | exempt（0017） | 同上 |
| sdk/guest 等非 user 角色 | exempt | 同上 |

角色变更：`upload_tasks.quota_mode`（0074）/`ingestion_jobs.owner_role` 创建时快照；
核账按快照分类，不可证明 → 阻断（identity_unresolvable），不停任务。

## 4. 容量冲突计算（§2.1 的显式解）

- **产品文件上限**（现状权威）：`UPLOAD_MAX_REQUEST_BYTES` = 10×1024³ =
  **10,737,418,240 B**（upload_guard.py:71；V2 create 13677 以之 413）。
- **COS 单任务可准入上限**（结构性）：`capacity − safety` =
  10,000,000,000 − 500,000,000 = **9,500,000,000 B**（cos_config.py:35–38）。
- **缺口**：产品上限比结构性准入上限大 **1,237,418,240 B（≈1.15 GiB）**。
  当前默认配置下，9.5 GB–10 GiB 的合法产品文件永远进不了池。

裁决（按方案 §2.1，不得静默缩限/移除余量/塞队列/留回退）：

1. 代码参数化两个独立量：产品文件上限（沿用现有允许上限，控制 API 自身的
   小请求体保护另算）与 COS 结构性准入上限（capacity−safety）。
2. `declared_size > 产品上限` → 413 `upload_too_large`（不建任务）。
3. `declared_size > 结构性准入上限` → **503 `cos_pool_below_product_limit`**
   （服务端配置故障；不是等待、不是回退、不是用户错误）。等待（202
   `cos_waiting_capacity`）只用于「结构性可容纳但当前余额不足」。
4. capability `max_size_bytes` 下发**产品上限**；`formats` 从 registry 派生。
   前端超限即拒绝建任务。
5. 发布预检：`结构性准入上限 ≥ 产品上限` 必须成立（启动诊断 +
   `_cos_upload_capability_payload`/create 双侧校验），不满足 → COS 能力不可用
   （fail-closed），交付运维配置草案（§U6：capacity 需 ≥ 产品上限+safety，
   即 ≥ 11,237,418,240 B 时维持 safety=500 MB；或按批准调整产品上限——
   属产品决策，不在本执行内擅改）。

## 5. 锁图与容量转移图

### 5.1 DB 行锁序（全仓固定，无环；0066/0067/0072/0074 头注释一致）

```
advisory slide 锁（pg_advisory_xact_lock('slide:'||slide_id)）   ← 发布/结算/删除第一把
→ 任务行（upload_tasks | ingestion_jobs | conversion_jobs FOR UPDATE）
→ slides 行（CAS）
→ upload_user_quotas 行
→ upload_reservations 行
→ cos_pool_state 行（恒最后；持池锁期间不回头等其它行）
```

### 5.2 文件锁（task_storage_lock flock；R12 协议）

- 锁键类：`upload_task` / `ingestion_job` / `conversion_job` + 任务 ID；
  跨类固定顺序 **upload_task → conversion_job**；同类锁内调用公共入口会自取
  同类锁的函数必须走 `*_under_storage_lock` 变体（AST 审计口径，R13）。
- claim 事务提交 → 取文件锁 → 锁内短事务重验（state/generation/绑定预约）
  → 文件 I/O → 进度/收口短事务 → 释放锁；终态短事务在锁内、**文件清理在锁外**
  （延迟收口表 cleanup_due）；清理编排等待 writer 退出后才删树。

### 5.3 容量转移图（统一生命周期；结算只有一份实现）

```
reserve（准入）──bind（0072 holder）──┬─ topup（ZIP 展开补占，413 可拒）
                                     ├─ renew（同 rid 重发租约；不重准入）
                                     ├─ consume（结算：reserved→used，settle_bytes 一次）
                                     │    V1/V2 原生=文件字节；V1 ZIP=Σ已发布 item 字节；
                                     │    KFB 上传=源字节；COS 原生=declared
                                     └─ release（清理确认后，expect_holder）
refund_used_bytes_locked（删除/撤回 ready 产物退 used）
```

责任归属：V1/V2 → holder=(upload_task, id)；COS 本地 → (ingestion_job, id,
purpose=ingest_local)；COS 池预约独立（cos_pool_state，远端清理确认后释放）；
转换产物 accounted_bytes 在 conversion 结算记账（上传侧已 consume 源字节）。

## 6. ZIP / 转换语义冻结（契约测试 = 既有套件，迁移时不得重猜）

权威测试（迁移前后必须全绿，断言不改）：

- `tests/test_zip_guard.py`：全部安全限制（成员数/深度/大小/压缩比/加密/
  symlink/设备/重复路径/穿越/实际 vs 声明/水位 507/顶层无关内容拒绝）。
- `tests/test_zip_slide_id_pg.py`：多 item 各得 slide_id；MRXS 同 bundle 原子
  发布；无法归组指名拒绝；item 失败不株连；恢复复用 slide_id；**部分失败无
  逐 item 退款——finish_commit 一次性结算 Σ已发布字节**；预占中途失效撤回已
  发布包（不退款：consume 未发生）。
- `tests/test_upload_accounting_recovery.py`：V1 单文件/ZIP 走任务机；intent
  先于提升；finish/settle 崩溃各一次结算；恢复幂等。
- `tests/test_kfb_upload.py` / `test_kfbf_upload.py`：202 conversion_pending；
  上传 settle=源字节；产物由转换 worker 结算；失败连带收口不株连幂等复用
  的前序 job；同 sha 改名不重写在途路径。
- `tests/test_cos_ingest_worker.py` / `test_capacity_lifecycle_cos.py` /
  `test_upload_v2.py`：COS 结算一次、清理确认后释放、取消互斥（commit intent
  后拒取消）、V2 计量语义。

冻结的产品规则（测试之上的文字合同）：

1. ZIP 的 item 粒度：确定性失败按 item 剔除进 `failures`，其余继续；全部失败
   整体 400/failed；**没有部分退款**。
2. MRXS = 入口 + 同 stem 伴侣目录，一 bundle 一 slide；裸 .mrxs 拒绝并提示。
3. KFB：上传任务结算**源字节**；产物字节由转换任务结算（accounted_bytes）；
   上传失败连带作废**本上传创建**的 job（upload_id 匹配），不株连同 owner+sha
   幂等复用的前序 job。
4. commit intent 持久化后不可盲目取消（三通道同裁决）。
5. 清理确认后才释放容量；清理失败持久重试（pending/failed + 退避）。

## 7. U0 结论与执行边界

- 基线健康：R15 反例已修、门禁基线在案；无需先修缺口，直接进入 U1。
- 唯一部署阻塞项：§4 容量缺口（配置层，非代码层；代码按裁决实现 fail-closed）。
- 生产授权边界重申：不部署、不核账 apply、不停写、不扩池、不启 COS capability；
  副本演练为验收手段（U4）。
