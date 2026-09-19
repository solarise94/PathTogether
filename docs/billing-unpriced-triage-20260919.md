# R1 三条未计价事件生产取证清单（2026-09-19）

状态：**R1 取证已完成（2026-09-19 回填，结论见 §7）；处置口径已定（§7.7）：
全部 11 条异常项按「供应商异常不扣费」入账结案**。三条事件逐条有证据：
均属判读矩阵分类 a（未计价，no_final_usage），根因为「run 在 provider 调用启动
后约 20ms 内被中止，provider 未实报任何 usage」（HP 侧 pi-ai 会话转录的全 0 占位
usage 佐证），平台链路（hold→release、outbox→ingest、dedup、幂等）全部按设计
工作，**无漏扣、无需修数**。取证全程只读（SELECT / 文件名清单 / 转录数值字段
抽取），未修改任何生产数据，未复制密钥、cookie、提示词或 raw_usage 全文。

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

### 5.3 待生产取证后才能下结论的事项（2026-09-19 已取证，结论见 §7）

- 三条事件各属 §3 矩阵的哪一类（a/b/c/d）→ **均为 a**（no_final_usage），
  b/c/d 逐条排除（§7.2）。
- `no_final_usage` 的 HP 侧佐证 → **provider 未实报 usage**（会话转录全 0 占位，
  §7.3）：三条均**不**落在 §5.1 已修复缺陷的类别内。
- `no_active_price_book` → 不适用（优先级低于 no_final_usage）；⑥ 已核：两类
  价格书对 deepseek/deepseek-flash 全程覆盖（§7.4）。
- `cpa-gateway` 网关是否恒回 usage 分块 → 本次三条不经 cpa-gateway（provider 均
  为 deepseek 直连）；该问题对历史事件无未决影响（现存 11 条 unpriced 全部
  no_final_usage 且全部有转录佐证），留作后续观察项。

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

---

## 7. 生产取证结论（2026-09-19 回填；全程只读，未改任何数据）

取证通道：homePC 只读 SQL（psql SELECT，svs_demo）+ HP 主机文件清单与日志
（journald）+ pi-ai 会话转录数值字段抽取（只取 stopReason/usage/errorMessage
存在性与数值，未取任何消息正文）。账号一律脱敏（`usr_XXXX…(长度)`）。

### 7.1 范围校准：现存 unpriced 共 11 条，不止三条

`ai_usage_events` 共 2275 行，unpriced 11 行，**原因 100% 为 `no_final_usage`**
（无其他原因分布）。按时间两组：

- **09-16 三条**（subject `usr_UpOL…(15)`）——与报告账号吻合、时间最早，即 §0
  「三条」的最可能所指；
- **今日（09-19）07:56–08:22 UTC 八条**（subject `usr_0VbW…(15)`）——同一根因
  的新发同类，发生在今日部署（修复版 HP）**之前**，取证一并覆盖。

### 7.2 三条事件逐字段取证（§1 表口径）

三条公共字段：provider=`deepseek`、model=`deepseek-flash`、subject_type=`user`；
status=`unpriced`、unpriced_reason=`no_final_usage`；五个 token 列**全 NULL**；
`raw_usage` 仅两键 `finish_reason=aborted` + `provider_meta_v1{meta_version,
service,stream_state=interrupted_no_final_usage}`；金额两列 NULL（不是 0 元）。

| 字段 | 事件 1 | 事件 2 | 事件 3 |
|---|---|---|---|
| event_id | use_625e98dd7cacc2b79176b71ef91402a0 | use_7bf7a97111b1a448144ad365b4826275 | use_ccc39b88536c9f1ba10116b9c93c0ee3 |
| call_id | call_f62d0af820d705c756f94fb5bf2192df | call_0342a99d738f8c56f6ada7dcaa618d9c | call_62d7ed7a74d47962d8b91815b8ad3b45 |
| occurred_at→received_at（lag） | 09-16 09:07:56.056→09:07:57.164（1.1s） | 09:16:06.063→09:16:07.966（1.9s） | 09:18:41.371→09:18:45.582（4.2s） |
| subject | user / usr_UpOL…(15) | 同左 | 同左 |
| ② 同 call 重投 | 仅此 1 行 | 仅此 1 行 | 仅此 1 行 |
| ③ hold | hold_05cdbb5…：open→**released**，est=rsv=110820000 nano（¥0.1108），settled_at≈received_at | hold_d45b630…：released，est=rsv=43548000（¥0.0435） | hold_da83492…：released，est=rsv=49982000（¥0.0500） |
| ④ debit | 0 行 | 0 行 | 0 行 |
| ⑤ ingest audit | 1 条：duplicate=false、real_debit_skipped=unpriced | 同左 | 同左 |
| ⑩ usage outbox | acked/ | acked/ | acked/ |
| ⑪ settle outbox | 无 dead 文件（release 投递成功） | 同左 | 同左 |
| ⑭ 拒绝事件 | 无 | 无 | 无 |
| session（转录佐证） | sess_b166e60a8df74759 | sess_f00ef8e2eabf455f | sess_f00ef8e2eabf455f |

**判读（§3 矩阵）：三条均为分类 a（未计价）**。逐条排除其他类：非 b（无
priced 行、无漏扣——hold 走 release 属设计行为）；非 c（无重投、无重复 audit）；
非 d（subject_type=user 且非 demo 口径豁免情形——此处「不扣费」的依据是
「中止发生在任何 usage 产生之前」，见 §7.3）。

### 7.3 根因：run 启动即被中止，provider 从未实报 usage

决定性证据来自 HP 主机 pi-ai 会话转录（`sidecar-sessions/<sess>.json`，只抽取
数值/枚举字段）：

- 三条事件对应的终态 assistant 消息均为 `stopReason=aborted` +
  `usage={input:0, output:0, cacheRead:0, cacheWrite:0, totalTokens:0}`（**全 0
  占位**，即 `usageIsProviderReported=false` 的情形）+ `errorMessage` 以
  「Request was aborted」开头。
- 中止消息时间戳与事件 occurred_at 相差 **19–25ms**（09:07:56.056→.081、
  09:16:06.063→.082、09:18:41.371→.390）：provider 流在产出任何内容/usage 分块
  之前即被切断。
- 同会话前后的正常 toolUse 消息 usage 均为非零真实计数（转录链路本身完好，
  排除「转录丢 usage」）。

结论：**这三条不是 §5.1 已修复缺陷（abort/error 终态丢弃实报 usage）的实例**
——provider 根本没有实报过 usage，旧代码没有可丢弃的东西；即使用今日修复版
重放，`usageIsProviderReported` 对全 0 占位仍判否，事件形态完全相同。按 §3
口径保留未知状态：**不补零、不追扣、不改写**。

中止来源的产品侧观察（与计费正确性无关，不展开）：中止在调用启动后约 20ms
到达，更像客户端自动抢占/重试（快速连发新请求）而非人工停止；09-16 三条落在
11 分钟内、今日 8 条落在 27 分钟内，呈连发形态。

「API 商 429 故障」假设核查（2026-09-19 晚，应运营反馈补查）：全部 75 个会话
转录在两个事件窗口（09-16 08:00–10:30、09-19 07:00–09:30 UTC）内 **0 条 error
终态、0 条 429/限流字样**；HP 两容器日志无 429 报错行（仅指标计数器恒
`"429":0`）。pi-ai 对 HTTP 429 会落 `stopReason=error` + 429 字样的
errorMessage，与实测的 `aborted` + 「Request was aborted」是两种不同终态——
即中止确为客户端 AbortController 发起，而非 provider 直接回 429。若用户当时在
UI 上看到了 DeepSeek 故障/限流（驱动其快速重试、抢占在途调用），该情节与计费
结论相容（429 调用 vendor 不计费、平台侧无 usage 不计费，双向无账），但留存
数据中没有直接证据，不作断言。

### 7.4 支撑性核对

- ⑥ 价格书：deepseek/deepseek-flash 两类书均覆盖全部事件时点——v3
  （`pb_deepseek_*_v3_flash_repricing`，2026-09-11 起生效、无截止，peak/off_peak
  双全）。即「若这些调用正常完成，会按 v3 价格正常计价」；旁证：同账号同时段
  正常完成的调用均 priced + 恰一次 usage_debit（见 §7.5 的三条 priced 事件）。
- ⑦ cutover 标志：不适用（分类在计价之前）。
- ⑧ 概览口径：unpriced 计数与原因分布已核（11 条全 no_final_usage），与 admin
  概览一致。
- HP 单行指标：当前与上一容器 `[usage-outbox]` 均 pending=0 / dead_total=0；
  无投递积压。
- 今日 8 条（usr_0VbW…(15)）聚合核对与三条完全同型：hold 8/8 released、debit
  0、audit 8 条 duplicate=false、outbox 8/8 acked；5 个会话转录中 8 条 aborted
  消息全部全 0 占位 + 「Request (was) aborted」；其中两条连 provider_request_id
  都为 NULL（中止发生在拿到响应头之前）。同样不修数。

### 7.5 附带发现：billing-settles dead ×3（与三条无关联，金额无影响）

`billing-settles/dead/` 现存 3 个文件，属另外三次调用；HP 日志有对应 P0 行
（`[billing-settles] P0 billing settle moved to dead`）。逐条核对：

| dead 文件（hold_id） | call / 时间（UTC） | 事件与扣费（DB 实证） | hold 现状 |
|---|---|---|---|
| hold_6e6abfac01b61f9465125bd9 | call_53cd20d0… / 09-02 08:30 | use_b674f2d5… priced 9523200 nano；debit −9523200（幂等键 usage:…，恰一次） | expired（惰性回收） |
| hold_7a05313e096a53340d7d1816 | call_1fe5f46b… / 09-16 09:08 | use_2e167342… priced 2680720；debit −2680720 恰一次 | expired（惰性回收） |
| hold_027d12e9e28d4016391b81fa | call_92fa2146… / 09-19 08:22 | use_54036f3b… priced 825000；debit −825000 恰一次 | **仍 open 且已过 expires_at（08:27:27）**，rsv=29384000 nano（¥0.029）预约占用中 |

要点：

- **钱已正确**：三事件经 /usage-events 孪生链 priced 且恰一次真实扣费；dead
  只影响 hold 的 settled 收口，不影响账本。
- HP 侧死信原因是「**确定性 4xx**」（retryable=false → dead）。确切状态码已
  不可考：PT 无请求级日志（容器仅启动日志），HP 的 P0 行不含响应码，18080 即
  gunicorn 本进程（无中间代理日志）。
- **429 假设已排除**：PT 插件限流的 429 信封恒带 `retryable=true`
  （`_PLUGIN_ERROR_RETRYABLE["rate_limited"]=True`），HP 的 `settleRetryable`
  对 `retryable=true` 只退避回 pending、**永不进 dead**；且 09-19 08:22 同分钟、
  同 installation 的 /usage-events POST 成功——per-installation 令牌桶也不会
  只拦 settle 放行 usage。故 dead 必为语义性确定性 4xx（400 invalid_request /
  403 forbidden / 404 hold_not_found / 409 hold_conflict 族）。曾核查
  「abort 后重试的调用更易 dead」相关性：3 条中仅 2 条吻合（09-02 那条前后无
  abort），不作模式断言。
- 遗留风险仅一项：hold_027d12e9 的 ¥0.029 预约在该主体下次触发惰性回收
  （`_expire_stale_holds_tx`，随新 hold/settle 事务执行）前一直占用；两个
  expired 的预约已释放。
- ~~建议（均为**待批准**项，本轮未执行任何写操作）~~ **两项已于 2026-09-19
  晚执行完毕**（用户批准「都做完然后上线吧」）：
  1. ~~修复手段（设计内路径）：把 3 个 dead 文件移回 pending 让 sender 重放~~
     **已执行**（先上线带类别日志的 HP 0.3.4 再重放）：三个 hold 全部
     settled、event_id 关联、actual=已扣金额（9523200/2680720/825000 nano），
     settled_at=2026-09-19 12:32:49 UTC；每事件 debit 恰 1 行（无双扣）；
     全表 open 且过期 hold 归零（¥0.029 预约占用已释放）；三个 outbox 文件
     按设计在成功后删除。原始 4xx 未复现，拒绝码仍不可考（见第 2 项的防护）。
  2. ~~代码级小改进（另立项）：`billing-settle-outbox` 的 P0 dead 日志行补
     响应码/失败类别~~ **已上线**：HP 0.3.4（`553252e`），`BillingHoldSettleResult`
     新增 `failure_category`，P0 dead 行携带类别；回归测试断言
     `category=settle_hold_conflict`；全量 1430 测试通过。今后同类死信可直接
     定位拒绝码。

### 7.6 红线合规声明

本次取证只读：未 UPDATE/DELETE 任何行、未移动/修改任何 outbox 文件、未重启
或改动任何服务；未复制密钥/cookie/提示词/raw_usage 全文（转录仅抽取
stopReason/usage 数值与 errorMessage 前 80 字符）。三条事件保持 unpriced 原状
（金额 NULL），不补零、不清零告警、不改写为 priced。

### 7.7 处置与入账：供应商异常不扣费（运营判定，2026-09-19）

依运营指令，现存全部 11 条 unpriced 异常项**按「供应商异常不扣费」入账结案**：

- **类目**：供应商异常（API 商故障窗口）→ 不扣费。
- **入账判据**（技术佐证，与运营判定相容）：11 条事件全部无任何可计费用量
  （provider 流在产出内容/usage 前被中止，转录全 0 占位佐证），平台侧无扣费、
  vendor 侧亦无可计量消费——双向无账，不存在待追扣/待退款金额。
- **范围**（event_id 全列，避免口径漂移）：
  - 09-16（usr_UpOL…(15)）：use_625e98dd7cacc2b79176b71ef91402a0、
    use_7bf7a97111b1a448144ad365b4826275、
    use_ccc39b88536c9f1ba10116b9c93c0ee3；
  - 09-19（usr_0VbW…(15)）：use_18c3738ff8742ee8fc7b5eee1609d811、
    use_ed5e2c956e790cce5427595ead6665dc、
    use_4ad87c5e50271521372666bd286f6109、
    use_0c35f4ad7325d93aa05add969820fdd4、
    use_ada0128a252430488744ee6ec6360e96、
    use_79dc6a63872fc5bd9c5b16bec0772160、
    use_a62a8165f523b90806718959625cd342、
    use_1f28ddaefd34d78d9d8ea0d5a8583fa6。
- **记录方式**：仅本文档入账；事件行保持原状（status=unpriced、金额 NULL），
  admin 异常列表继续如实显示 unpriced/no_final_usage（不冒充已计价、不清零）——
  本节即这些条目的结案依据，后续审计以本节为准。
- 若后续出现**同形态**（aborted + 全 0 占位）新条目，可沿用本口径直接结案；
  若出现「有实报 usage 却 unpriced」的形态（§5.1 修复针对的类别），须单独取证，
  不适用本节。

