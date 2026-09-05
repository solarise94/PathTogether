-- =========================================================================== --
-- 0036_share_rect_policy.sql：分享矩形策略字段（升级 C §6.4，批次 4b）
--
-- 背景：现有分享的 roi_sizes 是实际业务限制（6/6.5mm 预设子集），保留其语义。
-- 通用矩形升级后新增 rect_policy 两档：
--   - preset_only：仅允许原 6/6.5mm 正方形预设（默认档）；
--   - custom：创建者显式选择允许任意宽高矩形。
-- 旧分享缺该字段一律按 preset_only 解释（share_store_pg._share_rect_policy
-- 读侧 COALESCE 兜底），不因升级放宽既有分享的写入限制。
--
-- 本迁移只加可空列：已有行保持 NULL（读侧按 preset_only 解释），不回填、
-- 不改动任何既有分享的 roi_sizes/permissions。可重复执行（IF NOT EXISTS）。
-- --------------------------------------------------------------------------- --
ALTER TABLE shares ADD COLUMN IF NOT EXISTS rect_policy TEXT;

-- 兜底约束：已写入值必须是两档之一或 NULL（防御未来脏数据；幂等）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'shares_rect_policy_check'
    ) THEN
        ALTER TABLE shares ADD CONSTRAINT shares_rect_policy_check
            CHECK (rect_policy IS NULL OR rect_policy IN ('preset_only', 'custom'));
    END IF;
END $$;
