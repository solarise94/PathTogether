-- =========================================================================== --
-- 0068_upload_publish_intent.sql：统一本地发布的 publish intent 持久化
-- （docs/slide-id-refactor-p3-contract-20260925.md §2）。
--
-- upload_tasks ADD COLUMN commit_intent_json TEXT（可空）——P3 起 V2 分片与
-- V1 原生单文件走 slide_publish.publish_slide 六步发布合同（模块 docstring
-- 镜像）：提交前把 publish intent（task_ref、generation、slide_id、owner、
-- manifest、sha256、accounted_bytes——计划 §3.1-3「不能仅保存目标文件名」）
-- 与任务置 committing 落在**同一事务**（begin_commit/begin_legacy_commit 的
-- CAS 内写入），保证崩溃恢复可判定：
--   intent 前          → 任务仍 active/无任务，staging 可安全清理或重试；
--   intent 后/包发布后 → 幂等重跑发布（目标已存在且 manifest/sha 吻合 → 只做
--                        DB CAS；不吻合 → fail-closed 告警，不删不猜）；
--   DB 收口后          → intent 清空（finish 同事务），任务 committed。
--
-- 幂等：ADD COLUMN IF NOT EXISTS；对已应用库重跑 no-op。增列不删列——旧版
-- 镜像回滚读到新列不受影响（旧代码不 SELECT 该列）。
-- =========================================================================== --

ALTER TABLE upload_tasks
    ADD COLUMN IF NOT EXISTS commit_intent_json TEXT;

COMMENT ON COLUMN upload_tasks.commit_intent_json IS
    'publish intent（0068；P3 合同 §2/§3.3）：task_ref/generation/slide_id/'
    'owner/manifest/sha256/accounted_bytes 的 JSON。与任务置 committing 同事务'
    '写入（begin_commit CAS）；发布收口短事务内清空。committing 且有 intent '
    '= 崩溃恢复幂等重跑；无 intent = 回滚（语义同现状）';
