-- =========================================================================== --
-- 0016_login_id_rename.sql：登录标识物理改名（账户系统批次 C contract，
-- docs/account-system-simplification-fix-plan.md §4.2/§10 批次 C）
--
-- 批次 B 的兼容窗口（API 双键 login_id+email）已收口，本批把物理 schema 与
-- 规范名对齐：
--   - users.email → users.login_id：登录账号（可为用户名或邮箱形式），
--     trim + lower 规范化，大小写不敏感唯一；
--   - users_email_ci_key → users_login_id_ci_key：仅索引名改名。PG 在
--     RENAME COLUMN 时会自动把函数索引表达式 lower(email) 改写为
--     lower(login_id)（索引定义随列名联动，无需重建；由
--     tests/test_pg_infra.py 断言索引表达式与大小写不敏感唯一仍生效）；
--   - registration_invites.email_normalized → login_id_normalized：邀请绑定
--     列，语义为「允许兑换的登录账号」（批次 B 起已按 login_id 语义使用）。
--
-- 0001-0015 均无 COMMENT ON COLUMN 引用 users.email /
-- registration_invites.email_normalized（已核对），无随列 COMMENT 需要改写；
-- 这里按终态补两条列注释。
--
-- 破坏性说明（docs §13.2）：本迁移执行后旧镜像（读 users.email 的批次 B 及
-- 更早代码）无法在此 schema 上运行，回滚依赖 PG 备份，不提供 down 脚本。
--
-- 幂等性：RENAME 无 IF EXISTS 语法，用 information_schema.columns /
-- pg_indexes 目录查询的 DO 块守护（重跑 no-op，对齐 0015 风格）。
-- =========================================================================== --

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'users' AND column_name = 'email'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'users' AND column_name = 'login_id'
    ) THEN
        ALTER TABLE users RENAME COLUMN email TO login_id;
    END IF;
END
$$;
COMMENT ON COLUMN users.login_id IS
    '登录账号 login_id（可为用户名或邮箱形式）：写入侧 trim + lower 规范化，lower(login_id) 函数唯一索引保证大小写不敏感唯一（0016 由 email 改名）';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'users' AND indexname = 'users_email_ci_key'
    ) THEN
        ALTER INDEX users_email_ci_key RENAME TO users_login_id_ci_key;
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'registration_invites'
          AND column_name = 'email_normalized'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'registration_invites'
          AND column_name = 'login_id_normalized'
    ) THEN
        ALTER TABLE registration_invites
            RENAME COLUMN email_normalized TO login_id_normalized;
    END IF;
END
$$;
COMMENT ON COLUMN registration_invites.login_id_normalized IS
    '邀请绑定的登录账号（小写规范化；NULL = 不绑定，高风险）；兑换时常数时间比较（0016 由 email_normalized 改名）';
