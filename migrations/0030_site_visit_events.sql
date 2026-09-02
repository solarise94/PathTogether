-- =========================================================================== --
-- 0030_site_visit_events.sql：站点匿名访问事件表（Batch D2，docs
-- review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §3.4 / §4.4 /
-- §Batch D2）。
--
-- 语义（§4.4 站点访问统计口径）：
--
--   - 只记录**允许列表中的成功公开 HTML GET**（home/demo/register/login）；
--     /api/*、静态资源、health、admin、登录后工作区以及含分享 token、切片名、
--     项目 ID 的路径在 store 层即被拒绝，不会到达本表。
--   - 最小匿名字段，固定 11 列：event_id/occurred_at/dedup_bucket/page_key/
--     referrer_domain/utm_source/country_code/daily_visitor_hash/visitor_kind/
--     bot_name/expires_at。**无用户外键、无 user_id/invite_id/session_id，
--     无完整 IP、原始 User-Agent、URL query、token、资源 ID 列**——原始数据
--     在请求时计算派生值后即丢弃，绝不落库。
--   - daily_visitor_hash = HMAC-SHA256(secret, YYYY-MM-DD + IPv4/24 或
--     IPv6/64 前缀)：日轮换匿名访客哈希，跨日不可识别同一人；前缀本身不落库。
--     secret 只从 SITE_STATS_HMAC_SECRET_FILE（0600）读取，不复用 session
--     secret；缺失/权限错误时 store 停止采集（本表无新行），页面照常服务。
--   - 同一 (daily_visitor_hash, page_key, dedup_bucket) 唯一——10 分钟去重桶，
--     重复访问 ON CONFLICT DO NOTHING。
--   - visitor_kind 仅 'anonymous_human'|'signed_in_human'|'suspected_bot'；
--     bot_name 仅 suspected_bot 行非空（词表版本化于
--     site_stats_store.SITE_BOT_UA_RULESET_VERSION）。
--   - 原始事件保留 90 天（expires_at = occurred_at + 90 天，store 写入时
--     计算），由显式 retention 清理（site_stats_store.purge_expired）删除；
--     无「已归因触点不得删」的特殊分支——本表与用户归因完全无关
--     （不复用带归因外键与 90 天 cookie 语义的 acquisition_visits）。
--   - 聚合（dashboard_stats）只读本表，不联任何业务表；接口无写副作用。
--
-- 幂等/可重跑：全部 IF NOT EXISTS / ON CONFLICT DO NOTHING / DO 块按约束名
-- 判存；重跑不改变任何行。单迁移单事务。
-- 回滚：本迁移只加表/索引，紧急回滚不 DROP（§8.2）；回滚旧应用时停止写入、
-- 数据保留不删（site_visit_events 是独立附加数据）。
-- =========================================================================== #

-- --------------------------------------------------------------------------- #
-- 1. site_visit_events（最小匿名事件；append-only + 显式过期清理）
-- --------------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS site_visit_events (
    event_id           TEXT        PRIMARY KEY,   -- sve_<24hex>
    occurred_at        TIMESTAMPTZ NOT NULL,
    -- 10 分钟去重桶（epoch 秒 // 600）；与 daily_visitor_hash/page_key 联合唯一
    dedup_bucket       BIGINT      NOT NULL CHECK (dedup_bucket >= 0),
    -- 页面键：只允许 store 层 allowlist 映射结果（home/demo/register/login），
    -- DB 层仅约束形态（小写短标签），allowlist 收紧不需要改表
    page_key           TEXT        NOT NULL
                                   CONSTRAINT site_visit_events_page_key_check
                                   CHECK (page_key ~ '^[a-z0-9_]{1,32}$'),
    -- 只存 hostname（小写）；空 referrer/同站来源统一 'direct'。无 scheme/path
    referrer_domain    TEXT        CONSTRAINT site_visit_events_referrer_check
                                   CHECK (referrer_domain IS NULL OR (
                                       referrer_domain NOT LIKE '%://%'
                                       AND referrer_domain NOT LIKE '%/%')),
    -- 可选短 UTM source（store 清洗：^[A-Za-z0-9_-]{1,32}$）；其余 UTM/query
    -- 全部丢弃，绝不存原 query
    utm_source         TEXT        CONSTRAINT site_visit_events_utm_source_check
                                   CHECK (utm_source IS NULL OR
                                          utm_source ~ '^[A-Za-z0-9_-]{1,32}$'),
    -- D2 首发恒 'unknown'（不调第三方定位）；将来离线库另做 review
    country_code       TEXT        NOT NULL DEFAULT 'unknown'
                                   CONSTRAINT site_visit_events_country_check
                                   CHECK (country_code = 'unknown' OR
                                          country_code ~ '^[A-Za-z]{2}$'),
    -- 日轮换匿名访客哈希（HMAC-SHA256 hex，64 字符）；不含 IP/UA 本体
    daily_visitor_hash TEXT        NOT NULL
                                   CONSTRAINT site_visit_events_visitor_hash_check
                                   CHECK (daily_visitor_hash ~ '^[0-9a-f]{64}$'),
    -- 三分类固定词表：爬虫不进入人类/匿名访客近似数
    visitor_kind       TEXT        NOT NULL
                                   CONSTRAINT site_visit_events_kind_check
                                   CHECK (visitor_kind IN ('anonymous_human',
                                                           'signed_in_human',
                                                           'suspected_bot')),
    -- 疑似 bot 的规则命中名（词表见 site_stats_store，版本化常量）
    bot_name           TEXT,
    -- 90 天过期（store 写入时 = occurred_at + 90 天）；purge_expired 只删到期行
    expires_at         TIMESTAMPTZ NOT NULL,
    CONSTRAINT site_visit_events_retention_check
        CHECK (expires_at > occurred_at),
    -- 10 分钟去重桶：同一访客哈希 + 同页面 + 同桶至多一条（冲突即丢）
    CONSTRAINT site_visit_events_dedup_unique
        UNIQUE (daily_visitor_hash, page_key, dedup_bucket)
);

COMMENT ON TABLE site_visit_events IS
    '站点匿名访问事件（Batch D2 §4.4，与用户归因完全解耦）：只存 allowlist 公开'
    'HTML 页访问的最小匿名字段；无用户外键、无完整 IP/UA/query/token/资源 ID；'
    'daily_visitor_hash 日轮换（跨日不可识别同一人）；'
    '(daily_visitor_hash, page_key, dedup_bucket) 唯一 = 10 分钟去重；'
    '90 天过期由 purge_expired 显式清理，可随时直接删除';
COMMENT ON COLUMN site_visit_events.daily_visitor_hash IS
    'HMAC-SHA256(SITE_STATS_HMAC_SECRET_FILE secret, YYYY-MM-DD + IPv4/24 或 '
    'IPv6/64 网络前缀)：同日同前缀同哈希、跨日/跨前缀不同；前缀本身不落库';

-- --------------------------------------------------------------------------- #
-- 2. 聚合索引（dashboard_stats 固定聚合：时间窗 / 页面 / 外部来源）
-- --------------------------------------------------------------------------- #
CREATE INDEX IF NOT EXISTS idx_site_visit_events_occurred_at
    ON site_visit_events (occurred_at);
CREATE INDEX IF NOT EXISTS idx_site_visit_events_page_time
    ON site_visit_events (page_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_site_visit_events_referrer_time
    ON site_visit_events (referrer_domain, occurred_at);

-- --------------------------------------------------------------------------- #
-- 3. 迁移标志 audit（不含敏感信息；固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- #
INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0030_site_visit_events', now(), 'system',
     'site_stats.site_visit_events_applied', 'site_visit_events',
     jsonb_build_object(
         'new_tables', to_jsonb(ARRAY['site_visit_events']),
         'visitor_kinds', to_jsonb(ARRAY[
             'anonymous_human', 'signed_in_human', 'suspected_bot']),
         'dedup_unique', 'site_visit_events_dedup_unique',
         'dedup_bucket_seconds', 600,
         'retention_days', 90,
         'page_allowlist_note', 'home/demo/register/login；exact-match，'
             '由 site_stats_store.PAGE_ALLOWLIST 权威定义',
         'privacy_note', 'no user FK / no raw IP / no raw UA / no query / '
             'no token / no resource id; daily-rotating HMAC visitor hash',
         'geo_note', 'country_code 恒 unknown（D2 不调第三方定位）'))
ON CONFLICT (event_id) DO NOTHING;
