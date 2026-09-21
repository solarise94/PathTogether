# Agent 执行方案：自由注册、双协议与自愿研究数据共享

版本：2026-09-21-v3（按用户决定：协议文稿定位为非正式数据使用规则说明，不再以真实主体、联系方式为发布前置）。配套：[协议文稿](user-agreements-20260921-draft.md)、[统计与余额修复方案](agent-plan-20260921-traffic-balance-fixes.md)。

本文是后续开发规范，当前只写文档，不执行数据库迁移、不切换注册模式、不发送通知邮件、不采集研究数据。后续收到实施指令后，按阶段完成实现和验证，不把旧勾选或运营权限当作新研究授权。

## 1. 产品决定与默认值

| 项目 | 本版决定 |
| --- | --- |
| 注册 | 开放自助注册，邮箱验证成功后直接获得可用账号，无需管理员审批 |
| 每日名额 | 每个 Asia/Shanghai 自然日最多 5 个新自助账号；全站、所有入口、所有实例共用 |
| 计数时点 | 邮箱验证完成、账号成功激活的同一事务，不在打开页面或发送验证邮件时计数 |
| 通知 | 成功注册时同事务创建管理员邮件任务；实际邮件发送异步、有状态、可重试 |
| 注册复选框 | 恰好两个：必选《用户协议与数据处理说明》；自愿《数据共享与软件改进协议》 |
| 默认选择 | 首次展示两项均不预勾选；第二项不选也能注册、登录、读片和使用相同 AI 额度策略 |
| 第一项范围 | 账号及服务必要处理、AI 功能所需数据传输、必要运维与安全记录；不授权研究分析 |
| 第二项范围 | 指定研究者分析授权后产生的 agent 可见运行轨迹、对话、人工读片操作习惯，用于本软件改进 |
| 原始切片 | 不因研究选项自动复制完整原始 WSI、原图文件或附件；研究操作轨迹不含图像像素 |
| 等待 | 已由用户确认采用“停留观察动作”：只记录一次动作，不记录单次等待时长，不把加载卡顿算入 |
| 历史用户 | 新研究授权默认未同意，旧 true 仅保留历史证明，不能自动升级授权范围 |
| 实施开关 | 协议、自助注册、研究采集分阶段发布；研究采集独立开关默认关闭 |

“5 人”在本方案定义为 **5 个成功新建的自助账号**，不是能验证的独立自然人数。单人多个邮箱仍需现有反滥用机制，不能宣传已保证每天 5 个独立真人。

本版自助新建路径（包括保留的邀请码自助新建路径）统一占用名额，避免换入口/接口绕过。管理员明确手工建号、审批已有 pending 账号与该新建额度分开审计，不作为 public 注册的隐藏回退。删除或禁用已注册账号不退还当天名额。

## 2. 已核实的代码基础

| 文件/模块 | 当前状态 | 改造要点 |
| --- | --- | --- |
| `app.py::register` | public 是已有枚举，但返回 public_registration_not_supported；注册为介绍页弹窗 | 实现 public 分支；不是只改一个配置值 |
| `registration_store.py` | 邮件 token 同事务消费后建 pending_activation；邮箱/login_id 冲突有隔离逻辑 | 增加 public 原子建 active 用户分支，保留原激活流程 |
| `settings_store.py` | closed/invite_only/email_verify_invite_activation/public | public 成为可用模式，加入 HTTPS、Secure Cookie、邮件及协议配置前置 |
| `registration_mail_worker.py` | 加密 `registration_mail_jobs`，已支持确定失败重试与 uncertain 状态；仅在旧邮箱注册模式排 email_verify | 开放 public 验证邮件排水；增加成功注册通知 purpose，注册暂停后仍发送已经成功注册的通知 |
| `test_application_store.py` | 旧 consent_version=research-data-20260916-v1，仅存分享意愿；明确未开启研究采集 | 旧表不能作为新采集权威；兼容入口必须接入新 consent 服务 |
| `templates/entry.html`、`templates/_login_dialog.html`、`static/entry-auth.js` | 注册/登录弹窗；不存在独立 register.html | 修改实际弹窗表单；保留深链接、回退、移动端、密码管理器 |
| `templates/verify_email.html` | 验证 token 后设置密码，并可能提交测试申请 | public 模式改为最终确认注册，不强制研究方向/申请表/邀请码 |
| `static/app.js`、`static/viewer-core.js` | OpenSeadragon zoom、animation-finish 完成回调、工具栏和标注入口目前在 app.js；viewer-core.js 提供共享 OSD 创建与缩放绑定，无 animation-finish 或标注代码 | 人工手势归并依托 app.js 现有事件，采集只进新增独立模块，viewer-core.js 保持不内置采集；不把所有 OSD 事件直接作为人类行为 |
| HistoPilot `src/agent-runner.ts`、`session-store.ts`、`events.ts`、`server.ts` | 业务会话、SSE 与恢复需要运行记录 | 业务存储与研究读取/复制分开；不得扫描整个 sessions 目录作为研究入口 |
| migrations | 本轮看到最新 0059 | 实施时重新确认序号，使用新的追加迁移；不修改已发布迁移 |

开始时分别检查两个仓库工作区和适用 AGENTS.md；保留上轮统计/余额实现，不混淆“已有代码”和本文“待实现”。

## 3. 协议展示和授权记录

### 3.1 注册页只有两个复选框

精确标签见协议文稿。两个独立 checkbox、两个独立链接；第一项标“必选”，第二项标“自愿，可随时撤回”。点击链接不会改变选中状态。键盘/读屏/窄屏均可独立操作，不能合并为一个“全同意”，不得以浅色小字掩盖可选性。

没有接受第一项时客户端提示并且服务端拒绝；第二项 false 或未提供按 false 处理，不能拒绝注册。true 必须是明确布尔值及有效协议版本，不能用 Python truthiness 将字符串 `"false"` 当成同意。

对所有新建自助路径、无 JS 表单和直接 API 请求做同样检查，不只拦截前端按钮。

### 3.2 协议阅读入口

固定新增公开只读页面 `/legal/user-agreement`、`/legal/research-sharing`、`/legal/model-providers`，并提供版本化不可变链接。将它们加入实际认证守卫的公共页面白名单；否则新注册用户会被重定向登录而无法读协议。GET 不创建用户、不记录同意，不扩大研究采集范围。

文稿允许独立阅读/保存；验证邮件页面打开协议时不将验证 token 拼入链接，使用不泄露来源的链接/Referrer-Policy。缺当前发布文稿时不能把 public 宣称为可注册；文稿按数据使用规则说明发布，不要求真实法律主体信息；草稿查看与正式 published 状态分开。

首版以审核过的中文版本为准；如提供英文，独立登记 locale 和内容 hash，不把浏览器自动翻译当另一份已确认协议。

### 3.3 邮件跨设备流程

1. `/register` 收邮箱、两个选择、服务端发布的协议版本标识；不提前收密码，不提前建可用账号，不占名额。
2. 在验证任务绑定的 pending registration intent 中保存协议 hash/version、必选接受动作时间、可选选择和表单语言；不可依赖浏览器 cookie 还原选项。验证 token 仍只保存哈希，邮件正文仍加密。
3. 验证页列出此前主动做出的选择，允许在最终提交前修改可选项。展示已做出的选择不是替未操作用户预勾选。
4. 发布内容发生实质变化或 intent 缺有效证明时，最终页重新提供两个 checkbox，不使用“继续访问视为同意”。点击邮件链接的 GET 不消费 token、不创建账号、不确认协议。
5. 最终 POST 在下节原子事务中落正式协议凭据；可选研究授权只从账号成功注册时生效，不能覆盖发送邮件前的行为。

协议文档发布记录不可变：`document_type`、`version`、`content_sha256`、`locale`、`published_at`、`effective_at`、`requires_reconsent`、不可变文稿定位。禁止内容修改后沿用相同 version/hash。

### 3.4 建议新表（名称可与仓库规范统一，语义不可省）

- `user_agreement_acceptances`：id、user_id、document_type、version、content_sha256、accepted_at、source（register/account_reaccept）、locale。追加记录，不覆盖旧版本。无需完整 IP、UA。
- `user_research_consents`：user_id 主键；state=declined/granted/withdrawn/reconsent_required；scope_version；document_version/hash；epoch 正整数；granted_at、withdrawn_at、updated_at。缺行/异常=未授权。
- `user_research_consent_history`：每次 grant/withdraw/reconsent 的不可变记录，含 user_id、前后 state、epoch、操作者、文档 hash、时间、请求幂等键。管理者不能代用户 grant。
- `registration_intents` 或对现有验证作业的受控扩展：flow_mode、terms/research 选择、版本与 hash、registration_request_id、完成关联 user_id；与 email_verify job 一对一。不要把协议 proof 塞入自由文本 JSON 后完全不校验。

版本/epoch、用户身份、接受时间由服务端产生或验证。客户端提交的 user_id、角色、接受时间不能成为权威。

### 3.5 账户设置

提供“数据共享”设置页：当前选择、适用版本、生效时间、共享范围、撤回、申请删除研究副本。撤回无需管理员审批、无需解释原因、不得退出账号或降低原有额度。

建议接口：

- `GET /api/account/agreements`：当前发布文档、本人接受情况、本人研究状态；no-store。
- `POST /api/account/agreements/accept`：接受当前必选文档；CSRF、本人身份、版本校验。
- `PUT /api/account/research-consent`：`{enabled:boolean, document_version, expected_epoch}`；CAS 处理多页冲突，grant 时版本必填，withdraw 即使旧协议已下架也必须可用。
- `POST /api/account/research-data/deletion`：幂等创建本人研究副本删除任务，返回可查询状态；该操作不删除业务切片或临床/科研工作记录。

现存测试申请的 consent 修改路径全部接到此服务或显式停用旧写入；不能存在两份互不一致的“当前同意”。后台旧申请字段只读标记“历史版本”，不可用于开放新数据集。

## 4. 每日 5 个自由注册与管理员邮件

### 4.1 公共注册前置

public 必须满足现有安全前置和额外配置：TLS、Secure Cookie、PG、有效验证邮件发送器、加密载荷 key、非默认 token 盐、已发布的双协议文稿（规则说明性质，版本/hash 受控）、配置明确的管理员接收邮箱。

新增配置 `REGISTRATION_ADMIN_EMAIL`；可显式兼容部署已配置的 `TEST_APPLICATION_ADMIN_EMAIL`，但不以源码硬编码的个人邮箱静默兜底。配置检查只输出缺项，不输出凭据。缺前置时 public 不开启；临时邮件发送失败不撤销已成功账号。

前台文案与 [协议文稿](user-agreements-20260921-draft.md) 的注册提示保持一致：“当前开放邮箱验证注册，每日最多 5 个新自助账号，名额于北京时间每日 00:00 更新，以完成注册时的剩余名额为准。”不承诺发送邮件即已预留名额；具体展示措辞以协议文稿为准，后续修改需两处同步。

### 4.2 数据模型和并发

- `public_registration_days(day DATE PRIMARY KEY, successful_count INT CHECK 0..5)`。
- `public_registration_completions(user_id UNIQUE, registration_request_id UNIQUE, day, channel, created_at)`：只增、幂等、支持核对计数；不因用户注销级联删除计数凭据，可将 user_id 去标识化并保留不可重复的 completion_id。
- 用户创建、激活来源、额度创建、协议凭据、completion、day 计数、管理员通知 job 必须同一数据库事务；不在该事务内访问 SMTP/CLI/外部服务。
- 日期由服务端数据库 `clock_timestamp()` 转 Asia/Shanghai 获取，在锁内选定日桶；跨零点一致性定义为**成功占用名额时的北京时间日期**，不是客户端日期。事务在占位后跨零点提交仍归该桶，不需午夜定时重置任务。

统一锁序：registration intent/token → 相应日桶 → 新账号/同邮箱唯一性检查与插入。实现时审查旧邀请码路径使用的锁，统一锁序或使用可证明无环的独立顺序；并发用唯一约束兜底。

事务伪流程：

```text
BEGIN
  validate current mode / active document versions / explicit terms acceptance
  SELECT intent + verification job FOR UPDATE
  verify token HMAC matches this intent and its server-issued registration_request_id
  if that intent already completed:
      return only success + login URL; no identity fields, new session or repeated side effects
  validate expiry, allowed mail state, intent mode and password
  resolve existing email/login identity conflicts; never merge identities automatically
  compute quota_day in Asia/Shanghai
  INSERT day bucket ON CONFLICT DO NOTHING
  UPDATE day bucket SET successful_count = successful_count + 1
    WHERE day = quota_day AND successful_count < 5 RETURNING successful_count
  if no row: ROLLBACK -> registration_daily_limit
  create role=user, activation_state=active, activation_source=public_registration
  create initial spend allowance from existing user-default policy in same transaction
  write terms proof + optional consent (false valid)
  consume verification token
  INSERT completion with unique user/request
  INSERT encrypted registration_created mail job with unique business key
COMMIT
```

任何中间失败回滚名额、token 消费、账号和通知；不出现“账号建了但未扣名额”“发了通知但事务回滚”。初始 AI 额度读取现有默认策略，无默认有效策略则明确失败并回滚，不为“自由注册”创造无限 AI 权限。

注册成功后转 `/login?registered=1`，登录成功保持当前 `/app` 默认目的地；不回 pending 审批页。采用已建立的 session 清理/CSRF 轮换策略，不从完成查询接口泄露邮箱、签发会话或重放登录。

### 4.3 限额、幂等与反滥用

- 公共状态接口可返回当日余量和下次北京时间零点，只是快照，不是名额保证。全站配额 429 + `Retry-After` 到下一日；不能泄露某个邮箱是否存在。
- 保留现有验证邮件的单邮箱冷却/小时/日发送预算及应用预算。发送邮件日 5 封和新注册日 5 个是两个不同计数器，命名、页面和测试必须区分。
- quota 满时最终 POST 不消耗验证 token；如果链接后来过期，提示重新申请，不能承诺原链接第二天仍有效。
- registration_request_id 由服务端生成并绑定验证 intent；客户端不能借任意 request_id 查询或命中其他人的完成记录。完成查询先校验该 intent 的 token 证明，仅回成功与登录地址，不返回账号/邮箱、不自动登录，且不能借重放更改研究选择。
- 网络重试以 completion 幂等，不重复扣数或通知；不因 GET 邮件预览、验证码重发、失败密码、重复已注册邮箱占名额。
- IP 前缀限流只是辅助反滥用，不能把同一网段当同一用户，不新加设备指纹追踪。

### 4.4 存量模式和未完成验证

- intent 固定签发时 flow_mode，不把旧“邮箱验证后待审批”链接在切 public 后静默改成直接激活；旧 pending 用户不自动批量激活。
- 已签发旧链接在有效期内可保留原流程；若当前模式策略不允许继续，则显示具体的“注册流程已更新，请重新开始”提示并保留可核对记录，不能发邮件却让入口无故 503。
- public 关闭时新的 public 最终建号必须失败；通过服务端同事务模式校验给出清晰边界。既有 active 用户正常登录，通知任务继续排水。
- 对已验证邮箱的旧 pending 用户保留原审核/邀请码激活途径，不迫使重复创建用户；若将来提供“转自由注册激活”，须独立实现其配额与条款确认，不能借新建接口自动合并或重新授权。
- 原有邮箱验证后的研究方向和测试申请流程仅适用于旧申请模式；public 不要求填写研究方向，不再把新账号写成待审批 test_application。

### 4.5 通知邮件

沿用 `registration_mail_jobs`；新增 `purpose='registration_created'` 与可空的 `business_key`，对该 purpose 的 `business_key='registration_created:<completion_id>'` 唯一约束。更新现有 purpose CHECK，不复用 verify token 执行账户动作。

邮件正文仅包含：注册时间、账号 ID/邮箱、来源模式、当天成功数、管理员用户列表链接。默认不含研究共享选择，避免管理员以此区别对待用户；不含密码、验证链接、会话 cookie、对话、图像、行为轨迹或可直接修改账号的令牌。

发送状态 queued/sent/failed/uncertain；确定未发出可按现有有界退避重试，结果不确定不盲目重复发送，后台提醒管理员核对。无法保证跨 SMTP 的 exactly-once，必须准确区分“任务只创建一次”和“投递状态不确定”。

public 关闭后：停止新的自助建号和相应新验证请求；已经成功注册的通知继续发送。不能沿用“非旧模式就不发任何邮件”的判断。

## 5. 服务必要数据与研究数据分层

| 数据层 | 谁能因何用途处理 | 不同意研究是否存在 |
| --- | --- | --- |
| 账号、用户保存的切片/标注/会话 | 提供用户请求的服务；必要运维受控访问 | 可以存在，不能因此转为研究材料 |
| AI 请求到当前模型服务商 | 为该次 AI 读片推理所需的概览、选取区域、有关对话及工具结果 | 用户主动使用 AI 时按已披露范围处理，与研究勾选独立 |
| 安全/错误/计费用量日志 | 安全、故障处理、计费核对；最小字段，不带全文对话/图像 | 按必要性保留，禁止换名作为行为研究数据源 |
| 研究副本/研究行为事件 | 仅指定研究者，在当前有效可选授权和资源权利范围内用于本软件改进 | 不得创建、分析、复制或导出 |

不能承诺“未同意研究就不产生任何 agent 日志”，因为恢复会话和向用户展示结果可能需要业务存储；应承诺这些业务记录不因运营者具有权限就被用于研究分析。

第一版研究范围：可见输入输出、可见工具动作及结果摘要、人工视野变化、标注操作类型。排除模型隐藏推理内容、系统提示、认证信息、原始附件二进制、全量原始切片和无关会话。研究用对话正文须按下节受控去标识化，不能只把 user_id 换成 hash 就称完全匿名。

对外部模型服务商的必要推理传输不等于允许供应商训练，也不等于允许研究者把日志交给其他 AI 做研究分析。供应商名称、用途、传输字段、处理地点和已核实的保存/训练政策放在版本化披露页面；无法核实的承诺不能写成事实。

## 6. 研究授权必须同时约束采集和使用

### 6.1 权威和边界

建立独立 `research_store.py` / `research_consent_store.py`（可按实际模块组织合并）。任何前端上报、后端轨迹复制、研究浏览、导出和后续分析作业均调用同一权威策略；不能只在浏览器或某个后台列表检查一次 checkbox。

判定至少同时满足：

- 当前真实用户本人、账号 active，非 owner 预览态、非 demo/公开分享访客。
- 当前 state=granted，scope/document version 有效，epoch 一致。
- 数据在本次 grant 生效之后产生，且产生时有相同 epoch 的有效研究授权。
- 该资源属于用户有权分享的范围。第一版只允许本人拥有、明确标记可用于本项研究的项目/切片；仅“可查看他人切片”不足以授权研究。
- 研究采集/使用功能开关开启；撤回/删除任务/账号注销状态未阻断。

grant 后才读旧业务记录并复制，仍是未经授权的历史回填，禁止。旧会话中新消息可以逐条筛选，但不能因 session.updated_at 更新就复制整个旧会话；混合参与者对话按每条内容来源和所有相关权限检查，不满足时不纳入。

### 6.2 数据建议

- `research_subjects`：随机伪名与 user_id 的隔离映射，研究查询默认不联 users/email；不是可逆信息的“匿名化”声明。
- `research_viewing_sessions`：随机会话 ID、subject、consent_epoch、scope_version、slide/project 的研究伪名、started_day、status；不要复制真实文件名、路径、患者标签。
- `research_viewer_events`：event_id、session_id、seq、action、schema_version、consent_epoch、受限 payload、server_received_at、expires_at；唯一(session_id,event_id)与(session_id,seq)。前端事件不含 user_id/邮箱。
- `research_conversation_items`：来源事件 ID 的非公开映射、subject、consent_epoch、消息来源、可见内容的去标识化版本、准入检查结果、expires_at。必须有可撤回的数据来源链，不能只存“匿名文本”而失去删除能力。
- `research_data_deletion_jobs`：用户/epoch、状态、在线/导出/备份清理进度、安全错误码；幂等可重试。

研究副本与运营会话分开目录/表/访问服务。批量研究读取只从受控研究存储，不能给分析任务挂载整个生产 sessions/数据库凭据。

### 6.3 协议撤回和在途竞争

撤回提交在数据库事务中锁 consent 行，state=withdrawn、epoch++，写历史和删除任务。返回成功后：

1. 当前页面停监听、清内存队列、关闭 session；同源其他标签页通过 BroadcastChannel 或 storage 通知刷新，跨域靠服务端复验，不依赖浏览器通知。
2. 前端旧 grant、旧 epoch、离线重传全部拒绝；默认不把研究事件持久化到 localStorage/IndexedDB。
3. ingestion 与撤回使用同一 consent 行锁定顺序，保证撤回提交后的写入不成功；在撤回前刚提交的数据由删除作业清理。
4. 已排队研究导出/分析撤销；运行中作业按受控小批次复查，在不再有效时停止后续读取和发布，并清除工作副本。首版不允许无法撤回的外部分析作业。
5. 在线浏览和导出再次查询授权，即使备份/副本清理尚未完成也不可继续用于研究。

再同意产生新的 epoch、新的研究 session，从此后新数据开始，不复活历史已拒绝、撤回或排队丢弃的数据。

## 7. 人工读片行为采集规范

### 7.1 事件类型与采样

新建 `static/research-viewer-telemetry.js`，由正式登录态 app 显式装配。不要自动在共享 `viewer-core.js` 初始化采集，以免 demo、公开分享、管理员预览也开始上传。

| action | 触发 | 最小 payload |
| --- | --- | --- |
| zoom_in / zoom_out | 用户滚轮、触控缩放、缩放按钮/快捷键一次连续手势结束 | 归一化 before/after bbox、image zoom ratio、input_kind、中心是否变化 |
| pan | 用户拖拽/键盘移动一次手势结束 | 归一化 before/after bbox、input_kind |
| observe_pause | 有效人工查看周期内出现一次稳定观察状态，见下节 | 当前归一化 bbox、image zoom ratio、evidence=inferred_stable_view |
| annotation_create / update / delete | 用户操作且业务写入成功后 | 工具类型、形状类型、归一化范围、匿名局部标注 ID、来源 human |
| annotation_accept / reject | 用户明确审核 AI 标注并且业务写入成功 | action、匿名局部标注 ID、来源 human_review |

缩放引发中心变化仍是一条 zoom 事件，changed_center=true；不再重复产出一条 pan。OSD 自动 fit、加载时居中、resize、跟随 agent、程序回放不属于人工动作。浏览器输入事件只作为观测证据，无法证明是真人，不能用于封禁或考核。

用底层真实输入建立短期 gesture context，由 animation-finish 等完成回调归并；不能只监听 `zoom`/`pan` 就上传。连续滚轮按 250ms 静默归一手势，拖动按 pointerup/cancel 完成，键盘/按钮按一次交互完成；阈值只在客户端内存使用，不作为研究时长字段上传。取消手势不伪造成功标注。

坐标使用 level-0 视野转 [0,1] 的归一化范围，四位小数，边界裁剪、非有限值拒绝。image zoom ratio 明确不是物理倍数；MPP 缺失不伪造物理倍率。研究事件不含任意文本字段、标注原文、患者信息、鼠标完整轨迹、键盘逐键内容或截图。

### 7.2 “等待动作，不是等待时间”

本版采用用户已确认的自然停留观察语义，不新增必须点击的按钮：

- 完成一次人工打开/移动/缩放后，页面可见、窗口聚焦、当前切片 ready、没有加载遮罩/手势/弹窗/绘制中的视野稳定 **2 秒**，记录一次 `observe_pause`。
- 2 秒仅为客户端分类阈值，不能解释为用户真实阅读意图；payload 固定 evidence=inferred_stable_view。
- 每个稳定观察周期最多一次；保持静止 1 分钟也只记一次，不用心跳反复记 wait。
- 离开标签页、失焦、切片加载/请求失败、自动动画、纯 agent 移动、应用阻塞均取消检测；下一次有效人工交互才开启新周期。
- 不上传 `duration_ms`、`dwell_ms`、等待开始/结束时间或可还原单次等待长度的客户端精确时间戳；事件以 seq 保留先后关系。server_received_at 只用于接收、保留期和运维，不计算等待时长。
- 不将 observe_pause 当作阅读认真程度、病理诊断能力、医生工作绩效或眼动证据。

用户已在本任务确认上述“停留观察动作”定义。2 秒是本方案的工程默认阈值，可通过版本化规则调整；调整不允许新增等待时长字段或将推断改称真实注意力证据。

### 7.3 传输和服务端

建议接口：

- `POST /api/research/viewing-sessions`：登录+CSRF，提交业务 slide/project ID 供 ACL 验证，服务端创建研究 session 并绑定 user/epoch；返回研究 session、schema 和过期信息。没有授权则不创建。
- `POST /api/research/viewer-events`：`{viewing_session_id, consent_epoch, events:[...]}`；后端从 session 解析主体和切片，不信任客户端 user/owner/资源映射。
- 批次最大 50 条、64 KiB，内存缓冲最大 200 条；周期发送/达到阈值发送均可，失败不影响读片。全量关闭研究采集时没有该网络请求。
- 每个事件必须通过 enum、seq、id、数值和字段白名单校验，额外字段整批拒绝；未授权 403，版本不一致 409，限流 429，存储故障 503；响应不含账号、原始文件路径。
- 每用户/研究会话有有界速率限制，避免重试打满后台；超限可丢观测事件，不能阻塞标注或主业务。相同 event_id 重传幂等；不同内容复用 ID 为冲突，不覆盖旧事件。
- logout、切用户、切片、撤回时清除旧缓冲；会话过期需重新建 session，不能将旧数据贴到新授权上。
- 不用 1 次采样=1 次真实操作来对用户评分；数据集保留归并版本及粗粒度丢弃计数，让分析知道不完整。

### 7.4 标注和对话

标注习惯首版只收操作类别/形状/几何与是否审核 AI，不收自由文本标签、笔迹逐点序列或原始图片。借此可分析工具选择、调整/撤销/审核的顺序，而不是猜测医学判断。

对话研究副本不是普通日志：白名单抽取用户可见文本及可见 agent 事件；剔除附件与完整资源定位，采用本地规则/受控处理移除直接标识符。发现可能含患者/第三方个人信息、来源权利不明或处理失败则拒绝进入研究集，不能把“研究人员之后再看”当清洗手段。不能调用未披露的第三方 LLM 做脱敏。

只去掉姓名不能保证匿名；研究库仍按个人信息强度保护。首版不开展不可撤回的模型权重训练、公共数据集发布或第三方数据分发。后续要做时另立方案与授权范围，不借“改进软件”无限扩张。

## 8. 后台、保留期与退出

后台新增：

- 注册当日计数/剩余额度/时区、通知任务 queued/failed/uncertain 状态；不展示验证码或邮件密文解密正文。
- 用户列表只读展示当前授权状态/版本/生效或撤回时间。旧 consent 单独标“历史版本，未授权当前研究采集”。
- 研究页面只提供当前有效授权范围的聚合、会话动作序列；研究查看本身留审计。默认不显示用户邮箱/真实文件名。
- 第一版研究者为协议中明确列出的软件开发与研究负责人本人；不要把所有 owner/admin 自动变成研究查看者。权限单独命名，例如 research:read；授予他人前需更新披露和授权范围。
- 不提供一键全库导出、公共链接或第三方分享按钮。后续授权的内部受控导出必须带数据来源和撤回追踪，访问记录入审计。

建议作为本版实现约束的产品期限（不是声称法律规定的期限）：

| 对象 | 默认期限/处理 |
| --- | --- |
| 研究动作、研究用对话/轨迹副本 | 每条最多 90 天，到期删除 |
| 已不含可识别个体或可逆映射的统计结果 | 最多 365 天；不能证明不可识别则仍按研究原始记录处理 |
| 研究撤回/删除 | 立即停止使用；在线副本和研究工作副本 7 天内删除 |
| 隔离备份 | 最长 30 天轮换；恢复必须先应用撤回/删除清单，不能让数据复活 |
| 授权历史证明 | 当前授权期间以及结束后最多 1 年，仅为授权核验/争议处理，不用于研究；法定必要留存另行隔离说明 |
| 临时研究导出 | 首版关闭；后续若启用，最长 7 天，受撤回删除约束 |

保留任务、备份到期和恢复演练都是上线验收的一部分。若运行环境做不到这些期限，先改成真实可实现的期限并同步协议，再发布；不能发布无法兑现的删除承诺。

数据操作按应用和组织权限约束；拥有服务器 root/数据库管理员能力者仍可能技术性访问，不能宣传密码学意义上“本人也绝对看不到”。需要最小权限、独立读取入口、加密、访问审计和操作纪律；紧急支持访问仅限用户授权或必要安全事件，不能用于研究。

## 9. 协议内容与实际行为一致

用户已决定：协议文稿不是正式法律文件，不填写真实姓名、机构、联系邮箱或生效日期，以向用户说明数据使用规则为准。文稿仍需版本/hash 受控发布；模型服务商按真实运行配置在披露页列出名称与用途，不臆造未核实的保存或训练政策；后续若转为正式法律文件，再补充主体信息并发布新版本。

必要推理与可选研究分开；对于涉及敏感个人信息、向其他处理者提供或跨境的具体场景，不能假设这两个注册 checkbox 自动覆盖所有处理要求。第一版注册保持两个勾选；若某个 AI 功能依法需要额外的具体告知/单独同意，应在实际使用该功能时完成对应流程或暂不开放该数据场景，不新增一个强制“同意一切”的注册项。

依据参考：[个人信息保护法](https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm)关于告知、撤回、敏感信息和对外提供的要求；[网信办跨境规定问答](https://www.cac.gov.cn/2024-03/22/c_1712776611649184.htm)对去标识化与匿名化的区别。此处用于设计边界，不宣称文稿已完成所有上线合规事项。

## 10. 分阶段任务及验收

### P0：协议与迁移底座

发布静态可访问/可下载的版本化草稿页面、文档注册表、接受历史、consent 当前状态与历史、旧选项兼容层。采集开关关闭。编写 migration 空库/存量升级/重跑测试；旧 true 不自动 granted；旧 false/缺行保持拒绝。

完成标准：两个 checkbox 可独立操作；条款缺失后端拒绝；研究 false 成功；版本/hash 不匹配拒绝；直接 API/无 JS/移动端/键盘/跨设备邮件验证都保留真实选择；旧按钮不能重新打开未授权采集。

### P1：公共注册和通知

实现原子流程、每日名额、公用模式前置、旧模式兼容、通知 purpose/幂等和 worker。初始额度用现有策略，不改变受邀/已有用户额度。

验收矩阵：

- 20 个并发不同邮箱成功验证，只允许 5 个新自助账号 active，恰好 5 条 completion、5 个通知任务；跨进程与跨入口同时测试。
- 同 token/请求重试不多扣数、不重复通知、不泄露已有身份、不直接签发新会话。
- 零点前后、满额、事务回滚、DB 重启、邮箱唯一冲突、无初始额度配置均正确。
- 验证邮件阶段不占名额；配额满不消耗 token；过期链接给真实重试提示。
- SMTP 成功、确定失败重试、uncertain、通知配置缺失、worker 重启与 public 关闭场景明确。
- 研究同意/不同意两组注册获得相同账号状态与额度。public 不再进申请审批页。

### P2：可撤回研究权限和账户设置

完成 grant/withdraw CAS、多标签页、服务端权威、资源权利检查、读取/导出门控和删除链。研究采集仍关闭。

验收：用户 A 不能替 B 同意；owner 预览不能代用户同意；管理员无按钮强制 grant；缺版本/旧epoch/旧 true 均拒绝；撤回与在途写入竞态、撤回再同意、后台任务运行中撤回、备份恢复不复活。

### P3：人工行为与 agent 研究副本

增加独立采集模块、typed schema、服务端写入口、本地去标识化、研究管理视图。只在新协议和当前授权齐备后开启。

验收：

- false/未注册/demo/公开分享/预览态没有研究请求与研究写入；关闭研究功能后必要 AI 功能仍可用。
- 人工滚轮/拖动/按钮/触控各生成正确归并事件；自动动画、跟随 AI、resize、加载失败、隐藏页不算人工操作。
- observe_pause 在稳定周期只出现一次；后台等待/网络卡顿不产生；事件网络包与数据库无单次等待时长、开始/结束字段。
- 标注失败不产成功事件；AI 自动标注与人类审核不混同；不上传自由文本标注。
- 对话只包含授权后允许的消息/事件，旧会话和不同意用户不能被研究批量扫描。
- 重传、未知字段、超限批次、伪造资源、越权、版本冲突、断网和撤回期间发送均安全；研究故障不影响读片与业务保存。

### P4：发布包与上线

交付迁移、配置示例（不含真实凭据）、版本化协议、测试摘要、schema 字段表、授权/删除演练记录、注册邮件样例、更新/回滚步骤。更新管理插件版本/hash/pin，HistoPilot 如改内部协议则双方契约测试和构建都完成。

已有账号不强制自动同意新研究条款；在账户设置呈现自愿邀请。必选服务条款变更需要确认时，保留注销、删除/导出本人数据和撤回研究入口，不通过全站拦截阻止用户行使这些权利。

上线顺序：先兼容 schema 与后端→双协议前端/设置→确认邮件通道/管理员通知→切 public 并检查名额→最后独立启用已验收的研究采集。若用户只授权实现而未授权生产更新，交付可发布工件并明确未上线。

回滚先关闭研究采集和新 public 建号，不删除已经合法创建的账号、完成计数、授权凭据或撤回记录。保留管理员通知排水；回滚 UI/镜像时验证不会让旧 checkbox 绕过新 gate。禁止回滚到会忽略撤回记录的研究读取实现。

## 11. 实施后的测试入口

以下新增文件由实施 agent 创建，并将第 10 节验收矩阵落实为真实 DB/接口/浏览器行为测试；本文档阶段不创建空测试或运行生产流量。既有测试文件按届时仓库实际位置调整。

```bash
# PathTogether：保留旧注册、限流、激活、身份、额度和旧测试申请回归
.venv/bin/python -m pytest tests/test_registration_invites.py tests/test_registration_rate_limit.py tests/test_email_verify_activation.py tests/test_identity_email_unification.py tests/test_account_auth.py tests/test_spend_store.py tests/test_test_application_api.py tests/test_repair_invite_activated_applications.py -q
# 新增真实 PG 并发、通知、协议、撤回和研究准入回归
.venv/bin/python -m pytest tests/test_public_registration.py tests/test_user_agreements.py tests/test_research_consent.py tests/test_research_viewer_events.py -q
# 新增 UI 装配和人工手势归并测试
npx vitest run tests/js/entry-auth.test.ts tests/js/registration-consent.test.ts tests/js/research-viewer-telemetry.test.ts
# 新增端到端：实际两勾选、验证注册和查看切片后的网络/撤回行为
npx playwright test tests/e2e/registration-consent.spec.ts tests/e2e/research-viewer-telemetry.spec.ts
```

HistoPilot 若增加研究轨迹抽取：运行 `test/research-event-projection.test.ts`（新增），以及 `test/events.test.ts`、`test/session-store.test.ts`、`test/server.test.ts`、`test/access-and-attachments.test.ts` 和 `npm run build`。不触碰业务会话时不为了计划完整而伪造该仓变更。

静态检查还需涵盖：协议页面未登录可读、没有第三个注册 checkbox、没有预勾选研究、前后端字段一致、研究网络 payload 不含等待时长或直接身份、文稿版本/hash/pin 与实际文件一致。

## 12. Agent 最终交付格式

逐项标记：已实现/已测试/已发布/未解决。列出日限额并发证据、两个勾选的独立性、无同意零研究写入、撤回即时阻断、等待动作字段检查、通知可靠性、协议版本/hash 元数据。不得用“用户点击同意了”替代服务端证据，不得用已有运营日志冒充新授权研究数据。
