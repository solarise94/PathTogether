# PathTogether 管理工作台与 CI 一次性修复实施文档

> 状态：待实施（本文是执行规格，不代表代码、CI Secret、部署或线上验证已经完成）  
> 日期：2026-08-29  
> 主要执行者：coding agent  
> 涉及仓库：`PathTogether`、`HistoPilot`  
> 关联方案：`docs/admin-billing-plugin-implementation-plan.md`、`docs/admin-release-runbook.md`  
> 发布原则：可以分提交审阅，但不得分批声称上线；全部自动化与真实浏览器门槛通过后一次发布

## 1. 目的与结论

本轮要一次性关闭两组问题：

1. 生产 `/admin` 只显示未初始化的静态 HTML，用户、来源、额度等页面不能操作，
   CSS 未生效，导航点击无反应；
2. GitHub Actions 长期红灯，同时现有绿色 JS 测试使用假 DOM，未覆盖真实
   sandbox iframe、CSP、HTML 解析、HTTPS 反代和 `postMessage` 握手。

这不是“补一条 CSS”或“把一个失败测试 skip 掉”即可结束的工作。目标是建立一个
长期可维护的管理工作台装配契约，并让 CI 能在发布前真实阻断同类回归。

完成后必须同时达到：

- Admin 插件继续运行在 opaque sandbox iframe 中，安全边界不放宽；
- 外部 origin、宿主 bootstrap、桥接生命周期均只有一个权威实现；
- 用户、来源、账单、插件和审计页面具备 loading、empty、error、ready 四类状态；
- PathTogether JSON/PG/JS/浏览器测试全绿；
- HistoPilot 跨仓契约确实运行并全绿，而不是在 checkout 阶段失败；
- homePC 公网 HTTPS `/admin` 用真实 owner 会话完成端到端冒烟；
- GitHub Actions 链接、发布 commit、插件 release、浏览器证据均留档。

## 2. 已核实基线

### 2.1 生产现象

真实 Chrome 访问 `https://pt.solarise94.fun/admin` 可稳定复现：

- iframe 状态停在“等待宿主初始化消息…”；
- `.adm-page` 全部隐藏；
- 导航呈浏览器原生按钮样式，点击无反应；
- “重新加载插件”不能恢复；
- iframe DOM 中 `data-admin-permissions` 实际值只有 `[`。

部署文件与本地 `fe94de0` 对应文件 hash 一致，因此不是“服务器仍是旧 bundle”。

生产 PostgreSQL 当时的只读核查结果：

| 数据 | 数量 | 结论 |
|---|---:|---|
| 用户 | 9 | 用户页空白不是无数据 |
| AI usage events | 42 | 概览/计量已有可展示数据 |
| billing accounts | 1 | 账户页已有数据 |
| billing ledger entries | 1 | 账本页已有数据 |
| acquisition visits | 0 | 来源页应展示明确空状态 |
| user acquisition | 0 | 历史用户没有来源回填，不得猜测来源 |

### 2.2 GitHub Actions

PathTogether 最新失败 run：
`https://github.com/solarise94/PathTogether/actions/runs/33232469577`。

- JSON Python：842 passed；
- PostgreSQL：1116 passed、1 failed；
- HostBridge/JS：133 passed；
- PostgreSQL 连续六次失败于
  `tests/test_user_store.py::test_lazy_migration_owner_refs`。

该用例直接写 `shares.json` 并断言 JSON 懒迁移落盘，在
`STORAGE_BACKEND=postgres` 下不成立。本地已复现“默认 JSON 通过、PG 失败”。

HistoPilot 最新跨仓失败 run：
`https://github.com/solarise94/HistoPilot/actions/runs/33192226473`。

- HistoPilot 自身 Tests 通过；
- `Cross-repository config contract` 在 checkout PathTogether 时得到 `Not Found`；
- 两个仓库当前都是 private，默认 `${{ github.token }}` 仅能访问当前仓库；
- 因此跨仓契约测试从未开始执行。

### 2.3 当前测试盲区

`tests/js/admin-plugin-ui.test.ts` 明确采用 `new Function + 假 window/document`，
只能验证函数装配，不能证明浏览器可用。`tests/test_admin_plugin.py` 当前只检查原始
HTML 包含某个权限字符串，也只断言 test client 下的 `http://localhost` CSP，不能
发现以下问题：

- JSON 被 HTML 双引号截断；
- opaque iframe 下 CSS/JS 被 CSP 拦截；
- 公网 HTTPS、内部 HTTP 时 origin 推导错误；
- iframe load 发生在监听器注册前；
- 浏览器未建立桥接但单元测试仍绿。

## 3. 不变量与非目标

### 3.1 必须保持的不变量

1. iframe 保持 `sandbox="allow-scripts"`，不得增加 `allow-same-origin`；
2. CSP 不得使用 `*`，不得用 `unsafe-inline` 或 `unsafe-eval` 消除报错；
3. 插件 iframe 不接触 Flask session、CSRF token、数据库连接或 service token；
4. HostBridge 继续同时校验确切 `WindowProxy`、nonce、requestId、method schema 和权限；
5. `/admin` 仍只允许真实 owner actor；preview subject 不获得管理权限；
6. nano-CNY 金额在线路和浏览器中继续使用十进制字符串，不退回 JS `Number`；
7. Demo 不开户、不扣账；PR6 模拟扣费和 PR7 advisory hold 语义不因本修复改变；
8. admin 仍是 PathTogether 插件页面，不迁回 Viewer 侧栏，也不放进 mywebpage；
9. 插件缺失、禁用或握手失败不得破坏 Viewer、登录、分享和 AI 主流程；
10. 不根据 IP、邮箱、用户名或历史登录记录推断来源。

### 3.2 非目标

- 不新建独立 admin 服务或新数据库；
- 不引入 React/Vue 等前端框架；现有原生 HTML/CSS/JS 足以完成本轮；
- 不补做历史用户来源归因；如以后需要，必须单独定义数据来源和合法回填规则；
- 不开启真实余额拒绝、支付、退款、发票或税务能力；
- 不顺带重构无关 Viewer、HistoPilot agent 或计费核心。

## 4. 目标装配架构

```text
PUBLIC_BASE_URL（严格解析后的公网 origin）
                    │
                    ├── 生成 admin plugin HTML CSP
                    │
owner GET /admin ───┴── bootstrap JSON v1
                         │ permissions / asset URL / protocol version
                         ▼
                 Admin Host 生命周期状态机
          idle → loading → waiting → ready / error
                         │
              opaque sandbox iframe load
                         │
       WindowProxy + per-load nonce + requestId + schema
                         │
                         ▼
                  AdminBridge → Admin API v1
```

这里有三个单一事实来源：

- 公网资源 origin：严格解析后的 `PUBLIC_BASE_URL`；
- 页面启动配置：版本化 bootstrap JSON；
- iframe 状态：宿主生命周期状态机。

不得继续从 `request.host_url`、HTML data 属性和零散布尔变量分别推断上述状态。

## 5. 实施包 A：先建立失败复现与兼容边界

在修改产品代码前新增或调整回归测试，使以下问题在旧代码上确定失败：

1. 请求从内部 HTTP 到达，但 `PUBLIC_BASE_URL=https://pt.example` 时，插件 HTML
   CSP 必须只允许 `https://pt.example`；
2. 浏览器解析 `/admin` 后，bootstrap 权限数组必须是完整合法 JSON；
3. iframe 首次加载快于宿主脚本执行时，仍能完成初始化；
4. 5 秒内没有握手时，宿主必须进入可见 error 状态，而不是永久等待；
5. 真实 Chromium 中 CSS 生效、导航切换、用户列表加载；
6. acquisition 为零时显示来源空状态，而不是空白区域。

测试先红是根因复现证据；实现后同一组测试转绿。不得只在修复后补一个无法证明旧
代码会失败的测试。

兼容边界：本轮宿主与插件可以分提交，但新宿主在发布窗口内至少兼容当前插件协议；
不得要求两个不可原子部署的 artifact 必须同时到达才能显示可诊断错误。

## 6. 实施包 B：统一公网 origin 与 CSP

### 6.1 代码改造

将 `app.py::_admin_asset_html_csp()` 从 `request.host_url` 解耦，抽出一个平台级
“规范公网 origin”helper，要求：

- 优先且在生产唯一使用 `PUBLIC_BASE_URL`；
- 用 URL parser 校验，只允许 `http`/`https`；生产必须为 `https`；
- 拒绝 userinfo、query、fragment 和非根 path；
- 规范化尾斜杠和默认端口，返回纯 `scheme://host[:port]`；
- 测试/明确的本地开发模式可以回退到 request origin；生产配置缺失或非法时 fail
  closed，并给出不包含敏感信息的可操作错误；
- readiness 检查与 CSP 必须复用同一个 parser，不能各写一套字符串判断。

不要为了本问题全局信任任意 `X-Forwarded-*`。若 agent 认为还需引入 `ProxyFix`，
必须先证明 18080 只接受可信回环代理，并为直连伪造 header 补安全测试；否则本轮
以显式 `PUBLIC_BASE_URL` 作为 CSP 安全边界。

### 6.2 必测条件

- `PUBLIC_BASE_URL=https://pt.example` + 内部 request `http://localhost`：CSP 的
  script/style/img origin 均为 `https://pt.example`；
- URL 带凭据、query、fragment、非根 path 或非法 scheme：确定性拒绝；
- 本地 test client 未设配置：仍可生成 `http://localhost`，但该回退不能在生产启用；
- CSP 保持 `default-src 'none'` 和 `frame-ancestors 'self'`；
- 响应里没有 `*`、`unsafe-inline`、`unsafe-eval`、`allow-same-origin`。

## 7. 实施包 C：用版本化 bootstrap JSON 取代 JSON data 属性

### 7.1 模板契约

删除 `data-admin-permissions="{{ ... | tojson }}"`。改用不可执行的 JSON bootstrap
节点，例如：

```html
<script id="admin-bootstrap" type="application/json">
  {"schemaVersion":1,"protocolVersion":"...","permissions":[],"assetUrl":"..."}
</script>
```

实际模板必须继续使用 Jinja `tojson`，禁止字符串拼 JSON。bootstrap 只放非敏感、
启动必需字段；不得放 CSRF、session、安装密钥、用户数据或 API token。

Host 侧新增严格解析器：

- 校验顶层对象和 `schemaVersion`；
- permissions 必须是去重后的已知字符串数组；
- asset URL 必须是站内允许路径；
- 未知 schema version、缺字段、重复字段语义冲突或解析失败均进入可见错误状态；
- 不得静默回退为空权限数组。

### 7.2 必测条件

- 用真实 HTML parser 读取 bootstrap 节点，`JSON.parse` 后权限集合与服务端授权集合
  完全一致；
- 权限值含引号、尖括号等边界字符时仍不会逃逸成新标签或可执行脚本；
- bootstrap 损坏时页面显示“管理工作台配置无效”，桥接请求数为 0；
- 原始 HTML 不再包含 `data-admin-permissions`；
- user、anonymous、preview actor 不能借修改 DOM 扩大服务端权限。

## 8. 实施包 D：收敛 iframe/桥接生命周期

### 8.1 宿主状态机

用单一状态机替代“监听 load + 永久等待文本”的零散逻辑：

```text
idle → loading → waiting_handshake → ready
                           └───────→ error
ready/error → reload → loading（新 nonce，旧请求全部作废）
logout → disposed（nonce 和 pending request 全部作废）
```

实施要求：

1. 模板中的 iframe 不预先设置业务 `src`；Host 先安装 `load`/`message` 监听器，再赋
   `src`，消除初次 load race；
2. 每次 load/reload 生成新的 256-bit nonce，旧 nonce 和 pending request 立即失效；
3. 等待握手超过 5 秒进入 error，显示错误类别和“重新加载插件”操作；
4. reload 不刷新整个 Viewer，不复用旧 nonce；
5. 状态变化使用 DOM 属性和可测试事件表达，不只改一段文本；
6. 记录不含敏感内容的诊断码，例如 `bootstrap_invalid`、`asset_load_failed`、
   `handshake_timeout`、`permission_denied`；
7. 不在生产 console 输出 nonce、完整消息信封或用户数据。

### 8.2 插件侧要求

- message listener 必须在发送/等待任何请求前注册；
- 继续验证 `event.source === window.parent`、nonce、requestId 和响应 schema；
- 未 ready 时导航可以切换本地骨架，但不得发管理 API 请求；
- bridge 错误必须落到对应页面错误态，不能只 `console.error`；
- 快速连续点击、reload 和晚到响应不得把旧数据写回新页面。

### 8.3 必测条件

- load 在监听器注册前后的两种时序都能 ready；
- reload 后旧 nonce 的响应被丢弃；
- 非 parent window、错误 nonce、未知 requestId、未知 method 全部拒绝；
- handshake timeout 可见且可恢复；
- 权限不足显示明确状态，不能伪装成“暂无数据”；
- logout 后任何晚到响应均不更新 DOM。

## 9. 实施包 E：管理工作台 UI 与数据状态

### 9.1 结构

保留原生 HTML/CSS/JS，建立小型设计 token 和组件层，不引入框架：

```text
桌面：240px 左侧导航 + 流式主内容（最大 1280–1440px）
平板：可折叠侧栏 + 主内容
手机：顶部导航抽屉 + 单列卡片/列表
```

- 删除宿主与 iframe 内重复的大标题，只保留一个页面身份；
- 导航提供图标/文本、当前项、键盘 focus、`aria-current` 或 `aria-selected`；
- 概览使用 KPI 卡片展示用户、活跃、用量、缓存命中、模拟余额、unpriced 事件；
- 用户表默认只放高频列，余额、来源、状态和操作进入详情抽屉；
- 大表使用 sticky header、合理最小宽度和明确横向滚动提示；
- 危险写操作与普通操作视觉区分，继续保留确认和服务端 CAS；
- 不把页面宽度锁死在 860px。

### 9.2 统一状态组件

每个页面都必须具备：

- `loading`：骨架屏或进度提示；
- `empty`：解释为什么为空以及下一步；
- `error`：错误类别、request id（非敏感）和重试操作；
- `ready`：数据、更新时间、刷新操作。

来源为零时固定使用不误导的文案：

> 暂无来源归因数据。来源统计仅覆盖功能上线后的有效访问触点，历史用户尚未回填。

不得把 `permission_denied`、网络失败或 handshake 失败渲染成这个 empty 状态。

### 9.3 可访问性与响应式验收

- 仅键盘可切换所有导航、打开/关闭详情抽屉、触发非危险操作；
- focus 可见，不依赖颜色表达唯一状态；
- 390px、768px、1440px 三种视口无内容覆盖或不可达操作；
- 200% 缩放后不丢按钮和表单标签；
- loading/status 使用适当的 `aria-live`，但不重复播报每条表格数据。

## 10. 实施包 F：真实浏览器测试成为 CI 门禁

### 10.1 测试技术

在 PathTogether 增加 Playwright Chromium 测试和独立 npm script，例如
`test:e2e:admin`。不要用 jsdom/fake DOM 代替此层；原 Vitest 单元测试继续保留，
两者职责不同。

CI 测试应用应使用临时目录和临时 PostgreSQL，安装并 pin 仓库内 admin bundle，
创建一次性 owner 和普通用户。凭据仅存在 runner 进程环境，不写日志和 artifact。

浏览器 CI 可以使用本地 HTTP origin；“公网 HTTPS、内部 HTTP”的 scheme 分离由
Python CSP 回归测试覆盖。部署后的公网 Chrome 冒烟负责验证真实 TLS 代理链。

### 10.2 Chromium 必测流程

1. anonymous 访问 `/admin` 被重定向登录；
2. 普通 user 登录后访问 `/admin` 得到拒绝；
3. owner 登录进入 `/admin`；
4. 宿主在 5 秒内进入 `ready`，显示已授予权限数；
5. iframe computed style 证明 CSS 已应用，导航不是默认 block/native button 布局；
6. 点击“用户”，出现预置普通用户且关键字段正确；
7. 点击“邀请与来源”，零数据时出现规定 empty 文案；
8. 点击“额度与账单”“插件”“审计”均能切换并完成至少一次真实 bridge 请求；
9. reload 插件后重新 ready，旧请求/旧 nonce 不生效；
10. 页面没有未允许的 console error、pageerror、CSP violation 或失败资源请求；
11. 截取桌面和手机两张截图作为 CI artifact，仅含测试虚构数据。

E2E 必须断言业务结果和 DOM 状态，禁止仅以“页面能打开”作为通过标准。

## 11. 实施包 G：修复 PathTogether CI 语义

### 11.1 后端专属测试

为 `test_lazy_migration_owner_refs` 使用已有统一 `json_only` 标记。随后审计所有直接
读写 `SHARE_FILE`、`USER_FILE` 或旧 JSON 结构的测试，确保：

- JSON-only 用例在 PG job 明确 skip，并有可读 reason；
- PG-only 用例在 JSON job 明确 skip；
- 同时适用于两种后端的契约测试不得被误标记；
- 不使用裸 `return` 或吞异常制造假绿。

不要删除该懒迁移测试，也不要把整个 `test_user_store.py` 从 PG job 排除。

### 11.2 PG canary 顺序

把 PG canary 放在 PG 全量测试之前，或拆成独立必需 job。这样全量测试失败时仍能
证明 runner 真正启动了 PostgreSQL，避免当前“全量失败导致 canary skipped”。

### 11.3 Actions 运行时债务

当前日志提示旧 action 所用 Node 运行时已弃用。agent 实施时应重新查询官方当前稳定
major；本次审阅时 `actions/checkout`、`setup-node`、`setup-python` 当前稳定 major
均为 v7。升级后必须重新验证 cache、checkout 和 Python/Node 版本，不得只改版本号。

可选但推荐：使用 Dependabot 的 `github-actions` 更新检查，避免再次长期滞后。若采用
完整 commit SHA pin，则必须同时配置自动更新机制，不能留下手工失联的 SHA。

### 11.4 PathTogether CI 必需 job

- `python-json`；
- `python-pg-canary`；
- `python-pg`；
- `host-bridge-unit`；
- `admin-browser-e2e`。

任何 job 失败都不得发布。不得用 `continue-on-error`、整 job 条件跳过或降低断言来换绿。

## 12. 实施包 H：修复 HistoPilot 私有跨仓契约

### 12.1 最小权限凭据

为 HistoPilot Actions 配置一个 fine-grained PAT 或 GitHub App token，仅允许读取
`solarise94/PathTogether` 的 Contents。推荐 secret 名：
`PATHTOGETHER_READ_TOKEN`。

workflow 中：

- checkout HistoPilot 继续用当前仓库 token；
- checkout PathTogether 显式使用该 secret，并显式 `ref: main`；
- checkout 前增加不泄露值的 secret presence preflight；缺失时输出明确错误，不能继续
  让 checkout 显示含混的 404；
- `persist-credentials: false`；
- 不给 token `contents:write`、Actions 管理、issues、PR 或其他无关权限；
- 不把 token、HTTP extraheader 或带凭据 remote 写入 artifact/log。

凭据创建和写入 GitHub Secret 是外部权限操作。执行 agent 只有得到 owner 明确授权并
拥有安全输入渠道时才能执行；否则必须停在“workflow 已就绪、Secret 待 owner 配置”，
不能伪造绿色结果。

### 12.2 契约执行与证据

checkout 成功后必须实际运行 `npm run test:contract`。job summary 写入：

- HistoPilot commit SHA；
- PathTogether commit SHA；
- contract test 总数和结果；
- 不包含任何 secret 或本地绝对凭据路径。

当前 workflow 只在 HistoPilot 变化时触发。为减少跨仓漂移，至少增加每日 schedule 和
手工 `workflow_dispatch`；若要让 PathTogether 每次 main push 都即时触发 HistoPilot，
应另行采用 GitHub App/repository dispatch，并在赋予 Actions write 权限前做独立安全
审阅。本轮不能用一个超大 PAT 顺手解决事件触发。

## 13. 文件影响清单

agent 必须先以实际代码为准复核路径；预计变更面如下：

### PathTogether

- `app.py`：规范公网 origin、CSP、必要的 bootstrap 服务端装配；
- `templates/admin_host.html`：bootstrap JSON、iframe 延迟设 src、宿主状态容器；
- `static/admin-host.js`：bootstrap parser、生命周期状态机、握手 timeout/reload；
- `plugins/pathtogether-admin/ui/index.html`：语义结构、状态区、页面布局；
- `plugins/pathtogether-admin/ui/style.css`：设计 token、侧栏、响应式、状态组件；
- `plugins/pathtogether-admin/ui/main.js`：页面状态、错误分类、数据渲染与抽屉；
- `tests/test_admin_plugin.py`：CSP、bootstrap、权限和安全回归；
- `tests/js/admin-bridge.test.ts`：状态机、race、nonce 生命周期；
- `tests/js/admin-plugin-ui.test.ts`：纯逻辑/状态组件单元测试；
- `tests/e2e/`：真实 Chromium 管理工作台流程；
- `tests/test_user_store.py`：JSON-only 标记；
- `.github/workflows/tests.yml`：PG canary、E2E 和 action runtime；
- `package.json`、lockfile：Playwright 与测试 script；
- 可选 `.github/dependabot.yml`：Actions 依赖更新。

### HistoPilot

- `.github/workflows/cross-repo-contract.yml`：私库 token、preflight、明确 ref、
  credential cleanup、schedule/summary；
- 如契约因真实代码漂移失败，只修改契约或产品的权威所属仓库，不复制实现规避失败。

## 14. 实施顺序与提交策略

建议按以下顺序提交，便于 review 和回退；但仅在全部完成后发布：

1. `test(admin): reproduce bootstrap/csp/iframe failures`；
2. `fix(admin-host): canonical origin, bootstrap v1 and lifecycle state machine`；
3. `fix(admin-ui): responsive workbench and explicit data states`；
4. `test(admin): real Chromium e2e and CI gate`；
5. `fix(ci): backend markers, PG canary and current action runtimes`；
6. HistoPilot：`fix(ci): authenticated private cross-repo contract checkout`。

禁止把大量产品修复、测试降级和部署记录揉成一个无法审阅的 commit。禁止在中间提交
部署后宣称“修好了”。

## 15. 自动化验收矩阵

### 15.1 PathTogether 聚焦测试

```bash
python -m pytest \
  tests/test_admin_plugin.py \
  tests/test_user_store.py::test_lazy_migration_owner_refs -q

RUN_PG_TESTS=1 python -m pytest \
  tests/test_pg_backend_canary.py \
  tests/test_admin_plugin.py \
  tests/test_user_store.py::test_lazy_migration_owner_refs -q -rA

npm run test:js
npm run test:e2e:admin
```

验收：零失败；JSON-only 用例在 PG 命令中必须显示一条带原因的预期 skip；canary 必须
passed，不能 skip。

### 15.2 PathTogether 全量

```bash
python -m pytest tests -q --cov=. --cov-report=term --cov-report=xml \
  --cov-fail-under=44

RUN_PG_TESTS=1 python -m pytest tests -q --cov=. --cov-report=term \
  --cov-report=xml --cov-fail-under=44
```

验收：两条命令退出码均为 0；覆盖率不低于现有门槛；新增 skip 数必须逐条说明，不能
用批量 skip 隐藏失败。

### 15.3 HistoPilot

```bash
npm ci
npm run build
npm run test:unit
npm run test:integration
PATHTOGETHER_REPO=../PathTogether npm run test:contract
```

验收：全部退出码 0。GitHub 跨仓 job 还必须显示两个实际 checkout SHA，不能用本地绿
替代 GitHub 私库鉴权验证。

### 15.4 GitHub Actions

两个仓库目标分支 tip 必须全部绿色：

- PathTogether 五个必需 job 均 success；
- HistoPilot Tests success；
- HistoPilot Cross-repository config contract success，且日志证明 contract step 已运行；
- 不允许 cancelled、neutral、skipped 或 continue-on-error 冒充 success；
- 保存 run URL，写入实施结果或部署记录。

## 16. homePC 发布与真实浏览器验收

### 16.1 发布前

1. 两仓 worktree 只含本任务预期变更，保留无关未跟踪文件；
2. 本地聚焦、全量、E2E 和 GitHub Actions 全绿；
3. 按 `admin-release-runbook.md` 确认备份新鲜、插件挂载只读、release 可回滚；
4. 记录当前 PathTogether 镜像/commit、admin 插件 symlink target 和 hash；
5. 不含数据库 migration 时也不得跳过数据库健康与备份检查。

### 16.2 发布顺序

1. stage 新 admin bundle，完成 hash/pin preflight，不切 live；
2. 部署向后兼容的 PathTogether host 代码；
3. 宿主健康、登录、Viewer、旧插件兼容检查通过；
4. 原子切换 admin bundle symlink；
5. 重启或 reload 必要进程；
6. 立即执行公网 HTTPS Chrome 冒烟。

### 16.3 公网 Chrome 硬验收

使用真实 owner 会话，至少验证：

- `/admin` 进入后 5 秒内 ready；
- DevTools Network 中 `index.html`、`style.css`、`main.js` 均 200；
- 插件 HTML CSP 的资源 origin 是 `https://pt.solarise94.fun`；
- Console 无 CSP、脚本、未处理 Promise、bridge schema 错误；
- 权限数量大于 0，且与 owner 授权集合一致；
- 用户页展示生产现有用户，不要求暴露敏感字段；
- 来源为零时显示规定空状态；
- 额度、账单、插件、审计页均能加载；
- reload 插件后再次 ready；
- 390px 与桌面宽度均可操作；
- Viewer、登录、AI、分享冒烟不回归。

浏览器验收需要保存脱敏截图、关键 network/status 结果和时间。不得只用 curl 200 或
页面截图代替交互与 Console/Network 检查。

## 17. 回滚

触发以下任一条件立即回滚：

- iframe 不能 ready 或出现 CSP/bridge 安全错误；
- 用户、账单或审计数据出现错误主体/错误金额；
- Viewer、登录、AI、分享受影响；
- 新 bundle 无法通过 hash/pin；
- 生产 console/network 出现持续错误；
- 发布后必需 Actions 变红。

回滚顺序：

1. admin bundle symlink 原子切回上一 release；
2. PathTogether 切回上一已知镜像/commit；
3. 恢复前一组 pin/hash；
4. 重跑 Viewer 与 `/admin` 冒烟；
5. 保留失败 release、日志和脱敏证据供分析，不原地覆盖。

本轮预期不改数据库 schema，原则上不需要数据库回滚；如 agent 实施中发现必须迁移，
应停止并修订本文迁移、备份、forward-fix 和回滚章节后再继续，不能临时加 migration。

## 18. Agent 执行纪律

1. 开始前读取两仓状态、相关文档和当前 workflow，不覆盖用户无关改动；
2. 先提交可复现失败的测试，再改实现；
3. 每个修复都要说明旧代码为什么失败、新测试为什么能捕获；
4. 不通过放宽 CSP、扩大权限、增加 iframe sandbox 权限或跳过测试换取绿色；
5. 不打印、提交或写入 artifact 的 Secret、cookie、nonce、token、完整 IP、prompt；
6. 缺少 GitHub Secret、homePC 权限或 owner 浏览器会话时明确停在外部前置，不伪造验收；
7. 对 Action 版本、线上 commit、插件 target 等易漂移事实，在执行当日重新查询；
8. 本地通过、GitHub 通过、已部署、线上浏览器通过必须分开报告；
9. 任一必需测试失败都不允许使用“存量、无关”作为收口理由；要么修正测试适用范围，
   要么定位产品缺陷，最终目标分支必须全绿；
10. 完成时列出 commit、测试结果、Actions run URL、部署 release/hash、浏览器证据和
    所有仍未完成事项；只要其中一项缺失，状态仍是“实现中”。

## 19. 完成定义

只有以下所有复选项均满足，才能标记“管理工作台与 CI 修复完成”：

- [ ] 公网 origin/CSP 使用单一严格 parser，HTTPS 反代回归测试通过；
- [ ] bootstrap JSON v1 替代 JSON data 属性，浏览器解析权限完整；
- [ ] iframe load race、nonce 生命周期和 5 秒 timeout 均有测试；
- [ ] UI 有响应式布局及 loading/empty/error/ready 状态；
- [ ] 用户页真实显示数据，来源零数据展示准确空状态；
- [ ] JSON-only 懒迁移测试不再污染 PG job，JSON 与 PG 全量均绿；
- [ ] PG canary 确实执行；
- [ ] Playwright Chromium E2E 在 CI 通过；
- [ ] HistoPilot 能 checkout 私有 PathTogether 并实际执行 contract；
- [ ] PathTogether/HistoPilot 目标分支 GitHub Actions 全绿；
- [ ] homePC 使用版本化 release 发布并保留可用回滚目标；
- [ ] 公网 HTTPS Chrome 的用户、来源、账单、插件、审计、reload 冒烟通过；
- [ ] Viewer、登录、AI、分享无回归；
- [ ] 实施证据写入部署记录，且没有 secret 或敏感数据泄漏。

在全部满足前，不得使用“实现好了”“CI 已闭合”“生产可用”作为最终结论。
