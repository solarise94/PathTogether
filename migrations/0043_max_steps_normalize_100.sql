-- =========================================================================== --
-- 0043_max_steps_normalize_100.sql：存量 >100 步数设置的显式、受审计归一
-- （2026-09-10 修复规格 §2 A，docs fix-2026-09-10-steps-drawing-activate.md；
-- 仿 0031_max_steps_normalize.sql 先例）。
--
-- 语义：
--   - 注册 user 的单任务步数（ai_safety.platform_task_max_steps）与自带 API
--     步数上限（ai_safety.own_task_max_steps_limit）的新契约是 1..100：默认值
--     与硬上限均为 100（budget_store.DEFAULT_PLATFORM_TASK_MAX_STEPS /
--     DEFAULT_OWN_TASK_MAX_STEPS_LIMIT 自本批起 = 100）；
--   - 旧契约（Batch C 1..500）留下的 500 及更早的 >100 存量必须**显式归一并
--     写审计**，避免「页面/文档写 500、实际跑 100」的显示与行为漂移；运行时
--     读取路径已钳制（settings 读取 min(_MAX_STEPS_LIMIT)、ai_config.json
--     读取钳制、PUT 校验 400），但存量值仍需归一以保权威一致；
--   - 只触碰这两个 ai_safety.* 键；其他 platform_settings 键（含
--     demo_task_max_steps=20 / demo_max_concurrency 等非 user-step 字段）
--     一律不动；
--   - 仅处理 JSONB **标量数字**形态（与 settings_store._read_ai_safety_tx 的
--     读取假设一致）：jsonb_typeof(value)='number' 且 > 100 才归一；布尔/
--     字符串等非法形态留给运行时按默认值 fail-closed 处理，不在迁移里猜值；
--   - 不改 ai_config.json（那是文件不是表）：其 max_steps 由应用读取路径
--     钳制到 100，owner 下次保存时 PUT 校验按新上限拒绝并要求改值。
--
-- 幂等/可重跑：UPDATE 带 WHERE 条件，重跑无 >100 行即 0 行更新；audit 用固定
-- event_id + ON CONFLICT DO NOTHING（与 0023/0029/0031 迁移标志 audit 同款）。
-- 归一前的旧值先读进变量再 UPDATE，保证审计能看到 previous。
-- 回滚：数据归一不自动回滚（100 是新契约的合法值）；如需恢复旧值，以审计
-- detail 的 previous_over_limit 为依据显式 UPDATE（迁移不 DROP、不改账本）。
-- =========================================================================== --

DO $$
DECLARE
    v_previous jsonb;
    v_updated  int;
BEGIN
    -- -- 1. 先取归一前的旧值快照（无匹配行 → '{}'） -- --
    SELECT COALESCE(jsonb_object_agg(key, (value #>> '{}')::numeric), '{}'::jsonb)
      INTO v_previous
      FROM platform_settings
     WHERE key IN ('ai_safety.platform_task_max_steps',
                   'ai_safety.own_task_max_steps_limit')
       AND jsonb_typeof(value) = 'number'
       AND (value #>> '{}')::numeric > 100;

    -- -- 2. 归一 >100 的 user 步数设置（JSONB 标量 → 100；只动这两个键） -- --
    UPDATE platform_settings
       SET value      = '100'::jsonb,
           updated_at = now(),
           updated_by = 'migration-0043'
     WHERE key IN ('ai_safety.platform_task_max_steps',
                   'ai_safety.own_task_max_steps_limit')
       AND jsonb_typeof(value) = 'number'
       AND (value #>> '{}')::numeric > 100;
    GET DIAGNOSTICS v_updated = ROW_COUNT;

    -- -- 3. 迁移标志 audit（固定 event_id，重跑不重复） -- --
    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0043_max_steps_normalize_100', now(), 'system',
         'settings.max_steps_normalized_100', 'platform_settings',
         jsonb_build_object(
             'keys', to_jsonb(ARRAY[
                 'ai_safety.platform_task_max_steps',
                 'ai_safety.own_task_max_steps_limit']),
             'allowed_max', 100,
             'normalized_rows', v_updated,
             'previous_over_limit', v_previous,
             'note', 'values above 100 normalized to 100 (2026-09-10 §2 A '
                     '1..100 contract); other ai_safety.* / demo fields '
                     'untouched; ai_config.json clamped at read path'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
