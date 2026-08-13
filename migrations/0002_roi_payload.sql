-- =========================================================================== --
-- 0002_roi_payload.sql：ROI 全量负载 + 稳定插入序（Stage 3b-2）
--
-- 0001 的 rois 表只有离散列，但 JSON 语义（share_store_json.add_roi 等）会在每
-- 条 roi 上携带完整 dict（ts/source/created_by_session_id/revision/change_seq/
-- effect_key/几何字段等）。为了在 PostgreSQL 后端实现「像素级对齐」的往返，这里
-- 增加：
--   - rois.data（JSONB）：整条 roi 的权威 dict（读取/返回统一从这里取）；
--   - rois.insert_seq（BIGSERIAL）：按插入顺序的稳定序号，等价 JSON 文件内数组
--     顺序，供 token 内 index / 变更流排序使用。
-- 离散列（token/slide/annotation_id/label/type/shared/note/deleted/geom/…）仍
-- 同步填充，供按 token/slide/deleted/shared 的高效过滤与索引；data 保真。
-- 全部幂等（迁移 runner 按 schema_migrations 去重 + IF NOT EXISTS 双保险）。
-- =========================================================================== --

ALTER TABLE rois ADD COLUMN IF NOT EXISTS data JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE rois ADD COLUMN IF NOT EXISTS insert_seq BIGSERIAL;

-- token 内按插入序索引 + slide 按插入序索引（供 list_rois / list_changes 排序）
CREATE INDEX IF NOT EXISTS idx_rois_token_seq  ON rois(token, insert_seq);
CREATE INDEX IF NOT EXISTS idx_rois_slide_seq  ON rois(slide, insert_seq);
CREATE INDEX IF NOT EXISTS idx_rois_effect_key ON rois ((data->>'effect_key'))
    WHERE data ? 'effect_key';
