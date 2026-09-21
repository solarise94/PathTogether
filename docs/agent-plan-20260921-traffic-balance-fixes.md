# Agent 执行方案：访问统计、登录污染与 DeepSeek 余额更新

版本：2026-09-21-v1。目标仓库：PathTogether；HistoPilot 仅核验已有恢复修复。

本文把 [排查报告](investigation-20260921-traffic-balance-customer-errors.md) 转为可执行任务。当前任务只交付文档；后续 agent 接到“执行本方案”时按批次推进。本文不代表本次已经部署，也不能用其中的执行指令覆盖新的用户要求。

## 0. 开始前确认

1. 在两个仓库分别运行 `git status --short`、`git diff --stat`，读取届时适用的 AGENTS.md。不要清空工作区、覆盖未提交改动或把历史记录当最新状态。
2. PathTogether 工作区已有上一轮未提交实现：`app.py`、`site_stats_store.py`、管理插件 UI/manifest/pin 及相关测试。先审查 diff，再增量补齐，不能重复添加相同线程、路由或按钮。
3. 生产数据核验与修改分开：调查 SQL 使用只读事务、超时和固定时间窗；结果只保留聚合、错误类别和必要时间点。禁止把原始 IP、完整 UA、用户对话、图像、密钥或生产环境文件放入仓库。
4. 第一轮实现不改变历史 site_visit_events，不把“未命中词表”批量改成“真人”。不重新发起客户 AI 任务、上传或百度转存。
5. 新注册和研究授权由 [独立实现方案](agent-plan-20260921-registration-consent-research.md) 管理，不顺手夹进本批次。

## 1. 已确认的问题与目标

| 编号 | 已确认事实 | 完成后的行为 |
| --- | --- | --- |
| F1 来源列表 | 5／2／1 与生产 DB 一致，最近外部来源在 9 月 18 日；概览停留时不刷新 | 显示近 30 天口径、更新时间、可见页自动刷新和手动刷新；真实无新增时保持数值 |
| F2 分类 | OkHttp 被直接判为爬虫；UA 自报无法验证真人或 Google | 收窄规则，标签只表达证据强度，旧数据保持原分类 |
| F3 login 污染 | 扫描不存在的 WordPress/.env 路径后被 302 到 login；最大扫描来源产生 382 次 login | 不存在的路径 404，真实受保护路径仍保持原鉴权流程 |
| F4 余额 | 旧快照 ¥474.93；排查时官方 GET 返回 ¥473.81 | 每小时自动更新；失败留旧值、5 分钟退避，显示上次检查及错误，不依赖管理员打开页面 |
| F5 客户异常 | 一个 user 会话两次 assistant 尾消息恢复报错；已有生产修复 | 验证现有补齐逻辑与真实 wire 配对，不重复实现、不重跑生产会话 |
| F6 未定因异常 | 两次 /api/conversions 502 为上游拒绝连接；一上传停在约 42% | 保留有边界的诊断结论，不把未知原因写成已修复或主动退出 |

## 2. F1：统计刷新与口径

落点：`site_stats_store.dashboard_stats/_top_list/_recent`、`app.py` 的 owner site-stats API、`plugins/pathtogether-admin/ui/{index.html,main.js}`。

执行：

- 保留入口域名白名单、30 天外部来源窗口、排除 suspected_bot 的查询规则。来源名不能由计数反推，不将 direct 自动改为搜索引擎。
- 来源区显示“近 30 天累计；没有新的外部来源时数值不会增加”；direct 解释包含直接进入、站内跳转和浏览器未发送来源。
- 使用服务端 `generated_at` 显示数据更新时间。可见且桥接有效的概览每 60 秒更新；切换到其他页、文档隐藏、桥失效时不发统计请求；同一请求在途时合并重复刷新。
- 成功后的下一次刷新失败，保留旧结果并标注“可能已过期”，不能展示为最新成功，也不能用空数组覆盖。首次加载失败给可理解的不可用状态；无权限不暴露数据。
- 明确“最近访问为不同访客时间混排，只记录公开页面，不是用户访问路径”；不要向界面暴露 daily_visitor_hash，也不将同网段标识当账号。
- 产品说明中区分“已登录”和“匿名、未命中爬虫规则”。不宣称匿名数据是已验证真人。

验收：fake bridge 模拟成功→超时→恢复、隐藏页、切页、失效桥、在途双击；检查数值只跟随成功响应变化。固定来源样本测试同时验证 Google Referer 的正常浏览器被保留、Googlebot 被排除、跨入口同站跳转归 direct。

## 3. F2/F3：分类和路由

落点：`site_stats_store._classify_user_agent`、`app.py::_require_auth`。

执行：

- 复核当前 v3 改动：移除 OkHttp 独立规则；泛化标记收窄，正常产品名中间子串不能命中，SomeCompanyCrawler 等词尾仍可识别。
- 保留具体爬虫产品名优先、现有登录态与 bot 分类契约。不通过“已登录”推断所有同网段请求是真人。
- UI 说明 Googlebot 等是 UA 声明，未验证来源；不为了清理污染而新建 IP 黑名单或保存完整 UA。
- 在认证重定向前检查 Flask 路由不存在的 404 情形；直接 404。不要将所有 `url_rule is None` 一律当 404，保留方法不允许等已有行为。
- 实际 `/app` 未登录仍跳 `/login?next=/app`，已登录用户仍进工作台；不存在路径不能清除待激活 enrollment 会话。

验收：`/wp-admin/install.php`、`/.env`、`/.git/config` 返回 404 且无 Location；真实页面仍鉴权；未知 API 与方法错误有明确一致的响应。测试 Sogou 浏览器/爬虫、WhatsApp 浏览器/预览、OkHttp、正常 Chrome、Googlebot、RobotPhone、SomeCompanyCrawler、空 UA。

发布后观察新污染趋势，不改写旧榜单。用明确测试 UA 做极少量冒烟，将冒烟时刻记入发布记录，避免把自己的验证当客户行为。

## 4. F4：余额检查服务

落点：`app.py` 中 `_refresh_provider_balance`、`_run_provider_balance_check_once`、`_start_provider_balance_check_thread`、余额 GET/POST；`billing_store` 现有快照表；审计表；管理插件费用页。

基准策略：默认启用，每 300 秒检查；没有快照或快照满 3600 秒才请求官方余额端点；不发起模型推理，不修改用户额度。环境开关 `PROVIDER_BALANCE_AUTO_CHECK_ENABLED=0` 关闭自动检查。

执行顺序：

1. 审查手动刷新共享实现：手动 POST 保留 owner、预览限制、CSRF 与 60 秒成功/10 秒失败限速；后台走系统上下文，不模拟用户登录。
2. 自动检查在所有依赖函数定义后启动。测试环境关闭真实线程；生产多 worker / 重启 / preload 场景应验证只有运行中的进程负责调度，不能让 preload 父进程初始化造成线程丢失或重复。
3. 使用 PostgreSQL advisory lock 原子争用；锁内重读最近成功快照、最近自动尝试时间。不能只依赖进程内变量。
4. 官方请求有明确 timeout；失败不伪造零，不删除旧快照。金额继续用 Decimal 精确转换，不经 float。
5. `billing.provider_balance_auto_check` 保存安全结果码、检查时间、HTTP 状态和 system actor。失败原因覆盖未配置、网络、上游拒绝、非法响应、快照写入失败；不在审计放响应正文、请求头或解密 key。
6. 当前草实现的后台异常仅有通用日志：执行 agent 必须检查异常路径是否也能留下安全错误类别；DB 不可写时至少保留节流日志，不能假称检查成功。
7. 验证后台与手动同时触发不会造成失控请求。必要时将共享互斥下沉到服务层；不要通过移除手动鉴权解决并发。
8. GET 保留 `snapshot` 和 `age_seconds`，增加 `auto_check` 配置、最近时间及结果。卡片分别表达“最近成功余额”和“上次检查结果”，避免手动成功后仍把旧自动错误当作当前余额失效。
9. 缺配置或暂停自动检查应显示真实状态；不把 disabled、暂无快照、检查失败、余额不可用混为零元。

验收必须包含：

- 官方成功、4xx、5xx、timeout、非 JSON、CNY 条目缺失、非法金额、数据库失败。
- 首次启动、新鲜快照跳过、过期刷新、失败 5 分钟退避、到点恢复。
- 两个独立进程争用、多 worker 重启、手动/后台并发、锁释放。
- 失败保留旧值与原 observed_at；日志和 API 不含密钥；接口权限制约不变。

现有官方协议参考：[DeepSeek 查询余额](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/)。本方案不增加外部通知。

## 5. F5/F6：异常复核

HistoPilot 当前 `src/agent-runner.ts` 已有 `ensureContinuableTail`，生产 dist 也核实存在。先跑 `resume-assistant-tail` 和 `resume-wire-pairing` 两组，不因旧日志再创建第二套续跑逻辑。

复查近期事件时：

- 按真实会话/请求关联 agent_error、paused、finished 和 no_final_usage；不能按公共访问列表相邻位置拼用户路径。
- aborted 单独归为“中断，原因未定”，主动停止和网络错误有证据才细分。
- 两次转换 502 保留“上游连接拒绝”结论；如当时发布日志未保留，不猜测原因。
- 上传停在 42% 不自动重试、不删除；若下一轮要补遥测，采用必要错误类别和请求关联，不能借此先启用研究用途的行为采集。

输出按“确认故障／已有修复／原因未定／证据覆盖不足”分类，并写明窗口与入口范围。

## 6. 测试命令与工件

在 PathTogether 执行：

```bash
.venv/bin/python -m pytest tests/test_site_stats.py tests/test_admin_api_v1.py tests/test_account_auth.py tests/test_login_id.py tests/test_email_verify_activation.py tests/test_admin_plugin.py -q
npx vitest run tests/js/admin-plugin-ui.test.ts tests/js/admin-bridge.test.ts tests/js/admin-host-boot.test.ts
git diff --check
```

在 HistoPilot 执行：

```bash
npx vitest run test/resume-assistant-tail.test.ts test/resume-wire-pairing.test.ts
```

新增后台多进程、恢复退避、preload 测试应加入测试集后再交付。不要把上一轮通过记录视为后续改动的自动验证。

管理插件已有 0.4.11 草实现。发布时检查届时最新版本，按实际变更更新版本、三个 UI fileHashes 和 `plugins/source-policy.json` 中 manifest SHA256；不能修改文件后沿用旧 pin。

工件至少包含：最终 diff、测试摘要、文件和镜像摘要、插件版本/pin、配置差异、冒烟记录、回滚命令及未解决事项。

## 7. 更新和回滚次序

先完成可审查实现、测试与发布包。是否切生产依据届时用户授权，不把“写方案”视为当前部署指令；也不因普通实现取舍重复询问许可。

获得更新授权后：

1. 记录当前镜像/插件入口/pin，保护配置备份权限，不将 runtime.env 提交或打印。
2. 新后端兼容旧 UI；先部署后端并检查健康、DB、worker、余额开关和新后台记录，再更新管理插件及 pin。
3. 两个入口各验证 home/login/app、未知路径404、owner权限、统计新鲜度。读取真实余额只做一次必要验证，不改用户账本。
4. 等待一个可控检查周期或使用系统执行入口验证自动检查；保存最近成功/失败时间，确认多个 worker 只产生一轮自动请求。
5. 后端或 UI 出现新故障：先关闭 `PROVIDER_BALANCE_AUTO_CHECK_ENABLED` 停自动请求，再按范围回滚插件或镜像；保留合法快照、审计、用户数据，不通过删除生产行回滚。
6. 发布记录明确：新行为从何时生效、历史统计未重算、F6 哪些原因仍未确定。

## 完成判据

F1–F4 的行为和测试全部满足；F5 已核验而非重复改写；F6 无夸大结论；工件可复核；若要求部署则两个入口和后台调度均验证成功。只完成文档或本地代码时必须明确标注未上线。
