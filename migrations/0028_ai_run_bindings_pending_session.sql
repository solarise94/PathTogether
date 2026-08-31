-- =========================================================================== --
-- 0028_ai_run_bindings_pending_session.sql：金额硬闸绑定两阶段化（阶段 1
-- pending 行），P1-2 fail-closed 修复。
--
-- 语义变更（不改 0027 历史语义，仅放宽一列约束）：
--   - 根因：0027 的绑定行只在 HistoPilot 2xx SSE 头之后的 on_accepted 写入
--     （record_run_binding），而 HistoPilot 返回 session 后立刻后台 driveMain，
--     第一次 provider 调用前 authorizeHold 就会带 request_id 到达——此时绑定
--     行尚不存在，解析第①步落空 → 409 usage_subject_not_ready。冷启动 HP 把
--     not_ready 当 unknown mode 仍会调 provider（客户端侧误判），形成金额硬闸
--     的绑定失败窗口；
--   - 修复：绑定拆两阶段——Phase 1（起跑、调用 HP 之前）先 INSERT pending 行
--     （histopilot_session_id = NULL，主体已在行内）；Phase 2（on_accepted）
--     UPDATE 把 session attach 上去。pending 行按 request_id 命中即 resolve
--     （billing_store §7.2 第①步），HP 在 attach 前的 authorizeHold / usage
--     event 不再 not_ready；
--   - demo 主体不入本表（仍归 demo_runs.histopilot_session_id，0026 红线），
--     pending 只对 owner/user（0027 CHECK 不变）。
--
-- 幂等/可重跑：DROP NOT NULL 在列已 nullable 时是 no-op；重跑不改变任何行。
-- 回滚：先确认无 pending 行（histopilot_session_id IS NULL 计数为 0）再
-- ALTER TABLE ai_run_bindings ALTER COLUMN histopilot_session_id SET NOT NULL。
-- =========================================================================== --

ALTER TABLE ai_run_bindings
    ALTER COLUMN histopilot_session_id DROP NOT NULL;

COMMENT ON COLUMN ai_run_bindings.histopilot_session_id IS
    'HistoPilot session 绑定：NULL = pending（Phase 1 起跑前已写入主体、尚未 attach session，按 request_id 命中即 resolve）；非 NULL = Phase 2（on_accepted）已 attach，解析要求与事件 session 一致';
