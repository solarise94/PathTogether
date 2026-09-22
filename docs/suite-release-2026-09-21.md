# Suite release — 2026-09-21

将 traffic/余额修复(F1–F4)、公开注册 + 双协议、可撤回研究授权、读片遥测(采集开关默认关闭)、研究删除执行器与管理员处置入口部署到生产 `homepc`。代码在两仓 `wip/ser8-dev`;不合并 master。部署在晚间低峰(23:30)执行,遵循 [[deploy-evening-preference]]。

## 部署版本

| 组件 | 源提交 | 生产镜像 / 包 |
| --- | --- | --- |
| PathTogether | `4323436` | `localhost/pathtogether-demo:suite-20260921` |
| HistoPilot 服务 | `26fd2a9`(未改代码) | `localhost/histopilot-demo:suite-20260921`(与 suite-20260920 同镜像 `a1c67e34`,仅换标签) |
| HistoPilot 浏览器插件 | `26fd2a9`(未变) | `releases/histopilot-0.3.4`(符号链接未动) |
| 管理插件 | `4323436` | `releases/pathtogether-admin-0.4.12`(符号链接切换) |

镜像 ID:
- PathTogether: `1df716f329885ae5ad145f90bfcc8f782b01d80c2a06e52e8285263e8dc2001f`
- HistoPilot: `a1c67e34f6277bbc9cd80beaa46ae7a43f5a775ffadd877664c2bbed538b73c3`(沿用)

管理插件 0.4.12 manifest SHA-256: `0a5897400d3f504360a21934bcf9323ff110c32eb0b5367855577067df89cbf3`(与 `source-policy.json` pin 一致)。

## 部署过程

1. 部署前检查:两仓与远端同步且干净(PT `4323436`、HP `26fd2a9`);快速回归 147 通过;生产无 `research_data_deletion_jobs` 表(全新功能,无存量冲突)。
2. 备份:`~/svs-viewer-demo-data/backups/suite-20260921/svs_demo.dump`(mode 600),restore 验证通过(临时库 59 迁移,已删)。
3. prepare:`~/releases/suite-20260921/deploy.py prepare`(由 20260920 脚本改 TAG/ROOT),staged 容器配置逐项比对一致。
4. cutover:无活动会话/排队任务;停旧改名 `*-pre-suite-20260921`;先起 sidecar 再起 platform;`deploy.py cutover` 报 SIDECAR_HEALTH / PLATFORM_HEALTH 均 ok(sidecar=reachable)。
5. 迁移:`ensure_schema` 将生产从 59 → **65**(0060–0065),新建 agreement_documents、user_agreement_acceptances、user_research_consents、user_research_consent_history、research_subjects、research_viewing_sessions、research_viewer_events、research_conversation_items、research_data_deletion_jobs 等表。
6. **发布双协议文稿**(运行时,经 `agreement_store`):`ensure_builtin_documents()` 登记 + `publish_document()` 发布 user_agreement / research_sharing / model_providers 三份 `2026-09-21-v4`(published)。
7. **切注册模式为 public**(运行时,存 `platform_settings.registration_mode`,重启持久):`set_registration_mode("public")`;fail-closed 前置与文稿检查均通过(effective_mode=public,doc_failures=[],precondition_failures=[])。
8. **管理插件符号链接切换** `pathtogether-admin`: `releases/pathtogether-admin-0.4.10` → `releases/pathtogether-admin-0.4.12`(0.4.10 目录保留用于回滚)。

## 生产验证

- 入口:`/`、`/demo`、`/legal/user-agreement`、`/legal/research-sharing`、`/legal/model-providers` 均 200;`/admin` 302→login;双 `/healthz` ok(平台 sidecar=reachable)。
- 注册页(public):恰好两个 checkbox——必选《用户协议与数据处理说明》、自愿《数据共享与软件改进协议》,**均不预勾选**(grep 无 checked);链接指向 /legal;配额提示"每日最多 5 个新自助账号"。
- 研究采集开关 `RESEARCH_COLLECTION_ENABLED` 未设置(默认关闭);`/api/research/viewer-events`、`/api/research/viewing-sessions` 未登录 401(未放行)。
- 管理插件 0.4.12 已加载,UI 含"研究删除"页;`/api/admin/v1/research-deletion-jobs` 未登录 401(受保护)。
- 研究删除执行器 `research_deletion_worker --loop` 在容器内运行(PID 确认)。
- 余额自动检查:仅读快照,最新一条 deepseek ¥470.99(2026-09-21 15:43),未为验证发起真实官方请求。

## 回滚

- 回滚脚本:`ssh homepc 'python3 ~/releases/suite-20260921/deploy.py rollback'`(恢复 histopilot 插件符号链接与旧容器,复查双 healthz;不回滚 DB、不删新用户数据)。
- 保留的回滚容器:`histopilot-demo-pre-suite-20260921`、`pathtogether-demo-pre-suite-20260921`。
- 管理插件回滚:`cd ~/svs-viewer-demo-data/plugins && ln -sfn releases/pathtogether-admin-0.4.10 pathtogether-admin`。
- 注意:回滚容器不会自动撤销 registration_mode=public 与文稿 published(均为 DB 设置);如需回退注册模式,在容器内 `settings_store.set_registration_mode("invite_only")`(或此前生产使用的模式)。

## 未解决事项 / 备注

- 研究采集保持关闭,未产生研究数据;开启前建议单独做授权撤回的生产级演练。
- `REGISTRATION_ADMIN_EMAIL` 未显式设置,经 `TEST_APPLICATION_ADMIN_EMAIL`(已配置)兼容回退;如需分离可后续单独配置。
- 注册管理员通知邮件的实际投递依赖 SMTP 配置;本次未触发真实注册,未发送通知邮件。
- 研究删除任务若终态 failed(重试 5 次耗尽),需管理员在"研究删除"页人工"重新执行删除";无直接置 completed 入口(设计如此)。
