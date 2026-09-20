-- =========================================================================== --
-- 0058_demo_task_max_steps_100.sql：Demo 单次任务默认 20 → 100 步
--
-- 工单 C（docs viewer-demo-collaboration-review-plan-20260919.md §4）：
--   - 新契约：budget_store.DEFAULT_DEMO_TASK_MAX_STEPS 自本批起 = 100（对齐
--     注册用户 platform_task_max_steps 的 1..100 契约；app._demo_task_max_steps
--     运行时仍钳制 _MAX_STEPS_LIMIT=100，只读工具 / 单任务 / 预算闸不变）；
--   - 0010 先例（只抬仍停在旧缺省的行）：本迁移改列缺省，把仍停在旧缺省
--     20（或更早 10）的**开放**周期抬到 100；owner 已改成其他值的行不动；
--   - 权威设置在 platform_settings 键 ai_safety.demo_task_max_steps（批次 F
--     迁居；ai_budget_periods 列仅历史展示）。存量 JSON 标量 20/10 抬到 100，
--     非默认管理员自定义值（如 5、30、50）一律保留；
--
-- 保留规则（部署核对用；迁移内不 SELECT 落表，避免把设置值复制进审计）：
--   自定义值识别：UPDATE WHERE 只命中 jsonb_typeof(value)='number' 且
--   value IN (20, 10) 的行；改过的值不在 (20, 10) 内即自然保留。部署后核对：
--     SELECT value FROM platform_settings
--      WHERE key = 'ai_safety.demo_task_max_steps';
--   若返回非 100 的数字 → 是管理员显式自定义值，保留（运行时仍被
--   _MAX_STEPS_LIMIT=100 钳制，不放大权限）。
--
-- 幂等/可重跑：UPDATE 带 WHERE 条件，重跑 0 行更新；audit 固定 event_id +
-- ON CONFLICT DO NOTHING（0043 同款）。回滚不自动：100 是新契约合法值，恢复
-- 旧值以审计 detail 的 previous 为依据显式 UPDATE。
-- =========================================================================== --

ALTER TABLE ai_budget_periods
    ALTER COLUMN demo_task_max_steps SET DEFAULT 100;

UPDATE ai_budget_periods
   SET demo_task_max_steps = 100
 WHERE closed_at IS NULL
   AND demo_task_max_steps IN (20, 10);

COMMENT ON COLUMN ai_budget_periods.demo_task_max_steps IS
    'Demo 单次任务步数（默认 100，与平台 AI 单次步数上限对齐；权威值在 platform_settings ai_safety.demo_task_max_steps）';

DO $$
DECLARE
    v_previous jsonb;
    v_updated  int;
BEGIN
    -- 归一前旧值快照（无匹配行 → '{}'；供审计与人工回滚定位）
    SELECT COALESCE(jsonb_object_agg(key, (value #>> '{}')::numeric), '{}'::jsonb)
      INTO v_previous
      FROM platform_settings
     WHERE key = 'ai_safety.demo_task_max_steps'
       AND jsonb_typeof(value) = 'number'
       AND (value #>> '{}')::numeric IN (20, 10);

    -- 仍停在旧缺省（20/10 JSONB 标量）的设置抬到 100；自定义值保留
    UPDATE platform_settings
       SET value      = '100'::jsonb,
           updated_at = now(),
           updated_by = 'migration-0058'
     WHERE key = 'ai_safety.demo_task_max_steps'
       AND jsonb_typeof(value) = 'number'
       AND (value #>> '{}')::numeric IN (20, 10);
    GET DIAGNOSTICS v_updated = ROW_COUNT;

    -- 迁移标志 audit（固定 event_id，重跑不重复；0043 同款）
    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0058_demo_task_max_steps_100', now(), 'system',
         'settings.demo_task_max_steps_default_100', 'platform_settings',
         jsonb_build_object(
             'keys', to_jsonb(ARRAY['ai_safety.demo_task_max_steps']),
             'new_default', 100,
             'updated_rows', v_updated,
             'previous_old_default', v_previous,
             'note', 'demo single-run step default raised 20->100 (work '
                     'order C, plan 2026-09-19 section 4); admin-custom '
                     'values outside (20, 10) preserved; runtime still '
                     'clamped to _MAX_STEPS_LIMIT=100'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
