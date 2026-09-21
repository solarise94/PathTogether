-- =========================================================================== --
-- 0062_research_data_deletion_jobs.sql：研究副本删除任务
-- （docs/agent-plan-20260921-registration-consent-research.md §3.5/§6.2/§6.3，
-- P2 可撤回研究授权与账户设置）。
--
--   - 撤回提交在**同一数据库事务**内锁 consent 行、state=withdrawn、epoch++、
--     写不可变历史并创建本表删除任务（§6.3「撤回原子」——不存在「已撤回但
--     没有删除任务」的中间态；事务回滚则两者一起回滚）。
--   - POST /api/account/research-data/deletion 幂等创建 reason='user_request'
--     任务：每用户至多一条**未终态**（pending/running）任务，重复请求返回
--     既有任务（部分唯一索引兜底，应用层 ON CONFLICT DO NOTHING）。
--   - 任务记录创建时的 consent_epoch：恢复备份/重放旧数据时以任务清单为准，
--     撤回后不得让研究副本「复活」（§6.3 备份恢复不复活）。
--   - 该操作**不删除**业务切片、标注、临床/科研工作记录（§3.5），只清理
--     研究副本层；清理进度按在线副本/导出/备份三段跟踪，安全错误码不落
--     明文堆栈。
--
-- 幂等：全部 IF NOT EXISTS；不修改已发布迁移。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS research_data_deletion_jobs (
    job_id          TEXT        PRIMARY KEY,
    user_id         TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    -- 创建任务时的授权 epoch（无 consent 行的 user_request 允许 0）
    consent_epoch   INTEGER     NOT NULL CHECK (consent_epoch >= 0),
    -- withdrawal = 撤回事务内自动创建；user_request = 账户设置显式申请
    reason          TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending',
    online_cleared  BOOLEAN     NOT NULL DEFAULT FALSE,   -- 在线研究副本已清理
    exports_cleared BOOLEAN     NOT NULL DEFAULT FALSE,   -- 导出/工作副本已清理
    backups_pending BOOLEAN     NOT NULL DEFAULT TRUE,    -- 隔离备份轮换清理未完成
    error_code      TEXT,                               -- 受控错误码（不落明文堆栈）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    CHECK (reason IN ('withdrawal', 'user_request')),
    CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);
COMMENT ON TABLE research_data_deletion_jobs IS
    '研究副本删除任务（0062 P2，§3.5/§6.2）：撤回事务内原子创建或用户显式幂等申请；按在线/导出/备份三段跟踪清理进度；不删除业务切片或工作记录';

-- 每用户至多一条未终态任务（幂等创建的数据库兜底）
CREATE UNIQUE INDEX IF NOT EXISTS research_data_deletion_jobs_one_active
    ON research_data_deletion_jobs (user_id)
    WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_research_data_deletion_jobs_user
    ON research_data_deletion_jobs (user_id, created_at);
