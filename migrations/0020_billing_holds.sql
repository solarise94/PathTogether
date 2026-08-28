-- =========================================================================== --
-- 0020_billing_holds.sql：逐 model call 预授权 hold（admin-billing 方案
-- §6.1 迁移拆分第 3 步 / §12.3 Phase C，PR7——影子/advisory 形态，v0.5）。
--
-- 语义（owner 2026-08-28 指令 + §19 v0.5）：holds 是**advisory/影子**——
-- authorize **永不因余额拒绝**（registered 用户不做真实计费限制），只把
-- 「若开启硬额度会不会拒」（would_deny）算出来记录观测；不写任何
-- usage_debit（真实扣费仍走 PR6 ingest 模拟软扣费，两条链在影子期解耦）。
--
--   - call_id：与 ai_usage_events.call_id 同一命名空间（^call_[0-9a-f]{32}$），
--     UNIQUE 兜底同 call 只一条 hold（并发 authorize 由唯一约束 + SAVEPOINT
--     重读吸收，见 billing_store.authorize_hold）；
--   - account_id 可 NULL：主体尚无 billing_accounts 行时**不强制开户**
--     （影子期不因 hold 副作用改账户面；与 PR6 ingest 自动开户的语义差异
--     是刻意的——ingest 是账务事实，hold 只是预授权影子）；
--   - estimated_nano_cny 可 NULL：authorize 时刻无 active customer_charge
--     价目（未知模型/区间）→ 无从估算，would_deny 亦 NULL（未知不裁决）；
--   - balance_nano_cny：authorize 时刻的账户 ledger 有符号合计快照
--     （无账户 NULL；模拟期余额允许为负）；
--   - status：open → settled（带 event_id 终局结算）/ released（调用失败
--     无 usage 的正常终态）/ expired（TTL 惰性回收：authorize/settle 同事务
--     UPDATE ... WHERE status='open' AND expires_at < now()）；
--   - event_id **不加外键**（与 billing_ledger_entries.event_id 不同）：
--     HistoPilot durable outbox 投递与 hold settle 是两条独立重试链，
--     settle 可能先于 usage 事件入库到达（outbox 乱序/退避重投），影子期
--     容忍悬空引用、由观测口径核对，不为排序一致性引入跨链耦合；
--   - metadata：request_hash（authorize 载荷 canonical hash，幂等重放比对
--     用）+ charge_price_book_id + provider + ttl_seconds，全部非敏感。
--
-- 索引：(status, expires_at) 供惰性回收扫描；(subject_type, subject_id,
-- created_at) 供主体维度观测；(session_id) 供按 run 排查。全库无删除路径
-- （hold 是审计友好的终态机，不是可回收资源）。
--
-- 幂等（IF NOT EXISTS / ON CONFLICT 语义，对齐 0001-0019 风格）；由现有
-- migration runner（pg_store.ensure_schema → schema_migrations）记录。
-- billing 能力仅 STORAGE_BACKEND=postgres 开放（json/dual 稳定
-- pg_backend_required，路由层 fail-closed，不降级）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS billing_holds (
    hold_id              TEXT        PRIMARY KEY,   -- hold_<24hex>
    call_id              TEXT        NOT NULL UNIQUE
                                     CHECK (call_id ~ '^call_[0-9a-f]{32}$'),
    account_id           TEXT        REFERENCES billing_accounts(account_id),
    subject_type         TEXT        NOT NULL CHECK (subject_type IN ('owner','user')),
    subject_id           TEXT        NOT NULL,
    installation_id      TEXT        NOT NULL,
    session_id           TEXT        NOT NULL,
    model                TEXT        NOT NULL,
    estimated_nano_cny   BIGINT      CHECK (estimated_nano_cny >= 0),
    balance_nano_cny     BIGINT,     -- authorize 时刻余额快照（无账户 NULL）
    would_deny           BOOLEAN,    -- 若硬额度开启是否会拒（估算/余额未知 NULL）
    status               TEXT        NOT NULL DEFAULT 'open'
                                     CHECK (status IN ('open','settled','released','expired')),
    event_id             TEXT        CHECK (event_id IS NULL
                                           OR event_id ~ '^use_[0-9a-f]{32}$'),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at           TIMESTAMPTZ,
    expires_at           TIMESTAMPTZ NOT NULL,
    metadata             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- 终态一致性：settled 必带 event_id 与终态时间；open 行不得预挂 event_id
    CHECK (status <> 'settled' OR (event_id IS NOT NULL AND settled_at IS NOT NULL)),
    CHECK (status <> 'open' OR event_id IS NULL)
);
COMMENT ON TABLE billing_holds IS
    '逐 model call 预授权 hold（admin-billing §12.3，PR7 影子/advisory）：永不因余额拒绝，would_deny 仅观测；event_id 无 FK——outbox 乱序时 settle 可先于 usage 事件到达；TTL 惰性回收（authorize/settle 同事务标 expired）';
CREATE INDEX IF NOT EXISTS idx_billing_holds_expiry
    ON billing_holds (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_billing_holds_subject
    ON billing_holds (subject_type, subject_id, created_at);
CREATE INDEX IF NOT EXISTS idx_billing_holds_session
    ON billing_holds (session_id);
