-- =========================================================================== --
-- 0082_run_grant_drawing_skill.sql：AI 描绘改按需技能（viewer-drawing-skill-
-- contract-20261009.md §3，裁决已批准）。
--
-- run_grants 新增三列：
--   - allow_ai_drawing：本次 run 是否允许写入 AI 描绘（polygon/freehand）。
--     2026-10-10 起随 grant 发放默认 TRUE：是否描绘由模型按本轮消息决定，
--     写权限仍由 annotate 路径按 grant 有效性与创建者当前权限检查。列保留
--     作为日后由用户关闭描绘的开关；POST /api/plugin/v1/run-grants/<id>/skills
--     （false→true，事务内复查创建者写权）保留兼容。收回靠 revoke/expiry。
--   - drawing_bound_at：置位时间（审计辅助）。
--   - drawing_intent：default（随 grant 发放）| explicit | model | natural
--     （/skills 写入，旧 HP 兼容）；审计辅助，空串=未置位。
--
-- 旧 ai_session_drawing_flags（0039/0041）保留不删：表与 /drawing 端点照旧
-- 可写（generation/CAS 不变），但自本迁移起**不再参与新写入授权**——
-- polygon/freehand 写入口只认本列（契约 §4.3，裁决 3：无过渡 OR 窗口）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS（对齐 0041/0067 风格）。
-- =========================================================================== --

ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS allow_ai_drawing BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS drawing_bound_at TIMESTAMPTZ;
ALTER TABLE run_grants
    ADD COLUMN IF NOT EXISTS drawing_intent TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN run_grants.allow_ai_drawing IS
    '本次 run 是否允许写入 AI 描绘（契约 0082）：随 grant 默认 TRUE；保留为日后用户关闭描绘的开关，收回靠 revoke/expiry';
COMMENT ON COLUMN run_grants.drawing_bound_at IS
    '描绘技能置位时间（/skills 端点写入；未置位为 NULL）';
COMMENT ON COLUMN run_grants.drawing_intent IS
    '描绘来源 default|explicit|model|natural（grant 发放或 /skills 写入；未置位为空串）';
