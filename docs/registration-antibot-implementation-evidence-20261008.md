# 注册防刷与入口一致性：实施记录与上线前置

日期：2026-10-08（Asia/Shanghai）
分支：`registration-antibot`（基于生产线 `upload-convert-first` 5fa38db5；已部署，生产代码 revision `6ce10281`，尚未推送）
设计：`docs/registration-antibot-and-author-help-design-20261008.md`

## 1. 已实现

- Turnstile（`registration_antibot.py`）：服务端 Siteverify，`registration_start` / `registration_resend` 两个 action；token 非空且 ≤2048 才发请求；总等待约 5 秒，网络错误重试一次并复用同一 `idempotency_key`；必须 `success`、action 一致、hostname 在白名单且等于本次请求 Host。开启但未配齐、或使用 Cloudflare 测试密钥（未显式允许）时不发信（fail-closed）。
- 发送额度：同邮箱滚动 24 小时最多 2 次接纳投递（首发 + 重发，跨域名、跨进程合并），两次至少间隔 5 分钟；保留每 IP 前缀 30 次/24h 与全站 40 封/24h。计数与入队在同一事务，固定顺序 advisory lock（全站 → 邮箱），锁内重查；Turnstile 失败不占额度。
- 保留有效链接：冷却期重复申请不作废 token；重发经 `registration_mail_redeliveries` 复用原 token 与过期时间；剩余有效期不足 5 分钟才签发新 token；已完成注册的邮箱返回中性状态。worker 发送重发前锁原作业复核未消费/未作废/未过期/未完成，否则取消。
- 入口一致：统一站点映射 `histopilot.cn` / `histopilot.com`（旧 `pt.solarise94.fun` 归 `.cn`）；`entry_origin` 与 `form_locale` 在入队时冻结，邮件主题、站点名、验证链接、帮助链接、语言都取冻结值，worker 不再读 `PUBLIC_BASE_URL` 选域名。生产环境未知 Host 拒绝发信；`X-Forwarded-Host` 等头不参与。
- 幂等与回执：表单 `submission_id` 为服务端 HMAC 签名的无状态 id，重放在 Turnstile 之前直接回放已记录状态；`POST /register/resend` 绑定会话内匿名 receipt，receipt 不含 token 或账号身份。
- 页面：注册弹窗按需加载 Turnstile（仅注册视图可见时，显式渲染，过期/出错/重新显示/bfcache 时 reset）；无 token 时就地拦截提交；脚本加载失败给中性提示、重试与求助入口；提交后状态（已提交/冷却/重发已提交/新链接/处理中/上限/挑战失败/挑战不可用/暂不可用）配倒计时与求助入口；`/registration-help` 帮助页（固定原因词表、中英 mailto、复制邮箱、返回注册）；验证页错误态带求助链接。CSP 仅在渲染注册弹窗且 Turnstile 启用时追加 `https://challenges.cloudflare.com` 到 `script-src`/`frame-src`。
- 迁移：`0079_registration_antibot_redelivery.sql`（jobs/intents 加入口与语言列；新增 redeliveries、submissions 两表）。

## 2. 验证

- 后端门禁（独立复跑）：注册/CSP/迁移/认证相关 15 个文件 523 passed；R1 修复后相关文件 292 passed。
- 迁移演练：生产 schema-only dump（只读）+ 生产 `schema_migrations`（至 0078）→ 本地 `ensure_schema` 应用 0079 成功，二次执行幂等。
- 前端：`vitest run tests/js` 60 文件 949 passed；真实浏览器（Chromium，Cloudflare 测试密钥，假邮件发送器）37 项检查通过，无 CSP 违规；截图见本机 `/home/solarise/pytest-tmp/regab/shots/`（20–26 为终版）。
- 全量 pytest：见第 5 节。

## 3. 配置（全部 env）

| 变量 | 生产值 | 说明 |
|---|---|---|
| `REGISTRATION_TURNSTILE_REQUIRED` | `1` | 开启后所有可触发投递的请求必须通过挑战 |
| `TURNSTILE_SITE_KEY` | `0x4AAAAAAFQ5a-1qUKR2Uyyz` | 公开 sitekey |
| `TURNSTILE_SECRET` | 由 owner 写入 0600 secret env | 不进仓库、模板、日志、聊天 |
| `TURNSTILE_HOSTNAMES` | `histopilot.cn,histopilot.com` | 不得含 localhost/127.0.0.1 |
| `REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS` | 不设置 | 仅开发/测试 |

## 4. 原上线前置清单（实施时记录；完成情况见第 8 节）

1. Cloudflare 控制台确认该 widget 的 hostname 列表包含 `histopilot.cn` 与 `histopilot.com`，模式为 Managed。
2. owner 将 secret 写入发布目录的 0600 secret env（参照 `cos.secret.env` 的做法）；本机无 Wrangler，代理未获取也不应获取 secret。
3. 该次发布的 deploy.py 需把上表变量加入 EXTRA_ENV 白名单（否则 prepare 的 shape 比对会中止）。
4. 部署须逐次批准；上线后用真实 sitekey 做受控实测：一次成功提交 + 同 token 重放被拒；`.cn` 在国内网络、微信/邮箱内置浏览器加载挑战；`.cn`、`.com` 各一封受控邮件核对标题/语言/链接/帮助链接。
5. 建议灰度：先以 `REGISTRATION_TURNSTILE_REQUIRED=1` 在私有端口验收（私有端口 Host 不在映射内，按设计会拒绝发信，属预期），公开入口验收后再观察挑战通过率与求助量。

## 5. 全量回归

- 全量 pytest（`--ignore=tests/test_e2e_pg_reap.py --ignore=tests/e2e`，b809eab9）：2973 passed / 118 skipped / 3 failed。
  - `test_stage2_ui::test_containerfile_ships_app_modules`：Containerfile 漏 COPY `registration_antibot.py`（镜像会在 import 时崩溃）——真实发布阻断，已由 a323a9be 修复，该文件复跑 20 passed。
  - `test_conversion_drain::test_compare_prints_reservation_change_count`：新 worktree 缺 `.gate-tmp/` 目录导致写文件失败；建目录后单跑通过，基线 5fa38db5 同样通过。环境问题。
  - `test_spend_total_allowances::test_settle_release_expire_projection_accurate`：单跑通过（基线也通过），全量负载下的已知抖动。
- vitest `tests/js`：60 文件 949 passed。
- Playwright：`homepage-onboarding`、`toolbar-account-upgrade`、`entry-media` 30 passed（需把 `.venv/bin` 放进 PATH，webServer 用 `python3`）。

## 6. 与设计的差异 / 留意

- legacy `email_verify_invite_activation` 模式仍是“新请求 = 新 token 并作废旧 token”（冷却期不作废）；token 复用重发只用于 public 模式。旧 `/api/registration/resend` 保留给该模式并加了 Turnstile。
- 跨入口重发通过在另一域名重新提交 `/register` 实现（会话 cookie 按域名隔离），额度与复用规则相同。
- 英文入口在英文协议文稿发布前回退中文已发布文稿（版本/hash 仍严格校验）。
- 主工作区 `slide-id-refactor` 上未提交的注册文案改动（站点名、去掉名额提示、研究协议不重复询问）未并入本分支；合并时 `registration_mail_worker.py` 与相关模板会冲突，需人工取舍。

## 7. 发布交接复核

- 复核线上 `suite-20261008-domains` 的代码 revision 与分支基线只差文档，应用代码一致；生产 Turnstile 配置和已有 secret 均未发现。
- 用户确认控制台 widget 为托管模式，允许 `histopilot.cn` 和 `histopilot.com`；服务端生产白名单仅取这两个域名。
- 修复全站发送额度恢复时间查询：原来 jobs 与 redeliveries 做交叉连接，重发表为空时最早投递时间变为 NULL，会额外延迟恢复。改为合并投递时间后取最早值，新增真实 PostgreSQL 回归测试。
- 独立复跑注册、防刷、认证、CSP、Containerfile 与迁移相关 7 个测试文件：**257 passed**；完整前端 JS：**949 passed / 2 skipped**。
- 新增 `deploy/registration-antibot/` 发布工具：公开变量白名单、0600 secret 文件检查、交互式隐藏输入、基线和候选镜像身份检查、隔离数据库/挂载验收、备份、切换与回滚；离线部署保护检查 **10 passed**。
- homepc 发布目录为 `/home/solarise/releases/suite-20261008-registration`（0700），secret 文件 `turnstile.secret.env`（0600）。用户授权写 env 并排除 Git；已检查本机和 homepc 的 env/Cloudflare 配置，没有实际 secret 或 Cloudflare API 凭据，不能从公开 sitekey 推导 secret。
- `accept-check` 不对生产库执行迁移；生产快照恢复到独立 PostgreSQL 容器，所有可写生产挂载替换为隔离目录，所有后台 worker 关闭。缺密钥的验收只能证明缺配置时的行为，不能作为上线就绪证明。
- 实际 token 重放测试必须使用新的 submission ID；同 submission ID 重试应幂等回放。微信/邮箱内置浏览器须真实设备验证，尚不能据测试密钥浏览器结果宣称完成。
- 当前仍未推送、未切换生产；生产密钥保存并重跑隔离验收后，再进行正式部署批准。

## 8. 生产部署、dogfood 与界面修复

以下为 2026-10-08 后续执行结果，第 4、7 节的未完成状态为此前交接时记录。

### 发布

- 用户明确批准生产部署及 dogfood。生产 secret 已保存在 homepc 的发布目录，文件权限 0600；仅发布 env 文件读取，不进入 Git、前端或日志。
- 首次部署：`suite-20261008-registration`，代码 `034005c0`，镜像 `645d4ab2e7707b326727b2ad9c09536cabc65e393fc029ef1ad147e0294d6755`。
- 用户反馈界面问题后的修复：`suite-20261008-registration-ui`，代码 **`6ce1028120b8331e02295e16c8ef5929b87e47df`**。发布目录 `/home/solarise/releases/suite-20261008-registration-ui`。
- 两次发布均复核运行基线、隔离数据库验收、关闭验收 worker、替换生产可写挂载、备份与恢复目录清单、切换前后的在途任务；首次发布在生产应用 0079，界面修复没有新增迁移。
- 依赖清单与旧镜像完全一致，复用已验证的生产依赖构建。全部 344 个应用文件与源码一致，插件未变；首次部署两域名各 95 个静态文件核对通过。界面修复再核对两域名实际使用的新版本 CSS/i18n 资源哈希。
- 当前健康检查 PostgreSQL 正常、sidecar reachable；`REGISTRATION_TURNSTILE_REQUIRED=1`，服务器 hostname 白名单仅 `.cn`、`.com`，生产不允许测试密钥。原容器、切换前数据库备份均保留。

### Dogfood 实测

- 在真实 Chromium / Google Chrome 上检查 `.cn`、`.com` 的登录、注册、挑战加载、求助页、复制邮箱、无效验证链接与移动端布局。原检查中 25 项通过；两处未自动获取挑战 token 属于 Managed 模式要求真人勾选，不能称为产品故障或挑战通过。
- 用户亲自完成两入口的挑战并提交测试申请。两封验证邮件均进入其已连接 Gmail 的 **INBOX**，SPF / DKIM / DMARC 均为 pass；中文邮件站点名/链接为 `.cn`，英文邮件站点名/链接为 `.com`。
- 从实际收到的链接打开密码设置页，提交后两账号均完成注册，`activation_state=active`，来源和投递入口正确。测试脚本尝试读取提交响应时遇到页面跳转竞态，因此首次完成脚本报错；数据库复核确认建号成功。后续仅为这两个测试账号通过既有重置原语设置一次临时测试密码，再实测登录工作台及退出，4 项通过。该后续登录测试不是原密码的连续端到端证明。
- 生产模拟挑战脚本不可达、无 token、伪造 token：12 项检查通过；两个反向测试地址的邮件任务与 submission 均为零，不占发送额度。
- 旧 `.fun` 的 308 保留验证路径及 token 参数。重放已受理的测试 submission 恢复状态页，没有额外发送邮件；两个正向测试地址总计 2 个原始投递、0 个 redelivery。
- 没有保留用户的真实一次性 Turnstile token，故“真实已消费挑战 token + 新 submission ID”的重放拒绝尚未在生产实测；原单测覆盖该行为。微信/邮箱内置浏览器仍须真实设备验收。
- 两个测试账号已停用并撤销会话；临时测试密码、receipt/submission 文件、已消费的验证链接文件已删除。注册成功记录和日桶保留，未退款/重写成功计数。

### 用户反馈修复

1. 发送成功页三个底部链接在固定宽度弹窗内横向挤压：将 `.login-dialog-links` 改为单列 grid，独立排列、保持可点击区域，避免英文被拆成狭窄列。
2. 英文入口协议名称为中文：为注册弹窗和验证页的协议名称、接受说明、可选说明与协议版本提示增加中英文键；保留原协议链接、版本/hash 与勾选语义。该修改只涉及界面标签，未发布或改写法律文稿正文。
3. CSS/i18n 资源版本更新为 `20261008c`，避免旧资源缓存。

修复验证：后端相关 189 passed；前端相关 28 passed；隔离模板的真实浏览器 14 项通过（320 / 375 / 720 / 800 / 1440 像素、语言切换保持链接与勾选、验证页协议名）。公网最终复测 15 项通过，两域名的发送后状态在 320 / 375 / 1440 像素均无拥挤或溢出。首次公网测量的一项失败发生在新样式加载前，等待实际 grid 样式生效后复测通过，截图也逐张检查。

### 其他发现与证据位置

- `.com` 存在 Cloudflare 自动注入的 Web Analytics beacon 被原有 CSP 拦截的控制台提示。基线的 `script-src 'self'` 已不允许该脚本；实际 Turnstile iframe 能显示，主站 JS 与求助功能正常。没有为消除提示扩大本次 CSP 白名单或关闭防刷。可在 Cloudflare 的 [Web Analytics 配置](https://developers.cloudflare.com/web-analytics/get-started/) 中管理自动注入；该脚本的 [CSP 要求](https://developers.cloudflare.com/web-analytics/faq/) 与 Turnstile 不同。
- 用户报告代理出口被 Cloudflare 拦截；未提供具体响应码或规则记录，不能认定是哪条规则导致，也没有调整边缘防护规则。
- 浏览器脚本、结果与截图：本机 `/home/solarise/pytest-tmp/regab-production-20261008/`。`live-*-submitted-*.png`、`live-*-agreements.png` 为最终公网版本；临时敏感文件不在其中。
- 此次未推送 Git 分支；生产应用代码 commit 为 `6ce10281`，后续证据文档提交不改变已部署镜像。
