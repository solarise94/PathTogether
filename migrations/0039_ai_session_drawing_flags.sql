-- =========================================================================== --
-- 0039_ai_session_drawing_flags.sql：P1-4（review-2026-09-07）——
-- 「允许 AI 描绘」开关的**平台本地镜像**授权表。
--
-- 背景：H 批次上线了会话级 allow_ai_drawing 开关（权威在 HistoPilot 侧
-- session 文件），但 PT 插件标注写入口（/api/plugin/v1/.../annotations 的
-- type=polygon|freehand 路径）只校验 plugin token + run grant（绑定 session），
-- 不复核开关——旧 sidecar / 伪造重放可在用户未开启时写入描绘标注。
--
-- 修法（PT 本地镜像授权，不在 polygon 写路径同步回调 HP）：
--   - 浏览器 → PT 代理（POST /api/ai/session/<sid>/drawing）→ HP 成功后，
--     把 HP 返回的**权威** allow_ai_drawing 值 upsert 进本表；
--   - HP 失败（非 2xx / 响应体缺权威布尔值）不改变镜像；
--   - polygon/freehand 写入口：run grant 绑定 session 后查本表——
--     **无行或 false 一律 403 ai_drawing_disabled（fail closed）**，不落库。
--
-- 幂等性：CREATE TABLE IF NOT EXISTS（对齐 0005/0037 风格）。
-- 无外键：session 的权威状态在 HP 侧，PT 镜像行不引用任何本地表。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS ai_session_drawing_flags (
    session_id       TEXT        PRIMARY KEY,           -- HP 会话 id（与 run_grants.session_id 同一口径）
    allow_ai_drawing BOOLEAN     NOT NULL DEFAULT FALSE, -- HP 权威值的镜像；缺省即关（fail closed）
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()   -- 最近一次镜像 upsert 时间（审计辅助）
);

COMMENT ON TABLE ai_session_drawing_flags IS
    '会话级「允许 AI 描绘」开关的 PT 本地镜像（0039 / P1-4）。权威在 HP；写入口只认本表，无行或 false 一律拒绝';

COMMENT ON COLUMN ai_session_drawing_flags.allow_ai_drawing IS
    'HP 代理响应回传的权威布尔值；仅 HP 成功（2xx 且含权威布尔）才 upsert，HP 失败不改镜像';
