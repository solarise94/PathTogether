-- =========================================================================== --
-- 0037_identity_activation_email.sql：I+J 身份线 —— 激活状态机 + 邮箱唯一用户名
-- （设计文档第 8 节 + review J / P2-4；email_verify_invite_activation 模式）
--
-- 新增（users，全部可空/带默认，不改既有行语义）：
--   - activation_state：账号激活状态机
--       email_pending        邮箱已请求验证、邮箱确认尚未完成（当前实现中
--                            该阶段不落 users 行，状态物化为未消费的
--                            registration_mail_jobs 行；枚举保留给未来演进）
--       pending_activation   邮箱已验证、用户行已原子创建、待邀请码激活
--       active               邀请码激活成功（或存量 backfill）
--     disabled 与本状态机正交（禁用语义完全沿用 users.disabled）；
--   - activation_source：active/pending 的来源（legacy | invite_activation |
--     invite | admin），backfill 统一 'legacy'；
--   - activation_updated_at：状态迁移时间（审计辅助，非权威审计源）；
--   - email / email_normalized / email_verified_at：**只有可信已验证邮箱才填**
--     （email_verified_at 非 NULL）。存量 backfill **不**把带 @ 的 login_id
--     自动当作已验证邮箱（review J 红线：不静默合并账号、不伪造验证状态），
--     三列保持 NULL；
--   - users_email_identity_key 部分唯一索引：lower(email_normalized) 在
--     pending_activation + active 两态内唯一（email_pending 无行不涉及）。
--     未验证（email_normalized IS NULL）不占用邮箱身份。
--
-- 新表 registration_mail_jobs：注册验证邮件队列（表/模块名红线：**禁止**叫
-- outbox——billing 语义已占用）。明文 token 与含 token 的完整正文**绝不落库**：
-- 库内只存 token_hash（带域分离 HMAC-SHA-256）与加密后的冻结正文载荷
-- （registration_mail_worker.encrypt_payload，Fernet）。
--
-- 幂等性：ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / DO 块守护
-- （对齐 0015/0016 风格）；backfill UPDATE 幂等（只填 NULL 行）。
-- =========================================================================== --

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS activation_state TEXT NOT NULL DEFAULT 'active';
COMMENT ON COLUMN users.activation_state IS
    '激活状态机 email_pending→pending_activation→active（0037 起）；disabled 正交。存量 backfill=active';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS activation_source TEXT;
COMMENT ON COLUMN users.activation_source IS
    '激活来源：legacy（0037 backfill）/ invite_activation（邮箱+邀请码激活）/ invite / admin';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS activation_updated_at TIMESTAMPTZ;
COMMENT ON COLUMN users.activation_updated_at IS
    '激活状态最近迁移时间（审计辅助；权威审计在 audit_events）';

ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;
COMMENT ON COLUMN users.email IS
    '用户可信邮箱（展示主列 J 的来源；仅 email_verified_at 非 NULL 才可信）。存量 backfill 保持 NULL';

ALTER TABLE users ADD COLUMN IF NOT EXISTS email_normalized TEXT;
COMMENT ON COLUMN users.email_normalized IS
    '规范化邮箱（trim+lower，J 的唯一用户名口径）；未验证为 NULL，不占用唯一索引';

ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;
COMMENT ON COLUMN users.email_verified_at IS
    '邮箱一次性验证链接消费时间；NULL = 无可信已验证邮箱（绝不因 login_id 带 @ 回填）';

-- 激活状态枚举约束（含 email_pending 保留态；幂等 DO 块守护）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'users'::regclass
          AND conname = 'users_activation_state_check'
    ) THEN
        ALTER TABLE users ADD CONSTRAINT users_activation_state_check
            CHECK (activation_state IN
                   ('email_pending', 'pending_activation', 'active'));
    END IF;
END
$$;

-- 存量 backfill（J）：activation_state 列默认即 'active'，这里只补来源；
-- disabled 行同样标 legacy（禁用语义不变，状态机照常回填，启用后即为 active）
UPDATE users SET activation_source = 'legacy'
    WHERE activation_source IS NULL;

-- J 邮箱身份唯一（pending_activation + active 两态内大小写不敏感唯一；
-- 部分索引：未验证邮箱不占用）。同码并发只有一人成功由激活事务的
-- FOR UPDATE + CAS 保证，索引是数据库层兜底。
CREATE UNIQUE INDEX IF NOT EXISTS users_email_identity_key
    ON users (lower(email_normalized))
    WHERE email_normalized IS NOT NULL
      AND activation_state IN ('pending_activation', 'active');

-- --------------------------------------------------------------------------- --
-- registration_mail_jobs：注册验证邮件（I）
--   purpose          预留多用途（当前唯一值 'email_verify'）
--   email_normalized 配额主体（同邮箱 60s 冷却 / 时 3 / 日 5）+ 收件地址
--   token_hash       验证 token 的域分离 HMAC（一次性、30 分钟、只存 hash）
--   payload_enc      加密冻结正文（含验证链接=含明文 token；Fernet，
--                    registration_mail_worker.encrypt_payload）
--   status           queued→sent / failed / superseded（同邮箱新请求作废旧
--                    token）/ consumed（验证成功）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS registration_mail_jobs (
    job_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    email_normalized TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    payload_enc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_purpose_check'
    ) THEN
        ALTER TABLE registration_mail_jobs ADD CONSTRAINT
            registration_mail_jobs_purpose_check
            CHECK (purpose IN ('email_verify'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'registration_mail_jobs'::regclass
          AND conname = 'registration_mail_jobs_status_check'
    ) THEN
        ALTER TABLE registration_mail_jobs ADD CONSTRAINT
            registration_mail_jobs_status_check
            CHECK (status IN ('queued', 'sent', 'failed', 'superseded',
                              'consumed'));
    END IF;
END
$$;

COMMENT ON TABLE registration_mail_jobs IS
    '注册验证邮件队列（0037 I 线；禁止命名 outbox——billing 已占用）。token/正文含密信息只存 hash 与加密载荷';

CREATE INDEX IF NOT EXISTS registration_mail_jobs_queue_idx
    ON registration_mail_jobs (status, scheduled_at);
CREATE INDEX IF NOT EXISTS registration_mail_jobs_email_time_idx
    ON registration_mail_jobs (email_normalized, created_at);
