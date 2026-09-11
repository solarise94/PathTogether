-- =========================================================================== --
-- 0045_deepseek_flash_repricing.sql：deepseek-flash 官方降价后的价目迁移
-- （docs/fix-2026-09-11-deepseek-flash-repricing.md，2026-09-11）。
--
-- 背景：2026-09-10 DeepSeek 发布 V4.1-Flash（官方现网 ID deepseek-flash）并
-- **降价**；0044 给 deepseek-flash 插的费率是从 vision-exp 旧价复制的
-- （0044 头注「本批不改价目面值」——当时悬置的决策，本批落地）。官方
-- zh-cn 定价页（2026-09-11 核实，CNY/百万 tokens）：
--   deepseek-flash 空闲 0.02 / 1.0 / 4.0；高峰 0.04 / 2.0 / 8.0
--   deepseek-v4-pro 空闲 0.15 / 4.5 / 13.5；高峰 0.30 / 9.0 / 27.0（未调价）
-- 高峰定义不变：北京时间工作日 09:00–12:00、14:00–18:00，空闲价为高峰一半
-- ——与 billing_pricing.py 现行时段表一致，不动。官方 changelog：旧名
-- deepseek-v4-flash / deepseek-v4-flash-vision-exp 已退役、上游路由到
-- V4.1-Flash 并按其价格计费，故 flash 家族旧名（含限时模型
-- deepseek-v4.1-flash-expires-on-0910 的历史迟到事件重放）一并按新价入册；
-- 继续按旧价计 provider_cost 会与真实成本不符。
--
-- 换算（沿用 0022 口径）：nano_per_million = CNY × 1e9。flash 新费率：
--   空闲 20,000,000 / 1,000,000,000 / 4,000,000,000；
--   高峰 40,000,000 / 2,000,000,000 / 8,000,000,000。
--
-- 本迁移在一个事务内（migration runner：单次 execute + 单次 commit），完全
-- 沿用 0022 的价格书切换（cutover）模式，不原地 UPDATE（保留历史区间可查询）：
--   1. 记录 pricing_v3_cutover_at（DO 块内同一变量，保证 v2 书 effective_to
--      与 v3 书 effective_from 逐微秒一致；存 platform_settings，不含密钥）；
--      cutover = GREATEST(now(), '2026-09-11T00:00:01Z')——下限保护：官方
--      降价生效日之后、正常部署时钟之前的固定下限，避免时钟早于该日时
--      v2 收口点早于其生效区间产生异常边界（参照 0022 写法）；
--   2. 两本 v2_corrected 书收口 effective_to = cutover（带重跑守卫）：
--      历史区间可查询、rate 值不动——cutover 前的迟到事件重放仍按 v2 价
--      （find_active_rate 按 occurred_at 选书，语义不变）；
--   3. 插入 v3 两套书（provider_cost / customer_charge × 5 模型 × 峰/谷
--      = 20 行），effective_from = cutover，status='active'，
--      timezone/schedule 照抄 v2（'{"windows":[["09:00","12:00"],
--      ["14:00","18:00"]],"weekdays_only":true}' / Asia/Shanghai）；
--      flash 家族 4 模型（deepseek-flash / deepseek-v4-flash /
--      deepseek-v4-flash-vision-exp / deepseek-v4.1-flash-expires-on-0910）
--      → flash 新费率；deepseek-v4-pro → 从 v2 原样复制（官方未调价，
--      INSERT…SELECT 不字面写死，与 0042/0044 同款）；provider_cost 与
--      customer_charge 两本同价（维持本仓 cost==charge 惯例）；
--   4. DO 块内校验（任一失败 RAISE → 整事务回滚）：
--      a) 同 kind/provider/model 的 active 书有效区间不重叠（半开区间）；
--      b) v2 effective_to = v3 effective_from（重跑后同样成立）；
--      c) v3 中 flash 家族 4 模型 × 2 时段（两 kind 共 16 行）rate =
--         本头注列出的常量（行数 + 逐项值双重校验）；
--      d) v3 中 v4-pro 的 rate = v2 对应值（两 kind 共 4 行）；
--   5. 无密钥迁移标志：platform_settings.pricing_v3_cutover_at（epoch 秒）
--      + audit_events 一条固定 event_id 的说明行（只含时间/书 id/口径说明）。
--
-- 不做：不改 billing_pricing.py 的时段表与数学；不改模型目录；不动
-- v4-pro 费率、不动历史书的历史区间；不给 customer_charge 加利润率
-- （维持 cost==charge）。
--
-- 幂等/可重跑：收口 UPDATE 带 WHERE 守卫（已收口的书不再命中）；v3 书/
-- 行与标志全部 ON CONFLICT DO NOTHING；校验对「已迁移」状态重跑同样成立
-- （重跑时 now() 前移，但守卫使 v2.to / v3.from 保持首次的 cutover）。
-- 回滚：本迁移只新增书与标志，回滚 = 删除 v3 两书并把 v2 书 effective_to
-- 清回 NULL（运维操作，无自动 down；历史事件金额不受影响——v2 生效期间
-- 的事件已按 v2 价入账，回滚只影响其后的新事件定价）。
-- =========================================================================== --

DO $$
DECLARE
    -- cutover：正常部署 = now()；下限保护取官方降价生效日（2026-09-11
    -- 北京时间 08:00:01 = UTC 00:00:01）之后的第一秒，避免时钟异常时
    -- 产生 effective_to <= effective_from（违反 billing_price_books CHECK）
    cutover timestamptz := GREATEST(
        now(), '2026-09-11T00:00:01Z'::timestamptz);
    v2_ids text[] := ARRAY[
        'pb_deepseek_provider_cost_v2_corrected',
        'pb_deepseek_customer_charge_v2_corrected'];
    v3_ids text[] := ARRAY[
        'pb_deepseek_provider_cost_v3_flash_repricing',
        'pb_deepseek_customer_charge_v3_flash_repricing'];
    -- flash 家族：官方现网 ID + 两个已退役旧名 + 限时模型（历史迟到事件
    -- 重放要有正确费率）——上游已同价路由到 V4.1-Flash
    flash_models text[] := ARRAY[
        'deepseek-flash',
        'deepseek-v4-flash',
        'deepseek-v4-flash-vision-exp',
        'deepseek-v4.1-flash-expires-on-0910'];
    n int;
BEGIN
    -- -- 1) v2_corrected 书收口（重跑守卫：已收口或区间已更短的书不再命中） -- --
    UPDATE billing_price_books
       SET effective_to = cutover
     WHERE price_book_id = ANY (v2_ids)
       AND status = 'active'
       AND (effective_to IS NULL OR effective_to > cutover);

    -- -- 2) v3 书（两套 kind 同价，结构与 v2 独立；timezone/schedule 照抄 v2） -- --
    INSERT INTO billing_price_books
        (price_book_id, kind, currency, effective_from, effective_to,
         status, source_url, created_by)
    VALUES
        ('pb_deepseek_provider_cost_v3_flash_repricing', 'provider_cost',
         'CNY', cutover, NULL, 'active',
         'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
         'system-seed-0045'),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'customer_charge',
         'CNY', cutover, NULL, 'active',
         'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
         'system-seed-0045')
    ON CONFLICT (price_book_id) DO NOTHING;

    -- flash 家族新费率（CNY × 1e9，nano-CNY / 百万 tokens）：
    --   off_peak：0.02 / 1.0 / 4.0 → 20,000,000 / 1,000,000,000 / 4,000,000,000
    --   peak：    0.04 / 2.0 / 8.0 → 40,000,000 / 2,000,000,000 / 8,000,000,000
    -- （空闲价 = 高峰一半；时段表与 billing_pricing.py 一致，不动）
    INSERT INTO billing_rates
        (price_book_id, provider, model, time_band,
         cache_hit_nano_per_million, cache_miss_nano_per_million,
         output_nano_per_million, timezone, schedule)
    VALUES
        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-flash', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-flash', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-flash', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-flash', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),

        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),

        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-v4-flash-vision-exp', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),

        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-v4.1-flash-expires-on-0910', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_provider_cost_v3_flash_repricing', 'deepseek',
         'deepseek-v4.1-flash-expires-on-0910', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-v4.1-flash-expires-on-0910', 'off_peak',
         20000000, 1000000000, 4000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
        ('pb_deepseek_customer_charge_v3_flash_repricing', 'deepseek',
         'deepseek-v4.1-flash-expires-on-0910', 'peak',
         40000000, 2000000000, 8000000000, 'Asia/Shanghai',
         '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb)

    -- deepseek-v4-pro：官方未调价，从 v2 对应行原样复制（INSERT…SELECT 不
    -- 字面写死数值——与 0042/0044 同款；同 kind 的 v2 书经 kind 关联定位）
    ON CONFLICT (price_book_id, provider, model, time_band) DO NOTHING;

    INSERT INTO billing_rates
        (price_book_id, provider, model, time_band,
         cache_hit_nano_per_million, cache_miss_nano_per_million,
         output_nano_per_million, timezone, schedule)
    SELECT v3b.price_book_id, r.provider, r.model, r.time_band,
           r.cache_hit_nano_per_million, r.cache_miss_nano_per_million,
           r.output_nano_per_million, r.timezone, r.schedule
    FROM billing_rates r
    JOIN billing_price_books v2b ON v2b.price_book_id = r.price_book_id
    JOIN billing_price_books v3b ON v3b.kind = v2b.kind
    WHERE r.price_book_id = ANY (v2_ids)
      AND v3b.price_book_id = ANY (v3_ids)
      AND v2b.status = 'active'
      AND v3b.status = 'active'
      AND r.model = 'deepseek-v4-pro'
    ON CONFLICT (price_book_id, provider, model, time_band) DO NOTHING;

    -- -- 3) 校验 a：同 kind/provider/model 的 active 书区间不重叠（半开） -- --
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
            '0045 校验失败：active 价格书区间重叠（% 组）', n;
    END IF;

    -- -- 校验 b：v2 必须已收口且边界与 v3 一致（重跑后同样成立） -- --
    SELECT count(*) INTO n
      FROM billing_price_books
     WHERE price_book_id = ANY (v2_ids)
       AND (status <> 'active' OR effective_to IS NULL);
    IF n > 0 THEN
        RAISE EXCEPTION
            '0045 校验失败：v2 书未收口（% 本）', n;
    END IF;

    SELECT count(*) INTO n
      FROM billing_price_books v2
      JOIN billing_price_books v3
        ON v3.kind = v2.kind
       AND v3.price_book_id = ANY (v3_ids)
     WHERE v2.price_book_id = ANY (v2_ids)
       AND v2.effective_to IS DISTINCT FROM v3.effective_from;
    IF n > 0 THEN
        RAISE EXCEPTION
            '0045 校验失败：v2 effective_to 与 v3 effective_from 不一致';
    END IF;

    -- -- 校验 c：flash 家族 4 模型 × 2 时段（×2 kind = 16 行）齐备且 -- --
    -- -- rate = 本头注常量（off_peak 20e6/1e9/4e9；peak 40e6/2e9/8e9）  -- --
    SELECT count(*) INTO n
      FROM billing_rates r
      JOIN billing_price_books b ON b.price_book_id = r.price_book_id
     WHERE b.price_book_id = ANY (v3_ids)
       AND r.provider = 'deepseek'
       AND r.model = ANY (flash_models);
    IF n <> 16 THEN
        RAISE EXCEPTION
            '0045 校验失败：flash 家族应 16 行，实际 % 行', n;
    END IF;

    SELECT count(*) INTO n
      FROM billing_rates r
      JOIN billing_price_books b ON b.price_book_id = r.price_book_id
     WHERE b.price_book_id = ANY (v3_ids)
       AND r.provider = 'deepseek'
       AND r.model = ANY (flash_models)
       AND (r.cache_hit_nano_per_million
              <> CASE r.time_band WHEN 'peak' THEN 40000000
                                  ELSE 20000000 END
        OR r.cache_miss_nano_per_million
              <> CASE r.time_band WHEN 'peak' THEN 2000000000
                                  ELSE 1000000000 END
        OR r.output_nano_per_million
              <> CASE r.time_band WHEN 'peak' THEN 8000000000
                                  ELSE 4000000000 END);
    IF n > 0 THEN
        RAISE EXCEPTION
            '0045 校验失败：flash 新费率与文档常量不符（% 行）', n;
    END IF;

    -- -- 校验 d：v3 中 v4-pro 的 rate = v2 对应值（2 kind × 2 时段 = 4 行） -- --
    SELECT count(*) INTO n
      FROM billing_rates v3r
      JOIN billing_price_books v3b ON v3b.price_book_id = v3r.price_book_id
      JOIN billing_rates v2r
        ON v2r.price_book_id = ANY (v2_ids)
       AND v2r.provider = v3r.provider AND v2r.model = v3r.model
       AND v2r.time_band = v3r.time_band
      JOIN billing_price_books v2b
        ON v2b.price_book_id = v2r.price_book_id
       AND v2b.kind = v3b.kind
     WHERE v3r.price_book_id = ANY (v3_ids)
       AND v3r.provider = 'deepseek'
       AND v3r.model = 'deepseek-v4-pro';
    IF n <> 4 THEN
        RAISE EXCEPTION
            '0045 校验失败：v4-pro 应 4 行，实际 % 行', n;
    END IF;

    SELECT count(*) INTO n
      FROM billing_rates v3r
      JOIN billing_price_books v3b ON v3b.price_book_id = v3r.price_book_id
      JOIN billing_rates v2r
        ON v2r.price_book_id = ANY (v2_ids)
       AND v2r.provider = v3r.provider AND v2r.model = v3r.model
       AND v2r.time_band = v3r.time_band
      JOIN billing_price_books v2b
        ON v2b.price_book_id = v2r.price_book_id
       AND v2b.kind = v3b.kind
     WHERE v3r.price_book_id = ANY (v3_ids)
       AND v3r.provider = 'deepseek'
       AND v3r.model = 'deepseek-v4-pro'
       AND (v3r.cache_hit_nano_per_million
              <> v2r.cache_hit_nano_per_million
        OR v3r.cache_miss_nano_per_million
              <> v2r.cache_miss_nano_per_million
        OR v3r.output_nano_per_million
              <> v2r.output_nano_per_million
        OR v3r.timezone IS DISTINCT FROM v2r.timezone
        OR v3r.schedule IS DISTINCT FROM v2r.schedule);
    IF n > 0 THEN
        RAISE EXCEPTION
            '0045 校验失败：v3 的 v4-pro 价格与 v2 不一致（% 行）', n;
    END IF;

    -- -- 4) 迁移标志（不含密钥；epoch 秒，供 admin 只读口径使用） -- --
    INSERT INTO platform_settings (key, value, updated_at, updated_by)
    VALUES ('pricing_v3_cutover_at',
            to_jsonb(extract(epoch FROM cutover)::float8),
            cutover, 'migration-0045')
    ON CONFLICT (key) DO NOTHING;

    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0045_flash_repricing', cutover, 'system',
         'billing.flash_repricing_applied', 'billing_price_books',
         jsonb_build_object(
             'cutover_epoch', extract(epoch FROM cutover)::float8,
             'reason',
             'DeepSeek cut V4.1-Flash (deepseek-flash) official prices on '
             '2026-09-10; 0044 had copied vision-exp legacy rates pending '
             'the pricing decision landed by this migration',
             'superseded_price_book_ids', to_jsonb(v2_ids),
             'active_price_book_ids', to_jsonb(v3_ids),
             'repriced_models', to_jsonb(flash_models),
             'unchanged_models', to_jsonb(ARRAY['deepseek-v4-pro']::text[]),
             'official_note',
             'retired aliases deepseek-v4-flash / deepseek-v4-flash-'
             'vision-exp are routed upstream to V4.1-Flash and billed at '
             'its prices; peak = Asia/Shanghai weekdays 09:00-12:00 / '
             '14:00-18:00, off-peak = half of peak'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
