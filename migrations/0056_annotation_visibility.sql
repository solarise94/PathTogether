-- =========================================================================== --
-- 0056_annotation_visibility.sql：标注可见性授权（工单 A / P0 数据隔离）。
--
-- 契约：「能看切片」≠「能看标注」。个人标注默认私有；跨主体可见必须经
-- annotation_grants 显式授权；rois.shared 不再隐含「对同片所有分享/用户
-- 开放」，只保留「对本条标注所在 token（若为真实分享链接）开放」的收窄
-- 语义（见 annotation_access.py / share_store_pg.set_roi_shared）。
--
-- 新增：
--   - annotation_grants：标注级授权（被授权主体 = user 或 share_token）。
--     不加外键（rois.token 同理：ADMIN_TOKEN 无 shares 行；annotation_id
--     逻辑引用 rois.annotation_id，tombstone 亦保留授权以便删除事件对被
--     授权者仍可见）。
--   - rois.visibility_status（private|granted|unclaimed）：查询/审计辅助
--     列。unclaimed = 无 owner 且无 visitor 的存量行，正常列表 API 一律
--     排除（仅本地免认证单租户态与管理清点可见）；运行期由 add_roi /
--     授权函数维护。判定权威在 annotation_access.is_unclaimed（按数据
--     形态计算），本列只是索引/报表 aid。
--   - rois.client_action_id：幂等创建键（前端/客户端生成），唯一约束
--     (owner_user_id, client_action_id) WHERE client_action_id IS NOT NULL
--     ——重复提交/重试返回原标注，不再落第二条。
--
-- 存量映射（一次性、幂等，绝不批量公开/按 label 推 owner/删历史）：
--   1) 有 owner、无 visitor、token 为真实分享链接 → 授予该 token 只读
--      （「这条标注创建/分享在该链接上」，不外溢到同片兄弟链接）；
--   2) 访客记录（有 visitor）：创建访客保持 me 可写（运行期按 visitor
--      哈希判定，无需迁移）；shared=true 且 token 为该分享 → 授予该
--      token 只读；
--   3) token=admin + shared=true + 有 owner：保持 owner 私有，直到显式
--      新授权。**Breaking change**：旧行为 shared=true 的 admin 标注对同片
--      全部分享链接访客可见；迁移后不再可见（回滚本迁移不得恢复该
--      扩大权限的旧行为——回滚仅 DROP 本表/本列）；
--   4) 无 owner 且无 visitor → visibility_status='unclaimed'，隔离待认领
--      （仅管理清点/单租户态可见）。
--
-- 审计报表：share_store.annotation_visibility_report() 按上述口径列出
-- owned / visitor-bound / shared-true legacy / unclaimed / granted 计数
-- 与样本 annotation_id，供认领与合规核对。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS annotation_grants (
    annotation_id TEXT        NOT NULL,                -- 逻辑引用 rois.annotation_id（无 FK，tombstone 保留）
    grantee_kind  TEXT        NOT NULL,                -- 'user' | 'share_token'
    grantee_id    TEXT        NOT NULL,                -- user_id 或分享 token
    can_edit      BOOLEAN     NOT NULL DEFAULT FALSE,  -- 只读授权缺省；编辑需显式
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT,                                -- 操作者（user_id / 'migration-0056'）
    PRIMARY KEY (annotation_id, grantee_kind, grantee_id),
    CHECK (grantee_kind IN ('user', 'share_token'))
);

CREATE INDEX IF NOT EXISTS idx_annotation_grants_grantee
    ON annotation_grants(grantee_kind, grantee_id);
CREATE INDEX IF NOT EXISTS idx_annotation_grants_annotation
    ON annotation_grants(annotation_id);

ALTER TABLE rois ADD COLUMN IF NOT EXISTS visibility_status TEXT
    NOT NULL DEFAULT 'private';
COMMENT ON COLUMN rois.visibility_status IS
    'private|granted|unclaimed（0056）：查询/审计辅助；unclaimed=无 owner 且'
    '无 visitor，正常列表 API 排除。判定权威在 annotation_access.is_unclaimed';

ALTER TABLE rois ADD COLUMN IF NOT EXISTS client_action_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_rois_owner_client_action
    ON rois(owner_user_id, client_action_id)
    WHERE client_action_id IS NOT NULL;

-- --------------------------------------------------------------------------- --
-- 存量映射 1) 有 owner、无 visitor、token 为真实分享 → 授予该 token 只读
-- --------------------------------------------------------------------------- --
INSERT INTO annotation_grants (annotation_id, grantee_kind, grantee_id,
                               can_edit, created_by)
SELECT r.annotation_id, 'share_token', r.token, FALSE, 'migration-0056'
FROM rois r
JOIN shares s ON s.token = r.token
WHERE r.token IS DISTINCT FROM 'admin'
  AND r.annotation_id IS NOT NULL
  AND r.owner_user_id IS NOT NULL
  AND coalesce(r.data->>'visitor', '') = ''
ON CONFLICT DO NOTHING;

-- --------------------------------------------------------------------------- --
-- 存量映射 2) 访客记录 shared=true 且 token 为真实分享 → 授予该 token 只读
-- --------------------------------------------------------------------------- --
INSERT INTO annotation_grants (annotation_id, grantee_kind, grantee_id,
                               can_edit, created_by)
SELECT r.annotation_id, 'share_token', r.token, FALSE, 'migration-0056'
FROM rois r
JOIN shares s ON s.token = r.token
WHERE r.token IS DISTINCT FROM 'admin'
  AND r.annotation_id IS NOT NULL
  AND coalesce(r.data->>'visitor', '') <> ''
  AND r.shared
ON CONFLICT DO NOTHING;

-- --------------------------------------------------------------------------- --
-- 授权行回填 granted 状态（报表 aid）；此后由授权函数维护
-- --------------------------------------------------------------------------- --
UPDATE rois SET visibility_status = 'granted'
WHERE visibility_status = 'private'
  AND annotation_id IN (SELECT annotation_id FROM annotation_grants);

-- --------------------------------------------------------------------------- --
-- 存量映射 4) 无 owner 且无 visitor → unclaimed（隔离待认领，不删除）
-- --------------------------------------------------------------------------- --
UPDATE rois SET visibility_status = 'unclaimed'
WHERE visibility_status = 'private'
  AND owner_user_id IS NULL
  AND coalesce(data->>'visitor', '') = '';

-- 回滚说明（0009 之前的回滚惯例）：DROP INDEX idx_rois_owner_client_action;
-- ALTER TABLE rois DROP COLUMN IF EXISTS client_action_id,
--   DROP COLUMN IF EXISTS visibility_status; DROP TABLE IF EXISTS
--   annotation_grants。回滚**不得**恢复 shared=true 的旧全局公开语义。
