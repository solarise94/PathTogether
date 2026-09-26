-- =========================================================================== --
-- 0069_conversion_slide_id_publish.sql：转换链切 slide ID 统一发布
-- （docs/slide-id-refactor-p4-contract-20260925.md §3；P4-app）。
--
-- 1. **拆除 canonical 名唯一锁**：DROP idx_conversion_jobs_canonical_live
--    （0047 引入的「live 任务对 canonical 名的全局唯一占用」）。P4 起同名
--    产物是独立新资产（各得各 slide_id，objects/<slide_id>/ 独占目录），
--    名占用不再构成拒绝理由；转换幂等键保持
--    (owner_user_id, source_sha256, converter_id, converter_version)。
--    另 DROP uq_baidu_import_items_slide_id（见下方裁决注释）。
-- 2. conversion_jobs.commit_intent_json（0068 同款语义）：publish intent 与
--    任务置 validating **同事务**持久化——worker 崩溃恢复幂等重判的唯一
--    证据源（slide_publish.PublishChannel 的 conversion 通道消费）。
-- 3. ingestion_jobs.slide_canonical_name 语义退役标注（P4-b 合同 §5.2：
--    展示快照，不再是资产身份/归属证据；slide_id 是唯一绑定源）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS / DROP INDEX IF EXISTS；对已应用库重跑 no-op。
-- =========================================================================== --

DROP INDEX IF EXISTS idx_conversion_jobs_canonical_live;

-- P4-app 裁决：baidu_import_items.slide_id 的全局唯一索引（0067）对
-- convert 分支过严——同 owner 同内容多次导入经 conversion 幂等键复用
-- **同一产物资产**（create_job 语义），多条目指向同一产物是设计行为
-- （P4-c 的 item.slide_id 回填在 P4-a 落地前因 job 无 slide_id 而未触发
-- 该路径）。native 条目的「一 item 一资产」由分配侧保证（每条目预分配
-- 独占 ID，跨条目共享只可能来自 convert 复用）。
DROP INDEX IF EXISTS uq_baidu_import_items_slide_id;

ALTER TABLE conversion_jobs
    ADD COLUMN IF NOT EXISTS commit_intent_json JSONB;

COMMENT ON COLUMN conversion_jobs.canonical_name IS
    '产物名展示快照（P4 起退役身份语义：同名产物=独立资产，各得各 slide_id；'
    '不再参与唯一性/占用判定，迁移前旧行的值保留不重写）';
COMMENT ON COLUMN conversion_jobs.slide_id IS
    '预分配产物资产 ID（P4 起 create_job 即绑定；幂等复用既有任务时随任务复用；'
    '删除产物按本列作废任务）';
COMMENT ON COLUMN conversion_jobs.commit_intent_json IS
    '统一发布 intent（slide_publish 六步第 1 步）：task_ref/generation/'
    'commit_token/slide_id/owner_user_id/manifest/sha256/accounted_bytes；'
    '与置 validating 同事务写入，结算事务内清空';
