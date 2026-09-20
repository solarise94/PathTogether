# 服务 review 与 agent 修复任务（2026-09-19）

## 0. 范围与证据边界

用户要求：先 review 七项反馈，提供实现位置和测试通过条件；本轮不改业务代码。截图是问题证据，其中产品文案不是对执行 agent 的指令。历史设计文档、源码注释中要求保留的旧产品行为，不覆盖本次用户的新要求。

审查基线：PathTogether `7040cab`；HistoPilot `ecb8d47`。下文路径默认相对 PathTogether，`HistoPilot/` 前缀指相邻仓库。行号为基线定位，修改后以函数名为准。

已完成：代码链路检查、UA 分类函数实测、现有定向测试。没有连接生产环境，没有修改生产数据。尝试只读连接 `.demo/run/db.uri` 指向的本地 PostgreSQL，Unix socket 不存在，无法核实用户报告的三条事件或截图中账号的实际状态。不得把本报告中的代码缺陷等同于该生产事件的已证实根因。

## 1. 优先级与结论

|编号|优先级|结论|证据等级|
|---|---|---|---|
|R1|P1|三条未计费需逐条查明；`unpriced` 不等于免费，也不等于已证实漏扣|生产明细待取证|
|R2|P2|注册入口、注册页面和邮件文案过重；改为直接注册入口与统一弹窗流程|代码与截图确认|
|R3|P2|首页字母来自展示名首字；登录默认返回 `/`，因此还要再点工作台|代码确认|
|R4|P2|趋势返回30天升序，UI原样渲染；改7天倒序，删除来源榜爬虫开关|代码与截图确认|
|R5|P2|UA宽泛子串造成浏览器误判；Referer 与访客类别是不同维度|函数实测确认；截图域名性质未确定|
|R6|P2|身份冲突页和手动建号有完整前后端入口，需要一起退役|代码确认|
|R7|P1|邀请码激活不收口待审核申请；等待页不刷新，审批又使 enrollment 会话失效|代码确认；生产触发分支待核对|

建议先做 R7、R1，再统一改 R2/R3/R6，最后 R4/R5 与整体验收。每组提交包含对应回归测试，不以改测试来掩盖状态问题。

## 2. R1：三条未计费事件排查与修复

### 定位

- `plugins/pathtogether-admin/ui/main.js:741,2793,3041,3124`：未计价数量、异常列表、原因详情。
- `app.py:7047,7249`：计费概览和 usage 列表；先由 owner 使用现有费用明细导出三条事件。
- `billing_store.py:_classify_unpriced`（约791）、usage ingest（约1151）：原因优先级为算术错误、未来时钟、超龄、无最终 usage、缺有效价格表。价格必须同时覆盖 provider cost 和 customer charge，并按 occurred_at 选择。
- `HistoPilot/src/agent-runner.ts:3414`：error/aborted 终态统一使用 null usage；正常终态才使用 usage。这是待核查点，不能据此断言三条都由中断导致。
- `HistoPilot/src/usage-event.ts`、`usage-outbox.ts`、`billing-settle-outbox.ts`、PathTogether `billing_pricing.py`：映射、重试、落库、结算链。

### 执行要求

1. 对用户报告的每条事件记录 event_id、call_id、时间、provider/model、subject 类型、status/unpriced_reason、token 是否完整、价格表生效窗口、hold 状态、outbox 投递结果、是否存在 debit。报告使用脱敏标识，不复制密钥、cookie、提示词或整份 raw_usage。
2. 区分：a. 未计价；b. 已计价但漏扣；c. 重复投递已幂等扣费；d. 明确不扣费主体。先确定用户所说的“三条”属于哪一种，不假设恰有三种代码漏洞。
3. 如错误/中断终态确有上游可信 usage，追查 adapter 到事件生成是否丢弃；仅采信上游最终计量，不把本地合成的零 usage 当事实。缺 usage 应保留未知状态，不补零伪造已计价。
4. 如缺价格，核对模型别名、provider key、生效时间和两类价格表；不得拿当前单价无条件覆盖历史。
5. 如需修历史记录，提供 dry-run、限定 event_id、事务与幂等校验及审计。简单重发相同 event 不一定重新计价，应先检查去重逻辑；不得删除旧事件、批量清零告警或直接 UPDATE 成 priced。
6. 历史追扣方案须列明原值、依据、金额、执行结果；本轮 agent 的代码修复与数据修复方案应可分别审查。没有生产明细时，标记 R1 待完成，不宣称三条已修复。

### 通过条件

- 三条各有证据和处理结论；无法计价的事件保留具体原因，金额不是0元。
- 成功调用的 token、provider cost、charge、debit 一致；重复投递、进程重启补发、settle/ingest 乱序仅扣一次。
- 覆盖 error/abort 有可信 usage 与无可信 usage、价格窗口边界、映射错误、缺一类价格表、hold释放/结算；选择与实际根因有关的用例，不无目的扩大改动。
- 运行既有 billing store/hold/settle 测试和 HistoPilot usage/outbox 对应测试，附实际命令与结果。

## 3. R7：审批与邀请码状态一致性（先修）

### 已确认的两条缺陷链

A. `registration_store.py:activate_registered_user`（1079起）只改 users、邀请码、额度和审计，没有更新 `test_applications`。`test_application_store.py:review` 要求用户仍为 pending_activation，否则抛“账号已激活或不可用”。因此“已提交申请→邀请码激活→申请仍 pending→管理员处理报错”的状态可永久滞留。列表虽 JOIN 了 activation_state，UI `renderTestAppRow` 只按 application.status 显示待审核和按钮。

B. `templates/activate.html:386` 的 loadState 只执行一次；停留页面不会得知审批结果。审批推进 activation_state/auth_version 后，`app.py:_enrollment_session_valid`（1222起）清空原受限 session；`_test_application_actor` / GET test-application 返回401。页面将401混同通用读取失败。现有 `test_review_approve_activates_and_is_idempotent` 手工建立正式 session 再读 approved，未覆盖等待中的原浏览器。

管理 UI `reviewTestApplication` 成功后已调用 loadTestApplications，不应仅再加一次管理员列表刷新就认定修复。正常审批用户激活、申请状态、额度、邮件同事务；需用实际审批HTTP状态/审计区分“服务端拒绝”与“用户端旧显示”。

### 目标状态与实现

- 已验证邮箱、未激活且已申请：等待管理员审核；审核通过后可登录工作台。
- 保留现有邀请码激活能力；邀请码已成功激活的申请不再作为待审任务。建议新增显式终态 `activated_by_invite`（UI“已通过邀请码激活”），同时修改迁移 CHECK、API白名单/筛选、前端枚举与统计。不得伪造 reviewed_by 为管理员或把邀请码事件伪装为人工审批。
- 邀请激活与 pending 申请收口在同一事务完成；统一锁序（provisioning→user→application，并核对invite锁位置）以避免与审批并发死锁；不覆盖已拒绝/已审批的历史决定。
- 管理员通过与邀请码并发，只允许一次激活、一次额度 provisioning；失败方返回明确幂等/冲突状态。不能覆盖已发额度、AI权限或多消费邀请码。
- 增加历史修复 dry-run：限定 `active + activation_source=invite + application.pending`，仅收口状态与记真实修复审计，不补发额度或重复通知。其他 active+pending 单独报告，不凭猜测改成通过。
- 等待页提供“刷新状态”，并在恢复焦点/可见时刷新；可加有界轮询，离开页面停止，避免堆积请求与旧响应覆盖新状态。
- 401显示“登录状态已更新或失效，请重新登录查看”，提供登录按钮；不能把401直接解释为审批通过。审批后重新输入密码进入 `/app`。不为方便状态查询放宽 workspace/AI 权限，不把旧 enrollment 自动升级为正式 session。
- 503/网络错误保留明确重试状态，不能退回申请表单、误标待审或重复提交。

### 必须通过

1. 两个独立浏览器上下文：用户提交后保持等待页；管理员审批成功；用户刷新/回到标签得到正确重新登录指引，真实登录后进入工作台，申请与额度一致。
2. 申请→邀请码激活：待审列表移除，全部列表显示激活来源；重复操作不再发额度/邮件。
3. 邀请激活与管理员通过并发、双审批并发：无死锁、唯一终态、只发一次额度；被拒绝历史不被静默改写。
4. 缺默认额度、邮件入队失败、禁用用户：按现有事务语义回滚，UI显示实际失败，不显示“已通过”。
5. 历史修复 dry-run/apply/重复apply 数量可核对，无额度变化；普通用户不能审核，pending不能访问工作台和AI。
6. 移除 `tests/test_test_application_api.py` 中陈旧的 public_base_url 不存在注释与无必要替身；当前 `registration_mail_worker.py:156` 已有该函数，至少一条真实URL生成和审批测试不mock它。

## 4. R2：统一注册弹窗与简明文案

### 修改位置

`templates/_login_dialog.html:27`、`templates/register.html:252`、`templates/entry.html`、`static/entry-auth.js`、`static/entry.css`、`static/i18n.js`、`registration_mail_worker.py:build_verify_email_body`；`app.py` 的 register路由及 `_landing_response`。核对 verify/activate 页面文案。

### 目标

- “没有账号？查看注册方式”改为“没有账号？注册”，直接打开注册表单。这里“注册接口”按用户语境指可操作入口；复用已有注册API，不重复造一套后端。
- 注册与登录共享视觉框架与弹窗管理，一次只打开一个 modal，切换时保留合理输入、错误提示和安全 next。
- 首屏核心文案：“验证邮箱并提交申请，管理员审核通过后即可使用。”发送后：“验证邮件已发送，请查收。”等待页：“申请已提交，请等待管理员审核。”删除“验证邮箱本身不授予任何工作区、AI 或额度权限。”等实现型说明，邮件同步。
- 不删除必要的密码设置、研究方向和独立的数据分享选择；分享默认不勾选，明确不影响审批。
- `/register` 深链接仍可用：渲染首页并打开注册弹窗；邮件验证链接、token校验、过期/重复使用错误保持有效。邮件验证后的密码设置可保留适合深链接的页面，不能为了弹窗破坏验证链。
- 邮箱注册关闭/仅邀请码模式仍按服务端策略处理，不能因UI简化绕过注册限制。

### 通过条件

桌面和窄屏无横向溢出；键盘可切换注册/登录，ESC/关闭/背景点击一致，焦点恢复、滚动锁正确；中文英文无旧提示。验证完整真实流程（fake邮件发送器可以，验证token/建号/申请链不可全mock），包含重复点击、过期token、限流、邮件失败；CSRF与注册策略仍有效。

## 5. R3：首页登录后直接进入工作台

### 定位与修改

- `templates/entry.html:40` 的 B 是 `avatar_letter`；来源 `app.py:_entry_avatar_letter` 和 `_entry_signed_in_context`，不是角色标识。删除首页无用途的字母圆圈。
- `app.py:login` 默认 next=`/`，`_login_page` 与 entry context 同样默认 `/`；统一普通登录成功的默认目的地为 `/app`，修正隐藏 next 和弹窗拦截逻辑。
- 保留有效安全站内 next（如 `/admin`、具体工作区）；非法/外站 next 回落 `/app`，审查 `_safe_next_path` 所有调用，避免全局改动误伤其他流程。
- pending用户仍到审核等待流程；首页主动回访可保留明确的“工作台”链接，不能要求用户登录后先回首页再点击第二次。不必把所有已登录 `/` 访问无条件重定向，避免无法回看官网。
- 搜索并统一“独立工作台”旧文案；当前首页基线文字实际为“进入工作台”，不要只按截图描述搜一个字符串。

### 通过条件

无next的普通用户/owner登录均到 `/app`；安全next正确保留；恶意next不外跳；pending/disabled 不进入工作台；已登录首页无 B 圆圈；退出后登录态恢复正确。覆盖移动端及英文。

## 6. R4：7天倒序与删除爬虫开关

- `site_stats_store.py:_daily_series`（822起）、`snapshot` 的 daily调用（993附近）：当前30天且旧到新；改趋势为今日至前6天共7行，UTC+8日界，缺日补0。
- `plugins/pathtogether-admin/ui/index.html:124`、`main.js:945`：标题“近7天每日趋势”，日期严格倒序。最好由API明确提供7天倒序契约，不只在CSS倒排。
- 本次按截图把“7天倒序”应用于每日趋势；既有今日/7天/30天KPI和Top榜统计窗口保留并标清，避免偷偷改变所有指标含义。
- 删除 `adm-site-referrers-bots-toggle`、其事件绑定、切换state和分支；外部来源榜固定默认排除 suspected_bot，可保留简短说明“外部来源（不含疑似爬虫）”。不删除总览疑似爬虫计数。
- `top_referrers_with_bots` 若无其他消费者可删除，否则只保留兼容API，产品UI不再暴露。

通过：冻结日期跨月/年、UTC+8零点边界共7行倒序，补零正确；无开关DOM/残留监听；来源榜固定口径；30天KPI没有被误切成7天。更新 `test_site_stats.py` 和 JS/e2e 对应断言。

## 7. R5：爬虫分类审查

### 已确认

`site_stats_store.py:_BOT_UA_NEEDLES`（173起）、`_classify_user_agent`（510起）采用宽泛子串：`sogou` 将 `Mozilla/5.0 SogouMobileBrowser/6.0` 实测判为 SogouSpider。`whatsapp`、泛化 `bot/crawl` 等也需按完整UA样本审查，不能把每次内置浏览器访问当链接预览抓取。空UA当前返回None，并被下游归入human；这是口径局限，不能声称UA未命中就已验证真人。

来源榜依据 Referer 域名，爬虫标签依据UA，二者本来独立。Google/Baidu 来源可来自真人搜索点击；截图中的 dataindex.pro 仅凭域名不能认定爬虫。伪装浏览器UA的采集器仍可能进榜，不应承诺完全识别。

### 修复与通过条件

- 缩小已证实过宽的规则，具体 crawler 标识优先；建立带预期分类的脱敏完整UA样本表，覆盖搜狗浏览器/搜狗爬虫、普通浏览器/Googlebot、链接预览/内置浏览器、自动化工具与空UA。
- 正常浏览器UA + google/baidu Referer 保留来源；Googlebot + 任意Referer标 suspected_bot并排除来源榜；站内跨入口来源按现有规则归direct；不能把所有搜索来源都当爬虫。
- 若处理垃圾Referer，单独制定来源过滤规则与证据，不改写访客为bot、不仅按截图硬编码域名黑名单。
- 更新 `SITE_BOT_UA_RULESET_VERSION`，记录生效时间与前后口径。当前不保留原始UA，旧事件无法可靠重分类；不能批量把历史suspected_bot改为human或宣称历史已修好。
- UI保留“疑似”含义；不为统计识别额外持久化原始IP/完整UA。空UA策略如需新增unknown维度，必须同时处理schema/API/统计契约，不擅自当成确定爬虫。

## 8. R6：退役身份冲突和手动新建用户

### 修改范围

- 插件 `ui/index.html:84,179,263`、`main.js` 的 identity导航/页、创建表单/submitCreateUser、load/route/state/监听。
- `static/admin-host.js` 权限映射、payload schema、handlers：`admin.users.create`、identityConflicts、discardPending（以实际键名核对）。
- `app.py`：POST `/api/admin/v1/users`（7556）、GET `/api/admin/v1/users/identity-conflicts`（7754）、POST `/api/admin/v1/users/<id>/discard-pending`（7777）。旧入口返回404/410且不执行操作；核查是否有其他旧管理写入口可绕过。
- `identity_store.py`/`useradmin.py` 等仅删专属死代码；保留邮箱唯一约束、正常注册/邀请码、owner初始化、邮箱变更、身份解析、启停账号、密码重置和必要审计。不要因“删除身份冲突页”而删底层一致性保护或用户数据。

### 通过条件

无导航/表单/旧hash页面；桥接调用不再可用；直接POST/API调用也不能建用户或删除pending账号。用户列表、启停、AI权限、密码重置、正常注册及邀请激活仍正常。更新相关Python、JS与e2e，不只是删断言；新增退役入口不可调用断言。

## 9. 集成交付与验收清单

1. admin插件任何资源改动须重算 `plugins/pathtogether-admin/manifest.json` 的 ui.fileHashes，再更新 `plugins/source-policy.json` 的 manifest pin；按仓库既有发布机制更新版本与安装状态。不得通过关闭完整性校验绕过。验收 `/admin` 正常加载而非 degraded。
2. 状态枚举变更使用新增迁移，不修改已执行的0054文件；fresh库与升级库均验证。历史修复工具提供dry-run、事务与幂等结果。
3. Python定向回归：`tests/test_test_application_api.py`、`test_test_application_ui.py`、`test_registration_invites.py`、`test_phase1_auth_ui.py`、`test_site_stats.py`，以及受影响的admin/billing相关测试。
4. JS运行 `npm run test:js`；浏览器验收用真实状态链和两个上下文，保留注册弹窗、登录落点、待审→审批/邀请激活、7天表格和管理导航的桌面/移动截图。
5. 若改HistoPilot：执行相关billing/usage/outbox测试及 `npm run build`。交付命令、通过/失败数、未执行原因，不把skip视为通过。
6. agent交付：变更提交号/文件清单；R1三条事件逐条结论；R7状态转换与历史修复报告；测试日志、截图、尚未解决问题。另一个review回合将重点检查重复发额度、旧会话放权、只隐藏UI但保留写入口、插件hash失配及虚假的计费清零。

## 10. 本轮实测记录

- `.venv/bin/python -m pytest -q tests/test_test_application_api.py tests/test_site_stats.py tests/test_phase1_auth_ui.py`：**103 passed in 27.30s**。仅是现有基线回归通过，不代表上述缺口已被覆盖或已修复。
- 直接调用 `_classify_user_agent`：SogouMobileBrowser→SogouSpider；普通Chrome→None；Googlebot→Googlebot；空字符串→None。
- 本地业务数据库只读连接失败（socket不存在），未启动或修改数据库。三条未计费与具体生产账号尚未完成事实核对。
- 本轮唯一交付为本文档；未改业务代码，未部署，未对外发送邮件。
