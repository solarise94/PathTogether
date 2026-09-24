-- =========================================================================== --
-- 0066_ingestion_jobs_cos_pool.sql：COS 直传摄取任务与 10 GB 暂存池账本
-- （docs/cos-direct-upload-audit-plan.md §4/§6 Phase 1，裁决 A-presign-parts；
-- 分流规则 docs/upload-routing-open-source-review.md D1/D15）。
--
-- 新增实体：
--   - ingestion_jobs：COS 直传任务（source_kind 固定 cos_staging，transport
--     固定 presign_parts——B-SDK-STS 已裁决不做，不建双栈列语义）。
--     与 upload_tasks 完全分离（D1：不迁移、不复用浏览器串行 offset 表）。
--     状态机：waiting_capacity → preparing → uploading → completing →
--     queued → downloading → validating → ready → completed；
--     终态：completed / cancelled / failed / expired。
--     ready=本地原子提升+metadata+配额一次结算+恢复记录已成功（未过 Viewer
--     readiness）；completed=worker readiness probe 通过且 CAS 置 viewer_ready。
--     cleanup_* 独立于主状态（§6.2：COS 删除失败不得回滚本地成功切片）。
--   - ingestion_events：任务事件流（审计/幂等回放；不含任何秘密——
--     签名 URL、STS、SecretId/Key 禁止入 detail）。
--   - cos_pool_state：全局单行（id=1）容量账本。容量准入必须单事务内
--     SELECT ... FOR UPDATE 锁该行并预约，禁止进程内变量或先查后写。
--
-- 锁序（全仓唯一口径，函数 docstring 同步声明）：
--   - 一切触及既有 job 行的路径（准入/取消/结算/清理/续租/FIFO）：
--     ingestion_jobs 行 → upload_reservations 行 → upload_user_quotas 行
--     → cos_pool_state 行（沿用 V2 的 reservation→quota 顺序，pool 恒最后）；
--   - 纯新建（INSERT 新 job 行，不锁任何既有行）：无锁序约束；
--   - 持有 cos_pool_state 行锁期间不得回头等 quota/reservation/job 行（无环）。
--
-- 并发约束（DB 层兜底，应用层仍须先判）：
--   - 每身份至多一条 waiting_capacity（部分唯一索引）；
--   - 幂等键：同 (owner, idempotency_key) 在非 cancelled/failed/expired
--     状态内唯一——completed 复用返回既有行，取消/失败/过期后允许重建。
--
-- 幂等：IF NOT EXISTS 对已应用库重跑 no-op；cos_pool_state 首行由
-- INSERT ... ON CONFLICT DO NOTHING 惰性建立（capacity/safety 默认值
-- 10_000_000_000 / 500_000_000，均为十进制字节，见合同 §6.1——应用层
-- env 可覆盖时须与该行一致，准入算术以行值为权威）。
--
-- json/dual 后端 fail-closed：本表族只被 ingestion_store/cos_pool_store
-- 在 postgres 后端使用，json 模式不得启用 COS capability。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id                   TEXT        PRIMARY KEY,            -- inj_<hex>
    owner_user_id            TEXT        NOT NULL DEFAULT '',
    owner_role               TEXT        NOT NULL DEFAULT '',
    idempotency_key          TEXT,
    filename                 TEXT        NOT NULL,
    safe_name                TEXT        NOT NULL,               -- 本地提升目标 canonical 名
    format_ext               TEXT        NOT NULL,               -- 白名单内小写扩展名
    declared_size            BIGINT      NOT NULL CHECK (declared_size > 0),
    state                    TEXT        NOT NULL DEFAULT 'waiting_capacity'
                                         CHECK (state IN ('waiting_capacity', 'preparing',
                                                          'uploading', 'completing', 'queued',
                                                          'downloading', 'validating', 'ready',
                                                          'completed', 'cancelled', 'failed',
                                                          'expired')),
    transport                TEXT        NOT NULL DEFAULT 'presign_parts'
                                         CHECK (transport IN ('presign_parts')),
    policy_version           TEXT,
    route_reason             TEXT,
    -- 容量账本侧
    pool_reserved_bytes      BIGINT      NOT NULL DEFAULT 0,     -- 准入起持有，清理确认后释放
    capacity_admitted_at     TIMESTAMPTZ,
    local_reservation_id     TEXT,                               -- upload_reservations（quota 适用身份）
    -- COS 源身份（钉死；浏览器回报只作提示）
    bucket                   TEXT,
    object_key               TEXT,                               -- incoming/<owner>/<job>/rand 服务端生成
    upload_id                TEXT,                               -- worker 发起的 multipart uploadId
    cos_version_id           TEXT,                               -- Complete 后钉死的版本
    source_etag              TEXT,
    source_size_bytes        BIGINT,
    part_plan_json           JSONB,                              -- 冻结分块计划 [{n,offset,length}]
    -- 下载与费用
    download_checkpoint_json JSONB,                              -- {next_offset, ...} 持久断点
    downloaded_bytes         BIGINT      NOT NULL DEFAULT 0,     -- 已确认落盘
    logical_download_bytes   BIGINT      NOT NULL DEFAULT 0,     -- 逻辑下载（唯一字节）
    wire_download_bytes      BIGINT      NOT NULL DEFAULT 0,     -- 实际传输（含失败/重传，对账账单）
    -- 本地提交（§4：validating 内、原子提升之前持久化 commit intent）
    commit_intent_json       JSONB,
    commit_started_at        TIMESTAMPTZ,
    local_ready_at           TIMESTAMPTZ,
    slide_canonical_name     TEXT,
    sha256_actual            TEXT,
    viewer_ready             BOOLEAN     NOT NULL DEFAULT false,
    -- 远端清理（独立状态机，单独 lease/fencing）
    cleanup_status           TEXT        NOT NULL DEFAULT 'none'
                                         CHECK (cleanup_status IN ('none', 'pending', 'cleaned',
                                                                   'failed')),
    cleanup_attempts         INTEGER     NOT NULL DEFAULT 0,
    cleanup_last_error       TEXT,
    cleanup_next_retry_at    TIMESTAMPTZ,
    cleanup_lease_token      TEXT,
    cleanup_lease_expires_at TIMESTAMPTZ,
    -- worker 执行租约（fencing：generation 单调递增，旧 worker 收口被拒）
    worker_lease_token       TEXT,
    worker_lease_expires_at  TIMESTAMPTZ,
    worker_generation        INTEGER     NOT NULL DEFAULT 0,
    -- 期限与终态
    fail_code                TEXT,                               -- 稳定错误码（cos_waiting_timeout 等）
    waiting_expires_at       TIMESTAMPTZ,                        -- created_at + COS_WAITING_MAX_AGE
    job_deadline_at          TIMESTAMPTZ,                        -- capacity_admitted_at + COS_JOB_MAX_AGE
    terminal_at              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ingestion_jobs IS
    'COS 直传摄取任务（A-presign-parts）：浏览器只拿绑定 Content-Length 的 UploadPart 预签名，字节不经平台；worker 负责发起/核对/合并/下载/入库/清理';

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_owner_state
    ON ingestion_jobs (owner_user_id, state);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_waiting_fifo
    ON ingestion_jobs (created_at, job_id) WHERE state = 'waiting_capacity';
CREATE UNIQUE INDEX IF NOT EXISTS ingestion_jobs_one_waiting_per_owner
    ON ingestion_jobs (owner_user_id) WHERE state = 'waiting_capacity';
-- 每身份至多一条「已准入未提交」任务（COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY=1
-- 的 DB 兜底）。状态清单与 ACTIVE_UPLOAD_STATES（ingestion_store）一致；
-- 应用层在配额/池判定前先判，此索引兜底并发窗口（准入事务串行于池行锁，
-- 但 COUNT 只见已提交行，唯一索引是硬保证）。
CREATE UNIQUE INDEX IF NOT EXISTS ingestion_jobs_one_active_per_owner
    ON ingestion_jobs (owner_user_id)
    WHERE state IN ('preparing', 'uploading', 'completing', 'queued',
                    'downloading', 'validating');
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_state_lease
    ON ingestion_jobs (state, worker_lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_cleanup
    ON ingestion_jobs (cleanup_status, cleanup_next_retry_at);
CREATE UNIQUE INDEX IF NOT EXISTS ingestion_jobs_idempotency_live
    ON ingestion_jobs (owner_user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND state NOT IN ('cancelled', 'failed', 'expired');

COMMENT ON INDEX ingestion_jobs_one_waiting_per_owner IS
    '每身份至多一条等待容量任务（COS_MAX_WAITING_PER_IDENTITY=1 的 DB 兜底；应用层同事务先判）';
COMMENT ON INDEX ingestion_jobs_idempotency_live IS
    '创建幂等：同 (owner,key) 存活/已完成任务唯一；cancelled/failed/expired 后允许重建';

CREATE TABLE IF NOT EXISTS ingestion_events (
    event_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id     TEXT        NOT NULL,
    kind       TEXT        NOT NULL,
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ingestion_events_job_time
    ON ingestion_events (job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ingestion_events_job_kind_time
    ON ingestion_events (job_id, kind, created_at);

COMMENT ON TABLE ingestion_events IS
    '摄取任务事件流（审计/幂等回放）；禁止写入签名 URL、STS、SecretId/Key 等任何秘密';

CREATE TABLE IF NOT EXISTS cos_pool_state (
    id                    INTEGER      PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    capacity_bytes        BIGINT       NOT NULL,
    safety_bytes          BIGINT       NOT NULL,
    reserved_bytes        BIGINT       NOT NULL DEFAULT 0,
    observed_remote_bytes BIGINT,                       -- reconciler 分页列举所得；NULL=未观测
    reconcile_status      TEXT         NOT NULL DEFAULT 'ok'
                                        CHECK (reconcile_status IN ('ok', 'reconcile_required')),
    reconciled_at         TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE cos_pool_state IS
    'COS 10 GB 十进制暂存池全局账本（单行）：准入事务 FOR UPDATE 锁本行；reserved 为已准入未完成清理的预约和，observed 为 reconciler 实测；reconcile_required 时暂停新准入与新凭证（fail-closed）';

INSERT INTO cos_pool_state (id, capacity_bytes, safety_bytes)
VALUES (1, 10000000000, 500000000)
ON CONFLICT (id) DO NOTHING;
