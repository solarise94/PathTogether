-- =========================================================================== --
-- 0041_ai_session_drawing_generation.sql：三轮 review P1——描绘开关镜像的
-- 单调 generation（并发乱序防护）。
--
-- 背景（2026-09-08 三轮 review 复现）：并发「开启」与「关闭」请求响应乱序
-- 时，旧的开启成功响应（allow=true）晚于其后关闭请求的预写 false 到达，
-- on_response 按响应值直接 upsert 会把镜像写回 true——平台写闸与 HP 权威
-- 状态（false）相反，迟到描绘重新通过闸门。
--
-- 修法：镜像行加单调递增 generation。关闭预写 = 无条件 upsert false（gen+1，
-- 立即作废所有在途旧响应）；开启/关闭的 on_response 只能 CAS 到「自己发起
-- 时读到的 generation 基线」——被其间任何写入超越即 no-op。
--
-- 幂等：ADD COLUMN IF NOT EXISTS。存量行 generation=0（视为第一代之前，
-- 任何预写/CAS 从 1 起自增，语义安全）。旧镜像代码不写本列（显式列名
-- INSERT），回滚旧版本安全。
-- =========================================================================== --

ALTER TABLE ai_session_drawing_flags
    ADD COLUMN IF NOT EXISTS generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN ai_session_drawing_flags.generation IS
    '镜像写入单调代数（0041）：预写 upsert 自增；代理响应只能 CAS 到发起时基线，防并发乱序旧响应覆盖新状态';
