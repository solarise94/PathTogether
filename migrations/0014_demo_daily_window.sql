-- =========================================================================== --
-- 0014_demo_daily_window.sql：Demo 子池改「每日（滚动 24 小时窗口）」口径
--
-- 语义变更（docs/open-registration-security-remediation.md §3.7）：
--   - demo_turn_limit 从「预算周期内累计上限（默认 5）」改为「单日上限
--     （默认 50）」：闸按 ai_budget_reservations 流水的滚动 24h 窗口计数
--     （state IN (consumed, reserved)；released/过期回收不计），不再按周期
--     累计，也不因周期重置清零——修复周期不自动滚动时子池跑满后永久熄火；
--   - 每日口径与 platform/user_pool/owner_reserve 的周期口径不再可比，
--     app 层已移除「demo_turn_limit <= platform_turn_limit」及含 demo 的
--     周期加和约束（user_pool + owner_reserve <= platform 仍保留）。
--
-- 本迁移只改列缺省值与注释，不回填存量行：已存在的开放周期 demo_turn_limit
-- 保持原值（旧值按新口径解释为「单日上限」），由 owner 视需要调整或重置。
-- =========================================================================== --

ALTER TABLE ai_budget_periods
    ALTER COLUMN demo_turn_limit SET DEFAULT 50;
COMMENT ON COLUMN ai_budget_periods.demo_turn_limit IS
    'Demo 每日子额度上限：滚动 24h 窗口计数判定（0014 起，不按周期累计）';
