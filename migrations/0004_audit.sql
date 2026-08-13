-- =========================================================================== --
-- 0004_audit.sql：审计日志表 + 项目归档只读开关（Stage 3c-2，docs §5.3/§6.4/§v1.5）
--
-- 新增实体：
--   - audit_events：协作操作审计日志（非医疗审计）。权威负载存 detail JSONB
--     （少量上下文，绝不含 api_key / 明文密码）。json 后端为 shares.json 顶层
--     audit 数组（封顶 5000 条丢最旧）；pg 侧持久存储。
--   - projects.archived：纯只读开关（v1.5 已砍状态机，只剩归档只读）。默认 false，
--     旧数据兼容。
--
-- 全部幂等（IF NOT EXISTS；迁移 runner 按 schema_migrations 去重 + IF NOT EXISTS
-- 双保险，对齐 0001-0003 风格）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS audit_events (
    event_id        TEXT        PRIMARY KEY,             -- 形如 aud_<uuid>
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id   TEXT,                                -- 可空 = guest/内部
    actor_role      TEXT        NOT NULL DEFAULT '',
    action          TEXT        NOT NULL DEFAULT '',     -- 枚举字符串，见 docs
    target_type     TEXT,                                -- 如 share/user/annotation
    target_id       TEXT,
    slide           TEXT,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb  -- 少量上下文，绝不存密钥
);
COMMENT ON TABLE audit_events IS '协作操作审计日志（非医疗审计）。detail 绝不存 api_key/明文密码';
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events (action);

-- 项目归档只读开关（v1.5 纯只读，无状态机）
ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;
COMMENT ON COLUMN projects.archived IS '纯只读开关：true 时该项目切片对所有身份只读，解除归档才可写';
