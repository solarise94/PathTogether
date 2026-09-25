-- =========================================================================== --
-- 0067_slide_asset_identity.sql：slide ID 化重构的 schema 底座
-- （docs/slide-id-refactor-p1-contract-20260925.md §2，逐条对应；
-- 盘点依据 docs/slide-id-refactor-p0-inventory-20260925.md §2.5 / 裁决表
-- R-01~R-20；目标合同 docs/slide-id-storage-refactor-agent-plan-20260925.md §2）。
--
-- 本迁移只做 schema：不加载数据回填、不搬动文件、不调用云服务（合同 §2.5）。
-- 旧行为不变量：slides 既有行经默认值进入 asset_state='legacy' /
-- storage_layout='legacy'（= 待验证历史资产，P1 回填脚本
-- scripts/backfill_slide_asset_state.py 才是唯一把 legacy 推向
-- ready/failed 的入口）；旧读写端点在 P1-B 前不消费 asset_state 门禁。
--
-- 新增/变更一览（§2.1~§2.4）：
--   §2.1 slides 八列扩展（original_filename / storage_layout /
--        storage_relpath / format_ext / asset_state / published_at /
--        deleted_at / accounted_bytes）+ CHECK + 索引；
--        storage_relpath 部分唯一（唯一且不可变，R-20：服务端生成、
--        客户端不可设置、绝不返回浏览器）。
--   §2.2 任务表显式 slide_id：upload_tasks.slide_id（FK slides）+
--        批量表 upload_task_items（(task_id,item_key) PK、slide_id 全局
--        UNIQUE——R-13「同一任务同一逻辑 item」库层唯一约束）；ingestion_jobs
--        / conversion_jobs / baidu_import_items 各加 slide_id + 部分唯一
--        索引；conversion_job_sources 加 source_slide_id（P4 用）。
--   §2.3 关系表 ID 化：新表 share_slides（token×slide_id，R-04：
--        shares.slides JSONB 保留为兼容快照，不再参与授权判定）；
--        slide_view_grants (slide_id,user_id) 部分唯一（R-06：
--        slide_name 保留为历史快照）；project_slides.slide_id +
--        (project_id,slide_id) 部分唯一（R-07：旧 slide 文本列保留快照）；
--        rois/comments/change_log/run_grants/ai_session_principals/
--        annotation_access_events/audit_events 各加 slide_id 列 + 索引
--        （R-08/R-09/R-14：历史文本列不重写成猜测 ID，回填不到可信 ID 的
--        保持 NULL = unresolved）。
--   §2.4 slide_delete_jobs：P5 统一删除任务的持久化载体，schema 先行。
--
-- asset_state 状态机（合同 §4；slide_store.py 模块 docstring 镜像）：
--   allocate_slide → staging ──publish 成功──▶ ready ──request_delete──▶
--   deleting ──清理+结算完成──▶ deleted；
--   staging ──任务失败/取消──▶ failed（staging 清理后行可删或留 failed 证据）；
--   deleting 清理失败：停留 deleting 重试（slide_delete_jobs）；
--   legacy（0067 默认）──盘点/验证通过──▶ ready（layout 仍 legacy，P6 再迁
--   id_bundle）；legacy ──验证失败──▶ failed（保留证据，不可读）。
--   DB asset_state='ready' 是唯一可见性开关；deleting 立即拒绝新读取授权。
--
-- 锁顺序（合同 §5，全仓统一口径，0066 基础上扩展）：
--   pg_advisory_xact_lock(hashtext('slide:' || slide_id))  ← 第一把锁
--     → 任务行锁（upload_tasks / ingestion_jobs / conversion_jobs 行，
--        SELECT ... FOR UPDATE）
--     → slides 行锁 → upload_reservations 行 → upload_user_quotas 行
--     → cos_pool_state 行。
--   'slide:' 前缀与既有 hashtext(job_id) 键空间隔离；已审计无环。
--
-- 幂等：ADD COLUMN IF NOT EXISTS / CREATE [UNIQUE] INDEX IF NOT EXISTS /
-- CREATE TABLE IF NOT EXISTS / FK 用 pg_constraint 判存的 DO 块；对已应用
-- 库重跑 no-op（pg_store 机制：单文件单事务、按 schema_migrations 记录去重）。
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- §2.1 slides 八列扩展：身份与存储分离、资产状态机、发布/删除审计、计费字节
-- --------------------------------------------------------------------------- --
ALTER TABLE slides
    ADD COLUMN IF NOT EXISTS original_filename TEXT,          -- 原始 basename 展示快照（R-19；新资产应用层保证非空）
    ADD COLUMN IF NOT EXISTS storage_layout TEXT NOT NULL DEFAULT 'legacy'
        CHECK (storage_layout IN ('legacy', 'id_bundle')),    -- 只有 resolver 理解两种布局
    ADD COLUMN IF NOT EXISTS storage_relpath TEXT,            -- 服务端生成的包入口相对路径（R-20；唯一且不可变）
    ADD COLUMN IF NOT EXISTS format_ext TEXT,                 -- 实际逻辑格式（白名单小写扩展名），不从展示名推导
    ADD COLUMN IF NOT EXISTS asset_state TEXT NOT NULL DEFAULT 'legacy'
        CHECK (asset_state IN ('staging', 'legacy', 'ready',
                               'deleting', 'deleted', 'failed')),
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,        -- 发布审计时间
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,          -- 删除审计时间
    ADD COLUMN IF NOT EXISTS accounted_bytes BIGINT
        CHECK (accounted_bytes >= 0);                         -- 本地已计费物理字节稳定值；与 ready 发布同事务设置，删除结算用它

COMMENT ON COLUMN slides.asset_state IS
    '资产状态机（合同 §4）：staging/legacy/ready/deleting/deleted/failed；'
    'ready 是唯一可见性开关；legacy=0067 默认的待验证历史资产，回填脚本才迁移';
COMMENT ON COLUMN slides.storage_relpath IS
    '服务端生成的包入口相对路径（objects/<slide_id>/...）；唯一且不可变，'
    '客户端不可设置，绝不返回浏览器（R-20）';
COMMENT ON COLUMN slides.accounted_bytes IS
    '本地已计费物理字节稳定值：与 ready 发布同事务设置；删除清理成功后按其幂等减少 used_bytes（R-12）';

-- storage_relpath 唯一（部分唯一：NULL=未发布/legacy 布局不占用键）
CREATE UNIQUE INDEX IF NOT EXISTS uq_slides_storage_relpath
    ON slides (storage_relpath) WHERE storage_relpath IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_slides_owner_user_id ON slides (owner_user_id);
CREATE INDEX IF NOT EXISTS idx_slides_asset_state ON slides (asset_state);

-- --------------------------------------------------------------------------- --
-- §2.2 任务表显式 slide_id（R-13：ID 不得因刷新/worker 重领而重新分配）
-- --------------------------------------------------------------------------- --
ALTER TABLE upload_tasks ADD COLUMN IF NOT EXISTS slide_id TEXT;

-- FK 判存幂等（PostgreSQL 无 ADD CONSTRAINT IF NOT EXISTS）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'upload_tasks_slide_id_fkey'
          AND conrelid = 'upload_tasks'::regclass
    ) THEN
        ALTER TABLE upload_tasks
            ADD CONSTRAINT upload_tasks_slide_id_fkey
            FOREIGN KEY (slide_id) REFERENCES slides(slide_id);
    END IF;
END $$;

COMMENT ON COLUMN upload_tasks.slide_id IS
    '单切片任务直接绑定的资产 ID（0067 起新增任务写入；批量任务用 upload_task_items）';

-- 批量任务（V1 ZIP）「同一任务、同一逻辑 item」的库层唯一约束；
-- slide_id 全局 UNIQUE = 一个资产只属一个任务项
CREATE TABLE IF NOT EXISTS upload_task_items (
    task_id   TEXT        NOT NULL,                -- upload_tasks.upload_id
    item_key  TEXT        NOT NULL,                -- 任务内逻辑切片键（manifest 项标识）
    slide_id  TEXT        NOT NULL UNIQUE,         -- 全局唯一：一个资产只属一个任务项
    PRIMARY KEY (task_id, item_key)
);
COMMENT ON TABLE upload_task_items IS
    '批量上传任务的逻辑切片→slide_id 绑定（0067；R-13）：重试/恢复复用既有行，'
    '不得因刷新或 worker 重领重新分配 ID';

ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_jobs_slide_id
    ON ingestion_jobs (slide_id) WHERE slide_id IS NOT NULL;

ALTER TABLE conversion_jobs ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversion_jobs_slide_id
    ON conversion_jobs (slide_id) WHERE slide_id IS NOT NULL;

ALTER TABLE conversion_job_sources ADD COLUMN IF NOT EXISTS source_slide_id TEXT;

ALTER TABLE baidu_import_items ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_baidu_import_items_slide_id
    ON baidu_import_items (slide_id) WHERE slide_id IS NOT NULL;

-- --------------------------------------------------------------------------- --
-- §2.3 关系表 ID 化（R-04/R-06/R-07/R-08/R-09/R-14）
-- --------------------------------------------------------------------------- --

-- R-04：分享成员 ID 关系；shares.slides JSONB 保留为兼容快照，不再参与授权判定
CREATE TABLE IF NOT EXISTS share_slides (
    token    TEXT    NOT NULL REFERENCES shares(token) ON DELETE CASCADE,
    slide_id TEXT    NOT NULL REFERENCES slides(slide_id),
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token, slide_id)
);
CREATE INDEX IF NOT EXISTS idx_share_slides_slide ON share_slides (slide_id);
COMMENT ON TABLE share_slides IS
    '分享 token × slide_id 关系（0067；R-04）：可见性判定唯一来源；'
    'shares.slides JSONB 仅兼容输出快照。原文件删除后旧 token 不因同名新上传恢复访问';

-- R-06：slide_view_grants 主关联改 (slide_id, user_id)；slide_name 保留为历史快照
CREATE UNIQUE INDEX IF NOT EXISTS uq_slide_view_grants_slide_id_user
    ON slide_view_grants (slide_id, user_id) WHERE slide_id IS NOT NULL;

-- R-07：project_slides 加 slide_id（旧 slide 文本列保留为兼容快照；
-- 同名不同 ID 可同时入项目）
ALTER TABLE project_slides ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_slides_project_slide_id
    ON project_slides (project_id, slide_id) WHERE slide_id IS NOT NULL;

-- R-08/R-09/R-14：活动查询改 slide_id；历史文本列不重写成猜测 ID，
-- 回填不到可信 ID 的保持 NULL = unresolved（隔离，不猜归属）
ALTER TABLE rois ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE INDEX IF NOT EXISTS idx_rois_slide_id_seq ON rois (slide_id, insert_seq);

ALTER TABLE comments ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE INDEX IF NOT EXISTS idx_comments_slide_id ON comments (slide_id);

ALTER TABLE change_log ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE INDEX IF NOT EXISTS idx_change_log_slide_id_seq ON change_log (slide_id, seq);

ALTER TABLE run_grants ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE INDEX IF NOT EXISTS idx_run_grants_slide_id ON run_grants (slide_id);

ALTER TABLE ai_session_principals ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE INDEX IF NOT EXISTS idx_ai_session_principals_slide_id
    ON ai_session_principals (slide_id);

ALTER TABLE annotation_access_events ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE INDEX IF NOT EXISTS idx_annotation_access_events_slide_id_seq
    ON annotation_access_events (slide_id, seq);

ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS slide_id TEXT;
CREATE INDEX IF NOT EXISTS idx_audit_slide_id ON audit_events (slide_id);

-- --------------------------------------------------------------------------- --
-- §2.4 slide_delete_jobs（P5 统一删除任务，schema 先行）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS slide_delete_jobs (
    job_id       TEXT        PRIMARY KEY,
    slide_id     TEXT        NOT NULL UNIQUE,     -- 一个资产至多一条删除任务
    requested_by TEXT,
    state        TEXT        NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending', 'cleaning', 'done', 'failed')),
    attempts     INTEGER     NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE slide_delete_jobs IS
    '按 slide_id 的幂等删除任务（0067；P5 接线）：物理清理确认成功后一次性'
    '按 accounted_bytes 结算，重复执行/worker 重启不得重复减账';
