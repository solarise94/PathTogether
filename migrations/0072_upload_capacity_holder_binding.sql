-- =========================================================================== --
-- 0072_upload_capacity_holder_binding.sql：上传容量生命周期——持有者绑定
-- （2026-09-27，docs/task-capacity-lifecycle-repair-agent-plan-20260927.md §4.1）。
--
-- 模型：「任务持有容量，租约控制执行」。upload_reservations 行可绑定持有
-- 者（任务/批次 + 用途）；已绑定（holder_id 非空）的预约**不参加 TTL 过期
-- 回收**（expires_at 退化为执行租约时效，不再控制容量归属）——任务停止
-- 心跳不代表字节消失，容量只在发布结算（consume）或**清理确认后**
-- （release）才离开账本。
--
-- 列：
--   holder_kind / holder_id / purpose：持有者三元组（有限枚举，全空或全有
--     ——CHECK 约束）。种类×用途在 upload_guard.HOLDER_KINDS /
--     HOLDER_PURPOSES 固定：
--       upload_task    × upload        （V1/V2/ZIP 传输+发布）
--       ingestion_job  × ingest_local  （COS 摄取本地容量）
--       baidu_batch    × baidu_import  （百度批次一次性预算）
--     绑定必须与任务行创建同事务（跨表无外键，约束靠代码 + 审计双向核验）。
--   origin：'admission'（正常准入）/ 'reconcile'（存量核账补建）。
--     每小时准入计数只统计 origin='admission'（核账补记不算一次用户上传）。
--
-- 唯一性：同一 (holder_kind, holder_id, purpose) 至多一行未结算责任
-- （state='reserved'）。批次预算按批次记一次，不复制到子任务。
--
-- ingestion_jobs 本地清理进度（与远端 cleanup_* 分列，§5：本地与远端是
-- 不同资源，各自按清理证据释放）：local_cleanup_status ∈ none/pending/
-- cleaned/failed + attempts/last_error/next_retry_at。pending/failed 期间
-- local_reservation_id 的容量责任持续保留。
--
-- 幂等：IF NOT EXISTS / DO NOTHING；重跑 no-op。存量行绑定由
-- scripts/reconcile_upload_capacity.py 在维护窗口核账补齐（本迁移不猜）。
-- =========================================================================== --

ALTER TABLE upload_reservations
    ADD COLUMN IF NOT EXISTS holder_kind TEXT,
    ADD COLUMN IF NOT EXISTS holder_id   TEXT,
    ADD COLUMN IF NOT EXISTS purpose     TEXT,
    ADD COLUMN IF NOT EXISTS origin      TEXT NOT NULL DEFAULT 'admission';

ALTER TABLE upload_reservations
    DROP CONSTRAINT IF EXISTS upload_reservations_holder_complete;
ALTER TABLE upload_reservations
    ADD CONSTRAINT upload_reservations_holder_complete CHECK (
        (holder_kind IS NULL AND holder_id IS NULL AND purpose IS NULL)
        OR (holder_kind IS NOT NULL AND holder_id IS NOT NULL
            AND purpose IS NOT NULL));

ALTER TABLE upload_reservations
    DROP CONSTRAINT IF EXISTS upload_reservations_origin_kind;
ALTER TABLE upload_reservations
    ADD CONSTRAINT upload_reservations_origin_kind
    CHECK (origin IN ('admission', 'reconcile'));

CREATE UNIQUE INDEX IF NOT EXISTS upload_reservations_one_open_duty
    ON upload_reservations (holder_kind, holder_id, purpose)
    WHERE state = 'reserved' AND holder_id IS NOT NULL;

COMMENT ON COLUMN upload_reservations.holder_kind IS
    '持有者类型枚举：upload_task / ingestion_job / baidu_batch（空=准备期未绑定）';
COMMENT ON COLUMN upload_reservations.holder_id IS
    '持有者任务/批次 ID；绑定后不可改，释放/转实占需持有者上下文校验';
COMMENT ON COLUMN upload_reservations.purpose IS
    '容量用途枚举（按通道固定）：upload / ingest_local / baidu_import';
COMMENT ON COLUMN upload_reservations.origin IS
    'admission=正常准入；reconcile=存量核账补建（不计每小时准入数）';

ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS local_cleanup_status TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS local_cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS local_cleanup_last_error TEXT,
    ADD COLUMN IF NOT EXISTS local_cleanup_next_retry_at TIMESTAMPTZ;

ALTER TABLE ingestion_jobs
    DROP CONSTRAINT IF EXISTS ingestion_jobs_local_cleanup_status_kind;
ALTER TABLE ingestion_jobs
    ADD CONSTRAINT ingestion_jobs_local_cleanup_status_kind
    CHECK (local_cleanup_status IN ('none', 'pending', 'cleaned', 'failed'));

COMMENT ON COLUMN ingestion_jobs.local_cleanup_status IS
    '本地暂存清理进度（与远端 cleanup_* 独立）：pending/failed 期间本地预约'
    '容量责任保留，清理确认（cleaned）后才释放';
