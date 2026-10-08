SELECT 'upload_tasks', count(*) FROM upload_tasks WHERE state NOT IN ('committed','cancelled','expired','failed')
UNION ALL SELECT 'ingestion_jobs', count(*) FROM ingestion_jobs WHERE state NOT IN ('completed','failed','cancelled','expired')
UNION ALL SELECT 'slide_delete_jobs', count(*) FROM slide_delete_jobs WHERE state NOT IN ('done','failed')
UNION ALL SELECT 'conversion_jobs', count(*) FROM conversion_jobs WHERE state NOT IN ('done','completed','failed','cancelled')
UNION ALL SELECT 'baidu_import_batches', count(*) FROM baidu_import_batches WHERE state NOT IN ('done','completed','failed','cancelled')
UNION ALL SELECT 'producer_imports', count(*) FROM producer_imports WHERE state NOT IN ('done','completed','failed','cancelled')
UNION ALL SELECT 'slides_inflight', count(*) FROM slides WHERE asset_state IN ('staging','deleting');
