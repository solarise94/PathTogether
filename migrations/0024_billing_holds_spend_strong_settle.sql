-- =========================================================================== --
-- 0024_billing_holds_spend_strong_settle.sql：强一致 usage/hold 协议（批次 C，
-- docs ai-money-budget-bugfix-and-simplification-plan.md §3.3/§3.4/§4.2/§7.3/§8）。
--
-- 语义（把批次 B 的 spend window 投影接进 billing_holds 授权/结算事务）：
--
--   - spend_window_id：授权时刻解析到的 ai_spend_windows 行（可空——策略缺失
--     的 shadow 观测行、0024 之前的旧行均为 NULL）；不是 FK：窗口行由
--     get_or_create 事务创建，且回滚 shadow 不删窗口（§9.7），悬空引用由
--     对账器报告，不让 FK 阻断结算链；
--   - reserved_nano_cny：授权时写进窗口的预占额（= 授权估算；无估算/无窗口
--     → NULL）。release/settle/过期回收据此归还窗口 reserved；
--   - actual_nano_cny：settle 实际入账额（priced 事件的 customer_charge；
--     unpriced → NULL 未知）。actual > 估算按 actual 入账（§3.4.8）；
--   - enforcement_mode：**授权时刻**的全局 spend_enforcement_mode 快照
--     （shadow|registered|all，§7.3）。settle 按快照（而非当下全局值）裁决
--     旧 body 兼容与 debit 语义——每条 hold 的协议是授权时签的，混合期可
--     审计。0024 之前的旧行 NULL（按 shadow 兼容路径处理）；
--   - denial_reason：稳定拒绝码（§3.3：spend_budget_exhausted /
--     pricing_unavailable / spend_policy_missing / spend_window_unavailable）。
--     shadow 模式 authorized=true 的观测行也记（「若开硬闸会拒的原因」）；
--     hard 拒绝不写行（409/503 错误信封即观测），该列只在已落行上出现。
--
--   - subject_type CHECK 放宽为 ('owner','user','demo')：Demo 不再跳过 hold
--     （§4.2——demo 也写 hold 行 + 进 demo_global 周窗口投影）。demo 无
--     billing_accounts，account_id 保持可空（0020 语义不变）。
--
-- 不做：不改 0018/0020/0022/0023 的历史语义；不改既有列约束；旧行全部
-- NULL/原值，读路径按「无窗口投影的 shadow 兼容行」处理。不删旧路径。
--
-- 幂等/可重跑：ADD COLUMN IF NOT EXISTS + DO 块内按约束名判存；种子无。
-- 回滚：DROP 这五列并恢复 subject_type 旧 CHECK 前须先把 demo 行清出
-- billing_holds（运维操作，无自动 down；回滚到 shadow 不删 policy/window/
-- hold/ledger，§9.7）。
-- =========================================================================== --

ALTER TABLE billing_holds
    ADD COLUMN IF NOT EXISTS spend_window_id   TEXT,
    ADD COLUMN IF NOT EXISTS reserved_nano_cny BIGINT
        CONSTRAINT billing_holds_reserved_nano_check CHECK (reserved_nano_cny >= 0),
    ADD COLUMN IF NOT EXISTS actual_nano_cny   BIGINT
        CONSTRAINT billing_holds_actual_nano_check CHECK (actual_nano_cny >= 0),
    ADD COLUMN IF NOT EXISTS enforcement_mode  TEXT
        CONSTRAINT billing_holds_enforcement_mode_check CHECK (
            enforcement_mode IN ('shadow','registered','all')),
    ADD COLUMN IF NOT EXISTS denial_reason     TEXT;

DO $$
BEGIN
    -- -- subject_type 放宽（owner|user → owner|user|demo，§4.2） -- --
    -- 0020 的匿名内联 CHECK 被 PG 命名为 billing_holds_subject_type_check；
    -- 幂等：已放宽（重跑）时 DROP+ADD 同名同新义，行为不变。
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'billing_holds_subject_type_check'
           AND conrelid = 'billing_holds'::regclass) THEN
        EXECUTE 'ALTER TABLE billing_holds DROP CONSTRAINT '
                'billing_holds_subject_type_check';
    END IF;
    EXECUTE 'ALTER TABLE billing_holds ADD CONSTRAINT '
            'billing_holds_subject_type_check CHECK (subject_type IN '
            '(''owner'',''user'',''demo''))';

    -- -- 旧行兜底校验：0024 前的行不含 demo；新列全 NULL，不违反新 CHECK -- --
    IF EXISTS (SELECT 1 FROM billing_holds WHERE subject_type NOT IN
               ('owner','user','demo')) THEN
        RAISE EXCEPTION '0024 校验失败：存量 subject_type 超出词表';
    END IF;

    -- -- 迁移标志 audit（不含密钥；固定 event_id，重跑不重复） -- --
    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0024_billing_holds_spend_settle', now(), 'system',
         'billing.holds_spend_settle_applied', 'billing_holds',
         jsonb_build_object(
             'new_columns', to_jsonb(ARRAY[
                 'spend_window_id', 'reserved_nano_cny', 'actual_nano_cny',
                 'enforcement_mode', 'denial_reason']),
             'subject_type_allows_demo', true,
             'enforcement_mode_values', to_jsonb(ARRAY[
                 'shadow','registered','all']),
             'note', 'batch C: strong-consistency usage/hold settle chain; '
                     'legacy rows keep NULL spend columns and are treated '
                     'as shadow-compatible',
             'enforcement_mode_at_migration',
                 (SELECT value #>> '{}' FROM platform_settings
                   WHERE key = 'spend_enforcement_mode')))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
