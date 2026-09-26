-- =========================================================================== --
-- 0070_slide_delete_jobs_lease.sql：删除执行器的租约列（P5 合同 §1-2）。
--
-- docs/slide-id-refactor-p5-contract-20260925.md §1/§2：slide_delete_jobs 是
-- 删除的唯一执行载体；app.py 后台 daemon（参照 ai-binding-attach-retry 模式）
-- 周期性领取 pending/failed 任务（lease + 退避）。0067 建表时预留了
-- attempts/last_error，但未落 lease 列——P5 接线需要：
--   lease_owner       领取 worker 的标识（uuid/hostname:pid 形态，仅观测用
--                     ——并发安全由「claim 的 UPDATE ... WHERE 谓词 +
--                     FOR UPDATE SKIP LOCKED + slides 行级 CAS」共同裁定，
--                     不信任 lease_owner 做互斥判定）；
--   lease_expires_at  租约到期时刻：state='cleaning' 且到期 → 可被重领
--                     （worker 崩溃恢复）；结算幂等不依赖租约，而由
--                     deleting→deleted 的 CAS（同事务减账）唯一裁定——
--                     重复执行/双 worker 不得重复减账（合同 §2）。
-- 领取调度索引：(state, updated_at)——pending 即领、failed 按 updated_at+
-- 退避（base*2^attempts，封顶）、cleaning 按 lease_expires_at。
--
-- 幂等：ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS；重跑 no-op。
-- =========================================================================== --

ALTER TABLE slide_delete_jobs
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

COMMENT ON COLUMN slide_delete_jobs.lease_owner IS
    '领取本任务的 worker 标识（观测用；互斥由 claim UPDATE 谓词 + SKIP LOCKED + 结算 CAS 裁定）';
COMMENT ON COLUMN slide_delete_jobs.lease_expires_at IS
    '租约到期：state=cleaning 且到期可被重领（崩溃恢复）；重复执行不重复减账由 deleting→deleted CAS 唯一裁定';

CREATE INDEX IF NOT EXISTS idx_slide_delete_jobs_state_due
    ON slide_delete_jobs (state, updated_at);
