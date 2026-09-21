-- =========================================================================== --
-- 0061_public_registration.sql：P1 公共注册与管理员通知
-- （docs/agent-plan-20260921-registration-consent-research.md §3.3/§4）。
--
--   - registration_intents：验证邮件绑定的 pending registration intent
--     （§3.3.2/§3.4）。与 email_verify job 一对一（mail_job_id UNIQUE）；
--     flow_mode 固定签发时模式（§4.4：不把旧链接在切模式后静默改语义）；
--     保存必选协议 version/hash + 必选接受动作时间 + 可选研究选择与表单
--     语言，不依赖浏览器 cookie 还原选项；registration_request_id 服务端
--     生成（客户端不能借任意 request_id 命中他人完成记录）；完成关联
--     user_id（completion 幂等：已完成 intent 重放只回成功 + 登录地址）。
--     本阶段只为 public 流程建 intent（flow_mode CHECK 仅 'public'），旧
--     email_verify_invite_activation 链接无 intent 行、走原流程。
--   - public_registration_days(day PK, successful_count 0..5)：每日 5 个
--     自助账号名额桶（§4.2）。日期由服务端数据库 clock_timestamp() 转
--     Asia/Shanghai 在锁内选定；事务在占位后跨零点提交仍归该桶，不需
--     午夜定时重置任务。CHECK 0..5 是最后兜底。
--   - public_registration_completions：只增、幂等、支持核对计数
--     （user_id / registration_request_id 双 UNIQUE）；不建 users 外键——
--     不因用户注销级联删除计数凭据（§4.2）。
--   - registration_mail_jobs：purpose 词表扩 'registration_created'
--     （§4.5 管理员通知），新增可空 business_key 列 + 部分唯一索引
--     （business_key='registration_created:<completion_id>' 唯一——任务
--     只创建一次；投递状态不确定是另一回事，见 worker uncertain 语义）。
--
-- 幂等性：全部 IF NOT EXISTS / DO 块守护（对齐 0037/0040 风格）；purpose
-- CHECK「存在才删 + 无条件重建」对重复应用天然幂等。不修改已发布迁移。
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- registration_intents（§3.3.2/§3.4）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS registration_intents (
    intent_id               TEXT        PRIMARY KEY,
    registration_request_id TEXT        NOT NULL UNIQUE,  -- 服务端生成，绑定本 intent
    mail_job_id             TEXT        NOT NULL UNIQUE   -- 与 email_verify job 一对一
        REFERENCES registration_mail_jobs(job_id),
    email_normalized        TEXT        NOT NULL,
    flow_mode               TEXT        NOT NULL,          -- 签发时固定；本阶段仅 public
    terms_version           TEXT        NOT NULL,          -- 必选《用户协议与数据处理说明》
    terms_sha256            TEXT        NOT NULL,
    terms_accepted_at       TIMESTAMPTZ NOT NULL,          -- 必选接受动作时间（服务端产生）
    research_opt_in         BOOLEAN     NOT NULL DEFAULT FALSE,  -- 可选研究选择（false 有效）
    research_version        TEXT,                            -- 可选《数据共享与软件改进协议》
    research_sha256         TEXT,
    form_locale             TEXT        NOT NULL DEFAULT 'zh-CN',
    completed_user_id       TEXT,                          -- 完成关联 user_id（无外键：注销不留级联）
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at            TIMESTAMPTZ,
    CHECK (flow_mode IN ('public')),
    CHECK (terms_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (research_sha256 IS NULL OR research_sha256 ~ '^[0-9a-f]{64}$')
);
COMMENT ON TABLE registration_intents IS
    '验证邮件绑定的 pending registration intent（0061 P1，§3.3.2/§3.4）：协议 version/hash + 必选接受时间 + 可选研究选择 + 表单语言；与 email_verify job 一对一；flow_mode 固定签发时模式';
CREATE INDEX IF NOT EXISTS registration_intents_email_idx
    ON registration_intents (email_normalized, created_at);

-- --------------------------------------------------------------------------- --
-- public_registration_days（§4.2 每日 5 名额桶；全站、所有入口、所有实例共用）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS public_registration_days (
    day              DATE PRIMARY KEY,   -- Asia/Shanghai 自然日（服务端数据库时间）
    successful_count INT  NOT NULL DEFAULT 0
        CHECK (successful_count BETWEEN 0 AND 5)
);
COMMENT ON TABLE public_registration_days IS
    'public 自助注册每日名额桶（0061 P1，§4.2）：每 Asia/Shanghai 自然日最多 5 个成功新建自助账号；删除/禁用账号不退还名额';

-- --------------------------------------------------------------------------- --
-- public_registration_completions（§4.2 只增、幂等、支持核对计数）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS public_registration_completions (
    completion_id           TEXT        PRIMARY KEY,
    user_id                 TEXT        NOT NULL UNIQUE,  -- 无外键：用户注销不级联删除计数凭据
    registration_request_id TEXT        NOT NULL UNIQUE,
    day                     DATE        NOT NULL,
    channel                 TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (channel IN ('public'))
);
COMMENT ON TABLE public_registration_completions IS
    'public 注册完成凭据（0061 P1，§4.2）：只增、幂等（user_id/registration_request_id 唯一）、支持核对计数；不因用户注销级联删除';
CREATE INDEX IF NOT EXISTS public_registration_completions_day_idx
    ON public_registration_completions (day);

-- --------------------------------------------------------------------------- --
-- ai_spend_total_allowances.source 词表扩 'public_registration'（P1 §4.2：
-- public 建号同事务按现有默认策略建初始一次性总额度，来源独立审计标记，
-- 与 invite/admin_create/cutover 区分）
-- --------------------------------------------------------------------------- --
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'ai_spend_total_allowances'::regclass
          AND conname = 'ai_spend_total_allowances_source_check'
    ) THEN
        ALTER TABLE ai_spend_total_allowances
            DROP CONSTRAINT ai_spend_total_allowances_source_check;
    END IF;
    ALTER TABLE ai_spend_total_allowances ADD CONSTRAINT
        ai_spend_total_allowances_source_check
        CHECK (source IN ('cutover', 'invite', 'admin_create',
                          'public_registration'));
END
$$;

COMMENT ON CONSTRAINT ai_spend_total_allowances_source_check
    ON ai_spend_total_allowances IS
    '建行来源词表（0061 扩）：cutover/invite/admin_create 同 0029；public_registration=P1 自助注册同事务初始额度（现有默认策略）';

-- --------------------------------------------------------------------------- --
-- registration_mail_jobs：purpose 扩 'registration_created' + business_key
-- --------------------------------------------------------------------------- --
ALTER TABLE registration_mail_jobs
    ADD COLUMN IF NOT EXISTS business_key TEXT;
COMMENT ON COLUMN registration_mail_jobs.business_key IS
    '可空业务幂等键（0061 P1，§4.5）：registration_created 通知取 registration_created:<completion_id>，部分唯一索引保证任务只创建一次';

-- business_key 唯一（空值不约束）：通知任务幂等兜底
CREATE UNIQUE INDEX IF NOT EXISTS registration_mail_jobs_business_key_key
    ON registration_mail_jobs (business_key) WHERE business_key IS NOT NULL;

DO $$
BEGIN
    -- 旧 purpose CHECK（0040：email_verify/email_change；0054 测试申请通道
    -- 可能已扩 'test_application'）：存在才删（幂等）
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_purpose_check'
    ) THEN
        ALTER TABLE registration_mail_jobs
            DROP CONSTRAINT registration_mail_jobs_purpose_check;
    END IF;
    -- 无条件重建：词表加 'registration_created'（保留 0040/0054 既有全部用途值）
    ALTER TABLE registration_mail_jobs ADD CONSTRAINT
        registration_mail_jobs_purpose_check
        CHECK (purpose IN ('email_verify', 'email_change', 'test_application',
                           'test_decision', 'registration_created'));
END
$$;

COMMENT ON CONSTRAINT registration_mail_jobs_purpose_check
    ON registration_mail_jobs IS
    '用途词表（0061 扩）：email_verify=注册验证；email_change=登录用户改绑邮箱；test_application/test_decision=测试申请通知与决定（0054）；registration_created=自助注册成功管理员通知（business_key 幂等，注册暂停后仍排水）';
