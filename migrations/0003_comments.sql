-- =========================================================================== --
-- 0003_comments.sql：评论线程 + 审核状态字段（Stage 3c-1，docs §5.3）
--
-- 新增实体：
--   - comments 表：标注级评论线程（一层回复，软删）。权威负载存 data JSONB
--     （同 rois 语义），离散列镜像供过滤/索引。增删写 change_log
--     （op=comment_add/comment_delete），list_changes 以 type=comment 返回。
--
-- rois.data 已含 review_status（3c-1：AI 标注 pending / 人工 none），无需新增离散列
-- （review 只改 data 内字段，离散列无查询需求）。history 数组同样只存 data 内。
--
-- 全部幂等（IF NOT EXISTS）。change_log.op 无 CHECK 约束（0001 未加，保持一致）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS comments (
    comment_id      TEXT        PRIMARY KEY,             -- 形如 cmt_<uuid>
    annotation_id   TEXT,                                 -- 挂靠的标注稳定 id（可空）
    slide           TEXT        NOT NULL DEFAULT '',      -- legacy filename（变更流过滤用）
    token           TEXT        NOT NULL DEFAULT '',      -- 归属上下文：admin 伪 token 或分享 token
    author_user_id  TEXT,                                 -- 可空=guest
    author_label    TEXT        NOT NULL DEFAULT '',      -- 展示名快照
    body            TEXT        NOT NULL DEFAULT '',      -- ≤2000 字
    parent_id       TEXT,                                 -- 回复目标评论 id（一层，不嵌套）
    resolved        BOOLEAN     NOT NULL DEFAULT FALSE,
    deleted         BOOLEAN     NOT NULL DEFAULT FALSE,   -- 软删 tombstone
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    data            JSONB       NOT NULL DEFAULT '{}'::jsonb  -- 权威 dict（含 change_seq）
);
COMMENT ON COLUMN comments.deleted IS '软删 tombstone：list_comments 默认不返回，list_changes 仍返回（带 deleted=true）';

-- 评论查询索引（幂等）
CREATE INDEX IF NOT EXISTS idx_comments_annotation ON comments(annotation_id);
CREATE INDEX IF NOT EXISTS idx_comments_slide      ON comments(slide);
CREATE INDEX IF NOT EXISTS idx_comments_parent     ON comments(parent_id) WHERE parent_id IS NOT NULL;
