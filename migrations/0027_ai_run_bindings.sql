-- =========================================================================== --
-- 0027_ai_run_bindings.sql：金额时代 run→主体权威绑定表 + 安全参数迁居
-- platform_settings（批次 F，docs/ai-money-budget-bugfix-and-simplification-plan.md
-- §7.3 阶段 2「旧 turn 消费控制面退役」）。
--
-- 语义变更：
--   - ai_run_bindings：金额预算硬闸（spend_enforcement_mode）覆盖的主体
--     （owner/user）起跑时不再写 ai_budget_reservations——run 与计费主体的
--     权威绑定改由本表承担（billing_store §7.2 主体解析第①步的新主源，
--     request_id 幂等 + 跨主体复用拒绝）。绑定在 HistoPilot 2xx 接受后写入
--     （on_accepted 携带 histopilot_session_id），供随后到达的 usage event /
--     hold authorize 解析；
--   - demo 主体的绑定**不**入本表：仍归 demo_runs.histopilot_session_id
--     （0026，capability_id 即 subject_id），本表 CHECK 限定 owner/user；
--   - ai_budget_* 表与列一律保留（冻结历史 + 只读报表兼容一个版本），
--     pre-F 历史事件的解析回退（reservations 查询）不受本迁移影响；
--   - 安全参数迁居：demo_enabled / platform_task_max_steps /
--     own_task_max_steps_limit / demo_task_max_steps / demo_max_concurrency
--     从 ai_budget_periods 列迁出到 platform_settings（键前缀 ai_safety.*，
--     JSONB 标量值），运行时读取统一走 settings_store（fail-closed 回落
--     budget_store.DEFAULT_* 常量）。下方 backfill 只搬当前开放周期行的值，
--     ON CONFLICT DO NOTHING（已有键不覆盖）；ai_budget_periods 上的原列
--     保留给软闸回退路径与冻结报表，不再被管理写入口更新。
--
-- 幂等/可重跑：CREATE TABLE IF NOT EXISTS + ON CONFLICT DO NOTHING；
-- 已迁移状态重跑不改变任何行。
-- 回滚：DROP TABLE ai_run_bindings；platform_settings 的 ai_safety.* 键
-- 删除即可（读取侧无该键时回落 DEFAULT_* 常量）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS ai_run_bindings (
    request_id            TEXT        PRIMARY KEY,  -- 客户端幂等键（与 usage event / hold 的 request_id 同值）
    subject_type          TEXT        NOT NULL
                          CHECK (subject_type IN ('owner', 'user')),
    subject_id            TEXT        NOT NULL,     -- owner: user_id|"owner"；user: user_id（demo 绑定归 demo_runs，不入本表）
    histopilot_session_id TEXT        NOT NULL,     -- HistoPilot 2xx 接受后绑定（解析第①步按 session 匹配）
    installation_id       TEXT,                     -- 可选上下文（histopilot installation），仅审计/排障用
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE ai_run_bindings IS
    '金额时代 run→主体权威绑定（批次 F）：替代 ai_budget_reservations 的绑定角色；demo 主体绑定仍在 demo_runs（0026）';
COMMENT ON COLUMN ai_run_bindings.request_id IS
    '跨主体复用拒绝：同 request_id 换主体写入由应用层校验拒绝（RequestIdSubjectConflict）';
COMMENT ON COLUMN ai_run_bindings.histopilot_session_id IS
    'HistoPilot 2xx 接受后绑定（on_accepted 写入）；解析按 session 匹配才 resolve';

CREATE INDEX IF NOT EXISTS idx_ai_run_bindings_session
    ON ai_run_bindings (histopilot_session_id);
COMMENT ON INDEX idx_ai_run_bindings_session IS
    '§7.2 主体解析第①步：session_id → run binding（session 匹配才 resolve）';

-- --------------------------------------------------------------------------- --
-- 安全参数迁居 backfill：当前开放周期行 → platform_settings（ai_safety.*）。
-- 无开放周期行时 SELECT 不产出行（不写键），读取侧回落 DEFAULT_* 常量
-- （与列 DDL 缺省一致：20 / 500 / 20 / false / 2）。
-- --------------------------------------------------------------------------- --
INSERT INTO platform_settings (key, value, updated_at, updated_by)
SELECT 'ai_safety.demo_enabled',
       to_jsonb(COALESCE(p.demo_enabled, FALSE)), now(), '0027_backfill'
FROM (SELECT demo_enabled FROM ai_budget_periods
      WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1) p
ON CONFLICT (key) DO NOTHING;

INSERT INTO platform_settings (key, value, updated_at, updated_by)
SELECT 'ai_safety.demo_task_max_steps',
       to_jsonb(COALESCE(p.demo_task_max_steps, 20)), now(), '0027_backfill'
FROM (SELECT demo_task_max_steps FROM ai_budget_periods
      WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1) p
ON CONFLICT (key) DO NOTHING;

INSERT INTO platform_settings (key, value, updated_at, updated_by)
SELECT 'ai_safety.platform_task_max_steps',
       to_jsonb(COALESCE(p.platform_task_max_steps, 20)), now(), '0027_backfill'
FROM (SELECT platform_task_max_steps FROM ai_budget_periods
      WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1) p
ON CONFLICT (key) DO NOTHING;

INSERT INTO platform_settings (key, value, updated_at, updated_by)
SELECT 'ai_safety.own_task_max_steps_limit',
       to_jsonb(COALESCE(p.own_task_max_steps_limit, 500)), now(), '0027_backfill'
FROM (SELECT own_task_max_steps_limit FROM ai_budget_periods
      WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1) p
ON CONFLICT (key) DO NOTHING;

INSERT INTO platform_settings (key, value, updated_at, updated_by)
SELECT 'ai_safety.demo_max_concurrency',
       to_jsonb(COALESCE(p.demo_max_concurrency, 2)), now(), '0027_backfill'
FROM (SELECT demo_max_concurrency FROM ai_budget_periods
      WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1) p
ON CONFLICT (key) DO NOTHING;
