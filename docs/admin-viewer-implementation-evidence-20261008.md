# 后台/注册/Viewer 改版 —— 实现与验收证据

日期：2026-10-08，最后更新：2026-10-09。依据：[简化版实施设计](admin-viewer-simplified-20261008.md)。分支 `admin-viewer`，基线为生产线 `registration-antibot` @ 66cfa29c。状态：**已推送、已部署；线上代码 826fc6c7，最新动画渲染优化上线见 §15；未解决的 AI 阻塞见 §11。**

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

- 2026-10-09 已完成推送、0080 迁移、PathTogether 镜像部署、admin 插件包 0.4.16 发布与服务重启（详见 §9）。
- Dogfood 名单未提供：分类功能已实现，**未批量标记任何账号**。
- 迁移后所有旧的管理员永久查看授权立即结束，需要时在后台重新开启 1 小时。
- 保持现状、未在本轮处理：测试申请模块（惰性激活后不会再有新申请，历史仍可查看）；上传不带目标文件夹；目录页码只在内存记忆。
- 390px 顶栏的搜索/分享为图标按钮，命中区较小，可后续加大。

## 7. 独立审查第二轮（2026-10-08 晚）

审查方对 46fb4a5e 做了代码审查与隔离 dogfood，并提交测试债清理 `dac333d4`（只改测试与测试入口配置：`pytest.ini` testpaths、`vitest.config.ts` include、`package.json` test:js；6 个业务缺陷以严格预期失败标记交付）。主代理核对提交范围后合入，6 项全部修复，修复时只删除预期失败标记、不改断言。

| 缺陷 | 根因 | 修复 |
| --- | --- | --- |
| 结束管理员查看误撤销上传者 AI 授权（P1） | 调用了切片删除用的全量撤销函数 | `_revoke_run_grants_for_slide_id` 增加 `created_by_user_id` 过滤，结束查看只撤本管理员派生的授权；删除路径语义不变（6387359f） |
| 到期后 Viewer 不自动清屏 | 到期值只在列表接口（epoch 秒），打开时从 info 读；秒按毫秒解析；深链时 info 先于列表返回 | 统一换算，按列表值设定计时器并在每次列表加载后重新设定；到期与 403 两条路径都回到「未打开切片」初始状态（c4b7b3db、d641e919） |
| 搜索定位页码错误 | 只在切片内算页码，渲染却是文件夹+切片混排 | 渲染与定位共用 `fbOrderedEntries` + `fbPackPages` |
| 文件夹多时每页只剩一项 | 页容量扣除了整个目录所有文件夹的高度 | 每页按该页实际条目的真实高度装箱（≤8） |
| 分享选择器确认后浮层被同一次点击关闭 | 程序化打开发生在同一次冒泡中，外部点击处理器随即关闭 | 一次性跳过下一次外部点击，用宏任务兜底复位 |
| 仅有 ID 的资产无法在分享页保存标注（**生产已存在**） | `add_roi` 用 legacy 名快照判成员，新资产名为空 | 已知 slide_id 时按 `share_slides(token, slide_id)` 判成员；名称快照回落 original_filename（451d8a15） |

附带：`deleteSlide` 与临时查看到期共用新的无切片基线函数，同时修好删除后工具栏残留的同类问题。

门禁（admin-viewer @ f27794f3，主代理独立执行）：pytest 全量 2928 passed / 113 skipped / 0 failed；vitest 967 passed / 2 skipped；Playwright 全部 80 passed / 0 failed（原先 5 个基线失败已由测试债清理恢复为有效用例）；仓库内不再有预期失败标记。

注意：`add_roi` 的成员判定改走 `share_slides` 后，从未回填 `share_slides` 的旧分享写入会被拒——与读路径现状一致（读路径早已只认 `share_slides`）。

## 8. 第三轮复核（2026-10-09）

- 分页换页时，新页首张切片仍按 28px 条带计价（换页前算的 `cost` 未重算），短侧栏下第二页溢出（200px 可用、实占 272px）。修复：换页后按整卡 104px 重算。审查方回归补丁原样入库。
- 由此暴露的深链问题：`/app?slide=<id>` 打开的切片不保证在侧栏当前叠（此前靠首页溢出「碰巧」可见，全量 E2E 中前序用例多传切片时失败）。修复：深链在列表加载后调用与搜索相同的 `fbLocateSlide`，补 vitest 回归。两条新回归在修复前均失败。
- pytest 计数：全量命令带 `--ignore=tests/test_e2e_pg_reap.py --ignore=tests/e2e`，前者正好 3 项（收集 3044 = 3041 + 3）；已单独执行 `tests/test_e2e_pg_reap.py`：3 passed。本轮只改前端，Python 全量结果不变。
- 门禁：vitest 969 passed / 2 skipped；Playwright 全部 80 passed，连续两次。

## 9. 生产发布与真实 dogfood（2026-10-09）

用户明确授权 commit、推送、部署与部署后 dogfood。实际生产基线是注册 UI
版本 `6ce10281`（镜像 `suite-20261008-registration-ui`），已确认它是本分支
祖先；没有从落后的 `slide-id-refactor` 构建，也没有覆盖远端 main。

### 发布结果

| 项目 | 结果 |
| --- | --- |
| Git | `origin/admin-viewer` 已推送，包含原发布 `0f8e510b` 和本轮补修 `3317c2d1`、`d53bf495` |
| 线上代码 | `d53bf495d934f677e5347fc6cd5d6536f1de8df8` |
| 最终切换时间 | 2026-10-09 08:44:22（北京时间） |
| 镜像 | `localhost/pathtogether-demo:suite-20261009-admin-viewer-ai` |
| 镜像 ID | `69b914db27572237dc3788425e23053410ca0843b4f0906bf4d99416c8a479d9` |
| 迁移 | `0080_admin_viewer.sql` 已应用；生产原有 3 条永久管理员查看授权已结束 |
| Admin 插件 | `pathtogether-admin` 0.4.16；pin `f38122ba48c9f19fd73724aeaa6f689cc47bc2eaec579f4023fc83f3e7980dfa`；重启后 iframe 握手成功 |
| 分享部署 | 主进程使用已有 `share_server:combined_app`；`SHARE_BASE_URL=https://histopilot.cn` |

每次切换前都先将只读生产快照恢复到独立 PostgreSQL，替换可写挂载，关闭后台
worker，再运行候选镜像。0080 重复执行不改已结束授权；验收前后生产迁移记录
一致。切换前后两次检查所有导入、上传、转换和删除队列为空，停写后再次备份。
依赖沿用已验证的生产镜像，清除旧 `/app` 内容后复制 Git archive；镜像内 343
个运行文件逐一与提交核对，已删除的邀请码页面和脚本不会因基础镜像继承而残留。

备份及可回滚容器保留在 homepc；最终发布目录
`/home/solarise/releases/suite-20261009-admin-viewer-ai`（0700），数据库 dump 和
环境文件为私有文件，不入 Git。迁移 0080 对旧永久授权写入器并非完全向后兼容，
回滚说明见 [部署说明](../deploy/admin-viewer/README.md)。

### dogfood 新发现并修复的两个问题

1. **公网分享不可打开（3317c2d1）**：生产没有配置 `SHARE_BASE_URL`，生成
   `localhost:38000` 链接；同时入口只启动 `app:app`，没有挂载 `/s/*` 分享
   应用。改用已有合并 WSGI 入口并设置公开 HTTPS 分享地址。原有 ID-only 分享
   标注回归现在加载 `docker_entry.sh` 实际指定的 WSGI 对象，修复前真实返回
   404、修复后成功；同时检查分享健康和匿名访问主站 API 仍返回 401。
2. **新切片 AI 会话列表 403（d53bf495）**：实际插件按稳定 ID 查询
   `/api/ai/sessions?slide=sld_…`，列表后端却按旧文件名查归属。按现有插件与
   sidecar 的索引键契约解析 ID/冻结别名，再走统一资产读取门禁；未知 ID 不
   回退名称猜测，并继续按当前 user_id 过滤会话。回归覆盖同名不同用户资产、
   未授权用户/管理员、临时授权结束和切片删除；修复前属主读也返回 403，修复
   后正确放行属主而拒绝其他主体。

### 生产浏览器验证

通过公网 HTTPS、真实 Chromium、真实后端及真实 COS 上传进行验证，没有 mock
业务请求。使用两名独立 Dogfood 测试账号和合成图片，不查看真实用户切片。
普通账号经实际登录表单进入；管理员浏览器会话由已授权 SSH 运维会话临时签发，
没有重置真实 owner 密码。完成后移除本地凭据和浏览器 session 文件。

| 场景 | 结果 |
| --- | --- |
| 注册入口 | 邮箱字段、无邀请码；未发送实际验证邮件或绕过生产 CAPTCHA |
| 用户管理 | Dogfood/正式分类切换持久化；最近登录记录与排序；普通用户访问后台 403 |
| 上传与查看 | 3 次合成调色板 BMP 真实 COS 直传及入库；缩略图、瓦片像素和深链出图 |
| Viewer | 1440/1280/1024/900px 搜索分享入口；嵌套文件夹、返回上级、跨文件夹搜索、短窗口逐页不溢出；悬停抽出真实缩略图 |
| 文件夹删除 | 只确认一次；子文件夹回根；切片仍可读 |
| 分享 | 选择器确认不关闭分享浮层；创建与列表；匿名公网分享出图；鼠标画箭头后保存 200，刷新可回读；撤销后匿名链接拒绝 |
| 临时查看 | 默认约 3600 秒；重复开启不续期；后台查看打开 `/app`；行内提前结束后管理员 403、上传者 200 |
| 自动到期 | 只将专用 dogfood 授权缩短为 20 秒，先确认画面已打开，再观察自动清屏；到期后列表移除、缩略图 403。没有修改全局 1 小时时限 |
| AI 列表 | 最终版本真实插件请求均为 200；其他账号 403；管理员仅在临时授权有效时 200，结束后 403；上传者不受影响；删除资产后 403 |
| 运行状态 | 测试中无未捕获 JS 异常；最终容器日志无 Traceback、ERROR 或 Flask 异常 |

两域 `/healthz`、`/s/healthz` 均正常。隔离候选的全部 94 个静态文件 SHA-256
验证通过，`.cn` 公网 94 项也全部匹配。`.com` 从 homepc 下载全量资源过慢，
停止该批量检查，改从工作站验证主站/分享健康与 `app.js`、`share.js`、
`style.css` 的完整 SHA-256，均通过（1.4–3.8 秒/项）；不将其记作 `.com`
全量资源验证通过。两次后端补修没有改变静态资源。

本轮针对修复执行的回归：分享/临时授权/访问控制/栅格查看/协作 **93 passed**；
worker 入口相关 **58 passed**；AI 会话/代理/owner 隔离/临时授权/分享
**58 passed**；最后补充删除状态断言后 AI 会话模块 **9 passed**。分享入口和
AI ID 列表两条回归均确认修复前失败、修复后通过。本轮没有重复运行所有全量
pytest/vitest/Playwright，原发布基线全量结果见 §7–8。

没有执行付费模型推理，也没有验证真实邮箱收件；这些不在本次生产 dogfood 的
已通过范围内。24 位 BMP 按现行规则先本机转换，本次直传使用受支持的调色板
BMP 变体，不把转换提示误记为上传失败。

### 清理与证据

- 共撤销 **4 个**测试分享，删除 **3 张**合成切片和 **8 个**剩余测试文件夹
  （另有 1 个父文件夹在删除场景中删除）。两名账号再次禁用，保留 Dogfood 标记
  与审计记录；旧登录会话失效。未批量标记或删除其他账号。
- 数据库最终检查：两账号均 disabled/dogfood 且有最近登录时间；3 张切片均为
  deleted；有效测试分享和管理员查看授权均为 0。
- 工作站证据目录：`PathTogether/.gate-tmp/av-wt/.gate-tmp/deploy-admin-viewer-20261009/`。
  包含浏览器检查结果 JSON、脚本、`viewer-hover.png`、`share-annotation.png`、
  `temporary-expired.png`、`viewer-ai.png`；未将凭据、截图或数据库副本提交。

## 10. 第四轮（2026-10-09，初次验收时未部署；后续上线见 §11）

依据 [第四轮合同](admin-viewer-round4-20261009.md)。

| 项 | 结果 |
| --- | --- |
| 抽出卡片被画布遮住 | 抽出层改为挂在 body 上的固定定位层，浮在画布之前，命中区不变 |
| 顶栏按钮太小 | 搜索、分享（三节点图标）、矩形、箭头、描图改为 34px 大图标按钮，无文字，悬停提示与无障碍名称保留 |
| 「⋯」菜单被 AI 面板压住 | 顶栏层级高于 AI 面板；菜单改为不透明 |
| AI 面板 | 标题栏拖动、右下角缩放，限制在切片视框内，按用户记住，双击标题栏复位；≤768px 不可拖 |
| 概览用户数 | 用户总数与 AI access 只计正式用户 |
| 用户表 | 恢复余额列，新增研究数据（已授权/未授权/已撤回） |
| 测试申请 | 后台页面、桥方法、服务端路由下线（410），`test_application_store.py` 删除，表保留 |
| 用户反馈 | 账户弹层与侧栏「反馈问题」；客户端记录最近 300 条/15 分钟操作；`POST /api/feedback` 落库并入队邮件给管理员通知邮箱；迁移 0081 |

门禁（admin-viewer 合并头，主代理执行）：pytest 全量（不忽略任何文件）2938 passed / 0 failed；vitest 1000 passed；Playwright 83 passed（含新增 `user-feedback.spec.ts`：真实后端 202，附带记录无查询串、无输入内容）。0081 在生产 schema 副本上演练两次通过，邮件 purpose 约束保留全部旧值。

附带修复：生产热修 d53bf495 后 `test_ai_credentials::test_no_auth_full_compat` 失败——夹具只放文件不登记资产；补登记切片行（会话列表按资产走读门禁是正确行为）。

上线前注意：
- admin 插件需发布 0.4.17（0.4.16 已在生产）。
- 反馈邮件发往 `REGISTRATION_ADMIN_EMAIL`（公开注册前置条件已要求配置）。服务端附带的应用版本读 `APP_REVISION` 环境变量，当前部署未注入，需在部署环境中加入，否则该字段为空。
- AI 面板拖动/缩放在 E2E 环境中以注入官方面板结构验证（E2E 未启用 HistoPilot 插件），真实插件下需目视确认一次。


## 11. AI 标题栏与移除「添加视野」上线（2026-10-09）

**状态：已推送并上线；UI 与权限 dogfood 通过，真实 AI 完整对话未通过。**
本节不把真实插件 UI 回归中的模拟 AI 响应计作生产推理成功。

### 发布内容与验证

- `bf5ab139`：第四轮 review 修复、AI 标题切换/新对话/折叠选项 UI。
  Review 与原全量测试结果见 [review 记录](review-round4-ai-title-20261009.md)。
- `5084cb38`：移除真实插件插入的 `#ai-attach-view-btn`，保留原有选区附件行为。
  暂不在标记中增加新入口。真实插件 UI 门禁重新运行 **5 passed**。
- `014efdc0`：生产 dogfood 发现新上传的 ID-only 切片在 run grant 复查中被
  `creator_not_allowed` 拒绝；复查改用 grant 的稳定 slide_id。权限仍校验
  创建者有效、资产可读/未归档、拥有或当前 annotate 协作授权，owner 临时查看
  结束后不能继续使用。3 个新增回归在修复前失败；最终 4 个新增回归覆盖上传者
  user/owner、同名不同资产、禁用/删除、view-only 与撤销分享、结束临时查看。
  相关门禁先 **104 passed**，最终全部 `test_ai*.py` 与临时查看门禁 **252 passed**。
  这次小补丁没有重跑此前已通过的完整 pytest/vitest/默认 Playwright 全量。
- 最终生产镜像：`localhost/pathtogether-demo:suite-20261009-ai-grant`；
  `APP_REVISION=014efdc082dd7371f0265e4d9e97860e85e1b3f2`。
  迁移 `0081_user_feedback.sql` 已应用；admin 插件 **0.4.17**，切换后已重启。
  HistoPilot sidecar 镜像及插件保持原版本。
- 两次候选发布均从只读生产快照恢复到隔离数据库；全部写目录隔离、后台 worker
  关闭，重复迁移通过。镜像逐文件 hash 匹配对应 Git 内容，96 个静态文件在隔离
  候选服务校验通过。生产配置保留，原容器与切换前数据库备份保留以供回滚。
- 两个公网域名 `/healthz`、`/s/healthz` 均通过；`app.js`、`style.css`、
  `ai-panel-chrome.js`、`feedback-recorder.js` 与本地发布源的 SHA256 一致。
  从服务器下载所有公网静态文件的检查过慢，停止该只读检查后改为关键文件校验；
  不声称已完成两域名全部 96 个文件的公网校验。

### 真实浏览器 dogfood 已通过

使用新建并标记 Dogfood 的 uploader/visitor 账号和一张合成 BMP，全程未 mock
生产 API，未修改真实用户账号或关闭注册验证。

1. 真实上传、深链打开、实际画布像素和缩略图正常。
2. 真实安装的 AI 插件使用新标题栏；「添加视野」在初始化、刷新、移动端均不存在。
3. 新建草稿、折叠设置不启动模型；等待草稿保存完成后刷新可恢复文字。
4. 面板拖动、调整大小、刷新恢复尺寸；390px 手机选项不越界；手机切回桌面可拖动。
5. 普通用户不能访问后台或其他账号的切片会话。admin 0.4.17 正常握手，测试申请
   入口已移除，Dogfood 筛选可见测试账号。
6. 开启/结束 admin 临时查看正确改变会话列表访问权限，上传者访问保持有效。
7. 匿名分享页面与 ID-only 切片的 annotations 读端点正常；无 JavaScript 异常。

### 尚未解决的生产问题（需 HistoPilot 后端/插件后续修复）

**P1 — 新切片的真实 AI 开跑仍失败，历史详情也被拒绝。**
先修复 PT run grant 后，同一测试账号的请求成功创建了真实会话，但 SSE 随后返回
`agent_error: 读片助手异常：切片不存在`，会话状态为 `error`，未完成模型回复。
从内部可信接口读取该测试会话：记录的 `slide` 为显示文件名，`GET /session/:id`
的响应没有 `slide_id`；PT 用户侧详情接口因此按安全规则返回 403。
当前 HistoPilot 源码 `src/server.ts` 的 `handleRun` 解析了 `body.slide_id`，但传给
runner 的 `effectiveConfig` 未注入它；详情响应也漏了 `d.slide_id`。
这些是源码定位线索，需要在 HistoPilot 仓补齐真实跨服务回归后发布；不能通过
放宽 PT 按文件名授权来绕过。**本轮不宣称真实回复或真实历史切换验收通过。**
失败会话已结束，无后台测试 run 继续执行。未实际发送反馈邮件。

**P2 — 输入草稿后立即刷新，最后一次输入可能丢失。**
真实插件按 300ms debounce 保存草稿，没有刷新前同步落盘；一次快速操作重现了
空白恢复，等待持久化完成后重复验证正常。新 UI 沿用原插件草稿状态机，本次未
绕过它另建一套保存机制。后续应在插件生命周期中补刷新/关闭前落盘及对应回归。

### 清理与证据

测试分享已撤销，合成切片已删除，两名账号已禁用并保留 Dogfood 标记与审计记录；
数据库复核无仍存活的测试切片/有效分享。失败 AI 会话保留诊断记录。
临时登录凭据和 owner cookie 文件在检查后移除。

本地证据：`.gate-tmp/deploy-ai-title-20261009/`，包括 `results.json`（明确记录
真实 AI 失败）、`rest-results.json`（其余流程与清理）、`public-check.log`、
`cleanup.log`、发布日志、`desktop.png`、`mobile.png`。
服务器发布/回滚目录：`/home/solarise/releases/suite-20261009-ai-title` 与
`/home/solarise/releases/suite-20261009-ai-grant`。私有凭据、数据库副本与截图不进 Git。


## 12. 恢复浮卡草案与控件可读性（2026-10-09）

依据用户提供的当前界面截图与原 `viewer-folders.html` 草案，运行版本
`a6990103668b66fccc011e9e8fa2711b135fc8db` 已推送并上线（UI 主改动 c9b43ff8，
线上 dogfood 发现的两处补丁 a6990103）。

- 悬停浮卡从原叠位置右移并倾斜，原卡暂时隐藏，避免侧边重复显示两张卡。
  body 浮层继续保证不被侧栏裁切；不透明背景、阴影、180ms 抽出动画。
  保留左侧连续划选命中带、浮卡单片菜单及键盘入口；减弱动态效果时去除动画。
- 增加短悬停等待，避免预览在鼠标按下前覆盖直接点击的目标。焦点预览只响应
  键盘 `focus-visible`。滚动不会取消尚未出现的首个预览；已出现的预览会收回。
- 空态主按钮统一为「上传切片」/「Upload slides」，仍打开原导入抽屉。
- 桌面 1:1 控件至少 64×40px，倍率显示不被 flex 压缩；手机继续使用独立的
  抽屉和底部工具栏，低频入口放在更多菜单，触控命中区至少 44px。
- 翻页按钮增加背景和边框；页脚两列图标按钮，反馈使用强调色，密码与管理入口
  使用更短的标签，完整无障碍名称保留。退出入口不再把长用户名写入按钮，
  用户名保留在提示和无障碍名称中。
- 工作台 viewport 改用 overflow:clip，避免聚焦/scrollIntoView 把整个布局
  横向挪动。弹窗自身的滚动区域不受影响。中文 Reset 改为「重置视图」。

线上 dogfood 额外发现并修复：

- 手机侧栏遮罩盖住反馈弹窗，关闭按钮无法点中；密码、邮箱、数据共享使用相同
  弹窗结构，也受影响。四个入口统一复用现有收起抽屉方法后打开弹窗。
- 切片菜单锚点没有 id，外部点击监听拼出非法 `#` 选择器，抛异常且菜单未关闭。
  改用 DOM contains 判断菜单/锚点内部点击，不再依赖 id。
- 两项均先用真实 Chromium 回归复现失败，再验证修复通过。第一次生产检查的
  错误记录仍保存在 initial-results.json / initial-mobile-results.json；没有抹去失败。

最终代码验证：Vitest **1003 passed，2 existing skips**；完整 Playwright
**85 passed，0 failed**。其中顶栏/侧栏专项 **21 passed**，包含浮卡单份显示、
菜单外部点击、原位恢复、减少动效、控件尺寸、页脚图标、窗口切换及四个手机弹窗。
旧上传文案的断言已同步到本次需求。没有重跑未改动的 Python 测试。

发布前在隔离生产快照上验证候选镜像，96 个静态文件内容校验通过。没有新迁移，
后台插件仍为 0.4.17。发布使用原配置、备份和可回滚容器，最终 APP_REVISION 与
上述运行版本一致。两公网域名健康检查及关键静态文件 SHA256 校验通过。

界面审阅截图：`.gate-tmp/viewer-polish-20261009/desktop.png`、`mobile.png`，使用
生产模板/CSS/JS 与测试数据；缩略图复用原草案内的示例图，不是用户切片。
生产 dogfood、清理记录与截图保存在同目录；不将凭据或生产数据提交到 Git。

手机线上实测使用独立 390×844、isMobile/hasTouch 的 Chromium 上下文与真实登录：
侧栏点击打开、44px 页脚按钮、反馈/密码/邮箱/数据共享四个弹窗的打开和关闭、
底部更多中的完整 1:1 控件均通过，无 JavaScript 错误；未提交反馈或修改账号资料。
最初桌面切到手机的测试辅助函数读取了断点切换前的 aria-expanded，已改为等待
手机状态就绪再操作；该脚本问题与实际复现的遮罩问题分开记录。

发布目录：`/home/solarise/releases/suite-20261009-viewer-polish` 和
`/home/solarise/releases/suite-20261009-viewer-dialogs`（最终版本），后者保留可回滚
前一版的容器、备份、静态校验与发布日志。公网复核最终以 curl 验证两域名全部
关键文件通过；第一次 Python urllib 请求 .com 返回过一次 403，curl 与实际
浏览器路径正常，证据未将这次网络检查写成通过。

桌面线上复测：真实上传 8 张合成 BMP，连续划过 4 张卡时仅浮出一张且不打开切片、
不横移页面，控件尺寸与文案正确。补丁后第一次固定坐标点击没有收起菜单，随后
尝试点空态卡边缘被真实画布拦截；错误证据分别留在 postfix-fast-click-results.json
和 postfix-empty-target-results.json。不能据此确认另一个产品缺陷：改用可操作的
真实 canvas 元素定位后，单张新合成 BMP 的完整流程通过——菜单打开/外部关闭、
点击浮卡打开切片、认证后页脚图标、1280/1024/900px 工具栏与 390px 手机切换、
手机更多菜单均正常，无 JavaScript 错误。真实 canvas 回归也覆盖空态/已打开切片，
单独运行 **1 passed**；这是上述全量 85 项之外新增的测试，不混报全量计数。

数据库复核：本轮两个账号均保留 Dogfood 标记且已禁用，所有合成测试切片均删除，
没有有效测试分享。未运行 AI 推理、未发送反馈邮件，未触碰真实用户资料。
`cleanup.log` 保存聚合核验结果；临时凭据文件已移除。截图与完整失败/通过日志留在
`.gate-tmp/viewer-polish-20261009/`。§11 的既有 AI 后端阻塞不在本轮 UI 修复范围内。


## 13. 连续联动选片动画（2026-10-09）

用户反馈：浮卡逐张重建且挡住下一张，鼠标沿卡片中部下移会停在上一张或跳过一张。
先检索 GitHub 并阅读 [Motion Primitives Dock 源码](https://github.com/ibelick/motion-primitives/blob/main/components/core/dock.tsx)：
其共享鼠标坐标、按距离调整相邻项的方式适合本交互。本实现采用这一交互思路，
在现有原生 JS/CSS 中实现，不引入 React / Motion 运行依赖，也没有复制组件源码。

运行代码提交：`3b2b7fb6ea771a7151fb2421c9d4669812be24a2`。

- 每次进入一叠时为当前页建立可复用浮卡（最多 8 张）；上下划选不重建 DOM。
  以连续距离曲线联动当前及相邻卡片的展开、位移与轻微倾斜；rAF 合并写入，
  100ms CSS 插值衔接前后位置，不在每张之间重新等待 hover 延迟。
- 命中读取原始、固定的条带位置。即使浮卡覆盖下一张，鼠标纵坐标仍能选中
  正确条目；点击再次按位置解析，避免点击过渡中的旧浮层打开错误切片。
  横向移出侧栏进入完整预览时保留当前项，方便点击预览及其菜单。
- 浮层仍挂 body，限定在切片区与视口内；不覆盖分页和页脚。触屏直接打开，
  键盘入口保留；减少动态效果时取消插值/旋转。离开、Esc、翻页、重渲、收起、
  失焦和窗口变化均清理浮层。点击后抑制同坐标的自动重新弹出。

回归证据：在旧运行代码上执行同一浏览器划选测试，鼠标已到第二张，
`.fb-hit.extracted` 仍是第一张（fan-0.svs），测试失败；新实现通过。
测试从卡片中部覆盖区向下、反向逐张划过，检查顺序、节点复用、相邻联动、
悬停不打开切片、按坐标点击正确切片，以及离开/分页/侧栏收起清理。
最终 Vitest **1003 passed，2 existing skips**，完整 Playwright **87 passed**。
首轮完整浏览器运行有一项测试误写旧分页按钮 id，修正后完整重跑通过。
未重跑无改动的 Python 测试。

示例页面录屏：`.gate-tmp/fan-20261009/slide-fan-demo.mp4`；截图 `fan-desktop.png`。
录屏使用真实模板/JS/CSS 与虚构切片信息，缩略图沿用原草案示例图。

发布与线上验收：已提交推送并上线，APP_REVISION 为上述代码提交。先在隔离生产
快照验收候选镜像，96 个静态文件内容一致；新建备份并保留上一容器用于回滚。
没有迁移/插件/后端行为变更。两公网域名健康检查通过，app.js/style.css SHA256
与提交内容一致。发布目录 `/home/solarise/releases/suite-20261009-slide-fan`。

独立新建并标为 Dogfood 的普通用户，真实上传 8 张合成 BMP，未 mock 生产 API：

1. 在浮卡覆盖的中部区域上下逐张扫描，预览顺序正确，节点未重建，悬停不打开切片。
2. 连续三次按原条带坐标点击，每次请求的 slide_id 均为对应切片，画布成功打开。
3. 浮卡菜单与真实画布的外部关闭、减少动态效果及 Esc 清理正常。
4. 独立 390×844 的触控上下文，手指直接点开切片，不出现悬停浮卡。
5. 全程无 JavaScript 错误。清理 8 张测试切片并禁用 Dogfood 账号；数据库复核
   无存活测试切片/有效分享。临时登录凭据已删除，没有调用 AI 或发送反馈邮件。

本地证据在 `.gate-tmp/fan-20261009/`：`results.json`、`dogfood.log`、
`public-check.log`、`cleanup.log`、`live-fan.png` 与发布日志。


## 14. 抽牌、翻面与铺开开片（2026-10-09）

最终运行提交：`cad1e8dfe971d26b5b3b95877c6c3a08f4dd4f81`。
保留 §13 的连续悬停联动；点击切片时从当前浮卡位置抽出，翻过蓝色纹理背面，
再按主视窗的实际图像尺寸铺开。采用浏览器原生 Web Animations API，无新增依赖。
抽出/翻面 620ms，铺开 340ms，交接淡出 160ms；切片请求与动画并行，不延迟请求。

交接读取 OpenSeadragon 实际图像坐标。等当前切片真正绘制出瓦片后再铺开，
Canvas/WebGL 均通过 `tiled-image-drawn` 判断，不能只依赖 metadata 的 `open` 事件。
快速切换保留原有请求序号规则，同时取消旧动画；装饰层不接收鼠标事件。
请求失败、清屏、Esc、失焦和窗口变化会移除装饰层，8 秒超时也会退出装饰层。
手机、系统减少动态效果、缩略图未就绪时直接开片。原切片菜单及键盘入口保留。

线上第一轮发现一次连续点选没有生效。独立浏览器回归进一步稳定复现：鼠标按下
原始条带，跨过 80ms 悬停延迟后弹出预览，松开落在浮层上，浏览器把 click 发给
两者的共同祖先 body，切片按钮没有收到 click。补丁在 pointerdown 清理待显示
的 hover 定时器，按住鼠标时也不新建预览。不能同时清除移出侧栏的关闭定时器，
否则点击画布后预览可能滞留；最终版本保留该关闭路径。
`press-before.log` 保存旧版失败记录，`press-after.log` 保存修复后的针对性通过记录。

最终完整检查（最终运行代码上执行）：

| 检查 | 结果 |
| --- | --- |
| Vitest | 1003 passed，2 个既有 skip |
| Playwright | 90 passed，0 failed |
| 代码/静态资源 | 语法和 diff 检查通过，镜像 96 个静态资源校验通过 |

新增回归覆盖真实瓦片被延迟时仍保留预览、瓦片就绪后完成交接、减少动画、快速
切换与迟到响应、无权限响应清理、鼠标按住 150ms 后松开仍正确点击，以及移出
后点击画布关闭预览。全量运行同时修正了测试定位假设：允许首屏全是文件夹，
先分页定位独立测试资产；外部关闭测试点击无控件的画布区域，避免固定坐标
误点空态按钮。未降低原功能断言。本轮只涉及前端，未重跑 Python 套件。

最终发布目录：`/home/solarise/releases/suite-20261009-card-deal-final`。
已推送 `admin-viewer`，生产 APP_REVISION 为上述提交。发布前用隔离生产快照验收，
新建备份并保留上一运行容器；无新迁移、无插件更新（仍为 0.4.17）。两域名
`histopilot.cn` / `histopilot.com` 的主站/分享健康检查通过，JS/CSS 与提交逐字节一致。
未切换上线的中间候选 staged 容器已移除。

最终线上 dogfood 在独立新账号真实上传 3 张合成 BMP，依次验证连续联动悬停、
三次完整抽牌/翻面/铺开/瓦片交接、跨 hover 延迟按住 150ms 后松开的连续点选、
浮卡菜单、减少动态效果、Esc，以及独立 390×844 触控上下文直接开片。全部通过，
无 JavaScript 错误。之前一轮 8 张切片的完整正常交互也通过。

三轮共创建 3 个 Dogfood 账号、19 张合成测试切片；均已删除测试切片并禁用账号。
每轮均用数据库复核无存活切片及有效分享，临时凭据均已删除。没有运行 AI 推理、
发送反馈邮件或修改真实用户资料。§11 的既有 AI 后端阻塞不属于本次修复范围。

证据目录 `.gate-tmp/deal-20261009/`：`unit.log`、`browser.log`、`press-before.log`、
`press-after.log`、`final-cutover.log`、`final-public-check.log`、`results.json`、
`dogfood.log`、`cleanup-first.log`、`cleanup-second.log`、`cleanup-final.log`。
效果录屏 `slide-card-deal.mp4` 为线上第二轮正常交互实录，使用合成测试图像；
最后的点击边界补丁不改变动画外观。首轮失败和第二轮通过记录分别保留在
`first-run/`、`second-run/`，不把重试覆盖后的结果当作首次成功。

## 15. 选片动画渲染开销优化（2026-10-09）

用户澄清观察到的是设备掉帧感，而不是确定的加载卡顿。本轮运行代码
`826fc6c784dd9a2756809e645ce7c0aa04e71890`，在现有扑克牌效果上减少每帧布局与绘制。
悬停浮卡保持固定尺寸，使用等比缩放和位移；阴影固定，邻卡层级稳定。
抽牌飞行不再逐帧修改 left/top/width/height；铺开用最终尺寸的图片层做等比缩放，
卡片边框、标题和阴影作为整体淡出。图片节点移入落地层复用已有缩略图。
保留 620ms 抽牌、340ms 铺开、160ms 交接，以及真实瓦片已绘制后才铺开的条件；
没有新增缩略图预生成、网络请求入口或资源，也没有以降低切片画质换取动画性能。

性能对比使用同一台本机的 headless Chromium、1440×900、4 倍 CPU 限速、真实
Flask + PostgreSQL 与三张已加载缩略图，未 mock API、未限速网络。每种实现独立
测量三轮，下面是中位数。悬停操作固定为三次往返、每程 30 个鼠标移动步骤；
开片覆盖一次抽牌到真实瓦片交接结束。统计来自 CDP Performance/Tracing。

| 操作 / 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 往返划选：Layout 次数 | 183 | 12 |
| 往返划选：Paint 次数 | 792 | 50 |
| 往返划选：Paint 累计耗时 | 157.270 ms | 8.730 ms |
| 往返划选：TaskDuration | 1621.220 ms | 759.068 ms |
| 单次开片：Layout 次数 | 62 | 16 |
| 单次开片：Paint 次数 | 196 | 25 |
| 单次开片：Paint 累计耗时 | 58.142 ms | 5.889 ms |
| 单次开片：TaskDuration | 696.164 ms | 400.082 ms |

这些数据证明渲染工作量下降，不能换算成用户设备的帧率提升。两版划选的 rAF
p95 都约为 16.8ms；开片仍有约 183ms 的最大帧间隔。优化后 trace 中一个约
147ms 的 animation-frame 回调来自 OpenSeadragon 首次绘图。4 倍 CPU 限速下的
headless 环境不代表用户的显卡或浏览器，因此未声称已消除所有掉帧，也未因这项
合成环境测量改写切片引擎。带宽会影响等待真实切片可交接的时间，但本轮重点是
动画本身的布局/重绘成本，不是修改加载策略。

完整 Vitest：1003 passed，2 个既有 skip；完整 Playwright：90 passed。
另外 30 项针对性浏览器测试通过。增强真实 BMP 回归：逐帧采样铺开宽高比，检查
图片等比长大；铺开中按 Esc 后原卡、背景及新图片层全部清理。连续邻卡断言改为
读取真实 getBoundingClientRect，避免依赖以 left 属性实现运动。缩略图静态
边界单测改为检查底缘与宽高比。现有慢瓦片、快速切片、迟到响应、菜单、触控和
减少动态效果回归全部保留。本轮只有前端变更，未重跑 Python 套件。

性能原始数据与本机证据：`.gate-tmp/deal-perf-20261009/` 下的 `profile.cjs`、
`baseline.json`、`optimized.json`、`optimized-trace-*.json`、`unit-final.log`、
`focused.log`、`full-e2e.log`，以及 `fan.png`、`spread.png`、`landed.png`。

运行提交已推送并部署到 `/home/solarise/releases/suite-20261009-card-perf`。
发布复用已核验的生产依赖，346 项镜像文件、96 项静态资源及全部 3 个变更运行
文件一致；隔离生产快照验收通过，0081 重复执行无变化。发布前备份和上一版本
回滚容器均保留。未新增迁移或插件更新，APP_REVISION 已核对为上述运行提交。
两域名主站/分享健康检查、JS/CSS 逐字节校验均通过（缓存版本 20261009h）。

线上独立 Dogfood 账号真实上传 8 张合成 BMP，验证整页往返扫选且浮层复用、三次
完整翻牌铺开、快速 A→B 点选、浮卡菜单与真实画布关闭、减少动态效果和 Esc、
390×844 手机触控直接打开；均通过，无 JavaScript 错误。8 张测试切片已删除，
账号已禁用；数据库复核无存活测试切片及有效分享，临时凭据已删除。没有运行
AI 推理、发送反馈邮件或修改真实用户资料。本机三个隔离测试服务也已退出并
清理临时凭据。线上证据同目录 `results.json`、`dogfood.log`、`live-fan.png`、
`live-video/`、`cleanup.log`、`public-check.log`、`cutover.log`。
