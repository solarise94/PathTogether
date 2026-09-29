# C5 producer 导入 API 与权限合同（冻结稿 v1）

- 日期：2026-09-29。基线：PathTogether HEAD `11b1594`；下一个可用迁移号 **0077**（`migrations/` 止于 `0076_conversion_jobs_held.sql`）。
- 状态：设计冻结；§8 八项开放问题已于 2026-09-29 按本文建议定稿（用户确认按建议处理）。C5 双子代理——平台 producer import API 与百度插件——以本文为准；实现偏差需回改本文再动代码。
- 上游方案：`docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md` §1.1/§7/§8/§10.4。
- 对接（不重写、不编辑）：`docs/plugin-capability-layer-design.md`（用户编辑中）——本合同的新权限条目须登记进其 §6.1 权限矩阵（新增一行「producer 导入委托」）。
- 本文只定义合同；不写任何 SQL/代码。§3 只**提议** 0077 的表结构语义。

## 0. 范围与非目标

平台新增一个**通用、授权受限的机器生产者导入通道**（§7.2）：插件后端把**最终单文件产物**按有界流写入平台私有 staging，平台自行验证并经唯一 `slide_publish` 发布结算。它不是浏览器通道（浏览器上传仍只有 COS），不包含任何厂商格式/百度语义，不做跨用户按 hash 秒传或文件认领（§7.3）。

明确非目标：不为内部导入强制绕 COS 往返（同机 loopback 直提，认证/额度合同与远程插件相同）；不建第二套 conversion 状态机（§7.3）；不伪造旧 `upload_task`/`conversion_job`/COS 字段（§8）；首版不做共享目录 rename 优化（§7.2，接受一个短暂产物副本）。

## 1. 端点合同

### 1.1 通道与通用约定

- 全部挂在既有机器前缀 `/api/plugin/v1/` 下（该前缀绕过 Cookie 鉴权，`app.py:1196-1198` 的 before_request 白名单已覆盖新路径，无需改钩子）。
- 认证：`Authorization: Bearer <scoped JWT>`，逐端点经 `_require_plugin_token`（`app.py:13360`：签名/iss/aud/exp + installation 存在且 **enabled 每请求回查** + scope 包含检查）。停用插件的在途 token 立即失效（现有语义，`app.py:13569-13580`）。
- 错误信封：复用 `_plugin_error`（`app.py:13180`）与 `_PLUGIN_ERROR_RETRYABLE` 稳定码表（`app.py:13148`）；新增码见 §1.8。
- 幂等键：`Idempotency-Key` 头（1–128 `[A-Za-z0-9_-]`，同 `_REQUEST_ID_RE` 口径，`app.py:13821`）。幂等域 = **(installation_id, key)**；同键同载荷重放返回原任务，同键异载荷 `409 idempotency_conflict`（镜像百度批次语义，`baidu_import_store.py:802-811`）。
- 响应绝不携带 staging 路径、密文秘密（镜像 `baidu_import_http.py:9-10`、`ingestion_store._sanitize_detail` `ingestion_store.py:238`）。
- 限流：控制面端点（begin/commit/status/cancel/cleanup-confirm）沿用 per-installation 速率桶（`PLUGIN_RATE_LIMIT_PER_MIN` 默认 120/min，`app.py:13253`）；`write` 是数据面，单列 `PRODUCER_IMPORT_WRITE_RATE_LIMIT_PER_MIN`（默认放宽到 600/min，env 可调），仍 per-installation token bucket、超限 `429 rate_limited + Retry-After`（`_plugin_rate_limited_response`，`app.py:13192`）。`write` 不进像素预算/并发信号量（那两道闸是 regions 专用，`app.py:13239-13247`）。

### 1.2 `POST /api/plugin/v1/imports/begin`

请求（JSON）：

```jsonc
{
  "grant_id": "pig_…",            // 必填：用户导入委托 grant（§2.2），owner 只来自它
  "project_id": "…",              // 必填：目标项目；begin 与 commit 双重校验归属
  "filename": "sample.tif",       // 产物名（清洗入 slide_store.sanitize_original_filename）
  "format_ext": "tif",            // 期望产物扩展（.tif/.ome.tif 家族走 slide_format_registry）
  "declared_size": 123456789,     // 0 < declared_size <= UPLOAD_MAX_REQUEST_BYTES(10 GiB)
  "scratch_bytes": 234567890,     // 插件侧 scratch 初始申报（§4；可为 0 表示先无本地副本）
  "profile": {"photometric": "brightfield", "channels": 3},  // 自由 JSON，≤4 KiB，仅存证
  "idempotency_key": "…"          // 可选；缺省也要求 Idempotency-Key 头（二者必须相等）
}
```

行为（单事务）：grant 复核（§2.3）→ 项目归属复核（§2.5）→ 幂等重放裁决 → `slide_store.allocate_slide(grant.user_id, …)` 预分配 staging/id_bundle 资产（`slide_store.py:281`，创建即绑定 slide_id，镜像 ingestion P4-b `ingestion_store.py:25-27`）→ 建 `producer_imports` 行（state=`created`，commit_token 随机）→ **final 预约准入即绑定** `reserve_upload_locked(holder_kind="producer_import", holder_id=import_id, purpose="final")`（`upload_guard.py:351/362-365`，无未绑定窗口）→ scratch 预约同款 purpose=`"scratch"`（`scratch_bytes>0` 时；`upload_guard.py:104-108` 需扩枚举，见 §4）。

响应 `201`：

```jsonc
{"import_id": "pim_…", "slide_id": "sld_…", "state": "created",
 "write_token": "…",              // 任务级凭证（§2.3 第 3 层），仅本次返回一次
 "chunk_max_bytes": 67108864,     // 单块上限（§1.3），固定 64 MiB
 "confirmed_offset": 0, "idempotency_key": "…"}
```

同键重放：state 未终态 → 原样返回（`write_token` 不重发，凭 status+原 token 续传）；已终态 → 返回终态回执。`declared_size` 漂移（同键不同值）→ `409 idempotency_conflict`。

### 1.3 `POST /api/plugin/v1/imports/<import_id>/write`（有界流 + offset 续传）

请求：`Content-Type: application/octet-stream`；头 `X-Import-Offset: <int>`、`X-Import-Chunk-Sha256: <hex64>`、`Authorization: Bearer <JWT>` + `X-Import-Token: <write_token>`。body 是**一块**（1 B–64 MiB）。

行为：

1. JWT + `slide:import` scope + `write_token` 与任务行匹配（不匹配 → `403 forbidden`，不区分哪一层错）。
2. state 必须 ∈ {created, writing}；否则 `409 import_state_invalid`（含当前 state，供插件决策）。
3. offset 必须 == `confirmed_offset`；落后/超前 → `409 offset_conflict`，`details.expected_offset` 给出权威值（断线恢复：先 `GET status` 再从权威 offset 续传，§7.2 第 2 步）。
4. **写前容量闸**：`confirmed_offset + len(chunk)` ≤ `declared_size`；不满足 → `413 size_exceeded`（先 `POST …/topup` 补占再写，§4）。磁盘水位沿用 `upload_guard.check_disk_watermark`（507）。
5. 在 `task_storage_lock("producer_import", import_id)` 文件锁内（锁协议 `task_storage_lock.py:13-21`；KINDS 需扩，见 §4）追加写 `.staging/<import_id>/<commit_token>/data`（`slide_storage.staging_dir`，`slide_storage.py:155`），逐块校验 sha256（不符 → `409 checksum_mismatch`，confirmed_offset 不动，块作废可重发）；fsync 后事务更新 `confirmed_offset/received_bytes`，返回 `{confirmed_offset, remaining_final_bytes}`。
6. 流式落盘走 `save_limited` 同款逐块计数（`upload_guard.py:181`），不信任 `Content-Length`；单块计入 `UPLOAD_MAX_REQUEST_BYTES`（10 GiB，`upload_guard.py:71`）由 Werkzeug `MAX_CONTENT_LENGTH` 第一层拦。

平台**不接受**任意本机路径/URL（§7.2）：body 永远是字节流本身。

### 1.4 `POST /api/plugin/v1/imports/<import_id>/commit`

请求：`{}`（可带 `"declared_sha256"` 作交叉核对，不作信任依据——§7.2「不能信任客户端完整哈希声明」；平台自算值为权威。**声明不符 → `422 declared_checksum_mismatch`**，不持久化 intent、任务保持 `writing`；插件核对后可带正确声明重 commit，或取消任务——§8 裁决 6）。

行为（顺序即语义）：

1. 鉴权同 §1.3；state ∈ {created, writing} 且 `confirmed_offset == declared_size`（不等 → `409 incomplete_write`）。
2. **平台自证**：全文件 sha256 平台计算；大小核对；格式探测（magic + `slide_format_registry` + `slide_io.open_slide` 可开、有金字塔层——镜像 `baidu_ingest._probe_native` `baidu_ingest.py:169-178`）；查看能力按平台当前 viewer 支持矩阵判定（不支持查看 → `422 format_unsupported`，任务转 `failed`，走清理）。
3. 持久化 **commit intent**（CAS，同事务置 state=`committing` + `commit_intent_json`，镜像 `ingestion_store.worker_persist_commit_intent` `ingestion_store.py:864`）：`slide_publish.build_intent` 载荷（`slide_publish.py:189`：slide_id/owner/manifest/sha256/accounted_bytes）+ task_ref/generation(=commit_token)。
4. intent 落库后取消被拒（`409 commit_in_progress`——镜像 `CommitInProgress` `ingestion_store.py:144`；「取消先赢只发生在受理前」）。
5. 调 `slide_publish.publish_with_channel(..., channel=ProducerImportPublishChannel)`（§3.3）完成 FS no-clobber 发布 + 结算短事务（锁序：advisory slide 锁第一把 → producer_imports 行 → slides 行 → upload_user_quotas → upload_reservations，与 `slide_publish.py:40-41`/`ingestion_store.py:17-19` 全仓一致）。
6. 结算同事务：slides CAS staging→ready + accounted_bytes + 内容 revision + **consume final 预约**（expect_holder）+ 项目关联（§2.5）。
7. 响应 `200`：`{import_id, state: "published", slide_id, revision, sha256, accounted_bytes, cleanup_status}`。**回执幂等**：响应丢失后重发 commit / 读 status 均返回同一回执，不再建资产、不再结算（`is_settled` 幂等出口，镜像 `slide_publish.py:384-385`）。

commit 是**同步**端点（首版无 202 异步段）；大文件校验（sha256 全量计算）在请求线程完成，插件侧超时自理。崩溃在 intent 后 → 恢复路径重跑 publish 收口（no-clobber + verify_bundle 幂等吸收，`slide_publish.py:421-453`）。

### 1.5 `GET /api/plugin/v1/imports/<import_id>/status`

只读幂等。返回 `{import_id, state, slide_id?, revision?, confirmed_offset, declared_size, remaining_final_bytes, scratch_confirmed_bytes, cleanup_status, terminal_at?}`。终态含稳定回执（published/failed/cancelled + 原因码）。回执丢失按本端点读回，绝不重新 begin（§7.2 第 4 步）。

### 1.6 `POST /api/plugin/v1/imports/<import_id>/cancel`

- state ∈ {created, writing}：终态化 `cancelled` + **终态作废**（§3.4，staging 资产 CAS→failed）+ 平台 staging 树进本地清理 duty（§4.4）。final 预约**清理确认后释放**（不是立即）。
- state = committing：`409 commit_in_progress`（提交互斥，§7.2 第 4 步）。
- published/终态：幂等返回现状。
- 撤销授权：installation 匹配的任务可由插件撤；grant 用户（Cookie+CSRF）也可撤自己的任务（入口复用 PT 用户面，见 §2.2）。

### 1.7 `POST /api/plugin/v1/imports/<import_id>/scratch` 与 `POST …/cleanup-confirm`

- `scratch`：`{"delta_bytes": n}` 或 `{"total_bytes": n}`（二选一；重复同值幂等）——插件下载/转换推进时补占 scratch（§4.2）。响应 `{scratch_confirmed_bytes}`。
- `cleanup-confirm`：插件声明其受管任务根已清理（§5）。平台核验（受管根非空即 `409 cleanup_not_verified` + `details.residual_bytes`）后在短事务内：scratch 预约按持有者释放（`release_reservation_locked(expect_holder=("producer_import", import_id))`，`upload_guard.py:700/714-738`）+ 置 `plugin_cleanup_status=cleaned`。**重复调用幂等**；失败可重试。published + cleanup 未确认 = 保留已发布结果与清理状态，不重复上传（§7.3 任务序）。

### 1.8 新增错误码（进 `_PLUGIN_ERROR_RETRYABLE`）

| code | HTTP | retryable | 场景 |
|---|---|---|---|
| `import_not_found` | 404 | false | 任务不存在/非本 installation |
| `import_state_invalid` | 409 | false | 状态机非法操作 |
| `idempotency_conflict` | 409 | false | 同键异载荷（漂移拒绝） |
| `offset_conflict` | 409 | false | offset ≠ confirmed_offset |
| `checksum_mismatch` | 409 | false | 块 sha256 不符 |
| `incomplete_write` | 409 | false | commit 时字节不齐 |
| `commit_in_progress` | 409 | false | intent 后取消被拒 |
| `size_exceeded` | 413 | false | 超 declared_size，先 topup |
| `upload_quota_exceeded` | 413 | false | 透传 `upload_guard.QuotaExceeded` |
| `disk_watermark_exceeded` | 507 | true | 透传 `DiskWatermarkExceeded` |
| `format_unsupported` | 422 | false | 平台验证/查看能力不过 |
| `import_grant_invalid` | 403 | false | §2.3 任一失败（reason 细分） |
| `cleanup_not_verified` | 409 | false | 受管根非空 |

## 2. 授权模型

现状（2026-09-29 核实，§1.1）：插件写权限只有 `annotation:write`；用户委托只有按切片的 run grant（`_verify_run_grant` `app.py:17627`，TTL 默认 30 min，`app.py:12939`）；JWT scope 是全安装固定 5 项常量（`_PLUGIN_JWT_SCOPES` `app.py:12934`）——**没有**任何插件导入切片的入口。本节为此新增两层授权，不复用 run grant（run grant 绑 slide+session，语义是「替我标注」，不是「替我入资产」）。

### 2.1 新安装 scope：`slide:import`

- `plugins/manifest.schema.json` permissions 枚举（`:85-97`）与 `plugins/sdk/manifest.py` `MANIFEST_PERMISSIONS`（`:37`）各加一项 `slide:import`（manifestSchemaVersion minor bump；老插件零迁移）。
- 申请不建立信任：安装仍走 owner-only `/api/admin/plugins/install`（`app.py:13720`）+ source-policy manifest sha256 pin（`plugins/source-policy.json`、`app.py:2885`）。**admin 批准 = 安装行记 `approved_scopes`（含 `slide:import` 与否）**；未批准的申请在安装时被拒（fail-closed，登记失败 = 安装失败，现状语义 `app.py:13722-13727`）。
- JWT 发放改为**按安装行裁剪**：`/api/plugin/v1/auth/token`（`app.py:13500`）的 `scope` = 既有 5 项 ∩ 安装行 approved_scopes；`slide:import` 只出现在显式批准的安装。存量安装行无该字段 → 不发 `slide:import`（老 token 语义不变，避免存量自动提权——与 plugin-capability-layer-design §6.1「scope 缺口」防自动提权先例同向）。
- 逐端点 `_require_plugin_token("slide:import")`。

### 2.2 用户导入委托 grant（新形态，非 run grant）

新表 `plugin_import_grants`（0077，§3.1）：`(installation_id, user_id, project_id)` 绑定 + `expires_at` + `revoked_at`。语义：

- **创建（人类入口，PT 用户面）**：`POST /api/plugin/import-grants`（Cookie session + CSRF，与 `DELETE /api/ai/run-grants/<id>` `app.py:19615` 同面）：body `{plugin_id, project_id, ttl_seconds?}`。校验：登录用户、项目存在/未归档/**项目 owner 是本人**（沿用 `baidu_ingest.associate_slide` 的归属口径，`baidu_ingest.py:124-131`）。返回 `grant_id`（`pig_` 前缀）一次性展示给用户，由用户粘进/授权给插件 UI（插件 UI 经 HostBridge 或自身页面引导，C5 不强制桥协议改动）。UI 面板：`GET /api/plugin/import-grants` 列自己的活跃 grant、`DELETE /api/plugin/import-grants/<id>` 撤销。
- **默认 TTL 24 h**（run grant 的 30 min 对下载+转换+传输的多小时链路太短；env `IMPORT_GRANT_TTL_SECONDS` 可调）。
- **插件出示**：begin 请求带 `grant_id`；插件 JWT（installation）+ grant 双因子——installation 必须与 grant.installation_id 一致（镜像 run grant 的 installation 匹配，`app.py:17670-17671`）。
- **owner 只来自 grant.user_id**：请求体任何 owner/user 字段一律忽略；`allocate_slide` 的 owner、publish intent 的 owner、项目关联的 owner 全部取自 grant（§7.2「主体 owner 由可信委托确定」）。

### 2.3 校验链（begin 与每个写/提交/清理操作都重跑）

1. JWT 有效 + installation enabled（现有每请求回查，`app.py:13378-13380`）。
2. grant 存在、未撤销、未过期 → 否则 `403 import_grant_invalid`（reason：`grant_not_found/grant_revoked/grant_expired/installation_mismatch/project_mismatch/user_not_allowed`——稳定 reason，镜像 `plugin_v1_run_grant_verify` 的 reason 枚举，`app.py:18525-18528`）。
3. **任务级凭证**：begin 返回的 `write_token`（服务端随机、存任务行哈希）绑定唯一 import_id；write/commit/cancel/scratch/cleanup-confirm 都要求持有（§7.2「返回只能作用该任务的凭证」；恢复/重试长期授权绑定具体 job——token 与任务同寿命，grant 过期后**在途任务仍可用 token 收尾清理**，但不能再 begin 新任务）。
4. **创建者复查**（每次操作）：grant.user 对应用户存在且未禁用；项目仍存在、未归档、owner 仍是该用户（镜像 `_run_grant_creator_allowed` 的复查模式，`app.py:17594-17624`）。复查失败 → 新操作拒绝；在途提交/清理见 §2.4。

### 2.4 撤销 / 插件停用的分层语义（§7.2 末段）

| 事件 | 新操作（begin/新 write 块） | 在途 commit（intent 已落库） | 清理 |
|---|---|---|---|
| grant 撤销/过期 | 403 `import_grant_invalid` | **继续完成**（恢复路径收口 publish/settle；不把已进提交段的任务当失败删掉——§8 口径） | 允许（cleanup-confirm 不需要活跃 grant，只需 write_token + installation） |
| 插件 disable | 401（JWT 每请求回查 enabled） | **继续完成**（结算不依赖插件存活；恢复路径是平台自身 duty） | 允许（同上；write_token 校验不查 enabled，只查 installation 行存在） |
| 用户禁用/项目移交 | 403（复查失败） | 继续完成；项目关联失败记 `project_associate_state=failed`（产物不回滚） | 允许 |

### 2.5 项目归属与关联（提交事务内）

- begin 复核 + commit 锁内复核：project.owner_user_id == grant.user_id、未归档（同 `baidu_ingest.py:124-131`）。**本合同不开放「项目成员级」导入目标**——与现有百度入口口径一致（成员/协作者可入项目是未来独立变更，开放时必须同步改 `_subject_slide_permissions` 族，不在 C5）。
- 关联动作：settle 事务内 `share_store.add_slides_to_project(project_id, [], slide_ids=[slide_id])`（幂等：已在项目 → succeeded，`baidu_ingest.py:132-137`）；关联失败不回滚发布资产，置 `project_associate_state=failed` 并保留在 status 回执中（现状百度语义，`baidu_ingest.py:122`）。

### 2.6 agent/AI 工具调用约束

- producer import 端点**只接受 plugin JWT 主体**；agent-tool-token 调用一律 401（`_agent_tool_token_decode` 按 aud/typ 拒绝，`app.py:13118-13135`——与 plugin JWT 调 dispatch 被 403 的域隔离同构，`app.py:19014-19025`）。
- C5 不向 agent 注入任何导入写能力：dispatch 注册表校验层继续拒绝 `accessMode:"write"`（plugin-capability-layer §6.2 P1 裁决；`plugins/sdk/manifest.py` `validate_provides`）。若未来允许「AI 代用户触发百度导入」，必须走 §6.2 的写能力 + run grant 扩展 + 用户显式勾选，另行评审——C5 的 agent 最多**只读**查询插件侧导入状态（插件可声明 read 能力）。
- 任何插件侧自动重试服从本文 token/grant 生命周期，不得自造永久宽泛凭证。

## 3. 数据模型（迁移 0077 提案——只提议，不写 SQL 文件）

### 3.1 表

**`producer_imports`**（通用 producer job 表，§7.3「轻量 producer job 表」）：

- 身份与授权：`import_id` PK（`pim_` 前缀）、`installation_id`、`plugin_id`、`grant_id`、`owner_user_id`（= grant.user_id，冻结不可改）、`project_id`、`idempotency_key`（UNIQUE(installation_id, idempotency_key)）、`payload_sha256`（begin 载荷摘要，漂移裁决用）。
- 产物：`slide_id`（begin 预分配、唯一绑定源）、`filename`、`format_ext`、`declared_size`、`confirmed_offset`、`received_bytes`、`sha256_actual`、`profile_json`。
- 容量：`final_reservation_id`、`scratch_reservation_id`、`scratch_confirmed_bytes`。
- 提交：`commit_token`（generation/fencing 键）、`commit_intent_json`、`commit_started_at`、`write_token_hash`。
- 清理 duty（镜像 ingestion `local_cleanup_*` 四列 + 插件侧一组）：`local_cleanup_status/attempts/last_error/next_retry_at`（平台 staging 树）；`plugin_cleanup_status/attempts/last_error/next_retry_at`（受管根，§5）。
- 关联与时间：`project_associate_state`、`state`、`fail_code`、`created_at/updated_at/terminal_at`、`deadline_at`（begin + `PRODUCER_IMPORT_MAX_AGE`，默认 72 h，超期 sweep 终态化——镜像 ingestion 绝对期限 `ingestion_store.py:32-38`）。

**`producer_import_events`**：`(id, import_id, kind, detail)`，detail 经 `_sanitize_detail` 同款脱敏（禁 sign/secret/token/url 键，`ingestion_store.py:235-247`）。

**`plugin_import_grants`**：`(grant_id PK, installation_id, plugin_id, user_id, project_id, created_at, expires_at, revoked_at)`；UNIQUE 无（同用户可对同项目持多 grant）；撤销幂等。

不新增任何 COS/bucket/object_key/upload_id/conversion 列——本通道没有这些概念（§8「不填假值」）。

### 3.2 状态机

```
created → writing → committing → published → done
   │          │          │(恢复重跑 publish，幂等)
   └────┬─────┘          ├→(清理确认后)→ done
        ↓                ↓
  cancelled/failed（终态，带平台+插件两侧清理 duty；duty 收口后 → done）
```

- 合法转移表封闭（非法跳转 `409 import_state_invalid`，镜像 `LEGAL_TRANSITIONS` `ingestion_store.py:86-102`）。
- `published` = 发布结算成功、清理未毕；`done` = 双侧清理确认 + （如适用）scratch 释放。`published + cleanup_failed` 保留已发布结果与清理状态（§7.3）。
- 插件自身任务序 `queued→downloading→transforming→validating→delivering→awaiting_receipt→cleanup_pending→done`（§7.3）归**插件侧**状态，不经本表——本表只看平台视角的产物通道状态；插件可在 `profile_json`/事件里自带回显。

### 3.3 `ProducerImportPublishChannel`（接入唯一发布编排）

实现 `slide_publish.PublishChannel` 协议（`slide_publish.py:119-149`），逐钩子：

- `load_task(task_ref=import_id)` → producer_imports 行。
- `is_settled(task)` → state ∈ {published, done}（重复 commit/响应丢失重试的幂等出口）。
- `decode_intent` → `commit_intent_json`（损坏 `{}` → fail-closed 冲突）。
- `task_commit_token(task)` → `commit_token`。
- `precheck_locked`：行 FOR UPDATE → state==committing + token 匹配 + slide_id 绑定一致 + owner（grant 冻结值）与 intent 一致（不一致=不变量破坏隔离告警，不自动修正——`slide_publish.py:235-246` 同口径）+ final 预约 `renew_reservation_locked` 续租核验持有与归属（镜像 `ingestion_store.py:1071-1080`）。
- `settle`：短事务 [advisory slide 锁 → producer_imports 行 → slides CAS staging→ready + accounted_bytes + record_revision → consume final（expect_holder=("producer_import", import_id)）→ 项目关联（§2.5）→ 行收口 state=published + local/plugin cleanup=pending]，镜像 `worker_settle_ready` 六步（`ingestion_store.py:896-1005`）。

发布编排（六步、no-clobber、恢复幂等）**只此一份**，在 `slide_publish.publish_with_channel`；本通道不得复制（§7.2 第 3 步「唯一 slide_publish」）。

### 3.4 终态作废（terminal void，镜像 R16 `_abandon_staging_asset`）

`_void_staging_asset_locked(cur, job)`：终态（cancelled/failed/expired）事务内把 `slides` 行 staging→failed CAS（保留证据、不可读），仅当 state 仍 staging；CAS 失败 = 不变量破坏 fail-closed 记事件。语义与锁位与 `ingestion_store._abandon_staging_asset`（`ingestion_store.py:282-311`）一致——producer 无子任务（无 zip item/held conversion），故只作废本行 slide_id。staging 残留文件清理由平台本地清理 duty 负责（§4.4），`failed` 行保留。

## 4. 容量合同（§7.3）

### 4.1 持有者扩展

- `upload_guard.HOLDER_KINDS` += `"producer_import"`；`HOLDER_PURPOSES["producer_import"] = frozenset({"scratch", "final"})`（`upload_guard.py:100-108`）——同一 holder_id（import_id）可持两份不同用途预约；`reservation_holder_matches` 只比 (kind,id)（`upload_guard.py:317`），两份预约都匹配，靠任务行的两个 reservation_id 列区分用途。
- `task_storage_lock.KINDS` += `"producer_import"`（`task_storage_lock.py:57-63`），锁路径 `.task-locks/producer_import/<import_id>.lock`。
- `scripts/reconcile_upload_capacity.py` 学习新 holder kind：`_PURPOSE`/`_HOLDER_ID_KEY`（`:65-67`）加映射，状态 SQL 从 producer_imports 取（活跃 = 非 done 且有未收口责任；对照 `_RESERV_SQL` `:81`），冻结计划/核账覆盖该 holder（§7.3「冻结计划与容量对账需涵盖该类 holder」）。`scripts/upload_drain.py` 的核账阻断（`recon.collect + plan_actions`）随之自动涵盖；另在 `audit` 清单列出 producer 暂存树残留证据（逐成员扫描与核账同一实现）。

### 4.2 scratch vs final

- **final**：begin 按 `declared_size` 预占；发布结算 consume 转实占（实际字节）；取消/失败走清理确认后 release。
- **scratch**：插件本地下载+转换+输出副本的责任（同盘同责，§7.3）。begin 申报初值，`POST …/scratch` 补占（写下一阶段前请求；额度拒绝即停——不赌）。**释放只经 cleanup-confirm**（受管根核验通过），不接受插件一句 cleaned（§5）。
- 计费主体都是 grant.user_id 的平台配额行（`upload_guard` 唯一财务实现，§7.3「插件没有主库账号」）；quota_applies 判定同现状（role=user 受限、owner 不受限，`upload_guard.py:233-238`）。
- top-up 锁序与 `topup_reservation`（quota→reservation，`upload_guard.py:532-539`）一致；续租语义：绑定预约容量从不被 TTL 回收，租约过期只停执行许可（`upload_guard.py:256-274`）——插件失联保留责任并进对账，不靠 TTL 消失（§7.3）。
- 统一锁序（全通道恒定）：**文件锁最外 → advisory slide 锁 → producer_imports 行 → slides 行 → upload_user_quotas → upload_reservations**（0072/R10 全仓锁序 `upload_guard.py:690-697`、`slide_publish.py:40-41`）。

### 4.3 write 的 top-up 规则

`declared_size` 是 final 预约上界：confirmed_offset+len(chunk) 超界 → 插件先 `POST …/topup {"extra_bytes": n}`（final 用途补占，复用 `topup_reservation` 语义）再重发块；配额不足 `413 upload_quota_exceeded` 即停（可恢复错误，不降质量不改数据）。

### 4.4 清理 duty（双侧，镜像 ingestion 本地清理编排）

- **平台侧**（`.staging/<import_id>/` 树）：终态后 `local_cleanup_status=pending`；调度步进 `retry_local_cleanups` 同款（锁等待超时 ≠ 删除成功，`ingestion_store.py:1717-1760`）：task_storage_lock → 锁内重验终态 → `remove_staging_tree`（`slide_storage.py:179`）→ `confirm_local_cleanup` 释放 final（未 consume 时）→ cleaned。失败有界重试转 failed，容量保留告警，**不用 TTL 抹责任**（`ingestion_store.py:1800-1837`）。
- **插件侧**（受管根）：`plugin_cleanup_status` 同款四态；`cleanup-confirm` 端点驱动（§1.7）。
- published 后 final 已 consume（转 used），release 幂等 no-op（`upload_guard.py:728-729`）。

## 5. 插件 runner/清理信任边界（§7.3）

**平台验证（不信声明）**：字节（逐块+全量 sha256）、格式/查看能力、发布 no-clobber、配额记账、自身 staging 树清理、受管根**非空性**（cleanup-confirm 时平台列目录核对）。

**平台信任（显式列出）**：插件对「受管根下确无隐藏占用」的物理实现（平台只查非空，不查硬链接/外联副本）；插件进程的资源纪律（scratch 预约值是插件申报的账面值）。

**C5 机制**：

- **受管任务根**：插件工作目录固定为 `SHARE_DATA_DIR/plugin-work/<installation_id>/imports/<import_id>/`——平台派生、平台可列；插件不得提供任意路径，平台 API 任何端点不接受路径参数（§7.2）。
- **写者 fencing**：清理确认前插件必须先停写（插件任务序 `awaiting_receipt → cleanup_pending` 自证）；平台侧以 task_storage_lock 保证平台文件操作互斥；跨进程「插件已停写」由插件声明 + 受管根核对（首次列目录发现近期 mtime 异动 → 拒绝并退避重试）。
- **验证清理**：cleanup-confirm = 受管根存在且为空（或整个删除）才算 cleaned；非空 → `cleanup_not_verified` + 残余字节，scratch 不释放。

**明确延后（不在 C5）**：挂载命名空间/独立 UID 的强隔离、cgroup 级磁盘配额、受管清理器进程代删（§7.3「隔离挂载+受管清理器」的完整形态）。延后风险已由 scratch 预约 + 对账覆盖（账面责任始终在），物理隔离缺口在 C7 前评审（§8 裁决 5）。

## 6. 百度迁移映射（§7.1/§8）

### 6.1 留在平台（不迁）

- 用户面 API：`/api/remote-imports/baidu/*` 全部（`app.py:11216-11299`，Cookie+CSRF）：capabilities/enumerations/imports 的建批、列表、详情、取消、重试。
- `baidu_import_store` 的批次/条目/枚举/候选持久化与幂等（owner+key+digest，`baidu_import_store.py:802-811`）、配额准入（`quota_hook`→`reserve_upload`，`baidu_import_http.py:26-33`）、批次终态一次性 consume（`:92-102`）、`project_associate_state` 记录。
- 项目关联（§2.5）与统一发布（producer import channel）。

### 6.2 移入插件（pathtogether-baidu-import）

- 百度凭证/下载/重试限速（adapter 源站逻辑）、转换执行（现 `baidu_ingest.ingest_staging` 在 gunicorn 进程内同步 create/claim/process——§1.1 核实；这是迁出的首要收益）、源副本清理（`/apps/bdpan/<batch>/` 与本地 staging，`STAGING_ROOT` `baidu_import_store.py:79-80`）。
- 插件产出最终单文件后走本文 §1 端点交付；插件侧持有 grant（用户在插件 UI 授权）。

### 6.3 平台↔插件驱动桥（次要合同，C5 内冻结名称）

批次执行状态回写经机器通道（Bearer plugin JWT + scope `slide:import` + baidu 安装白名单，镜像 `_USAGE_INGEST_PLUGIN_IDS` 形态 `app.py:18570-18572`）：

- `POST /api/plugin/v1/baidu/batches/claim` / `heartbeat` / `items/<id>/report`（stage/error/ingest 引用）——保留 `baidu_import_store` 既有 lease/fencing 原语（`run_batch` 领取 CAS `:1505-1512`、`heartbeat_batch` `:1055`），只是把 claim 者从进程内 worker 换成插件后端。
- 条目发布不另开端点：插件对每个 item 走 §1 producer import（item.slide_id 预分配绑定逻辑保留在平台侧 begin，等价于 `_allocate_item_slide` `:1245` 的替位）。

### 6.4 存量过渡（§8 检查点 A 口径）

- 存量 `baidu_import_batches/items` 行**不迁移 schema**：A 前 frozen 旧执行路径继续排空在途批次；用户面列表对新旧行统一可读。
- 无法排空的批次按 §8 冻结迁移计划移交插件：接管 source 文件证据、容量 holder（`baidu_batch` 预约原样持有或经 `record_reconciled_residual_locked` 补记为 producer_import/scratch——**裁决：原样持有 `baidu_batch`，不换 holder**，避免双记账窗口——§8 裁决 8）、产物绑定与所有权；单一执行者接管 + 幂等回执；不重新生成已发布资产、不重新收费。
- 旧转换 item（conversion_job 路径）的交接服从 §8 转换链路自身裁决，不在本合同内。

## 7. C5 测试矩阵（源自方案 §10.4；每行 = 用例 → 断言）

| # | 用例 | 断言 |
|---|---|---|
| T1 | 受控替身全链路：下载→native core 转换→producer 导入→slide 可见 | slide_id ready 且 `authorize_read` 可读；项目含该 slide；final consumed=实际字节恰一次；scratch released 恰一次（对账脚本双向核账 0 差异） |
| T2 | 重复 begin（同键同载荷/异载荷） | 同载荷返回原 import_id/write_token 不重发；异载荷 409 `idempotency_conflict` |
| T3 | 重复 commit / commit 响应丢失后 status | 同一回执；无第二份资产/计费（slides 行唯一、reservation 单次 consume） |
| T4 | 重复 cancel / cleanup-confirm | 幂等；scratch 释放恰一次（release 幂等态可重放） |
| T5 | write 断线续传（错 offset） | 409 `offset_conflict` + expected_offset；续传后 confirmed_offset 单调、无空洞无重复字节 |
| T6 | 块 sha256 篡改 / 整体哈希与声明不符 | 409 `checksum_mismatch`（块级）；commit 声明与平台自算不符 → 422 `declared_checksum_mismatch`，无 intent、状态仍 `writing`（§8 裁决 6） |
| T7 | 超流长限制（块 > 64 MiB / declared_size 越界 / >10 GiB 产物） | 413 `size_exceeded`/`upload_too_large`；topup 后可续 |
| T8 | 伪造 owner / 任意路径 body / 越权 project | owner 恒 = grant.user_id（资产行核验）；无任何端点接受路径；非本人项目 403 `import_grant_invalid(project_mismatch)` |
| T9 | grant 过期/撤销、插件 disable、用户禁用 | 按 §2.4 表逐格断言：新操作拒绝码；intent 后 commit 恢复路径仍收口；cleanup-confirm 仍可用 |
| T10 | intent 竞态（cancel vs commit 并发） | 恰一方赢：intent 先 → `commit_in_progress`；cancel 先 → commit 409 `import_state_invalid`；无既发布又取消态 |
| T11 | 平台崩溃注入（intent 后/settle 前、FS 发布后/DB 前） | 恢复重跑 publish：no-clobber+verify_bundle 幂等收口；无「committed 文件 + 未结算」可见资产（DB ready 是唯一可见开关） |
| T12 | 插件重启（交付中途） | status+write_token 续传；无重复预约/重复块 |
| T13 | 发布成功清理失败（平台树删失败/受管根非空） | `published + cleanup_failed/pending`：结果保留、scratch 不释放、退避重试、对账可见 |
| T14 | 插件停止（disable+进程亡） | 核心原生 COS 上传/查看/删除回归全绿；producer 任务进对账清单不消失 |
| T15 | 未完成输入不可见/跨用户不可访问 | staging/failed 资产对任何主体（含 grant 用户）不可读；其他用户 status 404 |
| T16 | 百度映射回归 | 用户面列表新旧行统一；存量批次排空 no-go/go 证据（§6.4）；插件 claim/heartbeat/report lease 竞态被 fence |
| T17 | 配额/水位故障注入 | quota 拒绝→413 零外部副作用；磁盘水位→507；绑定预约不参与 TTL 回收（时间旅行测试） |
| T18 | 限流 | write 超速 429+Retry-After；控制面沿用 120/min 桶 |
| T19 | 对账/排空工具 | `reconcile_upload_capacity` 认得 producer_import holder（scratch/final）；drain audit 列出 producer 暂存残留证据 |

（真实百度账号下载为外部门禁，替身成功不宣称真实可用——§9.1。）

## 8. 裁决（2026-09-29 定稿，均按草案建议）

原为开放问题；用户确认按建议定稿。实现不得偏离，需变更先改本节。

1. **grant 覆盖多任务**：grant 绑 (installation, user, project)，TTL 内可 begin 多个任务；每任务另发 job 级 write_token（§7.2 的 job 绑定由 write_token 层满足）。
2. **grant 默认 TTL = 24 h**，env `IMPORT_GRANT_TTL_SECONDS` 可调（run grant 30 min 不适用长链路）。
3. **agent 触发导入：C5 不开放**（§2.6），留待 plugin-capability-layer P2 写能力评审。
4. **关联语义**：owner/项目权限校验在发布结算事务内完成；关联插入 PG 可同事务则同事务，否则紧邻同锁窗口插入 + 幂等状态收敛；关联失败不回滚已发布产物，记 `project_associate_state=failed` 并可重试（沿用 `baidu_ingest.py:122` 口径）。
5. **受管根隔离**：C5 采用平台派生目录 + cleanup-confirm 非空核验；挂载命名空间/独立 UID 强隔离延后到 C7 前评审（§5 已列风险）。
6. **declared_sha256 不符 → 422 `declared_checksum_mismatch`**（fail-closed）；不持久化 intent、状态保持 `writing`，插件可纠正声明后重 commit 或取消。
7. **scratch 不设倍率硬上限**；由对账兜底 + admin 可见，列为观测项（记录每任务 scratch 申报/实际峰值比）。
8. **百度存量 holder**：原样持有 `baidu_batch`，不迁移为 producer_import（§6.4），免冻结窗口双记账。
