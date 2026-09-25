# 文件管理升级：身份、存储与历史迁移的 Agent 执行计划

日期：2026-09-25。审查基线：PathTogether `a2b0ae3`，关联仓库 `../HistoPilot`。

**目标：每次新上传分配独立 slide_id，物理存储按 ID 隔离；用户文件名只用于展示和下载建议名。权限、分享、项目、标注、删除和配额绑定 ID。取消“同名文件即同一资产”的假设，移除为此产生的强制归属转移和冲突补救。**

本文是实施任务书，不代表已完成重构、数据迁移或上线。用户本轮授权的是 review 和编写任务书；后续实施 Agent 按用户的实施指令执行。COS capability 当前保持 off。本文不授权生产部署、生产数据搬迁或打开 capability。

范围覆盖旧文件管理系统整体收口，而非只改 COS。上线前必须执行配套的[历史数据迁移审计与切换手册](slide-storage-migration-audit-runbook-20260925.md)。最终正常服务的历史资产也使用 ID 独占目录；legacy 物理布局仅作为迁移期间的过渡，不作为永久第二套系统。旧 URL 可以经冻结别名继续访问原 ID，不需要为兼容旧链接保留旧磁盘布局。

## 1. 审查结论与范围

### 1.1 根因与裁决

问题不是 COS 签名或随机数不足，而是本地文件路径、数据库资产和权限共同使用了可重用的用户文件名。文件先落到可访问路径，归属后检查，失败时再删除或转移归属，因此不断产生跨用户访问、误删和配额遗漏。

采用以下明确裁决，实施中不得回到逐项打补丁：

1. **一次新的逻辑切片上传 = 一个新的 slide_id。** 同账号/不同账号上传同名、同内容文件都允许，默认是独立资产。幂等重试复用原任务及其 ID，不能重新分配；删除后重传也使用新 ID。
2. 复用 `slides.slide_id`，不要新增一套同义的 document_id/file_id。ID 必须随机、由服务端生成、数据库主键保证唯一；不要从原名、owner、内容哈希推导。现有 ID 不改号。
3. 文件原名不是唯一键，不参与路径、权限、幂等键、恢复匹配或删除定位。显示名可修改；修改显示名不移动文件、不变更授权。
4. 上传者在创建资产时确定；常规上传没有“接管已有资产”“强制改 owner”“同 SHA 认领已有文件”的分支。
5. **先建立不可见资产，后在一个数据库事务中完成发布和本地配额结算。** 文件存在不等于可读。所有读取入口共同检查发布状态。
6. 不实现同一 slide_id 下覆盖内容。更换内容就是新 slide_id，因此旧分享/标注/AI 上下文绝不自动继承。将来若做替换功能，另立合同。
7. 不扩大为存储平台重写：保留现有传输、格式解码、COS 协议与池账本，只收敛资产身份和本地发布边界。

### 1.2 当前代码事实与修改落点

路径均相对 PathTogether，`../HistoPilot` 明确指另一个仓库。函数名是定位锚点，执行时按当前 HEAD 核实，不依赖本文行号。

| 领域 | 已核实的现状 | 必须改变的职责 |
|---|---|---|
| 身份/元数据 | `migrations/0001_init.sql` 已有 `slides.slide_id`、`legacy_filename UNIQUE`、`display_name`、`alias`、`slide_assets` | 扩展已有身份，不另造实体；原名与存储分离 |
| 元数据入口 | `share_store_pg.py:set_slide_meta/get_slide_id/resolve_slide_ref` 按 legacy 名建行/取行；带 `sld_` 的字符串可直接返回 | 新资产显式建行，解析必须查存在性与状态，拒绝“前缀就是有效 ID” |
| 本地读取 | `app.py:_safe_name/_get_slide/_visible_slide_names/api_slides` 拼 `UPLOAD_DIR / name`、扫描目录 | 统一 ID resolver；列表改从已发布数据库资产查询 |
| V1 / ZIP | `app.py:api_upload` 及 `_extract_zip`、legacy commit/artifact 辅助函数，`upload_task_store.begin_legacy_commit` | 每个逻辑切片分配 ID，保留 ZIP/MRXS 安全解包和 manifest |
| V2 | `app.py:api_uploads_create/api_uploads_commit/_upload_v2_maintain`、`upload_task_store.py` | 创建时绑定 ID，恢复按任务身份，不再按 safe_name 查目标 |
| COS | `ingestion_store.py`、`cos_ingest_worker.py:process_validating/process_ready` | 复用同一发布接口；远端 key/version 与本地 slide_id 分别持久绑定 |
| 转换 | `conversion_store.py`、`conversion_worker.py:process_job` | source/canonical 定位改为 ID 下的 manifest；转换结果复用预分配 slide_id |
| 远程导入 | `scripts/baidu_import_worker.py` 及实际调用的导入服务，`baidu_import_items.slide_name` | 导入结果持久返回 slide_id，目标项目关联不再看文件名 |
| CLI 导入 | `scripts/import_slides.py:import_one` 从外部源提升文件并调用 set_slide_meta | 接入同一资产发布服务；不得与外部可写源共享 inode，保留明确 owner 参数 |
| 管理/维护 | `app.py` 管理库存、visibility 接口，以及 `plugins/pathtogether-admin` UI | 区分数据库资产列表与磁盘审计库存；操作传 ID，禁止库存扫描隐式认领；与已有插件改动协调 |
| 权限/分享 | `shares.slides` JSONB 名称数组、`grants` 领取关系、`slide_view_grants` 名称+部分 ID | 分享成员与显式授权统一绑定 ID，避免旧名重新指向新资产 |
| 项目/标注 | `project_slides.slide`、`rois.slide`、`comments.slide`、`change_log.slide` | 业务关联改用 ID；旧文本保留为兼容/历史快照 |
| AI/插件 | `run_grants.slide`、`ai_session_principals`、插件桥、`/internal/ai/*` | ID 贯穿授权、请求和恢复；不得把新文件名伪装成 slide_id |
| Demo/研究 | `demo_catalog` 已有 slide_id；研究/访问事件还需逐调用点核实 | 复用已存在 ID；删除/撤权/遥测要一致，不另建映射 |
| 前端 | `static/app.js` 大量 `state.slide.name`、`openSlide(name)`、项目选择和 localStorage | 所有操作键改 slide_id，名称仅显示；按新契约恢复任务 |
| HistoPilot | `src/platform/contract.ts` 已有 SlideIdRef，但 `legacyFilename()` 拒绝它；旧 Flask adapter 仍使用 filename；http-client.enc 可接受 ID 不等于后端已支持 | 补齐运行时 ID 通道与能力协商，不只修改 TS 类型 |

### 1.3 当前方案尚未解决的问题

基线 `a2b0ae3` 的 `force_slide_owner_follow_file` 不作为目标方案基础：

- 只删除 `slide_view_grants`，旧 shares/grants 仍可读新内容。
- stat 后路径被替换，转移 owner 会把替换者的内容交给错误用户。
- SELECT owner 无行锁，UPDATE 无 expected_owner 谓词，不是真正 CAS；conflict 返回前还有已执行的授权删除。
- 文件保留可读，但 fail_job 释放预约，未结算 used_bytes。

此前 87 项 COS 相关测试通过不覆盖这四个漏洞；复现已确认。执行 Agent 应把下面验收矩阵转成持久回归，不以旧通过数字作为重构完成标准。

## 2. 目标数据和存储合同

### 2.1 身份与名称

扩展 `slides`（迁移号在实施时选择下一个空闲号）：

| 字段 | 目标语义 |
|---|---|
| `slide_id` | 保留现有主键；新上传分配后永不复用 |
| `owner_user_id` | 服务端从身份确定；新资产必须有明确用户，包括有 UID 的平台 owner。无 UID 的本地模式先解析配置 owner，不能用空 owner 自动认领 |
| `original_filename` | 原始 basename 的展示快照；不接受目录语义，限制长度/控制字符，输出转义 |
| `display_name` | 复用现有字段，唯一可编辑展示名；初始为 original_filename |
| `legacy_filename` | 仅为历史入口保留的冻结别名；新上传设 NULL，不再写入原名，不允许旧别名重绑新 ID |
| `storage_layout` | `legacy` 或 `id_bundle`；只有 resolver 理解两种布局 |
| `storage_relpath` | 服务端生成的包入口相对路径；客户端不能设置，唯一且不可变 |
| `format_ext` | 实际逻辑格式，不能从展示名推导解码方式 |
| `asset_state` | `staging / ready / deleting / deleted / failed`，与传输任务状态分离 |
| `published_at/deleted_at` | 发布/删除审计时间 |
| `accounted_bytes` | 本地已计费物理字节的稳定值；与 ready 发布同事务设置，删除结算使用它 |

`alias` 不再与 display_name 并行维护两套可编辑名称。历史回填规则：非空 alias → display_name；否则非空现有 display_name；否则 legacy_filename。过渡 API 可输出旧 alias 字段，但只从 display_name 派生；旧 PATCH alias 映射到 display_name。旧列等兼容期结束再删。

保留 `slide_assets` 已有 revision/provenance 数据。首版一份新切片只有一份不可变发布内容，不引入第二套“当前代”指针系统。先查清 `content_sha256` 的现有语义；不要把“解码后的内容指纹”和“原文件 SHA-256”混写同一列，需要时新增明确命名的 file_sha256。新 ID 策略不能删除现有 revision 校验和 AI render fingerprint。

任务表 `upload_tasks/ingestion_jobs/conversion_jobs` 增加显式 slide_id；批量任务使用既有 artifacts/items manifest 表达 task→多 slide_id。不得因前端刷新或 worker 重领而重新分配 ID。为“同一任务、同一逻辑 item”建立库层唯一约束。

### 2.2 磁盘布局

```text
UPLOAD_DIR/
  .staging/<task_id>/<generation>/...      # 任务专属，不提供静态访问
  objects/<slide_id>/data.svs              # 单文件示例
  objects/<slide_id>/bundle/...           # 多文件格式，入口见 manifest
  objects/<slide_id>/manifest.json
  <历史文件及伴侣目录>                     # 仅迁移过渡，验收后按保留期退役
```

- 新物理目录由服务端 slide_id 派生，无用户可控片段；不同资产绝不共享可写目标。扩展名来自格式判定的白名单。
- MRXS、OME 多文件及转换 associated 文件不能逐个随机改名：只随机化外层资产目录，保留包内必要相对关系，manifest 指定唯一入口。完整包发布，不能入口先可读、伴侣文件后到。
- ZIP 是运输容器，不一定是一张切片：按既有识别逻辑拆成逻辑切片，每份独立 ID/目录。相互引用的文件必须归为同一包；无法确定归组就拒绝该 item，不跨资产目录互相引用。
- 保留解包路径穿越、绝对路径、符号链接、解压大小/数量限制。manifest 中的相对路径同样验证 containment，不从展示名拼路径。
- 不按 SHA 跨用户去重，不共享可写文件；同内容不代表同身份。
- CLI/外部目录导入必须复制到受管理暂存并验证；不能把仍能被外部写入的源文件硬链接为已发布资产。任务内部受控暂存的同卷提升另按发布合同处理。
- 不通过 Web 服务器直接暴露 objects 或 .staging，所有读必须经服务端授权。外部程序手工放文件不自动注册为可见切片。

### 2.3 统一接口（新增最小模块）

建议新增 `slide_store.py`（PG 资产状态）和 `slide_storage.py`（安全路径/包操作），必要时用一个薄 `slide_publish.py` 编排；不要让 worker import app，也不要复制 app 的发布实现。

| 接口 | 职责 |
|---|---|
| `allocate_slide(owner, original_filename, task_ref, format)` | 幂等创建 staging 资产；不查原名是否已存在 |
| `resolve_slide_id(slide_id)` | 校验 ID、查资产，返回受控 descriptor；缺失不凭前缀当成功 |
| `resolve_legacy_alias(alias)` | 仅查冻结历史映射，再进入同一个 resolver；无文件系统猜测 |
| `authorize_read(actor/capability, slide_id)` | 所有读取入口共用：状态 ready、身份权限、相应 token/撤销/范围检查 |
| `publish_slide(task_ref, generation, slide_id, manifest)` | 验证文件/任务/owner，执行第 3 节发布合同；不调用按名称隐式建行的 set_slide_meta |
| `request_delete(slide_id, actor)` | 原子改 deleting，立刻拒绝后续新读取，生成幂等清理工作 |
| `cleanup_slide(slide_id)` | 仅清理该 ID 的独占包；确认物理清理后结算，绝不以显示名扫描删除 |

descriptor 必须区分 ID、显示名、逻辑格式、存储路径、revision。路径仅内部使用，不返回给浏览器。

## 3. 发布、恢复、取消与配额

### 3.1 发布流程

1. 创建任务时在数据库绑定 slide_id 和 owner；文件写到任务/generation 专属 staging。
2. 完成大小、整文件哈希、格式试开、必要的代表性 tile readiness 校验。COS 分块可信核验与版本绑定继续保留。
3. 持久化 publish intent：task_ref、generation、slide_id、owner、manifest、哈希、实际结算字节。不能仅保存目标文件名。
4. 取得统一的每 slide_id 跨进程锁，重验任务 generation、资产 staging、取消状态和有效预约。所有 publish/delete/recovery 必须采用相同锁及锁顺序；进程内 mutex 不够。优先复用 PG advisory lock/行锁，先审计与现有 reservation→quota/pool 顺序是否有环，并写入模块注释。
5. 将完整包以 no-clobber 方式发布到唯一 ID 目录；禁止覆盖。文件操作必须有持久恢复记录，按耐久合同 fsync 文件及相关目录。正常同卷优先原子目录 rename；跨卷需要目标卷私有暂存、完整复制校验后再发布，不直接复制到可用目标。
6. 在短数据库事务里做 task/generation/state 的真实 CAS、验证既有 owner、consume reservation、设置 accounted_bytes 与 asset_state=ready、标记本地提交完成。任一步失败整体回滚。COS 远端删除不在这个事务内。
7. 只有第 6 步提交后，列表、Viewer、分享、下载、插件与 AI 才能读。所有兼容入口同样受此门禁。前端只在 ready/对应可查看任务态后打开 slide_id。

文件系统与 PG 不需要假装支持跨系统事务：**DB ready 是唯一可见性开关，唯一 ID 路径不复用，intent 保证崩溃恢复**。FS 已发布、DB 未提交的内容依然不可读；恢复按既有任务/ID 重试，不能按名称或 SHA 收养别人的资产。

### 3.2 并发与状态要求

- 发布与删除/取消按同一资产锁裁决。取消先赢则不能发布；提交已进入不可撤销段则返回稳定的 commit_in_progress，并继续完成/恢复。不能只在某个 API 路径加锁。
- 旧 generation 不能写新 generation staging、发布、撤销文件或结算。长操作有 lease 续租；失租要停后续副作用，不仅拒绝最后一条 SQL。
- 不做“stat 相同就 unlink/转 owner”。本任务只能操作自己独占的 ID 包，元数据 owner 不匹配说明不变量被破坏，应隔离并告警，不能自动修正 owner。
- 非 ready 内容始终不可读；数据库异常按拒绝读取处理，不回退扫描目录。
- 正在进行的读取可以完成已取得的受控文件句柄；删除后所有新授权请求拒绝。不得在同一 ID 路径放入新内容来规避该规则。

### 3.3 配额与回收

- ready 发布与本地 used_bytes 增加必须同事务、只发生一次；retained 文件不能随任务 failed 就变成 used=reserved=0。
- staging/待恢复/待清理文件持续持有有效容量责任。任务失败与预约释放解耦：清理确认后释放，或者在确定保留为已发布资产时正式结算；本方案不把失败残留自动发布。
- 删除先置 deleting 使读取失效，再物理清理；清理成功后按 accounted_bytes 幂等减少实占，置 deleted。重复 DELETE/worker 重试不得重复减账；删除失败继续占账并重试。
- 多文件包按实际保留文件之和结算；hardlink 的临时别名不重复收取。转换源与产物同时保留时都计本地容量，删除源后按明确规则释放其责任。
- 大小变化需要预约 top-up，不能发布后才发现超额。预约过期采用现有 fail-closed/重新准入合同，不复活过期预约。
- COS 池预约与本地配额继续分开：远端 Abort/版本清理确认后才释放池，不能用本地 ready/failed 代替远端清理证明。

## 4. 权限和引用迁移

### 4.1 对外 API 与前端

- 新接口显式采用 `/api/slides/<slide_id>/...` 及 `slide_id` 请求字段；实现前检查已有 Flask 路由是否冲突。集合 `/api/slides` 返回独立字段 `slide_id/original_filename/display_name/format_ext`。
- 新前端使用 slide_id 作为列表 key、DOM dataset、已选切片、项目成员、URL、删除与元数据 PATCH、标注/裁剪/AI 上下文的操作值；显示 display_name，辅助显示 original_filename。相同显示名可用日期等辅助信息区分，不修改 ID。
- 上传完成响应必须返回 slide_id；前端不得再用 `file.name`、`canonical_name` 或 `body.slide || file.name` 猜打开目标。
- localStorage 续传绑定账户域、task_id、slide_id；文件名/大小仅帮助用户挑选，不能证明恢复的是同一文件。保留现有哈希/分片一致性机制；换账户不得复用会话。
- 旧 URL/请求的 name 参数只作为显式 legacy alias 解析，并进入 ID 权限层。不能在同一个不带类型的字段上依次尝试 ID/路径/展示名。
- 新客户端与后端通过明确 capability/契约版本协商 ID 模式。不要悄悄改变旧 `name` 字段的含义。旧客户端访问历史资产仍兼容；新资产只能由支持 ID 的客户端操作，不通过伪装成旧文件名来兼容。
- 下载 Content-Disposition 使用经安全编码的 original_filename/display_name；不得泄露内部路径，也不得允许 CRLF 注入。

### 4.2 逐类引用

| 引用 | 执行要求 |
|---|---|
| 显式 view 授权 | `slide_view_grants` 主关联改 slide_id+user_id；取消 NULL-ID 退回名称匹配；未知旧记录隔离 |
| 分享与领取 | 新建 `share_slides(token, slide_id)` 或等价有约束关系，迁移 shares.slides 历史成员；grants 仍按 token 领取，读取成员从 ID 关系取得。旧 JSON 仅兼容输出/快照，不能继续参与授权 |
| 项目 | project_slides 增加/切换 slide_id，保留顺序，唯一键按项目+ID；同名不同 ID 可同时在项目中 |
| 标注/评论/变更流 | rois/comments/change_log 的活动查询按 slide_id；annotation_id 不变。历史文本不重写成猜测 ID，无法映射记录标为 unresolved，不展示在新资产下 |
| run/session 授权 | run_grants、AI session principal、插件 JWT 范围、后台执行请求显式绑定 slide_id。内容 revision/render token 的约束继续生效 |
| Demo | 保留 demo_catalog.slide_id，检查所有 alias→file 绕行；删除后 capability 不能读。既有运行撤销和预算释放不能遗漏 |
| 研究/审计 | 新记录同时记 ID 和名称快照；历史不可变审计保留原样，可另加解析结果，不重写历史身份。研究删除按真实 ID 关联验证 |
| cache/sidecar | slide_cache、tile cache、缩略图、manifest、associated、AI replay 与本地索引按 ID+已有 revision/render key；显示名修改不触发内容变化，删除准确失效该 ID |

旧分享 token 可以保留，但成员只能映射到迁移时已确认的旧 slide_id。原文件被删除后，旧 token 不能因新上传恰好同名而恢复访问。任何旧授权回填都不以“原名相同”作为新内容归属证明。

### 4.3 HistoPilot 必须同步适配

核查并修改：`src/platform/contract.ts`、`legacy-flask-adapter.ts`、`http-client.ts`、`src/flask-client.ts`、`agent-runner.ts`、`tools.ts`、`path-replay.ts`、`transform-context.ts`，以及 `integrations/pathtogether` 实际插件桥。

- 已有 SlideRef 联合类型保留，新的运行上下文使用 `{kind: 'slide-id', slideId}`。
- `legacyFilename()` 只能留在显式旧分支；ID 不通过它，也不变成“随机文件名”去走旧端点。
- 当前 HTTP 客户端能 encode ID 只说明路径序列化能力；必须做跨仓真实请求契约测试，证明后台路由/授权/返回身份一致。
- 旧会话及回放只能解析已冻结 alias；映射不存在/资产删除返回明确失效，不绑定新同名内容。
- 文本中给用户显示名称，工具参数和写入目标用 ID；同时检查批注写回、快照、区域裁剪、插件运行取消和共享会话。

## 5. 保留、拆除与收敛清单

| 处理 | 内容 | 条件/说明 |
|---|---|---|
| 删除 | `force_slide_owner_follow_file` 及 share_store 导出、所有调用 | 新发布管线接入时删除；绝不推广到 V1/V2 |
| 删除 | COS `adopted_existing/still_ours/promoted_ident` 驱动的跨 owner 认领、修复 owner、留在他人路径等逻辑 | 不保留双写补丁。损坏身份只隔离，不猜归属 |
| 删除/限制 | 用户原名层 `_upload_name_conflict` 和转换 canonical 名唯一锁 | 新上传不按原名冲突；内部 ID/path 冲突检测与 no-clobber 继续保留 |
| 收敛 | V1/V2/COS/conversion 中复制的本地 promote、commit intent、归属登记、结算、恢复 | 共用本地发布服务；传输阶段仍各自保留，不能用一个巨大通用上传框架替代 |
| 限制 | `set_slide_meta(name)` 自动创建身份、`resolve_slide_ref` 前缀直返 | 仅迁移兼容层使用明确旧合同；正常新读写改 ID |
| 删除 | 目录扫描自动视为切片目录、旧名自动接管缺文件元数据、UI 用文件名定位 | 管理库存盘工具仍可扫描，但只能报差异/隔离 |
| 保留 | 官方 COS SDK 薄适配、绑定长度/partNumber/uploadId 预签名、远端版本钉源、Complete 结果恢复 | 这些是独立正确性要求，改 ID 不能删 |
| 保留 | COS 容量池、FIFO、远端全版本/碎片对账、清理释放、签名限额 | 从原名中解耦，行为仍按原合同 |
| 保留 | V2 offset/分片哈希/幂等重放；格式校验；磁盘水位；CSRF/身份隔离 | ID 不替代权限、数据完整性与容量防护 |
| 保留 | no-clobber、真实 SQL CAS、lease fencing、readiness、一次结算、崩溃恢复 | 简化的是同名补救，不是这些安全边界 |
| 保留后退役 | legacy alias resolver、旧接口/DTO、旧 display alias 字段 | 有明确调用量/数据审计证据后再移除，不无限双轨 |

测试也需要简化：以目标不变量替换依赖 `_stat_ident` 调用次数、force-owner 内部路径的用例。不要“断言原样通过”绑架设计。保留场景意义（别人的内容/权限不被触碰），删除已废弃内部 API 的断言；原 COS 网络/预算/恢复用例继续保留。

## 6. 分阶段执行与完成标准

每阶段独立可审查提交，按依赖顺序执行。中间阶段不得开启新 writer 到生产；旧 writer 与 ID writer 不能同时对同一批资产无协议写入。

### P0：冻结合同、完整依赖清单与迁移盘点工具

- 记录两仓 HEAD、工作区他人改动；不带入 admin/注册邮件等既有未提交修改。
- 用 `rg` 补全第 1 节入口：所有 `UPLOAD_DIR/name`、legacy_filename、`.slide`/`.slides` 数据引用、`state.slide.name`、对外接口、导出/批处理与管理插件。
- 新增只读 dry-run 盘点工具（建议 `scripts/audit_slide_identity.py`）：输出 ID↔旧名↔owner↔文件存在性、授权/标注引用、活跃任务、容量账本差异及汇总；机器可读 JSON 报告只落受限目录，公开 evidence 只留脱敏计数。
- 把 legacy resolver、ID DTO、asset_state、发布/删除状态图、锁顺序写成代码旁合同；核对历史 ID 生成器与 HistoPilot 注释的 uuidv7 不一致，不为统一格式重置旧 ID。
- **完成标准**：每条入口都有迁移责任；每类历史引用有“映射/隔离/仅保留审计”裁决；没有待实施时猜测的原名归属规则。

### P1：新增 schema 与统一 resolver（旧读仍兼容）

- 增量迁移 slides、任务 ID、关系表与索引；不在 schema migration 中搬动文件或调用云服务。
- 实现 slide_store/slide_storage 与状态门禁，新增 ID API；旧端点先解析固定 alias 再进同一门禁。
- legacy 资产只在盘点/验证后标 ready；新增列默认不能让未知资产自动公开。旧数据回填分批、可重跑，保留 checkpoint。
- **完成标准**：随机 ID 不可越权；staging/failed/deleting 无任何可读通道；旧名映射不重绑；旧读取行为在可信迁移夹具上保持。

### P2：权限关系、前端与 HistoPilot 读通道

- 执行第 4 节全部 active 关系的 ID 适配，保留旧记录的冻结映射；未知来源不扩大权限。
- 前端/插件/sidecar 通过能力协商使用 ID；所有上传完成回调、项目选择、标注和 AI 参数使用 ID。
- UI 重命名只 PATCH display_name，不改 legacy alias/存储 key。下载原名保持可理解。
- **完成标准**：两张同显示名切片能分别打开/标注/分享/关联项目；改名不串片；两仓合同测试及真实浏览器验证通过。此时只用隔离测试数据，尚未放行生产新上传。

### P3：统一本地发布，先接 V2 和原生单文件 V1

- 新任务预分配 ID；落实第 3 节的 intent、状态门禁、锁、配额与恢复。
- 复用传输接收及哈希校验；不再按原名查冲突。兼容中的旧在途任务不能被悄悄改目标，见 P6。
- **完成标准**：V1/V2 同名并发、取消/删除/崩溃、满配额、跨账号隔离均通过；未提交资产即使物理文件已在 objects 也不可读。

### P4：接入 ZIP/MRXS、转换、远程导入和 COS

- ZIP 按逻辑包分配 ID，转换与导入任务保存同一 ID 链路，原名仅元数据。
- COS staging key/upload_id/version 保留原合同，下载和发布改为 ID 独占目录；移除第五节强制转移及同名认领逻辑。
- local ready/Viewer readiness/remote cleanup 前端文案仍区分；本地服务不把“上传 100%”当发布完成。
- **完成标准**：每个正式支持的上传/导入入口经过统一发布服务，不存在旧旁路。转换 failure/重试不重复 ID、项目关联或配额；COS 完成与远端清理的已有回归仍通过。

### P5：统一删除、回收和资源账本

- 删除/取消/reaper 全部按 ID 操作；删除失效覆盖分享、显式授权、Demo、插件、AI 和缓存入口。
- 删除任务持久化并可重试，物理清理成功后一次性减账；孤儿扫描只隔离报告，不通过文件名猜 owner。
- legacy 文件的删除仍需 legacy 布局安全实现，但新 writer 永不重用 legacy 名路径；其授权映射保留 tombstone。
- **完成标准**：同名重传新 ID 与旧删除互不影响；保留文件始终有账本责任；重复调用/worker 重启不重复释放。

### P6：完整历史迁移演练、兼容收口、删除旧代码

- 在脱敏副本或合成数据做 dry-run→回填→重跑→中断恢复；不操作当前生产。
- 实现配套手册规定的只读审计、逐资产计划、物理包迁移和独立验证工具。先逻辑映射，再迁移历史包，保留原 slide_id；不能仅完成新上传就宣称清完技术债。
- 在副本完成全部受支持格式、历史关系及中断恢复演练。最终正常服务的资产全部为 id_bundle；问题资产保留证据、隔离且不可读。legacy 物理读取实现只能留在受限迁移工具/过渡版本，最终运行时无此旁路。
- 迁移时暂停所有上传/转换/导入/清理 writer；已有 active 任务选择“排空旧管线”或“显式取消后按合同清理”。首选排空；不能让旧 worker 对新 schema/布局猜测续跑。
- 核对并移除新管线中全部原名定位/强制转移路径，保留有边界的兼容适配器；发布前记录旧接口调用者名单。
- **完成标准**：演练中的可服务历史资产全部完成 ID 包迁移，字节/哈希/授权/配额对账通过；损坏项隔离、旧分享不接新同名内容；active 任务为零或全部有明确处置记录；代码搜索无未解释的读写旁路。生产执行留给 P7 的具体切换方案。

### P7：交付验收与部署包

- 完成下节测试矩阵，记录实际执行命令、通过/失败/跳过数量及原因，不沿用“全量通过但固定排除”的模糊口径。
- 提供两仓提交对应、镜像/迁移顺序、只读检查、备份及恢复演练结果；新 ID 读端先就绪，最后才允许新 writer。
- 生产旧数据审计必须发生在生产修改之前；停写后再冻结最终清单，执行迁移并独立复核，全部硬门禁通过才重新开放服务。工具完成、演练通过、生产迁移完成是三个不同状态，不得混报。
- COS capability 仍 off；生产 CSP 三 origin、控制台生命周期/费用核对、Phase 5 池容量演练继续作为独立门禁，不因本重构测试通过而自动豁免。
- **完成标准**：可供用户批准的具体部署与验证方案。实施 Agent 不凭本文自行在生产创建账户、发邮件、改桶或开启 capability。

## 7. 历史数据与回滚边界

### 7.1 迁移分类

| 历史情况 | 处理 |
|---|---|
| metadata 与文件一致、owner 明确 | 保留 slide_id，迁入 ID 包；回填 display/original/state，冻结 legacy alias；绑定已有关系 |
| metadata 在、文件不在 | 标为 missing/不可发布的迁移问题项（可用 failed+明确 reason），保留原 ID/旧 alias，不允许新上传接管 |
| 文件在、metadata 不在 | 隔离报告，不猜 owner，不自动进入用户列表 |
| owner 空/多个引用相互矛盾/已知同名替换历史 | 要求人工确认；确认前不迁移其授权到新内容。管理员可见库存不等于普通用户可读 |
| 授权/分享引用无法唯一对应可信历史资产 | unresolved 并拒绝访问，不根据当前同名文件自动补绑 |
| 旧被删资产又同名上传，库中 ID 曾被复用 | 不能声称旧 ID 本来就安全；查历史证据，无法证明的旧分享/标注禁止继承，出明确迁移报告 |

### 7.2 可回滚性

- schema 先增不删，旧列在兼容期保留。新 writer 未开启前可以退回兼容版本，但要核对状态门禁不会被旧应用绕过。
- **一旦产生新 ID 资产，不允许直接回滚到只认文件名的老镜像。** 回滚目标必须仍支持新 schema/ID 读取；否则先暂停写入并前滚修复。不能把随机存储名批量改回原名来“兼容”。
- 不为回滚恢复旧授权到新资产；DB/文件备份恢复必须成对对应，不能单独恢复 owner 表或 objects 目录。
- 迁移阶段保留原始数据与映射清单；验证完成及保留期届满后，通过独立受控清理任务退役旧副本。禁止迁移脚本顺手删源。不修改邮箱注册配置、不重跑生产 dogfood；先在隔离环境使用合成 TIFF/MRXS 包和测试身份。

## 8. 必须执行的验收矩阵

| 场景 | 必须观察到的结果 |
|---|---|
| 甲乙均上传 a.svs；同账号再传同名同字节 | 不同 slide_id/物理目录，各自正确归属，显示名相同；原名重复不返回名称占用 |
| 同一任务重复创建/commit/恢复，响应丢失后重试 | 同一 slide_id、同一次配额结算，不生成重复切片 |
| 删除 A 后上传同名 B | B 新 ID，旧分享/显式授权/标注/AI session/Demo token 不指向 B |
| 显示名修改、Unicode 原名、包含目录/控制字符输入 | 修改不移动文件；显示/下载安全，绝不影响 ID 或路径 |
| 旧 metadata 属于甲、文件缺失，乙新上传同名 | 乙新 ID；甲旧记录不可读，绝不转移甲 owner/授权/别名/note |
| 同 SHA 的他人文件、平台 owner 文件 | 不认领、不删除、不重新归属；哈希只证明内容完整性 |
| stat 后替换/旧 worker 复活/同任务两 worker | 没有按原名删除/转 owner；旧 generation 不写新包、不发布、不结算 |
| intent 前后、包发布前后、DB commit 前后崩溃 | 未 ready 始终不可访问；恢复收口一次；存活字节有预约或实占 |
| 数据库结算失败、预约过期、磁盘满、清理失败 | 不可读、不漏账、不重复收费；故障解除后可恢复或安全清理 |
| 取消和发布、删除和发布、删除和新上传并发 | 有明确胜者；新 ID 资产不受旧任务清理影响 |
| 所有读取通道 | 列表/info/DZI/tile/thumbnail/crop/download/share/claimed grant/view grant/Demo/plugin/internal AI 均拒绝未发布/删除资产 |
| 旧分享保留 token | 仅访问已确认旧 ID，旧名 missing 时拒绝；新同名无继承 |
| ZIP/MRXS/转换伴侣目录、包内引用 | 入口与资源完整，不能跨 ID 包读取；真实解码与 tile 正常 |
| 转换/远程导入自动关联目标项目 | 关联正确 ID，不因同名重复关联其他资产 |
| 旧前端/新前端、旧 sidecar 会话/新 ID 会话 | 能力协商明确；旧映射只读到旧资产，不做猜测 fallback |
| 删除重放/回收重试/服务器重启 | DB deleting 门禁持续生效，物理清理与配额释放各一次 |
| 迁移重跑、缺文件、无元数据、权限歧义 | 无重绑、无自动认领、有隔离报告；回滚边界得到演练 |

测试实施要求：

- 用真实临时 PG 和真实目录验证 SQL 并发、发布/删除/配额；不要只用 Fake 返回值证明 CAS。故障注入采用明确的阶段屏障，不依赖 sleep 碰运气。
- 保留 COS Fake 协议测试，同时用 SDK 适配契约测试约束 Fake 与真实接口（含 HEAD latest/分页语义），不要为了测试桩的缺项改产品协议。
- 真实文件测试至少包含可解码合成 TIFF、平台已支持的多文件包、转换产物；浏览器验证列表同名并存、上传后打开正确 ID、分享/重命名/删除。
- 重点扩展：`test_slide_identity_pg.py`、`test_upload_v2.py`、`test_upload_accounting_recovery.py`、`test_upload_guard.py`、`test_import_slides.py`、conversion/baidu/COS API+worker 测试、权限/分享/插件/Demo/研究删除相关套件及 `tests/js`。
- 新增目标不变量用例组，替换旧 review 中对具体补救函数调用次数的断言；说明每条历史风险由哪个新测试覆盖。不能仅保留十几条旧复现后宣称完整迁移。

基础回归命令（实施 Agent 先检查两仓 scripts/环境，按改动范围补充实际 e2e 命令）：

```bash
# PathTogether
.venv/bin/python -m pytest tests/test_slide_identity_pg.py tests/test_upload_v2.py tests/test_upload_accounting_recovery.py tests/test_upload_guard.py tests/test_cos_ingest_worker.py tests/test_ingestion_store_phase1.py tests/test_ingestion_api.py -q
npm run test:js
# 最终门禁：完整 Python suite 与实际适用 Playwright 场景；逐项报告排除原因
.venv/bin/python -m pytest -q

# HistoPilot（在该仓目录运行）
npm run build
npm run test:unit
npm run test:integration
npm run test:contract
```

本文本轮为静态依赖审查和方案设计，没有为文档重跑全量测试。此前 review 的测试结果只是基线证据，不是本方案的验收结果。

## 9. Agent 最终交付格式

交付需包含：

1. 两仓变更清单及提交对应、实现后的 schema/API/锁顺序、兼容契约；每阶段实际完成标准。
2. 已拆除的函数/分支清单，保留兼容层清单及退出条件；新写入/读取旁路搜索结果。
3. 配套手册要求的审计报告、逐资产迁移计划、恢复日志、独立验证报告、隔离项处置、旧 token/旧会话兼容验证；明确哪些仅在副本执行。
4. 测试与浏览器验收结果、故障注入证据、配额/清理收口证明。
5. 生产部署/回滚执行清单，明确新 writer 开启后的最低可回滚版本；COS capability 门禁仍单独列出。

建议首个实施动作：完成 P0 清单和 P1 最小 resolver/状态门禁，随后用两份同名合成切片验证 ID→权限→真实文件读取，再迁移所有 writer。不要从继续扩展 force-owner 或一次性重命名生产目录开始。
