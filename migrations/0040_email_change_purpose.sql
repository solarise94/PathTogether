-- =========================================================================== --
-- 0040_email_change_purpose.sql：邮箱改绑闭环（review P1-3 收口，登录身份=邮箱）
--
-- registration_mail_jobs.purpose 词表扩 'email_change'：
--   - 已登录用户 POST /api/account/email/change/start 请求改绑邮箱；
--   - 复用既有 enqueue/send 通道（registration_mail_jobs →
--     registration_mail_worker.drain_once），冻结正文仍加密落库、token 只存
--     域分离 HMAC hash；
--   - payload（加密 dict）额外携带 ``user_id`` 绑定：确认时必须与当前登录
--     user 一致，防止 token 被用于其他账号；user_id **不落明文列**（只在
--     加密载荷内）。
--
-- 幂等性：DROP CONSTRAINT（存在才删）+ ADD CONSTRAINT 无条件重建。对齐
-- 0037 的 DO 块守护风格——「先删后建」对重复应用天然幂等（首次无约束可删
-- 时跳过 DROP）。重建瞬时完成（约束为行内 CHECK，不改表数据）。
-- =========================================================================== --

DO $$
BEGIN
    -- 0037 已建同名约束（purpose IN ('email_verify')）：存在才删（幂等）
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_purpose_check'
    ) THEN
        ALTER TABLE registration_mail_jobs
            DROP CONSTRAINT registration_mail_jobs_purpose_check;
    END IF;
    -- 无条件重建：词表加 'email_change'（与 identity_store.MAIL_PURPOSE_
    -- EMAIL_CHANGE 同词表；新值只由邮箱改绑闭环写入）
    ALTER TABLE registration_mail_jobs ADD CONSTRAINT
        registration_mail_jobs_purpose_check
        CHECK (purpose IN ('email_verify', 'email_change'));
END
$$;

COMMENT ON CONSTRAINT registration_mail_jobs_purpose_check
    ON registration_mail_jobs IS
    '用途词表（0040 扩）：email_verify=注册验证；email_change=登录用户改绑邮箱（payload.user_id 绑定）';
