-- =========================================================================== --
-- 0009_rollback_epoch.sql：在途 reserved 重放使旧 rollback CAS 失效
--
-- replayed 只是 reserve_turn / reserve_run 返回给后续调用者的瞬时字段，
-- 原请求闭包仍持有 replayed=false。原连接随后失败仍会 release，重放的
-- consume 就会撞 released。attempt 在 reserved 重放时故意不升（原执行
-- 仍可 consume），因此另增 rollback_epoch：仅在 reserved 重放时 +1，
-- 原请求带捕获到的 epoch 做 release CAS 即失败；确认式对账不传 epoch，
-- 仍可在 HistoPilot missing/abandoned 后释放。
-- =========================================================================== --

ALTER TABLE ai_budget_reservations
    ADD COLUMN IF NOT EXISTS rollback_epoch INT NOT NULL DEFAULT 0;
COMMENT ON COLUMN ai_budget_reservations.rollback_epoch IS
    '在途 reserved 重放递增；release 可选 CAS。consume 不校验本字段';

ALTER TABLE demo_sessions
    ADD COLUMN IF NOT EXISTS rollback_epoch INT NOT NULL DEFAULT 0;
COMMENT ON COLUMN demo_sessions.rollback_epoch IS
    'Demo 在途 reserved 重放递增；release_run 可选 CAS。consume_run 不校验';
