-- =========================================================================== --
-- 0008_demo_ip_run_rate.sql：Demo AI run 按 IP 前缀限流的查询索引
--
-- 公网 Demo 的每浏览器次数不能阻止清 cookie 换 capability。对账与预算仍按
-- request_id；本索引支撑按 ip_prefix_hash 统计 reserved/consumed run。
-- =========================================================================== --

CREATE INDEX IF NOT EXISTS idx_demo_sessions_ip_prefix_runs
    ON demo_sessions (ip_prefix_hash, reserved_at)
    WHERE ip_prefix_hash IS NOT NULL
      AND run_state IN ('reserved', 'consumed');
COMMENT ON INDEX idx_demo_sessions_ip_prefix_runs IS
    'Demo AI run IP 前缀限流：统计同前缀 reserved/consumed 次数';
