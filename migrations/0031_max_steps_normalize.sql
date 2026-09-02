-- =========================================================================== --
-- 0031_max_steps_normalize.sql：遗留 >500 步数设置的显式、受审计归一
-- （Batch C，docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
-- §Batch C 实现要求 4 / §4.5「目标注册 user 语义」）。
--
-- 语义：
--   - 注册 user 的单任务步数（ai_safety.platform_task_max_steps）与自带 API
--     步数上限（ai_safety.own_task_max_steps_limit）的新契约是 1..500：默认值
--     与硬上限均为 500（budget_store.DEFAULT_PLATFORM_TASK_MAX_STEPS /
--     DEFAULT_OWN_TASK_MAX_STEPS_LIMIT 自本批起 = 500）；
--   - 线上已有 500 无需改值；但旧数据库/旧管理端（曾放行 ≤1_000_000）可能
--     存量 >500 的 JSONB 标量。运行时已不再静默截断（保存路径稳定 400、
--     读取缺值回落 500），因此存量非法值必须**显式归一并写审计**，不得在
--     运行时悄悄改值（§4.5）；
--   - 只触碰这两个 ai_safety.* 键；其他 platform_settings 键（含
--     demo_task_max_steps / demo_max_concurrency 等非 user-step 字段）一律
--     不动；
--   - 仅处理 JSONB **标量数字**形态（与 settings_store._read_ai_safety_tx 的
--     读取假设一致）：jsonb_typeof(value)='number' 且 > 500 才归一；布尔/
--     字符串等非法形态留给运行时按默认值 fail-closed 处理，不在迁移里猜值。
--
-- 幂等/可重跑：UPDATE 带 WHERE 条件，重跑无 >500 行即 0 行更新；audit 用固定
-- event_id + ON CONFLICT DO NOTHING（与 0023/0029 迁移标志 audit 同款）。
-- 仓内审计先例：0004 建 audit_events 后，0023/0024/0029 均在迁移内写固定
-- event_id 的标志/结果审计，本迁移仿照该先例记录归一结果（无敏感内容：纯
-- 键名与数字）。归一前的旧值先读进变量再 UPDATE，保证审计能看到 previous。
-- 回滚：数据归一不自动回滚（500 是新契约的合法值）；如需恢复旧值，以审计
-- detail 的 previous_over_limit 为依据显式 UPDATE（§8.2：迁移不 DROP、不改
-- 账本）。
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
       AND (value #>> '{}')::numeric > 500;

    -- -- 2. 归一 >500 的 user 步数设置（JSONB 标量 → 500；只动这两个键） -- --
    UPDATE platform_settings
       SET value      = '500'::jsonb,
           updated_at = now(),
           updated_by = 'migration-0031'
     WHERE key IN ('ai_safety.platform_task_max_steps',
                   'ai_safety.own_task_max_steps_limit')
       AND jsonb_typeof(value) = 'number'
       AND (value #>> '{}')::numeric > 500;
    GET DIAGNOSTICS v_updated = ROW_COUNT;

    -- -- 3. 迁移标志 audit（固定 event_id，重跑不重复） -- --
    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0031_max_steps_normalize', now(), 'system',
         'settings.max_steps_normalized', 'platform_settings',
         jsonb_build_object(
             'keys', to_jsonb(ARRAY[
                 'ai_safety.platform_task_max_steps',
                 'ai_safety.own_task_max_steps_limit']),
             'allowed_max', 500,
             'normalized_rows', v_updated,
             'previous_over_limit', v_previous,
             'note', 'values above 500 normalized to 500 (Batch C 1..500 '
                     'contract); other ai_safety.* / demo fields untouched'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
