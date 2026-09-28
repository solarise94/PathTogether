-- =========================================================================== --
-- 0074_upload_tasks_quota_mode.sql：任务创建时配额身份快照（R15 核账合同）。
--
-- 背景（R15-1）：owner/本地免登录上传按身份合同（upload_guard.quota_applies：
-- 仅 role=user 且有 user_id 需要容量预约）合法地没有 reservation_id。核账
-- 工具只凭 rid 缺失判定 missing 会把正常豁免任务终态化。users.role 无应用
-- 内变更路径，但为使「创建时身份」可证明（角色事后经 SQL 变更不影响历史
-- 裁决），任务行持久化创建时快照：
--   duty   = 创建时 role=user（需预约）
--   exempt = 创建时非 user 角色 / 本地免登录（空 owner_user_id）
--   NULL   = 存量行：核账按当前 users.role 裁决（空 owner=本地免登录豁免；
--            非空但无用户行 = 不可证明 → blocker 人工核对，不自动终止）
-- 只服务核账/审计，运行时不读；不影响任何现有约束。
-- 幂等：ADD COLUMN IF NOT EXISTS。
-- =========================================================================== --

ALTER TABLE upload_tasks ADD COLUMN IF NOT EXISTS quota_mode TEXT;

COMMENT ON COLUMN upload_tasks.quota_mode IS
    '创建时配额身份快照（0074；R15）：duty=role user 需预约；exempt=非 user 角色/本地免登录；NULL=存量（按当前角色裁决）';
