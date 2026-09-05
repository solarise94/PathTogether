-- =========================================================================== --
-- 0035_slide_view_grants_asset_generation.sql：授权绑定资产代（升级 B R7）
--
-- 背景（review R7）：slide_view_grants 以 slide_name（legacy_filename）为键，
-- 而 legacy 名会经历「删除 → 同名再上传」的资产替换。若授权只按名匹配，
-- 「加入 A.svs → 删除 A.svs → 新文件再次使用 A.svs」会让旧授权静默继承到
-- 新内容上。本迁移为授权行补绑**资产生代**（当前同名元数据的稳定 slide_id），
-- 授权校验侧（share_store_pg.slide_view_grants_for_user）要求行上 slide_id
-- 与 slides 行当前 slide_id 一致（IS NOT DISTINCT FROM，兼容双方皆 NULL 的
-- 孤儿授权形态——无 meta 行的切片经管理台授权恢复可见的目标场景不变）。
--
-- 失效语义（升级 B 裁决）：同名替换后旧授权**不自动生效**，需要重新添加。
-- 上传通道全部 no-clobber（409 name_unavailable），同名替换必经删除路径，
-- 删除（app.py api_slide_delete）在 unlink 前按名 + slide_id 清理本表行。
-- 因此本迁移不做任何破坏性回填之外的动作：仅为既有行 backfill 当前同名
-- meta 的 slide_id（无 meta 行保持 NULL = 孤儿授权，语义不变）。
--
-- 可重复执行：ALTER ... IF NOT EXISTS / UPDATE ... WHERE slide_id IS NULL /
-- CREATE INDEX IF NOT EXISTS 均幂等；重放不改变已 backfill 的行。
-- --------------------------------------------------------------------------- --
ALTER TABLE slide_view_grants ADD COLUMN IF NOT EXISTS slide_id TEXT;

UPDATE slide_view_grants g
SET slide_id = s.slide_id
FROM slides s
WHERE s.legacy_filename = g.slide_name
  AND g.slide_id IS NULL
  AND s.slide_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_slide_view_grants_slide_id
    ON slide_view_grants(slide_id);
