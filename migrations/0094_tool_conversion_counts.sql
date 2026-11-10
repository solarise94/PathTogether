-- =========================================================================== --
-- 0094_tool_conversion_counts.sql：匿名「浏览器内本地转换完成」计数
-- （/tools/slides 转换器页 + 工作台上传面板 inline 转换）
--
-- 背景：切片转换完全在用户浏览器内完成（WASM），服务端此前不知道任何一次
-- 转换已完成。产品决定（2026-11-10）：从发布日起按 UTC+8 日界累计完成次数，
-- 并在转换器页展示一行匿名汇总；不做历史回填。
--
-- 隐私边界（硬约束）：本表**只**保存日、来源、归一化输入格式与计数——
-- 不保存文件名/大小/IP/用户 ID/UA/硬编码访客哈希。同一访客的日上限在
-- 进程内用日轮换 HMAC 判断，哈希本身不落库。
--
-- 幂等/可重跑：CREATE TABLE IF NOT EXISTS + 固定 event_id 的 audit 行
-- ON CONFLICT DO NOTHING；迁移 runner 提供事务。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS tool_conversion_daily (
    day     date   NOT NULL,
    source  text   NOT NULL CHECK (source IN ('converter', 'workbench')),
    format  text   NOT NULL,
    count   bigint NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (day, source, format)
);

COMMENT ON TABLE tool_conversion_daily IS
    'Anonymous completed local conversion counts: UTC+8 day, source and normalized input format only; no file, visitor or network identifiers';

COMMENT ON COLUMN tool_conversion_daily.format IS
    'Normalized input format token (server whitelist); unknown inputs are recorded as other';

-- --------------------------------------------------------------------------- --
-- 迁移标志 audit（固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- --
INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0094_tool_conversion_counts', now(), 'system',
     'tools.conversion_counts_schema_applied', 'tool_conversion_daily',
     jsonb_build_object(
         'sources', 'converter|workbench',
         'dimensions', 'day,source,format',
         'privacy', 'no file, visitor or network identifiers stored'))
ON CONFLICT (event_id) DO NOTHING;
