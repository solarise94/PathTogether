-- =========================================================================== --
-- 0018_billing.sql：金额计费数据层（admin-billing 方案 §6，PR2）
--
-- 新增六表（DDL 逐字对齐方案 §6.2–§6.6，v0.2 修订全部纳入）：
--   - billing_accounts：金额账户（余额不入列，权威余额 = ledger SUM(amount)）；
--   - billing_price_books / billing_rates：版本化价格表（provider_cost 与
--     customer_charge 两套独立 kind；同一 kind+provider+model 的 active 区间
--     不得重叠——重叠检查用事务串行化（billing_store.activate_price_book 的
--     固定 key pg_advisory_xact_lock + 区间拒绝），**明确不引入 btree_gist**）；
--   - ai_usage_events：原始用量事件。v0.2 修订：payload_hash CHAR(64) 格式
--     CHECK、enqueued_at NOT NULL、五个 token 列可空（中断无 usage 不得用 0
--     冒充）、reasoning<=output / total=hit+miss+output / priced 完整性三条
--     CHECK 由数据库兜底；
--   - billing_ledger_entries：不可变账本（符号语义 CHECK；usage_debit 每
--     event_id 只一条由部分唯一索引保证）；
--   - provider_balance_snapshots：供应商总余额快照（DeepSeek /user/balance，
--     只用于成本监控与对账，不生成用户 ledger entry）。
--
-- 种子：把 tests/fixtures/billing/deepseek_price_snapshot_2026-08-28.json 的
-- 2026-08-28 官方价格快照种为两套同价 active price book（provider_cost 与
-- customer_charge；影子阶段 charge = provider cost，两套结构仍独立）。幂等
-- INSERT（ON CONFLICT DO NOTHING）；生效区间 [2026-08-28 00:00:00+08, ∞)。
-- 值与夹具逐项一致（tests/test_billing_store.py 的 PG 用例会重放本文件并
-- 与夹具 JSON 比对，防漂移）。
--
-- billing 能力仅在 STORAGE_BACKEND=postgres 开放（json/dual 稳定
-- pg_backend_required，见 platform_features / billing_store 守卫）。本 PR 只
-- 影子计价：不写 usage_debit（0020 硬额度阶段才加 hold，不在本迁移）。
--
-- 全部幂等（IF NOT EXISTS / ON CONFLICT DO NOTHING，对齐 0001-0017 风格）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS billing_accounts (
    account_id          TEXT        PRIMARY KEY,
    user_id             TEXT        NOT NULL UNIQUE REFERENCES users(user_id),
    currency            TEXT        NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    status              TEXT        NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active','suspended','closed')),
    soft_spend_cap_nano BIGINT,
    hard_spend_cap_nano BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             BIGINT     NOT NULL DEFAULT 1
);
COMMENT ON TABLE billing_accounts IS
    '金额账户（admin-billing §6.2）：余额无可 UPDATE 列，权威余额 = billing_ledger_entries 有符号合计；Demo 主体永不开户';

CREATE TABLE IF NOT EXISTS billing_price_books (
    price_book_id  TEXT        PRIMARY KEY,
    kind           TEXT        NOT NULL CHECK (kind IN ('provider_cost','customer_charge')),
    currency       TEXT        NOT NULL CHECK (currency = 'CNY'),
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to   TIMESTAMPTZ,
    status         TEXT        NOT NULL CHECK (status IN ('draft','active','retired')),
    source_url     TEXT        NOT NULL DEFAULT '',
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
COMMENT ON TABLE billing_price_books IS
    '版本化价格表（admin-billing §6.3）：同 kind+provider+model 的 active 区间不得重叠（激活时事务串行化检查，不用 btree_gist）；只有 draft 可编辑';
CREATE INDEX IF NOT EXISTS idx_billing_price_books_lookup
    ON billing_price_books (kind, status, effective_from);

CREATE TABLE IF NOT EXISTS billing_rates (
    price_book_id              TEXT        NOT NULL REFERENCES billing_price_books,
    provider                   TEXT        NOT NULL,
    model                      TEXT        NOT NULL,
    time_band                  TEXT        NOT NULL CHECK (time_band IN ('peak','off_peak')),
    cache_hit_nano_per_million BIGINT      NOT NULL CHECK (cache_hit_nano_per_million >= 0),
    cache_miss_nano_per_million BIGINT     NOT NULL CHECK (cache_miss_nano_per_million >= 0),
    output_nano_per_million    BIGINT      NOT NULL CHECK (output_nano_per_million >= 0),
    timezone                   TEXT        NOT NULL DEFAULT 'Asia/Shanghai',
    schedule                   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (price_book_id, provider, model, time_band)
);
COMMENT ON TABLE billing_rates IS
    '价格行（nano-CNY / 百万 tokens）：三分项非负；时段判定规则见 timezone/schedule 与 billing_pricing.time_band_for';

CREATE TABLE IF NOT EXISTS ai_usage_events (
    event_id               TEXT        PRIMARY KEY,
    call_id                TEXT        NOT NULL UNIQUE,
    payload_hash           CHAR(64)    NOT NULL
                                       CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    schema_version         INT         NOT NULL,
    request_id             TEXT,
    session_id             TEXT        NOT NULL,
    subject_type           TEXT        NOT NULL CHECK (subject_type IN ('owner','user','demo')),
    subject_id             TEXT        NOT NULL,
    user_id                TEXT        REFERENCES users(user_id),
    provider               TEXT        NOT NULL,
    model                  TEXT        NOT NULL,
    provider_request_id    TEXT,
    cache_hit_input_tokens BIGINT      CHECK (cache_hit_input_tokens >= 0),
    cache_miss_input_tokens BIGINT     CHECK (cache_miss_input_tokens >= 0),
    output_tokens          BIGINT      CHECK (output_tokens >= 0),
    reasoning_tokens       BIGINT      CHECK (reasoning_tokens >= 0),
    total_tokens           BIGINT      CHECK (total_tokens >= 0),
    occurred_at            TIMESTAMPTZ NOT NULL,
    enqueued_at            TIMESTAMPTZ NOT NULL,
    received_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                 TEXT        NOT NULL CHECK (status IN ('priced','unpriced','void')),
    unpriced_reason        TEXT        NOT NULL DEFAULT '',
    provider_price_book_id TEXT        REFERENCES billing_price_books,
    charge_price_book_id   TEXT        REFERENCES billing_price_books,
    provider_cost_nano_cny BIGINT,
    charge_nano_cny        BIGINT,
    raw_usage              JSONB       NOT NULL DEFAULT '{}'::jsonb,
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
COMMENT ON TABLE ai_usage_events IS
    'AI 用量事件（admin-billing §6.4）：token 算术/priced 完整性/payload_hash 格式由 CHECK 兜底；同 event_id 重放先比 payload_hash（相同 duplicate、不同 409）；价格版本入行后固定，调价不重算历史';
CREATE INDEX IF NOT EXISTS idx_ai_usage_events_received
    ON ai_usage_events (received_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_events_status
    ON ai_usage_events (status, received_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_events_subject
    ON ai_usage_events (subject_type, subject_id, occurred_at);

CREATE TABLE IF NOT EXISTS billing_ledger_entries (
    entry_id        TEXT        PRIMARY KEY,
    account_id      TEXT        NOT NULL REFERENCES billing_accounts(account_id),
    event_id        TEXT        REFERENCES ai_usage_events(event_id),
    kind            TEXT        NOT NULL CHECK (kind IN
                        ('grant','topup','usage_debit','refund','manual_adjustment','expiry')),
    amount_nano_cny BIGINT      NOT NULL,
    idempotency_key TEXT        NOT NULL UNIQUE,
    reason          TEXT        NOT NULL DEFAULT '',
    actor_user_id   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CHECK (
      (kind IN ('grant','topup','refund') AND amount_nano_cny > 0)
      OR (kind IN ('usage_debit','expiry') AND amount_nano_cny < 0)
      OR (kind = 'manual_adjustment' AND amount_nano_cny <> 0)
    )
);
COMMENT ON TABLE billing_ledger_entries IS
    '不可变金额账本（admin-billing §6.5）：充值/赠送/退款为正、消费/过期为负、人工调整非零（符号由 CHECK 强制）；冲正只追加，禁止 UPDATE/DELETE';
-- usage_debit 的 event_id 唯一性（§6.5：同 event 只允许一条 debit，幂等防重复扣费）
CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_ledger_usage_debit_event
    ON billing_ledger_entries (event_id) WHERE kind = 'usage_debit';

CREATE TABLE IF NOT EXISTS provider_balance_snapshots (
    snapshot_id            TEXT        PRIMARY KEY,
    provider               TEXT        NOT NULL,
    currency               TEXT        NOT NULL,
    total_balance_nano     BIGINT      NOT NULL,
    granted_balance_nano   BIGINT      NOT NULL,
    topped_up_balance_nano BIGINT      NOT NULL,
    is_available           BOOLEAN     NOT NULL,
    observed_at            TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE provider_balance_snapshots IS
    '供应商总余额快照（admin-billing §6.6）：DeepSeek /user/balance 的十进制字符串经 Decimal 精确换算 nano；只作成本监控，不拆分为用户余额';
CREATE INDEX IF NOT EXISTS idx_provider_balance_snapshots_lookup
    ON provider_balance_snapshots (provider, observed_at DESC);

-- --------------------------------------------------------------------------- --
-- 种子：2026-08-28 DeepSeek 官方价格快照（与
-- tests/fixtures/billing/deepseek_price_snapshot_2026-08-28.json 逐项一致）。
-- 两套同价 active book：provider_cost（平台成本口径）与 customer_charge
-- （用户扣费口径；影子阶段同价，结构独立）。effective_from 取快照日
-- Asia/Shanghai 00:00:00（= UTC 2026-08-27T16:00:00Z），effective_to NULL。
-- --------------------------------------------------------------------------- --
INSERT INTO billing_price_books
    (price_book_id, kind, currency, effective_from, effective_to, status,
     source_url, created_by)
VALUES
    ('pb_deepseek_provider_cost_20260828', 'provider_cost', 'CNY',
     '2026-08-27T16:00:00Z'::timestamptz, NULL, 'active',
     'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/', 'system-seed'),
    ('pb_deepseek_customer_charge_20260828', 'customer_charge', 'CNY',
     '2026-08-27T16:00:00Z'::timestamptz, NULL, 'active',
     'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/', 'system-seed')
ON CONFLICT (price_book_id) DO NOTHING;

-- deepseek-v4-flash：空闲 0.05/1.5/4.5 CNY/百万 → 50/1500/4500 nano；
-- 高峰 0.1/3.0/9.0 → 100/3000/9000 nano（nano = CNY×1000，Decimal 精确换算）
INSERT INTO billing_rates
    (price_book_id, provider, model, time_band,
     cache_hit_nano_per_million, cache_miss_nano_per_million,
     output_nano_per_million, timezone, schedule)
VALUES
    ('pb_deepseek_provider_cost_20260828', 'deepseek', 'deepseek-v4-flash', 'off_peak',
     50, 1500, 4500, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_provider_cost_20260828', 'deepseek', 'deepseek-v4-flash', 'peak',
     100, 3000, 9000, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_customer_charge_20260828', 'deepseek', 'deepseek-v4-flash', 'off_peak',
     50, 1500, 4500, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_customer_charge_20260828', 'deepseek', 'deepseek-v4-flash', 'peak',
     100, 3000, 9000, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
-- deepseek-v4-pro：空闲 0.15/4.5/13.5 → 150/4500/13500；高峰 0.3/9.0/27.0 → 300/9000/27000
    ('pb_deepseek_provider_cost_20260828', 'deepseek', 'deepseek-v4-pro', 'off_peak',
     150, 4500, 13500, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_provider_cost_20260828', 'deepseek', 'deepseek-v4-pro', 'peak',
     300, 9000, 27000, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_customer_charge_20260828', 'deepseek', 'deepseek-v4-pro', 'off_peak',
     150, 4500, 13500, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_customer_charge_20260828', 'deepseek', 'deepseek-v4-pro', 'peak',
     300, 9000, 27000, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
-- deepseek-v4-flash-vision-exp：与 flash 同价（0.05/1.5/4.5；0.1/3.0/9.0）
    ('pb_deepseek_provider_cost_20260828', 'deepseek', 'deepseek-v4-flash-vision-exp', 'off_peak',
     50, 1500, 4500, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_provider_cost_20260828', 'deepseek', 'deepseek-v4-flash-vision-exp', 'peak',
     100, 3000, 9000, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_customer_charge_20260828', 'deepseek', 'deepseek-v4-flash-vision-exp', 'off_peak',
     50, 1500, 4500, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb),
    ('pb_deepseek_customer_charge_20260828', 'deepseek', 'deepseek-v4-flash-vision-exp', 'peak',
     100, 3000, 9000, 'Asia/Shanghai',
     '{"windows":[["09:00","12:00"],["14:00","18:00"]],"weekdays_only":true}'::jsonb)
ON CONFLICT (price_book_id, provider, model, time_band) DO NOTHING;
