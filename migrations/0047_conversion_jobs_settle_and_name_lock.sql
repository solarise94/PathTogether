-- =========================================================================== --
-- 0047：conversion_jobs 补 canonical 配额幂等键 + live canonical 唯一占用。
-- 0046 若已应用，本文件幂等补齐；新库 0046 已含列时 ADD COLUMN IF NOT EXISTS
-- 为 no-op。
-- =========================================================================== --

ALTER TABLE conversion_jobs
    ADD COLUMN IF NOT EXISTS canonical_settled_bytes BIGINT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_jobs_canonical_live
    ON conversion_jobs (canonical_name)
    WHERE canonical_name IS NOT NULL
      AND state NOT IN ('failed', 'cancelled');
