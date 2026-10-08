# 给 Opus 的实现交接

## 可直接复制的任务说明

请在当前仓库实现以下两份文档定义的改版，保留现有工作区改动，先核对真实代码再实施：

1. `docs/admin-viewer-registration-design-20261008.md`
2. `docs/admin-viewer-registration-acceptance-20261008.md`

已确认方向：

- 用户后台区分正式用户与 Dogfood，保留加入时间和最近登录，去掉新增的活跃指标；先标记，不清理真实账号。
- 邀请码功能整个退役，邮箱验证后直接注册，注册仍可关闭；已有用户身份和资产保留。
- Viewer 单层顶栏、左侧文件夹导航与堆叠缩略图；划过只预览、点击才打开；可返回上级；分叠避免长滚动；搜索跨文件夹查找并定位。
- 分享管理收进按钮；保留现有导入、读片、标注、分享和账户能力。
- 后台切片页是用户上传清单与管理员临时查看，不是用户分享权限页；当前管理员开启后 1 小时自动失效，也可提前结束。

先阅读主设计中的“代码现状”和“已确认/建议”边界。按分批顺序实现数据库、store、API、AdminBridge、UI及测试，不能只把原型 HTML 嵌入正式应用。无需部署生产环境；完成后报告迁移方式、测试证据、变更范围及未完成项。

## 原型与图像

- [Viewer 可交互原型](design-assets/admin-viewer-20261008/viewer-folders.html)
- [后台可交互原型](design-assets/admin-viewer-20261008/admin-temporary-view.html)
- [Viewer 截图](design-assets/admin-viewer-20261008/viewer-folders.png)
- [后台截图](design-assets/admin-viewer-20261008/admin-temporary-view.png)

可交互 HTML 可用浏览器本地打开，页面内有 sandbox iframe。它们只演示交互：示例用户、图片和租约均非生产数据。后台开启/结束与一小时到期是浏览器模拟，不是已有后端能力。

最终版本是 `viewer-folders` 和 `admin-temporary-view`。不要回到旧的底部切片匣、无目录列表、文件按钮或后台“加入工作区”文案。

## 特别容易实现错的地方

1. 后台清单现有 legacy 文件名分支会遗漏无 legacy_filename 的新资产。必须按 slide_id 贯通，不只为旧文件加 expires_at。
2. 旧直授是永久授权；临时查看不能只改 UI 倒计时，不能让旧 API 和底层角色短路成为旁路。
3. `authorize_read` 的 admin 全量分支有本地免认证用途；认证 owner 当前由 app 包装按 UID 走隔离。调整前审计所有调用点，保留合理本地开发能力，不扩大生产 owner 的读取权限。
4. 权限到期覆盖 thumbnail/tile/crop、搜索、缓存和派生 token；审查 AI/插件机器通道。默认临时 view 不自动授予下载、写标注、分享或 AI 权限。
5. 原项目多对多关联、归档和分享不能因为 UI 改叫文件夹就被破坏。目录移动不移动物理切片文件。
6. 删除邀请码时注意 public 注册与邮件共用代码。不要批量激活未验证/已禁用账号，也不要让重试重复初始化额度。
7. 后台 iframe 通过 AdminBridge 工作，有 manifest 文件哈希和来源 pin；不要绕过框架直接 fetch 或关闭校验。
8. 本轮交付尚未改业务代码或生产数据。已提供的代码证据对应 HEAD `19b6438e` 加工作区修改，行号/内容可能继续变化。

## 工作区交接边界

本次新增材料是三份 Markdown，以及 `docs/design-assets/admin-viewer-20261008/` 的两份 HTML 和两张 PNG。其余原有修改均不属于本次文稿交付。

开始实现前重新检查 `git status` 和已有 diff。已见的修改包括注册 worker/模板、后台插件 manifest/UI、source-policy 和测试，不能回退它们。不要为方便操作生产数据库；仓库的 Python/E2E 测试有隔离 PostgreSQL。

## 完成定义

主设计的功能与配套案例通过，保留已有关键能力；迁移可重复执行；临时权限真实失效；相关回归有证据；前后端都移除了邀请码业务入口。只做原型、只改按钮名称或只运行模拟测试不能宣称完成。
