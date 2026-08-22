-- =========================================================================== --
-- 0012_registration_invites.sql：邀请注册数据层（P0-B，docs
-- open-registration-security-remediation.md §4.2 / §3.7）
--
-- 新增实体：
--   - registration_invites：owner 签发的一次性邀请码。
--     * token 只存带域分离盐的 HMAC-SHA-256（token_hash UNIQUE），绝不存明文；
--       明文码仅在创建响应里返回一次（Cache-Control: no-store）。
--     * max_uses 固定 1（CHECK max_uses = 1）：一次性使用，不实现共享码。
--     * 默认绑定规范化邮箱（email_normalized 可空 = 不绑定，owner 显式高风险
--       选择）；兑换时常数时间比较。
--     * 默认 7 天过期（应用层 TTL 参数），owner 可提前 revoke。
--   - users.ai_access：注册用户平台 AI 访问开关。默认 TRUE 保持存量行为
--     （owner 线下创建的账号视为已授权）；邀请兑换创建的用户按邀请码模板
--     写入（默认 FALSE，docs §3.7「新邀请默认 ai_access=false」）。
--   - ai_budget_periods.owner_reserved_turn_limit / user_pool_turn_limit：
--     owner 保留池（user/Demo 不能消耗）与全部 user 共享池（docs §3.7 推荐测试
--     期默认：总 30 = owner 保留 10 + user 共享 15 + Demo 5；单 user 初始 3）。
--
-- registration_mode（closed|invite_only|public）存 platform_settings JSONB
-- （settings_store 读写），无 DDL；旧布尔 registration_open=true 的 fail-closed
-- 降级逻辑在 settings_store.get_registration_mode 内（不得自动映射为
-- invite_only/public）。
--
-- 全部幂等（IF NOT EXISTS / ADD COLUMN IF NOT EXISTS，对齐 0001-0011 风格）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS registration_invites (
    invite_id            TEXT        PRIMARY KEY,             -- 形如 inv_xxx
    token_hash           TEXT        NOT NULL UNIQUE,         -- 带域分离盐的 HMAC-SHA-256，绝不存明文
    email_normalized     TEXT,                                -- 绑定邮箱（小写规范化；NULL = 不绑定，高风险）
    created_by_user_id   TEXT        NOT NULL REFERENCES users(user_id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ NOT NULL,
    max_uses             INT         NOT NULL DEFAULT 1 CHECK (max_uses = 1),
    use_count            INT         NOT NULL DEFAULT 0,
    consumed_at          TIMESTAMPTZ,
    consumed_by_user_id  TEXT        REFERENCES users(user_id),
    revoked_at           TIMESTAMPTZ,
    ai_access            BOOLEAN     NOT NULL DEFAULT FALSE,  -- 邀请模板：新用户平台 AI 权限（默认关）
    cohort               TEXT        NOT NULL DEFAULT '',     -- 受控分组标签（owner 管理/审计用）
    note                 TEXT        NOT NULL DEFAULT ''      -- owner 备注脱敏；不存 token/密码
);
COMMENT ON TABLE registration_invites IS
    '邀请注册：一次性高熵邀请码，只存 token_hash；兑换与建号在同一事务（docs §4.2/§4.3）';
CREATE INDEX IF NOT EXISTS idx_registration_invites_created_at
    ON registration_invites (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_registration_invites_created_by
    ON registration_invites (created_by_user_id);

-- 注册用户平台 AI 开关（§3.7）：存量账号默认 TRUE（不改变现状），
-- 邀请兑换的新用户由 registration_store 按邀请模板显式写入（默认 FALSE）。
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS ai_access BOOLEAN NOT NULL DEFAULT TRUE;
COMMENT ON COLUMN users.ai_access IS
    '平台 AI 访问开关：邀请注册用户默认 FALSE，owner 显式授予（§3.7）';

-- AI 预算池隔离（§3.7）：owner 保留池 + user 共享池列。DDL 默认值仅作兜底，
-- 新周期创建时 budget_store 按 env 可配默认值显式写入。
ALTER TABLE ai_budget_periods
    ADD COLUMN IF NOT EXISTS owner_reserved_turn_limit INT NOT NULL DEFAULT 10;
ALTER TABLE ai_budget_periods
    ADD COLUMN IF NOT EXISTS user_pool_turn_limit INT NOT NULL DEFAULT 15;
COMMENT ON COLUMN ai_budget_periods.owner_reserved_turn_limit IS
    'owner 保留池：user/Demo 合计消耗不得超过 platform_turn_limit - 本值（docs §3.7）';
COMMENT ON COLUMN ai_budget_periods.user_pool_turn_limit IS
    '全部注册 user 共享的平台 AI 对话池（docs §3.7）';

-- 单 user 初始额度收紧到测试期推荐值 3（§3.7：邀请用户初始额度使用较小值）。
-- 仅调整未改过旧默认（10）的开放周期行，owner 自定义值不动。
ALTER TABLE ai_budget_periods
    ALTER COLUMN user_turn_limit SET DEFAULT 3;
UPDATE ai_budget_periods
    SET user_turn_limit = 3
    WHERE user_turn_limit = 10 AND closed_at IS NULL;
