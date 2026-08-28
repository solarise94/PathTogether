# PathTogether Admin 插件、用户额度与 AI 计费实施方案

> 状态：**实施前设计稿，尚未实现**
>
> 日期：2026-08-28
>
> 版本：v0.3（v0.2 基础上吸收 PR5 外部 review 的 P1 修订，见文末「v0.3 修订记录」）
>
> 范围：PathTogether + HistoPilot + mywebpage 来源入口；HistoPilot-DSH 协议不改
>
> 部署目标：homePC 当前 `pathtogether-demo` / `histopilot-demo` / `svs-pg` Podman 环境
>
> 核心决策：管理界面做成 PathTogether 内的受信任 `admin.workspace` 插件；身份、额度、来源、账本和管理 API 仍由 PathTogether 所有。

## 1. 决策摘要

本次升级采用以下边界：

```text
mywebpage（官网）
  └─ 产品 CTA /r/<source_code>?campaign=<slug>，只提供来源线索
          ↓
PathTogether（平台权威）
  ├─ 用户、邀请、AI 权限、来源归因
  ├─ 对话额度、价格表、用量事件、金额账本
  ├─ owner-only /admin 宿主页
  └─ sandbox AdminBridge
          ↑
pathtogether-admin（受信任 UI 插件）

HistoPilot（模型调用事实）
  └─ durable usage outbox
          ↓ at-least-once
PathTogether /api/plugin/v1/usage-events
          ↓
PostgreSQL 计价 + 幂等入账
```

必须保留三个独立概念，UI、API 和数据表不得混用名称：

| 概念 | 权威字段/实体 | 作用 |
|---|---|---|
| AI 使用权限 | `users.ai_access` | 用户能否使用平台 AI |
| 对话/步骤额度 | 现有 `ai_budget_*` | 防滥用、控制 Agent 次数、步骤和并发 |
| 金额额度/余额 | 新 `billing_*` | 按实际模型 token 计费、充值、调整、消费上限 |

现有 `ai_budget_periods`、`ai_budget_usage`、`ai_budget_reservations` 继续按“用户动作/对话次数”工作，**不得改造成 token 或金额表**。一次用户动作可能触发多次模型调用；复用现有 turn reservation 做金额账单会系统性少计。

第一阶段只做影子计量和管理员只读报表；在 §14 的硬计费门槛全部满足前，不得自动扣除用户金额余额。

## 2. 当前基线与缺口

### 2.1 已有能力

PathTogether 当前已经具备：

- PostgreSQL 权威用户表、owner/user 角色、禁用与 `auth_version` 会话失效；
- owner-only 用户列表、创建、禁用、启用、重置普通用户密码；
- 邀请注册、一次性高熵邀请码和 `cohort`；**邀请码兑换创建的用户**默认 `ai_access=false`；
- owner-only `ai_access` 授予/收回；
- turn 预算周期、平台池、owner 保留池、user 共享池、每用户上限和 request-id 幂等 reservation；
- Demo 24 小时滚动总池、每浏览器次数门槛和并发门槛；
- owner-only 审计事件；
- 插件 bundle 目录、manifest、来源 SHA-256 策略和 Plugin Contract。

当前管理 UI 位于主 Viewer 左侧栏，用户、插件、AI 预算分别由 `_app_shell.html` 和 `static/app.js` 硬编码。邀请管理另有 `/admin/registration` 页面。这些功能应迁入 admin 插件，但旧 API 在兼容期保留。

注意：`migrations/0012_registration_invites.sql` 中 `users.ai_access` 的数据库列缺省值是 `TRUE`，现有 `/api/admin/users` 直接创建用户也会得到 `TRUE`。因此实现和测试不得笼统假设“所有新用户默认 false”；只有邀请码兑换路径按 invitation template 显式写入 `false`。PR5 若要统一策略，必须另做产品决策和兼容迁移，不能借本项目静默改变现有管理员建号语义。

### 2.2 计费缺口

HistoPilot 当前 `RequestMetrics` 只有：

- `input_tokens`；
- `cached_tokens`；
- `cache_write_tokens`；
- 图片、Files API 和请求体观测字段。

当前缺少：

- `output_tokens` 和 `reasoning_tokens`；
- 内部 `call_id`、用户 `request_id`、权威 `principal_user_id`；
- provider/model/实际响应 ID；
- 可恢复的投递队列；
- 价格版本、供应商成本、用户应扣金额；
- 幂等账本与人工调账流水。

默认 metrics sink 只向 `console.info` 写 JSONL，属于 best-effort 观测，不能作为金额账单数据源。`pi-model.ts` 的 cost 字段当前全为 0，也不能成为价格权威。

### 2.3 Admin 插件缺口

现有通用插件不能直接承载管理员界面：

- manifest 权限枚举仅覆盖切片、导航和标注；
- 现有插槽主要是 `viewer.right-panel`；
- HistoPilot 和 sample 插件仍由 `templates/index.html` 手工注入；
- `/plugins/<id>/ui/*` 属于公开静态资源路由；
- 当前来源策略在策略文件缺失或插件未 pin 时允许 dev 模式加载。

因此必须增加专用 `admin.workspace` 宿主和 fail-closed 信任策略，不能让普通第三方插件脚本直接取得 owner Cookie、CSRF token 或任意管理 API 代理能力。

### 2.4 当前 homePC 部署约束

2026-08-28 只读核查结果：

- 当前容器名为 `pathtogether-demo`、`histopilot-demo` 和 `svs-pg`，前两者使用 host network；
- PathTogether 把 `/home/solarise/svs-viewer-demo-data/plugins` 挂载到 `/data/plugins`；
- `PLUGIN_BUNDLES_DIR` 已配置；
- HistoPilot bundle 已通过该目录独立投放；
- 插件目录当前以 RW 方式挂载，`plugins/histopilot` 是普通目录；当前不存在 `plugins/releases` 或原子切换 symlink；
- HistoPilot `/data/sessions` 是宿主机 bind mount，但 `/data/config` 是随机 ID 的匿名 Podman volume；
- rootless 用户 `Linger=yes`；已有 `project-sync-backup.timer`，但其脚本只备份 `youtube-trans` 和 `projects-cuda`，**不覆盖** `svs-viewer-demo-data`、usage outbox、插件目录或匿名 config volume；
- 本机 `ssh homePC` 当前解析为 `117.72.24.99:52044`；`docs/demo-deployment.md` 仍记录历史 LAN 地址 `192.168.3.223`。两者用途可能不同，发布 runbook 应以 SSH alias 为连接入口，并分别标注公网 SSH 端点和 LAN 地址，不能把二者直接互相替换。

本方案不新增官网后台容器，也不新增 admin 服务容器。PathTogether 主程序首次升级后，admin UI bundle 可以沿用现有插件目录独立发布。但 §16 的版本化 staging、原子切换、具名 config volume、专用备份与恢复演练是 PR3 的发布前置条件，不是已经具备的能力。上线时应把 PathTogether 对插件目录的挂载收紧为只读；发布动作在宿主机完成原子换版。

## 3. 领域所有权与信任边界

### 3.1 PathTogether 所有

- 用户、角色、认证状态和 `ai_access`；
- 邀请、注册模式、来源与用户归因；
- turn/step 防滥用预算；
- billing account、价格表、原始用量事件、计价结果和不可变账本；
- provider 总余额快照；
- owner 管理 API、CSRF、审计和 AdminBridge；
- API key 的加密存储与 DeepSeek 余额查询。

### 3.2 HistoPilot 所有

- agent/session/request/model-call 生命周期；
- 从 provider 最终响应提取 token usage；
- 为每次真实 provider 调用生成 `call_id`；
- 持久化 usage outbox、重试投递和 backlog 指标；
- 在未来硬额度阶段逐 model call 请求 hold/settlement。

HistoPilot 不读取 PathTogether PostgreSQL，不自行维护用户余额，也不根据 `user_id` 文本猜测计费主体。

### 3.3 Admin 插件所有

- 管理页面布局、表格、筛选、图表和交互；
- 通过 AdminBridge 请求声明过的管理能力；
- 客户端表单校验和危险操作二次确认。

Admin 插件不保存业务权威数据、不持有 service token、不直接访问 PostgreSQL、不读取 owner Cookie 或 CSRF token，也不能把管理响应写入 localStorage。

### 3.4 mywebpage 所有

- 官网页面与站内 pageview/source 统计；
- 指向 PathTogether `/r/<source_code>?campaign=<slug>` 的 CTA；
- 展示层 UTM 参数透传。

`mywebpage/content/sources.jsonl` 只能做官网访问分析，不能用于按 IP、邮箱或用户名认领 PathTogether 用户。

## 4. DeepSeek 用量与价格契约

实施日必须重新核对官方文档。本设计按 2026-08-28 官方契约：

```text
prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens
total_tokens  = prompt_tokens + completion_tokens
reasoning_tokens <= completion_tokens
```

官方来源：

- [Chat Completions usage 字段](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)
- [模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
- [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance/)

DeepSeek context cache 默认启用、best effort。计费必须使用 provider 返回的实际 hit/miss 数字，不得根据本地 stable prefix、图片缓存或历史请求推算。

截至该日期的官方 CNY/百万 tokens 价格快照：

| 模型 | 时段 | 缓存命中输入 | 缓存未命中输入 | 输出 |
|---|---:|---:|---:|---:|
| `deepseek-v4-flash` | 空闲 | 0.05 | 1.5 | 4.5 |
| `deepseek-v4-flash` | 高峰 | 0.10 | 3.0 | 9.0 |
| `deepseek-v4-pro` | 空闲 | 0.15 | 4.5 | 13.5 |
| `deepseek-v4-pro` | 高峰 | 0.30 | 9.0 | 27.0 |
| `deepseek-v4-flash-vision-exp` | 空闲 | 0.05 | 1.5 | 4.5 |
| `deepseek-v4-flash-vision-exp` | 高峰 | 0.10 | 3.0 | 9.0 |

高峰按北京时间工作日 09:00–12:00、14:00–18:00，其余为空闲。图片会换算成输入 token。因此：

- 计价时间取该次 provider 调用的 `occurred_at`，统一按 `Asia/Shanghai` 判断时段；服务端不得为了“能计价”而静默改用 `received_at`；
- 价格必须版本化并保留历史行；
- usage event 入库后固定其 `price_book_id` 和计算结果，后续调价不得重算历史账单；
- DeepSeek `/user/balance` 是供应商总余额，只能用于成本监控，不能拆分为平台用户余额。

时钟与延迟规则：允许 durable outbox 导致的正常延迟到达；若 `occurred_at > received_at + 5 分钟`，或早于 `received_at - 30 天`，事件写为 `unpriced`（`clock_skew_future` / `occurred_at_out_of_range`）并告警。实现增加 `enqueued_at` 供判断发送积压；不能用 `received_at` 代替 `occurred_at`，因为那会在价格时段边界静默改变账单。30 天窗口应做成受控配置，但任何放宽都只影响新事件。

## 5. 金额表示与计价规则

金额统一使用 `BIGINT nano_cny`：

```text
1 CNY = 1,000,000,000 nano_cny
```

原因：最低价 0.05 CNY/百万 token 等于每 token 50 nano-CNY；使用“分”、浮点数或整数微元会丢失单次调用精度。

单次费用：

```text
cost_nano_cny =
  cache_hit_tokens  * hit_rate_nano_per_million  / 1,000,000
+ cache_miss_tokens * miss_rate_nano_per_million / 1,000,000
+ output_tokens     * output_rate_nano_per_million / 1,000,000
```

实现使用整数运算。三个分项分别计算后相加，每个分项采用向上取整，避免大量小请求被系统性舍零：

```text
ceil(tokens * rate / 1,000,000)
```

必须同时保存两种金额：

- `provider_cost_nano_cny`：按 DeepSeek 官方成本价格表计算；
- `charge_nano_cny`：平台对用户的扣费价格表计算。

影子阶段 `charge_nano_cny` 可以复制 provider cost，但两套 price book 仍须独立，避免以后加折扣、赠送额度或服务费时重写历史结构。

## 6. PostgreSQL 数据模型

### 6.1 迁移拆分

当前最新迁移为 `0017_upload_tasks.sql`。本功能按以下顺序新增：

1. `migrations/0018_billing.sql`：价格、用量、账户、账本、余额快照；
2. `migrations/0019_acquisition.sql`：campaign、访问触点、用户归因、邀请来源字段；
3. `migrations/0020_billing_holds.sql`：只在硬额度阶段加入 model-call hold；不得提前开启执行路径。

0018/0019 必须幂等，并由现有 migration runner 记录。billing/acquisition 能力只在 `STORAGE_BACKEND=postgres` 开放；json/dual 返回稳定 `pg_backend_required`，不得降级到进程内余额。

### 6.2 `billing_accounts`

```sql
CREATE TABLE billing_accounts (
    account_id             TEXT PRIMARY KEY,
    user_id                TEXT NOT NULL UNIQUE REFERENCES users(user_id),
    currency               TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    status                 TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','suspended','closed')),
    soft_spend_cap_nano    BIGINT,
    hard_spend_cap_nano    BIGINT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    version                BIGINT NOT NULL DEFAULT 1
);
```

余额不设可直接 UPDATE 的列。权威可用余额为 ledger 的有符号金额合计，必要时增加事务内维护的 projection/cache，但 projection 可重建且不得替代 ledger。

### 6.3 `billing_price_books` / `billing_rates`

```sql
CREATE TABLE billing_price_books (
    price_book_id TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('provider_cost','customer_charge')),
    currency      TEXT NOT NULL CHECK (currency = 'CNY'),
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to   TIMESTAMPTZ,
    status         TEXT NOT NULL CHECK (status IN ('draft','active','retired')),
    source_url     TEXT NOT NULL DEFAULT '',
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE billing_rates (
    price_book_id                   TEXT NOT NULL REFERENCES billing_price_books,
    provider                        TEXT NOT NULL,
    model                           TEXT NOT NULL,
    time_band                       TEXT NOT NULL CHECK (time_band IN ('peak','off_peak')),
    cache_hit_nano_per_million      BIGINT NOT NULL CHECK (cache_hit_nano_per_million >= 0),
    cache_miss_nano_per_million     BIGINT NOT NULL CHECK (cache_miss_nano_per_million >= 0),
    output_nano_per_million         BIGINT NOT NULL CHECK (output_nano_per_million >= 0),
    timezone                        TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    schedule                        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (price_book_id, provider, model, time_band)
);
```

同一 `kind`、provider、model 的生效区间不得重叠。本项目明确选择**事务串行化检查**，不引入 `btree_gist`：激活 price book 时先取得固定的 billing price-book `pg_advisory_xact_lock`，再查询所有 active 区间并拒绝重叠，最后在同一事务更新状态。并发激活测试必须证明只有一个事务成功。只有 `draft` 可编辑；active/retired 行不可原地修改。

### 6.4 `ai_usage_events`

```sql
CREATE TABLE ai_usage_events (
    event_id                  TEXT PRIMARY KEY,
    call_id                   TEXT NOT NULL UNIQUE,
    payload_hash              CHAR(64) NOT NULL
                              CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    schema_version            INT NOT NULL,
    request_id                TEXT,
    session_id                TEXT NOT NULL,
    subject_type              TEXT NOT NULL CHECK (subject_type IN ('owner','user','demo')),
    subject_id                TEXT NOT NULL,
    user_id                   TEXT REFERENCES users(user_id),
    provider                  TEXT NOT NULL,
    model                     TEXT NOT NULL,
    provider_request_id       TEXT,
    cache_hit_input_tokens    BIGINT CHECK (cache_hit_input_tokens >= 0),
    cache_miss_input_tokens   BIGINT CHECK (cache_miss_input_tokens >= 0),
    output_tokens             BIGINT CHECK (output_tokens >= 0),
    reasoning_tokens          BIGINT CHECK (reasoning_tokens >= 0),
    total_tokens              BIGINT CHECK (total_tokens >= 0),
    occurred_at               TIMESTAMPTZ NOT NULL,
    enqueued_at               TIMESTAMPTZ NOT NULL,
    received_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                    TEXT NOT NULL CHECK (status IN ('priced','unpriced','void')),
    unpriced_reason           TEXT NOT NULL DEFAULT '',
    provider_price_book_id    TEXT REFERENCES billing_price_books,
    charge_price_book_id      TEXT REFERENCES billing_price_books,
    provider_cost_nano_cny    BIGINT,
    charge_nano_cny           BIGINT,
    raw_usage                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (reasoning_tokens IS NULL OR output_tokens IS NULL
           OR reasoning_tokens <= output_tokens),
    CHECK (total_tokens IS NULL
           OR (cache_hit_input_tokens IS NOT NULL
               AND cache_miss_input_tokens IS NOT NULL
               AND output_tokens IS NOT NULL
               AND total_tokens = cache_hit_input_tokens
                                  + cache_miss_input_tokens
                                  + output_tokens)),
    CHECK (status <> 'priced'
           OR (cache_hit_input_tokens IS NOT NULL
               AND cache_miss_input_tokens IS NOT NULL
               AND output_tokens IS NOT NULL
               AND total_tokens IS NOT NULL
               AND provider_price_book_id IS NOT NULL
               AND charge_price_book_id IS NOT NULL
               AND provider_cost_nano_cny IS NOT NULL
               AND charge_nano_cny IS NOT NULL))
);
```

约束：

- token 非负、`reasoning_tokens <= output_tokens` 和 `total_tokens = hit + miss + output` 必须由数据库 `CHECK` 兜底，不能只靠应用校验；
- 中断且无最终 usage 的 `unpriced` 事件允许 token 为 `NULL`，不得用 0 冒充；`priced` 行必须具备完整计价 token、两套 price book 和两种金额；
- `raw_usage` 只存 token 数、finish reason 和版本化 provider 元数据，不存 prompt、输出文本、图片、API key 或完整请求体；
- `provider_request_id` 可空，因上游 SDK/中断未必总能取得；内部 `call_id` 必须存在；
- 同一事件重复投递返回原行，不重复计价或入账；
- 未知模型、找不到有效价格、缺最终 usage、算术校验失败时写 `unpriced`，不猜测 token、不自动扣费。

`payload_hash` 由 PathTogether 在 schema 校验和字段规范化后自行计算，不能信任客户端提交值。哈希输入是账单语义字段的 canonical JSON（固定字段集合、UTF-8、key 排序、紧凑分隔、时间统一 UTC ISO-8601、整数保持整数、缺值为 `null`）；排除 `received_at`、重试次数、HTTP header 和 `raw_usage` 中非账单诊断字段。同一 `event_id` 重放时先重算并与已存 hash 比较，相同才返回 duplicate。

### 6.5 `billing_ledger_entries`

```sql
CREATE TABLE billing_ledger_entries (
    entry_id          TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES billing_accounts(account_id),
    event_id          TEXT REFERENCES ai_usage_events(event_id),
    kind              TEXT NOT NULL CHECK (kind IN
                      ('grant','topup','usage_debit','refund','manual_adjustment','expiry')),
    amount_nano_cny   BIGINT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    reason            TEXT NOT NULL DEFAULT '',
    actor_user_id     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (
      (kind IN ('grant','topup','refund') AND amount_nano_cny > 0)
      OR (kind IN ('usage_debit','expiry') AND amount_nano_cny < 0)
      OR (kind = 'manual_adjustment' AND amount_nano_cny <> 0)
    )
);
```

规则：

- 充值/赠送/退款为正，消费/过期为负，人工调整不得为 0；这些符号语义由数据库 `CHECK` 强制；
- usage debit 的 idempotency key 固定为 `usage:<event_id>`；
- 人工调整必须提供非空 `reason` 和调用方生成的 `idempotency_key`（PR5 修订：服务端**不再代生成**，缺失/空白一律 400 `invalid_request`——代生成会让「服务端已入账 + 浏览器超时 + 重试」以新 key 产出第二笔账；调用方 UI 在一次逻辑提交的全部重试中复用同一 key）；
- owner 不能编辑或删除已有 ledger entry；冲正必须追加新 entry；
- `event_id` 对 `kind='usage_debit'` 只允许一条，可用部分唯一索引保证；
- 写入 ledger 与 audit event 必须同一 PostgreSQL 事务提交。

### 6.6 `provider_balance_snapshots`

```sql
CREATE TABLE provider_balance_snapshots (
    snapshot_id            TEXT PRIMARY KEY,
    provider               TEXT NOT NULL,
    currency               TEXT NOT NULL,
    total_balance_nano     BIGINT NOT NULL,
    granted_balance_nano   BIGINT NOT NULL,
    topped_up_balance_nano BIGINT NOT NULL,
    is_available           BOOLEAN NOT NULL,
    observed_at            TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

PathTogether 使用已加密保存的官方 API key 定时读取 `/user/balance`。响应只入余额数字，API key 不进日志。余额快照用于告警与成本对账，不生成用户 ledger entry。

DeepSeek balance 响应中的金额是十进制字符串。解析必须使用 Python `Decimal`，验证币种和最多 9 位小数后精确换算为 nano-CNY；禁止先转 `float` 再乘比例。无法精确解析时不写伪造的零余额，只记录抓取失败指标和无敏感信息的错误类别。

## 7. HistoPilot 用量采集与 durable outbox

### 7.1 Usage 映射

当前 `pi-ai` OpenAI-completions 映射语义是：

- `usage.input` = prompt 中扣除 `cacheRead`/`cacheWrite` 后的普通输入；对 DeepSeek 即缓存未命中输入；
- `usage.cacheRead` = DeepSeek `prompt_cache_hit_tokens`；
- `usage.output` = `completion_tokens`，已经包含 reasoning；
- `usage.reasoning` = `completion_tokens_details.reasoning_tokens`；
- `cacheWrite` 不参与 DeepSeek 官方价格。

因此 `RequestMetrics.input_tokens` 应在兼容期保留但标记 deprecated，并新增语义明确的字段：

```ts
cache_miss_input_tokens: number | "unknown";
cache_hit_input_tokens: number | "unknown";
output_tokens: number | "unknown";
reasoning_tokens: number | "unknown";
```

不要把 `prompt_tokens` 与 `cacheRead` 再次相加。provider 返回的 `prompt_tokens` 如能取得，只用于一致性校验。

DeepSeek 流式响应的 usage 位于**最后一个正常数据 chunk**，不是额外的 usage-only chunk。实现必须在消费最终 chunk 时保留 usage，再进入完成/工具调用分支；不能等待一个并不存在的后续 chunk。当前 `publishMetricsOnce` 已有一次性保护，应复用这条终态闸门；但现有 `emitRequestMetrics` 会把 usage 收窄成旧字段，必须与新的 metrics builder 一起扩展，否则 provider 已返回的 output/reasoning 仍会在 sink 前丢失。

### 7.2 每次 model call 的身份

在真正创建 provider stream 之前生成：

```text
call_id  = call_<128-bit random>
event_id = use_<128-bit random>
```

事件绑定：

- `request_id`：PathTogether 用户动作幂等键；
- `session_id`：HistoPilot session；
- `principal_user_id`：PathTogether 签名 run context / session owner；
- `subject_type`/`subject_id`：owner、user 或 demo capability；
- `provider`/`model`：本次实际生效配置，不能使用 UI 提交但未生效的值。

DeepSeek `user_id` 只用于内容安全、KV cache 和调度隔离，不是平台计费权威。计费主体必须来自 PathTogether 签名上下文。

主体绑定不信任事件 body 中的 `subject_type`、`subject_id` 或 `user_id`。PathTogether 按以下顺序解析权威主体：

1. 正式 owner/user 调用：用事件 `request_id` 查 `ai_budget_reservations`，要求其 `state='consumed'` 且 `histopilot_session_id` 与事件 `session_id` 一致，以 reservation 中的 `subject_type/subject_id` 为准；
2. Demo 调用：用 `session_id` 查有效的 `demo_sessions.histopilot_session_id`，从该 capability 记录恢复 demo subject；
3. 若该 session 存在已绑定的 `run_grants`，再用 `session_id`、installation 和 `created_by_user_id` 做交叉校验；run grant 只覆盖需要写能力的 run，不能作为只读调用唯一的主体来源；
4. body 中的主体字段只能作为 assertion，与权威解析结果不一致时返回确定性 409 `usage_subject_conflict`，进入 dead/P0 告警；绑定行尚未提交时返回可重试 409 `usage_subject_not_ready`，不能先按 body 入账。

Demo subject **只计量、不开户、不写 ledger**。它可以进入 `ai_usage_events`、provider cost、统计和既有 24 小时滚动次数池，但不得自动创建 `billing_accounts`，也不存在可充值的 Demo 用户余额。

### 7.3 事件产生时机

每一次真实 provider HTTP 调用单独计事件，包括 Agent 工具循环中的后续模型调用。规则：

1. 收到带最终 usage 的正常/工具调用/长度终止响应：产生 priced candidate；
2. provider 返回错误且无 usage：不伪造零用量事件，可写诊断事件但不进账；
3. 流中断且拿不到最终 usage：产生 `unpriced` candidate，供管理员排查；
4. 瞬时错误重试后，只有实际返回 usage 的尝试生成计费事件；
5. `late_upload_settlement` 是 Files 上传观测，不是模型调用，不得进入计费；
6. 同一次 provider 调用的终态处理必须只调用一次 enqueue，测试需覆盖 abort/error 双出口。

### 7.4 Outbox 文件协议

新增 `HistoPilot/src/usage-outbox.ts`，默认目录：

```text
${HISTOPILOT_SESSIONS_DIR}/usage-outbox/
  pending/<event_id>.json
  sending/<event_id>.json
  acked/<event_id>.json
  dead/<event_id>.json
```

写入流程：

1. 在同目录写 `<event_id>.tmp`；
2. flush + fsync；
3. 原子 rename 到 `pending`；
4. 后台 worker 按文件名顺序 claim 到 `sending`；
5. POST PathTogether；
6. 200/201/幂等重复响应后 rename 到 `acked`；
7. 网络/429/5xx 指数退避并回 `pending`；
8. 确定性 schema 4xx、409 `usage_event_conflict`、409 `usage_subject_conflict` 移到 `dead` 并触发 P0 告警，不无限重试；`usage_subject_not_ready` 明确标记 `retryable=true`，按退避重试；
9. 启动恢复扫描 `sending` 并重新投递，依靠 `event_id` 幂等；
10. `acked` 按 7 天保留后清理，`dead` 不自动删除。

事件文件不得含对话文本、图片、完整 file ID、API key、登录账号或病例信息。目录权限 0700，文件 0600。

### 7.5 投递端点

新增：

```http
POST /api/plugin/v1/usage-events
Authorization: Bearer <plugin JWT>
Content-Type: application/json
Idempotency-Key: <event_id>
```

只接受已启用的 HistoPilot installation，验证 JWT、installation ID、schema version 和 PathTogether 签发的主体绑定。不得使用浏览器 owner session 调此端点。

PathTogether 单事务执行：

1. 校验 header/body `event_id` 一致，按 §7.2 解析权威主体，并计算 canonical `payload_hash`；
2. 尝试插入 `ai_usage_events`；若 `event_id` 或 `call_id` 已存在，锁定原行并比较 payload hash；
3. 新事件进行算术校验和 §4 的时钟偏差校验；
4. 查 `occurred_at` 时刻的 provider/customer price book；
5. 写价格 ID、成本和 charge；
6. 影子阶段不写 usage debit；硬计费阶段在同事务写唯一 ledger debit；Demo 永不进入 ledger；
7. 写内容无敏感数据的 ingest audit/operational log；
8. 返回 `{ok, event_id, duplicate, status, priced}`。

如果重复 event 的 payload hash 与原记录不同，返回 409 `usage_event_conflict` 并告警，绝不能以新 payload 覆盖旧账单。

## 8. Admin 插件宿主与安全模型

### 8.1 新宿主页

新增 owner-only：

```text
GET /admin
```

行为：

- `AUTH_ENABLED=true` 时必须存在真实登录 owner；否则跳 `/login?next=/admin`；
- 判断使用 `actor_identity()`，不能使用 preview 后的 effective subject；
- user/guest 返回 403 或安全重定向；
- 响应 `Cache-Control: no-store`；
- 设置严格 CSP，仅允许 self 脚本/样式和指定 iframe；
- admin 插件缺失/禁用/校验失败时显示平台生成的降级页，不影响 `/` Viewer。

### 8.2 Manifest v1.1 扩展

`plugins/manifest.schema.json` 增加可选：

```json
{
  "manifestSchemaVersion": "1.1.0",
  "id": "pathtogether-admin",
  "ui": {
    "entry": "/plugins/pathtogether-admin/ui/index.html",
    "slots": ["admin.workspace"]
  },
  "permissions": [],
  "adminPermissions": [
    "admin:overview:read",
    "admin:users:read",
    "admin:users:write",
    "admin:invites:read",
    "admin:invites:write",
    "admin:turn-budgets:read",
    "admin:turn-budgets:write",
    "admin:billing:read",
    "admin:billing:write",
    "admin:acquisition:read",
    "admin:audit:read"
  ]
}
```

`adminPermissions` 只是一项申请，不能自行建立信任。Host 必须同时满足：

- ID 位于代码级 `PRIVILEGED_ADMIN_PLUGIN_IDS`；
- production 配置中存在该 ID 的显式 SHA-256 pin；
- 实际 manifest hash 精确匹配；
- installation 已启用；
- 当前 actor 为 owner。

对 `admin.workspace` 禁止“策略文件缺失即 dev 放行”和“插件未 pin 也允许”。普通 viewer 插件的兼容行为可暂时保留，但 admin 插槽永远 fail closed。

### 8.3 资源与 iframe

不要复用公开 `/plugins/<id>/ui/*` 返回 admin HTML。新增 owner-only 资源路由：

```text
GET /admin/plugin-assets/<plugin_id>/<path>
```

只允许已校验 admin 插件目录内的 `.html/.js/.css/.png/.webp`；第一版明确拒绝 `.svg`、source map、任意下载和路径穿越，避免 SVG 脚本/外链语义扩大攻击面。路由必须按后缀返回固定 MIME，并给所有响应设置 `X-Content-Type-Options: nosniff`、`Cache-Control: no-store`；HTML 还设置严格 CSP。HTML 使用：

```html
<iframe sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
```

不添加 `allow-same-origin`、`allow-forms`、`allow-popups` 或 `allow-top-navigation`。iframe 成为 opaque origin，不能读取平台 Cookie、CSRF、父页面 DOM 或直接同源 fetch 管理 API。

由于 opaque iframe 发出的 `message` 的 `event.origin` 是字符串 `"null"`，Host **不得**用 origin 字符串鉴权。每次 iframe load 时 Host 生成新的 256-bit 随机一次性 nonce，保存确切的 `iframe.contentWindow` 引用，并通过初始化消息发送。之后只接受同时满足以下条件的消息：

- `event.source === iframe.contentWindow`；
- 消息携带当前 load 的 nonce，且协议版本匹配；
- request ID 在本次 load 内唯一、method 位于固定表、参数通过 schema；
- manifest permission 和当前真实 owner actor 仍有效。

iframe reload、插件切换、owner 登出或 pin 变化时立即作废旧 nonce 和全部在途请求。nonce 不放 URL、不写 DOM dataset/localStorage、不作为服务端鉴权凭据；初始化向 opaque origin 发送时可使用 `targetOrigin="*"`，安全边界来自精确 WindowProxy + 高熵 nonce + 服务端 owner/CSRF 复核。

### 8.4 AdminBridge

父页面实现 `static/admin-host.js`，使用 `postMessage` 提供固定方法表：

```text
admin.auth.get
admin.overview.get
admin.users.list
admin.users.create
admin.users.setEnabled
admin.users.setAiAccess
admin.users.resetPassword
admin.users.startPreview
admin.invites.list/create/revoke
admin.turnBudgets.get/update/newPeriod
admin.billing.account.get
admin.billing.account.updateCaps
admin.billing.adjust
admin.billing.usage.list
admin.billing.ledger.list
admin.billing.providerBalance.get
admin.acquisition.summary/list
admin.audit.list
admin.plugins.list/setEnabled/rotateSecret
```

Host 校验 plugin ID、协商版本、request ID、method、参数 schema、manifest 申请权限和当前 actor。Host 使用现有 `apiFetch`/CSRF 逻辑请求 PathTogether API；不向 iframe返回 CSRF token、session 内容或通用 fetch 能力。

所有写方法重新在服务端执行 `_require_owner()`，Bridge 权限不能代替服务端授权。

Bridge 方法到 manifest permission 的映射是代码级常量，未知方法或缺权限一律拒绝：

| Bridge 方法 | 所需 `adminPermission` |
|---|---|
| `admin.auth.get`、`admin.overview.get` | `admin:overview:read` |
| `admin.users.list` | `admin:users:read` |
| `admin.users.create/setEnabled/setAiAccess/resetPassword` | `admin:users:write` |
| `admin.invites.list` | `admin:invites:read` |
| `admin.invites.create/revoke` | `admin:invites:write` |
| `admin.turnBudgets.get` | `admin:turn-budgets:read` |
| `admin.turnBudgets.update/newPeriod` | `admin:turn-budgets:write` |
| `admin.billing.account.get/usage.list/ledger.list/providerBalance.get` | `admin:billing:read` |
| `admin.billing.account.updateCaps/adjust` | `admin:billing:write` |
| `admin.acquisition.summary/list` | `admin:acquisition:read` |
| `admin.audit.list` | `admin:audit:read` |
| `admin.users.startPreview`（PR5 修订：§10.2 身份预览入口；POST /api/admin/preview/start） | `admin:users:write` |
| `admin.plugins.list`（PR5 修订：插件列表+健康；GET /api/admin/plugins） | `admin:plugins:read` |
| `admin.plugins.setEnabled/rotateSecret`（PR5 修订：启停+凭证轮换，新 secret 仅一次透传；运行时 `/install` 不上桥——发布走 §16 版本化 releases） | `admin:plugins:write` |

## 9. Admin API v1

新插件使用分页、可版本化的新 API；旧 `/api/admin/users`、`/api/admin/settings/ai-budget` 等在迁移期保留：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/v1/overview` | 用户、用量、余额、outbox/unpriced 摘要 |
| GET | `/api/admin/v1/users` | cursor 分页、搜索、状态/来源筛选 |
| POST | `/api/admin/v1/users` | 创建普通用户 |
| POST | `/api/admin/v1/users/<id>/enable` | 启用 |
| POST | `/api/admin/v1/users/<id>/disable` | 禁用并推进 auth_version |
| POST | `/api/admin/v1/users/<id>/ai-access` | 设置平台 AI 权限 |
| POST | `/api/admin/v1/users/<id>/password-reset` | 重置普通用户密码 |
| GET/POST | `/api/admin/v1/invites` | 邀请列表/创建 |
| POST | `/api/admin/v1/invites/<id>/revoke` | 撤销邀请 |
| GET/PUT | `/api/admin/v1/turn-budgets` | 现有 turn 预算读取/更新 |
| POST | `/api/admin/v1/turn-budgets/new-period` | 开启新 turn 周期 |
| GET | `/api/admin/v1/billing/accounts/<user_id>` | 账户、余额和 spend cap |
| PUT | `/api/admin/v1/billing/accounts/<user_id>/caps` | 更新 soft/hard spend cap（version/CAS） |
| POST | `/api/admin/v1/billing/adjustments` | 赠送、充值、退款、人工调整 |
| GET | `/api/admin/v1/billing/usage-events` | 用量明细与 unpriced 队列 |
| GET | `/api/admin/v1/billing/ledger` | 不可变账本 |
| GET | `/api/admin/v1/billing/provider-balance` | DeepSeek 余额快照 |
| GET | `/api/admin/v1/acquisition/summary` | 来源漏斗汇总 |
| GET | `/api/admin/v1/acquisition/users` | 用户来源明细 |
| GET | `/api/admin/v1/audit` | 审计分页 |

公共返回不得包含 `password_hash`、`ai_config.api_key`、完整邀请 token/hash、完整 IP、原始 referrer query、outbox 文件路径或 provider credential fingerprint。

所有列表使用 cursor/limit，禁止一次返回全量 usage/ledger。导出功能不进入第一版。

caps 更新规则：`null` 表示清除该上限；非空值必须是非负 nano-CNY，且两者同时存在时 `soft <= hard`。请求携带当前 `billing_accounts.version`，服务端用 CAS 更新并递增 version；冲突返回 409，不做 last-write-wins。更新和 audit 必须同事务。Phase A 不为 Demo 建 account；普通用户的 account 在首次 grant/topup 或启用受控 debit 时显式创建，读取尚未开户用户返回 `account: null`，不得伪造 0 余额账户。

## 10. Admin 插件页面信息架构

### 10.1 概览

- active/disabled 用户数、AI access 用户数；
- 今日/本周期 model calls；
- cache hit/miss input、output、cache hit ratio；
- provider cost、用户 charge；
- DeepSeek 总余额和最近快照时间；
- unpriced 事件、重复冲突、usage ingestion lag；
- turn budget 使用情况。

页面必须同时标注“对话额度”和“金额余额”，不能只显示一个模糊的“额度”。

### 10.2 用户

每行显示：

- display name、login ID、role、enabled/disabled；
- `ai_access`；
- 创建时间、注册方式和 campaign；
- turn 使用/上限；
- 金额余额/soft cap/hard cap；
- 最近一次 AI 调用时间。

操作：身份预览、启停、AI access、重置普通用户密码、打开账本。owner 自身的禁用和管理员重置继续禁止，沿用现有 break-glass 不变量。

### 10.3 邀请与来源

- 注册模式；
- 创建/撤销邀请；
- 邀请绑定 login ID 掩码、AI access、cohort、campaign/source；
- campaign → 访问 → 注册 → 首次 AI 的转化漏斗；
- first touch 与 last touch 分开显示。

### 10.4 额度与账单

- turn/step 预算独立卡片；
- billing account、余额、消费上限；
- 人工调整表单必须输入原因；
- usage event 明细按模型、用户、时间、状态筛选；
- ledger 只读，冲正用新操作，不提供编辑/删除按钮；
- unpriced 事件单独告警，不混入“0 元调用”。

### 10.5 审计

至少展示 actor、action、target、时间、reason、idempotency key 后缀和金额变化。密码、token、API key、完整 IP 永不展示。

## 11. 用户来源归因

### 11.1 跳转入口

mywebpage 产品 CTA 改为：

```text
https://pt.solarise94.fun/r/mywebpage?campaign=<slug>&utm_medium=<...>
```

PathTogether `GET /r/<source_code>`：

1. 校验 source/campaign slug，只允许 `[a-z0-9_-]` 和长度上限；
2. 生成或读取随机匿名 `visitor_id`；
3. 保存 sanitized UTM、landing target、referrer domain；
4. 设置签名、HttpOnly、Secure、SameSite=Lax 的 `pt_acq` cookie；
5. 302 到固定 allowlist 中的 `/demo`、`/register` 或 `/`；
6. 禁止接受任意外部 redirect URL。

邀请码仍只在注册 POST body 中传递，绝不能放进 CTA URL、query、日志或 referrer。

### 11.2 数据表

```sql
CREATE TABLE acquisition_campaigns (
    campaign_id TEXT PRIMARY KEY,
    source_code TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active','paused','archived')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT
);

CREATE TABLE acquisition_visits (
    acquisition_id TEXT PRIMARY KEY,
    visitor_id_hash TEXT NOT NULL,
    source_code     TEXT NOT NULL,
    campaign_id    TEXT REFERENCES acquisition_campaigns,
    referrer_domain TEXT NOT NULL DEFAULT '',
    landing_path    TEXT NOT NULL DEFAULT '',
    utm_source      TEXT NOT NULL DEFAULT '',
    utm_medium      TEXT NOT NULL DEFAULT '',
    utm_campaign    TEXT NOT NULL DEFAULT '',
    ip_prefix_hash  TEXT NOT NULL DEFAULT '',
    touched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_acquisition_visits_visitor_time
    ON acquisition_visits (visitor_id_hash, touched_at);

CREATE TABLE user_acquisition (
    user_id              TEXT PRIMARY KEY REFERENCES users(user_id),
    first_acquisition_id TEXT REFERENCES acquisition_visits,
    last_acquisition_id  TEXT REFERENCES acquisition_visits,
    invite_id            TEXT REFERENCES registration_invites(invite_id),
    source_code          TEXT NOT NULL DEFAULT 'unknown',
    campaign_id          TEXT REFERENCES acquisition_campaigns,
    attributed_at        TIMESTAMPTZ NOT NULL,
    attribution_method   TEXT NOT NULL
);
```

`acquisition_visits` 的行粒度固定为“每次 `/r/<source_code>` 跳转一个不可变触点事件”，不得按 visitor 做 upsert 或用同一行累计 `first_seen_at/last_seen_at`。注册时在有效期内按 `touched_at, acquisition_id` 稳定选出 first/last event，因此同一访客跨 source/campaign 的 first-touch 与 last-touch 不会被折叠。若未来流量量级要求聚合，只能新增可重建 projection，不能改变原始事件粒度。

`registration_invites` 增加 `source_code`、`campaign_id`。优先级：

```text
邀请码显式 campaign
  > 有效 pt_acq first-party 触点
  > sanitized referrer/UTM
  > direct/unknown
```

注册兑换、用户插入、invite 消费和 `user_acquisition` 插入必须在同一 PostgreSQL 事务内完成。不得事后按 IP、login ID 或邮箱模糊匹配用户。

### 11.3 隐私与保留

- 不保存完整 IP，只保存带轮换盐的 IP 前缀 hash，且仅作反滥用/粗粒度统计；
- referrer 只保留 hostname，不保存 query；
- UTM 每字段限制长度并清理控制字符；
- 匿名 visit 默认 90 天清理；用户归因长期保留 campaign/source，但不复制原始 IP/referrer。PR5 修订（落地语义）：到期触点按引用关系分流——未被 `user_acquisition` 引用的行直接删除；被 first/last_acquisition_id 引用的行保留骨架（acquisition_id/source_code/campaign_id/touched_at），但把 `ip_prefix_hash`、`referrer_domain`、`utm_source`、`utm_medium`、`utm_campaign`、`landing_path`、`visitor_id_hash`（匿名联结键，过期后不再参与归因，一并置空以免延长可重识别窗口）全部置空串。两类处理由 `acquisition_store.run_visit_retention()` 单事务完成、幂等可重叠调度；
- 每日 retention 调度（PR5 修订）：PathTogether 启动后台 daemon 线程（env `ACQ_RETENTION_INTERVAL_SECONDS`，默认 86400；`<=0` 关闭；仅 PG backend 启动；循环内异常记日志不杀线程），定期执行上述删除+脱敏；多实例重叠安全（幂等），无需 advisory lock；
- 后台小样本来源统计应避免暴露可重新识别的单人组合。

## 12. 影子计量、软额度和硬额度

### 12.1 Phase A：影子计量

- usage event 正常入库和计价；
- 不写 `usage_debit`；
- admin 显示“影子成本/影子应扣”；
- 每日记录 DeepSeek balance snapshot；
- 观察至少 7–14 天；
- 对 provider 总余额变化、top-up/grant 变化和内部 provider cost 做人工解释，不宣称能从 balance API 精确拆出每个调用。

### 12.2 Phase B：软额度

- ledger 开始接受 grant/topup/manual adjustment；
- usage debit 可以在测试账户启用；
- 余额低于 soft cap 时告警；
- 超额只阻止新用户动作，不中断已开始的 Agent run；
- owner 可使用保留策略；Demo 继续使用独立的滚动次数/并发限制，只展示影子 provider cost，不创建金额账户或扣账。

### 12.3 Phase C：逐 model call 硬额度

硬额度不能只在用户动作开始时预占，因为 Agent 会发出多个 provider calls。HistoPilot 每次调用前请求：

```http
POST /api/plugin/v1/billing/holds
```

请求携带 call_id、主体、model、已知输入规模和最大输出 token。PathTogether 按最坏价格计算 hold；不足则在发模型请求前拒绝。结束后：

```http
POST /api/plugin/v1/billing/holds/<hold_id>/settle
```

同事务写 usage、实际 debit、释放差额。超时/崩溃 hold 惰性回收。0018/0019 阶段不实现该路径，避免在影子数据尚未证明可靠前制造误拒绝。

## 13. 实现批次与文件清单

### PR0：契约与测试夹具

PathTogether：

- 新增本方案文档；
- 增加 usage event JSON schema/fixture；
- 在 Plugin Contract 文档中登记 usage ingestion；
- 固定 DeepSeek price snapshot 测试夹具和北京时间边界用例。

HistoPilot：

- 固定 `Usage` 映射 fixture；
- 为 normal/tool/length/abort/retry 建 model-call usage fixture。

停止条件：usage 字段语义或 provider 最终流 usage 无法稳定取得时，不进入 PR1。

### PR1：HistoPilot durable usage outbox

预计修改：

- `HistoPilot/src/metrics.ts`；
- `HistoPilot/src/agent-runner.ts`；
- 新增 `HistoPilot/src/usage-event.ts`；
- 新增 `HistoPilot/src/usage-outbox.ts`；
- 修改现有 `HistoPilot/src/platform/http-client.ts`：在 `PathTogetherHttpClient` 上新增公开的 `postUsageEvent`；复用内部私有 `request/requestRaw` 的 scoped JWT、single-flight refresh 和仅一次 `401 token_expired` replay 语义，不另建不存在的 `plugin-client.ts`；
- 扩展 `emitRequestMetrics`/metrics builder，保留最终 output、reasoning、provider response ID 和 call identity；复用已有 `publishMetricsOnce` 终态闸门；
- 若新增 integration test 文件，必须同步加入 `HistoPilot/vitest.config.ts` 的显式 `INTEGRATION_FILES` 白名单，否则 `npm run test:integration` 不会运行它；
- 新增 unit/integration tests。

验收：崩溃恢复不丢事件；重复投递 event_id 不变；一次 provider call 只生成一个终态事件。

### PR2：PathTogether billing 存储与 ingestion

预计修改：

- `migrations/0018_billing.sql`；
- 新增 `billing_store.py`；
- 新增 `billing_pricing.py`；
- `app.py` 增加 `/api/plugin/v1/usage-events`；
- `platform_features.py` 增加 PG-only feature；
- 新增 `tests/test_billing_store.py`；
- 新增 `tests/test_usage_ingest.py`；
- 扩展 `tests/test_plugin_v1_transport.py`、`tests/test_audit_events.py`。

此 PR 只影子计价，不扣账本。

### PR3：Admin host 和 admin 插件只读版

PR3 的**硬前置**是先把 homePC 的插件发布从“普通目录直接覆盖”升级为 §16 的版本化 staging + 原子切换，并把 PathTogether 插件根改成容器内只读。若 HistoPilot bundle 仍从同一插件根发布，也必须迁入同一 releases 机制；前置未通过时不得声称 admin bundle 可独立安全回滚。

预计修改：

- `plugins/manifest.schema.json`、`plugins/sdk/manifest.py`；
- `app.py` 增加 `/admin` 和 owner-only asset route；
- 新增 `templates/admin_host.html`；
- 新增 `static/admin-host.js`；
- 新增 `plugins/pathtogether-admin/manifest.json`；
- 新增 `plugins/pathtogether-admin/ui/index.html`、`main.js`、`style.css`；
- 新增 `tests/test_admin_plugin.py` 和 JS bridge tests。

只读页先覆盖 overview、users、usage、provider balance。原 Viewer 侧栏管理 UI 暂不删除。

### PR4：来源归因

预计修改：

- `migrations/0019_acquisition.sql`；
- 新增 `acquisition_store.py`；
- 修改 `registration_store.py` 原子绑定来源；
- `app.py` 增加 `/r/<source_code>` 和 admin acquisition API；
- admin 插件增加来源/邀请页；
- mywebpage 只修改产品 CTA，不增加用户管理逻辑；
- 新增来源、开放重定向、Cookie、隐私与事务测试。

### PR5：额度写操作与 UI 迁移

- admin 插件接管用户、邀请、turn budget、billing adjustment 和 audit；
- billing adjustment 与 audit 同事务；
- 完成 UI parity 后，删除 `_app_shell.html` 中 users/plugins/AI budget 管理块及 `app.js` 对应代码；
- `/admin/registration` 保留一个版本的重定向兼容，再删除独立模板；
- Viewer 在 admin 插件缺失时仍完整可用。

### PR6：受控软扣费

- 指定测试账户启用 `usage_debit`；
- 观察余额、幂等、unpriced 和退款流程；
- 未达到 §14 门槛不得全量开启。

### PR7：硬额度（独立立项）

- `migrations/0020_billing_holds.sql`；
- per-call authorize/settle；
- crash/TTL/余额并发测试；
- 不与前述 PR 合并发布。

## 14. 测试与上线门槛

### 14.1 必测矩阵

| 类别 | 必须覆盖 |
|---|---|
| token 算术 | hit+miss=prompt；prompt+output=total；reasoning<=output |
| 价格时段 | 北京时间工作日 08:59/09:00、11:59/12:00、13:59/14:00、17:59/18:00、周末 |
| 事件时间 | 正常 outbox 延迟、未来偏差 >5 分钟、超 30 天、不得静默换成 received_at |
| 价格版本 | 调价前后事件固定到各自 price book，历史不重算 |
| 幂等 | 同 event 重放不重复扣；同 event 不同 payload 409 |
| 主体绑定 | reservation/session、demo session、可选 run grant 交叉校验；未就绪重试、冲突进 dead |
| outbox | enqueue 后崩溃、sending 后崩溃、429/5xx、确定性 4xx、409 payload/subject 冲突、重启恢复 |
| 模型调用 | 普通回答、tool call、多步 Agent、length、cancel、stream 中断、瞬时重试 |
| 账本 | 符号 CHECK；人工调整必填 reason；并发 debit 不透支；冲正只追加；Demo 永无 account/debit |
| 权限 | 匿名/user/preview subject 均不能访问 admin；真实 actor owner 可访问 |
| 插件 | 未 pin、hash mismatch、disabled、路径穿越、SVG/MIME sniff、未知 bridge method 全部拒绝 |
| AdminBridge | source WindowProxy、每 load nonce、重放 request ID、reload/logout 作废、permission 映射 |
| CSRF | 所有 browser write API 无 token 拒绝；iframe 永远拿不到 token |
| 消费上限 | soft/hard cap version CAS、非法大小关系、并发更新和服务端 owner 复核 |
| 来源 | first/last touch、invite campaign 优先级、过期 cookie、恶意 UTM、开放重定向 |
| 隐私 | API/日志/审计不出现密码、key、token、完整 IP、prompt 或图片 |
| 降级 | admin 插件缺失/禁用时 Viewer、登录、AI、分享不受影响 |

### 14.2 测试命令

PathTogether 聚焦测试：

```bash
python3 -m pytest \
  tests/test_billing_store.py \
  tests/test_usage_ingest.py \
  tests/test_admin_plugin.py \
  tests/test_plugin_v1_transport.py \
  tests/test_account_auth.py \
  tests/test_registration_invites.py \
  tests/test_audit_events.py -q

RUN_PG_TESTS=1 python3 -m pytest \
  tests/test_billing_store.py \
  tests/test_usage_ingest.py \
  tests/test_admin_plugin.py \
  tests/test_registration_invites.py -q

npm run test:js
python3 -m pytest tests -q
```

HistoPilot：

```bash
npm run build
npm run test:unit
npm run test:integration
npm run test:contract
npm test
```

新增测试文件尚不存在；命令是实现完成后的验收目标，不是当前已通过记录。

### 14.3 开启软扣费前的量化门槛

连续 7–14 天观察窗内必须满足：

- durable outbox 无确认丢失；
- usage outbox、插件 releases、HistoPilot config 与 PostgreSQL 均进入有恢复演练的备份范围；
- 重复 event 导致的重复 ledger debit = 0；
- 有最终 usage 的事件计价成功率 >= 99.9%；
- `unpriced` 均有可解释原因和处理队列，不得被显示为 0 元；
- provider/model/价格时段边界测试全部通过；
- DeepSeek balance snapshot 连续可用，余额下降与内部 provider cost 的差异能由充值、赠送余额变化、缺 usage 或时间窗口解释；
- admin 插件权限、pin、CSRF、preview actor/subject 测试全部通过；
- PostgreSQL 备份与迁移回滚演练完成。

任一门槛不满足，保持影子计量。

### 14.4 开启硬额度前的额外门槛

- per-call hold 在并发调用下无透支；
- HistoPilot 崩溃后 hold 可回收，已结算事件不会二次释放；
- hold 估算不会把 cache hit 按 miss 重复结算；
- 余额不足只阻止下一次 model call，不破坏已持久化 session；
- owner break-glass 和平台保留额度经过演练；
- 能一键关闭 hard enforcement 并回到软额度，但不删除账本或 usage 数据。

## 15. 观测与告警

至少输出以下不含敏感内容的指标：

```text
usage_outbox_pending
usage_outbox_oldest_age_seconds
usage_outbox_dead_total
usage_ingest_total{status=inserted|duplicate|conflict|unpriced}
usage_pricing_failures_total{reason}
billing_debit_total_nano_cny
billing_negative_available_accounts
provider_balance_nano_cny
provider_balance_snapshot_age_seconds
admin_bridge_denied_total{reason}
acquisition_visits_total{source_code}
acquisition_conversions_total{source_code}
```

告警优先级：

- P0：重复扣费、账本不平、负余额越过 hard cap、事件 payload 冲突；
- P1：outbox 最老事件 >15 分钟、dead event、provider 余额不可用/低余额；
- P2：unpriced 比例升高、来源触点异常激增、admin bridge 拒绝激增。

## 16. homePC 发布与回滚

### 16.1 发布顺序

当前部署尚不具备 releases/symlink 和完整备份，所以先完成“发布底座”，再部署功能：

1. 新增可审阅的 homePC 发布脚本与 runbook，脚本只接受版本化 bundle 目录，不接受对 live 目录原地 rsync；所有远程命令使用 `ssh homePC` alias，不硬编码公网或 LAN IP；
2. 把 HistoPilot `/data/config` 从匿名 volume 迁入具名 `histopilot-config` volume：先停写、复制、校验文件数/hash/权限，再以新卷启动并健康检查；旧匿名卷在至少一次恢复演练和保留期结束前不删除；
3. 建立专用备份：PostgreSQL 逻辑备份、`plugins/releases`、`sidecar-sessions/usage-outbox`、具名 HistoPilot config，以及必要的非敏感部署配置。备份目录 0700、文件 0600；含 credential 的 config 备份必须加密并设置保留期。现有 `project-sync-backup.timer` 不算本项目备份；
4. 做一次隔离目录恢复演练，验证数据库可还原、pending/dead 文件不丢、plugin manifest hash 一致、config 权限保持；
5. 建立版本化 `plugins/releases`，把现有 `histopilot` 普通目录先迁成一个不可变 release，再原子切换入口；PathTogether 的 admin bundle 使用相同机制；
6. 重建 `pathtogether-demo` 时将宿主机插件根以 `:ro` 挂载到 `/data/plugins`；发布者只在宿主机 releases 写入，容器内不得修改 bundle；
7. 校验 rootless `Linger=yes`，并把容器启动/重启策略、备份 timer 和健康探测写入 runbook；执行重启演练，不能只依赖当前交互 shell 中的容器；
8. 运行发布前置检查：PG backup 新鲜度、outbox/config backup 新鲜度、可用磁盘、目标 release 不存在、manifest/schema/hash、symlink target 位于插件根内、容器 mount 为 RO；任一失败即停止；
9. 再部署向后兼容的 PathTogether migrations/API；部署 HistoPilot outbox但保持 shadow ingest；
10. 观察 usage 入库后部署 admin bundle，更新 production 显式 pin，并原子切换 `/data/plugins/pathtogether-admin`；
11. 以 owner/user/匿名三种身份验证 `/admin`，再执行重启后复验；
12. 最后才修改 mywebpage CTA；达到门槛后另行显式开启软扣费。

admin bundle 更新不得直接覆盖正在服务的目录。推荐：

```text
plugins/releases/pathtogether-admin-<version>/...
plugins/pathtogether-admin -> releases/pathtogether-admin-<version>
plugins/releases/histopilot-<version>/...
plugins/histopilot -> releases/histopilot-<version>
```

通过同文件系统原子 symlink 切换或目录 rename 发布。若使用 symlink，插件加载器必须 `resolve()` 后验证目标仍位于受信任插件根内，再计算实际 manifest/bundle hash；不得允许链接逃逸。PathTogether 容器仅只读挂载插件根。发布脚本至少提供 `preflight`、`stage`、`switch`、`verify`、`rollback` 五个明确阶段，并把版本、hash、旧/新 target 和验证结果写入不含密钥的发布记录。

`docs/demo-deployment.md` 的地址字段应在发布底座 PR 中拆成“SSH alias（权威连接方式）/当前解析端点/可选 LAN 地址”三个字段并更新核查日期；不能仅把历史 `192.168.3.223` 文本替换成 `117.72.24.99:52044`，因为它们并非同一种地址语义。

### 16.2 回滚

- Admin UI 故障：关闭 admin plugin flag 或切回上一 bundle；Viewer 不回滚；
- HistoPilot 投递故障：暂停 sender，pending 文件保留，模型功能可按既定策略继续；
- 计价故障：关闭 debit，仅保留 raw usage/unpriced；不得删除事件；
- 来源故障：`/r/*` 降级到安全固定 302，不影响注册；
- 数据库迁移采用 forward-fix；旧代码在新增 nullable/独立表存在时仍可运行；
- 禁止用 `git reset --hard`、TRUNCATE billing 表或直接修改 ledger 作为回滚。

## 17. 明确不做

本阶段不做：

- 在 mywebpage 后台管理 PathTogether 用户或余额；
- 公开注册；
- 支付网关、发票、税务或真实货币退款；
- 多租户组织账单、团队共享钱包；
- 根据 IP、邮箱或用户名推断用户来源；
- 把 DeepSeek 总余额当作用户余额；
- 让 admin 插件直接访问数据库或持有 service token；
- 在 usage 缺失时根据字符数、请求体字节或本地 tokenizer 猜账；
- 在影子计量稳定前实现硬额度；
- 修改 HistoPilot-DSH 协议。

## 18. 完成定义

只有同时满足以下条件，才能称本方案“已实现”：

1. 0018/0019 migrations 在真实 PostgreSQL 通过，备份和 forward-fix 演练完成；
2. HistoPilot 每次 model call 能生成可恢复、可幂等投递的 usage event；
3. PathTogether 能按版本化 DeepSeek 价格计算 hit/miss/output 成本；
4. ledger 重放、并发和冲正测试证明不会重复扣费或静默改账；
5. `/admin` 只允许真实 owner actor，admin bundle 强制 pin 且运行在 opaque iframe；
6. 用户、邀请、turn 额度、金额额度、用量、来源和审计均可在 admin 插件查看；
7. mywebpage 仅通过 `/r/<source_code>?campaign=<slug>` 提供来源线索，注册归因在 PathTogether 原子完成；
8. 原 Viewer 侧栏管理 UI 在 parity 后移除，Viewer 在 admin 插件缺失时仍可用；
9. 聚焦测试、全量测试、homePC 容器验证和实际浏览器验收全部通过；
10. 达到 §14 的观察门槛后，扣费仍需单独显式开启，不能随部署自动生效。

在此之前，状态只能标为“设计”“实现中”“影子计量”或“灰度”，不得标为“计费已上线”。

## 19. v0.3 修订记录（2026-08-28）

PR5 外部 review 的 5 个 P1 阻断项，根因与修法各一句话：

1. **人工调账幂等键**：服务端曾对缺省 `idempotency_key` 代生成 `adj_<hex>`，使超时重试绕过幂等去重产出第二笔账；改为调用方（插件 UI，`crypto.getRandomValues` 生成 `adj_<32hex>`，同一逻辑提交的重试复用）生成，桥层与服务端双重 required，缺失一律 400（§6.5）。
2. **计费主体解析 run grant 回退**：`_resolve_usage_subject` 曾在 reservation/demo 均未命中时用 run grant 创建者补位充当权威主体，违反 §7.2 步骤 3「run grant 不能作为只读调用唯一的主体来源」；删除该回退（grant 查询只保留交叉校验语义，且继续覆盖失效/撤销行），①② 未命中一律 409 `usage_subject_not_ready`（可重试）。
3. **来源触点 90 天保留未真正落地**：旧 `cleanup_expired_visits` 只删未被引用的过期行，被归因引用的触点连同 IP 前缀 hash/referrer/UTM/landing/visitor 联结键被永久保留，且该函数无生产调度入口；新增 `run_visit_retention()`（单事务：未引用过期行删除 + 已归因过期行七字段脱敏置空，幂等返回计数）并由 `ACQ_RETENTION_INTERVAL_SECONDS`（默认 86400、≤0 关闭、仅 PG）驱动的后台 daemon 每日执行（§11.3）。
4. **UI parity 缺口**：旧侧栏删除后「身份预览」与「插件管理」（列表/健康/启停/凭证轮换）失去 UI 入口（API 均在）；补 `admin:plugins:read/write` 权限枚举（manifest/schema/pin 三处同步）、AdminBridge 四方法（`admin.users.startPreview`、`admin.plugins.list/setEnabled/rotateSecret`）与插件 UI 用户页预览按钮 + 插件页（§8.4/§10.2；运行时 `/install` 不上 UI，发布走 §16 版本化 releases）。
5. **§7.2 步骤 3 保持原文**：核对后实现已按上述第 2 条对齐方案原文（交叉校验语义），规格无需改动，仅实现侧删除与原文冲突的回退分支及其 docstring。
