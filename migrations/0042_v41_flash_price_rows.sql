-- =========================================================================== --
-- 0042_v41_flash_price_rows.sql：限时模型 deepseek-v4.1-flash-expires-on-0910
-- 计价行（2026-09-09 限时模型批次）。
--
-- 背景：admin 可把平台默认模型切到官方限时模型
-- deepseek-v4.1-flash-expires-on-0910（2026-09-10 到期）。计费链路
-- （authorize 最坏价估算 + ingest 计价）按 (kind, provider, model) 从
-- active 价格书取率；hard 模式下无价 → pricing_unavailable fail-closed
-- 拒绝 provider 调用。owner 决策：**与 deepseek-v4-flash / vision-exp 同价**
-- （off_peak 0.05/1.5/4.5 CNY；peak 0.1/3.0/9.0，每百万 tokens）。
--
-- 做法：向两本 active 书（pb_deepseek_provider_cost_v2_corrected /
-- pb_deepseek_customer_charge_v2_corrected，0022 建立）按 vision-exp 现行
-- 行**原样复制**（INSERT…SELECT，不字面写死数值——价目若在未来簿版本中
-- 调整，重放本迁移仍与 vision-exp 保持一致），ON CONFLICT DO NOTHING 幂等。
--
-- 回滚安全性：仅追加行，不改既有行；旧代码不查该模型行不受影响。
-- =========================================================================== --

INSERT INTO billing_rates
    (price_book_id, provider, model, time_band,
     cache_hit_nano_per_million, cache_miss_nano_per_million,
     output_nano_per_million, timezone, schedule)
SELECT r.price_book_id, r.provider,
       'deepseek-v4.1-flash-expires-on-0910', r.time_band,
       r.cache_hit_nano_per_million, r.cache_miss_nano_per_million,
       r.output_nano_per_million, r.timezone, r.schedule
FROM billing_rates r
JOIN billing_price_books b ON b.price_book_id = r.price_book_id
WHERE b.price_book_id IN ('pb_deepseek_provider_cost_v2_corrected',
                          'pb_deepseek_customer_charge_v2_corrected')
  AND b.status = 'active'
  AND r.model = 'deepseek-v4-flash-vision-exp'
ON CONFLICT (price_book_id, provider, model, time_band) DO NOTHING;
