# R1 三条未计价事件生产取证清单（2026-09-19）

状态：**R1 待完成（未宣称三条已修复）**。本文档是交给有生产数据库/生产主机权限
人员的只读取证清单与判读矩阵。本轮无生产访问权限（本地 `.demo/run/db.uri` 的
Unix socket 不存在），未连接、未启动、未修改任何生产/业务数据库。

关联：`docs/service-review-fix-plan-20260919.md` §2（R1）。本轮代码级成果见 §5：
HistoPilot 已确认缺陷一处（error/aborted 终态丢弃上游可信 usage）已修复并带回归
测试；PathTogether 侧核心链路审查未发现需改代码的缺陷，新增两条定向回归测试
（缺一类价格表、价格窗口边界）。

---

## 1. 逐事件取证字段（执行要求 1）

对用户报告的**每一条**事件，按下表逐字段记录（全部脱敏；一行一事件）：

| # | 字段 | 来源（表/文件） | 脱敏要求 |
|---|------|-----------------|----------|
| 1 | event_id | `ai_usage_events.event_id` | 完整（技术幂等键，非敏感） |
| 2 | call_id | `ai_usage_events.call_id` | 完整或前 12 字符 |
| 3 | occurred_at / enqueued_at / received_at | 同行三列 | 完整时间戳（判断时钟类原因的关键） |
| 4 | provider / model | 同行两列 | 完整（价格索引键） |
| 5 | subject_type / subject_id / user_id | 同行三列 | subject_id/user_id 只留前 8 字符 + 长度 |
| 6 | status / unpriced_reason | 同行两列 | 稳定词表原值 |
| 7 | token 是否完整 | 五个 token 列是否全 NULL / 部分 NULL / 齐全；**不要**粘贴 raw_usage 全文 | 只记「齐全/部分缺/全缺」+ raw_usage.finish_reason |
| 8 | 价格表生效窗口（两类） | `billing_price_books` + `billing_rates`：按 (kind, provider, model) 列出 active/draft/retired 书的 effective_from/to | 单价数值可保留 |
| 9 | hold 状态 | `billing_holds`（按 call_id）：status、estimated/actual、reserved、enforcement_mode、denial_reason、spend_window_id | hold_id 前 12 字符 |
| 10 | outbox 投递结果 | HP 主机 `${HISTOPILOT_SESSIONS_DIR}/usage-outbox/{pending,sending,acked,dead}/` 同名 `<event_id>.json`；dead 时记录响应 code | 文件内容不外带（含 raw_usage 镜像） |
| 11 | settle outbox | `${HISTOPILOT_SESSIONS_DIR}/billing-settles/{pending,sending,dead}/` 同名 `<hold_id>.json` | 同上 |
| 12 | 是否存在 debit | `billing_ledger_entries`（按 event_id）：kind、amount、idempotency_key、metadata.simulated | 完整金额 |
| 13 | ingest audit | `audit_events` action=`usage.ingest` target_id=event_id 的 detail（status/duplicate/unpriced_reason/simulated_debit_skipped） | 原样（设计即无敏感字段） |
| 14 | 拒绝事件 | `ai_spend_denial_events`（按 call_id）：reason、estimated | 原样 |

同时记录 HP 侧运行日志单行指标：`[usage-outbox]` 的
`usage_outbox_pending / usage_outbox_dead_total`、`[billing-sim-debit]` 的
`billing_sim_debit_failed_total`、`[billing-hold]` 的 hold 指标行。

## 2. 只读取证 SQL（psql 模板；只 SELECT，禁止 UPDATE/DELETE）

```sql
-- ① 事件主记录（逐条替换 :event_id）
SELECT event_id, call_id, schema_version, request_id, session_id,
       subject_type,
       left(subject_id, 8) || '…(' || length(subject_id) || ')' AS subject_id_masked,
       case when user_id is null then null
            else left(user_id, 8) || '…' end                AS user_id_masked,
       provider, model, provider_request_id,
       occurred_at, enqueued_at, received_at,
       status, unpriced_reason,
       cache_hit_input_tokens, cache_miss_input_tokens, output_tokens,
       reasoning_tokens, total_tokens,
       (raw_usage->>'finish_reason')                        AS finish_reason,
       provider_price_book_id, charge_price_book_id,
       provider_cost_nano_cny, charge_nano_cny,
       extract(epoch from received_at - occurred_at)        AS ingest_lag_seconds
FROM ai_usage_events
WHERE event_id = :'event_id';

-- ② 同 call_id 的全部事件（重投/换 event_id 重发都会留痕）
SELECT event_id, status, unpriced_reason, payload_hash,
       occurred_at, received_at
FROM ai_usage_events WHERE call_id = :'call_id' ORDER BY occurred_at;

-- ③ hold 链
SELECT hold_id, call_id, subject_type, status, event_id,
       estimated_nano_cny, actual_nano_cny, reserved_nano_cny,
       enforcement_mode, denial_reason,
       spend_window_id, spend_total_allowance_id,
       created_at, settled_at, expires_at,
       metadata->>'provider' AS provider, metadata->>'charge_price_book_id' AS charge_book
FROM billing_holds WHERE call_id = :'call_id';

-- ④ debit / 账本（该事件是否扣过、扣了几次）
SELECT entry_id, account_id, event_id, kind, amount_nano_cny,
       idempotency_key, reason,
       metadata->>'simulated' AS simulated, created_at
FROM billing_ledger_entries WHERE event_id = :'event_id';

-- ⑤ ingest audit（重复投递方向：outbox 先到还是 settle 先到）
SELECT ts, action, detail
FROM audit_events
WHERE action = 'usage.ingest' AND target_id = :'event_id'
ORDER BY ts;

-- ⑥ 价格书覆盖（按事件 provider+model；核对两类书是否都覆盖 occurred_at）
SELECT b.kind, b.price_book_id, b.status, b.effective_from, b.effective_to,
       r.provider, r.model, r.time_band,
       r.cache_hit_nano_per_million, r.cache_miss_nano_per_million,
       r.output_nano_per_million
FROM billing_price_books b JOIN billing_rates r USING (price_book_id)
WHERE r.provider = :'provider' AND r.model = :'model'
ORDER BY b.kind, b.effective_from;

-- ⑦ 价格代际标志（cutover 边界解读用）
SELECT key, value FROM platform_settings
WHERE key IN ('pricing_v2_cutover_at', 'pricing_v3_cutover_at');

-- ⑧ 概览口径核对（周期内 unpriced 计数与按原因分布）
SELECT unpriced_reason, count(*) FROM ai_usage_events
WHERE status = 'unpriced' AND occurred_at >= :'period_start'
GROUP BY unpriced_reason ORDER BY count(*) DESC;
```

HP 主机侧（只读）：

```bash
# outbox 四态与 settle outbox 三态中该事件/hold 的滞留情况
ls -la "$HISTOPILOT_SESSIONS_DIR/usage-outbox"/{pending,sending,acked,dead}/ | head -50
ls -la "$HISTOPILOT_SESSIONS_DIR/billing-settles"/{pending,sending,dead}/ | head -30
# 只看文件名（= event_id/hold_id），不要 cat 事件全文到工单
```

## 3. 判读矩阵（执行要求 2：先分类，再下结论）

| 判定 | 证据组合（字段见 §1） | 结论口径 |
|------|----------------------|----------|
| a. 未计价（unpriced） | status=unpriced + reason ∈ 词表；token 列与价格窗口佐证具体原因 | 金额为 NULL 不是 0 元；按 §4 原因处置 |
| b. 已计价但漏扣 | status=priced、charge_nano_cny>0，但 ④ 无 usage_debit 行（或 ⑬ audit 含 `*_skipped`：user_missing/zero_charge/account_suspended/disabled/failed） | 记录 skip 词表与当时 enforcement_mode；追扣走人工调账审批，不直接改事件 |
| c. 重复投递已幂等扣费 | ② 同 call_id 多行或 ⑤ 多条 audit 且仅一条 `duplicate=false`；④ usage_debit 恰一条 | 属正常幂等，不修数 |
| d. 明确不扣费主体 | subject_type=demo（§14.1 永不开户/永不入账）或 audit `skipped=demo_subject` | 正常口径 |

unpriced_reason → 直接原因对照：

- `arithmetic_mismatch`：token 部分缺或 total≠hit+miss+out；原始值镜像在
  raw_usage.reported_tokens_v1 → 查 HP 侧映射/网关聚合（usage-mapping 夹具口径）。
- `clock_skew_future`：occurred_at > received_at+5min → 对比 HP 主机时钟与 DB 时钟。
- `occurred_at_out_of_range`：occurred_at 早于 received_at−30d（env
  `BILLING_OCCURRED_AT_MAX_AGE_DAYS` 可调）→ outbox 长时间停摆后补投的典型痕迹，
  结合 §1-10 的 pending 文件 mtime 佐证。
- `no_final_usage`：五 token 全 NULL → **注意**：若 HP 侧（raw_usage/日志/网关）
  能证明 provider 实报过 usage，则属于本轮已修复的 HistoPilot 缺陷类别（§5）；
  若 provider 确未回 usage，则保留未知状态，不补零、不追扣。
- `no_active_price_book`：两类书未同时覆盖 (provider, model, 时段, occurred_at
  生效窗口) → 用 ⑥ 核对：模型别名不一致 / provider key 不一致
  （deepseek vs cpa-gateway）/ 只建了 provider_cost 或只有 customer_charge /
  时段行缺失 / occurred_at 落在收口后无接班的区间。修复方式是**补历史有效窗口的
  价格书**（effective_from/to 回填当时窗口），绝不拿当前单价无条件覆盖历史。

## 4. 历史处置红线（执行要求 4–6）

- 不删除旧事件、不批量清零告警、不直接 UPDATE 成 priced。
- 若取证后确需修历史：仅限限定 event_id 的 dry-run → 事务 → 幂等校验 → 审计，
  与代码修复分开提交审查；简单重发相同 event 不会重新计价（dedup 按
  payload_hash 幂等返回原行），重复投递前先查 ②/⑤。
- 追扣（分类 b）走 ledger 追加（人工调账/冲正），列明原值、依据、金额、执行
  结果；ingest 事件行不可变。
- unpriced 事件如事后拿到有效价格（补价格书），也不得改写旧行；如需按历史窗口
  重计，另立工具并 dry-run 审查。

## 5. 本轮代码级结论（2026-09-19，基线 PT 7040cab / HP ecb8d47）

### 5.1 已确认代码缺陷（已修复，HP 侧）

`HistoPilot/src/agent-runner.ts` publishMetricsOnce（原 3425 行附近）：error/
aborted 终态一律把 `fm.usage` 置 null——凡 `stopReason∈{error,aborted}` 或带
errorMessage 的终态，即使 provider 已送达最终 usage（pi-ai openai-completions 的
finish_reason=content_filter/network_error 在 usage 分块之后 throw、以及 abort 恰
发生在流收尾之后两种实态），也会被丢弃并落五字段 NULL 的 `no_final_usage` 事件，
hold 走 release。这直接产生一类「有真实用量却 unpriced + 漏扣」事件，是三条
生产事件的**候选根因之一**（是否即三条事件的根因，待 §1 取证后判定）。

修复：`HistoPilot/src/usage-event.ts` 新增 `usageIsProviderReported`（全 0 占位
≠ 上游实报；任一 token 分量 >0 即采信），agent-runner 改为
`usageIsProviderReported(fm.usage) ? fm.usage : null`。效果：

- error/aborted 带上游实报 usage → 事件带真实 token 计价，settle 携带事件结算
  （不再 release）；与 /usage-events 双向投递靠服务端 payload_hash dedup +
  `usage:<event_id>` ledger 幂等键保证只扣一次（既有机制，测试锁定）。
- error/aborted/done 且 usage 为全 0 占位（本地合成、provider 未回 usage）→
  维持五字段 NULL 的 unpriced 候选；done 全 0 不再伪造 0 元已计价（补零冒充
  已计价同样是本类缺陷）。

回归测试（HP）：

- `test/usage-event.test.ts`：`usageIsProviderReported` 单测（全 0/null/各分量）。
- `test/usage-event-agent.test.ts`：终态错误带实报 usage → 事件带真实 token 且
  finish_reason=content_filter；abort 带实报 usage → 真实 token；done 全 0 占位 →
  五字段 NULL；终态错误无 usage → 五字段 NULL（既有用例，fake 修正为 pi 全 0
  占位语义）。
- `test/billing-holds-agent.test.ts`：终态错误无 usage → settle 空 body（release，
  既有）；终态错误带实报 usage → settle 携带 `{event_id}` 结算（非 release）。

### 5.2 PathTogether 侧审查结论（无代码缺陷；新增定向回归）

核对项与结论：

- `_classify_unpriced`（billing_store.py:791）：优先级 = 算术 > 未来时钟(+5min)
  > 超龄(默认 30d) > 无最终 usage > 缺有效价格表，与文档一致；算术错时 token
  列置 NULL 且 raw_usage.reported_tokens_v1 留镜像；时钟类/缺价格类保留 token。
- ingest（billing_store.py:1088 起）：计价窗口按 occurred_at（不静默改用
  received_at）；provider_cost 与 customer_charge 两类书**同时**命中才 priced，
  缺一 → no_active_price_book 且金额保持 NULL；命中的书 id 落行（可定位缺哪类）。
- 价格边界：`find_active_rate` 半开区间 [effective_from, effective_to)，按
  occurred_at 取书；supersede 收口后迟到旧事件仍按旧书计价（价格版本固定）。
- 重复投递/乱序：dedup（event_id FOR UPDATE + payload_hash 比对 + call_id 唯一）
  → 同事件两方向（/usage-events 与 settle）只计价一次；debit 幂等键
  `usage:<event_id>`（部分唯一索引）只扣一次；并发竞态由 SAVEPOINT 重读分支
  兜底。既有测试 `test_replay_same_event_single_debit`、
  `test_concurrent_delivery_single_debit`、
  `test_outbox_settle_ordering_both_directions_single_consume` 锁定。
- admin 展示（app.py:7047/7249、plugins/pathtogether-admin/ui/main.js:741/2793/
  3041/3124）：unpriced 单独过滤、金额 NULL 显示「—」不冒充 0 元、原因词表原样
  展示；未发现展示层缺陷。

新增测试（PathTogether `tests/test_billing_store.py`）：

- `test_missing_one_price_book_kind_is_unpriced_not_zero`：只有 provider_cost 或
  只有 customer_charge active → unpriced no_active_price_book，金额 NULL、token
  保留、无 debit，且缺的一类书 id 为 NULL（取证定位线索）。
- `test_price_window_boundary_exact_instant_picks_by_occurred_at`：occurred_at ==
  effective_to−1µs → 旧书旧价；== effective_to（=新书 effective_from）→ 新书新价；
  收口无接班 → unpriced（不拿区间外现价覆盖）。

### 5.3 待生产取证后才能下结论的事项

- 三条事件各属 §3 矩阵的哪一类（a/b/c/d）；本轮不假设三条同根因。
- 若属 `no_final_usage`：HP 侧网关/日志能否证明 provider 实报过 usage（决定是否
  落在 §5.1 修复的类别内，以及事件发生时 HP 版本是否已含修复）。
- 若属 `no_active_price_book`：⑥ 中别名/provider key/生效窗口/时段行缺哪一样。
- `cpa-gateway` 网关是否恒回 usage 分块：若否，done 全 0 占位历史事件会以
  「priced 0 元（zero_charge skip）」形态存在，属分类 b 的特殊形态（修复后变为
  unpriced no_final_usage，如实入账）。

## 6. 本轮测试命令与结果

| 命令 | 结果 |
|------|------|
| HP: `npx vitest run --project unit test/usage-event.test.ts test/usage-mapping-fixtures.test.ts` | 2 files / 48 tests passed |
| HP: `npx vitest run --project integration test/usage-event-agent.test.ts test/billing-holds-agent.test.ts` | 2 files / 33 tests passed |
| HP: `npm test`（全量） | 73 files / 1430 tests passed |
| HP: `npm run build` | 通过（exit 0） |
| HP↔PT: `PATHTOGETHER_PYTHON=…/.venv/bin/python npx vitest run --project contract test/billing-protocol-contract.integration.test.ts`（真实 PT 端点 + 内嵌 PG：双向投递只扣一次、乱序、release、hard deny、崩溃恢复、新旧兼容矩阵） | 14 tests passed |
| PT: `.venv/bin/python -m pytest -q tests/test_billing_store.py -k "missing_one_price_book_kind or price_window_boundary_exact"` | 2 passed |
| PT: `.venv/bin/python -m pytest -q tests/ -k "billing or hold or settle or usage"` | 180 passed；另 1 failed（`tests/test_admin_plugin.py::test_trusted_when_all_conditions_hold`，与 R1 无关，见下注） |

注：该失败源于工作树中**并行的管理 UI 改动**（ui/main.js、ui/index.html）尚未按
集成验收清单 §9.1 重算 `plugins/pathtogether-admin/manifest.json` 的 ui.fileHashes，
插件信任判定按设计 fail-closed（`_admin_plugin_trusted` hash 校验 MISMATCH）。
R1 本轮未改任何 admin 插件/产品源码文件（仅 tests/test_billing_store.py 增两用例
+ 本文档）；剔除该用例后 -k 选择集 180/180 全绿。

（未执行项：HP `test:contract` 其余三个跨仓文件与 R1 无关且各自需要额外环境，
未点名运行；PT 其余 -k 未命中文件未被本轮选择集覆盖。）
