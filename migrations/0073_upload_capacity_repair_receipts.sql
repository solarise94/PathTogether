-- =========================================================================== --
-- 0073_upload_capacity_repair_receipts.sql：容量核账应用回执（R12 §4.2）。
--
-- 冻结计划 → 预检 → 应用 的幂等去重账：每个计划动作一个持久 action_key
-- （唯一），同计划重跑凭回执跳过、不重复收费；清理完成后重跑旧计划不得
-- 重新制造责任（回执在，跳过；无回执且状态漂移 → 拒绝）。
-- 仅审计/去重用途——**不存第二份容量余额**（容量责任仍在
-- upload_reservations，plan §3.5 唯一账本约束）。
-- 幂等：CREATE TABLE IF NOT EXISTS；重跑 no-op。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS upload_capacity_repair_receipts (
    action_key            TEXT PRIMARY KEY,
    plan_hash             TEXT NOT NULL,
    target_kind           TEXT NOT NULL,
    target_id             TEXT NOT NULL,
    action                TEXT NOT NULL,
    result_reservation_id TEXT,
    applied_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS upload_capacity_repair_receipts_by_plan
    ON upload_capacity_repair_receipts (plan_hash);

COMMENT ON TABLE upload_capacity_repair_receipts IS
    '容量核账计划应用回执（0073；R12）：action_key 幂等去重，不存第二份余额';
