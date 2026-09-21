-- =========================================================================== --
-- 0065_research_deletion_unique_unresolved.sql：未了结删除任务唯一约束
-- 收口（P2 并发缺口修复，review 2026-09-21）。
--
-- 缺口：0062 的部分唯一索引 research_data_deletion_jobs_one_active 只约束
-- status IN ('pending','running')；0064 引入 failed 终态后未同步更新索引。
-- 于是两个删除请求同时查到无任务时，若第一个任务在第二个请求插入前已
-- 执行失败转 failed（应用层 SELECT 与 INSERT 之间的竞态窗口），第二个
-- 仍能新建任务——产生多条「未了结」任务。而阻断语义自 0064 起就是
-- 「completed 之外（pending/running/failed）一律阻断」（research_consent_store
-- .UNRESOLVED_DELETION_JOBS_SQL），唯一约束必须与之一致：
--
--   pending / running：在队列或执行中；
--   failed：数据还没删成（退避重试中或达上限的终态，待人工处置）——
--     删除义务未了结，同样不得再建第二条；
--   completed：唯一解除约束的终态（任务行保留为备份恢复重放清单）。
--
-- PostgreSQL 部分唯一索引的 WHERE 谓词不能 ALTER，只能 DROP 旧索引 +
-- CREATE 新索引。迁移原子性：migration runner（pg_store.ensure_schema）
-- 对本文件单次 execute + 单次 commit，整体处于同一事务，校验失败 /
-- 建索引失败均整体回滚（0022 同款口径）。
--
-- 存量校验：建新索引前检查同 user 多条未了结（pending/running/failed）
-- 任务——存在即说明 0062→0065 缺口期已产生脏数据，RAISE 报错中止，
-- **不静默**合并/标完成（保留哪条、是否重放删除需人工按 error_code
-- 处置；静默标 completed 会让未删成的研究副本脱离阻断）。
--
-- 幂等：校验对「已迁移」状态重跑成立；DROP IF EXISTS + CREATE IF NOT
-- EXISTS 重跑 no-op。
-- =========================================================================== --

DO $$
DECLARE
    conflicts TEXT;
BEGIN
    SELECT string_agg(user_id || '(' || n || '条: ' || job_ids || ')', '; ')
      INTO conflicts
      FROM (SELECT user_id, count(*) AS n,
                   string_agg(job_id, ', ' ORDER BY created_at, job_id)
                       AS job_ids
              FROM research_data_deletion_jobs
             WHERE status IN ('pending', 'running', 'failed')
             GROUP BY user_id
            HAVING count(*) > 1) c;
    IF conflicts IS NOT NULL THEN
        RAISE EXCEPTION
            '0065 前置校验失败：以下用户存在多条未了结（pending/running/failed）研究副本删除任务：%。这是 0062 索引缺口期产生的存量，需人工核对处置（不得由迁移静默标记完成——未删成的研究副本不得脱离阻断），处置后重跑本迁移',
            conflicts;
    END IF;
END
$$;

-- 旧索引（只约束 pending/running）→ 新索引（与 UNRESOLVED_DELETION_JOBS_SQL
-- 对齐：completed 之外一律唯一）。同名重建，引用方（测试/运维清单）不换名。
DROP INDEX IF EXISTS research_data_deletion_jobs_one_active;

CREATE UNIQUE INDEX IF NOT EXISTS research_data_deletion_jobs_one_active
    ON research_data_deletion_jobs (user_id)
    WHERE status IN ('pending', 'running', 'failed');

COMMENT ON INDEX research_data_deletion_jobs_one_active IS
    '每用户至多一条未了结（pending/running/failed——completed 之外）研究副本删除任务（0062 建，0065 谓词扩到 failed：终态 failed 删除义务未了结，同样阻断再建；应用层 ON CONFLICT DO NOTHING 幂等兜底）';
