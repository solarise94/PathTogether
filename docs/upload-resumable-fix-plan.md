# 上传修复方案：CSRF 立即修复（P0）+ Upload V2 分片续传（P1）

> 状态：方案（未实施）。本次只做设计与审查，不改代码。
> 关联：`docs/open-registration-security-remediation.md`（P0-A §3.3 资源防护，本方案的配额/水位/在途机制即源自该文档）、`docs/account-system-simplification-fix-plan.md`、`docs/ai-viewport-observation-annotation-fix-plan.md`。
> 核实基线：2026-08-25 源码与测试审查。**未启动新的测试服务**，但引用了现有部署的只读证据（线上 access log、容器内文件哈希）。审查发现、glm-coder 全量核查与主代理独立核对三方一致；另经一轮设计 review 修订（见各节"review 修订"）。

## 0. 背景：为什么 1GB 传不上来

直接原因**不是** 20–36 Mbps 带宽，而是一个确定的上传 CSRF bug；同时当前单请求上传架构也确实不适合大切片。本次审查确认两条独立问题：

1. `[P0]` 生产前端上传漏传 CSRF token，服务端 400 拒绝（详见 §1）。
2. `[P1]` 当前是一次性 multipart POST，无断点续传、且存在"接收即二次落盘"，弱网大文件风险高（详见 §2）。

线上证据（2026-08-25）：最近两次 `/api/upload` 都是 400；第二次响应体 26 字节，恰等于 `jsonify(error="csrf_required")` 的实际输出长度（25 字节 JSON + jsonify 追加的换行）；线上容器 `app.py`、`static/app.js` 哈希与当前源码一致。

## 1. P0：上传 XHR 漏传 CSRF token

### 1.1 根因（三方核实一致）

- 前端唯一上传入口 `uploadFile()`（`PathTogether/static/app.js:3812`）用裸 `XMLHttpRequest`，`xhr.open("POST","/api/upload")` + `send(formData)`，**全文件 0 处 `setRequestHeader`**。拖拽（`app.js:3857`）与文件选择（`app.js:3880`）都汇入它。
- 其余约 30+ 处写请求全走 `apiFetch()`（`app.js:59-63` 自动附加 `X-CSRF-Token`）。绕过 apiFetch 的裸 `fetch()` 仅 6 处且全是 GET 只读。**唯一携带请求体却绕过 apiFetch 的就是上传。**
- 服务端统一 CSRF 钩子 `_csrf_protect`（`PathTogether/app.py:942-964`，`@app.before_request`）覆盖所有 Cookie 会话写端点；豁免前缀仅 `/internal/`、`/api/plugin/`、`/api/demo/`（`app.py:894`），`/api/upload` 不在内。缺 token → `app.py:964` 返回 `{"error":"csrf_required"}` 400。
- **免认证部署（AUTH_ENABLED=False）同样中招**：用户 GET 页面时已由安全方法分支 `ensure_csrf_token()`（`app.py:952-955`）拿到绑定 token 的 session，但裸 XHR 只带 cookie 不带头，`_csrf_validate` 在 `app.py:934` `not submitted` 判失败。即两种部署模式下大文件上传都是坏的。
- 钩子顺序：`_require_auth`(770) → `_plugin_v1_rate_limit`(827) → `_csrf_protect`(942)。

### 1.2 加重因素：为校验 CSRF 先解析整个 1GB body

`_csrf_submitted_token`（`app.py:920-925`）**先 `request.form.get(CSRF_FORM_FIELD)` 后查 header**。对 multipart/form-data，Werkzeug 访问 `request.form` 会整体解析 body（大文件 spool 到系统临时文件），只为找一个不存在的表单域，之后才轮到"免费"的 header 检查。于是整个 1GB 传完才被 400 拒绝——这就是"进度条到 100% 后失败"的表象。这是加重因素，不是根因。

### 1.3 测试盲区（为什么测试没拦住）

`PathTogether/tests/_pt_helpers.py:23` 的 `CsrfClient` 对所有写方法自动注入 `X-CSRF-Token`（模拟"正确前端"）。`test_upload_guard.py` 的上传调用全部经它包装。现有 `csrf_required` 断言只覆盖 `/login`、`/logout`、`/api/admin/users`、`/api/ai/config`、invites、改密等，**没有任何对 `/api/upload` 发无 token 原始请求的用例**。

### 1.4 P0 修复（小、可独立先行）

1. **前端补头**：`uploadFile()` 在 `xhr.open` 后、`send` 前加
   `xhr.setRequestHeader("X-CSRF-Token", csrfToken())`（复用 `app.js:51` 的 `csrfToken()`，从非 HttpOnly cookie 读）。
2. **服务端 CSRF 契约改为"API 只认 header"**（review 修订）：`/api/*` 写接口**只接受 `X-CSRF-Token` header，不回退 `request.form`**；表单域回退只保留给 `/login`、`/register` 等 HTML 表单路径。这样无 token 的 multipart 请求在校验时**不访问 `request.form`**，能在消费 body 之前即拒绝——否则"无 token 立即拒绝"的验收无法成立（见 §7）。
3. **补双层回归测试**：(a) pytest 裸 client（不包 CsrfClient）POST `/api/upload` 无 token → 400 `csrf_required`；(b) **vitest 按 `logout.test.ts` 的 loadApp harness 断言真实 `uploadFile()` 发出的请求携带 `X-CSRF-Token`**——仅测服务端"无 token 返 400"不能证明前端已修复。
4. **前端错误展示**：`uploadFile` 的 `load` 分支已显示 `data.error`（`app.js:3836`），会把 `csrf_required` 原样透出；建议把已知错误码映射为可读文案（见 §3.6），不再只显示"网络错误"。

> 影响面：前端一行 + 服务端 CSRF 取 token 改为"`/api/*` 只认 header、HTML 表单才回退 form 域"的**契约调整** + 测试。需注意：任何仍依赖 multipart 表单域 `csrf_token`（而非 header）的 `/api/*` 调用方会受影响——上线前需核对现有 `/api/*` 写请求是否都走 header（apiFetch 已是 header，uploadFile 本方案补 header）。

## 2. P1：架构性缺陷——无断点续传 + 重复落盘

### 2.1 无断点续传（核实成立）

- 全仓仅 `POST /api/upload` 一次性 multipart；无 chunk/offset/resume/tus/分片接口（glm-coder 全量反证搜索为空）。
- 断线、刷新、frp 抖动都要从 0 重传；无暂停/恢复/分片重试。
- 浏览器进度 100% 只代表数据发完，不代表服务端校验和入库完成。
- 多文件选择并发启动多个 `uploadFile`，却共用同一个进度条（`app.js:3854-3858` 循环写同一 `els.progressBar`）。

带宽不是主因：1GB 在 20–36 Mbps 下理论约 3.7–6.8 分钟、实际约 5–10 分钟，可以传；真正问题是"一次失败全重来"。

### 2.2 接收阶段二次落盘 + 防护时机晚（核实成立）

上传路由（`app.py:4440` `api_upload`）执行顺序：

1. `app.py:4457` 先访问 `request.files` —— **Werkzeug 此时已把整个 multipart 解析，大文件 spool 到系统临时文件**。
2. `app.py:4461` `_upload_acquire_reservation` 才做配额预占（PG 权威）。
3. `app.py:4534` `upload_guard.save_limited(file.stream, tmp)` 再从已暂存的 `file.stream` **复制**到 `UPLOAD_DIR/.uploading-*`。
4. `app.py:4548` 原子 `os.link(tmp, dest)` 提升；`app.py:4563` `_validate_slide_file` 校验；之后建立 ownership（zip 分支在 `app.py:4510-4516`）。

后果：

- **接收即双份落盘**：`file.stream` 源头是 Werkzeug `SpooledTemporaryFile(max_size=500*1024)`（实测 Werkzeug 3.1.7）——超过 500KB 即滚入系统临时目录真实文件。数 GB 切片必然先全量落系统临时盘，再由 `save_limited` 复制到 `UPLOAD_DIR/.uploading-*`，请求存续期内**双份落盘**（zip 分支约 3 份：系统临时 + zip 暂存 + 解压目录）。
- 配额 reservation、磁盘水位（`save_limited` 前的 `check_disk_watermark`）、在途限制（默认 3，`upload_guard.py:75`）、每小时限流都在 `request.files` 解析**之后**才执行，**不能保护接收阶段**；接收期唯一防护是 `MAX_CONTENT_LENGTH`（10GiB+1MiB slack，`app.py:139-140`）的粗粒度兜底。在途上限 3 意味着瞬时落盘峰值可再乘 3。
- 断线整文件重传，且配额/每小时限流仍计入失败尝试。
- 默认上限：`UPLOAD_MAX_REQUEST_BYTES = 10 GiB`（`upload_guard.py:61`）。1GB 没撞上限。

### 2.3 附带发现：单文件分支先提升后验证（与 docstring 相反）

路由 docstring（`app.py:4449`）宣称"验证成功后原子 link/rename 提升"，但单文件分支实际是 `4548 os.link(tmp,dest)` 提升**之后**才 `4563 _validate_slide_file(dest)` 试开验证，失败再 `dest.unlink` 清理。文档与实现相反；Upload V2 应把"验证成功"放在"原子提升/入库"之前（commit 校验总大小+哈希+可打开后才提升）。

## 3. Upload V2：分片续传设计

目标：真正流式（单次落盘）、断点续传、配额在初始化即预占、弱网只重传失败分片。

### 3.0 review 修订：先定状态机与配额一致性，再谈接口

原稿有几处无法直接实现，必须先补设计：

- **任务保留期（≥24h）与 reservation TTL（默认 1800s，`upload_guard.py:84`）不一致**：惰性回收后额度被释放、任务仍可写，形成配额超占。→ **上传任务必须做 reservation 续租**（见 §3.2.4）。
- **"并发 1–2 片"与单一 `confirmed_offset` 冲突**：若允许并行需持久化分片 bitmap。→ **首版裁定为严格串行**：只接受 `offset == confirmed_offset`（见 §3.2.2）。
- **"服务端边收边滚动 SHA-256"无法跨重启/重试恢复**。→ **裁定为：每片 SHA-256（客户端上报）+ commit 时服务端完整复算整文件哈希**（见 §3.2.3），不再二选一。
- **ZIP/MRXS 首版不进 V2**：只支持单文件 WSI；ZIP/MRXS 暂留旧 `/api/upload`（见 §3.4）。

### 3.1 数据模型与状态机（新增）

新增持久化表 `upload_tasks`（PG 权威；json 后端用等价文件记录，沿用 dual-backend 约定）：

| 字段 | 说明 |
|---|---|
| `upload_id` | PK |
| `owner_user_id` | 归属；任务按 `(upload_id, owner_user_id)` 绑定，他人 403（不泄露存在性） |
| `filename` / `safe_name` | 预校验后的目标名 |
| `declared_size` | 声明总字节（创建时校验 ≤ 单文件上限） |
| `chunk_size` | 服务端选定的分片大小 |
| `confirmed_offset` | 服务端已确认的字节偏移（串行模型下即唯一写入点） |
| `last_chunk_offset` / `last_chunk_length` / `last_chunk_sha256` | 最后一个已确认分片的坐标与哈希（用于重复 PUT 幂等比对，见 §3.2.1） |
| `sha256_expected` | **可选**：客户端创建时上报的整文件哈希（见 §3.2.3 裁决）；缺省时 commit 以服务端复算值为权威 |
| `sha256_actual` | commit 时服务端复算得到的整文件哈希（最终权威值） |
| `reservation_id` | 关联的配额预占 |
| `state` | `active → committing → committed` / `committing → active`（临时故障）/ `committing → failed`（确定性失败）；`active|failed → cancelled`；`active → expired` |
| `commit_token` / `commit_started_at` | commit 三段式的令牌与起点（见 §3.2.5） |
| `expires_at` | 任务 TTL（默认 24h，见 §3.2.4） |
| `created_at` / `updated_at` | |

**状态机**：

```
active --(PUT chunk, offset==confirmed_offset)--> active          # confirmed_offset 前进
active --(POST commit 受理)--> committing                          # 短事务：写 commit_token、续租
committing --(commit 收尾成功)--> committed
committing --(临时基础设施故障,事务外)--> active                   # 可重试 commit
committing --(确定性失败:哈希不匹配/非法切片)--> failed            # 见下：只能取消重传
active --(DELETE / TTL 到期)--> cancelled / expired                # 清理临时文件+释放 reservation
failed --(DELETE)--> cancelled                                     # 原内容不可改，只能取消后重新上传
```

**失败类型区分**（review 修订）：

- **临时基础设施故障**（磁盘抖动、锁超时、进程崩溃）：回到 `active`，允许重试 commit。
- **确定性失败**（整文件哈希与 `sha256_expected` 不匹配、`_validate_slide_file` 判定非法）：进入 `failed`，**原内容不可修改**，不能"修复后重新 commit"；只能取消（清理临时文件、释放 reservation）后重新上传。

**跨 worker 锁**：每个任务的**状态转移**在短事务内 `SELECT ... FOR UPDATE` 锁 `upload_tasks` 行。**注意：整文件哈希/OpenSlide 验证/文件提升不在行锁内**（见 §3.2.5 三段式，避免长事务）。

### 3.2 接口与关键契约

| 方法/路径 | 说明 |
|---|---|
| `POST /api/uploads` | 创建任务：预校验文件名、`declared_size` ≤ 上限、配额**初始化即 `reserve_upload(declared_size)`**；返回 `{upload_id, chunk_size, confirmed_offset:0}`。 |
| `PUT /api/uploads/<id>/chunk` | 原始二进制 body（非 multipart）+ `Content-Range`/`offset` + 长度 + **本片 SHA-256**。 |
| `GET /api/uploads/<id>` | 返回 `{state, confirmed_offset, chunk_size, expires_at}`（服务端权威，供刷新恢复）。 |
| `POST /api/uploads/<id>/commit` | 始终流式复算整文件并保存 `sha256_actual`；**仅当客户端提供了 `sha256_expected` 时才与之比对**（不一致即确定性失败）。校验总字节 == declared_size → 切片有效性检查 → 原子改名提升 → ownership 入库 → reservation 转实占。 |
| `DELETE /api/uploads/<id>` | 取消：清理临时文件、释放 reservation。 |

权限：`_require_auth` + `can_upload()` + `(upload_id, owner)` 绑定校验。CSRF：Cookie 会话写端点走统一 `_csrf_protect`，且这些是 `/api/*`，**只认 header**（见 §1.4）。

#### 3.2.1 重复 PUT 幂等（review 修订：只比对该偏移的分片哈希）

以 `(offset, length, sha256)` 为幂等键，对照最后确认分片（`last_chunk_*`）：

- `offset == confirmed_offset`：正常新分片，写入并推进。
- `offset < confirmed_offset`：仅当 `offset == last_chunk_offset` 且 `length`/`sha256` 与 `last_chunk_*` 一致 → 200 幂等返回当前 `confirmed_offset`（**不重复写**）；不一致 → 409。更早的分片（非最后一片）直接返回当前进度，**不声称完成哈希比对**。
- `offset > confirmed_offset`：409 `offset_mismatch`（带当前 `confirmed_offset` 供客户端对齐）。

#### 3.2.2 串行 offset（首版裁定）

只接受 `offset == confirmed_offset` 的写入；客户端顺序上传、单并发。这样无需分片 bitmap，`confirmed_offset` 即唯一写入点。并行分片（bitmap）作为后续增强，不进首版。

#### 3.2.3 哈希策略（review 修订：浏览器增量哈希不可假设）

每片客户端上报本片 SHA-256，服务端写入前校验本片。整文件哈希裁定为：

- 浏览器原生 Web Crypto **不支持增量哈希**；不能隐含要求把 1GB 全读入内存。
- 若客户端能算（Web Worker + 增量哈希库，流式读 `file.stream()`），创建任务时上报可选 `sha256_expected`，commit 时与服务端复算值比对，**不一致即拒绝**。
- 若未上报，commit 时服务端对完整文件流式复算 SHA-256，结果作为权威 `sha256_actual` 入库。
- **不做"边收边滚动整文件哈希"**（无法跨重启/重试恢复）。

#### 3.2.4 reservation 续租与 TTL

任务 TTL 默认 24h（`UPLOAD_TASK_TTL`，env 可调）。**每次成功 PUT chunk 时刷新任务 `expires_at` 并对 reservation 续租**（把 `UPLOAD_RESERVATION_TTL_SECONDS` 的过期点同步后移），保证"任务活多久、预占保多久"，避免惰性回收后仍写入的超占。取消/expire/commit 时释放或转实占。

#### 3.2.5 commit 三段式（review 修订：不长持行锁）

commit **不在同一事务/行锁内**完成整文件哈希 + OpenSlide 验证 + 提升 + 入库（1GB 会变成长事务，cancel/expire 长期阻塞）。改为三段：

1. **短事务 A（受理）**：`active → committing`，写 `commit_token` + `commit_started_at`，续租 reservation，随即**释放行锁**。
2. **事务外**：流式复算整文件哈希、`_validate_slide_file`（OpenSlide 试开）、原子提升（`os.link`/`os.replace`）。这些只读/文件操作不持 DB 锁。
3. **短事务 B（收口）**：凭 `commit_token` 校验任务仍在 `committing` 且 token 匹配 → ownership 入库、reservation `consume` 转实占、`committing → committed`。

**崩溃恢复**：进程在事务外崩溃后，任务停在 `committing`。`committing` 超时（`commit_started_at + COMMIT_TIMEOUT`）后由惰性恢复收口：按"临时文件是否已提升为正式文件 + 哈希/校验是否可复核"决定继续完成或回滚为 `active`。cancel/expire 遇到 `committing` **返回 409**（不阻塞等待长事务）。

### 3.3 落盘规则

- 服务端按 `offset` 写入**同一个** `.uploading-<id>.part`（按 offset `pwrite`），避免 multipart 暂存 → **单次落盘**。
- 初始化即 `reserve_upload(declared_size)` + `check_disk_watermark`，防护前移到接收之前。

### 3.4 兼容与范围裁定

- 保留现有 `POST /api/upload` 给小文件与 **ZIP/MRXS**。
- **首版 V2 只支持单文件 WSI**；ZIP/MRXS 留旧接口（否则需另写解压、配额补占、多文件原子归属方案，超首版范围）。
- 阈值（如 128 MiB，`UPLOAD_CHUNK_THRESHOLD` env 可调）：小于阈值走旧单请求，大于等于自动切分片。前端按 `file.size` 决定。

### 3.5 UI

- 分三段展示：**正在传输（服务端确认字节为准）→ 服务端校验 → 入库完成**。
- 进度以 `GET /api/uploads/<id>` 的 `confirmed_offset` 为准，不是 XHR 本地发送进度。
- 多文件各自独立进度条（修复共用进度条问题）。
- 失败显示服务端错误码对应文案。

### 3.6 前端错误码文案

把已知错误码映射为可读信息，至少覆盖：`csrf_required`（请刷新后重试）、`upload_guard_unavailable`、`name_unavailable`、`offset_mismatch`、`hash_mismatch`、`413`（超限）、`507`（磁盘不足）、配额/限流类。不再统一显示"网络错误"。

## 4. 代理/运维加固（非本次失败主因，可并行做）

当前线上 nginx 已配 `client_max_body_size 10g`、`proxy_request_buffering off`、`proxy_read_timeout 3600s`、`proxy_send_timeout 300s`，与应用默认 10GiB 一致，1GB 未撞上限；且请求已穿 frp/nginx 到应用拿到 400，可排除代理拦截。

建议：

- 上传路径显式 `client_body_timeout 300–600s` 容忍短暂无流量。
- 分片 commit/校验接口设更宽响应超时。
- nginx 上传日志加 `request_length`、`request_time`、`upstream_response_time`，以区分客户端断线 / 隧道中断 / 应用拒绝。
- 管理员自用可增加可恢复的 `rsync/SFTP → 安全导入命令` 运维通道；同局域网优先走 LAN。
- SVS 通常已压缩，额外打 ZIP 节省有限，不把压缩当主要优化。

## 5. 实施顺序与工作量

| 阶段 | 内容 | 量级 | 依赖 |
|---|---|---|---|
| U1 | 前端补 CSRF 头 + 服务端 API header-only 契约 + 前后端双测试 + 错误码文案 | S（约 1 天） | 无，可立即上线 |
| U2 | Upload V2 后端：upload_tasks 表/状态机/跨 worker 锁 + reservation 续租 + 串行 offset + 每片哈希 + commit 复算 + 单次落盘 | M | U1 |
| U3 | Upload V2 前端：分片上传器、断点恢复、分段进度、多文件独立进度 | M | U2 |
| U4 | 代理/日志加固 + 管理员 SFTP/rsync 导入通道 | S | 无 |

**优先级结论**：先修 U1（CSRF）让上传立刻恢复可用；随后直接做 U2/U3 分片续传与单次落盘——**不值得继续围绕单个超长 multipart 请求堆超时参数**。

## 6. 测试计划

- U1 回归：无 token 原始 POST `/api/upload` → 400 `csrf_required`（pytest 裸 client，不包 CsrfClient）；**vitest 断言真实 `uploadFile()` 发送 `X-CSRF-Token`**（loadApp harness）。
- U2 单测：任务状态机转移与跨 worker 行锁；串行 offset（offset 不等 `confirmed_offset` → 409）；重复 PUT 幂等；每片哈希校验；commit 整文件复算 + 大小校验；初始化预占在 body 接收前生效；**reservation 续租**（PUT 推进 expires_at）；commit 与 cancel/expire 竞态；取消/过期清理临时文件并释放 reservation。
- 配额/水位/在途：沿用 `test_upload_guard.py` 口径，补"预占发生在 body 接收之前"的时序用例。
- 保留现有 88 个 AUTH_ENABLED=False 兼容测试全绿（`current_identity` 归一 owner 的不变量不受影响）。

## 7. 验收标准

- 1GB SVS 在 20 Mbps 弱网下可上传成功；中途杀掉浏览器/断网后重开能从 `confirmed_offset` 续传。
- 无 token 上传在 body 接收前即 400（`/api/*` 只认 header）；前端 `uploadFile` 真实携带 CSRF 头（vitest 验证）。
- 上传任务存续期间 reservation 不超占（续租生效）；commit 与取消/过期无竞态残留。
- 服务端只落盘一次（无系统临时文件 + 复制两段写）。
- 上传 UI 三段状态可见，多文件各自进度独立，失败显示明确错误码文案。
