-- =========================================================================== --
-- 0007_reservation_attempt.sql：reservation / demo run 的 attempt 版本栅栏
--
-- 对账先反查 HistoPilot 得到 abandoned，再 release。同 request_id 重试若在
-- 查询后、释放前重新启动动作，旧结果会把新尝试退款。给
-- ai_budget_reservations 与 demo_sessions 增加单调 attempt，release/consume
-- 按 expected_attempt CAS；同 ID 的 reserved 重放递增 attempt。
-- =========================================================================== --

ALTER TABLE ai_budget_reservations
    ADD COLUMN IF NOT EXISTS attempt INT NOT NULL DEFAULT 1;
COMMENT ON COLUMN ai_budget_reservations.attempt IS
    '执行尝试版本：reserved 同 ID 重放或 released 再预占时递增；release/consume CAS 用';

ALTER TABLE demo_sessions
    ADD COLUMN IF NOT EXISTS attempt INT NOT NULL DEFAULT 1;
COMMENT ON COLUMN demo_sessions.attempt IS
    'Demo run 尝试版本：同 request_id 的 reserved 重放递增；release_run/consume_run CAS 用';
