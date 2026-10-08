-- =========================================================================== --
-- 0080_admin_viewer.sql：后台/Viewer 改版（docs/admin-viewer-simplified-
-- 20261008.md §1）。三组加列，全部幂等（IF NOT EXISTS / DO 块按约束名）：
--
--   1. users.account_kind（'real'|'dogfood'，默认 'real'）——后台用户分类
--      （Dogfood 名单由站长逐个标记，不批量回填）；users.last_login_at
--      （旧用户保持 NULL，不回填——只有登录成功路径写入）；
--   2. slide_view_grants.expires_at——管理员临时查看时限（§3.1/§3.2）。
--      存量永久授权把 expires_at 设为迁移时刻：旧授权立即结束（§0 简化：
--      冻结为历史，后台显示「已结束」，可重新开启）；列 NOT NULL，此后
--      任何写入方必须显式给到期时间；
--   3. projects.parent_project_id——文件夹层级（§5.3）。FK ON DELETE SET
--      NULL：删除父文件夹时子文件夹回根，切片引用随项目删除（现状），
--      切片本身不删。
-- =========================================================================== --

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS account_kind TEXT NOT NULL DEFAULT 'real';
COMMENT ON COLUMN users.account_kind IS
    '账号分类：real（正式用户，默认）| dogfood（内部试用，2026-10-08 §2）';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'users'::regclass
          AND conname = 'users_account_kind_check'
    ) THEN
        ALTER TABLE users ADD CONSTRAINT users_account_kind_check
            CHECK (account_kind IN ('real', 'dogfood'));
    END IF;
END
$$;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
COMMENT ON COLUMN users.last_login_at IS
    '最近一次登录成功时间（仅 login() 正常成功路径更新；旧用户保持 NULL，不回填）';

ALTER TABLE slide_view_grants
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
COMMENT ON COLUMN slide_view_grants.expires_at IS
    '授权到期时间（2026-10-08 §3 临时查看）；读门禁只认 expires_at > now()。存量行=迁移时刻（旧永久授权立即结束）';

-- 旧永久授权立即结束（幂等：重跑时 NULL 行只可能是迁移中途失败残留）
UPDATE slide_view_grants SET expires_at = now() WHERE expires_at IS NULL;

ALTER TABLE slide_view_grants
    ALTER COLUMN expires_at SET NOT NULL;

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS parent_project_id TEXT
        REFERENCES projects(project_id) ON DELETE SET NULL;
COMMENT ON COLUMN projects.parent_project_id IS
    '父文件夹（文件夹=项目，§5.3）；NULL=根。删除父级时子级回根（SET NULL）';

CREATE INDEX IF NOT EXISTS projects_parent_idx ON projects(parent_project_id);
