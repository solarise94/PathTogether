-- =========================================================================== --
-- 0057_demo_catalog_bilingual.sql：受管 Demo 目录双语展示字段
--
-- 工单 B（docs viewer-demo-collaboration-review-plan-20260919.md §3）：
--   - demo_catalog 原有 display_name / description 语义不变（中文/缺省语言）；
--   - 新增 display_name_en / description_en（可空，NULL = 无译文，读取端回落
--     display_name / description，绝不猜值）；
--   - 只覆盖受管 Demo 目录（owner 维护的 allowlist 条目）。用户自定义的
--     工作台别名（slides.alias）不自动翻译，不在本迁移范围；
--   - 纯 additive 列（ADD COLUMN IF NOT EXISTS，幂等可重跑），不改既有行：
--     旧数据缺英文 → 前端按当前语言回落中文字段（明确回退，不阻塞展示）。
-- =========================================================================== --

ALTER TABLE demo_catalog
    ADD COLUMN IF NOT EXISTS display_name_en TEXT,
    ADD COLUMN IF NOT EXISTS description_en  TEXT;

COMMENT ON COLUMN demo_catalog.display_name_en IS
    'Demo 目录条目英文名（可空；NULL 时读取端回落 display_name）';
COMMENT ON COLUMN demo_catalog.description_en IS
    'Demo 目录条目英文说明（可空；NULL 时读取端回落 description）';
