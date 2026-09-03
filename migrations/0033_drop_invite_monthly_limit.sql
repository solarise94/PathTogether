-- =========================================================================== --
-- 0033_drop_invite_monthly_limit.sql：物理删除 registration_invites 的旧
-- 月额度模板列 monthly_limit_nano_cny（R3 Wave2-Compat）。
--
-- 语义（代码侧已同步收口）：
--
--   1. 0032 已把 monthly_limit_nano_cny 面值回填进 total_limit_nano_cny
--      （total 为 NULL 才回填）；R3 Wave1-Money 代码停读 monthly 列，
--      R3 Wave2-Compat 删除 create_invite 的 monthly 形参与 SELECT 列——
--      本迁移把数据面收敛到与代码一致（DROP COLUMN）；
--   2. 历史数据不改写：audit/ledger 行（0025 时代的 registration.invite_
--      create detail 若含 monthly 键）只读保留，不受影响；
--   3. 路由层对 body 带 monthly_limit_nano_cny 一律 400 retired_spend_field
--      （绝不静默忽略），不再存在「兼容落总额度」的输入路径。
--
-- 幂等/可重跑：DROP COLUMN IF EXISTS 幂等；audit 固定 event_id +
-- ON CONFLICT DO NOTHING。单迁移单事务。
-- 回滚：恢复 DB 备份 + 旧镜像（无在线逆迁移；列删除后旧代码 SELECT 该列
-- 即报错，回滚必须连同镜像一起回退）。
-- =========================================================================== --

ALTER TABLE registration_invites
    DROP COLUMN IF EXISTS monthly_limit_nano_cny;

-- --------------------------------------------------------------------------- --
-- 迁移标志 audit（不含敏感信息；固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- --
DO $$
BEGIN
    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0033_drop_invite_monthly_limit', now(), 'system',
         'registration.invite_monthly_limit_dropped', 'registration_invites',
         jsonb_build_object(
             'dropped_column', 'monthly_limit_nano_cny',
             'authoritative_column', 'total_limit_nano_cny',
             'note', 'R3 Wave2-Compat: invite template amount is single-'
                     'track (total_limit_nano_cny only); 0032 already '
                     'backfilled legacy monthly face values; runtime code '
                     'stopped reading the column in R3 Wave1; rollback = '
                     'restore DB backup + previous image'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
