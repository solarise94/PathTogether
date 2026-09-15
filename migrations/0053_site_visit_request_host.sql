-- =========================================================================== --
-- 0053_site_visit_request_host.sql：site_visit_events 记录「目标域名」
-- （request_host = 本次访问命中的公网入口 hostname）。
--
-- 背景（review 2026-09-15，访问来源混入治理）：
--
--   - 目标域名（Host）决定「这条访问是不是 HistoPilot 服务」；Referer 只表示
--     「从哪里跳过来」，两者语义不同，不能互相替代；
--   - 此前事件不落 Host：同机/同 IP 其它域名的请求一旦混入即无法追溯、
--     无法按域名剔除，只能等 90 天自然过期；
--   - 采集侧同步改为独立白名单 SITE_STATS_ENTRY_HOSTS（site_stats_store，
--     fail-closed：未配置/Host 缺失即停止采集），落库值恒为白名单内的
--     规范化 hostname（小写、去端口/尾点）。
--
-- 历史数据隔离口径：
--
--   - 存量行 request_host 保持 NULL = 「目标域名未知」；
--   - dashboard 聚合默认只统计 request_host 命中当前白名单的行，NULL 行
--     不进入任何主口径，仅单独计数（legacy.d30_visits）供面板提示；
--   - 不回填、不删除、不猜测存量行归属（保留至 90 天过期自然清除）。
--
-- 幂等/可重跑：ADD COLUMN IF NOT EXISTS + DO 块按约束名判存 + IF NOT EXISTS
-- 索引；重跑不改变任何行。单迁移单事务。回滚：紧急回滚不 DROP（§8.2 惯例，
-- 附加列对旧应用无害——旧 INSERT 不含本列，NULL 语义即「未知」）。
-- =========================================================================== #

-- --------------------------------------------------------------------------- #
-- 1. request_host 列（NULL = 迁移前历史行，目标域名未知）
-- --------------------------------------------------------------------------- #
ALTER TABLE site_visit_events
    ADD COLUMN IF NOT EXISTS request_host TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'site_visit_events'::regclass
          AND conname = 'site_visit_events_request_host_check') THEN
        ALTER TABLE site_visit_events
            ADD CONSTRAINT site_visit_events_request_host_check
            CHECK (request_host IS NULL OR (
                request_host ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$'
                AND length(request_host) <= 253));
    END IF;
END $$;

COMMENT ON COLUMN site_visit_events.request_host IS
    '本次访问命中的公网入口 hostname（规范化：小写、无端口/尾点；恒取自 '
    'SITE_STATS_ENTRY_HOSTS 白名单内的值，由 site_stats_store 采集时写入）。'
    'NULL = 迁移前历史行，目标域名未知，不进入新口径聚合';

-- --------------------------------------------------------------------------- #
-- 2. 聚合索引（dashboard 全部查询都按 request_host 过滤 + 时间窗）
-- --------------------------------------------------------------------------- #
CREATE INDEX IF NOT EXISTS idx_site_visit_events_host_time
    ON site_visit_events (request_host, occurred_at);

-- --------------------------------------------------------------------------- #
-- 3. 迁移标志 audit（不含敏感信息；固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- #
INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0053_site_visit_request_host', now(), 'system',
     'site_stats.request_host_added', 'site_visit_events',
     jsonb_build_object(
         'new_columns', to_jsonb(ARRAY['request_host']),
         'check_constraint', 'site_visit_events_request_host_check',
         'index', 'idx_site_visit_events_host_time',
         'legacy_note', 'NULL request_host = 迁移前历史行（目标域名未知），'
             '默认排除出新口径聚合，不回填不删除',
         'collection_note', '采集侧同步启用 SITE_STATS_ENTRY_HOSTS 独立白名单'
             '（fail-closed）'))
ON CONFLICT (event_id) DO NOTHING;
