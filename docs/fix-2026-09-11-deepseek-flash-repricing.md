# fix-2026-09-11：deepseek-flash 官方降价后的价目迁移（0045）

## 背景

- 2026-09-10 DeepSeek 发布 V4.1-Flash（官方现网 ID `deepseek-flash`）并
  **降价**；0044 给 `deepseek-flash` 插的费率是从 vision-exp 旧价复制的
  （0044 头注明确写了「本批不改价目面值……避免把官方新面值混进未决策的
  整本价格书」——当时悬置的决策，本批落地）。
- 官方 zh-cn 定价页（2026-09-11 核实，单位 CNY/百万 tokens）：

  | 模型 | 缓存命中 | 缓存未命中 | 输出 |
  |---|---|---|---|
  | deepseek-flash 空闲 | 0.02 | 1.0 | 4.0 |
  | deepseek-flash 高峰 | 0.04 | 2.0 | 8.0 |
  | deepseek-v4-pro 空闲 | 0.15 | 4.5 | 13.5 |
  | deepseek-v4-pro 高峰 | 0.30 | 9.0 | 27.0 |

  高峰定义不变：北京时间工作日 09:00–12:00、14:00–18:00，空闲价为高峰一半
  ——与 `billing_pricing.py` 现行时段表一致，不动。
- 官方 changelog：旧名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp`
  已退役、上游路由到 V4.1-Flash 并按其价格计费；`deepseek-v4-pro` 价格未变。
- 换算（沿用 0022 口径）：nano_per_million = CNY × 1e9。
  flash 新费率：空闲 20,000,000 / 1,000,000,000 / 4,000,000,000；
  高峰 40,000,000 / 2,000,000,000 / 8,000,000,000。

## 设计

新迁移 `migrations/0045_deepseek_flash_repricing.sql`，完全沿用 0022 的
价格书切换（cutover）模式，**不原地 UPDATE**（保留历史区间可查询）：

1. `cutover = GREATEST(now(), <下限保护时间戳>)`（参照 0022 写法，下限取
   0044 之后、本迁移部署前的一个固定时间，如 '2026-09-11T00:00:01Z'）。
2. 两本 v2_corrected 书收口 `effective_to = cutover`（带重跑守卫）。
3. 新建两本书 `pb_deepseek_provider_cost_v3_flash_repricing` /
   `pb_deepseek_customer_charge_v3_flash_repricing`，
   `effective_from = cutover`、`status='active'`，timezone/schedule 照抄 v2。
4. v3 行：
   - `deepseek-flash`、`deepseek-v4-flash`、`deepseek-v4-flash-vision-exp`
     三个模型 → **flash 新费率**（上游已同价路由；含 vision-exp——继续按
     旧价计 provider_cost 会与真实成本不符）；
   - `deepseek-v4.1-flash-expires-on-0910` → 同为 flash 新费率（该模型已
     到期不可选，但历史迟到事件重放要有正确费率）；
   - `deepseek-v4-pro` → 从 v2 原样复制（官方未调价）。
   - provider_cost 与 customer_charge 两本同价（维持本仓 cost==charge 惯例，
     与 0022/0042/0044 一致）。
5. DO 块校验（任一失败 RAISE 回滚，参照 0022）：
   a) 同 kind/provider/model 的 active 书有效区间不重叠（半开）；
   b) v2.effective_to = v3.effective_from；
   c) v3 中 flash 家族 4 模型 × 2 时段的 rate 等于本文档列出的常量；
   d) v3 中 v4-pro 的 rate = v2 对应值。
6. `platform_settings` 写 `pricing_v3_cutover_at`（epoch 秒，ON CONFLICT
   DO NOTHING）+ 一条 audit_events 说明行（固定 event_id，无密钥）。

## 同步更新

- `tests/fixtures/billing/deepseek_price_snapshot_2026-09-11.json`：新快照
  夹具（格式照 2026-08-28 那份，含上表 CNY 面值与 nano 换算；`_comment`
  注明快照日与官方降价）。旧夹具**保留**（0022 时代的历史区间测试仍需要）。
- `tests/_billing_helpers.py` 的迁移重放列表加入 0045。
- 找出断言「flash 费率 == vision-exp 费率」或硬编码旧 flash 费率的测试
  （已知 `tests/test_admin_api_v1.py` 约 675-772 行、`test_billing_store.py`
  367 行附近；以实际搜索结果为准），改为断言新费率/新关系；凡是验证
  「cutover 前事件按旧价、cutover 后按新价」的语义要保留并补一条
  v2→v3 的用例。

## 不做

- 不改 `billing_pricing.py` 的时段表与数学；
- 不改模型目录（`app.py:12842-12869` 已与官方对齐）；
- 不动 v4-pro 费率、不动历史书的历史区间；
- 不给 customer_charge 加利润率（维持 cost==charge；如未来要加，另开决策）。

## 验收

- 迁移在干净库 + 已应用 0044 的库上都能跑通、可重跑（ON CONFLICT/守卫）；
- DO 块校验故意改错一个数能触发回滚（实现者本地验证后还原，或在测试中
  以事务内断言覆盖）；
- 相关测试全绿；`git diff` 只触及 migrations/0045、新夹具、测试文件。
