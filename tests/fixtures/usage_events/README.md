# Usage event 夹具与 canonical payload_hash 规范（PR0）

本目录是 `POST /api/plugin/v1/usage-events`（Plugin Contract §7.8；
`docs/admin-billing-plugin-implementation-plan.md` §6.4/§7.2/§7.5）的**契约夹具**：

- `schema_v1.json`：`schema_version=1` 的 request body JSON Schema（draft 2020-12），`additionalProperties:false`；
- `0*.json`：符合 schema 的样例事件，覆盖计价、中断、demo、字段缺省与幂等冲突重放场景；
- 自洽性校验见 `tests/test_usage_event_fixtures.py`（stdlib 实现，不依赖 PG / app）。

因为 schema `additionalProperties:false`，样例文件内**不能**携带 `_description` 之类的
说明键，场景说明统一记录在下文表格里。

## 样例事件清单

| 文件 | 场景 | 要点 |
|---|---|---|
| `01_owner_priced_flash_peak.json` | owner 正常 priced 调用，flash 高峰 | 北京时间周一 10:30（peak），全 token 齐、带 `provider_request_id`；canonical hash 见下文示例 |
| `02_user_priced_pro_offpeak_reasoning.json` | user 正常 priced 调用，pro 空闲 + reasoning | 北京时间周一 07:15（off_peak），`reasoning_tokens=512 <= output_tokens=1204`，finish_reason=tool_calls |
| `03_user_priced_vision_exp_peak.json` | user vision-exp 事件 | `deepseek-v4-flash-vision-exp`，图片换算输入 token 记在 `raw_usage.vision_image_tokens`；北京时间周一 15:20（peak） |
| `04_owner_interrupted_no_usage.json` | 流中断且拿不到最终 usage | 五个 token 字段显式为 `null`（不得缺省、不得用 0 冒充），`provider_request_id=null`，finish_reason=aborted |
| `05_demo_subject_offpeak.json` | demo 主体事件 | `subject_type=demo`、`request_id/user_id=null`；只计量，不开户、不写 ledger；北京时间周六 20:05（off_peak） |
| `06_user_priced_flash_no_provider_request_id.json` | 缺 `provider_request_id` | 该键整体缺省（可选字段），其余完整；北京时间周一 19:45（off_peak 晚间） |
| `07_replay_conflict_of_01.json` | 同 `event_id` 重放且 payload 不同 | 与 01 同 `event_id`/`call_id`，但 `output_tokens`/`total_tokens`/`enqueued_at` 不同 → canonical hash 不同 → 服务端必须返回 409 `usage_event_conflict`（PR2 幂等冲突用例） |

## 字段语义速查

完整定义以 `schema_v1.json` 为准。关键语义：

- `event_id`/`call_id`：投递端在创建 provider stream 前生成的 128-bit 随机 ID
  （`^use_[0-9a-f]{32}$` / `^call_[0-9a-f]{32}$`）。`event_id` 是全链路幂等键，
  `call_id` 服务端唯一；
- `occurred_at`：provider 调用发生时刻，**计价时段与时钟偏差校验的唯一时间依据**；
  `enqueued_at` 只用于判断 outbox 积压。两者都必须是带时区的 RFC3339/ISO-8601；
- 五个 token 字段（`cache_hit_input_tokens`/`cache_miss_input_tokens`/`output_tokens`/
  `reasoning_tokens`/`total_tokens`）**必须显式出现**：值为非负整数或 `null`，
  且上限 `2^53-1`（9007199254740991——同时保证 JSON number 精确可表示与
  PG BIGINT 安全，超限是确定性 schema 400，不是入库 500；v0.3 P2 修订）。
  `null` 只表示「中断且无最终 usage」的 unpriced 候选；不允许缺省与 null 混用，
  否则幂等哈希语义含糊。非 null 时必须满足
  `total = hit + miss + output` 且 `reasoning <= output`；
- `request_id`/`user_id`/`provider_request_id`：可空（null）也可整体缺省；
- `subject_type`/`subject_id`/`user_id` 只是投递端 assertion：服务端按
  admin-billing 方案 §7.2 的四步（reservation → demo capability → run grant 交叉校验
  → assertion 比对）权威解析计费主体；
- `raw_usage`：只允许 token 计数、`finish_reason` 和带 `meta_version` 的版本化
  provider 元数据。**禁止** prompt 文本、模型输出文本、图片或图片 URL、API key、
  完整请求/响应体、对话内容、病例信息。

## Canonical payload_hash（PR2 服务端实现的唯一依据）

`payload_hash` 由 PathTogether 在 schema 校验和字段规范化后自行计算
（SHA-256，64 位小写 hex），不信任客户端提交值。同一 `event_id` 重放时先重算
并与已存 hash 比较：相同 → 返回原行（duplicate）；不同 → 409 `usage_event_conflict`。

### 1. 输入字段集合（固定 18 个键，多一个少一个都算实现错误）

| # | 字段 | 处理 |
|---:|---|---|
| 1 | `cache_hit_input_tokens` | 原样；缺省 → `null` |
| 2 | `cache_miss_input_tokens` | 原样；缺省 → `null` |
| 3 | `call_id` | 原样 |
| 4 | `enqueued_at` | 时间规范化（见下） |
| 5 | `event_id` | 原样 |
| 6 | `model` | 原样 |
| 7 | `occurred_at` | 时间规范化（见下） |
| 8 | `output_tokens` | 原样；缺省 → `null` |
| 9 | `provider` | 原样 |
| 10 | `provider_request_id` | 原样；缺省 → `null` |
| 11 | `reasoning_tokens` | 原样；缺省 → `null` |
| 12 | `request_id` | 原样；缺省 → `null` |
| 13 | `schema_version` | 原样（整数保持整数） |
| 14 | `session_id` | 原样 |
| 15 | `subject_id` | 原样 |
| 16 | `subject_type` | 原样 |
| 17 | `total_tokens` | 原样；缺省 → `null` |
| 18 | `user_id` | 原样；缺省 → `null` |

**排除项**（不得进入哈希输入）：`received_at`（服务端接收时刻）、重试次数/投递
元数据（不存在于 body）、HTTP header（含 `Idempotency-Key`、Authorization）、
`raw_usage` 整体。`raw_usage` 内的 token 计数只是 provider 原始数字的镜像，
账单语义已经由顶层 token 字段唯一承载，其余子键全部是非账单诊断字段——因此
**整个 `raw_usage` 键不参与哈希**，修改 `raw_usage` 不改变 `payload_hash`。

### 2. 规范化规则

1. **固定字段集合**：只取上表 18 个键；body 中缺省的可选键补 `null`，
   不允许从输入中直接省略（保证「缺省」与「显式 null」哈希一致）；
2. **时间统一 UTC ISO-8601**：`occurred_at`/`enqueued_at` 解析后转为 UTC，
   恒定输出 `YYYY-MM-DDTHH:MM:SS.ffffffZ`（微秒固定 6 位，不足补零，丢弃更多
   小数位前必须先约定截断规则）；输入的 `Z`/`z`/`±HH:MM` 偏移在规范化后产生
   完全相同的字符串；
3. **整数保持整数**：token 数与 `schema_version` 以 JSON 整数序列化，
   不得转字符串或浮点；`null` 保持 `null`；
4. **字符串原样**：不做 trim、不折叠空白、不改大小写；
5. **UTF-8 + key 排序 + 紧凑分隔**：`json.dumps(obj, sort_keys=True,
   ensure_ascii=False, separators=(",", ":"), allow_nan=False)`，按 key 的
   Unicode 码点排序，无任何空白，非 ASCII 字符不转 `\uXXXX` 转义；
6. 哈希 = `sha256(canonical_json.encode("utf-8")).hexdigest()`（小写 hex）。

### 3. 已验证示例（fixture 01）

canonical JSON（一行，此处折行仅便于阅读）：

```json
{"cache_hit_input_tokens":1856,"cache_miss_input_tokens":2418,"call_id":"call_11223344556677889900aabbccddeeff","enqueued_at":"2026-09-07T02:30:13.120000Z","event_id":"use_0f1e2d3c4b5a69788796a5b4c3d2e1f0","model":"deepseek-v4-flash","occurred_at":"2026-09-07T02:30:12.345000Z","output_tokens":357,"provider":"deepseek","provider_request_id":"8a2c1f0e-9d3b-4c6a-b5e7-2f8d0a1c3e5f","reasoning_tokens":0,"request_id":"req_5d4e3f2a1b0c9d8e7f6a5b4c","schema_version":1,"session_id":"sess_7f3a2b1c9d4e5f6a7b8c","subject_id":"usr_owner0a1b2c3d","subject_type":"owner","total_tokens":4631,"user_id":"usr_owner0a1b2c3d"}
```

`payload_hash = c9db9d435b2d5fc181bb504875331bb1f54a47370f1860b5a45d1d84889896d3`

`tests/test_usage_event_fixtures.py::test_canonical_payload_hash_matches_readme`
按本节规则复算并校验该值；fixture 07 的 hash 与 01 不同
（`4e411df4b2de625f31fe5e5466e876d0eee00a65019a22687a25c55db250fd85`），
对应 409 冲突路径。
