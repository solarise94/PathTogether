-- =========================================================================== --
-- 0038_mail_uncertain_state.sql：P1-1 发送不确定态 —— registration_mail_jobs
-- status 词表新增 'uncertain'。
--
-- 背景（review P1-1）：SMTP/Agent Mail 在「DATA 结束符已发出、等待远端最终
-- 响应」期间发生本地超时/断连时，远端**可能已接受**——用户可能已收到邮件。
-- 此类作业绝不按 failed 自动重发（防重复邮件/重复建号），置 uncertain 只留
-- 人工核对；验证端（registration_store.check_verify_token /
-- verify_email_create_user）在有效期内接受 uncertain 作业的 token，防止
-- 「用户收到了邮件、链接却被判 invalid」。
--
-- 幂等性：DO 块守护（对齐 0037 的 IF NOT EXISTS 风格）：先 DROP 旧 status
-- CHECK（0037 建立的无 uncertain 词表）再重建；迁移序恒为 0037→0038，
-- 新库/存量库均收敛到同一词表。
-- =========================================================================== --

DO $$
BEGIN
    -- 旧 status CHECK（0037：queued/sent/failed/superseded/consumed）：存在则先删
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_status_check'
    ) THEN
        ALTER TABLE registration_mail_jobs
            DROP CONSTRAINT registration_mail_jobs_status_check;
    END IF;
    -- 重建为含 uncertain 的新词表
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_status_check'
    ) THEN
        ALTER TABLE registration_mail_jobs ADD CONSTRAINT
            registration_mail_jobs_status_check
            CHECK (status IN ('queued', 'sent', 'failed', 'uncertain',
                              'superseded', 'consumed'));
    END IF;
END
$$;

COMMENT ON CONSTRAINT registration_mail_jobs_status_check
    ON registration_mail_jobs IS
    'status 词表（0038 起）：queued→sent；failed=确定未发出（有界指数退避重试，attempts 上限后停发）；uncertain=发送结果不确定（远端可能已接受，不自动重发，留人工核对）；superseded/consumed 同 0037';

COMMENT ON TABLE registration_mail_jobs IS
    '注册验证邮件队列（0037 I 线；0038 增 uncertain 发送不确定态；禁止命名 outbox——billing 已占用）。token/正文含密信息只存 hash 与加密载荷';
