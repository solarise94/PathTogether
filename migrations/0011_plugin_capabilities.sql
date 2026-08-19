-- =========================================================================== --
-- 0011_plugin_capabilities.sql：插件能力注册表（docs §4.1，插件能力层 P1）
--
-- 安装/启用时解析 manifest.provides 并登记进 plugin_installations 行内嵌的
-- capabilities 数组（JSONB；登记失败 = 安装失败，fail-closed）。json 后端为
-- shares.json 顶层 plugin_installations 数组元素的同名字段。
--
-- 幂等（ADD COLUMN IF NOT EXISTS，对齐 0001-0010 风格）；ensure_schema 启动期
-- 自动应用。旧行（无该列数据/NULL）读侧一律归一为 []，兼容 0005 起的存量行。
-- =========================================================================== --

ALTER TABLE plugin_installations
    ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN plugin_installations.capabilities IS
    '能力注册表（docs §4.1）：[{name, version, description, parameters, '
    'access_mode, required_permissions, timeout_ms, base_url, enabled}]，'
    '安装时解析 manifest.provides 登记；enabled 与插件启停开关联动';
