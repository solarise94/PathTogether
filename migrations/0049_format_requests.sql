-- =========================================================================== --
-- 0049_format_requests.sql：「其他格式请求兼容」PostgreSQL 权威化（W2）。
--
-- 此前 format_request_store 走 JSONL + threading.RLock——两个 Gunicorn
-- worker + 独立 mail worker 并发整文件重写会互相丢行（R2）。本迁移把请求
-- 与邮件作业迁入 PG（权威源）；JSONL 只作为一次性导入格式
-- （scripts/migrate_format_requests.py）。
--
-- 语义（与 registration_mail_jobs 的 P1-1/P1-2 对齐）：
--   - business_status：submitted → reviewing → supported|declined
--     （submitted 可直接 declined；supported/declined 为终态，禁止回退）；
--   - version：CAS 乐观锁（admin_patch_status 409 依据）；
--   - mail_status：queued → sending → sent | failed | uncertain；
--     sent/uncertain 绝不自动重发；lease 租约过期回收见下；
--   - send_started_at：drain 在**释放领取事务之后、sender.send 之前**置位。
--     崩溃语义（F04）：
--       * sending 且 send_started_at IS NULL → 证明未发出，回收为 queued；
--       * sending 且 send_started_at 非空 → 可能已发出，置 uncertain，
--         绝不自动重发；
--   - 样本文件本体仍在磁盘（FORMAT_REQUEST_DIR/samples/），库内只存
--     sample_internal_ref（服务器路径，绝不进用户响应）；admin 下载经
--     专用通道读该路径。
-- 幂等：CREATE TABLE/INDEX IF NOT EXISTS + DO 块守护（对齐 0037/0046 风格）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS format_requests (
    id                  TEXT        PRIMARY KEY,
    owner_user_id       TEXT        NOT NULL,
    format_ext          TEXT        NOT NULL,
    message             TEXT        NOT NULL DEFAULT '',
    contact             TEXT        NOT NULL DEFAULT '',
    sample_name         TEXT,
    sample_size         BIGINT,
    sample_sha256       TEXT,
    sample_internal_ref TEXT,
    sample_missing      BOOLEAN     NOT NULL DEFAULT false,
    business_status     TEXT        NOT NULL DEFAULT 'submitted',
    admin_note          TEXT,
    version             INTEGER     NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE format_requests IS
    '「其他格式请求兼容」权威请求表（0049 W2）；sample_internal_ref 为服务器内'
    '部路径，绝不进用户响应（用户侧经 public_view 脱敏）';
COMMENT ON COLUMN format_requests.business_status IS
    '业务状态机 submitted→reviewing→supported|declined（submitted 可直接 declined；终态不可回退）';
COMMENT ON COLUMN format_requests.version IS
    'CAS 乐观锁版本（admin_patch_status 冲突时 409）';
COMMENT ON COLUMN format_requests.sample_internal_ref IS
    '样本文件服务器路径（admin 下载专用；用户响应绝不包含）';
COMMENT ON COLUMN format_requests.sample_missing IS
    '导入时样本文件缺失标记（W2 迁移工具置位；请求本身仍导入）';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'format_requests'::regclass
          AND conname = 'format_requests_business_status_check'
    ) THEN
        ALTER TABLE format_requests ADD CONSTRAINT
            format_requests_business_status_check
            CHECK (business_status IN
                   ('submitted', 'reviewing', 'supported', 'declined'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_format_requests_owner_created
    ON format_requests (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_format_requests_status_created
    ON format_requests (business_status, created_at);

-- --------------------------------------------------------------------------- --
-- format_request_mail_jobs：管理员通知邮件队列（一请求一作业，
-- UNIQUE(request_id)）。领取走 FOR UPDATE SKIP LOCKED + 租约
-- （lease_owner/lease_token/lease_expires_at）；发送绝不在 DB 事务内
-- （registration_mail_worker 的教训：不跨 SMTP 持锁）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS format_request_mail_jobs (
    job_id           TEXT        PRIMARY KEY,
    request_id       TEXT        NOT NULL
        REFERENCES format_requests(id) ON DELETE CASCADE,
    mail_status      TEXT        NOT NULL DEFAULT 'queued',
    attempts         INT         NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_token      TEXT,
    lease_expires_at TIMESTAMPTZ,
    send_started_at  TIMESTAMPTZ,
    last_error       TEXT,
    scheduled_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at          TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_id)
);

COMMENT ON TABLE format_request_mail_jobs IS
    '格式兼容请求管理员邮件队列（0049 W2）；sent/uncertain 绝不自动重发，'
    '租约过期的 sending 按 send_started_at 判 queued 回收或 uncertain 封存';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'format_request_mail_jobs'::regclass
          AND conname = 'format_request_mail_jobs_mail_status_check'
    ) THEN
        ALTER TABLE format_request_mail_jobs ADD CONSTRAINT
            format_request_mail_jobs_mail_status_check
            CHECK (mail_status IN
                   ('queued', 'sending', 'sent', 'failed', 'uncertain'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_format_request_mail_jobs_claim
    ON format_request_mail_jobs (mail_status, scheduled_at);
