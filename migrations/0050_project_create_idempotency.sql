-- =========================================================================== --
-- 0050_project_create_idempotency.sql：项目创建按 (owner, key) 幂等（W3）。
--
-- 新 UI 每份草稿生成一个 Idempotency-Key，网络重试重放同键同负载时应返回
-- 原项目而非再建一个；同键不同负载判 409。旧客户端不带键 → 维持现状
-- （随机 pid，每次调用各建一个，不写本表）。
--
-- 唯一性 = (owner_user_id, idempotency_key)；负载以 canonical JSON 的
-- sha256 摘要落库（payload_sha256，键序 name/note/slides，分隔符紧凑，
-- ensure_ascii=False；slides 保序——顺序影响 project_slides.position）。
-- project_id 外键随项目删除联动（删项目后同键可再建，不悬挂）。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS project_create_idempotency (
    owner_user_id   TEXT        NOT NULL,
    idempotency_key TEXT        NOT NULL,
    payload_sha256  TEXT        NOT NULL,
    project_id      TEXT        NOT NULL
        REFERENCES projects(project_id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_user_id, idempotency_key)
);

COMMENT ON TABLE project_create_idempotency IS
    '项目创建幂等记录：(属主, Idempotency-Key) 唯一；同键同负载重放返回原项目，'
    '同键不同负载 409；与项目行同事务写入，绝不单边落地';
