-- =========================================================================== --
-- 0064_research_deletion_execution.sql：研究副本删除执行器落地
-- （docs/agent-plan-20260921-registration-consent-research.md §6.3/§8，P1 修复：
-- 0062 只落任务与状态，本迁移补执行器所需的退避重试簿记）。
--
--   - attempts：执行尝试次数（领取时 +1；达到 research_consent_store
--     .MAX_DELETION_ATTEMPTS 后 failed 为终态，不再自动重试，需人工介入）；
--   - next_retry_at：失败退避后的下次可领取时间（指数退避；pending/新任务
--     默认 now() 立即可领取）；
--   - 领取索引：worker 轮询按 (status, next_retry_at) 扫待执行任务。
--
-- 多 worker / 重启安全由应用层保证：领取走 SELECT ... FOR UPDATE SKIP LOCKED；
-- 删除本身幂等（research_* 表按 user/consent_epoch 谓词删除，重复执行无害）；
-- running 超租约未更新可被回收重领。
--
-- 幂等：全部 IF NOT EXISTS；不修改已发布迁移。
-- =========================================================================== --

ALTER TABLE research_data_deletion_jobs
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now();

COMMENT ON COLUMN research_data_deletion_jobs.attempts IS
    '执行尝试次数（0064：领取时 +1；达 research_consent_store.MAX_DELETION_ATTEMPTS 后 failed 为终态）';
COMMENT ON COLUMN research_data_deletion_jobs.next_retry_at IS
    '失败退避后的下次可领取时间（0064；指数退避，pending 默认 now() 立即可领取）';

CREATE INDEX IF NOT EXISTS idx_research_data_deletion_jobs_due
    ON research_data_deletion_jobs (status, next_retry_at);
