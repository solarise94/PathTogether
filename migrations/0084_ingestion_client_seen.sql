-- =========================================================================== --
-- 0084_ingestion_client_seen.sql：COS 直传任务的客户端在场证据与安全失败细节
-- （upload-liveness 生产事故修复： vanished client 长期占用活跃名额与池预约）。
--
-- 新增列（均可空，旧行不回填——NULL = 从未见场，按 fail-safe 处理）：
--   - ingestion_jobs.client_seen_at TIMESTAMPTZ
--       任务归属客户端最近一次被服务端看到的时刻。创建即写入；owner 的
--       状态轮询 / parts/sign / upload-complete 触点按节流（服务端常量）
--       刷新。两个 liveness 闸消费它：
--         * waiting 准入闸：静默超过 COS_WAITING_CLIENT_SEEN_TIMEOUT_SECONDS
--           的等待任务不再准入，就地终止（fail_code=client_gone）；
--         * uploading 失活 sweep：静默超过 COS_UPLOADING_CLIENT_IDLE_SECONDS
--           的上传任务经 COS ListParts 活性复核后终止
--           （fail_code=client_idle_timeout）或刷新在场时刻。
--       NULL（升级窗口在途旧行）不参与任何失活判定——绝不因缺证据终止，
--       既有 waiting/job 绝对期限 sweep 照常兜底。
--   - ingestion_jobs.fail_detail TEXT
--       终态失败的安全细节（稳定白名单词表，见 ingestion_store.VALID_FAIL_
--       DETAILS：raster_format_mismatch / slide_open_unsupported /
--       slide_open_failed / invalid_slide）。fail_code 保持 validation_failed
--       兼容；白名单外的值一律不落库（不透出内部异常细节）。状态体按
--       fail_detail 透出，前端映射为可读的针对性文案。
--
-- 幂等：IF NOT EXISTS 对已应用库重跑 no-op；不新增索引（liveness 判定按
-- state + client_seen_at 过滤的行数极小——活跃/等待任务集合本身有界，
-- 不为它建部分索引）。
-- =========================================================================== --

ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS client_seen_at TIMESTAMPTZ;
ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS fail_detail TEXT;

COMMENT ON COLUMN ingestion_jobs.client_seen_at IS
    '归属客户端最近一次被服务端看到的时刻（创建写入；status/sign/complete 触点节流刷新；NULL=升级窗口旧行，liveness 判定 fail-safe 跳过）';
COMMENT ON COLUMN ingestion_jobs.fail_detail IS
    '终态失败的安全细节（白名单词表，如 raster_format_mismatch；fail_code 兼容不变）';

INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0084_ingestion_client_seen', now(), 'system',
     'ingestion.liveness_columns_applied', 'ingestion_jobs',
     jsonb_build_object('columns', 'client_seen_at,fail_detail',
                        'backfill', 'none; NULL rows are fail-safe (never terminated for liveness)'))
ON CONFLICT (event_id) DO NOTHING;
