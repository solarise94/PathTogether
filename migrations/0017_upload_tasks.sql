-- =========================================================================== --
-- 0017_upload_tasks.sql：Upload V2 分片续传任务表
-- （docs/upload-resumable-fix-plan.md §3.1，U2 后端）
--
-- 单表承载上传任务状态机（严格串行分片模型，§3.0 review 修订）：
--   active → committing → committed / committing → active（临时故障回滚）
--   committing → failed（确定性失败：整文件哈希不匹配 / 非法切片）
--   active → cancelled / expired（DELETE / TTL；failed → cancelled）
--
-- 字段要点（§3.1 数据模型）：
--   - confirmed_offset：服务端已确认字节偏移，串行模型下唯一写入点；
--   - last_chunk_*：最后已确认分片的 (offset,length,sha256)，重复 PUT 幂等
--     比对键（§3.2.1，非最后一片不比对哈希）；
--   - sha256_expected / sha256_actual：客户端可选上报 / commit 时服务端流式
--     复算（权威值）；不做边收边滚动哈希（§3.2.3）；
--   - commit_token / commit_started_at：commit 三段式（§3.2.5）的受理凭据，
--     短事务 A 写入、事务外做哈希+OpenSlide+原子提升、短事务 B 凭 token 收口；
--   - expires_at：任务 TTL（默认 24h，UPLOAD_TASK_TTL env），每次成功 PUT 刷新；
--   - reservation_id：关联 PG 配额预占（role=user 才有；owner/免认证为 NULL），
--     每次 PUT 续租（upload_guard.renew_reservation，§3.2.4）。
--
-- 并发：状态转移在短事务内 SELECT ... FOR UPDATE 锁本表行（upload_task_store）；
-- 整文件哈希 / OpenSlide 验证 / 文件提升**不在行锁内**（§3.2.5 三段式）。
--
-- owner_user_id 不设 users 外键：AUTH_ENABLED=False（本地免登录）形态 owner
-- 归一为空 user_id，任务按 (upload_id, owner_user_id) 绑定，他人 403 不泄露
-- 存在性。幂等（IF NOT EXISTS，对齐 0001-0016 风格）；json 后端等价文件记录
-- 见 upload_task_store.py（dual-backend 约定）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS upload_tasks (
    upload_id         TEXT        PRIMARY KEY,             -- 形如 upt_<hex>
    owner_user_id     TEXT        NOT NULL DEFAULT '',     -- 空 = 本地免登录 owner
    filename          TEXT        NOT NULL,
    safe_name         TEXT        NOT NULL,
    declared_size     BIGINT      NOT NULL CHECK (declared_size > 0),
    chunk_size        BIGINT      NOT NULL,
    confirmed_offset  BIGINT      NOT NULL DEFAULT 0,
    last_chunk_offset BIGINT,
    last_chunk_length BIGINT,
    last_chunk_sha256 TEXT,
    sha256_expected   TEXT,                                -- 客户端可选整文件哈希
    sha256_actual     TEXT,                                -- commit 时服务端复算（权威）
    reservation_id    TEXT,                                -- 关联 upload_reservations
    state             TEXT        NOT NULL DEFAULT 'active'
                                 CHECK (state IN ('active', 'committing', 'committed',
                                                  'failed', 'cancelled', 'expired')),
    commit_token      TEXT,
    commit_started_at TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ
);
COMMENT ON TABLE upload_tasks IS
    'Upload V2 分片续传任务（严格串行 offset；状态转移 FOR UPDATE 短事务，重 IO 不入锁）';

CREATE INDEX IF NOT EXISTS idx_upload_tasks_owner_state
    ON upload_tasks (owner_user_id, state);
CREATE INDEX IF NOT EXISTS idx_upload_tasks_expires
    ON upload_tasks (state, expires_at);
