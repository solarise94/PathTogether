-- =========================================================================== --
-- 0087_support_payments.sql：支付宝「AI 网页应用收款」赞助订单（沙箱，自愿赞助）。
--
-- 设计边界（docs/converter-support-page-design-20261021.md §支付宝 AI 付的后续扩展）：
--   - 赞助完全自愿、匿名可用；不绑定平台账号（无 user 外键），绝不兑换 AI
--     额度/存储/功能，不触碰 billing_* / spend_*（与消费额度保持独立）。
--   - 排序说明：生产 head 是 0084；0085 已被分支 kfb-index-diagnostics 占用、
--     0086 已被分支 password-reset 占用，故本文件取 0087。合并顺序以文件名
--     字典序为准（pg_store.ensure_schema 按文件名升序逐个应用），三个分支
--     合并后互不影响（各自建独立表）。
--   - 金额一律存整数「分」（BIGINT），元字符串只在边界上出现；notify 的
--     total_amount 校验用字符串规范化后与本地分值精确比对（禁浮点）。
--   - 状态机：created → paying(WAIT_BUYER_PAY) → paid(TRADE_SUCCESS/
--     TRADE_FINISHED)；created/paying → closed(TRADE_CLOSED/超时关单)。
--     paid 是吸收态（不可回退）；退款单独记账（support_refunds），不改状态。
--   - 异步通知事件表做持久化幂等（notify_id / 全参 hash 唯一），本地无公网
--     HTTPS notify_url 时通知可能永远不到——补偿查单（sweep → trade.query）
--     是沙箱验收路径。
--
-- 全部幂等（CREATE IF NOT EXISTS / DROP CONSTRAINT IF EXISTS 先删后建）。
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- 赞助订单。app_id / seller_pid 建单时快照自运行时支付配置——异步通知的业务
-- 归属校验以这份快照为准（契约：app_id/seller_id 从运行时配置校验，不从通知
-- 参数取可信预期值；快照保证配置轮换后历史订单仍按建单时配置校验）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS support_orders (
    out_trade_no  TEXT        PRIMARY KEY,              -- 商户订单号（随机高熵，即公开订单 ref）
    status        TEXT        NOT NULL
                  CHECK (status IN ('created', 'paying', 'paid', 'closed')),
    amount_fen    BIGINT      NOT NULL CHECK (amount_fen > 0),
    currency      TEXT        NOT NULL DEFAULT 'CNY',
    subject       TEXT        NOT NULL,
    mode          TEXT        NOT NULL CHECK (mode IN ('sandbox', 'production')),
    app_id        TEXT        NOT NULL,
    seller_pid    TEXT        NOT NULL,
    trade_no      TEXT,                                 -- 支付宝交易号（首次写入后不覆盖）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at       TIMESTAMPTZ,
    closed_at     TIMESTAMPTZ,
    closed_reason TEXT,
    last_query_at TIMESTAMPTZ,                          -- 补偿查单记录（sweep / 回跳页）
    last_query_result TEXT,
    -- 下单幂等键（2026-10-21 R1 评审 add-on）：支付页表单每次渲染生成的随机
    -- key；真双击的两次并发 POST 带同一 key，UNIQUE 索引 + ON CONFLICT DO
    -- NOTHING 保证只建一单。可空（NULL 不参与唯一——无 key 走会话级复用）。
    idempotency_key TEXT
);
-- 唯一索引：PostgreSQL 唯一索引对 NULL 不去重（无 key 的订单不受约束），
-- 且 ON CONFLICT (idempotency_key) 需要完整（非部分）唯一索引做推断。
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_orders_idempotency_key
    ON support_orders (idempotency_key);
COMMENT ON TABLE support_orders IS
    '支付宝赞助订单（0087，沙箱）：自愿赞助、匿名可用、不绑账号、不兑额度；金额存整数分';
COMMENT ON COLUMN support_orders.app_id IS
    '建单时运行时支付配置快照（notify 业务校验权威之一，非通知参数本身）';
COMMENT ON COLUMN support_orders.seller_pid IS
    '建单时运行时收款方 PID 快照（notify seller_id 校验权威之一）';

CREATE INDEX IF NOT EXISTS idx_support_orders_status_created
    ON support_orders (status, created_at);

-- --------------------------------------------------------------------------- --
-- 异步通知事件（持久化幂等）：event_key = notify_id（缺失时用全参规范化 hash）。
-- raw_params 不存 sign（验签值无重放价值，且避免把签名物料落库）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS support_notify_events (
    event_key    TEXT        PRIMARY KEY,
    notify_id    TEXT,
    out_trade_no TEXT        NOT NULL,
    trade_no     TEXT,
    trade_status TEXT,
    raw_params   JSONB       NOT NULL,
    accepted     BOOLEAN     NOT NULL DEFAULT FALSE,   -- 是否按付款/关单事件处理
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE support_notify_events IS
    '支付宝异步通知事件（0087）：持久化幂等去重；raw_params 已剥离 sign';

-- --------------------------------------------------------------------------- --
-- 退款（独立于订单状态机）。out_request_no 主键即支付宝侧幂等键：同一次退款
-- 重试必须复用原号与原金额（契约：防重复退款）。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS support_refunds (
    out_request_no    TEXT        PRIMARY KEY,
    out_trade_no      TEXT        NOT NULL
                      REFERENCES support_orders(out_trade_no) ON DELETE CASCADE,
    refund_amount_fen BIGINT      NOT NULL CHECK (refund_amount_fen > 0),
    status            TEXT        NOT NULL
                      CHECK (status IN ('requested', 'refunded', 'unknown', 'failed')),
    fund_change       TEXT,
    trade_no          TEXT,
    refund_reason     TEXT,
    requested_by      TEXT,                           -- 操作 owner 的 user_id
    -- 退款操作幂等键（R2-2，可空）：body 显式传入；同 key 直接返回原退款行
    -- 且不再发起任何支付宝调用。UNIQUE 索引对 NULL 不去重（无 key 不受约束）。
    idempotency_key   TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE support_refunds IS
    '支付宝赞助退款（0087）：owner-only 发起；out_request_no 幂等，重试复用原号';
CREATE INDEX IF NOT EXISTS idx_support_refunds_order
    ON support_refunds (out_trade_no, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_refunds_idempotency_key
    ON support_refunds (idempotency_key);

-- --------------------------------------------------------------------------- --
-- 后绑定幂等键别名（2026-10-23 R3/R4 评审 add-on）。上方原表 idempotency_key
-- 列只承载**建单/建行时**的 key；会话复用 / 未决退款解析路径上，请求携带的
-- **新** key 事后绑定到既有订单/退款行，落在别名表。key 与目标行永久绑定
-- （同 key 任何状态都解析到同一行），且必须**先落别名再返回**——堵住「复用/
-- 解析命中但键未持久化 → 同 key 重放另建一单/另退一笔」的窗口。key 全局唯一
-- （PRIMARY KEY）；FK ON DELETE CASCADE 随目标行清理。
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS support_order_key_aliases (
    idempotency_key TEXT        PRIMARY KEY,
    out_trade_no    TEXT        NOT NULL REFERENCES support_orders(out_trade_no) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS support_refund_key_aliases (
    idempotency_key TEXT        PRIMARY KEY,
    out_request_no  TEXT        NOT NULL REFERENCES support_refunds(out_request_no) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now());
COMMENT ON TABLE support_order_key_aliases IS
    '赞助订单后绑定幂等键别名（0087）：下单复用命中的新 key→原订单永久绑定，先落库再返回';
COMMENT ON TABLE support_refund_key_aliases IS
    '赞助退款后绑定幂等键别名（0087）：未决解析命中的新 key 与 refunded 置位同事务落库';
