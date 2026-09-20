-- =========================================================================== --
-- 0059_annotation_access_events.sql：授权增量事件 + AI 会话主体绑定
-- （review R1/R3 2026-09-20）。
--
--   - annotation_access_events：grant/revoke 写入 change_log 同号 seq，
--     投递给被授权主体（撤销后仍可见，以便失效/重建；不含几何/token）。
--   - ai_session_principals：session_id → user_id 本地绑定，供 spots 读取
--     主体解析（lite fork 无写 grant 时也能按会话属主过滤；不回调 sidecar）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS annotation_access_events (
    seq            BIGINT      PRIMARY KEY,
    slide          TEXT        NOT NULL,
    annotation_id  TEXT        NOT NULL,
    op             TEXT        NOT NULL,   -- grant | revoke
    grantee_kind   TEXT        NOT NULL,   -- user | share_token
    grantee_id     TEXT        NOT NULL,
    actor_user_id  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (op IN ('grant', 'revoke')),
    CHECK (grantee_kind IN ('user', 'share_token'))
);
CREATE INDEX IF NOT EXISTS idx_annotation_access_events_slide_seq
    ON annotation_access_events (slide, seq);
CREATE INDEX IF NOT EXISTS idx_annotation_access_events_grantee
    ON annotation_access_events (grantee_kind, grantee_id, seq);

CREATE TABLE IF NOT EXISTS ai_session_principals (
    session_id  TEXT        PRIMARY KEY,
    user_id     TEXT        NOT NULL,
    slide       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_session_principals_user
    ON ai_session_principals (user_id);
