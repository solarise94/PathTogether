-- =========================================================================== --
-- 0044_deepseek_flash_price_rows.sql：官方现网 ID deepseek-flash 计价行
-- （2026-09-10，api-docs.deepseek.com/zh-cn 定价页：deepseek-flash =
-- DeepSeek-V4.1-Flash，支持视觉；旧名 vision-exp / v4-flash 仍可调）。
--
-- 计费链路（authorize 最坏价估算 + ingest 计价）按 (kind, provider, model)
-- 从 active 价格书取率；hard 模式下无价 → pricing_unavailable fail-closed
-- 拒绝 provider 调用。本批不改价目面值：从 vision-exp 现行行原样复制
-- （与 0042 同款 INSERT…SELECT），避免把官方新面值（文档 USD 档）混进
-- 未决策的整本价格书。
--
-- 回滚安全性：仅追加行，不改既有行；旧代码不查该模型行不受影响。
-- =========================================================================== --

INSERT INTO billing_rates
    (price_book_id, provider, model, time_band,
     cache_hit_nano_per_million, cache_miss_nano_per_million,
     output_nano_per_million, timezone, schedule)
SELECT r.price_book_id, r.provider,
       'deepseek-flash', r.time_band,
       r.cache_hit_nano_per_million, r.cache_miss_nano_per_million,
       r.output_nano_per_million, r.timezone, r.schedule
FROM billing_rates r
JOIN billing_price_books b ON b.price_book_id = r.price_book_id
WHERE b.price_book_id IN ('pb_deepseek_provider_cost_v2_corrected',
                          'pb_deepseek_customer_charge_v2_corrected')
  AND b.status = 'active'
  AND r.model = 'deepseek-v4-flash-vision-exp'
ON CONFLICT (price_book_id, provider, model, time_band) DO NOTHING;
