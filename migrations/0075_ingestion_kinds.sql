-- 0075：ingestion 任务形态扩展（COS 统一上传 U2，
-- docs/cos-only-upload-agent-plan-20260928.md §3.2/§U2）。
--
-- /api/ingestions 自本迁移起接受全部用户上传形态：
--   kind='native'      原生单文件（既有语义，创建即预分配 slide_id）
--   kind='zip'         归档包（多逻辑切片；创建不预分配 job 级 slide——
--                      产物资产归 ingestion_job_items 逐 item 绑定，
--                      与 upload_task_items（0067）同构：重试/恢复按
--                      (job_id,item_key) 复用 slide_id，绝不重新分配）
--   kind='conversion'  convert-required 源（KFB/KFBF；创建不预分配 job 级
--                      slide——产物 slide_id 由 conversion_jobs.create_job
--                      预分配，源责任经 job.conversion_job_id 关联）
--
-- 存量行 kind 默认 'native'（升级前任务全部为原生单文件）。
-- sha256_expected：可选的整对象 sha256（创建时声明，下载校验后比对，
-- 不符=确定性失败 hash_mismatch；与 V2 commit 的同名参数对齐）。

ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS kind TEXT
    NOT NULL DEFAULT 'native';
ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS sha256_expected TEXT;
ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS conversion_job_id TEXT;

COMMENT ON COLUMN ingestion_jobs.kind IS
    '任务形态：native=原生单文件（创建即预分配 slide_id）；zip=归档包'
    '（产物经 ingestion_job_items 逐 item 绑定）；conversion=convert-required'
    ' 源（产物 slide_id 归 conversion_jobs，本表经 conversion_job_id 关联）';
COMMENT ON COLUMN ingestion_jobs.sha256_expected IS
    '可选整对象 sha256 声明（64 hex；下载校验后比对，不符=确定性失败）';
COMMENT ON COLUMN ingestion_jobs.conversion_job_id IS
    'conversion 形态的转换任务关联（受理时写入；幂等恢复按 upload_id=job_id 复用）';

-- 批量绑定（0067 upload_task_items 同构）。slide_id 全局 UNIQUE：一个资产
-- 只属一个任务项；state/fail_code 是 item 级结果证据（异步任务无法像 V1
-- 请求内返回 failures，须持久化）。
CREATE TABLE IF NOT EXISTS ingestion_job_items (
    job_id     TEXT        NOT NULL,                -- ingestion_jobs.job_id
    item_key   TEXT        NOT NULL,                -- 任务内逻辑切片键（zip 内相对路径）
    slide_id   TEXT        NOT NULL UNIQUE,         -- 全局唯一：一个资产只属一个任务项
    state      TEXT        NOT NULL DEFAULT 'pending',  -- pending|published|failed
    fail_code  TEXT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, item_key)
);
COMMENT ON TABLE ingestion_job_items IS
    'ingestion 批量任务的逻辑切片→slide_id 绑定与 item 级结果（0075；镜像'
    ' upload_task_items 的 R-13 语义：重试/恢复复用既有行，不重新分配 ID）';
