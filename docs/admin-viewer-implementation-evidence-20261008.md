# 后台/注册/Viewer 改版 —— 实现与验收证据

日期：2026-10-08。依据：[简化版实施设计](admin-viewer-simplified-20261008.md)。分支 `admin-viewer`，基线为生产线 `registration-antibot` @ 66cfa29c。状态：**已实现、已验收，未部署、未推送。**

## 1. 变更范围

| 部分 | 内容 |
| --- | --- |
| 迁移 | `migrations/0080_admin_viewer.sql`：`users.account_kind`（real/dogfood，带 CHECK）、`users.last_login_at`、`slide_view_grants.expires_at`（NOT NULL；存量行置为迁移时刻，即旧永久授权立即结束）、`projects.parent_project_id`（ON DELETE SET NULL） |
| 用户管理 | 登录成功写最近登录；用户列表 `kind`/`sort` 参数；`POST /api/admin/v1/users/<id>/account-kind` + 审计；后台用户页四列 + 分类筛选 + 排序，原有操作移入「详情」抽屉 |
| 临时查看 | `POST/DELETE /api/admin/v1/slides/<id>/temporary-view`（1 小时，重复开启不续期，结束立即生效并撤销 AI 运行授权、取消运行中的 run）；读门禁只认未到期授权；AI 运行授权到期不晚于临时查看；旧 visibility 端点 410；后台切片页五态 + 行内确认；「查看」打开 `/app?slide=<id>` |
| 注册 | 模式只剩 closed/public；邀请码端点、激活/enrollment 端点 410，`/activate` 重定向登录；已验证未禁用的 pending 用户登录时惰性激活（额度只初始化一次）；邀请码页面、桥方法、权限、模板分支、i18n 文案、`activate.html`、`activate-waiting.js` 删除；`registration_invites` 表保留为历史 |
| Viewer | 单层顶栏新增「搜索」「分享」（按测量折叠，低频控件先折叠）；左栏文件夹/切片堆叠浏览器（悬停抽出预览、点击打开、翻叠、页码记忆、卡片 ⋯ 保留原单片操作）；跨文件夹搜索与同名区分；分享管理移入浮层；「临时查看」虚拟文件夹与到期清屏；`openSlide` 迟到响应丢弃 |
| 后台插件 | `pathtogether-admin` 0.4.16，fileHashes 与 source-policy pin 已更新 |

## 2. 迁移演练

生产 schema-only 副本（停在 0078，今天 0079 演练时导出）+ 种子数据（两条旧永久授权、两名用户、一个项目）：

```
ensure_schema pass 1 ok
ensure_schema pass 2 ok          # 重复执行无变化
[0080_admin_viewer.sql, 0079_registration_antibot_redelivery.sql, 0078_ingestion_direct_class.sql]
users  [('usr_owner','real',None), ('usr_u1','real',None)]
grants [('sld_a', True), ('sld_b', True)]   # 旧永久授权均已结束
nullable [('expires_at','NO')]
check ok: CheckViolation                    # account_kind 非法值被拒
```

## 3. 测试结果（admin-viewer @ 74091d0e 前后）

| 门禁 | 结果 |
| --- | --- |
| pytest 全量（主代理独立执行，b0d8bc0b；之后只改了前端与 e2e 文件） | 2927 passed, 118 skipped, 0 failed |
| vitest `tests/js` | 61 files, 966 passed, 2 skipped |
| 插件哈希/pin/manifest + 注册入口 pytest | 158 passed |
| Playwright 全部 spec | 73 passed, 5 failed——5 项在生产基线 66cfa29c 上同样失败（import-project-upgrade 的 U05/U08 格式目录与百度能力用例、raster-image-compat 的 BMP 上传；基线上该组失败 7 项） |
| 变异检查 | 去掉单片读门禁的到期条件 → 4 个临时查看测试失败；去掉列表查询的到期条件 → 2 个失败 |

## 4. 真实浏览器验收（隔离 e2e 服务器，真实后端 + 后台 iframe）

| 场景 | 结果 |
| --- | --- |
| 文件夹：建子文件夹、翻叠与页码记忆、移动、环检测（界面过滤 + 服务端 409）、删除父文件夹后子文件夹回根、刷新持久 | 通过 |
| 跨文件夹搜索同名切片并定位打开 | 通过 |
| 后台开启临时查看 → Viewer「临时查看」文件夹 → 数据库拨过期 → 缩略图 403、卡片消失 → 后台显示已结束；后台结束查看后读取 403 | 通过 |
| 用户页筛选、最近登录排序（暂无记录排末尾）、标为 Dogfood 持久化、审计记录 | 通过 |
| 注册设置只有 closed/public；`#invites` 深链回落；邀请码 API 410 | 通过 |
| 导入抽屉、分享创建/列表/撤销、标注工具、390px 抽屉 | 通过 |

验收中发现并已修复的缺陷：

1. 后台「结束查看」点击无请求——确认条渲染在表格下方、滚出视口；改为行内确认，并补 UI 测试与 e2e 用例。
2. 「查看」打开 `/?slide=`（首页）——改为 `/app?slide=`（设计稿笔误）。
3. 分享列表恒显示「暂无分享」——`/api/share/list` 返回裸数组而前端读 `data.shares`。**此缺陷在生产基线已存在**，本分支前端兼容两种形态。
4. 删除文件夹弹出两层确认——只保留新确认。
5. 临时查看到期后点击残留卡片显示通用「无权访问」——改为识别到期、移除卡片并提示。

## 5. 截图

均在 `PathTogether/.gate-tmp/shots/`（未入库）：Viewer `new-topbar-1440-slide-open.png`、`app-folder-hover-1440.png`、`app-search-1440.png`、`app-share-1440.png`、`app-root-390.png`；后台 `users-1440.png`、`slides-1440.png`、`settings-1440.png`；live 验收 `live/` 子目录；行内确认 `av-admin-ui-wt/.gate-tmp/shots/def4-fixed-confirm.png`。

## 6. 未做与上线前事项

- 未部署、未推送。上线需要：0080 迁移、PathTogether 镜像、admin 插件包 0.4.16 发布（源策略 pin 有进程内缓存，换包后需重启）。
- Dogfood 名单未提供：分类功能已实现，**未批量标记任何账号**。
- 迁移后所有旧的管理员永久查看授权立即结束，需要时在后台重新开启 1 小时。
- 保持现状、未在本轮处理：测试申请模块（惰性激活后不会再有新申请，历史仍可查看）；上传不带目标文件夹；目录页码只在内存记忆。
- 390px 顶栏的搜索/分享为图标按钮，命中区较小，可后续加大。
