-- =========================================================================== --
-- 0013_upload_quotas.sql：用户上传空间配额 / reservation / 速率数据层
-- （docs/open-registration-security-remediation.md §3.3，P0-A 应用层）
--
-- 新增实体：
--   - upload_user_quotas：每用户配额行（quota_bytes 可由 owner 调整）。
--     used_bytes 只在上传成功时累加（consume），reserved_bytes 为在途预占；
--     两者之和 > quota_bytes 即拒绝。注意：删除切片不回退 used_bytes（保守
--     语义，防止删了再传绕过累计口径；需要回收空间时由 owner 直接调行）。
--   - upload_reservations：一次上传请求的预占记录（reserved|consumed|released）。
--     同时兼任「每小时上传请求数」的计数来源（reserved_at 一小时内创建的行数，
--     不论最终状态——限的是尝试次数）；state='reserved' 且未过期的行数即
--     「在途上传数」。reserved 过期由下一次 reserve 在同一事务内惰性回收
--     （锁配额行后先释放过期量再判定，防并发双扣）。
--
-- 并发安全（upload_guard.reserve_upload / topup_reservation）：
--   - 所有判定都在单事务内先 SELECT ... FOR UPDATE 锁 upload_user_quotas 行，
--     同用户的并发预占串行化，禁止「先扣一个维度再失败」的部分写；
--   - topup 额外先锁 reservation 行（锁序恒为 reservation → quota，与
--     reserve 只锁 quota 不构成环）。
--
-- 幂等（IF NOT EXISTS，对齐 0001-0011 风格）。json/dual 后端 fail-closed
-- 不启用（见 upload_guard.py 头注释）；表结构随主 schema 建好，切换后端无需
-- 二次迁移。conftest 的 TRUNCATE users ... CASCADE 会级联清空本表（FK）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS upload_user_quotas (
    user_id        TEXT        PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    quota_bytes    BIGINT      NOT NULL,               -- 缺省由应用层 env 决定（UPLOAD_USER_QUOTA_BYTES）
    used_bytes     BIGINT      NOT NULL DEFAULT 0,     -- 已成功落盘累计
    reserved_bytes BIGINT      NOT NULL DEFAULT 0,     -- 在途预占
    updated_at     TIMESTAMPTZ
);
COMMENT ON TABLE upload_user_quotas IS
    '每用户上传空间配额：used+reserved > quota 即拒（docs open-registration-security-remediation §3.3）';

CREATE TABLE IF NOT EXISTS upload_reservations (
    reservation_id TEXT        PRIMARY KEY,             -- 形如 upr_<hex>
    user_id        TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    reserved_bytes BIGINT      NOT NULL CHECK (reserved_bytes > 0),
    state          TEXT        NOT NULL DEFAULT 'reserved'
                             CHECK (state IN ('reserved', 'consumed', 'released')),
    reserved_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    settled_at     TIMESTAMPTZ,                         -- consume/release 时间
    settled_bytes  BIGINT,                             -- consume 时的实际落盘字节
    updated_at     TIMESTAMPTZ
);
COMMENT ON TABLE upload_reservations IS
    '上传预占：reserved 过期惰性回收；reserved_at 同时是每小时请求限流的计数来源（计尝试次数，不论终态）';

CREATE INDEX IF NOT EXISTS idx_upload_reservations_user_state
    ON upload_reservations (user_id, state, expires_at);
CREATE INDEX IF NOT EXISTS idx_upload_reservations_user_time
    ON upload_reservations (user_id, reserved_at);
