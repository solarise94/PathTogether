-- =========================================================================== --
-- 0025_invite_monthly_limit.sql：邀请码月额度覆盖模板（批次 D，docs
-- ai-money-budget-bugfix-and-simplification-plan.md §5.2/§8 批次 D）。
--
-- 语义：
--   - registration_invites 新增可选列 monthly_limit_nano_cny（BIGINT，NULL =
--     兑换时新用户继承全局 user_default 策略；非 NULL = 兑换事务内为新用户
--     创建 user_override 金额策略，面值即该 nano-CNY 值）；
--   - 金额口径与其余金额列一致：BIGINT nano-CNY（1 CNY = 1e9 nano），CHECK
--     非负（0 = 兑换用户首月即无可用额度，是合法的运维选择，不伪造语义）；
--   - 管理面写入（admin.invites.create）只接受十进制字符串 wire 形态并在
--     路由层校验（app.py _admin_v1_amount_in），DB 层不承担 wire 形态。
--
-- 幂等/可重跑：IF NOT EXISTS；已迁移状态重跑不改变任何行。
-- 回滚：删除本列即可（列仅是邀请模板，无外键/投影依赖；已兑换产生的
-- override 策略在 ai_spend_policies，回滚本列不影响其历史行）。
-- =========================================================================== --

ALTER TABLE registration_invites
    ADD COLUMN IF NOT EXISTS monthly_limit_nano_cny BIGINT
    CHECK (monthly_limit_nano_cny IS NULL OR monthly_limit_nano_cny >= 0);
COMMENT ON COLUMN registration_invites.monthly_limit_nano_cny IS
    '邀请模板：兑换时为新用户创建 user_override 月额度的 nano-CNY 面值（NULL=继承全局 user_default；批次 D §5.2）';
