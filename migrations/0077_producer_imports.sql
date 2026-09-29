-- =========================================================================== --
-- 0077_producer_imports.sql：C5 通用 producer 导入通道（2026-09-29，
-- docs/slide-tools/c5-producer-import-contract.md §2/§3）。
--
-- 三张表 + 一个安装行列：
--   producer_imports        通用 producer job 表（§3.1；状态机 §3.2）
--   producer_import_events  事件流水（detail 落库前经 _sanitize_detail 脱敏）
--   plugin_import_grants    用户导入委托 grant（§2.2；(installation,user,
--                            project) 绑定 + TTL + 撤销；非 run grant）
--   plugin_installations.approved_scopes（§2.1：admin 批准的权限面；
--                            存量行缺省 '{}'——老 token 语义不变，不会自动
--                            获得 slide:import）
--
-- 容量（§4）：producer_import 持有者经 upload_guard.HOLDER_KINDS 枚举扩展
-- 进 upload_reservations（同一 import_id 可持 final/scratch 两份不同用途的
-- 绑定预约；0072 的 (holder_kind,holder_id,purpose) 唯一索引按用途区分）。
-- 不新增任何 COS/bucket/object_key/upload_id/conversion 列——本通道没有
-- 这些概念（§3.1/§8「不填假值」）。
--
-- 幂等：CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DO NOTHING；
-- 重跑 no-op。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS producer_imports (
    import_id            TEXT PRIMARY KEY,
    installation_id      TEXT NOT NULL,
    plugin_id            TEXT NOT NULL DEFAULT '',
    grant_id             TEXT NOT NULL,
    owner_user_id        TEXT NOT NULL,
    project_id           TEXT,
    idempotency_key      TEXT,
    payload_sha256       TEXT NOT NULL DEFAULT '',
    slide_id             TEXT NOT NULL,
    filename             TEXT NOT NULL,
    format_ext           TEXT NOT NULL,
    declared_size        BIGINT NOT NULL,
    confirmed_offset     BIGINT NOT NULL DEFAULT 0,
    received_bytes       BIGINT NOT NULL DEFAULT 0,
    sha256_actual        TEXT,
    profile_json         TEXT,
    final_reservation_id    TEXT,
    scratch_reservation_id  TEXT,
    scratch_confirmed_bytes BIGINT NOT NULL DEFAULT 0,
    commit_token         TEXT NOT NULL,
    commit_intent_json   TEXT,
    commit_started_at    TIMESTAMPTZ,
    write_token_hash     TEXT NOT NULL,
    local_cleanup_status   TEXT NOT NULL DEFAULT 'none',
    local_cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    local_cleanup_last_error TEXT,
    local_cleanup_next_retry_at TIMESTAMPTZ,
    plugin_cleanup_status  TEXT NOT NULL DEFAULT 'none',
    plugin_cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    plugin_cleanup_last_error TEXT,
    plugin_cleanup_next_retry_at TIMESTAMPTZ,
    project_associate_state TEXT NOT NULL DEFAULT 'pending',
    state                TEXT NOT NULL DEFAULT 'created',
    fail_code            TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminal_at          TIMESTAMPTZ,
    deadline_at          TIMESTAMPTZ NOT NULL
);

-- 幂等域 = (installation_id, idempotency_key)（§1.1；同键同载荷重放/异载荷
-- 409 的判定靠 payload_sha256，业务层裁决）。
CREATE UNIQUE INDEX IF NOT EXISTS producer_imports_idem_domain
    ON producer_imports (installation_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

ALTER TABLE producer_imports
    DROP CONSTRAINT IF EXISTS producer_imports_state_kind;
ALTER TABLE producer_imports
    ADD CONSTRAINT producer_imports_state_kind
    CHECK (state IN ('created', 'writing', 'committing', 'published', 'done',
                     'cancelled', 'failed', 'expired'));

ALTER TABLE producer_imports
    DROP CONSTRAINT IF EXISTS producer_imports_local_cleanup_kind;
ALTER TABLE producer_imports
    ADD CONSTRAINT producer_imports_local_cleanup_kind
    CHECK (local_cleanup_status IN ('none', 'pending', 'cleaned', 'failed'));

ALTER TABLE producer_imports
    DROP CONSTRAINT IF EXISTS producer_imports_plugin_cleanup_kind;
ALTER TABLE producer_imports
    ADD CONSTRAINT producer_imports_plugin_cleanup_kind
    CHECK (plugin_cleanup_status IN ('none', 'pending', 'cleaned', 'failed'));

ALTER TABLE producer_imports
    DROP CONSTRAINT IF EXISTS producer_imports_associate_kind;
ALTER TABLE producer_imports
    ADD CONSTRAINT producer_imports_associate_kind
    CHECK (project_associate_state IN
           ('none', 'pending', 'succeeded', 'failed'));

ALTER TABLE producer_imports
    DROP CONSTRAINT IF EXISTS producer_imports_sizes_positive;
ALTER TABLE producer_imports
    ADD CONSTRAINT producer_imports_sizes_positive
    CHECK (declared_size > 0 AND confirmed_offset >= 0
           AND received_bytes >= 0 AND scratch_confirmed_bytes >= 0);

COMMENT ON TABLE producer_imports IS
    '通用 producer 导入任务（C5 合同 §3.1）：插件后端把最终单文件产物按有界'
    '流写入平台私有 staging，平台自行验证并经唯一 slide_publish 发布结算；'
    'owner 只来自 grant.user_id（冻结不可改）';
COMMENT ON COLUMN producer_imports.owner_user_id IS
    '= grant.user_id（begin 时冻结）；请求体任何 owner/user 字段一律忽略';
COMMENT ON COLUMN producer_imports.commit_token IS
    'generation/fencing 键（随机、begin 生成；commit intent 与发布代次比对）';
COMMENT ON COLUMN producer_imports.write_token_hash IS
    '任务级凭证 write_token 的 sha256（明文仅 begin 响应返回一次）';
COMMENT ON COLUMN producer_imports.deadline_at IS
    'begin + PRODUCER_IMPORT_MAX_AGE（默认 72h）绝对期限；超期 sweep 终态化';

CREATE TABLE IF NOT EXISTS producer_import_events (
    id         BIGSERIAL PRIMARY KEY,
    import_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS producer_import_events_import_idx
    ON producer_import_events (import_id, id);

COMMENT ON TABLE producer_import_events IS
    'producer 导入事件流水；detail 经 _sanitize_detail 同款脱敏（禁'
    ' sign/secret/token/url/password 形状键——含 write_token/commit_token）';

CREATE TABLE IF NOT EXISTS plugin_import_grants (
    grant_id       TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    plugin_id      TEXT NOT NULL DEFAULT '',
    user_id        TEXT NOT NULL,
    project_id     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    revoked_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS plugin_import_grants_user_idx
    ON plugin_import_grants (user_id, created_at);

COMMENT ON TABLE plugin_import_grants IS
    '用户导入委托 grant（C5 合同 §2.2；非 run grant）：(installation,user,'
    'project) 绑定 + expires_at + revoked_at；UNIQUE 无（同用户可对同项目持'
    '多 grant）；撤销幂等。owner 只来自本表 user_id';

-- §2.1：安装行批准的权限面。缺省 '{}' = 未批准任何扩展权限——存量安装
-- 行不自动获得 slide:import（防自动提权；JWT 发放按 approved_scopes 裁剪）。
ALTER TABLE plugin_installations
    ADD COLUMN IF NOT EXISTS approved_scopes TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN plugin_installations.approved_scopes IS
    'admin 批准的扩展权限面（C5 §2.1）：slide:import 只在显式批准的安装行'
    '出现；安装时未批准的扩展权限申请被拒（fail-closed）';
