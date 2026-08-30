-- =========================================================================== --
-- 0026_demo_runs.sql：Demo capability 与 run 分离 + IP 短窗口请求速率桶
-- （批次 E，docs/ai-money-budget-bugfix-and-simplification-plan.md §4/§8-E）。
--
-- 语义变更（§4.1）：
--   - demo_sessions 回归纯 capability 载体：匿名 token hash、过期时间、
--     IP 前缀 hash。run_state 一次性状态机（available|reserved|consumed）
--     退出新写入路径——历史行保留原语义可读（本迁移不改 demo_sessions 任何
--     列/约束），新 run 一律落 demo_runs 流水；
--   - demo_runs：每次 run 独立流水行（append-only / 终态保留：
--     reserved|accepted|finished|released|expired）。同 capability 可顺序
--     多次 run（终态后即可再开），同 capability 同时最多一个
--     reserved/accepted run——由部分唯一索引在 DB 层硬保证，不靠应用层自觉；
--   - UNIQUE(capability_id, request_id)：同 capability 同 request_id 只有一行
--     （released 后同 ID 重试走 UPDATE 复位，attempt+1，防 ABA）；
--   - capability 过期不能新开 run（应用层在 capability 行锁内校验
--     expires_at > now），既有终态流水永久保留；
--   - IP 保护从「24 小时成功 run 次数桶（0008 索引，本批退役）」改为
--     「每 IP 前缀每分钟请求数」短窗口防刷/防 DoS 桶（demo_ip_request_rate，
--     固定窗口计数，PG 权威，不累计成功次数、不构成消费额度）。
--
-- 支撑索引：
--   - uq_demo_runs_single_active：部分唯一索引 = 同 capability 单 active run
--     的数据库级约束（并发第二个 INSERT 直接 UniqueViolation）；
--   - idx_demo_runs_state_expires：对账/惰性过期扫描（reserved|accepted 且
--     expires_at 到期）；
--   - idx_demo_runs_session：billing_store §7.2 主体解析第②步
--     （session_id → demo_runs.histopilot_session_id → capability id）；
--   - idx_demo_runs_slide_active：revoke_by_slide 终止在途 run 的定位扫描。
--
-- 幂等/可重跑：IF NOT EXISTS；已迁移状态重跑不改变任何行。
-- 回滚：DROP TABLE demo_runs / demo_ip_request_rate 即可（新表无被依赖对象；
-- 0026 之后若已有 demo_runs 流水，回滚即放弃该流水——capability 一次性
-- 状态机自 0026 起不再写入，回滚后旧 run_state 列值即最终状态）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS demo_runs (
    demo_run_id           TEXT        PRIMARY KEY,
    capability_id         TEXT        NOT NULL,   -- demo_sessions.id（行不删除，应用层校验存在与过期）
    request_id            TEXT        NOT NULL,   -- 客户端幂等键（与 ai_budget_reservations 同值）
    state                 TEXT        NOT NULL DEFAULT 'reserved'
                          CHECK (state IN ('reserved', 'accepted', 'finished',
                                           'released', 'expired')),
    histopilot_session_id TEXT,                    -- HistoPilot 接受（2xx）后绑定
    slide_id              TEXT,                    -- run 创建时绑定（allowlist 校验在应用层）
    asset_revision        TEXT,                    -- 同上：禁止同名替换后继续复用
    attempt               INT         NOT NULL DEFAULT 1,   -- released 后同 ID 重试递增（防 ABA）
    rollback_epoch        INT         NOT NULL DEFAULT 0,   -- 在途重放递增：原请求 release CAS 失效
    ip_prefix_hash        TEXT,                    -- 可选、轮换盐，仅辅助限流/审计
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at           TIMESTAMPTZ,             -- HistoPilot 接受时刻（重连窗口基准）
    finished_at           TIMESTAMPTZ,             -- 流正常结束（终态 finished）
    expires_at            TIMESTAMPTZ NOT NULL,    -- reserved：预占 TTL；accepted：重连窗口
    UNIQUE (capability_id, request_id)
);
COMMENT ON TABLE demo_runs IS
    'Demo run 流水（批次 E §4.1）：同 capability 顺序多次、同时至多一个 active（部分唯一索引硬约束）；终态保留';
COMMENT ON COLUMN demo_runs.state IS
    'reserved=已预占未接受 accepted=HistoPilot 已接受 finished=流正常结束 released=接受前失败/放弃 expired=窗口到期惰性终态';

CREATE UNIQUE INDEX IF NOT EXISTS uq_demo_runs_single_active
    ON demo_runs (capability_id)
    WHERE state IN ('reserved', 'accepted');
COMMENT ON INDEX uq_demo_runs_single_active IS
    '同 capability 同时最多一个 reserved/accepted run（DB 级约束，批次 E §4.1）';

CREATE INDEX IF NOT EXISTS idx_demo_runs_state_expires
    ON demo_runs (state, expires_at);
COMMENT ON INDEX idx_demo_runs_state_expires IS
    '确认式对账/惰性过期扫描：active 状态且 expires_at 到期';

CREATE INDEX IF NOT EXISTS idx_demo_runs_session
    ON demo_runs (histopilot_session_id)
    WHERE histopilot_session_id IS NOT NULL;
COMMENT ON INDEX idx_demo_runs_session IS
    '§7.2 主体解析第②步：session_id → demo run → capability id（0026 起 demo 主体绑定源）';

CREATE INDEX IF NOT EXISTS idx_demo_runs_slide_active
    ON demo_runs (slide_id)
    WHERE state IN ('reserved', 'accepted');
COMMENT ON INDEX idx_demo_runs_slide_active IS
    'revoke_by_slide：定位并终止该切片的在途 demo run（capability 多切片复用，不整体失效）';

-- --------------------------------------------------------------------------- --
-- Demo IP 短窗口请求速率桶（§1.2/§4.1：防刷/防 DoS，不是消费额度）
-- 固定窗口计数：window_started_at 距今超过窗口长度即整桶重置。
-- 上限经 env DEMO_IP_RATE_PER_MINUTE 调整（demo_store.ip_rate_limit），
-- ≤0 关闭。json/dual 后端 fail-closed（platform_features）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS demo_ip_request_rate (
    ip_prefix_hash    TEXT        PRIMARY KEY,  -- 带盐 hash，绝不存完整 IP
    window_started_at TIMESTAMPTZ NOT NULL,
    request_count     INT         NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ
);
COMMENT ON TABLE demo_ip_request_rate IS
    'Demo 每 IP 前缀每分钟请求数固定窗口计数（批次 E：替代 24h 成功 run 桶；仅防刷，不累计成功次数）';
