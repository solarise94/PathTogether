# 分支收尾、主页与 COS fast 发布（2026-10-13）

状态：**代码已合入并推送；主页与 fast 下载通道已上线，线上 Dogfood 通过。**

## 发布身份与行为

- PathTogether [PR #13](https://github.com/solarise94/PathTogether/pull/13) 已 squash 合入 `main@a5d08a53`。镜像源码为 `3817ff5b2c99e07ed96e2baff54c8de88ab0c044`，该提交与合并后的 main 内容完全相同。
- 运行镜像 `localhost/pathtogether-demo:cos-fast-enable-20261013`，ID `cd873e283d6300cb9743126e14ab37e99f2925150b31bd4c9a85b62b41b054b4`。启用转发时复用已验收的同一不可变镜像，只改变两项环境变量：`COS_DOWNLOAD_TRANSPORT=fast` 与 `COS_FAST_RELAY_URL=https://goapi.solarise94.fun`。
- 当前 fast 维护入口是用户配置的 `tencentfast`，目录 `/opt/gokapi-relay`。下载地址与 COS 上游使用域名/DNS，不固定主机 IP。容器内业务桶域名解析到同地域内网；两段流量费用按用户确认的配置前提，本轮未独立核对账单。
- Go 源码已托管到私有 [gokapi-relay](https://github.com/solarise94/gokapi-relay)。运行镜像 `localhost/gokapi-relay:closeout-20261013-82897d9`，ID `sha256:88b2a50b0875551d6bc1b18e51b078d9fcef5c949ba9febe5261aad16a7c9251`。编译源为 `82897d9`；后续 `f4377f5` 只修竞态测试，`223fe83` 更新 Nginx 防缓存配置及说明，Nginx 配置已在 fast 应用。
- HistoPilot 运行版本、后台插件及 SAM2 服务沿用既有发布；本轮没有调用付费模型。

主页协作演示现在保持 SVG 等比缩放、卡片与视窗完整；两个主页提供“中国线路 / 国际线路”链接。界面语言在账户菜单内独立切换。用户已用手动线路选择替代 IP 地域跳转，旧地域跳转方案关闭。

COS 仍由浏览器按原流程直传；平台下载经 fast 的专用通道。fast 不保存切片、不注册分享文件，不计入 Gokapi 原有 200 GiB 配额，也不建立曾经提议的 100G 缓存。维持 4 路并发和 64 KiB 复制缓冲，忙时立即拒绝，不排队或自动改为公网直连。原 Go 服务的用户、环境、端口、挂载、512 MB 内存、CPU shares、重启策略及共享业务保留。

## Review 与测试

| 检查 | 实际结果 |
|---|---|
| PT PR 完整 CI | 四个 job 全绿，见 [run 38356522961](https://github.com/solarise94/PathTogether/actions/runs/38356522961) |
| CI pytest | 3094 passed / 115 skipped，0 failed |
| CI JS | 1115 passed / 2 skipped |
| CI 浏览器 | 128 passed |
| PG canary | 3 passed |
| 合并后 main CI | [run 38358876837](https://github.com/solarise94/PathTogether/actions/runs/38358876837) 成功 |
| Go CI | relay vet/race、既有模拟 AWS 全量测试及 Docker build 成功，最终 [run 38363175082](https://github.com/solarise94/gokapi-relay/actions/runs/38363175082) |

本机新建 Python 3.12 环境按当前 requirements 安装了 Zarr3/tifffile 依赖。全量首轮 3094 passed / 113 skipped / 2 failed；两项失败是主页测试按第一个 `</div>` 截取账户面板，新增语言区影响截取。将语言区放到账户操作之后，保留原断言；对应套件 53 passed，完整 CI 随后通过。之前 relay 分支的 127 项依赖环境失败没有在正确环境中重现。

Go review 补了上游必须返回 206 的检查和禁止跨源重定向；HTTP 200 携带正确 Range 头的回归在旧代码失败，新代码拒绝。新仓库干净 CI 暴露的主程序源码忽略规则、旧测试默认值/请求夹具及完成计数观察竞态也已修复，详见 Go 仓库 `docs/review-closeout-20261013.md`。fast 的全局公共 CDN 缓存头在两条专用 location 中明确覆盖为 no-store，同时关闭代理缓存、缓冲与临时文件。

## 真实验收与 Dogfood

- 两份真实 COS 对象版本分别完成三个范围读取，总计 32,000,246 字节，整对象 SHA 与来源一致；版本、范围、长度均符合合同。经当前公共 fast 域名实测耗时分别 1.243 s、0.546 s。该小型合成对象不能代表多 GB 切片吞吐或长期稳定性。
- Go 指标确认成功请求、origin/egress 字节一致，无上游错误、错版本、重试和遗留 inflight。401 探针是主动匿名测试，不是业务失败。
- 真实独立 Dogfood 用户经过 HTTP 登录、浏览器直传 COS、平台通过 fast 下载、校验入库、远端清理；288,881 字节的实际转换器合成 OME-TIFF 成功发布。
- 浏览器再次真实登录并打开 `/app?slide=<id>`，info、thumbnail 与实际瓦片均为 200，画面成功显示，没有“切片不存在”或 JS 异常；匿名访问该私有切片被拒绝。
- 另一次伪“转换器 BigTIFF”未带转换器来源标记，按合同被 `convert_in_browser` 拒绝并清理；这项负例不作为产品缺陷或成功转换统计。
- `.cn` 与 `.com` 的 320/390/1440px 主页均无横向溢出，线路当前状态、语言切换、三个协作阶段与等比 SVG 正常；两个域名各 105 个公开静态资源哈希核对通过。
- 已删除本轮合成切片与两个手工 COS 探针版本；四个测试 ingestion 均已终态且远端 cleaned。Dogfood 账户 used/reserved 均为 0，已禁用并保留审计；没有删除真实用户或历史数据。

没有宣称覆盖 Mac 实机帧率、真实 OS 保存对话框、全部扫描仪变体或真实付费 AI；这些边界与后续任务继续保留。

## 演练、发现与恢复

先使用生产 PostgreSQL 的隔离副本、替换所有可写挂载并关闭后台业务 worker，核对既有 0083、公开发现页面、ID-only 读图和源码资源。每次启用参数变化均重新演练并冻结配置指纹，再做零在途任务检查和停写前后备份。本轮没有新增迁移或改变预算上限。

重复演练曾复用上次生成的私有统计密钥，新访问事件使全表校验发生变化；改用新的隔离目录重跑，保护检查通过。首次发布后裸静态 URL 短暂命中旧缓存，当前新版本资源 URL 与裸 URL 均重新核对通过，线上浏览器亦通过。

设计里的历史 Ubuntu 主机与当前 DNS 入口不同。发现后已恢复历史主机原镜像、Compose 管理标签、环境和挂载，并移除新提供的凭证文件；用户配置 `tencentfast` 后才发布到实际 fast。

Compose 会按 service 标签重建改名容器，因此 fast 回滚使用从原 inspect 创建、没有 Compose service 标签的独立 standby，已核对镜像、环境、资源和挂载。Go 更新未改变数据库 schema，回滚保留新产生的数据，不用旧备份覆盖生产。

回滚顺序：

1. 在 homepc `/home/solarise/releases/cos-fast-enable-20261013` 执行 `python3 deploy.py rollback`，平台恢复同代码的显式 direct 传输；随后核对健康、预算和用户读图。
2. 如需回滚 Go，在 `tencentfast` 的 `/opt/gokapi-relay/releases/slide-relay-20261013` 执行 `python3 tencentfast-rollback.py rollback`，恢复旧镜像与 Nginx。平台必须先切回 direct。

用户提供的 CSV 只用于写私有密钥文件；没有提交 Git、镜像层或证据日志。私有部署目录与密钥文件限制权限，容器只读挂载。已对两仓 tracked archive 检查该凭证字符串，均无匹配。

## 分支与文档收尾

共关闭 40 个已完成的本地功能/发布分支（PT 32、HP 8），另将 2 个已被 PR #11 取代、服务器上已删除的 Dependabot 残留引用归档并清理。已完成分支先推送 `archive/20261013/<原分支名>` 标记再关闭；工作树切为同一提交的 detached 状态，目录及其未提交文档、旧测试产物未自动迁移或删除。根 PT 工作树 17 个修改与 21 个未跟踪文档/部署文件已额外保全到本轮私有目录，根 `slide-id-refactor` 保留。

漏合的 `fd0f7832` 复核关闭记录已进入主线；历史 Logo/统计发布文档补入，过时状态页链接到本记录。剩余实现任务见 [实现索引](followup/remaining-work-implementation-index-20261013.md)，重点是 [文件夹体验](followup/folder-experience-implementation-20261013.md)、[JPEG2000 转换](followup/jpeg2000-browser-conversion-implementation-20261013.md) 和矩形取消传播。本轮没有代替用户实施这些功能。

本机全部新测试数据、日志、截图与报告位于 `/mnt/ssd2/tests/closeout-20261013-WUvNlu/`。关键文件：`pt-ci-full.log`、`live-homepage-check.json`、`real-fast-live-check.json`、`dogfood-fast-upload-check.json`、`live-viewer-check.json`、`dogfood-cleanup-final.log`、`branch-closure-results.json` 及额外归档清单。所有运行在启动前设置了 TMPDIR/TMP/TEMP 与显式输出位置。
