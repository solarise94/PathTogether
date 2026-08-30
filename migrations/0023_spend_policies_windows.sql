-- =========================================================================== --
-- 0023_spend_policies_windows.sql：金额 policy/window 数据层（批次 B，docs
-- ai-money-budget-bugfix-and-simplification-plan.md §3.1/§3.2/§7.3/§8）。
--
-- 语义（本批仍为 shadow：不接入 run/hold/usage 请求路径，spend_enforcement_
-- mode 固定 "shadow"；把窗口接进 billing_holds 链路是批次 C）：
--
--   ai_spend_policies（§3.1）：
--     - scope_type ∈ demo_global | user_default | user_override | owner；
--       user_override 必须带 scope_id(=user_id)，其余 scope 必须 scope_id NULL
--       （CHECK 强制，不靠应用层自觉）；
--     - period_kind ∈ calendar_week | calendar_month | none（none 为将来
--       「无周期策略」预留位，本批种子与解析均不产生 none 行）；
--     - 金额全部 BIGINT nano-CNY（1 CNY = 1e9 nano），CHECK >= 0；
--     - 「同一 scope 同时只能解析到一条有效策略」由部分唯一索引硬性保证：
--       同 (scope_type, scope_id) 至多一条 enabled 且未收口（effective_to
--       IS NULL）的策略。带区间的历史/接班行由 store 写路径在固定 key
--       pg_advisory_xact_lock 内做区间重叠拒绝（与 billing_price_books
--       §6.3 同思路，不引入 btree_gist）；
--     - version 列供 CAS 更新（冲突映射 409）。
--
--   ai_spend_windows（§3.2）：
--     - enforcement projection：事务内维护的 spent/reserved（CHECK >= 0），
--       可从 usage events / open holds 重建（spend_store.reconcile_spend_
--       windows 对账，只报告不自动修）；
--     - UNIQUE(subject_type, subject_id, window_start, window_end) 兜底并发
--       创建只产生一行；窗口边界永远由服务端按 Asia/Shanghai 计算（周一
--       00:00 / 每月 1 日 00:00，左闭右开），DB 存 UTC TIMESTAMPTZ；
--     - limit_nano_snapshot 固定窗口创建时刻的策略额度：默认策略修改不
--       追溯已开窗口（「调整当前窗口」只改 snapshot，不取消已完成消费）。
--
--   种子（enabled=true, version=1；**额度面值是 owner 待决策的占位默认，
--   后台可改**——本迁移只负责结构与初始值，不代表额度已定）：
--     - spp_demo_global : demo_global   / calendar_week  / 50    CNY
--     - spp_user_default: user_default  / calendar_month / 20    CNY
--     - spp_owner       : owner         / calendar_month / 1,000 CNY
--   （换算 1 CNY = 1e9 nano-CNY：50→50_000_000_000；20→20_000_000_000；
--    1_000→1_000_000_000_000）
--
--   enforcement 开关（§7.3）：platform_settings.spend_enforcement_mode 只在
--   **不存在**时插入 "shadow"（ON CONFLICT DO NOTHING，不覆盖既有值——
--   回滚重跑不得把 owner 已切换的值改回 shadow）。
--
-- 幂等/可重跑：全部 IF NOT EXISTS / ON CONFLICT DO NOTHING；种子与开关对
-- 「已迁移」状态重跑不改变任何行。单迁移单事务（pg_store.ensure_schema
-- 一次 execute + 一次 commit；DO 块异常整体回滚）。
-- 回滚：本迁移只新增表/索引/种子/开关，回滚 = 删两张表并清
-- spend_enforcement_mode 键（运维操作，无自动 down；不影响既有 billing 账
-- 目——批次 B 没有任何写路径依赖这些表）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS ai_spend_policies (
    policy_id      TEXT        PRIMARY KEY,   -- spp_<24hex> 或 spp_<语义名>
    scope_type     TEXT        NOT NULL CHECK (scope_type IN
                                            ('demo_global','user_default',
                                             'user_override','owner')),
    -- user_override 必须带 scope_id(=user_id)；其余 scope 一律 NULL（DB 强制）
    scope_id       TEXT        CHECK ((scope_type = 'user_override')
                                      = (scope_id IS NOT NULL)),
    period_kind    TEXT        NOT NULL CHECK (period_kind IN
                                            ('calendar_week','calendar_month',
                                             'none')),
    limit_nano_cny BIGINT      NOT NULL CHECK (limit_nano_cny >= 0),
    enabled        BOOLEAN     NOT NULL DEFAULT true,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to   TIMESTAMPTZ,
    version        BIGINT      NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
COMMENT ON TABLE ai_spend_policies IS
    '金额策略（批次 B §3.1）：同一 scope 同时只能解析到一条有效策略（部分唯一索引硬性保证）；默认更新只影响新窗口；version/CAS 更新冲突映射 409';

-- 同一 scope 至多一条「enabled 且未收口」策略：解析的最小硬保证
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_spend_policies_scope_open
    ON ai_spend_policies (scope_type, COALESCE(scope_id, ''))
    WHERE enabled AND effective_to IS NULL;
-- 解析查询：按 scope 取有效策略（effective_from <= at < effective_to）
CREATE INDEX IF NOT EXISTS idx_ai_spend_policies_scope_time
    ON ai_spend_policies (scope_type, COALESCE(scope_id, ''),
                          effective_from DESC);

CREATE TABLE IF NOT EXISTS ai_spend_windows (
    window_id           TEXT        PRIMARY KEY,  -- spw_<24hex>
    policy_id           TEXT        NOT NULL
                                    REFERENCES ai_spend_policies(policy_id),
    policy_version      BIGINT      NOT NULL,
    subject_type        TEXT        NOT NULL CHECK (subject_type IN
                                                ('demo','user','owner')),
    subject_id          TEXT        NOT NULL,   -- demo 恒 'demo_global'；其余 user_id
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    limit_nano_snapshot BIGINT      NOT NULL
                                    CHECK (limit_nano_snapshot >= 0),
    spent_nano_cny      BIGINT      NOT NULL DEFAULT 0
                                    CHECK (spent_nano_cny >= 0),
    reserved_nano_cny   BIGINT      NOT NULL DEFAULT 0
                                    CHECK (reserved_nano_cny >= 0),
    status              TEXT        NOT NULL DEFAULT 'open'
                                    CHECK (status IN ('open','closed')),
    version             BIGINT      NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subject_type, subject_id, window_start, window_end),
    CHECK (window_end > window_start)
);
COMMENT ON TABLE ai_spend_windows IS
    '周/月消费窗口（批次 B §3.2）：limit_nano_snapshot 固定创建时策略额度（策略修改不追溯）；spent/reserved 是事务内 FOR UPDATE 维护、可从 usage/holds 重建的 enforcement projection；边界由服务端按 Asia/Shanghai 生成，客户端不得提交任意窗口';
CREATE INDEX IF NOT EXISTS idx_ai_spend_windows_subject
    ON ai_spend_windows (subject_type, subject_id, window_start DESC);
CREATE INDEX IF NOT EXISTS idx_ai_spend_windows_status
    ON ai_spend_windows (status);

DO $$
BEGIN
    -- -- 种子策略（占位默认额度：owner 待决策，后台可改） -- --
    INSERT INTO ai_spend_policies
        (policy_id, scope_type, scope_id, period_kind, limit_nano_cny,
         enabled, effective_from, effective_to, version, updated_by)
    VALUES
        ('spp_demo_global', 'demo_global', NULL, 'calendar_week',
         50000000000, true, now(), NULL, 1, 'system-seed-0023'),
        ('spp_user_default', 'user_default', NULL, 'calendar_month',
         20000000000, true, now(), NULL, 1, 'system-seed-0023'),
        ('spp_owner', 'owner', NULL, 'calendar_month',
         1000000000000, true, now(), NULL, 1, 'system-seed-0023')
    ON CONFLICT (policy_id) DO NOTHING;

    -- -- enforcement 开关：只在不存在时插入 "shadow"（不覆盖既有值） -- --
    INSERT INTO platform_settings (key, value, updated_at, updated_by)
    VALUES ('spend_enforcement_mode', '"shadow"'::jsonb, now(),
            'migration-0023')
    ON CONFLICT (key) DO NOTHING;

    -- -- 迁移标志 audit（不含密钥；固定 event_id，重跑不重复） -- --
    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0023_spend_windows', now(), 'system',
         'spend.policies_windows_applied', 'ai_spend_policies',
         jsonb_build_object(
             'seed_policy_ids', to_jsonb(ARRAY[
                 'spp_demo_global', 'spp_user_default', 'spp_owner']),
             'limits_placeholder', true,
             'note', 'seed limits are placeholder defaults pending owner '
                     'decision; editable via admin; enforcement stays shadow',
             'enforcement_mode', 'shadow'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
