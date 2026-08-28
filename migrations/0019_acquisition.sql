-- =========================================================================== --
-- 0019_acquisition.sql：用户来源归因数据层（admin-billing 方案 §11，PR4）
--
-- 新增三表（DDL 逐字对齐方案 §11.2 v0.2）：
--   - acquisition_campaigns：source/campaign 字典（status: active|paused|
--     archived）。本批无 campaign CRUD API（PR5）；行由 owner 直接 SQL 或
--     acquisition_store.create_campaign 种入；
--   - acquisition_visits：**每次 /r/<source_code> 跳转一个不可变触点事件**
--     （行粒度固定：不按 visitor upsert、不用同一行累计 first/last_seen；
--     同一访客跨 source/campaign 的 first-touch 与 last-touch 不会被折叠，
--     方案 §11.2 明文）。注册归因在有效期内按 (touched_at, acquisition_id)
--     稳定选 first/last；未来聚合只能新增可重建 projection，不改原始粒度；
--   - user_acquisition：注册时的归因结论（长期保留 campaign/source，不复制
--     原始 IP / referrer —— 那些只存在于 acquisition_visits 且随 90 天清理）。
--     归因优先级（§11.2）：邀请码显式 campaign > 有效 pt_acq first-party
--     触点 > sanitized referrer/UTM > direct/unknown。
--
-- registration_invites 增列（幂等 ADD COLUMN IF NOT EXISTS，向后兼容旧代码）：
--   - source_code TEXT NOT NULL DEFAULT ''（邀请显式来源；空 = 未指定）；
--   - campaign_id  TEXT REFERENCES acquisition_campaigns（NULL = 未指定）。
--
-- 隐私（§11.3）：库内不保存完整 IP（只存带盐 IP 前缀 hash，盐来自 env
-- ACQ_IP_SALT，未配置时应用层存空串=不采集）；referrer 只存 hostname；
-- UTM 字段应用层限长+清理控制字符；匿名触点默认 90 天清理
-- （acquisition_store.cleanup_expired_visits 只删未被 user_acquisition 引用
-- 且已过期的行，外键兜底已归因触点不被删）。
--
-- acquisition 能力仅 STORAGE_BACKEND=postgres（platform_features /
-- acquisition_store 守卫；/r/* 路由在 json/dual 降级为安全固定 302，§16.2）。
--
-- 全部幂等（IF NOT EXISTS / ADD COLUMN IF NOT EXISTS，对齐 0001-0018 风格；
-- 只新增，不修改既有表语义）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS acquisition_campaigns (
    campaign_id TEXT PRIMARY KEY,
    source_code TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active','paused','archived')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT
);
COMMENT ON TABLE acquisition_campaigns IS
    '来源 campaign 字典（admin-billing §11.2）：/r/ 与邀请可引用；status 非 active 的不参与新归因绑定';

CREATE TABLE IF NOT EXISTS acquisition_visits (
    acquisition_id TEXT PRIMARY KEY,
    visitor_id_hash TEXT NOT NULL,
    source_code     TEXT NOT NULL,
    campaign_id    TEXT REFERENCES acquisition_campaigns,
    referrer_domain TEXT NOT NULL DEFAULT '',
    landing_path    TEXT NOT NULL DEFAULT '',
    utm_source      TEXT NOT NULL DEFAULT '',
    utm_medium      TEXT NOT NULL DEFAULT '',
    utm_campaign    TEXT NOT NULL DEFAULT '',
    ip_prefix_hash  TEXT NOT NULL DEFAULT '',
    touched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE acquisition_visits IS
    '匿名触点事件（admin-billing §11.2）：每次 /r/<source_code> 跳转一行，不可变、不按 visitor 折叠；expires_at 到期后不再参与归因，未归因行由 90 天清理删除';

CREATE INDEX IF NOT EXISTS idx_acquisition_visits_visitor_time
    ON acquisition_visits (visitor_id_hash, touched_at);
-- 90 天清理的扫描索引（方案未列；清理是本批交付功能，行量随点击增长）
CREATE INDEX IF NOT EXISTS idx_acquisition_visits_expires
    ON acquisition_visits (expires_at);

CREATE TABLE IF NOT EXISTS user_acquisition (
    user_id              TEXT PRIMARY KEY REFERENCES users(user_id),
    first_acquisition_id TEXT REFERENCES acquisition_visits,
    last_acquisition_id  TEXT REFERENCES acquisition_visits,
    invite_id            TEXT REFERENCES registration_invites(invite_id),
    source_code          TEXT NOT NULL DEFAULT 'unknown',
    campaign_id          TEXT REFERENCES acquisition_campaigns,
    attributed_at        TIMESTAMPTZ NOT NULL,
    attribution_method   TEXT NOT NULL
);
COMMENT ON TABLE user_acquisition IS
    '注册归因结论（admin-billing §11.2）：与兑换/建号/invite 消费同一事务写入；优先级 invite campaign > 有效触点 > referrer/UTM > direct；不复制原始 IP/referrer';

-- 邀请码显式来源（§11.2「registration_invites 增加 source_code、campaign_id」）
ALTER TABLE registration_invites
    ADD COLUMN IF NOT EXISTS source_code TEXT NOT NULL DEFAULT '';
ALTER TABLE registration_invites
    ADD COLUMN IF NOT EXISTS campaign_id TEXT REFERENCES acquisition_campaigns;
COMMENT ON COLUMN registration_invites.source_code IS
    '邀请显式来源 slug（[a-z0-9_-]，空 = 未指定；归因优先级仅次于显式 campaign）';
COMMENT ON COLUMN registration_invites.campaign_id IS
    '邀请显式 campaign（FK acquisition_campaigns；NULL = 未指定，归因最高优先级）';
