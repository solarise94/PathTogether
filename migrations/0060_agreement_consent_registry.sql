-- =========================================================================== --
-- 0060_agreement_consent_registry.sql：协议文档注册表 + 协议接受凭据 +
-- 研究授权当前状态与不可变历史（docs/agent-plan-20260921-registration-
-- consent-research.md §3.3/§3.4，P0 协议与迁移底座）。
--
--   - agreement_documents：协议文档注册表。发布记录不可变——主键
--     (document_type, version, locale) + content_sha256 格式约束；同 version
--     换内容 = 新主键行（服务端拒绝沿用同 version 的不同 hash，见
--     agreement_store.ensure_builtin_documents 的 hash 比对）。status 仅
--     draft → published → retired 单向流转；同一 (document_type, locale)
--     至多一条 published（部分唯一索引），“当前生效文稿”无歧义。
--   - user_agreement_acceptances：必选/可选协议的接受凭据，只追加、不覆盖
--     旧版本；不存完整 IP/UA。
--   - user_research_consents：研究授权当前状态（缺行 = 未授权）。epoch 正
--     整数、每次状态迁移 +1，供客户端 CAS 与在途写入竞态判定。
--   - user_research_consent_history：每次 grant/withdraw 的不可变记录，
--     idempotency_key 唯一（非空时）支撑请求级幂等重放。
--
-- 全部 IF NOT EXISTS / ON CONFLICT 幂等；不修改任何已发布迁移。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS agreement_documents (
    document_type      TEXT        NOT NULL,
    version            TEXT        NOT NULL,
    locale             TEXT        NOT NULL DEFAULT 'zh-CN',
    title              TEXT        NOT NULL,
    content_sha256     TEXT        NOT NULL,
    content_path       TEXT        NOT NULL,  -- 不可变文稿定位（仓库内规范文稿相对路径）
    status             TEXT        NOT NULL DEFAULT 'draft',
    published_at       TIMESTAMPTZ,
    effective_at       TIMESTAMPTZ,
    requires_reconsent BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_type, version, locale),
    CHECK (document_type IN ('user_agreement', 'research_sharing', 'model_providers')),
    CHECK (status IN ('draft', 'published', 'retired')),
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);
-- 同一文档类型同一语言至多一条 published（当前发布文稿唯一）
CREATE UNIQUE INDEX IF NOT EXISTS agreement_documents_single_published
    ON agreement_documents (document_type, locale) WHERE status = 'published';

CREATE TABLE IF NOT EXISTS user_agreement_acceptances (
    acceptance_id  BIGSERIAL   PRIMARY KEY,
    user_id        TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    document_type  TEXT        NOT NULL,
    version        TEXT        NOT NULL,
    content_sha256 TEXT        NOT NULL,
    accepted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    source         TEXT        NOT NULL,
    locale         TEXT        NOT NULL DEFAULT 'zh-CN',
    CHECK (document_type IN ('user_agreement', 'research_sharing', 'model_providers')),
    CHECK (source IN ('register', 'account_reaccept')),
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS idx_user_agreement_acceptances_user_doc
    ON user_agreement_acceptances (user_id, document_type, accepted_at);

CREATE TABLE IF NOT EXISTS user_research_consents (
    user_id          TEXT        PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    state            TEXT        NOT NULL,
    scope_version    TEXT,
    document_version TEXT,
    document_sha256  TEXT,
    epoch            INTEGER     NOT NULL CHECK (epoch >= 1),
    granted_at       TIMESTAMPTZ,
    withdrawn_at     TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (state IN ('declined', 'granted', 'withdrawn', 'reconsent_required'))
);

CREATE TABLE IF NOT EXISTS user_research_consent_history (
    history_id       BIGSERIAL   PRIMARY KEY,
    user_id          TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    from_state       TEXT,       -- NULL = 此前无 consent 行
    to_state         TEXT        NOT NULL,
    epoch            INTEGER     NOT NULL CHECK (epoch >= 1),  -- 本次迁移后的新 epoch
    actor_user_id    TEXT        NOT NULL,  -- 操作者；grant 必须等于 user_id（服务层强制，管理者不能代用户 grant）
    document_version TEXT,
    document_sha256  TEXT,
    idempotency_key  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (to_state IN ('declined', 'granted', 'withdrawn', 'reconsent_required')),
    CHECK (from_state IS NULL OR from_state IN ('declined', 'granted', 'withdrawn', 'reconsent_required'))
);
-- 请求幂等键唯一（空值不约束）：网络重放不产生第二条历史
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_research_consent_history_idem
    ON user_research_consent_history (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_research_consent_history_user
    ON user_research_consent_history (user_id, created_at);
