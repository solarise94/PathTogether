-- 0078：直传类别声明（先转换后上传阶段 1，
-- docs/slide-tools/upload-convert-first-phase1.md §3）。
--
-- /api/ingestions 增加可选字段 direct_class（浏览器端嗅探文件头后随创建
-- 声明）：
--   'ome-tiff'                        TIFF 且 ImageDescription 含 OME-XML；
--   'converter-bigtiff'               本机转换工具导出的经典 BigTIFF
--                                     （描述 JSON 带转换器来源标记）；
--   'legacy-direct'                   无头级声明的普通直传（暂时的
--                                     direct-temporary 格式）；
--   'unconverted-variant:svs-jp2k'    JPEG2000 编码 SVS 的声明例外
--                                     （本阶段 .svs 直传关闭，仅此变体
--                                     凭声明放行）。
--
-- 摄取 worker 在 open_slide 试开之前按 upload_direct_class 核验声明与
-- 实际字节相符（不符 → validation_failed 族失败，错误码
-- convert_in_browser）。NULL = 未声明（存量任务/店内直建）：不做头级
-- 核验，行为与升级前一致。已入库切片的读取路径不受影响。
--
-- 存量行 direct_class 默认 NULL；重跑 no-op。

ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS direct_class TEXT;

COMMENT ON COLUMN ingestion_jobs.direct_class IS
    '可选直传类别声明（ome-tiff|converter-bigtiff|legacy-direct|'
    'unconverted-variant:svs-jp2k）；创建时声明，worker 在 open_slide 前'
    '按 upload_direct_class 核验；NULL=未声明（不做头级核验）';
