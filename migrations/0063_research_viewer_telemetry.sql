-- =========================================================================== --
-- 0063_research_viewer_telemetry.sql：人工读片行为采集研究存储
-- （docs/agent-plan-20260921-registration-consent-research.md §6.2/§7，P3）。
--
--   - research_subjects：随机伪名 ↔ user_id 的隔离映射（§6.2）。研究查询默认
--     不联 users/email——subject 表是受控的「可撤回数据来源链」入口（删除任务
--     按 user → subject → 会话/事件级联）；它不是可逆信息的「匿名化」声明。
--   - research_viewing_sessions：随机会话 ID、subject、consent_epoch、
--     scope_version、schema_version、切片**研究伪名**（不复制真实文件名/路径/
--     患者标签——服务端用带盐 keyed hash 生成）、started_day、status、
--     expires_at（§8：研究动作每条最多 90 天）。
--   - research_viewer_events：event_id、session_id、seq、action、
--     schema_version、consent_epoch、受限 payload（JSONB，服务端白名单校验，
--     前端事件不含 user_id/邮箱/等待时长字段）、server_received_at（仅接收/
--     保留期/运维，不用于计算等待时长）、expires_at；唯一 (session_id,
--     event_id) 支撑重传幂等，唯一 (session_id, seq) 保序——不同内容复用 ID
--     由服务层比对后拒绝（不覆盖旧事件）。
--   - research_conversation_items：研究用对话/轨迹副本（§6.2/§7.4）。P3 本轮
--     **只落表结构**（写入链路属后续阶段）：来源事件 ID 的非公开映射、
--     subject、consent_epoch、消息来源、去标识化内容、准入检查结果、
--     expires_at——必须有可撤回的数据来源链，不能只存「匿名文本」。
--
-- 撤回即时阻断（§6.3-3）：ingestion 与 withdraw 使用同一 user_research_consents
-- 行锁（research_store 事务内 SELECT ... FOR UPDATE），撤回提交后的写入不成功。
--
-- 幂等：全部 IF NOT EXISTS；不修改已发布迁移。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS research_subjects (
    subject_id  TEXT        PRIMARY KEY,             -- "rs_" + 随机 token（伪名）
    user_id     TEXT        NOT NULL UNIQUE REFERENCES users(user_id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE research_subjects IS
    '研究主体伪名映射（0063 P3，§6.2）：随机伪名与 user_id 隔离；研究查询默认不联 users/email，本表是撤回/删除链的受控来源入口';

CREATE TABLE IF NOT EXISTS research_viewing_sessions (
    session_id      TEXT        PRIMARY KEY,         -- "rvs_" + 随机 token
    subject_id      TEXT        NOT NULL REFERENCES research_subjects(subject_id) ON DELETE CASCADE,
    consent_epoch   INTEGER     NOT NULL CHECK (consent_epoch >= 1),
    scope_version   TEXT        NOT NULL,            -- 建会话时的 scope 版本（=协议版本）
    schema_version  TEXT        NOT NULL,            -- 事件 schema（research_store.SCHEMA_VERSION）
    slide_pseudonym TEXT        NOT NULL,            -- 切片研究伪名（带盐 keyed hash；绝存真实文件名/路径/患者标签）
    started_day     DATE        NOT NULL,            -- Asia/Shanghai 自然日（建会话时）
    status          TEXT        NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,            -- 90 天保留期（§8）
    closed_at       TIMESTAMPTZ,
    CHECK (status IN ('active', 'closed', 'expired', 'revoked'))
);
COMMENT ON TABLE research_viewing_sessions IS
    '研究读片会话（0063 P3，§6.2/§7.3）：服务端创建并绑定 subject/epoch；只存切片研究伪名；90 天过期';
CREATE INDEX IF NOT EXISTS idx_research_viewing_sessions_subject
    ON research_viewing_sessions (subject_id, created_at);
CREATE INDEX IF NOT EXISTS idx_research_viewing_sessions_expiry
    ON research_viewing_sessions (expires_at);

CREATE TABLE IF NOT EXISTS research_viewer_events (
    session_id         TEXT        NOT NULL REFERENCES research_viewing_sessions(session_id) ON DELETE CASCADE,
    event_id           TEXT        NOT NULL,         -- 客户端生成（"evt_" + 随机）
    seq                INTEGER     NOT NULL CHECK (seq >= 1),
    action             TEXT        NOT NULL,
    schema_version     TEXT        NOT NULL,
    consent_epoch      INTEGER     NOT NULL,
    payload            JSONB       NOT NULL,         -- 白名单校验后的受限字段（无 user_id/邮箱/时长字段）
    server_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (session_id, event_id),
    UNIQUE (session_id, seq),
    CHECK (action IN ('zoom_in', 'zoom_out', 'pan', 'observe_pause',
                      'annotation_create', 'annotation_update', 'annotation_delete',
                      'annotation_accept', 'annotation_reject'))
);
COMMENT ON TABLE research_viewer_events IS
    '人工读片行为研究事件（0063 P3，§7.1/§7.2）：归并后的手势/观察/标注动作；observe_pause 无时长字段，seq 保序，重传幂等';
CREATE INDEX IF NOT EXISTS idx_research_viewer_events_expiry
    ON research_viewer_events (expires_at);

-- P3 本轮只落表（写入链路后续阶段；§6.2/§7.4 对话研究副本）
CREATE TABLE IF NOT EXISTS research_conversation_items (
    item_id        TEXT        PRIMARY KEY,
    subject_id     TEXT        NOT NULL REFERENCES research_subjects(subject_id) ON DELETE CASCADE,
    consent_epoch  INTEGER     NOT NULL,
    source_kind    TEXT        NOT NULL,             -- user_message | agent_event
    source_ref     TEXT        NOT NULL,             -- 来源事件 ID 的非公开映射（撤回/删除链）
    origin         TEXT        NOT NULL,             -- 消息来源（可见性口径）
    content        TEXT        NOT NULL,             -- 去标识化后的可见内容
    admission      TEXT        NOT NULL,             -- 准入检查结果
    rejection_code TEXT,                             -- 拒绝原因（受控错误码）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    CHECK (source_kind IN ('user_message', 'agent_event')),
    CHECK (admission IN ('admitted', 'rejected')),
    CHECK (admission = 'admitted' OR rejection_code IS NOT NULL)
);
COMMENT ON TABLE research_conversation_items IS
    '研究用对话/轨迹副本（0063 P3，§6.2/§7.4）：来源链可撤回；发现直接标识符/来源不明/处理失败一律 rejected，不入研究集';
CREATE INDEX IF NOT EXISTS idx_research_conversation_items_subject
    ON research_conversation_items (subject_id, created_at);
CREATE INDEX IF NOT EXISTS idx_research_conversation_items_expiry
    ON research_conversation_items (expires_at);
