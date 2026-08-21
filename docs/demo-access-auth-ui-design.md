# Demo 入口、注册登录与权限 UI 设计

状态：安全审查修订稿

日期：2026-08-17

适用版本：PathTogether / HistoPilot 拆仓后的下一阶段

## 1. 结论

第一阶段采用“两套主入口 + 保留分享访客”的产品结构：

- `/demo`：任何人可直接进入，只能查看明确加入 Demo 目录的切片，并可在受控额度内运行一次只读 AI；
- `/`：已登录时继续承载现有完整 PathTogether，注册用户可使用平台 AI、自定义 AI、上传和分享自己的切片；
- `/s/<token>`：保留现有分享访客入口，权限继续由分享 token 的 `view / annotate / download` 决定。

第一阶段不开放自由注册。owner 继续手工创建或邀请测试账号；`/register` 提供明确的“当前未开放注册”状态。待 HTTPS、邮箱验证、平台 AI 配额和反滥用能力完整后，owner 才可打开自助注册。

Demo 不是一种新的持久用户角色。它是一份短期、路由限定的 capability grant，不能进入普通 `/api/*` 的 owner/user 权限路径，也不能复用“无 role 即 owner”的兼容语义。

当前测试期的预算默认值已确定：Demo 每次最多执行 10 个 Agent 任务步骤；注册用户使用平台 AI 时每次最多 20 步；使用自带 API 时可自行设置步数。每个注册用户可触发 10 次平台 AI 对话；所有使用平台凭据的 Demo、user 和 owner 共可触发 30 次平台 AI 对话，其中 Demo 最多消耗 5 次。owner 可在后台修改额度、查看用量并开启新的预算周期。Demo 只提供简单能力演示，不追求完整工作流。

本设计进入实施前必须先完成 §11 的现状链路加固。尤其是禁用 legacy adapter、关键安全字段 fail-closed、强制 run grant、稳定写入幂等和致命处理 annotate 403；这些不是 Demo 的可选优化。

## 2. 目标与非目标

### 2.1 目标

1. 让第一次访问的人不登录即可看到真实 Viewer 和 HistoPilot 导航效果。
2. 把匿名体验的成本和写入能力限制在明确边界内。
3. 让受邀测试用户登录后零配置使用平台 AI，也可切换到自己的模型凭据。
4. 让注册、登录、Demo 和分享入口在界面上容易理解，不再把普通登录写成“管理员登录”。
5. 所有权限由后端权威校验；前端隐藏按钮只负责降低误操作，不承担安全职责。
6. 保持拆仓边界：PathTogether 管身份、切片、权限、额度和正式标注；HistoPilot 管工具集、AI session、SSE 和临时观察。

### 2.2 第一阶段非目标

- 不做 organization、多租户、实名认证或复杂角色矩阵；
- 不做匿名用户自带 API key；
- 不让 Demo AI 写正式标注、评论或分享；
- 不开放无限自助注册；
- 不建设完整邮件营销、订阅、计费或套餐系统；
- 不把 AI 输出表述为临床诊断结论。

## 3. 入口与导航

### 3.1 路由

| 路由 | 访问者 | 行为 |
|---|---|---|
| `/` | 未登录 | 展示简洁入口页，主按钮“直接体验 Demo”，次按钮“登录” |
| `/` | 已登录 | 继续渲染现有完整应用，不迁移主路由 |
| `/demo` | 任意访客 | Demo Viewer；仅 Demo 切片和受限 AI |
| `/login` | 未登录 | 普通用户与 owner 共用登录页 |
| `/login` | 已登录 | 302 到安全校验后的 `next` 或 `/` |
| `/register` | 任意访客 | 按平台注册策略展示关闭状态或注册表单 |
| `/s/<token>` | 分享访客 | 现有 token 限定分享 Viewer |

不建议让未登录 `/` 直接跳 `/login`。同一路由按认证状态分流：未登录渲染入口页，已登录渲染现有应用。这样无需引入当前不存在的 `/app`，也不改变历史 `next=/`、书签和内部链接。未来若需要 `/app`，只能先作为 `/` 的兼容别名，再单独迁移。

### 3.2 入口页内容

入口页只承担分流，不做长营销页：

- 产品名：PathTogether；
- 一句话：`协作式数字病理切片查看与 AI 导航`；
- 主按钮：`直接体验 Demo`；
- 次按钮：`登录测试与协作`；
- 辅助说明：`Demo 无需登录，可查看示例切片并体验 1 次 AI 导航`；
- 底部声明：`仅用于研究、教学和软件演示，不用于临床诊断。`

## 4. 权限矩阵

| 能力 | 匿名 Demo | 注册用户 user | owner | 分享访客 guest |
|---|---:|---:|---:|---:|
| 查看 Demo 目录切片 | 是 | 是 | 是 | 否 |
| 查看自己的切片 | 否 | 是 | 是 | 否 |
| 查看公开/受邀切片 | 否 | 是 | 是 | token 内切片 |
| 使用平台 AI | 1 个最多 20 步的临时 run | 当前预算周期 10 次 | 是，计入全站总量 | 第一阶段否 |
| 使用自己的 AI 凭据 | 否 | 是 | 是 | 第一阶段否 |
| 上传和维护图库 | 否 | 仅自己的 | 全部 | 否 |
| 创建/编辑正式标注 | 否 | 自己或获 `annotate` 授权 | 全部 | 按 token 权限 |
| Demo 临时观察叠加 | 是 | 不适用 | 不适用 | 否 |
| 创建分享 | 否 | 仅自己的切片 | 全部 | 否 |
| 查看 AI 历史 | 仅当前临时 run | 仅自己的 | 全部 | 否 |
| 管理用户、插件和平台 AI | 否 | 否 | 是 | 否 |

“注册用户可使用平台 AI”表示平台已经配置官方凭据时可用，并不表示无限额度。当前测试期不做按日/月自动恢复，使用 owner 可手工重置的预算周期：每个 user 10 次、平台总计 30 次。用户自带凭据不消耗平台模型预算，步数可由用户设置，但仍受系统硬上限、并发和安全限流保护。

### 4.1 初始预算参数

| 参数 | 当前默认值 | 说明 |
|---|---:|---|
| Demo 每浏览器额度 | 1 次 / 24 小时 | 只允许创建一个 Demo 主 run |
| Demo 单次任务步骤 | 20 步 | 与注册用户平台 AI 单次步数对齐；10 步不够完成读片演示 |
| 注册用户平台 AI 单次任务步骤 | 20 步 | 由 owner 配置，user 只读 |
| 注册用户自带 API 单次任务步骤 | 用户设置，默认 20 步 | 当前允许 1–500；owner 可降低系统硬上限 |
| 注册用户平台 AI 对话额度 | 10 次 / 预算周期 | 对每个 role=user 账户分别计数 |
| 平台 AI 总额度 | 30 次 / 预算周期 | 包含 Demo、user 和 owner 使用平台凭据的全部触发 |
| Demo 平台 AI 子额度 | 5 次 / 预算周期 | 计入总 30 次；耗尽后不影响剩余注册用户额度 |
| 自定义 AI 对话额度 | 不计平台次数 | 使用用户自己的 key，步数由用户设置，仍受并发限制 |

这里的“一次对话”不是一个可无限续聊的 session，而是一次由用户主动触发并真正启动 Agent 的执行：

- 新建主运行 `/run`：计 1 次；
- 继续 `/continue`：计 1 次；
- 追问 `/ask`：计 1 次；
- 深读分支 `/branch`：计 1 次；
- SSE 断线重连、查看历史、取消、读取 session：不计；
- 同一个幂等请求的网关重试：不重复计；
- HistoPilot 接受执行前失败：释放预占；已经接受并开始执行：计 1 次。

Demo 第一阶段只开放一次 `/run`，不开放 continue、ask 和 branch，因此最多消耗一个 Demo 名额和 10 个任务步骤。

### 4.2 owner 后台预算设置

owner 后台增加“AI 预算”卡片，至少显示和允许修改：

- 当前预算周期开始时间；
- 平台总用量，例如 `12 / 30`；
- Demo 用量，例如 `2 / 5`，并分别展示 Demo、user、owner 的构成；
- 每个注册用户的用量，例如 `user@example.com 3 / 10`；
- 平台总对话上限，默认 30；
- Demo 子额度上限，默认 5；
- 每用户对话上限，默认 10；
- 注册用户平台 AI 单次任务步骤，默认 20；
- 自带 API 可设置的步数硬上限，当前默认 500；
- Demo 是否开启、每浏览器次数和 Demo 单次任务步骤（默认 10）；
- 当前运行数和最大并发；
- `保存设置` 与 `开启新预算周期`。

保存上限不清空已有用量：把总额度从 30 调到 50 后应显示 `已用 / 50`。若把上限调低到小于已用量，现有运行不取消，但新请求立即被拒绝。`开启新预算周期` 才把当前周期用量归零；该操作需要二次确认并写操作日志，旧周期统计保留用于排查，不做物理删除。

设置校验要求 `0 <= demo_turn_limit <= platform_turn_limit`，所有次数/步数为有界整数。Demo 子额度不是预留给 Demo 的专属 5 次，而是“Demo 最多能从总 30 次中消耗 5 次”；未使用的 Demo 子额度不会阻止注册用户使用总额度。

预算判断必须在 PostgreSQL 事务中原子预占。user 使用平台凭据时同时检查“用户额度 + 平台总额度”；Demo 同时检查“每浏览器额度 + Demo 子额度 + 平台总额度”；owner 检查平台总额度。不能先扣其中一个再失败，也不能依赖单个 gunicorn worker 的内存计数。

### 4.3 PostgreSQL 前置条件

Demo capability、跨 worker 预算、登录锁定和 reservation 回收都依赖一致事务。第一阶段只在 `STORAGE_BACKEND=postgres` 时开放：

- `STORAGE_BACKEND=json` 或 `dual` 时，`/demo`、Demo AI 和受额度保护的注册用户平台 AI 必须 fail-closed，不得退化到进程内计数；
- 若配置 `PUBLIC_DEMO_ENABLED=1` 但后端不是 `postgres`，进程拒绝启动并给出明确错误；
- 本地 json 模式仍可保留 owner 单机开发能力，但不得声称具备本文的公网 Demo、多用户预算或跨 worker 防爆破保证；
- json 模式下使用自带凭据（`credential_source=own`）的 run 放行但不记可观测用量；预算表不存在时不得因记账失败拒绝 own 凭据执行；
- `dual` 的 PostgreSQL 镜像不是预算权威来源，因此不能视为满足条件；
- README、容器示例和健康检查必须显示这些功能当前是否满足 PostgreSQL 前置条件。

## 5. 匿名 Demo 设计

### 5.1 Demo 切片目录

Demo 切片必须使用独立的 `demo_enabled` 或 `demo_catalog` 数据，不直接把现有 `public=true` 解释为互联网匿名可见。

原因：当前 `public` 表示注册用户可查看的公开协作切片。若直接扩大语义，owner 一次普通的“设为公开”操作就可能把真实切片发布到互联网。

要求：

- 只有 owner 能加入或移出 Demo 目录；
- UI 明确提示“加入后无需登录即可从互联网访问”；
- 建议 Demo 只使用合成、教学或已确认脱敏的切片；
- API 始终按 Demo allowlist 校验 slide，不接受任意文件名；
- Demo 目录可配置展示名、简介、排序和默认切片。

### 5.2 Demo capability

首次打开 `/demo` 时，PathTogether 签发随机、不透明的 Demo session cookie：

- `HttpOnly`、`SameSite=Lax`，HTTPS 部署时必须 `Secure`；
- 浏览器只持有随机明文，数据库只保存 token hash；
- capability 只能调用 `/api/demo/*`；
- 默认 24 小时到期；
- 不映射为 owner/user/guest，不进入普通资源鉴权函数；
- 退出 Demo 或过期后不能继续查看 AI session。

不能只把 `/demo` 加进全局认证白名单后复用普通 API。当前兼容模式会把无 role 请求归一成 owner；Demo API 必须有独立、fail-closed 的身份解析和 allowlist。

### 5.3 “一次 AI”的准确语义

匿名访问无法可靠识别真实个人，因此 UI 不应承诺“每人永久一次”。第一阶段定义为：

> 当前浏览器 24 小时内可创建 1 个 Demo AI 主 session。

一次 run 内允许 HistoPilot 完成有限的多步导航；建议 Demo 固定或限制以下参数：

| 参数 | 建议值 |
|---|---|
| `max_steps` | 20 |
| 可创建主 session 数 | 1 |
| `continue / ask / branch` | 禁止 |
| 用户任务长度 | 最多 300 字，或只提供预设任务 |
| session 可重连时间 | 1 小时（由 `/api/demo/ai/session/<id>/stream` 按 `consumed_at + 1h` 拒绝） |
| session 数据保留 | 最长 24 小时后清理 |

额度在 PostgreSQL 内原子预占，防止多标签页并发创建两个 run：

1. `POST /api/demo/ai/run` 原子把状态从 `available` 更新为 `reserved`；
2. PathTogether 收到 HistoPilot 的 `security_profile_applied` 确认（含 session id，见 §5.4）后写为 `consumed` 并记录 session id；
3. SSE 断线重连只读取已有 session，不再次扣额度；
4. 若请求在 HistoPilot 接受前失败，可释放预占；模型已经开始请求后不退额度；
5. `reserved` 默认 10 分钟过期；每次新预占前惰性回收过期项，并由定时任务周期性对账；
6. request id 由客户端在每次用户动作时生成（UUID），服务端在调用 HistoPilot 前持久化去重，并写入 HistoPilot session 元数据；浏览器双击、自动重试和网关重试携带同一 request id，命中已有 reservation 时不重复扣额度。对账任务按 request id 经 HistoPilot 反查端点（见 §5.4 第 7 条）查询：已创建 session 的 reservation 转 `consumed`；确定未创建或已失联超过 TTL 的才释放；**对账时 HistoPilot 不可达的 reservation 不释放、顺延过期时间，直至可确认**，避免误退款后白跑；
7. 同时叠加 IP/网段速率限制、Demo 子额度、owner 配置的平台总预算和 Demo 最大并发；
8. 达到 Demo 子额度时返回 `demo_budget_exhausted`；达到平台总预算时返回 `platform_ai_budget_exhausted`，都不回退到其他凭据。

浏览器断网、HistoPilot 超时或 PathTogether worker 崩溃都不能让 `reserved` 永久占位。释放操作必须幂等；已被 HistoPilot 接受的执行不能因晚到的清理任务被错误退款。

IP 只能作为辅助风控：共享网络会误伤，换网络也能绕过，不能作为“一次”的唯一依据。

### 5.4 Demo AI 工具与数据

Demo run 应使用 HistoPilot 的显式只读工具集：

- 允许：`goto`、`snapshot`、`mark_observation`、`complete_snapshot_review`、`finish`；
- 禁止：`create_annotation` 及今后任何平台写工具；
- 不发放 `annotation:write` run grant；
- HistoPilot session owner 使用不可反推用户的 Demo subject；
- AI 发现的区域通过 SSE 作为临时 overlay 展示，不写 PathTogether 正式标注库；
- session 标记为 `ephemeral`，由 HistoPilot 按 TTL 清理；
- PathTogether 只保存额度状态和 HistoPilot session 引用，不读取或改写 canonical messages。

只在 PathTogether 的 `internal/annotate` 端点拒绝写入还不够。HistoPilot 必须根本不向模型暴露写工具，避免模型反复调用一个注定失败的工具，也让工具 schema 与真实权限一致。

还必须同时满足以下安全条件：

1. Demo 部署禁止 `HISTOPILOT_ALLOW_LEGACY_ADAPTER=1`。legacy adapter 不消费 run grant，不能用于任何声称只读的 Demo；HistoPilot capability/health 必须报告当前 adapter mode，PathTogether 开启公开 Demo 时检测到 legacy 必须拒绝启用。公开 Demo 模式下，PathTogether 同时禁用仅靠 internal token 的旧 `/internal/ai/annotate` 写通道；正式写入只走带 run grant 的 Plugin Contract。
2. `readonly`、`ephemeral`、`tool_profile`、session TTL 和安全契约版本属于关键安全字段。HistoPilot 不认识、缺失或版本不兼容时必须以 4xx 拒绝整个 run，禁止静默忽略后继续执行。
3. PathTogether 与 HistoPilot 的跨仓 contract 测试必须断言：Demo 工具 schema 没有 `create_annotation`，未知关键安全字段被拒绝，普通非安全扩展字段的兼容策略另行定义。
4. Demo 使用独立只读 system prompt 与 snapshot 提示，不得再要求模型“落标”“创建标注”或声称已经写入标注库。
5. 非 Demo 的写入 run grant 也必须强制签发和强制验证；发放或校验失败时 run 不得开始，不能只记录日志继续。grant 必需当且仅当该 run 的 tool profile 含平台写工具：Demo run 和只读 user 的 `/ask`（lite fork，无写工具）不需要 grant，不得因缺 grant 被拒；含写工具的 run 必须持有效 grant。
6. 即使发生版本错配，PathTogether 的写入端点仍应按 run grant 和 capability 二次拒绝，形成纵深防御。
7. HistoPilot 必须提供按 request id 反查 session 的端点（如 `GET /session/by-request/<request_id>`，内部 token 鉴权），供 PathTogether 对账任务判定 reservation 终态；查询结果必须明确区分“确定不存在”与“暂时不可达”，后者由对账侧按 §5.3 第 6 条顺延，不得按“不存在”释放。

Demo run 的安全协商使用独立闭合 envelope，而不是把未知键散放进普通 config：

```json
{
  "security_contract_version": "1.0",
  "required_features": [
    "tool-profile:demo-readonly-v1",
    "session:ephemeral-v1",
    "session-ttl:v1"
  ],
  "tool_profile": "demo-readonly-v1",
  "session_ttl_seconds": 86400,
  "request_id": "req_opaque"
}
```

HistoPilot 必须在创建 session 和调用模型前完成校验：contract major 不支持、任一 required feature 未知、字段缺失或组合矛盾均返回 4xx。成功时先返回/发送 `security_profile_applied` 确认，其中包含实际 tool profile、ephemeral 和 TTL；PathTogether 收到确认后才把 reservation 转为 consumed。普通调优 config 仍可维持向后兼容，但不能承载这些安全不变量。

### 5.5 SSE 重连与事件完整性

Demo UI 必须与正式 UI 一样处理 HistoPilot 的 `event_reset`：

- 普通断线按最后 event id 增量重连，不增加额度；
- 游标已经老化出事件窗口时，收到 `event_reset` 后重新获取 session detail/完整 transcript，重建轨迹、当前状态和临时 overlay；
- 不把单纯调大 event buffer 当作正确性方案，buffer 只影响重置频率；
- 重建失败时明确提示“运行记录需要重新加载”，不能悄悄展示不完整结论；
- Demo 的自动化测试必须产生超过事件窗口的事件，再验证 reset 后 UI 状态与完整 session 一致。

### 5.6 Demo Viewer UI

Demo 页面保留：

- 切片选择；
- 缩放、旋转、镜像、比例尺；
- AI 面板、轨迹与临时观察高亮；
- Demo 剩余额度提示；
- 登录/注册转化入口。

Demo 页面隐藏或移除：

- 上传、拖拽上传和删除切片；
- 保存人工标注、评论和正式审核；
- 项目管理和分享管理；
- AI 凭据配置、高级参数、历史 session、继续、追问和分支；
- 用户管理和插件管理。

不能只用 CSS 隐藏。Demo 模式应由服务端渲染明确的页面模式，并由后端拒绝所有越界请求。

建议 AI 按钮状态文案：

- 可用：`体验 AI 导航（1 次）`；
- 运行中：`AI 正在读片…`；
- 已使用：`本次体验已使用`，旁边显示 `登录后继续使用`；
- Demo 子额度耗尽（`demo_budget_exhausted`）：`今日 Demo 体验次数已用完，登录后继续使用`；
- 平台总预算耗尽（`platform_ai_budget_exhausted`）：`当前平台 AI 体验额度已用完`；
- HistoPilot 不可达：`AI 暂不可用，切片仍可浏览`。

## 6. 登录页面优化

### 6.1 页面目标

登录页服务 owner 和普通 user，不再使用“管理员登录”作为标题。用户应能一眼知道：可以登录协作，也可以不登录体验 Demo。

建议文案：

- 标题：`登录 PathTogether`；
- 副标题：`继续查看、测试 AI，并与他人分享切片`；
- 账号标签：`邮箱或用户名`；
- 密码标签：`密码`；
- 主按钮：`登录`；
- 次入口：`没有账号？查看注册方式`；
- Demo 入口：`先体验 Demo`；
- 测试期找回提示：`忘记密码？请联系邀请你的管理员重置。`

在没有真正密码找回流程前，不展示一个不可用的“忘记密码”链接。

### 6.2 布局

桌面端建议左右结构：

- 左侧约 55%：品牌、一句话说明和抽象的切片 Viewer 视觉；
- 右侧约 45%：宽度 360–420px 的登录卡片；
- 不使用真实病例截图作为默认背景；若使用 Demo 切片缩略图，必须来自 Demo allowlist；
- 移动端改为单列，先显示品牌和表单，Demo 入口紧跟主按钮。

保持当前界面的简洁圆角与浅色视觉，但减少“后台管理系统”感。登录卡片只保留一个主色按钮，注册和 Demo 使用次级按钮或文本链接。

### 6.3 表单交互

- 支持邮箱或用户名登录；
- 密码输入框提供显示/隐藏按钮，按钮有可访问名称；
- Enter 提交；提交期间按钮显示 `登录中…` 并防重复提交；
- 失败统一显示 `账号或密码错误`，不泄露账号是否存在；
- 达到防爆破阈值显示剩余等待时间，不只显示笼统错误；
- 登录成功前清理旧 session 内容，再写入新身份；
- `next` 只允许站内绝对路径，禁止 `//`、协议和外部 origin；
- 登录成功后优先返回原页面，否则进入 `/`；
- 中英文文案均去掉 owner/admin 专属措辞。

登录失败计数和锁定状态不能继续使用 per-worker 内存字典。公网/多 worker 模式下必须使用 PostgreSQL 或等价的一致原子存储，**每账号与每规范化 IP 前缀各一个独立计数器**（若用账号 × IP 复合键，僵尸网络对单账号撞库时每条记录都是 fresh 的，永远达不到阈值），任一桶达到阈值即锁定，并保存 `locked_until`：

- 两个 gunicorn worker 看到同一失败次数和锁定截止时间；
- UI 的剩余等待时间来自服务端权威 `retry_after`，不能由浏览器猜测；
- 成功登录只清理该主体的失败状态，不影响其他来源；
- 存储不可用时保守拒绝登录写操作，不能退化为无防爆破；
- 原始密码、完整 session cookie 和不必要的原始 IP 不进入日志。

### 6.4 页面状态

| 状态 | 表现 |
|---|---|
| 正常 | 登录表单 + Demo 入口 + 注册入口 |
| 正在提交 | 表单暂时禁用，按钮显示进度 |
| 凭据错误 | 表单顶部行内错误；保留账号、清空密码 |
| IP 暂时锁定 | 展示倒计时和 Demo 入口 |
| 已登录 | 直接跳安全 `next` 或 `/` |
| AI 不可达 | 不影响登录，不在登录页制造全站故障感 |

## 7. 注册页面优化

### 7.1 第一阶段：注册关闭

默认 `registration_open=false`。访问 `/register` 时不要返回 404，也不要展示一个无法提交的表单，而是显示清晰状态：

- 标题：`当前采用邀请注册`；
- 说明：`测试账号由管理员创建。如果你已收到账号，请直接登录。`；
- 主按钮：`返回登录`；
- 次按钮：`先体验 Demo`。

owner 用户管理页继续支持创建 user 和重置密码。可后续增加一次性邀请链接，替代线下发送初始明文密码。

### 7.2 后续阶段：开放注册

owner 开启自助注册后，页面显示：

- 邮箱；
- 显示名，可选；
- 密码；
- 确认密码；
- 研究/教学用途提示；
- `创建账号` 主按钮；
- `已有账号？登录` 与 `先体验 Demo`。

开放注册的最低安全要求：

1. 邮箱验证后账号才可使用平台 AI；
2. 注册、验证邮件重发和登录分别限流；
3. 密码至少 8 位，并允许密码管理器粘贴和自动填充；
4. 错误响应不泄露过多账户信息；邮箱已存在时可提示登录或重置，但邮件接口仍返回统一结果；
5. 每用户平台 AI 预算周期额度、平台总预算和最大并发已启用；
6. owner 可停用账号，停用后现有 session 立即失效；
7. 注册写入、邮箱验证 token 消费和账号启用使用事务；
8. 提供 CSRF 防护，认证 Cookie 在公网强制 `Secure / HttpOnly / SameSite=Lax`；
9. 必须先有 HTTPS，明文 HTTP 部署不得开放注册或登录测试。

### 7.3 注册开关的权威来源

当前 `REGISTRATION_OPEN` 是启动期环境变量，只能显示，owner 无法在 UI 中真正修改。目标状态建议：

- PostgreSQL `platform_settings.registration_open` 为运行时权威值；
- env 只作为首次部署的 bootstrap 默认值；
- owner 在用户管理页切换；
- 变更写轻量操作日志；
- 多 worker 立即读取一致值，不依赖进程内常量；
- 从关闭切到开启前，后端检查 HTTPS/邮箱服务/用户 AI 额度配置是否齐备，不满足则拒绝开启并说明原因。

## 8. 登录后应用 UI

### 8.1 user 视角

注册用户登录并进入 `/` 后保留：

- 自己的项目和切片；
- 公开切片和已认领的分享切片；
- 上传、标注、评论；
- 创建自己切片的分享；
- 自己的 AI session；
- AI 来源选择：平台 AI / 自己的凭据。

隐藏：

- 用户管理；
- 插件 installation 管理；
- 平台 AI key 和全局调优参数；
- 设置切片进入 Demo 目录；
- 他人的私有切片和 AI session。

### 8.2 owner 视角

owner 额外看到：

- 用户与注册策略；
- Demo 目录管理；
- 平台 AI、Demo AI 额度、每用户用量和全站预算；
- 插件管理；
- 全部切片、分享和协作操作日志。

### 8.3 现有文案与控件调整

- `管理员登录` → `登录 PathTogether`；
- `请输入管理员账号以继续` → `登录后继续查看、测试 AI 和协作`；
- `AI 读片助手（管理员）` → `AI 导航助手`；
- owner AI 配置标题使用 `平台 AI 配置`；
- user AI 配置标题使用 `我的 AI 设置`；
- user 使用平台 AI 时，任务步骤显示为只读的 20 步（或 owner 当前设置）；
- user 切换到自己的 API 凭据后，启用任务步骤输入，默认 20、当前范围 1–500；
- 分享弹窗显式提供：`仅查看`、`允许标注`、`允许下载`，不再无提示地使用默认权限；
- user 创建分享时明确显示“只能分享你拥有的切片”；
- 所有禁用状态给出原因，不只把按钮变灰。

## 9. 建议 API 与数据模型

以下是设计接口名，实施时可在不改变语义的前提下调整命名。

### 9.1 Demo API

| 方法 | 路径 | 能力 |
|---|---|---|
| `GET` | `/api/demo/config` | Demo 开关、额度状态、AI 可达性 |
| `GET` | `/api/demo/slides` | Demo allowlist 中的切片摘要 |
| `GET` | `/api/demo/slides/<id>/info` | Demo 切片信息 |
| `GET` | `/api/demo/slides/<id>.dzi` | Deep Zoom 描述 |
| `GET` | `/api/demo/slides/<id>_files/...` | Demo tiles |
| `POST` | `/api/demo/ai/run` | 原子预占额度并创建只读临时 run |
| `GET` | `/api/demo/ai/session/<id>/stream` | 当前 capability 绑定 session 的 SSE 重连 |

所有接口同时校验 Demo capability、Demo slide allowlist 和 route scope。不能因为拿到某个 Demo session id 就读取其他 session。

### 9.2 认证与设置 API

| 方法 | 路径 | 第一阶段 |
|---|---|---|
| `GET/POST` | `/login` | 实现并优化现有页面 |
| `POST` | `/logout` | 改为 POST + CSRF；兼容期可保留 GET 后再移除 |
| `GET/POST` | `/register` | 关闭态页面；开放后提交注册 |
| `GET` | `/api/auth/info` | 增加可用于 UI 的 capability 摘要，不返回秘密 |
| `GET/PUT` | `/api/ai/config` | user 自带凭据配置增加 `max_steps`；仅自带 API 模式生效 |
| `PUT` | `/api/admin/settings/registration` | 后续由 owner 修改注册开关 |
| `GET/PUT` | `/api/admin/settings/ai-budget` | owner 查看用量并修改总量、用户量和任务步骤上限 |
| `POST` | `/api/admin/settings/ai-budget/reset` | 二次确认后开启新的预算周期 |

user 的 AI 配置需持久化自己的 `max_steps`：默认 20，当前允许 1–500。当 `use_platform=true` 时，服务端忽略该用户值并注入 owner 配置的平台 AI 步数（默认 20）；当 `use_platform=false` 且自带凭据完整时，才注入用户设置的步数。浏览器请求不能临时提交一个未保存的更大值绕过配置校验。

> **2026-08 更新**：user 的「自定义 API（自带凭据）」通道已下线——AI 服务统一由平台提供。`_resolve_ai_credentials` 对 user 恒返回平台（未配置则不可用并提示联系管理员）；user `PUT /api/ai/config` 任意字段一律 400「AI 服务由平台统一提供，用户无需配置」；user GET 的 `using` 只会是 `"platform"` 或 `null`。上表与上文中的「自带凭据 / use_platform=false / max_steps 用户自设」等描述为历史设计，保留作演进记录；`user_store` 中的旧凭据字段保留不读（无害存量）。

### 9.3 Demo session 表

建议字段：

```text
demo_sessions
  id
  token_hash
  created_at
  expires_at
  run_state          # available | reserved | consumed
  reserved_at
  reservation_expires_at
  consumed_at
  histopilot_session_id
  slide_id            # FK；删除时撤销 capability 并触发 session 清理
  asset_revision      # run 创建时绑定，禁止同名替换后继续复用
  ip_prefix_hash     # 可选、轮换盐，仅辅助限流
```

`reserved` 默认 10 分钟过期，语义见 §5.3。切片重命名依赖稳定 `slide_id` 不受影响；切片删除、移出 Demo 目录或 asset revision 变化时必须撤销对应 capability、终止/隐藏未完成 Demo session，并让清理任务删除事件和派生图引用，不能留下可继续读取的悬空 session。

原始 IP、明文 capability token、模型 API key 和完整 AI transcript 不进入此表。

### 9.4 平台 AI 预算数据

预算设置与用量建议使用一个当前周期记录和按主体聚合的用量记录：

```text
ai_budget_periods
  id
  started_at
  closed_at
  platform_turn_limit       # 默认 30
  demo_turn_limit           # 默认 5，包含在 platform_turn_limit 内
  user_turn_limit           # 默认 10
  platform_task_max_steps   # 注册用户平台 AI 默认 20
  own_task_max_steps_limit  # 自带 API 系统硬上限，默认 500
  demo_task_max_steps       # 默认 20
  created_by

ai_budget_usage
  period_id
  subject_type              # owner | user | demo
  subject_id
  credential_source         # platform | own
  accepted_turns
  reserved_turns
  updated_at

ai_budget_reservations
  request_id                # 幂等键
  period_id
  subject_type
  subject_id
  state                     # reserved | consumed | released
  reserved_at
  reservation_expires_at
  histopilot_session_id
```

所有 `credential_source=platform` 的 Demo、owner 和 user 执行都消耗平台总用量；Demo 还受 5 次子额度约束，role=user 还同时消耗该用户的 10 次额度。`credential_source=own` 只记录可观测用量，不扣平台总额度。每次触发使用幂等 request id 关联一条 reservation，避免超时重试重复计数，并按 §5.3 回收过期预占。

### 9.5 登录限流数据

生产登录锁定使用 PostgreSQL 权威记录，而不是进程内字典：

```text
auth_rate_limits
  scope               # account | ip_prefix
  subject_hash        # scope=account：规范化账号标识的带盐 hash；scope=ip_prefix：IP 前缀带盐 hash（轮换盐，不持久保存完整 IP）
  window_started_at
  failed_count
  locked_until
  updated_at
```

每次失败在同一事务内更新 account 与 ip_prefix 两条记录；任一达到各自阈值即锁定（建议：IP 前缀 5 次/窗、账号 10 次/窗）。锁定和成功登录后的清理（仅清该账号与来源 IP 前缀两条记录）都使用原子事务。记录按短 TTL 清理；API 用权威 `locked_until` 计算 `Retry-After` 和页面倒计时。

## 10. 安全与隐私边界

1. HTTPS 是公网 Demo、登录和注册的共同前置条件；当前明文公网端口只能用于无凭据诊断，不能作为正式入口。
2. Demo API 使用显式 allowlist；未知、被移除或归档的切片一律拒绝。
3. Demo capability 与登录 session 使用不同 cookie 名和解析逻辑。
4. PathTogether 不把平台 API key、用户 key、plugin secret 或 internal token 返回浏览器。
5. user 自定义 `base_url` 继续执行 SSRF、DNS rebinding 和重定向限制。
6. 登录、注册、Demo run、tile 和 region 分别限流；不能用一个宽泛桶替代成本不同的限制。
7. Demo 的 AI prompt 限长，并使用服务器端固定系统 prompt；浏览器不能覆盖工具集或模型密钥。
8. Demo session 清理同时删除事件和派生图缓存引用；清理失败需可观测和可重试。
9. Demo AI 输出始终显示研究/教学提示，不能伪装成正式诊断报告。
10. 任何仅靠前端隐藏实现的权限都视为未完成。
11. 公开 Demo 必须使用 PostgreSQL 权威存储并禁用 legacy adapter；任一前置不满足即 fail-closed。
12. HistoPilot 对关键安全字段必须显式协商且未知即拒绝，禁止把静默忽略当成兼容。
13. 所有 cookie-authenticated 状态写接口都纳入统一 CSRF 设施，不只覆盖登录、注册和退出；API token/internal/plugin 通道使用各自的非 Cookie 鉴权，不混用 CSRF 语义。
14. 登录成功前 `session.clear()`，退出使用 POST；兼容 GET 退出只能短期存在并明确下线时间。
15. 写入 run grant 的签发和验证必须 fail-closed；annotate 权限失败必须终止当前 Agent 执行并产生可观察错误事件。

## 11. 分阶段实施

### 11.1 现状链路加固：Demo 实施前完成

以下是现有链路的真实缺口，必须独立修复和验证，不能等 Demo UI 顺带覆盖：

1. **run grant 强制化**：PathTogether 发放失败就拒绝 run；HistoPilot 校验失败也拒绝 run，不再 best-effort 或只写日志。Demo 不拿写 grant，普通写入 run 必须拿到有效 grant。
2. **并发 run 幂等**：`fresh=true` 和网关重试携带稳定 request id；同一 slide/subject 的创建使用锁或 CAS，只允许一个权威 session 注册成功。失败或失索引的孤儿 session 有启动期/定时回收。
3. **标注幂等跨重启稳定**：effect key 不能依赖进程内会复位的计数器；使用持久化 session/bundle 序号、tool call id 和稳定 request id 组合，并有跨进程重启重复执行测试。
4. **annotate 403 为致命权限错误**：不得吞成普通工具文本继续烧步骤；立即停止 Agent、发 `agent_error`/权限错误事件，并保证没有后续写工具重试。
5. **HistoPilot 优雅停机**：绑定 SIGTERM/SIGINT，停止接收新 run、drain/中止在途 SSE、持久化最终状态、释放或等待 reservation 对账，然后在容器停止窗口内退出。
6. **统一 CSRF**：建立覆盖全部 Cookie 会话写端点的 CSRF 基础设施；登录成功前清理旧 session，退出改 POST。现有 GET logout 只作短期兼容。
7. **跨 worker 登录锁定**：把失败次数、`locked_until` 和 `retry_after` 迁到 PostgreSQL 权威状态，删除生产路径中的 per-worker 内存锁定语义。
8. **安全配置协商**：HistoPilot 对 `readonly / ephemeral / tool_profile / ttl / security_contract_version` 做严格 schema 校验；未知或缺失拒绝，跨仓 contract 测试锁定。

上述八项完成前，公开 Demo 只能保持关闭。

### Phase 0：公网前置

- 为公开入口配置域名和 HTTPS；
- 设置 `SESSION_COOKIE_SECURE=1`；
- 切换并验证 `STORAGE_BACKEND=postgres`；json/dual 不开放 Demo 和多用户平台 AI 预算；
- 确认 `HISTOPILOT_ALLOW_LEGACY_ADAPTER` 未开启，并加入启动期拒绝检查；
- 保证 PathTogether 与 HistoPilot 内部通道仍位于 loopback 或私有网络；
- 建立 owner 可配置的 AI 预算；初始值为 Demo 20 步/子额度 5 次、注册用户平台 AI 20 步、每 user 10 次、全站 30 次；自带 API 步数由 user 设置。

### Phase 1：入口和认证 UI

- 保留 `/` 为登录后应用；未登录 `/` 渲染入口分流页，不引入 `/app` 路由迁移；
- 优化 `/login` 文案、布局、错误与 loading 状态；
- 增加 `/register` 关闭态页面；
- 修正中英文里的 admin-only 旧文案；
- 建立统一 CSRF 中间件，盘点并覆盖全部 Cookie 会话写端点；把 logout 改为 POST，登录成功前清理旧 session；
- 登录防爆破迁入 PostgreSQL，跨 worker 返回一致的 `Retry-After`；
- user/owner UI 按 capability 显示；
- 分享 UI 补齐权限选择；
- owner 后台增加 AI 预算设置与当前用量。

### Phase 2：只读 Demo

- 增加 Demo allowlist 和 owner 管理入口；
- 增加独立 Demo capability 与 `/api/demo/*`；
- 完成 Viewer 只读页面；
- 增加 PostgreSQL 原子额度、全站预算与限流；
- 增加 Demo 5 次子额度与按 Demo/user/owner 的用量构成；
- HistoPilot 增加只读工具集、只读 prompt、ephemeral session 和 TTL 清理；
- 实现关键安全字段严格协商，未知字段拒绝 run；
- 实现 reservation 10 分钟 TTL、惰性回收、定时对账和 worker 崩溃恢复；
- 完成断线重连和幂等重试不重复扣额度，并处理 `event_reset` 全量重建；
- Demo 固定为一次简单 run、最多 20 个任务步骤。

### Phase 3：邀请注册完善

- owner 创建账号流程改为一次性邀请链接；
- 用户首次打开邀请链接设置自己的密码；
- 邀请 token 单次使用、短期有效、数据库只存 hash；
- 保留 owner 重置/停用能力。

### Phase 4：可选开放注册

- 邮箱验证、密码找回；
- owner 运行时注册开关；
- 每用户平台 AI 额度；
- 注册和邮件反滥用；
- 完整验证后再对公网打开。

## 12. 验收标准

### 12.1 匿名 Demo

- 未登录可打开 `/demo` 并只看到 Demo allowlist 切片；
- json/dual 后端或 legacy adapter 开启时，公开 Demo 无法启动；
- 不能通过改 URL 读取普通 public、私有或不存在的切片；
- 只能创建一个受限只读 AI session；并发双击也只有一个成功；
- Demo 最多消耗预算周期内 5 次子额度；耗尽后注册用户仍可使用剩余平台总额度，后台可见用量构成；
- `reserved` 超过 10 分钟会经对账安全转为 consumed 或 released；worker/HistoPilot/浏览器异常不造成永久泄漏或错误退款；
- SSE 重连不新增额度；事件老化触发 `event_reset` 后能全量重建 transcript、轨迹和 overlay；
- HistoPilot 工具 schema 中没有 `create_annotation`，只读 prompt 不要求或声称“落标”；
- 缺失或未知关键安全字段时 HistoPilot 拒绝 run；跨仓 contract 测试覆盖版本错配；
- Demo run 不产生正式标注、评论、分享或持久用户；
- 切片删除、移出 Demo 或 asset revision 改变后，旧 capability 与 session 不再可读并进入清理；
- capability 过期后不能读取原 session；
- 全站预算耗尽时 Viewer 仍可用，AI 给出明确降级提示。

### 12.2 登录与注册

- 登录页不再出现“仅管理员”文案；
- owner 和 user 均能从同一页面登录并进入正确 UI；
- 错误不泄露账号是否存在；锁定状态和等待时间清晰；
- 两个以上 gunicorn worker 共享同一登录失败计数和 `retry_after`，重启/负载均衡不能绕过锁定；账号桶与 IP 前缀桶独立计数、任一触发即锁定，模拟“多 IP 撞单账号”与“单 IP 撞多账号”两种模式都必须被锁定；
- 外部 `next`、`//host` 和协议 URL 均被拒绝；
- `registration_open=false` 时 `/register` 显示关闭态且无法创建账号；
- 注册开放前置条件不满足时，owner 无法误开启；
- 登录成功前清理旧 session；退出使用 POST；
- 全部 Cookie 会话写接口有统一 CSRF 测试，internal/plugin/token API 不误套 Cookie CSRF；
- Cookie 的 Secure/HttpOnly/SameSite 行为有配置与回归测试。

### 12.3 注册用户

- user 可使用平台 AI，也能保存和切换自己的凭据；
- user 每次平台 AI 触发最多 20 个任务步骤，当前预算周期最多 10 次；
- user 使用自带 API 时可设置每次任务步骤，默认 20，后端拒绝小于 1 或超过当前系统硬上限的值；
- 平台 AI 总计达到 30 次后，Demo、owner 和 user 的新平台 AI 请求均被拒绝，自定义 key 仍可按策略运行；
- owner 修改额度后立即生效，开启新预算周期后用量归零且旧统计仍保留；
- 并发请求不能突破用户 10 次或平台 30 次上限，失败预占能够正确释放；
- user 不能读取或修改平台 API key 和调优参数；
- user 只能上传、管理和分享自己的切片；
- 分享链接权限与 UI 选择一致；
- user 只能列出和继续自己的 AI session；
- owner 保持全部管理能力；
- PathTogether、HistoPilot 和跨仓 contract 测试全部结束并通过后才算完成。

### 12.4 现有 AI 链路加固

- run grant 发放或验证失败时请求被拒绝，sidecar 不启动 Agent；
- 并发 `fresh=true` 与同 request id 重试只有一个权威 session，重启对账后没有失索引孤儿文件；
- 标注 effect key 在 PathTogether/HistoPilot 任一进程重启后仍稳定，重复执行只落一条正式标注；
- annotate 403 立即终止 Agent 并产生错误事件，不作为普通 tool text 继续执行；
- SIGTERM 到来后拒绝新 run、收尾或明确中止在途 run、持久化状态，并在容器停止窗口内正常退出；
- 上述场景均有跨仓或端到端回归测试，不能以日志存在代替行为断言。

## 13. 需要拍板的产品参数

已确认的测试期默认值是：Demo 每浏览器 1 次且每次最多 20 步、预算周期内 Demo 子额度 5 次；注册用户使用平台 AI 时每次最多 20 步、每个预算周期 10 次；自带 API 时用户可设置步数；Demo/user/owner 共用平台总计 30 次。owner 后台可调整平台和 Demo 步数、次数及自带 API 的系统硬上限。其余仍需在实施时确认：

1. Demo 是否只提供预设任务，还是允许最多 300 字自由输入；
2. Demo session 清理时间采用 1 小时不可继续、24 小时物理清理是否合适；
3. Phase 3 是否需要一次性邀请链接，还是暂时继续由 owner 线下发送初始密码。

除上述参数外，权限边界、入口结构和拆仓职责可按本文直接进入实现设计。
