-- =========================================================================== --
-- 0032_user_total_allowance_single_track.sql：用户金额授权面切「单轨总额度」
-- （R3 Wave1-Money）。
--
-- 语义（代码侧已同步拆除 window/total_allowance 双轨运行时分支，本迁移
-- 把数据面收敛到与代码一致）：
--
--   1. 删除 platform_settings.user_spend_target 设定行（0029 seed 的
--      "window" / cutover 写入的 "total_allowance"）：代码不再读取该键
--      （spend_store / billing_store / user_store_pg / registration_store
--      的运行时分支全部拆除）。历史 audit 行（spend.enforcement_mode_update
--      等引用它的 updated_by 链）只读保留，不受影响；
--   2. 邀请模板金额收敛到单列：registration_invites.total_limit_nano_cny
--      为 NULL 而 monthly_limit_nano_cny（旧列，Batch B 起新邀请不再写入）
--      有面值的行，把旧面值回填进 total 列。回滚 = 恢复 DB 备份（monthly
--      列物理删除在 Wave 2，本迁移不动列）；
--   3. 删除 legacy 注册开关旧行 platform_settings.registration_open：
--      运行时权威是 registration_mode（app.py 只调 get_registration_mode），
--      该函数在 mode 键缺失时读 legacy 布尔——**缺行 = legacy_true=False =
--      降级 closed（fail-closed 安全默认）**；get_registration_open 已无
--      生产调用方（json/dual fallback 与测试专用）。删除后不可能放大注册
--      面：mode 键有行时 legacy 根本不读，mode 键缺行时缺 legacy 也返回
--      closed；
--   4. 把当时有效的 user_default 策略面值物化为 ai_spend_total_defaults
--      权威行（缺行才写，ON CONFLICT DO NOTHING 不覆盖 owner 决策）：
--      代码侧 _resolve_total_default_tx 已改为**只查** defaults 表（原
--      「缺行回退 user_default 策略面值」分支删除），迁移负责把旧世界
--      的有效值固化，保证新 user 开号模板不断档。
--
-- 幂等/可重跑：DELETE 幂等；UPDATE 带 WHERE（total 列仍 NULL 才回填，
-- 重跑 0 行）；物化 INSERT ... ON CONFLICT DO NOTHING；audit 固定
-- event_id + ON CONFLICT DO NOTHING。单迁移单事务。
-- 回滚：恢复 DB 备份 + 旧镜像（无在线逆迁移；代码单轨后无 flag 可切）。
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- 1. 删除 user_spend_target 设定行（运行时读取已全部拆除）
-- --------------------------------------------------------------------------- --
DELETE FROM platform_settings WHERE key = 'user_spend_target';

-- --------------------------------------------------------------------------- --
-- 2. 邀请模板金额回填：monthly 旧面值 → total 列（total 为 NULL 才回填）
-- --------------------------------------------------------------------------- --
UPDATE registration_invites
   SET total_limit_nano_cny = monthly_limit_nano_cny
 WHERE total_limit_nano_cny IS NULL
   AND monthly_limit_nano_cny IS NOT NULL;

-- --------------------------------------------------------------------------- --
-- 3. 删除 legacy 注册开关旧行（缺行 = get_registration_mode 降级 closed）
-- --------------------------------------------------------------------------- --
DELETE FROM platform_settings WHERE key = 'registration_open';

-- --------------------------------------------------------------------------- --
-- 4. user_default 策略当前有效面值 → ai_spend_total_defaults 权威行
--    （缺行才写；取 effective_from 最新的一条有效 user_default 策略）
-- --------------------------------------------------------------------------- --
INSERT INTO ai_spend_total_defaults
    (singleton, default_limit_nano_cny, version, updated_by)
SELECT 'global', p.limit_nano_cny, 1, 'migration-0032'
  FROM ai_spend_policies p
 WHERE p.scope_type = 'user_default'
   AND p.enabled
   AND p.effective_from <= now()
   AND (p.effective_to IS NULL OR p.effective_to > now())
 ORDER BY p.effective_from DESC
 LIMIT 1
ON CONFLICT (singleton) DO NOTHING;

-- --------------------------------------------------------------------------- --
-- 迁移标志 audit（不含敏感信息；固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- --
DO $$
DECLARE
    v_invites_with_total int;
    v_default_seeded     boolean;
BEGIN
    -- 报告口径：当前带 total 面值的邀请行数（回填与原生面值形态相同，
    -- 不做区分；纯观测，不影响迁移结果）
    SELECT count(*) INTO v_invites_with_total
      FROM registration_invites
     WHERE total_limit_nano_cny IS NOT NULL;

    SELECT EXISTS (
        SELECT 1 FROM ai_spend_total_defaults
         WHERE singleton = 'global' AND updated_by = 'migration-0032')
      INTO v_default_seeded;

    INSERT INTO audit_events
        (event_id, ts, actor_role, action, target_type, detail)
    VALUES
        ('aud_migration_0032_user_total_single_track', now(), 'system',
         'spend.total_allowance_single_track', 'platform_settings',
         jsonb_build_object(
             'deleted_settings', to_jsonb(ARRAY[
                 'user_spend_target', 'registration_open']),
             'invites_total_limit_column', 'total_limit_nano_cny',
             'invites_with_total_limit', v_invites_with_total,
             'default_materialized_from_policy', v_default_seeded,
             'note', 'R3 Wave1-Money: user money authorization is '
                     'single-track (ai_spend_total_allowances only); '
                     'user_spend_target runtime branches removed from code; '
                     'rollback = restore DB backup + previous image'))
    ON CONFLICT (event_id) DO NOTHING;
END $$;
