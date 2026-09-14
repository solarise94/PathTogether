-- =========================================================================== --
-- 0051_baidu_remote_imports.sql：百度分享导入（W5，spec §6.3）。
--
-- 四张表：
--   baidu_enumerations  分享枚举任务（share_url/extraction Fernet 加密落库，
--                       状态 queued → enumerating → ready|failed|expired；
--                       仅 ready 且 complete 且未过期可导入；枚举阶段
--                       transfer/download/delete 计数必须为 0）
--   baidu_candidates    枚举出的分享条目（目录/不可选条目也保留 + 原因；
--                       fs_id 恒为 TEXT 字符串，UNIQUE(enumeration_id, fs_id)
--                       去重；size_bytes BIGINT，公开视图转十进制字符串）
--   baidu_import_batches 导入批次（owner+idempotency_key 唯一；配额预占引用；
--                       状态 queued/running/succeeded/partial_failed/failed/
--                       cancelled；清理状态独立，不反向破坏 ready）
--   baidu_import_items  批次条目（阶段 queued → transferring → downloading →
--                       validating → converting → ingesting → ready 及
--                       failed/cancelled；崩溃恢复对账字段 transfer_task_id/
--                       source_sha256/ingest_token 幂等防重）
--
-- 无 users 外键（owner_user_id 为逻辑归属，与 upload_reservations 等表同款）；
-- 删除语义由应用层管理（枚举删除 CASCADE 候选；批次删除 CASCADE 条目）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS baidu_enumerations (
    id                 TEXT        PRIMARY KEY,
    owner_user_id      TEXT        NOT NULL,
    share_url_enc      TEXT        NOT NULL,
    extraction_enc     TEXT,
    state              TEXT        NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'enumerating', 'ready', 'failed',
                         'expired')),
    complete           BOOLEAN     NOT NULL DEFAULT FALSE,
    scanned_count      INTEGER     NOT NULL DEFAULT 0,
    candidate_count    INTEGER     NOT NULL DEFAULT 0,
    error_code         TEXT,
    incomplete_reason  TEXT,
    expires_at         TIMESTAMPTZ NOT NULL
        DEFAULT now() + interval '24 hours',
    max_depth          INTEGER     NOT NULL DEFAULT 32,
    max_entries        INTEGER     NOT NULL DEFAULT 10000,
    -- 枚举副作用审计计数：枚举阶段必须保持 0（spec §6.3）
    transfer_calls     INTEGER     NOT NULL DEFAULT 0,
    download_calls     INTEGER     NOT NULL DEFAULT 0,
    delete_calls       INTEGER     NOT NULL DEFAULT 0,
    lease_owner        TEXT,
    lease_token        TEXT,
    lease_expires_at   TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE baidu_enumerations IS
    '百度分享只读枚举任务（0051 W5）；share_url/extraction 用 BAIDU_SHARE_'
    'SECRET_KEY 派生 Fernet 加密，明文绝不落库/出线';

CREATE INDEX IF NOT EXISTS idx_baidu_enumerations_owner
    ON baidu_enumerations (owner_user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_baidu_enumerations_state
    ON baidu_enumerations (state, created_at);

CREATE TABLE IF NOT EXISTS baidu_candidates (
    id               TEXT        PRIMARY KEY,
    enumeration_id   TEXT        NOT NULL
        REFERENCES baidu_enumerations(id) ON DELETE CASCADE,
    fs_id            TEXT        NOT NULL,
    name             TEXT        NOT NULL,
    relative_path    TEXT        NOT NULL,
    size_bytes       BIGINT      NOT NULL DEFAULT 0,
    format           TEXT        NOT NULL DEFAULT '',
    capability       TEXT        NOT NULL DEFAULT '',
    selectable       BOOLEAN     NOT NULL DEFAULT FALSE,
    reason_code      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (enumeration_id, fs_id)
);

COMMENT ON TABLE baidu_candidates IS
    '百度分享枚举候选（0051 W5）；目录/channel.json 等不可选条目也保留原因；'
    'fs_id 恒为字符串（跨 JSON 不丢精度）';

CREATE INDEX IF NOT EXISTS idx_baidu_candidates_enum
    ON baidu_candidates (enumeration_id, relative_path);

CREATE TABLE IF NOT EXISTS baidu_import_batches (
    id                   TEXT        PRIMARY KEY,
    owner_user_id        TEXT        NOT NULL,
    enumeration_id       TEXT        NOT NULL
        REFERENCES baidu_enumerations(id),
    idempotency_key      TEXT        NOT NULL,
    payload_sha256       TEXT        NOT NULL,
    target_project_id    TEXT,
    state                TEXT        NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'succeeded', 'partial_failed',
                         'failed', 'cancelled')),
    quota_reservation_id TEXT,
    total_bytes          BIGINT      NOT NULL DEFAULT 0,
    cancel_requested     BOOLEAN     NOT NULL DEFAULT FALSE,
    error_code           TEXT,
    lease_owner          TEXT,
    lease_token          TEXT,
    lease_expires_at     TIMESTAMPTZ,
    cleanup_state        TEXT        NOT NULL DEFAULT 'not_needed'
        CHECK (cleanup_state IN ('not_needed', 'pending', 'succeeded',
                                 'failed')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, idempotency_key)
);

COMMENT ON TABLE baidu_import_batches IS
    '百度分享导入批次（0051 W5）；同 owner+幂等键唯一，同键同载荷重放返回'
    '原批次，同键异载荷 409；quota_reservation_id 引用 upload_reservations';

CREATE INDEX IF NOT EXISTS idx_baidu_import_batches_owner
    ON baidu_import_batches (owner_user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_baidu_import_batches_state
    ON baidu_import_batches (state, created_at);

CREATE TABLE IF NOT EXISTS baidu_import_items (
    id                   TEXT        PRIMARY KEY,
    batch_id             TEXT        NOT NULL
        REFERENCES baidu_import_batches(id) ON DELETE CASCADE,
    candidate_id         TEXT        NOT NULL,
    fs_id                TEXT        NOT NULL,
    name                 TEXT        NOT NULL,
    relative_path        TEXT        NOT NULL,
    stage                TEXT        NOT NULL DEFAULT 'queued'
        CHECK (stage IN ('queued', 'transferring', 'downloading',
                         'validating', 'converting', 'ingesting', 'ready',
                         'failed', 'cancelled')),
    error_code           TEXT,
    staging_path         TEXT,
    quota_reservation_id TEXT,
    conversion_job_id    TEXT,
    ingest_token         TEXT,
    cleanup_state        TEXT        NOT NULL DEFAULT 'not_needed'
        CHECK (cleanup_state IN ('not_needed', 'pending', 'succeeded',
                                 'failed')),
    source_size          BIGINT      NOT NULL DEFAULT 0,
    source_sha256        TEXT,
    -- 崩溃恢复对账：转存 task_id 已存在 → 先 poll/对账，不无条件重转存
    transfer_task_id     TEXT,
    attempt              INTEGER     NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE baidu_import_items IS
    '百度分享导入批次条目（0051 W5）；ingest_token/source_sha256/transfer_'
    'task_id 是崩溃恢复幂等凭证（重启先对账，不重复转存/下载/入库）';

CREATE INDEX IF NOT EXISTS idx_baidu_import_items_batch
    ON baidu_import_items (batch_id, stage);
