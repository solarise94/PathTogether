-- =========================================================================== --
-- 0095_password_reset_purpose.sql：自助找回密码（忘记密码 → 邮件重置链接）
--
-- registration_mail_jobs.purpose 词表扩 'password_reset'：
--   - 匿名/公开入口 POST /password-reset 对符合条件账号签发一次性重置 token；
--   - 复用既有 enqueue/send 通道（registration_mail_jobs →
--     registration_mail_worker.drain_once），冻结正文仍加密落库、token 只存
--     域分离 HMAC hash；
--   - payload（加密 dict）额外携带 ``user_id`` 与请求时的 ``auth_version``
--     绑定：消费时必须与当前用户/版本一致；任一其它改密路径推进版本后
--     token 自动失效。user_id/auth_version **不落明文列**（只在加密载荷内）。
--
-- 幂等性：DROP CONSTRAINT（存在才删）+ ADD CONSTRAINT 无条件重建。对齐
-- 0040/0061 的 DO 块守护风格——「先删后建」对重复应用天然幂等（首次无
-- 约束可删时跳过 DROP）。重建瞬时完成（约束为行内 CHECK，不改表数据）。
-- =========================================================================== --

DO $$
BEGIN
    -- 0061 已建同名约束（当前词表含 email_verify/email_change/
    -- test_application/test_decision/registration_created）：存在才删（幂等）
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_purpose_check'
    ) THEN
        ALTER TABLE registration_mail_jobs
            DROP CONSTRAINT registration_mail_jobs_purpose_check;
    END IF;
    -- 无条件重建：词表加 'password_reset'（保留 0040/0054/0061/0081 既有
    -- 全部用途值；新值只由 password_reset_store.MAIL_PURPOSE_PASSWORD_RESET
    -- 写入）
    ALTER TABLE registration_mail_jobs ADD CONSTRAINT
        registration_mail_jobs_purpose_check
        CHECK (purpose IN ('email_verify', 'email_change',
                           'test_application', 'test_decision',
                           'registration_created', 'user_feedback',
                           'password_reset'));
END
$$;

COMMENT ON CONSTRAINT registration_mail_jobs_purpose_check
    ON registration_mail_jobs IS
    '用途词表（0095 扩）：email_verify=注册验证；email_change=登录用户改绑邮箱；test_application/test_decision=测试申请通知与决定（0054）；registration_created=自助注册成功管理员通知（0061）；user_feedback=用户反馈（0081）；password_reset=自助找回密码（payload 绑定 user_id + auth_version）';
