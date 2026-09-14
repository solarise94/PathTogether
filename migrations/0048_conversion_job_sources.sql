-- =========================================================================== --
-- 0048：同内容换名上传的源文件别名。去重复用 a.tif 时 b.kfb 仍占盘，
-- 删除 canonical 时必须一并清掉所有源，避免孤儿 KFB。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS conversion_job_sources (
    job_id          TEXT        NOT NULL,
    source_name     TEXT        NOT NULL,
    upload_id       TEXT,
    source_sha256   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, source_name)
);

COMMENT ON TABLE conversion_job_sources IS
    '转换任务的源文件别名（含主源）；删产物时联动删除全部源 KFB';

CREATE INDEX IF NOT EXISTS idx_conversion_job_sources_upload
    ON conversion_job_sources (upload_id)
    WHERE upload_id IS NOT NULL;
