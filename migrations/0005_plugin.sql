-- =========================================================================== --
-- 0005_plugin.sql：插件安装凭证 + run grant（Stage 4-1a，docs §7.6）
--
-- 新增实体：
--   - plugin_installations：插件安装行。安装凭证只存 sha256 hex（secret_hash），
--     明文仅创建/轮换时返回一次，绝不落盘。enabled=false 即撤销该安装全部
--     在途 JWT（平台每次校验回查 enabled）。json 后端为 shares.json 顶层
--     plugin_installations 数组。
--   - run_grants：起跑授权（slide 级、默认 2h、可撤销、无 org——demo 单实例）。
--     created_by_user_id 是 annotate provenance 的用户溯源来源。json 后端为
--     shares.json 顶层 run_grants 数组。
--
-- 全部幂等（IF NOT EXISTS，对齐 0001-0004 风格）。两实体是平台运行时状态，
-- 但 json 仍是默认后端（内网零依赖红线），故双实现共享本迁移的表结构。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS plugin_installations (
    installation_id TEXT        PRIMARY KEY,             -- 形如 pin_<urlsafe>
    plugin_id       TEXT        NOT NULL,                -- 如 "histopilot"
    version         TEXT        NOT NULL DEFAULT '',     -- 插件产品版本
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,   -- false = 撤销全部在途 JWT
    secret_hash     TEXT        NOT NULL,                -- sha256(明文) hex；明文绝不落盘
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at     TIMESTAMPTZ
);
COMMENT ON TABLE plugin_installations IS
    '插件安装凭证（Stage 4-1a）：secret_hash 只存 hash，明文仅创建/轮换时返回一次';
COMMENT ON COLUMN plugin_installations.enabled IS
    'false 时该安装签发的 scoped JWT 立即不可用（每次校验回查）';

CREATE TABLE IF NOT EXISTS run_grants (
    grant_id           TEXT        PRIMARY KEY,          -- 形如 rgr_<urlsafe>
    installation_id    TEXT        NOT NULL,
    slide              TEXT        NOT NULL,             -- legacy filename（Stage 3b 映射前）
    session_id         TEXT        NOT NULL DEFAULT '',  -- 起跑时未定 → slide 级空串
    created_by_user_id TEXT,                              -- annotate provenance 用户来源
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,             -- 默认 created_at + 2h
    revoked            BOOLEAN     NOT NULL DEFAULT FALSE,
    revoked_at         TIMESTAMPTZ
);
COMMENT ON TABLE run_grants IS
    'run grant（Stage 4-1a docs §7.6）：slide 级短期授权，annotate 端点强制校验';
CREATE INDEX IF NOT EXISTS idx_run_grants_session  ON run_grants (session_id);
CREATE INDEX IF NOT EXISTS idx_run_grants_expires  ON run_grants (expires_at);
CREATE INDEX IF NOT EXISTS idx_run_grants_install  ON run_grants (installation_id);
