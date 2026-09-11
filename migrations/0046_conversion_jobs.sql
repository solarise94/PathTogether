-- =========================================================================== --
-- 0046_conversion_jobs.sql：KFB 等 convert-required 格式的后台转换任务
-- （docs/kfb-ingestion-converter-review.md Phase B）。
--
-- 上传 commit 对 .kfb 返回 202 + conversion_job_id；独立 worker 经
-- SELECT ... FOR UPDATE SKIP LOCKED 领取，写 canonical .tif 后再对 Viewer
-- 可见。不修改 upload_tasks 状态机。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS conversion_jobs (
    id                      TEXT        PRIMARY KEY,
    owner_user_id           TEXT        NOT NULL DEFAULT '',
    upload_id               TEXT,
    source_name             TEXT        NOT NULL,
    source_sha256           TEXT        NOT NULL,
    source_format           TEXT        NOT NULL,
    canonical_name          TEXT,
    converter_id            TEXT        NOT NULL,
    converter_version       TEXT        NOT NULL,
    state                   TEXT        NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'converting', 'validating', 'ready',
                         'failed', 'cancelled')),
    attempt                 INTEGER     NOT NULL DEFAULT 0,
    lease_owner             TEXT,
    lease_expires_at        TIMESTAMPTZ,
    heartbeat_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    error_code              TEXT,
    error_detail_internal   TEXT,
    canonical_settled_bytes BIGINT
);

COMMENT ON TABLE conversion_jobs IS
    '后台切片转换任务（KFB→BigTIFF）；Web 请求只建任务，worker 持租约执行';

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_jobs_owner_hash
    ON conversion_jobs (owner_user_id, source_sha256, converter_id,
                        converter_version);

CREATE INDEX IF NOT EXISTS idx_conversion_jobs_state_created
    ON conversion_jobs (state, created_at);

CREATE INDEX IF NOT EXISTS idx_conversion_jobs_canonical
    ON conversion_jobs (canonical_name)
    WHERE canonical_name IS NOT NULL;

-- 进行中/已就绪任务占用 canonical 名，防止与普通上传或另一转换互相覆盖
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_jobs_canonical_live
    ON conversion_jobs (canonical_name)
    WHERE canonical_name IS NOT NULL
      AND state NOT IN ('failed', 'cancelled');
