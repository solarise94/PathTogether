-- =========================================================================== --
-- 0079_registration_antibot_redelivery.sql：注册防刷 + 有效链接保留 + 入口
-- 一致性（docs/registration-antibot-and-author-help-design-20261008.md
-- §3/§4/§6/§8）。
--
--   - registration_mail_jobs：+ entry_origin / form_locale（可空，历史行为
--     NULL——重发时按加密正文里的冻结链接惰性恢复白名单 origin，恢复不了
--     不跨入口重发、改发新 token）；
--   - registration_intents：+ source_origin（初次提交入口，归因保留；重发
--     换入口时不得覆盖）；
--   - registration_mail_redeliveries（新表）：复用原 token 的重发投递。
--     registration_mail_jobs.token_hash UNIQUE，不能复制原作业复用 hash；
--     重发行引用原 job_id，独立状态/尝试/退避/入口/语言/加密正文。验证
--     查询仍只查原作业——重发行不是验证 token；
--   - registration_submissions（新表）：/register 表单 submission_id 幂等
--     （断网重试不得双入队）+ 匿名 registration receipt（session 存随机
--     receipt_id，库内映射到请求/作业；receipt 不暴露 token 与账号身份）。
--
-- 幂等性：全部 IF NOT EXISTS / ADD COLUMN IF NOT EXISTS（对齐 0061/0077
-- 风格）。不修改已发布迁移。
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- registration_mail_jobs：入口与语言冻结（§6：入队时保存，worker 绝不读 env 选域名）
-- --------------------------------------------------------------------------- --
ALTER TABLE registration_mail_jobs
    ADD COLUMN IF NOT EXISTS entry_origin TEXT;
ALTER TABLE registration_mail_jobs
    ADD COLUMN IF NOT EXISTS form_locale TEXT;

COMMENT ON COLUMN registration_mail_jobs.entry_origin IS
    '本次投递的注册入口 origin（0079 §6：入队时冻结；NULL=历史行，重发时按加密正文链接惰性恢复白名单 origin，恢复不了不跨入口重发）';
COMMENT ON COLUMN registration_mail_jobs.form_locale IS
    '本次投递的表单语言 zh|en（0079 §6：入队时冻结；NULL=历史行按 zh）';

-- --------------------------------------------------------------------------- --
-- registration_intents：初次来源归因（§6：重发换入口不得覆盖归因）
-- --------------------------------------------------------------------------- --
ALTER TABLE registration_intents
    ADD COLUMN IF NOT EXISTS source_origin TEXT;

COMMENT ON COLUMN registration_intents.source_origin IS
    '初次提交入口 origin（0079 §6 归因）：重发投递的 entry_origin 单独记录在投递行，不回写本列';

-- --------------------------------------------------------------------------- --
-- registration_mail_redeliveries：复用原 token 的重发投递（§4）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS registration_mail_redeliveries (
    redelivery_id  TEXT        PRIMARY KEY,
    job_id         TEXT        NOT NULL
        REFERENCES registration_mail_jobs(job_id),
    email_normalized TEXT      NOT NULL,
    payload_enc    TEXT        NOT NULL,   -- 复用原加密正文，或按本次入口/语言重构造（token 只在密文内）
    status         TEXT        NOT NULL DEFAULT 'queued',
    attempts       INT         NOT NULL DEFAULT 0,
    last_error     TEXT,
    entry_origin   TEXT,                   -- 本次投递入口（§6：跨入口重发用本次入口）
    form_locale    TEXT,                   -- 本次投递语言 zh|en
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    scheduled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at        TIMESTAMPTZ,
    CHECK (status IN ('queued', 'sent', 'failed', 'uncertain', 'cancelled')),
    CHECK (form_locale IS NULL OR form_locale IN ('zh', 'en'))
);
COMMENT ON TABLE registration_mail_redeliveries IS
    '复用原 token 的验证邮件重发投递（0079 §4）：引用原 job；同入口同语言复用原加密正文，跨入口/换语言按受保护原载荷重构造；验证只查原作业；发送前复核原作业未消费/未作废/未过期且 intent 未完成';

CREATE INDEX IF NOT EXISTS registration_mail_redeliveries_email_time_idx
    ON registration_mail_redeliveries (email_normalized, created_at);
CREATE INDEX IF NOT EXISTS registration_mail_redeliveries_queue_idx
    ON registration_mail_redeliveries (status, scheduled_at);

-- --------------------------------------------------------------------------- --
-- registration_submissions：submission_id 幂等 + 匿名 registration receipt（§8）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS registration_submissions (
    submission_id    TEXT        PRIMARY KEY,  -- 服务端签发、随表单提交（幂等键）
    receipt_id       TEXT        UNIQUE,       -- 匿名回执：session 存此随机 id，库内映射请求/作业
    email_normalized TEXT        NOT NULL,
    action           TEXT        NOT NULL,     -- start（/register 提交）| resend（/register/resend）
    state_kind       TEXT        NOT NULL,     -- 提交后的 register_state.kind（重放回放同状态）
    job_id           TEXT,
    redelivery_id    TEXT,
    resend_available_at TIMESTAMPTZ,           -- cooldown/newest 状态的可重发时间
    resume_at        TIMESTAMPTZ,              -- limit 状态的额度恢复时间
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (action IN ('start', 'resend'))
);
COMMENT ON TABLE registration_submissions IS
    '注册提交幂等与匿名回执（0079 §8）：submission_id 重放只回放已记录状态，不双入队；receipt_id 不暴露 token/账号身份，仅供同会话重发绑定原请求';

CREATE INDEX IF NOT EXISTS registration_submissions_email_time_idx
    ON registration_submissions (email_normalized, created_at);
