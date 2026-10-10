-- =========================================================================== --
-- 0082_run_grant_drawing_skill.sql：AI 描绘改按需技能（viewer-drawing-skill-
-- contract-20261009.md §3，裁决已批准）。
--
-- run_grants 新增三列：
--   - allow_ai_drawing：本次 run 的描绘技能授权。缺省 FALSE（fail closed）；
--     仅由 HP 在 run 接受处完成意图判定后，经机器端点
--     POST /api/plugin/v1/run-grants/<id>/skills 在同一事务（行锁）内
--     false→true 置位；不提供 true→false 通道——收回靠既有 revoke/expiry
--     （run 终态/cancel/TTL/权限钩子，与 annotate 同生命周期）。
--   - drawing_bound_at：置位时间（审计辅助）。
--   - drawing_intent：来源 explicit（用户点了 @描绘）| model（模型按本轮
--     消息自行描绘）| natural（旧 HP 兼容）；审计辅助，空串=未置位。
--
-- 旧 ai_session_drawing_flags（0039/0041）保留不删：表与 /drawing 端点照旧
-- 可写（generation/CAS 不变），但自本迁移起**不再参与新写入授权**——
-- polygon/freehand 写入口只认本列（契约 §4.3，裁决 3：无过渡 OR 窗口）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS（对齐 0041/0067 风格）。
-- =========================================================================== --

ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS allow_ai_drawing BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS drawing_bound_at TIMESTAMPTZ;
ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS drawing_intent TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN run_grants.allow_ai_drawing IS
    '本次 run 的描绘技能授权（契约 0082）：缺省 FALSE fail closed；仅 HP 判定成立后经 /skills 端点置 true，收回靠 revoke/expiry';
COMMENT ON COLUMN run_grants.drawing_bound_at IS
    '描绘技能置位时间（/skills 端点写入；未置位为 NULL）';
COMMENT ON COLUMN run_grants.drawing_intent IS
    '描绘来源 explicit|model|natural（/skills 端点写入；未置位为空串）';
