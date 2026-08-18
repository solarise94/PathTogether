-- =========================================================================== --
-- 0010_demo_task_max_steps_20.sql：Demo 单次任务默认 20 步
--
-- 10 步只够几次 goto/snapshot 就达限，读片演示几乎做不成。与注册用户平台 AI
-- 默认 20 步对齐。已应用的 0006 不改写；本迁移改列缺省，并把仍停在旧缺省 10
-- 的开放周期抬到 20（owner 已改成其他值的行不动）。
-- =========================================================================== --

ALTER TABLE ai_budget_periods
    ALTER COLUMN demo_task_max_steps SET DEFAULT 20;

UPDATE ai_budget_periods
   SET demo_task_max_steps = 20
 WHERE closed_at IS NULL
   AND demo_task_max_steps = 10;

COMMENT ON COLUMN ai_budget_periods.demo_task_max_steps IS
    'Demo 单次任务步数（默认 20，与平台 AI 单次步数对齐）';
