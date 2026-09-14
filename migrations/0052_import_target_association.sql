-- =========================================================================== --
-- 0052_import_target_association.sql：导入目标项目关联（W4 C02 / W5 B06）
--
-- conversion_jobs：本地上传在创建转换任务时记下 target_project_id，worker
-- 产物 ready 后幂等关联；失败（项目删除/权限撤销）只记 associate 失败，
-- 不回滚已入库产物。
-- baidu_import_items：入库后的可见切片名与项目关联状态（公开视图不含路径）。
-- =========================================================================== --

ALTER TABLE conversion_jobs
    ADD COLUMN IF NOT EXISTS target_project_id TEXT;

ALTER TABLE conversion_jobs
    ADD COLUMN IF NOT EXISTS project_associate_state TEXT NOT NULL DEFAULT 'not_needed';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'conversion_jobs'::regclass
          AND conname = 'conversion_jobs_project_associate_state_check'
    ) THEN
        ALTER TABLE conversion_jobs ADD CONSTRAINT
            conversion_jobs_project_associate_state_check
            CHECK (project_associate_state IN
                   ('not_needed', 'pending', 'succeeded', 'failed'));
    END IF;
END
$$;

COMMENT ON COLUMN conversion_jobs.target_project_id IS
    '可选：转换完成后幂等加入的目标项目；缺失/无权时 associate=failed，产物保留';

ALTER TABLE baidu_import_items
    ADD COLUMN IF NOT EXISTS slide_name TEXT;

ALTER TABLE baidu_import_items
    ADD COLUMN IF NOT EXISTS project_associate_state TEXT NOT NULL DEFAULT 'not_needed';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baidu_import_items'::regclass
          AND conname = 'baidu_import_items_project_associate_state_check'
    ) THEN
        ALTER TABLE baidu_import_items ADD CONSTRAINT
            baidu_import_items_project_associate_state_check
            CHECK (project_associate_state IN
                   ('not_needed', 'pending', 'succeeded', 'failed'));
    END IF;
END
$$;

COMMENT ON COLUMN baidu_import_items.slide_name IS
    '入库后 Viewer 可见名（native 为源名；convert-required 为 canonical）';
