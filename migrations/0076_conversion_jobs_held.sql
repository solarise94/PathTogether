-- 0076：conversion_jobs 不可领取态 held（R16 审查 P1-1）。
--
-- COS conversion 形态的交接顺序：父 ingestion 任务行锁内建/复用 held 子
-- 任务 → 持久化父 commit intent → 源文件搬入子任务 staging → 源字节结算
-- 与 held→queued 同一事务。held 不被转换 worker 领取（领取只看 queued/
-- converting/validating）；父任务在 intent 前进入终态时，同一终态事务把
-- 本任务创建的 held 子任务置 cancelled（产物 staging 资产置 failed）——
-- 取消先赢不留下可执行子任务，也无需事后补偿。
--
-- 注：原 0076_upload_drain_freeze（检查点 A 冻结清单）随检查点 A 取消而
-- 删除，从未部署到生产。

ALTER TABLE conversion_jobs DROP CONSTRAINT IF EXISTS conversion_jobs_state_check;
ALTER TABLE conversion_jobs ADD CONSTRAINT conversion_jobs_state_check
    CHECK (state IN ('held', 'queued', 'converting', 'validating', 'ready',
                     'failed', 'cancelled'));
