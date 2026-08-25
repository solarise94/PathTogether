-- =========================================================================== --
-- 0015_account_auth_version.sql：账户凭据版本 + 单 enabled owner 不变量
-- （账户系统批次 A，docs/account-system-simplification-fix-plan.md §4.1）
--
-- 新增：
--   - users.auth_version：Web session 凭据版本（正整数，缺省 1）。修改密码、
--     disable/enable、未来 role 变化必须在同一事务内 +1，使旧 session 的
--     auth_version 比对失效（§6.2）。内部安全字段，不进公共 API 输出；
--   - users_auth_version_positive CHECK 约束：版本必须 >= 1（先 NOT VALID
--     后 VALIDATE，对齐方案 §4.1 的两段式写法）；
--   - users_single_enabled_owner_key 部分唯一索引：role='owner' 且未禁用的行
--     最多一条，把「单一 enabled owner」落到数据库层兜底（应用层
--     create_bootstrap_owner 的 advisory lock + 启动检查为第一道防线）。
--
-- 部署前必须先完成 §4.3 数据审计：若现存 enabled owner 多于一个，应先人工
-- 明确主 owner 并处理其余账号，不得靠本索引创建失败来替代决策。
--
-- 幂等性：ADD COLUMN IF NOT EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS 自保；
-- ADD CONSTRAINT 无 IF NOT EXISTS 语法，用 pg_constraint 目录查询的 DO 块守护
-- （重跑 no-op）；VALIDATE CONSTRAINT 对已验证约束重跑安全（runner 的
-- schema_migrations 记录之外的双重保险，对齐 0001-0014 风格）。
-- =========================================================================== --

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS auth_version BIGINT NOT NULL DEFAULT 1;
COMMENT ON COLUMN users.auth_version IS
    'Web session 凭据版本：密码/禁用/启用/角色变化时同事务 +1，旧 session 比对失效（0015 起）';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'users'::regclass
          AND conname = 'users_auth_version_positive'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_auth_version_positive
            CHECK (auth_version >= 1) NOT VALID;
    END IF;
END
$$;

ALTER TABLE users VALIDATE CONSTRAINT users_auth_version_positive;

CREATE UNIQUE INDEX IF NOT EXISTS users_single_enabled_owner_key
    ON users (role)
    WHERE role = 'owner' AND NOT disabled;
