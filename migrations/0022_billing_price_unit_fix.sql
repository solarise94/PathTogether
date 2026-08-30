-- =========================================================================== --
-- 0022_billing_price_unit_fix.sql：价格单位修复（批次 A，docs
-- ai-money-budget-bugfix-and-simplification-plan.md §7.1 / P0-1）。
--
-- Bug：0018 种子把 CNY/百万 tokens 按 nano = CNY×1000（= 每 token 的 nano
-- 值）写进了「nano-CNY / 百万 tokens」列，计价时又除以 1e6，形成二次缩放
-- （0.05 CNY → 50 → 1,000,000 tokens 实付 50 nano = 5e-8 CNY，少算 1e6 倍）。
-- 正确换算：rate_nano_per_million = CNY × 1,000,000,000
-- （0.05 → 50,000,000；27.0 → 27,000,000,000）。CNY 面值沿用 0018 注释与
-- 夹具（tests/fixtures/billing/deepseek_price_snapshot_2026-08-28.json）的
-- 2026-08-28 快照，只修换算，不改价格点。
--
-- 本迁移在一个事务内（migration runner：单次 execute + 单次 commit）：
--   1. 记录 pricing_v2_cutover_at（DO 块内同一变量，保证旧书 effective_to
--      与新书 effective_from 逐微秒一致；存 platform_settings，不含密钥）；
--   2. 旧错误书（0018 两本）收口 effective_to = cutover：保留历史区间可
--      查询、历史 rate 值不动——cutover 前的迟到事件重放仍按旧书计价
--      （find_active_rate 按 occurred_at 选书，语义不变）；
--   3. 插入 corrected v2 两套书（provider_cost / customer_charge ×
--      3 模型 × 峰/谷 = 12 行），effective_from = cutover，rate =
--      CNY × 1e9；
--   4. DO 块内校验（任一失败 RAISE → 整事务回滚）：
--      a) 同 kind/provider/model 的 active 书有效区间不重叠（半开区间）；
--      b) 旧书边界与新书边界一致（legacy.effective_to = v2.effective_from）；
--      c) v2 rate = legacy rate × 1,000,000（跨代一致性：legacy=CNY×1000、
--         v2=CNY×1e9）；
--   5. 无密钥迁移标志：platform_settings.pricing_v2_cutover_at（epoch 秒）
--      + audit_events 一条固定 event_id 的说明行（只含时间/书 id/口径说明）。
--
-- 不做（§7.1 禁止项）：不改 0018 已应用的历史语义；不 UPDATE/DELETE
-- ai_usage_events / billing_ledger_entries 的历史金额；旧影子数据按
-- §7.2 口径标记为 "legacy pricing scale invalid; excluded from hard
-- enforcement"（admin 汇总只读展示，本批不改 enforcement、不删 turn）。
--
-- 幂等/可重跑：收口 UPDATE 带 WHERE 守卫（已收口的书不再命中）；新书与
-- 标志全部 ON CONFLICT DO NOTHING；校验对「已迁移」状态重跑同样成立。
-- 回滚：本迁移只新增书与标志，回滚 = 删除 v2 两书并清空旧书 effective_to
-- （运维操作，无自动 down；历史事件金额不受影响——v2 生效期间的事件已按
-- v2 价入账，回滚只影响其后的新事件定价）。
-- =========================================================================== --

DO $$
DECLARE
    -- cutover：正常部署 = now()；下限保护避免时钟早于种子起点时产生
    -- effective_to <= effective_from（违反 billing_price_books CHECK）
    cutover timestamptz := GREATEST(
        now(), '2026-08-27T16:00:01Z'::timestamptz);
    legacy_ids text[] := ARRAY[
        'pb_deepseek_provider_cost_20260828',
        'pb_deepseek_customer_charge_20260828'];
    v2_ids text[] := ARRAY[
        'pb_deepseek_provider_cost_v2_corrected',
        'pb_deepseek_customer_charge_v2_corrected'];
    n int;
BEGIN
    -- -- 1) 旧错误书收口（重跑守卫：已收口或区间已更短的书不再命中） -- --
    UPDATE billing_price_books
       SET effective_to = cutover
     WHERE price_book_id = ANY (legacy_ids)
       AND status = 'active'
       AND (effective_to IS NULL OR effective_to > cutover);

    -- -- 2) corrected v2 书（两套 kind 同价，结构与旧书独立） -- --
    INSERT INTO billing_price_books
        (price_book_id, kind, currency, effective_from, effective_to,
         status, source_url, created_by)
    VALUES
        ('pb_deepseek_provider_cost_v2_corrected', 'provider_cost', 'CNY',
         cutover, NULL, 'active',
         'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
         'system-seed-0022'),
        ('pb_deepseek_customer_charge_v2_corrected', 'customer_charge',
         'CNY', cutover, NULL, 'active',
         'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
         'system-seed-0022')
    ON CONFLICT (price_book_id) DO NOTHING;

    -- rate = CNY × 1e9（nano-CNY / 百万 tokens）：
    --   deepseek-v4-flash / vision-exp：空闲 0.05/1.5/4.5 →
    --     50,000,000 / 1,500,000,000 / 4,500,000,000；
    --     高峰 0.1/3.0/9.0 → 100,000,000 / 3,000,000,000 / 9,000,000,000
    --   deepseek-v4-pro：空闲 0.15/4.5/13.5 →
    --     150,000,000 / 4,500,000,000 / 13,500,000,000；
    --     高峰 0.3/9.0/27.0 → 300,000,000 / 9,000,000,000 / 27,000,000,000
    INSERT INTO billing_rates
        (price_book_id, provider, model, time_band,
         cache_hit_nano_per_million, cache_miss_nano_per_million,
         output_nano_per_million, timezone, schedule)
    VALUES
        ('pb_deepseek_provider_cost_v2_corrected', 'deepseek',
         'deepseek-v4-flash', 'off_peak',
         50000000, 1500000000, 4500000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_provider_cost_v2_corrected', 'deepseek',
         'deepseek-v4-flash', 'peak',
         100000000, 3000000000, 9000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v2_corrected', 'deepseek',
         'deepseek-v4-flash', 'off_peak',
         50000000, 1500000000, 4500000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v2_corrected', 'deepseek',
         'deepseek-v4-flash', 'peak',
         100000000, 3000000000, 9000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),

        ('pb_deepseek_provider_cost_v2_corrected', 'deepseek',
         'deepseek-v4-pro', 'off_peak',
         150000000, 4500000000, 13500000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_provider_cost_v2_corrected', 'deepseek',
         'deepseek-v4-pro', 'peak',
         300000000, 9000000000, 27000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v2_corrected', 'deepseek',
         'deepseek-v4-pro', 'off_peak',
         150000000, 4500000000, 13500000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v2_corrected', 'deepseek',
         'deepseek-v4-pro', 'peak',
         300000000, 9000000000, 27000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),

        ('pb_deepseek_provider_cost_v2_corrected', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'off_peak',
         50000000, 1500000000, 4500000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_provider_cost_v2_corrected', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'peak',
         100000000, 3000000000, 9000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v2_corrected', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'off_peak',
         50000000, 1500000000, 4500000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v2_corrected', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'peak',
         100000000, 3000000000, 9000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb)
    ON CONFLICT (price_book_id, provider, model, time_band) DO NOTHING;

    -- -- 3) 校验 a：同 kind/provider/model 的 active 书区间不重叠 -- --
    SELECT count(*) INTO n
      FROM billing_price_books b1
      JOIN billing_rates r1 ON r1.price_book_id = b1.price_book_id
      JOIN billing_price_books b2
        ON b2.kind = b1.kind AND b2.status = 'active'
      JOIN billing_rates r2
        ON r2.price_book_id = b2.price_book_id
       AND r2.provider = r1.provider AND r2.model = r1.model
     WHERE b1.status = 'active'
       AND b1.price_book_id < b2.price_book_id
       AND b1.effective_from < COALESCE(b2.effective_to,
                                        'infinity'::timestamptz)
       AND (b1.effective_to IS NULL OR b1.effective_to > b2.effective_from);
    IF n > 0 THEN
        RAISE EXCEPTION
            '0022 校验失败：active 价格书区间重叠（% 组）', n;
    END IF;

    -- -- 校验 b：旧书必须已收口且边界与新书一致（重跑后同样成立） -- --
    SELECT count(*) INTO n
      FROM billing_price_books
     WHERE price_book_id = ANY (legacy_ids)
       AND (status <> 'active' OR effective_to IS NULL);
    IF n > 0 THEN
        RAISE EXCEPTION
            '0022 校验失败：legacy 书未收口（% 本）', n;
    END IF;

    SELECT count(*) INTO n
      FROM billing_price_books l
      JOIN billing_price_books v
        ON v.kind = l.kind
       AND v.price_book_id = ANY (v2_ids)
     WHERE l.price_book_id = ANY (legacy_ids)
       AND l.effective_to IS DISTINCT FROM v.effective_from;
    IF n > 0 THEN
        RAISE EXCEPTION
            '0022 校验失败：legacy effective_to 与 v2 effective_from 不一致';
    END IF;

    -- -- 校验 c：v2 rate = legacy rate × 1,000,000（同 kind 12 行逐项） -- --
    SELECT count(*) INTO n
      FROM billing_rates v
      JOIN billing_price_books bv ON bv.price_book_id = v.price_book_id
      JOIN billing_rates l
        ON l.price_book_id = ANY (legacy_ids)
       AND l.provider = v.provider AND l.model = v.model
       AND l.time_band = v.time_band
      JOIN billing_price_books bl
        ON bl.price_book_id = l.price_book_id
       AND bl.kind = bv.kind
     WHERE v.price_book_id = ANY (v2_ids)
       AND (v.cache_hit_nano_per_million
              <> l.cache_hit_nano_per_million * 1000000
        OR v.cache_miss_nano_per_million
              <> l.cache_miss_nano_per_million * 1000000
        OR v.output_nano_per_million
              <> l.output_nano_per_million * 1000000);
    IF n > 0 THEN
        RAISE EXCEPTION
            '0022 校验失败：v2 价格不等于 legacy × 1e6（% 行）', n;
    END IF;

    -- -- 4) 迁移标志（不含密钥；epoch 秒，供 admin 只读口径使用） -- --
    INSERT INTO platform_settings (key, value, updated_at, updated_by)
    VALUES ('pricing_v2_cutover_at',
            to_jsonb(extract(epoch FROM cutover)::float8),
            cutover, 'migration-0022')
    ON CONFLICT (key) DO NOTHING;

    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0022_price_unit_fix', cutover, 'system',
         'billing.price_unit_fix_applied', 'billing_price_books',
         jsonb_build_object(
             'cutover_epoch', extract(epoch FROM cutover)::float8,
             'reason',
             '0018 seed wrote per-token nano into per-million column '
             '(CNY x1000 instead of CNY x1e9); batch A unit fix',
             'superseded_price_book_ids', to_jsonb(legacy_ids),
             'corrected_price_book_ids', to_jsonb(v2_ids),
             'legacy_pricing_note',
             'legacy pricing scale invalid; excluded from hard enforcement'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
