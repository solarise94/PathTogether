-- =========================================================================== --
-- 0029_user_total_allowances_and_denials.sql：注册 user 一次性总额度 + 金额
-- 拒绝事件（Batch B，docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
-- §Batch B 数据模型 1-7 / §3.1 / §3.2）。
--
-- 语义：
--
--   ai_spend_total_defaults（§Batch B 数据模型 1）：
--     - 单例配置表（singleton 恒 'global'）：新注册 user 的默认总额度 X、
--       version/CAS 与审计元数据。只有 role=user 使用它，owner/demo 不读；
--     - **本迁移不 seed 面值**：X 在 cutover apply 时从当时有效的
--       user_default 策略面值写入（scripts/cutover_user_total_allowances.py），
--       不硬编码新金额；store 读取缺行时回退 user_default 策略面值
--       （fail-safe，见 spend_store._resolve_total_default_tx 注释）。
--
--   ai_spend_total_allowances（数据模型 2）：每个注册 user 至多一行
--     （subject_id UNIQUE）。授权不等式 spent + reserved + estimated <=
--     limit；X 是绝对总上限——修改 X 只改 limit，绝不清 zero spent/reserved；
--     opening_spent 是迁移基线（cutover 时刻窗口 spent 快照），之后不再变化；
--     无周期、无轮换、无月初重建（§Batch B 迁移与额度语义 8）。subject_type
--     固定 'user'（CHECK 硬性拒绝 owner/demo 写入，DB 层兜底）。
--
--   billing_holds.spend_total_allowance_id（数据模型 3）：新 user hold 只绑
--     总额度行；demo/owner hold 只绑 spend_window_id。CHECK 兼容迁移前历史
--     hold（允许两目标皆 NULL 的 legacy 行），禁止两目标同时非 NULL；新代码
--     创建的 hold 由 store/真实 PG contract 强制恰好一个目标并校验主体匹配。
--
--   platform_settings 幂等 seed（数据模型 4）：user_spend_target="window"
--   （部署初期双目标代码并存但行为不变）；ai_dispatch_maintenance=false
--   （cutover 维护闸）。ON CONFLICT DO NOTHING，不覆盖既有值。
--
--   registration_invites.total_limit_nano_cny（数据模型 6）：邀请模板初始
--   总额度列（可空 = 兑换用户由 cutover/默认处理）；monthly 列保留只读兼容
--   一个发布周期，不在本迁移删除。
--
--   ai_spend_denial_events（数据模型 7）：append-only 金额硬拒绝事件——
--   authorize_hold 在 ROLLBACK TO SAVEPOINT sp_hold_business 之后、外层事务
--   提交之前复用原始 call_id 写入；唯一约束 (call_id, reason) 保证客户端
--   重试不重复计数。不保存 prompt/完整错误堆栈/患者数据。无 FK：hard 拒绝
--   不落 billing_holds 行，事件独立存在。
--
-- 幂等/可重跑：全部 IF NOT EXISTS / ON CONFLICT DO NOTHING / DO 块按约束名
-- 判存；种子对「已迁移」状态重跑不改变任何行。单迁移单事务。
-- 回滚：本迁移只加表/列/种子，紧急回滚不 DROP（§8.2）；总额度硬切换后的
-- 回滚必须先跑 scripts/cutover_user_total_allowances.py --mode rollback-plan
-- （先 reconcile、合并消费到新月窗口，再 CAS 切回 window），禁止直接切 flag。
-- =========================================================================== --

-- --------------------------------------------------------------------------- #
-- 1. 单例默认总额度配置（新 user 开户模板；不 seed 面值）
-- --------------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS ai_spend_total_defaults (
    singleton              TEXT        NOT NULL DEFAULT 'global'
                                       CONSTRAINT ai_spend_total_defaults_singleton_check
                                       CHECK (singleton = 'global'),
    default_limit_nano_cny BIGINT      NOT NULL CHECK (default_limit_nano_cny >= 0),
    version                BIGINT      NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by             TEXT,
    CONSTRAINT ai_spend_total_defaults_pkey PRIMARY KEY (singleton)
);
-- 单例：至多一行（PK 即 singleton，值恒 'global'）
COMMENT ON TABLE ai_spend_total_defaults IS
    '新注册 user 默认总额度 X（Batch B §Batch B 数据模型 1，单例）：只有 role=user 使用；面值由 cutover 从当时有效 user_default 策略写入，不硬编码；缺行时 store 回退 user_default 策略面值（fail-safe）；修改默认不追溯既有 user';

-- --------------------------------------------------------------------------- #
-- 2. 每用户唯一总额度投影（权威授权数据；无周期无轮换）
-- --------------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS ai_spend_total_allowances (
    allowance_id           TEXT        PRIMARY KEY,   -- sta_<24hex>
    -- subject_type 固定 'user'：owner/demo 恒走窗口（§3.1），DB 层拒绝其他
    -- 主体写法（store 断言 + CHECK 双保险中的 CHECK 半边）
    subject_type           TEXT        NOT NULL DEFAULT 'user'
                                       CONSTRAINT ai_spend_total_allowances_subject_check
                                       CHECK (subject_type = 'user'),
    subject_id             TEXT        NOT NULL UNIQUE,   -- user_id
    limit_nano_cny         BIGINT      NOT NULL CHECK (limit_nano_cny >= 0),
    opening_spent_nano_cny BIGINT      NOT NULL DEFAULT 0
                                       CHECK (opening_spent_nano_cny >= 0),
    spent_nano_cny         BIGINT      NOT NULL DEFAULT 0
                                       CHECK (spent_nano_cny >= 0),
    reserved_nano_cny      BIGINT      NOT NULL DEFAULT 0
                                       CHECK (reserved_nano_cny >= 0),
    -- 建行来源：cutover=月窗口迁移；invite=邀请模板；admin_create=owner 建号
    source                 TEXT        NOT NULL
                                       CONSTRAINT ai_spend_total_allowances_source_check
                                       CHECK (source IN ('cutover','invite',
                                                         'admin_create')),
    -- 建行时刻的全局默认 version（未读默认 NULL；审计用，不参与授权）
    default_version        BIGINT,
    version                BIGINT      NOT NULL DEFAULT 1 CHECK (version >= 1),
    -- cutover 建行时刻（invite/admin_create 行为 NULL——无源窗口可指向）
    cutover_at             TIMESTAMPTZ,
    -- 源窗口（source='cutover' 必填；旧窗口冻结不删，引用仅审计投影）
    source_window_id       TEXT,
    source_window_version  BIGINT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by             TEXT,
    -- cutover 行必须能追溯源窗口与切换时刻（回滚审计链）
    CONSTRAINT ai_spend_total_allowances_cutover_provenance_check
        CHECK (source <> 'cutover' OR (cutover_at IS NOT NULL
                                       AND source_window_id IS NOT NULL
                                       AND source_window_version IS NOT NULL))
);
COMMENT ON TABLE ai_spend_total_allowances IS
    '注册 user 一次性总额度（Batch B §3.1，每 user 唯一）：授权不等式 spent+reserved+estimated<=limit；X 是绝对总上限，修改只改 limit 不清 spent/reserved；opening_spent 是迁移基线不再变化；无周期无轮换';
CREATE INDEX IF NOT EXISTS idx_ai_spend_total_allowances_subject
    ON ai_spend_total_allowances (subject_id);

-- --------------------------------------------------------------------------- #
-- 3. billing_holds 增加总额度目标（与 spend_window_id 互斥）
-- --------------------------------------------------------------------------- #
ALTER TABLE billing_holds
    ADD COLUMN IF NOT EXISTS spend_total_allowance_id TEXT;

DO $$
BEGIN
    -- 互斥 CHECK（幂等：按约束名判存）：禁止两目标同时非 NULL；允许皆 NULL
    -- （0024 之前的历史 hold 行语义不变）
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'billing_holds_spend_target_mutex_check'
           AND conrelid = 'billing_holds'::regclass) THEN
        EXECUTE 'ALTER TABLE billing_holds ADD CONSTRAINT '
                'billing_holds_spend_target_mutex_check CHECK (NOT ('
                'spend_window_id IS NOT NULL AND '
                'spend_total_allowance_id IS NOT NULL))';
    END IF;
END $$;

-- --------------------------------------------------------------------------- #
-- 4. platform_settings 幂等 seed：user_spend_target / ai_dispatch_maintenance
-- --------------------------------------------------------------------------- #
INSERT INTO platform_settings (key, value, updated_at, updated_by)
VALUES
    ('user_spend_target', '"window"'::jsonb, now(), 'migration-0029'),
    ('ai_dispatch_maintenance', 'false'::jsonb, now(), 'migration-0029')
ON CONFLICT (key) DO NOTHING;

-- --------------------------------------------------------------------------- #
-- 5. registration_invites.total_limit_nano_cny（邀请模板初始总额度）
-- --------------------------------------------------------------------------- #
ALTER TABLE registration_invites
    ADD COLUMN IF NOT EXISTS total_limit_nano_cny BIGINT
        CONSTRAINT registration_invites_total_limit_check
        CHECK (total_limit_nano_cny >= 0);

-- --------------------------------------------------------------------------- #
-- 6. 金额硬拒绝事件（append-only；无 prompt/堆栈/患者数据）
-- --------------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS ai_spend_denial_events (
    denial_id          TEXT        PRIMARY KEY,   -- den_<24hex>
    call_id            TEXT        NOT NULL
                                   CHECK (call_id ~ '^call_[0-9a-f]{32}$'),
    subject_type       TEXT        NOT NULL
                                   CHECK (subject_type IN ('owner','user','demo')),
    subject_id         TEXT        NOT NULL,
    reason             TEXT        NOT NULL,   -- 稳定拒绝码（spend_* / pricing_*）
    estimated_nano_cny BIGINT      CHECK (estimated_nano_cny >= 0),
    occurred_at        TIMESTAMPTZ NOT NULL,
    -- 客户端同 call_id 重试不重复计数
    CONSTRAINT ai_spend_denial_events_call_reason_unique
        UNIQUE (call_id, reason)
);
COMMENT ON TABLE ai_spend_denial_events IS
    '金额硬拒绝事件（Batch B §4.6 Demo 统计数据源，append-only）：authorize_hold 在 ROLLBACK TO SAVEPOINT sp_hold_business 之后写入（不能在会随 savepoint 回滚的事务段内）；(call_id, reason) 唯一保证重试不重复计数；数据库整体不可用类拒绝不落本表（仅外部 metric）';
CREATE INDEX IF NOT EXISTS idx_ai_spend_denial_events_subject_time
    ON ai_spend_denial_events (subject_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ai_spend_denial_events_reason
    ON ai_spend_denial_events (subject_type, reason, occurred_at);

-- --------------------------------------------------------------------------- #
-- 迁移标志 audit（不含敏感信息；固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- #
INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0029_user_total_allowances', now(), 'system',
     'spend.total_allowances_applied', 'ai_spend_total_allowances',
     jsonb_build_object(
         'new_tables', to_jsonb(ARRAY[
             'ai_spend_total_defaults', 'ai_spend_total_allowances',
             'ai_spend_denial_events']),
         'new_columns', to_jsonb(ARRAY[
             'billing_holds.spend_total_allowance_id',
             'registration_invites.total_limit_nano_cny']),
         'hold_target_mutex_check', 'billing_holds_spend_target_mutex_check',
         'user_spend_target', 'window',
         'ai_dispatch_maintenance', false,
         'total_default_seeded', false,
         'note', 'batch B: user one-shot total allowances + denial events; '
                 'deploy keeps user_spend_target=window until controlled '
                 'cutover; default X is seeded by cutover script from the '
                 'effective user_default policy, not hardcoded here'))
ON CONFLICT (event_id) DO NOTHING;
