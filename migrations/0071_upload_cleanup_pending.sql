-- =========================================================================== --
-- 0071_upload_cleanup_pending.sql：上传暂存清理失败的可重试持久状态
-- （2026-09-26 独立审查 R6 问题 3 修复）。
--
-- 缺陷：取消/预占失效路径的暂存清理 OSError 被吞掉后调用方仍释放容量
-- 预约——文件残留而 reserved 归零，容量责任丢失。
-- 契约（合同 §1.4「清理确认后释放」的严格化）：
--   - 清理失败 → 任务保持 cancelled/failed + 本表落一行（upload_id 唯一），
--     **保留 reservation**（容量责任不清零）；
--   - 重试：用户重复 DELETE 同一任务幂等重试清理；管理员可经
--     DELETE /api/admin/v1/slides/staging-residue 确认清理——两种路径都在
--     **清理确认后**才释放预占并删本表行；
--   - attempts/last_error 记录重试证据（运维可见，不静默）。
--
-- 幂等：CREATE TABLE IF NOT EXISTS；重跑 no-op。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS upload_cleanup_pending (
    upload_id      TEXT PRIMARY KEY,
    reservation_id TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE upload_cleanup_pending IS
    '暂存清理失败待重试（0071；R6 审查修复）：行存在期间容量责任保留'
    '（reservation 不释放），清理确认成功后才由重试路径删行+释放';
