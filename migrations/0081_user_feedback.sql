-- =========================================================================== --
-- 0081_user_feedback.sql：用户反馈（docs/admin-viewer-round4-20261009.md §3）。
--
--   1. user_feedback 表：登录用户主动提交的问题描述 + 客户端环形缓冲记录
--      （client JSONB）+ 服务端附带上下文（server JSONB）。记录先落库、
--      邮件可丢（mail_job_id 为空 = 未配置管理员邮箱/未入队），便于邮件
--      丢失时查询；本轮不做后台反馈页面。
--   2. 频率限制索引 (user_id, created_at)：每小时 5 次 / 每天 20 次的滚动
--      窗口计数查询走该索引（事务内先锁 users 行再计数，防并发超发）。
--   3. registration_mail_jobs purpose 词表扩 'user_feedback'：复用现有注册
--      邮件队列 / worker / 发送器（收件人 = 现有管理员通知邮箱配置）。
--      test_application / test_decision 历史用途保留在词表内（表数据是
--      历史，不随通道退役改词表）。
--
-- 全部幂等（CREATE IF NOT EXISTS / DO 块按约束名先删后建）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS user_feedback (
    feedback_id TEXT        PRIMARY KEY,             -- 形如 ufb_<urlsafe>
    user_id     TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT        NOT NULL,                -- 问题描述（10..4000 字）
    client      JSONB       NOT NULL,                -- 客户端环形缓冲记录（主动附带）
    server      JSONB       NOT NULL,                -- 服务端附带上下文（版本/用户/审计/任务）
    mail_job_id TEXT        REFERENCES registration_mail_jobs(job_id)
);
COMMENT ON TABLE user_feedback IS
    '用户反馈（2026-10-09 §3）：先落库后投递；mail_job_id 为空 = 未入队（未配置管理员邮箱）。本轮无后台页面';
COMMENT ON COLUMN user_feedback.client IS
    '客户端环形缓冲记录（用户主动发送时附带；不含输入内容/密码/正文/Cookie/查询串/图像数据）';
COMMENT ON COLUMN user_feedback.server IS
    '服务端附带上下文：应用版本、用户 id/邮箱/角色/AI 权限/剩余额度、24h 审计事件（≤100）、最近上传/摄取/转换任务（≤20）';

CREATE INDEX IF NOT EXISTS idx_user_feedback_user_created
    ON user_feedback (user_id, created_at);

-- --------------------------------------------------------------------------- --
-- registration_mail_jobs purpose 词表扩 'user_feedback'
-- （保留 0040/0054/0061 既有全部用途值；无条件重建，重跑幂等）
-- --------------------------------------------------------------------------- --
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_purpose_check'
    ) THEN
        ALTER TABLE registration_mail_jobs
            DROP CONSTRAINT registration_mail_jobs_purpose_check;
    END IF;
    ALTER TABLE registration_mail_jobs ADD CONSTRAINT
        registration_mail_jobs_purpose_check
        CHECK (purpose IN ('email_verify', 'email_change', 'test_application',
                           'test_decision', 'registration_created',
                           'user_feedback'));
END
$$;

COMMENT ON CONSTRAINT registration_mail_jobs_purpose_check
    ON registration_mail_jobs IS
    '用途词表（0081 扩）：email_verify=注册验证；email_change=登录用户改绑邮箱；test_application/test_decision=测试申请通知与决定（0054，通道已退役、词表保留历史）；registration_created=自助注册成功管理员通知（0061）；user_feedback=用户反馈通知（0081，收件人=管理员通知邮箱）';
