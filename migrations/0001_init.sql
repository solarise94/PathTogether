-- =========================================================================== --
-- 0001_init.sql：全量目标 schema（Stage 3b 一次性建全，避免中途 ALTER）
--
-- 覆盖现有 JSON（shares.json / users.json）全部实体，并落地 Stage 3b 目标模型：
--   - 稳定 slide identity（slides / slide_assets）；
--   - projects / shares / grants / rois / change_log。
--
-- 命名一律 snake_case；全部 IF NOT EXISTS 幂等（迁移 runner 也按 schema_migrations
-- 去重，双重保险）。中文注释对齐 share_store.py 风格。
--
-- 注意外键策略：rois.token / rois.slide / change_log.token **不加** 外键——
-- JSON 语义里管理员标注用固定 ADMIN_TOKEN="admin"（无对应 shares 行），且 rois.slide
-- 引用的是 legacy filename（迁移期未必都在 slides 表内）。强外键会导致导入期违反。
-- 仅在数据完整性有保证的关系上加 FK：slide_assets→slides、project_slides→projects、
-- grants→shares。
-- =========================================================================== --

-- email 大小写不敏感唯一：
-- 原计划用 citext，但 pgserver（测试基建）内置的 PostgreSQL 不带 citext 扩展，
-- 且 user_store_json._normalize_email 已在写入侧统一小写。这里改用等价的函数
-- 唯一索引 lower(email)，保证大小写不敏感唯一且零扩展依赖（后续若部署全量 PG
-- 带 contrib，可平滑切回 citext）。
-- --------------------------------------------------------------------------- --
-- users：四级身份基础（owner/user；guest 无行，sdk 后续阶段）。对应 users.json。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT        PRIMARY KEY,             -- 形如 usr_xxx
    email           TEXT        NOT NULL,                -- 写入侧已小写；唯一见下方函数索引
    display_name    TEXT        NOT NULL DEFAULT '',
    password_hash   TEXT        NOT NULL DEFAULT '',     -- 空串=禁用密码登录
    role            TEXT        NOT NULL,                -- owner/user/guest/sdk
    disabled        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ai_config       JSONB       NOT NULL DEFAULT '{}'::jsonb  -- AI 读片助手配置
);

-- email 大小写不敏感唯一（等价 citext 语义，无扩展依赖）
CREATE UNIQUE INDEX IF NOT EXISTS users_email_ci_key ON users (lower(email));

-- --------------------------------------------------------------------------- --
-- slides：稳定切片身份（Stage 3b 目标）。迁移期靠 legacy_filename 与旧数据映射；
-- 新稳定 slide_id 供 3b-2 接入。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS slides (
    slide_id        TEXT        PRIMARY KEY,             -- 形如 sld_xxx（稳定 id）
    legacy_filename TEXT        UNIQUE,                  -- 即现 slide name（迁移期映射键）
    display_name    TEXT        NOT NULL DEFAULT '',     -- 别名/展示名
    alias           TEXT        NOT NULL DEFAULT '',
    note            TEXT        NOT NULL DEFAULT '',
    owner_user_id   TEXT,                                 -- 可空，归属 owner
    public          BOOLEAN     NOT NULL DEFAULT FALSE,
    roi_sizes       JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- 允许的 ROI 标记尺寸(mm)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------- --
-- slide_assets：切片内容资产 + revision。
--   当前先用 content_sha256/legacy_revision 两列占位：
--     - legacy_revision（text）：封装旧 mtime:size 指纹（保守误失效语义，
--       见 docs 迁移期说明），只能用于 legacy CAS；
--     - content_sha256（text）：Stage 3b-2 由解码后真实 JPEG bytes 计算填入，
--       形如 ar_sha256_* 的内容型 asset revision。
--   注释说明：旧 mtime:size 指纹即使 touch 文件也会变并触发 409（安全误失效）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS slide_assets (
    asset_id        TEXT        PRIMARY KEY,             -- 形如 ast_xxx
    slide_id        TEXT        NOT NULL REFERENCES slides(slide_id) ON DELETE CASCADE,
    content_sha256  TEXT,                                 -- 3b-2 接入：内容型 sha
    legacy_revision TEXT,                                 -- 旧 mtime:size 指纹（迁移期）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN slide_assets.legacy_revision IS
    '旧 mtime:size 指纹：仅 legacy CAS 用，touch 亦变更并触发 409（安全误失效）；3b-2 切内容型 sha256 后弃用';

-- --------------------------------------------------------------------------- --
-- projects / project_slides：病例分组。对应 shares.json 的 projects/slide_meta。
-- project_slides.slide 暂存 legacy filename（迁移期），3b-2 后切 slide_id。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS projects (
    project_id      TEXT        PRIMARY KEY,             -- 形如 prj_xxx
    name            TEXT        NOT NULL,
    note            TEXT        NOT NULL DEFAULT '',
    owner_user_id   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS project_slides (
    project_id      TEXT        NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    slide           TEXT        NOT NULL,                -- 迁移期：legacy filename
    position        INTEGER     NOT NULL DEFAULT 0
);

-- --------------------------------------------------------------------------- --
-- shares：分享链接主记录。对应 shares.json 的 shares。
-- slides/permissions/roi_sizes 暂保 JSONB 数组形态（3b-2 拆分时再规整）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS shares (
    token           TEXT        PRIMARY KEY,             -- 分享 token
    slides          JSONB       NOT NULL DEFAULT '[]'::jsonb,   -- 暂保数组形态
    permissions     JSONB       NOT NULL DEFAULT '["view","annotate"]'::jsonb,
    roi_sizes       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    expires_at      TIMESTAMPTZ,                               -- 可空=永不过期
    revoked         BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    creator_user_id TEXT
);

-- --------------------------------------------------------------------------- --
-- grants：分享认领关系（Stage 3a-2a）。对应 shares.json 的 grants。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS grants (
    id              TEXT        PRIMARY KEY,
    token           TEXT        NOT NULL REFERENCES shares(token) ON DELETE CASCADE,
    user_id         TEXT        NOT NULL,
    permissions     JSONB       NOT NULL DEFAULT '["view","annotate"]'::jsonb,
    claimed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    active          BOOLEAN     NOT NULL DEFAULT TRUE
);

-- --------------------------------------------------------------------------- --
-- rois：标注主数据。对应 shares.json 的 rois。
-- deleted（bool）：软删 tombstone，为 Stage 3c（协作/审核/事件）预留——见 docs。
-- 注：token/slide 不加外键（ADMIN_TOKEN="admin" 无 shares 行；slide 为 legacy 名）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS rois (
    id              TEXT        PRIMARY KEY,
    token           TEXT        NOT NULL,                -- 分享 token 或 ADMIN_TOKEN
    slide           TEXT        NOT NULL,                -- legacy filename
    annotation_id   TEXT        UNIQUE,                  -- 稳定标注 id（跨重排）
    label           TEXT        NOT NULL DEFAULT '',
    type            TEXT        NOT NULL,                -- rect/arrow/freehand
    geom            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    size_mm         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    shared          BOOLEAN     NOT NULL DEFAULT FALSE,
    note            TEXT        NOT NULL DEFAULT '',
    visitor         JSONB,                               -- 访客上下文（可空）
    owner_user_id   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted         BOOLEAN     NOT NULL DEFAULT FALSE   -- tombstone（Stage 3c 预留）
);
COMMENT ON COLUMN rois.deleted IS '软删 tombstone，Stage 3c 协作/审核阶段启用';

-- --------------------------------------------------------------------------- --
-- change_log：切片级变更流水（对应 shares.json 的 change_seq_by_slide）。
-- seq 为 bigserial 全局单调序号（docs §4.2）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS change_log (
    seq           BIGSERIAL    PRIMARY KEY,
    slide         TEXT        NOT NULL,
    token         TEXT        NOT NULL,
    annotation_id TEXT,
    op            TEXT        NOT NULL,                  -- add/update/delete
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------- --
-- 常用查询索引（幂等）
-- --------------------------------------------------------------------------- --
CREATE INDEX IF NOT EXISTS idx_rois_token       ON rois(token);
CREATE INDEX IF NOT EXISTS idx_rois_slide       ON rois(slide);
CREATE INDEX IF NOT EXISTS idx_change_log_slide ON change_log(slide, seq);
CREATE INDEX IF NOT EXISTS idx_grants_user      ON grants(user_id);
CREATE INDEX IF NOT EXISTS idx_project_slides_pid ON project_slides(project_id);
