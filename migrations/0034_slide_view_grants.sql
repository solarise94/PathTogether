-- =========================================================================== --
-- 0034_slide_view_grants.sql：切片可见性显式授权（review P0 2026-09-05 读隔离）
--
-- 背景：owner 不再默认可见全部切片（读隔离模型：自己的 ∪ public ∪ 显式
-- 授权）。此前唯一的授权原语是 share 认领（grants.token FK → shares.token），
-- 复用它是为每片切片伪造一条内部 share：污染分享列表、撤销 share 即静默
-- 收回授权、权限被 share permissions 夹逼、且存在 expires_at TTL 陷阱。
-- 故加这张最小直授表：持久、无 TTL、幂等（主键去重）。
--
-- 不加外键（与 0001 中 rois.slide 同理）：slide 以 legacy_filename 为键，
-- slides 行是 set_slide_meta 懒建立的——授权可先于 meta 行存在（孤儿切片
-- 经管理台授权后恢复可见，正是本表的目标场景之一）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS slide_view_grants (
    slide_name  TEXT        NOT NULL,                -- 切片名（legacy_filename）
    user_id     TEXT        NOT NULL,                -- 被授权主体（当前仅 owner 自授权）
    granted_by  TEXT,                                -- 操作者（admin actor user_id）
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (slide_name, user_id)
);

CREATE INDEX IF NOT EXISTS idx_slide_view_grants_user ON slide_view_grants(user_id);
