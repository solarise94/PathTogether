-- =========================================================================== --
-- 0021_upload_tasks_v1_artifacts.sql：V1（旧单请求 /api/upload）接入
-- upload_tasks 收口状态机（review-2026-08-29 §10.4 G7）。
--
-- V1 单文件 / ZIP 与 V2 共用 committing/committed 状态机与 PG 同事务 consume：
-- 内容验证完成后、提升之前，先把 artifact manifest 与 settle_bytes（=
-- declared_size）随 commit token 一起持久化到本表。ZIP 的多文件提升无法
-- 原子完成，崩溃恢复必须凭 manifest 区分「全未提升（回滚+释放）/ 全已提升
-- （幂等收口）/ 部分提升或证据冲突（fail-closed 告警，绝不按过期时间盲
-- release）」——因此 manifest 必须与任务同表持久化，禁止第二张补偿表。
--
-- 只增一列（幂等，对齐 0001-0018 风格）：V2 任务恒 NULL（单文件证据 =
-- safe_name + declared_size，恢复逻辑不变）。
-- =========================================================================== --

ALTER TABLE upload_tasks ADD COLUMN IF NOT EXISTS v1_artifacts TEXT;

COMMENT ON COLUMN upload_tasks.v1_artifacts IS
    'V1 legacy 上传的 artifact manifest（JSON 数组 [{name,size,sha256,slide}]；'
    '提升前随 commit 受理持久化，V2 任务恒 NULL）';
