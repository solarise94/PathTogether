-- =========================================================================== --
-- 0006_demo_budget_auth.sql：匿名 Demo / AI 预算 / 跨 worker 登录锁定数据层
-- （docs/demo-access-auth-ui-design.md §4.2/§4.3/§5.3/§9.3/§9.4/§9.5）
--
-- 新增实体：
--   - platform_settings：平台运行时设置（如 registration_open）。PG 权威，
--     env 只作首次部署 bootstrap 默认（docs §7.3）。json/dual 不可写。
--   - auth_rate_limits：登录防爆破权威记录。每账号与每 IP 前缀各一个独立计数器
--     （复合键防不住僵尸网络对单账号撞库），任一桶达阈值即锁定并保存
--     locked_until；两个 gunicorn worker 看到同一失败次数与锁定截止时间。
--   - ai_budget_periods / ai_budget_usage / ai_budget_reservations：平台 AI
--     预算周期、按主体聚合用量、request_id 幂等预占。预占在单事务内原子判定
--     （平台总量 + Demo 子量 + 每 user 量），禁止先扣一个维度再失败。
--   - demo_sessions：匿名 Demo capability（token 只存 hash）+ 一次性 run 的
--     available|reserved|consumed 状态机（reserved 默认 10 分钟过期，惰性回收）。
--   - demo_catalog：Demo 切片目录（owner allowlist，独立于 public 语义）。
--
-- 索引备注：
--   - demo_sessions(token_hash) 由 UNIQUE 约束覆盖；
--   - ai_budget_reservations(state, reservation_expires_at) 与
--     demo_sessions(run_state, reservation_expires_at) 供惰性回收/对账扫描；
--   - ai_budget_usage(period_id, credential_source) 供每次预占的平台总量聚合。
--
-- 全部幂等（IF NOT EXISTS，对齐 0001-0005 风格）。这些能力仅在
-- STORAGE_BACKEND=postgres 时开放（json/dual fail-closed，见 platform_features），
-- 但表结构随主 schema 一并建好，切换后端无需二次迁移。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS platform_settings (
    key        TEXT        PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ,
    updated_by TEXT
);
COMMENT ON TABLE platform_settings IS
    '平台运行时设置（PG 权威）：env 只作 bootstrap 默认，owner 可在后台修改';

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    scope             TEXT        NOT NULL,   -- account | ip_prefix
    subject_hash      TEXT        NOT NULL,   -- 带盐 hash，绝不存明文账号/完整 IP
    window_started_at TIMESTAMPTZ,
    failed_count      INT         NOT NULL DEFAULT 0,
    locked_until      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ,
    PRIMARY KEY (scope, subject_hash)
);
COMMENT ON TABLE auth_rate_limits IS
    '登录防爆破权威记录：账号桶与 IP 前缀桶独立计数，任一达阈值即锁定（docs §6.3/§9.5）';

CREATE TABLE IF NOT EXISTS ai_budget_periods (
    id                       SERIAL      PRIMARY KEY,
    started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at                TIMESTAMPTZ,
    platform_turn_limit      INT         NOT NULL DEFAULT 30,   -- 平台 AI 总对话额度
    demo_turn_limit          INT         NOT NULL DEFAULT 5,    -- Demo 子额度（含在总量内）
    user_turn_limit          INT         NOT NULL DEFAULT 10,   -- 每注册用户对话额度
    platform_task_max_steps  INT         NOT NULL DEFAULT 20,   -- 注册用户平台 AI 单次步数
    own_task_max_steps_limit INT         NOT NULL DEFAULT 500,  -- 自带 API 步数硬上限
    demo_task_max_steps      INT         NOT NULL DEFAULT 10,   -- Demo 单次任务步数
    demo_enabled             BOOLEAN     NOT NULL DEFAULT FALSE,
    demo_per_browser_limit   INT         NOT NULL DEFAULT 1,
    demo_max_concurrency     INT         NOT NULL DEFAULT 2,
    created_by               TEXT
);
COMMENT ON TABLE ai_budget_periods IS
    'AI 预算周期：closed_at IS NULL 即当前开放周期；reset 关旧开新，旧行与用量保留（docs §4.2/§9.4）';

CREATE TABLE IF NOT EXISTS ai_budget_usage (
    period_id         INT         NOT NULL REFERENCES ai_budget_periods(id),
    subject_type      TEXT        NOT NULL,   -- owner | user | demo
    subject_id        TEXT        NOT NULL,
    credential_source TEXT        NOT NULL,   -- platform | own
    accepted_turns    INT         NOT NULL DEFAULT 0,
    reserved_turns    INT         NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ,
    PRIMARY KEY (period_id, subject_type, subject_id, credential_source)
);
COMMENT ON TABLE ai_budget_usage IS
    '按主体聚合用量：平台总额度 = 同 period 内 credential_source=platform 的 Σ(accepted+reserved)；own 仅可观测';
CREATE INDEX IF NOT EXISTS idx_ai_budget_usage_period
    ON ai_budget_usage (period_id, credential_source);

CREATE TABLE IF NOT EXISTS ai_budget_reservations (
    request_id               TEXT        PRIMARY KEY,  -- 幂等键（客户端 UUID，重放不重复扣）
    period_id                INT         NOT NULL,
    subject_type             TEXT        NOT NULL,     -- owner | user | demo
    subject_id               TEXT        NOT NULL,
    credential_source        TEXT        NOT NULL,     -- platform | own
    state                    TEXT        NOT NULL,     -- reserved | consumed | released
    reserved_at              TIMESTAMPTZ,
    reservation_expires_at   TIMESTAMPTZ,
    histopilot_session_id     TEXT,
    updated_at               TIMESTAMPTZ
);
COMMENT ON TABLE ai_budget_reservations IS
    'AI 预占：request_id 幂等；reserved 过期惰性回收；已 consumed 拒绝释放（防误退款）（docs §5.3/§9.4）';
CREATE INDEX IF NOT EXISTS idx_ai_budget_reservations_state_expires
    ON ai_budget_reservations (state, reservation_expires_at);

CREATE TABLE IF NOT EXISTS demo_sessions (
    id                      TEXT        PRIMARY KEY,
    token_hash              TEXT        NOT NULL UNIQUE,  -- capability 明文绝不落库
    created_at              TIMESTAMPTZ,
    expires_at              TIMESTAMPTZ,
    run_state               TEXT        NOT NULL DEFAULT 'available',  -- available|reserved|consumed
    reserved_at             TIMESTAMPTZ,
    reservation_expires_at  TIMESTAMPTZ,
    consumed_at             TIMESTAMPTZ,
    histopilot_session_id   TEXT,
    slide_id                TEXT,        -- Demo allowlist 校验在应用层（slides 删除先于目录清理时不被 FK 阻断）
    asset_revision          TEXT,        -- run 创建时绑定，禁止同名替换后继续复用
    request_id              TEXT,
    ip_prefix_hash          TEXT         -- 可选、轮换盐，仅辅助限流
);
COMMENT ON TABLE demo_sessions IS
    '匿名 Demo capability：每浏览器限 1 个主 run，reserved 默认 10 分钟过期（docs §5.2/§5.3/§9.3）';
CREATE INDEX IF NOT EXISTS idx_demo_sessions_run_state_expires
    ON demo_sessions (run_state, reservation_expires_at);

CREATE TABLE IF NOT EXISTS demo_catalog (
    slide_id     TEXT        PRIMARY KEY,   -- 对应 slides.slide_id（存在性在应用层校验）
    display_name TEXT,
    description  TEXT,
    sort_order   INT         NOT NULL DEFAULT 0,
    is_default   BOOLEAN     NOT NULL DEFAULT FALSE,
    added_by     TEXT,
    added_at     TIMESTAMPTZ
);
COMMENT ON TABLE demo_catalog IS
    'Demo 切片目录（owner allowlist）：独立于 public 语义，移除时联动撤销 capability（docs §5.1/§9.3）';
